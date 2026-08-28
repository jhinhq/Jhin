"""deliver_question_answer: the one observation a parked ask ever gets.

The step projection wrote no ``tool_result`` for the ask, so if this activity
writes none either the model is left holding a tool call with no result — and
if it writes two, the provider rejects the request outright. Both edges are
pinned here, along with the rule that Postgres, not the workflow's routing
advice, decides whether anybody actually answered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from jhin_agent_worker.activities import AgentActivities
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentRun,
    Conversation,
    Message,
    RunEvent,
    Task,
    User,
    UserQuestion,
    Workspace,
)
from jhin_domain import RunStatus, TaskState, UserQuestionStatus, new_uuid7
from jhin_observability import noop_metrics, noop_tracer
from jhin_workflows.agent_task import DeliverQuestionAnswerInput


class StubPublisher:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.events.append(envelope)


class StubResources:
    def __init__(self, session_factory: Any) -> None:
        self.runtime = SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())
        self.session_factory = session_factory
        self.publisher = StubPublisher()
        self.crypto = None


class World:
    workspace: Workspace
    agent: Agent
    user: User
    task: Task
    run: AgentRun
    question: UserQuestion
    card: Message
    activities: AgentActivities
    publisher: StubPublisher
    session_factory: Any
    tool_call_id: str


@pytest.fixture
async def world() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    f = World()
    f.session_factory = maker
    resources = StubResources(maker)
    f.publisher = resources.publisher
    f.activities = AgentActivities(resources)  # type: ignore[arg-type]
    f.tool_call_id = str(new_uuid7())

    async with maker() as session:
        f.workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        f.user = User(
            email=f"v-{new_uuid7().hex[:8]}@example.com",
            display_name="Varand",
            password_hash="x",
        )
        session.add_all([f.workspace, f.user])
        await session.flush()
        ws = f.workspace.id
        f.agent = Agent(workspace_id=ws, name="Ada", slug="ada")
        session.add(f.agent)
        await session.flush()
        conversation = Conversation(
            workspace_id=ws,
            title="Deploys",
            primary_agent_id=f.agent.id,
            created_by_user_id=f.user.id,
            last_activity_at=datetime.now(UTC),
        )
        session.add(conversation)
        await session.flush()
        f.task = Task(
            workspace_id=ws,
            title="Chat turn",
            state=TaskState.RUNNING.value,
            assigned_agent_id=f.agent.id,
            conversation_id=conversation.id,
            correlation_id=new_uuid7(),
            metadata_json={"origin": "conversation"},
        )
        session.add(f.task)
        await session.flush()
        f.run = AgentRun(
            workspace_id=ws,
            agent_id=f.agent.id,
            task_id=f.task.id,
            status=RunStatus.WAITING_PERSON.value,
        )
        session.add(f.run)
        await session.flush()
        f.card = Message(
            workspace_id=ws,
            task_id=f.task.id,
            run_id=f.run.id,
            conversation_id=conversation.id,
            sender_type="agent",
            sender_id=f.agent.id,
            recipient_type="user",
            recipient_id=f.user.id,
            message_type="question",
            content_json={"kind": "user_question", "status": "pending"},
        )
        session.add(f.card)
        await session.flush()
        f.question = UserQuestion(
            workspace_id=ws,
            conversation_id=conversation.id,
            task_id=f.task.id,
            run_id=f.run.id,
            agent_id=f.agent.id,
            message_id=f.card.id,
            kind="memory_scope",
            question="Only Engineering, or company wide?",
            options_json=[{"value": "team", "label": "Only the Engineering team"}],
            dedupe_hash="d",
            idempotency_key="k",
            asked_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        session.add(f.question)
        await session.commit()

    yield f
    await engine.dispose()


def deliver_input(world: World, **overrides: Any) -> DeliverQuestionAnswerInput:
    values: dict[str, Any] = {
        "workspace_id": str(world.workspace.id),
        "task_id": str(world.task.id),
        "run_id": str(world.run.id),
        "agent_id": str(world.agent.id),
        "question_id": str(world.question.id),
        "provider_call_id": "toolu_abc",
        "gateway_tool_call_id": world.tool_call_id,
        "outcome": "answered",
    }
    values.update(overrides)
    return DeliverQuestionAnswerInput(**values)


async def answer(
    world: World,
    *,
    kind: str = "option",
    option_value: str = "team",
    text: str = "Only the Engineering team",
    granted_scope: str = "team",
    denied_reason: str = "",
    by_user: bool = True,
) -> None:
    async with world.session_factory() as session:
        row = await session.get(UserQuestion, world.question.id)
        assert row is not None
        row.status = UserQuestionStatus.ANSWERED.value
        row.answer_kind = kind
        row.answer_option_value = option_value
        row.answer_text = text
        row.granted_scope = granted_scope
        row.granted_authority = "workspace" if granted_scope else ""
        row.grant_denied_reason = denied_reason
        row.answered_at = datetime.now(UTC)
        row.answered_by_user_id = world.user.id if by_user else None
        await session.commit()


async def observations(world: World) -> list[dict[str, Any]]:
    async with world.session_factory() as session:
        rows = list(
            await session.scalars(
                select(Message).where(
                    Message.run_id == world.run.id, Message.message_type == "tool_result"
                )
            )
        )
    return [row.content_json for row in rows]


async def deliver(world: World, **overrides: Any) -> None:
    await ActivityEnvironment().run(
        world.activities.deliver_question_answer_activity, deliver_input(world, **overrides)
    )


async def test_the_answer_reaches_the_model_as_the_asks_own_result(world: World) -> None:
    await answer(world)
    await deliver(world)

    written = await observations(world)
    assert len(written) == 1
    assert written[0]["tool_call_id"] == world.tool_call_id
    assert written[0]["tool_name"] == "organization.ask_person"
    result = json.loads(written[0]["result"])
    assert result["status"] == "answered"
    assert result["answer_kind"] == "option"
    assert result["option_value"] == "team"
    assert result["answer"] == "Only the Engineering team"
    assert result["answered_by"] == "Varand"
    grant = result["memory_scope_grant"]
    assert grant["granted_scope"] == "team"
    assert grant["authorized_by_question_id"] == str(world.question.id)
    # The model is told the id to cite, and told to propose exactly once.
    assert "authorized_by_question_id" in grant["detail"]

    async with world.session_factory() as session:
        run = await session.get(AgentRun, world.run.id)
        assert run is not None and run.status == RunStatus.RUNNING.value
    assert [event.data["question_status"] for event in world.publisher.events] == ["answered"]


async def test_delivering_twice_writes_one_observation(world: World) -> None:
    """Two ``tool_result`` rows for one call become two ``tool_use_id``-matched
    blocks and the provider rejects the whole request."""
    await answer(world)
    await deliver(world)
    await deliver(world)

    assert len(await observations(world)) == 1
    async with world.session_factory() as session:
        events = list(
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == world.run.id,
                    RunEvent.event_type == "question.answered",
                )
            )
        )
    assert len(events) == 1


async def test_free_text_answers_authorise_no_wider_memory(world: World) -> None:
    await answer(
        world,
        kind="other",
        option_value="",
        text="Only the platform pod, not all of Engineering",
        granted_scope="",
        denied_reason="free_text_answer",
    )
    await deliver(world)

    result = json.loads((await observations(world))[0]["result"])
    assert result["answer_kind"] == "other"
    assert result["option_value"] == ""
    grant = result["memory_scope_grant"]
    assert grant["granted_scope"] == ""
    assert grant["denied_reason"] == "free_text_answer"
    assert "'agent' scope" in grant["detail"]


async def test_an_answer_the_person_could_not_authorise_says_who_can(world: World) -> None:
    await answer(
        world,
        option_value="workspace",
        text="Company wide",
        granted_scope="",
        denied_reason="insufficient_authority",
    )
    await deliver(world)

    grant = json.loads((await observations(world))[0]["result"])["memory_scope_grant"]
    assert grant["granted_scope"] == ""
    assert grant["denied_reason"] == "insufficient_authority"
    assert "admin" in grant["detail"]


async def test_an_open_question_carries_no_memory_grant(world: World) -> None:
    """Attaching one would invite the model to remember something nobody
    discussed."""
    async with world.session_factory() as session:
        row = await session.get(UserQuestion, world.question.id)
        assert row is not None
        row.kind = "open"
        await session.commit()
    await answer(world, option_value="staging", text="Deploy to staging", granted_scope="")
    await deliver(world)

    result = json.loads((await observations(world))[0]["result"])
    assert "memory_scope_grant" not in result


async def test_nobody_answering_is_said_plainly(world: World) -> None:
    await deliver(world, outcome="timed_out")

    result = json.loads((await observations(world))[0]["result"])
    assert result["status"] == "timed_out"
    assert "did not hear back" in result["detail"]
    async with world.session_factory() as session:
        row = await session.get(UserQuestion, world.question.id)
        assert row is not None and row.status == UserQuestionStatus.EXPIRED.value
        card = await session.get(Message, world.card.id)
        assert card is not None
        assert card.content_json["status"] == UserQuestionStatus.EXPIRED.value
        run = await session.get(AgentRun, world.run.id)
        assert run is not None and run.status == RunStatus.RUNNING.value


async def test_an_answer_that_beat_the_timer_is_still_an_answer(world: World) -> None:
    """The row is the authority, not the workflow's routing advice: somebody
    who answered while the timer was firing must not be told they were late."""
    await answer(world)
    await deliver(world, outcome="timed_out")

    result = json.loads((await observations(world))[0]["result"])
    assert result["status"] == "answered"
    assert result["answer"] == "Only the Engineering team"
    async with world.session_factory() as session:
        row = await session.get(UserQuestion, world.question.id)
        assert row is not None and row.status == UserQuestionStatus.ANSWERED.value


async def test_run_events_never_carry_the_words(world: World) -> None:
    await answer(world)
    await deliver(world)

    async with world.session_factory() as session:
        event = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == world.run.id, RunEvent.event_type == "question.answered"
            )
        )
    assert event is not None
    assert event.payload_json["granted_scope"] == "team"
    serialized = json.dumps(event.payload_json)
    assert "Engineering" not in serialized
    assert "company wide" not in serialized.lower()


@pytest.mark.parametrize(
    ("overrides", "failure_type"),
    [
        ({"run_id": str(new_uuid7())}, "question_run_binding_mismatch"),
        ({"agent_id": str(new_uuid7())}, "question_run_binding_mismatch"),
        ({"question_id": str(new_uuid7())}, "question_binding_mismatch"),
        (
            {"gateway_tool_call_id": "", "provider_call_id": ""},
            "question_tool_call_binding_missing",
        ),
        ({"gateway_tool_call_id": "not-a-uuid"}, "question_tool_call_binding_invalid"),
    ],
)
async def test_a_mismatched_binding_fails_without_retrying(
    world: World, overrides: dict[str, Any], failure_type: str
) -> None:
    """Retrying would not make the ids match, and would keep the run parked
    on a question nothing can answer."""
    await answer(world)
    with pytest.raises(ApplicationError) as error:
        await deliver(world, **overrides)
    assert error.value.type == failure_type
    assert error.value.non_retryable is True
    assert await observations(world) == []


async def test_a_question_from_another_run_is_refused(world: World) -> None:
    """The binding is on all four ids together, so an answer cannot be
    stitched onto a run that never asked."""
    async with world.session_factory() as session:
        row = await session.get(UserQuestion, world.question.id)
        assert row is not None
        row.run_id = None
        await session.commit()
    with pytest.raises(ApplicationError) as error:
        await deliver(world)
    assert error.value.type == "question_binding_mismatch"


async def test_the_provider_call_id_is_the_fallback_binding(world: World) -> None:
    """Old bundles carry no canonical id; the redacted provider id still
    pairs the observation with its call."""
    await answer(world)
    await deliver(world, gateway_tool_call_id="")

    written = await observations(world)
    assert len(written) == 1
    assert written[0]["tool_call_id"] == "toolu_abc"


def test_the_expiry_wording_tracks_the_wait() -> None:
    """The sentence the agent relays says thirty minutes; if the constant
    moved and the wording did not, the agent would tell the person something
    untrue."""
    from jhin_agent_worker.activities import _DETAIL_QUESTION_TIMED_OUT
    from jhin_tools.ask_person import PERSON_ANSWER_WAIT

    minutes = int(PERSON_ANSWER_WAIT.total_seconds() // 60)
    assert f"{minutes} minutes" in _DETAIL_QUESTION_TIMED_OUT
