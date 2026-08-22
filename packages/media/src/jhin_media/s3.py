"""S3-compatible ``MediaStore`` adapter boundary (not implemented in V1).

Larger deployments can keep ``media_asset`` as the metadata/ownership row and
move variant bytes to object storage. The configuration shape is fixed here
so the operator contract is known; every method raises
:class:`NotImplementedError` until the adapter lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import MediaAsset
from jhin_media.base import MediaStore, StoredVariant
from jhin_media.images import NormalizedAvatar


@dataclass(frozen=True)
class S3MediaStoreConfig:
    bucket: str
    endpoint_url: str | None = None
    region: str | None = None
    key_prefix: str = "media/"


class S3MediaStore(MediaStore):
    """Placeholder adapter: construction succeeds, storage operations do not."""

    def __init__(self, config: S3MediaStoreConfig) -> None:
        self.config = config

    def object_key(self, workspace_id: UUID, asset_id: UUID, size: int) -> str:
        return f"{self.config.key_prefix}{workspace_id}/{asset_id}/{size}.webp"

    async def put_avatar(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        *,
        owner_agent_id: UUID,
        created_by_user_id: UUID | None,
        normalized: NormalizedAvatar,
    ) -> MediaAsset:
        raise NotImplementedError("S3MediaStore is a boundary stub; use PostgresMediaStore")

    async def get_variant(
        self, session: AsyncSession, workspace_id: UUID, asset_id: UUID, size: int
    ) -> StoredVariant | None:
        raise NotImplementedError("S3MediaStore is a boundary stub; use PostgresMediaStore")

    async def retire(self, session: AsyncSession, workspace_id: UUID, asset_id: UUID) -> None:
        raise NotImplementedError("S3MediaStore is a boundary stub; use PostgresMediaStore")
