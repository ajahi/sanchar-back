"""handover_events — the AI<->human transition audit trail (why did it happen?)."""
import uuid
from typing import Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPKMixin


class HandoverEvent(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "handover_events"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    from_mode: Mapped[Optional[str]] = mapped_column(String(30))
    to_mode: Mapped[Optional[str]] = mapped_column(String(30))

    # e.g. customer_requested_human, low_confidence, refund_request, agent_manual_takeover
    reason: Mapped[Optional[str]] = mapped_column(String(100))
    triggered_by: Mapped[Optional[str]] = mapped_column(String(30))
    notes: Mapped[Optional[str]] = mapped_column(Text)
