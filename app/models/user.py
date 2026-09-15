"""users — everyone who logs into the platform.

Holds both platform operators/admins (tenant_id NULL) and business staff associated
with a tenant (tenant_id set). Which population a row belongs to is expressed through
its roles (e.g. super_admin vs owner/admin/agent), not a separate table.
"""
import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.role import Role


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("username", name="uq_users_username"),
    )

    # NULL for platform operators/admins; set for staff belonging to a business.
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[Optional[str]] = mapped_column(String(50))
    username: Mapped[Optional[str]] = mapped_column(String(50))

    verified: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )

    password_hash: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(30), server_default="active", nullable=False)

    # RBAC: a user's roles (and, through them, permissions) — see app/models/role.py.
    roles: Mapped[list["Role"]] = relationship(
        secondary="user_roles", lazy="selectin"
    )
