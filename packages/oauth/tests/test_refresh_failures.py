"""Refresh failures are classified, not retried blindly.

The distinction this file protects is the one that decides whether a Jhin
install stays welcome at a provider. ``invalid_grant`` means a person revoked
us: retrying cannot recover access and does trip abuse detection. A 503 means
the provider is having a bad minute: giving up on the first one would make
every connection fragile.

So a terminal failure stops immediately and says so on the row, a transient
one counts and backs off, and five consecutive transient failures become
terminal — with the row never, at any point, claiming to be active when it is
not.

Every case runs against a real in-process authorization server over a
loopback socket. Nothing here mocks ``httpx``: the classification depends on
status codes, bodies, and connection failures that a mock would simply assert
into existence.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

# Fully qualified: a bare ``tests`` resolves to the repository-root
# integration package, not this one.
from packages.oauth.tests.db_fixtures import (
    Tenant,
    crypto,
    session,
    session_factory,
    tenant,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_connectors.testing.fake_oauth import FakeAsConfig, FakeAuthorizationServer
from jhin_db.models import Connection, OAuthClientRegistration
from jhin_domain import ConnectionStatus, SecretType
from jhin_oauth.lifecycle import (
    CONFIG_REVOCATION_ENDPOINT_KEY,
    CONFIG_TOKEN_ENDPOINT_KEY,
    MAX_CONSECUTIVE_REFRESH_FAILURES,
    ConnectionTokenService,
    refresh_due_connections,
    token_map,
)
from jhin_oauth.persistence import OAuthClientStore
from jhin_oauth.types import ClientCredentials, TokenResponse
from jhin_secrets import SecretCrypto, SecretStore

# Re-exported so pytest sees the fixtures this module's cases ask for.
__all__ = ["Tenant", "crypto", "session", "session_factory", "tenant"]

ACCESS = "fake-access-token-for-tests"
REFRESH = "fake-refresh-token-for-tests"

StartServer = Callable[..., FakeAuthorizationServer]


@pytest.fixture(autouse=True)
def _allow_loopback() -> Iterator[None]:
    """The fake servers are on loopback; the policy needs to be told once."""
    import os

    previous_skip = os.environ.get("JHIN_CONNECTOR_SKIP_DNS_CHECK")
    os.environ["JHIN_CONNECTOR_SKIP_DNS_CHECK"] = "1"
    try:
        yield
    finally:
        if previous_skip is None:
            os.environ.pop("JHIN_CONNECTOR_SKIP_DNS_CHECK", None)
        else:
            os.environ["JHIN_CONNECTOR_SKIP_DNS_CHECK"] = previous_skip


@pytest.fixture
def start_as() -> Iterator[StartServer]:
    import os

    servers: list[FakeAuthorizationServer] = []
    previous = os.environ.get("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS")

    def start(config: FakeAsConfig | None = None) -> FakeAuthorizationServer:
        server = FakeAuthorizationServer(config).start()
        servers.append(server)
        os.environ["JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"] = ",".join(
            started.base_url for started in servers
        )
        return server

    try:
        yield start
    finally:
        for server in servers:
            server.stop()
        if previous is None:
            os.environ.pop("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", None)
        else:
            os.environ["JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"] = previous


async def _connected(
    session: AsyncSession,
    crypto: SecretCrypto,
    tenant: Tenant,
    server: FakeAuthorizationServer,
    *,
    expires_in_seconds: int = 30,
    refresh_expires_at: datetime | None = None,
    name: str = "Example MCP",
) -> Connection:
    """A connection in exactly the state the refresher finds them in."""
    clients = OAuthClientStore(session, crypto)
    registration: OAuthClientRegistration = await clients.save(
        workspace_id=tenant.workspace_id,
        issuer=server.issuer,
        redirect_uri="https://jhin.example.com/api/v1/oauth/callback",
        credentials=ClientCredentials(client_id="test-client", token_endpoint_auth_method="none"),
        scopes="read",
        source="dcr",
        created_by_user_id=tenant.user_id,
    )
    server.register_static_client(client_id="test-client", token_endpoint_auth_method="none")

    connection = Connection(
        workspace_id=tenant.workspace_id,
        connector_type="mcp",
        name=name,
        auth_type="oauth",
        config_json={
            "server_url": f"{server.base_url}/mcp",
            CONFIG_TOKEN_ENDPOINT_KEY: server.token_endpoint,
            CONFIG_REVOCATION_ENDPOINT_KEY: server.revocation_endpoint,
        },
        oauth_client_registration_id=registration.id,
        oauth_issuer=server.issuer,
        oauth_resource=f"{server.base_url}/mcp",
        oauth_scope="read",
        oauth_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        oauth_refresh_expires_at=refresh_expires_at,
        oauth_last_refresh_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(connection)
    await session.flush()

    secret = await SecretStore(session, crypto).create(
        workspace_id=tenant.workspace_id,
        name=f"connection/{connection.public_id}/credentials",
        plaintext=json.dumps(
            token_map(
                TokenResponse(
                    access_token=ACCESS,
                    refresh_token=REFRESH,
                    expires_at=connection.oauth_expires_at,
                    scope="read",
                    issuer=server.issuer,
                )
            )
        ),
        secret_type=SecretType.CONNECTION_CREDENTIALS,
        created_by_user_id=tenant.user_id,
    )
    connection.encrypted_secret_id = secret.id
    await session.commit()
    # Tell the fake about the refresh token this connection holds, the way a
    # real authorization server would remember one it issued.
    with server.state.lock:
        server.state.refresh_tokens[REFRESH] = {
            "client_id": "test-client",
            "scope": "read",
            "resource": f"{server.base_url}/mcp",
        }
    return connection


def _token_requests(server: FakeAuthorizationServer) -> int:
    return len(server.recorded_requests(path_suffix="/token"))


def _utc(value: datetime) -> datetime:
    """SQLite (these tests) hands back naive datetimes; Postgres does not."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --- Terminal failures --------------------------------------------------


