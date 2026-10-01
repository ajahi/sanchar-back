"""Auto-reply pipeline: one stored customer message in -> (maybe) one AI reply out.

The webhook stores the message and commits, then queues run_auto_reply(message_id) as a background
task, so Meta gets its 200 right away.

Human takeover is a timer, as in the Messenger MVP: while a person has replied in this chat within
the last HUMAN_PAUSE_MINUTES (from the Instagram app via an echo, or from the dashboard), the bot
stays quiet; every human reply refreshes the window, and once they go idle the bot takes over again.
"Human replied recently" is read from the stored messages (sender_type = 'agent'), so there is no
mode to flip back and nothing to lose on a restart.

ponytail: FastAPI BackgroundTasks, so a server restart loses in-flight replies; move to a jobs table
+ worker if that ever matters.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.handover import HandoverEvent
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.shop_media import ShopMedia
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services.auto_reply import build_system_prompt, generate_reply
from app.services.meta import instagram, whatsapp
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

HISTORY = 6  # previous messages given to the model
PHOTO_REASON = "customer_sent_image"
PHOTO_HOLDING_REPLY = (
    "Tapai ko photo pauna payo. Hamro team le herera chadai reply garnu hunchha. "
    "(Thanks for the photo, our team will check it and reply shortly.)"
)


async def human_active(db: AsyncSession, conversation_id) -> bool:
    """True while a person has replied in this chat within the pause window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.human_pause_minutes)
    found = await db.execute(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.sender_type == "agent",
            Message.created_at > cutoff,
        )
        .limit(1)
    )
    return found.first() is not None


async def build_knowledge(db: AsyncSession, tenant_id) -> str:
    """The tenant's whole shop context as one text block (the CAG context). Tenant-scoped, always."""
    docs = (
        await db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.status == "active")
            .order_by(KnowledgeDocument.created_at)
        )
    ).scalars()
    text = "\n\n".join(f"## {d.title}\n{d.content.strip()}" for d in docs if d.content and d.content.strip())
    if not text:
        return text
    # Items the admin marked sold out in the media panel; the Products text alone can't know.
    sold_out = (
        await db.execute(
            select(ShopMedia.title).where(
                ShopMedia.tenant_id == tenant_id, ShopMedia.in_stock.is_(False), ShopMedia.title.isnot(None)
            )
        )
    ).scalars().all()
    if sold_out:
        text += "\n\nSOLD OUT right now (do not offer these): " + "; ".join(sold_out)
    return text


async def _generate(system: str, history: list[tuple[str, str]], text: str) -> str:
    """One retry: the API occasionally answers 400/5xx/timeouts, and the customer is waiting."""
    try:
        return await generate_reply(system, history, text)
    except httpx.HTTPError:
        return await generate_reply(system, history, text)


async def _store_ai_message(db: AsyncSession, convo: Conversation, sent: dict, text: str) -> None:
    """Stored under Meta's message id so our own echo (Instagram sends one back) hits the unique index
    and is not mistaken for a person replying. If the echo won the race, relabel its row as ours."""
    stmt = pg_insert(Message).values(
        conversation_id=convo.id,
        external_message_id=sent.get("message_id"),
        sender_type="ai",
        message_type="text",
        content=text,
        ai_generated=True,
        meta={"model": settings.llm_model},
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["external_message_id"],
            index_where=Message.external_message_id.isnot(None),
            set_={"sender_type": "ai", "ai_generated": True},
        )
    )
    convo.last_message_at = datetime.now(timezone.utc)


