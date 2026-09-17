"""Refresh expiring Instagram Login tokens.

Instagram Login tokens last roughly 60 days and then stop working, taking the inbox with
them. This refreshes them before that happens. Run it daily from cron:

    # every day at 03:15
    15 3 * * * docker exec nepsocial-api python3 -m scripts.refresh_tokens --within-days 10

Usage:
    python -m scripts.refresh_tokens                    # refresh what expires within 10 days
    python -m scripts.refresh_tokens --within-days 20
    python -m scripts.refresh_tokens --status           # report only, change nothing
    python -m scripts.refresh_tokens --account <uuid>

Exit code is non-zero if any refresh failed, so cron surfaces the problem.
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.social_account import SocialAccount
from app.services import token_maintenance
from app.services.meta.target import describe


def _fmt(value) -> str:
    return value.isoformat() if value else "-"


async def _status() -> int:
    async with async_session_factory() as db:
        rows = await db.execute(
            select(SocialAccount).order_by(SocialAccount.created_at)
        )
        accounts = list(rows.scalars().all())

    if not accounts:
        print("No connected accounts.")
        return 0

    print(f"{'account':24} {'provider':16} {'token expires':26} route")
    for a in accounts:
        print(
            f"{(a.account_name or '-')[:24]:24} {a.auth_provider:16} "
            f"{_fmt(a.token_expires_at):26} {describe(a) or '-'}"
        )
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--within-days", type=int, default=10)
    parser.add_argument("--account", help="single account UUID")
    parser.add_argument(
        "--status", action="store_true", help="report token state without refreshing"
    )
    args = parser.parse_args()

    if args.status:
        return await _status()

    async with async_session_factory() as db:
        outcomes = await token_maintenance.refresh_expiring_tokens(
            db, within_days=args.within_days, account_id=args.account
        )

    if not outcomes:
        print("No Instagram Login accounts to refresh.")
        return 0

    failures = 0
    for outcome in outcomes:
        print(
            f"{(outcome.account_name or '-')[:24]:24} {outcome.status:10} "
            f"{outcome.detail}  expires={_fmt(outcome.expires_at)}"
        )
        if outcome.status == "failed":
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
