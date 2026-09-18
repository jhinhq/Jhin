"""Real Temporal continuation with production admission, finalization and outbox.

Model snapshot/steps are deterministic stubs. The PostgreSQL variant exercises
real admission locks when TEST_DATABASE_URL points to an isolated migrated DB.
"""

import asyncio
import os
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.coordination_activities import CoordinationActivities
from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, Message, Task, WorkRequest, Workspace
from jhin_domain import new_uuid7
from jhin_observability import noop_metrics, noop_tracer
from jhin_workflows.agent_task import AgentTaskInput, AgentTaskWorkflow, StepResult
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_COMMIT_AGENT_STEP,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    WorkRequestStart,
)
from jhin_workflows.task_queues import AGENT_TASK_QUEUE, TOOL_TASK_QUEUE
from jhin_workflows.work_request_task import WorkRequestTaskInput, WorkRequestTaskWorkflow


@workflow.defn(name="WorkRequestTaskWorkflow")
class DelayedReview:
    @workflow.run
    async def run(self, params: WorkRequestTaskInput):
        await workflow.sleep(timedelta(minutes=3))
        return await WorkRequestTaskWorkflow().run(params)


class Steps:
    def __init__(self, source, child, request, reviewer):
        self.source, self.child, self.request, self.reviewer = source, child, request, reviewer
        self.counts = Counter()

    @activity.defn(name=ACTIVITY_REASON_AGENT_STEP)
    async def reason(self, params: ReasonAgentStepInput) -> ReasonAgentStepResult:
        return ReasonAgentStepResult(call_count=0)

    @activity.defn(name=ACTIVITY_COMMIT_AGENT_STEP)
    async def commit(self, params: CommitAgentStepInput) -> StepResult:
        self.counts[params.task_id] += 1
        if params.task_id == str(self.source):
            return StepResult(
                done=False,
                work_request_starts=[
                    WorkRequestStart(
                        work_request_id=str(self.request),
                        task_id=str(self.child),
                        agent_id=str(self.reviewer),
                        side="requester",
                    )
                ],
            )
        return StepResult(done=True)

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def advertised(self, params: ResolveAdvertisedToolsInput) -> list:
        return []

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup(self, params: CleanupRunWorkspaceInput) -> CleanupRunWorkspaceResult:
        return CleanupRunWorkspaceResult(deleted=True)


