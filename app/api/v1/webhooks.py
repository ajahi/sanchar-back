"""Meta webhook receiver — Instagram DMs land here and become customers/conversations/messages.

  GET  /webhooks/instagram  -> subscription handshake (echo hub.challenge)
  POST /webhooks/instagram  -> events; signature-checked, idempotent on message id (mid)
Only `message` events are stored (text + attachments; is_echo = a reply the business sent
from the Instagram app). read/reaction/postback events are ignored. Always answers 200 so
Meta does not retry forever on a payload we cannot handle.
"""
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
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.services.meta.store import get_or_create_conversation

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


async def ingest_event(db: AsyncSession, ig_account_id: str, event: dict) -> None:
    """Store one `messaging` event; no-op for non-message events and unknown accounts."""
    msg = event.get("message")
    if not msg or not msg.get("mid") or msg.get("is_deleted"):
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

    is_echo = bool(msg.get("is_echo"))
    igsid = event["recipient"]["id"] if is_echo else event["sender"]["id"]
    token = decrypt_token(account.access_token_encrypted)
    customer, convo = await get_or_create_conversation(db, account, igsid, token)

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
    ts = event.get("timestamp")
    convo.last_message_at = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    )


@router.post("/instagram")
async def receive(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    raw = await request.body()
    if not instagram.verify_webhook_signature(raw, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad signature")
    payload = await request.json()
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
