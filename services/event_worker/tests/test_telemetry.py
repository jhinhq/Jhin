"""Event-worker trace ownership, lag, and lifecycle contracts."""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn, cast
from uuid import UUID

import pytest
import structlog
from nats.js.api import PubAck
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import WorkflowAlreadyStartedError

from jhin_db.base import Base
from jhin_db.models import Agent, Trigger, TriggerInvocation, Workspace
from jhin_domain import AgentStatus, new_uuid7
from jhin_event_worker.matcher import TriggerMatcher
from jhin_event_worker.normalizer import IngressNormalizer
from jhin_event_worker.processor import EventProcessor
from jhin_events.envelope import EventEnvelope, EventSource
from jhin_observability import JhinMetrics, MetricName, Observation, noop_metrics
from jhin_observability.metrics import build_jhin_metrics
from jhin_secrets.redaction import redact_event_dict

KNOWN_CORRELATION = UUID("018f0000-0000-7000-8000-000000000001")
KNOWN_EVENT = UUID("018f0000-0000-7000-8000-000000000002")
KNOWN_WORKSPACE = "018f0000-0000-7000-8000-000000000003"


def telemetry() -> ModuleType:
    return importlib.import_module("jhin_events.telemetry")


def worker_main() -> ModuleType:
    return importlib.import_module("jhin_event_worker.main")


@dataclass
class TraceHarness:
    provider: TracerProvider
    tracer: Tracer
    exporter: InMemorySpanExporter


@pytest.fixture
def tracing() -> Iterator[TraceHarness]:
    provider = TracerProvider(
        resource=Resource(
            {
                "service.name": "event-worker-test",
                "service.version": "test",
                "deployment.environment.name": "test",
            }
        )
    )
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    harness = TraceHarness(provider, provider.get_tracer("event-worker-tests"), exporter)
    try:
        yield harness
    finally:
        provider.shutdown()


def serialized_span(span: ReadableSpan) -> str:
    return json.dumps(
        {
            "name": span.name,
            "resource": dict(span.resource.attributes),
            "attributes": dict(span.attributes or {}),
            "events": [
                {"name": event.name, "attributes": dict(event.attributes or {})}
                for event in span.events
            ],
            "status": {
                "code": span.status.status_code.name,
                "description": span.status.description,
            },
        },
        default=str,
        sort_keys=True,
    )


class WorkerJetStream:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        *,
        headers: Mapping[str, str] | None = None,
    ) -> PubAck:
        self.published.append((subject, payload, dict(headers or {})))
        return cast(PubAck, object())


class WorkerMessage:
    def __init__(
        self,
        data: bytes,
        *,
        subject: str = "jhin.v1.workspace-1.task.created",
        headers: Mapping[str, str] | None = None,
        num_delivered: int = 1,
    ) -> None:
        self.data = data
        self.subject = subject
        self.headers = dict(headers or {})
        self.metadata = SimpleNamespace(num_delivered=num_delivered)
        self.acked = 0
        self.termed = 0
        self.naks = 0

    async def ack(self) -> None:
        self.acked += 1

    async def term(self) -> None:
        self.termed += 1

    async def nak(self, *, delay: int) -> None:
        assert delay == 2
        self.naks += 1


def envelope(*, workspace_id: str = KNOWN_WORKSPACE) -> EventEnvelope:
    return EventEnvelope(
        event_id=KNOWN_EVENT,
        event_type="task.created",
        workspace_id=workspace_id,
        correlation_id=KNOWN_CORRELATION,
        source=EventSource(type="system"),
        data={},
    )


class RecordingMatcher:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls: list[EventEnvelope] = []
        self.contexts: list[dict[str, str]] = []

    async def handle_event(self, value: EventEnvelope) -> None:
        self.calls.append(value)
        self.contexts.append(dict(structlog.contextvars.get_contextvars()))
        if self.failure is not None:
            raise self.failure


