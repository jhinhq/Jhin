"""The persona block reaches the model through the real reasoning activity,
with the register facet chosen by who is on the other side.

The pure rendering is covered in packages/agents; this is the seam that
matters in production: the activity reads the card off the frozen snapshot,
asks the situation module who the counterpart is, and composes the block
into the system prompt the provider actually receives. A person on a chat
turn reads "With people"; the requesting agent on a delegated child reads
"With teammates"; a trigger-started task, with nobody there, reads neither.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import jhin_agent_worker.reasoning as reasoning_module
from jhin_agent_worker.reasoning import AgentReasoningActivities
from jhin_agents.context import PERSONA_GUARDRAIL
from jhin_agents.platform_prompt import render_platform_preamble
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentRun,
    Conversation,
    Message,
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
from jhin_personas import PersonaCard, PersonaFacets
from jhin_workflows.agent_task.shared import ReasonAgentStepInput

T0 = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)

CARD = PersonaCard(
    name="the-skeptic",
    display_name="The Skeptic",
    description="Checks the claim before it becomes the plan.",
    tags=["professional", "review"],
    facets=PersonaFacets(
        voice="Dry, precise, quietly friendly.",
        stance="Separates what is known from what is assumed.",
        pace="Short by default.",
        when_unsure="Names the assumption, then asks one bounded question.",
        with_people="Warm and plain. Leads with the answer.",
        with_teammates="Terse and structured: claim, evidence, gap.",
        signature="Closes with 'Assumes:' when an answer rests on a guess.",
        never=["Hedge every sentence"],
    ),
)


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


def _reply(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        finish_reason="stop",
        model="persona-test",
        usage=ModelUsage(input_tokens=10, output_tokens=4, cached_tokens=0),
        latency_ms=3,
        provider_request_id=f"req-{text[:8]}",
        tool_calls=(),
    )


class PersonaWorld:
    sessions: async_sessionmaker[AsyncSession]
    reasoning: AgentReasoningActivities
    model: _Model
    workspace: Workspace
    agent: Agent
    manager: Agent
    user_id: UUID
    conversation: Conversation

    def snapshot(self, persona: PersonaCard | None) -> AgentExecutionSnapshot:
        return AgentExecutionSnapshot(
            agent_id=self.agent.id,
            workspace_id=self.workspace.id,
            workspace_name=self.workspace.name,
            name=self.agent.name,
            role_title="QA Engineer",
            system_prompt="You test everything twice.",
            autonomy_level="balanced",
            team_id=None,
            team_name=None,
            manager_agent_id=self.manager.id,
            manager_name=self.manager.name,
            model_profile=ModelProfileSnapshot(
                profile_id=new_uuid7(),
                provider_id=new_uuid7(),
                provider_type="persona-test",
                base_url=None,
                secret_id=None,
                model_name="persona-test",
                display_name="Persona test",
                input_cost_micros_per_million=1_000_000,
                output_cost_micros_per_million=1_000_000,
            ),
            temperature=None,
            max_output_tokens=None,
            run_limits=RunLimits(max_steps=5, max_run_minutes=5),
            persona=persona,
        )

    async def system_prompt(self, task: Task, *, persona: PersonaCard | None = CARD) -> str:
        """Run one reasoning step and return the system prompt the model
        client was handed."""
        async with self.sessions() as session:
            run = AgentRun(
                workspace_id=self.workspace.id,
                agent_id=self.agent.id,
                task_id=task.id,
                status=RunStatus.RUNNING.value,
            )
            session.add(run)
            await session.commit()
        self.model.responses.append(_reply("Done."))
        before = len(self.model.requests)
        await self.reasoning.reason_agent_step_activity(
            ReasonAgentStepInput(
                workspace_id=str(self.workspace.id),
                task_id=str(task.id),
                run_id=str(run.id),
                agent_id=str(self.agent.id),
                snapshot_json=self.snapshot(persona).model_dump_json(),
                step_index=0,
                advertised_tools=[],
            )
        )
        system = self.model.requests[before].messages[0]
        assert system.role == "system"
        return system.content

    async def chat_task(self) -> Task:
        """A chat turn, written the way the conversation endpoint writes it:
        a task on the conversation plus the seed user message."""
        async with self.sessions() as session:
            task = Task(
                workspace_id=self.workspace.id,
                title="Is the release ready?",
                description="Is the release ready?",
                state=TaskState.RUNNING.value,
                assigned_agent_id=self.agent.id,
                conversation_id=self.conversation.id,
                correlation_id=new_uuid7(),
                metadata_json={
                    "origin": "conversation",
                    "conversation_id": str(self.conversation.id),
                },
            )
            session.add(task)
            await session.flush()
            session.add(
                Message(
                    workspace_id=self.workspace.id,
                    task_id=task.id,
                    conversation_id=self.conversation.id,
                    sender_type=SenderType.USER.value,
                    sender_id=self.user_id,
                    recipient_type=RecipientType.AGENT.value,
                    recipient_id=self.agent.id,
                    message_type=MessageType.TEXT.value,
                    content_json={"text": "Is the release ready?"},
                    visibility=MessageVisibility.VISIBLE.value,
                )
            )
            await session.commit()
            return task

    async def delegated_task(self) -> Task:
        async with self.sessions() as session:
            task = Task(
                workspace_id=self.workspace.id,
                title="Check the release",
                description="please verify",
                state=TaskState.RUNNING.value,
                assigned_agent_id=self.agent.id,
                correlation_id=new_uuid7(),
                metadata_json={
                    "origin": "delegation",
                    "delegation": {
                        "kind": "delegation",
                        "delegated_by_agent_id": str(self.manager.id),
                        "delegated_by_agent_name": self.manager.name,
                    },
                },
            )
            session.add(task)
            await session.commit()
            return task

    async def trigger_task(self) -> Task:
        async with self.sessions() as session:
            task = Task(
                workspace_id=self.workspace.id,
                title="Nightly digest",
                description="run the digest",
                state=TaskState.RUNNING.value,
                assigned_agent_id=self.agent.id,
                correlation_id=new_uuid7(),
                metadata_json={"origin": "trigger"},
            )
            session.add(task)
            await session.commit()
            return task


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PersonaWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    world = PersonaWorld()
    world.sessions = sessions
    world.model = _Model()
    monkeypatch.setattr(reasoning_module, "build_model_client", lambda *_a, **_k: world.model)
    world.reasoning = AgentReasoningActivities(_Resources(sessions))  # type: ignore[arg-type]

    async with sessions() as session:
        world.workspace = Workspace(name="Persona", slug=f"persona-{new_uuid7().hex[:8]}")
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
        world.manager = Agent(
            workspace_id=world.workspace.id,
            name="Bisby",
            slug="bisby",
            role_title="Chief of Staff",
        )
        session.add(world.manager)
        await session.flush()
        world.agent = Agent(
            workspace_id=world.workspace.id,
            name="Connie",
            slug="connie",
            role_title="QA Engineer",
            manager_agent_id=world.manager.id,
        )
        session.add(world.agent)
        await session.flush()
        world.conversation = Conversation(
            workspace_id=world.workspace.id,
            title="Release",
            primary_agent_id=world.agent.id,
            created_by_user_id=user.id,
            last_activity_at=T0,
        )
        session.add(world.conversation)
        await session.commit()
        world.user_id = user.id
    yield world
    await engine.dispose()


async def test_chat_turn_reads_with_people(world: PersonaWorld) -> None:
    system = await world.system_prompt(await world.chat_task())
    assert "How you work — The Skeptic" in system
    assert "- With people: Warm and plain. Leads with the answer." in system
    assert "- With teammates:" not in system
    # ...and the counterpart it is keyed on is the one the prompt names.
    assert "Who you are talking with: Varand (workspace owner)" in system


async def test_delegated_child_reads_with_teammates(world: PersonaWorld) -> None:
    system = await world.system_prompt(await world.delegated_task())
    assert "How you work — The Skeptic" in system
    assert "- With teammates: Terse and structured: claim, evidence, gap." in system
    assert "- With people:" not in system
    assert "Bisby (Chief of Staff), an AI teammate in this workspace" in system


async def test_trigger_started_task_reads_neither(world: PersonaWorld) -> None:
    system = await world.system_prompt(await world.trigger_task())
    assert "How you work — The Skeptic" in system
    assert "- With people:" not in system
    assert "- With teammates:" not in system
    assert "Who you are talking with:" not in system
    # The rest of the card is unconditional.
    assert "- Voice: Dry, precise, quietly friendly." in system


async def test_block_sits_between_the_preamble_and_the_role_prompt(world: PersonaWorld) -> None:
    system = await world.system_prompt(await world.chat_task())
    preamble = render_platform_preamble(
        agent_name="Connie", role_title="QA Engineer", workspace_name=world.workspace.name
    )
    assert system.startswith(preamble)
    assert system[len(preamble) :].startswith("\n\n" + "How you work — The Skeptic\n")
    assert system.index(PERSONA_GUARDRAIL) < system.index("You test everything twice.")
    assert system.index("You test everything twice.") < system.index("Your manager is Bisby.")
    assert system.index("Your manager is Bisby.") < system.index("Current time:")


async def test_no_persona_on_the_snapshot_means_no_block(world: PersonaWorld) -> None:
    system = await world.system_prompt(await world.chat_task(), persona=None)
    assert "How you work" not in system
    assert PERSONA_GUARDRAIL not in system
    assert "Who you are talking with: Varand (workspace owner)" in system
