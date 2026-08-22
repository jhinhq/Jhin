"""Coordination activities (work requests, reviews, manager context) for the
agent worker. Kept in their own module so the Phase 10 rewrite of
``activities.py`` can adopt them without merge conflicts.

Integration points for ``AgentActivities`` (documented in
``docs/architecture/coordination.md``):

- ``run_agent_step_activity`` → after composing the task context, call
  :func:`organization_context` and :func:`manager_context` and pass the text
  into ``TaskContext(organization_context=..., manager_context=...)``.
- ``run_agent_step_activity`` → before executing an authorized tool call,
  call :func:`jhin_tools.reviews.check_review_gate`; on ``wait_review`` park
  the run (``StepResult.waiting_review_id`` is the suggested field), on
  ``blocked`` return the feedback as the observation.
- ``run_agent_step_activity`` → for each executed
  ``organization.respond_work_request`` call, lift the outcome with
  :func:`work_request_start_from_output` into ``StepResult.work_request_starts``
  (and include it in the committed step-result bundle so replays keep it).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from jhin_agent_worker.resources import Resources
from jhin_db.models import Agent
from jhin_observability import get_logger
from jhin_tools.directory import build_roster, render_roster
from jhin_tools.rollups import build_manager_rollup, render_manager_rollup
from jhin_tools.work_requests import finalize_work_request
from jhin_workflows.agent_task.shared import WorkRequestStart
from jhin_workflows.work_request_task import (
    ACTIVITY_FINALIZE_WORK_REQUEST,
    FinalizeWorkRequestInput,
)

logger = get_logger(__name__)


def work_request_start_from_output(output: dict[str, Any] | None) -> WorkRequestStart | None:
    """Lift an executed ``organization.respond_work_request`` output into the
    workflow contract when it created a task (accept); None otherwise."""
    if not output:
        return None
    task_id = output.get("created_task_id")
    request_id = output.get("work_request_id")
    if not isinstance(task_id, str) or not task_id or not isinstance(request_id, str):
        return None
    agent_id = output.get("agent_id")
    return WorkRequestStart(
        work_request_id=request_id,
        task_id=task_id,
        agent_id=str(agent_id) if isinstance(agent_id, str) else "",
    )


async def organization_context(session: AsyncSession, workspace_id: UUID, agent_id: UUID) -> str:
    """Roster block for the running agent's prompt (public identity only)."""
    agent = await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        return ""
    return render_roster(await build_roster(session, agent))


async def manager_context(session: AsyncSession, workspace_id: UUID, agent_id: UUID) -> str:
    """Rollup block for a manager's prompt; empty for agents with no reports.
    Only the current reporting manager receives it — the rollup is built
    from the agent's own manager chain, never from a requested agent id."""
    agent = await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        return ""
    return render_manager_rollup(await build_manager_rollup(session, agent))


class CoordinationActivities:
    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    @activity.defn(name=ACTIVITY_FINALIZE_WORK_REQUEST)
    async def finalize_work_request_activity(self, params: FinalizeWorkRequestInput) -> str:
        """Terminal projection for WorkRequestTaskWorkflow. Idempotent."""
        async with self._resources.session_factory() as session:
            request = await finalize_work_request(
                session,
                workspace_id=UUID(params.workspace_id),
                request_id=UUID(params.work_request_id),
                run_status=params.run_status,
            )
            await session.commit()
            status = request.status if request is not None else "missing"
        logger.info(
            "work_request.finalized",
            work_request_id=params.work_request_id,
            task_id=params.task_id,
            run_status=params.run_status,
            request_status=status,
        )
        return status
