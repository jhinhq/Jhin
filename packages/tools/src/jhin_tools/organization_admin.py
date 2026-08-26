"""Self-administration tools: create teammates and teams, update profiles.

Security model (docs/architecture/coordination.md, "Authorization and
safety"): agents must never modify grants, policies, budgets, status, or
model configuration — so these tools handle identity and placement only:

- ``organization.create_agent`` (elevated → human approval under the default
  balanced policy) creates an ACTIVE agent with safe defaults: the workspace
  default model profile, default run limits, a shape avatar, and the
  safe-by-default *collaboration* baseline (find colleagues, ask peers for
  help, respond to requests — :func:`jhin_policy.collaboration_grant_specs`).
  This baseline is a fixed platform default, not a capability the calling
  agent chooses; granting any *other* tool remains a human admin action, and
  higher-authority capabilities (delegation, connectors, sandbox, agent
  management) are never auto-granted.
- ``organization.update_agent_profile`` (write) edits the public profile of
  an existing agent. The target's ``system_prompt`` may be changed only by a
  caller in the target's manager chain (enforced by a registered validator
  and re-checked in the executor).
- ``organization.create_team`` (elevated → human approval) creates an empty
  team.

There is deliberately no tool for grants, memberships beyond creation
placement, budgets, deletion, or pausing. Idempotency comes from the
gateway's stable invocation claims (a retried call replays the recorded
outcome instead of re-running the executor) plus the case-insensitive
duplicate-name guards below.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AgentCapabilityGrant, AgentTeamMembership, AuditEvent, Team
from jhin_domain import (
    AVATAR_COLORS,
    AVATAR_SHAPES,
    ActorType,
    AgentStatus,
    AvatarKind,
    new_uuid7,
)
from jhin_policy import (
    DecisionType,
    Grant,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
    collaboration_grant_specs,
)
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.directory import (
    find_agent_by_reference,
    names_hint,
    resolve_agent_reference,
)
from jhin_tools.errors import ToolExecutionError
from jhin_tools.organization import _is_subordinate

ORGANIZATION_MANAGE_AGENTS_CAPABILITY = "organization.manage_agents"

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")
# Bound on candidate scans; workspaces are small organizations.
_SCAN_LIMIT = 1_000


def _slugify(value: str) -> str:
    slug = _SLUG_INVALID.sub("-", value.strip().lower()).strip("-")[:60].strip("-")
    return slug or f"agent-{new_uuid7().hex[:6]}"


def default_shape_avatar(name: str) -> tuple[str, str]:
    """Deterministic shape + color for a name.

    Mirrors ``defaultShapeFor`` in apps/web/lib/shapes.ts (hash * 31 + code,
    uint32) so a teammate created through this tool gets the same cube the
    web wizard would have defaulted to.
    """
    hashed = 0
    for ch in name.strip() or "Agent":
        hashed = (hashed * 31 + ord(ch)) & 0xFFFFFFFF
    return (
        AVATAR_SHAPES[hashed % len(AVATAR_SHAPES)],
        AVATAR_COLORS[(hashed // 7) % len(AVATAR_COLORS)],
    )


# --- name resolution -------------------------------------------------------


async def _resolve_team(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    team_id: str | None,
    team_name: str | None,
) -> Team | None:
    if team_id:
        try:
            parsed = UUID(team_id)
        except ValueError:
            raise ToolExecutionError(
                f"'{team_id}' is not a team id",
                code="team_not_found",
                side_effect_possible=False,
                hint="pass team_id as a UUID, or use team_name instead",
            ) from None
        team = await session.scalar(
            select(Team).where(Team.id == parsed, Team.workspace_id == workspace_id)
        )
        if team is None:
            raise ToolExecutionError(
                f"no team with id {team_id} in this workspace",
                code="team_not_found",
                side_effect_possible=False,
                hint=await _team_names_hint(session, workspace_id),
            )
        return team
    if team_name:
        teams = list(
            await session.scalars(
                select(Team).where(Team.workspace_id == workspace_id).limit(_SCAN_LIMIT)
            )
        )
        matches = [t for t in teams if t.name.strip().lower() == team_name.strip().lower()]
        if not matches:
            raise ToolExecutionError(
                f"no team named '{team_name}' in this workspace",
                code="team_not_found",
                side_effect_possible=False,
                hint=names_hint("Teams", [t.name for t in teams]),
            )
        if len(matches) > 1:
            raise ToolExecutionError(
                f"the team name '{team_name}' is ambiguous",
                code="team_name_ambiguous",
                side_effect_possible=False,
                hint="pass team_id instead to pick one exactly",
            )
        return matches[0]
    return None


async def _team_names_hint(session: AsyncSession, workspace_id: UUID) -> str:
    names = list(
        await session.scalars(
            select(Team.name).where(Team.workspace_id == workspace_id).limit(_SCAN_LIMIT)
        )
    )
    return names_hint("Teams", names)


async def _find_agent(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    agent_id: str | None,
    agent_name: str | None,
) -> Agent | Literal["ambiguous"] | None:
    """Quiet lookup shared by the validator (no exceptions) and executors.

    Administration reaches every agent in the workspace, hidden ones
    included: this capability already manages them.
    """
    return await find_agent_by_reference(
        session, workspace_id, agent_id=agent_id, agent_name=agent_name
    )


async def _resolve_agent(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    agent_id: str | None,
    agent_name: str | None,
    role: str,
) -> Agent:
    return await resolve_agent_reference(
        session,
        workspace_id,
        agent_id=agent_id,
        agent_name=agent_name,
        # Administration needs the full picture, e.g. to explain that a name
        # is already taken by an agent the directory does not list.
        hint_discoverable_only=False,
        role=role,
    )


# --- organization.create_agent (elevated) ---------------------------------


class CreateAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    role_title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4_000)
    public_purpose: str = Field(default="", max_length=1_000)
    system_prompt: str = Field(default="", max_length=20_000)
    team_id: str | None = Field(default=None, max_length=64)
    team_name: str | None = Field(default=None, max_length=200)
    manager_agent_id: str | None = Field(default=None, max_length=64)
    manager_name: str | None = Field(default=None, max_length=200)
    avatar_shape: str | None = Field(default=None, max_length=32)
    avatar_color: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _validate_avatar(self) -> CreateAgentInput:
        if (self.avatar_shape is None) != (self.avatar_color is None):
            raise ValueError("avatar_shape and avatar_color must be set together")
        if self.avatar_shape is not None and self.avatar_shape not in AVATAR_SHAPES:
            raise ValueError(f"avatar_shape must be one of: {', '.join(AVATAR_SHAPES)}")
        if self.avatar_color is not None and self.avatar_color.lower() not in AVATAR_COLORS:
            raise ValueError("avatar_color must be one of the fixed palette colors")
        return self


class CreateAgentOutput(BaseModel):
    agent_id: str
    name: str
    slug: str
    role_title: str = ""
    team_name: str = ""
    manager_name: str = ""
    summary: str


async def _create_agent(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(CreateAgentInput, payload)
    session = ctx.session

    # Duplicate-name guard (also the idempotency backstop should a replayed
    # call ever reach the executor twice): one name, one agent.
    duplicate = await session.scalar(
        select(Agent).where(
            Agent.workspace_id == ctx.workspace_id,
            func.lower(Agent.name) == data.name.strip().lower(),
        )
    )
    if duplicate is not None:
        raise ToolExecutionError(
            f"an agent named '{duplicate.name}' already exists in this workspace",
            code="agent_name_taken",
            side_effect_possible=False,
            hint="use organization.update_agent_profile to edit the existing agent instead",
        )

    team = await _resolve_team(
        session, ctx.workspace_id, team_id=data.team_id, team_name=data.team_name
    )
    manager: Agent | None = None
    if data.manager_agent_id or data.manager_name:
        manager = await _resolve_agent(
            session,
            ctx.workspace_id,
            agent_id=data.manager_agent_id,
            agent_name=data.manager_name,
            role="manager",
        )

    slug = _slugify(data.name)
    taken = await session.scalar(
        select(Agent.id).where(Agent.workspace_id == ctx.workspace_id, Agent.slug == slug)
    )
    if taken is not None:
        slug = f"{slug}-{new_uuid7().hex[:6]}"

    shape, color = (
        (data.avatar_shape, cast(str, data.avatar_color).lower())
        if data.avatar_shape is not None
        else default_shape_avatar(data.name)
    )

    # Safe defaults everywhere else: ACTIVE status, workspace default model
    # profile (model_profile_id=None), default limits (max_steps 20), and —
    # by the platform invariant — no capability grants of any kind.
    agent = Agent(
        workspace_id=ctx.workspace_id,
        name=data.name.strip(),
        slug=slug,
        role_title=data.role_title,
        description=data.description,
        public_purpose=data.public_purpose,
        system_prompt=data.system_prompt,
        status=AgentStatus.ACTIVE.value,
        team_id=team.id if team is not None else None,
        manager_agent_id=manager.id if manager is not None else None,
        avatar_kind=AvatarKind.SHAPE.value,
        avatar_shape=shape,
        avatar_color=color,
    )
    session.add(agent)
    await session.flush()
    if team is not None:
        session.add(
            AgentTeamMembership(
                workspace_id=ctx.workspace_id,
                agent_id=agent.id,
                team_id=team.id,
                is_primary=True,
            )
        )
    # Safe-by-default collaboration baseline (a fixed platform default, not
    # an agent-chosen grant): the new teammate can find colleagues, ask peers
    # for help, and answer requests. Nothing higher-authority is granted.
    for capability, scope in collaboration_grant_specs():
        session.add(
            AgentCapabilityGrant(
                workspace_id=ctx.workspace_id,
                agent_id=agent.id,
                capability=capability,
                scope_json=dict(scope),
                effect="allow",
            )
        )
    session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="agent.created",
            target_type="agent",
            target_id=agent.id,
            metadata_json={
                "name": agent.name,
                "slug": agent.slug,
                "team_id": str(team.id) if team is not None else None,
                "manager_agent_id": str(manager.id) if manager is not None else None,
                "run_id": str(ctx.run_id),
                "created_via": "organization.create_agent",
            },
        )
    )
    await session.flush()

    placement = f" on the {team.name} team" if team is not None else ""
    reporting = f", reporting to {manager.name}" if manager is not None else ""
    return CreateAgentOutput(
        agent_id=str(agent.id),
        name=agent.name,
        slug=agent.slug,
        role_title=agent.role_title,
        team_name=team.name if team is not None else "",
        manager_name=manager.name if manager is not None else "",
        summary=(
            f"Created teammate {agent.name}"
            + (f" ({agent.role_title})" if agent.role_title else "")
            + placement
            + reporting
            + ". It uses the workspace default model and can find colleagues "
            "and ask them for help; a workspace admin can grant any other "
            "tools it needs."
        ),
    )


# --- organization.update_agent_profile (write) -----------------------------


class UpdateAgentProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = Field(default=None, max_length=64)
    agent_name: str | None = Field(default=None, max_length=200)
    role_title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4_000)
    public_purpose: str | None = Field(default=None, max_length=1_000)
    expertise: list[str] | None = Field(default=None, max_length=20)
    availability: Literal["available", "unavailable"] | None = None
    system_prompt: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _validate_shape(self) -> UpdateAgentProfileInput:
        if not self.agent_id and not self.agent_name:
            raise ValueError("pass agent_id or agent_name to pick the agent to update")
        if all(
            value is None
            for value in (
                self.role_title,
                self.description,
                self.public_purpose,
                self.expertise,
                self.availability,
                self.system_prompt,
            )
        ):
            raise ValueError("pass at least one profile field to update")
        if self.expertise is not None and any(not 1 <= len(tag) <= 64 for tag in self.expertise):
            raise ValueError("expertise tags must contain 1 to 64 characters")
        return self


class UpdateAgentProfileOutput(BaseModel):
    agent_id: str
    name: str
    updated_fields: list[str]
    summary: str


async def validate_update_agent_profile(
    ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
) -> PolicyDecision | None:
    """Policy: only an agent in the target's manager chain may change the
    target's system prompt. Other profile fields carry no such restriction.

    Runs in the gateway before approval/execution; resolution failures fall
    through to the executor, which reports the clearer typed error.
    """
    data = cast(UpdateAgentProfileInput, payload)
    if data.system_prompt is None:
        return None
    target = await _find_agent(
        ctx.session, ctx.workspace_id, agent_id=data.agent_id, agent_name=data.agent_name
    )
    if target is None or isinstance(target, str):
        return None
    if not await _is_subordinate(ctx.session, ctx.workspace_id, ctx.agent_id, target.id):
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="not_target_manager",
            reason=(
                "only an agent in the target's manager chain may change its "
                "system prompt; update the other profile fields instead, or "
                "ask the target's manager"
            ),
        )
    return None


async def _update_agent_profile(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(UpdateAgentProfileInput, payload)
    session = ctx.session
    target = await _resolve_agent(
        session,
        ctx.workspace_id,
        agent_id=data.agent_id,
        agent_name=data.agent_name,
        role="agent",
    )

    updated: list[str] = []
    if data.role_title is not None:
        target.role_title = data.role_title
        updated.append("role_title")
    if data.description is not None:
        target.description = data.description
        updated.append("description")
    if data.public_purpose is not None:
        target.public_purpose = data.public_purpose
        updated.append("public_purpose")
    if data.expertise is not None:
        target.expertise_json = list(data.expertise)
        updated.append("expertise")
    if data.availability is not None:
        target.availability = data.availability
        updated.append("availability")
    if data.system_prompt is not None:
        # Defense in depth: the registered validator already vetoed callers
        # outside the manager chain; never trust that it ran.
        if not await _is_subordinate(session, ctx.workspace_id, ctx.agent_id, target.id):
            raise ToolExecutionError(
                "only an agent in the target's manager chain may change its system prompt",
                code="not_target_manager",
                side_effect_possible=False,
            )
        target.system_prompt = data.system_prompt
        updated.append("system_prompt")

    session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="agent.profile.updated",
            target_type="agent",
            target_id=target.id,
            metadata_json={
                "changed_fields": sorted(updated),
                "run_id": str(ctx.run_id),
                "updated_via": "organization.update_agent_profile",
            },
        )
    )
    await session.flush()
    return UpdateAgentProfileOutput(
        agent_id=str(target.id),
        name=target.name,
        updated_fields=updated,
        summary=f"Updated {target.name}'s profile ({', '.join(updated)}).",
    )


# --- organization.create_team (elevated) -----------------------------------


class CreateTeamInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4_000)
    manager_agent_id: str | None = Field(default=None, max_length=64)
    manager_name: str | None = Field(default=None, max_length=200)


class CreateTeamOutput(BaseModel):
    team_id: str
    name: str
    manager_name: str = ""
    summary: str


async def _create_team(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(CreateTeamInput, payload)
    session = ctx.session

    duplicate = await session.scalar(
        select(Team).where(
            Team.workspace_id == ctx.workspace_id,
            func.lower(Team.name) == data.name.strip().lower(),
        )
    )
    if duplicate is not None:
        raise ToolExecutionError(
            f"a team named '{duplicate.name}' already exists in this workspace",
            code="team_name_taken",
            side_effect_possible=False,
            hint="place agents on the existing team instead of creating a duplicate",
        )

    manager: Agent | None = None
    if data.manager_agent_id or data.manager_name:
        manager = await _resolve_agent(
            session,
            ctx.workspace_id,
            agent_id=data.manager_agent_id,
            agent_name=data.manager_name,
            role="manager",
        )

    team = Team(
        workspace_id=ctx.workspace_id,
        name=data.name.strip(),
        description=data.description,
        manager_agent_id=manager.id if manager is not None else None,
    )
    session.add(team)
    await session.flush()
    session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="team.created",
            target_type="team",
            target_id=team.id,
            metadata_json={
                "name": team.name,
                "manager_agent_id": str(manager.id) if manager is not None else None,
                "run_id": str(ctx.run_id),
                "created_via": "organization.create_team",
            },
        )
    )
    await session.flush()
    return CreateTeamOutput(
        team_id=str(team.id),
        name=team.name,
        manager_name=manager.name if manager is not None else "",
        summary=(
            f"Created team {team.name}"
            + (f", managed by {manager.name}" if manager is not None else "")
            + ". Creating a team grants no permissions to anyone."
        ),
    )


# --- registration (consumed by jhin_tools.builtin.build_builtin_catalog) ---

ORGANIZATION_ADMIN_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="organization.create_agent",
            description=(
                "Create a new AI teammate in this workspace. Use this tool "
                "whenever you are asked to add, hire, or create a teammate — "
                "calling it automatically sends the request to a human for "
                "approval, so do not route the request to an admin yourself. "
                "Give the teammate a name and role_title; optionally a "
                "team_name (or team_id), a manager_name (or "
                "manager_agent_id), a description, a public_purpose, and a "
                "system_prompt. The new teammate starts active with the "
                "workspace default model, a shape avatar, and only the "
                "default ability to find colleagues and ask them for help; "
                "only a human admin can grant any other tools."
            ),
            risk=RiskLevel.ELEVATED,
            input_model=CreateAgentInput,
            output_model=CreateAgentOutput,
            required_capability=ORGANIZATION_MANAGE_AGENTS_CAPABILITY,
            supports_approval=True,
        ),
        _create_agent,
        None,
    ),
    (
        ToolDefinition(
            name="organization.update_agent_profile",
            description=(
                "Update an existing teammate's public profile: role_title, "
                "description, public_purpose, expertise tags, or "
                "availability. Pick the teammate by agent_name or agent_id. "
                "system_prompt can be changed only for agents you manage. "
                "This tool can never change permissions, model, status, or "
                "limits."
            ),
            risk=RiskLevel.WRITE,
            input_model=UpdateAgentProfileInput,
            output_model=UpdateAgentProfileOutput,
            required_capability=ORGANIZATION_MANAGE_AGENTS_CAPABILITY,
            supports_approval=True,
        ),
        _update_agent_profile,
        validate_update_agent_profile,
    ),
    (
        ToolDefinition(
            name="organization.create_team",
            description=(
                "Create a new team in this workspace. Use this tool when "
                "asked to set up a team — calling it automatically sends the "
                "request to a human for approval. Give the team a name and "
                "optionally a description and a manager (manager_name or "
                "manager_agent_id). Creating a team grants no permissions to "
                "anyone."
            ),
            risk=RiskLevel.ELEVATED,
            input_model=CreateTeamInput,
            output_model=CreateTeamOutput,
            required_capability=ORGANIZATION_MANAGE_AGENTS_CAPABILITY,
            supports_approval=True,
        ),
        _create_team,
        None,
    ),
)
