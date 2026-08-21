"""Bounded, trace-only NATS telemetry seams."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager, suppress
from typing import Literal, Protocol, cast

from nats.js.api import PubAck
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, Tracer

from jhin_events.subjects import EVENT_DOMAINS, event_subject, ingress_subject
from jhin_observability import (
    SPAN_ATTRIBUTE_VALUES,
    TRACE_CARRIER_KEYS,
    SafeErrorCode,
    extract_trace_context,
    get_logger,
    inject_trace_headers,
    is_sensitive_key_name,
    record_span_error,
    safe_error,
    safe_span,
)

StreamName = Literal["INGRESS", "EVENTS", "DLQ"]
DlqOriginStream = Literal["INGRESS", "EVENTS"]
ConsumerName = Literal["event-worker-ingress", "event-worker"]

MSG_ID_HEADER = "Nats-Msg-Id"
MAX_NATS_HEADERS = 32
MAX_NATS_HEADER_NAME_BYTES = 64
MAX_NATS_HEADER_VALUE_BYTES = 1_024
MAX_NATS_HEADER_TOTAL_BYTES = 8_192

_NATS_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\Z", re.ASCII)
_NATS_HEADER_PREFIX_BYTES = len(b"NATS/1.0\r\n")
_NATS_HEADER_SUFFIX_BYTES = len(b"\r\n")

logger = get_logger(__name__)

assert set(SPAN_ATTRIBUTE_VALUES["jhin.subject_family"]) == (
    set(EVENT_DOMAINS) | {"ingress", "dlq", "other"}
)


class UnsafeNatsHeaderError(ValueError):
    """A caller supplied a header outside the closed NATS boundary."""

    def __init__(self) -> None:
        super().__init__("invalid NATS header")


class JetStreamPublisher(Protocol):
    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        *,
        headers: Mapping[str, str] | None = None,
    ) -> PubAck: ...


class ConsumerInfo(Protocol):
    num_pending: object


class ConsumerInfoClient(Protocol):
    async def consumer_info(self, stream: str, consumer: str) -> ConsumerInfo: ...


class NatsMessage(Protocol):
    subject: str
    headers: dict[str, str] | None

    async def nak(self, *, delay: int) -> None: ...


MessageHandler = Callable[[NatsMessage], Awaitable[None]]


def classify_subject(subject: str) -> tuple[StreamName, str]:
    """Return only the closed stream/family derived from a canonical subject."""
    if type(subject) is not str:
        raise ValueError("unsupported Jhin subject")
    if subject == "jhin.dlq.ingress" or subject == "jhin.dlq.events":
        return "DLQ", "dlq"

    parts = subject.split(".")
    if len(parts) < 5 or parts[:2] != ["jhin", "v1"]:
        raise ValueError("unsupported Jhin subject")
    workspace = parts[2]
    family = parts[3]
    try:
        if family == "ingress":
            if len(parts) < 6:
                raise ValueError
            canonical = ingress_subject(workspace, parts[4], ".".join(parts[5:]))
            stream: StreamName = "INGRESS"
        elif family in EVENT_DOMAINS:
            canonical = event_subject(workspace, ".".join(parts[3:]))
            stream = "EVENTS"
        else:
            raise ValueError("unsupported Jhin subject family")
    except ValueError as exc:
        if str(exc) == "unsupported Jhin subject family":
            raise
        raise ValueError("unsupported Jhin subject") from None
    if canonical != subject:
        raise ValueError("unsupported Jhin subject")
    return stream, family


def validate_stream_subject(stream: StreamName, subject: str) -> str:
    actual_stream, family = classify_subject(subject)
    if actual_stream != stream:
        raise ValueError("stream/subject mismatch")
    return family


def _validate_header_pair(key: object, value: object) -> tuple[str, str, str]:
    if type(key) is not str or type(value) is not str:
        raise UnsafeNatsHeaderError
    if any(character in key or character in value for character in ("\r", "\n", "\x00")):
        raise UnsafeNatsHeaderError
    try:
        encoded_key = key.encode("ascii", errors="strict")
        encoded_value = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise UnsafeNatsHeaderError from None
    if (
        not _NATS_HEADER_NAME_RE.fullmatch(key)
        or len(encoded_key) > MAX_NATS_HEADER_NAME_BYTES
        or len(encoded_value) > MAX_NATS_HEADER_VALUE_BYTES
        or is_sensitive_key_name(key)
    ):
        raise UnsafeNatsHeaderError
    return key, value, key.lower()


def _nats_wire_size(headers: Mapping[str, str]) -> int:
    size = _NATS_HEADER_PREFIX_BYTES + _NATS_HEADER_SUFFIX_BYTES
    for key, value in headers.items():
        size += len(key.encode("ascii"))
        size += len(b": ")
        size += len(value.strip().encode("utf-8"))
        size += len(b"\r\n")
    return size


def _validate_final_headers(headers: Mapping[str, str]) -> dict[str, str]:
    if len(headers) > MAX_NATS_HEADERS:
        raise UnsafeNatsHeaderError
    output: dict[str, str] = {}
    seen: set[str] = set()
    for raw_key, raw_value in headers.items():
        key, value, normalized = _validate_header_pair(raw_key, raw_value)
        if normalized in seen:
            raise UnsafeNatsHeaderError
        seen.add(normalized)
        output[key] = value
    if _nats_wire_size(output) > MAX_NATS_HEADER_TOTAL_BYTES:
        raise UnsafeNatsHeaderError
    return output


def _prepare_base_headers(
    headers: Mapping[str, str] | None,
    *,
    message_id: str | None,
) -> dict[str, str]:
    try:
        copied = {} if headers is None else dict(headers)
    except Exception:
        raise UnsafeNatsHeaderError from None
    output: dict[str, str] = {}
    seen_ordinary: set[str] = set()
    for raw_key, raw_value in copied.items():
        key, value, normalized = _validate_header_pair(raw_key, raw_value)
        if normalized in TRACE_CARRIER_KEYS or normalized == MSG_ID_HEADER.lower():
            continue
        if normalized in seen_ordinary:
            raise UnsafeNatsHeaderError
        seen_ordinary.add(normalized)
        output[key] = value
    if message_id is not None:
        key, value, _normalized = _validate_header_pair(MSG_ID_HEADER, message_id)
        output[key] = value
    return _validate_final_headers(output)


def _inject_bounded_trace_headers(base_headers: Mapping[str, str]) -> dict[str, str]:
    try:
        return _validate_final_headers(inject_trace_headers(base_headers))
    except Exception:
        return dict(base_headers)


def _enter_span(
    name: Literal["nats.publish", "nats.consume"],
    *,
    tracer: Tracer,
    kind: SpanKind,
    attributes: Mapping[str, str],
    context: Context | None = None,
) -> tuple[AbstractContextManager[Span] | None, Span | None]:
    manager: AbstractContextManager[Span] | None = None
    try:
        manager = safe_span(
            name,
            tracer=tracer,
            kind=kind,
            attributes=attributes,
            context=context,
        )
        return manager, manager.__enter__()
    except Exception:
        if manager is not None:
            with suppress(Exception):
                manager.__exit__(*sys.exc_info())
        return None, None


def _close_span(manager: AbstractContextManager[Span] | None) -> None:
    if manager is not None:
        with suppress(Exception):
            manager.__exit__(*sys.exc_info())


def _set_span_attribute(span: Span | None, key: str, value: str) -> None:
    if span is not None:
        with suppress(Exception):
            span.set_attribute(key, value)


def _record_closed_error(span: Span | None, error: Exception) -> None:
    if span is not None:
        with suppress(Exception):
            record_span_error(
                span,
                safe_error(error, code=SafeErrorCode.INTERNAL_ERROR),
            )


async def publish_jetstream(
    js: JetStreamPublisher,
    subject: str,
    payload: bytes,
    *,
    tracer: Tracer,
    headers: Mapping[str, str] | None = None,
    message_id: str | None = None,
    stream: StreamName,
) -> PubAck:
    """Validate once, instrument fail-open, and publish exactly once."""
    family = validate_stream_subject(stream, subject)
    base_headers = _prepare_base_headers(headers, message_id=message_id)
    manager, span = _enter_span(
        "nats.publish",
        tracer=tracer,
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "nats",
            "jhin.stream": stream,
            "jhin.subject_family": family,
        },
    )
    final_headers = (
        _inject_bounded_trace_headers(base_headers) if manager is not None else base_headers
    )
    try:
        result = await js.publish(subject, payload, headers=final_headers)
    except asyncio.CancelledError:
        _set_span_attribute(span, "jhin.outcome", "cancelled")
        raise
    except Exception as exc:
        _set_span_attribute(span, "jhin.outcome", "failed")
        _record_closed_error(span, exc)
        raise
    else:
        _set_span_attribute(span, "jhin.outcome", "ok")
        return result
    finally:
        _close_span(manager)


async def publish_invalid_envelope_dlq(
    js: JetStreamPublisher,
    *,
    origin_stream: object,
    error_count: int,
    tracer: Tracer,
) -> PubAck:
    if type(origin_stream) is not str or origin_stream not in {"INGRESS", "EVENTS"}:
        raise ValueError("invalid DLQ origin stream")
    validated_origin = cast(DlqOriginStream, origin_stream)
    if type(error_count) is not int or not 0 <= error_count <= 1_000:
        raise ValueError("invalid DLQ error count")
    payload = json.dumps(
        {
            "schema_version": 1,
            "reason": "invalid_envelope",
            "origin_stream": validated_origin,
            "error_count": error_count,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return await publish_jetstream(
        js,
        f"jhin.dlq.{validated_origin.lower()}",
        payload,
        tracer=tracer,
        stream="DLQ",
    )


def _closed_consumer_metadata(
    stream: str,
    durable: str,
    subject: object,
) -> tuple[str, str, str]:
    safe_stream = stream if stream in {"INGRESS", "EVENTS"} else "other"
    safe_consumer = durable if durable in {"event-worker-ingress", "event-worker"} else "other"
    family = "other"
    if type(subject) is str:
        with suppress(ValueError):
            actual_stream, candidate = classify_subject(subject)
            if actual_stream == safe_stream:
                family = candidate
    return safe_stream, safe_consumer, family


async def dispatch_or_nak(
    message: NatsMessage,
    *,
    tracer: Tracer,
    stream: str,
    durable: str,
    handler: MessageHandler,
) -> None:
    """Run one handler and its settlement inside one fail-open consumer span."""
    safe_stream, safe_consumer, family = _closed_consumer_metadata(
        stream,
        durable,
        getattr(message, "subject", None),
    )
    parent: Context | None = None
    try:
        headers = getattr(message, "headers", None)
        parent = extract_trace_context(headers if isinstance(headers, Mapping) else {})
    except Exception:
        parent = None
    manager, span = _enter_span(
        "nats.consume",
        tracer=tracer,
        kind=SpanKind.CONSUMER,
        context=parent,
        attributes={
            "messaging.system": "nats",
            "jhin.stream": safe_stream,
            "jhin.consumer": safe_consumer,
            "jhin.subject_family": family,
        },
    )
    try:
        try:
            await handler(message)
        except asyncio.CancelledError:
            _set_span_attribute(span, "jhin.outcome", "cancelled")
            raise
        except Exception as handler_error:
            handler_traceback = handler_error.__traceback__
            _set_span_attribute(span, "jhin.outcome", "failed")
            _record_closed_error(span, handler_error)
            with suppress(Exception):
                logger.exception(
                    "jetstream.consumer_handler_failed",
                    stream=safe_stream,
                    consumer=safe_consumer,
                    error_type=type(handler_error).__name__,
                    error_code=SafeErrorCode.INTERNAL_ERROR.value,
                )
            try:
                await message.nak(delay=2)
            except asyncio.CancelledError:
                _set_span_attribute(span, "jhin.outcome", "cancelled")
                raise
            except Exception as settlement_error:
                _record_closed_error(span, settlement_error)
                raise handler_error.with_traceback(handler_traceback) from None
        else:
            _set_span_attribute(span, "jhin.outcome", "ok")
    finally:
        _close_span(manager)


__all__ = [
    "MAX_NATS_HEADERS",
    "MAX_NATS_HEADER_NAME_BYTES",
    "MAX_NATS_HEADER_TOTAL_BYTES",
    "MAX_NATS_HEADER_VALUE_BYTES",
    "MSG_ID_HEADER",
    "ConsumerInfo",
    "ConsumerInfoClient",
    "ConsumerName",
    "DlqOriginStream",
    "JetStreamPublisher",
    "MessageHandler",
    "NatsMessage",
    "StreamName",
    "UnsafeNatsHeaderError",
    "classify_subject",
    "dispatch_or_nak",
    "publish_invalid_envelope_dlq",
    "publish_jetstream",
    "validate_stream_subject",
]
