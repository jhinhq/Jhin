"""Concurrent result delivery against an isolated, migrated PostgreSQL database."""

import asyncio
import os

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.models import Agent, Message, Task, WorkRequest, Workspace
from jhin_domain import new_uuid7
from jhin_tools.work_requests import finalize_work_request, prepare_work_request_continuation

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated TEST_DATABASE_URL required")


async def test_concurrent_result_finalizers_and_registration_create_one_continuation():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        workspace = Workspace(name="Result race", slug=f"result-race-{new_uuid7().hex}")
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
        child = Task(
            workspace_id=workspace.id,
            title="Review",
            assigned_agent_id=reviewer.id,
            state="completed",
            correlation_id=new_uuid7(),
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
            idempotency_key="one",
            status="accepted",
        )
        db.add(request)
        await db.commit()
    try:

        async def deliver(register):
            async with maker() as db:
                if register:
                    await prepare_work_request_continuation(
                        db,
                        workspace_id=workspace.id,
                        request_id=request.id,
                        requester_task_id=source.id,
                        requester_agent_id=writer.id,
                    )
                else:
                    await finalize_work_request(
                        db, workspace_id=workspace.id, request_id=request.id, run_status="completed"
                    )
                await db.commit()

        await asyncio.gather(*(deliver(index % 2) for index in range(8)))
        async with maker() as db:
            row = await db.get(WorkRequest, request.id)
            assert row.continuation_task_id is not None
            tasks = list(await db.scalars(select(Task).where(Task.workspace_id == workspace.id)))
            results = list(
                await db.scalars(
                    select(Message).where(
                        Message.workspace_id == workspace.id, Message.message_type == "result"
                    )
                )
            )
            assert len(tasks) == 3
            assert len(results) == 1
    finally:
        async with maker() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await db.commit()
        await engine.dispose()
