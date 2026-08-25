"""First-run onboarding state: per person, per workspace, and durable.

The behaviour that matters is negative — after somebody skips the tour it must
never come back on its own, on any device — so most of these assert what the
state is *not*.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.onboarding import service
from jhin_db.models import User, Workspace, WorkspaceMembership
from jhin_domain import WorkspaceRole


async def make_member(
    session: AsyncSession, workspace: Workspace, role: WorkspaceRole = WorkspaceRole.OWNER
) -> User:
    user = User(
        email=f"user-{uuid4().hex[:12]}@example.com",
        display_name="Member",
        password_hash="x",
    )
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=role.value))
    await session.flush()
    return user


async def make_workspace(session: AsyncSession) -> Workspace:
    workspace = Workspace(name="Test", slug=f"test-{uuid4().hex[:12]}")
    session.add(workspace)
    await session.flush()
    return workspace


async def test_a_brand_new_membership_is_pending(session: AsyncSession) -> None:
    workspace = await make_workspace(session)
    user = await make_member(session, workspace)

    state = await service.get_state(session, workspace.id, user.id)

    assert state.status == "pending"
    assert state.last_step is None
    assert state.updated_at is None


async def test_dismissing_survives_a_fresh_read(session: AsyncSession) -> None:
    workspace = await make_workspace(session)
    user = await make_member(session, workspace)

    workspace_id, user_id = workspace.id, user.id

    await service.set_state(
        session, workspace_id, user_id, new_status="dismissed", last_step="model"
    )
    # Detach everything: the read that follows must come back from the
    # database, not from the identity map that just wrote it.
    session.expunge_all()

    state = await service.get_state(session, workspace_id, user_id)
    assert state.status == "dismissed"
    assert state.last_step == "model"
    assert state.updated_at is not None


async def test_completing_is_recorded(session: AsyncSession) -> None:
    workspace = await make_workspace(session)
    user = await make_member(session, workspace)

    await service.set_state(
        session, workspace.id, user.id, new_status="completed", last_step="explore"
    )

    assert (await service.get_state(session, workspace.id, user.id)).status == "completed"


async def test_one_person_dismissing_does_not_silence_a_colleague(
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session)
    owner = await make_member(session, workspace)
    invited = await make_member(session, workspace, WorkspaceRole.MEMBER)

    await service.set_state(session, workspace.id, owner.id, new_status="dismissed", last_step=None)

    assert (await service.get_state(session, workspace.id, invited.id)).status == "pending"


async def test_state_is_per_workspace(session: AsyncSession) -> None:
    first = await make_workspace(session)
    second = await make_workspace(session)
    user = User(email="dual@example.com", display_name="Dual", password_hash="x")
    session.add(user)
    await session.flush()
    for workspace in (first, second):
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER.value
            )
        )
    await session.flush()

    await service.set_state(session, first.id, user.id, new_status="completed", last_step=None)

    assert (await service.get_state(session, second.id, user.id)).status == "pending"


async def test_unrelated_membership_settings_survive_a_write(session: AsyncSession) -> None:
    workspace = await make_workspace(session)
    user = await make_member(session, workspace)
    membership = await service._membership(session, workspace.id, user.id)
    membership.settings_json = {"something_else": {"kept": True}}
    await session.flush()

    await service.set_state(session, workspace.id, user.id, new_status="completed", last_step=None)

    refreshed = await service._membership(session, workspace.id, user.id)
    assert refreshed.settings_json["something_else"] == {"kept": True}


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "not-a-dict",
        {},
        {"status": "who-knows"},
        {"status": "completed", "updated_at": "not-a-timestamp"},
    ],
)
async def test_unreadable_state_degrades_instead_of_failing(
    session: AsyncSession, stored: object
) -> None:
    """Worst case is showing the introduction again, never a 500 on page load."""
    workspace = await make_workspace(session)
    user = await make_member(session, workspace)
    membership = await service._membership(session, workspace.id, user.id)
    membership.settings_json = {"onboarding": stored}
    await session.flush()

    state = await service.get_state(session, workspace.id, user.id)
    assert state.status in {"pending", "completed"}
    assert state.updated_at is None


async def test_a_missing_membership_is_a_404(session: AsyncSession) -> None:
    workspace = await make_workspace(session)
    stranger = User(email="stranger@example.com", display_name="S", password_hash="x")
    session.add(stranger)
    await session.flush()

    with pytest.raises(HTTPException) as raised:
        await service.get_state(session, workspace.id, stranger.id)
    assert raised.value.status_code == 404
