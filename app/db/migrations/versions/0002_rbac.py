"""RBAC: roles, permissions, user_roles, role_permissions; tenant_users.username

Revision ID: 0002_rbac
Revises: 0001_initial
Create Date: 2026-09-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_rbac"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)

ROLES = [
    ("owner", "Full control over the tenant, billing, and staff"),
    ("admin", "Manage staff, conversations, and integrations"),
    ("agent", "Handle conversations assigned to them"),
]

PERMISSIONS = [
    ("tenant.manage", "Update tenant profile and settings"),
    ("users.manage", "Create and manage staff accounts"),
    ("social_accounts.manage", "Connect and manage social channels"),
    ("conversations.manage", "Assign, close, and hand over conversations"),
    ("conversations.reply", "Send messages in conversations"),
]

# role name -> permission codes granted to it
ROLE_PERMISSIONS = {
    "owner": [code for code, _ in PERMISSIONS],
    "admin": [
        "users.manage",
        "social_accounts.manage",
        "conversations.manage",
        "conversations.reply",
    ],
    "agent": ["conversations.manage", "conversations.reply"],
}


def _pk() -> sa.Column:
    return sa.Column(
        "id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    # ---- catalog tables ----
    op.create_table(
        "roles",
        _pk(),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("description", sa.Text()),
        *_timestamps(),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    op.create_table(
        "permissions",
        _pk(),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("description", sa.Text()),
        *_timestamps(),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )

    # ---- many-to-many join tables ----
    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            UUID,
            sa.ForeignKey("tenant_users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id", UUID, sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
        ),
    )

    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id", UUID, sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "permission_id",
            UUID,
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # ---- tenant_users.username ----
    op.add_column("tenant_users", sa.Column("username", sa.String(50), nullable=True))
    op.create_unique_constraint(
        "uq_tenant_user_username", "tenant_users", ["tenant_id", "username"]
    )

    # ---- seed the role/permission catalog ----
    roles_table = sa.table(
        "roles", sa.column("name", sa.String), sa.column("description", sa.Text)
    )
    op.bulk_insert(
        roles_table, [{"name": name, "description": desc} for name, desc in ROLES]
    )

    permissions_table = sa.table(
        "permissions", sa.column("code", sa.String), sa.column("description", sa.Text)
    )
    op.bulk_insert(
        permissions_table,
        [{"code": code, "description": desc} for code, desc in PERMISSIONS],
    )

    for role_name, codes in ROLE_PERMISSIONS.items():
        codes_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            f"""
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
            WHERE r.name = '{role_name}' AND p.code IN ({codes_list})
            """
        )

    # ---- backfill user_roles from the old string column, then drop it ----
    op.execute(
        """
        INSERT INTO user_roles (user_id, role_id)
        SELECT tu.id, r.id FROM tenant_users tu JOIN roles r ON r.name = tu.role
        """
    )
    op.drop_column("tenant_users", "role")


def downgrade() -> None:
    op.add_column(
        "tenant_users",
        sa.Column("role", sa.String(30), server_default="agent", nullable=False),
    )
    # Best-effort backfill: prefer owner > admin > agent if a user somehow holds multiple.
    op.execute(
        """
        UPDATE tenant_users tu SET role = sub.name FROM (
            SELECT ur.user_id, r.name,
                   ROW_NUMBER() OVER (
                       PARTITION BY ur.user_id
                       ORDER BY CASE r.name
                           WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END
                   ) AS rn
            FROM user_roles ur JOIN roles r ON r.id = ur.role_id
        ) sub
        WHERE sub.user_id = tu.id AND sub.rn = 1
        """
    )

    op.drop_constraint("uq_tenant_user_username", "tenant_users", type_="unique")
    op.drop_column("tenant_users", "username")

    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_table("permissions")
    op.drop_table("roles")
