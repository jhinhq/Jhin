"""JetStream stream definitions and idempotent bootstrap (plan section 9.5).

Retention is configured explicitly — never rely on server defaults.
"""

from __future__ import annotations

from nats.js import JetStreamContext
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError

from jhin_events.subjects import EVENT_DOMAINS, SUBJECT_PREFIX

INGRESS_STREAM = "INGRESS"
EVENTS_STREAM = "EVENTS"
AUDIT_STREAM = "AUDIT"
DLQ_STREAM = "DLQ"

# Window in which JetStream drops publishes that reuse a Nats-Msg-Id.
DUPLICATE_WINDOW_SECONDS = 120.0

_DAY = 24 * 60 * 60


def _stream(name: str, description: str, subjects: list[str], max_age_days: int) -> StreamConfig:
    return StreamConfig(
        name=name,
        description=description,
        subjects=subjects,
        max_age=max_age_days * _DAY,
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        discard=DiscardPolicy.OLD,
        duplicate_window=DUPLICATE_WINDOW_SECONDS,
        num_replicas=1,
    )


def stream_configs() -> list[StreamConfig]:
    """Explicit configuration for every core stream."""
    return [
        _stream(
            INGRESS_STREAM,
            "Raw external events awaiting normalization",
            [f"{SUBJECT_PREFIX}.*.ingress.>"],
            max_age_days=7,
        ),
        _stream(
            EVENTS_STREAM,
            "Canonical normalized domain events",
            [f"{SUBJECT_PREFIX}.*.{domain}.>" for domain in EVENT_DOMAINS],
            max_age_days=14,
        ),
        _stream(
            AUDIT_STREAM,
            "Append-only audit trail events",
            [f"{SUBJECT_PREFIX}.*.audit.>"],
            max_age_days=30,
        ),
        _stream(
            DLQ_STREAM,
            "Sanitized dead-letter records after delivery exhaustion",
            ["jhin.dlq.>"],
            max_age_days=30,
        ),
    ]


async def ensure_streams(js: JetStreamContext) -> None:
    """Create the core streams, or converge their config if they exist."""
    for config in stream_configs():
        assert config.name is not None
        try:
            await js.stream_info(config.name)
        except NotFoundError:
            await js.add_stream(config)
        else:
            await js.update_stream(config)
