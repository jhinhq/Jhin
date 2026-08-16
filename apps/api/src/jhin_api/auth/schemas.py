"""Request/response contracts for authentication endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from jhin_domain import WorkspaceRole


class BootstrapStatus(BaseModel):
    needs_bootstrap: bool


class BootstrapRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    workspace_name: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    created_at: datetime


class MembershipOut(BaseModel):
    workspace_id: UUID
    workspace_name: str
    workspace_slug: str
    role: WorkspaceRole


class MeResponse(BaseModel):
    user: UserOut
    memberships: list[MembershipOut]
