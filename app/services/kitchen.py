"""Send confirmed chat orders into a business's own online-order system: the same request its website checkout
makes (thecubeventure.com's guest checkout), so its usual flow runs: owner email, kitchen ticket, staff call.

The site's menu and delivery areas are synced into order_integrations and written to a Shop Info topic, so the
bot quotes the exact item names and prices. The order the bot collected (free text, "2 Buff Jhol Momo") is then
matched against that menu as plain text, no model: an exact name (any word order), or a near spelling
("sadeko" / "sadheko"). A line that matches nothing, or two items equally well, is left for staff to pick:
an unmatched line is never guessed into the kitchen.

ponytail: one storefront API (the one thecubeventure.com uses); another site needs its own client here.
"""
import difflib
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.knowledge import KnowledgeDocument
from app.models.order_integration import OrderIntegration
from app.models.sales_lead import SalesLead
from app.services.http_client import SSL_CTX
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

SYNC_EVERY = timedelta(hours=6)  # menu and delivery charges older than this are re-read before an order is built
PICKUP = "Pick up"
MENU_TITLE = "Menu (synced from the website)"
_HEADERS = {"User-Agent": "Sanchar/1.0 (+https://sanchar.utsavupahar.com)"}

_NUM_WORDS = {"ek": 1, "euta": 1, "one": 1, "dui": 2, "duita": 2, "two": 2, "tin": 3, "tinta": 3, "three": 3,
              "char": 4, "charta": 4, "four": 4, "panch": 5, "panchta": 5, "five": 5}
_UNITS = {"plate", "plates", "pcs", "pc", "piece", "pieces", "cup", "cups", "glass", "glasses", "bottle", "bottles",
          "x", "ta", "wata", "ota", "each", "per", "set", "sets", "rs"}
_LINE_SPLIT = re.compile(r"\s*(?:[,;\n]|\band\b|\bra\b)\s*", re.IGNORECASE)  # names hold "&" and "/", never these


def allowed_url(url: str) -> bool:
    """Only https on a host in KITCHEN_HOSTS: the server calls this URL, so it is never free text."""
    u = urlparse(url)
    hosts = {h.strip().lower() for h in settings.kitchen_hosts.split(",") if h.strip()}
    return u.scheme == "https" and (u.hostname or "").lower() in hosts


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower().replace("mo:mo", "momo")))


async def sync(db: AsyncSession, integ: OrderIntegration) -> None:
    """Re-read the site's menu and delivery areas, and rewrite the Shop Info menu topic. Raises httpx.HTTPError."""
    base = integ.base_url.rstrip("/")
    items: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=20, verify=SSL_CTX, headers=_HEADERS) as client:
        for page in range(1, 50):  # products come grouped by category, 40 categories a page
            resp = await client.get(f"{base}/product/getall", params={"page": page, "limit": 40})
            resp.raise_for_status()
            groups = resp.json().get("data") or []
            if not groups:
                break
            for g in groups:
                for p in g.get("products") or []:
                    items[p["_id"]] = p
        resp = await client.get(f"{base}/product/getadd")
        resp.raise_for_status()
        areas = resp.json().get("data") or []
    integ.catalog = list(items.values())
    integ.locations = [{"address": a["address"], "rate": int(a.get("rate") or 0)} for a in areas if a.get("address")]
    integ.synced_at = datetime.now(timezone.utc)
    await _write_menu(db, integ)


def menu_text(integ: OrderIntegration) -> str:
    by_cat: dict[str, list[str]] = {}
    for p in integ.catalog:
        by_cat.setdefault(str(p.get("category") or "Other").strip(), []).append(f"{p['name'].strip()} - Rs {p['price']}")
    areas = ", ".join(f"{a['address']} Rs {a['rate']}" for a in integ.locations)
    return (
        "The full menu, from our website (updated automatically). Use these exact item names when taking an order.\n"
        + "\n".join(f"{cat}: " + "; ".join(lines) for cat, lines in by_cat.items())
        + (f"\nDelivery areas and delivery charge: {areas}. Pick up from the cafe is free. When taking a delivery "
           "order, ask which of these areas they are in and write that area in the address." if areas else "")
    )


