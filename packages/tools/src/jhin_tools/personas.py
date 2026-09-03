"""Persona tools: how an agent acts and sounds, chosen by the agent itself.

A persona is a structured card (``jhin_personas``): voice, stance, pace,
what to do when unsure, the register with people versus with teammates, a
signature, and a short ``never`` list. It reaches the system prompt as the
"How you work" block, under a guardrail line saying it shapes how the agent
says things and never what it may do.

Four tools, two capabilities:

- ``organization.persona.list`` (read) and ``organization.persona.assign_self``
  (write) under ``organization.persona.self``, a platform default: an agent
  may look through the workspace's enabled personas and wear one, or take
  it off. Only the caller's own row changes.
- ``organization.persona.create`` (elevated → human approval under the
  default balanced policy, like ``skills.create``) under the same
  capability: an agent proposes a card of its own. The card passes exactly
  the caps and content rules an admin's card passes — at schema time, so a
  facet that names a tool, carries a link, or talks about approvals is
  refused as invalid input before anything is staged for approval.
- ``organization.persona.assign`` (write) under
  ``organization.manage_agents``: dress a colleague. Restricted to agents in
  the target's manager chain — the rule ``update_agent_profile`` applies to
  a report's system prompt — enforced by a registered validator and
  re-checked in the executor.

Timing, stated in every description: a persona takes effect on the agent's
**next run** — the next chat turn or task — never mid-run. The worker
freezes the execution snapshot once at run start
(``resolve_snapshot_activity``), hashes it onto ``AgentRun.snapshot_hash``,
and every step re-reads that frozen snapshot; the card is part of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AuditEvent, Persona
from jhin_domain import ActorType
from jhin_personas import (
    FUN_TAG,
    MAX_CARD_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_FACET_CHARS,
    MAX_NAME_CHARS,
    MAX_NEVER_ITEM_CHARS,
    MAX_NEVER_ITEMS,
    MAX_TAGS,
    PersonaCard,
    PersonaFacets,
    check_content,
    is_valid_persona_name,
)
from jhin_policy import (
    PERSONA_SELF_CAPABILITY,
    DecisionType,
    Grant,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
)
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.directory import (
    find_agent_by_reference,
    names_hint,
    resolve_agent_reference,
)
from jhin_tools.errors import ToolExecutionError
from jhin_tools.organization import _is_subordinate
from jhin_tools.organization_admin import ORGANIZATION_MANAGE_AGENTS_CAPABILITY

# A workspace's persona library is a few dozen cards; the list tool shows
# at most this many and the scans below never read further than this.
MAX_PERSONAS_LISTED = 50
_SCAN_LIMIT = 1_000
_MAX_QUERY_CHARS = 100

# The same sentence in every summary, because the model otherwise tells the
# person "I now sound like X" in the very reply that still sounds like Y.
NEXT_RUN_NOTE = "this conversation keeps the voice it started with"


# --- shared lookups ---------------------------------------------------------


async def _caller(session: AsyncSession, ctx: ToolExecutionContext) -> Agent:
    me = await session.scalar(
        select(Agent).where(Agent.id == ctx.agent_id, Agent.workspace_id == ctx.workspace_id)
    )
    if me is None:
        raise ToolExecutionError(
            "the calling agent no longer exists in this workspace",
            code="agent_not_found",
            side_effect_possible=False,
        )
    return me


async def _enabled_personas(session: AsyncSession, workspace_id: UUID) -> list[Persona]:
    return list(
        await session.scalars(
            select(Persona)
            .where(Persona.workspace_id == workspace_id, Persona.enabled.is_(True))
            .order_by(Persona.display_name, Persona.name)
            .limit(_SCAN_LIMIT)
        )
    )


async def _persona_names_hint(session: AsyncSession, workspace_id: UUID) -> str:
    rows = await _enabled_personas(session, workspace_id)
    return names_hint("Personas", [row.name for row in rows])


async def _resolve_persona(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    persona_name: str | None,
    persona_id: str | None,
) -> Persona:
    """One *enabled* persona of this workspace, by name or id.

    A disabled card is deliberately "not found": disabling is how an admin
    takes a persona out of circulation, and an agent must not be able to
    put it back on by asking for it precisely.
    """
    row: Persona | None = None
    if persona_id:
        try:
            parsed = UUID(persona_id)
        except ValueError:
            parsed = None
        if parsed is not None:
            row = await session.scalar(
                select(Persona).where(
                    Persona.id == parsed,
                    Persona.workspace_id == workspace_id,
                    Persona.enabled.is_(True),
                )
            )
    elif persona_name:
        row = await session.scalar(
            select(Persona).where(
                Persona.workspace_id == workspace_id,
                Persona.name == persona_name.strip().lower(),
                Persona.enabled.is_(True),
            )
        )
    if row is None:
        label = persona_name or persona_id or ""
        raise ToolExecutionError(
            f"no enabled persona '{label}' in this workspace",
            code="persona_not_found",
            side_effect_possible=False,
            hint=await _persona_names_hint(session, workspace_id),
        )
    return row


def _assignment_audit(
    ctx: ToolExecutionContext,
    *,
    target: Agent,
    persona: Persona | None,
    previous_persona_id: UUID | None,
    via: str,
) -> AuditEvent:
    """The same ``persona.assigned`` row the API writes for a PATCH, with the
    run stamped on so an audit reader can tell an agent's choice from an
    admin's."""
    return AuditEvent(
        workspace_id=ctx.workspace_id,
        actor_type=ActorType.AGENT.value,
        actor_id=ctx.agent_id,
        action="persona.assigned",
        target_type="agent",
        target_id=target.id,
        metadata_json={
            "persona_id": str(persona.id) if persona is not None else None,
            "persona_name": persona.name if persona is not None else None,
            "previous_persona_id": (
                str(previous_persona_id) if previous_persona_id is not None else None
            ),
            "run_id": str(ctx.run_id),
            "via": via,
        },
    )


