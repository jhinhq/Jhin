"""Request/response contracts for avatars, media delivery, and generation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from jhin_domain import AvatarGenerationStatus, AvatarKind
from jhin_media import MAX_PROMPT_HINT_LENGTH

AvatarVariantSize = Literal[64, 128, 256]


class AvatarOut(BaseModel):
    agent_id: UUID
    workspace_id: UUID
    avatar_kind: AvatarKind
    active_avatar_asset_id: UUID | None
    # Relative authenticated path; append ``?size=64|128|256``.
    avatar_url: str | None
    # Accessible fallback when no asset is active (or while it loads).
    initials: str


class AvatarGenerateRequest(BaseModel):
    prompt_hint: str = Field(default="", max_length=MAX_PROMPT_HINT_LENGTH)


class ProviderDisclosure(BaseModel):
    """What the UI shows before/while an external provider renders an avatar."""

    provider_type: str
    provider_display_name: str
    model_profile_id: UUID | None
    model_name: str
    image_size: str
    # Operator-declared per-image price in micro-dollars; null = unknown.
    estimated_cost_micros: int | None
    # The prompt is sent to this provider; it contains public identity only.
    sends_public_identity: bool = True


class AvatarGenerationOut(BaseModel):
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    status: AvatarGenerationStatus
    prompt: str
    prompt_hint: str
    disclosure: ProviderDisclosure
    error: str | None
    error_code: str | None
    result_asset_id: UUID | None
    result_avatar_url: str | None
    temporal_workflow_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime
