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
from jhin_api.models import ollama_service, pricing_service, service
from jhin_api.models.schemas import (
    CatalogRefreshResultOut,
    ModelProfileCreate,
    ModelProfileOut,
    ModelProfileUpdate,
    ModelProviderCreate,
    ModelProviderOut,
    ModelProviderUpdate,
    OllamaLoadedModelOut,
    OllamaLoadedResult,
    OllamaLoadIn,
    OllamaLoadResultOut,
    OllamaModelDetailsOut,
    OllamaModelDetailsResult,
    OllamaModelOut,
    OllamaModelsResult,
    OllamaUnloadIn,
    PricingStatusOut,
    ProfilePricingRefreshResult,
    ProviderBalanceOut,
    ProviderDraftVerify,
    ProviderModelEntry,
    ProviderModelsResult,
    ProviderSpendOut,
    ProviderVerifyResult,
    ReconcilePricingResult,
    UntrackedModelOut,
    WorkspaceSpendOut,
)
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import ModelProfile, ModelProvider
from jhin_domain import ModelProviderType
from jhin_models.pricing import CATALOG_UPDATED

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

spend_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/spend",
    tags=["model-providers"],
    dependencies=[Depends(csrf_protect)],
)


def _provider_out(provider: ModelProvider) -> ModelProviderOut:
    return ModelProviderOut.model_validate(provider, from_attributes=True)


def _profile_out(profile: ModelProfile) -> ModelProfileOut:
    return ModelProfileOut.model_validate(profile, from_attributes=True)


@providers_router.get("")
async def list_providers(ctx: AdminCtx, db: DbSession) -> list[ModelProviderOut]:
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
async def get_provider(provider_id: UUID, ctx: AdminCtx, db: DbSession) -> ModelProviderOut:
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
    return ProviderModelsResult(
        models=[ProviderModelEntry.model_validate(m, from_attributes=True) for m in models],
        detail=detail,
        catalog_updated=CATALOG_UPDATED,
    )


