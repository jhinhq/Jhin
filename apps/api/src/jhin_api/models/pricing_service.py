"""Where a profile's price comes from, and the two actions that improve it.

:mod:`jhin_models.pricing` owns the precedence *rule*; this module is the one
place that enforces it against the database:

    user-entered > measured from spend > live from the provider
                 > refreshed catalog > built-in catalog > unknown

Two admin actions feed the lower layers.

**Reconcile pricing** asks OpenAI's Admin API what it actually billed,
per model, over a window of completed days, and divides that by token counts
— the provider's own where it reports them, otherwise Jhin's — to measure the
workspace's real effective rate (:mod:`jhin_models.observed_pricing`). This
is the only way to learn a negotiated contract price: OpenAI and Anthropic
publish no pricing endpoint at all.

**Refresh the price catalog** pulls LiteLLM's community price map through the
shared public-URL policy, trims it to the providers Jhin speaks, and stores
it as a catalog layer that sits *beneath* anything measured or typed but
above the static catalog compiled into this release.

Both write through :func:`apply_best_prices`, which will never overwrite a
price whose provenance it cannot vouch for. A profile whose ``price_source``
is ``NULL`` (posted straight to the API, or predating the column) is treated
as user-entered, because silently replacing a real contract price is a worse
failure than leaving a stale one in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from opentelemetry.trace import Tracer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.models import service
from jhin_connectors.endpoints import EndpointPolicyError, validate_public_http_url
from jhin_connectors.http_client import ProviderHTTPError, send_bounded_json
from jhin_db.models import (
    AgentRun,
    ModelObservedPrice,
    ModelProfile,
    ModelProvider,
    PriceCatalogSnapshot,
)
from jhin_models import AccountStatusUnsupported, ModelProviderError
from jhin_models.factory import ProviderConfigError
from jhin_models.observed_pricing import (
    DerivedRate,
    ModelTokenTotals,
    SkippedModel,
    collect_observations,
    derive_rates,
)
from jhin_models.pricing import (
    CATALOG_UPDATED,
    LITELLM_ATTRIBUTION,
    LITELLM_CATALOG_SOURCE,
    LITELLM_MAX_BYTES,
    LITELLM_PRICE_MAP_URL,
    LITELLM_PROJECT_URL,
    PRICE_SOURCE_PRECEDENCE,
    PRICING_PAGES,
    ModelPrice,
    PriceCandidate,
    PriceSource,
    RefreshedCatalog,
    catalog_is_stale,
    describe_price_source,
    lookup_price,
    lookup_refreshed_price,
    normalize_model_id,
    parse_litellm_price_map,
    refreshed_catalog_from_json,
    refreshed_catalog_to_json,
    resolve_price,
)
from jhin_observability import JhinMetrics
from jhin_secrets import SecretCrypto
from jhin_secrets.redaction import redact_text

# How far back a reconciliation looks. Long enough for a small workspace to
# accumulate a usable sample, short enough that a contract change from last
# quarter does not keep dragging the measured rate backwards.
RECONCILE_WINDOW_DAYS = 30
# The community map is ~1.8 MB; a slow mirror must not hold a request open.
CATALOG_FETCH_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)


# --- Period selection ---


def reconcile_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    """``[start, end)`` over whole *completed* UTC days.

    The current day is deliberately excluded from both halves. Cost buckets
    for today are still filling while Jhin's token counts for today are
    already complete, so including it would divide a partial bill by a full
    day of tokens and understate every rate.
    """
    current = now or datetime.now(UTC)
    end = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=RECONCILE_WINDOW_DAYS), end


# --- The refreshed catalog layer ---


async def load_catalog_snapshot(
    db: AsyncSession, workspace_id: UUID, source: str = LITELLM_CATALOG_SOURCE
) -> PriceCatalogSnapshot | None:
    row: PriceCatalogSnapshot | None = await db.scalar(
        select(PriceCatalogSnapshot).where(
            PriceCatalogSnapshot.workspace_id == workspace_id,
            PriceCatalogSnapshot.source == source,
        )
    )
    return row


async def load_refreshed_catalog(db: AsyncSession, workspace_id: UUID) -> RefreshedCatalog:
    """The stored community catalog, or an empty one when never refreshed."""
    snapshot = await load_catalog_snapshot(db, workspace_id)
    if snapshot is None:
        return {}
    return refreshed_catalog_from_json(snapshot.entries_json)


@dataclass(frozen=True)
class CatalogRefreshResult:
    updated: bool
    entry_count: int
    fetched_at: datetime | None
    source: str
    source_url: str
    # MIT requires the copyright and permission notice to travel with any
    # substantial portion of the material, and caching the map is
    # redistribution — so the notice rides along with every response and row.
    attribution: str
    detail: str
    repriced: list[AppliedPrice]


async def _fetch_price_map(url: str) -> object:
    """The community price map, through the shared public-URL policy."""
    safe_url = validate_public_http_url(url, kind="Price catalog URL")
    async with httpx.AsyncClient(timeout=CATALOG_FETCH_TIMEOUT) as client:
        request = client.build_request("GET", safe_url, headers={"accept": "application/json"})
        return await send_bounded_json(client, request, max_response_bytes=LITELLM_MAX_BYTES)


async def refresh_price_catalog(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    request_id: UUID,
    ip_hash: str,
    url: str = LITELLM_PRICE_MAP_URL,
) -> CatalogRefreshResult:
    """Fetch, trim, and store the LiteLLM price map, then reprice profiles.

    A failed refresh is reported, never fatal: the previous snapshot (or the
    built-in catalog) keeps serving prices, because losing pricing entirely
    is worse than pricing from a slightly older map.
    """
    existing = await load_catalog_snapshot(db, ctx.workspace_id)
    try:
        payload = await _fetch_price_map(url)
    except EndpointPolicyError as exc:
        return _refresh_failed(existing, url, str(exc))
    except ProviderHTTPError as exc:
        return _refresh_failed(existing, url, str(exc))
    except httpx.HTTPError as exc:
        return _refresh_failed(existing, url, f"network error: {type(exc).__name__}")

    catalog = parse_litellm_price_map(payload)
    entry_count = sum(len(entries) for entries in catalog.values())
    if entry_count == 0:
        return _refresh_failed(
            existing, url, "the catalog fetched successfully but contained no usable prices"
        )

    now = datetime.now(UTC)
    snapshot = existing or PriceCatalogSnapshot(
        workspace_id=ctx.workspace_id, source=LITELLM_CATALOG_SOURCE
    )
    snapshot.source_url = url
    snapshot.attribution = LITELLM_ATTRIBUTION
    snapshot.fetched_at = now
    snapshot.entry_count = entry_count
    snapshot.entries_json = refreshed_catalog_to_json(catalog)
    if existing is None:
        db.add(snapshot)
    audit.record(
        db,
        action="price_catalog.refreshed",
        target_type="workspace",
        target_id=ctx.workspace_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"source": LITELLM_CATALOG_SOURCE, "entry_count": entry_count},
    )
    repriced = await apply_best_prices(db, ctx.workspace_id, refreshed=catalog)
    await db.commit()
    return CatalogRefreshResult(
        updated=True,
        entry_count=entry_count,
        fetched_at=now,
        source=LITELLM_CATALOG_SOURCE,
        source_url=url,
        attribution=LITELLM_ATTRIBUTION,
        detail=(
            f"Refreshed {entry_count} community-maintained prices from LiteLLM "
            f"({LITELLM_ATTRIBUTION})"
            + (f"; repriced {len(repriced)} profile(s)" if repriced else "")
        ),
        repriced=repriced,
    )


def _refresh_failed(
    existing: PriceCatalogSnapshot | None, url: str, reason: str
) -> CatalogRefreshResult:
    fallback = (
        f"still using the catalog refreshed on {existing.fetched_at.date().isoformat()}"
        if existing is not None
        else f"still using the built-in price list (catalog {CATALOG_UPDATED})"
    )
    return CatalogRefreshResult(
        updated=False,
        entry_count=existing.entry_count if existing is not None else 0,
        fetched_at=existing.fetched_at if existing is not None else None,
        source=LITELLM_CATALOG_SOURCE,
        source_url=url,
        attribution=LITELLM_ATTRIBUTION,
        detail=f"Could not refresh the price catalog ({redact_text(reason)}) — {fallback}",
        repriced=[],
    )


# --- Central precedence enforcement ---


@dataclass(frozen=True)
class AppliedPrice:
    """One profile's price changing, with what it was and where it came from."""

    profile_id: UUID
    display_name: str
    model_name: str
    from_input_micros_per_million: int | None
    from_output_micros_per_million: int | None
    from_source: str | None
    to_input_micros_per_million: int
    to_output_micros_per_million: int
    to_source: PriceSource
    detail: str


