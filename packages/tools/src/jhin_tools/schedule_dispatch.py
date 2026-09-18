"""Transactional occurrence claiming, safe to repeat after worker interruption."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, Task
from jhin_db.models.schedule import ScheduleOccurrence
from jhin_domain import new_uuid7
from jhin_tools.scheduling import ScheduleError, get_schedule, next_occurrence


@dataclass
class ClaimedOccurrence:
    task_id: UUID | None = None
    occurrence_id: UUID | None = None
    agent_id: UUID | None = None
    brief: str = ""
    deleted: bool = False
    wait_seconds: float = 30


async def claim_occurrence(
    db: AsyncSession, workspace_id: UUID, schedule_id: UUID, *, now: datetime | None = None
) -> ClaimedOccurrence:
    now = now or datetime.now(UTC)
    try:
        schedule = await get_schedule(db, workspace_id, schedule_id, lock=True)
    except ScheduleError:
        return ClaimedOccurrence(deleted=True)
    active = await db.scalar(
        select(ScheduleOccurrence)
        .where(
            ScheduleOccurrence.schedule_id == schedule.id, ScheduleOccurrence.status == "running"
        )
        .order_by(ScheduleOccurrence.scheduled_for)
        .limit(1)
    )
    if active is not None:
        task = await db.get(Task, active.task_id) if active.task_id else None
        if task is None or task.state in {"completed", "failed", "cancelled"}:
            await finish_occurrence(
                db, workspace_id, active.id, task.state if task else "failed", now=now
            )
        else:
            return ClaimedOccurrence(task.id, active.id, task.assigned_agent_id, task.description)
    if schedule.deleted_at:
        return ClaimedOccurrence(deleted=True)
    if not schedule.enabled or schedule.next_run_at is None:
        return ClaimedOccurrence()
    if schedule.next_run_at > now:
        return ClaimedOccurrence(
            wait_seconds=min(60, max(1, (schedule.next_run_at - now).total_seconds()))
        )
    due = schedule.next_run_at
    owner = await db.get(Agent, schedule.agent_id)
    occurrence = ScheduleOccurrence(
        id=new_uuid7(),
        workspace_id=workspace_id,
        schedule_id=schedule.id,
        scheduled_for=due,
        started_at=now,
    )
    schedule.next_run_at = next_occurrence(
        now, schedule.local_time, schedule.timezone, schedule.weekdays
    )
    schedule.last_run_at = due
    db.add(occurrence)
    if owner is None or owner.status != "active" or owner.availability != "available":
        occurrence.status = "skipped_unavailable"
        occurrence.finished_at = now
        schedule.last_status = occurrence.status
        await db.flush()
        return ClaimedOccurrence()
    task = Task(
        id=new_uuid7(),
        workspace_id=workspace_id,
        title=schedule.name,
        description=schedule.brief,
        state="queued",
        assigned_agent_id=owner.id,
        correlation_id=new_uuid7(),
        metadata_json={
            "origin": "schedule",
            "schedule_id": str(schedule.id),
            "schedule_occurrence_id": str(occurrence.id),
            "schedule_version": schedule.version,
            "standing_brief": schedule.brief,
            "scheduled_for": due.isoformat(),
            "timezone": schedule.timezone,
        },
    )
    task.temporal_workflow_id = f"task-{task.id}"
    db.add(task)
    # No ORM relationship orders this FK; commit the task row before assigning it.
    await db.flush()
    occurrence.task_id = task.id
    occurrence.status = "running"
    schedule.last_status = "running"
    await db.flush()
    return ClaimedOccurrence(task.id, occurrence.id, owner.id, task.description)


async def finish_occurrence(
    db: AsyncSession,
    workspace_id: UUID,
    occurrence_id: UUID,
    status: str,
    *,
    now: datetime | None = None,
) -> None:
    occurrence = await db.scalar(
        select(ScheduleOccurrence).where(
            ScheduleOccurrence.id == occurrence_id, ScheduleOccurrence.workspace_id == workspace_id
        )
    )
    if occurrence is None:
        return
    schedule = await get_schedule(db, workspace_id, occurrence.schedule_id, lock=True)
    await db.refresh(occurrence)
    if occurrence.status != "running":
        return
    now = now or datetime.now(UTC)
    occurrence.status = status if status in {"completed", "cancelled"} else "failed"
    occurrence.finished_at = now
    schedule.last_status = occurrence.status
    # Preserve an explicit edit/pause made while this task ran. Upcoming times
    # within its occupied interval cannot launch another task under skip policy.
    skipped = 0
    while (
        schedule.enabled
        and schedule.next_run_at is not None
        and schedule.next_run_at <= now
        and skipped < 370
    ):
        due = schedule.next_run_at
        db.add(
            ScheduleOccurrence(
                id=new_uuid7(),
                workspace_id=workspace_id,
                schedule_id=schedule.id,
                scheduled_for=due,
                status="skipped_overlap",
                finished_at=now,
            )
        )
        schedule.next_run_at = next_occurrence(
            due, schedule.local_time, schedule.timezone, schedule.weekdays
        )
        skipped += 1
    # Bounded recovery after an exceptionally long outage; no catch-up storm.
    if schedule.enabled and schedule.next_run_at is not None and schedule.next_run_at <= now:
        schedule.next_run_at = next_occurrence(
            now, schedule.local_time, schedule.timezone, schedule.weekdays
        )
    await db.flush()
