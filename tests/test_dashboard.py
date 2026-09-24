"""Dashboard counts per channel, top queries, handover — against Postgres, always rolled back.

    python -m pytest tests/test_dashboard.py
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.api.v1.dashboard import dashboard
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.tenant import Tenant


async def _run() -> None:
    async with async_session_factory() as db:
        t, other = Tenant(name="T"), Tenant(name="Other")
        db.add_all([t, other])
        await db.flush()
        cust = Customer(tenant_id=t.id, external_user_id="c1")
        cust2 = Customer(tenant_id=other.id, external_user_id="c2")
        db.add_all([cust, cust2])
        await db.flush()
        ig = Conversation(tenant_id=t.id, customer_id=cust.id, channel="instagram", mode="human")
        noise = Conversation(tenant_id=other.id, customer_id=cust2.id, channel="instagram")
        db.add_all([ig, noise])
        await db.flush()
        old = datetime.now(timezone.utc) - timedelta(days=3)
        db.add_all(
            [
                Message(conversation_id=ig.id, sender_type="customer", content="Do you deliver?"),
                Message(conversation_id=ig.id, sender_type="customer", content=" do you deliver? "),
                Message(conversation_id=ig.id, sender_type="customer", content="PP"),
                Message(conversation_id=ig.id, sender_type="customer", content="hi"),  # not a question
                Message(conversation_id=ig.id, sender_type="customer", content="Hi "),
                Message(conversation_id=ig.id, sender_type="customer", content="hi"),
                Message(conversation_id=ig.id, sender_type="customer", content="yo kati ho"),
                Message(conversation_id=ig.id, sender_type="customer", content="yo कति हो"),
                Message(conversation_id=ig.id, sender_type="customer", content="this is nice"),
                Message(conversation_id=ig.id, sender_type="customer", content="within"),  # "with" != "which"
                Message(conversation_id=ig.id, sender_type="customer", content=None),  # media only
                Message(conversation_id=ig.id, sender_type="customer", content="old", created_at=old),
                Message(conversation_id=ig.id, sender_type="agent", content="yes"),
                Message(conversation_id=ig.id, sender_type="ai", content="yes!"),
                Message(conversation_id=noise.id, sender_type="customer", content="other tenant"),
            ]
        )
        await db.flush()

        out = await dashboard(tenant=t, db=db, days=1)
        assert out.new_messages == {"whatsapp": 0, "instagram": 11, "facebook": 0}
        assert out.messages_handled == {"whatsapp": 0, "instagram": 2, "facebook": 0}
        assert out.handover == {"whatsapp": 0, "instagram": 1, "facebook": 0}
        # "hi" (x3) is the most frequent text but not a question; greetings/statements stay out.
        top = [(q.text.strip().lower(), q.count) for q in out.top_queries]
        assert top[0] == ("do you deliver?", 2)
        assert {t for t, _ in top[1:]} <= {"pp", "yo kati ho", "yo कति हो"} and len(top) == 3

        assert (await dashboard(tenant=t, db=db, days=7)).new_messages["instagram"] == 12
        await db.rollback()


def test_dashboard() -> None:
    asyncio.run(_run())
