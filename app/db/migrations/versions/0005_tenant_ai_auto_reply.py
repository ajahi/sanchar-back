"""tenants.ai_auto_reply — per-tenant opt-in switch for AI replies (off by default)

Revision ID: 0005_ai_auto_reply
Revises: 0004_fk_set_null
Create Date: 2026-09-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_ai_auto_reply"
down_revision: Union[str, None] = "0004_fk_set_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("ai_auto_reply", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tenants", "ai_auto_reply")
