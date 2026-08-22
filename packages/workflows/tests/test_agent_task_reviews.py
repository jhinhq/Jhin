"""AgentTaskWorkflow review parking: the durable wait on review_decision
(signal after and before the park), the review → approval → effect chain,
and forwarding an AI reviewer's decision to the source task workflow."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows import AGENT_TASK_QUEUE, TOOL_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskInput, AgentTaskWorkflow
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_COMMIT_AGENT_STEP,
    ACTIVITY_COMMIT_APPROVAL_PROJECTION,
    ACTIVITY_COMMIT_REVIEW_PROJECTION,
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_FINALIZE_RUN_PROJECTION,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
    ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW,
    ACTIVITY_RESOLVE_SNAPSHOT,
    SIGNAL_REVIEW_DECISION,
    AdvertisedTool,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    CommitReviewProjectionInput,
    ExecuteBoundToolInput,
    FinalizeInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    ResolveBoundToolApprovalInput,
    ResolveBoundToolReviewInput,
    ReviewDecisionSignal,
    SnapshotResult,
    StepResult,
)

_REVIEW_ID = "018f4d52-8b93-7d41-8ac7-7f190f094444"
_APPROVAL_ID = "018f4d52-8b93-7d41-8ac7-7f190f095555"
_TOOL_CALL_ID = "018f4d52-8b93-7d41-8ac7-7f190f096666"


class ReviewWorld:
    """Deterministic activity boundaries; the real workflow runs on top."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.review_resolutions: list[ResolveBoundToolReviewInput] = []
        self.review_projections: list[CommitReviewProjectionInput] = []
        self.approval_resolutions: list[ResolveBoundToolApprovalInput] = []
        self.finalize_calls: list[FinalizeInput] = []
        self.parked = asyncio.Event()
        self.release_commit = asyncio.Event()
        self.release_commit.set()
        self.review_outcome = "executed"  # or "needs_approval"
        self.review_decisions: list[ReviewDecisionSignal] = []
        self.park_on_review = True
        self.reason_calls = 0

    def _record(self, name: str) -> None:
        self.calls.append((name, activity.info().task_queue))

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot(self, params: AgentTaskInput) -> SnapshotResult:
        self._record(ACTIVITY_RESOLVE_SNAPSHOT)
        return SnapshotResult(
            run_id=f"run-{params.task_id}", snapshot_json="{}", snapshot_hash="h", max_steps=3
        )

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def resolve_advertised_tools(
        self, _params: ResolveAdvertisedToolsInput
    ) -> list[AdvertisedTool]:
        self._record(ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
        return [AdvertisedTool(name="system.echo", description="", parameters={})]

    @activity.defn(name=ACTIVITY_REASON_AGENT_STEP)
    async def reason_agent_step(self, _params: ReasonAgentStepInput) -> ReasonAgentStepResult:
        self._record(ACTIVITY_REASON_AGENT_STEP)
        self.reason_calls += 1
        return ReasonAgentStepResult(call_count=1 if self.reason_calls == 1 else 0)

    @activity.defn(name=ACTIVITY_EXECUTE_BOUND_TOOL)
    async def execute_bound_tool(self, _params: ExecuteBoundToolInput) -> BoundToolResult:
        self._record(ACTIVITY_EXECUTE_BOUND_TOOL)
        if self.park_on_review:
            return BoundToolResult(
                tool_call_id=_TOOL_CALL_ID,
                status="needs_review",
                stop_reason="needs_review",
                review_id=_REVIEW_ID,
            )
        return BoundToolResult(tool_call_id=_TOOL_CALL_ID, status="executed")

    @activity.defn(name=ACTIVITY_COMMIT_AGENT_STEP)
    async def commit_agent_step(self, params: CommitAgentStepInput) -> StepResult:
        self._record(ACTIVITY_COMMIT_AGENT_STEP)
        if params.step_index == 0:
            await self.release_commit.wait()
            self.parked.set()
            return StepResult(
                done=False,
                waiting_review_id=_REVIEW_ID if self.park_on_review else None,
                review_decisions=list(self.review_decisions),
            )
        return StepResult(done=True)

    @activity.defn(name=ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW)
    async def resolve_bound_tool_review(
        self, params: ResolveBoundToolReviewInput
    ) -> BoundToolResult:
        self._record(ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW)
        self.review_resolutions.append(params)
        if self.review_outcome == "needs_approval":
            return BoundToolResult(
                tool_call_id=_TOOL_CALL_ID,
                status="needs_approval",
                approval_id=_APPROVAL_ID,
                stop_reason="needs_approval",
                review_id=_REVIEW_ID,
            )
        return BoundToolResult(tool_call_id=_TOOL_CALL_ID, status="executed")

    @activity.defn(name=ACTIVITY_COMMIT_REVIEW_PROJECTION)
    async def commit_review_projection(self, params: CommitReviewProjectionInput) -> StepResult:
        self._record(ACTIVITY_COMMIT_REVIEW_PROJECTION)
        self.review_projections.append(params)
        if self.review_outcome == "needs_approval":
            return StepResult(done=False, waiting_approval_id=_APPROVAL_ID)
        return StepResult(done=False)

    @activity.defn(name=ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL)
    async def resolve_bound_tool_approval(
        self, params: ResolveBoundToolApprovalInput
    ) -> BoundToolResult:
        self._record(ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL)
        self.approval_resolutions.append(params)
        return BoundToolResult(tool_call_id=_TOOL_CALL_ID, status="executed")

    @activity.defn(name=ACTIVITY_COMMIT_APPROVAL_PROJECTION)
    async def commit_approval_projection(
        self, _params: CommitApprovalProjectionInput
    ) -> StepResult:
        self._record(ACTIVITY_COMMIT_APPROVAL_PROJECTION)
        return StepResult(done=False)

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup_run_workspace(
        self, _params: CleanupRunWorkspaceInput
    ) -> CleanupRunWorkspaceResult:
        self._record(ACTIVITY_CLEANUP_RUN_WORKSPACE)
        return CleanupRunWorkspaceResult(deleted=True)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN_PROJECTION)
    async def finalize_run_projection(self, params: FinalizeInput) -> None:
        self._record(ACTIVITY_FINALIZE_RUN_PROJECTION)
        self.finalize_calls.append(params)

    def agent_activities(self) -> list[Any]:
        return [
            self.resolve_snapshot,
            self.reason_agent_step,
            self.commit_agent_step,
            self.commit_review_projection,
            self.commit_approval_projection,
            self.finalize_run_projection,
        ]

    def tool_activities(self) -> list[Any]:
        return [
            self.resolve_advertised_tools,
            self.execute_bound_tool,
            self.resolve_bound_tool_review,
            self.resolve_bound_tool_approval,
            self.cleanup_run_workspace,
        ]


