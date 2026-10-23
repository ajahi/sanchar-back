"""Which queued reply jobs are dropped (Postgres, rolled back; LLM and channel faked), and the no-broker fallback.

    python -m pytest tests/test_reply_queue.py
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import reply_pipeline, reply_queue
from app.services.meta import whatsapp

NOW = datetime.now(timezone.utc)


async def _run(monkeypatch) -> None:
    sent: list = []

    async def fake_send(token, account_id, to, text):
        sent.append(text)
        return {"message_id": f"wamid.q{len(sent)}"}

    async def fake_generate(system, history, text, hint=""):
        return "ok"

    monkeypatch.setattr(whatsapp, "send_text", fake_send)
    monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)

    async with async_session_factory() as db:
        tenant = Tenant(name="Shop", ai_auto_reply=True)
        db.add(tenant)
        await db.flush()
        db.add(KnowledgeDocument(tenant_id=tenant.id, title="Shop", content="Chura Rs 300"))
        acct = SocialAccount(tenant_id=tenant.id, platform="whatsapp", external_account_id="pn-q",
                             access_token_encrypted=encrypt_token("tok"))
        db.add(acct)
        await db.flush()

        async def chat(n: int, *msgs: tuple[str, float]) -> list:
            cust = Customer(tenant_id=tenant.id, external_user_id=f"97798000000{n}")
            db.add(cust)
            await db.flush()
            convo = Conversation(tenant_id=tenant.id, customer_id=cust.id, social_account_id=acct.id, channel="whatsapp")
            db.add(convo)
            await db.flush()
            rows = [Message(conversation_id=convo.id, sender_type=s, content="x", created_at=NOW - timedelta(minutes=m))
                    for s, m in msgs]
            db.add_all(rows)
            await db.flush()
            return rows

        async def replies(msg) -> int:
            before = len(sent)
            await reply_pipeline.auto_reply(db, msg.id, queued=True)
            return len(sent) - before

        cutoff = reply_pipeline.settings.reply_max_age_minutes
        (fresh,) = await chat(1, ("customer", 1))
        assert await replies(fresh) == 1
        assert await replies(fresh) == 0  # redelivered after the reply was stored: not answered twice
        (old,) = await chat(2, ("customer", cutoff + 1))
        assert await replies(old) == 0
        asked, _ = await chat(3, ("customer", 5), ("ai", 4))
        assert await replies(asked) == 0
        asked, _ = await chat(4, ("customer", 5), ("agent", 4))
        assert await replies(asked) == 0
        first, second = await chat(5, ("customer", 5), ("customer", 4))
        assert await replies(first) == 0 and await replies(second) == 1
        assert await reply_pipeline.auto_reply(db, "00000000-0000-0000-0000-000000000000", queued=True) is None
        await db.rollback()


def test_queued_job_drops(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))


def test_stale_reason_rules() -> None:
    fresh = datetime.now(timezone.utc)
    assert reply_pipeline.stale_reason(fresh, False, False) is None
    assert "cutoff" in reply_pipeline.stale_reason(fresh - timedelta(hours=1), False, False)
    assert "answered" in reply_pipeline.stale_reason(fresh, True, False)
    assert "newer" in reply_pipeline.stale_reason(fresh, False, True)


def test_publish_without_broker_returns_false(monkeypatch) -> None:
    monkeypatch.setattr(reply_queue.settings, "rabbitmq_url", "")
    assert asyncio.run(reply_queue.publish("anything")) is False
