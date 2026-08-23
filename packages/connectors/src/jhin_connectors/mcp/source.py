"""Workspace-scoped dynamic tool source for MCP connections.

Registered into the default :class:`ToolCatalog` by
``jhin_connectors.registry.build_default_catalog``; the tool worker calls
``catalog.for_workspace(session, workspace_id)`` before resolving advertised
tools or executing a bound call, so every discovered tool passes through the
ordinary registry guards, grants, scopes, policy, and sanitization.

Only durable state feeds this: the connection rows of the executing
workspace and their persisted discovery. Disabled connections contribute
nothing; when two connections share a ``server_slug`` the older one wins so
a later connection can never hijack an existing tool name.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.mcp.discovery import is_valid_server_slug
from jhin_connectors.mcp.manifest import MCP_CONNECTOR_TYPE
from jhin_connectors.mcp.tools import connection_tools
from jhin_db.models import Connection
from jhin_domain import ConnectionStatus
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


async def workspace_mcp_connections(
    session: AsyncSession, workspace_id: UUID, *, include_disabled: bool = False
) -> list[Connection]:
    """Usable MCP connections of one workspace, oldest first, one per slug."""
    query = (
        select(Connection)
        .where(
            Connection.workspace_id == workspace_id,
            Connection.connector_type == MCP_CONNECTOR_TYPE,
        )
        .order_by(Connection.created_at, Connection.id)
    )
    if not include_disabled:
        query = query.where(Connection.status != ConnectionStatus.DISABLED.value)
    rows = await session.scalars(query)
    chosen: list[Connection] = []
    seen: set[str] = set()
    for row in rows:
        slug = row.config_json.get("server_slug")
        if not isinstance(slug, str) or not is_valid_server_slug(slug) or slug in seen:
            continue
        seen.add(slug)
        chosen.append(row)
    return chosen


class McpToolSource:
    """:class:`jhin_tools.DynamicToolSource` for MCP connections."""

    async def load(
        self, session: AsyncSession, workspace_id: UUID
    ) -> Sequence[tuple[ToolDefinition, ToolExecutor]]:
        tools: list[tuple[ToolDefinition, ToolExecutor]] = []
        for connection in await workspace_mcp_connections(session, workspace_id):
            tools.extend(connection_tools(connection.config_json))
        return tools


async def workspace_mcp_tool_definitions(
    session: AsyncSession, workspace_id: UUID
) -> tuple[ToolDefinition, ...]:
    """Definition-only view for API discovery (no executors)."""
    from jhin_connectors.mcp.tools import connection_tool_definitions

    definitions: list[ToolDefinition] = []
    for connection in await workspace_mcp_connections(session, workspace_id):
        definitions.extend(connection_tool_definitions(connection.config_json))
    return tuple(definitions)


__all__ = ["McpToolSource", "workspace_mcp_connections", "workspace_mcp_tool_definitions"]
