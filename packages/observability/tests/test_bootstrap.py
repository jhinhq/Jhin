"""Validated configuration and transactional observability bootstrap tests."""

from __future__ import annotations

import atexit
import inspect
import json
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import grpc
import pytest
from _pytest.capture import CaptureFixture
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import NoOpTracerProvider
from structlog.types import EventDict, WrappedLogger

import jhin_observability.bootstrap as bootstrap_module
from jhin_observability import (
    DB_TABLE_VALUES,
    MAX_EXPORT_TIMEOUT_MILLIS,
    MAX_METRIC_EXPORT_INTERVAL_MILLIS,
    MAX_SPAN_EXPORT_BATCH_SIZE,
    MAX_SPAN_QUEUE_SIZE,
    SPAN_ATTRIBUTE_VALUES,
    SPAN_NAMES,
    TEMPORAL_ACTIVITY_NAMES,
    TEMPORAL_ACTIVITY_TYPE_VALUES,
    TEMPORAL_WORKFLOW_TYPE_VALUES,
    AttributeValue,
    JhinMetrics,
    MetricName,
    ObservabilityConfig,
    ObservabilityConfigurationError,
    ObservabilityNotInitializedError,
    ObservabilityRuntime,
    ObservabilitySettings,
    Observation,
    SpanName,
    TelemetryExporterStatus,
    bind_context,
    configure_json_logging,
    extract_trace_context,
    get_logger,
    get_runtime,
    initialize_observability,
    inject_trace_headers,
    noop_metrics,
    noop_tracer,
    normalize_span_attributes,
    record_span_error,
    safe_span,
    service_version,
)
from jhin_observability.exporters import (
    DiagnosticMetricExporter,
    DiagnosticSpanExporter,
    ExportDiagnostics,
)

PUBLIC_IMPORTS = (
    AttributeValue,
    DB_TABLE_VALUES,
    JhinMetrics,
    MetricName,
    Observation,
    ObservabilityConfig,
    ObservabilityConfigurationError,
    ObservabilityNotInitializedError,
    ObservabilityRuntime,
    ObservabilitySettings,
    SPAN_ATTRIBUTE_VALUES,
    SPAN_NAMES,
    SpanName,
    TEMPORAL_ACTIVITY_NAMES,
    TEMPORAL_ACTIVITY_TYPE_VALUES,
    TEMPORAL_WORKFLOW_TYPE_VALUES,
    TelemetryExporterStatus,
    bind_context,
    configure_json_logging,
    extract_trace_context,
    get_logger,
    get_runtime,
    initialize_observability,
    inject_trace_headers,
    noop_metrics,
    noop_tracer,
    normalize_span_attributes,
    record_span_error,
    safe_span,
    service_version,
)


class RecordingSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


class ShutdownRecordingSpanExporter(RecordingSpanExporter):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_requested = threading.Event()

    def shutdown(self) -> None:
        self.shutdown_requested.set()


class NoopMetricExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object
    ) -> MetricExportResult:
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        return None


def configured_config(**changes: Any) -> ObservabilityConfig:
    values: dict[str, object] = {
        "service_name": "api",
        "service_version": "0.1.0",
        "environment": "test",
        "otlp_endpoint": "https://collector.example.test:4317",
        "metric_export_interval_millis": 300_000,
    }
    values.update(changes)
    return ObservabilityConfig(**values)  # type: ignore[arg-type]


def install_in_memory_exporters(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RecordingSpanExporter, NoopMetricExporter]:
    span_exporter = RecordingSpanExporter()
    metric_exporter = NoopMetricExporter()
    monkeypatch.setattr(
        bootstrap_module,
        "_create_otlp_span_exporter",
        lambda _config, _credentials: span_exporter,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_create_otlp_metric_exporter",
        lambda _config, _credentials: metric_exporter,
    )
    return span_exporter, metric_exporter


def runtime_with_callbacks(
    config: ObservabilityConfig,
    callbacks: tuple[Callable[[int], None], ...],
) -> ObservabilityRuntime:
    tracer_provider = NoOpTracerProvider()
    meter_provider = NoOpMeterProvider()
    return ObservabilityRuntime(
        config=config,
        tracer=tracer_provider.get_tracer("test.runtime"),
        meter=meter_provider.get_meter("test.runtime"),
        metrics=noop_metrics(),
        _diagnostics=ExportDiagnostics(frozenset()),
        _owns_providers=True,
        _shutdown_callbacks=callbacks,
    )


