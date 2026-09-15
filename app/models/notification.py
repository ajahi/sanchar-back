"""notifications — a record of something that happened and how it was/should be delivered.

One row per notification. `source_table` + `entity_id` point back at what triggered it
(e.g. a conversations row); `event` names what happened; `medium` is how it goes out
(in_app / email / sms / push); `status` tracks delivery. `created_at` is the event time.

(Named source_table/created_at rather than table/time because both are SQL reserved words.)
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPKMixin


class Notification(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "notifications"

    # Tenant scope (spec §17); NULL = platform-level notification.
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Recipient; NULL = broadcast / no specific user.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # What triggered it: the source table name + the row's id.
    source_table: Mapped[Optional[str]] = mapped_column(String(50))
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(PG_UUID(as_uuid=True))

    # e.g. "message_received", "handover_created", "account_disconnected"
    event: Mapped[str] = mapped_column(String(100), nullable=False)

    # in_app | email | sms | push
    medium: Mapped[str] = mapped_column(String(30), server_default="in_app", nullable=False)
    # pending | sent | failed | read
    status: Mapped[str] = mapped_column(String(30), server_default="pending", nullable=False)

    subject: Mapped[Optional[str]] = mapped_column(String(255))
    message: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    # created_at (the event time) comes from CreatedAtMixin.
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
