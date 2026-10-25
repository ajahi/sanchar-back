"""Prompt + language-drift retry, with the model call faked (no network needed).

    python -m pytest tests/test_auto_reply.py
"""
import asyncio

from app.services import auto_reply


def test_prompt_carries_shop_context():
    p = auto_reply.build_system_prompt("  PRODUCT: X Rs 10  ")
    assert "PRODUCT: X Rs 10" in p and "SHOP INFORMATION" in p


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
