"""Memory management service: RBAC, explicit remember, versioned edits, pin,
contest, forget tombstones, and promotion review — against SQLite."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.memory import service
from jhin_api.memory.schemas import MemoryCreate, MemoryUpdate
from jhin_db.models import Agent, AuditEvent, MemoryRecord, Team, User, Workspace
from jhin_domain import ActorType, MemoryScope, MemoryStatus, WorkspaceRole, new_uuid7
from jhin_memory import ActorFacts, MemoryCandidate, SourceFacts, apply_candidates


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
