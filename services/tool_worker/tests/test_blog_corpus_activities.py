from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_connectors.ghost.client import GhostApiError
from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, Connection, Task, Workspace
from jhin_db.models.blog_corpus import BlogCorpusSync
from jhin_db.models.editorial import EditorialAssignment
from jhin_domain import new_uuid7
from jhin_tool_worker.blog_corpus_activities import BlogCorpusActivities
from jhin_workflows.blog_corpus.shared import BlogCorpusSyncInput


@pytest.fixture
async def corpus_worker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        workspace = Workspace(name="Corpus", slug="corpus")
        db.add(workspace)
        await db.flush()
        agent = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        connection = Connection(
            workspace_id=workspace.id,
            connector_type="ghost",
            name="Ghost",
            auth_type="api_key",
            config_json={},
        )
        db.add_all([agent, connection])
        await db.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Article",
            assigned_agent_id=agent.id,
            state="running",
            correlation_id=new_uuid7(),
        )
        db.add(task)
        await db.flush()
        assignment = EditorialAssignment(
            workspace_id=workspace.id,
            connection_id=connection.id,
            writer_agent_id=agent.id,
            publisher_agent_id=new_uuid7(),
        )
        grant = AgentCapabilityGrant(
            workspace_id=workspace.id,
            agent_id=agent.id,
            capability="ghost.archive.sync",
            effect="allow",
            scope_json={"connection_id": str(connection.id)},
        )
        db.add_all([assignment, grant])
        await db.flush()
        sync = BlogCorpusSync(
            workspace_id=workspace.id,
            connection_id=connection.id,
            assignment_id=assignment.id,
            agent_id=agent.id,
            task_id=task.id,
            run_id=new_uuid7(),
            active_key=str(connection.id),
        )
        db.add(sync)
        await db.commit()

    async def api(*_args):
        return connection, "http://fixture", "synthetic"

    monkeypatch.setattr("jhin_tool_worker.blog_corpus_activities._api", api)
    worker = BlogCorpusActivities(SimpleNamespace(session_factory=maker, crypto=None))
    yield worker, maker, sync, task
    await engine.dispose()


async def test_failed_page_retries_checkpoint_and_new_worker_resumes(corpus_worker, monkeypatch):
    worker, maker, sync, _task = corpus_worker
    attempts = []

    async def request(*_args, params):
        attempts.append(params["page"])
        if len(attempts) == 1:
            raise GhostApiError("Rate limited", status_code=429)
        return {"posts": [], "meta": {"pagination": {"page": 1, "total": 0, "next": None}}}

    monkeypatch.setattr("jhin_tool_worker.blog_corpus_activities.ghost_request", request)
    params = BlogCorpusSyncInput(str(sync.workspace_id), str(sync.id))
    with pytest.raises(GhostApiError):
        await worker.advance(params)
    assert (await worker.advance(params)).status == "running"
    restarted = BlogCorpusActivities(SimpleNamespace(session_factory=maker, crypto=None))
    assert (await restarted.advance(params)).status == "complete"
    assert attempts == [1, 1, 1]


async def test_archive_fetch_limits_full_body_pages_without_duplicate_lexical_payload(
    corpus_worker, monkeypatch
):
    worker, _maker, sync, _task = corpus_worker
    requested = []

    async def request(*_args, params):
        requested.append(params)
        # Real provider pages retain full HTML; no sampling/truncation fallback.
        assert params["limit"] <= 20
        assert params["formats"] == "html"
        assert params["filter"] == "status:published"
        assert params["order"] == "id asc"
        return {"posts": [], "meta": {"pagination": {"page": 1, "total": 0, "next": None}}}

    monkeypatch.setattr("jhin_tool_worker.blog_corpus_activities.ghost_request", request)
    params = BlogCorpusSyncInput(str(sync.workspace_id), str(sync.id))
    assert (await worker.advance(params)).status == "running"
    assert (await worker.advance(params)).status == "complete"
    assert len(requested) == 2


@pytest.mark.parametrize("revocation", ["grant", "task"])
async def test_current_authority_rechecked_before_background_fetch(
    corpus_worker, monkeypatch, revocation
):
    worker, maker, sync, task = corpus_worker
    calls = []

    async def request(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("revoked actor must make zero provider calls")

    monkeypatch.setattr("jhin_tool_worker.blog_corpus_activities.ghost_request", request)
    async with maker() as db:
        if revocation == "grant":
            await db.execute(delete(AgentCapabilityGrant))
        else:
            (await db.get(Task, task.id)).metadata_json = {"stop_requested_at": "now"}
        await db.commit()
    result = await worker.advance(BlogCorpusSyncInput(str(sync.workspace_id), str(sync.id)))
    assert result.status == "failed"
    assert calls == []