@pytest.mark.parametrize(
    "workspace_id",
    [
        "https://workspace-canary.invalid/path?authorization=canary",
        '{"workspace":"payload-canary"}',
        "w" * 129,
        "workspace canary",
        "workspace\ncanary",
    ],
)
async def test_event_processor_omits_unsafe_workspace_but_preserves_business_behavior(
    workspace_id: str,
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    js = WorkerJetStream()
    matcher = RecordingMatcher()
    processor = EventProcessor(js, matcher=matcher, tracer=tracing.tracer)  # type: ignore[arg-type]
    message = WorkerMessage(envelope(workspace_id=workspace_id).to_bytes())

    with structlog.testing.capture_logs() as records:
        await module.dispatch_or_nak(
            message,
            stream="EVENTS",
            durable="event-worker",
            handler=processor.handle,
            tracer=tracing.tracer,
        )

    assert len(matcher.calls) == 1
    assert matcher.calls[0].event_id == KNOWN_EVENT
    assert matcher.calls[0].workspace_id == workspace_id
    assert matcher.calls[0].correlation_id == KNOWN_CORRELATION
    assert matcher.contexts == [{"correlation_id": str(KNOWN_CORRELATION)}]
    assert message.acked == 1
    assert message.termed == 0
    assert message.naks == 0
    consumer = next(
        span for span in tracing.exporter.get_finished_spans() if span.name == "nats.consume"
    )
    attributes = dict(consumer.attributes or {})
    assert attributes["jhin.correlation_id"] == str(KNOWN_CORRELATION)
    assert "jhin.workspace_id" not in attributes
    rendered = serialized_span(consumer) + json.dumps(records, default=str)
    assert workspace_id not in rendered
    assert "authorization=canary" not in rendered
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()


async def test_event_processor_binds_valid_workspace_and_correlation_through_ack(
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    matcher = RecordingMatcher()
    processor = EventProcessor(
        WorkerJetStream(),
        matcher=matcher,
        tracer=tracing.tracer,  # type: ignore[arg-type]
    )
    message = WorkerMessage(envelope().to_bytes())
    await module.dispatch_or_nak(
        message,
        stream="EVENTS",
        durable="event-worker",
        handler=processor.handle,
        tracer=tracing.tracer,
    )
    assert matcher.contexts == [
        {
            "workspace_id": KNOWN_WORKSPACE,
            "correlation_id": str(KNOWN_CORRELATION),
        }
    ]
    consumer = next(
        span for span in tracing.exporter.get_finished_spans() if span.name == "nats.consume"
    )
    assert dict(consumer.attributes or {})["jhin.workspace_id"] == KNOWN_WORKSPACE
    assert dict(consumer.attributes or {})["jhin.correlation_id"] == str(KNOWN_CORRELATION)
    assert message.acked == 1
    assert structlog.contextvars.get_contextvars() == {}


async def test_event_processor_failure_clears_context_and_remains_redeliverable(
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    matcher = RecordingMatcher(failure=RuntimeError("matcher-error-body-canary"))
    processor = EventProcessor(
        WorkerJetStream(),
        matcher=matcher,
        tracer=tracing.tracer,  # type: ignore[arg-type]
    )
    message = WorkerMessage(envelope().to_bytes())
    await module.dispatch_or_nak(
        message,
        stream="EVENTS",
        durable="event-worker",
        handler=processor.handle,
        tracer=tracing.tracer,
    )
    assert len(matcher.calls) == 1
    assert message.acked == 0
    assert message.naks == 1
    assert structlog.contextvars.get_contextvars() == {}
    consumer = next(
        span for span in tracing.exporter.get_finished_spans() if span.name == "nats.consume"
    )
    assert "matcher-error-body-canary" not in serialized_span(consumer)


async def test_event_processor_dedupe_and_ack_behavior_is_unchanged(
    tracing: TraceHarness,
) -> None:
    matcher = RecordingMatcher()
    processor = EventProcessor(
        WorkerJetStream(),
        matcher=matcher,
        tracer=tracing.tracer,  # type: ignore[arg-type]
    )
    first = WorkerMessage(envelope().to_bytes(), num_delivered=1)
    redelivery = WorkerMessage(envelope().to_bytes(), num_delivered=2)
    await processor.handle(first)  # type: ignore[arg-type]
    await processor.handle(redelivery)  # type: ignore[arg-type]
    assert len(matcher.calls) == 1
    assert first.acked == 1
    assert redelivery.acked == 1


class ContextCapturingConnector:
    def __init__(self) -> None:
        self.contexts: list[dict[str, str]] = []

    def normalize_event(self, _raw: object) -> list[object]:
        self.contexts.append(dict(structlog.contextvars.get_contextvars()))
        return []


class SingleRegistry:
    def __init__(self, connector: ContextCapturingConnector) -> None:
        self.connector = connector

    def get(self, connector_type: str) -> ContextCapturingConnector | None:
        return self.connector if connector_type == "github" else None


async def test_normalizer_uses_the_same_safe_workspace_context_authority(
    tracing: TraceHarness,
) -> None:
    unsafe = "https://unsafe-workspace.invalid/?secret=workspace-canary"
    connector = ContextCapturingConnector()
    js = WorkerJetStream()
    normalizer = IngressNormalizer(
        js,
        registry=cast(Any, SingleRegistry(connector)),
        tracer=tracing.tracer,
    )
    value = EventEnvelope(
        event_id=KNOWN_EVENT,
        event_type="ingress.github.issues",
        workspace_id=unsafe,
        correlation_id=KNOWN_CORRELATION,
        source=EventSource(type="github"),
        data={"event": "issues", "delivery_id": "d-1", "payload": {}},
    )
    message = WorkerMessage(
        value.to_bytes(),
        subject="jhin.v1.workspace-1.ingress.github.issues",
    )
    await normalizer.handle(message)  # type: ignore[arg-type]
    assert connector.contexts == [{"correlation_id": str(KNOWN_CORRELATION)}]
    assert message.acked == 1
    assert structlog.contextvars.get_contextvars() == {}


@pytest.mark.parametrize(
    ("handler_type", "subject", "origin"),
    [
        (EventProcessor, "jhin.v1.workspace-subject-canary.task.created", "EVENTS"),
        (
            IngressNormalizer,
            "jhin.v1.workspace-subject-canary.ingress.github.issue.updated",
            "INGRESS",
        ),
    ],
)
async def test_invalid_handlers_publish_only_the_closed_dlq_document(
    handler_type: type[EventProcessor] | type[IngressNormalizer],
    subject: str,
    origin: str,
    tracing: TraceHarness,
) -> None:
    js = WorkerJetStream()
    handler = handler_type(js, tracer=tracing.tracer)  # type: ignore[call-arg,arg-type]
    message = WorkerMessage(
        b'{"raw":"raw-payload-canary","authorization":"auth-canary"}',
        subject=subject,
    )
    await handler.handle(message)  # type: ignore[arg-type]
    assert message.termed == 1
    assert message.acked == 0
    assert len(js.published) == 1
    dlq_subject, payload, _headers = js.published[0]
    assert dlq_subject == f"jhin.dlq.{origin.lower()}"
    assert json.loads(payload) == {
        "schema_version": 1,
        "reason": "invalid_envelope",
        "origin_stream": origin,
        "error_count": 3,
    }
    rendered = (
        payload.decode()
        + "\n"
        + "\n".join(serialized_span(span) for span in tracing.exporter.get_finished_spans())
    )
    for canary in (
        "raw-payload-canary",
        "auth-canary",
        "workspace-subject-canary",
    ):
        assert canary not in rendered


class LagJetStream:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {
            ("INGRESS", "event-worker-ingress"): 4,
            ("EVENTS", "event-worker"): 7,
        }
        self.calls: list[tuple[str, str]] = []
        self.cancelled: set[tuple[str, str]] = set()
        self.called = asyncio.Event()

    async def consumer_info(self, stream: str, consumer: str) -> SimpleNamespace:
        key = (stream, consumer)
        self.calls.append(key)
        self.called.set()
        value = self.values[key]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            try:
                resolved = await cast(Callable[[], Awaitable[object]], value)()
            except asyncio.CancelledError:
                self.cancelled.add(key)
                raise
            return SimpleNamespace(num_pending=resolved)
        return SimpleNamespace(num_pending=value)


def recording_metrics(
    *,
    failure: BaseException | None = None,
) -> tuple[JhinMetrics, list[tuple[MetricName, tuple[Observation, ...]]]]:
    recorded: list[tuple[MetricName, tuple[Observation, ...]]] = []
    noops = noop_metrics()

    def replace(name: MetricName, values: Sequence[Observation]) -> None:
        recorded.append((name, tuple(values)))
        if failure is not None:
            raise failure

    return JhinMetrics(noops.counter, noops.histogram, replace), recorded


def observed_lag(values: Sequence[Observation]) -> dict[tuple[str, str], int | float]:
    return {(item.attributes["stream"], item.attributes["consumer"]): item.value for item in values}


async def test_lag_sampler_replaces_exact_two_series_and_keeps_last_good_values() -> None:
    main = worker_main()
    js = LagJetStream()
    metrics, recorded = recording_metrics()
    last_values: dict[tuple[str, str], int] = {}
    await main.sample_nats_consumer_lag_once(
        js,
        metrics,
        main.CONSUMERS,
        last_values,
        probe_timeout_seconds=0.1,
    )
    assert len(recorded) == 1
    assert recorded[0][0] == "nats_consumer_lag"
    assert observed_lag(recorded[0][1]) == {
        ("INGRESS", "event-worker-ingress"): 4,
        ("EVENTS", "event-worker"): 7,
    }

    js.values = {
        ("INGRESS", "event-worker-ingress"): RuntimeError("probe canary"),
        ("EVENTS", "event-worker"): RuntimeError("probe canary"),
    }
    await main.sample_nats_consumer_lag_once(
        js,
        metrics,
        main.CONSUMERS,
        last_values,
        probe_timeout_seconds=0.1,
    )
    assert observed_lag(recorded[-1][1]) == {
        ("INGRESS", "event-worker-ingress"): 4,
        ("EVENTS", "event-worker"): 7,
    }


class HostileInt:
    def __init__(self) -> None:
        self.calls = 0

    def __int__(self) -> int:
        self.calls += 1
        raise AssertionError("hostile __int__ executed")


@pytest.mark.parametrize("invalid", [None, True, -1, 1.0, "1"])
async def test_lag_sampler_never_coerces_or_replaces_with_invalid_pending(
    invalid: object,
) -> None:
    main = worker_main()
    js = LagJetStream()
    js.values[("INGRESS", "event-worker-ingress")] = invalid
    metrics, recorded = recording_metrics()
    last_values = {
        ("INGRESS", "event-worker-ingress"): 9,
        ("EVENTS", "event-worker"): 8,
    }
    await main.sample_nats_consumer_lag_once(
        js, metrics, main.CONSUMERS, last_values, probe_timeout_seconds=0.1
    )
    assert observed_lag(recorded[-1][1]) == {
        ("INGRESS", "event-worker-ingress"): 9,
        ("EVENTS", "event-worker"): 7,
    }


async def test_lag_sampler_does_not_execute_hostile_integer_conversion() -> None:
    main = worker_main()
    hostile = HostileInt()
    js = LagJetStream()
    js.values[("INGRESS", "event-worker-ingress")] = hostile
    metrics, recorded = recording_metrics()
    last_values = {("INGRESS", "event-worker-ingress"): 9}
    await main.sample_nats_consumer_lag_once(
        js, metrics, main.CONSUMERS, last_values, probe_timeout_seconds=0.1
    )
    assert hostile.calls == 0
    assert observed_lag(recorded[-1][1]) == {
        ("INGRESS", "event-worker-ingress"): 9,
        ("EVENTS", "event-worker"): 7,
    }


async def test_lag_sampler_times_out_one_probe_and_still_samples_the_other() -> None:
    main = worker_main()
    never = asyncio.Event()

    async def blocked() -> object:
        await never.wait()
        return 99

    js = LagJetStream()
    js.values[("INGRESS", "event-worker-ingress")] = blocked
    metrics, recorded = recording_metrics()
    await main.sample_nats_consumer_lag_once(
        js, metrics, main.CONSUMERS, {}, probe_timeout_seconds=0.01
    )
    assert ("INGRESS", "event-worker-ingress") in js.cancelled
    assert js.calls == list(main.CONSUMERS)
    assert observed_lag(recorded[-1][1]) == {("EVENTS", "event-worker"): 7}


async def test_metric_replacement_failure_is_diagnostic_only() -> None:
    main = worker_main()
    metrics, recorded = recording_metrics(failure=RuntimeError("metric-set-canary"))
    await main.sample_nats_consumer_lag_once(
        LagJetStream(),
        metrics,
        main.CONSUMERS,
        {},
        probe_timeout_seconds=0.1,
    )
    assert len(recorded) == 1


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        -1,
        0.0,
        -1.0,
        math.inf,
        -math.inf,
        math.nan,
        10**1_000,
        "1",
        None,
    ],
)
async def test_lag_timeout_and_interval_validation_is_closed(value: object) -> None:
    main = worker_main()
    metrics, _ = recording_metrics()
    with pytest.raises(ValueError):
        await main.sample_nats_consumer_lag_once(
            LagJetStream(),
            metrics,
            main.CONSUMERS,
            {},
            probe_timeout_seconds=value,
        )
    with pytest.raises(ValueError):
        await main.poll_nats_consumer_lag(
            LagJetStream(),
            metrics,
            main.CONSUMERS,
            asyncio.Event(),
            interval_seconds=value,
            probe_timeout_seconds=0.1,
        )


async def test_lag_poller_stops_immediately_and_propagates_cancellation() -> None:
    main = worker_main()
    metrics, recorded = recording_metrics()
    stopped = asyncio.Event()
    stopped.set()
    js = LagJetStream()
    await main.poll_nats_consumer_lag(
        js,
        metrics,
        main.CONSUMERS,
        stopped,
        interval_seconds=0.1,
        probe_timeout_seconds=0.1,
    )
    assert js.calls == []
    assert recorded == []

    running = asyncio.create_task(
        main.poll_nats_consumer_lag(
            js,
            metrics,
            main.CONSUMERS,
            asyncio.Event(),
            interval_seconds=60.0,
            probe_timeout_seconds=0.1,
        )
    )
    await js.called.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


@dataclass
class FakeRuntime:
    events: list[str]
    tracer: object = field(default_factory=object)
    metrics: object = field(default_factory=object)

    def shutdown(self, timeout_millis: int = 5_000) -> None:
        assert timeout_millis == 5_000
        self.events.append("runtime.shutdown")


class FakeSettings:
    def __init__(self, events: list[str]) -> None:
        events.append("settings")
        self.events = events
        self.app_env = "test"
        self.log_level = "INFO"
        self.nats_url = "nats://test"
        self.database_url = "sqlite+aiosqlite:///:memory:"
        self.temporal_address = "temporal:7233"
        self.temporal_namespace = "default"
        self.trigger_cache_ttl_seconds = 5.0
        self.consumer_durable_name = "event-worker"
        self.ingress_durable_name = "event-worker-ingress"

    def observability_config(
        self,
        *,
        service_name: str,
        service_version: str,
        extra_log_processors: tuple[object, ...],
    ) -> object:
        assert service_name == "event-worker"
        assert service_version == "test-version"
        assert extra_log_processors == (redact_event_dict,)
        self.events.append("settings.observability_config")
        return object()


class FakeClient:
    def __init__(self, events: list[str], js: object, *, close_failure: bool = False) -> None:
        self.events = events
        self.js = js
        self.close_failure = close_failure

    def jetstream(self) -> object:
        self.events.append("nats.jetstream")
        return self.js

    async def close(self) -> None:
        self.events.append("nats.close")
        if self.close_failure:
            raise RuntimeError("close canary")


class FakeEngine:
    def __init__(self, events: list[str], *, dispose_failure: bool = False) -> None:
        self.events = events
        self.dispose_failure = dispose_failure

    async def dispose(self) -> None:
        self.events.append("engine.dispose")
        if self.dispose_failure:
            raise RuntimeError("dispose canary")


async def test_temporal_retry_forwards_the_existing_task5_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    settings = FakeSettings(events)
    client = object()
    calls: list[tuple[object, object]] = []

    async def connect(received_settings: object, received_runtime: object) -> object:
        calls.append((received_settings, received_runtime))
        return client

    monkeypatch.setattr(main, "connect_temporal_client", connect)
    assert await main.temporal_with_retry(settings, runtime) is client
    assert calls == [(settings, runtime)]


async def test_runtime_is_owned_when_stop_event_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    stop_error = RuntimeError("stop-event-construction-canary")
    monkeypatch.setattr(main, "Settings", lambda: FakeSettings(events))
    monkeypatch.setattr(main, "service_version", lambda _: "test-version", raising=False)

    def initialize(_config: object) -> FakeRuntime:
        events.append("runtime.initialize")
        return runtime

    def fail_stop_event() -> NoReturn:
        events.append("stop.construct")
        raise stop_error

    monkeypatch.setattr(main, "initialize_observability", initialize, raising=False)
    monkeypatch.setattr(main.asyncio, "Event", fail_stop_event)
    monkeypatch.setattr(main, "clear_heartbeat", lambda: events.append("heartbeat.clear"))

    with pytest.raises(RuntimeError) as excinfo:
        await main.main()
    assert excinfo.value is stop_error
    assert events == [
        "settings",
        "settings.observability_config",
        "runtime.initialize",
        "stop.construct",
        "heartbeat.clear",
        "runtime.shutdown",
    ]
    assert events.count("runtime.shutdown") == 1


class CleanupTrackingClient(FakeClient):
    def __init__(
        self,
        events: list[str],
        js: object,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        super().__init__(events, js)
        self.close_error = close_error
        self.cleanup_task_names: list[str] = []

    async def close(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.cleanup_task_names.append(task.get_name())
        self.events.append("nats.close")
        if self.close_error is not None:
            raise self.close_error


async def test_main_initializes_first_wires_exact_runtime_and_owns_all_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    js = object()
    client = FakeClient(events, js)
    engine = FakeEngine(events)
    consumer_tracers: list[object] = []
    handler_tracers: list[object] = []
    task_names: set[str] = set()

    monkeypatch.setattr(main, "Settings", lambda: FakeSettings(events))
    monkeypatch.setattr(main, "service_version", lambda _: "test-version", raising=False)

    def initialize(config: object) -> FakeRuntime:
        del config
        events.append("runtime.initialize")
        return runtime

    monkeypatch.setattr(main, "initialize_observability", initialize, raising=False)

    async def connect(settings: object) -> FakeClient:
        del settings
        events.append("nats.connect")
        return client

    async def ensure(stream_js: object) -> None:
        assert stream_js is js
        events.append("streams.ensure")

    async def temporal(settings: object, received_runtime: object) -> object:
        del settings
        assert received_runtime is runtime
        events.append("temporal.connect")
        return object()

    def create_engine(
        database_url: str,
        *,
        trace_sql: bool,
        tracer: object,
    ) -> FakeEngine:
        assert database_url == "sqlite+aiosqlite:///:memory:"
        assert trace_sql is True
        assert tracer is runtime.tracer
        events.append("engine.create")
        return engine

    monkeypatch.setattr(main, "connect_with_retry", connect)
    monkeypatch.setattr(main, "ensure_streams", ensure)
    monkeypatch.setattr(main, "temporal_with_retry", temporal)
    monkeypatch.setattr(main, "create_engine", create_engine)
    monkeypatch.setattr(main, "create_session_factory", lambda value: ("sessions", value))

    def matcher(*args: object, **kwargs: object) -> object:
        assert len(args) == 2
        assert args[0] == ("sessions", engine)
        assert kwargs["metrics"] is runtime.metrics
        assert kwargs["tracer"] is runtime.tracer
        assert kwargs["cache_ttl_seconds"] == 5.0
        events.append("matcher.create")
        return object()

    def processor(
        stream_js: object,
        *,
        matcher: object,
        tracer: object,
    ) -> object:
        del matcher
        assert stream_js is js
        handler_tracers.append(tracer)
        events.append("processor.create")
        return SimpleNamespace(handle=lambda _: None)

    def normalizer(stream_js: object, *, tracer: object) -> object:
        assert stream_js is js
        handler_tracers.append(tracer)
        events.append("normalizer.create")
        return SimpleNamespace(handle=lambda _: None)

    monkeypatch.setattr(main, "TriggerMatcher", matcher)
    monkeypatch.setattr(main, "EventProcessor", processor)
    monkeypatch.setattr(main, "IngressNormalizer", normalizer)

    async def heartbeat() -> None:
        task = asyncio.current_task()
        assert task is not None
        task_names.add(task.get_name())
        events.append("heartbeat.start")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("heartbeat.stop")

    async def lag(
        stream_js: object,
        metrics: object,
        consumers: object,
        stop: asyncio.Event,
        **kwargs: object,
    ) -> None:
        del consumers, kwargs
        assert stream_js is js
        assert metrics is runtime.metrics
        task = asyncio.current_task()
        assert task is not None
        task_names.add(task.get_name())
        events.append("lag.start")
        try:
            await stop.wait()
        finally:
            events.append("lag.stop")

    started: set[str] = set()

    async def consume(
        stream_js: object,
        *,
        stream: str,
        durable: str,
        handler: object,
        stop: asyncio.Event,
        tracer: object,
        **kwargs: object,
    ) -> None:
        del stream, handler, kwargs
        assert stream_js is js
        consumer_tracers.append(tracer)
        task = asyncio.current_task()
        assert task is not None
        task_names.add(task.get_name())
        started.add(durable)
        events.append(f"consumer.start.{durable}")
        if started == {"event-worker", "event-worker-ingress"}:
            stop.set()
        await stop.wait()
        events.append(f"consumer.stop.{durable}")

    monkeypatch.setattr(main, "run_heartbeat", heartbeat)
    monkeypatch.setattr(main, "poll_nats_consumer_lag", lag, raising=False)
    monkeypatch.setattr(main, "run_pull_consumer", consume)
    monkeypatch.setattr(main, "clear_heartbeat", lambda: events.append("heartbeat.clear"))

    await main.main()

    assert events[:3] == [
        "settings",
        "settings.observability_config",
        "runtime.initialize",
    ]
    assert consumer_tracers == [runtime.tracer, runtime.tracer]
    assert handler_tracers == [runtime.tracer, runtime.tracer]
    assert task_names == {
        "event-worker-heartbeat",
        "event-worker-nats-lag",
        "event-worker-events-consumer",
        "event-worker-ingress-consumer",
    }
    assert "heartbeat.stop" in events
    assert "lag.stop" in events
    assert events[-4:] == [
        "heartbeat.clear",
        "nats.close",
        "engine.dispose",
        "runtime.shutdown",
    ]
    current = asyncio.current_task()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not current and task.get_name() in task_names and not task.done()
    ]


async def test_cleanup_supervisor_reawaits_through_repeated_outer_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    js = object()
    client = CleanupTrackingClient(events, js)
    engine = FakeEngine(events)
    consumers_ready = asyncio.Event()
    heartbeat_ready = asyncio.Event()
    heartbeat_unwinding = asyncio.Event()
    release_heartbeat = asyncio.Event()
    started_consumers = 0

    monkeypatch.setattr(main, "Settings", lambda: FakeSettings(events))
    monkeypatch.setattr(main, "service_version", lambda _: "test-version", raising=False)
    monkeypatch.setattr(main, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main, "connect_with_retry", lambda _settings: _async_value(client))
    monkeypatch.setattr(main, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(
        main,
        "temporal_with_retry",
        lambda _settings, _runtime=None: _async_value(object()),
    )
    monkeypatch.setattr(main, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(main, "TriggerMatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        main,
        "EventProcessor",
        lambda *_args, **_kwargs: SimpleNamespace(handle=lambda _: None),
    )
    monkeypatch.setattr(
        main,
        "IngressNormalizer",
        lambda *_args, **_kwargs: SimpleNamespace(handle=lambda _: None),
    )

    async def stubborn_heartbeat() -> None:
        heartbeat_ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            heartbeat_unwinding.set()
            await release_heartbeat.wait()
            raise

    async def consume(*_args: object, **_kwargs: object) -> None:
        nonlocal started_consumers
        started_consumers += 1
        if started_consumers == 2:
            consumers_ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "run_heartbeat", stubborn_heartbeat)
    monkeypatch.setattr(main, "poll_nats_consumer_lag", _poll_until_stop)
    monkeypatch.setattr(main, "run_pull_consumer", consume)
    monkeypatch.setattr(main, "clear_heartbeat", lambda: events.append("heartbeat.clear"))

    task = asyncio.create_task(main.main(), name="event-worker-main-test")
    await consumers_ready.wait()
    await heartbeat_ready.wait()
    task.cancel("body-cancellation")
    await heartbeat_unwinding.wait()
    task.cancel("cleanup-cancellation-one")
    await asyncio.sleep(0)
    task.cancel("cleanup-cancellation-two")
    release_heartbeat.set()

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await task
    assert excinfo.value.args == ("body-cancellation",)
    assert client.cleanup_task_names == ["event-worker-cleanup"]
    assert events[-4:] == [
        "heartbeat.clear",
        "nats.close",
        "engine.dispose",
        "runtime.shutdown",
    ]
    assert not [
        candidate
        for candidate in asyncio.all_tasks()
        if candidate is not asyncio.current_task()
        and candidate.get_name() == "event-worker-cleanup"
        and not candidate.done()
    ]


async def test_cleanup_cancellation_outranks_active_body_error_after_all_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    cleanup_cancellation = asyncio.CancelledError("cleanup-cancellation")
    client = CleanupTrackingClient(events, object(), close_error=cleanup_cancellation)
    engine = FakeEngine(events)
    body_error = RuntimeError("body-error-canary")
    monkeypatch.setattr(main, "Settings", lambda: FakeSettings(events))
    monkeypatch.setattr(main, "service_version", lambda _: "test-version", raising=False)
    monkeypatch.setattr(main, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main, "connect_with_retry", lambda _settings: _async_value(client))
    monkeypatch.setattr(main, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(
        main,
        "temporal_with_retry",
        lambda _settings, _runtime=None: _async_value(object()),
    )
    monkeypatch.setattr(main, "create_engine", lambda *_args, **_kwargs: engine)

    def fail_session_factory(_engine: object) -> object:
        raise body_error

    monkeypatch.setattr(main, "create_session_factory", fail_session_factory)
    monkeypatch.setattr(main, "clear_heartbeat", lambda: events.append("heartbeat.clear"))

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await main.main()
    assert excinfo.value.args == ("cleanup-cancellation",)
    assert client.cleanup_task_names == ["event-worker-cleanup"]
    assert "engine.dispose" in events
    assert events[-1] == "runtime.shutdown"


async def test_active_body_error_outranks_cleanup_error_after_all_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    cleanup_error = RuntimeError("cleanup-error-canary")
    client = CleanupTrackingClient(events, object(), close_error=cleanup_error)
    engine = FakeEngine(events, dispose_failure=True)
    body_error = RuntimeError("body-error-canary")
    monkeypatch.setattr(main, "Settings", lambda: FakeSettings(events))
    monkeypatch.setattr(main, "service_version", lambda _: "test-version", raising=False)
    monkeypatch.setattr(main, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main, "connect_with_retry", lambda _settings: _async_value(client))
    monkeypatch.setattr(main, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(
        main,
        "temporal_with_retry",
        lambda _settings, _runtime=None: _async_value(object()),
    )
    monkeypatch.setattr(main, "create_engine", lambda *_args, **_kwargs: engine)

    def fail_session_factory(_engine: object) -> object:
        raise body_error

    monkeypatch.setattr(main, "create_session_factory", fail_session_factory)
    monkeypatch.setattr(main, "clear_heartbeat", lambda: events.append("heartbeat.clear"))

    with pytest.raises(RuntimeError) as excinfo:
        await main.main()
    assert excinfo.value is body_error
    assert client.cleanup_task_names == ["event-worker-cleanup"]
    assert "engine.dispose" in events
    assert events[-1] == "runtime.shutdown"


@pytest.mark.parametrize("failure_stage", ["nats", "temporal", "engine"])
async def test_partial_startup_failure_still_detaches_runtime_and_owned_resources(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    js = object()
    client = FakeClient(events, js)
    engine = FakeEngine(events)
    monkeypatch.setattr(main, "Settings", lambda: FakeSettings(events))
    monkeypatch.setattr(main, "service_version", lambda _: "test-version", raising=False)
    monkeypatch.setattr(
        main,
        "initialize_observability",
        lambda _config: (events.append("runtime.initialize"), runtime)[1],
        raising=False,
    )

    async def connect(_settings: object) -> FakeClient:
        events.append("nats.connect")
        if failure_stage == "nats":
            raise RuntimeError("startup canary")
        return client

    async def ensure(_js: object) -> None:
        events.append("streams.ensure")

    async def temporal(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        events.append("temporal.connect")
        if failure_stage == "temporal":
            raise RuntimeError("startup canary")
        return object()

    def create_engine(*_args: object, **_kwargs: object) -> FakeEngine:
        events.append("engine.create")
        if failure_stage == "engine":
            raise RuntimeError("startup canary")
        return engine

    monkeypatch.setattr(main, "connect_with_retry", connect)
    monkeypatch.setattr(main, "ensure_streams", ensure)
    monkeypatch.setattr(main, "temporal_with_retry", temporal)
    monkeypatch.setattr(main, "create_engine", create_engine)
    monkeypatch.setattr(main, "clear_heartbeat", lambda: events.append("heartbeat.clear"))

    with pytest.raises(RuntimeError, match="startup canary"):
        await main.main()
    assert events[-1] == "runtime.shutdown"
    if failure_stage != "nats":
        assert "nats.close" in events
    if failure_stage == "engine":
        assert "engine.dispose" not in events


async def test_cleanup_failures_cannot_skip_later_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    close_error = RuntimeError("close canary")
    client = CleanupTrackingClient(events, object(), close_error=close_error)
    engine = FakeEngine(events, dispose_failure=True)
    monkeypatch.setattr(main, "Settings", lambda: FakeSettings(events))
    monkeypatch.setattr(main, "service_version", lambda _: "test-version", raising=False)
    monkeypatch.setattr(main, "initialize_observability", lambda _config: runtime, raising=False)
    monkeypatch.setattr(main, "connect_with_retry", lambda _settings: _async_value(client))
    monkeypatch.setattr(main, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(
        main,
        "temporal_with_retry",
        lambda _settings, _runtime=None: _async_value(object()),
    )
    monkeypatch.setattr(main, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(main, "TriggerMatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        main,
        "EventProcessor",
        lambda *_args, **_kwargs: SimpleNamespace(handle=lambda _: None),
    )
    monkeypatch.setattr(
        main,
        "IngressNormalizer",
        lambda *_args, **_kwargs: SimpleNamespace(handle=lambda _: None),
    )

    async def stop_consumer(*_args: object, stop: asyncio.Event, **_kwargs: object) -> None:
        stop.set()

    monkeypatch.setattr(main, "run_pull_consumer", stop_consumer)
    monkeypatch.setattr(main, "run_heartbeat", lambda: _wait_forever())
    monkeypatch.setattr(main, "poll_nats_consumer_lag", _poll_until_stop, raising=False)
    monkeypatch.setattr(main, "clear_heartbeat", lambda: events.append("heartbeat.clear"))

    with pytest.raises(RuntimeError) as excinfo:
        await main.main()
    assert excinfo.value is close_error
    assert client.cleanup_task_names == ["event-worker-cleanup"]
    assert "nats.close" in events
    assert "engine.dispose" in events
    assert events[-1] == "runtime.shutdown"


async def _async_value(value: object) -> object:
    return value


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def _poll_until_stop(
    _js: object,
    _metrics: object,
    _consumers: object,
    stop: asyncio.Event,
    **_kwargs: object,
) -> None:
    await stop.wait()


def test_main_source_initializes_runtime_before_every_resource_owner() -> None:
    source = inspect.getsource(worker_main().main)
    settings_index = source.index("settings = Settings()")
    runtime_index = source.index("runtime = initialize_observability(")
    assert settings_index < runtime_index
    for resource_shape in (
        "connect_with_retry(",
        "temporal_with_retry(",
        "create_engine(",
        "asyncio.create_task(",
        "asyncio.TaskGroup(",
        "TriggerMatcher(",
        "EventProcessor(",
        "IngressNormalizer(",
    ):
        assert runtime_index < source.index(resource_shape)
    tree = ast.parse(source)
    main_node = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    settings_statement_index = next(
        index
        for index, statement in enumerate(main_node.body)
        if isinstance(statement, ast.Assign)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "Settings"
    )
    prebound_names = {
        statement.target.id
        for statement in main_node.body[:settings_statement_index]
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    }
    assert prebound_names == {
        "client",
        "engine",
        "heartbeat_task",
        "lag_task",
        "stop",
        "registered_signals",
    }
    runtime_statement_index = next(
        index
        for index, statement in enumerate(main_node.body)
        if isinstance(statement, ast.Assign)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "initialize_observability"
    )
    assert isinstance(main_node.body[runtime_statement_index + 1], ast.Try)
    engine_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_engine"
    )
    keywords = {keyword.arg: keyword.value for keyword in engine_call.keywords}
    assert isinstance(keywords["trace_sql"], ast.Constant)
    assert keywords["trace_sql"].value is True
    assert ast.unparse(keywords["tracer"]) == "runtime.tracer"
    assert source.count("tracer=runtime.tracer") >= 5
    temporal_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "temporal_with_retry"
    )
    assert [ast.unparse(argument) for argument in temporal_call.args] == [
        "settings",
        "runtime",
    ]
    assert "configure_json_logging" not in source


# --- Task 7: durable trigger transitions own their diagnostics -------------


@dataclass(frozen=True)
class TriggerTelemetryCase:
    workspace_id: UUID
    valid_trigger_id: UUID
    missing_agent_trigger_id: UUID

    def _event(self, connector_type: str, external_id: str, trigger_id: UUID) -> EventEnvelope:
        return EventEnvelope(
            event_id=new_uuid7(),
            event_type=f"connector.{connector_type}.issue.updated",
            workspace_id=str(self.workspace_id),
            correlation_id=new_uuid7(),
            source=EventSource(type=connector_type),
            data={
                "external_id": external_id,
                "trigger_test_id": str(trigger_id),
                "title": "private-trigger-title-canary",
                "description": "private-trigger-description-canary",
                "url": "https://private-trigger-url-canary.invalid/issue",
                "state": {"name": "Todo"},
                "changed_from": {"state": {"name": "Backlog"}},
            },
        )

    def event_for(self, connector_type: str, *, external_id: str) -> EventEnvelope:
        return self._event(connector_type, external_id, self.valid_trigger_id)

    def event_for_missing_agent(self, connector_type: str, *, external_id: str) -> EventEnvelope:
        return self._event(connector_type, external_id, self.missing_agent_trigger_id)


class FailingTemporalClient:
    """Identity-preserving fake: Temporal owns duplicate-start idempotency."""

    def __init__(self) -> None:
        self.fail_with: BaseException | None = None
        self.calls: list[dict[str, Any]] = []
        self.before_start: Callable[[], None] | None = None

    async def start_workflow(self, name: str, params: Any, *, id: str, task_queue: str) -> object:
        if self.before_start is not None:
            self.before_start()
        if self.fail_with is not None:
            raise self.fail_with
        if any(call["id"] == id for call in self.calls):
            raise WorkflowAlreadyStartedError(id, name)
        self.calls.append({"name": name, "params": params, "id": id, "task_queue": task_queue})
        return object()


@dataclass
class TriggerMetrics:
    metrics: JhinMetrics
    reader: InMemoryMetricReader
    provider: MeterProvider


@pytest.fixture
def trigger_metrics() -> Iterator[TriggerMetrics]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    owned = TriggerMetrics(
        metrics=build_jhin_metrics(provider.get_meter("trigger-test-meter")),
        reader=reader,
        provider=provider,
    )
    try:
        yield owned
    finally:
        provider.shutdown()


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _trigger_filter(trigger_test_id: UUID) -> dict[str, Any]:
    return {
        "all": [
            {"path": "data.trigger_test_id", "op": "eq", "value": str(trigger_test_id)},
            {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
        ]
    }


@pytest.fixture
async def trigger_case(
    session_factory: async_sessionmaker[AsyncSession],
) -> TriggerTelemetryCase:
    async with session_factory() as session:
        workspace = Workspace(name="Private trigger workspace", slug=f"trg-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id,
            name="Private trigger agent",
            slug="trigger-agent",
            status=AgentStatus.ACTIVE.value,
        )
        session.add(agent)
        await session.flush()
        valid_id = new_uuid7()
        missing_id = new_uuid7()
        session.add(
            Trigger(
                id=valid_id,
                workspace_id=workspace.id,
                name="private-valid-trigger-name-canary",
                enabled=True,
                connection_id=None,
                event_type=None,
                filter_json=_trigger_filter(valid_id),
                target_agent_id=agent.id,
                dedupe_window_seconds=300,
            )
        )
        session.add(
            Trigger(
                id=missing_id,
                workspace_id=workspace.id,
                name="private-missing-trigger-name-canary",
                enabled=True,
                connection_id=None,
                event_type=None,
                filter_json=_trigger_filter(missing_id),
                target_agent_id=new_uuid7(),
                dedupe_window_seconds=300,
            )
        )
        await session.commit()
        return TriggerTelemetryCase(
            workspace_id=workspace.id,
            valid_trigger_id=valid_id,
            missing_agent_trigger_id=missing_id,
        )


@pytest.fixture
def temporal() -> FailingTemporalClient:
    return FailingTemporalClient()


@pytest.fixture
def matcher(
    session_factory: async_sessionmaker[AsyncSession],
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    tracing: TraceHarness,
    trigger_case: TriggerTelemetryCase,
) -> TriggerMatcher:
    del trigger_case
    return TriggerMatcher(
        session_factory,
        cast(Any, temporal),
        metrics=trigger_metrics.metrics,
        tracer=tracing.tracer,
        cache_ttl_seconds=0.0,
    )


def metric_sum(owned: TriggerMetrics, name: str, **labels: str) -> float:
    data = owned.reader.get_metrics_data()
    if data is None:
        return 0.0
    return sum(
        float(point.value)
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
        for point in metric.data.data_points
        if dict(point.attributes) == labels
    )


def serialized_metrics(owned: TriggerMetrics) -> str:
    data = owned.reader.get_metrics_data()
    if data is None:
        return "[]"
    return json.dumps(
        [
            {
                "name": metric.name,
                "description": metric.description,
                "unit": metric.unit,
                "resource": dict(resource_metrics.resource.attributes),
                "points": [
                    {"attributes": dict(point.attributes), "value": point.value}
                    for point in metric.data.data_points
                ],
            }
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        ],
        default=str,
        sort_keys=True,
    )


async def invocation_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[TriggerInvocation]:
    async with session_factory() as session:
        return list(
            await session.scalars(
                select(TriggerInvocation).order_by(
                    TriggerInvocation.created_at, TriggerInvocation.id
                )
            )
        )


def dispatch_spans(tracing: TraceHarness) -> list[ReadableSpan]:
    return [
        span for span in tracing.exporter.get_finished_spans() if span.name == "trigger.dispatch"
    ]


def test_matcher_requires_explicit_metrics_and_tracer(
    session_factory: async_sessionmaker[AsyncSession],
    temporal: FailingTemporalClient,
    tracing: TraceHarness,
) -> None:
    with pytest.raises(TypeError):
        TriggerMatcher(session_factory, cast(Any, temporal))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        TriggerMatcher(session_factory, cast(Any, temporal), tracer=tracing.tracer)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        TriggerMatcher(session_factory, cast(Any, temporal), metrics=noop_metrics())  # type: ignore[call-arg]


async def test_handle_event_counts_started_duplicate_and_failed_after_commits(
    matcher: TriggerMatcher,
    trigger_case: TriggerTelemetryCase,
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    started_event = trigger_case.event_for("github", external_id="issue-1")
    await matcher.handle_event(started_event)
    await matcher.handle_event(started_event)
    await matcher.handle_event(
        trigger_case.event_for_missing_agent("github", external_id="issue-2")
    )

    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started", "duplicate", "failed"]
    assert rows[2].error == "invalid_request"
    assert len(temporal.calls) == 1
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="github", outcome="started"
        )
        == 1
    )
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_invocations_total",
            connector_type="github",
            outcome="duplicate",
        )
        == 1
    )
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="github", outcome="failed"
        )
        == 1
    )
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_failures_total",
            connector_type="github",
            failure_class="target",
        )
        == 1
    )
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_failures_total",
            connector_type="github",
            failure_class="dispatch",
        )
        == 0
    )


