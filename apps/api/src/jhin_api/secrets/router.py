"""Route handlers for /api/v1/workspaces/{workspace_id}/secrets (plan 13.4).

Secret management requires the admin role. GET returns masked metadata only;
no route in this module can emit plaintext.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.deps import AdminCtx, DbSession, SecretCryptoDep
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.secrets import service
from jhin_api.secrets.schemas import SecretCreate, SecretOut, SecretRotate, SecretUpdate
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import Secret

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/secrets",
    tags=["secrets"],
    dependencies=[Depends(csrf_protect)],
)


def _out(secret: Secret) -> SecretOut:
    return SecretOut.model_validate(secret, from_attributes=True)


@router.get("")
async def list_secrets(ctx: AdminCtx, db: DbSession) -> list[SecretOut]:
    return [_out(secret) for secret in await service.list_secrets(db, ctx.workspace_id)]


@router.post("", status_code=201)
async def create_secret(
    payload: SecretCreate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> SecretOut:
    secret = await service.create_secret(
        db,
        crypto,
        ctx,
        name=payload.name,
        value=payload.value,
        secret_type=payload.type,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _out(secret)


@router.patch("/{secret_id}")
async def rename_secret(
    secret_id: UUID, payload: SecretUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> SecretOut:
    secret = await service.rename_secret(
        db,
        ctx,
        secret_id,
        name=payload.name,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _out(secret)


@router.post("/{secret_id}/rotate")
async def rotate_secret(
    secret_id: UUID,
    payload: SecretRotate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
) -> SecretOut:
    secret = await service.rotate_secret(
        db,
        crypto,
        ctx,
        secret_id,
        value=payload.value,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _out(secret)


@router.delete("/{secret_id}", status_code=204)
async def delete_secret(secret_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    await service.delete_secret(
        db, ctx, secret_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
