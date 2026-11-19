"""RabbitMQ transport for auto-replies: the webhook publishes a message id, app/worker.py consumes it.

Queues (all durable, default exchange): `replies` is the work; a failed job goes to `replies.retry`, which
holds it reply_retry_seconds and dead-letters it back to `replies`; after MAX_TRIES it lands in `replies.dead`
for a person to look at. The message id is the whole payload: the worker reloads everything from the DB.

`webhooks` holds raw webhook bodies that the API verified and acked without storing (the worker runs the ingest);
it has the same retry and dead queues. Ingest is idempotent (message ids dedupe), so a retry is safe.

`tasks` holds the other background jobs (profile lookups, history sync, inbox backfill): {"task": name, ...ids}.
They are best-effort and log their own failures, so there is no retry; one that raises goes to `tasks.dead`.
Ids only, never a token: each task reads and decrypts its account's token itself.
"""
import json
import logging
import time

import aio_pika

from app.core.config import settings

log = logging.getLogger(__name__)

QUEUE, RETRY, DEAD = "replies", "replies.retry", "replies.dead"
TASKS, TASKS_DEAD = "tasks", "tasks.dead"
WEBHOOKS, WEBHOOKS_RETRY, WEBHOOKS_DEAD = "webhooks", "webhooks.retry", "webhooks.dead"
_WEBHOOK_TIMEOUT = 5  # the API waits this long for the broker before it ingests inline; Meta is waiting on the 200
MAX_TRIES = 3
_BACKOFF = 10  # seconds to leave a broker alone after a failed connect, so webhooks don't each wait on it
# Publishes share one channel lock, so a saturated event loop queues them for minutes with a healthy broker. A
# short timeout reads that as "broker down" and the in-process fallback then buries the loop (load test, 200 msgs/s).
# A dead connection raises at once, so this only has to outlast overload: as long as a reply is worth sending.
_PUBLISH_TIMEOUT = settings.reply_max_age_minutes * 60

_conn: aio_pika.abc.AbstractRobustConnection | None = None
_channel: aio_pika.abc.AbstractChannel | None = None
_down_until = 0.0


async def declare(channel: aio_pika.abc.AbstractChannel) -> None:
    """Idempotent; publisher and worker both call it, so start order does not matter."""
    for name in (QUEUE, DEAD, TASKS, TASKS_DEAD, WEBHOOKS, WEBHOOKS_DEAD):
        await channel.declare_queue(name, durable=True)
    for retry, back in ((RETRY, QUEUE), (WEBHOOKS_RETRY, WEBHOOKS)):
        await channel.declare_queue(
            retry,
            durable=True,
            arguments={
                "x-message-ttl": settings.reply_retry_seconds * 1000,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": back,
            },
        )


def job(message_id, attempts: int = 0) -> aio_pika.Message:
    return aio_pika.Message(
        str(message_id).encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT, headers={"attempts": attempts}
    )


async def _ensure() -> aio_pika.abc.AbstractChannel | None:
    global _conn, _channel, _down_until
    if _channel is not None and not _channel.is_closed:
        return _channel
    if not settings.rabbitmq_url or time.monotonic() < _down_until:
        return None
    try:
        _conn = await aio_pika.connect_robust(settings.rabbitmq_url, timeout=2)  # then it reconnects itself
        _channel = await _conn.channel()  # publisher confirms are on by default
        await declare(_channel)
    except Exception:  # noqa: BLE001
        log.exception("RabbitMQ unavailable, replies run in-process for now")
        _down_until = time.monotonic() + _BACKOFF
        _channel = None
        return None
    return _channel


async def _publish(message: aio_pika.Message, queue: str, timeout: float = _PUBLISH_TIMEOUT) -> bool:
    channel = await _ensure()
    if channel is None:
        return False
    try:
        await channel.default_exchange.publish(message, routing_key=queue, timeout=timeout)
    except Exception:  # noqa: BLE001
        log.exception("publishing to %s failed: %s", queue, message.body[:200])
        return False
    return True


async def publish(message_id) -> bool:
    """True once the broker has confirmed the job; False = nobody has it, run the reply in-process."""
    return await _publish(job(message_id), QUEUE)


def webhook_job(raw: bytes, attempts: int = 0) -> aio_pika.Message:
    return aio_pika.Message(raw, delivery_mode=aio_pika.DeliveryMode.PERSISTENT, headers={"attempts": attempts})


async def publish_webhook(raw: bytes) -> bool:
    """True once the broker has confirmed the body: only then may the API answer 200 without storing it."""
    return await _publish(webhook_job(raw), WEBHOOKS, _WEBHOOK_TIMEOUT)


def task_message(fn, **ids) -> aio_pika.Message:
    body = json.dumps({"task": fn.__name__, **{k: str(v) for k, v in ids.items()}})
    return aio_pika.Message(body.encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT)


async def run_later(bg, fn, **ids) -> None:
    """fn(**ids) on the worker (app.worker.TASK_FNS), or as a FastAPI background task (bg) when no broker has it.
    Call it after the commit: the worker may start before the request ends, and reads the stored rows."""
    if not await _publish(task_message(fn, **ids), TASKS):
        bg.add_task(fn, **ids)


async def stop() -> None:
    if _conn is not None:
        await _conn.close()