async def test_two_failed_deliveries_create_and_count_two_fresh_invocations(
    matcher: TriggerMatcher,
    trigger_case: TriggerTelemetryCase,
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    tracing: TraceHarness,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    temporal.fail_with = ConnectionError("provider-body-canary")
    for _delivery in range(2):
        with pytest.raises(ConnectionError) as caught:
            await matcher.handle_event(trigger_case.event_for("linear", external_id="issue-3"))
        assert caught.value is temporal.fail_with

    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["failed", "failed"]
    assert [row.error for row in rows] == ["upstream_unavailable", "upstream_unavailable"]
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="linear", outcome="started"
        )
        == 2
    )
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="linear", outcome="failed"
        )
        == 2
    )
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_failures_total",
            connector_type="linear",
            failure_class="dispatch",
        )
        == 2
    )
    spans = dispatch_spans(tracing)
    assert len(spans) == 2
    for span in spans:
        assert span.kind.name == "CLIENT"
        assert dict(span.attributes or {}) == {
            "jhin.connector_type": "linear",
            "jhin.outcome": "failed",
            "error.type": "ConnectionError",
            "error.code": "upstream_unavailable",
        }
        assert span.status.status_code.name == "ERROR"
        assert span.status.description is None
        assert span.events == ()
    payload = "\n".join(
        (*(serialized_span(span) for span in spans), serialized_metrics(trigger_metrics))
    )
    assert "provider-body-canary" not in payload


