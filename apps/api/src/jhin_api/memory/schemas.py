"""Schemas for the memory management API."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jhin_domain import MemoryKind, MemoryScope, MemoryStatus
from jhin_memory import DEFAULT_BACKFILL_LIMIT, MAX_BACKFILL_LIMIT
from jhin_memory.types import MAX_CANDIDATE_CHARS, MAX_TAGS


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    scope: str
    scope_id: UUID
    kind: str
    subject: str | None
    content: str
    source_conversation_id: UUID | None
    source_message_id: UUID | None
    source_task_id: UUID | None
    source_event_id: UUID | None
    visibility: str
    sensitivity: str
    confidence: float
    importance: float
    tags_json: list[str]
    status: str
    valid_from: datetime | None
    expires_at: datetime | None
    pinned_at: datetime | None
    forgotten_at: datetime | None
    version: int
    supersedes_id: UUID | None
    has_embedding: bool = False
    embedding_model: str | None
    created_by_type: str
    created_by_id: UUID | None
    policy_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MemoryListOut(BaseModel):
    items: list[MemoryOut]
    total: int


def _clean_tags(value: list[str]) -> list[str]:
    if len(value) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags")
    cleaned = [tag.strip().lower()[:50] for tag in value if tag.strip()]
    return list(dict.fromkeys(cleaned))


class MemoryCreate(BaseModel):
    """Explicit "remember this" by a person. Activates at the requested scope
    when the caller's role covers it (member: agent scope; admin: any)."""

    content: str = Field(min_length=1, max_length=MAX_CANDIDATE_CHARS)
    scope: MemoryScope = MemoryScope.AGENT
    # Required for scope=agent / scope=team respectively.
    agent_id: UUID | None = None
    team_id: UUID | None = None
    kind: MemoryKind = MemoryKind.FACT
    subject: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    importance: float = Field(default=0.6, ge=0.0, le=1.0)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    source_conversation_id: UUID | None = None
    source_message_id: UUID | None = None
    source_task_id: UUID | None = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        return _clean_tags(value)


class MemoryUpdate(BaseModel):
    """Edits create a new version; the previous version becomes superseded."""

    content: str | None = Field(default=None, min_length=1, max_length=MAX_CANDIDATE_CHARS)
    kind: MemoryKind | None = None
    subject: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    expires_at: datetime | None = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_tags(value)


class PinIn(BaseModel):
    pinned: bool = True


class ContestIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class MemoryStatusFilter(BaseModel):
    status: MemoryStatus


class EmbedMissingIn(BaseModel):
    """Bounded backfill batch for ``POST /memories/embed-missing``."""

    limit: int = Field(default=DEFAULT_BACKFILL_LIMIT, ge=1, le=MAX_BACKFILL_LIMIT)


class EmbedMissingOut(BaseModel):
    embedded: int
    remaining: int
    model: str
    dimensions: int | None


class DeduplicateOut(BaseModel):
    """Result of the admin retroactive dedup pass."""

    clusters: int
    superseded: int
    remaining_active: int
