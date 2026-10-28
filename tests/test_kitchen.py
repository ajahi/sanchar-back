"""Sending confirmed chat orders into a business's own order system (services/kitchen.py). The site is faked
(httpx.MockTransport): nothing reaches a real kitchen. DB parts run in a rolled-back transaction.

    python -m pytest tests/test_kitchen.py
"""
import asyncio
import json
import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.customer import Customer
from app.models.knowledge import KnowledgeDocument
from app.models.order_integration import OrderIntegration
from app.models.sales_lead import SalesLead
from app.models.tenant import Tenant
from app.services import kitchen

# Real names and prices from thecubeventure.com's menu (a slice).
MENU = [
    {"_id": "m1", "name": "Buff Jhol Momo", "price": 250, "category": "mo:mo"},
    {"_id": "m2", "name": "Veg Sadheko Momo", "price": 180, "category": "mo:mo"},
    {"_id": "m3", "name": "Chicken Steam Momo", "price": 180, "category": "mo:mo"},
    {"_id": "m4", "name": "Chicken Fried Momo", "price": 200, "category": "mo:mo"},
    {"_id": "m5", "name": "Pizza Momo- Veg", "price": 290, "category": "mo:mo"},
    {"_id": "m6", "name": "Chicken & Egg Rice", "price": 450, "category": "rice"},
    {"_id": "m7", "name": "Coke ice/lemon", "price": 150, "category": "other cold drinks"},
    {"_id": "m8", "name": "Coke 250ml", "price": 100, "category": "other cold drinks"},
    {"_id": "m9", "name": "Ramyun 2x Spicy", "price": 390, "category": "noodles"},
]
AREAS = [{"address": "alumaila chowk", "rate": 80}, {"address": "bagale tole", "rate": 50}, {"address": "bagar", "rate": 90}]


def test_lines_match_the_menu_or_are_left_for_staff():
    m = lambda t: (lambda item, q: (item and item["name"], q))(*kitchen.match_line(t, MENU))  # noqa: E731
    assert m("2 Buff Jhol Momo") == ("Buff Jhol Momo", 2)
    assert m("Buff Jhol momo - 2 plate Rs 250 each") == ("Buff Jhol Momo", 2)  # price and unit ignored
    assert m("duita veg sadeko momo") == ("Veg Sadheko Momo", 2)  # Nepali count, near spelling
    assert m("Veg Pizza Momo") == ("Pizza Momo- Veg", 1)  # any word order
    assert m("1 Chicken & Egg Rice") == ("Chicken & Egg Rice", 1)
    assert m("Buff Jhol Momo x2") == ("Buff Jhol Momo", 2)  # how the bot writes it
    assert m("2x Buff Jhol Momo") == m("Buff Jhol Momo ×2") == ("Buff Jhol Momo", 2)
    assert m("1 Ramyun 2x Spicy") == ("Ramyun 2x Spicy", 1)  # a "2x" inside a name is not a count
    assert m("Coke 250ml x3") == ("Coke 250ml", 3)
    assert m("Coke x1")[0] is None  # 250ml or ice/lemon? asked, not guessed
    assert m("chicken momo")[0] is None  # steam or fried? never guessed into the kitchen
    assert m("2 plate")[0] is None
    plan = kitchen.plan({"items": "2 Buff Jhol Momo, 1 Coke ice/lemon and 1 chicken momo", "address": "Alumaila chowk, near the pump"},
                        OrderIntegration(catalog=MENU, locations=AREAS))
    assert [(line["product_id"], line["quantity"]) for line in plan["lines"]] == [("m1", 2), ("m7", 1), (None, 1)]
    assert plan["location"] == "alumaila chowk"


def test_delivery_area_from_the_address():
    loc = lambda a: (kitchen.match_location(a, AREAS) or {}).get("address")  # noqa: E731
    assert loc("Ranipauwa petrol pump pachade, Alumaila Chowk") == "alumaila chowk"
    assert loc("alumila chowk") == "alumaila chowk"  # near spelling
    assert loc("bagale tole") == "bagale tole" and loc("Bagar") == "bagar"  # longest exact wins, whole words only
    assert loc("pick up garchu") == kitchen.PICKUP
    assert loc("Baneshwor, Kathmandu") is None  # not a delivery area: staff pick


