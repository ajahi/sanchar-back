"""initial schema — 10 core tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _pk() -> sa.Column:
    return sa.Column(
        "id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _meta() -> sa.Column:
    return sa.Column(
        "metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


def upgrade() -> None:
    # gen_random_uuid() lives in pgcrypto.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "tenants",
        _pk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("business_type", sa.String(100)),
        sa.Column("pan_number", sa.String(50)),
        sa.Column("location", sa.Text()),
        sa.Column("owner_name", sa.String(255)),
        sa.Column("owner_phone", sa.String(50)),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        _created_at(),
        _updated_at(),
    )

    op.create_table(
        "tenant_users",
        _pk(),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(50)),
        sa.Column("password_hash", sa.Text()),
        sa.Column("role", sa.String(30), server_default="agent", nullable=False),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("tenant_id", "email", name="uq_tenant_user_email"),
    )
    op.create_index("ix_tenant_users_tenant_id", "tenant_users", ["tenant_id"])

    op.create_table(
        "customers",
        _pk(),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column("email", sa.String(255)),
        sa.Column("external_user_id", sa.String(255)),
        sa.Column("external_username", sa.String(255)),
        _meta(),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "tenant_id", "external_user_id", name="uq_customer_external_user"
        ),
    )
    op.create_index("ix_customers_tenant_id", "customers", ["tenant_id"])

    op.create_table(
        "social_accounts",
        _pk(),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(30), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=False),
        sa.Column("account_name", sa.String(255)),
        sa.Column("access_token_encrypted", sa.Text()),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        _meta(),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "platform", "external_account_id", name="uq_social_platform_account"
        ),
    )
    op.create_index("ix_social_accounts_tenant_id", "social_accounts", ["tenant_id"])

    op.create_table(
        "conversations",
        _pk(),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            UUID,
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "social_account_id", UUID, sa.ForeignKey("social_accounts.id")
        ),
        sa.Column("external_conversation_id", sa.String(255)),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), server_default="open", nullable=False),
        sa.Column("mode", sa.String(30), server_default="ai", nullable=False),
        sa.Column("assigned_to", UUID, sa.ForeignKey("tenant_users.id")),
        sa.Column("subject", sa.String(255)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        _created_at(),
        _updated_at(),
    )
    op.create_index(
        "ix_conversations_tenant_status", "conversations", ["tenant_id", "status"]
    )
    op.create_index("ix_conversations_customer", "conversations", ["customer_id"])

    op.create_table(
        "messages",
        _pk(),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_message_id", sa.String(255)),
        sa.Column("sender_type", sa.String(30), nullable=False),
        sa.Column("sender_customer_id", UUID, sa.ForeignKey("customers.id")),
        sa.Column("sender_agent_id", UUID, sa.ForeignKey("tenant_users.id")),
        sa.Column("message_type", sa.String(30), server_default="text", nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("media_url", sa.Text()),
        _meta(),
        sa.Column(
            "ai_generated", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        _created_at(),
    )
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )
    # Idempotency (spec §22): a Meta message id may only be stored once.
    op.create_index(
        "idx_unique_external_message",
        "messages",
        ["external_message_id"],
        unique=True,
        postgresql_where=sa.text("external_message_id IS NOT NULL"),
    )

    op.create_table(
        "conversation_assignments",
        _pk(),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assigned_to", UUID, sa.ForeignKey("tenant_users.id")),
        sa.Column("assigned_by", UUID, sa.ForeignKey("tenant_users.id")),
        sa.Column("reason", sa.String(100)),
        _created_at(),
    )
    op.create_index(
        "ix_conversation_assignments_conversation_id",
        "conversation_assignments",
        ["conversation_id"],
    )

    op.create_table(
        "handover_events",
        _pk(),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_mode", sa.String(30)),
        sa.Column("to_mode", sa.String(30)),
        sa.Column("reason", sa.String(100)),
        sa.Column("triggered_by", sa.String(30)),
        sa.Column("notes", sa.Text()),
        _created_at(),
    )
    op.create_index(
        "ix_handover_events_conversation_id", "handover_events", ["conversation_id"]
    )

    op.create_table(
        "knowledge_documents",
        _pk(),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("document_type", sa.String(50)),
        sa.Column("content", sa.Text()),
        sa.Column("source_url", sa.Text()),
        _meta(),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        _created_at(),
        _updated_at(),
    )
    op.create_index(
        "ix_knowledge_documents_tenant_id", "knowledge_documents", ["tenant_id"]
    )

    op.create_table(
        "audit_logs",
        _pk(),
        sa.Column(
            "tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE")
        ),
        sa.Column("actor_type", sa.String(30)),
        sa.Column("actor_id", UUID),
        sa.Column("action", sa.String(100)),
        sa.Column("entity_type", sa.String(50)),
        sa.Column("entity_id", UUID),
        _meta(),
        _created_at(),
    )
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("knowledge_documents")
    op.drop_table("handover_events")
    op.drop_table("conversation_assignments")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("social_accounts")
    op.drop_table("customers")
    op.drop_table("tenant_users")
    op.drop_table("tenants")