def _is_priced(profile: ModelProfile) -> bool:
    return (
        profile.input_cost_micros_per_million is not None
        and profile.output_cost_micros_per_million is not None
    )


def may_write_price(profile: ModelProfile, new_source: PriceSource) -> bool:
    """Whether an automatic source may write over this profile's price.

    Three rules, in order:

    1. A profile with no price has nothing to protect — anything may fill it.
    2. A price an admin typed, or one whose provenance is unknown, is never
       overwritten automatically. Unknown counts as typed: this is the safe
       direction to be wrong in.
    3. Otherwise the new source must rank at least as high as the one that
       wrote the current value, so a stale catalog can never displace a
       measured rate.
    """
    if not _is_priced(profile):
        return True
    current = profile.price_source
    if current is None or current == "user":
        return False
    if current not in PRICE_SOURCE_PRECEDENCE:
        return False
    return PRICE_SOURCE_PRECEDENCE.index(new_source) <= PRICE_SOURCE_PRECEDENCE.index(current)


def observed_candidate(row: ModelObservedPrice | None) -> PriceCandidate | None:
    """A measured rate as a candidate, when it has a usable input/output pair.

    A blended-only measurement is deliberately *not* offered here: it has no
    split to put in the profile's two columns, and inventing one would be the
    confident guess this whole feature exists to avoid. It is still shown in
    the UI so an admin can act on it.
    """
    if row is None or row.confidence == "low":
        return None
    if row.input_cost_micros_per_million is None or row.output_cost_micros_per_million is None:
        return None
    return PriceCandidate(
        source="observed",
        input_cost_micros_per_million=row.input_cost_micros_per_million,
        output_cost_micros_per_million=row.output_cost_micros_per_million,
        detail=row.note,
    )


