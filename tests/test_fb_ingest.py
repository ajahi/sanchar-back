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
