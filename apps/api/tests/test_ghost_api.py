"""Public Ghost evidence and access diagnostics enforce the execution boundary."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from jhin_api.connections import service
from jhin_api.connections.editorial import router as editorial_router
from jhin_api.connections.ghost_access import ghost_agent_access
from jhin_api.connections.router import router as connections_router
from jhin_api.deps import AdminCtx, ViewerCtx, get_db
from jhin_api.security.csrf import csrf_protect
from jhin_connectors.base import ConnectionHealth
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentTeamMembership,
    Connection,
    GhostEditorialReview,
    Team,
    Workspace,
)
from jhin_domain import new_uuid7
from jhin_secrets.variables import VariableActor, VariableError, VariableStore

KEY = "34" * 12 + ":" + "cd" * 32


async def test_assignment_release_requires_version_and_invalidates_exact_package(
    client, session, ghost
):
    from jhin_db.models.editorial import EditorialAssignment

    row = EditorialAssignment(
        workspace_id=ghost.connection.workspace_id,
        connection_id=ghost.connection.id,
        writer_agent_id=ghost.writer.id,
        publisher_agent_id=ghost.director.id,
    )
    session.add(row)
    await session.commit()
    base = (
        f"/api/v1/workspaces/{row.workspace_id}/connections/{row.connection_id}"
        f"/editorial-reviews/assignments/{row.id}"
    )
    fetched = await client.get(base)
    assert fetched.status_code == 200 and fetched.json()["release_intent"] == "draft_only"
    changed = await client.put(
        base + "/release-intent",
        json={"expected_version": 1, "release_intent": "publish_after_ashley_review"},
    )
    assert changed.status_code == 200 and changed.json()["editorial_version"] == 2
    stale = await client.put(
        base + "/release-intent", json={"expected_version": 1, "release_intent": "draft_only"}
    )
    assert stale.status_code == 409
    row.phase = "cancelled"
    await session.commit()
    cancelled = await client.put(
        base + "/release-intent", json={"expected_version": 2, "release_intent": "draft_only"}
    )
    assert cancelled.status_code == 409


@pytest.fixture
async def ghost(session, admin_ctx, crypto):
    team = Team(workspace_id=admin_ctx.workspace_id, name="Marketing")
    session.add(team)
    await session.flush()
    writer = Agent(
        workspace_id=admin_ctx.workspace_id, name="Writer", slug="ghost-writer", team_id=team.id
    )
    director = Agent(
        workspace_id=admin_ctx.workspace_id, name="Director", slug="ghost-director", team_id=team.id
    )
    session.add_all([writer, director])
    await session.flush()
    for agent in (writer, director):
        session.add(
            AgentTeamMembership(
                workspace_id=admin_ctx.workspace_id,
                team_id=team.id,
                agent_id=agent.id,
                is_primary=True,
            )
        )
        session.add(
            AgentCapabilityGrant(
                workspace_id=admin_ctx.workspace_id,
                agent_id=agent.id,
                capability="ghost.*",
                effect="allow",
                scope_json={"variable_audience": True},
            )
        )
    store = VariableStore(session, crypto)
    variable = await store.set(
        VariableActor(admin_ctx.workspace_id, "user", admin_ctx.user.id, is_admin=True),
        name="ghost.admin_key",
        scope="team",
        scope_id=team.id,
        sensitive=True,
        value=KEY,
    )
    connection = Connection(
        workspace_id=admin_ctx.workspace_id,
        name="Blog",
        connector_type="ghost",
        auth_type="api_key",
        config_json={
            "admin_url": "https://blog.example.test",
            "admin_key_variable_id": str(variable.id),
            "configured_by_agent_id": str(writer.id),
            "publisher_agent_id": str(director.id),
        },
    )
    session.add(connection)
    await session.flush()
    await store.bind(
        VariableActor(admin_ctx.workspace_id, "agent", writer.id),
        variable.id,
        connection.id,
        credential_field="admin_key",
        approved_origin="https://blog.example.test",
    )
    await session.commit()
    return SimpleNamespace(
        team=team,
        writer=writer,
        director=director,
        variable=variable,
        connection=connection,
        store=store,
    )


@pytest.fixture
async def client(session, admin_ctx, ghost):
    app = FastAPI()
    app.include_router(editorial_router)
    app.include_router(connections_router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[ViewerCtx.__metadata__[0].dependency] = lambda: admin_ctx
    app.dependency_overrides[AdminCtx.__metadata__[0].dependency] = lambda: admin_ctx
    app.dependency_overrides[csrf_protect] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as browser:
        yield browser


def review(ghost, *, workspace_id=None, connection_id=None, feedback=""):
    return GhostEditorialReview(
        workspace_id=workspace_id or ghost.connection.workspace_id,
        connection_id=connection_id or ghost.connection.id,
        post_id=new_uuid7().hex[:24],
        revision=new_uuid7().hex,
        admin_url="https://blog.example.test",
        provider_updated_at="2026-09-12T12:00:00Z",
        snapshot_json={
            "title": "Draft " + KEY,
            "html": "<p>" + KEY + "</p>",
            "url": "https://blog.example.test/draft",
        },
        author_agent_id=ghost.writer.id,
        publisher_agent_id=ghost.director.id,
        feedback=feedback,
    )


async def test_editorial_pages_and_details_are_isolated_bounded_and_redacted(
    client, session, ghost
):
    rows = [review(ghost) for _ in range(3)]
    session.add_all(rows)
    other_workspace = Workspace(name="Other", slug="ghost-other")
    session.add(other_workspace)
    await session.flush()
    foreign = review(ghost, workspace_id=other_workspace.id)
    other_connection = review(ghost, connection_id=new_uuid7())
    session.add_all([foreign, other_connection])
    await session.commit()
    base = (
        f"/api/v1/workspaces/{ghost.connection.workspace_id}/connections/"
        f"{ghost.connection.id}/editorial-reviews"
    )
    first = await client.get(base, params={"limit": 2})
    assert first.status_code == 200 and "no-store" in first.headers["cache-control"]
    assert len(first.json()["items"]) == 2 and KEY not in first.text
    assert all(row["html"] is None for row in first.json()["items"])
    second = await client.get(
        base, params={"limit": 2, "before_id": first.json()["next_before_id"]}
    )
    assert len(second.json()["items"]) == 1 and second.json()["next_before_id"] is None
    actual = {row["review_id"] for row in first.json()["items"] + second.json()["items"]}
    assert actual == {str(row.id) for row in rows}
    detail = await client.get(base + "/" + str(rows[0].id))
    assert detail.status_code == 200 and KEY not in detail.text
    assert "<p>" in detail.json()["html"] and "no-store" in detail.headers["cache-control"]
    for refused in (foreign, other_connection):
        assert (await client.get(base + "/" + str(refused.id))).status_code == 404
    assert (await client.get(base, params={"limit": 101})).status_code == 422


async def test_editorial_feedback_is_redacted_at_public_projection(client, session, ghost):
    row = review(ghost, feedback="Provider echoed " + KEY)
    session.add(row)
    await session.commit()
    path = (
        f"/api/v1/workspaces/{ghost.connection.workspace_id}/connections/"
        f"{ghost.connection.id}/editorial-reviews/{row.id}"
    )
    response = await client.get(path)
    assert response.status_code == 200 and KEY not in response.text


async def test_disabled_verification_consumes_bound_key_but_keeps_app_disabled(
    session, crypto, ghost, monkeypatch
):
    received = []

    async def verify(ctx):
        received.append(ctx.credentials)
        return ConnectionHealth(ok=True, message="Provider echoed " + KEY)

    monkeypatch.setattr(
        service, "get_connector", lambda _kind: SimpleNamespace(verify_connection=verify)
    )
    ghost.connection.status = "disabled"
    await session.flush()
    health = await service._check_connection(session, crypto, ghost.connection)
    assert received == [{"admin_key": KEY}] and health.ok
    assert KEY not in health.message and ghost.connection.status == "disabled"
    assert ghost.connection.last_verified_at is not None
    with pytest.raises(VariableError):
        await ghost.store.resolve_bound(
            VariableActor(ghost.connection.workspace_id, "agent", ghost.writer.id),
            ghost.variable.id,
            ghost.connection.id,
            credential_field="admin_key",
            approved_origin="https://blog.example.test",
        )


async def test_verification_refuses_revoked_team_and_changed_origin(session, crypto, ghost):
    membership = await session.scalar(
        select(AgentTeamMembership).where(AgentTeamMembership.agent_id == ghost.writer.id)
    )
    membership.left_at = datetime.now(UTC)
    await session.flush()
    with pytest.raises(HTTPException) as refused:
        await service._stored_credentials(session, crypto, ghost.connection)
    assert KEY not in str(refused.value)
    membership.left_at = None
    ghost.connection.config_json = {
        **ghost.connection.config_json,
        "admin_url": "https://other.example.test",
    }
    await session.flush()
    with pytest.raises(HTTPException):
        await service._stored_credentials(session, crypto, ghost.connection)


async def test_access_summary_distinguishes_writer_director_and_explicit_deny(
    client, session, ghost
):
    path = (
        f"/api/v1/workspaces/{ghost.connection.workspace_id}/connections/"
        f"{ghost.connection.id}/access-summary"
    )
    response = await client.get(path)
    assert response.status_code == 200 and KEY not in response.text
    agents = {row["agent_id"]: row for row in response.json()["agents"]}
    assert "ghost.post.publish" not in agents[str(ghost.writer.id)]["authorized_tool_names"]
    assert "ghost.post.publish" in agents[str(ghost.director.id)]["authorized_tool_names"]
    session.add(
        AgentCapabilityGrant(
            workspace_id=ghost.connection.workspace_id,
            agent_id=ghost.director.id,
            capability="ghost.post.publish",
            effect="deny",
            scope_json={"connection_id": str(ghost.connection.id)},
        )
    )
    await session.flush()
    agents = {row["agent_id"]: row for row in (await client.get(path)).json()["agents"]}
    assert "ghost.post.publish" not in agents[str(ghost.director.id)]["authorized_tool_names"]
    membership = await session.scalar(
        select(AgentTeamMembership).where(AgentTeamMembership.agent_id == ghost.writer.id)
    )
    membership.left_at = datetime.now(UTC)
    await session.flush()
    access = await ghost_agent_access(session, ghost.connection)
    assert not any(row["agent_id"] == ghost.writer.id and row["authorized"] for row in access)
    assert KEY not in json.dumps(access, default=str)


async def test_access_summary_refuses_foreign_connection(client, session, ghost):
    other = Workspace(name="Foreign", slug="ghost-foreign")
    session.add(other)
    await session.flush()
    connection = Connection(
        workspace_id=other.id, name="Foreign", connector_type="ghost", auth_type="api_key"
    )
    session.add(connection)
    await session.commit()
    response = await client.get(
        f"/api/v1/workspaces/{ghost.connection.workspace_id}/connections/{connection.id}/access-summary"
    )
    assert response.status_code == 404


async def test_access_summary_does_not_echo_secret_material_in_grant_scope(session, ghost):
    session.add(
        AgentCapabilityGrant(
            workspace_id=ghost.connection.workspace_id,
            agent_id=ghost.writer.id,
            capability="ghost.post.read",
            effect="allow",
            scope_json={"connection_id": str(ghost.connection.id), "unexpected_value": KEY},
        )
    )
    await session.flush()
    result = await ghost_agent_access(session, ghost.connection)
    assert KEY not in json.dumps(result, default=str)


async def test_manual_app_grant_cannot_override_a_private_variable_audience(session, ghost):
    outsider = Agent(
        workspace_id=ghost.connection.workspace_id, name="Other", slug="ghost-outsider"
    )
    session.add(outsider)
    await session.flush()
    session.add(
        AgentCapabilityGrant(
            workspace_id=ghost.connection.workspace_id,
            agent_id=outsider.id,
            capability="ghost.post.read",
            effect="allow",
            scope_json={"connection_id": str(ghost.connection.id)},
        )
    )
    await session.flush()
    result = await ghost_agent_access(session, ghost.connection)
    assert not any(row["agent_id"] == outsider.id and row["authorized"] for row in result)


async def test_changed_origin_is_not_reported_as_usable_variable_access(session, ghost):
    ghost.connection.config_json = {
        **ghost.connection.config_json,
        "admin_url": "https://changed.example.test",
    }
    await session.flush()
    result = await ghost_agent_access(session, ghost.connection)
    assert not any(row["authorized"] for row in result)
