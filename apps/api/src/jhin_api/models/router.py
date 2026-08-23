"""Routes for model providers and profiles (plan 19).

/api/v1/workspaces/{workspace_id}/model-providers  (+ /{id}/verify)
/api/v1/workspaces/{workspace_id}/model-profiles

Reading requires membership; managing requires admin.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.deps import AdminCtx, DbSession, ObservabilityRuntimeDep, SecretCryptoDep, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.models import service
from jhin_api.models.schemas import (
    ModelProfileCreate,
    ModelProfileOut,
    ModelProfileUpdate,
    ModelProviderCreate,
    ModelProviderOut,
    ModelProviderUpdate,
    ProviderDraftVerify,
    ProviderModelsResult,
    ProviderVerifyResult,
)
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import ModelProfile, ModelProvider

providers_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/model-providers",
    tags=["model-providers"],
    dependencies=[Depends(csrf_protect)],
)

profiles_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/model-profiles",
    tags=["model-profiles"],
    dependencies=[Depends(csrf_protect)],
)


def _provider_out(provider: ModelProvider) -> ModelProviderOut:
    return ModelProviderOut.model_validate(provider, from_attributes=True)


def _profile_out(profile: ModelProfile) -> ModelProfileOut:
    return ModelProfileOut.model_validate(profile, from_attributes=True)


@providers_router.get("")
async def list_providers(ctx: ViewerCtx, db: DbSession) -> list[ModelProviderOut]:
    return [_provider_out(p) for p in await service.list_providers(db, ctx.workspace_id)]


@providers_router.post("", status_code=201)
async def create_provider(
    payload: ModelProviderCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> ModelProviderOut:
    provider = await service.create_provider(
        db,
        ctx,
        values=payload.model_dump(),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _provider_out(provider)


@providers_router.get("/{provider_id}")
async def get_provider(provider_id: UUID, ctx: ViewerCtx, db: DbSession) -> ModelProviderOut:
    return _provider_out(await service.get_provider(db, ctx.workspace_id, provider_id))


@providers_router.patch("/{provider_id}")
async def update_provider(
    provider_id: UUID, payload: ModelProviderUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> ModelProviderOut:
    provider = await service.update_provider(
        db,
        ctx,
        provider_id,
        changes=payload.model_dump(exclude_unset=True),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _provider_out(provider)


@providers_router.delete("/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> None:
    await service.delete_provider(
        db, ctx, provider_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


@providers_router.post("/verify-draft")
async def verify_draft(
    payload: ProviderDraftVerify,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> ProviderVerifyResult:
    ok, detail = await service.verify_draft(
        db,
        crypto,
        ctx,
        provider_type=payload.type.value,
        base_url=payload.base_url,
        api_key=payload.api_key,
        secret_id=payload.secret_id,
        metrics=runtime.metrics,
        tracer=runtime.tracer,
    )
    return ProviderVerifyResult(ok=ok, detail=detail)


@providers_router.get("/{provider_id}/models")
async def list_provider_models(
    provider_id: UUID,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> ProviderModelsResult:
    models, detail = await service.list_provider_models(
        db, crypto, ctx, provider_id, runtime.metrics, runtime.tracer
    )
    return ProviderModelsResult(models=models, detail=detail)


@providers_router.post("/{provider_id}/verify")
async def verify_provider(
    provider_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> ProviderVerifyResult:
    ok, detail = await service.verify_provider(
        db,
        crypto,
        ctx,
        provider_id,
        runtime.metrics,
        runtime.tracer,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return ProviderVerifyResult(ok=ok, detail=detail)


@profiles_router.get("")
async def list_profiles(ctx: ViewerCtx, db: DbSession) -> list[ModelProfileOut]:
    return [_profile_out(p) for p in await service.list_profiles(db, ctx.workspace_id)]


@profiles_router.post("", status_code=201)
async def create_profile(
    payload: ModelProfileCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> ModelProfileOut:
    profile = await service.create_profile(
        db,
        ctx,
        values=payload.model_dump(),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _profile_out(profile)


@profiles_router.get("/{profile_id}")
async def get_profile(profile_id: UUID, ctx: ViewerCtx, db: DbSession) -> ModelProfileOut:
    return _profile_out(await service.get_profile(db, ctx.workspace_id, profile_id))


@profiles_router.patch("/{profile_id}")
async def update_profile(
    profile_id: UUID, payload: ModelProfileUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> ModelProfileOut:
    profile = await service.update_profile(
        db,
        ctx,
        profile_id,
        changes=payload.model_dump(exclude_unset=True),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _profile_out(profile)


@profiles_router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    await service.delete_profile(
        db, ctx, profile_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
