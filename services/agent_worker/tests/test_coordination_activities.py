"""Coordination activity helpers: lifting accepted requests into the
workflow contract, roster/manager prompt context, and the finalize activity
against SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_agent_worker.coordination_activities import (
    CoordinationActivities,
    manager_context,
    organization_context,
    work_request_start_from_output,
)
from jhin_db.base import Base
from jhin_db.models import Agent, Task, WorkRequest, Workspace
from jhin_domain import TaskState, WorkRequestStatus, new_uuid7
from jhin_workflows.work_request_task import FinalizeWorkRequestInput


@pytest.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class FakeResources:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory


def test_work_request_start_lifting() -> None:
    assert work_request_start_from_output(None) is None
    assert work_request_start_from_output({"work_request_id": "r", "created_task_id": None}) is None
    start = work_request_start_from_output(
        {"work_request_id": "r", "created_task_id": "t", "status": "accepted"}
    )
    assert start is not None and start.task_id == "t" and start.work_request_id == "r"


async def test_context_blocks_and_finalize_activity(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        cto = Agent(workspace_id=workspace.id, name="CTO", slug="cto")
        session.add(cto)
        await session.flush()
        swe = Agent(workspace_id=workspace.id, name="SWE", slug="swe", manager_agent_id=cto.id)
        writer = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        session.add_all([swe, writer])
        await session.flush()
        requester_task = Task(
            workspace_id=workspace.id,
            title="Parent",
            state=TaskState.RUNNING.value,
            assigned_agent_id=swe.id,
            correlation_id=new_uuid7(),
        )
        session.add(requester_task)
        await session.flush()
        created = Task(
            workspace_id=workspace.id,
            title="Docs",
            state=TaskState.COMPLETED.value,
            assigned_agent_id=writer.id,
            correlation_id=new_uuid7(),
            metadata_json={"reported_result": {"summary": "Docs done", "status": "completed"}},
        )
        session.add(created)
        await session.flush()
        request = WorkRequest(
            workspace_id=workspace.id,
            requester_agent_id=swe.id,
            requester_task_id=requester_task.id,
            target_agent_id=writer.id,
            title="Docs",
            idempotency_key="k",
            status=WorkRequestStatus.ACCEPTED.value,
            created_task_id=created.id,
            metadata_json={"requester_agent_name": "SWE", "target_agent_name": "Writer"},
        )
        session.add(request)
        await session.commit()

        roster = await organization_context(session, workspace.id, swe.id)
        assert "Your manager:" in roster and "CTO" in roster
        assert await manager_context(session, workspace.id, writer.id) == ""
        manager_block = await manager_context(session, workspace.id, cto.id)
        assert "SWE" in manager_block
        workspace_id, request_id, task_id = workspace.id, request.id, created.id

    activities = CoordinationActivities(FakeResources(maker))  # type: ignore[arg-type]
    params = FinalizeWorkRequestInput(
        workspace_id=str(workspace_id),
        work_request_id=str(request_id),
        task_id=str(task_id),
        run_status="completed",
    )
    assert await activities.finalize_work_request_activity(params) == "completed"
    assert await activities.finalize_work_request_activity(params) == "completed"
    async with maker() as session:
        row: Any = await session.get(WorkRequest, request_id)
        assert row.status == WorkRequestStatus.COMPLETED.value
