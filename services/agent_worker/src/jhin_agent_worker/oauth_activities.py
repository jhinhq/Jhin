"""The proactive OAuth refresh sweep (``docs/architecture/oauth.md``).

``OAuthRefreshWorkflow``'s only activity, and it runs here because the
workflow runs on ``AGENT_TASK_QUEUE``. Every window it asks one bounded
question — which of this workspace's OAuth connections expire within the
horizon — and renews them, each in its own transaction so one provider's bad
minute cannot roll back another connection's rotated refresh token. Without
it, a connection nobody has used all week is a connection that fails the next
time somebody needs it.

The sweep's counterpart, the *on-use* renewer, deliberately does not live
here. It is a hook ``jhin_connectors.execution`` calls while resolving a
connection for a tool call, so it belongs in the only process that runs
connector tools: see :mod:`jhin_tool_worker.oauth_refresh`. This worker does
not depend on ``jhin_connectors`` — the tool worker exists precisely so that
connector code runs isolated from agent reasoning — and installing a hook
into a module this process cannot import would be an import error at
startup, not an optimisation.
"""

from __future__ import annotations

from uuid import UUID

import httpx
from temporalio import activity

from jhin_agent_worker.resources import Resources
from jhin_oauth.lifecycle import refresh_due_connections
from jhin_observability import get_logger
from jhin_workflows.oauth_refresh import (
    ACTIVITY_REFRESH_DUE_CONNECTIONS,
    OAuthRefreshInput,
    OAuthRefreshSweep,
)

logger = get_logger(__name__)

#: Outbound OAuth calls never follow redirects — a 3xx on a token endpoint is
#: a way to move a request to a host the SSRF policy never approved — and time
#: out quickly, because a sweep must not hold a worker slot on one slow
#: provider.
OAUTH_HTTP_TIMEOUT_SECONDS = 15.0


def _oauth_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(OAUTH_HTTP_TIMEOUT_SECONDS),
    )


class OAuthActivities:
    """Temporal activities for the per-workspace OAuth refresher."""

    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    @activity.defn(name=ACTIVITY_REFRESH_DUE_CONNECTIONS)
    async def refresh_due_oauth_connections(self, params: OAuthRefreshInput) -> OAuthRefreshSweep:
        """Renew every OAuth connection in this workspace that expires soon.

        Returns a tally rather than raising on individual failures: a
        connection whose grant was revoked is a fact for the workspace's Apps
        page to show, not a reason to fail the sweep and retry it against
        every other connection again.
        """
        async with _oauth_client() as client:
            result = await refresh_due_connections(
                self._resources.session_factory,
                self._resources.crypto,
                client,
                workspace_id=UUID(params.workspace_id),
            )
        if result.needs_reauth:
            logger.info(
                "oauth.refresh_sweep_needs_reauth",
                needs_reauth=result.needs_reauth,
                refreshed=result.refreshed,
            )
        return OAuthRefreshSweep(
            considered=result.considered,
            refreshed=result.refreshed,
            needs_reauth=result.needs_reauth,
            transient_failures=result.transient_failures,
            remaining_oauth_connections=result.remaining_oauth_connections,
        )


__all__ = ["OAUTH_HTTP_TIMEOUT_SECONDS", "OAuthActivities"]
