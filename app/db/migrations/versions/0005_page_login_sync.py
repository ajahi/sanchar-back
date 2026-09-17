"""Facebook Login for Business: Page-backed accounts, conversation dedup, sync runs

Adds what the historical-import path needs:
  * social_accounts.external_page_id — Instagram messaging is driven through the owning
    Page's token, so the Page id has to be stored alongside the IG account id.
  * social_accounts.last_synced_at  — when a backfill last completed for this account.
  * conversations unique (social_account_id, external_conversation_id) — Meta's thread id
    becomes the idempotency key that makes re-importing a thread a no-op. Existing rows
    are all NULL here (webhooks never learned the thread id), so the index is safe to add.
  * sync_runs — an auditable record of each import attempt.

Revision ID: 0005_page_login_sync
Revises: 0004_fk_set_null
Create Date: 2026-02-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_page_login_sync"
down_revision: Union[str, None] = "0004_fk_set_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "social_accounts",
        sa.Column("external_page_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_social_accounts_external_page_id",
        "social_accounts",
        ["external_page_id"],
    )
    op.add_column(
        "social_accounts",
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "uq_conversation_external",
        "conversations",
        ["social_account_id", "external_conversation_id"],
        unique=True,
        postgresql_where=sa.text("external_conversation_id IS NOT NULL"),
    )

    op.create_table(
        "sync_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("social_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column(
            "trigger_source", sa.String(length=30), server_default="manual", nullable=False
        ),
        sa.Column("conversations_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "conversations_created", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "conversations_failed", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("messages_created", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("messages_skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="sync_runs_tenant_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["social_account_id"],
            ["social_accounts.id"],
            name="sync_runs_social_account_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="sync_runs_pkey"),
    )
    op.create_index("ix_sync_runs_tenant_id", "sync_runs", ["tenant_id"])
    op.create_index(
        "ix_sync_runs_account_created", "sync_runs", ["social_account_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_sync_runs_account_created", table_name="sync_runs")
    op.drop_index("ix_sync_runs_tenant_id", table_name="sync_runs")
    op.drop_table("sync_runs")

    op.drop_index("uq_conversation_external", table_name="conversations")

    op.drop_column("social_accounts", "last_synced_at")
    op.drop_index("ix_social_accounts_external_page_id", table_name="social_accounts")
    op.drop_column("social_accounts", "external_page_id")