async def observed_for_profile(
    db: AsyncSession, provider: ModelProvider, profile: ModelProfile
) -> ModelObservedPrice | None:
    """The measured rate for one profile's model on its provider, if any."""
    row: ModelObservedPrice | None = await db.scalar(
        select(ModelObservedPrice).where(
            ModelObservedPrice.provider_id == provider.id,
            ModelObservedPrice.model_key == normalize_model_id(profile.model_name),
        )
    )
    return row


def catalog_candidate(
    provider_type: str, model_name: str, refreshed: RefreshedCatalog
) -> PriceCandidate | None:
    """The best *catalog* price: the refreshed community map, else built-in."""
    return _price_candidate(
        "refreshed_catalog", lookup_refreshed_price(refreshed, provider_type, model_name)
    ) or _price_candidate("catalog", lookup_price(provider_type, model_name))


def _price_candidate(source: PriceSource, price: ModelPrice | None) -> PriceCandidate | None:
    if price is None:
        return None
    return PriceCandidate(
        source=source,
        input_cost_micros_per_million=price.input_cost_micros_per_million,
        output_cost_micros_per_million=price.output_cost_micros_per_million,
        context_window=price.context_window,
    )


def best_candidate(
    *,
    provider_type: str,
    model_name: str,
    observed: ModelObservedPrice | None,
    refreshed: RefreshedCatalog,
) -> PriceCandidate | None:
    """The best automatic price for a model, precedence applied.

    Live provider prices are not consulted here — those need a network call
    per provider and are handled by ``refresh_profile_pricing``. The layers
    below them are all local, which is what makes repricing cheap enough to
    run after every reconciliation and catalog refresh.
    """
    candidates = [
        observed_candidate(observed),
        _price_candidate(
            "refreshed_catalog", lookup_refreshed_price(refreshed, provider_type, model_name)
        ),
        _price_candidate("catalog", lookup_price(provider_type, model_name)),
    ]
    return resolve_price([c for c in candidates if c is not None])


