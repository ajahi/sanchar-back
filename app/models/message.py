"""messages — individual chat messages (never one giant chat blob)."""
import uuid
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPKMixin


class Message(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        # Idempotency (spec §22): a Meta message id may only be stored once.
        Index(
            "idx_unique_external_message",
            "external_message_id",
            unique=True,
            postgresql_where=text("external_message_id IS NOT NULL"),
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    external_message_id: Mapped[Optional[str]] = mapped_column(String(255))

    # customer | ai | agent | system
    sender_type: Mapped[str] = mapped_column(String(30), nullable=False)

    sender_customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id")
    )
    sender_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    message_type: Mapped[str] = mapped_column(String(30), server_default="text", nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text)
    media_url: Mapped[Optional[str]] = mapped_column(Text)

    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ai_generated: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
