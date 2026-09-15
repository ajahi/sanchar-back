"""Rename tenant_users -> users (unified identity), add notifications, seed super_admin

Revision ID: 0003_users_notifications
Revises: 0002_rbac
Create Date: 2026-09-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_users_notifications"
down_revision: Union[str, None] = "0002_rbac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # ---- tenant_users -> users ----
    op.rename_table("tenant_users", "users")
    op.execute("ALTER INDEX ix_tenant_users_tenant_id RENAME TO ix_users_tenant_id")

    op.alter_column("users", "phone", new_column_name="phone_number")
    op.add_column(
        "users",
        sa.Column(
            "verified", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    # Platform admins have no tenant.
    op.alter_column("users", "tenant_id", existing_type=UUID, nullable=True)

    # Per-tenant uniqueness is unsafe once tenant_id can be NULL -> go global.
    op.drop_constraint("uq_tenant_user_email", "users", type_="unique")
    op.drop_constraint("uq_tenant_user_username", "users", type_="unique")
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_unique_constraint("uq_users_username", "users", ["username"])

    # ---- platform role + permission ----
    op.execute(
        """
        INSERT INTO roles (name, description)
        VALUES ('super_admin', 'Platform operator with cross-tenant control')
        """
    )
    op.execute(
        """
        INSERT INTO permissions (code, description)
        VALUES ('platform.manage', 'Operate the platform across all tenants')
        """
    )
    # super_admin gets every permission (including platform.manage).
    op.execute(
        """
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
        WHERE r.name = 'super_admin'
        """
    )

    # ---- notifications ----
    op.create_table(
        "notifications",
        sa.Column(
            "id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE")),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("source_table", sa.String(50)),
        sa.Column("entity_id", UUID),
        sa.Column("event", sa.String(100), nullable=False),
        sa.Column("medium", sa.String(30), server_default="in_app", nullable=False),
        sa.Column("status", sa.String(30), server_default="pending", nullable=False),
        sa.Column("subject", sa.String(255)),
        sa.Column("message", sa.Text()),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])


def downgrade() -> None:
    op.drop_table("notifications")

    op.execute("DELETE FROM permissions WHERE code = 'platform.manage'")
    op.execute("DELETE FROM roles WHERE name = 'super_admin'")

    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.drop_constraint("uq_users_username", "users", type_="unique")
    op.create_unique_constraint("uq_tenant_user_email", "users", ["tenant_id", "email"])
    op.create_unique_constraint(
        "uq_tenant_user_username", "users", ["tenant_id", "username"]
    )

    op.alter_column("users", "tenant_id", existing_type=UUID, nullable=False)
    op.drop_column("users", "verified")
    op.alter_column("users", "phone_number", new_column_name="phone")

    op.execute("ALTER INDEX ix_users_tenant_id RENAME TO ix_tenant_users_tenant_id")
    op.rename_table("users", "tenant_users")
