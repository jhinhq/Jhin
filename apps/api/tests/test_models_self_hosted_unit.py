"""The self-hosted pricing assumption at the API layer.

An unpriced model on Ollama or an OpenAI-compatible endpoint is reported as
free — with the word "assumed" attached — instead of as unknown, and is never
counted as untracked spend. The rule these tests defend is that the
assumption is a reading of the row's nulls, not a number written to them: a
real price from any source still fills the row, a typed price outranks the
assumption, and clearing that price falls back to it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.models import pricing_service, service
from jhin_db.models import Agent, AgentRun, ModelProfile, ModelProvider
from jhin_domain import new_uuid7
from jhin_models import ModelClient, ModelListing
from jhin_observability import noop_metrics, noop_tracer
from jhin_secrets import SecretCrypto

ASSUMED_FREE = (
    "Assumed free: a self-hosted endpoint has no per-token price. "
    "Enter prices if this endpoint bills you."
)


async def _profile(
    session: AsyncSession,
    ws: UUID,
    *,
    provider_type: str,
    model: str,
    base_url: str | None = None,
) -> ModelProfile:
    provider = ModelProvider(
        workspace_id=ws,
        type=provider_type,
        display_name=f"{provider_type} {uuid4().hex[:4]}",
        base_url=base_url,
    )
    session.add(provider)
    await session.flush()
    profile = ModelProfile(
        workspace_id=ws, provider_id=provider.id, model_name=model, display_name=model
    )
    session.add(profile)
    await session.flush()
    return profile


async def _runs(session: AsyncSession, ws: UUID, profile: ModelProfile, *, count: int) -> None:
    agent = Agent(workspace_id=ws, name=f"A{uuid4().hex[:4]}", slug=f"a-{uuid4().hex[:6]}")
    session.add(agent)
    await session.flush()
    when = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        minutes=1
    )
    for _ in range(count):
        session.add(
            AgentRun(
                workspace_id=ws,
                agent_id=agent.id,
                model_profile_id=profile.id,
                status="succeeded",
                input_tokens=1_000,
                output_tokens=100,
                estimated_cost_micros=0,
                created_at=when,
            )
        )
    await session.flush()


async def _status_row(session: AsyncSession, ws: UUID) -> Any:
    status = await pricing_service.pricing_status(session, ws, since=service.month_start_utc())
    assert len(status.profiles) == 1
    return status.profiles[0]


# --- Untracked spend ---


async def test_untracked_models_skip_self_hosted_profiles_but_not_hosted_ones(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """A run on a self-hosted model really did cost $0; only the hosted one is untracked."""
    ws = admin_ctx.workspace_id
    ollama = await _profile(session, ws, provider_type="ollama", model="qwen3.8:latest")
    compatible = await _profile(session, ws, provider_type="openai_compatible", model="local-llm")
    hosted = await _profile(session, ws, provider_type="openai", model="gpt-5.6-terra")
    for profile in (ollama, compatible, hosted):
        await _runs(session, ws, profile, count=2)
    await session.commit()

    rows = await pricing_service.untracked_models(session, ws, service.month_start_utc())
    assert [row.model_name for row in rows] == ["gpt-5.6-terra"]
    assert rows[0].runs == 2


# --- Pricing status rows ---


async def test_pricing_status_reports_a_self_hosted_profile_as_assumed_free(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    ws = admin_ctx.workspace_id
    profile = await _profile(session, ws, provider_type="ollama", model="qwen3.8:latest")
    await _runs(session, ws, profile, count=3)
    await session.commit()

    status = await pricing_service.pricing_status(session, ws, since=service.month_start_utc())
    view = status.profiles[0]
    assert view.priced is True
    assert view.assumed_free is True
    assert view.price_source == "self_hosted"
    assert view.price_source_label == ASSUMED_FREE
    assert (view.input_cost_micros_per_million, view.output_cost_micros_per_million) == (0, 0)
    assert view.runs_this_month == 3
    assert status.untracked == []
    assert status.untracked_runs == 0
    # Reporting $0 wrote nothing: the row keeps its nulls and claims no source.
    assert profile.input_cost_micros_per_million is None
    assert profile.output_cost_micros_per_million is None
    assert profile.price_source is None


async def test_a_hosted_profile_without_a_price_is_still_unknown(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """The assumption is the one exception to "unknown is never zero"."""
    ws = admin_ctx.workspace_id
    await _profile(session, ws, provider_type="openai", model="gpt-5.6-terra")
    await session.commit()

    view = await _status_row(session, ws)
    assert view.priced is False
    assert view.assumed_free is False
    assert view.price_source is None
    assert view.price_source_label == "No price is known for this model"


async def test_assumed_free_flips_with_a_stored_price_and_back(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """A typed price outranks the assumption; clearing it falls back to it."""
    ws = admin_ctx.workspace_id
    profile = await _profile(session, ws, provider_type="openai_compatible", model="local-llm")
    await session.commit()
    assert (await _status_row(session, ws)).assumed_free is True

    priced = await service.update_profile(
        session,
        admin_ctx,
        profile.id,
        changes={
            "input_cost_micros_per_million": 150_000,
            "output_cost_micros_per_million": 600_000,
        },
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert priced.price_source == "user"
    assert pricing_service.assumed_free(priced, "openai_compatible") is False
    view = await _status_row(session, ws)
    assert view.assumed_free is False
    assert view.priced is True
    assert view.price_source == "user"
    assert view.input_cost_micros_per_million == 150_000
    assert view.output_cost_micros_per_million == 600_000

    cleared = await service.update_profile(
        session,
        admin_ctx,
        profile.id,
        changes={"input_cost_micros_per_million": None, "output_cost_micros_per_million": None},
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert cleared.price_source is None
    assert cleared.input_cost_micros_per_million is None
    assert pricing_service.assumed_free(cleared, "openai_compatible") is True
    view = await _status_row(session, ws)
    assert view.assumed_free is True
    assert view.price_source == "self_hosted"
    assert (view.input_cost_micros_per_million, view.output_cost_micros_per_million) == (0, 0)


async def test_a_real_source_may_still_fill_a_self_hosted_profile(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """Reading as free must not protect the row from a source that knows a number."""
    ws = admin_ctx.workspace_id
    profile = await _profile(session, ws, provider_type="ollama", model="qwen3.8:latest")
    await session.commit()

    assert pricing_service.has_stored_price(profile) is False
    assert pricing_service.assumed_free(profile, "ollama") is True
    assert pricing_service.may_write_price(profile, "observed")
    assert pricing_service.may_write_price(profile, "catalog")


# --- Refreshing a self-hosted profile's price ---


class _SilentClient(ModelClient):
    """A provider whose model list carries no prices, as a local Ollama does."""

    def __init__(self) -> None:
        self.closed = 0

    async def generate(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def stream(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def verify(self) -> str:
        return "ok"

    async def list_models_detailed(self) -> list[ModelListing]:
        return []

    async def close(self) -> None:
        self.closed += 1


def _patch_factory(monkeypatch: pytest.MonkeyPatch, client: ModelClient) -> None:
    def build(provider_type, *, base_url, api_key, metrics, tracer, admin_api_key=None):  # type: ignore[no-untyped-def]
        return client

    monkeypatch.setattr(service, "build_model_client", build)


async def test_refresh_reports_the_assumption_without_storing_it(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = admin_ctx.workspace_id
    profile = await _profile(
        session,
        ws,
        provider_type="ollama",
        model="qwen3.8:latest",
        base_url="http://ollama.internal:11434",
    )
    await session.commit()
    client = _SilentClient()
    _patch_factory(monkeypatch, client)

    result, updated, source, detail = await service.refresh_profile_pricing(
        session,
        crypto,
        admin_ctx,
        profile.id,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated is False
    assert source == "self_hosted"
    assert detail == ASSUMED_FREE
    assert client.closed == 1
    assert result.input_cost_micros_per_million is None
    assert result.output_cost_micros_per_million is None
    assert result.price_source is None


async def test_refresh_keeps_a_stored_price_on_a_self_hosted_profile(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed $0 is a price the admin chose; the assumption never replaces it."""
    ws = admin_ctx.workspace_id
    profile = await _profile(
        session,
        ws,
        provider_type="ollama",
        model="qwen3.8:latest",
        base_url="http://ollama.internal:11434",
    )
    profile.input_cost_micros_per_million = 0
    profile.output_cost_micros_per_million = 0
    profile.price_source = "user"
    await session.commit()
    _patch_factory(monkeypatch, _SilentClient())

    result, updated, source, detail = await service.refresh_profile_pricing(
        session,
        crypto,
        admin_ctx,
        profile.id,
        noop_metrics(),
        noop_tracer(),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated is False
    assert source is None
    assert "kept the stored one" in detail
    assert result.price_source == "user"
    assert result.input_cost_micros_per_million == 0


# --- The wire shape ---


async def test_profile_endpoints_carry_assumed_free(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """Exercises the real ASGI path, so the response models validate
    ``self_hosted`` and ``assumed_free`` end to end."""
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
    profile = await _profile(session, ws, provider_type="ollama", model="qwen3.8:latest")
    await _runs(session, ws, profile, count=3)
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

        listed = await client.get(f"{base}/model-profiles", headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()[0]["assumed_free"] is True
        assert listed.json()[0]["input_cost_micros_per_million"] is None
        assert listed.json()[0]["price_source"] is None

        status = await client.get(f"{base}/model-profiles/pricing-status", headers=headers)
        assert status.status_code == 200, status.text
        row = status.json()["profiles"][0]
        assert row["priced"] is True
        assert row["assumed_free"] is True
        assert row["price_source"] == "self_hosted"
        assert row["price_source_label"] == ASSUMED_FREE
        assert status.json()["untracked_runs"] == 0

        spend = await client.get(f"{base}/spend", headers=headers)
        assert spend.status_code == 200, spend.text
        assert spend.json()["untracked"] == []

        priced = await client.patch(
            f"{base}/model-profiles/{profile.id}",
            json={
                "input_cost_micros_per_million": 150_000,
                "output_cost_micros_per_million": 600_000,
            },
            headers=headers,
        )
        assert priced.status_code == 200, priced.text
        assert priced.json()["assumed_free"] is False
        assert priced.json()["price_source"] == "user"

        cleared = await client.patch(
            f"{base}/model-profiles/{profile.id}",
            json={"input_cost_micros_per_million": None, "output_cost_micros_per_million": None},
            headers=headers,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["assumed_free"] is True
        assert cleared.json()["price_source"] is None

        single = await client.get(f"{base}/model-profiles/{profile.id}", headers=headers)
        assert single.status_code == 200, single.text
        assert single.json()["assumed_free"] is True