def test_all_shared_interfaces_are_public() -> None:
    assert all(item is not None for item in PUBLIC_IMPORTS)


def test_empty_endpoint_installs_noop_telemetry_but_json_logging(
    capsys: CaptureFixture[str],
) -> None:
    runtime = initialize_observability(
        ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    )
    with runtime.tracer.start_as_current_span("test.noop") as span:
        assert span.is_recording() is False
    get_logger(__name__).info("api.started")
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1
    assert runtime.metrics is noop_metrics()
    assert runtime.status().configured is False


@pytest.mark.parametrize(
    ("endpoint", "insecure"),
    [
        ("https://collector.example.test:4317", False),
        ("http://otel-collector:4317", True),
        ("http://localhost:4317/", True),
        ("http://127.0.0.1:4317", True),
        ("http://[::1]:4317", True),
    ],
)
def test_valid_otlp_transport_configuration(endpoint: str, insecure: bool) -> None:
    assert (
        configured_config(otlp_endpoint=endpoint, otlp_insecure=insecure).otlp_endpoint == endpoint
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"otlp_endpoint": None, "otlp_insecure": True}, "requires an OTLP endpoint"),
        ({"otlp_endpoint": None, "otlp_ca_file": Path("ca.pem")}, "requires an OTLP endpoint"),
        (
            {
                "otlp_endpoint": None,
                "otlp_client_certificate_file": Path("client.pem"),
                "otlp_client_key_file": Path("client.key"),
            },
            "requires an OTLP endpoint",
        ),
        ({"otlp_endpoint": "grpc://collector:4317"}, r"absolute HTTP\(S\) URL"),
        ({"otlp_endpoint": "http://otel-collector:4317", "otlp_insecure": False}, "cleartext OTLP"),
        (
            {"otlp_endpoint": "http://collector.example.test:4317", "otlp_insecure": True},
            "cleartext OTLP",
        ),
        ({"otlp_endpoint": "http://localhost", "otlp_insecure": True}, "cleartext OTLP"),
        ({"otlp_endpoint": "http://localhost:4318", "otlp_insecure": True}, "cleartext OTLP"),
        ({"otlp_endpoint": "http://LOCALHOST:4317", "otlp_insecure": True}, "cleartext OTLP"),
        ({"otlp_endpoint": "http://localhost:4317/v1/traces", "otlp_insecure": True}, "root path"),
        (
            {
                "otlp_endpoint": "http://localhost:4317",
                "otlp_insecure": True,
                "otlp_ca_file": Path("ca.pem"),
            },
            "HTTP OTLP",
        ),
        (
            {
                "otlp_endpoint": "http://localhost:4317",
                "otlp_insecure": True,
                "otlp_client_certificate_file": Path("client.pem"),
                "otlp_client_key_file": Path("client.key"),
            },
            "HTTP OTLP",
        ),
        (
            {"otlp_endpoint": "https://collector.example.test:4317", "otlp_insecure": True},
            "HTTPS OTLP",
        ),
        ({"otlp_endpoint": "https://collector.example.test:4317/v1/metrics"}, "root path"),
        (
            {"otlp_endpoint": "https://user:pass@collector.example.test:4317"},
            "credentials, query, or fragment",
        ),
        (
            {"otlp_endpoint": "https://collector.example.test:4317?token=canary"},
            "credentials, query, or fragment",
        ),
        (
            {"otlp_endpoint": "https://collector.example.test:4317#canary"},
            "credentials, query, or fragment",
        ),
        ({"otlp_endpoint": "https://collector.example.test:bad"}, "valid host and port"),
        ({"otlp_endpoint": "https://collector.example.test:0"}, "valid host and port"),
        ({"otlp_endpoint": "https://collector.example.test:99999"}, "valid host and port"),
        ({"otlp_endpoint": "https://bad host:4317"}, "valid host and port"),
        ({"otlp_endpoint": "https://:4317"}, r"absolute HTTP\(S\) URL"),
        ({"otlp_client_certificate_file": Path("client.pem")}, "configured together"),
        ({"otlp_client_key_file": Path("client.key")}, "configured together"),
    ],
)
def test_invalid_otlp_transport_configuration(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        configured_config(**changes)


@pytest.mark.parametrize(
    "value",
    [True, False, "0.1", None, float("nan"), float("inf"), -float("inf"), -0.01, 1.01],
)
def test_direct_sample_ratio_rejects_non_numeric_and_out_of_range(value: object) -> None:
    with pytest.raises(ValueError, match="trace sample ratio"):
        configured_config(trace_sample_ratio=value)


def test_direct_config_rejects_unregistered_trace_sampler() -> None:
    with pytest.raises(ValueError, match="trace sampler"):
        configured_config(trace_sampler="credential-canary")


@pytest.mark.parametrize(
    ("field", "ceiling"),
    [
        ("span_queue_size", MAX_SPAN_QUEUE_SIZE),
        ("span_export_batch_size", MAX_SPAN_EXPORT_BATCH_SIZE),
        ("export_timeout_millis", MAX_EXPORT_TIMEOUT_MILLIS),
        ("metric_export_interval_millis", MAX_METRIC_EXPORT_INTERVAL_MILLIS),
    ],
)
@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5])
def test_direct_numeric_limits_reject_wrong_types_and_nonpositive_values(
    field: str, ceiling: int, invalid: object
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        configured_config(**{field: invalid})
    with pytest.raises(ValueError, match="maximum"):
        configured_config(**{field: ceiling + 1})


def test_direct_numeric_limits_accept_exact_ceilings() -> None:
    config = configured_config(
        span_queue_size=MAX_SPAN_QUEUE_SIZE,
        span_export_batch_size=MAX_SPAN_EXPORT_BATCH_SIZE,
        export_timeout_millis=MAX_EXPORT_TIMEOUT_MILLIS,
        metric_export_interval_millis=MAX_METRIC_EXPORT_INTERVAL_MILLIS,
    )
    assert config.span_queue_size == MAX_SPAN_QUEUE_SIZE


def test_export_batch_cannot_exceed_queue() -> None:
    with pytest.raises(ValueError, match="cannot exceed queue size"):
        configured_config(span_queue_size=4, span_export_batch_size=5)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OTEL_BSP_MAX_QUEUE_SIZE", "01"),
        ("OTEL_BSP_MAX_QUEUE_SIZE", "+1"),
        ("OTEL_BSP_MAX_QUEUE_SIZE", "1.0"),
        ("OTEL_BSP_MAX_QUEUE_SIZE", "1e3"),
        ("OTEL_BSP_MAX_QUEUE_SIZE", "true"),
        ("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "01"),
        ("OTEL_EXPORTER_OTLP_TIMEOUT_MILLIS", "1e3"),
        ("OTEL_METRIC_EXPORT_INTERVAL_MILLIS", "true"),
        ("OTEL_TRACES_SAMPLER_ARG", "1e-1"),
        ("OTEL_TRACES_SAMPLER_ARG", "nan"),
        ("OTEL_TRACES_SAMPLER_ARG", "inf"),
        ("OTEL_TRACES_SAMPLER_ARG", "true"),
    ],
)
def test_settings_reject_noncanonical_numeric_environment_strings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        ObservabilitySettings(_env_file=None)


