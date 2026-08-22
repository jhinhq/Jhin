"""Memory embeddings through the API: explicit "remember" embeds best-effort,
the admin ``embed-missing`` backfill is bounded and idempotent, a workspace
without an embedding profile gets a typed 409, and profile ``config_json``
validation rejects malformed ``embeddings`` blocks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.memory import service
from jhin_api.memory.schemas import EmbedMissingIn, MemoryCreate, MemoryUpdate
from jhin_api.models.schemas import ModelProfileCreate, ModelProfileUpdate
from jhin_db.models import Agent, AuditEvent, MemoryRecord, ModelProfile, ModelProvider, Team, User
from jhin_domain import MemoryStatus, WorkspaceRole, new_uuid7
from jhin_memory import embedding as embedding_module
from jhin_models import (
    EmbeddingResult,
    ModelClient,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
)
from jhin_models.testing import deterministic_embedding


class StubEmbeddingClient(ModelClient):
    provider_name = "stub"
    fail = False
    calls: ClassVar[list[list[str]]] = []

    async def embed(
        self, texts: Sequence[str], *, model: str, dimensions: int | None = None
    ) -> EmbeddingResult:
        StubEmbeddingClient.calls.append(list(texts))
        if StubEmbeddingClient.fail:
            raise ModelProviderError("stub: boom", status_code=500, retryable=True)
        dims = dimensions or 8
        return EmbeddingResult(
            vectors=tuple(tuple(deterministic_embedding(t, dimensions=dims)) for t in texts),
            model=model,
            dimensions=dims,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError

    async def verify(self) -> str:  # pragma: no cover
        return "ok"

    async def close(self) -> None:
        return None


class World:
    agent: Agent
    profile: ModelProfile


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    StubEmbeddingClient.fail = False
    StubEmbeddingClient.calls = []
    monkeypatch.setattr(
        embedding_module, "build_model_client", lambda *args, **kwargs: StubEmbeddingClient()
    )


@pytest.fixture
async def world(session: AsyncSession, admin_ctx: WorkspaceContext) -> World:
    w = World()
    team = Team(workspace_id=admin_ctx.workspace_id, name="Eng")
    session.add(team)
    await session.flush()
    w.agent = Agent(workspace_id=admin_ctx.workspace_id, name="Ava", slug="ava", team_id=team.id)
    provider = ModelProvider(
        workspace_id=admin_ctx.workspace_id,
        type="openai_compatible",
        display_name="fake",
        base_url="http://fake/v1",
        enabled=True,
    )
    session.add_all([w.agent, provider])
    await session.flush()
    w.profile = ModelProfile(
        workspace_id=admin_ctx.workspace_id,
        provider_id=provider.id,
        display_name="embed",
        model_name="embed",
        config_json={"embeddings": {"enabled": True, "model": "fake-embed", "dimensions": 8}},
    )
    session.add(w.profile)
    await session.flush()
    return w


@pytest.fixture
async def member_ctx(session: AsyncSession, admin_ctx: WorkspaceContext) -> WorkspaceContext:
    user = User(email=f"m-{new_uuid7().hex[:6]}@example.com", display_name="M", password_hash="x")
    session.add(user)
    await session.flush()
    return WorkspaceContext(
        user=user, workspace_id=admin_ctx.workspace_id, role=WorkspaceRole.MEMBER
    )


DEPS = service.EmbeddingDeps(crypto=None)


async def test_remember_embeds_best_effort(
    session: AsyncSession, admin_ctx: WorkspaceContext, world: World
) -> None:
    record = await service.create_memory(
        session,
        admin_ctx,
        MemoryCreate(content="Ava prefers concise updates.", agent_id=world.agent.id),
        embedding=DEPS,
    )
    assert record.embedding_model == "fake-embed"
    assert record.embedding_dimensions == 8
    assert StubEmbeddingClient.calls == [["Ava prefers concise updates."]]

    StubEmbeddingClient.fail = True
    failed = await service.create_memory(
        session,
        admin_ctx,
        MemoryCreate(content="Ava dislikes long meetings.", agent_id=world.agent.id),
        embedding=DEPS,
    )
    assert failed.status == MemoryStatus.ACTIVE.value
    assert failed.embedding_json is None

    StubEmbeddingClient.fail = False
    edited = await service.update_memory(
        session,
        admin_ctx,
        failed.id,
        MemoryUpdate(content="Ava dislikes meetings."),
        embedding=DEPS,
    )
    assert edited.embedding_model == "fake-embed"
    audits = list(
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "memory.created"))
    )
    assert sorted(a.metadata_json["embedded"] for a in audits) == [False, True]


async def test_without_deps_nothing_is_embedded(
    session: AsyncSession, admin_ctx: WorkspaceContext, world: World
) -> None:
    record = await service.create_memory(
        session, admin_ctx, MemoryCreate(content="plain", agent_id=world.agent.id)
    )
    assert record.embedding_json is None and StubEmbeddingClient.calls == []


async def test_embed_missing_is_bounded_idempotent_and_audited(
    session: AsyncSession, admin_ctx: WorkspaceContext, world: World
) -> None:
    for i in range(3):
        await service.create_memory(
            session, admin_ctx, MemoryCreate(content=f"note {i}", agent_id=world.agent.id)
        )
    first = await service.embed_missing(session, admin_ctx, embedding=DEPS, limit=2)
    assert first == (2, 1, "fake-embed", 8)
    second = await service.embed_missing(session, admin_ctx, embedding=DEPS, limit=2)
    assert second == (1, 0, "fake-embed", 8)
    third = await service.embed_missing(session, admin_ctx, embedding=DEPS, limit=2)
    assert third == (0, 0, "fake-embed", 8)
    rows = list(await session.scalars(select(MemoryRecord)))
    assert all(r.embedding_model == "fake-embed" for r in rows)
    audits = list(
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "memory.embed_missing"))
    )
    assert [a.metadata_json["embedded"] for a in audits] == [2, 1, 0]
    assert EmbedMissingIn().limit == 100
    with pytest.raises(ValidationError):
        EmbedMissingIn(limit=0)
    with pytest.raises(ValidationError):
        EmbedMissingIn(limit=10_000)


async def test_embed_missing_requires_admin_and_an_embedding_profile(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    member_ctx: WorkspaceContext,
    world: World,
) -> None:
    with pytest.raises(HTTPException) as forbidden:
        await service.embed_missing(session, member_ctx, embedding=DEPS, limit=10)
    assert forbidden.value.status_code == 403
    world.profile.config_json = {}
    await session.flush()
    with pytest.raises(HTTPException) as conflict:
        await service.embed_missing(session, admin_ctx, embedding=DEPS, limit=10)
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "embeddings_unsupported"  # type: ignore[index]


def test_profile_config_validation() -> None:
    base: dict[str, Any] = {
        "provider_id": new_uuid7(),
        "model_name": "m",
        "display_name": "m",
    }
    ok = ModelProfileCreate(
        **base,
        config_json={
            "embeddings": {
                "enabled": True,
                "model": "text-embedding-3-small",
                "dimensions": 512,
                "cost_micros_per_million": 20_000,
            },
            "image_generation": {"enabled": False},
        },
    )
    assert ok.config_json["embeddings"]["dimensions"] == 512
    assert ModelProfileCreate(**base).config_json == {}
    assert ModelProfileUpdate(config_json=None).config_json is None
    for bad in (
        {"embeddings": "yes"},
        {"embeddings": {"enabled": True}},
        {"embeddings": {"enabled": True, "model": "m", "dimensions": 0}},
        {"embeddings": {"enabled": True, "model": "m", "dimensions": 9_000}},
        {"embeddings": {"enabled": True, "model": "m", "cost_micros_per_million": -1}},
    ):
        with pytest.raises(ValidationError, match=r"config_json\.embeddings"):
            ModelProfileCreate(**base, config_json=bad)
        with pytest.raises(ValidationError, match=r"config_json\.embeddings"):
            ModelProfileUpdate(config_json=bad)
