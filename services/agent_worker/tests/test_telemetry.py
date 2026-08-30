"""Committed agent reasoning and terminal-run telemetry contracts."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import textwrap
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, ClassVar, cast
from uuid import UUID

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
from opentelemetry.trace import Tracer
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import set_committed_value
from temporalio.exceptions import ApplicationError

import jhin_agent_worker.projections as projections_module
import jhin_agent_worker.reasoning as reasoning_module
from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.compatibility import AgentCompatibilityActivities
from jhin_agent_worker.projections import AgentProjectionActivities
from jhin_agent_worker.reasoning import AgentReasoningActivities
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, AuditEvent, Message, RunEvent, Task, Workspace
from jhin_domain import ModelProviderType, RunStatus, new_uuid7
from jhin_models import (
    ModelClient,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from jhin_models.telemetry import InstrumentedModelClient
from jhin_observability import JhinMetrics
from jhin_observability.metrics import build_jhin_metrics
from jhin_tools import AGENT_BEFORE_BIND, PHASE9_AFTER_MANIFEST
from jhin_workflows.agent_task.shared import (
    AdvertisedTool,
    FinalizeInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
)


class _OrderedCounter:
    def __init__(self, wrapped: Any, name: str, order: list[str]) -> None:
        self._wrapped = wrapped
        self._name = name
        self._order = order

    def add(self, amount: int | float, **labels: str) -> None:
        self._order.append(self._name)
        self._wrapped.add(amount, **labels)


class _OrderedHistogram:
    def __init__(self, wrapped: Any, name: str, order: list[str]) -> None:
        self._wrapped = wrapped
        self._name = name
        self._order = order

    def record(self, amount: int | float, **labels: str) -> None:
        self._order.append(self._name)
        self._wrapped.record(amount, **labels)


class _CountingInstrument:
    def __init__(self, wrapped: Any, name: str, calls: list[tuple[Any, ...]]) -> None:
        self._wrapped = wrapped
        self._name = name
        self._calls = calls

    def add(self, amount: int | float, **labels: str) -> None:
        self._calls.append(("add", self._name, amount, dict(labels)))
        self._wrapped.add(amount, **labels)

    def record(self, amount: int | float, **labels: str) -> None:
        self._calls.append(("record", self._name, amount, dict(labels)))
        self._wrapped.record(amount, **labels)


class _CountingMetrics:
    is_noop = False

    def __init__(self, wrapped: JhinMetrics) -> None:
        self._wrapped = wrapped
        self.calls: list[tuple[Any, ...]] = []

    def counter(self, name: str) -> _CountingInstrument:
        self.calls.append(("counter", name))
        return _CountingInstrument(self._wrapped.counter(cast(Any, name)), name, self.calls)

    def histogram(self, name: str) -> _CountingInstrument:
        self.calls.append(("histogram", name))
        return _CountingInstrument(
            self._wrapped.histogram(cast(Any, name)),
            name,
            self.calls,
        )

    def set_observable(self, name: str, observations: object) -> None:
        self.calls.append(("observable", name))
        self._wrapped.set_observable(cast(Any, name), cast(Any, observations))


def _ordered_metrics(metrics: JhinMetrics, order: list[str]) -> JhinMetrics:
    return JhinMetrics(
        lambda name: _OrderedCounter(metrics.counter(name), name, order),
        lambda name: _OrderedHistogram(metrics.histogram(name), name, order),
        lambda name, observations: metrics.set_observable(name, observations),
        is_noop=metrics.is_noop,
    )


@dataclass
class _Telemetry:
    metrics: JhinMetrics
    reader: InMemoryMetricReader
    metric_provider: MeterProvider
    tracer: Tracer
    exporter: InMemorySpanExporter
    trace_provider: TracerProvider
    order: list[str]


@contextmanager
def _owned_telemetry() -> Iterator[_Telemetry]:
    """One isolated telemetry world; loop-folded tests open a fresh one per case."""
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()
    order: list[str] = []
    reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
    base_metrics = build_jhin_metrics(cast(Meter, metric_provider.get_meter("agent-test", "1")))
    metrics = _ordered_metrics(base_metrics, order)
    exporter = InMemorySpanExporter()
    trace_provider = TracerProvider(
        resource=Resource.create({"service.name": "agent-test", "safe.resource": "bounded"}),
        shutdown_on_exit=False,
    )
    trace_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = trace_provider.get_tracer("agent-test", "1")
    owned = _Telemetry(
        metrics,
        reader,
        metric_provider,
        tracer,
        exporter,
        trace_provider,
        order,
    )
    try:
        yield owned
    finally:
        assert otel_context.get_current() is entry_context
        assert trace.get_current_span() is entry_span
        trace_provider.shutdown()
        metric_provider.shutdown()


@pytest.fixture
def telemetry() -> Iterator[_Telemetry]:
    with _owned_telemetry() as owned:
        yield owned


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


def _histogram_points(telemetry: _Telemetry, name: str, **labels: str) -> list[Any]:
    return [point for point in _metric_points(telemetry, name) if dict(point.attributes) == labels]


def _spans(telemetry: _Telemetry, name: str) -> list[Any]:
    return [span for span in telemetry.exporter.get_finished_spans() if span.name == name]


def _traceback_contains(
    head: TracebackType | None,
    expected: TracebackType | None,
) -> bool:
    while head is not None:
        if head is expected:
            return True
        head = head.tb_next
    return False


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
        "cause_type": None if error.__cause__ is None else type(error.__cause__),
        "cause_args": None if error.__cause__ is None else error.__cause__.args,
    }


def _point_payload(point: Any) -> dict[str, Any]:
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
        "exemplars": [
            {
                "attributes": dict(getattr(exemplar, "filtered_attributes", {}) or {}),
                "value": getattr(exemplar, "value", None),
                "time_unix_nano": getattr(exemplar, "time_unix_nano", None),
                "span_id": getattr(exemplar, "span_id", None),
                "trace_id": getattr(exemplar, "trace_id", None),
            }
            for exemplar in (getattr(point, "exemplars", ()) or ())
        ],
    }


def _stable_point_payload(point: Any) -> dict[str, Any]:
    """Remove collection timestamps while retaining every recorded point field."""
    payload = _point_payload(point)
    payload.pop("time_unix_nano")
    for exemplar in payload["exemplars"]:
        exemplar.pop("time_unix_nano")
    return payload


def _complete_export_payload(telemetry: _Telemetry) -> str:
    """Serialize every trace/metric field that may carry product material."""
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


class _RawModel(ModelClient):
    def __init__(self) -> None:
        self.responses: list[ModelResponse] = []
        self.requests: list[ModelRequest] = []
        self.generate_error: BaseException | None = None
        self.raised_traceback: TracebackType | None = None
        self.close_calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.generate_error is not None:
            try:
                raise self.generate_error
            except BaseException as error:
                self.raised_traceback = error.__traceback__
                raise
        if not self.responses:
            raise AssertionError("a model response was not configured")
        return self.responses.pop(0)

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        raise AssertionError("agent reasoning must not stream")

    async def verify(self) -> str:
        raise AssertionError("agent reasoning must not verify")

    async def close(self) -> None:
        self.close_calls += 1


class _Publisher:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.events.append(envelope)


_UNSET = object()


class _ProbeSession(AsyncSession):
    fail_next_commit: ClassVar[BaseException | None] = None
    task_correlation_override: ClassVar[object] = _UNSET
    run_started_override: ClassVar[object] = _UNSET
    usage_read_override: ClassVar[tuple[str, object] | None] = None
    commit_order: ClassVar[list[str] | None] = None
    scalar_statements: ClassVar[list[Any] | None] = None
    committed_completed_at: ClassVar[datetime | None] = None

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if type(self).scalar_statements is not None:
            type(self).scalar_statements.append(statement)
        result = await super().scalar(statement, *args, **kwargs)
        if isinstance(result, Task) and type(self).task_correlation_override is not _UNSET:
            result.correlation_id = cast(UUID, type(self).task_correlation_override)
        if isinstance(result, AgentRun) and type(self).run_started_override is not _UNSET:
            set_committed_value(result, "started_at", type(self).run_started_override)
            assert result not in self.dirty
        return result

    async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if type(self).scalar_statements is not None:
            type(self).scalar_statements.append(statement)
        result = await super().scalars(statement, *args, **kwargs)
        override = type(self).usage_read_override
        if override is None:
            return result
        rows = result.all()
        field, value = override
        for row in rows:
            if isinstance(row, RunEvent) and row.event_type == "agent.step.reasoning":
                payload = deepcopy(row.payload_json)
                usage = dict(cast(dict[str, object], payload["usage"]))
                usage[field] = value
                payload["usage"] = usage
                set_committed_value(row, "payload_json", payload)
                assert row not in self.dirty
        return _ScalarRows(rows)

    async def commit(self) -> None:
        failure = type(self).fail_next_commit
        if failure is not None:
            type(self).fail_next_commit = None
            raise failure
        await super().commit()
        for value in self.identity_map.values():
            if isinstance(value, AgentRun) and type(value.completed_at) is datetime:
                type(self).committed_completed_at = value.completed_at
        if type(self).commit_order is not None:
            type(self).commit_order.append("db_commit")


class _ScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)

    def all(self) -> list[Any]:
        return list(self._rows)


class _Barrier:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.failure: BaseException | None = None
        self.callback: Callable[[str, UUID], Awaitable[None]] | None = None

    async def arrive_and_wait(self, name: str, identity: UUID) -> None:
        if name == PHASE9_AFTER_MANIFEST:
            self.order.append("phase9_after_manifest")
        if self.callback is not None:
            await self.callback(name, identity)
        if name == PHASE9_AFTER_MANIFEST and self.failure is not None:
            raise self.failure


class _Resources:
    def __init__(
        self,
        sessions: async_sessionmaker[_ProbeSession],
        telemetry: _Telemetry,
    ) -> None:
        self.runtime = SimpleNamespace(metrics=telemetry.metrics, tracer=telemetry.tracer)
        self.session_factory = sessions
        self.publisher = _Publisher()
        self.crypto = None
        self.test_barrier = _Barrier(telemetry.order)


class _ProbeReasoning(AgentReasoningActivities):
    def __init__(self, resources: Any, order: list[str]) -> None:
        super().__init__(resources)
        self.order = order
        self.hook: Callable[[], Awaitable[None]] | None = None

    async def _after_reasoning_bind_commit(self) -> None:
        self.order.append("after_reasoning_bind_commit")
        if self.hook is not None:
            await self.hook()


@dataclass
class AgentWorld:
    engine: Any
    sessions: async_sessionmaker[_ProbeSession]
    resources: _Resources
    reasoning: _ProbeReasoning
    projections: AgentProjectionActivities
    raw_model: _RawModel
    factory_calls: list[tuple[object, object, object, object, object]]
    params: ReasonAgentStepInput
    snapshot: AgentExecutionSnapshot
    workspace_id: UUID
    task_id: UUID
    run_id: UUID
    agent_id: UUID
    correlation_id: UUID
    telemetry: _Telemetry

    def response(self, **updates: object) -> ModelResponse:
        response = ModelResponse(
            text="bounded completion",
            finish_reason="stop",
            model="bounded-model",
            usage=ModelUsage(input_tokens=7, output_tokens=3, cached_tokens=1),
            latency_ms=11,
            provider_request_id="bounded-provider-request",
        )
        return response.model_copy(update=updates)

    async def count_events(self, event_type: str) -> int:
        async with self.sessions() as session:
            return (
                await session.scalar(
                    select(func.count(RunEvent.id)).where(
                        RunEvent.run_id == self.run_id,
                        RunEvent.event_type == event_type,
                    )
                )
                or 0
            )

    async def count_audits(self, action: str) -> int:
        async with self.sessions() as session:
            return (
                await session.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.workspace_id == self.workspace_id,
                        AuditEvent.action == action,
                    )
                )
                or 0
            )

    async def load_event(self, event_type: str) -> RunEvent | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == self.run_id,
                    RunEvent.event_type == event_type,
                )
            )

    async def load_run(self) -> AgentRun:
        async with self.sessions() as session:
            run = await session.get(AgentRun, self.run_id)
            assert run is not None
            return run

    async def seed_complete_pair(self, *, usage: Mapping[str, object] | None = None) -> None:
        usage_payload = {
            "input_tokens": 7,
            "output_tokens": 3,
            "cached_tokens": 1,
            "cost_micros": 13,
        }
        if usage is not None:
            usage_payload.update(usage)
        async with self.sessions() as session:
            session.add_all(
                (
                    RunEvent(
                        workspace_id=self.workspace_id,
                        task_id=self.task_id,
                        run_id=self.run_id,
                        seq=0,
                        event_type="agent.step.tool_manifest",
                        payload_json={
                            "step": self.params.step_index,
                            "manifest": {"count": 0, "calls": []},
                        },
                    ),
                    RunEvent(
                        workspace_id=self.workspace_id,
                        task_id=self.task_id,
                        run_id=self.run_id,
                        seq=1,
                        event_type="agent.step.reasoning",
                        payload_json={
                            "format_version": 1,
                            "step": self.params.step_index,
                            "completion_sanitized": "bounded completion",
                            "model": "bounded-model",
                            "finish_reason": "stop",
                            "provider_request_id": "bounded-provider-request",
                            "provider_call_ids": [],
                            "transitions": [],
                            "done": True,
                            "usage": usage_payload,
                            "latency_ms": 11,
                        },
                    ),
                )
            )
            await session.commit()

    async def seed_manifest_only(self) -> None:
        async with self.sessions() as session:
            session.add(
                RunEvent(
                    workspace_id=self.workspace_id,
                    task_id=self.task_id,
                    run_id=self.run_id,
                    seq=0,
                    event_type="agent.step.tool_manifest",
                    payload_json={
                        "step": self.params.step_index,
                        "manifest": {"count": 0, "calls": []},
                    },
                )
            )
            await session.commit()


@asynccontextmanager
async def _owned_world(
    monkeypatch: pytest.MonkeyPatch,
    telemetry: _Telemetry,
    tmp_path: Path,
) -> AsyncIterator[AgentWorld]:
    """One isolated database world; loop-folded tests open a fresh one per case."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / f'agent-telemetry-{new_uuid7().hex}.db'}"
    )
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=_ProbeSession,
    )
    resources = _Resources(sessions, telemetry)
    raw_model = _RawModel()
    factory_calls: list[tuple[object, object, object, object, object]] = []

    def build_model(
        provider_type: object,
        *,
        base_url: object,
        api_key: object,
        metrics: object,
        tracer: object,
        **kwargs: object,
    ) -> ModelClient:
        assert kwargs == {}
        factory_calls.append((provider_type, base_url, api_key, metrics, tracer))
        return InstrumentedModelClient(
            raw_model,
            provider_type=provider_type,
            metrics=cast(JhinMetrics, metrics),
            tracer=cast(Tracer, tracer),
        )

    monkeypatch.setattr(reasoning_module, "build_model_client", build_model)
    async with sessions() as session:
        workspace = Workspace(name="Agent telemetry", slug=f"agent-tel-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Telemetry agent", slug="telemetry-agent")
        session.add(agent)
        await session.flush()
        correlation_id = new_uuid7()
        task = Task(
            workspace_id=workspace.id,
            title="Bounded reasoning task",
            description="Bounded reasoning task",
            assigned_agent_id=agent.id,
            correlation_id=correlation_id,
        )
        session.add(task)
        await session.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.RUNNING.value,
            started_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        session.add(run)
        await session.commit()

    snapshot = AgentExecutionSnapshot(
        agent_id=agent.id,
        workspace_id=workspace.id,
        name=agent.name,
        role_title="",
        system_prompt="bounded system prompt",
        autonomy_level="balanced",
        team_id=None,
        team_name=None,
        manager_agent_id=None,
        manager_name=None,
        model_profile=ModelProfileSnapshot(
            profile_id=new_uuid7(),
            provider_id=new_uuid7(),
            provider_type=ModelProviderType.OLLAMA.value,
            base_url="http://localhost:11434/v1",
            secret_id=None,
            model_name="bounded-model",
            display_name="Bounded model",
            input_cost_micros_per_million=1_000_000,
            output_cost_micros_per_million=2_000_000,
        ),
        temperature=None,
        max_output_tokens=None,
        run_limits=RunLimits(max_steps=5, max_run_minutes=5),
    )
    params = ReasonAgentStepInput(
        workspace_id=str(workspace.id),
        task_id=str(task.id),
        run_id=str(run.id),
        agent_id=str(agent.id),
        snapshot_json=snapshot.model_dump_json(),
        step_index=0,
    )
    reasoning = _ProbeReasoning(resources, telemetry.order)
    owned = AgentWorld(
        engine=engine,
        sessions=sessions,
        resources=resources,
        reasoning=reasoning,
        projections=AgentProjectionActivities(cast(Any, resources)),
        raw_model=raw_model,
        factory_calls=factory_calls,
        params=params,
        snapshot=snapshot,
        workspace_id=workspace.id,
        task_id=task.id,
        run_id=run.id,
        agent_id=agent.id,
        correlation_id=correlation_id,
        telemetry=telemetry,
    )
    try:
        yield owned
    finally:
        _ProbeSession.fail_next_commit = None
        _ProbeSession.task_correlation_override = _UNSET
        _ProbeSession.run_started_override = _UNSET
        _ProbeSession.usage_read_override = None
        _ProbeSession.commit_order = None
        _ProbeSession.scalar_statements = None
        _ProbeSession.committed_completed_at = None
        await engine.dispose()


