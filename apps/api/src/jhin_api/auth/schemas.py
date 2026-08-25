"""Request/response contracts for authentication endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from jhin_api.security.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from jhin_domain import WorkspaceRole


class BootstrapStatus(BaseModel):
    needs_bootstrap: bool


class BootstrapRequest(BaseModel):
    email: EmailStr
    # The full policy (common-password list, no-email-in-password) is enforced
    # in the service so the message can explain *why*; the bound here just
    # keeps obviously-too-short values out of the hasher.
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    display_name: str = Field(min_length=1, max_length=200)
    workspace_name: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class SessionsRevokedResponse(BaseModel):
    revoked_sessions: int


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
