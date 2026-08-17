"""Event worker: durable JetStream pull consumer on the EVENTS stream.

Bootstraps the core streams at startup (idempotent), then consumes with a
durable pull consumer so unacked messages are redelivered after restarts.
"""

from __future__ import annotations

import asyncio
import signal

import nats
from nats.aio.client import Client as NatsClient

from jhin_event_worker.normalizer import IngressNormalizer
from jhin_event_worker.processor import EventProcessor
from jhin_event_worker.settings import Settings
from jhin_events.consumer import run_pull_consumer
from jhin_events.streams import EVENTS_STREAM, INGRESS_STREAM, ensure_streams
from jhin_observability import configure_logging, get_logger
from jhin_observability.healthfile import clear_heartbeat, run_heartbeat

logger = get_logger(__name__)


async def connect_with_retry(settings: Settings) -> NatsClient:
    delay = 1.0
    while True:
        try:
            return await nats.connect(settings.nats_url, connect_timeout=3)
        except Exception as exc:
            logger.warning(
                "nats.connect_retry",
                url=settings.nats_url,
                error=f"{type(exc).__name__}: {exc}"[:200],
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def main() -> None:
    settings = Settings()
    configure_logging("event-worker", settings.log_level)

    client = await connect_with_retry(settings)
    js = client.jetstream()
    await ensure_streams(js)
    logger.info("nats.connected", url=settings.nats_url, stream=EVENTS_STREAM)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    heartbeat_task = asyncio.create_task(run_heartbeat())
    processor = EventProcessor(js)
    normalizer = IngressNormalizer(js)
    try:
        # Two durable consumers side by side: canonical EVENTS processing and
        # INGRESS normalization (raw webhook payloads → connector.* events).
        await asyncio.gather(
            run_pull_consumer(
                js,
                stream=EVENTS_STREAM,
                durable=settings.consumer_durable_name,
                handler=processor.handle,
                stop=stop,
            ),
            run_pull_consumer(
                js,
                stream=INGRESS_STREAM,
                durable=settings.ingress_durable_name,
                handler=normalizer.handle,
                stop=stop,
            ),
        )
        logger.info("worker.stopping")
    finally:
        heartbeat_task.cancel()
        clear_heartbeat()
        await client.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
