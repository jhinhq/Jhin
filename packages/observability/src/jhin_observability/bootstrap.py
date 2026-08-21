"""Transactional optional no-op/OTLP observability bootstrap."""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import grpc
from opentelemetry.metrics import Meter, NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from opentelemetry.trace import NoOpTracerProvider, Tracer

from jhin_observability.config import (
    ObservabilityConfig,
    ObservabilityConfigurationError,
    ObservabilityNotInitializedError,
)
from jhin_observability.exporters import (
    BoundedBatchSpanProcessor,
    DiagnosticMetricExporter,
    DiagnosticSpanExporter,
    ExportDiagnostics,
    ExportDiagnosticsSnapshot,
)
from jhin_observability.logging import configure_json_logging
from jhin_observability.metrics import JhinMetrics, noop_metrics


class _Diagnostics(Protocol):
    def snapshot(self) -> ExportDiagnosticsSnapshot: ...


@dataclass(frozen=True)
class TelemetryExporterStatus:
    configured: bool
    last_success_at: datetime | None
    dropped_items: int
    last_error_code: Literal["export_timeout", "export_failed"] | None


@dataclass
class ObservabilityRuntime:
    config: ObservabilityConfig
    tracer: Tracer
    meter: Meter
    metrics: JhinMetrics
    _diagnostics: _Diagnostics
    _owns_providers: bool
    _shutdown_callbacks: tuple[Callable[[int], None], ...] = ()
    _shutdown_condition: threading.Condition = field(
        default_factory=threading.Condition,
        repr=False,
    )
    _shutdown_state: Literal["running", "shutting_down", "complete"] = field(
        default="running",
        repr=False,
    )
    _shutdown_complete: bool = False

    def status(self) -> TelemetryExporterStatus:
        snapshot = self._diagnostics.snapshot()
        return TelemetryExporterStatus(
            configured=self.config.otlp_endpoint is not None,
            last_success_at=snapshot.last_success_at,
            dropped_items=snapshot.dropped_items,
            last_error_code=snapshot.last_error_code,
        )

    def shutdown(self, timeout_millis: int = 5_000) -> None:
        if timeout_millis < 0:
            raise ValueError("timeout_millis must not be negative")
        deadline = time.monotonic() + timeout_millis / 1_000
        owns_shutdown = False
        global _runtime
        with _BOOTSTRAP_LOCK, self._shutdown_condition:
            if self._shutdown_state == "complete":
                return
            if self._shutdown_state == "running":
                self._shutdown_state = "shutting_down"
                if _runtime is self:
                    _runtime = None
                owns_shutdown = True

        if not owns_shutdown:
            with self._shutdown_condition:
                while not self._shutdown_complete:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    self._shutdown_condition.wait(timeout=remaining)
            return

        try:
            if self._owns_providers:
                for callback in reversed(self._shutdown_callbacks):
                    remaining = max(0, int((deadline - time.monotonic()) * 1_000))
                    try:
                        callback(remaining)
                    except Exception:
                        continue
        finally:
            with self._shutdown_condition:
                self._shutdown_state = "complete"
                self._shutdown_complete = True
                self._shutdown_condition.notify_all()


_BOOTSTRAP_LOCK = threading.Lock()
_runtime: ObservabilityRuntime | None = None


def _clear_runtime_if_owner(runtime: ObservabilityRuntime) -> None:
    global _runtime
    with _BOOTSTRAP_LOCK:
        if _runtime is runtime:
            _runtime = None


def get_runtime() -> ObservabilityRuntime:
    with _BOOTSTRAP_LOCK:
        runtime = _runtime
        if runtime is None:
            raise ObservabilityNotInitializedError("observability is not initialized")
        with runtime._shutdown_condition:
            if runtime._shutdown_state != "running":
                raise ObservabilityNotInitializedError("observability is not initialized")
        return runtime


def initialize_observability(config: ObservabilityConfig) -> ObservabilityRuntime:
    global _runtime
    with _BOOTSTRAP_LOCK:
        if _runtime is not None:
            with _runtime._shutdown_condition:
                running = _runtime._shutdown_state == "running"
            if running and _runtime.config == config:
                return _runtime
            raise ObservabilityConfigurationError("observability already initialized")
        runtime = _construct_runtime(config)
        _runtime = runtime
        return runtime


