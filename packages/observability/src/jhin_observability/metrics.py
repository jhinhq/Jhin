"""Frozen, cardinality-safe metric instruments for Jhin services."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from opentelemetry.metrics import CallbackOptions, Meter
from opentelemetry.metrics import Observation as OTelObservation

from jhin_observability.registry import TEMPORAL_ACTIVITY_NAMES, MetricName

InstrumentKind = Literal["counter", "histogram", "gauge"]


class MetricLabelError(ValueError):
    """A metric name, label key, or label set is outside the frozen registry."""


@dataclass(frozen=True)
class MetricSpec:
    kind: InstrumentKind
    unit: str
    labels: frozenset[str]


def _spec(kind: InstrumentKind, unit: str, *labels: str) -> MetricSpec:
    return MetricSpec(kind, unit, frozenset(labels))


METRIC_SPECS: Mapping[MetricName, MetricSpec] = MappingProxyType(
    {
        "agent_runs_total": _spec("counter", "{run}", "service", "outcome"),
        "agent_run_duration_seconds": _spec("histogram", "s", "outcome"),
        "agent_run_failures_total": _spec("counter", "{failure}", "failure_class"),
        "model_requests_total": _spec("counter", "{request}", "provider_type", "outcome"),
        "model_tokens_total": _spec("counter", "{token}", "provider_type", "direction"),
        "model_cost_estimate": _spec("counter", "USD", "provider_type"),
        "tool_calls_total": _spec("counter", "{call}", "tool_family", "risk", "outcome"),
        "tool_call_failures_total": _spec("counter", "{failure}", "tool_family", "failure_class"),
        "trigger_invocations_total": _spec("counter", "{invocation}", "connector_type", "outcome"),
        "trigger_failures_total": _spec("counter", "{failure}", "connector_type", "failure_class"),
        "sandbox_jobs_total": _spec("counter", "{job}", "outcome", "network_policy"),
        "sandbox_job_duration_seconds": _spec("histogram", "s", "outcome"),
        "nats_consumer_lag": _spec("gauge", "{message}", "stream", "consumer"),
        "temporal_activity_failures": _spec(
            "counter", "{failure}", "task_queue", "activity", "failure_class"
        ),
        "connector_health": _spec("gauge", "1", "connector_type"),
        "connector_connections": _spec("gauge", "{connection}", "connector_type", "outcome"),
    }
)

ALLOWED_METRIC_LABELS = frozenset(
    {
        "service",
        "environment",
        "outcome",
        "failure_class",
        "provider_type",
        "connector_type",
        "tool_family",
        "risk",
        "network_policy",
        "stream",
        "consumer",
        "task_queue",
        "activity",
        "http_method",
        "http_route",
        "http_status_class",
        "direction",
    }
)
FORBIDDEN_IDENTIFIER_LABELS = frozenset(
    {
        "workspace_id",
        "user_id",
        "agent_id",
        "team_id",
        "task_id",
        "run_id",
        "event_id",
        "message_id",
        "connection_id",
        "approval_id",
        "tool_call_id",
        "sandbox_job_id",
        "request_id",
        "correlation_id",
        "trace_id",
        "url",
        "hostname",
        "repository",
        "project",
        "model_name",
    }
)
LABEL_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "service": frozenset(
            {
                "api",
                "agent-worker",
                "tool-worker",
                "event-worker",
                "workflow-worker",
                "sandbox-runner",
                "web",
            }
        ),
        "environment": frozenset({"dev", "test", "staging", "production"}),
        "outcome": frozenset(
            {
                "ok",
                "started",
                "completed",
                "failed",
                "cancelled",
                "timeout",
                "denied",
                "rejected",
                "duplicate",
                "execution_unknown",
                "healthy",
                "unhealthy",
                "other",
            }
        ),
        "failure_class": frozenset(
            {
                "authentication",
                "authorization",
                "validation",
                "rate_limit",
                "timeout",
                "transport",
                "dispatch",
                "target",
                "provider",
                "policy",
                "budget",
                "execution_unknown",
                "internal",
                "other",
            }
        ),
        "provider_type": frozenset(
            {"openai", "anthropic", "openrouter", "ollama", "openai_compatible", "other"}
        ),
        "connector_type": frozenset({"github", "linear", "vercel", "supabase", "cli", "other"}),
        "tool_family": frozenset(
            {"system", "organization", "github", "linear", "vercel", "supabase", "cli", "other"}
        ),
        "risk": frozenset({"read", "write", "elevated", "destructive", "other"}),
        "network_policy": frozenset({"none", "internet", "other"}),
        "stream": frozenset({"INGRESS", "EVENTS", "other"}),
        "consumer": frozenset({"event-worker-ingress", "event-worker", "other"}),
        "task_queue": frozenset(
            {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue", "other"}
        ),
        "activity": frozenset((*TEMPORAL_ACTIVITY_NAMES, "other")),
        "direction": frozenset({"input", "output", "cached"}),
        "http_method": frozenset(
            {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"}
        ),
        "http_status_class": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "other"}),
    }
)
ROUTE_LABEL_VALUES = frozenset({"/api/:path*", "other"})


def instrument_contracts() -> dict[str, tuple[InstrumentKind, str, set[str]]]:
    return {name: (spec.kind, spec.unit, set(spec.labels)) for name, spec in METRIC_SPECS.items()}


def _metric_spec(name: MetricName) -> MetricSpec:
    try:
        return METRIC_SPECS[name]
    except KeyError as exc:
        raise MetricLabelError("unregistered metric name") from exc


def normalize_labels(name: MetricName, labels: Mapping[str, str]) -> dict[str, str]:
    supplied = set(labels)
    unknown = supplied - ALLOWED_METRIC_LABELS
    if unknown:
        raise MetricLabelError(f"forbidden metric label: {sorted(unknown)[0]}")
    spec = _metric_spec(name)
    if supplied != set(spec.labels):
        missing = sorted(spec.labels - supplied)
        extra = sorted(supplied - spec.labels)
        raise MetricLabelError(f"metric label contract mismatch; missing={missing}; extra={extra}")
    normalized: dict[str, str] = {}
    for key in sorted(spec.labels):
        value = labels[key]
        if not isinstance(value, str):
            raise MetricLabelError(f"metric label {key} must be a string")
        allowed = ROUTE_LABEL_VALUES if key == "http_route" else LABEL_VALUES[key]
        normalized[key] = value if value in allowed else "other"
    return normalized


def _finite_nonnegative(value: int | float, *, instrument: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{instrument} measurement must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"{instrument} measurement must be finite and non-negative")
    return numeric


class AddInstrument(Protocol):
    def add(
        self,
        amount: int | float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        """Record one monotonic counter point."""


class RecordInstrument(Protocol):
    def record(
        self,
        amount: int | float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        """Record one histogram point."""


@dataclass(frozen=True)
class Observation:
    value: int | float
    attributes: Mapping[str, str]


class BoundCounter(Protocol):
    def add(self, amount: int | float, **labels: str) -> None:
        """Record through the validated metric-label boundary."""


class BoundHistogram(Protocol):
    def record(self, amount: int | float, **labels: str) -> None:
        """Record through the validated metric-label boundary."""


@dataclass(frozen=True)
class JhinMetrics:
    _counter_getter: Callable[[MetricName], BoundCounter]
    _histogram_getter: Callable[[MetricName], BoundHistogram]
    _observable_setter: Callable[[MetricName, Sequence[Observation]], None]
    is_noop: bool = False

    def counter(self, name: MetricName) -> BoundCounter:
        return self._counter_getter(name)

    def histogram(self, name: MetricName) -> BoundHistogram:
        return self._histogram_getter(name)

    def set_observable(self, name: MetricName, observations: Sequence[Observation]) -> None:
        self._observable_setter(name, observations)


@dataclass(frozen=True)
class _BoundCounter:
    name: MetricName
    instrument: AddInstrument

    def add(self, amount: int | float, **labels: str) -> None:
        numeric = _finite_nonnegative(amount, instrument=self.name)
        attributes = normalize_labels(self.name, labels)
        try:  # noqa: SIM105 - keep the backend-only exception boundary explicit
            self.instrument.add(numeric, attributes)
        except Exception:
            pass


@dataclass(frozen=True)
class _BoundHistogram:
    name: MetricName
    instrument: RecordInstrument

    def record(self, amount: int | float, **labels: str) -> None:
        numeric = _finite_nonnegative(amount, instrument=self.name)
        attributes = normalize_labels(self.name, labels)
        try:  # noqa: SIM105 - keep the backend-only exception boundary explicit
            self.instrument.record(numeric, attributes)
        except Exception:
            pass


class _ObservableState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[MetricName, tuple[Observation, ...]] = {}

    def replace(self, name: MetricName, values: Sequence[Observation]) -> None:
        spec = _metric_spec(name)
        if spec.kind != "gauge":
            raise MetricLabelError("set_observable requires a gauge")
        if len(values) > 128:
            raise MetricLabelError("observable metric exceeds 128 samples")

        normalized_values: list[Observation] = []
        identities: set[tuple[tuple[str, str], ...]] = set()
        for value in values:
            normalized_attributes = normalize_labels(name, value.attributes)
            identity = tuple((key, normalized_attributes[key]) for key in sorted(spec.labels))
            if identity in identities:
                raise MetricLabelError("duplicate normalized observable identity")
            identities.add(identity)
            normalized_values.append(
                Observation(
                    _finite_nonnegative(value.value, instrument=name),
                    normalized_attributes,
                )
            )
        normalized = tuple(normalized_values)

        with self._lock:
            self._values[name] = normalized

    def observe(self, name: MetricName) -> list[OTelObservation]:
        with self._lock:
            values = self._values.get(name, ())
        return [OTelObservation(value.value, attributes=dict(value.attributes)) for value in values]


def build_jhin_metrics(meter: Meter) -> JhinMetrics:
    counters = {
        name: _BoundCounter(name, meter.create_counter(name, unit=spec.unit))
        for name, spec in METRIC_SPECS.items()
        if spec.kind == "counter"
    }
    histograms = {
        name: _BoundHistogram(name, meter.create_histogram(name, unit=spec.unit))
        for name, spec in METRIC_SPECS.items()
        if spec.kind == "histogram"
    }
    state = _ObservableState()

    def callback(selected: MetricName) -> Callable[[CallbackOptions], Iterable[OTelObservation]]:
        def observe(_options: CallbackOptions) -> Iterable[OTelObservation]:
            return state.observe(selected)

        return observe

    for name, spec in METRIC_SPECS.items():
        if spec.kind == "gauge":
            meter.create_observable_gauge(
                name,
                callbacks=[callback(name)],
                unit=spec.unit,
            )

    def counter(name: MetricName) -> BoundCounter:
        try:
            return counters[name]
        except KeyError as exc:
            raise MetricLabelError("counter requested for non-counter metric") from exc

    def histogram(name: MetricName) -> BoundHistogram:
        try:
            return histograms[name]
        except KeyError as exc:
            raise MetricLabelError("histogram requested for non-histogram metric") from exc

    return JhinMetrics(counter, histogram, state.replace)


@dataclass(frozen=True)
class _ValidatedNoopCounter:
    name: MetricName

    def add(self, amount: int | float, **labels: str) -> None:
        _finite_nonnegative(amount, instrument=self.name)
        normalize_labels(self.name, labels)


@dataclass(frozen=True)
class _ValidatedNoopHistogram:
    name: MetricName

    def record(self, amount: int | float, **labels: str) -> None:
        _finite_nonnegative(amount, instrument=self.name)
        normalize_labels(self.name, labels)


_VALIDATED_NOOP_COUNTERS = {
    name: _ValidatedNoopCounter(name)
    for name, spec in METRIC_SPECS.items()
    if spec.kind == "counter"
}
_VALIDATED_NOOP_HISTOGRAMS = {
    name: _ValidatedNoopHistogram(name)
    for name, spec in METRIC_SPECS.items()
    if spec.kind == "histogram"
}
_VALIDATED_NOOP_STATE = _ObservableState()


def _noop_counter(name: MetricName) -> BoundCounter:
    try:
        return _VALIDATED_NOOP_COUNTERS[name]
    except KeyError as exc:
        raise MetricLabelError("counter requested for non-counter metric") from exc


def _noop_histogram(name: MetricName) -> BoundHistogram:
    try:
        return _VALIDATED_NOOP_HISTOGRAMS[name]
    except KeyError as exc:
        raise MetricLabelError("histogram requested for non-histogram metric") from exc


_VALIDATED_NOOP_METRICS = JhinMetrics(
    _noop_counter,
    _noop_histogram,
    _VALIDATED_NOOP_STATE.replace,
    is_noop=True,
)


def noop_metrics() -> JhinMetrics:
    return _VALIDATED_NOOP_METRICS


__all__ = [
    "ALLOWED_METRIC_LABELS",
    "FORBIDDEN_IDENTIFIER_LABELS",
    "LABEL_VALUES",
    "METRIC_SPECS",
    "ROUTE_LABEL_VALUES",
    "BoundCounter",
    "BoundHistogram",
    "InstrumentKind",
    "JhinMetrics",
    "MetricLabelError",
    "MetricName",
    "MetricSpec",
    "Observation",
    "build_jhin_metrics",
    "instrument_contracts",
    "noop_metrics",
    "normalize_labels",
]
