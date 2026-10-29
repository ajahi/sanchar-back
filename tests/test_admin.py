"""Super-admin tenant list + AI switch — against Postgres, always rolled back.

    python -m pytest tests/test_admin.py
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.api.v1.admin import AiToggle, list_tenants, set_tenant_ai
from app.core.tenant import require_role
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.role import Role
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant import TenantUpdate


async def _run() -> None:
    async with async_session_factory() as db:
        t = Tenant(name="AdminT")
        db.add(t)
        await db.flush()
        cust = Customer(tenant_id=t.id, external_user_id="c1")
        db.add(cust)
        await db.flush()
        conv = Conversation(tenant_id=t.id, customer_id=cust.id, channel="instagram", mode="human")
        db.add(conv)
        await db.flush()
        db.add_all(
            [
                Message(conversation_id=conv.id, sender_type="customer", content="hi"),
                Message(conversation_id=conv.id, sender_type="ai", content="hello"),
            ]
        )
        admin = User(name="A", email="a@admin.test", roles=[Role(name="super_admin_test")])
        db.add(admin)
        await db.flush()

        row = next(r for r in await list_tenants(db=db) if r["id"] == t.id)
        assert (row["messages"], row["ai_replies"], row["open_handovers"], row["customers"]) == (2, 1, 1, 1)
        assert row["ai_auto_reply"] is False

        out = await set_tenant_ai(t.id, AiToggle(enabled=True), admin, db)
        assert out["ai_auto_reply"] is True and t.ai_auto_reply is True
        with pytest.raises(HTTPException) as e:
            await set_tenant_ai(admin.id, AiToggle(enabled=True), admin, db)
        assert e.value.status_code == 404

        with pytest.raises(HTTPException) as e:  # owners/agents are not super admins
            await require_role("super_admin")(admin)
        assert e.value.status_code == 403

        assert "ai_auto_reply" not in TenantUpdate.model_fields  # owners can no longer flip it
        await db.rollback()


def test_admin() -> None:
    asyncio.run(_run())


def test_activity_counts_today_and_running_totals():
    """The growth chart's data: today's new rows land in the last day, totals run up to the day, gaps are 0."""
    from app.api.v1.admin import activity

    async def go() -> None:
        async with async_session_factory() as db:
            base = (await activity(db, days=7))[-1]
            t = Tenant(name="GrowthT")
            db.add(t)
            await db.flush()
            cust = Customer(tenant_id=t.id, external_user_id=f"g-{t.id}")
            db.add(cust)
            await db.flush()
            conv = Conversation(tenant_id=t.id, customer_id=cust.id, channel="instagram")
            db.add(conv)
            await db.flush()
            db.add_all([Message(conversation_id=conv.id, sender_type="customer", content="hi"),
                        Message(conversation_id=conv.id, sender_type="ai", content="hello")])
            await db.flush()
            rows = await activity(db, days=7)
            assert len(rows) == 7 and rows[0]["day"] < rows[-1]["day"]
            today = rows[-1]
            assert (today["conversations"], today["messages"], today["ai_replies"]) == (
                base["conversations"] + 1, base["messages"] + 2, base["ai_replies"] + 1)
            assert today["total_messages"] == base["total_messages"] + 2
            assert all(a["total_messages"] <= b["total_messages"] for a, b in zip(rows, rows[1:]))
            await db.rollback()

    asyncio.run(go())


def test_funnel_edit_is_a_new_version_and_tenants_pick_one():
    """Saving a funnel adds a live version (the old one kept for rollback); a broken funnel is refused."""
    import uuid

    from app.api.v1.admin import FunnelIn, TenantFunnel, activate_funnel, save_funnel, set_tenant_funnel

    async def go() -> None:
        async with async_session_factory() as db:
            db.commit = db.flush  # stay inside the rolled-back transaction
            admin = User(name="A", email=f"funnel-{uuid.uuid4().hex[:8]}@example.com")
            t = Tenant(name="FunnelT")
            db.add_all([admin, t])
            await db.flush()
            key = f"agency_{uuid.uuid4().hex[:6]}"
            fields = [{"key": "name", "label": "name"}, {"key": "phone", "label": "phone"}, {"key": "need", "label": "need"}]
            body = dict(key=key, name="Agency", label="enquiry", steps="Ask what they need, then write {label}.",
                        fields=fields, triggers=["notify"])
            v1 = await save_funnel(FunnelIn(**body), admin, db)
            v2 = await save_funnel(FunnelIn(**{**body, "steps": "Ask their budget too, then write {label}."}), admin, db)
            assert (v1["version"], v2["version"], v2["active"]) == (1, 2, True)
            assert (await activate_funnel(key, 1, db))["active"]  # rollback
            from app.models.funnel import Funnel
            from sqlalchemy import select
            live = (await db.execute(select(Funnel.version).where(Funnel.key == key, Funnel.active.is_(True)))).scalars().all()
            assert live == [1]

            for bad, why in [({"steps": "no label marker here at all"}, "{label}"),
                             ({"triggers": ["website_post"]}, "need fields"),
                             ({"triggers": ["fax"]}, "Unknown trigger"),
                             ({"label": "summary"}, "summary")]:
                with pytest.raises(HTTPException) as e:
                    await save_funnel(FunnelIn(**{**body, **bad}), admin, db)
                assert why in e.value.detail

            assert (await set_tenant_funnel(t.id, TenantFunnel(funnel_key=key), admin, db))["funnel_key"] == key
            with pytest.raises(HTTPException):
                await set_tenant_funnel(t.id, TenantFunnel(funnel_key="no_such_funnel"), admin, db)
            await db.rollback()

    asyncio.run(go())
