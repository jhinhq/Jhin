"""MCP connections through the API service layer: create, discover tools,
risk overrides, access summary, the workspace tool catalog, and the Apps
catalog (docs/architecture/mcp.md)."""

from collections.abc import Iterator
from typing import Any, TypedDict
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.connections import service
from jhin_api.connections.router import list_catalog
from jhin_api.connections.schemas import CatalogAppOut, ConnectionToolsOut
from jhin_api.deps import WorkspaceContext
from jhin_api.policy.router import list_tools
from jhin_connectors.mcp import DISCOVERY_KEY, OVERRIDES_KEY
from jhin_connectors.testing.fake_mcp import DEFAULT_TOKEN, FakeMcpServer
from jhin_db.models import Agent, AgentCapabilityGrant, AuditEvent, Connection
from jhin_domain import ConnectionStatus, new_uuid7
from jhin_secrets import SecretCrypto


class _RequestAuditArgs(TypedDict):
    request_id: UUID
    ip_hash: str


REQ: _RequestAuditArgs = {"request_id": new_uuid7(), "ip_hash": "test"}


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMcpServer]:
    with FakeMcpServer() as server:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
        yield server


async def _create(
    session: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    server: FakeMcpServer,
    *,
    token: str = DEFAULT_TOKEN,
    slug: str = "fake",
    name: str = "Fake MCP",
) -> Connection:
    connection, webhook = await service.create_connection(
        session,
        crypto,
        ctx,
        connector_type="mcp",
        name=name,
        auth_type="bearer",
        credentials={"token": token},
        config={"server_url": server.mcp_url, "server_slug": slug},
        **REQ,
    )
    assert webhook is None  # MCP connections have no webhooks
    return connection


