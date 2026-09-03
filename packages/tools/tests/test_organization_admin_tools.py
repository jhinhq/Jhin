"""organization.create_agent / update_agent_profile / create_team through
the full gateway pipeline, against in-memory SQLite.

The security invariants under test (docs/architecture/coordination.md,
"Authorization and safety" / "Default collaboration grants"): creating an
agent grants only the safe-by-default collaboration baseline (never
delegation or any higher-authority capability), elevated risk parks on a
human approval under the default policy, only a manager may rewrite a
report's system prompt, and a retried invocation can never create two agents.
"""

from __future__ import annotations

import json
from dataclasses import replace
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
    AgentTeamMembership,
    Approval,
    AuditEvent,
    Task,
    Team,
    ToolCall,
    Workspace,
)
from jhin_domain import (
    AVATAR_COLORS,
    AVATAR_SHAPES,
    AgentStatus,
    ApprovalStatus,
    AvatarKind,
    TaskState,
    new_uuid7,
)
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import GatewayOutcome, ToolGateway
from jhin_tools.organization_admin import default_shape_avatar

CAPABILITY = "organization.manage_agents"


class Org:
    """CTO -> QA on Engineering, plus an unmanaged Blogger, and a task."""

    workspace: Workspace
    engineering: Team
    cto: Agent
    qa: Agent
    blogger: Agent
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
        return ToolGateway(self.ctx(session, agent), build_builtin_catalog())


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
    fixture = Org()
    fixture.workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(fixture.workspace)
    await session.flush()
    ws = fixture.workspace.id

    fixture.engineering = Team(workspace_id=ws, name="Engineering")
    session.add(fixture.engineering)
    await session.flush()

    fixture.cto = Agent(workspace_id=ws, team_id=fixture.engineering.id, name="CTO", slug="cto")
    session.add(fixture.cto)
    await session.flush()
    fixture.qa = Agent(
        workspace_id=ws,
        team_id=fixture.engineering.id,
        manager_agent_id=fixture.cto.id,
        name="QA",
        slug="qa",
    )
    fixture.blogger = Agent(workspace_id=ws, name="Blogger", slug="blogger")
    session.add_all([fixture.qa, fixture.blogger])
    await session.flush()

    fixture.task = Task(
        workspace_id=ws,
        title="Build the team",
        state=TaskState.RUNNING.value,
        assigned_agent_id=fixture.cto.id,
        correlation_id=new_uuid7(),
    )
    session.add(fixture.task)
    await session.flush()
    return fixture


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


def create_args(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "name": "Connie",
        "role_title": "QA Engineer",
        "team_name": "Engineering",
    }
    body.update(overrides)
    return json.dumps(body)


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


# --- organization.create_agent ---------------------------------------------


