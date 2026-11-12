"""Menu words however customers spell them (services/kitchen.item_hint, spellings): the model gets every menu item
a word points to, by the menu's own names, and never sees the owner's {spellings}. The menu is The Cube Cafe's
(tests/fixtures/cube_menu.csv). The preview part runs against Postgres, rolled back.

    python -m pytest tests/test_menu_spelling.py
"""
import asyncio
import csv
from pathlib import Path

from app.db.session import async_session_factory
from app.models.knowledge import KnowledgeDocument
from app.models.order_integration import OrderIntegration
from app.models.tenant import Tenant
from app.services import kitchen, reply_pipeline

with open(Path(__file__).parent / "fixtures" / "cube_menu.csv", encoding="utf-8") as f:
    MENU = [{"_id": f"p{i}", "name": r["name"], "price": int(r["price"]), "category": r["category"]}
            for i, r in enumerate(csv.DictReader(f))]
WRAP, RICE = "Chicken Shawarma (Rs 350, Wrap)", "Shawarma Chicken Rice (Rs 450, Rice)"


def hint(text: str, spelled: dict | None = None) -> str:
    return kitchen.item_hint(text, MENU, spelled)


def test_sound():
    for w in ("shawarma", "sworma", "sorma", "suorma", "sourma", "shwarma"):
        assert kitchen.sound(w) == "srm", w
    assert kitchen.sound("chaumin") == kitchen.sound("chowmein") == "cmn"
    assert kitchen.sound("lafing") == kitchen.sound("laphing")


def test_every_shawarma_is_named_however_spelled():
    for w in ("sorma", "sworma", "suorma", "sourma", "shwarma", "shawarma"):
        h = hint(f"{w} painxa?")
        assert WRAP in h and RICE in h and "these 2 menu items" in h, w
        assert "Soup" not in h and "Rs 280" not in h, w  # not shorba


def test_their_other_words_narrow_it():
    h = hint("2 ota sorma rice")
    assert RICE in h and WRAP not in h
    h = hint("sworma wrap lyaidinu na")
    assert WRAP in h and RICE not in h


def test_a_word_in_many_items_names_the_menu_word_only():
    h = hint("1 plate chaumin")
    assert "'chaumin' is what our menu writes 'Chowmein'" in h and "Rs 180" not in h
    assert hint("lafing chahiyo").count("Laphing") >= 5  # 6 laphing items: all listed


def test_a_word_written_right_in_a_few_names_lists_them():
    h = hint("2 ota dry mix lyaidinu na")
    assert all(x in h for x in ("Buff Dry Mix (Rs 190", "Chicken Dry Mix (Rs 190", "Veg Dry Mix (Rs 160"))
    assert hint("2 ota chicken dry mix") == ""  # one item, named in full: the menu text is enough


def test_ordinary_chat_gets_no_note():
    for text in ("hello", "namaste", "order garnu milcha?", "momo kati ho?", "spicy kam garnus", "delivery painxa?",
                 "aaja khulla cha?", "thik xa dhanyabad", "Expert ma 2nd floor ma lyaidinu na", "phone ta uthyna ta"):
        assert hint(text) == "", text


def test_owner_spellings_are_used_and_hidden():
    info = ("Spellings customers use\nSadheko {sandheko, sadeko}\nWrap: Tuna Wrap - Rs 400; Chicken Shawarma {suwarma} - Rs 350"
            "\nVanilla Ice cream (2 scoop) - Rs 120")
    spelled = kitchen.spellings(info)
    assert spelled == {"sandheko": "Sadheko", "sadeko": "Sadheko", "suwarma": "Chicken Shawarma"}
    shown = kitchen.strip_spellings(info)
    assert "{" not in shown and "sandheko" not in shown and "(2 scoop)" in shown  # round brackets are kept
    h = hint("chicken sandheko 1 plate", spelled)
    assert "Chicken Sadheko (Rs 380" in h and "Chicken Sadheko Momo (Rs 230" in h and "Buff" not in h
    assert WRAP in hint("suwarma", spelled) and RICE in hint("suwarma", spelled)


async def _preview(monkeypatch) -> None:
    seen: dict = {}

    async def fake_generate(system, history, text, hint=""):
        seen["system"], seen["hint"] = system, hint
        return "Chicken Shawarma Rs 350 ra Shawarma Chicken Rice Rs 450 cha. Kun chahiyo?"

    monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)
    async with async_session_factory() as db:
        try:
            tenant = Tenant(name="Cube Cafe")
            db.add(tenant)
            await db.flush()
            db.add(KnowledgeDocument(tenant_id=tenant.id, title="Spellings", content="Chicken Shawarma {sorma, sworma}"))
            db.add(OrderIntegration(tenant_id=tenant.id, base_url="https://site.test/api/v1", catalog=MENU, locations=[]))
            await db.flush()
            out = await reply_pipeline.preview_reply(db, tenant, "sorma painxa?")
            assert out.startswith("Chicken Shawarma Rs 350")
            assert "sorma" not in seen["system"] and "{" not in seen["system"]  # the model never sees the spellings
            assert WRAP in seen["hint"] and RICE in seen["hint"]
        finally:
            await db.rollback()


def test_try_it_gets_the_note_and_never_the_spellings(monkeypatch):
    asyncio.run(_preview(monkeypatch))
