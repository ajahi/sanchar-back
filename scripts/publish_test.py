"""Try posting a photo to a connected Instagram account (needs instagram_business_content_publish).

Dry run by default: shows which account and photo would be used, posts nothing. Add --publish to post.
The photo is one already uploaded in Sanchar (Shop Photos / a campaign card), so it is on public storage,
which Instagram needs: it downloads the photo from that url.

Usage (on the server):
    docker exec nepsocial-api python -m scripts.publish_test                       # list accounts + photo
    docker exec nepsocial-api python -m scripts.publish_test --account myshop      # dry run for one account
    docker exec nepsocial-api python -m scripts.publish_test --account myshop --caption "Hello" --publish
"""
import argparse
import asyncio

import httpx
from sqlalchemy import select

from app.core.security import decrypt_token
from app.db.session import async_session_factory
from app.models.shop_media import ShopMedia
from app.models.social_account import SocialAccount
from app.services.meta import instagram


async def main(account: str | None, media_id: str | None, caption: str, publish: bool) -> None:
    async with async_session_factory() as db:
        accounts = (await db.execute(select(SocialAccount).where(SocialAccount.platform == "instagram"))).scalars().all()
        if account:
            accounts = [a for a in accounts if account in (a.account_name, a.external_account_id)]
        if len(accounts) != 1:
            if not accounts:
                print(f"No Instagram account matches {account!r}." if account else "No Instagram account is connected.")
                return
            print("Connected Instagram accounts (pick one with --account <name>):")
            for a in accounts:
                print(f"  {a.account_name or '?':<30} id {a.external_account_id}")
            return
        acc = accounts[0]

        q = select(ShopMedia).where(ShopMedia.tenant_id == acc.tenant_id, ShopMedia.source == "upload")
        q = q.where(ShopMedia.id == media_id) if media_id else q.order_by(ShopMedia.created_at.desc())
        photo = (await db.execute(q.limit(1))).scalar_one_or_none()
        if photo is None:
            print("No uploaded photo found for this shop. Upload one in Sanchar (Shop Photos or a campaign card) first.")
            return

    print(f"Account : {acc.account_name} (id {acc.external_account_id})")
    print(f"Photo   : {photo.title or 'untitled'} (media id {photo.id})")
    print(f"          {photo.url}")
    print(f"Caption : {caption}")
    if not publish:
        print("\nDry run: nothing posted. Add --publish to post it.")
        return

    try:
        post = await instagram.publish_image(decrypt_token(acc.access_token_encrypted), acc.external_account_id, photo.url, caption)
    except httpx.HTTPStatusError as exc:  # Meta explains what is wrong (missing permission, bad image...) in the body
        print(f"\nInstagram said no (HTTP {exc.response.status_code}): {exc.response.text}")
        return
    print(f"\nPosted. Post id: {post['id']}")
    print(f"Link   : {post['permalink']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", help="Instagram username or account id (as listed)")
    p.add_argument("--media-id", help="a Shop Photos id; default: the newest uploaded photo")
    p.add_argument("--caption", default="Test post from Sanchar")
    p.add_argument("--publish", action="store_true", help="really post it (default: dry run)")
    a = p.parse_args()
    asyncio.run(main(a.account, a.media_id, a.caption, a.publish))
