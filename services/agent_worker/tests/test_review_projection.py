"""Agent-side projections of review parking and resumption, mirroring the
approval projection tests: a ``pending_review`` row parks the run as
``waiting_review`` (idempotently), an executed ``organization.review.submit``
is lifted into a review_decision signal, and ``commit_review_projection``
repairs crash gaps without duplicating durable bundles."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from jhin_agent_worker.projections import AgentProjectionActivities
from jhin_agent_worker.reasoning import AgentStepReasoningRecord, AgentStepUsage
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentRun,
    Approval,
    Message,
    RunEvent,
    Task,
    ToolCall,
    WorkReview,
    Workspace,
)
from jhin_domain import (
    ApprovalStatus,
    ReviewMode,
    RunStatus,
    ToolCallStatus,
    WorkReviewStatus,
    new_uuid7,
)
from jhin_observability import noop_metrics, noop_tracer
from jhin_tools import stable_tool_invocation_id
from jhin_workflows.agent_task.shared import (
    CommitAgentStepInput,
    CommitReviewProjectionInput,
    ReviewDecisionSignal,
)

TOOL = "system.demo.destructive"


class _Publisher:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.events.append(envelope)


class _Resources:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.runtime = SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())
        self.session_factory = sessions
        self.publisher = _Publisher()
        self.crypto = None


class World:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.resources = _Resources(sessions)
        self.projections = AgentProjectionActivities(self.resources)  # type: ignore[arg-type]

    async def seed(
        self,
        *,
        tool_status: str,
        review_status: str = WorkReviewStatus.PENDING.value,
        tool_name: str = TOOL,
        output: dict[str, Any] | None = None,
        with_approval: bool = False,
        existing_bundle: bool = False,
    ) -> CommitReviewProjectionInput:
        async with self.sessions() as session:
            workspace = Workspace(name="Review", slug=f"review-{new_uuid7().hex[:8]}")
            session.add(workspace)
            await session.flush()
            manager = Agent(workspace_id=workspace.id, name="Manager", slug="manager")
            session.add(manager)
            await session.flush()
            agent = Agent(
                workspace_id=workspace.id,
                name="Reviewed",
                slug="reviewed",
                manager_agent_id=manager.id,
            )
            session.add(agent)
            await session.flush()
            task = Task(
                workspace_id=workspace.id,
                title="Reviewed task",
                assigned_agent_id=agent.id,
                correlation_id=new_uuid7(),
            )
            session.add(task)
            await session.flush()
            run = AgentRun(
                workspace_id=workspace.id,
                agent_id=agent.id,
                task_id=task.id,
                status=RunStatus.WAITING_REVIEW.value,
            )
            session.add(run)
            await session.flush()
            tool_call_id = stable_tool_invocation_id(run.id, 0, 0)
            review = WorkReview(
                id=new_uuid7(),
                workspace_id=workspace.id,
                task_id=task.id,
                run_id=run.id,
                tool_call_id=tool_call_id,
                subject_agent_id=agent.id,
                trigger_key=f"pre_action:{tool_call_id}:p",
                mode=ReviewMode.PRE_ACTION.value,
                evidence_json={"tool_name": tool_name},
                reviewer_type="agent",
                reviewer_agent_id=manager.id,
                status=review_status,
                verdict=(
                    "approve"
                    if review_status == WorkReviewStatus.APPROVED.value
                    else "changes_requested"
                    if review_status == WorkReviewStatus.CHANGES_REQUESTED.value
                    else None
                ),
                feedback="Looks safe." if review_status != "pending" else "",
                requested_at=run.created_at,
                decided_at=None if review_status == "pending" else run.created_at,
                decided_by_agent_id=None if review_status == "pending" else manager.id,
            )
            session.add(review)
            await session.flush()
            approval_id = None
            if with_approval:
                approval = Approval(
                    workspace_id=workspace.id,
                    task_id=task.id,
                    run_id=run.id,
                    requested_by_agent_id=agent.id,
                    action_type=tool_name,
                    action_payload_sanitized={"risk": "destructive", "provider_call_id": "p-1"},
                    reason="approval required",
                    status=ApprovalStatus.PENDING.value,
                    requested_at=run.created_at,
                )
                session.add(approval)
                await session.flush()
                approval_id = approval.id
            session.add(
                ToolCall(
                    id=tool_call_id,
                    workspace_id=workspace.id,
                    run_id=run.id,
                    agent_id=agent.id,
                    tool_name=tool_name,
                    sanitized_input_json={"label": "once"},
                    sanitized_output_json=output or {},
                    status=tool_status,
                    review_id=review.id,
                    approval_id=approval_id,
                    error_code=(
                        "review_changes_requested"
                        if tool_status == ToolCallStatus.DENIED.value
                        else "execution_outcome_unknown"
                        if tool_status == ToolCallStatus.EXECUTION_UNKNOWN.value
                        else None
                    ),
                )
            )
            session.add_all(
                [
                    RunEvent(
                        workspace_id=workspace.id,
                        run_id=run.id,
                        task_id=task.id,
                        seq=0,
                        event_type="agent.step.tool_manifest",
                        payload_json={
                            "step": 0,
                            "manifest": {
                                "count": 1,
                                "calls": [
                                    {
                                        "ordinal": 0,
                                        "lossless": True,
                                        "tool_name": tool_name,
                                        "arguments_json": '{"label":"once"}',
                                    }
                                ],
                            },
                        },
                    ),
                    RunEvent(
                        workspace_id=workspace.id,
                        run_id=run.id,
                        task_id=task.id,
                        seq=1,
                        event_type="agent.step.reasoning",
                        payload_json=AgentStepReasoningRecord(
                            step=0,
                            completion_sanitized="Use the reviewed tool.",
                            model="review-test",
                            finish_reason="tool_calls",
                            provider_request_id="review-request-1",
                            provider_call_ids=("provider-call-1",),
                            transitions=(),
                            done=False,
                            usage=AgentStepUsage(
                                input_tokens=0, output_tokens=0, cached_tokens=0, cost_micros=0
                            ),
                            latency_ms=0,
                        ).to_payload(),
                    ),
                ]
            )
            if existing_bundle:
                session.add(
                    RunEvent(
                        workspace_id=workspace.id,
                        run_id=run.id,
                        task_id=task.id,
                        seq=2,
                        event_type=f"review.{review_status}",
                        payload_json={"review_id": str(review.id)},
                    )
                )
            await session.commit()
            self.workspace_id = workspace.id
            self.task_id = task.id
            self.run_id = run.id
            self.agent_id = agent.id
            self.tool_call_id = tool_call_id
            self.review_id = review.id
            return CommitReviewProjectionInput(
                workspace_id=str(workspace.id),
                task_id=str(task.id),
                run_id=str(run.id),
                agent_id=str(agent.id),
                review_id=str(review.id),
                tool_call_id=str(tool_call_id),
            )

    def step_params(self) -> CommitAgentStepInput:
        return CommitAgentStepInput(
            workspace_id=str(self.workspace_id),
            task_id=str(self.task_id),
            run_id=str(self.run_id),
            agent_id=str(self.agent_id),
            step_index=0,
            gateway_tool_call_ids=[str(self.tool_call_id)],
        )

    async def counts(self) -> tuple[int, int]:
        async with self.sessions() as session:
            messages = await session.scalar(select(func.count(Message.id))) or 0
            events = await session.scalar(select(func.count(RunEvent.id))) or 0
            return messages, events

    async def run(self) -> AgentRun:
        async with self.sessions() as session:
            run = await session.get(AgentRun, self.run_id)
            assert run is not None
            return run

    async def event_types(self) -> list[str]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(RunEvent.event_type)
                    .where(RunEvent.run_id == self.run_id)
                    .order_by(RunEvent.seq)
                )
            )


@pytest.fixture
async def world() -> AsyncIterator[World]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield World(sessions)
    await engine.dispose()


async def test_pending_review_row_parks_the_run_idempotently(world: World) -> None:
    await world.seed(tool_status=ToolCallStatus.PENDING_REVIEW.value)
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        run.status = RunStatus.RUNNING.value
        await session.commit()

    first = await world.projections.commit_agent_step_activity(world.step_params())
    replay = await world.projections.commit_agent_step_activity(world.step_params())

    assert first.waiting_review_id == str(world.review_id)
    assert first.waiting_approval_id is None and first.done is False
    assert replay.waiting_review_id == first.waiting_review_id
    assert (await world.run()).status == RunStatus.WAITING_REVIEW.value
    events = await world.event_types()
    assert events.count("node.request_review") == 1
    assert events.count("agent.step.committed") == 1
    messages, _ = await world.counts()
    assert messages == 1  # the tool_call message only; no tool_result while parked
    assert [e.event_type for e in world.resources.publisher.events].count(
        "agent.run.waiting_review"
    ) == 1


async def test_pending_review_row_without_review_binding_fails_closed(world: World) -> None:
    await world.seed(tool_status=ToolCallStatus.PENDING_REVIEW.value)
    async with world.sessions() as session:
        row = await session.get(ToolCall, world.tool_call_id)
        assert row is not None
        row.review_id = None
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        run.status = RunStatus.RUNNING.value
        await session.commit()

    with pytest.raises(ApplicationError) as error:
        await world.projections.commit_agent_step_activity(world.step_params())
    assert error.value.type == "tool_projection_binding_mismatch"


async def test_submitted_review_is_lifted_into_a_decision_signal(world: World) -> None:
    await world.seed(
        tool_status=ToolCallStatus.COMPLETED.value,
        review_status=WorkReviewStatus.APPROVED.value,
        tool_name="organization.review.submit",
        output={
            "review_id": "r-1",
            "status": "approved",
            "verdict": "approve",
            "task_id": "t-1",
            "source_workflow_id": "task-t-1",
        },
    )
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        run.status = RunStatus.RUNNING.value
        await session.commit()

    first = await world.projections.commit_agent_step_activity(world.step_params())
    replay = await world.projections.commit_agent_step_activity(world.step_params())

    expected = [
        ReviewDecisionSignal(review_id="r-1", status="approved", source_workflow_id="task-t-1")
    ]
    assert first.review_decisions == expected and replay.review_decisions == expected
    async with world.sessions() as session:
        committed = await session.scalar(
            select(RunEvent).where(RunEvent.event_type == "agent.step.committed")
        )
        assert committed is not None
        assert committed.payload_json["result"]["review_decisions"] == [asdict(expected[0])]


async def test_review_projection_waits_for_the_decision(world: World) -> None:
    params = await world.seed(tool_status=ToolCallStatus.PENDING_REVIEW.value)

    with pytest.raises(ApplicationError) as pending:
        await ActivityEnvironment().run(world.projections.commit_review_projection_activity, params)
    assert pending.value.type == "review_pending" and pending.value.non_retryable is False


async def test_a_reviewed_sandbox_job_reaches_the_timeline(world: World) -> None:
    """As on the approval path: the job that ran because a reviewer let it
    through belongs in the timeline that reviewer will read back."""
    params = await world.seed(
        tool_status=ToolCallStatus.COMPLETED.value,
        review_status=WorkReviewStatus.APPROVED.value,
        tool_name="cli.test.run",
        output={
            "sandbox_job_id": "01a06e90-0000-7000-8000-0000000012cd",
            "command": "bash ./run_tests.sh",
            "status": "completed",
            "exit_code": 0,
            "duration_ms": 88,
            "stdout": "ok",
            "stderr": "",
        },
    )

    await ActivityEnvironment().run(world.projections.commit_review_projection_activity, params)

    types = await world.event_types()
    assert types.count("sandbox.job") == 1, types
    assert types.index("node.execute_tool") < types.index("sandbox.job") < types.index("tool.call")
    async with world.sessions() as session:
        event = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == world.run_id, RunEvent.event_type == "sandbox.job"
            )
        )
    assert event is not None
    assert event.payload_json["sandbox_job_id"] == "01a06e90-0000-7000-8000-0000000012cd"
    assert event.payload_json["command"] == "bash ./run_tests.sh"
    assert event.payload_json["tool_name"] == "cli.test.run"
    assert event.payload_json["after_review"] is True


async def test_review_projection_commits_the_resumed_outcome_once(world: World) -> None:
    params = await world.seed(
        tool_status=ToolCallStatus.COMPLETED.value,
        review_status=WorkReviewStatus.APPROVED.value,
        output={"marker": "executed-after-review"},
    )

    first = await ActivityEnvironment().run(
        world.projections.commit_review_projection_activity, params
    )
    replay = await ActivityEnvironment().run(
        world.projections.commit_review_projection_activity, params
    )

    assert first.done is False and replay.done is False
    assert first.waiting_approval_id is None
    assert (await world.run()).status == RunStatus.RUNNING.value
    messages, _ = await world.counts()
    assert messages == 1
    events = await world.event_types()
    assert events.count("review.approved") == 1
    assert events.count("node.execute_tool") == 1 and events.count("tool.call") == 1
    async with world.sessions() as session:
        message = await session.scalar(select(Message))
        assert message is not None
        assert message.content_json["review_id"] == str(world.review_id)
        assert message.content_json["status"] == "executed"
    resumed = [e for e in world.resources.publisher.events if e.event_type == "agent.run.resumed"]
    assert len(resumed) == 1 and resumed[0].data["review_id"] == str(world.review_id)


async def test_review_projection_replays_an_existing_bundle(world: World) -> None:
    params = await world.seed(
        tool_status=ToolCallStatus.COMPLETED.value,
        review_status=WorkReviewStatus.APPROVED.value,
        existing_bundle=True,
    )
    before = await world.counts()

    result = await ActivityEnvironment().run(
        world.projections.commit_review_projection_activity, params
    )

    assert result.done is False
    assert await world.counts() == before
    assert world.resources.publisher.events == []


async def test_changes_requested_returns_the_feedback_as_the_observation(world: World) -> None:
    params = await world.seed(
        tool_status=ToolCallStatus.DENIED.value,
        review_status=WorkReviewStatus.CHANGES_REQUESTED.value,
    )

    result = await ActivityEnvironment().run(
        world.projections.commit_review_projection_activity, params
    )

    assert result.done is False
    assert (await world.run()).status == RunStatus.RUNNING.value
    async with world.sessions() as session:
        message = await session.scalar(select(Message))
        assert message is not None and message.content_json["status"] == "denied"
    events = await world.event_types()
    assert "review.changes_requested" in events


async def test_approved_review_that_staged_an_approval_parks_on_it(world: World) -> None:
    params = await world.seed(
        tool_status=ToolCallStatus.PENDING_APPROVAL.value,
        review_status=WorkReviewStatus.APPROVED.value,
        with_approval=True,
    )

    first = await ActivityEnvironment().run(
        world.projections.commit_review_projection_activity, params
    )
    replay = await ActivityEnvironment().run(
        world.projections.commit_review_projection_activity, params
    )

    assert first.waiting_approval_id is not None
    assert replay.waiting_approval_id == first.waiting_approval_id
    assert (await world.run()).status == RunStatus.WAITING_APPROVAL.value
    messages, _ = await world.counts()
    assert messages == 0  # no tool_result until the human decides
    events = await world.event_types()
    assert events.count("review.approved") == 1 and events.count("node.request_approval") == 1
    published = [e.event_type for e in world.resources.publisher.events]
    assert published.count("agent.run.waiting_approval") == 1


async def test_execution_unknown_after_review_stops_the_run(world: World) -> None:
    params = await world.seed(
        tool_status=ToolCallStatus.EXECUTION_UNKNOWN.value,
        review_status=WorkReviewStatus.APPROVED.value,
    )

    with pytest.raises(ApplicationError) as error:
        await ActivityEnvironment().run(world.projections.commit_review_projection_activity, params)
    assert error.value.type == "tool_execution_unknown"
    run = await world.run()
    assert run.status == RunStatus.FAILED.value and run.error_code == "tool_execution_unknown"


async def test_review_projection_never_reopens_a_terminal_run(world: World) -> None:
    params = await world.seed(
        tool_status=ToolCallStatus.COMPLETED.value,
        review_status=WorkReviewStatus.APPROVED.value,
    )
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        run.status = RunStatus.CANCELLED.value
        await session.commit()

    with pytest.raises(ApplicationError) as error:
        await ActivityEnvironment().run(world.projections.commit_review_projection_activity, params)
    assert error.value.type == "run_already_terminal"
    assert (await world.run()).status == RunStatus.CANCELLED.value
