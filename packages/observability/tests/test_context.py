"""Trace-only propagation and bounded span-attribute tests."""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Iterator
from typing import Any, TypeGuard, get_type_hints

import pytest
import structlog
from opentelemetry import baggage, trace
from opentelemetry.context import Context, attach, detach
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

import jhin_observability
import jhin_observability.context as context_module
from jhin_observability import (
    ObservabilityNotInitializedError,
    SafeError,
    SafeErrorCode,
    bind_context,
    extract_trace_context,
    inject_trace_headers,
    noop_tracer,
    normalize_span_attributes,
    record_span_error,
    safe_span,
)
from jhin_observability.registry import (
    DB_TABLE_VALUES,
    SPAN_ATTRIBUTE_VALUES,
    SPAN_NAMES,
    TEMPORAL_ACTIVITY_NAMES,
    TEMPORAL_ACTIVITY_TYPE_VALUES,
    TEMPORAL_WORKFLOW_TYPE_VALUES,
)


class HostileContextId:
    def __getattribute__(self, name: str) -> Any:
        if name == "__class__":
            raise AssertionError("validator executed hostile __class__")
        return object.__getattribute__(self, name)

    def __str__(self) -> str:
        raise AssertionError("validator coerced hostile context ID")


class ContextIdStringSubclass(str):
    def __str__(self) -> str:
        raise AssertionError("validator coerced string subclass")


class PollutingTracePropagator:
    def __init__(self) -> None:
        self.received_carrier: dict[str, str] | None = None

    def inject(self, carrier: dict[str, str]) -> None:
        assert carrier == {}
        self.received_carrier = carrier
        carrier.update(
            {
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                "tracestate": "vendor=value",
                "baggage": "password=propagator-canary",
                "TraceParent": "mixed-case-propagator-canary",
                "x-propagator-extra": "propagator-canary",
            }
        )


