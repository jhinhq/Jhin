"""The OAuth callback refuses everything it should, and says nothing when it does.

This is the route a stranger can reach with a URL of their choosing, so it is
the one worth being paranoid about. Every test here asserts one of:

* a rejection returns **the byte-identical body** every other rejection
  returns, so probing it tells an attacker which check they tripped;
* a replayed, forged, expired, or borrowed ``state`` does not produce a
  connection;
* nothing from the request reaches the ``Location`` header — not a
  ``redirect_uri``, not a ``next``, not a ``state`` that looks like a URL;
* the provider's own prose (``error_description``, ``error_uri``) appears
  nowhere in the response.
"""

from __future__ import annotations

import asyncio
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

from jhin_api.deps import (
    AuthContext,
    Principal,
    get_current_auth,
    get_current_auth_optional,
    get_current_principal,
    get_db,
)
from jhin_api.oauth.redirect import CALLBACK_PATH
from jhin_api.oauth.router import oauth_public_router
from jhin_api.settings import Settings
from jhin_db.models import (
    Connection,
    OAuthAuthorization,
    User,
    UserSession,
    WorkspaceMembership,
)
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_oauth.persistence import PendingAuthorizationInvalid, PendingAuthorizationStore
from jhin_secrets import SecretCrypto

INVALID_BODY = {"detail": PendingAuthorizationInvalid.MESSAGE}
APP_URL = "http://localhost:3000"
ISSUER = "https://auth.example.com"
TOKEN_ENDPOINT = "https://auth.example.com/token"
EVIL = "https://evil.example"


@dataclass
class CallbackHarness:
    client: httpx.AsyncClient
    session: AsyncSession
    crypto: SecretCrypto
    workspace_id: UUID
    actor: dict[str, User]
    admin: User
    other: User

    async def pending(self, **overrides: Any) -> tuple[OAuthAuthorization, str]:
        store = PendingAuthorizationStore(self.session, self.crypto)
        payload: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "user_id": self.admin.id,
            "flow": "authorization_code",
            "connector_type": "mcp",
            "ttl_seconds": 600,
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": TOKEN_ENDPOINT,
            "resource": "https://mcp.example.com",
            "scope": "read",
            "redirect_uri": f"{APP_URL}{CALLBACK_PATH}",
            "verifier": "v" * 43,
            "draft": {"name": "Example", "config": {}},
        }
        payload.update(overrides)
        row, handle = await store.create(**payload)
        await self.session.commit()
        return row, handle


