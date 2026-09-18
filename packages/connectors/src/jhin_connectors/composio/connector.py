"""One managed connector for app toolkits without a native Jhin adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.composio import ComposioError, validate_account
from jhin_connectors.composio.tools import connection_tool_definitions, discovery_payload
from jhin_connectors.composio.tools_client import ComposioToolsClient, pinned_version, valid_toolkit
from jhin_connectors.manifest import AuthSchemeSpec, ConfigFieldSpec, ConnectorManifest
from jhin_connectors.mcp.discovery import is_valid_server_slug
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor

COMPOSIO_CONNECTOR_TYPE = "composio"
COMPOSIO_MANIFEST = ConnectorManifest(
    connector_type=COMPOSIO_CONNECTOR_TYPE,
    display_name="Managed app",
    icon="plug",
    description="Sign in to your app and use its tools through Jhin's approvals and permissions.",
    auth_schemes=(AuthSchemeSpec(type="managed", label="Sign in", secret_fields=()),),
    config_fields=(
        ConfigFieldSpec(name="toolkit", label="App", required=True),
        ConfigFieldSpec(name="server_slug", label="Short name", required=True),
        ConfigFieldSpec(name="toolkit_version", label="Toolkit version"),
    ),
    docs_url="https://docs.composio.dev/docs/tools-direct/toolkit-versioning",
)


async def discover(ctx: VerifyContext) -> dict[str, Any]:
    config = ComposioConnector().validate_settings(ctx.auth_type, ctx.config)
    toolkit = config["toolkit"]
    if ctx.credentials.get("composio_toolkit") != toolkit:
        raise ComposioError("Managed app binding does not match this connection")
    account_id = ctx.credentials.get("composio_account_id", "")
    user_id = ctx.credentials.get("composio_user_id", "")
    auth_config_id = ctx.credentials.get("composio_auth_config_id", "")
    if not user_id or not auth_config_id:
        raise ComposioError("Managed app account binding is incomplete")
    client = ComposioToolsClient()
    account = await client.get_account(account_id)
    validate_account(
        account,
        account_id=account_id,
        user_id=user_id,
        toolkit=toolkit,
        auth_config_id=auth_config_id,
    )
    items = await client.list_tools(toolkit, version=config.get("toolkit_version", "latest"))
    return discovery_payload(items)


class ComposioConnector(Connector):
    manifest = COMPOSIO_MANIFEST

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        if auth_type != "managed":
            raise ValueError("Managed apps require managed authentication")
        if not valid_toolkit(config.get("toolkit")):
            raise ValueError("Managed app toolkit must be a lowercase app slug")
        if not is_valid_server_slug(config.get("server_slug")):
            raise ValueError(
                "Managed app short name must be 1-32 lowercase letters, digits, or underscores"
            )
        if config.get("toolkit_version"):
            try:
                pinned_version(config["toolkit_version"])
            except ComposioError as error:
                raise ValueError(str(error)) from None
        return dict(config)

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        try:
            payload = await discover(ctx)
        except (ComposioError, ValueError) as error:
            return ConnectionHealth(
                ok=False,
                message=str(error),
                details={"needs_reauth": "true"}
                if isinstance(error, ComposioError) and error.needs_reauth
                else {},
            )
        count = len(payload["composio_tools"])
        return ConnectionHealth(
            ok=True,
            message=f"Connected: {count} app tools discovered",
            details={"tool_count": str(count)},
        )

    async def refresh_discovery(self, ctx: VerifyContext) -> dict[str, Any]:
        return await discover(ctx)

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return ()

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return ()

    def connection_tool_definitions(self, config: Mapping[str, Any]) -> tuple[ToolDefinition, ...]:
        return connection_tool_definitions(config)
