"""Finite, fail-open exporter adapters with closed diagnostics."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from opentelemetry.context import Context
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Span

from jhin_observability.logging import get_logger

ExportSignal = Literal["traces", "metrics"]
ExportErrorCode = Literal["export_timeout", "export_failed"]
logger = get_logger(__name__)


@dataclass(frozen=True)
class ExportDiagnosticsSnapshot:
    last_success_at: datetime | None
    dropped_items: int
    last_error_code: ExportErrorCode | None


class ExportDiagnostics:
    """Thread-safe diagnostics aggregated across configured signal sources."""

    def __init__(self, active_signals: frozenset[ExportSignal]) -> None:
        self._lock = threading.Lock()
        self._active_signals = active_signals
        self._dropped_total = 0
        self._dropped_reported = 0
        self._last_success_by_signal: dict[ExportSignal, datetime | None] = dict.fromkeys(
            active_signals
        )
        self._error_by_signal: dict[ExportSignal, ExportErrorCode | None] = dict.fromkeys(
            active_signals
        )
        self.drop_event_emitted = threading.Event()

    def increment_dropped_atomic(self) -> None:
        with self._lock:
            self._dropped_total += 1

    def take_unreported_drop_delta(self) -> int:
        with self._lock:
            delta = self._dropped_total - self._dropped_reported
            self._dropped_reported = self._dropped_total
            return delta

    def record_success(self, source: ExportSignal, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("export success timestamp must be timezone-aware")
        with self._lock:
            self._require_active(source)
            failed_before = any(code is not None for code in self._error_by_signal.values())
            self._last_success_by_signal[source] = at.astimezone(UTC)
            self._error_by_signal[source] = None
            failed_after = any(code is not None for code in self._error_by_signal.values())
            return failed_before and not failed_after

    def record_failure(self, source: ExportSignal, code: ExportErrorCode) -> None:
        if code not in {"export_timeout", "export_failed"}:
            raise ValueError("unregistered telemetry export error code")
        with self._lock:
            self._require_active(source)
            self._error_by_signal[source] = code

    def snapshot(self) -> ExportDiagnosticsSnapshot:
        with self._lock:
            errors = tuple(self._error_by_signal.values())
            if "export_timeout" in errors:
                last_error_code: ExportErrorCode | None = "export_timeout"
            elif "export_failed" in errors:
                last_error_code = "export_failed"
            else:
                last_error_code = None
            successes = tuple(self._last_success_by_signal.values())
            last_success_at = (
                min(cast(tuple[datetime, ...], successes))
                if successes and all(value is not None for value in successes)
                else None
            )
            return ExportDiagnosticsSnapshot(
                last_success_at=last_success_at,
                dropped_items=self._dropped_total,
                last_error_code=last_error_code,
            )

    def _require_active(self, source: ExportSignal) -> None:
        if source not in self._active_signals:
            raise ValueError("inactive telemetry export signal")


def _failure_code(error: BaseException) -> ExportErrorCode:
    return "export_timeout" if isinstance(error, TimeoutError) else "export_failed"


def _emit_failure(code: ExportErrorCode) -> None:
    logger.warning("telemetry.export_failed", error_code=code)


def _emit_recovered() -> None:
    logger.info("telemetry.export_recovered")


class DiagnosticSpanExporter(SpanExporter):
    """Span exporter wrapper that never exposes delegate failures or details."""

    reports_diagnostics = True

    def __init__(self, delegate: SpanExporter, diagnostics: ExportDiagnostics) -> None:
        self._delegate = delegate
        self._diagnostics = diagnostics

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._delegate.export(spans)
        except Exception as error:
            code = _failure_code(error)
            self._diagnostics.record_failure("traces", code)
            _emit_failure(code)
            return SpanExportResult.FAILURE
        if result is SpanExportResult.SUCCESS:
            if self._diagnostics.record_success("traces", datetime.now(UTC)):
                _emit_recovered()
        else:
            self._diagnostics.record_failure("traces", "export_failed")
            _emit_failure("export_failed")
        return result

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:
            return None


class DiagnosticMetricExporter(MetricExporter):
    """Metric exporter wrapper sharing aggregate health with trace export."""

    def __init__(self, delegate: MetricExporter, diagnostics: ExportDiagnostics) -> None:
        delegate_values = cast(Any, delegate)
        super().__init__(
            preferred_temporality=getattr(delegate_values, "_preferred_temporality", None),
            preferred_aggregation=getattr(delegate_values, "_preferred_aggregation", None),
        )
        self._delegate = delegate
        self._diagnostics = diagnostics

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: Any,
    ) -> MetricExportResult:
        try:
            result = self._delegate.export(
                metrics_data,
                timeout_millis=timeout_millis,
                **kwargs,
            )
        except Exception as error:
            code = _failure_code(error)
            self._diagnostics.record_failure("metrics", code)
            _emit_failure(code)
            return MetricExportResult.FAILURE
        if result is MetricExportResult.SUCCESS:
            if self._diagnostics.record_success("metrics", datetime.now(UTC)):
                _emit_recovered()
        else:
            self._diagnostics.record_failure("metrics", "export_failed")
            _emit_failure("export_failed")
        return result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        try:
            return self._delegate.force_flush(timeout_millis=timeout_millis)
        except Exception:
            return False

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        try:
            self._delegate.shutdown(timeout_millis=timeout_millis, **kwargs)
        except Exception:
            return None


class BoundedBatchSpanProcessor(SpanProcessor):
    """A single-daemon span exporter with finite queue and bounded callers."""

    _DROP_REPORT_INTERVAL_SECONDS = 30.0

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        diagnostics: ExportDiagnostics,
        max_queue_size: int,
        max_export_batch_size: int,
        export_timeout_millis: int,
    ) -> None:
        if max_queue_size <= 0 or max_export_batch_size <= 0:
            raise ValueError("span queue and batch sizes must be positive")
        if max_export_batch_size > max_queue_size:
            raise ValueError("span export batch size cannot exceed queue size")
        if export_timeout_millis <= 0:
            raise ValueError("span export timeout must be positive")
        self._exporter = exporter
        self.diagnostics = diagnostics
        self._queue: queue.Queue[ReadableSpan] = queue.Queue(maxsize=max_queue_size)
        self._queue_capacity = max_queue_size
        self._max_export_batch_size = max_export_batch_size
        self._export_timeout_millis = export_timeout_millis
        self._shutdown_requested = threading.Event()
        self._all_done = threading.Event()
        self._all_done.set()
        self._exporter_shutdown_lock = threading.Lock()
        self._exporter_shutdown_thread: threading.Thread | None = None
        self.stopped = threading.Event()
        self._last_drop_report_at: float | None = None
        self.worker_thread = threading.Thread(
            target=self._run,
            name="jhin-otel-span-exporter",
            daemon=True,
        )
        self.worker_thread.start()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        if self._shutdown_requested.is_set():
            self.diagnostics.increment_dropped_atomic()
            return
        try:
            self._queue.put_nowait(span)
        except queue.Full:
            self.diagnostics.increment_dropped_atomic()
            return
        self._all_done.clear()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        if timeout_millis < 0:
            return False
        return self._all_done.wait(timeout=timeout_millis / 1_000)

    def shutdown(self, timeout_millis: int = 5_000) -> None:
        if timeout_millis < 0:
            raise ValueError("timeout_millis must not be negative")
        deadline = time.monotonic() + timeout_millis / 1_000
        self._shutdown_requested.set()
        self._all_done.wait(timeout=max(0.0, deadline - time.monotonic()))
        if not self.stopped.is_set():
            self._request_exporter_shutdown(max(0, int((deadline - time.monotonic()) * 1_000)))
        self.stopped.wait(timeout=max(0.0, deadline - time.monotonic()))

    def _request_exporter_shutdown(self, timeout_millis: int) -> None:
        with self._exporter_shutdown_lock:
            worker = self._exporter_shutdown_thread
            if worker is None:
                worker = threading.Thread(
                    target=self._shutdown_exporter,
                    name="jhin-otel-span-exporter-shutdown",
                    daemon=True,
                )
                self._exporter_shutdown_thread = worker
                worker.start()
        worker.join(timeout=max(0, timeout_millis) / 1_000)

    def _shutdown_exporter(self) -> None:
        with suppress(Exception):
            self._exporter.shutdown()

    def _run(self) -> None:
        try:
            while not self._shutdown_requested.is_set() or not self._queue.empty():
                try:
                    first = self._queue.get(timeout=0.05)
                except queue.Empty:
                    self._report_drops_from_exporter_thread()
                    continue
                batch = [first]
                while len(batch) < self._max_export_batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                try:
                    result = self._exporter.export(batch)
                    if not getattr(self._exporter, "reports_diagnostics", False):
                        if result is SpanExportResult.SUCCESS:
                            if self.diagnostics.record_success("traces", datetime.now(UTC)):
                                _emit_recovered()
                        else:
                            self.diagnostics.record_failure("traces", "export_failed")
                            _emit_failure("export_failed")
                except Exception as error:
                    if not getattr(self._exporter, "reports_diagnostics", False):
                        code = _failure_code(error)
                        self.diagnostics.record_failure("traces", code)
                        _emit_failure(code)
                finally:
                    for _span in batch:
                        self._queue.task_done()
                    if self._queue.unfinished_tasks == 0:
                        self._all_done.set()
                self._report_drops_from_exporter_thread()
        finally:
            self._report_drops_from_exporter_thread()
            self._request_exporter_shutdown(self._export_timeout_millis)
            self._all_done.set()
            self.stopped.set()

    def _report_drops_from_exporter_thread(self) -> None:
        now = time.monotonic()
        if (
            self._last_drop_report_at is not None
            and now - self._last_drop_report_at < self._DROP_REPORT_INTERVAL_SECONDS
        ):
            return
        delta = self.diagnostics.take_unreported_drop_delta()
        if delta <= 0:
            return
        self._last_drop_report_at = now
        logger.warning(
            "telemetry.queue_dropped",
            count=delta,
            queue_capacity=self._queue_capacity,
        )
        self.diagnostics.drop_event_emitted.set()


__all__ = [
    "BoundedBatchSpanProcessor",
    "DiagnosticMetricExporter",
    "DiagnosticSpanExporter",
    "ExportDiagnostics",
    "ExportDiagnosticsSnapshot",
    "ExportErrorCode",
    "ExportSignal",
]
