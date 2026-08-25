"""Model provider and model profile rows (plan 6.7, 6.8).

A provider is an endpoint + credential reference; a profile is a named,
priced model configuration on a provider. Costs are stored as micro-dollars
per million tokens so all arithmetic stays in integers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonDict, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin


class ModelProvider(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "model_provider"
    __table_args__ = (UniqueConstraint("workspace_id", "display_name"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str | None] = mapped_column(String(500), default=None)
    secret_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="SET NULL"), default=None
    )
    # Optional billing/admin credential (OpenAI admin key) for spend reporting;
    # stored exactly like the API key and never displayed.
    admin_secret_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="SET NULL"), default=None
    )
    # User-entered prepaid credit (micro-dollars) for providers without a
    # balance API; "remaining" is estimated against tracked/reported spend.
    credits_loaded_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    @property
    def has_admin_key(self) -> bool:
        """Whether a billing/admin credential is attached (never its value)."""
        return self.admin_secret_id is not None


class ModelProfile(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "model_profile"
    __table_args__ = (UniqueConstraint("workspace_id", "display_name"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("model_provider.id", ondelete="CASCADE"), index=True
    )
    model_name: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    context_window: Mapped[int | None] = mapped_column(Integer, default=None)
    input_cost_micros_per_million: Mapped[int | None] = mapped_column(Integer, default=None)
    output_cost_micros_per_million: Mapped[int | None] = mapped_column(Integer, default=None)
    # Where the stored price came from, one of ``jhin_models.pricing``'s
    # PriceSource values. This is what makes "never overwrite a price the
    # admin typed" enforceable: automatic refreshes only touch a row they
    # can prove they wrote themselves. NULL means unknown provenance — rows
    # that predate this column, or a price posted straight to the API — and
    # is deliberately treated as user-entered, because clobbering a real
    # contract price is far worse than leaving a stale one alone.
    price_source: Mapped[str | None] = mapped_column(String(32), default=None)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_reasoning: Mapped[bool] = mapped_column(Boolean, default=False)
    # Free-form adapter configuration, including optional ordered fallbacks
    # (plan 15.3) under a "fallback_profile_ids" key when configured.
    config_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)


class ModelObservedPrice(Base, UuidPkMixin, TimestampMixin):
    """A per-token rate *measured* from the provider's own invoice.

    OpenAI and Anthropic publish no pricing API, so a workspace on a
    negotiated contract — or using a model too new for any catalog — has no
    way to learn its real rate except by dividing dollars actually billed by
    tokens actually sent. One row per (provider, model) holds the latest such
    measurement together with the evidence behind it, so the UI can show not
    just the number but how much traffic it rests on and how far to trust it.

    A separate table rather than a column on ``model_profile`` because the
    measurement is a property of the *model on a provider*, not of a profile:
    several profiles can point at one model, and a measurement outlives the
    profile that happened to produce the traffic.
    """

    __tablename__ = "model_observed_price"
    __table_args__ = (UniqueConstraint("provider_id", "model_key"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("model_provider.id", ondelete="CASCADE"), index=True
    )
    # Normalised model identifier (jhin_models.pricing.normalize_model_id).
    model_key: Mapped[str] = mapped_column(String(200))
    # Split rates when the derivation could produce them; a blended rate
    # instead when the provider reported one undifferentiated cost and no
    # catalog ratio existed to split it. Never both, never a guessed pair.
    input_cost_micros_per_million: Mapped[int | None] = mapped_column(Integer, default=None)
    output_cost_micros_per_million: Mapped[int | None] = mapped_column(Integer, default=None)
    blended_cost_micros_per_million: Mapped[int | None] = mapped_column(Integer, default=None)
    # "provider_quantity" | "split" | "catalog_ratio" | "blended".
    derivation: Mapped[str] = mapped_column(String(32))
    # "high" | "medium" | "low" — see jhin_models.observed_pricing.
    confidence: Mapped[str] = mapped_column(String(16))
    # The human sentence explaining the derivation and its assumptions.
    note: Mapped[str] = mapped_column(Text, default="")
    # The evidence: sample size and the dollars it was divided out of.
    sample_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    sample_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    sample_runs: Mapped[int] = mapped_column(Integer, default=0)
    sample_cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    # Whole completed UTC days, [start, end).
    period_start: Mapped[datetime] = mapped_column(UtcDateTime)
    period_end: Mapped[datetime] = mapped_column(UtcDateTime)
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime)


class PriceCatalogSnapshot(Base, UuidPkMixin, TimestampMixin):
    """A community price catalog fetched once and kept for offline use.

    The built-in catalog in :mod:`jhin_models.pricing` ages; LiteLLM's
    community map does not. One row per workspace per source holds the
    trimmed, parsed map so the refresh is a deliberate admin action rather
    than a network dependency on every price lookup — and so a later fetch
    failure degrades to the last good snapshot instead of to nothing.
    """

    __tablename__ = "price_catalog_snapshot"
    __table_args__ = (UniqueConstraint("workspace_id", "source"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    # Catalog origin, e.g. "litellm".
    source: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(String(500), default="")
    # Provenance notice stored with the cached copy. LiteLLM's price map is
    # MIT-licensed, and MIT requires the copyright and permission notice to
    # travel with substantial portions of the material — caching it is
    # redistribution, so the notice lives on the row rather than only in docs.
    attribution: Mapped[str] = mapped_column(String(300), default="")
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    # ``{provider_type: {model_key: [input, output, context]}}`` — the
    # compact projection from ``pricing.refreshed_catalog_to_json``, a few
    # kilobytes rather than the ~1.8 MB source document.
    entries_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
