"""Rolling this release back without leaving the old one a status it hates.

``claimed`` is a new value in a plain ``varchar`` column, so neither the
deploy nor the rollback needs a migration — but the previous release meets a
row in ``claimed`` with ``GatewayStateError("... has unexpected status
'claimed'")``, which the activity turns into a non-retryable failure and the
run dies of. One command closes them, and this is what it promises: it closes
every one, as a *failure* naming what happened, and it touches nothing else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, AuditEvent, Task, ToolCall, Workspace
from jhin_domain import RunStatus, ToolCallStatus, new_uuid7
from jhin_tool_worker.rollback import ROLLED_BACK_CODE, close_claimed_tool_calls

pytestmark = pytest.mark.anyio


class _World:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], ids: dict[str, UUID]) -> None:
        self.sessions = sessions
        self.ids = ids

    async def call(self, status: str, *, tool_name: str = "cli.file.list") -> UUID:
        call_id = new_uuid7()
        async with self.sessions() as session:
            session.add(
                ToolCall(
                    id=call_id,
                    workspace_id=self.ids["workspace"],
                    agent_id=self.ids["agent"],
                    run_id=self.ids["run"],
                    tool_name=tool_name,
                    status=status,
                    sanitized_input_json={"path": ""},
                    sanitized_output_json={},
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()
        return call_id

    async def row(self, call_id: UUID) -> ToolCall:
        async with self.sessions() as session:
            row = await session.get(ToolCall, call_id)
            assert row is not None
            return row

    async def audit(self, call_id: UUID) -> list[AuditEvent]:
        async with self.sessions() as session:
            return list(
                await session.scalars(select(AuditEvent).where(AuditEvent.target_id == call_id))
            )


@pytest.fixture
async def world(tmp_path: Any) -> AsyncIterator[_World]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as setup:
        workspace = Workspace(name="Rollback", slug=f"rollback-{new_uuid7().hex[:8]}")
        setup.add(workspace)
        await setup.flush()
        agent = Agent(workspace_id=workspace.id, name="Roller", slug="roller")
        setup.add(agent)
        await setup.flush()
        task = Task(workspace_id=workspace.id, title="Rollback", correlation_id=new_uuid7())
        setup.add(task)
        await setup.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.RUNNING.value,
        )
        setup.add(run)
        await setup.commit()
        ids = {
            "workspace": workspace.id,
            "agent": agent.id,
            "task": task.id,
            "run": run.id,
        }
    yield _World(sessions, ids)
    await engine.dispose()


async def test_a_claimed_call_is_closed_as_a_failure_the_old_release_can_read(
    world: _World,
) -> None:
    """Not ``executing``, which is what the old release wrote at this moment.

    ``claimed`` means something exact — the dispatch compare-and-set commits
    before the executor is entered, so nothing ran — and that fact does not
    stop being true because the code that could read it was removed. Writing
    ``executing`` would throw the proof away and hand an operator a pile of
    "manual reconciliation is required" for calls that provably did nothing.
    """
    call_id = await world.call(ToolCallStatus.CLAIMED.value)

    report = await close_claimed_tool_calls(world.sessions)

    row = await world.row(call_id)
    assert report.closed == (call_id,)
    assert (row.status, row.error_code) == (ToolCallStatus.FAILED.value, ROLLED_BACK_CODE)
    assert row.completed_at is not None
    assert "never started" in row.sanitized_output_json["hint"]
    assert "nothing ran" in row.sanitized_output_json["detail"]
    # The trail says what the closure rested on, not just that it happened.
    [event] = await world.audit(call_id)
    assert event.action == "tool.call.failed"
    assert event.metadata_json["code"] == ROLLED_BACK_CODE
    assert "no executor ran" in event.metadata_json["evidence"]


@pytest.mark.parametrize(
    "status",
    [
        ToolCallStatus.EXECUTING.value,
        ToolCallStatus.EXECUTION_UNKNOWN.value,
        ToolCallStatus.COMPLETED.value,
        ToolCallStatus.FAILED.value,
        ToolCallStatus.PENDING_APPROVAL.value,
        ToolCallStatus.PENDING_REVIEW.value,
    ],
)
async def test_no_other_status_is_touched(world: _World, status: str) -> None:
    """Every one of these is a value the old release already understands, and
    a rollback that rewrote them would be inventing outcomes."""
    call_id = await world.call(status)

    report = await close_claimed_tool_calls(world.sessions)

    assert report.closed == ()
    assert (await world.row(call_id)).status == status
    assert await world.audit(call_id) == []


async def test_a_dry_run_reports_without_writing(world: _World) -> None:
    call_id = await world.call(ToolCallStatus.CLAIMED.value)

    report = await close_claimed_tool_calls(world.sessions, dry_run=True)

    assert (report.closed, report.dry_run) == ((call_id,), True)
    assert "would close" in report.describe()
    assert (await world.row(call_id)).status == ToolCallStatus.CLAIMED.value


async def test_running_it_twice_is_the_same_as_running_it_once(world: _World) -> None:
    """An operator under time pressure should not have to remember whether
    they already ran it."""
    await world.call(ToolCallStatus.CLAIMED.value)

    first = await close_claimed_tool_calls(world.sessions)
    second = await close_claimed_tool_calls(world.sessions)

    assert len(first.closed) == 1
    assert second.closed == ()
    assert "nothing to close" in second.describe()


async def test_an_empty_table_is_a_success_not_a_silence(world: _World) -> None:
    report = await close_claimed_tool_calls(world.sessions)
    assert report.closed == ()
    assert "nothing to close" in report.describe()
