"""Historical Instagram conversation import ("backfill") and resync.

Webhooks only deliver messages that arrive *after* a Page is subscribed, so a
newly-connected account shows an empty inbox until someone happens to write in. This
module closes that gap by crawling `/{page-id}/conversations?platform=instagram` and
persisting every existing thread, participant and message.

Design notes worth keeping in mind:

**Idempotency has to survive two independent sources of truth.** The same message can
arrive from the Conversations API during a backfill and from a webhook moments later.
Meta's message id is the primary key for that reconciliation, but the id returned by the
Conversations API is not guaranteed to be identical to the webhook `mid`, so every
message is also checked against a natural key (timestamp + sender + body). The DB partial
unique index on `external_message_id` remains the last line of defence for genuine races.

**A bad thread must not sink the run.** Each conversation is imported inside its own
savepoint, so one malformed payload costs one thread rather than the whole import.

**Backfilled messages keep their original timestamp.** `messages.created_at` is set from
Meta's `created_time` instead of defaulting to import time — otherwise a freshly imported
history would render in the wrong chronological order.

**Imported history is ordered, and only the newest thread stays open.** Meta returns
threads newest-first, and `conversations` allows a single `status='open'` row per
customer+account, so the most recent thread per customer is left open and older ones are
imported as `closed` rather than being dropped.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.sync_run import SyncRun
from app.services.meta import graph, instagram
from app.services.meta.ids import normalize_message_id

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- helpers


def parse_meta_timestamp(value: Any) -> Optional[datetime]:
    """Parse a Meta timestamp: epoch seconds/ms, or ISO-8601 with `+0000`/`Z`.

    Meta is inconsistent — webhooks send epoch milliseconds while the Conversations API
    sends strings like `2026-09-17T13:01:46+0000`, which `datetime.fromisoformat` only
    learned to accept in Python 3.11. Normalising here keeps every caller simple.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # "+0000" -> "+00:00" (only when the offset has no colon already)
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _natural_key(
    created_at: Optional[datetime],
    sender_type: str,
    content: Optional[str],
    media_url: Optional[str],
) -> tuple[str, str, str, str]:
    """Source-independent identity for a message, used to skip cross-source duplicates."""
    return (
        created_at.isoformat() if created_at else "",
        sender_type,
        (content or "").strip(),
        media_url or "",
    )


@dataclass
class SyncStats:
    """Running totals for one import; mirrored onto the `sync_runs` row."""

    conversations_seen: int = 0
    conversations_created: int = 0
    conversations_failed: int = 0
    messages_created: int = 0
    messages_skipped: int = 0
    truncated: bool = False

    def as_columns(self) -> dict[str, int]:
        return {
            "conversations_seen": self.conversations_seen,
            "conversations_created": self.conversations_created,
            "conversations_failed": self.conversations_failed,
            "messages_created": self.messages_created,
            "messages_skipped": self.messages_skipped,
        }


# ------------------------------------------------------------------ conversation import


async def _resolve_customer(
    db: AsyncSession,
    account: SocialAccount,
    payload: dict[str, Any],
    *,
    fetch_profiles: bool,
    token: str,
) -> Optional[Customer]:
    """Find or create the customer in a thread.

    The business's own participant is identified by comparing against the account id, so
    the remaining participant is the customer. A thread with no second participant cannot
    be attributed and is skipped rather than guessed at.
    """
    participants = ((payload.get("participants") or {}).get("data")) or []
    business_id = str(account.external_account_id or "")

    other: Optional[dict[str, Any]] = None
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        pid = str(participant.get("id") or "")
        if pid and pid != business_id:
            other = participant
            break
    if other is None:
        return None

    igsid = str(other.get("id"))
    username = other.get("username")

    customer = (
        await db.execute(
            select(Customer).where(
                Customer.tenant_id == account.tenant_id,
                Customer.external_user_id == igsid,
            )
        )
    ).scalar_one_or_none()

    if customer is None:
        name = None
        if fetch_profiles:
            profile = await instagram.fetch_customer_profile(igsid, token)
            name = profile.get("name")
            username = profile.get("username") or username
        customer = Customer(
            tenant_id=account.tenant_id,
            external_user_id=igsid,
            name=name,
            external_username=username,
        )
        db.add(customer)
        await db.flush()
    elif username and not customer.external_username:
        customer.external_username = username

    return customer