async def test_trigger_dispatch_span_is_a_consumer_child_without_product_material(
    matcher: TriggerMatcher,
    trigger_case: TriggerTelemetryCase,
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    tracing: TraceHarness,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = trigger_case.event_for("github", external_id="private-external-id-canary")
    with tracing.tracer.start_as_current_span("nats.consume") as parent:
        await matcher.handle_event(event)

    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started"]
    spans = dispatch_spans(tracing)
    assert len(spans) == 1
    span = spans[0]
    assert span.parent is not None
    assert span.parent.span_id == parent.get_span_context().span_id
    assert span.kind.name == "CLIENT"
    assert dict(span.attributes or {}) == {
        "jhin.connector_type": "github",
        "jhin.outcome": "started",
    }
    assert span.status.status_code.name == "UNSET"
    payload = "\n".join((serialized_span(span), serialized_metrics(trigger_metrics)))
    canaries = {
        str(trigger_case.valid_trigger_id),
        str(trigger_case.workspace_id),
        str(event.event_id),
        str(event.correlation_id),
        rows[0].idempotency_key,
        str(rows[0].id),
        rows[0].workflow_id or "<workflow-id>",
        temporal.calls[0]["id"],
        "private-external-id-canary",
        "private-trigger-title-canary",
        "private-trigger-description-canary",
        "private-trigger-url-canary",
        "private-valid-trigger-name-canary",
    }
    for canary in canaries:
        assert canary not in payload


async def test_crash_after_started_commit_is_reconciled_without_a_second_start(
    session_factory: async_sessionmaker[AsyncSession],
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    tracing: TraceHarness,
    trigger_case: TriggerTelemetryCase,
) -> None:
    crash = RuntimeError("crash-after-started-commit")
    barrier_identities: list[UUID] = []
    armed = True

    async def barrier(invocation_id: UUID) -> None:
        barrier_identities.append(invocation_id)
        if armed:
            raise crash

    matcher = TriggerMatcher(
        session_factory,
        cast(Any, temporal),
        metrics=trigger_metrics.metrics,
        tracer=tracing.tracer,
        cache_ttl_seconds=0.0,
        pre_dispatch_barrier=barrier,
    )
    event = trigger_case.event_for("github", external_id="issue-crash")
    with pytest.raises(RuntimeError) as caught:
        await matcher.handle_event(event)
    assert caught.value is crash
    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started"]
    assert rows[0].task_id is None
    assert temporal.calls == []
    assert barrier_identities == [rows[0].id]

    armed = False
    # Two redeliveries: the first reconciles, the second finds the same
    # deterministic workflow already started and treats that as success.
    await matcher.handle_event(event)
    await matcher.handle_event(event)

    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started", "duplicate", "duplicate"]
    assert len(temporal.calls) == 1
    assert temporal.calls[0]["id"] == rows[0].workflow_id
    assert temporal.calls[0]["params"].invocation_id == str(rows[0].id)
    assert barrier_identities == [rows[0].id, rows[0].id, rows[0].id]
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="github", outcome="started"
        )
        == 1
    )
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_invocations_total",
            connector_type="github",
            outcome="duplicate",
        )
        == 2
    )
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="github", outcome="failed"
        )
        == 0
    )
    assert [dict(span.attributes or {})["jhin.outcome"] for span in dispatch_spans(tracing)] == [
        "started",
        "started",
    ]


