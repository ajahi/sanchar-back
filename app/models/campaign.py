"""campaigns — what an ad (or a reel / bio link) promotes: its own price, a one-line description and photos.

campaign_links maps an ad id / `ref` code from Meta to a campaign. Unknown ids register themselves
(campaign_id NULL) so staff can assign them once; conversations point at the link they came from, so
mapping an ad later applies to every chat that arrived through it.
"""
import uuid
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Campaign(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "campaigns"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[Optional[int]] = mapped_column(Integer)  # whole rupees
    blurb: Mapped[Optional[str]] = mapped_column(Text)  # e.g. "pure chinnon silk fabric, comes with quality guarantee"
    status: Mapped[str] = mapped_column(String(20), server_default="active", nullable=False)  # active | paused


class CampaignLink(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "campaign_links"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "external_id", name="uq_campaign_link"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # ad | ref
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    ad_title: Mapped[Optional[str]] = mapped_column(String(255))
    ad_image_url: Mapped[Optional[str]] = mapped_column(Text)
