"""Schedule activities plus restart-safe reconciliation of scheduler workflows."""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from jhin_agent_worker.resources import Resources
from jhin_db.models.schedule import AgentSchedule
from jhin_observability import get_logger
from jhin_tools.schedule_dispatch import claim_occurrence, finish_occurrence
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.schedules.shared import (
    ScheduleClaim,
    ScheduleFinish,
    ScheduleTick,
    schedule_workflow_id,
)

logger = get_logger(__name__)


class ScheduleActivities:
    def __init__(self, resources: Resources):
        self.resources = resources

    @activity.defn(name="claim_schedule_occurrence")
    async def claim(self, params: ScheduleTick) -> ScheduleClaim:
        async with self.resources.session_factory() as db:
            result = await claim_occurrence(db, UUID(params.workspace_id), UUID(params.schedule_id))
            await db.commit()
            return ScheduleClaim(
                task_id=str(result.task_id or ""),
                occurrence_id=str(result.occurrence_id or ""),
                agent_id=str(result.agent_id or ""),
                brief=result.brief,
                deleted=result.deleted,
                wait_seconds=result.wait_seconds,
            )

    @activity.defn(name="finish_schedule_occurrence")
    async def finish(self, params: ScheduleFinish) -> None:
        async with self.resources.session_factory() as db:
            await finish_occurrence(
                db, UUID(params.workspace_id), UUID(params.occurrence_id), params.status
            )
            await db.commit()


async def reconcile_schedules(resources: Resources, client: Client) -> int:
    """Bounded keyset scan. Temporal workflow identity is the second dedupe layer."""
    count = 0
    cursor = None
    while True:
        async with resources.session_factory() as db:
            query = select(AgentSchedule.id, AgentSchedule.workspace_id).where(
                AgentSchedule.deleted_at.is_(None)
            )
            if cursor is not None:
                query = query.where(AgentSchedule.id > cursor)
            rows = (await db.execute(query.order_by(AgentSchedule.id).limit(200))).all()
        if not rows:
            return count
        for schedule_id, workspace_id in rows:
            try:
                await client.start_workflow(
                    "AgentScheduleWorkflow",
                    ScheduleTick(str(workspace_id), str(schedule_id)),
                    id=schedule_workflow_id(str(workspace_id), str(schedule_id)),
                    task_queue=AGENT_TASK_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                )
                count += 1
            except WorkflowAlreadyStartedError:
                pass
        cursor = rows[-1].id


async def schedule_reconciliation_loop(resources: Resources, client: Client) -> None:
    while True:
        try:
            await reconcile_schedules(resources, client)
        except Exception:
            logger.exception("schedules.reconcile_failed")
        await asyncio.sleep(15)
