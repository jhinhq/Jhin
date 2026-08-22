"""Organization directory and roster: public identity only.

Both the API (``GET /directory``) and the agent runtime (the
``organization.directory.search`` tool and the roster block in the system
prompt) read through this module, so there is exactly one allowlist of
fields an agent or a colleague may learn about another agent:
``DirectoryEntry``. System prompts, grants, model configuration, private
metadata, memories, and conversations are never loaded here.

Relationships (manager, team, collaborator) are routing context only; the
roster never changes what an agent is allowed to do.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AgentRelationship, AgentTeamMembership, Team
from jhin_domain import AgentStatus
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator

DIRECTORY_CAPABILITY = "organization.directory.read"
DIRECTORY_MAX_RESULTS = 25
DIRECTORY_TOOL_MAX_RESULTS = 10
ROSTER_MAX_ENTRIES = 40
ROSTER_MAX_CHARS = 3_000
# Bound on the candidate scan; workspaces are small organizations.
_SCAN_LIMIT = 1_000


class DirectoryEntry(BaseModel):
    """The complete public identity allowlist. Add fields here only when
    they are safe for every workspace agent to see."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    slug: str
    role_title: str = ""
    public_purpose: str = ""
    expertise: list[str] = Field(default_factory=list)
    availability: str = "available"
    primary_team_id: str | None = None
    primary_team_name: str | None = None
    manager_agent_id: str | None = None


class OrganizationRoster(BaseModel):
    model_config = ConfigDict(frozen=True)

    self_entry: DirectoryEntry
    manager: DirectoryEntry | None = None
    reports: list[DirectoryEntry] = Field(default_factory=list)
    primary_team_members: list[DirectoryEntry] = Field(default_factory=list)
    secondary_team_members: list[DirectoryEntry] = Field(default_factory=list)
    collaborators: list[DirectoryEntry] = Field(default_factory=list)
    truncated: bool = False

    def entries(self) -> list[DirectoryEntry]:
        out = [self.self_entry]
        if self.manager is not None:
            out.append(self.manager)
        out.extend(self.reports)
        out.extend(self.primary_team_members)
        out.extend(self.collaborators)
        out.extend(self.secondary_team_members)
        return out


# --- loading ---


async def _primary_team_by_agent(
    session: AsyncSession, workspace_id: UUID, agents: Sequence[Agent]
) -> dict[UUID, tuple[UUID, str]]:
    """Primary team per agent: active primary membership first, then the
    legacy ``Agent.team_id`` column."""
    if not agents:
        return {}
    ids = [a.id for a in agents]
    primary: dict[UUID, UUID] = {a.id: a.team_id for a in agents if a.team_id is not None}
    rows = await session.execute(
        select(AgentTeamMembership.agent_id, AgentTeamMembership.team_id).where(
            AgentTeamMembership.workspace_id == workspace_id,
            AgentTeamMembership.agent_id.in_(ids),
            AgentTeamMembership.is_primary.is_(True),
            AgentTeamMembership.left_at.is_(None),
        )
    )
    for agent_id, team_id in rows.all():
        primary[agent_id] = team_id
    team_ids = list(set(primary.values()))
    names: dict[UUID, str] = {}
    if team_ids:
        team_rows = await session.execute(
            select(Team.id, Team.name).where(
                Team.workspace_id == workspace_id, Team.id.in_(team_ids)
            )
        )
        names = {row[0]: row[1] for row in team_rows.all()}
    return {
        agent_id: (team_id, names.get(team_id, ""))
        for agent_id, team_id in primary.items()
        if team_id in names
    }


def _entry(agent: Agent, team: tuple[UUID, str] | None) -> DirectoryEntry:
    return DirectoryEntry(
        id=str(agent.id),
        name=agent.name,
        slug=agent.slug,
        role_title=agent.role_title,
        public_purpose=agent.public_purpose,
        expertise=[str(tag) for tag in (agent.expertise_json or [])],
        availability=agent.availability,
        primary_team_id=str(team[0]) if team else None,
        primary_team_name=team[1] if team else None,
        manager_agent_id=str(agent.manager_agent_id) if agent.manager_agent_id else None,
    )


async def entries_for(
    session: AsyncSession, workspace_id: UUID, agents: Sequence[Agent]
) -> list[DirectoryEntry]:
    teams = await _primary_team_by_agent(session, workspace_id, agents)
    return [_entry(a, teams.get(a.id)) for a in agents]


