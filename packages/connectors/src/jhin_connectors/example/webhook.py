"""Webhook normalization for the example connector (plan 11, 9.2)."""

from __future__ import annotations

from jhin_connectors.base import NormalizedEvent, RawWebhookEvent


def normalize(raw: RawWebhookEvent) -> list[NormalizedEvent]:
    """Map raw provider events to canonical ``connector.example.*`` events.

    Unknown events are dropped (return []) — webhook content is untrusted
    input and must never raise."""
    if raw.event != "ping":
        return []
    return [
        NormalizedEvent(
            event_type="connector.example.ping",
            data={"message": str(raw.payload.get("message", ""))[:200]},
        )
    ]
