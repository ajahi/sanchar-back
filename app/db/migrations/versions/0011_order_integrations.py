"""order_integrations: a business's own online-order system that confirmed chat orders are sent into

Revision ID: 0011_order_integrations
Revises: 0010_media_tags
Create Date: 2026-10-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_order_integrations"
down_revision: Union[str, None] = "0010_media_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_integrations",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("send_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("auto_send", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("catalog", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("locations", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.Column("knowledge_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_documents.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("order_integrations")
