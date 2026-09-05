"""The exact message sequence a chat turn hands to the provider.

This is the layer that was missing. ``_load_history`` and ``build_messages``
were each tested alone, so nothing noticed that together they put the current
question *before* everything said earlier and then deleted the copy that sat in
the right place. The newest user message reaching the model was the previous
turn's question, and agents answered that instead -- turn 3 of a chat replying
verbatim with turn 2's answer.

These tests run the real activity against real database rows, written exactly
as the chat endpoint writes them, and assert on what the model client actually
received.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import jhin_agent_worker.reasoning as reasoning_module
from jhin_agent_worker.reasoning import AgentReasoningActivities
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentRun,
    Conversation,
    Message,
    RunEvent,
    Task,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import (
    MessageType,
    MessageVisibility,
    RecipientType,
    RunStatus,
    SenderType,
    TaskState,
    WorkspaceRole,
    new_uuid7,
)
from jhin_models import ModelRequest, ModelResponse, ModelUsage
from jhin_observability import noop_metrics, noop_tracer
from jhin_workflows.agent_task.shared import AdvertisedTool, ReasonAgentStepInput

T0 = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)


class _Model:
    def __init__(self) -> None:
        self.responses: list[ModelResponse] = []
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


class _Publisher:
    async def publish(self, _envelope: Any) -> None:
        return None


class _Resources:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.runtime = SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())
        self.session_factory = sessions
        self.publisher = _Publisher()
        self.crypto = None


class ChatWorld:
    sessions: async_sessionmaker[AsyncSession]
    reasoning: AgentReasoningActivities
    model: _Model
    workspace: Workspace
    agent: Agent
    conversation: Conversation

    def snapshot(self) -> AgentExecutionSnapshot:
        return AgentExecutionSnapshot(
            agent_id=self.agent.id,
            workspace_id=self.workspace.id,
            name=self.agent.name,
            role_title="Senior Software Engineer",
            system_prompt="",
            autonomy_level="balanced",
            team_id=None,
            team_name=None,
            manager_agent_id=None,
            manager_name=None,
            model_profile=ModelProfileSnapshot(
                profile_id=new_uuid7(),
                provider_id=new_uuid7(),
                provider_type="prompt-test",
                base_url=None,
                secret_id=None,
                model_name="prompt-test",
                display_name="Prompt test",
                input_cost_micros_per_million=1_000_000,
                output_cost_micros_per_million=1_000_000,
            ),
            temperature=None,
            max_output_tokens=None,
            run_limits=RunLimits(max_steps=5, max_run_minutes=5),
        )

    async def run_step(
        self, task: Task, *, step_index: int = 0, tools: tuple[str, ...] = ()
    ) -> list[tuple[str, str]]:
        """Run one reasoning step and return the (role, content) sequence the
        model client was handed."""
        async with self.sessions() as session:
            run = AgentRun(
                workspace_id=self.workspace.id,
                agent_id=self.agent.id,
                task_id=task.id,
                status=RunStatus.RUNNING.value,
            )
            session.add(run)
            await session.commit()
        before = len(self.model.requests)
        await self.reasoning.reason_agent_step_activity(
            ReasonAgentStepInput(
                workspace_id=str(self.workspace.id),
                task_id=str(task.id),
                run_id=str(run.id),
                agent_id=str(self.agent.id),
                snapshot_json=self.snapshot().model_dump_json(),
                step_index=step_index,
                advertised_tools=[
                    AdvertisedTool(name=name, description=name, parameters={"type": "object"})
                    for name in tools
                ],
            )
        )
        request = self.model.requests[before]
        return [(message.role, message.content) for message in request.messages]


def _reply(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        finish_reason="stop",
        model="prompt-test",
        usage=ModelUsage(input_tokens=10, output_tokens=4, cached_tokens=0),
        latency_ms=3,
        provider_request_id=f"req-{text[:8]}",
        tool_calls=(),
    )


@pytest.fixture
async def chat(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ChatWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    world = ChatWorld()
    world.sessions = sessions
    world.model = _Model()
    monkeypatch.setattr(reasoning_module, "build_model_client", lambda *_a, **_k: world.model)
    world.reasoning = AgentReasoningActivities(_Resources(sessions))  # type: ignore[arg-type]

    async with sessions() as session:
        world.workspace = Workspace(name="Chat", slug=f"chat-{new_uuid7().hex[:8]}")
        session.add(world.workspace)
        await session.flush()
        user = User(
            email=f"{new_uuid7().hex[:8]}@example.test",
            display_name="Varand",
            password_hash="x",
        )
        session.add(user)
        await session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=world.workspace.id,
                user_id=user.id,
                role=WorkspaceRole.OWNER.value,
            )
        )
        world.agent = Agent(workspace_id=world.workspace.id, name="Bisby", slug="bisby")
        session.add(world.agent)
        await session.flush()
        world.conversation = Conversation(
            workspace_id=world.workspace.id,
            title="Hey what is your name?",
            primary_agent_id=world.agent.id,
            created_by_user_id=user.id,
            last_activity_at=T0,
        )
        session.add(world.conversation)
        await session.commit()
        world.user_id = user.id  # type: ignore[attr-defined]
    yield world
    await engine.dispose()


async def _turn(
    world: ChatWorld,
    session: AsyncSession,
    *,
    seconds: int,
    text: str,
    reply: str | None,
) -> Task:
    """One chat turn, written exactly as conversations.service._run_turn writes
    it: a task whose description is the message, plus a seed user message
    carrying the same text."""
    task = Task(
        workspace_id=world.workspace.id,
        title=text[:120],
        description=text,
        state=TaskState.COMPLETED.value if reply else TaskState.RUNNING.value,
        assigned_agent_id=world.agent.id,
        conversation_id=world.conversation.id,
        correlation_id=new_uuid7(),
        metadata_json={"origin": "conversation", "conversation_id": str(world.conversation.id)},
        created_at=T0 + timedelta(seconds=seconds),
        updated_at=T0 + timedelta(seconds=seconds),
    )
    session.add(task)
    await session.flush()
    session.add(
        Message(
            workspace_id=world.workspace.id,
            task_id=task.id,
            conversation_id=world.conversation.id,
            sender_type=SenderType.USER.value,
            sender_id=world.user_id,  # type: ignore[attr-defined]
            recipient_type=RecipientType.AGENT.value,
            recipient_id=world.agent.id,
            message_type=MessageType.TEXT.value,
            content_json={"text": text},
            visibility=MessageVisibility.VISIBLE.value,
            created_at=T0 + timedelta(seconds=seconds),
        )
    )
    if reply:
        session.add(
            Message(
                workspace_id=world.workspace.id,
                task_id=task.id,
                conversation_id=world.conversation.id,
                sender_type=SenderType.AGENT.value,
                sender_id=world.agent.id,
                recipient_type=RecipientType.USER.value,
                message_type=MessageType.TEXT.value,
                content_json={"text": reply},
                visibility=MessageVisibility.VISIBLE.value,
                created_at=T0 + timedelta(seconds=seconds + 1),
            )
        )
    return task


async def test_three_turn_chat_hands_the_newest_question_last(chat: ChatWorld) -> None:
    """The reported failure, end to end. Three turns, each its own task (which
    is what the API does once the previous run has finished), and the third
    prompt must be asking the third question."""
    async with chat.sessions() as session:
        await _turn(
            chat,
            session,
            seconds=0,
            text="Hey what is your name?",
            reply="My name is Bisby.",
        )
        await _turn(
            chat,
            session,
            seconds=100,
            text="Whos in your team?",
            reply="I'm on the Engineering team.",
        )
        current = await _turn(chat, session, seconds=200, text="Whats my name?", reply=None)
        await session.commit()

    chat.model.responses.append(_reply("You're Varand."))
    sequence = await chat.run_step(current)

    assert [role for role, _ in sequence] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert sequence[1:] == [
        ("user", "Hey what is your name?"),
        ("assistant", "My name is Bisby."),
        ("user", "Whos in your team?"),
        ("assistant", "I'm on the Engineering team."),
        ("user", "Whats my name?"),
    ]
    # Stated once, and never dressed up as a work brief.
    assert sum("Whats my name?" in content for _, content in sequence) == 1
    assert not any(content.startswith("Task: ") for _, content in sequence)


async def test_the_agent_is_told_who_it_is_speaking_to_and_when(chat: ChatWorld) -> None:
    """ "Whats my name?" is answerable only if the person's name is in the
    prompt. It reaches the system message, and survives on a chat turn."""
    async with chat.sessions() as session:
        current = await _turn(chat, session, seconds=0, text="Whats my name?", reply=None)
        await session.commit()

    chat.model.responses.append(_reply("You're Varand."))
    sequence = await chat.run_step(current)

    system = sequence[0][1]
    assert "Varand" in system
    assert "Current time:" in system


async def test_an_assigned_work_task_keeps_its_brief_first(chat: ChatWorld) -> None:
    """Work is framed by its brief, which has to lead. Only a chat turn's
    description is "the latest thing somebody said"."""
    async with chat.sessions() as session:
        task = Task(
            workspace_id=chat.workspace.id,
            title="Audit the retry logic",
            description="Check every backoff path.",
            state=TaskState.RUNNING.value,
            assigned_agent_id=chat.agent.id,
            correlation_id=new_uuid7(),
        )
        session.add(task)
        await session.commit()

    chat.model.responses.append(_reply("Starting the audit."))
    sequence = await chat.run_step(task)

    assert sequence[1][0] == "user"
    assert sequence[1][1] == "Task: Audit the retry logic\n\nCheck every backoff path."


async def _previous_run_offered(
    world: ChatWorld, session: AsyncSession, task: Task, tools: list[str]
) -> None:
    """A finished run of ``task`` that recorded what it was offered."""
    run = AgentRun(
        workspace_id=world.workspace.id,
        agent_id=world.agent.id,
        task_id=task.id,
        status=RunStatus.COMPLETED.value,
    )
    session.add(run)
    await session.flush()
    session.add(
        RunEvent(
            workspace_id=world.workspace.id,
            task_id=task.id,
            run_id=run.id,
            seq=3,
            event_type="agent.step.tools_offered",
            payload_json={"step": 0, "count": len(tools), "tools": tools, "truncated": False},
        )
    )


NOTICE = "Your tools changed since your last reply in this conversation."


async def test_tools_changed_notice_names_what_was_added_and_removed(chat: ChatWorld) -> None:
    """The reported failure: asked "can you try now?" eight seconds after the
    grant, the engineer answered from what it had said a turn earlier. The
    previous run's durable offer is what the notice is built from."""
    async with chat.sessions() as session:
        previous = await _turn(
            chat,
            session,
            seconds=0,
            text="Can you use the github tool?",
            reply="I do not have a GitHub tool.",
        )
        await _previous_run_offered(chat, session, previous, ["memory.recall", "linear.issue.read"])
        current = await _turn(chat, session, seconds=100, text="Can you try now?", reply=None)
        await session.commit()

    chat.model.responses.append(_reply("Trying now."))
    sequence = await chat.run_step(
        current, tools=("memory.recall", "github.repository.read", "github.branch.list")
    )

    system = sequence[0][1]
    assert (
        f"{NOTICE} Added: github.branch.list, github.repository.read. Removed: "
        "linear.issue.read. Do not rely on anything you said about your tools before this turn."
    ) in system
    # It sits with the situation blocks, ahead of the tool guidance.
    assert system.index("Current time:") < system.index(NOTICE)
    assert system.index(NOTICE) < system.index("You may call the provided tools")


