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
    # No ``min_length`` here, deliberately, unlike ``BootstrapRequest``: the
    # policy is enforced in the service either way, and letting Pydantic reject
    # a short password first would answer a signed-in user with
    # "String should have at least 12 characters" instead of the sentence the
    # policy actually wants to say. One rule, one message.
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


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


class ApiKeyIdentityOut(BaseModel):
    """The key a caller presented, resolved to what it may actually do.

    ``scopes`` is the *effective* set — already intersected with the key's
    role ceiling and with its creator's role today — so a client can gate its
    UI on exactly what the next call will be allowed to do.
    """

    id: UUID
    name: str
    prefix: str
    workspace_id: UUID
    role_ceiling: WorkspaceRole
    scopes: list[str]


class IdentityResponse(BaseModel):
    """Who is calling, and where they may act.

    The same shape for both credentials, which is the point: a client boots
    off this one call without knowing which it holds. A session lists every
    workspace the user belongs to and leaves ``api_key`` null; a key lists
    exactly the one workspace it is bound to and describes itself.
    """

    user: UserOut
    memberships: list[MembershipOut]
    api_key: ApiKeyIdentityOut | None = None
