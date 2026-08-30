"""Committed, authority-proven tool activity telemetry contracts."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import contextvars
import importlib
import inspect
import json
import logging
import textwrap
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode, Tracer
from opentelemetry.util.types import Attributes, AttributeValue
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from temporalio.exceptions import ApplicationError

import jhin_tool_worker.activities as activities_module
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    AuditEvent,
    RunEvent,
    Task,
    ToolCall,
    Workspace,
)
from jhin_domain import ApprovalStatus, RunStatus, ToolCallStatus, new_uuid7
from jhin_observability import (
    JhinMetrics,
    ObservabilityConfig,
    ObservabilityRuntime,
    SafeErrorCode,
    noop_metrics,
    noop_tracer,
)
from jhin_observability.metrics import build_jhin_metrics
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tool_worker.activities import ToolActivities
from jhin_tools import (
    ToolCatalog,
    ToolExecutionContext,
    ToolExecutionError,
    ToolGateway,
    stable_tool_invocation_id,
)
from jhin_workflows.agent_task.shared import (
    BoundToolResult,
    ExecuteBoundToolInput,
    ResolveBoundToolApprovalInput,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str = Field(description="private-input-schema-canary")
    private_url: str | None = None
    private_secret: str | None = None
    private_provider_id: str | None = None
    private_connection_id: str | None = None
    private_request_id: str | None = None


class _ToolOutput(BaseModel):
    receipt: str = Field(description="private-output-schema-canary")


@dataclass
class _Telemetry:
    metrics: JhinMetrics
    reader: InMemoryMetricReader
    metric_provider: MeterProvider
    tracer: Tracer
    exporter: InMemorySpanExporter
    trace_provider: TracerProvider


@contextlib.contextmanager
def _fresh_telemetry() -> Iterator[_Telemetry]:
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    resource = Resource.create(
        {
            "service.name": "bounded-tool-worker",
            "private.resource": "private-resource-canary",
        }
    )
    reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=[reader], resource=resource)
    metrics = build_jhin_metrics(metric_provider.get_meter("tool-test-meter"))
    exporter = InMemorySpanExporter()
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(SimpleSpanProcessor(exporter))
    owned = _Telemetry(
        metrics=metrics,
        reader=reader,
        metric_provider=metric_provider,
        tracer=trace_provider.get_tracer("tool-test-tracer"),
        exporter=exporter,
        trace_provider=trace_provider,
    )
    try:
        yield owned
    finally:
        metric_provider.shutdown()
        trace_provider.shutdown()
        assert otel_context.get_current() is entry_context
        assert trace.get_current_span() is entry_span


@pytest.fixture
def telemetry() -> Iterator[_Telemetry]:
    with _fresh_telemetry() as owned:
        yield owned


@contextlib.contextmanager
def _named_case(case_id: str) -> Iterator[None]:
    """Attach the folded-loop case identity to any escaping failure.

    Loop-folded tests iterate the exact former parametrize matrix inside one
    test function; this note makes any failure name its case exactly as the
    old parametrize id did.
    """
    try:
        yield
    except BaseException as error:
        error.add_note(f"[folded case] {case_id}")
        raise


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


def _metric_sum(telemetry: _Telemetry, name: str, **labels: str) -> float:
    return sum(
        float(point.value)
        for point in _metric_points(telemetry, name)
        if dict(point.attributes) == labels
    )


def _metric_point_multiset(
    telemetry: _Telemetry,
) -> list[tuple[str, tuple[tuple[str, object], ...], int | float]]:
    data = telemetry.reader.get_metrics_data()
    if data is None:
        return []
    return sorted(
        (
            metric.name,
            tuple(sorted(dict(point.attributes).items())),
            cast(int | float, point.value),
        )
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        for point in metric.data.data_points
    )


def _tool_spans(telemetry: _Telemetry) -> list[Any]:
    return [
        span
        for span in telemetry.exporter.get_finished_spans()
        if span.name in {"tool.gateway.execute", "tool.approval.resolve"}
    ]


def _point_payload(point: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "attributes": dict(point.attributes),
        "start_time_unix_nano": point.start_time_unix_nano,
        "time_unix_nano": point.time_unix_nano,
    }
    for field in ("value", "count", "sum", "min", "max", "bucket_counts", "explicit_bounds"):
        if hasattr(point, field):
            value = getattr(point, field)
            payload[field] = list(value) if isinstance(value, tuple) else value
    payload["exemplars"] = [
        {
            "filtered_attributes": dict(exemplar.filtered_attributes or {}),
            "value": exemplar.value,
            "time_unix_nano": exemplar.time_unix_nano,
            "span_id": exemplar.span_id,
            "trace_id": exemplar.trace_id,
        }
        for exemplar in getattr(point, "exemplars", ())
    ]
    return payload


def _complete_export_payload(telemetry: _Telemetry) -> str:
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


def _traceback_frame_names(head: TracebackType | None) -> tuple[str, ...]:
    names: list[str] = []
    while head is not None:
        names.append(head.tb_frame.f_code.co_name)
        head = head.tb_next
    return tuple(names)


def _traceback_tail(head: TracebackType | None) -> TracebackType | None:
    while head is not None and head.tb_next is not None:
        head = head.tb_next
    return head


def _application_error_public(error: ApplicationError) -> dict[str, object]:
    return {
        "message": error.message,
        "args": error.args,
        "details": tuple(error.details),
        "type": error.type,
        "non_retryable": error.non_retryable,
        "next_retry_delay": error.next_retry_delay,
        "category": error.category,
        "suppress_context": error.__suppress_context__,
    }


def _exception_chain_shape(
    error: BaseException | None,
    *,
    seen: frozenset[int] = frozenset(),
) -> object:
    if error is None:
        return None
    if id(error) in seen:
        return (type(error).__name__, "<cycle>")
    next_seen = seen | {id(error)}
    return (
        type(error).__name__,
        error.__suppress_context__,
        _exception_chain_shape(error.__cause__, seen=next_seen),
        _exception_chain_shape(error.__context__, seen=next_seen),
    )


class _Resources:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        telemetry: _Telemetry,
    ) -> None:
        self.runtime = SimpleNamespace(metrics=telemetry.metrics, tracer=telemetry.tracer)
        self.session_factory = sessions
        self.crypto = None
        self.test_barrier = None
        self.telemetry = telemetry


class _ProbeSession(AsyncSession):
    fail_activity_commit: BaseException | None = None
    commit_raised_traceback: TracebackType | None = None
    activity_commit_callers: ClassVar[list[str]] = []
    activity_session_ids: ClassVar[list[int]] = []
    created_session_ids: ClassVar[list[int]] = []
    capture_authority_sql: ClassVar[bool] = False
    authority_sql: ClassVar[list[tuple[int, str]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        type(self).created_session_ids.append(id(self))

    async def commit(self) -> None:
        caller = inspect.currentframe()
        caller_name = (
            "" if caller is None or caller.f_back is None else caller.f_back.f_code.co_name
        )
        failure = type(self).fail_activity_commit
        if failure is not None and caller_name in {
            "execute_bound_tool_activity",
            "resolve_bound_tool_approval_activity",
        }:
            type(self).fail_activity_commit = None
            try:
                raise failure
            except BaseException as error:
                type(self).commit_raised_traceback = error.__traceback__
                raise
        if caller_name in {
            "execute_bound_tool_activity",
            "resolve_bound_tool_approval_activity",
        }:
            type(self).activity_commit_callers.append(caller_name)
            type(self).activity_session_ids.append(id(self))
        await super().commit()

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if type(self).capture_authority_sql:
            type(self).authority_sql.append((id(self), str(statement)))
        return await super().execute(statement, *args, **kwargs)


class _ProbeToolActivities(ToolActivities):
    def __init__(self, resources: Any, catalog: ToolCatalog) -> None:
        super().__init__(resources, catalog)
        self.authority_calls: list[dict[str, object]] = []
        self.authority_current_spans: list[object] = []
        self.authority_metric_counts: list[tuple[int, int]] = []
        self.authority_commit_snapshots: list[tuple[str, ...]] = []
        self.authority_fresh_session_deltas: list[int] = []
        self.authority_sql_deltas: list[int] = []
        self.authority_mutator: Callable[[object], object] | None = None
        self.authority_before_load: Callable[[], Awaitable[None]] | None = None
        self.authority_failure: BaseException | None = None
        self.authority_raised_traceback: TracebackType | None = None

    async def _load_tool_telemetry_authority(self, **kwargs: object) -> object:
        self.authority_calls.append(dict(kwargs))
        self.authority_current_spans.append(trace.get_current_span())
        telemetry = cast(_Telemetry, self._resources.telemetry)
        self.authority_metric_counts.append(
            (
                len(_metric_points(telemetry, "tool_calls_total")),
                len(_metric_points(telemetry, "tool_call_failures_total")),
            )
        )
        self.authority_commit_snapshots.append(tuple(_ProbeSession.activity_commit_callers))
        if self.authority_failure is not None:
            try:
                raise self.authority_failure
            except BaseException as error:
                self.authority_raised_traceback = error.__traceback__
                raise
        if self.authority_before_load is not None:
            await self.authority_before_load()
        sessions_before = len(_ProbeSession.created_session_ids)
        sql_before = len(_ProbeSession.authority_sql)
        _ProbeSession.capture_authority_sql = True
        try:
            authority = await super()._load_tool_telemetry_authority(**kwargs)  # type: ignore[misc]
        finally:
            _ProbeSession.capture_authority_sql = False
        self.authority_fresh_session_deltas.append(
            len(_ProbeSession.created_session_ids) - sessions_before
        )
        self.authority_sql_deltas.append(len(_ProbeSession.authority_sql) - sql_before)
        if self.authority_mutator is not None:
            return self.authority_mutator(authority)
        return authority


@dataclass
class ToolWorld:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    resources: _Resources
    catalog: ToolCatalog
    activities: _ProbeToolActivities
    telemetry: _Telemetry
    workspace_id: UUID
    task_id: UUID
    run_id: UUID
    agent_id: UUID
    effects: list[str]
    effect_spans: list[object]
    failure_executor: Callable[[ToolExecutionContext, BaseModel], Awaitable[BaseModel]]

    @property
    def invocation_id(self) -> UUID:
        return stable_tool_invocation_id(self.run_id, 2, 0)

    def execute_params(self) -> ExecuteBoundToolInput:
        return ExecuteBoundToolInput(
            workspace_id=str(self.workspace_id),
            run_id=str(self.run_id),
            step_index=2,
            ordinal=0,
        )

    def approval_params(self, approval_id: UUID | str) -> ResolveBoundToolApprovalInput:
        return ResolveBoundToolApprovalInput(
            workspace_id=str(self.workspace_id),
            task_id=str(self.task_id),
            run_id=str(self.run_id),
            agent_id=str(self.agent_id),
            approval_id=str(approval_id),
        )

    async def seed_manifest(
        self,
        tool_name: str,
        *,
        value: str = "private-input-canary",
        extra: Mapping[str, str] | None = None,
    ) -> None:
        arguments = {"value": value, **dict(extra or {})}
        arguments_json = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self.sessions() as session:
            await session.execute(
                delete(RunEvent).where(
                    RunEvent.run_id == self.run_id,
                    RunEvent.event_type == "agent.step.tool_manifest",
                )
            )
            session.add(
                RunEvent(
                    workspace_id=self.workspace_id,
                    run_id=self.run_id,
                    task_id=self.task_id,
                    seq=0,
                    event_type="agent.step.tool_manifest",
                    payload_json={
                        "private_manifest_canary": "private-manifest-canary",
                        "step": 2,
                        "manifest": {
                            "count": 1,
                            "calls": [
                                {
                                    "ordinal": 0,
                                    "lossless": True,
                                    "tool_name": tool_name,
                                    "arguments_json": arguments_json,
                                }
                            ],
                        },
                    },
                )
            )
            await session.commit()

    async def append_manifest(
        self,
        tool_name: str,
        *,
        step_index: int,
        seq: int,
        value: str = "private-input-canary",
    ) -> None:
        arguments_json = json.dumps(
            {"value": value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self.sessions() as session:
            session.add(
                RunEvent(
                    workspace_id=self.workspace_id,
                    run_id=self.run_id,
                    task_id=self.task_id,
                    seq=seq,
                    event_type="agent.step.tool_manifest",
                    payload_json={
                        "step": step_index,
                        "manifest": {
                            "count": 1,
                            "calls": [
                                {
                                    "ordinal": 0,
                                    "lossless": True,
                                    "tool_name": tool_name,
                                    "arguments_json": arguments_json,
                                }
                            ],
                        },
                    },
                )
            )
            await session.commit()

    async def decide(self, approval_id: UUID | str, status: str) -> None:
        async with self.sessions() as session:
            approval = await session.get(Approval, UUID(str(approval_id)))
            assert approval is not None
            approval.status = status
            approval.decided_at = datetime.now(UTC)
            await session.commit()

    async def park(
        self,
        *,
        value: str = "private-input-canary",
        extra: Mapping[str, str] | None = None,
    ) -> BoundToolResult:
        await self.seed_manifest("system.approval", value=value, extra=extra)
        result = await self.activities.execute_bound_tool_activity(self.execute_params())
        assert result.status == "needs_approval"
        assert result.approval_id is not None
        return result

    async def tool_call(self) -> ToolCall | None:
        async with self.sessions() as session:
            return await session.get(ToolCall, self.invocation_id)

    async def product_snapshot(self) -> dict[str, object]:
        async with self.sessions() as session:
            row = await session.get(ToolCall, self.invocation_id)
            run = await session.get(AgentRun, self.run_id)
            manifest_event = await session.scalar(
                select(RunEvent).where(
                    RunEvent.workspace_id == self.workspace_id,
                    RunEvent.run_id == self.run_id,
                    RunEvent.event_type == "agent.step.tool_manifest",
                )
            )
            approval = (
                None
                if row is None or row.approval_id is None
                else await session.get(Approval, row.approval_id)
            )
            audits = list(
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.workspace_id == self.workspace_id)
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                )
            )
            return deepcopy(
                {
                    "tool_call": None
                    if row is None
                    else {
                        column.name: getattr(row, column.name) for column in row.__table__.columns
                    },
                    "agent_run": None
                    if run is None
                    else {
                        column.name: getattr(run, column.name) for column in run.__table__.columns
                    },
                    "manifest_event": None
                    if manifest_event is None
                    else {
                        column.name: getattr(manifest_event, column.name)
                        for column in manifest_event.__table__.columns
                    },
                    "approval": None
                    if approval is None
                    else {
                        column.name: getattr(approval, column.name)
                        for column in approval.__table__.columns
                    },
                    "audits": [
                        {
                            "action": audit.action,
                            "target_type": audit.target_type,
                            "target_id": audit.target_id,
                            "metadata_json": audit.metadata_json,
                        }
                        for audit in audits
                    ],
                    "effects": list(self.effects),
                }
            )

    async def clone_isolated(self) -> ToolWorld:
        async with self.sessions() as session:
            workspace = Workspace(
                name="Private control workspace canary",
                slug=f"tool-control-{new_uuid7().hex[:8]}",
            )
            session.add(workspace)
            await session.flush()
            agent = Agent(
                workspace_id=workspace.id,
                name="Private control agent canary",
                slug="tool-control-agent",
            )
            session.add(agent)
            await session.flush()
            task = Task(
                workspace_id=workspace.id,
                title="Private control task canary",
                assigned_agent_id=agent.id,
                correlation_id=new_uuid7(),
            )
            session.add(task)
            await session.flush()
            run = AgentRun(
                workspace_id=workspace.id,
                agent_id=agent.id,
                task_id=task.id,
                status=RunStatus.RUNNING.value,
            )
            session.add(run)
            for capability in ("system.echo", "system.fail", "system.approval"):
                session.add(
                    AgentCapabilityGrant(
                        workspace_id=workspace.id,
                        agent_id=agent.id,
                        capability=capability,
                        scope_json={},
                        effect="allow",
                    )
                )
            await session.commit()

        clone = ToolWorld(
            engine=self.engine,
            sessions=self.sessions,
            resources=self.resources,
            catalog=self.catalog,
            activities=_ProbeToolActivities(self.resources, self.catalog),
            telemetry=self.telemetry,
            workspace_id=workspace.id,
            task_id=task.id,
            run_id=run.id,
            agent_id=agent.id,
            effects=self.effects,
            effect_spans=self.effect_spans,
            failure_executor=self.failure_executor,
        )
        await clone.seed_manifest("system.echo")
        return clone

    def reset_diagnostics(self) -> None:
        self.telemetry.exporter.clear()
        self.activities.authority_calls.clear()
        self.activities.authority_current_spans.clear()
        self.activities.authority_metric_counts.clear()
        self.activities.authority_commit_snapshots.clear()
        self.activities.authority_fresh_session_deltas.clear()
        self.activities.authority_sql_deltas.clear()


@contextlib.asynccontextmanager
async def _fresh_world(telemetry: _Telemetry) -> AsyncIterator[ToolWorld]:
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=_ProbeSession, expire_on_commit=False)
    effects: list[str] = []
    effect_spans: list[object] = []

    async def execute_success(_context: ToolExecutionContext, payload: BaseModel) -> BaseModel:
        parsed = _ToolInput.model_validate(payload.model_dump())
        effect_spans.append(trace.get_current_span())
        effects.append(parsed.value)
        return _ToolOutput(receipt=f"private-output-{parsed.value}")

    async def execute_failure(_context: ToolExecutionContext, payload: BaseModel) -> BaseModel:
        _ToolInput.model_validate(payload.model_dump())
        raise ToolExecutionError(
            "private-executor-message-canary",
            code="private_executor_code",
            side_effect_possible=False,
        )

    catalog = ToolCatalog()
    for name, risk, executor, supports_approval in (
        ("system.echo", RiskLevel.WRITE, execute_success, False),
        ("system.fail", RiskLevel.READ, execute_failure, False),
        ("system.approval", RiskLevel.ELEVATED, execute_success, True),
    ):
        catalog.register(
            ToolDefinition(
                name=name,
                description=f"private-description-{name}",
                risk=risk,
                input_model=_ToolInput,
                output_model=_ToolOutput,
                required_capability=name,
                supports_approval=supports_approval,
            ),
            executor,
        )

    async with sessions() as session:
        workspace = Workspace(name="Private workspace canary", slug=f"tool-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Private agent canary", slug="tool-agent")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Private task canary",
            assigned_agent_id=agent.id,
            correlation_id=new_uuid7(),
        )
        session.add(task)
        await session.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.RUNNING.value,
        )
        session.add(run)
        for capability in ("system.echo", "system.fail", "system.approval"):
            session.add(
                AgentCapabilityGrant(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    capability=capability,
                    scope_json={},
                    effect="allow",
                )
            )
        await session.commit()

    resources = _Resources(sessions, telemetry)
    activities = _ProbeToolActivities(resources, catalog)
    owned = ToolWorld(
        engine=engine,
        sessions=sessions,
        resources=resources,
        catalog=catalog,
        activities=activities,
        telemetry=telemetry,
        workspace_id=workspace.id,
        task_id=task.id,
        run_id=run.id,
        agent_id=agent.id,
        effects=effects,
        effect_spans=effect_spans,
        failure_executor=execute_failure,
    )
    await owned.seed_manifest("system.echo")
    try:
        yield owned
    finally:
        _ProbeSession.fail_activity_commit = None
        _ProbeSession.commit_raised_traceback = None
        _ProbeSession.activity_commit_callers.clear()
        _ProbeSession.activity_session_ids.clear()
        _ProbeSession.created_session_ids.clear()
        _ProbeSession.capture_authority_sql = False
        _ProbeSession.authority_sql.clear()
        await engine.dispose()
        assert otel_context.get_current() is entry_context
        assert trace.get_current_span() is entry_span


@pytest.fixture
async def world(telemetry: _Telemetry) -> AsyncIterator[ToolWorld]:
    async with _fresh_world(telemetry) as owned:
        yield owned


@contextlib.asynccontextmanager
async def _fresh_world_context() -> AsyncIterator[ToolWorld]:
    """One fully isolated telemetry+world pair, exactly as the fixtures build it.

    Used by loop-folded tests so every iteration of a former parametrize matrix
    gets the same per-case isolation (fresh DB, fresh telemetry providers,
    fresh probe state) that a separate collected item used to get.
    """
    with _fresh_telemetry() as telemetry:
        async with _fresh_world(telemetry) as owned:
            yield owned


_RESULT_APPROVAL_AUTHORITY_UNSET = object()


def _canonical_product_snapshot(
    world: ToolWorld,
    snapshot: dict[str, object],
    *,
    result_approval_authority: object = _RESULT_APPROVAL_AUTHORITY_UNSET,
) -> object:
    dynamic_ids = {
        str(world.workspace_id): "<workspace_id>",
        str(world.task_id): "<task_id>",
        str(world.run_id): "<run_id>",
        str(world.agent_id): "<agent_id>",
        str(world.invocation_id): "<tool_call_id>",
    }
    approval = snapshot.get("approval")
    if isinstance(approval, dict) and approval.get("id") is not None:
        dynamic_ids[str(approval["id"])] = "<approval_id>"
    manifest_event = snapshot.get("manifest_event")
    if isinstance(manifest_event, dict) and manifest_event.get("id") is not None:
        dynamic_ids[str(manifest_event["id"])] = "<manifest_event_id>"

    normalized_datetime_paths = frozenset(
        {
            ("tool_call", "created_at"),
            ("tool_call", "started_at"),
            ("tool_call", "completed_at"),
            ("agent_run", "created_at"),
            ("agent_run", "updated_at"),
            ("agent_run", "started_at"),
            ("agent_run", "completed_at"),
            ("manifest_event", "created_at"),
            ("approval", "created_at"),
            ("approval", "updated_at"),
            ("approval", "requested_at"),
            ("approval", "decided_at"),
        }
    )
    nullable_datetime_paths = frozenset(
        {
            ("tool_call", "started_at"),
            ("tool_call", "completed_at"),
            ("agent_run", "started_at"),
            ("agent_run", "completed_at"),
            ("approval", "decided_at"),
        }
    )

    def normalize(value: object, *, path: tuple[str, ...] = ()) -> object:
        if path == ("effects",):
            return "<asserted-separately>"
        if path == ("tool_call", "duration_ms"):
            assert value is None or type(value) is int
            return "<duration_ms>"
        if path in normalized_datetime_paths:
            if value is None and path in nullable_datetime_paths:
                return None
            if type(value) is datetime:
                return (
                    "<datetime>",
                    "aware" if value.tzinfo is not None else "naive",
                )
            return value
        if path == ("result", "approval_id") and (
            result_approval_authority is not _RESULT_APPROVAL_AUTHORITY_UNSET
        ):
            if result_approval_authority is None:
                assert value is None
                return None
            assert type(result_approval_authority) is str
            assert str(UUID(result_approval_authority)) == result_approval_authority
            assert type(value) is str
            assert str(UUID(value)) == value
            assert value == result_approval_authority
            return "<approval_id>"
        if isinstance(value, UUID):
            return dynamic_ids.get(str(value), str(value))
        if isinstance(value, dict):
            return {
                str(item_key): normalize(item, path=(*path, str(item_key)))
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [normalize(item, path=(*path, "[]")) for item in value]
        if isinstance(value, tuple):
            return tuple(normalize(item, path=(*path, "[]")) for item in value)
        if isinstance(value, str):
            normalized = value
            for identifier, placeholder in dynamic_ids.items():
                normalized = normalized.replace(identifier, placeholder)
            return normalized
        return value

    return normalize(snapshot)


def _bound_result_approval_authority(
    result: BoundToolResult,
    parked: BoundToolResult | None,
    product_snapshot: dict[str, object],
) -> str | None:
    tool_call = product_snapshot["tool_call"]
    assert type(tool_call) is dict
    approval = product_snapshot["approval"]
    if approval is None:
        assert parked is None or parked.approval_id is None
        assert result.approval_id is None
        assert tool_call["approval_id"] is None
        return None

    assert type(approval) is dict
    persisted_id = approval["id"]
    assert type(persisted_id) is UUID
    expected = str(persisted_id)
    assert str(UUID(expected)) == expected
    if parked is not None:
        assert type(parked.approval_id) is str
        assert parked.approval_id == expected
    assert type(result.approval_id) is str
    assert result.approval_id == expected
    assert tool_call["approval_id"] == persisted_id
    return expected


class _DateTimeSubclass(datetime):
    pass


def _canonical_snapshot_probe(
    *, started_at: object, include_started_at: bool = True
) -> dict[str, object]:
    tool_call: dict[str, object] = {
        "id": UUID("00000000-0000-0000-0000-000000000005"),
        "created_at": datetime(2026, 1, 1),
    }
    if include_started_at:
        tool_call["started_at"] = started_at
    return {
        "tool_call": tool_call,
        "agent_run": None,
        "manifest_event": None,
        "approval": None,
        "audits": [],
        "effects": [],
    }


def test_canonical_product_snapshot_normalizes_only_exact_reviewed_datetimes() -> None:
    world = cast(
        ToolWorld,
        SimpleNamespace(
            workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
            task_id=UUID("00000000-0000-0000-0000-000000000002"),
            run_id=UUID("00000000-0000-0000-0000-000000000003"),
            agent_id=UUID("00000000-0000-0000-0000-000000000004"),
            invocation_id=UUID("00000000-0000-0000-0000-000000000005"),
        ),
    )
    first = _canonical_snapshot_probe(started_at=datetime(2026, 1, 2, 3, 4, 5))
    second = _canonical_snapshot_probe(started_at=datetime(2030, 6, 7, 8, 9, 10))
    assert _canonical_product_snapshot(world, first) == _canonical_product_snapshot(
        world,
        second,
    )

    variants = [
        _canonical_snapshot_probe(started_at=datetime(2026, 1, 2, tzinfo=UTC)),
        _canonical_snapshot_probe(started_at=_DateTimeSubclass(2026, 1, 2)),
        _canonical_snapshot_probe(started_at="2026-01-02T00:00:00"),
        _canonical_snapshot_probe(started_at=7),
        _canonical_snapshot_probe(started_at=None),
        _canonical_snapshot_probe(started_at=datetime(2026, 1, 2), include_started_at=False),
        {
            **_canonical_snapshot_probe(started_at=datetime(2026, 1, 2)),
            "unexpected": datetime(2026, 1, 2),
        },
    ]
    canonical_first = _canonical_product_snapshot(world, first)
    assert all(
        _canonical_product_snapshot(world, variant) != canonical_first for variant in variants
    )

    nullable = _canonical_snapshot_probe(started_at=None)
    assert _canonical_product_snapshot(world, nullable) == _canonical_product_snapshot(
        world,
        _canonical_snapshot_probe(started_at=None),
    )

    malformed_a = _canonical_snapshot_probe(started_at="a")
    malformed_b = _canonical_snapshot_probe(started_at="b")
    assert _canonical_product_snapshot(world, malformed_a) != _canonical_product_snapshot(
        world,
        malformed_b,
    )

    nested_a = _canonical_snapshot_probe(started_at=datetime(2026, 1, 2))
    nested_b = deepcopy(nested_a)
    nested_a["audits"] = [{"metadata_json": {"created_at": datetime(2026, 1, 3)}}]
    nested_b["audits"] = [{"metadata_json": {"created_at": datetime(2026, 1, 4)}}]
    assert _canonical_product_snapshot(world, nested_a) != _canonical_product_snapshot(
        world,
        nested_b,
    )


class _StringSubclass(str):
    pass


def test_result_approval_normalization_is_narrow_exact_and_authority_bound() -> None:
    world = cast(
        ToolWorld,
        SimpleNamespace(
            workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
            task_id=UUID("00000000-0000-0000-0000-000000000002"),
            run_id=UUID("00000000-0000-0000-0000-000000000003"),
            agent_id=UUID("00000000-0000-0000-0000-000000000004"),
            invocation_id=UUID("00000000-0000-0000-0000-000000000005"),
        ),
    )
    first = "00000000-0000-0000-0000-000000000006"
    second = "00000000-0000-0000-0000-000000000007"
    assert _canonical_product_snapshot(
        world,
        {"result": {"approval_id": first}, "metadata": {"approval_id": "unchanged"}},
        result_approval_authority=first,
    ) == _canonical_product_snapshot(
        world,
        {"result": {"approval_id": second}, "metadata": {"approval_id": "unchanged"}},
        result_approval_authority=second,
    )
    assert _canonical_product_snapshot(
        world,
        {"result": {"approval_id": None}, "metadata": {"approval_id": first}},
        result_approval_authority=None,
    ) == {"result": {"approval_id": None}, "metadata": {"approval_id": first}}

    for invalid in (
        "not-a-uuid",
        _StringSubclass(first),
        second,
    ):
        with pytest.raises((AssertionError, ValueError)):
            _canonical_product_snapshot(
                world,
                {"result": {"approval_id": invalid}},
                result_approval_authority=first,
            )


async def _prepare_terminal_case(
    world: ToolWorld,
    *,
    activity_kind: str,
    product_kind: str,
) -> BoundToolResult | None:
    if activity_kind == "execute":
        assert product_kind in {"success", "failure", "cancel"}
        await world.seed_manifest(
            "system.echo" if product_kind in {"success", "cancel"} else "system.fail"
        )
        world.reset_diagnostics()
        return None
    parked = await world.park()
    await world.decide(
        parked.approval_id or "",
        ApprovalStatus.APPROVED.value
        if product_kind in {"success", "cancel", "denied", "failed"}
        else ApprovalStatus.REJECTED.value,
    )
    if product_kind == "denied":
        async with world.sessions() as session:
            await session.execute(
                delete(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == world.agent_id,
                    AgentCapabilityGrant.capability == "system.approval",
                )
            )
            await session.commit()
    elif product_kind == "failed":
        world.catalog._executors["system.approval"] = world.failure_executor
    world.reset_diagnostics()
    return parked


async def _invoke_terminal_case(
    world: ToolWorld,
    *,
    activity_kind: str,
    product_kind: str,
    parked: BoundToolResult | None,
) -> tuple[BoundToolResult | None, ApplicationError | None, tuple[tuple[str, str, int], ...]]:
    try:
        if activity_kind == "execute":
            result = await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            result = await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )
    except ApplicationError as error:
        frames: list[tuple[str, str, int]] = []
        traceback = error.__traceback__
        while traceback is not None:
            frames.append(
                (
                    Path(traceback.tb_frame.f_code.co_filename).name,
                    traceback.tb_frame.f_code.co_name,
                    traceback.tb_lineno,
                )
            )
            traceback = traceback.tb_next
        return None, error, tuple(frames[1:])
    expected_status = {
        "success": "executed",
        "failure": "rejected",
        "denied": "denied",
        "failed": "failed",
    }[product_kind]
    assert result.status == expected_status
    return result, None, ()


async def _invoke_terminal_activity_raw(
    world: ToolWorld,
    *,
    activity_kind: str,
    parked: BoundToolResult | None,
) -> BoundToolResult:
    if activity_kind == "execute":
        return await world.activities.execute_bound_tool_activity(world.execute_params())
    assert parked is not None
    return await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id or "")
    )


class _FalseyHandle:
    def __bool__(self) -> bool:
        return False


class _PreproductBackendTrap:
    def __init__(self) -> None:
        self.accesses: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.accesses.append(name)
        raise AssertionError(f"telemetry backend touched before schema validation: {name}")


class _BoolExplodingHandle:
    def __init__(self) -> None:
        self.bool_calls = 0

    def __bool__(self) -> bool:
        self.bool_calls += 1
        raise AssertionError("owned runtime handles must never be truth-tested")


def test_constructor_binds_exact_runtime_handles_even_when_falsey() -> None:
    metrics = _FalseyHandle()
    tracer = _FalseyHandle()
    resources = SimpleNamespace(runtime=SimpleNamespace(metrics=metrics, tracer=tracer))
    catalog = ToolCatalog()

    activities = ToolActivities(cast(Any, resources), catalog)

    assert activities._metrics is metrics
    assert activities._tracer is tracer
    assert activities._resources is resources
    assert activities._catalog is catalog


def test_constructor_binds_bool_hostile_handles_without_inspection() -> None:
    metrics = _BoolExplodingHandle()
    tracer = _BoolExplodingHandle()
    resources = SimpleNamespace(runtime=SimpleNamespace(metrics=metrics, tracer=tracer))

    activities = ToolActivities(cast(Any, resources), ToolCatalog())

    assert activities._metrics is metrics
    assert activities._tracer is tracer
    assert metrics.bool_calls == 0
    assert tracer.bool_calls == 0


def test_constructor_binds_one_real_runtime_graph_by_exact_identity(
    telemetry: _Telemetry,
) -> None:
    runtime = ObservabilityRuntime(
        config=ObservabilityConfig(
            service_name="tool-worker",
            service_version="test",
            environment="test",
        ),
        tracer=telemetry.tracer,
        meter=telemetry.metric_provider.get_meter("tool-runtime-identity"),
        metrics=telemetry.metrics,
        _diagnostics=cast(Any, object()),
        _owns_providers=False,
    )
    resources = SimpleNamespace(runtime=runtime)

    activities = ToolActivities(cast(Any, resources), ToolCatalog())

    assert activities._metrics is runtime.metrics
    assert activities._tracer is runtime.tracer
    assert activities._resources.runtime is runtime


_PACKAGE_OWNED_TOOL_SCHEMA_CONSTANTS = frozenset(
    {
        "_TOOL_ROW_COMPLETED",
        "_TOOL_ROW_FAILED",
        "_TOOL_ROW_DENIED",
        "_TOOL_ROW_REJECTED",
        "_TOOL_ROW_EXECUTION_UNKNOWN",
        "_TOOL_ROW_PENDING_APPROVAL",
        "_TOOL_OUTCOME_COMPLETED",
        "_TOOL_OUTCOME_ACCEPTED",
        "_TOOL_OUTCOME_FAILED",
        "_TOOL_OUTCOME_DENIED",
        "_TOOL_OUTCOME_REJECTED",
        "_TOOL_OUTCOME_EXECUTION_UNKNOWN",
        "_TOOL_OUTCOME_OTHER",
        "_TOOL_FAILURE_INTERNAL",
        "_TOOL_FAILURE_POLICY",
        "_TOOL_FAILURE_EXECUTION_UNKNOWN",
    }
)
_ACTIVITY_OWNED_TOOL_SCHEMA_CONSTANTS = frozenset(
    {
        "_TOOL_EXECUTE_SPAN_NAME",
        "_TOOL_APPROVAL_SPAN_NAME",
        "_TOOL_FAMILY_ATTRIBUTE",
        "_TOOL_RISK_ATTRIBUTE",
        "_TOOL_OUTCOME_ATTRIBUTE",
        "_TOOL_CALLS_METRIC",
        "_TOOL_FAILURES_METRIC",
        "_TOOL_FAMILY_LABEL",
        "_TOOL_RISK_LABEL",
        "_TOOL_OUTCOME_LABEL",
        "_TOOL_FAILURE_LABEL",
        "_TOOL_MEASUREMENT",
        "_TOOL_OUTCOME_CANCELLED",
        "_TOOL_ERROR_TYPE_ATTRIBUTE",
        "_TOOL_ERROR_CODE_ATTRIBUTE",
        "_TOOL_ERROR_TYPE_VALUE",
        "_TOOL_INTERNAL_ERROR_CODE",
        "_TOOL_POLICY_ERROR_CODE",
        "_TOOL_EXECUTION_UNKNOWN_ERROR_CODE",
    }
)


@pytest.mark.parametrize(
    "resources",
    [
        SimpleNamespace(),
        SimpleNamespace(runtime=None),
        SimpleNamespace(runtime=SimpleNamespace(tracer=object())),
        SimpleNamespace(runtime=SimpleNamespace(metrics=object())),
    ],
)
def test_constructor_rejects_missing_owned_runtime_handles(resources: object) -> None:
    with pytest.raises((AttributeError, TypeError, ValueError)):
        ToolActivities(cast(Any, resources), ToolCatalog())


def test_constructor_has_only_exact_owned_handle_bindings_and_no_fallback() -> None:
    source = inspect.getsource(ToolActivities.__init__)
    function = cast(ast.FunctionDef, ast.parse(textwrap.dedent(source)).body[0])
    assert len(function.body) == 4
    expected = (
        ("_resources", "resources"),
        ("_catalog", "catalog"),
        ("_metrics", "resources.runtime.metrics"),
        ("_tracer", "resources.runtime.tracer"),
    )
    for statement, (attribute, expression) in zip(function.body, expected, strict=True):
        assert isinstance(statement, ast.Assign)
        assert len(statement.targets) == 1
        target = statement.targets[0]
        assert isinstance(target, ast.Attribute)
        assert isinstance(target.value, ast.Name)
        assert (target.value.id, target.attr) == ("self", attribute)
        assert ast.unparse(statement.value) == expression

    forbidden_calls = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert (
        not {
            "getattr",
            "get_runtime",
            "noop_metrics",
            "noop_tracer",
            "init_observability",
        }
        & forbidden_calls
    )
    module_tree = ast.parse(Path(inspect.getsourcefile(ToolActivities) or "").read_text())
    forbidden_runtime_symbols = {
        "get_runtime",
        "noop_metrics",
        "noop_tracer",
        "init_observability",
        "initialize_observability",
    }
    for node in ast.walk(module_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "jhin_observability":
            assert not forbidden_runtime_symbols & {alias.name for alias in node.names}
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_runtime_symbols
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_runtime_symbols


@pytest.mark.parametrize(
    ("case", "span_name", "family", "risk", "outcome", "failure_class"),
    [
        ("executed", "tool.gateway.execute", "system", "write", "completed", None),
        ("failed", "tool.gateway.execute", "system", "read", "failed", "internal"),
        ("denied", "tool.gateway.execute", "other", "other", "denied", "policy"),
        ("needs_approval", "tool.gateway.execute", "system", "elevated", "accepted", None),
        ("rejected", "tool.approval.resolve", "system", "elevated", "rejected", "policy"),
        (
            "execution_unknown",
            "tool.approval.resolve",
            "system",
            "elevated",
            "execution_unknown",
            "execution_unknown",
        ),
    ],
)
async def test_real_outcome_status_table_records_only_owned_terminal_transition(
    world: ToolWorld,
    case: str,
    span_name: str,
    family: str,
    risk: str,
    outcome: str,
    failure_class: str | None,
) -> None:
    public_result: BoundToolResult | ApplicationError
    if case == "executed":
        public_result = await world.activities.execute_bound_tool_activity(world.execute_params())
        assert public_result.status == "executed"
    elif case == "failed":
        await world.seed_manifest("system.fail")
        with pytest.raises(ApplicationError) as caught:
            await world.activities.execute_bound_tool_activity(world.execute_params())
        public_result = caught.value
        assert public_result.type == "private_executor_code"
    elif case == "denied":
        await world.seed_manifest("private.unknown-tool")
        with pytest.raises(ApplicationError) as caught:
            await world.activities.execute_bound_tool_activity(world.execute_params())
        public_result = caught.value
        assert public_result.type == "tool_not_found"
    else:
        parked = await world.park()
        if case == "needs_approval":
            public_result = parked
            assert public_result.status == "needs_approval"
        else:
            await world.decide(
                parked.approval_id or "",
                ApprovalStatus.REJECTED.value
                if case == "rejected"
                else ApprovalStatus.APPROVED.value,
            )
            if case == "execution_unknown":
                async with world.sessions() as session:
                    row = await session.get(ToolCall, world.invocation_id)
                    assert row is not None
                    row.status = ToolCallStatus.EXECUTING.value
                    await session.commit()
            world.reset_diagnostics()
            public_result = await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )
            assert public_result.status == case

    spans = [span for span in _tool_spans(world.telemetry) if span.name == span_name]
    assert len(spans) == 1
    span = spans[0]
    expected_attributes = {
        "jhin.tool_family": family,
        "jhin.risk": risk,
        "jhin.outcome": outcome,
    }
    assert dict(span.attributes or {}) == expected_attributes or (
        failure_class is not None
        and {
            key: value
            for key, value in dict(span.attributes or {}).items()
            if not key.startswith("error.")
        }
        == expected_attributes
    )
    if failure_class is None:
        assert span.status.status_code is StatusCode.UNSET
        assert "error.code" not in dict(span.attributes or {})
    else:
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description is None
        assert span.attributes["error.type"] == "ToolActivityError"
        assert (
            span.attributes["error.code"]
            == {
                "internal": "internal_error",
                "policy": "authorization_failed",
                "execution_unknown": "execution_unknown",
            }[failure_class]
        )
        assert set(dict(span.attributes or {})) <= {
            "jhin.tool_family",
            "jhin.risk",
            "jhin.outcome",
            "error.type",
            "error.code",
        }

    terminal = case != "needs_approval"
    assert len(_metric_points(world.telemetry, "tool_calls_total")) == int(terminal)
    if terminal:
        assert (
            _metric_sum(
                world.telemetry,
                "tool_calls_total",
                tool_family=family,
                risk=risk,
                outcome=outcome,
            )
            == 1
        )
    assert len(_metric_points(world.telemetry, "tool_call_failures_total")) == int(
        failure_class is not None
    )
    if failure_class is not None:
        assert (
            _metric_sum(
                world.telemetry,
                "tool_call_failures_total",
                tool_family=family,
                failure_class=failure_class,
            )
            == 1
        )
    assert len(world.activities.authority_calls) == 1
    assert world.activities.authority_metric_counts == [(0, 0)]
    expected_commit_owner = (
        "resolve_bound_tool_approval_activity"
        if span_name == "tool.approval.resolve"
        else "execute_bound_tool_activity"
    )
    assert world.activities.authority_commit_snapshots
    assert world.activities.authority_commit_snapshots[-1][-1] == expected_commit_owner
    assert world.activities.authority_fresh_session_deltas == [1]
    assert len(world.activities.authority_sql_deltas) == 1
    assert world.activities.authority_sql_deltas[0] >= 1
    assert set(_ProbeSession.activity_session_ids).isdisjoint(
        {session_id for session_id, _statement in _ProbeSession.authority_sql}
    )
    authority_sql = "\n".join(
        statement.lower() for _session_id, statement in _ProbeSession.authority_sql
    )
    assert "tool_call" in authority_sql
    assert "agent_run" in authority_sql
    assert "run_event" in authority_sql
    if span_name == "tool.approval.resolve":
        assert "approval" in authority_sql

    expected_points = []
    if terminal:
        expected_points.append(
            (
                "tool_calls_total",
                tuple(
                    sorted(
                        {
                            "tool_family": family,
                            "risk": risk,
                            "outcome": outcome,
                        }.items()
                    )
                ),
                1,
            )
        )
    if failure_class is not None:
        expected_points.append(
            (
                "tool_call_failures_total",
                tuple(
                    sorted(
                        {
                            "tool_family": family,
                            "failure_class": failure_class,
                        }.items()
                    )
                ),
                1,
            )
        )
    assert _metric_point_multiset(world.telemetry) == sorted(expected_points)


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_postcommit_proof_reads_fresh_sql_not_the_gateway_outcome_or_activity_session(
    world: ToolWorld,
    activity_kind: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    activity_sessions_before = tuple(_ProbeSession.activity_session_ids)

    async def mutate_after_product_commit() -> None:
        async with world.sessions() as session:
            row = await session.get(ToolCall, world.invocation_id)
            assert row is not None
            assert row.status == ToolCallStatus.COMPLETED.value
            row.status = ToolCallStatus.FAILED.value
            await session.commit()

    world.activities.authority_before_load = mutate_after_product_commit

    result, error, _frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )

    assert error is None
    assert result is not None
    assert result.status == "executed"
    row = await world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.FAILED.value
    assert _metric_point_multiset(world.telemetry) == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }
    assert spans[0].status.status_code is StatusCode.UNSET
    assert spans[0].events == ()
    assert world.activities.authority_fresh_session_deltas == [1]
    assert len(world.activities.authority_sql_deltas) == 1
    assert world.activities.authority_sql_deltas[0] >= 1
    assert tuple(_ProbeSession.activity_session_ids[: len(activity_sessions_before)]) == (
        activity_sessions_before
    )
    query_session_ids = {session_id for session_id, _statement in _ProbeSession.authority_sql}
    assert query_session_ids
    assert query_session_ids.isdisjoint(set(_ProbeSession.activity_session_ids))


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_fresh_proof_selects_only_the_requested_manifest_step(
    world: ToolWorld,
    activity_kind: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    tool_name = "system.echo" if activity_kind == "execute" else "system.approval"
    await world.append_manifest(
        tool_name,
        step_index=3,
        seq=3,
        value="unrelated-other-step-input",
    )

    result, error, frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )

    assert error is None
    assert frames == ()
    assert result is not None
    assert result.status == "executed"
    assert world.effects == ["private-input-canary"]
    assert _metric_point_multiset(world.telemetry) == [
        (
            "tool_calls_total",
            (
                ("outcome", "completed"),
                ("risk", "write" if activity_kind == "execute" else "elevated"),
                ("tool_family", "system"),
            ),
            1,
        )
    ]
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert spans[0].attributes["jhin.outcome"] == "completed"
    assert world.activities.authority_fresh_session_deltas == [1]


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
@pytest.mark.parametrize("duplicate_kind", ["identical", "conflicting"])
async def test_duplicate_requested_manifest_step_is_never_telemetry_authority(
    world: ToolWorld,
    activity_kind: str,
    duplicate_kind: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    tool_name = "system.echo" if activity_kind == "execute" else "system.approval"

    async def add_same_step_after_product_commit() -> None:
        await world.append_manifest(
            tool_name if duplicate_kind == "identical" else "system.fail",
            step_index=2,
            seq=22,
            value=(
                "private-input-canary"
                if duplicate_kind == "identical"
                else "conflicting-same-step-input"
            ),
        )

    world.activities.authority_before_load = add_same_step_after_product_commit

    result, error, frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )

    assert error is None
    assert frames == ()
    assert result is not None
    assert result.status == "executed"
    assert world.effects == ["private-input-canary"]
    assert _metric_point_multiset(world.telemetry) == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }
    assert spans[0].status.status_code is StatusCode.UNSET
    assert spans[0].events == ()


_COMMON_PERSISTED_AUTHORITY_FIELDS = (
    "tool_call_id",
    "tool_call_workspace_id",
    "tool_call_run_id",
    "tool_call_agent_id",
    "tool_call_tool_name",
    "tool_call_sanitized_input",
    "tool_call_status",
    "agent_run_id",
    "agent_run_workspace_id",
    "agent_run_agent_id",
    "agent_run_task_id",
    "manifest_workspace_id",
    "manifest_run_id",
    "manifest_task_id",
    "manifest_step_index",
    "manifest_count",
    "manifest_ordinal",
    "manifest_lossless",
    "manifest_tool_name",
    "manifest_arguments_raw",
    "manifest_canonical_input",
)
_APPROVAL_PERSISTED_AUTHORITY_FIELDS = (
    "tool_call_approval_id",
    "approval_id",
    "approval_workspace_id",
    "approval_task_id",
    "approval_run_id",
    "approval_requested_by_agent_id",
    "approval_action_type",
    "approval_status",
)


async def _mutate_one_persisted_authority_field(
    world: ToolWorld,
    *,
    field: str,
) -> None:
    async with world.sessions() as session:
        tool_call = await session.get(ToolCall, world.invocation_id)
        agent_run = await session.get(AgentRun, world.run_id)
        manifest = await session.scalar(
            select(RunEvent).where(
                RunEvent.workspace_id == world.workspace_id,
                RunEvent.run_id == world.run_id,
                RunEvent.event_type == "agent.step.tool_manifest",
            )
        )
        assert tool_call is not None
        assert agent_run is not None
        assert manifest is not None
        approval = (
            None
            if tool_call.approval_id is None
            else await session.get(Approval, tool_call.approval_id)
        )

        if field == "tool_call_id":
            tool_call.id = new_uuid7()
        elif field == "tool_call_workspace_id":
            tool_call.workspace_id = new_uuid7()
        elif field == "tool_call_run_id":
            tool_call.run_id = new_uuid7()
        elif field == "tool_call_agent_id":
            tool_call.agent_id = new_uuid7()
        elif field == "tool_call_tool_name":
            tool_call.tool_name = "system.changed"
        elif field == "tool_call_sanitized_input":
            tool_call.sanitized_input_json = {"value": "changed"}
        elif field == "tool_call_status":
            tool_call.status = (
                ToolCallStatus.FAILED.value
                if tool_call.status != ToolCallStatus.FAILED.value
                else ToolCallStatus.COMPLETED.value
            )
        elif field == "tool_call_approval_id":
            assert approval is not None
            tool_call.approval_id = new_uuid7()
        elif field == "agent_run_id":
            agent_run.id = new_uuid7()
        elif field == "agent_run_workspace_id":
            agent_run.workspace_id = new_uuid7()
        elif field == "agent_run_agent_id":
            agent_run.agent_id = new_uuid7()
        elif field == "agent_run_task_id":
            agent_run.task_id = new_uuid7()
        elif field == "manifest_workspace_id":
            manifest.workspace_id = new_uuid7()
        elif field == "manifest_run_id":
            manifest.run_id = new_uuid7()
        elif field == "manifest_task_id":
            manifest.task_id = new_uuid7()
        elif field.startswith("manifest_"):
            payload = deepcopy(manifest.payload_json)
            manifest_body = cast(dict[str, object], payload["manifest"])
            calls = cast(list[dict[str, object]], manifest_body["calls"])
            call = calls[0]
            if field == "manifest_step_index":
                payload["step"] = 3
            elif field == "manifest_count":
                manifest_body["count"] = 2
            elif field == "manifest_ordinal":
                call["ordinal"] = 1
            elif field == "manifest_lossless":
                call["lossless"] = False
            elif field == "manifest_tool_name":
                call["tool_name"] = "system.changed"
            elif field == "manifest_arguments_raw":
                original = cast(str, call["arguments_json"])
                call["arguments_json"] = json.dumps(
                    json.loads(original),
                    ensure_ascii=False,
                    indent=1,
                    sort_keys=True,
                )
                assert call["arguments_json"] != original
            elif field == "manifest_canonical_input":
                call["arguments_json"] = '{"value":"changed"}'
            else:
                raise AssertionError(f"unknown manifest authority field: {field}")
            manifest.payload_json = payload
        else:
            assert approval is not None
            if field == "approval_id":
                approval.id = new_uuid7()
            elif field == "approval_workspace_id":
                approval.workspace_id = new_uuid7()
            elif field == "approval_task_id":
                approval.task_id = new_uuid7()
            elif field == "approval_run_id":
                approval.run_id = new_uuid7()
            elif field == "approval_requested_by_agent_id":
                approval.requested_by_agent_id = new_uuid7()
            elif field == "approval_action_type":
                approval.action_type = "system.changed"
            elif field == "approval_status":
                approval.status = (
                    ApprovalStatus.REJECTED.value
                    if approval.status != ApprovalStatus.REJECTED.value
                    else ApprovalStatus.APPROVED.value
                )
            else:
                raise AssertionError(f"unknown approval authority field: {field}")
        await session.commit()


_PERSISTED_AUTHORITY_FIELD_CASES = [
    *(
        (activity_kind, field)
        for field in _COMMON_PERSISTED_AUTHORITY_FIELDS
        for activity_kind in ("execute", "approval")
    ),
    *(("approval", field) for field in _APPROVAL_PERSISTED_AUTHORITY_FIELDS),
]


async def test_each_persisted_authority_field_is_freshly_loaded_not_synthesized() -> None:
    # Loop-folded: the exact former parametrize matrix, one isolated world per case.
    for activity_kind, field in _PERSISTED_AUTHORITY_FIELD_CASES:
        ctx = f"activity_kind={activity_kind} field={field}"
        with _named_case(ctx):
            async with _fresh_world_context() as world:
                await _check_persisted_authority_field_case(
                    world,
                    activity_kind=activity_kind,
                    field=field,
                    ctx=ctx,
                )


async def _check_persisted_authority_field_case(
    world: ToolWorld,
    *,
    activity_kind: str,
    field: str,
    ctx: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    snapshots: list[dict[str, object]] = []

    async def mutate_exactly_one_field_after_product_commit() -> None:
        snapshots.append(await world.product_snapshot())
        await _mutate_one_persisted_authority_field(world, field=field)
        snapshots.append(await world.product_snapshot())

    world.activities.authority_before_load = mutate_exactly_one_field_after_product_commit

    result, error, frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )

    assert error is None, ctx
    assert frames == (), ctx
    assert result == BoundToolResult(
        tool_call_id=str(world.invocation_id),
        status="executed",
        approval_id=None if parked is None else parked.approval_id,
        stop_reason=None,
    ), ctx
    assert len(snapshots) == 2, ctx
    assert snapshots[0] != snapshots[1], ctx
    assert await world.product_snapshot() == snapshots[1], ctx
    assert world.effects == ["private-input-canary"], ctx
    assert world.activities.authority_fresh_session_deltas == [1], ctx
    assert len(world.activities.authority_sql_deltas) == 1, ctx
    assert world.activities.authority_sql_deltas[0] >= 1, ctx
    assert _metric_point_multiset(world.telemetry) == [], ctx
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1, ctx
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }, ctx
    assert spans[0].status.status_code is StatusCode.UNSET, ctx
    assert spans[0].events == (), ctx
    assert not ({"error.code", "error.type"} & set(spans[0].attributes)), ctx


_NON_SUCCESS_PERSISTED_AUTHORITY_CASES = [
    *(
        (case, field)
        for case in ("failed", "denied")
        for field in _COMMON_PERSISTED_AUTHORITY_FIELDS
    ),
    *(
        (case, field)
        for case in (
            "needs_approval",
            "rejected",
            "execution_unknown",
            "approval_denied",
            "approval_failed",
        )
        for field in (
            *_COMMON_PERSISTED_AUTHORITY_FIELDS,
            *_APPROVAL_PERSISTED_AUTHORITY_FIELDS,
        )
    ),
]


async def _prepare_persisted_authority_state(
    world: ToolWorld,
    *,
    case: str,
) -> BoundToolResult | None:
    if case in {"failed", "denied", "needs_approval"}:
        await world.seed_manifest(
            {
                "failed": "system.fail",
                "denied": "private.unknown",
                "needs_approval": "system.approval",
            }[case]
        )
        world.reset_diagnostics()
        return None
    parked = await world.park()
    assert parked.approval_id is not None
    await world.decide(
        parked.approval_id,
        ApprovalStatus.REJECTED.value if case == "rejected" else ApprovalStatus.APPROVED.value,
    )
    if case == "approval_denied":
        async with world.sessions() as session:
            await session.execute(
                delete(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == world.agent_id,
                    AgentCapabilityGrant.capability == "system.approval",
                )
            )
            await session.commit()
    elif case == "approval_failed":
        world.catalog._executors["system.approval"] = world.failure_executor
    elif case == "execution_unknown":
        async with world.sessions() as session:
            row = await session.get(ToolCall, world.invocation_id)
            assert row is not None
            row.status = ToolCallStatus.EXECUTING.value
            await session.commit()
    world.reset_diagnostics()
    return parked


async def _invoke_persisted_authority_state(
    world: ToolWorld,
    *,
    case: str,
    parked: BoundToolResult | None,
) -> tuple[BoundToolResult | None, ApplicationError | None]:
    try:
        if case in {"failed", "denied", "needs_approval"}:
            return (
                await world.activities.execute_bound_tool_activity(world.execute_params()),
                None,
            )
        assert parked is not None
        return (
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            ),
            None,
        )
    except ApplicationError as error:
        return None, error


async def test_non_success_states_load_every_persisted_authority_field_from_fresh_db() -> None:
    # Loop-folded: the exact former parametrize matrix, one isolated world per case.
    for case, field in _NON_SUCCESS_PERSISTED_AUTHORITY_CASES:
        ctx = f"case={case} field={field}"
        with _named_case(ctx):
            async with _fresh_world_context() as world:
                await _check_non_success_persisted_authority_case(
                    world,
                    case=case,
                    field=field,
                    ctx=ctx,
                )


async def _check_non_success_persisted_authority_case(
    world: ToolWorld,
    *,
    case: str,
    field: str,
    ctx: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_persisted_authority_state(control, case=case)
    parked = await _prepare_persisted_authority_state(world, case=case)

    control_effects_before = len(world.effects)
    control_result, control_error = await _invoke_persisted_authority_state(
        control,
        case=case,
        parked=control_parked,
    )
    control_effect_delta = len(world.effects) - control_effects_before
    control_snapshot = await control.product_snapshot()
    world.telemetry.exporter.clear()
    snapshots: list[dict[str, object]] = []

    async def mutate_exactly_one_field_after_product_commit() -> None:
        snapshots.append(await world.product_snapshot())
        await _mutate_one_persisted_authority_field(world, field=field)
        snapshots.append(await world.product_snapshot())

    world.activities.authority_before_load = mutate_exactly_one_field_after_product_commit
    target_effects_before = len(world.effects)

    result, error = await _invoke_persisted_authority_state(
        world,
        case=case,
        parked=parked,
    )

    assert len(snapshots) == 2, ctx
    assert _canonical_product_snapshot(world, snapshots[0]) == (
        _canonical_product_snapshot(control, control_snapshot)
    ), ctx
    assert snapshots[0] != snapshots[1], ctx
    assert await world.product_snapshot() == snapshots[1], ctx
    assert len(world.effects) - target_effects_before == control_effect_delta, ctx
    if control_error is not None:
        assert result is None, ctx
        assert error is not None, ctx
        assert _application_error_public(error) == _application_error_public(control_error), ctx
        assert _traceback_frame_names(error.__traceback__) == _traceback_frame_names(
            control_error.__traceback__
        ), ctx
    else:
        assert control_result is not None, ctx
        assert result is not None, ctx
        assert error is None, ctx
        target_approval_authority = _bound_result_approval_authority(
            result,
            parked,
            snapshots[0],
        )
        control_approval_authority = _bound_result_approval_authority(
            control_result,
            control_parked,
            control_snapshot,
        )
        assert _canonical_product_snapshot(
            world,
            {"result": asdict(result)},
            result_approval_authority=target_approval_authority,
        ) == _canonical_product_snapshot(
            control,
            {"result": asdict(control_result)},
            result_approval_authority=control_approval_authority,
        ), ctx
    assert world.activities.authority_fresh_session_deltas == [1], ctx
    assert len(world.activities.authority_sql_deltas) == 1, ctx
    assert world.activities.authority_sql_deltas[0] >= 1, ctx
    assert _metric_point_multiset(world.telemetry) == [], ctx
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1, ctx
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }, ctx
    assert spans[0].status.status_code is StatusCode.UNSET, ctx
    assert spans[0].events == (), ctx
    assert not ({"error.code", "error.type"} & set(spans[0].attributes)), ctx


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_every_replay_reproves_the_fresh_manifest_relation_after_its_commit(
    world: ToolWorld,
    activity_kind: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    first_result, first_error, _frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )
    assert first_error is None
    assert first_result is not None

    async def corrupt_manifest_relation_after_replay_commit() -> None:
        async with world.sessions() as session:
            event = await session.scalar(
                select(RunEvent).where(
                    RunEvent.workspace_id == world.workspace_id,
                    RunEvent.run_id == world.run_id,
                    RunEvent.event_type == "agent.step.tool_manifest",
                )
            )
            assert event is not None
            event.task_id = new_uuid7()
            await session.commit()

    world.activities.authority_before_load = corrupt_manifest_relation_after_replay_commit

    replay_result, replay_error, _frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )

    assert replay_error is None
    assert replay_result == first_result
    assert _metric_point_multiset(world.telemetry) == [
        (
            "tool_calls_total",
            (
                ("outcome", "completed"),
                ("risk", "write" if activity_kind == "execute" else "elevated"),
                ("tool_family", "system"),
            ),
            1,
        )
    ]
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 2
    assert [span.attributes["jhin.outcome"] for span in spans] == ["completed", "other"]
    assert dict(spans[-1].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }
    assert world.activities.authority_fresh_session_deltas == [1, 1]
    assert all(delta >= 1 for delta in world.activities.authority_sql_deltas)


async def test_execute_replay_preserves_result_and_span_but_never_duplicates_terminal_metric(
    world: ToolWorld,
) -> None:
    first = await world.activities.execute_bound_tool_activity(world.execute_params())
    first_state = await world.product_snapshot()
    second = await world.activities.execute_bound_tool_activity(world.execute_params())

    assert second == first
    assert await world.product_snapshot() == first_state
    assert world.effects == ["private-input-canary"]
    assert (
        _metric_sum(
            world.telemetry,
            "tool_calls_total",
            tool_family="system",
            risk="write",
            outcome="completed",
        )
        == 1
    )
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 2
    assert [span.attributes["jhin.outcome"] for span in spans] == ["completed", "completed"]
    assert all("error.code" not in span.attributes for span in spans)
    assert len(world.activities.authority_calls) == 2
    assert world.activities.authority_fresh_session_deltas == [1, 1]
    assert all(delta >= 1 for delta in world.activities.authority_sql_deltas)
    assert _metric_point_multiset(world.telemetry) == [
        (
            "tool_calls_total",
            (("outcome", "completed"), ("risk", "write"), ("tool_family", "system")),
            1,
        )
    ]


async def test_nonterminal_replay_stays_accepted_and_never_records_counter(
    world: ToolWorld,
) -> None:
    first = await world.park()
    second = await world.activities.execute_bound_tool_activity(world.execute_params())

    assert second == first
    assert _metric_points(world.telemetry, "tool_calls_total") == []
    assert _metric_points(world.telemetry, "tool_call_failures_total") == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 2
    assert [span.attributes["jhin.outcome"] for span in spans] == ["accepted", "accepted"]
    assert spans[-1].status.status_code is StatusCode.UNSET
    assert "error.code" not in spans[-1].attributes
    assert len(world.activities.authority_calls) == 2
    assert world.activities.authority_fresh_session_deltas == [1, 1]
    assert all(delta >= 1 for delta in world.activities.authority_sql_deltas)
    assert _metric_point_multiset(world.telemetry) == []


async def test_seeded_executing_recovery_counts_first_unknown_once_then_suppresses_replay(
    world: ToolWorld,
) -> None:
    parked = await world.park()
    await world.decide(parked.approval_id or "", ApprovalStatus.APPROVED.value)
    async with world.sessions() as session:
        row = await session.get(ToolCall, world.invocation_id)
        assert row is not None
        row.status = ToolCallStatus.EXECUTING.value
        await session.commit()
    world.reset_diagnostics()

    first = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id or "")
    )
    second = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id or "")
    )

    assert first == second
    assert first.status == "execution_unknown"
    row = await world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.EXECUTION_UNKNOWN.value
    assert (
        _metric_sum(
            world.telemetry,
            "tool_calls_total",
            tool_family="system",
            risk="elevated",
            outcome="execution_unknown",
        )
        == 1
    )
    assert (
        _metric_sum(
            world.telemetry,
            "tool_call_failures_total",
            tool_family="system",
            failure_class="execution_unknown",
        )
        == 1
    )
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 2
    assert [span.attributes["jhin.outcome"] for span in spans] == [
        "execution_unknown",
        "execution_unknown",
    ]
    assert spans[-1].status.status_code is StatusCode.ERROR
    assert spans[-1].attributes["error.code"] == "execution_unknown"
    assert set(spans[-1].attributes) <= {
        "jhin.tool_family",
        "jhin.risk",
        "jhin.outcome",
        "error.code",
        "error.type",
    }
    assert len(world.activities.authority_calls) == 2
    assert world.activities.authority_fresh_session_deltas == [1, 1]
    assert all(delta >= 1 for delta in world.activities.authority_sql_deltas)
    assert _metric_point_multiset(world.telemetry) == [
        (
            "tool_call_failures_total",
            (("failure_class", "execution_unknown"), ("tool_family", "system")),
            1,
        ),
        (
            "tool_calls_total",
            (
                ("outcome", "execution_unknown"),
                ("risk", "elevated"),
                ("tool_family", "system"),
            ),
            1,
        ),
    ]


@pytest.mark.parametrize(
    ("case", "outcome", "family", "risk", "failure_class", "safe_error_code"),
    [
        ("failed", "failed", "system", "read", "internal", "internal_error"),
        ("denied", "denied", "other", "other", "policy", "authorization_failed"),
        ("rejected", "rejected", "system", "elevated", "policy", "authorization_failed"),
    ],
)
async def test_proven_failure_replay_keeps_terminal_span_but_never_duplicates_metrics(
    world: ToolWorld,
    case: str,
    outcome: str,
    family: str,
    risk: str,
    failure_class: str,
    safe_error_code: str,
) -> None:
    parked: BoundToolResult | None = None
    if case == "failed":
        await world.seed_manifest("system.fail")
    elif case == "denied":
        await world.seed_manifest("private.unknown")
    else:
        parked = await world.park()
        await world.decide(parked.approval_id or "", ApprovalStatus.REJECTED.value)
        world.reset_diagnostics()

    public_errors: list[dict[str, object]] = []
    public_results: list[BoundToolResult] = []
    for _attempt in range(2):
        if case in {"failed", "denied"}:
            with pytest.raises(ApplicationError) as caught:
                await world.activities.execute_bound_tool_activity(world.execute_params())
            public_errors.append(_application_error_public(caught.value))
        else:
            assert parked is not None
            public_results.append(
                await world.activities.resolve_bound_tool_approval_activity(
                    world.approval_params(parked.approval_id or "")
                )
            )

    if public_errors:
        assert public_errors[1] == public_errors[0]
    if public_results:
        assert public_results[1] == public_results[0]
        assert public_results[0].status == "rejected"
    assert (
        _metric_sum(
            world.telemetry,
            "tool_calls_total",
            tool_family=family,
            risk=risk,
            outcome=outcome,
        )
        == 1
    )
    assert (
        _metric_sum(
            world.telemetry,
            "tool_call_failures_total",
            tool_family=family,
            failure_class=failure_class,
        )
        == 1
    )
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 2
    assert [span.attributes["jhin.outcome"] for span in spans] == [outcome, outcome]
    for span in spans:
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes["error.code"] == safe_error_code
    assert len(world.activities.authority_calls) == 2
    assert world.activities.authority_fresh_session_deltas == [1, 1]
    assert all(delta >= 1 for delta in world.activities.authority_sql_deltas)
    assert _metric_point_multiset(world.telemetry) == [
        (
            "tool_call_failures_total",
            (("failure_class", failure_class), ("tool_family", family)),
            1,
        ),
        (
            "tool_calls_total",
            (("outcome", outcome), ("risk", risk), ("tool_family", family)),
            1,
        ),
    ]


@pytest.mark.parametrize(
    ("case", "status", "failure_class"),
    [
        ("denied", "denied", "policy"),
        ("failed", "failed", "internal"),
    ],
)
async def test_approval_denied_and_failed_preserve_bound_result_asymmetry(
    world: ToolWorld,
    case: str,
    status: str,
    failure_class: str,
) -> None:
    parked = await world.park()
    await world.decide(parked.approval_id or "", ApprovalStatus.APPROVED.value)
    if case == "denied":
        async with world.sessions() as session:
            await session.execute(
                delete(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == world.agent_id,
                    AgentCapabilityGrant.capability == "system.approval",
                )
            )
            await session.commit()
    else:
        world.catalog._executors["system.approval"] = world.failure_executor
    world.telemetry.exporter.clear()

    result = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id or "")
    )

    assert isinstance(result, BoundToolResult)
    assert result.status == status
    row = await world.tool_call()
    assert row is not None
    assert row.status == status
    assert (
        _metric_sum(
            world.telemetry,
            "tool_calls_total",
            tool_family="system",
            risk="elevated",
            outcome=status,
        )
        == 1
    )
    assert (
        _metric_sum(
            world.telemetry,
            "tool_call_failures_total",
            tool_family="system",
            failure_class=failure_class,
        )
        == 1
    )
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert spans[0].name == "tool.approval.resolve"
    assert spans[0].attributes["jhin.outcome"] == status
    assert spans[0].status.status_code is StatusCode.ERROR


@pytest.mark.parametrize(
    (
        "case",
        "result_status",
        "telemetry_outcome",
        "failure_class",
        "safe_error_code",
        "expected_effects",
    ),
    [
        (
            "completed",
            "executed",
            "completed",
            None,
            None,
            ["private-input-canary"],
        ),
        ("denied", "denied", "denied", "policy", "authorization_failed", []),
        ("failed", "failed", "failed", "internal", "internal_error", []),
        (
            "rejected",
            "rejected",
            "rejected",
            "policy",
            "authorization_failed",
            [],
        ),
        (
            "execution_unknown",
            "execution_unknown",
            "execution_unknown",
            "execution_unknown",
            "execution_unknown",
            [],
        ),
    ],
)
async def test_every_approval_terminal_replay_reproves_and_never_recounts(
    world: ToolWorld,
    case: str,
    result_status: str,
    telemetry_outcome: str,
    failure_class: str | None,
    safe_error_code: str | None,
    expected_effects: list[str],
) -> None:
    parked = await world.park()
    assert parked.approval_id is not None
    await world.decide(
        parked.approval_id,
        ApprovalStatus.REJECTED.value if case == "rejected" else ApprovalStatus.APPROVED.value,
    )
    if case == "denied":
        async with world.sessions() as session:
            await session.execute(
                delete(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == world.agent_id,
                    AgentCapabilityGrant.capability == "system.approval",
                )
            )
            await session.commit()
    elif case == "failed":
        world.catalog._executors["system.approval"] = world.failure_executor
    elif case == "execution_unknown":
        async with world.sessions() as session:
            row = await session.get(ToolCall, world.invocation_id)
            assert row is not None
            row.status = ToolCallStatus.EXECUTING.value
            await session.commit()
    world.reset_diagnostics()

    first = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id)
    )
    first_snapshot = await world.product_snapshot()
    first_effects = list(world.effects)
    second = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id)
    )

    expected = BoundToolResult(
        tool_call_id=str(world.invocation_id),
        status=result_status,
        approval_id=parked.approval_id,
        stop_reason="execution_unknown" if case == "execution_unknown" else None,
    )
    assert first == expected
    assert second == first
    assert await world.product_snapshot() == first_snapshot
    assert world.effects == first_effects == expected_effects
    assert world.activities.authority_fresh_session_deltas == [1, 1]
    assert len(world.activities.authority_sql_deltas) == 2
    assert all(delta >= 1 for delta in world.activities.authority_sql_deltas)
    assert len(world.activities.authority_calls) == 2

    expected_points = [
        (
            "tool_calls_total",
            (
                ("outcome", telemetry_outcome),
                ("risk", "elevated"),
                ("tool_family", "system"),
            ),
            1,
        )
    ]
    if failure_class is not None:
        expected_points.append(
            (
                "tool_call_failures_total",
                (("failure_class", failure_class), ("tool_family", "system")),
                1,
            )
        )
    assert _metric_point_multiset(world.telemetry) == sorted(expected_points)

    spans = _tool_spans(world.telemetry)
    assert len(spans) == 2
    for span in spans:
        assert span.name == "tool.approval.resolve"
        assert {
            key: value
            for key, value in dict(span.attributes).items()
            if not key.startswith("error.")
        } == {
            "jhin.tool_family": "system",
            "jhin.risk": "elevated",
            "jhin.outcome": telemetry_outcome,
        }
        assert span.events == ()
        assert span.links == ()
        if safe_error_code is None:
            assert span.status.status_code is StatusCode.UNSET
            assert not ({"error.code", "error.type"} & set(span.attributes))
        else:
            assert span.status.status_code is StatusCode.ERROR
            assert span.attributes["error.type"] == "ToolActivityError"
            assert span.attributes["error.code"] == safe_error_code


@pytest.mark.parametrize(
    (
        "case",
        "result_status",
        "telemetry_outcome",
        "approval_status",
        "safe_error_code",
    ),
    [
        ("completed", "executed", "completed", "approved", None),
        ("denied", "denied", "denied", "approved", "authorization_failed"),
        ("failed", "failed", "failed", "approved", "internal_error"),
        ("rejected", "rejected", "rejected", "rejected", "authorization_failed"),
        (
            "execution_unknown",
            "execution_unknown",
            "execution_unknown",
            "approved",
            "execution_unknown",
        ),
    ],
)
async def test_execute_retry_after_approval_resolution_proves_terminal_approval_state(
    world: ToolWorld,
    case: str,
    result_status: str,
    telemetry_outcome: str,
    approval_status: str,
    safe_error_code: str | None,
) -> None:
    parked = await world.park()
    assert parked.approval_id is not None
    await world.decide(
        parked.approval_id,
        ApprovalStatus.REJECTED.value if case == "rejected" else ApprovalStatus.APPROVED.value,
    )
    if case == "denied":
        async with world.sessions() as session:
            await session.execute(
                delete(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == world.agent_id,
                    AgentCapabilityGrant.capability == "system.approval",
                )
            )
            await session.commit()
    elif case == "failed":
        world.catalog._executors["system.approval"] = world.failure_executor
    elif case == "execution_unknown":
        async with world.sessions() as session:
            row = await session.get(ToolCall, world.invocation_id)
            assert row is not None
            row.status = ToolCallStatus.EXECUTING.value
            await session.commit()
    world.reset_diagnostics()

    first = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id)
    )
    assert first.status == result_status
    snapshot = await world.product_snapshot()
    points = _metric_point_multiset(world.telemetry)
    effects = list(world.effects)
    tool_call = snapshot["tool_call"]
    approval = snapshot["approval"]
    assert type(tool_call) is dict
    assert type(approval) is dict
    assert approval["status"] == approval_status
    world.reset_diagnostics()

    replay_result: BoundToolResult | None = None
    replay_error: ApplicationError | None = None
    try:
        replay_result = await world.activities.execute_bound_tool_activity(world.execute_params())
    except ApplicationError as error:
        replay_error = error

    if safe_error_code is None or case == "execution_unknown":
        assert replay_error is None
        assert replay_result == first
    else:
        assert replay_result is None
        assert replay_error is not None
        assert replay_error.message == ("bound tool execution was rejected before a usable outcome")
        assert replay_error.type == tool_call["error_code"]
        assert replay_error.non_retryable is True
        assert _traceback_frame_names(replay_error.__traceback__)[-1] == ("_raise_ordinary_failure")
    assert await world.product_snapshot() == snapshot
    assert world.effects == effects
    assert _metric_point_multiset(world.telemetry) == points
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert spans[0].name == "tool.gateway.execute"
    assert {
        key: value
        for key, value in dict(spans[0].attributes).items()
        if not key.startswith("error.")
    } == {
        "jhin.tool_family": "system",
        "jhin.risk": "elevated",
        "jhin.outcome": telemetry_outcome,
    }
    assert spans[0].events == ()
    assert spans[0].links == ()
    if safe_error_code is None:
        assert spans[0].status.status_code is StatusCode.UNSET
        assert not ({"error.code", "error.type"} & set(spans[0].attributes))
    else:
        assert spans[0].status.status_code is StatusCode.ERROR
        assert spans[0].attributes["error.type"] == "ToolActivityError"
        assert spans[0].attributes["error.code"] == safe_error_code


@pytest.mark.parametrize(
    ("case", "wrong_status"),
    [
        ("executed", ToolCallStatus.FAILED.value),
        ("failed", ToolCallStatus.COMPLETED.value),
        ("denied", ToolCallStatus.REJECTED.value),
        ("needs_approval", ToolCallStatus.COMPLETED.value),
        ("rejected", ToolCallStatus.DENIED.value),
        ("execution_unknown", ToolCallStatus.EXECUTING.value),
    ],
)
async def test_each_outcome_to_durable_status_mismatch_is_independently_suppressed(
    world: ToolWorld,
    case: str,
    wrong_status: str,
) -> None:
    parked: BoundToolResult | None = None
    if case == "failed":
        await world.seed_manifest("system.fail")
    elif case == "denied":
        await world.seed_manifest("private.unknown")
    elif case in {"needs_approval", "rejected", "execution_unknown"}:
        parked = await world.park()
        if case != "needs_approval":
            await world.decide(
                parked.approval_id or "",
                ApprovalStatus.REJECTED.value
                if case == "rejected"
                else ApprovalStatus.APPROVED.value,
            )
            if case == "execution_unknown":
                async with world.sessions() as session:
                    row = await session.get(ToolCall, world.invocation_id)
                    assert row is not None
                    row.status = ToolCallStatus.EXECUTING.value
                    await session.commit()
            world.telemetry.exporter.clear()
            world.activities.authority_calls.clear()
    world.activities.authority_mutator = lambda authority: replace(
        cast(Any, authority),
        row_status=wrong_status,
    )

    if case in {"failed", "denied"}:
        with pytest.raises(ApplicationError):
            await world.activities.execute_bound_tool_activity(world.execute_params())
    elif case in {"executed", "needs_approval"}:
        await world.activities.execute_bound_tool_activity(world.execute_params())
    else:
        assert parked is not None
        await world.activities.resolve_bound_tool_approval_activity(
            world.approval_params(parked.approval_id or "")
        )

    assert _metric_points(world.telemetry, "tool_calls_total") == []
    assert _metric_points(world.telemetry, "tool_call_failures_total") == []
    spans = _tool_spans(world.telemetry)
    assert spans
    assert spans[-1].attributes["jhin.outcome"] == "other"
    assert spans[-1].status.status_code is StatusCode.UNSET
    assert spans[-1].events == ()
    assert not ({"error.code", "error.type"} & set(spans[-1].attributes))


class _WorkerDiagnostic(Exception):
    pass


@pytest.mark.parametrize(
    "late_catalog",
    ["removed", RuntimeError, ValueError, KeyError, AttributeError, _WorkerDiagnostic],
)
async def test_owned_terminal_transition_with_late_untrusted_catalog_uses_other_labels(
    world: ToolWorld,
    late_catalog: str | type[Exception],
) -> None:
    original_get = world.catalog.get

    def get_after_effect(name: str) -> object:
        if world.effects:
            if late_catalog == "removed":
                return None
            assert isinstance(late_catalog, type)
            raise late_catalog("private-late-catalog-failure")
        return original_get(name)

    world.catalog.get = cast(Any, get_after_effect)

    result = await world.activities.execute_bound_tool_activity(world.execute_params())

    assert result.status == "executed"
    assert world.effects == ["private-input-canary"]
    assert (
        _metric_sum(
            world.telemetry,
            "tool_calls_total",
            tool_family="other",
            risk="other",
            outcome="completed",
        )
        == 1
    )
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "completed",
    }


@pytest.mark.parametrize("failure_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
async def test_late_catalog_cancellation_and_fatal_authority_propagate_after_product_commit(
    world: ToolWorld,
    failure_type: type[BaseException],
) -> None:
    original_get = world.catalog.get
    failure = failure_type("late-catalog-authority")
    raised_traceback: TracebackType | None = None

    def get_after_effect(name: str) -> object:
        nonlocal raised_traceback
        if world.effects:
            try:
                raise failure
            except BaseException as error:
                raised_traceback = error.__traceback__
                raise
        return original_get(name)

    world.catalog.get = cast(Any, get_after_effect)

    with pytest.raises(failure_type) as caught:
        await world.activities.execute_bound_tool_activity(world.execute_params())

    assert caught.value is failure
    assert raised_traceback is not None
    assert _traceback_tail(caught.value.__traceback__) is raised_traceback
    assert world.effects == ["private-input-canary"]
    row = await world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value
    assert _metric_points(world.telemetry, "tool_calls_total") == []
    assert _metric_points(world.telemetry, "tool_call_failures_total") == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert spans[0].attributes["jhin.outcome"] == "other"
    assert spans[0].status.status_code is StatusCode.UNSET
    assert not ({"error.code", "error.type"} & set(spans[0].attributes))


@pytest.mark.parametrize(
    "catalog_failure_type",
    [_WorkerDiagnostic, asyncio.CancelledError, KeyboardInterrupt, SystemExit],
)
@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_row_status_is_proved_before_any_late_catalog_access(
    world: ToolWorld,
    activity_kind: str,
    catalog_failure_type: type[BaseException],
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    proof_loaded = False
    catalog_calls = 0
    original_get = world.catalog.get
    catalog_failure = catalog_failure_type("catalog-after-row-mismatch")

    def mismatch_row_status(authority: object) -> object:
        nonlocal proof_loaded
        proof_loaded = True
        return replace(
            cast(Any, authority),
            row_status=ToolCallStatus.FAILED.value,
        )

    def fail_if_catalog_is_reached(name: str) -> object:
        nonlocal catalog_calls
        if proof_loaded:
            catalog_calls += 1
            raise catalog_failure
        return original_get(name)

    world.activities.authority_mutator = mismatch_row_status
    world.catalog.get = cast(Any, fail_if_catalog_is_reached)

    escaped: BaseException | None = None
    result: BoundToolResult | None = None
    error: ApplicationError | None = None
    frames: tuple[tuple[str, str, int], ...] = ()
    try:
        result, error, frames = await _invoke_terminal_case(
            world,
            activity_kind=activity_kind,
            product_kind="success",
            parked=parked,
        )
    except BaseException as caught:
        escaped = caught

    assert escaped is None
    assert error is None
    assert frames == ()
    assert result is not None
    assert result.status == "executed"
    assert proof_loaded is True
    assert catalog_calls == 0
    assert world.effects == ["private-input-canary"]
    row = await world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value
    assert _metric_point_multiset(world.telemetry) == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }
    assert spans[0].status.status_code is StatusCode.UNSET
    assert spans[0].events == ()


async def test_invocation_mismatch_preserves_existing_public_error_and_suppresses_retry_metric(
    world: ToolWorld,
) -> None:
    first = await world.activities.execute_bound_tool_activity(world.execute_params())
    first_state = await world.product_snapshot()
    await world.seed_manifest("system.fail", value="changed-private-input")

    with pytest.raises(ApplicationError) as caught:
        await world.activities.execute_bound_tool_activity(world.execute_params())

    assert caught.value.type == "tool_invocation_mismatch"
    assert caught.value.non_retryable is True
    assert world.effects == ["private-input-canary"]
    assert await world.product_snapshot() != first_state
    assert first.status == "executed"
    assert len(_metric_points(world.telemetry, "tool_calls_total")) == 1
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 2
    assert spans[-1].attributes["jhin.outcome"] == "other"
    assert spans[-1].status.status_code is StatusCode.UNSET
    assert "error.code" not in spans[-1].attributes
    assert len(world.activities.authority_calls) == 2
    assert world.activities.authority_fresh_session_deltas == [1, 1]
    assert all(delta >= 1 for delta in world.activities.authority_sql_deltas)
    assert _metric_point_multiset(world.telemetry) == [
        (
            "tool_calls_total",
            (("outcome", "completed"), ("risk", "write"), ("tool_family", "system")),
            1,
        )
    ]


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_coordinated_wrong_real_row_and_outcome_id_cannot_prove_telemetry(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    wrong_id = new_uuid7()
    method_name = "request" if activity_kind == "execute" else "resolve_approved"
    original = getattr(ToolGateway, method_name)

    async def return_coordinated_wrong_id(
        gateway: ToolGateway,
        *args: object,
        **kwargs: object,
    ) -> object:
        outcome = cast(Any, await original(gateway, *args, **kwargs))
        row = await gateway._ctx.session.get(ToolCall, outcome.tool_call_id)
        assert row is not None
        row.id = wrong_id
        return outcome.model_copy(update={"tool_call_id": wrong_id})

    monkeypatch.setattr(ToolGateway, method_name, return_coordinated_wrong_id)

    with pytest.raises(ApplicationError) as caught:
        if activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )

    expected_message = (
        "runtime tool call identity did not match its bound invocation"
        if activity_kind == "execute"
        else "approval tool identity changed during resolution"
    )
    assert _application_error_public(caught.value) == {
        "message": expected_message,
        "args": (f"tool_invocation_mismatch: {expected_message}",),
        "details": (),
        "type": "tool_invocation_mismatch",
        "non_retryable": True,
        "next_retry_delay": None,
        "category": 0,
        "suppress_context": False,
    }
    assert _traceback_frame_names(caught.value.__traceback__)[-1] == (
        "execute_bound_tool_activity"
        if activity_kind == "execute"
        else "resolve_bound_tool_approval_activity"
    )
    async with world.sessions() as session:
        assert await session.get(ToolCall, world.invocation_id) is None
        wrong_row = await session.get(ToolCall, wrong_id)
        assert wrong_row is not None
        assert wrong_row.status == ToolCallStatus.COMPLETED.value
    assert world.effects == ["private-input-canary"]
    assert _metric_point_multiset(world.telemetry) == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }
    assert spans[0].status.status_code is StatusCode.UNSET
    assert spans[0].events == ()


async def test_every_postcommit_authority_tuple_mismatch_suppresses_diagnostics_only() -> None:
    # Loop-folded: the exact former parametrize matrix, one isolated world per case.
    cases: list[tuple[str, object]] = [
        ("outcome_tool_call_id", new_uuid7()),
        ("outcome_status", "failed"),
        ("outcome_tool_name", "system.other"),
        ("outcome_sanitized_input_json", {"value": "changed"}),
        ("outcome_replayed", True),
        ("outcome_decision_code", "invocation_mismatch"),
        ("tool_call_id", new_uuid7()),
        ("workspace_id", new_uuid7()),
        ("run_id", new_uuid7()),
        ("agent_id", new_uuid7()),
        ("tool_name", "system.other"),
        ("sanitized_input_json", {"value": "changed"}),
        ("task_id", new_uuid7()),
        ("agent_run_id", new_uuid7()),
        ("agent_run_workspace_id", new_uuid7()),
        ("agent_run_agent_id", new_uuid7()),
        ("agent_run_task_id", new_uuid7()),
        ("manifest_workspace_id", new_uuid7()),
        ("manifest_run_id", new_uuid7()),
        ("manifest_step_index", 3),
        ("manifest_ordinal", 1),
        ("manifest_lossless", False),
        ("manifest_tool_name", "system.other"),
        ("manifest_arguments_json", '{"value":"changed"}'),
        ("manifest_canonical_input_json", {"value": "changed"}),
        ("derived_tool_call_id", new_uuid7()),
        ("row_status", ToolCallStatus.FAILED.value),
        ("approval_id", new_uuid7()),
        ("approval_workspace_id", new_uuid7()),
        ("approval_task_id", new_uuid7()),
        ("approval_run_id", new_uuid7()),
        ("approval_requested_by_agent_id", new_uuid7()),
        ("approval_action_type", "system.other"),
        ("approval_status", ApprovalStatus.APPROVED.value),
    ]
    for field, replacement in cases:
        ctx = f"field={field} replacement={replacement!r}"
        with _named_case(ctx):
            async with _fresh_world_context() as world:
                world.activities.authority_mutator = (
                    lambda authority, _field=field, _replacement=replacement: replace(
                        cast(Any, authority),
                        **{_field: _replacement},
                    )
                )

                result = await world.activities.execute_bound_tool_activity(world.execute_params())

                assert result.status == "executed", ctx
                assert world.effects == ["private-input-canary"], ctx
                row = await world.tool_call()
                assert row is not None, ctx
                assert row.status == ToolCallStatus.COMPLETED.value, ctx
                assert _metric_points(world.telemetry, "tool_calls_total") == [], ctx
                assert _metric_points(world.telemetry, "tool_call_failures_total") == [], ctx
                spans = _tool_spans(world.telemetry)
                assert len(spans) == 1, ctx
                assert spans[0].attributes["jhin.outcome"] == "other", ctx
                assert spans[0].status.status_code is StatusCode.UNSET, ctx
                assert "error.code" not in spans[0].attributes, ctx


async def test_every_approval_postcommit_authority_field_mismatch_is_diagnostic_only() -> None:
    # Loop-folded: the exact former parametrize matrix, one isolated world per case.
    cases: list[tuple[str, object]] = [
        ("outcome_tool_call_id", new_uuid7()),
        ("outcome_status", "rejected"),
        ("outcome_tool_name", "system.other"),
        ("outcome_sanitized_input_json", {"value": "changed"}),
        ("outcome_replayed", True),
        ("outcome_decision_code", "invocation_mismatch"),
        ("tool_call_id", new_uuid7()),
        ("workspace_id", new_uuid7()),
        ("run_id", new_uuid7()),
        ("agent_id", new_uuid7()),
        ("tool_name", "system.other"),
        ("sanitized_input_json", {"value": "changed"}),
        ("task_id", new_uuid7()),
        ("agent_run_id", new_uuid7()),
        ("agent_run_workspace_id", new_uuid7()),
        ("agent_run_agent_id", new_uuid7()),
        ("agent_run_task_id", new_uuid7()),
        ("manifest_workspace_id", new_uuid7()),
        ("manifest_run_id", new_uuid7()),
        ("manifest_step_index", 3),
        ("manifest_ordinal", 1),
        ("manifest_lossless", False),
        ("manifest_tool_name", "system.other"),
        ("manifest_arguments_json", '{"value":"changed"}'),
        ("manifest_canonical_input_json", {"value": "changed"}),
        ("derived_tool_call_id", new_uuid7()),
        ("row_status", ToolCallStatus.REJECTED.value),
        ("approval_id", new_uuid7()),
        ("approval_workspace_id", new_uuid7()),
        ("approval_task_id", new_uuid7()),
        ("approval_run_id", new_uuid7()),
        ("approval_requested_by_agent_id", new_uuid7()),
        ("approval_action_type", "system.other"),
        ("approval_status", ApprovalStatus.REJECTED.value),
    ]
    for field, replacement in cases:
        ctx = f"field={field} replacement={replacement!r}"
        with _named_case(ctx):
            async with _fresh_world_context() as world:
                parked = await world.park()
                await world.decide(parked.approval_id or "", ApprovalStatus.APPROVED.value)
                world.telemetry.exporter.clear()
                world.activities.authority_calls.clear()
                world.activities.authority_mutator = (
                    lambda authority, _field=field, _replacement=replacement: replace(
                        cast(Any, authority),
                        **{_field: _replacement},
                    )
                )

                result = await world.activities.resolve_bound_tool_approval_activity(
                    world.approval_params(parked.approval_id or "")
                )

                assert result.status == "executed", ctx
                assert world.effects == ["private-input-canary"], ctx
                row = await world.tool_call()
                assert row is not None, ctx
                assert row.status == ToolCallStatus.COMPLETED.value, ctx
                assert _metric_points(world.telemetry, "tool_calls_total") == [], ctx
                assert _metric_points(world.telemetry, "tool_call_failures_total") == [], ctx
                spans = _tool_spans(world.telemetry)
                assert len(spans) == 1, ctx
                assert spans[0].name == "tool.approval.resolve", ctx
                assert spans[0].attributes["jhin.outcome"] == "other", ctx
                assert spans[0].status.status_code is StatusCode.UNSET, ctx
                assert spans[0].events == (), ctx
                assert not ({"error.code", "error.type"} & set(spans[0].attributes)), ctx


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
@pytest.mark.parametrize("proof_shape", ["missing", "duplicate"])
async def test_missing_or_duplicate_postcommit_proof_is_never_telemetry_authority(
    world: ToolWorld,
    activity_kind: str,
    proof_shape: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    if proof_shape == "missing":
        world.activities.authority_mutator = lambda _authority: None
    else:
        world.activities.authority_mutator = lambda authority: (authority, authority)

    result, error, _frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )

    assert error is None
    assert result is not None
    assert result.status == "executed"
    row = await world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value
    assert _metric_points(world.telemetry, "tool_calls_total") == []
    assert _metric_points(world.telemetry, "tool_call_failures_total") == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert spans[0].attributes["jhin.outcome"] == "other"
    assert spans[0].status.status_code is StatusCode.UNSET
    assert spans[0].events == ()
    assert not ({"error.code", "error.type"} & set(spans[0].attributes))


@pytest.mark.parametrize(
    ("activity_kind", "product_kind"),
    [
        ("execute", "success"),
        ("execute", "failure"),
        ("approval", "success"),
        ("approval", "failure"),
    ],
)
async def test_tool_span_is_exact_current_child_through_gateway_proof_and_public_exit(
    world: ToolWorld,
    activity_kind: str,
    product_kind: str,
) -> None:
    parked: BoundToolResult | None = None
    if activity_kind == "execute":
        await world.seed_manifest("system.echo" if product_kind == "success" else "system.fail")
    else:
        parked = await world.park()
        await world.decide(
            parked.approval_id or "",
            ApprovalStatus.APPROVED.value
            if product_kind == "success"
            else ApprovalStatus.REJECTED.value,
        )
        world.telemetry.exporter.clear()
        world.activities.authority_calls.clear()
        world.activities.authority_current_spans.clear()
        world.effect_spans.clear()

    with world.telemetry.tracer.start_as_current_span("bounded.parent") as parent:
        parent_context = parent.get_span_context()
        if activity_kind == "execute" and product_kind == "failure":
            with pytest.raises(ApplicationError):
                await world.activities.execute_bound_tool_activity(world.execute_params())
        elif activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            result = await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )
            assert result.status == ("executed" if product_kind == "success" else "rejected")
        assert trace.get_current_span() is parent

    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    tool_span = spans[0]
    assert tool_span.parent is not None
    assert tool_span.parent.span_id == parent_context.span_id
    assert tool_span.parent.trace_id == parent_context.trace_id
    assert len(world.activities.authority_current_spans) == 1
    proof_span = world.activities.authority_current_spans[0]
    assert proof_span.get_span_context() == tool_span.context
    if product_kind == "success":
        assert len(world.effect_spans) == 1
        assert world.effect_spans[0].get_span_context() == tool_span.context
    else:
        assert world.effect_spans == []
    assert trace.get_current_span() is not tool_span
    assert tool_span.end_time is not None
    assert tool_span.attributes.get("jhin.outcome") in {
        "completed",
        "failed",
        "rejected",
    }
    assert not {
        "jhin.workspace_id",
        "jhin.task_id",
        "jhin.run_id",
        "jhin.agent_id",
        "jhin.tool_call_id",
        "jhin.approval_id",
    } & set(tool_span.attributes)


async def test_tool_telemetry_inherits_predecessor_context_without_copying_parent_ids(
    world: ToolWorld,
) -> None:
    inherited = {
        "jhin.workspace_id": str(world.workspace_id),
        "jhin.task_id": str(world.task_id),
        "jhin.run_id": str(world.run_id),
        "jhin.correlation_id": str(new_uuid7()),
    }

    with world.telemetry.tracer.start_as_current_span(
        "agent.reason_step",
        attributes=inherited,
    ) as parent:
        parent_context = parent.get_span_context()
        result = await world.activities.execute_bound_tool_activity(world.execute_params())
        assert result.status == "executed"

    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    span = spans[0]
    assert span.parent is not None
    assert span.parent.trace_id == parent_context.trace_id
    assert span.parent.span_id == parent_context.span_id
    assert not set(inherited) & set(span.attributes)
    assert dict(span.attributes) == {
        "jhin.tool_family": "system",
        "jhin.risk": "write",
        "jhin.outcome": "completed",
    }
    for point in _metric_points(world.telemetry, "tool_calls_total"):
        assert not any("id" in key for key in point.attributes)


async def test_execute_span_begins_only_after_manifest_and_runtime_identity_validation() -> None:
    # Loop-folded: the exact former parametrize matrix, one isolated world per case.
    for invalid_case in (
        "invalid-workspace",
        "invalid-run-id",
        "wrong-workspace",
        "position",
        "missing-manifest",
        "malformed-manifest",
        "missing-run-row",
        "missing-run-context",
        "missing-task-row",
        "task-workspace-mismatch",
        "task-agent-mismatch",
        "missing-agent-row",
        "agent-workspace-mismatch",
    ):
        ctx = f"invalid_case={invalid_case}"
        with _named_case(ctx):
            async with _fresh_world_context() as world:
                await _check_execute_span_prevalidation_case(
                    world,
                    invalid_case=invalid_case,
                    ctx=ctx,
                )


async def _check_execute_span_prevalidation_case(
    world: ToolWorld,
    *,
    invalid_case: str,
    ctx: str,
) -> None:
    params = world.execute_params()
    if invalid_case == "invalid-workspace":
        params.workspace_id = "not-a-uuid"
    elif invalid_case == "invalid-run-id":
        params.run_id = "not-a-uuid"
    elif invalid_case == "wrong-workspace":
        params.workspace_id = str(new_uuid7())
    elif invalid_case == "position":
        params.ordinal = 999
    elif invalid_case == "missing-manifest":
        async with world.sessions() as session:
            await session.execute(delete(RunEvent).where(RunEvent.run_id == world.run_id))
            await session.commit()
    elif invalid_case == "malformed-manifest":
        async with world.sessions() as session:
            event = await session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == world.run_id,
                    RunEvent.event_type == "agent.step.tool_manifest",
                )
            )
            assert event is not None
            payload = deepcopy(event.payload_json)
            payload["manifest"]["calls"][0]["lossless"] = False
            event.payload_json = payload
            await session.commit()
    elif invalid_case == "missing-run-row":
        async with world.sessions() as session:
            await session.execute(delete(AgentRun).where(AgentRun.id == world.run_id))
            await session.commit()
    elif invalid_case == "missing-run-context":
        async with world.sessions() as session:
            run = await session.get(AgentRun, world.run_id)
            assert run is not None
            run.task_id = None
            await session.commit()
    elif invalid_case == "missing-task-row":
        async with world.sessions() as session:
            run = await session.get(AgentRun, world.run_id)
            assert run is not None
            run.task_id = new_uuid7()
            await session.commit()
    elif invalid_case == "task-workspace-mismatch":
        async with world.sessions() as session:
            workspace = Workspace(
                name="Wrong task workspace",
                slug=f"wrong-task-{new_uuid7().hex[:8]}",
            )
            session.add(workspace)
            await session.flush()
            task = Task(
                workspace_id=workspace.id,
                assigned_agent_id=world.agent_id,
                title="Wrong workspace task",
                correlation_id=new_uuid7(),
            )
            session.add(task)
            await session.flush()
            run = await session.get(AgentRun, world.run_id)
            assert run is not None
            run.task_id = task.id
            await session.commit()
    elif invalid_case == "task-agent-mismatch":
        async with world.sessions() as session:
            agent = Agent(
                workspace_id=world.workspace_id,
                name="Wrong task agent",
                slug=f"wrong-task-agent-{new_uuid7().hex[:8]}",
            )
            session.add(agent)
            await session.flush()
            task = await session.get(Task, world.task_id)
            assert task is not None
            task.assigned_agent_id = agent.id
            await session.commit()
    elif invalid_case == "missing-agent-row":
        async with world.sessions() as session:
            run = await session.get(AgentRun, world.run_id)
            assert run is not None
            run.agent_id = new_uuid7()
            await session.commit()
    else:
        async with world.sessions() as session:
            workspace = Workspace(
                name="Wrong agent workspace",
                slug=f"wrong-agent-{new_uuid7().hex[:8]}",
            )
            session.add(workspace)
            await session.flush()
            agent = Agent(
                workspace_id=workspace.id,
                name="Wrong workspace agent",
                slug="wrong-workspace-agent",
            )
            session.add(agent)
            await session.flush()
            run = await session.get(AgentRun, world.run_id)
            assert run is not None
            run.agent_id = agent.id
            await session.commit()

    with pytest.raises(ApplicationError):
        await world.activities.execute_bound_tool_activity(params)

    assert world.effects == [], ctx
    assert await world.tool_call() is None, ctx
    assert world.activities.authority_calls == [], ctx
    assert _tool_spans(world.telemetry) == [], ctx
    assert _metric_points(world.telemetry, "tool_calls_total") == [], ctx
    assert _metric_points(world.telemetry, "tool_call_failures_total") == [], ctx


async def test_approval_span_begins_only_after_full_durable_and_manifest_validation() -> None:
    # Loop-folded: the exact former parametrize matrix, one isolated world per case.
    for invalid_case in (
        "invalid-workspace-uuid",
        "invalid-task-uuid",
        "invalid-run-uuid",
        "invalid-agent-uuid",
        "invalid-approval-uuid",
        "missing-approval",
        "missing-run-row",
        "missing-tool-call-row",
        "pending",
        "manifest-mismatch",
        "run-task-mismatch",
        "run-agent-mismatch",
        "task-workspace-mismatch",
        "task-agent-mismatch",
        "agent-workspace-mismatch",
        "approval-workspace-mismatch",
        "approval-task-mismatch",
        "approval-run-mismatch",
        "approval-requester-mismatch",
        "tool-call-workspace-mismatch",
        "tool-call-run-mismatch",
        "tool-call-agent-mismatch",
        "invalid-decision",
    ):
        ctx = f"invalid_case={invalid_case}"
        with _named_case(ctx):
            async with _fresh_world_context() as world:
                await _check_approval_span_prevalidation_case(
                    world,
                    invalid_case=invalid_case,
                    ctx=ctx,
                )


async def _check_approval_span_prevalidation_case(
    world: ToolWorld,
    *,
    invalid_case: str,
    ctx: str,
) -> None:
    if invalid_case.startswith("invalid-") and invalid_case.endswith("-uuid"):
        params = world.approval_params(new_uuid7())
        setattr(
            params,
            {
                "invalid-workspace-uuid": "workspace_id",
                "invalid-task-uuid": "task_id",
                "invalid-run-uuid": "run_id",
                "invalid-agent-uuid": "agent_id",
                "invalid-approval-uuid": "approval_id",
            }[invalid_case],
            "not-a-uuid",
        )
    elif invalid_case == "missing-approval":
        params = world.approval_params(new_uuid7())
    else:
        parked = await world.park()
        params = world.approval_params(parked.approval_id or "")
        if invalid_case == "manifest-mismatch":
            await world.seed_manifest("system.echo", value="changed")
        elif invalid_case == "missing-run-row":
            async with world.sessions() as session:
                await session.execute(delete(AgentRun).where(AgentRun.id == world.run_id))
                await session.commit()
        elif invalid_case == "missing-tool-call-row":
            async with world.sessions() as session:
                await session.execute(delete(ToolCall).where(ToolCall.id == world.invocation_id))
                await session.commit()
        elif invalid_case == "run-task-mismatch":
            async with world.sessions() as session:
                run = await session.get(AgentRun, world.run_id)
                assert run is not None
                run.task_id = new_uuid7()
                await session.commit()
        elif invalid_case == "run-agent-mismatch":
            async with world.sessions() as session:
                run = await session.get(AgentRun, world.run_id)
                assert run is not None
                run.agent_id = new_uuid7()
                await session.commit()
        elif invalid_case == "task-workspace-mismatch":
            async with world.sessions() as session:
                task = await session.get(Task, world.task_id)
                assert task is not None
                task.workspace_id = new_uuid7()
                await session.commit()
        elif invalid_case == "task-agent-mismatch":
            async with world.sessions() as session:
                task = await session.get(Task, world.task_id)
                assert task is not None
                task.assigned_agent_id = new_uuid7()
                await session.commit()
        elif invalid_case == "agent-workspace-mismatch":
            async with world.sessions() as session:
                agent = await session.get(Agent, world.agent_id)
                assert agent is not None
                agent.workspace_id = new_uuid7()
                await session.commit()
        elif invalid_case.startswith("approval-"):
            async with world.sessions() as session:
                approval = await session.get(Approval, UUID(parked.approval_id or ""))
                assert approval is not None
                setattr(
                    approval,
                    {
                        "approval-workspace-mismatch": "workspace_id",
                        "approval-task-mismatch": "task_id",
                        "approval-run-mismatch": "run_id",
                        "approval-requester-mismatch": "requested_by_agent_id",
                    }[invalid_case],
                    new_uuid7(),
                )
                await session.commit()
        elif invalid_case.startswith("tool-call-"):
            async with world.sessions() as session:
                row = await session.get(ToolCall, world.invocation_id)
                assert row is not None
                setattr(
                    row,
                    {
                        "tool-call-workspace-mismatch": "workspace_id",
                        "tool-call-run-mismatch": "run_id",
                        "tool-call-agent-mismatch": "agent_id",
                    }[invalid_case],
                    new_uuid7(),
                )
                await session.commit()
        elif invalid_case == "invalid-decision":
            async with world.sessions() as session:
                approval = await session.get(Approval, UUID(parked.approval_id or ""))
                assert approval is not None
                approval.status = "private-invalid-decision"
                await session.commit()
        world.telemetry.exporter.clear()
        world.activities.authority_calls.clear()

    with pytest.raises(ApplicationError):
        await world.activities.resolve_bound_tool_approval_activity(params)

    assert world.effects == [], ctx
    assert world.activities.authority_calls == [], ctx
    assert _tool_spans(world.telemetry) == [], ctx
    assert _metric_points(world.telemetry, "tool_calls_total") == [], ctx
    assert _metric_points(world.telemetry, "tool_call_failures_total") == [], ctx


async def test_hostile_postcommit_reload_preserves_exact_product_authority() -> None:
    # Loop-folded: the exact former parametrize cross-product, one isolated
    # world and monkeypatch scope per case.
    for diagnostic_type in (RuntimeError, ValueError, KeyError, AttributeError, _WorkerDiagnostic):
        for activity_kind, product_kind in (
            ("execute", "success"),
            ("execute", "failure"),
            ("approval", "success"),
            ("approval", "failure"),
        ):
            ctx = (
                f"diagnostic_type={diagnostic_type.__name__}"
                f" activity_kind={activity_kind} product_kind={product_kind}"
            )
            with _named_case(ctx):
                async with _fresh_world_context() as world:
                    with pytest.MonkeyPatch.context() as monkeypatch:
                        await _check_hostile_postcommit_reload_case(
                            world,
                            monkeypatch,
                            activity_kind=activity_kind,
                            product_kind=product_kind,
                            diagnostic_type=diagnostic_type,
                            ctx=ctx,
                        )


async def _check_hostile_postcommit_reload_case(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    activity_kind: str,
    product_kind: str,
    diagnostic_type: type[Exception],
    ctx: str,
) -> None:
    control = await world.clone_isolated()
    bound_results: list[BoundToolResult] = []
    raised_errors: list[tuple[ApplicationError, TracebackType | None]] = []
    original_bound_result = activities_module._bound_result
    original_raise_failure = activities_module._raise_ordinary_failure

    def observed_bound_result(outcome: object) -> BoundToolResult:
        result = original_bound_result(cast(Any, outcome))
        bound_results.append(result)
        return result

    def observed_raise_failure(outcome: object) -> None:
        try:
            original_raise_failure(cast(Any, outcome))
        except ApplicationError as error:
            raised_errors.append((error, error.__traceback__))
            raise

    monkeypatch.setattr(activities_module, "_bound_result", observed_bound_result)
    monkeypatch.setattr(activities_module, "_raise_ordinary_failure", observed_raise_failure)

    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    control_effect_count = len(world.effects)
    control_result, control_error, control_frames = await _invoke_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
        parked=control_parked,
    )
    control_effect_delta = len(world.effects) - control_effect_count
    control_product_snapshot = await control.product_snapshot()
    control_snapshot = _canonical_product_snapshot(control, control_product_snapshot)

    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    before_points = {
        name: len(_metric_points(world.telemetry, name))
        for name in ("tool_calls_total", "tool_call_failures_total")
    }
    before_effect_count = len(world.effects)
    _ProbeSession.activity_commit_callers.clear()
    world.activities.authority_failure = diagnostic_type("private-diagnostic-reload")

    result, error, frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
        parked=parked,
    )
    target_product_snapshot = await world.product_snapshot()

    if control_error is not None:
        assert error is not None, ctx
        assert result is None, ctx
        assert _application_error_public(error) == _application_error_public(control_error), ctx
        assert frames == control_frames, ctx
        assert raised_errors[-1][0] is error, ctx
        assert _traceback_tail(error.__traceback__) is _traceback_tail(raised_errors[-1][1]), ctx
    else:
        assert control_result is not None, ctx
        assert result is not None, ctx
        assert error is None, ctx
        assert result is bound_results[-1], ctx
        target_approval_authority = _bound_result_approval_authority(
            result,
            parked,
            target_product_snapshot,
        )
        control_approval_authority = _bound_result_approval_authority(
            control_result,
            control_parked,
            control_product_snapshot,
        )
        assert _canonical_product_snapshot(
            world,
            {"result": asdict(result)},
            result_approval_authority=target_approval_authority,
        ) == _canonical_product_snapshot(
            control,
            {"result": asdict(control_result)},
            result_approval_authority=control_approval_authority,
        ), ctx

    assert _canonical_product_snapshot(world, target_product_snapshot) == control_snapshot, ctx
    assert len(world.effects) - before_effect_count == control_effect_delta, ctx
    assert _ProbeSession.activity_commit_callers == [
        "execute_bound_tool_activity"
        if activity_kind == "execute"
        else "resolve_bound_tool_approval_activity"
    ], ctx
    assert len(world.activities.authority_calls) == 1, ctx
    assert {
        name: len(_metric_points(world.telemetry, name))
        for name in ("tool_calls_total", "tool_call_failures_total")
    } == before_points, ctx
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1, ctx
    assert spans[0].attributes["jhin.outcome"] == "other", ctx
    assert spans[0].status.status_code is StatusCode.UNSET, ctx
    assert not ({"error.code", "error.type"} & set(spans[0].attributes)), ctx


async def test_postcommit_reload_cancellation_and_fatal_authority_propagate_exactly() -> None:
    # Loop-folded: the exact former parametrize cross-product, one isolated
    # world per case.
    for failure_type in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        for activity_kind, product_kind in (
            ("execute", "success"),
            ("execute", "failure"),
            ("approval", "success"),
            ("approval", "failure"),
        ):
            ctx = (
                f"failure_type={failure_type.__name__}"
                f" activity_kind={activity_kind} product_kind={product_kind}"
            )
            with _named_case(ctx):
                async with _fresh_world_context() as world:
                    await _check_postcommit_reload_fatal_case(
                        world,
                        failure_type=failure_type,
                        activity_kind=activity_kind,
                        product_kind=product_kind,
                        ctx=ctx,
                    )


async def _check_postcommit_reload_fatal_case(
    world: ToolWorld,
    *,
    failure_type: type[BaseException],
    activity_kind: str,
    product_kind: str,
    ctx: str,
) -> None:
    control = await world.clone_isolated()
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    control_effect_count = len(world.effects)
    await _invoke_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
        parked=control_parked,
    )
    control_effect_delta = len(world.effects) - control_effect_count
    control_product_snapshot = await control.product_snapshot()
    control_snapshot = _canonical_product_snapshot(control, control_product_snapshot)

    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    before_points = {
        name: len(_metric_points(world.telemetry, name))
        for name in ("tool_calls_total", "tool_call_failures_total")
    }
    before_effect_count = len(world.effects)
    _ProbeSession.activity_commit_callers.clear()
    failure = failure_type("postcommit-reload-authority")
    world.activities.authority_failure = failure

    with pytest.raises(failure_type) as caught:
        if activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )

    assert caught.value is failure, ctx
    assert world.activities.authority_raised_traceback is not None, ctx
    assert _traceback_tail(caught.value.__traceback__) is (
        world.activities.authority_raised_traceback
    ), ctx
    assert (
        _traceback_frame_names(caught.value.__traceback__).count("_load_tool_telemetry_authority")
        == 1
    ), ctx
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == (
        control_snapshot
    ), ctx
    assert len(world.effects) - before_effect_count == control_effect_delta, ctx
    assert _ProbeSession.activity_commit_callers == [
        "execute_bound_tool_activity"
        if activity_kind == "execute"
        else "resolve_bound_tool_approval_activity"
    ], ctx
    assert {
        name: len(_metric_points(world.telemetry, name))
        for name in ("tool_calls_total", "tool_call_failures_total")
    } == before_points, ctx
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1, ctx
    assert spans[0].attributes["jhin.outcome"] == "other", ctx
    assert spans[0].status.status_code is StatusCode.UNSET, ctx
    assert not ({"error.code", "error.type"} & set(spans[0].attributes)), ctx


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_product_cancellation_is_exact_and_closes_one_cancelled_span_without_counters(
    world: ToolWorld,
    activity_kind: str,
) -> None:
    control = await world.clone_isolated()
    control.resources = SimpleNamespace(
        runtime=SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer()),
        session_factory=world.sessions,
        crypto=None,
        test_barrier=None,
        telemetry=world.telemetry,
    )
    control.activities = _ProbeToolActivities(control.resources, control.catalog)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    control_cancellation = asyncio.CancelledError("control-product-cancellation")
    product_cancellation = asyncio.CancelledError("owned-product-cancellation")
    owners = {
        control.run_id: _OwnedFailure(control_cancellation),
        world.run_id: _OwnedFailure(product_cancellation),
    }

    async def cancel_executor(
        context: ToolExecutionContext,
        _payload: BaseModel,
    ) -> BaseModel:
        owners[context.run_id].raise_owned()
        raise AssertionError("unreachable")

    tool_name = "system.echo" if activity_kind == "execute" else "system.approval"
    world.catalog._executors[tool_name] = cancel_executor

    with pytest.raises(asyncio.CancelledError) as control_caught:
        if activity_kind == "execute":
            await control.activities.execute_bound_tool_activity(control.execute_params())
        else:
            assert control_parked is not None
            await control.activities.resolve_bound_tool_approval_activity(
                control.approval_params(control_parked.approval_id or "")
            )
    assert control_caught.value is control_cancellation
    control_product_snapshot = await control.product_snapshot()
    control_snapshot = _canonical_product_snapshot(control, control_product_snapshot)
    world.telemetry.exporter.clear()

    with pytest.raises(asyncio.CancelledError) as caught:
        if activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )

    assert caught.value is product_cancellation
    assert owners[world.run_id].raised_traceback is not None
    assert _traceback_tail(caught.value.__traceback__) is owners[world.run_id].raised_traceback
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert _metric_points(world.telemetry, "tool_calls_total") == []
    assert _metric_points(world.telemetry, "tool_call_failures_total") == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    span = spans[0]
    assert dict(span.attributes) == {
        "jhin.tool_family": "system",
        "jhin.risk": "write" if activity_kind == "execute" else "elevated",
        "jhin.outcome": "cancelled",
    }
    assert span.status.status_code is StatusCode.UNSET
    assert span.events == ()


@pytest.mark.parametrize(
    ("activity_kind", "product_kind"),
    [
        ("execute", "success"),
        ("execute", "failure"),
        ("approval", "success"),
        ("approval", "failure"),
    ],
)
async def test_activity_gateway_commit_failure_preserves_existing_authority_and_zero_metrics(
    world: ToolWorld,
    activity_kind: str,
    product_kind: str,
) -> None:
    parked: BoundToolResult | None = None
    if activity_kind == "execute":
        await world.seed_manifest("system.echo" if product_kind == "success" else "system.fail")
    else:
        parked = await world.park()
        await world.decide(
            parked.approval_id or "",
            ApprovalStatus.APPROVED.value
            if product_kind == "success"
            else ApprovalStatus.REJECTED.value,
        )
        world.telemetry.exporter.clear()
    failure = RuntimeError("private-activity-commit-failure")
    _ProbeSession.fail_activity_commit = failure

    with pytest.raises(RuntimeError) as caught:
        if activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )

    assert caught.value is failure
    assert _ProbeSession.commit_raised_traceback is not None
    assert _traceback_tail(caught.value.__traceback__) is _ProbeSession.commit_raised_traceback
    assert _metric_points(world.telemetry, "tool_calls_total") == []
    assert _metric_points(world.telemetry, "tool_call_failures_total") == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert spans[0].attributes["jhin.outcome"] == "other"
    assert spans[0].status.status_code is StatusCode.UNSET
    assert spans[0].events == ()
    assert not ({"error.code", "error.type"} & set(spans[0].attributes))


def test_activity_uses_the_pure_package_description_without_shadow_mapping_tables() -> None:
    source = Path(inspect.getsourcefile(ToolActivities) or "").read_text()
    tree = ast.parse(source)
    imported = {
        (alias.name, alias.asname)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "jhin_tools.telemetry"
        for alias in node.names
    }
    assert {
        ("ToolTelemetryDescription", None),
        ("_tool_status_authority", None),
        ("describe_tool_telemetry", None),
    } <= imported
    mapper_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "describe_tool_telemetry"
    ]
    assert len(mapper_calls) == 1
    status_authority_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_tool_status_authority"
    ]
    assert len(status_authority_calls) == 1
    parent_by_child = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    assert not isinstance(parent_by_child[mapper_calls[0]], ast.Expr)
    assert not isinstance(parent_by_child[status_authority_calls[0]], ast.Expr)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "describe_tool_telemetry"
        for node in ast.walk(tree)
    )
    assigned_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert (
        not {
            "_OUTCOME_TO_ROW_STATUS",
            "_OUTCOME_TO_TELEMETRY_OUTCOME",
            "_OUTCOME_TO_FAILURE_CLASS",
        }
        & assigned_names
    )
    closed_gateway_values = {
        "executed",
        "failed",
        "denied",
        "rejected",
        "execution_unknown",
        "needs_approval",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        values = {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and type(child.value) is str
        }
        assert len(values & closed_gateway_values) < 2, "worker cannot shadow package maps"


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_activity_calls_the_package_mapper_once_only_after_fresh_durable_proof(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    package_telemetry = importlib.import_module("jhin_tools.telemetry")
    real_mapper = package_telemetry.describe_tool_telemetry
    calls: list[tuple[object, object, object, int, int, tuple[str, ...]]] = []
    catalog_lookups: list[tuple[object, int]] = []
    original_get = world.catalog.get
    effect_baseline = len(world.effects)

    def observed_get(name: object) -> object:
        catalog_lookups.append((name, len(world.effects)))
        return original_get(cast(Any, name))

    world.catalog.get = cast(Any, observed_get)

    def observed_mapper(catalog: object, tool_name: object, gateway_status: object) -> object:
        calls.append(
            (
                catalog,
                tool_name,
                gateway_status,
                len(world.activities.authority_calls),
                len(world.activities.authority_fresh_session_deltas),
                tuple(_ProbeSession.activity_commit_callers),
            )
        )
        return real_mapper(catalog, tool_name, gateway_status)

    monkeypatch.setattr(activities_module, "describe_tool_telemetry", observed_mapper)

    result, error, _frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )

    assert error is None
    assert result is not None
    assert len(calls) == 1
    catalog, tool_name, gateway_status, proof_count, fresh_count, commits = calls[0]
    assert catalog is world.catalog
    assert tool_name == ("system.echo" if activity_kind == "execute" else "system.approval")
    assert gateway_status == "executed"
    assert proof_count == 1
    assert fresh_count == 1
    assert commits[-1] == (
        "execute_bound_tool_activity"
        if activity_kind == "execute"
        else "resolve_bound_tool_approval_activity"
    )
    assert [name for name, effect_count in catalog_lookups if effect_count > effect_baseline] == [
        tool_name
    ]


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
@pytest.mark.parametrize("sentinel_case", ["terminal-failure", "nonterminal", "row-mismatch"])
async def test_every_package_description_field_drives_worker_span_and_metric_authority(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
    sentinel_case: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    package_telemetry = importlib.import_module("jhin_tools.telemetry")
    description_type = package_telemetry.ToolTelemetryDescription
    if sentinel_case == "terminal-failure":
        sentinel = description_type(
            tool_family="github",
            risk="destructive",
            expected_row_status="completed",
            outcome="denied",
            failure_class="policy",
            terminal_countable=True,
        )
    elif sentinel_case == "nonterminal":
        sentinel = description_type(
            tool_family="github",
            risk="destructive",
            expected_row_status="completed",
            outcome="accepted",
            failure_class=None,
            terminal_countable=False,
        )
    else:
        sentinel = description_type(
            tool_family="github",
            risk="destructive",
            expected_row_status="failed",
            outcome="denied",
            failure_class="policy",
            terminal_countable=True,
        )
    mapper_calls: list[tuple[object, object, object]] = []

    def sentinel_mapper(catalog: object, tool_name: object, status: object) -> object:
        mapper_calls.append((catalog, tool_name, status))
        return sentinel

    monkeypatch.setattr(activities_module, "describe_tool_telemetry", sentinel_mapper)

    result, error, _frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
        parked=parked,
    )

    assert error is None
    assert result is not None
    assert result.status == "executed"
    assert mapper_calls == [
        (
            world.catalog,
            "system.echo" if activity_kind == "execute" else "system.approval",
            "executed",
        )
    ]
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    if sentinel_case == "terminal-failure":
        assert {
            key: value
            for key, value in dict(spans[0].attributes).items()
            if not key.startswith("error.")
        } == {
            "jhin.tool_family": "github",
            "jhin.risk": "destructive",
            "jhin.outcome": "denied",
        }
        assert spans[0].status.status_code is StatusCode.ERROR
        assert _metric_point_multiset(world.telemetry) == [
            (
                "tool_call_failures_total",
                (("failure_class", "policy"), ("tool_family", "github")),
                1,
            ),
            (
                "tool_calls_total",
                (
                    ("outcome", "denied"),
                    ("risk", "destructive"),
                    ("tool_family", "github"),
                ),
                1,
            ),
        ]
    else:
        expected_outcome = "accepted" if sentinel_case == "nonterminal" else "other"
        assert dict(spans[0].attributes) == {
            "jhin.tool_family": "github" if sentinel_case == "nonterminal" else "other",
            "jhin.risk": "destructive" if sentinel_case == "nonterminal" else "other",
            "jhin.outcome": expected_outcome,
        }
        assert spans[0].status.status_code is StatusCode.UNSET
        assert _metric_point_multiset(world.telemetry) == []


async def test_ordinary_package_mapper_failure_is_unproven_diagnostic_fallback() -> None:
    # Loop-folded: the exact former parametrize cross-product, one isolated
    # world and monkeypatch scope per case.
    for diagnostic_type in (RuntimeError, ValueError, KeyError, AttributeError, _WorkerDiagnostic):
        for activity_kind, product_kind in (
            ("execute", "success"),
            ("execute", "failure"),
            ("approval", "success"),
            ("approval", "failure"),
        ):
            ctx = (
                f"diagnostic_type={diagnostic_type.__name__}"
                f" activity_kind={activity_kind} product_kind={product_kind}"
            )
            with _named_case(ctx):
                async with _fresh_world_context() as world:
                    with pytest.MonkeyPatch.context() as monkeypatch:
                        await _check_package_mapper_diagnostic_case(
                            world,
                            monkeypatch,
                            activity_kind=activity_kind,
                            product_kind=product_kind,
                            diagnostic_type=diagnostic_type,
                            ctx=ctx,
                        )


async def _check_package_mapper_diagnostic_case(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    activity_kind: str,
    product_kind: str,
    diagnostic_type: type[Exception],
    ctx: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    bound_results: dict[UUID, list[BoundToolResult]] = {}
    raised_errors: dict[UUID, list[tuple[ApplicationError, TracebackType | None]]] = {}
    original_bound_result = activities_module._bound_result
    original_raise_failure = activities_module._raise_ordinary_failure

    def observed_bound_result(outcome: object) -> BoundToolResult:
        result = original_bound_result(cast(Any, outcome))
        bound_results.setdefault(cast(Any, outcome).tool_call_id, []).append(result)
        return result

    def observed_raise_failure(outcome: object) -> None:
        try:
            original_raise_failure(cast(Any, outcome))
        except ApplicationError as error:
            raised_errors.setdefault(cast(Any, outcome).tool_call_id, []).append(
                (error, error.__traceback__)
            )
            raise

    monkeypatch.setattr(activities_module, "_bound_result", observed_bound_result)
    monkeypatch.setattr(activities_module, "_raise_ordinary_failure", observed_raise_failure)
    control_effects_before = len(world.effects)
    control_commits_before = len(_ProbeSession.activity_commit_callers)
    control_result, control_error, control_frames = await _invoke_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
        parked=control_parked,
    )
    control_effect_delta = len(world.effects) - control_effects_before
    expected_commit_owner = (
        "execute_bound_tool_activity"
        if activity_kind == "execute"
        else "resolve_bound_tool_approval_activity"
    )
    assert _ProbeSession.activity_commit_callers[control_commits_before:] == (
        [expected_commit_owner]
    ), ctx
    control_product_snapshot = await control.product_snapshot()
    control_snapshot = _canonical_product_snapshot(control, control_product_snapshot)
    world.telemetry.exporter.clear()
    mapper_calls = 0

    def fail_mapper(_catalog: object, _tool_name: object, _gateway_status: object) -> object:
        nonlocal mapper_calls
        mapper_calls += 1
        raise diagnostic_type("private-package-mapper-diagnostic")

    monkeypatch.setattr(activities_module, "describe_tool_telemetry", fail_mapper, raising=False)

    target_effects_before = len(world.effects)
    target_commits_before = len(_ProbeSession.activity_commit_callers)
    result, error, frames = await _invoke_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
        parked=parked,
    )
    target_product_snapshot = await world.product_snapshot()

    assert mapper_calls == 1, ctx
    if control_error is not None:
        assert result is None, ctx
        assert error is not None, ctx
        assert _application_error_public(error) == _application_error_public(control_error), ctx
        assert frames == control_frames, ctx
        owned_error, owned_traceback = raised_errors[world.invocation_id][-1]
        assert error is owned_error, ctx
        assert _traceback_tail(error.__traceback__) is _traceback_tail(owned_traceback), ctx
    else:
        assert control_result is not None, ctx
        assert error is None, ctx
        assert result is not None, ctx
        assert result is bound_results[world.invocation_id][-1], ctx
        target_approval_authority = _bound_result_approval_authority(
            result,
            parked,
            target_product_snapshot,
        )
        control_approval_authority = _bound_result_approval_authority(
            control_result,
            control_parked,
            control_product_snapshot,
        )
        assert _canonical_product_snapshot(
            world,
            {"result": asdict(result)},
            result_approval_authority=target_approval_authority,
        ) == _canonical_product_snapshot(
            control,
            {"result": asdict(control_result)},
            result_approval_authority=control_approval_authority,
        ), ctx
    assert _canonical_product_snapshot(world, target_product_snapshot) == control_snapshot, ctx
    assert len(world.effects) - target_effects_before == control_effect_delta, ctx
    assert _ProbeSession.activity_commit_callers[target_commits_before:] == (
        [expected_commit_owner]
    ), ctx
    assert _metric_point_multiset(world.telemetry) == [], ctx
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1, ctx
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }, ctx
    assert spans[0].status.status_code is StatusCode.UNSET, ctx
    assert spans[0].events == (), ctx
    assert spans[0].links == (), ctx


async def test_package_mapper_cancellation_and_fatal_authority_propagate_exactly() -> None:
    # Loop-folded: the exact former parametrize cross-product, one isolated
    # world and monkeypatch scope per case.
    for product_kind in ("success", "failure"):
        for activity_kind in ("execute", "approval"):
            for failure_type in (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                ctx = (
                    f"product_kind={product_kind} activity_kind={activity_kind}"
                    f" failure_type={failure_type.__name__}"
                )
                with _named_case(ctx):
                    async with _fresh_world_context() as world:
                        with pytest.MonkeyPatch.context() as monkeypatch:
                            await _check_package_mapper_fatal_case(
                                world,
                                monkeypatch,
                                failure_type=failure_type,
                                activity_kind=activity_kind,
                                product_kind=product_kind,
                                ctx=ctx,
                            )


async def _check_package_mapper_fatal_case(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure_type: type[BaseException],
    activity_kind: str,
    product_kind: str,
    ctx: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    failure = failure_type("package-mapper-authority")
    raised_traceback: TracebackType | None = None

    def fail_mapper(_catalog: object, _tool_name: object, _status: object) -> object:
        nonlocal raised_traceback
        try:
            raise failure
        except BaseException as error:
            raised_traceback = error.__traceback__
            raise

    monkeypatch.setattr(activities_module, "describe_tool_telemetry", fail_mapper, raising=False)

    with pytest.raises(failure_type) as caught:
        if activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )

    assert caught.value is failure, ctx
    assert raised_traceback is not None, ctx
    assert _traceback_tail(caught.value.__traceback__) is raised_traceback, ctx
    assert _traceback_frame_names(caught.value.__traceback__).count("fail_mapper") == 1, ctx
    row = await world.tool_call()
    assert row is not None, ctx
    assert (
        row.status
        == {
            ("execute", "success"): ToolCallStatus.COMPLETED.value,
            ("execute", "failure"): ToolCallStatus.FAILED.value,
            ("approval", "success"): ToolCallStatus.COMPLETED.value,
            ("approval", "failure"): ToolCallStatus.REJECTED.value,
        }[(activity_kind, product_kind)]
    ), ctx
    assert _metric_point_multiset(world.telemetry) == [], ctx
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1, ctx
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }, ctx
    assert spans[0].status.status_code is StatusCode.UNSET, ctx


_FIXED_TOOL_SCHEMA_INVALID_CASES: list[tuple[str, object]] = [
    ("_TOOL_EXECUTE_SPAN_NAME", "unregistered.span"),
    ("_TOOL_APPROVAL_SPAN_NAME", "unregistered.span"),
    ("_TOOL_EXECUTE_SPAN_NAME", "tool.approval.resolve"),
    ("_TOOL_APPROVAL_SPAN_NAME", "tool.gateway.execute"),
    ("_TOOL_FAMILY_ATTRIBUTE", "unregistered.attribute"),
    ("_TOOL_RISK_ATTRIBUTE", "unregistered.attribute"),
    ("_TOOL_OUTCOME_ATTRIBUTE", "unregistered.attribute"),
    ("_TOOL_FAMILY_ATTRIBUTE", "jhin.risk"),
    ("_TOOL_RISK_ATTRIBUTE", "jhin.outcome"),
    ("_TOOL_OUTCOME_ATTRIBUTE", "jhin.tool_family"),
    ("_TOOL_CALLS_METRIC", "unregistered_metric"),
    ("_TOOL_FAILURES_METRIC", "unregistered_metric"),
    ("_TOOL_CALLS_METRIC", "tool_call_failures_total"),
    ("_TOOL_FAILURES_METRIC", "tool_calls_total"),
    ("_TOOL_FAMILY_LABEL", "workspace_id"),
    ("_TOOL_RISK_LABEL", "workspace_id"),
    ("_TOOL_OUTCOME_LABEL", "workspace_id"),
    ("_TOOL_FAILURE_LABEL", "workspace_id"),
    ("_TOOL_FAMILY_LABEL", "risk"),
    ("_TOOL_RISK_LABEL", "outcome"),
    ("_TOOL_OUTCOME_LABEL", "tool_family"),
    ("_TOOL_FAILURE_LABEL", "risk"),
    ("_TOOL_MEASUREMENT", True),
    ("_TOOL_MEASUREMENT", 0),
    ("_TOOL_MEASUREMENT", 2),
    ("_TOOL_MEASUREMENT", 1.0),
    ("_TOOL_ROW_COMPLETED", "failed"),
    ("_TOOL_ROW_FAILED", "completed"),
    ("_TOOL_ROW_DENIED", "rejected"),
    ("_TOOL_ROW_REJECTED", "denied"),
    ("_TOOL_ROW_EXECUTION_UNKNOWN", "executing"),
    ("_TOOL_ROW_PENDING_APPROVAL", "completed"),
    ("_TOOL_OUTCOME_COMPLETED", "healthy"),
    ("_TOOL_OUTCOME_ACCEPTED", "started"),
    ("_TOOL_OUTCOME_FAILED", "timeout"),
    ("_TOOL_OUTCOME_DENIED", "rejected"),
    ("_TOOL_OUTCOME_REJECTED", "denied"),
    ("_TOOL_OUTCOME_EXECUTION_UNKNOWN", "other"),
    ("_TOOL_OUTCOME_OTHER", "completed"),
    ("_TOOL_OUTCOME_CANCELLED", "failed"),
    ("_TOOL_FAILURE_INTERNAL", "other"),
    ("_TOOL_FAILURE_POLICY", "authorization"),
    ("_TOOL_FAILURE_EXECUTION_UNKNOWN", "internal"),
    ("_TOOL_FAILURE_INTERNAL", "policy"),
    ("_TOOL_FAILURE_POLICY", "execution_unknown"),
    ("_TOOL_ERROR_TYPE_ATTRIBUTE", "error.kind"),
    ("_TOOL_ERROR_CODE_ATTRIBUTE", "error.reason"),
    ("_TOOL_ERROR_TYPE_ATTRIBUTE", "error.code"),
    ("_TOOL_ERROR_CODE_ATTRIBUTE", "error.type"),
    ("_TOOL_ERROR_TYPE_VALUE", "private invalid type"),
    ("_TOOL_ERROR_TYPE_VALUE", "RuntimeError"),
    ("_TOOL_INTERNAL_ERROR_CODE", "other"),
    ("_TOOL_POLICY_ERROR_CODE", "policy"),
    ("_TOOL_EXECUTION_UNKNOWN_ERROR_CODE", "unknown"),
    ("_TOOL_INTERNAL_ERROR_CODE", SafeErrorCode.TIMEOUT.value),
    ("_TOOL_POLICY_ERROR_CODE", SafeErrorCode.CONFLICT.value),
    (
        "_TOOL_EXECUTION_UNKNOWN_ERROR_CODE",
        SafeErrorCode.INTERNAL_ERROR.value,
    ),
]


async def test_invalid_fixed_tool_schema_fails_before_product_db_or_backend() -> None:
    # Loop-folded: the exact former parametrize cross-product, one isolated
    # world and monkeypatch scope per case. The guard test
    # test_fixed_tool_schema_parameters_have_one_explicit_constant_owner proves
    # this loop iterates _FIXED_TOOL_SCHEMA_INVALID_CASES and that the list
    # covers every owned schema constant exactly once.
    for activity_kind in ("execute", "approval"):
        for constant, invalid in _FIXED_TOOL_SCHEMA_INVALID_CASES:
            ctx = f"activity_kind={activity_kind} constant={constant} invalid={invalid!r}"
            with _named_case(ctx):
                async with _fresh_world_context() as world:
                    with pytest.MonkeyPatch.context() as monkeypatch:
                        await _check_invalid_fixed_tool_schema_case(
                            world,
                            monkeypatch,
                            constant=constant,
                            invalid=invalid,
                            activity_kind=activity_kind,
                            ctx=ctx,
                        )


async def _check_invalid_fixed_tool_schema_case(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    constant: str,
    invalid: object,
    activity_kind: str,
    ctx: str,
) -> None:
    parked: BoundToolResult | None = None
    if activity_kind == "approval":
        parked = await world.park()
        await world.decide(parked.approval_id or "", ApprovalStatus.APPROVED.value)
        world.telemetry.exporter.clear()
        world.activities.authority_calls.clear()
    before = await world.product_snapshot()
    effects_before = list(world.effects)
    session_count_before = len(_ProbeSession.created_session_ids)
    commit_count_before = len(_ProbeSession.activity_commit_callers)
    metrics_trap = _PreproductBackendTrap()
    tracer_trap = _PreproductBackendTrap()
    world.activities._metrics = cast(Any, metrics_trap)
    world.activities._tracer = cast(Any, tracer_trap)
    owner = (
        importlib.import_module("jhin_tools.telemetry")
        if constant in _PACKAGE_OWNED_TOOL_SCHEMA_CONSTANTS
        else activities_module
    )
    monkeypatch.setattr(owner, constant, invalid, raising=False)

    with pytest.raises((TypeError, ValueError)):
        if activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )

    assert world.effects == effects_before, ctx
    assert len(_ProbeSession.created_session_ids) == session_count_before, ctx
    assert len(_ProbeSession.activity_commit_callers) == commit_count_before, ctx
    assert metrics_trap.accesses == [], ctx
    assert tracer_trap.accesses == [], ctx
    assert world.activities.authority_calls == [], ctx
    assert _metric_points(world.telemetry, "tool_calls_total") == [], ctx
    assert _metric_points(world.telemetry, "tool_call_failures_total") == [], ctx
    assert _tool_spans(world.telemetry) == [], ctx
    assert await world.product_snapshot() == before, ctx
    assert len(_ProbeSession.created_session_ids) == session_count_before + 1, ctx


def test_fixed_tool_schema_parameters_have_one_explicit_constant_owner() -> None:
    assert _PACKAGE_OWNED_TOOL_SCHEMA_CONSTANTS.isdisjoint(_ACTIVITY_OWNED_TOOL_SCHEMA_CONSTANTS)
    # The loop-folded test must iterate the shared module-level case list, so
    # reading that list here reads the exact set of exercised constants.
    source = inspect.getsource(test_invalid_fixed_tool_schema_fails_before_product_db_or_backend)
    function = cast(ast.AsyncFunctionDef, ast.parse(textwrap.dedent(source)).body[0])
    loop_iterated_names = {
        node.iter.id
        for node in ast.walk(function)
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Name)
    }
    assert "_FIXED_TOOL_SCHEMA_INVALID_CASES" in loop_iterated_names
    parameter_names = [constant for constant, _invalid in _FIXED_TOOL_SCHEMA_INVALID_CASES]
    assert parameter_names
    owners = _PACKAGE_OWNED_TOOL_SCHEMA_CONSTANTS | _ACTIVITY_OWNED_TOOL_SCHEMA_CONSTANTS
    assert set(parameter_names) == owners
    assert all(
        int(name in _PACKAGE_OWNED_TOOL_SCHEMA_CONSTANTS)
        + int(name in _ACTIVITY_OWNED_TOOL_SCHEMA_CONSTANTS)
        == 1
        for name in parameter_names
    )


def test_both_activity_entrypoints_prevalidate_before_any_product_expression() -> None:
    for method_name in (
        "execute_bound_tool_activity",
        "resolve_bound_tool_approval_activity",
    ):
        source = inspect.getsource(getattr(ToolActivities, method_name))
        function = cast(ast.AsyncFunctionDef, ast.parse(textwrap.dedent(source)).body[0])
        first = function.body[0]
        value = first.value if isinstance(first, (ast.Assign, ast.Expr)) else None
        if isinstance(value, ast.Await):
            value = value.value
        assert isinstance(value, ast.Call)
        assert isinstance(value.func, ast.Name)
        assert value.func.id == "_prevalidate_tool_telemetry_schema"


def test_tool_worker_declares_only_the_two_registered_counter_instruments() -> None:
    tree = ast.parse(Path(inspect.getsourcefile(ToolActivities) or "").read_text())
    allowed = {"_TOOL_CALLS_METRIC", "_TOOL_FAILURES_METRIC"}
    getters = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"counter", "histogram", "set_observable"}
    ]
    assert getters
    for getter in getters:
        assert getter.func.attr == "counter"
        assert len(getter.args) == 1
        assert isinstance(getter.args[0], ast.Name)
        assert getter.args[0].id in allowed
    assigned_metric_names = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) is str
        and (target.id in allowed or node.value.value.endswith(("_total", "_seconds")))
    }
    assert assigned_metric_names == {
        "_TOOL_CALLS_METRIC": "tool_calls_total",
        "_TOOL_FAILURES_METRIC": "tool_call_failures_total",
    }


class _OwnedFailure:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.raised_traceback: TracebackType | None = None

    def raise_owned(self) -> None:
        try:
            raise self.failure
        except BaseException as error:
            self.raised_traceback = error.__traceback__
            raise


def _assert_exact_owned_failure(
    caught: BaseException,
    owner: _OwnedFailure,
) -> None:
    assert caught is owner.failure
    assert owner.raised_traceback is not None
    assert _traceback_tail(caught.__traceback__) is owner.raised_traceback
    frames = _traceback_frame_names(caught.__traceback__)
    owned_frames = _traceback_frame_names(owner.raised_traceback)
    assert frames[-len(owned_frames) :] == owned_frames
    assert frames.count("raise_owned") == 1


class _HostileInstrument:
    def __init__(self, wrapped: Any, owner: _OwnedFailure, *, fail_write: bool) -> None:
        self.wrapped = wrapped
        self.owner = owner
        self.fail_write = fail_write
        self.calls = 0

    def add(self, amount: int | float, **labels: str) -> None:
        self.calls += 1
        if self.fail_write:
            self.owner.raise_owned()
        self.wrapped.add(amount, **labels)


class _HostileMetrics:
    is_noop = False

    def __init__(
        self,
        wrapped: JhinMetrics,
        owner: _OwnedFailure,
        *,
        target: str,
        phase: str,
    ) -> None:
        self.wrapped = wrapped
        self.owner = owner
        self.target = target
        self.phase = phase
        self.getter_calls: list[str] = []
        self.instrument: _HostileInstrument | None = None

    def counter(self, name: str) -> _HostileInstrument:
        self.getter_calls.append(name)
        if name == self.target and self.phase == "getter":
            self.owner.raise_owned()
        instrument = _HostileInstrument(
            self.wrapped.counter(cast(Any, name)),
            self.owner,
            fail_write=name == self.target and self.phase == "write",
        )
        if name == self.target:
            self.instrument = instrument
        return instrument

    def histogram(self, name: str) -> Any:
        return self.wrapped.histogram(cast(Any, name))

    def set_observable(self, name: str, observations: object) -> None:
        self.wrapped.set_observable(cast(Any, name), cast(Any, observations))


class _HostileSpan(trace.Span):
    def __init__(self, owner: _OwnedFailure, phase: str) -> None:
        self.owner = owner
        self.phase = phase
        self.attributes: dict[str, AttributeValue] = {}
        self.attribute_calls: list[tuple[str, AttributeValue]] = []
        self.status_calls: list[tuple[Status | StatusCode, str | None]] = []
        self.event_calls: list[tuple[str, Attributes, int | None]] = []
        self.name_calls: list[str] = []
        self.exception_calls: list[tuple[BaseException, Attributes, int | None, bool]] = []
        self.end_calls = 0

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self.attribute_calls.append((key, value))
        if self.phase == "late_attribute" and key == "jhin.outcome":
            self.owner.raise_owned()
        if self.phase == "error_attribute" and key.startswith("error."):
            self.owner.raise_owned()
        self.attributes[key] = value

    def set_attributes(self, attributes: Mapping[str, AttributeValue]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def add_event(
        self,
        name: str,
        attributes: Attributes = None,
        timestamp: int | None = None,
    ) -> None:
        self.event_calls.append((name, attributes, timestamp))

    def update_name(self, name: str) -> None:
        self.name_calls.append(name)

    def set_status(
        self,
        status: Status | StatusCode,
        description: str | None = None,
    ) -> None:
        self.status_calls.append((status, description))
        if self.phase == "error_status":
            self.owner.raise_owned()

    def end(self, end_time: int | None = None) -> None:
        self.end_calls += 1
        if self.phase in {"span_end", "span_end_before_detach"}:
            self.owner.raise_owned()

    def is_recording(self) -> bool:
        return True

    def get_span_context(self) -> trace.SpanContext:
        return trace.INVALID_SPAN_CONTEXT

    def record_exception(
        self,
        exception: BaseException,
        attributes: Attributes = None,
        timestamp: int | None = None,
        escaped: bool = False,
    ) -> None:
        self.exception_calls.append((exception, attributes, timestamp, escaped))


class _HostileSpanManager:
    def __init__(self, owner: _OwnedFailure, phase: str, span: _HostileSpan) -> None:
        self.owner = owner
        self.phase = phase
        self.span = span
        self.enter_calls = 0
        self.exit_calls = 0
        self.token: object | None = None

    def __enter__(self) -> _HostileSpan:
        self.enter_calls += 1
        if self.phase == "manager_enter":
            self.owner.raise_owned()
        self.token = otel_context.attach(trace.set_span_in_context(self.span))
        return self.span

    def __exit__(self, *_args: object) -> None:
        self.exit_calls += 1
        try:
            self.span.end()
        finally:
            token = self.token
            self.token = None
            if token is not None:
                otel_context.detach(cast(Any, token))
            if self.phase == "detach":
                self.owner.raise_owned()
            if self.phase == "manager_exit":
                self.owner.raise_owned()


class _HostileTracer:
    def __init__(self, owner: _OwnedFailure, phase: str) -> None:
        self.owner = owner
        self.phase = phase
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.span = _HostileSpan(owner, phase)
        self.manager = _HostileSpanManager(owner, phase, self.span)

    def start_as_current_span(self, name: object, **kwargs: object) -> _HostileSpanManager:
        self.calls.append((name, dict(kwargs)))
        if self.phase == "tracer_start":
            self.owner.raise_owned()
        return self.manager


class _PoisoningSpanManager(_HostileSpanManager):
    def __enter__(self) -> _HostileSpan:
        self.enter_calls += 1
        self.token = otel_context.attach(trace.set_span_in_context(self.span))
        if self.phase == "attach_then_enter_fail":
            self.owner.raise_owned()
        return self.span

    def __exit__(self, *_args: object) -> None:
        self.exit_calls += 1
        self.span.end()
        if self.phase == "exit_before_detach":
            self.owner.raise_owned()
        token = self.token
        self.token = None
        if token is not None:
            if self.phase == "detach_before_restore":
                self.token = token
                self.owner.raise_owned()
            otel_context.detach(cast(Any, token))


class _PoisoningTracer(_HostileTracer):
    def __init__(self, owner: _OwnedFailure, phase: str) -> None:
        self.owner = owner
        self.phase = phase
        self.calls = []
        self.span = _HostileSpan(owner, phase)
        self.manager = _PoisoningSpanManager(owner, phase, self.span)


class _SetupThenCleanupSpanManager:
    def __init__(
        self,
        setup_owner: _OwnedFailure,
        cleanup_owner: _OwnedFailure,
        cleanup_phase: str,
        span: _HostileSpan,
    ) -> None:
        self.setup_owner = setup_owner
        self.cleanup_owner = cleanup_owner
        self.cleanup_phase = cleanup_phase
        self.span = span
        self.enter_calls = 0
        self.exit_calls = 0
        self.token: object | None = None

    def __enter__(self) -> _HostileSpan:
        self.enter_calls += 1
        self.token = otel_context.attach(trace.set_span_in_context(self.span))
        self.setup_owner.raise_owned()
        raise AssertionError("unreachable")

    def __exit__(self, *_args: object) -> None:
        self.exit_calls += 1
        self.span.end()
        if self.cleanup_phase == "exit_before_detach":
            self.cleanup_owner.raise_owned()
        token = self.token
        self.token = None
        if token is not None:
            if self.cleanup_phase == "detach_before_restore":
                self.token = token
                self.cleanup_owner.raise_owned()
            otel_context.detach(cast(Any, token))


class _SetupThenCleanupTracer:
    def __init__(
        self,
        setup_owner: _OwnedFailure,
        cleanup_owner: _OwnedFailure,
        cleanup_phase: str,
    ) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.span = _HostileSpan(
            cleanup_owner,
            "span_end_before_detach" if cleanup_phase == "span_end_before_detach" else "healthy",
        )
        self.manager = _SetupThenCleanupSpanManager(
            setup_owner,
            cleanup_owner,
            cleanup_phase,
            self.span,
        )

    def start_as_current_span(
        self,
        name: object,
        **kwargs: object,
    ) -> _SetupThenCleanupSpanManager:
        self.calls.append((name, dict(kwargs)))
        return self.manager


def _assert_hostile_manager_round_trip(
    span: object,
    manager: _HostileSpanManager,
) -> None:
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    assert isinstance(span, trace.Span)
    assert not inspect.isabstract(type(span))
    with manager as yielded:
        assert yielded is span
        assert trace.get_current_span() is span
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span
    assert manager.enter_calls == 1
    assert manager.exit_calls == 1
    assert manager.token is None
    assert isinstance(span, _HostileSpan)
    assert span.end_calls == 1


def test_hostile_span_fixture_is_concrete_current_and_restores_context() -> None:
    owner = _OwnedFailure(RuntimeError("unused-hostile-span-failure"))
    span = _HostileSpan(owner, "healthy")
    manager = _HostileSpanManager(owner, "healthy", span)

    _assert_hostile_manager_round_trip(span, manager)

    assert span.event_calls == []
    assert span.name_calls == []
    assert span.exception_calls == []


def test_hostile_span_fixture_oracle_rejects_plain_abstract_and_wrong_attach() -> None:
    owner = _OwnedFailure(RuntimeError("unused-hostile-span-mutation"))
    span = _HostileSpan(owner, "healthy")
    manager = _HostileSpanManager(owner, "healthy", span)
    with pytest.raises(AssertionError):
        _assert_hostile_manager_round_trip(SimpleNamespace(), manager)

    class _MissingRecordExceptionSpan(_HostileSpan):
        record_exception = trace.Span.record_exception

    assert inspect.isabstract(_MissingRecordExceptionSpan)
    with pytest.raises(TypeError):
        _MissingRecordExceptionSpan(owner, "healthy")

    class _WrongAttachManager(_HostileSpanManager):
        def __enter__(self) -> _HostileSpan:
            self.enter_calls += 1
            self.token = otel_context.attach(trace.set_span_in_context(trace.INVALID_SPAN))
            return self.span

    wrong_span = _HostileSpan(owner, "healthy")
    wrong_manager = _WrongAttachManager(owner, "healthy", wrong_span)
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    with pytest.raises(AssertionError):
        _assert_hostile_manager_round_trip(wrong_span, wrong_manager)
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span
    assert wrong_manager.enter_calls == 1
    assert wrong_manager.exit_calls == 1
    assert wrong_span.end_calls == 1


_HOSTILE_PHASES = [
    "tracer_start",
    "manager_enter",
    "late_attribute",
    "manager_exit",
    "span_end",
    "detach",
    "calls_getter",
    "calls_write",
    "error_status",
    "error_attribute",
    "failures_getter",
    "failures_write",
]
_HOSTILE_PRODUCT_CASES = [
    ("execute", "success"),
    ("execute", "failure"),
    ("execute", "cancel"),
    ("approval", "success"),
    ("approval", "failure"),
    ("approval", "denied"),
    ("approval", "failed"),
    ("approval", "cancel"),
]


def _install_hostile_telemetry(
    world: ToolWorld,
    *,
    phase: str,
    failure: BaseException,
) -> tuple[_OwnedFailure, _HostileTracer | None, _HostileMetrics | None]:
    owner = _OwnedFailure(failure)
    tracer: _HostileTracer | None = None
    metrics: _HostileMetrics | None = None
    if phase in {
        "tracer_start",
        "manager_enter",
        "late_attribute",
        "manager_exit",
        "span_end",
        "detach",
        "error_status",
        "error_attribute",
    }:
        tracer = _HostileTracer(owner, phase)
        world.resources.runtime.tracer = cast(Tracer, tracer)
    else:
        target = "tool_calls_total" if phase.startswith("calls_") else "tool_call_failures_total"
        metrics = _HostileMetrics(
            world.telemetry.metrics,
            owner,
            target=target,
            phase=phase.rsplit("_", 1)[1],
        )
        world.resources.runtime.metrics = cast(JhinMetrics, metrics)
    world.activities = _ProbeToolActivities(world.resources, world.catalog)
    return owner, tracer, metrics


def _install_poisoning_tracer(
    world: ToolWorld,
    *,
    phase: str,
    failure: BaseException,
) -> tuple[_OwnedFailure, _PoisoningTracer]:
    owner = _OwnedFailure(failure)
    tracer = _PoisoningTracer(owner, phase)
    world.resources.runtime.tracer = cast(Tracer, tracer)
    world.activities = _ProbeToolActivities(world.resources, world.catalog)
    return owner, tracer


def _install_setup_then_cleanup_tracer(
    world: ToolWorld,
    *,
    cleanup_phase: str,
    setup_failure: Exception,
    cleanup_cancellation: asyncio.CancelledError,
) -> tuple[_OwnedFailure, _OwnedFailure, _SetupThenCleanupTracer]:
    setup_owner = _OwnedFailure(setup_failure)
    cleanup_owner = _OwnedFailure(cleanup_cancellation)
    tracer = _SetupThenCleanupTracer(setup_owner, cleanup_owner, cleanup_phase)
    world.resources.runtime.tracer = cast(Tracer, tracer)
    world.activities = _ProbeToolActivities(world.resources, world.catalog)
    return setup_owner, cleanup_owner, tracer


def _install_gateway_current_span_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[UUID, list[object]]:
    calls: dict[UUID, list[object]] = {}
    for method_name in ("request", "resolve_approved", "resolve_rejected"):
        original = getattr(ToolGateway, method_name)

        async def observed(
            gateway: ToolGateway,
            *args: object,
            _original: Callable[..., Awaitable[object]] = original,
            **kwargs: object,
        ) -> object:
            calls.setdefault(gateway._ctx.run_id, []).append(trace.get_current_span())
            return await _original(gateway, *args, **kwargs)

        monkeypatch.setattr(ToolGateway, method_name, observed)
    return calls


def _use_noop_activity_runtime(world: ToolWorld) -> None:
    world.resources = SimpleNamespace(
        runtime=SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer()),
        session_factory=world.sessions,
        crypto=None,
        test_barrier=None,
        telemetry=world.telemetry,
    )
    world.activities = _ProbeToolActivities(world.resources, world.catalog)


@pytest.mark.parametrize("poison_phase", ["attach_then_enter_fail", "exit_before_detach"])
@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_poisoned_tool_span_lifecycle_restores_exact_context_and_preserves_product(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
    poison_phase: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    control_effects_before = len(world.effects)
    control_result, control_error, control_frames = await _invoke_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
        parked=control_parked,
    )
    assert control_error is None
    assert control_frames == ()
    assert control_result is not None
    control_effect_delta = len(world.effects) - control_effects_before
    control_product_snapshot = await control.product_snapshot()
    control_snapshot = _canonical_product_snapshot(control, control_product_snapshot)
    world.telemetry.exporter.clear()
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    diagnostic = RuntimeError(f"poison-{poison_phase}")
    owner, tracer = _install_poisoning_tracer(
        world,
        phase=poison_phase,
        failure=diagnostic,
    )
    outer_context = otel_context.get_current()
    outer_span = trace.get_current_span()
    observed: dict[str, object] = {}
    effects_before = len(world.effects)

    async def invoke_in_isolated_context() -> BoundToolResult:
        observed["entry_context"] = otel_context.get_current()
        observed["entry_span"] = trace.get_current_span()
        result, error, frames = await _invoke_terminal_case(
            world,
            activity_kind=activity_kind,
            product_kind="success",
            parked=parked,
        )
        observed["exit_context"] = otel_context.get_current()
        observed["exit_span"] = trace.get_current_span()
        assert error is None
        assert frames == ()
        assert result is not None
        return result

    task = asyncio.create_task(
        invoke_in_isolated_context(),
        context=contextvars.Context(),
    )
    result = await task

    assert result.status == "executed"
    assert observed["exit_context"] is observed["entry_context"]
    assert observed["exit_span"] is observed["entry_span"]
    assert otel_context.get_current() is outer_context
    assert trace.get_current_span() is outer_span
    assert owner.raised_traceback is not None
    assert len(tracer.calls) == 1
    assert tracer.manager.enter_calls == 1
    assert tracer.manager.exit_calls == 1
    assert tracer.span.end_calls == 1
    assert len(gateway_calls[world.run_id]) == 1
    assert gateway_calls[world.run_id][0] is (
        observed["entry_span"] if poison_phase == "attach_then_enter_fail" else tracer.span
    )
    target_product_snapshot = await world.product_snapshot()
    target_approval_authority = _bound_result_approval_authority(
        result,
        parked,
        target_product_snapshot,
    )
    control_approval_authority = _bound_result_approval_authority(
        control_result,
        control_parked,
        control_product_snapshot,
    )
    assert _canonical_product_snapshot(
        world,
        {"result": asdict(result)},
        result_approval_authority=target_approval_authority,
    ) == _canonical_product_snapshot(
        control,
        {"result": asdict(control_result)},
        result_approval_authority=control_approval_authority,
    )
    assert _canonical_product_snapshot(world, target_product_snapshot) == control_snapshot
    assert len(world.effects) - effects_before == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == _expected_product_metric_points(
        activity_kind=activity_kind,
        product_kind="success",
    )
    tracer.manager.token = None


@pytest.mark.parametrize("setup_phase", ["tracer_start", "manager_enter"])
@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
@pytest.mark.parametrize("product_exception", ["error", "cancel"])
async def test_setup_diagnostic_never_enters_the_product_exception_chain(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    setup_phase: str,
    activity_kind: str,
    product_exception: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    cancellation_owners: dict[UUID, _OwnedFailure] | None = None
    if product_exception == "cancel":
        cancellation_owners = _install_product_cancellation(
            world,
            control,
            activity_kind=activity_kind,
        )

    async def invoke_raw(
        candidate: ToolWorld,
        candidate_parked: BoundToolResult | None,
    ) -> None:
        if activity_kind == "execute":
            await candidate.activities.execute_bound_tool_activity(candidate.execute_params())
        else:
            assert candidate_parked is not None
            await candidate.activities.resolve_bound_tool_approval_activity(
                candidate.approval_params(candidate_parked.approval_id or "")
            )

    control_product_error = RuntimeError("owned-product-error")
    if product_exception == "error":
        _ProbeSession.fail_activity_commit = control_product_error
    control_effects_before = len(world.effects)
    control_caught: BaseException | None = None
    try:
        await invoke_raw(control, control_parked)
    except BaseException as error:
        control_caught = error
    assert control_caught is not None
    if product_exception == "error":
        assert control_caught is control_product_error
        control_origin = _ProbeSession.commit_raised_traceback
    else:
        assert cancellation_owners is not None
        assert control_caught is cancellation_owners[control.run_id].failure
        control_origin = cancellation_owners[control.run_id].raised_traceback
    assert control_origin is not None
    assert _traceback_tail(control_caught.__traceback__) is control_origin
    control_frames = _traceback_frame_names(control_caught.__traceback__)
    control_chain = _exception_chain_shape(control_caught)
    control_effect_delta = len(world.effects) - control_effects_before
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()

    product_error = RuntimeError("owned-product-error")
    if product_exception == "error":
        _ProbeSession.fail_activity_commit = product_error
    setup_diagnostic = RuntimeError(f"setup-{setup_phase}-diagnostic")
    diagnostic_owner, tracer, _metrics = _install_hostile_telemetry(
        world,
        phase=setup_phase,
        failure=setup_diagnostic,
    )
    assert tracer is not None
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    effects_before_target = len(world.effects)
    target_caught: BaseException | None = None
    try:
        await invoke_raw(world, parked)
    except BaseException as error:
        target_caught = error

    assert target_caught is not None
    if product_exception == "error":
        assert target_caught is product_error
        target_origin = _ProbeSession.commit_raised_traceback
    else:
        assert cancellation_owners is not None
        assert target_caught is cancellation_owners[world.run_id].failure
        target_origin = cancellation_owners[world.run_id].raised_traceback
    assert target_origin is not None
    assert _traceback_tail(target_caught.__traceback__) is target_origin
    assert _traceback_frame_names(target_caught.__traceback__) == control_frames
    assert _exception_chain_shape(target_caught) == control_chain
    assert diagnostic_owner.raised_traceback is not None
    assert len(gateway_calls[world.run_id]) == 1
    assert gateway_calls[world.run_id][0] is entry_span
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert len(world.effects) - effects_before_target == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == []


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
@pytest.mark.parametrize("product_exception", ["error", "cancel"])
async def test_attached_setup_diagnostic_restores_context_without_polluting_product_chain(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
    product_exception: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    cancellation_owners: dict[UUID, _OwnedFailure] | None = None
    if product_exception == "cancel":
        cancellation_owners = _install_product_cancellation(
            world,
            control,
            activity_kind=activity_kind,
        )

    async def capture_in_isolated_context(
        candidate: ToolWorld,
        candidate_parked: BoundToolResult | None,
    ) -> tuple[BaseException, object, object, object, object]:
        entry_context = otel_context.get_current()
        entry_span = trace.get_current_span()
        try:
            await _invoke_terminal_activity_raw(
                candidate,
                activity_kind=activity_kind,
                parked=candidate_parked,
            )
        except BaseException as error:
            return (
                error,
                entry_context,
                entry_span,
                otel_context.get_current(),
                trace.get_current_span(),
            )
        raise AssertionError("expected product failure")

    control_product_error = RuntimeError("control-owned-product-error")
    if product_exception == "error":
        _ProbeSession.fail_activity_commit = control_product_error
    effects_before_control = len(world.effects)
    control_task = asyncio.create_task(
        capture_in_isolated_context(control, control_parked),
        context=contextvars.Context(),
    )
    (
        control_caught,
        control_entry_context,
        control_entry_span,
        control_exit_context,
        control_exit_span,
    ) = await control_task
    assert control_exit_context is control_entry_context
    assert control_exit_span is control_entry_span
    if product_exception == "error":
        assert control_caught is control_product_error
        control_origin = _ProbeSession.commit_raised_traceback
    else:
        assert cancellation_owners is not None
        control_owner = cancellation_owners[control.run_id]
        assert control_caught is control_owner.failure
        control_origin = control_owner.raised_traceback
    assert control_origin is not None
    assert _traceback_tail(control_caught.__traceback__) is control_origin
    control_frames = _traceback_frame_names(control_caught.__traceback__)
    control_chain = _exception_chain_shape(control_caught)
    control_effect_delta = len(world.effects) - effects_before_control
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()

    product_error = RuntimeError("target-owned-product-error")
    if product_exception == "error":
        _ProbeSession.fail_activity_commit = product_error
    setup_error = RuntimeError("attached-setup-diagnostic")
    setup_owner, tracer = _install_poisoning_tracer(
        world,
        phase="attach_then_enter_fail",
        failure=setup_error,
    )
    outer_context = otel_context.get_current()
    outer_span = trace.get_current_span()
    effects_before_target = len(world.effects)
    target_task = asyncio.create_task(
        capture_in_isolated_context(world, parked),
        context=contextvars.Context(),
    )
    (
        target_caught,
        target_entry_context,
        target_entry_span,
        target_exit_context,
        target_exit_span,
    ) = await target_task

    if product_exception == "error":
        assert target_caught is product_error
        target_origin = _ProbeSession.commit_raised_traceback
    else:
        assert cancellation_owners is not None
        target_owner = cancellation_owners[world.run_id]
        assert target_caught is target_owner.failure
        target_origin = target_owner.raised_traceback
    assert target_origin is not None
    assert _traceback_tail(target_caught.__traceback__) is target_origin
    assert _traceback_frame_names(target_caught.__traceback__) == control_frames
    assert _exception_chain_shape(target_caught) == control_chain
    assert setup_owner.raised_traceback is not None
    assert tracer.manager.enter_calls == 1
    assert tracer.manager.exit_calls == 1
    assert tracer.span.end_calls == 1
    assert tracer.manager.token is None
    assert target_exit_context is target_entry_context
    assert target_exit_span is target_entry_span
    assert otel_context.get_current() is outer_context
    assert trace.get_current_span() is outer_span
    assert gateway_calls[control.run_id] == [control_entry_span]
    assert gateway_calls[world.run_id] == [target_entry_span]
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert len(world.effects) - effects_before_target == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == []


@pytest.mark.parametrize(
    "setup_phase",
    ["tracer_start", "manager_enter", "attach_then_enter_fail"],
)
@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_setup_telemetry_cancellation_is_primary_before_product_and_restores_context(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    setup_phase: str,
    activity_kind: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    initial_snapshot = _canonical_product_snapshot(world, await world.product_snapshot())
    initial_effect_count = len(world.effects)
    initial_commit_count = len(_ProbeSession.activity_commit_callers)
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    cancellation = asyncio.CancelledError(f"owned-setup-cancellation-{setup_phase}")
    if setup_phase == "attach_then_enter_fail":
        owner, tracer = _install_poisoning_tracer(
            world,
            phase=setup_phase,
            failure=cancellation,
        )
    else:
        owner, hostile_tracer, _metrics = _install_hostile_telemetry(
            world,
            phase=setup_phase,
            failure=cancellation,
        )
        assert hostile_tracer is not None
        tracer = hostile_tracer
    outer_context = otel_context.get_current()
    outer_span = trace.get_current_span()
    observed: dict[str, object] = {}

    async def invoke_in_isolated_context() -> None:
        observed["entry_context"] = otel_context.get_current()
        observed["entry_span"] = trace.get_current_span()
        try:
            await _invoke_terminal_activity_raw(
                world,
                activity_kind=activity_kind,
                parked=parked,
            )
        except BaseException:
            observed["exit_context"] = otel_context.get_current()
            observed["exit_span"] = trace.get_current_span()
            raise
        raise AssertionError("setup telemetry cancellation was swallowed")

    task = asyncio.create_task(
        invoke_in_isolated_context(),
        context=contextvars.Context(),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    _assert_exact_owned_failure(caught.value, owner)
    assert _exception_chain_shape(caught.value) == (
        "CancelledError",
        False,
        None,
        None,
    )
    assert observed["exit_context"] is observed["entry_context"]
    assert observed["exit_span"] is observed["entry_span"]
    assert otel_context.get_current() is outer_context
    assert trace.get_current_span() is outer_span
    assert len(tracer.calls) == 1
    assert tracer.manager.enter_calls == int(setup_phase != "tracer_start")
    assert tracer.manager.exit_calls == 0
    assert tracer.span.end_calls == 0
    if setup_phase != "attach_then_enter_fail":
        assert tracer.manager.token is None
    assert gateway_calls.get(world.run_id, []) == []
    assert len(_ProbeSession.activity_commit_callers) == initial_commit_count
    assert len(world.effects) == initial_effect_count
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == initial_snapshot
    assert _metric_point_multiset(world.telemetry) == []
    tracer.manager.token = None


@pytest.mark.parametrize(
    "cleanup_phase",
    ["exit_before_detach", "span_end_before_detach", "detach_before_restore"],
)
@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_setup_recovery_cancellation_is_primary_and_restores_poisoned_context(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
    cleanup_phase: str,
) -> None:
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    initial_snapshot = _canonical_product_snapshot(world, await world.product_snapshot())
    initial_effect_count = len(world.effects)
    initial_commit_count = len(_ProbeSession.activity_commit_callers)
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    setup_error = RuntimeError("ordinary-attached-setup-diagnostic")
    cleanup_cancellation = asyncio.CancelledError(
        f"owned-setup-cleanup-cancellation-{cleanup_phase}"
    )
    setup_owner, cleanup_owner, tracer = _install_setup_then_cleanup_tracer(
        world,
        cleanup_phase=cleanup_phase,
        setup_failure=setup_error,
        cleanup_cancellation=cleanup_cancellation,
    )
    outer_context = otel_context.get_current()
    outer_span = trace.get_current_span()
    observed: dict[str, object] = {}

    async def invoke_in_isolated_context() -> None:
        observed["entry_context"] = otel_context.get_current()
        observed["entry_span"] = trace.get_current_span()
        try:
            await _invoke_terminal_activity_raw(
                world,
                activity_kind=activity_kind,
                parked=parked,
            )
        except BaseException:
            observed["exit_context"] = otel_context.get_current()
            observed["exit_span"] = trace.get_current_span()
            raise
        raise AssertionError("setup recovery cancellation was swallowed")

    task = asyncio.create_task(
        invoke_in_isolated_context(),
        context=contextvars.Context(),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    _assert_exact_owned_failure(caught.value, cleanup_owner)
    assert setup_owner.raised_traceback is not None
    assert _exception_chain_shape(caught.value) == (
        "CancelledError",
        False,
        None,
        ("RuntimeError", False, None, None),
    )
    assert observed["exit_context"] is observed["entry_context"]
    assert observed["exit_span"] is observed["entry_span"]
    assert otel_context.get_current() is outer_context
    assert trace.get_current_span() is outer_span
    assert len(tracer.calls) == 1
    assert tracer.manager.enter_calls == 1
    assert tracer.manager.exit_calls == 1
    assert tracer.span.end_calls == 1
    assert gateway_calls.get(world.run_id, []) == []
    assert len(_ProbeSession.activity_commit_callers) == initial_commit_count
    assert len(world.effects) == initial_effect_count
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == initial_snapshot
    assert _metric_point_multiset(world.telemetry) == []
    tracer.manager.token = None


_POSTCOMMIT_PRIMARY_CANCELLATION_CASES = [
    ("execute", "success", "late_attribute"),
    ("approval", "success", "late_attribute"),
    ("execute", "failure", "error_status"),
    ("approval", "failed", "error_status"),
    ("execute", "failure", "error_attribute"),
    ("approval", "denied", "error_attribute"),
    ("execute", "success", "calls_getter"),
    ("approval", "success", "calls_getter"),
    ("execute", "success", "calls_write"),
    ("approval", "success", "calls_write"),
    ("execute", "failure", "failures_getter"),
    ("approval", "failed", "failures_getter"),
    ("execute", "failure", "failures_write"),
    ("approval", "denied", "failures_write"),
]


async def test_postcommit_setter_and_metric_cancellation_is_primary_with_committed_product() -> (
    None
):
    # Loop-folded: the exact former parametrize case list, one isolated world
    # and monkeypatch scope per case.
    for activity_kind, product_kind, phase in _POSTCOMMIT_PRIMARY_CANCELLATION_CASES:
        ctx = f"activity_kind={activity_kind} product_kind={product_kind} phase={phase}"
        with _named_case(ctx):
            async with _fresh_world_context() as world:
                with pytest.MonkeyPatch.context() as monkeypatch:
                    await _check_postcommit_primary_cancellation_case(
                        world,
                        monkeypatch,
                        activity_kind=activity_kind,
                        product_kind=product_kind,
                        phase=phase,
                        ctx=ctx,
                    )


async def _check_postcommit_primary_cancellation_case(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    activity_kind: str,
    product_kind: str,
    phase: str,
    ctx: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    effects_before_control = len(world.effects)
    await _invoke_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
        parked=control_parked,
    )
    control_effect_delta = len(world.effects) - effects_before_control
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    cancellation = asyncio.CancelledError(f"owned-postcommit-cancellation-{phase}")
    owner, tracer, metrics = _install_hostile_telemetry(
        world,
        phase=phase,
        failure=cancellation,
    )
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    effects_before_target = len(world.effects)
    commits_before_target = len(_ProbeSession.activity_commit_callers)

    with pytest.raises(asyncio.CancelledError) as caught:
        await _invoke_terminal_activity_raw(
            world,
            activity_kind=activity_kind,
            parked=parked,
        )

    _assert_exact_owned_failure(caught.value, owner)
    assert _exception_chain_shape(caught.value) == (
        "CancelledError",
        False,
        None,
        None,
    ), ctx
    assert otel_context.get_current() is entry_context, ctx
    assert trace.get_current_span() is entry_span, ctx
    assert _ProbeSession.activity_commit_callers[commits_before_target:] == [
        (
            "execute_bound_tool_activity"
            if activity_kind == "execute"
            else "resolve_bound_tool_approval_activity"
        )
    ], ctx
    assert len(gateway_calls[world.run_id]) == 1, ctx
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == (
        control_snapshot
    ), ctx
    assert len(world.effects) - effects_before_target == control_effect_delta, ctx
    if tracer is not None:
        assert tracer.manager.enter_calls == 1, ctx
        assert tracer.manager.exit_calls == 1, ctx
        assert tracer.span.end_calls == 1, ctx
        assert tracer.manager.token is None, ctx
        assert gateway_calls[world.run_id] == [tracer.span], ctx
    else:
        assert metrics is not None, ctx
        assert gateway_calls[world.run_id][0] is not entry_span, ctx

    expected_points = _expected_product_metric_points(
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    if phase in {"late_attribute", "error_status", "error_attribute"} or phase.startswith("calls_"):
        expected_points = []
    elif phase.startswith("failures_"):
        expected_points = [point for point in expected_points if point[0] == "tool_calls_total"]
    assert _metric_point_multiset(world.telemetry) == sorted(expected_points), ctx


@pytest.mark.parametrize(
    "phase",
    ["exit_before_detach", "span_end_before_detach", "detach_before_restore"],
)
@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_postcommit_teardown_cancellation_is_primary_and_restores_poisoned_context(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
    phase: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    effects_before_control = len(world.effects)
    await _invoke_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
        parked=control_parked,
    )
    control_effect_delta = len(world.effects) - effects_before_control
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    cancellation = asyncio.CancelledError(f"owned-teardown-cancellation-{phase}")
    owner, tracer = _install_poisoning_tracer(
        world,
        phase=phase,
        failure=cancellation,
    )
    outer_context = otel_context.get_current()
    outer_span = trace.get_current_span()
    effects_before_target = len(world.effects)
    commits_before_target = len(_ProbeSession.activity_commit_callers)
    observed: dict[str, object] = {}

    async def invoke_in_isolated_context() -> None:
        observed["entry_context"] = otel_context.get_current()
        observed["entry_span"] = trace.get_current_span()
        try:
            await _invoke_terminal_activity_raw(
                world,
                activity_kind=activity_kind,
                parked=parked,
            )
        except BaseException:
            observed["exit_context"] = otel_context.get_current()
            observed["exit_span"] = trace.get_current_span()
            raise
        raise AssertionError("teardown telemetry cancellation was swallowed")

    task = asyncio.create_task(
        invoke_in_isolated_context(),
        context=contextvars.Context(),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    _assert_exact_owned_failure(caught.value, owner)
    assert _exception_chain_shape(caught.value) == (
        "CancelledError",
        False,
        None,
        None,
    )
    assert observed["exit_context"] is observed["entry_context"]
    assert observed["exit_span"] is observed["entry_span"]
    assert otel_context.get_current() is outer_context
    assert trace.get_current_span() is outer_span
    assert tracer.manager.enter_calls == 1
    assert tracer.manager.exit_calls == 1
    assert tracer.span.end_calls == 1
    assert len(gateway_calls[world.run_id]) == 1
    assert gateway_calls[world.run_id] == [tracer.span]
    assert _ProbeSession.activity_commit_callers[commits_before_target:] == [
        (
            "execute_bound_tool_activity"
            if activity_kind == "execute"
            else "resolve_bound_tool_approval_activity"
        )
    ]
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert len(world.effects) - effects_before_target == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == _expected_product_metric_points(
        activity_kind=activity_kind,
        product_kind="success",
    )
    tracer.manager.token = None


@pytest.mark.parametrize("phase", ["manager_exit", "span_end", "detach"])
async def test_teardown_cancellation_never_replaces_committed_product_application_error(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    raised_errors: dict[UUID, tuple[ApplicationError, TracebackType | None]] = {}
    original_raise_failure = activities_module._raise_ordinary_failure

    def observed_raise_failure(outcome: object) -> None:
        try:
            original_raise_failure(cast(Any, outcome))
        except ApplicationError as error:
            raised_errors[cast(Any, outcome).tool_call_id] = (error, error.__traceback__)
            raise

    monkeypatch.setattr(activities_module, "_raise_ordinary_failure", observed_raise_failure)
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    await _prepare_terminal_case(control, activity_kind="execute", product_kind="failure")
    await _prepare_terminal_case(world, activity_kind="execute", product_kind="failure")
    effects_before_control = len(world.effects)
    _control_result, control_error, control_frames = await _invoke_terminal_case(
        control,
        activity_kind="execute",
        product_kind="failure",
        parked=None,
    )
    assert control_error is not None
    assert control_error is raised_errors[control.invocation_id][0]
    control_chain = _exception_chain_shape(control_error)
    control_effect_delta = len(world.effects) - effects_before_control
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()
    cancellation = asyncio.CancelledError(f"secondary-teardown-cancellation-{phase}")
    owner, tracer, _metrics = _install_hostile_telemetry(
        world,
        phase=phase,
        failure=cancellation,
    )
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    effects_before_target = len(world.effects)

    result, error, frames = await _invoke_terminal_case(
        world,
        activity_kind="execute",
        product_kind="failure",
        parked=None,
    )

    assert result is None
    assert error is not None
    exact_error, original_traceback = raised_errors[world.invocation_id]
    assert error is exact_error
    assert _traceback_tail(error.__traceback__) is _traceback_tail(original_traceback)
    assert _application_error_public(error) == _application_error_public(control_error)
    assert frames == control_frames
    assert _exception_chain_shape(error) == control_chain
    assert owner.raised_traceback is not None
    assert tracer is not None
    assert tracer.manager.enter_calls == 1
    assert tracer.manager.exit_calls == 1
    assert tracer.span.end_calls == 1
    assert tracer.manager.token is None
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert len(world.effects) - effects_before_target == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == _expected_product_metric_points(
        activity_kind="execute",
        product_kind="failure",
    )


@pytest.mark.parametrize(
    "phase",
    ["exit_before_detach", "span_end_before_detach", "detach_before_restore"],
)
async def test_poisoned_teardown_cancellation_restores_context_and_preserves_application_error(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    raised_errors: dict[UUID, tuple[ApplicationError, TracebackType | None]] = {}
    original_raise_failure = activities_module._raise_ordinary_failure

    def observed_raise_failure(outcome: object) -> None:
        try:
            original_raise_failure(cast(Any, outcome))
        except ApplicationError as error:
            raised_errors[cast(Any, outcome).tool_call_id] = (error, error.__traceback__)
            raise

    monkeypatch.setattr(activities_module, "_raise_ordinary_failure", observed_raise_failure)
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    await _prepare_terminal_case(control, activity_kind="execute", product_kind="failure")
    await _prepare_terminal_case(world, activity_kind="execute", product_kind="failure")

    async def invoke_in_isolated_context(
        candidate: ToolWorld,
    ) -> tuple[
        BoundToolResult | None,
        ApplicationError | None,
        tuple[tuple[str, str, int], ...],
        object,
        object,
        object,
        object,
    ]:
        entry_context = otel_context.get_current()
        entry_span = trace.get_current_span()
        result, error, frames = await _invoke_terminal_case(
            candidate,
            activity_kind="execute",
            product_kind="failure",
            parked=None,
        )
        return (
            result,
            error,
            frames,
            entry_context,
            entry_span,
            otel_context.get_current(),
            trace.get_current_span(),
        )

    effects_before_control = len(world.effects)
    control_task = asyncio.create_task(
        invoke_in_isolated_context(control),
        context=contextvars.Context(),
    )
    (
        control_result,
        control_error,
        control_frames,
        control_entry_context,
        control_entry_span,
        control_exit_context,
        control_exit_span,
    ) = await control_task
    assert control_result is None
    assert control_error is not None
    assert control_error is raised_errors[control.invocation_id][0]
    assert control_exit_context is control_entry_context
    assert control_exit_span is control_entry_span
    control_chain = _exception_chain_shape(control_error)
    control_effect_delta = len(world.effects) - effects_before_control
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()
    cleanup_cancellation = asyncio.CancelledError(f"poisoned-app-error-cleanup-{phase}")
    cleanup_owner, tracer = _install_poisoning_tracer(
        world,
        phase=phase,
        failure=cleanup_cancellation,
    )
    outer_context = otel_context.get_current()
    outer_span = trace.get_current_span()
    effects_before_target = len(world.effects)
    target_task = asyncio.create_task(
        invoke_in_isolated_context(world),
        context=contextvars.Context(),
    )
    (
        result,
        error,
        frames,
        target_entry_context,
        target_entry_span,
        target_exit_context,
        target_exit_span,
    ) = await target_task

    assert result is None
    assert error is not None
    exact_error, original_traceback = raised_errors[world.invocation_id]
    assert error is exact_error
    assert _traceback_tail(error.__traceback__) is _traceback_tail(original_traceback)
    assert _application_error_public(error) == _application_error_public(control_error)
    assert frames == control_frames
    assert _exception_chain_shape(error) == control_chain
    assert cleanup_owner.raised_traceback is not None
    assert tracer.manager.enter_calls == 1
    assert tracer.manager.exit_calls == 1
    assert tracer.span.end_calls == 1
    assert target_exit_context is target_entry_context
    assert target_exit_span is target_entry_span
    assert otel_context.get_current() is outer_context
    assert trace.get_current_span() is outer_span
    assert gateway_calls[control.run_id] == [control_entry_span]
    assert gateway_calls[world.run_id] == [tracer.span]
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert len(world.effects) - effects_before_target == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == _expected_product_metric_points(
        activity_kind="execute",
        product_kind="failure",
    )
    tracer.manager.token = None


@pytest.mark.parametrize(
    "phase",
    ["exit_before_detach", "span_end_before_detach", "detach_before_restore"],
)
@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_poisoned_teardown_cancellation_restores_context_and_preserves_product_cancellation(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
    phase: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    product_owners = _install_product_cancellation(
        world,
        control,
        activity_kind=activity_kind,
    )

    async def invoke_in_isolated_context(
        candidate: ToolWorld,
        candidate_parked: BoundToolResult | None,
    ) -> tuple[asyncio.CancelledError, tuple[str, ...], object, object, object, object]:
        entry_context = otel_context.get_current()
        entry_span = trace.get_current_span()
        cancellation, frames = await _invoke_cancellation_case(
            candidate,
            activity_kind=activity_kind,
            parked=candidate_parked,
        )
        return (
            cancellation,
            frames,
            entry_context,
            entry_span,
            otel_context.get_current(),
            trace.get_current_span(),
        )

    effects_before_control = len(world.effects)
    control_task = asyncio.create_task(
        invoke_in_isolated_context(control, control_parked),
        context=contextvars.Context(),
    )
    (
        control_cancellation,
        control_frames,
        control_entry_context,
        control_entry_span,
        control_exit_context,
        control_exit_span,
    ) = await control_task
    control_owner = product_owners[control.run_id]
    _assert_exact_owned_failure(control_cancellation, control_owner)
    assert control_exit_context is control_entry_context
    assert control_exit_span is control_entry_span
    control_chain = _exception_chain_shape(control_cancellation)
    control_effect_delta = len(world.effects) - effects_before_control
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()
    cleanup_cancellation = asyncio.CancelledError(f"poisoned-product-cancel-cleanup-{phase}")
    cleanup_owner, tracer = _install_poisoning_tracer(
        world,
        phase=phase,
        failure=cleanup_cancellation,
    )
    outer_context = otel_context.get_current()
    outer_span = trace.get_current_span()
    effects_before_target = len(world.effects)
    target_task = asyncio.create_task(
        invoke_in_isolated_context(world, parked),
        context=contextvars.Context(),
    )
    (
        product_cancellation,
        product_frames,
        target_entry_context,
        target_entry_span,
        target_exit_context,
        target_exit_span,
    ) = await target_task

    product_owner = product_owners[world.run_id]
    _assert_exact_owned_failure(product_cancellation, product_owner)
    assert product_frames == control_frames
    assert _exception_chain_shape(product_cancellation) == control_chain
    assert cleanup_owner.raised_traceback is not None
    assert tracer.manager.enter_calls == 1
    assert tracer.manager.exit_calls == 1
    assert tracer.span.end_calls == 1
    assert target_exit_context is target_entry_context
    assert target_exit_span is target_entry_span
    assert otel_context.get_current() is outer_context
    assert trace.get_current_span() is outer_span
    assert gateway_calls[control.run_id] == [control_entry_span]
    assert gateway_calls[world.run_id] == [tracer.span]
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert len(world.effects) - effects_before_target == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == []
    tracer.manager.token = None


class _HostileProofEquality:
    def __init__(self, owner: _OwnedFailure) -> None:
        self.owner = owner

    def __eq__(self, _other: object) -> bool:
        self.owner.raise_owned()
        raise AssertionError("unreachable")

    def __ne__(self, _other: object) -> bool:
        self.owner.raise_owned()
        raise AssertionError("unreachable")


async def test_postcommit_proof_exceptions_preserve_exact_product_and_base_authority() -> None:
    # Loop-folded: the exact former parametrize cross-product, one isolated
    # world and monkeypatch scope per case.
    for product_kind in ("success", "failure"):
        for activity_kind in ("execute", "approval"):
            for proof_seam in ("equality", "stable-derivation"):
                for failure_type in (
                    _WorkerDiagnostic,
                    asyncio.CancelledError,
                    KeyboardInterrupt,
                    SystemExit,
                ):
                    ctx = (
                        f"product_kind={product_kind} activity_kind={activity_kind}"
                        f" proof_seam={proof_seam} failure_type={failure_type.__name__}"
                    )
                    with _named_case(ctx):
                        async with _fresh_world_context() as world:
                            with pytest.MonkeyPatch.context() as monkeypatch:
                                await _check_postcommit_proof_exception_case(
                                    world,
                                    monkeypatch,
                                    failure_type=failure_type,
                                    proof_seam=proof_seam,
                                    activity_kind=activity_kind,
                                    product_kind=product_kind,
                                    ctx=ctx,
                                )


async def _check_postcommit_proof_exception_case(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure_type: type[BaseException],
    proof_seam: str,
    activity_kind: str,
    product_kind: str,
    ctx: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    control_effects_before = len(world.effects)
    control_result, control_error, control_frames = await _invoke_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
        parked=control_parked,
    )
    control_effect_delta = len(world.effects) - control_effects_before
    control_product_snapshot = await control.product_snapshot()
    control_snapshot = _canonical_product_snapshot(control, control_product_snapshot)
    world.telemetry.exporter.clear()

    failure = failure_type(f"proof-{proof_seam}-authority")
    owner = _OwnedFailure(failure)
    if proof_seam == "equality":
        hostile_value = _HostileProofEquality(owner)
        world.activities.authority_mutator = lambda authority: replace(
            cast(Any, authority),
            outcome_status=hostile_value,
        )
    else:
        real_stable_id = activities_module.stable_tool_invocation_id
        armed = False
        armed_calls = 0

        async def arm_derivation_failure() -> None:
            nonlocal armed
            armed = True

        def fail_only_in_matcher(
            run_id: UUID,
            step_index: int,
            ordinal: int,
        ) -> UUID:
            nonlocal armed_calls
            if armed:
                armed_calls += 1
                if armed_calls == 2:
                    owner.raise_owned()
            return real_stable_id(run_id, step_index, ordinal)

        world.activities.authority_before_load = arm_derivation_failure
        monkeypatch.setattr(
            activities_module,
            "stable_tool_invocation_id",
            fail_only_in_matcher,
        )

    target_effects_before = len(world.effects)
    if issubclass(failure_type, Exception):
        result, error, frames = await _invoke_terminal_case(
            world,
            activity_kind=activity_kind,
            product_kind=product_kind,
            parked=parked,
        )
        if control_error is not None:
            assert result is None, ctx
            assert error is not None, ctx
            assert _application_error_public(error) == (_application_error_public(control_error)), (
                ctx
            )
            assert frames == control_frames, ctx
        else:
            assert control_result is not None, ctx
            assert result is not None, ctx
            assert error is None, ctx
            target_snapshot = await world.product_snapshot()
            target_approval_authority = _bound_result_approval_authority(
                result,
                parked,
                target_snapshot,
            )
            control_approval_authority = _bound_result_approval_authority(
                control_result,
                control_parked,
                control_product_snapshot,
            )
            assert _canonical_product_snapshot(
                world,
                {"result": asdict(result)},
                result_approval_authority=target_approval_authority,
            ) == _canonical_product_snapshot(
                control,
                {"result": asdict(control_result)},
                result_approval_authority=control_approval_authority,
            ), ctx
    else:
        with pytest.raises(failure_type) as caught:
            if activity_kind == "execute":
                await world.activities.execute_bound_tool_activity(world.execute_params())
            else:
                assert parked is not None, ctx
                await world.activities.resolve_bound_tool_approval_activity(
                    world.approval_params(parked.approval_id or "")
                )
        assert caught.value is failure, ctx
        assert owner.raised_traceback is not None, ctx
        assert _traceback_tail(caught.value.__traceback__) is owner.raised_traceback, ctx
        assert _traceback_frame_names(caught.value.__traceback__).count("raise_owned") == 1, ctx

    assert owner.raised_traceback is not None, ctx
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == (
        control_snapshot
    ), ctx
    assert len(world.effects) - target_effects_before == control_effect_delta, ctx
    assert _metric_point_multiset(world.telemetry) == [], ctx
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1, ctx
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "other",
    }, ctx
    assert spans[0].status.status_code is StatusCode.UNSET, ctx
    assert spans[0].events == (), ctx


def _install_product_cancellation(
    world: ToolWorld,
    control: ToolWorld,
    *,
    activity_kind: str,
) -> dict[UUID, _OwnedFailure]:
    owners = {
        world.run_id: _OwnedFailure(asyncio.CancelledError("owned-product-cancellation")),
        control.run_id: _OwnedFailure(asyncio.CancelledError("control-product-cancellation")),
    }

    async def cancel_executor(
        context: ToolExecutionContext,
        _payload: BaseModel,
    ) -> BaseModel:
        owners[context.run_id].raise_owned()
        raise AssertionError("unreachable")

    tool_name = "system.echo" if activity_kind == "execute" else "system.approval"
    world.catalog._executors[tool_name] = cancel_executor
    return owners


async def _invoke_cancellation_case(
    world: ToolWorld,
    *,
    activity_kind: str,
    parked: BoundToolResult | None,
) -> tuple[asyncio.CancelledError, tuple[str, ...]]:
    try:
        if activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )
    except asyncio.CancelledError as error:
        return error, _traceback_frame_names(error.__traceback__)
    raise AssertionError("product cancellation was swallowed")


def _expected_product_metric_points(
    *,
    activity_kind: str,
    product_kind: str,
) -> list[tuple[str, tuple[tuple[str, object], ...], int | float]]:
    if product_kind == "cancel":
        return []
    if product_kind == "success":
        return [
            (
                "tool_calls_total",
                (
                    ("outcome", "completed"),
                    ("risk", "write" if activity_kind == "execute" else "elevated"),
                    ("tool_family", "system"),
                ),
                1,
            )
        ]
    outcome = (
        "failed"
        if activity_kind == "execute"
        else {"failure": "rejected", "denied": "denied", "failed": "failed"}[product_kind]
    )
    risk = "read" if activity_kind == "execute" else "elevated"
    failure_class = (
        "internal" if activity_kind == "execute" or product_kind == "failed" else "policy"
    )
    return [
        (
            "tool_call_failures_total",
            (("failure_class", failure_class), ("tool_family", "system")),
            1,
        ),
        (
            "tool_calls_total",
            (("outcome", outcome), ("risk", risk), ("tool_family", "system")),
            1,
        ),
    ]


def _hostile_phase_is_reached(phase: str, product_kind: str) -> bool:
    if phase in {
        "tracer_start",
        "manager_enter",
        "late_attribute",
        "manager_exit",
        "span_end",
        "detach",
    }:
        return True
    if phase in {"error_status", "error_attribute"}:
        return product_kind in {"failure", "denied", "failed"}
    if phase.startswith("calls_"):
        return product_kind != "cancel"
    if phase.startswith("failures_"):
        return product_kind in {"failure", "denied", "failed"}
    raise AssertionError(f"unknown hostile phase {phase}")


async def test_hostile_telemetry_is_diagnostic_only_after_valid_schema() -> None:
    # Loop-folded: the exact former parametrize cross-product
    # (_HOSTILE_PHASES x _HOSTILE_PRODUCT_CASES x diagnostic types), one
    # isolated world and monkeypatch scope per case.
    for phase in _HOSTILE_PHASES:
        for activity_kind, product_kind in _HOSTILE_PRODUCT_CASES:
            for diagnostic_type in (
                RuntimeError,
                ValueError,
                KeyError,
                AttributeError,
                _WorkerDiagnostic,
            ):
                ctx = (
                    f"phase={phase} activity_kind={activity_kind}"
                    f" product_kind={product_kind}"
                    f" diagnostic_type={diagnostic_type.__name__}"
                )
                with _named_case(ctx):
                    async with _fresh_world_context() as world:
                        with pytest.MonkeyPatch.context() as monkeypatch:
                            await _check_hostile_telemetry_diagnostic_case(
                                world,
                                monkeypatch,
                                diagnostic_type=diagnostic_type,
                                activity_kind=activity_kind,
                                phase=phase,
                                product_kind=product_kind,
                                ctx=ctx,
                            )


async def _check_hostile_telemetry_diagnostic_case(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    diagnostic_type: type[BaseException],
    activity_kind: str,
    phase: str,
    product_kind: str,
    ctx: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    bound_results: dict[UUID, list[BoundToolResult]] = {}
    raised_errors: dict[UUID, list[tuple[ApplicationError, TracebackType | None]]] = {}
    original_bound_result = activities_module._bound_result
    original_raise_failure = activities_module._raise_ordinary_failure

    def observed_bound_result(outcome: object) -> BoundToolResult:
        result = original_bound_result(cast(Any, outcome))
        bound_results.setdefault(cast(Any, outcome).tool_call_id, []).append(result)
        return result

    def observed_raise_failure(outcome: object) -> None:
        try:
            original_raise_failure(cast(Any, outcome))
        except ApplicationError as error:
            raised_errors.setdefault(cast(Any, outcome).tool_call_id, []).append(
                (error, error.__traceback__)
            )
            raise

    monkeypatch.setattr(activities_module, "_bound_result", observed_bound_result)
    monkeypatch.setattr(activities_module, "_raise_ordinary_failure", observed_raise_failure)
    cancellation_owners: dict[UUID, _OwnedFailure] | None = None
    effects_before_control = len(world.effects)
    if product_kind == "cancel":
        cancellation_owners = _install_product_cancellation(
            world,
            control,
            activity_kind=activity_kind,
        )
        control_cancellation, control_cancel_frames = await _invoke_cancellation_case(
            control,
            activity_kind=activity_kind,
            parked=control_parked,
        )
        assert control_cancellation is cancellation_owners[control.run_id].failure, ctx
        control_result = None
        control_error = None
        control_frames: tuple[tuple[str, str, int], ...] = ()
    else:
        control_result, control_error, control_frames = await _invoke_terminal_case(
            control,
            activity_kind=activity_kind,
            product_kind=product_kind,
            parked=control_parked,
        )
        control_cancel_frames = ()
    control_effect_delta = len(world.effects) - effects_before_control
    control_product_snapshot = await control.product_snapshot()
    control_snapshot = _canonical_product_snapshot(control, control_product_snapshot)
    world.telemetry.exporter.clear()
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    diagnostic = diagnostic_type("telemetry")
    owner, tracer, metrics = _install_hostile_telemetry(
        world,
        phase=phase,
        failure=diagnostic,
    )
    effects_before_product = len(world.effects)

    if product_kind == "cancel":
        product_cancellation, product_cancel_frames = await _invoke_cancellation_case(
            world,
            activity_kind=activity_kind,
            parked=parked,
        )
        assert cancellation_owners is not None, ctx
        assert product_cancellation is cancellation_owners[world.run_id].failure, ctx
        assert product_cancel_frames == control_cancel_frames, ctx
        assert cancellation_owners[world.run_id].raised_traceback is not None, ctx
        assert _traceback_tail(product_cancellation.__traceback__) is (
            cancellation_owners[world.run_id].raised_traceback
        ), ctx
    else:
        result, error, frames = await _invoke_terminal_case(
            world,
            activity_kind=activity_kind,
            product_kind=product_kind,
            parked=parked,
        )
        if control_error is not None:
            assert result is None, ctx
            assert error is not None, ctx
            assert _application_error_public(error) == (_application_error_public(control_error)), (
                ctx
            )
            assert frames == control_frames, ctx
            owned_error, owned_traceback = raised_errors[world.invocation_id][-1]
            assert error is owned_error, ctx
            assert _traceback_tail(error.__traceback__) is _traceback_tail(owned_traceback), ctx
        else:
            assert control_result is not None, ctx
            assert result is not None, ctx
            assert error is None, ctx
            assert result is bound_results[world.invocation_id][-1], ctx
            target_product_snapshot = await world.product_snapshot()
            target_approval_authority = _bound_result_approval_authority(
                result,
                parked,
                target_product_snapshot,
            )
            control_approval_authority = _bound_result_approval_authority(
                control_result,
                control_parked,
                control_product_snapshot,
            )
            assert _canonical_product_snapshot(
                world,
                {"result": asdict(result)},
                result_approval_authority=target_approval_authority,
            ) == _canonical_product_snapshot(
                control,
                {"result": asdict(control_result)},
                result_approval_authority=control_approval_authority,
            ), ctx

    target_product_snapshot = await world.product_snapshot()
    assert _canonical_product_snapshot(world, target_product_snapshot) == control_snapshot, ctx
    assert len(world.effects) - effects_before_product == control_effect_delta, ctx
    assert len(gateway_calls.get(control.run_id, [])) == 1, ctx
    assert len(gateway_calls.get(world.run_id, [])) == 1, ctx
    reached = _hostile_phase_is_reached(phase, product_kind)
    assert (owner.raised_traceback is not None) is reached, ctx
    assert otel_context.get_current() is entry_context, ctx
    assert trace.get_current_span() is entry_span, ctx
    if tracer is not None:
        assert len(tracer.calls) == 1, ctx
        expected_enter = int(phase != "tracer_start")
        expected_exit = int(phase not in {"tracer_start", "manager_enter"})
        assert tracer.manager.enter_calls == expected_enter, ctx
        assert tracer.manager.exit_calls == expected_exit, ctx
        assert tracer.span.end_calls == expected_exit, ctx
        assert tracer.manager.token is None, ctx
        assert tracer.span.event_calls == [], ctx
        assert tracer.span.name_calls == [], ctx
        assert tracer.span.exception_calls == [], ctx
        downstream_span = gateway_calls[world.run_id][0]
        if phase in {"tracer_start", "manager_enter"}:
            assert downstream_span is entry_span, ctx
        else:
            assert downstream_span is tracer.span, ctx
    else:
        assert metrics is not None, ctx
        expected_getters: list[str] = []
        if product_kind != "cancel":
            expected_getters.append("tool_calls_total")
        if product_kind in {"failure", "denied", "failed"}:
            expected_getters.append("tool_call_failures_total")
        assert metrics.getter_calls == expected_getters, ctx
        assert metrics.getter_calls.count(metrics.target) == int(reached), ctx
        if metrics.instrument is not None:
            assert metrics.instrument.calls == int(phase.endswith("_write") and reached), ctx
        spans = _tool_spans(world.telemetry)
        assert len(spans) == 1, ctx
        assert gateway_calls[world.run_id][0].get_span_context() == spans[0].context, ctx

    expected_points = _expected_product_metric_points(
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    if reached and phase.startswith(("calls_", "failures_")):
        target = "tool_calls_total" if phase.startswith("calls_") else "tool_call_failures_total"
        expected_points = [point for point in expected_points if point[0] != target]
    assert _metric_point_multiset(world.telemetry) == sorted(expected_points), ctx


async def test_hostile_telemetry_never_swallows_or_reraises_fatal_authority() -> None:
    # Loop-folded: the exact former parametrize cross-product
    # (_HOSTILE_PHASES x _HOSTILE_PRODUCT_CASES x fatal types), one isolated
    # world and monkeypatch scope per case.
    for phase in _HOSTILE_PHASES:
        for activity_kind, product_kind in _HOSTILE_PRODUCT_CASES:
            for fatal_type in (KeyboardInterrupt, SystemExit):
                ctx = (
                    f"phase={phase} activity_kind={activity_kind}"
                    f" product_kind={product_kind} fatal_type={fatal_type.__name__}"
                )
                with _named_case(ctx):
                    async with _fresh_world_context() as world:
                        with pytest.MonkeyPatch.context() as monkeypatch:
                            await _check_hostile_telemetry_fatal_case(
                                world,
                                monkeypatch,
                                fatal_type=fatal_type,
                                activity_kind=activity_kind,
                                phase=phase,
                                product_kind=product_kind,
                                ctx=ctx,
                            )


async def _check_hostile_telemetry_fatal_case(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fatal_type: type[BaseException],
    activity_kind: str,
    phase: str,
    product_kind: str,
    ctx: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind=product_kind,
    )
    initial_snapshot = _canonical_product_snapshot(world, await world.product_snapshot())
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    cancellation_owners: dict[UUID, _OwnedFailure] | None = None
    effects_before_control = len(world.effects)
    if product_kind == "cancel":
        cancellation_owners = _install_product_cancellation(
            world,
            control,
            activity_kind=activity_kind,
        )
        control_cancellation, control_cancel_frames = await _invoke_cancellation_case(
            control,
            activity_kind=activity_kind,
            parked=control_parked,
        )
        assert control_cancellation is cancellation_owners[control.run_id].failure, ctx
        control_result = None
        control_error = None
        control_frames: tuple[tuple[str, str, int], ...] = ()
    else:
        control_result, control_error, control_frames = await _invoke_terminal_case(
            control,
            activity_kind=activity_kind,
            product_kind=product_kind,
            parked=control_parked,
        )
        control_cancel_frames = ()
    control_effect_delta = len(world.effects) - effects_before_control
    control_product_snapshot = await control.product_snapshot()
    control_snapshot = _canonical_product_snapshot(control, control_product_snapshot)
    world.telemetry.exporter.clear()
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    fatal = fatal_type("fatal-tool-telemetry")
    owner, tracer, metrics = _install_hostile_telemetry(
        world,
        phase=phase,
        failure=fatal,
    )
    reached = _hostile_phase_is_reached(phase, product_kind)
    effects_before_product = len(world.effects)

    if not reached:
        if product_kind == "cancel":
            cancellation, cancellation_frames = await _invoke_cancellation_case(
                world,
                activity_kind=activity_kind,
                parked=parked,
            )
            assert cancellation_owners is not None, ctx
            assert cancellation is cancellation_owners[world.run_id].failure, ctx
            assert cancellation_frames == control_cancel_frames, ctx
            target_product_snapshot = await world.product_snapshot()
        else:
            result, error, result_frames = await _invoke_terminal_case(
                world,
                activity_kind=activity_kind,
                product_kind=product_kind,
                parked=parked,
            )
            target_product_snapshot = await world.product_snapshot()
            if control_error is not None:
                assert result is None, ctx
                assert error is not None, ctx
                assert _application_error_public(error) == (
                    _application_error_public(control_error)
                ), ctx
                assert result_frames == control_frames, ctx
            else:
                assert control_result is not None, ctx
                assert result is not None, ctx
                assert error is None, ctx
                target_approval_authority = _bound_result_approval_authority(
                    result,
                    parked,
                    target_product_snapshot,
                )
                control_approval_authority = _bound_result_approval_authority(
                    control_result,
                    control_parked,
                    control_product_snapshot,
                )
                assert _canonical_product_snapshot(
                    world,
                    {"result": asdict(result)},
                    result_approval_authority=target_approval_authority,
                ) == _canonical_product_snapshot(
                    control,
                    {"result": asdict(control_result)},
                    result_approval_authority=control_approval_authority,
                ), ctx
        assert owner.raised_traceback is None, ctx
        assert _canonical_product_snapshot(world, target_product_snapshot) == control_snapshot, ctx
        assert len(gateway_calls.get(world.run_id, [])) == 1, ctx
        assert len(world.effects) - effects_before_product == control_effect_delta, ctx
        assert _metric_point_multiset(world.telemetry) == _expected_product_metric_points(
            activity_kind=activity_kind,
            product_kind=product_kind,
        ), ctx
        assert otel_context.get_current() is entry_context, ctx
        assert trace.get_current_span() is entry_span, ctx
        return

    with pytest.raises(fatal_type) as caught:
        if activity_kind == "execute":
            await world.activities.execute_bound_tool_activity(world.execute_params())
        else:
            assert parked is not None, ctx
            await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id or "")
            )

    assert caught.value is fatal, ctx
    assert owner.raised_traceback is not None, ctx
    assert _traceback_tail(caught.value.__traceback__) is owner.raised_traceback, ctx
    frames = _traceback_frame_names(caught.value.__traceback__)
    # Same check as before the loop-fold: the frame that invoked the activity
    # (now this extracted per-case helper) heads the propagated traceback.
    assert frames[0] == "_check_hostile_telemetry_fatal_case", ctx
    assert frames[-1] == "raise_owned", ctx
    assert frames.count("raise_owned") == 1, ctx
    assert otel_context.get_current() is entry_context, ctx
    assert trace.get_current_span() is entry_span, ctx
    if tracer is not None:
        assert tracer.span.event_calls == [], ctx
        assert tracer.span.name_calls == [], ctx
        assert tracer.span.exception_calls == [], ctx
    pre_product = phase in {"tracer_start", "manager_enter"}
    if pre_product:
        assert _canonical_product_snapshot(world, await world.product_snapshot()) == (
            initial_snapshot
        ), ctx
        assert gateway_calls.get(world.run_id, []) == [], ctx
        assert len(world.effects) - effects_before_product == 0, ctx
    else:
        assert _canonical_product_snapshot(world, await world.product_snapshot()) == (
            control_snapshot
        ), ctx
        assert len(gateway_calls.get(world.run_id, [])) == 1, ctx
        assert len(world.effects) - effects_before_product == control_effect_delta, ctx
        downstream_span = gateway_calls[world.run_id][0]
        if tracer is not None:
            assert downstream_span is tracer.span, ctx
        else:
            assert metrics is not None, ctx
            spans = _tool_spans(world.telemetry)
            assert len(spans) == 1, ctx
            assert downstream_span.get_span_context() == spans[0].context, ctx


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
@pytest.mark.parametrize(
    "phase",
    ["late_attribute", "manager_exit", "span_end", "detach"],
)
async def test_secondary_telemetry_cancellation_never_replaces_active_product_cancellation(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
    phase: str,
) -> None:
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    product_owners = _install_product_cancellation(
        world,
        control,
        activity_kind=activity_kind,
    )
    effects_before_control = len(world.effects)
    control_cancellation, control_frames = await _invoke_cancellation_case(
        control,
        activity_kind=activity_kind,
        parked=control_parked,
    )
    control_owner = product_owners[control.run_id]
    _assert_exact_owned_failure(control_cancellation, control_owner)
    control_chain = _exception_chain_shape(control_cancellation)
    control_effect_delta = len(world.effects) - effects_before_control
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    diagnostic = asyncio.CancelledError("diagnostic-during-product-cancellation")
    diagnostic_owner, tracer, _metrics = _install_hostile_telemetry(
        world,
        phase=phase,
        failure=diagnostic,
    )
    effects_before_target = len(world.effects)

    product_cancellation, product_frames = await _invoke_cancellation_case(
        world,
        activity_kind=activity_kind,
        parked=parked,
    )

    product_owner = product_owners[world.run_id]
    _assert_exact_owned_failure(product_cancellation, product_owner)
    assert product_frames == control_frames
    assert _exception_chain_shape(product_cancellation) == control_chain
    assert diagnostic_owner.raised_traceback is not None
    assert tracer is not None
    assert tracer.manager.exit_calls == 1
    assert tracer.span.end_calls == 1
    assert tracer.manager.token is None
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span
    assert gateway_calls[control.run_id]
    assert gateway_calls[world.run_id] == [tracer.span]
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert len(world.effects) - effects_before_target == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == []


@pytest.mark.parametrize("activity_kind", ["execute", "approval"])
async def test_cancellation_path_mapper_cancellation_is_secondary_to_product_cancellation(
    world: ToolWorld,
    monkeypatch: pytest.MonkeyPatch,
    activity_kind: str,
) -> None:
    gateway_calls = _install_gateway_current_span_spy(monkeypatch)
    control = await world.clone_isolated()
    _use_noop_activity_runtime(control)
    control_parked = await _prepare_terminal_case(
        control,
        activity_kind=activity_kind,
        product_kind="success",
    )
    parked = await _prepare_terminal_case(
        world,
        activity_kind=activity_kind,
        product_kind="success",
    )
    gateway_counts_before = {
        control.run_id: len(gateway_calls.get(control.run_id, [])),
        world.run_id: len(gateway_calls.get(world.run_id, [])),
    }
    product_owners = _install_product_cancellation(
        world,
        control,
        activity_kind=activity_kind,
    )
    effects_before_control = len(world.effects)
    control_cancellation, control_frames = await _invoke_cancellation_case(
        control,
        activity_kind=activity_kind,
        parked=control_parked,
    )
    control_owner = product_owners[control.run_id]
    _assert_exact_owned_failure(control_cancellation, control_owner)
    control_chain = _exception_chain_shape(control_cancellation)
    control_effect_delta = len(world.effects) - effects_before_control
    control_snapshot = _canonical_product_snapshot(control, await control.product_snapshot())
    world.telemetry.exporter.clear()
    mapper_cancellation = asyncio.CancelledError("secondary-cancellation-path-mapper")
    mapper_owner = _OwnedFailure(mapper_cancellation)

    def fail_mapper(_catalog: object, _tool_name: object, _status: object) -> object:
        mapper_owner.raise_owned()
        raise AssertionError("unreachable")

    monkeypatch.setattr(activities_module, "describe_tool_telemetry", fail_mapper)
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    effects_before_target = len(world.effects)

    product_cancellation, product_frames = await _invoke_cancellation_case(
        world,
        activity_kind=activity_kind,
        parked=parked,
    )

    product_owner = product_owners[world.run_id]
    _assert_exact_owned_failure(product_cancellation, product_owner)
    assert product_frames == control_frames
    assert _exception_chain_shape(product_cancellation) == control_chain
    assert mapper_owner.raised_traceback is not None
    assert otel_context.get_current() is entry_context
    assert trace.get_current_span() is entry_span
    assert len(gateway_calls[control.run_id]) == gateway_counts_before[control.run_id] + 1
    assert len(gateway_calls[world.run_id]) == gateway_counts_before[world.run_id] + 1
    assert _canonical_product_snapshot(world, await world.product_snapshot()) == control_snapshot
    assert len(world.effects) - effects_before_target == control_effect_delta
    assert _metric_point_multiset(world.telemetry) == []
    spans = _tool_spans(world.telemetry)
    assert len(spans) == 1
    assert dict(spans[0].attributes) == {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "cancelled",
    }
    assert spans[0].status.status_code is StatusCode.UNSET
    assert spans[0].events == ()


@dataclass(frozen=True)
class _LoggerState:
    level: int
    disabled: bool
    propagate: bool
    handlers: tuple[logging.Handler, ...]


@dataclass(frozen=True)
class _HandlerState:
    level: int
    formatter: logging.Formatter | None
    filters: tuple[logging.Filter, ...]


@pytest.fixture
def tool_log_capture(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[pytest.LogCaptureFixture]:
    logger_names = (
        "",
        "aiosqlite",
        "jhin_tool_worker",
        "jhin_tools",
        __name__,
    )
    loggers = {name: logging.getLogger(name) for name in logger_names}
    logger_states = {
        name: _LoggerState(
            level=logger.level,
            disabled=logger.disabled,
            propagate=logger.propagate,
            handlers=tuple(logger.handlers),
        )
        for name, logger in loggers.items()
    }
    handlers = {
        caplog.handler,
        *(handler for logger in loggers.values() for handler in logger.handlers),
    }
    handler_states = {
        handler: _HandlerState(
            level=handler.level,
            formatter=handler.formatter,
            filters=tuple(handler.filters),
        )
        for handler in handlers
    }
    try:
        root = loggers[""]
        root.disabled = False
        root.setLevel(logging.WARNING)
        loggers["aiosqlite"].disabled = False
        loggers["aiosqlite"].setLevel(logging.WARNING)
        for name in ("jhin_tool_worker", "jhin_tools", __name__):
            logger = loggers[name]
            logger.disabled = False
            logger.propagate = True
            logger.setLevel(logging.DEBUG)
        caplog.handler.setLevel(logging.DEBUG)
        caplog.clear()
        yield caplog
    finally:
        for handler, state in handler_states.items():
            handler.setLevel(state.level)
            handler.setFormatter(state.formatter)
            handler.filters[:] = state.filters
        for name, state in logger_states.items():
            logger = loggers[name]
            logger.setLevel(state.level)
            logger.disabled = state.disabled
            logger.propagate = state.propagate
            logger.handlers[:] = state.handlers
        assert {
            name: _LoggerState(
                level=logger.level,
                disabled=logger.disabled,
                propagate=logger.propagate,
                handlers=tuple(logger.handlers),
            )
            for name, logger in loggers.items()
        } == logger_states
        assert {
            handler: _HandlerState(
                level=handler.level,
                formatter=handler.formatter,
                filters=tuple(handler.filters),
            )
            for handler in handlers
        } == handler_states


@pytest.mark.parametrize(
    "privacy_case",
    [
        "approval-success",
        "execution-failure",
        "unknown-tool",
        "execution-replay",
        "approval-replay",
        "approval-denied-replay",
        "approval-failed-replay",
        "approval-execution-unknown",
        "execution-cancel",
        "approval-cancel",
    ],
)
async def test_complete_tool_export_and_process_sinks_exclude_all_product_material(
    world: ToolWorld,
    tool_log_capture: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    privacy_case: str,
) -> None:
    caplog = tool_log_capture
    assert logging.getLogger().getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("aiosqlite").getEffectiveLevel() >= logging.WARNING
    caplog.clear()
    input_canary = f"private-input-{privacy_case}-canary"
    url_canary = f"https://private-{privacy_case}.invalid/secret"
    secret_canary = f"private-secret-{privacy_case}-canary"
    provider_canary = f"private-provider-{privacy_case}-canary"
    connection_canary = str(new_uuid7())
    request_canary = str(new_uuid7())
    extras = {
        "private_url": url_canary,
        "private_secret": secret_canary,
        "private_provider_id": provider_canary,
        "private_connection_id": connection_canary,
        "private_request_id": request_canary,
    }
    logging.getLogger("aiosqlite").debug(
        "driver-log-must-be-suppressed %s",
        input_canary,
    )
    logging.getLogger("jhin_tool_worker.telemetry.child").debug(
        "bounded-tool-descendant",
        extra={"bounded_descendant_tool_field": "bounded-descendant-value"},
    )
    dynamic_canaries: list[str] = []
    expected_span_names: list[str]
    expected_outcomes: list[str]
    expected_points: list[tuple[str, tuple[tuple[str, object], ...], int | float]]

    if privacy_case == "approval-success":
        parked = await world.park(value=input_canary, extra=extras)
        assert parked.approval_id is not None
        dynamic_canaries.append(parked.approval_id)
        async with world.sessions() as session:
            approval = await session.get(Approval, UUID(parked.approval_id))
            assert approval is not None
            approval.reason = "private-approval-reason-canary"
            approval.action_payload_sanitized = {
                **approval.action_payload_sanitized,
                "private_approval_payload": "private-approval-payload-canary",
            }
            await session.commit()
        await world.decide(parked.approval_id, ApprovalStatus.APPROVED.value)
        result = await world.activities.resolve_bound_tool_approval_activity(
            world.approval_params(parked.approval_id)
        )
        assert result.status == "executed"
        expected_span_names = ["tool.gateway.execute", "tool.approval.resolve"]
        expected_outcomes = ["accepted", "completed"]
        expected_points = [
            (
                "tool_calls_total",
                (
                    ("outcome", "completed"),
                    ("risk", "elevated"),
                    ("tool_family", "system"),
                ),
                1,
            )
        ]
        static_canaries = [
            "system.approval",
            "private-description-system.approval",
            f"private-output-{input_canary}",
            "private-approval-reason-canary",
            "private-approval-payload-canary",
        ]
    elif privacy_case == "execution-failure":
        await world.seed_manifest("system.fail", value=input_canary, extra=extras)
        with pytest.raises(ApplicationError):
            await world.activities.execute_bound_tool_activity(world.execute_params())
        expected_span_names = ["tool.gateway.execute"]
        expected_outcomes = ["failed"]
        expected_points = [
            (
                "tool_call_failures_total",
                (("failure_class", "internal"), ("tool_family", "system")),
                1,
            ),
            (
                "tool_calls_total",
                (("outcome", "failed"), ("risk", "read"), ("tool_family", "system")),
                1,
            ),
        ]
        static_canaries = [
            "system.fail",
            "private-description-system.fail",
            "private-executor-message-canary",
            "private_executor_code",
        ]
    elif privacy_case == "unknown-tool":
        raw_tool_name = "private.unknown_tool_canary"
        await world.seed_manifest(raw_tool_name, value=input_canary, extra=extras)
        with pytest.raises(ApplicationError):
            await world.activities.execute_bound_tool_activity(world.execute_params())
        expected_span_names = ["tool.gateway.execute"]
        expected_outcomes = ["denied"]
        expected_points = [
            (
                "tool_call_failures_total",
                (("failure_class", "policy"), ("tool_family", "other")),
                1,
            ),
            (
                "tool_calls_total",
                (("outcome", "denied"), ("risk", "other"), ("tool_family", "other")),
                1,
            ),
        ]
        static_canaries = [raw_tool_name, "tool_not_found"]
    elif privacy_case == "execution-replay":
        await world.seed_manifest("system.echo", value=input_canary, extra=extras)
        first = await world.activities.execute_bound_tool_activity(world.execute_params())
        second = await world.activities.execute_bound_tool_activity(world.execute_params())
        assert first == second
        expected_span_names = ["tool.gateway.execute", "tool.gateway.execute"]
        expected_outcomes = ["completed", "completed"]
        expected_points = [
            (
                "tool_calls_total",
                (
                    ("outcome", "completed"),
                    ("risk", "write"),
                    ("tool_family", "system"),
                ),
                1,
            )
        ]
        static_canaries = [
            "system.echo",
            "private-description-system.echo",
            f"private-output-{input_canary}",
        ]
    elif privacy_case in {
        "approval-replay",
        "approval-denied-replay",
        "approval-failed-replay",
        "approval-execution-unknown",
        "approval-cancel",
    }:
        parked = await world.park(value=input_canary, extra=extras)
        assert parked.approval_id is not None
        dynamic_canaries.append(parked.approval_id)
        async with world.sessions() as session:
            approval = await session.get(Approval, UUID(parked.approval_id))
            assert approval is not None
            approval.reason = "private-approval-reason-canary"
            approval.action_payload_sanitized = {
                **approval.action_payload_sanitized,
                "private_approval_payload": "private-approval-payload-canary",
            }
            await session.commit()
        await world.decide(
            parked.approval_id,
            ApprovalStatus.REJECTED.value
            if privacy_case == "approval-replay"
            else ApprovalStatus.APPROVED.value,
        )
        if privacy_case == "approval-denied-replay":
            async with world.sessions() as session:
                await session.execute(
                    delete(AgentCapabilityGrant).where(
                        AgentCapabilityGrant.agent_id == world.agent_id,
                        AgentCapabilityGrant.capability == "system.approval",
                    )
                )
                await session.commit()
        elif privacy_case == "approval-failed-replay":
            world.catalog._executors["system.approval"] = world.failure_executor

        if privacy_case in {
            "approval-replay",
            "approval-denied-replay",
            "approval-failed-replay",
        }:
            first = await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id)
            )
            second = await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id)
            )
            assert first == second
            replay_outcome = {
                "approval-replay": "rejected",
                "approval-denied-replay": "denied",
                "approval-failed-replay": "failed",
            }[privacy_case]
            failure_class = "internal" if replay_outcome == "failed" else "policy"
            assert first.status == replay_outcome
            expected_span_names = [
                "tool.gateway.execute",
                "tool.approval.resolve",
                "tool.approval.resolve",
            ]
            expected_outcomes = ["accepted", replay_outcome, replay_outcome]
            expected_points = [
                (
                    "tool_call_failures_total",
                    (("failure_class", failure_class), ("tool_family", "system")),
                    1,
                ),
                (
                    "tool_calls_total",
                    (
                        ("outcome", replay_outcome),
                        ("risk", "elevated"),
                        ("tool_family", "system"),
                    ),
                    1,
                ),
            ]
        elif privacy_case == "approval-execution-unknown":
            async with world.sessions() as session:
                row = await session.get(ToolCall, world.invocation_id)
                assert row is not None
                row.status = ToolCallStatus.EXECUTING.value
                await session.commit()
            result = await world.activities.resolve_bound_tool_approval_activity(
                world.approval_params(parked.approval_id)
            )
            assert result.status == "execution_unknown"
            expected_span_names = ["tool.gateway.execute", "tool.approval.resolve"]
            expected_outcomes = ["accepted", "execution_unknown"]
            expected_points = [
                (
                    "tool_call_failures_total",
                    (("failure_class", "execution_unknown"), ("tool_family", "system")),
                    1,
                ),
                (
                    "tool_calls_total",
                    (
                        ("outcome", "execution_unknown"),
                        ("risk", "elevated"),
                        ("tool_family", "system"),
                    ),
                    1,
                ),
            ]
        else:
            cancellation = asyncio.CancelledError("private-approval-cancellation-canary")
            owner = _OwnedFailure(cancellation)

            async def cancel_approval(
                _context: ToolExecutionContext,
                _payload: BaseModel,
            ) -> BaseModel:
                owner.raise_owned()
                raise AssertionError("unreachable")

            world.catalog._executors["system.approval"] = cancel_approval
            with pytest.raises(asyncio.CancelledError) as caught:
                await world.activities.resolve_bound_tool_approval_activity(
                    world.approval_params(parked.approval_id)
                )
            assert caught.value is cancellation
            expected_span_names = ["tool.gateway.execute", "tool.approval.resolve"]
            expected_outcomes = ["accepted", "cancelled"]
            expected_points = []
        static_canaries = [
            "system.approval",
            "private-description-system.approval",
            "private-approval-reason-canary",
            "private-approval-payload-canary",
        ]
        if privacy_case == "approval-cancel":
            static_canaries.append("private-approval-cancellation-canary")
        elif privacy_case == "approval-failed-replay":
            static_canaries.extend(["private-executor-message-canary", "private_executor_code"])
    else:
        await world.seed_manifest("system.echo", value=input_canary, extra=extras)
        cancellation = asyncio.CancelledError("private-execution-cancellation-canary")
        owner = _OwnedFailure(cancellation)

        async def cancel_execution(
            _context: ToolExecutionContext,
            _payload: BaseModel,
        ) -> BaseModel:
            owner.raise_owned()
            raise AssertionError("unreachable")

        world.catalog._executors["system.echo"] = cancel_execution
        with pytest.raises(asyncio.CancelledError) as caught:
            await world.activities.execute_bound_tool_activity(world.execute_params())
        assert caught.value is cancellation
        expected_span_names = ["tool.gateway.execute"]
        expected_outcomes = ["cancelled"]
        expected_points = []
        static_canaries = [
            "system.echo",
            "private-description-system.echo",
            "private-execution-cancellation-canary",
        ]

    async with world.sessions() as session:
        task = await session.get(Task, world.task_id)
        assert task is not None
        correlation_id = task.correlation_id
    print("bounded-tool-stdout")
    logging.getLogger(__name__).debug(
        "bounded-tool-log",
        extra={"bounded_structured_tool_field": "bounded-tool-value"},
    )
    captured = capsys.readouterr()
    assert any(
        record.__dict__.get("bounded_structured_tool_field") == "bounded-tool-value"
        for record in caplog.records
    )
    assert any(
        record.__dict__.get("bounded_descendant_tool_field") == "bounded-descendant-value"
        for record in caplog.records
    )
    logs = json.dumps([record.__dict__ for record in caplog.records], sort_keys=True, default=str)
    process_payload = "\n".join((caplog.text, logs, captured.out, captured.err))
    export_payload = _complete_export_payload(world.telemetry)
    canaries = [
        input_canary,
        url_canary,
        secret_canary,
        provider_canary,
        connection_canary,
        request_canary,
        "private-input-schema-canary",
        "private-output-schema-canary",
        "private-manifest-canary",
        str(world.workspace_id),
        str(world.task_id),
        str(world.run_id),
        str(world.agent_id),
        str(world.invocation_id),
        str(correlation_id),
        *static_canaries,
        *dynamic_canaries,
    ]
    for canary in canaries:
        assert canary not in export_payload
        assert canary not in process_payload

    all_spans = list(world.telemetry.exporter.get_finished_spans())
    assert [span.name for span in all_spans] == expected_span_names
    assert [span.attributes["jhin.outcome"] for span in all_spans] == expected_outcomes
    assert _metric_point_multiset(world.telemetry) == sorted(expected_points)
    for span in all_spans:
        assert set(span.attributes) <= {
            "jhin.tool_family",
            "jhin.risk",
            "jhin.outcome",
            "error.type",
            "error.code",
        }
        assert span.events == ()
        assert span.links == ()
    for point in _metric_points(world.telemetry, "tool_calls_total"):
        assert set(point.attributes) == {"tool_family", "risk", "outcome"}
    for point in _metric_points(world.telemetry, "tool_call_failures_total"):
        assert set(point.attributes) == {"tool_family", "failure_class"}


def _production_process_sink_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    violations: list[str] = []
    forbidden_modules = frozenset({"builtins", "logging"})
    forbidden_names = frozenset({"__builtins__", "__import__", "logging", "print"})
    forbidden_attributes = frozenset(
        {
            "critical",
            "debug",
            "error",
            "exception",
            "getLogger",
            "info",
            "log",
            "print",
            "warning",
        }
    )
    forbidden_dynamic_names = frozenset({"builtins", "getLogger", "logging", "print"})

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in forbidden_modules:
                    violations.append(f"import:{node.lineno}:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".", 1)[0] in forbidden_modules:
                violations.append(f"from-import:{node.lineno}:{node.module}")
        elif isinstance(node, ast.Name) and node.id in forbidden_names:
            violations.append(f"name:{node.lineno}:{node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
            violations.append(f"attribute:{node.lineno}:{node.attr}")
        elif isinstance(node, ast.Constant) and node.value in forbidden_dynamic_names:
            violations.append(f"dynamic-name:{node.lineno}:{node.value}")
    return violations


def test_tool_privacy_capture_does_not_hide_production_prints_or_enable_root_debug() -> None:
    production_paths = (
        Path(inspect.getsourcefile(ToolActivities) or ""),
        REPO_ROOT / "packages/tools/src/jhin_tools/telemetry.py",
    )
    assert {
        str(path): _production_process_sink_violations(path.read_text())
        for path in production_paths
    } == {str(path): [] for path in production_paths}
    fixture_source = inspect.getsource(tool_log_capture)
    assert "caplog.set_level" not in fixture_source
    assert 'loggers["aiosqlite"].setLevel(logging.WARNING)' in fixture_source
    assert "root.setLevel(logging.WARNING)" in fixture_source


@pytest.mark.parametrize(
    "source",
    [
        "def leak(secret):\n    print(secret)\n",
        "def leak(secret):\n    emit = print\n    emit(secret)\n",
        "from builtins import print as emit\nemit('secret')\n",
        "import builtins as b\nemit = b.print\nemit('secret')\n",
        "def leak(secret):\n    __builtins__['print'](secret)\n",
        "import logging as log\nlog.debug('secret')\n",
        "from logging import getLogger as factory\nfactory().info('secret')\n",
        "logger.debug('secret')\n",
        "sink = logger.error\nsink('secret')\n",
        "root = __import__('logging').getLogger()\nroot.warning('secret')\n",
    ],
)
def test_tool_privacy_static_guard_rejects_logging_and_print_aliases(source: str) -> None:
    assert _production_process_sink_violations(source)
