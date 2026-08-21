"""Trace-only propagation and bounded span-attribute tests."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Iterator
from contextlib import suppress
from contextvars import Context as ContextVarsContext
from contextvars import Token
from types import TracebackType
from typing import Any, TypeGuard, cast, get_type_hints

import pytest
import structlog
from opentelemetry import baggage, trace
from opentelemetry.context import Context, attach, detach, get_current
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


def _traceback_tail(traceback: TracebackType | None) -> TracebackType | None:
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    return traceback


def _restore_test_context(tokens: list[Token[Context]]) -> None:
    for token in reversed(tokens):
        with suppress(ValueError):
            detach(token)


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


def test_trace_carrier_keys_are_context_public_exact_and_immutable() -> None:
    assert "TRACE_CARRIER_KEYS" in context_module.__all__
    assert type(context_module.TRACE_CARRIER_KEYS) is frozenset
    assert frozenset({"traceparent", "tracestate", "baggage"}) == context_module.TRACE_CARRIER_KEYS


def test_trace_carrier_keys_package_export_is_the_context_authority() -> None:
    package_export = getattr(jhin_observability, "TRACE_CARRIER_KEYS", None)
    assert package_export is context_module.TRACE_CARRIER_KEYS
    assert "TRACE_CARRIER_KEYS" in jhin_observability.__all__


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


def test_stream_is_an_exact_registered_model_operation() -> None:
    assert SPAN_ATTRIBUTE_VALUES["jhin.operation"] == frozenset(
        {
            "generate",
            "stream",
            "verify",
            "issue_comment_create",
            "execute_read",
            "execute_write",
            "submit",
            "cancel",
            "status",
            "cleanup",
            "other",
        }
    )
    assert normalize_span_attributes({"jhin.operation": "stream"}) == {"jhin.operation": "stream"}


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


def test_set_span_attributes_is_public_normalizes_then_contains_backend_failure() -> None:
    setter = getattr(context_module, "set_span_attributes", None)
    assert callable(setter)
    assert getattr(jhin_observability, "set_span_attributes", None) is setter
    assert "set_span_attributes" in context_module.__all__
    assert "set_span_attributes" in jhin_observability.__all__

    class HostileSpan:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def set_attribute(self, key: str, value: object) -> None:
            self.calls.append((key, value))
            if key == "jhin.outcome":
                raise RuntimeError("late-attribute-backend-private-canary")

    span = HostileSpan()
    cast(Any, setter)(
        cast(Any, span),
        {
            "jhin.outcome": "attacker-selected-arbitrary-outcome",
            "jhin.operation": "generate",
        },
    )
    assert span.calls == [
        ("jhin.outcome", "other"),
        ("jhin.operation", "generate"),
    ]

    span.calls.clear()
    with pytest.raises(ValueError, match="unregistered span attribute key"):
        cast(Any, setter)(
            cast(Any, span),
            {
                "jhin.operation": "generate",
                "private.payload": "late-attribute-private-canary",
            },
        )
    assert span.calls == []


@pytest.mark.parametrize(
    "failure_seam",
    ["start", "enter", "end", "exit", "detach"],
)
def test_safe_span_contains_start_enter_exit_end_and_detach_failures(
    failure_seam: str,
) -> None:
    entry_context = get_current()
    owned_context = Context({"safe-span-owned": failure_seam})
    telemetry_context = Context({"safe-span-telemetry": failure_seam})
    active_tokens: list[Token[Context]] = []
    product_result = object()
    product_calls = 0

    def attach_for_test(context: Context) -> Token[Context]:
        token = attach(context)
        active_tokens.append(token)
        return token

    class HostileSpan:
        def is_recording(self) -> bool:
            return True

        def end(self) -> None:
            if failure_seam == "end":
                attach_for_test(telemetry_context)
                raise RuntimeError("span-end-backend-private-canary")

    class HostileManager:
        def __init__(self) -> None:
            self.token: Token[Context] | None = None
            self.span = HostileSpan()

        def __enter__(self) -> HostileSpan:
            self.token = attach_for_test(owned_context)
            if failure_seam == "enter":
                raise RuntimeError("span-enter-backend-private-canary")
            return self.span

        def __exit__(self, *_args: object) -> None:
            if failure_seam == "end":
                self.span.end()
            if failure_seam == "exit":
                attach_for_test(telemetry_context)
                raise RuntimeError("span-exit-backend-private-canary")
            if failure_seam == "detach":
                attach_for_test(telemetry_context)
                raise RuntimeError("span-detach-backend-private-canary")
            if self.token is not None:
                detach(self.token)
                active_tokens.remove(self.token)
                self.token = None

    manager = HostileManager()

    class HostileTracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> HostileManager:
            if failure_seam == "start":
                attach_for_test(telemetry_context)
                raise RuntimeError("span-start-backend-private-canary")
            return manager

    def invoke_product() -> object:
        nonlocal product_calls
        with safe_span("model.request", tracer=cast(Any, HostileTracer())) as span:
            product_calls += 1
            if failure_seam in {"start", "enter"}:
                assert span.is_recording() is False
                assert get_current() is entry_context
            else:
                assert cast(Any, span) is manager.span
                assert get_current() is owned_context
            return product_result

    try:
        assert invoke_product() is product_result
        assert product_calls == 1
        assert get_current() is entry_context
    finally:
        _restore_test_context(active_tokens)

    assert get_current() is entry_context


def test_safe_span_does_not_reattach_body_leaked_foreign_context() -> None:
    isolated_context = ContextVarsContext()
    observed_contexts: dict[str, Context] = {}

    def invoke_product() -> None:
        entry_context = get_current()
        owned_context = Context({"safe-span-owned": "leaked-foreign"})
        foreign_context = Context({"safe-span-foreign": "leaked-foreign"})

        class Manager:
            owned_token: Token[Context] | None = None

            def __enter__(self) -> trace.NonRecordingSpan:
                self.owned_token = attach(owned_context)
                return trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)

            def __exit__(self, *_args: object) -> None:
                assert self.owned_token is not None
                detach(self.owned_token)

        class Tracer:
            def start_as_current_span(self, *_args: object, **_kwargs: object) -> Manager:
                return Manager()

        with safe_span("model.request", tracer=cast(Any, Tracer())):
            attach(foreign_context)
            assert get_current() is foreign_context

        observed_contexts["entry"] = entry_context
        assert get_current() is entry_context

    isolated_context.run(invoke_product)
    assert isolated_context.run(get_current) is observed_contexts["entry"]


def test_safe_span_real_tracer_restores_preexisting_foreign_entry_context(
    exporting_tracer: tuple[Tracer, InMemorySpanExporter],
) -> None:
    tracer, exporter = exporting_tracer
    entry_context = get_current()
    foreign_context = extract_trace_context(
        {"traceparent": "00-11111111111111111111111111111111-2222222222222222-01"}
    )
    foreign_span_context = trace.get_current_span(foreign_context).get_span_context()
    foreign_token = attach(foreign_context)
    telemetry_span_id: int | None = None
    try:
        with safe_span("model.request", tracer=tracer) as span:
            telemetry_span_id = span.get_span_context().span_id
            assert span.is_recording() is True
            assert get_current() is not foreign_context
        assert telemetry_span_id is not None
        assert get_current() is foreign_context
        assert trace.get_current_span().get_span_context() == foreign_span_context
        assert trace.get_current_span().get_span_context().span_id != telemetry_span_id
    finally:
        detach(foreign_token)

    assert get_current() is entry_context
    assert trace.get_current_span().get_span_context().span_id != telemetry_span_id
    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    assert finished[0].context.span_id == telemetry_span_id


def test_safe_span_real_tracer_restores_entry_after_body_local_foreign_context(
    exporting_tracer: tuple[Tracer, InMemorySpanExporter],
) -> None:
    tracer, exporter = exporting_tracer
    entry_context = get_current()
    temporary_foreign = extract_trace_context(
        {"traceparent": "00-33333333333333333333333333333333-4444444444444444-01"}
    )
    temporary_span_context = trace.get_current_span(temporary_foreign).get_span_context()
    telemetry_span_id: int | None = None

    with safe_span("model.request", tracer=tracer) as span:
        telemetry_context = get_current()
        telemetry_span_id = span.get_span_context().span_id
        temporary_token = attach(temporary_foreign)
        try:
            assert get_current() is temporary_foreign
            assert trace.get_current_span().get_span_context() == temporary_span_context
        finally:
            detach(temporary_token)
        assert get_current() is telemetry_context
        assert trace.get_current_span() is span
        assert span.is_recording() is True

    assert telemetry_span_id is not None
    assert get_current() is entry_context
    assert trace.get_current_span().get_span_context().span_id != telemetry_span_id
    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    assert finished[0].context.span_id == telemetry_span_id


def test_safe_span_rejects_invalid_schema_before_tracer_manager_body_or_context_touch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touches: list[str] = []

    class CanaryManager:
        def __enter__(self) -> Any:
            touches.append("manager-enter")
            return type("CanarySpan", (), {"is_recording": lambda self: True})()

        def __exit__(self, *_args: object) -> None:
            touches.append("manager-exit")

    class CanaryTracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> CanaryManager:
            touches.append("tracer-start")
            return CanaryManager()

    def touch_context() -> Context:
        touches.append("context")
        return Context()

    monkeypatch.setattr(context_module, "_current_otel_context", touch_context)
    tracer = cast(Any, CanaryTracer())
    cases = [
        (
            cast(Any, "private.unregistered.span"),
            {"jhin.operation": "generate"},
            "unregistered span name",
        ),
        (
            "model.request",
            {
                "jhin.operation": "generate",
                "private.payload": "invalid-schema-private-canary",
            },
            "unregistered span attribute key: private.payload",
        ),
    ]
    for name, attributes, message in cases:
        with (
            pytest.raises(ValueError, match=message),
            safe_span(name, tracer=tracer, attributes=cast(Any, attributes)),
        ):
            touches.append("body")
        assert touches == []


@pytest.mark.parametrize("product_outcome", ["success", "ordinary", "cancelled"])
def test_safe_span_preserves_result_exception_traceback_and_cancellation_exactly(
    product_outcome: str,
) -> None:
    entry_context = get_current()
    owned_context = Context({"safe-span-authority": product_outcome})
    telemetry_context = Context({"safe-span-cleanup": product_outcome})
    active_tokens: list[Token[Context]] = []
    product_result = object()
    product_error: BaseException
    if product_outcome == "cancelled":
        product_error = asyncio.CancelledError("product-cancellation-authority")
    else:
        product_error = RuntimeError("product-failure-authority")
    product_traceback: TracebackType | None = None
    product_calls = 0
    exit_arguments: tuple[object, object, object] | None = None

    def attach_for_test(context: Context) -> Token[Context]:
        token = attach(context)
        active_tokens.append(token)
        return token

    class HostileManager:
        def __enter__(self) -> Any:
            attach_for_test(owned_context)
            return type("RecordingSpan", (), {"is_recording": lambda self: True})()

        def __exit__(self, *args: object) -> None:
            nonlocal exit_arguments
            exit_arguments = cast(tuple[object, object, object], args)
            attach_for_test(telemetry_context)
            raise RuntimeError("span-cleanup-backend-private-canary")

    class HostileTracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> HostileManager:
            return HostileManager()

    def invoke_product() -> object:
        nonlocal product_calls, product_traceback
        with safe_span("model.request", tracer=cast(Any, HostileTracer())):
            product_calls += 1
            if product_outcome == "success":
                return product_result
            try:
                raise product_error
            except BaseException as error:
                product_traceback = _traceback_tail(error.__traceback__)
                raise

    caught: BaseException | None = None
    result: object | None = None
    try:
        try:
            result = invoke_product()
        except BaseException as error:
            caught = error
        assert product_calls == 1
        assert get_current() is entry_context
        if product_outcome == "success":
            assert caught is None
            assert result is product_result
            assert exit_arguments == (None, None, None)
        else:
            assert caught is product_error
            assert _traceback_tail(caught.__traceback__) is product_traceback
            assert exit_arguments is not None
            assert exit_arguments[1] is product_error
            assert _traceback_tail(cast(TracebackType, exit_arguments[2])) is product_traceback
    finally:
        _restore_test_context(active_tokens)


@pytest.mark.parametrize("product_outcome", ["success", "ordinary", "cancelled"])
def test_safe_span_exit_cancellation_respects_active_product_authority(
    product_outcome: str,
) -> None:
    cleanup_cancellation = asyncio.CancelledError("span-exit-cancellation")
    product_error: BaseException
    if product_outcome == "cancelled":
        product_error = asyncio.CancelledError("product-cancellation-authority")
    else:
        product_error = RuntimeError("product-failure-authority")
    product_traceback: TracebackType | None = None
    exit_arguments: tuple[object, object, object] | None = None
    product_result = object()
    product_calls = 0

    class CancellingManager:
        def __enter__(self) -> Any:
            return type("RecordingSpan", (), {"is_recording": lambda self: True})()

        def __exit__(self, *args: object) -> None:
            nonlocal exit_arguments
            exit_arguments = cast(tuple[object, object, object], args)
            raise cleanup_cancellation

    class CancellingTracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> CancellingManager:
            return CancellingManager()

    def invoke_product() -> object:
        nonlocal product_calls, product_traceback
        with safe_span("model.request", tracer=cast(Any, CancellingTracer())):
            product_calls += 1
            if product_outcome == "success":
                return product_result
            try:
                raise product_error
            except BaseException as error:
                product_traceback = _traceback_tail(error.__traceback__)
                raise

    caught: BaseException | None = None
    try:
        invoke_product()
    except BaseException as error:
        caught = error

    assert product_calls == 1
    assert exit_arguments is not None
    if product_outcome == "success":
        assert caught is cleanup_cancellation
        assert exit_arguments == (None, None, None)
    else:
        assert caught is product_error
        assert _traceback_tail(caught.__traceback__) is product_traceback
        assert exit_arguments[1] is product_error
        assert _traceback_tail(cast(TracebackType, exit_arguments[2])) is product_traceback


@pytest.mark.parametrize("failure_seam", ["start", "enter", "exit"])
@pytest.mark.parametrize("signal_type", [KeyboardInterrupt, SystemExit])
def test_safe_span_does_not_swallow_keyboard_interrupt_or_system_exit(
    failure_seam: str,
    signal_type: type[BaseException],
) -> None:
    entry_context = get_current()
    signal = signal_type("backend-process-authority")
    active_tokens: list[Token[Context]] = []
    product_calls = 0

    class Manager:
        def __enter__(self) -> Any:
            active_tokens.append(attach(Context({"base-exception": failure_seam})))
            if failure_seam == "enter":
                raise signal
            return type("RecordingSpan", (), {"is_recording": lambda self: True})()

        def __exit__(self, *_args: object) -> None:
            if failure_seam == "exit":
                raise signal

    class Tracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> Manager:
            if failure_seam == "start":
                raise signal
            return Manager()

    try:
        with (
            pytest.raises(signal_type) as caught,
            safe_span("model.request", tracer=cast(Any, Tracer())),
        ):
            product_calls += 1
        assert caught.value is signal
        assert product_calls == (1 if failure_seam == "exit" else 0)
        assert get_current() is entry_context
    finally:
        _restore_test_context(active_tokens)


@pytest.mark.parametrize("failure_seam", ["status", "error.type", "error.code"])
def test_record_span_error_contains_each_backend_failure_without_text(
    failure_seam: str,
) -> None:
    calls: list[tuple[str, object]] = []

    class HostileSpan:
        def set_status(self, status: object) -> None:
            calls.append(("status", status))
            if failure_seam == "status":
                raise RuntimeError("error-status-backend-private-canary")

        def set_attribute(self, key: str, value: object) -> None:
            calls.append((key, value))
            if failure_seam == key:
                raise RuntimeError("error-attribute-backend-private-canary")

    record_span_error(
        cast(Any, HostileSpan()),
        SafeError(type="TimeoutError", code=SafeErrorCode.TIMEOUT),
    )
    assert [name for name, _value in calls] == ["status", "error.type", "error.code"]
    status = cast(Any, calls[0][1])
    assert status.status_code.name == "ERROR"
    assert status.description is None
    assert calls[1:] == [("error.type", "TimeoutError"), ("error.code", "timeout")]
    assert "private-canary" not in repr(calls)


@pytest.mark.parametrize("product_outcome", ["success", "ordinary", "cancelled"])
def test_bind_context_contains_span_failure_and_always_restores_tokens(
    product_outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    product_calls = 0
    product_result = object()
    product_error: BaseException
    if product_outcome == "cancelled":
        product_error = asyncio.CancelledError("bound-product-cancellation")
    else:
        product_error = RuntimeError("bound-product-failure")
    product_traceback: TracebackType | None = None

    class HostileSpan:
        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            calls.append((key, value))
            raise RuntimeError("bound-span-backend-private-canary")

    monkeypatch.setattr(trace, "get_current_span", lambda: HostileSpan())
    before = structlog.contextvars.get_contextvars()

    def invoke_product() -> object:
        nonlocal product_calls, product_traceback
        with bind_context(request_id="request-bound", task_id="task-bound"):
            product_calls += 1
            assert structlog.contextvars.get_contextvars() == {
                **before,
                "request_id": "request-bound",
                "task_id": "task-bound",
            }
            if product_outcome == "success":
                return product_result
            try:
                raise product_error
            except BaseException as error:
                product_traceback = _traceback_tail(error.__traceback__)
                raise

    caught: BaseException | None = None
    result: object | None = None
    try:
        result = invoke_product()
    except BaseException as error:
        caught = error

    assert product_calls == 1
    assert calls == [
        ("jhin.request_id", "request-bound"),
        ("jhin.task_id", "task-bound"),
    ]
    assert structlog.contextvars.get_contextvars() == before
    if product_outcome == "success":
        assert caught is None
        assert result is product_result
    else:
        assert caught is product_error
        assert _traceback_tail(caught.__traceback__) is product_traceback
