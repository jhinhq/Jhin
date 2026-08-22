"""Embedding wiring: profile selection order, best-effort record/query
embedding with a stub client, failure leaving records untouched, and the
idempotent ``embed_missing`` backfill — all against SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, MemoryRecord, ModelProfile, ModelProvider, Team, Workspace
from jhin_domain import MemoryStatus, new_uuid7
from jhin_memory import (
    MemoryEmbedder,
    build_memory_context,
    resolve_memory_embedder,
    select_embedding_profile,
)
from jhin_models import (
    EmbeddingConfig,
    EmbeddingResult,
    ModelClient,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from jhin_models.testing import deterministic_embedding
from jhin_observability import noop_metrics

EMBED_CONFIG = {"embeddings": {"enabled": True, "model": "fake-embed", "dimensions": 8}}


class StubEmbeddingClient(ModelClient):
    provider_name = "stub"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []
        self.closed = False

    async def embed(
        self, texts: Sequence[str], *, model: str, dimensions: int | None = None
    ) -> EmbeddingResult:
        self.calls.append(list(texts))
        if self.fail:
            raise ModelProviderError("stub: boom", status_code=500, retryable=True)
        dims = dimensions or 8
        return EmbeddingResult(
            vectors=tuple(tuple(deterministic_embedding(t, dimensions=dims)) for t in texts),
            model=model,
            dimensions=dims,
            usage=ModelUsage(input_tokens=7 * len(texts)),
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError

    async def verify(self) -> str:  # pragma: no cover
        return "ok"

    async def close(self) -> None:
        self.closed = True


class World:
    workspace: Workspace
    agent: Agent
    provider: ModelProvider
    chat_profile: ModelProfile
    embed_profile: ModelProfile


async def _profile(
    session: AsyncSession, w: World, name: str, config: dict[str, Any] | None = None
) -> ModelProfile:
    profile = ModelProfile(
        workspace_id=w.workspace.id,
        provider_id=w.provider.id,
        display_name=name,
        model_name=name,
        config_json=config or {},
    )
    session.add(profile)
    await session.flush()
    return profile


@pytest.fixture
async def w(session: AsyncSession) -> World:
    w = World()
    w.workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
    session.add(w.workspace)
    await session.flush()
    w.provider = ModelProvider(
        workspace_id=w.workspace.id,
        type="openai_compatible",
        display_name="fake",
        base_url="http://fake/v1",
        enabled=True,
    )
    session.add(w.provider)
    await session.flush()
    w.chat_profile = await _profile(session, w, "chat")
    w.embed_profile = await _profile(session, w, "embed", EMBED_CONFIG)
    team = Team(workspace_id=w.workspace.id, name="Eng")
    session.add(team)
    await session.flush()
    w.agent = Agent(workspace_id=w.workspace.id, name="Me", slug="me", team_id=team.id)
    session.add(w.agent)
    await session.flush()
    return w


def embedder(client: StubEmbeddingClient) -> MemoryEmbedder:
    return MemoryEmbedder(
        client,
        config=EmbeddingConfig.model_validate(EMBED_CONFIG["embeddings"]),
        provider_type="openai_compatible",
        metrics=noop_metrics(),
    )


async def seed(session: AsyncSession, w: World, content: str, **overrides: Any) -> MemoryRecord:
    values: dict[str, Any] = {
        "workspace_id": w.workspace.id,
        "scope": "agent",
        "scope_id": w.agent.id,
        "kind": "fact",
        "content": content,
        "content_hash": new_uuid7().hex,
        "visibility": "agent",
        "status": MemoryStatus.ACTIVE.value,
        "created_by_type": "agent",
        "created_by_id": w.agent.id,
    }
    values.update(overrides)
    record = MemoryRecord(**values)
    session.add(record)
    await session.flush()
    return record


class TestProfileSelection:
    async def test_any_enabled_profile_when_agent_and_default_lack_it(
        self, session: AsyncSession, w: World
    ) -> None:
        w.agent.model_profile_id = w.chat_profile.id
        w.workspace.default_model_profile_id = w.chat_profile.id
        await session.flush()
        selected = await select_embedding_profile(session, w.workspace.id, w.agent.id)
        assert selected is not None
        assert selected.profile.id == w.embed_profile.id
        assert selected.config.model == "fake-embed"

    async def test_agent_profile_wins_then_workspace_default(
        self, session: AsyncSession, w: World
    ) -> None:
        agent_profile = await _profile(
            session, w, "agent-embed", {"embeddings": {"enabled": True, "model": "agent-model"}}
        )
        default_profile = await _profile(
            session, w, "default-embed", {"embeddings": {"enabled": True, "model": "ws-model"}}
        )
        w.workspace.default_model_profile_id = default_profile.id
        await session.flush()
        selected = await select_embedding_profile(session, w.workspace.id, w.agent.id)
        assert selected is not None and selected.config.model == "ws-model"
        w.agent.model_profile_id = agent_profile.id
        await session.flush()
        selected = await select_embedding_profile(session, w.workspace.id, w.agent.id)
        assert selected is not None and selected.config.model == "agent-model"

    async def test_disabled_provider_or_config_is_skipped(
        self, session: AsyncSession, w: World
    ) -> None:
        w.provider.enabled = False
        await session.flush()
        assert await select_embedding_profile(session, w.workspace.id, w.agent.id) is None
        w.provider.enabled = True
        w.embed_profile.config_json = {"embeddings": {"enabled": False, "model": "x"}}
        await session.flush()
        assert await select_embedding_profile(session, w.workspace.id, w.agent.id) is None

    async def test_other_workspace_profiles_are_invisible(
        self, session: AsyncSession, w: World
    ) -> None:
        other = Workspace(name="O", slug=f"o-{new_uuid7().hex[:8]}")
        session.add(other)
        await session.flush()
        assert await select_embedding_profile(session, other.id, None) is None

    async def test_resolve_builds_an_embedder_for_openai_compatible(
        self, session: AsyncSession, w: World
    ) -> None:
        resolved = await resolve_memory_embedder(session, None, workspace_id=w.workspace.id)
        assert resolved is not None
        assert resolved.model == "fake-embed" and resolved.dimensions == 8
        await resolved.close()

    async def test_resolve_is_none_for_unsupported_provider(
        self, session: AsyncSession, w: World
    ) -> None:
        w.provider.type = "anthropic"
        w.provider.base_url = None
        await session.flush()
        # No secret → anthropic config error → swallowed as "no embedder".
        assert await resolve_memory_embedder(session, None, workspace_id=w.workspace.id) is None


class TestEmbedRecords:
    async def test_records_get_embeddings_and_usage_is_counted(
        self, session: AsyncSession, w: World
    ) -> None:
        client = StubEmbeddingClient()
        records = [await seed(session, w, "alpha"), await seed(session, w, "beta")]
        count = await embedder(client).embed_records(session, records, workspace_id=w.workspace.id)
        assert count == 2
        assert client.calls == [["alpha", "beta"]]
        for record in records:
            assert record.embedding_model == "fake-embed"
            assert record.embedding_dimensions == 8
            assert record.embedding_json == deterministic_embedding(record.content, dimensions=8)

    async def test_failure_leaves_records_without_embeddings(
        self, session: AsyncSession, w: World
    ) -> None:
        record = await seed(session, w, "alpha")
        count = await embedder(StubEmbeddingClient(fail=True)).embed_records(
            session, [record], workspace_id=w.workspace.id
        )
        assert count == 0
        assert record.embedding_json is None and record.embedding_model is None

    async def test_non_live_or_empty_records_are_skipped(
        self, session: AsyncSession, w: World
    ) -> None:
        client = StubEmbeddingClient()
        proposed = await seed(session, w, "alpha", status=MemoryStatus.PROPOSED.value)
        count = await embedder(client).embed_records(
            session, [proposed], workspace_id=w.workspace.id
        )
        assert count == 0 and client.calls == []

    async def test_query_embedding_and_degraded_flag(self, session: AsyncSession, w: World) -> None:
        record = await seed(session, w, "we deploy on friday")
        ok = embedder(StubEmbeddingClient())
        await ok.embed_records(session, [record], workspace_id=w.workspace.id)
        vector = await ok.embed_query("deploy day", workspace_id=w.workspace.id)
        assert vector is not None and len(vector) == 8
        assert await ok.embed_query("   ", workspace_id=w.workspace.id) is None
        assert (
            await embedder(StubEmbeddingClient(fail=True)).embed_query(
                "deploy day", workspace_id=w.workspace.id
            )
            is None
        )

        hybrid = await build_memory_context(
            session,
            workspace_id=w.workspace.id,
            agent_id=w.agent.id,
            query="deploy day",
            query_embedding=vector,
            embedding_model=ok.model,
        )
        assert hybrid.provenance.mode == "hybrid"
        assert hybrid.provenance.degraded is False
        assert hybrid.provenance.policy["semantic_scored"] == 1
        lexical = await build_memory_context(
            session, workspace_id=w.workspace.id, agent_id=w.agent.id, query="deploy day"
        )
        assert lexical.provenance.mode == "lexical"
        assert lexical.provenance.degraded is True


class TestBackfill:
    async def test_embed_missing_is_bounded_and_idempotent(
        self, session: AsyncSession, w: World
    ) -> None:
        for i in range(5):
            await seed(session, w, f"note {i}")
        stale = await seed(
            session,
            w,
            "stale",
            embedding_json=[1.0] * 8,
            embedding_model="old-model",
            embedding_dimensions=8,
        )
        await seed(session, w, "", status=MemoryStatus.FORGOTTEN.value)
        await seed(session, w, "proposed", status=MemoryStatus.PROPOSED.value)
        client = StubEmbeddingClient()
        backfill = embedder(client)

        embedded, remaining = await backfill.embed_missing(
            session, workspace_id=w.workspace.id, limit=4
        )
        assert (embedded, remaining) == (4, 2)
        embedded, remaining = await backfill.embed_missing(
            session, workspace_id=w.workspace.id, limit=100
        )
        assert (embedded, remaining) == (2, 0)
        assert stale.embedding_model == "fake-embed"
        embedded, remaining = await backfill.embed_missing(session, workspace_id=w.workspace.id)
        assert (embedded, remaining) == (0, 0)
        assert len(client.calls) == 2

        rows = list(
            await session.scalars(
                select(MemoryRecord).where(MemoryRecord.embedding_model == "fake-embed")
            )
        )
        assert len(rows) == 6
        forgotten_ids: list[UUID] = [
            r.id
            for r in await session.scalars(
                select(MemoryRecord).where(MemoryRecord.status == MemoryStatus.FORGOTTEN.value)
            )
        ]
        assert forgotten_ids and all(r.id not in forgotten_ids for r in rows)

    async def test_embed_missing_failure_changes_nothing(
        self, session: AsyncSession, w: World
    ) -> None:
        await seed(session, w, "note")
        embedded, remaining = await embedder(StubEmbeddingClient(fail=True)).embed_missing(
            session, workspace_id=w.workspace.id
        )
        assert (embedded, remaining) == (0, 1)
