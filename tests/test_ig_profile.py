"""GET /social-accounts/instagram/profile: live fields when Instagram answers, stored
id/username (live=False) when it doesn't. Postgres, always rolled back.

    python -m pytest tests/test_ig_profile.py
"""
import asyncio

import httpx

from app.api.v1 import social_accounts
from app.api.v1.social_accounts import _get_or_create_account, instagram_profiles
from app.db.session import async_session_factory
from app.models.tenant import Tenant


async def _fake_fetch_profile(token: str, fields: str) -> dict:
    if token == "dead":
        raise httpx.HTTPError("expired")
    assert "profile_picture_url" in fields
    return {"user_id": "ig-live", "username": "shop_live", "name": "Shop", "account_type": "BUSINESS",
            "profile_picture_url": "https://x/p.jpg", "followers_count": 10, "follows_count": 2, "media_count": 5}


async def _run() -> None:
    async with async_session_factory() as db:
        kw = dict(expires_in=60, source_uri="")
        tenant, owner, _ = await _get_or_create_account(db, ig_user_id="ig-live", username="old_name", long_lived_token="ok", **kw)
        await _get_or_create_account(db, ig_user_id="ig-dead", username="shop_dead", long_lived_token="dead", user=owner, **kw)
        await _get_or_create_account(  # another tenant's account must not show up
            db, ig_user_id="ig-other", username="other", long_lived_token="ok", **kw
        )

        live, dead = await instagram_profiles(tenant=await db.get(Tenant, tenant.id), db=db)
        assert live.live and live.username == "shop_live" and live.followers_count == 10
        assert live.profile_picture_url == "https://x/p.jpg"
        assert not dead.live and dead.id == "ig-dead" and dead.username == "shop_dead"
        assert dead.profile_picture_url is None
        await db.rollback()


def test_instagram_profiles(monkeypatch) -> None:
    monkeypatch.setattr(social_accounts.instagram, "fetch_profile", _fake_fetch_profile)
    asyncio.run(_run())