def _params() -> AgentTaskInput:
    return AgentTaskInput(
        workspace_id=str(uuid.uuid4()), task_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4())
    )


async def _start(env: WorkflowEnvironment, params: AgentTaskInput) -> WorkflowHandle[Any, Any]:
    return await env.client.start_workflow(
        AgentTaskWorkflow.run, params, id=f"task-{params.task_id}", task_queue=AGENT_TASK_QUEUE
    )


@pytest.fixture
async def env() -> Any:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _workers(env: WorkflowEnvironment, *worlds: ReviewWorld) -> tuple[Worker, Worker]:
    agent_acts: list[Any] = []
    tool_acts: list[Any] = []
    for world in worlds:
        agent_acts += world.agent_activities()
        tool_acts += world.tool_activities()
    return (
        Worker(
            env.client,
            task_queue=AGENT_TASK_QUEUE,
            workflows=[AgentTaskWorkflow],
            activities=agent_acts,
        ),
        Worker(env.client, task_queue=TOOL_TASK_QUEUE, activities=tool_acts),
    )


async def test_parks_on_review_and_resumes_on_the_decision_signal(env: Any) -> None:
    world = ReviewWorld()
    params = _params()
    agent_worker, tool_worker = _workers(env, world)
    async with agent_worker, tool_worker:
        handle = await _start(env, params)
        await asyncio.wait_for(world.parked.wait(), timeout=5)
        for _ in range(200):
            status = await handle.query(AgentTaskWorkflow.status)
            if status.status == "waiting_review":
                break
            await asyncio.sleep(0.01)
        assert status.status == "waiting_review"
        assert status.waiting_reason == f"review:{_REVIEW_ID}"
        await handle.signal(SIGNAL_REVIEW_DECISION, args=[_REVIEW_ID, "approved"])
        result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result.status == "completed"
    assert world.review_resolutions == [
        ResolveBoundToolReviewInput(
            workspace_id=params.workspace_id,
            task_id=params.task_id,
            run_id=f"run-{params.task_id}",
            agent_id=params.agent_id,
            review_id=_REVIEW_ID,
        )
    ]
    assert world.review_projections == [
        CommitReviewProjectionInput(
            workspace_id=params.workspace_id,
            task_id=params.task_id,
            run_id=f"run-{params.task_id}",
            agent_id=params.agent_id,
            review_id=_REVIEW_ID,
            tool_call_id=_TOOL_CALL_ID,
        )
    ]
    resolve_index = world.calls.index((ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW, TOOL_TASK_QUEUE))
    assert world.calls[resolve_index + 1] == (
        ACTIVITY_COMMIT_REVIEW_PROJECTION,
        AGENT_TASK_QUEUE,
    )
    assert world.calls[resolve_index + 2] == (ACTIVITY_RESOLVE_ADVERTISED_TOOLS, TOOL_TASK_QUEUE)


async def test_decision_delivered_before_the_park_still_resumes(env: Any) -> None:
    world = ReviewWorld()
    world.release_commit.clear()
    params = _params()
    agent_worker, tool_worker = _workers(env, world)
    async with agent_worker, tool_worker:
        handle = await _start(env, params)
        # The reviewer decides while the step commit is still in flight.
        await handle.signal(SIGNAL_REVIEW_DECISION, args=[_REVIEW_ID, "changes_requested"])
        world.release_commit.set()
        result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result.status == "completed"
    assert [r.review_id for r in world.review_resolutions] == [_REVIEW_ID]


