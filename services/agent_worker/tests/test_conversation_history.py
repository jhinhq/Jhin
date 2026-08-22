"""Conversation-aware prompt history: earlier tasks in the same conversation
precede the current task's transcript, visible text only, capped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_agent_worker.activities import (
    CONVERSATION_HISTORY_MAX_CHARS,
    CONVERSATION_HISTORY_MAX_MESSAGES,
    CONVERSATION_HISTORY_OMITTED_MARKER,
    AgentActivities,
)
from jhin_db.base import Base
from jhin_db.models import Agent, Conversation, Message, Task, Workspace
from jhin_domain import (
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    new_uuid7,
)
from jhin_observability import noop_metrics, noop_tracer

T0 = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


class StubResources:
    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory
        self.publisher = None
        self.crypto = None
        self.runtime = SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())


class World:
    workspace: Workspace
    agent: Agent
    conversation: Conversation
    activities: AgentActivities
    session_factory: Any


@pytest.fixture
async def world() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    fixture = World()
    fixture.session_factory = maker
    fixture.activities = AgentActivities(StubResources(maker))  # type: ignore[arg-type]
    async with maker() as session:
        fixture.workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(fixture.workspace)
        await session.flush()
        fixture.agent = Agent(workspace_id=fixture.workspace.id, name="Atlas", slug="atlas")
        session.add(fixture.agent)
        await session.flush()
        fixture.conversation = Conversation(
            workspace_id=fixture.workspace.id,
            title="Roadmap",
            primary_agent_id=fixture.agent.id,
            last_activity_at=T0,
        )
        session.add(fixture.conversation)
        await session.commit()
    yield fixture
    await engine.dispose()


def make_task(
    world: World, *, seconds: int, description: str, in_conversation: bool = True
) -> Task:
    return Task(
        workspace_id=world.workspace.id,
        title="Roadmap",
        description=description,
        state=TaskState.COMPLETED.value,
        assigned_agent_id=world.agent.id,
        conversation_id=world.conversation.id if in_conversation else None,
        correlation_id=new_uuid7(),
        created_at=T0 + timedelta(seconds=seconds),
        updated_at=T0 + timedelta(seconds=seconds),
    )


def make_message(
    world: World,
    task: Task,
    *,
    seconds: int,
    text: str,
    agent: bool = False,
    message_type: str = MessageType.TEXT.value,
    visibility: str = MessageVisibility.VISIBLE.value,
) -> Message:
    return Message(
        workspace_id=world.workspace.id,
        task_id=task.id,
        conversation_id=task.conversation_id,
        sender_type=SenderType.AGENT.value if agent else SenderType.USER.value,
        sender_id=world.agent.id if agent else None,
        recipient_type=RecipientType.USER.value if agent else RecipientType.AGENT.value,
        message_type=message_type,
        content_json={"text": text},
        visibility=visibility,
        created_at=T0 + timedelta(seconds=seconds),
    )


async def load(world: World, session: AsyncSession, task: Task) -> list[tuple[str, str, str]]:
    history = await world.activities._load_history(session, task)
    return [(turn.role, turn.kind, turn.text) for turn in history]


async def test_earlier_tasks_precede_current_history_without_internal_rows(world: World) -> None:
    async with world.session_factory() as session:
        earlier = make_task(world, seconds=0, description="First ask")
        unrelated = make_task(world, seconds=1, description="Other thread", in_conversation=False)
        current = make_task(world, seconds=100, description="Second ask")
        session.add_all([earlier, unrelated, current])
        await session.flush()
        session.add_all(
            [
                make_message(world, earlier, seconds=0, text="First ask"),
                make_message(
                    world,
                    earlier,
                    seconds=1,
                    text="tool transcript",
                    agent=True,
                    message_type=MessageType.TOOL_CALL.value,
                    visibility=MessageVisibility.INTERNAL.value,
                ),
                make_message(
                    world,
                    earlier,
                    seconds=2,
                    text="visible but tool",
                    agent=True,
                    message_type=MessageType.TOOL_RESULT.value,
                ),
                make_message(
                    world,
                    earlier,
                    seconds=3,
                    text="hidden note",
                    agent=True,
                    visibility=MessageVisibility.INTERNAL.value,
                ),
                make_message(world, earlier, seconds=4, text="Here is the plan", agent=True),
                make_message(world, unrelated, seconds=5, text="Not this one"),
                make_message(world, current, seconds=100, text="Second ask"),
                make_message(
                    world,
                    current,
                    seconds=101,
                    text="",
                    agent=True,
                    message_type=MessageType.TOOL_CALL.value,
                    visibility=MessageVisibility.INTERNAL.value,
                ),
                make_message(world, current, seconds=102, text="Working on it", agent=True),
            ]
        )
        await session.commit()

        turns = await load(world, session, current)

    assert turns == [
        ("user", "text", "First ask"),
        ("agent", "text", "Here is the plan"),
        # The current task's seed message duplicates task.description and is
        # still deduplicated; its tool transcript still enters as before.
        ("agent", "tool_call", ""),
        ("agent", "text", "Working on it"),
    ]


async def test_no_conversation_means_unchanged_history(world: World) -> None:
    async with world.session_factory() as session:
        task = make_task(world, seconds=0, description="Solo", in_conversation=False)
        session.add(task)
        await session.flush()
        session.add_all(
            [
                make_message(world, task, seconds=0, text="Solo"),
                make_message(world, task, seconds=1, text="Reply", agent=True),
            ]
        )
        await session.commit()
        assert await load(world, session, task) == [("agent", "text", "Reply")]


async def test_message_cap_keeps_most_recent_and_marks_omission(world: World) -> None:
    async with world.session_factory() as session:
        earlier = make_task(world, seconds=0, description="seed")
        current = make_task(world, seconds=10_000, description="now")
        session.add_all([earlier, current])
        await session.flush()
        total = CONVERSATION_HISTORY_MAX_MESSAGES + 5
        session.add_all(
            make_message(world, earlier, seconds=i + 1, text=f"m{i}", agent=i % 2 == 1)
            for i in range(total)
        )
        await session.commit()
        turns = await load(world, session, current)

    assert turns[0] == ("user", "text", CONVERSATION_HISTORY_OMITTED_MARKER)
    kept = turns[1:]
    assert len(kept) == CONVERSATION_HISTORY_MAX_MESSAGES
    assert kept[0][2] == "m5" and kept[-1][2] == f"m{total - 1}"


async def test_char_cap_drops_oldest_first(world: World) -> None:
    async with world.session_factory() as session:
        earlier = make_task(world, seconds=0, description="seed")
        current = make_task(world, seconds=10_000, description="now")
        session.add_all([earlier, current])
        await session.flush()
        big = "x" * 5_000
        session.add_all(
            make_message(world, earlier, seconds=i + 1, text=f"{i}:{big}", agent=i % 2 == 1)
            for i in range(6)  # ~30k chars > CONVERSATION_HISTORY_MAX_CHARS
        )
        await session.commit()
        turns = await load(world, session, current)

    assert turns[0][2] == CONVERSATION_HISTORY_OMITTED_MARKER
    kept = [t[2] for t in turns[1:]]
    assert sum(len(t) for t in kept) <= CONVERSATION_HISTORY_MAX_CHARS
    assert kept[0].startswith("2:") and kept[-1].startswith("5:")


async def test_marker_absent_when_nothing_dropped(world: World) -> None:
    async with world.session_factory() as session:
        earlier = make_task(world, seconds=0, description="seed")
        current = make_task(world, seconds=10, description="now")
        session.add_all([earlier, current])
        await session.flush()
        session.add(make_message(world, earlier, seconds=1, text="short"))
        await session.commit()
        turns = await load(world, session, current)
    assert turns == [("user", "text", "short")]
