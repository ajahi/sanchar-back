"""sync_runs — one row per historical conversation import / resync attempt.

A backfill talks to a slow, rate-limited, paginated third-party API, so "did it work?"
needs to be answerable after the fact. Each run records what it saw and what it wrote,
which makes a re-run safe to reason about and failures debuggable without log archaeology.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin

# Lifecycle: pending -> running -> succeeded | failed | partial
SYNC_STATUSES = ("pending", "running", "succeeded", "partial", "failed")


class SyncRun(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_account_created", "social_account_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("social_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(String(30), server_default="pending", nullable=False)
    # What kicked this off: manual (API), cli, or scheduled. Named `trigger_source`
    # because `trigger` is a reserved word in Postgres.
    trigger_source: Mapped[str] = mapped_column(
        String(30), server_default="manual", nullable=False
    )

    conversations_seen: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    conversations_created: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    # Threads that could not be imported. A non-zero value with status `partial` is the
    # signal that a re-run is worth doing.
    conversations_failed: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    messages_created: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    # Duplicates skipped — the proof that re-running an import is harmless.
    messages_skipped: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)

    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