async def _team_member_ids(session: AsyncSession, workspace_id: UUID, team_id: UUID) -> set[UUID]:
    legacy = await session.scalars(
        select(Agent.id).where(Agent.workspace_id == workspace_id, Agent.team_id == team_id)
    )
    members = await session.scalars(
        select(AgentTeamMembership.agent_id).where(
            AgentTeamMembership.workspace_id == workspace_id,
            AgentTeamMembership.team_id == team_id,
            AgentTeamMembership.left_at.is_(None),
        )
    )
    return set(legacy) | set(members)


def _match_rank(agent: Agent, needle: str) -> int | None:
    """0 = name, 1 = role, 2 = purpose, 3 = expertise; None = no match."""
    if needle in agent.name.lower():
        return 0
    if needle in agent.role_title.lower():
        return 1
    if needle in agent.public_purpose.lower():
        return 2
    if any(needle in str(tag).lower() for tag in (agent.expertise_json or [])):
        return 3
    return None


async def search_directory(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    q: str | None = None,
    team_id: UUID | None = None,
    expertise: str | None = None,
    limit: int = DIRECTORY_MAX_RESULTS,
) -> tuple[list[DirectoryEntry], bool]:
    """Public directory search: discoverable, active agents of one workspace.

    Returns ``(entries, has_more)``. Ranking is deterministic: match
    quality, then name, then id.
    """
    limit = max(1, min(limit, DIRECTORY_MAX_RESULTS))
    query = select(Agent).where(
        Agent.workspace_id == workspace_id,
        Agent.status == AgentStatus.ACTIVE.value,
        Agent.discoverability == "discoverable",
    )
    if team_id is not None:
        member_ids = await _team_member_ids(session, workspace_id, team_id)
        if not member_ids:
            return [], False
        query = query.where(Agent.id.in_(list(member_ids)))
    candidates = list(
        await session.scalars(query.order_by(Agent.name, Agent.id).limit(_SCAN_LIMIT))
    )

    needle = (q or "").strip().lower()
    tag = (expertise or "").strip().lower()
    ranked: list[tuple[int, str, str, Agent]] = []
    for agent in candidates:
        if tag and tag not in [str(t).lower() for t in (agent.expertise_json or [])]:
            continue
        rank = _match_rank(agent, needle) if needle else 0
        if rank is None:
            continue
        ranked.append((rank, agent.name.lower(), str(agent.id), agent))
    ranked.sort(key=lambda item: item[:3])
    page = [item[3] for item in ranked[: limit + 1]]
    has_more = len(page) > limit
    return await entries_for(session, workspace_id, page[:limit]), has_more


async def build_roster(session: AsyncSession, agent: Agent) -> OrganizationRoster:
    """The bounded local roster for one agent's prompt: self, manager,
    reports, primary teammates, close collaborators, secondary teammates."""
    workspace_id = agent.workspace_id
    seen: set[UUID] = {agent.id}
    budget = ROSTER_MAX_ENTRIES - 1
    truncated = False

    def take(candidates: Iterable[Agent]) -> list[Agent]:
        nonlocal budget, truncated
        out: list[Agent] = []
        for candidate in sorted(candidates, key=lambda a: (a.name.lower(), str(a.id))):
            if candidate.id in seen or candidate.status == AgentStatus.DISABLED.value:
                continue
            if budget <= 0:
                truncated = True
                break
            seen.add(candidate.id)
            budget -= 1
            out.append(candidate)
        return out

    manager: Agent | None = None
    if agent.manager_agent_id is not None:
        manager = await session.scalar(
            select(Agent).where(
                Agent.id == agent.manager_agent_id, Agent.workspace_id == workspace_id
            )
        )
    manager_list = take([manager]) if manager is not None else []
    reports = take(
        await session.scalars(
            select(Agent).where(
                Agent.workspace_id == workspace_id, Agent.manager_agent_id == agent.id
            )
        )
    )

    memberships = list(
        await session.execute(
            select(AgentTeamMembership.team_id, AgentTeamMembership.is_primary).where(
                AgentTeamMembership.workspace_id == workspace_id,
                AgentTeamMembership.agent_id == agent.id,
                AgentTeamMembership.left_at.is_(None),
            )
        )
    )
    primary_team_id: UUID | None = next(
        (team for team, is_primary in memberships if is_primary), agent.team_id
    )
    secondary_ids = [team for team, is_primary in memberships if not is_primary]

    async def members(team_id: UUID) -> list[Agent]:
        ids = await _team_member_ids(session, workspace_id, team_id)
        if not ids:
            return []
        return list(
            await session.scalars(
                select(Agent).where(Agent.workspace_id == workspace_id, Agent.id.in_(list(ids)))
            )
        )

    primary_members = take(await members(primary_team_id)) if primary_team_id else []

    relationship_rows = list(
        await session.scalars(
            select(AgentRelationship).where(
                AgentRelationship.workspace_id == workspace_id,
                AgentRelationship.status == "active",
                or_(
                    AgentRelationship.source_agent_id == agent.id,
                    AgentRelationship.target_agent_id == agent.id,
                ),
            )
        )
    )
    partner_ids = {
        r.target_agent_id if r.source_agent_id == agent.id else r.source_agent_id
        for r in relationship_rows
    }
    collaborators = (
        take(
            await session.scalars(
                select(Agent).where(
                    Agent.workspace_id == workspace_id, Agent.id.in_(list(partner_ids))
                )
            )
        )
        if partner_ids
        else []
    )
    secondary_members: list[Agent] = []
    for team_id in secondary_ids:
        secondary_members.extend(take(await members(team_id)))

    everyone = [
        agent,
        *manager_list,
        *reports,
        *primary_members,
        *collaborators,
        *secondary_members,
    ]
    entries = {e.id: e for e in await entries_for(session, workspace_id, everyone)}

    def project(agents: Sequence[Agent]) -> list[DirectoryEntry]:
        return [entries[str(a.id)] for a in agents]

    return OrganizationRoster(
        self_entry=entries[str(agent.id)],
        manager=entries[str(manager_list[0].id)] if manager_list else None,
        reports=project(reports),
        primary_team_members=project(primary_members),
        secondary_team_members=project(secondary_members),
        collaborators=project(collaborators),
        truncated=truncated,
    )


