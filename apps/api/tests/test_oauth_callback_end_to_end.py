"""A whole browser sign-in, through the real app, and every way it can end.

The other callback tests mount the OAuth router on a bare ``FastAPI``. This
one builds the application with ``create_app`` and drives it with an ASGI
client, so the middleware stack, the exception handlers, the route-scope
table, and the real dependency graph are all in the picture. That matters
here more than usual: the defect an operator hit was not in the handler's
logic at all — it was in what happened to a refusal on its way out, and a
harness that skips the layers a response passes through is a harness that
cannot see it.

Every case asserts the same three things before it asserts anything
specific: a 303, a ``Location`` on this instance's own Apps page, and an
empty body. There is no status and no input for which this route answers
JSON.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_api.deps import (
    AuthContext,
    Principal,
    get_current_auth,
    get_current_auth_optional,
    get_current_principal,
    get_db,
    get_secret_crypto,
)
from jhin_api.main import create_app
from jhin_api.oauth import service
from jhin_api.oauth.redirect import CALLBACK_PATH, redirect_uri
from jhin_api.settings import Settings
from jhin_connectors.oauth_providers import STATIC_PROVIDERS
from jhin_connectors.testing.fake_github_oauth import (
    FakeGitHubOAuthConfig,
    FakeGitHubOAuthServer,
)
from jhin_db.base import Base
from jhin_db.models import (
    Connection,
    OAuthAuthorization,
    OAuthClientRegistration,
    Secret,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_oauth.persistence import PendingAuthorizationStore
from jhin_secrets import SecretCrypto
from jhin_secrets.crypto import MasterKey, decode_master_key_material, generate_master_key_material

APP_URL = "http://localhost:3000"
ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
RIGHT_SECRET = "fake-github-client-secret-right"
WRONG_SECRET = "fake-github-client-secret-wrong"

#: Every response this module collected, so one assertion at the end can say
#: "and none of them was JSON" about the whole file rather than case by case.
COLLECTED: list[httpx.Response] = []


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        app_url=APP_URL,
        database_url="sqlite+aiosqlite:///:memory:",
        otel_exporter_otlp_endpoint=None,
    )


class _NoTemporal:
    async def get(self) -> Any:
        raise RuntimeError("temporal is unavailable in this test")


@dataclasses.dataclass
class Stack:
    """The real app, a real session, and the actors that drive it."""

    client: httpx.AsyncClient
    app: FastAPI
    session: AsyncSession
    crypto: SecretCrypto
    settings: Settings
    workspace_id: Any
    actor: dict[str, User]
    admin: User
    other: User
    auth_override: Any

    def sign_out(self) -> None:
        self.app.dependency_overrides[get_current_auth_optional] = lambda: None

    def sign_in(self) -> None:
        self.app.dependency_overrides[get_current_auth_optional] = self.auth_override

    async def get(self, path: str, **params: str) -> httpx.Response:
        response = await self.client.get(path, params=params)
        COLLECTED.append(response)
        return response


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubOAuthServer]:
    """GitHub's OAuth surface on loopback, with the provider table pointed at it."""
    with FakeGitHubOAuthServer(FakeGitHubOAuthConfig(expected_client_secret=RIGHT_SECRET)) as fake:
        monkeypatch.setenv(ALLOWLIST_ENV, fake.base_url)
        provider = dataclasses.replace(
            STATIC_PROVIDERS["github"],
            authorization_endpoint=fake.authorize_url,
            token_endpoint=fake.device_token_url,
        )
        monkeypatch.setattr(service, "_static_provider_for", lambda _connector_type: provider)
        yield fake


