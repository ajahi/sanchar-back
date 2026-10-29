"""funnels — how the bot leads a chat to a result for one kind of business (restaurant order, consultancy enquiry,
...): its steps (prompt text), the fields it collects and what runs once the customer confirms (services/funnels.py).
An edit is a new version; one version per key is active. A tenant picks a key (tenants.funnel_key).

funnel_submissions — what a funnel collected: one row per confirmation, so a repeat order is a new row."""
import uuid
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Funnel(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "funnels"
    __table_args__ = (UniqueConstraint("key", "version", name="uq_funnels_key_version"),)

    key: Mapped[str] = mapped_column(String(50), nullable=False)  # "restaurant"
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)  # "Restaurant / Food & Beverage"
    label: Mapped[str] = mapped_column(String(30), nullable=False)  # the hidden label's name: [order: ...]
    steps: Mapped[str] = mapped_column(Text, nullable=False)  # prompt text with {label} (and {summary_label})
    # [{key, label, required, max}] in label order (auto_reply.split_label)
    fields: Mapped[list] = mapped_column(JSONB, nullable=False)
    # run in order after a confirmation: "save_order", "notify", "website_post" (services/funnels.TRIGGERS)
    triggers: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    # run before each reply: [{"type": "opening_hours", "message": "... {opens} ..."}] (services/funnels.CHECKS)
    checks: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)


class FunnelSubmission(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "funnel_submissions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL")
    )
    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sales_leads.id", ondelete="SET NULL")
    )
    funnel_key: Mapped[str] = mapped_column(String(50), nullable=False)  # "general" = the built-in default
    funnel_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # new -> sent (into the business's website) | needs_staff (not sent: staff finish it from Orders)
    status: Mapped[str] = mapped_column(String(30), server_default="new", nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
