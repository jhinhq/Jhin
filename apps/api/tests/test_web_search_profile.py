"""Profile validation for model-native web search (docs/architecture/web.md)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.models import service
from jhin_api.models.schemas import ModelProfileCreate, validate_profile_config
from jhin_db.models import ModelProvider
from jhin_domain import new_uuid7


async def _provider(
    session: AsyncSession, ctx: WorkspaceContext, provider_type: str
) -> ModelProvider:
    provider = ModelProvider(
        workspace_id=ctx.workspace_id, type=provider_type, display_name=f"P-{provider_type}"
    )
    session.add(provider)
    await session.flush()
    return provider


def _values(
    provider: ModelProvider, model_name: str, config: dict[str, object]
) -> dict[str, object]:
    return {
        "provider_id": provider.id,
        "model_name": model_name,
        "display_name": f"{model_name} profile",
        "config_json": config,
    }


async def test_create_rejects_web_search_on_unsupported_models(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    provider = await _provider(session, admin_ctx, "openai")
    with pytest.raises(HTTPException) as info:
        await service.create_profile(
            session,
            admin_ctx,
            values=_values(provider, "gpt-4o-mini", {"web_search": {"enabled": True}}),
            request_id=new_uuid7(),
            ip_hash="test",
        )
    assert info.value.status_code == 400
    assert "search-preview" in info.value.detail


async def test_create_accepts_web_search_where_supported(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    openai = await _provider(session, admin_ctx, "openai")
    profile = await service.create_profile(
        session,
        admin_ctx,
        values=_values(
            openai, "gpt-4o-mini-search-preview", {"web_search": {"enabled": True, "max_uses": 3}}
        ),
        request_id=new_uuid7(),
        ip_hash="test",
    )
    assert profile.config_json["web_search"]["enabled"] is True

    anthropic = await _provider(session, admin_ctx, "anthropic")
    created = await service.create_profile(
        session,
        admin_ctx,
        values=_values(anthropic, "claude-x", {"web_search": {"enabled": True}}),
        request_id=new_uuid7(),
        ip_hash="test",
    )
    assert created.config_json["web_search"]["enabled"] is True


async def test_update_rejects_switching_to_an_unsupported_model(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    provider = await _provider(session, admin_ctx, "openai")
    profile = await service.create_profile(
        session,
        admin_ctx,
        values=_values(provider, "gpt-4o-mini-search-preview", {"web_search": {"enabled": True}}),
        request_id=new_uuid7(),
        ip_hash="test",
    )
    with pytest.raises(HTTPException) as info:
        await service.update_profile(
            session,
            admin_ctx,
            profile.id,
            changes={"model_name": "gpt-4o-mini"},
            request_id=new_uuid7(),
            ip_hash="test",
        )
    assert info.value.status_code == 400

    # Disabling the flag alongside the model change is fine.
    updated = await service.update_profile(
        session,
        admin_ctx,
        profile.id,
        changes={"model_name": "gpt-4o-mini", "config_json": {}},
        request_id=new_uuid7(),
        ip_hash="test",
    )
    assert updated.model_name == "gpt-4o-mini"


def test_schema_validates_the_web_search_block_shape() -> None:
    with pytest.raises(ValueError, match=r"config_json\.web_search"):
        validate_profile_config({"web_search": "yes"})
    with pytest.raises(ValueError, match="max_uses"):
        validate_profile_config({"web_search": {"enabled": True, "max_uses": 0}})
    assert validate_profile_config({"web_search": {"enabled": True, "max_uses": 5}})

    payload = ModelProfileCreate(
        provider_id=new_uuid7(),
        model_name="claude-x",
        display_name="X",
        config_json={"web_search": {"enabled": True}},
    )
    assert payload.config_json["web_search"]["enabled"] is True