def _persona_choice(*, persona_name: str | None, persona_id: str | None, clear: bool) -> None:
    """Shared shape rule: exactly one of name/id, or ``clear`` alone."""
    chosen = [value for value in (persona_name, persona_id) if value]
    if clear:
        if chosen:
            raise ValueError("pass clear alone: it takes the persona off, no name or id needed")
    elif len(chosen) != 1:
        raise ValueError("pass exactly one of persona_name or persona_id, or clear=true")


# --- organization.persona.list (read) --------------------------------------


class PersonaListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(
        default="",
        max_length=_MAX_QUERY_CHARS,
        description="Optional search over name, display name, description, and tags.",
    )
    fun_only: bool = Field(
        default=False,
        description="Only the light-hearted personas (tagged 'fun').",
    )


class PersonaSummary(BaseModel):
    name: str
    display_name: str
    description: str
    tags: list[str]
    source: str
    # Whether this is the persona the calling agent wears right now.
    current: bool


class PersonaListOutput(BaseModel):
    personas: list[PersonaSummary]
    current_persona_name: str
    summary: str


def _matches(row: Persona, needle: str) -> bool:
    haystack = (row.name, row.display_name.lower(), row.description.lower(), *row.tags_json)
    return any(needle in text for text in haystack)


async def _persona_list(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PersonaListInput, payload)
    session = ctx.session
    me = await _caller(session, ctx)
    needle = " ".join(data.q.split()).lower()
    listed: list[PersonaSummary] = []
    for row in await _enabled_personas(session, ctx.workspace_id):
        if data.fun_only and FUN_TAG not in row.tags_json:
            continue
        if needle and not _matches(row, needle):
            continue
        listed.append(
            PersonaSummary(
                name=row.name,
                display_name=row.display_name,
                description=row.description,
                tags=list(row.tags_json),
                source=row.source,
                current=row.id == me.persona_id,
            )
        )
        if len(listed) >= MAX_PERSONAS_LISTED:
            break
    # Reported even when the worn card is disabled or filtered out of the
    # list: "what am I wearing" should not depend on the search.
    current_name = ""
    current_display = ""
    if me.persona_id is not None:
        worn = await session.scalar(
            select(Persona).where(
                Persona.id == me.persona_id, Persona.workspace_id == ctx.workspace_id
            )
        )
        if worn is not None:
            current_name = worn.name
            current_display = worn.display_name
    wearing = (
        f"you currently wear {current_display} ({current_name})"
        if current_name
        else "you currently wear none"
    )
    return PersonaListOutput(
        personas=listed,
        current_persona_name=current_name,
        summary=(
            f"{len(listed)} persona(s) listed; {wearing}. A persona you choose takes "
            "effect on your next run, never mid-run."
        ),
    )


