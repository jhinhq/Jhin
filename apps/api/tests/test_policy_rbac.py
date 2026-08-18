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
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import AuthContext, WorkspaceContext, get_current_auth, get_db
from jhin_api.policy.router import router as policy_router
from jhin_api.settings import Settings
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import WorkspaceRole, new_uuid7

CSRF_TOKEN = "policy-rbac-csrf"
CSRF_HEADERS = {"x-csrf-token": CSRF_TOKEN}
GRANT_PAYLOAD = {
    "capability": "github.issue.write",
    "scope": {"repository": "acme/api"},
    "effect": "allow",
}


@dataclass
class PolicyRbacHarness:
    client: httpx.AsyncClient
    actor: dict[str, User]
    users: dict[str, User]
    workspace_id: UUID
    agent: Agent
    existing_grant: AgentCapabilityGrant
    foreign_workspace: Workspace
    foreign_agent: Agent
    foreign_grant: AgentCapabilityGrant


@pytest.fixture
async def policy_rbac(
    session: AsyncSession, admin_ctx: WorkspaceContext
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
    session.add_all([existing_grant, foreign_grant])
    await session.commit()

    actor = {"user": users[WorkspaceRole.ADMIN.value]}
    app = FastAPI()
    app.state.settings = Settings()

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(policy_router)

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
    app.dependency_overrides[get_current_auth] = override_auth
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
