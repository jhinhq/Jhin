"""Recovery dispatch uses durable state across worker instances and retries."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from jhin_agent_worker.coordination_activities import CoordinationActivities
from jhin_db.base import Base
from jhin_db.models import Agent, Task, WorkRequest, Workspace
from jhin_domain import new_uuid7


@pytest.fixture
async def outbox():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        workspace = Workspace(name="W", slug="w")
        db.add(workspace)
        await db.flush()
        writer = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        reviewer = Agent(workspace_id=workspace.id, name="Reviewer", slug="reviewer")
        db.add_all([writer, reviewer])
        await db.flush()
        source = Task(
            workspace_id=workspace.id,
            title="Original",
            assigned_agent_id=writer.id,
            state="completed",
            correlation_id=new_uuid7(),
        )
        successor = Task(
            workspace_id=workspace.id,
            title="Continue",
            assigned_agent_id=writer.id,
            state="queued",
            correlation_id=new_uuid7(),
        )
        db.add_all([source, successor])
        await db.flush()
        row = WorkRequest(
            workspace_id=workspace.id,
            requester_agent_id=writer.id,
            target_agent_id=reviewer.id,
            requester_task_id=source.id,
            title="Review",
            status="completed",
            idempotency_key="one",
            continuation_requested_at=datetime.now(UTC),
            continuation_task_id=successor.id,
        )
        db.add(row)
        await db.commit()
        yield maker, row.id, source.id, successor.id
    await engine.dispose()


class Server:
    def __init__(self):
        self.started = set()
        self.lose_ack = True

    async def start_workflow(self, name, params, *, id, task_queue, id_reuse_policy):
        assert id_reuse_policy == WorkflowIDReusePolicy.REJECT_DUPLICATE
        if id in self.started:
            raise WorkflowAlreadyStartedError(id, name)
        self.started.add(id)
        if self.lose_ack:
            raise ConnectionError("response lost after Temporal accepted start")


async def test_restart_recovers_lost_start_ack_without_second_execution(outbox):
    maker, request_id, _source, successor = outbox
    server = Server()
    first = CoordinationActivities(SimpleNamespace(session_factory=maker), server)
    with pytest.raises(ConnectionError):
        await first.dispatch_work_request_continuations()
    async with maker() as db:
        assert (await db.get(WorkRequest, request_id)).continuation_dispatched_at is None
    restarted = CoordinationActivities(SimpleNamespace(session_factory=maker), server)
    assert await restarted.dispatch_work_request_continuations() == 1
    assert await restarted.dispatch_work_request_continuations() == 0
    assert server.started == {f"task-{successor}"}
    async with maker() as db:
        assert (await db.get(WorkRequest, request_id)).continuation_dispatched_at is not None


async def test_cancellation_between_result_commit_and_dispatch_suppresses_start(outbox):
    maker, request_id, source, successor = outbox
    async with maker() as db:
        (await db.get(Task, source)).state = "cancelled"
        await db.commit()
    server = Server()
    worker = CoordinationActivities(SimpleNamespace(session_factory=maker), server)
    assert await worker.dispatch_work_request_continuations() == 0
    assert not server.started
    async with maker() as db:
        assert (await db.get(Task, successor)).state == "cancelled"
        assert (await db.get(WorkRequest, request_id)).continuation_suppressed_reason
