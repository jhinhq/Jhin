"""Connection resolution inside tool executors (plan 13.5).

The gateway has already authorized the call (capability + scope, which
includes the connection id). This helper does the execution-time part:
load the connection row workspace-scoped, decrypt its credential secret,
and hand both to the connector executor. Plaintext lives only for the
duration of the call and is registered with the process redactor so it can
never reach persisted output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from jhin_db.models import Connection
from jhin_domain import ConnectionStatus
from jhin_secrets import SecretMaterialError, SecretStore, decode_string_secret_map
from jhin_tools.builtin import ToolExecutionContext


class ConnectionResolutionError(Exception):
    """The tool call references a connection that cannot be used. Messages
    are safe to show to models and users — never credential material."""


@dataclass(frozen=True)
class ResolvedConnection:
    """One usable connection with its decrypted credential fields."""

    connection: Connection
    credentials: dict[str, str]

    @property
    def config(self) -> dict[str, Any]:
        return dict(self.connection.config_json)


async def resolve_connection(
    ctx: ToolExecutionContext, connection_id: str | UUID, *, connector_type: str
) -> ResolvedConnection:
    """Load + decrypt one connection for immediate use inside an executor.

    Workspace isolation (plan 48.4): the lookup is always scoped to the
    calling run's workspace, so a connection id from another workspace
    behaves exactly like a missing connection.
    """
    try:
        target = connection_id if isinstance(connection_id, UUID) else UUID(connection_id)
    except ValueError:
        raise ConnectionResolutionError("connection_id is not a valid UUID") from None

    connection = await ctx.session.scalar(
        select(Connection).where(
            Connection.id == target,
            Connection.workspace_id == ctx.workspace_id,
            Connection.connector_type == connector_type,
        )
    )
    if connection is None:
        raise ConnectionResolutionError(
            f"no {connector_type} connection {target} in this workspace"
        )
    if connection.status == ConnectionStatus.DISABLED.value:
        raise ConnectionResolutionError(f"connection '{connection.name}' is disabled")
    if connection.encrypted_secret_id is None:
        raise ConnectionResolutionError(f"connection '{connection.name}' has no stored credential")
    if ctx.crypto is None:
        raise ConnectionResolutionError(
            "this process holds no master key and cannot use connections"
        )

    store = SecretStore(ctx.session, ctx.crypto)
    plaintext = await store.reveal(ctx.workspace_id, connection.encrypted_secret_id)
    try:
        parsed = decode_string_secret_map(plaintext)
    except SecretMaterialError:
        raise ConnectionResolutionError(
            f"stored credential for '{connection.name}' is malformed"
        ) from None
    return ResolvedConnection(connection=connection, credentials=parsed)
