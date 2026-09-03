"""Immutable agent execution snapshots (plan 7.1).

A snapshot freezes everything a run needs to know about its agent at start
time: identity, prompt, persona, placement, model profile, and limits. Its
hash is stored on the run so audits can prove which configuration executed.

Credentials are deliberately absent: the snapshot carries the provider and
secret *ids* only. Plaintext is resolved inside the model-call activity at
the moment of use (plan 13.5) and never serialized into workflow state.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, ModelProfile, ModelProvider, Persona, Team, Workspace
from jhin_models import ReasoningConfig, WebSearchConfig
from jhin_personas import PersonaCard, PersonaFacets


class SnapshotError(Exception):
    """Snapshot resolution failed; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RunLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_steps: int
    max_run_minutes: int


class ModelProfileSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: UUID
    provider_id: UUID
    provider_type: str
    base_url: str | None
    secret_id: UUID | None
    model_name: str
    display_name: str
    input_cost_micros_per_million: int | None
    output_cost_micros_per_million: int | None
    # Model-native web search opt-in from the profile's config_json
    # (docs/architecture/web.md). None when the profile does not enable it.
    web_search: WebSearchConfig | None = None
    # Reasoning-effort override from the profile's config_json plus its
    # supports_reasoning flag. None when the profile says nothing, in which
    # case the adapter applies the automatic tool-compatibility rule.
    reasoning: ReasoningConfig | None = None


class AgentExecutionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: UUID
    workspace_id: UUID
    # Additive (platform preamble): snapshots serialized before this field
    # existed deserialize with "" and render without the workspace clause.
    workspace_name: str = ""
    name: str
    role_title: str
    system_prompt: str
    autonomy_level: str
    team_id: UUID | None
    team_name: str | None
    manager_agent_id: UUID | None
    manager_name: str | None
    model_profile: ModelProfileSnapshot
    temperature: float | None
    max_output_tokens: int | None
    run_limits: RunLimits
    # Additive (personas): the card this agent wears, frozen with the rest
    # of the snapshot so snapshot_hash proves which card a run saw and a
    # reassignment mid-run cannot change a running prompt. Snapshots
    # serialized before this field deserialize with None and render no
    # block, the same replay rule as workspace_name.
    persona: PersonaCard | None = None

    def snapshot_hash(self) -> str:
        """Stable content hash of the full snapshot (stored on the run)."""
        canonical = self.model_dump_json()
        return hashlib.sha256(canonical.encode()).hexdigest()


def _enabled_web_search(config_json: dict[str, object] | None) -> WebSearchConfig | None:
    """The profile's model-native web search opt-in, or None when disabled."""
    config = WebSearchConfig.from_profile_config(dict(config_json or {}))
    return config if config.enabled else None


def _reasoning_override(
    config_json: dict[str, object] | None, supports_reasoning: bool
) -> ReasoningConfig | None:
    """The profile's reasoning opinion, or None when it has none.

    ``config_json.reasoning.effort`` is the explicit override; the profile's
    ``supports_reasoning`` column is folded in as a hint so a reasoning model
    whose name the matcher does not recognize is still treated as one.
    """
    config = ReasoningConfig.from_profile_config(dict(config_json or {}))
    if supports_reasoning and not config.supports_reasoning:
        config = config.model_copy(update={"supports_reasoning": True})
    return config if config.is_set else None


def _persona_card(row: Persona) -> PersonaCard | None:
    """The persona row as a validated card, or None when the stored document
    no longer validates (a card written under an older rule set, say).

    A persona that cannot render degrades to no block, never to a failed
    run: the card shapes how the agent sounds and is not worth stopping
    work over.
    """
    try:
        return PersonaCard(
            name=row.name,
            display_name=row.display_name,
            description=row.description,
            tags=list(row.tags_json),
            facets=PersonaFacets.model_validate(row.facets_json),
        )
    except ValidationError:
        return None


async def resolve_snapshot(
    session: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> AgentExecutionSnapshot:
    """Resolve the immutable snapshot for one agent in one workspace.

    Model profile precedence (plan 15.2): the agent's explicit profile, then
    the workspace default. No profile at all is a configuration error.
    """
    agent = await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        raise SnapshotError("agent_not_found", f"agent {agent_id} not found in workspace")

    workspace = await session.get(Workspace, workspace_id)
    profile_id = agent.model_profile_id
    if profile_id is None:
        profile_id = workspace.default_model_profile_id if workspace else None
    if profile_id is None:
        raise SnapshotError(
            "no_model_profile",
            f"agent '{agent.name}' has no model profile and the workspace has no default",
        )

    profile = await session.scalar(
        select(ModelProfile).where(
            ModelProfile.id == profile_id, ModelProfile.workspace_id == workspace_id
        )
    )
    if profile is None:
        raise SnapshotError("no_model_profile", "configured model profile no longer exists")

    provider = await session.scalar(
        select(ModelProvider).where(
            ModelProvider.id == profile.provider_id,
            ModelProvider.workspace_id == workspace_id,
        )
    )
    if provider is None:
        raise SnapshotError("provider_not_found", "model provider no longer exists")
    if not provider.enabled:
        raise SnapshotError("provider_disabled", f"provider '{provider.display_name}' is disabled")

    team_name: str | None = None
    if agent.team_id is not None:
        team_name = await session.scalar(select(Team.name).where(Team.id == agent.team_id))
    manager_name: str | None = None
    if agent.manager_agent_id is not None:
        manager_name = await session.scalar(
            select(Agent.name).where(Agent.id == agent.manager_agent_id)
        )

    persona: PersonaCard | None = None
    if agent.persona_id is not None:
        # A disabled or deleted persona is "no persona" for this run: the
        # assignment stays on the agent row, so re-enabling it takes effect
        # on the next run, but nothing of it reaches this run's prompt.
        persona_row = await session.scalar(
            select(Persona).where(
                Persona.id == agent.persona_id,
                Persona.workspace_id == workspace_id,
                Persona.enabled.is_(True),
            )
        )
        if persona_row is not None:
            persona = _persona_card(persona_row)

    return AgentExecutionSnapshot(
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        workspace_name=workspace.name if workspace is not None else "",
        name=agent.name,
        role_title=agent.role_title,
        system_prompt=agent.system_prompt,
        autonomy_level=agent.autonomy_level,
        team_id=agent.team_id,
        team_name=team_name,
        manager_agent_id=agent.manager_agent_id,
        manager_name=manager_name,
        model_profile=ModelProfileSnapshot(
            profile_id=profile.id,
            provider_id=provider.id,
            provider_type=provider.type,
            base_url=provider.base_url,
            secret_id=provider.secret_id,
            model_name=profile.model_name,
            display_name=profile.display_name,
            input_cost_micros_per_million=profile.input_cost_micros_per_million,
            output_cost_micros_per_million=profile.output_cost_micros_per_million,
            web_search=_enabled_web_search(profile.config_json),
            reasoning=_reasoning_override(profile.config_json, profile.supports_reasoning),
        ),
        temperature=agent.temperature,
        max_output_tokens=agent.max_output_tokens,
        run_limits=RunLimits(max_steps=agent.max_steps, max_run_minutes=agent.max_run_minutes),
        persona=persona,
    )