def test_settings_accept_canonical_numeric_environment_strings_and_blank_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "2048")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "512")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT_MILLIS", "5000")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL_MILLIS", "60000")
    config = ObservabilitySettings(_env_file=None).observability_config(
        service_name="api", service_version="0.1.0"
    )
    assert config.otlp_endpoint is None
    assert config.trace_sample_ratio == 0.25
    assert config.span_queue_size == 2048


@pytest.mark.parametrize(
    ("name", "ceiling"),
    [
        ("OTEL_BSP_MAX_QUEUE_SIZE", MAX_SPAN_QUEUE_SIZE),
        ("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", MAX_SPAN_EXPORT_BATCH_SIZE),
        ("OTEL_EXPORTER_OTLP_TIMEOUT_MILLIS", MAX_EXPORT_TIMEOUT_MILLIS),
        ("OTEL_METRIC_EXPORT_INTERVAL_MILLIS", MAX_METRIC_EXPORT_INTERVAL_MILLIS),
    ],
)
def test_settings_numeric_fields_accept_exact_ceiling_and_reject_above_it(
    monkeypatch: pytest.MonkeyPatch, name: str, ceiling: int
) -> None:
    monkeypatch.setenv(name, str(ceiling))
    ObservabilitySettings(_env_file=None)
    monkeypatch.setenv(name, str(ceiling + 1))
    with pytest.raises(ValueError):
        ObservabilitySettings(_env_file=None)


def test_blank_endpoint_rejects_insecure_or_tls_settings_after_normalization() -> None:
    with pytest.raises(ValueError, match="requires an OTLP endpoint"):
        ObservabilitySettings(
            otel_exporter_otlp_endpoint="   ",
            otel_exporter_otlp_insecure=True,
        ).observability_config(service_name="api", service_version="0.1.0")


