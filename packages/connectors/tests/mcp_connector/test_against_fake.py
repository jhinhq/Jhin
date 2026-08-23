"""MCP connector end-to-end against the in-process fake MCP server: real
SDK transport (Streamable HTTP + SSE fallback), real credential decryption,
the dynamic per-workspace catalog, and the gateway's grant/scope/risk
decisions for discovered tools."""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.base import VerifyContext
from jhin_connectors.mcp import (
    DISCOVERY_KEY,
    OVERRIDES_KEY,
    McpConnector,
    McpToolSource,
    stored_tools,
)
from jhin_connectors.registry import build_default_catalog
from jhin_connectors.testing.fake_mcp import DEFAULT_TOKEN, FakeMcpServer
from jhin_db.models import Agent, AgentCapabilityGrant, Connection, ToolCall, Workspace
from jhin_domain import ToolCallStatus
from jhin_policy import Grant, GrantEffect
from jhin_tools import allowed_tool_definitions
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import ToolGateway


@pytest.fixture(scope="module")
def fake_mcp() -> Iterator[FakeMcpServer]:
    previous = os.environ.get("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS")
    with FakeMcpServer() as server:
        os.environ["JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"] = server.base_url
        try:
            yield server
        finally:
            if previous is None:
                os.environ.pop("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", None)
            else:
                os.environ["JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"] = previous


def _ctx(server: FakeMcpServer, **config: Any) -> VerifyContext:
    return VerifyContext(
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config={"server_url": server.mcp_url, "server_slug": "fake", **config},
    )


async def test_verify_discovers_tools_over_streamable_http(fake_mcp: FakeMcpServer) -> None:
    health = await McpConnector().verify_connection(_ctx(fake_mcp))
    assert health.ok
    assert health.message.startswith("ok: 6 tools")
    assert health.details["server_name"] == "Fake MCP"
    discovery = await McpConnector().refresh_discovery(_ctx(fake_mcp))
    assert discovery is not None
    tools = {tool.slug: tool for tool in stored_tools(discovery)}
    assert tools["echo"].derived_risk.value == "read"
    assert tools["create_note"].derived_risk.value == "write"
    assert tools["delete_everything"].derived_risk.value == "destructive"
    assert tools["unannotated"].derived_risk.value == "write"
    assert tools["echo"].annotations.read_only_hint is True
    assert tools["echo"].input_schema["properties"]["text"]["type"] == "string"


async def test_verify_falls_back_to_sse_and_reports_bad_tokens(fake_mcp: FakeMcpServer) -> None:
    sse = VerifyContext(
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config={"server_url": fake_mcp.sse_url, "server_slug": "fake", "transport": "auto"},
    )
    assert (await McpConnector().verify_connection(sse)).ok
    explicit = VerifyContext(
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config={"server_url": fake_mcp.sse_url, "server_slug": "fake", "transport": "sse"},
    )
    assert (await McpConnector().verify_connection(explicit)).ok
    header = VerifyContext(
        auth_type="header",
        credentials={"token": DEFAULT_TOKEN},
        config={
            "server_url": fake_mcp.mcp_url,
            "server_slug": "fake",
            "header_name": "X-Fake-Mcp-Key",
        },
    )
    assert (await McpConnector().verify_connection(header)).ok

    bad = VerifyContext(
        auth_type="bearer",
        credentials={"token": "wrong-token-value"},
        config={"server_url": fake_mcp.mcp_url, "server_slug": "fake"},
    )
    health = await McpConnector().verify_connection(bad)
    assert not health.ok
    assert "401" in health.message
    assert "wrong-token-value" not in health.message
    assert fake_mcp.mcp_url not in health.message


async def test_metadata_lists_tool_slugs(fake_mcp: FakeMcpServer) -> None:
    metadata = await McpConnector().fetch_metadata(_ctx(fake_mcp))
    assert metadata["server_slug"] == "fake"
    assert "echo" in metadata["tool_names"]


# --- gateway integration ----------------------------------------------------


