"""order_integrations — a business's own online-order system (its website checkout) that confirmed chat orders
are sent into, so its usual flow runs: owner email, kitchen ticket (KOT), staff call. One per business.
services/kitchen.py talks to it; catalog and locations are synced copies of its menu and delivery areas."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class OrderIntegration(TimestampMixin, Base):
    __tablename__ = "order_integrations"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. https://admin.thecubeventure.com/api/v1
    email: Mapped[Optional[str]] = mapped_column(String(255))  # sent as the order's email: chats have none
    # Off: nothing reaches the kitchen, staff only see what would be sent. auto_send: send the moment the customer
    # confirms, when every line and the delivery area matched; otherwise staff send it from Orders.
    send_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    auto_send: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    catalog: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)  # the site's products
    locations: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)  # [{address, rate}]
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # The Shop Info topic the synced menu is written to (so the bot quotes exact names and prices).
    knowledge_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_documents.id", ondelete="SET NULL")
    )
