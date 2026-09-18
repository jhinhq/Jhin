"""Real PostgreSQL schedule claims; fixture-scoped and safe beside other test work."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.models import Agent, Task, Workspace
from jhin_db.models.schedule import AgentSchedule, ScheduleOccurrence
from jhin_domain import new_uuid7
from jhin_tools.schedule_dispatch import claim_occurrence, finish_occurrence
from jhin_tools.scheduling import ScheduleCreate, ScheduleUpdate, create_schedule, update_schedule

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated TEST_DATABASE_URL required")


async def test_postgres_concurrent_create_claim_and_restart_use_one_task():
    engine = create_async_engine(URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = new_uuid7()
    try:
        async with sessions() as db:
            workspace = Workspace(
                id=workspace_id, name="Schedule regression", slug=f"schedule-{workspace_id.hex}"
            )
            db.add(workspace)
            await db.flush()
            agent = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
            db.add(agent)
            await db.commit()
            agent_id = agent.id
        now = datetime(2031, 3, 1, tzinfo=UTC)
        fields = ScheduleCreate(
            agent_id=agent_id,
            name="Daily draft",
            brief="Draft only; director publishes.",
            local_time="09:00",
            timezone="America/Los_Angeles",
            idempotency_key="same",
        )

        async def create():
            async with sessions() as db:
                row = await create_schedule(db, workspace_id, fields, now=now)
                await db.commit()
                return row.id, row.next_run_at

        first, duplicate = await asyncio.gather(create(), create())
        assert first == duplicate
        schedule_id, due = first

        async def claim():
            async with sessions() as db:
                result = await claim_occurrence(db, workspace_id, schedule_id, now=due)
                await db.commit()
                return result

        claims = await asyncio.gather(claim(), claim())
        assert claims[0].task_id == claims[1].task_id
        assert claims[0].occurrence_id == claims[1].occurrence_id
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count()).select_from(Task).where(Task.workspace_id == workspace_id)
                )
                == 1
            )
            task = await db.get(Task, claims[0].task_id)
            assert task.temporal_workflow_id == f"task-{task.id}"
            assert task.description == fields.brief
            await update_schedule(
                db,
                workspace_id,
                schedule_id,
                ScheduleUpdate(
                    expected_version=1, timezone="Europe/London", brief="A new standing brief"
                ),
                now=due,
            )
            await db.commit()
        # A restarted scheduler finds the existing occurrence, original brief, and exact task.
        restarted = await claim()
        assert restarted.task_id == claims[0].task_id and restarted.brief == fields.brief
        async with sessions() as db:
            await finish_occurrence(
                db, workspace_id, restarted.occurrence_id, "completed", now=due + timedelta(hours=1)
            )
            await db.commit()
        async with sessions() as db:
            row = await db.get(AgentSchedule, schedule_id)
            assert row.timezone == "Europe/London" and row.brief == "A new standing brief"
            assert row.next_run_at == datetime(2031, 3, 2, 9, tzinfo=UTC)
            occurrence = await db.get(ScheduleOccurrence, restarted.occurrence_id)
            assert occurrence.status == "completed"
    finally:
        async with sessions() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await db.commit()
        await engine.dispose()
