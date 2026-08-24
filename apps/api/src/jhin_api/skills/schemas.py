"""Schemas for the skills library and per-agent enablement API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jhin_skills import (
    DEFAULT_CATEGORY,
    MAX_DESCRIPTION_CHARS,
    MAX_FILES,
    MAX_NAME_CHARS,
    is_valid_skill_name,
)

MAX_CATEGORY_CHARS = 64


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
    # The stored value is nullable ("General" when unset); the router
    # coalesces it before returning, the same way it fills in file_count.
    category: str | None = None
    created_by_agent_id: UUID | None = None
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


def _clean_category(value: str | None) -> str | None:
    """Free-text category: trimmed, empty means "unset" (-> General)."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    content: str = ""
    files: list[SkillFile] = Field(default_factory=list, max_length=MAX_FILES)
    # Free text; defaults to "General" (DEFAULT_CATEGORY) when omitted or blank.
    category: str | None = Field(default=None, max_length=MAX_CATEGORY_CHARS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("category")
    @classmethod
    def _category(cls, value: str | None) -> str | None:
        return _clean_category(value)


class SkillUpdate(BaseModel):
    """Partial update; the name is immutable (it is the agent-facing id)."""

    description: str | None = Field(default=None, min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    content: str | None = None
    files: list[SkillFile] | None = Field(default=None, max_length=MAX_FILES)
    enabled: bool | None = None
    category: str | None = Field(default=None, max_length=MAX_CATEGORY_CHARS)

    @field_validator("category")
    @classmethod
    def _category(cls, value: str | None) -> str | None:
        return _clean_category(value)


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


class SkillSourceOut(BaseModel):
    """One browsable skill repository: either one of the maintainer-reviewed
    defaults, or a workspace admin's own custom addition
    (`GET /skill-sources`)."""

    source: str
    label: str
    description: str
    url: str
    # False for the hardcoded defaults; True for a workspace's own addition
    # (only a custom entry can be removed with DELETE).
    custom: bool = False


class SkillSourceCreateIn(BaseModel):
    """Add a workspace-custom browse source (admin). Validated live against
    GitHub before it is persisted — see `service.add_custom_source`."""

    source: str = Field(
        min_length=3,
        max_length=300,
        description=(
            "owner/repo, optionally /path — e.g. anthropics/skills or "
            "anthropics/skills/document-skills"
        ),
    )
    label: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=500)


class BrowseSkillOut(BaseModel):
    """One skill found while browsing a source, parsed but not imported."""

    source: str
    name: str
    description: str
    path: str
    installed: bool
    # Computed the same way an install would derive it, for display/filter
    # purposes only — nothing is written until the skill is installed.
    category: str = DEFAULT_CATEGORY


class BrowseListOut(BaseModel):
    source: str
    skills: list[BrowseSkillOut]


class BrowseInstallIn(BaseModel):
    source: str = Field(min_length=3, max_length=300)
    skill_path: str = Field(min_length=1, max_length=500)


class BrowseInstallOut(BaseModel):
    skill: SkillOut
    # "installed": newly created; "already_installed": idempotent replay.
    status: str


class AgentSkillOut(BaseModel):
    """One workspace skill as seen from an agent's profile."""

    skill_id: UUID
    name: str
    description: str
    source: str
    category: str = DEFAULT_CATEGORY
    enabled: bool
    enabled_for_agent: bool


class AgentSkillsUpdate(BaseModel):
    """Replace the agent's enabled-skill set with exactly these skills."""

    skill_ids: list[UUID] = Field(max_length=200)
