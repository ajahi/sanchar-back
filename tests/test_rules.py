"""Behaviour rules from a funnel's .md file (services/rules.py, funnel_md.py): detection, the reply fixes, the
file round trip, checks on save, and that only funnels with a rule get it (a consultancy is never told "delivery").
The chats are the real ones from The Cube Cafe (Nov 12). DB parts run against Postgres.

    python -m pytest tests/test_rules.py
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.admin import FunnelFile, FunnelIn, funnel_from_md, funnel_to_md, save_funnel
from app.db.session import async_session_factory
from app.models.knowledge import KnowledgeDocument
from app.models.order_integration import OrderIntegration
from app.models.tenant import Tenant
from app.models.user import User
from app.services import funnel_md, reply_pipeline, rules

AREAS = [{"address": "Chipledhunga", "rate": 80}, {"address": "Bagale Tole", "rate": 100}]
RESTAURANT_MD = (Path(__file__).parents[1] / "funnels" / "restaurant.md").read_text(encoding="utf-8")
RESTAURANT = SimpleNamespace(**funnel_md.from_md(RESTAURANT_MD))
RULE = {r["when"]: r for r in rules.of(RESTAURANT)}
CONSULTANCY = SimpleNamespace(key="consultancy", label="enquiry", checks=[])  # no delivery rules at all


def ctx(*texts: str, **kw) -> rules.Context:
    return rules.Context(texts=list(texts), areas=AREAS, **kw)


def said(*texts: str):
    return rules.fulfilment(RULE["customer_said_fulfilment"], ctx(*texts))


def test_delivery_and_pickup_as_customers_say_it():
    for text in ("Expert ma k 2nd floor ma 2 ota dry mix lyaidinu na", "Chicken steam momo mathi munal store ma lyaidena milxa?",
                 "1 thali office ma lyaunu paryo", "2 chicken momo Kupondole ma pathaidinu",
                 "can you deliver 1 veg thali to my office?", "ghar samma aaucha?", "2 buff momo, Chipledhunga"):
        assert said(text)[0] == "delivery", text
    for text in ("2 chicken momo, 7 baje aaera lanchu", "pickup garchu", "ma aafai lina aauchu", "take away please"):
        assert said(text)[0] == "pickup", text
    for text in ("momo kati ho?", "menu send garnus", "hajur ko number dinus ta", "chicken"):
        assert said(text) is None, text
    assert said("2 momo Chipledhunga ma pathaunu", "na, ma aafai lina aauchu")[0] == "pickup"  # changed their mind
    assert said("Expert ma 2 ota dry mix lyaidinu na", "chicken")[0] == "delivery"  # said earlier, still holds


def test_a_place_named():
    place = lambda *t: rules.place(RULE["customer_gave_place"], ctx(*t))  # noqa: E731
    assert place("Expert ma k 2nd floor ma 2 ota dry mix lyaidinu na")
    assert place("Chicken steam momo mathi munal store ma lyaidena milxa?")
    assert place("2 buff momo, Chipledhunga")  # one of our areas
    assert place("can you deliver it to Baneshwor?")
    assert not place("2 momo lyaidinu na") and not place("ghar ma pathaidinus") and not place("I want to get it delivered")
    assert not place("2 momo Kupondole ma pathaunu", "na, ma aafai lina aauchu")  # pick up after all


def test_reply_fixes():
    real = ("Chicken Dry Mix - Rs 190. Delivery ki pickup? If delivery, please tell which area and your name & phone "
            "number. If pickup, just confirm.")
    out = rules.drop_fulfilment_question(real)
    assert out == "Chicken Dry Mix - Rs 190. Please tell which area and your name & phone number."
    assert rules.drop_address_question("Kun area ma parcha? Tapai ko address pathaunus.") == "Kun area ma parcha?"
    assert rules.drop_address_question("Tapai ko address ra phone number dinus.") == "Tapai ko phone number dinus."
    for fine in ("Buff Jhol Momo Rs 250.", "Tapai ko order:\nPick up (cafe bata)\nTotal: Rs 500", "Hamro phone number: 061-417004."):
        assert rules.drop_fulfilment_question(fine) == fine and rules.drop_address_question(fine) == fine
    closed = rules.run(RESTAURANT, ctx("2 momo", closed="Aaja banda chau."))
    assert rules.finish("Delivery ki pickup?", closed, "Aaja banda chau.") == "Aaja banda chau."
    assert rules.finish("Momo Rs 180.", closed, "Aaja banda chau.") == "Momo Rs 180."


def test_only_funnels_with_the_rule_get_it():
    out = rules.run(RESTAURANT, ctx("Expert ma k 2nd floor ma 2 ota dry mix lyaidinu na", "chicken"))
    assert any("DELIVERY" in n for n in out.notes) and any("their address" in n for n in out.notes)
    assert out.remove == {"fulfilment_question", "address_question"}
    visa = rules.run(CONSULTANCY, ctx("documents pathaidinu", "Australia ko visa ko lagi"))
    assert visa.notes == [] and visa.remove == set()
    assert rules.finish("Delivery ki pickup?", visa, None) == "Delivery ki pickup?"  # nothing to fix there


def test_the_file_round_trips_and_its_examples_pass():
    draft = funnel_md.from_md(RESTAURANT_MD)
    assert funnel_md.from_md(funnel_md.to_md(draft)) == draft
    assert [e for r in rules.of(RESTAURANT) for e in rules.problems(r)] == []
    assert "{label}" in draft["steps"] and draft["key"] == "restaurant"
    assert "delivery_words: [deliver, bring," in RESTAURANT_MD  # one line per word list: readable diffs


def test_wrong_rules_are_explained():
    assert "must be one of" in rules.problems({"name": "X", "when": "always"})[0]
    assert "remove_from_reply" in rules.problems({"name": "X", "when": "customer_said_fulfilment",
                                                  "remove_from_reply": ["everything"]})[0]
    assert "only works with when: shop_closed" in rules.problems({"name": "X", "when": "customer_gave_place",
                                                                  "replace_reply_if_it_asks": ["order_details"]})[0]
    bad = {"name": "Pickup words", "when": "customer_said_fulfilment",
           "examples": [{"customer": "parcel pathaidinus", "expect": {"fulfilment": "pickup"}}]}
    assert rules.problems(bad) == ['Pickup words: "parcel pathaidinus" should be pickup, the words give delivery.']
    with pytest.raises(funnel_md.FunnelFileError):
        funnel_md.from_md("key: x\nno header")


async def _admin() -> None:
    draft = await funnel_from_md(FunnelFile(markdown=RESTAURANT_MD))
    assert draft["key"] == "restaurant" and any(c["type"] == "rule" for c in draft["checks"])
    back = await funnel_to_md(FunnelIn.model_validate(draft))
    assert funnel_md.from_md(back["markdown"]) == draft
    broken = RESTAURANT_MD.replace('expect: {fulfilment: none}', 'expect: {fulfilment: delivery}')
    with pytest.raises(HTTPException) as e:
        await funnel_from_md(FunnelFile(markdown=broken))
    assert e.value.status_code == 422 and "menu send garnus" in e.value.detail
    with pytest.raises(HTTPException) as e:
        await funnel_from_md(FunnelFile(markdown="---\nkey: [unclosed\n---\nsteps"))
    assert e.value.status_code == 422 and "YAML" in e.value.detail
    async with async_session_factory() as db:
        db.commit = db.flush
        try:
            admin = User(name="A", email="a@rules.test")
            db.add(admin)
            await db.flush()
            bad = {**draft, "checks": [*draft["checks"], {"type": "rule", "name": "Oops", "when": "sometimes"}]}
            with pytest.raises(HTTPException) as e:
                await save_funnel(FunnelIn.model_validate(bad), admin, db)
            assert e.value.status_code == 422 and "Oops" in e.value.detail
            saved = await save_funnel(FunnelIn.model_validate(draft), admin, db)
            assert saved["checks"] == draft["checks"] and saved["active"]
        finally:
            await db.rollback()


def test_admin_import_export_and_save(monkeypatch):
    asyncio.run(_admin())


async def _preview(monkeypatch, funnel) -> tuple[str, str]:
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
            return await reply_pipeline.preview_reply(db, tenant, "chicken", history, funnel), seen["hint"]
        finally:
            await db.rollback()


def test_try_it_follows_the_funnels_rules(monkeypatch):
    only_rules = SimpleNamespace(**{**vars(RESTAURANT), "checks": rules.of(RESTAURANT)})  # no hours to read

    async def both():
        return await _preview(monkeypatch, only_rules), await _preview(monkeypatch, SimpleNamespace(
            **{**vars(RESTAURANT), "checks": []}))

    (out, hint), (bare, bare_hint) = asyncio.run(both())
    assert "DELIVERY" in hint and "Expert ma k 2nd floor" in hint
    assert out == "Chicken Dry Mix - Rs 190. Please tell which area and your name & phone number."
    assert "DELIVERY" not in bare_hint and "Delivery ki pickup?" in bare  # no rule in the file: nothing enforced
