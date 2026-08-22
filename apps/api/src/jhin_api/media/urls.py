"""Relative media URL construction (import-light so agent schemas can use it)."""

from uuid import UUID

DEFAULT_AVATAR_SIZE = 128


def media_path(workspace_id: UUID, asset_id: UUID) -> str:
    return f"/api/v1/workspaces/{workspace_id}/media/{asset_id}"


def avatar_url_for(workspace_id: UUID, asset_id: UUID | None) -> str | None:
    """Relative, authenticated path (append ``?size=64|128|256``), or null."""
    if asset_id is None:
        return None
    return media_path(workspace_id, asset_id)
