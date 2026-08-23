"""Provider-neutral, payload-free model attempt telemetry."""

from __future__ import annotations

import asyncio
import math
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import Any, cast

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Tracer

from jhin_domain import ModelProviderType
from jhin_models.base import (
    AccountStatus,
    ModelClient,
    ModelListing,
    ModelRequest,
    ModelResponse,
)
from jhin_models.embeddings import EmbeddingClient, EmbeddingResult
from jhin_models.images import ImageGenerationClient
from jhin_observability import (
    SPAN_NAMES,
    AttributeValue,
    JhinMetrics,
    MetricName,
    SafeErrorCode,
    SpanName,
    noop_metrics,
    normalize_span_attributes,
    record_span_error,
    safe_error,
    safe_span,
    set_span_attributes,
)

_FALLBACK_SPAN = trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)
_MODEL_SPAN_NAME = "model.request"
_MODEL_PROVIDER_ATTRIBUTE_KEY = "jhin.provider_type"
_MODEL_OPERATION_ATTRIBUTE_KEY = "jhin.operation"
_MODEL_RETRY_ATTRIBUTE_KEY = "jhin.retry_count"
_MODEL_OUTCOME_ATTRIBUTE_KEY = "jhin.outcome"
_MODEL_LATENCY_ATTRIBUTE_KEY = "jhin.latency_ms"
_MODEL_METRIC_NAME = "model_requests_total"
_MODEL_METRIC_PROVIDER_LABEL = "provider_type"
_MODEL_METRIC_OUTCOME_LABEL = "outcome"
_MODEL_MEASUREMENT = 1


def _normalize_provider_type(value: object) -> str:
    if type(value) is ModelProviderType:
        return value.value
    if type(value) is not str:
        return "other"
    try:
        return ModelProviderType(value).value
    except ValueError:
        return "other"


def _validated_span_schema(
    *,
    provider_type: object,
    operation: object,
    outcome: object | None = None,
    latency_ms: object | None = None,
) -> dict[str, AttributeValue]:
    if _MODEL_SPAN_NAME not in SPAN_NAMES:
        raise ValueError("unregistered model span name")
    attributes: dict[Any, Any] = {
        _MODEL_PROVIDER_ATTRIBUTE_KEY: _normalize_provider_type(provider_type),
        _MODEL_OPERATION_ATTRIBUTE_KEY: operation,
        _MODEL_RETRY_ATTRIBUTE_KEY: 0,
    }
    if outcome is not None:
        attributes[_MODEL_OUTCOME_ATTRIBUTE_KEY] = outcome
    if latency_ms is not None:
        attributes[_MODEL_LATENCY_ATTRIBUTE_KEY] = latency_ms
    return normalize_span_attributes(attributes)


def _validated_metric_point(
    *,
    provider_type: object,
    outcome: object,
) -> tuple[str, str, dict[str, str]]:
    normalized = _validated_span_schema(
        provider_type=provider_type,
        operation="other",
        outcome=outcome,
    )
    normalized_provider = cast(str, normalized[_MODEL_PROVIDER_ATTRIBUTE_KEY])
    normalized_outcome = cast(str, normalized[_MODEL_OUTCOME_ATTRIBUTE_KEY])
    labels: dict[Any, Any] = {
        _MODEL_METRIC_PROVIDER_LABEL: normalized_provider,
        _MODEL_METRIC_OUTCOME_LABEL: normalized_outcome,
    }
    noop_metrics().counter(cast(MetricName, _MODEL_METRIC_NAME)).add(
        _MODEL_MEASUREMENT,
        **labels,
    )
    return normalized_provider, normalized_outcome, cast(dict[str, str], labels)


def _prevalidate_attempt_contract(
    *, provider_type: object, operation: object
) -> dict[str, AttributeValue]:
    attributes = _validated_span_schema(
        provider_type=provider_type,
        operation=operation,
        latency_ms=0,
    )
    normalized_provider = attributes[_MODEL_PROVIDER_ATTRIBUTE_KEY]
    for outcome in ("ok", "failed", "cancelled", "other"):
        _validated_metric_point(provider_type=normalized_provider, outcome=outcome)
    attributes.pop(_MODEL_LATENCY_ATTRIBUTE_KEY)
    return attributes


def _run_diagnostic[T](action: Callable[[], T]) -> T | None:
    try:
        return action()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return None


@contextmanager
def _attempt_span(
    tracer: Tracer,
    *,
    provider_type: str,
    operation: str,
) -> Iterator[Span]:
    """Contain every non-fatal diagnostic failure, including cancellation."""
    attributes = _prevalidate_attempt_contract(
        provider_type=provider_type,
        operation=operation,
    )
    manager = safe_span(
        cast(SpanName, _MODEL_SPAN_NAME),
        tracer=tracer,
        kind=SpanKind.CLIENT,
        attributes=attributes,
    )
    try:
        span = manager.__enter__()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        yield _FALLBACK_SPAN
        return

    try:
        yield span
    finally:
        error_type, error, error_traceback = sys.exc_info()
        try:
            manager.__exit__(error_type, error, error_traceback)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            pass


