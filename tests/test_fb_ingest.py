"""Messenger ingest: customer keyed by PSID with the Graph name, one facebook conversation, message
idempotent on mid, echo = agent, thumbs-up is a sticker (not a photo), unknown Page ignored, and the
webhook handshake / signature / Page-object routing.

Runs against the configured Postgres inside a transaction that is always rolled back.
    python -m pytest tests/test_fb_ingest.py
"""
import asyncio
import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.v1.webhooks import ingest_event, stamp_webhook_event
from app.core.config import settings
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.main import app
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services.meta import facebook


async def _run(monkeypatch) -> None:
    async def fake_profile(*_a, **_k):
        return {"name": "Sita"}

    monkeypatch.setattr(facebook, "fetch_customer_profile", fake_profile)  # no network in tests

    async with async_session_factory() as db:
        t = Tenant(name="T")
        db.add(t)
        await db.flush()
        db.add(
            SocialAccount(
                tenant_id=t.id, platform="facebook", external_account_id="page-1",
                access_token_encrypted=encrypt_token("tok"),
            )
        )
        await db.flush()

        def ev(mid, **message):
            return {"sender": {"id": "psid-1"}, "recipient": {"id": "page-1"}, "timestamp": 1700000000000,
                    "message": {"mid": mid, **message}}

        hi = ev("m1", text="hi")
        first = await ingest_event(db, "page-1", hi, platform="facebook")
        assert first is not None and await ingest_event(db, "page-1", hi, platform="facebook") is None  # redelivery
        assert await ingest_event(db, "page-unknown", ev("m9", text="x"), platform="facebook") is None
        # the same ids on the Instagram path must not match a facebook account
        assert await ingest_event(db, "page-1", ev("m8", text="x")) is None

        like = ev("m2", attachments=[{"type": "image", "payload": {"url": "http://x/like.png", "sticker_id": 369239263222822}}])
        photo = ev("m3", attachments=[{"type": "image", "payload": {"url": "http://x/p.jpg"}}])
        for e in (like, photo):
            await ingest_event(db, "page-1", e, platform="facebook")
        echo = {"sender": {"id": "page-1"}, "recipient": {"id": "psid-1"},
                "message": {"mid": "m4", "text": "yo", "is_echo": True}}
        assert await ingest_event(db, "page-1", echo, platform="facebook") is None  # echo never queues a reply

        customer = (await db.execute(select(Customer).where(Customer.tenant_id == t.id))).scalar_one()
        assert (customer.external_user_id, customer.name) == ("psid-1", "Sita")
        convo = (await db.execute(select(Conversation).where(Conversation.tenant_id == t.id))).scalar_one()
        assert convo.channel == "facebook" and first[0] == convo.id
        msgs = (
            await db.execute(
                select(Message).where(Message.conversation_id == convo.id).order_by(Message.external_message_id)
            )
        ).scalars().all()
        assert [(m.external_message_id, m.sender_type, m.message_type) for m in msgs] == [
            ("m1", "customer", "text"), ("m2", "customer", "sticker"),
            ("m3", "customer", "image"), ("m4", "agent", "text"),
        ]

        await stamp_webhook_event(db, "facebook", "page-1")
        acct = (await db.execute(select(SocialAccount).where(SocialAccount.tenant_id == t.id))).scalar_one()
        assert acct.meta["last_webhook_at"]
        await db.rollback()


def test_fb_ingest(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))


def test_fb_webhook_http(monkeypatch) -> None:
    monkeypatch.setattr(settings, "facebook_webhook_verify_token", "fbtok")
    monkeypatch.setattr(settings, "instagram_webhook_verify_token", "")
    monkeypatch.setattr(settings, "meta_app_secret", "shh")
    client = TestClient(app)
    q = {"hub.mode": "subscribe", "hub.challenge": "42"}
    assert client.get("/api/v1/webhooks/facebook", params={**q, "hub.verify_token": "fbtok"}).text == "42"
    assert client.get("/api/v1/webhooks/facebook", params={**q, "hub.verify_token": "nope"}).status_code == 403
    # an unset token must not match the empty hub.verify_token Meta never sends
    monkeypatch.setattr(settings, "facebook_webhook_verify_token", "")
    assert client.get("/api/v1/webhooks/facebook", params=q).status_code == 403

    body = json.dumps({"object": "page", "entry": [{"id": "no-such-page", "messaging": []}]}).encode()
    bad = client.post("/api/v1/webhooks/facebook", content=body, headers={"x-hub-signature-256": "sha256=0"})
    assert bad.status_code == 403
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    ok = client.post("/api/v1/webhooks/facebook", content=body, headers={"x-hub-signature-256": sig})
    assert ok.status_code == 200 and ok.json() == {"status": "ok"}  # "page" is routed, not "ignored"


