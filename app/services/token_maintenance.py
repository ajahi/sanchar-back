"""Keeping Instagram Login tokens alive.

This is the maintenance task the Facebook Login flow did not need. A Page token derived
from a long-lived user token never expires, but an Instagram Login long-lived token lasts
roughly 60 days. When it lapses, every conversation read, every reply and every sync for
that account starts failing with error 190 — quietly, until someone notices the inbox has
gone silent.

Meta's rules shape the design:
  * a refresh is only accepted once the token is at least **24 hours old**;
  * it is refused once the token has **expired**, so there is no recovering by refreshing —
    the account has to reconnect through the browser;
  * each refresh returns a new ~60-day token, so refreshing on a schedule keeps an account
    alive indefinitely.

Intended usage is a daily cron:

    python -m scripts.refresh_tokens --within-days 10
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_token, encrypt_token
from app.models.social_account import SocialAccount
from app.services.meta import graph
from app.services.meta import instagram_login as ig_login
from app.services.meta.target import INSTAGRAM_LOGIN, provider_of

log = logging.getLogger(__name__)


@dataclass
class RefreshOutcome:
    """What happened to one account, for logging and CLI output."""

    account_id: str
    account_name: Optional[str]
    status: str  # refreshed | skipped | failed
    detail: str = ""
    expires_at: Optional[datetime] = None


async def refresh_account_token(
    db: AsyncSession, account: SocialAccount, *, autocommit: bool = True
) -> RefreshOutcome:
    """Refresh one account's token if its flow uses expiring tokens.

    Never raises: a maintenance sweep must not stop at the first bad account.
    """
    label = account.account_name or account.external_account_id

    if provider_of(account) != INSTAGRAM_LOGIN:
        return RefreshOutcome(
            str(account.id), label, "skipped", "token does not expire (Facebook Login)"
        )
    if not account.access_token_encrypted:
        return RefreshOutcome(str(account.id), label, "failed", "no stored token")

    try:
        payload = await ig_login.refresh_long_lived_token(
            decrypt_token(account.access_token_encrypted)
        )
    except graph.GraphError as exc:
        # A dead or too-young token is expected occasionally; recovering from an expired
        # one requires the user to reconnect, so say that plainly.
        hint = (
            "token is no longer refreshable — reconnect the account"
            if exc.is_auth_error
            else "will retry on the next run"
        )
        log.warning("token refresh failed for %s: %s (%s)", label, exc, hint)
        return RefreshOutcome(str(account.id), label, "failed", f"{exc} ({hint})")

    new_token = payload.get("access_token")
    if not new_token:
        return RefreshOutcome(str(account.id), label, "failed", "no access_token in response")

    seconds = ig_login.expires_in_seconds(payload)
    account.access_token_encrypted = encrypt_token(new_token)
    account.token_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=seconds) if seconds else None
    )
    if autocommit:
        await db.commit()

    log.info("refreshed token for %s, valid until %s", label, account.token_expires_at)
    return RefreshOutcome(
        str(account.id), label, "refreshed", f"valid ~{seconds // 86400}d", account.token_expires_at
    )


async def refresh_expiring_tokens(
    db: AsyncSession,
    *,
    within_days: int = 10,
    account_id: Optional[str] = None,
    autocommit: bool = True,
) -> list[RefreshOutcome]:
    """Refresh every Instagram Login account whose token expires within `within_days`.

    Also refreshes accounts with no recorded expiry, since an unknown expiry is exactly the
    case that silently lapses.
    """
    stmt = select(SocialAccount).where(
        SocialAccount.platform == "instagram",
        # Only this provider needs refreshing; Page tokens are permanent.
        SocialAccount.auth_provider == INSTAGRAM_LOGIN,
        SocialAccount.status == "active",
    )
    if account_id:
        stmt = stmt.where(SocialAccount.id == account_id)

    accounts = (await db.execute(stmt.order_by(SocialAccount.created_at))).scalars().all()

    cutoff = datetime.now(timezone.utc) + timedelta(days=within_days)
    outcomes: list[RefreshOutcome] = []
    for account in accounts:
        expires = account.token_expires_at
        if expires is not None and expires > cutoff:
            outcomes.append(
                RefreshOutcome(
                    str(account.id),
                    account.account_name or account.external_account_id,
                    "skipped",
                    f"expires {expires.date()} (outside {within_days}d window)",
                    expires,
                )
            )
            continue
        outcomes.append(await refresh_account_token(db, account, autocommit=autocommit))

    return outcomes