async def test_approved_review_falls_through_into_the_approval_wait(env: Any) -> None:
    world = ReviewWorld()
    world.review_outcome = "needs_approval"
    params = _params()
    agent_worker, tool_worker = _workers(env, world)
    async with agent_worker, tool_worker:
        handle = await _start(env, params)
        await asyncio.wait_for(world.parked.wait(), timeout=5)
        await handle.signal(SIGNAL_REVIEW_DECISION, args=[_REVIEW_ID, "approved"])
        for _ in range(300):
            status = await handle.query(AgentTaskWorkflow.status)
            if status.status == "waiting_approval":
                break
            await asyncio.sleep(0.01)
        assert status.status == "waiting_approval"
        assert status.waiting_reason == f"approval:{_APPROVAL_ID}"
        await handle.signal("approval_decision", args=[_APPROVAL_ID, "approved"])
        result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result.status == "completed"
    names = [name for name, _queue in world.calls]
    assert names.index(ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW) < names.index(
        ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL
    )
    assert [a.approval_id for a in world.approval_resolutions] == [_APPROVAL_ID]


async def test_cancel_while_parked_finalizes_as_cancelled(env: Any) -> None:
    world = ReviewWorld()
    params = _params()
    agent_worker, tool_worker = _workers(env, world)
    async with agent_worker, tool_worker:
        handle = await _start(env, params)
        await asyncio.wait_for(world.parked.wait(), timeout=5)
        await handle.signal("cancel")
        result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result.status == "cancelled"
    assert world.review_resolutions == []


async def test_reviewer_step_forwards_the_decision_to_the_source_workflow(env: Any) -> None:
    source = ReviewWorld()
    source_params = _params()
    reviewer = ReviewWorld()
    reviewer.park_on_review = False
    reviewer_params = _params()
    reviewer.review_decisions = [
        ReviewDecisionSignal(
            review_id=_REVIEW_ID,
            status="approved",
            source_workflow_id=f"task-{source_params.task_id}",
        )
    ]
    # One worker pair serves both runs; the activities dispatch by name, so
    # route by task id.
    router = _Router(source, reviewer, source_params.task_id)
    agent_worker, tool_worker = _workers(env, router)
    async with agent_worker, tool_worker:
        source_handle = await _start(env, source_params)
        await asyncio.wait_for(source.parked.wait(), timeout=5)
        reviewer_handle = await _start(env, reviewer_params)
        assert (await asyncio.wait_for(reviewer_handle.result(), timeout=10)).status == "completed"
        result = await asyncio.wait_for(source_handle.result(), timeout=10)

    assert result.status == "completed"
    assert [r.review_id for r in source.review_resolutions] == [_REVIEW_ID]


class _Router(ReviewWorld):
    """Dispatch activities to the world owning the run (by task id)."""

    def __init__(self, source: ReviewWorld, reviewer: ReviewWorld, source_task_id: str) -> None:
        super().__init__()
        self._source = source
        self._reviewer = reviewer
        self._source_task_id = source_task_id

    def _pick(self, task_id: str | None = None, run_id: str | None = None) -> ReviewWorld:
        key = task_id if task_id is not None else (run_id or "").removeprefix("run-")
        return self._source if key == self._source_task_id else self._reviewer

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot(self, params: AgentTaskInput) -> SnapshotResult:
        return await self._pick(params.task_id).resolve_snapshot(params)

    @activity.defn(name=ACTIVITY_REASON_AGENT_STEP)
    async def reason_agent_step(self, params: ReasonAgentStepInput) -> ReasonAgentStepResult:
        return await self._pick(params.task_id).reason_agent_step(params)

    @activity.defn(name=ACTIVITY_EXECUTE_BOUND_TOOL)
    async def execute_bound_tool(self, params: ExecuteBoundToolInput) -> BoundToolResult:
        return await self._pick(run_id=params.run_id).execute_bound_tool(params)

    @activity.defn(name=ACTIVITY_COMMIT_AGENT_STEP)
    async def commit_agent_step(self, params: CommitAgentStepInput) -> StepResult:
        return await self._pick(params.task_id).commit_agent_step(params)

    @activity.defn(name=ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW)
    async def resolve_bound_tool_review(
        self, params: ResolveBoundToolReviewInput
    ) -> BoundToolResult:
        return await self._pick(params.task_id).resolve_bound_tool_review(params)

    @activity.defn(name=ACTIVITY_COMMIT_REVIEW_PROJECTION)
    async def commit_review_projection(self, params: CommitReviewProjectionInput) -> StepResult:
        return await self._pick(params.task_id).commit_review_projection(params)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN_PROJECTION)
    async def finalize_run_projection(self, params: FinalizeInput) -> None:
        await self._pick(params.task_id).finalize_run_projection(params)