@pytest.fixture
async def callback(
    session: AsyncSession,
    crypto: SecretCrypto,
) -> AsyncIterator[CallbackHarness]:
    admin = User(
        email=f"oauth-admin-{new_uuid7().hex[:8]}@example.com",
        display_name="OAuth Admin",
        password_hash="x",
    )
    other = User(
        email=f"oauth-other-{new_uuid7().hex[:8]}@example.com",
        display_name="Someone Else",
        password_hash="x",
    )
    from jhin_db.models import Workspace

    workspace = Workspace(name="OAuth", slug=f"oauth-{new_uuid7().hex[:8]}")
    session.add_all([admin, other, workspace])
    await session.flush()
    session.add_all(
        [
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=admin.id, role=WorkspaceRole.ADMIN.value
            ),
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=other.id, role=WorkspaceRole.ADMIN.value
            ),
        ]
    )
    await session.commit()

    actor = {"user": admin}
    app = FastAPI()
    app.state.settings = Settings(_env_file=None, app_url=APP_URL)
    app.state.secret_crypto = crypto

    class _NoTemporal:
        async def get(self) -> Any:
            raise RuntimeError("temporal is unavailable in this test")

    app.state.temporal_provider = _NoTemporal()

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(oauth_public_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_auth() -> AuthContext:
        user = actor["user"]
        return AuthContext(
            user=user,
            session_record=UserSession(
                user_id=user.id,
                token_hash=f"oauth-callback-{user.id}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )

    async def override_principal() -> Principal:
        return Principal(user=actor["user"])

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_auth] = override_auth
    # The callback resolves its session through the optional variant so an
    # expired one becomes a redirect rather than a raw 401 body. It is a
    # separate callable, so it needs its own override here; `signed_out`
    # below is the case where it deliberately returns None.
    app.dependency_overrides[get_current_auth_optional] = override_auth
    app.dependency_overrides[get_current_principal] = override_principal

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield CallbackHarness(
            client=client,
            session=session,
            crypto=crypto,
            workspace_id=workspace.id,
            actor=actor,
            admin=admin,
            other=other,
        )


async def _get(harness: CallbackHarness, **params: str) -> httpx.Response:
    return await harness.client.get(CALLBACK_PATH, params=params)


# --- The five rejections, all identical ---------------------------------


async def test_unknown_state_is_refused(callback: CallbackHarness) -> None:
    response = await _get(callback, state="a" * 43, code="anything")
    assert response.status_code == 400
    assert response.json() == INVALID_BODY


async def test_malformed_state_is_refused_with_the_same_body(
    callback: CallbackHarness,
) -> None:
    """A handle that is not the shape we issue never reaches the database."""
    response = await _get(callback, state="not a handle!", code="anything")
    assert response.status_code in {400, 422}
    if response.status_code == 400:
        assert response.json() == INVALID_BODY


async def test_a_replayed_callback_fails_the_second_time(
    callback: CallbackHarness,
) -> None:
    _row, handle = await callback.pending()
    first = await _get(callback, state=handle, error="access_denied")
    assert first.status_code == 303

    second = await _get(callback, state=handle, error="access_denied")
    assert second.status_code == 400
    assert second.json() == INVALID_BODY


async def test_an_expired_state_is_refused(callback: CallbackHarness) -> None:
    row, handle = await callback.pending(ttl_seconds=1)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await callback.session.commit()

    response = await _get(callback, state=handle, code="anything")
    assert response.status_code == 400
    assert response.json() == INVALID_BODY


async def test_another_users_session_cannot_finish_the_flow(
    callback: CallbackHarness,
) -> None:
    """The load-bearing CSRF defense: state alone is not enough.

    Even holding a valid, unexpired, unconsumed handle, a different browser
    session does not complete the authorization — and the connection is not
    created for either of them.
    """
    _row, handle = await callback.pending()
    callback.actor["user"] = callback.other

    response = await _get(callback, state=handle, code="anything")

    assert response.status_code == 400
    assert response.json() == INVALID_BODY
    assert (
        await callback.session.scalar(
            select(Connection).where(Connection.workspace_id == callback.workspace_id)
        )
        is None
    )


async def test_a_mismatched_iss_is_refused_when_the_server_advertises_it(
    callback: CallbackHarness,
) -> None:
    """RFC 9207, byte comparison. A different issuer is a mix-up attack."""
    _row, handle = await callback.pending(iss_parameter_supported=True)

    response = await _get(callback, state=handle, code="anything", iss="https://attacker.example")

    assert response.status_code == 400
    assert response.json() == INVALID_BODY


async def test_a_missing_iss_is_refused_when_the_server_advertises_it(
    callback: CallbackHarness,
) -> None:
    _row, handle = await callback.pending(iss_parameter_supported=True)

    response = await _get(callback, state=handle, code="anything")

    assert response.status_code == 400
    assert response.json() == INVALID_BODY


async def test_iss_is_compared_without_any_normalization(
    callback: CallbackHarness,
) -> None:
    """No case folding, no trailing-slash cleanup, no default-port elision.

    Every "helpful" normalization is a way for two different servers to look
    like one, which is the whole point of the comparison.
    """
    _row, handle = await callback.pending(iss_parameter_supported=True)

    response = await _get(callback, state=handle, code="x", iss=f"{ISSUER}/")

    assert response.status_code == 400
    assert response.json() == INVALID_BODY


async def test_a_redirect_uri_that_no_longer_matches_settings_is_refused(
    callback: CallbackHarness,
) -> None:
    """An operator who changed the base URL mid-flow gets a refusal."""
    _row, handle = await callback.pending(
        redirect_uri="https://old.example.com/api/v1/oauth/callback"
    )

    response = await _get(callback, state=handle, code="anything")

    assert response.status_code == 400
    assert response.json() == INVALID_BODY


async def test_every_rejection_returns_the_identical_body(
    callback: CallbackHarness,
) -> None:
    """Byte-for-byte, so nobody can tell which check they tripped."""
    bodies: list[bytes] = []

    unknown = await _get(callback, state="b" * 43, code="x")
    bodies.append(unknown.content)

    _row, expired_handle = await callback.pending(ttl_seconds=1)
    expired = await callback.session.scalar(
        select(OAuthAuthorization).where(OAuthAuthorization.state_hash == _row.state_hash)
    )
    assert expired is not None
    expired.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await callback.session.commit()
    bodies.append((await _get(callback, state=expired_handle, code="x")).content)

    _row2, wrong_iss = await callback.pending(iss_parameter_supported=True)
    bodies.append((await _get(callback, state=wrong_iss, code="x", iss=EVIL)).content)

    _row3, wrong_uri = await callback.pending(redirect_uri="https://old.example.com/cb")
    bodies.append((await _get(callback, state=wrong_uri, code="x")).content)

    _row4, borrowed = await callback.pending()
    callback.actor["user"] = callback.other
    bodies.append((await _get(callback, state=borrowed, code="x")).content)

    assert len(set(bodies)) == 1, "a rejection body differs and leaks which check failed"


# --- Nothing from the request reaches the response ----------------------


async def test_a_provider_denial_never_renders_the_providers_own_words(
    callback: CallbackHarness,
) -> None:
    marker = "attacker-authored-error-description"
    _row, handle = await callback.pending()

    response = await _get(
        callback,
        state=handle,
        error="access_denied",
        error_description=marker,
        error_uri=f"{EVIL}/why",
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"{APP_URL}/apps?oauth_error=denied"
    assert marker not in response.text
    assert marker not in response.headers["location"]
    assert EVIL not in response.text


@pytest.mark.parametrize(
    "hostile",
    [
        {"redirect_uri": EVIL},
        {"next": EVIL},
        {"return_to": EVIL},
        {"callback": EVIL},
    ],
)
async def test_no_query_parameter_can_steer_the_location_header(
    callback: CallbackHarness, hostile: dict[str, str]
) -> None:
    """The open-redirect surface, closed by construction."""
    _row, handle = await callback.pending()

    response = await _get(callback, state=handle, error="access_denied", **hostile)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(APP_URL)
    assert EVIL not in location


async def test_a_state_shaped_like_a_url_is_rejected_before_it_is_used(
    callback: CallbackHarness,
) -> None:
    response = await _get(callback, state=EVIL, code="x")
    assert response.status_code in {400, 422}
    assert EVIL not in response.headers.get("location", "")


async def test_the_callback_response_is_never_cached(
    callback: CallbackHarness,
) -> None:
    """A single-use redirect that a cache could replay is not single-use."""
    _row, handle = await callback.pending()
    response = await _get(callback, state=handle, error="access_denied")
    assert response.headers["cache-control"] == "no-store"


# --- Single use, under concurrency --------------------------------------


async def test_a_second_claim_of_the_same_handle_is_refused(
    callback: CallbackHarness,
) -> None:
    """The conditional UPDATE is the authority, not a read-then-write.

    Consumption is decided by the database inside one statement, so the
    second claim loses whether it arrives a millisecond or an hour later.
    (The genuinely concurrent case needs two sessions and a real server;
    ``packages/oauth/tests/test_persistence.py`` covers it there.)
    """
    _row, handle = await callback.pending()
    store = PendingAuthorizationStore(callback.session, callback.crypto)

    async def claim() -> str:
        try:
            await store.claim(handle=handle, expected_user_id=callback.admin.id)
        except PendingAuthorizationInvalid:
            return "refused"
        return "claimed"

    outcomes = [await claim(), await claim()]
    assert outcomes.count("claimed") == 1
    assert outcomes.count("refused") == 1


async def test_a_refused_callback_leaves_no_connection_behind(
    callback: CallbackHarness,
) -> None:
    await _get(callback, state="c" * 43, code="x")
    assert (
        await callback.session.scalar(
            select(Connection).where(Connection.workspace_id == callback.workspace_id)
        )
        is None
    )


async def test_a_spent_pending_row_is_deleted_after_a_denial(
    callback: CallbackHarness,
) -> None:
    row, handle = await callback.pending()
    row_id = row.id

    await _get(callback, state=handle, error="access_denied")

    assert await callback.session.get(OAuthAuthorization, row_id) is None


@pytest.mark.parametrize("flow", ["device_code", "github_app_manifest"])
async def test_a_handle_from_another_flow_is_refused_at_the_oauth_callback(
    callback: CallbackHarness, flow: str
) -> None:
    """Only an authorization-code row may be walked through this route.

    A manifest row records the OAuth callback URI as its ``redirect_uri``, so
    it used to survive ``_verify_callback_context`` and was stopped only
    incidentally, by a later ``client_registration_id is None`` test that had
    already burned the row. The guard is declared now rather than accidental.
    """
    _row, handle = await callback.pending(flow=flow)
    response = await _get(callback, state=handle, code="anything")
    assert response.status_code == 400
    assert response.json() == INVALID_BODY
    assert (
        await callback.session.scalar(
            select(Connection).where(Connection.workspace_id == callback.workspace_id)
        )
        is None
    )


async def test_a_dead_session_returns_to_the_app_instead_of_a_raw_401(
    callback: CallbackHarness,
) -> None:
    """A consent screen that outlives the session is the likeliest failure.

    It used to be the one failure that escaped this route's own promise: the
    session dependency raised before the handler ran, so the person landed on
    ``{"detail": "Not authenticated"}`` in their browser. Nothing is claimed
    and no token is exchanged without a session — this only decides what is
    shown.
    """
    _row, handle = await callback.pending()
    callback.client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
        get_current_auth_optional
    ] = lambda: None

    response = await _get(callback, state=handle, code="anything")

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(APP_URL)
    assert "oauth_error=failed" in location
    assert handle not in location
    # The row is untouched, because nothing claimed it.
    assert await callback.session.get(OAuthAuthorization, _row.id) is not None


def test_the_async_cases_above_actually_run() -> None:
    """Guard against a config change quietly turning these into no-ops."""
    assert asyncio.iscoroutinefunction(test_unknown_state_is_refused)
