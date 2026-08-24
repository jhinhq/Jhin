"""skills.read through the full gateway pipeline against in-memory SQLite:
deny-by-default, workspace and per-agent enablement, grant name-scope
patterns, reference-file fetch, and output bounding."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, AgentSkill, Skill, Task, Workspace
from jhin_domain import TaskState, new_uuid7
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import GatewayOutcome, ToolGateway
from jhin_tools.skills_tools import MAX_READ_CHARS


class Org:
    workspace: Workspace
    me: Agent
    other: Agent
    task: Task

    def gateway(self, session: AsyncSession, agent: Agent) -> ToolGateway:
        ctx = ToolExecutionContext(
            session=session,
            workspace_id=self.workspace.id,
            task_id=self.task.id,
            run_id=new_uuid7(),
            agent_id=agent.id,
            agent_name=agent.name,
        )
        return ToolGateway(ctx, build_builtin_catalog())


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


def _skill(workspace: Workspace, name: str, **overrides: Any) -> Skill:
    values: dict[str, Any] = {
        "workspace_id": workspace.id,
        "name": name,
        "description": f"Description of {name}.",
        "content": f"# {name}\n\nFull instructions for {name}.",
        "files_json": [],
        "source": "custom",
        "enabled": True,
    }
    values.update(overrides)
    return Skill(**values)


@pytest.fixture
async def org(session: AsyncSession) -> Org:
    f = Org()
    f.workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(f.workspace)
    await session.flush()
    ws = f.workspace.id
    f.me = Agent(workspace_id=ws, name="Me", slug="me")
    f.other = Agent(workspace_id=ws, name="Other", slug="other")
    session.add_all([f.me, f.other])
    await session.flush()
    f.task = Task(
        workspace_id=ws,
        title="Task",
        state=TaskState.RUNNING.value,
        assigned_agent_id=f.me.id,
        correlation_id=new_uuid7(),
    )
    release = _skill(
        f.workspace,
        "release-notes",
        files_json=[{"path": "template.md", "content": "# {product} {version}"}],
    )
    updates = _skill(f.workspace, "writing-clear-updates")
    private = _skill(f.workspace, "private-skill")
    switched_off = _skill(f.workspace, "switched-off", enabled=False)
    long_skill = _skill(f.workspace, "long-skill", content="x" * (MAX_READ_CHARS + 6_000))
    session.add_all([f.task, release, updates, private, switched_off, long_skill])
    await session.flush()
    session.add_all(
        [
            AgentSkill(workspace_id=ws, agent_id=f.me.id, skill_id=release.id),
            AgentSkill(workspace_id=ws, agent_id=f.me.id, skill_id=updates.id),
            AgentSkill(workspace_id=ws, agent_id=f.me.id, skill_id=switched_off.id),
            AgentSkill(workspace_id=ws, agent_id=f.me.id, skill_id=long_skill.id),
            AgentSkill(workspace_id=ws, agent_id=f.other.id, skill_id=private.id),
        ]
    )
    await session.flush()
    return f


async def grant(
    session: AsyncSession, org: Org, agent: Agent, scope: dict[str, Any] | None = None
) -> None:
    session.add(
        AgentCapabilityGrant(
            workspace_id=org.workspace.id,
            agent_id=agent.id,
            capability="skills.read",
            scope_json=scope or {},
            effect="allow",
        )
    )
    await session.flush()


async def read(session: AsyncSession, org: Org, agent: Agent, **body: Any) -> GatewayOutcome:
    return await org.gateway(session, agent).request("skills.read", json.dumps(body))


class TestSkillsRead:
    async def test_denied_without_grant(self, session: AsyncSession, org: Org) -> None:
        outcome = await read(session, org, org.me, name="release-notes")
        assert outcome.status == "denied"

    async def test_reads_instructions_and_lists_files(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me)
        outcome = await read(session, org, org.me, name="release-notes")
        assert outcome.status == "executed", outcome.decision_reason
        output = outcome.sanitized_output or {}
        assert output["name"] == "release-notes"
        assert "Full instructions for release-notes" in output["content"]
        assert output["files"] == ["template.md"]
        assert output["truncated"] is False
        assert output["version"] == 1

    async def test_reads_a_reference_file(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me)
        outcome = await read(session, org, org.me, name="release-notes", file="template.md")
        assert outcome.status == "executed"
        assert (outcome.sanitized_output or {})["content"] == "# {product} {version}"

    async def test_missing_file_fails_without_execution(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me)
        outcome = await read(session, org, org.me, name="release-notes", file="nope.md")
        assert outcome.status != "executed"

    async def test_skill_enabled_for_another_agent_is_invisible(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me)
        outcome = await read(session, org, org.me, name="private-skill")
        assert outcome.status != "executed"

    async def test_workspace_disabled_skill_is_invisible(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me)
        outcome = await read(session, org, org.me, name="switched-off")
        assert outcome.status != "executed"

    async def test_unknown_name_fails(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me)
        outcome = await read(session, org, org.me, name="does-not-exist")
        assert outcome.status != "executed"

    async def test_grant_scope_pattern_limits_readable_names(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, scope={"name": "release-*"})
        allowed = await read(session, org, org.me, name="release-notes")
        assert allowed.status == "executed", allowed.decision_reason
        denied = await read(session, org, org.me, name="writing-clear-updates")
        assert denied.status == "denied"

    async def test_long_content_is_paged_with_a_flag(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me)
        outcome = await read(session, org, org.me, name="long-skill")
        assert outcome.status == "executed"
        output = outcome.sanitized_output or {}
        assert output["truncated"] is True
        assert len(output["content"]) == MAX_READ_CHARS
        # Paging: the final page returns the remainder, unflagged.
        last = await read(session, org, org.me, name="long-skill", offset=MAX_READ_CHARS)
        last_output = last.sanitized_output or {}
        assert last_output["truncated"] is False
        assert len(last_output["content"]) == 6_000

    async def test_malformed_input_is_rejected(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me)
        outcome = await org.gateway(session, org.me).request(
            "skills.read", json.dumps({"name": "release-notes", "extra": True})
        )
        assert outcome.status != "executed"
