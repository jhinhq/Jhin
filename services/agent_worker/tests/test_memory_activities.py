"""Memory maintenance activities against in-memory SQLite with a stub model
client: bounded visible-only sources, typed failures, deterministic policy
application, explicit-remember handling, and idempotent re-application."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.testing import ActivityEnvironment

from jhin_agent_worker import memory_activities as module
from jhin_agent_worker.memory_activities import MemoryActivities, load_source_text
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AuditEvent,
    Conversation,
    MemoryRecord,
    Message,
    ModelProfile,
    ModelProvider,
    Task,
    Team,
    Workspace,
)
from jhin_domain import (
    MemoryStatus,
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    new_uuid7,
)
from jhin_memory import embedding as embedding_module
from jhin_models import (
    EmbeddingResult,
    ModelClient,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from jhin_models.testing import deterministic_embedding
from jhin_observability import noop_metrics, noop_tracer
from jhin_workflows.memory_maintenance import (
    ApplyMemoryCandidatesInput,
    ExtractMemoryCandidatesInput,
)


class StubClient(ModelClient):
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(text=self.text, usage=ModelUsage(input_tokens=3, output_tokens=2))

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        raise NotImplementedError

    async def verify(self) -> str:
        return "ok"

    async def close(self) -> None:
        self.closed = True


class StubEmbeddingClient(StubClient):
    fail = False

    def __init__(self) -> None:
        super().__init__("")

    async def embed(
        self, texts: Sequence[str], *, model: str, dimensions: int | None = None
    ) -> EmbeddingResult:
        if StubEmbeddingClient.fail:
            raise ModelProviderError("stub: boom", status_code=500, retryable=True)
        dims = dimensions or 8
        return EmbeddingResult(
            vectors=tuple(tuple(deterministic_embedding(t, dimensions=dims)) for t in texts),
            model=model,
            dimensions=dims,
        )


EMBED_CONFIG = {"embeddings": {"enabled": True, "model": "fake-embed", "dimensions": 8}}


class StubResources:
    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory
        self.crypto = None
        self.runtime = SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())


class World:
    workspace: Workspace
    team: Team
    agent: Agent
    task: Task
    team_task: Task
    conversation: Conversation
    user_message: Message
    agent_message: Message
    internal_message: Message
    activities: MemoryActivities
    session_factory: Any


@pytest.fixture
async def world() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    w = World()
    w.session_factory = maker
    w.activities = MemoryActivities(StubResources(maker))  # type: ignore[arg-type]
    async with maker() as session:
        w.workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(w.workspace)
        await session.flush()
        ws = w.workspace.id
        provider = ModelProvider(
            workspace_id=ws,
            type="openai_compatible",
            display_name="fake",
            base_url="http://fake",
            enabled=True,
        )
        session.add(provider)
        await session.flush()
        profile = ModelProfile(
            workspace_id=ws,
            provider_id=provider.id,
            display_name="fake-mini",
            model_name="fake-mini",
        )
        session.add(profile)
        await session.flush()
        w.workspace.default_model_profile_id = profile.id
        w.team = Team(workspace_id=ws, name="Eng")
        session.add(w.team)
        await session.flush()
        w.agent = Agent(workspace_id=ws, name="Ava", slug="ava", team_id=w.team.id)
        session.add(w.agent)
        await session.flush()
        w.conversation = Conversation(
            workspace_id=ws,
            title="Chat",
            primary_agent_id=w.agent.id,
            last_activity_at=datetime.now(UTC),
        )
        session.add(w.conversation)
        await session.flush()
        w.task = Task(
            workspace_id=ws,
            title="Chat turn",
            state=TaskState.COMPLETED.value,
            assigned_agent_id=w.agent.id,
            conversation_id=w.conversation.id,
            correlation_id=new_uuid7(),
            metadata_json={"result": {"summary": "done"}},
        )
        w.team_task = Task(
            workspace_id=ws,
            title="Team task",
            state=TaskState.COMPLETED.value,
            assigned_agent_id=w.agent.id,
            assigned_team_id=w.team.id,
            correlation_id=new_uuid7(),
        )
        session.add_all([w.task, w.team_task])
        await session.flush()

        def msg(sender: SenderType, text: str, visibility: MessageVisibility) -> Message:
            return Message(
                workspace_id=ws,
                task_id=w.task.id,
                conversation_id=w.conversation.id,
                sender_type=sender.value,
                sender_id=w.agent.id if sender is SenderType.AGENT else None,
                recipient_type=RecipientType.TASK.value,
                recipient_id=w.task.id,
                message_type="text",
                content_json={"text": text},
                visibility=visibility.value,
            )

        w.user_message = msg(
            SenderType.USER, "Please remember I prefer concise updates.", MessageVisibility.VISIBLE
        )
        w.internal_message = msg(
            SenderType.AGENT,
            "hidden scratchpad sk-proj-abcdefghijklmnopqrstuvwxyz12",
            MessageVisibility.INTERNAL,
        )
        w.agent_message = msg(
            SenderType.AGENT, "Noted: concise updates from now on.", MessageVisibility.VISIBLE
        )
        session.add_all([w.user_message, w.internal_message, w.agent_message])
        await session.commit()
    yield w
    await engine.dispose()


VALID_OUTPUT = json.dumps(
    {
        "candidates": [
            {
                "content": "The user prefers concise updates.",
                "kind": "preference",
                "subject": "user.update_style",
            },
        ]
    }
)


def extract_input(w: World, **overrides: Any) -> ExtractMemoryCandidatesInput:
    values: dict[str, Any] = {
        "workspace_id": str(w.workspace.id),
        "agent_id": str(w.agent.id),
        "source_kind": "message",
        "source_id": str(w.agent_message.id),
        "task_id": str(w.task.id),
        "conversation_id": str(w.conversation.id),
    }
    values.update(overrides)
    return ExtractMemoryCandidatesInput(**values)


def apply_input(w: World, **overrides: Any) -> ApplyMemoryCandidatesInput:
    values: dict[str, Any] = {
        "workspace_id": str(w.workspace.id),
        "agent_id": str(w.agent.id),
        "source_kind": "message",
        "source_id": str(w.agent_message.id),
        "candidates_json": [{"content": "The user prefers concise updates.", "kind": "preference"}],
        "task_id": str(w.task.id),
        "conversation_id": str(w.conversation.id),
        "idempotency_key": "memory-maintenance-message-x",
    }
    values.update(overrides)
    return ApplyMemoryCandidatesInput(**values)


class TestLoadSource:
    async def test_message_source_is_visible_only_and_bounded(self, world: World) -> None:
        async with world.session_factory() as session:
            loaded = await load_source_text(
                session,
                workspace_id=world.workspace.id,
                agent_id=world.agent.id,
                source_kind="message",
                source_id=world.agent_message.id,
            )
        assert loaded is not None
        text, refs = loaded
        assert "user: Please remember" in text
        assert "assistant: Noted" in text
        assert "scratchpad" not in text
        assert "sk-proj" not in text
        assert refs["task_id"] == str(world.task.id)

    async def test_internal_message_is_not_a_source(self, world: World) -> None:
        async with world.session_factory() as session:
            assert (
                await load_source_text(
                    session,
                    workspace_id=world.workspace.id,
                    agent_id=world.agent.id,
                    source_kind="message",
                    source_id=world.internal_message.id,
                )
            ) is None

    async def test_task_outcome_source(self, world: World) -> None:
        async with world.session_factory() as session:
            loaded = await load_source_text(
                session,
                workspace_id=world.workspace.id,
                agent_id=world.agent.id,
                source_kind="task_outcome",
                source_id=world.task.id,
            )
        assert loaded is not None
        text, _ = loaded
        assert text.startswith("task: Chat turn")
        assert '"summary": "done"' in text

    async def test_unknown_kind_or_missing_source(self, world: World) -> None:
        async with world.session_factory() as session:
            assert (
                await load_source_text(
                    session,
                    workspace_id=world.workspace.id,
                    agent_id=world.agent.id,
                    source_kind="transcript",
                    source_id=world.task.id,
                )
            ) is None
            assert (
                await load_source_text(
                    session,
                    workspace_id=world.workspace.id,
                    agent_id=world.agent.id,
                    source_kind="message",
                    source_id=new_uuid7(),
                )
            ) is None


class TestExtract:
    async def test_success(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        client = StubClient(VALID_OUTPUT)
        monkeypatch.setattr(module, "build_model_client", lambda *a, **k: client)
        result = await ActivityEnvironment().run(
            world.activities.extract_memory_candidates_activity, extract_input(world)
        )
        assert result.ok
        assert result.model == "fake-mini"
        assert result.candidates_json[0]["content"] == "The user prefers concise updates."
        assert result.input_tokens == 3
        assert client.closed
        assert client.requests[0].messages[0].role == "system"

    async def test_malformed_output_is_typed(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            module, "build_model_client", lambda *a, **k: StubClient("[fake-mini] Completed")
        )
        result = await ActivityEnvironment().run(
            world.activities.extract_memory_candidates_activity, extract_input(world)
        )
        assert not result.ok
        assert result.error.startswith("malformed_output")

    async def test_internal_source_is_refused(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(module, "build_model_client", lambda *a, **k: StubClient(VALID_OUTPUT))
        result = await ActivityEnvironment().run(
            world.activities.extract_memory_candidates_activity,
            extract_input(world, source_id=str(world.internal_message.id)),
        )
        assert not result.ok
        assert result.error == "source_not_visible"

    async def test_missing_model_profile_is_typed(self, world: World) -> None:
        async with world.session_factory() as session:
            ws = await session.get(Workspace, world.workspace.id)
            assert ws is not None
            ws.default_model_profile_id = None
            await session.commit()
        result = await ActivityEnvironment().run(
            world.activities.extract_memory_candidates_activity, extract_input(world)
        )
        assert not result.ok
        assert result.error == "snapshot:no_model_profile"


class TestApply:
    async def test_ordinary_turn_activates_private_memory(self, world: World) -> None:
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity, apply_input(world)
        )
        assert result.ok
        assert result.activated == 1
        async with world.session_factory() as session:
            record = await session.scalar(select(MemoryRecord))
            assert record is not None
            assert record.scope == "agent"
            assert record.scope_id == world.agent.id
            assert record.status == MemoryStatus.ACTIVE.value
            assert record.source_message_id == world.agent_message.id
            assert record.source_conversation_id == world.conversation.id
            assert record.created_by_type == "agent"
            audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.action == "memory.maintained")
            )
            assert audit is not None
            assert audit.metadata_json["activated"] == 1
            assert "concise" not in str(audit.metadata_json)

    async def test_reapplication_is_idempotent(self, world: World) -> None:
        env = ActivityEnvironment()
        first = await env.run(world.activities.apply_memory_candidates_activity, apply_input(world))
        second = await env.run(
            world.activities.apply_memory_candidates_activity, apply_input(world)
        )
        assert first.activated == 1
        assert second.activated == 0 and second.duplicates == 1
        async with world.session_factory() as session:
            assert len(list(await session.scalars(select(MemoryRecord)))) == 1

    async def test_model_scope_cannot_amplify(self, world: World) -> None:
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity,
            apply_input(
                world,
                candidates_json=[{"content": "Company-wide rule.", "requested_scope": "workspace"}],
            ),
        )
        assert result.ok
        assert result.rejected == 1 and result.activated == 0
        assert "non_amplification" in result.reasons

    async def test_team_task_outcome_activates_team_memory(self, world: World) -> None:
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity,
            apply_input(
                world,
                source_kind="task_outcome",
                source_id=str(world.team_task.id),
                task_id="",
                conversation_id="",
                candidates_json=[{"content": "Eng deploys Tuesdays.", "requested_scope": "team"}],
            ),
        )
        assert result.activated == 1
        async with world.session_factory() as session:
            record = await session.scalar(select(MemoryRecord))
            assert record is not None
            assert record.scope == "team" and record.scope_id == world.team.id
            assert record.source_task_id == world.team_task.id

    async def test_explicit_remember_uses_user_scope_and_authority(self, world: World) -> None:
        user_id = new_uuid7()
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity,
            apply_input(
                world,
                source_id=str(world.user_message.id),
                remember_enabled=True,
                requested_scope="workspace",
                actor_user_id=str(user_id),
                actor_authority="workspace",
            ),
        )
        assert result.activated == 1
        async with world.session_factory() as session:
            record = await session.scalar(select(MemoryRecord))
            assert record is not None
            assert record.scope == "workspace"
            assert record.created_by_type == "user"
            assert record.created_by_id == user_id

    async def test_explicit_remember_without_authority_is_rejected(self, world: World) -> None:
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity,
            apply_input(
                world,
                remember_enabled=True,
                requested_scope="workspace",
                actor_user_id=str(new_uuid7()),
                actor_authority="agent",
            ),
        )
        assert result.rejected == 1
        assert "insufficient_authority" in result.reasons

    async def test_secret_candidate_is_rejected(self, world: World) -> None:
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity,
            apply_input(
                world,
                candidates_json=[{"content": "Token ghp_abcdefghijklmnopqrstuvwxyz0123456789"}],
            ),
        )
        assert result.rejected == 1
        async with world.session_factory() as session:
            assert (await session.scalar(select(MemoryRecord))) is None

    async def test_invalid_candidates_and_missing_source_are_typed(self, world: World) -> None:
        env = ActivityEnvironment()
        bad = await env.run(
            world.activities.apply_memory_candidates_activity,
            apply_input(world, candidates_json=[{"content": "x", "status": "active"}]),
        )
        assert not bad.ok and bad.error.startswith("invalid_candidates")
        missing = await env.run(
            world.activities.apply_memory_candidates_activity,
            apply_input(world, source_id=str(new_uuid7())),
        )
        assert not missing.ok and missing.error == "source_not_found"


class TestApplyEmbeds:
    async def _enable_embeddings(self, world: World) -> None:
        async with world.session_factory() as session:
            profile = await session.scalar(
                select(ModelProfile).where(ModelProfile.workspace_id == world.workspace.id)
            )
            assert profile is not None
            profile.config_json = EMBED_CONFIG
            await session.commit()

    async def test_created_records_are_embedded_best_effort(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._enable_embeddings(world)
        monkeypatch.setattr(
            embedding_module, "build_model_client", lambda *a, **k: StubEmbeddingClient()
        )
        StubEmbeddingClient.fail = False
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity, apply_input(world)
        )
        assert result.ok and result.activated == 1
        async with world.session_factory() as session:
            record = await session.get(MemoryRecord, UUID(result.created_ids[0]))
            assert record is not None
            assert record.embedding_model == "fake-embed"
            assert record.embedding_dimensions == 8
            audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.action == "memory.maintained")
            )
            assert audit is not None and audit.metadata_json["embedded"] == 1

    async def test_embedding_failure_keeps_the_record(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._enable_embeddings(world)
        monkeypatch.setattr(
            embedding_module, "build_model_client", lambda *a, **k: StubEmbeddingClient()
        )
        StubEmbeddingClient.fail = True
        try:
            result = await ActivityEnvironment().run(
                world.activities.apply_memory_candidates_activity, apply_input(world)
            )
        finally:
            StubEmbeddingClient.fail = False
        assert result.ok and result.activated == 1
        async with world.session_factory() as session:
            record = await session.get(MemoryRecord, UUID(result.created_ids[0]))
            assert record is not None
            assert record.status == MemoryStatus.ACTIVE.value
            assert record.embedding_json is None

    async def test_without_embedding_profile_nothing_is_embedded(self, world: World) -> None:
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity, apply_input(world)
        )
        assert result.ok
        async with world.session_factory() as session:
            record = await session.get(MemoryRecord, UUID(result.created_ids[0]))
            assert record is not None and record.embedding_json is None


class TestExtractionContext:
    async def test_known_memories_are_passed_to_the_model(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with world.session_factory() as session:
            session.add(
                MemoryRecord(
                    workspace_id=world.workspace.id,
                    scope="agent",
                    scope_id=world.agent.id,
                    kind="fact",
                    content="Known fact about deploys.",
                    content_hash="x",
                    visibility="agent",
                    status=MemoryStatus.ACTIVE.value,
                    created_by_type="agent",
                )
            )
            await session.commit()
        client = StubClient(VALID_OUTPUT)
        monkeypatch.setattr(module, "build_model_client", lambda *a, **k: client)
        result = await ActivityEnvironment().run(
            world.activities.extract_memory_candidates_activity, extract_input(world)
        )
        assert result.ok
        user_prompt = client.requests[0].messages[1].content
        assert "<known_memories>" in user_prompt
        assert "Known fact about deploys." in user_prompt


class TestQualityScreening:
    async def test_agent_self_identity_candidate_is_rejected(self, world: World) -> None:
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity,
            apply_input(world, candidates_json=[{"content": "The AI teammate's name is Ava."}]),
        )
        assert result.ok
        assert result.rejected == 1 and result.activated == 0
        assert "self_reference" in result.reasons
        async with world.session_factory() as session:
            assert (await session.scalar(select(MemoryRecord))) is None

    async def test_paraphrases_collapse_to_one_active_record(self, world: World) -> None:
        env = ActivityEnvironment()
        for content in (
            "We deploy every other Thursday.",
            "The release day is every other Thursday.",
            "Release day is every other Thursday.",
        ):
            await env.run(
                world.activities.apply_memory_candidates_activity,
                apply_input(world, candidates_json=[{"content": content, "subject": "deploy.day"}]),
            )
        async with world.session_factory() as session:
            rows = list(await session.scalars(select(MemoryRecord)))
            active = [r for r in rows if r.status == MemoryStatus.ACTIVE.value]
            assert len(active) == 1
            assert active[0].policy_json.get("confirmations") == 1
            assert all(
                r.status == MemoryStatus.SUPERSEDED.value for r in rows if r.id != active[0].id
            )


class TestAgentToAgentLearning:
    async def _make_delegation(self, world: World, *, same_team: bool) -> Task:
        async with world.session_factory() as session:
            boss = Agent(
                workspace_id=world.workspace.id,
                name="CTO",
                slug=f"cto-{new_uuid7().hex[:6]}",
                team_id=world.team.id if same_team else None,
            )
            session.add(boss)
            await session.flush()
            parent = Task(
                workspace_id=world.workspace.id,
                title="Parent",
                state=TaskState.RUNNING.value,
                assigned_agent_id=boss.id,
                correlation_id=new_uuid7(),
            )
            session.add(parent)
            await session.flush()
            child = Task(
                workspace_id=world.workspace.id,
                title="Write the deploy docs",
                description="Document the deploy pipeline.",
                state=TaskState.COMPLETED.value,
                assigned_agent_id=world.agent.id,
                parent_task_id=parent.id,
                correlation_id=parent.correlation_id,
                metadata_json={
                    "origin": "delegation",
                    "delegation": {
                        "kind": "delegation",
                        "blocking": True,
                        "delegated_by_agent_id": str(boss.id),
                        "delegated_by_agent_name": "CTO",
                        "parent_task_id": str(parent.id),
                        "expected_output": "A docs page",
                    },
                    "reported_result": {"summary": "Docs written.", "status": "completed"},
                },
            )
            session.add(child)
            await session.flush()
            session.add(
                Message(
                    workspace_id=world.workspace.id,
                    task_id=parent.id,
                    sender_type=SenderType.AGENT.value,
                    sender_id=world.agent.id,
                    recipient_type=RecipientType.AGENT.value,
                    recipient_id=boss.id,
                    message_type="review_result",
                    content_json={
                        "summary": "Looks good.",
                        "child_task_id": str(child.id),
                        "verdict": "pass",
                    },
                    visibility=MessageVisibility.VISIBLE.value,
                )
            )
            await session.commit()
            return child

    async def test_child_task_source_includes_the_structured_exchange(self, world: World) -> None:
        child = await self._make_delegation(world, same_team=True)
        async with world.session_factory() as session:
            loaded = await load_source_text(
                session,
                workspace_id=world.workspace.id,
                agent_id=world.agent.id,
                source_kind="task_outcome",
                source_id=child.id,
            )
        assert loaded is not None
        text, _refs = loaded
        assert "delegation from CTO (delegation)" in text
        assert "expected output: A docs page" in text
        assert "description: Document the deploy pipeline." in text
        assert '"summary": "Docs written."' in text
        assert "review feedback: Looks good. (verdict: pass)" in text

    async def test_same_team_delegation_may_activate_team_memory(self, world: World) -> None:
        child = await self._make_delegation(world, same_team=True)
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity,
            apply_input(
                world,
                source_kind="task_outcome",
                source_id=str(child.id),
                task_id="",
                conversation_id="",
                candidates_json=[
                    {"content": "Deploy docs live in the wiki.", "requested_scope": "team"}
                ],
            ),
        )
        assert result.activated == 1
        async with world.session_factory() as session:
            record = await session.scalar(select(MemoryRecord))
            assert record is not None
            assert record.scope == "team" and record.scope_id == world.team.id

    async def test_cross_team_delegation_stays_agent_private(self, world: World) -> None:
        child = await self._make_delegation(world, same_team=False)
        result = await ActivityEnvironment().run(
            world.activities.apply_memory_candidates_activity,
            apply_input(
                world,
                source_kind="task_outcome",
                source_id=str(child.id),
                task_id="",
                conversation_id="",
                candidates_json=[
                    {"content": "Deploy docs live in the wiki.", "requested_scope": "team"}
                ],
            ),
        )
        assert result.rejected == 1
        assert "non_amplification" in result.reasons
