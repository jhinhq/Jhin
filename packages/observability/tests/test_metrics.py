"""Exact metric registry, validation, and cardinality tests."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, cast, get_args

import pytest
from opentelemetry.metrics import Meter
from opentelemetry.metrics import Observation as OTelObservation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import jhin_observability as observability_module
import jhin_observability.metrics as metrics_module
from jhin_observability.metrics import JhinMetrics, Observation
from jhin_observability.registry import MetricName as RegistryMetricName

EXPECTED = {
    "agent_runs_total": ("counter", "{run}", {"service", "outcome"}),
    "agent_run_duration_seconds": ("histogram", "s", {"outcome"}),
    "agent_run_failures_total": ("counter", "{failure}", {"failure_class"}),
    "model_requests_total": ("counter", "{request}", {"provider_type", "outcome"}),
    "model_tokens_total": ("counter", "{token}", {"provider_type", "direction"}),
    "model_cost_estimate": ("counter", "USD", {"provider_type"}),
    "tool_calls_total": ("counter", "{call}", {"tool_family", "risk", "outcome"}),
    "tool_call_failures_total": (
        "counter",
        "{failure}",
        {"tool_family", "failure_class"},
    ),
    "trigger_invocations_total": (
        "counter",
        "{invocation}",
        {"connector_type", "outcome"},
    ),
    "trigger_failures_total": (
        "counter",
        "{failure}",
        {"connector_type", "failure_class"},
    ),
    "sandbox_jobs_total": ("counter", "{job}", {"outcome", "network_policy"}),
    "sandbox_job_duration_seconds": ("histogram", "s", {"outcome"}),
    "nats_consumer_lag": ("gauge", "{message}", {"stream", "consumer"}),
    "temporal_activity_failures": (
        "counter",
        "{failure}",
        {"task_queue", "activity", "failure_class"},
    ),
    "connector_health": ("gauge", "1", {"connector_type"}),
    "connector_connections": (
        "gauge",
        "{connection}",
        {"connector_type", "outcome"},
    ),
}

EXPECTED_ALLOWED_LABELS = frozenset(
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
EXPECTED_FORBIDDEN_LABELS = frozenset(
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
EXPECTED_LABEL_VALUES = {
    "service": {
        "api",
        "agent-worker",
        "tool-worker",
        "event-worker",
        "workflow-worker",
        "sandbox-runner",
        "web",
    },
    "environment": {"dev", "test", "staging", "production"},
    "outcome": {
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
    },
    "failure_class": {
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
    },
    "provider_type": {
        "openai",
        "anthropic",
        "openrouter",
        "ollama",
        "openai_compatible",
        "other",
    },
    "connector_type": {"github", "linear", "vercel", "supabase", "cli", "other"},
    "tool_family": {
        "system",
        "organization",
        "github",
        "linear",
        "vercel",
        "supabase",
        "cli",
        "other",
    },
    "risk": {"read", "write", "elevated", "destructive", "other"},
    "network_policy": {"none", "internet", "other"},
    "stream": {"INGRESS", "EVENTS", "other"},
    "consumer": {"event-worker-ingress", "event-worker", "other"},
    "task_queue": {
        "jhin-workflow-queue",
        "jhin-agent-queue",
        "jhin-tool-queue",
        "other",
    },
    "activity": {
        "reason_agent_step",
        "commit_agent_step",
        "commit_approval_projection",
        "resolve_advertised_tools",
        "execute_bound_tool",
        "resolve_bound_tool_approval",
        "sync_external_tool",
        "cleanup_run_workspace",
        "resolve_snapshot",
        "run_agent_step",
        "resolve_approval",
        "finalize_run",
        "finalize_run_projection",
        "summarize_delegation",
        "deliver_delegation_result",
        "prepare_triggered_task",
        "sync_external",
        "resolve_engineering_plan",
        "create_engineering_child_task",
        "finalize_engineering_ticket",
        "record_beat",
        "other",
    },
    "direction": {"input", "output", "cached"},
    "http_method": {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"},
    "http_status_class": {"1xx", "2xx", "3xx", "4xx", "5xx", "other"},
}
BASELINE_LABELS = {
    "service": "api",
    "outcome": "ok",
    "failure_class": "internal",
    "provider_type": "openai",
    "direction": "input",
    "tool_family": "system",
    "risk": "read",
    "connector_type": "github",
    "network_policy": "none",
    "stream": "INGRESS",
    "consumer": "event-worker-ingress",
    "task_queue": "jhin-workflow-queue",
    "activity": "reason_agent_step",
}

InstrumentCase = tuple[str, str, frozenset[str]]
INSTRUMENT_CASES: tuple[InstrumentCase, ...] = tuple(
    (name, kind, frozenset(labels)) for name, (kind, _unit, labels) in EXPECTED.items()
)
CARDINALITY_CASES = tuple(
    (name, kind, labels, label)
    for name, kind, labels in INSTRUMENT_CASES
    for label in sorted(labels)
)
FORBIDDEN_CASES = tuple(
    (name, kind, labels, forbidden)
    for name, kind, labels in INSTRUMENT_CASES
    for forbidden in sorted(EXPECTED_FORBIDDEN_LABELS)
)
EXTRA_CASES = tuple(
    (name, kind, labels, extra)
    for name, kind, labels in INSTRUMENT_CASES
    for extra in sorted(EXPECTED_ALLOWED_LABELS - labels)
)
MISSING_CASES = tuple(
    (name, kind, labels, missing)
    for name, kind, labels in INSTRUMENT_CASES
    for missing in sorted(labels)
)
NON_STRING_CASES = tuple(
    (name, kind, labels, invalid)
    for name, kind, labels in INSTRUMENT_CASES
    for invalid in sorted(labels)
)
INVALID_MEASUREMENTS: tuple[object, ...] = (
    True,
    -1,
    math.nan,
    math.inf,
    -math.inf,
    "1",
)


@dataclass
class _AddRecorder:
    calls: list[tuple[float, dict[str, str]]] = field(default_factory=list)

    def add(
        self,
        amount: int | float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        self.calls.append((float(amount), dict(attributes or {})))


@dataclass
class _RecordRecorder:
    calls: list[tuple[float, dict[str, str]]] = field(default_factory=list)

    def record(
        self,
        amount: int | float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        self.calls.append((float(amount), dict(attributes or {})))


@dataclass
class _RecordingMeter:
    counters: dict[str, _AddRecorder] = field(default_factory=dict)
    histograms: dict[str, _RecordRecorder] = field(default_factory=dict)
    gauge_callbacks: dict[str, Callable[[object], list[OTelObservation]]] = field(
        default_factory=dict
    )

    def create_counter(self, name: str, *, unit: str) -> _AddRecorder:
        recorder = _AddRecorder()
        self.counters[name] = recorder
        return recorder

    def create_histogram(self, name: str, *, unit: str) -> _RecordRecorder:
        recorder = _RecordRecorder()
        self.histograms[name] = recorder
        return recorder

    def create_observable_gauge(
        self,
        name: str,
        *,
        callbacks: Sequence[Callable[[object], list[OTelObservation]]],
        unit: str,
    ) -> object:
        self.gauge_callbacks[name] = callbacks[0]
        return object()


@dataclass(frozen=True)
class _FailingAddInstrument:
    name: str
    backend: _FailingMetricBackend

    def add(
        self,
        amount: int | float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        self.backend.calls.append(("add", self.name, amount, attributes))
        raise self.backend.error


@dataclass(frozen=True)
class _FailingRecordInstrument:
    name: str
    backend: _FailingMetricBackend

    def record(
        self,
        amount: int | float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        self.backend.calls.append(("record", self.name, amount, attributes))
        raise self.backend.error


@dataclass
class _FailingMetricBackend:
    error: BaseException
    calls: list[tuple[str, str, int | float, Mapping[str, str] | None]] = field(
        default_factory=list
    )

    def create_counter(self, name: str, *, unit: str) -> _FailingAddInstrument:
        return _FailingAddInstrument(name, self)

    def create_histogram(self, name: str, *, unit: str) -> _FailingRecordInstrument:
        return _FailingRecordInstrument(name, self)

    def create_observable_gauge(
        self,
        name: str,
        *,
        callbacks: Sequence[Callable[[object], list[OTelObservation]]],
        unit: str,
    ) -> object:
        return object()


@contextlib.contextmanager
def _fresh_in_memory_metrics() -> Iterator[tuple[JhinMetrics, InMemoryMetricReader]]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
    try:
        metrics = metrics_module.build_jhin_metrics(
            provider.get_meter("jhin-observability-test", version="0.1.0")
        )
        yield metrics, reader
    finally:
        provider.shutdown()


@pytest.fixture
def in_memory_metrics() -> Iterator[tuple[JhinMetrics, InMemoryMetricReader]]:
    with _fresh_in_memory_metrics() as pair:
        yield pair


def _expect_error(
    case: str,
    call: Callable[[], object],
    expected: type[Exception],
    match: str,
) -> Exception:
    """`pytest.raises(expected, match=match)` equivalent that names the folded case.

    Replicates pytest.raises semantics (subclass catch, ``re.search`` on ``str(exc)``)
    while prefixing every failure with the loop-folded case identity.
    """
    try:
        call()
    except expected as exc:
        assert re.search(match, str(exc)), f"{case}: {exc!r} does not match {match!r}"
        return exc
    except Exception as exc:
        pytest.fail(f"{case}: expected {expected.__name__}, got {exc!r}")
    pytest.fail(f"{case}: {expected.__name__} was not raised")


def _labels(label_names: frozenset[str]) -> dict[str, str]:
    return {key: BASELINE_LABELS[key] for key in label_names}


def _metric_name(name: str) -> RegistryMetricName:
    return cast(RegistryMetricName, name)


def _record(
    metrics: JhinMetrics,
    name: str,
    kind: str,
    labels: Mapping[str, object],
    measurement: object = 1,
) -> None:
    typed_labels = cast(dict[str, str], dict(labels))
    if kind == "counter":
        metrics.counter(_metric_name(name)).add(cast(Any, measurement), **typed_labels)
    elif kind == "histogram":
        metrics.histogram(_metric_name(name)).record(cast(Any, measurement), **typed_labels)
    else:
        metrics.set_observable(
            _metric_name(name),
            (Observation(cast(Any, measurement), typed_labels),),
        )


def _metric_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    return list(metric.data.data_points)
    return []


def series_for(
    reader: InMemoryMetricReader,
    name: str,
) -> set[tuple[tuple[str, str], ...]]:
    return {
        tuple(sorted((str(key), str(value)) for key, value in point.attributes.items()))
        for point in _metric_points(reader, name)
    }


def _facade(kind: str) -> tuple[JhinMetrics, _RecordingMeter | None]:
    if kind == "noop":
        return metrics_module.noop_metrics(), None
    meter = _RecordingMeter()
    return metrics_module.build_jhin_metrics(cast(Meter, meter)), meter


def _failing_facade(error: BaseException) -> tuple[JhinMetrics, _FailingMetricBackend]:
    backend = _FailingMetricBackend(error)
    return metrics_module.build_jhin_metrics(cast(Meter, backend)), backend


def _state_snapshot(
    facade_kind: str,
    meter: _RecordingMeter | None,
    name: str,
) -> list[tuple[float, dict[str, str]]]:
    if facade_kind == "noop":
        observations = metrics_module._VALIDATED_NOOP_STATE.observe(_metric_name(name))
    else:
        assert meter is not None
        observations = meter.gauge_callbacks[name](object())
    return [
        (
            float(item.value),
            dict(cast(Mapping[str, str], item.attributes or {})),
        )
        for item in observations
    ]


def _assert_no_recorder_calls(meter: _RecordingMeter | None, case: str = "") -> None:
    if meter is None:
        return
    assert all(not recorder.calls for recorder in meter.counters.values()), case
    assert all(not recorder.calls for recorder in meter.histograms.values()), case


def test_registry_exactly_matches_required_contract() -> None:
    assert metrics_module.instrument_contracts() == EXPECTED


def test_registry_and_closed_label_values_are_immutable_and_exact() -> None:
    assert metrics_module.ALLOWED_METRIC_LABELS == EXPECTED_ALLOWED_LABELS
    assert metrics_module.FORBIDDEN_IDENTIFIER_LABELS == EXPECTED_FORBIDDEN_LABELS
    assert {key: set(value) for key, value in metrics_module.LABEL_VALUES.items()} == (
        EXPECTED_LABEL_VALUES
    )
    assert {"/api/:path*", "other"} == metrics_module.ROUTE_LABEL_VALUES
    with pytest.raises(TypeError):
        cast(Any, metrics_module.METRIC_SPECS)["agent_runs_total"] = object()
    with pytest.raises(TypeError):
        cast(Any, metrics_module.LABEL_VALUES)["service"] = frozenset({"attacker"})


def test_metric_name_has_one_authority_and_public_contract_exports() -> None:
    assert observability_module.MetricName is RegistryMetricName
    assert metrics_module.MetricName is RegistryMetricName
    assert set(get_args(RegistryMetricName)) == set(EXPECTED)
    assert observability_module.Observation is Observation
    assert observability_module.instrument_contracts is metrics_module.instrument_contracts
    assert (
        observability_module.FORBIDDEN_IDENTIFIER_LABELS
        is metrics_module.FORBIDDEN_IDENTIFIER_LABELS
    )


def test_each_instrument_label_normalizes_unregistered_values_to_one_series() -> None:
    # Loop-folded from parametrize over CARDINALITY_CASES; every case gets a fresh
    # in-memory provider (as the old per-case fixture did) so series never mix.
    for name, kind, label_names, varied_label in CARDINALITY_CASES:
        case = f"name={name} kind={kind} varied_label={varied_label}"
        with _fresh_in_memory_metrics() as (metrics, reader):
            expected = _labels(label_names)
            expected[varied_label] = "other"
            expected_series = {tuple(sorted(expected.items()))}

            for index in range(32):
                labels = _labels(label_names)
                labels[varied_label] = f"unregistered-{varied_label}-{index}"
                _record(metrics, name, kind, labels)
                if kind == "gauge":
                    assert series_for(reader, name) == expected_series, f"{case} index={index}"

            assert series_for(reader, name) == expected_series, case


def test_every_identifier_label_is_rejected_before_recording() -> None:
    # Loop-folded from parametrize over facade_kind x FORBIDDEN_CASES: exact same
    # matrix, one collected item. Each case builds its own facade, so no state leaks.
    for facade_kind in ("configured", "noop"):
        for name, kind, label_names, forbidden in FORBIDDEN_CASES:
            case = f"facade={facade_kind} name={name} kind={kind} forbidden={forbidden}"
            metrics, meter = _facade(facade_kind)
            labels = _labels(label_names)
            labels[forbidden] = "secret-canary"
            before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

            caught = _expect_error(
                case,
                partial(_record, metrics, name, kind, labels),
                metrics_module.MetricLabelError,
                forbidden,
            )

            assert "secret-canary" not in str(caught), case
            _assert_no_recorder_calls(meter, case)
            if kind == "gauge":
                assert _state_snapshot(facade_kind, meter, name) == before, case


def test_every_globally_allowed_but_instrument_extra_label_is_rejected() -> None:
    # Loop-folded from parametrize over facade_kind x EXTRA_CASES: exact same matrix.
    for facade_kind in ("configured", "noop"):
        for name, kind, label_names, extra in EXTRA_CASES:
            case = f"facade={facade_kind} name={name} kind={kind} extra={extra}"
            metrics, meter = _facade(facade_kind)
            labels = _labels(label_names)
            labels[extra] = "other"
            before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

            _expect_error(
                case,
                partial(_record, metrics, name, kind, labels),
                metrics_module.MetricLabelError,
                extra,
            )

            _assert_no_recorder_calls(meter, case)
            if kind == "gauge":
                assert _state_snapshot(facade_kind, meter, name) == before, case


def test_each_required_label_is_rejected_when_missing() -> None:
    # Loop-folded from parametrize over facade_kind x MISSING_CASES: exact same matrix.
    for facade_kind in ("configured", "noop"):
        for name, kind, label_names, missing in MISSING_CASES:
            case = f"facade={facade_kind} name={name} kind={kind} missing={missing}"
            metrics, meter = _facade(facade_kind)
            labels = _labels(label_names)
            del labels[missing]
            before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

            _expect_error(
                case,
                partial(_record, metrics, name, kind, labels),
                metrics_module.MetricLabelError,
                missing,
            )

            _assert_no_recorder_calls(meter, case)
            if kind == "gauge":
                assert _state_snapshot(facade_kind, meter, name) == before, case


def test_each_required_label_rejects_non_string_values() -> None:
    # Loop-folded from parametrize over facade_kind x NON_STRING_CASES: exact same matrix.
    for facade_kind in ("configured", "noop"):
        for name, kind, label_names, invalid_label in NON_STRING_CASES:
            case = f"facade={facade_kind} name={name} kind={kind} invalid_label={invalid_label}"
            metrics, meter = _facade(facade_kind)
            labels: dict[str, object] = {}
            labels.update(_labels(label_names))
            labels[invalid_label] = 7
            before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

            _expect_error(
                case,
                partial(_record, metrics, name, kind, labels),
                metrics_module.MetricLabelError,
                invalid_label,
            )

            _assert_no_recorder_calls(meter, case)
            if kind == "gauge":
                assert _state_snapshot(facade_kind, meter, name) == before, case


def test_each_public_path_rejects_invalid_measurements_before_state_change() -> None:
    # Loop-folded from parametrize over facade_kind x INSTRUMENT_CASES x
    # INVALID_MEASUREMENTS: exact same cross-product, one collected item.
    for facade_kind in ("configured", "noop"):
        for name, kind, label_names in INSTRUMENT_CASES:
            for measurement in INVALID_MEASUREMENTS:
                case = f"facade={facade_kind} name={name} kind={kind} measurement={measurement!r}"
                metrics, meter = _facade(facade_kind)
                labels = _labels(label_names)
                if kind == "gauge":
                    _record(metrics, name, kind, labels, 17)
                before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

                _expect_error(
                    case,
                    partial(_record, metrics, name, kind, labels, measurement),
                    ValueError,
                    "measurement",
                )

                if kind == "gauge":
                    assert _state_snapshot(facade_kind, meter, name) == before, case
                else:
                    _assert_no_recorder_calls(meter, case)


def test_each_instrument_rejects_every_wrong_requested_kind() -> None:
    # Loop-folded from parametrize over facade_kind x INSTRUMENT_CASES: exact same matrix.
    for facade_kind in ("configured", "noop"):
        for name, kind, _label_names in INSTRUMENT_CASES:
            case = f"facade={facade_kind} name={name} kind={kind}"
            metrics, meter = _facade(facade_kind)
            metric_name = _metric_name(name)
            wrong_calls: list[tuple[str, Callable[[], object]]] = []
            if kind != "counter":
                wrong_calls.append(("counter", partial(metrics.counter, metric_name)))
            if kind != "histogram":
                wrong_calls.append(("histogram", partial(metrics.histogram, metric_name)))
            if kind != "gauge":
                wrong_calls.append(("gauge", partial(metrics.set_observable, metric_name, ())))

            for wrong_kind, wrong_call in wrong_calls:
                _expect_error(
                    f"{case} requested={wrong_kind}",
                    wrong_call,
                    metrics_module.MetricLabelError,
                    r"non-|requires a gauge",
                )

            _assert_no_recorder_calls(meter, case)


def test_bound_counter_contains_backend_add_after_strict_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend_error = Exception("counter-backend-secret-canary")
    metrics, backend = _failing_facade(backend_error)
    name = _metric_name("model_requests_total")
    counter = metrics.counter(name)
    valid_labels = {"provider_type": "openai", "outcome": "ok"}

    with pytest.raises(
        metrics_module.MetricLabelError,
        match="counter requested for non-counter metric",
    ):
        metrics.counter(_metric_name("agent_run_duration_seconds"))
    with pytest.raises(
        metrics_module.MetricLabelError,
        match="counter requested for non-counter metric",
    ):
        metrics.counter(_metric_name("unregistered_metric"))
    with pytest.raises(ValueError, match="must be numeric"):
        counter.add(cast(Any, True), **valid_labels)
    with pytest.raises(ValueError, match="finite and non-negative"):
        counter.add(-1, **valid_labels)
    with pytest.raises(metrics_module.MetricLabelError, match=r"missing=.*outcome"):
        counter.add(1, provider_type="openai")
    with pytest.raises(metrics_module.MetricLabelError, match=r"extra=.*service"):
        counter.add(1, provider_type="openai", outcome="ok", service="api")
    with pytest.raises(metrics_module.MetricLabelError, match="workspace_id"):
        counter.add(1, provider_type="openai", outcome="ok", workspace_id="secret")
    with pytest.raises(metrics_module.MetricLabelError, match="provider_type must be a string"):
        counter.add(1, provider_type=cast(Any, 7), outcome="ok")
    assert backend.calls == []

    noop_counter = metrics_module.noop_metrics().counter(name)
    with pytest.raises(ValueError, match="finite and non-negative"):
        noop_counter.add(-1, **valid_labels)
    assert noop_counter.add(1, **valid_labels) is None

    caller_labels = {
        "provider_type": "counter-label-secret-canary",
        "outcome": "ok",
    }
    caplog.set_level(logging.DEBUG)
    result = counter.add(73.125, **caller_labels)

    assert result is None
    assert caller_labels == {
        "provider_type": "counter-label-secret-canary",
        "outcome": "ok",
    }
    assert backend.calls == [
        (
            "add",
            "model_requests_total",
            73.125,
            {"outcome": "ok", "provider_type": "other"},
        )
    ]
    assert "73.125" not in caplog.text
    assert "counter-label-secret-canary" not in caplog.text
    assert "counter-backend-secret-canary" not in caplog.text


def test_bound_histogram_contains_backend_record_after_strict_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend_error = Exception("histogram-backend-secret-canary")
    metrics, backend = _failing_facade(backend_error)
    name = _metric_name("agent_run_duration_seconds")
    histogram = metrics.histogram(name)
    valid_labels = {"outcome": "ok"}

    with pytest.raises(
        metrics_module.MetricLabelError,
        match="histogram requested for non-histogram metric",
    ):
        metrics.histogram(_metric_name("agent_runs_total"))
    with pytest.raises(
        metrics_module.MetricLabelError,
        match="histogram requested for non-histogram metric",
    ):
        metrics.histogram(_metric_name("unregistered_metric"))
    with pytest.raises(ValueError, match="must be numeric"):
        histogram.record(cast(Any, True), **valid_labels)
    with pytest.raises(ValueError, match="finite and non-negative"):
        histogram.record(-1, **valid_labels)
    with pytest.raises(metrics_module.MetricLabelError, match=r"missing=.*outcome"):
        histogram.record(1)
    with pytest.raises(metrics_module.MetricLabelError, match=r"extra=.*service"):
        histogram.record(1, outcome="ok", service="api")
    with pytest.raises(metrics_module.MetricLabelError, match="workspace_id"):
        histogram.record(1, outcome="ok", workspace_id="secret")
    with pytest.raises(metrics_module.MetricLabelError, match="outcome must be a string"):
        histogram.record(1, outcome=cast(Any, 7))
    assert backend.calls == []

    noop_histogram = metrics_module.noop_metrics().histogram(name)
    with pytest.raises(ValueError, match="finite and non-negative"):
        noop_histogram.record(-1, **valid_labels)
    assert noop_histogram.record(1, **valid_labels) is None

    caller_labels = {"outcome": "histogram-label-secret-canary"}
    caplog.set_level(logging.DEBUG)
    result = histogram.record(91.625, **caller_labels)

    assert result is None
    assert caller_labels == {"outcome": "histogram-label-secret-canary"}
    assert backend.calls == [
        (
            "record",
            "agent_run_duration_seconds",
            91.625,
            {"outcome": "other"},
        )
    ]
    assert "91.625" not in caplog.text
    assert "histogram-label-secret-canary" not in caplog.text
    assert "histogram-backend-secret-canary" not in caplog.text


@pytest.mark.parametrize(
    ("kind", "name", "amount", "labels", "expected_attributes"),
    [
        pytest.param(
            "counter",
            "model_requests_total",
            37.5,
            {"provider_type": "authority-provider", "outcome": "ok"},
            {"outcome": "ok", "provider_type": "other"},
            id="counter",
        ),
        pytest.param(
            "histogram",
            "agent_run_duration_seconds",
            41.5,
            {"outcome": "authority-outcome"},
            {"outcome": "other"},
            id="histogram",
        ),
    ],
)
@pytest.mark.parametrize(
    "exception_type",
    [
        pytest.param(asyncio.CancelledError, id="cancelled-error"),
        pytest.param(KeyboardInterrupt, id="keyboard-interrupt"),
        pytest.param(SystemExit, id="system-exit"),
    ],
)
def test_bound_metrics_never_contain_nonordinary_base_exceptions(
    kind: str,
    name: str,
    amount: float,
    labels: dict[str, str],
    expected_attributes: dict[str, str],
    exception_type: type[BaseException],
) -> None:
    authority = exception_type("metric authority")
    metrics, backend = _failing_facade(authority)
    original_labels = dict(labels)
    operation = "add" if kind == "counter" else "record"

    with pytest.raises(exception_type) as caught:
        if kind == "counter":
            metrics.counter(_metric_name(name)).add(amount, **labels)
        else:
            metrics.histogram(_metric_name(name)).record(amount, **labels)

    assert caught.value is authority
    assert labels == original_labels
    assert backend.calls == [(operation, name, amount, expected_attributes)]


@pytest.mark.parametrize("facade_kind", ["configured", "noop"])
def test_duplicate_normalized_observable_identities_are_rejected_atomically(
    facade_kind: str,
) -> None:
    metrics, meter = _facade(facade_kind)
    metric_name = _metric_name("connector_health")
    metrics.set_observable(
        metric_name,
        (Observation(7, {"connector_type": "github"}),),
    )
    before = _state_snapshot(facade_kind, meter, "connector_health")

    with pytest.raises(
        metrics_module.MetricLabelError,
        match="duplicate normalized observable identity",
    ) as caught:
        metrics.set_observable(
            metric_name,
            (
                Observation(1, {"connector_type": "unregistered-first"}),
                Observation(2, {"connector_type": "unregistered-second"}),
            ),
        )

    rendered = str(caught.value)
    assert "unregistered-first" not in rendered
    assert "unregistered-second" not in rendered
    assert _state_snapshot(facade_kind, meter, "connector_health") == before


def test_gauge_replacement_removes_stale_observations(
    in_memory_metrics: tuple[JhinMetrics, InMemoryMetricReader],
) -> None:
    metrics, reader = in_memory_metrics
    metric_name = _metric_name("connector_health")
    metrics.set_observable(
        metric_name,
        (
            Observation(1, {"connector_type": "github"}),
            Observation(0, {"connector_type": "linear"}),
        ),
    )
    assert series_for(reader, "connector_health") == {
        (("connector_type", "github"),),
        (("connector_type", "linear"),),
    }

    metrics.set_observable(
        metric_name,
        (Observation(1, {"connector_type": "vercel"}),),
    )

    assert series_for(reader, "connector_health") == {
        (("connector_type", "vercel"),),
    }


def test_failed_oversized_gauge_replacement_preserves_all_prior_observations(
    in_memory_metrics: tuple[JhinMetrics, InMemoryMetricReader],
) -> None:
    metrics, reader = in_memory_metrics
    metric_name = _metric_name("connector_health")
    metrics.set_observable(
        metric_name,
        (
            Observation(1, {"connector_type": "github"}),
            Observation(0, {"connector_type": "linear"}),
        ),
    )
    before = series_for(reader, "connector_health")

    with pytest.raises(metrics_module.MetricLabelError, match="128"):
        metrics.set_observable(
            metric_name,
            tuple(
                Observation(index, {"connector_type": f"dynamic-{index}"}) for index in range(129)
            ),
        )

    assert series_for(reader, "connector_health") == before


def test_observable_state_accepts_exactly_128_unique_points_then_rejects_129(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = (
        "reason_agent_step",
        "commit_agent_step",
        "commit_approval_projection",
        "resolve_advertised_tools",
        "execute_bound_tool",
        "resolve_bound_tool_approval",
        "sync_external_tool",
        "cleanup_run_workspace",
        "resolve_snapshot",
        "run_agent_step",
        "resolve_approval",
        "finalize_run",
        "finalize_run_projection",
        "summarize_delegation",
        "deliver_delegation_result",
        "prepare_triggered_task",
    )
    outcomes = (
        "ok",
        "started",
        "completed",
        "failed",
        "cancelled",
        "timeout",
        "denied",
        "rejected",
        "duplicate",
    )
    fake_spec = metrics_module.MetricSpec(
        "gauge",
        "1",
        frozenset({"activity", "outcome"}),
    )
    monkeypatch.setattr(metrics_module, "_metric_spec", lambda _name: fake_spec)
    state = metrics_module._ObservableState()
    candidates = tuple(
        Observation(index, {"activity": activity, "outcome": outcome})
        for index, (activity, outcome) in enumerate(
            (activity, outcome) for activity in activities for outcome in outcomes
        )
    )
    assert len(candidates) > 128

    state.replace(_metric_name("connector_health"), candidates[:128])
    assert len(state.observe(_metric_name("connector_health"))) == 128

    with pytest.raises(metrics_module.MetricLabelError, match="128"):
        state.replace(_metric_name("connector_health"), candidates[:129])
    assert len(state.observe(_metric_name("connector_health"))) == 128
