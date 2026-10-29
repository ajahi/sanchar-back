"""Opening hours (services/hours.py): open/closed at fixed Nepal times, and the hours read out of Shop Info once
(the model faked).

    python -m pytest tests/test_hours.py
"""
import asyncio
from datetime import datetime

from app.models.tenant import Tenant
from app.services import hours
from app.services.hours import NEPAL

CAFE = {d: ["10:00", "21:00"] for d in hours.DAYS} | {"sat": None}  # closed on Saturday
LATE = {"fri": ["18:00", "02:00"]}  # Friday night past midnight, closed the rest of the week


def at(day: int, hhmm: str) -> datetime:  # 2026-11-02 is a Monday; day 0 = Monday
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 2 + day, h, m, tzinfo=NEPAL)


def test_open_and_closed():
    assert hours.is_open(None) and hours.is_open({}) and hours.closed_message(None) is None  # no hours: always open
    assert hours.is_open(CAFE, at(0, "10:00")) and hours.is_open(CAFE, at(0, "20:59"))
    assert not hours.is_open(CAFE, at(0, "09:59")) and not hours.is_open(CAFE, at(0, "21:00"))
    assert not hours.is_open(CAFE, at(5, "12:00"))  # Saturday
    assert hours.is_open(LATE, at(4, "23:30")) and hours.is_open(LATE, at(5, "01:59"))  # Friday night into Saturday
    assert not hours.is_open(LATE, at(5, "02:00")) and not hours.is_open(LATE, at(4, "17:00"))


def test_when_it_opens_next():
    assert hours.closed_message(CAFE, now=at(0, "08:00")) == "Hami ahile banda chau, aaja bihana 10 baje dekhi order lina sakchau."
    assert hours.closed_message(CAFE, now=at(0, "22:00")) == "Hami ahile banda chau, bholi bihana 10 baje dekhi order lina sakchau."
    assert hours.next_opening(CAFE, at(4, "22:00")) == "Aaitabar bihana 10 baje"  # Friday night; Saturday closed
    assert hours.next_opening({"mon": ["14:30", "20:00"]}, at(0, "09:00")) == "aaja diuso 2:30 baje"
    assert hours.closed_message(CAFE, "Office {opens} khulcha, booking garna milcha.", at(0, "22:00")) == (
        "Office bholi bihana 10 baje khulcha, booking garna milcha.")  # each funnel's own message
    assert hours.closed_message({"mon": None}, now=at(0, "09:00")) == "Hami ahile banda chau."


def test_the_models_table_is_checked():
    assert hours.parse('Here: {"mon": ["10:00", "21:00"], "sat": null}') == {"mon": ["10:00", "21:00"], "sat": None}
    assert hours.parse("{}") == {}  # no hours in Shop Info
    for bad in ('{"monday": ["10:00", "21:00"]}', '{"mon": ["10:00"]}', '{"mon": ["25:00", "21:00"]}', '{"mon": "10-21"}',
                '{"mon": ["10am", "9pm"]}', "I could not find any hours.", '{"mon": '):
        assert hours.parse(bad) is None, bad


def test_hours_are_read_once_per_shop_info(monkeypatch):
    calls: list[str] = []

    async def fake_chat(messages):
        calls.append(messages[-1]["content"])
        return '{"mon": ["10:00", "21:00"], "tue": ["10:00", "21:00"]}' if "10am" in calls[-1] else "{}"

    monkeypatch.setattr(hours.auto_reply, "chat", fake_chat)

    async def go():
        t = Tenant(name="Cafe")
        assert await hours.for_tenant(None, t, "Momo Rs 150. Open 10am to 9pm Mon-Tue.") == {"mon": ["10:00", "21:00"], "tue": ["10:00", "21:00"]}
        assert await hours.for_tenant(None, t, "Momo Rs 150. Open 10am to 9pm Mon-Tue.") is not None and len(calls) == 1  # kept
        assert await hours.for_tenant(None, t, "Momo Rs 160. No hours here.") is None and len(calls) == 2  # Shop Info changed

        async def down(messages):
            raise RuntimeError("model down")

        monkeypatch.setattr(hours.auto_reply, "chat", down)
        assert await hours.for_tenant(None, t, "Changed again") is None  # open; tried again next message

    asyncio.run(go())
