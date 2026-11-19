"""Confirmed orders (and bookings, enquiries: any funnel submission) sent as JSON to the business's own API.

Each business sets it up itself (tenants.json_post: {"url", "secret", "enabled"}, the /tenants/me/json-post
endpoints). It is separate from the website checkout post (kitchen.send), which stays as it is: a business uses
either, or both.

    POST <url>
    Content-Type: application/json
    X-Sanchar-Event: order.confirmed          (<the funnel's label>.confirmed)
    X-Sanchar-Delivery: <submission id>       the same on every retry: a repeat with this id is the same order
    X-Sanchar-Signature: sha256=<hex>         HMAC-SHA256 of the raw body with the business's secret
    body: payload() below; the order's prices come from kitchen.quote_order when the business has a synced menu

Retries: 3 tries (now, +2 s, +6 s) on a network error or a 5xx; a 4xx is not retried. The outcome goes on the
submission (result.json_post); a failure pings the business. Only public https addresses are called.
ponytail: retries inside one task, no outbox: a restart in the middle loses that send (the submission shows it).
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.funnel import FunnelSubmission
from app.models.order_integration import OrderIntegration
from app.models.tenant import Tenant
from app.services import kitchen
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

RETRY_DELAYS = (0, 2, 6)  # seconds before each try
TIMEOUT = 10


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


async def url_problem(url: str) -> str | None:
    """Why this address can't be used, or None: https only, and a host on the public internet (never the server's
    own network: the server makes this call)."""
    u = urlparse(url or "")
    if u.scheme != "https" or not u.hostname:
        return "Use an https:// address."
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return f"{u.hostname} could not be found."
    if any(not ipaddress.ip_address(info[4][0]).is_global for info in infos):
        return "That address is inside a private network: use your API's public address."
    return None


def payload(*, label: str, sub: FunnelSubmission, tenant: Tenant, customer: Customer | None,
            convo: Conversation | None, order: dict | None) -> dict:
    fields = sub.fields or {}
    return {
        "event": f"{label}.confirmed",
        "id": str(sub.id),
        "created_at": (sub.created_at or datetime.now(timezone.utc)).isoformat(),
        "business": {"id": str(tenant.id), "name": tenant.name},
        "funnel": {"key": sub.funnel_key, "version": sub.funnel_version},
        "customer": {
            "name": fields.get("name") or (customer.name if customer else None),
            "phone": fields.get("phone") or (customer.phone if customer else None),
            "channel": convo.channel if convo else None,
            "handle": customer.external_username if customer else None,
        },
        "fields": fields,  # everything the funnel collected, as confirmed
        "order": order,  # items with price and amount, delivery, total, unmatched lines (kitchen.quote_order) or null
        "conversation_id": str(sub.conversation_id) if sub.conversation_id else None,
    }


async def send(url: str, secret: str, event: str, delivery_id: str, body: dict) -> dict:
    """POST it; {"status": "sent" | "failed", "attempts", "at", "code"?, "error"?}. Never raises."""
    at = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731
    if problem := await url_problem(url):
        return {"status": "failed", "attempts": 0, "at": at(), "error": problem}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "Sanchar/1.0", "X-Sanchar-Event": event,
               "X-Sanchar-Delivery": delivery_id, "X-Sanchar-Signature": sign(secret, raw)}
    last: dict = {}
    tries = 0
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        for delay in RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            tries += 1
            try:
                resp = await client.post(url, content=raw, headers=headers)
            except httpx.HTTPError as exc:
                last = {"error": repr(exc)[:300]}
                continue
            if resp.status_code < 300:
                return {"status": "sent", "attempts": tries, "at": at(), "code": resp.status_code}
            last = {"code": resp.status_code, "error": resp.text[:300]}
            if resp.status_code < 500:  # the API refused this order: sending it again won't change that
                break
    return {"status": "failed", "attempts": tries, "at": at(), **last}


async def post_submission(submission_id, label: str) -> None:
    """Send one confirmed submission to the business's API, if it has one switched on. Its own session: whatever
    fails here never costs the customer their reply or the saved order. Never raises."""
    try:
        async with async_session_factory() as s:
            sub = await s.get(FunnelSubmission, submission_id)
            tenant = await s.get(Tenant, sub.tenant_id) if sub else None
            cfg = (tenant.json_post or {}) if tenant else {}
            if not (sub and cfg.get("enabled") and cfg.get("url")):
                return
            customer = await s.get(Customer, sub.customer_id) if sub.customer_id else None
            convo = await s.get(Conversation, sub.conversation_id) if sub.conversation_id else None
            integ = await s.get(OrderIntegration, sub.tenant_id)
            order = (kitchen.quote_order(sub.fields, integ)
                     if integ is not None and integ.catalog and {"items", "address"} <= set(sub.fields) else None)
            body = payload(label=label, sub=sub, tenant=tenant, customer=customer, convo=convo, order=order)
            await s.commit()  # no DB connection held during the call
            result = await send(cfg["url"], cfg.get("secret") or "", body["event"], body["id"], body)
            log.info("json post of %s %s for tenant %s: %s", label, sub.id, sub.tenant_id, result)
            sub.result = {**(sub.result or {}), "json_post": result}
            if result["status"] != "sent":
                await create_notification(
                    s, event="json_post_failed", tenant_id=sub.tenant_id, source_table="funnel_submissions",
                    entity_id=sub.id, subject=f"Your API did not receive a confirmed {label}",
                    message=f"{cfg['url']} answered {result.get('code') or result.get('error')}. The {label} is "
                            "saved in Sanchar: check your API, then add it there by hand.",
                    payload={"submission_id": str(sub.id)},
                )
            await s.commit()
    except Exception:  # noqa: BLE001 — never let this reach the reply
        log.exception("json post of submission %s failed", submission_id)
