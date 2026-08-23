"""Provider balance, workspace spend, budget validation, and pricing refresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.models import service
from jhin_api.models.schemas import (
    ModelProviderCreate,
    ModelProviderOut,
    ModelProviderUpdate,
    ProviderBalanceOut,
    ProviderModelsResult,
)
from jhin_api.workspaces import service as workspaces
from jhin_api.workspaces.schemas import WorkspaceSettingsIn, WorkspaceUpdate
from jhin_db.models import Agent, AgentRun, ModelProfile, ModelProvider, Secret, Workspace
from jhin_domain import new_uuid7
from jhin_models import (
    AccountStatus,
    AccountStatusUnsupported,
    ModelClient,
    ModelListing,
    ModelProviderError,
)
from jhin_observability import noop_metrics, noop_tracer
from jhin_secrets import SecretCrypto, SecretStore


class _Client(ModelClient):
    def __init__(
        self,
        *,
        status: AccountStatus | None = None,
        error: Exception | None = None,
        listings: list[ModelListing] | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.listings = listings or []
        self.calls = 0
        self.closed = 0

    async def generate(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def stream(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def verify(self) -> str:
        return "ok"

    async def list_models_detailed(self) -> list[ModelListing]:
        if self.error is not None:
            raise self.error
        return self.listings

    async def get_account_status(self) -> AccountStatus | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.status

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    service._clear_account_status_cache()


def _patch_factory(monkeypatch: pytest.MonkeyPatch, client: _Client) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def build(provider_type, *, base_url, api_key, admin_api_key, metrics, tracer):  # type: ignore[no-untyped-def]
        seen.append({"type": provider_type, "api_key": api_key, "admin_api_key": admin_api_key})
        return client

    monkeypatch.setattr(service, "build_model_client", build)
    return seen


async def _provider(
    session: AsyncSession, ws: UUID, *, type_: str = "openai", name: str = "P"
) -> tuple[ModelProvider, ModelProfile]:
    provider = ModelProvider(workspace_id=ws, type=type_, display_name=name)
    session.add(provider)
    await session.flush()
    profile = ModelProfile(
        workspace_id=ws,
        provider_id=provider.id,
        model_name="gpt-4o",
        display_name=f"{name} profile",
    )
    session.add(profile)
    await session.flush()
    return provider, profile


async def _agent(session: AsyncSession, ws: UUID) -> Agent:
    agent = Agent(workspace_id=ws, name=f"A{uuid4().hex[:4]}", slug=f"a-{uuid4().hex[:6]}")
    session.add(agent)
    await session.flush()
    return agent


def _run(
    ws: UUID, agent: Agent, profile: ModelProfile | None, cost: int, when: datetime
) -> AgentRun:
    return AgentRun(
        workspace_id=ws,
        agent_id=agent.id,
        model_profile_id=profile.id if profile else None,
        estimated_cost_micros=cost,
        created_at=when,
    )


async def test_tracked_spend_sums_by_calendar_month_and_provider(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    provider_a, profile_a = await _provider(session, ws, name="A")
    _provider_b, profile_b = await _provider(session, ws, type_="ollama", name="B")
    agent = await _agent(session, ws)
    now = datetime.now(UTC)
    this_month = service.month_start_utc(now) + timedelta(hours=1)
    last_month = service.month_start_utc(now) - timedelta(days=1)
    session.add_all(
        [
            _run(ws, agent, profile_a, 1_000, this_month),
            _run(ws, agent, profile_a, 2_000, now),
            _run(ws, agent, profile_a, 40_000, last_month),
            _run(ws, agent, profile_b, 500, now),
            _run(ws, agent, None, 9_999, now),  # orphaned run: no provider
        ]
    )
    await session.commit()

    _patch_factory(monkeypatch, _Client(error=AccountStatusUnsupported("no admin key")))
    balance = await service.get_provider_balance(
        session, crypto, admin_ctx, provider_a.id, noop_metrics(), noop_tracer()
    )
    assert balance.tracked_spent_month_micros == 3_000
    assert balance.tracked_spent_total_micros == 43_000
    assert balance.source == "tracked"
    assert balance.detail == "no admin key"
    assert balance.provider_spent_month_micros is None
    assert balance.estimated_remaining_micros is None

    spend = await service.get_workspace_spend(session, ws)
    assert spend.spent_month_micros == 3_000 + 500 + 9_999
    assert spend.spent_total_micros == 43_000 + 500 + 9_999
    assert spend.period_start == service.month_start_utc(now)
    by_name = {entry.provider.display_name: entry for entry in spend.providers}
    assert by_name["A"].spent_month_micros == 3_000
    assert by_name["B"].spent_total_micros == 500
    assert spend.monthly_budget_micros is None
    assert spend.warning_threshold == 0.8


async def test_live_status_is_cached_per_provider_and_reveals_admin_key(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    provider, _ = await _provider(session, ws)
    store = SecretStore(session, crypto)
    api_secret = await store.create(workspace_id=ws, name="api", plaintext="sk-live")
    admin_secret = await store.create(workspace_id=ws, name="admin", plaintext="sk-admin")
    provider.secret_id = api_secret.id
    provider.admin_secret_id = admin_secret.id
    provider.credits_loaded_micros = 20_000_000
    await session.commit()

    client = _Client(
        status=AccountStatus(
            spent_month_micros=3_750_000,
            source="openai_admin",
            detail="From OpenAI's admin API (month to date)",
        )
    )
    seen = _patch_factory(monkeypatch, client)
    first = await service.get_provider_balance(
        session, crypto, admin_ctx, provider.id, noop_metrics(), noop_tracer()
    )
    second = await service.get_provider_balance(
        session, crypto, admin_ctx, provider.id, noop_metrics(), noop_tracer()
    )
    assert client.calls == 1, "second poll within the TTL must hit the cache"
    assert client.closed == 1
    assert seen == [{"type": "openai", "api_key": "sk-live", "admin_api_key": "sk-admin"}]
    assert first.source == "openai_admin"
    assert first.provider_spent_month_micros == 3_750_000
    assert first.estimated_remaining_micros == 20_000_000 - 3_750_000
    assert first.detail == "From OpenAI's admin API (month to date)"
    assert second.provider_spent_month_micros == 3_750_000

    # Expire the cache: the next poll goes live again.
    cached = service._ACCOUNT_STATUS_CACHE[provider.id]
    service._ACCOUNT_STATUS_CACHE[provider.id] = service._CachedAccountStatus(
        fetched_at=cached.fetched_at - service.ACCOUNT_STATUS_CACHE_TTL_SECONDS - 1,
        status=cached.status,
        error=cached.error,
    )
    await service.get_provider_balance(
        session, crypto, admin_ctx, provider.id, noop_metrics(), noop_tracer()
    )
    assert client.calls == 2


async def test_openrouter_balance_reports_remaining_and_failures_degrade(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    provider, _ = await _provider(session, ws, type_="openrouter")
    await session.commit()

    _patch_factory(
        monkeypatch,
        _Client(
            status=AccountStatus(
                remaining_micros=37_500_000,
                granted_micros=50_000_000,
                source="openrouter",
                detail="Live from OpenRouter",
            )
        ),
    )
    balance = await service.get_provider_balance(
        session, crypto, admin_ctx, provider.id, noop_metrics(), noop_tracer()
    )
    assert balance.source == "openrouter"
    assert balance.provider_remaining_micros == 37_500_000
    assert ProviderBalanceOut.model_validate(balance, from_attributes=True).source == "openrouter"

    service._clear_account_status_cache()
    _patch_factory(monkeypatch, _Client(error=ModelProviderError("openrouter: HTTP 500: boom")))
    degraded = await service.get_provider_balance(
        session, crypto, admin_ctx, provider.id, noop_metrics(), noop_tracer()
    )
    assert degraded.source == "tracked"
    assert degraded.provider_remaining_micros is None
    assert degraded.detail == "openrouter: HTTP 500: boom"

    # Failures are cached too (no hammering a failing billing API).
    assert service._ACCOUNT_STATUS_CACHE[provider.id].error is not None


async def test_balance_times_out_slow_billing_apis(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    import asyncio

    ws = admin_ctx.workspace_id
    provider, _ = await _provider(session, ws, type_="openrouter")
    await session.commit()

    class Slow(_Client):
        async def get_account_status(self) -> AccountStatus | None:
            await asyncio.sleep(5)
            return None

    _patch_factory(monkeypatch, Slow())
    monkeypatch.setattr(service, "ACCOUNT_STATUS_TIMEOUT_SECONDS", 0.01)
    balance = await service.get_provider_balance(
        session, crypto, admin_ctx, provider.id, noop_metrics(), noop_tracer()
    )
    assert balance.source == "tracked"
    assert balance.detail is not None and "did not answer in time" in balance.detail


async def test_estimate_remaining_rules() -> None:
    assert (
        service.estimate_remaining(
            credits_loaded_micros=None, provider_spent_month_micros=5, tracked_spent_total_micros=1
        )
        is None
    )
    assert (
        service.estimate_remaining(
            credits_loaded_micros=100, provider_spent_month_micros=30, tracked_spent_total_micros=90
        )
        == 70
    )
    assert (
        service.estimate_remaining(
            credits_loaded_micros=100,
            provider_spent_month_micros=None,
            tracked_spent_total_micros=90,
        )
        == 10
    )


async def test_balance_404s_for_unknown_provider(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await service.get_provider_balance(
            session, crypto, admin_ctx, new_uuid7(), noop_metrics(), noop_tracer()
        )
    assert excinfo.value.status_code == 404


# --- Provider schema: admin key and loaded credits ---


def test_provider_schemas_accept_billing_fields_and_expose_has_admin_key() -> None:
    secret_id = new_uuid7()
    create = ModelProviderCreate(
        type="openai", display_name="OpenAI", admin_secret_id=secret_id, credits_loaded_micros=5
    )
    assert create.admin_secret_id == secret_id
    assert create.credits_loaded_micros == 5
    update = ModelProviderUpdate(credits_loaded_micros=None)
    assert update.model_dump(exclude_unset=True) == {"credits_loaded_micros": None}
    with pytest.raises(ValidationError):
        ModelProviderUpdate(credits_loaded_micros=-1)
    out = ModelProviderOut.model_validate(
        ModelProvider(
            id=new_uuid7(),
            workspace_id=new_uuid7(),
            type="openai",
            display_name="x",
            base_url=None,
            secret_id=None,
            admin_secret_id=secret_id,
            credits_loaded_micros=None,
            enabled=True,
            last_verified_at=None,
            last_error=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        from_attributes=True,
    )
    assert out.has_admin_key is True
    assert "admin_secret_id" not in out.model_dump(), "the admin secret id is never exposed"


async def test_admin_secret_must_belong_to_the_workspace(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> None:
    ws = admin_ctx.workspace_id
    other = Workspace(name="Other", slug=f"other-{uuid4().hex[:6]}")
    session.add(other)
    await session.flush()
    foreign = await SecretStore(session, crypto).create(
        workspace_id=other.id, name="foreign", plaintext="x"
    )
    with pytest.raises(HTTPException) as excinfo:
        await service.create_provider(
            session,
            admin_ctx,
            values={"type": "openai", "display_name": "P", "admin_secret_id": foreign.id},
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert excinfo.value.status_code == 422

    provider, _ = await _provider(session, ws)
    await session.commit()
    mine = await SecretStore(session, crypto).create(workspace_id=ws, name="mine", plaintext="y")
    updated = await service.update_provider(
        session,
        admin_ctx,
        provider.id,
        changes={"admin_secret_id": mine.id, "credits_loaded_micros": 1_000_000},
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated.admin_secret_id == mine.id
    assert updated.has_admin_key is True
    assert updated.credits_loaded_micros == 1_000_000
    assert isinstance(await session.get(Secret, mine.id), Secret)


# --- Workspace budget ---


def test_budget_settings_validation() -> None:
    ok = WorkspaceSettingsIn.model_validate({"budget": {"monthly_budget_micros": 50_000_000}})
    assert ok.budget is not None
    assert ok.budget.monthly_budget_micros == 50_000_000
    assert ok.budget.warning_threshold == 0.8
    with pytest.raises(ValidationError):
        WorkspaceSettingsIn.model_validate({"budget": {"monthly_budget_micros": -1}})
    with pytest.raises(ValidationError):
        WorkspaceSettingsIn.model_validate({"budget": {"warning_threshold": 1.5}})
    with pytest.raises(ValidationError):
        WorkspaceSettingsIn.model_validate({"budget": {"unknown": 1}})
    cleared = WorkspaceSettingsIn.model_validate({"budget": {"monthly_budget_micros": None}})
    assert cleared.budget is not None and cleared.budget.monthly_budget_micros is None


async def test_budget_is_stored_alongside_other_settings_and_read_by_spend(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    ws = admin_ctx.workspace_id
    workspace = await session.get(Workspace, ws)
    assert workspace is not None
    workspace.settings_json = {"delegation": {"max_task_depth": 3}}
    await session.commit()

    payload = WorkspaceUpdate.model_validate(
        {"settings": {"budget": {"monthly_budget_micros": 25_000_000, "warning_threshold": 0.5}}}
    )
    await workspaces.update(
        session,
        admin_ctx,
        changes=payload.model_dump(exclude_unset=True, exclude_none=True),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    await session.refresh(workspace)
    assert workspace.settings_json == {
        "delegation": {"max_task_depth": 3},
        "budget": {"monthly_budget_micros": 25_000_000, "warning_threshold": 0.5},
    }
    spend = await service.get_workspace_spend(session, ws)
    assert spend.monthly_budget_micros == 25_000_000
    assert spend.warning_threshold == 0.5
    assert service.budget_from_settings({"budget": "junk"}) == (None, 0.8)
    assert service.budget_from_settings({"budget": {"warning_threshold": 9}}) == (None, 1.0)


# --- Model listing with prices and pricing refresh ---


async def test_list_provider_models_returns_listings(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    provider, _ = await _provider(session, admin_ctx.workspace_id)
    await session.commit()
    listing = ModelListing(
        id="gpt-4o",
        input_cost_micros_per_million=2_500_000,
        output_cost_micros_per_million=10_000_000,
        context_window=128_000,
        source="catalog",
    )
    _patch_factory(monkeypatch, _Client(listings=[listing]))
    models, detail = await service.list_provider_models(
        session, crypto, admin_ctx, provider.id, noop_metrics(), noop_tracer()
    )
    assert detail is None
    result = ProviderModelsResult(models=[m.model_dump() for m in models], detail=detail)  # type: ignore[misc]
    assert result.models[0].id == "gpt-4o"
    assert result.models[0].source == "catalog"
    assert result.models[0].input_cost_micros_per_million == 2_500_000


async def test_refresh_pricing_prefers_provider_list_then_catalog(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto, monkeypatch
) -> None:
    ws = admin_ctx.workspace_id
    _provider_row, profile = await _provider(session, ws)
    await session.commit()

    # Provider list lookup fails -> catalog (gpt-4o is an OpenAI catalog entry).
    _patch_factory(monkeypatch, _Client(error=ModelProviderError("openai: HTTP 401")))
    refreshed, updated, source, detail = await service.refresh_profile_pricing(
        session,
        crypto,
        admin_ctx,
        profile.id,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated is True and source == "catalog"
    assert refreshed.input_cost_micros_per_million == 2_500_000
    assert refreshed.output_cost_micros_per_million == 10_000_000
    assert refreshed.context_window == 128_000
    assert "catalog updated" in detail

    # Provider list carries a different price: it wins.
    live = ModelListing(
        id="GPT-4O",
        input_cost_micros_per_million=1,
        output_cost_micros_per_million=2,
        source="provider",
    )
    _patch_factory(monkeypatch, _Client(listings=[live]))
    refreshed, updated, source, _ = await service.refresh_profile_pricing(
        session,
        crypto,
        admin_ctx,
        profile.id,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert (updated, source) == (True, "provider")
    assert refreshed.input_cost_micros_per_million == 1
    assert refreshed.context_window == 128_000, "unknown context keeps the stored value"

    # Already up to date.
    _, updated, source, detail = await service.refresh_profile_pricing(
        session,
        crypto,
        admin_ctx,
        profile.id,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated is False and source == "provider"
    assert detail.startswith("Prices already match")

    # Unknown model: nothing changes, explained.
    profile.model_name = "mystery-model"
    await session.commit()
    _patch_factory(monkeypatch, _Client(listings=[]))
    _, updated, source, detail = await service.refresh_profile_pricing(
        session,
        crypto,
        admin_ctx,
        profile.id,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert (updated, source) == (False, None)
    assert detail.startswith("No price is known")
