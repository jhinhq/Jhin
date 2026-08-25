"""Request/response contracts for invitations, API keys, and the scope catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from jhin_domain import WorkspaceRole, is_known_scope

# Expiry is expressed as an amount plus a unit so the UI can offer a natural
# picker; "never" is the explicit absence of an expiry, not a huge number.
ExpiryUnit = Literal["minutes", "hours", "days", "never"]
InvitationStatus = Literal["pending", "accepted", "revoked", "expired"]
ApiKeyStatus = Literal["active", "revoked", "expired"]

MAX_EXPIRY_AMOUNT = 100_000


class InvitationCreate(BaseModel):
    email: EmailStr
    role: WorkspaceRole


class InvitationOut(BaseModel):
    id: UUID
    email: str
    role: WorkspaceRole
    status: InvitationStatus
    invited_by_user_id: UUID | None
    invited_by_name: str | None
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class InvitationCreated(BaseModel):
    """The invite URL is returned exactly once, at creation."""

    invitation: InvitationOut
    invite_url: str
    token: str


class InvitationPreview(BaseModel):
    """Everything the public accept screen is allowed to know."""

    workspace_name: str
    email: str
    role: WorkspaceRole
    expires_at: datetime


class InvitationAccept(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class ScopeOut(BaseModel):
    key: str
    category: str
    action: str
    label: str
    description: str
    min_role: WorkspaceRole
    # False when the requesting user's own role may not hold this scope; the UI
    # greys it out and explains why instead of hiding it.
    available: bool


class ScopeCategoryOut(BaseModel):
    key: str
    label: str
    description: str
    scopes: list[ScopeOut]


class ScopeCatalogOut(BaseModel):
    your_role: WorkspaceRole
    categories: list[ScopeCategoryOut]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=200)
    expires_in: int | None = Field(default=None, ge=1, le=MAX_EXPIRY_AMOUNT)
    expires_unit: ExpiryUnit = "never"

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted({scope for scope in value if not is_known_scope(scope)})
        if unknown:
            raise ValueError(f"Unknown scopes: {', '.join(unknown)}")
        return value


class ApiKeyOut(BaseModel):
    id: UUID
    name: str
    prefix: str
    scopes: list[str]
    role_ceiling: WorkspaceRole
    created_by_user_id: UUID | None
    created_by_name: str | None
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    status: ApiKeyStatus


class ApiKeyCreated(BaseModel):
    """``key`` is shown once and never retrievable again."""

    api_key: ApiKeyOut
    key: str


class ApiKeyUsageOut(BaseModel):
    id: UUID
    api_key_id: UUID
    api_key_name: str | None
    api_key_prefix: str | None
    acting_user_id: UUID | None
    acting_user_name: str | None
    method: str
    path: str
    status_code: int
    created_at: datetime


class ApiKeyUsagePage(BaseModel):
    items: list[ApiKeyUsageOut]
    total: int
