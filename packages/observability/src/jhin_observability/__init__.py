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
from jhin_observability.logging import configure_json_logging, configure_logging, get_logger
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


def __getattr__(name: str) -> Any:
    if name in {
        "ObservabilityRuntime",
        "TelemetryExporterStatus",
        "get_runtime",
        "initialize_observability",
    }:
        from jhin_observability import bootstrap

        return getattr(bootstrap, name)
    raise AttributeError(name)


__all__ = [
    "DB_TABLE_VALUES",
    "EVENT_FIELD_RULES",
    "FORBIDDEN_IDENTIFIER_LABELS",
    "MAX_EXPORT_TIMEOUT_MILLIS",
    "MAX_METRIC_EXPORT_INTERVAL_MILLIS",
    "MAX_SPAN_EXPORT_BATCH_SIZE",
    "MAX_SPAN_QUEUE_SIZE",
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
    "Observation",
    "SafeError",
    "SafeErrorCode",
    "SpanName",
    "TelemetryExporterStatus",
    "bind_context",
    "configure_json_logging",
    "configure_logging",
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
    "structural_redaction",
]
