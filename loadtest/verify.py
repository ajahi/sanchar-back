"""After a k6 run: what actually landed in the DB, and how long replies took.

    python -m loadtest.verify

Compare `customer messages` with k6's `messages_sent`: the webhook returns 200 even when ingest fails,
so any gap is silently lost DMs. `ai replies` vs customer messages gap = bot stayed quiet or its LLM/Graph
call failed. Reply latency = inbound stored -> AI reply stored, for conversations with a single customer message.
With RABBITMQ_URL set it also prints how many jobs are still in each reply queue: `replies` > 0 after the drain
wait means the workers are behind, `replies.dead` > 0 means jobs failed all their tries.
A customer message with no AI reply is normal: only the newest message of a quick burst in a chat is answered.
So with the queue, judge replies by `chats with no AI reply`: once the queues are empty it should be 0.
"""
import asyncio
import json
import urllib.request

from sqlalchemy import text

from app.core.config import settings
from app.db.session import async_session_factory
from app.services import reply_queue

COUNTS = """
select m.sender_type, count(*) from messages m
join conversations c on c.id = m.conversation_id join tenants t on t.id = c.tenant_id
where t.name like 'loadtest-%' group by 1"""

LATENCY = """
with per as (
  select min(m.created_at) filter (where m.sender_type = 'customer') as cust,
         min(m.created_at) filter (where m.sender_type = 'ai') as ai,
         count(*) filter (where m.sender_type = 'customer') as n
  from messages m join conversations c on c.id = m.conversation_id join tenants t on t.id = c.tenant_id
  where t.name like 'loadtest-%' group by c.id)
select count(*), percentile_cont(0.5) within group (order by extract(epoch from ai - cust)),
       percentile_cont(0.95) within group (order by extract(epoch from ai - cust)),
       percentile_cont(0.99) within group (order by extract(epoch from ai - cust)),
       max(extract(epoch from ai - cust))
from per where n = 1 and ai is not null"""

UNANSWERED = """
select count(*), count(*) filter (where ai = 0) from (
  select c.id, count(*) filter (where m.sender_type = 'ai') as ai
  from messages m join conversations c on c.id = m.conversation_id join tenants t on t.id = c.tenant_id
  where t.name like 'loadtest-%' group by c.id) per"""


async def queue_depths() -> str | None:
    if not settings.rabbitmq_url:
        return None
    import aio_pika

    try:
        async with await aio_pika.connect_robust(settings.rabbitmq_url, timeout=3) as conn:
            channel = await conn.channel()
            names = (reply_queue.QUEUE, reply_queue.RETRY, reply_queue.DEAD, reply_queue.TASKS, reply_queue.TASKS_DEAD,
                     reply_queue.WEBHOOKS, reply_queue.WEBHOOKS_RETRY, reply_queue.WEBHOOKS_DEAD)
            depths = [(await channel.declare_queue(n, passive=True)).declaration_result.message_count for n in names]
        return "  ".join(f"{n}={d}" for n, d in zip(names, depths))
    except Exception as exc:  # noqa: BLE001 — a diagnostic line, never fail the run over it
        return f"unavailable ({exc!r})"


async def main() -> None:
    async with async_session_factory() as db:
        counts = dict((await db.execute(text(COUNTS))).all())
        n, p50, p95, p99, worst = (await db.execute(text(LATENCY))).one()
        chats, unanswered = (await db.execute(text(UNANSWERED))).one()
    print(f"customer messages stored: {counts.get('customer', 0)}   <- compare with k6 messages_sent")
    print(f"ai replies stored:        {counts.get('ai', 0)}")
    print(f"chats with no AI reply:   {unanswered} of {chats}")
    if n:
        print(f"reply latency over {n} chats: p50={p50:.1f}s  p95={p95:.1f}s  p99={p99:.1f}s  max={worst:.1f}s")
    if (depths := await queue_depths()) is not None:
        print(f"jobs left in the queues:  {depths}")
    try:
        mock = json.load(urllib.request.urlopen("http://localhost:9000/stats", timeout=3))
        print(f"mock saw: {mock}")
    except OSError:
        print("mock server not reachable on :9000 (skipping its counters)")


if __name__ == "__main__":
    asyncio.run(main())
