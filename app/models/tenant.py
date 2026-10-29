"""tenants — the business using the SaaS platform (the tenancy boundary)."""
from typing import Optional

from sqlalchemy import Boolean, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Tenant(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    business_type: Mapped[Optional[str]] = mapped_column(String(100))
    pan_number: Mapped[Optional[str]] = mapped_column(String(50))
    location: Mapped[Optional[str]] = mapped_column(Text)

    owner_name: Mapped[Optional[str]] = mapped_column(String(255))
    owner_phone: Mapped[Optional[str]] = mapped_column(String(50))

    status: Mapped[str] = mapped_column(String(30), server_default="active", nullable=False)

    # Opt-in: the bot only answers customers once the owner has added knowledge and switched this on.
    ai_auto_reply: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    # The conversation funnel the bot follows (funnels.key, its active version); None = the built-in order steps.
    funnel_key: Mapped[Optional[str]] = mapped_column(String(50))
    # The opening hours read out of Shop Info, {"from": <Shop Info fingerprint>, "hours": {"mon": [...], ...}}; filled
    # by services/hours.py, only for funnels with the opening_hours check. Nobody edits it.
    opening_hours: Mapped[Optional[dict]] = mapped_column(JSONB)
