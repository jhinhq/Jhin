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
    AgentRun,
    MemoryRecord,
    Message,
    Task,
    Team,
    ToolCall,
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
        created_by_type="user",
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
    # These tests exercise scope/duplicates/cards for a fact the human stated.
    # Unsupported model-only claims have a separate regression below.
    source_task = task or org.task
    session.add(
        Message(
            workspace_id=org.workspace.id,
            task_id=source_task.id,
            conversation_id=source_task.conversation_id,
            sender_type="user",
            recipient_type="task",
            recipient_id=source_task.id,
            message_type="text",
            visibility="visible",
            content_json={"text": payload["content"]},
        )
    )
    await session.flush()
    return await org.gateway(session, agent, task, run_id).request(
        "memory.propose", json.dumps(payload)
    )


class TestSearch:
    async def test_standing_capture_saves_without_question_and_reports_exact_team(
        self, session, org
    ):
        from jhin_db.models import WorkspaceMembership
        from jhin_db.models.memory_capture import MemoryCapturePolicy

        await grant(session, org, org.me, "memory.propose")
        await grant(session, org, org.me, "memory.read")
        now = datetime.now(UTC)
        owner = User(
            email=f"{new_uuid7()}@example.test", display_name="Owner", password_hash="unused"
        )
        session.add(owner)
        await session.flush()
        source = Message(
            workspace_id=org.workspace.id,
            task_id=org.task.id,
            sender_type="user",
            sender_id=owner.id,
            recipient_type="agent",
            recipient_id=org.me.id,
            message_type="text",
            visibility="visible",
            created_at=now,
            content_json={"text": "Our blog uses friendly, practical language."},
        )
        session.add_all(
            [
                source,
                WorkspaceMembership(workspace_id=org.workspace.id, user_id=owner.id, role="owner"),
                MemoryCapturePolicy(
                    workspace_id=org.workspace.id,
                    scope="team",
                    scope_id=org.team.id,
                    granted_by_user_id=owner.id,
                    source_user_id=owner.id,
                    actor_ids_json=[str(org.me.id)],
                    allowed_classes_json=["editorial_style"],
                    effective_from=now - timedelta(seconds=5),
                ),
            ]
        )
        await session.flush()
        available = await search(session, org, org.me, "blog")
        assert available.sanitized_output["capture_policies"][0]["scope_id"] == str(org.team.id)
        result = await org.gateway(session, org.me).request(
            "memory.propose",
            json.dumps(
                {
                    "content": source.content_json["text"],
                    "requested_scope": "team",
                    "scope_id": str(org.team.id),
                    "source_message_id": str(source.id),
                    "capture_class": "editorial_style",
                }
            ),
        )
        assert result.status == "executed"
        assert result.sanitized_output["status"] == "active"
        record = await session.get(MemoryRecord, UUID(result.sanitized_output["memory_id"]))
        assert record.scope_id == org.team.id
        assert record.source_message_id == source.id
        assert await session.scalar(select(UserQuestion)) is None

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
    @pytest.mark.parametrize(
        "unsupported",
        [
            "Blog articles go live Mondays at 9:00 AM Pacific Time (America/Los_Angeles) "
            "and Ashley approves the exact draft before publication.",
            "Marketing team posts blog articles on Mondays at 9am PST (America/Los_Angeles).",
        ],
    )
    async def test_posting_preference_recovery_keeps_exact_human_words(
        self, session, org, unsupported
    ):
        await grant(session, org, org.me, "memory.propose")
        stated = "We post blogs on 9am PST on Mondays"
        message = Message(
            workspace_id=org.workspace.id,
            task_id=org.task.id,
            sender_type="user",
            recipient_type="task",
            recipient_id=org.task.id,
            message_type="text",
            visibility="visible",
            content_json={"text": stated},
        )
        session.add(message)
        await session.flush()
        gateway = org.gateway(session, org.me)
        result = await gateway.request("memory.propose", json.dumps({"content": unsupported}))
        output = result.sanitized_output
        assert output["reasons"] == ["unsupported_claim"]
        assert await session.scalar(select(MemoryRecord)) is None
        suggestions = output["suggested_proposals"]
        assert len(suggestions) == 1
        assert suggestions[0]["source_message_id"] == str(message.id)
        assert not suggestions[0]["source_tool_call_id"]
        assert suggestions[0]["arguments"]["content"] == stated
        assert suggestions[0]["arguments"]["requested_scope"] == "agent"
        assert "Ashley" not in json.dumps(suggestions)
        saved = await gateway.request("memory.propose", json.dumps(suggestions[0]["arguments"]))
        assert saved.sanitized_output["outcome"] == "activate"
        record = await session.get(MemoryRecord, UUID(saved.sanitized_output["memory_id"]))
        assert record.content == stated and record.scope == "agent"
        assert record.policy_json["evidence"]["kind"] == "human_statement"
        assert record.policy_json["evidence"]["message_id"] == str(message.id)

    @pytest.mark.parametrize(
        "excluded",
        [
            "assistant",
            "internal",
            "other_task",
            "other_workspace",
            "secret",
            "password",
            "identity",
            "large",
        ],
    )
    async def test_human_recovery_excludes_foreign_hidden_secret_or_oversized_messages(
        self, session, org, excluded
    ):
        await grant(session, org, org.me, "memory.propose")
        text = "We post blogs on 9am PST on Mondays"
        if excluded == "secret":
            text += "; Ghost API key: " + "a" * 24 + ":" + "b" * 64
        if excluded == "password":
            text += "; password: do-not-return-this"
        if excluded == "identity":
            text = "Your name is Mindy and you belong to Marketing"
        if excluded == "large":
            text += "; exact conditions " + "x" * 2000
        session.add(
            Message(
                workspace_id=new_uuid7() if excluded == "other_workspace" else org.workspace.id,
                task_id=org.team_task.id if excluded == "other_task" else org.task.id,
                sender_type="agent" if excluded == "assistant" else "user",
                recipient_type="task",
                recipient_id=org.task.id,
                message_type="text",
                visibility="internal" if excluded == "internal" else "visible",
                content_json={"text": text},
            )
        )
        await session.flush()
        result = await org.gateway(session, org.me).request(
            "memory.propose", json.dumps({"content": "Blog publishing is ready every Monday."})
        )
        assert result.sanitized_output["suggested_proposals"] == []
        assert text not in json.dumps(result.sanitized_output)

    async def test_human_recovery_respects_snapshot_and_output_bounds(self, session, org):
        from jhin_memory.evidence import human_statement_excerpts
        from jhin_memory.types import SourceFacts, SourceRef

        cutoff = datetime(2031, 1, 1, tzinfo=UTC)
        messages = [
            Message(
                workspace_id=org.workspace.id,
                task_id=org.task.id,
                sender_type="user",
                recipient_type="task",
                recipient_id=org.task.id,
                message_type="text",
                visibility="visible",
                content_json={"text": f"Our weekly report uses format {index}."},
                created_at=cutoff - timedelta(minutes=index),
            )
            for index in range(6)
        ]
        session.add_all(messages)
        session.add(
            Message(
                workspace_id=org.workspace.id,
                task_id=org.task.id,
                sender_type="user",
                recipient_type="task",
                recipient_id=org.task.id,
                message_type="text",
                visibility="visible",
                content_json={"text": "Later input must not leak into an earlier snapshot."},
                created_at=cutoff + timedelta(minutes=1),
            )
        )
        await session.flush()
        source = SourceFacts(
            workspace_id=org.workspace.id,
            agent_id=org.me.id,
            ref=SourceRef(task_id=org.task.id, message_id=messages[0].id),
        )
        excerpts = await human_statement_excerpts(session, source, limit=100)
        assert [item.message_id for item in excerpts] == [str(row.id) for row in messages[:4]]
        assert await human_statement_excerpts(session, source, limit=0) == []
        assert (
            await human_statement_excerpts(session, source.model_copy(update={"internal": True}))
            == []
        )
        assert (
            await human_statement_excerpts(
                session, source.model_copy(update={"workspace_id": new_uuid7()})
            )
            == []
        )

    async def test_verified_recovery_facts_respect_snapshot_cutoff_and_bounds(self, session, org):
        from jhin_memory.evidence import verified_tool_facts
        from jhin_memory.types import SourceFacts, SourceRef

        cutoff = datetime(2031, 1, 1, tzinfo=UTC)
        message = Message(
            workspace_id=org.workspace.id,
            task_id=org.task.id,
            sender_type="user",
            recipient_type="task",
            recipient_id=org.task.id,
            content_json={"text": "Save the verified setup details."},
            created_at=cutoff,
        )
        run = AgentRun(workspace_id=org.workspace.id, agent_id=org.me.id, task_id=org.task.id)
        session.add_all([message, run])
        await session.flush()
        facts = [
            f"Verified Ghost installation URL: https://site-{index}.example" for index in range(10)
        ]
        for future in (False, True):
            session.add(
                ToolCall(
                    workspace_id=org.workspace.id,
                    run_id=run.id,
                    agent_id=org.me.id,
                    tool_name="ghost.connection.bind",
                    status="completed",
                    created_at=cutoff - timedelta(minutes=2),
                    completed_at=cutoff + timedelta(minutes=1)
                    if future
                    else cutoff - timedelta(minutes=1),
                    sanitized_output_json={
                        "verified_memory_facts": ["Future Ghost URL: https://future.example"]
                        if future
                        else ["Oversized " + "x" * 2000, "secure_input:" + str(new_uuid7()), *facts]
                    },
                )
            )
        await session.flush()
        source = SourceFacts(
            workspace_id=org.workspace.id,
            agent_id=org.me.id,
            ref=SourceRef(task_id=org.task.id, message_id=message.id),
        )
        result = await verified_tool_facts(session, source)
        assert [fact.content for fact in result] == facts[:8]
        assert (
            await verified_tool_facts(session, source.model_copy(update={"internal": True})) == []
        )
        assert (
            await verified_tool_facts(
                session, source.model_copy(update={"workspace_id": new_uuid7()})
            )
            == []
        )

    async def test_unsupported_compound_can_recover_with_exact_verified_fact(self, session, org):
        await grant(session, org, org.me, "memory.propose")
        run = AgentRun(workspace_id=org.workspace.id, agent_id=org.me.id, task_id=org.task.id)
        session.add(run)
        await session.flush()
        facts = [
            "Ghost Admin URL: https://confirmed.example/blog",
            f"Ghost connection: {new_uuid7()}",
        ]
        call = ToolCall(
            workspace_id=org.workspace.id,
            run_id=run.id,
            agent_id=org.me.id,
            tool_name="ghost.connection.bind",
            status="completed",
            sanitized_output_json={"verified_memory_facts": facts, "publisher_agent_id": None},
        )
        session.add(call)
        await session.flush()
        gateway = org.gateway(session, org.me, run_id=run.id)
        result = await gateway.request(
            "memory.propose",
            json.dumps({"content": "Ghost CMS is connected and the director can publish drafts."}),
        )
        output = result.sanitized_output
        assert output["outcome"] == "reject" and output["reasons"] == ["unsupported_claim"]
        assert await session.scalar(select(MemoryRecord)) is None
        suggestions = output["suggested_proposals"]
        assert [item["arguments"]["content"] for item in suggestions] == facts
        assert all(item["source_tool_call_id"] == str(call.id) for item in suggestions)
        assert "one" in output["detail"] and "verified" in output["detail"]
        saved = await gateway.request("memory.propose", json.dumps(suggestions[0]["arguments"]))
        assert saved.sanitized_output["outcome"] == "activate"
        record = await session.get(MemoryRecord, UUID(saved.sanitized_output["memory_id"]))
        assert record.content == facts[0] and record.scope == "agent"
        assert record.policy_json["evidence"]["kind"] == "verified_tool_fact"
        assert record.policy_json["evidence"]["tool_call_id"] == str(call.id)
        assert "publish" not in json.dumps(suggestions)

    @pytest.mark.parametrize("excluded", ["failed", "executing", "cli", "other_task", "secret"])
    async def test_recovery_never_suggests_unverified_foreign_or_secret_facts(
        self, session, org, excluded
    ):
        await grant(session, org, org.me, "memory.propose")
        run = AgentRun(
            workspace_id=org.workspace.id,
            agent_id=org.me.id,
            task_id=org.team_task.id if excluded == "other_task" else org.task.id,
        )
        session.add(run)
        await session.flush()
        fact = (
            "Ghost Admin key: " + "a" * 24 + ":" + "b" * 64
            if excluded == "secret"
            else "Ghost Admin URL: https://unsupported.example"
        )
        session.add(
            ToolCall(
                workspace_id=org.workspace.id,
                run_id=run.id,
                agent_id=org.me.id,
                tool_name="cli.command.execute" if excluded == "cli" else "ghost.connection.bind",
                status=excluded if excluded in {"failed", "executing"} else "completed",
                sanitized_output_json={"verified_memory_facts": [fact]},
            )
        )
        await session.flush()
        result = await org.gateway(session, org.me).request(
            "memory.propose", json.dumps({"content": "Ghost CMS is configured for publication."})
        )
        assert result.sanitized_output["suggested_proposals"] == []
        assert fact not in json.dumps(result.sanitized_output)

    @pytest.mark.parametrize(
        "tool_status,tool_name,expected",
        [
            ("completed", "ghost.connection.bind", "activate"),
            ("failed", "ghost.connection.bind", "reject"),
            ("completed", "cli.command.execute", "reject"),
        ],
    )
    async def test_native_fact_requires_completed_supported_tool(
        self, session, org, tool_status, tool_name, expected
    ):
        await grant(session, org, org.me, "memory.propose")
        run = AgentRun(workspace_id=org.workspace.id, agent_id=org.me.id, task_id=org.task.id)
        session.add(run)
        await session.flush()
        text = "Ghost Admin origin is https://confirmed.example"
        call = ToolCall(
            workspace_id=org.workspace.id,
            run_id=run.id,
            agent_id=org.me.id,
            tool_name=tool_name,
            status=tool_status,
            sanitized_output_json={"verified_memory_facts": [text]},
        )
        session.add(call)
        await session.flush()
        outcome = await org.gateway(session, org.me, run_id=run.id).request(
            "memory.propose", json.dumps({"content": text, "kind": "fact"})
        )
        assert outcome.sanitized_output["outcome"] == expected, outcome
        if expected == "activate":
            row = await session.get(MemoryRecord, UUID(outcome.sanitized_output["memory_id"]))
            assert row.policy_json["evidence"]["tool_call_id"] == str(call.id)

    async def test_assistant_claim_cannot_establish_successful_setup(self, session, org):
        await grant(session, org, org.me, "memory.propose")
        text = "Ghost is connected and automatically publishing daily."
        session.add(
            Message(
                workspace_id=org.workspace.id,
                task_id=org.task.id,
                sender_type="agent",
                sender_id=org.me.id,
                recipient_type="task",
                recipient_id=org.task.id,
                message_type="text",
                visibility="visible",
                content_json={"text": text},
            )
        )
        await session.flush()
        outcome = await org.gateway(session, org.me).request(
            "memory.propose", json.dumps({"content": text})
        )
        assert outcome.sanitized_output["outcome"] == "reject"
        assert outcome.sanitized_output["reasons"] == ["unsupported_claim"]
        assert await session.scalar(select(MemoryRecord)) is None

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
    @pytest.mark.parametrize("supersedes", [False, True])
    async def test_new_card_does_not_copy_raw_legacy_credential(
        self, session: AsyncSession, org: Org, supersedes: bool
    ) -> None:
        from jhin_memory import SourceFacts
        from jhin_tools.memory import _write_memory_card

        key = "a" * 24 + ":" + "b" * 64
        original = f"Ghost setup used {key}; drafts need review."
        previous = await seed(session, org, original, scope=MemoryScope.AGENT, scope_id=org.me.id)
        current = await seed(
            session,
            org,
            "Ghost drafts need director review.",
            scope=MemoryScope.AGENT,
            scope_id=org.me.id,
        )
        current.subject = previous.subject = "ghost.review"
        if supersedes:
            previous.status = "superseded"
            current.supersedes_id = previous.id
        await session.flush()
        ctx = ToolExecutionContext(
            session=session,
            workspace_id=org.workspace.id,
            task_id=org.task.id,
            run_id=new_uuid7(),
            agent_id=org.me.id,
            agent_name=org.me.name,
        )
        await _write_memory_card(
            ctx, current, SourceFacts(workspace_id=org.workspace.id, agent_id=org.me.id)
        )
        card = (await cards(session))[0]
        payload = json.dumps(card.content_json)
        assert key not in payload and "b" * 64 not in payload
        assert "REDACTED legacy credential" in payload
        assert card.content_json["memory_id"] == str(current.id)
        assert card.content_json["content"] == current.content
        await session.refresh(previous)
        assert previous.content == original

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
