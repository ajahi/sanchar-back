"""social_accounts.auth_provider — record which Meta auth flow issued each token

The backend now supports both flows at once, and they are not interchangeable: Instagram
Login uses graph.instagram.com with the Instagram account id, while Facebook Login for
Business uses graph.facebook.com with the Page id. Which one applies has to be stored, not
inferred from whether a Page id happens to be present.

The server default `instagram_login` is also the correct backfill value: every row that
existed before this migration was created by the Instagram Login flow.

Revision ID: 0006_auth_provider
Revises: 0005_page_login_sync
Create Date: 2026-02-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_auth_provider"
down_revision: Union[str, None] = "0005_page_login_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "social_accounts",
        sa.Column(
            "auth_provider",
            sa.String(length=30),
            server_default="instagram_login",
            nullable=False,
        ),
    )
    # Any row that already carries a Page id came from the Facebook Login flow.
    op.execute(
        "UPDATE social_accounts SET auth_provider = 'facebook_login' "
        "WHERE external_page_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("social_accounts", "auth_provider")
