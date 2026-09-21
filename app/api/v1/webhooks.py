"""Meta webhook receiver — Instagram DMs become customers/conversations/messages.

  GET  /webhooks/instagram  -> subscription handshake (echo hub.challenge)
  POST /webhooks/instagram  -> events; signature-checked, idempotent on message id (mid)

The flow for one inbound message (ingest_event):
  1. is this a message we store?        skip unsends + non-message events
  2. find or create the customer        by tenant_id + Instagram-scoped user id (igsid)
  3. find or create the conversation     one open thread per customer per account
  4. insert the message                 linked to the conversation, idempotent on mid
  5. bump last_message_at (dashboard sort) + notify admins if the thread is brand new
"""
import json
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decrypt_token
from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.services.meta import instagram
from app.services.notifications import create_notification

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = logging.getLogger(__name__)


@router.get("/instagram", response_class=PlainTextResponse)
async def verify(
    hub_mode: Annotated[str, Query(alias="hub.mode")] = "",
    hub_token: Annotated[str, Query(alias="hub.verify_token")] = "",
    hub_challenge: Annotated[str, Query(alias="hub.challenge")] = "",
) -> str:
    if hub_mode != "subscribe" or hub_token != settings.instagram_webhook_verify_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad verify token")
    return hub_challenge


# --- step 2: customer -------------------------------------------------------
async def _get_or_create_customer(
    db: AsyncSession, account: SocialAccount, igsid: str, token: str
) -> Customer:
    customer = (
        await db.execute(
            select(Customer).where(
                Customer.tenant_id == account.tenant_id,
                Customer.external_user_id == igsid,
            )
        )
    ).scalar_one_or_none()
    if customer is not None:
        return customer

    profile = await instagram.fetch_customer_profile(token, igsid)
    customer = Customer(
        tenant_id=account.tenant_id,
        external_user_id=igsid,
        name=profile.get("name"),
        external_username=profile.get("username"),
    )
    db.add(customer)
    await db.flush()
    return customer


# --- step 3: conversation ---------------------------------------------------
async def _get_or_create_conversation(
    db: AsyncSession, account: SocialAccount, customer: Customer
) -> tuple[Conversation, bool]:
    # ponytail: select-then-insert. The `uq_open_conversation` partial index catches the
    # rare concurrent first-message race (this delivery fails -> Meta retries -> retry finds it).
    convo = (
        await db.execute(
            select(Conversation).where(
                Conversation.customer_id == customer.id,
                Conversation.social_account_id == account.id,
                Conversation.status == "open",
            )
        )
    ).scalar_one_or_none()
    if convo is not None:
        return convo, False

    convo = Conversation(
        tenant_id=account.tenant_id,
        customer_id=customer.id,
        social_account_id=account.id,
        channel="instagram",
    )
    db.add(convo)
    await db.flush()
    return convo, True


async def ingest_event(db: AsyncSession, ig_account_id: str, event: dict) -> None:
    """Store one Instagram `messaging` event, following the 5-step flow above."""
    # 1. keep only real messages (an unsend, or a read/reaction/postback, is not stored).
    match event:
        case {"message": {"is_deleted": True}}:
            return
        case {"message": {"mid": str()} as msg}:
            pass
        case _:
            return

    account = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.platform == "instagram",
                SocialAccount.external_account_id == ig_account_id,
            )
        )
    ).scalar_one_or_none()
    if account is None or not account.access_token_encrypted:
        return

    # is_echo -> a reply the business sent from the Instagram app; the customer is the
    # *recipient* of that echo, and the *sender* of a normal inbound message.
    is_echo = bool(msg.get("is_echo"))
    igsid = event["recipient"]["id"] if is_echo else event["sender"]["id"]
    token = decrypt_token(account.access_token_encrypted)

    customer = await _get_or_create_customer(db, account, igsid, token)          # step 2
    convo, convo_is_new = await _get_or_create_conversation(db, account, customer)  # step 3

    # 4. the message row, linked to the conversation, idempotent on Meta's mid.
    attachments = msg.get("attachments") or []
    first = attachments[0] if attachments else {}
    await db.execute(
        pg_insert(Message)
        .values(
            conversation_id=convo.id,
            external_message_id=msg["mid"],
            sender_type="agent" if is_echo else "customer",
            sender_customer_id=None if is_echo else customer.id,
            message_type=first.get("type", "text"),
            content=msg.get("text"),
            media_url=(first.get("payload") or {}).get("url"),
            meta={"attachments": attachments} if attachments else {},
        )
        .on_conflict_do_nothing(
            index_elements=["external_message_id"],
            index_where=Message.external_message_id.isnot(None),
        )
    )

    # 5. dashboard sort key, + one notification when a brand-new customer thread opens.
    ts = event.get("timestamp")
    convo.last_message_at = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    )
    if convo_is_new and not is_echo:
        await create_notification(
            db,
            event="message_received",
            tenant_id=account.tenant_id,
            source_table="conversations",
            entity_id=convo.id,
            subject=f"New Instagram chat from {customer.name or customer.external_username or 'a customer'}",
            message=msg.get("text"),
            payload={"conversation_id": str(convo.id), "customer_id": str(customer.id)},
        )


@router.post("/instagram")
async def receive(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    raw = await request.body()

    log.info(">>> IG WEBHOOK HIT <<<")
    log.info("Headers: %s", dict(request.headers))
    log.info("Raw body: %s", raw.decode("utf-8", errors="replace"))

    if not instagram.verify_webhook_signature(raw, request.headers.get("x-hub-signature-256", "")):
        log.warning("Bad signature")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad signature")

    payload = json.loads(raw)
    log.info("Parsed payload:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))

    if payload.get("object") != "instagram":
        return {"status": "ignored"}
    try:
        for entry in payload.get("entry", []):
            for event in entry.get("messaging", []):
                await ingest_event(db, str(entry.get("id", "")), event)
        await db.commit()
    except Exception:  # noqa: BLE001 — log it, never make Meta retry forever
        log.exception("instagram webhook ingest failed")
        await db.rollback()
    return {"status": "ok"}