async def test_started_row_with_linked_task_suppresses_redelivery(
    matcher: TriggerMatcher,
    trigger_case: TriggerTelemetryCase,
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    tracing: TraceHarness,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = trigger_case.event_for("github", external_id="issue-linked")
    await matcher.handle_event(event)
    async with session_factory() as session:
        row = (await invocation_rows(session_factory))[0]
        linked = await session.get(TriggerInvocation, row.id)
        assert linked is not None
        linked.task_id = new_uuid7()
        await session.commit()
    temporal.calls.clear()
    tracing.exporter.clear()

    await matcher.handle_event(event)

    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started", "duplicate"]
    assert temporal.calls == []
    assert dispatch_spans(tracing) == []
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_invocations_total",
            connector_type="github",
            outcome="duplicate",
        )
        == 1
    )
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="github", outcome="started"
        )
        == 1
    )


async def test_dispatch_failure_after_a_fresh_authority_follows_a_prior_failure(
    matcher: TriggerMatcher,
    trigger_case: TriggerTelemetryCase,
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event = trigger_case.event_for("vercel", external_id="issue-recover")
    temporal.fail_with = TimeoutError("temporal-timeout-canary")
    with pytest.raises(TimeoutError):
        await matcher.handle_event(event)
    temporal.fail_with = None

    await matcher.handle_event(event)

    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["failed", "started"]
    assert len(temporal.calls) == 1
    assert temporal.calls[0]["params"].invocation_id == str(rows[1].id)
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="vercel", outcome="started"
        )
        == 2
    )
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="vercel", outcome="failed"
        )
        == 1
    )
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_failures_total",
            connector_type="vercel",
            failure_class="dispatch",
        )
        == 1
    )


