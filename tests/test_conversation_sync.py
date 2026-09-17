"""Historical import ("backfill") and webhook ingest behaviour.

Real Postgres, every test inside a transaction that is always rolled back. Meta is never
called: `instagram.iter_conversations` / `iter_messages` are replaced with fixtures shaped
like the real payload in `next-app/conversation_example.json`.

    python -m pytest tests/test_conversation_sync.py

The properties under test are the ones that make a resync safe to run repeatedly:
idempotency across two independent message-id spellings, per-thread failure isolation,
preserved timestamps, and direction inference without the webhook-only `is_echo` field.
"""
import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, func, select

from app.api.v1 import webhooks as webhooks_module
from app.api.v1.webhooks import ingest_event
from app.core.config import settings
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.sync_run import SyncRun
from app.models.tenant import Tenant
from app.services import conversation_sync
from app.services.meta import instagram

# The business's own Instagram account, exactly as it appears in the real payload.
BUSINESS_IG_ID = "17841431601921729"
BUSINESS_USERNAME = "utsvuphr"
PAGE_ID = "620492727822627"
CUSTOMER_IG_ID = "2112398152694610"

# The webhook signature check is a real HMAC against the configured app secret.
TEST_APP_SECRET = "test-app-secret"


@pytest.fixture(autouse=True)
def _fixed_app_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the app secret so signature assertions do not depend on a developer's .env."""
    monkeypatch.setattr(settings, "meta_app_secret", TEST_APP_SECRET)


def _conversation(
    conv_id: str,
    updated_time: str,
    customer_id: str,
    customer_username: str,
    messages: list[dict],
    *,
    messages_paging: dict | None = None,
) -> dict:
    """One Conversations-API thread, participants mirroring Meta's real shape."""
    block: dict = {"data": messages}
    if messages_paging:
        block["paging"] = messages_paging
    return {
        "id": conv_id,
        "updated_time": updated_time,
        "participants": {
            "data": [
                {"username": BUSINESS_USERNAME, "id": BUSINESS_IG_ID},
                {"username": customer_username, "id": customer_id},
            ]
        },
        "messages": block,
    }


def _message(
    mid: str, text: str, created_time: str, *, from_id: str = CUSTOMER_IG_ID
) -> dict:
    """A Graph Message node — note there is no `is_echo` field in this shape."""
    return {
        "id": mid,
        "message": text,
        "created_time": created_time,
        "from": {"id": from_id, "username": ""},
    }


def _threads() -> list[dict]:
    """Newest-first, as Meta returns them, including two threads for one customer."""
    return [
        _conversation(
            "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6MQ",
            "2026-09-17T13:01:46+0000",
            "2112398152694610",
            "ajahi_amit",
            [
                _message("m_mid.001", "is this available?", "2026-09-17T12:00:00+0000"),
                _message(
                    "m_mid.002",
                    "yes it is",
                    "2026-09-17T12:05:00+0000",
                    from_id=BUSINESS_IG_ID,
                ),
                _message("m_mid.003", "great, ordering now", "2026-09-17T13:01:46+0000"),
            ],
        ),
        _conversation(
            "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6Mg",
            "2026-09-04T10:42:39+0000",
            "4433730993509535",
            "acharyabinash7",
            [_message("m_mid.010", "hello?", "2026-09-04T10:42:39+0000")],
        ),
        _conversation(
            "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6Mw",
            "2026-09-04T06:14:39+0000",
            "1041380435442681",
            "utsavupahar",
            [_message("m_mid.020", "price please", "2026-09-04T06:14:39+0000")],
        ),
        # Older thread, same customer as thread 1 -> must import as `closed`.
        _conversation(
            "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6NA",
            "2026-08-01T09:00:00+0000",
            "2112398152694610",
            "ajahi_amit",
            [_message("m_mid.030", "old enquiry", "2026-08-01T09:00:00+0000")],
        ),
    ]


