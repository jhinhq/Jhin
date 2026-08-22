"""Provider-neutral model telemetry contract tests.

The production telemetry module is deliberately imported late.  That keeps
the first TDD run collectable while the module is still absent and makes the
RED a contract assertion instead of an import accident.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import json
import logging
import math
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, Tracer

from jhin_domain import ModelProviderType
from jhin_models.base import (
    ModelClient,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ToolSchema,
)
from jhin_observability import JhinMetrics, noop_metrics, noop_tracer
from jhin_observability.metrics import build_jhin_metrics

REPO_ROOT = Path(__file__).resolve().parents[3]


class WorkError(RuntimeError):
    pass


class CleanupError(RuntimeError):
    pass


class HostileTelemetryError(RuntimeError):
    pass


def _telemetry_module() -> Any:
    spec = importlib.util.find_spec("jhin_models.telemetry")
    assert spec is not None, "jhin_models.telemetry must provide the model wrapper"
    return importlib.import_module("jhin_models.telemetry")


def _instrumented(
    wrapped: ModelClient,
    telemetry: _Telemetry,
    *,
    provider_type: object = "openai",
    metrics: JhinMetrics | None = None,
    tracer: Tracer | None = None,
) -> ModelClient:
    wrapper = _telemetry_module().InstrumentedModelClient
    return cast(
        ModelClient,
        wrapper(
            wrapped,
            provider_type=provider_type,
            metrics=telemetry.metrics if metrics is None else metrics,
            tracer=telemetry.tracer if tracer is None else tracer,
        ),
    )


def model_request(*, canary: str = "request-body-canary") -> ModelRequest:
    return ModelRequest(
        model=f"private-model-{canary}",
        messages=(
            ModelMessage(role="user", content=canary),
            ModelMessage(
                role="assistant",
                content=f"assistant-{canary}",
                tool_calls=(
                    ModelToolCall(
                        id=f"call-{canary}",
                        name=f"tool-{canary}",
                        arguments_json=json.dumps({"secret": canary}),
                    ),
                ),
            ),
        ),
        tools=(
            ToolSchema(
                name=f"schema-{canary}",
                description=f"description-{canary}",
                parameters={"private": canary},
            ),
        ),
        extra={"private": canary},
    )


class FakeModelClient(ModelClient):
    def __init__(
        self,
        *,
        response: ModelResponse | None = None,
        verify_result: object = "verified",
        generate_error: BaseException | None = None,
        verify_error: BaseException | None = None,
        stream_factory: Callable[[], AsyncIterator[str]] | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.response = response or ModelResponse(text="completion", latency_ms=17)
        self.verify_result = verify_result
        self.generate_error = generate_error
        self.verify_error = verify_error
        self.stream_factory = stream_factory or (lambda: CloseableIterator(("chunk",)))
        self.close_error = close_error
        self.generate_calls = 0
        self.verify_calls = 0
        self.stream_calls = 0
        self.close_calls = 0
        self.raised_traceback: TracebackType | None = None

    @staticmethod
    def _raise(error: BaseException) -> None:
        raise error

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.generate_calls += 1
        if self.generate_error is not None:
            try:
                self._raise(self.generate_error)
            except BaseException as error:
                self.raised_traceback = error.__traceback__
                raise
        return self.response

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        self.stream_calls += 1
        return self.stream_factory()

    async def verify(self) -> str:
        self.verify_calls += 1
        if self.verify_error is not None:
            try:
                self._raise(self.verify_error)
            except BaseException as error:
                self.raised_traceback = error.__traceback__
                raise
        return cast(str, self.verify_result)

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            try:
                self._raise(self.close_error)
            except BaseException as error:
                self.raised_traceback = error.__traceback__
                raise


class CloseableIterator(AsyncIterator[str]):
    def __init__(
        self,
        chunks: tuple[object, ...],
        *,
        work_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self.chunks = chunks
        self.work_error = work_error
        self.cleanup_error = cleanup_error
        self.index = 0
        self.aclose_calls = 0
        self.seen_spans: list[tuple[bool, object]] = []
        self.work_traceback: TracebackType | None = None
        self.cleanup_traceback: TracebackType | None = None

    def __aiter__(self) -> CloseableIterator:
        return self

    async def __anext__(self) -> str:
        current = trace.get_current_span()
        self.seen_spans.append((current.is_recording(), current.get_span_context()))
        if self.index < len(self.chunks):
            chunk = self.chunks[self.index]
            self.index += 1
            return cast(str, chunk)
        if self.work_error is not None:
            try:
                raise self.work_error
            except BaseException as error:
                self.work_traceback = error.__traceback__
                raise
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_calls += 1
        if self.cleanup_error is not None:
            try:
                raise self.cleanup_error
            except BaseException as error:
                self.cleanup_traceback = error.__traceback__
                raise


class ProtocolOnlyIterator(AsyncIterator[str]):
    def __init__(
        self,
        chunks: tuple[object, ...],
        *,
        work_error: BaseException | None = None,
    ) -> None:
        self.chunks = chunks
        self.work_error = work_error
        self.index = 0
        self.seen_spans: list[tuple[bool, object]] = []

    def __aiter__(self) -> ProtocolOnlyIterator:
        return self

    async def __anext__(self) -> str:
        current = trace.get_current_span()
        self.seen_spans.append((current.is_recording(), current.get_span_context()))
        if self.index < len(self.chunks):
            chunk = self.chunks[self.index]
            self.index += 1
            return cast(str, chunk)
        if self.work_error is not None:
            raise self.work_error
        raise StopAsyncIteration


class BlockingCleanupIterator(AsyncIterator[str]):
    def __init__(self) -> None:
        self.work_started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.aclose_calls = 0
        self.first_cancellation: asyncio.CancelledError | None = None

    def __aiter__(self) -> BlockingCleanupIterator:
        return self

    async def __anext__(self) -> str:
        self.work_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            self.first_cancellation = error
            raise
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.cleanup_started.set()
        await asyncio.Event().wait()


@dataclass
class _Telemetry:
    metrics: JhinMetrics
    reader: InMemoryMetricReader
    metric_provider: MeterProvider
    tracer: Tracer
    exporter: InMemorySpanExporter
    trace_provider: TracerProvider


@pytest.fixture
def telemetry() -> Iterator[_Telemetry]:
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
    metrics = build_jhin_metrics(cast(Meter, metric_provider.get_meter("model-test", "1")))

    exporter = InMemorySpanExporter()
    trace_provider = TracerProvider(
        resource=Resource.create({"service.name": "model-test", "safe.resource": "bounded"}),
        shutdown_on_exit=False,
    )
    trace_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = trace_provider.get_tracer("model-test", "1")
    owned = _Telemetry(metrics, reader, metric_provider, tracer, exporter, trace_provider)
    try:
        yield owned
    finally:
        assert otel_context.get_current() is entry_context
        assert trace.get_current_span() is entry_span
        trace_provider.shutdown()
        metric_provider.shutdown()


def _metric_points(telemetry: _Telemetry, name: str) -> list[Any]:
    data = telemetry.reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def metric_sum(telemetry: _Telemetry, name: str, **labels: str) -> float:
    return sum(
        float(point.value)
        for point in _metric_points(telemetry, name)
        if dict(point.attributes) == labels
    )


def _model_spans(telemetry: _Telemetry) -> list[Any]:
    return [
        span for span in telemetry.exporter.get_finished_spans() if span.name == "model.request"
    ]


def _only_model_span(telemetry: _Telemetry) -> Any:
    spans = _model_spans(telemetry)
    assert len(spans) == 1
    return spans[0]


def _traceback_contains(head: TracebackType | None, expected: TracebackType | None) -> bool:
    while head is not None:
        if head is expected:
            return True
        head = head.tb_next
    return False


def _point_payload(point: Any) -> dict[str, Any]:
    exemplars = []
    for exemplar in getattr(point, "exemplars", ()) or ():
        exemplars.append(
            {
                "attributes": dict(getattr(exemplar, "filtered_attributes", {}) or {}),
                "value": getattr(exemplar, "value", None),
                "time_unix_nano": getattr(exemplar, "time_unix_nano", None),
                "span_id": getattr(exemplar, "span_id", None),
                "trace_id": getattr(exemplar, "trace_id", None),
            }
        )
    return {
        "attributes": dict(getattr(point, "attributes", {}) or {}),
        "start_time_unix_nano": getattr(point, "start_time_unix_nano", None),
        "time_unix_nano": getattr(point, "time_unix_nano", None),
        "value": getattr(point, "value", None),
        "sum": getattr(point, "sum", None),
        "count": getattr(point, "count", None),
        "min": getattr(point, "min", None),
        "max": getattr(point, "max", None),
        "bucket_counts": list(getattr(point, "bucket_counts", ()) or ()),
        "explicit_bounds": list(getattr(point, "explicit_bounds", ()) or ()),
        "exemplars": exemplars,
    }


def complete_export_payload(telemetry: _Telemetry) -> str:
    """Serialize every trace/metric field that may carry user material."""
    spans: list[dict[str, Any]] = []
    for span in telemetry.exporter.get_finished_spans():
        context = span.context
        parent = span.parent
        spans.append(
            {
                "name": span.name,
                "kind": span.kind.name,
                "attributes": dict(span.attributes or {}),
                "status": {
                    "code": span.status.status_code.name,
                    "description": span.status.description,
                },
                "events": [
                    {
                        "name": event.name,
                        "timestamp": event.timestamp,
                        "attributes": dict(event.attributes or {}),
                    }
                    for event in span.events
                ],
                "links": [
                    {
                        "attributes": dict(link.attributes or {}),
                        "trace_id": link.context.trace_id,
                        "span_id": link.context.span_id,
                        "trace_flags": int(link.context.trace_flags),
                        "trace_state": list(link.context.trace_state.items()),
                    }
                    for link in span.links
                ],
                "context": None
                if context is None
                else {
                    "trace_id": context.trace_id,
                    "span_id": context.span_id,
                    "trace_flags": int(context.trace_flags),
                    "trace_state": list(context.trace_state.items()),
                },
                "parent": None
                if parent is None
                else {
                    "trace_id": parent.trace_id,
                    "span_id": parent.span_id,
                    "trace_flags": int(parent.trace_flags),
                    "trace_state": list(parent.trace_state.items()),
                },
                "resource": dict(span.resource.attributes),
                "resource_schema_url": span.resource.schema_url,
                "scope": {
                    "name": span.instrumentation_scope.name,
                    "version": span.instrumentation_scope.version,
                    "schema_url": span.instrumentation_scope.schema_url,
                    "attributes": dict(span.instrumentation_scope.attributes or {}),
                },
            }
        )

    metrics: list[dict[str, Any]] = []
    data = telemetry.reader.get_metrics_data()
    if data is not None:
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                scope = scope_metrics.scope
                for metric in scope_metrics.metrics:
                    metrics.append(
                        {
                            "name": metric.name,
                            "description": metric.description,
                            "unit": metric.unit,
                            "resource": dict(resource_metrics.resource.attributes),
                            "resource_schema_url": resource_metrics.schema_url,
                            "scope": {
                                "name": scope.name,
                                "version": scope.version,
                                "schema_url": scope.schema_url,
                                "attributes": dict(scope.attributes or {}),
                                "metrics_schema_url": scope_metrics.schema_url,
                            },
                            "points": [_point_payload(point) for point in metric.data.data_points],
                        }
                    )
    return json.dumps({"spans": spans, "metrics": metrics}, sort_keys=True, default=str)


async def test_generate_success_preserves_identity_and_records_one_attempt(
    telemetry: _Telemetry,
) -> None:
    response = ModelResponse(text="completion-body", latency_ms=17)
    raw = FakeModelClient(response=response)

    result = await _instrumented(raw, telemetry).generate(model_request())

    assert result is response
    assert raw.generate_calls == 1
    assert metric_sum(telemetry, "model_requests_total", provider_type="openai", outcome="ok") == 1
    span = _only_model_span(telemetry)
    assert span.kind.name == "CLIENT"
    assert dict(span.attributes) == {
        "jhin.provider_type": "openai",
        "jhin.operation": "generate",
        "jhin.retry_count": 0,
        "jhin.outcome": "ok",
        "jhin.latency_ms": 17,
    }


@pytest.mark.parametrize("operation", ["generate", "verify"])
async def test_generate_and_verify_preserve_ordinary_failure_and_traceback(
    telemetry: _Telemetry, operation: str
) -> None:
    error = ModelProviderError("private-provider-body", status_code=500, retryable=True)
    raw = FakeModelClient(
        generate_error=error if operation == "generate" else None,
        verify_error=error if operation == "verify" else None,
    )
    client = _instrumented(raw, telemetry, provider_type="anthropic")

    with pytest.raises(ModelProviderError) as raised:
        if operation == "generate":
            await client.generate(model_request())
        else:
            await client.verify()

    assert raised.value is error
    assert _traceback_contains(raised.value.__traceback__, raw.raised_traceback)
    assert raw.generate_calls + raw.verify_calls == 1
    assert (
        metric_sum(
            telemetry,
            "model_requests_total",
            provider_type="anthropic",
            outcome="failed",
        )
        == 1
    )
    span = _only_model_span(telemetry)
    assert dict(span.attributes) == {
        "jhin.provider_type": "anthropic",
        "jhin.operation": operation,
        "jhin.retry_count": 0,
        "jhin.outcome": "failed",
        "error.type": "ModelProviderError",
        "error.code": "upstream_unavailable",
    }
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert list(span.events) == []


@pytest.mark.parametrize("operation", ["generate", "verify"])
async def test_generate_and_verify_preserve_cancellation_without_error_fields(
    telemetry: _Telemetry, operation: str
) -> None:
    cancellation = asyncio.CancelledError("owned-cancel")
    raw = FakeModelClient(
        generate_error=cancellation if operation == "generate" else None,
        verify_error=cancellation if operation == "verify" else None,
    )
    client = _instrumented(raw, telemetry)

    with pytest.raises(asyncio.CancelledError) as raised:
        if operation == "generate":
            await client.generate(model_request())
        else:
            await client.verify()

    assert raised.value is cancellation
    assert _traceback_contains(raised.value.__traceback__, raw.raised_traceback)
    assert (
        metric_sum(telemetry, "model_requests_total", provider_type="openai", outcome="cancelled")
        == 1
    )
    span = _only_model_span(telemetry)
    assert dict(span.attributes) == {
        "jhin.provider_type": "openai",
        "jhin.operation": operation,
        "jhin.retry_count": 0,
        "jhin.outcome": "cancelled",
    }
    assert span.status.status_code is StatusCode.UNSET


async def test_verify_success_preserves_result_identity(telemetry: _Telemetry) -> None:
    result_object = cast(str, object())
    raw = FakeModelClient(verify_result=result_object)

    result = await _instrumented(raw, telemetry, provider_type="openrouter").verify()

    assert result is result_object
    assert raw.verify_calls == 1
    assert (
        metric_sum(telemetry, "model_requests_total", provider_type="openrouter", outcome="ok") == 1
    )
    assert dict(_only_model_span(telemetry).attributes) == {
        "jhin.provider_type": "openrouter",
        "jhin.operation": "verify",
        "jhin.retry_count": 0,
        "jhin.outcome": "ok",
    }


@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("operation", ["generate", "verify"])
async def test_generate_and_verify_do_not_catch_fatal_base_exceptions(
    telemetry: _Telemetry, fatal_type: type[BaseException], operation: str
) -> None:
    fatal = fatal_type("fatal")
    raw = FakeModelClient(
        generate_error=fatal if operation == "generate" else None,
        verify_error=fatal if operation == "verify" else None,
    )
    client = _instrumented(raw, telemetry)
    with pytest.raises(fatal_type) as raised:
        if operation == "generate":
            await client.generate(model_request())
        else:
            await client.verify()
    assert raised.value is fatal
    assert _model_spans(telemetry) == [] or "error.code" not in dict(
        _only_model_span(telemetry).attributes
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        (17.75, 17.75),
        (-1, 0),
        (300_000, 300_000),
        (300_001, 300_000),
        (math.nan, None),
        (math.inf, None),
        (10**400, None),
    ],
)
async def test_generate_latency_has_exact_late_boundary_and_overflow_semantics(
    telemetry: _Telemetry, value: object, expected: int | float | None
) -> None:
    response = ModelResponse.model_construct(text="ok", latency_ms=value)
    raw = FakeModelClient(response=response)

    assert await _instrumented(raw, telemetry).generate(model_request()) is response

    attributes = dict(_only_model_span(telemetry).attributes)
    if expected is None:
        assert "jhin.latency_ms" not in attributes
    else:
        assert attributes["jhin.latency_ms"] == expected


@pytest.mark.parametrize("cleanup_kind", ["ok", "failure", "cancellation"])
@pytest.mark.parametrize("work_kind", ["exhaustion", "failure", "cancellation", "early"])
async def test_stream_work_by_cleanup_matrix(
    telemetry: _Telemetry, work_kind: str, cleanup_kind: str
) -> None:
    chunk = object()
    work_error: BaseException | None = None
    if work_kind == "failure":
        work_error = WorkError("private-work-body")
    elif work_kind == "cancellation":
        work_error = asyncio.CancelledError("work-cancel")
    cleanup_error: BaseException | None = None
    if cleanup_kind == "failure":
        cleanup_error = CleanupError("private-cleanup-body")
    elif cleanup_kind == "cancellation":
        cleanup_error = asyncio.CancelledError("cleanup-cancel")
    iterator = CloseableIterator((chunk,), work_error=work_error, cleanup_error=cleanup_error)
    raw = FakeModelClient(stream_factory=lambda: iterator)
    stream = _instrumented(raw, telemetry, provider_type="ollama").stream(model_request())

    caught: BaseException | None = None
    observed: list[object] = []
    consumer_spans: list[tuple[bool, object]] = []
    try:
        if work_kind == "early":
            observed.append(await anext(stream))
            current = trace.get_current_span()
            consumer_spans.append((current.is_recording(), current.get_span_context()))
            await stream.aclose()
        else:
            async for item in stream:
                observed.append(item)
                current = trace.get_current_span()
                consumer_spans.append((current.is_recording(), current.get_span_context()))
    except BaseException as error:
        caught = error

    assert observed == [chunk]
    assert observed[0] is chunk
    assert raw.stream_calls == 1
    assert iterator.aclose_calls == 1
    if work_kind == "failure":
        assert caught is work_error
        assert _traceback_contains(caught.__traceback__, iterator.work_traceback)
        expected_outcome = "failed"
    elif work_kind == "cancellation":
        assert caught is work_error
        assert _traceback_contains(caught.__traceback__, iterator.work_traceback)
        expected_outcome = "cancelled"
    elif work_kind == "early":
        assert caught is None
        expected_outcome = "cancelled"
    elif cleanup_kind == "failure":
        assert caught is cleanup_error
        assert _traceback_contains(caught.__traceback__, iterator.cleanup_traceback)
        expected_outcome = "failed"
    elif cleanup_kind == "cancellation":
        assert caught is cleanup_error
        assert _traceback_contains(caught.__traceback__, iterator.cleanup_traceback)
        expected_outcome = "cancelled"
    else:
        assert caught is None
        expected_outcome = "ok"

    assert (
        metric_sum(
            telemetry,
            "model_requests_total",
            provider_type="ollama",
            outcome=expected_outcome,
        )
        == 1
    )
    span = _only_model_span(telemetry)
    assert span.attributes["jhin.operation"] == "stream"
    assert span.attributes["jhin.outcome"] == expected_outcome
    if expected_outcome == "failed":
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes["error.code"] == "upstream_unavailable"
    else:
        assert span.status.status_code is StatusCode.UNSET
        assert "error.code" not in span.attributes
    assert iterator.seen_spans
    assert all(
        was_recording and context == span.context for was_recording, context in iterator.seen_spans
    )
    assert all(
        was_recording and context == span.context for was_recording, context in consumer_spans
    )


@pytest.mark.parametrize("work_kind", ["exhaustion", "failure", "cancellation", "early"])
async def test_stream_supports_protocol_only_iterators_without_false_cleanup(
    telemetry: _Telemetry, work_kind: str
) -> None:
    chunk = object()
    work_error: BaseException | None = None
    if work_kind == "failure":
        work_error = WorkError("work")
    elif work_kind == "cancellation":
        work_error = asyncio.CancelledError("cancel")
    iterator = ProtocolOnlyIterator((chunk,), work_error=work_error)
    raw = FakeModelClient(stream_factory=lambda: iterator)
    stream = _instrumented(raw, telemetry).stream(model_request())

    caught: BaseException | None = None
    try:
        if work_kind == "early":
            assert await anext(stream) is chunk
            await stream.aclose()
        else:
            assert [item async for item in stream] == [chunk]
    except BaseException as error:
        caught = error

    if work_kind in {"failure", "cancellation"}:
        assert caught is work_error
    else:
        assert caught is None
    expected = {
        "exhaustion": "ok",
        "failure": "failed",
        "cancellation": "cancelled",
        "early": "cancelled",
    }[work_kind]
    assert (
        metric_sum(telemetry, "model_requests_total", provider_type="openai", outcome=expected) == 1
    )


async def test_never_consumed_stream_creates_nothing(telemetry: _Telemetry) -> None:
    iterator = CloseableIterator(("chunk",))
    raw = FakeModelClient(stream_factory=lambda: iterator)

    stream = _instrumented(raw, telemetry).stream(model_request())
    del stream
    await asyncio.sleep(0)

    assert raw.stream_calls == 0
    assert iterator.aclose_calls == 0
    assert _model_spans(telemetry) == []
    assert _metric_points(telemetry, "model_requests_total") == []


async def test_repeated_cancellation_during_cleanup_closes_once_and_preserves_first(
    telemetry: _Telemetry,
) -> None:
    iterator = BlockingCleanupIterator()
    raw = FakeModelClient(stream_factory=lambda: iterator)
    stream = _instrumented(raw, telemetry).stream(model_request())

    task = asyncio.create_task(anext(stream))
    await iterator.work_started.wait()
    task.cancel("first-cancellation")
    await iterator.cleanup_started.wait()
    task.cancel("second-cancellation")
    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert raised.value.args == ("first-cancellation",)
    assert raised.value is iterator.first_cancellation
    assert iterator.aclose_calls == 1
    assert (
        metric_sum(telemetry, "model_requests_total", provider_type="openai", outcome="cancelled")
        == 1
    )
    assert len(_model_spans(telemetry)) == 1


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (None, None),
        (CleanupError("close-failure"), CleanupError),
        (asyncio.CancelledError("close-cancel"), asyncio.CancelledError),
    ],
)
async def test_close_delegates_once_without_telemetry(
    telemetry: _Telemetry, error: BaseException | None, error_type: type[BaseException] | None
) -> None:
    raw = FakeModelClient(close_error=error)
    client = _instrumented(raw, telemetry)
    if error_type is None:
        await client.close()
    else:
        with pytest.raises(error_type) as raised:
            await client.close()
        assert raised.value is error
        assert _traceback_contains(raised.value.__traceback__, raw.raised_traceback)
    assert raw.close_calls == 1
    assert _model_spans(telemetry) == []
    assert _metric_points(telemetry, "model_requests_total") == []


class _HostileMetrics:
    def counter(self, _name: object) -> Any:
        raise HostileTelemetryError("metric-backend-canary")

    def histogram(self, _name: object) -> Any:
        raise HostileTelemetryError("metric-backend-canary")

    def set_observable(self, _name: object, _observations: object) -> None:
        raise HostileTelemetryError("metric-backend-canary")


class _HostileTracer:
    def start_as_current_span(self, *args: object, **kwargs: object) -> Any:
        raise HostileTelemetryError("tracer-backend-canary")


class _HostileCounter:
    def add(self, _amount: object, **_labels: str) -> None:
        raise HostileTelemetryError("metric-add-canary")


class _AddHostileMetrics:
    def counter(self, _name: object) -> _HostileCounter:
        return _HostileCounter()


class _SpanProxy:
    def __init__(self, wrapped: Any, phase: str) -> None:
        self._wrapped = wrapped
        self._phase = phase

    def get_span_context(self) -> Any:
        return self._wrapped.get_span_context()

    def is_recording(self) -> bool:
        return self._wrapped.is_recording()

    def set_attribute(self, key: str, value: object) -> None:
        if self._phase in {"late_set", "error_attribute"}:
            raise HostileTelemetryError("span-set-canary")
        self._wrapped.set_attribute(key, value)

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def add_event(self, *args: object, **kwargs: object) -> None:
        self._wrapped.add_event(*args, **kwargs)

    def set_status(self, *args: object, **kwargs: object) -> None:
        if self._phase == "error_status":
            raise HostileTelemetryError("span-status-canary")
        self._wrapped.set_status(*args, **kwargs)

    def update_name(self, name: str) -> None:
        self._wrapped.update_name(name)

    def end(self, end_time: int | None = None) -> None:
        self._wrapped.end(end_time=end_time)
        if self._phase == "end":
            raise HostileTelemetryError("span-end-canary")

    def record_exception(self, *args: object, **kwargs: object) -> None:
        self._wrapped.record_exception(*args, **kwargs)


class _ManagerProxy:
    def __init__(self, wrapped: Any, phase: str) -> None:
        self._wrapped = wrapped
        self._phase = phase

    def __enter__(self) -> Any:
        if self._phase == "manager_enter":
            raise HostileTelemetryError("manager-enter-canary")
        return self._wrapped.__enter__()

    def __exit__(self, *args: object) -> bool:
        result = self._wrapped.__exit__(*args)
        if self._phase == "manager_exit":
            raise HostileTelemetryError("manager-exit-canary")
        return bool(result)


class _LifecycleTracer:
    def __init__(self, wrapped: Tracer, phase: str) -> None:
        self._wrapped = wrapped
        self._phase = phase

    def start_as_current_span(self, *args: object, **kwargs: object) -> Any:
        if self._phase == "construction":
            raise HostileTelemetryError("span-construction-canary")
        name = cast(str, args[0] if args else kwargs["name"])
        span = _SpanProxy(
            self._wrapped.start_span(
                name,
                context=kwargs.get("context"),
                kind=kwargs.get("kind"),
                attributes=kwargs.get("attributes"),
            ),
            self._phase,
        )
        manager = trace.use_span(
            cast(Any, span),
            end_on_exit=True,
            record_exception=False,
            set_status_on_exception=False,
        )
        return _ManagerProxy(manager, self._phase)


class _BaseExceptionSpanProxy(_SpanProxy):
    def __init__(self, wrapped: Any, phase: str, error: BaseException) -> None:
        super().__init__(wrapped, phase)
        self._error = error
        self.raised_traceback: TracebackType | None = None

    def _raise_owned(self) -> None:
        try:
            raise self._error
        except BaseException as error:
            self.raised_traceback = error.__traceback__
            raise

    def end(self, end_time: int | None = None) -> None:
        self._wrapped.end(end_time=end_time)
        if self._phase == "end":
            self._raise_owned()


class _BaseExceptionManagerProxy:
    def __init__(
        self,
        wrapped: Any,
        phase: str,
        error: BaseException,
        span: _BaseExceptionSpanProxy,
    ) -> None:
        self._wrapped = wrapped
        self._phase = phase
        self._error = error
        self._span = span
        self.raised_traceback: TracebackType | None = None

    def __enter__(self) -> Any:
        return self._wrapped.__enter__()

    def __exit__(self, *args: object) -> bool:
        result = self._wrapped.__exit__(*args)
        if self._phase == "manager_exit":
            try:
                raise self._error
            except BaseException as error:
                self.raised_traceback = error.__traceback__
                raise
        return bool(result)


class _BaseExceptionLifecycleTracer:
    def __init__(self, wrapped: Tracer, phase: str, error: BaseException) -> None:
        self._wrapped = wrapped
        self._phase = phase
        self._error = error
        self.span: _BaseExceptionSpanProxy | None = None
        self.manager: _BaseExceptionManagerProxy | None = None

    def start_as_current_span(self, *args: object, **kwargs: object) -> Any:
        name = cast(str, args[0] if args else kwargs["name"])
        span = _BaseExceptionSpanProxy(
            self._wrapped.start_span(
                name,
                context=kwargs.get("context"),
                kind=kwargs.get("kind"),
                attributes=kwargs.get("attributes"),
            ),
            self._phase,
            self._error,
        )
        manager = trace.use_span(
            cast(Any, span),
            end_on_exit=True,
            record_exception=False,
            set_status_on_exception=False,
        )
        proxy = _BaseExceptionManagerProxy(manager, self._phase, self._error, span)
        self.span = span
        self.manager = proxy
        return proxy


@pytest.mark.parametrize("mode", ["success", "failure", "cancellation"])
async def test_hostile_telemetry_never_changes_generate_product_authority(
    telemetry: _Telemetry, mode: str
) -> None:
    error: BaseException | None = None
    if mode == "failure":
        error = ModelProviderError("product-error")
    elif mode == "cancellation":
        error = asyncio.CancelledError("product-cancel")
    response = ModelResponse(text="same-product-result", latency_ms=1)
    raw = FakeModelClient(response=response, generate_error=error)
    client = _instrumented(
        raw,
        telemetry,
        metrics=cast(JhinMetrics, _HostileMetrics()),
        tracer=cast(Tracer, _HostileTracer()),
    )

    if error is None:
        assert await client.generate(model_request()) is response
    else:
        with pytest.raises(type(error)) as raised:
            await client.generate(model_request())
        assert raised.value is error
        assert _traceback_contains(raised.value.__traceback__, raw.raised_traceback)
    assert raw.generate_calls == 1
    assert not trace.get_current_span().is_recording()


@pytest.mark.parametrize(
    "phase",
    [
        "construction",
        "manager_enter",
        "late_set",
        "error_attribute",
        "error_status",
        "manager_exit",
        "end",
        "detach",
        "metric_getter",
        "metric_add",
    ],
)
@pytest.mark.parametrize(
    "mode",
    [
        "generate_success",
        "generate_failure",
        "generate_cancellation",
        "verify_success",
        "verify_failure",
        "verify_cancellation",
        "stream_success",
        "stream_failure",
        "stream_cancellation",
    ],
)
async def test_full_hostile_lifecycle_preserves_every_started_attempt_authority(
    telemetry: _Telemetry,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    mode: str,
) -> None:
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    product_error: BaseException | None = None
    if mode.endswith("failure"):
        product_error = ModelProviderError("private-product-failure")
    elif mode.endswith("cancellation"):
        product_error = asyncio.CancelledError("private-product-cancellation")

    iterator = CloseableIterator(("same-chunk",), work_error=product_error)
    response = ModelResponse(text="same-generate-result", latency_ms=23)
    raw = FakeModelClient(
        response=response,
        generate_error=product_error,
        verify_result="same-verify-result",
        verify_error=product_error,
        stream_factory=lambda: iterator,
    )
    metrics: object = telemetry.metrics
    if phase == "metric_getter":
        metrics = _HostileMetrics()
    elif phase == "metric_add":
        metrics = _AddHostileMetrics()
    tracer: object = _LifecycleTracer(telemetry.tracer, phase)
    if phase == "detach":
        original_detach = otel_context.detach

        def hostile_detach(token: object) -> None:
            original_detach(token)
            raise HostileTelemetryError("detach-canary")

        monkeypatch.setattr(otel_context, "detach", hostile_detach)
    client = _instrumented(
        raw,
        telemetry,
        metrics=cast(JhinMetrics, metrics),
        tracer=cast(Tracer, tracer),
    )

    caught: BaseException | None = None
    result: object | None = None
    try:
        if mode.startswith("generate"):
            result = await client.generate(model_request())
        elif mode.startswith("verify"):
            result = await client.verify()
        else:
            result = [chunk async for chunk in client.stream(model_request())]
    except BaseException as error:
        caught = error

    if product_error is None:
        assert caught is None
        expected_result: object = ["same-chunk"]
        if mode.startswith("generate"):
            expected_result = response
        elif mode.startswith("verify"):
            expected_result = "same-verify-result"
        assert (
            result is expected_result if mode.startswith("generate") else result == expected_result
        )
    else:
        assert caught is product_error
        expected_traceback = iterator.work_traceback
        if mode.startswith(("generate", "verify")):
            expected_traceback = raw.raised_traceback
        assert _traceback_contains(caught.__traceback__, expected_traceback)
    assert raw.generate_calls + raw.verify_calls + raw.stream_calls == 1
    assert iterator.aclose_calls == (1 if mode.startswith("stream") else 0)
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span
    expected_outcome = {
        "success": "ok",
        "failure": "failed",
        "cancellation": "cancelled",
    }[mode.rsplit("_", 1)[1]]
    expected_metric = 0 if phase in {"metric_getter", "metric_add"} else 1
    assert (
        metric_sum(
            telemetry,
            "model_requests_total",
            provider_type="openai",
            outcome=expected_outcome,
        )
        == expected_metric
    )
    spans = _model_spans(telemetry)
    assert len(spans) == (0 if phase in {"construction", "manager_enter"} else 1)
    if spans:
        assert spans[0].end_time is not None


class _ExplodingMetrics:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def counter(self, _name: object) -> Any:
        self.calls += 1
        return self

    def add(self, _amount: object, **_labels: str) -> None:
        raise self.error


class _ExplodingTracer:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def start_as_current_span(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        raise self.error


@pytest.mark.parametrize("backend_phase", ["construction", "metric_add"])
@pytest.mark.parametrize("product_mode", ["success", "failure", "cancellation"])
async def test_backend_cancellation_is_diagnostic_and_preserves_product_authority(
    telemetry: _Telemetry,
    backend_phase: str,
    product_mode: str,
) -> None:
    backend_cancellation = asyncio.CancelledError("backend-cancellation")
    product_error: BaseException | None = None
    if product_mode == "failure":
        product_error = ModelProviderError("product-failure")
    elif product_mode == "cancellation":
        product_error = asyncio.CancelledError("first-product-cancellation")
    response = ModelResponse(text="same-product", latency_ms=3)
    raw = FakeModelClient(response=response, generate_error=product_error)
    metrics: object = telemetry.metrics
    tracer: object = telemetry.tracer
    if backend_phase == "construction":
        tracer = _ExplodingTracer(backend_cancellation)
    else:
        metrics = _ExplodingMetrics(backend_cancellation)
    client = _instrumented(
        raw,
        telemetry,
        metrics=cast(JhinMetrics, metrics),
        tracer=cast(Tracer, tracer),
    )
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()

    if product_error is None:
        assert await client.generate(model_request()) is response
    else:
        with pytest.raises(type(product_error)) as caught:
            await client.generate(model_request())
        assert caught.value is product_error
        assert _traceback_contains(caught.value.__traceback__, raw.raised_traceback)

    assert raw.generate_calls == 1
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span


@pytest.mark.parametrize("backend_phase", ["construction", "metric_add"])
@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
async def test_backend_fatal_base_exceptions_propagate_exactly(
    telemetry: _Telemetry,
    backend_phase: str,
    fatal_type: type[BaseException],
) -> None:
    fatal = fatal_type("backend-fatal")
    raw = FakeModelClient(response=ModelResponse(text="product", latency_ms=2))
    metrics: object = telemetry.metrics
    tracer: object = telemetry.tracer
    exploding: object
    if backend_phase == "construction":
        exploding = _ExplodingTracer(fatal)
        tracer = exploding
    else:
        exploding = _ExplodingMetrics(fatal)
        metrics = exploding
    client = _instrumented(
        raw,
        telemetry,
        metrics=cast(JhinMetrics, metrics),
        tracer=cast(Tracer, tracer),
    )
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()

    with pytest.raises(fatal_type) as caught:
        await client.generate(model_request())

    assert caught.value is fatal
    assert raw.generate_calls == (0 if backend_phase == "construction" else 1)
    assert cast(Any, exploding).calls == 1
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span


@pytest.mark.parametrize("backend_phase", ["manager_exit", "end", "detach"])
@pytest.mark.parametrize("backend_kind", ["cancellation", "keyboard", "system_exit"])
@pytest.mark.parametrize("product_mode", ["success", "failure", "cancellation"])
async def test_teardown_base_exceptions_preserve_exact_product_and_context_authority(
    telemetry: _Telemetry,
    monkeypatch: pytest.MonkeyPatch,
    backend_phase: str,
    backend_kind: str,
    product_mode: str,
) -> None:
    backend_error: BaseException
    if backend_kind == "cancellation":
        backend_error = asyncio.CancelledError("diagnostic-teardown-cancellation")
    elif backend_kind == "keyboard":
        backend_error = KeyboardInterrupt("fatal-teardown-keyboard")
    else:
        backend_error = SystemExit("fatal-teardown-system-exit")
    product_error: BaseException | None = None
    if product_mode == "failure":
        product_error = ModelProviderError("active-product-failure")
    elif product_mode == "cancellation":
        product_error = asyncio.CancelledError("first-product-cancellation")
    response = ModelResponse(text="same-product-result", latency_ms=7)
    raw = FakeModelClient(response=response, generate_error=product_error)
    tracer = _BaseExceptionLifecycleTracer(
        telemetry.tracer,
        backend_phase,
        backend_error,
    )
    detach_traceback: list[TracebackType | None] = []
    if backend_phase == "detach":
        original_detach = otel_context.detach

        def raise_after_exact_detach(token: object) -> None:
            original_detach(token)
            try:
                raise backend_error
            except BaseException as error:
                detach_traceback.append(error.__traceback__)
                raise

        monkeypatch.setattr(otel_context, "detach", raise_after_exact_detach)
    client = _instrumented(raw, telemetry, tracer=cast(Tracer, tracer))
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()

    caught: BaseException | None = None
    result: object | None = None
    try:
        result = await client.generate(model_request())
    except BaseException as error:
        caught = error

    if backend_kind == "cancellation":
        if product_error is None:
            assert caught is None
            assert result is response
        else:
            assert caught is product_error
            assert _traceback_contains(caught.__traceback__, raw.raised_traceback)
    else:
        assert caught is backend_error
        backend_traceback = detach_traceback[0] if backend_phase == "detach" else None
        if backend_phase == "manager_exit":
            assert tracer.manager is not None
            backend_traceback = tracer.manager.raised_traceback
        elif backend_phase == "end":
            assert tracer.span is not None
            backend_traceback = tracer.span.raised_traceback
        assert _traceback_contains(caught.__traceback__, backend_traceback)

    assert raw.generate_calls == 1
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span
    assert len(_model_spans(telemetry)) == 1
    assert _model_spans(telemetry)[0].end_time is not None


async def test_synchronous_wrapped_stream_factory_failure_preserves_object_and_traceback(
    telemetry: _Telemetry,
) -> None:
    failure = ModelProviderError("synchronous-stream-factory-failure")
    raised_traceback: TracebackType | None = None

    def fail_stream_factory() -> AsyncIterator[str]:
        nonlocal raised_traceback
        try:
            raise failure
        except BaseException as error:
            raised_traceback = error.__traceback__
            raise

    raw = FakeModelClient(stream_factory=fail_stream_factory)
    stream = _instrumented(raw, telemetry).stream(model_request())

    with pytest.raises(ModelProviderError) as caught:
        await anext(stream)

    assert caught.value is failure
    assert _traceback_contains(caught.value.__traceback__, raised_traceback)
    assert raw.stream_calls == 1
    assert (
        metric_sum(
            telemetry,
            "model_requests_total",
            provider_type="openai",
            outcome="failed",
        )
        == 1
    )
    span = _only_model_span(telemetry)
    assert span.attributes["jhin.operation"] == "stream"
    assert span.attributes["jhin.outcome"] == "failed"


@pytest.mark.parametrize(
    ("constant", "invalid"),
    [
        ("_MODEL_SPAN_NAME", "unregistered.span"),
        ("_MODEL_PROVIDER_ATTRIBUTE_KEY", "private.provider"),
        ("_MODEL_METRIC_NAME", "unregistered_metric"),
        ("_MODEL_METRIC_PROVIDER_LABEL", "wrong_provider_label"),
        ("_MODEL_METRIC_OUTCOME_LABEL", "wrong_outcome_label"),
        ("_MODEL_MEASUREMENT", True),
        ("_MODEL_MEASUREMENT", -1),
        ("_MODEL_MEASUREMENT", math.inf),
    ],
)
async def test_structurally_invalid_model_schema_precedes_product_and_backend_calls(
    telemetry: _Telemetry,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    invalid: object,
) -> None:
    module = _telemetry_module()
    monkeypatch.setattr(module, constant, invalid, raising=False)

    class UntouchedMetrics:
        calls = 0

        def counter(self, _name: object) -> object:
            self.calls += 1
            raise AssertionError("invalid schema must not reach the metric backend")

    class UntouchedTracer:
        calls = 0

        def start_as_current_span(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("invalid schema must not reach the tracer backend")

    metrics = UntouchedMetrics()
    tracer = UntouchedTracer()
    raw = FakeModelClient()
    client = _instrumented(
        raw,
        telemetry,
        metrics=cast(JhinMetrics, metrics),
        tracer=cast(Tracer, tracer),
    )
    with pytest.raises((TypeError, ValueError)):
        await client.generate(model_request())
    assert raw.generate_calls == 0
    assert metrics.calls == 0
    assert tracer.calls == 0


async def test_regex_safe_unknown_closed_values_normalize_to_other(
    telemetry: _Telemetry,
) -> None:
    module = _telemetry_module()
    with module._attempt_span(
        telemetry.tracer,
        provider_type="unknown-provider",
        operation="unknown-operation",
    ) as span:
        module._finish_attempt(
            telemetry.metrics,
            span,
            provider_type="unknown-provider",
            outcome="unknown-outcome",
        )
    exported = _only_model_span(telemetry)
    assert exported.attributes["jhin.provider_type"] == "other"
    assert exported.attributes["jhin.operation"] == "other"
    assert exported.attributes["jhin.outcome"] == "other"
    assert (
        metric_sum(
            telemetry,
            "model_requests_total",
            provider_type="other",
            outcome="other",
        )
        == 1
    )


class _HostileProvider:
    def __init__(self) -> None:
        self.str_calls = 0
        self.repr_calls = 0

    def __str__(self) -> str:
        self.str_calls += 1
        raise AssertionError("provider must not be stringified")

    def __repr__(self) -> str:
        self.repr_calls += 1
        raise AssertionError("provider must not be rendered")


class _SpoofedProvider(_HostileProvider):
    @property
    def __class__(self) -> type[ModelProviderType]:
        return ModelProviderType

    @property
    def value(self) -> str:
        raise AssertionError("spoofed provider value must not be accessed")


async def test_provider_normalization_is_bounded_and_never_stringifies_attackers(
    telemetry: _Telemetry,
) -> None:
    hostile = _HostileProvider()
    raw = FakeModelClient()

    await _instrumented(raw, telemetry, provider_type=hostile).generate(model_request())

    assert hostile.str_calls == hostile.repr_calls == 0
    assert metric_sum(telemetry, "model_requests_total", provider_type="other", outcome="ok") == 1
    assert _only_model_span(telemetry).attributes["jhin.provider_type"] == "other"


async def test_provider_normalization_rejects_class_spoofing_without_value_access(
    telemetry: _Telemetry,
) -> None:
    hostile = _SpoofedProvider()
    raw = FakeModelClient()

    await _instrumented(raw, telemetry, provider_type=hostile).verify()

    assert raw.verify_calls == 1
    assert hostile.str_calls == hostile.repr_calls == 0
    assert metric_sum(telemetry, "model_requests_total", provider_type="other", outcome="ok") == 1


async def test_complete_export_and_process_sinks_exclude_all_model_material(
    telemetry: _Telemetry,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    canaries = {
        "prompt-body-canary",
        "completion-body-canary",
        "stream-chunk-canary",
        "provider-error-body-canary",
        "private-model-prompt-body-canary",
        "base-url-api-key-provider-id-metadata-canary",
        "finish-reason-canary",
    }
    response = ModelResponse(
        text="completion-body-canary",
        finish_reason="finish-reason-canary",
        model="response-model-canary",
        latency_ms=9,
        provider_request_id="provider-request-canary",
        tool_calls=(
            ModelToolCall(
                id="response-call-canary",
                name="response-tool-canary",
                arguments_json='{"secret":"response-arguments-canary"}',
            ),
        ),
    )
    success = _instrumented(FakeModelClient(response=response), telemetry)
    await success.generate(model_request(canary="prompt-body-canary"))
    await success.generate(model_request(canary="base-url-api-key-provider-id-metadata-canary"))
    iterator = CloseableIterator(("stream-chunk-canary",))
    streamed = _instrumented(
        FakeModelClient(stream_factory=lambda: iterator), telemetry, provider_type="anthropic"
    )
    assert [chunk async for chunk in streamed.stream(model_request())] == ["stream-chunk-canary"]
    failed = _instrumented(
        FakeModelClient(verify_error=ModelProviderError("provider-error-body-canary")),
        telemetry,
    )
    with pytest.raises(ModelProviderError):
        await failed.verify()

    print("bounded-test-stdout")
    logging.getLogger(__name__).debug(
        "bounded-test-log",
        extra={"bounded_structured_field": "bounded-structured-value"},
    )
    captured = capsys.readouterr()
    assert any(
        record.__dict__.get("bounded_structured_field") == "bounded-structured-value"
        for record in caplog.records
    )
    structured_records = json.dumps(
        [record.__dict__ for record in caplog.records],
        sort_keys=True,
        default=str,
    )
    payload = "\n".join(
        (
            complete_export_payload(telemetry),
            caplog.text,
            structured_records,
            captured.out,
            captured.err,
        )
    )
    for canary in canaries | {
        "response-model-canary",
        "provider-request-canary",
        "response-call-canary",
        "response-tool-canary",
        "response-arguments-canary",
    }:
        assert canary not in payload


def _bound_target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _bound_target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_bound_target_names(child) for child in target.elts), set())
    return set()


def _pattern_capture_names(pattern: ast.pattern) -> set[str]:
    names: set[str] = set()
    pending = [pattern]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.MatchAs):
            if current.pattern is not None:
                pending.append(current.pattern)
            if current.name not in {None, "_"}:
                names.add(current.name)
        elif isinstance(current, ast.MatchStar):
            if current.name not in {None, "_"}:
                names.add(current.name)
        elif isinstance(current, ast.MatchSequence):
            pending.extend(current.patterns)
        elif isinstance(current, ast.MatchMapping):
            pending.extend(current.patterns)
            if current.rest not in {None, "_"}:
                names.add(current.rest)
        elif isinstance(current, ast.MatchClass):
            pending.extend(current.patterns)
            pending.extend(current.kwd_patterns)
        elif isinstance(current, ast.MatchOr):
            pending.extend(current.patterns)
    return names


class _LexicalLocalCollector(ast.NodeVisitor):
    """Collect names Python treats as locals throughout one code scope."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.external: set[str] = set()

    def _target(self, target: ast.AST) -> None:
        self.names.update(_bound_target_names(target))

    def visit_Global(self, node: ast.Global) -> None:
        self.external.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.external.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(alias.asname or alias.name.partition(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._target(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._target(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._target(node.target)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._target(node.target)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._target(target)

    def visit_For(self, node: ast.For) -> None:
        self._target(node.target)
        self.visit(node.iter)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.names.update(_pattern_capture_names(case.pattern))
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    ) -> None:
        for generator in node.generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension


class _FactoryAuditVisitor(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.scopes: list[dict[str, str]] = [{}]
        self.scope_kinds = ["module"]
        self.code_scope_indices = [0]
        self.calls: list[ast.Call] = []
        self.ambiguous_calls: list[ast.Call] = []
        self.branch_depth = 0
        self.mutated_factory_qualifiers: set[str] = set()

    def _bind(self, name: str, value: str = "other", *, scope_index: int = -1) -> None:
        scope = self.scopes[scope_index]
        previous = scope.get(name)
        tainted = (
            name == "build_model_client"
            or value in {"factory_function", "models_module", "factory_module"}
            or previous
            in {
                "factory_function",
                "models_module",
                "factory_module",
                "tainted_other",
            }
        )
        if self.branch_depth and value in {
            "factory_function",
            "models_module",
            "factory_module",
        }:
            scope[name] = "tainted_other"
        elif previous not in {None, "uninitialized"}:
            scope[name] = "tainted_other" if tainted else "other"
        else:
            scope[name] = "tainted_other" if tainted and value == "other" else value

    def _binding(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _qualified(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return {
                "factory_function": "jhin_models.build_model_client",
                "models_module": "jhin_models",
                "factory_module": "jhin_models.factory",
            }.get(self._binding(node.id))
        if isinstance(node, ast.Attribute):
            owner = self._qualified(node.value)
            return None if owner is None else f"{owner}.{node.attr}"
        return None

    def _bind_target(self, target: ast.AST, *, scope_index: int = -1) -> None:
        for name in _bound_target_names(target):
            self._bind(name, scope_index=scope_index)

    def _invalidate_attribute_target(self, target: ast.AST) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for child in target.elts:
                self._invalidate_attribute_target(child)
            return
        if isinstance(target, ast.Starred):
            self._invalidate_attribute_target(target.value)
            return
        qualified = self._qualified(target)
        if qualified in {
            "jhin_models.build_model_client",
            "jhin_models.factory.build_model_client",
        }:
            self.mutated_factory_qualifiers.add(cast(str, qualified))

    def _is_factory_call(self, node: ast.Call) -> bool:
        qualified = self._qualified(node.func)
        return (
            qualified
            in {
                "jhin_models.build_model_client",
                "jhin_models.factory.build_model_client",
            }
            and qualified not in self.mutated_factory_qualifiers
        )

    def _is_ambiguous_factory_call(self, node: ast.Call) -> bool:
        qualified = self._qualified(node.func)
        if qualified in self.mutated_factory_qualifiers:
            return True
        if isinstance(node.func, ast.Name):
            return self._binding(node.func.id) == "tainted_other"
        return False

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            name = alias.asname or alias.name
            binding = "other"
            if (
                node.level == 0
                and node.module in {"jhin_models", "jhin_models.factory"}
                and alias.name == "build_model_client"
            ):
                binding = "factory_function"
            self._bind(name, binding)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.partition(".")[0]
            binding = "other"
            if alias.name == "jhin_models":
                binding = "models_module"
            elif alias.name == "jhin_models.factory":
                binding = "factory_module" if alias.asname else "models_module"
            self._bind(name, binding)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._invalidate_attribute_target(target)
            self._bind_target(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._invalidate_attribute_target(node.target)
        self._bind_target(node.target)
        self.visit(node.annotation)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._invalidate_attribute_target(node.target)
        self._bind_target(node.target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_target(node.target, scope_index=self.code_scope_indices[-1])

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._invalidate_attribute_target(target)
            self._bind_target(target)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self.branch_depth += 1
        try:
            self._bind_target(node.target)
            for statement in (*node.body, *node.orelse):
                self.visit(statement)
        finally:
            self.branch_depth -= 1

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
        self.branch_depth += 1
        try:
            for item in node.items:
                if item.optional_vars is not None:
                    self._bind_target(item.optional_vars)
            for statement in node.body:
                self.visit(statement)
        finally:
            self.branch_depth -= 1

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self._bind(node.name)
        for statement in node.body:
            self.visit(statement)

    def _visit_conditional(self, statements: tuple[ast.AST, ...]) -> None:
        self.branch_depth += 1
        try:
            for statement in statements:
                self.visit(statement)
        finally:
            self.branch_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._visit_conditional((*node.body, *node.orelse))

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._visit_conditional((*node.body, *node.orelse))

    def visit_Try(self, node: ast.Try) -> None:
        conditional: list[ast.AST] = [*node.body, *node.orelse, *node.finalbody]
        for handler in node.handlers:
            conditional.append(handler)
        self._visit_conditional(tuple(conditional))

    visit_TryStar = visit_Try

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        self.branch_depth += 1
        try:
            for case in node.cases:
                for name in _pattern_capture_names(case.pattern):
                    self._bind(name)
                if case.guard is not None:
                    self.visit(case.guard)
                for statement in case.body:
                    self.visit(statement)
        finally:
            self.branch_depth -= 1

    @staticmethod
    def _argument_names(arguments: ast.arguments) -> set[str]:
        names = {
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            )
        }
        if arguments.vararg is not None:
            names.add(arguments.vararg.arg)
        if arguments.kwarg is not None:
            names.add(arguments.kwarg.arg)
        return names

    @staticmethod
    def _lexical_names(statements: list[ast.stmt]) -> set[str]:
        collector = _LexicalLocalCollector()
        for statement in statements:
            collector.visit(statement)
        return collector.names - collector.external

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        self._bind(node.name)

        saved_scopes = self.scopes
        saved_kinds = self.scope_kinds
        saved_code_indices = self.code_scope_indices
        parent_scopes = saved_scopes[:-1] if saved_kinds[-1] == "class" else saved_scopes
        parent_kinds = saved_kinds[:-1] if saved_kinds[-1] == "class" else saved_kinds
        local_names = self._lexical_names(node.body)
        local_scope = dict.fromkeys(local_names, "uninitialized")
        for name in self._argument_names(node.args):
            local_scope[name] = "tainted_other" if name == "build_model_client" else "other"
        self.scopes = [*parent_scopes, local_scope]
        self.scope_kinds = [*parent_kinds, "function"]
        self.code_scope_indices = [*saved_code_indices, len(self.scopes) - 1]
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self.scopes = saved_scopes
            self.scope_kinds = saved_kinds
            self.code_scope_indices = saved_code_indices

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        collector = _LexicalLocalCollector()
        collector.visit(node.body)
        local_names = collector.names - collector.external
        saved_scopes = self.scopes
        saved_kinds = self.scope_kinds
        parent_scopes = saved_scopes[:-1] if saved_kinds[-1] == "class" else saved_scopes
        parent_kinds = saved_kinds[:-1] if saved_kinds[-1] == "class" else saved_kinds
        local_scope = dict.fromkeys(local_names, "uninitialized")
        for name in self._argument_names(node.args):
            local_scope[name] = "tainted_other" if name == "build_model_client" else "other"
        self.scopes = [*parent_scopes, local_scope]
        self.scope_kinds = [*parent_kinds, "lambda"]
        self.code_scope_indices.append(len(self.scopes) - 1)
        try:
            self.visit(node.body)
        finally:
            self.code_scope_indices.pop()
            self.scopes = saved_scopes
            self.scope_kinds = saved_kinds

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._bind(node.name)
        self.scopes.append({})
        self.scope_kinds.append("class")
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self.scope_kinds.pop()
            self.scopes.pop()

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    ) -> None:
        first, *remaining = node.generators
        self.visit(first.iter)
        saved_scopes = self.scopes
        saved_kinds = self.scope_kinds
        parent_scopes = saved_scopes[:-1] if saved_kinds[-1] == "class" else saved_scopes
        parent_kinds = saved_kinds[:-1] if saved_kinds[-1] == "class" else saved_kinds
        self.scopes = [*parent_scopes, {}]
        self.scope_kinds = [*parent_kinds, "comprehension"]
        try:
            self._bind_target(first.target)
            for condition in first.ifs:
                self.visit(condition)
            for generator in remaining:
                self.visit(generator.iter)
                self._bind_target(generator.target)
                for condition in generator.ifs:
                    self.visit(condition)
            if isinstance(node, ast.DictComp):
                self.visit(node.key)
                self.visit(node.value)
            else:
                self.visit(node.elt)
        finally:
            self.scopes = saved_scopes
            self.scope_kinds = saved_kinds

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_factory_call(node):
            self.calls.append(node)
        elif self._is_ambiguous_factory_call(node):
            self.ambiguous_calls.append(node)
        self.generic_visit(node)


_PACKAGE_FACTORY_CALLS = frozenset(
    {
        "jhin_models.build_model_client",
        "jhin_models.factory.build_model_client",
    }
)


@dataclass(frozen=True)
class _FactoryBinding:
    qualified: str | None
    direct: bool
    initialized: bool
    rebound: bool
    tainted: bool


def _factory_spelling(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _factory_spelling(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


class _FactoryScopeLocals(ast.NodeVisitor):
    """Battle-tested Python code-scope local collection adapted from Task 6."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.external: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        self.external.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.external.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        for parameter in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            if parameter.annotation is not None:
                self.visit(parameter.annotation)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self.visit(node.args.vararg.annotation)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self.visit(node.args.kwarg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.names.update(_pattern_capture_names(case.pattern))
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.add(node.name)
        if node.type is not None:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        values: tuple[ast.AST, ...],
    ) -> None:
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))


class _BindingAwareFactoryAuditVisitor(ast.NodeVisitor):
    """Resolve package factory authority with Python's lexical binding rules."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.scopes: list[dict[str, _FactoryBinding]] = [{}]
        self.scope_kinds: list[str] = ["module"]
        self.hidden_class_scope_indices: set[int] = set()
        self.code_scope_indices: list[int] = [0]
        self.declared_scope_names: set[str] = set()
        self.branch_depth = 0
        self.mutated_qualifiers: set[str] = set()
        self.calls: list[ast.Call] = []
        self.ambiguous_calls: list[ast.Call] = []
        self.authority_escapes: list[ast.AST] = []

    def visit_Module(self, node: ast.Module) -> None:
        self.declared_scope_names = {
            name
            for descendant in ast.walk(node)
            if isinstance(descendant, (ast.Global, ast.Nonlocal))
            for name in descendant.names
        }
        self.generic_visit(node)

    def _push_scope(self, scope: dict[str, _FactoryBinding], kind: str) -> int:
        self.scopes.append(scope)
        self.scope_kinds.append(kind)
        return len(self.scopes) - 1

    def _pop_scope(self) -> None:
        self.scope_kinds.pop()
        self.scopes.pop()

    def _hide_active_class_scopes(self) -> set[int]:
        previous = set(self.hidden_class_scope_indices)
        self.hidden_class_scope_indices.update(
            index for index, kind in enumerate(self.scope_kinds) if kind == "class"
        )
        return previous

    @staticmethod
    def _authority_tainted(name: str, qualified: str | None = None) -> bool:
        return (
            name == "build_model_client"
            or qualified in _PACKAGE_FACTORY_CALLS
            or qualified in {"jhin_models", "jhin_models.factory"}
        )

    def _lookup(self, name: str) -> _FactoryBinding:
        for index in range(len(self.scopes) - 1, -1, -1):
            if index in self.hidden_class_scope_indices:
                continue
            if name in self.scopes[index]:
                binding = self.scopes[index][name]
                break
        else:
            binding = _FactoryBinding(
                None,
                True,
                False,
                False,
                self._authority_tainted(name),
            )
        if name in self.declared_scope_names:
            return _FactoryBinding(
                None,
                False,
                True,
                True,
                binding.tainted or self._authority_tainted(name),
            )
        return binding

    def _resolve(self, node: ast.AST) -> _FactoryBinding:
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            qualified = f"{base.qualified}.{node.attr}" if base.qualified else None
            return _FactoryBinding(
                qualified,
                base.direct,
                base.initialized,
                base.rebound,
                base.tainted or self._authority_tainted(node.attr, qualified),
            )
        return _FactoryBinding(None, True, True, False, False)

    def _bind(
        self,
        name: str,
        binding: _FactoryBinding,
        *,
        scope_index: int = -1,
    ) -> None:
        scope = self.scopes[scope_index]
        previous = scope.get(name)
        rebound = (
            binding.rebound
            or bool(previous and previous.initialized)
            or bool(self.branch_depth and binding.tainted)
        )
        tainted = (
            binding.tainted
            or self._authority_tainted(name, binding.qualified)
            or bool(previous and previous.tainted)
        )
        scope[name] = _FactoryBinding(
            None if rebound else binding.qualified,
            binding.direct and not rebound,
            True,
            rebound,
            tainted,
        )

    def _mark_mutated_prefix(self, target: ast.AST) -> None:
        if isinstance(target, (ast.List, ast.Tuple)):
            for child in target.elts:
                self._mark_mutated_prefix(child)
            return
        if isinstance(target, ast.Starred):
            self._mark_mutated_prefix(target.value)
            return
        if not isinstance(target, ast.Attribute):
            return
        qualified = self._resolve(target).qualified
        if qualified and qualified.startswith("jhin_models"):
            self.mutated_qualifiers.add(qualified)

    def _assign(
        self,
        target: ast.AST,
        value: ast.AST | None,
        *,
        scope_index: int = -1,
    ) -> None:
        self._mark_mutated_prefix(target)
        if isinstance(target, (ast.List, ast.Tuple)):
            for child in target.elts:
                self._assign(child, None, scope_index=scope_index)
            return
        if isinstance(target, ast.Starred):
            self._assign(target.value, None, scope_index=scope_index)
            return
        if not isinstance(target, ast.Name):
            return
        resolved = (
            self._resolve(value)
            if value is not None
            else _FactoryBinding(
                None,
                True,
                True,
                False,
                self._authority_tainted(target.id),
            )
        )
        binding = (
            _FactoryBinding(
                resolved.qualified,
                False,
                True,
                resolved.rebound,
                resolved.tainted,
            )
            if resolved.qualified is not None and not resolved.qualified.startswith("local:")
            else _FactoryBinding(
                f"local:{target.id}",
                True,
                True,
                resolved.rebound,
                resolved.tainted or self._authority_tainted(target.id),
            )
        )
        self._bind(target.id, binding, scope_index=scope_index)

    def _is_mutated(self, qualified: str | None) -> bool:
        return bool(
            qualified
            and any(
                qualified == prefix or qualified.startswith(f"{prefix}.")
                for prefix in self.mutated_qualifiers
            )
        )

    def _is_authority_value(self, binding: _FactoryBinding) -> bool:
        return (
            binding.qualified
            in {
                "jhin_models",
                "jhin_models.factory",
                *_PACKAGE_FACTORY_CALLS,
            }
            or self._is_mutated(binding.qualified)
            or (
                binding.tainted
                and (
                    binding.qualified is None
                    or binding.qualified.startswith("local:")
                    or binding.rebound
                )
            )
        )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and self._is_authority_value(self._resolve(node)):
            self.authority_escapes.append(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        binding = self._resolve(node)
        if self._is_authority_value(binding):
            self.authority_escapes.append(node)
            return
        if not isinstance(node.value, (ast.Name, ast.Attribute)):
            self.visit(node.value)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".", 1)[0]
            qualified = alias.name if alias.asname else alias.name.split(".", 1)[0]
            self._bind(
                name,
                _FactoryBinding(
                    qualified,
                    alias.asname is None,
                    True,
                    False,
                    self._authority_tainted(name, qualified),
                ),
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        for alias in node.names:
            if alias.name == "*":
                if node.level == 0 and node.module in {"jhin_models", "jhin_models.factory"}:
                    qualified = f"{node.module}.build_model_client"
                    self._bind(
                        "build_model_client",
                        _FactoryBinding(qualified, False, True, False, True),
                    )
                continue
            name = alias.asname or alias.name
            if node.level:
                self._bind(
                    name,
                    _FactoryBinding(
                        None,
                        False,
                        True,
                        False,
                        self._authority_tainted(name),
                    ),
                )
                continue
            qualified = f"{node.module}.{alias.name}"
            self._bind(
                name,
                _FactoryBinding(
                    qualified,
                    alias.asname is None,
                    True,
                    False,
                    self._authority_tainted(name, qualified),
                ),
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._assign(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._assign(node.target, node.value)
        self.visit(node.annotation)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._assign(node.target, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._assign(node.target, node.value, scope_index=self.code_scope_indices[-1])

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._assign(target, None)

    def _visit_branch(self, nodes: tuple[ast.AST, ...]) -> None:
        self.branch_depth += 1
        try:
            for node in nodes:
                self.visit(node)
        finally:
            self.branch_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._visit_branch((*node.body, *node.orelse))

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._visit_branch((*node.body, *node.orelse))

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self.branch_depth += 1
        try:
            self._assign(node.target, None)
            for statement in (*node.body, *node.orelse):
                self.visit(statement)
        finally:
            self.branch_depth -= 1

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
        self.branch_depth += 1
        try:
            for item in node.items:
                if item.optional_vars is not None:
                    self._assign(item.optional_vars, None)
            for statement in node.body:
                self.visit(statement)
        finally:
            self.branch_depth -= 1

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_Try(self, node: ast.Try) -> None:
        branches: list[ast.AST] = [*node.body, *node.orelse, *node.finalbody]
        branches.extend(node.handlers)
        self._visit_branch(tuple(branches))

    visit_TryStar = visit_Try

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self._assign(ast.Name(id=node.name, ctx=ast.Store()), None)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        self.branch_depth += 1
        try:
            for case in node.cases:
                for name in _pattern_capture_names(case.pattern):
                    self._assign(ast.Name(id=name, ctx=ast.Store()), None)
                if case.guard is not None:
                    self.visit(case.guard)
                for statement in case.body:
                    self.visit(statement)
        finally:
            self.branch_depth -= 1

    @staticmethod
    def _parameters(arguments: ast.arguments) -> list[ast.arg]:
        parameters = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
        if arguments.vararg is not None:
            parameters.append(arguments.vararg)
        if arguments.kwarg is not None:
            parameters.append(arguments.kwarg)
        return parameters

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        parameters = self._parameters(node.args)
        for parameter in parameters:
            if parameter.annotation is not None:
                self.visit(parameter.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        self._bind(
            node.name,
            _FactoryBinding(
                f"local:{node.name}",
                True,
                True,
                False,
                self._authority_tainted(node.name),
            ),
        )
        collector = _FactoryScopeLocals()
        for statement in node.body:
            collector.visit(statement)
        scope = {
            name: _FactoryBinding(
                None,
                True,
                False,
                False,
                self._authority_tainted(name),
            )
            for name in collector.names - collector.external
        }
        for parameter in parameters:
            scope[parameter.arg] = _FactoryBinding(
                f"local:{parameter.arg}",
                True,
                True,
                False,
                self._authority_tainted(parameter.arg),
            )
        previous_hidden = self._hide_active_class_scopes()
        scope_index = self._push_scope(scope, "function")
        self.code_scope_indices.append(scope_index)
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self.code_scope_indices.pop()
            self._pop_scope()
            self.hidden_class_scope_indices = previous_hidden

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        collector = _FactoryScopeLocals()
        collector.visit(node.body)
        scope = {
            name: _FactoryBinding(
                None,
                True,
                False,
                False,
                self._authority_tainted(name),
            )
            for name in collector.names - collector.external
        }
        for parameter in self._parameters(node.args):
            scope[parameter.arg] = _FactoryBinding(
                f"local:{parameter.arg}",
                True,
                True,
                False,
                self._authority_tainted(parameter.arg),
            )
        previous_hidden = self._hide_active_class_scopes()
        scope_index = self._push_scope(scope, "lambda")
        self.code_scope_indices.append(scope_index)
        try:
            self.visit(node.body)
        finally:
            self.code_scope_indices.pop()
            self._pop_scope()
            self.hidden_class_scope_indices = previous_hidden

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        values: tuple[ast.AST, ...],
    ) -> None:
        first, *remaining = generators
        self.visit(first.iter)
        collector = _FactoryScopeLocals()
        for generator in generators:
            collector.visit(generator.target)
        scope = {
            name: _FactoryBinding(
                None,
                True,
                False,
                False,
                self._authority_tainted(name),
            )
            for name in collector.names
        }
        previous_hidden = self._hide_active_class_scopes()
        self._push_scope(scope, "comprehension")
        try:
            self._assign(first.target, None)
            for condition in first.ifs:
                self.visit(condition)
            for generator in remaining:
                self.visit(generator.iter)
                self._assign(generator.target, None)
                for condition in generator.ifs:
                    self.visit(condition)
            for value in values:
                self.visit(value)
        finally:
            self._pop_scope()
            self.hidden_class_scope_indices = previous_hidden

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        previous_hidden = self._hide_active_class_scopes()
        self._push_scope({}, "class")
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._pop_scope()
            self.hidden_class_scope_indices = previous_hidden
        self._bind(
            node.name,
            _FactoryBinding(
                f"local:{node.name}",
                True,
                True,
                False,
                self._authority_tainted(node.name),
            ),
        )

    def visit_Call(self, node: ast.Call) -> None:
        binding = self._resolve(node.func)
        spelling = _factory_spelling(node.func)
        direct_shape = isinstance(node.func, (ast.Name, ast.Attribute))
        exact = (
            direct_shape
            and binding.qualified in _PACKAGE_FACTORY_CALLS
            and not self._is_mutated(binding.qualified)
        )
        if exact and not binding.rebound:
            self.calls.append(node)
        elif (
            binding.tainted
            or spelling.rsplit(".", 1)[-1] == "build_model_client"
            or self._is_mutated(binding.qualified)
        ):
            self.ambiguous_calls.append(node)
        if not exact:
            self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)


def _factory_audit(
    sources: Mapping[str, str],
    *,
    expected_handles: Mapping[str, tuple[str, str]],
) -> list[str]:
    failures: list[str] = []
    owners: list[str] = []
    for filename, source in sources.items():
        tree = ast.parse(source, filename=filename)
        visitor = _BindingAwareFactoryAuditVisitor(filename)
        visitor.visit(tree)
        if visitor.ambiguous_calls:
            failures.append(
                f"owners:ambiguous:{filename}:"
                f"{sorted(call.lineno for call in visitor.ambiguous_calls)!r}"
            )
        if visitor.authority_escapes:
            failures.append(
                f"owners:authority_escape:{filename}:"
                f"{sorted(node.lineno for node in visitor.authority_escapes)!r}"
            )
        for call in visitor.calls:
            owners.append(filename)
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            if set(keywords) & {None} or not {"metrics", "tracer"} <= set(keywords):
                failures.append(f"{filename}:{call.lineno}:missing")
                continue
            metric_expr = ast.unparse(keywords["metrics"])
            tracer_expr = ast.unparse(keywords["tracer"])
            expected = expected_handles.get(filename)
            if expected is None or (metric_expr, tracer_expr) != expected:
                failures.append(f"{filename}:{call.lineno}:handles")
    if set(owners) != set(expected_handles) or len(owners) != len(expected_handles):
        failures.append(f"owners:{sorted(owners)!r}")
    return failures


def test_semantic_factory_auditor_handles_aliases_swaps_extra_callers_and_local_seams() -> None:
    valid = {
        "api.py": (
            "from jhin_models import build_model_client as build\n"
            "build('openai', metrics=metrics, tracer=tracer)\n"
        ),
        "agent.py": (
            "import jhin_models.factory\n"
            "jhin_models.factory.build_model_client("
            "'openai', metrics=self.metrics, tracer=self.tracer)\n"
            "self._build_model_client(request)\n"
        ),
    }
    expected = {
        "api.py": ("metrics", "tracer"),
        "agent.py": ("self.metrics", "self.tracer"),
    }
    assert _factory_audit(valid, expected_handles=expected) == []

    missing = dict(valid)
    missing["api.py"] = (
        "from jhin_models.factory import build_model_client\n"
        "build_model_client('openai', tracer=tracer)\n"
    )
    assert any(
        "missing" in failure for failure in _factory_audit(missing, expected_handles=expected)
    )

    swapped = dict(valid)
    swapped["agent.py"] = (
        "import jhin_models.factory as factory\n"
        "factory.build_model_client('openai', metrics=tracer, tracer=metrics)\n"
    )
    assert any(
        "handles" in failure for failure in _factory_audit(swapped, expected_handles=expected)
    )

    foreign = dict(valid)
    foreign["api.py"] = (
        "from jhin_models import build_model_client\n"
        "build_model_client('openai', metrics=foreign.metrics, tracer=attacker_tracer)\n"
    )
    assert any(
        "handles" in failure for failure in _factory_audit(foreign, expected_handles=expected)
    )

    rebound = dict(valid)
    rebound["api.py"] = (
        "from jhin_models import build_model_client\n"
        "build_model_client = lambda *args, **kwargs: None\n"
        "build_model_client('openai', metrics=metrics, tracer=tracer)\n"
    )
    assert any(
        "owners" in failure for failure in _factory_audit(rebound, expected_handles=expected)
    )

    shadowed = dict(valid)
    shadowed["api.py"] = (
        "from jhin_models import build_model_client\n"
        "def invoke(build_model_client):\n"
        "    return build_model_client('openai', metrics=metrics, tracer=tracer)\n"
    )
    assert any(
        "owners" in failure for failure in _factory_audit(shadowed, expected_handles=expected)
    )

    local_shadow = dict(valid)
    local_shadow["api.py"] = (
        "from jhin_models import build_model_client\n"
        "def invoke():\n"
        "    build_model_client = local_factory\n"
        "    return build_model_client('openai', metrics=metrics, tracer=tracer)\n"
    )
    assert any(
        "owners" in failure for failure in _factory_audit(local_shadow, expected_handles=expected)
    )

    comprehension = dict(valid)
    comprehension["api.py"] = (
        "from jhin_models import build_model_client\n"
        "values = [build_model_client('openai', metrics=metrics, tracer=tracer) "
        "for build_model_client in factories]\n"
    )
    assert any(
        "owners" in failure for failure in _factory_audit(comprehension, expected_handles=expected)
    )

    extra = dict(valid)
    extra["extra.py"] = (
        "from jhin_models import build_model_client\n"
        "build_model_client('ollama', metrics=metrics, tracer=tracer)\n"
    )
    assert any("owners" in failure for failure in _factory_audit(extra, expected_handles=expected))

    annotation_extra = dict(valid)
    annotation_extra["api.py"] += (
        "def hidden(value: build('openai', metrics=metrics, tracer=tracer)) -> "
        "build('openai', metrics=metrics, tracer=tracer):\n"
        "    return value\n"
    )
    assert any(
        "owners" in failure
        for failure in _factory_audit(annotation_extra, expected_handles=expected)
    )

    tainted_attribute_extra = dict(valid)
    tainted_attribute_extra["extra.py"] = (
        "import jhin_models\n"
        "if condition:\n"
        "    jhin_models = local_module\n"
        "jhin_models.build_model_client('openai', metrics=metrics, tracer=tracer)\n"
    )
    assert any(
        "owners" in failure
        for failure in _factory_audit(tainted_attribute_extra, expected_handles=expected)
    )

    aliased_factory_module_extra = dict(valid)
    aliased_factory_module_extra["extra.py"] = (
        "from jhin_models import factory as f\n"
        "f.build_model_client('openai', metrics=metrics, tracer=tracer)\n"
    )
    assert any(
        "owners" in failure
        for failure in _factory_audit(aliased_factory_module_extra, expected_handles=expected)
    )

    wildcard_extra = dict(valid)
    wildcard_extra["extra.py"] = (
        "from jhin_models import *\nbuild_model_client('openai', metrics=metrics, tracer=tracer)\n"
    )
    assert any(
        "owners" in failure for failure in _factory_audit(wildcard_extra, expected_handles=expected)
    )


@pytest.mark.parametrize(
    ("extra_form", "extra_source"),
    [
        (
            "tainted_attribute_root",
            "import jhin_models\n"
            "if condition:\n"
            "    jhin_models = local_module\n"
            "jhin_models.build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "aliased_factory_module",
            "from jhin_models import factory as f\n"
            "f.build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "package_wildcard",
            "from jhin_models import *\n"
            "build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "factory_wildcard",
            "from jhin_models.factory import *\n"
            "build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "assigned_function_alias",
            "from jhin_models import build_model_client\n"
            "factory_alias = build_model_client\n"
            "factory_alias('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "assigned_module_alias",
            "import jhin_models\n"
            "factory_alias = jhin_models.factory\n"
            "factory_alias.build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "tuple_unpacked_function_alias",
            "from jhin_models import build_model_client\n"
            "(factory_alias,) = (build_model_client,)\n"
            "factory_alias('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "list_unpacked_function_alias",
            "from jhin_models import build_model_client\n"
            "[factory_alias] = [build_model_client]\n"
            "factory_alias('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "conditional_expression_alias",
            "from jhin_models import build_model_client\n"
            "factory_alias = build_model_client if condition else local_factory\n"
            "factory_alias('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "boolean_expression_alias",
            "from jhin_models import build_model_client\n"
            "factory_alias = build_model_client or local_factory\n"
            "factory_alias('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "direct_conditional_callee",
            "from jhin_models import build_model_client\n"
            "(build_model_client if condition else local_factory)("
            "'openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "direct_boolean_callee",
            "from jhin_models import build_model_client\n"
            "(build_model_client or local_factory)("
            "'openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "container_escape",
            "from jhin_models import build_model_client\n"
            "escaped = {'factory': build_model_client}\n",
        ),
        (
            "return_escape",
            "from jhin_models import build_model_client\n"
            "def expose():\n"
            "    return build_model_client\n",
        ),
        (
            "argument_escape",
            "from jhin_models import build_model_client\nconsume(build_model_client)\n",
        ),
    ],
)
def test_semantic_factory_auditor_rejects_every_hidden_extra_package_caller(
    extra_form: str,
    extra_source: str,
) -> None:
    sources = {
        "api.py": (
            "from jhin_models import build_model_client as build\n"
            "build('openai', metrics=metrics, tracer=tracer)\n"
        ),
        "agent.py": (
            "import jhin_models.factory\n"
            "jhin_models.factory.build_model_client("
            "'openai', metrics=self.metrics, tracer=self.tracer)\n"
        ),
        "extra.py": extra_source,
    }
    failures = _factory_audit(
        sources,
        expected_handles={
            "api.py": ("metrics", "tracer"),
            "agent.py": ("self.metrics", "self.tracer"),
        },
    )
    assert any("owners" in failure for failure in failures), extra_form


@pytest.mark.parametrize(
    ("binding_form", "api_source"),
    [
        (
            "call_before_later_assignment",
            "from jhin_models import build_model_client\n"
            "def invoke():\n"
            "    build_model_client('openai', metrics=metrics, tracer=tracer)\n"
            "    build_model_client = local_factory\n",
        ),
        (
            "lambda_parameter",
            "from jhin_models import build_model_client\n"
            "invoke = lambda build_model_client: build_model_client("
            "'openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "dict_comprehension",
            "from jhin_models import build_model_client\n"
            "values = {build_model_client: build_model_client("
            "'openai', metrics=metrics, tracer=tracer) "
            "for build_model_client in factories}\n",
        ),
        (
            "named_expression",
            "from jhin_models import build_model_client\n"
            "def invoke():\n"
            "    (build_model_client := local_factory)\n"
            "    return build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "for_target",
            "from jhin_models import build_model_client\n"
            "def invoke():\n"
            "    for build_model_client in factories:\n"
            "        build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "with_target",
            "from jhin_models import build_model_client\n"
            "def invoke():\n"
            "    with manager as build_model_client:\n"
            "        build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "except_target",
            "from jhin_models import build_model_client\n"
            "def invoke():\n"
            "    try:\n"
            "        operation()\n"
            "    except Exception as build_model_client:\n"
            "        build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "delete_target",
            "from jhin_models import build_model_client\n"
            "def invoke():\n"
            "    del build_model_client\n"
            "    return build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "class_local_not_closure",
            "class Owner:\n"
            "    from jhin_models import build_model_client\n"
            "    def invoke(self):\n"
            "        return build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "class_comprehension_does_not_close_over_class_local",
            "build_model_client = local_factory\n"
            "class Owner:\n"
            "    from jhin_models import build_model_client\n"
            "    values = [build_model_client('openai', metrics=metrics, tracer=tracer) "
            "for _ in factories]\n",
        ),
        (
            "match_capture_is_function_local",
            "from jhin_models import build_model_client\n"
            "def invoke(value):\n"
            "    match value:\n"
            "        case {'factory': build_model_client}:\n"
            "            pass\n"
            "    return build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "package_module_attribute_rebound",
            "import jhin_models\n"
            "jhin_models.build_model_client = local_factory\n"
            "jhin_models.build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "package_factory_prefix_rebound",
            "import jhin_models\n"
            "jhin_models.factory = local_module\n"
            "jhin_models.factory.build_model_client("
            "'openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "conditional_package_import_is_ambiguous",
            "build_model_client = local_factory\n"
            "if condition:\n"
            "    from jhin_models import build_model_client\n"
            "build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
        (
            "try_package_import_is_ambiguous",
            "build_model_client = local_factory\n"
            "try:\n"
            "    from jhin_models import build_model_client\n"
            "except ImportError:\n"
            "    pass\n"
            "build_model_client('openai', metrics=metrics, tracer=tracer)\n",
        ),
    ],
)
def test_semantic_factory_auditor_rejects_every_lexical_shadow_form(
    binding_form: str,
    api_source: str,
) -> None:
    sources = {
        "api.py": api_source,
        "agent.py": (
            "from jhin_models.factory import build_model_client as build\n"
            "build('ollama', metrics=self.metrics, tracer=self.tracer)\n"
        ),
    }
    failures = _factory_audit(
        sources,
        expected_handles={
            "api.py": ("metrics", "tracer"),
            "agent.py": ("self.metrics", "self.tracer"),
        },
    )
    assert any("owners" in failure for failure in failures), binding_form


def test_exact_production_model_factory_owners_supply_semantic_handles() -> None:
    candidates = tuple(
        path
        for area in ("apps", "packages", "services")
        for path in (REPO_ROOT / area).glob("*/src/**/*.py")
    )
    sources = {str(path.relative_to(REPO_ROOT)): path.read_text() for path in candidates}
    assert (
        _factory_audit(
            sources,
            expected_handles={
                "apps/api/src/jhin_api/models/service.py": ("metrics", "tracer"),
                "services/agent_worker/src/jhin_agent_worker/reasoning.py": (
                    "self._metrics",
                    "self._tracer",
                ),
            },
        )
        == []
    )


def test_model_package_default_runtime_handles_are_explicit_noops() -> None:
    assert noop_metrics().is_noop
    assert noop_tracer() is noop_tracer()
