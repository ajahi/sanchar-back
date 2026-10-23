"""Reply worker: python -m app.worker  (run as many as the LLM rate limit allows).

Consumes `replies`, runs process_reply_job per message and acks it. A failed job is republished with its try
count to `replies.retry`, or to `replies.dead` after MAX_TRIES. A crash before the ack makes the broker
redeliver, and process_reply_job drops a job that was already answered.
"""
import asyncio
import logging
import uuid

import aio_pika
import uvloop  # comes with uvicorn[standard]; what the API runs on too

from app.core.config import settings
from app.core.log import log_failure
from app.db.session import engine
from app.services import http_client, reply_queue
from app.services.reply_pipeline import process_reply_job

log = logging.getLogger("app.worker")


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
    queue = await channel.get_queue(reply_queue.QUEUE)
    tasks: set[asyncio.Task] = set()

    async def on_message(msg: aio_pika.abc.AbstractIncomingMessage) -> None:
        task = asyncio.create_task(handle(channel, msg))  # prefetch bounds how many run at once
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    await queue.consume(on_message)
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