async def _mcp_connection(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    server: FakeMcpServer,
    *,
    overrides: dict[str, str] | None = None,
    status: str = "active",
    discovery_override: list[dict[str, Any]] | None = None,
) -> Connection:
    discovery = await McpConnector().refresh_discovery(_ctx(server))
    assert discovery is not None
    config: dict[str, object] = {
        "server_url": server.mcp_url,
        "server_slug": "fake",
        "transport": "auto",
        **discovery,
    }
    if discovery_override is not None:
        config[DISCOVERY_KEY] = discovery_override
    if overrides:
        config[OVERRIDES_KEY] = overrides
    return await make_connection(
        workspace,
        connector_type="mcp",
        name=f"fake-{status}",
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config=config,
        status=status,
    )


async def _grant(
    session: AsyncSession,
    context: ToolExecutionContext,
    capability: str,
    scope: dict[str, str],
    *,
    effect: str = "allow",
) -> None:
    if await session.scalar(select(Agent).where(Agent.id == context.agent_id)) is None:
        session.add(
            Agent(
                id=context.agent_id,
                workspace_id=context.workspace_id,
                name=context.agent_name,
                slug=f"scout-{context.agent_id.hex[:6]}",
            )
        )
    session.add(
        AgentCapabilityGrant(
            workspace_id=context.workspace_id,
            agent_id=context.agent_id,
            capability=capability,
            scope_json=scope,
            effect=effect,
        )
    )
    await session.flush()


async def _workspace_catalog(context: ToolExecutionContext) -> ToolCatalog:
    catalog = build_default_catalog()
    assert catalog.has_dynamic_sources
    return await catalog.for_workspace(context.session, context.workspace_id)


