"""Opening hours, read from the business's own Shop Info: is it open right now (Nepal time), and when does it open next.

Only funnels with the "opening_hours" check use this (restaurant: no orders while closed; a consultancy keeps booking).
The hours are written in Shop Info as free text ("10 baje dekhi 9 baje samma, Shanibar banda"), so the model reads
them out once into a table, saved on the tenant with a fingerprint of the Shop Info it came from; it is read again only
when Shop Info changes. The open/closed decision is code: the model has no clock.

Table: {"sun": ["10:00", "21:00"], "mon": ["10:00", "21:00"], ..., "sat": null}. A day missing or null is closed.
A close at or before the open runs past midnight ("18:00", "02:00"). {} or None (no hours in Shop Info) = always open.
ponytail: one range per day, no holidays; a business that needs them writes them in Shop Info and we extend the table.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, time, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.services import auto_reply

log = logging.getLogger(__name__)

NEPAL = timezone(timedelta(hours=5, minutes=45))  # no daylight saving
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # datetime.weekday() order
_NEPALI_DAYS = ("Sombar", "Mangalbar", "Budhabar", "Bihibar", "Shukrabar", "Shanibar", "Aaitabar")
DEFAULT_MESSAGE = "Hami ahile banda chau, {opens} dekhi order lina sakchau."

EXTRACT = """Below is a business's own information. Find its opening hours and reply with only a JSON object, no other text:
{"mon": ["HH:MM", "HH:MM"], "tue": ..., "wed": ..., "thu": ..., "fri": ..., "sat": ..., "sun": ...}
Each day is [opening time, closing time] in 24-hour time, or null if it is closed that day. "Open daily 10am-9pm" gives
every day ["10:00", "21:00"]. If the information says nothing about opening hours, reply {}."""


def _clock(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def valid(hours: dict) -> bool:
    """Every key a day, every value null or ["HH:MM", "HH:MM"]."""
    try:
        for day, r in hours.items():
            if day not in DAYS or (r is not None and (len(r) != 2 or not all(isinstance(x, str) and len(x) == 5 for x in r))):
                return False
            if r is not None:
                _clock(r[0]), _clock(r[1])  # ValueError on "25:00" or "ab:cd"
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def _range(hours: dict, day: int) -> tuple[time, time] | None:
    r = hours.get(DAYS[day % 7])
    return (_clock(r[0]), _clock(r[1])) if r else None


def is_open(hours: dict | None, now: datetime | None = None) -> bool:
    if not hours:
        return True
    now = (now or datetime.now(NEPAL)).astimezone(NEPAL)
    t, today = now.time(), now.weekday()
    r = _range(hours, today)
    if r and (r[0] <= t < r[1] if r[0] < r[1] else t >= r[0]):
        return True
    y = _range(hours, today - 1)  # yesterday's range running past midnight
    return bool(y and y[1] <= y[0] and t < y[1])


def _when(at: time) -> str:
    part = "bihana" if at.hour < 12 else "diuso" if at.hour < 17 else "beluka"
    h = at.hour % 12 or 12
    return f"{part} {h}{f':{at.minute:02d}' if at.minute else ''} baje"


def next_opening(hours: dict, now: datetime | None = None) -> str | None:
    """"bholi bihana 10 baje", "aaja diuso 2 baje", "Aaitabar bihana 10 baje", or None if no day opens."""
    now = (now or datetime.now(NEPAL)).astimezone(NEPAL)
    for ahead in range(8):
        r = _range(hours, now.weekday() + ahead)
        if r and (ahead or now.time() < r[0]):
            day = "aaja" if ahead == 0 else "bholi" if ahead == 1 else _NEPALI_DAYS[(now.weekday() + ahead) % 7]
            return f"{day} {_when(r[0])}"
    return None


def closed_message(hours: dict | None, template: str = DEFAULT_MESSAGE, now: datetime | None = None) -> str | None:
    """What the bot says to an order while the business is closed ({opens} filled in); None while it is open."""
    if is_open(hours, now):
        return None
    when = next_opening(hours, now)
    return template.replace("{opens}", when) if when else "Hami ahile banda chau."


def closed_note(message: str) -> str:
    """The note for the model while closed (sent like photo_hint, so the cached prompt stays the same)."""
    return ("Note from the business, not the customer: the business is CLOSED right now. Answer questions normally, "
            f"but do not take an order or ask for order details. If they want to order, tell them: \"{message}\"")


def parse(reply: str) -> dict | None:
    """The model's hours table out of its reply, or None when it isn't one."""
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    try:
        table = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    return table if isinstance(table, dict) and valid(table) else None


async def for_tenant(db: AsyncSession, tenant: Tenant, knowledge: str) -> dict | None:
    """The tenant's hours as its Shop Info states them. Read by the model only when Shop Info changed since the last
    read (the table is kept on tenants.opening_hours with the fingerprint). On any failure: None (open), retried on
    the next message. Changes the tenant, never commits."""
    fingerprint = hashlib.sha256(knowledge.encode()).hexdigest()[:16]
    saved = tenant.opening_hours or {}
    if saved.get("from") == fingerprint:
        return saved.get("hours") or None
    try:
        reply = await auto_reply.chat([{"role": "system", "content": EXTRACT}, {"role": "user", "content": knowledge}])
    except Exception:  # noqa: BLE001 — the model down must not stop replies; open, and try again next message
        log.warning("reading opening hours failed for tenant %s", tenant.id, exc_info=True)
        return None
    table = parse(reply)
    if table is None:
        log.warning("opening hours for tenant %s: not a table: %r", tenant.id, reply[:200])
        return None
    tenant.opening_hours = {"from": fingerprint, "hours": table}
    log.info("opening hours for tenant %s read from Shop Info: %s", tenant.id, table or "none given")
    return table or None
