"""Meta webhook receiver — Instagram DMs land here and become customers/conversations/messages.

  GET  /webhooks/instagram  -> subscription handshake (echo hub.challenge)
  POST /webhooks/instagram  -> events; signature-checked, idempotent on message id (mid)

Handles both `object: "instagram"` (entry id = the Instagram professional account id) and
`object: "page"` (entry id = the Page id), because a Page-backed app can be delivered
either. Each entry's `messaging` and `standby` arrays are processed; `standby` events are
what arrive while another app holds the thread, and they belong in the same inbox.

Robustness decisions:

* **One savepoint per event.** Previously the whole delivery shared a single transaction,
  so a single malformed event rolled back every message in the batch — and because the
  handler still answered 200, Meta never redelivered it. The data was simply gone. Now a
  bad event costs exactly that event.
* **A total failure asks Meta to retry.** If nothing at all could be written the handler
  replies 503 rather than 200. A *partial* failure still replies 200, deliberately: a
  permanently poisonous event must not trap the delivery in an infinite retry loop that
  ends with Meta disabling the webhook. Duplicate deliveries are harmless either way.
* **No Graph calls on the request path.** Profile lookups are handed to a background task
  so the response is not held open for a third-party API (see `customer_profiles`).
* Non-message events (read, delivery, reaction, postback) are ignored on purpose.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import PlainTextResponse
from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.services.customer_profiles import enrich_customers
from app.services.meta import instagram
from app.services.meta.ids import normalize_message_id

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = logging.getLogger(__name__)

# `instagram` is what Instagram messaging actually sends; `page` is accepted so a
# Messenger/Page delivery is not silently dropped.
SUPPORTED_OBJECTS = ("instagram", "page")


@dataclass
class IngestResult:
    """Outcome of one event: "stored", "ignored" (not a message), or "unknown_account"."""

    status: str
    # (igsid, social_account_id) when the customer still needs a profile lookup.
    enrichment: Optional[tuple[str, str]] = None


@router.get("/instagram", response_class=PlainTextResponse)
async def verify(
    hub_mode: Annotated[str, Query(alias="hub.mode")] = "",
    hub_token: Annotated[str, Query(alias="hub.verify_token")] = "",
    hub_challenge: Annotated[str, Query(alias="hub.challenge")] = "",
) -> str:
    if hub_mode != "subscribe" or hub_token != settings.meta_webhook_verify_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad verify token")
    return hub_challenge


async def _resolve_account(db: AsyncSession, entry_id: str) -> Optional[SocialAccount]:
    """Find the account an entry belongs to.

    An Instagram entry carries the IG account id, a Page entry carries the Page id, so
    both columns are checked. Active accounts only — a disconnected account should stop
    accumulating traffic rather than quietly keep a thread alive.
    """
    if not entry_id:
        return None
    return (
        await db.execute(
            select(SocialAccount)
            .where(
                SocialAccount.status == "active",
                or_(
                    SocialAccount.external_account_id == entry_id,
                    SocialAccount.external_page_id == entry_id,
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _get_or_create_customer(
    db: AsyncSession, account: SocialAccount, igsid: str
) -> Customer:
    """Find or create the customer, tolerating a concurrent insert of the same IGSID.

    `ON CONFLICT DO NOTHING` + re-select rather than select-then-insert: two overlapping
    deliveries for a brand-new customer would otherwise collide on
    `uq_customer_external_user` and lose one of the messages.
    """
    inserted = (
        await db.execute(
            pg_insert(Customer)
            .values(tenant_id=account.tenant_id, external_user_id=igsid)
            .on_conflict_do_nothing(index_elements=["tenant_id", "external_user_id"])
            .returning(Customer.id)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return await db.get(Customer, inserted)

    return (
        await db.execute(
            select(Customer).where(
                Customer.tenant_id == account.tenant_id,
                Customer.external_user_id == igsid,
            )
        )
    ).scalar_one()


async def _get_or_create_conversation(
    db: AsyncSession, account: SocialAccount, customer: Customer
) -> Conversation:
    """Find or create the customer's open thread for this account (race-safe)."""
    inserted = (
        await db.execute(
            pg_insert(Conversation)
            .values(
                tenant_id=account.tenant_id,
                customer_id=customer.id,
                social_account_id=account.id,
                channel=instagram.PLATFORM,
                status="open",
            )
            .on_conflict_do_nothing(
                index_elements=["customer_id", "social_account_id"],
                index_where=text("status = 'open'"),
            )
            .returning(Conversation.id)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return await db.get(Conversation, inserted)

    return (
        await db.execute(
            select(Conversation).where(
                Conversation.customer_id == customer.id,
                Conversation.social_account_id == account.id,
                Conversation.status == "open",
            )
        )
    ).scalar_one()


async def ingest_event(
    db: AsyncSession, ig_account_id: str, event: dict
) -> IngestResult:
    """Store one messaging event.

    Adds to the session but does not commit; the caller owns the transaction. Never
    raises for a payload it cannot use — only for genuine infrastructure failures.
    """
    msg = event.get("message")
    if not msg or not msg.get("mid") or msg.get("is_deleted"):
        return IngestResult("ignored")

    account = await _resolve_account(db, ig_account_id)
    if account is None or not account.access_token_encrypted:
        log.warning("instagram webhook for unknown/unlinked account id=%s", ig_account_id)
        return IngestResult("unknown_account")

    is_echo = bool(msg.get("is_echo"))
    # An echo is outbound: the counterpart lives in `recipient`, not `sender`.
    party = event.get("recipient") if is_echo else event.get("sender")
    igsid = str((party or {}).get("id") or "")
    if not igsid:
        log.warning("instagram webhook event without a participant id: %s", event)
        return IngestResult("ignored")

    customer = await _get_or_create_customer(db, account, igsid)
    convo = await _get_or_create_conversation(db, account, customer)

    attachments = [a for a in (msg.get("attachments") or []) if isinstance(a, dict)]
    if attachments:
        media_url, message_type = instagram.attachment_media(attachments[0])
    else:
        media_url, message_type = None, "text"

    await db.execute(
        pg_insert(Message)
        .values(
            conversation_id=convo.id,
            # Same normalisation the backfill applies, so a message imported from the
            # Conversations API and its later webhook echo resolve to one row.
            external_message_id=normalize_message_id(msg["mid"]),
            sender_type="agent" if is_echo else "customer",
            sender_customer_id=None if is_echo else customer.id,
            message_type=message_type,
            content=msg.get("text"),
            media_url=media_url,
            meta={"attachments": attachments} if attachments else {},
        )
        .on_conflict_do_nothing(
            index_elements=["external_message_id"],
            index_where=Message.external_message_id.isnot(None),
        )
    )

    ts = event.get("timestamp")
    convo.last_message_at = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        if isinstance(ts, (int, float))
        else datetime.now(timezone.utc)
    )

    # Ask for a profile lookup only while we still know nothing about this customer.
    enrichment = (
        (igsid, str(account.id)) if not customer.name else None
    )
    return IngestResult("stored", enrichment)


@router.post("/instagram")
async def receive(
    request: Request,
    background: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    raw = await request.body()
    if not instagram.verify_webhook_signature(
        raw, request.headers.get("x-hub-signature-256", "")
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad signature")

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body"
        )

    if payload.get("object") not in SUPPORTED_OBJECTS:
        return {"status": "ignored", "object": payload.get("object")}

    stored = ignored = failed = 0
    pending: list[tuple[str, str]] = []

    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        for channel in ("messaging", "standby"):
            for event in entry.get(channel) or []:
                if not isinstance(event, dict):
                    continue
                try:
                    # A savepoint per event: its failure rolls back only this event.
                    async with db.begin_nested():
                        result = await ingest_event(db, entry_id, event)
                except Exception:  # noqa: BLE001 — isolate per-event failure
                    failed += 1
                    log.exception(
                        "webhook event failed (entry=%s channel=%s)", entry_id, channel
                    )
                    continue
                if result.status == "stored":
                    stored += 1
                    if result.enrichment:
                        pending.append(result.enrichment)
                else:
                    ignored += 1

    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — the batch is not durable
        await db.rollback()
        log.exception("webhook commit failed; asking Meta to redeliver")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Ingest failed"
        ) from exc

    if pending:
        background.add_task(enrich_customers, pending)

    if failed and not stored:
        # Nothing was persisted, so this is not a duplicate-delivery risk — ask for a
        # retry instead of dropping the batch.
        log.error("instagram webhook delivery fully failed (%d events)", failed)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Ingest failed"
        )

    return {"status": "ok", "stored": stored, "ignored": ignored, "failed": failed}
