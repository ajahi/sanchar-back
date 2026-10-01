"""shop_media — images admins can send to customers: the page's latest Instagram posts (links only,
refreshed from Instagram) and files uploaded to object storage. Also carries the in-stock flag."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class ShopMedia(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "shop_media"
    # NULL ig_media_id (uploads) never collides in Postgres unique constraints.
    __table_args__ = (UniqueConstraint("tenant_id", "ig_media_id", name="uq_shop_media_ig"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source: Mapped[str] = mapped_column(String(20), nullable=False)  # instagram | upload
    ig_media_id: Mapped[Optional[str]] = mapped_column(String(64))
    file_key: Mapped[Optional[str]] = mapped_column(Text)  # object-storage key, uploads only
    url: Mapped[str] = mapped_column(Text, nullable=False)  # Instagram CDN links expire; refreshed on sync
    media_type: Mapped[str] = mapped_column(String(20), server_default="IMAGE", nullable=False)

    title: Mapped[Optional[str]] = mapped_column(String(255))
    caption: Mapped[Optional[str]] = mapped_column(Text)
    permalink: Mapped[Optional[str]] = mapped_column(Text)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    in_stock: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