async def _seed_account(db, *, token: str = "page-token") -> SocialAccount:
    tenant = Tenant(name="Backfill Test")
    db.add(tenant)
    await db.flush()
    account = SocialAccount(
        tenant_id=tenant.id,
        platform="instagram",
        external_account_id=BUSINESS_IG_ID,
        external_page_id=PAGE_ID,
        account_name=BUSINESS_USERNAME,
        access_token_encrypted=encrypt_token(token),
    )
    db.add(account)
    await db.flush()
    return account


def _patch_conversations(payloads: list[dict]) -> None:
    async def _iter(page_id, token, **kwargs):
        for payload in payloads:
            yield payload

    instagram.iter_conversations = _iter


# --------------------------------------------------------------------- baseline import


async def _run_import() -> None:
    async with async_session_factory() as db:
        account = await _seed_account(db)
        _patch_conversations(_threads())

        run = await conversation_sync.sync_account(db, account, autocommit=False)

        assert run.status == "succeeded", run.error
        assert run.conversations_seen == 4
        assert run.conversations_created == 4
        assert run.conversations_failed == 0
        # 3 + 1 + 1 + 1 messages, all new.
        assert run.messages_created == 6
        assert run.messages_skipped == 0

        # The business participant must never be stored as a customer.
        usernames = (
            await db.execute(select(Customer.external_username).order_by(Customer.external_username))
        ).scalars().all()
        assert usernames == ["acharyabinash7", "ajahi_amit", "utsavupahar"]
        assert BUSINESS_USERNAME not in usernames

        await db.rollback()


def test_import_creates_threads_customers_and_messages() -> None:
    asyncio.run(_run_import())


# ------------------------------------------------------------------------- idempotency


async def _run_twice() -> None:
    async with async_session_factory() as db:
        account = await _seed_account(db)
        _patch_conversations(_threads())

        first = await conversation_sync.sync_account(db, account, autocommit=False)
        assert first.messages_created == 6

        second = await conversation_sync.sync_account(db, account, autocommit=False)

        assert second.status == "succeeded", second.error
        # No new rows, and every message accounted for as a skip.
        assert second.conversations_created == 0
        assert second.messages_created == 0
        assert second.messages_skipped == 6

        total = (await db.execute(select(func.count()).select_from(Message))).scalar_one()
        assert total == 6
        convos = (await db.execute(select(func.count()).select_from(Conversation))).scalar_one()
        assert convos == 4

        await db.rollback()


def test_reimport_is_idempotent() -> None:
    asyncio.run(_run_twice())


# ------------------------------------------- graph vs webhook message-id spellings


async def _run_id_normalisation() -> None:
    """A webhook `mid.001` and a Graph `m_mid.001` are the same message, not two."""
    async with async_session_factory() as db:
        account = await _seed_account(db)

        # The webhook arrives first, storing the un-prefixed id.
        result = await ingest_event(
            db,
            BUSINESS_IG_ID,
            {
                "sender": {"id": "2112398152694610"},
                "recipient": {"id": BUSINESS_IG_ID},
                "timestamp": 1787000000000,
                "message": {"mid": "mid.001", "text": "is this available?"},
            },
        )
        assert result.status == "stored"
        await db.flush()

        # The backfill then reports the same message with the Graph `m_` prefix.
        _patch_conversations([_threads()[0]])
        run = await conversation_sync.sync_account(db, account, autocommit=False)

        assert run.messages_created == 2, "only the two unseen messages are new"
        assert run.messages_skipped == 1, "the echoed message is recognised as a duplicate"

        stored = (
            await db.execute(select(Message.external_message_id).order_by(Message.external_message_id))
        ).scalars().all()
        assert "mid.001" in stored
        assert "m_mid.001" not in stored, "the Graph prefix must be normalised away"

        await db.rollback()


def test_graph_and_webhook_ids_collapse_to_one_message() -> None:
    asyncio.run(_run_id_normalisation())


# ------------------------------------------------------------------ failure isolation


