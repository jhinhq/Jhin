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

    def build(provider_type, *, base_url, api_key, metrics, tracer):  # type: ignore[no-untyped-def]
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