@pytest.fixture
async def world(
    monkeypatch: pytest.MonkeyPatch,
    telemetry: _Telemetry,
    tmp_path: Path,
) -> AsyncIterator[AgentWorld]:
    async with _owned_world(monkeypatch, telemetry, tmp_path) as owned:
        yield owned


def _orm_row_payload(row: Any) -> tuple[tuple[str, object], ...]:
    return tuple(
        (column.name, deepcopy(getattr(row, column.name))) for column in row.__table__.columns
    )


async def _durable_reasoning_payload(world: AgentWorld) -> dict[str, object]:
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        events = (
            await session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == world.run_id)
                .order_by(RunEvent.seq, RunEvent.id)
            )
        ).all()
        audits = (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.workspace_id == world.workspace_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        ).all()
    return {
        "run": _orm_row_payload(run),
        "events": tuple(_orm_row_payload(row) for row in events),
        "audits": tuple(_orm_row_payload(row) for row in audits),
    }


async def _capture_reason_application_error(world: AgentWorld) -> ApplicationError:
    try:
        await world.reasoning.reason_agent_step_activity(world.params)
    except ApplicationError as error:
        return error
    raise AssertionError("reasoning unexpectedly succeeded")


class _FalseyHandle:
    def __bool__(self) -> bool:
        return False


class _BackendTouchMetrics:
    is_noop = False

    def __init__(self) -> None:
        self.calls = 0

    def counter(self, _name: object) -> Any:
        self.calls += 1
        raise RuntimeError("telemetry backend must not run before schema validation")

    def histogram(self, _name: object) -> Any:
        self.calls += 1
        raise RuntimeError("telemetry backend must not run before schema validation")

    def set_observable(self, _name: object, _observations: object) -> None:
        self.calls += 1
        raise RuntimeError("telemetry backend must not run before schema validation")


