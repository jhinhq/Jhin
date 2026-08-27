"""Model provider/profile business logic (plan 6.7, 6.8, 15).

Verification decrypts the provider credential in process memory, makes one
cheap live call through the adapter, and discards the plaintext (plan 13.5).
The credential never appears in the response, the audit row, or logs (the
process redactor knows the value after decryption).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, status
from opentelemetry.trace import Tracer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, AgentRun, ModelProfile, ModelProvider, Secret, Workspace
from jhin_models import (
    AccountStatus,
    AccountStatusUnsupported,
    ModelClient,
    ModelListing,
    ModelProviderError,
    ReasoningConfig,
    WebSearchConfig,
    build_model_client,
    reasoning_unsupported_reason,
    web_search_unsupported_reason,
)
from jhin_models.factory import ProviderConfigError
from jhin_models.pricing import (
    PriceCandidate,
    PriceSource,
    describe_price_source,
    resolve_price,
)
from jhin_observability import JhinMetrics
from jhin_secrets import SecretCrypto, SecretStore
from jhin_secrets.material import register_secret_material
from jhin_secrets.redaction import redact_text

# Provider billing APIs are polled by the UI; one live call per provider per
# minute is plenty and keeps us polite to the billing endpoints.
ACCOUNT_STATUS_CACHE_TTL_SECONDS = 60.0
# Best-effort: a slow billing API must not hold the balance request hostage.
ACCOUNT_STATUS_TIMEOUT_SECONDS = 8.0


def _provider_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model provider not found")


def _profile_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model profile not found")


def _build_verification_client(
    provider_type: str,
    base_url: str | None,
    api_key: str | None,
    metrics: JhinMetrics,
    tracer: Tracer,
    admin_api_key: str | None = None,
) -> ModelClient:
    """The single factory call site for provider checks (audited by telemetry tests)."""
    return build_model_client(
        provider_type,
        base_url=base_url,
        api_key=api_key,
        admin_api_key=admin_api_key,
        metrics=metrics,
        tracer=tracer,
    )


async def provider_client(
    db: AsyncSession,
    crypto: SecretCrypto,
    workspace_id: UUID,
    provider: ModelProvider,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    with_admin_key: bool = False,
) -> ModelClient:
    """Adapter for a saved provider; credentials are revealed in memory only."""
    store = SecretStore(db, crypto)
    api_key: str | None = None
    if provider.secret_id is not None:
        api_key = await store.reveal(workspace_id, provider.secret_id)
    admin_api_key: str | None = None
    if with_admin_key and provider.admin_secret_id is not None:
        admin_api_key = await store.reveal(workspace_id, provider.admin_secret_id)
    return _build_verification_client(
        provider.type, provider.base_url, api_key, metrics, tracer, admin_api_key=admin_api_key
    )


async def _validate_secret(db: AsyncSession, workspace_id: UUID, secret_id: UUID | None) -> None:
    if secret_id is None:
        return
    exists = await db.scalar(
        select(Secret.id).where(Secret.id == secret_id, Secret.workspace_id == workspace_id)
    )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="secret_id does not reference a secret in this workspace",
        )


# --- Providers ---


async def list_providers(db: AsyncSession, workspace_id: UUID) -> list[ModelProvider]:
    rows = await db.scalars(
        select(ModelProvider)
        .where(ModelProvider.workspace_id == workspace_id)
        .order_by(ModelProvider.created_at)
    )
    return list(rows)


async def get_provider(db: AsyncSession, workspace_id: UUID, provider_id: UUID) -> ModelProvider:
    provider = await db.scalar(
        select(ModelProvider).where(
            ModelProvider.id == provider_id, ModelProvider.workspace_id == workspace_id
        )
    )
    if provider is None:
        raise _provider_not_found()
    return provider


async def create_provider(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> ModelProvider:
    await _validate_secret(db, ctx.workspace_id, values.get("secret_id"))
    await _validate_secret(db, ctx.workspace_id, values.get("admin_secret_id"))
    provider = ModelProvider(workspace_id=ctx.workspace_id, **values)
    db.add(provider)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A provider with this name already exists in the workspace",
        ) from exc
    audit.record(
        db,
        action="provider.created",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"display_name": provider.display_name, "type": provider.type},
    )
    await db.commit()
    return provider


async def update_provider(
    db: AsyncSession,
    ctx: WorkspaceContext,
    provider_id: UUID,
    *,
    changes: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> ModelProvider:
    provider = await get_provider(db, ctx.workspace_id, provider_id)
    if "secret_id" in changes:
        await _validate_secret(db, ctx.workspace_id, changes["secret_id"])
    if "admin_secret_id" in changes:
        await _validate_secret(db, ctx.workspace_id, changes["admin_secret_id"])
    for field, value in changes.items():
        setattr(provider, field, value)
    _ACCOUNT_STATUS_CACHE.pop(provider.id, None)  # credentials may have changed
    audit.record(
        db,
        action="provider.updated",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed_fields": sorted(changes)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A provider with this name already exists in the workspace",
        ) from exc
    return provider


async def _hand_over_workspace_default(
    db: AsyncSession, workspace_id: UUID, *, losing: Sequence[UUID], subject: str
) -> ModelProfile | None:
    """Move the workspace default off profiles that are about to disappear.

    An agent with no model of its own runs on the workspace default, so
    emptying that slot never fails loudly — every such agent simply starts
    failing at its next run ("no model profile and the workspace has no
    default"). A surviving profile therefore inherits the slot, mirroring the
    "the first profile becomes the default" rule in ``create_profile``. When
    nothing survives to inherit it *and* agents are relying on the default,
    refuse instead and say what to add first.

    Returns the profile promoted into the slot, or None when the default was
    never at stake or nothing is left to promote.
    """
    # Pin the workspace explicitly: this is a *write*, and selecting the row
    # by profile id alone would let a mis-scoped profile id clear another
    # workspace's default.
    workspace = await db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.default_model_profile_id.in_(losing),
        )
    )
    if workspace is None:
        return None
    successor = await db.scalar(
        select(ModelProfile)
        .where(ModelProfile.workspace_id == workspace_id, ModelProfile.id.not_in(losing))
        .order_by(ModelProfile.created_at)
        .limit(1)
    )
    if successor is None:
        # Nothing to fall back on: name the agents that would quietly break.
        stranded = list(
            await db.scalars(
                select(Agent.name)
                .where(Agent.workspace_id == workspace_id, Agent.model_profile_id.is_(None))
                .order_by(Agent.name)
                .limit(5)
            )
        )
        if stranded:
            listed = ", ".join(stranded)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Deleting this {subject} would leave the workspace with no model to "
                    f"fall back on, and agents still run on the workspace default "
                    f"({listed}). Add another model first, then delete this {subject}."
                ),
            )
    workspace.default_model_profile_id = successor.id if successor is not None else None
    return successor


async def delete_provider(
    db: AsyncSession,
    ctx: WorkspaceContext,
    provider_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    provider = await get_provider(db, ctx.workspace_id, provider_id)
    profile_ids = list(
        await db.scalars(select(ModelProfile.id).where(ModelProfile.provider_id == provider.id))
    )
    promoted: ModelProfile | None = None
    if profile_ids:
        # Agents pinned to one of this provider's profiles keep it alive; name
        # them so the admin knows what to change. Profiles cascade away with
        # the provider, so the workspace default has to move with them.
        agent_names = list(
            await db.scalars(
                select(Agent.name)
                .where(
                    Agent.workspace_id == ctx.workspace_id,
                    Agent.model_profile_id.in_(profile_ids),
                )
                .order_by(Agent.name)
                .limit(5)
            )
        )
        if agent_names:
            listed = ", ".join(agent_names)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Agents still use this provider's models ({listed}). "
                    "Change their model first, then delete the provider."
                ),
            )
        promoted = await _hand_over_workspace_default(
            db, ctx.workspace_id, losing=profile_ids, subject="provider"
        )
    audit.record(
        db,
        action="provider.deleted",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "display_name": provider.display_name,
            "type": provider.type,
            "promoted_default": promoted.display_name if promoted is not None else None,
        },
    )
    await db.delete(provider)
    await db.commit()


async def verify_draft(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    provider_type: str,
    base_url: str | None,
    api_key: str | None,
    secret_id: UUID | None,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> tuple[bool, str]:
    """Live credential check for a provider that has not been saved yet.

    The key travels only through this request; nothing is written. When
    ``secret_id`` is given instead, the stored secret is used (workspace
    scoped, 422 when unknown).
    """
    key = api_key
    if key is not None:
        # An inline key never passes through SecretStore, so nothing has
        # registered it with the redactor. Register it now: provider errors
        # below can quote up to 500 characters of the upstream body, and some
        # providers echo part of the key back on an auth failure.
        register_secret_material(key)
    if key is None and secret_id is not None:
        await _validate_secret(db, ctx.workspace_id, secret_id)
        key = await SecretStore(db, crypto).reveal(ctx.workspace_id, secret_id)
    try:
        client = _build_verification_client(provider_type, base_url, key, metrics, tracer)
    except ProviderConfigError as exc:
        return False, redact_text(str(exc))
    try:
        return True, await client.verify()
    except ModelProviderError as exc:
        return False, redact_text(str(exc))
    finally:
        await client.close()


async def list_provider_models(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> tuple[list[ModelListing], str | None]:
    """Models (with prices when known) from the provider, or an explanation.

    Read-only: nothing is stored on the provider row. A provider that cannot
    list models (or rejects the credentials) yields an empty list plus a
    redacted detail so the UI can fall back to free-text entry.
    """
    provider = await get_provider(db, ctx.workspace_id, provider_id)
    try:
        client = await provider_client(db, crypto, ctx.workspace_id, provider, metrics, tracer)
    except ProviderConfigError as exc:
        return [], str(exc)
    try:
        return await client.list_models_detailed(), None
    except ModelProviderError as exc:
        return [], redact_text(str(exc))
    finally:
        await client.close()


# --- Balance and spend ---


@dataclass(frozen=True)
class _CachedAccountStatus:
    fetched_at: float
    status: AccountStatus | None
    error: str | None


# provider id -> last live billing lookup (success or failure), see TTL above.
_ACCOUNT_STATUS_CACHE: dict[UUID, _CachedAccountStatus] = {}


def _clear_account_status_cache() -> None:
    _ACCOUNT_STATUS_CACHE.clear()


def month_start_utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _tracked_spend(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    since: datetime,
    provider_id: UUID | None = None,
    profile_gone: bool = False,
) -> tuple[int, int]:
    """(this period, all time) sum of ``agent_run.estimated_cost_micros``.

    ``provider_id`` narrows to runs on that provider's profiles. ``profile_gone``
    narrows to runs whose profile no longer exists: deleting a model clears
    ``agent_run.model_profile_id`` (FK ON DELETE SET NULL) but the money was
    still spent, so that cost has to land somewhere the breakdown can show.
    """
    base = (
        select(
            func.coalesce(
                func.sum(AgentRun.estimated_cost_micros).filter(AgentRun.created_at >= since), 0
            ),
            func.coalesce(func.sum(AgentRun.estimated_cost_micros), 0),
        )
        .select_from(AgentRun)
        .where(AgentRun.workspace_id == workspace_id)
    )
    if provider_id is not None:
        base = base.join(ModelProfile, ModelProfile.id == AgentRun.model_profile_id).where(
            ModelProfile.provider_id == provider_id
        )
    elif profile_gone:
        # Correlated EXISTS rather than NOT IN: a NULL profile id matches no
        # row, which is exactly the case being counted, and NOT IN would go
        # NULL-blind on it.
        base = base.where(
            ~select(ModelProfile.id).where(ModelProfile.id == AgentRun.model_profile_id).exists()
        )
    row = (await db.execute(base)).one()
    return int(row[0] or 0), int(row[1] or 0)


async def _live_account_status(
    db: AsyncSession,
    crypto: SecretCrypto,
    workspace_id: UUID,
    provider: ModelProvider,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> _CachedAccountStatus:
    """Provider billing lookup, memoised per provider for the cache TTL."""
    cached = _ACCOUNT_STATUS_CACHE.get(provider.id)
    now = time.monotonic()
    if cached is not None and now - cached.fetched_at < ACCOUNT_STATUS_CACHE_TTL_SECONDS:
        return cached
    status_value: AccountStatus | None = None
    error: str | None = None
    try:
        client = await provider_client(
            db, crypto, workspace_id, provider, metrics, tracer, with_admin_key=True
        )
    except ProviderConfigError as exc:
        error = str(exc)
    else:
        try:
            status_value = await asyncio.wait_for(
                client.get_account_status(), timeout=ACCOUNT_STATUS_TIMEOUT_SECONDS
            )
        except AccountStatusUnsupported as exc:
            error = str(exc)
        except TimeoutError:
            error = "The provider's billing API did not answer in time"
        except ModelProviderError as exc:
            error = redact_text(str(exc))
        finally:
            await client.close()
    result = _CachedAccountStatus(fetched_at=now, status=status_value, error=error)
    _ACCOUNT_STATUS_CACHE[provider.id] = result
    return result


@dataclass(frozen=True)
class ProviderBalance:
    tracked_spent_month_micros: int
    tracked_spent_total_micros: int
    provider_spent_month_micros: int | None
    provider_remaining_micros: int | None
    credits_loaded_micros: int | None
    estimated_remaining_micros: int | None
    source: Literal["openrouter", "openai_admin", "tracked"]
    detail: str | None
    fetched_at: datetime


def estimate_remaining(
    *,
    credits_loaded_micros: int | None,
    provider_spent_month_micros: int | None,
    tracked_spent_total_micros: int,
) -> int | None:
    """``credits - provider month spend`` when both known, else ``credits -
    tracked total`` when credits are set, else unknown."""
    if credits_loaded_micros is None:
        return None
    if provider_spent_month_micros is not None:
        return credits_loaded_micros - provider_spent_month_micros
    return credits_loaded_micros - tracked_spent_total_micros


async def get_provider_balance(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> ProviderBalance:
    """Tracked spend plus a best-effort live balance/spend from the provider."""
    provider = await get_provider(db, ctx.workspace_id, provider_id)
    now = datetime.now(UTC)
    month, total = await _tracked_spend(
        db, ctx.workspace_id, since=month_start_utc(now), provider_id=provider.id
    )
    live = await _live_account_status(db, crypto, ctx.workspace_id, provider, metrics, tracer)

    source: Literal["openrouter", "openai_admin", "tracked"] = "tracked"
    detail: str | None = live.error
    provider_spent: int | None = None
    provider_remaining: int | None = None
    if live.status is not None:
        provider_spent = live.status.spent_month_micros
        provider_remaining = live.status.remaining_micros
        if live.status.source in ("openrouter", "openai_admin"):
            source = live.status.source  # type: ignore[assignment]
        detail = live.status.detail or None
    elif detail is None:
        detail = "Tracked by Jhin"
    return ProviderBalance(
        tracked_spent_month_micros=month,
        tracked_spent_total_micros=total,
        provider_spent_month_micros=provider_spent,
        provider_remaining_micros=provider_remaining,
        credits_loaded_micros=provider.credits_loaded_micros,
        estimated_remaining_micros=estimate_remaining(
            credits_loaded_micros=provider.credits_loaded_micros,
            provider_spent_month_micros=provider_spent,
            tracked_spent_total_micros=total,
        ),
        source=source,
        detail=detail,
        fetched_at=now,
    )


@dataclass(frozen=True)
class ProviderSpend:
    provider: ModelProvider
    spent_month_micros: int
    spent_total_micros: int


@dataclass(frozen=True)
class WorkspaceSpend:
    spent_month_micros: int
    spent_total_micros: int
    period_start: datetime
    providers: list[ProviderSpend]
    # Cost from models that have since been deleted. Their runs keep the cost
    # and lose the profile, so without this bucket the per-provider breakdown
    # quietly stops adding up to the total above.
    deleted_model_month_micros: int
    deleted_model_total_micros: int
    monthly_budget_micros: int | None
    warning_threshold: float
    fetched_at: datetime


def budget_from_settings(settings_json: dict[str, Any]) -> tuple[int | None, float]:
    """``(monthly_budget_micros, warning_threshold)`` from ``settings_json.budget``."""
    raw = settings_json.get("budget")
    if not isinstance(raw, dict):
        return None, 0.8
    budget = raw.get("monthly_budget_micros")
    threshold = raw.get("warning_threshold", 0.8)
    budget_micros = int(budget) if isinstance(budget, int | float) and budget >= 0 else None
    warning = float(threshold) if isinstance(threshold, int | float) else 0.8
    return budget_micros, min(max(warning, 0.0), 1.0)


async def get_workspace_spend(db: AsyncSession, workspace_id: UUID) -> WorkspaceSpend:
    """Tracked spend this month / all time, per provider, plus the budget.

    Every profile belongs to a provider and dies with it, so the per-provider
    sums plus the deleted-model bucket always add back up to the totals.
    """
    now = datetime.now(UTC)
    since = month_start_utc(now)
    month, total = await _tracked_spend(db, workspace_id, since=since)
    providers: list[ProviderSpend] = []
    for provider in await list_providers(db, workspace_id):
        p_month, p_total = await _tracked_spend(
            db, workspace_id, since=since, provider_id=provider.id
        )
        providers.append(
            ProviderSpend(provider=provider, spent_month_micros=p_month, spent_total_micros=p_total)
        )
    gone_month, gone_total = await _tracked_spend(db, workspace_id, since=since, profile_gone=True)
    workspace = await db.get(Workspace, workspace_id)
    budget, threshold = budget_from_settings(workspace.settings_json if workspace else {})
    return WorkspaceSpend(
        spent_month_micros=month,
        spent_total_micros=total,
        period_start=since,
        providers=providers,
        deleted_model_month_micros=gone_month,
        deleted_model_total_micros=gone_total,
        monthly_budget_micros=budget,
        warning_threshold=threshold,
        fetched_at=now,
    )


async def verify_provider(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    request_id: UUID,
    ip_hash: str,
) -> tuple[bool, str]:
    """One cheap live call through the adapter; result stored on the row."""
    provider = await get_provider(db, ctx.workspace_id, provider_id)

    ok, detail = True, ""
    try:
        client = await provider_client(db, crypto, ctx.workspace_id, provider, metrics, tracer)
    except ProviderConfigError as exc:
        ok, detail = False, str(exc)
    else:
        try:
            detail = await client.verify()
        except ModelProviderError as exc:
            ok, detail = False, redact_text(str(exc))
        finally:
            await client.close()

    now = datetime.now(UTC)
    provider.last_verified_at = now if ok else provider.last_verified_at
    provider.last_error = None if ok else detail
    audit.record(
        db,
        action="provider.verified" if ok else "provider.verify_failed",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"display_name": provider.display_name, "ok": ok},
    )
    await db.commit()
    return ok, detail


# --- Profiles ---


async def list_profiles(db: AsyncSession, workspace_id: UUID) -> list[ModelProfile]:
    rows = await db.scalars(
        select(ModelProfile)
        .where(ModelProfile.workspace_id == workspace_id)
        .order_by(ModelProfile.created_at)
    )
    return list(rows)


async def get_profile(db: AsyncSession, workspace_id: UUID, profile_id: UUID) -> ModelProfile:
    profile = await db.scalar(
        select(ModelProfile).where(
            ModelProfile.id == profile_id, ModelProfile.workspace_id == workspace_id
        )
    )
    if profile is None:
        raise _profile_not_found()
    return profile


PRICE_FIELDS = ("input_cost_micros_per_million", "output_cost_micros_per_million")


def _carries_price(values: dict[str, Any]) -> bool:
    """Whether this payload names a price field at all.

    ``create`` always dumps every field, so this is true there; ``update``
    dumps only what the client sent. Either way, *naming* the field is the
    deliberate act — clearing a price is as much a decision as setting one.
    """
    return any(field in values for field in PRICE_FIELDS)


def _sets_a_real_price(values: dict[str, Any]) -> bool:
    """Whether an actual number is being stored (not just cleared to null)."""
    return any(values.get(field) is not None for field in PRICE_FIELDS)


def _require_web_search_support(
    provider: ModelProvider, model_name: str, config_json: dict[str, Any] | None
) -> None:
    """Reject ``config_json.web_search`` when the provider/model cannot honor
    it (docs/architecture/web.md path 2) — the flag must fail at save time,
    never silently at run time."""
    config = WebSearchConfig.from_profile_config(config_json or {})
    if not config.enabled:
        return
    reason = web_search_unsupported_reason(provider.type, model_name)
    if reason is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"config_json.web_search cannot be enabled: {reason}",
        )


def _require_reasoning_support(
    provider: ModelProvider,
    model_name: str,
    config_json: dict[str, Any] | None,
    supports_reasoning: bool,
) -> None:
    """Reject ``config_json.reasoning`` when the provider/model cannot honor
    it (docs/architecture/models.md) — a pinned effort the provider would 400
    on must fail at save time, not mid-run.

    The tools-vs-effort conflict is deliberately *not* rejected here: whether
    an agent advertises tools is a per-agent grant, not a profile fact, so a
    profile pinning ``effort: "high"`` is legitimate for a tools-free agent.
    That conflict is caught pre-flight in the adapter instead, with a named,
    actionable message.
    """
    config = ReasoningConfig.from_profile_config(config_json or {})
    if config.effort is None:
        return
    reason = reasoning_unsupported_reason(
        provider.type,
        model_name,
        supports_reasoning=supports_reasoning or config.supports_reasoning,
    )
    if reason is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"config_json.reasoning cannot be set: {reason}",
        )


async def create_profile(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> ModelProfile:
    provider = await get_provider(db, ctx.workspace_id, values["provider_id"])  # workspace scope
    _require_web_search_support(
        provider, str(values.get("model_name", "")), values.get("config_json")
    )
    _require_reasoning_support(
        provider,
        str(values.get("model_name", "")),
        values.get("config_json"),
        bool(values.get("supports_reasoning", False)),
    )
    profile = ModelProfile(workspace_id=ctx.workspace_id, **values)
    # A price arriving through the public endpoint is, by definition, one a
    # human entered — nothing automatic ever writes through here. Stamping it
    # is what later lets the automatic sources know to leave it alone
    # (jhin_api.models.pricing_service.may_write_price). A profile created
    # *without* prices claims no provenance: it is simply unpriced, and any
    # source may fill it.
    if _sets_a_real_price(values):
        profile.price_source = "user"
    db.add(profile)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A profile with this name already exists in the workspace",
        ) from exc
    # A workspace with no default cannot run an agent at all ("no model
    # profile and the workspace has no default"), so the first profile to
    # exist becomes the default. Only ever fills an empty slot — a later
    # profile never displaces a default someone chose.
    workspace = await db.get(Workspace, ctx.workspace_id)
    adopted_default = workspace is not None and workspace.default_model_profile_id is None
    if adopted_default and workspace is not None:
        workspace.default_model_profile_id = profile.id
    audit.record(
        db,
        action="model_profile.created",
        target_type="model_profile",
        target_id=profile.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "display_name": profile.display_name,
            "model_name": profile.model_name,
            "became_workspace_default": adopted_default,
        },
    )
    await db.commit()
    return profile


async def update_profile(
    db: AsyncSession,
    ctx: WorkspaceContext,
    profile_id: UUID,
    *,
    changes: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
    price_source: str | None = "user",
) -> ModelProfile:
    """Apply ``changes`` to a profile.

    ``price_source`` stamps the provenance of any price in ``changes`` and
    defaults to ``"user"`` because the only caller that reaches here without
    a human behind it is Jhin's own price refresh, which passes its actual
    source. Getting this backwards would let an automatic refresh disguise
    itself as a contract price and become un-overwritable.
    """
    profile = await get_profile(db, ctx.workspace_id, profile_id)
    provider = await get_provider(
        db, ctx.workspace_id, changes.get("provider_id", profile.provider_id)
    )
    _require_web_search_support(
        provider,
        str(changes.get("model_name", profile.model_name)),
        changes.get("config_json", profile.config_json),
    )
    _require_reasoning_support(
        provider,
        str(changes.get("model_name", profile.model_name)),
        changes.get("config_json", profile.config_json),
        bool(changes.get("supports_reasoning", profile.supports_reasoning)),
    )
    for field, value in changes.items():
        setattr(profile, field, value)
    if _carries_price(changes):
        # Clearing a price clears its provenance too, so the row does not keep
        # claiming a source for a number that is no longer there.
        profile.price_source = price_source if _sets_a_real_price(changes) else None
    audit.record(
        db,
        action="model_profile.updated",
        target_type="model_profile",
        target_id=profile.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed_fields": sorted(changes)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A profile with this name already exists in the workspace",
        ) from exc
    return profile


async def delete_profile(
    db: AsyncSession,
    ctx: WorkspaceContext,
    profile_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    profile = await get_profile(db, ctx.workspace_id, profile_id)
    in_use_by_agent = await db.scalar(
        select(Agent.id)
        .where(Agent.workspace_id == ctx.workspace_id, Agent.model_profile_id == profile.id)
        .limit(1)
    )
    if in_use_by_agent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Profile is in use by agents; change their model first",
        )
    # Pinning is only half the story: an agent with no model of its own runs on
    # the workspace default, so deleting the default hands the slot to another
    # profile (or refuses, when there is none left and agents need it).
    promoted = await _hand_over_workspace_default(
        db, ctx.workspace_id, losing=[profile.id], subject="model"
    )
    audit.record(
        db,
        action="model_profile.deleted",
        target_type="model_profile",
        target_id=profile.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "display_name": profile.display_name,
            "promoted_default": promoted.display_name if promoted is not None else None,
        },
    )
    await db.delete(profile)
    await db.commit()


async def refresh_profile_pricing(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    profile_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    request_id: UUID,
    ip_hash: str,
    force: bool = False,
) -> tuple[ModelProfile, bool, PriceSource | None, str]:
    """Re-look up one profile's price across every source and store the best.

    Consults, in the order :mod:`jhin_models.pricing` defines: a rate measured
    from real spend, the provider's own live model list, the refreshed
    community catalog, then the built-in list prices. A price an admin typed
    is left alone and reported as such unless ``force`` is set, which is how
    the UI offers "update it anyway" with the difference shown first.

    Returns ``(profile, updated, source, detail)``.
    """
    from jhin_api.models import pricing_service

    profile = await get_profile(db, ctx.workspace_id, profile_id)
    provider = await get_provider(db, ctx.workspace_id, profile.provider_id)

    live: ModelListing | None = None
    lookup_detail: str | None = None
    try:
        client = await provider_client(db, crypto, ctx.workspace_id, provider, metrics, tracer)
    except ProviderConfigError as exc:
        lookup_detail = str(exc)
    else:
        try:
            wanted = profile.model_name.strip().lower()
            for entry in await client.list_models_detailed():
                if entry.id.lower() == wanted and entry.source == "provider":
                    live = entry
                    break
        except ModelProviderError as exc:
            lookup_detail = redact_text(str(exc))
        finally:
            await client.close()

    refreshed = await pricing_service.load_refreshed_catalog(db, ctx.workspace_id)
    observed = await pricing_service.observed_for_profile(db, provider, profile)
    candidates = [
        pricing_service.observed_candidate(observed),
        PriceCandidate(
            source="provider",
            input_cost_micros_per_million=live.input_cost_micros_per_million,
            output_cost_micros_per_million=live.output_cost_micros_per_million,
            context_window=live.context_window,
        )
        if live is not None
        else None,
        pricing_service.catalog_candidate(provider.type, profile.model_name, refreshed),
    ]
    winner = resolve_price([c for c in candidates if c is not None])
    if winner is None:
        detail = "No price is known for this model on any source"
        if lookup_detail:
            detail = f"{detail} ({lookup_detail})"
        return profile, False, None, detail

    snapshot = await pricing_service.load_catalog_snapshot(db, ctx.workspace_id)
    label = describe_price_source(
        winner.source, refreshed_at=snapshot.fetched_at.date() if snapshot else None
    )
    if not force and not pricing_service.may_write_price(profile, winner.source):
        theirs = _price_pair(
            profile.input_cost_micros_per_million, profile.output_cost_micros_per_million
        )
        offered = _price_pair(
            winner.input_cost_micros_per_million, winner.output_cost_micros_per_million
        )
        return (
            profile,
            False,
            winner.source,
            f"Kept your own price ({theirs}). {label} would set {offered} instead — "
            "refresh again with 'update anyway' to take it.",
        )

    changes: dict[str, Any] = {}
    if profile.input_cost_micros_per_million != winner.input_cost_micros_per_million:
        changes["input_cost_micros_per_million"] = winner.input_cost_micros_per_million
    if profile.output_cost_micros_per_million != winner.output_cost_micros_per_million:
        changes["output_cost_micros_per_million"] = winner.output_cost_micros_per_million
    if winner.context_window is not None and profile.context_window != winner.context_window:
        changes["context_window"] = winner.context_window
    if not changes and profile.price_source == winner.source:
        return profile, False, winner.source, f"Prices already match. {label}"
    if not changes:
        profile.price_source = winner.source
        await db.commit()
        return profile, False, winner.source, f"Prices already match. {label}"

    # ``force`` records the human decision: the admin chose this price, so it
    # becomes theirs and no later automatic refresh will move it again.
    stamped: PriceSource = "user" if force and winner.source != "observed" else winner.source
    await update_profile(
        db,
        ctx,
        profile.id,
        changes=changes,
        request_id=request_id,
        ip_hash=ip_hash,
        price_source=stamped,
    )
    return profile, True, winner.source, f"Prices updated. {label}"


def _price_pair(input_micros: int | None, output_micros: int | None) -> str:
    def one(value: int | None) -> str:
        return "unset" if value is None else f"${value / 1_000_000:.2f}"

    return f"{one(input_micros)} in / {one(output_micros)} out per 1M tokens"