async def test_no_notice_on_a_first_turn_or_when_the_set_is_unchanged(chat: ChatWorld) -> None:
    async with chat.sessions() as session:
        first = await _turn(chat, session, seconds=0, text="Hello?", reply=None)
        await session.commit()
    chat.model.responses.append(_reply("Hi."))
    assert NOTICE not in (await chat.run_step(first, tools=("memory.recall",)))[0][1]

    async with chat.sessions() as session:
        previous = await _turn(chat, session, seconds=100, text="Still there?", reply="Yes.")
        await _previous_run_offered(chat, session, previous, ["memory.recall"])
        current = await _turn(chat, session, seconds=200, text="Same tools?", reply=None)
        await session.commit()
    chat.model.responses.append(_reply("Same."))
    assert NOTICE not in (await chat.run_step(current, tools=("memory.recall",)))[0][1]


async def test_no_notice_on_assigned_work(chat: ChatWorld) -> None:
    """Work has no earlier reply of its own to contradict, so nothing is said
    even when a chat run of this agent recorded another set."""
    async with chat.sessions() as session:
        previous = await _turn(chat, session, seconds=0, text="Earlier chat", reply="Done.")
        await _previous_run_offered(chat, session, previous, ["linear.issue.read"])
        task = Task(
            workspace_id=chat.workspace.id,
            title="Audit the retry logic",
            description="Check every backoff path.",
            assigned_agent_id=chat.agent.id,
            conversation_id=chat.conversation.id,
            correlation_id=new_uuid7(),
            metadata_json={"origin": "delegation"},
        )
        session.add(task)
        await session.commit()

    chat.model.responses.append(_reply("Audited."))
    system = (await chat.run_step(task, tools=("memory.recall",)))[0][1]

    assert NOTICE not in system