class _BackendTouchTracer:
    def __init__(self) -> None:
        self.calls = 0

    def start_as_current_span(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        raise RuntimeError("telemetry backend must not run before schema validation")


def test_all_agent_activity_groups_bind_one_exact_falsey_runtime_graph() -> None:
    metrics = cast(JhinMetrics, _FalseyHandle())
    tracer = cast(Tracer, _FalseyHandle())
    temporal = cast(Any, object())
    resources = cast(
        Any,
        SimpleNamespace(runtime=SimpleNamespace(metrics=metrics, tracer=tracer)),
    )

    reasoning = AgentReasoningActivities(resources)
    projections = AgentProjectionActivities(resources, temporal_client=temporal)
    composite = AgentActivities(resources, temporal_client=temporal)
    compatibility = AgentCompatibilityActivities(resources, temporal)

    for owner in (
        reasoning,
        projections,
        composite,
        compatibility._reasoning,
        compatibility._projections,
    ):
        assert owner._metrics is metrics
        assert owner._tracer is tracer
    assert projections._temporal_client is temporal
    assert composite._temporal_client is temporal
    assert compatibility._projections._temporal_client is temporal


def test_agent_activity_constructors_reject_missing_runtime_handles_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Loop-folded parametrize matrix: same full cross-product, one collected item.
    fallback_calls: list[str] = []

    def forbidden_metrics() -> object:
        fallback_calls.append("metrics")
        raise AssertionError("missing runtime metrics must not install a fallback")

    def forbidden_tracer() -> object:
        fallback_calls.append("tracer")
        raise AssertionError("missing runtime tracer must not install a fallback")

    monkeypatch.setattr(reasoning_module, "noop_metrics", forbidden_metrics)
    monkeypatch.setattr(projections_module, "noop_metrics", forbidden_metrics)
    monkeypatch.setattr(reasoning_module, "noop_tracer", forbidden_tracer, raising=False)
    monkeypatch.setattr(projections_module, "noop_tracer", forbidden_tracer, raising=False)
    for missing in ("runtime", "metrics", "tracer"):
        for owner_kind in ("reasoning", "projection", "composite", "compatibility"):
            case = f"missing={missing}, owner_kind={owner_kind}"
            metrics = cast(JhinMetrics, object())
            tracer = cast(Tracer, object())
            if missing == "runtime":
                resources = SimpleNamespace()
            elif missing == "metrics":
                resources = SimpleNamespace(runtime=SimpleNamespace(tracer=tracer))
            else:
                resources = SimpleNamespace(runtime=SimpleNamespace(metrics=metrics))
            temporal = cast(Any, object())

            caught: BaseException | None = None
            try:
                if owner_kind == "reasoning":
                    AgentReasoningActivities(cast(Any, resources))
                elif owner_kind == "projection":
                    AgentProjectionActivities(cast(Any, resources), temporal_client=temporal)
                elif owner_kind == "composite":
                    AgentActivities(cast(Any, resources), temporal_client=temporal)
                else:
                    AgentCompatibilityActivities(cast(Any, resources), temporal)
            except BaseException as error:
                caught = error

            assert isinstance(caught, AttributeError), case
            assert fallback_calls == [], case


def test_composite_agent_constructor_calls_both_base_initializers_once_with_exact_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = cast(Any, object())
    temporal = cast(Any, object())
    calls: list[tuple[str, object, object | None]] = []

    def reason_init(self: object, received: object) -> None:
        calls.append(("reasoning", received, None))

    def projection_init(
        self: object,
        received: object,
        temporal_client: object | None = None,
    ) -> None:
        calls.append(("projection", received, temporal_client))

    monkeypatch.setattr(AgentReasoningActivities, "__init__", reason_init)
    monkeypatch.setattr(AgentProjectionActivities, "__init__", projection_init)

    AgentActivities(resources, temporal_client=temporal)

    assert calls == [
        ("reasoning", resources, None),
        ("projection", resources, temporal),
    ]


_REASONING_SCHEMA_MUTATIONS: tuple[tuple[str, object], ...] = (
    ("_REASON_SPAN_NAME", "agent.unregistered"),
    ("_REASON_WORKSPACE_ATTRIBUTE", "jhin.unregistered"),
    ("_REASON_TASK_ATTRIBUTE", "jhin.unregistered"),
    ("_REASON_RUN_ATTRIBUTE", "jhin.unregistered"),
    ("_REASON_CORRELATION_ATTRIBUTE", "jhin.unregistered"),
    ("_REASON_OUTCOME_KEY", "jhin.unregistered"),
    ("_REASON_COMPLETED_VALUE", "unregistered_outcome"),
    ("_REASON_FAILED_VALUE", "unregistered_outcome"),
    ("_REASON_CANCELLED_VALUE", "unregistered_outcome"),
    ("_TOKEN_METRIC", "unregistered_metric"),
    ("_COST_METRIC", "unregistered_metric"),
    ("_TOKEN_PROVIDER_LABEL", "unregistered_label"),
    ("_TOKEN_DIRECTION_LABEL", "unregistered_label"),
    ("_TOKEN_INPUT_VALUE", "unregistered_direction"),
    ("_TOKEN_OUTPUT_VALUE", "unregistered_direction"),
    ("_TOKEN_CACHED_VALUE", "unregistered_direction"),
    ("_COST_PROVIDER_LABEL", "unregistered_label"),
    ("_USAGE_VALIDATION_MEASUREMENT", -1),
)


async def test_invalid_reasoning_telemetry_schema_fails_before_every_product_or_backend_touch(
    world: AgentWorld,
) -> None:
    # Loop-folded parametrize matrix: every mutation runs against both entrypoints,
    # one collected item.  Each mutation is applied in its own monkeypatch context so
    # exactly one schema constant is invalid per case.  A single world is safe because
    # every case asserts the activity fails before touching the database, telemetry
    # backends, secrets, or the model factory - state cannot drift between cases.
    # The "activity" pass runs first (no manifest rows); the "legacy" pass then seeds
    # its manifest-only row once, exactly as each fresh parametrized world did.
    for entrypoint in ("activity", "legacy"):
        if entrypoint == "legacy":
            await world.seed_manifest_only()
        for mutation, invalid_value in _REASONING_SCHEMA_MUTATIONS:
            case = f"entrypoint={entrypoint}, mutation={mutation}, invalid_value={invalid_value!r}"
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(reasoning_module, mutation, invalid_value, raising=False)
                backend_metrics = _BackendTouchMetrics()
                backend_tracer = _BackendTouchTracer()
                world.resources.runtime.metrics = cast(JhinMetrics, backend_metrics)
                world.resources.runtime.tracer = cast(Tracer, backend_tracer)
                world.reasoning = _ProbeReasoning(world.resources, world.telemetry.order)
                _ProbeSession.scalar_statements = []
                secret_probe = _SecretStoreProbe("private-prevalidation-secret")
                monkeypatch.setattr(
                    reasoning_module,
                    "SecretStore",
                    lambda *_args, _probe=secret_probe, **_kwargs: _probe,
                )
                snapshot = world.snapshot.model_copy(
                    update={
                        "model_profile": world.snapshot.model_profile.model_copy(
                            update={"secret_id": new_uuid7()}
                        )
                    }
                )
                params = replace(world.params, snapshot_json=snapshot.model_dump_json())
                world.raw_model.responses.append(world.response())

                caught: ValueError | None = None
                try:
                    if entrypoint == "activity":
                        await world.reasoning.reason_agent_step_activity(params)
                    else:
                        await world.reasoning.reason_agent_step(
                            params,
                            legacy_sidecar_repair=True,
                        )
                except ValueError as error:
                    caught = error

                assert caught is not None, case
                assert _ProbeSession.scalar_statements == [], case
                assert secret_probe.reveals == 0, case
                assert world.factory_calls == [], case
                assert world.raw_model.requests == [], case
                assert await world.count_events("agent.step.tool_manifest") == int(
                    entrypoint == "legacy"
                ), case
                assert await world.count_events("agent.step.reasoning") == 0, case
                assert backend_metrics.calls == 0, case
                assert backend_tracer.calls == 0, case
                assert _spans(world.telemetry, "agent.reason_step") == [], case
                assert _spans(world.telemetry, "model.request") == [], case


async def test_fresh_commit_hook_owns_current_reason_span_model_child_and_usage(
    world: AgentWorld,
) -> None:
    world.raw_model.responses.append(world.response())
    hook_span: list[Any] = []

    async def inspect_committed_pair() -> None:
        current = trace.get_current_span()
        assert current.is_recording()
        assert _spans(world.telemetry, "agent.reason_step") == []
        async with world.sessions() as session:
            rows = list(
                await session.scalars(
                    select(RunEvent).where(
                        RunEvent.run_id == world.run_id,
                        RunEvent.event_type.in_(
                            ("agent.step.tool_manifest", "agent.step.reasoning")
                        ),
                    )
                )
            )
        assert {row.event_type for row in rows} == {
            "agent.step.tool_manifest",
            "agent.step.reasoning",
        }
        hook_span.append(current)

    world.reasoning.hook = inspect_committed_pair
    result = await world.reasoning.reason_agent_step_activity(world.params)

    assert result == ReasonAgentStepResult(call_count=0)
    assert len(hook_span) == 1
    reason_spans = _spans(world.telemetry, "agent.reason_step")
    model_spans = _spans(world.telemetry, "model.request")
    assert len(reason_spans) == len(model_spans) == 1
    reason_span = reason_spans[0]
    model_span = model_spans[0]
    assert hook_span[0].get_span_context() == reason_span.context
    assert model_span.parent == reason_span.context
    assert reason_span.end_time is not None
    assert reason_span.attributes["jhin.workspace_id"] == str(world.workspace_id)
    assert reason_span.attributes["jhin.task_id"] == str(world.task_id)
    assert reason_span.attributes["jhin.run_id"] == str(world.run_id)
    assert reason_span.attributes["jhin.correlation_id"] == str(world.correlation_id)
    assert "jhin.agent_id" not in reason_span.attributes
    assert world.factory_calls == [
        (
            ModelProviderType.OLLAMA.value,
            "http://localhost:11434/v1",
            None,
            world.telemetry.metrics,
            world.telemetry.tracer,
        )
    ]
    assert (
        _metric_sum(
            world.telemetry,
            "model_tokens_total",
            provider_type="ollama",
            direction="input",
        )
        == 7
    )
    assert (
        _metric_sum(
            world.telemetry,
            "model_tokens_total",
            provider_type="ollama",
            direction="output",
        )
        == 3
    )
    assert (
        _metric_sum(
            world.telemetry,
            "model_tokens_total",
            provider_type="ollama",
            direction="cached",
        )
        == 1
    )
    assert _metric_sum(
        world.telemetry,
        "model_cost_estimate",
        provider_type="ollama",
    ) == pytest.approx(0.000013)
    order = world.telemetry.order
    hook_index = order.index("after_reasoning_bind_commit")
    barrier_index = order.index("phase9_after_manifest")
    usage_indices = [
        index
        for index, name in enumerate(order)
        if name in {"model_tokens_total", "model_cost_estimate"}
    ]
    assert usage_indices
    assert hook_index < barrier_index < min(usage_indices)


async def test_complete_pair_replay_creates_no_second_span_model_or_usage(
    world: AgentWorld,
) -> None:
    world.raw_model.responses.append(world.response())
    first = await world.reasoning.reason_agent_step_activity(world.params)
    before_points = {
        name: sum(float(point.value) for point in _metric_points(world.telemetry, name))
        for name in ("model_tokens_total", "model_cost_estimate")
    }

    replay = await world.reasoning.reason_agent_step_activity(world.params)

    assert replay == first
    assert len(world.raw_model.requests) == 1
    assert len(world.factory_calls) == 1
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1
    assert len(_spans(world.telemetry, "model.request")) == 1
    assert {
        name: sum(float(point.value) for point in _metric_points(world.telemetry, name))
        for name in ("model_tokens_total", "model_cost_estimate")
    } == before_points


async def test_seeded_complete_pair_replay_precedes_identity_secret_and_span_work(
    world: AgentWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await world.seed_complete_pair()
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        await session.delete(run)
        await session.commit()
    hostile_correlation = _HostileCorrelation()
    _ProbeSession.task_correlation_override = hostile_correlation

    snapshot = world.snapshot.model_copy(
        update={
            "model_profile": world.snapshot.model_profile.model_copy(
                update={"secret_id": new_uuid7()}
            )
        }
    )
    params = replace(world.params, snapshot_json=snapshot.model_dump_json())
    secret_probe = _SecretStoreProbe()
    monkeypatch.setattr(
        reasoning_module,
        "SecretStore",
        lambda *_args, **_kwargs: secret_probe,
    )

    assert await world.reasoning.reason_agent_step_activity(params) == (
        ReasonAgentStepResult(call_count=0)
    )
    assert secret_probe.reveals == 0
    assert world.factory_calls == []
    assert world.raw_model.requests == []
    assert _spans(world.telemetry, "agent.reason_step") == []
    assert _spans(world.telemetry, "model.request") == []
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    assert hostile_correlation.str_calls == hostile_correlation.repr_calls == 0


class _SecretStoreProbe:
    def __init__(self, value: str = "bounded-test-secret") -> None:
        self.reveals = 0
        self.value = value

    async def reveal(self, _workspace_id: UUID, _secret_id: UUID) -> str:
        self.reveals += 1
        return self.value


async def _add_other_workspace(session: AsyncSession) -> Workspace:
    workspace = Workspace(name="Other workspace", slug=f"other-ws-{new_uuid7().hex[:8]}")
    session.add(workspace)
    await session.flush()
    return workspace


async def _add_other_agent(session: AsyncSession, workspace_id: UUID) -> Agent:
    agent = Agent(
        workspace_id=workspace_id,
        name=f"Other agent {new_uuid7().hex[:4]}",
        slug=f"other-agent-{new_uuid7().hex[:8]}",
    )
    session.add(agent)
    await session.flush()
    return agent


async def _add_other_task(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    agent_id: UUID,
) -> Task:
    task = Task(
        workspace_id=workspace_id,
        title="Other task",
        description="Other task",
        assigned_agent_id=agent_id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    return task


async def test_every_reasoning_identity_mismatch_rejects_before_secret_factory_or_span(
    tmp_path: Path,
) -> None:
    # Loop-folded parametrize list: same cases, one collected item.  Each mismatch
    # mutates persisted rows destructively, so every case gets its own fresh world
    # (exactly the isolation the parametrized fixtures provided).
    mismatches = (
        "missing_workspace",
        "missing_task",
        "missing_run",
        "missing_agent",
        "input_workspace",
        "input_task",
        "input_run",
        "input_agent",
        "persisted_run_workspace",
        "persisted_run_task",
        "persisted_run_agent",
        "persisted_task_workspace",
        "persisted_agent_workspace",
        "task_assignment",
        "snapshot_workspace",
        "snapshot_agent",
    )
    for mismatch in mismatches:
        case = f"mismatch={mismatch}"
        with _owned_telemetry() as telemetry, pytest.MonkeyPatch.context() as monkeypatch:
            async with _owned_world(monkeypatch, telemetry, tmp_path) as world:
                await _assert_identity_mismatch_rejects(world, monkeypatch, mismatch, case)


async def _assert_identity_mismatch_rejects(
    world: AgentWorld,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
    case: str,
) -> None:
    params = replace(world.params)
    snapshot = world.snapshot
    async with world.sessions() as session:
        workspace = await session.get(Workspace, world.workspace_id)
        agent = await session.get(Agent, world.agent_id)
        run = await session.get(AgentRun, world.run_id)
        task = await session.get(Task, world.task_id)
        assert workspace is not None and agent is not None and run is not None and task is not None
        if mismatch == "missing_workspace":
            await session.delete(workspace)
        elif mismatch == "missing_task":
            await session.delete(task)
        elif mismatch == "missing_run":
            await session.delete(run)
        elif mismatch == "missing_agent":
            await session.delete(agent)
        elif mismatch == "input_workspace":
            params.workspace_id = str(new_uuid7())
        elif mismatch == "input_task":
            params.task_id = str(
                (
                    await _add_other_task(
                        session,
                        workspace_id=world.workspace_id,
                        agent_id=world.agent_id,
                    )
                ).id
            )
        elif mismatch == "input_run":
            params.run_id = str(new_uuid7())
        elif mismatch == "input_agent":
            params.agent_id = str((await _add_other_agent(session, world.workspace_id)).id)
        elif mismatch == "persisted_run_workspace":
            run.workspace_id = (await _add_other_workspace(session)).id
        elif mismatch == "persisted_run_task":
            run.task_id = (
                await _add_other_task(
                    session,
                    workspace_id=world.workspace_id,
                    agent_id=world.agent_id,
                )
            ).id
        elif mismatch == "persisted_run_agent":
            run.agent_id = (await _add_other_agent(session, world.workspace_id)).id
        elif mismatch == "persisted_task_workspace":
            task.workspace_id = (await _add_other_workspace(session)).id
        elif mismatch == "persisted_agent_workspace":
            agent.workspace_id = (await _add_other_workspace(session)).id
        elif mismatch == "task_assignment":
            task.assigned_agent_id = (await _add_other_agent(session, world.workspace_id)).id
        elif mismatch == "snapshot_workspace":
            snapshot = snapshot.model_copy(update={"workspace_id": new_uuid7()})
        elif mismatch == "snapshot_agent":
            snapshot = snapshot.model_copy(update={"agent_id": new_uuid7()})
        await session.commit()

    secret_id = new_uuid7()
    snapshot = snapshot.model_copy(
        update={"model_profile": snapshot.model_profile.model_copy(update={"secret_id": secret_id})}
    )
    params.snapshot_json = snapshot.model_dump_json()
    secret_probe = _SecretStoreProbe()
    monkeypatch.setattr(
        reasoning_module,
        "SecretStore",
        lambda *_args, **_kwargs: secret_probe,
    )
    world.raw_model.responses.append(world.response())

    with pytest.raises(ApplicationError):
        await world.reasoning.reason_agent_step_activity(params)

    assert secret_probe.reveals == 0, case
    assert world.factory_calls == [], case
    assert world.raw_model.requests == [], case
    assert _spans(world.telemetry, "agent.reason_step") == [], case
    assert _spans(world.telemetry, "model.request") == [], case


class _HostileCorrelation:
    def __init__(self) -> None:
        self.str_calls = 0
        self.repr_calls = 0

    @property
    def __class__(self) -> type[UUID]:
        return UUID

    def __str__(self) -> str:
        self.str_calls += 1
        raise AssertionError("hostile correlation must not be stringified")

    def __repr__(self) -> str:
        self.repr_calls += 1
        raise AssertionError("hostile correlation must not be rendered")


class _UUIDSubclass(UUID):
    pass


@pytest.mark.parametrize("correlation_kind", ["subclass", "spoof"])
async def test_persisted_correlation_requires_exact_builtin_uuid_before_effects(
    world: AgentWorld,
    monkeypatch: pytest.MonkeyPatch,
    correlation_kind: str,
) -> None:
    hostile: object
    if correlation_kind == "subclass":
        hostile = _UUIDSubclass(str(world.correlation_id))
    else:
        hostile = _HostileCorrelation()
    _ProbeSession.task_correlation_override = hostile
    input_substitute = _HostileCorrelation()
    world.params.correlation_id = input_substitute  # type: ignore[attr-defined]
    snapshot_payload = json.loads(world.params.snapshot_json)
    snapshot_payload["correlation_id"] = "snapshot-correlation-substitute-canary"
    snapshot_payload["model_profile"]["secret_id"] = str(new_uuid7())
    world.params.snapshot_json = json.dumps(snapshot_payload)
    secret_probe = _SecretStoreProbe("private-correlation-order-secret")
    monkeypatch.setattr(
        reasoning_module,
        "SecretStore",
        lambda *_args, **_kwargs: secret_probe,
    )
    world.raw_model.responses.append(world.response())

    with pytest.raises(ApplicationError):
        await world.reasoning.reason_agent_step_activity(world.params)

    assert world.factory_calls == []
    assert world.raw_model.requests == []
    assert secret_probe.reveals == 0
    assert _spans(world.telemetry, "agent.reason_step") == []
    assert _spans(world.telemetry, "model.request") == []
    assert input_substitute.str_calls == input_substitute.repr_calls == 0
    if isinstance(hostile, _HostileCorrelation):
        assert hostile.str_calls == hostile.repr_calls == 0


class _HostileAgentTelemetryError(RuntimeError):
    pass


class _AgentSpanProxy:
    def __init__(self, wrapped: Any, phase: str, error: BaseException) -> None:
        self._wrapped = wrapped
        self._phase = phase
        self._error = error
        self.raised_traceback: TracebackType | None = None

    def _raise_owned(self) -> None:
        try:
            raise self._error
        except BaseException as error:
            self.raised_traceback = error.__traceback__
            raise

    def get_span_context(self) -> Any:
        return self._wrapped.get_span_context()

    def is_recording(self) -> bool:
        return self._wrapped.is_recording()

    def set_attribute(self, key: str, value: object) -> None:
        if self._phase == "late_set" and key == "jhin.outcome":
            self._raise_owned()
        if self._phase == "error_attribute" and key.startswith("error."):
            self._raise_owned()
        self._wrapped.set_attribute(key, value)

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def add_event(self, *args: object, **kwargs: object) -> None:
        self._wrapped.add_event(*args, **kwargs)

    def set_status(self, *args: object, **kwargs: object) -> None:
        if self._phase == "error_status":
            self._raise_owned()
        self._wrapped.set_status(*args, **kwargs)

    def update_name(self, name: str) -> None:
        self._wrapped.update_name(name)

    def end(self, end_time: int | None = None) -> None:
        self._wrapped.end(end_time=end_time)
        if self._phase == "end":
            self._raise_owned()

    def record_exception(self, *args: object, **kwargs: object) -> None:
        self._wrapped.record_exception(*args, **kwargs)


class _AgentManagerProxy:
    def __init__(
        self,
        wrapped: Any,
        phase: str,
        error: BaseException,
        span: _AgentSpanProxy,
    ) -> None:
        self._wrapped = wrapped
        self._phase = phase
        self._error = error
        self._span = span
        self.raised_traceback: TracebackType | None = None

    def _raise_owned(self) -> None:
        try:
            raise self._error
        except BaseException as error:
            self.raised_traceback = error.__traceback__
            raise

    def __enter__(self) -> Any:
        if self._phase == "manager_enter":
            self._raise_owned()
        return self._wrapped.__enter__()

    def __exit__(self, *args: object) -> bool:
        result = self._wrapped.__exit__(*args)
        if self._phase == "manager_exit":
            self._raise_owned()
        return bool(result)


class _SelectiveAgentLifecycleTracer:
    def __init__(self, wrapped: Tracer, phase: str, error: BaseException) -> None:
        self._wrapped = wrapped
        self._phase = phase
        self._error = error
        self.agent_calls = 0
        self.raised_traceback: TracebackType | None = None
        self.span: _AgentSpanProxy | None = None
        self.manager: _AgentManagerProxy | None = None

    def _raise_owned(self) -> None:
        try:
            raise self._error
        except BaseException as error:
            self.raised_traceback = error.__traceback__
            raise

    def start_as_current_span(self, *args: object, **kwargs: object) -> Any:
        name = cast(str, args[0] if args else kwargs["name"])
        if name != "agent.reason_step":
            return self._wrapped.start_as_current_span(*args, **kwargs)
        self.agent_calls += 1
        if self._phase == "construction":
            self._raise_owned()
        span = _AgentSpanProxy(
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
        proxy = _AgentManagerProxy(manager, self._phase, self._error, span)
        self.span = span
        self.manager = proxy
        return proxy


_REASON_SPAN_PHASES = (
    "construction",
    "manager_enter",
    "late_set",
    "error_status",
    "error_attribute",
    "manager_exit",
    "end",
    "detach",
)


async def test_hostile_reason_span_lifecycle_preserves_product_db_and_context_authority(
    tmp_path: Path,
) -> None:
    # Loop-folded parametrize matrix: same full cross-product, one collected item.
    # Each case owns a fresh telemetry+database world, matching the old per-item fixtures.
    for diagnostic_kind in ("ordinary", "cancellation"):
        for mode in ("success", "failure", "cancellation"):
            for phase in _REASON_SPAN_PHASES:
                case = f"diagnostic_kind={diagnostic_kind}, mode={mode}, phase={phase}"
                with (
                    _owned_telemetry() as telemetry,
                    pytest.MonkeyPatch.context() as monkeypatch,
                ):
                    async with _owned_world(monkeypatch, telemetry, tmp_path) as world:
                        await _assert_hostile_reason_span_lifecycle(
                            world, monkeypatch, phase, mode, diagnostic_kind, case
                        )


async def _assert_hostile_reason_span_lifecycle(
    world: AgentWorld,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    mode: str,
    diagnostic_kind: str,
    case: str,
) -> None:
    diagnostic: BaseException = _HostileAgentTelemetryError("private-agent-span-diagnostic")
    if diagnostic_kind == "cancellation":
        diagnostic = asyncio.CancelledError("private-agent-span-diagnostic-cancellation")
    tracer = _SelectiveAgentLifecycleTracer(world.telemetry.tracer, phase, diagnostic)
    world.resources.runtime.tracer = cast(Tracer, tracer)
    world.reasoning = _ProbeReasoning(world.resources, world.telemetry.order)
    detach_calls = 0
    if phase == "detach":
        original_detach = otel_context.detach

        def fail_agent_detach(token: object) -> None:
            nonlocal detach_calls
            original_detach(token)
            detach_calls += 1
            if detach_calls == 2:
                try:
                    raise diagnostic
                except BaseException:
                    raise

        monkeypatch.setattr(otel_context, "detach", fail_agent_detach)

    product_error: BaseException | None = None
    if mode == "success":
        world.raw_model.responses.append(world.response())
    elif mode == "failure":
        product_error = ModelProviderError("private-reason-product-failure")
        world.raw_model.generate_error = product_error
    else:
        product_error = asyncio.CancelledError("private-reason-product-cancellation")
        world.raw_model.generate_error = product_error
    entry_context = otel_context.get_current()
    entry_span = trace.get_current_span()

    caught: BaseException | None = None
    result: ReasonAgentStepResult | None = None
    try:
        result = await world.reasoning.reason_agent_step_activity(world.params)
    except BaseException as error:
        caught = error

    if mode == "success":
        assert caught is None, case
        assert result == ReasonAgentStepResult(call_count=0), case
        assert await world.count_events("agent.step.tool_manifest") == 1, case
        assert await world.count_events("agent.step.reasoning") == 1, case
        assert (
            _metric_sum(
                world.telemetry,
                "model_tokens_total",
                provider_type="ollama",
                direction="input",
            )
            == 7
        ), case
    elif mode == "failure":
        assert isinstance(caught, ApplicationError), case
        assert caught.type == "model_provider_error", case
        assert await world.count_events("agent.step.tool_manifest") == 0, case
        assert await world.count_events("agent.step.reasoning") == 0, case
        assert _metric_points(world.telemetry, "model_tokens_total") == [], case
    else:
        assert caught is product_error, case
        assert _traceback_contains(caught.__traceback__, world.raw_model.raised_traceback), case
        assert await world.count_events("agent.step.tool_manifest") == 0, case
        assert await world.count_events("agent.step.reasoning") == 0, case
        assert _metric_points(world.telemetry, "model_tokens_total") == [], case
    assert tracer.agent_calls == 1, case
    assert len(world.raw_model.requests) == 1, case
    assert otel_context.get_current() is entry_context, case
    assert trace.get_current_span() is entry_span, case
    expected_reason_spans = 0 if phase in {"construction", "manager_enter"} else 1
    assert len(_spans(world.telemetry, "agent.reason_step")) == expected_reason_spans, case
    assert len(_spans(world.telemetry, "model.request")) == 1, case


async def test_hostile_reason_span_preserves_exact_application_error_and_durable_payload(
    tmp_path: Path,
) -> None:
    # Loop-folded parametrize matrix: same full cross-product, one collected item.
    # Each case owns a fresh telemetry+database world, matching the old per-item fixtures.
    for diagnostic_kind in ("ordinary", "cancellation"):
        for phase in _REASON_SPAN_PHASES:
            case = f"diagnostic_kind={diagnostic_kind}, phase={phase}"
            with (
                _owned_telemetry() as telemetry,
                pytest.MonkeyPatch.context() as monkeypatch,
            ):
                async with _owned_world(monkeypatch, telemetry, tmp_path) as world:
                    await _assert_hostile_reason_span_preserves_error(
                        world, monkeypatch, phase, diagnostic_kind, case
                    )


async def _assert_hostile_reason_span_preserves_error(
    world: AgentWorld,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    diagnostic_kind: str,
    case: str,
) -> None:
    product_error = ModelProviderError("bounded-control-provider-failure")
    world.raw_model.generate_error = product_error
    before = await _durable_reasoning_payload(world)

    clean = await _capture_reason_application_error(world)
    clean_payload = await _durable_reasoning_payload(world)
    clean_public = _application_error_public(clean)
    clean_traceback = _traceback_frame_names(clean.__traceback__)

    diagnostic: BaseException = _HostileAgentTelemetryError("hostile-reason-control-diagnostic")
    if diagnostic_kind == "cancellation":
        diagnostic = asyncio.CancelledError("hostile-reason-control-cancellation")
    tracer = _SelectiveAgentLifecycleTracer(world.telemetry.tracer, phase, diagnostic)
    world.resources.runtime.tracer = cast(Tracer, tracer)
    world.reasoning = _ProbeReasoning(world.resources, world.telemetry.order)
    detach_calls = 0
    if phase == "detach":
        original_detach = otel_context.detach

        def fail_agent_detach(token: object) -> None:
            nonlocal detach_calls
            original_detach(token)
            detach_calls += 1
            if detach_calls == 2:
                try:
                    raise diagnostic
                except BaseException:
                    raise

        monkeypatch.setattr(otel_context, "detach", fail_agent_detach)

    hostile = await _capture_reason_application_error(world)
    hostile_payload = await _durable_reasoning_payload(world)

    assert clean_payload == before, case
    assert hostile_payload == clean_payload, case
    assert _application_error_public(hostile) == clean_public, case
    assert _traceback_frame_names(hostile.__traceback__) == clean_traceback, case
    assert clean_public == {
        "message": "bounded-control-provider-failure",
        "args": ("model_provider_error: bounded-control-provider-failure",),
        "details": (),
        "type": "model_provider_error",
        "non_retryable": True,
        "next_retry_delay": None,
        "category": clean.category,
        "suppress_context": True,
        "cause_type": None,
        "cause_args": None,
    }, case
    assert len(world.raw_model.requests) == 2, case
    assert len(world.factory_calls) == 2, case


_FATAL_REASON_CASES = [
    (phase, mode)
    for phase, modes in (
        ("construction", ("success", "failure", "cancellation")),
        ("manager_enter", ("success", "failure", "cancellation")),
        ("late_set", ("success", "failure", "cancellation")),
        ("error_status", ("failure",)),
        ("error_attribute", ("failure",)),
        ("manager_exit", ("success", "failure", "cancellation")),
        ("end", ("success", "failure", "cancellation")),
        ("detach", ("success", "failure", "cancellation")),
    )
    for mode in modes
]

_FATAL_REASON_PHASE_FRAMES = {
    "construction": (
        "__enter__",
        "_reason_span",
        "__enter__",
        "safe_span",
        "start_as_current_span",
        "_raise_owned",
    ),
    "manager_enter": (
        "__enter__",
        "_reason_span",
        "__enter__",
        "safe_span",
        "__enter__",
        "_raise_owned",
    ),
    "late_set": (
        "_finish_reason_span",
        "_run_agent_diagnostic",
        "<lambda>",
        "set_span_attributes",
        "set_attribute",
        "_raise_owned",
    ),
    "error_status": (
        "_finish_reason_span",
        "_run_agent_diagnostic",
        "<lambda>",
        "record_span_error",
        "set_status",
        "_raise_owned",
    ),
    "error_attribute": (
        "_finish_reason_span",
        "_run_agent_diagnostic",
        "<lambda>",
        "record_span_error",
        "set_attribute",
        "_raise_owned",
    ),
    "manager_exit": (
        "__exit__",
        "_reason_span",
        "__exit__",
        "safe_span",
        "__exit__",
        "_raise_owned",
    ),
    "end": (
        "__exit__",
        "_reason_span",
        "__exit__",
        "safe_span",
        "__exit__",
        "__exit__",
        "use_span",
        "end",
        "_raise_owned",
    ),
    "detach": (
        "__exit__",
        "_reason_span",
        "__exit__",
        "safe_span",
        "__exit__",
        "__exit__",
        "use_span",
        "fatal_agent_detach",
        "_raise_owned",
    ),
}


async def test_fatal_reason_span_backend_error_propagates_exactly_with_bound_timing(
    tmp_path: Path,
) -> None:
    # Loop-folded parametrize matrix: same full cross-product, one collected item.
    # Each case owns a fresh telemetry+database world.  The reasoning call stays
    # lexically inside this function because the expected traceback frame names
    # begin with this test function's own name.
    for fatal_type in (KeyboardInterrupt, SystemExit):
        for phase, mode in _FATAL_REASON_CASES:
            case = f"fatal_type={fatal_type.__name__}, phase={phase}, mode={mode}"
            with (
                _owned_telemetry() as telemetry,
                pytest.MonkeyPatch.context() as monkeypatch,
            ):
                async with _owned_world(monkeypatch, telemetry, tmp_path) as world:
                    fatal = fatal_type("fatal-agent-span-backend")
                    tracer = _SelectiveAgentLifecycleTracer(world.telemetry.tracer, phase, fatal)
                    world.resources.runtime.tracer = cast(Tracer, tracer)
                    world.reasoning = _ProbeReasoning(world.resources, world.telemetry.order)
                    detach_calls = 0
                    if phase == "detach":
                        original_detach = otel_context.detach

                        def fatal_agent_detach(
                            token: object,
                            original_detach: Callable[[object], None] = original_detach,
                            tracer: _SelectiveAgentLifecycleTracer = tracer,
                        ) -> None:
                            nonlocal detach_calls
                            original_detach(token)
                            detach_calls += 1
                            if detach_calls == 2:
                                tracer._raise_owned()

                        monkeypatch.setattr(otel_context, "detach", fatal_agent_detach)

                    product_error: BaseException | None = None
                    if mode == "success":
                        world.raw_model.responses.append(world.response())
                    elif mode == "failure":
                        product_error = ModelProviderError("active-agent-product-failure")
                        world.raw_model.generate_error = product_error
                    else:
                        product_error = asyncio.CancelledError("active-agent-product-cancellation")
                        world.raw_model.generate_error = product_error
                    entry_context = otel_context.get_current()
                    entry_span = trace.get_current_span()

                    with pytest.raises(fatal_type) as caught:
                        await world.reasoning.reason_agent_step_activity(world.params)

                    assert caught.value is fatal, case
                    raise_sites = [
                        site
                        for site in (
                            tracer.raised_traceback,
                            None if tracer.manager is None else tracer.manager.raised_traceback,
                            None if tracer.span is None else tracer.span.raised_traceback,
                        )
                        if site is not None
                    ]
                    assert len(raise_sites) == 1, case
                    assert _traceback_tail(caught.value.__traceback__) is raise_sites[0], case
                    common_frames = (
                        "test_fatal_reason_span_backend_error_propagates_exactly_with_bound_timing",
                        "reason_agent_step_activity",
                        "reason_agent_step",
                    )
                    assert _traceback_frame_names(caught.value.__traceback__) == (
                        common_frames + _FATAL_REASON_PHASE_FRAMES[phase]
                    ), case
                    assert tracer.agent_calls == 1, case
                    expected_product_calls = 0 if phase in {"construction", "manager_enter"} else 1
                    assert len(world.raw_model.requests) == expected_product_calls, case
                    if expected_product_calls and mode == "success":
                        assert await world.count_events("agent.step.tool_manifest") == 1, case
                        assert await world.count_events("agent.step.reasoning") == 1, case
                    else:
                        assert await world.count_events("agent.step.tool_manifest") == 0, case
                        assert await world.count_events("agent.step.reasoning") == 0, case
                    assert otel_context.get_current() is entry_context, case
                    assert trace.get_current_span() is entry_span, case


class _IntSubclass(int):
    pass


class _HostileIntSubclass(_IntSubclass):
    touches = 0

    def _touch(self) -> None:
        type(self).touches += 1
        raise AssertionError("non-exact persisted integer must not be inspected or divided")

    def __float__(self) -> float:
        self._touch()

    def __truediv__(self, _other: object) -> object:
        self._touch()

    def __rtruediv__(self, _other: object) -> object:
        self._touch()

    def __le__(self, _other: object) -> bool:
        self._touch()

    def __lt__(self, _other: object) -> bool:
        self._touch()

    def __ge__(self, _other: object) -> bool:
        self._touch()

    def __gt__(self, _other: object) -> bool:
        self._touch()


_USAGE_CASES: list[tuple[object, int | None]] = [
    (False, None),
    (-1, None),
    (1.5, None),
    (0, None),
    (_HostileIntSubclass(9), None),
    (9, 9),
    (10**300, 10**300),
    (10**400, None),
]


async def test_each_late_persisted_usage_value_is_independently_bounded_after_commit(
    tmp_path: Path,
) -> None:
    # Loop-folded parametrize matrix: same full cross-product, one collected item.
    # Each case owns a fresh telemetry+database world, matching the old per-item
    # fixtures (each case commits durable rows and records metric points).
    field_directions: tuple[tuple[str, str | None], ...] = (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cached_tokens", "cached"),
        ("cost_micros", None),
    )
    for owner in ("fresh", "legacy"):
        for late_value, expected_target in _USAGE_CASES:
            for field, direction in field_directions:
                case = (
                    f"owner={owner}, field={field}, late_value={late_value!r}"
                    f" ({type(late_value).__name__}), expected_target={expected_target!r}"
                )
                with (
                    _owned_telemetry() as telemetry,
                    pytest.MonkeyPatch.context() as monkeypatch,
                ):
                    async with _owned_world(monkeypatch, telemetry, tmp_path) as world:
                        await _assert_late_usage_value_bounded(
                            world, field, direction, late_value, expected_target, owner, case
                        )


async def _assert_late_usage_value_bounded(
    world: AgentWorld,
    field: str,
    direction: str | None,
    late_value: object,
    expected_target: int | None,
    owner: str,
    case: str,
) -> None:
    if isinstance(late_value, _HostileIntSubclass):
        type(late_value).touches = 0
    counting_metrics = _CountingMetrics(world.telemetry.metrics)
    world.resources.runtime.metrics = cast(JhinMetrics, counting_metrics)
    world.reasoning = _ProbeReasoning(world.resources, world.telemetry.order)
    world.raw_model.responses.append(world.response())
    durable_payloads: list[dict[str, Any]] = []
    if owner == "legacy":
        await world.seed_manifest_only()

    async def mutate_committed_usage() -> None:
        async with world.sessions() as session:
            event = await session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == world.run_id,
                    RunEvent.event_type == "agent.step.reasoning",
                )
            )
            assert event is not None
            payload = deepcopy(event.payload_json)
            durable_payloads.append(payload)

    _ProbeSession.usage_read_override = (field, late_value)
    world.reasoning.hook = mutate_committed_usage
    if owner == "legacy":
        result = await world.reasoning.reason_agent_step(
            world.params,
            legacy_sidecar_repair=True,
        )
    else:
        result = await world.reasoning.reason_agent_step_activity(world.params)

    assert result == ReasonAgentStepResult(call_count=0), case
    assert len(durable_payloads) == 1, case
    durable = await world.load_event("agent.step.reasoning")
    assert durable is not None, case
    assert durable.payload_json == durable_payloads[0], case
    expected_tokens: dict[str, int | None] = {
        "input": 7,
        "output": 3,
        "cached": 1,
    }
    expected_cost: int | None = 13
    if direction is None:
        expected_cost = expected_target
    else:
        expected_tokens[direction] = expected_target
    for token_direction, expected in expected_tokens.items():
        expected_labels = {
            "provider_type": "ollama",
            "direction": token_direction,
        }
        raw_adds = [
            call
            for call in counting_metrics.calls
            if call[0] == "add" and call[1] == "model_tokens_total" and call[3] == expected_labels
        ]
        points = [
            point
            for point in _metric_points(world.telemetry, "model_tokens_total")
            if dict(point.attributes) == expected_labels
        ]
        if expected is None:
            assert raw_adds == [], case
            assert points == [], case
        else:
            assert len(raw_adds) == 1, case
            assert type(raw_adds[0][2]) is int, case
            assert raw_adds[0][2] == expected, case
            assert len(points) == 1, case
            assert type(points[0].value) is float, case
            assert points[0].value == float(expected), case
    raw_cost_adds = [
        call
        for call in counting_metrics.calls
        if call[0] == "add"
        and call[1] == "model_cost_estimate"
        and call[3] == {"provider_type": "ollama"}
    ]
    cost_points = [
        point
        for point in _metric_points(world.telemetry, "model_cost_estimate")
        if dict(point.attributes) == {"provider_type": "ollama"}
    ]
    if expected_cost is None:
        assert raw_cost_adds == [], case
        assert cost_points == [], case
    else:
        expected_cost_value = expected_cost / 1_000_000
        assert len(raw_cost_adds) == 1, case
        assert type(raw_cost_adds[0][2]) is float, case
        assert raw_cost_adds[0][2] == expected_cost_value, case
        assert len(cost_points) == 1, case
        assert type(cost_points[0].value) is float, case
        assert cost_points[0].value == expected_cost_value, case
    expected_token_calls = sum(value is not None for value in expected_tokens.values())
    expected_cost_calls = int(expected_cost is not None)
    assert (
        sum(call[:2] == ("counter", "model_tokens_total") for call in counting_metrics.calls)
        == expected_token_calls
    ), case
    assert (
        sum(call[:2] == ("add", "model_tokens_total") for call in counting_metrics.calls)
        == expected_token_calls
    ), case
    assert (
        sum(call[:2] == ("counter", "model_cost_estimate") for call in counting_metrics.calls)
        == expected_cost_calls
    ), case
    assert (
        sum(call[:2] == ("add", "model_cost_estimate") for call in counting_metrics.calls)
        == expected_cost_calls
    ), case
    if isinstance(late_value, _HostileIntSubclass):
        assert type(late_value).touches == 0, case

    _ProbeSession.usage_read_override = None
    calls_before_replay = list(counting_metrics.calls)
    points_before_replay = {
        name: tuple(_stable_point_payload(point) for point in _metric_points(world.telemetry, name))
        for name in ("model_tokens_total", "model_cost_estimate")
    }

    assert await world.reasoning.reason_agent_step_activity(world.params) == result, case

    replayed = await world.load_event("agent.step.reasoning")
    assert replayed is not None, case
    assert replayed.payload_json == durable_payloads[0], case
    assert counting_metrics.calls == calls_before_replay, case
    assert {
        name: tuple(_stable_point_payload(point) for point in _metric_points(world.telemetry, name))
        for name in ("model_tokens_total", "model_cost_estimate")
    } == points_before_replay, case
    assert len(world.raw_model.requests) == 1, case
    assert len(world.factory_calls) == 1, case


def _assert_positive_finite_helper_shape(source: str) -> None:
    helper_tree = ast.parse(textwrap.dedent(source))
    helper_function = cast(ast.FunctionDef, helper_tree.body[0])
    assert len(helper_function.body) == 5
    first_guard = cast(ast.If, helper_function.body[0])
    assert ast.dump(first_guard.test, include_attributes=False) == ast.dump(
        ast.parse("type(value) is not int or value <= 0", mode="eval").body,
        include_attributes=False,
    )
    assert len(first_guard.body) == 1
    assert ast.unparse(first_guard.body[0]) == "return None"
    assert first_guard.orelse == []
    assert ast.unparse(helper_function.body[1]) == "exact = cast(int, value)"

    conversion = cast(ast.Try, helper_function.body[2])
    assert len(conversion.body) == 1
    assert ast.unparse(conversion.body[0]) == "numeric = float(exact)"
    assert len(conversion.handlers) == 1
    assert ast.unparse(conversion.handlers[0].type) == "(OverflowError, ValueError)"
    assert [ast.unparse(statement) for statement in conversion.handlers[0].body] == ["return None"]
    assert conversion.orelse == conversion.finalbody == []

    finite_guard = cast(ast.If, helper_function.body[3])
    assert ast.unparse(finite_guard.test) == "not math.isfinite(numeric)"
    assert [ast.unparse(statement) for statement in finite_guard.body] == ["return None"]
    assert finite_guard.orelse == []
    assert ast.unparse(helper_function.body[4]) == "return (exact, numeric)"

    body_nodes = [node for statement in helper_function.body for node in ast.walk(statement)]
    assert not any(
        isinstance(node, (ast.BinOp, ast.AugAssign))
        or (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert)))
        for node in body_nodes
    )
    assert sorted(ast.unparse(node.func) for node in body_nodes if isinstance(node, ast.Call)) == [
        "cast",
        "float",
        "math.isfinite",
        "type",
    ]
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr in {"_metrics", "counter", "histogram", "add", "record"}
        for node in body_nodes
    )


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        pytest.param(
            "numeric = float(exact)",
            "numeric = float(exact + 0)",
            id="pre-finite-add",
        ),
        pytest.param(
            "numeric = float(exact)",
            "numeric = float(exact * 1)",
            id="pre-finite-multiply",
        ),
        pytest.param(
            "        numeric = float(exact)",
            "        metrics.counter('model_tokens_total')\n        numeric = float(exact)",
            id="pre-finite-metric-lookup",
        ),
    ],
)
def test_positive_finite_helper_auditor_rejects_prevalidation_effects(
    needle: str,
    replacement: str,
) -> None:
    source = textwrap.dedent(inspect.getsource(reasoning_module._positive_finite_int))
    assert needle in source
    mutant = source.replace(needle, replacement, 1)

    with pytest.raises(AssertionError):
        _assert_positive_finite_helper_shape(mutant)


