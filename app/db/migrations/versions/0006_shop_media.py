"""shop_media — Instagram post links + uploaded images, with an in-stock flag

Revision ID: 0006_shop_media
Revises: 0005_ai_auto_reply
Create Date: 2026-10-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_shop_media"
down_revision: Union[str, None] = "0005_ai_auto_reply"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shop_media",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("ig_media_id", sa.String(64)),
        sa.Column("file_key", sa.Text()),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(20), server_default="IMAGE", nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column("caption", sa.Text()),
        sa.Column("permalink", sa.Text()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("in_stock", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "ig_media_id", name="uq_shop_media_ig"),
    )
    op.create_index("ix_shop_media_tenant_id", "shop_media", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_shop_media_tenant_id", table_name="shop_media")
    op.drop_table("shop_media")
