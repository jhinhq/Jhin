"""Media assets and avatar generation records (experience design: agent
avatars and media).

``MediaAsset`` stores the *normalized* WebP variants of one avatar inline in
PostgreSQL (``bytea``) so a single-node self-hosted install is backup-safe
without object storage. Original upload bytes are never persisted. The
``MediaStore`` boundary in ``jhin_media`` keeps an S3-compatible adapter
possible for larger deployments without changing these rows.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import AvatarGenerationStatus, MediaAssetStatus

MEDIA_KIND_AVATAR = "avatar"
AVATAR_VARIANT_SIZES: tuple[int, ...] = (64, 128, 256)


class MediaAsset(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "media_asset"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_media_asset_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "owner_agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_media_asset_owner_agent",
            ondelete="CASCADE",
            use_alter=True,
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'rejected', 'retired')",
            name="status_values",
        ),
        CheckConstraint("kind IN ('avatar')", name="kind_values"),
        Index("ix_media_asset_workspace_owner", "workspace_id", "owner_agent_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default=MEDIA_KIND_AVATAR)
    owner_agent_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    status: Mapped[str] = mapped_column(String(16), default=MediaAssetStatus.PENDING.value)
    # Always ``image/webp`` for avatars: every variant is re-encoded.
    content_type: Mapped[str] = mapped_column(String(64), default="image/webp")
    # Dimensions of the largest variant (square).
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    # SHA-256 (hex) over the concatenated normalized variants; doubles as ETag.
    sha256: Mapped[str] = mapped_column(String(64))
    variant_64: Mapped[bytes] = mapped_column(LargeBinary)
    variant_128: Mapped[bytes] = mapped_column(LargeBinary)
    variant_256: Mapped[bytes] = mapped_column(LargeBinary)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    def variant_bytes(self, size: int) -> bytes:
        match size:
            case 64:
                return self.variant_64
            case 128:
                return self.variant_128
            case 256:
                return self.variant_256
        raise ValueError(f"unsupported avatar variant size {size}")


class AvatarGeneration(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "avatar_generation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_avatar_generation_agent",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="status_values",
        ),
        Index("ix_avatar_generation_workspace_agent", "workspace_id", "agent_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(StdUuid)
    # Derived from public identity fields plus the explicit user hint only;
    # never from system prompts, memory, or conversations.
    prompt: Mapped[str] = mapped_column(Text)
    prompt_hint: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    provider_type: Mapped[str] = mapped_column(String(32))
    provider_display_name: Mapped[str] = mapped_column(String(200))
    model_profile_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("model_profile.id", ondelete="SET NULL"), default=None
    )
    model_name: Mapped[str] = mapped_column(String(200))
    image_size: Mapped[str] = mapped_column(String(16), default="1024x1024")
    estimated_cost_micros: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(16), default=AvatarGenerationStatus.QUEUED.value)
    # User-safe, provider-redacted error summary.
    error: Mapped[str | None] = mapped_column(Text, default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    result_asset_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("media_asset.id", ondelete="SET NULL"), default=None
    )
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(200), default=None)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
