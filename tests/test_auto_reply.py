"""Prompt + language-drift retry, with the model call faked (no network needed).

    python -m pytest tests/test_auto_reply.py
"""
import asyncio

from app.services import auto_reply


def test_prompt_carries_shop_context():
    p = auto_reply.build_system_prompt("  PRODUCT: X Rs 10  ")
    assert "PRODUCT: X Rs 10" in p and "SHOP INFORMATION" in p


def test_long_reply_goes_as_several_messages():
    menu = "\n".join(f"Item {i} - Rs {i * 10}" for i in range(120))  # ~2000 chars, like a menu listing
    parts = auto_reply.split_long(menu)
    assert len(parts) == 3 and all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == menu  # cut at line breaks: nothing lost, no item split
    assert auto_reply.split_long("short") == ["short"] and auto_reply.split_long("") == []
    assert [len(p) for p in auto_reply.split_long("x" * 2500)] == [1000, 1000, 500]  # no break anywhere


def _run(monkeypatch, user_text: str, answers: list[str]) -> tuple[str, int]:
    calls = []

    async def fake_chat(messages):
        calls.append(messages)
        return answers[len(calls) - 1]

    monkeypatch.setattr(auto_reply, "_chat", fake_chat)
    return asyncio.run(auto_reply.generate_reply("sys", [], user_text)), len(calls)


def test_devanagari_drift_gets_one_rewrite(monkeypatch):
    reply, calls = _run(monkeypatch, "price kati ho?", ["Rs 300 ho. अरु के चाहिन्छ?", "Rs 300 ho. Aru kehi chahiyo?"])
    assert calls == 2 and reply == "Rs 300 ho. Aru kehi chahiyo?"


def test_no_rewrite_when_reply_is_clean_or_customer_wrote_devanagari(monkeypatch):
    assert _run(monkeypatch, "price kati ho?", ["Rs 300 ho."]) == ("Rs 300 ho.", 1)
    assert _run(monkeypatch, "मूल्य कति हो?", ["रु ३०० हो।"]) == ("रु ३०० हो।", 1)


def test_photo_list_in_prompt_and_labels_split_out():
    p = auto_reply.build_system_prompt("Kurtha Rs 1800", photos=[("Red Kurtha", ["red", "tihar"]), (None, ["chura"])])
    assert "P1 Red Kurtha [red, tihar]" in p and "P2 Untitled photo [chura]" in p and "[photo:P1]" in p
    assert "PHOTOS YOU CAN SEND" not in auto_reply.build_system_prompt("Kurtha Rs 1800")  # no photos, no section

    split = auto_reply.split_photos
    assert split("Cha! Rs 1800. [photo:P2]", 2) == ("Cha! Rs 1800.", [1])
    # unknown labels and links can't pick anything; repeats count once; our history marker is never echoed
    assert split("Here [photo:P9] [photo: http://x.y/a.jpg] [Photo : p1] [photo:P1] [sent photo: Red Kurtha]", 2) == ("Here", [0])
    assert split("[photo:P1]", 1) == ("", [0])  # a photo-only answer
    assert split("Hamro chura box (P1) ramro cha. [photo:P1]", 1) == ("Hamro chura box ramro cha.", [0])  # leaked label
    assert split("a [photo:P1] b [photo:P2] c [photo:P3] d [photo:P4]", 4) == ("a b c d", [0, 1, 2])  # at most 3
    assert split("Line one\nLine two [photo:P1]", 1) == ("Line one\nLine two", [0])  # line breaks kept


