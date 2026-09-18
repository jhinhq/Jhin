"""An inactive managed connection still owns its advertised namespace."""

import pytest

from jhin_connectors.composio import COMPOSIO_ISSUER
from jhin_connectors.composio.source import workspace_composio_connections


@pytest.mark.parametrize("status", ["disabled", "needs_reauth", "error"])
async def test_inactive_oldest_connection_prevents_namespace_takeover(
    session, workspace, make_connection, status
):
    for name, current_status in (("Old owner", status), ("New duplicate", "active")):
        row = await make_connection(
            workspace,
            connector_type="composio",
            name=name,
            auth_type="managed",
            status=current_status,
            config={"server_slug": "work_slack", "toolkit": "slack"},
        )
        row.oauth_issuer = COMPOSIO_ISSUER
    await session.flush()
    assert await workspace_composio_connections(session, workspace.id) == []


async def test_active_oldest_connection_keeps_namespace(session, workspace, make_connection):
    older = await make_connection(
        workspace,
        connector_type="composio",
        name="Owner",
        auth_type="managed",
        config={"server_slug": "work_slack", "toolkit": "slack"},
    )
    older.oauth_issuer = COMPOSIO_ISSUER
    newer = await make_connection(
        workspace,
        connector_type="composio",
        name="Duplicate",
        auth_type="managed",
        config={"server_slug": "work_slack", "toolkit": "slack"},
    )
    newer.oauth_issuer = COMPOSIO_ISSUER
    await session.flush()
    assert await workspace_composio_connections(session, workspace.id) == [older]
