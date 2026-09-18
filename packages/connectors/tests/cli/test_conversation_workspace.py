from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_connectors.cli.workspace import bind_workspace, release_run_bindings
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentRun,
    Conversation,
    SandboxJob,
    SandboxWorkspace,
    Task,
    Workspace,
)
from jhin_tools.errors import ToolExecutionError


@pytest.mark.asyncio
async def test_chats_isolate_files_and_finalize_retains_the_disk(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    w, a, c1, c2, t1, t2, r1, r2 = [uuid4() for _ in range(8)]
    async with factory() as db:
        db.add(Workspace(id=w, name="test", slug="test"))
        db.add(
            Agent(id=a, workspace_id=w, name="same agent", slug="same-agent", role_title="worker")
        )
        for cid, tid, rid in [(c1, t1, r1), (c2, t2, r2)]:
            db.add(
                Conversation(
                    id=cid,
                    workspace_id=w,
                    title="chat",
                    primary_agent_id=a,
                    workspace_version=1,
                    last_activity_at=datetime.now(UTC),
                )
            )
            db.add(
                Task(
                    id=tid,
                    workspace_id=w,
                    conversation_id=cid,
                    title="test",
                    correlation_id=uuid4(),
                )
            )
            db.add(AgentRun(id=rid, workspace_id=w, agent_id=a, task_id=tid, status="running"))
        await db.commit()
    first = await bind_workspace(factory, workspace_id=w, agent_id=a, run_id=r1)
    second = await bind_workspace(factory, workspace_id=w, agent_id=a, run_id=r2)
    assert first.key != second.key
    assert first.kind == second.kind == "conversation"
    child_task, child_run = uuid4(), uuid4()
    async with factory() as db:
        db.add(
            Task(
                id=child_task,
                workspace_id=w,
                conversation_id=c1,
                parent_task_id=t1,
                title="delegate",
                correlation_id=uuid4(),
            )
        )
        db.add(
            AgentRun(id=child_run, workspace_id=w, agent_id=a, task_id=child_task, status="running")
        )
        await db.commit()
    colleague = await bind_workspace(factory, workspace_id=w, agent_id=a, run_id=child_run)
    assert colleague.kind == "delegated" and colleague.key not in {first.key, second.key}
    deleted = []

    async def delete(key):
        deleted.append(key)
        return True

    capture = AsyncMock()
    monkeypatch.setattr("jhin_connectors.cli.chat_snapshots.capture_run_outputs", capture)
    # A failed model turn may have abandoned a still-running command. Neither
    # cleanup nor the next turn may hand its files to a new writer yet.
    async with factory() as db:
        old_run = await db.get(AgentRun, r1)
        old_run.status = "failed"
        job = SandboxJob(workspace_id=w, run_id=r1, task_id=t1, image="test", status="running")
        db.add(job)
        await db.commit()
        job_id = job.id
    await release_run_bindings(factory, workspace_id=w, run_id=r1, delete_workspace=delete)
    capture.assert_not_awaited()
    async with factory() as db:
        held = await db.get(SandboxWorkspace, first.row_id)
        assert held.holder_run_id == r1
        next_run = uuid4()
        db.add(AgentRun(id=next_run, workspace_id=w, agent_id=a, task_id=t1, status="running"))
        await db.commit()
    with pytest.raises(ToolExecutionError, match="confirmed completion"):
        await bind_workspace(factory, workspace_id=w, agent_id=a, run_id=next_run)
    async with factory() as db:
        pending = await db.get(SandboxJob, job_id)
        pending.status = "completed"
        await db.commit()
    await release_run_bindings(factory, workspace_id=w, run_id=r1, delete_workspace=delete)
    # Production passes a deletion callback too: that must not suppress capture.
    capture.assert_awaited_once_with(factory, w, r1)
    assert deleted == []
    async with factory() as db:
        row = await db.scalar(select(SandboxWorkspace).where(SandboxWorkspace.id == first.row_id))
        assert row is not None and row.holder_run_id is None
        row.holder_user_id = uuid4()
        await db.commit()
    with pytest.raises(ToolExecutionError) as error:
        await bind_workspace(factory, workspace_id=w, agent_id=a, run_id=r1)
    assert error.value.code == "workspace_human_control"
    await engine.dispose()
