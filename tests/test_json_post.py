"""Confirmed orders sent as JSON to the business's own API (services/json_post.py, /tenants/me/json-post): the
signature, retries without duplicates, refusals, private addresses, the settings, and a real submission end to end.
Network faked with httpx.MockTransport. DB parts against Postgres.

    python -m pytest tests/test_json_post.py
"""
import asyncio
import csv
import hashlib
import hmac
import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.tenants import get_json_post, rotate_json_post_secret, save_json_post
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.funnel import FunnelSubmission
from app.models.notification import Notification
from app.models.order_integration import OrderIntegration
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant import JsonPostIn
from app.services import json_post

URL = "https://orders.example.com/hook"
REAL_CLIENT = httpx.AsyncClient
with open(Path(__file__).parent / "fixtures" / "cube_menu.csv", encoding="utf-8") as f:
    MENU = [{"_id": f"p{i}", "name": r["name"], "price": int(r["price"]), "category": r["category"]}
            for i, r in enumerate(csv.DictReader(f))]


def fake_api(monkeypatch, answers: list):
    """The business's API: answers in order (a status code, or an exception to raise); returns what it received."""
    got: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return httpx.Response(a, text="ok" if a < 300 else "nope")

    monkeypatch.setattr(json_post.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(json_post, "RETRY_DELAYS", (0, 0, 0))

    async def public(url):
        return None if url.startswith("https://") else "Use an https:// address."

    monkeypatch.setattr(json_post, "url_problem", public)
    return got


def test_signed_and_retried_without_duplicates(monkeypatch):
    got = fake_api(monkeypatch, [500, httpx.ConnectError("down"), 200])
    out = asyncio.run(json_post.send(URL, "s3cret", "order.confirmed", "sub-1", {"total": 380}))
    assert out["status"] == "sent" and out["attempts"] == 3
    assert {r.headers["X-Sanchar-Delivery"] for r in got} == {"sub-1"}  # same id every try: the API can de-duplicate
    raw = got[-1].content
    assert got[-1].headers["X-Sanchar-Signature"] == "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    assert json.loads(raw) == {"total": 380} and got[-1].headers["X-Sanchar-Event"] == "order.confirmed"


def test_a_refusal_is_not_retried(monkeypatch):
    got = fake_api(monkeypatch, [422])
    out = asyncio.run(json_post.send(URL, "s", "order.confirmed", "sub-2", {}))
    assert out["status"] == "failed" and out["attempts"] == 1 and out["code"] == 422 and len(got) == 1
    fake_api(monkeypatch, [503, 503, 503])
    assert asyncio.run(json_post.send(URL, "s", "e", "id", {}))["attempts"] == 3


def test_only_public_https_addresses(monkeypatch):
    assert asyncio.run(json_post.url_problem("http://orders.example.com")) == "Use an https:// address."
    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.10"):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, ip=ip, **k: [(2, 1, 6, "", (ip, 443))])
        assert "private network" in asyncio.run(json_post.url_problem("https://my-api.example.com")), ip
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert asyncio.run(json_post.url_problem("https://my-api.example.com")) is None


async def _settings(monkeypatch) -> None:
    async def public(url):
        return None if url.startswith("https://") else "Use an https:// address."

    monkeypatch.setattr(json_post, "url_problem", public)
    async with async_session_factory() as db:
        db.commit = db.flush
        try:
            tenant, owner = Tenant(name="Cube Cafe"), User(name="O", email="o@jsonpost.test")
            db.add_all([tenant, owner])
            await db.flush()
            with pytest.raises(HTTPException) as e:
                await save_json_post(JsonPostIn(enabled=True), tenant, owner, db)
            assert e.value.status_code == 422
            with pytest.raises(HTTPException) as e:
                await save_json_post(JsonPostIn(url="http://insecure.example.com", enabled=True), tenant, owner, db)
            assert "https" in e.value.detail
            out = await save_json_post(JsonPostIn(url=URL, enabled=True), tenant, owner, db)
            assert out.enabled and out.url == URL and len(out.secret) > 30
            again = await save_json_post(JsonPostIn(url=URL, enabled=False), tenant, owner, db)
            assert again.secret == out.secret and not again.enabled  # switching off keeps the secret
            rotated = await rotate_json_post_secret(tenant, owner, db)
            assert rotated.secret != out.secret and (await get_json_post(tenant)).secret == rotated.secret
        finally:
            await db.rollback()


def test_settings(monkeypatch):
    asyncio.run(_settings(monkeypatch))


async def _end_to_end(monkeypatch, answers) -> tuple[FunnelSubmission, list, int]:
    got = fake_api(monkeypatch, answers)
    async with async_session_factory() as db:
        tenant = Tenant(name="Cube Cafe", json_post={"url": URL, "secret": "k", "enabled": True})
        db.add(tenant)
        await db.flush()
        cust = Customer(tenant_id=tenant.id, external_user_id="ig-1", external_username="ajahi_amit")
        db.add(cust)
        await db.flush()
        convo = Conversation(tenant_id=tenant.id, customer_id=cust.id, channel="instagram", mode="ai")
        db.add_all([convo, OrderIntegration(tenant_id=tenant.id, base_url="https://site.test/api/v1", send_enabled=False,
                                            auto_send=False, catalog=MENU, locations=[{"address": "Chipledhunga", "rate": 80}])])
        await db.flush()
        sub = FunnelSubmission(tenant_id=tenant.id, conversation_id=convo.id, customer_id=cust.id, funnel_key="restaurant",
                               funnel_version=3, fields={"name": "Amit", "phone": "9812345678", "address": "Chipledhunga, Expert 2nd floor",
                                                         "items": "2 x Chicken Dry Mix"}, result={})
        db.add(sub)
        await db.commit()
        sub_id, tenant_id = sub.id, tenant.id
    await json_post.post_submission(sub_id, "order")
    async with async_session_factory() as db:
        sub = await db.get(FunnelSubmission, sub_id)
        pings = len((await db.execute(select(Notification).where(Notification.tenant_id == tenant_id,
                                                                  Notification.event == "json_post_failed"))).all())
        return sub, got, pings


def test_a_confirmed_order_reaches_the_api(monkeypatch):
    sub, got, pings = asyncio.run(_end_to_end(monkeypatch, [200]))
    assert sub.result["json_post"]["status"] == "sent" and pings == 0
    body = json.loads(got[0].content)
    assert body["event"] == "order.confirmed" and body["id"] == str(sub.id)
    assert body["customer"] == {"name": "Amit", "phone": "9812345678", "channel": "instagram", "handle": "ajahi_amit"}
    assert (body["order"]["subtotal"], body["order"]["delivery"]["charge"], body["order"]["total"]) == (380, 80, 460)
    assert body["fields"]["address"] == "Chipledhunga, Expert 2nd floor"


def test_a_failed_send_is_kept_and_the_business_pinged(monkeypatch):
    sub, got, pings = asyncio.run(_end_to_end(monkeypatch, [500, 500, 500]))
    assert sub.result["json_post"]["status"] == "failed" and sub.result["json_post"]["attempts"] == 3 and pings == 1
