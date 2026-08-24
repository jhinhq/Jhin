"""skills.read through the full gateway pipeline against in-memory SQLite:
deny-by-default, workspace and per-agent enablement, grant name-scope
patterns, reference-file fetch, and output bounding."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    AgentSkill,
    Approval,
    AuditEvent,
    Skill,
    Task,
    Workspace,
)
from jhin_domain import ApprovalStatus, TaskState, new_uuid7
from jhin_skills import MAX_CONTENT_BYTES
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import GatewayOutcome, ToolGateway
from jhin_tools.skills_tools import MAX_READ_CHARS


class Org:
    workspace: Workspace
    me: Agent
    other: Agent
    task: Task

    def ctx(self, session: AsyncSession, agent: Agent) -> ToolExecutionContext:
        return ToolExecutionContext(
            session=session,
            workspace_id=self.workspace.id,
            task_id=self.task.id,
            run_id=new_uuid7(),
            agent_id=agent.id,
            agent_name=agent.name,
        )

    def gateway(self, session: AsyncSession, agent: Agent) -> ToolGateway:
        ctx = self.ctx(session, agent)
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


# --- skills.create / skills.update ------------------------------------------


async def grant_manage(session: AsyncSession, org: Org, agent: Agent) -> None:
    session.add(
        AgentCapabilityGrant(
            workspace_id=org.workspace.id,
            agent_id=agent.id,
            capability="skills.manage",
            scope_json={},
            effect="allow",
        )
    )
    await session.flush()


async def approve_and_execute(
    gateway: ToolGateway, session: AsyncSession, outcome: GatewayOutcome
) -> GatewayOutcome:
    assert outcome.status == "needs_approval", (outcome.decision_code, outcome.decision_reason)
    assert outcome.approval_id is not None
    approval = await session.get(Approval, outcome.approval_id)
    assert approval is not None
    approval.status = ApprovalStatus.APPROVED.value
    approval.decided_at = datetime.now(UTC)
    await session.flush()
    return await gateway.resolve_approved(outcome.approval_id)


def create_args(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "name": "team-standup-notes",
        "description": "Write a crisp daily standup summary from raw notes.",
        "content": "# Standup notes\n\nSummarize blockers first, then progress.",
    }
    body.update(overrides)
    return json.dumps(body)


class TestSkillsCreate:
    async def test_denied_without_grant(self, session: AsyncSession, org: Org) -> None:
        gateway = org.gateway(session, org.me)
        outcome = await gateway.request("skills.create", create_args())
        assert outcome.status == "denied"
        assert await session.scalar(select(Skill).where(Skill.name == "team-standup-notes")) is None

    async def test_is_approval_gated_and_creates_nothing_before_approval(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant_manage(session, org, org.me)
        gateway = org.gateway(session, org.me)
        outcome = await gateway.request("skills.create", create_args())
        assert outcome.status == "needs_approval"
        assert outcome.risk == "elevated"
        # zero side effect pre-approval
        assert await session.scalar(select(Skill).where(Skill.name == "team-standup-notes")) is None

        # The approval card payload reads well: name and a short preview.
        approval = await session.get(Approval, outcome.approval_id)
        assert approval is not None
        payload = approval.action_payload_sanitized
        assert payload["input"]["name"] == "team-standup-notes"
        assert "Summarize blockers" in payload["input"]["content"]

    async def test_approval_creates_enabled_agent_authored_skill(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant_manage(session, org, org.me)
        gateway = org.gateway(session, org.me)
        parked = await gateway.request("skills.create", create_args())
        outcome = await approve_and_execute(gateway, session, parked)
        assert outcome.status == "executed", outcome.decision_reason
        output = outcome.sanitized_output or {}
        assert output["name"] == "team-standup-notes"
        assert output["version"] == 1

        record = await session.scalar(select(Skill).where(Skill.name == "team-standup-notes"))
        assert record is not None
        assert record.workspace_id == org.workspace.id
        assert record.enabled is True  # the human already approved the call
        assert record.source == "agent_authored"
        assert record.created_by_agent_id == org.me.id

        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "skill.created", AuditEvent.target_id == record.id
            )
        )
        assert audit is not None
        assert audit.actor_id == org.me.id
        assert audit.metadata_json["created_via"] == "skills.create"

    async def test_duplicate_name_is_rejected(self, session: AsyncSession, org: Org) -> None:
        await grant_manage(session, org, org.me)
        gateway = org.gateway(session, org.me)
        first = await approve_and_execute(
            gateway, session, await gateway.request("skills.create", create_args())
        )
        assert first.status == "executed"
        second_parked = await gateway.request(
            "skills.create", create_args(description="A different pitch entirely.")
        )
        second = await approve_and_execute(gateway, session, second_parked)
        assert second.status != "executed"

    async def test_secret_content_is_rejected(self, session: AsyncSession, org: Org) -> None:
        await grant_manage(session, org, org.me)
        gateway = org.gateway(session, org.me)
        parked = await gateway.request(
            "skills.create", create_args(content="token ghp_" + "a" * 36)
        )
        outcome = await approve_and_execute(gateway, session, parked)
        assert outcome.status != "executed"
        assert await session.scalar(select(Skill).where(Skill.name == "team-standup-notes")) is None

    async def test_oversize_content_is_rejected(self, session: AsyncSession, org: Org) -> None:
        # A body this large also exceeds the gateway's own lossless-approval
        # bound (8,192 chars/string), so it is denied outright rather than
        # ever reaching a human approval — still never executed either way.
        await grant_manage(session, org, org.me)
        gateway = org.gateway(session, org.me)
        outcome = await gateway.request(
            "skills.create", create_args(content="x" * (MAX_CONTENT_BYTES + 1))
        )
        assert outcome.status != "executed"
        assert await session.scalar(select(Skill).where(Skill.name == "team-standup-notes")) is None

    async def test_bad_name_is_schema_rejected(self, session: AsyncSession, org: Org) -> None:
        await grant_manage(session, org, org.me)
        outcome = await org.gateway(session, org.me).request(
            "skills.create", create_args(name="Not A Slug")
        )
        assert outcome.status != "executed"
        assert outcome.status != "needs_approval"

    async def test_retried_invocation_creates_exactly_one_skill(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant_manage(session, org, org.me)
        ctx = org.ctx(session, org.me)
        session.add(
            AgentRun(
                id=ctx.run_id,
                workspace_id=ctx.workspace_id,
                task_id=ctx.task_id,
                agent_id=ctx.agent_id,
            )
        )
        await session.flush()
        await session.commit()
        gateway = ToolGateway(ctx, build_builtin_catalog())
        invocation_id = new_uuid7()
        args = create_args()

        parked = await gateway.request("skills.create", args, invocation_id=invocation_id)
        await session.commit()
        outcome = await approve_and_execute(gateway, session, parked)
        await session.commit()

        replay = await gateway.request("skills.create", args, invocation_id=invocation_id)
        assert replay.status == "executed"
        assert replay.tool_call_id == outcome.tool_call_id
        count = await session.scalar(
            select(func.count()).select_from(Skill).where(Skill.name == "team-standup-notes")
        )
        assert count == 1


class TestSkillsUpdate:
    async def test_denied_without_grant(self, session: AsyncSession, org: Org) -> None:
        outcome = await org.gateway(session, org.me).request(
            "skills.update", json.dumps({"name": "does-not-exist", "description": "x"})
        )
        assert outcome.status == "denied"

    async def test_updates_own_authored_skill(self, session: AsyncSession, org: Org) -> None:
        await grant_manage(session, org, org.me)
        gateway = org.gateway(session, org.me)
        created = await approve_and_execute(
            gateway, session, await gateway.request("skills.create", create_args())
        )
        assert created.status == "executed"

        parked = await gateway.request(
            "skills.update",
            json.dumps({"name": "team-standup-notes", "content": "# v2\n\nNew body."}),
        )
        outcome = await approve_and_execute(gateway, session, parked)
        assert outcome.status == "executed", outcome.decision_reason
        output = outcome.sanitized_output or {}
        assert output["updated_fields"] == ["content"]
        assert output["version"] == 2

        record = await session.scalar(select(Skill).where(Skill.name == "team-standup-notes"))
        assert record is not None
        assert record.content == "# v2\n\nNew body."
        assert record.version == 2

    async def test_cannot_update_a_skill_authored_by_another_agent(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant_manage(session, org, org.me)
        await grant_manage(session, org, org.other)
        gateway_me = org.gateway(session, org.me)
        created = await approve_and_execute(
            gateway_me, session, await gateway_me.request("skills.create", create_args())
        )
        assert created.status == "executed"

        gateway_other = org.gateway(session, org.other)
        outcome = await gateway_other.request(
            "skills.update",
            json.dumps({"name": "team-standup-notes", "description": "Hijacked."}),
        )
        assert outcome.status == "denied"
        assert outcome.decision_code == "not_skill_author"
        record = await session.scalar(select(Skill).where(Skill.name == "team-standup-notes"))
        assert record is not None
        assert record.description != "Hijacked."

    async def test_cannot_update_a_human_authored_skill(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant_manage(session, org, org.me)
        session.add(
            Skill(
                workspace_id=org.workspace.id,
                name="human-made",
                description="Written by a person.",
                content="# Human made\n",
                source="custom",
                enabled=True,
            )
        )
        await session.flush()
        outcome = await org.gateway(session, org.me).request(
            "skills.update",
            json.dumps({"name": "human-made", "description": "Agent hijack attempt."}),
        )
        assert outcome.status == "denied"
        assert outcome.decision_code == "not_skill_author"

    async def test_unknown_skill_fails_without_side_effect(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant_manage(session, org, org.me)
        outcome = await org.gateway(session, org.me).request(
            "skills.update", json.dumps({"name": "does-not-exist", "description": "x"})
        )
        assert outcome.status != "executed"

    async def test_no_fields_is_schema_rejected(self, session: AsyncSession, org: Org) -> None:
        await grant_manage(session, org, org.me)
        outcome = await org.gateway(session, org.me).request(
            "skills.update", json.dumps({"name": "team-standup-notes"})
        )
        assert outcome.status != "executed"
        assert outcome.status != "needs_approval"