async def test_invalid_grant_is_terminal_and_stops_immediately(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    """A revoked grant does not come back, and retrying it gets us throttled."""
    server = start_as(FakeAsConfig(fail_token_with="invalid_grant", token_error_status=400))
    connection = await _connected(session, crypto, tenant, server)

    async with httpx.AsyncClient(follow_redirects=False) as client:
        outcome = await ConnectionTokenService(session, crypto, client).refresh(connection)

    assert outcome.terminal is True
    assert outcome.refreshed is False
    assert connection.status == ConnectionStatus.NEEDS_REAUTH.value
    assert connection.last_error
    assert connection.oauth_refresh_failures == 0
    assert _token_requests(server) == 1


async def test_invalid_client_is_terminal_and_forgets_the_dcr_registration(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    """A new client id cannot inherit this user's grant, so somebody must reconnect."""
    server = start_as(FakeAsConfig(fail_token_with="invalid_client", token_error_status=401))
    connection = await _connected(session, crypto, tenant, server)
    registration_id = connection.oauth_client_registration_id

    async with httpx.AsyncClient(follow_redirects=False) as client:
        outcome = await ConnectionTokenService(session, crypto, client).refresh(connection)

    assert outcome.terminal is True
    assert connection.status == ConnectionStatus.NEEDS_REAUTH.value
    assert registration_id is not None
    assert await session.get(OAuthClientRegistration, registration_id) is None


async def test_a_refresh_token_past_its_own_expiry_is_never_even_attempted(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    """The request would fail and count against us for nothing."""
    server = start_as()
    connection = await _connected(
        session,
        crypto,
        tenant,
        server,
        refresh_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    async with httpx.AsyncClient(follow_redirects=False) as client:
        outcome = await ConnectionTokenService(session, crypto, client).refresh(connection)

    assert outcome.terminal is True
    assert connection.status == ConnectionStatus.NEEDS_REAUTH.value
    assert _token_requests(server) == 0


async def test_a_connection_with_no_refresh_token_is_terminal_without_a_request(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    server = start_as()
    connection = await _connected(session, crypto, tenant, server)
    assert connection.encrypted_secret_id is not None
    await SecretStore(session, crypto).rotate(
        tenant.workspace_id,
        connection.encrypted_secret_id,
        json.dumps({"access_token": ACCESS, "token_type": "Bearer"}),
    )
    await session.commit()

    async with httpx.AsyncClient(follow_redirects=False) as client:
        outcome = await ConnectionTokenService(session, crypto, client).refresh(connection)

    assert outcome.terminal is True
    assert connection.status == ConnectionStatus.NEEDS_REAUTH.value
    assert _token_requests(server) == 0


async def test_an_unusable_token_endpoint_is_terminal_rather_than_dialled(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    """Stored endpoints are re-validated at use time, so tightening the
    outbound allow-list takes effect on connections that already exist."""
    server = start_as()
    connection = await _connected(session, crypto, tenant, server)
    connection.config_json = {
        **connection.config_json,
        CONFIG_TOKEN_ENDPOINT_KEY: "http://169.254.169.254/token",
    }
    await session.commit()

    async with httpx.AsyncClient(follow_redirects=False) as client:
        outcome = await ConnectionTokenService(session, crypto, client).refresh(connection)

    assert outcome.terminal is True
    assert connection.status == ConnectionStatus.NEEDS_REAUTH.value


# --- Transient failures -------------------------------------------------


async def test_a_503_increments_the_counter_without_changing_status(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    server = start_as(
        FakeAsConfig(fail_token_with="temporarily_unavailable", token_error_status=503)
    )
    connection = await _connected(session, crypto, tenant, server)

    async with httpx.AsyncClient(follow_redirects=False) as client:
        outcome = await ConnectionTokenService(session, crypto, client).refresh(connection)

    assert outcome.terminal is False
    assert outcome.refreshed is False
    assert connection.status == ConnectionStatus.ACTIVE.value
    assert connection.oauth_refresh_failures == 1


async def test_a_429_is_transient_too(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    server = start_as(FakeAsConfig(fail_token_with="slow_down", token_error_status=429))
    connection = await _connected(session, crypto, tenant, server)

    async with httpx.AsyncClient(follow_redirects=False) as client:
        outcome = await ConnectionTokenService(session, crypto, client).refresh(connection)

    assert outcome.terminal is False
    assert connection.status == ConnectionStatus.ACTIVE.value


async def test_the_fifth_consecutive_transient_failure_becomes_terminal(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    """Half an hour of a provider being unreachable is a real problem to show."""
    server = start_as(FakeAsConfig(fail_token_with="server_error", token_error_status=503))
    connection = await _connected(session, crypto, tenant, server)

    async with httpx.AsyncClient(follow_redirects=False) as client:
        service = ConnectionTokenService(session, crypto, client)
        outcomes = [
            await service.refresh(connection) for _ in range(MAX_CONSECUTIVE_REFRESH_FAILURES)
        ]

    assert [outcome.terminal for outcome in outcomes[:-1]] == [False] * (
        MAX_CONSECUTIVE_REFRESH_FAILURES - 1
    )
    assert outcomes[-1].terminal is True
    assert connection.status == ConnectionStatus.NEEDS_REAUTH.value


# --- Success ------------------------------------------------------------


async def test_a_successful_refresh_rotates_the_stored_tokens_and_clears_failures(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    server = start_as()
    connection = await _connected(session, crypto, tenant, server)
    connection.oauth_refresh_failures = 3
    await session.commit()
    before = connection.oauth_expires_at

    async with httpx.AsyncClient(follow_redirects=False) as client:
        service = ConnectionTokenService(session, crypto, client)
        outcome = await service.refresh(connection)
        renewed = await service.access_token(connection)

    assert outcome.refreshed is True
    assert outcome.terminal is False
    assert connection.status == ConnectionStatus.ACTIVE.value
    assert connection.oauth_refresh_failures == 0
    assert connection.last_error is None
    assert before is not None
    assert connection.oauth_expires_at is not None
    # SQLite hands datetimes back naive; the comparison is about the instant.
    assert _utc(connection.oauth_expires_at) > _utc(before)
    assert renewed != ACCESS


async def test_a_stale_token_is_renewed_on_use_with_exactly_one_token_request(
    session: AsyncSession, crypto: SecretCrypto, tenant: Tenant, start_as: StartServer
) -> None:
    """Refresh-on-use, and the row lock that keeps it from happening twice."""
    server = start_as()
    connection = await _connected(session, crypto, tenant, server, expires_in_seconds=5)

    async with httpx.AsyncClient(follow_redirects=False) as client:
        service = ConnectionTokenService(session, crypto, client)
        first = await service.access_token(connection)
        second = await service.access_token(connection)

    assert first == second
    assert first != ACCESS
    assert _token_requests(server) == 1


# --- The proactive sweep ------------------------------------------------


async def test_the_sweep_renews_only_what_is_due(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    crypto: SecretCrypto,
    tenant: Tenant,
    start_as: StartServer,
) -> None:
    server = start_as()
    due = await _connected(session, crypto, tenant, server, expires_in_seconds=60, name="Due")
    await _connected(
        session, crypto, tenant, server, expires_in_seconds=36_000, name="Nowhere near due"
    )

    async with httpx.AsyncClient(follow_redirects=False) as client:
        result = await refresh_due_connections(
            session_factory, crypto, client, workspace_id=tenant.workspace_id
        )

    assert result.considered == 1
    assert result.refreshed == 1
    assert result.needs_reauth == 0
    assert result.remaining_oauth_connections == 2
    await session.refresh(due)
    assert due.status == ConnectionStatus.ACTIVE.value


async def test_one_dead_connection_does_not_roll_back_anothers_rotation(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    crypto: SecretCrypto,
    tenant: Tenant,
    start_as: StartServer,
) -> None:
    """Per-connection transactions, because a rolled-back rotation is lost access.

    The healthy connection's provider rotated its refresh token before the
    other one failed. Undoing that write would leave a connection nothing was
    wrong with holding a refresh token the provider has already retired.
    """
    healthy_server = start_as()
    dead_server = start_as(FakeAsConfig(fail_token_with="invalid_grant", token_error_status=400))
    healthy = await _connected(
        session, crypto, tenant, healthy_server, expires_in_seconds=30, name="Healthy"
    )
    dead = await _connected(
        session, crypto, tenant, dead_server, expires_in_seconds=30, name="Revoked"
    )

    async with httpx.AsyncClient(follow_redirects=False) as client:
        result = await refresh_due_connections(
            session_factory, crypto, client, workspace_id=tenant.workspace_id
        )

    assert result.refreshed == 1
    assert result.needs_reauth == 1
    await session.refresh(healthy)
    await session.refresh(dead)
    assert healthy.status == ConnectionStatus.ACTIVE.value
    assert dead.status == ConnectionStatus.NEEDS_REAUTH.value
    assert result.remaining_oauth_connections == 1


async def test_the_sweep_reports_no_remaining_connections_when_there_are_none(
    session_factory: async_sessionmaker[AsyncSession],
    crypto: SecretCrypto,
    tenant: Tenant,
) -> None:
    """This is what lets the workflow stop rather than tick forever."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        result = await refresh_due_connections(
            session_factory, crypto, client, workspace_id=tenant.workspace_id
        )

    assert result.considered == 0
    assert result.remaining_oauth_connections == 0