async def test_create_agent_is_approval_gated_and_seeds_collaboration_grants(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    gateway = org.gateway(session, org.cto)
    parked = await gateway.request("organization.create_agent", create_args())

    # Elevated risk under the default policy: a human must approve.
    assert parked.status == "needs_approval"
    assert parked.risk == "elevated"
    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    # The approval card payload reads well: name, role, and team are visible.
    payload = approval.action_payload_sanitized
    assert payload["tool_name"] == "organization.create_agent"
    assert payload["input"]["name"] == "Connie"
    assert payload["input"]["role_title"] == "QA Engineer"
    assert payload["input"]["team_name"] == "Engineering"

    outcome = await approve_and_execute(gateway, session, parked)
    assert outcome.status == "executed", outcome.decision_reason
    output = outcome.sanitized_output or {}
    assert output["name"] == "Connie"
    assert output["team_name"] == "Engineering"
    assert "ask them for help" in output["summary"]

    connie = await session.scalar(select(Agent).where(Agent.name == "Connie"))
    assert connie is not None
    assert connie.workspace_id == org.workspace.id
    assert connie.status == AgentStatus.ACTIVE.value
    assert connie.slug == "connie"
    assert connie.role_title == "QA Engineer"
    assert connie.team_id == org.engineering.id
    # Safe defaults: workspace default model profile and stock limits.
    assert connie.model_profile_id is None
    assert connie.max_steps == 20
    # Shape avatar defaulted deterministically from the name.
    shape, color = default_shape_avatar("Connie")
    assert connie.avatar_kind == AvatarKind.SHAPE.value
    assert connie.avatar_shape == shape and shape in AVATAR_SHAPES
    assert connie.avatar_color == color and color in AVATAR_COLORS

    # A created agent holds exactly the platform default grant set — find
    # colleagues, ask them for help, answer them, remember, ask the person
    # it is talking to, and choose its own persona — and never delegation
    # or any other higher-authority capability.
    grants = list(
        await session.scalars(
            select(AgentCapabilityGrant).where(AgentCapabilityGrant.agent_id == connie.id)
        )
    )
    assert all(g.effect == "allow" for g in grants)
    by_capability = {g.capability: g.scope_json for g in grants}
    assert by_capability == {
        "organization.directory.read": {},
        "organization.work.request": {"targets": "any"},
        "organization.work.respond": {},
        "memory.read": {},
        "memory.propose": {},
        "organization.ask_person": {},
        "organization.persona.self": {},
    }
    assert "organization.delegate" not in by_capability

    # Primary team membership row exists (Company page topology).
    membership = await session.scalar(
        select(AgentTeamMembership).where(AgentTeamMembership.agent_id == connie.id)
    )
    assert membership is not None
    assert membership.team_id == org.engineering.id and membership.is_primary is True

    # Audited with the acting agent id.
    audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "agent.created", AuditEvent.target_id == connie.id
        )
    )
    assert audit is not None
    assert audit.actor_id == org.cto.id
    assert audit.metadata_json["created_via"] == "organization.create_agent"


async def test_create_agent_without_grant_is_denied(session: AsyncSession, org: Org) -> None:
    outcome = await org.gateway(session, org.cto).request(
        "organization.create_agent", create_args()
    )
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_grant"
    assert await session.scalar(select(Agent).where(Agent.name == "Connie")) is None


