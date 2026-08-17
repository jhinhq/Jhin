"""Event worker: durable JetStream pull consumer on the EVENTS stream.

Bootstraps the core streams at startup (idempotent), then consumes with a
durable pull consumer so unacked messages are redelivered after restarts.
"""

from __future__ import annotations

import asyncio
import signal

import nats
from nats.aio.client import Client as NatsClient
from temporalio.client import Client as TemporalClient

from jhin_db import create_engine, create_session_factory
from jhin_event_worker.matcher import TriggerMatcher
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


async def temporal_with_retry(settings: Settings) -> TemporalClient:
    delay = 1.0
    while True:
        try:
            return await TemporalClient.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
        except Exception as exc:
            logger.warning(
                "temporal.connect_retry",
                address=settings.temporal_address,
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

    temporal = await temporal_with_retry(settings)
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    logger.info("temporal.connected", address=settings.temporal_address)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    heartbeat_task = asyncio.create_task(run_heartbeat())
    matcher = TriggerMatcher(
        session_factory, temporal, cache_ttl_seconds=settings.trigger_cache_ttl_seconds
    )
    processor = EventProcessor(js, matcher=matcher)
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
        await engine.dispose()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
