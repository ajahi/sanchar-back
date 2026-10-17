"""campaigns: details for the bot, and the Instagram post published for the campaign

Revision ID: 0009_campaign_post
Revises: 0008_campaigns
Create Date: 2026-10-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_campaign_post"
down_revision: Union[str, None] = "0008_campaigns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("details", sa.Text()))
    op.add_column("campaigns", sa.Column("ig_post_id", sa.String(64)))
    op.add_column("campaigns", sa.Column("ig_permalink", sa.Text()))


def downgrade() -> None:
    op.drop_column("campaigns", "ig_permalink")
    op.drop_column("campaigns", "ig_post_id")
    op.drop_column("campaigns", "details")
