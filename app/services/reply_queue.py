"""RabbitMQ transport for auto-replies: the webhook publishes a message id, app/worker.py consumes it.

Queues (all durable, default exchange): `replies` is the work; a failed job goes to `replies.retry`, which
holds it reply_retry_seconds and dead-letters it back to `replies`; after MAX_TRIES it lands in `replies.dead`
for a person to look at. The message id is the whole payload: the worker reloads everything from the DB.
"""
import logging
import time

import aio_pika

from app.core.config import settings

log = logging.getLogger(__name__)

QUEUE, RETRY, DEAD = "replies", "replies.retry", "replies.dead"
MAX_TRIES = 3
_BACKOFF = 10  # seconds to leave a broker alone after a failed connect, so webhooks don't each wait on it

_conn: aio_pika.abc.AbstractRobustConnection | None = None
_channel: aio_pika.abc.AbstractChannel | None = None
_down_until = 0.0


async def declare(channel: aio_pika.abc.AbstractChannel) -> None:
    """Idempotent; publisher and worker both call it, so start order does not matter."""
    await channel.declare_queue(QUEUE, durable=True)
    await channel.declare_queue(DEAD, durable=True)
    await channel.declare_queue(
        RETRY,
        durable=True,
        arguments={
            "x-message-ttl": settings.reply_retry_seconds * 1000,
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": QUEUE,
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


async def publish(message_id) -> bool:
    """True once the broker has confirmed the job; False = nobody has it, run the reply in-process."""
    channel = await _ensure()
    if channel is None:
        return False
    try:
        await channel.default_exchange.publish(job(message_id), routing_key=QUEUE, timeout=5)
    except Exception:  # noqa: BLE001
        log.exception("publishing reply job %s failed", message_id)
        return False
    return True


async def stop() -> None:
    if _conn is not None:
        await _conn.close()
