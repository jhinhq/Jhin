"""Typed contracts for PeriodicReviewWorkflow (coordination release).

Dependency-light (stdlib dataclasses only) so the API can start/signal the
workflow without agent-runtime dependencies. Activities are referenced by
name; the implementations live in the agent worker's coordination module.
"""

from __future__ import annotations

from dataclasses import dataclass

ACTIVITY_LOAD_PERIODIC_REVIEW_POLICY = "load_periodic_review_policy"
ACTIVITY_OPEN_PERIODIC_REVIEW = "open_periodic_review"

SIGNAL_PERIODIC_REVIEW_STOP = "stop"
SIGNAL_PERIODIC_REVIEW_REFRESH = "refresh"

# Windows processed by one workflow run before continue-as-new keeps the
# history bounded.
PERIODIC_REVIEW_WINDOWS_PER_RUN = 500


def periodic_review_workflow_id(policy_id: str) -> str:
    return f"review-periodic-{policy_id}"


@dataclass
class PeriodicReviewInput:
    workspace_id: str
    policy_id: str
    windows_done: int = 0


@dataclass
class PeriodicReviewPolicyState:
    """Current policy facts, reloaded before every window."""

    exists: bool
    enabled: bool
    period_seconds: int


@dataclass
class OpenPeriodicReviewInput:
    workspace_id: str
    policy_id: str
    # ISO-8601 UTC instants (``YYYY-MM-DDTHH:MM:SSZ``); the trigger key is
    # derived from ``window_start`` so each window opens at most one review.
    window_start: str
    window_end: str


@dataclass
class OpenPeriodicReviewResult:
    review_id: str | None
    status: str
    created: bool


@dataclass
class PeriodicReviewResult:
    policy_id: str
    windows_done: int
    reason: str  # "disabled" | "deleted" | "stopped"