@providers_router.get("/{provider_id}/balance")
async def provider_balance(
    provider_id: UUID,
    # Admin, not viewer: this decrypts the provider credential and makes a live
    # authenticated billing call, exactly like the sibling `/models` route.
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> ProviderBalanceOut:
    balance = await service.get_provider_balance(
        db, crypto, ctx, provider_id, runtime.metrics, runtime.tracer
    )
    return ProviderBalanceOut.model_validate(balance, from_attributes=True)


@providers_router.get("/{provider_id}/ollama/models")
async def list_ollama_models(
    provider_id: UUID,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> OllamaModelsResult:
    """Models installed on an Ollama host, and which of them are loaded in memory."""
    snapshot = await ollama_service.list_ollama_models(
        db, crypto, ctx, provider_id, runtime.metrics, runtime.tracer
    )
    return OllamaModelsResult(
        models=[
            OllamaModelOut.model_validate(row, from_attributes=True) for row in snapshot.models
        ],
        detail=snapshot.detail,
        fetched_at=snapshot.fetched_at,
    )


@providers_router.get("/{provider_id}/ollama/loaded")
async def list_ollama_loaded(
    provider_id: UUID,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> OllamaLoadedResult:
    """Models an Ollama host holds in memory right now; cheap enough to poll."""
    snapshot = await ollama_service.list_loaded_models(
        db, crypto, ctx, provider_id, runtime.metrics, runtime.tracer
    )
    return OllamaLoadedResult(
        models=[
            OllamaLoadedModelOut.model_validate(row, from_attributes=True)
            for row in snapshot.models
        ],
        detail=snapshot.detail,
        fetched_at=snapshot.fetched_at,
    )


# Declared after the bare ``/ollama/models`` route: model names carry ``:``
# and ``/`` (``tripolskypetr/qwen3.6-uncensored-aggressive:latest``), so the
# name is the rest of the path rather than one segment.
@providers_router.get("/{provider_id}/ollama/models/{name:path}")
async def show_ollama_model(
    provider_id: UUID,
    name: str,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> OllamaModelDetailsResult:
    """Architecture, capabilities and licence of one model on an Ollama host."""
    details, detail = await ollama_service.show_ollama_model(
        db, crypto, ctx, provider_id, runtime.metrics, runtime.tracer, name=name
    )
    return OllamaModelDetailsResult(
        model=(
            OllamaModelDetailsOut.model_validate(details, from_attributes=True)
            if details is not None
            else None
        ),
        detail=detail,
    )


@providers_router.post("/{provider_id}/ollama/load")
async def load_ollama_model(
    provider_id: UUID,
    payload: OllamaLoadIn,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> OllamaLoadResultOut:
    """Load a model into memory on an Ollama host; slow loads answer `loading` and carry on."""
    outcome = await ollama_service.load_ollama_model(
        db,
        crypto,
        ctx,
        provider_id,
        runtime.metrics,
        runtime.tracer,
        model=payload.model,
        keep_alive=payload.keep_alive,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return OllamaLoadResultOut.model_validate(outcome, from_attributes=True)


@providers_router.post("/{provider_id}/ollama/unload")
async def unload_ollama_model(
    provider_id: UUID,
    payload: OllamaUnloadIn,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> OllamaLoadResultOut:
    """Unload a model from memory on an Ollama host."""
    outcome = await ollama_service.unload_ollama_model(
        db,
        crypto,
        ctx,
        provider_id,
        runtime.metrics,
        runtime.tracer,
        model=payload.model,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return OllamaLoadResultOut.model_validate(outcome, from_attributes=True)


@spend_router.get("")
async def workspace_spend(ctx: ViewerCtx, db: DbSession) -> WorkspaceSpendOut:
    spend = await service.get_workspace_spend(db, ctx.workspace_id)
    # Runs on unpriced models contributed $0 to the totals above. Reporting
    # them alongside is the difference between "you spent this much" and "you
    # spent at least this much"; only the second one is true.
    untracked = await pricing_service.untracked_models(db, ctx.workspace_id, spend.period_start)
    return WorkspaceSpendOut(
        spent_month_micros=spend.spent_month_micros,
        spent_total_micros=spend.spent_total_micros,
        period_start=spend.period_start,
        providers=[
            ProviderSpendOut(
                provider_id=entry.provider.id,
                display_name=entry.provider.display_name,
                type=ModelProviderType(entry.provider.type),
                spent_month_micros=entry.spent_month_micros,
                spent_total_micros=entry.spent_total_micros,
            )
            for entry in spend.providers
        ],
        deleted_model_month_micros=spend.deleted_model_month_micros,
        deleted_model_total_micros=spend.deleted_model_total_micros,
        monthly_budget_micros=spend.monthly_budget_micros,
        warning_threshold=spend.warning_threshold,
        fetched_at=spend.fetched_at,
        untracked=[
            UntrackedModelOut.model_validate(row, from_attributes=True) for row in untracked
        ],
        untracked_runs=sum(row.runs for row in untracked),
    )


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


@profiles_router.get("/pricing-status")
async def pricing_status(ctx: ViewerCtx, db: DbSession) -> PricingStatusOut:
    """Every profile's price, its provenance, and what could improve it."""
    status = await pricing_service.pricing_status(
        db, ctx.workspace_id, since=service.month_start_utc()
    )
    return PricingStatusOut.model_validate(status, from_attributes=True)


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


@profiles_router.post("/{profile_id}/refresh-pricing")
async def refresh_profile_pricing(
    profile_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
    # A price an admin typed is never replaced silently. ``force=true`` is the
    # UI's "update anyway" after showing the difference.
    force: bool = False,
) -> ProfilePricingRefreshResult:
    profile, updated, source, detail = await service.refresh_profile_pricing(
        db,
        crypto,
        ctx,
        profile_id,
        runtime.metrics,
        runtime.tracer,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
        force=force,
    )
    return ProfilePricingRefreshResult(
        updated=updated, source=source, detail=detail, profile=_profile_out(profile)
    )


@profiles_router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    await service.delete_profile(
        db, ctx, profile_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


@profiles_router.post("/reconcile-pricing")
async def reconcile_pricing(
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> ReconcilePricingResult:
    """Measure real per-token rates from what the provider actually billed.

    Admin: this decrypts the billing credential and makes a live authenticated
    call, exactly like the sibling `/balance` route.
    """
    result = await pricing_service.reconcile_pricing(
        db,
        crypto,
        ctx,
        runtime.metrics,
        runtime.tracer,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return ReconcilePricingResult.model_validate(result, from_attributes=True)


@profiles_router.post("/refresh-catalog")
async def refresh_price_catalog(
    request: Request, ctx: AdminCtx, db: DbSession
) -> CatalogRefreshResultOut:
    """Pull the LiteLLM community price map and merge it as a catalog layer."""
    result = await pricing_service.refresh_price_catalog(
        db, ctx, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return CatalogRefreshResultOut.model_validate(result, from_attributes=True)
