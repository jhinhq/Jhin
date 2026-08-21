"""Event worker: durable JetStream pull consumer on the EVENTS stream.

Bootstraps the core streams at startup (idempotent), then consumes with a
durable pull consumer so unacked messages are redelivered after restarts.
"""

from __future__ import annotations

import asyncio
import math
import signal
import sys
from contextlib import suppress
from typing import cast

import nats
from nats.aio.client import Client as NatsClient
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.client import Client as TemporalClient

from jhin_db import create_engine, create_session_factory
from jhin_event_worker.matcher import TriggerMatcher
from jhin_event_worker.normalizer import IngressNormalizer
from jhin_event_worker.processor import EventProcessor
from jhin_event_worker.settings import Settings
from jhin_events.consumer import run_pull_consumer
from jhin_events.streams import EVENTS_STREAM, INGRESS_STREAM, ensure_streams
from jhin_events.telemetry import (
    ConsumerInfoClient,
    ConsumerName,
    JetStreamPublisher,
    StreamName,
)
from jhin_observability import (
    JhinMetrics,
    Observation,
    configure_json_logging,
    get_logger,
    initialize_observability,
    normalize_environment,
    service_version,
)
from jhin_observability.healthfile import clear_heartbeat, run_heartbeat
from jhin_secrets.redaction import redact_event_dict

logger = get_logger(__name__)

CONSUMERS: tuple[tuple[StreamName, ConsumerName], ...] = (
    ("INGRESS", "event-worker-ingress"),
    ("EVENTS", "event-worker"),
)


async def connect_with_retry(settings: Settings) -> NatsClient:
    delay = 1.0
    while True:
        try:
            return await nats.connect(settings.nats_url, connect_timeout=3)
        except Exception as exc:
            logger.warning(
                "nats.connect_retry",
                error_type=type(exc).__name__,
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
                error_type=type(exc).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


def _positive_finite_seconds(value: object, *, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be a finite positive number")
    return float(value)


async def sample_nats_consumer_lag_once(
    js: ConsumerInfoClient,
    metrics: JhinMetrics,
    consumers: tuple[tuple[StreamName, ConsumerName], ...],
    last_values: dict[tuple[StreamName, ConsumerName], int],
    *,
    probe_timeout_seconds: float = 2.0,
) -> None:
    timeout = _positive_finite_seconds(
        probe_timeout_seconds,
        field="probe_timeout_seconds",
    )
    observations: list[Observation] = []
    for stream, consumer in consumers:
        key = (stream, consumer)
        try:
            info = await asyncio.wait_for(
                js.consumer_info(stream, consumer),
                timeout=timeout,
            )
            pending = info.num_pending
            if type(pending) is not int or pending < 0:
                raise ValueError("invalid NATS consumer lag")
            last_values[key] = pending
        except Exception as exc:
            logger.warning(
                "telemetry.nats_lag_probe_failed",
                stream=stream,
                consumer=consumer,
                error_type=type(exc).__name__,
            )
        if key in last_values:
            observations.append(
                Observation(
                    last_values[key],
                    {"stream": stream, "consumer": consumer},
                )
            )
    try:
        metrics.set_observable("nats_consumer_lag", observations)
    except Exception as exc:
        logger.warning(
            "telemetry.nats_lag_probe_failed",
            stream="other",
            consumer="other",
            error_type=type(exc).__name__,
        )


async def poll_nats_consumer_lag(
    js: ConsumerInfoClient,
    metrics: JhinMetrics,
    consumers: tuple[tuple[StreamName, ConsumerName], ...],
    stop: asyncio.Event,
    *,
    interval_seconds: float = 10.0,
    probe_timeout_seconds: float = 2.0,
) -> None:
    interval = _positive_finite_seconds(interval_seconds, field="interval_seconds")
    probe_timeout = _positive_finite_seconds(
        probe_timeout_seconds,
        field="probe_timeout_seconds",
    )
    last_values: dict[tuple[StreamName, ConsumerName], int] = {}
    while not stop.is_set():
        await sample_nats_consumer_lag_once(
            js,
            metrics,
            consumers,
            last_values,
            probe_timeout_seconds=probe_timeout,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


async def main() -> None:
    settings = Settings()
    runtime = initialize_observability(
        settings.observability_config(
            service_name="event-worker",
            service_version=service_version("jhin-event-worker"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    configure_json_logging(
        service="event-worker",
        environment=normalize_environment(settings.app_env),
        level=settings.log_level,
        extra_processors=(redact_event_dict,),
    )
    client: NatsClient | None = None
    engine: AsyncEngine | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    lag_task: asyncio.Task[None] | None = None
    stop = asyncio.Event()
    registered_signals: list[signal.Signals] = []
    try:
        client = await connect_with_retry(settings)
        js = client.jetstream()
        publish_js = cast(JetStreamPublisher, js)
        await ensure_streams(js)
        logger.info("nats.connected", stream=EVENTS_STREAM)

        temporal = await temporal_with_retry(settings)
        engine = create_engine(settings.database_url, trace_sql=True, tracer=runtime.tracer)
        session_factory = create_session_factory(engine)
        logger.info("temporal.connected", task_queue="other")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
                registered_signals.append(sig)

        heartbeat_task = asyncio.create_task(
            run_heartbeat(),
            name="event-worker-heartbeat",
        )
        lag_task = asyncio.create_task(
            poll_nats_consumer_lag(
                cast(ConsumerInfoClient, js),
                runtime.metrics,
                CONSUMERS,
                stop,
            ),
            name="event-worker-nats-lag",
        )
        matcher = TriggerMatcher(
            session_factory,
            temporal,
            cache_ttl_seconds=settings.trigger_cache_ttl_seconds,
        )
        processor = EventProcessor(
            publish_js,
            matcher=matcher,
            tracer=runtime.tracer,
        )
        normalizer = IngressNormalizer(publish_js, tracer=runtime.tracer)

        # Product consumers fail together; diagnostics remain separately owned.
        async with asyncio.TaskGroup() as consumers:
            consumers.create_task(
                run_pull_consumer(
                    js,
                    stream=EVENTS_STREAM,
                    durable=settings.consumer_durable_name,
                    handler=processor.handle,
                    stop=stop,
                    tracer=runtime.tracer,
                ),
                name="event-worker-events-consumer",
            )
            consumers.create_task(
                run_pull_consumer(
                    js,
                    stream=INGRESS_STREAM,
                    durable=settings.ingress_durable_name,
                    handler=normalizer.handle,
                    stop=stop,
                    tracer=runtime.tracer,
                ),
                name="event-worker-ingress-consumer",
            )
        logger.info("worker.stopping")
    finally:
        active_error = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        stop.set()
        for task in (heartbeat_task, lag_task):
            if task is not None and not task.done():
                task.cancel()
        background_tasks = [task for task in (heartbeat_task, lag_task) if task is not None]
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        loop = asyncio.get_running_loop()
        for sig in registered_signals:
            with suppress(Exception):
                loop.remove_signal_handler(sig)
        try:
            clear_heartbeat()
        except BaseException as exc:
            cleanup_error = exc
        if client is not None:
            try:
                await client.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if engine is not None:
            try:
                await engine.dispose()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        try:
            runtime.shutdown(timeout_millis=5_000)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if active_error is None and cleanup_error is not None:
            raise cleanup_error


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
