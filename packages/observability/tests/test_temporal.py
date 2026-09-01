"""Temporal 1.31 compatibility, privacy, propagation, and fail-open contracts."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib
import inspect
import logging
import tomllib
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from contextvars import Context as ContextVarsContext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast, get_args, get_origin, get_type_hints

import pytest
import temporalio
import temporalio.activity
import temporalio.client
import temporalio.common
import temporalio.worker
import temporalio.workflow
from nexusrpc.handler import CancelOperationContext, StartOperationContext
from opentelemetry.context import Context
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode, get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from temporalio.api.common.v1 import Payload
from temporalio.api.enums.v1 import EventType
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.opentelemetry._interceptor import (
    TracingWorkflowInboundInterceptor as SdkWorkflowInboundInterceptor,
)
from temporalio.contrib.opentelemetry._interceptor import _CompletedWorkflowSpanParams
from temporalio.converter import PayloadConverter
from temporalio.exceptions import CancelledError as TemporalCancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ExecuteActivityInput,
    ExecuteNexusOperationCancelInput,
    ExecuteNexusOperationStartInput,
    ExecuteWorkflowInput,
    HandleQueryInput,
    HandleSignalInput,
    HandleUpdateInput,
    StartActivityInput,
    StartNexusOperationInput,
    WorkflowInterceptorClassInput,
)

import jhin_agent_worker.resources as agent_resources
from jhin_observability import JhinMetrics, ObservabilityRuntime, noop_metrics, noop_tracer
from jhin_observability import metrics as metrics_module

REPO_ROOT = Path(__file__).resolve().parents[3]
TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
SECOND_TRACEPARENT = "00-11111111111111111111111111111111-2222222222222222-01"


async def _async_result(value: Any) -> Any:
    return value


@temporalio.workflow.defn(name="Task6TelemetryGraphWorkflow", sandboxed=False)
class _Task6TelemetryGraphWorkflow:
    def __init__(self) -> None:
        self._completed = 0

    async def _activity(self, label: str) -> None:
        await temporalio.workflow.execute_activity(
            "reason_agent_step",
            label,
            result_type=str,
            start_to_close_timeout=timedelta(seconds=5),
        )
        self._completed += 1

    @temporalio.workflow.run
    async def run(self, prefix: str) -> int:
        await self._activity(f"{prefix}-start")
        await temporalio.workflow.wait_condition(lambda: self._completed >= 3)
        return self._completed

    @temporalio.workflow.signal(name="trace_signal")
    async def trace_signal(self, label: str) -> None:
        await self._activity(label)

    @temporalio.workflow.update(name="trace_update")
    async def trace_update(self, label: str) -> str:
        await self._activity(label)
        return label


def _temporal() -> Any:
    """Import late so the pre-production RED remains collection-safe."""
    return importlib.import_module("jhin_observability.temporal")


@contextmanager
def _recording_tracer() -> Any:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield provider.get_tracer("task-6-test"), exporter
    finally:
        provider.shutdown()


def _json_payload_with_serialized_size(payload: Payload, target: int) -> Payload:
    assert payload.metadata.get("encoding") == b"json/plain"
    compact = payload.data.rstrip(b" \t\r\n")
    for padding in range(target + 1):
        candidate = Payload(metadata=dict(payload.metadata), data=compact + b" " * padding)
        wire = candidate.SerializeToString()
        if len(wire) != target:
            continue
        round_tripped = Payload()
        round_tripped.ParseFromString(wire)
        assert round_tripped.data == candidate.data
        assert round_tripped.metadata == candidate.metadata
        assert round_tripped.SerializeToString() == wire
        return candidate
    raise AssertionError(f"cannot construct wire-stable {target}-byte JSON Payload")


def _start_activity_input(
    label: str,
    *,
    headers: Mapping[str, Payload] | None = None,
) -> StartActivityInput:
    return StartActivityInput(
        activity="reason_agent_step",
        args=(label,),
        activity_id=None,
        task_queue=None,
        schedule_to_close_timeout=None,
        schedule_to_start_timeout=None,
        start_to_close_timeout=timedelta(seconds=5),
        heartbeat_timeout=None,
        retry_policy=None,
        cancellation_type=temporalio.workflow.ActivityCancellationType.TRY_CANCEL,
        headers=dict(headers or {}),
        disable_eager_execution=False,
        versioning_intent=None,
        summary=None,
        priority=temporalio.common.Priority(),
        arg_types=None,
        ret_type=str,
    )


def _valid_payload(traceparent: str = TRACEPARENT, *, tracestate: str | None = None) -> Payload:
    carrier = {"traceparent": traceparent}
    if tracestate is not None:
        carrier["tracestate"] = tracestate
    payloads = PayloadConverter.default.to_payloads([carrier])
    assert payloads is not None and len(payloads) == 1
    return payloads[0]


def _span_context(carrier: Mapping[str, str]) -> Context:
    return TraceContextTextMapPropagator().extract(carrier)


def _traceback_tail(traceback: Any) -> Any:
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    return traceback


class _ClientTerminal:
    def __init__(self, *, result: object = None, failure: BaseException | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.result = result
        self.failure = failure

    async def _call(self, operation: str, input: object) -> object:
        self.calls.append((operation, input))
        if self.failure is not None:
            raise self.failure
        return self.result

    async def start_workflow(self, input: object) -> object:
        return await self._call("start_workflow", input)

    async def query_workflow(self, input: object) -> object:
        return await self._call("query_workflow", input)

    async def signal_workflow(self, input: object) -> object:
        return await self._call("signal_workflow", input)

    async def start_workflow_update(self, input: object) -> object:
        return await self._call("start_workflow_update", input)

    async def start_update_with_start_workflow(self, input: object) -> object:
        return await self._call("start_update_with_start_workflow", input)

    async def start_activity(self, input: object) -> object:
        return await self._call("start_activity", input)


def _client_input(operation: str, headers: Mapping[str, Payload] | None = None) -> Any:
    common = {
        "headers": dict(headers or {}),
        "id": "workflow-id",
        "run_id": None,
        "workflow": "AgentTaskWorkflow",
        "signal": "approved",
        "query": "status",
        "update": "change",
        "update_id": "update-id",
        "activity_type": "reason_agent_step",
    }
    if operation == "start_update_with_start_workflow":
        return SimpleNamespace(
            start_workflow_input=SimpleNamespace(**common),
            update_workflow_input=SimpleNamespace(**common),
        )
    return SimpleNamespace(**common)


class _ActivityTerminal:
    def __init__(self, result: object = None, failure: BaseException | None = None) -> None:
        self.calls = 0
        self.result = result
        self.failure = failure
        self.failure_traceback: Any = None

    async def execute_activity(self, _input: object) -> object:
        self.calls += 1
        if self.failure is not None:
            try:
                raise self.failure
            except BaseException as error:
                self.failure_traceback = _traceback_tail(error.__traceback__)
                raise
        return self.result


class _NexusTerminal:
    def __init__(self, result: object = None, failure: BaseException | None = None) -> None:
        self.start_calls = 0
        self.cancel_calls = 0
        self.result = result
        self.failure = failure
        self.start_inputs: list[object] = []
        self.cancel_inputs: list[object] = []

    async def execute_nexus_operation_start(self, input: object) -> object:
        self.start_calls += 1
        self.start_inputs.append(input)
        if self.failure is not None:
            raise self.failure
        return self.result

    async def execute_nexus_operation_cancel(self, input: object) -> None:
        self.cancel_calls += 1
        self.cancel_inputs.append(input)
        if self.failure is not None:
            raise self.failure


def _start_context(headers: Mapping[str, str]) -> StartOperationContext:
    return StartOperationContext(
        service="private-service-canary",
        operation="private-operation-canary",
        headers=headers,
        task_cancellation=cast(Any, object()),
        request_id="request-canary",
    )


def _cancel_context(headers: Mapping[str, str]) -> CancelOperationContext:
    return CancelOperationContext(
        service="private-service-canary",
        operation="private-operation-canary",
        headers=headers,
        task_cancellation=cast(Any, object()),
    )


def test_temporal_131_private_surface_matches_exact_pin() -> None:
    module = _temporal()
    project = tomllib.loads(
        (REPO_ROOT / "packages/observability/pyproject.toml").read_text(encoding="utf-8")
    )
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"].count("temporalio==1.31.0") == 1
    assert [
        (item["name"], item["version"]) for item in lock["package"] if item["name"] == "temporalio"
    ] == [("temporalio", "1.31.0")]
    assert temporalio.__version__ == "1.31.0"
    root_init = inspect.signature(TracingInterceptor.__init__).parameters
    assert tuple(root_init) == ("self", "tracer", "always_create_workflow_spans")
    assert [parameter.kind for parameter in root_init.values()] == [
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    ]
    assert root_init["tracer"].default is None
    assert root_init["always_create_workflow_spans"].default is False
    root_hints = get_type_hints(TracingInterceptor.__init__)
    assert type(None) in get_args(root_hints["tracer"])
    assert root_hints["always_create_workflow_spans"] is bool
    assert root_hints["return"] is type(None)

    start = inspect.signature(TracingInterceptor._start_as_current_span).parameters
    assert tuple(start) == (
        "self",
        "name",
        "attributes",
        "input_with_headers",
        "input_with_ctx",
        "kind",
        "context",
    )
    assert all(
        start[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("attributes", "input_with_headers", "input_with_ctx", "kind", "context")
    )
    assert start["attributes"].default is inspect.Parameter.empty
    assert start["input_with_headers"].default is None
    assert start["input_with_ctx"].default is None
    assert start["kind"].default is inspect.Parameter.empty
    assert start["context"].default is None
    start_hints = get_type_hints(TracingInterceptor._start_as_current_span)
    assert start_hints["name"] is str
    assert start_hints["kind"] is SpanKind
    assert get_origin(start_hints["return"]) is not None

    completed = inspect.signature(TracingInterceptor._completed_workflow_span).parameters
    assert tuple(completed) == ("self", "params")
    completed_hints = get_type_hints(TracingInterceptor._completed_workflow_span)
    assert completed_hints["params"] is _CompletedWorkflowSpanParams
    assert type(None) in get_args(completed_hints["return"])
    assert tuple(_CompletedWorkflowSpanParams.__dataclass_fields__) == (
        "context",
        "name",
        "attributes",
        "time_ns",
        "link_context",
        "exception",
        "kind",
        "parent_missing",
    )
    assert _CompletedWorkflowSpanParams.__dataclass_params__.frozen is True

    worker_inputs = {
        ExecuteActivityInput: ("fn", "args", "executor", "headers"),
        ExecuteWorkflowInput: ("type", "run_fn", "args", "headers"),
        HandleSignalInput: ("signal", "args", "headers"),
        HandleQueryInput: ("id", "query", "args", "headers"),
        HandleUpdateInput: ("id", "update", "args", "headers"),
        ExecuteNexusOperationStartInput: ("ctx", "input"),
        ExecuteNexusOperationCancelInput: ("ctx", "token"),
        StartNexusOperationInput: (
            "endpoint",
            "service",
            "operation",
            "input",
            "schedule_to_close_timeout",
            "schedule_to_start_timeout",
            "start_to_close_timeout",
            "cancellation_type",
            "headers",
            "summary",
            "output_type",
        ),
    }
    for input_type, expected_fields in worker_inputs.items():
        signature = inspect.signature(input_type)
        assert tuple(signature.parameters) == expected_fields
        assert tuple(field.name for field in dataclasses.fields(input_type)) == expected_fields
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in signature.parameters.values()
        )
        hints = get_type_hints(input_type)
        if "headers" in expected_fields:
            if input_type is StartNexusOperationInput:
                assert type(None) in get_args(hints["headers"])
                assert any(get_origin(item) is Mapping for item in get_args(hints["headers"]))
            else:
                assert get_origin(hints["headers"]) is Mapping
    assert inspect.signature(StartNexusOperationInput).parameters["output_type"].default is None
    assert get_type_hints(ExecuteActivityInput)["headers"] == Mapping[str, Payload]
    assert get_type_hints(ExecuteWorkflowInput)["headers"] == Mapping[str, Payload]
    assert get_type_hints(ExecuteNexusOperationStartInput)["ctx"] is StartOperationContext
    assert get_type_hints(ExecuteNexusOperationCancelInput)["ctx"] is CancelOperationContext

    client_inputs = {
        temporalio.client.StartWorkflowInput: (
            "workflow",
            "args",
            "id",
            "task_queue",
            "execution_timeout",
            "run_timeout",
            "task_timeout",
            "id_reuse_policy",
            "id_conflict_policy",
            "retry_policy",
            "cron_schedule",
            "memo",
            "search_attributes",
            "start_delay",
            "headers",
            "start_signal",
            "start_signal_args",
            "static_summary",
            "static_details",
            "ret_type",
            "rpc_metadata",
            "rpc_timeout",
            "request_eager_start",
            "priority",
            "callbacks",
            "links",
            "request_id",
            "versioning_override",
        ),
        temporalio.client.SignalWorkflowInput: (
            "id",
            "run_id",
            "signal",
            "args",
            "headers",
            "rpc_metadata",
            "rpc_timeout",
        ),
        temporalio.client.QueryWorkflowInput: (
            "id",
            "run_id",
            "query",
            "args",
            "reject_condition",
            "headers",
            "ret_type",
            "rpc_metadata",
            "rpc_timeout",
        ),
        temporalio.client.StartWorkflowUpdateInput: (
            "id",
            "run_id",
            "first_execution_run_id",
            "update_id",
            "update",
            "args",
            "wait_for_stage",
            "headers",
            "ret_type",
            "rpc_metadata",
            "rpc_timeout",
            "callbacks",
            "links",
            "request_id",
        ),
        temporalio.client.StartWorkflowUpdateWithStartInput: (
            "start_workflow_input",
            "update_workflow_input",
            "rpc_metadata",
            "rpc_timeout",
            "_on_start",
            "_on_start_error",
        ),
        temporalio.client.StartActivityInput: (
            "activity_type",
            "args",
            "id",
            "task_queue",
            "result_type",
            "schedule_to_close_timeout",
            "start_to_close_timeout",
            "schedule_to_start_timeout",
            "heartbeat_timeout",
            "id_reuse_policy",
            "id_conflict_policy",
            "retry_policy",
            "priority",
            "search_attributes",
            "summary",
            "start_delay",
            "headers",
            "rpc_metadata",
            "rpc_timeout",
        ),
    }
    client_namespace = {
        **vars(importlib.import_module("temporalio.client._interceptor")),
        **vars(temporalio.client),
        "Self": Self,
    }
    for input_type, expected_fields in client_inputs.items():
        signature = inspect.signature(input_type)
        assert tuple(signature.parameters) == expected_fields
        assert tuple(field.name for field in dataclasses.fields(input_type)) == expected_fields
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in signature.parameters.values()
        )
        hints = get_type_hints(
            input_type,
            globalns=client_namespace,
            localns=client_namespace,
        )
        if "headers" in expected_fields:
            assert hints["headers"] == Mapping[str, Payload]
    assert (
        inspect.signature(temporalio.client.StartWorkflowInput)
        .parameters["versioning_override"]
        .default
        is None
    )
    for name in ("callbacks", "links", "request_id"):
        assert (
            inspect.signature(temporalio.client.StartWorkflowUpdateInput).parameters[name].default
            is None
        )

    workflow_hooks = {
        "execute_workflow": (ExecuteWorkflowInput, Any),
        "handle_signal": (HandleSignalInput, type(None)),
        "handle_query": (HandleQueryInput, Any),
        "handle_update_validator": (HandleUpdateInput, type(None)),
        "handle_update_handler": (HandleUpdateInput, Any),
    }
    for name, (input_type, return_type) in workflow_hooks.items():
        sdk_method = getattr(SdkWorkflowInboundInterceptor, name)
        custom_method = getattr(module.TracingWorkflowInboundInterceptor, name)
        sdk_signature = inspect.signature(sdk_method)
        custom_signature = inspect.signature(custom_method)
        assert (
            tuple(sdk_signature.parameters)
            == tuple(custom_signature.parameters)
            == (
                "self",
                "input",
            )
        )
        assert [item.kind for item in sdk_signature.parameters.values()] == [
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ]
        sdk_hints = get_type_hints(sdk_method)
        custom_hints = get_type_hints(custom_method)
        assert sdk_hints["input"] is custom_hints["input"] is input_type
        assert sdk_hints["return"] is custom_hints["return"] is return_type

    worker_signature = inspect.signature(temporalio.worker.Worker)
    assert tuple(worker_signature.parameters) == (
        "client",
        "task_queue",
        "activities",
        "nexus_service_handlers",
        "workflows",
        "activity_executor",
        "workflow_task_executor",
        "nexus_task_executor",
        "workflow_runner",
        "unsandboxed_workflow_runner",
        "plugins",
        "interceptors",
        "build_id",
        "identity",
        "max_cached_workflows",
        "max_concurrent_workflow_tasks",
        "max_concurrent_activities",
        "max_concurrent_local_activities",
        "max_concurrent_nexus_tasks",
        "tuner",
        "max_concurrent_workflow_task_polls",
        "nonsticky_to_sticky_poll_ratio",
        "max_concurrent_activity_task_polls",
        "no_remote_activities",
        "sticky_queue_schedule_to_start_timeout",
        "max_heartbeat_throttle_interval",
        "default_heartbeat_throttle_interval",
        "max_activities_per_second",
        "max_task_queue_activities_per_second",
        "max_eager_activity_reservations_per_workflow_task",
        "graceful_shutdown_timeout",
        "workflow_failure_exception_types",
        "shared_state_manager",
        "debug_mode",
        "disable_eager_activity_execution",
        "on_fatal_error",
        "use_worker_versioning",
        "disable_safe_workflow_eviction",
        "deployment_config",
        "patch_activation_callback",
        "workflow_task_poller_behavior",
        "activity_task_poller_behavior",
        "nexus_task_poller_behavior",
        "disable_payload_error_limit",
        "max_workflow_task_external_storage_concurrency",
    )
    assert worker_signature.parameters["client"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in worker_signature.parameters.items()
        if name != "client"
    )
    assert worker_signature.parameters["activities"].default == []
    assert worker_signature.parameters["workflows"].default == []
    assert worker_signature.parameters["interceptors"].default == []
    worker_hints = get_type_hints(temporalio.worker.Worker.__init__)
    assert get_origin(worker_hints["activities"]) is not None
    assert get_origin(worker_hints["workflows"]) is not None
    assert get_origin(worker_hints["interceptors"]) is not None

    replayer_signature = inspect.signature(temporalio.worker.Replayer)
    assert tuple(replayer_signature.parameters) == (
        "workflows",
        "workflow_task_executor",
        "workflow_runner",
        "unsandboxed_workflow_runner",
        "namespace",
        "data_converter",
        "interceptors",
        "plugins",
        "build_id",
        "identity",
        "workflow_failure_exception_types",
        "debug_mode",
        "runtime",
        "disable_safe_workflow_eviction",
        "header_codec_behavior",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in replayer_signature.parameters.values()
    )
    assert replayer_signature.parameters["workflows"].default is inspect.Parameter.empty
    assert replayer_signature.parameters["interceptors"].default == []
    replayer_hints = get_type_hints(temporalio.worker.Replayer.__init__)
    assert get_origin(replayer_hints["workflows"]) is not None
    assert get_origin(replayer_hints["interceptors"]) is not None

    connect_signature = inspect.signature(temporalio.client.Client.connect).parameters
    assert connect_signature["interceptors"].kind is inspect.Parameter.KEYWORD_ONLY
    assert connect_signature["interceptors"].default == []
    connect_hints = get_type_hints(
        temporalio.client.Client.connect,
        globalns=client_namespace,
        localns=client_namespace,
    )
    assert get_origin(connect_hints["interceptors"]) is not None
    assert tuple(inspect.signature(module.SafeTemporalTracingInterceptor).parameters) == (
        "tracer",
        "role",
    )
    assert tuple(inspect.signature(module.TemporalActivityMetricsInterceptor).parameters) == (
        "metrics",
        "task_queue",
    )
    assert get_type_hints(module.build_temporal_worker)["return"] is temporalio.worker.Worker


def test_client_role_is_worker_noop_and_worker_role_registers_one_extern() -> None:
    module = _temporal()
    tracer = noop_tracer()
    client = module.SafeTemporalTracingInterceptor(tracer, role="client")
    worker = module.SafeTemporalTracingInterceptor(tracer, role="worker")
    client_externs: dict[str, Callable[..., object]] = {}
    worker_externs: dict[str, Callable[..., object]] = {}
    assert client.workflow_interceptor_class(WorkflowInterceptorClassInput(client_externs)) is None
    workflow_class = worker.workflow_interceptor_class(
        WorkflowInterceptorClassInput(worker_externs)
    )
    assert set(worker_externs) == {"__temporal_opentelemetry_completed_span"}
    assert workflow_class is module.TracingWorkflowInboundInterceptor
    terminal = _ActivityTerminal(result="ok")
    assert client.intercept_activity(cast(Any, terminal)) is terminal
    nexus_terminal = _NexusTerminal(result="ok")
    assert client.intercept_nexus_operation(cast(Any, nexus_terminal)) is nexus_terminal
    client_terminal = _ClientTerminal()
    assert worker.intercept_client(cast(Any, client_terminal)) is client_terminal

    constructed: list[str] = []

    class ClientWrapper(temporalio.client.OutboundInterceptor):
        def __init__(
            self,
            next: temporalio.client.OutboundInterceptor,
            label: str,
        ) -> None:
            super().__init__(next)
            self.label = label

    class ClientMarker(temporalio.client.Interceptor):
        def __init__(self, label: str) -> None:
            self.label = label

        def intercept_client(
            self, next: temporalio.client.OutboundInterceptor
        ) -> temporalio.client.OutboundInterceptor:
            constructed.append(self.label)
            return ClientWrapper(next, self.label)

    client_order = temporalio.client.Client(
        cast(Any, object()),
        interceptors=[ClientMarker("first"), ClientMarker("second")],
    )
    assert constructed == ["second", "first"]
    outer = cast(Any, client_order)._impl
    assert (outer.label, outer.next.label) == ("first", "second")

    runtime = SimpleNamespace(tracer=tracer, metrics=noop_metrics())
    client_worker_interceptor = module.temporal_client_interceptors(runtime)[0]
    worker_interceptors = module.temporal_worker_interceptors(
        runtime, task_queue="jhin-agent-queue"
    )
    assert [type(item) for item in worker_interceptors] == [
        module.SafeTemporalTracingInterceptor,
        module.TemporalActivityMetricsInterceptor,
    ]
    activity_chain: object = terminal
    for interceptor in reversed([client_worker_interceptor, *worker_interceptors]):
        activity_chain = interceptor.intercept_activity(cast(Any, activity_chain))
    assert isinstance(activity_chain, module._SafeActivityInboundInterceptor)
    assert isinstance(cast(Any, activity_chain).next, module._TemporalActivityMetricsInbound)
    assert cast(Any, activity_chain).next.next is terminal

    nexus_chain: object = nexus_terminal
    for interceptor in reversed([client_worker_interceptor, *worker_interceptors]):
        nexus_chain = interceptor.intercept_nexus_operation(cast(Any, nexus_chain))
    assert isinstance(nexus_chain, module._SafeNexusOperationInboundInterceptor)
    assert cast(Any, nexus_chain).next is nexus_terminal
    with pytest.raises(ValueError, match=r"^invalid Temporal interceptor role$"):
        module.SafeTemporalTracingInterceptor(tracer, role=cast(Any, "CLIENT"))
    with pytest.raises(ValueError, match=r"^invalid Temporal interceptor role$"):
        module.SafeTemporalTracingInterceptor(
            tracer, role=cast(Any, type("Role", (str,), {})("client"))
        )


def test_temporal_payload_carrier_boundary_and_immutability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _temporal()
    application_payload = Payload(metadata={"encoding": b"binary/plain"}, data=b"application")
    original = {
        "application": application_payload,
        "_tracer-data": _valid_payload(SECOND_TRACEPARENT),
    }
    context = _span_context({"traceparent": TRACEPARENT})
    encoded = module.encode_temporal_trace_headers(original, context=context)
    assert original["_tracer-data"] is not encoded["_tracer-data"]
    assert original["application"] is encoded["application"] is application_payload
    assert original["_tracer-data"] is not encoded["_tracer-data"]
    decoded_carrier, decoded_context = module.decode_temporal_trace_carrier(encoded)
    assert decoded_carrier == {"traceparent": TRACEPARENT}
    assert get_current_span(decoded_context).get_span_context().is_remote is True

    boundary = _json_payload_with_serialized_size(_valid_payload(), 1_024)
    oversized = _json_payload_with_serialized_size(_valid_payload(), 1_025)
    for payload in (boundary, oversized):
        wire = payload.SerializeToString()
        round_tripped = Payload()
        round_tripped.ParseFromString(wire)
        assert round_tripped.data == payload.data
        assert round_tripped.metadata == payload.metadata
        assert round_tripped.SerializeToString() == wire
    assert module.decode_temporal_trace_carrier({"_tracer-data": boundary})[0] == {
        "traceparent": TRACEPARENT
    }

    class ConverterSpy:
        calls = 0

        def from_payloads(self, _payloads: object) -> list[object]:
            self.calls += 1
            raise AssertionError("oversized carrier reached the payload converter")

    original_converter = module._PAYLOAD_CONVERTER
    converter_spy = ConverterSpy()
    monkeypatch.setattr(module, "_PAYLOAD_CONVERTER", converter_spy)
    assert module.decode_temporal_trace_carrier({"_tracer-data": oversized}) == (None, Context())
    assert converter_spy.calls == 0
    monkeypatch.setattr(module, "_PAYLOAD_CONVERTER", original_converter)

    malformed = (
        object(),
        Payload(metadata={"encoding": b"json/plain"}, data=b"not-json"),
        PayloadConverter.default.to_payloads([{"traceparent": TRACEPARENT, "baggage": "secret"}])[
            0
        ],
        PayloadConverter.default.to_payloads([{"traceparent": TRACEPARENT, "unknown": "secret"}])[
            0
        ],
        PayloadConverter.default.to_payloads([{"traceparent": [TRACEPARENT]}])[0],
        _valid_payload("invalid"),
    )
    for value in malformed:
        assert module.decode_temporal_trace_carrier({"_tracer-data": value}) == (None, Context())


@pytest.mark.asyncio
async def test_client_signal_interceptor_injects_valid_carrier_at_rpc_input() -> None:
    module = _temporal()
    application_payload = Payload(
        metadata={"encoding": b"binary/plain"}, data=b"private-business-value"
    )
    original_headers = {"application": application_payload}
    input = temporalio.client.SignalWorkflowInput(
        id="workflow-id",
        run_id=None,
        signal="trace_signal",
        args=("private-business-argument",),
        headers=original_headers,
        rpc_metadata={},
        rpc_timeout=None,
    )
    result = object()
    terminal = _ClientTerminal(result=result)

    with _recording_tracer() as (tracer, exporter):
        wrapped = module.SafeTemporalTracingInterceptor(tracer, role="client").intercept_client(
            cast(Any, terminal)
        )
        with tracer.start_as_current_span("api.signal") as api_parent:
            api_parent_context = api_parent.get_span_context()
            actual = await wrapped.signal_workflow(input)

        assert actual is result
        assert terminal.calls == [("signal_workflow", input)]
        assert original_headers == {"application": application_payload}
        assert input.headers is not original_headers
        assert input.headers["application"] is application_payload

        carrier, carrier_context = module.decode_temporal_trace_carrier(input.headers)
        assert carrier is not None
        assert set(carrier) <= {"traceparent", "tracestate"}
        assert "private-business" not in repr(carrier)
        spans = exporter.get_finished_spans()
        client_span = next(span for span in spans if span.name == "temporal.signal_workflow")
        assert client_span.parent is not None
        assert client_span.parent.trace_id == api_parent_context.trace_id
        assert client_span.parent.span_id == api_parent_context.span_id
        propagated = get_current_span(carrier_context).get_span_context()
        assert propagated.trace_id == client_span.context.trace_id
        assert propagated.span_id == client_span.context.span_id


def test_malformed_tracestate_never_reaches_otel_warning_logger(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _temporal()
    canary = "malformed-tracestate-payload-canary"
    payload = _valid_payload(tracestate=f"bad member={canary}")
    boundary = _valid_payload(tracestate=f"a={'x' * 256}")
    overlong_canary = "tracestate-member-overflow-private-canary"
    overlong = _valid_payload(tracestate=f"a={'x' * 256}{overlong_canary}")
    leading_ows_canary = "tracestate-leading-ows-private-canary"
    leading_ows = _valid_payload(tracestate=f" a={leading_ows_canary}")
    assert module.decode_temporal_trace_carrier({"_tracer-data": boundary})[0] == {
        "traceparent": TRACEPARENT,
        "tracestate": f"a={'x' * 256}",
    }
    with caplog.at_level(logging.WARNING):
        assert module.decode_temporal_trace_carrier({"_tracer-data": payload}) == (
            None,
            Context(),
        )
        assert module.decode_temporal_trace_carrier({"_tracer-data": overlong}) == (
            None,
            Context(),
        )
        assert module.decode_temporal_trace_carrier({"_tracer-data": leading_ows}) == (
            None,
            Context(),
        )
    captured = capsys.readouterr()
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert canary not in rendered + captured.out + captured.err
    assert overlong_canary not in rendered + captured.out + captured.err
    assert leading_ows_canary not in rendered + captured.out + captured.err


@pytest.mark.asyncio
async def test_nexus_uses_bounded_string_carrier_and_preserves_business_headers(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _temporal()
    business_value = "business-value-canary"
    original = {
        "Business-Key": business_value,
        "TraceParent": SECOND_TRACEPARENT,
        "baggage": "credential-canary",
        "_TRACER-DATA": "payload-canary",
    }
    tracestate = "a=" + "x" * 253 + ",b=" + "y" * 254
    assert len(tracestate.encode()) == 512
    context = _span_context({"traceparent": TRACEPARENT, "tracestate": tracestate})
    encoded = module.encode_nexus_trace_headers(original, context=context)
    assert encoded["Business-Key"] is original["Business-Key"]
    assert encoded["traceparent"] == TRACEPARENT
    assert encoded["tracestate"] == tracestate
    assert set(encoded).isdisjoint({"TraceParent", "baggage", "_TRACER-DATA"})
    assert module._nexus_trace_carrier_within_limit("x" * 1_011, "") is True
    assert module._nexus_trace_carrier_within_limit("x" * 1_012, "") is False
    assert original["TraceParent"] == SECOND_TRACEPARENT
    decoded = module.decode_nexus_trace_context(encoded)
    assert get_current_span(decoded).get_span_context().is_remote is True
    for bad in (
        {"TraceParent": TRACEPARENT},
        {"traceparent": TRACEPARENT, "baggage": "secret"},
        {"traceparent": TRACEPARENT, "tracestate": "z" * 513},
        {"traceparent": cast(Any, Payload())},
    ):
        assert module.decode_nexus_trace_context(bad) == Context()

    class OutboundTerminal:
        def __init__(self) -> None:
            self.calls: list[StartNexusOperationInput[Any, Any]] = []
            self.result = object()

        async def start_nexus_operation(self, input: StartNexusOperationInput[Any, Any]) -> object:
            self.calls.append(input)
            return self.result

    outbound_terminal = OutboundTerminal()
    workflow = object.__new__(module.TracingWorkflowInboundInterceptor)
    outbound = module._SafeWorkflowOutboundInterceptor(cast(Any, outbound_terminal), workflow)
    outbound_original = {
        "Business-Key": business_value,
        "traceparent": SECOND_TRACEPARENT,
    }
    business_input = object()
    outbound_input = StartNexusOperationInput(
        "endpoint-private-canary",
        "service-private-canary",
        "operation-private-canary",
        business_input,
        None,
        None,
        None,
        temporalio.workflow.NexusOperationCancellationType.WAIT_REQUESTED,
        outbound_original,
        None,
    )

    with _recording_tracer() as (tracer, exporter):
        with tracer.start_as_current_span("nexus-outbound-parent") as parent:
            assert await outbound.start_nexus_operation(outbound_input) is outbound_terminal.result
            parent_context = parent.get_span_context()
        assert outbound_terminal.calls == [outbound_input]
        assert outbound_input.input is business_input
        assert outbound_input.headers is not outbound_original
        assert outbound_original == {
            "Business-Key": business_value,
            "traceparent": SECOND_TRACEPARENT,
        }
        assert outbound_input.headers is not None
        assert outbound_input.headers["Business-Key"] is business_value
        outbound_context = module.decode_nexus_trace_context(outbound_input.headers)
        assert (
            get_current_span(outbound_context).get_span_context().span_id == parent_context.span_id
        )

        inbound_terminal = _NexusTerminal(result="nexus-result")
        inbound = module.SafeTemporalTracingInterceptor(
            tracer, role="worker"
        ).intercept_nexus_operation(cast(Any, inbound_terminal))
        original_start_headers = {
            "Business-Key": business_value,
            "traceparent": TRACEPARENT,
        }
        original_cancel_headers = dict(original_start_headers)
        original_start_ctx = _start_context(original_start_headers)
        original_cancel_ctx = _cancel_context(original_cancel_headers)
        start_input = ExecuteNexusOperationStartInput(original_start_ctx, business_input)
        cancel_input = ExecuteNexusOperationCancelInput(original_cancel_ctx, "cancel-token")
        assert await inbound.execute_nexus_operation_start(start_input) == "nexus-result"
        await inbound.execute_nexus_operation_cancel(cancel_input)
        assert inbound_terminal.start_inputs == [start_input]
        assert inbound_terminal.cancel_inputs == [cancel_input]
        assert start_input.ctx is not original_start_ctx
        assert cancel_input.ctx is not original_cancel_ctx
        assert original_start_ctx.headers is original_start_headers
        assert original_cancel_ctx.headers is original_cancel_headers
        assert original_start_headers["traceparent"] == TRACEPARENT
        assert original_cancel_headers["traceparent"] == TRACEPARENT
        assert start_input.ctx.headers["Business-Key"] is business_value
        assert cancel_input.ctx.headers["Business-Key"] is business_value

        server_spans = [
            span for span in exporter.get_finished_spans() if span.name == "sandbox.server"
        ]
        assert len(server_spans) == 2
        incoming_parent_id = int(TRACEPARENT.split("-")[2], 16)
        for wrapped_input, span in zip((start_input, cancel_input), server_spans, strict=True):
            downstream_context = module.decode_nexus_trace_context(wrapped_input.ctx.headers)
            downstream_span = get_current_span(downstream_context).get_span_context()
            assert downstream_span.trace_id == span.context.trace_id
            assert downstream_span.span_id == span.context.span_id
            assert span.parent is not None and span.parent.span_id == incoming_parent_id
            assert span.kind is SpanKind.SERVER

        malformed_canary = "nexus-tracestate-overflow-private-canary"
        malformed_headers = {
            "Business-Key": business_value,
            "traceparent": TRACEPARENT,
            "tracestate": f"a={'x' * 256}{malformed_canary}",
        }
        malformed_start_ctx = _start_context(malformed_headers)
        malformed_cancel_ctx = _cancel_context(malformed_headers)
        malformed_start = ExecuteNexusOperationStartInput(malformed_start_ctx, business_input)
        malformed_cancel = ExecuteNexusOperationCancelInput(malformed_cancel_ctx, "malformed-token")
        with caplog.at_level(logging.WARNING):
            assert await inbound.execute_nexus_operation_start(malformed_start) == "nexus-result"
            await inbound.execute_nexus_operation_cancel(malformed_cancel)
        malformed_spans = [
            span for span in exporter.get_finished_spans() if span.name == "sandbox.server"
        ][-2:]
        assert all(span.parent is None for span in malformed_spans)
        assert malformed_start.ctx is not malformed_start_ctx
        assert malformed_cancel.ctx is not malformed_cancel_ctx
        assert malformed_start.ctx.headers["Business-Key"] is business_value
        assert malformed_cancel.ctx.headers["Business-Key"] is business_value
        assert "tracestate" not in malformed_start.ctx.headers
        assert "tracestate" not in malformed_cancel.ctx.headers

    captured = capsys.readouterr()
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    span_rendered = repr(exporter.get_finished_spans())
    assert malformed_canary not in rendered + captured.out + captured.err + span_rendered


@pytest.mark.asyncio
async def test_update_with_start_sanitizes_both_header_maps_without_current_span() -> None:
    module = _temporal()
    stale_start = _valid_payload(TRACEPARENT)
    stale_update = _valid_payload(SECOND_TRACEPARENT)
    terminal = _ClientTerminal(result="result")
    wrapped = module.SafeTemporalTracingInterceptor(noop_tracer(), role="client").intercept_client(
        cast(Any, terminal)
    )
    input = _client_input("start_update_with_start_workflow")
    input.start_workflow_input.headers = {"_tracer-data": stale_start}
    input.update_workflow_input.headers = {"_tracer-data": stale_update}
    assert await wrapped.start_update_with_start_workflow(input) == "result"
    assert terminal.calls == [("start_update_with_start_workflow", input)]
    assert "_tracer-data" not in input.start_workflow_input.headers
    assert "_tracer-data" not in input.update_workflow_input.headers
    assert stale_start is not stale_update


@pytest.mark.asyncio
async def test_update_with_start_forwards_one_new_payload_to_both_halves() -> None:
    module = _temporal()
    terminal = _ClientTerminal(result="result")
    with _recording_tracer() as (tracer, _exporter):
        wrapped = module.SafeTemporalTracingInterceptor(tracer, role="client").intercept_client(
            cast(Any, terminal)
        )
        input = _client_input("start_update_with_start_workflow")
        with tracer.start_as_current_span("parent"):
            assert await wrapped.start_update_with_start_workflow(input) == "result"
    start_payload = input.start_workflow_input.headers["_tracer-data"]
    update_payload = input.update_workflow_input.headers["_tracer-data"]
    assert start_payload is update_payload
    decoded, _ = module.decode_temporal_trace_carrier({"_tracer-data": start_payload})
    assert decoded is not None and set(decoded) <= {"traceparent", "tracestate"}


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_kind", ["tracestate", "oversized"])
async def test_signal_and_update_activity_parent_the_incoming_context_not_start_context(
    malformed_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _temporal()
    workflow_start_context = _span_context({"traceparent": TRACEPARENT})
    signal_context = _span_context({"traceparent": SECOND_TRACEPARENT})
    update_context = _span_context(
        {"traceparent": "00-33333333333333333333333333333333-4444444444444444-01"}
    )
    signal_headers = module.encode_temporal_trace_headers({}, context=signal_context)
    update_headers = module.encode_temporal_trace_headers({}, context=update_context)
    update_result = object()
    query_result = object()

    class OutboundTerminal:
        def __init__(self) -> None:
            self.inputs: list[StartActivityInput] = []

        def start_activity(self, input: StartActivityInput) -> Any:
            self.inputs.append(input)
            return cast(Any, object())

    class WorkflowTerminal:
        def __init__(self) -> None:
            self.outbound: Any = None
            self.seen: list[tuple[str, Any]] = []
            self.calls: dict[str, int] = {}

        def init(self, outbound: object) -> None:
            self.outbound = outbound

        def _record(self, label: str) -> None:
            self.calls[label] = self.calls.get(label, 0) + 1
            self.seen.append((label, get_current_span().get_span_context()))

        async def handle_signal(self, input: HandleSignalInput) -> None:
            label = cast(str, input.args[0])
            self._record(label)
            self.outbound.start_activity(_start_activity_input(label))

        async def handle_update_handler(self, input: HandleUpdateInput) -> object:
            label = cast(str, input.args[0])
            self._record(label)
            self.outbound.start_activity(_start_activity_input(label))
            return update_result

        def handle_update_validator(self, _input: HandleUpdateInput) -> None:
            self._record("validator")

        async def handle_query(self, _input: HandleQueryInput) -> object:
            self._record("query")
            return query_result

    outbound_terminal = OutboundTerminal()
    terminal = WorkflowTerminal()
    workflow = object.__new__(module.TracingWorkflowInboundInterceptor)
    temporalio.worker.WorkflowInboundInterceptor.__init__(workflow, cast(Any, terminal))
    workflow.header_key = "_tracer-data"
    workflow.text_map_propagator = TraceContextTextMapPropagator()
    workflow.payload_converter = PayloadConverter.default
    workflow._workflow_context_carrier = None
    workflow._extern_functions = {"__temporal_opentelemetry_completed_span": lambda _params: {}}
    workflow.init(cast(Any, outbound_terminal))

    ambient = get_current_span(workflow_start_context).get_span_context()
    token = module.otel_context.attach(workflow_start_context)
    try:
        await workflow.handle_signal(
            HandleSignalInput("trace_signal", ("valid-signal",), signal_headers)
        )
        assert get_current_span().get_span_context() == ambient
        assert (
            await workflow.handle_update_handler(
                HandleUpdateInput("update-id", "trace_update", ("valid-update",), update_headers)
            )
            is update_result
        )
        assert get_current_span().get_span_context() == ambient
        workflow.handle_update_validator(
            HandleUpdateInput("update-id", "trace_update", (), update_headers)
        )
        assert get_current_span().get_span_context() == ambient
        assert (
            await workflow.handle_query(HandleQueryInput("query-id", "status", (), signal_headers))
            is query_result
        )
        assert get_current_span().get_span_context() == ambient
    finally:
        module.otel_context.detach(token)

    expected_contexts = {
        "valid-signal": get_current_span(signal_context).get_span_context(),
        "valid-update": get_current_span(update_context).get_span_context(),
        "validator": get_current_span(update_context).get_span_context(),
        "query": get_current_span(signal_context).get_span_context(),
    }
    assert [label for label, _context in terminal.seen] == [
        "valid-signal",
        "valid-update",
        "validator",
        "query",
    ]
    for label, context in terminal.seen:
        expected = expected_contexts[label]
        assert context.trace_id == expected.trace_id
        assert context.span_id == expected.span_id

    assert [cast(str, input.args[0]) for input in outbound_terminal.inputs] == [
        "valid-signal",
        "valid-update",
    ]
    for input, expected in zip(
        outbound_terminal.inputs,
        (expected_contexts["valid-signal"], expected_contexts["valid-update"]),
        strict=True,
    ):
        carrier, context = module.decode_temporal_trace_carrier(input.headers)
        assert carrier is not None
        propagated = get_current_span(context).get_span_context()
        assert propagated.trace_id == expected.trace_id
        assert propagated.span_id == expected.span_id

    activity_results = {
        "valid-signal": object(),
        "valid-update": object(),
        "malformed-signal": object(),
        "malformed-update": object(),
    }

    class ActivityTerminal:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.seen: dict[str, Any] = {}

        async def execute_activity(self, input: ExecuteActivityInput) -> object:
            label = cast(str, input.args[0])
            self.calls.append(label)
            self.seen[label] = get_current_span().get_span_context()
            return activity_results[label]

    monkeypatch.setattr(
        temporalio.activity,
        "info",
        lambda: SimpleNamespace(
            activity_type="reason_agent_step",
            workflow_id="workflow-id",
            workflow_run_id="run-id",
        ),
    )
    activity_terminal = ActivityTerminal()
    with _recording_tracer() as (tracer, exporter):
        activity_wrapper = module.SafeTemporalTracingInterceptor(
            tracer, role="worker"
        ).intercept_activity(cast(Any, activity_terminal))
        for input in outbound_terminal.inputs:
            label = cast(str, input.args[0])
            result = await activity_wrapper.execute_activity(
                ExecuteActivityInput(lambda: None, input.args, None, input.headers)
            )
            assert result is activity_results[label]

        valid_spans = exporter.get_finished_spans()
        assert len(valid_spans) == 2
        for label, expected in (
            ("valid-signal", expected_contexts["valid-signal"]),
            ("valid-update", expected_contexts["valid-update"]),
        ):
            activity_span = next(
                span
                for span in valid_spans
                if span.context.span_id == activity_terminal.seen[label].span_id
            )
            assert activity_span.name == "temporal.activity.reason_agent_step"
            assert activity_span.parent is not None
            assert activity_span.context.trace_id == expected.trace_id
            assert activity_span.parent.trace_id == expected.trace_id
            assert activity_span.parent.span_id == expected.span_id

        malformed_canary = f"direct-{malformed_kind}-private-canary"
        if malformed_kind == "tracestate":
            malformed_payload = _valid_payload(tracestate=f"a={'x' * 256}{malformed_canary}")
        else:
            malformed_payload = _json_payload_with_serialized_size(
                _valid_payload(tracestate=f"a={malformed_canary}"), 1_025
            )
        malformed_headers = {"_tracer-data": malformed_payload}

        before_malformed = len(outbound_terminal.inputs)
        token = module.otel_context.attach(workflow_start_context)
        try:
            with caplog.at_level(logging.WARNING):
                await workflow.handle_signal(
                    HandleSignalInput("trace_signal", ("malformed-signal",), malformed_headers)
                )
                assert get_current_span().get_span_context() == ambient
                assert (
                    await workflow.handle_update_handler(
                        HandleUpdateInput(
                            "update-id",
                            "trace_update",
                            ("malformed-update",),
                            malformed_headers,
                        )
                    )
                    is update_result
                )
                assert get_current_span().get_span_context() == ambient
        finally:
            module.otel_context.detach(token)

        malformed_inputs = outbound_terminal.inputs[before_malformed:]
        assert [cast(str, input.args[0]) for input in malformed_inputs] == [
            "malformed-signal",
            "malformed-update",
        ]
        assert terminal.calls["malformed-signal"] == 1
        assert terminal.calls["malformed-update"] == 1
        for label, context in terminal.seen[-2:]:
            assert label.startswith("malformed-")
            assert context.is_valid is False

        for input in malformed_inputs:
            assert "_tracer-data" not in input.headers
            label = cast(str, input.args[0])
            result = await activity_wrapper.execute_activity(
                ExecuteActivityInput(lambda: None, input.args, None, input.headers)
            )
            assert result is activity_results[label]

        all_spans = exporter.get_finished_spans()
        malformed_spans = all_spans[-2:]
        assert len(malformed_spans) == 2
        assert all(span.name == "temporal.activity.reason_agent_step" for span in malformed_spans)
        assert all(span.parent is None for span in malformed_spans)
        assert activity_terminal.calls == [
            "valid-signal",
            "valid-update",
            "malformed-signal",
            "malformed-update",
        ]

        captured = capsys.readouterr()
        rendered = "\n".join(record.getMessage() for record in caplog.records)
        assert malformed_canary not in (rendered + captured.out + captured.err + repr(all_spans))


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_kind", ["tracestate", "oversized"])
async def test_malformed_workflow_start_hook_roots_without_logging(
    malformed_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _temporal()
    workflow_result = object()
    activity_result = object()

    class OutboundTerminal:
        def __init__(self) -> None:
            self.inputs: list[StartActivityInput] = []

        def start_activity(self, input: StartActivityInput) -> Any:
            self.inputs.append(input)
            return cast(Any, object())

    class WorkflowTerminal:
        def __init__(self) -> None:
            self.calls = 0
            self.seen: Any = None
            self.outbound: Any = None

        def init(self, outbound: object) -> None:
            self.outbound = outbound

        async def execute_workflow(self, _input: ExecuteWorkflowInput) -> object:
            self.calls += 1
            self.seen = get_current_span().get_span_context()
            self.outbound.start_activity(_start_activity_input("malformed-start"))
            return workflow_result

    class ActivityTerminal:
        def __init__(self) -> None:
            self.calls = 0
            self.seen: Any = None

        async def execute_activity(self, _input: ExecuteActivityInput) -> object:
            self.calls += 1
            self.seen = get_current_span().get_span_context()
            return activity_result

    outbound_terminal = OutboundTerminal()
    workflow_terminal = WorkflowTerminal()
    workflow = object.__new__(module.TracingWorkflowInboundInterceptor)
    temporalio.worker.WorkflowInboundInterceptor.__init__(workflow, cast(Any, workflow_terminal))
    workflow.header_key = "_tracer-data"
    workflow.text_map_propagator = TraceContextTextMapPropagator()
    workflow.payload_converter = PayloadConverter.default
    workflow._workflow_context_carrier = None
    workflow._extern_functions = {"__temporal_opentelemetry_completed_span": lambda _params: {}}
    workflow.init(cast(Any, outbound_terminal))

    malformed_canary = f"workflow-start-{malformed_kind}-private-canary"
    if malformed_kind == "tracestate":
        malformed_payload = _valid_payload(tracestate=f"a={'x' * 256}{malformed_canary}")
    else:
        malformed_payload = _json_payload_with_serialized_size(
            _valid_payload(tracestate=f"a={malformed_canary}"), 1_025
        )
    workflow_input = ExecuteWorkflowInput(
        type("Workflow", (), {}),
        cast(Any, None),
        (),
        {"_tracer-data": malformed_payload},
    )
    ambient_context = _span_context({"traceparent": TRACEPARENT})
    ambient = get_current_span(ambient_context).get_span_context()

    activity_terminal = ActivityTerminal()
    monkeypatch.setattr(
        temporalio.activity,
        "info",
        lambda: SimpleNamespace(
            activity_type="reason_agent_step",
            workflow_id="workflow-id",
            workflow_run_id="run-id",
        ),
    )
    with _recording_tracer() as (tracer, exporter):
        activity_wrapper = module.SafeTemporalTracingInterceptor(
            tracer, role="worker"
        ).intercept_activity(cast(Any, activity_terminal))
        token = module.otel_context.attach(ambient_context)
        try:
            with caplog.at_level(logging.WARNING):
                assert await workflow.execute_workflow(workflow_input) is workflow_result
            assert get_current_span().get_span_context() == ambient
        finally:
            module.otel_context.detach(token)

        assert workflow_terminal.calls == 1
        assert workflow_terminal.seen.is_valid is False
        assert len(outbound_terminal.inputs) == 1
        activity_input = outbound_terminal.inputs[0]
        assert "_tracer-data" not in activity_input.headers
        with caplog.at_level(logging.WARNING):
            assert (
                await activity_wrapper.execute_activity(
                    ExecuteActivityInput(
                        lambda: None,
                        activity_input.args,
                        None,
                        activity_input.headers,
                    )
                )
                is activity_result
            )
        assert activity_terminal.calls == 1
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "temporal.activity.reason_agent_step"
        assert spans[0].parent is None
        assert activity_terminal.seen.span_id == spans[0].context.span_id
        assert all(
            not span.name.startswith(("RunWorkflow:", "CompleteWorkflow:")) for span in spans
        )

        captured = capsys.readouterr()
        rendered = "\n".join(record.getMessage() for record in caplog.records)
        assert malformed_canary not in (rendered + captured.out + captured.err + repr(spans))


@pytest.mark.asyncio
async def test_time_skipping_start_update_parentage_signal_completion_and_replay() -> None:
    module = _temporal()
    activity_contexts: dict[str, Any] = {}
    activity_events = {
        label: asyncio.Event() for label in ("valid-start", "valid-signal", "valid-update")
    }

    @temporalio.activity.defn(name="reason_agent_step")
    async def capture_activity(label: str) -> str:
        activity_contexts[label] = get_current_span().get_span_context()
        activity_events[label].set()
        return label

    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    environment: WorkflowEnvironment | None = None
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
        with _recording_tracer() as (tracer, exporter):
            metrics = metrics_module.build_jhin_metrics(
                meter_provider.get_meter("task-6-temporal-test")
            )
            runtime = SimpleNamespace(tracer=tracer, metrics=metrics)
            traced_config = environment.client.config()
            traced_config["interceptors"] = module.temporal_client_interceptors(runtime)
            traced_client = temporalio.client.Client(**traced_config)
            worker_interceptors = module.temporal_worker_interceptors(
                runtime, task_queue="jhin-agent-queue"
            )

            parent_contexts: dict[str, Any] = {}
            async with temporalio.worker.Worker(
                environment.client,
                task_queue="task-6-telemetry-queue",
                workflows=[_Task6TelemetryGraphWorkflow],
                activities=[capture_activity],
                interceptors=worker_interceptors,
            ):
                with tracer.start_as_current_span("api.valid-start") as parent:
                    parent_contexts["valid-start"] = parent.get_span_context()
                    valid_handle = await traced_client.start_workflow(
                        _Task6TelemetryGraphWorkflow.run,
                        "valid",
                        id="task-6-valid-trace-graph",
                        task_queue="task-6-telemetry-queue",
                    )
                await asyncio.wait_for(activity_events["valid-start"].wait(), timeout=10)

                with tracer.start_as_current_span("api.valid-signal"):
                    await valid_handle.signal("trace_signal", "valid-signal")
                await asyncio.wait_for(activity_events["valid-signal"].wait(), timeout=10)

                with tracer.start_as_current_span("api.valid-update") as parent:
                    parent_contexts["valid-update"] = parent.get_span_context()
                    assert (
                        await valid_handle.execute_update("trace_update", "valid-update")
                        == "valid-update"
                    )
                await asyncio.wait_for(activity_events["valid-update"].wait(), timeout=10)
                assert await valid_handle.result() == 3
                assert all(event.is_set() for event in activity_events.values())
                valid_history = await valid_handle.fetch_history()
                signal_event_type = int(EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED)
                signal_events = [
                    event
                    for event in valid_history.events
                    if int(event.event_type) == signal_event_type
                ]
                assert len(signal_events) == 1

            finished = exporter.get_finished_spans()
            assert len(finished) == 9
            for label, expected_client_name in (
                ("valid-start", "temporal.start_workflow"),
                ("valid-update", "temporal.client.other"),
            ):
                parent = next(span for span in finished if span.name == f"api.{label}")
                client_span = next(
                    span
                    for span in finished
                    if span.name == expected_client_name
                    and span.context.trace_id == parent_contexts[label].trace_id
                )
                activity_span = next(
                    span
                    for span in finished
                    if span.context.span_id == activity_contexts[label].span_id
                )
                assert parent.context.trace_id == parent_contexts[label].trace_id
                assert parent.context.span_id == parent_contexts[label].span_id
                assert client_span.parent is not None
                assert client_span.parent.trace_id == parent.context.trace_id
                assert client_span.parent.span_id == parent.context.span_id
                assert activity_span.name == "temporal.activity.reason_agent_step"
                assert activity_span.parent is not None
                assert activity_span.context.trace_id == client_span.context.trace_id
                assert activity_span.parent.trace_id == client_span.context.trace_id
                assert activity_span.parent.span_id == client_span.context.span_id

            assert all(
                not span.name.startswith(("RunWorkflow:", "CompleteWorkflow:")) for span in finished
            )
            before_replay = len(exporter.get_finished_spans())
            await temporalio.worker.Replayer(
                workflows=[_Task6TelemetryGraphWorkflow],
                interceptors=module.temporal_worker_interceptors(
                    runtime, task_queue="jhin-agent-queue"
                ),
            ).replay_workflow(valid_history)
            assert len(exporter.get_finished_spans()) == before_replay
    finally:
        try:
            if environment is not None:
                await environment.shutdown()
        finally:
            meter_provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation", [asyncio.CancelledError(), TemporalCancelledError()])
async def test_temporal_and_asyncio_cancellation_are_never_failure_metrics(
    cancellation: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _temporal()
    monkeypatch.setattr(
        module.activity,
        "info",
        lambda: SimpleNamespace(
            activity_type="reason_agent_step",
            workflow_id="workflow-id",
            workflow_run_id="run-id",
        ),
    )
    additions: list[tuple[int, dict[str, str]]] = []
    metrics = JhinMetrics(
        lambda _name: SimpleNamespace(
            add=lambda amount, **labels: additions.append((amount, labels))
        ),
        lambda _name: cast(Any, None),
        lambda _name, _values: None,
    )
    terminal = _ActivityTerminal(failure=cancellation)
    metrics_wrapped = module.TemporalActivityMetricsInterceptor(
        metrics, task_queue="jhin-tool-queue"
    ).intercept_activity(cast(Any, terminal))
    with _recording_tracer() as (tracer, exporter):
        wrapped = module.SafeTemporalTracingInterceptor(tracer, role="worker").intercept_activity(
            cast(Any, metrics_wrapped)
        )
        caught: BaseException | None = None
        try:
            await wrapped.execute_activity(ExecuteActivityInput(lambda: None, (), None, {}))
        except BaseException as exc:
            caught = exc
        spans = exporter.get_finished_spans()
    assert caught is cancellation
    assert _traceback_tail(caught.__traceback__) is terminal.failure_traceback
    assert terminal.calls == 1
    assert additions == []
    assert len(spans) == 1
    cancellation_span = spans[0]
    assert cancellation_span.status.status_code is StatusCode.UNSET
    assert cancellation_span.events == ()
    assert not any(key.startswith("error.") for key in dict(cancellation_span.attributes or {}))

    ordinary_failure = RuntimeError("ordinary-activity-failure-private-canary")
    ordinary_terminal = _ActivityTerminal(failure=ordinary_failure)
    ordinary_metrics = module.TemporalActivityMetricsInterceptor(
        metrics, task_queue="jhin-tool-queue"
    ).intercept_activity(cast(Any, ordinary_terminal))
    with _recording_tracer() as (tracer, _exporter):
        ordinary_wrapped = module.SafeTemporalTracingInterceptor(
            tracer, role="worker"
        ).intercept_activity(cast(Any, ordinary_metrics))
        for _attempt in range(2):
            with pytest.raises(RuntimeError) as raised:
                await ordinary_wrapped.execute_activity(
                    ExecuteActivityInput(lambda: None, (), None, {})
                )
            assert raised.value is ordinary_failure
    assert ordinary_terminal.calls == 2
    assert additions == [
        (
            1,
            {
                "task_queue": "jhin-tool-queue",
                "activity": "reason_agent_step",
                "failure_class": "internal",
            },
        ),
        (
            1,
            {
                "task_queue": "jhin-tool-queue",
                "activity": "reason_agent_step",
                "failure_class": "internal",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_each_public_wrapper_calls_downstream_once_under_hostile_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _temporal()

    class HostileTracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("telemetry-setup-canary")

    root = module.SafeTemporalTracingInterceptor(cast(Any, HostileTracer()), role="client")
    terminal = _ClientTerminal(result=object())
    wrapped = root.intercept_client(cast(Any, terminal))
    operations = (
        "start_workflow",
        "query_workflow",
        "signal_workflow",
        "start_workflow_update",
        "start_update_with_start_workflow",
        "start_activity",
    )
    for operation in operations:
        result = await getattr(wrapped, operation)(_client_input(operation))
        assert result is terminal.result
    assert [operation for operation, _input in terminal.calls] == list(operations)

    class HostileInput:
        def __getattribute__(self, _name: str) -> object:
            raise RuntimeError("hostile-pre-call-property-canary")

    hostile_terminal = _ClientTerminal(result="hostile-result")
    hostile_wrapped = root.intercept_client(cast(Any, hostile_terminal))
    hostile_inputs: list[object] = []
    for operation in operations:
        hostile_input = HostileInput()
        hostile_inputs.append(hostile_input)
        assert await getattr(hostile_wrapped, operation)(hostile_input) == "hostile-result"
    assert [item for _operation, item in hostile_terminal.calls] == hostile_inputs

    worker_root = module.SafeTemporalTracingInterceptor(cast(Any, HostileTracer()), role="worker")
    monkeypatch.setattr(
        module.activity, "info", lambda: (_ for _ in ()).throw(RuntimeError("info"))
    )
    activity_terminal = _ActivityTerminal(result="activity")
    activity_wrapper = worker_root.intercept_activity(cast(Any, activity_terminal))
    assert (
        await activity_wrapper.execute_activity(ExecuteActivityInput(lambda: None, (), None, {}))
        == "activity"
    )
    assert activity_terminal.calls == 1
    hostile_activity_terminal = _ActivityTerminal(result="hostile-activity")
    hostile_activity = worker_root.intercept_activity(cast(Any, hostile_activity_terminal))
    assert await hostile_activity.execute_activity(cast(Any, HostileInput())) == "hostile-activity"
    assert hostile_activity_terminal.calls == 1

    nexus_terminal = _NexusTerminal(result="nexus")
    nexus_wrapper = worker_root.intercept_nexus_operation(cast(Any, nexus_terminal))
    start = ExecuteNexusOperationStartInput(_start_context({"traceparent": TRACEPARENT}), None)
    cancel = ExecuteNexusOperationCancelInput(
        _cancel_context({"traceparent": TRACEPARENT}), "token"
    )
    assert await nexus_wrapper.execute_nexus_operation_start(start) == "nexus"
    await nexus_wrapper.execute_nexus_operation_cancel(cancel)
    assert (nexus_terminal.start_calls, nexus_terminal.cancel_calls) == (1, 1)
    hostile_nexus_terminal = _NexusTerminal(result="hostile-nexus")
    hostile_nexus = worker_root.intercept_nexus_operation(cast(Any, hostile_nexus_terminal))
    assert await hostile_nexus.execute_nexus_operation_start(cast(Any, HostileInput())) == (
        "hostile-nexus"
    )
    await hostile_nexus.execute_nexus_operation_cancel(cast(Any, HostileInput()))
    assert (hostile_nexus_terminal.start_calls, hostile_nexus_terminal.cancel_calls) == (1, 1)

    metric_business_error = RuntimeError("metric-business-private-canary")

    def raising_counter(_name: str) -> object:
        raise RuntimeError("metric-counter-telemetry-canary")

    def raising_add(_amount: int, **_labels: str) -> None:
        raise RuntimeError("metric-add-telemetry-canary")

    hostile_metric_sets = (
        JhinMetrics(
            raising_counter,
            lambda _name: cast(Any, None),
            lambda _name, _values: None,
        ),
        JhinMetrics(
            lambda _name: SimpleNamespace(add=raising_add),
            lambda _name: cast(Any, None),
            lambda _name, _values: None,
        ),
    )
    monkeypatch.setattr(
        module.activity,
        "info",
        lambda: SimpleNamespace(
            activity_type="reason_agent_step",
            workflow_id="workflow-id",
            workflow_run_id="run-id",
        ),
    )
    for hostile_metrics in hostile_metric_sets:
        metric_terminal = _ActivityTerminal(failure=metric_business_error)
        metric_wrapper = module.TemporalActivityMetricsInterceptor(
            hostile_metrics, task_queue="jhin-agent-queue"
        ).intercept_activity(cast(Any, metric_terminal))
        with pytest.raises(RuntimeError) as raised:
            await metric_wrapper.execute_activity(ExecuteActivityInput(lambda: None, (), None, {}))
        assert raised.value is metric_business_error
        assert metric_terminal.calls == 1

    classifier_metrics = JhinMetrics(
        lambda _name: SimpleNamespace(add=lambda _amount, **_labels: None),
        lambda _name: cast(Any, None),
        lambda _name, _values: None,
    )
    with monkeypatch.context() as classifier_patch:
        classifier_patch.setattr(
            module,
            "_failure_class",
            lambda _error: (_ for _ in ()).throw(
                RuntimeError("metric-classifier-telemetry-canary")
            ),
        )
        metric_terminal = _ActivityTerminal(failure=metric_business_error)
        metric_wrapper = module.TemporalActivityMetricsInterceptor(
            classifier_metrics, task_queue="jhin-agent-queue"
        ).intercept_activity(cast(Any, metric_terminal))
        with pytest.raises(RuntimeError) as raised:
            await metric_wrapper.execute_activity(ExecuteActivityInput(lambda: None, (), None, {}))
        assert raised.value is metric_business_error
        assert metric_terminal.calls == 1

    malformed_hook_canary = "actual-hook-tracestate-private-canary"
    malformed_payload = _valid_payload(tracestate=f"a={'x' * 256}{malformed_hook_canary}")
    client_terminal = _ClientTerminal(result="workflow-start-result")
    with _recording_tracer() as (tracer, client_exporter):
        client_wrapper = module.SafeTemporalTracingInterceptor(
            tracer, role="client"
        ).intercept_client(cast(Any, client_terminal))
        start_input = _client_input("start_workflow", {"_tracer-data": malformed_payload})
        with caplog.at_level(logging.WARNING):
            assert await client_wrapper.start_workflow(start_input) == "workflow-start-result"
        assert client_terminal.calls == [("start_workflow", start_input)]
        client_spans = client_exporter.get_finished_spans()
        assert len(client_spans) == 1 and client_spans[0].parent is None
        downstream_client_context = module.decode_temporal_trace_carrier(start_input.headers)[1]
        assert (
            get_current_span(downstream_client_context).get_span_context().span_id
            == client_spans[0].context.span_id
        )

    class ContextActivityTerminal:
        def __init__(self) -> None:
            self.calls = 0
            self.seen = None

        async def execute_activity(self, _input: object) -> str:
            self.calls += 1
            self.seen = get_current_span().get_span_context()
            return "activity-result"

    context_activity_terminal = ContextActivityTerminal()
    with _recording_tracer() as (tracer, activity_exporter):
        activity_wrapper = module.SafeTemporalTracingInterceptor(
            tracer, role="worker"
        ).intercept_activity(cast(Any, context_activity_terminal))
        with caplog.at_level(logging.WARNING):
            assert (
                await activity_wrapper.execute_activity(
                    ExecuteActivityInput(
                        lambda: None,
                        (),
                        None,
                        {"_tracer-data": malformed_payload},
                    )
                )
                == "activity-result"
            )
        activity_spans = activity_exporter.get_finished_spans()
        assert context_activity_terminal.calls == 1
        assert len(activity_spans) == 1 and activity_spans[0].parent is None
        assert context_activity_terminal.seen.span_id == activity_spans[0].context.span_id

    class WorkflowContextTerminal:
        def __init__(self) -> None:
            self.calls = 0
            self.seen = None

        async def execute_workflow(self, _input: object) -> str:
            self.calls += 1
            self.seen = get_current_span().get_span_context()
            return "workflow-result"

    workflow_terminal = WorkflowContextTerminal()
    workflow_wrapper = object.__new__(module.TracingWorkflowInboundInterceptor)
    temporalio.worker.WorkflowInboundInterceptor.__init__(
        workflow_wrapper, cast(Any, workflow_terminal)
    )
    workflow_wrapper.header_key = "_tracer-data"
    workflow_wrapper.text_map_propagator = TraceContextTextMapPropagator()
    workflow_wrapper.payload_converter = PayloadConverter.default
    workflow_wrapper._workflow_context_carrier = None
    workflow_wrapper._extern_functions = {
        "__temporal_opentelemetry_completed_span": lambda _params: {}
    }
    workflow_input = ExecuteWorkflowInput(
        type("Workflow", (), {}),
        cast(Any, None),
        (),
        {"_tracer-data": malformed_payload},
    )
    with caplog.at_level(logging.WARNING):
        assert await workflow_wrapper.execute_workflow(workflow_input) == "workflow-result"
    assert workflow_terminal.calls == 1
    assert workflow_terminal.seen.is_valid is False

    captured = capsys.readouterr()
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    spans_rendered = repr(client_spans) + repr(activity_spans)
    assert malformed_hook_canary not in rendered + captured.out + captured.err + spans_rendered


@pytest.mark.asyncio
async def test_hostile_span_lifecycle_restores_invocation_context_and_business_authority() -> None:
    for failure_seam, business_fails in [
        ("manager-enter", False),
        ("manager-enter", True),
        ("manager-exit", False),
        ("manager-exit", True),
        ("manager-exit-noop", False),
        ("manager-exit-noop", True),
        ("entry-context", False),
        ("entry-context", True),
        ("propagator", False),
        ("propagator", True),
        ("converter", False),
        ("converter", True),
        ("span-status", True),
        ("span-attribute", True),
    ]:
        with pytest.MonkeyPatch.context() as monkeypatch:
            await _run_hostile_span_lifecycle_case(failure_seam, business_fails, monkeypatch)


async def _run_hostile_span_lifecycle_case(
    failure_seam: str,
    business_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = f"failure_seam={failure_seam!r} business_fails={business_fails!r}"
    module = _temporal()
    ambient_context = _span_context({"traceparent": TRACEPARENT})
    hostile_context = _span_context({"traceparent": SECOND_TRACEPARENT})
    telemetry_context = _span_context(
        {"traceparent": "00-33333333333333333333333333333333-4444444444444444-01"}
    )
    original_attach = module.otel_context.attach
    original_detach = module.otel_context.detach
    original_get_current = module.otel_context.get_current
    ambient_token = original_attach(ambient_context)
    business_result = object()
    business_error = RuntimeError("span-shell-business-private-canary")

    class HostileSpan:
        def set_status(self, _status: object) -> None:
            if failure_seam == "span-status":
                original_attach(telemetry_context)
                raise RuntimeError("span-status-telemetry-canary")

        def set_attribute(self, _key: str, _value: object) -> None:
            if failure_seam == "span-attribute":
                original_attach(telemetry_context)
                raise RuntimeError("span-attribute-telemetry-canary")

    class HostileManager:
        def __init__(self) -> None:
            self.token: object | None = None

        def __enter__(self) -> HostileSpan:
            self.token = original_attach(hostile_context)
            if failure_seam == "manager-enter":
                raise RuntimeError("manager-enter-telemetry-canary")
            return HostileSpan()

        def __exit__(self, *_args: object) -> None:
            if failure_seam in {"manager-enter", "manager-exit"}:
                original_attach(telemetry_context)
                raise RuntimeError("manager-exit-telemetry-canary")
            if failure_seam == "manager-exit-noop":
                return
            assert self.token is not None, case
            original_detach(self.token)
            self.token = None

    manager = HostileManager()

    class HostileTracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> HostileManager:
            return manager

    class Terminal:
        def __init__(self) -> None:
            self.calls = 0
            self.failure_traceback: Any = None

        async def signal_workflow(self, _input: object) -> object:
            self.calls += 1
            if business_fails:
                try:
                    raise business_error
                except BaseException as error:
                    self.failure_traceback = _traceback_tail(error.__traceback__)
                    raise
            return business_result

    root = module.SafeTemporalTracingInterceptor(cast(Any, HostileTracer()), role="client")
    terminal = Terminal()
    wrapped = root.intercept_client(cast(Any, terminal))
    if failure_seam == "entry-context":
        monkeypatch.setattr(
            module.otel_context,
            "get_current",
            lambda: (_ for _ in ()).throw(RuntimeError("current-context-telemetry-canary")),
        )
    elif failure_seam == "propagator":

        def hostile_inject(*_args: object, **_kwargs: object) -> None:
            original_attach(telemetry_context)
            raise RuntimeError("propagator-telemetry-canary")

        monkeypatch.setattr(
            module._TRACE_PROPAGATOR,
            "inject",
            hostile_inject,
        )
    elif failure_seam == "converter":

        def hostile_to_payloads(_values: object) -> None:
            original_attach(telemetry_context)
            raise RuntimeError("converter-telemetry-canary")

        monkeypatch.setattr(
            module,
            "_PAYLOAD_CONVERTER",
            SimpleNamespace(to_payloads=hostile_to_payloads),
        )

    caught: BaseException | None = None
    result: object | None = None
    try:
        try:
            result = await wrapped.signal_workflow(_client_input("signal_workflow"))
        except BaseException as error:
            caught = error
        assert terminal.calls == 1, case
        assert original_get_current() is ambient_context, case
        if business_fails:
            assert caught is business_error, case
            assert _traceback_tail(caught.__traceback__) is terminal.failure_traceback, case
        else:
            assert caught is None, case
            assert result is business_result, case
    finally:
        if manager.token is not None:
            with suppress(BaseException):
                original_detach(manager.token)
        with suppress(BaseException):
            original_detach(ambient_token)


@pytest.mark.asyncio
async def test_entry_context_failure_still_sanitizes_temporal_and_nexus_inputs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _temporal()
    canary = "entry-context-stale-private-canary"
    application_payload = Payload(metadata={"encoding": b"binary/plain"}, data=b"application-value")
    stale_payload = _valid_payload(tracestate=f"a={canary}")
    original_signal_headers = {
        "application": application_payload,
        "_tracer-data": stale_payload,
    }
    signal_input = temporalio.client.SignalWorkflowInput(
        id="workflow-id",
        run_id=None,
        signal="trace_signal",
        args=(),
        headers=original_signal_headers,
        rpc_metadata={},
        rpc_timeout=None,
    )
    signal_result = object()
    signal_terminal = _ClientTerminal(result=signal_result)

    business_value = "".join(("business", "-value"))
    original_nexus_headers = {
        "Business-Key": business_value,
        "traceparent": TRACEPARENT,
        "tracestate": f"a={canary}",
        "baggage": canary,
        "_tracer-data": canary,
    }
    start_context = _start_context(original_nexus_headers)
    cancel_context = _cancel_context(original_nexus_headers)
    start_input = ExecuteNexusOperationStartInput(start_context, object())
    cancel_input = ExecuteNexusOperationCancelInput(cancel_context, "token")
    nexus_result = object()
    nexus_terminal = _NexusTerminal(result=nexus_result)

    class UnusedTracer:
        calls = 0

        def start_as_current_span(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("entry-context failure reached tracer")

    tracer = UnusedTracer()
    signal_wrapper = module.SafeTemporalTracingInterceptor(
        cast(Any, tracer), role="client"
    ).intercept_client(cast(Any, signal_terminal))
    nexus_wrapper = module.SafeTemporalTracingInterceptor(
        cast(Any, tracer), role="worker"
    ).intercept_nexus_operation(cast(Any, nexus_terminal))
    monkeypatch.setattr(
        module.otel_context,
        "get_current",
        lambda: (_ for _ in ()).throw(RuntimeError("entry-context-telemetry-canary")),
    )

    with caplog.at_level(logging.WARNING):
        assert await signal_wrapper.signal_workflow(signal_input) is signal_result
        assert await nexus_wrapper.execute_nexus_operation_start(start_input) is nexus_result
        await nexus_wrapper.execute_nexus_operation_cancel(cancel_input)

    assert tracer.calls == 0
    assert signal_terminal.calls == [("signal_workflow", signal_input)]
    assert original_signal_headers == {
        "application": application_payload,
        "_tracer-data": stale_payload,
    }
    assert signal_input.headers == {"application": application_payload}
    assert signal_input.headers["application"] is application_payload
    assert (nexus_terminal.start_calls, nexus_terminal.cancel_calls) == (1, 1)
    assert start_input.ctx is not start_context
    assert cancel_input.ctx is not cancel_context
    assert start_context.headers is original_nexus_headers
    assert cancel_context.headers is original_nexus_headers
    assert start_input.ctx.headers == {"Business-Key": business_value}
    assert cancel_input.ctx.headers == {"Business-Key": business_value}
    assert start_input.ctx.headers["Business-Key"] is business_value
    assert cancel_input.ctx.headers["Business-Key"] is business_value

    captured = capsys.readouterr()
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    downstream = repr(signal_input.headers) + repr(start_input.ctx) + repr(cancel_input.ctx)
    assert canary not in rendered + captured.out + captured.err + downstream


@pytest.mark.parametrize("boundary", ["span-shell", "workflow"])
def test_temporal_context_boundaries_do_not_reattach_body_leaked_foreign_context(
    boundary: str,
) -> None:
    module = _temporal()
    isolated_context = ContextVarsContext()
    outer_entry = module.otel_context.get_current()
    observed: dict[str, Context] = {}

    async def exercise() -> None:
        entry_context = module.otel_context.get_current()
        span_owned_context = Context({"temporal-context-owner": boundary})
        foreign_context = Context({"temporal-context-foreign": boundary})
        product_result = object()
        calls = 0

        class Terminal:
            async def _call(self) -> object:
                nonlocal calls
                calls += 1
                observed["owned"] = module.otel_context.get_current()
                module.otel_context.attach(foreign_context)
                assert module.otel_context.get_current() is foreign_context
                return product_result

            async def signal_workflow(self, _input: object) -> object:
                return await self._call()

            async def handle_signal(self, _input: object) -> object:
                return await self._call()

        terminal = Terminal()
        if boundary == "span-shell":

            class Manager:
                token: object | None = None

                def __enter__(self) -> SimpleNamespace:
                    self.token = module.otel_context.attach(span_owned_context)
                    return SimpleNamespace()

                def __exit__(self, *_args: object) -> None:
                    assert self.token is not None
                    module.otel_context.detach(self.token)

            class Tracer:
                def start_as_current_span(self, *_args: object, **_kwargs: object) -> Manager:
                    return Manager()

            wrapper = module.SafeTemporalTracingInterceptor(
                cast(Any, Tracer()), role="client"
            ).intercept_client(cast(Any, terminal))
            result = await wrapper.signal_workflow(_client_input("signal_workflow"))
        else:
            incoming_context = _span_context({"traceparent": TRACEPARENT})
            headers = module.encode_temporal_trace_headers({}, context=incoming_context)
            workflow = object.__new__(module.TracingWorkflowInboundInterceptor)
            temporalio.worker.WorkflowInboundInterceptor.__init__(workflow, cast(Any, terminal))
            workflow.header_key = "_tracer-data"
            workflow.text_map_propagator = TraceContextTextMapPropagator()
            workflow.payload_converter = PayloadConverter.default
            workflow._workflow_context_carrier = None
            result = await workflow.handle_signal(HandleSignalInput("signal", (), headers))

        assert result is product_result
        assert calls == 1
        assert observed["owned"] is not entry_context
        assert module.otel_context.get_current() is entry_context
        observed["entry"] = entry_context

    isolated_context.run(asyncio.run, exercise())
    assert isolated_context.run(module.otel_context.get_current) is observed["entry"]
    assert module.otel_context.get_current() is outer_entry


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["span-shell", "workflow"])
async def test_temporal_context_boundaries_preserve_ambient_entry_through_owner_detach(
    boundary: str,
) -> None:
    module = _temporal()
    prior_entry = module.otel_context.get_current()
    ambient_context = Context({"temporal-context-ambient": boundary})
    ambient_token = module.otel_context.attach(ambient_context)
    product_result = object()
    calls = 0
    observed: dict[str, Context] = {}
    finished_span_id: int | None = None

    class Terminal:
        async def _call(self) -> object:
            nonlocal calls
            calls += 1
            observed["owned"] = module.otel_context.get_current()
            return product_result

        async def signal_workflow(self, _input: object) -> object:
            return await self._call()

        async def handle_signal(self, _input: object) -> object:
            return await self._call()

    terminal = Terminal()
    try:
        if boundary == "span-shell":
            with _recording_tracer() as (tracer, exporter):
                wrapper = module.SafeTemporalTracingInterceptor(
                    tracer, role="client"
                ).intercept_client(cast(Any, terminal))
                result = await wrapper.signal_workflow(_client_input("signal_workflow"))
                finished = exporter.get_finished_spans()
                assert len(finished) == 1
                assert finished[0].end_time is not None
                finished_span_id = finished[0].context.span_id
        else:
            incoming_context = _span_context({"traceparent": SECOND_TRACEPARENT})
            headers = module.encode_temporal_trace_headers({}, context=incoming_context)
            workflow = object.__new__(module.TracingWorkflowInboundInterceptor)
            temporalio.worker.WorkflowInboundInterceptor.__init__(workflow, cast(Any, terminal))
            workflow.header_key = "_tracer-data"
            workflow.text_map_propagator = TraceContextTextMapPropagator()
            workflow.payload_converter = PayloadConverter.default
            workflow._workflow_context_carrier = None
            result = await workflow.handle_signal(HandleSignalInput("signal", (), headers))

        assert result is product_result
        assert calls == 1
        assert observed["owned"] is not ambient_context
        assert module.otel_context.get_current() is ambient_context
        if finished_span_id is not None:
            assert get_current_span().get_span_context().span_id != finished_span_id
    finally:
        module.otel_context.detach(ambient_token)

    assert module.otel_context.get_current() is prior_entry
    if finished_span_id is not None:
        assert get_current_span().get_span_context().span_id != finished_span_id


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["span-shell", "workflow"])
async def test_temporal_context_boundaries_support_body_local_foreign_scope_when_detached_before_return(  # noqa: E501
    boundary: str,
) -> None:
    module = _temporal()
    entry_context = module.otel_context.get_current()
    temporary_foreign = Context({"temporal-context-temporary": boundary})
    product_result = object()
    calls = 0
    observed: dict[str, Context] = {}

    class Terminal:
        async def _call(self) -> object:
            nonlocal calls
            calls += 1
            owned_context = module.otel_context.get_current()
            observed["owned"] = owned_context
            temporary_token = module.otel_context.attach(temporary_foreign)
            try:
                assert module.otel_context.get_current() is temporary_foreign
            finally:
                module.otel_context.detach(temporary_token)
            assert module.otel_context.get_current() is owned_context
            observed["after-detach"] = module.otel_context.get_current()
            return product_result

        async def signal_workflow(self, _input: object) -> object:
            return await self._call()

        async def handle_signal(self, _input: object) -> object:
            return await self._call()

    terminal = Terminal()
    if boundary == "span-shell":
        with _recording_tracer() as (tracer, _exporter):
            wrapper = module.SafeTemporalTracingInterceptor(tracer, role="client").intercept_client(
                cast(Any, terminal)
            )
            result = await wrapper.signal_workflow(_client_input("signal_workflow"))
    else:
        incoming_context = _span_context({"traceparent": SECOND_TRACEPARENT})
        headers = module.encode_temporal_trace_headers({}, context=incoming_context)
        workflow = object.__new__(module.TracingWorkflowInboundInterceptor)
        temporalio.worker.WorkflowInboundInterceptor.__init__(workflow, cast(Any, terminal))
        workflow.header_key = "_tracer-data"
        workflow.text_map_propagator = TraceContextTextMapPropagator()
        workflow.payload_converter = PayloadConverter.default
        workflow._workflow_context_carrier = None
        result = await workflow.handle_signal(HandleSignalInput("signal", (), headers))

    assert result is product_result
    assert calls == 1
    assert observed["owned"] is not entry_context
    assert observed["after-detach"] is observed["owned"]
    assert module.otel_context.get_current() is entry_context


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["span-shell", "workflow"])
@pytest.mark.parametrize("business_fails", [False, True])
async def test_fail_open_cleanup_never_clobbers_foreign_downstream_context(
    boundary: str,
    business_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _temporal()
    ambient_context = _span_context({"traceparent": TRACEPARENT})
    owned_context = _span_context({"traceparent": SECOND_TRACEPARENT})
    foreign_context = _span_context(
        {"traceparent": "00-55555555555555555555555555555555-6666666666666666-01"}
    )
    telemetry_context = _span_context(
        {"traceparent": "00-77777777777777777777777777777777-8888888888888888-01"}
    )
    original_attach = module.otel_context.attach
    original_detach = module.otel_context.detach
    ambient_token = original_attach(ambient_context)
    business_result = object()
    business_error = RuntimeError("foreign-context-business-private-canary")

    class Terminal:
        def __init__(self) -> None:
            self.calls = 0
            self.failure_traceback: Any = None

        async def _call(self) -> object:
            self.calls += 1
            foreign_token = original_attach(foreign_context)
            try:
                if business_fails:
                    try:
                        raise business_error
                    except BaseException as error:
                        self.failure_traceback = _traceback_tail(error.__traceback__)
                        raise
                return business_result
            finally:
                original_detach(foreign_token)

        async def signal_workflow(self, _input: object) -> object:
            return await self._call()

        async def execute_workflow(self, _input: object) -> object:
            return await self._call()

    terminal = Terminal()
    manager_token: object | None = None
    teardown_calls = 0

    class Manager:
        def __enter__(self) -> SimpleNamespace:
            nonlocal manager_token
            manager_token = original_attach(owned_context)
            return SimpleNamespace(
                set_status=lambda _status: None,
                set_attribute=lambda _key, _value: None,
            )

        def __exit__(self, *_args: object) -> None:
            nonlocal teardown_calls
            teardown_calls += 1
            original_attach(telemetry_context)
            raise RuntimeError("foreign-context-exit-telemetry-canary")

    class Tracer:
        def start_as_current_span(self, *_args: object, **_kwargs: object) -> Manager:
            return Manager()

    caught: BaseException | None = None
    result: object | None = None
    try:
        if boundary == "span-shell":
            wrapper = module.SafeTemporalTracingInterceptor(
                cast(Any, Tracer()), role="client"
            ).intercept_client(cast(Any, terminal))
            invocation = wrapper.signal_workflow(_client_input("signal_workflow"))
        else:

            def hostile_detach(_token: object) -> None:
                nonlocal teardown_calls
                teardown_calls += 1
                original_attach(telemetry_context)
                raise RuntimeError("foreign-context-detach-telemetry-canary")

            monkeypatch.setattr(module.otel_context, "detach", hostile_detach)
            workflow = object.__new__(module.TracingWorkflowInboundInterceptor)
            temporalio.worker.WorkflowInboundInterceptor.__init__(workflow, cast(Any, terminal))
            workflow.header_key = "_tracer-data"
            workflow.text_map_propagator = TraceContextTextMapPropagator()
            workflow.payload_converter = PayloadConverter.default
            workflow._workflow_context_carrier = None
            workflow._extern_functions = {
                "__temporal_opentelemetry_completed_span": lambda _params: {}
            }
            headers = module.encode_temporal_trace_headers({}, context=owned_context)
            invocation = workflow.execute_workflow(
                ExecuteWorkflowInput(type("Workflow", (), {}), cast(Any, None), (), headers)
            )
        try:
            result = await invocation
        except BaseException as error:
            caught = error
        assert terminal.calls == 1
        assert teardown_calls == 1
        assert module.otel_context.get_current() is ambient_context
        if business_fails:
            assert caught is business_error
            assert _traceback_tail(caught.__traceback__) is terminal.failure_traceback
        else:
            assert caught is None
            assert result is business_result
    finally:
        if manager_token is not None:
            with suppress(BaseException):
                original_detach(manager_token)
        with suppress(BaseException):
            original_detach(ambient_token)


@pytest.mark.asyncio
async def test_workflow_entry_is_fail_open_and_calls_terminal_once() -> None:
    for business_fails in [False, True]:
        for failure_seam in [
            "headers",
            "get-current",
            "set-context",
            "attach",
            "finalize",
            "detach",
            "detach-noop",
        ]:
            with pytest.MonkeyPatch.context() as monkeypatch:
                await _run_workflow_entry_fail_open_case(failure_seam, business_fails, monkeypatch)


async def _run_workflow_entry_fail_open_case(
    failure_seam: str,
    business_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = f"failure_seam={failure_seam!r} business_fails={business_fails!r}"
    module = _temporal()
    business_error = RuntimeError("workflow-business-private-canary")
    business_result = object()
    ambient_context = _span_context({"traceparent": TRACEPARENT})
    incoming_context = _span_context({"traceparent": SECOND_TRACEPARENT})
    telemetry_context = _span_context(
        {"traceparent": "00-77777777777777777777777777777777-8888888888888888-01"}
    )
    incoming_headers = module.encode_temporal_trace_headers({}, context=incoming_context)
    original_detach = module.otel_context.detach
    original_get_current = module.otel_context.get_current
    ambient_token = module.otel_context.attach(ambient_context)

    class Terminal:
        def __init__(self) -> None:
            self.calls = 0
            self.failure_traceback: Any = None

        async def execute_workflow(self, _input: object) -> object:
            self.calls += 1
            if business_fails:
                try:
                    raise business_error
                except BaseException as error:
                    self.failure_traceback = _traceback_tail(error.__traceback__)
                    raise
            return business_result

    class HostileHeaders:
        @property
        def headers(self) -> object:
            raise RuntimeError("workflow-header-telemetry-canary")

    terminal = Terminal()
    workflow = object.__new__(module.TracingWorkflowInboundInterceptor)
    temporalio.worker.WorkflowInboundInterceptor.__init__(workflow, cast(Any, terminal))
    workflow.header_key = "_tracer-data"
    workflow.text_map_propagator = TraceContextTextMapPropagator()
    workflow.payload_converter = PayloadConverter.default
    workflow._workflow_context_carrier = None
    workflow._extern_functions = {"__temporal_opentelemetry_completed_span": lambda _params: {}}
    input: object = ExecuteWorkflowInput(
        type("Workflow", (), {}), cast(Any, None), (), incoming_headers
    )
    if failure_seam == "headers":
        input = HostileHeaders()
    elif failure_seam == "get-current":
        monkeypatch.setattr(
            module.otel_context,
            "get_current",
            lambda: (_ for _ in ()).throw(RuntimeError("workflow-current-telemetry-canary")),
        )
    elif failure_seam == "set-context":
        workflow._set_on_context = lambda _context: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("workflow-set-context-telemetry-canary")
        )
    elif failure_seam == "attach":
        monkeypatch.setattr(
            module.otel_context,
            "attach",
            lambda _context: (_ for _ in ()).throw(
                RuntimeError("workflow-attach-telemetry-canary")
            ),
        )
    elif failure_seam == "finalize":
        workflow._completed_span = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("workflow-finalize-telemetry-canary")
        )
    elif failure_seam == "detach":

        def hostile_detach(_token: object) -> None:
            module.otel_context.attach(telemetry_context)
            raise RuntimeError("workflow-detach-telemetry-canary")

        monkeypatch.setattr(module.otel_context, "detach", hostile_detach)
    else:
        monkeypatch.setattr(
            module.otel_context,
            "detach",
            lambda _token: None,
        )

    try:
        caught: BaseException | None = None
        result: object | None = None
        try:
            result = await workflow.execute_workflow(cast(Any, input))
        except BaseException as error:
            caught = error
        assert terminal.calls == 1, case
        assert original_get_current() is ambient_context, case
        if business_fails:
            assert caught is business_error, case
            assert _traceback_tail(caught.__traceback__) is terminal.failure_traceback, case
        else:
            assert caught is None, case
            assert result is business_result, case
    finally:
        with suppress(BaseException):
            original_detach(ambient_token)


@pytest.mark.asyncio
async def test_production_replayer_emits_zero_workflow_spans() -> None:
    module = _temporal()
    fixture = REPO_ROOT / "packages/workflows/tests/fixtures/phase9_temporal/agent-tool-step.json"
    history = temporalio.client.WorkflowHistory.from_json(
        "task-6-production-replay",
        fixture.read_text(encoding="utf-8"),
    )
    from jhin_workflows.agent_task import AgentTaskWorkflow
    from jhin_workflows.engineering_ticket import EngineeringTicketWorkflow
    from jhin_workflows.triggered_task import TriggeredTaskWorkflow

    with _recording_tracer() as (tracer, exporter):
        await temporalio.worker.Replayer(
            workflows=[AgentTaskWorkflow, TriggeredTaskWorkflow, EngineeringTicketWorkflow],
            interceptors=[module.SafeTemporalTracingInterceptor(tracer, role="worker")],
        ).replay_workflow(history)
        assert exporter.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_signal_history_replay_is_deterministic_with_production_interceptor() -> None:
    module = _temporal()
    fixture = (
        REPO_ROOT / "packages/workflows/tests/fixtures/phase9_temporal/agent-parked-approval.json"
    )
    history = temporalio.client.WorkflowHistory.from_json(
        "task-6-signal-replay",
        fixture.read_text(encoding="utf-8"),
    )
    from jhin_workflows.agent_task import AgentTaskWorkflow

    with _recording_tracer() as (tracer, exporter):
        await temporalio.worker.Replayer(
            workflows=[AgentTaskWorkflow],
            interceptors=[module.SafeTemporalTracingInterceptor(tracer, role="worker")],
        ).replay_workflow(history)
        assert exporter.get_finished_spans() == ()


def test_helpers_forward_exact_runtime_and_list_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _temporal()
    runtime = SimpleNamespace(tracer=noop_tracer(), metrics=noop_metrics())
    client_list = [object()]
    worker_list = [object()]
    connected = object()
    captured: dict[str, object] = {}

    async def connect(*args: object, **kwargs: object) -> object:
        captured["connect_args"] = args
        captured["connect_kwargs"] = kwargs
        return connected

    class Worker:
        def __init__(self, client: object, **kwargs: object) -> None:
            captured["worker_client"] = client
            captured["worker_kwargs"] = kwargs

    monkeypatch.setattr(module, "temporal_client_interceptors", lambda value: client_list)
    monkeypatch.setattr(
        module, "temporal_worker_interceptors", lambda value, *, task_queue: worker_list
    )
    monkeypatch.setattr(module.Client, "connect", connect)
    monkeypatch.setattr(module, "Worker", Worker)

    settings = SimpleNamespace(temporal_address="temporal:7233", temporal_namespace="default")

    async def exercise() -> None:
        assert await module.connect_temporal_client(settings, runtime) is connected

    asyncio.run(exercise())
    workflows = [type("Workflow", (), {})]
    activities = [lambda: None]
    worker = module.build_temporal_worker(
        connected,
        runtime=runtime,
        task_queue="jhin-agent-queue",
        workflows=workflows,
        activities=activities,
    )
    assert isinstance(worker, Worker)
    assert cast(dict[str, object], captured["connect_kwargs"])["interceptors"] is client_list
    worker_kwargs = cast(dict[str, object], captured["worker_kwargs"])
    assert worker_kwargs["interceptors"] is worker_list
    assert worker_kwargs["workflows"] is workflows
    assert worker_kwargs["activities"] is activities


def test_error_span_is_closed_and_preserves_business_exception() -> None:
    module = _temporal()
    business = RuntimeError("private-exception-canary")
    with _recording_tracer() as (tracer, exporter):
        interceptor = module.SafeTemporalTracingInterceptor(tracer, role="client")
        caught: BaseException | None = None
        try:
            with interceptor._start_as_current_span(
                "StartWorkflow:private-workflow-name",
                attributes={"temporalWorkflowID": "workflow-1"},
                kind=SpanKind.CLIENT,
            ):
                raise business
        except BaseException as exc:
            caught = exc
        assert caught is business
        span = exporter.get_finished_spans()[0]
        assert span.name == "temporal.start_workflow"
        assert span.events == ()
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description is None
        assert dict(span.attributes or {}) == {
            "temporal.workflow_id": "workflow-1",
            "error.type": "RuntimeError",
            "error.code": "internal_error",
        }
        rendered = repr(span)
        assert "private-exception-canary" not in rendered


_AUTHORITY_CALLS = frozenset(
    {
        "jhin_observability.connect_temporal_client",
        "jhin_observability.build_temporal_worker",
        "jhin_observability.initialize_observability",
        "jhin_observability.configure_json_logging",
        "jhin_observability.configure_logging",
        "jhin_observability.SafeTemporalTracingInterceptor",
        "jhin_observability.TemporalActivityMetricsInterceptor",
        "jhin_observability.temporal.SafeTemporalTracingInterceptor",
        "jhin_observability.temporal.TemporalActivityMetricsInterceptor",
        "temporalio.client.Client.connect",
        "temporalio.worker.Worker",
        "jhin_db.create_engine",
        "jhin_events.EventPublisher",
        "jhin_events.publisher.EventPublisher",
    }
)
_AUTHORITY_LEAVES = (
    frozenset(name.rsplit(".", 1)[-1] for name in _AUTHORITY_CALLS) - {"connect"}
) | {"Client", "temporal_client_interceptors", "temporal_worker_interceptors"}


@dataclasses.dataclass(frozen=True)
class _Binding:
    qualified: str | None
    direct: bool
    initialized: bool
    rebound: bool
    authority_tainted: bool
    origin: str = "other"
    origin_scope_index: int | None = None


@dataclasses.dataclass(frozen=True)
class _ScannedCall:
    node: ast.Call
    qualified: str | None
    spelling: str
    direct: bool
    unresolved_authority: bool
    functions: tuple[str, ...]
    classes: tuple[str, ...]
    rebound_names: frozenset[str]
    root_origins: tuple[tuple[str, str], ...]
    anonymous_scopes: tuple[str, ...]


def _dotted_spelling(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_spelling(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _module_name(path: Path) -> str:
    parts = path.as_posix().split("/src/", 1)
    if len(parts) != 2:
        return "fixture"
    relative = parts[1]
    if relative.endswith("/__init__.py"):
        relative = relative[: -len("/__init__.py")]
    elif relative.endswith(".py"):
        relative = relative[:-3]
    return relative.replace("/", ".")


def _pattern_capture_names(pattern: ast.pattern) -> frozenset[str]:
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
    return frozenset(names)


class _LocalNameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

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
        if not node.type_params:
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

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.names.add(node.name.id)

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


class _BindingCallScanner(ast.NodeVisitor):
    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.scopes: list[dict[str, _Binding]] = [{}]
        self.scope_kinds: list[str] = ["module"]
        self.hidden_class_scope_indices: set[int] = set()
        self.class_scope_post_bindings: dict[int, tuple[str, int]] = {}
        self.deferred_post_class_bindings: list[tuple[str, int]] = []
        self.code_scope_indices: list[int] = [0]
        self.functions: list[str] = []
        self.classes: list[str] = []
        self.anonymous_scopes: list[str] = []
        self.calls: list[_ScannedCall] = []
        self.declared_scope_names: set[str] = set()

    def visit_Module(self, node: ast.Module) -> None:
        self.declared_scope_names = {
            name
            for descendant in ast.walk(node)
            if isinstance(descendant, (ast.Global, ast.Nonlocal))
            for name in descendant.names
        }
        self.generic_visit(node)

    def _push_scope(self, scope: dict[str, _Binding], kind: str) -> int:
        self.scopes.append(scope)
        self.scope_kinds.append(kind)
        return len(self.scopes) - 1

    def _pop_scope(self) -> dict[str, _Binding]:
        self.scope_kinds.pop()
        return self.scopes.pop()

    def _hide_active_class_scopes(
        self,
        *,
        deferred: bool,
    ) -> tuple[set[int], list[tuple[str, int]]]:
        previous = set(self.hidden_class_scope_indices)
        previous_post_bindings = list(self.deferred_post_class_bindings)
        active_class_indices = {
            index for index, kind in enumerate(self.scope_kinds) if kind == "class"
        }
        self.hidden_class_scope_indices.update(active_class_indices)
        if deferred:
            for index in sorted(active_class_indices):
                post_binding = self.class_scope_post_bindings.get(index)
                if (
                    post_binding is not None
                    and post_binding not in self.deferred_post_class_bindings
                ):
                    self.deferred_post_class_bindings.append(post_binding)
        return previous, previous_post_bindings

    def _lookup(self, name: str) -> _Binding:
        binding_scope_index: int | None = None
        for index in range(len(self.scopes) - 1, -1, -1):
            if index in self.hidden_class_scope_indices:
                continue
            scope = self.scopes[index]
            if name in scope:
                binding = scope[name]
                binding_scope_index = index
                break
        else:
            binding = _Binding(
                qualified=None,
                direct=True,
                initialized=False,
                rebound=False,
                authority_tainted=name in _AUTHORITY_LEAVES,
            )
        post_binding_scope_indices = [
            scope_index
            for pending_name, scope_index in self.deferred_post_class_bindings
            if pending_name == name
        ]
        if post_binding_scope_indices and (
            binding_scope_index is None or binding_scope_index <= max(post_binding_scope_indices)
        ):
            return _Binding(
                qualified=None,
                direct=False,
                initialized=True,
                rebound=True,
                authority_tainted=(binding.authority_tainted or name in _AUTHORITY_LEAVES),
            )
        if name in self.declared_scope_names:
            return _Binding(
                qualified=None,
                direct=False,
                initialized=True,
                rebound=True,
                authority_tainted=(binding.authority_tainted or name in _AUTHORITY_LEAVES),
                origin="other",
                origin_scope_index=None,
            )
        return binding

    def _resolve(self, node: ast.AST) -> _Binding:
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            qualified = f"{base.qualified}.{node.attr}" if base.qualified is not None else None
            return _Binding(
                qualified=qualified,
                direct=base.direct,
                initialized=base.initialized,
                rebound=base.rebound,
                authority_tainted=(base.authority_tainted or node.attr in _AUTHORITY_LEAVES),
                origin=base.origin,
                origin_scope_index=base.origin_scope_index,
            )
        return _Binding(None, True, True, False, False)

    def _bind(self, name: str, binding: _Binding, *, scope_index: int = -1) -> None:
        scope = self.scopes[scope_index]
        previous = scope.get(name)
        rebound = binding.rebound or bool(previous and previous.initialized)
        tainted = (
            binding.authority_tainted
            or name in _AUTHORITY_LEAVES
            or bool(previous and previous.authority_tainted)
        )
        scope[name] = dataclasses.replace(
            binding,
            initialized=True,
            rebound=rebound,
            authority_tainted=tainted,
            origin=("other" if rebound else binding.origin),
            origin_scope_index=(None if rebound else binding.origin_scope_index),
        )

    def _bind_assignment(
        self,
        target: ast.AST,
        value: ast.AST | None,
        *,
        scope_index: int = -1,
        allow_canonical_initializer: bool = False,
    ) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._bind_assignment(item, None, scope_index=scope_index)
            return
        if isinstance(target, ast.Starred):
            self._bind_assignment(target.value, None, scope_index=scope_index)
            return
        if not isinstance(target, ast.Name):
            return
        resolved = (
            self._resolve(value) if value is not None else _Binding(None, True, True, False, False)
        )
        actual_scope_index = scope_index if scope_index >= 0 else len(self.scopes) - 1
        canonical_initializer = False
        if allow_canonical_initializer and isinstance(value, ast.Call):
            callee = self._resolve(value.func)
            canonical_initializer = (
                callee.qualified == "jhin_observability.initialize_observability"
                and callee.direct
                and not callee.rebound
            )
        if resolved.qualified is not None and not resolved.qualified.startswith("local:"):
            binding = dataclasses.replace(
                resolved,
                direct=False,
                origin="other",
                origin_scope_index=None,
            )
        else:
            binding = _Binding(
                qualified=f"local:{target.id}",
                direct=True,
                initialized=True,
                rebound=False,
                authority_tainted=resolved.authority_tainted,
                origin=("canonical_initializer" if canonical_initializer else "other"),
                origin_scope_index=(actual_scope_index if canonical_initializer else None),
            )
        self._bind(target.id, binding, scope_index=scope_index)

    def _rebound_names(self) -> frozenset[str]:
        rebound = {
            name for scope in self.scopes for name, binding in scope.items() if binding.rebound
        }
        return frozenset(rebound | self.declared_scope_names)

    def _current_root_origins(self) -> tuple[tuple[str, str], ...]:
        current_scope_index = self.code_scope_indices[-1]
        names = {name for scope in self.scopes for name in scope}
        origins = []
        for name in names:
            binding = self._lookup(name)
            if (
                not binding.rebound
                and binding.origin != "other"
                and binding.origin_scope_index == current_scope_index
            ):
                origins.append((name, binding.origin))
        return tuple(sorted(origins))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".", 1)[0]
            qualified = alias.name if alias.asname else alias.name.split(".", 1)[0]
            self._bind(
                name,
                _Binding(qualified, alias.asname is None, True, False, False),
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            if node.level:
                self._bind(
                    name,
                    _Binding(
                        None,
                        False,
                        True,
                        False,
                        alias.name in _AUTHORITY_LEAVES or name in _AUTHORITY_LEAVES,
                    ),
                )
                continue
            qualified = f"{node.module}.{alias.name}"
            self._bind(
                name,
                _Binding(
                    qualified,
                    alias.asname is None,
                    True,
                    False,
                    qualified in _AUTHORITY_CALLS,
                ),
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_assignment(
                target,
                node.value,
                allow_canonical_initializer=(
                    len(node.targets) == 1 and isinstance(target, ast.Name)
                ),
            )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._bind_assignment(node.target, node.value)
        self.visit(node.annotation)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._bind_assignment(node.target, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_assignment(
            node.target,
            node.value,
            scope_index=self.code_scope_indices[-1],
        )

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._bind_assignment(target, None)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind_assignment(node.target, None)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_assignment(item.optional_vars, None)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self._bind_assignment(ast.Name(id=node.name, ctx=ast.Store()), None)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.visit(case.pattern)
            for name in sorted(_pattern_capture_names(case.pattern)):
                self._bind_assignment(ast.Name(id=name, ctx=ast.Store()), None)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        parameters = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg)
        type_bindings = {
            type_parameter.name: _Binding(
                f"local:{type_parameter.name}",
                True,
                True,
                False,
                type_parameter.name in _AUTHORITY_LEAVES,
            )
            for type_parameter in node.type_params
        }
        if type_bindings:
            self._push_scope(dict(type_bindings), "type_params")
            for type_parameter in node.type_params:
                self.visit(type_parameter)
            for parameter in parameters:
                if parameter.annotation is not None:
                    self.visit(parameter.annotation)
            if node.returns is not None:
                self.visit(node.returns)
            self._pop_scope()
        else:
            for parameter in parameters:
                if parameter.annotation is not None:
                    self.visit(parameter.annotation)
            if node.returns is not None:
                self.visit(node.returns)
        self._bind(
            node.name,
            _Binding(
                f"{self.module_name}.{node.name}",
                True,
                True,
                False,
                False,
            ),
        )
        collector = _LocalNameCollector()
        for statement in node.body:
            collector.visit(statement)
        scope = {
            name: _Binding(
                None,
                True,
                False,
                False,
                name in _AUTHORITY_LEAVES,
            )
            for name in collector.names
        }
        scope.update(type_bindings)
        scope_index = len(self.scopes)
        for parameter in parameters:
            scope[parameter.arg] = _Binding(
                f"local:{parameter.arg}",
                True,
                True,
                False,
                parameter.arg in _AUTHORITY_LEAVES,
                origin="parameter",
                origin_scope_index=scope_index,
            )
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=True)
        body_scope_index = self._push_scope(scope, "function")
        self.code_scope_indices.append(body_scope_index)
        self.functions.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.functions.pop()
        self.code_scope_indices.pop()
        self._pop_scope()
        self.hidden_class_scope_indices = previous_hidden
        self.deferred_post_class_bindings = previous_post_bindings

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        collector = _LocalNameCollector()
        collector.visit(node.body)
        scope = {
            name: _Binding(
                None,
                True,
                False,
                False,
                name in _AUTHORITY_LEAVES,
            )
            for name in collector.names
        }
        parameters = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg)
        scope_index = len(self.scopes)
        for parameter in parameters:
            scope[parameter.arg] = _Binding(
                f"local:{parameter.arg}",
                True,
                True,
                False,
                parameter.arg in _AUTHORITY_LEAVES,
                origin="parameter",
                origin_scope_index=scope_index,
            )
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=True)
        body_scope_index = self._push_scope(scope, "lambda")
        self.code_scope_indices.append(body_scope_index)
        self.anonymous_scopes.append("lambda")
        self.visit(node.body)
        self.anonymous_scopes.pop()
        self.code_scope_indices.pop()
        self._pop_scope()
        self.hidden_class_scope_indices = previous_hidden
        self.deferred_post_class_bindings = previous_post_bindings

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        values: tuple[ast.AST, ...],
    ) -> None:
        first, *remaining = generators
        self.visit(first.iter)
        collector = _LocalNameCollector()
        for generator in generators:
            collector.visit(generator.target)
        scope = {
            name: _Binding(
                None,
                True,
                False,
                False,
                name in _AUTHORITY_LEAVES,
            )
            for name in collector.names
        }
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=True)
        self._push_scope(scope, "comprehension")
        self.anonymous_scopes.append("comprehension")
        self._bind_assignment(first.target, None)
        for condition in first.ifs:
            self.visit(condition)
        for generator in remaining:
            self.visit(generator.iter)
            self._bind_assignment(generator.target, None)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)
        self.anonymous_scopes.pop()
        self._pop_scope()
        self.hidden_class_scope_indices = previous_hidden
        self.deferred_post_class_bindings = previous_post_bindings

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        type_scope = {
            parameter.name: _Binding(
                f"local:{parameter.name}",
                True,
                True,
                False,
                parameter.name in _AUTHORITY_LEAVES,
            )
            for parameter in node.type_params
        }
        scope_index = self._push_scope(type_scope, "type_params")
        self.code_scope_indices.append(scope_index)
        self.anonymous_scopes.append("type_alias")
        for type_parameter in node.type_params:
            self.visit(type_parameter)
        self.visit(node.value)
        self.anonymous_scopes.pop()
        self.code_scope_indices.pop()
        self._pop_scope()
        self._bind_assignment(node.name, None)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent_scope_index = len(self.scopes) - 1
        post_binding = (
            None
            if self.scope_kinds[parent_scope_index] == "class"
            else (node.name, parent_scope_index)
        )
        for decorator in node.decorator_list:
            self.visit(decorator)
        type_bindings = {
            parameter.name: _Binding(
                f"local:{parameter.name}",
                True,
                True,
                False,
                parameter.name in _AUTHORITY_LEAVES,
            )
            for parameter in node.type_params
        }
        if type_bindings:
            self._push_scope(dict(type_bindings), "type_params")
            for type_parameter in node.type_params:
                self.visit(type_parameter)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
        else:
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
        previous_hidden, previous_post_bindings = self._hide_active_class_scopes(deferred=False)
        class_scope_index = self._push_scope({}, "class")
        if post_binding is not None:
            self.class_scope_post_bindings[class_scope_index] = post_binding
        self.code_scope_indices.append(class_scope_index)
        self.classes.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.classes.pop()
        self.code_scope_indices.pop()
        self.class_scope_post_bindings.pop(class_scope_index, None)
        self._pop_scope()
        self.hidden_class_scope_indices = previous_hidden
        self.deferred_post_class_bindings = previous_post_bindings
        if type_bindings:
            self._pop_scope()
        self._bind(
            node.name,
            _Binding(
                f"{self.module_name}.{node.name}",
                True,
                True,
                False,
                node.name in _AUTHORITY_LEAVES,
            ),
        )

    def visit_Call(self, node: ast.Call) -> None:
        binding = self._resolve(node.func)
        self.calls.append(
            _ScannedCall(
                node=node,
                qualified=binding.qualified,
                spelling=_dotted_spelling(node.func),
                direct=binding.direct,
                unresolved_authority=(
                    binding.authority_tainted
                    and (binding.qualified is None or binding.qualified.startswith("local:"))
                ),
                functions=tuple(self.functions),
                classes=tuple(self.classes),
                rebound_names=self._rebound_names(),
                root_origins=self._current_root_origins(),
                anonymous_scopes=tuple(self.anonymous_scopes),
            )
        )
        self.generic_visit(node)


def _scan_calls(path: Path) -> tuple[_ScannedCall, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    scanner = _BindingCallScanner(_module_name(path))
    scanner.visit(tree)
    return tuple(scanner.calls)


def _expression_shape(node: ast.AST | None) -> tuple[Any, ...]:
    if isinstance(node, ast.Name):
        return ("name", node.id)
    if isinstance(node, ast.Attribute):
        return ("attr", _expression_shape(node.value), node.attr)
    return (type(node).__name__ if node is not None else "None",)


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def _inventory(items: list[str]) -> dict[str, int]:
    return {owner: items.count(owner) for owner in set(items)}


def _tracer_argument_is_exact(
    scanned: _ScannedCall,
    *,
    expected_shape: tuple[Any, ...],
    root_name: str | None,
    expected_origin: str | None,
) -> bool:
    return (
        _expression_shape(_keyword(scanned.node, "tracer")) == expected_shape
        and (root_name is None or dict(scanned.root_origins).get(root_name) == expected_origin)
        and (root_name is None or root_name not in scanned.rebound_names)
    )


@dataclasses.dataclass(frozen=True)
class _TracerSite:
    shape: tuple[Any, ...]
    root_name: str | None
    origin: str | None
    classes: tuple[str, ...]
    functions: tuple[str, ...]


_ENGINE_TRACER_SITES = {
    # jhin-admin bootstraps no observability runtime, so it passes the same
    # no-op the dev seed does rather than a tracer nothing would collect from.
    "apps/api/src/jhin_api/cli/main.py": _TracerSite(
        ("Call",),
        None,
        None,
        (),
        ("_run",),
    ),
    "apps/api/src/jhin_api/main.py": _TracerSite(
        ("attr", ("name", "runtime"), "tracer"),
        "runtime",
        "canonical_initializer",
        (),
        ("create_app", "lifespan"),
    ),
    "apps/api/src/jhin_api/seed.py": _TracerSite(
        ("Call",),
        None,
        None,
        (),
        ("run",),
    ),
    "services/agent_worker/src/jhin_agent_worker/resources.py": _TracerSite(
        ("attr", ("name", "runtime"), "tracer"),
        "runtime",
        "parameter",
        ("Resources",),
        ("create",),
    ),
    "services/event_worker/src/jhin_event_worker/main.py": _TracerSite(
        ("attr", ("name", "runtime"), "tracer"),
        "runtime",
        "canonical_initializer",
        (),
        ("main",),
    ),
    "services/tool_worker/src/jhin_tool_worker/resources.py": _TracerSite(
        ("attr", ("name", "runtime"), "tracer"),
        "runtime",
        "parameter",
        ("ToolWorkerResources",),
        ("create",),
    ),
}

_PUBLISHER_TRACER_SITES = {
    "apps/api/src/jhin_api/conversations/router.py": _TracerSite(
        ("attr", ("name", "runtime"), "tracer"),
        "runtime",
        "parameter",
        (),
        ("get_optional_publisher",),
    ),
    "services/agent_worker/src/jhin_agent_worker/resources.py": _TracerSite(
        ("attr", ("name", "runtime"), "tracer"),
        "runtime",
        "parameter",
        ("Resources",),
        ("create",),
    ),
    "services/event_worker/src/jhin_event_worker/normalizer.py": _TracerSite(
        ("attr", ("name", "self"), "_tracer"),
        "self",
        "parameter",
        ("IngressNormalizer",),
        ("__init__",),
    ),
    "services/tool_worker/src/jhin_tool_worker/resources.py": _TracerSite(
        ("attr", ("name", "runtime"), "tracer"),
        "runtime",
        "parameter",
        ("ToolWorkerResources",),
        ("create",),
    ),
}


def _tracer_site_is_exact(
    relative: str,
    scanned: _ScannedCall,
    *,
    sites: dict[str, _TracerSite],
) -> bool:
    site = sites.get(relative)
    return bool(
        site is not None
        and scanned.classes == site.classes
        and scanned.functions == site.functions
        and not scanned.anonymous_scopes
        and _tracer_argument_is_exact(
            scanned,
            expected_shape=site.shape,
            root_name=site.root_name,
            expected_origin=site.origin,
        )
    )


def _production_python_paths() -> tuple[Path, ...]:
    roots = (
        REPO_ROOT / "apps",
        REPO_ROOT / "services",
        REPO_ROOT / "packages",
    )
    return tuple(
        sorted(
            (
                path
                for root in roots
                for path in root.rglob("*.py")
                if "src" in path.parts
                and "tests" not in path.parts
                and "__pycache__" not in path.parts
            ),
            key=lambda path: path.as_posix(),
        )
    )


def test_temporal_wiring_and_long_lived_tracer_authority_are_exact() -> None:
    helper_clients: list[str] = []
    helper_workers: list[str] = []
    direct_clients: list[str] = []
    direct_workers: list[str] = []
    interceptor_constructors: list[tuple[str, str, str]] = []
    engines: list[str] = []
    publishers: list[str] = []
    bad: list[str] = []
    allowed_direct_clients = {
        "packages/observability/src/jhin_observability/temporal.py",
        "apps/api/src/jhin_api/temporal.py",
        "packages/workflows/src/jhin_workflows/poller_health.py",
    }
    allowed_client_scopes = {
        "packages/observability/src/jhin_observability/temporal.py": (
            (),
            ("connect_temporal_client",),
        ),
        "apps/api/src/jhin_api/temporal.py": (
            ("TemporalClientProvider",),
            ("get",),
        ),
        "packages/workflows/src/jhin_workflows/poller_health.py": (
            (),
            ("queue_has_workflow_poller",),
        ),
    }
    helper_client_owners = {
        "services/agent_worker/src/jhin_agent_worker/main.py": 1,
        "services/tool_worker/src/jhin_tool_worker/main.py": 1,
        "services/event_worker/src/jhin_event_worker/main.py": 1,
        "services/workflow_worker/src/jhin_workflow_worker/main.py": 1,
    }
    helper_worker_owners = {
        owner: 1
        for owner in helper_client_owners
        if owner != "services/event_worker/src/jhin_event_worker/main.py"
    }
    for path in _production_python_paths():
        relative = path.relative_to(REPO_ROOT).as_posix()
        scanned_calls = _scan_calls(path)
        for scanned in scanned_calls:
            call = scanned.node
            qualified = scanned.qualified
            if scanned.unresolved_authority:
                bad.append(f"{relative}:{call.lineno}:unresolved-authority:{scanned.spelling}")
            if qualified in _AUTHORITY_CALLS and not scanned.direct:
                bad.append(f"{relative}:{call.lineno}:indirect-authority-alias")
            if qualified == "jhin_observability.connect_temporal_client":
                helper_clients.append(relative)
            elif qualified == "jhin_observability.build_temporal_worker":
                helper_workers.append(relative)
            elif qualified == "temporalio.client.Client.connect":
                direct_clients.append(relative)
                if relative not in allowed_direct_clients:
                    bad.append(f"{relative}:{call.lineno}:direct-client")
                elif (scanned.classes, scanned.functions) != allowed_client_scopes[relative]:
                    bad.append(f"{relative}:{call.lineno}:client-scope")
                interceptors = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == "interceptors"),
                    None,
                )
                if not (
                    isinstance(interceptors, ast.Call)
                    and any(
                        nested.node is interceptors
                        and nested.qualified
                        in {
                            "jhin_observability.temporal_client_interceptors",
                            "jhin_observability.temporal.temporal_client_interceptors",
                        }
                        and nested.direct
                        for nested in scanned_calls
                    )
                ):
                    bad.append(f"{relative}:{call.lineno}:client-list")
            elif qualified == "temporalio.worker.Worker":
                direct_workers.append(relative)
                if (
                    relative != "packages/observability/src/jhin_observability/temporal.py"
                    or scanned.classes
                    or scanned.functions != ("build_temporal_worker",)
                ):
                    bad.append(f"{relative}:{call.lineno}:direct-worker")
            if qualified in {
                "jhin_observability.SafeTemporalTracingInterceptor",
                "jhin_observability.TemporalActivityMetricsInterceptor",
                "jhin_observability.temporal.SafeTemporalTracingInterceptor",
                "jhin_observability.temporal.TemporalActivityMetricsInterceptor",
            }:
                interceptor_constructors.append(
                    (relative, scanned.functions[-1] if scanned.functions else "", qualified)
                )
                allowed_builders = (
                    {"temporal_client_interceptors", "temporal_worker_interceptors"}
                    if qualified.endswith("SafeTemporalTracingInterceptor")
                    else {"temporal_worker_interceptors"}
                )
                if (
                    relative != "packages/observability/src/jhin_observability/temporal.py"
                    or scanned.classes
                    or len(scanned.functions) != 1
                    or scanned.functions[0] not in allowed_builders
                ):
                    bad.append(f"{relative}:{call.lineno}:interceptor-scope")
            if qualified == "jhin_db.create_engine":
                engines.append(relative)
                if not _tracer_site_is_exact(
                    relative,
                    scanned,
                    sites=_ENGINE_TRACER_SITES,
                ):
                    bad.append(f"{relative}:{call.lineno}:engine-tracer")
            if qualified in {
                "jhin_events.EventPublisher",
                "jhin_events.publisher.EventPublisher",
            }:
                publishers.append(relative)
                if not _tracer_site_is_exact(
                    relative,
                    scanned,
                    sites=_PUBLISHER_TRACER_SITES,
                ):
                    bad.append(f"{relative}:{call.lineno}:publisher-tracer")
    assert _inventory(helper_clients) == helper_client_owners
    assert _inventory(helper_workers) == helper_worker_owners
    assert _inventory(direct_clients) == dict.fromkeys(allowed_direct_clients, 1)
    assert direct_workers == ["packages/observability/src/jhin_observability/temporal.py"]
    assert sorted(interceptor_constructors) == sorted(
        [
            (
                "packages/observability/src/jhin_observability/temporal.py",
                "temporal_client_interceptors",
                "jhin_observability.temporal.SafeTemporalTracingInterceptor",
            ),
            (
                "packages/observability/src/jhin_observability/temporal.py",
                "temporal_worker_interceptors",
                "jhin_observability.temporal.SafeTemporalTracingInterceptor",
            ),
            (
                "packages/observability/src/jhin_observability/temporal.py",
                "temporal_worker_interceptors",
                "jhin_observability.temporal.TemporalActivityMetricsInterceptor",
            ),
        ]
    )
    assert _inventory(engines) == {
        "apps/api/src/jhin_api/cli/main.py": 1,
        "apps/api/src/jhin_api/main.py": 1,
        "apps/api/src/jhin_api/seed.py": 1,
        "services/agent_worker/src/jhin_agent_worker/resources.py": 1,
        "services/event_worker/src/jhin_event_worker/main.py": 1,
        "services/tool_worker/src/jhin_tool_worker/resources.py": 1,
    }
    assert _inventory(publishers) == {
        "apps/api/src/jhin_api/conversations/router.py": 1,
        "services/agent_worker/src/jhin_agent_worker/resources.py": 1,
        "services/event_worker/src/jhin_event_worker/normalizer.py": 1,
        "services/tool_worker/src/jhin_tool_worker/resources.py": 1,
    }
    assert bad == []


def test_temporal_wiring_audit_rejects_alias_rebinding_and_ad_hoc_lists(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    candidates = (
        "from jhin_observability import connect_temporal_client as connect\n"
        "async def main(settings, runtime):\n await connect(settings, runtime)\n",
        "from jhin_observability import connect_temporal_client as connect\n"
        "connect = object()\n"
        "async def main(settings, runtime):\n await connect(settings, runtime)\n",
        "import temporalio.client as temporal_client\n"
        "async def main(runtime):\n"
        " await temporal_client.Client.connect('x', interceptors=[])\n",
        "import temporalio.client as temporal_client\n"
        "temporal_client = object()\n"
        "async def main(runtime):\n await temporal_client.Client.connect('x')\n",
        "from temporalio.client import Client as TC\n"
        "async def main(runtime):\n await TC.connect('x', interceptors=[])\n",
        "from temporalio.client import Client\n"
        "async def main(Client, runtime):\n"
        " await Client.connect('x', interceptors=[])\n",
        "from jhin_observability import build_temporal_worker\n"
        "def main(client, runtime):\n"
        " helper = build_temporal_worker\n"
        " return helper(client, runtime=runtime, task_queue='x', workflows=[], activities=[])\n",
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def main(runtime):\n"
        " return await Client.connect(\n"
        "  'x', interceptors=not_temporal_client_interceptors(runtime)\n"
        " )\n",
        "from jhin_observability import connect_temporal_client\n"
        "async def main(settings, runtime):\n"
        " helper = (alias := connect_temporal_client)\n"
        " return await alias(settings, runtime)\n",
        "from jhin_observability import connect_temporal_client\n"
        "del connect_temporal_client\n"
        "async def main(settings, runtime):\n"
        " return await connect_temporal_client(settings, runtime)\n",
        "from jhin_observability import connect_temporal_client\n"
        "for connect_temporal_client in (object(),):\n pass\n"
        "async def main(settings, runtime):\n"
        " return await connect_temporal_client(settings, runtime)\n",
        "from jhin_observability import connect_temporal_client\n"
        "with manager() as connect_temporal_client:\n pass\n"
        "async def main(settings, runtime):\n"
        " return await connect_temporal_client(settings, runtime)\n",
        "from jhin_observability import connect_temporal_client\n"
        "try:\n raise RuntimeError\n"
        "except RuntimeError as connect_temporal_client:\n pass\n"
        "async def main(settings, runtime):\n"
        " return await connect_temporal_client(settings, runtime)\n",
        "from jhin_observability import SafeTemporalTracingInterceptor\n"
        "def outer():\n"
        " def temporal_client_interceptors(runtime):\n"
        "  return [SafeTemporalTracingInterceptor(runtime.tracer, role='client')]\n"
        " return temporal_client_interceptors\n",
        "from jhin_observability import SafeTemporalTracingInterceptor\n"
        "class Builders:\n"
        " def temporal_client_interceptors(self, runtime):\n"
        "  return [SafeTemporalTracingInterceptor(runtime.tracer, role='client')]\n",
        "from temporalio.worker import Worker\n"
        "class Builders:\n"
        " def build_temporal_worker(self, client):\n"
        "  return Worker(client, task_queue='x', workflows=[], activities=[])\n",
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime):\n"
        " return (lambda Client: Client.connect(\n"
        "  'x', interceptors=temporal_client_interceptors(runtime)\n"
        " ))(Client)\n",
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime, clients):\n"
        " return [Client.connect(\n"
        "  'x', interceptors=temporal_client_interceptors(runtime)\n"
        " ) for Client in clients]\n",
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime, clients):\n"
        " return {Client.connect(\n"
        "  'x', interceptors=temporal_client_interceptors(runtime)\n"
        " ) for Client in clients}\n",
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime, clients):\n"
        " return (Client.connect(\n"
        "  'x', interceptors=temporal_client_interceptors(runtime)\n"
        " ) for Client in clients)\n",
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime, clients):\n"
        " return {Client: Client.connect(\n"
        "  'x', interceptors=temporal_client_interceptors(runtime)\n"
        " ) for Client in clients}\n",
        "from jhin_observability import connect_temporal_client\n"
        "(*connect_temporal_client,) = values\n"
        "connect_temporal_client(settings, runtime)\n",
    )
    for candidate in candidates:
        source.write_text(candidate, encoding="utf-8")
        calls = _scan_calls(source)
        authority = [call for call in calls if call.qualified in _AUTHORITY_CALLS]
        unresolved = [call for call in calls if call.unresolved_authority]
        indirect = [call for call in authority if not call.direct]
        bad_client_list = [
            call
            for call in authority
            if call.qualified == "temporalio.client.Client.connect"
            and not any(
                nested.node is _keyword(call.node, "interceptors")
                and nested.qualified
                in {
                    "jhin_observability.temporal_client_interceptors",
                    "jhin_observability.temporal.temporal_client_interceptors",
                }
                and nested.direct
                for nested in calls
            )
        ]
        bad_constructor_scope = [
            call
            for call in authority
            if call.qualified
            in {
                "jhin_observability.SafeTemporalTracingInterceptor",
                "jhin_observability.TemporalActivityMetricsInterceptor",
                "jhin_observability.temporal.SafeTemporalTracingInterceptor",
                "jhin_observability.temporal.TemporalActivityMetricsInterceptor",
            }
            and (
                call.classes
                or len(call.functions) != 1
                or call.functions[0]
                not in {
                    "temporal_client_interceptors",
                    "temporal_worker_interceptors",
                }
            )
        ]
        bad_worker_scope = [
            call
            for call in authority
            if call.qualified == "temporalio.worker.Worker"
            and (call.classes or call.functions != ("build_temporal_worker",))
        ]
        assert (
            unresolved or indirect or bad_client_list or bad_constructor_scope or bad_worker_scope
        )

    source.write_text(
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime, clients):\n"
        " [None for Client in clients]\n"
        " return await Client.connect(\n"
        "  'x', interceptors=temporal_client_interceptors(runtime)\n"
        " )\n",
        encoding="utf-8",
    )
    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].qualified == "temporalio.client.Client.connect"
    assert client_calls[0].direct
    assert not client_calls[0].unresolved_authority


def test_temporal_wiring_inventory_rejects_extra_missing_and_untraced_resources(
    tmp_path: Path,
) -> None:
    source = tmp_path / "service.py"
    owner = "services/example/src/example/main.py"

    def helper_inventory(candidate: str) -> dict[str, int]:
        source.write_text(candidate, encoding="utf-8")
        return _inventory(
            [
                owner
                for call in _scan_calls(source)
                if call.qualified == "jhin_observability.connect_temporal_client"
            ]
        )

    exact_helper = (
        "from jhin_observability import connect_temporal_client\n"
        "async def main(settings, runtime):\n"
        " return await connect_temporal_client(settings, runtime)\n"
    )
    missing_helper = "async def main(settings, runtime):\n return None\n"
    extra_helper = (
        "from jhin_observability import connect_temporal_client\n"
        "async def main(settings, runtime):\n"
        " await connect_temporal_client(settings, runtime)\n"
        " return await connect_temporal_client(settings, runtime)\n"
    )
    assert helper_inventory(exact_helper) == {owner: 1}
    assert helper_inventory(missing_helper) != {owner: 1}
    assert helper_inventory(extra_helper) != {owner: 1}

    def sole_call(candidate: str, qualified: str) -> _ScannedCall:
        source.write_text(candidate, encoding="utf-8")
        calls = [call for call in _scan_calls(source) if call.qualified == qualified]
        assert len(calls) == 1
        return calls[0]

    exact_engine = sole_call(
        "from jhin_db import create_engine\n"
        "def acquire(runtime):\n"
        " return create_engine('db', tracer=runtime.tracer)\n",
        "jhin_db.create_engine",
    )
    missing_engine_tracer = sole_call(
        "from jhin_db import create_engine\ndef acquire(runtime):\n return create_engine('db')\n",
        "jhin_db.create_engine",
    )
    expected_runtime_tracer = ("attr", ("name", "runtime"), "tracer")
    assert _tracer_argument_is_exact(
        exact_engine,
        expected_shape=expected_runtime_tracer,
        root_name="runtime",
        expected_origin="parameter",
    )
    assert not _tracer_argument_is_exact(
        missing_engine_tracer,
        expected_shape=expected_runtime_tracer,
        root_name="runtime",
        expected_origin="parameter",
    )

    exact_publisher = sole_call(
        "from jhin_events import EventPublisher\n"
        "def acquire(js, runtime):\n"
        " return EventPublisher(js, tracer=runtime.tracer)\n",
        "jhin_events.EventPublisher",
    )
    missing_publisher_tracer = sole_call(
        "from jhin_events import EventPublisher\n"
        "def acquire(js, runtime):\n return EventPublisher(js)\n",
        "jhin_events.EventPublisher",
    )
    assert _tracer_argument_is_exact(
        exact_publisher,
        expected_shape=expected_runtime_tracer,
        root_name="runtime",
        expected_origin="parameter",
    )
    assert not _tracer_argument_is_exact(
        missing_publisher_tracer,
        expected_shape=expected_runtime_tracer,
        root_name="runtime",
        expected_origin="parameter",
    )


@pytest.mark.parametrize(
    "expression",
    [
        "[(runtime := other) for other in values]",
        "{(runtime := other) for other in values}",
        "{other: (runtime := other) for other in values}",
        "((runtime := other) for other in values)",
    ],
)
def test_temporal_wiring_audit_tracks_comprehension_walrus_in_enclosing_scope(
    expression: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "resources.py"
    source.write_text(
        "from jhin_db import create_engine\n"
        "def acquire(runtime, values):\n"
        f" {expression}\n"
        " return create_engine('db', tracer=runtime.tracer)\n",
        encoding="utf-8",
    )

    engine_calls = [
        call for call in _scan_calls(source) if call.qualified == "jhin_db.create_engine"
    ]
    assert len(engine_calls) == 1
    engine_call = engine_calls[0]
    assert "runtime" in engine_call.rebound_names
    assert not _tracer_argument_is_exact(
        engine_call,
        expected_shape=("attr", ("name", "runtime"), "tracer"),
        root_name="runtime",
        expected_origin="parameter",
    )


@pytest.mark.parametrize(
    "pattern",
    [
        "Client",
        "[*Client]",
        '{"business": _, **Client}',
        '[Holder(value=Client)] | {"client": Holder(value=Client)}',
    ],
)
def test_temporal_wiring_audit_rejects_structural_pattern_capture_aliases(
    pattern: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime, value):\n"
        " match value:\n"
        f"  case {pattern}:\n"
        "   return await Client.connect(\n"
        "    'x', interceptors=temporal_client_interceptors(runtime)\n"
        "   )\n",
        encoding="utf-8",
    )

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].unresolved_authority


def test_temporal_wiring_audit_does_not_bind_match_wildcard(tmp_path: Path) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime, value):\n"
        " match value:\n"
        "  case _:\n"
        "   return await Client.connect(\n"
        "    'x', interceptors=temporal_client_interceptors(runtime)\n"
        "   )\n",
        encoding="utf-8",
    )

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].qualified == "temporalio.client.Client.connect"
    assert client_calls[0].direct
    assert not client_calls[0].unresolved_authority


@pytest.mark.parametrize(
    "candidate",
    [
        (
            "from temporalio.client import Client\n"
            "from jhin_observability import temporal_client_interceptors\n"
            "def mutate():\n"
            " global Client\n"
            " Client = Client\n"
            "async def connect_temporal_client(runtime):\n"
            " mutate()\n"
            " return await Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " )\n"
        ),
        (
            "from temporalio.client import Client\n"
            "from jhin_observability import temporal_client_interceptors\n"
            "async def connect_temporal_client(runtime):\n"
            " mutate()\n"
            " return await Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " )\n"
            "def mutate():\n"
            " global Client\n"
            " Client = Client\n"
        ),
        (
            "from jhin_observability import temporal_client_interceptors\n"
            "async def connect_temporal_client(runtime):\n"
            " from temporalio.client import Client\n"
            " def mutate():\n"
            "  nonlocal Client\n"
            "  Client = Client\n"
            " mutate()\n"
            " return await Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " )\n"
        ),
    ],
)
def test_temporal_wiring_audit_rejects_declared_scope_authority_mutation(
    candidate: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "client.py"
    source.write_text(candidate, encoding="utf-8")

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].unresolved_authority


@pytest.mark.parametrize(
    "candidate",
    [
        (
            "from temporalio.client import Client\n"
            "from jhin_observability import temporal_client_interceptors\n"
            "type Client = object\n"
            "Client.connect('x', interceptors=temporal_client_interceptors(runtime))\n"
        ),
        (
            "from temporalio.client import Client\n"
            "from jhin_observability import temporal_client_interceptors\n"
            "def shadow[Client](runtime):\n"
            " return Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " )\n"
        ),
        (
            "from temporalio.client import Client\n"
            "from jhin_observability import temporal_client_interceptors\n"
            "class Shadow[Client]:\n"
            " def call(self, runtime):\n"
            "  return Client.connect(\n"
            "   'x', interceptors=temporal_client_interceptors(runtime)\n"
            "  )\n"
        ),
        "from temporalio.client import Client\ntype Alias[Client] = Client.connect('x')\n",
        "from temporalio.client import Client\ntype Alias[**Client] = Client.connect('x')\n",
        "from temporalio.client import Client\ntype Alias[*Client] = Client.connect('x')\n",
    ],
)
def test_temporal_wiring_audit_rejects_pep695_authority_shadowing(
    candidate: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "client.py"
    source.write_text(candidate, encoding="utf-8")

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].unresolved_authority


@pytest.mark.parametrize(
    "scoped_declaration",
    [
        "def shadow[Client]():\n return Client.connect('inner')\n",
        "class Shadow[Client]:\n value = Client.connect('inner')\n",
        "type Shadow[Client] = Client.connect('inner')\n",
    ],
)
def test_temporal_wiring_audit_pep695_type_parameters_do_not_leak(
    scoped_declaration: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        f"from temporalio.client import Client\n{scoped_declaration}Client.connect('outer')\n",
        encoding="utf-8",
    )

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 2
    assert client_calls[0].unresolved_authority
    assert client_calls[1].qualified == "temporalio.client.Client.connect"
    assert client_calls[1].direct
    assert not client_calls[1].unresolved_authority


def test_temporal_wiring_audit_rejects_relative_authority_import(tmp_path: Path) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        "from .temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "async def connect_temporal_client(runtime):\n"
        " return await Client.connect(\n"
        "  'x', interceptors=temporal_client_interceptors(runtime)\n"
        " )\n",
        encoding="utf-8",
    )

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].unresolved_authority


@pytest.mark.parametrize(
    "member",
    [
        (
            " async def connect_temporal_client(self, runtime):\n"
            "  return await Client.connect(\n"
            "   'x', interceptors=temporal_client_interceptors(runtime)\n"
            "  )\n"
        ),
        (
            " value = (lambda runtime: Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " ))(object())\n"
        ),
        (
            " value = [Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " ) for _ in ()]\n"
        ),
    ],
)
def test_temporal_wiring_audit_skips_class_namespace_for_nested_code_lookup(
    member: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        "from jhin_observability import temporal_client_interceptors\n"
        "Client = object()\n"
        "class Container:\n"
        " from temporalio.client import Client\n" + member,
        encoding="utf-8",
    )

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].unresolved_authority


@pytest.mark.parametrize(
    "definition",
    [
        (
            "@Client.connect('x', interceptors=temporal_client_interceptors(runtime))\n"
            "def Client():\n pass\n"
        ),
        (
            "def Client(value=Client.connect(\n"
            " 'x', interceptors=temporal_client_interceptors(runtime)\n"
            ")):\n pass\n"
        ),
        (
            "def Client(value: Client.connect(\n"
            " 'x', interceptors=temporal_client_interceptors(runtime)\n"
            ")):\n pass\n"
        ),
        (
            "@Client.connect('x', interceptors=temporal_client_interceptors(runtime))\n"
            "class Client:\n pass\n"
        ),
        (
            "class Client(Client.connect(\n"
            " 'x', interceptors=temporal_client_interceptors(runtime)\n"
            ")):\n pass\n"
        ),
        (
            "class Client(metaclass=Client.connect(\n"
            " 'x', interceptors=temporal_client_interceptors(runtime)\n"
            ")):\n pass\n"
        ),
        (
            "class Client:\n"
            " value = Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " )\n"
        ),
    ],
)
def test_temporal_wiring_audit_rejects_same_name_definition_authority_calls(
    definition: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        f"{definition}",
        encoding="utf-8",
    )

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].qualified == "temporalio.client.Client.connect"
    assert client_calls[0].direct
    assert not client_calls[0].unresolved_authority


@pytest.mark.parametrize(
    "member",
    [
        (
            " def invoke(self, runtime):\n"
            "  return Client.connect(\n"
            "   'x', interceptors=temporal_client_interceptors(runtime)\n"
            "  )\n"
        ),
        (
            " invoke = lambda runtime: Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " )\n"
        ),
        (
            " invocations = (Client.connect(\n"
            "  'x', interceptors=temporal_client_interceptors(runtime)\n"
            " ) for _ in ())\n"
        ),
    ],
)
def test_temporal_wiring_audit_uses_post_class_binding_in_deferred_code(
    member: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        "from temporalio.client import Client\n"
        "from jhin_observability import temporal_client_interceptors\n"
        "class Client:\n" + member,
        encoding="utf-8",
    )

    client_calls = [call for call in _scan_calls(source) if call.spelling == "Client.connect"]
    assert len(client_calls) == 1
    assert client_calls[0].unresolved_authority


@pytest.mark.parametrize(
    "assignment",
    [
        "value: initialize_observability(config)\n",
        "value: initialize_observability(config) = None\n",
        "class Container:\n value: initialize_observability(config)\n",
        "class Container:\n value: initialize_observability(config) = None\n",
    ],
)
def test_temporal_wiring_audit_scans_annotated_assignment_authority_calls(
    assignment: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\n" + assignment,
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _scan_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].qualified == "jhin_observability.initialize_observability"


@pytest.mark.parametrize(
    "body",
    [
        (
            "value: (initialize_observability := replacement) = "
            "initialize_observability(config)\n"
            "initialize_observability(config)\n"
        ),
        (
            "class Container:\n"
            " value: (initialize_observability := replacement) = "
            "initialize_observability(config)\n"
            " initialize_observability(config)\n"
        ),
    ],
)
def test_temporal_wiring_audit_applies_annassign_walrus_after_value(
    body: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\nreplacement = object()\n" + body,
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _scan_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 2
    assert initialize_calls[0].qualified == "jhin_observability.initialize_observability"
    assert initialize_calls[0].direct
    assert initialize_calls[1].unresolved_authority


@pytest.mark.parametrize(
    "binding",
    [
        "def runtime():\n  return None",
        "class runtime:\n  pass",
        "import jhin_observability as runtime",
        (
            "try:\n"
            "  raise ExceptionGroup('x', [RuntimeError()])\n"
            "except* RuntimeError as runtime:\n"
            "  pass"
        ),
        "runtime: object = value",
        "runtime, *rest = values",
    ],
)
def test_tracer_authority_rejects_same_spelled_untrusted_local_root(
    binding: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "resources.py"
    indented_binding = "\n".join(f" {line}" for line in binding.splitlines())
    source.write_text(
        "from jhin_db import create_engine\n"
        "def acquire(value=None, values=()):\n"
        f"{indented_binding}\n"
        " return create_engine('db', tracer=runtime.tracer)\n",
        encoding="utf-8",
    )

    engine_calls = [
        call for call in _scan_calls(source) if call.qualified == "jhin_db.create_engine"
    ]
    assert len(engine_calls) == 1
    assert not _tracer_argument_is_exact(
        engine_calls[0],
        expected_shape=("attr", ("name", "runtime"), "tracer"),
        root_name="runtime",
        expected_origin="parameter",
    )


@pytest.mark.parametrize(
    "hidden_definition",
    [
        "@initialize_observability(config)\ndef child():\n pass\n",
        "def child(value=initialize_observability(config)):\n pass\n",
        "class Child(metaclass=initialize_observability(config)):\n pass\n",
    ],
)
def test_temporal_wiring_audit_scans_definition_time_authority_calls(
    hidden_definition: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\n"
        "initialize_observability(config)\n"
        f"{hidden_definition}",
        encoding="utf-8",
    )

    initialize_calls = [
        call
        for call in _scan_calls(source)
        if call.qualified == "jhin_observability.initialize_observability"
    ]
    assert len(initialize_calls) == 2


def test_temporal_wiring_audit_predeclares_definition_time_walrus(
    tmp_path: Path,
) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from jhin_observability import initialize_observability\n"
        "def outer(config, replacement):\n"
        " initialize_observability(config)\n"
        " @(initialize_observability := replacement)\n"
        " def child():\n"
        "  pass\n",
        encoding="utf-8",
    )

    initialize_calls = [
        call for call in _scan_calls(source) if call.spelling == "initialize_observability"
    ]
    assert len(initialize_calls) == 1
    assert initialize_calls[0].unresolved_authority


def test_tracer_authority_rejects_same_file_scope_relocation(tmp_path: Path) -> None:
    source = tmp_path / "resources.py"

    def engine_call(candidate: str) -> _ScannedCall:
        source.write_text(
            "from jhin_db import create_engine\n" + candidate,
            encoding="utf-8",
        )
        calls = [call for call in _scan_calls(source) if call.qualified == "jhin_db.create_engine"]
        assert len(calls) == 1
        return calls[0]

    exact = engine_call(
        "class Resources:\n"
        " @classmethod\n"
        " def create(cls, runtime):\n"
        "  return create_engine('db', tracer=runtime.tracer)\n"
    )
    relocated = engine_call(
        "def create(runtime):\n return create_engine('db', tracer=runtime.tracer)\n"
    )
    relative = "services/agent_worker/src/jhin_agent_worker/resources.py"
    assert _tracer_site_is_exact(relative, exact, sites=_ENGINE_TRACER_SITES)
    assert not _tracer_site_is_exact(relative, relocated, sites=_ENGINE_TRACER_SITES)


@pytest.mark.parametrize(
    "nested_body",
    [
        ("  return (lambda runtime: create_engine(\n   'db', tracer=runtime.tracer\n  ))(other)\n"),
        ("  return [create_engine('db', tracer=runtime.tracer)\n   for _ in values]\n"),
        (
            "  def nested(runtime):\n"
            "   return create_engine('db', tracer=runtime.tracer)\n"
            "  return nested(other)\n"
        ),
    ],
)
def test_tracer_authority_rejects_nested_site_parameter_laundering(
    nested_body: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "resources.py"
    source.write_text(
        "from jhin_db import create_engine\n"
        "class Resources:\n"
        " @classmethod\n"
        " def create(cls, runtime, other=None, values=()):\n"
        f"{nested_body}",
        encoding="utf-8",
    )
    engine_calls = [
        call for call in _scan_calls(source) if call.qualified == "jhin_db.create_engine"
    ]
    assert len(engine_calls) == 1
    relative = "services/agent_worker/src/jhin_agent_worker/resources.py"
    assert not _tracer_site_is_exact(relative, engine_calls[0], sites=_ENGINE_TRACER_SITES)


class _DisposableEngine:
    def __init__(self, events: list[str], failure: BaseException | None = None) -> None:
        self.events = events
        self.failure = failure

    async def dispose(self) -> None:
        self.events.append("engine.dispose")
        if self.failure is not None:
            raise self.failure


class _DrainableNats:
    def __init__(self, events: list[str], failure: BaseException | None = None) -> None:
        self.events = events
        self.failure = failure

    def jetstream(self) -> object:
        self.events.append("nats.jetstream")
        return object()

    async def drain(self) -> None:
        self.events.append("nats.drain")
        if self.failure is not None:
            raise self.failure


@pytest.mark.asyncio
async def test_agent_resource_factory_is_transactional_at_every_acquisition_boundary() -> None:
    for stage in [
        "engine",
        "session",
        "connect",
        "jetstream",
        "streams",
        "key",
        "crypto",
        "barrier",
        "publisher",
        "dataclass",
        "logger",
    ]:
        for failure_kind in ["error", "base", "cancel"]:
            with pytest.MonkeyPatch.context() as monkeypatch:
                await _run_agent_resource_factory_boundary_case(stage, failure_kind, monkeypatch)


async def _run_agent_resource_factory_boundary_case(
    stage: str,
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = f"stage={stage!r} failure_kind={failure_kind!r}"
    events: list[str] = []
    engine = _DisposableEngine(events)
    connection = _DrainableNats(events)
    failure: BaseException
    if failure_kind == "error":
        failure = RuntimeError(f"{stage}-private-canary")
    elif failure_kind == "base":
        failure = BaseException(f"{stage}-private-canary")
    else:
        failure = asyncio.CancelledError(f"{stage}-private-canary")
    tracer = object()
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(tracer=tracer, metrics=noop_metrics()),
    )
    settings = SimpleNamespace(
        database_url="db",
        nats_url="nats",
        test_crash_barrier_dir=None,
        test_crash_barrier_name=None,
        test_crash_barrier_match=None,
    )
    settings_before = vars(settings).copy()

    def create_engine(_url: str, *, tracer: object) -> _DisposableEngine:
        assert tracer is runtime.tracer, case
        events.append("engine.create")
        if stage == "engine":
            raise failure
        return engine

    def sessions(_engine: object) -> object:
        events.append("session.create")
        if stage == "session":
            raise failure
        return object()

    async def connect(_url: str) -> _DrainableNats:
        events.append("nats.connect")
        if stage == "connect":
            raise failure
        return connection

    original_jetstream = connection.jetstream

    def jetstream() -> object:
        if stage == "jetstream":
            events.append("nats.jetstream")
            raise failure
        return original_jetstream()

    connection.jetstream = jetstream  # type: ignore[method-assign]

    async def streams(_js: object) -> None:
        events.append("streams.ensure")
        if stage == "streams":
            raise failure

    def key() -> bytes:
        events.append("key.load")
        if stage == "key":
            raise failure
        return b"0" * 32

    class Crypto:
        def __init__(self, _key: bytes) -> None:
            events.append("crypto.create")
            if stage == "crypto":
                raise failure

    class Barrier:
        def __init__(self, _config: object) -> None:
            events.append("barrier.create")
            if stage == "barrier":
                raise failure

    class Publisher:
        def __init__(self, _js: object, *, tracer: object) -> None:
            events.append("publisher.create")
            assert tracer is runtime.tracer, case
            if stage == "publisher":
                raise failure

    monkeypatch.setattr(agent_resources, "create_engine", create_engine)
    monkeypatch.setattr(agent_resources, "create_session_factory", sessions)
    monkeypatch.setattr(agent_resources.nats, "connect", connect)
    monkeypatch.setattr(agent_resources, "ensure_streams", streams)
    monkeypatch.setattr(agent_resources, "load_master_key", key)
    monkeypatch.setattr(agent_resources, "SecretCrypto", Crypto)
    monkeypatch.setattr(agent_resources, "CrashBarrier", Barrier)
    monkeypatch.setattr(agent_resources, "EventPublisher", Publisher)
    if stage == "logger":
        monkeypatch.setattr(
            agent_resources.logger,
            "info",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
        )
    if stage == "dataclass":
        monkeypatch.setattr(
            agent_resources.Resources,
            "__init__",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
        )
    caught: BaseException | None = None
    try:
        await agent_resources.Resources.create(cast(Any, settings), runtime=runtime)
    except BaseException as error:
        caught = error
    assert caught is failure, case
    assert vars(settings) == settings_before, case
    assert runtime.tracer is tracer, case
    assert events.count("engine.dispose") == (0 if stage == "engine" else 1), case
    if stage not in {"engine", "session", "connect"}:
        assert events[-2:] == ["nats.drain", "engine.dispose"], case


@pytest.mark.asyncio
async def test_agent_partial_factory_cleanup_cannot_mask_the_acquisition_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    acquisition_error = RuntimeError("publisher-private-canary")
    drain_error = BaseException("drain-cleanup-private-canary")
    dispose_cancellation = asyncio.CancelledError("dispose-cleanup-private-canary")
    engine = _DisposableEngine(events)
    engine.failure = dispose_cancellation
    connection = _DrainableNats(events, drain_error)
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(tracer=object(), metrics=noop_metrics()),
    )
    settings = SimpleNamespace(
        database_url="db",
        nats_url="nats",
        test_crash_barrier_dir=None,
        test_crash_barrier_name=None,
        test_crash_barrier_match=None,
    )

    monkeypatch.setattr(agent_resources, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(agent_resources, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(agent_resources.nats, "connect", lambda _url: _async_result(connection))
    monkeypatch.setattr(agent_resources, "ensure_streams", lambda _js: _async_result(None))
    monkeypatch.setattr(agent_resources, "load_master_key", lambda: b"0" * 32)
    monkeypatch.setattr(
        agent_resources,
        "EventPublisher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(acquisition_error),
    )

    caught: BaseException | None = None
    try:
        await agent_resources.Resources.create(cast(Any, settings), runtime=runtime)
    except BaseException as error:
        caught = error
    assert caught is acquisition_error
    assert events[-2:] == ["nats.drain", "engine.dispose"]


@pytest.mark.asyncio
async def test_agent_resource_graph_retains_runtime_publisher_and_tracer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    engine = _DisposableEngine(events)
    connection = _DrainableNats(events)
    tracer = object()
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(tracer=tracer, metrics=noop_metrics()),
    )
    captured: dict[str, object] = {}
    settings = SimpleNamespace(
        database_url="db",
        nats_url="nats",
        test_crash_barrier_dir=None,
        test_crash_barrier_name=None,
        test_crash_barrier_match=None,
    )

    monkeypatch.setattr(
        agent_resources,
        "create_engine",
        lambda _url, *, tracer: captured.update(engine_tracer=tracer) or engine,
    )
    monkeypatch.setattr(agent_resources, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(agent_resources.nats, "connect", lambda _url: _async_result(connection))
    monkeypatch.setattr(agent_resources, "ensure_streams", lambda _js: _async_result(None))
    monkeypatch.setattr(agent_resources, "load_master_key", lambda: b"0" * 32)

    class Publisher:
        def __init__(self, _js: object, *, tracer: object) -> None:
            captured["publisher_tracer"] = tracer
            self.tracer = tracer

    monkeypatch.setattr(agent_resources, "EventPublisher", Publisher)
    resources = await agent_resources.Resources.create(cast(Any, settings), runtime=runtime)
    assert resources.runtime is runtime
    assert captured == {"engine_tracer": tracer, "publisher_tracer": tracer}
    assert cast(Any, resources.publisher).tracer is tracer
    await resources.close()


@pytest.mark.asyncio
async def test_agent_resources_store_runtime_and_close_all_after_drain_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    drain_error = RuntimeError("drain-private-canary")
    engine = _DisposableEngine(events)
    connection = _DrainableNats(events, drain_error)
    runtime = cast(ObservabilityRuntime, SimpleNamespace(tracer=object()))
    resources = agent_resources.Resources(
        runtime=runtime,
        engine=cast(Any, engine),
        session_factory=cast(Any, object()),
        nats_connection=cast(Any, connection),
        publisher=cast(Any, object()),
        crypto=cast(Any, object()),
        test_barrier=cast(Any, object()),
    )
    with pytest.raises(RuntimeError) as raised:
        await resources.close()
    assert raised.value is drain_error
    assert resources.runtime is runtime
    assert events == ["nats.drain", "engine.dispose"]