# --- organization.persona.create (elevated) --------------------------------

_TEXT_FIELDS: tuple[str, ...] = (
    "display_name",
    "description",
    "voice",
    "stance",
    "pace",
    "when_unsure",
    "with_people",
    "with_teammates",
    "signature",
)


class PersonaCreateInput(BaseModel):
    """Flat facet fields rather than a nested object, so the model's call is
    one level deep and a schema error names the facet that broke."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=MAX_NAME_CHARS,
        description="Lowercase letters, digits, and hyphens; how the persona is referred to.",
    )
    display_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_CHARS)
    description: str = Field(
        min_length=1,
        max_length=MAX_DESCRIPTION_CHARS,
        description="One line: what this persona is like.",
    )
    tags: list[str] = Field(
        default_factory=list,
        max_length=MAX_TAGS,
        description="Short lowercase tags; 'fun' marks a light-hearted persona.",
    )
    voice: str = Field(
        min_length=1,
        max_length=MAX_FACET_CHARS,
        description="How you sound, in one or two sentences.",
    )
    stance: str = Field(
        default="",
        max_length=MAX_FACET_CHARS,
        description="How you take positions and handle disagreement.",
    )
    pace: str = Field(
        default="",
        max_length=MAX_FACET_CHARS,
        description="Brevity versus depth, and when to go long.",
    )
    when_unsure: str = Field(
        default="",
        max_length=MAX_FACET_CHARS,
        description="Whether you state an assumption or ask the person, and how.",
    )
    with_people: str = Field(
        default="",
        max_length=MAX_FACET_CHARS,
        description="Your register with the person you serve.",
    )
    with_teammates: str = Field(
        default="",
        max_length=MAX_FACET_CHARS,
        description="Your register with colleagues (other agents).",
    )
    signature: str = Field(
        default="",
        max_length=MAX_FACET_CHARS,
        description="One small recurring flourish, optional.",
    )
    never: list[str] = Field(
        default_factory=list,
        max_length=MAX_NEVER_ITEMS,
        description=f"Up to {MAX_NEVER_ITEMS} short things to avoid.",
    )
    assign_to_me: bool = Field(
        default=True,
        description="Wear the new persona yourself from your next run onward.",
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not is_valid_persona_name(value):
            raise ValueError(
                "persona names use lowercase letters, digits, and hyphens "
                f"(at most {MAX_NAME_CHARS} characters)"
            )
        return value

    # The same rules PersonaCard applies, run here per field so the gateway's
    # schema summary names the facet ("voice: value_error") rather than the
    # whole card: the model's next move should be to fix one field.
    @field_validator(*_TEXT_FIELDS)
    @classmethod
    def _check_text(cls, value: str, info: ValidationInfo) -> str:
        collapsed = " ".join(value.split())
        check_content(collapsed, field=str(info.field_name))
        return collapsed

    @field_validator("never")
    @classmethod
    def _check_never(cls, items: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in items:
            item = " ".join(raw.split())
            if not item or len(item) > MAX_NEVER_ITEM_CHARS:
                raise ValueError(f"never items must be 1 to {MAX_NEVER_ITEM_CHARS} characters each")
            check_content(item, field="never")
            cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _check_card(self) -> PersonaCreateInput:
        # Everything the fields above do not cover — the card total, tag
        # shape, distinct never items — is PersonaCard's to decide.
        try:
            self.card()
        except ValueError as error:
            raise ValueError(
                f"the persona card is invalid (each facet at most {MAX_FACET_CHARS} "
                f"characters, the whole card at most {MAX_CARD_CHARS}): {error}"
            ) from None
        return self

    def card(self) -> PersonaCard:
        return PersonaCard(
            name=self.name,
            display_name=self.display_name,
            description=self.description,
            tags=list(self.tags),
            facets=PersonaFacets(
                voice=self.voice,
                stance=self.stance,
                pace=self.pace,
                when_unsure=self.when_unsure,
                with_people=self.with_people,
                with_teammates=self.with_teammates,
                signature=self.signature,
                never=list(self.never),
            ),
        )


class PersonaCreateOutput(BaseModel):
    persona_id: str
    name: str
    display_name: str
    assigned: bool
    summary: str


async def _persona_create(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PersonaCreateInput, payload)
    card = data.card()
    session = ctx.session

    # Also the idempotency backstop should a replayed call ever reach the
    # executor twice: one name, one card.
    existing = await session.scalar(
        select(Persona).where(Persona.workspace_id == ctx.workspace_id, Persona.name == card.name)
    )
    if existing is not None:
        raise ToolExecutionError(
            f"a persona named {card.name!r} already exists in this workspace",
            code="persona_name_taken",
            side_effect_possible=False,
            hint=(
                "pick a different name, or wear the existing persona with "
                "organization.persona.assign_self"
            ),
        )

    # Enabled straight away: the human already approved this call, which is
    # the same review a custom card gets from the admin who writes it.
    row = Persona(
        workspace_id=ctx.workspace_id,
        name=card.name,
        display_name=card.display_name,
        description=card.description,
        tags_json=list(card.tags),
        facets_json=card.facets.model_dump(),
        source="agent",
        created_by_agent_id=ctx.agent_id,
        enabled=True,
        version=1,
    )
    session.add(row)
    await session.flush()
    session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="persona.created",
            target_type="persona",
            target_id=row.id,
            metadata_json={
                "name": row.name,
                "source": row.source,
                "run_id": str(ctx.run_id),
                "created_via": "organization.persona.create",
            },
        )
    )

    assigned = False
    if data.assign_to_me:
        me = await _caller(session, ctx)
        previous = me.persona_id
        me.persona_id = row.id
        session.add(
            _assignment_audit(
                ctx,
                target=me,
                persona=row,
                previous_persona_id=previous,
                via="organization.persona.create",
            )
        )
        assigned = True
    await session.flush()

    wearing = (
        f"You wear it from your next run onward; {NEXT_RUN_NOTE}."
        if assigned
        else "Wear it with organization.persona.assign_self; it takes effect on the next run."
    )
    return PersonaCreateOutput(
        persona_id=str(row.id),
        name=row.name,
        display_name=row.display_name,
        assigned=assigned,
        summary=f"Created the {row.display_name} persona ({row.name}). {wearing}",
    )


# --- organization.persona.assign_self (write) ------------------------------


class PersonaAssignSelfInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona_name: str | None = Field(
        default=None,
        max_length=MAX_NAME_CHARS,
        description="The persona's name, as listed by organization.persona.list.",
    )
    persona_id: str | None = Field(default=None, max_length=64)
    clear: bool = Field(default=False, description="Take your current persona off instead.")

    @model_validator(mode="after")
    def _validate_shape(self) -> PersonaAssignSelfInput:
        _persona_choice(
            persona_name=self.persona_name, persona_id=self.persona_id, clear=self.clear
        )
        return self


class PersonaAssignSelfOutput(BaseModel):
    persona_name: str
    display_name: str
    cleared: bool
    summary: str


async def _persona_assign_self(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PersonaAssignSelfInput, payload)
    session = ctx.session
    me = await _caller(session, ctx)
    persona: Persona | None = None
    if not data.clear:
        persona = await _resolve_persona(
            session,
            ctx.workspace_id,
            persona_name=data.persona_name,
            persona_id=data.persona_id,
        )
    previous = me.persona_id
    me.persona_id = persona.id if persona is not None else None
    session.add(
        _assignment_audit(
            ctx,
            target=me,
            persona=persona,
            previous_persona_id=previous,
            via="organization.persona.assign_self",
        )
    )
    await session.flush()
    if persona is None:
        return PersonaAssignSelfOutput(
            persona_name="",
            display_name="",
            cleared=True,
            summary=f"You wear no persona from your next run onward; {NEXT_RUN_NOTE}.",
        )
    return PersonaAssignSelfOutput(
        persona_name=persona.name,
        display_name=persona.display_name,
        cleared=False,
        summary=(
            f"You now wear the {persona.display_name} persona from your next run onward; "
            f"{NEXT_RUN_NOTE}."
        ),
    )


# --- organization.persona.assign (write, agents you manage) ----------------


class PersonaAssignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = Field(default=None, max_length=64)
    agent_name: str | None = Field(default=None, max_length=200)
    persona_name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    persona_id: str | None = Field(default=None, max_length=64)
    clear: bool = Field(default=False, description="Take the teammate's persona off instead.")

    @model_validator(mode="after")
    def _validate_shape(self) -> PersonaAssignInput:
        if not self.agent_id and not self.agent_name:
            raise ValueError("pass agent_id or agent_name to pick the teammate")
        _persona_choice(
            persona_name=self.persona_name, persona_id=self.persona_id, clear=self.clear
        )
        return self


class PersonaAssignOutput(BaseModel):
    agent_id: str
    agent_name: str
    persona_name: str
    cleared: bool
    summary: str


_NOT_TARGET_MANAGER = (
    "only an agent in the target's manager chain may change its persona; ask its "
    "manager, or use organization.persona.assign_self for your own"
)


async def validate_persona_assign(
    ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
) -> PolicyDecision | None:
    """Policy: dressing a colleague is a manager's call, the same "agents
    you manage" rule ``validate_update_agent_profile`` applies to a report's
    system prompt.

    Runs in the gateway before approval/execution; an unresolvable target
    falls through to the executor, which reports the clearer typed error.
    """
    data = cast(PersonaAssignInput, payload)
    target = await find_agent_by_reference(
        ctx.session, ctx.workspace_id, agent_id=data.agent_id, agent_name=data.agent_name
    )
    if target is None or isinstance(target, str):
        return None
    if not await _is_subordinate(ctx.session, ctx.workspace_id, ctx.agent_id, target.id):
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="not_target_manager",
            reason=_NOT_TARGET_MANAGER,
        )
    return None


async def _persona_assign(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PersonaAssignInput, payload)
    session = ctx.session
    target = await resolve_agent_reference(
        session,
        ctx.workspace_id,
        agent_id=data.agent_id,
        agent_name=data.agent_name,
        # Administration needs the full picture, hidden agents included.
        hint_discoverable_only=False,
        role="agent",
    )
    # Defense in depth: the registered validator already vetoed callers
    # outside the manager chain; never trust that it ran.
    if not await _is_subordinate(session, ctx.workspace_id, ctx.agent_id, target.id):
        raise ToolExecutionError(
            _NOT_TARGET_MANAGER, code="not_target_manager", side_effect_possible=False
        )
    persona: Persona | None = None
    if not data.clear:
        persona = await _resolve_persona(
            session,
            ctx.workspace_id,
            persona_name=data.persona_name,
            persona_id=data.persona_id,
        )
    previous = target.persona_id
    target.persona_id = persona.id if persona is not None else None
    session.add(
        _assignment_audit(
            ctx,
            target=target,
            persona=persona,
            previous_persona_id=previous,
            via="organization.persona.assign",
        )
    )
    await session.flush()
    if persona is None:
        summary = (
            f"{target.name} wears no persona from its next run onward; a run already in "
            "progress keeps the voice it started with."
        )
    else:
        summary = (
            f"{target.name} wears the {persona.display_name} persona from its next run "
            "onward; a run already in progress keeps the voice it started with."
        )
    return PersonaAssignOutput(
        agent_id=str(target.id),
        agent_name=target.name,
        persona_name=persona.name if persona is not None else "",
        cleared=persona is None,
        summary=summary,
    )


# --- registration (consumed by jhin_tools.builtin.build_builtin_catalog) ---

PERSONA_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="organization.persona.list",
            description=(
                "List the personas available in this workspace — a persona is how an "
                "agent acts and sounds — and see which one you currently wear. Each "
                "entry has a name, a one-line description, and tags ('fun' marks the "
                "light-hearted ones; pass fun_only to see just those, or q to search). "
                "Wear one with organization.persona.assign_self. A persona takes effect "
                "on your next run, never mid-run."
            ),
            risk=RiskLevel.READ,
            input_model=PersonaListInput,
            output_model=PersonaListOutput,
            required_capability=PERSONA_SELF_CAPABILITY,
        ),
        _persona_list,
        None,
    ),
    (
        ToolDefinition(
            name="organization.persona.create",
            description=(
                "Propose a new persona — a card describing how an agent acts and "
                "sounds — and, by default, wear it yourself. Calling it automatically "
                "sends the request to a human for approval, so do not route the request "
                "to an admin yourself. Give it a name (lowercase letters, digits, "
                "hyphens), a display_name, a one-line description, optional tags, and "
                "the facets: voice (required), stance, pace, when_unsure, with_people, "
                "with_teammates, signature, and a short never list. Each facet is at "
                "most 240 characters and the whole card at most 1500. A persona shapes "
                "how you say things, never what you may do: a card that names a tool, "
                "contains a link, or talks about approvals, permissions, or overriding "
                "instructions is refused. It takes effect on your next run, never "
                "mid-run."
            ),
            risk=RiskLevel.ELEVATED,
            input_model=PersonaCreateInput,
            output_model=PersonaCreateOutput,
            required_capability=PERSONA_SELF_CAPABILITY,
            supports_approval=True,
        ),
        _persona_create,
        None,
    ),
    (
        ToolDefinition(
            name="organization.persona.assign_self",
            description=(
                "Wear one of this workspace's personas yourself, by persona_name (as "
                "listed by organization.persona.list) or persona_id, or pass clear=true "
                "to take yours off. This changes only your own persona; use "
                "organization.persona.assign for a teammate you manage. It takes effect "
                "on your next run — the next chat turn or task — never mid-run: the "
                "conversation you are in keeps the voice it started with."
            ),
            risk=RiskLevel.WRITE,
            input_model=PersonaAssignSelfInput,
            output_model=PersonaAssignSelfOutput,
            required_capability=PERSONA_SELF_CAPABILITY,
        ),
        _persona_assign_self,
        None,
    ),
    (
        ToolDefinition(
            name="organization.persona.assign",
            description=(
                "Choose the persona a teammate you manage wears: pick the teammate by "
                "agent_name or agent_id and the persona by persona_name or persona_id, "
                "or pass clear=true to take theirs off. Only an agent in the teammate's "
                "manager chain may do this; for yourself use "
                "organization.persona.assign_self. It takes effect on the teammate's "
                "next run, never mid-run."
            ),
            risk=RiskLevel.WRITE,
            input_model=PersonaAssignInput,
            output_model=PersonaAssignOutput,
            required_capability=ORGANIZATION_MANAGE_AGENTS_CAPABILITY,
            supports_approval=True,
        ),
        _persona_assign,
        validate_persona_assign,
    ),
)

__all__ = [
    "MAX_PERSONAS_LISTED",
    "NEXT_RUN_NOTE",
    "PERSONA_TOOLS",
    "PersonaAssignInput",
    "PersonaAssignSelfInput",
    "PersonaCreateInput",
    "PersonaListInput",
    "validate_persona_assign",
]
