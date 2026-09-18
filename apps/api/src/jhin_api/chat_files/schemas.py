from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    repository_url: str | None = Field(default=None, max_length=2000)
    source_revision: str | None = Field(default=None, max_length=300)
    context: str = Field(default="", max_length=50_000)

    @field_validator("repository_url")
    @classmethod
    def repository(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Repository URLs must be HTTPS without embedded credentials")
        return value


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    repository_url: str | None = Field(default=None, max_length=2000)
    source_revision: str | None = Field(default=None, max_length=300)
    context: str | None = Field(default=None, max_length=50_000)
    archived: bool | None = None

    @field_validator("repository_url")
    @classmethod
    def repository(cls, value: str | None) -> str | None:
        return ProjectCreate.repository(value)


class ProjectOut(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    workspace_id: UUID
    archived: bool
    created_at: datetime
    updated_at: datetime


class RevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    file_id: UUID
    version: int
    sha256: str
    size_bytes: int
    mime_type: str
    preview_kind: str
    created_at: datetime
    created_by_user_id: UUID | None
    source_run_id: UUID | None


class FileOut(BaseModel):
    id: UUID
    workspace_id: UUID
    conversation_id: UUID
    name: str
    path: str
    kind: str
    status: str
    error: str | None
    current_revision_id: UUID
    version: int
    mime_type: str
    size_bytes: int
    sha256: str
    preview_kind: str
    extracted_text: str
    extraction_truncated: bool
    created_at: datetime
    updated_at: datetime
    download_url: str
    preview_url: str


class FileListOut(BaseModel):
    items: list[FileOut]
    has_more: bool = False


class RevisionListOut(BaseModel):
    items: list[RevisionOut]


class ContentOut(BaseModel):
    file_id: UUID
    revision_id: UUID
    path: str
    content: str
    truncated: bool
    editable: bool
    sha256: str


class ContentSave(BaseModel):
    content: str = Field(max_length=1_000_000)
    expected_revision_id: UUID
    lease_generation: int = Field(ge=1)


class PublishIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    title: str | None = Field(default=None, max_length=300)


class CheckpointIn(BaseModel):
    label: str = Field(default="Checkpoint", min_length=1, max_length=200)


class CheckpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    label: str
    manifest_json: dict[str, Any]
    excluded_json: list[Any]
    created_at: datetime


class RestoreIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    expected_revisions: dict[str, str | None]
    lease_generation: int = Field(ge=1)


class AnnotationIn(BaseModel):
    revision_id: UUID
    text: str = Field(min_length=1, max_length=10_000)
    location: dict[str, Any] = Field(default_factory=dict)


class AnnotationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    file_id: UUID
    revision_id: UUID
    text: str
    location_json: dict[str, Any]
    created_by_user_id: UUID | None
    created_at: datetime
