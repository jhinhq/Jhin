"""situation_context: who the agent is talking with, and what time it is.

Both blocks are derived live from workspace rows on every run — no agent
ever has to learn them — so these tests exercise the resolution paths a
real run takes: a human chat, a delegated child task, a work request, a
trigger-started task with no counterpart, and the timezone fallbacks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_agent_worker.situation import resolve_timezone, situation_context
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    Conversation,
    Message,
    Task,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import (
    MessageVisibility,
    RecipientType,
    SenderType,
    WorkspaceRole,
    new_uuid7,
)

NOW = datetime(2026, 8, 24, 4, 14, tzinfo=UTC)  # 21:14 the previous day in LA


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


async def make_workspace(session: AsyncSession, *, timezone: str = "UTC") -> Workspace:
    workspace = Workspace(name="Varand Test", slug=f"w-{new_uuid7().hex[:8]}")
    workspace.default_timezone = timezone
    session.add(workspace)
    await session.flush()
    return workspace


async def make_member(
    session: AsyncSession,
    workspace: Workspace,
    *,
    display_name: str,
    email: str,
    role: WorkspaceRole = WorkspaceRole.OWNER,
) -> User:
    user = User(email=email, display_name=display_name, password_hash="x")
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=role.value))
    await session.flush()
    return user


async def make_agent(session: AsyncSession, workspace: Workspace, name: str, role: str) -> Agent:
    agent = Agent(
        workspace_id=workspace.id,
        name=name,
        slug=name.lower(),
        role_title=role,
    )
    session.add(agent)
    await session.flush()
    return agent


async def make_chat_task(
    session: AsyncSession,
    workspace: Workspace,
    agent: Agent,
    *,
    created_by: UUID | None,
    speakers: tuple[UUID, ...] = (),
) -> Task:
    conversation = Conversation(
        workspace_id=workspace.id,
        title="Chat",
        primary_agent_id=agent.id,
        created_by_user_id=created_by,
        last_activity_at=NOW,
    )
    session.add(conversation)
    await session.flush()
    task = Task(
        workspace_id=workspace.id,
        title="Chat",
        description="hello",
        assigned_agent_id=agent.id,
        conversation_id=conversation.id,
        correlation_id=new_uuid7(),
        metadata_json={"origin": "message", "conversation_id": str(conversation.id)},
    )
    session.add(task)
    await session.flush()
    for speaker in speakers:
        session.add(
            Message(
                workspace_id=workspace.id,
                task_id=task.id,
                conversation_id=conversation.id,
                sender_type=SenderType.USER.value,
                sender_id=speaker,
                recipient_type=RecipientType.AGENT.value,
                recipient_id=agent.id,
                content_json={"text": "hello"},
                visibility=MessageVisibility.VISIBLE.value,
            )
        )
        await session.flush()
    return task


# --- who am I talking to ------------------------------------------------


async def test_human_chat_names_the_person_and_their_workspace_role(
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session)
    user = await make_member(
        session, workspace, display_name="Varand", email="zelsoft@example.test"
    )
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    task = await make_chat_task(session, workspace, agent, created_by=user.id, speakers=(user.id,))

    _time, who = await situation_context(session, workspace_id=workspace.id, task=task, now=NOW)
    assert who.startswith(
        "Who you are talking with: Varand (workspace owner), a person in this workspace."
    )
    # Never the email, and never the raw enum value.
    assert "zelsoft@example.test" not in who
    assert "@" not in who


async def test_role_wording_is_plain_english_for_every_role(session: AsyncSession) -> None:
    workspace = await make_workspace(session)
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    for role, words in (
        (WorkspaceRole.ADMIN, "workspace admin"),
        (WorkspaceRole.MEMBER, "workspace member"),
        (WorkspaceRole.VIEWER, "workspace viewer"),
    ):
        user = await make_member(
            session,
            workspace,
            display_name=f"P {role.value}",
            email=f"{role.value}@example.test",
            role=role,
        )
        task = await make_chat_task(
            session, workspace, agent, created_by=user.id, speakers=(user.id,)
        )
        _time, who = await situation_context(session, workspace_id=workspace.id, task=task, now=NOW)
        assert f"({words})" in who


async def test_the_person_who_spoke_last_is_the_one_being_answered(
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session)
    opener = await make_member(session, workspace, display_name="Varand", email="a@example.test")
    replier = await make_member(
        session,
        workspace,
        display_name="Dana",
        email="b@example.test",
        role=WorkspaceRole.MEMBER,
    )
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    task = await make_chat_task(
        session, workspace, agent, created_by=opener.id, speakers=(opener.id, replier.id)
    )

    _time, who = await situation_context(session, workspace_id=workspace.id, task=task, now=NOW)
    assert "Dana (workspace member)" in who
    # Only the current counterpart — other members are not enumerated here.
    assert "Varand" not in who


async def test_first_turn_falls_back_to_the_conversation_creator(
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session)
    user = await make_member(session, workspace, display_name="Varand", email="a@example.test")
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    # No message rows yet: the seed message may not be visible to the first
    # composing step.
    task = await make_chat_task(session, workspace, agent, created_by=user.id)

    _time, who = await situation_context(session, workspace_id=workspace.id, task=task, now=NOW)
    assert "Varand (workspace owner)" in who


async def test_delegated_child_task_talks_to_the_requesting_agent(
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session)
    user = await make_member(session, workspace, display_name="Varand", email="a@example.test")
    manager = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    worker = await make_agent(session, workspace, "Connie", "QA Engineer")
    parent = await make_chat_task(
        session, workspace, manager, created_by=user.id, speakers=(user.id,)
    )
    child = Task(
        workspace_id=workspace.id,
        title="Check the release",
        description="please verify",
        assigned_agent_id=worker.id,
        parent_task_id=parent.id,
        conversation_id=parent.conversation_id,
        correlation_id=parent.correlation_id,
        metadata_json={
            "origin": "delegation",
            "delegation": {
                "kind": "delegation",
                "delegated_by_agent_id": str(manager.id),
                "delegated_by_agent_name": manager.name,
            },
        },
    )
    session.add(child)
    await session.flush()

    _time, who = await situation_context(session, workspace_id=workspace.id, task=child, now=NOW)
    assert "Bisby (Chief of Staff), an AI teammate in this workspace" in who
    assert "who delegated this task to you" in who
    # The human who started the parent thread is not this agent's counterpart.
    assert "Varand" not in who


async def test_work_request_child_task_talks_to_the_requesting_agent(
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session)
    requester = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    target = await make_agent(session, workspace, "Connie", "QA Engineer")
    task = Task(
        workspace_id=workspace.id,
        title="Look into flakes",
        description="please",
        assigned_agent_id=target.id,
        correlation_id=new_uuid7(),
        metadata_json={
            "origin": "work_request",
            "work_request": {
                "requester_agent_id": str(requester.id),
                "requester_agent_name": requester.name,
            },
        },
    )
    session.add(task)
    await session.flush()

    _time, who = await situation_context(session, workspace_id=workspace.id, task=task, now=NOW)
    assert "Bisby (Chief of Staff), an AI teammate in this workspace" in who
    assert "who asked you for this work" in who


async def test_trigger_started_task_has_no_interlocutor_block(
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session)
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    task = Task(
        workspace_id=workspace.id,
        title="Nightly digest",
        description="run the digest",
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
        metadata_json={"origin": "trigger"},
    )
    session.add(task)
    await session.flush()

    time_context, who = await situation_context(
        session, workspace_id=workspace.id, task=task, now=NOW
    )
    assert who == ""
    # The clock is unconditional even when nobody is on the other side.
    assert time_context.startswith("Current time: ")


async def test_a_speaker_who_is_not_a_member_is_not_named(session: AsyncSession) -> None:
    workspace = await make_workspace(session)
    outsider = User(email="out@example.test", display_name="Outsider", password_hash="x")
    session.add(outsider)
    await session.flush()
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    task = await make_chat_task(
        session, workspace, agent, created_by=outsider.id, speakers=(outsider.id,)
    )

    _time, who = await situation_context(session, workspace_id=workspace.id, task=task, now=NOW)
    assert who == ""


# --- what time is it ----------------------------------------------------


async def test_non_utc_workspace_timezone_is_applied_and_named(
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session, timezone="America/Los_Angeles")
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    task = Task(
        workspace_id=workspace.id,
        title="t",
        description="d",
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()

    time_context, _who = await situation_context(
        session, workspace_id=workspace.id, task=task, now=NOW
    )
    assert time_context.startswith(
        "Current time: Sunday, 23 August 2026, 21:14 (America/Los_Angeles)."
    )


@pytest.mark.parametrize("configured", ["", "   ", "Not/A_Zone"])
async def test_missing_or_invalid_timezone_falls_back_to_utc(
    session: AsyncSession, configured: str
) -> None:
    workspace = await make_workspace(session, timezone=configured)
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    task = Task(
        workspace_id=workspace.id,
        title="t",
        description="d",
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()

    time_context, _who = await situation_context(
        session, workspace_id=workspace.id, task=task, now=NOW
    )
    assert time_context.startswith("Current time: Monday, 24 August 2026, 04:14 (UTC).")


def test_timezone_resolution_never_raises() -> None:
    assert str(resolve_timezone(None).key) == "UTC"
    assert str(resolve_timezone("Nope/Nope").key) == "UTC"
    assert str(resolve_timezone(" Europe/Paris ").key) == "Europe/Paris"


async def test_resolution_is_stable_for_a_fixed_clock(session: AsyncSession) -> None:
    """A recomposed step with the same inputs yields the same blocks, so a
    retried activity cannot silently change the recorded prompt shape."""
    workspace = await make_workspace(session, timezone="Europe/Paris")
    user = await make_member(session, workspace, display_name="Varand", email="a@example.test")
    agent = await make_agent(session, workspace, "Bisby", "Chief of Staff")
    task = await make_chat_task(session, workspace, agent, created_by=user.id, speakers=(user.id,))
    first = await situation_context(session, workspace_id=workspace.id, task=task, now=NOW)
    second = await situation_context(session, workspace_id=workspace.id, task=task, now=NOW)
    assert first == second
