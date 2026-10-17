"""campaigns, campaign_links; photos and conversations point at them

Revision ID: 0008_campaigns
Revises: 0007_sales_leads
Create Date: 2026-10-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_campaigns"
down_revision: Union[str, None] = "0007_sales_leads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("price", sa.Integer()),
        sa.Column("blurb", sa.Text()),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])

    op.create_table(
        "campaign_links",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("campaign_id", UUID),
        sa.Column("ad_title", sa.String(255)),
        sa.Column("ad_image_url", sa.Text()),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "kind", "external_id", name="uq_campaign_link"),
    )
    op.create_index("ix_campaign_links_tenant_id", "campaign_links", ["tenant_id"])

    op.add_column("shop_media", sa.Column("campaign_id", UUID))
    op.add_column("shop_media", sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False))
    op.create_foreign_key("fk_shop_media_campaign", "shop_media", "campaigns", ["campaign_id"], ["id"], ondelete="SET NULL")

    op.add_column("conversations", sa.Column("campaign_link_id", UUID))
    op.create_foreign_key(
        "fk_conversations_campaign_link", "conversations", "campaign_links", ["campaign_link_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_conversations_campaign_link", "conversations", type_="foreignkey")
    op.drop_column("conversations", "campaign_link_id")
    op.drop_constraint("fk_shop_media_campaign", "shop_media", type_="foreignkey")
    op.drop_column("shop_media", "sort_order")
    op.drop_column("shop_media", "campaign_id")
    op.drop_index("ix_campaign_links_tenant_id", table_name="campaign_links")
    op.drop_table("campaign_links")
    op.drop_index("ix_campaigns_tenant_id", table_name="campaigns")
    op.drop_table("campaigns")