def _record_attempt(metrics: JhinMetrics, *, provider_type: str, outcome: str) -> None:
    _, _, labels = _validated_metric_point(
        provider_type=provider_type,
        outcome=outcome,
    )

    def record() -> None:
        metrics.counter(cast(MetricName, _MODEL_METRIC_NAME)).add(
            _MODEL_MEASUREMENT,
            **labels,
        )

    _run_diagnostic(record)


def _finish_attempt(
    metrics: JhinMetrics,
    span: Span,
    *,
    provider_type: str,
    outcome: str,
    error: Exception | None = None,
) -> None:
    _, normalized_outcome, _ = _validated_metric_point(
        provider_type=provider_type,
        outcome=outcome,
    )
    outcome_attributes = _validated_span_schema(
        provider_type=provider_type,
        operation="other",
        outcome=normalized_outcome,
    )
    _run_diagnostic(
        lambda: set_span_attributes(
            span,
            {_MODEL_OUTCOME_ATTRIBUTE_KEY: outcome_attributes[_MODEL_OUTCOME_ATTRIBUTE_KEY]},
        )
    )
    if error is not None:
        _run_diagnostic(
            lambda: record_span_error(
                span,
                safe_error(error, code=SafeErrorCode.UPSTREAM_UNAVAILABLE),
            )
        )
    _record_attempt(metrics, provider_type=provider_type, outcome=outcome)


