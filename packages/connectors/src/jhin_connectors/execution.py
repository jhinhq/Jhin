"""Connection resolution inside tool executors (plan 13.5).

The gateway has already authorized the call (capability + scope, which
includes the connection id). This helper does the execution-time part:
load the connection row workspace-scoped, decrypt its credential secret,
and hand both to the connector executor. Plaintext lives only for the
duration of the call and is registered with the process redactor so it can
never reach persisted output.

OAuth adds one thing to that: a connection whose grant has died must fail
here, loudly and by name, rather than half-way through a provider call with
a 401 nobody can interpret. It also adds a *hook* rather than a call —
renewing an OAuth token lives in ``jhin_oauth``, which already depends on
this package's outbound policy, so this module must not import it back. A
worker that can renew tokens installs its renewer at startup; a worker that
cannot still refuses a dead connection correctly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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


class ConnectionNeedsReauthError(ConnectionResolutionError):
    """An OAuth connection whose grant a person has to renew.

    Deliberately its own class on the existing refusal path: an agent can say
    this out loud and it names the one action that fixes it. The message
    carries no token, no URL, and no provider error text.
    """


NEEDS_REAUTH_MESSAGE = "This app needs to be reconnected. Ask an admin to reconnect {name} in Apps."

#: A renewer takes one connection and refreshes its access token if it is due,
#: raising :class:`ConnectionNeedsReauthError` when the grant is gone.
ConnectionTokenRenewer = Callable[[ToolExecutionContext, Connection], Awaitable[None]]

_renewer: ConnectionTokenRenewer | None = None


def set_connection_token_renewer(renewer: ConnectionTokenRenewer | None) -> None:
    """Install (or clear) the process-wide OAuth token renewer.

    A registration hook rather than an import because the dependency only
    goes one way: ``jhin_oauth`` builds on this package's outbound policy, so
    this package cannot import ``jhin_oauth`` without closing the loop. A
    worker that holds a master key and an HTTP client installs the real
    renewer at startup; anywhere else, connections are still resolved and a
    dead one is still refused — only the on-use renewal is absent, and the
    proactive sweep covers that.
    """
    global _renewer
    _renewer = renewer


def connection_token_renewer() -> ConnectionTokenRenewer | None:
    """The installed renewer, if this process has one."""
    return _renewer


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
    if connection.status == ConnectionStatus.NEEDS_REAUTH.value:
        # Fail here, by name, rather than in the middle of a provider call
        # with a 401 the agent has no way to interpret or act on.
        raise ConnectionNeedsReauthError(NEEDS_REAUTH_MESSAGE.format(name=connection.name))
    if connection.encrypted_secret_id is None:
        raise ConnectionResolutionError(f"connection '{connection.name}' has no stored credential")
    if ctx.crypto is None:
        raise ConnectionResolutionError(
            "this process holds no master key and cannot use connections"
        )

    renewer = _renewer
    if renewer is not None and connection.oauth_expires_at is not None:
        # Renew before decrypting, so what we read below is the token the
        # renewal just wrote rather than the one it replaced.
        await renewer(ctx, connection)

    store = SecretStore(ctx.session, ctx.crypto)
    plaintext = await store.reveal(ctx.workspace_id, connection.encrypted_secret_id)
    try:
        parsed = decode_string_secret_map(plaintext)
    except SecretMaterialError:
        raise ConnectionResolutionError(
            f"stored credential for '{connection.name}' is malformed"
        ) from None
    return ResolvedConnection(connection=connection, credentials=parsed)
