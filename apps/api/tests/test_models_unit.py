"""Provider draft verification and profile deletion semantics."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.models import service
from jhin_db.models import ModelProfile, ModelProvider, Workspace
from jhin_models import ModelClient, ModelProviderError
from jhin_observability import noop_metrics, noop_tracer
from jhin_secrets import SecretCrypto


class _FakeClient(ModelClient):
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.closed = False

    async def generate(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def stream(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def verify(self) -> str:
        if self.fail:
            raise ModelProviderError("openai: HTTP 401: invalid key", status_code=401)
        return "ok: 3 models visible"

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("fail", [False, True])
async def test_verify_draft_checks_credentials_without_persisting(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    seen: list[tuple[str, str | None, str | None]] = []
    client = _FakeClient(fail=fail)

    def build(provider_type, *, base_url, api_key, metrics, tracer, admin_api_key=None):  # type: ignore[no-untyped-def]
        seen.append((provider_type, base_url, api_key))
        return client

    monkeypatch.setattr(service, "build_model_client", build)
    ok, detail = await service.verify_draft(
        session,
        crypto,
        admin_ctx,
        provider_type="openai",
        base_url=None,
        api_key="sk-test",
        secret_id=None,
        metrics=noop_metrics(),
        tracer=noop_tracer(),
    )
    assert seen == [("openai", None, "sk-test")]
    assert client.closed
    assert ok is not fail
    assert ("ok:" in detail) is not fail
    providers = await service.list_providers(session, admin_ctx.workspace_id)
    assert providers == []


async def test_deleting_the_default_profile_clears_the_workspace_default(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    from uuid import uuid4

    from jhin_domain import new_uuid7

    provider = ModelProvider(
        workspace_id=admin_ctx.workspace_id, type="openai", display_name="OpenAI"
    )
    session.add(provider)
    await session.flush()
    profile = ModelProfile(
        workspace_id=admin_ctx.workspace_id,
        provider_id=provider.id,
        model_name="gpt-5-mini",
        display_name="Mini",
    )
    session.add(profile)
    await session.flush()
    workspace = await session.get(Workspace, admin_ctx.workspace_id)
    assert workspace is not None
    workspace.default_model_profile_id = profile.id
    await session.commit()

    await service.delete_profile(
        session, admin_ctx, profile.id, request_id=new_uuid7(), ip_hash=str(uuid4())
    )
    await session.refresh(workspace)
    assert workspace.default_model_profile_id is None
    assert await session.get(ModelProfile, profile.id) is None


async def test_delete_provider_clears_default_but_refuses_when_agents_use_it(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    from uuid import uuid4

    from fastapi import HTTPException

    from jhin_db.models import Agent
    from jhin_domain import new_uuid7

    ws = admin_ctx.workspace_id
    provider = ModelProvider(workspace_id=ws, type="openai", display_name="OpenAI")
    session.add(provider)
    await session.flush()
    profile = ModelProfile(
        workspace_id=ws, provider_id=provider.id, model_name="gpt-5-mini", display_name="Mini"
    )
    session.add(profile)
    await session.flush()
    workspace = await session.get(Workspace, ws)
    assert workspace is not None
    workspace.default_model_profile_id = profile.id
    agent = Agent(workspace_id=ws, name="Bisby", slug="bisby", model_profile_id=profile.id)
    session.add(agent)
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await service.delete_provider(
            session, admin_ctx, provider.id, request_id=new_uuid7(), ip_hash=str(uuid4())
        )
    assert excinfo.value.status_code == 409
    assert "Bisby" in str(excinfo.value.detail)

    # Unpinning the agent does not make the provider safe to delete: an agent
    # with no model of its own runs on the workspace default, which is about
    # to disappear with the provider's profiles.
    agent.model_profile_id = None
    await session.commit()
    with pytest.raises(HTTPException) as fallback_failure:
        await service.delete_provider(
            session, admin_ctx, provider.id, request_id=new_uuid7(), ip_hash=str(uuid4())
        )
    assert fallback_failure.value.status_code == 409
    assert "Bisby" in str(fallback_failure.value.detail)

    # With no agent left to strand, the last provider goes and the default
    # empties with it.
    await session.delete(agent)
    await session.commit()
    await service.delete_provider(
        session, admin_ctx, provider.id, request_id=new_uuid7(), ip_hash=str(uuid4())
    )
    await session.refresh(workspace)
    assert workspace.default_model_profile_id is None
    assert await session.get(ModelProvider, provider.id) is None


async def test_deleting_a_provider_hands_the_default_to_a_surviving_model(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """The default follows the profiles that cascade away with the provider."""
    from uuid import uuid4

    from jhin_db.models import Agent
    from jhin_domain import new_uuid7

    ws = admin_ctx.workspace_id
    leaving = ModelProvider(workspace_id=ws, type="openai", display_name="OpenAI")
    staying = ModelProvider(workspace_id=ws, type="anthropic", display_name="Anthropic")
    session.add_all([leaving, staying])
    await session.flush()
    default = ModelProfile(
        workspace_id=ws, provider_id=leaving.id, model_name="gpt-5-mini", display_name="Mini"
    )
    survivor = ModelProfile(
        workspace_id=ws, provider_id=staying.id, model_name="claude", display_name="Claude"
    )
    session.add_all([default, survivor])
    await session.flush()
    workspace = await session.get(Workspace, ws)
    assert workspace is not None
    workspace.default_model_profile_id = default.id
    session.add(Agent(workspace_id=ws, name="Bisby", slug="bisby", model_profile_id=None))
    await session.commit()

    await service.delete_provider(
        session, admin_ctx, leaving.id, request_id=new_uuid7(), ip_hash=str(uuid4())
    )
    await session.refresh(workspace)
    assert workspace.default_model_profile_id == survivor.id


async def test_first_profile_becomes_the_workspace_default(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """A workspace with no default cannot run any agent, so the first profile
    adopts the empty slot — and a second one never displaces it."""
    from uuid import uuid4

    from jhin_domain import new_uuid7

    ws = admin_ctx.workspace_id
    workspace = await session.get(Workspace, ws)
    assert workspace is not None
    assert workspace.default_model_profile_id is None

    provider = ModelProvider(workspace_id=ws, type="openai", display_name="OpenAI")
    session.add(provider)
    await session.flush()

    first = await service.create_profile(
        session,
        admin_ctx,
        values={"provider_id": provider.id, "model_name": "gpt-5-mini", "display_name": "First"},
        request_id=new_uuid7(),
        ip_hash=str(uuid4()),
    )
    await session.refresh(workspace)
    assert workspace.default_model_profile_id == first.id

    second = await service.create_profile(
        session,
        admin_ctx,
        values={"provider_id": provider.id, "model_name": "gpt-5", "display_name": "Second"},
        request_id=new_uuid7(),
        ip_hash=str(uuid4()),
    )
    await session.refresh(workspace)
    assert workspace.default_model_profile_id == first.id
    assert second.id != first.id


async def test_deleting_the_default_model_promotes_another_one(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """Agents without a model of their own run on the workspace default, so
    the slot is handed on rather than emptied while something can fill it."""
    from uuid import uuid4

    from jhin_db.models import Agent
    from jhin_domain import new_uuid7

    ws = admin_ctx.workspace_id
    provider = ModelProvider(workspace_id=ws, type="openai", display_name="OpenAI")
    session.add(provider)
    await session.flush()
    default = ModelProfile(
        workspace_id=ws, provider_id=provider.id, model_name="gpt-5-mini", display_name="Mini"
    )
    spare = ModelProfile(
        workspace_id=ws, provider_id=provider.id, model_name="gpt-5", display_name="Full"
    )
    session.add_all([default, spare])
    await session.flush()
    workspace = await session.get(Workspace, ws)
    assert workspace is not None
    workspace.default_model_profile_id = default.id
    session.add(Agent(workspace_id=ws, name="Bisby", slug="bisby", model_profile_id=None))
    await session.commit()

    await service.delete_profile(
        session, admin_ctx, default.id, request_id=new_uuid7(), ip_hash=str(uuid4())
    )
    await session.refresh(workspace)
    assert workspace.default_model_profile_id == spare.id
    assert await session.get(ModelProfile, default.id) is None


async def test_deleting_the_last_model_is_refused_while_agents_rely_on_it(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """The old guard only caught agents *pinned* to the profile. An agent that
    falls back to the workspace default is just as dependent on it, and losing
    the only model left it failing at its next run instead of saying so here."""
    from uuid import uuid4

    from fastapi import HTTPException

    from jhin_db.models import Agent
    from jhin_domain import new_uuid7

    ws = admin_ctx.workspace_id
    provider = ModelProvider(workspace_id=ws, type="openai", display_name="OpenAI")
    session.add(provider)
    await session.flush()
    only = ModelProfile(
        workspace_id=ws, provider_id=provider.id, model_name="gpt-5-mini", display_name="Mini"
    )
    session.add(only)
    await session.flush()
    workspace = await session.get(Workspace, ws)
    assert workspace is not None
    workspace.default_model_profile_id = only.id
    session.add(Agent(workspace_id=ws, name="Bisby", slug="bisby", model_profile_id=None))
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await service.delete_profile(
            session, admin_ctx, only.id, request_id=new_uuid7(), ip_hash=str(uuid4())
        )
    assert excinfo.value.status_code == 409
    detail = str(excinfo.value.detail)
    assert "Bisby" in detail
    assert "Add another model first" in detail
    assert await session.get(ModelProfile, only.id) is not None
    await session.refresh(workspace)
    assert workspace.default_model_profile_id == only.id


async def test_spend_breakdown_still_adds_up_after_a_model_is_deleted(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """A deleted model's runs keep their cost, so the money has to keep a home.

    Without the deleted-model bucket the provider breakdown quietly stopped
    summing to the reported total, and nothing said the difference existed.
    """
    from uuid import uuid4

    from jhin_db.models import Agent, AgentRun
    from jhin_domain import new_uuid7

    ws = admin_ctx.workspace_id
    provider = ModelProvider(workspace_id=ws, type="openai", display_name="OpenAI")
    session.add(provider)
    await session.flush()
    kept = ModelProfile(
        workspace_id=ws, provider_id=provider.id, model_name="gpt-5", display_name="Full"
    )
    doomed = ModelProfile(
        workspace_id=ws, provider_id=provider.id, model_name="gpt-5-mini", display_name="Mini"
    )
    session.add_all([kept, doomed])
    await session.flush()
    agent = Agent(workspace_id=ws, name="Bisby", slug="bisby", model_profile_id=kept.id)
    session.add(agent)
    await session.flush()
    session.add_all(
        [
            AgentRun(
                workspace_id=ws,
                agent_id=agent.id,
                model_profile_id=kept.id,
                estimated_cost_micros=1_500_000,
            ),
            AgentRun(
                workspace_id=ws,
                agent_id=agent.id,
                model_profile_id=doomed.id,
                estimated_cost_micros=2_500_000,
            ),
        ]
    )
    await session.commit()

    before = await service.get_workspace_spend(session, ws)
    assert before.spent_total_micros == 4_000_000
    assert before.deleted_model_total_micros == 0

    await service.delete_profile(
        session, admin_ctx, doomed.id, request_id=new_uuid7(), ip_hash=str(uuid4())
    )

    after = await service.get_workspace_spend(session, ws)
    assert after.spent_total_micros == 4_000_000
    assert after.deleted_model_total_micros == 2_500_000
    assert after.deleted_model_month_micros == 2_500_000
    by_provider = sum(entry.spent_total_micros for entry in after.providers)
    assert by_provider + after.deleted_model_total_micros == after.spent_total_micros
