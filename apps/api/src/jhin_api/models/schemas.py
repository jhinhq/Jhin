"""Request/response contracts for model providers and profiles (plan 6.7, 6.8)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator

from jhin_domain import ModelProviderType
from jhin_models import (
    DEFAULT_KEEP_ALIVE,
    KEEP_ALIVE_UNLOAD,
    EmbeddingConfig,
    ReasoningConfig,
    WebSearchConfig,
    validate_keep_alive,
)
from jhin_models.embeddings import EMBEDDINGS_CONFIG_KEY
from jhin_models.reasoning import REASONING_CONFIG_KEY
from jhin_models.web_search import WEB_SEARCH_CONFIG_KEY


def _validate_config_block(
    config_json: dict[str, Any],
    key: str,
    model_type: type[EmbeddingConfig] | type[WebSearchConfig] | type[ReasoningConfig],
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
    must parse as :class:`EmbeddingConfig`, ``web_search``
    (``{enabled, max_uses}``) as :class:`WebSearchConfig`, and ``reasoning``
    (``{effort, supports_reasoning}``) as :class:`ReasoningConfig` — which
    rejects any effort outside ``none``/``low``/``medium``/``high``. Other
    keys are passed through. Provider/model support for ``web_search`` and
    ``reasoning`` is checked in the service layer, where the provider row is
    known.
    """
    _validate_config_block(config_json, EMBEDDINGS_CONFIG_KEY, EmbeddingConfig)
    _validate_config_block(config_json, WEB_SEARCH_CONFIG_KEY, WebSearchConfig)
    _validate_config_block(config_json, REASONING_CONFIG_KEY, ReasoningConfig)
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


#: Where a stored price came from. Mirrors ``jhin_models.pricing.PriceSource``
#: and is ordered by authority: user-entered beats a measured rate, which
#: beats a live provider price, which beats a refreshed catalog, which beats
#: the list prices built into the release.
PriceSourceName = Literal["user", "observed", "provider", "refreshed_catalog", "catalog"]


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
    # Cost carried by runs whose model profile has since been deleted. The
    # spend is real and stays in the totals, so it needs a name of its own —
    # otherwise the provider breakdown silently stops adding up.
    deleted_model_month_micros: int
    deleted_model_total_micros: int
    monthly_budget_micros: int | None
    warning_threshold: float
    fetched_at: datetime
    # Runs this period on models with no price: their real cost is missing
    # from the totals above, so the UI must say so rather than imply the
    # number is complete.
    untracked: list["UntrackedModelOut"] = Field(default_factory=list)
    untracked_runs: int = 0


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
    # Which of the five sources last wrote the price. The UI renders it as a
    # badge, and it is what makes "never overwrite a price you typed"
    # inspectable rather than a promise.
    price_source: PriceSourceName | None = None
    supports_tools: bool
    supports_reasoning: bool
    config_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProfilePricingRefreshResult(BaseModel):
    """Outcome of re-looking up a profile's prices."""

    updated: bool
    source: PriceSourceName | None
    detail: str
    profile: ModelProfileOut


class PriceCandidateOut(BaseModel):
    """A price some source is offering for a model."""

    source: PriceSourceName
    input_cost_micros_per_million: int | None = None
    output_cost_micros_per_million: int | None = None
    context_window: int | None = None
    detail: str = ""


class ObservedRateOut(BaseModel):
    """A rate measured from the provider's own invoice, with its evidence.

    ``blended_cost_micros_per_million`` is filled instead of the input/output
    pair when the provider reported one undifferentiated cost and no list
    price existed to split it — an honest single number rather than a guessed
    pair. ``note`` spells out the derivation and any assumption in it.
    """

    model_key: str
    input_cost_micros_per_million: int | None
    output_cost_micros_per_million: int | None
    blended_cost_micros_per_million: int | None
    derivation: Literal["provider_quantity", "split", "catalog_ratio", "blended"]
    confidence: Literal["high", "medium", "low"]
    note: str
    sample_runs: int
    sample_input_tokens: int
    sample_output_tokens: int
    computed_at: datetime


class ProfilePricingOut(BaseModel):
    """One profile's price, where it came from, and what could improve it."""

    profile_id: UUID
    display_name: str
    model_name: str
    provider_id: UUID
    provider_type: ModelProviderType
    input_cost_micros_per_million: int | None
    output_cost_micros_per_million: int | None
    price_source: PriceSourceName | None
    price_source_label: str
    priced: bool
    pricing_page_url: str | None
    runs_this_month: int
    suggestion: PriceCandidateOut | None
    suggestion_label: str | None
    observed: ObservedRateOut | None


class UntrackedModelOut(BaseModel):
    """A model that ran but had no price, so its spend was recorded as zero."""

    model_name: str
    runs: int
    input_tokens: int
    output_tokens: int


class PricingStatusOut(BaseModel):
    """Everything the Models page needs to talk honestly about prices."""

    catalog_updated: str
    catalog_stale: bool
    refreshed_source: str | None
    refreshed_fetched_at: datetime | None
    refreshed_entry_count: int
    # MIT attribution for the cached community catalog, shown wherever one of
    # its prices is used (see docs/architecture/models.md).
    refreshed_attribution: str | None
    refreshed_project_url: str
    profiles: list[ProfilePricingOut]
    untracked: list[UntrackedModelOut]
    untracked_runs: int
    reconcile_available: bool
    reconcile_detail: str
    pricing_pages: dict[str, str]


