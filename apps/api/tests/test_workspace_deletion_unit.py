"""The counts behind the delete-workspace confirmation.

The dialog tells an owner what they are about to destroy, and they decide on
the strength of those numbers, so the numbers have to be real: counted from
the rows that actually cascade, and never borrowed from a neighbouring
workspace.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.workspaces import service
from jhin_db.models import (
    Agent,
    Conversation,
    Message,
    Task,
    User,
    Workspace,
    WorkspaceMembership,
)
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
    session.add(
        WorkspaceMembership(
            workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER.value
        )
    )
    await session.flush()
    return WorkspaceContext(user=user, workspace_id=workspace.id, role=WorkspaceRole.OWNER)


async def add_content(session: AsyncSession, ctx: WorkspaceContext, *, agents: int) -> None:
    for index in range(agents):
        session.add(
            Agent(
                workspace_id=ctx.workspace_id,
                name=f"Agent {index}",
                slug=f"agent-{index}",
            )
        )
    conversation = Conversation(
        workspace_id=ctx.workspace_id,
        title="Kickoff",
        last_activity_at=datetime.now(UTC),
    )
    session.add(conversation)
    session.add(
        Task(workspace_id=ctx.workspace_id, title="Do the thing", correlation_id=new_uuid7())
    )
    await session.flush()
    session.add(
        Message(
            workspace_id=ctx.workspace_id,
            conversation_id=conversation.id,
            sender_type="user",
            recipient_type="agent",
        )
    )
    await session.flush()


async def test_deletion_summary_counts_what_is_there(session: AsyncSession) -> None:
    ctx = await make_workspace(session, "counted")
    await add_content(session, ctx, agents=3)

    summary = await service.deletion_summary(session, ctx.workspace_id)

    assert summary["agents"] == 3
    assert summary["conversations"] == 1
    assert summary["messages"] == 1
    assert summary["tasks"] == 1
    assert summary["members"] == 1
    # Categories with nothing in them report zero rather than going missing —
    # the dialog decides what to show, not the API.
    assert summary["skills"] == 0
    assert summary["secrets"] == 0
    assert summary["api_keys"] == 0


async def test_deletion_summary_never_counts_another_workspace(session: AsyncSession) -> None:
    mine = await make_workspace(session, "mine")
    theirs = await make_workspace(session, "theirs")
    await add_content(session, mine, agents=2)
    await add_content(session, theirs, agents=7)

    assert (await service.deletion_summary(session, mine.workspace_id))["agents"] == 2
    assert (await service.deletion_summary(session, theirs.workspace_id))["agents"] == 7


async def test_deletion_summary_of_an_empty_workspace_is_all_zeros(
    session: AsyncSession,
) -> None:
    ctx = await make_workspace(session, "empty")
    summary = await service.deletion_summary(session, ctx.workspace_id)
    assert set(summary) == {
        "agents",
        "teams",
        "tasks",
        "conversations",
        "messages",
        "memories",
        "skills",
        "connections",
        "triggers",
        "api_keys",
        "secrets",
        "members",
    }
    assert all(value == 0 for key, value in summary.items() if key != "members")