async def test_secondary_failure_update_errors_reraise_the_exact_temporal_error(
    matcher: TriggerMatcher,
    trigger_case: TriggerTelemetryCase,
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = ConnectionError("temporal-down-canary")
    temporal.fail_with = original
    real_factory = matcher._session_factory

    def broken_factory(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("secondary-update-failure")

    def poison_sessions() -> None:
        matcher._session_factory = cast(Any, broken_factory)

    temporal.before_start = poison_sessions
    with pytest.raises(ConnectionError) as caught:
        await matcher.handle_event(trigger_case.event_for("github", external_id="issue-secondary"))
    assert caught.value is original
    assert caught.value.__context__ is None

    matcher._session_factory = real_factory
    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started"]
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="github", outcome="failed"
        )
        == 0
    )
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_failures_total",
            connector_type="github",
            failure_class="dispatch",
        )
        == 0
    )


class HostileTriggerMetrics:
    is_noop = False

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.counters: list[str] = []

    def counter(self, name: str) -> NoReturn:
        self.counters.append(name)
        raise self.failure

    def histogram(self, name: str) -> NoReturn:
        raise AssertionError(name)

    def set_observable(self, name: str, observations: object) -> NoReturn:
        raise AssertionError((name, observations))


class HostileTracer:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls = 0

    def start_as_current_span(self, *_args: object, **_kwargs: object) -> NoReturn:
        self.calls += 1
        raise self.failure


