"""Invitations, membership authority, scoped API keys, and role boundaries.

One harness, because these are one feature: who may act in a workspace, and
what a credential minted inside it is allowed to do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jhin_api.access.keys import generate_key
from jhin_api.access.router import (
    api_keys_router,
    invitations_router,
    public_invitations_router,
)
from jhin_api.agents.router import router as agents_router
from jhin_api.auth.router import router as auth_router
from jhin_api.deps import (
    DbSession,
    Principal,
    WorkspaceContext,
    get_current_principal,
    get_db,
)
from jhin_api.main import ApiKeyUsageMiddleware
from jhin_api.models.router import providers_router, spend_router
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.security.tokens import hash_token
from jhin_api.settings import Settings
from jhin_api.triggers.router import router as triggers_router
from jhin_api.workspaces.router import router as workspaces_router
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    ApiKey,
    ApiKeyUsage,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from jhin_domain import AgentStatus, UserStatus, WorkspaceRole, new_uuid7

CSRF = "access-control-csrf"
CSRF_HEADERS = {"x-csrf-token": CSRF}
GOOD_PASSWORD = "correct-horse-battery-staple"


@dataclass
class Harness:
    client: httpx.AsyncClient
    app: FastAPI
    session: AsyncSession
    actor: dict[str, User]
    users: dict[str, User]
    workspace: Workspace
    other_workspace: Workspace
    agent: Agent

    def act_as(self, role: WorkspaceRole) -> None:
        self.actor["user"] = self.users[role.value]

    @property
    def base(self) -> str:
        return f"/api/v1/workspaces/{self.workspace.id}"


def _user(role: str) -> User:
    return User(
        email=f"{role}-{new_uuid7().hex[:8]}@example.com",
        display_name=role.title(),
        password_hash="x",
    )


@pytest.fixture
async def access() -> AsyncIterator[Harness]:
    # StaticPool so the request session and the usage-middleware session share
    # one in-memory database, exactly as they share one Postgres in production.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session = maker()

    workspace = Workspace(name="Access", slug=f"access-{new_uuid7().hex[:8]}")
    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add_all([workspace, other])
    await session.flush()

    users = {role.value: _user(role.value) for role in WorkspaceRole}
    users["admin2"] = _user("admin2")
    users["member2"] = _user("member2")
    session.add_all(list(users.values()))
    await session.flush()
    for name, user in users.items():
        role = "admin" if name == "admin2" else "member" if name == "member2" else name
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=role))
    agent = Agent(
        workspace_id=workspace.id,
        name="Boundary Agent",
        slug=f"boundary-{new_uuid7().hex[:8]}",
        status=AgentStatus.ACTIVE.value,
    )
    session.add(agent)
    await session.commit()

    actor = {"user": users[WorkspaceRole.ADMIN.value]}
    settings = Settings()
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = maker
    app.state.api_key_limiter = LoginRateLimiter(
        account_max_attempts=settings.api_key_max_attempts,
        ip_max_attempts=settings.api_key_ip_max_attempts,
    )
    app.state.login_limiter = LoginRateLimiter(
        account_max_attempts=settings.login_max_attempts,
        ip_max_attempts=settings.login_ip_max_attempts,
    )
    app.add_middleware(ApiKeyUsageMiddleware)

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    for router in (
        auth_router,
        workspaces_router,
        invitations_router,
        public_invitations_router,
        api_keys_router,
        agents_router,
        providers_router,
        spend_router,
        triggers_router,
    ):
        app.include_router(router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_principal(request: Request, db: DbSession) -> Principal:
        # Bearer requests must exercise the real key path; everything else
        # stands in for a signed-in browser without minting cookies.
        if request.headers.get("authorization"):
            return await get_current_principal(request, db, settings)
        return Principal(user=actor["user"])

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = override_principal

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("jhin_csrf", CSRF)
        yield Harness(
            client=client,
            app=app,
            session=session,
            actor=actor,
            users=users,
            workspace=workspace,
            other_workspace=other,
            agent=agent,
        )
    await session.close()
    await engine.dispose()


# --------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------


async def test_invitation_round_trip_creates_the_account_and_the_membership(
    access: Harness,
) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    created = await access.client.post(
        f"{access.base}/invitations",
        json={"email": "New.Person@Example.com", "role": "member"},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    token = body["token"]
    assert body["invite_url"].endswith(f"/invite/{token}")
    assert body["invitation"]["email"] == "new.person@example.com"
    assert body["invitation"]["status"] == "pending"

    preview = await access.client.get(f"/api/v1/invitations/{token}")
    assert preview.status_code == 200
    # The public screen learns the workspace name and nothing else about it.
    assert preview.json() == {
        "workspace_name": access.workspace.name,
        "email": "new.person@example.com",
        "role": "member",
        "expires_at": preview.json()["expires_at"],
    }

    accepted = await access.client.post(
        f"/api/v1/invitations/{token}/accept",
        json={"display_name": "New Person", "password": GOOD_PASSWORD},
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["user"]["email"] == "new.person@example.com"
    assert accepted.json()["memberships"][0]["workspace_id"] == str(access.workspace.id)
    # Accepting signs the new account in rather than dumping them on a login form.
    assert "jhin_session" in accepted.cookies

    user = await access.session.scalar(select(User).where(User.email == "new.person@example.com"))
    assert user is not None
    membership = await access.session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == access.workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    assert membership is not None
    assert membership.role == WorkspaceRole.MEMBER.value


async def test_an_invitation_is_single_use(access: Harness) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    token = (
        await access.client.post(
            f"{access.base}/invitations",
            json={"email": "once@example.com", "role": "member"},
            headers=CSRF_HEADERS,
        )
    ).json()["token"]

    first = await access.client.post(
        f"/api/v1/invitations/{token}/accept",
        json={"display_name": "Once", "password": GOOD_PASSWORD},
    )
    second = await access.client.post(
        f"/api/v1/invitations/{token}/accept",
        json={"display_name": "Twice", "password": GOOD_PASSWORD},
    )
    assert first.status_code == 201
    assert second.status_code == 404


async def test_an_invitation_to_an_existing_account_cannot_be_accepted_anonymously(
    access: Harness,
) -> None:
    """Holding the link must not be enough to become an account that exists.

    An email address is public information, so if accepting were anonymous
    anyone able to send an invitation could aim one at an address they do not
    control, click it themselves, and be handed a signed-in session for that
    account -- along with every other workspace it belongs to. The invited
    account has to do the accepting itself.
    """
    access.act_as(WorkspaceRole.ADMIN)
    # Someone with a Jhin account who is not in this workspace yet -- the
    # ordinary reason to invite an address that already exists.
    victim = User(
        email="already.has.an.account@example.com",
        display_name="Already Has An Account",
        password_hash="argon2-placeholder",
        status=UserStatus.ACTIVE.value,
    )
    access.session.add(victim)
    await access.session.commit()

    token = (
        await access.client.post(
            f"{access.base}/invitations",
            json={"email": victim.email, "role": "admin"},
            headers=CSRF_HEADERS,
        )
    ).json()["token"]

    # No session cookie: whoever holds the link is nobody in particular.
    stolen = await access.client.post(
        f"/api/v1/invitations/{token}/accept",
        json={"display_name": "Not Them", "password": GOOD_PASSWORD},
    )
    assert stolen.status_code == 401, stolen.text
    assert "jhin_session" not in stolen.cookies
    # And the refusal must not have quietly changed the victim's credential.
    await access.session.refresh(victim)
    assert victim.password_hash is not None

    # The link is still pending, so the real invitee can sign in and use it.
    assert (await access.client.get(f"/api/v1/invitations/{token}")).status_code == 200


async def test_expired_revoked_and_unknown_tokens_are_indistinguishable(
    access: Harness,
) -> None:
    access.act_as(WorkspaceRole.ADMIN)

    async def make(email: str) -> tuple[str, UUID]:
        body = (
            await access.client.post(
                f"{access.base}/invitations",
                json={"email": email, "role": "member"},
                headers=CSRF_HEADERS,
            )
        ).json()
        return body["token"], UUID(body["invitation"]["id"])

    expired_token, expired_id = await make("expired@example.com")
    revoked_token, revoked_id = await make("revoked@example.com")

    expired = await access.session.get(WorkspaceInvitation, expired_id)
    assert expired is not None
    expired.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await access.session.commit()

    assert (
        await access.client.delete(f"{access.base}/invitations/{revoked_id}", headers=CSRF_HEADERS)
    ).status_code == 204

    responses = [
        await access.client.get(f"/api/v1/invitations/{expired_token}"),
        await access.client.get(f"/api/v1/invitations/{revoked_token}"),
        await access.client.get("/api/v1/invitations/not-a-real-token"),
    ]
    assert [r.status_code for r in responses] == [404, 404, 404]
    assert len({r.json()["detail"] for r in responses}) == 1


async def test_only_the_stored_hash_of_an_invitation_token_is_persisted(
    access: Harness,
) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    token = (
        await access.client.post(
            f"{access.base}/invitations",
            json={"email": "hashed@example.com", "role": "member"},
            headers=CSRF_HEADERS,
        )
    ).json()["token"]

    stored = await access.session.scalar(
        select(WorkspaceInvitation).where(WorkspaceInvitation.email == "hashed@example.com")
    )
    assert stored is not None
    assert stored.token_hash == hash_token(token)
    assert token not in stored.token_hash
    # Listing an invitation never re-reveals the link.
    listed = await access.client.get(f"{access.base}/invitations")
    assert token not in listed.text


async def test_re_inviting_the_same_address_supersedes_the_outstanding_link(
    access: Harness,
) -> None:
    access.act_as(WorkspaceRole.ADMIN)

    async def invite() -> str:
        return (
            await access.client.post(
                f"{access.base}/invitations",
                json={"email": "again@example.com", "role": "member"},
                headers=CSRF_HEADERS,
            )
        ).json()["token"]

    first = await invite()
    second = await invite()

    assert (await access.client.get(f"/api/v1/invitations/{first}")).status_code == 404
    assert (await access.client.get(f"/api/v1/invitations/{second}")).status_code == 200


async def test_accepting_with_a_weak_password_is_refused_and_creates_nothing(
    access: Harness,
) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    token = (
        await access.client.post(
            f"{access.base}/invitations",
            json={"email": "weak@example.com", "role": "member"},
            headers=CSRF_HEADERS,
        )
    ).json()["token"]

    refused = await access.client.post(
        f"/api/v1/invitations/{token}/accept",
        json={"display_name": "Weak", "password": "short"},
    )
    assert refused.status_code == 422
    assert await access.session.scalar(select(User).where(User.email == "weak@example.com")) is None
    # The link survives a rejected attempt so the invitee can try again.
    assert (await access.client.get(f"/api/v1/invitations/{token}")).status_code == 200


@pytest.mark.parametrize("role", [WorkspaceRole.VIEWER, WorkspaceRole.MEMBER])
async def test_only_admins_can_invite(access: Harness, role: WorkspaceRole) -> None:
    access.act_as(role)
    refused = await access.client.post(
        f"{access.base}/invitations",
        json={"email": "nope@example.com", "role": "member"},
        headers=CSRF_HEADERS,
    )
    listed = await access.client.get(f"{access.base}/invitations")
    assert refused.status_code == 403
    assert listed.status_code == 403


async def test_an_admin_may_invite_an_admin_but_not_an_owner(access: Harness) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    as_admin = await access.client.post(
        f"{access.base}/invitations",
        json={"email": "new-admin@example.com", "role": "admin"},
        headers=CSRF_HEADERS,
    )
    as_owner = await access.client.post(
        f"{access.base}/invitations",
        json={"email": "new-owner@example.com", "role": "owner"},
        headers=CSRF_HEADERS,
    )
    assert as_admin.status_code == 201, as_admin.text
    assert as_owner.status_code == 403

    access.act_as(WorkspaceRole.OWNER)
    owner_invite = await access.client.post(
        f"{access.base}/invitations",
        json={"email": "new-owner@example.com", "role": "owner"},
        headers=CSRF_HEADERS,
    )
    assert owner_invite.status_code == 201, owner_invite.text


# --------------------------------------------------------------------------
# Membership authority and the last-owner rule
# --------------------------------------------------------------------------


async def _membership_id(access: Harness, user: User) -> UUID:
    membership = await access.session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == access.workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    assert membership is not None
    return membership.id


async def test_the_last_owner_cannot_be_demoted_or_removed(access: Harness) -> None:
    access.act_as(WorkspaceRole.OWNER)
    owner_membership = await _membership_id(access, access.users["owner"])

    demoted = await access.client.patch(
        f"{access.base}/members/{owner_membership}",
        json={"role": "admin"},
        headers=CSRF_HEADERS,
    )
    removed = await access.client.delete(
        f"{access.base}/members/{owner_membership}", headers=CSRF_HEADERS
    )
    assert demoted.status_code == 409
    assert removed.status_code == 409

    still = await access.session.scalar(
        select(WorkspaceMembership).where(WorkspaceMembership.id == owner_membership)
    )
    assert still is not None
    assert still.role == WorkspaceRole.OWNER.value


async def test_an_owner_may_step_down_once_a_second_owner_exists(access: Harness) -> None:
    access.act_as(WorkspaceRole.OWNER)
    promoted = await access.client.patch(
        f"{access.base}/members/{await _membership_id(access, access.users['admin'])}",
        json={"role": "owner"},
        headers=CSRF_HEADERS,
    )
    assert promoted.status_code == 200, promoted.text

    stepped_down = await access.client.patch(
        f"{access.base}/members/{await _membership_id(access, access.users['owner'])}",
        json={"role": "admin"},
        headers=CSRF_HEADERS,
    )
    assert stepped_down.status_code == 200, stepped_down.text


async def test_an_admin_cannot_demote_or_remove_a_peer_admin(access: Harness) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    peer = await _membership_id(access, access.users["admin2"])

    demoted = await access.client.patch(
        f"{access.base}/members/{peer}", json={"role": "viewer"}, headers=CSRF_HEADERS
    )
    removed = await access.client.delete(f"{access.base}/members/{peer}", headers=CSRF_HEADERS)
    assert demoted.status_code == 403
    assert removed.status_code == 403

    access.act_as(WorkspaceRole.OWNER)
    by_owner = await access.client.patch(
        f"{access.base}/members/{peer}", json={"role": "viewer"}, headers=CSRF_HEADERS
    )
    assert by_owner.status_code == 200, by_owner.text


async def test_an_admin_may_promote_a_member_and_step_down_themselves(
    access: Harness,
) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    promoted = await access.client.patch(
        f"{access.base}/members/{await _membership_id(access, access.users['member'])}",
        json={"role": "admin"},
        headers=CSRF_HEADERS,
    )
    self_demoted = await access.client.patch(
        f"{access.base}/members/{await _membership_id(access, access.users['admin'])}",
        json={"role": "member"},
        headers=CSRF_HEADERS,
    )
    assert promoted.status_code == 200, promoted.text
    assert self_demoted.status_code == 200, self_demoted.text


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------


async def _mint(
    access: Harness, role: WorkspaceRole, scopes: list[str], **extra: object
) -> dict[str, Any]:
    access.act_as(role)
    response = await access.client.post(
        f"{access.base}/api-keys",
        json={"name": f"{role.value} key", "scopes": scopes, **extra},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


async def test_a_key_is_revealed_once_and_never_again(access: Harness) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:read"])
    plaintext = created["key"]
    assert plaintext.startswith(f"jhin_{created['api_key']['prefix']}_")

    listed = await access.client.get(f"{access.base}/api-keys")
    assert listed.status_code == 200
    assert plaintext not in listed.text
    assert listed.json()[0]["prefix"] == created["api_key"]["prefix"]

    stored = await access.session.scalar(
        select(ApiKey).where(ApiKey.prefix == created["api_key"]["prefix"])
    )
    assert stored is not None
    assert plaintext not in stored.key_hash
    assert stored.key_hash == hash_token(plaintext.split("_", 2)[2])


async def test_a_valid_key_authenticates_and_an_out_of_scope_call_is_refused(
    access: Harness,
) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:read"])
    key = created["key"]

    allowed = await access.client.get(f"{access.base}/agents", headers=_bearer(key))
    refused = await access.client.get(f"{access.base}/model-providers", headers=_bearer(key))
    assert allowed.status_code == 200, allowed.text
    assert refused.status_code == 403
    assert "models:read" in refused.json()["detail"]


async def test_a_read_scope_does_not_buy_a_write(access: Harness) -> None:
    key = (await _mint(access, WorkspaceRole.ADMIN, ["agents:read"]))["key"]
    refused = await access.client.post(
        f"{access.base}/agents/{access.agent.id}/pause", headers=_bearer(key)
    )
    assert refused.status_code == 403
    assert "agents:write" in refused.json()["detail"]


async def test_a_wildcard_grants_its_whole_category_and_nothing_else(
    access: Harness,
) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:*"])
    assert set(created["api_key"]["scopes"]) == {"agents:read", "agents:write", "agents:admin"}
    key = created["key"]
    assert (
        await access.client.post(
            f"{access.base}/agents/{access.agent.id}/pause", headers=_bearer(key)
        )
    ).status_code == 200
    assert (
        await access.client.get(f"{access.base}/triggers", headers=_bearer(key))
    ).status_code == 403


async def test_a_member_cannot_mint_a_key_carrying_admin_scopes(access: Harness) -> None:
    access.act_as(WorkspaceRole.MEMBER)
    refused = await access.client.post(
        f"{access.base}/api-keys",
        json={"name": "escalate", "scopes": ["chats:write", "audit:read"]},
        headers=CSRF_HEADERS,
    )
    assert refused.status_code == 422
    assert "audit:read" in refused.json()["detail"]


async def test_a_member_wildcard_key_is_capped_at_member_scopes(access: Harness) -> None:
    created = await _mint(access, WorkspaceRole.MEMBER, ["memories:*"])
    scopes = set(created["api_key"]["scopes"])
    assert scopes == {"memories:read", "memories:write"}
    assert "memories:admin" not in scopes
    assert created["api_key"]["role_ceiling"] == "member"


async def test_a_key_loses_power_the_moment_its_creator_is_demoted(
    access: Harness,
) -> None:
    key = (await _mint(access, WorkspaceRole.ADMIN, ["agents:write"]))["key"]
    assert (
        await access.client.post(
            f"{access.base}/agents/{access.agent.id}/pause", headers=_bearer(key)
        )
    ).status_code == 200

    access.act_as(WorkspaceRole.OWNER)
    demoted = await access.client.patch(
        f"{access.base}/members/{await _membership_id(access, access.users['admin'])}",
        json={"role": "member"},
        headers=CSRF_HEADERS,
    )
    assert demoted.status_code == 200, demoted.text

    after = await access.client.post(
        f"{access.base}/agents/{access.agent.id}/resume", headers=_bearer(key)
    )
    assert after.status_code == 403


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (lambda record: setattr(record, "revoked_at", datetime.now(UTC)), "revoked"),
        (
            lambda record: setattr(record, "expires_at", datetime.now(UTC) - timedelta(seconds=1)),
            "expired",
        ),
    ],
)
async def test_revoked_and_expired_keys_are_rejected_with_one_neutral_message(
    access: Harness, mutate: Any, label: str
) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:read"])
    record = await access.session.scalar(
        select(ApiKey).where(ApiKey.prefix == created["api_key"]["prefix"])
    )
    assert record is not None
    mutate(record)
    await access.session.commit()

    response = await access.client.get(f"{access.base}/agents", headers=_bearer(created["key"]))
    assert response.status_code == 401, label
    assert response.json()["detail"] == "Invalid or expired API key"


async def test_a_garbage_or_unknown_key_is_rejected_the_same_way(access: Harness) -> None:
    unknown = generate_key().plaintext
    for candidate in ("jhin_deadbeef_nope", unknown, "Bearer-nonsense"):
        response = await access.client.get(f"{access.base}/agents", headers=_bearer(candidate))
        assert response.status_code == 401, candidate
        assert response.json()["detail"] in {
            "Invalid or expired API key",
            "Not authenticated",
        }


async def test_a_key_cannot_reach_another_workspace(access: Harness) -> None:
    key = (await _mint(access, WorkspaceRole.ADMIN, ["agents:read"]))["key"]
    response = await access.client.get(
        f"/api/v1/workspaces/{access.other_workspace.id}/agents", headers=_bearer(key)
    )
    # 404, not 403: a key must not confirm that another workspace exists.
    assert response.status_code == 404


async def test_credential_endpoints_are_sealed_against_every_key(access: Harness) -> None:
    key = (await _mint(access, WorkspaceRole.OWNER, ["models:*", "spend:read"]))["key"]
    response = await access.client.post(
        f"{access.base}/model-providers/verify-draft",
        json={"type": "openai", "api_key": "sk-not-a-real-key"},
        headers=_bearer(key),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "This endpoint is not available to API keys"


async def test_a_key_cannot_mint_another_key(access: Harness) -> None:
    key = (await _mint(access, WorkspaceRole.OWNER, ["api_keys:*"]))["key"]
    response = await access.client.post(
        f"{access.base}/api-keys",
        json={"name": "spawn", "scopes": ["agents:read"]},
        headers=_bearer(key),
    )
    assert response.status_code == 403
    assert "cannot create other API keys" in response.json()["detail"]


async def test_revoking_a_key_takes_effect_immediately(access: Harness) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:read"])
    key = created["key"]
    assert (
        await access.client.get(f"{access.base}/agents", headers=_bearer(key))
    ).status_code == 200

    access.act_as(WorkspaceRole.ADMIN)
    revoked = await access.client.delete(
        f"{access.base}/api-keys/{created['api_key']['id']}", headers=CSRF_HEADERS
    )
    assert revoked.status_code == 204
    assert (
        await access.client.get(f"{access.base}/agents", headers=_bearer(key))
    ).status_code == 401


async def test_expiry_is_taken_as_an_amount_and_a_unit(access: Harness) -> None:
    created = await _mint(
        access, WorkspaceRole.ADMIN, ["agents:read"], expires_in=30, expires_unit="minutes"
    )
    expires_at = created["api_key"]["expires_at"]
    assert expires_at is not None

    forever = await _mint(access, WorkspaceRole.ADMIN, ["agents:read"], expires_unit="never")
    assert forever["api_key"]["expires_at"] is None

    access.act_as(WorkspaceRole.ADMIN)
    missing_amount = await access.client.post(
        f"{access.base}/api-keys",
        json={"name": "bad", "scopes": ["agents:read"], "expires_unit": "days"},
        headers=CSRF_HEADERS,
    )
    assert missing_amount.status_code == 422


async def test_unknown_scope_strings_are_refused_at_the_boundary(access: Harness) -> None:
    access.act_as(WorkspaceRole.OWNER)
    response = await access.client.post(
        f"{access.base}/api-keys",
        json={"name": "typo", "scopes": ["agents:reed", "*"]},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 422
    assert "agents:reed" in response.text


async def test_the_scope_catalog_marks_what_this_role_may_not_grant(
    access: Harness,
) -> None:
    access.act_as(WorkspaceRole.MEMBER)
    catalog = await access.client.get(f"{access.base}/api-keys/scopes")
    assert catalog.status_code == 200
    body = catalog.json()
    assert body["your_role"] == "member"
    flat = {scope["key"]: scope for category in body["categories"] for scope in category["scopes"]}
    assert flat["chats:write"]["available"] is True
    assert flat["audit:read"]["available"] is False
    assert flat["audit:read"]["min_role"] == "admin"
    # Every scope carries plain-language copy the UI can render as-is.
    assert all(scope["label"] and scope["description"] for scope in flat.values())


# --------------------------------------------------------------------------
# CSRF interaction
# --------------------------------------------------------------------------


async def test_a_bearer_request_needs_no_csrf_header(access: Harness) -> None:
    key = (await _mint(access, WorkspaceRole.ADMIN, ["agents:write"]))["key"]
    response = await access.client.post(
        f"{access.base}/agents/{access.agent.id}/pause", headers=_bearer(key)
    )
    assert response.status_code == 200, response.text


async def test_a_cookie_session_is_still_csrf_protected_even_with_a_bearer_header(
    access: Harness,
) -> None:
    key = (await _mint(access, WorkspaceRole.ADMIN, ["agents:write"]))["key"]
    access.client.cookies.set("jhin_session", "a-browser-session")
    try:
        response = await access.client.post(
            f"{access.base}/agents/{access.agent.id}/pause", headers=_bearer(key)
        )
    finally:
        access.client.cookies.delete("jhin_session")
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token missing or invalid"


# --------------------------------------------------------------------------
# Usage log
# --------------------------------------------------------------------------


async def _usage_for(access: Harness, role: WorkspaceRole) -> list[dict[str, Any]]:
    access.act_as(role)
    response = await access.client.get(f"{access.base}/api-keys/usage", params={"limit": 200})
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def test_usage_is_recorded_for_allowed_and_denied_calls_alike(
    access: Harness,
) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:read"])
    key = created["key"]
    await access.client.get(f"{access.base}/agents", headers=_bearer(key))
    await access.client.get(f"{access.base}/model-providers", headers=_bearer(key))

    rows = await _usage_for(access, WorkspaceRole.OWNER)
    statuses = sorted(row["status_code"] for row in rows)
    assert statuses == [200, 403]
    assert all(row["api_key_prefix"] == created["api_key"]["prefix"] for row in rows)
    # The route template is logged, never the raw URL with its query string.
    assert {row["path"] for row in rows} <= {
        "/api/v1/workspaces/{workspace_id}/agents",
        "/api/v1/workspaces/{workspace_id}/model-providers",
    }


async def test_usage_visibility_follows_the_role_of_the_reader(access: Harness) -> None:
    async def call(role: WorkspaceRole | str) -> None:
        actual = role if isinstance(role, WorkspaceRole) else WorkspaceRole.ADMIN
        if isinstance(role, str):
            access.actor["user"] = access.users[role]
        else:
            access.act_as(actual)
        response = await access.client.post(
            f"{access.base}/api-keys",
            json={"name": f"{role} key", "scopes": ["agents:read"]},
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 201, response.text
        await access.client.get(f"{access.base}/agents", headers=_bearer(response.json()["key"]))

    await call(WorkspaceRole.OWNER)
    await call(WorkspaceRole.ADMIN)
    await call("admin2")
    await call(WorkspaceRole.MEMBER)
    await call(WorkspaceRole.VIEWER)

    ids = {name: str(user.id) for name, user in access.users.items()}

    owner_view = {row["acting_user_id"] for row in await _usage_for(access, WorkspaceRole.OWNER)}
    assert owner_view == {
        ids["owner"],
        ids["admin"],
        ids["admin2"],
        ids["member"],
        ids["viewer"],
    }

    admin_view = {row["acting_user_id"] for row in await _usage_for(access, WorkspaceRole.ADMIN)}
    # Own calls plus everyone below; never the owner's or a peer admin's.
    assert admin_view == {ids["admin"], ids["member"], ids["viewer"]}
    assert ids["owner"] not in admin_view
    assert ids["admin2"] not in admin_view

    member_view = {row["acting_user_id"] for row in await _usage_for(access, WorkspaceRole.MEMBER)}
    assert member_view == {ids["member"]}

    viewer_view = {row["acting_user_id"] for row in await _usage_for(access, WorkspaceRole.VIEWER)}
    assert viewer_view == {ids["viewer"]}


async def test_usage_never_crosses_workspaces(access: Harness) -> None:
    key = (await _mint(access, WorkspaceRole.ADMIN, ["agents:read"]))["key"]
    await access.client.get(f"{access.base}/agents", headers=_bearer(key))

    stray = ApiKeyUsage(
        workspace_id=access.other_workspace.id,
        api_key_id=uuid4(),
        acting_user_id=access.users["owner"].id,
        method="GET",
        path="/api/v1/workspaces/{workspace_id}/agents",
        status_code=200,
    )
    access.session.add(stray)
    await access.session.commit()

    rows = await _usage_for(access, WorkspaceRole.OWNER)
    assert all(row["id"] != str(stray.id) for row in rows)


# --------------------------------------------------------------------------
# Corrected role boundaries (regressions for the audit)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "lowest_allowed"),
    [
        ("POST", "/agents/{agent}/pause", WorkspaceRole.ADMIN),
        ("POST", "/agents/{agent}/resume", WorkspaceRole.ADMIN),
        ("GET", "/model-providers", WorkspaceRole.ADMIN),
        ("GET", "/spend", WorkspaceRole.VIEWER),
        ("GET", "/agents", WorkspaceRole.VIEWER),
        ("GET", "/triggers", WorkspaceRole.VIEWER),
    ],
)
async def test_endpoint_role_floors(
    access: Harness, method: str, suffix: str, lowest_allowed: WorkspaceRole
) -> None:
    url = access.base + suffix.replace("{agent}", str(access.agent.id))
    below = {
        WorkspaceRole.VIEWER: [],
        WorkspaceRole.MEMBER: [WorkspaceRole.VIEWER],
        WorkspaceRole.ADMIN: [WorkspaceRole.VIEWER, WorkspaceRole.MEMBER],
    }[lowest_allowed]

    for role in below:
        access.act_as(role)
        denied = await access.client.request(method, url, headers=CSRF_HEADERS)
        assert denied.status_code == 403, f"{role.value} reached {method} {suffix}"

    access.act_as(lowest_allowed)
    allowed = await access.client.request(method, url, headers=CSRF_HEADERS)
    assert allowed.status_code < 400, f"{lowest_allowed.value} blocked from {method} {suffix}"


async def test_a_viewer_can_read_triggers_but_not_test_one(access: Harness) -> None:
    access.act_as(WorkspaceRole.VIEWER)
    assert (await access.client.get(f"{access.base}/triggers")).status_code == 200

    access.act_as(WorkspaceRole.MEMBER)
    tested = await access.client.post(
        f"{access.base}/triggers/{uuid4()}/test",
        json={"event": {}},
        headers=CSRF_HEADERS,
    )
    assert tested.status_code == 403


async def test_workspace_context_carries_the_effective_scope_set(access: Harness) -> None:
    """The context handed to services is already intersected, not raw."""
    created = await _mint(access, WorkspaceRole.MEMBER, ["memories:*", "chats:read"])
    record = await access.session.scalar(
        select(ApiKey).where(ApiKey.prefix == created["api_key"]["prefix"])
    )
    assert record is not None
    assert set(record.scopes_json) == {"memories:read", "memories:write", "chats:read"}
    assert WorkspaceRole(record.role_ceiling) is WorkspaceRole.MEMBER


def test_workspace_context_defaults_to_no_api_key() -> None:
    """Session-authenticated contexts must not look like key contexts."""
    ctx = WorkspaceContext(
        user=User(email="a@b.c", display_name="A", password_hash="x"),
        workspace_id=uuid4(),
        role=WorkspaceRole.ADMIN,
    )
    assert ctx.api_key is None


async def test_hammering_a_bad_invitation_token_is_eventually_locked_out(
    access: Harness,
) -> None:
    """Guessing is already hopeless; this bounds the free work an address can buy."""
    limiter: LoginRateLimiter = access.app.state.login_limiter
    limiter.ip_max_attempts = 3
    statuses = [
        (await access.client.get("/api/v1/invitations/definitely-not-a-real-token")).status_code
        for _ in range(6)
    ]
    assert statuses[0] == 404
    assert statuses[-1] == 429


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
#
# `GET /auth/me` is session-only, so a key-holding client (the desktop app,
# a CLI) has no first call it can make: it knows neither its user nor the
# workspace every other route is keyed by. `GET /auth/identity` is that call,
# and what matters is that it reports the *effective* authority rather than
# whatever was requested at creation.

IDENTITY = "/api/v1/auth/identity"


async def test_a_key_can_read_its_own_identity(access: Harness) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["chats:read", "tasks:read"])
    response = await access.client.get(IDENTITY, headers=_bearer(created["key"]))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == access.users["admin"].email
    # Exactly the workspace the key is bound to — never the creator's others.
    assert [m["workspace_id"] for m in body["memberships"]] == [str(access.workspace.id)]
    assert body["memberships"][0]["workspace_name"] == access.workspace.name
    assert body["api_key"]["prefix"] == created["api_key"]["prefix"]
    assert body["api_key"]["workspace_id"] == str(access.workspace.id)
    assert set(body["api_key"]["scopes"]) == {"chats:read", "tasks:read"}


async def test_identity_is_reachable_at_any_scope(access: Harness) -> None:
    """Not scope-gated: a key that can do almost nothing can still boot a client."""
    created = await _mint(access, WorkspaceRole.VIEWER, ["chats:read"])
    response = await access.client.get(IDENTITY, headers=_bearer(created["key"]))
    assert response.status_code == 200, response.text
    assert response.json()["api_key"]["scopes"] == ["chats:read"]


async def test_identity_never_echoes_the_key_itself(access: Harness) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:read"])
    response = await access.client.get(IDENTITY, headers=_bearer(created["key"]))
    assert created["key"] not in response.text


async def test_a_session_sees_every_workspace_and_no_key(access: Harness) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    response = await access.client.get(IDENTITY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key"] is None
    assert [m["workspace_id"] for m in body["memberships"]] == [str(access.workspace.id)]


async def test_the_reported_membership_role_is_the_keys_ceiling(
    access: Harness,
) -> None:
    """The role a client gates its UI on is the key's, not the workspace's top role.

    A viewer's key in a workspace that also has owners must render a viewer.
    """
    created = await _mint(access, WorkspaceRole.VIEWER, ["chats:read"])
    response = await access.client.get(IDENTITY, headers=_bearer(created["key"]))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key"]["role_ceiling"] == "viewer"
    assert body["memberships"][0]["role"] == "viewer"


async def test_demoting_the_creator_narrows_what_identity_reports(
    access: Harness,
) -> None:
    """The same capping every other route applies, so the client is told the truth."""
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:read", "agents:admin"])
    assert set(created["api_key"]["scopes"]) == {"agents:read", "agents:admin"}

    membership = await access.session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == access.workspace.id,
            WorkspaceMembership.user_id == access.users["admin"].id,
        )
    )
    assert membership is not None
    membership.role = WorkspaceRole.MEMBER.value
    await access.session.commit()

    response = await access.client.get(IDENTITY, headers=_bearer(created["key"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key"]["role_ceiling"] == "member"
    # `agents:admin` needs admin, so it is gone rather than merely unusable.
    assert body["api_key"]["scopes"] == ["agents:read"]


async def test_a_key_whose_creator_left_the_workspace_is_told_why(
    access: Harness,
) -> None:
    created = await _mint(access, WorkspaceRole.ADMIN, ["agents:read"])

    membership = await access.session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == access.workspace.id,
            WorkspaceMembership.user_id == access.users["admin"].id,
        )
    )
    assert membership is not None
    await access.session.delete(membership)
    await access.session.commit()

    response = await access.client.get(IDENTITY, headers=_bearer(created["key"]))
    assert response.status_code == 403, response.text
    assert "no longer a member" in response.json()["detail"]


async def test_a_bad_key_never_falls_back_to_a_session_on_identity(
    access: Harness,
) -> None:
    access.act_as(WorkspaceRole.ADMIN)
    response = await access.client.get(IDENTITY, headers=_bearer("jhin_deadbeef_nope"))
    assert response.status_code == 401, response.text
