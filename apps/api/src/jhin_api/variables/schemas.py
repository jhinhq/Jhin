from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

VariableScope = Literal["agent", "team", "company"]


class VariableMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    workspace_id: UUID
    name: str
    scope: VariableScope
    scope_id: UUID
    sensitive: bool
    configured: bool
    version: int
    source_variable_id: UUID | None = None
    source_version: int | None = None
    description: str
    created_by_type: str
    created_by_id: UUID
    updated_by_type: str
    updated_by_id: UUID
    created_at: datetime
    updated_at: datetime


class PlainVariableOut(VariableMetadata):
    sensitive: Literal[False]
    value: str


class SensitiveVariableOut(VariableMetadata):
    sensitive: Literal[True]


VariableOut = Annotated[PlainVariableOut | SensitiveVariableOut, Field(discriminator="sensitive")]


class VariableListOut(BaseModel):
    items: list[VariableOut]
    total: int


class VariableCreateMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    scope: VariableScope
    scope_id: UUID
    description: str = Field(default="", max_length=2000)


class VariableCreate(VariableCreateMetadata):
    sensitive: Literal[False] = False
    value: str = Field(max_length=8192)


class SensitiveVariableCreate(VariableCreateMetadata):
    sensitive: Literal[True] = True
    value: SecretStr = Field(min_length=1, max_length=8192)


class VariableUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    value: str | None = Field(default=None, max_length=8192)


class SensitiveVariableUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    value: SecretStr = Field(min_length=1, max_length=8192)


class VariableCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    scope: VariableScope
    scope_id: UUID
    name: str | None = Field(default=None, min_length=1, max_length=120)
