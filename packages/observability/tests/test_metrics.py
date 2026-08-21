"""Exact metric registry, validation, and cardinality tests."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
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


@pytest.fixture
def in_memory_metrics() -> Iterator[tuple[JhinMetrics, InMemoryMetricReader]]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
    try:
        metrics = metrics_module.build_jhin_metrics(
            provider.get_meter("jhin-observability-test", version="0.1.0")
        )
        yield metrics, reader
    finally:
        provider.shutdown()


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


def _assert_no_recorder_calls(meter: _RecordingMeter | None) -> None:
    if meter is None:
        return
    assert all(not recorder.calls for recorder in meter.counters.values())
    assert all(not recorder.calls for recorder in meter.histograms.values())


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


@pytest.mark.parametrize(
    ("name", "kind", "label_names", "varied_label"),
    CARDINALITY_CASES,
)
def test_each_instrument_label_normalizes_unregistered_values_to_one_series(
    in_memory_metrics: tuple[JhinMetrics, InMemoryMetricReader],
    name: str,
    kind: str,
    label_names: frozenset[str],
    varied_label: str,
) -> None:
    metrics, reader = in_memory_metrics
    expected = _labels(label_names)
    expected[varied_label] = "other"
    expected_series = {tuple(sorted(expected.items()))}

    for index in range(32):
        labels = _labels(label_names)
        labels[varied_label] = f"unregistered-{varied_label}-{index}"
        _record(metrics, name, kind, labels)
        if kind == "gauge":
            assert series_for(reader, name) == expected_series

    assert series_for(reader, name) == expected_series


@pytest.mark.parametrize("facade_kind", ["configured", "noop"])
@pytest.mark.parametrize(
    ("name", "kind", "label_names", "forbidden"),
    FORBIDDEN_CASES,
)
def test_every_identifier_label_is_rejected_before_recording(
    facade_kind: str,
    name: str,
    kind: str,
    label_names: frozenset[str],
    forbidden: str,
) -> None:
    metrics, meter = _facade(facade_kind)
    labels = _labels(label_names)
    labels[forbidden] = "secret-canary"
    before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

    with pytest.raises(metrics_module.MetricLabelError, match=forbidden) as caught:
        _record(metrics, name, kind, labels)

    assert "secret-canary" not in str(caught.value)
    _assert_no_recorder_calls(meter)
    if kind == "gauge":
        assert _state_snapshot(facade_kind, meter, name) == before


@pytest.mark.parametrize("facade_kind", ["configured", "noop"])
@pytest.mark.parametrize(
    ("name", "kind", "label_names", "extra"),
    EXTRA_CASES,
)
def test_every_globally_allowed_but_instrument_extra_label_is_rejected(
    facade_kind: str,
    name: str,
    kind: str,
    label_names: frozenset[str],
    extra: str,
) -> None:
    metrics, meter = _facade(facade_kind)
    labels = _labels(label_names)
    labels[extra] = "other"
    before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

    with pytest.raises(metrics_module.MetricLabelError, match=extra):
        _record(metrics, name, kind, labels)

    _assert_no_recorder_calls(meter)
    if kind == "gauge":
        assert _state_snapshot(facade_kind, meter, name) == before


@pytest.mark.parametrize("facade_kind", ["configured", "noop"])
@pytest.mark.parametrize(
    ("name", "kind", "label_names", "missing"),
    MISSING_CASES,
)
def test_each_required_label_is_rejected_when_missing(
    facade_kind: str,
    name: str,
    kind: str,
    label_names: frozenset[str],
    missing: str,
) -> None:
    metrics, meter = _facade(facade_kind)
    labels = _labels(label_names)
    del labels[missing]
    before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

    with pytest.raises(metrics_module.MetricLabelError, match=missing):
        _record(metrics, name, kind, labels)

    _assert_no_recorder_calls(meter)
    if kind == "gauge":
        assert _state_snapshot(facade_kind, meter, name) == before


@pytest.mark.parametrize("facade_kind", ["configured", "noop"])
@pytest.mark.parametrize(
    ("name", "kind", "label_names", "invalid_label"),
    NON_STRING_CASES,
)
def test_each_required_label_rejects_non_string_values(
    facade_kind: str,
    name: str,
    kind: str,
    label_names: frozenset[str],
    invalid_label: str,
) -> None:
    metrics, meter = _facade(facade_kind)
    labels: dict[str, object] = {}
    labels.update(_labels(label_names))
    labels[invalid_label] = 7
    before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

    with pytest.raises(metrics_module.MetricLabelError, match=invalid_label):
        _record(metrics, name, kind, labels)

    _assert_no_recorder_calls(meter)
    if kind == "gauge":
        assert _state_snapshot(facade_kind, meter, name) == before


@pytest.mark.parametrize("facade_kind", ["configured", "noop"])
@pytest.mark.parametrize(("name", "kind", "label_names"), INSTRUMENT_CASES)
@pytest.mark.parametrize("measurement", INVALID_MEASUREMENTS)
def test_each_public_path_rejects_invalid_measurements_before_state_change(
    facade_kind: str,
    name: str,
    kind: str,
    label_names: frozenset[str],
    measurement: object,
) -> None:
    metrics, meter = _facade(facade_kind)
    labels = _labels(label_names)
    if kind == "gauge":
        _record(metrics, name, kind, labels, 17)
    before = _state_snapshot(facade_kind, meter, name) if kind == "gauge" else []

    with pytest.raises(ValueError, match="measurement"):
        _record(metrics, name, kind, labels, measurement)

    if kind == "gauge":
        assert _state_snapshot(facade_kind, meter, name) == before
    else:
        _assert_no_recorder_calls(meter)


@pytest.mark.parametrize("facade_kind", ["configured", "noop"])
@pytest.mark.parametrize(("name", "kind", "label_names"), INSTRUMENT_CASES)
def test_each_instrument_rejects_every_wrong_requested_kind(
    facade_kind: str,
    name: str,
    kind: str,
    label_names: frozenset[str],
) -> None:
    metrics, meter = _facade(facade_kind)
    metric_name = _metric_name(name)
    wrong_calls: list[Callable[[], object]] = []
    if kind != "counter":
        wrong_calls.append(lambda: metrics.counter(metric_name))
    if kind != "histogram":
        wrong_calls.append(lambda: metrics.histogram(metric_name))
    if kind != "gauge":
        wrong_calls.append(lambda: metrics.set_observable(metric_name, ()))

    for wrong_call in wrong_calls:
        with pytest.raises(
            metrics_module.MetricLabelError,
            match=r"non-|requires a gauge",
        ):
            wrong_call()

    _assert_no_recorder_calls(meter)


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
