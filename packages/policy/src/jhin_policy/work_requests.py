"""The peer/cross-team work-request permission model (coordination release).

Work requests are distinct from delegation: they ask a peer for help and
create a standalone task only when the target accepts. Authorization is
still deny-by-default on capability grants:

- capability: ``organization.work.request`` — without an allow grant an
  agent cannot open requests at all.
- grant scope key ``targets`` — the relationships the requester may reach:
  ``"subordinates"``, ``"team"`` (agents sharing a team), ``"any"`` (any
  active agent in the workspace), or a list. **Missing means ``"team"``**:
  peer help inside the requester's own team; cross-team reach needs ``any``.
- grant scope key ``target_agent_id`` — optional fnmatch/list pin.

Structural guards no grant can waive (loaded live by the caller):

- no self-request; the target must exist, be active, and be available;
- request-chain depth stays within ``coordination.max_request_depth``;
- per-agent caps: open requests per requester, requests per hour per
  requester, and active request-created tasks per target;
- no ping-pong: a target may not open a request back to an agent that
  already has an open/active request to it on the same root task.

This module is pure (no I/O).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jhin_policy.capabilities import capability_matches
from jhin_policy.evaluator import Grant, GrantEffect, _scope_value_matches

WORK_REQUEST_CAPABILITY = "organization.work.request"
WORK_RESPOND_CAPABILITY = "organization.work.respond"
# The directory-read capability lives with the directory tool, but the
# collaboration baseline below needs it too, so it is named here (a plain
# string, no import) to keep this pure policy module dependency-free.
DIRECTORY_READ_CAPABILITY = "organization.directory.read"


def collaboration_grant_specs() -> tuple[tuple[str, dict[str, Any]], ...]:
    """The safe-by-default "can talk to colleagues" grant baseline.

    Every ordinary agent should be able to *ask* a teammate for help without
    a human first hand-granting an obscure capability — the product is a
    company of agents that work together. Three allow grants make that real:

    - ``organization.directory.read`` — find the right colleague by name,
      role, or expertise (public identity only);
    - ``organization.work.request`` scoped ``targets: any`` — open a peer
      help request to any active teammate. This is safe by default: a work
      request cannot make the target do anything the target is not already
      permitted to do, it only *asks* (the target — or a human on its
      behalf — accepts, declines, or asks for clarification), it creates at
      most one task that stays visible and stoppable, and every structural
      guard (no self-request, availability, depth, per-agent caps, and the
      no-ping-pong rule) still runs in :func:`evaluate_work_request`;
    - ``organization.work.respond`` — respond to requests addressed to you,
      so an agent can be asked as well as ask.

    Delegation (``organization.delegate``) is deliberately **not** here: it
    transfers ownership/authority and stays deny-by-default.
    """
    return (
        (DIRECTORY_READ_CAPABILITY, {}),
        (WORK_REQUEST_CAPABILITY, {"targets": "any"}),
        (WORK_RESPOND_CAPABILITY, {}),
    )


DEFAULT_MAX_REQUEST_DEPTH = 4
DEFAULT_MAX_PENDING_REQUESTS_PER_AGENT = 10
DEFAULT_MAX_REQUESTS_PER_AGENT_PER_HOUR = 30
DEFAULT_MAX_ACTIVE_REQUEST_TASKS_PER_AGENT = 3

_VALID_TARGETS = ("subordinates", "team", "any")


class CoordinationSettings(BaseModel):
    """Workspace knobs under ``workspace.settings_json.coordination``."""

    model_config = ConfigDict(frozen=True)

    max_request_depth: int = Field(default=DEFAULT_MAX_REQUEST_DEPTH, ge=1, le=20)
    max_pending_requests_per_agent: int = Field(
        default=DEFAULT_MAX_PENDING_REQUESTS_PER_AGENT, ge=1, le=500
    )
    max_requests_per_agent_per_hour: int = Field(
        default=DEFAULT_MAX_REQUESTS_PER_AGENT_PER_HOUR, ge=1, le=5000
    )
    max_active_request_tasks_per_agent: int = Field(
        default=DEFAULT_MAX_ACTIVE_REQUEST_TASKS_PER_AGENT, ge=1, le=500
    )


def coordination_settings(settings_json: dict[str, Any] | None) -> CoordinationSettings:
    """Parse workspace settings defensively; malformed values fall back."""
    raw = (settings_json or {}).get("coordination")
    if not isinstance(raw, dict):
        return CoordinationSettings()
    try:
        return CoordinationSettings.model_validate(raw)
    except Exception:
        return CoordinationSettings()


class WorkRequestFacts(BaseModel):
    """Everything the decision needs, resolved from Postgres by the caller."""

    model_config = ConfigDict(frozen=True)

    requester_agent_id: str
    target_agent_id: str
    target_exists: bool = False
    target_active: bool = False
    target_available: bool = True
    target_is_subordinate: bool = False
    target_in_same_team: bool = False
    # Depth the *new* request would have (1 for ordinary work).
    request_depth: int = 1
    # Requester-side counters.
    open_requests_by_requester: int = 0
    requests_last_hour_by_requester: int = 0
    # Target-side load: tasks created from accepted requests still active.
    active_request_tasks_for_target: int = 0
    # The target already has an open/active request addressed to the
    # requester on the same root task (ping-pong guard).
    reverse_request_open: bool = False


class WorkRequestDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    code: str
    reason: str


def _deny(code: str, reason: str) -> WorkRequestDecision:
    return WorkRequestDecision(allowed=False, code=code, reason=reason)


def _targets_of(scope: dict[str, Any]) -> tuple[str, ...]:
    raw = scope.get("targets", "team")
    values = raw if isinstance(raw, list) else [raw]
    cleaned = tuple(str(v) for v in values if str(v) in _VALID_TARGETS)
    return cleaned or ("team",)


def _relationship_permits(scope: dict[str, Any], facts: WorkRequestFacts) -> bool:
    for target in _targets_of(scope):
        if target == "any":
            return True
        if target == "subordinates" and facts.target_is_subordinate:
            return True
        if target == "team" and facts.target_in_same_team:
            return True
    return False


def _id_pin_permits(scope: dict[str, Any], facts: WorkRequestFacts) -> bool:
    if "target_agent_id" not in scope:
        return True
    return _scope_value_matches(scope["target_agent_id"], facts.target_agent_id)


def _grant_covers(grant: Grant, facts: WorkRequestFacts) -> bool:
    return _relationship_permits(grant.scope, facts) and _id_pin_permits(grant.scope, facts)


def evaluate_work_request(
    grants: Sequence[Grant],
    facts: WorkRequestFacts,
    settings: CoordinationSettings | None = None,
) -> WorkRequestDecision:
    """Decide one work request. Relationships are routing context only:
    they constrain a grant's reach, never create authority on their own."""
    limits = settings or CoordinationSettings()

    if facts.requester_agent_id == facts.target_agent_id:
        return _deny("self_request", "an agent cannot request work from itself")
    if not facts.target_exists:
        return _deny("target_not_found", "target agent does not exist in this workspace")
    if not facts.target_active:
        return _deny("target_inactive", "target agent is not active")
    if not facts.target_available:
        return _deny("target_unavailable", "target agent is marked unavailable")

    # Structural guards before grants: no grant can waive them.
    if facts.reverse_request_open:
        return _deny(
            "request_ping_pong",
            "the target already has an open request to you on this work; "
            "answer it instead of opening a request back",
        )
    if facts.request_depth > limits.max_request_depth:
        return _deny(
            "request_depth_exceeded",
            f"work-request chain depth limit is {limits.max_request_depth}",
        )
    if facts.open_requests_by_requester >= limits.max_pending_requests_per_agent:
        return _deny(
            "requester_pending_limit",
            f"you already have {facts.open_requests_by_requester} open requests "
            f"(limit {limits.max_pending_requests_per_agent})",
        )
    if facts.requests_last_hour_by_requester >= limits.max_requests_per_agent_per_hour:
        return _deny(
            "requester_rate_limit",
            f"request rate limit is {limits.max_requests_per_agent_per_hour} per hour",
        )
    if facts.active_request_tasks_for_target >= limits.max_active_request_tasks_per_agent:
        return _deny(
            "target_capacity_exceeded",
            "the target agent is already working on "
            f"{facts.active_request_tasks_for_target} requested tasks "
            f"(limit {limits.max_active_request_tasks_per_agent})",
        )

    matching = [g for g in grants if capability_matches(g.capability, WORK_REQUEST_CAPABILITY)]
    for grant in matching:
        if grant.effect is GrantEffect.DENY and _grant_covers(grant, facts):
            return _deny("explicit_deny", "requesting work from this target is explicitly denied")
    allow = [g for g in matching if g.effect is GrantEffect.ALLOW]
    if not allow:
        return _deny(
            "no_grant", f"agent has no capability grant matching '{WORK_REQUEST_CAPABILITY}'"
        )
    if not any(_grant_covers(grant, facts) for grant in allow):
        return _deny(
            "request_target_not_permitted",
            "no work-request grant permits this target (relationship or agent pin mismatch)",
        )
    return WorkRequestDecision(
        allowed=True, code="granted", reason="work request permitted by capability grant"
    )