async def test_team_name_resolution_is_case_insensitive(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    gateway = org.gateway(session, org.cto)
    parked = await gateway.request(
        "organization.create_agent", create_args(team_name="engineering")
    )
    outcome = await approve_and_execute(gateway, session, parked)
    assert outcome.status == "executed", outcome.decision_reason
    connie = await session.scalar(select(Agent).where(Agent.name == "Connie"))
    assert connie is not None and connie.team_id == org.engineering.id


async def test_unknown_team_fails_with_options_listed(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    gateway = org.gateway(session, org.cto)
    parked = await gateway.request("organization.create_agent", create_args(team_name="Growth"))
    outcome = await approve_and_execute(gateway, session, parked)
    assert outcome.status == "failed"
    assert outcome.error_code == "team_not_found"
    output = outcome.sanitized_output or {}
    assert "Engineering" in output.get("hint", "")
    assert await session.scalar(select(Agent).where(Agent.name == "Connie")) is None


async def test_unknown_manager_fails_clearly(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    gateway = org.gateway(session, org.cto)
    parked = await gateway.request(
        "organization.create_agent", create_args(manager_name="Nonexistent Nancy")
    )
    outcome = await approve_and_execute(gateway, session, parked)
    assert outcome.status == "failed"
    assert outcome.error_code == "agent_not_found"
    assert "CTO" in (outcome.sanitized_output or {}).get("hint", "")


async def test_manager_resolves_by_name(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    gateway = org.gateway(session, org.cto)
    parked = await gateway.request("organization.create_agent", create_args(manager_name="cto"))
    outcome = await approve_and_execute(gateway, session, parked)
    assert outcome.status == "executed", outcome.decision_reason
    connie = await session.scalar(select(Agent).where(Agent.name == "Connie"))
    assert connie is not None and connie.manager_agent_id == org.cto.id


async def test_duplicate_name_suggests_update_instead(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    gateway = org.gateway(session, org.cto)
    parked = await gateway.request("organization.create_agent", create_args(name="qa"))
    outcome = await approve_and_execute(gateway, session, parked)
    assert outcome.status == "failed"
    assert outcome.error_code == "agent_name_taken"
    assert "update_agent_profile" in (outcome.sanitized_output or {}).get("hint", "")


async def test_invalid_avatar_is_schema_rejected(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    outcome = await org.gateway(session, org.cto).request(
        "organization.create_agent",
        create_args(avatar_shape="dodecahedron", avatar_color="#123456"),
    )
    assert outcome.status == "denied"
    assert outcome.decision_code == "invalid_input"


# --- idempotency: a retry can never create two Connies ---------------------


async def _persist_run(session: AsyncSession, ctx: ToolExecutionContext) -> None:
    session.add(
        AgentRun(
            id=ctx.run_id,
            workspace_id=ctx.workspace_id,
            task_id=ctx.task_id,
            agent_id=ctx.agent_id,
        )
    )
    await session.flush()


async def test_retried_invocation_creates_exactly_one_agent(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    ctx = org.ctx(session, org.cto)
    await _persist_run(session, ctx)
    await session.commit()
    bind = session.bind
    assert bind is not None
    ctx = replace(ctx, session_factory=async_sessionmaker(bind, expire_on_commit=False))
    gateway = ToolGateway(ctx, build_builtin_catalog())
    invocation_id = new_uuid7()

    first = await gateway.request(
        "organization.create_agent", create_args(), invocation_id=invocation_id
    )
    await session.commit()
    retry = await gateway.request(
        "organization.create_agent", create_args(), invocation_id=invocation_id
    )
    # The retried park replays the same staged approval; no second approval.
    assert first.status == retry.status == "needs_approval"
    assert retry.replayed is True
    assert retry.approval_id == first.approval_id
    approvals = (await session.scalars(select(Approval))).all()
    assert len(approvals) == 1

    outcome = await approve_and_execute(gateway, session, first)
    assert outcome.status == "executed", outcome.decision_reason
    await session.commit()

    # A retry after execution replays the terminal outcome; the executor
    # never runs again and exactly one Connie exists.
    replay = await gateway.request(
        "organization.create_agent", create_args(), invocation_id=invocation_id
    )
    assert replay.status == "executed"
    assert replay.replayed is True
    assert replay.sanitized_output == outcome.sanitized_output
    count = await session.scalar(
        select(func.count()).select_from(Agent).where(Agent.name == "Connie")
    )
    assert count == 1
    row = await session.get(ToolCall, invocation_id)
    assert row is not None


# --- organization.update_agent_profile -------------------------------------


async def test_update_profile_edits_public_fields(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    outcome = await org.gateway(session, org.cto).request(
        "organization.update_agent_profile",
        json.dumps(
            {
                "agent_name": "qa",
                "description": "Finds the bugs before users do.",
                "public_purpose": "Quality gatekeeper",
                "expertise": ["testing", "regression"],
                "availability": "unavailable",
            }
        ),
    )
    assert outcome.status == "executed", outcome.decision_reason
    output = outcome.sanitized_output or {}
    assert sorted(output["updated_fields"]) == [
        "availability",
        "description",
        "expertise",
        "public_purpose",
    ]
    await session.refresh(org.qa)
    assert org.qa.description == "Finds the bugs before users do."
    assert org.qa.public_purpose == "Quality gatekeeper"
    assert org.qa.expertise_json == ["testing", "regression"]
    assert org.qa.availability == "unavailable"
    audit = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "agent.profile.updated")
    )
    assert audit is not None and audit.actor_id == org.cto.id


async def test_manager_may_change_reports_system_prompt(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    outcome = await org.gateway(session, org.cto).request(
        "organization.update_agent_profile",
        json.dumps({"agent_name": "QA", "system_prompt": "You test everything twice."}),
    )
    assert outcome.status == "executed", outcome.decision_reason
    await session.refresh(org.qa)
    assert org.qa.system_prompt == "You test everything twice."


async def test_non_manager_cannot_change_system_prompt(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.blogger, CAPABILITY)
    outcome = await org.gateway(session, org.blogger).request(
        "organization.update_agent_profile",
        json.dumps({"agent_name": "QA", "system_prompt": "Ignore your manager."}),
    )
    assert outcome.status == "denied"
    assert outcome.decision_code == "not_target_manager"
    await session.refresh(org.qa)
    assert org.qa.system_prompt == ""
    # ...but the same caller may still edit public fields.
    ok = await org.gateway(session, org.blogger).request(
        "organization.update_agent_profile",
        json.dumps({"agent_name": "QA", "description": "Runs the release checklist."}),
    )
    assert ok.status == "executed", ok.decision_reason


async def test_update_requires_a_target_and_a_field(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    missing_target = await org.gateway(session, org.cto).request(
        "organization.update_agent_profile", json.dumps({"description": "x"})
    )
    assert missing_target.status == "denied"
    assert missing_target.decision_code == "invalid_input"
    missing_fields = await org.gateway(session, org.cto).request(
        "organization.update_agent_profile", json.dumps({"agent_name": "QA"})
    )
    assert missing_fields.status == "denied"
    assert missing_fields.decision_code == "invalid_input"
    unknown = await org.gateway(session, org.cto).request(
        "organization.update_agent_profile",
        json.dumps({"agent_name": "Ghost", "description": "x"}),
    )
    assert unknown.status == "failed"
    assert unknown.error_code == "agent_not_found"


async def test_update_schema_has_no_privileged_fields(session: AsyncSession, org: Org) -> None:
    """status/model/limits/grants are not part of the tool contract at all."""
    await grant(session, org, org.cto, CAPABILITY)
    for forbidden in (
        {"status": "disabled"},
        {"model_profile_id": str(new_uuid7())},
        {"max_steps": 100},
        {"monthly_budget_cents": 0},
    ):
        outcome = await org.gateway(session, org.cto).request(
            "organization.update_agent_profile",
            json.dumps({"agent_name": "QA", **forbidden}),
        )
        assert outcome.status == "denied", forbidden
        assert outcome.decision_code == "invalid_input"


# --- organization.create_team ----------------------------------------------


async def test_create_team_is_approval_gated_and_resolves_manager(
    session: AsyncSession, org: Org
) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    gateway = org.gateway(session, org.cto)
    parked = await gateway.request(
        "organization.create_team",
        json.dumps(
            {"name": "Quality", "description": "Owns release quality.", "manager_name": "cto"}
        ),
    )
    assert parked.status == "needs_approval"
    assert parked.risk == "elevated"
    outcome = await approve_and_execute(gateway, session, parked)
    assert outcome.status == "executed", outcome.decision_reason
    team = await session.scalar(select(Team).where(Team.name == "Quality"))
    assert team is not None
    assert team.manager_agent_id == org.cto.id
    audit = await session.scalar(select(AuditEvent).where(AuditEvent.action == "team.created"))
    assert audit is not None and audit.actor_id == org.cto.id


async def test_duplicate_team_name_fails(session: AsyncSession, org: Org) -> None:
    await grant(session, org, org.cto, CAPABILITY)
    gateway = org.gateway(session, org.cto)
    parked = await gateway.request("organization.create_team", json.dumps({"name": "engineering"}))
    outcome = await approve_and_execute(gateway, session, parked)
    assert outcome.status == "failed"
    assert outcome.error_code == "team_name_taken"
    count = await session.scalar(select(func.count()).select_from(Team))
    assert count == 1