@pytest.fixture
async def stack(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stack]:
    settings = _settings()
    crypto = SecretCrypto(MasterKey(key=decode_master_key_material(generate_master_key_material())))
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session = maker()

    stamp = new_uuid7().hex[:8]
    admin = User(email=f"e2e-admin-{stamp}@example.com", display_name="A", password_hash="x")
    other = User(email=f"e2e-other-{stamp}@example.com", display_name="B", password_hash="x")
    workspace = Workspace(name="E2E", slug=f"e2e-{new_uuid7().hex[:8]}")
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
    session.expunge(admin)
    session.expunge(other)
    actor = {"user": admin}

    import jhin_api.main as main_module

    monkeypatch.setattr(main_module, "_load_secret_crypto", lambda: None)
    app = create_app(settings)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_auth() -> AuthContext:
        user = actor["user"]
        return AuthContext(
            user=user,
            session_record=UserSession(
                user_id=user.id,
                token_hash=f"e2e-{user.id}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )

    async def override_principal() -> Principal:
        return Principal(user=actor["user"])

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_auth] = override_auth
    app.dependency_overrides[get_current_auth_optional] = override_auth
    app.dependency_overrides[get_current_principal] = override_principal
    app.dependency_overrides[get_secret_crypto] = lambda: crypto

    async with app.router.lifespan_context(app):
        app.state.temporal_provider = _NoTemporal()
        app.state.secret_crypto = crypto
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Stack(
                client=client,
                app=app,
                session=session,
                crypto=crypto,
                settings=settings,
                workspace_id=workspace.id,
                actor=actor,
                admin=admin,
                other=other,
                auth_override=override_auth,
            )
    await session.close()
    await engine.dispose()


async def _register(stack: Stack, *, secret: str) -> OAuthClientRegistration:
    from jhin_api.deps import WorkspaceContext
    from jhin_api.oauth.schemas import OAuthClientCreate

    ctx = WorkspaceContext(
        user=stack.admin, workspace_id=stack.workspace_id, role=WorkspaceRole.ADMIN
    )
    created = await service.create_client(
        stack.session,
        stack.crypto,
        ctx,
        stack.settings,
        OAuthClientCreate(
            issuer=STATIC_PROVIDERS["github"].issuer,
            client_id="Iv23lixxxxxxxxxxxxxx",
            client_secret=secret,
            token_endpoint_auth_method="client_secret_post",
        ),
        request_id=new_uuid7(),
        ip_hash="0" * 64,
    )
    row = await stack.session.get(OAuthClientRegistration, created.id)
    assert row is not None
    return row


async def _start(stack: Stack) -> tuple[str, str]:
    """Start an authorization and follow the fake's redirect. Returns state, code."""
    from jhin_api.deps import WorkspaceContext
    from jhin_api.oauth.schemas import OAuthStartIn

    ctx = WorkspaceContext(
        user=stack.admin, workspace_id=stack.workspace_id, role=WorkspaceRole.ADMIN
    )
    async with httpx.AsyncClient() as http_client:
        started = await service.start_authorization(
            stack.session,
            stack.crypto,
            ctx,
            http_client,
            stack.settings,
            OAuthStartIn(connector_type="github", name="GitHub"),
            request_id=new_uuid7(),
            ip_hash="0" * 64,
        )
        consent = await http_client.get(started.authorization_url, follow_redirects=False)
    assert consent.status_code == 302
    sent_back_to = urlsplit(consent.headers["location"])
    assert f"{sent_back_to.scheme}://{sent_back_to.netloc}{sent_back_to.path}" == redirect_uri(
        stack.settings
    )
    query = parse_qs(sent_back_to.query)
    return query["state"][0], query["code"][0]


async def _walk(stack: Stack) -> httpx.Response:
    state, code = await _start(stack)
    return await stack.get(CALLBACK_PATH, state=state, code=code)


async def _connections(stack: Stack) -> list[Connection]:
    rows = await stack.session.scalars(
        select(Connection).where(Connection.workspace_id == stack.workspace_id)
    )
    return list(rows)


def _lands_on_apps(response: httpx.Response) -> str:
    """The three assertions every case makes, and the flag it landed with."""
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location == f"{APP_URL}/apps" or location.startswith(f"{APP_URL}/apps?"), location
    assert response.content == b""
    assert "application/json" not in response.headers.get("content-type", "")
    flags = parse_qs(urlsplit(location).query)
    return flags.get("oauth_error", [""])[0]


# --- The whole thing works ----------------------------------------------


async def test_a_connect_succeeds_end_to_end(stack: Stack, fake: FakeGitHubOAuthServer) -> None:
    registration = await _register(stack, secret=RIGHT_SECRET)

    response = await _walk(stack)

    assert _lands_on_apps(response) == ""
    (connection,) = await _connections(stack)
    assert response.headers["location"] == f"{APP_URL}/apps?connection={connection.public_id}"
    assert connection.auth_type == "oauth"
    assert connection.oauth_issuer == "https://github.com"
    assert connection.oauth_client_registration_id == registration.id

    body = fake.recorded_bodies()[-1]
    assert body["client_secret"] == RIGHT_SECRET
    assert body["code_verifier"]
    assert "resource" not in body


# --- And every way it can fail lands on a Jhin page ----------------------


async def test_an_unknown_state_lands_on_the_recovery_page(stack: Stack) -> None:
    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state="a" * 43, code="x")) == "expired"
    assert await _connections(stack) == []


