"""Durable pull-consumer helpers for JetStream (plan section 9.5)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig
from nats.js.errors import NotFoundError

from nats.aio.msg import Msg  # isort: skip

logger = structlog.stdlib.get_logger(__name__)

MessageHandler = Callable[[Msg], Awaitable[None]]

DEFAULT_ACK_WAIT_SECONDS = 30
DEFAULT_MAX_DELIVER = 5


async def ensure_pull_consumer(
    js: JetStreamContext,
    *,
    stream: str,
    durable: str,
    ack_wait_seconds: int = DEFAULT_ACK_WAIT_SECONDS,
    max_deliver: int = DEFAULT_MAX_DELIVER,
) -> None:
    """Create the durable pull consumer if it does not exist yet."""
    try:
        await js.consumer_info(stream, durable)
    except NotFoundError:
        await js.add_consumer(
            stream,
            ConsumerConfig(
                durable_name=durable,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=ack_wait_seconds,
                max_deliver=max_deliver,
            ),
        )
        logger.info("jetstream.consumer.created", stream=stream, durable=durable)


async def run_pull_consumer(
    js: JetStreamContext,
    *,
    stream: str,
    durable: str,
    handler: MessageHandler,
    stop: asyncio.Event,
    batch: int = 10,
    fetch_timeout_seconds: float = 5.0,
) -> None:
    """Fetch/dispatch loop for a durable pull consumer.

    The handler owns acknowledgment (ack/nak/term). Handler exceptions are
    logged and the message is nak'd for redelivery.
    """
    await ensure_pull_consumer(js, stream=stream, durable=durable)
    subscription = await js.pull_subscribe_bind(durable, stream=stream)
    logger.info("jetstream.consumer.loop_started", stream=stream, durable=durable)
    while not stop.is_set():
        try:
            messages = await subscription.fetch(batch=batch, timeout=fetch_timeout_seconds)
        except NatsTimeoutError:
            continue
        for message in messages:
            try:
                await handler(message)
            except Exception:
                logger.exception(
                    "jetstream.consumer.handler_error",
                    stream=stream,
                    durable=durable,
                    subject=message.subject,
                )
                await message.nak(delay=2)
