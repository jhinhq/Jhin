"""Phase 2 API integration tests (plan 45 Phase 2 exit test, at the API level).

Uses the dev-stack Postgres on 127.0.0.1:55432 with a dedicated test database
created and migrated from empty for every test, so these also prove the
migration chain works from an empty database. The FastAPI app runs in-process
via httpx's ASGI transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jhin_api.main import create_app
from jhin_api.security.passwords import hash_password
from jhin_api.settings import Settings
from jhin_db import create_engine, create_session_factory
from jhin_db.migrate import upgrade_to_head
from jhin_db.models import AuditEvent, User

pytestmark = pytest.mark.integration

PG_HOST = "127.0.0.1"
PG_PORT = 55432
TEST_DB = "jhin_phase2_test"
ADMIN_DSN = f"postgresql://jhin:jhin@{PG_HOST}:{PG_PORT}/postgres"
TEST_DB_URL = f"postgresql+asyncpg://jhin:jhin@{PG_HOST}:{PG_PORT}/{TEST_DB}"

OWNER = {
    "email": "owner@example.com",
    "password": "a-long-dev-password",
    "display_name": "Owner",
    "workspace_name": "Acme",
}


async def _recreate_database() -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()


async def _drop_database() -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture
def migrated_db_url() -> Iterator[str]:
    """Fresh, fully migrated database per test (migrations run from empty)."""
    asyncio.run(_recreate_database())
    upgrade_to_head(TEST_DB_URL)
    yield TEST_DB_URL
    asyncio.run(_drop_database())


@dataclass
class ApiHarness:
    client: httpx.AsyncClient
    transport: httpx.ASGITransport
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    def new_client(self) -> httpx.AsyncClient:
        """Independent cookie jar — a second browser/user."""
        return httpx.AsyncClient(transport=self.transport, base_url="http://test")

    def csrf(self, client: httpx.AsyncClient | None = None) -> dict[str, str]:
        jar = (client or self.client).cookies
        token = jar.get("jhin_csrf")
        assert token, "no CSRF cookie set; log in first"
        return {"x-csrf-token": token}


@pytest.fixture
async def api(migrated_db_url: str) -> AsyncIterator[ApiHarness]:
    settings = Settings(database_url=migrated_db_url, login_max_attempts=3)
    app = create_app(settings)
    # ASGITransport does not run lifespan; wire state exactly as lifespan does.
    engine = create_engine(migrated_db_url)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield ApiHarness(
            client=client,
            transport=transport,
            engine=engine,
            session_factory=app.state.session_factory,
        )
    await engine.dispose()


async def _bootstrap(api: ApiHarness) -> str:
    """Bootstrap the owner and return the created workspace id."""
    response = await api.client.post("/api/v1/auth/bootstrap", json=OWNER)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["memberships"][0]["role"] == "owner"
    workspace_id: str = body["memberships"][0]["workspace_id"]
    return workspace_id


async def _create_agent(api: ApiHarness, ws: str, name: str, **extra: Any) -> dict[str, Any]:
    response = await api.client.post(
        f"/api/v1/workspaces/{ws}/agents",
        json={"name": name, **extra},
        headers=api.csrf(),
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_exit_scenario_bootstrap_login_org_graph(api: ApiHarness) -> None:
    """Plan 45 Phase 2 exit test at the API level: bootstrap owner -> login ->
    Engineering -> CTO -> SWE + QA -> org graph intact across sessions."""
    # First run: bootstrap is required and available.
    status = await api.client.get("/api/v1/auth/bootstrap-status")
    assert status.json() == {"needs_bootstrap": True}

    ws = await _bootstrap(api)

    # Bootstrap disables itself.
    assert (await api.client.get("/api/v1/auth/bootstrap-status")).json() == {
        "needs_bootstrap": False
    }
    again = await api.client.post("/api/v1/auth/bootstrap", json=OWNER)
    assert again.status_code == 403

    me = await api.client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == OWNER["email"]

    # Build the Engineering hierarchy.
    team_resp = await api.client.post(
        f"/api/v1/workspaces/{ws}/teams",
        json={"name": "Engineering", "description": "Builds the product"},
        headers=api.csrf(),
    )
    assert team_resp.status_code == 201, team_resp.text
    team = team_resp.json()

    cto = await _create_agent(
        api, ws, "CTO", role_title="Chief Technology Officer", team_id=team["id"]
    )
    swe = await _create_agent(
        api, ws, "Senior Software Engineer", team_id=team["id"], manager_agent_id=cto["id"]
    )
    qa = await _create_agent(api, ws, "QA Engineer", team_id=team["id"], manager_agent_id=cto["id"])

    set_manager = await api.client.patch(
        f"/api/v1/workspaces/{ws}/teams/{team['id']}",
        json={"manager_agent_id": cto["id"]},
        headers=api.csrf(),
    )
    assert set_manager.status_code == 200, set_manager.text

    # Log out (session revoked server-side), then back in: a "reload".
    logout = await api.client.post("/api/v1/auth/logout", headers=api.csrf())
    assert logout.status_code == 204
    assert (await api.client.get("/api/v1/auth/me")).status_code == 401

    login = await api.client.post(
        "/api/v1/auth/login", json={"email": OWNER["email"], "password": OWNER["password"]}
    )
    assert login.status_code == 200, login.text

    # Hierarchy is intact.
    graph = (await api.client.get(f"/api/v1/workspaces/{ws}/org-graph")).json()
    assert [t["name"] for t in graph["teams"]] == ["Engineering"]
    assert graph["teams"][0]["manager_agent_id"] == cto["id"]
    agents = {a["id"]: a for a in graph["agents"]}
    assert len(agents) == 3
    assert agents[cto["id"]]["manager_agent_id"] is None
    assert agents[swe["id"]]["manager_agent_id"] == cto["id"]
    assert agents[qa["id"]]["manager_agent_id"] == cto["id"]
    assert all(a["team_id"] == team["id"] for a in agents.values())


async def test_cycle_rejection_for_agents_and_teams(api: ApiHarness) -> None:
    ws = await _bootstrap(api)
    cto = await _create_agent(api, ws, "CTO")
    swe = await _create_agent(api, ws, "SWE", manager_agent_id=cto["id"])
    lead = await _create_agent(api, ws, "Lead", manager_agent_id=swe["id"])

    # Direct cycle: CTO -> SWE -> CTO.
    direct = await api.client.patch(
        f"/api/v1/workspaces/{ws}/agents/{cto['id']}",
        json={"manager_agent_id": swe["id"]},
        headers=api.csrf(),
    )
    assert direct.status_code == 409
    assert "cycle" in direct.json()["detail"].lower()

    # Deep cycle: CTO -> Lead -> SWE -> CTO.
    deep = await api.client.patch(
        f"/api/v1/workspaces/{ws}/agents/{cto['id']}",
        json={"manager_agent_id": lead["id"]},
        headers=api.csrf(),
    )
    assert deep.status_code == 409

    # Self-management.
    self_ref = await api.client.patch(
        f"/api/v1/workspaces/{ws}/agents/{cto['id']}",
        json={"manager_agent_id": cto["id"]},
        headers=api.csrf(),
    )
    assert self_ref.status_code == 409

    # Team nesting cycle: A -> B -> A.
    team_a = (
        await api.client.post(
            f"/api/v1/workspaces/{ws}/teams", json={"name": "A"}, headers=api.csrf()
        )
    ).json()
    team_b = (
        await api.client.post(
            f"/api/v1/workspaces/{ws}/teams",
            json={"name": "B", "parent_team_id": team_a["id"]},
            headers=api.csrf(),
        )
    ).json()
    team_cycle = await api.client.patch(
        f"/api/v1/workspaces/{ws}/teams/{team_a['id']}",
        json={"parent_team_id": team_b["id"]},
        headers=api.csrf(),
    )
    assert team_cycle.status_code == 409

    # Cross-workspace references are rejected outright.
    other_ws = (
        await api.client.post("/api/v1/workspaces", json={"name": "Other"}, headers=api.csrf())
    ).json()
    foreign = await api.client.post(
        f"/api/v1/workspaces/{other_ws['id']}/agents",
        json={"name": "Spy", "manager_agent_id": cto["id"]},
        headers=api.csrf(),
    )
    assert foreign.status_code == 422


async def test_rbac_roles_and_workspace_isolation(api: ApiHarness) -> None:
    ws = await _bootstrap(api)
    team = (
        await api.client.post(
            f"/api/v1/workspaces/{ws}/teams", json={"name": "Engineering"}, headers=api.csrf()
        )
    ).json()
    agent = await _create_agent(api, ws, "CTO", team_id=team["id"])

    # Create a second user account directly (no invite flow until later phases).
    async with api.session_factory() as session:
        session.add(
            User(
                email="viewer@example.com",
                display_name="Viewer",
                password_hash=hash_password("another-long-password"),
            )
        )
        await session.commit()

    async with api.new_client() as second:
        login = await second.post(
            "/api/v1/auth/login",
            json={"email": "viewer@example.com", "password": "another-long-password"},
        )
        assert login.status_code == 200

        # Not a member: workspace is invisible (404, not 403).
        assert (await second.get(f"/api/v1/workspaces/{ws}/teams")).status_code == 404
        assert (await second.get("/api/v1/workspaces")).json() == []

        # Owner adds them as viewer.
        added = await api.client.post(
            f"/api/v1/workspaces/{ws}/members",
            json={"email": "viewer@example.com", "role": "viewer"},
            headers=api.csrf(),
        )
        assert added.status_code == 201, added.text
        membership_id = added.json()["id"]

        # Viewer: reads pass, writes and admin views fail.
        assert (await second.get(f"/api/v1/workspaces/{ws}/teams")).status_code == 200
        assert (await second.get(f"/api/v1/workspaces/{ws}/org-graph")).status_code == 200
        csrf2 = api.csrf(second)
        denied_write = await second.post(
            f"/api/v1/workspaces/{ws}/teams", json={"name": "Nope"}, headers=csrf2
        )
        assert denied_write.status_code == 403
        denied_pause = await second.post(
            f"/api/v1/workspaces/{ws}/agents/{agent['id']}/pause", headers=csrf2
        )
        assert denied_pause.status_code == 403
        assert (await second.get(f"/api/v1/workspaces/{ws}/audit-events")).status_code == 403

        # Promote to member: pause/resume become available, admin writes stay closed.
        promoted = await api.client.patch(
            f"/api/v1/workspaces/{ws}/members/{membership_id}",
            json={"role": "member"},
            headers=api.csrf(),
        )
        assert promoted.status_code == 200
        paused = await second.post(
            f"/api/v1/workspaces/{ws}/agents/{agent['id']}/pause", headers=csrf2
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        resumed = await second.post(
            f"/api/v1/workspaces/{ws}/agents/{agent['id']}/resume", headers=csrf2
        )
        assert resumed.json()["status"] == "active"
        still_denied = await second.post(
            f"/api/v1/workspaces/{ws}/teams", json={"name": "Nope"}, headers=csrf2
        )
        assert still_denied.status_code == 403

        # Isolation both ways: owner cannot see the second user's workspace.
        their_ws = (
            await second.post("/api/v1/workspaces", json={"name": "Private"}, headers=csrf2)
        ).json()
        assert (
            await api.client.get(f"/api/v1/workspaces/{their_ws['id']}/teams")
        ).status_code == 404

        # Last-owner protection.
        owner_membership = next(
            m
            for m in (await api.client.get(f"/api/v1/workspaces/{ws}/members")).json()
            if m["role"] == "owner"
        )
        demote = await api.client.patch(
            f"/api/v1/workspaces/{ws}/members/{owner_membership['id']}",
            json={"role": "admin"},
            headers=api.csrf(),
        )
        assert demote.status_code == 409


async def test_csrf_required_for_cookie_mutations(api: ApiHarness) -> None:
    ws = await _bootstrap(api)
    no_header = await api.client.post(f"/api/v1/workspaces/{ws}/teams", json={"name": "X"})
    assert no_header.status_code == 403
    assert "csrf" in no_header.json()["detail"].lower()
    with_header = await api.client.post(
        f"/api/v1/workspaces/{ws}/teams", json={"name": "X"}, headers=api.csrf()
    )
    assert with_header.status_code == 201


async def test_login_rate_limit_blocks_after_failures(api: ApiHarness) -> None:
    await _bootstrap(api)
    bad = {"email": OWNER["email"], "password": "wrong-password"}
    for _ in range(3):  # harness configures login_max_attempts=3
        assert (await api.client.post("/api/v1/auth/login", json=bad)).status_code == 401
    blocked = await api.client.post(
        "/api/v1/auth/login", json={"email": OWNER["email"], "password": OWNER["password"]}
    )
    assert blocked.status_code == 429


async def test_audit_trail_records_and_filters(api: ApiHarness) -> None:
    ws = await _bootstrap(api)
    team = (
        await api.client.post(
            f"/api/v1/workspaces/{ws}/teams", json={"name": "Engineering"}, headers=api.csrf()
        )
    ).json()
    agent = await _create_agent(api, ws, "CTO")
    await api.client.patch(
        f"/api/v1/workspaces/{ws}/teams/{team['id']}",
        json={"description": "updated"},
        headers=api.csrf(),
    )
    await api.client.post(f"/api/v1/workspaces/{ws}/agents/{agent['id']}/pause", headers=api.csrf())
    await api.client.delete(f"/api/v1/workspaces/{ws}/teams/{team['id']}", headers=api.csrf())

    page = (await api.client.get(f"/api/v1/workspaces/{ws}/audit-events")).json()
    actions = [e["action"] for e in page["events"]]
    for expected in (
        "workspace.created",
        "auth.owner_bootstrapped",
        "team.created",
        "team.updated",
        "team.deleted",
        "agent.created",
        "agent.paused",
    ):
        assert expected in actions, f"missing audit action {expected}: {actions}"
    assert page["total"] == len(actions)
    # Newest first.
    assert actions[0] == "team.deleted"

    # Filters.
    only_team_created = (
        await api.client.get(
            f"/api/v1/workspaces/{ws}/audit-events", params={"action": "team.created"}
        )
    ).json()
    assert {e["action"] for e in only_team_created["events"]} == {"team.created"}
    only_agents = (
        await api.client.get(
            f"/api/v1/workspaces/{ws}/audit-events", params={"target_type": "agent"}
        )
    ).json()
    assert {e["target_type"] for e in only_agents["events"]} == {"agent"}
    future = (
        await api.client.get(
            f"/api/v1/workspaces/{ws}/audit-events",
            params={"created_from": "2099-01-01T00:00:00Z"},
        )
    ).json()
    assert future["events"] == []

    # Auth events (login/logout) are recorded even without a workspace scope.
    await api.client.post("/api/v1/auth/logout", headers=api.csrf())
    async with api.session_factory() as session:
        rows = (await session.scalars(select(AuditEvent.action))).all()
    assert "auth.logout" in rows
    assert "auth.login" in rows

    # Append-only at the application layer: no API mutates audit rows, and the
    # table still contains every event after all the mutations above.
    async with api.session_factory() as session:
        count = await session.scalar(text("SELECT count(*) FROM audit_event"))
    assert count is not None and count >= page["total"]