async def test_a_missing_state_lands_on_the_recovery_page(stack: Stack) -> None:
    assert _lands_on_apps(await stack.get(CALLBACK_PATH)) == "expired"


async def test_an_over_long_state_lands_on_the_recovery_page(stack: Stack) -> None:
    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state="a" * 5000)) == "expired"


async def test_an_over_long_code_lands_on_the_recovery_page(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, _code = await _start(stack)

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code="c" * 3000)) == (
        "expired"
    )
    assert await _connections(stack) == []


async def test_an_expired_state_lands_on_the_recovery_page(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)
    row = await stack.session.scalar(select(OAuthAuthorization))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await stack.session.commit()

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code=code)) == "expired"
    assert await _connections(stack) == []


async def test_a_wrong_session_lands_on_the_recovery_page(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)
    stack.actor["user"] = stack.other

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code=code)) == "expired"
    assert await _connections(stack) == []


async def test_a_declined_consent_lands_on_denied(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, _code = await _start(stack)

    response = await stack.get(CALLBACK_PATH, state=state, error="access_denied")

    assert _lands_on_apps(response) == "denied"
    assert response.headers["location"].endswith("&app=github")
    assert await _connections(stack) == []


async def test_a_wrong_client_secret_lands_on_client_rejected(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=WRONG_SECRET)

    assert _lands_on_apps(await _walk(stack)) == "client_rejected"
    assert await _connections(stack) == []


async def test_a_callback_url_the_app_does_not_list_lands_on_callback_mismatch(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    fake.refuse_next_exchange("redirect_uri_mismatch")

    assert _lands_on_apps(await _walk(stack)) == "callback_mismatch"
    assert await _connections(stack) == []


async def test_a_registration_deleted_mid_flow_lands_on_registration_gone(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    registration = await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)
    await stack.session.delete(registration)
    await stack.session.commit()

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code=code)) == (
        "registration_gone"
    )
    assert await _connections(stack) == []


async def test_an_iss_mismatch_lands_on_issuer_mismatch(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)
    row = await stack.session.scalar(select(OAuthAuthorization))
    assert row is not None
    row.iss_parameter_supported = True
    await stack.session.commit()

    response = await stack.get(
        CALLBACK_PATH, state=state, code=code, iss="https://attacker.example"
    )

    assert _lands_on_apps(response) == "issuer_mismatch"
    assert "attacker.example" not in response.headers["location"]
    assert await _connections(stack) == []


async def test_a_redirect_uri_rewritten_mid_flow_lands_on_redirect_changed(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)
    row = await stack.session.scalar(select(OAuthAuthorization))
    assert row is not None
    row.redirect_uri = "https://moved.example.com/api/v1/oauth/callback"
    await stack.session.commit()

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code=code)) == (
        "redirect_changed"
    )
    assert await _connections(stack) == []


async def test_a_deleted_verifier_secret_lands_on_failed(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)
    row = await stack.session.scalar(select(OAuthAuthorization))
    assert row is not None
    secret = await stack.session.get(Secret, row.verifier_secret_id)
    assert secret is not None
    await stack.session.delete(secret)
    await stack.session.commit()

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code=code)) == "failed"
    assert await _connections(stack) == []


async def test_a_token_endpoint_the_policy_no_longer_allows_lands_on_failed(
    stack: Stack, fake: FakeGitHubOAuthServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)
    monkeypatch.setenv(ALLOWLIST_ENV, "http://nowhere.invalid")

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code=code)) == "failed"
    assert await _connections(stack) == []


async def test_an_internal_failure_lands_on_the_recovery_page(
    stack: Stack, fake: FakeGitHubOAuthServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)

    async def raiser(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(service, "complete_authorization", raiser)

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code=code)) == "expired"
    assert await _connections(stack) == []


# --- The operator's own stories -----------------------------------------


async def test_a_second_identical_callback_lands_on_the_same_connection(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    """A refresh, a back-button, a duplicated navigation. Not a dead end."""
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)

    first = await stack.get(CALLBACK_PATH, state=state, code=code)
    exchanges = len(fake.recorded_bodies())
    second = await stack.get(CALLBACK_PATH, state=state, code=code)

    assert second.headers["location"] == first.headers["location"]
    assert second.status_code == first.status_code
    assert len(await _connections(stack)) == 1
    assert len(fake.recorded_bodies()) == exchanges, "the code was exchanged twice"


