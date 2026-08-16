"""Subject conventions for the Jhin event backbone (plan section 9.2).

Canonical form::

    jhin.v1.<workspace_id>.<domain>.<entity>.<event>

Raw external events land under ``jhin.v1.<workspace_id>.ingress.<connector>.<event>``
before normalization; audit events under ``jhin.v1.<workspace_id>.audit.*``;
dead letters under ``jhin.dlq.*``.
"""

from __future__ import annotations

SUBJECT_PREFIX = "jhin.v1"

# Canonical first-level domains captured by the EVENTS stream. Adding a domain
# here (and re-running stream bootstrap) extends the EVENTS stream subjects.
EVENT_DOMAINS: tuple[str, ...] = (
    "task",
    "agent",
    "tool",
    "approval",
    "connector",
    "trigger",
    "workflow",
    "system",
)

_RESERVED_TOKENS = {"ingress", "audit", "dlq"}


def _validate_token(value: str, *, field: str) -> str:
    if not value or any(ch in value for ch in (" ", ".", "*", ">")):
        raise ValueError(f"invalid subject token for {field}: {value!r}")
    return value


def event_subject(workspace_id: str, event_type: str) -> str:
    """Subject for a canonical domain event, e.g. ``task.created``."""
    _validate_token(workspace_id, field="workspace_id")
    parts = event_type.split(".")
    if len(parts) < 2:
        raise ValueError(f"event_type must be '<domain>.<...>.<event>', got {event_type!r}")
    for part in parts:
        _validate_token(part, field="event_type")
    domain = parts[0]
    if domain in _RESERVED_TOKENS:
        raise ValueError(f"domain {domain!r} is reserved; use the dedicated helper")
    if domain not in EVENT_DOMAINS:
        raise ValueError(f"unknown event domain {domain!r}; known: {EVENT_DOMAINS}")
    return f"{SUBJECT_PREFIX}.{workspace_id}.{event_type}"


def ingress_subject(workspace_id: str, connector: str, event: str) -> str:
    """Subject for a raw external event prior to normalization."""
    _validate_token(workspace_id, field="workspace_id")
    _validate_token(connector, field="connector")
    _validate_token(event, field="event")
    return f"{SUBJECT_PREFIX}.{workspace_id}.ingress.{connector}.{event}"


def audit_subject(workspace_id: str, action: str) -> str:
    _validate_token(workspace_id, field="workspace_id")
    _validate_token(action, field="action")
    return f"{SUBJECT_PREFIX}.{workspace_id}.audit.{action}"


def dlq_subject(origin_stream: str) -> str:
    _validate_token(origin_stream, field="origin_stream")
    return f"jhin.dlq.{origin_stream.lower()}"
