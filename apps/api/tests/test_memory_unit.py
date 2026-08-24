"""Memory management service: RBAC, explicit remember, versioned edits, pin,
contest, forget tombstones, and promotion review — against SQLite."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.memory import service
from jhin_api.memory.schemas import MemoryCreate, MemoryUpdate
from jhin_db.models import Agent, AuditEvent, MemoryRecord, Team, User, Workspace
from jhin_domain import ActorType, MemoryScope, MemoryStatus, WorkspaceRole, new_uuid7
from jhin_memory import (
    ActorFacts,
    AdjudicationPair,
    MemoryCandidate,
    SourceFacts,
    apply_candidates,
    content_hash,
)


class World:
    team: Team
    agent: Agent


@pytest.fixture
async def world(session: AsyncSession, admin_ctx: WorkspaceContext) -> World:
    w = World()
    w.team = Team(workspace_id=admin_ctx.workspace_id, name="Eng")
    session.add(w.team)
    await session.flush()
    w.agent = Agent(workspace_id=admin_ctx.workspace_id, name="Ava", slug="ava", team_id=w.team.id)
    session.add(w.agent)
    await session.flush()
    return w


async def ctx_with_role(
    session: AsyncSession, admin_ctx: WorkspaceContext, role: WorkspaceRole
) -> WorkspaceContext:
    user = User(
        email=f"{role.value}-{new_uuid7().hex[:6]}@example.com", display_name="U", password_hash="x"
    )
    session.add(user)
    await session.flush()
    return WorkspaceContext(user=user, workspace_id=admin_ctx.workspace_id, role=role)


@pytest.fixture
async def member_ctx(session: AsyncSession, admin_ctx: WorkspaceContext) -> WorkspaceContext:
    return await ctx_with_role(session, admin_ctx, WorkspaceRole.MEMBER)


@pytest.fixture
async def viewer_ctx(session: AsyncSession, admin_ctx: WorkspaceContext) -> WorkspaceContext:
    return await ctx_with_role(session, admin_ctx, WorkspaceRole.VIEWER)


def create_payload(world: World, **overrides: Any) -> MemoryCreate:
    values: dict[str, Any] = {"content": "Ava prefers concise updates.", "agent_id": world.agent.id}
    values.update(overrides)
    return MemoryCreate.model_validate(values)


async def audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id))
    return [row.action for row in rows]


async def seed_proposed(
    session: AsyncSession, admin_ctx: WorkspaceContext, world: World
) -> MemoryRecord:
    source = SourceFacts(
        workspace_id=admin_ctx.workspace_id,
        agent_id=world.agent.id,
        visibility=MemoryScope.WORKSPACE,
        team_id=world.team.id,
    )
    result = await apply_candidates(
        session,
        candidates=[
            MemoryCandidate(
                content="The company is fully remote.", requested_scope=MemoryScope.WORKSPACE
            )
        ],
        source=source,
        actor=ActorFacts(actor_type=ActorType.AGENT, actor_id=world.agent.id),
    )
    await session.commit()
    assert result.created[0].status == MemoryStatus.PROPOSED.value
    return result.created[0]


class TestCreate:
    async def test_member_remembers_agent_scope_as_active(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        record = await service.create_memory(session, member_ctx, create_payload(world))
        assert record.status == MemoryStatus.ACTIVE.value
        assert record.scope == "agent"
        assert record.scope_id == world.agent.id
        assert record.created_by_type == "user"
        assert record.created_by_id == member_ctx.user.id
        assert "explicit_remember" in record.policy_json["reasons"]
        assert await audit_actions(session) == ["memory.created"]

    async def test_member_cannot_write_team_or_workspace_scope(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        for scope in (MemoryScope.TEAM, MemoryScope.WORKSPACE):
            with pytest.raises(HTTPException) as exc:
                await service.create_memory(
                    session, member_ctx, create_payload(world, scope=scope, team_id=world.team.id)
                )
            assert exc.value.status_code == 403

    async def test_viewer_cannot_write(
        self, session: AsyncSession, viewer_ctx: WorkspaceContext, world: World
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.create_memory(session, viewer_ctx, create_payload(world))
        assert exc.value.status_code == 403

    async def test_admin_remembers_workspace_scope_as_active(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, world: World
    ) -> None:
        record = await service.create_memory(
            session,
            admin_ctx,
            create_payload(world, scope=MemoryScope.WORKSPACE, agent_id=None, content="Remote."),
        )
        assert record.status == MemoryStatus.ACTIVE.value
        assert record.scope == "workspace"
        assert record.scope_id == admin_ctx.workspace_id

    async def test_admin_remembers_team_scope(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, world: World
    ) -> None:
        record = await service.create_memory(
            session,
            admin_ctx,
            create_payload(world, scope=MemoryScope.TEAM, agent_id=None, team_id=world.team.id),
        )
        assert record.scope == "team"
        assert record.scope_id == world.team.id

    async def test_missing_agent_for_agent_scope(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.create_memory(session, member_ctx, create_payload(world, agent_id=None))
        assert exc.value.status_code == 422

    async def test_unknown_agent_is_404(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.create_memory(
                session, member_ctx, create_payload(world, agent_id=new_uuid7())
            )
        assert exc.value.status_code == 404

    async def test_secret_is_rejected(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.create_memory(
                session,
                member_ctx,
                create_payload(world, content="Token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
            )
        assert exc.value.status_code == 422
        assert (await session.scalar(select(MemoryRecord))) is None

    async def test_duplicate_is_409(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        first = await service.create_memory(session, member_ctx, create_payload(world))
        with pytest.raises(HTTPException) as exc:
            await service.create_memory(
                session, member_ctx, create_payload(world, content="ava prefers concise updates")
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["memory_id"] == str(first.id)  # type: ignore[index]


class TestReadAndList:
    async def test_get_and_cross_workspace_404(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        record = await service.create_memory(session, member_ctx, create_payload(world))
        assert (
            await service.get_memory(session, member_ctx.workspace_id, record.id)
        ).id == record.id
        other = Workspace(name="Other", slug=f"o-{new_uuid7().hex[:8]}")
        session.add(other)
        await session.flush()
        with pytest.raises(HTTPException) as exc:
            await service.get_memory(session, other.id, record.id)
        assert exc.value.status_code == 404

    async def test_list_filters(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, world: World
    ) -> None:
        a = await service.create_memory(session, admin_ctx, create_payload(world))
        t = await service.create_memory(
            session,
            admin_ctx,
            create_payload(
                world,
                scope=MemoryScope.TEAM,
                agent_id=None,
                team_id=world.team.id,
                content="Team ships Tuesdays",
            ),
        )
        proposed = await seed_proposed(session, admin_ctx, world)
        items, total = await service.list_memories(session, admin_ctx.workspace_id)
        assert total == 3
        items, _ = await service.list_memories(
            session, admin_ctx.workspace_id, agent_id=world.agent.id
        )
        assert [i.id for i in items] == [a.id]
        items, _ = await service.list_memories(
            session, admin_ctx.workspace_id, team_id=world.team.id
        )
        assert [i.id for i in items] == [t.id]
        items, _ = await service.list_memories(
            session, admin_ctx.workspace_id, status_filter=MemoryStatus.PROPOSED
        )
        assert [i.id for i in items] == [proposed.id]
        items, _ = await service.list_memories(session, admin_ctx.workspace_id, q="tuesdays")
        assert [i.id for i in items] == [t.id]
        items, _ = await service.list_memories(
            session, admin_ctx.workspace_id, scope=MemoryScope.WORKSPACE
        )
        assert [i.id for i in items] == [proposed.id]

    async def test_forgotten_hidden_by_default(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        record = await service.create_memory(session, member_ctx, create_payload(world))
        await service.forget_memory(session, member_ctx, record.id)
        items, total = await service.list_memories(session, member_ctx.workspace_id)
        assert total == 0 and items == []
        items, _ = await service.list_memories(
            session, member_ctx.workspace_id, status_filter=MemoryStatus.FORGOTTEN
        )
        assert [i.id for i in items] == [record.id]


class TestEdit:
    async def test_edit_creates_new_version(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        old = await service.create_memory(session, member_ctx, create_payload(world))
        new = await service.update_memory(
            session,
            member_ctx,
            old.id,
            MemoryUpdate(content="Ava prefers bullet points.", tags=["style"]),
        )
        assert new.id != old.id
        assert new.version == 2
        assert new.supersedes_id == old.id
        assert new.status == MemoryStatus.ACTIVE.value
        assert new.tags_json == ["style"]
        assert old.status == MemoryStatus.SUPERSEDED.value
        assert (await audit_actions(session))[-1] == "memory.edited"
        with pytest.raises(HTTPException) as exc:
            await service.update_memory(session, member_ctx, old.id, MemoryUpdate(content="again"))
        assert exc.value.status_code == 409

    async def test_edit_rejects_secret(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        old = await service.create_memory(session, member_ctx, create_payload(world))
        with pytest.raises(HTTPException) as exc:
            await service.update_memory(
                session,
                member_ctx,
                old.id,
                MemoryUpdate(content="Authorization: Bearer abcdefghijklmnopqrstuvwxyz"),
            )
        assert exc.value.status_code == 422

    async def test_member_cannot_edit_team_memory(
        self,
        session: AsyncSession,
        admin_ctx: WorkspaceContext,
        member_ctx: WorkspaceContext,
        world: World,
    ) -> None:
        team = await service.create_memory(
            session,
            admin_ctx,
            create_payload(world, scope=MemoryScope.TEAM, agent_id=None, team_id=world.team.id),
        )
        with pytest.raises(HTTPException) as exc:
            await service.update_memory(session, member_ctx, team.id, MemoryUpdate(content="x"))
        assert exc.value.status_code == 403


class TestControls:
    async def test_pin_unpin(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        record = await service.create_memory(session, member_ctx, create_payload(world))
        assert (await service.pin_memory(session, member_ctx, record.id, pinned=True)).pinned_at
        assert (
            await service.pin_memory(session, member_ctx, record.id, pinned=False)
        ).pinned_at is None
        assert (await audit_actions(session))[-2:] == ["memory.pinned", "memory.unpinned"]

    async def test_contest(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        record = await service.create_memory(session, member_ctx, create_payload(world))
        contested = await service.contest_memory(session, member_ctx, record.id, reason="outdated")
        assert contested.status == MemoryStatus.CONTESTED.value
        assert (await audit_actions(session))[-1] == "memory.contested"

    async def test_forget_leaves_content_free_tombstone(
        self, session: AsyncSession, member_ctx: WorkspaceContext, world: World
    ) -> None:
        old = await service.create_memory(session, member_ctx, create_payload(world))
        new = await service.update_memory(
            session, member_ctx, old.id, MemoryUpdate(content="Ava likes tabs.")
        )
        new.embedding_json = [0.1, 0.2]
        await session.flush()
        await service.forget_memory(session, member_ctx, new.id)
        for record in (old, new):
            await session.refresh(record)
            assert record.status == MemoryStatus.FORGOTTEN.value
            assert record.content == ""
            assert record.content_hash == ""
            assert record.embedding_json is None
            assert record.forgotten_at is not None
        events = list(
            await session.scalars(select(AuditEvent).where(AuditEvent.action == "memory.forgotten"))
        )
        assert len(events) == 1
        assert events[0].target_id == new.id
        assert set(events[0].metadata_json["forgotten_ids"]) == {str(old.id), str(new.id)}
        assert "tabs" not in str(events[0].metadata_json)
        assert "concise" not in str(events[0].metadata_json)

    async def test_viewer_cannot_forget(
        self,
        session: AsyncSession,
        member_ctx: WorkspaceContext,
        viewer_ctx: WorkspaceContext,
        world: World,
    ) -> None:
        record = await service.create_memory(session, member_ctx, create_payload(world))
        with pytest.raises(HTTPException) as exc:
            await service.forget_memory(session, viewer_ctx, record.id)
        assert exc.value.status_code == 403


class TestPromotionReview:
    async def test_admin_approves_proposed(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, world: World
    ) -> None:
        proposed = await seed_proposed(session, admin_ctx, world)
        approved = await service.approve_memory(session, admin_ctx, proposed.id)
        assert approved.status == MemoryStatus.ACTIVE.value
        assert approved.policy_json["approved_by_user"] == str(admin_ctx.user.id)
        assert (await audit_actions(session))[-1] == "memory.approved"
        with pytest.raises(HTTPException) as exc:
            await service.approve_memory(session, admin_ctx, proposed.id)
        assert exc.value.status_code == 409

    async def test_member_cannot_approve(
        self,
        session: AsyncSession,
        admin_ctx: WorkspaceContext,
        member_ctx: WorkspaceContext,
        world: World,
    ) -> None:
        proposed = await seed_proposed(session, admin_ctx, world)
        with pytest.raises(HTTPException) as exc:
            await service.approve_memory(session, member_ctx, proposed.id)
        assert exc.value.status_code == 403

    async def test_admin_rejects_proposed(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, world: World
    ) -> None:
        proposed = await seed_proposed(session, admin_ctx, world)
        rejected = await service.reject_memory(session, admin_ctx, proposed.id)
        assert rejected.status == MemoryStatus.REJECTED.value
        assert (await audit_actions(session))[-1] == "memory.rejected"


def _active_record(
    admin_ctx: WorkspaceContext,
    world: World,
    content: str,
    *,
    subject: str | None = None,
    confidence: float = 0.5,
    embedding: list[float] | None = None,
    model: str | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        workspace_id=admin_ctx.workspace_id,
        scope="agent",
        scope_id=world.agent.id,
        kind="fact",
        subject=subject,
        content=content,
        content_hash=content_hash(content),
        visibility="agent",
        confidence=confidence,
        status=MemoryStatus.ACTIVE.value,
        created_by_type="agent",
        created_by_id=world.agent.id,
        embedding_json=embedding,
        embedding_model=model,
    )


class TestDeduplicate:
    async def test_clusters_keep_the_best_and_audit(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, world: World
    ) -> None:
        rows = [
            _active_record(
                admin_ctx,
                world,
                "We deploy every other Thursday.",
                subject="deploy.day",
                confidence=0.6,
            ),
            _active_record(
                admin_ctx,
                world,
                "The release day is every other Thursday.",
                subject="deploy.day",
                confidence=0.5,
            ),
            _active_record(
                admin_ctx,
                world,
                "Release day is every other Thursday.",
                subject="deploy.day",
                confidence=0.9,
            ),
            _active_record(admin_ctx, world, "Varand prefers concise updates."),
        ]
        session.add_all(rows)
        await session.flush()

        clusters, superseded, remaining, adjudicated, llm = await service.deduplicate_memories(
            session, admin_ctx
        )
        assert (clusters, superseded, remaining) == (1, 2, 2)
        assert (adjudicated, llm) == (0, False)  # no deps → no smart matching
        keeper = await session.get(MemoryRecord, rows[2].id)
        assert keeper is not None
        assert keeper.status == MemoryStatus.ACTIVE.value  # highest confidence wins
        assert keeper.policy_json["confirmations"] == 2
        for loser_row in (rows[0], rows[1]):
            refreshed = await session.get(MemoryRecord, loser_row.id)
            assert refreshed is not None
            assert refreshed.status == MemoryStatus.SUPERSEDED.value
            assert refreshed.policy_json["deduplicated_into"] == str(rows[2].id)
            assert refreshed.content  # history is kept, not tombstoned
        untouched = await session.get(MemoryRecord, rows[3].id)
        assert untouched is not None and untouched.status == MemoryStatus.ACTIVE.value
        assert (await audit_actions(session))[-1] == "memory.deduplicated"
        # Idempotent: a second pass has nothing left to merge.
        assert await service.deduplicate_memories(session, admin_ctx) == (0, 0, 2, 0, False)

    async def test_semantic_cluster_via_stored_embeddings(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, world: World
    ) -> None:
        rows = [
            _active_record(
                admin_ctx,
                world,
                "Shipping cadence is biweekly.",
                confidence=0.8,
                embedding=[1.0, 0.0],
                model="m",
            ),
            _active_record(
                admin_ctx,
                world,
                "We release every second week.",
                confidence=0.5,
                embedding=[0.99, 0.1],
                model="m",
            ),
        ]
        session.add_all(rows)
        await session.flush()
        clusters, superseded, remaining, _, _ = await service.deduplicate_memories(
            session, admin_ctx
        )
        assert (clusters, superseded, remaining) == (1, 1, 1)
        keeper = await session.get(MemoryRecord, rows[0].id)
        assert keeper is not None and keeper.status == MemoryStatus.ACTIVE.value

    async def test_member_cannot_deduplicate(
        self, session: AsyncSession, member_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.deduplicate_memories(session, member_ctx)
        assert exc.value.status_code == 403


class WeekdayAdjudicator:
    """SAME when both statements name the same weekday, DIFFERENT otherwise —
    the deterministic analogue of the fake provider's adjudication rule."""

    def __init__(self) -> None:
        self.pairs: list[AdjudicationPair] = []
        self.closed = False

    async def adjudicate(
        self, pairs: Sequence[AdjudicationPair], *, workspace_id: UUID
    ) -> list[bool]:
        self.pairs.extend(pairs)
        days = ("monday", "tuesday", "wednesday", "thursday", "friday")
        verdicts: list[bool] = []
        for pair in pairs:
            values_a = {d for d in days if d in pair.content_a.casefold()}
            values_b = {d for d in days if d in pair.content_b.casefold()}
            verdicts.append(bool(values_a) and values_a == values_b)
        return verdicts

    async def close(self) -> None:
        self.closed = True


