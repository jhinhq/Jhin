"""Answering the questions agents ask: authority, idempotency, and the card.

Driven over HTTP rather than against the service, because three of the things
that matter here are route-level: the member/viewer bar, the "exactly one of
option_value or other_text" schema, and the shape the browser actually posts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jhin_api.deps import (
    DbSession,
    Principal,
    get_current_principal,
    get_db,
    get_temporal_client,
)
from jhin_api.questions.router import router as questions_router
from jhin_api.settings import Settings
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AuditEvent,
    Conversation,
    Message,
    Task,
    User,
    UserQuestion,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import (
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    UserQuestionStatus,
    WorkspaceRole,
    new_uuid7,
    structured_content,
)

CSRF = "questions-csrf"
CSRF_HEADERS = {"x-csrf-token": CSRF}
# Real wall-clock time, because the service compares expires_at against it to
# decide whether an answer arrived late. A frozen date would silently make
# every fixture question late the moment that date passed.
NOW = datetime.now(UTC)

SCOPE_OPTIONS: list[dict[str, str]] = [
    {"value": "team", "label": "Only the Engineering team", "detail": "Saved for your team"},
    {"value": "workspace", "label": "Company wide", "detail": "Saved for everyone"},
]


class FakeHandle:
    def __init__(self, client: FakeTemporal, workflow_id: str) -> None:
        self._client = client
        self._workflow_id = workflow_id

    async def signal(self, name: str, *, args: list[str]) -> None:
        if self._client.failures:
            self._client.failures -= 1
            raise OSError("simulated Temporal signal failure")
        self._client.signals.append((self._workflow_id, name, tuple(args)))


class FakeTemporal:
    def __init__(self) -> None:
        self.signals: list[tuple[str, str, tuple[str, ...]]] = []
        self.failures = 0

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(self, workflow_id)


@dataclass
class Harness:
    client: httpx.AsyncClient
    session: AsyncSession
    temporal: FakeTemporal
    actor: dict[str, User]
    users: dict[str, User]
    workspace: Workspace
    other_workspace: Workspace
    agent: Agent
    conversation: Conversation
    task: Task

    def act_as(self, role: WorkspaceRole) -> None:
        self.actor["user"] = self.users[role.value]

    @property
    def base(self) -> str:
        return f"/api/v1/workspaces/{self.workspace.id}/questions"

    async def ask(
        self,
        *,
        kind: str = "memory_scope",
        options: list[dict[str, str]] | None = None,
        allow_other: bool = True,
        status: str = UserQuestionStatus.PENDING.value,
        workspace_id: UUID | None = None,
        with_message: bool = True,
        expires_at: datetime | None = None,
    ) -> UserQuestion:
        """One asked question, plus the chat card the tool writes beside it."""
        chosen = SCOPE_OPTIONS if options is None else options
        text = (
            "Is this deployment schedule only for the Engineering team, or does "
            "the whole company deploy on Mondays at 9am PST?"
        )
        question = UserQuestion(
            workspace_id=workspace_id or self.workspace.id,
            conversation_id=self.conversation.id,
            task_id=self.task.id,
            run_id=None,
            agent_id=self.agent.id,
            kind=kind,
            question=text,
            context="You told me we deploy on Mondays at 9am PST.",
            options_json=list(chosen),
            allow_other=allow_other,
            dedupe_hash=new_uuid7().hex,
            idempotency_key=f"{new_uuid7()}:{new_uuid7()}",
            status=status,
            asked_at=NOW,
            expires_at=expires_at or NOW + timedelta(minutes=30),
        )
        self.session.add(question)
        await self.session.flush()
        if with_message:
            message = Message(
                workspace_id=question.workspace_id,
                task_id=self.task.id,
                conversation_id=self.conversation.id,
                sender_type=SenderType.AGENT.value,
                sender_id=self.agent.id,
                recipient_type=RecipientType.USER.value,
                recipient_id=self.users[WorkspaceRole.ADMIN.value].id,
                message_type=MessageType.QUESTION.value,
                visibility=MessageVisibility.VISIBLE.value,
                content_json=structured_content(
                    text,
                    recommended_next_action="await_answer",
                    kind="user_question",
                    question_id=str(question.id),
                    question=text,
                    question_kind=kind,
                    options=list(chosen),
                    allow_other=allow_other,
                    status=UserQuestionStatus.PENDING.value,
                    delivered="observation",
                    text=text,
                ),
            )
            self.session.add(message)
            await self.session.flush()
            question.message_id = message.id
        await self.session.commit()
        return question


def _user(role: str) -> User:
    return User(
        email=f"{role}-{new_uuid7().hex[:8]}@example.com",
        display_name=role.title(),
        password_hash="x",
    )


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session = maker()

    workspace = Workspace(name="Questions", slug=f"questions-{new_uuid7().hex[:8]}")
    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add_all([workspace, other])
    await session.flush()
    users = {role.value: _user(role.value) for role in WorkspaceRole}
    session.add_all(list(users.values()))
    await session.flush()
    for role, user in users.items():
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=role))
    agent = Agent(workspace_id=workspace.id, name="Ada", slug=f"ada-{new_uuid7().hex[:8]}")
    session.add(agent)
    await session.flush()
    conversation = Conversation(
        workspace_id=workspace.id,
        title="Deploys",
        primary_agent_id=agent.id,
        created_by_user_id=users[WorkspaceRole.ADMIN.value].id,
        last_activity_at=NOW,
    )
    session.add(conversation)
    await session.flush()
    task = Task(
        workspace_id=workspace.id,
        title="Deploys",
        conversation_id=conversation.id,
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
        temporal_workflow_id=f"agent-task-{new_uuid7()}",
    )
    session.add(task)
    await session.commit()

    actor = {"user": users[WorkspaceRole.ADMIN.value]}
    temporal = FakeTemporal()
    settings = Settings()
    app = FastAPI()
    app.state.settings = settings

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(questions_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_principal(request: Request, db: DbSession) -> Principal:
        return Principal(user=actor["user"])

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_principal] = override_principal
    app.dependency_overrides[get_temporal_client] = lambda: temporal

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("jhin_csrf", CSRF)
        yield Harness(
            client=client,
            session=session,
            temporal=temporal,
            actor=actor,
            users=users,
            workspace=workspace,
            other_workspace=other,
            agent=agent,
            conversation=conversation,
            task=task,
        )
    await session.close()
    await engine.dispose()


async def _answer(harness: Harness, question: UserQuestion, body: dict[str, Any]) -> httpx.Response:
    return await harness.client.post(
        f"{harness.base}/{question.id}/answer", json=body, headers=CSRF_HEADERS
    )


async def _reread(harness: Harness, model: type[Any], row_id: UUID) -> Any:
    """Read a row back from the database rather than from the identity map,
    so these assertions are about what was committed and not about what was
    merely assigned to an instance the test already holds."""
    harness.session.expire_all()
    return await harness.session.get(model, row_id)


# --------------------------------------------------------------------------
# The happy path, end to end
# --------------------------------------------------------------------------


async def test_picking_an_option_records_the_choice_stamps_the_card_and_wakes_the_run(
    harness: Harness,
) -> None:
    question = await harness.ask()
    message_id, asked_text = question.message_id, question.question
    assert message_id is not None

    response = await _answer(harness, question, {"option_value": "team"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed"] is True
    answered = body["question"]
    assert answered["status"] == "answered"
    assert answered["answer_kind"] == "option"
    assert answered["answer_option_value"] == "team"
    # The label, not the machine key: the row has to read as a sentence.
    assert answered["answer_text"] == "Only the Engineering team"
    assert answered["answered_by_name"] == "Admin"
    assert answered["agent_name"] == "Ada"
    assert answered["granted_scope"] == "team"
    assert answered["grant_denied_reason"] == ""

    assert harness.temporal.signals == [
        (harness.task.temporal_workflow_id, "question_answer", (str(question.id),))
    ]

    # The card the person is looking at was mutated in the same commit, so a
    # poll that lands a millisecond later never shows a live box for a
    # question that is already answered.
    message = await _reread(harness, Message, message_id)
    assert message is not None
    content = message.content_json
    assert content["status"] == "answered"
    assert content["answer"] == "Only the Engineering team"
    assert content["answer_kind"] == "option"
    assert content["answered_by_name"] == "Admin"
    assert content["answered_at"]
    # What was asked is never rewritten; the feed line is.
    assert content["question"] == asked_text
    assert content["options"] == SCOPE_OPTIONS
    assert content["text"].endswith("— Admin answered: Only the Engineering team")


async def test_the_audit_row_records_what_was_authorised_and_none_of_the_words(
    harness: Harness,
) -> None:
    question = await harness.ask()

    await _answer(harness, question, {"other_text": "Only the platform pod"})

    event = await harness.session.scalar(
        select(AuditEvent).where(AuditEvent.action == "question.answered")
    )
    assert event is not None
    assert event.target_type == "user_question" and event.target_id == question.id
    assert event.actor_id == harness.users[WorkspaceRole.ADMIN.value].id
    assert event.metadata_json == {
        "kind": "memory_scope",
        "answer_kind": "other",
        "option_value": "",
        "granted_scope": "",
        "grant_denied_reason": "free_text_answer",
        "late": False,
    }
    serialized = str(event.metadata_json)
    assert "platform pod" not in serialized and question.question not in serialized


# --------------------------------------------------------------------------
# A typed answer is never a picked one
# --------------------------------------------------------------------------


async def test_typing_an_option_word_for_word_is_still_a_typed_answer(
    harness: Harness,
) -> None:
    """The asymmetry is the point: a person who typed did not choose, so
    nothing wider than the agent's own memory is authorised."""
    question = await harness.ask()

    response = await _answer(harness, question, {"other_text": "Only the Engineering team"})

    assert response.status_code == 200, response.text
    answered = response.json()["question"]
    assert answered["answer_kind"] == "other"
    assert answered["answer_option_value"] == ""
    assert answered["answer_text"] == "Only the Engineering team"
    assert answered["granted_scope"] == ""
    assert answered["grant_denied_reason"] == "free_text_answer"