async def _observed_by_key(
    db: AsyncSession, workspace_id: UUID
) -> dict[tuple[UUID, str], ModelObservedPrice]:
    rows = await db.scalars(
        select(ModelObservedPrice).where(ModelObservedPrice.workspace_id == workspace_id)
    )
    return {(row.provider_id, row.model_key): row for row in rows}


async def apply_best_prices(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    refreshed: RefreshedCatalog | None = None,
    provider_id: UUID | None = None,
) -> list[AppliedPrice]:
    """Write the best available automatic price onto every eligible profile.

    Stages the changes in the caller's transaction; the caller commits.
    Profiles the precedence rule protects are left exactly as they are.
    """
    catalog = refreshed if refreshed is not None else await load_refreshed_catalog(db, workspace_id)
    observed = await _observed_by_key(db, workspace_id)
    providers = {
        row.id: row
        for row in await db.scalars(
            select(ModelProvider).where(ModelProvider.workspace_id == workspace_id)
        )
    }
    query = select(ModelProfile).where(ModelProfile.workspace_id == workspace_id)
    if provider_id is not None:
        query = query.where(ModelProfile.provider_id == provider_id)

    applied: list[AppliedPrice] = []
    for profile in await db.scalars(query):
        provider = providers.get(profile.provider_id)
        if provider is None:
            continue
        key = normalize_model_id(profile.model_name)
        candidate = best_candidate(
            provider_type=provider.type,
            model_name=profile.model_name,
            observed=observed.get((provider.id, key)),
            refreshed=catalog,
        )
        if candidate is None or not candidate.is_usable:
            continue
        if not may_write_price(profile, candidate.source):
            continue
        new_input = candidate.input_cost_micros_per_million
        new_output = candidate.output_cost_micros_per_million
        assert new_input is not None and new_output is not None  # is_usable
        unchanged = (
            profile.input_cost_micros_per_million == new_input
            and profile.output_cost_micros_per_million == new_output
            and profile.price_source == candidate.source
        )
        if unchanged:
            continue
        applied.append(
            AppliedPrice(
                profile_id=profile.id,
                display_name=profile.display_name,
                model_name=profile.model_name,
                from_input_micros_per_million=profile.input_cost_micros_per_million,
                from_output_micros_per_million=profile.output_cost_micros_per_million,
                from_source=profile.price_source,
                to_input_micros_per_million=new_input,
                to_output_micros_per_million=new_output,
                to_source=candidate.source,
                detail=candidate.detail or describe_price_source(candidate.source),
            )
        )
        profile.input_cost_micros_per_million = new_input
        profile.output_cost_micros_per_million = new_output
        profile.price_source = candidate.source
        if profile.context_window is None and candidate.context_window:
            profile.context_window = candidate.context_window
    return applied


# --- Reconciliation: measuring the real rate from real spend ---


@dataclass(frozen=True)
class ProviderReconcile:
    provider_id: UUID
    display_name: str
    provider_type: str
    derived: list[DerivedRate]
    skipped: list[SkippedModel]
    applied: list[AppliedPrice]
    period_start: datetime
    period_end: datetime
    billed_micros: int
    unattributed_micros: int
    unattributed_labels: list[str]
    detail: str


@dataclass(frozen=True)
class SkippedProvider:
    provider_id: UUID
    display_name: str
    reason: str


@dataclass(frozen=True)
class ReconcileResult:
    providers: list[ProviderReconcile]
    skipped_providers: list[SkippedProvider]
    computed_at: datetime
    detail: str


