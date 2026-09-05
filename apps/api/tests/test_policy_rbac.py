"""Route-level RBAC and workspace-isolation tests for capability grants."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.access.keys import ApiKeyPrincipal
from jhin_api.connections.router import router as connections_router
from jhin_api.deps import (
    AuthContext,
    Principal,
    WorkspaceContext,
    get_current_auth,
    get_current_principal,
    get_db,
)
from jhin_api.policy.router import router as policy_router
from jhin_api.settings import Settings
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    Connection,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from jhin_db.models.connection import new_public_id
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_secrets import SecretCrypto

CSRF_TOKEN = "policy-rbac-csrf"
CSRF_HEADERS = {"x-csrf-token": CSRF_TOKEN}
GRANT_PAYLOAD = {
    "capability": "github.issue.comment",
    "scope": {"repository": "acme/api"},
    "effect": "allow",
}
CONFIG_PAYLOAD = {"config": {"base_url": ""}}


@dataclass
class PolicyRbacHarness:
    client: httpx.AsyncClient
    # ``user`` is who is calling; ``api_key`` (when set) makes that call a
    # bearer-key call with exactly these scopes instead of a browser session.
    actor: dict[str, Any]
    users: dict[str, User]
    workspace_id: UUID
    agent: Agent
    existing_grant: AgentCapabilityGrant
    foreign_workspace: Workspace
    foreign_agent: Agent
    foreign_grant: AgentCapabilityGrant
    connection: Connection
    foreign_connection: Connection


def _connection(workspace_id: UUID, name: str) -> Connection:
    return Connection(
        workspace_id=workspace_id,
        connector_type="github",
        name=name,
        auth_type="pat",
        public_id=new_public_id(),
        config_json={},
    )


@pytest.fixture
async def policy_rbac(
    session: AsyncSession, admin_ctx: WorkspaceContext, crypto: SecretCrypto
) -> AsyncIterator[PolicyRbacHarness]:
    users = {WorkspaceRole.ADMIN.value: admin_ctx.user}
    for role in (WorkspaceRole.VIEWER, WorkspaceRole.MEMBER):
        user = User(
            email=f"policy-{role.value}-{new_uuid7().hex[:8]}@example.com",
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

    agent = Agent(
        workspace_id=admin_ctx.workspace_id,
        name="Policy Route Agent",
        slug=f"policy-route-agent-{new_uuid7().hex[:8]}",
    )
    session.add(agent)
    await session.flush()
    existing_grant = AgentCapabilityGrant(
        workspace_id=admin_ctx.workspace_id,
        agent_id=agent.id,
        capability="github.repository.read",
        scope_json={"repository": "acme/api"},
        effect="allow",
    )

    foreign_workspace = Workspace(
        name="Foreign Policy Route",
        slug=f"foreign-policy-route-{new_uuid7().hex[:8]}",
    )
    session.add(foreign_workspace)
    await session.flush()
    foreign_agent = Agent(
        workspace_id=foreign_workspace.id,
        name="Foreign Policy Agent",
        slug=f"foreign-policy-agent-{new_uuid7().hex[:8]}",
    )
    session.add(foreign_agent)
    await session.flush()
    foreign_grant = AgentCapabilityGrant(
        workspace_id=foreign_workspace.id,
        agent_id=foreign_agent.id,
        capability="github.repository.read",
        scope_json={"repository": "foreign/private"},
        effect="allow",
    )
    connection = _connection(admin_ctx.workspace_id, "GitHub")
    foreign_connection = _connection(foreign_workspace.id, "Foreign GitHub")
    session.add_all([existing_grant, foreign_grant, connection, foreign_connection])
    await session.commit()

    actor: dict[str, Any] = {"user": users[WorkspaceRole.ADMIN.value]}
    app = FastAPI()
    app.state.settings = Settings()
    app.state.secret_crypto = crypto

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(policy_router)
    app.include_router(connections_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_auth() -> AuthContext:
        user = actor["user"]
        return AuthContext(
            user=user,
            session_record=UserSession(
                user_id=user.id,
                token_hash=f"policy-rbac-{user.id}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )

    app.dependency_overrides[get_db] = override_db

    async def _principal() -> Principal:
        return Principal(user=(await override_auth()).user, api_key=actor.get("api_key"))

    app.dependency_overrides[get_current_auth] = override_auth
    app.dependency_overrides[get_current_principal] = _principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("jhin_csrf", CSRF_TOKEN)
        yield PolicyRbacHarness(
            client=client,
            actor=actor,
            users=users,
            workspace_id=admin_ctx.workspace_id,
            agent=agent,
            existing_grant=existing_grant,
            foreign_workspace=foreign_workspace,
            foreign_agent=foreign_agent,
            foreign_grant=foreign_grant,
            connection=connection,
            foreign_connection=foreign_connection,
        )


@pytest.mark.parametrize("role", [WorkspaceRole.VIEWER, WorkspaceRole.MEMBER])
async def test_non_admin_cannot_create_or_revoke_capability_grants(
    policy_rbac: PolicyRbacHarness,
    session: AsyncSession,
    role: WorkspaceRole,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[role.value]
    base_url = f"/api/v1/workspaces/{policy_rbac.workspace_id}/agents/{policy_rbac.agent.id}/grants"

    created = await policy_rbac.client.post(
        base_url,
        json=GRANT_PAYLOAD,
        headers=CSRF_HEADERS,
    )
    revoked = await policy_rbac.client.delete(
        f"{base_url}/{policy_rbac.existing_grant.id}",
        headers=CSRF_HEADERS,
    )

    assert created.status_code == 403, created.text
    assert revoked.status_code == 403, revoked.text
    assert await session.get(AgentCapabilityGrant, policy_rbac.existing_grant.id) is not None


async def test_admin_can_create_and_revoke_capability_grants(
    policy_rbac: PolicyRbacHarness,
    session: AsyncSession,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]
    base_url = f"/api/v1/workspaces/{policy_rbac.workspace_id}/agents/{policy_rbac.agent.id}/grants"

    created = await policy_rbac.client.post(
        base_url,
        json=GRANT_PAYLOAD,
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    assert created.json()["capability"] == GRANT_PAYLOAD["capability"]

    grant_id = UUID(created.json()["id"])
    revoked = await policy_rbac.client.delete(f"{base_url}/{grant_id}", headers=CSRF_HEADERS)
    assert revoked.status_code == 204, revoked.text
    assert await session.get(AgentCapabilityGrant, grant_id) is None


async def test_capability_grant_mutations_require_csrf(
    policy_rbac: PolicyRbacHarness,
    session: AsyncSession,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]
    base_url = f"/api/v1/workspaces/{policy_rbac.workspace_id}/agents/{policy_rbac.agent.id}/grants"

    created = await policy_rbac.client.post(base_url, json=GRANT_PAYLOAD)
    revoked = await policy_rbac.client.delete(f"{base_url}/{policy_rbac.existing_grant.id}")

    assert created.status_code == 403, created.text
    assert revoked.status_code == 403, revoked.text
    assert await session.get(AgentCapabilityGrant, policy_rbac.existing_grant.id) is not None


async def test_capability_grant_routes_hide_cross_workspace_targets(
    policy_rbac: PolicyRbacHarness,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]
    local_workspace_url = f"/api/v1/workspaces/{policy_rbac.workspace_id}/agents"

    create_for_foreign_agent = await policy_rbac.client.post(
        f"{local_workspace_url}/{policy_rbac.foreign_agent.id}/grants",
        json=GRANT_PAYLOAD,
        headers=CSRF_HEADERS,
    )
    revoke_foreign_grant = await policy_rbac.client.delete(
        f"{local_workspace_url}/{policy_rbac.foreign_agent.id}/grants/"
        f"{policy_rbac.foreign_grant.id}",
        headers=CSRF_HEADERS,
    )
    mutate_foreign_workspace = await policy_rbac.client.post(
        f"/api/v1/workspaces/{policy_rbac.foreign_workspace.id}/agents/"
        f"{policy_rbac.agent.id}/grants",
        json=GRANT_PAYLOAD,
        headers=CSRF_HEADERS,
    )

    assert create_for_foreign_agent.status_code == 404, create_for_foreign_agent.text
    assert revoke_foreign_grant.status_code == 404, revoke_foreign_grant.text
    assert mutate_foreign_workspace.status_code == 404, mutate_foreign_workspace.text


def _bundle_url(harness: PolicyRbacHarness, agent: Agent, bundle_id: str = "github-read") -> str:
    return f"/api/v1/workspaces/{harness.workspace_id}/agents/{agent.id}/bundles/{bundle_id}"


def _config_url(harness: PolicyRbacHarness, connection: Connection) -> str:
    return f"/api/v1/workspaces/{harness.workspace_id}/connections/{connection.id}/config"


@pytest.mark.parametrize("role", [WorkspaceRole.VIEWER, WorkspaceRole.MEMBER])
async def test_non_admin_cannot_apply_or_remove_bundles_or_edit_connection_config(
    policy_rbac: PolicyRbacHarness,
    session: AsyncSession,
    role: WorkspaceRole,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[role.value]

    applied = await policy_rbac.client.post(
        _bundle_url(policy_rbac, policy_rbac.agent), json={}, headers=CSRF_HEADERS
    )
    removed = await policy_rbac.client.delete(
        _bundle_url(policy_rbac, policy_rbac.agent), headers=CSRF_HEADERS
    )
    configured = await policy_rbac.client.patch(
        _config_url(policy_rbac, policy_rbac.connection), json=CONFIG_PAYLOAD, headers=CSRF_HEADERS
    )

    assert applied.status_code == 403, applied.text
    assert removed.status_code == 403, removed.text
    assert configured.status_code == 403, configured.text
    assert await session.get(AgentCapabilityGrant, policy_rbac.existing_grant.id) is not None
    # Reads stay viewer+.
    listed = await policy_rbac.client.get(
        f"/api/v1/workspaces/{policy_rbac.workspace_id}/agents/{policy_rbac.agent.id}/bundles"
    )
    assert listed.status_code == 200, listed.text
    workspace_bundles = await policy_rbac.client.get(
        f"/api/v1/workspaces/{policy_rbac.workspace_id}/tools/bundles"
    )
    assert workspace_bundles.status_code == 200, workspace_bundles.text


async def test_bundle_and_config_mutations_require_csrf(
    policy_rbac: PolicyRbacHarness,
    session: AsyncSession,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]

    applied = await policy_rbac.client.post(_bundle_url(policy_rbac, policy_rbac.agent), json={})
    removed = await policy_rbac.client.delete(_bundle_url(policy_rbac, policy_rbac.agent))
    configured = await policy_rbac.client.patch(
        _config_url(policy_rbac, policy_rbac.connection), json=CONFIG_PAYLOAD
    )

    assert applied.status_code == 403, applied.text
    assert removed.status_code == 403, removed.text
    assert configured.status_code == 403, configured.text
    assert await session.get(AgentCapabilityGrant, policy_rbac.existing_grant.id) is not None


async def test_bundle_and_config_routes_hide_cross_workspace_targets(
    policy_rbac: PolicyRbacHarness,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]

    applied = await policy_rbac.client.post(
        _bundle_url(policy_rbac, policy_rbac.foreign_agent), json={}, headers=CSRF_HEADERS
    )
    removed = await policy_rbac.client.delete(
        _bundle_url(policy_rbac, policy_rbac.foreign_agent), headers=CSRF_HEADERS
    )
    listed = await policy_rbac.client.get(
        f"/api/v1/workspaces/{policy_rbac.workspace_id}/agents/"
        f"{policy_rbac.foreign_agent.id}/bundles"
    )
    configured = await policy_rbac.client.patch(
        _config_url(policy_rbac, policy_rbac.foreign_connection),
        json=CONFIG_PAYLOAD,
        headers=CSRF_HEADERS,
    )

    assert applied.status_code == 404, applied.text
    assert removed.status_code == 404, removed.text
    assert listed.status_code == 404, listed.text
    assert configured.status_code == 404, configured.text


async def test_admin_can_turn_a_bundle_on_and_off_through_the_routes(
    policy_rbac: PolicyRbacHarness,
    session: AsyncSession,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]

    applied = await policy_rbac.client.post(
        _bundle_url(policy_rbac, policy_rbac.agent), json={}, headers=CSRF_HEADERS
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["needs"] == []
    assert len(body["grants_created"]) == 5
    assert all(row["connection_name"] == "GitHub" for row in body["grants_created"])

    statuses = await policy_rbac.client.get(
        f"/api/v1/workspaces/{policy_rbac.workspace_id}/agents/{policy_rbac.agent.id}/bundles"
    )
    assert statuses.status_code == 200, statuses.text
    read = next(item for item in statuses.json() if item["id"] == "github-read")
    assert read["state"] == "on"

    preview = await policy_rbac.client.delete(
        _bundle_url(policy_rbac, policy_rbac.agent) + "?dry_run=true", headers=CSRF_HEADERS
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    # The fixture's own read row is a github-read capability too: turning the
    # bundle off means off, and the preview names it as hand-made.
    assert len(preview.json()["revoked"]) == 6
    assert [row["id"] for row in preview.json()["hand_made"]] == [
        str(policy_rbac.existing_grant.id)
    ]

    removed = await policy_rbac.client.delete(
        _bundle_url(policy_rbac, policy_rbac.agent), headers=CSRF_HEADERS
    )
    assert removed.status_code == 200, removed.text
    rows = list(
        await session.scalars(
            select(AgentCapabilityGrant).where(
                AgentCapabilityGrant.agent_id == policy_rbac.agent.id
            )
        )
    )
    assert rows == []


async def test_grant_out_reports_problems_through_the_route(
    policy_rbac: PolicyRbacHarness,
) -> None:
    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]

    listed = await policy_rbac.client.get(
        f"/api/v1/workspaces/{policy_rbac.workspace_id}/agents/{policy_rbac.agent.id}/grants"
    )

    assert listed.status_code == 200, listed.text
    (row,) = listed.json()
    assert row["problems"] == []
    assert row["connection_name"] is None


async def test_bundle_reads_show_connection_choices_only_to_callers_who_may_read_connections(
    policy_rbac: PolicyRbacHarness,
    session: AsyncSession,
) -> None:
    """A need's choices are the connection inventory — ids, names, statuses,
    allow-lists — that ``GET /connections`` keeps behind the admin role and
    the ``apps:read`` scope. The bundle reads stay viewer+ and readable with
    ``agents:read``; what is withheld from everyone else is the choices."""
    session.add(_connection(policy_rbac.workspace_id, "GitHub (second)"))
    await session.commit()
    ws = policy_rbac.workspace_id
    urls = (
        f"/api/v1/workspaces/{ws}/tools/bundles",
        f"/api/v1/workspaces/{ws}/agents/{policy_rbac.agent.id}/bundles",
    )

    async def github_read_choices(url: str) -> list[dict[str, Any]]:
        response = await policy_rbac.client.get(url)
        assert response.status_code == 200, response.text
        bundle = next(item for item in response.json() if item["id"] == "github-read")
        assert bundle["readiness"]["state"] == "needs"
        (need,) = bundle["readiness"]["needs"]
        assert need["kind"] == "choose"
        assert need["connector_type"] == "github"
        return list(need["choices"])

    for role in (WorkspaceRole.VIEWER, WorkspaceRole.MEMBER):
        policy_rbac.actor["user"] = policy_rbac.users[role.value]
        for url in urls:
            assert await github_read_choices(url) == []

    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]
    for url in urls:
        assert {choice["name"] for choice in await github_read_choices(url)} == {
            "GitHub",
            "GitHub (second)",
        }

    def key(*scopes: str) -> ApiKeyPrincipal:
        return ApiKeyPrincipal(
            id=new_uuid7(),
            workspace_id=ws,
            name="automation",
            prefix="jhin_test",
            role_ceiling=WorkspaceRole.ADMIN,
            scopes=frozenset(scopes),
        )

    policy_rbac.actor["api_key"] = key("agents:read")
    for url in urls:
        assert await github_read_choices(url) == []
    policy_rbac.actor["api_key"] = key("agents:read", "apps:read")
    for url in urls:
        assert len(await github_read_choices(url)) == 2

    # Applying answers the same question, under the same rule.
    policy_rbac.actor["api_key"] = key("agents:admin")
    applied = await policy_rbac.client.post(
        _bundle_url(policy_rbac, policy_rbac.agent), json={}, headers=CSRF_HEADERS
    )
    assert applied.status_code == 200, applied.text
    (need,) = applied.json()["needs"]
    assert need["kind"] == "choose"
    assert need["choices"] == []
    policy_rbac.actor.pop("api_key")


async def test_grant_rows_name_connections_only_to_callers_who_may_read_them(
    policy_rbac: PolicyRbacHarness,
    session: AsyncSession,
) -> None:
    """``connection_name`` and the sentences in ``problems`` can carry a
    connection's name and status -- the inventory ``GET /connections`` keeps
    behind the admin role and ``apps:read``. The grant and bundle reads stay
    viewer+; what a viewer, a member, or an ``agents:read`` key gets is the
    neutral form of the same facts."""
    ws = policy_rbac.workspace_id
    off = _connection(ws, "GitHub (off)")
    off.status = "disabled"
    session.add(off)
    await session.commit()

    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]
    created = await policy_rbac.client.post(
        f"/api/v1/workspaces/{ws}/agents/{policy_rbac.agent.id}/grants",
        json={"capability": "github.repository.read", "scope": {"connection_id": str(off.id)}},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    grant_id = created.json()["id"]
    grants_url = f"/api/v1/workspaces/{ws}/agents/{policy_rbac.agent.id}/grants"
    bundles_url = f"/api/v1/workspaces/{ws}/agents/{policy_rbac.agent.id}/bundles"

    async def row() -> dict[str, Any]:
        listed = await policy_rbac.client.get(grants_url)
        assert listed.status_code == 200, listed.text
        return next(item for item in listed.json() if item["id"] == grant_id)

    async def bundle_sentences() -> list[str]:
        listed = await policy_rbac.client.get(bundles_url)
        assert listed.status_code == 200, listed.text
        return [
            sentence
            for bundle in listed.json()
            for problem in bundle["problems"]
            for sentence in problem["problems"]
        ]

    for role in (WorkspaceRole.VIEWER, WorkspaceRole.MEMBER):
        policy_rbac.actor["user"] = policy_rbac.users[role.value]
        seen = await row()
        assert seen["connection_name"] is None
        assert seen["problems"] == ["The pinned connection is disabled."]
        assert all("GitHub (off)" not in sentence for sentence in await bundle_sentences())

    policy_rbac.actor["user"] = policy_rbac.users[WorkspaceRole.ADMIN.value]
    seen = await row()
    assert seen["connection_name"] == "GitHub (off)"
    assert seen["problems"] == ["Connection 'GitHub (off)' is disabled."]

    def key(*scopes: str) -> ApiKeyPrincipal:
        return ApiKeyPrincipal(
            id=new_uuid7(),
            workspace_id=ws,
            name="automation",
            prefix="jhin_test",
            role_ceiling=WorkspaceRole.ADMIN,
            scopes=frozenset(scopes),
        )

    policy_rbac.actor["api_key"] = key("agents:read")
    assert (await row())["connection_name"] is None
    policy_rbac.actor["api_key"] = key("agents:read", "apps:read")
    assert (await row())["connection_name"] == "GitHub (off)"
    policy_rbac.actor.pop("api_key")
