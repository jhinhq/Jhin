"""New connections are checked without requiring a separate Test action."""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.connections import service
from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import service as oauth
from jhin_connectors.mcp import DISCOVERY_KEY
from jhin_connectors.testing.fake_github import FakeGitHubServer
from jhin_connectors.testing.fake_mcp import DEFAULT_TOKEN, FakeMcpServer
from jhin_db.models import Connection, OAuthClientRegistration
from jhin_domain import new_uuid7
from jhin_oauth.tokens import TokenResponse
from jhin_secrets import SecretCrypto

REQ = {"request_id": new_uuid7(), "ip_hash": "test"}


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubServer]:
    with FakeGitHubServer() as server:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
        yield server


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMcpServer]:
    with FakeMcpServer() as server:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
        yield server


@pytest.mark.parametrize(
    "token,expected_status", [("fake-github-pat", "active"), ("wrong", "needs_reauth")]
)
async def test_manual_creation_checks_the_actual_credential(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
    token: str,
    expected_status: str,
) -> None:
    connection, _ = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="github",
        name="GitHub",
        auth_type="pat",
        credentials={"token": token},
        config={"base_url": fake_github.base_url},
        **REQ,
    )
    await session.refresh(connection)
    assert connection.last_verified_at is not None
    assert connection.status == expected_status
    assert (connection.last_error is None) == (expected_status == "active")


async def test_manual_mcp_creation_discovers_tools_before_returning(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection, _ = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="mcp",
        name="MCP",
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config={"server_url": fake_mcp.mcp_url, "server_slug": "example"},
        **REQ,
    )
    await session.refresh(connection)
    assert connection.last_verified_at is not None
    assert connection.status == "active"
    assert "echo" in {tool["slug"] for tool in connection.config_json[DISCOVERY_KEY]}


async def test_unreachable_initial_check_keeps_saved_connection_with_a_safe_error(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = service.get_connector("github")

    async def unavailable(_ctx: Any) -> Any:
        raise RuntimeError("provider leaked token-value")

    monkeypatch.setattr(type(connector), "verify_connection", lambda _self, ctx: unavailable(ctx))
    connection, _ = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="github",
        name="GitHub",
        auth_type="pat",
        credentials={"token": "token-value"},
        config={},
        **REQ,
    )
    await session.refresh(connection)
    assert connection.last_verified_at is not None
    assert connection.status == "error"
    assert connection.last_error and "token-value" not in connection.last_error


async def test_initial_mcp_discovery_failure_does_not_report_a_ready_app(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_self: Any, _ctx: Any) -> Any:
        raise RuntimeError("discovery unavailable")

    monkeypatch.setattr(type(service.get_connector("mcp")), "refresh_discovery", unavailable)
    connection, _ = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="mcp",
        name="MCP",
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config={"server_url": fake_mcp.mcp_url, "server_slug": "example"},
        **REQ,
    )
    assert connection.last_verified_at is not None
    assert connection.status == "error"
    assert connection.last_error
    assert DISCOVERY_KEY not in connection.config_json


async def test_initial_check_timeout_saves_a_retryable_error(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_provider(_self: Any, _ctx: Any) -> Any:
        await asyncio.sleep(10)

    timeout = asyncio.timeout
    monkeypatch.setattr(service.asyncio, "timeout", lambda _seconds: timeout(0.001))
    monkeypatch.setattr(type(service.get_connector("github")), "verify_connection", slow_provider)
    connection, _ = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="github",
        name="GitHub",
        auth_type="pat",
        credentials={"token": "token"},
        config={},
        **REQ,
    )
    await session.refresh(connection)
    assert connection.last_verified_at is not None
    assert connection.status == "error"
    assert connection.last_error and "retry" in connection.last_error


@pytest.mark.parametrize(
    "token,expected_status", [(DEFAULT_TOKEN, "active"), ("wrong", "needs_reauth")]
)
@pytest.mark.parametrize("disabled", [False, True])
async def test_mcp_oauth_completion_checks_and_discovers_with_the_new_token(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
    token: str,
    expected_status: str,
    disabled: bool,
) -> None:
    registration = OAuthClientRegistration(
        workspace_id=admin_ctx.workspace_id,
        issuer=fake_mcp.base_url,
        redirect_uri="http://localhost:3000/api/oauth/callback",
        client_id="client",
        source="manual",
    )
    session.add(registration)
    await session.flush()
    row = SimpleNamespace(
        workspace_id=admin_ctx.workspace_id,
        connection_id=None,
        connector_type="mcp",
        draft_json={
            "name": "MCP",
            "config": {"server_url": fake_mcp.mcp_url, "server_slug": "example"},
        },
        issuer=fake_mcp.base_url,
        resource=fake_mcp.mcp_url,
        scope="read",
        token_endpoint=f"{fake_mcp.base_url}/token",
        revocation_endpoint=None,
    )
    if disabled:
        existing = Connection(
            workspace_id=admin_ctx.workspace_id,
            connector_type="mcp",
            auth_type="oauth",
            name="MCP",
            config_json=row.draft_json["config"],
            status="disabled",
        )
        session.add(existing)
        await session.flush()
        row.connection_id = existing.id
    async with httpx.AsyncClient() as client:
        connection = await oauth._persist_connection(
            session,
            crypto,
            client,
            row=row,
            tokens=TokenResponse(access_token=token, token_type="Bearer", scope="read"),
            registration_id=registration.id,
            registration_source="manual",
            user_id=admin_ctx.user.id,
            **REQ,
        )
    await session.commit()
    await session.refresh(connection)
    assert connection.last_verified_at is not None
    assert connection.status == ("disabled" if disabled else expected_status)
    if expected_status == "active":
        assert "echo" in {tool["slug"] for tool in connection.config_json[DISCOVERY_KEY]}
    else:
        assert DISCOVERY_KEY not in connection.config_json


@pytest.mark.parametrize("auth_type", ["management_token", "postgres", "unsupported"])
def test_supabase_tools_match_the_credentials_the_executor_accepts(auth_type: str) -> None:
    from jhin_connectors.supabase.database_tools import SUPABASE_DATABASE_TOOLS
    from jhin_connectors.supabase.management_tools import SUPABASE_MANAGEMENT_TOOLS

    connection = Connection(connector_type="supabase", auth_type=auth_type, config_json={})
    allowed = {
        "management_token": SUPABASE_MANAGEMENT_TOOLS,
        "postgres": SUPABASE_DATABASE_TOOLS,
    }.get(auth_type, ())
    assert {tool.name for tool in service._connection_tools(connection)} == {
        tool.name for tool, _executor in allowed
    }
