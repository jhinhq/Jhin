"""Trace-only propagation and safe span/context helpers."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TypeGuard
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace import NoOpTracerProvider, Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from jhin_observability.errors import SafeError
from jhin_observability.redaction import structural_redaction
from jhin_observability.registry import (
    SPAN_ATTRIBUTE_VALUES,
    SPAN_NAMES,
    AttributeValue,
    SpanName,
)

_TRACEPARENT_HEADER = "traceparent"
_TRACESTATE_HEADER = "tracestate"
_BAGGAGE_HEADER = "baggage"
_TRACE_CONTEXT_HEADERS = (_TRACEPARENT_HEADER, _TRACESTATE_HEADER)
TRACE_CARRIER_KEYS = frozenset((*_TRACE_CONTEXT_HEADERS, _BAGGAGE_HEADER))
TRACE_PROPAGATOR = TraceContextTextMapPropagator()

SPAN_ID_ATTRIBUTE_KEYS = frozenset(
    {
        "jhin.request_id",
        "jhin.correlation_id",
        "jhin.workspace_id",
        "jhin.task_id",
        "jhin.run_id",
        "jhin.job_id",
        "temporal.workflow_id",
        "temporal.run_id",
    }
)
SPAN_NUMERIC_ATTRIBUTE_KEYS = frozenset(
    {
        "http.response.status_code",
        "jhin.latency_ms",
        "jhin.retry_count",
        "temporal.attempt",
    }
)
SAFE_SPAN_ATTRIBUTE_KEYS = frozenset(
    {*SPAN_ATTRIBUTE_VALUES, *SPAN_ID_ATTRIBUTE_KEYS, *SPAN_NUMERIC_ATTRIBUTE_KEYS}
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_NOOP_TRACER = NoOpTracerProvider().get_tracer("jhin-observability.package-noop")


class _CaseInsensitiveGetter(Getter[Mapping[str, str]]):
    def get(self, carrier: Mapping[str, str], key: str) -> list[str] | None:
        value = carrier.get(key)
        return [value] if value is not None else None

    def keys(self, carrier: Mapping[str, str]) -> list[str]:
        return list(carrier)


_TRACE_GETTER = _CaseInsensitiveGetter()


def noop_tracer() -> Tracer:
    """Return the explicit package/seed/host no-op tracer."""
    return _NOOP_TRACER


def is_safe_context_id(value: object) -> TypeGuard[str]:
    """Return whether an exact built-in string satisfies the context ID grammar."""
    return type(value) is str and _ID_RE.fullmatch(value) is not None


def extract_trace_context(headers: Mapping[str, str]) -> Context:
    carrier: dict[str, str] = {}
    for key, value in headers.items():
        normalized = key.lower()
        if normalized in _TRACE_CONTEXT_HEADERS and normalized not in carrier:
            carrier[normalized] = value
    return TRACE_PROPAGATOR.extract(carrier=carrier, getter=_TRACE_GETTER)


def inject_trace_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    copied = {} if headers is None else dict(headers)
    preserved = {
        key: value for key, value in copied.items() if key.lower() not in TRACE_CARRIER_KEYS
    }
    injected: dict[str, str] = {}
    TRACE_PROPAGATOR.inject(carrier=injected)
    for key in _TRACE_CONTEXT_HEADERS:
        if key in injected:
            preserved[key] = injected[key]
    return preserved


def _structurally_unchanged(key: str, value: object) -> bool:
    redacted = structural_redaction({key: value})
    return isinstance(redacted, dict) and key in redacted and redacted[key] == value


def normalize_span_attributes(
    attributes: Mapping[str, AttributeValue] | None,
) -> dict[str, AttributeValue]:
    output: dict[str, AttributeValue] = {}
    for key, value in (attributes or {}).items():
        if key not in SAFE_SPAN_ATTRIBUTE_KEYS:
            raise ValueError(f"unregistered span attribute key: {key}")
        if key in SPAN_ID_ATTRIBUTE_KEYS:
            if is_safe_context_id(value) and _structurally_unchanged(key, value):
                output[key] = value
            continue
        if key in SPAN_NUMERIC_ATTRIBUTE_KEYS:
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and 0 <= value <= 1_000_000_000
                and _structurally_unchanged(key, value)
            ):
                output[key] = value
            continue
        allowed = SPAN_ATTRIBUTE_VALUES[key]
        output[key] = (
            value
            if isinstance(value, str) and _structurally_unchanged(key, value) and value in allowed
            else "other"
        )
    return output


@contextmanager
def bind_context(
    *,
    request_id: str | UUID | None = None,
    correlation_id: str | UUID | None = None,
    workspace_id: str | UUID | None = None,
    task_id: str | UUID | None = None,
    run_id: str | UUID | None = None,
) -> Iterator[None]:
    supplied = {
        "request_id": request_id,
        "correlation_id": correlation_id,
        "workspace_id": workspace_id,
        "task_id": task_id,
        "run_id": run_id,
    }
    values: dict[str, str] = {}
    for key, value in supplied.items():
        if value is None:
            continue
        rendered = str(value) if type(value) is UUID else value
        if not is_safe_context_id(rendered):
            raise ValueError(f"invalid {key}")
        values[key] = rendered
    tokens = structlog.contextvars.bind_contextvars(**values)
    span = trace.get_current_span()
    for key, value in values.items():
        if span.is_recording():
            span.set_attribute(f"jhin.{key}", value)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


@contextmanager
def safe_span(
    name: SpanName,
    *,
    tracer: Tracer | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, AttributeValue] | None = None,
    context: Context | None = None,
) -> Iterator[Span]:
    if name not in SPAN_NAMES:
        raise ValueError("unregistered span name")
    if tracer is None:
        from jhin_observability.bootstrap import get_runtime

        selected_tracer = get_runtime().tracer
    else:
        selected_tracer = tracer
    with selected_tracer.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=normalize_span_attributes(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


def record_span_error(span: Span, error: SafeError) -> None:
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("error.type", error.type)
    span.set_attribute("error.code", error.code.value)


__all__ = [
    "SAFE_SPAN_ATTRIBUTE_KEYS",
    "SPAN_ID_ATTRIBUTE_KEYS",
    "SPAN_NUMERIC_ATTRIBUTE_KEYS",
    "bind_context",
    "extract_trace_context",
    "inject_trace_headers",
    "is_safe_context_id",
    "noop_tracer",
    "normalize_span_attributes",
    "record_span_error",
    "safe_span",
]
