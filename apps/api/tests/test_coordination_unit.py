"""Coordination service logic against SQLite: human work-request handling
(accept starts one workflow, retries are no-ops, decline creates no task),
review policy CRUD + scope validation, human review decisions, manager
rollup, the directory read, and the activity/attention projections."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from jhin_api.conversations import service as conversations
from jhin_api.coordination import service
from jhin_api.coordination.schemas import ReviewPolicyIn, ReviewPolicyUpdate, WorkRequestCreate
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, ReviewPolicy, Task, WorkRequest, WorkReview
from jhin_domain import (
    ActivityKind,
    ReviewMode,
    ReviewScopeKind,
    TaskState,
    WorkRequestStatus,
    WorkspaceRole,
    new_uuid7,
)
from jhin_policy import ReviewCondition, ReviewConditionKind, ReviewerSelector
from jhin_tools.directory import search_directory
from jhin_tools.reviews import open_review
from jhin_tools.work_requests import create_work_request


class FakeHandle:
    def __init__(self, temporal: FakeTemporal, workflow_id: str) -> None:
        self._temporal = temporal
        self._id = workflow_id

    async def signal(self, name: str, args: list[Any] | None = None) -> None:
        if self._temporal.signal_error is not None:
            raise self._temporal.signal_error
        self._temporal.signals.append((self._id, name, list(args or [])))


class FakeTemporal:
    def __init__(self) -> None:
        self.started: list[tuple[str, Any, str]] = []
        self.signals: list[tuple[str, str, list[Any]]] = []
        self.signal_error: Exception | None = None
        self.start_error: Exception | None = None

    async def start_workflow(self, name: str, arg: Any, *, id: str, task_queue: str) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started.append((name, arg, id))

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(self, workflow_id)


@pytest.fixture
async def agents(session: AsyncSession, admin_ctx: WorkspaceContext) -> tuple[Agent, Agent, Agent]:
    ws = admin_ctx.workspace_id
    cto = Agent(workspace_id=ws, name="CTO", slug="cto", role_title="CTO")
    session.add(cto)
    await session.flush()
    swe = Agent(workspace_id=ws, name="SWE", slug="swe", manager_agent_id=cto.id)
    writer = Agent(workspace_id=ws, name="Writer", slug="writer", expertise_json=["docs"])
    session.add_all([swe, writer])
    await session.flush()
    return cto, swe, writer


def member(ctx: WorkspaceContext) -> WorkspaceContext:
    return WorkspaceContext(user=ctx.user, workspace_id=ctx.workspace_id, role=WorkspaceRole.MEMBER)


async def test_human_create_accept_retry_and_decline(
    session: AsyncSession, admin_ctx: WorkspaceContext, agents: tuple[Agent, Agent, Agent]
) -> None:
    _, swe, writer = agents
    body = WorkRequestCreate(
        requester_agent_id=swe.id,
        target_agent_id=writer.id,
        title="Docs for the API",
        description="Write the reference page.",
        idempotency_key="docs-1",
    )
    # A member without a requester grant is refused; admin authority applies.
    with pytest.raises(HTTPException) as denied:
        await service.create_work_request(
            session, member(admin_ctx), body, request_id=new_uuid7(), ip_hash="h"
        )
    assert denied.value.status_code == 409 and "no_grant" in str(denied.value.detail)
    request, created = await service.create_work_request(
        session, admin_ctx, body, request_id=new_uuid7(), ip_hash="h"
    )
    assert created and request.requested_by_user_id == admin_ctx.user.id
    _, created_again = await service.create_work_request(
        session, admin_ctx, body, request_id=new_uuid7(), ip_hash="h"
    )
    assert not created_again

    temporal = FakeTemporal()
    accepted = await service.respond_work_request(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        request.id,
        decision="accept",
        response="On it",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert accepted.status == WorkRequestStatus.ACCEPTED.value
    assert len(temporal.started) == 1
    name, arg, workflow_id = temporal.started[0]
    assert name == "WorkRequestTaskWorkflow"
    assert workflow_id == f"work-request-{request.id}"
    assert arg.task_id == str(accepted.created_task_id)
    # Retrying the accept neither creates a second task nor a second workflow.
    await service.respond_work_request(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        request.id,
        decision="accept",
        response="",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert len(temporal.started) == 1
    tasks = list(await session.scalars(select(Task).where(Task.assigned_agent_id == writer.id)))
    assert len(tasks) == 1 and tasks[0].parent_task_id is None

    # Decline on another request creates nothing.
    other, _ = await service.create_work_request(
        session,
        admin_ctx,
        WorkRequestCreate(
            requester_agent_id=swe.id,
            target_agent_id=writer.id,
            title="Second",
            description="More docs",
            idempotency_key="docs-2",
        ),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    declined = await service.respond_work_request(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        other.id,
        decision="decline",
        response="No capacity",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert declined.status == WorkRequestStatus.DECLINED.value
    assert len(temporal.started) == 1
    items, total = await service.list_work_requests(session, admin_ctx.workspace_id)
    assert total == 2 and items[0].target_agent_name == "Writer"
    with pytest.raises(HTTPException) as missing:
        await service.get_work_request(session, new_uuid7(), request.id)
    assert missing.value.status_code == 404


async def test_review_policy_crud_and_scope_validation(
    session: AsyncSession, admin_ctx: WorkspaceContext, agents: tuple[Agent, Agent, Agent]
) -> None:
    cto, _, _ = agents
    with pytest.raises(ValueError):
        ReviewPolicyIn(name="bad", scope_kind=ReviewScopeKind.TEAM)
    with pytest.raises(ValueError):
        ReviewPolicyIn(name="bad", scope_kind=ReviewScopeKind.WORKSPACE, scope_key="x")
    with pytest.raises(ValueError):
        ReviewPolicyIn(name="bad", mode=ReviewMode.PERIODIC)
    body = ReviewPolicyIn(
        name="agent scope",
        scope_kind=ReviewScopeKind.AGENT,
        scope_id=cto.id,
        mode=ReviewMode.PRE_ACTION,
        conditions=[ReviewCondition(kind=ReviewConditionKind.DESTRUCTIVE_ACTION)],
        reviewer=ReviewerSelector(kind="human"),
        fail_closed=True,
    )
    policy = await service.create_review_policy(
        session, admin_ctx, body, request_id=new_uuid7(), ip_hash="h"
    )
    assert policy.conditions_json == [{"kind": "destructive_action", "threshold": None}]
    assert policy.reviewer_selector_json["kind"] == "human"
    with pytest.raises(HTTPException) as unknown_agent:
        await service.create_review_policy(
            session,
            admin_ctx,
            body.model_copy(update={"scope_id": new_uuid7()}),
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert unknown_agent.value.status_code == 404
    updated = await service.update_review_policy(
        session,
        admin_ctx,
        policy.id,
        ReviewPolicyUpdate(enabled=False, priority=5),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated.enabled is False and updated.priority == 5
    await service.delete_review_policy(
        session, admin_ctx, policy.id, request_id=new_uuid7(), ip_hash="h"
    )
    assert await session.get(ReviewPolicy, policy.id) is None


async def test_human_review_decision_and_attention(
    session: AsyncSession, admin_ctx: WorkspaceContext, agents: tuple[Agent, Agent, Agent]
) -> None:
    cto, swe, _ = agents
    task = Task(
        workspace_id=admin_ctx.workspace_id,
        title="Risky change",
        state=TaskState.RUNNING.value,
        assigned_agent_id=swe.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    human_review, _ = await open_review(
        session,
        workspace_id=admin_ctx.workspace_id,
        subject_agent_id=swe.id,
        trigger_key="t1",
        mode=ReviewMode.PRE_ACTION,
        selector=ReviewerSelector(kind="human"),
        fail_closed=True,
        task_id=task.id,
        evidence={"summary": "Deleting the table"},
    )
    ai_review, _ = await open_review(
        session,
        workspace_id=admin_ctx.workspace_id,
        subject_agent_id=swe.id,
        trigger_key="t2",
        mode=ReviewMode.BEFORE_CLOSE,
        selector=ReviewerSelector(),
        fail_closed=False,
        task_id=task.id,
    )
    assert ai_review.reviewer_agent_id == cto.id

    attention = await conversations.attention(session, admin_ctx.workspace_id)
    assert [r.id for r in attention.pending_reviews] == [human_review.id]
    assert attention.counts.reviews == 1 and attention.counts.total == 1
    assert attention.pending_reviews[0].subject_agent_name == "SWE"

    feed = await conversations.list_activity(session, admin_ctx.workspace_id)
    review_cards = [c for c in feed.items if c.kind is ActivityKind.NEEDS_REVIEW]
    assert {c.review_id for c in review_cards} == {human_review.id, ai_review.id}
    assert any("Deleting the table" in c.summary for c in review_cards)

    # A member cannot decide an AI-assigned review; an admin can.
    with pytest.raises(HTTPException) as forbidden:
        await service.decide_review(
            session,
            member(admin_ctx),
            ai_review.id,
            verdict="approve",
            feedback="",
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert forbidden.value.status_code == 403
    decided = await service.decide_review(
        session,
        member(admin_ctx),
        human_review.id,
        verdict="changes_requested",
        feedback="Back up first.",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert decided.status == "changes_requested" and decided.decided_by_user_id == admin_ctx.user.id
    with pytest.raises(HTTPException) as conflict:
        await service.decide_review(
            session,
            admin_ctx,
            human_review.id,
            verdict="approve",
            feedback="",
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert conflict.value.status_code == 409
    _items, total, pending = await service.list_reviews(session, admin_ctx.workspace_id)
    assert total == 2 and pending == 0
    assert (await conversations.attention(session, admin_ctx.workspace_id)).counts.reviews == 0


async def test_human_decision_signals_the_parked_task_workflow(
    session: AsyncSession, admin_ctx: WorkspaceContext, agents: tuple[Agent, Agent, Agent]
) -> None:
    """POST /reviews/{id}/decide commits first, then wakes the source task's
    AgentTaskWorkflow with review_decision (the same durable contract the
    approval decision uses); repeating the same verdict re-signals."""
    _, swe, _ = agents
    task = Task(
        workspace_id=admin_ctx.workspace_id,
        title="Parked on review",
        state=TaskState.RUNNING.value,
        assigned_agent_id=swe.id,
        correlation_id=new_uuid7(),
        temporal_workflow_id="task-parked",
    )
    session.add(task)
    await session.flush()
    tool_call_id = new_uuid7()
    review, _ = await open_review(
        session,
        workspace_id=admin_ctx.workspace_id,
        subject_agent_id=swe.id,
        trigger_key=f"pre_action:{tool_call_id}:p",
        mode=ReviewMode.PRE_ACTION,
        selector=ReviewerSelector(kind="human"),
        fail_closed=True,
        task_id=task.id,
        tool_call_id=tool_call_id,
    )
    temporal = FakeTemporal()
    decided = await service.decide_review(
        session,
        member(admin_ctx),
        review.id,
        verdict="approve",
        feedback="Go ahead.",
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert decided.status == "approved"
    assert temporal.signals == [("task-parked", "review_decision", [str(review.id), "approved"])]

    # Same verdict again: no second decision, the wake-up is repeated.
    again = await service.decide_review(
        session,
        member(admin_ctx),
        review.id,
        verdict="approve",
        feedback="Go ahead.",
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert again.decided_at == decided.decided_at
    assert len(temporal.signals) == 2
    # A different verdict after the fact conflicts.
    with pytest.raises(HTTPException) as conflict:
        await service.decide_review(
            session,
            member(admin_ctx),
            review.id,
            verdict="changes_requested",
            feedback="no",
            request_id=new_uuid7(),
            ip_hash="h",
            temporal=temporal,  # type: ignore[arg-type]
        )
    assert conflict.value.status_code == 409

    # Signal failure on a review that parks a tool call is surfaced (409)
    # after the decision is durable, so the client can retry the same verdict.
    other_call = new_uuid7()
    parked, _ = await open_review(
        session,
        workspace_id=admin_ctx.workspace_id,
        subject_agent_id=swe.id,
        trigger_key=f"pre_action:{other_call}:p",
        mode=ReviewMode.PRE_ACTION,
        selector=ReviewerSelector(kind="human"),
        fail_closed=True,
        task_id=task.id,
        tool_call_id=other_call,
    )
    temporal.signal_error = RPCError("gone", RPCStatusCode.NOT_FOUND, b"")
    with pytest.raises(HTTPException) as undelivered:
        await service.decide_review(
            session,
            member(admin_ctx),
            parked.id,
            verdict="changes_requested",
            feedback="Not yet.",
            request_id=new_uuid7(),
            ip_hash="h",
            temporal=temporal,  # type: ignore[arg-type]
        )
    assert undelivered.value.status_code == 409
    assert (await session.get(WorkReview, parked.id)).status == "changes_requested"  # type: ignore[union-attr]
    # A review that gates nothing ignores delivery failures.
    loose, _ = await open_review(
        session,
        workspace_id=admin_ctx.workspace_id,
        subject_agent_id=swe.id,
        trigger_key="explicit",
        mode=ReviewMode.BEFORE_CLOSE,
        selector=ReviewerSelector(kind="human"),
        fail_closed=False,
        task_id=task.id,
    )
    decided_loose = await service.decide_review(
        session,
        member(admin_ctx),
        loose.id,
        verdict="approve",
        feedback="",
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert decided_loose.status == "approved"


async def test_periodic_policy_lifecycle_starts_and_stops_its_workflow(
    session: AsyncSession, admin_ctx: WorkspaceContext, agents: tuple[Agent, Agent, Agent]
) -> None:
    cto, _, _ = agents
    temporal = FakeTemporal()
    body = ReviewPolicyIn(
        name="weekly",
        scope_kind=ReviewScopeKind.AGENT,
        scope_id=cto.id,
        mode=ReviewMode.PERIODIC,
        conditions=[ReviewCondition(kind=ReviewConditionKind.ALWAYS)],
        reviewer=ReviewerSelector(kind="human"),
        period_seconds=7 * 24 * 3600,
    )
    policy = await service.create_review_policy(
        session,
        admin_ctx,
        body,
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    workflow_id = f"review-periodic-{policy.id}"
    assert [(n, i) for n, _a, i in temporal.started] == [("PeriodicReviewWorkflow", workflow_id)]
    assert temporal.started[0][1].policy_id == str(policy.id)

    # A cadence change refreshes the running workflow (duplicate start is a no-op).
    temporal.start_error = WorkflowAlreadyStartedError(workflow_id, "PeriodicReviewWorkflow")
    await service.update_review_policy(
        session,
        admin_ctx,
        policy.id,
        ReviewPolicyUpdate(period_seconds=3600),
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert temporal.signals[-1] == (workflow_id, "refresh", [])
    # Disabling stops it; re-enabling starts a fresh one under the same id.
    await service.update_review_policy(
        session,
        admin_ctx,
        policy.id,
        ReviewPolicyUpdate(enabled=False),
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert temporal.signals[-1] == (workflow_id, "stop", [])
    temporal.start_error = None
    await service.update_review_policy(
        session,
        admin_ctx,
        policy.id,
        ReviewPolicyUpdate(enabled=True),
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert len(temporal.started) == 2
    # Unrelated edits do not touch the workflow; delete stops it.
    signals_before = len(temporal.signals)
    await service.update_review_policy(
        session,
        admin_ctx,
        policy.id,
        ReviewPolicyUpdate(name="weekly (renamed)"),
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert len(temporal.signals) == signals_before and len(temporal.started) == 2
    await service.delete_review_policy(
        session,
        admin_ctx,
        policy.id,
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert temporal.signals[-1] == (workflow_id, "stop", [])
    # Non-periodic policies never start a workflow.
    await service.create_review_policy(
        session,
        admin_ctx,
        ReviewPolicyIn(name="gate", conditions=[ReviewCondition(kind=ReviewConditionKind.ALWAYS)]),
        request_id=new_uuid7(),
        ip_hash="h",
        temporal=temporal,  # type: ignore[arg-type]
    )
    assert len(temporal.started) == 2


async def test_activity_projects_work_requests_without_duplicates(
    session: AsyncSession, admin_ctx: WorkspaceContext, agents: tuple[Agent, Agent, Agent]
) -> None:
    _, swe, writer = agents
    task = Task(
        workspace_id=admin_ctx.workspace_id,
        title="Launch",
        state=TaskState.RUNNING.value,
        assigned_agent_id=swe.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    request, _ = await create_work_request(
        session,
        workspace_id=admin_ctx.workspace_id,
        requester=swe,
        target=writer,
        requester_task=task,
        requester_run_id=None,
        title="Write launch post",
        description="Announce it.",
        idempotency_key="k",
    )
    feed = await conversations.list_activity(session, admin_ctx.workspace_id)
    asked = [c for c in feed.items if c.kind is ActivityKind.ASKED_AGENT]
    assert len(asked) == 1
    card = asked[0]
    assert card.id == f"work_request:{request.id}:asked"
    assert card.work_request_id == request.id
    assert card.actor_agent_name == "SWE" and card.target_agent_name == "Writer"
    assert card.task_id == task.id
    # Agent filter matches either side of the request.
    by_writer = await conversations.list_activity(
        session, admin_ctx.workspace_id, agent_id=writer.id
    )
    assert any(c.work_request_id == request.id for c in by_writer.items)

    request.status = WorkRequestStatus.DECLINED.value
    request.response = "Busy this week"
    await session.flush()
    feed = await conversations.list_activity(session, admin_ctx.workspace_id)
    reported = [c for c in feed.items if c.kind is ActivityKind.REPORTED]
    assert len(reported) == 1
    assert reported[0].id == f"work_request:{request.id}:reported"
    assert "declined" in reported[0].summary and "Busy this week" in reported[0].summary


async def test_rollup_and_directory_reads(
    session: AsyncSession, admin_ctx: WorkspaceContext, agents: tuple[Agent, Agent, Agent]
) -> None:
    cto, swe, writer = agents
    rollup = await service.manager_rollup(session, admin_ctx.workspace_id, cto.id)
    assert [r.name for r in rollup.reports] == ["SWE"]
    with pytest.raises(HTTPException) as missing:
        await service.manager_rollup(session, new_uuid7(), cto.id)
    assert missing.value.status_code == 404
    entries, _ = await search_directory(session, admin_ctx.workspace_id, expertise="docs")
    assert [e.name for e in entries] == ["Writer"]
    assert entries[0].id == str(writer.id)
    assert swe.id != writer.id


async def test_work_request_row_constraints(
    session: AsyncSession, admin_ctx: WorkspaceContext, agents: tuple[Agent, Agent, Agent]
) -> None:
    _, swe, writer = agents
    row = WorkRequest(
        workspace_id=admin_ctx.workspace_id,
        requester_agent_id=swe.id,
        target_agent_id=writer.id,
        title="x",
        idempotency_key="dup",
    )
    session.add(row)
    await session.flush()
    review = WorkReview(
        workspace_id=admin_ctx.workspace_id,
        trigger_key="k",
        mode="post_action",
        reviewer_type="none",
        status="skipped",
        requested_at=row.created_at,
        work_request_id=row.id,
    )
    session.add(review)
    await session.flush()
    assert isinstance(review.id, UUID)
