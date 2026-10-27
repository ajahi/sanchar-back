"""Inbox — list a tenant's conversations, read a thread, reply as an agent over Instagram, Messenger or WhatsApp."""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_token
from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.campaign import Campaign, CampaignLink
from app.models.customer import Customer
from app.models.handover import HandoverEvent
from app.models.message import Message
from app.models.sales_lead import SalesLead
from app.models.social_account import SocialAccount
from app.models.user import User
from app.api.v1.shop_media import own_media
from app.api.v1.webhooks import SYNC_LOCK_MINUTES, run_history_sync
from app.schemas.conversation import ConversationOut, MessageOut, ReplyIn
from app.schemas.shop_media import SendMediaIn
from app.services.meta import CHANNELS

log = logging.getLogger(__name__)
router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _own_conversation(
    db: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> tuple[Conversation, Customer]:
    # Tenant-scoped fetch (spec §17).
    row = (
        await db.execute(
            select(Conversation, Customer)
            .join(Customer, Customer.id == Conversation.customer_id)
            .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return row.tuple()


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
    status_: Annotated[str, Query(alias="status")] = "open",
) -> list[ConversationOut]:
    # Needs attention = a handover to a person exists that no agent message follows. A human reply
    # (dashboard or the Instagram app) clears it, so there is no flag to reset.
    needs_attention = exists().where(
        HandoverEvent.conversation_id == Conversation.id,
        HandoverEvent.to_mode == "human",
        ~exists().where(
            Message.conversation_id == HandoverEvent.conversation_id,
            Message.sender_type == "agent",
            Message.created_at > HandoverEvent.created_at,
        ),
    )
    rows = await db.execute(
        select(
            Conversation, Customer, needs_attention.label("needs_attention"), SalesLead.phone,
            SalesLead.meta["order"], SalesLead.status, Campaign.name, func.coalesce(CampaignLink.ad_title, CampaignLink.external_id),
            CampaignLink.kind, CampaignLink.external_id,
        )
        .join(Customer, Customer.id == Conversation.customer_id)
        .outerjoin(SalesLead, SalesLead.customer_id == Conversation.customer_id)
        .outerjoin(CampaignLink, CampaignLink.id == Conversation.campaign_link_id)
        .outerjoin(Campaign, Campaign.id == CampaignLink.campaign_id)
        .where(Conversation.tenant_id == tenant.id, Conversation.status == status_)
        .order_by(Conversation.last_message_at.desc().nulls_last())
    )
    return [
        ConversationOut(
            id=c.id,
            channel=c.channel,
            status=c.status,
            mode=c.mode,
            needs_attention=attention,
            last_message_at=c.last_message_at,
            customer_id=cu.id,
            customer_name=cu.name,
            customer_username=cu.external_username,
            lead_phone=lead_phone,
            lead_order=lead_order,
            lead_status=lead_status,
            campaign_name=campaign_name,
            ad_title=ad_title,
            ad_kind=ad_kind,
            ad_code=ad_code,
        )
        for c, cu, attention, lead_phone, lead_order, lead_status, campaign_name, ad_title, ad_kind, ad_code
        in rows.all()
    ]


@router.post("/sync-history", dependencies=[Depends(require_role("owner", "admin"))])
async def sync_history(
    tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)], bg: BackgroundTasks
) -> dict:
    """"Load older chats": for each Instagram/Messenger account of this tenant, pull older threads from
    Meta in the background (50 per click, continuing where the last click stopped). Safe to repeat:
    messages are stored once. The inbox's 5s refresh shows the chats as they arrive."""
    accounts = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.tenant_id == tenant.id,
                SocialAccount.platform.in_(("instagram", "facebook")),
                SocialAccount.access_token_encrypted.isnot(None),
            )
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    started = running = 0
    for acc in accounts:
        since = (acc.meta or {}).get("history_syncing_at")
        if since and now - datetime.fromisoformat(since) < timedelta(minutes=SYNC_LOCK_MINUTES):
            running += 1
            continue
        acc.meta = {**(acc.meta or {}), "history_syncing_at": now.isoformat()}
        bg.add_task(run_history_sync, acc.id)
        started += 1
    await db.commit()  # the lock is saved before the background task starts
    return {"started": started, "already_running": running}


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[Message]:
    await _own_conversation(db, tenant.id, conversation_id)
    rows = await db.execute(
        select(Message, User.name)
        .outerjoin(User, User.id == Message.sender_agent_id)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    out = []
    for msg, name in rows.all():
        msg.sender_name = name  # transient attr read by MessageOut
        out.append(msg)
    return out


async def _store_sent(db: AsyncSession, convo: Conversation, user, sent: dict, **values) -> Message:
    """Store what a person sent from the dashboard under Meta's message id. Instagram / Messenger echo it back as a
    webhook, often before the send call returns (always for a photo), so that echo may already be stored under this
    id as an unattributed 'agent' message: take that row over (who sent it, our media url) instead of inserting a
    second one, which would fail on the unique id."""
    stmt = pg_insert(Message).values(
        conversation_id=convo.id, external_message_id=sent.get("message_id"), sender_type="agent",
        sender_agent_id=user.id, **values,
    )
    msg = (
        await db.scalars(
            stmt.on_conflict_do_update(
                index_elements=["external_message_id"],
                index_where=Message.external_message_id.isnot(None),
                set_={"sender_agent_id": user.id, **({"media_url": values["media_url"]} if "media_url" in values else {})},
            ).returning(Message),
            execution_options={"populate_existing": True},
        )
    ).one()
    convo.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    return msg


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def reply(
    conversation_id: uuid.UUID,
    body: ReplyIn,
    tenant: CurrentTenant,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Message:
    convo, customer = await _own_conversation(db, tenant.id, conversation_id)
    account = (
        await db.get(SocialAccount, convo.social_account_id) if convo.social_account_id else None
    )
    if account is None or not account.access_token_encrypted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"{convo.channel} account not connected"
        )
    # Every channel's send_text takes (token, business account id, customer id, text) and returns {message_id}.
    send_text = CHANNELS[account.platform].send_text
    try:
        sent = await send_text(
            decrypt_token(account.access_token_encrypted),
            account.external_account_id,
            customer.external_user_id,
            body.text,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.response.text[:500])
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{account.platform} unreachable"
        )

    msg = await _store_sent(db, convo, user, sent, content=body.text)
    msg.sender_name = user.name
    return msg


@router.post(
    "/{conversation_id}/media",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def send_media(
    conversation_id: uuid.UUID,
    body: SendMediaIn,
    tenant: CurrentTenant,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Message:
    """Send one of the shop's images (see /shop-media) to the customer as an agent message."""
    convo, customer = await _own_conversation(db, tenant.id, conversation_id)
    media = await own_media(db, tenant.id, body.media_id)
    account = (
        await db.get(SocialAccount, convo.social_account_id) if convo.social_account_id else None
    )
    if account is None or not account.access_token_encrypted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"{convo.channel} account not connected"
        )
    send_image = getattr(CHANNELS[account.platform], "send_image", None)
    if send_image is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Sending images isn't supported on WhatsApp yet")
    try:
        sent = await send_image(
            decrypt_token(account.access_token_encrypted),
            account.external_account_id,
            customer.external_user_id,
            media.url,
        )
    except httpx.HTTPStatusError as exc:
        log.warning("%s image send failed (%s) for %s: %s", account.platform, exc.response.status_code, media.url, exc.response.text[:500])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.response.text[:500])
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{account.platform} unreachable")

    msg = await _store_sent(db, convo, user, sent, message_type="image", media_url=media.url)
    msg.sender_name = user.name
    return msg
