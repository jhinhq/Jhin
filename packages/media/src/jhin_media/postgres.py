"""PostgreSQL-backed ``MediaStore``: variants live in ``media_asset`` bytea
columns, so backups and workspace deletion cover avatars automatically."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import MediaAsset
from jhin_db.models.media import AVATAR_VARIANT_SIZES, MEDIA_KIND_AVATAR
from jhin_domain import MediaAssetStatus
from jhin_media.base import MediaStore, StoredVariant
from jhin_media.images import NormalizedAvatar


class PostgresMediaStore(MediaStore):
    async def put_avatar(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        *,
        owner_agent_id: UUID,
        created_by_user_id: UUID | None,
        normalized: NormalizedAvatar,
    ) -> MediaAsset:
        missing = [size for size in AVATAR_VARIANT_SIZES if size not in normalized.variants]
        if missing:
            raise ValueError(f"normalized avatar is missing variants: {missing}")
        asset = MediaAsset(
            workspace_id=workspace_id,
            kind=MEDIA_KIND_AVATAR,
            owner_agent_id=owner_agent_id,
            status=MediaAssetStatus.ACTIVE.value,
            content_type=normalized.content_type,
            width=normalized.width,
            height=normalized.height,
            sha256=normalized.sha256,
            variant_64=normalized.variant(64),
            variant_128=normalized.variant(128),
            variant_256=normalized.variant(256),
            created_by_user_id=created_by_user_id,
        )
        session.add(asset)
        await session.flush()
        return asset

    async def get_variant(
        self, session: AsyncSession, workspace_id: UUID, asset_id: UUID, size: int
    ) -> StoredVariant | None:
        if size not in AVATAR_VARIANT_SIZES:
            return None
        asset = await session.scalar(
            select(MediaAsset).where(
                MediaAsset.id == asset_id,
                MediaAsset.workspace_id == workspace_id,
                MediaAsset.status == MediaAssetStatus.ACTIVE.value,
            )
        )
        if asset is None:
            return None
        return StoredVariant(
            asset_id=asset.id,
            size=size,
            content_type=asset.content_type,
            data=asset.variant_bytes(size),
            sha256=asset.sha256,
        )

    async def retire(self, session: AsyncSession, workspace_id: UUID, asset_id: UUID) -> None:
        asset = await session.scalar(
            select(MediaAsset).where(
                MediaAsset.id == asset_id, MediaAsset.workspace_id == workspace_id
            )
        )
        if asset is None or asset.status == MediaAssetStatus.RETIRED.value:
            return
        asset.status = MediaAssetStatus.RETIRED.value
        asset.retired_at = datetime.now(UTC)
        await session.flush()
