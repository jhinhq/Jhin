"""Routes for the persona library.

/api/v1/workspaces/{workspace_id}/personas     library CRUD, install-builtins,
                                              enable/disable, duplicate

Which agent wears which persona is set on the agent itself
(``PATCH /agents/{agent_id}`` with ``persona_id``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from jhin_api.deps import AdminCtx, DbSession, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.personas import service
from jhin_api.personas.schemas import (
    InstallBuiltinPersonasOut,
    PersonaCreate,
    PersonaDuplicateIn,
    PersonaListOut,
    PersonaOut,
    PersonaUpdate,
)
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import Persona

personas_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/personas",
    tags=["personas"],
    dependencies=[Depends(csrf_protect)],
)


async def _out(db: DbSession, record: Persona) -> PersonaOut:
    counts = await service.agent_counts(db, record.workspace_id)
    return PersonaOut.from_record(record, agent_count=counts.get(record.id, 0))


@personas_router.get("")
async def list_personas(
    ctx: ViewerCtx,
    db: DbSession,
    q: str | None = None,
    source: str | None = None,
    tag: str | None = None,
    enabled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> PersonaListOut:
    items, total = await service.list_personas(
        db,
        ctx.workspace_id,
        q=q,
        source=source,
        tag=tag,
        enabled=enabled,
        limit=limit,
        offset=offset,
    )
    counts = await service.agent_counts(db, ctx.workspace_id)
    return PersonaListOut(
        items=[
            PersonaOut.from_record(record, agent_count=counts.get(record.id, 0)) for record in items
        ],
        total=total,
    )


@personas_router.post("", status_code=status.HTTP_201_CREATED)
async def create_persona(
    payload: PersonaCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> PersonaOut:
    """Create a custom persona. A persona takes effect on an agent's next
    run, never mid-run."""
    record = await service.create_persona(
        db, ctx, payload, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return await _out(db, record)


@personas_router.post("/install-builtins")
async def install_builtins(
    request: Request, ctx: AdminCtx, db: DbSession
) -> InstallBuiltinPersonasOut:
    """Install the shipped cast (idempotent): missing cards are added, a
    built-in card older than the shipped pack is refreshed in place, and
    custom cards are never touched."""
    result = await service.install_builtins(
        db, ctx, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return InstallBuiltinPersonasOut(
        installed=len(result.installed),
        refreshed=len(result.refreshed),
        skipped=len(result.skipped),
        names=result.names,
    )


@personas_router.get("/{persona_id}")
async def get_persona(persona_id: UUID, ctx: ViewerCtx, db: DbSession) -> PersonaOut:
    return await _out(db, await service.get_persona(db, ctx.workspace_id, persona_id))


@personas_router.patch("/{persona_id}")
async def update_persona(
    persona_id: UUID, payload: PersonaUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> PersonaOut:
    """Edit a custom persona. A built-in persona is read-only apart from
    ``enabled``; duplicate it to edit a copy."""
    record = await service.update_persona(
        db, ctx, persona_id, payload, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return await _out(db, record)


@personas_router.delete("/{persona_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_persona(persona_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    """Delete a custom persona; agents wearing it are left with none."""
    await service.delete_persona(
        db, ctx, persona_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


@personas_router.post("/{persona_id}/enable")
async def enable_persona(
    persona_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> PersonaOut:
    record = await service.set_enabled(
        db, ctx, persona_id, True, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return await _out(db, record)


@personas_router.post("/{persona_id}/disable")
async def disable_persona(
    persona_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> PersonaOut:
    """Switch a persona off. Agents keep their assignment, but their next
    run renders no persona until it is enabled again."""
    record = await service.set_enabled(
        db, ctx, persona_id, False, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return await _out(db, record)


@personas_router.post("/{persona_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_persona(
    persona_id: UUID,
    payload: PersonaDuplicateIn,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> PersonaOut:
    """Copy a persona into a new custom one — how a built-in gets edited."""
    record = await service.duplicate_persona(
        db,
        ctx,
        persona_id,
        name=payload.name,
        display_name=payload.display_name,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return await _out(db, record)