async def _token_totals(
    db: AsyncSession,
    workspace_id: UUID,
    provider_id: UUID,
    *,
    start: datetime,
    end: datetime,
) -> list[ModelTokenTotals]:
    """Jhin's own tokens per model over the period, keyed the catalog's way."""
    rows = await db.execute(
        select(
            ModelProfile.model_name,
            func.coalesce(func.sum(AgentRun.input_tokens), 0),
            func.coalesce(func.sum(AgentRun.output_tokens), 0),
            func.count(AgentRun.id),
        )
        .select_from(AgentRun)
        .join(ModelProfile, ModelProfile.id == AgentRun.model_profile_id)
        .where(
            AgentRun.workspace_id == workspace_id,
            ModelProfile.provider_id == provider_id,
            AgentRun.created_at >= start,
            AgentRun.created_at < end,
        )
        .group_by(ModelProfile.model_name)
    )
    # Several profiles can name the same model; fold them onto one key so the
    # denominator matches the single line the invoice reports.
    merged: dict[str, ModelTokenTotals] = {}
    for model_name, input_tokens, output_tokens, runs in rows:
        key = normalize_model_id(str(model_name))
        current = merged.get(key)
        merged[key] = ModelTokenTotals(
            model_key=key,
            input_tokens=(current.input_tokens if current else 0) + int(input_tokens or 0),
            output_tokens=(current.output_tokens if current else 0) + int(output_tokens or 0),
            runs=(current.runs if current else 0) + int(runs or 0),
        )
    return list(merged.values())


async def _persist_rates(
    db: AsyncSession,
    workspace_id: UUID,
    provider_id: UUID,
    rates: list[DerivedRate],
    *,
    period_start: datetime,
    period_end: datetime,
    computed_at: datetime,
) -> None:
    existing = {
        row.model_key: row
        for row in await db.scalars(
            select(ModelObservedPrice).where(ModelObservedPrice.provider_id == provider_id)
        )
    }
    for rate in rates:
        row = existing.get(rate.model_key)
        if row is None:
            row = ModelObservedPrice(
                workspace_id=workspace_id, provider_id=provider_id, model_key=rate.model_key
            )
            db.add(row)
        row.input_cost_micros_per_million = rate.input_micros_per_million
        row.output_cost_micros_per_million = rate.output_micros_per_million
        row.blended_cost_micros_per_million = rate.blended_micros_per_million
        row.derivation = rate.derivation
        row.confidence = rate.confidence
        row.note = rate.note
        row.sample_input_tokens = rate.input_tokens
        row.sample_output_tokens = rate.output_tokens
        row.sample_runs = rate.runs
        row.sample_cost_micros = rate.cost_micros
        row.period_start = period_start
        row.period_end = period_end
        row.computed_at = computed_at


def _reference_price(provider_type: str, refreshed: RefreshedCatalog) -> Any:
    """The list price a derivation may use as a ratio and a sanity check.

    The refreshed catalog is preferred over the built-in one: a newer model's
    ratio is only knowable from a catalog that has heard of it.
    """

    def lookup(model_key: str) -> ModelPrice | None:
        return lookup_refreshed_price(refreshed, provider_type, model_key) or lookup_price(
            provider_type, model_key
        )

    return lookup


async def reconcile_pricing(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    request_id: UUID,
    ip_hash: str,
    now: datetime | None = None,
) -> ReconcileResult:
    """Measure real per-token rates from every provider that can report spend.

    Today that means OpenAI providers with an admin key: no other supported
    provider exposes itemised cost. Providers that cannot are skipped with a
    reason rather than silently omitted, so the admin can see what it would
    take to include them.
    """
    period_start, period_end = reconcile_period(now)
    computed_at = datetime.now(UTC)
    refreshed = await load_refreshed_catalog(db, ctx.workspace_id)
    outcomes: list[ProviderReconcile] = []
    skipped_providers: list[SkippedProvider] = []

    for provider in await service.list_providers(db, ctx.workspace_id):
        reason = _cannot_reconcile(provider)
        if reason is not None:
            skipped_providers.append(
                SkippedProvider(
                    provider_id=provider.id, display_name=provider.display_name, reason=reason
                )
            )
            continue
        outcome = await _reconcile_provider(
            db,
            crypto,
            ctx,
            provider,
            metrics,
            tracer,
            refreshed=refreshed,
            period_start=period_start,
            period_end=period_end,
            computed_at=computed_at,
        )
        if isinstance(outcome, SkippedProvider):
            skipped_providers.append(outcome)
        else:
            outcomes.append(outcome)

    applied_total = sum(len(outcome.applied) for outcome in outcomes)
    derived_total = sum(len(outcome.derived) for outcome in outcomes)
    audit.record(
        db,
        action="model_pricing.reconciled",
        target_type="workspace",
        target_id=ctx.workspace_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "providers": len(outcomes),
            "rates_derived": derived_total,
            "profiles_repriced": applied_total,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        },
    )
    await db.commit()

    if not outcomes and skipped_providers:
        detail = "No provider in this workspace can report itemised spend yet"
    elif derived_total == 0:
        detail = (
            "Nothing measurable yet: the providers answered, but no model had enough "
            "matched spend and usage in the period"
        )
    else:
        detail = (
            f"Measured {derived_total} model rate(s) from real spend between "
            f"{period_start.date().isoformat()} and {period_end.date().isoformat()}; "
            f"repriced {applied_total} profile(s)"
        )
    return ReconcileResult(
        providers=outcomes,
        skipped_providers=skipped_providers,
        computed_at=computed_at,
        detail=detail,
    )


