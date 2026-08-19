"""Reasoning-boundary guarantees retained from the Phase 9 activity suite."""

from __future__ import annotations

import json
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
from jhin_db.models import Agent, AgentRun, AuditEvent, Message, RunEvent, Task, ToolCall, Workspace
from jhin_domain import RunStatus, new_uuid7
from jhin_models import ModelRequest, ModelResponse, ModelToolCall, ModelUsage
from jhin_secrets import get_redactor
from jhin_tools import MAX_TOOL_CALLS_PER_STEP
from jhin_workflows.agent_task.shared import AdvertisedTool, ReasonAgentStepInput

_TOOL_NAME = "test.external.write"


class _FakeModelClient:
    def __init__(self) -> None:
        self.responses: list[ModelResponse] = []
        self.requests: list[ModelRequest] = []
        self.close_count = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("a model response was not configured")
        return self.responses.pop(0)

    async def close(self) -> None:
        self.close_count += 1


class _Publisher:
    async def publish(self, _envelope: Any) -> None:
        return None


class _Resources:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = sessions
        self.publisher = _Publisher()
        self.crypto = None


@dataclass
class _World:
    reasoning: AgentReasoningActivities
    sessions: async_sessionmaker[AsyncSession]
    client: _FakeModelClient
    workspace_id: Any
    run_id: Any
    params: ReasonAgentStepInput

    async def event_payloads(self) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            return [
                event.payload_json
                for event in await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == self.run_id).order_by(RunEvent.seq)
                )
            ]

    async def assert_no_execution_artifacts(self) -> None:
        async with self.sessions() as session:
            assert await session.scalar(select(func.count(ToolCall.id))) == 0
            assert await session.scalar(select(func.count(Message.id))) == 0


def _response(arguments_json: str = '{"value":"once"}') -> ModelResponse:
    return ModelResponse(
        text="Perform the structured call.",
        finish_reason="tool_calls",
        model="phase9-test-model",
        usage=ModelUsage(input_tokens=3, output_tokens=2),
        latency_ms=2,
        provider_request_id="phase9-request",
        tool_calls=(
            ModelToolCall(
                id="phase9-provider-call",
                name=_TOOL_NAME,
                arguments_json=arguments_json,
            ),
        ),
    )


@pytest.fixture
async def phase9_world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    resources = _Resources(sessions)
    client = _FakeModelClient()
    monkeypatch.setattr(reasoning_module, "build_model_client", lambda *_a, **_kw: client)

    async with sessions() as session:
        workspace = Workspace(name="Phase 9 reasoning", slug=f"phase9-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Reasoner", slug="reasoner")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Retain reasoning boundaries",
            description="Retain reasoning boundaries",
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
            provider_type="phase9-test",
            base_url=None,
            secret_id=None,
            model_name="phase9-test-model",
            display_name="Phase 9 test model",
            input_cost_micros_per_million=0,
            output_cost_micros_per_million=0,
        ),
        temperature=None,
        max_output_tokens=None,
        run_limits=RunLimits(max_steps=2, max_run_minutes=2),
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
                name=_TOOL_NAME,
                description="A deterministic external write.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            )
        ],
    )
    yield _World(
        reasoning=AgentReasoningActivities(resources),  # type: ignore[arg-type]
        sessions=sessions,
        client=client,
        workspace_id=workspace.id,
        run_id=run.id,
        params=params,
    )
    get_redactor().clear()
    await engine.dispose()


