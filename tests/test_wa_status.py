"""GET /social-accounts/whatsapp (the Channels page's PING): a webhook stamps waba_id +
last_webhook_at, live fields when Meta answers, stored values (live=False) when it doesn't,
and another tenant's number never shows. Postgres, always rolled back.

    python -m pytest tests/test_wa_status.py
"""
import asyncio

import httpx

from app.api.v1 import social_accounts
from app.api.v1.social_accounts import whatsapp_accounts
from app.api.v1.webhooks import stamp_whatsapp_event
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant


async def _fake_fetch_phone_number(token: str, phone_number_id: str, fields: str) -> dict:
    if token == "dead":
        raise httpx.HTTPError("revoked")
    assert "status" in fields
    return {"display_phone_number": "+977 976-0000000", "verified_name": "Shop",
            "status": "CONNECTED", "quality_rating": "GREEN"}


async def _fake_fetch_subscribed_apps(token: str, waba_id: str) -> list[str]:
    assert waba_id == "waba-1"
    return ["messenger-rag"]


async def _run() -> None:
    async with async_session_factory() as db:
        mine, other = Tenant(name="T"), Tenant(name="Other")
        db.add_all([mine, other])
        await db.flush()
        db.add_all([
            SocialAccount(tenant_id=mine.id, platform="whatsapp", external_account_id="pn-live",
                          access_token_encrypted=encrypt_token("ok")),
            SocialAccount(tenant_id=mine.id, platform="whatsapp", external_account_id="pn-dead",
                          account_name="Stored Name", access_token_encrypted=encrypt_token("dead"),
                          meta={"display_phone_number": "+977 976-1111111"}),
            SocialAccount(tenant_id=other.id, platform="whatsapp", external_account_id="pn-other",
                          access_token_encrypted=encrypt_token("ok")),
        ])
        await db.flush()
        await stamp_whatsapp_event(db, "pn-live", "waba-1")
        await stamp_whatsapp_event(db, "pn-unknown", "waba-x")  # not ours: ignored
        await db.flush()

        # same created_at inside one transaction, so the order isn't fixed; key by id
        got = {a.phone_number_id: a for a in await whatsapp_accounts(tenant=mine, db=db)}
        assert set(got) == {"pn-live", "pn-dead"}
        live, dead = got["pn-live"], got["pn-dead"]
        assert live.live and live.status == "CONNECTED" and live.quality_rating == "GREEN"
        assert live.waba_id == "waba-1" and live.subscribed_apps == ["messenger-rag"]
        assert live.last_webhook_at is not None
        assert not dead.live and dead.status is None and dead.subscribed_apps is None
        assert (dead.verified_name, dead.display_phone_number) == ("Stored Name", "+977 976-1111111")
        await db.rollback()


def test_whatsapp_accounts(monkeypatch) -> None:
    monkeypatch.setattr(social_accounts.whatsapp, "fetch_phone_number", _fake_fetch_phone_number)
    monkeypatch.setattr(social_accounts.whatsapp, "fetch_subscribed_apps", _fake_fetch_subscribed_apps)
    asyncio.run(_run())