def test_body_is_what_the_website_posts():
    order = {"name": "Ram", "phone": "+9779812345678", "address": "bagale tole", "items": "2 Buff Jhol Momo"}
    body = kitchen.build_body(order, [(MENU[0], 2), (MENU[6], 1)], AREAS[1], "cafe@x.np")
    assert body["data"] == {"fullName": "Ram", "address": "bagale tole", "phone": "9812345678", "email": "cafe@x.np",
                            "deliveryLocation": "bagale tole"}
    assert body["product"] == ["m1", "m7"] and body["item"][0]["quantity"] == 2
    assert (body["deliveryOption"], body["deliveryCharge"], body["totalAmount"]) == ("bagale tole", 50, 2 * 250 + 150 + 50)
    pickup = kitchen.build_body(order, [(MENU[0], 1)], {"address": kitchen.PICKUP, "rate": 0}, None)
    assert (pickup["deliveryOption"], pickup["data"]["deliveryLocation"], pickup["totalAmount"]) == ("Pick up", "Pick-up", 250)
    assert body["item"][0]["requests"] is None  # no request: as before
    noted = kitchen.build_body({**order, "requests": "hold the chillies"}, [(MENU[0], 2), (MENU[6], 1)], AREAS[1], None)
    assert [i["requests"] for i in noted["item"]] == ["hold the chillies"] * 2  # the site's per-item box, every line


def test_only_listed_hosts():
    assert kitchen.allowed_url("https://admin.thecubeventure.com/api/v1")
    for url in ("http://admin.thecubeventure.com/api/v1", "https://evil.example/api", "https://localhost/api",
                "https://admin.thecubeventure.com.evil.example/api"):
        assert not kitchen.allowed_url(url), url


def test_sync_then_auto_send(monkeypatch):
    """Sync fills the menu and the Shop Info topic; a confirmed order goes out once, only when sending and
    auto-send are on and everything matched; switched off, nothing is posted."""
    posted: list[dict] = []

    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/product/getall"):
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"data": [{"_id": "mo:mo", "products": MENU}] if page == 1 else []})
        if request.url.path.endswith("/product/getadd"):
            return httpx.Response(200, json={"data": AREAS})
        if request.url.path.endswith("/order/placeorder"):
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "data": {"_id": "site-1", "intradeOrderId": 7400}})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(kitchen.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(site), **kw))
    from app.api.v1 import kitchen as api
    from app.db.session import async_session_factory
    from app.schemas.kitchen import Line, SendIn

    async def go() -> None:
        async with async_session_factory() as db:
            db.commit = db.flush  # stay inside the rolled-back transaction
            t = Tenant(name=f"Cafe {uuid.uuid4().hex[:6]}")
            db.add(t)
            await db.flush()
            cust = Customer(tenant_id=t.id, external_user_id=f"c-{t.id}")
            db.add(cust)
            await db.flush()
            integ = OrderIntegration(tenant_id=t.id, base_url="https://admin.thecubeventure.com/api/v1", email="cafe@x.np",
                                     catalog=[], locations=[])
            db.add(integ)
            await db.flush()
            await kitchen.sync(db, integ)
            assert len(integ.catalog) == len(MENU) and integ.locations == AREAS
            topic = await db.get(KnowledgeDocument, integ.knowledge_id)
            assert "Buff Jhol Momo - Rs 250" in topic.content and "alumaila chowk Rs 80" in topic.content

            order = {"name": "Ram", "phone": "+9779812345678", "address": "Alumaila chowk", "items": "2 Buff Jhol Momo"}
            lead = SalesLead(tenant_id=t.id, customer_id=cust.id, channel="instagram", phone=order["phone"], meta={"order": order})
            db.add(lead)
            await db.flush()

            await kitchen.auto_send(db, lead)  # sending is off by default
            assert posted == []
            with pytest.raises(HTTPException) as off:  # staff pressing send while it is off: nothing goes out
                await api.send_order(lead.id, SendIn(lines=[Line(product_id="m1", quantity=2)], location="alumaila chowk"), t, db)
            assert off.value.status_code == 409 and posted == []

            integ.send_enabled = True
            await kitchen.auto_send(db, lead)  # on, but auto-send is not: waits for staff
            assert posted == []
            integ.auto_send = True
            await kitchen.auto_send(db, lead)
            await kitchen.auto_send(db, lead)  # the same order never twice
            assert len(posted) == 1 and posted[0]["totalAmount"] == 2 * 250 + 80
            assert (lead.meta["kitchen"]["kitchen_order_id"], kitchen.already_sent(lead)) == (7400, True)

            # a changed order with a line nobody can match waits for staff, who pick the item and send it
            lead.meta = {"order": {**order, "items": "1 chicken momo"}, "kitchen": lead.meta["kitchen"]}
            await kitchen.auto_send(db, lead)
            assert len(posted) == 1
            plan = await api.order_plan(lead.id, t, db)
            assert plan.lines[0].product_id is None and plan.body is None and plan.sent is None
            await api.send_order(lead.id, SendIn(lines=[Line(product_id="m3", quantity=1)], location="alumaila chowk"), t, db)
            assert len(posted) == 2 and posted[1]["item"][0]["name"] == "Chicken Steam Momo"
            await db.rollback()

    asyncio.run(go())