def test_fb_login_redirect(monkeypatch) -> None:
    from urllib.parse import parse_qs, urlparse

    from app.core.security import create_access_token

    client = TestClient(app, follow_redirects=False)
    # not configured -> 503, never a broken redirect to Facebook
    assert client.get("/api/v1/social-accounts/facebook/login").status_code == 503

    monkeypatch.setattr(settings, "facebook_app_id", "123")
    monkeypatch.setattr(settings, "meta_app_secret", "shh")
    monkeypatch.setattr(settings, "facebook_redirect_uri", "https://x.test/cb")
    anon = client.get("/api/v1/social-accounts/facebook/login")
    assert "fb_error=login_required" in anon.headers["location"]  # Pages need a tenant to attach to

    cookie = create_access_token("00000000-0000-4000-8000-000000000001", tenant_id=None, roles=[])
    client.cookies.set(settings.session_cookie_name, cookie)
    go = client.get("/api/v1/social-accounts/facebook/login")
    url = urlparse(go.headers["location"])
    q = parse_qs(url.query)
    assert url.netloc == "www.facebook.com" and q["client_id"] == ["123"] and q["redirect_uri"] == ["https://x.test/cb"]
    assert "pages_messaging" in q["scope"][0] and q["state"][0]

    # callback: user denied, and a forged state, both bounce to the dashboard without touching Meta
    assert "fb_error=access_denied" in client.get("/api/v1/social-accounts/facebook/callback?error=access_denied").headers["location"]
    assert "fb_error=invalid_state" in client.get("/api/v1/social-accounts/facebook/callback?code=c&state=forged").headers["location"]


import httpx

_REAL_CLIENT = httpx.AsyncClient  # captured before any patching: monkeypatch replaces it on the shared httpx module


def _mock_graph(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        facebook.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
    )


def test_fetch_linked_instagram(monkeypatch) -> None:
    seen = []

    def ok(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "p1", "instagram_business_account": {"id": "17841431601921729"}})

    _mock_graph(monkeypatch, ok)
    assert asyncio.run(facebook.fetch_linked_instagram("TOK", "p1")) == "17841431601921729"
    assert seen[0].url.path.endswith("/p1") and seen[0].headers["authorization"] == "Bearer TOK"

    _mock_graph(monkeypatch, lambda r: httpx.Response(200, json={"id": "p1"}))  # Page with no Instagram
    assert asyncio.run(facebook.fetch_linked_instagram("TOK", "p1")) is None
    _mock_graph(monkeypatch, lambda r: httpx.Response(400, json={"error": {}}))  # Meta refuses: unknown, not an error
    assert asyncio.run(facebook.fetch_linked_instagram("TOK", "p1")) is None


async def _linked_instagram_rules(monkeypatch) -> None:
    from app.api.v1.social_accounts import _linked_instagram, _tenant_instagram_ids

    async def fake_linked(*_a):
        return "ig-1"

    monkeypatch.setattr(facebook, "fetch_linked_instagram", fake_linked)
    async with async_session_factory() as db:
        mine, other = Tenant(name="Mine"), Tenant(name="Other")
        db.add_all([mine, other])
        await db.flush()
        db.add(SocialAccount(tenant_id=mine.id, platform="instagram", external_account_id="ig-1"))
        await db.flush()
        assert await _linked_instagram(db, mine.id, "tok", "page-1") == "ig-1"  # same workspace: fine
        assert "ig-1" in await _tenant_instagram_ids(db, mine.id)
        try:
            await _linked_instagram(db, other.id, "tok", "page-1")  # its Instagram belongs to someone else
            raise AssertionError("should have refused")
        except PermissionError:
            pass
        await db.rollback()


def test_linked_instagram_rules(monkeypatch) -> None:
    asyncio.run(_linked_instagram_rules(monkeypatch))


def test_profile_prefers_known_participant_name() -> None:
    from app.api.v1.webhooks import _profile

    calls = []

    class Chan:
        async def fetch_customer_profile(self, token, psid):
            calls.append(psid)
            return {"name": "From Graph"}

    known = {"id": "p1", "name": "Sita K"}  # a conversation participant entry
    assert asyncio.run(_profile(Chan(), "t", "p1", known, True)) == known and calls == []  # no extra Graph call
    assert asyncio.run(_profile(Chan(), "t", "p1", {"id": "p1"}, True)) == {"name": "From Graph"}  # no name: ask
    assert asyncio.run(_profile(Chan(), "t", "p1", None, False)) == {}  # lookups switched off