# --------------------------------------------------------------------------
# The grant matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "option", "expected_scope", "expected_reason"),
    [
        (WorkspaceRole.ADMIN, "workspace", "workspace", ""),
        (WorkspaceRole.ADMIN, "team", "team", ""),
        # A member's ceiling is agent scope: their answer is recorded, and the
        # agent is told an admin has to record it more widely.
        (WorkspaceRole.MEMBER, "team", "", "insufficient_authority"),
        (WorkspaceRole.MEMBER, "workspace", "", "insufficient_authority"),
        (WorkspaceRole.MEMBER, "agent", "agent", ""),
    ],
)
async def test_the_answering_persons_role_decides_what_their_answer_authorises(
    harness: Harness,
    role: WorkspaceRole,
    option: str,
    expected_scope: str,
    expected_reason: str,
) -> None:
    question = await harness.ask(
        options=[
            {"value": option, "label": option.title(), "detail": ""},
            {"value": "agent" if option != "agent" else "team", "label": "Other", "detail": ""},
        ]
    )
    harness.act_as(role)

    response = await _answer(harness, question, {"option_value": option})

    assert response.status_code == 200, response.text
    answered = response.json()["question"]
    assert answered["granted_scope"] == expected_scope
    assert answered["grant_denied_reason"] == expected_reason


