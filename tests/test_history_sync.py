"""History sync: Graph paging (both channels) and the background sync that pulls older threads.
The DB test runs in a rolled-back transaction except where noted.
    python -m pytest tests/test_history_sync.py
"""
import asyncio

import httpx
from sqlalchemy import select

from app.api.v1 import webhooks
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.sales_lead import SalesLead
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services.meta import facebook, instagram

_REAL_CLIENT = httpx.AsyncClient  # before any patching: monkeypatch replaces it on the shared httpx module


def _mock(monkeypatch, module, handler) -> None:
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
    )


def test_paging_cursor_both_channels(monkeypatch) -> None:
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        more = "after" not in req.url.params  # first page has a next, the page after the cursor is the last
        paging = {"cursors": {"after": "CUR1"}, "next": "https://x"} if more else {"cursors": {"after": "END"}}
        return httpx.Response(200, json={"data": [{"id": "t"}], "paging": paging})

    for module in (instagram, facebook):
        _mock(monkeypatch, module, handler)
        seen.clear()
        data, cursor = asyncio.run(module.fetch_conversations_page("TOK", 10))
        assert data == [{"id": "t"}] and cursor == "CUR1" and seen[0]["limit"] == "10"
        data, cursor = asyncio.run(module.fetch_conversations_page("TOK", 10, "CUR1"))
        assert cursor is None and seen[1]["after"] == "CUR1"  # no `next` = no more pages
        assert asyncio.run(module.fetch_recent_conversations("TOK", 5)) == [{"id": "t"}]  # old helper still works


def _thread(tid, psid, msgs):
    return {"id": tid, "updated_time": "2026-10-01T10:00:00+0000",
            "participants": {"data": [{"id": "page-7", "name": "Shop"}, {"id": psid, "name": f"Cust {psid}"}]},
            "messages": {"data": msgs}}  # Graph lists newest first


def _msg(mid, who, text, t):
    return {"id": mid, "created_time": f"2026-10-01T10:0{t}:00+0000", "from": {"id": who}, "message": text}


async def _run(monkeypatch) -> None:
    pages = {
        None: ([_thread("t1", "c1", [_msg("h2", "c1", "ok 9812345678", 2), _msg("h1", "c1", "price?", 1)])], "P2"),
        "P2": ([_thread("t2", "c2", [_msg("h3", "c2", "hi", 1)])], None),
    }

    async def fake_page(token, limit, after=None):
        return pages[after]

    monkeypatch.setattr(facebook, "fetch_conversations_page", fake_page)
    async with async_session_factory() as db:
        t = Tenant(name="Sync T")
        db.add(t)
        await db.flush()
        acc = SocialAccount(tenant_id=t.id, platform="facebook", external_account_id="page-7",
                            access_token_encrypted=encrypt_token("tok"),
                            meta={"history_syncing_at": "2026-10-01T00:00:00+00:00"})
        db.add(acc)
        await db.commit()  # run_history_sync uses its own sessions, so this one must be committed
        acc_id, tenant_id = acc.id, t.id

    try:
        await webhooks.run_history_sync(acc_id)
        await webhooks.run_history_sync(acc_id)  # second click starts from the top again: nothing doubles
        async with async_session_factory() as db:
            convos = (await db.execute(select(Conversation).where(Conversation.tenant_id == tenant_id))).scalars().all()
            assert len(convos) == 2 and {c.channel for c in convos} == {"facebook"}
            ids = (await db.execute(select(Message.external_message_id).join(Conversation).where(
                Conversation.tenant_id == tenant_id))).scalars().all()
            assert sorted(ids) == ["h1", "h2", "h3"]
            lead = (await db.execute(select(SalesLead).where(SalesLead.tenant_id == tenant_id))).scalar_one()
            assert lead.phone == "+9779812345678" and lead.channel == "facebook"  # found in an OLD message
            account = await db.get(SocialAccount, acc_id)
            assert "history_syncing_at" not in account.meta and account.meta["history_after"] is None
    finally:  # committed data: remove the tenant (everything under it cascades)
        async with async_session_factory() as db:
            tenant = await db.get(Tenant, tenant_id)
            if tenant is not None:
                await db.delete(tenant)
                await db.commit()


def test_run_history_sync(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))
