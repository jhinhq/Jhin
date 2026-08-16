"""Request/response contracts for workspaces and memberships."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from jhin_domain import WorkspaceRole


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    default_timezone: str = Field(default="UTC", max_length=64)


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    default_timezone: str | None = Field(default=None, max_length=64)
    default_model_profile_id: UUID | None = None


class WorkspaceOut(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str
    default_timezone: str
    default_model_profile_id: UUID | None
    created_at: datetime
    updated_at: datetime


class MemberCreate(BaseModel):
    email: EmailStr
    role: WorkspaceRole


class MemberUpdate(BaseModel):
    role: WorkspaceRole


class MemberOut(BaseModel):
    id: UUID
    user_id: UUID
    email: str
    display_name: str
    role: WorkspaceRole
    created_at: datetime
