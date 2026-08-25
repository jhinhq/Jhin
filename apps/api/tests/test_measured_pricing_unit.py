"""Measured pricing: reconciliation, catalog refresh, and precedence.

The rule these tests defend is that Jhin never quietly overwrites a price a
human entered, never invents a price it does not have, and always says which
of the five sources a number came from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.models import pricing_service, service
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_db.models import (
    Agent,
    AgentRun,
    AuditEvent,
    ModelObservedPrice,
    ModelProfile,
    ModelProvider,
    PriceCatalogSnapshot,
)
from jhin_domain import new_uuid7
from jhin_models import ModelClient, ModelListing, ModelProviderError
from jhin_models.observed_pricing import CostLine, ModelCostReport
from jhin_models.pricing import LITELLM_PRICE_MAP_URL
from jhin_models.testing import SAMPLE_LITELLM_PRICE_MAP as LITELLM_FIXTURE
from jhin_observability import noop_metrics, noop_tracer
from jhin_secrets import SecretCrypto, SecretStore


class _AdminClient(ModelClient):
    """A provider whose Admin API itemises spend, as OpenAI's really does."""

    def __init__(
        self, report: ModelCostReport | None = None, error: Exception | None = None
    ) -> None:
        self.report = report
        self.error = error
        self.closed = 0
        self.periods: list[tuple[Any, Any]] = []

    async def generate(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def stream(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def verify(self) -> str:
        return "ok"

    async def list_models_detailed(self) -> list[ModelListing]:
        return []

    async def fetch_model_costs(self, *, start: Any, end: Any) -> ModelCostReport:
        self.periods.append((start, end))
        if self.error is not None:
            raise self.error
        assert self.report is not None
        return self.report

    async def close(self) -> None:
        self.closed += 1


def _patch_factory(monkeypatch: pytest.MonkeyPatch, client: ModelClient) -> None:
    def build(provider_type, *, base_url, api_key, admin_api_key, metrics, tracer):  # type: ignore[no-untyped-def]
        return client

    monkeypatch.setattr(service, "build_model_client", build)


def _report(*lines: CostLine, ignored_micros: int = 0) -> ModelCostReport:
    return ModelCostReport(
        lines=list(lines),
        total_micros=sum(line.cost_micros for line in lines) + ignored_micros,
        ignored_micros=ignored_micros,
        ignored_labels=["assistants api | file search"] if ignored_micros else [],
    )


async def _openai_provider(
    session: AsyncSession,
    crypto: SecretCrypto,
    ws: UUID,
    *,
    with_admin_key: bool = True,
    model: str = "gpt-4o",
    name: str = "OpenAI",
) -> tuple[ModelProvider, ModelProfile]:
    admin_secret_id = None
    if with_admin_key:
        secret = await SecretStore(session, crypto).create(
            workspace_id=ws, name=f"admin-{uuid4().hex[:6]}", plaintext="sk-admin"
        )
        admin_secret_id = secret.id
    provider = ModelProvider(
        workspace_id=ws, type="openai", display_name=name, admin_secret_id=admin_secret_id
    )
    session.add(provider)
    await session.flush()
    profile = ModelProfile(
        workspace_id=ws,
        provider_id=provider.id,
        model_name=model,
        display_name=f"{name} {model}",
    )
    session.add(profile)
    await session.flush()
    return provider, profile


async def _runs(
    session: AsyncSession,
    ws: UUID,
    profile: ModelProfile,
    *,
    count: int = 25,
    input_tokens: int = 40_000,
    output_tokens: int = 4_000,
    days_ago: int = 5,
) -> None:
    agent = Agent(workspace_id=ws, name=f"A{uuid4().hex[:4]}", slug=f"a-{uuid4().hex[:6]}")
    session.add(agent)
    await session.flush()
    when = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days_ago
    )
    for _ in range(count):
        session.add(
            AgentRun(
                workspace_id=ws,
                agent_id=agent.id,
                model_profile_id=profile.id,
                status="succeeded",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_micros=0,
                created_at=when,
            )
        )
    await session.flush()


async def _reconcile(session: AsyncSession, crypto: SecretCrypto, ctx: WorkspaceContext) -> Any:
    return await pricing_service.reconcile_pricing(
        session,
        crypto,
        ctx,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
    )


# --- The reconciliation window ---


def test_the_current_day_is_excluded_from_the_measurement_window() -> None:
    """Today's invoice is still filling while today's tokens are complete.

    Dividing a partial bill by a full day of usage would understate every
    rate, so the window ends at midnight today.
    """
    now = datetime(2026, 8, 24, 15, 30, tzinfo=UTC)
    start, end = pricing_service.reconcile_period(now)
    assert end == datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    assert start == end - timedelta(days=pricing_service.RECONCILE_WINDOW_DAYS)


# --- Reconciliation end to end ---


async def test_reconciliation_measures_a_rate_and_writes_it_onto_the_profile(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    """The exact path: the invoice carries both dollars and billed tokens.

    $0.10 for 1,000,000 input tokens is $0.10 per 1M — a heavy discount off
    the $2.50 list price, which is precisely the fact a list price cannot
    tell you.
    """
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    await _runs(session, ws, profile)
    await session.commit()

    _patch_factory(
        monkeypatch,
        _AdminClient(
            _report(
                CostLine("gpt-4o", "input", 100_000, billed_tokens=1_000_000),
                CostLine("gpt-4o", "output", 100_000, billed_tokens=100_000),
                ignored_micros=20_000,
            )
        ),
    )
    result = await _reconcile(session, crypto, admin_ctx)

    assert len(result.providers) == 1
    outcome = result.providers[0]
    rate = outcome.derived[0]
    assert rate.derivation == "provider_quantity"
    assert rate.input_micros_per_million == 100_000
    assert rate.output_micros_per_million == 1_000_000

    await session.refresh(profile)
    assert profile.input_cost_micros_per_million == 100_000
    assert profile.output_cost_micros_per_million == 1_000_000
    assert profile.price_source == "observed"
    assert outcome.applied[0].to_source == "observed"
    assert outcome.unattributed_micros == 20_000, "non-model spend is reported, not attributed"


async def test_a_measured_rate_is_persisted_with_the_evidence_behind_it(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    provider, profile = await _openai_provider(session, crypto, ws)
    await _runs(session, ws, profile)
    await session.commit()
    _patch_factory(
        monkeypatch,
        _AdminClient(
            _report(
                CostLine("gpt-4o", "input", 2_500_000),
                CostLine("gpt-4o", "output", 1_000_000),
            )
        ),
    )
    await _reconcile(session, crypto, admin_ctx)

    row = await session.scalar(
        select(ModelObservedPrice).where(ModelObservedPrice.provider_id == provider.id)
    )
    assert row is not None
    assert row.model_key == "gpt-4o"
    assert row.derivation == "split"
    assert row.sample_runs == 25
    assert row.sample_input_tokens == 1_000_000
    assert row.sample_cost_micros == 3_500_000
    assert row.period_end > row.period_start
    assert "divided by Jhin's own token counts" in row.note


async def test_a_blended_measurement_is_recorded_but_never_split_onto_the_profile(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    """An unknown model with one undifferentiated cost line.

    The blended rate is real information and is stored, but writing it into
    the profile's separate input and output columns would be a fabrication.
    """
    ws = admin_ctx.workspace_id
    provider, profile = await _openai_provider(session, crypto, ws, model="gpt-5.6-terra")
    await _runs(session, ws, profile)
    await session.commit()
    _patch_factory(monkeypatch, _AdminClient(_report(CostLine("gpt-5.6-terra", None, 2_200_000))))

    result = await _reconcile(session, crypto, admin_ctx)
    rate = result.providers[0].derived[0]
    assert rate.derivation == "blended"
    assert rate.blended_micros_per_million == 2_000_000

    row = await session.scalar(
        select(ModelObservedPrice).where(ModelObservedPrice.provider_id == provider.id)
    )
    assert row is not None and row.blended_cost_micros_per_million == 2_000_000
    assert row.input_cost_micros_per_million is None

    await session.refresh(profile)
    assert profile.input_cost_micros_per_million is None, "no invented split"
    assert result.providers[0].applied == []


async def test_too_small_a_sample_is_skipped_with_a_reason_the_admin_can_read(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    await _runs(session, ws, profile, count=1, input_tokens=100, output_tokens=10)
    await session.commit()
    _patch_factory(
        monkeypatch,
        _AdminClient(
            _report(
                CostLine("gpt-4o", "input", 2_500_000),
                CostLine("gpt-4o", "output", 1_000_000),
            )
        ),
    )
    result = await _reconcile(session, crypto, admin_ctx)
    outcome = result.providers[0]
    assert outcome.derived == []
    assert "at least 3 are needed" in outcome.skipped[0].reason

    # No measured rate, but the profile still ends up priced from the catalog:
    # failing to measure must not leave a model tracking spend as $0.
    await session.refresh(profile)
    assert profile.price_source == "catalog"
    assert profile.input_cost_micros_per_million == 2_500_000


async def test_providers_that_cannot_report_spend_are_skipped_with_the_fix(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    ws = admin_ctx.workspace_id
    await _openai_provider(session, crypto, ws, with_admin_key=False, name="No admin key")
    session.add(ModelProvider(workspace_id=ws, type="anthropic", display_name="Anthropic"))
    await session.commit()

    result = await _reconcile(session, crypto, admin_ctx)
    reasons = {row.display_name: row.reason for row in result.skipped_providers}
    assert "add one to measure real rates" in reasons["No admin key"]
    assert "does not report itemised spend" in reasons["Anthropic"]
    assert result.providers == []
    assert "No provider in this workspace can report itemised spend" in result.detail


async def test_a_failing_billing_api_skips_that_provider_without_failing_the_request(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    await _openai_provider(session, crypto, ws)
    await session.commit()
    _patch_factory(
        monkeypatch, _AdminClient(error=ModelProviderError("openai: admin API HTTP 401"))
    )
    result = await _reconcile(session, crypto, admin_ctx)
    assert result.providers == []
    assert "HTTP 401" in result.skipped_providers[0].reason


async def test_reconciliation_is_audited(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    await _runs(session, ws, profile)
    await session.commit()
    _patch_factory(
        monkeypatch,
        _AdminClient(_report(CostLine("gpt-4o", "input", 100_000, billed_tokens=1_000_000))),
    )
    await _reconcile(session, crypto, admin_ctx)

    event = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "model_pricing.reconciled")
    )
    assert event is not None
    assert event.workspace_id == ws
    assert event.actor_id == admin_ctx.user.id
    assert event.metadata_json["providers"] == 1


# --- Precedence: the user always wins ---


async def test_a_measured_rate_never_overwrites_a_price_an_admin_typed(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    profile.input_cost_micros_per_million = 777
    profile.output_cost_micros_per_million = 888
    profile.price_source = "user"
    await _runs(session, ws, profile)
    await session.commit()
    _patch_factory(
        monkeypatch,
        _AdminClient(
            _report(
                CostLine("gpt-4o", "input", 100_000, billed_tokens=1_000_000),
                CostLine("gpt-4o", "output", 100_000, billed_tokens=100_000),
            )
        ),
    )
    result = await _reconcile(session, crypto, admin_ctx)

    assert result.providers[0].derived, "the rate is still measured and reported"
    assert result.providers[0].applied == [], "but nothing is written"
    await session.refresh(profile)
    assert (profile.input_cost_micros_per_million, profile.price_source) == (777, "user")


async def test_an_unknown_provenance_price_is_protected_like_a_typed_one(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    """Rows predating provenance, or posted straight to the API, are theirs.

    Being wrong in this direction leaves a stale price; being wrong the other
    way silently replaces a real contract price. We take the stale one.
    """
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    profile.input_cost_micros_per_million = 42
    profile.output_cost_micros_per_million = 43
    profile.price_source = None
    await session.commit()

    assert not pricing_service.may_write_price(profile, "observed")
    assert not pricing_service.may_write_price(profile, "catalog")


async def test_an_unpriced_profile_may_be_filled_by_anything(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    """Nothing to protect: an empty price is the problem, not an opinion."""
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    assert pricing_service.may_write_price(profile, "catalog")
    assert pricing_service.may_write_price(profile, "observed")


async def test_a_lower_source_cannot_displace_a_higher_one(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    profile.input_cost_micros_per_million = 1
    profile.output_cost_micros_per_million = 2
    profile.price_source = "observed"
    await session.commit()

    assert not pricing_service.may_write_price(profile, "catalog")
    assert not pricing_service.may_write_price(profile, "refreshed_catalog")
    assert pricing_service.may_write_price(profile, "observed"), "a fresher measurement may"


async def test_a_price_posted_through_the_api_is_stamped_as_the_users(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """Nothing automatic writes through the public endpoint, so this is a human."""
    ws = admin_ctx.workspace_id
    provider = ModelProvider(workspace_id=ws, type="openai", display_name="P")
    session.add(provider)
    await session.flush()
    await session.commit()

    profile = await service.create_profile(
        session,
        admin_ctx,
        values={
            "provider_id": provider.id,
            "model_name": "gpt-5.6-terra",
            "display_name": "Terra",
            "input_cost_micros_per_million": 2_000_000,
            "output_cost_micros_per_million": 12_000_000,
            "context_window": None,
            "supports_tools": True,
            "supports_reasoning": False,
            "config_json": {},
        },
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert profile.price_source == "user"

    updated = await service.update_profile(
        session,
        admin_ctx,
        profile.id,
        changes={"input_cost_micros_per_million": 3_000_000},
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated.price_source == "user"

    # Renaming a profile is not a price edit and must not re-stamp it.
    renamed = await service.update_profile(
        session,
        admin_ctx,
        profile.id,
        changes={"display_name": "Terra II"},
        request_id=new_uuid7(),
        ip_hash="h",
        price_source="catalog",
    )
    assert renamed.price_source == "user"


# --- Catalog refresh ---


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    async def fake(url: str) -> object:
        return payload

    monkeypatch.setattr(pricing_service, "_fetch_price_map", fake)


async def test_catalog_refresh_stores_a_trimmed_snapshot_and_reprices(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    """A model the built-in catalog has never heard of gets priced."""
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws, model="gpt-5.6-terra")
    await session.commit()
    _patch_fetch(monkeypatch, LITELLM_FIXTURE)

    result = await pricing_service.refresh_price_catalog(
        session, admin_ctx, request_id=new_uuid7(), ip_hash="h"
    )
    assert result.updated is True
    assert result.entry_count > 0
    assert "LiteLLM" in result.detail

    await session.refresh(profile)
    assert profile.input_cost_micros_per_million == 2_000_000
    assert profile.output_cost_micros_per_million == 12_000_000
    assert profile.price_source == "refreshed_catalog"
    assert profile.context_window == 922_000

    snapshot = await session.scalar(
        select(PriceCatalogSnapshot).where(PriceCatalogSnapshot.workspace_id == ws)
    )
    assert snapshot is not None
    assert snapshot.source == "litellm"
    assert snapshot.source_url == LITELLM_PRICE_MAP_URL


async def test_catalog_refresh_is_audited(
    session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch
) -> None:
    _patch_fetch(monkeypatch, LITELLM_FIXTURE)
    await pricing_service.refresh_price_catalog(
        session, admin_ctx, request_id=new_uuid7(), ip_hash="h"
    )
    event = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "price_catalog.refreshed")
    )
    assert event is not None and event.metadata_json["source"] == "litellm"


@pytest.mark.parametrize(
    ("failure", "fragment"),
    [
        (httpx.ConnectError("boom"), "network error"),
        (EndpointPolicyError("Price catalog URL is not public"), "not public"),
    ],
)
async def test_a_failed_refresh_falls_back_instead_of_breaking_pricing(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch,
    failure: Exception,
    fragment: str,
) -> None:
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    await session.commit()

    async def fail(url: str) -> object:
        raise failure

    monkeypatch.setattr(pricing_service, "_fetch_price_map", fail)
    result = await pricing_service.refresh_price_catalog(
        session, admin_ctx, request_id=new_uuid7(), ip_hash="h"
    )
    assert result.updated is False
    assert fragment in result.detail
    assert "still using the built-in price list" in result.detail

    # The built-in catalog still prices gpt-4o, so pricing is not lost.
    applied = await pricing_service.apply_best_prices(session, ws)
    await session.commit()
    await session.refresh(profile)
    assert profile.input_cost_micros_per_million == 2_500_000
    assert applied[0].to_source == "catalog"


async def test_a_failed_refresh_keeps_the_previous_snapshot(
    session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch
) -> None:
    _patch_fetch(monkeypatch, LITELLM_FIXTURE)
    first = await pricing_service.refresh_price_catalog(
        session, admin_ctx, request_id=new_uuid7(), ip_hash="h"
    )

    async def fail(url: str) -> object:
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(pricing_service, "_fetch_price_map", fail)
    second = await pricing_service.refresh_price_catalog(
        session, admin_ctx, request_id=new_uuid7(), ip_hash="h"
    )
    assert second.updated is False
    assert second.entry_count == first.entry_count
    assert "still using the catalog refreshed on" in second.detail

    catalog = await pricing_service.load_refreshed_catalog(session, admin_ctx.workspace_id)
    assert "gpt-5.6-terra" in catalog["openai"]


async def test_an_empty_price_map_never_wipes_a_good_snapshot(
    session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch
) -> None:
    _patch_fetch(monkeypatch, LITELLM_FIXTURE)
    await pricing_service.refresh_price_catalog(
        session, admin_ctx, request_id=new_uuid7(), ip_hash="h"
    )
    _patch_fetch(monkeypatch, {"sample_spec": {"litellm_provider": "doc"}})
    result = await pricing_service.refresh_price_catalog(
        session, admin_ctx, request_id=new_uuid7(), ip_hash="h"
    )
    assert result.updated is False
    assert "contained no usable prices" in result.detail


async def test_the_fetch_is_size_capped_and_goes_through_the_public_url_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A community file is untrusted input: bounded, and never a private host."""
    with pytest.raises(EndpointPolicyError):
        await pricing_service._fetch_price_map("http://169.254.169.254/latest/meta-data")
    with pytest.raises(EndpointPolicyError):
        await pricing_service._fetch_price_map("http://localhost:8080/prices.json")


# --- Honest reporting for the UI ---


async def test_pricing_status_names_the_source_and_offers_the_pricing_page(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws, model="gpt-5.6-terra")
    await _runs(session, ws, profile, count=3, days_ago=0)
    await session.commit()

    status = await pricing_service.pricing_status(
        session, ws, since=service.month_start_utc(), today=None
    )
    view = status.profiles[0]
    assert view.priced is False
    assert view.price_source is None
    assert view.price_source_label == "No price is known for this model"
    assert view.pricing_page_url == "https://platform.openai.com/docs/pricing"
    assert view.runs_this_month == 3
    assert status.untracked[0].model_name == "gpt-5.6-terra"
    assert status.untracked_runs == 3
    assert status.reconcile_available is True


async def test_pricing_status_explains_why_measuring_is_unavailable(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    ws = admin_ctx.workspace_id
    await _openai_provider(session, crypto, ws, with_admin_key=False)
    await session.commit()
    status = await pricing_service.pricing_status(session, ws, since=service.month_start_utc())
    assert status.reconcile_available is False
    assert "admin key" in status.reconcile_detail


async def test_pricing_status_suggests_a_better_price_without_applying_it(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    profile.input_cost_micros_per_million = 999
    profile.output_cost_micros_per_million = 999
    profile.price_source = "user"
    await session.commit()

    status = await pricing_service.pricing_status(session, ws, since=service.month_start_utc())
    view = status.profiles[0]
    assert view.price_source_label == "Entered by an admin in this workspace"
    assert view.suggestion is not None
    assert view.suggestion.source == "catalog"
    assert view.suggestion.input_cost_micros_per_million == 2_500_000
    assert view.input_cost_micros_per_million == 999, "the suggestion is not applied"


async def test_a_priced_profile_that_already_matches_gets_no_suggestion(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    profile.input_cost_micros_per_million = 2_500_000
    profile.output_cost_micros_per_million = 10_000_000
    profile.price_source = "catalog"
    await session.commit()

    status = await pricing_service.pricing_status(session, ws, since=service.month_start_utc())
    assert status.profiles[0].suggestion is None


async def test_untracked_models_only_counts_runs_on_unpriced_profiles(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    ws = admin_ctx.workspace_id
    _p1, unpriced = await _openai_provider(session, crypto, ws, model="gpt-5.6-terra", name="A")
    _p2, priced = await _openai_provider(session, crypto, ws, model="gpt-4o", name="B")
    priced.input_cost_micros_per_million = 2_500_000
    priced.output_cost_micros_per_million = 10_000_000
    await _runs(session, ws, unpriced, count=3, days_ago=0)
    await _runs(session, ws, priced, count=5, days_ago=0)
    await session.commit()

    rows = await pricing_service.untracked_models(session, ws, service.month_start_utc())
    assert [row.model_name for row in rows] == ["gpt-5.6-terra"]
    assert rows[0].runs == 3


async def test_refresh_pricing_reports_the_user_price_it_declined_to_replace(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    """The admin sees the difference and can choose, rather than being
    silently overruled or silently left stale."""
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    profile.input_cost_micros_per_million = 9_000_000
    profile.output_cost_micros_per_million = 9_000_000
    profile.price_source = "user"
    await session.commit()
    _patch_factory(monkeypatch, _AdminClient())

    _row, updated, source, detail = await service.refresh_profile_pricing(
        session,
        crypto,
        admin_ctx,
        profile.id,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated is False and source == "catalog"
    assert "Kept your own price ($9.00 in / $9.00 out per 1M tokens)" in detail
    assert "$2.50 in / $10.00 out" in detail

    _forced_row, forced, _source, _detail = await service.refresh_profile_pricing(
        session,
        crypto,
        admin_ctx,
        profile.id,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
        force=True,
    )
    assert forced is True
    await session.refresh(profile)
    assert profile.input_cost_micros_per_million == 2_500_000
    assert profile.price_source == "user", "an explicit override stays the admin's own"


# --- Routes ---


async def test_pricing_routes_are_reachable_and_admin_gated(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    """Exercises the real ASGI path, including response-model validation.

    The service functions are covered above; this catches the seam between
    them and the schemas, which unit-testing the dataclasses cannot.
    """
    from collections.abc import AsyncIterator

    from fastapi import FastAPI, Request

    from jhin_api.deps import Principal, get_current_principal, get_db
    from jhin_api.models.router import profiles_router, spend_router
    from jhin_api.settings import Settings
    from jhin_db.models import WorkspaceMembership
    from jhin_domain import WorkspaceRole

    ws = admin_ctx.workspace_id
    session.add(
        WorkspaceMembership(
            workspace_id=ws, user_id=admin_ctx.user.id, role=WorkspaceRole.ADMIN.value
        )
    )
    _provider, profile = await _openai_provider(session, crypto, ws, model="gpt-5.6-terra")
    await _runs(session, ws, profile, count=3, days_ago=0)
    await session.commit()

    app = FastAPI()
    app.state.settings = Settings()

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(profiles_router)
    app.include_router(spend_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_principal() -> Principal:
        return Principal(user=admin_ctx.user)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = override_principal

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("jhin_csrf", "csrf")
        base = f"/api/v1/workspaces/{ws}"
        headers = {"x-csrf-token": "csrf"}

        status_response = await client.get(f"{base}/model-profiles/pricing-status", headers=headers)
        assert status_response.status_code == 200, status_response.text
        body = status_response.json()
        assert body["profiles"][0]["priced"] is False
        assert body["untracked_runs"] == 3
        assert body["refreshed_project_url"] == "https://github.com/BerriAI/litellm"

        spend_response = await client.get(f"{base}/spend", headers=headers)
        assert spend_response.status_code == 200, spend_response.text
        assert spend_response.json()["untracked"][0]["model_name"] == "gpt-5.6-terra"

        profiles = await client.get(f"{base}/model-profiles", headers=headers)
        assert profiles.status_code == 200
        assert "price_source" in profiles.json()[0], "the UI needs the provenance it renders"


async def test_creating_a_profile_without_prices_claims_no_provenance(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """An unpriced profile is not "the admin's price" — it has no price.

    Stamping it ``user`` would be self-defeating: the row would then be
    protected from the very sources meant to fill it.
    """
    ws = admin_ctx.workspace_id
    provider = ModelProvider(workspace_id=ws, type="openai", display_name="P")
    session.add(provider)
    await session.flush()
    await session.commit()

    profile = await service.create_profile(
        session,
        admin_ctx,
        values={
            "provider_id": provider.id,
            "model_name": "gpt-4o",
            "display_name": "Unpriced",
            "input_cost_micros_per_million": None,
            "output_cost_micros_per_million": None,
            "context_window": None,
            "supports_tools": True,
            "supports_reasoning": False,
            "config_json": {},
        },
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert profile.price_source is None
    assert pricing_service.may_write_price(profile, "catalog")


async def test_clearing_a_price_clears_its_provenance(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    """A row must not keep claiming a source for a number it no longer has."""
    ws = admin_ctx.workspace_id
    _provider, profile = await _openai_provider(session, crypto, ws)
    profile.input_cost_micros_per_million = 5
    profile.output_cost_micros_per_million = 6
    profile.price_source = "user"
    await session.commit()

    cleared = await service.update_profile(
        session,
        admin_ctx,
        profile.id,
        changes={
            "input_cost_micros_per_million": None,
            "output_cost_micros_per_million": None,
        },
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert cleared.price_source is None
    assert pricing_service.may_write_price(cleared, "catalog")