async def test_create_rejects_unsafe_servers_and_bad_slugs(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", raising=False)
    with pytest.raises(HTTPException) as excinfo:
        await service.create_connection(
            session,
            crypto,
            admin_ctx,
            connector_type="mcp",
            name="Internal",
            auth_type="bearer",
            credentials={"token": "t"},
            config={"server_url": "http://169.254.169.254/mcp", "server_slug": "meta"},
            **REQ,
        )
    assert excinfo.value.status_code == 422
    assert "169.254" not in str(excinfo.value.detail)
    with pytest.raises(HTTPException) as excinfo:
        await service.create_connection(
            session,
            crypto,
            admin_ctx,
            connector_type="mcp",
            name="Bad slug",
            auth_type="none",
            credentials={},
            config={"server_url": "https://mcp.example.com/mcp", "server_slug": "Not Ok"},
            **REQ,
        )
    assert excinfo.value.status_code == 422
    assert "server_slug" in str(excinfo.value.detail)
    assert await session.scalar(select(Connection)) is None


async def test_verify_persists_discovery_and_public_config_hides_it(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _create(session, crypto, admin_ctx, fake_mcp)
    assert DISCOVERY_KEY not in connection.config_json
    updated, health = await service.verify_connection(
        session, crypto, admin_ctx, connection.id, **REQ
    )
    assert health.ok and updated.status == ConnectionStatus.ACTIVE.value
    assert health.details["tool_count"] == "6"
    slugs = {tool["slug"] for tool in updated.config_json[DISCOVERY_KEY]}
    assert {"echo", "create_note", "delete_everything"} <= slugs
    public = service.public_connection_config(updated)
    assert set(public) == {"server_url", "server_slug", "transport"}
    assert DEFAULT_TOKEN not in str(public)


async def test_verify_failure_keeps_connection_usable_without_leaking(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _create(session, crypto, admin_ctx, fake_mcp, token="wrong-token-zz")
    updated, health = await service.verify_connection(
        session, crypto, admin_ctx, connection.id, **REQ
    )
    assert not health.ok and updated.status == ConnectionStatus.ERROR.value
    assert updated.last_error is not None
    assert "wrong-token-zz" not in updated.last_error
    assert fake_mcp.base_url not in updated.last_error


async def test_tools_listing_discovers_once_then_serves_stored_tools(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _create(session, crypto, admin_ctx, fake_mcp)
    listing = ConnectionToolsOut.model_validate(
        await service.list_connection_tools(session, crypto, admin_ctx, connection.id, **REQ)
    )
    assert listing.dynamic and listing.capability_pattern == "mcp.fake.*"
    assert listing.discovered_at is not None
    by_name = {tool.name: tool for tool in listing.tools}
    assert by_name["mcp.fake.echo"].risk == "read"
    assert by_name["mcp.fake.echo"].provider_name == "echo"
    assert by_name["mcp.fake.echo"].annotations["read_only_hint"] is True
    assert by_name["mcp.fake.delete_everything"].risk == "destructive"
    assert by_name["mcp.fake.unannotated"].risk == "write"
    assert by_name["mcp.fake.echo"].input_schema["properties"]["text"]["type"] == "string"
    audit_actions = {row.action for row in await session.scalars(select(AuditEvent))}
    assert "connection.tools_discovered" in audit_actions

    # Served from the stored discovery afterwards: the server can go away.
    fake_mcp.stop()
    again = ConnectionToolsOut.model_validate(
        await service.list_connection_tools(session, crypto, admin_ctx, connection.id, **REQ)
    )
    assert {tool.name for tool in again.tools} == set(by_name)
    with pytest.raises(HTTPException) as excinfo:
        await service.list_connection_tools(
            session, crypto, admin_ctx, connection.id, refresh=True, **REQ
        )
    assert excinfo.value.status_code == 502
    assert fake_mcp.base_url not in str(excinfo.value.detail)


async def test_risk_overrides_are_validated_audited_and_enforced_in_listing(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _create(session, crypto, admin_ctx, fake_mcp)
    await service.list_connection_tools(session, crypto, admin_ctx, connection.id, **REQ)
    listing = ConnectionToolsOut.model_validate(
        await service.update_tool_risk_overrides(
            session,
            admin_ctx,
            connection.id,
            overrides={"echo": "destructive", "delete_everything": "write"},
            **REQ,
        )
    )
    by_name = {tool.name: tool for tool in listing.tools}
    assert by_name["mcp.fake.echo"].risk == "destructive"
    assert by_name["mcp.fake.echo"].derived_risk == "read"
    assert by_name["mcp.fake.echo"].risk_override == "destructive"
    assert by_name["mcp.fake.delete_everything"].risk == "write"
    stored = await session.get(Connection, connection.id)
    assert stored is not None
    assert stored.config_json[OVERRIDES_KEY] == {
        "echo": "destructive",
        "delete_everything": "write",
    }

    cleared = ConnectionToolsOut.model_validate(
        await service.update_tool_risk_overrides(
            session, admin_ctx, connection.id, overrides={"echo": None}, **REQ
        )
    )
    assert {tool.name: tool.risk for tool in cleared.tools}["mcp.fake.echo"] == "read"

    with pytest.raises(HTTPException) as excinfo:
        await service.update_tool_risk_overrides(
            session, admin_ctx, connection.id, overrides={"nope": "read"}, **REQ
        )
    assert excinfo.value.status_code == 422
    with pytest.raises(HTTPException) as excinfo:
        await service.update_tool_risk_overrides(
            session, admin_ctx, connection.id, overrides={"echo": "catastrophic"}, **REQ
        )
    assert excinfo.value.status_code == 422
    actions = [row.action for row in await session.scalars(select(AuditEvent))]
    assert actions.count("connection.tool_risk_overrides_updated") == 2


async def test_static_connectors_list_their_registered_tools(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
) -> None:
    connection, _ = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="github",
        name="GitHub",
        auth_type="pat",
        credentials={"token": "ghp_fake"},
        config={},
        **REQ,
    )
    listing = ConnectionToolsOut.model_validate(
        await service.list_connection_tools(session, crypto, admin_ctx, connection.id, **REQ)
    )
    assert not listing.dynamic and listing.capability_pattern == "github.*"
    assert "github.repository.read" in {tool.name for tool in listing.tools}
    with pytest.raises(HTTPException) as excinfo:
        await service.update_tool_risk_overrides(
            session, admin_ctx, connection.id, overrides={"x": "read"}, **REQ
        )
    assert excinfo.value.status_code == 422


async def test_workspace_tool_catalog_and_access_summary_include_mcp_tools(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _create(session, crypto, admin_ctx, fake_mcp)
    await service.list_connection_tools(session, crypto, admin_ctx, connection.id, **REQ)
    tools = await list_tools(ctx=admin_ctx, db=session)
    names = {tool.name for tool in tools}
    assert "mcp.fake.echo" in names and "system.echo" in names
    echo = next(tool for tool in tools if tool.name == "mcp.fake.echo")
    assert echo.scope_keys == ("connection_id", "tool")
    assert echo.supports_approval

    agent = Agent(workspace_id=admin_ctx.workspace_id, name="Scout", slug="scout")
    session.add(agent)
    await session.flush()
    session.add(
        AgentCapabilityGrant(
            workspace_id=admin_ctx.workspace_id,
            agent_id=agent.id,
            capability="mcp.fake.*",
            scope_json={"connection_id": str(connection.id), "tool": "ec*"},
            effect="allow",
        )
    )
    await session.commit()
    summary: dict[str, Any] = await service.connection_access_summary(
        session, admin_ctx.workspace_id, connection.id
    )
    agents = summary["agents"]
    assert len(agents) == 1 and agents[0]["authorized"]
    assert agents[0]["grants"][0]["eligible_tool_names"] == [
        "mcp.fake.create_note",
        "mcp.fake.delete_everything",
        "mcp.fake.echo",
        "mcp.fake.huge_text",
        "mcp.fake.picture",
        "mcp.fake.unannotated",
    ]

    # Another workspace's catalog does not see these tools.
    other = WorkspaceContext(user=admin_ctx.user, workspace_id=new_uuid7(), role=admin_ctx.role)
    assert "mcp.fake.echo" not in {tool.name for tool in await list_tools(ctx=other, db=session)}


async def test_catalog_endpoint_returns_public_entries() -> None:
    entries = await list_catalog(_auth=None)  # type: ignore[arg-type]
    assert all(isinstance(entry, CatalogAppOut) for entry in entries)
    github = next(entry for entry in entries if entry.slug == "github")
    assert github.connector_type == "github"
    assert github.mcp_url == "https://api.githubcopilot.com/mcp/"
    unverified = [entry for entry in entries if entry.url_unverified]
    assert unverified and all(
        entry.setup_note or entry.auth_note or entry.docs_url for entry in unverified
    )
    assert not any("token" in (entry.mcp_url or "") for entry in entries)


async def test_a_reconnect_draft_survives_both_gates_that_used_to_reject_it(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
) -> None:
    """Reconnect used to be a guaranteed 500 for every OAuth connection.

    A live OAuth connection's ``config_json`` carries server-side bookkeeping
    beside its manifest settings: the recorded token endpoint, the discovered
    tool list, any pending step-up scope. Passing that whole dict into the
    start payload hit two independent refusals — the pending store rejects a
    draft key containing "token", and ``normalize_config`` rejects any key the
    manifest does not declare — and neither is caught, so the amber banner's
    only button answered 500. Sending just the public config clears both.
    """
    connection = await _create(session, crypto, admin_ctx, fake_mcp)
    # Everything a real authorized-and-used connection accumulates.
    connection.auth_type = "oauth"
    connection.config_json = {
        **connection.config_json,
        "oauth_issuer": "https://auth.example.com",
        "oauth_resource": fake_mcp.mcp_url,
        "oauth_scope": "read",
        "oauth_token_endpoint": "https://auth.example.com/token",
        "oauth_revocation_endpoint": "https://auth.example.com/revoke",
        "oauth_pending_scope": "read write",
        "oauth_scope_step_ups": {"echo": "2026-01-01T00:00:00Z"},
        DISCOVERY_KEY: [{"slug": "echo", "name": "echo"}],
        "mcp_discovered_at": "2026-01-01T00:00:00Z",
    }
    await session.commit()

    carried = service.public_connection_config(connection)
    assert "oauth_token_endpoint" not in carried
    assert "oauth_scope_step_ups" not in carried
    assert DISCOVERY_KEY not in carried
    assert carried["server_url"] == fake_mcp.mcp_url

    # Gate one: the pending-authorization store's credential-key screen.
    from jhin_oauth.persistence import _validated_draft

    _validated_draft({"name": connection.name, "config": carried})

    # Gate two: the connector's own config validator, which the callback runs
    # on the way back in.
    from jhin_connectors import normalize_config

    normalize_config(service.get_connector("mcp").manifest, "oauth", dict(carried))


async def test_reconnecting_keeps_the_discovered_tool_list(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_mcp: FakeMcpServer,
) -> None:
    """Re-authorizing replaces the grant, not the row's accumulated knowledge."""
    connection = await _create(session, crypto, admin_ctx, fake_mcp)
    connection.auth_type = "oauth"
    connection.config_json = {
        **connection.config_json,
        DISCOVERY_KEY: [{"slug": "echo", "name": "echo"}],
        OVERRIDES_KEY: {"echo": "low"},
        "oauth_pending_scope": "read write",
    }
    await session.commit()

    reauthorized = await service.create_connection_from_oauth(
        session,
        workspace_id=admin_ctx.workspace_id,
        connection_id=connection.id,
        connector_type="mcp",
        name=connection.name,
        config={
            "server_url": fake_mcp.mcp_url,
            "server_slug": "fake",
            "oauth_issuer": "https://auth.example.com",
            "oauth_resource": fake_mcp.mcp_url,
            "oauth_scope": "read write",
            "oauth_token_endpoint": "https://auth.example.com/token",
        },
        created_by_user_id=admin_ctx.user.id,
    )
    await session.commit()

    assert reauthorized.id == connection.id
    assert reauthorized.config_json[DISCOVERY_KEY] == [{"slug": "echo", "name": "echo"}]
    assert reauthorized.config_json[OVERRIDES_KEY] == {"echo": "low"}
    assert reauthorized.config_json["oauth_scope"] == "read write"
    # The satisfied step-up is not carried forward, or every later reconnect
    # would keep asking for a scope the grant already has.
    assert "oauth_pending_scope" not in reauthorized.config_json
