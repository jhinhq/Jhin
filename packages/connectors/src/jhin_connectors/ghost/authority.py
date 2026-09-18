"""Installation-wide authority shared by setup and every editorial operation."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.ghost.client import GhostApiError
from jhin_db.models import Connection, Workspace
from jhin_db.models.editorial import GhostInstallation
from jhin_secrets.variables import VariableError, canonical_admin_url


async def installation_authority(
    session: AsyncSession,
    workspace_id: UUID,
    admin_url: str,
    publisher_id: UUID,
    *,
    establish: bool = False,
) -> None:
    base = canonical_admin_url(admin_url)
    if establish:
        # Serialize claims across different connections/keys before creating the
        # unique installation record. This is also the variable mutation order.
        await session.scalar(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        )
    installation = await session.scalar(
        select(GhostInstallation)
        .where(GhostInstallation.workspace_id == workspace_id, GhostInstallation.admin_url == base)
        .execution_options(populate_existing=True)
    )
    conflict = installation is not None and installation.publisher_agent_id != publisher_id
    connections = await session.scalars(
        select(Connection).where(
            Connection.workspace_id == workspace_id, Connection.connector_type == "ghost"
        )
    )
    for candidate in connections:
        try:
            target = canonical_admin_url(str(candidate.config_json.get("admin_url", "")))
        except VariableError:
            continue
        configured = candidate.config_json.get("publisher_agent_id")
        if target == base and configured and str(configured) != str(publisher_id):
            conflict = True
    if conflict:
        raise GhostApiError(
            "Conflicting installation publishers; an administrator must resolve the conflict",
            code="ghost_installation_publisher_conflict",
        )
    if establish and installation is None:
        session.add(
            GhostInstallation(
                workspace_id=workspace_id, admin_url=base, publisher_agent_id=publisher_id
            )
        )
        await session.flush()
