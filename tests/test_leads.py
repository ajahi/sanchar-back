"""Sales leads: Nepali mobile numbers are spotted in text, saved on the customer, and become one lead
per customer. The DB test runs in a transaction that is always rolled back.
    python -m pytest tests/test_leads.py
"""
import asyncio

from sqlalchemy import select

from app.api.v1.webhooks import ingest_event
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.customer import Customer
from app.models.sales_lead import SalesLead
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services.leads import extract_phone
from app.services.meta import instagram


def test_extract_phone() -> None:
    ok = {
        "9812345678": "+9779812345678",
        "my number is 98-1234 5678 thanks": "+9779812345678",
        "+977 9841234567": "+9779841234567",
        "call 9779741234567 pls": "+9779741234567",
        "009779801234567": "+9779801234567",
        "९८१२३४५६७८": "+9779812345678",  # Devanagari digits
    }
    for text, want in ok.items():
        assert extract_phone(text) == want, text
    for text in ("", None, "price is Rs 9800", "order 123498123456789", "98123456", "6812345678", "hello"):
        assert extract_phone(text) is None, text  # prices, ids, short or non-mobile numbers


async def _run(monkeypatch) -> None:
    async def no_profile(*_a, **_k):
        return {}

    monkeypatch.setattr(instagram, "fetch_customer_profile", no_profile)
    async with async_session_factory() as db:
        t = Tenant(name="T")
        db.add(t)
        await db.flush()
        db.add(SocialAccount(tenant_id=t.id, platform="instagram", external_account_id="biz-9",
                             access_token_encrypted=encrypt_token("tok")))
        await db.flush()

        def ev(mid, text, sender="cust-9"):
            return {"sender": {"id": sender}, "recipient": {"id": "biz-9"}, "timestamp": 1700000000000,
                    "message": {"mid": mid, "text": text}}

        await ingest_event(db, "biz-9", ev("m1", "price?"))
        assert (await db.execute(select(SalesLead).where(SalesLead.tenant_id == t.id))).first() is None

        await ingest_event(db, "biz-9", ev("m2", "ok call me 9812345678"))
        await ingest_event(db, "biz-9", ev("m2", "ok call me 9812345678"))  # redelivery: still one lead
        await ingest_event(db, "biz-9", ev("m3", "sorry use 9841111111 instead"))  # newer number replaces
        echo = {"sender": {"id": "biz-9"}, "recipient": {"id": "cust-9"},
                "message": {"mid": "m4", "text": "we will call 9800000000", "is_echo": True}}
        await ingest_event(db, "biz-9", echo)  # our own side's number is not the customer's

        leads = (await db.execute(select(SalesLead).where(SalesLead.tenant_id == t.id))).scalars().all()
        assert [(l.phone, l.channel, l.status) for l in leads] == [("+9779841111111", "instagram", "new")]
        customer = (await db.execute(select(Customer).where(Customer.tenant_id == t.id))).scalar_one()
        assert customer.phone == "+9779841111111" and leads[0].customer_id == customer.id
        await db.rollback()


def test_capture_lead_via_webhook(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))


def test_bot_order_saved_on_lead_and_listed() -> None:
    """The order the bot collected lands on the customer's lead (made if missing), pings staff once, repeats are
    no-ops, a changed order goes back to new; the Orders list shows it and staff confirm it. Rolled back."""
    import uuid

    from app.api.v1.leads import list_leads, set_status
    from app.models.conversation import Conversation
    from app.models.notification import Notification
    from app.schemas.lead import LeadPatch
    from app.services.leads import save_order

    run = uuid.uuid4().hex[:8]

    async def go() -> None:
        async with async_session_factory() as db:
            db.commit = db.flush  # stay inside the rolled-back transaction
            t, other = Tenant(name="Order shop"), Tenant(name="Other")
            db.add_all([t, other])
            await db.flush()
            acct = SocialAccount(tenant_id=t.id, platform="facebook", external_account_id=f"pg-{run}")
            cust = Customer(tenant_id=t.id, external_user_id=f"psid-{run}", name="ram.fb")
            db.add_all([acct, cust])
            await db.flush()
            convo = Conversation(tenant_id=t.id, customer_id=cust.id, social_account_id=acct.id, channel="facebook")
            db.add(convo)
            await db.flush()
            order = {"name": "Ram", "phone": "98-1234-5678", "address": "Baneshwor", "items": "2 chura box"}

            async def save(o):
                return await save_order(db, account=acct, convo=convo, customer=cust, order=o)

            async def pings():
                return (await db.execute(select(Notification).where(
                    Notification.tenant_id == t.id, Notification.event == "order_received"))).scalars().all()

            assert await save(order) is not None  # (the bot re-stating an order is dropped before this: test_funnels)
            assert len(await pings()) == 1
            [row] = await list_leads(t, db, None)
            assert (row.phone, row.status, row.customer_name, row.order) == ("+9779812345678", "new", "ram.fb", order)
            assert cust.phone == "+9779812345678"

            await set_status(row.id, LeadPatch(status="confirmed"), t, db)
            assert await list_leads(t, db, "new") == []
            assert await list_leads(other, db, None) == []  # another business never sees it

            changed = {**order, "items": "3 chura box"}
            assert await save(changed) is not None
            [row] = await list_leads(t, db, "new")  # back in the queue to confirm again
            assert row.order == changed and len(await pings()) == 2
            await db.rollback()

    asyncio.run(go())
