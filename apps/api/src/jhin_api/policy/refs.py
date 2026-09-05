"""The workspace's connections as the planner sees them.

One query, one shape (:class:`jhin_policy.ConnectionRef`), shared by grant
validation, grant annotation and the bundle endpoints so they can never
disagree about which connection is active or which sandbox borrows which
GitHub credential.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Connection
from jhin_policy import ConnectionRef


def connection_ref(connection: Connection) -> ConnectionRef:
    config = connection.config_json if isinstance(connection.config_json, dict) else {}
    git_connection_id = ""
    allowed: tuple[str, ...] | None = None
    if connection.connector_type == "cli":
        git_connection_id = str(config.get("git_connection_id") or "")
        raw = config.get("allowed_repositories")
        allowed = (
            tuple(item for item in raw if isinstance(item, str) and item)
            if isinstance(raw, list)
            else ()
        )
    return ConnectionRef(
        id=str(connection.id),
        connector_type=connection.connector_type,
        name=connection.name,
        status=connection.status,
        git_connection_id=git_connection_id,
        allowed_repositories=allowed,
    )


async def connection_refs(db: AsyncSession, workspace_id: UUID) -> list[ConnectionRef]:
    rows = await db.scalars(
        select(Connection)
        .where(Connection.workspace_id == workspace_id)
        .order_by(Connection.created_at)
    )
    return [connection_ref(row) for row in rows]


__all__ = ["connection_ref", "connection_refs"]
