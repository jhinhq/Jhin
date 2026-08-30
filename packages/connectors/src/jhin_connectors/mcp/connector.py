"""The generic MCP :class:`Connector` (docs/architecture/mcp.md)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp import ClientSession
from mcp import types as mcp_types

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.mcp.client import (
    McpAuthChallengeError,
    McpClient,
    McpConnectionError,
    validate_header_name,
    validate_mcp_server_url,
    validate_transport,
)
from jhin_connectors.mcp.discovery import (
    MAX_TOOLS,
    DiscoveredTool,
    discovered_from_mcp,
    discovery_payload,
    is_valid_server_slug,
)
from jhin_connectors.mcp.manifest import AUTH_HEADER, MCP_MANIFEST, TRANSPORT_AUTO
from jhin_connectors.mcp.oauth import (
    AUTH_OAUTH,
    OAUTH_RESOURCE_KEY,
    challenge_from_response,
    resource_binding,
    validate_oauth_config,
)
from jhin_connectors.mcp.tools import client_for
from jhin_connectors.mcp.tools import connection_tool_definitions as _definitions_for
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


async def discover(ctx: VerifyContext) -> tuple[McpClient, list[DiscoveredTool]]:
    """Initialize a session and list tools. Raises :class:`McpConnectionError`
    (display-safe) when the server cannot be reached or refuses the handshake."""
    client = client_for(ctx.config, ctx.credentials, auth_type=ctx.auth_type)

    async def body(session: ClientSession) -> list[mcp_types.Tool]:
        tools: list[mcp_types.Tool] = []
        cursor: str | None = None
        while len(tools) < MAX_TOOLS:
            page = await session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
        return tools

    listed = await client.run(body)
    return client, discovered_from_mcp(listed)


def _refused_credential(ctx: VerifyContext, error: McpAuthChallengeError) -> ConnectionHealth:
    """Report a ``401``/``403`` as the specific thing it is.

    A static token that stopped working needs a new token; an OAuth
    connection needs somebody to sign in again, or — when the server says the
    grant is too narrow — a reconnect that asks for more. The health record
    carries a ``needs_reauth`` flag so the API can move the row to
    ``needs_reauth`` rather than the generic ``error``, which is the
    difference between "something is broken" and "here is the button".

    The status code is the only part of the server's answer that is repeated.
    """
    if ctx.auth_type != AUTH_OAUTH:
        return ConnectionHealth(ok=False, message=str(error))
    challenge = challenge_from_response(
        error.status_code, {"www-authenticate": error.www_authenticate}
    )
    if challenge is not None and challenge.needs_more_scope:
        message = (
            "this app's sign-in does not cover everything Jhin needs; "
            "reconnect it to grant the extra permission"
        )
    else:
        message = "this app's sign-in is no longer accepted; reconnect it"
    return ConnectionHealth(
        ok=False,
        message=f"{message} (HTTP {error.status_code})",
        details={"needs_reauth": "true"},
    )


class McpConnector(Connector):
    manifest = MCP_MANIFEST

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(config)
        if not is_valid_server_slug(normalized.get("server_slug")):
            raise ValueError(
                "config field 'server_slug' must be 1-32 lowercase letters, digits, or underscores"
            )
        server_url = normalized.get("server_url")
        if not isinstance(server_url, str):
            raise ValueError("config field 'server_url' must be text")
        try:
            normalized["server_url"] = validate_mcp_server_url(server_url)
        except EndpointPolicyError:
            raise ValueError(
                "config field 'server_url' is not allowed: use a public https MCP endpoint "
                "or an origin the operator has allow-listed"
            ) from None
        normalized["transport"] = validate_transport(normalized.get("transport", TRANSPORT_AUTO))
        if auth_type == AUTH_HEADER:
            normalized["header_name"] = validate_header_name(normalized.get("header_name"))
        # The OAuth settings are written by the authorization flow rather than
        # typed into a form, so they are checked for shape and kept. An OAuth
        # connection created without a recorded audience gets the one its own
        # endpoint implies, which is the only value that can ever match at
        # call time anyway.
        normalized.update(validate_oauth_config(normalized))
        if auth_type == AUTH_OAUTH and not normalized.get(OAUTH_RESOURCE_KEY):
            normalized[OAUTH_RESOURCE_KEY] = resource_binding(normalized["server_url"])
        return normalized

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        try:
            client, tools = await discover(ctx)
        except McpAuthChallengeError as error:
            return _refused_credential(ctx, error)
        except McpConnectionError as error:
            return ConnectionHealth(ok=False, message=str(error))
        except (EndpointPolicyError, ValueError) as error:
            return ConnectionHealth(ok=False, message=str(error))
        init = client.initialize_result
        server_name = init.serverInfo.name if init is not None else "MCP server"
        protocol = str(init.protocolVersion) if init is not None else ""
        return ConnectionHealth(
            ok=True,
            message=f"ok: {len(tools)} tools discovered from {server_name}",
            details={
                "server_name": server_name[:200],
                "protocol_version": protocol[:50],
                "tool_count": str(len(tools)),
            },
        )

    async def refresh_discovery(self, ctx: VerifyContext) -> dict[str, Any] | None:
        _client, tools = await discover(ctx)
        return discovery_payload(tools)

    async def fetch_metadata(self, ctx: VerifyContext) -> dict[str, Any]:
        """Compact picker data: tool slugs and their derived risk."""
        _client, tools = await discover(ctx)
        return {
            "server_slug": str(ctx.config.get("server_slug", "")),
            "tool_names": [tool.slug for tool in tools],
        }

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        # Tools are discovered per connection (see McpToolSource).
        return ()

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return ()

    def connection_tool_definitions(self, config: Mapping[str, Any]) -> tuple[ToolDefinition, ...]:
        return _definitions_for(config)


__all__ = ["McpConnector", "discover"]
