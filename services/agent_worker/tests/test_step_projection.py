"""Agent projections rebuild transcript and timeline only from durable IDs."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import flag_modified
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_agent_worker.projections import AgentProjectionActivities
from jhin_agent_worker.reasoning import AgentStepReasoningRecord, AgentStepUsage
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentRun,
    Approval,
    Message,
    RunEvent,
    Task,
    ToolCall,
    Workspace,
)
from jhin_domain import (
    ApprovalStatus,
    MessageVisibility,
    RunStatus,
    SenderType,
    TaskState,
    ToolCallStatus,
    new_uuid7,
)
from jhin_observability import noop_metrics, noop_tracer
from jhin_tools import stable_tool_invocation_id
from jhin_workflows import AGENT_TASK_QUEUE, TOOL_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskInput, AgentTaskWorkflow
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_SNAPSHOT,
    AdvertisedTool,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    ExecuteBoundToolInput,
    FinalizeInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    SnapshotResult,
    WorkRequestStart,
)


class _Publisher:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.events.append(envelope)


class _Resources:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.runtime = SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())
        self.session_factory = sessions
        self.publisher = _Publisher()
        self.crypto = None


class _CancellationWorkflowActivities:
    def __init__(self, world: ProjectionWorld) -> None:
        self._world = world
        self.execute_started = asyncio.Event()
        self.release_execute = asyncio.Event()

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot(self, _params: AgentTaskInput) -> SnapshotResult:
        return SnapshotResult(
            run_id=str(self._world.run_id),
            snapshot_json="{}",
            snapshot_hash="projection-cancellation",
            max_steps=2,
        )

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def resolve_advertised_tools(
        self,
        _params: ResolveAdvertisedToolsInput,
    ) -> list[AdvertisedTool]:
        return [
            AdvertisedTool(
                name="system.echo",
                description="Echo one value",
                parameters={"type": "object"},
            )
        ]

    @activity.defn(name=ACTIVITY_REASON_AGENT_STEP)
    async def reason_agent_step(
        self,
        _params: ReasonAgentStepInput,
    ) -> ReasonAgentStepResult:
        return ReasonAgentStepResult(call_count=3)

    @activity.defn(name=ACTIVITY_EXECUTE_BOUND_TOOL)
    async def execute_bound_tool(self, params: ExecuteBoundToolInput) -> BoundToolResult:
        assert params.ordinal == 0
        self.execute_started.set()
        await self.release_execute.wait()
        return BoundToolResult(
            tool_call_id=str(stable_tool_invocation_id(self._world.run_id, 0, 0)),
            status="executed",
        )

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup_run_workspace(
        self,
        _params: CleanupRunWorkspaceInput,
    ) -> CleanupRunWorkspaceResult:
        return CleanupRunWorkspaceResult(deleted=True)


@dataclass
class ProjectionWorld:
    projections: AgentProjectionActivities
    sessions: async_sessionmaker[AsyncSession]
    publisher: _Publisher
    workspace_id: Any
    agent_id: Any
    task_id: Any
    run_id: Any

    def commit_params(
        self,
        *,
        ids: list[str] | None = None,
        cancelled_after_tool_call_id: str | None = None,
    ) -> CommitAgentStepInput:
        return CommitAgentStepInput(
            workspace_id=str(self.workspace_id),
            task_id=str(self.task_id),
            run_id=str(self.run_id),
            agent_id=str(self.agent_id),
            step_index=0,
            gateway_tool_call_ids=(
                ids if ids is not None else [str(stable_tool_invocation_id(self.run_id, 0, 0))]
            ),
            cancelled_after_tool_call_id=cancelled_after_tool_call_id,
        )

    async def load_commit_payload(self) -> dict[str, Any]:
        async with self.sessions() as session:
            event = await session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == self.run_id,
                    RunEvent.event_type == "agent.step.committed",
                )
            )
            assert event is not None
            return event.payload_json

    async def load_tool_call_ids(self) -> list[str]:
        async with self.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == self.run_id)
                    .order_by(ToolCall.created_at)
                )
            )
            return [str(row.id) for row in rows]

    async def seed_step(
        self,
        *,
        statuses: list[str],
        manifest_count: int | None = None,
        tool_names: list[str] | None = None,
        approval_ordinals: frozenset[int] = frozenset(),
        outputs: list[dict[str, Any]] | None = None,
        completion: str = "Calling a tool.",
    ) -> None:
        count = manifest_count if manifest_count is not None else len(statuses)
        names = tool_names if tool_names is not None else ["system.echo"] * count
        assert len(names) == count
        row_outputs = outputs if outputs is not None else [{} for _ in statuses]
        assert len(row_outputs) == len(statuses)
        async with self.sessions() as session:
            session.add_all(
                [
                    RunEvent(
                        workspace_id=self.workspace_id,
                        task_id=self.task_id,
                        run_id=self.run_id,
                        seq=0,
                        event_type="agent.step.tool_manifest",
                        payload_json={
                            "step": 0,
                            "manifest": {
                                "count": count,
                                "calls": [
                                    {
                                        "ordinal": ordinal,
                                        "lossless": True,
                                        "tool_name": names[ordinal],
                                        "arguments_json": (
                                            '{"value":"first"}'
                                            if ordinal == 0
                                            else '{"value":"must-not-project"}'
                                        ),
                                    }
                                    for ordinal in range(count)
                                ],
                            },
                        },
                    ),
                    RunEvent(
                        workspace_id=self.workspace_id,
                        task_id=self.task_id,
                        run_id=self.run_id,
                        seq=1,
                        event_type="agent.step.reasoning",
                        payload_json=AgentStepReasoningRecord(
                            step=0,
                            completion_sanitized=completion,
                            model="projection-test",
                            finish_reason="tool_calls",
                            provider_request_id="private-provider-request",
                            provider_call_ids=tuple(
                                f"private-provider-call-{ordinal + 1}" for ordinal in range(count)
                            ),
                            transitions=(
                                {"node": "load_context", "detail": "context loaded"},
                                {"node": "reason", "detail": "model responded"},
                                {"node": "call_tool", "detail": "tool requested"},
                            ),
                            done=count == 0,
                            usage=AgentStepUsage(
                                input_tokens=7,
                                output_tokens=3,
                                cached_tokens=1,
                                cost_micros=10,
                            ),
                            latency_ms=4,
                        ).to_payload(),
                    ),
                ]
            )
            for ordinal, status in enumerate(statuses):
                approval_id = None
                if ordinal in approval_ordinals:
                    approval = Approval(
                        workspace_id=self.workspace_id,
                        task_id=self.task_id,
                        run_id=self.run_id,
                        requested_by_agent_id=self.agent_id,
                        action_type=names[ordinal],
                        action_payload_sanitized={"risk": "destructive"},
                        reason="approval required",
                        status=ApprovalStatus.PENDING.value,
                        requested_at=datetime.now(UTC),
                    )
                    session.add(approval)
                    await session.flush()
                    approval_id = approval.id
                session.add(
                    ToolCall(
                        id=stable_tool_invocation_id(self.run_id, 0, ordinal),
                        workspace_id=self.workspace_id,
                        run_id=self.run_id,
                        agent_id=self.agent_id,
                        tool_name=names[ordinal],
                        sanitized_input_json={"value": f"call-{ordinal}"},
                        sanitized_output_json=row_outputs[ordinal],
                        status=status,
                        approval_id=approval_id,
                        error_code=(
                            "execution_outcome_unknown"
                            if status == ToolCallStatus.EXECUTION_UNKNOWN.value
                            else None
                        ),
                    )
                )
            await session.commit()

    async def seed_manifest_and_tool_call(self, *, status: str) -> None:
        await self.seed_step(statuses=[status], manifest_count=2)

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

    async def count_projection_messages(self) -> int:
        async with self.sessions() as session:
            return (
                await session.scalar(
                    select(func.count(Message.id)).where(Message.run_id == self.run_id)
                )
                or 0
            )

    async def load_run(self) -> AgentRun:
        async with self.sessions() as session:
            run = await session.get(AgentRun, self.run_id)
            assert run is not None
            return run


@pytest.fixture
async def world() -> AsyncIterator[ProjectionWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    resources = _Resources(sessions)
    async with sessions() as session:
        workspace = Workspace(name="Projection", slug=f"projection-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Projector", slug="projector")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Project calls",
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
        await session.commit()
    yield ProjectionWorld(
        projections=AgentProjectionActivities(resources),  # type: ignore[arg-type]
        sessions=sessions,
        publisher=resources.publisher,
        workspace_id=workspace.id,
        agent_id=agent.id,
        task_id=task.id,
        run_id=run.id,
    )
    await engine.dispose()


async def test_projection_is_idempotent_and_unknown_is_durable(
    world: ProjectionWorld,
) -> None:
    await world.seed_manifest_and_tool_call(status=ToolCallStatus.EXECUTION_UNKNOWN.value)

    with pytest.raises(ApplicationError) as first:
        await world.projections.commit_agent_step_activity(world.commit_params())
    assert first.value.type == "tool_execution_unknown"
    with pytest.raises(ApplicationError) as replay:
        await world.projections.commit_agent_step_activity(world.commit_params())
    assert replay.value.type == "tool_execution_unknown"
    assert await world.count_events("agent.step.committed") == 1
    assert await world.count_projection_messages() == 2
    assert (await world.load_run()).error_code == "tool_execution_unknown"

    async with world.sessions() as session:
        public_events = list(
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == world.run_id,
                    RunEvent.event_type != "agent.step.reasoning",
                )
            )
        )
        serialized = str([event.payload_json for event in public_events])
        assert "private-provider-request" not in serialized
        assert "private-provider-call" not in serialized


async def test_generic_finalization_preserves_execution_unknown_diagnosis(
    world: ProjectionWorld,
) -> None:
    await world.seed_manifest_and_tool_call(status=ToolCallStatus.EXECUTION_UNKNOWN.value)
    invocation_id = str(stable_tool_invocation_id(world.run_id, 0, 0))
    expected_message = (
        f"tool call {invocation_id} execution outcome is unknown; manual reconciliation is required"
    )

    with pytest.raises(ApplicationError) as projection_error:
        await world.projections.commit_agent_step_activity(world.commit_params())
    assert projection_error.value.type == "tool_execution_unknown"
    assert projection_error.value.message == expected_message

    await world.projections.finalize_run_projection_activity(
        FinalizeInput(
            workspace_id=str(world.workspace_id),
            task_id=str(world.task_id),
            run_id=str(world.run_id),
            status=RunStatus.FAILED.value,
            steps_used=0,
            error_code="step_failed",
            error_message="the workflow observed a generic step activity failure",
        )
    )

    run = await world.load_run()
    assert run.error_code == "tool_execution_unknown"
    assert run.error_message == expected_message
    async with world.sessions() as session:
        event = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == world.run_id,
                RunEvent.event_type == "run.failed",
            )
        )
        assert event is not None
        assert event.payload_json["error_code"] == "tool_execution_unknown"
        assert event.payload_json["error_message"] == expected_message


async def test_projection_rejects_noncanonical_tool_id_prefix(world: ProjectionWorld) -> None:
    await world.seed_manifest_and_tool_call(status=ToolCallStatus.EXECUTION_UNKNOWN.value)

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(
            world.commit_params(ids=[str(new_uuid7())])
        )

    assert error.value.type == "tool_projection_binding_mismatch"
    assert await world.count_events("agent.step.committed") == 0
    assert await world.count_projection_messages() == 0


async def test_cancellation_truncation_commits_exact_completed_prefix_atomically(
    world: ProjectionWorld,
) -> None:
    await world.seed_step(
        statuses=[ToolCallStatus.COMPLETED.value],
        manifest_count=3,
    )
    completed_id = str(stable_tool_invocation_id(world.run_id, 0, 0))
    params = world.commit_params(
        ids=[completed_id],
        cancelled_after_tool_call_id=completed_id,
    )

    result = await world.projections.commit_agent_step_activity(params)

    assert result.done is False
    assert await world.load_tool_call_ids() == [completed_id]
    assert await world.count_projection_messages() == 2
    assert await world.count_events("agent.step.committed") == 1
    payload = await world.load_commit_payload()
    assert payload["gateway_tool_call_ids"] == [completed_id]
    assert payload["cancelled_after_tool_call_id"] == completed_id


async def test_workflow_cancellation_commits_real_projection_without_a_later_effect(
    world: ProjectionWorld,
) -> None:
    await world.seed_step(
        statuses=[ToolCallStatus.COMPLETED.value],
        manifest_count=3,
    )
    completed_id = str(stable_tool_invocation_id(world.run_id, 0, 0))
    activities = _CancellationWorkflowActivities(world)
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        async with (
            Worker(
                environment.client,
                task_queue=AGENT_TASK_QUEUE,
                workflows=[AgentTaskWorkflow],
                activities=[
                    activities.resolve_snapshot,
                    activities.reason_agent_step,
                    world.projections.commit_agent_step_activity,
                    world.projections.finalize_run_projection_activity,
                ],
            ),
            Worker(
                environment.client,
                task_queue=TOOL_TASK_QUEUE,
                activities=[
                    activities.resolve_advertised_tools,
                    activities.execute_bound_tool,
                    activities.cleanup_run_workspace,
                ],
            ),
        ):
            params = AgentTaskInput(
                workspace_id=str(world.workspace_id),
                task_id=str(world.task_id),
                agent_id=str(world.agent_id),
            )
            handle = await environment.client.start_workflow(
                AgentTaskWorkflow.run,
                params,
                id=f"projection-cancellation-{world.run_id}",
                task_queue=AGENT_TASK_QUEUE,
            )
            result_task = asyncio.create_task(handle.result())
            await asyncio.wait_for(activities.execute_started.wait(), timeout=5)
            await handle.signal("cancel")
            activities.release_execute.set()
            result = await asyncio.wait_for(result_task, timeout=5)
    finally:
        activities.release_execute.set()
        await environment.shutdown()

    assert result.status == "cancelled"
    assert result.steps_used == 1
    assert await world.load_tool_call_ids() == [completed_id]
    assert await world.count_projection_messages() == 2
    assert await world.count_events("agent.step.committed") == 1
    payload = await world.load_commit_payload()
    assert payload["gateway_tool_call_ids"] == [completed_id]
    assert payload["cancelled_after_tool_call_id"] == completed_id
    assert (await world.load_run()).status == RunStatus.CANCELLED.value


async def test_cancellation_truncation_cannot_hide_a_later_effect(
    world: ProjectionWorld,
) -> None:
    await world.seed_step(
        statuses=[
            ToolCallStatus.COMPLETED.value,
            ToolCallStatus.COMPLETED.value,
        ],
        manifest_count=3,
    )
    completed_id = str(stable_tool_invocation_id(world.run_id, 0, 0))
    params = world.commit_params(
        ids=[completed_id],
        cancelled_after_tool_call_id=completed_id,
    )

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(params)

    assert error.value.type == "tool_projection_binding_mismatch"
    assert await world.count_events("agent.step.committed") == 0
    assert await world.count_projection_messages() == 0


async def test_cancellation_truncation_rejects_a_nonexecuted_prefix(
    world: ProjectionWorld,
) -> None:
    await world.seed_step(
        statuses=[ToolCallStatus.FAILED.value],
        manifest_count=2,
    )
    failed_id = str(stable_tool_invocation_id(world.run_id, 0, 0))

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(
            world.commit_params(
                ids=[failed_id],
                cancelled_after_tool_call_id=failed_id,
            )
        )

    assert error.value.type == "tool_projection_binding_mismatch"
    assert await world.count_events("agent.step.committed") == 0
    assert await world.count_projection_messages() == 0


@pytest.mark.parametrize("stop_kind", ["approval", "delegation", "execution_unknown"])
async def test_projection_rejects_rows_after_an_earlier_durable_stop(
    world: ProjectionWorld,
    stop_kind: str,
) -> None:
    statuses = [ToolCallStatus.COMPLETED.value, ToolCallStatus.COMPLETED.value]
    names = ["system.echo", "system.echo"]
    approvals: frozenset[int] = frozenset()
    outputs: list[dict[str, Any]] = [{}, {}]
    if stop_kind == "approval":
        statuses[0] = ToolCallStatus.PENDING_APPROVAL.value
        approvals = frozenset({0})
    elif stop_kind == "delegation":
        names[0] = "organization.delegate_task"
        outputs[0] = {
            "child_task_id": str(new_uuid7()),
            "target_agent_id": str(new_uuid7()),
            "blocking": True,
            "kind": "delegation",
        }
    else:
        statuses[0] = ToolCallStatus.EXECUTION_UNKNOWN.value
    await world.seed_step(
        statuses=statuses,
        tool_names=names,
        approval_ordinals=approvals,
        outputs=outputs,
    )
    ids = [str(stable_tool_invocation_id(world.run_id, 0, ordinal)) for ordinal in range(2)]

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(world.commit_params(ids=ids))

    assert error.value.type == "tool_projection_binding_mismatch"
    assert await world.count_events("agent.step.committed") == 0
    assert await world.count_projection_messages() == 0


@pytest.mark.parametrize("retry_ids", [[], [str(new_uuid7())]])
async def test_committed_projection_replay_requires_the_exact_bound_ids(
    world: ProjectionWorld,
    retry_ids: list[str],
) -> None:
    await world.seed_step(statuses=[ToolCallStatus.COMPLETED.value])
    canonical = str(stable_tool_invocation_id(world.run_id, 0, 0))
    first = await world.projections.commit_agent_step_activity(world.commit_params(ids=[canonical]))

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(world.commit_params(ids=retry_ids))

    assert first.execution_unknown_tool_call_id is None
    assert error.value.type == "tool_projection_binding_mismatch"
    assert await world.count_events("agent.step.committed") == 1


async def test_pending_approval_without_matching_approval_fails_closed(
    world: ProjectionWorld,
) -> None:
    await world.seed_step(statuses=[ToolCallStatus.PENDING_APPROVAL.value])

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(world.commit_params())

    assert error.value.type == "tool_projection_binding_mismatch"
    assert await world.count_events("agent.step.committed") == 0
    assert (await world.load_run()).status == RunStatus.RUNNING.value


async def test_finalize_projection_is_idempotent_for_an_already_finalized_run(
    world: ProjectionWorld,
) -> None:
    params = FinalizeInput(
        workspace_id=str(world.workspace_id),
        task_id=str(world.task_id),
        run_id=str(world.run_id),
        status=RunStatus.COMPLETED.value,
        steps_used=2,
    )

    await world.projections.finalize_run_projection_activity(params)
    await world.projections.finalize_run_projection_activity(params)

    assert await world.count_events("run.completed") == 1
    assert (await world.load_run()).status == RunStatus.COMPLETED.value
    async with world.sessions() as session:
        task = await session.get(Task, world.task_id)
        assert task is not None
        assert task.state == TaskState.COMPLETED.value
    assert len(world.publisher.events) == 2


async def test_finalize_projection_rejects_a_run_bound_to_another_task(
    world: ProjectionWorld,
) -> None:
    async with world.sessions() as session:
        other = Task(
            workspace_id=world.workspace_id,
            title="Unrelated task",
            assigned_agent_id=world.agent_id,
            correlation_id=new_uuid7(),
        )
        session.add(other)
        await session.commit()
        other_task_id = other.id

    with pytest.raises(ApplicationError) as error:
        await world.projections.finalize_run_projection_activity(
            FinalizeInput(
                workspace_id=str(world.workspace_id),
                task_id=str(other_task_id),
                run_id=str(world.run_id),
                status=RunStatus.COMPLETED.value,
                steps_used=1,
            )
        )

    assert error.value.type == "run_not_found"
    assert (await world.load_run()).status == RunStatus.RUNNING.value
    async with world.sessions() as session:
        task = await session.get(Task, other_task_id)
        assert task is not None
        assert task.state == TaskState.QUEUED.value
    assert world.publisher.events == []


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("step", False),
        ("ordinal", "0"),
        ("ordinal", False),
        ("ordinal", 0.0),
        ("lossless", 1),
    ],
)
async def test_projection_rejects_coercible_manifest_scalars(
    world: ProjectionWorld,
    field: str,
    wrong_value: Any,
) -> None:
    await world.seed_step(statuses=[ToolCallStatus.COMPLETED.value])
    async with world.sessions() as session:
        event = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == world.run_id,
                RunEvent.event_type == "agent.step.tool_manifest",
            )
        )
        assert event is not None
        payload = deepcopy(event.payload_json)
        if field == "step":
            payload["step"] = wrong_value
        else:
            payload["manifest"]["calls"][0][field] = wrong_value
        event.payload_json = payload
        flag_modified(event, "payload_json")
        await session.commit()

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(world.commit_params())

    assert error.value.type == "tool_step_manifest_invalid"
    assert await world.count_events("agent.step.committed") == 0


async def _wait_for_postgres_lock_waiters(
    observer: asyncpg.Connection,
    *,
    database_name: str,
    expected: int,
) -> None:
    for _attempt in range(300):
        waiting = await observer.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND wait_event_type = 'Lock' "
            "AND query LIKE '%agent_run%'",
            database_name,
        )
        if waiting == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} PostgreSQL run-lock waiters")


@pytest.mark.integration
async def test_concurrent_finalize_projection_serializes_one_terminal_event() -> None:
    host = os.environ.get("JHIN_POSTGRES_HOST", "127.0.0.1")
    port = int(os.environ.get("POSTGRES_DEV_PORT", "55432"))
    database_name = f"jhin_projection_{uuid4().hex}"
    admin_dsn = f"postgresql://jhin:jhin@{host}:{port}/postgres"
    database_url = f"postgresql+asyncpg://jhin:jhin@{host}:{port}/{database_name}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await admin.close()

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        resources = _Resources(sessions)
        async with sessions() as session:
            workspace = Workspace(
                name="Finalize concurrency",
                slug=f"finalize-{new_uuid7().hex[:8]}",
            )
            session.add(workspace)
            await session.flush()
            agent = Agent(workspace_id=workspace.id, name="Finalizer", slug="finalizer")
            session.add(agent)
            await session.flush()
            task = Task(
                workspace_id=workspace.id,
                title="Finalize once",
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
            await session.commit()
        params = FinalizeInput(
            workspace_id=str(workspace.id),
            task_id=str(task.id),
            run_id=str(run.id),
            status=RunStatus.COMPLETED.value,
            steps_used=1,
        )
        projections = AgentProjectionActivities(resources)  # type: ignore[arg-type]
        observer = await asyncpg.connect(admin_dsn)
        try:
            async with sessions() as blocker:
                locked = await blocker.scalar(
                    select(AgentRun).where(AgentRun.id == run.id).with_for_update()
                )
                assert locked is not None
                calls = [
                    asyncio.create_task(projections.finalize_run_projection_activity(params))
                    for _attempt in range(2)
                ]
                wait_error: BaseException | None = None
                try:
                    await _wait_for_postgres_lock_waiters(
                        observer,
                        database_name=database_name,
                        expected=2,
                    )
                    assert not any(call.done() for call in calls)
                except BaseException as error:
                    wait_error = error
                finally:
                    await blocker.commit()
                results = await asyncio.gather(*calls, return_exceptions=True)
                if wait_error is not None:
                    raise wait_error
                assert results == [None, None]
        finally:
            await observer.close()

        async with sessions() as session:
            terminal_events = await session.scalar(
                select(func.count(RunEvent.id)).where(
                    RunEvent.run_id == run.id,
                    RunEvent.event_type == "run.completed",
                )
            )
            persisted_run = await session.get(AgentRun, run.id)
            persisted_task = await session.get(Task, task.id)
            assert terminal_events == 1
            assert persisted_run is not None
            assert persisted_run.status == RunStatus.COMPLETED.value
            assert persisted_task is not None
            assert persisted_task.state == TaskState.COMPLETED.value
        assert len(resources.publisher.events) == 2
    finally:
        await engine.dispose()
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                database_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        finally:
            await admin.close()


# --- coordination and memory wiring ---


async def test_accepted_work_request_is_lifted_and_replayed_from_the_commit(
    world: ProjectionWorld,
) -> None:
    created_task_id = str(new_uuid7())
    request_id = str(new_uuid7())
    await world.seed_step(
        statuses=[ToolCallStatus.COMPLETED.value, ToolCallStatus.COMPLETED.value],
        tool_names=["organization.respond_work_request", "organization.respond_work_request"],
        outputs=[
            {
                "work_request_id": request_id,
                "created_task_id": created_task_id,
                "agent_id": str(world.agent_id),
                "status": "accepted",
            },
            # A decline creates no task and therefore starts nothing.
            {"work_request_id": str(new_uuid7()), "status": "declined"},
        ],
    )
    ids = [str(stable_tool_invocation_id(world.run_id, 0, ordinal)) for ordinal in range(2)]

    first = await world.projections.commit_agent_step_activity(world.commit_params(ids=ids))
    replay = await world.projections.commit_agent_step_activity(world.commit_params(ids=ids))

    expected = [
        WorkRequestStart(
            work_request_id=request_id, task_id=created_task_id, agent_id=str(world.agent_id)
        )
    ]
    assert first.work_request_starts == expected
    assert replay.work_request_starts == expected
    payload = await world.load_commit_payload()
    assert payload["result"]["work_request_starts"] == [asdict(expected[0])]
    assert await world.count_events("agent.step.committed") == 1


class _StartRecorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.error = error

    async def start_workflow(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=kwargs.get("id"))


async def _finalize(world: ProjectionWorld, status: str) -> None:
    await world.projections.finalize_run_projection_activity(
        FinalizeInput(
            workspace_id=str(world.workspace_id),
            task_id=str(world.task_id),
            run_id=str(world.run_id),
            status=status,
            steps_used=1,
        )
    )


async def test_completed_run_starts_detached_memory_maintenance(world: ProjectionWorld) -> None:
    recorder = _StartRecorder()
    world.projections._temporal_client = cast(Any, recorder)

    await _finalize(world, RunStatus.COMPLETED.value)

    assert len(recorder.calls) == 1
    args, kwargs = recorder.calls[0]
    params = args[1]
    assert params.source_kind == "task_outcome"
    assert params.source_id == str(world.task_id)
    assert params.turn_marker == str(world.run_id)
    assert params.agent_id == str(world.agent_id)
    assert params.remember_enabled is False
    assert kwargs["id"] == f"memory-maintenance-task_outcome-{world.task_id}-{world.run_id}"
    assert (await world.load_run()).status == RunStatus.COMPLETED.value
    # A repeated finalization (retry) owns nothing and starts nothing more.
    await _finalize(world, RunStatus.COMPLETED.value)
    assert len(recorder.calls) == 1


async def test_memory_maintenance_is_best_effort(world: ProjectionWorld) -> None:
    recorder = _StartRecorder(error=RuntimeError("temporal unavailable"))
    world.projections._temporal_client = cast(Any, recorder)

    await _finalize(world, RunStatus.COMPLETED.value)

    assert len(recorder.calls) == 1
    assert (await world.load_run()).status == RunStatus.COMPLETED.value
    assert await world.count_events("run.completed") == 1


async def test_failed_run_does_not_start_memory_maintenance(world: ProjectionWorld) -> None:
    recorder = _StartRecorder()
    world.projections._temporal_client = cast(Any, recorder)

    await _finalize(world, RunStatus.FAILED.value)

    assert recorder.calls == []


async def test_failed_call_observation_carries_the_connector_hint(world: ProjectionWorld) -> None:
    """A pre-effect failure's static hint (stored on the row by the gateway)
    reaches the model's observation so it can correct the call."""
    await world.seed_step(
        statuses=[ToolCallStatus.FAILED.value],
        outputs=[{"error": "database_sql_not_allowed", "hint": "use one SELECT with a LIMIT"}],
    )
    await world.projections.commit_agent_step_activity(world.commit_params())
    async with world.sessions() as session:
        results = [
            message.content_json
            for message in await session.scalars(
                select(Message).where(
                    Message.run_id == world.run_id, Message.message_type == "tool_result"
                )
            )
        ]
    assert len(results) == 1
    observation = json.loads(str(results[0]["result"]))
    # The seeded row carries no error_code, so the status names the error.
    assert observation["error"] == "failed"
    assert observation["detail"] == "tool execution failed: use one SELECT with a LIMIT"