@pytest.fixture
def exporting_tracer() -> Iterator[tuple[Tracer, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield provider.get_tracer("test.context"), exporter
    finally:
        provider.shutdown()


def test_traceparent_is_validated_and_baggage_is_discarded() -> None:
    ctx = extract_trace_context(
        {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "vendor=value",
            "baggage": "workspace_id=attacker,metric_label=attacker",
        }
    )
    span_context = trace.get_current_span(ctx).get_span_context()
    assert format(span_context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert baggage.get_all(context=ctx) == {}


def test_invalid_traceparent_produces_no_remote_parent() -> None:
    ctx = extract_trace_context({"traceparent": "00-invalid-invalid-01"})
    assert trace.get_current_span(ctx).get_span_context().is_valid is False


def test_bind_context_clears_all_values_after_exit() -> None:
    with bind_context(request_id="r", correlation_id="c", task_id="t", run_id="run"):
        assert structlog.contextvars.get_contextvars()["request_id"] == "r"
    assert structlog.contextvars.get_contextvars() == {}


def test_bind_context_rejects_unbounded_identifiers() -> None:
    with (
        pytest.raises(ValueError, match="invalid request_id"),
        bind_context(request_id="https://customer.example.test/secret"),
    ):
        pass


def test_safe_context_id_is_package_public_with_exact_type_guard_signature() -> None:
    validator = jhin_observability.is_safe_context_id
    assert validator is context_module.is_safe_context_id
    signature = inspect.signature(validator)
    assert tuple(signature.parameters) == ("value",)
    assert signature.parameters["value"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["value"].default is inspect.Parameter.empty
    assert list(get_type_hints(validator)) == ["value", "return"]
    assert get_type_hints(validator) == {
        "value": object,
        "return": TypeGuard[str],
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        7,
        "",
        "https://customer.example.test/id",
        '{"password":"payload-canary"}',
        "x" * 129,
        " leading",
        "trailing ",
        "line\nbreak",
        "tab\tvalue",
        "nul\x00value",
    ],
)
def test_safe_context_id_rejects_nonexact_and_unsafe_values_without_user_code(
    value: object,
) -> None:
    validator = context_module.is_safe_context_id
    assert validator(value) is False


def test_safe_context_id_does_not_execute_hostile_class_or_string_subclass_code() -> None:
    validator = context_module.is_safe_context_id
    assert validator(HostileContextId()) is False
    assert validator(ContextIdStringSubclass("valid-looking-id")) is False


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "request-123._:value",
        "a" + "x" * 127,
    ],
)
def test_safe_context_id_accepts_only_valid_builtin_string_boundaries(value: str) -> None:
    validator = context_module.is_safe_context_id
    candidate: object = value
    assert validator(candidate) is True


def test_mixed_case_trace_carrier_is_rebuilt_with_canonical_keys() -> None:
    parent = extract_trace_context(
        {
            "TraceParent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "TraceState": "vendor=value",
            "BaGgAgE": "password=carrier-canary",
        }
    )
    token = attach(parent)
    try:
        output = inject_trace_headers(
            {
                "X-Safe": "kept",
                "TRACEPARENT": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
                "traceState": "attacker=value",
                "BAGGAGE": "secret=carrier-canary",
            }
        )
    finally:
        detach(token)
    lowered = [key.lower() for key in output]
    assert output["X-Safe"] == "kept"
    assert lowered.count("traceparent") == 1
    assert lowered.count("tracestate") <= 1
    assert "baggage" not in lowered
    assert output["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert "carrier-canary" not in json.dumps(output)


def test_trace_injection_strips_every_case_variant_and_preserves_exact_order() -> None:
    parent = extract_trace_context(
        {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "vendor=value",
        }
    )
    headers = {
        "X-First": "first",
        "traceparent": "lower-stale",
        "TraceParent": "mixed-stale",
        "TRACEPARENT": "upper-stale",
        "tracestate": "lower-state-stale",
        "TraceState": "mixed-state-stale",
        "TRACESTATE": "upper-state-stale",
        "baggage": "lower-baggage-canary",
        "BaGgAgE": "mixed-baggage-canary",
        "BAGGAGE": "upper-baggage-canary",
        "X-Second": "second",
    }
    original = dict(headers)
    token = attach(parent)
    try:
        output = inject_trace_headers(headers)
    finally:
        detach(token)
    assert headers == original
    assert list(output) == ["X-First", "X-Second", "traceparent", "tracestate"]
    assert output == {
        "X-First": "first",
        "X-Second": "second",
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "vendor=value",
    }


@pytest.mark.parametrize(
    "current",
    [
        Context(),
        trace.set_span_in_context(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT), Context()),
    ],
    ids=["no-current-span", "invalid-current-context"],
)
def test_trace_injection_never_revives_stale_carriers_without_valid_current_span(
    current: Context,
) -> None:
    headers = {
        "Ordinary": "kept",
        "TRACEPARENT": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
        "TraceState": "attacker=value",
        "BAGGAGE": "secret=carrier-canary",
    }
    original = dict(headers)
    token = attach(current)
    try:
        output = inject_trace_headers(headers)
        empty_output = inject_trace_headers()
    finally:
        detach(token)
    assert headers == original
    assert output == {"Ordinary": "kept"}
    assert empty_output == {}


def test_trace_injection_uses_fresh_carrier_and_merges_only_canonical_trace_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    propagator = PollutingTracePropagator()
    monkeypatch.setattr(context_module, "TRACE_PROPAGATOR", propagator)
    headers = {
        "Safe-Header": "kept",
        "BAGGAGE": "secret=caller-canary",
    }
    original = dict(headers)
    output = inject_trace_headers(headers)
    assert headers == original
    assert propagator.received_carrier is not headers
    assert output == {
        "Safe-Header": "kept",
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "vendor=value",
    }


def test_safe_span_requires_runtime_unless_caller_explicitly_supplies_noop() -> None:
    with pytest.raises(ObservabilityNotInitializedError), safe_span("model.request"):
        pass
    with safe_span("model.request", tracer=noop_tracer()) as span:
        assert span.is_recording() is False


def test_single_span_registry_covers_exactly_every_registered_activity() -> None:
    activity_spans = {name for name in SPAN_NAMES if name.startswith("temporal.activity.")}
    assert activity_spans == {
        *(f"temporal.activity.{name}" for name in TEMPORAL_ACTIVITY_NAMES),
        "temporal.activity.other",
    }


def test_span_attribute_registries_are_immutable_and_complete() -> None:
    with pytest.raises(TypeError):
        SPAN_ATTRIBUTE_VALUES["jhin.outcome"] = frozenset({"attacker"})  # type: ignore[index]
    assert "service_instance_heartbeat" in DB_TABLE_VALUES
    assert frozenset((*TEMPORAL_ACTIVITY_NAMES, "other")) == TEMPORAL_ACTIVITY_TYPE_VALUES
    assert "TriggeredTaskWorkflow" in TEMPORAL_WORKFLOW_TYPE_VALUES


def test_subject_family_registry_is_exact_and_immutable() -> None:
    expected = frozenset(
        {
            "ingress",
            "task",
            "agent",
            "tool",
            "approval",
            "connector",
            "trigger",
            "workflow",
            "system",
            "dlq",
            "other",
        }
    )
    actual = SPAN_ATTRIBUTE_VALUES["jhin.subject_family"]
    assert actual == expected
    assert isinstance(actual, frozenset)
    assert "run" not in actual


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("http.request.method", "GET", "GET"),
        ("http.request.method", "TRACE", "other"),
        ("http.route", "/api/customers/acme", "other"),
        ("db.table", "service_instance_heartbeat", "service_instance_heartbeat"),
        ("db.table", "customer_canary", "other"),
        ("jhin.outcome", "completed", "completed"),
        ("jhin.outcome", "credential=span-canary", "other"),
        ("jhin.provider_type", "https://collector.example.test", "other"),
        ("jhin.provider_type", "collector.example.test", "other"),
        ("jhin.operation", '{"password":"payload-canary"}', "other"),
        ("jhin.connector_type", "customer-alphanumeric-canary", "other"),
        ("jhin.request_id", "request-123", "request-123"),
        ("http.response.status_code", 503, 503),
    ],
)
def test_span_attributes_are_normalized_per_key(
    key: str, value: str | int, expected: str | int
) -> None:
    assert normalize_span_attributes({key: value}) == {key: expected}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("jhin.request_id", ""),
        ("jhin.request_id", "x" * 129),
        ("http.response.status_code", True),
        ("http.response.status_code", -1),
        ("jhin.latency_ms", math.inf),
        ("jhin.retry_count", 1_000_000_001),
    ],
)
def test_invalid_id_and_numeric_span_values_are_omitted(
    key: str, value: str | bool | int | float
) -> None:
    assert normalize_span_attributes({key: value}) == {}


