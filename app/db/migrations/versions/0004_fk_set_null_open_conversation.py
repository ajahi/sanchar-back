"""SET NULL on user/social_account references; one open conversation per customer+account

Revision ID: 0004_fk_set_null
Revises: 0003_users_notifications
Create Date: 2026-09-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_fk_set_null"
down_revision: Union[str, None] = "0003_users_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, referenced table) — constraint names are Postgres defaults (<table>_<col>_fkey).
_FKS = [
    ("conversations", "assigned_to", "users"),
    ("conversations", "social_account_id", "social_accounts"),
    ("messages", "sender_agent_id", "users"),
    ("conversation_assignments", "assigned_to", "users"),
    ("conversation_assignments", "assigned_by", "users"),
]


def _recreate(ondelete: Union[str, None]) -> None:
    for table, col, ref in _FKS:
        name = f"{table}_{col}_fkey"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, ref, [col], ["id"], ondelete=ondelete)


def upgrade() -> None:
    _recreate("SET NULL")
    op.create_index(
        "uq_open_conversation",
        "conversations",
        ["customer_id", "social_account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_open_conversation", table_name="conversations")
    _recreate(None)
