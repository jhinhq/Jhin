"""Trace-safe, bounded NATS transport contracts."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, cast
from uuid import UUID

import pytest
import structlog
from nats.aio.client import Client as NatsClient
from nats.js.api import PubAck
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

from jhin_events.envelope import EventEnvelope, EventSource
from jhin_events.publisher import EventPublisher
from jhin_events.subjects import EVENT_DOMAINS
from jhin_observability import (
    SPAN_ATTRIBUTE_VALUES,
    TRACE_CARRIER_KEYS,
    bind_context,
    extract_trace_context,
    noop_tracer,
)

VALID_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
VALID_TRACESTATE = "vendor=value"
KNOWN_CORRELATION = UUID("018f0000-0000-7000-8000-000000000001")
KNOWN_EVENT = UUID("018f0000-0000-7000-8000-000000000002")
KNOWN_WORKSPACE = "018f0000-0000-7000-8000-000000000003"


def telemetry() -> ModuleType:
    """Import at test execution time so missing Task 5 behavior is valid RED."""
    return importlib.import_module("jhin_events.telemetry")


def event_envelope(
    *,
    event_type: str = "task.created",
    workspace_id: str = KNOWN_WORKSPACE,
    correlation_id: UUID = KNOWN_CORRELATION,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=KNOWN_EVENT,
        event_type=event_type,
        workspace_id=workspace_id,
        correlation_id=correlation_id,
        source=EventSource(type="system"),
        data={},
    )


@dataclass(frozen=True)
class PublishedMessage:
    subject: str
    payload: bytes
    headers: dict[str, str]


class RecordingJetStream:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.published: list[PublishedMessage] = []
        self.publish_span_ids: list[int] = []

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        *,
        headers: Mapping[str, str] | None = None,
    ) -> PubAck:
        self.publish_span_ids.append(trace.get_current_span().get_span_context().span_id)
        self.published.append(PublishedMessage(subject, payload, dict(headers or {})))
        if self.failure is not None:
            raise self.failure
        return cast(PubAck, object())


class CapturingNatsClient(NatsClient):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[bytes] = []

    async def _send_command(self, cmd: bytes, priority: bool = False) -> None:
        assert priority is False
        self.commands.append(cmd)


class NatsEncodingJetStream:
    """Exercise the resolved nats-py HPUB encoder without a broker."""

    def __init__(self) -> None:
        self.calls = 0
        self.headers: dict[str, str] = {}
        self.header_block = b""
        self.header_size = 0
        self.total_size = 0

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        *,
        headers: Mapping[str, str] | None = None,
    ) -> PubAck:
        self.calls += 1
        self.headers = dict(headers or {})
        client = CapturingNatsClient()
        await client._send_publish(
            subject,
            "",
            payload,
            len(payload),
            self.headers,
        )
        command = client.commands[-1]
        command_line, encoded = command.split(b"\r\n", 1)
        fields = command_line.split()
        self.header_size = int(fields[-2])
        self.total_size = int(fields[-1])
        self.header_block = encoded[: self.header_size]
        assert len(self.header_block) == self.header_size
        assert self.total_size == self.header_size + len(payload)
        return cast(PubAck, object())


class BrokenSpanManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> Any:
        self.events.append("enter")
        raise RuntimeError("span setup failed")

    def __exit__(self, *_args: Any) -> None:
        self.events.append("exit")
        return None


class RecordingMessage:
    def __init__(
        self,
        *,
        subject: str,
        data: bytes = b"{}",
        headers: Mapping[str, str] | None = None,
        nak_failure: BaseException | None = None,
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = dict(headers or {})
        self.nak_failure = nak_failure
        self.acked = 0
        self.naks = 0
        self.ack_span_ids: list[int] = []
        self.nak_span_ids: list[int] = []

    async def ack(self) -> None:
        self.acked += 1
        self.ack_span_ids.append(trace.get_current_span().get_span_context().span_id)

    async def nak(self, *, delay: int) -> None:
        assert delay == 2
        self.naks += 1
        self.nak_span_ids.append(trace.get_current_span().get_span_context().span_id)
        if self.nak_failure is not None:
            raise self.nak_failure


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
                "service.name": "events-test",
                "service.version": "test",
                "deployment.environment.name": "test",
            }
        )
    )
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    harness = TraceHarness(provider, provider.get_tracer("events-tests"), exporter)
    try:
        yield harness
    finally:
        provider.shutdown()


def spans_named(tracing: TraceHarness, name: str) -> list[ReadableSpan]:
    return [span for span in tracing.exporter.get_finished_spans() if span.name == name]


def serialized_span(span: ReadableSpan) -> str:
    return json.dumps(
        {
            "name": span.name,
            "context": {
                "trace_id": format(span.context.trace_id, "032x") if span.context else None,
                "span_id": format(span.context.span_id, "016x") if span.context else None,
            },
            "parent": {
                "trace_id": format(span.parent.trace_id, "032x") if span.parent else None,
                "span_id": format(span.parent.span_id, "016x") if span.parent else None,
            },
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


def _span_id_from_traceparent(value: str) -> int:
    return int(value.split("-")[2], 16)


def _assert_empty_contexts() -> None:
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()


def test_subject_registry_and_classifier_have_one_exact_authority() -> None:
    module = telemetry()
    assert set(SPAN_ATTRIBUTE_VALUES["jhin.subject_family"]) == (
        set(EVENT_DOMAINS) | {"ingress", "dlq", "other"}
    )
    for domain in EVENT_DOMAINS:
        assert module.classify_subject(f"jhin.v1.workspace-1.{domain}.entity.changed") == (
            "EVENTS",
            domain,
        )


def test_transport_consumes_the_one_public_trace_carrier_authority() -> None:
    module = telemetry()
    assert module.TRACE_CARRIER_KEYS is TRACE_CARRIER_KEYS
    assert not hasattr(module, "_TRACE_CARRIER_KEYS")
    assert module.classify_subject("jhin.v1.workspace-1.ingress.github.issue.updated") == (
        "INGRESS",
        "ingress",
    )
    assert module.classify_subject("jhin.dlq.ingress") == ("DLQ", "dlq")
    assert module.classify_subject("jhin.dlq.events") == ("DLQ", "dlq")


@pytest.mark.parametrize(
    "subject",
    [
        "",
        "jhin.v1",
        "jhin.v1..task.created",
        "jhin.v1.workspace-1.task",
        "jhin.v1.workspace-1.task.",
        "jhin.v1.workspace-1.ingress.github",
        "jhin.v1.workspace-1.ingress..created",
        "jhin.v1.workspace-1.audit.created",
        "jhin.v1.workspace-1.run.created",
        "jhin.v1.workspace-1.task.created.extra*",
        "jhin.dlq.ingress.extra",
        "jhin.dlq.other",
    ],
)
def test_subject_classifier_rejects_every_noncanonical_shape_without_echo(
    subject: str,
) -> None:
    module = telemetry()
    with pytest.raises(ValueError) as excinfo:
        module.classify_subject(subject)
    assert str(excinfo.value) in {
        "unsupported Jhin subject",
        "unsupported Jhin subject family",
    }
    if subject:
        assert subject not in str(excinfo.value)


def test_stream_subject_mismatch_is_closed() -> None:
    module = telemetry()
    subject = "jhin.v1.workspace-subject-canary.task.created"
    with pytest.raises(ValueError, match=r"^stream/subject mismatch$") as excinfo:
        module.validate_stream_subject("INGRESS", subject)
    assert "workspace-subject-canary" not in str(excinfo.value)


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": "secret"},
        {"AUTHORIZATION": "secret"},
        {"x-api-key": "secret"},
        {"X-API-KEY": "secret"},
        {"Cookie": "secret"},
        {"COOKIE": "secret"},
        {"PASSWORD": "secret"},
        {"SECRET": "secret"},
        {"TOKEN": "secret"},
        {"X-PRIVATE-KEY": "secret"},
        {"X-DSN": "secret"},
        {"bad name": "value"},
        {"-bad": "value"},
        {"x\rname": "value"},
        {"x": "value\nnext"},
        {"x": "value\x00next"},
        {"x": "\ud800"},
        {"x" * 65: "value"},
        {"x": "v" * 1_025},
        {"X-Test": "one", "x-test": "two"},
        {cast(Any, 7): "value"},
        {"x": cast(Any, 7)},
    ],
)
async def test_invalid_nats_headers_fail_closed_before_transport(
    headers: Mapping[Any, Any],
) -> None:
    module = telemetry()
    js = RecordingJetStream()
    original = dict(headers)
    with pytest.raises(module.UnsafeNatsHeaderError) as excinfo:
        await module.publish_jetstream(
            js,
            "jhin.v1.workspace-1.task.created",
            b"payload-canary",
            headers=headers,
            stream="EVENTS",
            tracer=noop_tracer(),
        )
    assert str(excinfo.value) == "invalid NATS header"
    assert js.published == []
    assert dict(headers) == original
    assert not any(str(value) in str(excinfo.value) for value in headers.values())


async def test_header_count_name_and_value_caps_are_exact() -> None:
    module = telemetry()
    subject = "jhin.v1.workspace-1.task.created"

    at_count = {f"h{index}": "v" for index in range(32)}
    js = RecordingJetStream()
    await module.publish_jetstream(
        js, subject, b"", headers=at_count, stream="EVENTS", tracer=noop_tracer()
    )
    assert len(js.published[0].headers) == 32

    over_count = {f"h{index}": "v" for index in range(33)}
    with pytest.raises(module.UnsafeNatsHeaderError):
        await module.publish_jetstream(
            RecordingJetStream(),
            subject,
            b"",
            headers=over_count,
            stream="EVENTS",
            tracer=noop_tracer(),
        )


async def test_resolved_nats_wire_budget_includes_injected_carriers_and_dedupe(
    tracing: TraceHarness,
) -> None:
    exact = {f"h{index}": "v" * 1_024 for index in range(7)}
    exact["h7"] = "v" * 817
    exact.update(
        {
            "TraceParent": "stale-parent",
            "TRACESTATE": "stale=state",
            "BAGGAGE": "private-canary=do-not-forward",
            "nats-msg-id": "attacker-controlled-id",
        }
    )
    exact_original = dict(exact)
    parent = extract_trace_context(
        {"traceparent": VALID_TRACEPARENT, "tracestate": VALID_TRACESTATE}
    )
    envelope = event_envelope()

    js = NatsEncodingJetStream()
    with tracing.tracer.start_as_current_span("test.parent", context=parent):
        await EventPublisher(js, tracer=tracing.tracer).publish(
            envelope,
            headers=exact,
        )
    assert exact == exact_original
    assert js.calls == 1
    assert js.header_size == 8_192
    assert js.total_size == 8_192 + len(envelope.to_bytes())
    normalized = [key.lower() for key in js.headers]
    assert len(normalized) == len(set(normalized))
    assert set(js.headers) == {
        *(f"h{index}" for index in range(8)),
        "Nats-Msg-Id",
        "traceparent",
        "tracestate",
    }
    assert js.header_block.count(b"Nats-Msg-Id: ") == 1
    assert js.header_block.count(b"traceparent: ") == 1
    assert js.header_block.count(b"tracestate: ") == 1
    assert b"baggage: " not in js.header_block.lower()
    assert b"stale-parent" not in js.header_block
    assert b"attacker-controlled-id" not in js.header_block
    assert b"private-canary" not in js.header_block

    over = dict(exact)
    over["h7"] += "v"
    over_original = dict(over)
    fallback = NatsEncodingJetStream()
    with tracing.tracer.start_as_current_span("test.parent.over", context=parent):
        await EventPublisher(fallback, tracer=tracing.tracer).publish(
            event_envelope(),
            headers=over,
        )
    assert over == over_original
    assert fallback.calls == 1
    assert fallback.header_size == 8_097
    assert set(fallback.headers) == {
        *(f"h{index}" for index in range(8)),
        "Nats-Msg-Id",
    }
    assert fallback.header_block.count(b"Nats-Msg-Id: ") == 1
    assert b"traceparent: " not in fallback.header_block.lower()
    assert b"tracestate: " not in fallback.header_block.lower()
    assert b"baggage: " not in fallback.header_block.lower()


async def test_injected_carriers_respect_the_exact_32_header_cap(
    tracing: TraceHarness,
) -> None:
    parent = extract_trace_context(
        {"traceparent": VALID_TRACEPARENT, "tracestate": VALID_TRACESTATE}
    )
    at_cap = {f"h{index}": "v" for index in range(29)}
    at_cap_original = dict(at_cap)
    at_cap_js = NatsEncodingJetStream()
    with tracing.tracer.start_as_current_span("test.parent.count", context=parent):
        await EventPublisher(at_cap_js, tracer=tracing.tracer).publish(
            event_envelope(),
            headers=at_cap,
        )
    assert at_cap == at_cap_original
    assert len(at_cap_js.headers) == 32
    assert sum(key.lower() == "nats-msg-id" for key in at_cap_js.headers) == 1
    assert sum(key.lower() == "traceparent" for key in at_cap_js.headers) == 1
    assert sum(key.lower() == "tracestate" for key in at_cap_js.headers) == 1

    over_cap = {f"h{index}": "v" for index in range(30)}
    over_cap_original = dict(over_cap)
    fallback_js = NatsEncodingJetStream()
    with tracing.tracer.start_as_current_span("test.parent.count.over", context=parent):
        await EventPublisher(fallback_js, tracer=tracing.tracer).publish(
            event_envelope(),
            headers=over_cap,
        )
    assert over_cap == over_cap_original
    assert len(fallback_js.headers) == 31
    assert sum(key.lower() == "nats-msg-id" for key in fallback_js.headers) == 1
    assert all(key.lower() not in TRACE_CARRIER_KEYS for key in fallback_js.headers)


async def test_publisher_rebuilds_trace_carrier_and_preserves_input_mapping(
    tracing: TraceHarness,
) -> None:
    supplied = {
        "safe-header": "safe",
        "TraceParent": "stale",
        "TRACESTATE": "stale=value",
        "BaGgAgE": "workspace_id=attacker",
        "nats-msg-id": "attacker-id",
    }
    original = dict(supplied)
    js = RecordingJetStream()
    parent = extract_trace_context(
        {"traceparent": VALID_TRACEPARENT, "tracestate": VALID_TRACESTATE}
    )

    with tracing.tracer.start_as_current_span("test.parent", context=parent):
        await EventPublisher(js, tracer=tracing.tracer).publish(event_envelope(), headers=supplied)

    assert supplied == original
    assert len(js.published) == 1
    headers = js.published[0].headers
    assert headers["safe-header"] == "safe"
    assert headers["Nats-Msg-Id"] == str(KNOWN_EVENT)
    assert headers["tracestate"] == VALID_TRACESTATE
    assert {key.lower() for key in headers}.isdisjoint({"baggage"})
    assert sum(key.lower() == "traceparent" for key in headers) == 1
    assert sum(key.lower() == "tracestate" for key in headers) == 1
    assert sum(key.lower() == "nats-msg-id" for key in headers) == 1
    producer = spans_named(tracing, "nats.publish")[0]
    assert _span_id_from_traceparent(headers["traceparent"]) == producer.context.span_id
    assert js.publish_span_ids == [producer.context.span_id]
    assert dict(producer.attributes or {}) == {
        "messaging.system": "nats",
        "jhin.stream": "EVENTS",
        "jhin.subject_family": "task",
        "jhin.outcome": "ok",
    }
    _assert_empty_contexts()


async def test_no_current_span_removes_every_stale_carrier() -> None:
    module = telemetry()
    js = RecordingJetStream()
    await module.publish_jetstream(
        js,
        "jhin.v1.workspace-1.task.created",
        b"",
        headers={
            "TRACEPARENT": VALID_TRACEPARENT,
            "TraceState": VALID_TRACESTATE,
            "BAGGAGE": "secret=value",
            "x-safe": "yes",
        },
        stream="EVENTS",
        tracer=noop_tracer(),
    )
    assert js.published[0].headers == {"x-safe": "yes"}


@pytest.mark.parametrize(
    "error_count",
    [True, -1, 1_001, 1.0, "1", object()],
)
async def test_dlq_error_count_is_exact_and_fails_before_publish(error_count: object) -> None:
    module = telemetry()
    js = RecordingJetStream()
    with pytest.raises(ValueError, match=r"^invalid DLQ error count$"):
        await module.publish_invalid_envelope_dlq(
            js,
            origin_stream="INGRESS",
            error_count=error_count,
            tracer=noop_tracer(),
        )
    assert js.published == []


@pytest.mark.parametrize("origin_stream", ["INGRESS", "EVENTS"])
async def test_dlq_document_is_exact_closed_and_traced(
    origin_stream: str,
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    js = RecordingJetStream()
    await module.publish_invalid_envelope_dlq(
        js,
        origin_stream=origin_stream,
        error_count=2,
        tracer=tracing.tracer,
    )
    assert len(js.published) == 1
    published = js.published[0]
    assert published.subject == f"jhin.dlq.{origin_stream.lower()}"
    assert json.loads(published.payload) == {
        "schema_version": 1,
        "reason": "invalid_envelope",
        "origin_stream": origin_stream,
        "error_count": 2,
    }
    span = spans_named(tracing, "nats.publish")[0]
    assert dict(span.attributes or {}) == {
        "messaging.system": "nats",
        "jhin.stream": "DLQ",
        "jhin.subject_family": "dlq",
        "jhin.outcome": "ok",
    }


class RecordingFailureLogger:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any], int, int]] = []

    def exception(self, event: str, **kwargs: Any) -> None:
        context = trace.get_current_span().get_span_context()
        self.calls.append((event, kwargs, context.trace_id, context.span_id))
        if self.fail:
            raise RuntimeError("logger instrumentation failed")


async def test_consumer_span_owns_handler_ack_parent_and_bound_correlation(
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    message = RecordingMessage(
        subject="jhin.v1.workspace-1.task.created",
        headers={
            "traceparent": VALID_TRACEPARENT,
            "baggage": "workspace_id=attacker",
        },
    )
    seen: dict[str, str] = {}

    async def handler(msg: RecordingMessage) -> None:
        with bind_context(correlation_id=KNOWN_CORRELATION):
            seen.update(structlog.contextvars.get_contextvars())
            await msg.ack()

    await module.dispatch_or_nak(
        message,
        stream="EVENTS",
        durable="event-worker",
        handler=handler,
        tracer=tracing.tracer,
    )

    assert message.acked == 1
    assert message.naks == 0
    span = spans_named(tracing, "nats.consume")[0]
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == "00f067aa0ba902b7"
    assert dict(span.attributes or {}) == {
        "messaging.system": "nats",
        "jhin.stream": "EVENTS",
        "jhin.consumer": "event-worker",
        "jhin.subject_family": "task",
        "jhin.correlation_id": str(KNOWN_CORRELATION),
        "jhin.outcome": "ok",
    }
    assert message.ack_span_ids == [span.context.span_id]
    assert seen == {"correlation_id": str(KNOWN_CORRELATION)}
    _assert_empty_contexts()


async def test_handler_failure_log_and_nak_remain_inside_one_span(
    tracing: TraceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = telemetry()
    logger = RecordingFailureLogger()
    monkeypatch.setattr(module, "logger", logger)
    message = RecordingMessage(subject="jhin.v1.workspace-1.task.created")
    calls = 0

    async def handler(_msg: RecordingMessage) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("handler-secret-canary")

    await module.dispatch_or_nak(
        message,
        stream="EVENTS",
        durable="event-worker",
        handler=handler,
        tracer=tracing.tracer,
    )

    assert calls == 1
    assert message.naks == 1
    span = spans_named(tracing, "nats.consume")[0]
    assert message.nak_span_ids == [span.context.span_id]
    assert dict(span.attributes or {})["jhin.outcome"] == "failed"
    assert span.status.status_code.name == "ERROR"
    assert dict(span.attributes or {})["error.code"] == "internal_error"
    assert dict(span.attributes or {})["error.type"] == "RuntimeError"
    assert logger.calls == [
        (
            "jetstream.consumer_handler_failed",
            {
                "stream": "EVENTS",
                "consumer": "event-worker",
                "error_type": "RuntimeError",
                "error_code": "internal_error",
            },
            span.context.trace_id,
            span.context.span_id,
        )
    ]
    assert "handler-secret-canary" not in serialized_span(span)
    _assert_empty_contexts()


async def test_nak_failure_preserves_the_original_handler_exception(
    tracing: TraceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = telemetry()
    original_recorder = module.record_span_error
    recorded_types: list[str] = []

    def record_error(span: Any, error: Any) -> None:
        recorded_types.append(error.type)
        original_recorder(span, error)

    monkeypatch.setattr(module, "record_span_error", record_error)
    original = RuntimeError("authoritative-handler-canary")
    message = RecordingMessage(
        subject="jhin.v1.workspace-1.task.created",
        nak_failure=ConnectionError("settlement-canary"),
    )

    async def handler(_msg: RecordingMessage) -> None:
        raise original

    with pytest.raises(RuntimeError) as excinfo:
        await module.dispatch_or_nak(
            message,
            stream="EVENTS",
            durable="event-worker",
            handler=handler,
            tracer=tracing.tracer,
        )
    assert excinfo.value is original
    assert excinfo.value.__cause__ is None
    assert message.naks == 1
    assert recorded_types == ["RuntimeError", "ConnectionError"]
    span = spans_named(tracing, "nats.consume")[0]
    rendered = serialized_span(span)
    assert "authoritative-handler-canary" not in rendered
    assert "settlement-canary" not in rendered
    _assert_empty_contexts()


async def test_consumer_settlement_cancellation_remains_authoritative(
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    message = RecordingMessage(
        subject="jhin.v1.workspace-1.task.created",
        nak_failure=asyncio.CancelledError(),
    )

    async def handler(_msg: RecordingMessage) -> None:
        raise RuntimeError("superseded-handler-canary")

    with pytest.raises(asyncio.CancelledError):
        await module.dispatch_or_nak(
            message,
            stream="EVENTS",
            durable="event-worker",
            handler=handler,
            tracer=tracing.tracer,
        )
    assert message.naks == 1
    span = spans_named(tracing, "nats.consume")[0]
    assert dict(span.attributes or {})["jhin.outcome"] == "cancelled"
    assert "superseded-handler-canary" not in serialized_span(span)
    _assert_empty_contexts()


async def test_consumer_cancellation_propagates_without_nak(
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    message = RecordingMessage(subject="jhin.v1.workspace-1.task.created")
    entered = asyncio.Event()

    async def handler(_msg: RecordingMessage) -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        module.dispatch_or_nak(
            message,
            stream="EVENTS",
            durable="event-worker",
            handler=handler,
            tracer=tracing.tracer,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert message.naks == 0
    span = spans_named(tracing, "nats.consume")[0]
    assert dict(span.attributes or {})["jhin.outcome"] == "cancelled"
    _assert_empty_contexts()


async def test_consumer_instrumentation_failures_do_not_skip_handler_or_nak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = telemetry()
    message = RecordingMessage(subject="jhin.v1.workspace-1.task.created")
    calls = 0
    span_events: list[str] = []

    def broken_span(*_args: Any, **_kwargs: Any) -> BrokenSpanManager:
        return BrokenSpanManager(span_events)

    def broken_extract(_headers: Mapping[str, str]) -> Any:
        raise RuntimeError("extract failed")

    async def handler(_msg: RecordingMessage) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("handler failed")

    monkeypatch.setattr(module, "safe_span", broken_span)
    monkeypatch.setattr(module, "extract_trace_context", broken_extract)
    monkeypatch.setattr(
        module,
        "record_span_error",
        lambda *_: (_ for _ in ()).throw(RuntimeError()),
    )
    monkeypatch.setattr(module, "logger", RecordingFailureLogger(fail=True))

    await module.dispatch_or_nak(
        message,
        stream="EVENTS",
        durable="event-worker",
        handler=handler,
        tracer=noop_tracer(),
    )
    assert calls == 1
    assert message.naks == 1
    assert span_events == ["enter", "exit"]
    _assert_empty_contexts()


async def test_publish_instrumentation_failure_is_fail_open_and_transport_is_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = telemetry()
    js = RecordingJetStream()
    span_events: list[str] = []

    def broken_span(*_args: Any, **_kwargs: Any) -> BrokenSpanManager:
        return BrokenSpanManager(span_events)

    monkeypatch.setattr(module, "safe_span", broken_span)
    await module.publish_jetstream(
        js,
        "jhin.v1.workspace-1.task.created",
        b"payload",
        headers={"safe-header": "safe"},
        message_id="id-1",
        stream="EVENTS",
        tracer=noop_tracer(),
    )
    assert len(js.published) == 1
    assert js.published[0].headers == {
        "safe-header": "safe",
        "Nats-Msg-Id": "id-1",
    }
    assert span_events == ["enter", "exit"]


async def test_publish_trace_injection_failure_closes_real_span_and_publishes_once(
    monkeypatch: pytest.MonkeyPatch,
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    js = RecordingJetStream()

    def broken_inject(_headers: Mapping[str, str] | None = None) -> dict[str, str]:
        raise RuntimeError("inject failed")

    monkeypatch.setattr(module, "inject_trace_headers", broken_inject)
    await module.publish_jetstream(
        js,
        "jhin.v1.workspace-1.task.created",
        b"payload",
        headers={"safe-header": "safe"},
        message_id="id-1",
        stream="EVENTS",
        tracer=tracing.tracer,
    )
    assert len(js.published) == 1
    assert js.published[0].headers == {
        "safe-header": "safe",
        "Nats-Msg-Id": "id-1",
    }
    span = spans_named(tracing, "nats.publish")[0]
    assert dict(span.attributes or {})["jhin.outcome"] == "ok"
    assert js.publish_span_ids == [span.context.span_id]
    _assert_empty_contexts()


async def test_transport_failure_remains_authoritative_and_is_not_duplicated(
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    transport_error = ConnectionError("transport-secret-canary")
    js = RecordingJetStream(failure=transport_error)
    with pytest.raises(ConnectionError) as excinfo:
        await module.publish_jetstream(
            js,
            "jhin.v1.workspace-1.task.created",
            b"raw-payload-canary",
            stream="EVENTS",
            tracer=tracing.tracer,
        )
    assert excinfo.value is transport_error
    assert len(js.published) == 1
    span = spans_named(tracing, "nats.publish")[0]
    assert dict(span.attributes or {})["jhin.outcome"] == "failed"
    assert dict(span.attributes or {})["error.code"] == "internal_error"
    rendered = serialized_span(span)
    assert "transport-secret-canary" not in rendered
    assert "raw-payload-canary" not in rendered
    _assert_empty_contexts()


async def test_malformed_carrier_creates_safe_consumer_root(
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    message = RecordingMessage(
        subject="jhin.v1.workspace-1.task.created",
        headers={"traceparent": "malformed", "tracestate": "malformed"},
    )

    async def handler(msg: RecordingMessage) -> None:
        await msg.ack()

    await module.dispatch_or_nak(
        message,
        stream="EVENTS",
        durable="event-worker",
        handler=handler,
        tracer=tracing.tracer,
    )
    span = spans_named(tracing, "nats.consume")[0]
    assert span.parent is None
    _assert_empty_contexts()


async def test_deterministic_two_hop_trace_has_exact_five_span_parent_graph(
    tracing: TraceHarness,
) -> None:
    module = telemetry()
    js = RecordingJetStream()
    remote = extract_trace_context(
        {"traceparent": VALID_TRACEPARENT, "tracestate": VALID_TRACESTATE}
    )

    with tracing.tracer.start_as_current_span("http.server.request", context=remote):
        await module.publish_jetstream(
            js,
            "jhin.v1.workspace-subject-canary.ingress.github.issue.updated",
            b"raw-payload-canary",
            headers={
                "BAGGAGE": "authorization=auth-canary",
                "x-safe-canary": "complete-header-value-canary",
            },
            message_id="ingress-id",
            stream="INGRESS",
            tracer=tracing.tracer,
        )

    ingress_published = js.published[0]
    ingress_message = RecordingMessage(
        subject=ingress_published.subject,
        data=ingress_published.payload,
        headers=ingress_published.headers,
    )

    async def normalize(msg: RecordingMessage) -> None:
        await module.publish_jetstream(
            js,
            "jhin.v1.workspace-subject-canary.task.created",
            b"normalized-payload-canary",
            message_id="event-id",
            stream="EVENTS",
            tracer=tracing.tracer,
        )
        await msg.ack()

    await module.dispatch_or_nak(
        ingress_message,
        stream="INGRESS",
        durable="event-worker-ingress",
        handler=normalize,
        tracer=tracing.tracer,
    )
    event_published = js.published[1]
    event_message = RecordingMessage(
        subject=event_published.subject,
        data=event_published.payload,
        headers=event_published.headers,
    )

    async def process(msg: RecordingMessage) -> None:
        await msg.ack()

    await module.dispatch_or_nak(
        event_message,
        stream="EVENTS",
        durable="event-worker",
        handler=process,
        tracer=tracing.tracer,
    )

    finished = list(tracing.exporter.get_finished_spans())
    assert len(finished) == 5
    server = next(span for span in finished if span.name == "http.server.request")
    producers = spans_named(tracing, "nats.publish")
    ingress_producer = next(
        span for span in producers if dict(span.attributes or {})["jhin.stream"] == "INGRESS"
    )
    events_producer = next(
        span for span in producers if dict(span.attributes or {})["jhin.stream"] == "EVENTS"
    )
    consumers = spans_named(tracing, "nats.consume")
    ingress_consumer = next(
        span for span in consumers if dict(span.attributes or {})["jhin.stream"] == "INGRESS"
    )
    events_consumer = next(
        span for span in consumers if dict(span.attributes or {})["jhin.stream"] == "EVENTS"
    )
    assert ingress_producer.parent is not None
    assert ingress_producer.parent.span_id == server.context.span_id
    assert ingress_consumer.parent is not None
    assert ingress_consumer.parent.span_id == ingress_producer.context.span_id
    assert events_producer.parent is not None
    assert events_producer.parent.span_id == ingress_consumer.context.span_id
    assert events_consumer.parent is not None
    assert events_consumer.parent.span_id == events_producer.context.span_id
    assert {span.context.trace_id for span in finished} == {server.context.trace_id}
    assert (
        _span_id_from_traceparent(ingress_published.headers["traceparent"])
        == ingress_producer.context.span_id
    )
    assert (
        _span_id_from_traceparent(event_published.headers["traceparent"])
        == events_producer.context.span_id
    )
    assert ingress_published.headers["tracestate"] == VALID_TRACESTATE
    assert event_published.headers["tracestate"] == VALID_TRACESTATE
    assert all("baggage" not in {key.lower() for key in item.headers} for item in js.published)
    rendered = "\n".join(serialized_span(span) for span in finished)
    for canary in (
        "workspace-subject-canary",
        "raw-payload-canary",
        "normalized-payload-canary",
        "x-safe-canary",
        "complete-header-value-canary",
        "authorization",
        "auth-canary",
    ):
        assert canary not in rendered
    _assert_empty_contexts()


def test_public_tracer_seams_are_keyword_only_and_explicit() -> None:
    module = telemetry()
    import inspect

    for function in (
        module.publish_jetstream,
        module.dispatch_or_nak,
    ):
        parameter = inspect.signature(function).parameters["tracer"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
