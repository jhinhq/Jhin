"""Refresh-on-use for connector tool calls (``docs/architecture/oauth.md``).

The other half of "a connected app stays connected". The proactive sweep
(:mod:`jhin_agent_worker.oauth_activities`) renews tokens on a timer; this
renews the one token a tool call is about to use, in the moment it is needed,
so a connection that went stale between sweeps still works.

It lives in the tool worker for a concrete reason rather than a tidy one.
``jhin_connectors.execution`` invokes the renewer while resolving a
connection for a tool call, and connector tools run *only* in this process —
the tool worker exists so connector code executes isolated from agent
reasoning. This is therefore the only worker that both imports
``jhin_connectors`` and holds a master key, which is exactly the pair the
renewer needs.

It is *installed* rather than imported by the connectors package because the
dependency runs one way: ``jhin_oauth`` builds on that package's outbound URL
policy, so the connectors package must not import ``jhin_oauth`` back. A
process that never installs a renewer still resolves connections and still
refuses a dead one correctly; it simply leaves renewal to the sweep.
"""

from __future__ import annotations

import contextlib

import httpx

from jhin_connectors import execution
from jhin_db.models import Connection
from jhin_oauth.lifecycle import ConnectionNeedsReauthError, ConnectionTokenService
from jhin_observability import get_logger
from jhin_tools.builtin import ToolExecutionContext

logger = get_logger(__name__)

#: Outbound OAuth calls never follow redirects — a 3xx on a token endpoint is
#: a way to move a request to a host the SSRF policy never approved — and time
#: out quickly, because a renewal must not stall the tool call waiting on it.
OAUTH_HTTP_TIMEOUT_SECONDS = 15.0


def _oauth_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(OAUTH_HTTP_TIMEOUT_SECONDS),
    )


async def renew_connection_token(ctx: ToolExecutionContext, connection: Connection) -> None:
    """Refresh-on-use: renew this connection's token if it is nearly stale.

    Runs inside the tool call that needs the token, under the row lock
    ``ConnectionTokenService`` takes, so two workers reaching the same
    connection together produce one token request rather than two — which
    matters because a second request against a rotating provider invalidates
    the first's refresh token.

    A dead grant surfaces as the connectors package's own refusal, so the
    agent gets one sentence naming the app and the fix rather than a provider
    401 it cannot interpret.
    """
    if ctx.crypto is None:
        return
    async with _oauth_client() as client:
        service = ConnectionTokenService(ctx.session, ctx.crypto, client)
        try:
            await service.access_token(connection)
        except ConnectionNeedsReauthError as exc:
            raise execution.ConnectionNeedsReauthError(str(exc)) from None
        except Exception:
            # A provider that could not be reached is not a reason to refuse a
            # call that may still work with the token we already hold: the
            # sweep will keep trying, and an expired token fails honestly at
            # the provider. Never let upkeep turn a usable call into an error.
            logger.warning("oauth.refresh_on_use_failed")


def install_refresh_on_use() -> None:
    """Register the on-use renewer for this process. Idempotent."""
    execution.set_connection_token_renewer(renew_connection_token)


def uninstall_refresh_on_use() -> None:
    """Remove the renewer again (used by tests and orderly shutdown)."""
    with contextlib.suppress(Exception):
        execution.set_connection_token_renewer(None)


__all__ = [
    "OAUTH_HTTP_TIMEOUT_SECONDS",
    "install_refresh_on_use",
    "renew_connection_token",
    "uninstall_refresh_on_use",
]
