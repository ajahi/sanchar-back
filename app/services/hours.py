"""Opening hours (tenants.opening_hours): is the business open right now, Nepal time, and when does it open next.

The model has no clock, so code decides: while closed, the bot still answers questions but does not take an order
(reply_pipeline adds a note for the model, funnels.finish_reply refuses a summary or order the model writes anyway).

Format: {"sun": ["10:00", "21:00"], "mon": ["10:00", "21:00"], ..., "sat": null}. A day missing or null is closed.
A close at or before the open runs past midnight ("18:00", "02:00"). None (no hours set) = always open.
ponytail: one range per day, no holidays; add a "closed until" date when a business needs it.
"""
from datetime import datetime, time, timedelta, timezone

NEPAL = timezone(timedelta(hours=5, minutes=45))  # no daylight saving
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # datetime.weekday() order
_NEPALI_DAYS = ("Sombar", "Mangalbar", "Budhabar", "Bihibar", "Shukrabar", "Shanibar", "Aaitabar")


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


def closed_message(hours: dict | None, now: datetime | None = None) -> str | None:
    """What the bot says to an order while the business is closed; None while it is open."""
    if is_open(hours, now):
        return None
    when = next_opening(hours, now)
    return f"Hami ahile banda chau, {when} dekhi order lina sakchau." if when else "Hami ahile banda chau, order lina sakdainau."


def closed_note(message: str) -> str:
    """The note for the model while closed (sent like photo_hint, so the cached prompt stays the same)."""
    return ("Note from the business, not the customer: the business is CLOSED right now. Answer questions normally, "
            f"but do not take an order or ask for order details. If they want to order, tell them: \"{message}\"")
