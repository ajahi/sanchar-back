"""Auto-reply pipeline: one stored customer message in -> (maybe) one AI reply out.

The webhook stores the message and commits, then queues run_auto_reply(message_id) as a background
task, so Meta gets its 200 right away. auto_reply() decides, generates (services/auto_reply.py),
sends on the channel, stores the reply, and hands over to a human when the model or an error says so.

Stays silent (leaves the chat to the team) when: the tenant hasn't switched AI on, the chat is already
with a human, the tenant has no knowledge yet, the message has no text, or a newer customer message
exists (that one gets the reply, and its history includes this one).

ponytail: FastAPI BackgroundTasks, so a server restart loses in-flight replies; move to a jobs table
+ worker if that ever matters. A human echo can race our own reply's echo (see webhooks.ingest_event).
"""
import logging
from datetime import datetime, timezone

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
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services.auto_reply import build_system_prompt, generate_reply
from app.services.meta import instagram, whatsapp
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

HISTORY = 6  # previous messages given to the model


async def hand_over(db: AsyncSession, convo: Conversation, *, reason: str, triggered_by: str) -> None:
    """AI -> human. The dashboard's Handover count reads conversations.mode, so flipping it is the
    whole signal; the event row is the audit trail. Only AI-triggered handovers notify the team:
    when a person took over themselves they already know."""
    if convo.mode != "ai":
        return
    convo.mode = "human"
    db.add(
        HandoverEvent(
            conversation_id=convo.id, from_mode="ai", to_mode="human", reason=reason, triggered_by=triggered_by
        )
    )
    if triggered_by == "ai":
        await create_notification(
            db,
            event="handover",
            tenant_id=convo.tenant_id,
            source_table="conversations",
            entity_id=convo.id,
            subject="A customer needs a person",
            message=reason,
            payload={"conversation_id": str(convo.id), "reason": reason},
        )


async def build_knowledge(db: AsyncSession, tenant_id) -> str:
    """The tenant's whole knowledge as one text block (the CAG context). Tenant-scoped, always."""
    docs = (
        await db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.status == "active")
            .order_by(KnowledgeDocument.created_at)
        )
    ).scalars()
    return "\n\n".join(f"## {d.title}\n{d.content.strip()}" for d in docs if d.content and d.content.strip())


async def _generate(system: str, history: list[tuple[str, str]], text: str):
    """One retry: Groq occasionally answers 400/5xx/timeouts, and the customer is waiting."""
    try:
        return await generate_reply(system, history, text)
    except httpx.HTTPError:
        return await generate_reply(system, history, text)


async def auto_reply(db: AsyncSession, message_id) -> None:
    msg = await db.get(Message, message_id)
    if msg is None or msg.sender_type != "customer" or not (msg.content or "").strip():
        return
    convo = await db.get(Conversation, msg.conversation_id)
    tenant = await db.get(Tenant, convo.tenant_id)
    if convo.mode != "ai" or not tenant.ai_auto_reply:
        return

    newer = await db.execute(
        select(Message.id)
        .where(
            Message.conversation_id == convo.id,
            Message.sender_type == "customer",
            Message.created_at > msg.created_at,
        )
        .limit(1)
    )
    if newer.first():
        return

    account = await db.get(SocialAccount, convo.social_account_id) if convo.social_account_id else None
    customer = await db.get(Customer, convo.customer_id)
    if account is None or not account.access_token_encrypted or customer is None:
        return
    knowledge = await build_knowledge(db, tenant.id)
    if not knowledge:
        log.info("auto-reply skipped: tenant %s has no knowledge yet", tenant.id)
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

    system = build_system_prompt(tenant.name, knowledge, contact=tenant.owner_phone or "")
    try:
        result = await _generate(system, history, msg.content)
    except Exception:  # noqa: BLE001 — any LLM failure: say nothing wrong, let a person answer
        log.exception("LLM failed for conversation %s", convo.id)
        await hand_over(db, convo, reason="llm_error", triggered_by="ai")
        return

    send_text = whatsapp.send_text if account.platform == "whatsapp" else instagram.send_text
    try:
        sent = await send_text(
            decrypt_token(account.access_token_encrypted),
            account.external_account_id,
            customer.external_user_id,
            result.reply,
        )
    except httpx.HTTPError:
        log.exception("sending AI reply failed for conversation %s", convo.id)
        await hand_over(db, convo, reason="send_failed", triggered_by="ai")
        return

    # Stored under Meta's message id so our own echo (Instagram sends one back) hits the unique index
    # and is not mistaken for a person replying. If the echo won the race, relabel its row as ours.
    stmt = pg_insert(Message).values(
        conversation_id=convo.id,
        external_message_id=sent.get("message_id"),
        sender_type="ai",
        message_type="text",
        content=result.reply,
        ai_generated=True,
        meta={"model": settings.llm_model, "tokens": result.tokens, "handover": result.handover, "reason": result.reason},
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["external_message_id"],
            index_where=Message.external_message_id.isnot(None),
            set_={"sender_type": "ai", "ai_generated": True},
        )
    )
    convo.last_message_at = datetime.now(timezone.utc)
    if result.handover:
        await hand_over(db, convo, reason=result.reason or "ai_handover", triggered_by="ai")


async def run_auto_reply(message_id) -> None:
    """Background-task entry: own session, own commit, never raises into the request."""
    try:
        async with async_session_factory() as db:
            await auto_reply(db, message_id)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("auto-reply failed for message %s", message_id)
