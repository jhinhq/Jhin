"""Typed contracts between AvatarGenerationWorkflow and its activities.

Same convention as the other workflow packages: stdlib dataclasses only,
activities referenced by name, implementations on the agent worker (which
holds the database, master key, and provider adapters).
"""

from __future__ import annotations

from dataclasses import dataclass

ACTIVITY_GENERATE_AVATAR = "generate_avatar"
ACTIVITY_FAIL_AVATAR_GENERATION = "fail_avatar_generation"


def avatar_generation_workflow_id(generation_id: str) -> str:
    return f"avatar-generation-{generation_id}"


@dataclass
class AvatarGenerationInput:
    workspace_id: str
    agent_id: str
    generation_id: str


@dataclass
class GenerateAvatarResult:
    asset_id: str
    sha256: str


@dataclass
class FailAvatarGenerationInput:
    workspace_id: str
    agent_id: str
    generation_id: str
    error_code: str
    error: str


@dataclass
class AvatarGenerationResult:
    generation_id: str
    status: str  # "succeeded" | "failed"
    asset_id: str | None = None
    error_code: str | None = None