def _safe_latency(value: object) -> int | float | None:
    if type(value) not in {int, float}:
        return None
    bounded_value = cast(int | float, value)
    try:
        numeric = float(bounded_value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return max(0, min(300_000, bounded_value))


async def _cleanup_iterator(
    iterator: AsyncIterator[str],
) -> tuple[BaseException | None, TracebackType | None]:
    try:
        close = getattr(iterator, "aclose", None)
        if not callable(close):
            return None, None
        await close()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        return error, error.__traceback__
    return None, None


class _InstrumentedEmbeddingClient:
    """Embedding view of an instrumented client (see ``embedding_client``)."""

    def __init__(self, wrapped: EmbeddingClient, *, owner: InstrumentedModelClient) -> None:
        self._wrapped = wrapped
        self._owner = owner

    @property
    def provider_name(self) -> str:
        return self._owner.provider_name

    async def embed(
        self, texts: Sequence[str], *, model: str, dimensions: int | None = None
    ) -> EmbeddingResult:
        return await self._owner._embed_instrumented(
            self._wrapped, texts, model=model, dimensions=dimensions
        )


class InstrumentedModelClient(ModelClient):
    """One telemetry wrapper around one provider adapter."""

    def __init__(
        self,
        wrapped: ModelClient,
        *,
        provider_type: object,
        metrics: JhinMetrics,
        tracer: Tracer,
    ) -> None:
        self._wrapped = wrapped
        self._provider_type = _normalize_provider_type(provider_type)
        self._metrics = metrics
        self._tracer = tracer

    async def generate(self, request: ModelRequest) -> ModelResponse:
        with _attempt_span(
            self._tracer,
            provider_type=self._provider_type,
            operation="generate",
        ) as span:
            try:
                response = await self._wrapped.generate(request)
            except asyncio.CancelledError:
                _finish_attempt(
                    self._metrics,
                    span,
                    provider_type=self._provider_type,
                    outcome="cancelled",
                )
                raise
            except Exception as error:
                _finish_attempt(
                    self._metrics,
                    span,
                    provider_type=self._provider_type,
                    outcome="failed",
                    error=error,
                )
                raise

            latency = _safe_latency(response.latency_ms)
            if latency is not None:
                _run_diagnostic(
                    lambda: set_span_attributes(
                        span,
                        {_MODEL_LATENCY_ATTRIBUTE_KEY: latency},
                    )
                )
            _finish_attempt(
                self._metrics,
                span,
                provider_type=self._provider_type,
                outcome="ok",
            )
            return response

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        async def iterate() -> AsyncIterator[str]:
            iterator: AsyncIterator[str] | None = None
            outcome: str | None = None
            ordinary_error: Exception | None = None
            with _attempt_span(
                self._tracer,
                provider_type=self._provider_type,
                operation="stream",
            ) as span:
                try:
                    try:
                        iterator = self._wrapped.stream(request)
                        async for chunk in iterator:
                            yield chunk
                    except GeneratorExit:
                        outcome = "cancelled"
                        raise
                    except asyncio.CancelledError:
                        outcome = "cancelled"
                        raise
                    except Exception as error:
                        outcome = "failed"
                        ordinary_error = error
                        raise
                    else:
                        outcome = "ok"
                finally:
                    active_error = sys.exc_info()[1]
                    cleanup_error: BaseException | None = None
                    cleanup_traceback: TracebackType | None = None
                    if iterator is not None:
                        cleanup_error, cleanup_traceback = await _cleanup_iterator(iterator)

                    if active_error is None and cleanup_error is not None:
                        if isinstance(cleanup_error, asyncio.CancelledError):
                            outcome = "cancelled"
                        elif isinstance(cleanup_error, Exception):
                            outcome = "failed"
                            ordinary_error = cleanup_error
                        else:
                            raise cleanup_error.with_traceback(cleanup_traceback)

                    if outcome is not None:
                        _finish_attempt(
                            self._metrics,
                            span,
                            provider_type=self._provider_type,
                            outcome=outcome,
                            error=ordinary_error,
                        )

                    if active_error is None and cleanup_error is not None:
                        raise cleanup_error.with_traceback(cleanup_traceback)

        return iterate()

    @property
    def provider_name(self) -> str:
        return str(getattr(self._wrapped, "provider_name", type(self._wrapped).__name__))

    def embedding_client(self) -> EmbeddingClient:
        """Unwrap for ``as_embedding_client``; the returned client keeps the
        attempt telemetry (``operation=embed``) of this wrapper."""
        from jhin_models.embeddings import as_embedding_client

        wrapped = as_embedding_client(self._wrapped)
        return _InstrumentedEmbeddingClient(wrapped, owner=self)

    async def _embed_instrumented(
        self,
        wrapped: EmbeddingClient,
        texts: Sequence[str],
        *,
        model: str,
        dimensions: int | None,
    ) -> EmbeddingResult:
        with _attempt_span(
            self._tracer,
            provider_type=self._provider_type,
            operation="embed",
        ) as span:
            try:
                result = await wrapped.embed(texts, model=model, dimensions=dimensions)
            except asyncio.CancelledError:
                _finish_attempt(
                    self._metrics,
                    span,
                    provider_type=self._provider_type,
                    outcome="cancelled",
                )
                raise
            except Exception as error:
                _finish_attempt(
                    self._metrics,
                    span,
                    provider_type=self._provider_type,
                    outcome="failed",
                    error=error,
                )
                raise
            latency = _safe_latency(result.latency_ms)
            if latency is not None:
                _run_diagnostic(
                    lambda: set_span_attributes(
                        span,
                        {_MODEL_LATENCY_ATTRIBUTE_KEY: latency},
                    )
                )
            _finish_attempt(
                self._metrics,
                span,
                provider_type=self._provider_type,
                outcome="ok",
            )
            return result

    def image_generation_client(self) -> ImageGenerationClient:
        """Unwrap for ``as_image_generation_client``: the adapter decides
        support, so unsupported providers still raise naming the adapter."""
        from jhin_models.images import as_image_generation_client

        return as_image_generation_client(self._wrapped)

    async def verify(self) -> str:
        with _attempt_span(
            self._tracer,
            provider_type=self._provider_type,
            operation="verify",
        ) as span:
            try:
                result = await self._wrapped.verify()
            except asyncio.CancelledError:
                _finish_attempt(
                    self._metrics,
                    span,
                    provider_type=self._provider_type,
                    outcome="cancelled",
                )
                raise
            except Exception as error:
                _finish_attempt(
                    self._metrics,
                    span,
                    provider_type=self._provider_type,
                    outcome="failed",
                    error=error,
                )
                raise
            _finish_attempt(
                self._metrics,
                span,
                provider_type=self._provider_type,
                outcome="ok",
            )
            return result

    async def _simple_attempt[T](self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        """One span + one metric point around a payload-free adapter call."""
        with _attempt_span(
            self._tracer,
            provider_type=self._provider_type,
            operation=operation,
        ) as span:
            try:
                result = await call()
            except asyncio.CancelledError:
                _finish_attempt(
                    self._metrics,
                    span,
                    provider_type=self._provider_type,
                    outcome="cancelled",
                )
                raise
            except Exception as error:
                _finish_attempt(
                    self._metrics,
                    span,
                    provider_type=self._provider_type,
                    outcome="failed",
                    error=error,
                )
                raise
            _finish_attempt(
                self._metrics,
                span,
                provider_type=self._provider_type,
                outcome="ok",
            )
            return result

    async def list_models(self) -> list[str]:
        return await self._simple_attempt("list_models", self._wrapped.list_models)

    async def list_models_detailed(self) -> list[ModelListing]:
        return await self._simple_attempt("list_models", self._wrapped.list_models_detailed)

    async def get_account_status(self) -> AccountStatus | None:
        return await self._simple_attempt("account_status", self._wrapped.get_account_status)

    async def close(self) -> None:
        await self._wrapped.close()


__all__ = ["InstrumentedModelClient"]