def test_photo_hint_points_at_tag_matches():
    photos = [("box-refernce", ["two row chura box", "tihar", "bhai-tika gifts"]),
              ("Mountain Bike (Giant, 21-speed)", ["mountain bike", "giant"]),
              ("Blue me away.", [])]
    hint = auto_reply.photo_hint
    assert "P1 box-refernce" in hint("hajur ko dokan ma bhai tika ma kai xa hola ?", photos)  # "bhai-tika gifts"
    assert "P1 box-refernce" in hint("Tihar ko lagi ke cha?", photos)
    assert "P2 Mountain Bike" in hint("mountain bike ko photo dekhau", photos)
    assert hint("electric bike kati ho?", photos) == ""  # "bike" alone is not the tag "mountain bike"
    assert hint("hi", photos) == "" and hint("blue", photos) == ""  # filler and partial names don't match
    assert "Note from the business" in hint("tihar", photos)  # framed as ours, not the customer's words


def test_order_label_split_out():
    assert "[order: name | phone | address | items | special request, or none]" in auto_reply.build_system_prompt("x")
    # a special request rides along; "none" (or no fifth part) means there is none
    _, order = auto_reply.split_order("Dhanyabad! [order: Ram | 9812345678 | Bagar | 2 Buff Jhol Momo | piro kam, chado garnus]")
    assert order["requests"] == "piro kam, chado garnus" and order["items"] == "2 Buff Jhol Momo"
    for none in ("| none]", "| Chaina.]", "| ]", "]"):
        assert "requests" not in auto_reply.split_order(f"[order: Ram | 9812345678 | Bagar | 1 Momo {none}")[1]
    text, order = auto_reply.split_order(
        "Ram ji, 2 Chura Box Baneshwor ma. Hami call garera confirm garchau.\n"
        "[order: Ram Shrestha | 9812345678 | Baneshwor, Kathmandu | 2 x Two Row Chura Box]"
    )
    assert text == "Ram ji, 2 Chura Box Baneshwor ma. Hami call garera confirm garchau."
    assert order == {"name": "Ram Shrestha", "phone": "9812345678", "address": "Baneshwor, Kathmandu",
                     "items": "2 x Two Row Chura Box"}
    # an incomplete label is removed but is not an order; no label leaves the text alone
    assert auto_reply.split_order("Thik cha. [ORDER: Ram | | Baneshwor | box]") == ("Thik cha.", None)
    assert auto_reply.split_order("Naam pathaunus. [order: name | phone | address | 2 box]") == ("Naam pathaunus.", None)
    assert auto_reply.split_order("Hajur [photo:P1]") == ("Hajur [photo:P1]", None)


def test_picked_photo_grows_into_its_set():
    # newest first, as sendable_photos lists them: the menu was uploaded page 1 -> page 3
    photos = [
        ("Menu page 3", ["menu", "drinks"]),
        ("Chura box", ["tihar", "chura"]),
        ("Menu page 2", ["menu", "momo"]),
        ("Menu page 1", ["menu", "breakfast"]),
        ("Lehenga red", ["lehenga", "wedding"]),
        ("Lehenga blue", ["lehenga", "wedding"]),
    ]
    # the customer asked for the menu, the model picked one page: every "menu" photo, in upload order
    assert auto_reply.expand_picks("menu pathaunu na", photos, [2]) == [3, 2, 0]
    # no tag in their words: the photos tagged exactly alike form the set
    assert auto_reply.expand_picks("dekhau na", photos, [4]) == [5, 4]
    # a photo with its own tags stays alone; picks keep their order; never more than MAX_SET_PHOTOS
    assert auto_reply.expand_picks("chura?", photos, [1, 0]) == [1, 0]
    assert auto_reply.expand_picks("chura?", photos, [1]) == [1]
    many = [(f"Menu {i}", ["menu"]) for i in range(15)]
    assert len(auto_reply.expand_picks("menu", many, [0])) == auto_reply.MAX_SET_PHOTOS
    assert auto_reply.sent_photos_note(["Menu", "Menu", "Menu"]) == "[sent 3 photos: Menu]"
    assert auto_reply.sent_photos_note(["Chura box"]) == "[sent photo: Chura box]"
    # the model copying a history note never reaches the customer
    assert auto_reply.split_photos("Yo ho [sent 3 photos: Menu] [photo:P1]", 2) == ("Yo ho", [0])
