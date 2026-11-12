"""tenants.closure: the owner's "we are closed" switch with its own message (services/hours.closure_message)

Revision ID: 0014_tenant_closure
Revises: 0013_funnel_checks
Create Date: 2026-11-12

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0014_tenant_closure"
down_revision: Union[str, None] = "0013_funnel_checks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS closure JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS closure")