async def _find_or_create_conversation(
    db: AsyncSession,
    account: SocialAccount,
    customer: Customer,
    external_id: str,
    updated_at: Optional[datetime],
) -> tuple[Conversation, bool]:
    """Resolve the thread for a Meta conversation id, in three deliberate steps.

    1. Exact match on the stored Meta thread id — the normal resync path.
    2. Adopt an open thread that a webhook created before it knew the Meta thread id
       (webhook payloads carry no conversation id). Without this, connecting an account
       and then importing would produce two threads for the same conversation.
    3. Otherwise create it. If the customer already has an open thread, this one is
       historical and is imported as `closed` so the one-open-thread-per-customer
       constraint holds and nothing is lost.
    """
    convo = (
        await db.execute(
            select(Conversation).where(
                Conversation.social_account_id == account.id,
                Conversation.external_conversation_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if convo is not None:
        if updated_at and (convo.last_message_at is None or updated_at > convo.last_message_at):
            convo.last_message_at = updated_at
        return convo, False

    adoptable = (
        await db.execute(
            select(Conversation).where(
                Conversation.customer_id == customer.id,
                Conversation.social_account_id == account.id,
                Conversation.status == "open",
                Conversation.external_conversation_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if adoptable is not None:
        adoptable.external_conversation_id = external_id
        if updated_at and (
            adoptable.last_message_at is None or updated_at > adoptable.last_message_at
        ):
            adoptable.last_message_at = updated_at
        await db.flush()
        return adoptable, False

    already_open = (
        await db.execute(
            select(Conversation.id)
            .where(
                Conversation.customer_id == customer.id,
                Conversation.social_account_id == account.id,
                Conversation.status == "open",
            )
            .limit(1)
        )
    ).first() is not None

    convo = Conversation(
        tenant_id=account.tenant_id,
        customer_id=customer.id,
        social_account_id=account.id,
        channel=instagram.PLATFORM,
        external_conversation_id=external_id,
        status="closed" if already_open else "open",
        last_message_at=updated_at,
    )
    db.add(convo)
    await db.flush()
    return convo, True


async def _collect_messages(
    payload: dict[str, Any],
    conversation_id: str,
    token: str,
    *,
    max_messages: Optional[int],
) -> list[dict[str, Any]]:
    """Messages for a thread: the inline list, plus its own edge if that list was cut off.

    The inline `messages` block is capped by Meta, and it reports the truncation through
    `paging.next` rather than a count, so that is what we key off.
    """
    block = payload.get("messages") or {}
    inline = [m for m in (block.get("data") or []) if isinstance(m, dict)]
    truncated = bool((block.get("paging") or {}).get("next"))
    if not truncated:
        return inline

    merged: dict[str, dict[str, Any]] = {
        str(m["id"]): m for m in inline if m.get("id")
    }
    async for message in instagram.iter_messages(
        conversation_id, token, max_messages=max_messages
    ):
        if message.get("id"):
            merged.setdefault(str(message["id"]), message)
    return list(merged.values())


async def _insert_messages(
    db: AsyncSession,
    account: SocialAccount,
    convo: Conversation,
    customer: Customer,
    messages: list[dict[str, Any]],
    stats: SyncStats,
) -> None:
    """Insert the messages of one thread, skipping anything already stored."""
    if not messages:
        return

    existing = (
        await db.execute(
            select(
                Message.external_message_id,
                Message.created_at,
                Message.sender_type,
                Message.content,
                Message.media_url,
            ).where(Message.conversation_id == convo.id)
        )
    ).all()
    known_ids = {row[0] for row in existing if row[0]}
    known_keys = {_natural_key(row[1], row[2], row[3], row[4]) for row in existing}

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    newest: Optional[datetime] = None

    for raw in messages:
        if raw.get("is_deleted"):
            continue

        # Collapse the Graph (`m_mid.…`) and webhook (`mid.…`) spellings of a message id
        # so a backfilled row and its later webhook echo share one key.
        mid = normalize_message_id(raw.get("id"))
        created_at = parse_meta_timestamp(raw.get("created_time")) or now
        newest = created_at if newest is None or created_at > newest else newest

        from_id = str(((raw.get("from") or {}).get("id")) or "")
        # `is_echo` only exists on webhooks — Graph Message nodes have no such field — so
        # direction is inferred from the sender id, with `is_echo` as a secondary signal.
        is_business = bool(raw.get("is_echo")) or (
            bool(from_id) and from_id == str(account.external_account_id or "")
        )
        sender_type = "agent" if is_business else "customer"

        attachments = [a for a in (raw.get("attachments") or []) if isinstance(a, dict)]
        if attachments:
            media_url, message_type = instagram.attachment_media(attachments[0])
        else:
            media_url, message_type = None, "text"
        content = raw.get("message")

        key = _natural_key(created_at, sender_type, content, media_url)
        if (mid and mid in known_ids) or key in known_keys:
            stats.messages_skipped += 1
            continue
        if mid:
            known_ids.add(mid)
        known_keys.add(key)

        meta: dict[str, Any] = {"backfill": True}
        if attachments:
            meta["attachments"] = attachments

        rows.append(
            {
                "conversation_id": convo.id,
                "external_message_id": mid,
                "sender_type": sender_type,
                # Only customer messages point at a customer; agent echoes do not.
                "sender_customer_id": None if is_business else customer.id,
                "message_type": message_type,
                "content": content,
                "media_url": media_url,
                "meta": meta,
                # Preserve the original send time so imported history orders correctly.
                "created_at": created_at,
            }
        )

    if not rows:
        return

    result = await db.execute(
        pg_insert(Message)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["external_message_id"],
            index_where=Message.external_message_id.isnot(None),
        )
        .returning(Message.id)
    )
    inserted = len(result.all())
    stats.messages_created += inserted
    stats.messages_skipped += len(rows) - inserted

    if newest and (convo.last_message_at is None or newest > convo.last_message_at):
        convo.last_message_at = newest


async def _import_conversation(
    db: AsyncSession,
    account: SocialAccount,
    payload: dict[str, Any],
    token: str,
    stats: SyncStats,
    *,
    max_messages: Optional[int],
    fetch_profiles: bool,
) -> bool:
    """Import one thread. Returns True when a new conversation row was created."""
    external_id = str(payload.get("id") or "")
    if not external_id:
        return False

    customer = await _resolve_customer(
        db, account, payload, fetch_profiles=fetch_profiles, token=token
    )
    if customer is None:
        log.warning(
            "skipping conversation %s: no counterpart participant", external_id
        )
        return False

    updated_at = parse_meta_timestamp(payload.get("updated_time"))
    convo, created = await _find_or_create_conversation(
        db, account, customer, external_id, updated_at
    )
    messages = await _collect_messages(
        payload, external_id, token, max_messages=max_messages
    )
    await _insert_messages(db, account, convo, customer, messages, stats)
    return created


# ------------------------------------------------------------------------- entrypoint


async def _fail_stale_runs(
    db: AsyncSession, account_id: uuid.UUID, *, except_run_id: Optional[uuid.UUID] = None
) -> int:
    """Close out runs left `pending`/`running` by a crash or a restart.

    Without this a killed process leaves a row that looks like an import still in flight
    forever, and the API would report `running` indefinitely.
    """
    stmt = select(SyncRun).where(
        SyncRun.social_account_id == account_id,
        SyncRun.status.in_(("pending", "running")),
    )
    if except_run_id is not None:
        stmt = stmt.where(SyncRun.id != except_run_id)

    stale = (await db.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc)
    for run in stale:
        run.status = "failed"
        run.error = run.error or "abandoned: superseded by a newer run or process restart"
        run.finished_at = now
    return len(stale)


async def sync_account(
    db: AsyncSession,
    account: SocialAccount,
    *,
    trigger_source: str = "manual",
    run: Optional[SyncRun] = None,
    max_conversations: Optional[int] = None,
    max_messages: Optional[int] = None,
    fetch_profiles: bool = False,
    autocommit: bool = True,
) -> SyncRun:
    """Import (or re-import) every Instagram thread for an account.

    Always leaves a `SyncRun` row behind describing what happened, and never raises for a
    Graph or payload failure — the caller inspects `run.status` / `run.error`. Re-running
    is safe and is the intended response to a `partial` result.

    Pass an existing `run` to execute a run row the caller already created (which is how
    the API hands back something to poll before the work starts).

    `autocommit=False` keeps everything in the caller's transaction, which is what the
    test-suite rollback style needs.
    """
    now = datetime.now(timezone.utc)
    if run is None:
        run = SyncRun(
            tenant_id=account.tenant_id,
            social_account_id=account.id,
            trigger_source=trigger_source,
        )
        db.add(run)

    run.status = "running"
    run.started_at = run.started_at or now
    await db.flush()

    await _fail_stale_runs(db, account.id, except_run_id=run.id)

    stats = SyncStats()
    ceiling = max_conversations or settings.sync_max_conversations
    message_ceiling = max_messages or settings.sync_max_messages_per_conversation

    if not account.access_token_encrypted:
        run.status = "failed"
        run.error = "account has no stored access token"
        run.finished_at = datetime.now(timezone.utc)
        if autocommit:
            await db.commit()
        return run

    # Instagram messaging is driven by the Page token, so the Page id is the right path
    # root; fall back to the account id for rows created before Page login existed.
    page_id = account.external_page_id or account.external_account_id
    token = decrypt_token(account.access_token_encrypted)

    failure: Optional[str] = None
    try:
        async for payload in instagram.iter_conversations(
            page_id, token, max_conversations=ceiling
        ):
            stats.conversations_seen += 1
            if stats.conversations_seen >= ceiling:
                stats.truncated = True
            try:
                # One savepoint per thread: a malformed payload costs one conversation,
                # not the entire import.
                async with db.begin_nested():
                    created = await _import_conversation(
                        db,
                        account,
                        payload,
                        token,
                        stats,
                        max_messages=message_ceiling,
                        fetch_profiles=fetch_profiles,
                    )
                if created:
                    stats.conversations_created += 1
            except Exception as exc:  # noqa: BLE001 — isolate per-conversation failure
                stats.conversations_failed += 1
                log.exception(
                    "conversation import failed (account=%s conversation=%s)",
                    account.id,
                    payload.get("id"),
                )
                failure = failure or f"{type(exc).__name__}: {exc}"

            if autocommit:
                # Commit each thread so a long import makes durable progress and a crash
                # or rate-limit never discards work that already succeeded.
                _apply_counters(run, stats)
                await db.commit()

        if (
            stats.conversations_failed
            and stats.conversations_created == 0
            and stats.conversations_seen
        ):
            run.status = "failed"
        elif stats.conversations_failed or stats.truncated:
            run.status = "partial"
        else:
            run.status = "succeeded"

    except graph.GraphError as exc:
        run.error = str(exc)
        run.status = "partial" if stats.conversations_seen else "failed"
        log.warning("sync aborted for account %s: %s", account.id, exc)
    except Exception as exc:  # noqa: BLE001 — surface anything else on the run row
        run.error = f"{type(exc).__name__}: {exc}"
        run.status = "partial" if stats.conversations_seen else "failed"
        log.exception("sync crashed for account %s", account.id)

    if failure and not run.error:
        run.error = failure

    if run.status in ("succeeded", "partial"):
        account.last_synced_at = datetime.now(timezone.utc)

    _apply_counters(run, stats)
    run.finished_at = datetime.now(timezone.utc)
    if autocommit:
        await db.commit()
    return run


def _apply_counters(run: SyncRun, stats: SyncStats) -> None:
    for key, value in stats.as_columns().items():
        setattr(run, key, value)
    run.meta = {**(run.meta or {}), "truncated": stats.truncated}


async def sync_account_by_id(
    account_id: uuid.UUID,
    *,
    trigger_source: str = "manual",
    run_id: Optional[uuid.UUID] = None,
    **kwargs: Any,
) -> Optional[SyncRun]:
    """Run a sync on its own DB session — for background tasks and the CLI.

    Background work outlives the request that scheduled it, so it must open a session
    rather than borrow the handler's (already closed) one.
    """
    async with async_session_factory() as db:
        account = await db.get(SocialAccount, account_id)
        if account is None:
            log.warning("sync requested for account %s which no longer exists", account_id)
            return None
        run = await db.get(SyncRun, run_id) if run_id else None
        return await sync_account(
            db, account, trigger_source=trigger_source, run=run, **kwargs
        )