async def hand_over_photo(db: AsyncSession, convo, tenant, account, customer) -> None:
    """A customer's photo (usually "do you have this?") is for a person: the bot does not guess from
    pixels. Record the handover, ping the dashboard, tell the customer someone will look.
    ponytail: no mode flip, the reply timer takes over once a person answers; a sticky mode if photos
    must keep the bot silent until someone clicks 'hand back'."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.human_pause_minutes)
    recent = await db.execute(
        select(HandoverEvent.id)
        .where(
            HandoverEvent.conversation_id == convo.id,
            HandoverEvent.reason == PHOTO_REASON,
            HandoverEvent.created_at > cutoff,
        )
        .limit(1)
    )
    if recent.first() is not None:  # several photos in a row: one handover, one holding reply
        return
    db.add(
        HandoverEvent(
            conversation_id=convo.id, from_mode="ai", to_mode="human",
            reason=PHOTO_REASON, triggered_by="system", notes="Customer sent an image",
        )
    )
    who = customer.name or customer.external_username or "a customer"
    await create_notification(
        db,
        event="handover_created",
        tenant_id=tenant.id,
        source_table="conversations",
        entity_id=convo.id,
        subject=f"{who} sent a photo",
        message="A customer sent an image. The bot did not answer; please reply.",
        payload={"conversation_id": str(convo.id), "customer_id": str(customer.id), "reason": PHOTO_REASON},
    )
    send_text = whatsapp.send_text if account.platform == "whatsapp" else instagram.send_text
    try:
        sent = await send_text(
            decrypt_token(account.access_token_encrypted),
            account.external_account_id,
            customer.external_user_id,
            PHOTO_HOLDING_REPLY,
        )
    except httpx.HTTPError:
        log.exception("sending photo holding reply failed for conversation %s", convo.id)
        return
    await _store_ai_message(db, convo, sent, PHOTO_HOLDING_REPLY)


async def auto_reply(db: AsyncSession, message_id) -> None:
    msg = await db.get(Message, message_id)
    if msg is None or msg.sender_type != "customer":
        return
    is_photo = msg.message_type == "image"
    if not is_photo and not (msg.content or "").strip():
        return
    convo = await db.get(Conversation, msg.conversation_id)
    tenant = await db.get(Tenant, convo.tenant_id)
    if not tenant.ai_auto_reply:
        log.info("auto-reply skipped: AI is off for tenant %s", tenant.id)
        return
    if await human_active(db, convo.id):
        log.info("auto-reply skipped: a person is handling conversation %s", convo.id)
        return

    account = await db.get(SocialAccount, convo.social_account_id) if convo.social_account_id else None
    customer = await db.get(Customer, convo.customer_id)
    if account is None or not account.access_token_encrypted or customer is None:
        log.info("auto-reply skipped: conversation %s has no connected account", convo.id)
        return
    if is_photo:
        await hand_over_photo(db, convo, tenant, account, customer)
        return
    knowledge = await build_knowledge(db, tenant.id)
    if not knowledge:
        log.info("auto-reply skipped: tenant %s has no shop context yet", tenant.id)
        return

    prior = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == convo.id,
                Message.id != msg.id,
                Message.created_at <= msg.created_at,
                Message.content.isnot(None),
            )
            .order_by(Message.created_at.desc())
            .limit(HISTORY)
        )
    ).scalars()
    history = [("customer" if m.sender_type == "customer" else "assistant", m.content) for m in reversed(list(prior))]

    try:
        reply = await _generate(build_system_prompt(knowledge), history, msg.content)
    except Exception:  # noqa: BLE001 — stay quiet rather than send something wrong; a person can answer
        log.exception("LLM failed for conversation %s", convo.id)
        return
    if not reply:
        return

    # A person usually jumps in DURING the second or two of generation: re-check right before sending.
    if await human_active(db, convo.id):
        log.info("auto-reply dropped: a person took over conversation %s while generating", convo.id)
        return

    send_text = whatsapp.send_text if account.platform == "whatsapp" else instagram.send_text
    try:
        sent = await send_text(
            decrypt_token(account.access_token_encrypted),
            account.external_account_id,
            customer.external_user_id,
            reply,
        )
    except httpx.HTTPError:
        log.exception("sending AI reply failed for conversation %s", convo.id)
        return

    await _store_ai_message(db, convo, sent, reply)


async def run_auto_reply(message_id) -> None:
    """Background-task entry: own session, own commit, never raises into the request."""
    try:
        async with async_session_factory() as db:
            await auto_reply(db, message_id)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("auto-reply failed for message %s", message_id)
