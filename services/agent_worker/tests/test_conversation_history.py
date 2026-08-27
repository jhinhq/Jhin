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
from jhin_agent_worker.reasoning import _is_chat_turn, _load_history_parts
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
        # What the chat endpoints actually write, and what _is_chat_turn keys
        # off to decide whether the description is a brief or the person's
        # latest message.
        metadata_json={"origin": "conversation"},
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
        # The seed message is kept: it is the person's current question, and
        # sitting here -- after the earlier conversation, before this task's own
        # transcript -- is where the question has to be on every step of the
        # run. build_messages omits the "Task: ..." brief for a chat turn so it
        # is stated once, not twice.
        ("user", "text", "Second ask"),
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
        # No conversation means no prefix -- not that the task's own opening
        # turn disappears.
        assert await load(world, session, task) == [
            ("user", "text", "Solo"),
            ("agent", "text", "Reply"),
        ]


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


async def test_system_notices_never_reach_the_model(world: World) -> None:
    """Run-failure notices and notes are for humans; the model sees dialogue only."""
    async with world.session_factory() as session:
        earlier = make_task(world, seconds=0, description="First ask")
        current = make_task(world, seconds=100, description="Second ask")
        session.add_all([earlier, current])
        await session.flush()
        failure = make_message(
            world, earlier, seconds=2, text="Run failed: openai: HTTP 429", agent=True
        )
        failure.sender_type = SenderType.SYSTEM.value
        failure.sender_id = None
        failure.message_type = MessageType.ERROR.value
        session.add_all(
            [
                make_message(world, earlier, seconds=0, text="First ask"),
                make_message(world, earlier, seconds=1, text="On it", agent=True),
                failure,
                make_message(
                    world,
                    earlier,
                    seconds=3,
                    text="operator note",
                    agent=True,
                    message_type=MessageType.NOTE.value,
                ),
                make_message(world, current, seconds=100, text="Second ask"),
            ]
        )
        await session.commit()
        turns = await load(world, session, current)

    assert turns == [
        ("user", "text", "First ask"),
        ("agent", "text", "On it"),
        ("user", "text", "Second ask"),
    ]


async def test_a_mid_run_instruction_reads_as_plain_language(world: World) -> None:
    """The workflow drains a live instruction exactly once, so on every later
    step the history row is all that survives. Rendered as structured JSON it
    stranded the person's actual words behind a client turn id."""
    async with world.session_factory() as session:
        task = make_task(world, seconds=0, description="Draft the release note")
        session.add(task)
        await session.flush()
        steer = make_message(world, task, seconds=2, text="keep it under 200 words")
        steer.message_type = MessageType.INSTRUCTION.value
        steer.content_json = {"text": "keep it under 200 words", "client_turn_id": "ct-42"}
        session.add_all(
            [
                make_message(world, task, seconds=0, text="Draft the release note"),
                make_message(world, task, seconds=1, text="On it", agent=True),
                steer,
            ]
        )
        await session.commit()
        turns = await load(world, session, task)

    assert turns[-1] == ("user", "text", "Additional instruction: keep it under 200 words")
    # Worded exactly as build_messages words a freshly drained instruction, so
    # the two forms collapse into one message rather than two.
    assert not any("client_turn_id" in text for _, _, text in turns)
    assert not any(text.startswith("[instruction]") for _, _, text in turns)


async def test_a_work_request_inside_a_chat_keeps_its_brief(world: World) -> None:
    """A colleague's task inherits the requester's conversation_id, but its
    description is a composed framing brief that has to keep its leading
    position. This is why the shape is keyed off origin, not conversation_id."""
    async with world.session_factory() as session:
        task = make_task(world, seconds=0, description="Bisby asked you this. Answer it yourself.")
        task.metadata_json = {"origin": "work_request", "work_request": {"id": "wr-1"}}
        session.add(task)
        await session.flush()
        await session.commit()
        _, own = await _load_history_parts(session, task)

    assert task.conversation_id is not None
    assert _is_chat_turn(task, own) is False


async def test_a_chat_task_whose_seed_row_is_missing_keeps_its_brief(world: World) -> None:
    """The safety net: on anything unexpected the task falls back to the brief
    rather than reaching the model with no question in it at all."""
    async with world.session_factory() as session:
        task = make_task(world, seconds=0, description="Whats my name?")
        session.add(task)
        await session.flush()
        await session.commit()
        _, own = await _load_history_parts(session, task)

    assert own == ()
    assert _is_chat_turn(task, own) is False
