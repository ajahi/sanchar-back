"""One-off: decrypt and print the most recently connected Instagram account's
access token, so you can curl-test the real Conversations API by hand.

DO NOT paste this token into chat, a commit, or anywhere else it could leak.

Usage: python -m scripts.print_token
"""
import asyncio

from sqlalchemy import select

from app.core.security import decrypt_token
from app.db.session import async_session_factory
from app.models.social_account import SocialAccount


async def main() -> None:
    async with async_session_factory() as db:
        account = (
            await db.execute(
                select(SocialAccount).order_by(SocialAccount.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if account is None or not account.access_token_encrypted:
            print("no connected account with a token found")
            return
        print(f"account: {account.account_name} ({account.external_account_id})")
        print(f"token:   {decrypt_token(account.access_token_encrypted)}")


if __name__ == "__main__":
    asyncio.run(main())
