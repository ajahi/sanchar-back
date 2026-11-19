"""POST /social-accounts/whatsapp (Channels → Connect): links a number to the caller's tenant,
subscribes its account to our webhooks, refuses an account we can't reach, a number not on that
account, and a number another tenant already linked. Postgres, always rolled back.

    python -m pytest tests/test_wa_link.py
"""
import asyncio

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1 import social_accounts
from app.api.v1.social_accounts import _get_or_create_account, link_whatsapp, whatsapp_signup
from app.core.security import decrypt_token
from app.db.session import async_session_factory
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.schemas.social_account import WhatsAppLinkIn, WhatsAppSignupIn

subscribed: list[str] = []


async def _fake_numbers(token: str, waba_id: str) -> list[dict]:
    assert token == "platform-tok"
    if waba_id == "99999":  # not shared with us
        raise httpx.HTTPStatusError("400", request=httpx.Request("GET", "x"), response=httpx.Response(400))
    return [{"id": "11111", "display_phone_number": "+977 989-8989898", "verified_name": "Tenant1 Shop"}]


async def _fake_subscribe(token: str, waba_id: str) -> None:
    subscribed.append(waba_id)


async def _status(coro) -> int:
    with pytest.raises(HTTPException) as err:
        await coro
    return err.value.status_code


async def _run() -> None:
    async with async_session_factory() as db:
        db.commit = db.flush  # the endpoint commits; keep everything in the rolled-back transaction
        kw = dict(expires_in=60, source_uri="", long_lived_token="t")
        t1, owner1, _ = await _get_or_create_account(db, ig_user_id="ig-t1", username="tenant1", **kw)
        t2, owner2, _ = await _get_or_create_account(db, ig_user_id="ig-t2", username="tenant2", **kw)
        t1, t2 = await db.get(Tenant, t1.id), await db.get(Tenant, t2.id)
        link = lambda pn, waba, tenant, user: link_whatsapp(  # noqa: E731
            WhatsAppLinkIn(phone_number_id=pn, waba_id=waba), tenant=tenant, user=user, db=db
        )

        assert await _status(link("11111", "99999", t1, owner1)) == 400  # can't reach the account
        assert await _status(link("22222", "55555", t1, owner1)) == 400  # number not on it
        assert subscribed == []

        out = await link("11111", "55555", t1, owner1)
        assert (out.display_phone_number, out.waba_id, out.live) == ("+977 989-8989898", "55555", True)
        await link("11111", "55555", t1, owner1)  # re-link by the same tenant is fine
        assert subscribed == ["55555", "55555"]

        acc = (await db.execute(select_wa("11111"))).scalar_one()
        assert acc.tenant_id == t1.id and acc.account_name == "Tenant1 Shop"
        assert decrypt_token(acc.access_token_encrypted) == "platform-tok"
        assert acc.meta["waba_id"] == "55555"

        assert await _status(link("11111", "55555", t2, owner2)) == 409  # never moved to tenant2
        await db.rollback()


def select_wa(phone_number_id: str):
    return select(SocialAccount).where(
        SocialAccount.platform == "whatsapp", SocialAccount.external_account_id == phone_number_id
    )


def test_link_whatsapp(monkeypatch) -> None:
    monkeypatch.setattr(social_accounts.settings, "waba_token", "platform-tok")
    monkeypatch.setattr(social_accounts.whatsapp, "fetch_waba_phone_numbers", _fake_numbers)
    monkeypatch.setattr(social_accounts.whatsapp, "subscribe_app", _fake_subscribe)
    asyncio.run(_run())


registered: list[str] = []


async def _fake_exchange(code: str) -> str:
    return "business-tok"


async def _fake_granted(token: str) -> list[str]:
    assert token == "business-tok"
    return ["55555"]  # Meta's record of the grant, not anything the browser sent


async def _fake_signup_numbers(token: str, waba_id: str) -> list[dict]:
    return [
        {"id": "33333", "display_phone_number": "+977 980-0000001", "verified_name": "Old", "status": "CONNECTED"},
        {"id": "44444", "display_phone_number": "+977 980-0000002", "verified_name": "New", "status": "PENDING"},
    ]


async def _fake_register(token: str, phone_number_id: str, pin: str) -> None:
    assert len(pin) == 6 and pin.isdigit()
    registered.append(phone_number_id)


async def _run_signup() -> None:
    async with async_session_factory() as db:
        db.commit = db.flush
        kw = dict(expires_in=60, source_uri="", long_lived_token="t")
        t1, owner1, _ = await _get_or_create_account(db, ig_user_id="ig-s1", username="signup1", **kw)
        t2, owner2, _ = await _get_or_create_account(db, ig_user_id="ig-s2", username="signup2", **kw)
        t1, t2 = await db.get(Tenant, t1.id), await db.get(Tenant, t2.id)
        signup = lambda tenant, user: whatsapp_signup(  # noqa: E731
            WhatsAppSignupIn(code="one-time-code"), tenant=tenant, user=user, db=db
        )

        out = await signup(t1, owner1)
        assert [o.phone_number_id for o in out] == ["33333", "44444"]
        assert registered == ["44444"]  # only the number new to Cloud API
        new = (await db.execute(select_wa("44444"))).scalar_one()
        assert decrypt_token(new.access_token_encrypted) == "business-tok" and "pin_encrypted" in new.meta

        assert await _status(signup(t2, owner2)) == 409  # both numbers belong to tenant1: nothing linked
        await db.rollback()


def test_whatsapp_signup(monkeypatch) -> None:
    monkeypatch.setattr(social_accounts.facebook, "exchange_popup_code", _fake_exchange)
    monkeypatch.setattr(social_accounts.whatsapp, "fetch_granted_wabas", _fake_granted)
    monkeypatch.setattr(social_accounts.whatsapp, "fetch_waba_phone_numbers", _fake_signup_numbers)
    monkeypatch.setattr(social_accounts.whatsapp, "subscribe_app", _fake_subscribe)
    monkeypatch.setattr(social_accounts.whatsapp, "register_number", _fake_register)
    asyncio.run(_run_signup())
