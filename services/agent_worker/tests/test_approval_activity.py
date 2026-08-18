"""Approval activity repairs crash gaps without duplicating durable bundles."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from jhin_agent_worker.activities import AgentActivities
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, Approval, Message, RunEvent, Task, ToolCall, Workspace
from jhin_domain import (
    ApprovalStatus,
    MessageVisibility,
    RecipientType,
    RunStatus,
    SenderType,
    ToolCallStatus,
    new_uuid7,
)
from jhin_workflows.agent_task import ResolveApprovalInput, RunStepInput, StepResult


async def test_committed_step_retry_returns_durable_result_without_calling_model(
    activity_world,
) -> None:
    activities, resources, sessions = activity_world
    async with sessions() as session:
        workspace = Workspace(name="Step replay", slug=f"step-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Replay", slug="replay")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Replay a committed step",
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
            input_tokens=11,
            output_tokens=7,
            steps_used=3,
        )
        session.add(run)
        await session.flush()
        expected = StepResult(
            done=False,
            input_tokens=5,
            output_tokens=2,
            cached_tokens=1,
            cost_micros=9,
        )
        session.add(
            RunEvent(
                workspace_id=workspace.id,
                run_id=run.id,
                task_id=task.id,
                seq=4,
                event_type="agent.step.committed",
                payload_json={"step": 2, "result": asdict(expected)},
            )
        )
        snapshot = AgentExecutionSnapshot(
            agent_id=agent.id,
            workspace_id=workspace.id,
            name=agent.name,
            role_title="",
            system_prompt="",
            autonomy_level="supervised",
            team_id=None,
            team_name=None,
            manager_agent_id=None,
            manager_name=None,
            model_profile=ModelProfileSnapshot(
                profile_id=new_uuid7(),
                provider_id=new_uuid7(),
                provider_type="must-not-be-built",
                base_url=None,
                secret_id=None,
                model_name="never-called",
                display_name="Never called",
                input_cost_micros_per_million=None,
                output_cost_micros_per_million=None,
            ),
            temperature=None,
            max_output_tokens=None,
            run_limits=RunLimits(max_steps=5, max_run_minutes=5),
        )
        params = RunStepInput(
            workspace_id=str(workspace.id),
            task_id=str(task.id),
            run_id=str(run.id),
            agent_id=str(agent.id),
            snapshot_json=snapshot.model_dump_json(),
            step_index=2,
        )
        await session.commit()

    result = await ActivityEnvironment().run(activities.run_agent_step_activity, params)

    assert result == expected
    async with sessions() as session:
        persisted_run = await session.get(AgentRun, run.id)
        assert persisted_run is not None
        assert persisted_run.input_tokens == 11
        assert persisted_run.output_tokens == 7
        assert persisted_run.steps_used == 3
        assert await session.scalar(select(func.count(RunEvent.id))) == 1
    assert resources.publisher.events == []


class _Publisher:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.events.append(envelope)


class _Resources:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = sessions
        self.publisher = _Publisher()
        self.crypto = None


@pytest.fixture
async def activity_world() -> AsyncIterator[tuple[AgentActivities, _Resources, Any]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    resources = _Resources(sessions)
    yield AgentActivities(resources), resources, sessions  # type: ignore[arg-type]
    await engine.dispose()


async def _seed_terminal_approval(
    sessions: async_sessionmaker[AsyncSession],
    *,
    existing_bundle: bool,
    terminal_status: str = ToolCallStatus.COMPLETED.value,
) -> ResolveApprovalInput:
    async with sessions() as session:
        workspace = Workspace(name="Approval", slug=f"approval-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Approver", slug="approver")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Approved task",
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
        await session.flush()
        approval = Approval(
            workspace_id=workspace.id,
            task_id=task.id,
            run_id=run.id,
            requested_by_agent_id=agent.id,
            action_type="system.demo.destructive",
            action_payload_sanitized={
                "approval_format_version": 2,
                "workspace_id": str(workspace.id),
                "agent_id": str(agent.id),
                "run_id": str(run.id),
                "task_id": str(task.id),
                "tool_name": "system.demo.destructive",
                "capability": "system.demo.destructive",
                "risk": "destructive",
                "input": {"label": "once"},
                "connection_authorization_digest": None,
                "provider_call_id": "provider-call-1",
            },
            reason="approved",
            status=ApprovalStatus.APPROVED.value,
            requested_at=run.created_at,
        )
        session.add(approval)
        await session.flush()
        tool_call = ToolCall(
            workspace_id=workspace.id,
            run_id=run.id,
            agent_id=agent.id,
            tool_name="system.demo.destructive",
            sanitized_input_json={"label": "once"},
            sanitized_output_json=(
                {"marker": "already-executed"}
                if terminal_status == ToolCallStatus.COMPLETED.value
                else {}
            ),
            status=terminal_status,
            approval_id=approval.id,
            duration_ms=7,
            error_code=(
                "execution_outcome_unknown"
                if terminal_status == ToolCallStatus.EXECUTION_UNKNOWN.value
                else None
            ),
        )
        session.add(tool_call)
        await session.flush()
        if existing_bundle:
            session.add(
                Message(
                    workspace_id=workspace.id,
                    task_id=task.id,
                    run_id=run.id,
                    sender_type=SenderType.AGENT.value,
                    sender_id=agent.id,
                    recipient_type=RecipientType.TASK.value,
                    recipient_id=task.id,
                    message_type="tool_result",
                    content_json={
                        "tool_call_id": str(tool_call.id),
                        "provider_call_id": "provider-call-1",
                        "approval_id": str(approval.id),
                        "tool_name": "system.demo.destructive",
                        "status": (
                            "execution_unknown"
                            if terminal_status == ToolCallStatus.EXECUTION_UNKNOWN.value
                            else "executed"
                        ),
                        "result": (
                            '{"error":"execution_outcome_unknown"}'
                            if terminal_status == ToolCallStatus.EXECUTION_UNKNOWN.value
                            else '{"marker": "already-executed"}'
                        ),
                    },
                    visibility=MessageVisibility.INTERNAL.value,
                )
            )
            session.add(
                RunEvent(
                    workspace_id=workspace.id,
                    run_id=run.id,
                    task_id=task.id,
                    seq=0,
                    event_type="tool.call",
                    payload_json={"approval_id": str(approval.id)},
                )
            )
        await session.commit()
        return ResolveApprovalInput(
            workspace_id=str(workspace.id),
            task_id=str(task.id),
            run_id=str(run.id),
            agent_id=str(agent.id),
            approval_id=str(approval.id),
            decision="approved",
        )


async def test_terminal_retry_repairs_a_missing_outer_bundle(activity_world) -> None:
    activities, resources, sessions = activity_world
    params = await _seed_terminal_approval(sessions, existing_bundle=False)

    result = await ActivityEnvironment().run(activities.resolve_approval_activity, params)

    assert result.done is False
    async with sessions() as session:
        message_count = await session.scalar(select(func.count(Message.id)))
        event_count = await session.scalar(select(func.count(RunEvent.id)))
        assert message_count == 1
        assert event_count == 3
    assert len(resources.publisher.events) == 1


async def test_terminal_retry_does_not_duplicate_an_existing_outer_bundle(
    activity_world,
) -> None:
    activities, resources, sessions = activity_world
    params = await _seed_terminal_approval(sessions, existing_bundle=True)

    result = await ActivityEnvironment().run(activities.resolve_approval_activity, params)

    assert result.done is False
    async with sessions() as session:
        message_count = await session.scalar(select(func.count(Message.id)))
        event_count = await session.scalar(select(func.count(RunEvent.id)))
        assert message_count == 1
        assert event_count == 1
    assert resources.publisher.events == []


@pytest.mark.parametrize("existing_bundle", [False, True])
async def test_execution_unknown_approval_stops_and_replays_without_resuming(
    activity_world,
    existing_bundle: bool,
) -> None:
    activities, resources, sessions = activity_world
    params = await _seed_terminal_approval(
        sessions,
        existing_bundle=existing_bundle,
        terminal_status=ToolCallStatus.EXECUTION_UNKNOWN.value,
    )

    for _attempt in range(2):
        with pytest.raises(ApplicationError) as error:
            await ActivityEnvironment().run(activities.resolve_approval_activity, params)
        assert error.value.type == "tool_execution_unknown"
        assert error.value.non_retryable is True

    async with sessions() as session:
        run = await session.get(AgentRun, UUID(params.run_id))
        assert run is not None
        assert run.status == RunStatus.FAILED.value
        assert run.error_code == "tool_execution_unknown"
        assert run.error_message is not None
        tool_call = await session.scalar(
            select(ToolCall).where(ToolCall.approval_id == UUID(params.approval_id))
        )
        assert tool_call is not None
        assert str(tool_call.id) in run.error_message
        messages = list(await session.scalars(select(Message)))
        assert len(messages) == 1
        assert messages[0].content_json["tool_call_id"] == str(tool_call.id)
        assert messages[0].content_json["status"] == "execution_unknown"
        event_count = await session.scalar(select(func.count(RunEvent.id)))
        assert event_count == (1 if existing_bundle else 2)
    assert resources.publisher.events == []
