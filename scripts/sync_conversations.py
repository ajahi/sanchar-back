"""Historical Instagram conversation import / resync — CLI.

Webhooks only carry messages sent after a Page is subscribed, so a newly connected
account has an empty inbox until someone writes in. This script runs the same importer
the API exposes, from a shell.

Usage:
    python -m scripts.sync_conversations --list
    python -m scripts.sync_conversations --account <uuid>
    python -m scripts.sync_conversations --all
    python -m scripts.sync_conversations --all --fetch-profiles
    python -m scripts.sync_conversations --account <uuid> --max-conversations 100
    python -m scripts.sync_conversations --account <uuid> --subscribe-webhooks

Re-running is safe: messages already stored are skipped, which is exactly what the
`messages_skipped` counter in each run reports.

Note that Graph exposes only a thread's 20 most recent messages, so this restores
threads and recent context — not a complete DM archive. Depth arrives via webhooks from
the moment the account is connected.
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.security import decrypt_token
from app.db.session import async_session_factory
from app.models.social_account import SocialAccount
from app.services import conversation_sync
from app.services.meta import facebook_login as fb_login
from app.services.meta import graph
from app.services.meta.target import describe


async def _list_accounts() -> int:
    async with async_session_factory() as db:
        rows = await db.execute(select(SocialAccount).order_by(SocialAccount.created_at))
        accounts = list(rows.scalars().all())

    if not accounts:
        print("No connected accounts.")
        return 0

    print(f"{'id':38} {'provider':16} {'account':20} last_synced")
    for a in accounts:
        print(
            f"{str(a.id):38} {a.auth_provider:16} {(a.account_name or '-')[:20]:20} "
            f"{a.last_synced_at.isoformat() if a.last_synced_at else 'never'}"
        )
        # Show the host and path root the sync will actually use, since the two auth flows
        # differ and a wrong provider is invisible otherwise.
        route = describe(a)
        print(f"{'':38} -> {route or 'NO USABLE TOKEN'}")
    return 0


async def _resolve_targets(account_id: str | None) -> list:
    """Accounts to act on: one by UUID or external id, or every connected account."""
    async with async_session_factory() as db:
        stmt = select(SocialAccount).order_by(SocialAccount.created_at)
        if account_id:
            parsed = _as_uuid(account_id)
            stmt = stmt.where(
                SocialAccount.id == parsed
                if parsed
                else SocialAccount.external_account_id == account_id
            )
        rows = await db.execute(stmt)
        return list(rows.scalars().all())


def _as_uuid(value: str):
    """The value as a UUID, or None when it is an external account id instead."""
    import uuid

    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _sync_one(account, *, max_conversations, fetch_profiles) -> conversation_sync.SyncRun:
    async with async_session_factory() as db:
        # Re-load inside this session; the object came from a different one.
        account = await db.get(SocialAccount, account.id)
        return await conversation_sync.sync_account(
            db,
            account,
            trigger_source="cli",
            max_conversations=max_conversations,
            fetch_profiles=fetch_profiles,
        )


async def _subscribe(account) -> int:
    """Re-register this account for messaging webhooks, on whichever flow it uses."""
    from app.services.meta import instagram_login as ig_login
    from app.services.meta.target import FACEBOOK_LOGIN, provider_of

    async with async_session_factory() as db:
        account = await db.get(SocialAccount, account.id)
        token = decrypt_token(account.access_token_encrypted)
        try:
            if provider_of(account) == FACEBOOK_LOGIN:
                if not account.external_page_id:
                    print(f"{account.external_account_id}: no Page id stored; reconnect.")
                    return 1
                result = await fb_login.subscribe_page_webhooks(
                    account.external_page_id, token
                )
            else:
                result = await ig_login.subscribe_webhooks(token)
        except graph.GraphError as exc:
            print(f"{account.external_account_id}: subscribe failed: {exc}", file=sys.stderr)
            return 1
    print(f"{account.external_account_id}: subscribed -> {result}")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="list connected accounts")
    parser.add_argument("--account", help="account id (UUID) or external Instagram account id")
    parser.add_argument("--all", action="store_true", help="sync every connected account")
    parser.add_argument(
        "--max-conversations",
        type=int,
        default=None,
        help="cap threads per account (default: SYNC_MAX_CONVERSATIONS)",
    )
    parser.add_argument(
        "--fetch-profiles",
        action="store_true",
        help="also fetch each new customer's display name (one extra API call each)",
    )
    parser.add_argument(
        "--subscribe-webhooks",
        action="store_true",
        help="re-register this Page for messaging webhooks instead of syncing",
    )
    args = parser.parse_args()

    if args.list:
        return await _list_accounts()

    if not args.account and not args.all:
        parser.print_help()
        return 2

    accounts = await _resolve_targets(args.account)
    if not accounts:
        if args.account:
            print(f"No account matched {args.account!r}. Try --list.", file=sys.stderr)
        else:
            print("No connected accounts.", file=sys.stderr)
        return 1

    if args.subscribe_webhooks:
        failures = 0
        for account in accounts:
            failures += await _subscribe(account)
        return 1 if failures else 0

    exit_code = 0
    for account in accounts:
        label = account.account_name or account.external_account_id
        print(f"→ {label} ({account.id})")
        run = await _sync_one(
            account,
            max_conversations=args.max_conversations,
            fetch_profiles=args.fetch_profiles,
        )
        print(
            f"  {run.status}: seen={run.conversations_seen} "
            f"created={run.conversations_created} failed={run.conversations_failed} "
            f"messages={run.messages_created} skipped={run.messages_skipped}"
            + (" (truncated)" if (run.meta or {}).get("truncated") else "")
        )
        if run.error:
            print(f"  error: {run.error}", file=sys.stderr)
        if run.status in ("failed", "partial"):
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
