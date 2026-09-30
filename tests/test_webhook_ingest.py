"""Webhook ingest: one customer + one open conversation, message idempotent on mid, echo = agent.

Runs against the configured Postgres inside a transaction that is always rolled back.
    python -m pytest tests/test_webhook_ingest.py
"""
import asyncio

from sqlalchemy import select

from app.api.v1.webhooks import ingest_event
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services.meta import instagram


async def _run() -> None:
    async def _no_profile(*_a, **_k):
        return {}

    instagram.fetch_customer_profile = _no_profile  # no network in tests

    async with async_session_factory() as db:
        t = Tenant(name="T")
        db.add(t)
        await db.flush()
        db.add(
            SocialAccount(
                tenant_id=t.id,
                platform="instagram",
                external_account_id="biz-1",
                access_token_encrypted=encrypt_token("tok"),
            )
        )
        await db.flush()

        cust = {"sender": {"id": "cust-1"}, "recipient": {"id": "biz-1"}, "timestamp": 1700000000000,
                "message": {"mid": "m1", "text": "hi"}}
        echo = {"sender": {"id": "biz-1"}, "recipient": {"id": "cust-1"},
                "message": {"mid": "m2", "text": "yo", "is_echo": True}}
        read = {"sender": {"id": "cust-1"}, "recipient": {"id": "biz-1"}, "read": {"mid": "m2"}}
        # a NEW customer message returns a reply job; the redelivery must not (else Meta retries double-reply)
        first = await ingest_event(db, "biz-1", cust)
        assert first is not None and await ingest_event(db, "biz-1", cust) is None
        convo = (await db.execute(select(Conversation).where(Conversation.tenant_id == t.id))).scalar_one()
        assert first[0] == convo.id

        # our own reply echoes back under the id we stored: not stored twice, not a person
        db.add(Message(conversation_id=convo.id, external_message_id="ours", sender_type="ai", content="bot"))
        await db.flush()
        own_echo = {"sender": {"id": "biz-1"}, "recipient": {"id": "cust-1"},
                    "message": {"mid": "ours", "text": "bot", "is_echo": True}}
        assert await ingest_event(db, "biz-1", own_echo) is None

        # a person answering from the Instagram app is stored as an agent message (that pauses the bot)
        assert await ingest_event(db, "biz-1", echo) is None
        for ev in (echo, read):  # redelivered echo + read receipt change nothing
            await ingest_event(db, "biz-1", ev)

        convos = (await db.execute(select(Conversation).where(Conversation.tenant_id == t.id))).scalars().all()
        assert len(convos) == 1
        msgs = (
            await db.execute(
                select(Message).where(Message.conversation_id == convos[0].id).order_by(Message.external_message_id)
            )
        ).scalars().all()
        assert [(m.external_message_id, m.sender_type) for m in msgs] == [
            ("m1", "customer"), ("m2", "agent"), ("ours", "ai"),
        ]
        await db.rollback()


def test_ingest() -> None:
    asyncio.run(_run())
