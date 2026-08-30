"""Typed contracts for OAuthRefreshWorkflow (docs/architecture/oauth.md).

Dependency-light (stdlib dataclasses only) so the API can start and signal
the workflow without pulling in the agent runtime. Activities are referenced
by name; the implementation lives in the agent worker.
"""

from __future__ import annotations

from dataclasses import dataclass

OAUTH_REFRESH_WORKFLOW = "OAuthRefreshWorkflow"
ACTIVITY_REFRESH_DUE_CONNECTIONS = "refresh_due_oauth_connections"

SIGNAL_OAUTH_REFRESH_NOW = "refresh_now"
SIGNAL_OAUTH_REFRESH_STOP = "stop"

#: Sweeps one run performs before continuing as new. At the default
#: five-minute cadence that is a day per run, which keeps each history small
#: without churning through workflow runs.
OAUTH_REFRESH_WINDOWS_PER_RUN = 288

#: Consecutive empty sweeps before the workflow exits. Three, not one: a
#: workspace that momentarily has no OAuth connections (the last one is being
#: re-authorized) should not lose its refresher and have to be restarted.
OAUTH_REFRESH_IDLE_WINDOWS = 3

DEFAULT_OAUTH_REFRESH_INTERVAL_SECONDS = 300
MIN_OAUTH_REFRESH_INTERVAL_SECONDS = 60


def oauth_refresh_workflow_id(workspace_id: str) -> str:
    """One refresher per workspace, not per connection.

    A workflow per connection would mean thousands of durable timers for a
    thing that is one bounded query; per workspace it is one timer and one
    sweep, and the workflow id makes starting it idempotent from anywhere.
    """
    return f"oauth-refresh-{workspace_id}"


@dataclass
class OAuthRefreshInput:
    workspace_id: str
    interval_seconds: int = DEFAULT_OAUTH_REFRESH_INTERVAL_SECONDS
    windows_done: int = 0
    idle_windows: int = 0


@dataclass
class OAuthRefreshSweep:
    """One sweep's tally, as the activity reports it."""

    considered: int = 0
    refreshed: int = 0
    needs_reauth: int = 0
    transient_failures: int = 0
    remaining_oauth_connections: int = 0


@dataclass
class OAuthRefreshResult:
    workspace_id: str
    windows_done: int
    refreshed: int
    reason: str  # "idle" | "stopped"
