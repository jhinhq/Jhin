"""Durable result delivery survives ordering, retries, and cancelled turns."""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, Message, Task, WorkRequest, Workspace
from jhin_domain import new_uuid7
from jhin_tools.work_requests import finalize_work_request, prepare_work_request_continuation


@pytest.fixture
async def world():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        workspace = Workspace(name="W", slug="w")
        db.add(workspace)
        await db.flush()
        writer = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        reviewer = Agent(workspace_id=workspace.id, name="Reviewer", slug="reviewer")
        db.add_all([writer, reviewer])
        await db.flush()
        source = Task(
            workspace_id=workspace.id,
            title="Article",
            description="Original brief",
            assigned_agent_id=writer.id,
            state="completed",
            correlation_id=new_uuid7(),
        )
        child = Task(
            workspace_id=workspace.id,
            title="Review",
            assigned_agent_id=reviewer.id,
            state="completed",
            correlation_id=new_uuid7(),
            metadata_json={"reported_result": {"summary": "Revise the introduction"}},
        )
        db.add_all([source, child])
        await db.flush()
        request = WorkRequest(
            workspace_id=workspace.id,
            requester_agent_id=writer.id,
            target_agent_id=reviewer.id,
            requester_task_id=source.id,
            created_task_id=child.id,
            title="Review article",
            status="accepted",
            idempotency_key="one",
            metadata_json={"revision_round": 1},
        )
        db.add(request)
        await db.commit()
        yield db, workspace, source, request
    await engine.dispose()


@pytest.mark.parametrize("result_first", [False, True])
async def test_result_and_continuation_claim_survive_either_order_and_retries(world, result_first):
    db, workspace, source, request = world

    async def prepare():
        return await prepare_work_request_continuation(
            db,
            workspace_id=workspace.id,
            request_id=request.id,
            requester_task_id=source.id,
            requester_agent_id=request.requester_agent_id,
        )

    async def finish():
        return await finalize_work_request(
            db, workspace_id=workspace.id, request_id=request.id, run_status="completed"
        )

    if result_first:
        await finish()
        assert request.continuation_task_id is None
    await prepare()
    await finish()
    await db.commit()  # a new dispatch can recover solely from committed database state
    for _ in range(2):
        await prepare()
        await finish()
    tasks = list((await db.scalars(select(Task))).all())
    assert len(tasks) == 3
    continuation = await db.get(Task, request.continuation_task_id)
    assert continuation.assigned_agent_id == request.requester_agent_id
    assert continuation.state == "queued"
    assert continuation.metadata_json["queue_after_task_id"] == str(source.id)
    assert (
        continuation.metadata_json["work_request_result"]["result_message_id"]
        == request.metadata_json["result_message_id"]
    )
    assert continuation.metadata_json["revision_round"] == 1
    assert "Original brief" in continuation.description
    assert "Revise the introduction" in continuation.description
    results = list(
        (await db.scalars(select(Message).where(Message.message_type == "result"))).all()
    )
    assert len(results) == 1
    assert request.continuation_dispatched_at is None


@pytest.mark.parametrize("cancel", ["cancelled", "stop_requested_at"])
async def test_cancelled_requester_receives_evidence_but_is_not_revived(world, cancel):
    db, workspace, source, request = world
    await prepare_work_request_continuation(
        db,
        workspace_id=workspace.id,
        request_id=request.id,
        requester_task_id=source.id,
        requester_agent_id=request.requester_agent_id,
    )
    if cancel == "cancelled":
        source.state = "cancelled"
    else:
        source.metadata_json = {"stop_requested_at": "2026-09-15T12:00:00Z"}
    await db.flush()
    await finalize_work_request(
        db, workspace_id=workspace.id, request_id=request.id, run_status="completed"
    )
    assert request.continuation_task_id is None
    assert request.continuation_suppressed_reason == "requester_cancelled"
    assert request.metadata_json["result_message_id"]


async def test_another_task_cannot_arm_the_requesters_continuation(world):
    db, workspace, _source, request = world
    with pytest.raises(ValueError, match="requester"):
        await prepare_work_request_continuation(
            db,
            workspace_id=workspace.id,
            request_id=request.id,
            requester_task_id=UUID(int=999),
            requester_agent_id=request.requester_agent_id,
        )
