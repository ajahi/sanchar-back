"""Delivery or pick up, as the customer already said it (services/kitchen.fulfilment, fulfilment_hint,
drop_fulfilment_question): the bot is told, and "Delivery ki pickup?" is taken out of a reply that asks anyway.
The chats are the real ones from The Cube Cafe (Nov 12). The preview part runs against Postgres, rolled back.

    python -m pytest tests/test_fulfilment.py
"""
import asyncio

from app.db.session import async_session_factory
from app.models.knowledge import KnowledgeDocument
from app.models.order_integration import OrderIntegration
from app.models.tenant import Tenant
from app.services import kitchen, reply_pipeline

AREAS = [{"address": "Chipledhunga", "rate": 80}, {"address": "Bagale Tole", "rate": 100}]


def said(*texts: str):
    return kitchen.fulfilment(list(texts), AREAS)


def test_delivery_as_customers_say_it():
    for text in (
        "Expert ma k 2nd floor ma 2 ota dry mix lyaidinu na",
        "Chicken steam momo mathi munal store ma lyaidena milxa?",
        "1 thali office ma lyaunu paryo",
        "2 chicken momo Kupondole ma pathaidinu",
        "can you deliver 1 veg thali to my office?",
        "ghar samma aaucha?",
        "2 buff momo, Chipledhunga",  # one of our delivery areas
    ):
        assert said(text)[0] == "delivery", text


def test_pickup_as_customers_say_it():
    for text in ("2 chicken momo, 7 baje aaera lanchu", "pickup garchu", "ma aafai lina aauchu", "take away please"):
        assert said(text)[0] == "pickup", text


def test_not_said_and_changed_mind():
    for text in ("momo kati ho?", "menu send garnus", "hajur ko number dinus ta", "chicken"):
        assert said(text) is None, text
    assert said("2 momo Chipledhunga ma pathaunu", "na, ma aafai lina aauchu")[0] == "pickup"
    assert said("pickup garchu", "aba ghar ma pathaidinus")[0] == "delivery"
    assert said("Expert ma 2 ota dry mix lyaidinu na", "chicken")[0] == "delivery"  # said earlier, still holds


def test_the_question_is_taken_out():
    real = ("Chicken Dry Mix - Rs 190. Delivery ki pickup? If delivery, please tell which area (e.g., chipledhunga, "
            "bagale tole, etc.) and your name & phone number. If pickup, just confirm.")
    out = kitchen.drop_fulfilment_question(real)
    assert "ki pickup" not in out and "If pickup" not in out and "If delivery" not in out
    assert out.startswith("Chicken Dry Mix - Rs 190.") and "Please tell which area" in out and "phone number" in out
    first = ("Dry Mix Combo ko kun type chahanu huncha? Veg Dry Mix (Rs 160), Chicken Dry Mix (Rs 190) or Buff Dry "
             "Mix (Rs 190) - kunai ek ya sabai? Delivery ki pickup?")
    out = kitchen.drop_fulfilment_question(first)
    assert out.endswith("kunai ek ya sabai?") and "pickup" not in out
    for fine in ("Buff Jhol Momo Rs 250.", "Tapai ko order:\nPick up (cafe bata)\nTotal: Rs 500",
                 "Hamro phone number: 061-417004."):
        assert kitchen.drop_fulfilment_question(fine) == fine


def test_hint_says_which():
    assert "DELIVERY" in kitchen.fulfilment_hint(said("Expert ma 2nd floor ma lyaidinu na"))
    assert "PICK UP" in kitchen.fulfilment_hint(said("aaera lanchu"))
    assert kitchen.fulfilment_hint(None) == ""


async def _preview(monkeypatch) -> None:
    seen: dict = {}

    async def fake_generate(system, history, text, hint=""):  # what the real model answered on Nov 12
        seen["hint"] = hint
        return ("Chicken Dry Mix - Rs 190. Delivery ki pickup? If delivery, please tell which area and your name & "
                "phone number. If pickup, just confirm.")

    monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)
    async with async_session_factory() as db:
        try:
            tenant = Tenant(name="Cube Cafe")
            db.add(tenant)
            await db.flush()
            db.add(KnowledgeDocument(tenant_id=tenant.id, title="Menu", content="Chicken Dry Mix - Rs 190"))
            db.add(OrderIntegration(tenant_id=tenant.id, base_url="https://site.test/api/v1", send_enabled=False,
                                    auto_send=False, catalog=[], locations=AREAS))
            await db.flush()
            history = [("customer", "Expert ma k 2nd floor ma 2 ota dry mix lyaidinu na"),
                       ("assistant", "Dry Mix ko kun type chahanu huncha?")]
            out = await reply_pipeline.preview_reply(db, tenant, "chicken", history)
            assert "DELIVERY" in seen["hint"] and "Expert ma k 2nd floor" in seen["hint"]
            assert "pickup" not in out.lower() and out.startswith("Chicken Dry Mix - Rs 190.")
        finally:
            await db.rollback()


def test_try_it_never_asks_again(monkeypatch):
    asyncio.run(_preview(monkeypatch))
