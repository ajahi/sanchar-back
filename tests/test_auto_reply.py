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
