"""Bounded, fail-open exporter and source-aware diagnostic tests."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from _pytest.capture import CaptureFixture
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext, TraceFlags, TraceState

from jhin_observability import configure_json_logging
from jhin_observability.exporters import (
    BoundedBatchSpanProcessor,
    DiagnosticMetricExporter,
    DiagnosticSpanExporter,
    ExportDiagnostics,
)


def readable_span(index: int) -> ReadableSpan:
    return ReadableSpan(
        name=f"test.{index}",
        context=SpanContext(
            trace_id=index + 1,
            span_id=index + 1,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        ),
        resource=Resource({}),
        start_time=index,
        end_time=index + 1,
    )


class ReleasableBlockingSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.entered.set()
        self.release.wait()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.release.set()


class FailingSpanExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise RuntimeError("https://secret:credential@collector.example.test/span-canary")

    def shutdown(self) -> None:
        return None


class MutableSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.result = SpanExportResult.SUCCESS

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.result

    def shutdown(self) -> None:
        return None


class MutableMetricExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.result = MetricExportResult.SUCCESS

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object
    ) -> MetricExportResult:
        return self.result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        return None


def stop_processor(
    processor: BoundedBatchSpanProcessor,
    exporter: ReleasableBlockingSpanExporter | None = None,
) -> None:
    if exporter is not None:
        exporter.release.set()
    processor.shutdown(timeout_millis=250)
    assert processor.stopped.wait(timeout=1.0)
    processor.worker_thread.join(timeout=1.0)
    assert processor.worker_thread.is_alive() is False


def test_full_span_queue_only_increments_atomic_drop_counter_on_product_thread(
    capsys: CaptureFixture[str],
) -> None:
    configure_json_logging("test", "test")
    exporter = ReleasableBlockingSpanExporter()
    diagnostics = ExportDiagnostics(frozenset({"traces"}))
    processor = BoundedBatchSpanProcessor(
        exporter,
        diagnostics=diagnostics,
        max_queue_size=2,
        max_export_batch_size=1,
        export_timeout_millis=25,
    )
    try:
        started = time.perf_counter()
        for index in range(50):
            processor.on_end(readable_span(index))
        elapsed = time.perf_counter() - started
        assert elapsed < 0.050
        assert diagnostics.snapshot().dropped_items >= 47
        assert capsys.readouterr().out == ""
        exporter.release.set()
        assert diagnostics.drop_event_emitted.wait(timeout=1.0)
        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        dropped = [record for record in records if record["event"] == "telemetry.queue_dropped"]
        assert len(dropped) == 1
        assert dropped[0]["count"] >= 47
        assert dropped[0]["queue_capacity"] == 2
    finally:
        stop_processor(processor, exporter)


def test_export_failure_is_safe_and_does_not_raise_to_caller(
    capsys: CaptureFixture[str],
) -> None:
    configure_json_logging("test", "test")
    processor = BoundedBatchSpanProcessor(
        FailingSpanExporter(),
        diagnostics=ExportDiagnostics(frozenset({"traces"})),
        max_queue_size=4,
        max_export_batch_size=2,
        export_timeout_millis=25,
    )
    try:
        processor.on_end(readable_span(1))
        assert processor.force_flush(timeout_millis=100) is True
        status = processor.diagnostics.snapshot()
        assert status.last_error_code == "export_failed"
        record = json.loads(capsys.readouterr().out)
        assert record["event"] == "telemetry.export_failed"
        assert record["error_code"] == "export_failed"
        assert "endpoint" not in record
        assert "exception" not in record
        assert "signal" not in record
        assert "span-canary" not in json.dumps(record)
    finally:
        stop_processor(processor)


def test_force_flush_and_shutdown_obey_deadline_when_exporter_is_blocked() -> None:
    exporter = ReleasableBlockingSpanExporter()
    processor = BoundedBatchSpanProcessor(
        exporter,
        diagnostics=ExportDiagnostics(frozenset({"traces"})),
        max_queue_size=4,
        max_export_batch_size=1,
        export_timeout_millis=25,
    )
    processor.on_end(readable_span(1))
    assert exporter.entered.wait(timeout=1.0)
    started = time.monotonic()
    assert processor.force_flush(timeout_millis=30) is False
    processor.shutdown(timeout_millis=30)
    assert time.monotonic() - started < 0.100
    exporter.release.set()
    assert processor.stopped.wait(timeout=1.0)
    processor.worker_thread.join(timeout=1.0)
    assert processor.worker_thread.is_alive() is False


def test_shutdown_releases_blocked_exporter_within_deadline() -> None:
    exporter = ReleasableBlockingSpanExporter()
    processor = BoundedBatchSpanProcessor(
        exporter,
        diagnostics=ExportDiagnostics(frozenset({"traces"})),
        max_queue_size=4,
        max_export_batch_size=1,
        export_timeout_millis=25,
    )
    processor.on_end(readable_span(1))
    assert exporter.entered.wait(timeout=1.0)
    started = time.monotonic()
    processor.shutdown(timeout_millis=30)
    assert time.monotonic() - started < 0.100
    assert processor.stopped.wait(timeout=1.0)
    processor.worker_thread.join(timeout=1.0)
    assert processor.worker_thread.is_alive() is False


def test_drop_reporting_is_aggregated_at_most_once_per_window(
    capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_json_logging("test", "test")
    exporter = ReleasableBlockingSpanExporter()
    diagnostics = ExportDiagnostics(frozenset({"traces"}))
    monotonic_now = [10.0]
    monkeypatch.setattr(
        "jhin_observability.exporters.time.monotonic",
        lambda: monotonic_now[0],
    )
    processor = BoundedBatchSpanProcessor(
        exporter,
        diagnostics=diagnostics,
        max_queue_size=1,
        max_export_batch_size=1,
        export_timeout_millis=25,
    )
    try:
        diagnostics.increment_dropped_atomic()
        diagnostics.increment_dropped_atomic()
        assert diagnostics.drop_event_emitted.wait(timeout=1.0)
        first_records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        diagnostics.drop_event_emitted.clear()
        diagnostics.increment_dropped_atomic()
        monotonic_now[0] = 20.0
        assert diagnostics.drop_event_emitted.wait(timeout=0.1) is False
        monotonic_now[0] = 41.0
        assert diagnostics.drop_event_emitted.wait(timeout=1.0)
        second_records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [record["count"] for record in [*first_records, *second_records]] == [2, 1]
    finally:
        exporter.release.set()
        stop_processor(processor, exporter)


def test_source_aware_diagnostics_preserve_intermediate_failures() -> None:
    diagnostics = ExportDiagnostics(frozenset({"traces", "metrics"}))
    first = datetime(2026, 8, 20, 12, tzinfo=UTC)
    second = first + timedelta(seconds=1)

    diagnostics.record_failure("traces", "export_failed")
    assert diagnostics.record_success("metrics", second) is False
    assert diagnostics.snapshot().last_error_code == "export_failed"
    assert diagnostics.snapshot().last_success_at is None

    diagnostics.record_failure("metrics", "export_timeout")
    assert diagnostics.record_success("traces", first) is False
    snapshot = diagnostics.snapshot()
    assert snapshot.last_error_code == "export_timeout"
    assert snapshot.last_success_at == first

    assert diagnostics.record_success("metrics", second) is True
    snapshot = diagnostics.snapshot()
    assert snapshot.last_error_code is None
    assert snapshot.last_success_at == first
    assert diagnostics.record_success("metrics", second) is False


def test_metric_failure_trace_success_remains_failed_until_metric_recovers() -> None:
    diagnostics = ExportDiagnostics(frozenset({"traces", "metrics"}))
    now = datetime.now(UTC)
    diagnostics.record_failure("metrics", "export_failed")
    assert diagnostics.record_success("traces", now) is False
    assert diagnostics.snapshot().last_error_code == "export_failed"
    assert diagnostics.record_success("metrics", now) is True


def test_diagnostic_wrappers_emit_exactly_one_final_recovery(
    capsys: CaptureFixture[str],
) -> None:
    configure_json_logging("test", "test")
    diagnostics = ExportDiagnostics(frozenset({"traces", "metrics"}))
    span_delegate = MutableSpanExporter()
    metric_delegate = MutableMetricExporter()
    span = DiagnosticSpanExporter(span_delegate, diagnostics)
    metric = DiagnosticMetricExporter(metric_delegate, diagnostics)

    span_delegate.result = SpanExportResult.FAILURE
    assert span.export((readable_span(1),)) is SpanExportResult.FAILURE
    metric_delegate.result = MetricExportResult.FAILURE
    assert metric.export(MetricsData(())) is MetricExportResult.FAILURE
    span_delegate.result = SpanExportResult.SUCCESS
    assert span.export((readable_span(2),)) is SpanExportResult.SUCCESS
    metric_delegate.result = MetricExportResult.SUCCESS
    assert metric.export(MetricsData(())) is MetricExportResult.SUCCESS

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sum(record["event"] == "telemetry.export_recovered" for record in records) == 1
    assert all("signal" not in record for record in records)
    assert diagnostics.snapshot().last_error_code is None


def test_diagnostics_reject_unknown_sources_naive_timestamps_and_codes() -> None:
    diagnostics = ExportDiagnostics(frozenset({"traces"}))
    with pytest.raises(ValueError, match="inactive telemetry export signal"):
        diagnostics.record_failure("metrics", "export_failed")
    with pytest.raises(ValueError, match="timezone-aware"):
        diagnostics.record_success("traces", datetime(2026, 8, 20))
    with pytest.raises(ValueError, match="unregistered telemetry export error code"):
        diagnostics.record_failure("traces", "credential-canary")  # type: ignore[arg-type]
