"""Opening hours (services/hours.py) at fixed Nepal times.

    python -m pytest tests/test_hours.py
"""
from datetime import datetime

from app.services import hours
from app.services.hours import NEPAL

CAFE = {d: ["10:00", "21:00"] for d in hours.DAYS} | {"sat": None}  # closed on Saturday
LATE = {"fri": ["18:00", "02:00"]}  # Friday night past midnight, closed the rest of the week


def at(day: int, hhmm: str) -> datetime:  # 2026-11-02 is a Monday; day 0 = Monday
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 2 + day, h, m, tzinfo=NEPAL)


def test_open_and_closed():
    assert hours.is_open(None) and hours.closed_message(None) is None  # no hours set: always open
    assert hours.is_open(CAFE, at(0, "10:00")) and hours.is_open(CAFE, at(0, "20:59"))
    assert not hours.is_open(CAFE, at(0, "09:59")) and not hours.is_open(CAFE, at(0, "21:00"))
    assert not hours.is_open(CAFE, at(5, "12:00"))  # Saturday
    assert hours.is_open(LATE, at(4, "23:30")) and hours.is_open(LATE, at(5, "01:59"))  # Friday night into Saturday
    assert not hours.is_open(LATE, at(5, "02:00")) and not hours.is_open(LATE, at(4, "17:00"))


def test_when_it_opens_next():
    assert hours.closed_message(CAFE, at(0, "08:00")) == "Hami ahile banda chau, aaja bihana 10 baje dekhi order lina sakchau."
    assert hours.closed_message(CAFE, at(0, "22:00")) == "Hami ahile banda chau, bholi bihana 10 baje dekhi order lina sakchau."
    assert hours.next_opening(CAFE, at(4, "22:00")) == "Aaitabar bihana 10 baje"  # Friday night; Saturday closed
    assert hours.next_opening({"mon": ["14:30", "20:00"]}, at(0, "09:00")) == "aaja diuso 2:30 baje"
    assert hours.closed_message({}, at(0, "09:00")) is None  # {} = no hours set, open
    assert hours.closed_message({"mon": None}, at(0, "09:00")) == "Hami ahile banda chau, order lina sakdainau."


def test_hours_format_is_checked():
    assert hours.valid(CAFE) and hours.valid({}) and hours.valid(LATE)
    for bad in ({"monday": ["10:00", "21:00"]}, {"mon": ["10:00"]}, {"mon": ["25:00", "21:00"]}, {"mon": "10-21"},
                {"mon": ["10am", "9pm"]}):
        assert not hours.valid(bad), bad