class AppliedPriceOut(BaseModel):
    """One profile's price changing, old value and new source included."""

    profile_id: UUID
    display_name: str
    model_name: str
    from_input_micros_per_million: int | None
    from_output_micros_per_million: int | None
    from_source: PriceSourceName | None
    to_input_micros_per_million: int
    to_output_micros_per_million: int
    to_source: PriceSourceName
    detail: str


class DerivedRateOut(BaseModel):
    """A rate this reconciliation measured."""

    model_key: str
    derivation: Literal["provider_quantity", "split", "catalog_ratio", "blended"]
    confidence: Literal["high", "medium", "low"]
    note: str
    input_micros_per_million: int | None
    output_micros_per_million: int | None
    blended_micros_per_million: int | None
    input_tokens: int
    output_tokens: int
    runs: int
    cost_micros: int


class SkippedModelOut(BaseModel):
    """A model the reconciliation refused to price, and why."""

    model_key: str
    reason: str


class SkippedProviderOut(BaseModel):
    provider_id: UUID
    display_name: str
    reason: str


class ProviderReconcileOut(BaseModel):
    provider_id: UUID
    display_name: str
    provider_type: ModelProviderType
    derived: list[DerivedRateOut]
    skipped: list[SkippedModelOut]
    applied: list[AppliedPriceOut]
    period_start: datetime
    period_end: datetime
    billed_micros: int
    unattributed_micros: int
    unattributed_labels: list[str]
    detail: str


class ReconcilePricingResult(BaseModel):
    """What a reconciliation derived, applied, and deliberately skipped."""

    providers: list[ProviderReconcileOut]
    skipped_providers: list[SkippedProviderOut]
    computed_at: datetime
    detail: str


class CatalogRefreshResultOut(BaseModel):
    """Outcome of refreshing the community price catalog."""

    updated: bool
    entry_count: int
    fetched_at: datetime | None
    source: str
    source_url: str
    attribution: str
    detail: str
    repriced: list[AppliedPriceOut]


class OllamaModelOut(BaseModel):
    """One model on an Ollama host: ``/api/tags`` merged with ``/api/ps`` and
    ``/api/show``.

    ``context_length`` is the architecture's maximum from ``/api/show``, not
    the context a running instance was started with. ``keeps_loaded`` is
    computed server-side because a ``keep_alive`` of ``-1`` shows up as an
    ``expires_at`` centuries out, which is not a date worth rendering.
    """

    name: str
    size_bytes: int
    family: str | None
    parameter_size: str | None
    quantization: str | None
    modified_at: datetime | None
    context_length: int | None
    capabilities: list[str]
    loaded: bool
    size_vram_bytes: int | None
    expires_at: datetime | None
    keeps_loaded: bool


class OllamaModelsResult(BaseModel):
    """Installed models with their loaded state, or an explanation of why
    the list is empty."""

    models: list[OllamaModelOut]
    detail: str | None
    fetched_at: datetime


class OllamaLoadedModelOut(BaseModel):
    """One model resident in memory on the host (``/api/ps``).
    ``size_vram_bytes`` is 0 on a CPU-only host."""

    name: str
    size_bytes: int
    size_vram_bytes: int
    expires_at: datetime | None
    keeps_loaded: bool
    context_length: int | None


class OllamaLoadedResult(BaseModel):
    models: list[OllamaLoadedModelOut]
    detail: str | None
    fetched_at: datetime


class OllamaModelDetailsOut(BaseModel):
    name: str
    family: str | None
    parameter_size: str | None
    quantization: str | None
    context_length: int | None
    capabilities: list[str]
    license: str | None


class OllamaModelDetailsResult(BaseModel):
    model: OllamaModelDetailsOut | None
    detail: str | None


class OllamaLoadIn(BaseModel):
    """Bring a model into memory on the host and keep it there for
    ``keep_alive`` after its last request: a duration like ``5m`` or ``1h``,
    or ``-1`` to keep it until an explicit unload."""

    model: str = Field(min_length=1, max_length=200)
    keep_alive: str = Field(default=DEFAULT_KEEP_ALIVE, max_length=16)

    @field_validator("keep_alive")
    @classmethod
    def _keep_alive(cls, value: str) -> str:
        cleaned = validate_keep_alive(value)
        if cleaned == KEEP_ALIVE_UNLOAD:
            raise ValueError("keep_alive 0 unloads a model; call unload instead")
        return cleaned


class OllamaUnloadIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)


OllamaLoadStatus = Literal["loaded", "loading", "unloaded", "failed"]


class OllamaLoadResultOut(BaseModel):
    """Outcome of a load or unload.

    ``loading`` is a success: the host is still reading the model in after
    the response budget ran out, and ``/ollama/loaded`` shows it once done.
    """

    ok: bool
    status: OllamaLoadStatus
    model: str
    keep_alive: str | None
    detail: str


# ``WorkspaceSpendOut`` references a class defined further down; resolve it
# now so the first request does not pay for a deferred rebuild.
WorkspaceSpendOut.model_rebuild()
