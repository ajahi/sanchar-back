"""Human-request keywords hand over without calling the LLM (no network needed).

    python -m pytest tests/test_auto_reply.py
"""
import asyncio

from app.services.auto_reply import build_system_prompt, generate_reply, wants_human


def test_human_request_hands_over_offline():
    for text in ("I want to talk to a real person", "manche sanga kura garna paryo", "Manager chahiyo"):
        assert wants_human(text)
        r = asyncio.run(generate_reply("sys", [], text))
        assert r.handover and r.reason == "customer_requested_human" and r.tokens == 0
    assert not wants_human("pashmina shawl ko price kati ho?")


def test_prompt_carries_tenant_knowledge_and_language():
    p = build_system_prompt("Shop A", "PRODUCT: X Rs 10", language="english", contact="+977")
    assert "Shop A" in p and "PRODUCT: X Rs 10" in p and "English" in p


def test_amount_guard_regexes():
    from app.services.auto_reply import AMOUNT_RE, NUMBER_RE

    known = {n.replace(",", "") for n in NUMBER_RE.findall("Shawl Rs 4,500. Topi Rs 800")}
    assert AMOUNT_RE.findall("Price Rs 4500 ya NPR 800") == ["4500", "800"]
    assert all(a.replace(",", "") in known for a in AMOUNT_RE.findall("Rs 4,500"))
    assert not all(a.replace(",", "") in known for a in AMOUNT_RE.findall("Total Rs 1,775"))
