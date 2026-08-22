"""The ``MediaStore`` boundary.

V1 ships the PostgreSQL implementation (normalized variants inline, backup-
safe for single-node self-hosting). The interface is deliberately narrow so
an S3-compatible store can replace the byte storage without touching the
API, the normalizer, or the ``media_asset`` metadata rows.

Stores never commit: they stage rows in the caller's session so avatar
activation and asset persistence land in one transaction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import MediaAsset
from jhin_media.images import NormalizedAvatar


@dataclass(frozen=True)
class StoredVariant:
    asset_id: UUID
    size: int
    content_type: str
    data: bytes
    sha256: str


class MediaStore(ABC):
    @abstractmethod
    async def put_avatar(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        *,
        owner_agent_id: UUID,
        created_by_user_id: UUID | None,
        normalized: NormalizedAvatar,
    ) -> MediaAsset:
        """Persist every variant as one ``active`` asset (flush, no commit)."""

    @abstractmethod
    async def get_variant(
        self, session: AsyncSession, workspace_id: UUID, asset_id: UUID, size: int
    ) -> StoredVariant | None:
        """Return one active variant, or ``None`` when unknown in this workspace."""

    @abstractmethod
    async def retire(self, session: AsyncSession, workspace_id: UUID, asset_id: UUID) -> None:
        """Mark an asset non-servable (flush, no commit). Unknown ids are a no-op."""
