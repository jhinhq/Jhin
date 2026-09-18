from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from jhin_db.models import Agent, Task
from jhin_db.models.schedule import ScheduleOccurrence
from jhin_tools.scheduling import (
    ScheduleCreate,
    ScheduleError,
    ScheduleUpdate,
    create_schedule,
    delete_schedule,
    update_schedule,
)


@pytest.fixture
async def schedule(session, admin_ctx):
    agent = Agent(workspace_id=admin_ctx.workspace_id, name="Writer", slug="writer")
    session.add(agent)
    await session.flush()
    data = ScheduleCreate(
        agent_id=agent.id,
        name="Daily draft",
        brief="Draft only; director reviews and publishes.",
        local_time="09:00",
        timezone="America/Los_Angeles",
        idempotency_key="daily",
    )
    row = await create_schedule(
        session, admin_ctx.workspace_id, data, now=datetime(2026, 9, 12, 10, tzinfo=UTC)
    )
    await session.commit()
    return row, data


async def test_schedule_crud_idempotency_and_stale_version(session, admin_ctx, schedule):
    row, data = schedule
    assert row.next_run_at == datetime(2026, 9, 12, 16, tzinfo=UTC)
    assert (await create_schedule(session, admin_ctx.workspace_id, data)).id == row.id
    with pytest.raises(ScheduleError, match="different"):
        await create_schedule(
            session, admin_ctx.workspace_id, data.model_copy(update={"brief": "Changed"})
        )
    await update_schedule(
        session, admin_ctx.workspace_id, row.id, ScheduleUpdate(expected_version=1, enabled=False)
    )
    assert row.next_run_at is None
    with pytest.raises(ScheduleError, match="reload"):
        await update_schedule(
            session,
            admin_ctx.workspace_id,
            row.id,
            ScheduleUpdate(expected_version=1, enabled=True),
        )
    await update_schedule(
        session, admin_ctx.workspace_id, row.id, ScheduleUpdate(expected_version=2, enabled=True)
    )
    assert row.next_run_at > datetime.now(UTC)
    await delete_schedule(session, admin_ctx.workspace_id, row.id, 3)
    assert row.deleted_at and not row.enabled


async def test_schedule_occurrence_claim_replays_task_and_preserves_brief(
    session, admin_ctx, schedule
):
    from jhin_tools.schedule_dispatch import claim_occurrence, finish_occurrence

    row, _ = schedule
    now = row.next_run_at
    first = await claim_occurrence(session, row.workspace_id, row.id, now=now)
    await session.commit()
    replay = await claim_occurrence(session, row.workspace_id, row.id, now=now)
    assert first.task_id == replay.task_id and first.occurrence_id == replay.occurrence_id
    assert await session.scalar(select(func.count()).select_from(Task)) == 1
    task = await session.get(Task, first.task_id)
    assert task.description == row.brief and task.metadata_json["origin"] == "schedule"
    assert task.temporal_workflow_id == f"task-{task.id}"
    await finish_occurrence(
        session, row.workspace_id, first.occurrence_id, "completed", now=now + timedelta(days=2)
    )
    await session.commit()
    following = await claim_occurrence(
        session, row.workspace_id, row.id, now=now + timedelta(days=2)
    )
    assert following.task_id is None
    assert row.next_run_at > now + timedelta(days=2)
    assert (
        await session.scalar(
            select(func.count())
            .select_from(ScheduleOccurrence)
            .where(ScheduleOccurrence.status == "skipped_overlap")
        )
        == 2
    )


async def test_paused_and_deleted_schedules_never_dispatch(session, admin_ctx, schedule):
    from jhin_tools.schedule_dispatch import claim_occurrence

    row, _ = schedule
    due = row.next_run_at
    await update_schedule(
        session, row.workspace_id, row.id, ScheduleUpdate(expected_version=1, enabled=False)
    )
    assert (await claim_occurrence(session, row.workspace_id, row.id, now=due)).task_id is None
    await delete_schedule(session, row.workspace_id, row.id, 2)
    assert (await claim_occurrence(session, row.workspace_id, row.id, now=due)).deleted