async def test_an_open_question_authorises_no_memory_at_all(harness: Harness) -> None:
    question = await harness.ask(
        kind="open",
        options=[
            {"value": "friday", "label": "Friday", "detail": ""},
            {"value": "monday", "label": "Monday", "detail": ""},
        ],
    )

    response = await _answer(harness, question, {"option_value": "friday"})

    answered = response.json()["question"]
    assert answered["granted_scope"] == "" and answered["grant_denied_reason"] == ""


async def test_an_offered_option_that_is_not_a_scope_grants_nothing(harness: Harness) -> None:
    """The ask tool refuses this shape, so the row is a bug upstream. It must
    still not 500 on the person's answer, and it must not grant."""
    question = await harness.ask(
        options=[
            {"value": "engineering", "label": "Engineering only", "detail": ""},
            {"value": "workspace", "label": "Company wide", "detail": ""},
        ]
    )

    response = await _answer(harness, question, {"option_value": "engineering"})

    assert response.status_code == 200, response.text
    answered = response.json()["question"]
    assert answered["granted_scope"] == "" and answered["grant_denied_reason"] == "not_a_scope"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"option_value": "team", "other_text": "both at once"},
        {"other_text": "   "},
        {"option_value": "team", "unexpected": "field"},
    ],
    ids=["neither", "both", "blank", "extra"],
)
async def test_an_answer_must_be_exactly_one_option_or_one_sentence(
    harness: Harness, body: dict[str, Any]
) -> None:
    question = await harness.ask()
    question_id = question.id

    response = await _answer(harness, question, body)

    assert response.status_code == 422, response.text
    unchanged = await _reread(harness, UserQuestion, question_id)
    assert unchanged.status == UserQuestionStatus.PENDING.value


