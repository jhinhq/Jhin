"""Human-facing coordination services.

Humans and agents share the persistence/transition functions in
``jhin_tools.work_requests`` / ``jhin_tools.reviews``; this module adds
workspace RBAC framing (404 across workspaces), audit, the durable workflow
start on human acceptance, and read projections.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import TemporalError, WorkflowAlreadyStartedError
from temporalio.service import RPCError

from jhin_api.audit import service as audit
from jhin_api.coordination.schemas import (
    ReviewPolicyIn,
    ReviewPolicyUpdate,
    WorkRequestCreate,
    WorkRequestOut,
    WorkReviewOut,
)
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, ReviewPolicy, Task, WorkRequest, WorkReview, Workspace
from jhin_domain import (
    ActorType,
    ReviewerType,
    ReviewMode,
    TaskState,
    WorkReviewStatus,
    WorkspaceRole,
    role_satisfies,
)
from jhin_policy import (
    Grant,
    GrantEffect,
    coordination_settings,
    evaluate_work_request,
)
from jhin_tools import reviews as reviews_service
from jhin_tools import work_requests as wr
from jhin_tools.rollups import ManagerRollup, build_manager_rollup
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.agent_task.shared import SIGNAL_REVIEW_DECISION
from jhin_workflows.periodic_review import (
    SIGNAL_PERIODIC_REVIEW_REFRESH,
    SIGNAL_PERIODIC_REVIEW_STOP,
    PeriodicReviewInput,
    periodic_review_workflow_id,
)
from jhin_workflows.work_request_task import WorkRequestTaskInput, work_request_workflow_id

MAX_PAGE_SIZE = 100


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


async def _agent(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> Agent:
    agent = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        raise _not_found("Agent")
    return agent


# --- work requests ---


async def _project_requests(
    db: AsyncSession, workspace_id: UUID, rows: list[WorkRequest]
) -> list[WorkRequestOut]:
    ids = {r.requester_agent_id for r in rows} | {r.target_agent_id for r in rows}
    names: dict[UUID, str] = {}
    if ids:
        result = await db.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == workspace_id, Agent.id.in_(list(ids))
            )
        )
        names = {row[0]: row[1] for row in result.all()}
    out: list[WorkRequestOut] = []
    for row in rows:
        item = WorkRequestOut.model_validate(row)
        item.requester_agent_name = names.get(row.requester_agent_id)
        item.target_agent_name = names.get(row.target_agent_id)
        out.append(item)
    return out


async def list_work_requests(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    status_filter: str | None = None,
    agent_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WorkRequestOut], int]:
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    query = select(WorkRequest).where(WorkRequest.workspace_id == workspace_id)
    if status_filter:
        query = query.where(WorkRequest.status == status_filter)
    if agent_id is not None:
        query = query.where(
            (WorkRequest.requester_agent_id == agent_id) | (WorkRequest.target_agent_id == agent_id)
        )
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        await db.scalars(
            query.order_by(WorkRequest.created_at.desc(), WorkRequest.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return await _project_requests(db, workspace_id, rows), int(total)


async def get_work_request(db: AsyncSession, workspace_id: UUID, request_id: UUID) -> WorkRequest:
    request = await wr.get_work_request(db, workspace_id, request_id)
    if request is None:
        raise _not_found("Work request")
    return request


async def project_work_request(
    db: AsyncSession, workspace_id: UUID, request: WorkRequest
) -> WorkRequestOut:
    return (await _project_requests(db, workspace_id, [request]))[0]


async def _agent_grants(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> list[Grant]:
    from jhin_db.models import AgentCapabilityGrant

    rows = await db.scalars(
        select(AgentCapabilityGrant).where(
            AgentCapabilityGrant.workspace_id == workspace_id,
            AgentCapabilityGrant.agent_id == agent_id,
        )
    )
    grants: list[Grant] = []
    for row in rows:
        try:
            grants.append(
                Grant(
                    capability=row.capability, scope=row.scope_json, effect=GrantEffect(row.effect)
                )
            )
        except ValueError:
            continue
    return grants


async def create_work_request(
    db: AsyncSession,
    ctx: WorkspaceContext,
    body: WorkRequestCreate,
    *,
    request_id: UUID,
    ip_hash: str,
) -> tuple[WorkRequest, bool]:
    """A member opens a request on behalf of a requesting agent. The same
    structural guards apply; grant checks apply unless the caller is an
    admin (humans already hold the authority to assign work)."""
    requester = await _agent(db, ctx.workspace_id, body.requester_agent_id)
    target = await _agent(db, ctx.workspace_id, body.target_agent_id)
    task: Task | None = None
    if body.requester_task_id is not None:
        task = await db.scalar(
            select(Task).where(
                Task.id == body.requester_task_id, Task.workspace_id == ctx.workspace_id
            )
        )
        if task is None:
            raise _not_found("Task")
    facts = await wr.load_work_request_facts(
        db,
        workspace_id=ctx.workspace_id,
        requester_agent_id=requester.id,
        target_agent_id=str(target.id),
        task_id=task.id if task else None,
    )
    workspace = await db.get(Workspace, ctx.workspace_id)
    settings = coordination_settings(workspace.settings_json if workspace else None)
    grants = await _agent_grants(db, ctx.workspace_id, requester.id)
    if role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        # Admin authority stands in for the requester's grant, not for the
        # structural guards (self/inactive/depth/caps/ping-pong).
        grants = [Grant(capability="organization.work.request", scope={"targets": "any"})]
    decision = evaluate_work_request(grants, facts, settings)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"{decision.code}: {decision.reason}"
        )
    derived_key = wr.default_idempotency_key(
        requester.id, str(target.id), body.title, body.description
    )
    key = body.idempotency_key or f"user:{ctx.user.id}:{derived_key}"
    request, created = await wr.create_work_request(
        db,
        workspace_id=ctx.workspace_id,
        requester=requester,
        target=target,
        requester_task=task,
        requester_run_id=None,
        title=body.title,
        description=body.description,
        expected_output=body.expected_output,
        idempotency_key=key,
        requested_by_user_id=ctx.user.id,
    )
    if created:
        audit.record(
            db,
            action="work_request.created_by_user",
            target_type="work_request",
            target_id=request.id,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user.id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={"requester_agent_id": str(requester.id), "target_agent_id": str(target.id)},
        )
    await db.commit()
    return request, created


async def start_work_request_workflow(
    db: AsyncSession, temporal: TemporalClient, request: WorkRequest, task: Task
) -> None:
    """Start WorkRequestTaskWorkflow for a committed accepted request. On
    failure the created task is marked failed and the caller gets 503."""
    try:
        await temporal.start_workflow(
            "WorkRequestTaskWorkflow",
            WorkRequestTaskInput(
                workspace_id=str(request.workspace_id),
                work_request_id=str(request.id),
                task_id=str(task.id),
                agent_id=str(request.target_agent_id),
            ),
            id=work_request_workflow_id(str(request.id)),
            task_queue=AGENT_TASK_QUEUE,
        )
    except (RPCError, TemporalError, OSError) as exc:
        task.state = TaskState.FAILED.value
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start the work request workflow (Temporal unavailable)",
        ) from exc


async def respond_work_request(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient | None,
    request_id_: UUID,
    *,
    decision: str,
    response: str,
    request_id: UUID,
    ip_hash: str,
) -> WorkRequest:
    """Admin decides on behalf of the target agent. Accept starts the
    durable workflow exactly once; retries of an accepted request are
    no-ops; decline never creates a task."""
    request = await get_work_request(db, ctx.workspace_id, request_id_)
    try:
        if decision == "accept":
            request, task, created = await wr.accept_work_request(
                db, request, response=response, decided_by_user_id=ctx.user.id
            )
        elif decision == "decline":
            request = await wr.decline_work_request(
                db, request, response=response, decided_by_user_id=ctx.user.id
            )
            created = False
        else:
            request = await wr.request_clarification(
                db, request, response=response or "Please clarify.", decided_by_user_id=ctx.user.id
            )
            created = False
    except wr.WorkRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"{exc.code}: {exc.message}"
        ) from exc
    audit.record(
        db,
        action=f"work_request.{decision}_by_user",
        target_type="work_request",
        target_id=request.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"status": request.status},
    )
    await db.commit()
    if decision == "accept" and created:
        if temporal is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Task orchestration is unavailable",
            )
        await start_work_request_workflow(db, temporal, request, task)
    return request


# --- review policies ---


async def list_review_policies(db: AsyncSession, workspace_id: UUID) -> list[ReviewPolicy]:
    rows = await db.scalars(
        select(ReviewPolicy)
        .where(ReviewPolicy.workspace_id == workspace_id)
        .order_by(ReviewPolicy.priority, ReviewPolicy.created_at, ReviewPolicy.id)
    )
    return list(rows)


async def get_review_policy(db: AsyncSession, workspace_id: UUID, policy_id: UUID) -> ReviewPolicy:
    policy = await db.scalar(
        select(ReviewPolicy).where(
            ReviewPolicy.id == policy_id, ReviewPolicy.workspace_id == workspace_id
        )
    )
    if policy is None:
        raise _not_found("Review policy")
    return policy


async def _validate_scope_target(
    db: AsyncSession, workspace_id: UUID, body: ReviewPolicyIn
) -> None:
    from jhin_db.models import Team

    if body.scope_kind.value == "agent" and body.scope_id is not None:
        await _agent(db, workspace_id, body.scope_id)
    if body.scope_kind.value == "team" and body.scope_id is not None:
        team = await db.scalar(
            select(Team).where(Team.id == body.scope_id, Team.workspace_id == workspace_id)
        )
        if team is None:
            raise _not_found("Team")


def _periodic_active(policy: ReviewPolicy) -> bool:
    return bool(policy.enabled) and policy.mode == ReviewMode.PERIODIC.value


async def _signal_periodic_review(temporal: TemporalClient, policy_id: UUID, signal: str) -> None:
    """Best effort: the workflow also reloads the policy before every
    window, so a lost stop/refresh only delays the effect by one window."""
    handle = temporal.get_workflow_handle(periodic_review_workflow_id(str(policy_id)))
    try:
        await handle.signal(signal)
    except (RPCError, TemporalError, OSError):
        return


async def sync_periodic_review_workflow(
    temporal: TemporalClient | None,
    policy: ReviewPolicy,
    *,
    deleted: bool = False,
) -> None:
    """Start/stop the durable ``PeriodicReviewWorkflow`` for a committed
    policy change. Enabled periodic policies get exactly one workflow
    (``review-periodic-{policy_id}``; a duplicate start becomes a
    ``refresh`` signal); disabled, re-moded, or deleted policies are told to
    stop. Called after commit; failures never roll the policy back."""
    if temporal is None:
        return
    if deleted or not _periodic_active(policy):
        await _signal_periodic_review(temporal, policy.id, SIGNAL_PERIODIC_REVIEW_STOP)
        return
    try:
        await temporal.start_workflow(
            "PeriodicReviewWorkflow",
            PeriodicReviewInput(workspace_id=str(policy.workspace_id), policy_id=str(policy.id)),
            id=periodic_review_workflow_id(str(policy.id)),
            task_queue=AGENT_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        await _signal_periodic_review(temporal, policy.id, SIGNAL_PERIODIC_REVIEW_REFRESH)
    except (RPCError, TemporalError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The review policy was saved, but its periodic review workflow could not "
                "be started (Temporal unavailable)"
            ),
        ) from exc


async def create_review_policy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    body: ReviewPolicyIn,
    *,
    request_id: UUID,
    ip_hash: str,
    temporal: TemporalClient | None = None,
) -> ReviewPolicy:
    await _validate_scope_target(db, ctx.workspace_id, body)
    policy = ReviewPolicy(
        workspace_id=ctx.workspace_id,
        name=body.name,
        scope_kind=body.scope_kind.value,
        scope_id=body.scope_id,
        scope_key=body.scope_key,
        enabled=body.enabled,
        mode=body.mode.value,
        conditions_json=[c.model_dump(mode="json") for c in body.conditions],
        reviewer_selector_json=body.reviewer.model_dump(mode="json"),
        fail_closed=body.fail_closed,
        priority=body.priority,
        period_seconds=body.period_seconds,
        created_by_user_id=ctx.user.id,
    )
    db.add(policy)
    await db.flush()
    audit.record(
        db,
        action="review_policy.created",
        target_type="review_policy",
        target_id=policy.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": policy.name, "mode": policy.mode, "scope_kind": policy.scope_kind},
    )
    await db.commit()
    await sync_periodic_review_workflow(temporal, policy)
    return policy


async def update_review_policy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    policy_id: UUID,
    body: ReviewPolicyUpdate,
    *,
    request_id: UUID,
    ip_hash: str,
    temporal: TemporalClient | None = None,
) -> ReviewPolicy:
    policy = await get_review_policy(db, ctx.workspace_id, policy_id)
    changed: dict[str, Any] = {}
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key == "conditions":
            policy.conditions_json = [c.model_dump(mode="json") for c in body.conditions or []]
        elif key == "reviewer" and body.reviewer is not None:
            policy.reviewer_selector_json = body.reviewer.model_dump(mode="json")
        elif key == "mode" and body.mode is not None:
            policy.mode = body.mode.value
        elif value is not None or key == "period_seconds":
            setattr(policy, key, value)
        changed[key] = data[key] if not hasattr(data[key], "model_dump") else str(data[key])
    audit.record(
        db,
        action="review_policy.updated",
        target_type="review_policy",
        target_id=policy.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"fields": sorted(changed)},
    )
    await db.commit()
    if changed.keys() & {"enabled", "mode", "period_seconds", "reviewer", "scope_kind", "scope_id"}:
        await sync_periodic_review_workflow(temporal, policy)
    return policy


async def delete_review_policy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    policy_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
    temporal: TemporalClient | None = None,
) -> None:
    policy = await get_review_policy(db, ctx.workspace_id, policy_id)
    audit.record(
        db,
        action="review_policy.deleted",
        target_type="review_policy",
        target_id=policy.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": policy.name},
    )
    await db.delete(policy)
    await db.commit()
    await sync_periodic_review_workflow(temporal, policy, deleted=True)


# --- reviews ---


async def project_reviews(
    db: AsyncSession, workspace_id: UUID, rows: list[WorkReview]
) -> list[WorkReviewOut]:
    agent_ids = {r.subject_agent_id for r in rows if r.subject_agent_id} | {
        r.reviewer_agent_id for r in rows if r.reviewer_agent_id
    }
    names: dict[UUID, str] = {}
    if agent_ids:
        result = await db.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == workspace_id, Agent.id.in_(list(agent_ids))
            )
        )
        names = {row[0]: row[1] for row in result.all()}
    task_ids = [r.task_id for r in rows if r.task_id]
    titles: dict[UUID, str] = {}
    if task_ids:
        result = await db.execute(
            select(Task.id, Task.title).where(
                Task.workspace_id == workspace_id, Task.id.in_(task_ids)
            )
        )
        titles = {row[0]: row[1] for row in result.all()}
    out: list[WorkReviewOut] = []
    for row in rows:
        item = WorkReviewOut.model_validate(row)
        item.subject_agent_name = names.get(row.subject_agent_id) if row.subject_agent_id else None
        item.reviewer_agent_name = (
            names.get(row.reviewer_agent_id) if row.reviewer_agent_id else None
        )
        item.task_title = titles.get(row.task_id) if row.task_id else None
        out.append(item)
    return out


async def list_reviews(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    status_filter: str | None = None,
    reviewer: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WorkReviewOut], int, int]:
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    query = select(WorkReview).where(WorkReview.workspace_id == workspace_id)
    if status_filter:
        query = query.where(WorkReview.status == status_filter)
    if reviewer in (ReviewerType.HUMAN.value, ReviewerType.AGENT.value):
        query = query.where(WorkReview.reviewer_type == reviewer)
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pending = await db.scalar(
        select(func.count())
        .select_from(WorkReview)
        .where(
            WorkReview.workspace_id == workspace_id,
            WorkReview.status == WorkReviewStatus.PENDING.value,
            WorkReview.reviewer_type == ReviewerType.HUMAN.value,
        )
    )
    rows = list(
        await db.scalars(
            query.order_by(WorkReview.requested_at.desc(), WorkReview.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return await project_reviews(db, workspace_id, rows), int(total), int(pending or 0)


async def get_review(db: AsyncSession, workspace_id: UUID, review_id: UUID) -> WorkReview:
    review = await reviews_service.get_review(db, workspace_id, review_id)
    if review is None:
        raise _not_found("Review")
    return review


async def decide_review(
    db: AsyncSession,
    ctx: WorkspaceContext,
    review_id: UUID,
    *,
    verdict: str,
    feedback: str,
    request_id: UUID,
    ip_hash: str,
    temporal: TemporalClient | None = None,
) -> WorkReview:
    """A human decides a review. Members decide human-assigned reviews;
    admins may also decide on behalf of an AI reviewer. The decision gates
    or records only — it never touches approvals or grants.

    Like approval decisions, the row is committed first and the source task
    workflow is then woken with the ``review_decision`` signal; repeating the
    same verdict re-sends the signal so a commit→signal failure is repairable
    without recording a second decision."""
    review = await get_review(db, ctx.workspace_id, review_id)
    if review.reviewer_type == ReviewerType.AGENT.value and not role_satisfies(
        ctx.role, WorkspaceRole.ADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This review is assigned to an AI reviewer; only an admin may decide it",
        )
    if review.status != WorkReviewStatus.PENDING.value:
        if review.verdict == verdict and review.decided_by_user_id == ctx.user.id:
            await signal_review_workflow(temporal, db, review)
            return review
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Review is already {review.status}"
        )
    try:
        review = await reviews_service.decide_review(
            db, review, verdict=verdict, feedback=feedback, decided_by_user_id=ctx.user.id
        )
    except reviews_service.ReviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        ) from exc
    audit.record(
        db,
        action="work_review.decided_by_user",
        target_type="work_review",
        target_id=review.id,
        workspace_id=ctx.workspace_id,
        actor_type=ActorType.USER,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"verdict": verdict, "status": review.status},
    )
    await db.commit()
    await signal_review_workflow(temporal, db, review)
    return review


async def signal_review_workflow(
    temporal: TemporalClient | None, db: AsyncSession, review: WorkReview
) -> None:
    """Wake the source task's ``AgentTaskWorkflow`` with ``review_decision``.
    The decision is already durable; a failure is surfaced (409) only when a
    tool call is actually parked on this review, so the caller can retry the
    idempotent decision. Reviews that gate nothing ignore delivery errors."""
    if temporal is None or review.task_id is None:
        return
    task = await db.scalar(
        select(Task).where(Task.id == review.task_id, Task.workspace_id == review.workspace_id)
    )
    if task is None or task.temporal_workflow_id is None:
        return
    handle = temporal.get_workflow_handle(task.temporal_workflow_id)
    try:
        await handle.signal(SIGNAL_REVIEW_DECISION, args=[str(review.id), review.status])
    except (RPCError, TemporalError, OSError) as exc:
        if review.tool_call_id is None:
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Review decision '{review.verdict}' was recorded, but the task workflow "
                "could not be signaled (it may have already finished)"
            ),
        ) from exc


# --- rollups ---


async def manager_rollup(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> ManagerRollup:
    manager = await _agent(db, workspace_id, agent_id)
    return await build_manager_rollup(db, manager)
