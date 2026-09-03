"""Schemas for the persona library and per-agent assignment.

A persona is a structured card (``jhin_personas.PersonaCard``): identity,
tags, and named facets. The card model *is* the create body, so the content
rules run at the API boundary and a 422 names the facet that broke one.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from jhin_personas import (
    MAX_DESCRIPTION_CHARS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_NAME_CHARS,
    MAX_TAGS,
    PersonaCard,
    PersonaFacets,
    is_valid_persona_name,
)

if TYPE_CHECKING:
    from jhin_db.models import Persona

# The facets as the API accepts them: the package's own model, so every
# cap and content rule applies before a service ever sees the card.
PersonaFacetsIn = PersonaFacets

BUILT_IN_SOURCE = "built_in"


class PersonaFacetsOut(BaseModel):
    voice: str
    stance: str
    pace: str
    when_unsure: str
    with_people: str
    with_teammates: str
    signature: str
    never: list[str]


class PersonaOut(BaseModel):
    """One persona. No list/detail split: a card is at most 1.5 KB, unlike a
    skill body."""

    id: UUID
    workspace_id: UUID
    name: str
    display_name: str
    description: str
    tags: list[str]
    source: str
    facets: PersonaFacetsOut
    enabled: bool
    version: int
    # True for the shipped cast: the web renders Duplicate instead of Edit.
    read_only: bool
    # Agents wearing it (one grouped count query per list call).
    agent_count: int = 0
    created_by_user_id: UUID | None
    created_by_agent_id: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: Persona, *, agent_count: int = 0) -> PersonaOut:
        return cls(
            id=record.id,
            workspace_id=record.workspace_id,
            name=record.name,
            display_name=record.display_name,
            description=record.description,
            tags=list(record.tags_json),
            source=record.source,
            facets=PersonaFacetsOut.model_validate(record.facets_json),
            enabled=record.enabled,
            version=record.version,
            read_only=record.source == BUILT_IN_SOURCE,
            agent_count=agent_count,
            created_by_user_id=record.created_by_user_id,
            created_by_agent_id=record.created_by_agent_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class PersonaListOut(BaseModel):
    items: list[PersonaOut]
    total: int


class InstallBuiltinPersonasOut(BaseModel):
    installed: int
    # A built-in row whose card was older than the shipped pack and was
    # brought up to date in place.
    refreshed: int
    skipped: int
    # The cards this call wrote: installed or refreshed.
    names: list[str]


class AgentPersonaSummary(BaseModel):
    """The persona an agent wears, as the agent's own representation shows it."""

    id: UUID
    name: str
    display_name: str
    tags: list[str]
    enabled: bool

    @classmethod
    def from_record(cls, record: Persona) -> AgentPersonaSummary:
        return cls(
            id=record.id,
            name=record.name,
            display_name=record.display_name,
            tags=list(record.tags_json),
            enabled=record.enabled,
        )


class PersonaCreate(PersonaCard):
    """The card itself: name, display name, description, tags, facets.

    Inheriting the card rather than restating it keeps one validation path:
    a create body that would not be a valid card is a 422 here, naming the
    field, and never reaches the service.
    """


class PersonaUpdate(BaseModel):
    """Partial update; the name is immutable (it is the agent-facing id).

    Only sizes are checked here. The service merges the change into the
    stored card and validates the result as one ``PersonaCard``, which is
    where the content rules and the card total live.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=MAX_DISPLAY_NAME_CHARS)
    description: str | None = Field(default=None, min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)
    facets: PersonaFacetsIn | None = None
    enabled: bool | None = None


class PersonaDuplicateIn(BaseModel):
    """Copy a persona (usually a read-only built-in) into an editable custom one."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_CHARS)
    display_name: str | None = Field(default=None, min_length=1, max_length=MAX_DISPLAY_NAME_CHARS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_persona_name(value):
            raise ValueError(
                "persona names use lowercase letters, digits, and hyphens "
                f"(at most {MAX_NAME_CHARS} characters)"
            )
        return value
