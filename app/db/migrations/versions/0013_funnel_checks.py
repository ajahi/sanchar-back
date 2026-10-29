"""funnels.checks (run before each reply) + tenants.opening_hours (read from Shop Info); restaurant gets the hours check

Revision ID: 0013_funnel_checks
Revises: 0012_funnels
Create Date: 2026-10-29

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013_funnel_checks"
down_revision: Union[str, None] = "0012_funnels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE funnels ADD COLUMN checks JSONB NOT NULL DEFAULT '[]'::jsonb")
    # IF NOT EXISTS: a column of this name was once added by hand on the server
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS opening_hours JSONB")
    op.execute("UPDATE tenants SET opening_hours = NULL")  # whatever it held then is not the {from, hours} shape
    op.execute(
        """UPDATE funnels SET checks = '[{"type": "opening_hours", "message": "Hami ahile banda chau, {opens} dekhi order lina sakchau."}]'::jsonb
           WHERE key = 'restaurant'"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS opening_hours")
    op.execute("ALTER TABLE funnels DROP COLUMN checks")