def test_settings_reject_direct_booleans_for_numeric_fields() -> None:
    with pytest.raises(ValueError):
        ObservabilitySettings(otel_bsp_max_queue_size=True)
    with pytest.raises(ValueError):
        ObservabilitySettings(otel_traces_sampler_arg=True)


def test_settings_environment_is_closed() -> None:
    assert ObservabilitySettings(app_env="dev").app_env == "dev"
    with pytest.raises(ValueError):
        ObservabilitySettings(app_env="development")


def test_settings_forward_known_secret_processors() -> None:
    def processor(_logger: WrappedLogger, _name: str, event: EventDict) -> EventDict:
        return event

    config = ObservabilitySettings().observability_config(
        service_name="api",
        service_version="0.1.0",
        extra_log_processors=(processor,),
    )
    assert config.extra_log_processors == (processor,)


def test_service_version_uses_distribution_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jhin_observability.config.version", lambda name: "9.8.7")
    assert service_version("jhin-api") == "9.8.7"


def test_service_version_missing_metadata_is_a_safe_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing(_name: str) -> str:
        raise PackageNotFoundError("credential-canary")

    monkeypatch.setattr("jhin_observability.config.version", missing)
    with pytest.raises(ObservabilityConfigurationError) as caught:
        service_version("secret-distribution-canary")
    rendered = str(caught.value)
    assert "credential-canary" not in rendered
    assert "secret-distribution-canary" not in rendered


def test_initialize_is_idempotent_only_for_the_same_config() -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    assert initialize_observability(config) is initialize_observability(config)
    with pytest.raises(ObservabilityConfigurationError, match="already initialized") as caught:
        initialize_observability(replace(config, service_name="agent-worker"))
    assert "api" not in str(caught.value)
    assert "agent-worker" not in str(caught.value)


def test_runtime_config_and_protected_health_status_are_public_and_exact() -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    runtime = initialize_observability(config)
    assert runtime.config is config
    assert isinstance(runtime, ObservabilityRuntime)
    assert [field.name for field in fields(TelemetryExporterStatus)] == [
        "configured",
        "last_success_at",
        "dropped_items",
        "last_error_code",
    ]
    assert runtime.status() == TelemetryExporterStatus(
        configured=False,
        last_success_at=None,
        dropped_items=0,
        last_error_code=None,
    )


def test_shutdown_is_idempotent_and_permits_reinitialize() -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    first = initialize_observability(config)
    first.shutdown()
    first.shutdown()
    second = initialize_observability(config)
    assert second is not first
    with pytest.raises(ValueError, match="must not be negative"):
        second.shutdown(-1)


