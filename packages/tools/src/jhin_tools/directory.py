"""Organization directory and roster: public identity only.

Both the API (``GET /directory``) and the agent runtime (the
``organization.directory.search`` tool and the roster block in the system
prompt) read through this module, so there is exactly one allowlist of
fields an agent or a colleague may learn about another agent:
``DirectoryEntry``. System prompts, grants, model configuration, private
metadata, memories, and conversations are never loaded here.

This module is also the one place that turns *a colleague's name* into an
agent row (:func:`resolve_agent_reference`). Agents refer to each other by
name — the roster deliberately hides ids from agents that have no tool
taking one — so every tool that accepts a colleague must accept a name, and
they all resolve it the same way here rather than each inventing its own
matching rules.

``organization.colleague_status`` answers "what is X doing right now?" from
the same public footing: identity plus work status derived from
authoritative task/run/review rows, and nothing else (see
:class:`jhin_tools.rollups.ColleagueStatus`).

The roster is an agent's *knowledge* of its colleagues: it is meant to be
used, including to answer a person's questions about the team. It is not an
authorization artifact — relationships (manager, team, collaborator) never
change what an agent is allowed to do, and the rendered block says so.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AgentRelationship, AgentTeamMembership, Team
from jhin_domain import AgentStatus
from jhin_policy import RiskLevel, ToolDefinition, capability_matches
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.errors import ToolExecutionError
from jhin_tools.rollups import ColleagueStatus, build_colleague_status

DIRECTORY_CAPABILITY = "organization.directory.read"
DIRECTORY_MAX_RESULTS = 25
DIRECTORY_TOOL_MAX_RESULTS = 10
ROSTER_MAX_ENTRIES = 40
ROSTER_MAX_CHARS = 3_000
# Tools whose arguments are agent ids. The roster prints ids only for an
# agent that holds one of these — for everyone else an id is pure noise the
# model can only misuse (echoing it at a person).
ID_CONSUMING_CAPABILITIES: frozenset[str] = frozenset(
    {"organization.delegate", "organization.work.request"}
)
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
    # Everyone else discoverable in the workspace, so "who else works here?"
    # is answerable without a tool call in a small organization.
    others: list[DirectoryEntry] = Field(default_factory=list)
    truncated: bool = False

    def entries(self) -> list[DirectoryEntry]:
        out = [self.self_entry]
        if self.manager is not None:
            out.append(self.manager)
        out.extend(self.reports)
        out.extend(self.primary_team_members)
        out.extend(self.collaborators)
        out.extend(self.secondary_team_members)
        out.extend(self.others)
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


# --- name resolution ---
#
# Agents know their colleagues by name, not by uuid: the roster prints an id
# only for an agent holding a tool that consumes one, and the platform
# preamble forbids putting an id in a message to a person. A tool that only
# accepted ``target_agent_id`` was therefore unusable for the most ordinary
# ask there is ("ask the CTO what he is working on"), so every colleague
# argument resolves through the one function below.

# How many names a "here is who exists" hint may list.
MAX_HINT_NAMES = 15


def names_hint(label: str, names: Sequence[str]) -> str:
    """A bounded, sorted "here is what exists" hint for a failed lookup."""
    unique = sorted(set(names))
    shown = ", ".join(unique[:MAX_HINT_NAMES])
    if len(unique) > MAX_HINT_NAMES:
        shown += ", …"
    return f"{label}: {shown}" if shown else f"this workspace has no {label.lower()} yet"


def _visible[SelectT: Select[Any]](query: SelectT, *, discoverable_only: bool) -> SelectT:
    """Optionally narrow a scan to agents the directory would list at all."""
    if not discoverable_only:
        return query
    return query.where(
        Agent.status == AgentStatus.ACTIVE.value,
        Agent.discoverability == "discoverable",
    )


async def agent_names_hint(
    session: AsyncSession, workspace_id: UUID, *, discoverable_only: bool = True
) -> str:
    """Candidate colleague names for a failed lookup.

    Defaults to discoverable, active agents only: a failed name lookup must
    never become a way to enumerate agents the directory and the roster
    deliberately hide.
    """
    names = list(
        await session.scalars(
            _visible(
                select(Agent.name).where(Agent.workspace_id == workspace_id),
                discoverable_only=discoverable_only,
            ).limit(_SCAN_LIMIT)
        )
    )
    return names_hint("Agents", names)


async def find_agent_by_reference(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    agent_id: str | None = None,
    agent_name: str | None = None,
    discoverable_only: bool = False,
) -> Agent | Literal["ambiguous"] | None:
    """Quiet colleague lookup by id or by name (no exceptions raised).

    ``agent_id`` wins when both are given. Name matching is
    case-insensitive and runs in decreasing strength — exact name, exact
    slug, exact role title, then a *unique* substring of a name or role
    title — so "CTO", "cto", and "Chief Technology Officer" all reach the
    same colleague while a needle matching several agents reports
    ``"ambiguous"`` instead of silently picking one.
    """
    if agent_id:
        try:
            parsed = UUID(agent_id)
        except ValueError:
            return None
        by_id: Agent | None = await session.scalar(
            _visible(
                select(Agent).where(Agent.id == parsed, Agent.workspace_id == workspace_id),
                discoverable_only=discoverable_only,
            )
        )
        return by_id
    needle = (agent_name or "").strip().lower()
    if not needle:
        return None
    candidates = list(
        await session.scalars(
            _visible(
                select(Agent).where(Agent.workspace_id == workspace_id),
                discoverable_only=discoverable_only,
            )
            .order_by(Agent.name, Agent.id)
            .limit(_SCAN_LIMIT)
        )
    )
    tiers = (
        lambda a: a.name.strip().lower() == needle,
        lambda a: a.slug.strip().lower() == needle,
        lambda a: a.role_title.strip().lower() == needle,
        lambda a: needle in a.name.strip().lower(),
        lambda a: needle in a.role_title.strip().lower(),
    )
    for matches_tier in tiers:
        matches = [a for a in candidates if matches_tier(a)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "ambiguous"
    return None


async def resolve_agent_reference(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    agent_id: str | None = None,
    agent_name: str | None = None,
    discoverable_only: bool = False,
    hint_discoverable_only: bool = True,
    role: str = "agent",
) -> Agent:
    """:func:`find_agent_by_reference`, raising a model-readable failure.

    An unknown name comes back as a bounded failure that *names the
    candidates* rather than a bare "not found", because the model's next
    move should be to retry with a real colleague's name. ``role`` only
    shapes the message ("the manager name '…'").
    """
    found = await find_agent_by_reference(
        session,
        workspace_id,
        agent_id=agent_id,
        agent_name=agent_name,
        discoverable_only=discoverable_only,
    )
    if isinstance(found, str):
        raise ToolExecutionError(
            f"the {role} name '{agent_name}' matches more than one agent",
            code="agent_name_ambiguous",
            side_effect_possible=False,
            hint="pass the agent id instead to pick one exactly",
        )
    if found is None:
        label = agent_name or agent_id or ""
        raise ToolExecutionError(
            f"no agent '{label}' in this workspace",
            code="agent_not_found",
            side_effect_possible=False,
            hint=await agent_names_hint(
                session, workspace_id, discoverable_only=hint_discoverable_only
            ),
        )
    return found


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

    # Whatever budget is left goes to the rest of the workspace, so a small
    # organization is fully known to every agent and "who else works here?"
    # never needs a tool call. Discoverable, active agents only — the same
    # visibility rule the directory search applies.
    others = take(
        await session.scalars(
            select(Agent)
            .where(
                Agent.workspace_id == workspace_id,
                Agent.status == AgentStatus.ACTIVE.value,
                Agent.discoverability == "discoverable",
            )
            .order_by(Agent.name, Agent.id)
            .limit(_SCAN_LIMIT)
        )
    )

    everyone = [
        agent,
        *manager_list,
        *reports,
        *primary_members,
        *collaborators,
        *secondary_members,
        *others,
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
        others=project(others),
        truncated=truncated,
    )


ROSTER_HEADER = (
    "Your colleagues. This section is your own knowledge of who works in "
    "this organization: when someone asks who is on your team, who your "
    "manager is, who else works here, or who could help with something, "
    "answer them from this list, by name, in your own words. Knowing a "
    "colleague is not permission to act for them: relationships here grant "
    "no capabilities, and you still act only through the tools you have "
    "been granted."
)

_ID_GUIDANCE = (
    "The bracketed agent ids are tool arguments only (pass one as "
    "target_agent_id). Never write an id in a message to a person — refer "
    "to colleagues by name."
)


def _line(entry: DirectoryEntry, *, with_id: bool) -> str:
    """One colleague, human-readable first. The id (when the agent has a
    tool that consumes one) trails at the end so it reads as machine detail
    rather than as part of the colleague's identity."""
    line = f"- {entry.name}"
    if entry.role_title:
        line += f" — {entry.role_title}"
    if entry.primary_team_name:
        line += f", {entry.primary_team_name} team"
    else:
        # Say it rather than leave a gap. Omitting the team read as "unstated"
        # rather than "none", and asked which team such a colleague was on, an
        # agent invented one out of their expertise tags -- a Chief of Staff
        # with "operations, planning" became "on the Operations team".
        line += ", not on a team"
    if entry.availability != "available":
        line += f" (currently {entry.availability})"
    if entry.expertise:
        line += f". Expertise: {', '.join(entry.expertise[:6])}"
    if entry.public_purpose:
        line += f". {entry.public_purpose[:120]}"
    if not line.endswith("."):
        line += "."
    if with_id:
        line += f" [agent id: {entry.id}]"
    return line


