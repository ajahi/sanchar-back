"""Seed one tenant + one owner user for local testing.

Usage:  python -m scripts.seed
Idempotent: re-running updates the owner's password rather than duplicating.
"""
import asyncio

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import async_session_factory
from app.models.role import Role
from app.models.tenant import Tenant
from app.models.user import User

OWNER_EMAIL = "owner@utsavupahar.com"
OWNER_PASSWORD = "changeme123"

SUPER_ADMIN_EMAIL = "admin@nepsocial.com"
SUPER_ADMIN_PASSWORD = "changeme123"


async def main() -> None:
    async with async_session_factory() as db:
        result = await db.execute(select(Tenant).where(Tenant.name == "Utsav Upahar"))
        tenant = result.scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                name="Utsav Upahar",
                business_type="Gift Shop",
                location="Kathmandu",
                owner_name="Amit",
            )
            db.add(tenant)
            await db.flush()

        result = await db.execute(
            select(User).where(User.tenant_id == tenant.id, User.email == OWNER_EMAIL)
        )
        owner = result.scalar_one_or_none()
        if owner is None:
            owner_role = (
                await db.execute(select(Role).where(Role.name == "owner"))
            ).scalar_one()
            owner = User(
                tenant_id=tenant.id,
                name="Amit (Owner)",
                email=OWNER_EMAIL,
                verified=True,
                roles=[owner_role],
                password_hash=hash_password(OWNER_PASSWORD),
            )
            db.add(owner)
        else:
            owner.password_hash = hash_password(OWNER_PASSWORD)

        # Platform super-admin: no tenant, holds the super_admin role.
        result = await db.execute(select(User).where(User.email == SUPER_ADMIN_EMAIL))
        super_admin = result.scalar_one_or_none()
        if super_admin is None:
            super_role = (
                await db.execute(select(Role).where(Role.name == "super_admin"))
            ).scalar_one()
            super_admin = User(
                tenant_id=None,
                name="Platform Admin",
                email=SUPER_ADMIN_EMAIL,
                verified=True,
                roles=[super_role],
                password_hash=hash_password(SUPER_ADMIN_PASSWORD),
            )
            db.add(super_admin)
        else:
            super_admin.password_hash = hash_password(SUPER_ADMIN_PASSWORD)

        await db.commit()
        print(f"Tenant:      {tenant.id}  ({tenant.name})")
        print(f"Owner:       {OWNER_EMAIL} / {OWNER_PASSWORD}")
        print(f"Super-admin: {SUPER_ADMIN_EMAIL} / {SUPER_ADMIN_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