def test_concurrent_equal_initialization_constructs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    original = bootstrap_module._construct_runtime
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked(candidate: ObservabilityConfig) -> ObservabilityRuntime:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=1.0)
        return original(candidate)

    monkeypatch.setattr(bootstrap_module, "_construct_runtime", blocked)
    results: list[ObservabilityRuntime] = []
    threads = [
        threading.Thread(target=lambda: results.append(initialize_observability(config)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=1.0)
    release.set()
    for thread in threads:
        thread.join(timeout=1.0)
        assert thread.is_alive() is False
    assert calls == 1
    assert results[0] is results[1]


def test_shutdown_detaches_owner_before_equal_reinitialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    entered = threading.Event()
    release = threading.Event()
    callbacks = 0

    def callback(_remaining: int) -> None:
        nonlocal callbacks
        callbacks += 1
        entered.set()
        assert release.wait(timeout=1.0)

    monkeypatch.setattr(
        bootstrap_module,
        "_construct_runtime",
        lambda candidate: runtime_with_callbacks(candidate, (callback,)),
    )
    first = initialize_observability(config)
    shutdown_thread = threading.Thread(target=first.shutdown)
    shutdown_thread.start()
    assert entered.wait(timeout=1.0)
    second = initialize_observability(config)
    assert second is not first
    release.set()
    shutdown_thread.join(timeout=1.0)
    assert shutdown_thread.is_alive() is False
    assert callbacks == 1


def test_two_shutdown_callers_do_not_run_callbacks_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    entered = threading.Event()
    release = threading.Event()
    callbacks = 0

    def callback(_remaining: int) -> None:
        nonlocal callbacks
        callbacks += 1
        entered.set()
        assert release.wait(timeout=1.0)

    monkeypatch.setattr(
        bootstrap_module,
        "_construct_runtime",
        lambda candidate: runtime_with_callbacks(candidate, (callback,)),
    )
    runtime = initialize_observability(config)
    first = threading.Thread(target=runtime.shutdown)
    second = threading.Thread(target=lambda: runtime.shutdown(30))
    first.start()
    assert entered.wait(timeout=1.0)
    second.start()
    second.join(timeout=0.2)
    assert second.is_alive() is False
    assert runtime._shutdown_complete is False
    release.set()
    first.join(timeout=1.0)
    assert first.is_alive() is False
    assert vars(runtime)["_shutdown_complete"] is True
    assert callbacks == 1


def test_shutdown_callback_failure_is_contained_and_notifies_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    completed = threading.Event()

    def fails(_remaining: int) -> None:
        raise RuntimeError("credential-canary")

    def succeeds(_remaining: int) -> None:
        completed.set()

    monkeypatch.setattr(
        bootstrap_module,
        "_construct_runtime",
        lambda candidate: runtime_with_callbacks(candidate, (fails, succeeds)),
    )
    runtime = initialize_observability(config)
    runtime.shutdown()
    assert completed.is_set()
    assert runtime._shutdown_complete is True
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()


def test_expired_shutdown_deadline_still_requests_every_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        bootstrap_module,
        "_construct_runtime",
        lambda candidate: runtime_with_callbacks(
            candidate,
            (
                lambda remaining: calls.append(("first", remaining)),
                lambda remaining: calls.append(("second", remaining)),
            ),
        ),
    )
    runtime = initialize_observability(config)
    runtime.shutdown(timeout_millis=0)
    assert calls == [("second", 0), ("first", 0)]
    assert vars(runtime)["_shutdown_complete"] is True


def test_partial_cleanup_is_requested_after_shared_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = ShutdownRecordingSpanExporter()
    monotonic_values = iter((10.0, 11.0))
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: next(monotonic_values, 11.0),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_create_otlp_span_exporter",
        lambda _config, _credentials: exporter,
    )

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected-bootstrap-failure")

    monkeypatch.setattr(bootstrap_module, "_create_span_processor", fail)
    with pytest.raises(RuntimeError, match="injected-bootstrap-failure"):
        initialize_observability(configured_config(export_timeout_millis=1))
    assert exporter.shutdown_requested.wait(timeout=1.0)


@pytest.mark.parametrize(
    "failing_factory",
    [
        "_create_span_processor",
        "_create_otlp_metric_exporter",
        "_create_metric_reader",
        "_create_tracer_provider",
        "_create_meter_provider",
    ],
)
def test_partial_bootstrap_failure_cleans_every_owned_worker(
    monkeypatch: pytest.MonkeyPatch, failing_factory: str
) -> None:
    install_in_memory_exporters(monkeypatch)
    before = set(threading.enumerate())

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected-bootstrap-failure")

    monkeypatch.setattr(bootstrap_module, failing_factory, fail)
    with pytest.raises(RuntimeError, match="injected-bootstrap-failure"):
        initialize_observability(configured_config())
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    survivors = [thread for thread in threading.enumerate() if thread not in before]
    for thread in survivors:
        thread.join(timeout=1.0)
    assert [thread.name for thread in survivors if thread.is_alive()] == []


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_tls_files_fail_closed_without_path_or_exception_text(
    tmp_path: Path, path_kind: str
) -> None:
    candidate = tmp_path / "secret-client-canary.pem"
    if path_kind == "directory":
        candidate.mkdir()
    config = configured_config(otlp_ca_file=candidate)
    with pytest.raises(ObservabilityConfigurationError) as caught:
        initialize_observability(config)
    rendered = str(caught.value)
    assert str(candidate) not in rendered
    assert "secret-client-canary" not in rendered
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()


def test_unreadable_tls_file_fails_closed_without_path_or_exception_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = tmp_path / "unreadable-client-canary.pem"
    candidate.write_bytes(b"tls-canary")

    def unreadable(_path: Path) -> bytes:
        raise PermissionError("permission-canary")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(ObservabilityConfigurationError) as caught:
        initialize_observability(configured_config(otlp_ca_file=candidate))
    rendered = str(caught.value)
    assert "unreadable-client-canary" not in rendered
    assert "permission-canary" not in rendered