class TestDeduplicateAdjudication:
    """Gray-zone pairs the rule cannot match are settled by the workspace
    default chat model (monkeypatched resolver) — SAME merges, a changed
    value stays split, and everything remains idempotent and audited."""

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> WeekdayAdjudicator:
        stub = WeekdayAdjudicator()

        async def fake_resolver(*args: Any, **kwargs: Any) -> WeekdayAdjudicator:
            return stub

        monkeypatch.setattr(service, "resolve_memory_adjudicator", fake_resolver)
        return stub

    async def test_live_paraphrase_pair_is_merged(
        self,
        session: AsyncSession,
        admin_ctx: WorkspaceContext,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub = self._patch(monkeypatch)
        rows = [
            _active_record(
                admin_ctx,
                world,
                "We deploy every other Thursday.",
                subject="deploy.days",
                confidence=0.9,
            ),
            _active_record(
                admin_ctx,
                world,
                "The release day is every other Thursday.",
                subject="release.day",
                confidence=1.0,
            ),
        ]
        session.add_all(rows)
        await session.flush()

        result = await service.deduplicate_memories(
            session, admin_ctx, deps=service.EmbeddingDeps(crypto=None)
        )
        assert result == (1, 1, 1, 1, True)
        assert len(stub.pairs) == 1 and stub.closed
        keeper = await session.get(MemoryRecord, rows[1].id)
        assert keeper is not None and keeper.status == MemoryStatus.ACTIVE.value
        loser = await session.get(MemoryRecord, rows[0].id)
        assert loser is not None
        assert loser.status == MemoryStatus.SUPERSEDED.value
        assert loser.policy_json["deduplicated_into"] == str(rows[1].id)
        events = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "memory.deduplicated")
            )
        )
        assert events[-1].metadata_json["adjudicated"] == 1
        assert events[-1].metadata_json["llm"] is True
        # Idempotent: nothing left to merge — and with no uncertain pairs the
        # adjudicator is never even resolved (llm=False: no smart matching ran).
        assert await service.deduplicate_memories(
            session, admin_ctx, deps=service.EmbeddingDeps(crypto=None)
        ) == (0, 0, 1, 0, False)

    async def test_value_change_stays_distinct(
        self,
        session: AsyncSession,
        admin_ctx: WorkspaceContext,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._patch(monkeypatch)
        session.add_all(
            [
                _active_record(
                    admin_ctx, world, "We deploy every other Thursday.", subject="deploy.day"
                ),
                _active_record(admin_ctx, world, "We deploy every Friday.", subject="deploy.day"),
            ]
        )
        await session.flush()
        result = await service.deduplicate_memories(
            session, admin_ctx, deps=service.EmbeddingDeps(crypto=None)
        )
        assert result == (0, 0, 2, 1, True)

    async def test_without_default_profile_behaves_as_today(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, world: World
    ) -> None:
        # No monkeypatch: the real resolver finds no default profile in this
        # workspace, so the pass runs rule-only and reports llm=False.
        session.add_all(
            [
                _active_record(
                    admin_ctx,
                    world,
                    "We deploy every other Thursday.",
                    subject="deploy.days",
                ),
                _active_record(
                    admin_ctx,
                    world,
                    "The release day is every other Thursday.",
                    subject="release.day",
                ),
            ]
        )
        await session.flush()
        result = await service.deduplicate_memories(
            session, admin_ctx, deps=service.EmbeddingDeps(crypto=None)
        )
        assert result == (0, 0, 2, 0, False)
