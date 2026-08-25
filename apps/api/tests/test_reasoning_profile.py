"""Profile validation for `config_json.reasoning` (docs/architecture/models.md)."""

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
    provider: ModelProvider,
    model_name: str,
    config: dict[str, object],
    *,
    supports_reasoning: bool = False,
) -> dict[str, object]:
    return {
        "provider_id": provider.id,
        "model_name": model_name,
        "display_name": f"{model_name} profile",
        "config_json": config,
        "supports_reasoning": supports_reasoning,
    }


def test_schema_rejects_unknown_effort_values() -> None:
    with pytest.raises(ValueError, match=r"config_json\.reasoning"):
        validate_profile_config({"reasoning": "none"})
    with pytest.raises(ValueError, match=r"config_json\.reasoning"):
        validate_profile_config({"reasoning": {"effort": "extreme"}})
    # "minimal" is a real OpenAI value that current reasoning models reject,
    # so profiles may not pin it.
    with pytest.raises(ValueError, match=r"config_json\.reasoning"):
        validate_profile_config({"reasoning": {"effort": "minimal"}})

    for effort in ("none", "low", "medium", "high"):
        assert validate_profile_config({"reasoning": {"effort": effort}})
    assert validate_profile_config({"reasoning": {"effort": None}}) == {
        "reasoning": {"effort": None}
    }

    payload = ModelProfileCreate(
        provider_id=new_uuid7(),
        model_name="gpt-5.6-terra",
        display_name="X",
        config_json={"reasoning": {"effort": "none"}},
    )
    assert payload.config_json["reasoning"]["effort"] == "none"


async def test_create_accepts_an_effort_on_a_reasoning_model(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    provider = await _provider(session, admin_ctx, "openai")
    profile = await service.create_profile(
        session,
        admin_ctx,
        values=_values(provider, "gpt-5.6-terra", {"reasoning": {"effort": "none"}}),
        request_id=new_uuid7(),
        ip_hash="test",
    )
    assert profile.config_json["reasoning"]["effort"] == "none"


async def test_create_rejects_an_effort_on_a_non_reasoning_model(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    provider = await _provider(session, admin_ctx, "openai")
    with pytest.raises(HTTPException) as info:
        await service.create_profile(
            session,
            admin_ctx,
            values=_values(provider, "gpt-4o-mini", {"reasoning": {"effort": "high"}}),
            request_id=new_uuid7(),
            ip_hash="test",
        )
    assert info.value.status_code == 400
    assert "not a reasoning model" in info.value.detail


async def test_supports_reasoning_flag_unlocks_an_unrecognized_model(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    provider = await _provider(session, admin_ctx, "openai_compatible")
    profile = await service.create_profile(
        session,
        admin_ctx,
        values=_values(
            provider, "mystery-r1", {"reasoning": {"effort": "low"}}, supports_reasoning=True
        ),
        request_id=new_uuid7(),
        ip_hash="test",
    )
    assert profile.config_json["reasoning"]["effort"] == "low"


async def test_create_rejects_an_effort_on_a_provider_without_one(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    provider = await _provider(session, admin_ctx, "anthropic")
    with pytest.raises(HTTPException) as info:
        await service.create_profile(
            session,
            admin_ctx,
            values=_values(provider, "claude-sonnet-4-5", {"reasoning": {"effort": "low"}}),
            request_id=new_uuid7(),
            ip_hash="test",
        )
    assert info.value.status_code == 400
    assert "does not accept a reasoning effort" in info.value.detail


async def test_update_rejects_switching_to_a_non_reasoning_model(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    provider = await _provider(session, admin_ctx, "openai")
    profile = await service.create_profile(
        session,
        admin_ctx,
        values=_values(provider, "gpt-5-mini", {"reasoning": {"effort": "low"}}),
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

    updated = await service.update_profile(
        session,
        admin_ctx,
        profile.id,
        changes={"model_name": "gpt-4o-mini", "config_json": {}},
        request_id=new_uuid7(),
        ip_hash="test",
    )
    assert updated.model_name == "gpt-4o-mini"
