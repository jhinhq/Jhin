"""Request/response contracts for the secret store (plan 13.4).

``SecretOut`` is the only shape that ever leaves the API and it has no field
that can carry plaintext — GET never returns secret material by construction.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from jhin_domain import SecretType


class SecretCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=8192)
    type: SecretType = SecretType.API_KEY


class SecretUpdate(BaseModel):
    """Rename only. Changing the value goes through rotate."""

    name: str = Field(min_length=1, max_length=200)


class SecretRotate(BaseModel):
    value: str = Field(min_length=1, max_length=8192)


class SecretOut(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    type: SecretType
    masked_hint: str
    key_version: int
    last_used_at: datetime | None
    rotated_at: datetime | None
    created_at: datetime
    updated_at: datetime
