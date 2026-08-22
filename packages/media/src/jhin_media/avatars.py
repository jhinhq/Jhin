"""Atomic avatar activation shared by the API (uploads) and the agent worker
(generation results).

An agent's ``active_avatar_asset_id`` only moves after *every* variant of the
replacement has been validated and staged; the previous asset is retired in
the same transaction. Any failure before the caller commits leaves the
previous avatar untouched.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, MediaAsset
from jhin_domain import AvatarKind
from jhin_media.base import MediaStore
from jhin_media.images import NormalizedAvatar


class AgentNotFound(LookupError):
    """No agent with that id in that workspace (callers map this to 404)."""


async def _locked_agent(session: AsyncSession, workspace_id: UUID, agent_id: UUID) -> Agent:
    agent = await session.scalar(
        select(Agent)
        .where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
        .with_for_update()
    )
    if agent is None:
        raise AgentNotFound(str(agent_id))
    return agent


async def activate_avatar(
    session: AsyncSession,
    store: MediaStore,
    *,
    workspace_id: UUID,
    agent_id: UUID,
    normalized: NormalizedAvatar,
    avatar_kind: AvatarKind,
    created_by_user_id: UUID | None,
) -> tuple[Agent, MediaAsset, UUID | None]:
    """Stage the new asset and switch the agent to it (flush, no commit).

    Returns the agent, the new asset, and the id of the asset it replaced.
    """
    agent = await _locked_agent(session, workspace_id, agent_id)
    asset = await store.put_avatar(
        session,
        workspace_id,
        owner_agent_id=agent.id,
        created_by_user_id=created_by_user_id,
        normalized=normalized,
    )
    previous_asset_id = agent.active_avatar_asset_id
    agent.active_avatar_asset_id = asset.id
    agent.avatar_kind = avatar_kind.value
    await session.flush()
    if previous_asset_id is not None and previous_asset_id != asset.id:
        await store.retire(session, workspace_id, previous_asset_id)
    return agent, asset, previous_asset_id


async def clear_avatar(
    session: AsyncSession, store: MediaStore, *, workspace_id: UUID, agent_id: UUID
) -> tuple[Agent, UUID | None]:
    """Return the agent to initials (flush, no commit); idempotent."""
    agent = await _locked_agent(session, workspace_id, agent_id)
    previous_asset_id = agent.active_avatar_asset_id
    agent.active_avatar_asset_id = None
    agent.avatar_kind = AvatarKind.INITIALS.value
    await session.flush()
    if previous_asset_id is not None:
        await store.retire(session, workspace_id, previous_asset_id)
    return agent, previous_asset_id
