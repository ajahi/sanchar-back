"""shop_media.tags: a few words per photo (type, colour, finish, brand) the admin confirms

Revision ID: 0010_media_tags
Revises: 0009_campaign_post
Create Date: 2026-10-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_media_tags"
down_revision: Union[str, None] = "0009_campaign_post"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shop_media",
        sa.Column("tags", postgresql.ARRAY(sa.String(30)), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("shop_media", "tags")
