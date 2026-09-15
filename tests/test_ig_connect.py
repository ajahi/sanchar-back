"""Connect-mode guard: an IG account already linked to tenant A cannot be attached to tenant B.

Runs against the configured Postgres inside a transaction that is always rolled back.
    python -m pytest tests/test_ig_connect.py   (or: python -m tests.test_ig_connect)
"""
import asyncio

import pytest

from app.api.v1.social_accounts import _get_or_create_account
from app.db.session import async_session_factory
from app.models.tenant import Tenant
from app.models.user import User


async def _run() -> None:
    async with async_session_factory() as db:
        a, b = Tenant(name="A"), Tenant(name="B")
        db.add_all([a, b])
        await db.flush()
        ub = User(tenant_id=b.id, name="ub", email="ub@test.local")
        db.add(ub)
        await db.flush()

        kw = dict(ig_user_id="ig-1", username="x", long_lived_token="t", expires_in=60, source_uri="")
        tenant, owner, acct = await _get_or_create_account(db, **kw)  # anonymous -> new tenant
        assert acct.tenant_id == tenant.id and owner.tenant_id == tenant.id

        with pytest.raises(PermissionError):
            await _get_or_create_account(db, user=ub, **kw)  # linked elsewhere -> refused

        kw["ig_user_id"] = "ig-2"
        t2, u2, acct2 = await _get_or_create_account(db, user=ub, **kw)  # connect -> tenant B
        assert (t2.id, u2.id, acct2.tenant_id) == (b.id, ub.id, b.id)
        await db.rollback()


def test_connect_guard() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    test_connect_guard()
    print("ok")