def test_tls_bytes_are_read_only_at_bootstrap_and_never_exposed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ca = tmp_path / "ca.pem"
    certificate = tmp_path / "client.pem"
    key = tmp_path / "client.key"
    ca.write_bytes(b"ca-canary-bytes")
    certificate.write_bytes(b"certificate-canary-bytes")
    key.write_bytes(b"key-canary-bytes")
    captured: dict[str, bytes | None] = {}
    real_ssl_channel_credentials = grpc.ssl_channel_credentials

    def credentials(
        root_certificates: bytes | None = None,
        private_key: bytes | None = None,
        certificate_chain: bytes | None = None,
    ) -> grpc.ChannelCredentials:
        captured.update(
            root_certificates=root_certificates,
            private_key=private_key,
            certificate_chain=certificate_chain,
        )
        return real_ssl_channel_credentials()

    monkeypatch.setattr(grpc, "ssl_channel_credentials", credentials)
    install_in_memory_exporters(monkeypatch)
    config = configured_config(
        otlp_ca_file=ca,
        otlp_client_certificate_file=certificate,
        otlp_client_key_file=key,
    )
    runtime = initialize_observability(config)
    assert captured == {
        "root_certificates": b"ca-canary-bytes",
        "private_key": b"key-canary-bytes",
        "certificate_chain": b"certificate-canary-bytes",
    }
    assert "canary-bytes" not in repr(runtime.status())


def test_resource_has_exactly_three_configured_attributes_and_ignores_detectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "host.name=resource-canary")
    span_exporter, _metric_exporter = install_in_memory_exporters(monkeypatch)
    runtime = initialize_observability(configured_config(trace_sampler="always_on"))
    with runtime.tracer.start_as_current_span("test.resource"):
        pass
    runtime.shutdown()
    resource = span_exporter.spans[0].resource
    assert resource is not None
    assert dict(resource.attributes) == {
        "service.name": "api",
        "service.version": "0.1.0",
        "deployment.environment.name": "test",
    }
    assert "resource-canary" not in json.dumps(dict(resource.attributes))


def test_metric_reader_receives_exact_configured_timings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float | None] = {}

    class Reader:
        def __init__(
            self,
            exporter: MetricExporter,
            export_interval_millis: float | None = None,
            export_timeout_millis: float | None = None,
        ) -> None:
            captured["interval"] = export_interval_millis
            captured["timeout"] = export_timeout_millis

    monkeypatch.setattr(bootstrap_module, "PeriodicExportingMetricReader", Reader)
    config = configured_config(
        export_timeout_millis=1_234,
        metric_export_interval_millis=56_789,
    )
    result = bootstrap_module._create_metric_reader(NoopMetricExporter(), config)
    assert isinstance(result, Reader)
    assert captured == {"interval": 56_789, "timeout": 1_234}


def test_providers_do_not_register_atexit_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_in_memory_exporters(monkeypatch)
    registered: list[object] = []
    monkeypatch.setattr(
        atexit, "register", lambda callback, *_args, **_kwargs: registered.append(callback)
    )
    initialize_observability(configured_config())
    assert registered == []


def test_dependency_import_and_exporter_signature_compatibility() -> None:
    assert NoOpTracerProvider.__module__ == "opentelemetry.trace"
    assert grpc.ChannelCredentials is not None
    assert TracerProvider is not None
    assert MeterProvider is not None
    assert PeriodicExportingMetricReader is not None

    span_base = inspect.signature(SpanExporter.export)
    span_wrapper = inspect.signature(DiagnosticSpanExporter.export)
    assert tuple(span_wrapper.parameters) == tuple(span_base.parameters)
    metric_base = inspect.signature(MetricExporter.export)
    metric_wrapper = inspect.signature(DiagnosticMetricExporter.export)
    assert tuple(metric_wrapper.parameters) == tuple(metric_base.parameters)
    assert [parameter.kind for parameter in metric_wrapper.parameters.values()] == [
        parameter.kind for parameter in metric_base.parameters.values()
    ]

    credentials = grpc.ssl_channel_credentials()
    span = OTLPSpanExporter(
        endpoint="https://localhost:4317",
        credentials=credentials,
        timeout=0.01,
    )
    metric = OTLPMetricExporter(
        endpoint="https://localhost:4317",
        credentials=credentials,
        timeout=0.01,
    )
    span.shutdown()
    metric.shutdown()


def test_noop_facade_import_does_not_import_bootstrap_or_exporters() -> None:
    code = (
        "import sys; from jhin_observability.metrics import noop_metrics; "
        "assert noop_metrics().is_noop; "
        "assert 'jhin_observability.bootstrap' not in sys.modules; "
        "assert 'jhin_observability.exporters' not in sys.modules"
    )
    result = __import__("subprocess").run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