async def test_an_option_nobody_offered_is_refused(harness: Harness) -> None:
    question = await harness.ask()

    response = await _answer(harness, question, {"option_value": "everyone"})

    assert response.status_code == 422
    assert "everyone" in response.json()["detail"]


async def test_a_question_that_takes_no_typed_answer_refuses_one(harness: Harness) -> None:
    question = await harness.ask(
        kind="open",
        allow_other=False,
        options=[
            {"value": "friday", "label": "Friday", "detail": ""},
            {"value": "monday", "label": "Monday", "detail": ""},
        ],
    )

    response = await _answer(harness, question, {"other_text": "Tuesday"})

    assert response.status_code == 422
    assert response.json()["detail"] == "This question does not take a typed answer"


async def test_a_question_in_another_workspace_is_simply_not_found(harness: Harness) -> None:
    elsewhere = await harness.ask(workspace_id=harness.other_workspace.id, with_message=False)

    missing = await _answer(harness, elsewhere, {"option_value": "team"})
    invented = await harness.client.post(
        f"{harness.base}/{new_uuid7()}/answer",
        json={"option_value": "team"},
        headers=CSRF_HEADERS,
    )

    assert missing.status_code == 404 and invented.status_code == 404


async def test_a_cancelled_question_says_the_agent_stopped_waiting(harness: Harness) -> None:
    question = await harness.ask(status=UserQuestionStatus.CANCELLED.value)

    response = await _answer(harness, question, {"option_value": "team"})

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Ada stopped waiting for this. Send it as a message instead."
    )


async def test_a_late_answer_to_an_expired_question_is_still_recorded(harness: Harness) -> None:
    """The person's intent is real; the lateness is what the audit records."""
    question = await harness.ask(
        status=UserQuestionStatus.EXPIRED.value, expires_at=NOW - timedelta(minutes=1)
    )

    response = await _answer(harness, question, {"option_value": "team"})

    assert response.status_code == 200, response.text
    assert response.json()["question"]["status"] == "answered"
    event = await harness.session.scalar(
        select(AuditEvent).where(AuditEvent.action == "question.answered")
    )
    assert event is not None and event.metadata_json["late"] is True


# --------------------------------------------------------------------------
# Single winner, and the repair path
# --------------------------------------------------------------------------


async def test_a_different_second_answer_loses_and_names_who_won(harness: Harness) -> None:
    question = await harness.ask()
    question_id = question.id
    await _answer(harness, question, {"option_value": "team"})
    harness.act_as(WorkspaceRole.MEMBER)

    conflict = await _answer(harness, question, {"option_value": "workspace"})
    also_conflict = await _answer(harness, question, {"other_text": "Only the Engineering team"})

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Admin already answered this."
    # Typed words that read like the recorded label are still a different
    # answer, and must not slide through the idempotent path.
    assert also_conflict.status_code == 409
    stored = await _reread(harness, UserQuestion, question_id)
    assert stored.answer_option_value == "team" and stored.granted_scope == "team"


