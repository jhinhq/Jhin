"""memory.search / memory.propose through the full gateway pipeline against
in-memory SQLite: deny-by-default, scoped to the calling agent, and policy
routed (never activates workspace memory, never amplifies visibility)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    MemoryRecord,
    Message,
    Task,
    Team,
    User,
    UserQuestion,
    Workspace,
)
from jhin_domain import MemoryScope, MemoryStatus, TaskState, UserQuestionStatus, new_uuid7
from jhin_tools.builtin import ToolExecutionContext, build_builtin_catalog
from jhin_tools.gateway import GatewayOutcome, ToolGateway


class Org:
    workspace: Workspace
    team: Team
    me: Agent
    other: Agent
    task: Task
    team_task: Task

    def gateway(
        self,
        session: AsyncSession,
        agent: Agent,
        task: Task | None = None,
        run_id: Any = None,
    ) -> ToolGateway:
        ctx = ToolExecutionContext(
            session=session,
            workspace_id=self.workspace.id,
            task_id=(task or self.task).id,
            run_id=run_id or new_uuid7(),
            agent_id=agent.id,
            agent_name=agent.name,
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
    session.add(f.team)
    await session.flush()
    f.me = Agent(workspace_id=ws, team_id=f.team.id, name="Me", slug="me")
    f.other = Agent(workspace_id=ws, team_id=f.team.id, name="Other", slug="other")
    session.add_all([f.me, f.other])
    await session.flush()
    f.task = Task(
        workspace_id=ws,
        title="Private task",
        state=TaskState.RUNNING.value,
        assigned_agent_id=f.me.id,
        correlation_id=new_uuid7(),
    )
    f.team_task = Task(
        workspace_id=ws,
        title="Team task",
        state=TaskState.RUNNING.value,
        assigned_agent_id=f.me.id,
        assigned_team_id=f.team.id,
        correlation_id=new_uuid7(),
    )
    session.add_all([f.task, f.team_task])
    await session.flush()
    return f


async def grant(session: AsyncSession, org: Org, agent: Agent, capability: str) -> None:
    session.add(
        AgentCapabilityGrant(
            workspace_id=org.workspace.id,
            agent_id=agent.id,
            capability=capability,
            scope_json={},
            effect="allow",
        )
    )
    await session.flush()


async def seed(
    session: AsyncSession, org: Org, content: str, *, scope: MemoryScope, scope_id: Any
) -> MemoryRecord:
    record = MemoryRecord(
        workspace_id=org.workspace.id,
        scope=scope.value,
        scope_id=scope_id,
        kind="fact",
        content=content,
        content_hash=new_uuid7().hex,
        visibility=scope.value,
        status=MemoryStatus.ACTIVE.value,
        created_by_type="agent",
    )
    session.add(record)
    await session.flush()
    return record


async def search(session: AsyncSession, org: Org, agent: Agent, query: str) -> GatewayOutcome:
    return await org.gateway(session, agent).request(
        "memory.search", json.dumps({"query": query, "limit": 10})
    )


async def propose(
    session: AsyncSession,
    org: Org,
    agent: Agent,
    task: Task | None = None,
    run_id: Any = None,
    **body: Any,
) -> GatewayOutcome:
    payload = {"content": "We deploy on Tuesdays.", **body}
    return await org.gateway(session, agent, task, run_id).request(
        "memory.propose", json.dumps(payload)
    )


class TestSearch:
    async def test_denied_without_grant(self, session: AsyncSession, org: Org) -> None:
        outcome = await search(session, org, org.me, "deploy")
        assert outcome.status == "denied"

    async def test_returns_only_authorized_records(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.read")
        mine = await seed(
            session, org, "my deploy note", scope=MemoryScope.AGENT, scope_id=org.me.id
        )
        theirs = await seed(
            session, org, "their deploy note", scope=MemoryScope.AGENT, scope_id=org.other.id
        )
        team = await seed(
            session, org, "team deploy note", scope=MemoryScope.TEAM, scope_id=org.team.id
        )
        outcome = await search(session, org, org.me, "deploy note")
        assert outcome.status == "executed", outcome.decision_reason
        ids = {item["id"] for item in (outcome.sanitized_output or {})["items"]}
        assert ids == {str(mine.id), str(team.id)}
        assert str(theirs.id) not in ids
        assert (outcome.sanitized_output or {})["degraded"] is True

    async def test_rejects_malformed_input(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.read")
        outcome = await org.gateway(session, org.me).request(
            "memory.search", json.dumps({"query": "x", "agent_id": str(org.other.id)})
        )
        assert outcome.status != "executed"


class TestPropose:
    async def test_denied_without_grant(self, session: AsyncSession, org: Org) -> None:
        outcome = await propose(session, org, org.me)
        assert outcome.status == "denied"

    async def test_private_memory_activates(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, subject="deploy.day")
        assert outcome.status == "executed", outcome.decision_reason
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "activate"
        assert output["status"] == "active"
        record = await session.get(MemoryRecord, __import__("uuid").UUID(output["memory_id"]))
        assert record is not None
        assert record.scope == "agent"
        assert record.scope_id == org.me.id
        assert record.source_task_id == org.task.id
        assert record.created_by_type == "agent"
        assert record.created_by_id == org.me.id

    async def test_team_scope_from_private_task_is_rejected(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, requested_scope="team")
        assert outcome.status == "executed"
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "reject"
        assert "non_amplification" in output["reasons"]
        assert (await session.scalar(select(MemoryRecord))) is None

    async def test_team_scope_from_team_task_activates(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, org.team_task, requested_scope="team")
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "activate"
        record = await session.scalar(select(MemoryRecord))
        assert record is not None
        assert record.scope == "team"
        assert record.scope_id == org.team.id

    async def test_workspace_scope_never_activates_directly(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, org.team_task, requested_scope="workspace")
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "reject"  # team-visible source < workspace
        assert (await session.scalar(select(MemoryRecord))) is None

    async def test_secret_is_rejected(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(
            session, org, org.me, content="API key: sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        )
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "reject"
        assert any(r.startswith("secret:") for r in output["reasons"])

    async def test_duplicate_reports_existing(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        first = (await propose(session, org, org.me)).sanitized_output or {}
        second = (await propose(session, org, org.me)).sanitized_output or {}
        assert second["outcome"] == "duplicate"
        assert second["memory_id"] == first["memory_id"]

    async def test_model_cannot_set_status(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, status="active")
        assert outcome.status != "executed"


class TestAnsweredScopeGrant:
    """A memory wider than the chat it came from is authorised by a row the
    API wrote, and by nothing the model says. Every check re-reads that row.
    """

    @staticmethod
    async def answered_question(
        session: AsyncSession,
        org: Org,
        *,
        run_id: Any,
        agent: Agent | None = None,
        granted_scope: str = "team",
        granted_authority: str = "workspace",
        status: str = UserQuestionStatus.ANSWERED.value,
        answered_by: User | None = None,
        consumed_at: Any = None,
    ) -> UserQuestion:
        question = UserQuestion(
            workspace_id=org.workspace.id,
            conversation_id=None,
            task_id=org.task.id,
            run_id=run_id,
            agent_id=(agent or org.me).id,
            kind="memory_scope",
            question="Is this only for Engineering, or company wide?",
            options_json=[{"value": "team", "label": "Only Engineering"}],
            dedupe_hash=new_uuid7().hex,
            idempotency_key=new_uuid7().hex,
            status=status,
            asked_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            answered_at=datetime.now(UTC),
            answered_by_user_id=answered_by.id if answered_by is not None else None,
            answer_kind="option",
            answer_option_value=granted_scope or "team",
            answer_text="Only the Engineering team",
            granted_scope=granted_scope,
            granted_authority=granted_authority,
            grant_consumed_at=consumed_at,
        )
        session.add(question)
        await session.flush()
        return question

    async def test_an_answer_lets_a_team_memory_out_of_a_private_chat(
        self, session: AsyncSession, org: Org
    ) -> None:
        """The case the whole feature exists for: a 1:1 chat is agent-visible,
        so without the answer this is `non_amplification`."""
        await grant(session, org, org.me, "memory.propose")
        run_id = new_uuid7()
        person = User(
            email=f"v-{new_uuid7().hex[:8]}@example.com", display_name="Varand", password_hash="x"
        )
        session.add(person)
        await session.flush()
        question = await self.answered_question(session, org, run_id=run_id, answered_by=person)
        outcome = await propose(
            session,
            org,
            org.me,
            run_id=run_id,
            content="Engineering deploys on Mondays at 9am PST.",
            requested_scope="team",
            authorized_by_question_id=str(question.id),
        )
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "activate", output
        record = await session.scalar(select(MemoryRecord))
        assert record is not None
        assert record.scope == "team"
        assert record.scope_id == org.team.id
        # The memory is attributed to the person whose authority it used.
        assert record.created_by_type == "user"
        assert record.created_by_id == person.id
        # And the answer is spent.
        assert question.grant_consumed_at is not None

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            ({"granted_scope": ""}, "scope_not_authorized"),
            ({"granted_scope": "workspace"}, "scope_mismatch"),
            ({"status": UserQuestionStatus.PENDING.value}, "question_not_answered"),
        ],
    )
    async def test_the_row_decides_not_the_argument(
        self, session: AsyncSession, org: Org, mutate: dict[str, Any], expected: str
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        run_id = new_uuid7()
        question = await self.answered_question(session, org, run_id=run_id, **mutate)
        outcome = await propose(
            session,
            org,
            org.me,
            run_id=run_id,
            content="Engineering deploys on Mondays at 9am PST.",
            requested_scope="team",
            authorized_by_question_id=str(question.id),
        )
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "reject"
        assert output["reasons"] == [expected]
        assert output["detail"] and expected not in output["detail"]
        assert (await session.scalar(select(MemoryRecord))) is None

    async def test_a_question_that_does_not_exist_authorises_nothing(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(
            session,
            org,
            org.me,
            content="Engineering deploys on Mondays at 9am PST.",
            requested_scope="team",
            authorized_by_question_id=str(new_uuid7()),
        )
        output = outcome.sanitized_output or {}
        assert output["reasons"] == ["question_not_found"]
        assert (await session.scalar(select(MemoryRecord))) is None

    async def test_another_agents_answer_is_not_yours_to_spend(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        run_id = new_uuid7()
        question = await self.answered_question(session, org, run_id=run_id, agent=org.other)
        outcome = await propose(
            session,
            org,
            org.me,
            run_id=run_id,
            content="Engineering deploys on Mondays at 9am PST.",
            requested_scope="team",
            authorized_by_question_id=str(question.id),
        )
        assert (outcome.sanitized_output or {})["reasons"] == ["question_not_yours"]

    async def test_an_answer_from_an_earlier_run_no_longer_authorises(
        self, session: AsyncSession, org: Org
    ) -> None:
        """What stops a question answered last month authorising a memory
        today."""
        await grant(session, org, org.me, "memory.propose")
        question = await self.answered_question(session, org, run_id=new_uuid7())
        outcome = await propose(
            session,
            org,
            org.me,
            run_id=new_uuid7(),
            content="Engineering deploys on Mondays at 9am PST.",
            requested_scope="team",
            authorized_by_question_id=str(question.id),
        )
        assert (outcome.sanitized_output or {})["reasons"] == ["question_not_this_run"]

    async def test_one_answer_is_worth_one_memory(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        run_id = new_uuid7()
        question = await self.answered_question(session, org, run_id=run_id)
        first = await propose(
            session,
            org,
            org.me,
            run_id=run_id,
            content="Engineering deploys on Mondays at 9am PST.",
            requested_scope="team",
            authorized_by_question_id=str(question.id),
        )
        assert (first.sanitized_output or {})["outcome"] == "activate"
        second = await propose(
            session,
            org,
            org.me,
            run_id=run_id,
            content="Engineering also freezes deploys in December.",
            requested_scope="team",
            authorized_by_question_id=str(question.id),
        )
        assert (second.sanitized_output or {})["reasons"] == ["grant_already_used"]
        assert len(list(await session.scalars(select(MemoryRecord)))) == 1

    async def test_a_refused_grant_never_falls_back_to_the_agents_own_memory(
        self, session: AsyncSession, org: Org
    ) -> None:
        """A downgrade would file a memory as the agent's own while the
        person believes it is company-wide. Nothing is saved instead."""
        await grant(session, org, org.me, "memory.propose")
        run_id = new_uuid7()
        question = await self.answered_question(session, org, run_id=run_id, granted_scope="")
        outcome = await propose(
            session,
            org,
            org.me,
            run_id=run_id,
            content="Engineering deploys on Mondays at 9am PST.",
            requested_scope="team",
            authorized_by_question_id=str(question.id),
        )
        assert (outcome.sanitized_output or {})["outcome"] == "reject"
        assert (await session.scalar(select(MemoryRecord))) is None

    async def test_proposing_without_a_question_is_unchanged(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me)
        output = outcome.sanitized_output or {}
        assert output["outcome"] == "activate"
        record = await session.scalar(select(MemoryRecord))
        assert record is not None and record.created_by_type == "agent"


async def cards(session: AsyncSession) -> list[Message]:
    """Every visible memory card in the workspace, oldest first."""
    rows = await session.scalars(select(Message).order_by(Message.created_at, Message.id))
    return [m for m in rows if m.content_json.get("kind") == "memory_saved"]


class TestMemorySavedCard:
    """The chat has to show that a memory was written, because the agent
    saying so is a claim and the two bugs that produced this feature were
    both a false one."""

    async def test_a_saved_memory_writes_one_card_the_person_can_read(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(
            session, org, org.me, content="We deploy on Mondays at 9am PST.", subject="deploy.day"
        )
        record_id = (outcome.sanitized_output or {})["memory_id"]
        written = await cards(session)
        assert len(written) == 1
        card = written[0]
        assert card.message_type == "status"
        assert card.visibility == "visible"
        assert card.sender_type == "agent"
        assert card.sender_id == org.me.id
        assert card.task_id == org.task.id
        assert card.content_json["memory_id"] == record_id
        assert card.content_json["action"] == "saved"
        assert card.content_json["scope"] == "agent"
        assert card.content_json["scope_label"] == "just you and me"
        assert card.content_json["content"] == "We deploy on Mondays at 9am PST."
        assert card.content_json["superseded"] == ""
        # Readable by a renderer that has never heard of this card.
        assert card.content_json["summary"] == "We deploy on Mondays at 9am PST."

    async def test_a_refused_proposal_writes_no_card(self, session: AsyncSession, org: Org) -> None:
        """The chat must never show "saved" for something that was not."""
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(session, org, org.me, requested_scope="team")
        assert (outcome.sanitized_output or {})["outcome"] == "reject"
        assert await cards(session) == []

    async def test_a_duplicate_writes_no_second_card(self, session: AsyncSession, org: Org) -> None:
        """Also the gateway-replay case: re-running the same call re-proposes
        content that is now an exact duplicate of itself."""
        await grant(session, org, org.me, "memory.propose")
        await propose(session, org, org.me)
        second = await propose(session, org, org.me)
        assert (second.sanitized_output or {})["outcome"] == "duplicate"
        assert len(await cards(session)) == 1

    async def test_two_proposals_in_one_run_get_one_card_each(
        self, session: AsyncSession, org: Org
    ) -> None:
        await grant(session, org, org.me, "memory.propose")
        run_id = new_uuid7()
        await propose(session, org, org.me, run_id=run_id, content="We deploy on Mondays.")
        await propose(session, org, org.me, run_id=run_id, content="Standup is at 10am.")
        written = await cards(session)
        assert [c.content_json["content"] for c in written] == [
            "We deploy on Mondays.",
            "Standup is at 10am.",
        ]

    async def test_a_correction_says_updated_and_shows_what_it_replaced(
        self, session: AsyncSession, org: Org
    ) -> None:
        """The bug this card exists for: a correction the agent acknowledged
        and never stored. When it IS stored, the card has to show both the new
        words and the ones they replaced."""
        await grant(session, org, org.me, "memory.propose")
        await propose(
            session, org, org.me, content="We deploy on Mondays at 9am.", subject="deploy.day"
        )
        await propose(
            session,
            org,
            org.me,
            content="We deploy on Mondays at 10am now, not 9am.",
            subject="deploy.day",
            confidence=0.95,
        )
        written = await cards(session)
        assert len(written) == 2
        latest = written[-1]
        assert latest.content_json["action"] == "updated"
        assert latest.content_json["content"] == "We deploy on Mondays at 10am now, not 9am."
        assert latest.content_json["superseded"] == "We deploy on Mondays at 9am."

    async def test_a_team_memory_names_the_real_team(self, session: AsyncSession, org: Org) -> None:
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(
            session, org, org.me, org.team_task, requested_scope="team", subject="deploy.day"
        )
        assert (outcome.sanitized_output or {})["outcome"] == "activate"
        card = (await cards(session))[0]
        assert card.content_json["scope"] == "team"
        assert card.content_json["scope_label"] == "the Engineering team"

    async def test_the_authorised_path_writes_the_card_too(
        self, session: AsyncSession, org: Org
    ) -> None:
        """A memory a person authorised through a question is the one they are
        most likely to have a wrong idea about, so it is the one that most
        needs the card."""
        await grant(session, org, org.me, "memory.propose")
        run_id = new_uuid7()
        person = User(
            email=f"v-{new_uuid7().hex[:8]}@example.com", display_name="Varand", password_hash="x"
        )
        session.add(person)
        await session.flush()
        question = await TestAnsweredScopeGrant.answered_question(
            session,
            org,
            run_id=run_id,
            granted_scope="workspace",
            granted_authority="workspace",
            answered_by=person,
        )
        outcome = await propose(
            session,
            org,
            org.me,
            run_id=run_id,
            content="We deploy on Mondays at 9am PST.",
            requested_scope="workspace",
            authorized_by_question_id=str(question.id),
        )
        assert (outcome.sanitized_output or {})["outcome"] == "activate"
        card = (await cards(session))[0]
        assert card.content_json["scope"] == "workspace"
        assert card.content_json["scope_label"] == "everyone in the workspace"

    async def test_the_scope_label_comes_from_the_stored_record(
        self, session: AsyncSession, org: Org
    ) -> None:
        """The label is written from the record's own scope, never from what
        the agent asked for: a card reading "the Engineering team" over an
        agent-scoped write is the mislabelling bug one surface along."""
        await grant(session, org, org.me, "memory.propose")
        outcome = await propose(
            session, org, org.me, content="We deploy on Mondays.", requested_scope="agent"
        )
        record = await session.get(
            MemoryRecord, UUID((outcome.sanitized_output or {})["memory_id"])
        )
        assert record is not None and record.scope == "agent"
        assert (await cards(session))[0].content_json["scope_label"] == "just you and me"
