"""Request/response contracts for model providers and profiles (plan 6.7, 6.8)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator

from jhin_domain import ModelProviderType
from jhin_models import EmbeddingConfig, WebSearchConfig
from jhin_models.embeddings import EMBEDDINGS_CONFIG_KEY
from jhin_models.web_search import WEB_SEARCH_CONFIG_KEY


def _validate_config_block(
    config_json: dict[str, Any], key: str, model_type: type[EmbeddingConfig] | type[WebSearchConfig]
) -> None:
    raw = config_json.get(key)
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError(f"config_json.{key} must be an object")
    try:
        model_type.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or key}: {err['msg']}" for err in exc.errors()
        )
        raise ValueError(f"config_json.{key} is invalid: {problems}") from None


def validate_profile_config(config_json: dict[str, Any]) -> dict[str, Any]:
    """Validate the typed capability blocks inside ``config_json``.

    ``embeddings`` (``{enabled, model, dimensions, cost_micros_per_million}``)
    must parse as :class:`EmbeddingConfig` and ``web_search``
    (``{enabled, max_uses}``) as :class:`WebSearchConfig`; other keys are
    passed through. Provider/model support for ``web_search`` is checked in
    the service layer, where the provider row is known.
    """
    _validate_config_block(config_json, EMBEDDINGS_CONFIG_KEY, EmbeddingConfig)
    _validate_config_block(config_json, WEB_SEARCH_CONFIG_KEY, WebSearchConfig)
    return config_json


class ModelProviderCreate(BaseModel):
    type: ModelProviderType
    display_name: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    secret_id: UUID | None = None
    # Optional billing/admin credential (OpenAI admin key) for spend reporting.
    admin_secret_id: UUID | None = None
    # Prepaid credit the admin loaded, in micro-dollars (for "≈ remaining").
    credits_loaded_micros: int | None = Field(default=None, ge=0, le=10**15)
    enabled: bool = True


class ModelProviderUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    secret_id: UUID | None = None
    admin_secret_id: UUID | None = None
    credits_loaded_micros: int | None = Field(default=None, ge=0, le=10**15)
    enabled: bool | None = None


class ModelProviderOut(BaseModel):
    id: UUID
    workspace_id: UUID
    type: ModelProviderType
    display_name: str
    base_url: str | None
    secret_id: UUID | None
    credits_loaded_micros: int | None = None
    has_admin_key: bool = False
    enabled: bool
    last_verified_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


BalanceSource = Literal["openrouter", "openai_admin", "tracked"]


class ProviderBalanceOut(BaseModel):
    """Balance and spend for one provider.

    ``tracked_*`` sums ``agent_run.estimated_cost_micros`` for runs on this
    provider's profiles (Jhin's own bookkeeping). ``provider_*`` comes from
    the provider's billing API when one exists and is reachable (cached for
    a minute); otherwise ``source`` is ``"tracked"`` and ``detail`` explains.
    """

    tracked_spent_month_micros: int
    tracked_spent_total_micros: int
    provider_spent_month_micros: int | None
    provider_remaining_micros: int | None
    credits_loaded_micros: int | None
    estimated_remaining_micros: int | None
    source: BalanceSource
    detail: str | None
    fetched_at: datetime


class ProviderSpendOut(BaseModel):
    provider_id: UUID
    display_name: str
    type: ModelProviderType
    spent_month_micros: int
    spent_total_micros: int


class WorkspaceSpendOut(BaseModel):
    """Tracked spend across the workspace plus the optional monthly budget."""

    spent_month_micros: int
    spent_total_micros: int
    period_start: datetime
    providers: list[ProviderSpendOut]
    monthly_budget_micros: int | None
    warning_threshold: float
    fetched_at: datetime


class ProviderVerifyResult(BaseModel):
    ok: bool
    detail: str


class ProviderDraftVerify(BaseModel):
    """Credentials to test before a provider is saved. Nothing is persisted."""

    type: ModelProviderType
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    secret_id: UUID | None = None


class ProviderModelEntry(BaseModel):
    """One pickable model with pricing when the source knows it."""

    id: str
    input_cost_micros_per_million: int | None = None
    output_cost_micros_per_million: int | None = None
    context_window: int | None = None
    source: Literal["provider", "catalog"] | None = None


class ProviderModelsResult(BaseModel):
    """Models a provider exposes (with prices when known), for the picker."""

    models: list[ProviderModelEntry]
    detail: str | None = None
    catalog_updated: str | None = None


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


class ProfilePricingRefreshResult(BaseModel):
    """Outcome of re-looking up a profile's prices."""

    updated: bool
    source: Literal["provider", "catalog"] | None
    detail: str
    profile: ModelProfileOut
