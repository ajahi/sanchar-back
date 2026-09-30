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
