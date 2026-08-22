"""Durable review parking at the tool-worker boundary (mirrors the approval
tests): a pending pre-action review persists the call as ``pending_review``
before any approval row or execution claim exists, retries replay the park,
and the decided review resumes the very same call exactly once through the
normal authorization → approval → claim → effect path."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError

from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    AuditEvent,
    ReviewPolicy,
    RunEvent,
    Task,
    ToolCall,
    WorkReview,
    Workspace,
)
from jhin_domain import ApprovalStatus, RunStatus, ToolCallStatus, new_uuid7
from jhin_observability import noop_metrics, noop_tracer
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tool_worker.activities import ToolActivities
from jhin_tools import ToolCatalog, ToolExecutionContext
from jhin_tools.reviews import decide_review
from jhin_workflows.agent_task.shared import (
    BoundToolResult,
    ExecuteBoundToolInput,
    ResolveBoundToolApprovalInput,
    ResolveBoundToolReviewInput,
)

WRITE_TOOL = "test.reviewed.write"
ELEVATED_TOOL = "test.reviewed.elevated"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class _Output(BaseModel):
    receipt: str


@dataclass
class _Effect:
    count: int = 0


@dataclass
class _Resources:
    session_factory: async_sessionmaker[AsyncSession]
    runtime: object = field(
        default_factory=lambda: SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())
    )
    crypto: None = None
    test_barrier: None = None


@dataclass
class ReviewWorld:
    activities: ToolActivities
    sessions: async_sessionmaker[AsyncSession]
    effect: _Effect
    workspace: Workspace
    manager: Agent
    agent: Agent
    task: Task
    run: AgentRun

    def execute_params(self, ordinal: int = 0) -> ExecuteBoundToolInput:
        return ExecuteBoundToolInput(
            workspace_id=str(self.workspace.id),
            run_id=str(self.run.id),
            step_index=1,
            ordinal=ordinal,
        )

    def review_params(self, review_id: str) -> ResolveBoundToolReviewInput:
        return ResolveBoundToolReviewInput(
            workspace_id=str(self.workspace.id),
            task_id=str(self.task.id),
            run_id=str(self.run.id),
            agent_id=str(self.agent.id),
            review_id=review_id,
        )

    def approval_params(self, approval_id: str) -> ResolveBoundToolApprovalInput:
        return ResolveBoundToolApprovalInput(
            workspace_id=str(self.workspace.id),
            task_id=str(self.task.id),
            run_id=str(self.run.id),
            agent_id=str(self.agent.id),
            approval_id=approval_id,
        )

    async def seed_manifest(self, *tool_names: str) -> None:
        async with self.sessions() as session:
            session.add(
                RunEvent(
                    workspace_id=self.workspace.id,
                    run_id=self.run.id,
                    task_id=self.task.id,
                    seq=0,
                    event_type="agent.step.tool_manifest",
                    payload_json={
                        "step": 1,
                        "manifest": {
                            "count": len(tool_names),
                            "calls": [
                                {
                                    "ordinal": ordinal,
                                    "lossless": True,
                                    "tool_name": name,
                                    "arguments_json": json.dumps(
                                        {"value": "reviewed"}, separators=(",", ":")
                                    ),
                                }
                                for ordinal, name in enumerate(tool_names)
                            ],
                        },
                    },
                )
            )
            await session.commit()

    async def decide(self, review_id: str, verdict: str, feedback: str = "ok") -> None:
        async with self.sessions() as session:
            review = await session.get(WorkReview, UUID(review_id))
            assert review is not None
            await decide_review(
                session,
                review,
                verdict=verdict,
                feedback=feedback,
                decided_by_agent_id=self.manager.id,
            )
            await session.commit()

    async def approve_in_database(self, approval_id: str) -> None:
        async with self.sessions() as session:
            approval = await session.get(Approval, UUID(approval_id))
            assert approval is not None
            approval.status = ApprovalStatus.APPROVED.value
            approval.decided_at = datetime.now(UTC)
            await session.commit()

    async def revoke_grant(self, capability: str) -> None:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(AgentCapabilityGrant).where(AgentCapabilityGrant.capability == capability)
            )
            for row in rows:
                await session.delete(row)
            await session.commit()

    async def park(self, tool_name: str = WRITE_TOOL) -> BoundToolResult:
        await self.seed_manifest(tool_name)
        parked = await self.activities.execute_bound_tool_activity(self.execute_params())
        assert parked.status == "needs_review"
        assert parked.stop_reason == "needs_review"
        assert parked.review_id is not None
        assert parked.approval_id is None
        return parked

    async def tool_call(self, tool_call_id: str) -> ToolCall:
        async with self.sessions() as session:
            row = await session.get(ToolCall, UUID(tool_call_id))
            assert row is not None
            return row

    async def audit_actions(self) -> list[str]:
        async with self.sessions() as session:
            return list(await session.scalars(select(AuditEvent.action).order_by(AuditEvent.id)))


async def _build_world(sessions: async_sessionmaker[AsyncSession]) -> ReviewWorld:
    effect = _Effect()

    async def execute_effect(_ctx: ToolExecutionContext, _payload: BaseModel) -> BaseModel:
        effect.count += 1
        return _Output(receipt=f"receipt-{effect.count}")

    catalog = ToolCatalog()
    for name, risk in ((WRITE_TOOL, RiskLevel.WRITE), (ELEVATED_TOOL, RiskLevel.ELEVATED)):
        catalog.register(
            ToolDefinition(
                name=name,
                description="Review-gated deterministic effect",
                risk=risk,
                input_model=_Input,
                output_model=_Output,
                required_capability=name,
                supports_approval=True,
            ),
            execute_effect,
        )

    async with sessions() as session:
        workspace = Workspace(name="Reviews", slug=f"reviews-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        manager = Agent(workspace_id=workspace.id, name="Manager", slug="manager")
        session.add(manager)
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id,
            name="Reviewed agent",
            slug="reviewed-agent",
            manager_agent_id=manager.id,
        )
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Resolve one review",
            assigned_agent_id=agent.id,
            correlation_id=new_uuid7(),
            temporal_workflow_id="task-review-test",
        )
        session.add(task)
        await session.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.RUNNING.value,
        )
        session.add(run)
        for name in (WRITE_TOOL, ELEVATED_TOOL):
            session.add(
                AgentCapabilityGrant(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    capability=name,
                    scope_json={},
                    effect="allow",
                )
            )
        session.add(
            ReviewPolicy(
                workspace_id=workspace.id,
                name="manager reviews everything first",
                mode="pre_action",
                conditions_json=[{"kind": "always"}],
                reviewer_selector_json={"kind": "reporting_manager"},
                fail_closed=True,
            )
        )
        await session.commit()

    return ReviewWorld(
        activities=ToolActivities(_Resources(sessions), catalog),  # type: ignore[arg-type]
        sessions=sessions,
        effect=effect,
        workspace=workspace,
        manager=manager,
        agent=agent,
        task=task,
        run=run,
    )


@pytest.fixture
async def world() -> AsyncIterator[ReviewWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield await _build_world(sessions)
    await engine.dispose()


async def test_pending_review_parks_before_any_claim_and_replays_on_retry(
    world: ReviewWorld,
) -> None:
    parked = await world.park()

    row = await world.tool_call(parked.tool_call_id)
    assert row.status == ToolCallStatus.PENDING_REVIEW.value
    assert str(row.review_id) == parked.review_id
    assert row.approval_id is None
    async with world.sessions() as session:
        review = await session.get(WorkReview, UUID(parked.review_id or ""))
        assert review is not None and review.status == "pending"
        assert review.reviewer_agent_id == world.manager.id
        assert review.tool_call_id == row.id
        assert await session.scalar(select(Approval)) is None
    assert world.effect.count == 0

    # A worker restart re-runs the bound execution: same park, no effect.
    replay = await world.activities.execute_bound_tool_activity(world.execute_params())
    assert replay == parked
    assert world.effect.count == 0
    actions = await world.audit_actions()
    assert actions.count("review.requested") == 1
    assert "tool.call.claimed" not in actions


async def test_resolution_waits_for_the_database_decision(world: ReviewWorld) -> None:
    parked = await world.park()

    with pytest.raises(ApplicationError) as pending:
        await world.activities.resolve_bound_tool_review_activity(
            world.review_params(parked.review_id or "")
        )
    assert pending.value.type == "review_pending"
    assert pending.value.non_retryable is False
    assert world.effect.count == 0


async def test_approved_review_executes_the_original_call_exactly_once(
    world: ReviewWorld,
) -> None:
    parked = await world.park()
    await world.decide(parked.review_id or "", "approve")

    first = await world.activities.resolve_bound_tool_review_activity(
        world.review_params(parked.review_id or "")
    )
    second = await world.activities.resolve_bound_tool_review_activity(
        world.review_params(parked.review_id or "")
    )

    assert first.tool_call_id == parked.tool_call_id
    assert first.status == "executed" and first.stop_reason is None
    assert second.status == "executed" and second.tool_call_id == parked.tool_call_id
    assert world.effect.count == 1
    row = await world.tool_call(parked.tool_call_id)
    assert row.status == ToolCallStatus.COMPLETED.value
    assert row.sanitized_output_json == {"receipt": "receipt-1"}
    actions = await world.audit_actions()
    assert actions.count("tool.call.claimed") == 1
    assert "tool.call.review_approved" in actions


async def test_changes_requested_denies_with_reviewer_feedback(world: ReviewWorld) -> None:
    parked = await world.park()
    await world.decide(parked.review_id or "", "changes_requested", "Back up the table first.")

    result = await world.activities.resolve_bound_tool_review_activity(
        world.review_params(parked.review_id or "")
    )

    assert result.status == "denied" and result.tool_call_id == parked.tool_call_id
    assert world.effect.count == 0
    row = await world.tool_call(parked.tool_call_id)
    assert row.status == ToolCallStatus.DENIED.value
    assert row.error_code == "review_changes_requested"
    async with world.sessions() as session:
        denial = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "tool.call.denied")
        )
        assert denial is not None
        assert denial.metadata_json["reason"] == "Back up the table first."


async def test_gate_order_review_then_human_approval_then_effect(world: ReviewWorld) -> None:
    parked = await world.park(ELEVATED_TOOL)
    await world.decide(parked.review_id or "", "approve")

    staged = await world.activities.resolve_bound_tool_review_activity(
        world.review_params(parked.review_id or "")
    )

    assert staged.status == "needs_approval" and staged.stop_reason == "needs_approval"
    assert staged.approval_id is not None and staged.review_id == parked.review_id
    assert world.effect.count == 0
    row = await world.tool_call(parked.tool_call_id)
    assert row.status == ToolCallStatus.PENDING_APPROVAL.value
    assert str(row.approval_id) == staged.approval_id
    # A retry of the review resolution replays the staged approval.
    again = await world.activities.resolve_bound_tool_review_activity(
        world.review_params(parked.review_id or "")
    )
    assert again.status == "needs_approval" and again.approval_id == staged.approval_id

    await world.approve_in_database(staged.approval_id)
    executed = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(staged.approval_id)
    )
    assert executed.status == "executed" and executed.tool_call_id == parked.tool_call_id
    assert world.effect.count == 1


async def test_resumption_reloads_live_authorization(world: ReviewWorld) -> None:
    parked = await world.park()
    await world.decide(parked.review_id or "", "approve")
    await world.revoke_grant(WRITE_TOOL)

    result = await world.activities.resolve_bound_tool_review_activity(
        world.review_params(parked.review_id or "")
    )

    assert result.status == "denied"
    assert world.effect.count == 0
    row = await world.tool_call(parked.tool_call_id)
    assert row.status == ToolCallStatus.DENIED.value


async def test_review_context_must_match_the_parked_run(world: ReviewWorld) -> None:
    parked = await world.park()
    await world.decide(parked.review_id or "", "approve")
    params = world.review_params(parked.review_id or "")
    params.agent_id = str(world.manager.id)

    with pytest.raises(ApplicationError) as mismatch:
        await world.activities.resolve_bound_tool_review_activity(params)
    assert mismatch.value.type == "review_context_not_found"
    assert world.effect.count == 0