def render_roster(
    roster: OrganizationRoster,
    *,
    max_chars: int = ROSTER_MAX_CHARS,
    capabilities: Iterable[str] = (),
) -> str:
    """The "Your colleagues" prompt block.

    Framed as knowledge the agent may answer from — the previous "routing
    context only" header read as reference data the model was not supposed
    to speak from, and agents answered "who is on your team?" without ever
    naming their manager. The security statement is kept verbatim in intent:
    the block still says plainly that knowing a colleague grants nothing.

    ``capabilities`` are the running agent's granted capability patterns.
    They only decide presentation: ids appear when the agent has a tool that
    takes one, and the "look further" hint appears when it can search the
    directory. Nothing here is an authorization check.
    """
    granted = list(capabilities)

    def has(capability: str) -> bool:
        return any(capability_matches(pattern, capability) for pattern in granted)

    with_ids = any(has(capability) for capability in ID_CONSUMING_CAPABILITIES)
    can_search = has(DIRECTORY_CAPABILITY)

    team = roster.self_entry.primary_team_name
    sections: list[tuple[str, list[DirectoryEntry]]] = [
        ("Your manager", [roster.manager] if roster.manager else []),
        ("Your direct reports", roster.reports),
        (f"Your team ({team})" if team else "Your team", roster.primary_team_members),
        ("Close collaborators", roster.collaborators),
        ("Other teams you belong to", roster.secondary_team_members),
        ("Others in this workspace", roster.others),
    ]
    # The same silence that made an agent invent a colleague's team, now about
    # itself and its own manager. Asked "what team are you on?" with nothing
    # stated, a teamless agent built one out of its role and expertise; asked
    # "who do you report to?", it named whoever sounded most senior. Say both,
    # including when the answer is nobody.
    lines = [
        ROSTER_HEADER,
        f"You are {roster.self_entry.name}"
        + (f", {roster.self_entry.role_title}" if roster.self_entry.role_title else "")
        + (f" on the {team} team." if team else ", and you are not on a team."),
    ]
    if roster.manager is None:
        lines.append("You have no manager in this workspace.")
    listed = 0
    for title, entries in sections:
        if not entries:
            continue
        listed += len(entries)
        lines.append(f"{title}:")
        lines.extend(_line(e, with_id=with_ids) for e in entries)
    if listed == 0:
        lines.append(
            "You are the only agent in this workspace right now; you have no "
            "colleagues to name yet."
        )
    elif with_ids:
        lines.append(_ID_GUIDANCE)
    if roster.truncated:
        lines.append(
            "There are more colleagues than are listed above."
            + (
                " Look someone up with organization.directory.search rather "
                "than saying you do not know."
                if can_search
                else ""
            )
        )
    elif can_search and listed:
        lines.append(
            "If someone asks about a colleague who is not listed above, look "
            "them up with organization.directory.search before answering that "
            "you do not know them."
        )
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


class ColleagueStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str = Field(default="", max_length=200)
    # Optional: only agents that hold a tool taking an id ever see one.
    agent_id: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _needs_a_colleague(self) -> ColleagueStatusInput:
        if not self.agent_name.strip() and not self.agent_id.strip():
            raise ValueError("pass agent_name (or agent_id) to pick the colleague")
        return self


async def _colleague_status(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(ColleagueStatusInput, payload)
    colleague = await resolve_agent_reference(
        ctx.session,
        ctx.workspace_id,
        agent_id=data.agent_id.strip() or None,
        agent_name=data.agent_name.strip() or None,
        # A status lookup is a discovery: an agent the directory and roster
        # hide must not become visible through it, by name or by id.
        discoverable_only=True,
        role="colleague",
    )
    if colleague.id == ctx.agent_id:
        raise ToolExecutionError(
            "that is you — you already know what you are working on",
            code="self_status",
            side_effect_possible=False,
            hint="pass a colleague's name instead",
        )
    teams = await _primary_team_by_agent(ctx.session, ctx.workspace_id, [colleague])
    team = teams.get(colleague.id)
    return await build_colleague_status(ctx.session, colleague, team_name=team[1] if team else "")


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
    (
        ToolDefinition(
            name="organization.colleague_status",
            description=(
                "Find out what a colleague is doing right now. Pass their "
                'name (agent_name) — for example "what is the CTO working '
                'on?" Returns their availability, the tasks they are '
                "working on and have queued, what they recently finished, "
                "when they were last active, and how much is waiting on "
                "them. Use this before telling anyone you do not know what a "
                "colleague is up to. Public work status only: it never "
                "returns a colleague's instructions, permissions, private "
                "notes, or conversations."
            ),
            risk=RiskLevel.READ,
            input_model=ColleagueStatusInput,
            output_model=ColleagueStatus,
            required_capability=DIRECTORY_CAPABILITY,
        ),
        _colleague_status,
        None,
    ),
)