@pytest.mark.parametrize("failure_stage", ["build", "request"])
async def test_untyped_provider_failure_is_redacted_and_has_no_raw_cause(
    phase9_world: _World,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    secret = "provider-boundary-secret"
    get_redactor().register(secret)

    if failure_stage == "build":

        def fail_build(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"adapter reflected {secret}")

        monkeypatch.setattr(reasoning_module, "build_model_client", fail_build)
    else:

        async def fail_request(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"provider reflected {secret}")

        monkeypatch.setattr(reasoning_module, "execute_step", fail_request)

    with pytest.raises(ApplicationError) as error:
        await phase9_world.reasoning.reason_agent_step_activity(phase9_world.params)

    assert error.value.type in {"provider_config", "model_provider_error"}
    assert secret not in str(error.value)
    assert error.value.__cause__ is None
    assert await phase9_world.event_payloads() == []
    await phase9_world.assert_no_execution_artifacts()


async def test_model_client_close_failure_is_redacted_and_does_not_replace_result(
    phase9_world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "provider-close-secret"
    get_redactor().register(secret)
    phase9_world.client.responses.append(
        _response().model_copy(update={"finish_reason": "stop", "tool_calls": ()})
    )

    async def fail_close() -> None:
        raise RuntimeError(f"close reflected {secret}")

    monkeypatch.setattr(phase9_world.client, "close", fail_close)

    result = await phase9_world.reasoning.reason_agent_step_activity(phase9_world.params)

    assert result.call_count == 0
    assert secret not in json.dumps(await phase9_world.event_payloads(), ensure_ascii=False)
    await phase9_world.assert_no_execution_artifacts()


async def test_manifest_canonicalizes_strict_json_and_keeps_provider_ids_private(
    phase9_world: _World,
) -> None:
    phase9_world.client.responses.append(_response('{ "z" : 2, "value" : "once" }'))

    result = await phase9_world.reasoning.reason_agent_step_activity(phase9_world.params)

    assert result.call_count == 1
    manifest, reasoning = await phase9_world.event_payloads()
    assert manifest["manifest"]["calls"][0]["arguments_json"] == '{"value":"once","z":2}'
    assert "phase9-provider-call" not in json.dumps(manifest)
    assert reasoning["provider_call_ids"] == ["phase9-provider-call"]
    await phase9_world.assert_no_execution_artifacts()


async def test_invalid_json_with_escaped_secret_never_reaches_durable_state(
    phase9_world: _World,
) -> None:
    secret = 'invalid-json-secret-"-line\nnext'
    escaped_secret = json.dumps(secret, ensure_ascii=False)[1:-1]
    get_redactor().register(secret)
    phase9_world.client.responses.append(_response(f'{{"value":"{escaped_secret}"'))

    with pytest.raises(ApplicationError) as error:
        await phase9_world.reasoning.reason_agent_step_activity(phase9_world.params)

    assert error.value.type == "tool_step_manifest_not_lossless"
    async with phase9_world.sessions() as session:
        run = await session.get(AgentRun, phase9_world.run_id)
        audits = list(await session.scalars(select(AuditEvent)))
        assert run is not None
        persisted = json.dumps(
            {"error": run.error_message, "audits": [audit.metadata_json for audit in audits]},
            ensure_ascii=False,
        )
    assert secret not in persisted
    assert escaped_secret not in persisted
    assert await phase9_world.event_payloads() == []
    await phase9_world.assert_no_execution_artifacts()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
async def test_nonstandard_json_number_stops_before_manifest(
    phase9_world: _World,
    constant: str,
) -> None:
    phase9_world.client.responses.append(_response(f'{{"value":{constant}}}'))

    with pytest.raises(ApplicationError) as error:
        await phase9_world.reasoning.reason_agent_step_activity(phase9_world.params)

    assert error.value.type == "tool_step_manifest_not_lossless"
    assert await phase9_world.event_payloads() == []
    await phase9_world.assert_no_execution_artifacts()


@pytest.mark.parametrize(
    "arguments_json",
    [
        '{"value":"first","value":"second"}',
        '["not-an-object"]',
        '"not-an-object"',
        "42",
        "null",
    ],
)
async def test_duplicate_or_non_object_arguments_stop_before_manifest(
    phase9_world: _World,
    arguments_json: str,
) -> None:
    phase9_world.client.responses.append(_response(arguments_json))

    with pytest.raises(ApplicationError) as error:
        await phase9_world.reasoning.reason_agent_step_activity(phase9_world.params)

    assert error.value.type == "tool_step_manifest_not_lossless"
    assert await phase9_world.event_payloads() == []
    await phase9_world.assert_no_execution_artifacts()


async def test_tool_call_limit_fails_before_manifest(
    phase9_world: _World,
) -> None:
    tool_calls = tuple(
        ModelToolCall(
            id=f"provider-call-{ordinal}",
            name=_TOOL_NAME,
            arguments_json='{"value":"must-not-run"}',
        )
        for ordinal in range(MAX_TOOL_CALLS_PER_STEP + 1)
    )
    phase9_world.client.responses.append(_response().model_copy(update={"tool_calls": tool_calls}))

    with pytest.raises(ApplicationError) as error:
        await phase9_world.reasoning.reason_agent_step_activity(phase9_world.params)

    assert error.value.type == "tool_call_limit_exceeded"
    assert error.value.non_retryable is True
    assert len(phase9_world.client.requests) == 1
    assert await phase9_world.event_payloads() == []
    await phase9_world.assert_no_execution_artifacts()