class Publisher:
    async def publish(self, _event):
        pass


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_late_result_runs_one_successor_after_source_releases_only_workspace_slot(
    monkeypatch,
    backend,
):
    url = os.environ.get("TEST_DATABASE_URL") if backend == "postgres" else "sqlite+aiosqlite://"
    if not url:
        pytest.skip("isolated TEST_DATABASE_URL required")
    engine = create_async_engine(url)
    if backend == "sqlite":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        workspace = Workspace(
            name="Durable",
            slug=f"durable-{new_uuid7().hex}",
            settings_json={"concurrency": {"max_concurrent_runs": 1}},
        )
        db.add(workspace)
        await db.flush()
        writer = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        reviewer = Agent(workspace_id=workspace.id, name="Reviewer", slug="reviewer")
        db.add_all([writer, reviewer])
        await db.flush()
        source = Task(
            workspace_id=workspace.id,
            title="Article",
            assigned_agent_id=writer.id,
            state="queued",
            correlation_id=new_uuid7(),
        )
        child = Task(
            workspace_id=workspace.id,
            title="Review",
            assigned_agent_id=reviewer.id,
            state="queued",
            correlation_id=new_uuid7(),
            metadata_json={"reported_result": {"summary": "Revise the introduction"}},
        )
        db.add_all([source, child])
        await db.flush()
        request = WorkRequest(
            workspace_id=workspace.id,
            requester_agent_id=writer.id,
            requester_task_id=source.id,
            target_agent_id=reviewer.id,
            created_task_id=child.id,
            title="Review",
            status="accepted",
            idempotency_key="one",
        )
        db.add(request)
        await db.commit()

    async def snapshot(*_args, **_kwargs):
        return SimpleNamespace(
            model_profile=SimpleNamespace(
                profile_id=None, display_name="Fixture", model_name="fixture"
            ),
            name="Fixture",
            snapshot_hash=lambda: "fixture",
            model_dump_json=lambda: "{}",
            run_limits=SimpleNamespace(max_steps=3),
        )

    monkeypatch.setattr("jhin_agent_worker.activities.resolve_snapshot", snapshot)

    async def skip_memory(*_args, **_kwargs):
        return "disabled_in_fixture", None

    monkeypatch.setattr(
        "jhin_agent_worker.coordination_activities.start_memory_maintenance", skip_memory
    )
    resources = SimpleNamespace(
        session_factory=maker,
        crypto=None,
        publisher=Publisher(),
        runtime=SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer()),
    )
    agents = AgentActivities(resources)
    steps = Steps(source.id, child.id, request.id, reviewer.id)
    env = await WorkflowEnvironment.start_time_skipping()
    coordination = CoordinationActivities(resources, env.client)
    try:
        async with (
            Worker(
                env.client,
                task_queue=AGENT_TASK_QUEUE,
                workflows=[AgentTaskWorkflow, DelayedReview],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=[
                    agents.resolve_snapshot_activity,
                    agents.finalize_run_projection_activity,
                    steps.reason,
                    steps.commit,
                    coordination.prepare_work_request_continuation_activity,
                    coordination.finalize_work_request_activity,
                ],
            ),
            Worker(
                env.client, task_queue=TOOL_TASK_QUEUE, activities=[steps.advertised, steps.cleanup]
            ),
        ):
            result = await env.client.execute_workflow(
                AgentTaskWorkflow.run,
                AgentTaskInput(
                    workspace_id=str(workspace.id), task_id=str(source.id), agent_id=str(writer.id)
                ),
                id=f"task-{source.id}",
                task_queue=AGENT_TASK_QUEUE,
            )
            assert result.status == "completed"
            async with maker() as db:
                assert (await db.get(WorkRequest, request.id)).continuation_task_id is None
                assert (await db.get(AgentRun, UUID(result.run_id))).status == "completed"
            review_handle = env.client.get_workflow_handle(f"work-request-{request.id}")
            advance = asyncio.create_task(env.sleep(timedelta(minutes=3)))
            review_description = await review_handle.describe()
            await asyncio.wait_for(
                env.client.get_workflow_handle(
                    review_handle.id, run_id=review_description.run_id
                ).result(),
                timeout=15,
            )
            advance.cancel()
            await asyncio.gather(advance, return_exceptions=True)
            completed_review = await review_handle.describe()
            assert completed_review.close_time - completed_review.start_time >= timedelta(minutes=3)
            async with maker() as db:
                successor = (await db.get(WorkRequest, request.id)).continuation_task_id
                assert successor is not None
            successor_handle = env.client.get_workflow_handle(f"task-{successor}")
            successor_description = await successor_handle.describe()
            await asyncio.wait_for(
                env.client.get_workflow_handle(
                    successor_handle.id, run_id=successor_description.run_id
                ).result(),
                timeout=15,
            )
            assert await coordination.dispatch_work_request_continuations() == 0
            assert steps.counts == {str(source.id): 1, str(child.id): 1, str(successor): 1}
            async with maker() as db:
                runs = list(
                    await db.scalars(
                        select(AgentRun)
                        .where(AgentRun.workspace_id == workspace.id)
                        .order_by(AgentRun.started_at)
                    )
                )
                assert len(runs) == 3
                assert all(run.status == "completed" for run in runs)
                assert all(
                    runs[index].completed_at <= runs[index + 1].started_at for index in range(2)
                )
                assert (
                    len(
                        list(
                            await db.scalars(
                                select(Message).where(
                                    Message.workspace_id == workspace.id,
                                    Message.message_type == "result",
                                )
                            )
                        )
                    )
                    == 1
                )
    finally:
        await env.shutdown()
        async with maker() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await db.commit()
        await engine.dispose()
