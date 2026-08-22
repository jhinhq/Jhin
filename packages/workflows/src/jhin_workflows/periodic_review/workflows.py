"""PeriodicReviewWorkflow: one durable scheduler per enabled ``periodic``
review policy (``docs/architecture/coordination.md``).

Windows are deterministic UTC intervals of ``period_seconds`` aligned to the
epoch, computed from ``workflow.now()`` so replay is stable. At the end of
each window one activity opens at most one ``work_review`` (idempotent per
``(policy, window_start)`` trigger key) whose evidence is the reviewer's
manager rollup for the period. The policy is reloaded before every window,
so a disabled/deleted policy ends the workflow even if the ``stop`` signal
was lost; ``refresh`` re-reads the cadence immediately. This file must stay
deterministic and free of I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows.periodic_review.shared import (
    ACTIVITY_LOAD_PERIODIC_REVIEW_POLICY,
    ACTIVITY_OPEN_PERIODIC_REVIEW,
    PERIODIC_REVIEW_WINDOWS_PER_RUN,
    SIGNAL_PERIODIC_REVIEW_REFRESH,
    SIGNAL_PERIODIC_REVIEW_STOP,
    OpenPeriodicReviewInput,
    OpenPeriodicReviewResult,
    PeriodicReviewInput,
    PeriodicReviewPolicyState,
    PeriodicReviewResult,
)

_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)
_WINDOW_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_MIN_PERIOD = timedelta(seconds=60)


def window_bounds(now: datetime, period: timedelta) -> tuple[datetime, datetime]:
    """The epoch-aligned window containing ``now``."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = now.astimezone(UTC) - epoch
    index = int(elapsed.total_seconds() // period.total_seconds())
    start = epoch + period * index
    return start, start + period


@workflow.defn(name="PeriodicReviewWorkflow")
class PeriodicReviewWorkflow:
    def __init__(self) -> None:
        self._stop = False
        self._refresh = False

    @workflow.signal(name=SIGNAL_PERIODIC_REVIEW_STOP)
    def stop(self) -> None:
        """The policy was disabled or deleted; exit without opening more."""
        self._stop = True

    @workflow.signal(name=SIGNAL_PERIODIC_REVIEW_REFRESH)
    def refresh(self) -> None:
        """The cadence changed; reload the policy before the next window."""
        self._refresh = True

    def _stop_requested(self) -> bool:
        """Re-read signal-owned state across awaits."""
        return self._stop

    @workflow.run
    async def run(self, params: PeriodicReviewInput) -> PeriodicReviewResult:
        windows_done = params.windows_done
        while True:
            if self._stop_requested():
                return PeriodicReviewResult(params.policy_id, windows_done, "stopped")
            state: PeriodicReviewPolicyState = await workflow.execute_activity(
                ACTIVITY_LOAD_PERIODIC_REVIEW_POLICY,
                params,
                result_type=PeriodicReviewPolicyState,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY,
            )
            if not state.exists:
                return PeriodicReviewResult(params.policy_id, windows_done, "deleted")
            if not state.enabled or state.period_seconds <= 0:
                return PeriodicReviewResult(params.policy_id, windows_done, "disabled")
            period = max(timedelta(seconds=state.period_seconds), _MIN_PERIOD)
            window_start, window_end = window_bounds(workflow.now(), period)
            self._refresh = False
            remaining = window_end - workflow.now()
            if remaining > timedelta(0):
                with contextlib.suppress(asyncio.TimeoutError):
                    await workflow.wait_condition(
                        lambda: self._stop or self._refresh, timeout=remaining
                    )
            if self._stop_requested():
                return PeriodicReviewResult(params.policy_id, windows_done, "stopped")
            if self._refresh:
                # Cadence changed mid-window: recompute with the new period
                # rather than opening a review for a window that no longer
                # matches the policy.
                continue
            opened: OpenPeriodicReviewResult = await workflow.execute_activity(
                ACTIVITY_OPEN_PERIODIC_REVIEW,
                OpenPeriodicReviewInput(
                    workspace_id=params.workspace_id,
                    policy_id=params.policy_id,
                    window_start=window_start.strftime(_WINDOW_FORMAT),
                    window_end=window_end.strftime(_WINDOW_FORMAT),
                ),
                result_type=OpenPeriodicReviewResult,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_ACTIVITY_RETRY,
            )
            if opened.status == "policy_missing":
                return PeriodicReviewResult(params.policy_id, windows_done, "deleted")
            windows_done += 1
            if windows_done % PERIODIC_REVIEW_WINDOWS_PER_RUN == 0:
                workflow.continue_as_new(
                    PeriodicReviewInput(
                        workspace_id=params.workspace_id,
                        policy_id=params.policy_id,
                        windows_done=windows_done,
                    )
                )