def _cannot_reconcile(provider: ModelProvider) -> str | None:
    """Why this provider cannot be measured, or ``None`` when it can."""
    if provider.type != "openai":
        return (
            f"{provider.type} does not report itemised spend through its API, so its rates "
            "cannot be measured — set prices by hand or refresh the price catalog"
        )
    if provider.admin_secret_id is None:
        return (
            "this OpenAI provider has no admin key, and only the organization Admin API "
            "reports what was actually billed — add one to measure real rates"
        )
    if not provider.enabled:
        return "the provider is disabled"
    return None


async def _reconcile_provider(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider: ModelProvider,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    refreshed: RefreshedCatalog,
    period_start: datetime,
    period_end: datetime,
    computed_at: datetime,
) -> ProviderReconcile | SkippedProvider:
    def skip(reason: str) -> SkippedProvider:
        return SkippedProvider(
            provider_id=provider.id, display_name=provider.display_name, reason=reason
        )

    try:
        client = await service.provider_client(
            db, crypto, ctx.workspace_id, provider, metrics, tracer, with_admin_key=True
        )
    except ProviderConfigError as exc:
        return skip(redact_text(str(exc)))

    fetch = getattr(client, "fetch_model_costs", None)
    if fetch is None:
        await client.close()
        return skip("this provider's adapter cannot report itemised spend")
    try:
        report = await fetch(start=period_start.date(), end=period_end.date())
    except AccountStatusUnsupported as exc:
        return skip(redact_text(str(exc)))
    except ModelProviderError as exc:
        return skip(redact_text(str(exc)))
    finally:
        await client.close()

    tokens = await _token_totals(
        db, ctx.workspace_id, provider.id, start=period_start, end=period_end
    )
    period_label = f"{period_start.date().isoformat()} to {period_end.date().isoformat()}"
    derived, skipped = derive_rates(
        costs=collect_observations(report.lines),
        tokens=tokens,
        reference_price=_reference_price(provider.type, refreshed),
        period_label=period_label,
    )
    await _persist_rates(
        db,
        ctx.workspace_id,
        provider.id,
        derived,
        period_start=period_start,
        period_end=period_end,
        computed_at=computed_at,
    )
    await db.flush()
    applied = await apply_best_prices(
        db, ctx.workspace_id, refreshed=refreshed, provider_id=provider.id
    )
    detail = _provider_detail(report, derived, skipped, period_label)
    return ProviderReconcile(
        provider_id=provider.id,
        display_name=provider.display_name,
        provider_type=provider.type,
        derived=derived,
        skipped=skipped,
        applied=applied,
        period_start=period_start,
        period_end=period_end,
        billed_micros=report.total_micros,
        unattributed_micros=report.ignored_micros,
        unattributed_labels=report.ignored_labels,
        detail=detail,
    )