def test_usage_cost_semantics_validate_exact_finite_bound_before_unit_division() -> None:
    _assert_positive_finite_helper_shape(inspect.getsource(reasoning_module._positive_finite_int))

    owner_tree = ast.parse(
        textwrap.dedent(inspect.getsource(AgentReasoningActivities._record_committed_usage))
    )
    owner_function = cast(ast.AsyncFunctionDef, owner_tree.body[0])
    bounded_assignments = [
        node
        for node in ast.walk(owner_tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_positive_finite_int"
        and ast.dump(node.value.args[0], include_attributes=False)
        == ast.dump(
            ast.parse("usage.get('cost_micros')", mode="eval").body,
            include_attributes=False,
        )
    ]
    divisions = [
        node
        for node in ast.walk(owner_tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    ]
    arithmetic = [node for node in ast.walk(owner_tree) if isinstance(node, ast.BinOp)]
    assert len(bounded_assignments) == len(divisions) == 1
    assert arithmetic == divisions
    bounded_assignment = bounded_assignments[0]
    assert ast.unparse(bounded_assignment.targets[0]) == "bounded_cost"
    cost_guards = [
        node
        for node in ast.walk(owner_tree)
        if isinstance(node, ast.If)
        and ast.dump(node.test, include_attributes=False)
        == ast.dump(
            ast.parse("bounded_cost is not None", mode="eval").body,
            include_attributes=False,
        )
    ]
    assert len(cost_guards) == 1
    cost_guard = cost_guards[0]
    assert cost_guard.orelse == []
    guarded_nodes = {id(node) for statement in cost_guard.body for node in ast.walk(statement)}
    division = divisions[0]
    assert id(division) in guarded_nodes
    assert ast.unparse(division.left) == "numeric_cost"
    assert ast.literal_eval(division.right) == 1_000_000
    numeric_assignments = [
        node
        for statement in cost_guard.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Assign)
        and ast.dump(node.targets[0], include_attributes=False)
        == ast.dump(
            cast(
                ast.Assign,
                ast.parse("(_amount, numeric_cost) = bounded_cost").body[0],
            ).targets[0],
            include_attributes=False,
        )
        and ast.unparse(node.value) == "bounded_cost"
    ]
    assert len(numeric_assignments) == 1
    assert bounded_assignment.lineno < cost_guard.lineno
    assert numeric_assignments[0].lineno < division.lineno

    record_calls = [
        node
        for node in ast.walk(owner_function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_record_usage_counter"
    ]
    assert len(record_calls) == 2
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"counter", "histogram", "add", "record"}
        for node in ast.walk(owner_function)
    )
    token_target = cast(
        ast.For,
        ast.parse("for field, direction in (): pass").body[0],
    ).target
    token_loops = [
        node
        for node in ast.walk(owner_function)
        if isinstance(node, ast.For)
        and ast.dump(node.target, include_attributes=False)
        == ast.dump(token_target, include_attributes=False)
    ]
    assert len(token_loops) == 1
    token_loop = token_loops[0]
    assert len(token_loop.body) == 4
    token_bound = cast(ast.Assign, token_loop.body[0])
    assert ast.unparse(token_bound.targets[0]) == "bounded"
    assert ast.unparse(token_bound.value) == "_positive_finite_int(usage.get(field))"
    token_guard = cast(ast.If, token_loop.body[1])
    assert ast.unparse(token_guard.test) == "bounded is None"
    assert len(token_guard.body) == 1 and isinstance(token_guard.body[0], ast.Continue)
    assert token_guard.orelse == []
    token_unpack = cast(ast.Assign, token_loop.body[2])
    assert ast.unparse(token_unpack.value) == "bounded"
    assert ast.dump(token_unpack.targets[0], include_attributes=False) == ast.dump(
        cast(ast.Assign, ast.parse("(amount, _numeric) = bounded").body[0]).targets[0],
        include_attributes=False,
    )
    token_record = cast(ast.Expr, token_loop.body[3]).value
    assert isinstance(token_record, ast.Call) and token_record in record_calls
    assert (
        ast.unparse(
            next(keyword.value for keyword in token_record.keywords if keyword.arg == "amount")
        )
        == "amount"
    )
    cost_records = [
        call
        for call in record_calls
        if ast.unparse(next(keyword.value for keyword in call.keywords if keyword.arg == "name"))
        == "_COST_METRIC"
    ]
    assert len(cost_records) == 1
    cost_amount = next(
        keyword.value for keyword in cost_records[0].keywords if keyword.arg == "amount"
    )
    assert cost_amount is division
    assert id(cost_records[0]) in guarded_nodes


