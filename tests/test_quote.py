"""The calculation (services/kitchen.quote, quote_order) on The Cube Cafe's menu: what every summary total and
every sent order uses.

    python -m pytest tests/test_quote.py
"""
import csv
from pathlib import Path
from types import SimpleNamespace

from app.services import kitchen

with open(Path(__file__).parent / "fixtures" / "cube_menu.csv", encoding="utf-8") as f:
    MENU = [{"_id": f"p{i}", "name": r["name"], "price": int(r["price"]), "category": r["category"]}
            for i, r in enumerate(csv.DictReader(f))]
AREAS = [{"address": "Chipledhunga", "rate": 80}, {"address": "Bagale Tole", "rate": 100}]
INTEG = SimpleNamespace(catalog=MENU, locations=AREAS)
BY_NAME = {p["name"]: p for p in MENU}


def test_quote_adds_up_items_and_delivery():
    q = kitchen.quote([(BY_NAME["Chicken Dry Mix"], 2), (BY_NAME["Coke 250ml"], 1)], AREAS[0])
    assert [(i["name"], i["quantity"], i["price"], i["amount"]) for i in q["items"]] == [
        ("Chicken Dry Mix", 2, 190, 380), ("Coke 250ml", 1, 100, 100)]
    assert (q["subtotal"], q["delivery"]["charge"], q["total"]) == (480, 80, 560)
    assert q["delivery"] == {"type": "delivery", "area": "Chipledhunga", "charge": 80, "listed": True}
    pickup = kitchen.quote([(BY_NAME["Chicken Dry Mix"], 2)], None)
    assert pickup["total"] == 380 and pickup["delivery"]["type"] == "pickup"
    unlisted = kitchen.quote([(BY_NAME["Veg Dry Mix"], 1)], {"address": "Bhaktapur", "rate": 0, "unlisted": True})
    assert unlisted["total"] == 160 and unlisted["delivery"]["listed"] is False  # staff call about the charge


def test_quote_order_from_what_the_customer_wrote():
    q = kitchen.quote_order({"items": "2 x Chicken Dry Mix, 1 Coke 250ml, 1 x Sowrma", "address": "Bagale Tole, near the temple"}, INTEG)
    assert (q["subtotal"], q["delivery"]["charge"], q["total"]) == (480, 100, 580)
    assert q["unmatched"] == ["1 x Sowrma"]  # left out of the total, never guessed
    assert kitchen.quote_order({"items": "1 Buff Jhol Momo", "address": "Pick up"}, INTEG)["total"] == 250


def test_the_website_order_uses_the_same_total():
    lines, loc = [(BY_NAME["Chicken Dry Mix"], 2)], AREAS[1]
    body = kitchen.build_body({"name": "Ram", "phone": "9812345678", "address": "Bagale Tole"}, lines, loc, None)
    assert body["totalAmount"] == kitchen.quote(lines, loc)["total"] == 480
