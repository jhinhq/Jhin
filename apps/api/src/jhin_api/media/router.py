"""Routes for agent avatars and authenticated media delivery.

- ``POST/DELETE .../agents/{agent_id}/avatar`` and ``POST .../avatar/generate``
  require admin (identity changes are management actions) plus CSRF.
- ``GET .../media/{asset_id}`` and generation status are viewer reads with
  exact workspace scoping; foreign ids are 404.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi import status as http_status

from jhin_api.deps import AdminCtx, DbSession, TemporalDep, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.media import service
from jhin_api.media.schemas import AvatarGenerateRequest, AvatarGenerationOut, AvatarOut
from jhin_api.media.urls import DEFAULT_AVATAR_SIZE
from jhin_api.security.csrf import csrf_protect
from jhin_media import MAX_UPLOAD_BYTES, MediaStore, PostgresMediaStore

# One day: variants are content-addressed (the asset id changes on every
# replacement), so private caching is safe; nosniff keeps browsers honest.
MEDIA_CACHE_CONTROL = "private, max-age=86400, immutable"

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}",
    tags=["media"],
    dependencies=[Depends(csrf_protect)],
)


def get_media_store(request: Request) -> MediaStore:
    store: MediaStore | None = getattr(request.app.state, "media_store", None)
    if store is None:
        store = PostgresMediaStore()
        request.app.state.media_store = store
    return store


MediaStoreDep = Annotated[MediaStore, Depends(get_media_store)]


@router.get("/agents/{agent_id}/avatar")
async def get_avatar(agent_id: UUID, ctx: ViewerCtx, db: DbSession) -> AvatarOut:
    from jhin_api.agents.service import get_agent

    return service.avatar_out(await get_agent(db, ctx.workspace_id, agent_id))


@router.post("/agents/{agent_id}/avatar", status_code=http_status.HTTP_201_CREATED)
async def upload_avatar(
    agent_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    store: MediaStoreDep,
    file: Annotated[UploadFile, File(description="PNG, JPEG, or WebP; at most 8 MB")],
) -> AvatarOut:
    # Read one byte past the limit so an oversized body is refused without
    # buffering the whole thing.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"code": "too_large", "message": "Avatar uploads are limited to 8 MB"},
        )
    agent = await service.upload_avatar(
        db,
        ctx,
        store,
        agent_id,
        data=data,
        declared_content_type=file.content_type,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return service.avatar_out(agent)


@router.delete("/agents/{agent_id}/avatar")
async def remove_avatar(
    agent_id: UUID, request: Request, ctx: AdminCtx, db: DbSession, store: MediaStoreDep
) -> AvatarOut:
    agent = await service.remove_avatar(
        db, ctx, store, agent_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return service.avatar_out(agent)


@router.post("/agents/{agent_id}/avatar/generate", status_code=http_status.HTTP_202_ACCEPTED)
async def generate_avatar(
    agent_id: UUID,
    payload: AvatarGenerateRequest,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    temporal: TemporalDep,
) -> AvatarGenerationOut:
    generation = await service.request_generation(
        db,
        ctx,
        temporal,
        agent_id,
        prompt_hint=payload.prompt_hint,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return service.generation_out(generation)


@router.get("/agents/{agent_id}/avatar/generation")
async def get_avatar_generation(
    agent_id: UUID, ctx: ViewerCtx, db: DbSession
) -> AvatarGenerationOut:
    return service.generation_out(await service.latest_generation(db, ctx.workspace_id, agent_id))


@router.get("/media/{asset_id}")
async def get_media(
    asset_id: UUID,
    request: Request,
    ctx: ViewerCtx,
    db: DbSession,
    store: MediaStoreDep,
    size: Annotated[int, Query(description="64, 128, or 256")] = DEFAULT_AVATAR_SIZE,
) -> Response:
    if size not in (64, 128, 256):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="size must be one of 64, 128, 256",
        )
    variant = await service.get_media_variant(db, store, ctx.workspace_id, asset_id, size)
    etag = f'"{variant.sha256[:32]}-{size}"'
    headers = {
        "Cache-Control": MEDIA_CACHE_CONTROL,
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{asset_id}-{size}.webp"',
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=http_status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=variant.data, media_type=variant.content_type, headers=headers)
