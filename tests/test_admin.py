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
