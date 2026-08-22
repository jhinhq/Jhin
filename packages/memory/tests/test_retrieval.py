"""Hybrid retrieval against SQLite: live authorization, exclusions, caps,
provenance, and the lexical fallback path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AgentTeamMembership, MemoryRecord, RunEvent, Team, Workspace
from jhin_domain import ActorType, MemoryScope, MemoryStatus, new_uuid7
from jhin_memory import (
    ActorFacts,
    MemoryCandidate,
    SourceFacts,
    apply_candidates,
    build_memory_context,
    forget_record,
    record_retrieval_provenance,
    set_embedding,
)
from jhin_memory.retrieval import MEMORY_RETRIEVED_EVENT
from jhin_models.testing import deterministic_embedding

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class World:
    workspace: Workspace
    team: Team
    other_team: Team
    me: Agent
    other: Agent


@pytest.fixture
async def w(session: AsyncSession) -> World:
    w = World()
    w.workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
    session.add(w.workspace)
    await session.flush()
    w.team = Team(workspace_id=w.workspace.id, name="Eng")
    w.other_team = Team(workspace_id=w.workspace.id, name="Sales")
    session.add_all([w.team, w.other_team])
    await session.flush()
    w.me = Agent(workspace_id=w.workspace.id, name="Me", slug="me", team_id=w.team.id)
    w.other = Agent(
        workspace_id=w.workspace.id, name="Other", slug="other", team_id=w.other_team.id
    )
    session.add_all([w.me, w.other])
    await session.flush()
    return w


async def seed(
    session: AsyncSession,
    w: World,
    content: str,
    *,
    scope: MemoryScope = MemoryScope.AGENT,
    scope_id: UUID | None = None,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    created_at: datetime = NOW - timedelta(days=1),
    **overrides: Any,
) -> MemoryRecord:
    if scope_id is None:
        scope_id = {
            MemoryScope.AGENT: w.me.id,
            MemoryScope.TEAM: w.team.id,
            MemoryScope.WORKSPACE: w.workspace.id,
        }[scope]
    record = MemoryRecord(
        workspace_id=w.workspace.id,
        scope=scope.value,
        scope_id=scope_id,
        kind="fact",
        content=content,
        content_hash=new_uuid7().hex,
        visibility=scope.value,
        status=status.value,
        created_by_type="agent",
        created_by_id=w.me.id,
        **{"valid_from": created_at, **overrides},
    )
    session.add(record)
    await session.flush()
    record.created_at = created_at
    await session.flush()
    return record


async def context_ids(session: AsyncSession, w: World, query: str, **kw: Any) -> list[UUID]:
    ctx = await build_memory_context(
        session, workspace_id=w.workspace.id, agent_id=w.me.id, query=query, now=NOW, **kw
    )
    return [item.id for item in ctx.items]


class TestAuthorization:
    async def test_own_team_and_workspace_scopes_are_visible(
        self, session: AsyncSession, w: World
    ) -> None:
        mine = await seed(session, w, "I prefer tabs")
        team = await seed(session, w, "Our team deploys on Tuesday", scope=MemoryScope.TEAM)
        company = await seed(session, w, "The company is remote", scope=MemoryScope.WORKSPACE)
        ids = await context_ids(session, w, "anything")
        assert {mine.id, team.id, company.id} <= set(ids)

    async def test_other_agents_private_and_other_team_memory_are_excluded(
        self, session: AsyncSession, w: World
    ) -> None:
        theirs = await seed(session, w, "Other agent secret pref", scope_id=w.other.id)
        other_team = await seed(
            session, w, "Sales pipeline detail", scope=MemoryScope.TEAM, scope_id=w.other_team.id
        )
        ids = await context_ids(session, w, "secret pipeline")
        assert theirs.id not in ids
        assert other_team.id not in ids

    async def test_team_membership_changes_apply_live(
        self, session: AsyncSession, w: World
    ) -> None:
        sales = await seed(
            session, w, "Sales pipeline detail", scope=MemoryScope.TEAM, scope_id=w.other_team.id
        )
        assert sales.id not in await context_ids(session, w, "pipeline")
        session.add(
            AgentTeamMembership(
                workspace_id=w.workspace.id, agent_id=w.me.id, team_id=w.other_team.id
            )
        )
        await session.flush()
        assert sales.id in await context_ids(session, w, "pipeline")

    async def test_explicit_team_ids_override(self, session: AsyncSession, w: World) -> None:
        team = await seed(session, w, "Our team deploys on Tuesday", scope=MemoryScope.TEAM)
        assert team.id not in await context_ids(session, w, "deploys", team_ids=[])

    async def test_other_workspace_is_invisible(self, session: AsyncSession, w: World) -> None:
        other_ws = Workspace(name="X", slug=f"x-{new_uuid7().hex[:8]}")
        session.add(other_ws)
        await session.flush()
        foreign = await seed(
            session, w, "Foreign company fact", scope=MemoryScope.WORKSPACE, scope_id=other_ws.id
        )
        foreign.workspace_id = other_ws.id
        await session.flush()
        assert foreign.id not in await context_ids(session, w, "foreign")


class TestExclusions:
    @pytest.mark.parametrize(
        "status",
        [
            MemoryStatus.PROPOSED,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.REJECTED,
            MemoryStatus.FORGOTTEN,
        ],
    )
    async def test_non_live_statuses_are_never_injected(
        self, session: AsyncSession, w: World, status: MemoryStatus
    ) -> None:
        record = await seed(session, w, "deploy tuesday", status=status)
        assert record.id not in await context_ids(session, w, "deploy tuesday")

    async def test_contested_is_injected_and_labelled(
        self, session: AsyncSession, w: World
    ) -> None:
        record = await seed(session, w, "deploy tuesday", status=MemoryStatus.CONTESTED)
        ctx = await build_memory_context(
            session, workspace_id=w.workspace.id, agent_id=w.me.id, query="deploy", now=NOW
        )
        assert [i.id for i in ctx.items] == [record.id]
        assert "contested" in ctx.text

    async def test_expired_and_not_yet_valid_are_excluded(
        self, session: AsyncSession, w: World
    ) -> None:
        expired = await seed(session, w, "old", expires_at=NOW - timedelta(seconds=1))
        future = await seed(session, w, "future", valid_from=NOW + timedelta(days=1))
        live = await seed(session, w, "live", expires_at=NOW + timedelta(days=1))
        ids = await context_ids(session, w, "old future live")
        assert expired.id not in ids
        assert future.id not in ids
        assert live.id in ids

    async def test_forgotten_record_is_gone_immediately(
        self, session: AsyncSession, w: World
    ) -> None:
        record = await seed(session, w, "remember this deploy detail")
        await set_embedding(session, record, [0.1, 0.2, 0.3], model="fake-embed")
        assert record.id in await context_ids(session, w, "deploy")
        forgotten = await forget_record(session, record, now=NOW)
        assert forgotten == [record.id]
        assert record.content == ""
        assert record.embedding_json is None
        assert record.status == MemoryStatus.FORGOTTEN.value
        assert record.id not in await context_ids(session, w, "deploy")


class TestRanking:
    async def test_lexical_match_outranks_unrelated(self, session: AsyncSession, w: World) -> None:
        hit = await seed(session, w, "The staging database lives in Frankfurt")
        miss = await seed(session, w, "Varand likes espresso", created_at=NOW)
        ids = await context_ids(session, w, "where is the staging database")
        assert ids.index(hit.id) < ids.index(miss.id)

    async def test_semantic_similarity_is_used_when_embeddings_match(
        self, session: AsyncSession, w: World
    ) -> None:
        near = await seed(session, w, "alpha")
        far = await seed(session, w, "beta", created_at=NOW)
        await set_embedding(session, near, [1.0, 0.0], model="m")
        await set_embedding(session, far, [0.0, 1.0], model="m")
        ctx = await build_memory_context(
            session,
            workspace_id=w.workspace.id,
            agent_id=w.me.id,
            query="zzz",
            query_embedding=[1.0, 0.0],
            now=NOW,
        )
        assert [i.id for i in ctx.items] == [near.id, far.id]
        assert ctx.provenance.mode == "hybrid"
        assert ctx.provenance.degraded is False

    async def test_mismatched_dimensions_are_never_compared(
        self, session: AsyncSession, w: World
    ) -> None:
        record = await seed(session, w, "alpha")
        await set_embedding(session, record, [1.0, 0.0, 0.0], model="m")
        ctx = await build_memory_context(
            session,
            workspace_id=w.workspace.id,
            agent_id=w.me.id,
            query="alpha",
            query_embedding=[1.0, 0.0],
            now=NOW,
        )
        assert [i.id for i in ctx.items] == [record.id]
        # The query was embedded, so the run is not degraded — but nothing
        # could be scored semantically and the provenance says so.
        assert ctx.provenance.mode == "hybrid"
        assert ctx.provenance.degraded is False
        assert ctx.provenance.policy["semantic_scored"] == 0

    async def test_mismatched_model_is_never_compared(
        self, session: AsyncSession, w: World
    ) -> None:
        near = await seed(session, w, "alpha")
        await set_embedding(session, near, [1.0, 0.0], model="old-model")
        ctx = await build_memory_context(
            session,
            workspace_id=w.workspace.id,
            agent_id=w.me.id,
            query="zzz",
            query_embedding=[1.0, 0.0],
            embedding_model="new-model",
            now=NOW,
        )
        assert ctx.provenance.policy["semantic_scored"] == 0
        assert ctx.provenance.policy["embedding_model"] == "new-model"

    async def test_semantic_relation_outranks_lexical_lookalike(
        self, session: AsyncSession, w: World
    ) -> None:
        """With the fake provider's hashed bag-of-words vectors a memory that
        shares vocabulary with the query ranks above one that only shares a
        token the lexical scorer also sees."""
        related = await seed(session, w, "We deploy the api to production every friday afternoon")
        lookalike = await seed(session, w, "The friday lunch order is always pizza", created_at=NOW)
        for record in (related, lookalike):
            await set_embedding(
                session, record, deterministic_embedding(record.content), model="fake-embed"
            )
        query = "when do we deploy to production"
        ids = await context_ids(
            session,
            w,
            query,
            query_embedding=deterministic_embedding(query),
            embedding_model="fake-embed",
        )
        assert ids.index(related.id) < ids.index(lookalike.id)

    async def test_without_embeddings_is_degraded_lexical(
        self, session: AsyncSession, w: World
    ) -> None:
        await seed(session, w, "alpha")
        ctx = await build_memory_context(
            session, workspace_id=w.workspace.id, agent_id=w.me.id, query="alpha", now=NOW
        )
        assert ctx.provenance.mode == "lexical"
        assert ctx.provenance.degraded is True

    async def test_pinned_records_get_a_bonus(self, session: AsyncSession, w: World) -> None:
        plain = await seed(session, w, "plain note", created_at=NOW)
        pinned = await seed(
            session, w, "pinned note", created_at=NOW - timedelta(days=20), pinned_at=NOW
        )
        ids = await context_ids(session, w, "unrelated query")
        assert ids.index(pinned.id) < ids.index(plain.id)


class TestCapsAndProvenance:
    async def test_record_cap(self, session: AsyncSession, w: World) -> None:
        for i in range(6):
            await seed(session, w, f"note {i}")
        ctx = await build_memory_context(
            session,
            workspace_id=w.workspace.id,
            agent_id=w.me.id,
            query="note",
            max_records=3,
            now=NOW,
        )
        assert len(ctx.items) == 3
        assert ctx.provenance.policy["selected"] == 3
        assert ctx.provenance.policy["authorized_candidates"] == 6

    async def test_char_cap_bounds_rendered_text(self, session: AsyncSession, w: World) -> None:
        for i in range(5):
            await seed(session, w, f"note {i} " + "x" * 400)
        ctx = await build_memory_context(
            session,
            workspace_id=w.workspace.id,
            agent_id=w.me.id,
            query="note",
            max_chars=1000,
            now=NOW,
        )
        assert sum(len(i.content) for i in ctx.items) <= 1000
        assert len(ctx.items) <= 3

    async def test_context_hash_is_deterministic_and_selection_sensitive(
        self, session: AsyncSession, w: World
    ) -> None:
        a = await seed(session, w, "alpha")
        first = await build_memory_context(
            session, workspace_id=w.workspace.id, agent_id=w.me.id, query="alpha", now=NOW
        )
        again = await build_memory_context(
            session, workspace_id=w.workspace.id, agent_id=w.me.id, query="alpha", now=NOW
        )
        assert first.provenance.context_hash == again.provenance.context_hash
        await forget_record(session, a, now=NOW)
        after = await build_memory_context(
            session, workspace_id=w.workspace.id, agent_id=w.me.id, query="alpha", now=NOW
        )
        assert after.provenance.context_hash != first.provenance.context_hash
        assert after.text == ""

    async def test_provenance_event_is_content_free(self, session: AsyncSession, w: World) -> None:
        record = await seed(session, w, "very private content")
        ctx = await build_memory_context(
            session, workspace_id=w.workspace.id, agent_id=w.me.id, query="private", now=NOW
        )
        run_id = new_uuid7()
        event = await record_retrieval_provenance(
            session, workspace_id=w.workspace.id, run_id=run_id, task_id=None, context=ctx
        )
        stored = await session.scalar(select(RunEvent).where(RunEvent.id == event.id))
        assert stored is not None
        assert stored.event_type == MEMORY_RETRIEVED_EVENT
        assert stored.seq == 0
        assert stored.payload_json["record_ids"] == [str(record.id)]
        assert stored.payload_json["versions"] == [1]
        assert "very private" not in str(stored.payload_json)
        second = await record_retrieval_provenance(
            session, workspace_id=w.workspace.id, run_id=run_id, task_id=None, context=ctx
        )
        assert second.seq == 1

    async def test_rendered_text_labels_scope_and_source(
        self, session: AsyncSession, w: World
    ) -> None:
        source = SourceFacts(workspace_id=w.workspace.id, agent_id=w.me.id)
        await apply_candidates(
            session,
            candidates=[MemoryCandidate(content="Prefers espresso", kind="preference")],  # type: ignore[arg-type]
            source=source,
            actor=ActorFacts(actor_type=ActorType.AGENT, actor_id=w.me.id),
            now=NOW,
        )
        ctx = await build_memory_context(
            session, workspace_id=w.workspace.id, agent_id=w.me.id, query="espresso", now=NOW
        )
        assert "Recalled memory" in ctx.text
        assert "[preference · your private memory] Prefers espresso" in ctx.text