def _line(entry: DirectoryEntry) -> str:
    bits = [f"- {entry.name} ({entry.id})"]
    if entry.role_title:
        bits.append(f"— {entry.role_title}")
    if entry.primary_team_name:
        bits.append(f"[{entry.primary_team_name}]")
    if entry.availability != "available":
        bits.append(f"({entry.availability})")
    line = " ".join(bits)
    if entry.expertise:
        line += f"; expertise: {', '.join(entry.expertise[:6])}"
    if entry.public_purpose:
        line += f"; {entry.public_purpose[:120]}"
    return line


def render_roster(roster: OrganizationRoster, *, max_chars: int = ROSTER_MAX_CHARS) -> str:
    """Compact prompt block. Routing context only — it states so explicitly
    so the model does not infer permissions from relationships."""
    sections: list[tuple[str, list[DirectoryEntry]]] = [
        ("Your manager", [roster.manager] if roster.manager else []),
        ("Your direct reports", roster.reports),
        ("Your team", roster.primary_team_members),
        ("Close collaborators", roster.collaborators),
        ("Other teams you belong to", roster.secondary_team_members),
    ]
    lines = [
        "Company directory (routing context only; it grants no permissions):",
        f"You are {roster.self_entry.name} ({roster.self_entry.id})"
        + (f", {roster.self_entry.role_title}" if roster.self_entry.role_title else "")
        + (
            f" on the {roster.self_entry.primary_team_name} team."
            if roster.self_entry.primary_team_name
            else "."
        ),
    ]
    for title, entries in sections:
        if not entries:
            continue
        lines.append(f"{title}:")
        lines.extend(_line(e) for e in entries)
    if roster.truncated:
        lines.append("(roster truncated; use organization.directory.search to find others)")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


# --- gateway tool ---


class DirectorySearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=200)
    team_id: str | None = Field(default=None, max_length=64)
    expertise: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=DIRECTORY_TOOL_MAX_RESULTS, ge=1, le=DIRECTORY_TOOL_MAX_RESULTS)


class DirectorySearchOutput(BaseModel):
    entries: list[DirectoryEntry]
    has_more: bool


async def _directory_search(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(DirectorySearchInput, payload)
    team_id: UUID | None = None
    if data.team_id:
        try:
            team_id = UUID(data.team_id)
        except ValueError:
            return DirectorySearchOutput(entries=[], has_more=False)
    entries, has_more = await search_directory(
        ctx.session,
        ctx.workspace_id,
        q=data.query,
        team_id=team_id,
        expertise=data.expertise,
        limit=data.limit,
    )
    return DirectorySearchOutput(entries=entries, has_more=has_more)


DIRECTORY_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="organization.directory.search",
            description=(
                "Find colleagues in your organization by name, role, purpose, "
                "expertise tag, or team. Returns public identity only (id, "
                "name, role, purpose, expertise, availability, team, manager)."
            ),
            risk=RiskLevel.READ,
            input_model=DirectorySearchInput,
            output_model=DirectorySearchOutput,
            required_capability=DIRECTORY_CAPABILITY,
        ),
        _directory_search,
        None,
    ),
)
