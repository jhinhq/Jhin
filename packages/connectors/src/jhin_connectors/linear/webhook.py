"""Linear webhook verification and normalization (plan 11.3, 10.4, 48.5).

Signature scheme (per Linear's developer docs): every delivery carries a
``Linear-Signature`` header holding the bare hex-encoded HMAC-SHA256 of the
raw request body, keyed with the webhook signing secret. Unlike GitHub there
is no ``sha256=`` prefix. The parsed payload additionally carries a
``webhookTimestamp`` field (Unix **milliseconds**); deliveries whose
timestamp is more than :data:`TIMESTAMP_TOLERANCE_SECONDS` away from the
current time are rejected to prevent replays. Verification order is a
security invariant: HMAC first (on the raw bytes), JSON parsing second,
timestamp third — all before any state changes.

Normalization maps Linear ``Issue``/``Comment`` deliveries to canonical
``connector.linear.*`` events. The critical part for the trigger engine
(plan 10.4, 26): Linear updates include an ``updatedFrom`` object holding
the *previous* values of changed fields. That is normalized into a
``changed_from`` mirror of the event data shape, so a trigger filter can
address both the current state (``data.state.name``) and the fact that the
state changed (``data.changed_from.state`` present). See
``jhin_triggers.filters`` for the generic ``transitioned_to`` semantics
built on this convention.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from jhin_connectors.base import NormalizedEvent, RawWebhookEvent

SIGNATURE_HEADER = "Linear-Signature"
EVENT_HEADER = "Linear-Event"
DELIVERY_HEADER = "Linear-Delivery"

# Linear entity types accepted at the webhook endpoint (header values).
WEBHOOK_EVENTS: tuple[str, ...] = ("Issue", "Comment")

# Linear recommends rejecting deliveries more than a minute old.
TIMESTAMP_TOLERANCE_SECONDS = 60.0

_MAX_TEXT = 300
_MAX_DESCRIPTION = 4_000

_ACTION_EVENT = {"create": "created", "update": "updated", "remove": "removed"}


def sign_payload(secret: str, body: bytes) -> str:
    """The exact ``Linear-Signature`` value Linear would send for this body."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time verification of ``Linear-Signature`` (plan 48.5)."""
    if not signature_header or not secret:
        return False
    return hmac.compare_digest(sign_payload(secret, body), signature_header.strip())


def timestamp_is_fresh(payload: dict[str, Any], *, now: float | None = None) -> bool:
    """Replay guard: ``webhookTimestamp`` (Unix ms) within the tolerance."""
    raw = payload.get("webhookTimestamp")
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        return False
    current = time.time() if now is None else now
    return abs(current - float(raw) / 1000.0) <= TIMESTAMP_TOLERANCE_SECONDS


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _team(data: dict[str, Any]) -> dict[str, str]:
    team = _obj(data.get("team"))
    return {
        "id": str(team.get("id", "")),
        "key": str(team.get("key", "")),
        "name": str(team.get("name", "")),
    }


def _state(data: dict[str, Any]) -> dict[str, str]:
    state = _obj(data.get("state"))
    return {
        "id": str(state.get("id", "")),
        "name": str(state.get("name", "")),
        "type": str(state.get("type", "")),
    }


# Linear's ``updatedFrom`` holds flat previous values of changed fields
# (e.g. ``stateId``). Mirror them into the nested normalized shape so
# ``data.changed_from`` parallels ``data`` and the generic trigger DSL can
# resolve the same dotted paths on both (plan 10.4).
_UPDATED_FROM_MIRRORS: dict[str, tuple[str, ...]] = {
    "stateId": ("state", "id"),
    "teamId": ("team", "id"),
    "assigneeId": ("assignee", "id"),
    "title": ("title",),
    "description": ("description",),
    "priority": ("priority",),
}

# Bookkeeping noise that never indicates a meaningful transition.
_UPDATED_FROM_IGNORED = frozenset({"updatedAt", "sortOrder", "boardOrder", "prioritySortOrder"})


def changed_from(updated_from: Any) -> dict[str, Any]:
    """``updatedFrom`` → nested ``changed_from`` mirror of the data shape."""
    result: dict[str, Any] = {}
    for key, previous in _obj(updated_from).items():
        if key in _UPDATED_FROM_IGNORED:
            continue
        path = _UPDATED_FROM_MIRRORS.get(key)
        if path is None:
            result[key] = previous
            continue
        cursor = result
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = previous
    return result


def _normalize_issue(action: str, payload: dict[str, Any]) -> list[NormalizedEvent]:
    data = _obj(payload.get("data"))
    identifier = str(data.get("identifier", ""))
    if not identifier:
        return []
    event: dict[str, Any] = {
        "action": action,
        "external_id": identifier,
        "issue_id": str(data.get("id", "")),
        "title": str(data.get("title", ""))[:_MAX_TEXT],
        "description": str(data.get("description") or "")[:_MAX_DESCRIPTION],
        "url": str(payload.get("url") or data.get("url") or ""),
        "team": _team(data),
        "state": _state(data),
        "priority": int(data.get("priority") or 0),
        "assignee_id": str(_obj(data.get("assignee")).get("id") or data.get("assigneeId") or ""),
        "labels": [
            str(label.get("name", ""))
            for label in data.get("labels", [])
            if isinstance(label, dict)
        ],
    }
    if action == "update":
        event["changed_from"] = changed_from(payload.get("updatedFrom"))
    return [
        NormalizedEvent(event_type=f"connector.linear.issue.{_ACTION_EVENT[action]}", data=event)
    ]


def _normalize_comment(action: str, payload: dict[str, Any]) -> list[NormalizedEvent]:
    if action != "create":
        # Only comment creation is canonical for now (plan 11.3).
        return []
    data = _obj(payload.get("data"))
    comment_id = str(data.get("id", ""))
    if not comment_id:
        return []
    issue = _obj(data.get("issue"))
    return [
        NormalizedEvent(
            event_type="connector.linear.comment.created",
            data={
                "action": action,
                "external_id": comment_id,
                "body": str(data.get("body", ""))[:_MAX_DESCRIPTION],
                "issue_id": str(data.get("issueId") or issue.get("id") or ""),
                "issue_identifier": str(issue.get("identifier", "")),
                "url": str(payload.get("url") or ""),
                "user_id": str(data.get("userId") or _obj(data.get("user")).get("id") or ""),
            },
        )
    ]


def normalize(raw: RawWebhookEvent) -> list[NormalizedEvent]:
    """Map one verified Linear delivery to canonical domain events.

    Webhook payloads are untrusted input: unknown entity types, unknown
    actions, and malformed shapes normalize to [] — never an exception.
    """
    payload = raw.payload
    action = str(payload.get("action", ""))
    if action not in _ACTION_EVENT:
        return []
    if raw.event == "Issue":
        return _normalize_issue(action, payload)
    if raw.event == "Comment":
        return _normalize_comment(action, payload)
    return []


def parse_payload(body: bytes) -> dict[str, Any]:
    """Parse a verified body; raises ValueError when not a JSON object."""
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("payload must be a JSON object")
    return parsed
