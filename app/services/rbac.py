"""Helpers for resolving Role rows by name (the RBAC role catalog)."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.role import Role


async def get_roles_by_names(db: AsyncSession, names: list[str]) -> list[Role]:
    """Load Role rows matching the given names (order not guaranteed)."""
    if not names:
        return []
    result = await db.execute(select(Role).where(Role.name.in_(names)))
    return list(result.scalars().all())
