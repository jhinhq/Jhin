"""Pausing a task has to reach the database.

The workflow holds the pause in memory and only writes the task row when the
run reaches a terminal state, so before this the chat showed "Working…"
forever and never offered Resume (the control is gated on the paused state).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.tasks import service as tasks_service
from jhin_db.models import Agent, Task
from jhin_domain import TaskState, new_uuid7


class FakeHandle:
    def __init__(self, client: FakeTemporal, workflow_id: str) -> None:
        self._client = client
        self._workflow_id = workflow_id

    async def signal(self, name: str, *args: Any) -> None:
        self._client.signals.append((self._workflow_id, name, args))


class FakeTemporal:
    def __init__(self) -> None:
        self.signals: list[tuple[str, str, tuple[Any, ...]]] = []

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(self, workflow_id)


async def make_task(
    session: AsyncSession, ctx: WorkspaceContext, *, state: str
) -> tuple[Agent, Task]:
    agent = Agent(workspace_id=ctx.workspace_id, name="Atlas", slug="atlas", role_title="CTO")
    session.add(agent)
    await session.flush()
    task = Task(
        workspace_id=ctx.workspace_id,
        title="Plan the roadmap",
        description="Plan the roadmap",
        state=state,
        assigned_agent_id=agent.id,
        temporal_workflow_id="wf-1",
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    return agent, task


async def signal(
    session: AsyncSession,
    ctx: WorkspaceContext,
    temporal: FakeTemporal,
    task_id: UUID,
    *,
    signal: str,
    new_state: str,
    from_states: tuple[str, ...],
) -> Task:
    return await tasks_service.signal_task(
        session,
        ctx,
        temporal,  # type: ignore[arg-type]
        task_id,
        signal=signal,
        action=f"task.{signal}d",
        new_state=new_state,
        from_states=from_states,
        request_id=new_uuid7(),
        ip_hash="h",
    )


async def test_pause_records_paused_and_resume_records_running(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    temporal = FakeTemporal()
    _, task = await make_task(session, admin_ctx, state=TaskState.RUNNING.value)

    paused = await signal(
        session,
        admin_ctx,
        temporal,
        task.id,
        signal="pause",
        new_state=TaskState.PAUSED.value,
        from_states=(TaskState.QUEUED.value, TaskState.RUNNING.value),
    )
    assert paused.state == TaskState.PAUSED.value
    assert temporal.signals == [("wf-1", "pause", ())]

    resumed = await signal(
        session,
        admin_ctx,
        temporal,
        task.id,
        signal="resume",
        new_state=TaskState.RUNNING.value,
        from_states=(TaskState.PAUSED.value,),
    )
    assert resumed.state == TaskState.RUNNING.value


async def test_state_is_only_written_from_the_state_it_was_read_in(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    # The write is conditional on the state the transition expects, so a run
    # that moved on between the signal and this write keeps its real outcome
    # instead of being rewritten. Resuming a task that is not paused is the
    # reachable case: the signal goes through, the row is left alone.
    temporal = FakeTemporal()
    _, task = await make_task(session, admin_ctx, state=TaskState.RUNNING.value)

    result = await signal(
        session,
        admin_ctx,
        temporal,
        task.id,
        signal="resume",
        new_state=TaskState.RUNNING.value,
        from_states=(TaskState.PAUSED.value,),
    )
    assert result.state == TaskState.RUNNING.value
    assert temporal.signals == [("wf-1", "resume", ())]


async def test_signalling_a_finished_task_still_conflicts(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    temporal = FakeTemporal()
    _, task = await make_task(session, admin_ctx, state=TaskState.COMPLETED.value)
    with pytest.raises(HTTPException) as excinfo:
        await signal(
            session,
            admin_ctx,
            temporal,
            task.id,
            signal="pause",
            new_state=TaskState.PAUSED.value,
            from_states=(TaskState.QUEUED.value, TaskState.RUNNING.value),
        )
    assert excinfo.value.status_code == 409
