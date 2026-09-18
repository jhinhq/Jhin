"""Persisted managed app tools, scoped to the executing workspace."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.composio import COMPOSIO_ISSUER
from jhin_connectors.composio.tools import connection_tool_definitions, connection_tools
from jhin_connectors.composio.tools_client import valid_toolkit
from jhin_connectors.mcp.discovery import is_valid_server_slug
from jhin_db.models import Connection
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


async def workspace_composio_connections(
    session: AsyncSession, workspace_id: UUID
) -> list[Connection]:
    rows = await session.scalars(
        select(Connection)
        .where(
            Connection.workspace_id == workspace_id,
            Connection.connector_type == "composio",
        )
        .order_by(Connection.created_at, Connection.id)
    )
    seen: set[str] = set()
    selected: list[Connection] = []
    for row in rows:
        slug = row.config_json.get("server_slug")
        if not is_valid_server_slug(slug) or slug in seen:
            continue
        seen.add(str(slug))
        # Inactive owners retain their namespace. Otherwise disabling one
        # connection would make a newer duplicate inherit its tool names.
        if (
            row.status != "active"
            or row.auth_type != "managed"
            or row.oauth_issuer != COMPOSIO_ISSUER
            or not valid_toolkit(row.config_json.get("toolkit"))
        ):
            continue
        selected.append(row)
    return selected


class ComposioToolSource:
    async def load(
        self, session: AsyncSession, workspace_id: UUID
    ) -> Sequence[tuple[ToolDefinition, ToolExecutor]]:
        result: list[tuple[ToolDefinition, ToolExecutor]] = []
        for connection in await workspace_composio_connections(session, workspace_id):
            result.extend(connection_tools(connection.config_json))
        return result


async def workspace_composio_tool_definitions(
    session: AsyncSession, workspace_id: UUID
) -> tuple[ToolDefinition, ...]:
    result: list[ToolDefinition] = []
    for connection in await workspace_composio_connections(session, workspace_id):
        result.extend(connection_tool_definitions(connection.config_json))
    return tuple(result)
