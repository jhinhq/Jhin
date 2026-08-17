"""Deterministic trigger idempotency keys (plan 9.4, 48.6).

Two semantically identical events — same trigger, same external entity,
same resolved condition evidence, inside the same dedupe window — must
yield the *same* key, no matter their delivery ids or event ids. The key
is checked against the ``trigger_invocation`` table (partial unique index
on started rows) and also seeds the Temporal workflow id, so duplicate
suppression holds even across racing event-worker replicas.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from jhin_triggers.filters import EvaluationResult

WORKFLOW_ID_PREFIX = "triggered-task"


def _jsonable(value: Any) -> Any:
    """Best-effort canonical form for hashing resolved condition values."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


def transition_fingerprint(result: EvaluationResult) -> str:
    """Hash of the evidence the filter matched on.

    Uses each condition's (path, op, resolved current, resolved previous):
    a re-delivered or re-fired event for the same transition resolves the
    same values and collapses; a *different* transition (e.g. Todo →
    Backlog → Todo again outside the window) produces a fresh fingerprint.
    """
    canonical = json.dumps(
        [
            [c.path, c.op, _jsonable(c.value), _jsonable(c.actual), _jsonable(c.previous)]
            for c in result.conditions
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_idempotency_key(
    *,
    trigger_id: UUID,
    connection_id: UUID | None,
    external_id: str,
    fingerprint: str,
    dedupe_window_seconds: int,
    occurred_at: datetime,
) -> str:
    """The full deterministic key (plan 9.4).

    The time bucket floors ``occurred_at`` to the dedupe window, so repeats
    within one window share a key. Window 0 disables time bucketing (every
    identical transition dedupes forever).
    """
    if dedupe_window_seconds > 0:
        bucket = int(occurred_at.timestamp()) // dedupe_window_seconds
    else:
        bucket = 0
    material = "|".join(
        [
            str(trigger_id),
            str(connection_id or ""),
            external_id,
            fingerprint,
            str(dedupe_window_seconds),
            str(bucket),
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def workflow_id_for_key(idempotency_key: str) -> str:
    """Deterministic Temporal workflow id — the second dedupe defense.

    Temporal's default duplicate-start policy rejects a second start with
    the same workflow id while the first is open, closing the race window
    between check and insert on the invocation table.
    """
    return f"{WORKFLOW_ID_PREFIX}-{idempotency_key[:32]}"
