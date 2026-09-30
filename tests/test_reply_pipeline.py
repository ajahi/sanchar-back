"""The auto-reply pipeline against Postgres (always rolled back), with the LLM and the channel faked.

    python -m pytest tests/test_reply_pipeline.py
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.handover import HandoverEvent
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.notification import Notification
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import reply_pipeline
from app.services.auto_reply import Reply

T0 = datetime.now(timezone.utc) - timedelta(minutes=10)


async def _run(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []
    script: list = []  # what the fake LLM returns next (a Reply, or an exception to raise)
    seen: dict = {}

    async def fake_send(token, account_id, to, text):
        sent.append((to, text))
        return {"message_id": f"wamid.ai{len(sent)}"}

    async def fake_generate(system, history, text):
        seen.update(system=system, history=history, text=text)
        nxt = script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(reply_pipeline.whatsapp, "send_text", fake_send)
    monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)

    async with async_session_factory() as db:
        tenant = Tenant(name="Shop", ai_auto_reply=True, owner_phone="+977")
        empty = Tenant(name="No knowledge", ai_auto_reply=True)
        db.add_all([tenant, empty])
        await db.flush()
        db.add(KnowledgeDocument(tenant_id=tenant.id, title="Products", content="Shawl Rs 4,500"))
        acct = SocialAccount(tenant_id=tenant.id, platform="whatsapp", external_account_id="pn-1",
                             access_token_encrypted=encrypt_token("tok"))
        acct2 = SocialAccount(tenant_id=empty.id, platform="whatsapp", external_account_id="pn-2",
                              access_token_encrypted=encrypt_token("tok"))
        cust = Customer(tenant_id=tenant.id, external_user_id="9779800000001")
        cust2 = Customer(tenant_id=empty.id, external_user_id="9779800000002")
        db.add_all([acct, acct2, cust, cust2])
        await db.flush()
        convo = Conversation(tenant_id=tenant.id, customer_id=cust.id, social_account_id=acct.id, channel="whatsapp")
        convo2 = Conversation(tenant_id=empty.id, customer_id=cust2.id, social_account_id=acct2.id, channel="whatsapp")
        db.add_all([convo, convo2])
        await db.flush()

        def customer_msg(c, text, minutes):
            m = Message(conversation_id=c.id, sender_type="customer", content=text,
                        created_at=T0 + timedelta(minutes=minutes))
            db.add(m)
            return m

        m1 = customer_msg(convo, "shawl price?", 0)
        m_other = customer_msg(convo2, "hello", 0)
        await db.flush()

        # 1. normal answer: sent to the customer, stored under Meta's id, chat stays with the AI
        script.append(Reply("Rs 4,500 ho.", False, "", 10))
        await reply_pipeline.auto_reply(db, m1.id)
        assert sent == [("9779800000001", "Rs 4,500 ho.")] and "Shawl Rs 4,500" in seen["system"]
        ai = (await db.execute(select(Message).where(Message.external_message_id == "wamid.ai1"))).scalar_one()
        assert ai.sender_type == "ai" and ai.ai_generated and convo.mode == "ai"
        ai.created_at = T0 + timedelta(minutes=1)  # the fake customer timeline is in the past; real replies precede the next message

        # 2. a newer customer message exists -> the older one stays silent; the newer one hands over
        m2 = customer_msg(convo, "malai refund chahiyo", 5)
        await db.flush()
        await reply_pipeline.auto_reply(db, m1.id)
        assert len(sent) == 1
        script.append(Reply("Team le reply garchha.", True, "refund request", 12))
        await reply_pipeline.auto_reply(db, m2.id)
        assert len(sent) == 2 and convo.mode == "human"
        assert [t for _, t in seen["history"]] == ["shawl price?", "Rs 4,500 ho."]  # prior turns, oldest first
        ev = (await db.execute(select(HandoverEvent).where(HandoverEvent.conversation_id == convo.id))).scalar_one()
        assert (ev.reason, ev.triggered_by, ev.to_mode) == ("refund request", "ai", "human")
        note = (await db.execute(select(Notification).where(Notification.event == "handover"))).scalar_one()
        assert note.tenant_id == tenant.id

        # 3. chat is with a person -> silent
        m3 = customer_msg(convo, "hello?", 8)
        await db.flush()
        await reply_pipeline.auto_reply(db, m3.id)
        assert len(sent) == 2

        # 4. switch off -> silent; on again + LLM down -> no message, hands over as llm_error
        convo.mode, tenant.ai_auto_reply = "ai", False
        await reply_pipeline.auto_reply(db, m3.id)
        assert len(sent) == 2
        tenant.ai_auto_reply = True
        script.extend([httpx.ConnectError("down"), httpx.ConnectError("down")])
        await reply_pipeline.auto_reply(db, m3.id)
        assert len(sent) == 2 and convo.mode == "human"
        events = (await db.execute(select(HandoverEvent.reason).where(HandoverEvent.conversation_id == convo.id))).scalars()
        assert "llm_error" in list(events)

        # 5. a tenant with no knowledge yet is never answered by the bot (and never sees another tenant's)
        await reply_pipeline.auto_reply(db, m_other.id)
        assert len(sent) == 2 and convo2.mode == "ai"
        await db.rollback()


def test_reply_pipeline(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))
