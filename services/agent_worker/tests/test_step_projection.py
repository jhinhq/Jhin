"""Agent projections rebuild transcript and timeline only from durable IDs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.projections import AgentProjectionActivities
from jhin_agent_worker.reasoning import AgentStepReasoningRecord, AgentStepUsage
from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, Message, RunEvent, Task, ToolCall, Workspace
from jhin_domain import RunStatus, ToolCallStatus, new_uuid7
from jhin_tools import stable_tool_invocation_id
from jhin_workflows.agent_task.shared import CommitAgentStepInput


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


@dataclass
class ProjectionWorld:
    projections: AgentProjectionActivities
    sessions: async_sessionmaker[AsyncSession]
    workspace_id: Any
    agent_id: Any
    task_id: Any
    run_id: Any

    def commit_params(self, *, ids: list[str] | None = None) -> CommitAgentStepInput:
        return CommitAgentStepInput(
            workspace_id=str(self.workspace_id),
            task_id=str(self.task_id),
            run_id=str(self.run_id),
            agent_id=str(self.agent_id),
            step_index=0,
            gateway_tool_call_ids=(
                ids
                if ids is not None
                else [str(stable_tool_invocation_id(self.run_id, 0, 0))]
            ),
        )

    async def seed_manifest_and_tool_call(self, *, status: str) -> None:
        call_id = stable_tool_invocation_id(self.run_id, 0, 0)
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
                                "count": 2,
                                "calls": [
                                    {
                                        "ordinal": 0,
                                        "lossless": True,
                                        "tool_name": "system.echo",
                                        "arguments_json": '{"value":"first"}',
                                    },
                                    {
                                        "ordinal": 1,
                                        "lossless": True,
                                        "tool_name": "system.echo",
                                        "arguments_json": '{"value":"must-not-project"}',
                                    },
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
                            completion_sanitized="Calling a tool.",
                            model="projection-test",
                            finish_reason="tool_calls",
                            provider_request_id="private-provider-request",
                            provider_call_ids=(
                                "private-provider-call-1",
                                "private-provider-call-2",
                            ),
                            transitions=(
                                {"node": "load_context", "detail": "context loaded"},
                                {"node": "reason", "detail": "model responded"},
                                {"node": "call_tool", "detail": "tool requested"},
                            ),
                            done=False,
                            usage=AgentStepUsage(
                                input_tokens=7,
                                output_tokens=3,
                                cached_tokens=1,
                                cost_micros=10,
                            ),
                            latency_ms=4,
                        ).to_payload(),
                    ),
                    ToolCall(
                        id=call_id,
                        workspace_id=self.workspace_id,
                        run_id=self.run_id,
                        agent_id=self.agent_id,
                        tool_name="system.echo",
                        sanitized_input_json={"value": "first"},
                        sanitized_output_json={},
                        status=status,
                        error_code=(
                            "execution_outcome_unknown"
                            if status == ToolCallStatus.EXECUTION_UNKNOWN.value
                            else None
                        ),
                    ),
                ]
            )
            await session.commit()

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


async def test_projection_rejects_noncanonical_tool_id_prefix(world: ProjectionWorld) -> None:
    await world.seed_manifest_and_tool_call(status=ToolCallStatus.EXECUTION_UNKNOWN.value)

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(
            world.commit_params(ids=[str(new_uuid7())])
        )

    assert error.value.type == "tool_projection_binding_mismatch"
    assert await world.count_events("agent.step.committed") == 0
    assert await world.count_projection_messages() == 0
