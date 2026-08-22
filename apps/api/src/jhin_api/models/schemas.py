"""Request/response contracts for model providers and profiles (plan 6.7, 6.8)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator

from jhin_domain import ModelProviderType
from jhin_models import EmbeddingConfig
from jhin_models.embeddings import EMBEDDINGS_CONFIG_KEY


def validate_profile_config(config_json: dict[str, Any]) -> dict[str, Any]:
    """Validate the typed capability blocks inside ``config_json``.

    ``embeddings`` (``{enabled, model, dimensions, cost_micros_per_million}``)
    must parse as :class:`EmbeddingConfig`; other keys are passed through.
    """
    raw = config_json.get(EMBEDDINGS_CONFIG_KEY)
    if raw is None:
        return config_json
    if not isinstance(raw, dict):
        raise ValueError(f"config_json.{EMBEDDINGS_CONFIG_KEY} must be an object")
    try:
        EmbeddingConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or EMBEDDINGS_CONFIG_KEY}: {err['msg']}"
            for err in exc.errors()
        )
        raise ValueError(f"config_json.{EMBEDDINGS_CONFIG_KEY} is invalid: {problems}") from None
    return config_json


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

    @field_validator("config_json")
    @classmethod
    def _config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_profile_config(value)


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

    @field_validator("config_json")
    @classmethod
    def _config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else validate_profile_config(value)


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
