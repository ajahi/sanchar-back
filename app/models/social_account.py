"""social_accounts — connected messaging channels (Instagram now; Messenger/WhatsApp later)."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class SocialAccount(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint("platform", "external_account_id", name="uq_social_platform_account"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    platform: Mapped[str] = mapped_column(String(30), nullable=False)

    # The id Meta uses for this identity in webhook `entry[].id` and in Graph paths.
    # Instagram: the professional account id. Facebook: the Page id.
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_name: Mapped[Optional[str]] = mapped_column(String(255))

    # Facebook Login for Business: the Page that owns this messaging identity. Instagram
    # messaging is driven through the Page's token, so the Page id is needed to call
    # /{page-id}/conversations and /{page-id}/messages.
    external_page_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    # Fernet ciphertext only — never store the plaintext Meta token.
    access_token_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    # NULL means "does not expire" — a Page token derived from a long-lived user token
    # is permanent, so this stays NULL for the Facebook Login flow.
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Last successful historical import/resync for this account.
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(30), server_default="active", nullable=False)

    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