def test_fresh_and_legacy_usage_owners_share_one_post_barrier_validator() -> None:
    owner_tree = ast.parse(
        textwrap.dedent(inspect.getsource(AgentReasoningActivities._reason_agent_step_core))
    )
    usage_calls = sorted(
        (
            node
            for node in ast.walk(owner_tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "_record_committed_usage"
        ),
        key=lambda node: node.lineno,
    )
    hook_calls = sorted(
        (
            node
            for node in ast.walk(owner_tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "_after_reasoning_bind_commit"
        ),
        key=lambda node: node.lineno,
    )
    barrier_calls = sorted(
        (
            node
            for node in ast.walk(owner_tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "arrive_and_wait"
            and any(
                isinstance(argument, ast.Name) and argument.id == "PHASE9_AFTER_MANIFEST"
                for argument in node.value.args
            )
        ),
        key=lambda node: node.lineno,
    )
    assert len(hook_calls) == len(barrier_calls) == len(usage_calls) == 2
    for hook, barrier, usage in zip(hook_calls, barrier_calls, usage_calls, strict=True):
        assert hook.lineno < barrier.lineno < usage.lineno


@pytest.mark.parametrize(
    ("raw_provider", "expected_provider"),
    [
        pytest.param("openai", "openai", id="openai"),
        pytest.param("anthropic", "anthropic", id="anthropic"),
        pytest.param("openrouter", "openrouter", id="openrouter"),
        pytest.param("ollama", "ollama", id="ollama"),
        pytest.param("openai_compatible", "openai_compatible", id="openai-compatible"),
        pytest.param("OPENAI", "other", id="case-lookalike"),
        pytest.param("openai-private-canary", "other", id="known-prefix"),
        pytest.param(
            "openai_compatible_extra-canary",
            "other",
            id="compatible-prefix",
        ),
        pytest.param("private-regex-safe-provider-canary", "other", id="unknown"),
    ],
)
async def test_committed_usage_provider_label_exact_registry_and_closed_other(
    world: AgentWorld,
    raw_provider: str,
    expected_provider: str,
) -> None:
    snapshot = world.snapshot.model_copy(
        update={
            "model_profile": world.snapshot.model_profile.model_copy(
                update={"provider_type": raw_provider}
            )
        }
    )
    params = replace(world.params, snapshot_json=snapshot.model_dump_json())
    world.raw_model.responses.append(world.response())

    assert await world.reasoning.reason_agent_step_activity(params) == (
        ReasonAgentStepResult(call_count=0)
    )
    assert world.factory_calls[0][0] == raw_provider
    assert (
        _metric_sum(
            world.telemetry,
            "model_tokens_total",
            provider_type=expected_provider,
            direction="input",
        )
        == 7
    )
    assert _metric_sum(
        world.telemetry,
        "model_cost_estimate",
        provider_type=expected_provider,
    ) == pytest.approx(0.000013)
    payload = _complete_export_payload(world.telemetry)
    if expected_provider == "other":
        assert raw_provider not in payload


@pytest.mark.parametrize("phase", ["getter", "write"])
@pytest.mark.parametrize("target", ["model_tokens_total", "model_cost_estimate"])
@pytest.mark.parametrize("diagnostic_kind", ["ordinary", "cancellation"])
async def test_hostile_committed_usage_metric_seams_suppress_only_target_point(
    world: AgentWorld,
    phase: str,
    target: str,
    diagnostic_kind: str,
) -> None:
    diagnostic: BaseException = RuntimeError("private-usage-diagnostic")
    if diagnostic_kind == "cancellation":
        diagnostic = asyncio.CancelledError("private-usage-diagnostic-cancellation")
    hostile = _SelectiveHostileAgentMetrics(
        world.telemetry.metrics,
        target=target,
        phase=phase,
        error=diagnostic,
    )
    world.resources.runtime.metrics = cast(JhinMetrics, hostile)
    world.reasoning = _ProbeReasoning(world.resources, world.telemetry.order)
    world.raw_model.responses.append(world.response())

    result = await world.reasoning.reason_agent_step_activity(world.params)

    assert result == ReasonAgentStepResult(call_count=0)
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert len(world.raw_model.requests) == 1
    assert any(call.startswith(f"{target}:") for call in hostile.calls)
    expected_tokens = 0 if target == "model_tokens_total" else 7 + 3 + 1
    assert (
        sum(float(point.value) for point in _metric_points(world.telemetry, "model_tokens_total"))
        == expected_tokens
    )
    expected_cost = 0 if target == "model_cost_estimate" else 0.000013
    assert sum(
        float(point.value) for point in _metric_points(world.telemetry, "model_cost_estimate")
    ) == pytest.approx(expected_cost)
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1
    assert len(_spans(world.telemetry, "model.request")) == 1


@pytest.mark.parametrize("phase", ["getter", "write"])
@pytest.mark.parametrize("target", ["model_tokens_total", "model_cost_estimate"])
@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
async def test_fatal_committed_usage_metric_error_propagates_after_owner_barrier(
    world: AgentWorld,
    phase: str,
    target: str,
    fatal_type: type[BaseException],
) -> None:
    fatal = fatal_type("fatal-committed-usage-diagnostic")
    hostile = _SelectiveHostileAgentMetrics(
        world.telemetry.metrics,
        target=target,
        phase=phase,
        error=fatal,
    )
    world.resources.runtime.metrics = cast(JhinMetrics, hostile)
    world.reasoning = _ProbeReasoning(world.resources, world.telemetry.order)
    world.raw_model.responses.append(world.response())

    with pytest.raises(fatal_type) as caught:
        await world.reasoning.reason_agent_step_activity(world.params)

    assert caught.value is fatal
    raise_site = hostile.raised_traceback
    if raise_site is None and hostile.instrument is not None:
        raise_site = hostile.instrument.raised_traceback
    assert raise_site is not None
    assert _traceback_tail(caught.value.__traceback__) is raise_site
    expected_frames = (
        "test_fatal_committed_usage_metric_error_propagates_after_owner_barrier",
        "reason_agent_step_activity",
        "reason_agent_step",
        "_reason_agent_step_core",
        "_record_committed_usage",
        "_record_usage_counter",
        "_run_agent_diagnostic",
        "record",
        "counter" if phase == "getter" else "add",
    )
    if phase == "getter":
        expected_frames += ("_raise_owned",)
    assert _traceback_frame_names(caught.value.__traceback__) == expected_frames
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert len(world.raw_model.requests) == 1
    assert "after_reasoning_bind_commit" in world.telemetry.order
    assert "phase9_after_manifest" in world.telemetry.order
    assert world.telemetry.order.index("after_reasoning_bind_commit") < (
        world.telemetry.order.index("phase9_after_manifest")
    )
    expected_tokens = 0 if target == "model_tokens_total" else 7 + 3 + 1
    assert sum(
        float(point.value) for point in _metric_points(world.telemetry, "model_tokens_total")
    ) == pytest.approx(expected_tokens)
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1


@pytest.mark.parametrize("mode", ["success", "failure", "cancellation"])
async def test_complete_agent_export_and_process_sinks_exclude_all_product_material(
    world: AgentWorld,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    caplog.set_level(logging.DEBUG, logger=__name__)
    caplog.set_level(logging.DEBUG, logger="jhin_agent_worker")
    caplog.set_level(logging.DEBUG, logger="jhin_models")
    caplog.clear()
    profile_id = new_uuid7()
    provider_id = new_uuid7()
    secret_id = new_uuid7()
    api_key = "private-agent-api-key-canary"
    snapshot = world.snapshot.model_copy(
        update={
            "name": "private-agent-display-canary",
            "role_title": "private-agent-role-canary",
            "system_prompt": "private-system-prompt-canary",
            "model_profile": world.snapshot.model_profile.model_copy(
                update={
                    "profile_id": profile_id,
                    "provider_id": provider_id,
                    "base_url": "https://private-base-url-canary.invalid/v1",
                    "secret_id": secret_id,
                    "model_name": "private-profile-model-canary",
                    "display_name": "private-profile-display-canary",
                }
            ),
        }
    )
    params = replace(
        world.params,
        snapshot_json=snapshot.model_dump_json(),
        instruction="private-instruction-canary",
        user_instructions=["private-user-instruction-canary"],
        advertised_tools=[
            AdvertisedTool(
                name="private-tool-name-canary",
                description="private-tool-description-canary",
                parameters={"private-schema-key-canary": {"type": "string"}},
            )
        ],
    )
    async with world.sessions() as session:
        task = await session.get(Task, world.task_id)
        assert task is not None
        task.title = "private-task-title-canary"
        task.description = "private-task-description-canary"
        task.metadata_json = {"private-metadata-canary": "private-metadata-value-canary"}
        await session.commit()

    secret_probe = _SecretStoreProbe(api_key)
    monkeypatch.setattr(
        reasoning_module,
        "SecretStore",
        lambda *_args, **_kwargs: secret_probe,
    )
    product_error: BaseException | None = None
    if mode == "success":
        world.raw_model.responses.append(
            world.response(
                text="private-completion-canary",
                finish_reason="private-finish-reason-canary",
                model="private-response-model-canary",
                provider_request_id="private-provider-request-canary",
                tool_calls=(
                    ModelToolCall(
                        id="private-provider-call-canary",
                        name="private-tool-name-canary",
                        arguments_json=('{"payload":"private-tool-arguments-canary"}'),
                    ),
                ),
            )
        )
        assert await world.reasoning.reason_agent_step_activity(params) == (
            ReasonAgentStepResult(call_count=1)
        )
        assert await world.reasoning.reason_agent_step_activity(params) == (
            ReasonAgentStepResult(call_count=1)
        )
    elif mode == "failure":
        product_error = ModelProviderError("private-provider-body-error-canary")
        world.raw_model.generate_error = product_error
        with pytest.raises(ApplicationError) as caught:
            await world.reasoning.reason_agent_step_activity(params)
        assert caught.value.type == "model_provider_error"
        assert "private-provider-body-error-canary" in str(caught.value)
    else:
        product_error = asyncio.CancelledError("private-product-cancellation-canary")
        world.raw_model.generate_error = product_error
        with pytest.raises(asyncio.CancelledError) as caught:
            await world.reasoning.reason_agent_step_activity(params)
        assert caught.value is product_error
        assert _traceback_contains(caught.value.__traceback__, world.raw_model.raised_traceback)

    assert secret_probe.reveals == 1
    reason_spans = _spans(world.telemetry, "agent.reason_step")
    model_spans = _spans(world.telemetry, "model.request")
    assert len(reason_spans) == len(model_spans) == 1
    reason_span = reason_spans[0]
    assert reason_span.kind.name == "INTERNAL"
    assert model_spans[0].parent == reason_span.context
    expected_outcome = {
        "success": "completed",
        "failure": "failed",
        "cancellation": "cancelled",
    }[mode]
    assert reason_span.attributes["jhin.outcome"] == expected_outcome
    assert reason_span.status.description is None
    assert set(reason_span.attributes) <= {
        "jhin.workspace_id",
        "jhin.task_id",
        "jhin.run_id",
        "jhin.correlation_id",
        "jhin.outcome",
        "error.type",
        "error.code",
    }

    print("bounded-agent-stdout")
    logging.getLogger(__name__).debug(
        "bounded-agent-log",
        extra={"bounded_structured_agent_field": "bounded-agent-value"},
    )
    captured = capsys.readouterr()
    assert any(
        record.__dict__.get("bounded_structured_agent_field") == "bounded-agent-value"
        for record in caplog.records
    )
    structured_records = json.dumps(
        [record.__dict__ for record in caplog.records],
        sort_keys=True,
        default=str,
    )
    export_payload = _complete_export_payload(world.telemetry)
    payload = "\n".join(
        (
            export_payload,
            caplog.text,
            structured_records,
            captured.out,
            captured.err,
        )
    )
    canaries = {
        "private-agent-display-canary",
        "private-agent-role-canary",
        "private-system-prompt-canary",
        "private-profile-model-canary",
        "private-profile-display-canary",
        "private-instruction-canary",
        "private-user-instruction-canary",
        "private-task-title-canary",
        "private-task-description-canary",
        "private-metadata-canary",
        "private-metadata-value-canary",
        "private-tool-name-canary",
        "private-tool-description-canary",
        "private-schema-key-canary",
        "private-tool-arguments-canary",
        "private-completion-canary",
        "private-finish-reason-canary",
        "private-response-model-canary",
        "private-provider-request-canary",
        "private-provider-call-canary",
        "private-provider-body-error-canary",
        "private-product-cancellation-canary",
        "private-base-url-canary",
        api_key,
        str(profile_id),
        str(provider_id),
        str(secret_id),
        str(world.agent_id),
    }
    for canary in canaries:
        assert canary not in payload

    export_document = cast(dict[str, Any], json.loads(export_payload))
    exported_spans = cast(list[dict[str, Any]], export_document["spans"])
    serialized_metrics = json.dumps(export_document["metrics"], sort_keys=True)
    serialized_model_spans = json.dumps(
        [span for span in exported_spans if span["name"] == "model.request"],
        sort_keys=True,
    )
    process_and_logs = "\n".join((caplog.text, structured_records, captured.out, captured.err))
    authorized_ids = {
        "jhin.workspace_id": str(world.workspace_id),
        "jhin.task_id": str(world.task_id),
        "jhin.run_id": str(world.run_id),
        "jhin.correlation_id": str(world.correlation_id),
    }
    serialized_reason_spans = [
        span for span in exported_spans if span["name"] == "agent.reason_step"
    ]
    assert len(serialized_reason_spans) == 1
    reason_attributes = serialized_reason_spans[0]["attributes"]
    assert {key: reason_attributes[key] for key in authorized_ids} == authorized_ids
    reason_remainder = deepcopy(serialized_reason_spans[0])
    remainder_attributes = cast(dict[str, object], reason_remainder["attributes"])
    for key in authorized_ids:
        assert remainder_attributes.pop(key) == authorized_ids[key]
    serialized_reason_remainder = json.dumps(reason_remainder, sort_keys=True, default=str)
    for identifier in authorized_ids.values():
        assert identifier not in serialized_reason_remainder
        assert identifier not in serialized_metrics
        assert identifier not in serialized_model_spans
        assert identifier not in process_and_logs
    assert str(world.agent_id) not in export_payload
    assert str(world.agent_id) not in process_and_logs


async def test_reasoning_commit_failure_rolls_back_pair_and_emits_no_usage(
    world: AgentWorld,
) -> None:
    failure = RuntimeError("reasoning-commit-authority")
    _ProbeSession.fail_next_commit = failure
    world.raw_model.responses.append(world.response())

    with pytest.raises(RuntimeError) as caught:
        await world.reasoning.reason_agent_step_activity(world.params)

    assert caught.value is failure
    assert await world.count_events("agent.step.tool_manifest") == 0
    assert await world.count_events("agent.step.reasoning") == 0
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    assert len(world.raw_model.requests) == 1
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1
    assert len(_spans(world.telemetry, "model.request")) == 1


async def test_post_commit_barrier_crash_keeps_pair_but_loses_usage_at_most_once(
    world: AgentWorld,
) -> None:
    crash = RuntimeError("phase9-post-commit-crash")
    world.resources.test_barrier.failure = crash
    world.raw_model.responses.append(world.response())

    with pytest.raises(RuntimeError) as caught:
        await world.reasoning.reason_agent_step_activity(world.params)

    assert caught.value is crash
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    world.resources.test_barrier.failure = None

    assert await world.reasoning.reason_agent_step_activity(world.params) == (
        ReasonAgentStepResult(call_count=0)
    )
    assert len(world.raw_model.requests) == 1
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []


async def test_legacy_sidecar_repair_is_a_fresh_usage_owner(world: AgentWorld) -> None:
    await world.seed_manifest_only()
    world.telemetry.order.clear()
    _ProbeSession.commit_order = world.telemetry.order
    world.raw_model.responses.append(world.response())
    hook_spans: list[Any] = []

    async def inspect_sidecar_commit() -> None:
        current = trace.get_current_span()
        assert current.is_recording()
        assert _spans(world.telemetry, "agent.reason_step") == []
        assert await world.count_events("agent.step.tool_manifest") == 1
        assert await world.count_events("agent.step.reasoning") == 1
        hook_spans.append(current)

    world.reasoning.hook = inspect_sidecar_commit

    result = await world.reasoning.reason_agent_step(
        world.params,
        legacy_sidecar_repair=True,
    )

    assert result == ReasonAgentStepResult(call_count=0)
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert len(hook_spans) == 1
    reason_spans = _spans(world.telemetry, "agent.reason_step")
    model_spans = _spans(world.telemetry, "model.request")
    assert len(reason_spans) == len(model_spans) == 1
    assert hook_spans[0].get_span_context() == reason_spans[0].context
    assert model_spans[0].parent == reason_spans[0].context
    assert (
        _metric_sum(
            world.telemetry,
            "model_tokens_total",
            provider_type="ollama",
            direction="input",
        )
        == 7
    )
    assert _metric_sum(
        world.telemetry,
        "model_cost_estimate",
        provider_type="ollama",
    ) == pytest.approx(0.000013)
    order = world.telemetry.order
    assert order.count("db_commit") == 1
    commit_index = order.index("db_commit")
    hook_index = order.index("after_reasoning_bind_commit")
    barrier_index = order.index("phase9_after_manifest")
    usage_indices = [
        index
        for index, name in enumerate(order)
        if name in {"model_tokens_total", "model_cost_estimate"}
    ]
    assert usage_indices
    assert commit_index < hook_index < barrier_index < min(usage_indices)


async def test_legacy_sidecar_commit_failure_keeps_manifest_without_usage(
    world: AgentWorld,
) -> None:
    await world.seed_manifest_only()
    failure = RuntimeError("legacy-sidecar-commit-authority")
    _ProbeSession.fail_next_commit = failure
    world.raw_model.responses.append(world.response())

    with pytest.raises(RuntimeError) as caught:
        await world.reasoning.reason_agent_step(
            world.params,
            legacy_sidecar_repair=True,
        )

    assert caught.value is failure
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 0
    assert len(world.raw_model.requests) == 1
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1
    assert len(_spans(world.telemetry, "model.request")) == 1


async def test_legacy_sidecar_barrier_crash_replay_never_duplicates_diagnostics(
    world: AgentWorld,
) -> None:
    await world.seed_manifest_only()
    crash = RuntimeError("legacy-sidecar-postcommit-barrier-crash")
    world.resources.test_barrier.failure = crash
    world.raw_model.responses.append(world.response())

    with pytest.raises(RuntimeError) as caught:
        await world.reasoning.reason_agent_step(
            world.params,
            legacy_sidecar_repair=True,
        )

    assert caught.value is crash
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert len(world.raw_model.requests) == 1
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1
    assert len(_spans(world.telemetry, "model.request")) == 1
    world.resources.test_barrier.failure = None

    assert await world.reasoning.reason_agent_step(
        world.params,
        legacy_sidecar_repair=True,
    ) == ReasonAgentStepResult(call_count=0)
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert len(world.raw_model.requests) == 1
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1


async def test_concurrent_complete_pair_winner_suppresses_loser_usage(
    world: AgentWorld,
) -> None:
    world.raw_model.responses.append(world.response())
    installed = False

    async def install_winner(name: str, _identity: UUID) -> None:
        nonlocal installed
        if name == AGENT_BEFORE_BIND and not installed:
            installed = True
            await world.seed_complete_pair()

    world.resources.test_barrier.callback = install_winner

    result = await world.reasoning.reason_agent_step_activity(world.params)

    assert result == ReasonAgentStepResult(call_count=0)
    assert installed
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert len(world.raw_model.requests) == 1
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1


async def test_non_lossless_manifest_failure_commits_product_state_without_usage(
    world: AgentWorld,
) -> None:
    world.raw_model.responses.append(
        world.response(
            tool_calls=(
                # A tool name past the provider-text cap cannot be stored
                # losslessly (malformed arguments, by contrast, bind as a
                # retryable placeholder and no longer fail the step).
                ModelToolCall(
                    id="private-provider-call",
                    name="private-tool-name-" + "n" * 200,
                    arguments_json='{"value": 1}',
                ),
            )
        )
    )

    with pytest.raises(ApplicationError) as caught:
        await world.reasoning.reason_agent_step_activity(world.params)

    assert caught.value.type == "tool_step_manifest_not_lossless"
    run = await world.load_run()
    assert run.status == RunStatus.FAILED.value
    assert run.error_code == "tool_step_manifest_not_lossless"
    assert await world.count_audits("agent.step.manifest_not_lossless") == 1
    assert await world.count_events("agent.step.tool_manifest") == 0
    assert await world.count_events("agent.step.reasoning") == 0
    assert _metric_points(world.telemetry, "model_tokens_total") == []
    assert _metric_points(world.telemetry, "model_cost_estimate") == []
    assert len(_spans(world.telemetry, "agent.reason_step")) == 1


def _finalize_params(
    world: AgentWorld,
    *,
    status: str = RunStatus.COMPLETED.value,
    run_id: str | object | None = _UNSET,
    error_code: str | None = None,
) -> FinalizeInput:
    return FinalizeInput(
        workspace_id=str(world.workspace_id),
        task_id=str(world.task_id),
        run_id=str(world.run_id) if run_id is _UNSET else cast(str | None, run_id),
        status=status,
        steps_used=2,
        error_code=error_code,
        error_message="bounded terminal detail" if error_code is not None else None,
    )


async def _finalization_product_state(world: AgentWorld) -> dict[str, object]:
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        task = await session.get(Task, world.task_id)
        assert run is not None and task is not None
        events = list(
            await session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == world.run_id)
                .order_by(RunEvent.seq, RunEvent.id)
            )
        )
        messages = list(
            await session.scalars(
                select(Message)
                .where(Message.run_id == world.run_id)
                .order_by(Message.created_at, Message.id)
            )
        )
        return deepcopy(
            {
                "run": {
                    field: getattr(run, field)
                    for field in (
                        "id",
                        "workspace_id",
                        "agent_id",
                        "task_id",
                        "parent_run_id",
                        "status",
                        "reason",
                        "model_profile_id",
                        "snapshot_hash",
                        "started_at",
                        "completed_at",
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "estimated_cost_micros",
                        "steps_used",
                        "temporal_workflow_id",
                        "temporal_run_id",
                        "langgraph_thread_id",
                        "error_code",
                        "error_message",
                    )
                },
                "task": {
                    field: getattr(task, field)
                    for field in (
                        "id",
                        "workspace_id",
                        "external_source",
                        "external_id",
                        "title",
                        "description",
                        "state",
                        "priority",
                        "assigned_agent_id",
                        "assigned_team_id",
                        "parent_task_id",
                        "trigger_id",
                        "temporal_workflow_id",
                        "correlation_id",
                        "metadata_json",
                    )
                },
                "events": [
                    {
                        "workspace_id": event.workspace_id,
                        "run_id": event.run_id,
                        "task_id": event.task_id,
                        "seq": event.seq,
                        "event_type": event.event_type,
                        "payload_json": event.payload_json,
                    }
                    for event in events
                ],
                "messages": [
                    {
                        "workspace_id": message.workspace_id,
                        "task_id": message.task_id,
                        "run_id": message.run_id,
                        "sender_type": message.sender_type,
                        "sender_id": message.sender_id,
                        "recipient_type": message.recipient_type,
                        "recipient_id": message.recipient_id,
                        "message_type": message.message_type,
                        "content_json": message.content_json,
                        "visibility": message.visibility,
                    }
                    for message in messages
                ],
            }
        )


async def _restore_pre_finalization_state(
    world: AgentWorld,
    initial: Mapping[str, object],
) -> None:
    initial_run = cast(Mapping[str, object], initial["run"])
    initial_task = cast(Mapping[str, object], initial["task"])
    async with world.sessions() as session:
        await session.execute(delete(Message).where(Message.run_id == world.run_id))
        await session.execute(delete(RunEvent).where(RunEvent.run_id == world.run_id))
        run = await session.get(AgentRun, world.run_id)
        task = await session.get(Task, world.task_id)
        assert run is not None and task is not None
        for field, value in initial_run.items():
            setattr(run, field, deepcopy(value))
        task.state = cast(str, initial_task["state"])
        await session.commit()


def _publisher_product_payload(world: AgentWorld) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (event.workspace_id, event.event_type, deepcopy(event.data))
        for event in world.resources.publisher.events
    ]


def _counter_point_map(telemetry: _Telemetry, name: str) -> dict[tuple[tuple[str, str], ...], Any]:
    points = _metric_points(telemetry, name)
    mapped = {
        tuple(sorted(cast(Mapping[str, str], point.attributes).items())): point.value
        for point in points
    }
    assert len(mapped) == len(points)
    return mapped


def _histogram_point_map(
    telemetry: _Telemetry,
    name: str,
) -> dict[tuple[tuple[str, str], ...], tuple[int, float]]:
    points = _metric_points(telemetry, name)
    mapped = {
        tuple(sorted(cast(Mapping[str, str], point.attributes).items())): (
            point.count,
            point.sum,
        )
        for point in points
    }
    assert len(mapped) == len(points)
    return mapped


_FINALIZATION_SCHEMA_MUTATIONS: tuple[tuple[str, object], ...] = (
    ("_AGENT_RUNS_METRIC", "unregistered_metric"),
    ("_AGENT_DURATION_METRIC", "unregistered_metric"),
    ("_AGENT_FAILURES_METRIC", "unregistered_metric"),
    ("_AGENT_SERVICE_LABEL", "unregistered_label"),
    ("_AGENT_OUTCOME_LABEL", "unregistered_label"),
    ("_AGENT_FAILURE_LABEL", "unregistered_label"),
    ("_AGENT_SERVICE_VALUE", "unregistered_service"),
    ("_AGENT_COMPLETED_VALUE", "unregistered_outcome"),
    ("_AGENT_FAILED_VALUE", "unregistered_outcome"),
    ("_AGENT_CANCELLED_VALUE", "unregistered_outcome"),
    ("_AGENT_EXECUTION_UNKNOWN_VALUE", "unregistered_failure"),
    ("_AGENT_BUDGET_VALUE", "unregistered_failure"),
    ("_AGENT_INTERNAL_VALUE", "unregistered_failure"),
    ("_FINALIZATION_VALIDATION_MEASUREMENT", -1),
)


async def test_invalid_finalization_metric_schema_fails_before_db_product_or_backend_touch(
    world: AgentWorld,
) -> None:
    # Loop-folded parametrize matrix: every mutation runs against both run_id kinds,
    # one collected item.  Each mutation is applied in its own monkeypatch context so
    # exactly one schema constant is invalid per case.  A single world is safe because
    # every case asserts finalization fails before touching the database, publisher,
    # or metric backend - state cannot drift between cases.
    for run_id_kind in ("present", "none"):
        for mutation, invalid_value in _FINALIZATION_SCHEMA_MUTATIONS:
            case = (
                f"run_id_kind={run_id_kind}, mutation={mutation}, invalid_value={invalid_value!r}"
            )
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(projections_module, mutation, invalid_value, raising=False)
                backend_metrics = _BackendTouchMetrics()
                world.resources.runtime.metrics = cast(JhinMetrics, backend_metrics)
                world.projections = AgentProjectionActivities(cast(Any, world.resources))
                _ProbeSession.scalar_statements = []

                caught: ValueError | None = None
                try:
                    await world.projections.finalize_run_projection_activity(
                        _finalize_params(
                            world,
                            run_id=None if run_id_kind == "none" else _UNSET,
                        )
                    )
                except ValueError as error:
                    caught = error

                assert caught is not None, case
                assert _ProbeSession.scalar_statements == [], case
                run = await world.load_run()
                assert run.status == RunStatus.RUNNING.value, case
                assert run.completed_at is None, case
                assert await world.count_events("run.completed") == 0, case
                assert world.resources.publisher.events == [], case
                assert backend_metrics.calls == 0, case


@pytest.mark.parametrize("start_zone", [UTC, timezone(timedelta(hours=5, minutes=30))])
@pytest.mark.parametrize(
    "elapsed",
    [timedelta(seconds=5), timedelta(days=93, hours=7, minutes=11, seconds=13)],
)
async def test_finalize_owner_commits_once_before_run_counter_and_exact_duration(
    world: AgentWorld,
    start_zone: timezone,
    elapsed: timedelta,
) -> None:
    world.telemetry.order.clear()
    _ProbeSession.commit_order = world.telemetry.order
    durable_started_at = (await world.load_run()).started_at
    started_at = (datetime.now(UTC) - elapsed).astimezone(start_zone)
    _ProbeSession.run_started_override = started_at
    params = _finalize_params(world)

    await world.projections.finalize_run_projection_activity(params)
    first = await world.load_run()
    first_completed_at = first.completed_at
    committed_completed_at = _ProbeSession.committed_completed_at
    assert committed_completed_at is not None
    await world.projections.finalize_run_projection_activity(params)

    run = await world.load_run()
    assert run.status == RunStatus.COMPLETED.value
    assert run.completed_at is first_completed_at or run.completed_at == first_completed_at
    assert first_completed_at is not None
    assert await world.count_events("run.completed") == 1
    assert (
        _metric_sum(
            world.telemetry,
            "agent_runs_total",
            service="agent-worker",
            outcome="completed",
        )
        == 1
    )
    duration_points = _histogram_points(
        world.telemetry,
        "agent_run_duration_seconds",
        outcome="completed",
    )
    assert len(duration_points) == 1
    assert duration_points[0].count == 1
    expected_duration = (committed_completed_at - started_at).total_seconds()
    assert type(duration_points[0].sum) is float
    assert duration_points[0].sum == expected_duration
    assert (await world.load_run()).started_at == durable_started_at
    assert _metric_points(world.telemetry, "agent_run_failures_total") == []
    assert world.telemetry.order.count("db_commit") == 1
    commit_index = world.telemetry.order.index("db_commit")
    assert commit_index < world.telemetry.order.index("agent_runs_total")
    assert commit_index < world.telemetry.order.index("agent_run_duration_seconds")
    assert [event.event_type for event in world.resources.publisher.events] == [
        "agent.run.completed",
        "task.completed",
    ]


@pytest.mark.parametrize(
    ("status", "error_code", "failure_class"),
    [
        (RunStatus.COMPLETED.value, None, None),
        (RunStatus.CANCELLED.value, None, None),
        (RunStatus.FAILED.value, "tool_execution_unknown", "execution_unknown"),
        (RunStatus.FAILED.value, "max_steps_exceeded", "budget"),
        (RunStatus.FAILED.value, "provider_failed", "internal"),
    ],
)
async def test_finalize_status_and_failure_class_mapping_is_closed(
    world: AgentWorld,
    status: str,
    error_code: str | None,
    failure_class: str | None,
) -> None:
    await world.projections.finalize_run_projection_activity(
        _finalize_params(world, status=status, error_code=error_code)
    )

    assert (
        _metric_sum(
            world.telemetry,
            "agent_runs_total",
            service="agent-worker",
            outcome=status,
        )
        == 1
    )
    failure_points = _metric_points(world.telemetry, "agent_run_failures_total")
    if failure_class is None:
        assert failure_points == []
    else:
        assert (
            _metric_sum(
                world.telemetry,
                "agent_run_failures_total",
                failure_class=failure_class,
            )
            == 1
        )
        assert len(failure_points) == 1


async def test_complete_finalization_export_and_process_sinks_exclude_product_material(
    world: AgentWorld,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG, logger=__name__)
    caplog.set_level(logging.DEBUG, logger="jhin_agent_worker")
    caplog.clear()
    _ProbeSession.run_started_override = datetime.now(UTC) - timedelta(seconds=5)
    error_code = "private-final-error-code-canary"
    error_message = "private-final-error-message-canary"
    params = _finalize_params(
        world,
        status=RunStatus.FAILED.value,
        error_code=error_code,
    )
    params.error_message = error_message

    await world.projections.finalize_run_projection_activity(params)

    print("bounded-finalization-stdout")
    logging.getLogger(__name__).debug(
        "bounded-finalization-log",
        extra={"bounded_structured_finalization_field": "bounded-finalization-value"},
    )
    captured = capsys.readouterr()
    assert any(
        record.__dict__.get("bounded_structured_finalization_field") == "bounded-finalization-value"
        for record in caplog.records
    )
    structured_records = json.dumps(
        [record.__dict__ for record in caplog.records],
        sort_keys=True,
        default=str,
    )
    export_payload = _complete_export_payload(world.telemetry)
    process_and_logs = "\n".join((caplog.text, structured_records, captured.out, captured.err))
    for canary in (
        error_code,
        error_message,
        str(world.workspace_id),
        str(world.task_id),
        str(world.run_id),
        str(world.agent_id),
    ):
        assert canary not in export_payload
        assert canary not in process_and_logs
    assert world.telemetry.exporter.get_finished_spans() == ()
    run_points = _metric_points(world.telemetry, "agent_runs_total")
    duration_points = _metric_points(world.telemetry, "agent_run_duration_seconds")
    failure_points = _metric_points(world.telemetry, "agent_run_failures_total")
    assert [dict(point.attributes) for point in run_points] == [
        {"service": "agent-worker", "outcome": "failed"}
    ]
    assert [dict(point.attributes) for point in duration_points] == [{"outcome": "failed"}]
    assert [dict(point.attributes) for point in failure_points] == [{"failure_class": "internal"}]


async def test_prefailed_uncompleted_execution_unknown_run_still_owns_final_metrics(
    world: AgentWorld,
) -> None:
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        run.status = RunStatus.FAILED.value
        run.error_code = "tool_execution_unknown"
        run.error_message = "bounded preserved execution unknown"
        run.completed_at = None
        await session.commit()

    await world.projections.finalize_run_projection_activity(
        _finalize_params(
            world,
            status=RunStatus.FAILED.value,
            error_code="different_error",
        )
    )

    run = await world.load_run()
    assert run.completed_at is not None
    assert run.error_code == "tool_execution_unknown"
    assert (
        _metric_sum(
            world.telemetry,
            "agent_runs_total",
            service="agent-worker",
            outcome="failed",
        )
        == 1
    )
    assert (
        _metric_sum(
            world.telemetry,
            "agent_run_failures_total",
            failure_class="execution_unknown",
        )
        == 1
    )


class _DatetimeSubclass(datetime):
    pass


class _HostileStartedAt:
    def __init__(self) -> None:
        self.touches = 0

    @property
    def __class__(self) -> type[datetime]:
        return datetime

    @property
    def tzinfo(self) -> object:
        self.touches += 1
        raise AssertionError("hostile persisted start must not be inspected")

    def __sub__(self, _other: object) -> object:
        self.touches += 1
        raise AssertionError("hostile persisted start must not be subtracted")

    def __str__(self) -> str:
        self.touches += 1
        raise AssertionError("hostile persisted start must not be rendered")

    def __repr__(self) -> str:
        self.touches += 1
        raise AssertionError("hostile persisted start must not be rendered")


class _HostileTimezone(tzinfo):
    def __init__(self, error: BaseException, *, fail_on_call: int) -> None:
        self.error = error
        self.fail_on_call = fail_on_call
        self.utcoffset_calls = 0
        self.raised_traceback: TracebackType | None = None

    def utcoffset(self, _value: datetime | None) -> timedelta:
        self.utcoffset_calls += 1
        if self.utcoffset_calls == self.fail_on_call:
            try:
                raise self.error
            except BaseException as error:
                self.raised_traceback = error.__traceback__
                raise
        return timedelta(0)

    def dst(self, _value: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str:
        return "bounded-hostile-zone"


def _without_completion_timestamp(state: Mapping[str, object]) -> dict[str, object]:
    normalized = deepcopy(dict(state))
    run = cast(dict[str, object], normalized["run"])
    assert type(run["completed_at"]) is datetime
    run["completed_at"] = "owned-completion-timestamp"
    return normalized


async def _clean_failed_finalization_control(
    world: AgentWorld,
    *,
    started_at: datetime,
) -> tuple[dict[str, object], list[tuple[str, str, dict[str, Any]]]]:
    _ProbeSession.run_started_override = started_at
    await world.projections.finalize_run_projection_activity(
        _finalize_params(
            world,
            status=RunStatus.FAILED.value,
            error_code="provider_failed",
        )
    )
    return await _finalization_product_state(world), _publisher_product_payload(world)


@pytest.mark.parametrize("fail_on_call", [1, 2], ids=["utcoffset", "subtraction"])
@pytest.mark.parametrize(
    "diagnostic",
    [
        RuntimeError("hostile-duration-timezone"),
        asyncio.CancelledError("hostile-duration-cancellation"),
    ],
    ids=["ordinary", "cancellation"],
)
async def test_hostile_exact_datetime_timezone_suppresses_only_duration(
    world: AgentWorld,
    fail_on_call: int,
    diagnostic: BaseException,
) -> None:
    initial_state = await _finalization_product_state(world)
    wall_time = datetime.now(UTC) - timedelta(days=17, seconds=5)
    clean_started_at = wall_time.replace(tzinfo=UTC)
    clean_state, clean_published = await _clean_failed_finalization_control(
        world,
        started_at=clean_started_at,
    )
    run_labels = (("outcome", "failed"), ("service", "agent-worker"))
    duration_labels = (("outcome", "failed"),)
    failure_labels = (("failure_class", "internal"),)
    clean_runs = _counter_point_map(world.telemetry, "agent_runs_total")
    clean_durations = _histogram_point_map(world.telemetry, "agent_run_duration_seconds")
    clean_failures = _counter_point_map(world.telemetry, "agent_run_failures_total")
    assert clean_runs == {run_labels: 1}
    assert set(clean_durations) == {duration_labels}
    assert clean_durations[duration_labels][0] == 1
    assert clean_failures == {failure_labels: 1}

    await _restore_pre_finalization_state(world, initial_state)
    world.resources.publisher.events.clear()
    zone = _HostileTimezone(diagnostic, fail_on_call=fail_on_call)
    hostile_started_at = wall_time.replace(tzinfo=zone)
    assert type(hostile_started_at) is datetime
    _ProbeSession.run_started_override = hostile_started_at

    await world.projections.finalize_run_projection_activity(
        _finalize_params(
            world,
            status=RunStatus.FAILED.value,
            error_code="provider_failed",
        )
    )

    hostile_state = await _finalization_product_state(world)
    assert _without_completion_timestamp(hostile_state) == _without_completion_timestamp(
        clean_state
    )
    assert _publisher_product_payload(world) == clean_published
    assert _counter_point_map(world.telemetry, "agent_runs_total") == {run_labels: 2}
    assert (
        _histogram_point_map(
            world.telemetry,
            "agent_run_duration_seconds",
        )
        == clean_durations
    )
    assert _counter_point_map(world.telemetry, "agent_run_failures_total") == {failure_labels: 2}
    assert zone.utcoffset_calls == fail_on_call
    assert zone.raised_traceback is not None


@pytest.mark.parametrize("fail_on_call", [1, 2], ids=["utcoffset", "subtraction"])
@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
async def test_hostile_exact_datetime_timezone_preserves_fatal_authority(
    world: AgentWorld,
    fail_on_call: int,
    fatal_type: type[BaseException],
) -> None:
    initial_state = await _finalization_product_state(world)
    wall_time = datetime.now(UTC) - timedelta(days=17, seconds=5)
    clean_state, _clean_published = await _clean_failed_finalization_control(
        world,
        started_at=wall_time.replace(tzinfo=UTC),
    )
    run_labels = (("outcome", "failed"), ("service", "agent-worker"))
    failure_labels = (("failure_class", "internal"),)
    clean_durations = _histogram_point_map(world.telemetry, "agent_run_duration_seconds")

    await _restore_pre_finalization_state(world, initial_state)
    world.resources.publisher.events.clear()
    fatal = fatal_type("fatal-duration-timezone")
    zone = _HostileTimezone(fatal, fail_on_call=fail_on_call)
    _ProbeSession.run_started_override = wall_time.replace(tzinfo=zone)

    with pytest.raises(fatal_type) as caught:
        await world.projections.finalize_run_projection_activity(
            _finalize_params(
                world,
                status=RunStatus.FAILED.value,
                error_code="provider_failed",
            )
        )

    assert caught.value is fatal
    assert zone.raised_traceback is not None
    assert _traceback_tail(caught.value.__traceback__) is zone.raised_traceback
    assert _traceback_frame_names(caught.value.__traceback__) == (
        "test_hostile_exact_datetime_timezone_preserves_fatal_authority",
        "finalize_run_projection_activity",
        "_persisted_duration_seconds",
        "utcoffset",
    )
    hostile_state = await _finalization_product_state(world)
    assert _without_completion_timestamp(hostile_state) == _without_completion_timestamp(
        clean_state
    )
    assert _publisher_product_payload(world) == []
    assert _counter_point_map(world.telemetry, "agent_runs_total") == {run_labels: 2}
    assert (
        _histogram_point_map(
            world.telemetry,
            "agent_run_duration_seconds",
        )
        == clean_durations
    )
    assert _counter_point_map(world.telemetry, "agent_run_failures_total") == {failure_labels: 1}
    assert zone.utcoffset_calls == fail_on_call


@pytest.mark.parametrize(
    "started_kind",
    ["none", "naive", "malformed", "subclass", "spoof", "future"],
)
async def test_invalid_or_future_persisted_start_suppresses_only_duration(
    world: AgentWorld,
    started_kind: str,
) -> None:
    durable_started_at = (await world.load_run()).started_at
    hostile: _HostileStartedAt | None = None
    if started_kind == "none":
        started: object = None
    elif started_kind == "naive":
        started = datetime.now()
    elif started_kind == "malformed":
        started = object()
    elif started_kind == "subclass":
        started = _DatetimeSubclass.now(UTC) - timedelta(seconds=5)
    elif started_kind == "spoof":
        hostile = _HostileStartedAt()
        started = hostile
    else:
        started = datetime.now(UTC) + timedelta(days=1)
    _ProbeSession.run_started_override = started

    await world.projections.finalize_run_projection_activity(_finalize_params(world))

    assert (
        _metric_sum(
            world.telemetry,
            "agent_runs_total",
            service="agent-worker",
            outcome="completed",
        )
        == 1
    )
    assert _metric_points(world.telemetry, "agent_run_duration_seconds") == []
    run = await world.load_run()
    assert run.started_at == durable_started_at
    assert run.status == RunStatus.COMPLETED.value
    assert run.completed_at is not None
    assert run.steps_used == 2
    assert run.error_code is None
    assert run.error_message is None
    event = await world.load_event("run.completed")
    assert event is not None
    assert event.payload_json == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_micros": 0,
        "steps_used": 2,
        "error_code": None,
        "error_message": None,
    }
    assert [item.event_type for item in world.resources.publisher.events] == [
        "agent.run.completed",
        "task.completed",
    ]
    if hostile is not None:
        assert hostile.touches == 0


async def test_finalize_without_run_id_emits_no_run_metrics(world: AgentWorld) -> None:
    _ProbeSession.commit_order = world.telemetry.order
    await world.projections.finalize_run_projection_activity(_finalize_params(world, run_id=None))

    async with world.sessions() as session:
        task = await session.get(Task, world.task_id)
    assert task is not None
    assert task.state == RunStatus.COMPLETED.value
    run = await world.load_run()
    assert run.status == RunStatus.RUNNING.value
    assert run.completed_at is None
    assert await world.count_events("run.completed") == 0
    assert world.telemetry.order == ["db_commit"]
    assert _metric_points(world.telemetry, "agent_runs_total") == []
    assert _metric_points(world.telemetry, "agent_run_duration_seconds") == []
    assert _metric_points(world.telemetry, "agent_run_failures_total") == []
    assert [item.event_type for item in world.resources.publisher.events] == [
        "agent.run.completed",
        "task.completed",
    ]
    first, second = world.resources.publisher.events
    assert first.workspace_id == str(world.workspace_id)
    assert first.data == {
        "run_id": None,
        "task_id": str(world.task_id),
        "error_code": None,
    }
    assert second.workspace_id == str(world.workspace_id)
    assert second.data == {
        "task_id": str(world.task_id),
        "run_id": None,
    }


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (RunStatus.COMPLETED.value, None),
        (RunStatus.FAILED.value, "provider_failed"),
    ],
)
async def test_finalize_commit_failure_emits_no_metric_and_rolls_back_owner_state(
    world: AgentWorld,
    status: str,
    error_code: str | None,
) -> None:
    failure = RuntimeError("finalize-commit-authority")
    _ProbeSession.fail_next_commit = failure
    _ProbeSession.commit_order = world.telemetry.order

    with pytest.raises(RuntimeError) as caught:
        await world.projections.finalize_run_projection_activity(
            _finalize_params(world, status=status, error_code=error_code)
        )

    assert caught.value is failure
    run = await world.load_run()
    assert run.status == RunStatus.RUNNING.value
    assert run.completed_at is None
    assert await world.count_events(f"run.{status}") == 0
    assert _metric_points(world.telemetry, "agent_runs_total") == []
    assert _metric_points(world.telemetry, "agent_run_duration_seconds") == []
    assert _metric_points(world.telemetry, "agent_run_failures_total") == []
    assert world.telemetry.order == []
    assert world.resources.publisher.events == []