@pytest.mark.parametrize("key", ["url", "hostname", "payload", "error.type", "error.code"])
def test_unregistered_and_error_only_span_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="unregistered span attribute key"):
        normalize_span_attributes({key: "span-canary"})


def test_no_unsafe_canary_reaches_an_exported_span(
    exporting_tracer: tuple[Tracer, InMemorySpanExporter],
) -> None:
    tracer, exporter = exporting_tracer
    with safe_span(
        "connector.http",
        tracer=tracer,
        attributes={
            "jhin.connector_type": "credential=span-canary",
            "jhin.operation": "https://customer.example.test/span-canary",
            "jhin.outcome": "x" * 500,
            "jhin.provider_type": "collector.example.test",
            "jhin.tool_family": '{"password":"payload-canary"}',
            "jhin.risk": "customer-alphanumeric-canary",
            "jhin.request_id": "request-123",
        },
    ):
        pass
    attributes = dict(exporter.get_finished_spans()[0].attributes or {})
    serialized = json.dumps(attributes, sort_keys=True)
    assert attributes == {
        "jhin.connector_type": "other",
        "jhin.operation": "other",
        "jhin.outcome": "other",
        "jhin.provider_type": "other",
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.request_id": "request-123",
    }
    assert "span-canary" not in serialized
    assert "customer.example.test" not in serialized
    assert "collector.example.test" not in serialized
    assert "payload-canary" not in serialized
    assert "customer-alphanumeric-canary" not in serialized


def test_record_span_error_is_the_only_error_attribute_writer(
    exporting_tracer: tuple[Tracer, InMemorySpanExporter],
) -> None:
    tracer, exporter = exporting_tracer
    with safe_span("model.request", tracer=tracer) as span:
        record_span_error(
            span,
            SafeError(type="TimeoutError", code=SafeErrorCode.TIMEOUT),
        )
    finished = exporter.get_finished_spans()[0]
    assert finished.status.is_ok is False
    assert finished.attributes == {
        "error.type": "TimeoutError",
        "error.code": "timeout",
    }