async def _write_menu(db: AsyncSession, integ: OrderIntegration) -> None:
    doc = await db.get(KnowledgeDocument, integ.knowledge_id) if integ.knowledge_id else None
    if doc is None:
        doc = KnowledgeDocument(tenant_id=integ.tenant_id, title=MENU_TITLE, document_type="topic", meta={"source": "kitchen"})
        db.add(doc)
        await db.flush()
        integ.knowledge_id = doc.id
    doc.content, doc.status = menu_text(integ), "active"


async def fresh(db: AsyncSession, integ: OrderIntegration) -> None:
    """Sync if the copy is old; on failure keep using it (an hour-old price beats no order)."""
    if integ.synced_at and datetime.now(timezone.utc) - integ.synced_at < SYNC_EVERY:
        return
    try:
        await sync(db, integ)
    except httpx.HTTPError:
        log.warning("kitchen menu sync failed for tenant %s", integ.tenant_id, exc_info=True)


def match_line(text: str, catalog: list[dict]) -> tuple[dict | None, int]:
    """(menu item or None, quantity) for one order line like "2 Buff Jhol Momo - Rs 250 each"."""
    words = _norm(re.sub(r"rs\.?\s*[\d,]+", " ", text, flags=re.IGNORECASE)).split()
    qty = next((int(w) for w in words if w.isdigit() and 0 < int(w) < 100), None) or next(
        (_NUM_WORDS[w] for w in words if w in _NUM_WORDS), 1)
    name = [w for w in words if not w.isdigit() and w not in _NUM_WORDS and w not in _UNITS]
    if not name:
        return None, qty
    same = [p for p in catalog if sorted(_norm(p["name"]).split()) == sorted(name)]  # exact, any word order
    if len(same) == 1:
        return same[0], qty
    wanted = " ".join(name)
    scored = sorted(((difflib.SequenceMatcher(None, wanted, _norm(p["name"])).ratio(), i) for i, p in enumerate(catalog)), reverse=True)
    if scored and scored[0][0] >= 0.9 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.04):
        return catalog[scored[0][1]], qty
    return None, qty


def match_location(address: str, locations: list[dict]) -> dict | None:
    """The delivery area named in the address (exact, or a near spelling), {"address": PICKUP, "rate": 0} for a
    pick-up, or None: staff pick it from the same list the website's dropdown shows."""
    a = _norm(address)
    if re.search(r"\bpick ?up\b", a):
        return {"address": PICKUP, "rate": 0}
    hits = [loc for loc in locations if f" {_norm(loc['address'])} " in f" {a} "]
    if hits:
        return max(hits, key=lambda loc: len(loc["address"]))
    words, best = a.split(), (0.0, None)
    for loc in locations:
        n = _norm(loc["address"])
        k = len(n.split())
        for i in range(len(words) - k + 1):
            r = difflib.SequenceMatcher(None, n, " ".join(words[i:i + k])).ratio()
            if r >= 0.88 and r > best[0]:
                best = (r, loc)
    return best[1]


def plan(order: dict, integ: OrderIntegration) -> dict:
    """The lines and area Sanchar would send for this order, before anyone edits them."""
    lines = []
    for text in [t for t in _LINE_SPLIT.split(order["items"]) if t.strip()]:
        item, qty = match_line(text, integ.catalog)
        lines.append({"text": text.strip(), "product_id": item["_id"] if item else None, "quantity": qty})
    loc = match_location(order["address"], integ.locations)
    return {"lines": lines, "location": loc["address"] if loc else None}


def build_body(order: dict, lines: list[tuple[dict, int]], location: dict, email: str | None) -> dict:
    """What the website's guest checkout posts (its Billing page), for these menu items and delivery area."""
    pickup = location["address"] == PICKUP
    charge = 0 if pickup else int(location["rate"])
    phone = re.sub(r"\D", "", order["phone"])
    phone = phone[3:] if phone.startswith("977") and len(phone) == 13 else phone  # the site takes 98XXXXXXXX
    return {
        "data": {"fullName": order["name"], "address": order["address"], "phone": phone, "email": email or "",
                 "deliveryLocation": "Pick-up" if pickup else location["address"]},
        "item": [{**p, "quantity": q, "selectedSize": None, "selectedColor": None, "requests": None} for p, q in lines],
        "product": [p["_id"] for p, _ in lines],
        "deliveryOption": PICKUP if pickup else location["address"],
        "totalAmount": sum(int(p["price"]) * q for p, q in lines) + charge,
        "deliveryCharge": charge,
    }


