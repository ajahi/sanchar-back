"""sales_leads — a customer who gave their phone number in a chat (one row per customer per business).
metadata.order = {name, phone, address, items} once the bot has collected an order (services/leads.save_order)."""
import uuid
from typing import Optional

from sqlalchemy import ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class SalesLead(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "sales_leads"
    __table_args__ = (UniqueConstraint("tenant_id", "customer_id", name="uq_sales_lead_customer"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    # Where the number came from: the chat, the channel account, and the message that carried it.
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    social_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("social_accounts.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )

    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), nullable=False)  # normalised, e.g. +9779812345678
    # new | confirmed | cancelled. Staff confirm orders in the Orders view; a new or changed order resets it to new.
    status: Mapped[str] = mapped_column(String(30), server_default="new", nullable=False)
    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