async def test_sending_the_same_answer_again_repairs_a_failed_wake_up(harness: Harness) -> None:
    question = await harness.ask()
    harness.temporal.failures = 1

    first = await _answer(harness, question, {"option_value": "team"})
    second = await _answer(harness, question, {"option_value": "team"})

    # Recorded either way: the row is the authority, the signal is a courtesy.
    assert first.status_code == 200 and first.json()["resumed"] is False
    assert first.json()["question"]["status"] == "answered"
    assert second.status_code == 200 and second.json()["resumed"] is True
    assert harness.temporal.signals == [
        (harness.task.temporal_workflow_id, "question_answer", (str(question.id),))
    ]
    audited = await harness.session.scalars(
        select(AuditEvent).where(AuditEvent.action == "question.answered")
    )
    assert len(list(audited)) == 1


async def test_a_question_with_no_running_workflow_is_answered_but_not_resumed(
    harness: Harness,
) -> None:
    question = await harness.ask()
    harness.task.temporal_workflow_id = None
    await harness.session.commit()

    response = await _answer(harness, question, {"option_value": "workspace"})

    assert response.status_code == 200, response.text
    assert response.json()["resumed"] is False
    assert response.json()["question"]["granted_scope"] == "workspace"


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


async def test_a_viewer_may_read_a_question_and_may_not_answer_it(harness: Harness) -> None:
    question = await harness.ask()
    question_id = question.id
    harness.act_as(WorkspaceRole.VIEWER)

    readable = await harness.client.get(f"{harness.base}/{question.id}")
    refused = await _answer(harness, question, {"option_value": "team"})

    assert readable.status_code == 200
    assert readable.json()["question"] == question.question
    assert refused.status_code == 403
    stored = await _reread(harness, UserQuestion, question_id)
    assert stored.status == UserQuestionStatus.PENDING.value


async def test_the_list_filters_by_status_and_conversation_and_puts_open_ones_first(
    harness: Harness,
) -> None:
    pending = await harness.ask()
    answered = await harness.ask()
    await _answer(harness, answered, {"option_value": "team"})

    everything = await harness.client.get(harness.base)
    only_pending = await harness.client.get(harness.base, params={"status": "pending"})
    elsewhere = await harness.client.get(harness.base, params={"conversation_id": str(new_uuid7())})
    nonsense = await harness.client.get(harness.base, params={"status": "asleep"})

    assert everything.status_code == 200
    assert everything.json()["total"] == 2
    # Newest last: the still-open one is asked first and still comes first,
    # because the only reason to read this list is to find what is unanswered.
    assert [item["id"] for item in everything.json()["items"]] == [
        str(pending.id),
        str(answered.id),
    ]
    assert [item["id"] for item in only_pending.json()["items"]] == [str(pending.id)]
    assert only_pending.json()["total"] == 1
    assert elsewhere.json()["items"] == []
    assert nonsense.status_code == 422


async def test_the_projection_never_leaks_how_the_grant_is_decided(harness: Harness) -> None:
    """``run_id``, the dedupe and idempotency keys, the recorded authority,
    and the consumption stamps are how the platform decides whether a model
    may widen a memory. None of them are anyone's business over HTTP."""
    question = await harness.ask()
    await _answer(harness, question, {"option_value": "team"})

    body = (await harness.client.get(f"{harness.base}/{question.id}")).json()

    assert set(body) == {
        "id",
        "workspace_id",
        "conversation_id",
        "task_id",
        "message_id",
        "agent_id",
        "agent_name",
        "kind",
        "question",
        "context",
        "options",
        "allow_other",
        "status",
        "asked_at",
        "expires_at",
        "answered_at",
        "answered_by_user_id",
        "answered_by_name",
        "answer_kind",
        "answer_option_value",
        "answer_text",
        "granted_scope",
        "grant_denied_reason",
    }
    assert body["options"] == SCOPE_OPTIONS
