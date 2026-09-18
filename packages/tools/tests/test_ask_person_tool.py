"""organization.ask_person through the full gateway pipeline against
in-memory SQLite: what the schema refuses, what the budgets refuse, and what
a repeat gets instead of a second box on somebody's screen."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AuditEvent,
    Conversation,
    Message,
    Task,
    Team,
    User,
    UserQuestion,
    Workspace,
)
from jhin_domain import MessageType, TaskState, UserQuestionStatus, new_uuid7
from jhin_tools.ask_person import (
    MAX_QUESTIONS_PER_CONVERSATION_HOUR,
    MAX_QUESTIONS_PER_RUN,
    PERSON_ANSWER_WAIT,
    AskPersonInput,
    AskPersonOption,
    AskPersonOutput,
    _ask_person,
)
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import GatewayOutcome, ToolGateway

SCOPE_OPTIONS = [
    {"value": "team", "label": "Only the Engineering team", "detail": "Saved for your team"},
    {"value": "workspace", "label": "Company wide", "detail": "Saved for everyone"},
]


class Org:
    workspace: Workspace
    team: Team
    me: Agent
    user: User
    conversation: Conversation
    task: Task
    run_id: UUID

    def gateway(
        self, session: AsyncSession, *, task: Task | None = None, run_id: UUID | None = None
    ) -> ToolGateway:
        ctx = ToolExecutionContext(
            session=session,
            workspace_id=self.workspace.id,
            task_id=(task or self.task).id,
            run_id=run_id or self.run_id,
            agent_id=self.me.id,
            agent_name=self.me.name,
        )
        return ToolGateway(ctx, build_builtin_catalog())


@pytest.fixture
async def session() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
async def org(session: AsyncSession) -> Org:
    f = Org()
    f.workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(f.workspace)
    await session.flush()
    ws = f.workspace.id
    f.team = Team(workspace_id=ws, name="Engineering")
    f.user = User(
        email=f"varand-{new_uuid7().hex[:8]}@example.com",
        display_name="Varand",
        password_hash="x",
    )
    session.add_all([f.team, f.user])
    await session.flush()
    f.me = Agent(workspace_id=ws, team_id=f.team.id, name="Ada", slug="ada")
    session.add(f.me)
    await session.flush()
    f.conversation = Conversation(
        workspace_id=ws,
        title="Deploys",
        primary_agent_id=f.me.id,
        created_by_user_id=f.user.id,
        last_activity_at=datetime.now(UTC),
    )
    session.add(f.conversation)
    await session.flush()
    f.task = Task(
        workspace_id=ws,
        title="Chat turn",
        state=TaskState.RUNNING.value,
        assigned_agent_id=f.me.id,
        conversation_id=f.conversation.id,
        correlation_id=new_uuid7(),
        metadata_json={"origin": "conversation"},
    )
    session.add(f.task)
    await session.flush()
    session.add(
        AgentCapabilityGrant(
            workspace_id=ws,
            agent_id=f.me.id,
            capability="organization.ask_person",
            scope_json={},
            effect="allow",
        )
    )
    await session.flush()
    f.run_id = new_uuid7()
    return f


async def test_required_question_blocks_real_gateway_actions_until_valid_answer(session, org):
    from pydantic import BaseModel

    from jhin_policy import RiskLevel, ToolDefinition
    from jhin_tools.builtin import ToolCatalog
    from jhin_tools.readiness import resolve_required_answer

    class Arguments(BaseModel):
        url: str

    calls = []

    async def execute(ctx, payload):
        calls.append(payload.url)
        return payload

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="research.fetch",
            description="Fetch",
            risk=RiskLevel.READ,
            input_model=Arguments,
            output_model=Arguments,
            required_capability="research.read",
        ),
        execute,
    )
    session.add(
        AgentCapabilityGrant(
            workspace_id=org.workspace.id,
            agent_id=org.me.id,
            capability="research.read",
            effect="allow",
            scope_json={},
        )
    )
    ctx = ToolExecutionContext(
        session=session,
        workspace_id=org.workspace.id,
        task_id=org.task.id,
        run_id=org.run_id,
        agent_id=org.me.id,
        agent_name=org.me.name,
    )
    asked = await _ask_person(
        ctx,
        AskPersonInput(
            question="What is the Ghost Admin URL?",
            input_key="ghost_admin_url",
            value_type="url",
            options=[],
            required=True,
        ),
    )
    question = await session.get(UserQuestion, UUID(asked.question_id))
    # The old timeout cannot authorize dependent research or other actions.
    question.expires_at = datetime.now(UTC) - timedelta(hours=1)
    gateway = ToolGateway(ctx, catalog)
    denied = await gateway.request("research.fetch", '{"url":"https://guessed.example"}')
    assert denied.decision_code == "required_input_missing" and calls == []
    with pytest.raises(ValueError):
        await resolve_required_answer(session, question, "some guessed website")
    assert org.task.metadata_json["required_inputs"]
    await resolve_required_answer(session, question, "https://actual.example/admin/")
    question.status = "answered"
    await session.flush()
    allowed = await gateway.request("research.fetch", '{"url":"https://actual.example/admin/"}')
    assert allowed.status == "executed" and calls == ["https://actual.example/admin/"]


async def test_live_publishing_setup_question_is_accepted_without_rewording(session, org):
    question_text = (
        "To set up the recurring Monday 9am (Pacific) blog publishing job, tell me the setup: "
        "(1) Where do posts get published — the site/destination URL, and (2) each week do I "
        "auto-publish the post or leave it as a draft for you/Mindy to review? (3) What kind "
        "of post or topic does each Monday cover? If we don't yet have a Ghost connection "
        "set up, let me know and I'll walk you through the Admin URL + stored API key "
        "you'll need to give me."
    )
    context_text = (
        "You asked to set up recurring blog publishing on Mondays at 9am PST. I'll assume "
        '"PST" means Pacific local time (America/Los_Angeles) — which in September is actually '
        "9am PDT. Correct me if you meant fixed UTC-8. I currently have no publishing "
        "destination stored, so I need these details before creating the recurring job."
    )
    result = await org.gateway(session).request(
        "organization.ask_person",
        json.dumps(
            {
                "question": question_text,
                "context": context_text,
                "required": True,
                "allow_other": True,
            }
        ),
    )
    assert result.status == "executed", result
    row = await session.get(UserQuestion, UUID(result.sanitized_output["question_id"]))
    assert row.question == question_text and row.context == context_text


def test_question_and_supplementary_context_accept_the_documented_boundary():
    data = AskPersonInput(question="q" * 1000, context="c" * 2000)
    assert data.question == "q" * 1000 and data.context == "c" * 2000


@pytest.mark.parametrize("field,length", [("question", 1001), ("context", 2001)])
def test_question_limits_reject_oversized_words_instead_of_truncating(field, length):
    from pydantic import ValidationError

    values = {"question": "Which site should receive the posts?", field: "x" * length}
    with pytest.raises(ValidationError) as error:
        AskPersonInput(**values)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "string_too_long"


@pytest.mark.parametrize(
    "value_type,values",
    [
        ("timezone", ["America/Los_Angeles", "Etc/GMT+8"]),
        ("time", ["09:00", "10:00"]),
        ("url", ["https://first.example", "https://second.example"]),
    ],
)
def test_typed_question_choices_display_their_actual_values(value_type, values):
    data = AskPersonInput(
        question="Which value should I use?",
        value_type=value_type,
        options=[
            {"label": "Misleading fixed PST", "value": value, "detail": "Invented interpretation"}
            for value in values
        ],
    )
    assert [item.value for item in data.options] == values
    assert [item.label for item in data.options] == values
    assert all(item.detail == "" for item in data.options)
    assert data.allow_other


@pytest.mark.parametrize(
    "value_type,values",
    [
        ("timezone", ["pdt_pst", "pst_fixed"]),
        ("time", ["nine_am", "ten_am"]),
        ("url", ["my_blog", "other_blog"]),
    ],
)
def test_typed_question_aliases_become_free_text_without_guessing(value_type, values):
    data = AskPersonInput(
        question="What is the actual value?",
        value_type=value_type,
        allow_other=False,
        options=[{"label": "America/Los_Angeles", "value": value} for value in values],
    )
    assert data.options == [] and data.allow_other


async def test_live_timezone_alias_question_reaches_a_free_text_card(session, org):
    result = await org.gateway(session).request(
        "organization.ask_person",
        json.dumps(
            {
                "question": "Which timezone should Monday 9am use?",
                "value_type": "timezone",
                "required": True,
                "input_key": "publishing_timezone",
                "allow_other": False,
                "options": [
                    {"label": "Pacific with daylight saving", "value": "pdt_pst"},
                    {"label": "Fixed Pacific standard time", "value": "pst_fixed"},
                ],
            }
        ),
    )
    assert result.status == "executed", result
    row = await session.get(UserQuestion, UUID(result.sanitized_output["question_id"]))
    assert row.options_json == [] and row.allow_other and row.value_type == "timezone"


@pytest.mark.parametrize("cli_refusal", [False, True])
async def test_unchanged_failed_operation_is_bounded_in_gateway(session, org, cli_refusal):
    from pydantic import BaseModel

    from jhin_policy import RiskLevel, ToolDefinition
    from jhin_tools.builtin import ToolCatalog
    from jhin_tools.errors import ToolExecutionError

    class Arguments(BaseModel):
        url: str

    class CommandResult(BaseModel):
        stdout: str
        exit_code: int

    calls = []

    async def execute(ctx, payload):
        calls.append(payload.url)
        if cli_refusal:
            return CommandResult(stdout="HTTP/2 403 Forbidden", exit_code=0)
        raise ToolExecutionError(
            "Forbidden", code="http_403", hint="Correct access", side_effect_possible=False
        )

    catalog = ToolCatalog()
    name = "cli.http_probe" if cli_refusal else "research.fetch"
    catalog.register(
        ToolDefinition(
            name=name,
            description="Fetch",
            risk=RiskLevel.READ,
            input_model=Arguments,
            output_model=CommandResult if cli_refusal else Arguments,
            required_capability="research.read",
        ),
        execute,
    )
    session.add(
        AgentCapabilityGrant(
            workspace_id=org.workspace.id,
            agent_id=org.me.id,
            capability="research.read",
            effect="allow",
            scope_json={},
        )
    )
    ctx = ToolExecutionContext(
        session=session,
        workspace_id=org.workspace.id,
        task_id=org.task.id,
        run_id=org.run_id,
        agent_id=org.me.id,
        agent_name=org.me.name,
    )
    gateway = ToolGateway(ctx, catalog)
    for _ in range(2):
        assert (await gateway.request(name, '{"url":"https://actual.example"}')).status == (
            "executed" if cli_refusal else "failed"
        )
    blocked = await gateway.request(name, '{"url":"https://actual.example"}')
    assert blocked.decision_code == "unchanged_failure_limit" and len(calls) == 2


async def test_reserved_publisher_question_uses_canonical_card_and_cannot_reuse_unreserved_answer(
    session, org
):
    wording = "Which agent may review and publish Ghost drafts?"
    old = await ask(
        session,
        org,
        question=wording,
        context="Do not use this person",
        kind="open",
        options=[],
        required=False,
    )
    row = await session.get(UserQuestion, UUID(old.sanitized_output["question_id"]))
    row.status = "answered"
    row.answer_text = "Bisby"
    await session.flush()
    new = await ask(
        session,
        org,
        question="Which person must never publish?",
        context="Avoid this person",
        kind="open",
        options=[],
        input_key="ghost_publisher_agent_id",
    )
    assert new.sanitized_output["status"] == "asked"
    current = await session.get(UserQuestion, UUID(new.sanitized_output["question_id"]))
    assert current.id != row.id
    assert current.question == wording and current.context == ""
    assert current.required and current.options_json == [] and current.allow_other
    assert "ghost_publisher_agent_id" not in org.task.metadata_json.get("resolved_inputs", {})
    card = await session.get(Message, current.message_id)
    assert card.content_json["question"] == wording


async def ask(
    session: AsyncSession,
    org: Org,
    *,
    task: Task | None = None,
    run_id: UUID | None = None,
    **body: Any,
) -> GatewayOutcome:
    payload: dict[str, Any] = {
        "question": "Is this deployment schedule only for Engineering, or company wide?",
        "context": "You told me we deploy on Mondays and I want to file it correctly.",
        "options": SCOPE_OPTIONS,
        "kind": "memory_scope",
        **body,
    }
    return await org.gateway(session, task=task, run_id=run_id).request(
        "organization.ask_person", json.dumps(payload)
    )


def as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres does not."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def question_count(session: AsyncSession, org: Org) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(UserQuestion)
            .where(UserQuestion.workspace_id == org.workspace.id)
        )
        or 0
    )


class TestSchema:
    @pytest.mark.parametrize(
        ("body", "why"),
        [
            ({"options": SCOPE_OPTIONS[:1]}, "one option is not a choice"),
            (
                {
                    "options": [{"value": f"o{n}", "label": f"Option {n}"} for n in range(5)],
                    "kind": "open",
                },
                "five options is a list to read, not a decision",
            ),
            (
                {"options": [SCOPE_OPTIONS[0], dict(SCOPE_OPTIONS[0])]},
                "duplicate values cannot be told apart by the answer endpoint",
            ),
            (
                {
                    "options": [
                        {"value": "engineering", "label": "Engineering"},
                        {"value": "workspace", "label": "Company wide"},
                    ]
                },
                "a memory_scope option must name a real scope",
            ),
            (
                {
                    "kind": "open",
                    "options": [
                        {"value": "other", "label": "Something else"},
                        {"value": "monday", "label": "Monday"},
                    ],
                },
                "'other' is the free-text row, not an option",
            ),
        ],
    )
    async def test_refused_before_anything_is_written(
        self, session: AsyncSession, org: Org, body: dict[str, Any], why: str
    ) -> None:
        outcome = await ask(session, org, **body)
        assert outcome.status != "executed", why
        assert await question_count(session, org) == 0

    async def test_a_scope_question_always_leaves_a_way_out(
        self, session: AsyncSession, org: Org
    ) -> None:
        """A choice the person cannot decline is a leading question."""
        outcome = await ask(session, org, allow_other=False)
        assert outcome.status == "executed", outcome.decision_reason
        row = await session.scalar(select(UserQuestion))
        assert row is not None and row.allow_other is True


class TestAsking:
    async def test_writes_the_row_the_card_reads(self, session: AsyncSession, org: Org) -> None:
        before = datetime.now(UTC)
        outcome = await ask(session, org)
        assert outcome.status == "executed", outcome.decision_reason
        output = outcome.sanitized_output or {}
        assert output["status"] == "asked"

        row = await session.scalar(select(UserQuestion))
        assert row is not None
        assert str(row.id) == output["question_id"]
        assert row.status == UserQuestionStatus.PENDING.value
        assert row.conversation_id == org.conversation.id
        assert row.run_id == org.run_id
        # The row is only live as long as the run will wait for it.
        assert (
            timedelta(seconds=0)
            <= as_utc(row.expires_at) - before - PERSON_ANSWER_WAIT
            < timedelta(seconds=5)
        )
        # Nothing about the answer is decided by the agent.
        assert (row.answer_kind, row.granted_scope, row.granted_authority) == ("", "", "")

        message = await session.get(Message, row.message_id)
        assert message is not None
        assert message.message_type == MessageType.QUESTION.value
        content = message.content_json
        assert content["kind"] == "user_question"
        assert content["question_id"] == str(row.id)
        assert content["status"] == "pending"
        assert [option["value"] for option in content["options"]] == ["team", "workspace"]
        assert content["allow_other"] is True
        assert content["asked_by_agent_name"] == "Ada"
        # The person who opened the chat is who it is addressed to.
        assert message.recipient_id == org.user.id

    async def test_the_agent_never_reads_its_own_question_as_a_message(
        self, session: AsyncSession, org: Org
    ) -> None:
        """``delivered: observation`` is what keeps the question out of the
        task history; the answer reaches the model once, as the call's
        result."""
        await ask(session, org)
        row = await session.scalar(select(UserQuestion))
        assert row is not None
        message = await session.get(Message, row.message_id)
        assert message is not None
        assert message.content_json["delivered"] == "observation"

    async def test_audit_records_the_shape_and_not_the_words(
        self, session: AsyncSession, org: Org
    ) -> None:
        await ask(session, org)
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "question.asked")
        )
        assert event is not None
        assert event.metadata_json["kind"] == "memory_scope"
        assert event.metadata_json["options"] == ["team", "workspace"]
        serialized = json.dumps(event.metadata_json)
        assert "deployment schedule" not in serialized


class TestBudgets:
    async def test_a_fourth_question_in_a_run_is_refused_in_step(
        self, session: AsyncSession, org: Org
    ) -> None:
        for n in range(MAX_QUESTIONS_PER_RUN):
            outcome = await ask(session, org, question=f"Question number {n}?")
            assert (outcome.sanitized_output or {})["status"] == "asked"
        outcome = await ask(session, org, question="One more thing?")
        assert outcome.status == "executed", outcome.decision_reason
        output = outcome.sanitized_output or {}
        assert output["status"] == "not_asked"
        assert "limit" in output["detail"]
        assert await question_count(session, org) == MAX_QUESTIONS_PER_RUN

    async def test_the_hourly_cap_survives_a_new_run(self, session: AsyncSession, org: Org) -> None:
        """The per-run cap alone lets consecutive runs re-ask forever."""
        for n in range(MAX_QUESTIONS_PER_CONVERSATION_HOUR):
            outcome = await ask(session, org, question=f"Question number {n}?", run_id=new_uuid7())
            assert (outcome.sanitized_output or {})["status"] == "asked"
        outcome = await ask(session, org, question="Yet another thing?", run_id=new_uuid7())
        assert (outcome.sanitized_output or {})["status"] == "not_asked"
        assert await question_count(session, org) == MAX_QUESTIONS_PER_CONVERSATION_HOUR

    async def test_an_old_question_does_not_count_against_the_hour(
        self, session: AsyncSession, org: Org
    ) -> None:
        for n in range(MAX_QUESTIONS_PER_CONVERSATION_HOUR):
            await ask(session, org, question=f"Question number {n}?", run_id=new_uuid7())
        stale = datetime.now(UTC) - timedelta(hours=2)
        for row in await session.scalars(select(UserQuestion)):
            row.asked_at = stale
        await session.flush()
        outcome = await ask(session, org, question="Fresh hour?", run_id=new_uuid7())
        assert (outcome.sanitized_output or {})["status"] == "asked"


class TestRepeats:
    async def test_a_pending_twin_is_refused_without_a_second_box(
        self, session: AsyncSession, org: Org
    ) -> None:
        first = await ask(session, org)
        again = await ask(session, org, run_id=new_uuid7())
        output = again.sanitized_output or {}
        assert output["status"] == "already_asked"
        assert output["question_id"] == (first.sanitized_output or {})["question_id"]
        assert await question_count(session, org) == 1

    async def test_a_reworded_repeat_is_still_a_repeat(
        self, session: AsyncSession, org: Org
    ) -> None:
        """Normalized like memory content, so moving a comma is not a new
        question."""
        await ask(session, org)
        again = await ask(
            session,
            org,
            question="  is this DEPLOYMENT schedule only for Engineering, or company wide  ",
        )
        assert (again.sanitized_output or {})["status"] == "already_asked"
        assert await question_count(session, org) == 1

    async def test_an_answered_twin_returns_the_answer_it_already_has(
        self, session: AsyncSession, org: Org
    ) -> None:
        first = await ask(session, org)
        row = await session.scalar(select(UserQuestion))
        assert row is not None
        row.status = UserQuestionStatus.ANSWERED.value
        row.answer_kind = "option"
        row.answer_option_value = "team"
        row.answer_text = "Only the Engineering team"
        row.granted_scope = "team"
        row.answered_at = datetime.now(UTC)
        row.answered_by_user_id = org.user.id
        await session.flush()

        again = await ask(session, org)
        output = again.sanitized_output or {}
        assert output["status"] == "already_answered"
        assert output["question_id"] == (first.sanitized_output or {})["question_id"]
        assert output["answer_kind"] == "option"
        assert output["option_value"] == "team"
        assert output["answer"] == "Only the Engineering team"
        # Same run, so the grant is still spendable.
        assert output["granted_scope"] == "team"
        assert await question_count(session, org) == 1

    async def test_a_grant_from_an_earlier_run_is_not_offered_again(
        self, session: AsyncSession, org: Org
    ) -> None:
        """The answer is still useful; the authority it carried is not."""
        await ask(session, org)
        row = await session.scalar(select(UserQuestion))
        assert row is not None
        row.status = UserQuestionStatus.ANSWERED.value
        row.answer_kind = "option"
        row.answer_option_value = "team"
        row.answer_text = "Only the Engineering team"
        row.granted_scope = "team"
        await session.flush()

        again = await ask(session, org, run_id=new_uuid7())
        output = again.sanitized_output or {}
        assert output["status"] == "already_answered"
        assert output["answer"] == "Only the Engineering team"
        assert output["granted_scope"] == ""

    async def test_an_expired_twin_may_be_asked_again(
        self, session: AsyncSession, org: Org
    ) -> None:
        await ask(session, org)
        row = await session.scalar(select(UserQuestion))
        assert row is not None
        row.status = UserQuestionStatus.EXPIRED.value
        await session.flush()
        again = await ask(session, org, run_id=new_uuid7())
        assert (again.sanitized_output or {})["status"] == "asked"
        assert await question_count(session, org) == 2


class TestIdempotency:
    async def test_a_retried_call_reuses_its_row_instead_of_asking_twice(
        self, session: AsyncSession, org: Org
    ) -> None:
        """The gateway pins a call to a stable id, so a retry that reaches the
        executor again must land on the same question rather than a second box
        with the same words."""
        first = await ask(session, org)
        question_id = (first.sanitized_output or {})["question_id"]
        row = await session.scalar(select(UserQuestion))
        assert row is not None
        # The key is this run and this exact call, so a retry of the same
        # call collides instead of writing a second question.
        assert row.idempotency_key == f"{org.run_id}:{first.tool_call_id}"

        # Replay the executor directly on that identity; the wording is
        # changed so the dedupe guard cannot catch it, leaving the unique
        # constraint as the only thing holding the line.
        ctx = ToolExecutionContext(
            session=session,
            workspace_id=org.workspace.id,
            task_id=org.task.id,
            run_id=org.run_id,
            agent_id=org.me.id,
            agent_name=org.me.name,
            tool_call_id=first.tool_call_id,
        )
        replayed = await _ask_person(
            ctx,
            AskPersonInput(
                question="Reworded so the repeat guard does not catch it?",
                options=[AskPersonOption(**option) for option in SCOPE_OPTIONS],
                kind="memory_scope",
            ),
        )
        assert isinstance(replayed, AskPersonOutput)
        assert replayed.status == "asked"
        assert replayed.question_id == question_id
        assert await question_count(session, org) == 1

    async def test_a_retry_after_the_question_closed_does_not_park_again(
        self, session: AsyncSession, org: Org
    ) -> None:
        """Waiting thirty minutes for an answer that can never come is worse
        than saying so now."""
        first = await ask(session, org)
        row = await session.scalar(select(UserQuestion))
        assert row is not None
        row.status = UserQuestionStatus.CANCELLED.value
        await session.flush()
        ctx = ToolExecutionContext(
            session=session,
            workspace_id=org.workspace.id,
            task_id=org.task.id,
            run_id=org.run_id,
            agent_id=org.me.id,
            agent_name=org.me.name,
            tool_call_id=first.tool_call_id,
        )
        replayed = await _ask_person(
            ctx,
            AskPersonInput(
                question="Reworded so the repeat guard does not catch it?",
                options=[AskPersonOption(**option) for option in SCOPE_OPTIONS],
                kind="memory_scope",
            ),
        )
        assert isinstance(replayed, AskPersonOutput)
        assert replayed.status == "not_asked"
        assert await question_count(session, org) == 1


def test_the_model_cannot_write_what_a_scope_option_says() -> None:
    """The label is what a person reads; the value is what the API grants.
    While the model authored both and nothing bound them together, an option
    reading "Only the Engineering team" could carry ``workspace`` and turn one
    click into a company-wide memory."""
    data = AskPersonInput(
        question="Is that just your team?",
        kind="memory_scope",
        options=[
            AskPersonOption(value="team", label="Company wide", detail="everyone"),
            AskPersonOption(value="workspace", label="Only the Engineering team", detail="just us"),
        ],
    )
    by_value = {option.value: option for option in data.options}
    assert "Company wide" not in by_value["team"].label
    assert "Engineering" not in by_value["workspace"].label
    assert "workspace" in by_value["workspace"].label.lower()
    # And the escape hatch cannot be switched off on a scope question.
    assert data.allow_other is True


def test_an_open_question_still_uses_the_agents_own_words() -> None:
    data = AskPersonInput(
        question="Which environment?",
        options=[
            AskPersonOption(value="staging", label="Staging", detail=""),
            AskPersonOption(value="prod", label="Production", detail=""),
        ],
    )
    assert [option.label for option in data.options] == ["Staging", "Production"]
