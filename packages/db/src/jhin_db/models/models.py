"""Model provider and model profile rows (plan 6.7, 6.8).

A provider is an endpoint + credential reference; a profile is a named,
priced model configuration on a provider. Costs are stored as micro-dollars
per million tokens so all arithmetic stays in integers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


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
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_reasoning: Mapped[bool] = mapped_column(Boolean, default=False)
    # Free-form adapter configuration, including optional ordered fallbacks
    # (plan 15.3) under a "fallback_profile_ids" key when configured.
    config_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
