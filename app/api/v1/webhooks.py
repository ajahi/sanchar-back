"""Meta webhook receiver — Instagram DMs and WhatsApp messages become customers/conversations/messages.

  GET  /webhooks/instagram  -> subscription handshake (echo hub.challenge)
  POST /webhooks/instagram  -> events; signature-checked, idempotent on message id (mid / wamid)

WhatsApp (object "whatsapp_business_account") posts to the same URL — it's the callback set on
the Meta app — and goes through ingest_whatsapp_message, which follows the same 5 steps.

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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import case, func, select
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
from app.services.reply_pipeline import run_auto_reply

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
        # Earlier lookup may have failed/returned empty (stale thread, transient error) — retry
        # rather than leave the row nameless forever.
        if customer.name is None and customer.external_username is None:
            profile = await instagram.fetch_customer_profile(token, igsid)
            if profile:
                customer.name = profile.get("name")
                customer.external_username = profile.get("username")
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
        channel=account.platform,
    )
    db.add(convo)
    await db.flush()
    return convo, True


def graph_attachment(msg: dict) -> tuple[str, str | None]:
    """(message_type, media_url) of a Graph conversation message's first attachment.

    Graph shapes differ from the webhook's {type, payload.url}: image_data / video_data / file_url.
    """
    att = ((msg.get("attachments") or {}).get("data") or [{}])[0]
    media = att.get("image_data") or att.get("video_data") or {}
    url = media.get("url") or att.get("file_url")
    if not url:
        return "text", None
    if "image_data" in att:
        return "image", url
    if "video_data" in att:
        return "video", url
    return (att.get("mime_type") or "").split("/")[0] or "file", url


async def backfill_recent_conversations(
    db: AsyncSession, account: SocialAccount, token: str, limit: int = 5
) -> None:
    """Seed the inbox from Graph on connect so the dashboard isn't empty until the next DM.

    ponytail: Graph message ids and webhook mids are the same value for Instagram, so the
    external_message_id unique index dedupes overlap; if they ever differ you get one duplicate
    per message that arrives during the login window.
    """
    me = account.external_account_id
    for conv in await instagram.fetch_recent_conversations(token, limit):
        others = [p for p in (conv.get("participants") or {}).get("data", []) if p.get("id") != me]
        if not others:
            continue
        customer = await _get_or_create_customer(db, account, others[0]["id"], token)
        convo, _ = await _get_or_create_conversation(db, account, customer)
        for msg in (conv.get("messages") or {}).get("data", []):
            is_agent = (msg.get("from") or {}).get("id") == me
            message_type, media_url = graph_attachment(msg)
            stmt = pg_insert(Message).values(
                conversation_id=convo.id,
                external_message_id=msg["id"],
                sender_type="agent" if is_agent else "customer",
                sender_customer_id=None if is_agent else customer.id,
                message_type=message_type,
                content=msg.get("message") or None,
                media_url=media_url,
                created_at=datetime.fromisoformat(msg["created_time"]),
            )
            # Existing row: take a fresh media url when Graph has one (fills rows stored before
            # attachments were fetched, and renews CDN urls that expire); otherwise keep what's there.
            has_media = stmt.excluded.media_url.isnot(None)
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["external_message_id"],
                    index_where=Message.external_message_id.isnot(None),
                    set_={
                        "media_url": func.coalesce(stmt.excluded.media_url, Message.media_url),
                        "message_type": case(
                            (has_media, stmt.excluded.message_type), else_=Message.message_type
                        ),
                    },
                )
            )
        updated = datetime.fromisoformat(conv["updated_time"]) if conv.get("updated_time") else None
        if updated and (convo.last_message_at is None or updated > convo.last_message_at):
            convo.last_message_at = updated


async def ingest_event(db: AsyncSession, ig_account_id: str, event: dict) -> tuple | None:
    """Store one Instagram `messaging` event, following the 5-step flow above.

    Returns (conversation_id, message_id) when a NEW customer message was stored (the caller queues
    an auto-reply for it), else None. A NEW echo (a person answered from the Instagram app) is stored
    as an 'agent' message, which pauses the bot for a while (see reply_pipeline.human_active); our own
    replies echo back under the id we stored, so they are not stored twice and do not pause it.
    """
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
    inserted = await db.scalar(
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
        .returning(Message.id)
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
    return (convo.id, inserted) if inserted and not is_echo else None


async def stamp_whatsapp_event(db: AsyncSession, phone_number_id: str, waba_id: str) -> None:
    """Note that Meta reached us for this number (any event: message or status). The Channels
    page shows it as the live webhook check. entry.id on a WhatsApp event is the WhatsApp
    Business Account id, which the page needs to look up the webhook subscription."""
    account = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.platform == "whatsapp",
                SocialAccount.external_account_id == phone_number_id,
            )
        )
    ).scalar_one_or_none()
    if account is not None:
        account.meta = {
            **(account.meta or {}),
            "waba_id": waba_id,
            "last_webhook_at": datetime.now(timezone.utc).isoformat(),
        }


async def ingest_whatsapp_message(
    db: AsyncSession, phone_number_id: str, contacts: list[dict], msg: dict
) -> tuple | None:
    """Store one WhatsApp `messages[]` item — same steps and return value as ingest_event.

    The customer is keyed by wa_id (their phone number, digits only). WhatsApp puts the display
    name in the same payload (contacts[].profile.name), so there is no profile fetch.
    """
    account = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.platform == "whatsapp",
                SocialAccount.external_account_id == phone_number_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        return None

    # 2. customer
    wa_id = msg["from"]
    name = next(
        ((c.get("profile") or {}).get("name") for c in contacts if c.get("wa_id") == wa_id), None
    )
    customer = (
        await db.execute(
            select(Customer).where(
                Customer.tenant_id == account.tenant_id, Customer.external_user_id == wa_id
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        customer = Customer(
            tenant_id=account.tenant_id,
            external_user_id=wa_id,
            external_username=f"+{wa_id}",
            phone=f"+{wa_id}",
            name=name,
        )
        db.add(customer)
        await db.flush()
    elif name and not customer.name:
        customer.name = name

    convo, convo_is_new = await _get_or_create_conversation(db, account, customer)  # step 3

    # 4. message. Media comes as an id, not a url (needs a Graph fetch) — keep the raw item in
    # meta and show the caption for now.
    kind = msg.get("type", "text")
    part = msg.get(kind) or {}
    content = part.get("body") if kind == "text" else part.get("caption")
    inserted = await db.scalar(
        pg_insert(Message)
        .values(
            conversation_id=convo.id,
            external_message_id=msg["id"],
            sender_type="customer",
            sender_customer_id=customer.id,
            message_type=kind,
            content=content,
            meta={"whatsapp": msg},
        )
        .on_conflict_do_nothing(
            index_elements=["external_message_id"],
            index_where=Message.external_message_id.isnot(None),
        )
        .returning(Message.id)
    )

    # 5. dashboard sort key + notification on a brand-new thread
    ts = msg.get("timestamp")
    convo.last_message_at = (
        datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else datetime.now(timezone.utc)
    )
    if convo_is_new:
        await create_notification(
            db,
            event="message_received",
            tenant_id=account.tenant_id,
            source_table="conversations",
            entity_id=convo.id,
            subject=f"New WhatsApp chat from {customer.name or customer.phone}",
            message=content,
            payload={"conversation_id": str(convo.id), "customer_id": str(customer.id)},
        )
    return (convo.id, inserted) if inserted else None


@router.post("/instagram")
async def receive(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)], bg: BackgroundTasks
) -> dict:
    raw = await request.body()

    log.info(">>> IG WEBHOOK HIT <<<")
    log.info("Headers: %s", dict(request.headers))
    log.info("Raw body: %s", raw.decode("utf-8", errors="replace"))

    if not instagram.verify_webhook_signature(raw, request.headers.get("x-hub-signature-256", "")):
        log.warning("Bad signature")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad signature")

    payload = json.loads(raw)
    log.info("Parsed payload:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))

    obj = payload.get("object")
    if obj not in ("instagram", "whatsapp_business_account"):
        return {"status": "ignored"}
    to_reply: dict = {}  # conversation_id -> its newest new message (one reply per chat per batch)
    try:
        for entry in payload.get("entry", []):
            if obj == "instagram":
                for event in entry.get("messaging", []):
                    if job := await ingest_event(db, str(entry.get("id", "")), event):
                        to_reply[job[0]] = job[1]
                continue
            # WhatsApp: entry.changes[].value holds messages[] (inbound) and statuses[] (delivery
            # reports on our sends). ponytail: statuses are only logged above; store them on the
            # message when the UI needs sent/delivered/failed.
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")
                await stamp_whatsapp_event(db, phone_number_id, str(entry.get("id", "")))
                for msg in value.get("messages", []):
                    if job := await ingest_whatsapp_message(db, phone_number_id, value.get("contacts", []), msg):
                        to_reply[job[0]] = job[1]
        await db.commit()
    except Exception:  # noqa: BLE001 — log it, never make Meta retry forever
        log.exception("%s webhook ingest failed", obj)
        await db.rollback()
        return {"status": "ok"}
    for message_id in to_reply.values():  # after the commit: the task reads the stored rows
        bg.add_task(run_auto_reply, message_id)
    return {"status": "ok"}

