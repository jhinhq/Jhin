"""Cross-workspace isolation regressions.

Every one of these fails if a service query stops filtering by workspace. They
are deliberately written against the service layer, because that is where the
filter lives — the RBAC dependency only proves *membership*, not that the row
you asked for belongs to the workspace you are a member of.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.models import service as models_service
from jhin_db.models import Agent, ModelProfile, ModelProvider, User, Workspace
from jhin_domain import WorkspaceRole, new_uuid7


async def make_workspace(session: AsyncSession, name: str) -> WorkspaceContext:
    user = User(
        email=f"{name}-{new_uuid7().hex[:8]}@example.com",
        display_name=name,
        password_hash="x",
    )
    workspace = Workspace(name=name, slug=f"{name}-{new_uuid7().hex[:8]}")
    session.add_all([user, workspace])
    await session.flush()
    return WorkspaceContext(user=user, workspace_id=workspace.id, role=WorkspaceRole.ADMIN)


async def make_provider_and_profile(
    session: AsyncSession, ctx: WorkspaceContext
) -> tuple[ModelProvider, ModelProfile]:
    provider = ModelProvider(
        workspace_id=ctx.workspace_id,
        type="openai",
        display_name="OpenAI",
    )
    session.add(provider)
    await session.flush()
    profile = ModelProfile(
        workspace_id=ctx.workspace_id,
        provider_id=provider.id,
        model_name="gpt-4o-mini",
        display_name=f"default-{new_uuid7().hex[:6]}",
    )
    session.add(profile)
    await session.flush()
    return provider, profile


# --- reads ------------------------------------------------------------------


async def test_provider_from_another_workspace_is_a_404(session: AsyncSession) -> None:
    mine = await make_workspace(session, "mine")
    theirs = await make_workspace(session, "theirs")
    provider, _ = await make_provider_and_profile(session, theirs)
    with pytest.raises(HTTPException) as caught:
        await models_service.get_provider(session, mine.workspace_id, provider.id)
    assert caught.value.status_code == 404


async def test_profile_from_another_workspace_is_a_404(session: AsyncSession) -> None:
    mine = await make_workspace(session, "mine")
    theirs = await make_workspace(session, "theirs")
    _, profile = await make_provider_and_profile(session, theirs)
    with pytest.raises(HTTPException) as caught:
        await models_service.get_profile(session, mine.workspace_id, profile.id)
    assert caught.value.status_code == 404


async def test_listing_never_crosses_the_workspace_boundary(session: AsyncSession) -> None:
    mine = await make_workspace(session, "mine")
    theirs = await make_workspace(session, "theirs")
    await make_provider_and_profile(session, theirs)
    assert await models_service.list_providers(session, mine.workspace_id) == []
    assert await models_service.list_profiles(session, mine.workspace_id) == []


# --- writes -----------------------------------------------------------------


async def test_deleting_a_profile_cannot_clear_another_workspaces_default(
    session: AsyncSession,
) -> None:
    """The delete path clears "the workspace whose default is this profile".

    Selecting that row by profile id alone reaches across the boundary, so the
    query is pinned to the acting workspace and this proves it stays pinned.
    """
    mine = await make_workspace(session, "mine")
    theirs = await make_workspace(session, "theirs")
    _, my_profile = await make_provider_and_profile(session, mine)
    _, their_profile = await make_provider_and_profile(session, theirs)

    their_workspace = await session.get(Workspace, theirs.workspace_id)
    assert their_workspace is not None
    their_workspace.default_model_profile_id = their_profile.id
    my_workspace = await session.get(Workspace, mine.workspace_id)
    assert my_workspace is not None
    my_workspace.default_model_profile_id = my_profile.id
    await session.flush()

    await models_service.delete_profile(
        session, mine, my_profile.id, request_id=new_uuid7(), ip_hash="hash"
    )
    await session.refresh(their_workspace)
    assert their_workspace.default_model_profile_id == their_profile.id
    await session.refresh(my_workspace)
    assert my_workspace.default_model_profile_id is None


async def test_deleting_a_profile_ignores_agents_in_other_workspaces(
    session: AsyncSession,
) -> None:
    """A foreign agent pinned to this profile must not block the delete — and,
    more importantly, its name must not leak into the 409 message."""
    mine = await make_workspace(session, "mine")
    theirs = await make_workspace(session, "theirs")
    _, my_profile = await make_provider_and_profile(session, mine)
    session.add(
        Agent(
            workspace_id=theirs.workspace_id,
            name="Their Secret Agent",
            slug=f"their-agent-{new_uuid7().hex[:6]}",
            model_profile_id=my_profile.id,
        )
    )
    await session.flush()

    await models_service.delete_profile(
        session, mine, my_profile.id, request_id=new_uuid7(), ip_hash="hash"
    )
    assert await models_service.list_profiles(session, mine.workspace_id) == []


async def test_deleting_a_provider_does_not_name_agents_from_other_workspaces(
    session: AsyncSession,
) -> None:
    mine = await make_workspace(session, "mine")
    theirs = await make_workspace(session, "theirs")
    my_provider, my_profile = await make_provider_and_profile(session, mine)
    session.add(
        Agent(
            workspace_id=theirs.workspace_id,
            name="Their Secret Agent",
            slug=f"their-agent-{new_uuid7().hex[:6]}",
            model_profile_id=my_profile.id,
        )
    )
    await session.flush()

    await models_service.delete_provider(
        session, mine, my_provider.id, request_id=new_uuid7(), ip_hash="hash"
    )
    assert await models_service.list_providers(session, mine.workspace_id) == []


async def test_deleting_a_provider_still_blocks_on_agents_in_this_workspace(
    session: AsyncSession,
) -> None:
    """The scoping fix must not weaken the in-use guard it narrowed."""
    mine = await make_workspace(session, "mine")
    my_provider, my_profile = await make_provider_and_profile(session, mine)
    session.add(
        Agent(
            workspace_id=mine.workspace_id,
            name="My Agent",
            slug=f"my-agent-{new_uuid7().hex[:6]}",
            model_profile_id=my_profile.id,
        )
    )
    await session.flush()
    with pytest.raises(HTTPException) as caught:
        await models_service.delete_provider(
            session, mine, my_provider.id, request_id=new_uuid7(), ip_hash="hash"
        )
    assert caught.value.status_code == 409
    assert "My Agent" in str(caught.value.detail)


async def test_deleting_a_profile_still_blocks_on_agents_in_this_workspace(
    session: AsyncSession,
) -> None:
    mine = await make_workspace(session, "mine")
    _, my_profile = await make_provider_and_profile(session, mine)
    session.add(
        Agent(
            workspace_id=mine.workspace_id,
            name="My Agent",
            slug=f"my-agent-{new_uuid7().hex[:6]}",
            model_profile_id=my_profile.id,
        )
    )
    await session.flush()
    with pytest.raises(HTTPException) as caught:
        await models_service.delete_profile(
            session, mine, my_profile.id, request_id=new_uuid7(), ip_hash="hash"
        )
    assert caught.value.status_code == 409
