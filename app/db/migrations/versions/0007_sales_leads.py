"""sales_leads — customers who gave their phone number in a chat

Revision ID: 0007_sales_leads
Revises: 0006_shop_media
Create Date: 2026-10-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_sales_leads"
down_revision: Union[str, None] = "0006_shop_media"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sales_leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("social_account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("phone", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), server_default="new", nullable=False),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["social_account_id"], ["social_accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "customer_id", name="uq_sales_lead_customer"),
    )
    op.create_index("ix_sales_leads_tenant_id", "sales_leads", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_sales_leads_tenant_id", table_name="sales_leads")
    op.drop_table("sales_leads")
