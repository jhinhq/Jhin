"""Event-worker trace ownership, lag, and lifecycle contracts."""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
import structlog
from nats.js.api import PubAck
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

from jhin_event_worker.normalizer import IngressNormalizer
from jhin_event_worker.processor import EventProcessor
from jhin_events.envelope import EventEnvelope, EventSource
from jhin_observability import JhinMetrics, MetricName, Observation, noop_metrics
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


async def test_runtime_is_owned_when_logging_configuration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = worker_main()
    events: list[str] = []
    runtime = FakeRuntime(events)
    logging_error = RuntimeError("logging-config-canary")
    monkeypatch.setattr(main, "Settings", lambda: FakeSettings(events))
    monkeypatch.setattr(main, "service_version", lambda _: "test-version", raising=False)
    monkeypatch.setattr(
        main,
        "initialize_observability",
        lambda _config: (events.append("runtime.initialize"), runtime)[1],
        raising=False,
    )

    def fail_logging(**_kwargs: object) -> None:
        events.append("logging.configure")
        raise logging_error

    monkeypatch.setattr(main, "configure_json_logging", fail_logging)

    with pytest.raises(RuntimeError) as excinfo:
        await main.main()
    assert excinfo.value is logging_error
    assert events == [
        "settings",
        "settings.observability_config",
        "runtime.initialize",
        "logging.configure",
        "runtime.shutdown",
    ]


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

    async def temporal(settings: object) -> object:
        del settings
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
        del args, kwargs
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
    monkeypatch.setattr(main, "configure_json_logging", lambda **_kwargs: None)
    monkeypatch.setattr(main, "connect_with_retry", lambda _settings: _async_value(client))
    monkeypatch.setattr(main, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(main, "temporal_with_retry", lambda _settings: _async_value(object()))
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
    monkeypatch.setattr(main, "configure_json_logging", lambda **_kwargs: None)
    monkeypatch.setattr(main, "connect_with_retry", lambda _settings: _async_value(client))
    monkeypatch.setattr(main, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(main, "temporal_with_retry", lambda _settings: _async_value(object()))
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
    monkeypatch.setattr(main, "configure_json_logging", lambda **_kwargs: None)
    monkeypatch.setattr(main, "connect_with_retry", lambda _settings: _async_value(client))
    monkeypatch.setattr(main, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(main, "temporal_with_retry", lambda _settings: _async_value(object()))
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

    async def temporal(_settings: object) -> object:
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
    monkeypatch.setattr(main, "temporal_with_retry", lambda _settings: _async_value(object()))
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
