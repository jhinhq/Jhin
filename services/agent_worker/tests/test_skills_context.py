"""skills_prompt_context: deny-by-default enablement joins and rendering."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_agent_worker.skills_activities import skills_prompt_context
from jhin_db.base import Base
from jhin_db.models import Agent, AgentSkill, Skill, Workspace
from jhin_domain import new_uuid7


@pytest.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_lists_only_skills_enabled_for_the_agent(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        me = Agent(workspace_id=workspace.id, name="Me", slug="me")
        other = Agent(workspace_id=workspace.id, name="Other", slug="other")
        session.add_all([me, other])
        await session.flush()

        def skill(name: str, enabled: bool = True) -> Skill:
            return Skill(
                workspace_id=workspace.id,
                name=name,
                description=f"About {name}.",
                content="body",
                source="custom",
                enabled=enabled,
            )

        mine = skill("release-notes")
        disabled = skill("switched-off", enabled=False)
        theirs = skill("private-skill")
        session.add_all([mine, disabled, theirs])
        await session.flush()
        session.add_all(
            [
                AgentSkill(workspace_id=workspace.id, agent_id=me.id, skill_id=mine.id),
                AgentSkill(workspace_id=workspace.id, agent_id=me.id, skill_id=disabled.id),
                AgentSkill(workspace_id=workspace.id, agent_id=other.id, skill_id=theirs.id),
            ]
        )
        await session.flush()

        block = await skills_prompt_context(session, workspace.id, me.id)
        assert "Skills available to you" in block
        assert "- release-notes — About release-notes." in block
        assert "switched-off" not in block
        assert "private-skill" not in block
        assert "skills.read" in block

        assert await skills_prompt_context(session, workspace.id, other.id) != ""
        empty_agent = Agent(workspace_id=workspace.id, name="None", slug="none")
        session.add(empty_agent)
        await session.flush()
        assert await skills_prompt_context(session, workspace.id, empty_agent.id) == ""
