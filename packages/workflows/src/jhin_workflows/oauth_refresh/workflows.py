"""OAuthRefreshWorkflow: one durable refresher per workspace
(``docs/architecture/oauth.md``).

An access token that expires while nobody is looking is a connection that
fails the next time an agent needs it — at three in the morning, inside a
scheduled run, with nobody to reconnect it. This workflow is the answer: a
durable timer that wakes every few minutes, asks one bounded question ("which
of this workspace's OAuth connections expire soon?"), and renews them.

One workflow per *workspace*, deliberately. Per connection would be thousands
of durable timers to do what one query does. The workflow id is derived from
the workspace id, so starting it is idempotent from anywhere — the OAuth
callback starts it after every successful authorization and does not care
whether it was already running.

It exits when the workspace has had no OAuth connections for three
consecutive windows, so an install that uses none costs nothing, and the next
authorization starts it again.

This file must stay deterministic and free of I/O: every clock reading is
``workflow.now()``, and all work happens in the activity.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows.oauth_refresh.shared import (
    ACTIVITY_REFRESH_DUE_CONNECTIONS,
    MIN_OAUTH_REFRESH_INTERVAL_SECONDS,
    OAUTH_REFRESH_IDLE_WINDOWS,
    OAUTH_REFRESH_WINDOWS_PER_RUN,
    SIGNAL_OAUTH_REFRESH_NOW,
    SIGNAL_OAUTH_REFRESH_STOP,
    OAuthRefreshInput,
    OAuthRefreshResult,
    OAuthRefreshSweep,
)

# Three attempts with a short ladder: a provider having a bad minute is worth
# retrying inside the window, and a provider having a bad hour is the sweep's
# own transient-failure counter's problem, not the retry policy's.
_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=3,
)


@workflow.defn(name="OAuthRefreshWorkflow")
class OAuthRefreshWorkflow:
    def __init__(self) -> None:
        self._stop = False
        self._sweep_now = False

    @workflow.signal(name=SIGNAL_OAUTH_REFRESH_STOP)
    def stop(self) -> None:
        """The workspace is going away, or an operator turned this off."""
        self._stop = True

    @workflow.signal(name=SIGNAL_OAUTH_REFRESH_NOW)
    def refresh_now(self) -> None:
        """A connection was just authorized; do not wait out the timer."""
        self._sweep_now = True

    def _stop_requested(self) -> bool:
        """Re-read signal-owned state across awaits."""
        return self._stop

    @workflow.run
    async def run(self, params: OAuthRefreshInput) -> OAuthRefreshResult:
        windows_done = params.windows_done
        idle_windows = params.idle_windows
        refreshed_total = 0
        interval = timedelta(
            seconds=max(params.interval_seconds, MIN_OAUTH_REFRESH_INTERVAL_SECONDS)
        )
        while True:
            if self._stop_requested():
                return OAuthRefreshResult(
                    params.workspace_id, windows_done, refreshed_total, "stopped"
                )
            self._sweep_now = False
            with contextlib.suppress(asyncio.TimeoutError):
                await workflow.wait_condition(
                    lambda: self._stop or self._sweep_now, timeout=interval
                )
            if self._stop_requested():
                return OAuthRefreshResult(
                    params.workspace_id, windows_done, refreshed_total, "stopped"
                )

            sweep: OAuthRefreshSweep = await workflow.execute_activity(
                ACTIVITY_REFRESH_DUE_CONNECTIONS,
                OAuthRefreshInput(
                    workspace_id=params.workspace_id,
                    interval_seconds=params.interval_seconds,
                    windows_done=windows_done,
                    idle_windows=idle_windows,
                ),
                result_type=OAuthRefreshSweep,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_ACTIVITY_RETRY,
            )
            windows_done += 1
            refreshed_total += sweep.refreshed
            # Exit only after three consecutive empty windows: a workspace
            # whose single OAuth connection is mid-re-authorization should
            # keep its refresher rather than need one started again.
            idle_windows = 0 if sweep.remaining_oauth_connections else idle_windows + 1
            if idle_windows >= OAUTH_REFRESH_IDLE_WINDOWS:
                return OAuthRefreshResult(
                    params.workspace_id, windows_done, refreshed_total, "idle"
                )
            if windows_done % OAUTH_REFRESH_WINDOWS_PER_RUN == 0:
                workflow.continue_as_new(
                    OAuthRefreshInput(
                        workspace_id=params.workspace_id,
                        interval_seconds=params.interval_seconds,
                        windows_done=windows_done,
                        idle_windows=idle_windows,
                    )
                )
