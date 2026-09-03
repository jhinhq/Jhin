"""Persona library service: RBAC, CRUD with versioning, the shipped cast,
and audit.

RBAC mirrors skills: any workspace member (viewer+) may read the library;
every mutation requires admin, because a persona is composed into an
agent's system prompt on every run. Every mutation is audited content-free.

Built-in rows (``source == "built_in"``) are the curated cast Jhin ships and
stay read-only: a workspace duplicates one to edit a copy, and may only
enable or disable the original. That is what makes "install missing
defaults" safe to double as "refresh": a newer shipped card can replace an
older built-in row in place without ever overwriting a person's edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.personas.schemas import (
    BUILT_IN_SOURCE,
    PersonaCreate,
    PersonaUpdate,
)
from jhin_api.slugs import with_suffix
from jhin_db.models import Agent, Persona
from jhin_domain import WorkspaceRole, role_satisfies
from jhin_personas import (
    MAX_DISPLAY_NAME_CHARS,
    MAX_NAME_CHARS,
    PERSONA_SOURCES,
    PersonaCard,
    load_builtin_personas,
)

MAX_PAGE_SIZE = 100
TARGET_TYPE = "persona"

_SOURCES = PERSONA_SOURCES
_COPY_NAME_SUFFIX = "-copy"
_COPY_DISPLAY_SUFFIX = " (copy)"
# What ``with_suffix`` appends: a hyphen and six hex characters.
_RANDOM_SUFFIX_CHARS = 7


@dataclass(frozen=True)
class BuiltinInstall:
    """What one pass over the shipped cast did to a workspace."""

    installed: list[str]
    refreshed: list[str]
    skipped: list[str]

    @property
    def names(self) -> list[str]:
        return [*self.installed, *self.refreshed]


def _require_admin(ctx: WorkspaceContext) -> None:
    if not role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Managing personas requires an admin")


def _audit(
    session: AsyncSession,
    ctx: WorkspaceContext,
    action: str,
    target_id: UUID | None,
    *,
    request_id: UUID | None,
    ip_hash: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    audit.record(
        session,
        action=action,
        target_type=TARGET_TYPE,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        target_id=target_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata=metadata,
    )


def _card_errors(error: ValidationError) -> list[dict[str, Any]]:
    """The same shape FastAPI gives a request-body 422, so the web can
    surface a service-side card failure under the field it belongs to."""
    return [
        {"type": item["type"], "loc": ["body", *item["loc"]], "msg": item["msg"]}
        for item in error.errors()
    ]


def _validated_card(values: dict[str, Any]) -> PersonaCard:
    try:
        return PersonaCard.model_validate(values)
    except ValidationError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_card_errors(error)
        ) from None


async def list_personas(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    q: str | None = None,
    source: str | None = None,
    tag: str | None = None,
    enabled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Persona], int]:
    if source is not None and source not in _SOURCES:
        raise HTTPException(422, f"invalid source {source!r}")
    query = select(Persona).where(Persona.workspace_id == workspace_id)
    if q:
        needle = f"%{q.lower()}%"
        query = query.where(
            func.lower(Persona.name).like(needle)
            | func.lower(Persona.display_name).like(needle)
            | func.lower(Persona.description).like(needle)
        )
    if source is not None:
        query = query.where(Persona.source == source)
    if enabled is not None:
        query = query.where(Persona.enabled.is_(enabled))
    rows = list(await db.scalars(query.order_by(Persona.display_name, Persona.name)))
    # The tag filter runs here rather than in SQL: a workspace holds a few
    # dozen cards at most, and JSON-list containment is not portable
    # between the SQLite the unit tests use and PostgreSQL.
    if tag is not None:
        rows = [row for row in rows if tag in row.tags_json]
    total = len(rows)
    start = max(offset, 0)
    return rows[start : start + min(max(limit, 1), MAX_PAGE_SIZE)], total


async def agent_counts(db: AsyncSession, workspace_id: UUID) -> dict[UUID, int]:
    """How many agents wear each persona, in one grouped query."""
    rows = await db.execute(
        select(Agent.persona_id, func.count())
        .where(Agent.workspace_id == workspace_id, Agent.persona_id.is_not(None))
        .group_by(Agent.persona_id)
    )
    return {persona_id: int(count) for persona_id, count in rows.all()}


async def get_persona(db: AsyncSession, workspace_id: UUID, persona_id: UUID) -> Persona:
    record = await db.scalar(
        select(Persona).where(Persona.id == persona_id, Persona.workspace_id == workspace_id)
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Persona not found")
    return record


async def persona_for_agent(db: AsyncSession, agent: Agent) -> Persona | None:
    """The row an agent's ``persona_id`` points at, disabled or not: the
    agent's own representation shows a switched-off persona as worn-but-off
    rather than pretending it was never assigned."""
    if agent.persona_id is None:
        return None
    record: Persona | None = await db.scalar(
        select(Persona).where(
            Persona.id == agent.persona_id, Persona.workspace_id == agent.workspace_id
        )
    )
    return record


async def _name_taken(db: AsyncSession, workspace_id: UUID, name: str) -> bool:
    return (
        await db.scalar(
            select(Persona.id).where(Persona.workspace_id == workspace_id, Persona.name == name)
        )
        is not None
    )


async def create_persona(
    db: AsyncSession,
    ctx: WorkspaceContext,
    payload: PersonaCreate,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> Persona:
    _require_admin(ctx)
    if await _name_taken(db, ctx.workspace_id, payload.name):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a persona named {payload.name!r} already exists"
        )
    record = Persona(
        workspace_id=ctx.workspace_id,
        name=payload.name,
        display_name=payload.display_name,
        description=payload.description,
        tags_json=list(payload.tags),
        facets_json=payload.facets.model_dump(),
        source="custom",
        created_by_user_id=ctx.user.id,
        enabled=True,
        version=1,
    )
    db.add(record)
    await db.flush()
    _audit(
        db,
        ctx,
        "persona.created",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": record.name, "source": record.source},
    )
    await db.commit()
    return record


async def update_persona(
    db: AsyncSession,
    ctx: WorkspaceContext,
    persona_id: UUID,
    payload: PersonaUpdate,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> Persona:
    _require_admin(ctx)
    record = await get_persona(db, ctx.workspace_id, persona_id)
    edits_card = any(
        value is not None
        for value in (payload.display_name, payload.description, payload.tags, payload.facets)
    )
    if edits_card and record.source == BUILT_IN_SOURCE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Built-in personas are read-only; duplicate it to edit a copy",
        )
    changed: list[str] = []
    if edits_card:
        # Merge, then validate the whole card once: the content rules and
        # the card total only mean something over the finished card.
        facets = payload.facets if payload.facets is not None else record.facets_json
        card = _validated_card(
            {
                "name": record.name,
                "display_name": payload.display_name or record.display_name,
                "description": payload.description or record.description,
                "tags": list(payload.tags if payload.tags is not None else record.tags_json),
                "facets": facets,
            }
        )
        facets_json = card.facets.model_dump()
        if card.display_name != record.display_name:
            record.display_name = card.display_name
            changed.append("display_name")
        if card.description != record.description:
            record.description = card.description
            changed.append("description")
        if list(card.tags) != list(record.tags_json):
            record.tags_json = list(card.tags)
            changed.append("tags")
        if facets_json != record.facets_json:
            record.facets_json = facets_json
            changed.append("facets")
    toggled: bool | None = None
    if payload.enabled is not None and payload.enabled != record.enabled:
        record.enabled = payload.enabled
        toggled = payload.enabled
        changed.append("enabled")
    # Tags are a gallery label, not part of what an agent reads; only a
    # change to the card's wording is a new version.
    if any(field in changed for field in ("display_name", "description", "facets")):
        record.version += 1
    await db.flush()
    _audit(
        db,
        ctx,
        "persona.updated",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": record.name, "version": record.version, "changed_fields": changed},
    )
    if toggled is not None:
        _audit(
            db,
            ctx,
            "persona.enabled" if toggled else "persona.disabled",
            record.id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={"name": record.name},
        )
    await db.commit()
    return record


async def set_enabled(
    db: AsyncSession,
    ctx: WorkspaceContext,
    persona_id: UUID,
    enabled: bool,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> Persona:
    """Enable/disable as a thin wrapper over the same toggle. Disabling keeps
    every ``Agent.persona_id`` that points here; the run snapshot simply
    stops rendering the card."""
    return await update_persona(
        db,
        ctx,
        persona_id,
        PersonaUpdate(enabled=enabled),
        request_id=request_id,
        ip_hash=ip_hash,
    )


async def delete_persona(
    db: AsyncSession,
    ctx: WorkspaceContext,
    persona_id: UUID,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> None:
    _require_admin(ctx)
    record = await get_persona(db, ctx.workspace_id, persona_id)
    if record.source == BUILT_IN_SOURCE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Built-in personas cannot be deleted; disable it instead",
        )
    name = record.name
    # SQLite in unit tests does not run the FK's SET NULL; detach the
    # agents explicitly (a no-op on PostgreSQL, where the FK does it).
    detached = int(await db.scalar(select(func.count()).where(Agent.persona_id == record.id)) or 0)
    await db.execute(update(Agent).where(Agent.persona_id == record.id).values(persona_id=None))
    await db.delete(record)
    _audit(
        db,
        ctx,
        "persona.deleted",
        persona_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": name, "detached_agents": detached},
    )
    await db.commit()


def _copy_name(base: str) -> str:
    trimmed = base[: MAX_NAME_CHARS - len(_COPY_NAME_SUFFIX)].rstrip("-")
    return f"{trimmed}{_COPY_NAME_SUFFIX}"


def _copy_display_name(base: str) -> str:
    trimmed = base[: MAX_DISPLAY_NAME_CHARS - len(_COPY_DISPLAY_SUFFIX)].rstrip()
    return f"{trimmed}{_COPY_DISPLAY_SUFFIX}"


async def duplicate_persona(
    db: AsyncSession,
    ctx: WorkspaceContext,
    persona_id: UUID,
    *,
    name: str | None = None,
    display_name: str | None = None,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> Persona:
    """Copy a persona into a new custom one — the way a built-in gets edited."""
    _require_admin(ctx)
    source = await get_persona(db, ctx.workspace_id, persona_id)
    if name is not None:
        if await _name_taken(db, ctx.workspace_id, name):
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"a persona named {name!r} already exists"
            )
        new_name = name
    else:
        new_name = _copy_name(source.name)
        if await _name_taken(db, ctx.workspace_id, new_name):
            new_name = with_suffix(new_name[: MAX_NAME_CHARS - _RANDOM_SUFFIX_CHARS].rstrip("-"))
    card = _validated_card(
        {
            "name": new_name,
            "display_name": display_name or _copy_display_name(source.display_name),
            "description": source.description,
            "tags": list(source.tags_json),
            "facets": source.facets_json,
        }
    )
    record = Persona(
        workspace_id=ctx.workspace_id,
        name=card.name,
        display_name=card.display_name,
        description=card.description,
        tags_json=list(card.tags),
        facets_json=card.facets.model_dump(),
        source="custom",
        created_by_user_id=ctx.user.id,
        enabled=True,
        version=1,
    )
    db.add(record)
    await db.flush()
    _audit(
        db,
        ctx,
        "persona.created",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "name": record.name,
            "source": record.source,
            "duplicated_from": str(source.id),
        },
    )
    await db.commit()
    return record


async def _install_builtins_core(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    actor_id: UUID | None,
    request_id: UUID | None,
    ip_hash: str | None,
    source: str,
) -> BuiltinInstall:
    """Install the shipped cast; refresh a built-in row the pack has moved
    past; leave everything else alone.

    Stages the rows and the audit event in the caller's transaction — does
    not commit — so a caller building a workspace can install the cast
    atomically with the rest of workspace creation. A row that is not
    ``built_in`` but shares a shipped name (someone's own card, made before
    the cast arrived) is skipped: it is theirs, not ours to replace.
    """
    installed: list[str] = []
    refreshed: list[str] = []
    skipped: list[str] = []
    for built in load_builtin_personas():
        card = built.card
        existing = await db.scalar(
            select(Persona).where(Persona.workspace_id == workspace_id, Persona.name == card.name)
        )
        if existing is None:
            db.add(
                Persona(
                    workspace_id=workspace_id,
                    name=card.name,
                    display_name=card.display_name,
                    description=card.description,
                    tags_json=list(card.tags),
                    facets_json=card.facets.model_dump(),
                    source=BUILT_IN_SOURCE,
                    enabled=True,
                    version=built.version,
                )
            )
            installed.append(card.name)
            continue
        if existing.source == BUILT_IN_SOURCE and existing.version < built.version:
            # Built-ins are read-only, so this replaces a shipped card with
            # a newer shipped card and never a person's wording.
            existing.display_name = card.display_name
            existing.description = card.description
            existing.tags_json = list(card.tags)
            existing.facets_json = card.facets.model_dump()
            existing.version = built.version
            refreshed.append(card.name)
            continue
        skipped.append(card.name)
    await db.flush()
    audit.record(
        db,
        action="persona.builtins_installed",
        target_type=TARGET_TYPE,
        workspace_id=workspace_id,
        actor_id=actor_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "installed": len(installed),
            "refreshed": len(refreshed),
            "skipped": len(skipped),
            "source": source,
        },
    )
    return BuiltinInstall(installed=installed, refreshed=refreshed, skipped=skipped)


async def install_builtins(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> BuiltinInstall:
    """Admin action ("Install missing defaults"): idempotently install any of
    the shipped cast this workspace is still missing and refresh any
    built-in card the pack has moved past. Custom rows are never touched."""
    _require_admin(ctx)
    result = await _install_builtins_core(
        db,
        ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        source="manual",
    )
    await db.commit()
    return result


async def install_builtin_personas_for_new_workspace(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    actor_id: UUID,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> BuiltinInstall:
    """Called by workspace creation: every new workspace starts with the
    shipped cast installed and enabled. No admin check — the caller is still
    building the workspace and its owner membership in the same transaction;
    this never runs against an *existing* workspace (that stays the explicit,
    idempotent ``install_builtins`` admin action above)."""
    return await _install_builtins_core(
        db,
        workspace_id,
        actor_id=actor_id,
        request_id=request_id,
        ip_hash=ip_hash,
        source="default",
    )
