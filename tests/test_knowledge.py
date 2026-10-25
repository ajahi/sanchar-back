"""Shop-context endpoints against Postgres (always rolled back), LLM faked.

    python -m pytest tests/test_knowledge.py
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app.api.v1 import knowledge
from app.db.session import async_session_factory
from app.models.knowledge import KnowledgeDocument
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.knowledge import TestIn, TopicIn


async def _run(monkeypatch) -> None:
    async def fake_generate(system, history, text, hint=""):
        assert "## Courses\nMBA - 2 years" in system  # the owner's own topic title and answers reach the bot
        return "MBA is 2 years."

    monkeypatch.setattr(knowledge, "generate_reply", fake_generate)

    async with async_session_factory() as db:
        db.commit = db.flush  # the endpoints commit; in a test that must stay inside the rolled-back transaction
        mine, other = Tenant(name="Shop"), Tenant(name="Other")
        db.add_all([mine, other])
        await db.flush()
        user = User(tenant_id=mine.id, name="O", email="knowledge-test@example.com")
        db.add(user)
        await db.flush()

        # empty to start, with starter suggestions per business type
        got = await knowledge.get_knowledge(tenant=mine, db=db)
        assert got.topics == [] and not got.ai_enabled
        assert {"Clothing", "Consultancy", "Nail salon"} <= set(got.suggestions)

        # testing the bot with no context is refused
        with pytest.raises(HTTPException) as e:
            await knowledge.test_bot(TestIn(message="hi"), tenant=mine, db=db)
        assert e.value.status_code == 409

        # any topic the owner names; content trimmed; the starter hint kept
        t = await knowledge.add_topic(TopicIn(title=" Courses ", content="  MBA - 2 years  ", hint="one per line"), mine, user, db)
        assert (t.title, t.content, t.hint) == ("Courses", "MBA - 2 years", "one per line")
        empty = await knowledge.add_topic(TopicIn(title="Countries"), mine, user, db)
        assert [x.title for x in (await knowledge.get_knowledge(tenant=mine, db=db)).topics] == ["Courses", "Countries"]

        # the bot is fed it, and another tenant sees none of it
        assert (await knowledge.test_bot(TestIn(message="mba kati barsa?"), tenant=mine, db=db)).reply == "MBA is 2 years."
        with pytest.raises(HTTPException):
            await knowledge.test_bot(TestIn(message="hi"), tenant=other, db=db)
        assert (await knowledge.get_knowledge(tenant=other, db=db)).topics == []

        # rename + edit in place; another tenant cannot touch it; unknown id 404s; blank title refused
        t = await knowledge.save_topic(t.id, TopicIn(title="Programs", content="MBA - 2 years"), mine, user, db)
        assert t.title == "Programs"
        for tenant, topic_id in ((other, t.id), (mine, uuid.uuid4())):
            with pytest.raises(HTTPException) as e:
                await knowledge.save_topic(topic_id, TopicIn(title="x"), tenant, user, db)
            assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:
            await knowledge.add_topic(TopicIn(title="   "), mine, user, db)
        assert e.value.status_code == 422

        # the size cap is on all topics together
        monkeypatch.setattr(knowledge, "MAX_CHARS", 20)
        with pytest.raises(HTTPException) as e:
            await knowledge.save_topic(empty.id, TopicIn(title="Countries", content="x" * 10), mine, user, db)
        assert e.value.status_code == 422

        # delete removes it
        await knowledge.delete_topic(empty.id, mine, user, db)
        assert [x.title for x in (await knowledge.get_knowledge(tenant=mine, db=db)).topics] == ["Programs"]

        # rows from the old fixed sections show up as ordinary topics
        db.add(KnowledgeDocument(tenant_id=mine.id, title="Delivery", document_type="delivery", content="Rs 100"))
        await db.flush()
        assert "Delivery" in [x.title for x in (await knowledge.get_knowledge(tenant=mine, db=db)).topics]
        await db.rollback()


def test_knowledge(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))
