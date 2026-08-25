"""Company topology API and service invariants."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents import service
from jhin_api.agents.router import router as agents_router
from jhin_api.deps import (
    AuthContext,
    Principal,
    WorkspaceContext,
    get_current_auth,
    get_current_principal,
    get_db,
)
from jhin_api.org.router import router as org_router
from jhin_api.settings import Settings
from jhin_api.teams import service as team_service
from jhin_api.teams.router import router as teams_router
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRelationship,
    AgentTeamMembership,
    AuditEvent,
    Team,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import WorkspaceRole, new_uuid7


def _request_meta() -> dict[str, Any]:
    return {"request_id": new_uuid7(), "ip_hash": "test-ip-hash"}


async def _team(session: AsyncSession, workspace_id: UUID, name: str) -> Team:
    team = Team(workspace_id=workspace_id, name=name)
    session.add(team)
    await session.flush()
    return team


async def _agent(
    session: AsyncSession,
    workspace_id: UUID,
    name: str,
    *,
    manager_agent_id: UUID | None = None,
) -> Agent:
    agent = Agent(
        workspace_id=workspace_id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{new_uuid7().hex[:6]}",
        manager_agent_id=manager_agent_id,
    )
    session.add(agent)
    await session.flush()
    return agent


async def _active_memberships(
    session: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> list[AgentTeamMembership]:
    rows = await session.scalars(
        select(AgentTeamMembership)
        .where(
            AgentTeamMembership.workspace_id == workspace_id,
            AgentTeamMembership.agent_id == agent_id,
            AgentTeamMembership.left_at.is_(None),
        )
        .order_by(AgentTeamMembership.team_id)
    )
    return list(rows)


async def test_create_agent_can_be_managerless_teamless_with_public_identity(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await service.create_agent(
        session,
        admin_ctx,
        values={
            "name": "Independent Researcher",
            "public_purpose": "Investigates reliability risks",
            "expertise_json": ["reliability", "testing"],
            "discoverability": "discoverable",
            "availability": "available",
            "secondary_team_ids": [],
        },
        **_request_meta(),
    )

    assert agent.team_id is None
    assert agent.manager_agent_id is None
    assert agent.public_purpose == "Investigates reliability risks"
    assert agent.expertise_json == ["reliability", "testing"]
    assert await _active_memberships(session, admin_ctx.workspace_id, agent.id) == []


async def test_create_agent_adds_primary_and_secondary_memberships_atomically(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    primary = await _team(session, admin_ctx.workspace_id, "Engineering")
    secondary = await _team(session, admin_ctx.workspace_id, "Research")

    agent = await service.create_agent(
        session,
        admin_ctx,
        values={
            "name": "Builder",
            "team_id": primary.id,
            "secondary_team_ids": [secondary.id],
        },
        **_request_meta(),
    )
    memberships = await _active_memberships(session, admin_ctx.workspace_id, agent.id)

    assert agent.team_id == primary.id
    assert {(row.team_id, row.is_primary) for row in memberships} == {
        (primary.id, True),
        (secondary.id, False),
    }


async def test_legacy_agent_update_keeps_primary_pointer_and_memberships_in_sync(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    first = await _team(session, admin_ctx.workspace_id, "Engineering")
    second = await _team(session, admin_ctx.workspace_id, "Research")
    third = await _team(session, admin_ctx.workspace_id, "Operations")
    agent = await service.create_agent(
        session,
        admin_ctx,
        values={"name": "Builder", "team_id": first.id, "secondary_team_ids": [second.id]},
        **_request_meta(),
    )

    updated = await service.update_agent(
        session,
        admin_ctx,
        agent.id,
        changes={"team_id": second.id, "secondary_team_ids": [third.id]},
        **_request_meta(),
    )
    memberships = await _active_memberships(session, admin_ctx.workspace_id, agent.id)

    assert updated.team_id == second.id
    assert {(row.team_id, row.is_primary) for row in memberships} == {
        (second.id, True),
        (third.id, False),
    }


async def test_legacy_team_patch_promotes_an_existing_secondary_membership(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    primary = await _team(session, admin_ctx.workspace_id, "Engineering")
    promoted = await _team(session, admin_ctx.workspace_id, "Research")
    agent = await service.create_agent(
        session,
        admin_ctx,
        values={
            "name": "Builder",
            "team_id": primary.id,
            "secondary_team_ids": [promoted.id],
        },
        **_request_meta(),
    )

    updated = await service.update_agent(
        session,
        admin_ctx,
        agent.id,
        changes={"team_id": promoted.id},
        **_request_meta(),
    )
    memberships = await _active_memberships(session, admin_ctx.workspace_id, agent.id)

    assert updated.team_id == promoted.id
    assert [(row.team_id, row.is_primary) for row in memberships] == [(promoted.id, True)]


async def test_replace_memberships_changes_primary_and_handles_last_primary_removal(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    first = await _team(session, admin_ctx.workspace_id, "Engineering")
    second = await _team(session, admin_ctx.workspace_id, "Research")
    agent = await _agent(session, admin_ctx.workspace_id, "Builder")

    await service.replace_memberships(
        session,
        admin_ctx,
        agent.id,
        primary_team_id=first.id,
        secondary_team_ids=[second.id],
        **_request_meta(),
    )
    await service.replace_memberships(
        session,
        admin_ctx,
        agent.id,
        primary_team_id=second.id,
        secondary_team_ids=[],
        **_request_meta(),
    )
    memberships = await _active_memberships(session, admin_ctx.workspace_id, agent.id)
    primary_pointer = await session.scalar(select(Agent.team_id).where(Agent.id == agent.id))
    assert primary_pointer == second.id
    assert [(row.team_id, row.is_primary) for row in memberships] == [(second.id, True)]

    await service.replace_memberships(
        session,
        admin_ctx,
        agent.id,
        primary_team_id=None,
        secondary_team_ids=[first.id],
        **_request_meta(),
    )
    memberships = await _active_memberships(session, admin_ctx.workspace_id, agent.id)
    primary_pointer = await session.scalar(select(Agent.team_id).where(Agent.id == agent.id))
    assert primary_pointer is None
    assert [(row.team_id, row.is_primary) for row in memberships] == [(first.id, False)]

    actions = list(
        await session.scalars(select(AuditEvent.action).where(AuditEvent.target_id == agent.id))
    )
    assert actions.count("agent.memberships.updated") == 3


@pytest.mark.parametrize("secondary_indexes", [[0, 0], [0, 1]])
async def test_replace_memberships_rejects_duplicate_or_primary_overlap(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    secondary_indexes: list[int],
) -> None:
    teams = [
        await _team(session, admin_ctx.workspace_id, "Engineering"),
        await _team(session, admin_ctx.workspace_id, "Research"),
    ]
    agent = await _agent(session, admin_ctx.workspace_id, "Builder")

    with pytest.raises(HTTPException) as exc_info:
        await service.replace_memberships(
            session,
            admin_ctx,
            agent.id,
            primary_team_id=teams[0].id,
            secondary_team_ids=[teams[index].id for index in secondary_indexes],
            **_request_meta(),
        )

    assert exc_info.value.status_code == 409


async def test_manager_updates_reuse_cycle_check_and_reject_cycles(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    root = await _agent(session, admin_ctx.workspace_id, "Root")
    report = await _agent(session, admin_ctx.workspace_id, "Report", manager_agent_id=root.id)

    with pytest.raises(HTTPException) as exc_info:
        await service.update_agent(
            session,
            admin_ctx,
            root.id,
            changes={"manager_agent_id": report.id},
            **_request_meta(),
        )

    assert exc_info.value.status_code == 409


async def test_new_topology_references_are_hidden_across_workspaces(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    foreign_workspace = Workspace(name="Foreign", slug=f"foreign-{new_uuid7().hex[:8]}")
    session.add(foreign_workspace)
    await session.flush()
    foreign_team = await _team(session, foreign_workspace.id, "Foreign Team")
    foreign_agent = await _agent(session, foreign_workspace.id, "Foreign Agent")
    local_agent = await _agent(session, admin_ctx.workspace_id, "Local Agent")

    with pytest.raises(HTTPException) as membership_error:
        await service.replace_memberships(
            session,
            admin_ctx,
            local_agent.id,
            primary_team_id=foreign_team.id,
            secondary_team_ids=[],
            **_request_meta(),
        )
    assert membership_error.value.status_code == 404

    with pytest.raises(HTTPException) as relationship_error:
        await service.create_relationship(
            session,
            admin_ctx,
            local_agent.id,
            target_agent_id=foreign_agent.id,
            kind="advisor",
            purpose="Private foreign edge",
            **_request_meta(),
        )
    assert relationship_error.value.status_code == 404


async def test_close_collaborator_is_canonical_symmetric_and_deletable_from_either_end(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    first = await _agent(session, admin_ctx.workspace_id, "First")
    second = await _agent(session, admin_ctx.workspace_id, "Second")
    source, target = (first, second) if first.id > second.id else (second, first)

    relationship = await service.create_relationship(
        session,
        admin_ctx,
        source.id,
        target_agent_id=target.id,
        kind="close_collaborator",
        purpose="Pair on critical work",
        **_request_meta(),
    )
    assert relationship.source_agent_id < relationship.target_agent_id

    with pytest.raises(HTTPException) as duplicate_error:
        await service.create_relationship(
            session,
            admin_ctx,
            target.id,
            target_agent_id=source.id,
            kind="close_collaborator",
            purpose="Same symmetric relationship",
            **_request_meta(),
        )
    assert duplicate_error.value.status_code == 409

    listed = await service.list_relationships(session, admin_ctx.workspace_id, source.id)
    assert [row.id for row in listed] == [relationship.id]

    await service.delete_relationship(
        session,
        admin_ctx,
        source.id,
        relationship.id,
        **_request_meta(),
    )
    await session.refresh(relationship)
    assert relationship.status == "inactive"
    actions = list(
        await session.scalars(
            select(AuditEvent.action).where(AuditEvent.target_id == relationship.id)
        )
    )
    assert actions == ["agent.relationship.created", "agent.relationship.deleted"]


@pytest.mark.parametrize("kind", ["advisor", "preferred_reviewer"])
async def test_directed_relationships_preserve_direction_and_reject_duplicates(
    session: AsyncSession, admin_ctx: WorkspaceContext, kind: str
) -> None:
    source = await _agent(session, admin_ctx.workspace_id, f"{kind} source")
    target = await _agent(session, admin_ctx.workspace_id, f"{kind} target")

    relationship = await service.create_relationship(
        session,
        admin_ctx,
        source.id,
        target_agent_id=target.id,
        kind=kind,
        purpose="Routing context only",
        **_request_meta(),
    )
    assert relationship.source_agent_id == source.id
    assert relationship.target_agent_id == target.id

    with pytest.raises(HTTPException) as exc_info:
        await service.create_relationship(
            session,
            admin_ctx,
            source.id,
            target_agent_id=target.id,
            kind=kind,
            purpose="Duplicate",
            **_request_meta(),
        )
    assert exc_info.value.status_code == 409


async def test_relationships_do_not_create_memberships_or_capability_grants(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    source = await _agent(session, admin_ctx.workspace_id, "Source")
    target = await _agent(session, admin_ctx.workspace_id, "Target")

    await service.create_relationship(
        session,
        admin_ctx,
        source.id,
        target_agent_id=target.id,
        kind="advisor",
        purpose="Advice only",
        **_request_meta(),
    )

    membership_count = await session.scalar(select(func.count()).select_from(AgentTeamMembership))
    grant_count = await session.scalar(select(func.count()).select_from(AgentCapabilityGrant))
    assert membership_count == 0
    assert grant_count == 0


async def test_team_detail_groups_active_members_without_duplicates(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    team = await _team(session, admin_ctx.workspace_id, "Engineering")
    primary_agent = await _agent(session, admin_ctx.workspace_id, "Primary")
    secondary_agent = await _agent(session, admin_ctx.workspace_id, "Secondary")
    session.add_all(
        [
            AgentTeamMembership(
                workspace_id=admin_ctx.workspace_id,
                agent_id=primary_agent.id,
                team_id=team.id,
                is_primary=True,
            ),
            AgentTeamMembership(
                workspace_id=admin_ctx.workspace_id,
                agent_id=secondary_agent.id,
                team_id=team.id,
                is_primary=False,
            ),
        ]
    )
    await session.commit()

    grouped = await team_service.get_team_memberships(session, admin_ctx.workspace_id, team.id)
    assert [item.agent_id for item in grouped.primary] == [primary_agent.id]
    assert [item.agent_id for item in grouped.secondary] == [secondary_agent.id]
    assert len({item.agent_id for item in grouped.primary + grouped.secondary}) == 2


@pytest.fixture
async def topology_client(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, User], Agent, Agent]]:
    users = {"admin": admin_ctx.user}
    for role in (WorkspaceRole.VIEWER, WorkspaceRole.MEMBER):
        user = User(
            email=f"{role.value}-{new_uuid7().hex[:8]}@example.com",
            display_name=role.value.title(),
            password_hash="x",
        )
        session.add(user)
        await session.flush()
        users[role.value] = user
    for role_name, user in users.items():
        session.add(
            WorkspaceMembership(
                workspace_id=admin_ctx.workspace_id,
                user_id=user.id,
                role=role_name,
            )
        )
    source = await _agent(session, admin_ctx.workspace_id, "Route Source")
    target = await _agent(session, admin_ctx.workspace_id, "Route Target")
    await session.commit()

    actor = {"user": users["admin"]}
    app = FastAPI()
    app.state.settings = Settings()

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(agents_router)
    app.include_router(teams_router)
    app.include_router(org_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_auth() -> AuthContext:
        return AuthContext(
            user=actor["user"],
            session_record=UserSession(
                user_id=actor["user"].id,
                token_hash=f"fake-{actor['user'].id}",
                expires_at=source.created_at,
            ),
        )

    app.dependency_overrides[get_db] = override_db

    async def _principal() -> Principal:
        return Principal(user=(await override_auth()).user)

    app.dependency_overrides[get_current_auth] = override_auth
    app.dependency_overrides[get_current_principal] = _principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("jhin_csrf", "test-csrf")
        client.actor = actor  # type: ignore[attr-defined]
        yield client, users, source, target


async def test_viewer_reads_topology_and_viewer_member_mutations_are_forbidden(
    topology_client: tuple[httpx.AsyncClient, dict[str, User], Agent, Agent],
    admin_ctx: WorkspaceContext,
) -> None:
    client, users, source, target = topology_client
    workspace_id = admin_ctx.workspace_id
    actor: dict[str, User] = client.actor  # type: ignore[attr-defined]
    headers = {"x-csrf-token": "test-csrf"}

    actor["user"] = users["viewer"]
    assert (
        await client.get(f"/api/v1/workspaces/{workspace_id}/agents/{source.id}/memberships")
    ).status_code == 200
    assert (await client.get(f"/api/v1/workspaces/{workspace_id}/org-graph")).status_code == 200

    mutations = [
        (
            "post",
            f"/api/v1/workspaces/{workspace_id}/agents",
            {"name": "Unauthorized Agent"},
        ),
        (
            "put",
            f"/api/v1/workspaces/{workspace_id}/agents/{source.id}/memberships",
            {"primary_team_id": None, "secondary_team_ids": []},
        ),
        (
            "post",
            f"/api/v1/workspaces/{workspace_id}/agents/{source.id}/relationships",
            {
                "target_agent_id": str(target.id),
                "kind": "advisor",
                "purpose": "Advice",
            },
        ),
        (
            "patch",
            f"/api/v1/workspaces/{workspace_id}/agents/{source.id}",
            {"public_purpose": "Unauthorized"},
        ),
    ]
    for role in ("viewer", "member"):
        actor["user"] = users[role]
        for method, url, payload in mutations:
            response = await client.request(method, url, json=payload, headers=headers)
            assert response.status_code == 403, (role, method, response.text)


async def test_admin_topology_mutations_succeed_and_require_csrf(
    topology_client: tuple[httpx.AsyncClient, dict[str, User], Agent, Agent],
    admin_ctx: WorkspaceContext,
) -> None:
    client, users, source, target = topology_client
    workspace_id = admin_ctx.workspace_id
    actor: dict[str, User] = client.actor  # type: ignore[attr-defined]
    actor["user"] = users["admin"]
    relationship_url = f"/api/v1/workspaces/{workspace_id}/agents/{source.id}/relationships"

    missing_csrf = await client.post(
        relationship_url,
        json={"target_agent_id": str(target.id), "kind": "advisor", "purpose": "Advice"},
    )
    assert missing_csrf.status_code == 403

    headers = {"x-csrf-token": "test-csrf"}
    membership_update = await client.put(
        f"/api/v1/workspaces/{workspace_id}/agents/{source.id}/memberships",
        json={"primary_team_id": None, "secondary_team_ids": []},
        headers=headers,
    )
    assert membership_update.status_code == 200, membership_update.text
    agent_update = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/agents/{source.id}",
        json={"public_purpose": "Coordinates route testing"},
        headers=headers,
    )
    assert agent_update.status_code == 200, agent_update.text
    agent_create = await client.post(
        f"/api/v1/workspaces/{workspace_id}/agents",
        json={"name": "Admin Created"},
        headers=headers,
    )
    assert agent_create.status_code == 201, agent_create.text
    created = await client.post(
        relationship_url,
        json={"target_agent_id": str(target.id), "kind": "advisor", "purpose": "Advice"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    relationship_id = created.json()["id"]

    for role in ("viewer", "member"):
        actor["user"] = users[role]
        denied = await client.delete(f"{relationship_url}/{relationship_id}", headers=headers)
        assert denied.status_code == 403

    actor["user"] = users["admin"]
    deleted = await client.delete(f"{relationship_url}/{relationship_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text


async def test_topology_routes_hide_cross_workspace_resources(
    topology_client: tuple[httpx.AsyncClient, dict[str, User], Agent, Agent],
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
) -> None:
    client, users, source, _ = topology_client
    actor: dict[str, User] = client.actor  # type: ignore[attr-defined]
    actor["user"] = users["admin"]
    foreign_workspace = Workspace(name="Foreign Route", slug=f"foreign-{new_uuid7().hex[:8]}")
    session.add(foreign_workspace)
    await session.flush()
    foreign_team = await _team(session, foreign_workspace.id, "Foreign Route Team")
    foreign_agent = await _agent(session, foreign_workspace.id, "Foreign Route Agent")
    await session.commit()
    headers = {"x-csrf-token": "test-csrf"}

    read = await client.get(
        f"/api/v1/workspaces/{admin_ctx.workspace_id}/agents/{foreign_agent.id}/memberships"
    )
    assert read.status_code == 404
    create = await client.post(
        f"/api/v1/workspaces/{admin_ctx.workspace_id}/agents/{source.id}/relationships",
        json={
            "target_agent_id": str(foreign_agent.id),
            "kind": "advisor",
            "purpose": "Must stay hidden",
        },
        headers=headers,
    )
    assert create.status_code == 404

    headers = {"x-csrf-token": "test-csrf"}
    for payload in (
        {"name": "Foreign Primary", "team_id": str(foreign_team.id)},
        {"name": "Foreign Manager", "manager_agent_id": str(foreign_agent.id)},
    ):
        response = await client.post(
            f"/api/v1/workspaces/{admin_ctx.workspace_id}/agents",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 404, response.text
    for payload in (
        {"team_id": str(foreign_team.id)},
        {"manager_agent_id": str(foreign_agent.id)},
    ):
        response = await client.patch(
            f"/api/v1/workspaces/{admin_ctx.workspace_id}/agents/{source.id}",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    "payload",
    [{}, {"primary_team_id": None}, {"secondary_team_ids": []}],
)
async def test_membership_replacement_requires_the_complete_document(
    topology_client: tuple[httpx.AsyncClient, dict[str, User], Agent, Agent],
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    payload: dict[str, Any],
) -> None:
    client, users, source, _ = topology_client
    actor: dict[str, User] = client.actor  # type: ignore[attr-defined]
    actor["user"] = users["admin"]
    team = await _team(session, admin_ctx.workspace_id, "Required Payload Team")
    source.team_id = team.id
    session.add(
        AgentTeamMembership(
            workspace_id=admin_ctx.workspace_id,
            agent_id=source.id,
            team_id=team.id,
            is_primary=True,
        )
    )
    await session.commit()

    response = await client.put(
        f"/api/v1/workspaces/{admin_ctx.workspace_id}/agents/{source.id}/memberships",
        json=payload,
        headers={"x-csrf-token": "test-csrf"},
    )
    assert response.status_code == 422
    memberships = await _active_memberships(session, admin_ctx.workspace_id, source.id)
    assert [(row.team_id, row.is_primary) for row in memberships] == [(team.id, True)]


def test_topology_schemas_reject_malformed_literals_and_bounds() -> None:
    from pydantic import ValidationError

    from jhin_api.agents.schemas import AgentCreate, RelationshipCreate

    with pytest.raises(ValidationError):
        AgentCreate(name="A", discoverability="everyone")
    with pytest.raises(ValidationError):
        AgentCreate(name="A", availability="sometimes")
    with pytest.raises(ValidationError):
        AgentCreate(name="A", expertise_json=["tag"] * 21)
    with pytest.raises(ValidationError):
        RelationshipCreate(
            target_agent_id=new_uuid7(), kind="manager", purpose="Not a relationship kind"
        )


async def test_relationship_list_is_workspace_scoped(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    source = await _agent(session, admin_ctx.workspace_id, "Scoped Source")
    target = await _agent(session, admin_ctx.workspace_id, "Scoped Target")
    relationship = await service.create_relationship(
        session,
        admin_ctx,
        source.id,
        target_agent_id=target.id,
        kind="advisor",
        purpose="Scoped",
        **_request_meta(),
    )

    foreign_workspace = Workspace(name="Foreign List", slug=f"foreign-{new_uuid7().hex[:8]}")
    session.add(foreign_workspace)
    await session.flush()
    foreign_source = await _agent(session, foreign_workspace.id, "Foreign Source")
    foreign_target = await _agent(session, foreign_workspace.id, "Foreign Target")
    session.add(
        AgentRelationship(
            workspace_id=foreign_workspace.id,
            source_agent_id=foreign_source.id,
            target_agent_id=foreign_target.id,
            kind="advisor",
            purpose="Foreign",
        )
    )
    await session.commit()

    listed = await service.list_relationships(session, admin_ctx.workspace_id, source.id)
    assert [row.id for row in listed] == [relationship.id]
