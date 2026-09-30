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
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import reply_pipeline

NOW = datetime.now(timezone.utc)


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


async def _run(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []
    script: list = []  # what the fake LLM returns next (a string, or an exception to raise)
    seen: dict = {}
    hooks: list = []  # awaited inside the fake LLM call: simulates things that happen while it generates

    async def fake_send(token, account_id, to, text):
        sent.append((to, text))
        return {"message_id": f"wamid.ai{len(sent)}"}

    async def fake_generate(system, history, text):
        seen.update(system=system, history=history, text=text)
        if hooks:
            await hooks.pop()()
        nxt = script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(reply_pipeline.whatsapp, "send_text", fake_send)
    monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)

    async with async_session_factory() as db:
        tenant = Tenant(name="Shop", ai_auto_reply=True)
        empty = Tenant(name="No context", ai_auto_reply=True)
        db.add_all([tenant, empty])
        await db.flush()
        db.add(KnowledgeDocument(tenant_id=tenant.id, title="Shop", content="Chura CH-001 Rs 300"))
        acct = SocialAccount(tenant_id=tenant.id, platform="whatsapp", external_account_id="pn-1",
                             access_token_encrypted=encrypt_token("tok"))
        acct2 = SocialAccount(tenant_id=empty.id, platform="whatsapp", external_account_id="pn-2",
                              access_token_encrypted=encrypt_token("tok"))
        db.add_all([acct, acct2])
        await db.flush()

        async def new_chat(t, a, n):
            cust = Customer(tenant_id=t.id, external_user_id=f"977980000000{n}")
            db.add(cust)
            await db.flush()
            convo = Conversation(tenant_id=t.id, customer_id=cust.id, social_account_id=a.id, channel="whatsapp")
            db.add(convo)
            await db.flush()
            return convo

        def add(convo, sender, text, minutes_ago):
            m = Message(conversation_id=convo.id, sender_type=sender, content=text, created_at=ago(minutes_ago))
            db.add(m)
            return m

        # 1. plain reply: sent to the customer, stored under the channel message id as ours, context reached the model
        c1 = await new_chat(tenant, acct, 1)
        m1 = add(c1, "customer", "hello", 30)
        await db.flush()
        script.append("Hi! How can I help?")
        await reply_pipeline.auto_reply(db, m1.id)
        assert sent == [("9779800000001", "Hi! How can I help?")] and "Chura CH-001 Rs 300" in seen["system"]
        ai = (await db.execute(select(Message).where(Message.external_message_id == "wamid.ai1"))).scalar_one()
        assert ai.sender_type == "ai" and ai.ai_generated
        ai.created_at = ago(29)  # the fake timeline is in the past; real replies precede the next message

        # 2. the next message sees the earlier turns, oldest first
        m2 = add(c1, "customer", "price of CH-001?", 28)
        await db.flush()
        script.append("Rs 300.")
        await reply_pipeline.auto_reply(db, m2.id)
        assert len(sent) == 2 and seen["history"] == [("customer", "hello"), ("assistant", "Hi! How can I help?")]

        # 3. a person replied a minute ago -> the bot stays quiet; once they have been idle past the window it resumes
        c3 = await new_chat(tenant, acct, 3)
        human = add(c3, "agent", "Namaste, I will help you.", 1)
        m3 = add(c3, "customer", "delivery?", 0.5)
        await db.flush()
        await reply_pipeline.auto_reply(db, m3.id)
        assert len(sent) == 2
        human.created_at = ago(reply_pipeline.settings.human_pause_minutes + 1)
        script.append("Rs 100 inside the city.")
        await reply_pipeline.auto_reply(db, m3.id)
        assert len(sent) == 3

        # 4. a person jumps in WHILE the model is generating -> the reply is dropped
        c4 = await new_chat(tenant, acct, 4)
        m4 = add(c4, "customer", "hello?", 5)
        await db.flush()

        async def human_steps_in():
            db.add(Message(conversation_id=c4.id, sender_type="agent", content="I am here", created_at=ago(0)))
            await db.flush()

        hooks.append(human_steps_in)
        script.append("should never be sent")
        await reply_pipeline.auto_reply(db, m4.id)
        assert len(sent) == 3

        # 5. AI switched off -> silent
        c5 = await new_chat(tenant, acct, 5)
        m5 = add(c5, "customer", "hi", 5)
        await db.flush()
        tenant.ai_auto_reply = False
        await reply_pipeline.auto_reply(db, m5.id)
        assert len(sent) == 3
        tenant.ai_auto_reply = True

        # 6. model down (both tries) -> nothing sent, nothing raised
        script.extend([httpx.ConnectError("down"), httpx.ConnectError("down")])
        await reply_pipeline.auto_reply(db, m5.id)
        assert len(sent) == 3

        # 7. a tenant with no shop context is never answered by the bot
        c7 = await new_chat(empty, acct2, 7)
        m7 = add(c7, "customer", "hello", 5)
        await db.flush()
        await reply_pipeline.auto_reply(db, m7.id)
        assert len(sent) == 3
        await db.rollback()


def test_reply_pipeline(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))