async def test_a_prefetch_that_wins_the_race_does_not_cost_the_connection(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    """The classic cause, proved end to end: the row survives the prefetch."""
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)

    prefetched = await stack.client.get(
        CALLBACK_PATH,
        params={"state": state, "code": code},
        headers={"Sec-Purpose": "prefetch;prerender"},
    )
    COLLECTED.append(prefetched)
    assert prefetched.status_code == 303
    assert prefetched.headers["location"] == f"{APP_URL}/apps"
    assert await _connections(stack) == []

    real = await stack.get(CALLBACK_PATH, state=state, code=code)

    assert _lands_on_apps(real) == ""
    assert len(await _connections(stack)) == 1


async def test_a_dead_session_lands_on_signed_out_and_the_flow_survives_a_retry(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    """The operator's Cloudflare Access story, end to end.

    An SSO login in the middle of the round trip can leave the browser back
    at Jhin without the session it started with. Nothing is claimed without
    one, so the pending row is still there when they sign in and try again.
    """
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)
    stack.sign_out()

    signed_out = await stack.get(CALLBACK_PATH, state=state, code=code)

    assert _lands_on_apps(signed_out) == "signed_out"
    row = await stack.session.scalar(select(OAuthAuthorization))
    assert row is not None
    assert row.consumed_at is None

    stack.sign_in()
    retried = await stack.get(CALLBACK_PATH, state=state, code=code)

    assert _lands_on_apps(retried) == ""
    assert len(await _connections(stack)) == 1


async def test_a_refused_callback_can_be_refreshed_without_changing_its_answer(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    await _register(stack, secret=WRONG_SECRET)
    state, code = await _start(stack)

    first = await stack.get(CALLBACK_PATH, state=state, code=code)
    exchanges = len(fake.recorded_bodies())
    second = await stack.get(CALLBACK_PATH, state=state, code=code)

    assert _lands_on_apps(first) == "client_rejected"
    assert second.headers["location"] == first.headers["location"]
    assert len(fake.recorded_bodies()) == exchanges


async def test_a_thirty_minute_state_outlives_a_twenty_minute_round_trip(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    """An Access login, a GitHub sign-in, and an installation picker.

    Twenty minutes is not an unusual round trip for that sequence, and under
    the old ten-minute state it died at the claim as an indistinguishable
    ``state_expired``.
    """
    assert stack.settings.oauth_state_ttl_seconds == 1800
    await _register(stack, secret=RIGHT_SECRET)
    state, code = await _start(stack)

    row = await stack.session.scalar(select(OAuthAuthorization))
    assert row is not None
    aged = datetime.now(UTC) - timedelta(minutes=20)
    row.created_at = aged
    row.expires_at = aged + timedelta(seconds=stack.settings.oauth_state_ttl_seconds)
    row.retain_until = row.expires_at
    await stack.session.commit()
    still_claimable = row.expires_at
    if still_claimable.tzinfo is None:
        still_claimable = still_claimable.replace(tzinfo=UTC)
    assert still_claimable > datetime.now(UTC)

    assert _lands_on_apps(await stack.get(CALLBACK_PATH, state=state, code=code)) == ""
    assert len(await _connections(stack)) == 1

    # The same row under the old ten-minute budget would already be dead.
    assert aged + timedelta(seconds=600) < datetime.now(UTC)


async def test_a_state_that_names_nothing_never_reaches_a_provider(
    stack: Stack, fake: FakeGitHubOAuthServer
) -> None:
    """A pre-claim refusal costs one UPDATE and two SELECTs, and no network."""
    before = len(fake.recorded_bodies())

    await stack.get(CALLBACK_PATH, state="z" * 43, code="x")

    assert len(fake.recorded_bodies()) == before


async def test_a_pending_row_is_still_reachable_through_the_store(stack: Stack) -> None:
    """Sanity: the harness mints rows the real store would recognise."""
    store = PendingAuthorizationStore(stack.session, stack.crypto)
    row, handle = await store.create(
        workspace_id=stack.workspace_id,
        user_id=stack.admin.id,
        flow="authorization_code",
        connector_type="github",
        ttl_seconds=stack.settings.oauth_state_ttl_seconds,
    )
    await stack.session.commit()
    assert row.retain_until is not None
    assert (
        await store.recall(
            handle=handle, expected_user_id=stack.admin.id, expected_flow="authorization_code"
        )
        is None
    )


def test_no_response_in_the_whole_flow_is_json() -> None:
    """One assertion over everything this module collected.

    It runs last by file order and reads what the cases above left behind, so
    a case that starts answering JSON fails here even if its own assertions
    were relaxed.
    """
    assert COLLECTED, "no responses were collected"
    for response in COLLECTED:
        assert response.status_code == 303, response.request.url
        assert response.content == b""
        assert "application/json" not in response.headers.get("content-type", "")
