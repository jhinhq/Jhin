"""Agent reasoning binds a public-safe manifest and private sidecar atomically."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError

import jhin_agent_worker.reasoning as reasoning_module
from jhin_agent_worker.reasoning import AgentReasoningActivities
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, RunEvent, Task, ToolCall, Workspace
from jhin_domain import RunStatus, new_uuid7
from jhin_models import ModelRequest, ModelResponse, ModelToolCall, ModelUsage
from jhin_workflows.agent_task.shared import (
    AdvertisedTool,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
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


class _FailingCommitSession(AsyncSession):
    fail_next_commit: BaseException | None = None

    async def commit(self) -> None:
        failure = type(self).fail_next_commit
        if failure is not None:
            type(self).fail_next_commit = None
            raise failure
        await super().commit()


class _Resources:
    def __init__(self, sessions: async_sessionmaker[_FailingCommitSession]) -> None:
        self.session_factory = sessions
        self.publisher = _Publisher()
        self.crypto = None


@dataclass
class _Effect:
    count: int = 0


@dataclass
class ReasoningWorld:
    reasoning: AgentReasoningActivities
    sessions: async_sessionmaker[_FailingCommitSession]
    model: _Model
    effect: _Effect
    params: ReasonAgentStepInput
    workspace_id: Any
    task_id: Any
    run_id: Any

    async def load_event(self, event_type: str, *, step: int = 0) -> RunEvent:
        async with self.sessions() as session:
            events = list(
                await session.scalars(
                    select(RunEvent).where(
                        RunEvent.run_id == self.run_id,
                        RunEvent.event_type == event_type,
                    )
                )
            )
        return next(event for event in events if event.payload_json.get("step") == step)

    async def count_events(self, event_type: str) -> int:
        async with self.sessions() as session:
            return (
                await session.scalar(
                    select(func.count(RunEvent.id)).where(
                        RunEvent.run_id == self.run_id,
                        RunEvent.event_type == event_type,
                    )
                )
                or 0
            )

    async def tool_call_count(self) -> int:
        async with self.sessions() as session:
            return (
                await session.scalar(
                    select(func.count(ToolCall.id)).where(ToolCall.run_id == self.run_id)
                )
                or 0
            )


def two_call_response() -> ModelResponse:
    return ModelResponse(
        text="Calling two tools.",
        finish_reason="tool_calls",
        model="reasoning-test",
        usage=ModelUsage(input_tokens=7, output_tokens=3, cached_tokens=1),
        latency_ms=4,
        provider_request_id="provider-request-1",
        tool_calls=(
            ModelToolCall(
                id="provider-call-1",
                name="system.echo",
                arguments_json='{"value":"first"}',
            ),
            ModelToolCall(
                id="provider-call-2",
                name="system.echo",
                arguments_json='{"value":"second"}',
            ),
        ),
    )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ReasoningWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=_FailingCommitSession,
    )
    resources = _Resources(sessions)
    model = _Model()
    monkeypatch.setattr(reasoning_module, "build_model_client", lambda *_args, **_kwargs: model)

    async with sessions() as session:
        workspace = Workspace(name="Reasoning", slug=f"reasoning-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Reasoner", slug="reasoner")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Bind calls",
            description="Bind calls",
            assigned_agent_id=agent.id,
            correlation_id=new_uuid7(),
        )
        session.add(task)
        await session.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.RUNNING.value,
        )
        session.add(run)
        await session.commit()

    snapshot = AgentExecutionSnapshot(
        agent_id=agent.id,
        workspace_id=workspace.id,
        name=agent.name,
        role_title="",
        system_prompt="",
        autonomy_level="balanced",
        team_id=None,
        team_name=None,
        manager_agent_id=None,
        manager_name=None,
        model_profile=ModelProfileSnapshot(
            profile_id=new_uuid7(),
            provider_id=new_uuid7(),
            provider_type="reasoning-test",
            base_url=None,
            secret_id=None,
            model_name="reasoning-test",
            display_name="Reasoning test",
            input_cost_micros_per_million=1_000_000,
            output_cost_micros_per_million=1_000_000,
        ),
        temperature=None,
        max_output_tokens=None,
        run_limits=RunLimits(max_steps=5, max_run_minutes=5),
    )
    params = ReasonAgentStepInput(
        workspace_id=str(workspace.id),
        task_id=str(task.id),
        run_id=str(run.id),
        agent_id=str(agent.id),
        snapshot_json=snapshot.model_dump_json(),
        step_index=0,
        advertised_tools=[
            AdvertisedTool(
                name="system.echo",
                description="Echo a value",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}},
            )
        ],
    )
    yield ReasoningWorld(
        reasoning=AgentReasoningActivities(resources),  # type: ignore[arg-type]
        sessions=sessions,
        model=model,
        effect=_Effect(),
        params=params,
        workspace_id=workspace.id,
        task_id=task.id,
        run_id=run.id,
    )
    _FailingCommitSession.fail_next_commit = None
    await engine.dispose()


async def test_reasoning_returns_count_after_atomic_lossless_bind(
    world: ReasoningWorld,
) -> None:
    world.model.responses.append(two_call_response())

    result = await world.reasoning.reason_agent_step_activity(world.params)

    assert result == ReasonAgentStepResult(call_count=2)
    assert not hasattr(result, "tool_calls")
    assert not hasattr(result, "text")
    manifest = await world.load_event("agent.step.tool_manifest")
    reasoning = await world.load_event("agent.step.reasoning")
    assert set(manifest.payload_json) == {"step", "manifest"}
    assert [call["arguments_json"] for call in manifest.payload_json["manifest"]["calls"]] == [
        '{"value":"first"}',
        '{"value":"second"}',
    ]
    assert reasoning.payload_json["provider_call_ids"] == [
        "provider-call-1",
        "provider-call-2",
    ]
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert await world.tool_call_count() == 0
    assert world.effect.count == 0
    assert world.model.requests[0].tools[0].name == "system.echo"


async def test_new_reasoning_bind_rolls_back_manifest_and_reasoning_together(
    world: ReasoningWorld,
) -> None:
    world.model.responses.append(two_call_response())
    _FailingCommitSession.fail_next_commit = RuntimeError("injected commit failure")

    with pytest.raises(RuntimeError, match="injected commit failure"):
        await world.reasoning.reason_agent_step_activity(world.params)

    assert await world.count_events("agent.step.tool_manifest") == 0
    assert await world.count_events("agent.step.reasoning") == 0
    assert await world.tool_call_count() == 0
    assert world.effect.count == 0


@pytest.mark.parametrize("lossy_kind", ["nonobject", "long_name", "secret"])
async def test_nonlossless_reasoning_fails_before_effects(
    world: ReasoningWorld,
    lossy_kind: str,
) -> None:
    response = two_call_response()
    call = response.tool_calls[0]
    if lossy_kind == "nonobject":
        call = call.model_copy(update={"arguments_json": "[]"})
    elif lossy_kind == "long_name":
        call = call.model_copy(update={"name": "n" * 201})
    else:
        from jhin_secrets import get_redactor

        get_redactor().register("reasoning-manifest-secret")
        call = call.model_copy(
            update={"arguments_json": '{"value":"reasoning-manifest-secret"}'}
        )
    world.model.responses.append(response.model_copy(update={"tool_calls": (call,)}))
    try:
        with pytest.raises(ApplicationError) as error:
            await world.reasoning.reason_agent_step_activity(world.params)
        assert error.value.type == "tool_step_manifest_not_lossless"
        assert await world.tool_call_count() == 0
        assert world.effect.count == 0
    finally:
        from jhin_secrets import get_redactor

        get_redactor().clear()


async def test_complete_reasoning_pair_replays_without_model_call(world: ReasoningWorld) -> None:
    world.model.responses.append(two_call_response())
    first = await world.reasoning.reason_agent_step_activity(world.params)

    replay = await world.reasoning.reason_agent_step_activity(world.params)

    assert replay == first
    assert len(world.model.requests) == 1


async def test_manifest_without_reasoning_fails_closed_for_new_activity(
    world: ReasoningWorld,
) -> None:
    async with world.sessions() as session:
        session.add(
            RunEvent(
                workspace_id=world.workspace_id,
                task_id=world.task_id,
                run_id=world.run_id,
                seq=0,
                event_type="agent.step.tool_manifest",
                payload_json={
                    "step": 0,
                    "manifest": {
                        "count": 1,
                        "calls": [
                            {
                                "ordinal": 0,
                                "lossless": True,
                                "tool_name": "system.echo",
                                "arguments_json": '{"value":"same"}',
                            }
                        ],
                    },
                },
            )
        )
        await session.commit()

    with pytest.raises(ApplicationError) as error:
        await world.reasoning.reason_agent_step_activity(world.params)

    assert error.value.type == "reasoning_sidecar_missing"
    assert error.value.non_retryable is True
    assert world.model.requests == []
