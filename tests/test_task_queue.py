"""Background tasks via the `tasks` queue: the no-broker fallback, the worker's dispatch, and that the profile task
reads its token from the DB (the queue carries ids only).

    python -m pytest tests/test_task_queue.py
"""
import asyncio
import json
import uuid

from sqlalchemy import select

from app import worker
from app.api.v1 import webhooks
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import reply_queue


class _Bg:
    def __init__(self):
        self.added = []

    def add_task(self, fn, **kw):
        self.added.append((fn, kw))


def test_run_later_without_broker_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(reply_queue.settings, "rabbitmq_url", "")
    bg, account_id = _Bg(), uuid.uuid4()
    asyncio.run(reply_queue.run_later(bg, webhooks.run_history_sync, account_id=account_id))
    assert bg.added == [(webhooks.run_history_sync, {"account_id": account_id})]


class _Msg:
    def __init__(self, body: bytes):
        self.body, self.acked = body, False

    async def ack(self):
        self.acked = True


class _Channel:
    def __init__(self):
        self.published = []
        self.default_exchange = self

    async def publish(self, message, routing_key):
        self.published.append((routing_key, message.body))


def test_worker_runs_a_task_by_name_and_dead_letters_a_bad_one(monkeypatch) -> None:
    calls = []

    async def fake_sync(account_id):
        calls.append(account_id)

    monkeypatch.setitem(worker.TASK_FNS, "run_history_sync", fake_sync)
    account_id = uuid.uuid4()
    channel = _Channel()
    ok = _Msg(reply_queue.task_message(webhooks.run_history_sync, account_id=account_id).body)
    bad = _Msg(json.dumps({"task": "no_such_task"}).encode())
    asyncio.run(worker.handle_task(channel, ok))
    asyncio.run(worker.handle_task(channel, bad))
    assert calls == [account_id] and ok.acked  # the id arrives as a UUID again
    assert channel.published == [(reply_queue.TASKS_DEAD, bad.body)] and bad.acked


async def _profile(monkeypatch) -> None:
    seen = []

    async def fake_profile(token, igsid):
        seen.append((token, igsid))
        return {"name": "Sita", "username": "sita.np"}

    monkeypatch.setattr(webhooks.CHANNELS["instagram"], "fetch_customer_profile", fake_profile)
    async with async_session_factory() as db:
        t = Tenant(name="Shop")
        db.add(t)
        await db.flush()
        acct = SocialAccount(tenant_id=t.id, platform="instagram", external_account_id="biz-tq",
                             access_token_encrypted=encrypt_token("tok-tq"))
        cust = Customer(tenant_id=t.id, external_user_id="igsid-tq")
        db.add_all([acct, cust])
        await db.flush()
        convo = Conversation(tenant_id=t.id, customer_id=cust.id, social_account_id=acct.id, channel="instagram")
        db.add(convo)
        await db.commit()

    await webhooks.fill_customer_profile(customer_id=cust.id, convo_id=convo.id)
    assert seen == [("tok-tq", "igsid-tq")]  # the token came from the DB, decrypted
    async with async_session_factory() as db:
        assert (await db.execute(select(Customer.name, Customer.external_username).where(Customer.id == cust.id))).one() == (
            "Sita", "sita.np")


def test_profile_task_reads_its_token_from_the_db(monkeypatch) -> None:
    asyncio.run(_profile(monkeypatch))