@pytest.mark.parametrize("failure_type", [RuntimeError, ValueError, KeyError])
async def test_hostile_metrics_and_tracer_are_diagnostic_only(
    session_factory: async_sessionmaker[AsyncSession],
    temporal: FailingTemporalClient,
    trigger_case: TriggerTelemetryCase,
    failure_type: type[Exception],
) -> None:
    metrics = HostileTriggerMetrics(failure_type("hostile-metrics"))
    tracer = HostileTracer(failure_type("hostile-tracer"))
    matcher = TriggerMatcher(
        session_factory,
        cast(Any, temporal),
        metrics=cast(JhinMetrics, metrics),
        tracer=cast(Tracer, tracer),
        cache_ttl_seconds=0.0,
    )
    event = trigger_case.event_for("github", external_id="issue-hostile")
    await matcher.handle_event(event)
    await matcher.handle_event(event)
    await matcher.handle_event(trigger_case.event_for_missing_agent("github", external_id="x"))
    temporal.fail_with = ConnectionError("temporal-down")
    with pytest.raises(ConnectionError):
        await matcher.handle_event(trigger_case.event_for("linear", external_id="issue-hostile-2"))

    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started", "duplicate", "failed", "failed"]
    assert [row.error for row in rows[2:]] == ["invalid_request", "upstream_unavailable"]
    assert len(temporal.calls) == 1
    # started, reconciled duplicate (Temporal rejects the duplicate id), failed
    assert tracer.calls == 3
    assert metrics.counters == [
        "trigger_invocations_total",  # started
        "trigger_invocations_total",  # duplicate
        "trigger_invocations_total",  # failed (target)
        "trigger_failures_total",
        "trigger_invocations_total",  # started
        "trigger_invocations_total",  # failed (dispatch)
        "trigger_failures_total",
    ]


