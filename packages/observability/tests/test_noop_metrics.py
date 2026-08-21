"""Tests for the dependency-light metrics facade."""

from typing import get_args

from jhin_observability import MetricName as PublicMetricName
from jhin_observability.metrics import MetricName as MetricsMetricName
from jhin_observability.metrics import noop_metrics
from jhin_observability.registry import MetricName as RegistryMetricName


def test_noop_metrics_is_available_without_bootstrap_or_exporter_imports() -> None:
    metrics = noop_metrics()
    metrics.counter("model_requests_total").add(1, provider_type="openai", outcome="ok")
    metrics.histogram("agent_run_duration_seconds").record(1.25, outcome="completed")
    metrics.set_observable("connector_health", ())
    assert metrics.is_noop is True


def test_metric_name_has_one_authority() -> None:
    assert PublicMetricName is RegistryMetricName
    assert MetricsMetricName is RegistryMetricName
    assert set(get_args(RegistryMetricName)) == {
        "agent_runs_total",
        "agent_run_duration_seconds",
        "agent_run_failures_total",
        "model_requests_total",
        "model_tokens_total",
        "model_cost_estimate",
        "tool_calls_total",
        "tool_call_failures_total",
        "trigger_invocations_total",
        "trigger_failures_total",
        "sandbox_jobs_total",
        "sandbox_job_duration_seconds",
        "nats_consumer_lag",
        "temporal_activity_failures",
        "connector_health",
        "connector_connections",
    }