async def test_failed_finalize_records_all_diagnostics_only_after_owner_commit(
    world: AgentWorld,
) -> None:
    world.telemetry.order.clear()
    _ProbeSession.commit_order = world.telemetry.order
    _ProbeSession.run_started_override = datetime.now(UTC) - timedelta(seconds=5)

    await world.projections.finalize_run_projection_activity(
        _finalize_params(
            world,
            status=RunStatus.FAILED.value,
            error_code="provider_failed",
        )
    )

    assert world.telemetry.order.count("db_commit") == 1
    commit_index = world.telemetry.order.index("db_commit")
    assert commit_index < world.telemetry.order.index("agent_runs_total")
    assert commit_index < world.telemetry.order.index("agent_run_duration_seconds")
    assert commit_index < world.telemetry.order.index("agent_run_failures_total")
    assert (
        _metric_sum(
            world.telemetry,
            "agent_runs_total",
            service="agent-worker",
            outcome="failed",
        )
        == 1
    )
    assert len(_metric_points(world.telemetry, "agent_run_duration_seconds")) == 1
    assert (
        _metric_sum(
            world.telemetry,
            "agent_run_failures_total",
            failure_class="internal",
        )
        == 1
    )


@pytest.mark.parametrize("non_owner", ["missing-run", "workspace-mismatch", "task-mismatch"])
async def test_finalize_non_owner_preserves_exact_error_product_and_zero_telemetry(
    world: AgentWorld,
    non_owner: str,
) -> None:
    params = _finalize_params(world)
    target_task_id = world.task_id
    if non_owner == "missing-run":
        params.run_id = str(new_uuid7())
    elif non_owner == "workspace-mismatch":
        params.workspace_id = str(new_uuid7())
    else:
        async with world.sessions() as session:
            other_task = await _add_other_task(
                session,
                workspace_id=world.workspace_id,
                agent_id=world.agent_id,
            )
            await session.commit()
            target_task_id = other_task.id
        params.task_id = str(target_task_id)
    before_run = await world.load_run()
    run_payload = (
        before_run.status,
        before_run.completed_at,
        before_run.steps_used,
        before_run.error_code,
        before_run.error_message,
    )
    async with world.sessions() as session:
        original_task = await session.get(Task, world.task_id)
        target_task = await session.get(Task, target_task_id)
    assert original_task is not None and target_task is not None
    task_payloads = {
        original_task.id: (original_task.state, original_task.updated_at),
        target_task.id: (target_task.state, target_task.updated_at),
    }

    caught: ApplicationError | None = None
    try:
        await world.projections.finalize_run_projection_activity(params)
    except ApplicationError as error:
        caught = error

    assert caught is not None
    assert _application_error_public(caught) == {
        "message": "agent run not found for final projection",
        "args": ("run_not_found: agent run not found for final projection",),
        "details": (),
        "type": "run_not_found",
        "non_retryable": True,
        "next_retry_delay": None,
        "category": caught.category,
        "suppress_context": False,
        "cause_type": None,
        "cause_args": None,
    }
    assert _traceback_frame_names(caught.__traceback__) == (
        "test_finalize_non_owner_preserves_exact_error_product_and_zero_telemetry",
        "finalize_run_projection_activity",
    )
    after_run = await world.load_run()
    assert (
        after_run.status,
        after_run.completed_at,
        after_run.steps_used,
        after_run.error_code,
        after_run.error_message,
    ) == run_payload
    async with world.sessions() as session:
        reloaded_original = await session.get(Task, world.task_id)
        reloaded_target = await session.get(Task, target_task_id)
    assert reloaded_original is not None and reloaded_target is not None
    assert {
        reloaded_original.id: (reloaded_original.state, reloaded_original.updated_at),
        reloaded_target.id: (reloaded_target.state, reloaded_target.updated_at),
    } == task_payloads
    assert await world.count_events("run.completed") == 0
    assert world.resources.publisher.events == []
    assert _metric_points(world.telemetry, "agent_runs_total") == []
    assert _metric_points(world.telemetry, "agent_run_duration_seconds") == []
    assert _metric_points(world.telemetry, "agent_run_failures_total") == []


