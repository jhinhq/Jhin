"""Approval inbox and decision logic (plan 6.16, 12.5).

Decision ordering is deliberate: the approval row is committed *first* (it is
the authority the agent worker re-reads), then the workflow is signaled to
wake up. If the signal cannot be delivered (workflow already finished), the
decision stays recorded and the caller is told.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import TemporalError
from temporalio.service import RPCError

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, Approval, Task
from jhin_domain import ApprovalStatus

MAX_PAGE_SIZE = 200


async def list_approvals(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[Approval, str | None, str | None]], int, int]:
    """Inbox: pending first, newest within each group (plan 17.11)."""
    limit = min(limit, MAX_PAGE_SIZE)
    query = (
        select(Approval, Agent.name, Task.title)
        .outerjoin(Agent, Agent.id == Approval.requested_by_agent_id)
        .outerjoin(Task, Task.id == Approval.task_id)
        .where(Approval.workspace_id == workspace_id)
    )
    if status_filter:
        query = query.where(Approval.status == status_filter)

    count_query = select(func.count()).select_from(
        select(Approval.id).where(Approval.workspace_id == workspace_id).subquery()
        if not status_filter
        else select(Approval.id)
        .where(Approval.workspace_id == workspace_id, Approval.status == status_filter)
        .subquery()
    )
    total = await db.scalar(count_query) or 0
    pending_count = (
        await db.scalar(
            select(func.count()).where(
                Approval.workspace_id == workspace_id,
                Approval.status == ApprovalStatus.PENDING.value,
            )
        )
        or 0
    )

    pending_first = (Approval.status != ApprovalStatus.PENDING.value).asc()
    rows = await db.execute(
        query.order_by(pending_first, Approval.requested_at.desc()).limit(limit).offset(offset)
    )
    items = [(approval, agent_name, task_title) for approval, agent_name, task_title in rows.all()]
    return items, int(total), int(pending_count)


async def get_approval(db: AsyncSession, workspace_id: UUID, approval_id: UUID) -> Approval:
    approval = await db.scalar(
        select(Approval).where(Approval.id == approval_id, Approval.workspace_id == workspace_id)
    )
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
    return approval


async def decide(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    approval_id: UUID,
    *,
    decision: str,  # "approved" | "rejected"
    request_id: UUID,
    ip_hash: str,
) -> Approval:
    approval = await get_approval(db, ctx.workspace_id, approval_id)

    already_this_decision = approval.status == decision
    if approval.status != ApprovalStatus.PENDING.value and not already_this_decision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Approval is already {approval.status}",
        )

    if not already_this_decision:
        approval.status = decision
        approval.decided_at = datetime.now(UTC)
        approval.decided_by_user_id = ctx.user.id
        audit.record(
            db,
            action=f"approval.{decision}",
            target_type="approval",
            target_id=approval.id,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user.id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={
                "action_type": approval.action_type,
                "task_id": str(approval.task_id) if approval.task_id else None,
                "run_id": str(approval.run_id) if approval.run_id else None,
            },
        )
        # Commit before signaling: the worker activity re-reads this row as
        # the sole authority for what was decided (plan 52).
        await db.commit()

    await signal_workflow(temporal, db, approval, decision)
    return approval


async def signal_workflow(
    temporal: TemporalClient, db: AsyncSession, approval: Approval, decision: str
) -> None:
    """Wake the parked AgentTaskWorkflow. Failure is surfaced, not hidden:
    the decision is already durable in Postgres."""
    if approval.task_id is None:
        return
    task = await db.scalar(select(Task).where(Task.id == approval.task_id))
    if task is None or task.temporal_workflow_id is None:
        return
    handle = temporal.get_workflow_handle(task.temporal_workflow_id)
    try:
        await handle.signal("approval_decision", args=[str(approval.id), decision])
    except (RPCError, TemporalError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Decision '{decision}' was recorded, but the task workflow could not "
                "be signaled (it may have already finished)"
            ),
        ) from exc
