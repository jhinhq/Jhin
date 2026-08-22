"""Privacy-safe Temporal tracing, propagation, and attempt metrics."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from functools import partial
from typing import Any, Literal, Protocol, cast

import nexusrpc.handler
import opentelemetry.context as otel_context
import temporalio.activity as activity
import temporalio.client
import temporalio.worker
import temporalio.workflow
from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer, get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.opentelemetry._interceptor import (
    TracingWorkflowInboundInterceptor as _SdkWorkflowInboundInterceptor,
)
from temporalio.contrib.opentelemetry._interceptor import _CompletedWorkflowSpanParams
from temporalio.converter import PayloadConverter
from temporalio.exceptions import CancelledError as TemporalCancelledError
from temporalio.exceptions import FailureError
from temporalio.worker import Worker
from temporalio.workflow import info as _sdk_workflow_info

from jhin_observability.bootstrap import ObservabilityRuntime
from jhin_observability.errors import SafeErrorCode, safe_error
from jhin_observability.metrics import JhinMetrics
from jhin_observability.registry import (
    TEMPORAL_ACTIVITY_NAMES,
    TEMPORAL_WORKFLOW_TYPE_VALUES,
)

TemporalInterceptorRole = Literal["client", "worker"]
MAX_TEMPORAL_TRACER_DATA_BYTES = 1_024
MAX_NEXUS_TRACE_CARRIER_BYTES = 1_024

type CarrierDict = dict[str, str]
type _SdkCarrierDict = dict[str, str | list[str]]
_TRACEPARENT = "traceparent"
_TRACESTATE = "tracestate"
_BAGGAGE = "baggage"
_RESERVED_PAYLOAD = "_tracer-data"
_TRACE_KEYS = frozenset({_TRACEPARENT, _TRACESTATE})
_NEXUS_TELEMETRY_KEYS = frozenset({_TRACEPARENT, _TRACESTATE, _BAGGAGE, _RESERVED_PAYLOAD})
_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-"
    r"(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_TRACESTATE_KEY_RE = re.compile(
    r"^(?:[a-z][a-z0-9_\-*/]{0,255}|"
    r"[a-z0-9][a-z0-9_\-*/]{0,240}@[a-z][a-z0-9_\-*/]{0,13})$"
)
_TRACE_PROPAGATOR = TraceContextTextMapPropagator()
_PAYLOAD_CONVERTER = PayloadConverter.default
_workflow_info = partial(_sdk_workflow_info)


class ObservabilityTemporalSettings(Protocol):
    temporal_address: str
    temporal_namespace: str


def _valid_traceparent(value: str) -> bool:
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None or match["version"] == "ff":
        return False
    if match["version"] == "00" and len(value) != 55:
        return False
    return match["trace"] != "0" * 32 and match["span"] != "0" * 16


def _valid_tracestate(value: str) -> bool:
    try:
        if len(value.encode("utf-8")) > 512:
            return False
    except Exception:
        return False
    if not value:
        return False
    members = re.split(r"[ \t]*,[ \t]*", value)
    if len(members) > 32:
        return False
    seen: set[str] = set()
    for raw_member in members:
        if not raw_member or raw_member.startswith((" ", "\t")):
            return False
        member = raw_member.rstrip(" \t")
        if member.count("=") != 1:
            return False
        key, member_value = member.split("=", 1)
        if not _TRACESTATE_KEY_RE.fullmatch(key) or key in seen:
            return False
        seen.add(key)
        if not member_value or len(member_value) > 256 or member_value[-1] == " ":
            return False
        if any(
            ord(character) < 0x20 or ord(character) > 0x7E or character in {",", "="}
            for character in member_value
        ):
            return False
    return True


def _validated_carrier_context(carrier: object) -> tuple[CarrierDict | None, Context]:
    if type(carrier) is not dict:
        return None, Context()
    typed = cast(dict[object, object], carrier)
    if not typed or set(typed) not in ({_TRACEPARENT}, _TRACE_KEYS):
        return None, Context()
    if any(type(key) is not str or type(value) is not str for key, value in typed.items()):
        return None, Context()
    traceparent = cast(str, typed[_TRACEPARENT])
    if not _valid_traceparent(traceparent):
        return None, Context()
    tracestate = cast(str | None, typed.get(_TRACESTATE))
    if tracestate is not None and not _valid_tracestate(tracestate):
        return None, Context()
    canonical: CarrierDict = {_TRACEPARENT: traceparent}
    if tracestate is not None:
        canonical[_TRACESTATE] = tracestate
    try:
        context = _TRACE_PROPAGATOR.extract(canonical)
        span_context = get_current_span(context).get_span_context()
        if not span_context.is_valid or not span_context.is_remote:
            return None, Context()
    except BaseException:
        return None, Context()
    return canonical, context


def _copy_temporal_headers(headers: Mapping[str, Payload]) -> dict[str, Payload]:
    try:
        copied = dict(headers)
    except BaseException:
        return {}
    copied.pop(_RESERVED_PAYLOAD, None)
    return copied


def encode_temporal_trace_headers(
    headers: Mapping[str, Payload],
    *,
    context: Context | None = None,
) -> dict[str, Payload]:
    """Return a sanitized copy with one bounded protobuf trace carrier."""
    copied = _copy_temporal_headers(headers)
    try:
        carrier: CarrierDict = {}
        _TRACE_PROPAGATOR.inject(carrier, context=context)
        canonical, _ = _validated_carrier_context(carrier)
        if canonical is None:
            return copied
        payloads = _PAYLOAD_CONVERTER.to_payloads([canonical])
        if payloads is None or len(payloads) != 1 or type(payloads[0]) is not Payload:
            return copied
        payload = payloads[0]
        if len(payload.SerializeToString()) > MAX_TEMPORAL_TRACER_DATA_BYTES:
            return copied
        copied[_RESERVED_PAYLOAD] = payload
    except BaseException:
        pass
    return copied


def decode_temporal_trace_carrier(
    headers: Mapping[str, Payload],
) -> tuple[CarrierDict | None, Context]:
    """Decode only the bounded canonical protobuf carrier without logging input."""
    try:
        payload = headers.get(_RESERVED_PAYLOAD)
        if type(payload) is not Payload:
            return None, Context()
        if len(payload.SerializeToString()) > MAX_TEMPORAL_TRACER_DATA_BYTES:
            return None, Context()
        values = _PAYLOAD_CONVERTER.from_payloads([payload])
        if len(values) != 1:
            return None, Context()
        return _validated_carrier_context(values[0])
    except BaseException:
        return None, Context()


def _nexus_trace_carrier_within_limit(traceparent: str, tracestate: str) -> bool:
    try:
        size = len(f"traceparent:{traceparent}\n".encode())
        if tracestate:
            size += len(f"tracestate:{tracestate}\n".encode())
        return size <= MAX_NEXUS_TRACE_CARRIER_BYTES
    except BaseException:
        return False


def _nexus_trace_carrier_size(headers: Mapping[str, str]) -> int:
    traceparent = headers.get(_TRACEPARENT, "")
    tracestate = headers.get(_TRACESTATE, "")
    return len(f"traceparent:{traceparent}\n".encode()) + (
        len(f"tracestate:{tracestate}\n".encode()) if tracestate else 0
    )


def _copy_nexus_business_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    try:
        copied = dict(headers or {})
    except BaseException:
        return {}
    return {
        key: value
        for key, value in copied.items()
        if type(key) is str and key.lower() not in _NEXUS_TELEMETRY_KEYS
    }


def encode_nexus_trace_headers(
    headers: Mapping[str, str] | None,
    *,
    context: Context | None = None,
) -> dict[str, str]:
    """Return business headers plus a bounded canonical W3C string carrier."""
    copied = _copy_nexus_business_headers(headers)
    try:
        candidate: CarrierDict = {}
        _TRACE_PROPAGATOR.inject(candidate, context=context)
        traceparent = candidate.get(_TRACEPARENT, "")
        tracestate = candidate.get(_TRACESTATE, "")
        if not _nexus_trace_carrier_within_limit(traceparent, tracestate):
            return copied
        canonical, _ = _validated_carrier_context(candidate)
        if canonical is not None:
            copied.update(canonical)
    except BaseException:
        pass
    return copied


def decode_nexus_trace_context(headers: Mapping[str, str]) -> Context:
    """Extract only canonical lowercase W3C Nexus trace headers."""
    try:
        candidate: CarrierDict = {}
        for key, value in headers.items():
            if type(key) is not str:
                return Context()
            lowered = key.lower()
            if lowered in _NEXUS_TELEMETRY_KEYS:
                if key != lowered or lowered in {_BAGGAGE, _RESERVED_PAYLOAD}:
                    return Context()
                if type(value) is not str:
                    return Context()
                candidate[key] = value
        traceparent = candidate.get(_TRACEPARENT, "")
        tracestate = candidate.get(_TRACESTATE, "")
        if not _nexus_trace_carrier_within_limit(traceparent, tracestate):
            return Context()
        _, context = _validated_carrier_context(candidate)
        return context
    except BaseException:
        return Context()


def _span_name(name: str) -> str:
    if name.startswith(("StartWorkflow:", "SignalWithStartWorkflow:")):
        return "temporal.start_workflow"
    if name.startswith("SignalWorkflow:"):
        return "temporal.signal_workflow"
    if name.startswith(("RunActivity:", "StartActivity:")):
        activity_name = name.split(":", 1)[1]
        if activity_name in TEMPORAL_ACTIVITY_NAMES:
            return f"temporal.activity.{activity_name}"
        return "temporal.activity.other"
    if name.startswith("RunStartNexusOperationHandler:") or name.startswith(
        "RunCancelNexusOperationHandler:"
    ):
        return "sandbox.server"
    return "temporal.client.other"


def normalize_temporal_attributes(attributes: Attributes | None) -> dict[str, Any]:
    """Translate the SDK's dynamic attributes into the closed registry."""
    output: dict[str, Any] = {}
    for raw_key, value in (attributes or {}).items():
        key = {
            "temporalWorkflowID": "temporal.workflow_id",
            "temporalRunID": "temporal.run_id",
            "temporalWorkflowType": "temporal.workflow_type",
            "temporalActivityType": "temporal.activity_type",
            "temporalTaskQueue": "temporal.task_queue",
            "temporalAttempt": "temporal.attempt",
        }.get(raw_key, raw_key)
        if key in {"temporal.workflow_id", "temporal.run_id"}:
            if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
                output[key] = value
        elif key == "temporal.workflow_type":
            output[key] = value if value in TEMPORAL_WORKFLOW_TYPE_VALUES else "other"
        elif key == "temporal.activity_type":
            output[key] = value if value in TEMPORAL_ACTIVITY_NAMES else "other"
        elif key == "temporal.task_queue":
            output[key] = (
                value
                if value in {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue"}
                else "other"
            )
        elif key == "temporal.attempt" and type(value) is int and 0 <= value <= 1_000_000_000:
            output[key] = value
    return output


def _is_cancellation(error: BaseException) -> bool:
    return isinstance(error, (asyncio.CancelledError, TemporalCancelledError))


def _current_otel_context() -> Context | None:
    try:
        return otel_context.get_current()
    except BaseException:
        return None


def _best_effort_restore_context(context: Context) -> None:
    current = _current_otel_context()
    if current is None or current is context:
        return
    with suppress(BaseException):
        otel_context.attach(context)


def _validate_role(value: object) -> TemporalInterceptorRole:
    if type(value) is not str or value not in {"client", "worker"}:
        raise ValueError("invalid Temporal interceptor role")
    return cast(TemporalInterceptorRole, value)


class SafeTemporalTracingInterceptor(TracingInterceptor):
    def __init__(self, tracer: Tracer, *, role: TemporalInterceptorRole) -> None:
        validated_role = _validate_role(role)
        super().__init__(tracer, always_create_workflow_spans=False)
        self.role: TemporalInterceptorRole = validated_role
        self.text_map_propagator = _TRACE_PROPAGATOR
        self.payload_converter = _PAYLOAD_CONVERTER

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        if self.role == "worker":
            return next
        return _SafeClientOutboundInterceptor(next, self)

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        if self.role == "client":
            return next
        return _SafeActivityInboundInterceptor(next, self)

    def intercept_nexus_operation(
        self, next: temporalio.worker.NexusOperationInboundInterceptor
    ) -> temporalio.worker.NexusOperationInboundInterceptor:
        if self.role == "client":
            return next
        return _SafeNexusOperationInboundInterceptor(next, self)

    def workflow_interceptor_class(  # type: ignore[override]
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[temporalio.worker.WorkflowInboundInterceptor] | None:
        if self.role == "client":
            return None
        super().workflow_interceptor_class(input)
        return TracingWorkflowInboundInterceptor

    def _context_to_headers(self, headers: Mapping[str, Payload]) -> Mapping[str, Payload]:
        return encode_temporal_trace_headers(headers)

    def _completed_workflow_span(
        self, params: _CompletedWorkflowSpanParams
    ) -> _SdkCarrierDict | None:
        try:
            canonical, _ = _validated_carrier_context(dict(params.context))
            return cast(_SdkCarrierDict | None, canonical)
        except BaseException:
            return None

    @contextmanager
    def _start_as_current_span(
        self,
        name: str,
        *,
        attributes: Attributes = None,
        input_with_headers: Any | None = None,
        input_with_ctx: Any | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        context: Context | None = None,
    ) -> Iterator[None]:
        entry_context = _current_otel_context()
        if input_with_headers is not None:
            with suppress(BaseException):
                input_with_headers.headers = _copy_temporal_headers(input_with_headers.headers)
        if input_with_ctx is not None:
            with suppress(BaseException):
                input_with_ctx.ctx = dataclasses.replace(
                    input_with_ctx.ctx,
                    headers=_copy_nexus_business_headers(input_with_ctx.ctx.headers),
                )
        if entry_context is None:
            yield None
            return
        if _current_otel_context() is not entry_context:
            _best_effort_restore_context(entry_context)
            yield None
            return

        manager: Any = None
        span: Any = None
        owned_context: Context | None = None
        try:
            manager = self.tracer.start_as_current_span(
                _span_name(name),
                attributes=normalize_temporal_attributes(attributes),
                kind=kind,
                context=context,
                record_exception=False,
                set_status_on_exception=False,
            )
            span = manager.__enter__()
            owned_context = _current_otel_context()
            if owned_context is None:
                raise RuntimeError("unavailable OTel context")
            if input_with_headers is not None:
                input_with_headers.headers = encode_temporal_trace_headers(
                    input_with_headers.headers
                )
            if input_with_ctx is not None:
                input_with_ctx.ctx = dataclasses.replace(
                    input_with_ctx.ctx,
                    headers=encode_nexus_trace_headers(input_with_ctx.ctx.headers),
                )
            if _current_otel_context() is not owned_context:
                raise RuntimeError("unstable OTel setup context")
        except BaseException:
            if manager is not None:
                with suppress(BaseException):
                    manager.__exit__(None, None, None)
            _best_effort_restore_context(entry_context)
            manager = None
            span = None
            owned_context = None

        authoritative: BaseException | None = None
        traceback: Any = None
        try:
            yield None
        except BaseException as error:
            authoritative = error
            traceback = error.__traceback__
            if span is not None and isinstance(error, Exception) and not _is_cancellation(error):
                before_error_telemetry = _current_otel_context()
                try:
                    closed = safe_error(error, code=SafeErrorCode.INTERNAL_ERROR)
                    span.set_status(Status(StatusCode.ERROR))
                    span.set_attribute("error.type", closed.type)
                    span.set_attribute("error.code", closed.code.value)
                except BaseException:
                    pass
                if before_error_telemetry is not None:
                    _best_effort_restore_context(before_error_telemetry)
        finally:
            if manager is not None:
                try:
                    if authoritative is None:
                        manager.__exit__(None, None, None)
                    else:
                        manager.__exit__(type(authoritative), authoritative, traceback)
                except BaseException:
                    pass
                _best_effort_restore_context(entry_context)
        if authoritative is not None:
            raise authoritative.with_traceback(traceback)


class _SafeClientOutboundInterceptor(temporalio.client.OutboundInterceptor):
    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        root: SafeTemporalTracingInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    async def _call(
        self,
        operation: str,
        input: Any,
        name: str,
        *,
        activity_name: bool = False,
    ) -> Any:
        try:
            workflow_id = getattr(input, "id", None)
            attributes: dict[str, Any] = {}
            if type(workflow_id) is str:
                attributes["temporalWorkflowID"] = workflow_id
            if activity_name:
                candidate = getattr(input, "activity_type", None)
                if type(candidate) is str and candidate in TEMPORAL_ACTIVITY_NAMES:
                    name = f"StartActivity:{candidate}"
        except BaseException:
            return await cast(Any, getattr(self.next, operation))(input)
        with self.root._start_as_current_span(
            name,
            attributes=attributes,
            input_with_headers=input,
            kind=SpanKind.CLIENT,
        ):
            return await cast(Any, getattr(self.next, operation))(input)

    async def start_workflow(self, input: temporalio.client.StartWorkflowInput) -> Any:
        return await self._call("start_workflow", input, "StartWorkflow:")

    async def query_workflow(self, input: temporalio.client.QueryWorkflowInput) -> Any:
        return await self._call("query_workflow", input, "QueryWorkflow:")

    async def signal_workflow(self, input: temporalio.client.SignalWorkflowInput) -> Any:
        return await self._call("signal_workflow", input, "SignalWorkflow:")

    async def start_workflow_update(self, input: temporalio.client.StartWorkflowUpdateInput) -> Any:
        return await self._call(
            "start_workflow_update",
            input,
            "StartWorkflowUpdate:",
        )

    async def start_update_with_start_workflow(
        self, input: temporalio.client.StartWorkflowUpdateWithStartInput
    ) -> Any:
        try:
            start = input.start_workflow_input
            update = input.update_workflow_input
            start_headers = _copy_temporal_headers(start.headers)
            update_headers = _copy_temporal_headers(update.headers)
            workflow_id = start.id
            attributes: dict[str, Any] = {}
            if type(workflow_id) is str:
                attributes["temporalWorkflowID"] = workflow_id
            start.headers = start_headers
            update.headers = update_headers
        except BaseException:
            return await self.next.start_update_with_start_workflow(input)
        with self.root._start_as_current_span(
            "StartUpdateWithStartWorkflow:",
            attributes=attributes,
            input_with_headers=start,
            kind=SpanKind.CLIENT,
        ):
            with suppress(BaseException):
                payload = start.headers.get(_RESERVED_PAYLOAD)
                if type(payload) is Payload:
                    update.headers = {**update.headers, _RESERVED_PAYLOAD: payload}
            return await self.next.start_update_with_start_workflow(input)

    async def start_activity(self, input: temporalio.client.StartActivityInput) -> Any:
        return await self._call(
            "start_activity",
            input,
            "StartActivity:",
            activity_name=True,
        )


class _SafeActivityInboundInterceptor(temporalio.worker.ActivityInboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        root: SafeTemporalTracingInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    async def execute_activity(self, input: temporalio.worker.ExecuteActivityInput) -> Any:
        context = Context()
        name = "other"
        attributes: dict[str, Any] = {}
        try:
            context = decode_temporal_trace_carrier(input.headers)[1]
            info = activity.info()
            name = info.activity_type
            attributes = {
                "temporalWorkflowID": info.workflow_id,
                "temporalRunID": info.workflow_run_id,
                "temporalActivityType": info.activity_type,
            }
        except BaseException:
            pass
        with self.root._start_as_current_span(
            f"RunActivity:{name}",
            context=context,
            attributes=attributes,
            kind=SpanKind.SERVER,
        ):
            return await self.next.execute_activity(input)


class _SafeNexusOperationInboundInterceptor(temporalio.worker.NexusOperationInboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.NexusOperationInboundInterceptor,
        root: SafeTemporalTracingInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    async def execute_nexus_operation_start(
        self, input: temporalio.worker.ExecuteNexusOperationStartInput
    ) -> (
        nexusrpc.handler.StartOperationResultSync[Any] | nexusrpc.handler.StartOperationResultAsync
    ):
        context = Context()
        with suppress(BaseException):
            context = decode_nexus_trace_context(input.ctx.headers)
        with self.root._start_as_current_span(
            "RunStartNexusOperationHandler:other",
            attributes={},
            input_with_ctx=input,
            kind=SpanKind.SERVER,
            context=context,
        ):
            return await self.next.execute_nexus_operation_start(input)

    async def execute_nexus_operation_cancel(
        self, input: temporalio.worker.ExecuteNexusOperationCancelInput
    ) -> None:
        context = Context()
        with suppress(BaseException):
            context = decode_nexus_trace_context(input.ctx.headers)
        with self.root._start_as_current_span(
            "RunCancelNexusOperationHandler:other",
            attributes={},
            input_with_ctx=input,
            kind=SpanKind.SERVER,
            context=context,
        ):
            return await self.next.execute_nexus_operation_cancel(input)


@contextmanager
def _workflow_attached(
    interceptor: TracingWorkflowInboundInterceptor,
    input: object,
) -> Iterator[None]:
    entry_context = _current_otel_context()
    if entry_context is None:
        yield None
        return
    context = Context()
    with suppress(BaseException):
        context = decode_temporal_trace_carrier(cast(Any, input).headers)[1]
    try:
        context = interceptor._set_on_context(context)
        token = otel_context.attach(context)
    except BaseException:
        _best_effort_restore_context(entry_context)
        yield None
        return
    owned_context = _current_otel_context()
    if owned_context is None or owned_context is not context:
        with suppress(BaseException):
            otel_context.detach(token)
        _best_effort_restore_context(entry_context)
        yield None
        return
    try:
        yield None
    finally:
        with suppress(BaseException):
            otel_context.detach(token)
        _best_effort_restore_context(entry_context)


class TracingWorkflowInboundInterceptor(_SdkWorkflowInboundInterceptor):
    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        temporalio.worker.WorkflowInboundInterceptor.init(
            self, _SafeWorkflowOutboundInterceptor(outbound, self)
        )

    def _context_to_headers(self, headers: Mapping[str, Payload]) -> Mapping[str, Payload]:
        return encode_temporal_trace_headers(headers)

    async def execute_workflow(self, input: temporalio.worker.ExecuteWorkflowInput) -> Any:
        workflow_type = "other"
        with suppress(BaseException):
            candidate = _workflow_info().workflow_type
            if type(candidate) is str and candidate in TEMPORAL_WORKFLOW_TYPE_VALUES:
                workflow_type = candidate

        result: Any = None
        authoritative: BaseException | None = None
        traceback: Any = None
        with _workflow_attached(self, input):
            with suppress(BaseException):
                self._completed_span(
                    f"RunWorkflow:{workflow_type}",
                    kind=SpanKind.SERVER,
                )
            try:
                result = await self.next.execute_workflow(input)
            except BaseException as error:
                authoritative = error
                traceback = error.__traceback__
            if authoritative is None or isinstance(authoritative, FailureError):
                with suppress(BaseException):
                    self._completed_span(
                        f"CompleteWorkflow:{workflow_type}",
                        exception=(
                            authoritative if isinstance(authoritative, FailureError) else None
                        ),
                        kind=SpanKind.INTERNAL,
                    )
        if authoritative is not None:
            raise authoritative.with_traceback(traceback)
        return result

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        with _workflow_attached(self, input):
            return await self.next.handle_signal(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        with _workflow_attached(self, input):
            return await self.next.handle_query(input)

    def handle_update_validator(self, input: temporalio.worker.HandleUpdateInput) -> None:
        with _workflow_attached(self, input):
            return self.next.handle_update_validator(input)

    async def handle_update_handler(self, input: temporalio.worker.HandleUpdateInput) -> Any:
        with _workflow_attached(self, input):
            return await self.next.handle_update_handler(input)


class _SafeWorkflowOutboundInterceptor(temporalio.worker.WorkflowOutboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.WorkflowOutboundInterceptor,
        root: TracingWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self.root = root

    def continue_as_new(self, input: temporalio.worker.ContinueAsNewInput) -> Any:
        with suppress(BaseException):
            input.headers = self.root._context_to_headers(input.headers)
        return self.next.continue_as_new(input)

    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        with suppress(BaseException):
            input.headers = self.root._context_to_headers(input.headers)
        return await self.next.signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        with suppress(BaseException):
            input.headers = self.root._context_to_headers(input.headers)
        return await self.next.signal_external_workflow(input)

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle[Any]:
        with suppress(BaseException):
            input.headers = self.root._context_to_headers(input.headers)
        return self.next.start_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle[Any, Any]:
        with suppress(BaseException):
            input.headers = self.root._context_to_headers(input.headers)
        return await self.next.start_child_workflow(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle[Any]:
        with suppress(BaseException):
            input.headers = self.root._context_to_headers(input.headers)
        return self.next.start_local_activity(input)

    async def start_nexus_operation(
        self, input: temporalio.worker.StartNexusOperationInput[Any, Any]
    ) -> temporalio.workflow.NexusOperationHandle[Any]:
        with suppress(BaseException):
            input.headers = encode_nexus_trace_headers(input.headers)
        return await self.next.start_nexus_operation(input)


def _failure_class(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, PermissionError):
        return "authorization"
    if isinstance(error, (ConnectionError, OSError)):
        return "transport"
    if isinstance(error, (TypeError, ValueError)):
        return "validation"
    return "internal"


class TemporalActivityMetricsInterceptor(temporalio.worker.Interceptor):
    def __init__(self, metrics: JhinMetrics, *, task_queue: str) -> None:
        self.metrics = metrics
        self.task_queue = task_queue

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        return _TemporalActivityMetricsInbound(next, self.metrics, self.task_queue)


class _TemporalActivityMetricsInbound(temporalio.worker.ActivityInboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        metrics: JhinMetrics,
        task_queue: str,
    ) -> None:
        super().__init__(next)
        self.metrics = metrics
        self.task_queue = task_queue

    async def execute_activity(self, input: temporalio.worker.ExecuteActivityInput) -> Any:
        try:
            return await self.next.execute_activity(input)
        except BaseException as error:
            if _is_cancellation(error) or not isinstance(error, Exception):
                raise
            try:
                activity_name = activity.info().activity_type
                self.metrics.counter("temporal_activity_failures").add(
                    1,
                    task_queue=(
                        self.task_queue
                        if self.task_queue
                        in {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue"}
                        else "other"
                    ),
                    activity=(
                        activity_name if activity_name in TEMPORAL_ACTIVITY_NAMES else "other"
                    ),
                    failure_class=_failure_class(error),
                )
            except BaseException:
                pass
            raise


def temporal_client_interceptors(
    runtime: ObservabilityRuntime,
) -> list[temporalio.client.Interceptor]:
    return [SafeTemporalTracingInterceptor(runtime.tracer, role="client")]


def temporal_worker_interceptors(
    runtime: ObservabilityRuntime,
    *,
    task_queue: str,
) -> list[temporalio.worker.Interceptor]:
    return [
        SafeTemporalTracingInterceptor(runtime.tracer, role="worker"),
        TemporalActivityMetricsInterceptor(runtime.metrics, task_queue=task_queue),
    ]


async def connect_temporal_client(
    settings: ObservabilityTemporalSettings,
    runtime: ObservabilityRuntime,
) -> Client:
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        interceptors=temporal_client_interceptors(runtime),
    )


def build_temporal_worker(
    client: Client,
    *,
    runtime: ObservabilityRuntime,
    task_queue: str,
    workflows: Sequence[type[Any]],
    activities: Sequence[Callable[..., Any]],
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
        interceptors=temporal_worker_interceptors(runtime, task_queue=task_queue),
    )


__all__ = [
    "MAX_TEMPORAL_TRACER_DATA_BYTES",
    "ObservabilityTemporalSettings",
    "SafeTemporalTracingInterceptor",
    "TemporalActivityMetricsInterceptor",
    "TemporalInterceptorRole",
    "TracingWorkflowInboundInterceptor",
    "build_temporal_worker",
    "connect_temporal_client",
    "decode_nexus_trace_context",
    "decode_temporal_trace_carrier",
    "encode_nexus_trace_headers",
    "encode_temporal_trace_headers",
    "normalize_temporal_attributes",
    "temporal_client_interceptors",
    "temporal_worker_interceptors",
]
