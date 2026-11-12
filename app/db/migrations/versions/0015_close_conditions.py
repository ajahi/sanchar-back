"""tenants.closure renamed to tenants.close_conditions (the owner's "we are closed" switch, services/hours.py)

0014_tenant_closure is already applied on servers, so it stays as it was; this renames its column.

Revision ID: 0015_close_conditions
Revises: 0014_tenant_closure
Create Date: 2026-11-12

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0015_close_conditions"
down_revision: Union[str, None] = "0014_tenant_closure"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """DO $$ BEGIN
             IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'tenants' AND column_name = 'closure')
                AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                WHERE table_name = 'tenants' AND column_name = 'close_conditions') THEN
               ALTER TABLE tenants RENAME COLUMN closure TO close_conditions;
             END IF;
           END $$"""
    )
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS close_conditions JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants RENAME COLUMN close_conditions TO closure")