class _ExplodingAgentCounter:
    def __init__(self, error: BaseException, calls: list[str], name: str) -> None:
        self._error = error
        self._calls = calls
        self._name = name
        self.raised_traceback: TracebackType | None = None

    def add(self, _amount: object, **_labels: str) -> None:
        self._calls.append(f"{self._name}:add")
        try:
            raise self._error
        except BaseException as error:
            self.raised_traceback = error.__traceback__
            raise


class _ExplodingAgentHistogram:
    def __init__(self, error: BaseException, calls: list[str], name: str) -> None:
        self._error = error
        self._calls = calls
        self._name = name
        self.raised_traceback: TracebackType | None = None

    def record(self, _amount: object, **_labels: str) -> None:
        self._calls.append(f"{self._name}:record")
        try:
            raise self._error
        except BaseException as error:
            self.raised_traceback = error.__traceback__
            raise


class _SelectiveHostileAgentMetrics:
    is_noop = False

    def __init__(
        self,
        wrapped: JhinMetrics,
        *,
        target: str,
        phase: str,
        error: BaseException,
    ) -> None:
        self._wrapped = wrapped
        self._target = target
        self._phase = phase
        self._error = error
        self.calls: list[str] = []
        self.raised_traceback: TracebackType | None = None
        self.instrument: _ExplodingAgentCounter | _ExplodingAgentHistogram | None = None

    def _raise_owned(self) -> None:
        try:
            raise self._error
        except BaseException as error:
            self.raised_traceback = error.__traceback__
            raise

    def counter(self, name: str) -> Any:
        self.calls.append(f"{name}:getter")
        if name == self._target and self._phase == "getter":
            self._raise_owned()
        if name == self._target and self._phase == "write":
            self.instrument = _ExplodingAgentCounter(self._error, self.calls, name)
            return self.instrument
        return self._wrapped.counter(cast(Any, name))

    def histogram(self, name: str) -> Any:
        self.calls.append(f"{name}:getter")
        if name == self._target and self._phase == "getter":
            self._raise_owned()
        if name == self._target and self._phase == "write":
            self.instrument = _ExplodingAgentHistogram(self._error, self.calls, name)
            return self.instrument
        return self._wrapped.histogram(cast(Any, name))

    def set_observable(self, name: str, observations: object) -> None:
        self._wrapped.set_observable(cast(Any, name), cast(Any, observations))


