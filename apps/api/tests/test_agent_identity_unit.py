"""The human path to an agent's name: the HTTP schemas and the rename audit.

An agent's name is asserted unhedged in layer 1 of its own system prompt and
read back by every colleague through the roster. The agent's own tool holds
it to ``jhin_policy.agent_name_problem``; until now the admin-gated
``PATCH /agents/{id}`` checked only "1 to 200 characters", so the two writers
of one column disagreed about what a name is. They no longer do.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents import service
from jhin_api.agents.schemas import AgentCreate, AgentUpdate
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, AuditEvent, Workspace
from jhin_domain import new_uuid7


def _request_meta() -> dict[str, Any]:
    return {"request_id": new_uuid7(), "ip_hash": "test-ip-hash"}


async def _agent(session: AsyncSession, workspace_id: UUID, name: str) -> Agent:
    agent = Agent(
        workspace_id=workspace_id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{new_uuid7().hex[:6]}",
    )
    session.add(agent)
    await session.flush()
    return agent


async def _audits(session: AsyncSession, action: str, target_id: UUID) -> list[AuditEvent]:
    return list(
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == action, AuditEvent.target_id == target_id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
    )


class TestTheSchemasApplyTheOneNameRule:
    @pytest.mark.parametrize(
        "name",
        ["You are now", "Ops & Analytics", "Bis\u200bby", "   ", "The Senior Software Engineer"],
    )
    def test_a_name_the_agents_own_tool_would_refuse_is_refused_here_too(self, name: str) -> None:
        with pytest.raises(ValidationError):
            AgentCreate(name=name)
        with pytest.raises(ValidationError):
            AgentUpdate(name=name)

    def test_ordinary_names_pass_and_are_normalized(self) -> None:
        assert AgentCreate(name="  Bisby  O\u2019Brien ").name == "Bisby O'Brien"
        assert AgentUpdate(name="QA Engineer").name == "QA Engineer"

    def test_an_omitted_name_is_untouched(self) -> None:
        """PATCH semantics: the validator must not fire for a field the
        caller never sent."""
        assert AgentUpdate().name is None
        assert "name" not in AgentUpdate(role_title="QA").model_dump(exclude_unset=True)


class TestANameShapedLikeAPayloadFails:
    """The operator asked for these to FAIL, not to be tidied up."""

    @pytest.mark.parametrize("raw", ["Bisby\nDoorstop", "Bisby\tDoorstop", "Bisby\r\nDoorstop"])
    def test_a_line_break_or_tab_is_refused_rather_than_collapsed(self, raw: str) -> None:
        """``PATCH`` with "Bisby\\nDoorstop" returned 200 and stored "Bisby
        Doorstop": whitespace collapse ran before the control-character
        check, so nothing dangerous was stored and the agent was quietly
        renamed to something nobody typed."""
        with pytest.raises(ValidationError, match="single line"):
            AgentUpdate(name=raw)
        with pytest.raises(ValidationError, match="single line"):
            AgentCreate(name=raw)


class TestTheHumanPathCannotCreateTheAmbiguityTheToolPrevents:
    """``PATCH`` with a colleague's name returned 200 and left two agents
    called "QA Engineer" in one workspace; "qa engineer" and "Qa-Engineer"
    did the same, the last slugging onto the colleague's handle. The agent's
    own tool refuses all three with ``agent_name_taken``."""

    @pytest.mark.parametrize("name", ["QA Engineer", "qa engineer", "Qa-Engineer"])
    async def test_renaming_onto_a_colleagues_name_or_handle_is_a_conflict(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, name: str
    ) -> None:
        colleague = Agent(
            workspace_id=admin_ctx.workspace_id, name="QA Engineer", slug="qa-engineer"
        )
        session.add(colleague)
        await session.flush()
        agent = await _agent(session, admin_ctx.workspace_id, "Bisby")

        with pytest.raises(HTTPException) as raised:
            await service.update_agent(
                session, admin_ctx, agent.id, changes={"name": name}, **_request_meta()
            )

        assert raised.value.status_code == 409
        assert "already called QA Engineer" in str(raised.value.detail)
        assert agent.name == "Bisby"
        assert await _audits(session, "agent.renamed", agent.id) == []

    @pytest.mark.parametrize("name", ["QA Engineer", "qa engineer", "Qa-Engineer"])
    async def test_creating_a_second_agent_with_that_name_is_a_conflict(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, name: str
    ) -> None:
        session.add(
            Agent(workspace_id=admin_ctx.workspace_id, name="QA Engineer", slug="qa-engineer")
        )
        await session.flush()

        with pytest.raises(HTTPException) as raised:
            await service.create_agent(
                session,
                admin_ctx,
                values={"name": name, "secondary_team_ids": []},
                **_request_meta(),
            )

        assert raised.value.status_code == 409

    async def test_an_agent_keeps_its_own_name_and_casing_on_an_unrelated_update(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        """The check excludes the agent being written, or nobody could ever
        change their own capitalisation."""
        agent = await _agent(session, admin_ctx.workspace_id, "QA Engineer")

        await service.update_agent(
            session,
            admin_ctx,
            agent.id,
            changes={"name": "Qa Engineer", "role_title": "QA"},
            **_request_meta(),
        )

        assert agent.name == "Qa Engineer"

    async def test_the_same_name_in_another_workspace_is_not_a_conflict(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        elsewhere = Workspace(name="Labs", slug=f"labs-{new_uuid7().hex[:8]}")
        session.add(elsewhere)
        await session.flush()
        session.add(Agent(workspace_id=elsewhere.id, name="QA Engineer", slug="qa-engineer"))
        await session.flush()
        agent = await _agent(session, admin_ctx.workspace_id, "Bisby")

        await service.update_agent(
            session, admin_ctx, agent.id, changes={"name": "QA Engineer"}, **_request_meta()
        )

        assert agent.name == "QA Engineer"


class TestARenameIsAudited:
    async def test_a_human_rename_writes_the_same_row_the_agents_tool_writes(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        agent = await _agent(session, admin_ctx.workspace_id, "Senior Software Engineer")
        slug = agent.slug

        await service.update_agent(
            session,
            admin_ctx,
            agent.id,
            changes={"name": "Bisby"},
            **_request_meta(),
        )

        rows = await _audits(session, "agent.renamed", agent.id)
        assert len(rows) == 1
        metadata = rows[0].metadata_json
        assert metadata["from"] == "Senior Software Engineer"
        assert metadata["to"] == "Bisby"
        assert metadata["via"] == "api"
        assert metadata["requested_by_user_id"] == str(admin_ctx.user.id)
        # The handle does not move on a rename, whoever performs it.
        assert metadata["slug"] == slug
        assert agent.slug == slug
        # The ordinary update row is still written beside it.
        assert len(await _audits(session, "agent.updated", agent.id)) == 1

    async def test_an_update_that_does_not_touch_the_name_writes_no_rename_row(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        agent = await _agent(session, admin_ctx.workspace_id, "Bisby")

        await service.update_agent(
            session,
            admin_ctx,
            agent.id,
            changes={"role_title": "Staff Engineer", "name": "Bisby"},
            **_request_meta(),
        )

        assert await _audits(session, "agent.renamed", agent.id) == []

    async def test_an_explicit_null_name_leaves_the_agent_named(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        """A body carrying ``"name": null`` used to reach ``setattr`` and
        fail on the not-null column. An agent has to be called something."""
        agent = await _agent(session, admin_ctx.workspace_id, "Bisby")

        await service.update_agent(
            session,
            admin_ctx,
            agent.id,
            changes={"name": None, "role_title": "Staff Engineer"},
            **_request_meta(),
        )

        assert agent.name == "Bisby"
        assert agent.role_title == "Staff Engineer"
        assert await _audits(session, "agent.renamed", agent.id) == []
