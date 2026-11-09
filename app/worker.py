"""Reply worker: python -m app.worker  (run as many as the LLM rate limit allows).

Consumes `replies`, runs process_reply_job per message and acks it. A failed job is republished with its try
count to `replies.retry`, or to `replies.dead` after MAX_TRIES. A crash before the ack makes the broker
redeliver, and process_reply_job drops a job that was already answered.

It also consumes `webhooks` (raw bodies the API acked unstored: process_webhook_job ingests them, with the same
retry and dead queues) on its own channel.

It also consumes `tasks` (profile lookups, history sync, inbox backfill: reply_queue.run_later) on its own channel,
TASK_CONCURRENCY at a time, so a long history sync never takes a reply slot. A task that raises goes to tasks.dead.
"""
import asyncio
import json
import logging
import uuid

import aio_pika
import uvloop  # comes with uvicorn[standard]; what the API runs on too

from app.core.config import settings
from app.core.log import log_failure
from app.db.session import engine
from app.services import http_client, reply_queue
from app.services.reply_pipeline import process_reply_job
from app.api.v1.social_accounts import _after_login, _backfill
from app.api.v1.webhooks import fill_customer_profile, process_webhook_job, run_history_sync

log = logging.getLogger("app.worker")

TASK_FNS = {f.__name__: f for f in (fill_customer_profile, run_history_sync, _backfill, _after_login)}
# shortcut: fixed; a history sync pages Graph for up to a minute, so 5 at once is plenty. Make it a setting if
# the tasks queue backs up.
TASK_CONCURRENCY = 5
# shortcut: fixed; each ingest holds one pooled DB connection for a few ms, so this stays well under the pool.
WEBHOOK_CONCURRENCY = 8


class _Spawn:
    """Stands in for FastAPI's BackgroundTasks: the follow-ups of an ingest run as tasks in this process."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task] = set()

    def add_task(self, fn, *args, **kw) -> None:
        task = asyncio.create_task(fn(*args, **kw))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


_spawn = _Spawn()


async def handle_webhook(channel: aio_pika.abc.AbstractChannel, msg: aio_pika.abc.AbstractIncomingMessage) -> None:
    tries = int(msg.headers.get("attempts", 0)) + 1
    try:
        await process_webhook_job(msg.body, _spawn)
    except Exception:  # noqa: BLE001
        log_failure(log, "webhook job failed (try %s of %s)", tries, reply_queue.MAX_TRIES)
        target = reply_queue.WEBHOOKS_RETRY if tries < reply_queue.MAX_TRIES else reply_queue.WEBHOOKS_DEAD
        await channel.default_exchange.publish(reply_queue.webhook_job(msg.body, tries), routing_key=target)
    await msg.ack()  # after the republish: a crash in between means a duplicate job (deduped), never a lost one


async def handle(channel: aio_pika.abc.AbstractChannel, msg: aio_pika.abc.AbstractIncomingMessage) -> None:
    message_id = uuid.UUID(msg.body.decode())
    tries = int(msg.headers.get("attempts", 0)) + 1
    try:
        await process_reply_job(message_id)
    except Exception:  # noqa: BLE001
        log_failure(log, "reply job %s failed (try %s of %s)", message_id, tries, reply_queue.MAX_TRIES)
        target = reply_queue.RETRY if tries < reply_queue.MAX_TRIES else reply_queue.DEAD
        await channel.default_exchange.publish(reply_queue.job(message_id, tries), routing_key=target)
    await msg.ack()  # after the republish: a crash in between means a duplicate job, never a lost one


async def handle_task(channel: aio_pika.abc.AbstractChannel, msg: aio_pika.abc.AbstractIncomingMessage) -> None:
    try:
        job = json.loads(msg.body)
        fn = TASK_FNS[job.pop("task")]
        await fn(**{k: uuid.UUID(v) for k, v in job.items()})
    except Exception:  # noqa: BLE001 — the tasks log their own failures; this is a bad body or a bug
        log_failure(log, "task %s failed", msg.body[:200])
        await channel.default_exchange.publish(
            aio_pika.Message(msg.body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT), routing_key=reply_queue.TASKS_DEAD
        )
    await msg.ack()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.rabbitmq_url:
        raise SystemExit("RABBITMQ_URL is not set")
    http_client.start()
    conn = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await conn.channel()
    await channel.set_qos(prefetch_count=settings.reply_concurrency)
    await reply_queue.declare(channel)
    task_channel = await conn.channel()  # its own prefetch: tasks never take a reply slot
    await task_channel.set_qos(prefetch_count=TASK_CONCURRENCY)
    webhook_channel = await conn.channel()  # its own prefetch too: ingest never waits behind a reply
    await webhook_channel.set_qos(prefetch_count=WEBHOOK_CONCURRENCY)
    tasks: set[asyncio.Task] = set()

    def consumer(handler, ch):
        async def on_message(msg: aio_pika.abc.AbstractIncomingMessage) -> None:
            task = asyncio.create_task(handler(ch, msg))  # prefetch bounds how many run at once
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return on_message

    await (await channel.get_queue(reply_queue.QUEUE)).consume(consumer(handle, channel))
    await (await task_channel.get_queue(reply_queue.TASKS)).consume(consumer(handle_task, task_channel))
    await (await webhook_channel.get_queue(reply_queue.WEBHOOKS)).consume(consumer(handle_webhook, webhook_channel))
    log.info("reply worker up, concurrency %s", settings.reply_concurrency)
    try:
        await asyncio.Future()
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
        await conn.close()
        await http_client.stop()
        await engine.dispose()


if __name__ == "__main__":
    uvloop.run(main())