async def test_primary_metric_cancellation_propagates_after_started_commit_and_reconciles(
    session_factory: async_sessionmaker[AsyncSession],
    temporal: FailingTemporalClient,
    trigger_metrics: TriggerMetrics,
    tracing: TraceHarness,
    trigger_case: TriggerTelemetryCase,
) -> None:
    cancellation = asyncio.CancelledError("owned-metric-cancellation")
    hostile = TriggerMatcher(
        session_factory,
        cast(Any, temporal),
        metrics=cast(JhinMetrics, HostileTriggerMetrics(cancellation)),
        tracer=tracing.tracer,
        cache_ttl_seconds=0.0,
    )
    event = trigger_case.event_for("github", external_id="issue-cancel")
    with pytest.raises(asyncio.CancelledError) as caught:
        await hostile.handle_event(event)
    assert caught.value is cancellation
    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started"]
    assert temporal.calls == []

    healthy = TriggerMatcher(
        session_factory,
        cast(Any, temporal),
        metrics=trigger_metrics.metrics,
        tracer=tracing.tracer,
        cache_ttl_seconds=0.0,
    )
    await healthy.handle_event(event)
    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["started", "duplicate"]
    assert len(temporal.calls) == 1
    assert temporal.calls[0]["id"] == rows[0].workflow_id
    assert (
        metric_sum(
            trigger_metrics, "trigger_invocations_total", connector_type="github", outcome="started"
        )
        == 0
    )
    assert (
        metric_sum(
            trigger_metrics,
            "trigger_invocations_total",
            connector_type="github",
            outcome="duplicate",
        )
        == 1
    )


async def test_secondary_metric_cancellation_never_replaces_the_temporal_error(
    session_factory: async_sessionmaker[AsyncSession],
    temporal: FailingTemporalClient,
    tracing: TraceHarness,
    trigger_case: TriggerTelemetryCase,
) -> None:
    class StartedThenCancelMetrics(HostileTriggerMetrics):
        def __init__(self) -> None:
            super().__init__(asyncio.CancelledError("secondary-metric-cancellation"))
            self.armed = False

        def counter(self, name: str) -> Any:
            self.counters.append(name)
            if self.armed:
                raise self.failure
            return SimpleNamespace(add=lambda *_args, **_kwargs: None)

    metrics = StartedThenCancelMetrics()
    matcher = TriggerMatcher(
        session_factory,
        cast(Any, temporal),
        metrics=cast(JhinMetrics, metrics),
        tracer=tracing.tracer,
        cache_ttl_seconds=0.0,
    )
    original = ConnectionError("temporal-down")
    temporal.fail_with = original

    def arm() -> None:
        metrics.armed = True

    temporal.before_start = arm
    with pytest.raises(ConnectionError) as caught:
        await matcher.handle_event(trigger_case.event_for("github", external_id="issue-sec"))
    assert caught.value is original
    rows = await invocation_rows(session_factory)
    assert [row.status for row in rows] == ["failed"]
    assert rows[0].error == "upstream_unavailable"
    assert metrics.counters == [
        "trigger_invocations_total",
        "trigger_invocations_total",
        "trigger_failures_total",
    ]