_FINALIZATION_METRIC_TARGET_CASES: tuple[tuple[str, str, str | None], ...] = (
    ("agent_runs_total", RunStatus.COMPLETED.value, None),
    ("agent_runs_total", RunStatus.FAILED.value, "provider_failed"),
    ("agent_run_duration_seconds", RunStatus.COMPLETED.value, None),
    ("agent_run_duration_seconds", RunStatus.FAILED.value, "provider_failed"),
    ("agent_run_failures_total", RunStatus.FAILED.value, "provider_failed"),
)


async def test_hostile_finalization_metric_seams_preserve_commit_and_other_points(
    tmp_path: Path,
) -> None:
    # Loop-folded parametrize matrix: same full cross-product, one collected item.
    # Each case owns a fresh telemetry+database world, matching the old per-item
    # fixtures (each case compares exact cumulative metric point maps).
    for diagnostic_kind in ("ordinary", "cancellation"):
        for target, status, error_code in _FINALIZATION_METRIC_TARGET_CASES:
            for phase in ("getter", "write"):
                case = (
                    f"diagnostic_kind={diagnostic_kind}, target={target}, "
                    f"status={status}, error_code={error_code!r}, phase={phase}"
                )
                with (
                    _owned_telemetry() as telemetry,
                    pytest.MonkeyPatch.context() as monkeypatch,
                ):
                    async with _owned_world(monkeypatch, telemetry, tmp_path) as world:
                        await _assert_hostile_finalization_metric_seam(
                            world, phase, target, status, error_code, diagnostic_kind, case
                        )


async def _assert_hostile_finalization_metric_seam(
    world: AgentWorld,
    phase: str,
    target: str,
    status: str,
    error_code: str | None,
    diagnostic_kind: str,
    case: str,
) -> None:
    initial_state = await _finalization_product_state(world)
    started_at = datetime.now(UTC) - timedelta(seconds=5)
    _ProbeSession.run_started_override = started_at
    params = _finalize_params(world, status=status, error_code=error_code)

    await world.projections.finalize_run_projection_activity(params)

    clean_completed_at = _ProbeSession.committed_completed_at
    assert clean_completed_at is not None, case
    clean_state = await _finalization_product_state(world)
    clean_publisher = _publisher_product_payload(world)
    run_labels = tuple(sorted({"service": "agent-worker", "outcome": status}.items()))
    duration_labels = (("outcome", status),)
    failure_labels = (("failure_class", "internal"),)
    clean_runs = _counter_point_map(world.telemetry, "agent_runs_total")
    clean_durations = _histogram_point_map(
        world.telemetry,
        "agent_run_duration_seconds",
    )
    clean_failures = _counter_point_map(world.telemetry, "agent_run_failures_total")
    clean_duration = (clean_completed_at - started_at).total_seconds()
    assert clean_runs == {run_labels: 1}, case
    assert clean_durations == {duration_labels: (1, clean_duration)}, case
    assert clean_failures == ({failure_labels: 1} if status == RunStatus.FAILED.value else {}), case

    await _restore_pre_finalization_state(world, initial_state)
    world.resources.publisher.events.clear()
    _ProbeSession.committed_completed_at = None
    observed_metrics = _CountingMetrics(world.telemetry.metrics)
    diagnostic: BaseException = RuntimeError("hostile-agent-metric")
    if diagnostic_kind == "cancellation":
        diagnostic = asyncio.CancelledError("diagnostic-agent-metric-cancellation")
    hostile = _SelectiveHostileAgentMetrics(
        cast(JhinMetrics, observed_metrics),
        target=target,
        phase=phase,
        error=diagnostic,
    )
    world.resources.runtime.metrics = cast(JhinMetrics, hostile)
    world.projections = AgentProjectionActivities(cast(Any, world.resources))

    await world.projections.finalize_run_projection_activity(params)

    hostile_completed_at = _ProbeSession.committed_completed_at
    assert hostile_completed_at is not None, case
    hostile_state = await _finalization_product_state(world)
    comparable_clean_state = deepcopy(clean_state)
    cast(dict[str, object], comparable_clean_state["run"])["completed_at"] = cast(
        Mapping[str, object], hostile_state["run"]
    )["completed_at"]
    assert hostile_state == comparable_clean_state, case
    assert _publisher_product_payload(world) == clean_publisher, case
    assert any(call.startswith(f"{target}:") for call in hostile.calls), case
    hostile_duration = (hostile_completed_at - started_at).total_seconds()
    expected_writes: list[tuple[Any, ...]] = []
    if target != "agent_runs_total":
        expected_writes.append(
            (
                "add",
                "agent_runs_total",
                1,
                {"service": "agent-worker", "outcome": status},
            )
        )
    if target != "agent_run_duration_seconds":
        expected_writes.append(
            (
                "record",
                "agent_run_duration_seconds",
                hostile_duration,
                {"outcome": status},
            )
        )
    if status == RunStatus.FAILED.value and target != "agent_run_failures_total":
        expected_writes.append(
            (
                "add",
                "agent_run_failures_total",
                1,
                {"failure_class": "internal"},
            )
        )
    assert [
        call for call in observed_metrics.calls if call[0] in {"add", "record"}
    ] == expected_writes, case

    after_runs = _counter_point_map(world.telemetry, "agent_runs_total")
    assert after_runs == {run_labels: clean_runs[run_labels] + int(target != "agent_runs_total")}, (
        case
    )
    after_durations = _histogram_point_map(
        world.telemetry,
        "agent_run_duration_seconds",
    )
    expected_duration_count = 1 + int(target != "agent_run_duration_seconds")
    expected_duration_sum = clean_duration + (
        0 if target == "agent_run_duration_seconds" else hostile_duration
    )
    assert after_durations == {duration_labels: (expected_duration_count, expected_duration_sum)}, (
        case
    )
    after_failures = _counter_point_map(world.telemetry, "agent_run_failures_total")
    if status == RunStatus.FAILED.value:
        assert after_failures == {
            failure_labels: clean_failures[failure_labels]
            + int(target != "agent_run_failures_total")
        }, case
    else:
        assert after_failures == {}, case


async def test_fatal_finalization_metric_error_propagates_after_durable_commit(
    tmp_path: Path,
) -> None:
    # Loop-folded parametrize matrix: same full cross-product, one collected item.
    # Each case owns a fresh telemetry+database world.  The finalization call stays
    # lexically inside this function because the expected traceback frame names
    # begin with this test function's own name.
    for fatal_type in (KeyboardInterrupt, SystemExit):
        for target, status, error_code in _FINALIZATION_METRIC_TARGET_CASES:
            for phase in ("getter", "write"):
                case = (
                    f"fatal_type={fatal_type.__name__}, target={target}, "
                    f"status={status}, error_code={error_code!r}, phase={phase}"
                )
                with (
                    _owned_telemetry() as telemetry,
                    pytest.MonkeyPatch.context() as monkeypatch,
                ):
                    async with _owned_world(monkeypatch, telemetry, tmp_path) as world:
                        fatal = fatal_type("fatal-agent-metric")
                        hostile = _SelectiveHostileAgentMetrics(
                            world.telemetry.metrics,
                            target=target,
                            phase=phase,
                            error=fatal,
                        )
                        world.resources.runtime.metrics = cast(JhinMetrics, hostile)
                        world.projections = AgentProjectionActivities(cast(Any, world.resources))
                        _ProbeSession.run_started_override = datetime.now(UTC) - timedelta(
                            seconds=5
                        )

                        with pytest.raises(fatal_type) as caught:
                            await world.projections.finalize_run_projection_activity(
                                _finalize_params(world, status=status, error_code=error_code)
                            )

                        assert caught.value is fatal, case
                        raise_site = hostile.raised_traceback
                        if raise_site is None and hostile.instrument is not None:
                            raise_site = hostile.instrument.raised_traceback
                        assert raise_site is not None, case
                        assert _traceback_tail(caught.value.__traceback__) is raise_site, case
                        instrument_helper = (
                            "_record_agent_histogram"
                            if target == "agent_run_duration_seconds"
                            else "_record_agent_counter"
                        )
                        backend_method = (
                            "histogram"
                            if target == "agent_run_duration_seconds" and phase == "getter"
                            else "record"
                            if target == "agent_run_duration_seconds"
                            else "counter"
                            if phase == "getter"
                            else "add"
                        )
                        expected_frames = (
                            "test_fatal_finalization_metric_error_propagates_after_durable_commit",
                            "finalize_run_projection_activity",
                            instrument_helper,
                            "_run_agent_metric",
                            "<lambda>",
                            backend_method,
                        )
                        if phase == "getter":
                            expected_frames += ("_raise_owned",)
                        assert (
                            _traceback_frame_names(caught.value.__traceback__) == expected_frames
                        ), case
                        run = await world.load_run()
                        assert run.status == status, case
                        assert run.completed_at is not None, case
                        assert await world.count_events(f"run.{status}") == 1, case
                        expected_run = 0 if target == "agent_runs_total" else 1
                        assert (
                            _metric_sum(
                                world.telemetry,
                                "agent_runs_total",
                                service="agent-worker",
                                outcome=status,
                            )
                            == expected_run
                        ), case
                        assert len(
                            _metric_points(world.telemetry, "agent_run_duration_seconds")
                        ) == int(target == "agent_run_failures_total"), case
                        assert _metric_points(world.telemetry, "agent_run_failures_total") == [], (
                            case
                        )
                        assert world.resources.publisher.events == [], case