def _provider_detail(
    report: Any, derived: list[DerivedRate], skipped: list[SkippedModel], period_label: str
) -> str:
    if not report.lines:
        return (
            f"The provider reported {_usd(report.total_micros)} of spend for {period_label} but "
            "none of it was itemised per model, so no rate could be measured"
        )
    parts = [
        f"Measured {len(derived)} rate(s) from {_usd(report.total_micros)} billed in {period_label}"
    ]
    if skipped:
        parts.append(f"{len(skipped)} model(s) skipped")
    if report.ignored_micros:
        parts.append(
            f"{_usd(report.ignored_micros)} could not be attributed to a model "
            f"({', '.join(report.ignored_labels[:3]) or 'unlabelled lines'})"
        )
    return "; ".join(parts)


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:.2f}"


# --- Pricing status for the UI ---


@dataclass(frozen=True)
class ObservedRateView:
    model_key: str
    input_cost_micros_per_million: int | None
    output_cost_micros_per_million: int | None
    blended_cost_micros_per_million: int | None
    derivation: str
    confidence: str
    note: str
    sample_runs: int
    sample_input_tokens: int
    sample_output_tokens: int
    computed_at: datetime


@dataclass(frozen=True)
class ProfilePricingView:
    profile_id: UUID
    display_name: str
    model_name: str
    provider_id: UUID
    provider_type: str
    input_cost_micros_per_million: int | None
    output_cost_micros_per_million: int | None
    price_source: str | None
    price_source_label: str
    priced: bool
    pricing_page_url: str | None
    runs_this_month: int
    suggestion: PriceCandidate | None
    suggestion_label: str | None
    observed: ObservedRateView | None


@dataclass(frozen=True)
class UntrackedModel:
    model_name: str
    runs: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class PricingStatus:
    catalog_updated: str
    catalog_stale: bool
    refreshed_source: str | None
    refreshed_fetched_at: datetime | None
    refreshed_entry_count: int
    refreshed_attribution: str | None
    refreshed_project_url: str
    profiles: list[ProfilePricingView]
    untracked: list[UntrackedModel]
    untracked_runs: int
    reconcile_available: bool
    reconcile_detail: str
    pricing_pages: dict[str, str]


async def _runs_by_profile(
    db: AsyncSession, workspace_id: UUID, since: datetime
) -> dict[UUID, tuple[int, int, int]]:
    rows = await db.execute(
        select(
            AgentRun.model_profile_id,
            func.count(AgentRun.id),
            func.coalesce(func.sum(AgentRun.input_tokens), 0),
            func.coalesce(func.sum(AgentRun.output_tokens), 0),
        )
        .where(
            AgentRun.workspace_id == workspace_id,
            AgentRun.created_at >= since,
            AgentRun.model_profile_id.is_not(None),
        )
        .group_by(AgentRun.model_profile_id)
    )
    return {
        row[0]: (int(row[1] or 0), int(row[2] or 0), int(row[3] or 0))
        for row in rows
        if row[0] is not None
    }


async def untracked_models(
    db: AsyncSession, workspace_id: UUID, since: datetime
) -> list[UntrackedModel]:
    """Models that ran this period but carry no price, so cost 0 was recorded.

    This is the honest counterpart to the spend total: without it, a run on an
    unpriced model is indistinguishable from a free one.
    """
    profiles = {
        row.id: row
        for row in await db.scalars(
            select(ModelProfile).where(ModelProfile.workspace_id == workspace_id)
        )
    }
    usage = await _runs_by_profile(db, workspace_id, since)
    merged: dict[str, UntrackedModel] = {}
    for profile_id, (runs, input_tokens, output_tokens) in usage.items():
        profile = profiles.get(profile_id)
        if profile is None or _is_priced(profile):
            continue
        current = merged.get(profile.model_name)
        merged[profile.model_name] = UntrackedModel(
            model_name=profile.model_name,
            runs=(current.runs if current else 0) + runs,
            input_tokens=(current.input_tokens if current else 0) + input_tokens,
            output_tokens=(current.output_tokens if current else 0) + output_tokens,
        )
    return sorted(merged.values(), key=lambda row: (-row.runs, row.model_name))


