"""Monthly budget helpers (plan 15.5): settings parsing, calendar-month
spend summation, and the friendly denial messages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.budget import (
    budget_denial_message,
    format_budget_dollars,
    month_spend_micros,
    month_start_utc,
    workspace_budget_settings,
)
from jhin_db.models import Agent, AgentRun, Task, Workspace
from jhin_domain import RunStatus, TaskState, new_uuid7


def test_month_start_is_the_first_utc_instant() -> None:
    now = datetime(2026, 8, 21, 13, 37, 5, tzinfo=UTC)
    assert month_start_utc(now) == datetime(2026, 8, 1, tzinfo=UTC)


def test_workspace_budget_settings_parse_defensively() -> None:
    assert workspace_budget_settings(None) == (None, 0.8)
    assert workspace_budget_settings({}) == (None, 0.8)
    assert workspace_budget_settings({"budget": "nope"}) == (None, 0.8)
    assert workspace_budget_settings({"budget": {"monthly_budget_micros": -5}}) == (None, 0.8)
    assert workspace_budget_settings(
        {"budget": {"monthly_budget_micros": 2_000_000, "warning_threshold": 0.5}}
    ) == (2_000_000, 0.5)
    assert workspace_budget_settings(
        {"budget": {"monthly_budget_micros": 0, "warning_threshold": 9}}
    ) == (0, 1.0)


def test_format_budget_dollars() -> None:
    assert format_budget_dollars(5_000_000) == "$5.00"
    assert format_budget_dollars(10_000) == "$0.01"
    assert format_budget_dollars(0) == "$0.00"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


async def _world(session: AsyncSession) -> tuple[Workspace, Agent, Agent, Task]:
    workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
    session.add(workspace)
    await session.flush()
    bisby = Agent(workspace_id=workspace.id, name="Bisby", slug="bisby")
    other = Agent(workspace_id=workspace.id, name="Other", slug="other")
    session.add_all([bisby, other])
    await session.flush()
    task = Task(
        workspace_id=workspace.id,
        title="T",
        state=TaskState.COMPLETED.value,
        assigned_agent_id=bisby.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    return workspace, bisby, other, task


def _run(
    workspace: Workspace, agent: Agent, task: Task, cost: int, created_at: datetime | None = None
) -> AgentRun:
    run = AgentRun(
        workspace_id=workspace.id,
        agent_id=agent.id,
        task_id=task.id,
        status=RunStatus.COMPLETED.value,
        estimated_cost_micros=cost,
    )
    if created_at is not None:
        run.created_at = created_at
    return run


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),  # month boundary: first second
        datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),  # month boundary: last second
    ],
)
async def test_month_spend_counts_only_this_month(session: AsyncSession, now: datetime) -> None:
    workspace, bisby, other, task = await _world(session)
    start = month_start_utc(now)
    session.add_all(
        [
            _run(workspace, bisby, task, 1_000, created_at=start),
            _run(workspace, bisby, task, 2_000, created_at=now),
            _run(workspace, other, task, 40_000, created_at=now),
            _run(workspace, bisby, task, 999_999, created_at=start - timedelta(seconds=1)),
        ]
    )
    await session.commit()

    assert await month_spend_micros(session, workspace.id, now=now) == 43_000
    assert await month_spend_micros(session, workspace.id, agent_id=bisby.id, now=now) == 3_000


async def test_denial_message_prefers_the_agent_budget(session: AsyncSession) -> None:
    workspace, bisby, _other, task = await _world(session)
    session.add(_run(workspace, bisby, task, 5_000_000))
    await session.commit()

    message = await budget_denial_message(
        session,
        workspace_id=workspace.id,
        agent_id=bisby.id,
        agent_name=bisby.name,
        agent_budget_cents=500,
        workspace_settings_json={"budget": {"monthly_budget_micros": 1_000_000}},
    )
    assert message == (
        "Bisby reached its monthly budget ($5.00) — raise it in the agent's "
        "settings or wait for next month."
    )


async def test_denial_message_falls_back_to_the_workspace_budget(session: AsyncSession) -> None:
    workspace, bisby, other, task = await _world(session)
    session.add(_run(workspace, other, task, 2_000_000))  # someone else spent it
    await session.commit()

    message = await budget_denial_message(
        session,
        workspace_id=workspace.id,
        agent_id=bisby.id,
        agent_name=bisby.name,
        agent_budget_cents=500,  # Bisby's own spend is 0
        workspace_settings_json={"budget": {"monthly_budget_micros": 1_500_000}},
    )
    assert message == (
        "This workspace reached its monthly model budget ($1.50) — raise it "
        "in workspace Settings or wait for next month."
    )


async def test_no_denial_with_headroom_or_without_budgets(session: AsyncSession) -> None:
    workspace, bisby, _other, task = await _world(session)
    session.add(_run(workspace, bisby, task, 4_990_000))
    await session.commit()

    with_headroom = await budget_denial_message(
        session,
        workspace_id=workspace.id,
        agent_id=bisby.id,
        agent_name=bisby.name,
        agent_budget_cents=500,
        workspace_settings_json=None,
    )
    assert with_headroom is None

    unlimited = await budget_denial_message(
        session,
        workspace_id=workspace.id,
        agent_id=bisby.id,
        agent_name=bisby.name,
        agent_budget_cents=None,
        workspace_settings_json={},
    )
    assert unlimited is None
