"""Shop-context endpoints against Postgres (always rolled back), LLM faked.

    python -m pytest tests/test_knowledge.py
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.api.v1 import knowledge
from app.db.session import async_session_factory
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.knowledge import SectionIn, TestIn
from app.services.auto_reply import Reply


async def _run(monkeypatch) -> None:
    async def fake_generate(system, history, text):
        assert "Shawl Rs 4,500" in system and "Shop" in system
        return Reply("Rs 4,500 ho.", False, "", 5)

    monkeypatch.setattr(knowledge, "generate_reply", fake_generate)

    async with async_session_factory() as db:
        mine, other = Tenant(name="Shop"), Tenant(name="Other")
        db.add_all([mine, other])
        await db.flush()
        user = User(tenant_id=mine.id, name="O", email="knowledge-test@example.com")
        db.add(user)
        await db.flush()

        # empty to start; every guided section listed in order
        got = await knowledge.get_knowledge(tenant=mine, db=db)
        assert [s.key for s in got.sections] == [k for k, _, _ in knowledge.SECTIONS]
        assert all(s.content == "" for s in got.sections) and not got.ai_enabled

        # testing the bot with no context is refused
        with pytest.raises(HTTPException) as e:
            await knowledge.test_bot(TestIn(message="hi"), tenant=mine, db=db)
        assert e.value.status_code == 409

        # save, update in place, read back (trimmed)
        await knowledge.save_section("products", SectionIn(content="  Shawl Rs 4,500  "), mine, user, db)
        await knowledge.save_section("products", SectionIn(content="Shawl Rs 4,500"), mine, user, db)
        got = await knowledge.get_knowledge(tenant=mine, db=db)
        assert {s.key: s.content for s in got.sections}["products"] == "Shawl Rs 4,500"

        # the bot is fed exactly that, and another tenant sees none of it
        assert (await knowledge.test_bot(TestIn(message="price?"), tenant=mine, db=db)).reply == "Rs 4,500 ho."
        with pytest.raises(HTTPException):
            await knowledge.test_bot(TestIn(message="price?"), tenant=other, db=db)
        assert all(s.content == "" for s in (await knowledge.get_knowledge(tenant=other, db=db)).sections)

        # unknown section 404s; over the size cap is refused; clearing removes the section
        with pytest.raises(HTTPException) as e:
            await knowledge.save_section("nope", SectionIn(content="x"), mine, user, db)
        assert e.value.status_code == 404
        monkeypatch.setattr(knowledge, "MAX_CHARS", 20)
        with pytest.raises(HTTPException) as e:
            await knowledge.save_section("about", SectionIn(content="x" * 10), mine, user, db)
        assert e.value.status_code == 422
        await knowledge.save_section("products", SectionIn(content="   "), mine, user, db)
        assert all(s.content == "" for s in (await knowledge.get_knowledge(tenant=mine, db=db)).sections)
        await db.rollback()


def test_knowledge(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))