async def _final_messages(world: ProjectionWorld) -> list[Message]:
    async with world.sessions() as session:
        return list(
            await session.scalars(
                select(Message)
                .where(Message.run_id == world.run_id)
                .order_by(Message.created_at, Message.id)
            )
        )


async def test_a_final_answer_is_persisted_as_the_agent_saying_it(
    world: ProjectionWorld,
) -> None:
    await world.seed_step(statuses=[], manifest_count=0, completion="Connie is on QA duty.")

    await world.projections.commit_agent_step_activity(world.commit_params(ids=[]))

    messages = await _final_messages(world)
    assert [(m.sender_type, m.message_type) for m in messages] == [(SenderType.AGENT.value, "text")]
    assert messages[0].content_json["text"] == "Connie is on QA duty."


@pytest.mark.parametrize("completion", ["", "   ", "\n\t  \n"])
async def test_an_empty_completion_never_becomes_an_empty_agent_bubble(
    world: ProjectionWorld,
    completion: str,
) -> None:
    """A run that ends with no text must not leave a blank bubble behind —
    nor silence: the reader gets an honest system note instead."""
    await world.seed_step(statuses=[], manifest_count=0, completion=completion)

    await world.projections.commit_agent_step_activity(world.commit_params(ids=[]))

    messages = await _final_messages(world)
    assert [(m.sender_type, m.message_type) for m in messages] == [
        (SenderType.SYSTEM.value, "note")
    ]
    note = messages[0]
    assert note.sender_id is None
    assert note.visibility == MessageVisibility.VISIBLE.value
    assert note.content_json["text"] == (
        "Projector could not complete this request and did not leave a reply."
    )
    assert note.content_json["reason"] == "empty_completion"


async def test_the_empty_completion_note_carries_a_reported_summary(
    world: ProjectionWorld,
) -> None:
    async with world.sessions() as session:
        task = await session.get(Task, world.task_id)
        assert task is not None
        task.metadata_json = {"reported_result": {"summary": "Looked up Connie's record."}}
        flag_modified(task, "metadata_json")
        await session.commit()
    await world.seed_step(statuses=[], manifest_count=0, completion="")

    await world.projections.commit_agent_step_activity(world.commit_params(ids=[]))

    messages = await _final_messages(world)
    assert messages[0].content_json["text"] == (
        "Projector could not complete this request and did not leave a reply. "
        "Its reported result: Looked up Connie's record."
    )
