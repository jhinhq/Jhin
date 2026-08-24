"""Schemas for the skills library and per-agent enablement API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jhin_skills import (
    MAX_DESCRIPTION_CHARS,
    MAX_FILES,
    MAX_NAME_CHARS,
    is_valid_skill_name,
)


class SkillFile(BaseModel):
    """One reference file of a skill: a relative path plus UTF-8 text."""

    path: str = Field(min_length=1, max_length=255)
    content: str


class SkillOut(BaseModel):
    """List-view summary: identity and state, without the (large) body."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    description: str
    source: str
    source_url: str
    enabled: bool
    version: int
    file_count: int = 0
    created_at: datetime
    updated_at: datetime


class SkillDetailOut(SkillOut):
    content: str
    files: list[SkillFile] = Field(default_factory=list)


class SkillListOut(BaseModel):
    items: list[SkillOut]
    total: int


def _validate_name(value: str) -> str:
    if not is_valid_skill_name(value):
        raise ValueError(
            "skill names use lowercase letters, digits, and hyphens "
            f"(at most {MAX_NAME_CHARS} characters)"
        )
    return value


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    content: str = ""
    files: list[SkillFile] = Field(default_factory=list, max_length=MAX_FILES)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _validate_name(value)


class SkillUpdate(BaseModel):
    """Partial update; the name is immutable (it is the agent-facing id)."""

    description: str | None = Field(default=None, min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    content: str | None = None
    files: list[SkillFile] | None = Field(default=None, max_length=MAX_FILES)
    enabled: bool | None = None


class SkillImportIn(BaseModel):
    """Import from a public GitHub repository via the codeload zip archive."""

    github: str = Field(
        min_length=3,
        max_length=300,
        description="owner/repo or owner/repo/path, e.g. anthropics/skills",
    )


class ImportedSkillOut(BaseModel):
    name: str
    description: str = ""
    # "proposed": created disabled, awaiting review; "skipped": not created.
    status: str
    reason: str = ""


class SkillImportOut(BaseModel):
    created: int
    skipped: int
    skills: list[ImportedSkillOut]
    warnings: list[str]


class InstallBuiltinsOut(BaseModel):
    installed: int
    skipped: int
    names: list[str]


class AgentSkillOut(BaseModel):
    """One workspace skill as seen from an agent's profile."""

    skill_id: UUID
    name: str
    description: str
    source: str
    enabled: bool
    enabled_for_agent: bool


class AgentSkillsUpdate(BaseModel):
    """Replace the agent's enabled-skill set with exactly these skills."""

    skill_ids: list[UUID] = Field(max_length=200)