def _construct_runtime(config: ObservabilityConfig) -> ObservabilityRuntime:
    configure_json_logging(
        config.service_name,
        config.environment,
        extra_processors=config.extra_log_processors,
    )
    if config.otlp_endpoint is None:
        tracer_provider = NoOpTracerProvider()
        meter_provider = NoOpMeterProvider()
        return ObservabilityRuntime(
            config=config,
            tracer=tracer_provider.get_tracer(
                "jhin-observability",
                instrumenting_library_version=config.service_version,
            ),
            meter=meter_provider.get_meter(
                "jhin-observability",
                version=config.service_version,
            ),
            metrics=noop_metrics(),
            _diagnostics=ExportDiagnostics(frozenset()),
            _owns_providers=False,
        )
    return _construct_configured_runtime(config)


def _construct_configured_runtime(config: ObservabilityConfig) -> ObservabilityRuntime:
    deadline = time.monotonic() + config.export_timeout_millis / 1_000
    cleanup_callbacks: list[Callable[[int], None]] = []
    try:
        credentials = _build_channel_credentials(config)

        raw_span_exporter = _create_otlp_span_exporter(config, credentials)
        cleanup_callbacks.append(
            lambda remaining: _shutdown_span_exporter(raw_span_exporter, remaining)
        )
        diagnostics = ExportDiagnostics(frozenset({"traces", "metrics"}))
        span_exporter = DiagnosticSpanExporter(raw_span_exporter, diagnostics)
        span_processor = _create_span_processor(span_exporter, diagnostics, config)
        cleanup_callbacks[-1] = span_processor.shutdown

        raw_metric_exporter = _create_otlp_metric_exporter(config, credentials)
        cleanup_callbacks.append(
            lambda remaining: _shutdown_metric_exporter(raw_metric_exporter, remaining)
        )
        metric_exporter = DiagnosticMetricExporter(raw_metric_exporter, diagnostics)
        metric_reader = _create_metric_reader(metric_exporter, config)
        cleanup_callbacks[-1] = lambda remaining: _shutdown_metric_reader(
            metric_reader,
            remaining,
        )

        resource = Resource(
            {
                "service.name": config.service_name,
                "service.version": config.service_version,
                "deployment.environment.name": config.environment,
            }
        )
        tracer_provider = _create_tracer_provider(resource, config)
        tracer_provider.add_span_processor(span_processor)
        meter_provider = _create_meter_provider(resource, metric_reader)
        cleanup_callbacks[-1] = lambda remaining: _shutdown_meter_provider(
            meter_provider,
            metric_reader,
            remaining,
        )

        return ObservabilityRuntime(
            config=config,
            tracer=tracer_provider.get_tracer(
                "jhin-observability",
                instrumenting_library_version=config.service_version,
            ),
            meter=meter_provider.get_meter(
                "jhin-observability",
                version=config.service_version,
            ),
            metrics=noop_metrics(),
            _diagnostics=diagnostics,
            _owns_providers=True,
            _shutdown_callbacks=tuple(cleanup_callbacks),
        )
    except Exception:
        _run_cleanup_callbacks(cleanup_callbacks, deadline)
        raise


def _run_cleanup_callbacks(callbacks: list[Callable[[int], None]], deadline: float) -> None:
    for callback in reversed(callbacks):
        remaining = max(0, int((deadline - time.monotonic()) * 1_000))
        try:
            callback(remaining)
        except Exception:
            continue


def _shutdown_span_exporter(exporter: SpanExporter, timeout_millis: int) -> None:
    def shutdown() -> None:
        try:
            exporter.shutdown()
        except Exception:
            return

    worker = threading.Thread(
        target=shutdown,
        name="jhin-otel-partial-span-exporter-shutdown",
        daemon=True,
    )
    worker.start()
    worker.join(timeout=max(0, timeout_millis) / 1_000)


def _request_bounded_shutdown(
    shutdown: Callable[[], None],
    *,
    thread_name: str,
    timeout_millis: int,
) -> None:
    worker = threading.Thread(
        target=shutdown,
        name=thread_name,
        daemon=True,
    )
    worker.start()
    worker.join(timeout=max(0, timeout_millis) / 1_000)


def _shutdown_metric_exporter(exporter: MetricExporter, timeout_millis: int) -> None:
    def shutdown() -> None:
        with suppress(Exception):
            exporter.shutdown(timeout_millis=timeout_millis)

    _request_bounded_shutdown(
        shutdown,
        thread_name="jhin-otel-partial-metric-exporter-shutdown",
        timeout_millis=timeout_millis,
    )


def _shutdown_metric_reader(reader: MetricReader, timeout_millis: int) -> None:
    def shutdown() -> None:
        with suppress(Exception):
            reader.shutdown(timeout_millis=timeout_millis)

    _request_bounded_shutdown(
        shutdown,
        thread_name="jhin-otel-partial-metric-reader-shutdown",
        timeout_millis=timeout_millis,
    )


def _shutdown_meter_provider(
    provider: MeterProvider,
    reader: MetricReader,
    timeout_millis: int,
) -> None:
    def shutdown() -> None:
        if timeout_millis == 0:
            with suppress(Exception):
                provider.shutdown(timeout_millis=0)
            with suppress(Exception):
                reader.shutdown(timeout_millis=0)
            return
        with suppress(Exception):
            provider.shutdown(timeout_millis=timeout_millis)

    _request_bounded_shutdown(
        shutdown,
        thread_name="jhin-otel-meter-provider-shutdown",
        timeout_millis=timeout_millis,
    )


def _read_tls_file(path: Path | None) -> bytes | None:
    if path is None:
        return None
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or not os.access(path, os.R_OK):
            raise OSError
        return path.read_bytes()
    except OSError:
        raise ObservabilityConfigurationError("OTLP TLS credential file is unavailable") from None


def _build_channel_credentials(
    config: ObservabilityConfig,
) -> grpc.ChannelCredentials | None:
    if config.otlp_endpoint is None or config.otlp_endpoint.startswith("http://"):
        return None
    ca_bytes = _read_tls_file(config.otlp_ca_file)
    certificate_bytes = _read_tls_file(config.otlp_client_certificate_file)
    key_bytes = _read_tls_file(config.otlp_client_key_file)
    return grpc.ssl_channel_credentials(
        root_certificates=ca_bytes,
        private_key=key_bytes,
        certificate_chain=certificate_bytes,
    )


def _create_otlp_span_exporter(
    config: ObservabilityConfig,
    credentials: grpc.ChannelCredentials | None,
) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(
        endpoint=config.otlp_endpoint,
        insecure=config.otlp_insecure,
        credentials=credentials,
        timeout=config.export_timeout_millis / 1_000,
    )


def _create_otlp_metric_exporter(
    config: ObservabilityConfig,
    credentials: grpc.ChannelCredentials | None,
) -> MetricExporter:
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    return OTLPMetricExporter(
        endpoint=config.otlp_endpoint,
        insecure=config.otlp_insecure,
        credentials=credentials,
        timeout=config.export_timeout_millis / 1_000,
    )


def _create_span_processor(
    exporter: SpanExporter,
    diagnostics: ExportDiagnostics,
    config: ObservabilityConfig,
) -> BoundedBatchSpanProcessor:
    return BoundedBatchSpanProcessor(
        exporter,
        diagnostics=diagnostics,
        max_queue_size=config.span_queue_size,
        max_export_batch_size=config.span_export_batch_size,
        export_timeout_millis=config.export_timeout_millis,
    )


def _create_metric_reader(
    exporter: MetricExporter,
    config: ObservabilityConfig,
) -> MetricReader:
    return PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=config.metric_export_interval_millis,
        export_timeout_millis=config.export_timeout_millis,
    )


def _sampler(config: ObservabilityConfig) -> Sampler:
    if config.trace_sampler == "always_on":
        return ALWAYS_ON
    if config.trace_sampler == "always_off":
        return ALWAYS_OFF
    return ParentBased(TraceIdRatioBased(config.trace_sample_ratio))


def _create_tracer_provider(
    resource: Resource,
    config: ObservabilityConfig,
) -> TracerProvider:
    return TracerProvider(
        resource=resource,
        sampler=_sampler(config),
        shutdown_on_exit=False,
    )


def _create_meter_provider(resource: Resource, reader: MetricReader) -> MeterProvider:
    return MeterProvider(
        metric_readers=(reader,),
        resource=resource,
        shutdown_on_exit=False,
    )


def _reset_observability_for_test() -> None:
    with _BOOTSTRAP_LOCK:
        runtime = _runtime
    if runtime is not None:
        runtime.shutdown()
    _clear_runtime_if_owner(runtime) if runtime is not None else None


__all__ = [
    "ObservabilityRuntime",
    "TelemetryExporterStatus",
    "get_runtime",
    "initialize_observability",
]
