"""conversations — a customer interaction thread on a channel."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Conversation(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_tenant_status", "tenant_id", "status"),
        Index("ix_conversations_customer", "customer_id"),
        # One open thread per customer per channel account (webhook find-or-create race).
        Index(
            "uq_open_conversation",
            "customer_id",
            "social_account_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        # Meta's conversation id — the dedup key that makes historical import idempotent
        # and lets a resync find a thread a webhook created moments earlier.
        Index(
            "uq_conversation_external",
            "social_account_id",
            "external_conversation_id",
            unique=True,
            postgresql_where=text("external_conversation_id IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    social_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("social_accounts.id", ondelete="SET NULL")
    )

    external_conversation_id: Mapped[Optional[str]] = mapped_column(String(255))

    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), server_default="open", nullable=False)
    mode: Mapped[str] = mapped_column(String(30), server_default="ai", nullable=False)

    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    subject: Mapped[Optional[str]] = mapped_column(String(255))
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
