"""memory.search / memory.propose through the full gateway pipeline against
in-memory SQLite: deny-by-default, scoped to the calling agent, and policy
routed (never activates workspace memory, never amplifies visibility)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, MemoryRecord, Task, Team, Workspace
from jhin_domain import MemoryScope, MemoryStatus, TaskState, new_uuid7
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import GatewayOutcome, ToolGateway


class Org:
    workspace: Workspace
    team: Team
    me: Agent
    other: Agent
    task: Task
    team_task: Task

    def gateway(self, session: AsyncSession, agent: Agent, task: Task | None = None) -> ToolGateway:
        ctx = ToolExecutionContext(
            session=session,
            workspace_id=self.workspace.id,
            task_id=(task or self.task).id,
            run_id=new_uuid7(),
            agent_id=agent.id,
            agent_name=agent.name,
        )
        return ToolGateway(ctx, build_builtin_catalog())


@pytest.fixture
async def session() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
async def org(session: AsyncSession) -> Org:
    f = Org()
    f.workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(f.workspace)
    await session.flush()
    ws = f.workspace.id
    f.team = Team(workspace_id=ws, name="Engineering")
    session.add(f.team)
    await session.flush()
    f.me = Agent(workspace_id=ws, team_id=f.team.id, name="Me", slug="me")
    f.other = Agent(workspace_id=ws, team_id=f.team.id, name="Other", slug="other")
    session.add_all([f.me, f.other])
    await session.flush()
    f.task = Task(
        workspace_id=ws,
        title="Private task",
        state=TaskState.RUNNING.value,
        assigned_agent_id=f.me.id,
        correlation_id=new_uuid7(),
    )
    f.team_task = Task(
        workspace_id=ws,
        title="Team task",
        state=TaskState.RUNNING.value,
        assigned_agent_id=f.me.id,
        assigned_team_id=f.team.id,
        correlation_id=new_uuid7(),
    )
    session.add_all([f.task, f.team_task])
    await session.flush()
    return f


async def grant(session: AsyncSession, org: Org, agent: Agent, capability: str) -> None:
    session.add(
        AgentCapabilityGrant(
            workspace_id=org.workspace.id,
            agent_id=agent.id,
            capability=capability,
            scope_json={},
            effect="allow",
        )
    )
    await session.flush()


async def seed(
    session: AsyncSession, org: Org, content: str, *, scope: MemoryScope, scope_id: Any
) -> MemoryRecord:
    record = MemoryRecord(
        workspace_id=org.workspace.id,
        scope=scope.value,
        scope_id=scope_id,
        kind="fact",
        content=content,
        content_hash=new_uuid7().hex,
        visibility=scope.value,
        status=MemoryStatus.ACTIVE.value,
        created_by_type="agent",
    )
    session.add(record)
    await session.flush()
    return record


async def search(session: AsyncSession, org: Org, agent: Agent, query: str) -> GatewayOutcome:
    return await org.gateway(session, agent).request(
        "memory.search", json.dumps({"query": query, "limit": 10})
    )


async def propose(
    session: AsyncSession, org: Org, agent: Agent, task: Task | None = None, **body: Any
) -> GatewayOutcome:
    payload = {"content": "We deploy on Tuesdays.", **body}
    return await org.gateway(session, agent, task).request("memory.propose", json.dumps(payload))


class TestSearch:
    async def test_denied_without_grant(self, session: AsyncSession, org: Org) -> None:
        outcome = await search(session, org, org.me, "deploy")
        assert outcome.status == "denied"

    async def test_returns_only_authorized_records(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.read")
        mine = await seed(
            session, org, "my deploy note", scope=MemoryScope.AGENT, scope_id=org.me.id
        )
        theirs = await seed(
            session, org, "their deploy note", scope=MemoryScope.AGENT, scope_id=org.other.id
        )
        team = await seed(
            session, org, "team deploy note", scope=MemoryScope.TEAM, scope_id=org.team.id
        )
        outcome = await search(session, org, org.me, "deploy note")
        assert outcome.status == "executed", outcome.decision_reason
        ids = {item["id"] for item in (outcome.sanitized_output or {})["items"]}
        assert ids == {str(mine.id), str(team.id)}
        assert str(theirs.id) not in ids
        assert (outcome.sanitized_output or {})["degraded"] is True

    async def test_rejects_malformed_input(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.read")
        outcome = await org.gateway(session, org.me).request(
            "memory.search", json.dumps({"query": "x", "agent_id": str(org.other.id)})
        )
        assert outcome.status != "executed"


class TestPropose:
    async def test_denied_without_grant(self, session: AsyncSession, org: Org) -> None:
        outcome = await propose(session, org, org.me)
        assert outcome.status == "denied"

    async def test_private_memory_activates(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, subject="deploy.day")
        assert outcome.status == "executed", outcome.decision_reason
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "activate"
        assert output["status"] == "active"
        record = await session.get(MemoryRecord, __import__("uuid").UUID(output["memory_id"]))
        assert record is not None
        assert record.scope == "agent"
        assert record.scope_id == org.me.id
        assert record.source_task_id == org.task.id
        assert record.created_by_type == "agent"
        assert record.created_by_id == org.me.id

    async def test_team_scope_from_private_task_is_rejected(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, requested_scope="team")
        assert outcome.status == "executed"
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "reject"
        assert "non_amplification" in output["reasons"]
        assert (await session.scalar(select(MemoryRecord))) is None

    async def test_team_scope_from_team_task_activates(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, org.team_task, requested_scope="team")
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "activate"
        record = await session.scalar(select(MemoryRecord))
        assert record is not None
        assert record.scope == "team"
        assert record.scope_id == org.team.id

    async def test_workspace_scope_never_activates_directly(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, org.team_task, requested_scope="workspace")
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "reject"  # team-visible source < workspace
        assert (await session.scalar(select(MemoryRecord))) is None

    async def test_secret_is_rejected(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(
            session, org, org.me, content="API key: sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        )
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "reject"
        assert any(r.startswith("secret:") for r in output["reasons"])

    async def test_duplicate_reports_existing(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        first = (await propose(session, org, org.me)).sanitized_output or {}
        second = (await propose(session, org, org.me)).sanitized_output or {}
        assert second["outcome"] == "duplicate"
        assert second["memory_id"] == first["memory_id"]

    async def test_model_cannot_set_status(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, status="active")
        assert outcome.status != "executed"
