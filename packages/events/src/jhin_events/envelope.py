"""Canonical event envelope for the Jhin event backbone (plan section 9.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import uuid_utils
from pydantic import BaseModel, ConfigDict, Field


def new_uuid7() -> UUID:
    """Time-ordered UUIDv7 as a stdlib UUID (usable as a NATS dedupe id)."""
    return UUID(str(uuid_utils.uuid7()))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EventSource(BaseModel):
    """Origin of an event: a connector type plus optional connection instance."""

    model_config = ConfigDict(frozen=True)

    type: str
    connection_id: UUID | None = None


class EventEnvelope(BaseModel):
    """Every event published to NATS uses this envelope.

    ``event_id`` doubles as the JetStream deduplication id, so duplicate
    publishes within the stream's duplicate window are dropped server-side.
    """

    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(default_factory=new_uuid7)
    event_type: str
    event_version: int = 1
    workspace_id: str
    occurred_at: datetime = Field(default_factory=_utc_now)
    received_at: datetime = Field(default_factory=_utc_now)
    correlation_id: UUID = Field(default_factory=new_uuid7)
    causation_id: UUID | None = None
    source: EventSource
    data: dict[str, Any] = Field(default_factory=dict)

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> EventEnvelope:
        return cls.model_validate_json(raw)