async def _run_isolation() -> None:
    """One malformed thread must cost one thread, not the whole import."""
    async with async_session_factory() as db:
        account = await _seed_account(db)
        threads = _threads()

        # Sabotage the second thread with a participant list that cannot be attributed.
        threads[1]["participants"] = {"data": [{"id": BUSINESS_IG_ID, "username": BUSINESS_USERNAME}]}

        _patch_conversations(threads)
        run = await conversation_sync.sync_account(db, account, autocommit=False)

        # The unusable thread is skipped rather than aborting the run.
        assert run.conversations_seen == 4
        assert run.conversations_created == 3
        assert run.status == "succeeded", run.error
        assert run.messages_created == 5

        await db.rollback()


def test_unusable_thread_does_not_abort_the_import() -> None:
    asyncio.run(_run_isolation())


async def _run_hard_failure_isolation() -> None:
    """An exception mid-thread is contained by that thread's savepoint."""
    async with async_session_factory() as db:
        account = await _seed_account(db)
        threads = _threads()
        original = conversation_sync._import_conversation
        calls = {"n": 0}

        async def flaky(db_, account_, payload, token, stats, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return await original(db_, account_, payload, token, stats, **kwargs)

        conversation_sync._import_conversation = flaky
        try:
            _patch_conversations(threads)
            run = await conversation_sync.sync_account(db, account, autocommit=False)
        finally:
            conversation_sync._import_conversation = original

        assert run.conversations_failed == 1
        assert run.conversations_created == 3
        assert run.status == "partial", run.status
        assert "boom" in (run.error or "")
        # The surviving threads really were written.
        assert (await db.execute(select(func.count()).select_from(Conversation))).scalar_one() == 3

        await db.rollback()


def test_thread_exception_is_contained_by_its_savepoint() -> None:
    asyncio.run(_run_hard_failure_isolation())


# --------------------------------------------------------- ordering, timestamps, direction


async def _run_semantics() -> None:
    async with async_session_factory() as db:
        account = await _seed_account(db)
        _patch_conversations(_threads())
        await conversation_sync.sync_account(db, account, autocommit=False)

        # Two threads belong to ajahi_amit: newest open, older closed.
        statuses = dict(
            (
                await db.execute(
                    select(Conversation.external_conversation_id, Conversation.status).where(
                        Conversation.external_conversation_id.in_(
                            ["aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6MQ", "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6NA"]
                        )
                    )
                )
            ).all()
        )
        assert statuses["aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6MQ"] == "open"
        assert statuses["aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6NA"] == "closed"

        # Circular import guard: imported lazily to keep module import order simple.
        messages = (
            await db.execute(select(Message).order_by(Message.created_at))
        ).scalars().all()
        by_content = {m.content: m for m in messages}

        # Timestamps come from Meta, not from import time.
        assert by_content["is this available?"].created_at == datetime(
            2026, 9, 17, 12, 0, tzinfo=timezone.utc
        )
        # Direction is inferred from the sender id because Graph has no `is_echo`.
        assert by_content["yes it is"].sender_type == "agent"
        assert by_content["yes it is"].sender_customer_id is None
        assert by_content["is this available?"].sender_type == "customer"
        assert by_content["is this available?"].sender_customer_id is not None
        # The stored id is normalised even though Graph sent the m_ form.
        assert by_content["yes it is"].external_message_id == "mid.002"

        await db.rollback()


def test_timestamps_direction_and_thread_status() -> None:
    asyncio.run(_run_semantics())


# ----------------------------------------------------------------- truncated messages


async def _run_truncated_messages() -> None:
    """When Meta reports more messages than it inlined, walk the thread's own edge."""
    async with async_session_factory() as db:
        account = await _seed_account(db)
        thread = _conversation(
            "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6OQ",
            "2026-09-17T13:01:46+0000",
            "2112398152694610",
            "ajahi_amit",
            [_message("m_mid.100", "first", "2026-09-17T10:00:00+0000")],
            messages_paging={"next": "https://graph.facebook.com/next-page"},
        )
        _patch_conversations([thread])

        async def _extra_messages(conversation_id, token, **kwargs):
            yield _message("m_mid.101", "second", "2026-09-17T11:00:00+0000")

        instagram.iter_messages = _extra_messages
        run = await conversation_sync.sync_account(db, account, autocommit=False)

        assert run.messages_created == 2, "the inlined page plus the thread's own edge"
        contents = (await db.execute(select(Message.content).order_by(Message.created_at))).scalars().all()
        assert contents == ["first", "second"]

        await db.rollback()


def test_truncated_inline_messages_are_followed_up() -> None:
    asyncio.run(_run_truncated_messages())


# ------------------------------------------------------------------- webhook ingest


async def _run_webhook_ingest() -> None:
    async with async_session_factory() as db:
        await _seed_account(db)

        customer_event = {
            "sender": {"id": "cust-1"},
            "recipient": {"id": BUSINESS_IG_ID},
            "timestamp": 1700000000000,
            "message": {"mid": "m1", "text": "hi"},
        }
        echo_event = {
            "sender": {"id": BUSINESS_IG_ID},
            "recipient": {"id": "cust-1"},
            "timestamp": 1700000001000,
            "message": {"mid": "m2", "text": "yo", "is_echo": True},
        }
        read_event = {"sender": {"id": "cust-1"}, "recipient": {"id": BUSINESS_IG_ID}, "read": {"mid": "m2"}}

        # Second customer_event simulates a Meta redelivery of the same mid.
        for event in (customer_event, customer_event, echo_event, read_event):
            await ingest_event(db, BUSINESS_IG_ID, event)

        convos = (await db.execute(select(Conversation))).scalars().all()
        assert len(convos) == 1
        messages = (
            await db.execute(select(Message).order_by(Message.external_message_id))
        ).scalars().all()
        assert [(m.external_message_id, m.sender_type) for m in messages] == [
            ("m1", "customer"),
            ("m2", "agent"),
        ]

        # A redelivered event is dropped, not duplicated.
        assert (
            await db.execute(select(func.count()).select_from(Message))
        ).scalar_one() == 2

        # An unlinked account is reported, not silently swallowed.
        unknown = await ingest_event(db, "not-our-page", customer_event)
        assert unknown.status == "unknown_account"

        await db.rollback()


def test_webhook_ingest_is_idempotent_and_direction_aware() -> None:
    asyncio.run(_run_webhook_ingest())


class _FakeRequest:
    """The minimum of Starlette's Request surface that the webhook route touches."""

    def __init__(self, payload: dict, signature: str | None = None) -> None:
        self._body = json.dumps(payload).encode()
        self.headers = {
            "x-hub-signature-256": _sign(self._body) if signature is None else signature
        }

    async def body(self) -> bytes:
        return self._body

    async def json(self) -> dict:
        return json.loads(self._body)


def _sign(body: bytes) -> str:
    """A genuine X-Hub-Signature-256 for `body` under the app secret."""
    return "sha256=" + hmac.new(TEST_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _purge_tenant(db, tenant_id) -> None:
    """Delete a tenant and everything under it, in FK-safe order.

    The webhook route commits for real (deliberately — a delivery must be durable), so
    unlike every other test here it cannot rely on a rollback to clean up after itself.
    """
    conversation_ids = select(Conversation.id).where(Conversation.tenant_id == tenant_id)
    await db.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
    await db.execute(delete(Conversation).where(Conversation.tenant_id == tenant_id))
    await db.execute(delete(Customer).where(Customer.tenant_id == tenant_id))
    await db.execute(delete(SyncRun).where(SyncRun.tenant_id == tenant_id))
    await db.execute(delete(SocialAccount).where(SocialAccount.tenant_id == tenant_id))
    await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
    await db.commit()


async def _run_route_isolation() -> None:
    """A poisoned event must not take the rest of the delivery with it."""
    from fastapi import BackgroundTasks

    async with async_session_factory() as db:
        account = await _seed_account(db)
        tenant_id = account.tenant_id

        payload = {
            "object": "instagram",
            "entry": [
                {
                    "id": BUSINESS_IG_ID,
                    "messaging": [
                        {"sender": {"id": "c-good"}, "recipient": {"id": BUSINESS_IG_ID},
                         "timestamp": 1700000000000,
                         "message": {"mid": "ok-1", "text": "hello"}},
                        # No mid -> ignored, not an error.
                        {"sender": {"id": "c-2"}, "recipient": {"id": BUSINESS_IG_ID},
                         "message": {"text": "no id here"}},
                        {"sender": {"id": "c-3"}, "recipient": {"id": BUSINESS_IG_ID},
                         "timestamp": 1700000002000,
                         "message": {"mid": "ok-2", "text": "second"}},
                    ],
                }
            ],
            # A `standby` array must be ingested too: those are messages handled while
            # another app owns the thread, and they belong in the same inbox.
            "entry_standby": None,
        }
        del payload["entry_standby"]

        try:
            result = await webhooks_module.receive(
                request=_FakeRequest(payload, _sign(json.dumps(payload).encode())),
                background=BackgroundTasks(),
                db=db,
            )

            assert result["stored"] == 2
            assert result["ignored"] == 1
            assert result["failed"] == 0
            assert (
                await db.execute(select(func.count()).select_from(Message))
            ).scalar_one() == 2
        finally:
            await _purge_tenant(db, tenant_id)


def test_route_stores_events_and_skips_non_messages() -> None:
    asyncio.run(_run_route_isolation())


async def _run_route_standby() -> None:
    """`standby` events are ingested alongside `messaging`."""
    from fastapi import BackgroundTasks

    async with async_session_factory() as db:
        account = await _seed_account(db)
        tenant_id = account.tenant_id
        payload = {
            "object": "instagram",
            "entry": [
                {
                    "id": BUSINESS_IG_ID,
                    "standby": [
                        {"sender": {"id": "c-standby"}, "recipient": {"id": BUSINESS_IG_ID},
                         "timestamp": 1700000000000,
                         "message": {"mid": "sb-1", "text": "handled elsewhere"}},
                    ],
                }
            ],
        }
        try:
            result = await webhooks_module.receive(
                request=_FakeRequest(payload, _sign(json.dumps(payload).encode())),
                background=BackgroundTasks(),
                db=db,
            )
            assert result["stored"] == 1
            assert (
                await db.execute(select(func.count()).select_from(Message))
            ).scalar_one() == 1
        finally:
            await _purge_tenant(db, tenant_id)


def test_route_ingests_standby_events() -> None:
    asyncio.run(_run_route_standby())


async def _run_route_rejects_bad_signature() -> None:
    from fastapi import BackgroundTasks, HTTPException

    async with async_session_factory() as db:
        account = await _seed_account(db)
        tenant_id = account.tenant_id
        payload = {"object": "instagram", "entry": []}
        try:
            try:
                await webhooks_module.receive(
                    request=_FakeRequest(payload, "sha256=deadbeef"),
                    background=BackgroundTasks(),
                    db=db,
                )
            except HTTPException as exc:
                assert exc.status_code == 403
            else:
                raise AssertionError("a bad signature must be rejected")
        finally:
            await _purge_tenant(db, tenant_id)


def test_route_rejects_forged_signature() -> None:
    asyncio.run(_run_route_rejects_bad_signature())


# ------------------------------------------------------------------------- misc units


async def _run_stale_runs() -> None:
    """A run left `running` by a crash is closed out by the next import."""
    async with async_session_factory() as db:
        account = await _seed_account(db)
        zombie = SyncRun(
            tenant_id=account.tenant_id,
            social_account_id=account.id,
            status="running",
            trigger_source="manual",
        )
        db.add(zombie)
        await db.flush()

        _patch_conversations(_threads())
        run = await conversation_sync.sync_account(db, account, autocommit=False)

        await db.flush()
        await db.refresh(zombie)
        assert zombie.status == "failed"
        assert "superseded" in (zombie.error or "")
        assert run.status == "succeeded"
        assert account.last_synced_at is not None

        await db.rollback()


def test_abandoned_runs_are_closed_out() -> None:
    asyncio.run(_run_stale_runs())