def _observed_view(row: ModelObservedPrice | None) -> ObservedRateView | None:
    if row is None:
        return None
    return ObservedRateView(
        model_key=row.model_key,
        input_cost_micros_per_million=row.input_cost_micros_per_million,
        output_cost_micros_per_million=row.output_cost_micros_per_million,
        blended_cost_micros_per_million=row.blended_cost_micros_per_million,
        derivation=row.derivation,
        confidence=row.confidence,
        note=row.note,
        sample_runs=row.sample_runs,
        sample_input_tokens=row.sample_input_tokens,
        sample_output_tokens=row.sample_output_tokens,
        computed_at=row.computed_at,
    )


async def pricing_status(
    db: AsyncSession, workspace_id: UUID, *, since: datetime, today: date | None = None
) -> PricingStatus:
    """Everything the Models page needs to talk honestly about prices."""
    snapshot = await load_catalog_snapshot(db, workspace_id)
    refreshed = refreshed_catalog_from_json(snapshot.entries_json) if snapshot else {}
    observed = await _observed_by_key(db, workspace_id)
    providers = {p.id: p for p in await service.list_providers(db, workspace_id)}
    usage = await _runs_by_profile(db, workspace_id, since)

    views: list[ProfilePricingView] = []
    for profile in await service.list_profiles(db, workspace_id):
        provider = providers.get(profile.provider_id)
        provider_type = provider.type if provider else "openai_compatible"
        key = normalize_model_id(profile.model_name)
        observed_row = observed.get((profile.provider_id, key))
        candidate = best_candidate(
            provider_type=provider_type,
            model_name=profile.model_name,
            observed=observed_row,
            refreshed=refreshed,
        )
        # Only offer a suggestion the admin could act on: one that differs
        # from what is stored and that precedence would actually let through.
        suggestion = candidate
        if candidate is not None and (
            candidate.input_cost_micros_per_million == profile.input_cost_micros_per_million
            and candidate.output_cost_micros_per_million == profile.output_cost_micros_per_million
        ):
            suggestion = None
        views.append(
            ProfilePricingView(
                profile_id=profile.id,
                display_name=profile.display_name,
                model_name=profile.model_name,
                provider_id=profile.provider_id,
                provider_type=provider_type,
                input_cost_micros_per_million=profile.input_cost_micros_per_million,
                output_cost_micros_per_million=profile.output_cost_micros_per_million,
                price_source=profile.price_source,
                price_source_label=describe_price_source(
                    profile.price_source,  # type: ignore[arg-type]
                    priced=_is_priced(profile),
                    refreshed_at=snapshot.fetched_at.date() if snapshot else None,
                ),
                priced=_is_priced(profile),
                pricing_page_url=PRICING_PAGES.get(provider_type),
                runs_this_month=usage.get(profile.id, (0, 0, 0))[0],
                suggestion=suggestion,
                suggestion_label=(
                    describe_price_source(
                        suggestion.source,
                        refreshed_at=snapshot.fetched_at.date() if snapshot else None,
                    )
                    if suggestion is not None
                    else None
                ),
                observed=_observed_view(observed_row),
            )
        )

    untracked = await untracked_models(db, workspace_id, since)
    reconcilable = [p for p in providers.values() if _cannot_reconcile(p) is None]
    return PricingStatus(
        catalog_updated=CATALOG_UPDATED,
        catalog_stale=catalog_is_stale(today=today),
        refreshed_source=snapshot.source if snapshot else None,
        refreshed_fetched_at=snapshot.fetched_at if snapshot else None,
        refreshed_entry_count=snapshot.entry_count if snapshot else 0,
        refreshed_attribution=(snapshot.attribution or LITELLM_ATTRIBUTION) if snapshot else None,
        refreshed_project_url=LITELLM_PROJECT_URL,
        profiles=views,
        untracked=untracked,
        untracked_runs=sum(row.runs for row in untracked),
        reconcile_available=bool(reconcilable),
        reconcile_detail=(
            f"{len(reconcilable)} provider(s) can report itemised spend"
            if reconcilable
            else (
                "Measuring real rates needs an OpenAI provider with an admin key — no other "
                "provider reports what it actually billed"
            )
        ),
        pricing_pages=dict(PRICING_PAGES),
    )
