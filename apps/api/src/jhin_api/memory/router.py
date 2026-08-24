"""Routes for curated memory.

/api/v1/workspaces/{workspace_id}/memories   list/create/get/edit,
                                            pin, contest, forget, approve, reject,
                                            embed-missing (admin backfill)
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from jhin_api.deps import AdminCtx, DbSession, MemberCtx, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.memory import service
from jhin_api.memory.schemas import (
    ContestIn,
    DeduplicateOut,
    EmbedMissingIn,
    EmbedMissingOut,
    MemoryCreate,
    MemoryListOut,
    MemoryOut,
    MemoryUpdate,
    PinIn,
)
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import MemoryRecord
from jhin_domain import MemoryScope, MemoryStatus
from jhin_observability import ObservabilityRuntime

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/memories",
    tags=["memory"],
    dependencies=[Depends(csrf_protect)],
)


def _embedding_deps(request: Request) -> service.EmbeddingDeps:
    """Soft dependencies: embeddings are best-effort, so a missing master key
    or observability runtime must not turn a memory write into a 503."""
    runtime = getattr(request.app.state, "observability", None)
    metrics = runtime.metrics if isinstance(runtime, ObservabilityRuntime) else None
    tracer = runtime.tracer if isinstance(runtime, ObservabilityRuntime) else None
    return service.EmbeddingDeps(
        crypto=getattr(request.app.state, "secret_crypto", None), metrics=metrics, tracer=tracer
    )


def _out(record: MemoryRecord) -> MemoryOut:
    out = MemoryOut.model_validate(record)
    return out.model_copy(update={"has_embedding": bool(record.embedding_json)})


@router.get("")
async def list_memories(
    ctx: ViewerCtx,
    db: DbSession,
    scope: str | None = None,
    agent_id: UUID | None = None,
    team_id: UUID | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> MemoryListOut:
    scope_filter: MemoryScope | None = None
    status_filter: MemoryStatus | None = None
    try:
        if scope is not None:
            scope_filter = MemoryScope(scope)
        if status is not None:
            status_filter = MemoryStatus(status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid filter: {exc}") from None
    items, total = await service.list_memories(
        db,
        ctx.workspace_id,
        scope=scope_filter,
        agent_id=agent_id,
        team_id=team_id,
        status_filter=status_filter,
        q=q,
        limit=limit,
        offset=offset,
    )
    return MemoryListOut(items=[_out(r) for r in items], total=total)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_memory(
    payload: MemoryCreate, request: Request, ctx: MemberCtx, db: DbSession
) -> MemoryOut:
    record = await service.create_memory(
        db,
        ctx,
        payload,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
        embedding=_embedding_deps(request),
    )
    return _out(record)


@router.post("/embed-missing")
async def embed_missing(
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    payload: EmbedMissingIn | None = None,
) -> EmbedMissingOut:
    limit = payload.limit if payload is not None else EmbedMissingIn().limit
    embedded, remaining, model, dimensions = await service.embed_missing(
        db,
        ctx,
        embedding=_embedding_deps(request),
        limit=limit,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return EmbedMissingOut(
        embedded=embedded, remaining=remaining, model=model, dimensions=dimensions
    )


@router.post("/deduplicate")
async def deduplicate(request: Request, ctx: AdminCtx, db: DbSession) -> DeduplicateOut:
    """Retroactive cleanup: collapse active near-duplicates per scope (admin).

    Keeps the best record of each duplicate cluster and supersedes the rest;
    gray-zone paraphrase pairs are adjudicated by the workspace default chat
    model when one exists (``llm`` reports whether smart matching ran);
    audited content-free as ``memory.deduplicated``."""
    clusters, superseded, remaining, adjudicated, llm = await service.deduplicate_memories(
        db,
        ctx,
        deps=_embedding_deps(request),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return DeduplicateOut(
        clusters=clusters,
        superseded=superseded,
        remaining_active=remaining,
        adjudicated=adjudicated,
        llm=llm,
    )


@router.get("/{memory_id}")
async def get_memory(memory_id: UUID, ctx: ViewerCtx, db: DbSession) -> MemoryOut:
    return _out(await service.get_memory(db, ctx.workspace_id, memory_id))


@router.patch("/{memory_id}")
async def update_memory(
    memory_id: UUID, payload: MemoryUpdate, request: Request, ctx: MemberCtx, db: DbSession
) -> MemoryOut:
    record = await service.update_memory(
        db,
        ctx,
        memory_id,
        payload,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
        embedding=_embedding_deps(request),
    )
    return _out(record)


@router.post("/{memory_id}/pin")
async def pin_memory(
    memory_id: UUID,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    payload: PinIn | None = None,
) -> MemoryOut:
    pinned = payload.pinned if payload is not None else True
    record = await service.pin_memory(
        db, ctx, memory_id, pinned=pinned, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(record)


@router.post("/{memory_id}/contest")
async def contest_memory(
    memory_id: UUID,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    payload: ContestIn | None = None,
) -> MemoryOut:
    reason = payload.reason if payload is not None else ""
    record = await service.contest_memory(
        db, ctx, memory_id, reason=reason, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(record)


@router.post("/{memory_id}/forget")
async def forget_memory(
    memory_id: UUID, request: Request, ctx: MemberCtx, db: DbSession
) -> MemoryOut:
    record = await service.forget_memory(
        db, ctx, memory_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(record)


@router.post("/{memory_id}/approve")
async def approve_memory(
    memory_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> MemoryOut:
    record = await service.approve_memory(
        db, ctx, memory_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(record)


@router.post("/{memory_id}/reject")
async def reject_memory(
    memory_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> MemoryOut:
    record = await service.reject_memory(
        db, ctx, memory_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(record)