async def test_dynamic_catalog_registers_discovered_tools_per_workspace(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _mcp_connection(session, workspace, make_connection, fake_mcp)
    catalog = await _workspace_catalog(context)
    names = catalog.registry.names()
    assert "mcp.fake.echo" in names and "mcp.fake.delete_everything" in names
    assert "system.echo" in names
    assert catalog.get("mcp.fake.delete_everything") is not None
    assert catalog.get("mcp.fake.delete_everything")[0].risk.value == "destructive"  # type: ignore[index]

    # Other workspaces see nothing; disabled connections contribute nothing.
    other = Workspace(name="Other", slug=f"other-{workspace.id.hex[:8]}")
    session.add(other)
    await session.flush()
    assert await McpToolSource().load(session, other.id) == []
    connection.status = "disabled"
    await session.flush()
    assert "mcp.fake.echo" not in (await _workspace_catalog(context)).registry.names()

    grants = [Grant(capability="mcp.fake.*", scope={}, effect=GrantEffect.ALLOW)]
    connection.status = "active"
    await session.flush()
    advertised = allowed_tool_definitions(await _workspace_catalog(context), grants)
    assert {tool.name for tool in advertised} == {
        "mcp.fake.echo",
        "mcp.fake.create_note",
        "mcp.fake.delete_everything",
        "mcp.fake.picture",
        "mcp.fake.huge_text",
        "mcp.fake.unannotated",
    }


async def test_granted_read_tool_executes_through_the_gateway(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _mcp_connection(session, workspace, make_connection, fake_mcp)
    await _grant(session, context, "mcp.fake.*", {"connection_id": str(connection.id)})
    gateway = ToolGateway(context, await _workspace_catalog(context))
    outcome = await gateway.request(
        "mcp.fake.echo",
        f'{{"connection_id": "{connection.id}", "arguments": {{"text": "hi"}}}}',
    )
    assert outcome.status == "executed", outcome
    assert outcome.risk == "read"
    assert outcome.sanitized_output["text"] == "hi"
    assert outcome.sanitized_output["structured_content"] == {"result": "hi"}
    assert "Untrusted" in outcome.sanitized_output["notice"]
    row = await session.scalar(select(ToolCall).where(ToolCall.id == outcome.tool_call_id))
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value
    assert row.connection_id == connection.id
    assert DEFAULT_TOKEN not in str(row.sanitized_output_json)


async def test_destructive_tool_requires_approval_and_overrides_apply(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _mcp_connection(
        session, workspace, make_connection, fake_mcp, overrides={"echo": "destructive"}
    )
    await _grant(session, context, "mcp.fake.*", {"connection_id": str(connection.id)})
    gateway = ToolGateway(context, await _workspace_catalog(context))
    parked = await gateway.request(
        "mcp.fake.delete_everything",
        f'{{"connection_id": "{connection.id}", "arguments": {{"confirm": true}}}}',
    )
    assert parked.status == "needs_approval" and parked.risk == "destructive"
    assert fake_mcp.snapshot()["deleted"] == 0
    overridden = await gateway.request(
        "mcp.fake.echo", f'{{"connection_id": "{connection.id}", "arguments": {{"text": "x"}}}}'
    )
    assert overridden.status == "needs_approval" and overridden.risk == "destructive"


async def test_deny_by_default_and_tool_scope_globs(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _mcp_connection(session, workspace, make_connection, fake_mcp)
    gateway = ToolGateway(context, await _workspace_catalog(context))
    arguments = f'{{"connection_id": "{connection.id}", "arguments": {{"text": "x"}}}}'
    denied = await gateway.request("mcp.fake.echo", arguments)
    assert denied.status == "denied" and denied.decision_code == "no_grant"

    await _grant(
        session, context, "mcp.fake.*", {"connection_id": str(connection.id), "tool": "ec*"}
    )
    gateway = ToolGateway(context, await _workspace_catalog(context))
    assert (await gateway.request("mcp.fake.echo", arguments)).status == "executed"
    mismatch = await gateway.request(
        "mcp.fake.create_note",
        f'{{"connection_id": "{connection.id}", "arguments": {{"title": "t"}}}}',
    )
    assert mismatch.status == "denied" and mismatch.decision_code == "scope_mismatch"
    assert fake_mcp.snapshot()["notes"] == []

    # A grant pinned to another connection id never authorizes this one.
    foreign = await gateway.request(
        "mcp.fake.echo",
        '{"connection_id": "00000000-0000-0000-0000-000000000000", "arguments": {}}',
    )
    assert foreign.status == "denied"


async def test_binary_and_huge_outputs_are_bounded(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _mcp_connection(session, workspace, make_connection, fake_mcp)
    await _grant(session, context, "mcp.fake.*", {})
    gateway = ToolGateway(context, await _workspace_catalog(context))
    picture = await gateway.request(
        "mcp.fake.picture", f'{{"connection_id": "{connection.id}", "arguments": {{}}}}'
    )
    assert picture.status == "executed"
    assert picture.sanitized_output["content"] == [
        {"type": "image", "mime_type": "image/png", "omitted": True}
    ]
    huge = await gateway.request(
        "mcp.fake.huge_text",
        f'{{"connection_id": "{connection.id}", "arguments": {{"kilobytes": 300}}}}',
    )
    assert huge.status == "executed"
    assert huge.sanitized_output["truncated"] is True
    assert len(huge.sanitized_output["text"]) <= 8_192  # gateway per-string cap applies too


async def test_risk_drift_on_the_server_fails_closed(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_mcp: FakeMcpServer,
) -> None:
    discovery = await McpConnector().refresh_discovery(_ctx(fake_mcp))
    assert discovery is not None
    tampered = [
        {**tool, "derived_risk": "read"} if tool["slug"] == "delete_everything" else tool
        for tool in discovery[DISCOVERY_KEY]
    ]
    connection = await _mcp_connection(
        session, workspace, make_connection, fake_mcp, discovery_override=tampered
    )
    await _grant(session, context, "mcp.fake.*", {})
    gateway = ToolGateway(context, await _workspace_catalog(context))
    outcome = await gateway.request(
        "mcp.fake.delete_everything",
        f'{{"connection_id": "{connection.id}", "arguments": {{"confirm": true}}}}',
    )
    assert outcome.status == "failed" and outcome.error_code == "mcp_tool_changed"
    assert fake_mcp.snapshot()["deleted"] == 0


async def test_executor_refuses_connections_of_another_server(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_mcp: FakeMcpServer,
) -> None:
    connection = await _mcp_connection(session, workspace, make_connection, fake_mcp)
    other = await make_connection(
        workspace,
        connector_type="mcp",
        name="other-server",
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config={**connection.config_json, "server_slug": "other"},
    )
    await _grant(session, context, "mcp.fake.*", {})
    gateway = ToolGateway(context, await _workspace_catalog(context))
    outcome = await gateway.request(
        "mcp.fake.echo", f'{{"connection_id": "{other.id}", "arguments": {{"text": "x"}}}}'
    )
    assert outcome.status == "failed" and outcome.error_code == "mcp_connection_mismatch"