def resolve(integ: OrderIntegration, picks: list[tuple[str, int]], location: str) -> tuple[list[tuple[dict, int]], dict]:
    """Menu items and area for ids / an area name chosen by staff (or by plan). ValueError if one is not on the menu."""
    by_id = {p["_id"]: p for p in integ.catalog}
    if not picks or any(pid not in by_id or not 0 < q < 100 for pid, q in picks):
        raise ValueError("Pick an item from the menu, with a quantity, for every line.")
    if location == PICKUP:
        loc = {"address": PICKUP, "rate": 0}
    else:
        loc = next((a for a in integ.locations if a["address"] == location), None)
        if loc is None:
            raise ValueError("Pick the delivery area from the list.")
    return [(by_id[pid], q) for pid, q in picks], loc


async def send(db: AsyncSession, integ: OrderIntegration, lead: SalesLead, body: dict) -> dict:
    """Place the order on the site; record it on the lead. Raises httpx.HTTPError (or ValueError if the site
    answers without success). Commits before the network call (no DB connection held while it runs)."""
    await db.commit()
    async with httpx.AsyncClient(timeout=20, verify=SSL_CTX, headers=_HEADERS) as client:
        resp = await client.post(f"{integ.base_url.rstrip('/')}/order/placeorder", json=body)
        resp.raise_for_status()
        answer = resp.json()
    if not answer.get("success"):
        raise ValueError(str(answer.get("message") or "The order system did not accept the order")[:300])
    data = answer.get("data") or {}
    sent = {
        "order": lead.meta.get("order"),  # which collected order this was: a changed order may be sent again
        "site_order_id": data.get("_id"),
        "kitchen_order_id": data.get("intradeOrderId"),
        "total": body["totalAmount"],
        "area": body["deliveryOption"],
        "lines": [[i["name"], i["quantity"], i["price"]] for i in body["item"]],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    lead.meta = {**{k: v for k, v in (lead.meta or {}).items() if k != "kitchen_error"}, "kitchen": sent}
    return sent


def already_sent(lead: SalesLead) -> bool:
    k = (lead.meta or {}).get("kitchen")
    return bool(k) and k.get("order") == (lead.meta or {}).get("order")


async def auto_send(db: AsyncSession, lead: SalesLead) -> None:
    """Right after the customer confirmed: send it if this business turned sending and auto-send on, and every
    line and the area matched. Anything else waits in Orders for staff. Never raises."""
    integ = await db.get(OrderIntegration, lead.tenant_id)
    order = (lead.meta or {}).get("order")
    if integ is None or not (integ.send_enabled and integ.auto_send) or not order or already_sent(lead):
        return
    await fresh(db, integ)
    p = plan(order, integ)
    if p["location"] is None or any(line["product_id"] is None for line in p["lines"]):
        log.info("kitchen auto-send skipped for lead %s: a line or the area needs staff", lead.id)
        return
    try:
        lines, loc = resolve(integ, [(line["product_id"], line["quantity"]) for line in p["lines"]], p["location"])
        await send(db, integ, lead, build_body(order, lines, loc, integ.email))
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else str(exc)[:300]
        log.warning("kitchen auto-send failed for lead %s: %s", lead.id, detail)
        lead.meta = {**lead.meta, "kitchen_error": detail}
        await create_notification(
            db, event="kitchen_send_failed", tenant_id=lead.tenant_id, source_table="sales_leads", entity_id=lead.id,
            subject=f"Order from {order['name']} did not reach the kitchen",
            message="Open Orders and send it again, or call the cafe.", payload={"lead_id": str(lead.id)},
        )
