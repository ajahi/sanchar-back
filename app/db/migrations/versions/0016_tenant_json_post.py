"""tenants.json_post: where confirmed orders are also sent as JSON, set by the business (services/json_post.py)

Revision ID: 0016_tenant_json_post
Revises: 0015_close_conditions
Create Date: 2026-11-12

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016_tenant_json_post"
down_revision: Union[str, None] = "0015_close_conditions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS json_post JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS json_post")
