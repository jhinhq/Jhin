"""Request/response contracts for model providers and profiles (plan 6.7, 6.8)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from jhin_domain import ModelProviderType


class ModelProviderCreate(BaseModel):
    type: ModelProviderType
    display_name: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    secret_id: UUID | None = None
    enabled: bool = True


class ModelProviderUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    secret_id: UUID | None = None
    enabled: bool | None = None


class ModelProviderOut(BaseModel):
    id: UUID
    workspace_id: UUID
    type: ModelProviderType
    display_name: str
    base_url: str | None
    secret_id: UUID | None
    enabled: bool
    last_verified_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ProviderVerifyResult(BaseModel):
    ok: bool
    detail: str


class ModelProfileCreate(BaseModel):
    provider_id: UUID
    model_name: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    context_window: int | None = Field(default=None, ge=1)
    input_cost_micros_per_million: int | None = Field(default=None, ge=0)
    output_cost_micros_per_million: int | None = Field(default=None, ge=0)
    supports_tools: bool = True
    supports_reasoning: bool = False
    config_json: dict[str, Any] = Field(default_factory=dict)


class ModelProfileUpdate(BaseModel):
    provider_id: UUID | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    context_window: int | None = Field(default=None, ge=1)
    input_cost_micros_per_million: int | None = Field(default=None, ge=0)
    output_cost_micros_per_million: int | None = Field(default=None, ge=0)
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    config_json: dict[str, Any] | None = None


class ModelProfileOut(BaseModel):
    id: UUID
    workspace_id: UUID
    provider_id: UUID
    model_name: str
    display_name: str
    context_window: int | None
    input_cost_micros_per_million: int | None
    output_cost_micros_per_million: int | None
    supports_tools: bool
    supports_reasoning: bool
    config_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
