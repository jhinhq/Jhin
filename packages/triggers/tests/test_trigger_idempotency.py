"""Idempotency key determinism: identical transitions collapse, distinct don't."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from jhin_triggers import (
    build_idempotency_key,
    evaluate_filter,
    transition_fingerprint,
    workflow_id_for_key,
)

TRIGGER_ID = UUID("01920000-0000-7000-8000-000000000001")
CONNECTION_ID = UUID("01920000-0000-7000-8000-000000000002")

FILTER = {
    "all": [
        {"path": "data.team.key", "op": "eq", "value": "ENG"},
        {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
    ]
}


def event(*, state: str = "Todo", previous_id: str = "state-backlog") -> dict[str, Any]:
    return {
        "event_type": "connector.linear.issue.updated",
        "data": {
            "external_id": "ENG-142",
            "team": {"key": "ENG"},
            "state": {"id": f"state-{state.lower()}", "name": state},
            "changed_from": {"state": {"id": previous_id}},
        },
    }


def key_for(payload: dict[str, Any], *, occurred_at: datetime, window: int = 300) -> str:
    fingerprint = transition_fingerprint(evaluate_filter(FILTER, payload))
    return build_idempotency_key(
        trigger_id=TRIGGER_ID,
        connection_id=CONNECTION_ID,
        external_id="ENG-142",
        fingerprint=fingerprint,
        dedupe_window_seconds=window,
        occurred_at=occurred_at,
    )


def test_semantically_identical_events_share_a_key() -> None:
    t1 = datetime(2026, 8, 17, 12, 0, 10, tzinfo=UTC)
    t2 = datetime(2026, 8, 17, 12, 0, 55, tzinfo=UTC)  # same 300s bucket
    assert key_for(event(), occurred_at=t1) == key_for(event(), occurred_at=t2)


def test_different_transition_evidence_changes_the_key() -> None:
    when = datetime(2026, 8, 17, 12, 0, 10, tzinfo=UTC)
    from_backlog = key_for(event(previous_id="state-backlog"), occurred_at=when)
    from_done = key_for(event(previous_id="state-done"), occurred_at=when)
    assert from_backlog != from_done


def test_window_bucket_separates_repeats() -> None:
    inside = datetime(2026, 8, 17, 12, 0, 10, tzinfo=UTC)
    next_bucket = datetime(2026, 8, 17, 12, 10, 0, tzinfo=UTC)
    assert key_for(event(), occurred_at=inside) != key_for(event(), occurred_at=next_bucket)


def test_window_zero_dedupes_forever() -> None:
    t1 = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 20, 8, 30, 0, tzinfo=UTC)
    assert key_for(event(), occurred_at=t1, window=0) == key_for(event(), occurred_at=t2, window=0)


def test_key_depends_on_trigger_and_entity() -> None:
    when = datetime(2026, 8, 17, 12, 0, 10, tzinfo=UTC)
    fingerprint = transition_fingerprint(evaluate_filter(FILTER, event()))
    base = build_idempotency_key(
        trigger_id=TRIGGER_ID,
        connection_id=CONNECTION_ID,
        external_id="ENG-142",
        fingerprint=fingerprint,
        dedupe_window_seconds=300,
        occurred_at=when,
    )
    other_trigger = build_idempotency_key(
        trigger_id=UUID("01920000-0000-7000-8000-00000000ffff"),
        connection_id=CONNECTION_ID,
        external_id="ENG-142",
        fingerprint=fingerprint,
        dedupe_window_seconds=300,
        occurred_at=when,
    )
    other_issue = build_idempotency_key(
        trigger_id=TRIGGER_ID,
        connection_id=CONNECTION_ID,
        external_id="ENG-999",
        fingerprint=fingerprint,
        dedupe_window_seconds=300,
        occurred_at=when,
    )
    assert len({base, other_trigger, other_issue}) == 3


def test_unserializable_values_do_not_crash_fingerprinting() -> None:
    result = evaluate_filter(
        {"all": [{"path": "data.blob", "op": "exists"}]},
        {"data": {"blob": object()}},
    )
    assert transition_fingerprint(result)  # no exception, stable non-empty hex


def test_workflow_id_is_deterministic_and_bounded() -> None:
    when = datetime(2026, 8, 17, 12, 0, 10, tzinfo=UTC)
    key = key_for(event(), occurred_at=when)
    assert workflow_id_for_key(key) == workflow_id_for_key(key)
    assert workflow_id_for_key(key).startswith("triggered-task-")
    assert len(workflow_id_for_key(key)) <= 64
