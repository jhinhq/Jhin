"""Personas: how an agent acts and sounds, one card per row.

A ``persona`` row is a structured card — voice, stance, pace, how it handles
uncertainty, how it registers with people versus colleagues, a signature,
and a short ``never`` list — held as one validated JSON document in
``facets_json``. The card is ``jhin_personas.PersonaFacets`` at every
boundary: the TOML loader produces it, the API and the gateway accept it,
this column stores it, the run snapshot freezes it, and the prompt renders
it. One column rather than eight because the database could never be the
authority anyway (the per-facet cap is only one of the rules; the card cap,
the ``never`` item cap, and the content rules all live in ``jhin_personas``),
and because a new facet must not cost a migration.

Personas are workspace content, like skills: unique by ``(workspace_id,
name)``, with ``source`` saying who wrote the card. ``built_in`` rows are
Jhin's curated cast and stay read-only — a workspace duplicates one to edit
a copy. An agent wears a persona through ``Agent.persona_id``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonDict, JsonList, StdUuid, TimestampMixin, UuidPkMixin


class Persona(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "persona"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_persona_workspace_id_name"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    # Slug, the same rule as Skill.name: the agent-facing identifier.
    name: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(200))
    tags_json: Mapped[list[str]] = mapped_column(
        JsonList, default=list, server_default=text("'[]'")
    )
    # The whole card as one validated document: PersonaFacets.model_dump().
    facets_json: Mapped[dict[str, Any]] = mapped_column(
        JsonDict, default=dict, server_default=text("'{}'")
    )
    # built_in | custom | agent
    source: Mapped[str] = mapped_column(String(16), default="custom")
    # Set for source="custom": the admin who wrote it. SET NULL on delete so
    # a departed person does not take a workspace's personas with them.
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), nullable=True, default=None
    )
    # Set for source="agent": the agent whose persona.create call made it.
    created_by_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid,
        ForeignKey("agent.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        default=None,
    )
    # Workspace state, editable even on a built-in row: a disabled persona
    # keeps its assignments but stops rendering into any agent's prompt.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # custom/agent rows: bumped on any wording change. built_in rows: the
    # shipped pack's version, so "install missing defaults" can refresh a
    # card in place when the pack is newer without ever overwriting an edit.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
