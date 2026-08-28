"""Pausing a task has to be honest about whether the run actually stopped.

The workflow observes a pause between steps. A run with no further step
boundary -- a single long generation, which is exactly when a person reaches
for Pause -- never reaches one. Writing "paused" when the signal was accepted
therefore showed a stopped task and a Resume button for a run that carried on
and billed, so the state is now written by the workflow when it genuinely
parks.
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


async def make_task(session: AsyncSession, ctx: WorkspaceContext, *, state: str) -> Task:
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
    return task


async def signal(
    session: AsyncSession,
    ctx: WorkspaceContext,
    temporal: FakeTemporal,
    task_id: UUID,
    *,
    name: str,
) -> Task:
    return await tasks_service.signal_task(
        session,
        ctx,
        temporal,  # type: ignore[arg-type]
        task_id,
        signal=name,
        action=f"task.{name}d",
        request_id=new_uuid7(),
        ip_hash="h",
    )


async def test_pause_is_delivered_but_not_claimed(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    temporal = FakeTemporal()
    task = await make_task(session, admin_ctx, state=TaskState.RUNNING.value)

    result = await signal(session, admin_ctx, temporal, task.id, name="pause")

    assert temporal.signals == [("wf-1", "pause", ())]
    # Still running, because it still is: the run stops at its next step, and
    # a run that never reaches one never stops at all.
    assert result.state == TaskState.RUNNING.value


async def test_resume_is_delivered_but_not_claimed(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    temporal = FakeTemporal()
    task = await make_task(session, admin_ctx, state=TaskState.PAUSED.value)

    result = await signal(session, admin_ctx, temporal, task.id, name="resume")

    assert temporal.signals == [("wf-1", "resume", ())]
    assert result.state == TaskState.PAUSED.value


async def test_signalling_a_finished_task_still_conflicts(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    temporal = FakeTemporal()
    task = await make_task(session, admin_ctx, state=TaskState.COMPLETED.value)
    with pytest.raises(HTTPException) as excinfo:
        await signal(session, admin_ctx, temporal, task.id, name="pause")
    assert excinfo.value.status_code == 409
