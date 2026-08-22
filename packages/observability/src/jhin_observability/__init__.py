"""Shared JSON-v1 logging, tracing, and metrics contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jhin_observability.config import (
    MAX_EXPORT_TIMEOUT_MILLIS,
    MAX_METRIC_EXPORT_INTERVAL_MILLIS,
    MAX_SPAN_EXPORT_BATCH_SIZE,
    MAX_SPAN_QUEUE_SIZE,
    ObservabilityConfig,
    ObservabilityConfigurationError,
    ObservabilityNotInitializedError,
    ObservabilitySettings,
    service_version,
)
from jhin_observability.context import (
    TRACE_CARRIER_KEYS,
    bind_context,
    extract_trace_context,
    inject_trace_headers,
    is_safe_context_id,
    noop_tracer,
    normalize_span_attributes,
    record_span_error,
    safe_span,
    set_span_attributes,
)
from jhin_observability.errors import SafeError, SafeErrorCode, safe_error
from jhin_observability.events import (
    EVENT_FIELD_RULES,
    filter_log_event,
    normalize_connector_type,
    normalize_environment,
    normalize_event_family,
    normalize_sandbox_outcome,
)
from jhin_observability.logging import configure_json_logging, get_logger
from jhin_observability.metrics import (
    FORBIDDEN_IDENTIFIER_LABELS,
    JhinMetrics,
    MetricLabelError,
    Observation,
    instrument_contracts,
    noop_metrics,
)
from jhin_observability.redaction import is_sensitive_key_name, structural_redaction
from jhin_observability.registry import (
    DB_TABLE_VALUES,
    SPAN_ATTRIBUTE_VALUES,
    SPAN_NAMES,
    TEMPORAL_ACTIVITY_NAMES,
    TEMPORAL_ACTIVITY_TYPE_VALUES,
    TEMPORAL_WORKFLOW_TYPE_VALUES,
    AttributeValue,
    MetricName,
    SpanName,
)
from jhin_observability.sqlalchemy import (
    install_sqlalchemy_tracing,
    normalized_sql_metadata,
)

if TYPE_CHECKING:
    from jhin_observability.bootstrap import (
        ObservabilityRuntime,
        TelemetryExporterStatus,
        get_runtime,
        initialize_observability,
    )
    from jhin_observability.temporal import (
        MAX_TEMPORAL_TRACER_DATA_BYTES,
        ObservabilityTemporalSettings,
        SafeTemporalTracingInterceptor,
        TemporalActivityMetricsInterceptor,
        TemporalInterceptorRole,
        build_temporal_worker,
        connect_temporal_client,
        temporal_client_interceptors,
        temporal_worker_interceptors,
    )


_BOOTSTRAP_EXPORTS = frozenset(
    {
        "ObservabilityRuntime",
        "TelemetryExporterStatus",
        "get_runtime",
        "initialize_observability",
    }
)
_TEMPORAL_EXPORTS = frozenset(
    {
        "MAX_TEMPORAL_TRACER_DATA_BYTES",
        "ObservabilityTemporalSettings",
        "SafeTemporalTracingInterceptor",
        "TemporalActivityMetricsInterceptor",
        "TemporalInterceptorRole",
        "build_temporal_worker",
        "connect_temporal_client",
        "temporal_client_interceptors",
        "temporal_worker_interceptors",
    }
)


def __getattr__(name: str) -> Any:
    if name in _BOOTSTRAP_EXPORTS:
        from jhin_observability import bootstrap

        value = getattr(bootstrap, name)
        globals()[name] = value
        return value
    if name in _TEMPORAL_EXPORTS:
        from jhin_observability import temporal

        value = getattr(temporal, name)
        globals()[name] = value
        return value
    raise AttributeError(name)


__all__ = [
    "DB_TABLE_VALUES",
    "EVENT_FIELD_RULES",
    "FORBIDDEN_IDENTIFIER_LABELS",
    "MAX_EXPORT_TIMEOUT_MILLIS",
    "MAX_METRIC_EXPORT_INTERVAL_MILLIS",
    "MAX_SPAN_EXPORT_BATCH_SIZE",
    "MAX_SPAN_QUEUE_SIZE",
    "MAX_TEMPORAL_TRACER_DATA_BYTES",
    "SPAN_ATTRIBUTE_VALUES",
    "SPAN_NAMES",
    "TEMPORAL_ACTIVITY_NAMES",
    "TEMPORAL_ACTIVITY_TYPE_VALUES",
    "TEMPORAL_WORKFLOW_TYPE_VALUES",
    "TRACE_CARRIER_KEYS",
    "AttributeValue",
    "JhinMetrics",
    "MetricLabelError",
    "MetricName",
    "ObservabilityConfig",
    "ObservabilityConfigurationError",
    "ObservabilityNotInitializedError",
    "ObservabilityRuntime",
    "ObservabilitySettings",
    "ObservabilityTemporalSettings",
    "Observation",
    "SafeError",
    "SafeErrorCode",
    "SafeTemporalTracingInterceptor",
    "SpanName",
    "TelemetryExporterStatus",
    "TemporalActivityMetricsInterceptor",
    "TemporalInterceptorRole",
    "bind_context",
    "build_temporal_worker",
    "configure_json_logging",
    "connect_temporal_client",
    "extract_trace_context",
    "filter_log_event",
    "get_logger",
    "get_runtime",
    "initialize_observability",
    "inject_trace_headers",
    "install_sqlalchemy_tracing",
    "instrument_contracts",
    "is_safe_context_id",
    "is_sensitive_key_name",
    "noop_metrics",
    "noop_tracer",
    "normalize_connector_type",
    "normalize_environment",
    "normalize_event_family",
    "normalize_sandbox_outcome",
    "normalize_span_attributes",
    "normalized_sql_metadata",
    "record_span_error",
    "safe_error",
    "safe_span",
    "service_version",
    "set_span_attributes",
    "structural_redaction",
    "temporal_client_interceptors",
    "temporal_worker_interceptors",
]
