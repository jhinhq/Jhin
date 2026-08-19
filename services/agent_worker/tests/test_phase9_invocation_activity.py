"""Phase 9 activity-level guarantees for at-most-once tool invocations.

These tests keep the database, model request composition, policy evaluation,
gateway, transcript, and step-marker persistence real.  Only the external
model provider and the external effect are deterministic fakes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError

import jhin_agent_worker.activities as activities_module
import jhin_agent_worker.reasoning as reasoning_module
from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.projections import AgentProjectionActivities
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    AuditEvent,
    Message,
    RunEvent,
    Task,
    ToolCall,
    Workspace,
)
from jhin_domain import RunStatus, ToolCallStatus, new_uuid7
from jhin_models import ModelRequest, ModelResponse, ModelToolCall, ModelUsage
from jhin_policy import RiskLevel, ToolDefinition
from jhin_secrets import get_redactor
from jhin_tools import (
    MAX_TOOL_CALLS_PER_STEP,
    ToolCatalog,
    ToolExecutionContext,
    ToolGateway,
    stable_tool_invocation_id,
)
from jhin_workflows.agent_task import FinalizeInput, RunStepInput, StepResult

_TOOL_NAME = "test.external.write"
_INPUT_TOKENS = 13
_OUTPUT_TOKENS = 5
_CACHED_TOKENS = 2
_COST_MICROS = _INPUT_TOKENS + _OUTPUT_TOKENS


class _EffectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    label: str = ""


class _EffectOutput(BaseModel):
    receipt: str


@dataclass
class _EffectState:
    count: int = 0
    fail_after_effect: bool = False


class _FakeModelClient:
    def __init__(self) -> None:
        self.responses: list[ModelResponse] = []
        self.requests: list[ModelRequest] = []
        self.close_count = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("the committed step should have prevented another model call")
        return self.responses.pop(0)

    async def close(self) -> None:
        self.close_count += 1


class _Publisher:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.events.append(envelope)


class _Resources:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = sessions
        self.publisher = _Publisher()
        self.crypto = None


@dataclass
class _World:
    activities: AgentActivities
    sessions: async_sessionmaker[AsyncSession]
    publisher: _Publisher
    client: _FakeModelClient
    catalog: ToolCatalog
    effect: _EffectState
    workspace_id: Any
    agent_id: Any
    task_id: Any
    run_id: Any
    params: RunStepInput


def _tool_response(provider_call_id: str, *, request_id: str) -> ModelResponse:
    return ModelResponse(
        text="I will perform the requested action.",
        finish_reason="tool_calls",
        model="phase9-test-model",
        usage=ModelUsage(
            input_tokens=_INPUT_TOKENS,
            output_tokens=_OUTPUT_TOKENS,
            cached_tokens=_CACHED_TOKENS,
        ),
        latency_ms=4,
        provider_request_id=request_id,
        tool_calls=(
            ModelToolCall(
                id=provider_call_id,
                name=_TOOL_NAME,
                arguments_json='{"value":"once"}',
            ),
        ),
    )


def _calls_response(calls: list[tuple[str, str]], *, request_id: str) -> ModelResponse:
    return ModelResponse(
        text="Perform the structured calls.",
        finish_reason="tool_calls" if calls else "stop",
        model="phase9-test-model",
        usage=ModelUsage(input_tokens=3, output_tokens=2),
        latency_ms=2,
        provider_request_id=request_id,
        tool_calls=tuple(
            ModelToolCall(
                id=f"provider-{request_id}-{ordinal}",
                name=name,
                arguments_json=json.dumps({"value": value}, separators=(",", ":")),
            )
            for ordinal, (name, value) in enumerate(calls)
        ),
    )


async def _rows(
    sessions: async_sessionmaker[AsyncSession], model: Any, *, run_id: Any
) -> list[Any]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(model).where(model.run_id == run_id).order_by(model.created_at, model.id)
            )
        )


async def _assert_one_canonical_bundle(
    world: _World, *, provider_call_id: str
) -> tuple[StepResult, list[RunEvent]]:
    expected_invocation_id = stable_tool_invocation_id(world.run_id, 0, 0)
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        assert run.input_tokens == _INPUT_TOKENS
        assert run.output_tokens == _OUTPUT_TOKENS
        assert run.cached_tokens == _CACHED_TOKENS
        assert run.estimated_cost_micros == _COST_MICROS
        assert run.steps_used == 1

        tool_calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.run_id == world.run_id))
        )
        assert len(tool_calls) == 1
        assert tool_calls[0].id == expected_invocation_id

        messages = list(
            await session.scalars(
                select(Message)
                .where(
                    Message.run_id == world.run_id,
                    Message.message_type.in_(("tool_call", "tool_result")),
                )
                .order_by(Message.created_at, Message.id)
            )
        )
        assert len(messages) == 2
        by_type = {message.message_type: message for message in messages}
        assert set(by_type) == {"tool_call", "tool_result"}
        for message in by_type.values():
            assert message.content_json["tool_call_id"] == str(expected_invocation_id)
            assert message.content_json["provider_call_id"] == provider_call_id

        events = list(
            await session.scalars(
                select(RunEvent).where(RunEvent.run_id == world.run_id).order_by(RunEvent.seq)
            )
        )
        markers = [event for event in events if event.event_type == "agent.step.committed"]
        assert len(markers) == 1
        assert markers[0].payload_json["step"] == 0
        assert markers[0].payload_json["gateway_tool_call_ids"] == [str(expected_invocation_id)]
        assert len({event.seq for event in events}) == len(events)
        assert [event.event_type for event in events] == [
            "agent.step.tool_manifest",
            "agent.step.reasoning",
            "node.load_context",
            "node.reason",
            "node.call_tool",
            "node.policy_check",
            "node.execute_tool",
            "node.observe",
            "tool.call",
            "agent.step.committed",
        ]
        result = StepResult(**markers[0].payload_json["result"])
    return result, events


@pytest.fixture
async def phase9_world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    resources = _Resources(sessions)
    activities = AgentActivities(resources)  # type: ignore[arg-type]
    client = _FakeModelClient()
    effect = _EffectState()

    async def execute_external_effect(
        _context: ToolExecutionContext, _payload: BaseModel
    ) -> BaseModel:
        effect.count += 1
        if effect.fail_after_effect:
            raise RuntimeError("response was lost after the external effect")
        return _EffectOutput(receipt=f"external-receipt-{effect.count}")

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name=_TOOL_NAME,
            description="A deterministic external write used by activity recovery tests.",
            risk=RiskLevel.WRITE,
            input_model=_EffectInput,
            output_model=_EffectOutput,
            required_capability=_TOOL_NAME,
        ),
        execute_external_effect,
    )
    monkeypatch.setattr(activities_module, "build_default_catalog", lambda: catalog)
    monkeypatch.setattr(
        activities_module,
        "build_model_client",
        lambda *_args, **_kwargs: client,
    )

    async with sessions() as session:
        workspace = Workspace(name="Phase 9 activity", slug=f"phase9-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Reliable agent", slug="reliable-agent")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Perform exactly one external action",
            description="Perform exactly one external action",
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
        session.add(
            AgentCapabilityGrant(
                workspace_id=workspace.id,
                agent_id=agent.id,
                capability=_TOOL_NAME,
                scope_json={},
                effect="allow",
            )
        )
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
            input_cost_micros_per_million=1_000_000,
            output_cost_micros_per_million=1_000_000,
        ),
        temperature=None,
        max_output_tokens=None,
        run_limits=RunLimits(max_steps=5, max_run_minutes=5),
    )
    params = RunStepInput(
        workspace_id=str(workspace.id),
        task_id=str(task.id),
        run_id=str(run.id),
        agent_id=str(agent.id),
        snapshot_json=snapshot.model_dump_json(),
        step_index=0,
    )
    yield _World(
        activities=activities,
        sessions=sessions,
        publisher=resources.publisher,
        client=client,
        catalog=catalog,
        effect=effect,
        workspace_id=workspace.id,
        agent_id=agent.id,
        task_id=task.id,
        run_id=run.id,
        params=params,
    )
    await engine.dispose()


async def test_run_step_retry_reuses_bound_reasoning_and_persists_one_canonical_bundle(
    phase9_world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before the step commit must not repeat a completed mutation."""
    world = phase9_world
    world.client.responses.extend(
        [
            _tool_response("provider-call-before-crash", request_id="request-before-crash"),
            _tool_response("provider-call-after-retry", request_id="request-after-retry"),
        ]
    )
    original = AgentProjectionActivities._record_gateway_result
    crash_once = True

    def fail_before_outer_bundle_commit(
        self: AgentProjectionActivities, *args: Any, **kwargs: Any
    ) -> int:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise RuntimeError("worker crashed before the outer bundle commit")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        AgentProjectionActivities,
        "_record_gateway_result",
        fail_before_outer_bundle_commit,
    )

    with pytest.raises(RuntimeError, match="outer bundle commit"):
        await world.activities.run_agent_step_activity(world.params)
    result = await world.activities.run_agent_step_activity(world.params)

    assert result.done is False
    assert world.effect.count == 1
    assert len(world.client.requests) == 1
    persisted, _events = await _assert_one_canonical_bundle(
        world, provider_call_id="provider-call-before-crash"
    )
    assert result == persisted


@pytest.mark.parametrize("failure_stage", ["build", "request"])
async def test_untyped_provider_failure_is_redacted_and_has_no_raw_cause(
    phase9_world: _World,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    world = phase9_world
    secret = "provider-boundary-secret"
    redactor = get_redactor()
    redactor.clear()
    redactor.register(secret)

    if failure_stage == "build":

        def fail_build(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"adapter reflected {secret}")

        monkeypatch.setattr(activities_module, "build_model_client", fail_build)
    else:

        async def fail_request(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"provider reflected {secret}")

        monkeypatch.setattr(reasoning_module, "execute_step", fail_request)

    try:
        with pytest.raises(ApplicationError) as error:
            await world.activities.run_agent_step_activity(world.params)
        assert error.value.type in {"provider_config", "model_provider_error"}
        assert secret not in str(error.value)
        assert error.value.__cause__ is None
        assert world.effect.count == 0
    finally:
        redactor.clear()


async def test_model_client_close_failure_is_redacted_and_does_not_replace_result(
    phase9_world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = phase9_world
    secret = "provider-close-secret"
    redactor = get_redactor()
    redactor.clear()
    redactor.register(secret)
    world.client.responses.append(_calls_response([], request_id="close-failure"))

    async def fail_close() -> None:
        raise RuntimeError(f"close reflected {secret}")

    monkeypatch.setattr(world.client, "close", fail_close)
    try:
        result = await world.activities.run_agent_step_activity(world.params)
        assert result.done is True
        async with world.sessions() as session:
            persisted = json.dumps(
                {
                    "messages": [
                        message.content_json
                        for message in await session.scalars(
                            select(Message).where(Message.run_id == world.run_id)
                        )
                    ],
                    "events": [
                        event.payload_json
                        for event in await session.scalars(
                            select(RunEvent).where(RunEvent.run_id == world.run_id)
                        )
                    ],
                },
                ensure_ascii=False,
            )
            assert secret not in persisted
    finally:
        redactor.clear()


async def test_manifest_canonicalizes_json_order_and_ignores_provider_ids(
    phase9_world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = phase9_world
    first = _tool_response("provider-format-one", request_id="format-one")
    retry = _tool_response("provider-format-two", request_id="format-two")
    first_call = first.tool_calls[0]
    retry_call = retry.tool_calls[0]
    first = first.model_copy(
        update={
            "tool_calls": (
                first_call.model_copy(update={"arguments_json": '{"value":"once","label":"same"}'}),
            )
        }
    )
    retry = retry.model_copy(
        update={
            "tool_calls": (
                retry_call.model_copy(
                    update={"arguments_json": '{ "label" : "same", "value" : "once" }'}
                ),
            )
        }
    )
    world.client.responses.extend([first, retry])
    original = AgentProjectionActivities._record_gateway_result
    crash_once = True

    def crash_before_bundle(
        self: AgentProjectionActivities, *args: Any, **kwargs: Any
    ) -> int:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise RuntimeError("canonical formatting crash")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AgentProjectionActivities, "_record_gateway_result", crash_before_bundle)

    with pytest.raises(RuntimeError, match="formatting crash"):
        await world.activities.run_agent_step_activity(world.params)
    repaired = await world.activities.run_agent_step_activity(world.params)

    assert repaired.done is False
    assert world.effect.count == 1
    assert len(world.client.requests) == 1
    await _assert_one_canonical_bundle(world, provider_call_id="provider-format-one")


async def test_successful_two_call_step_uses_provider_order_for_canonical_ids(
    phase9_world: _World,
) -> None:
    world = phase9_world
    world.client.responses.append(
        _calls_response(
            [(_TOOL_NAME, "first"), (_TOOL_NAME, "second")],
            request_id="ordered-calls",
        )
    )

    result = await world.activities.run_agent_step_activity(world.params)

    assert result.done is False
    assert world.effect.count == 2
    expected_ids = [stable_tool_invocation_id(world.run_id, 0, ordinal) for ordinal in range(2)]
    async with world.sessions() as session:
        rows = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.run_id == world.run_id)
                .order_by(ToolCall.sanitized_input_json["value"].as_string())
            )
        )
        assert {row.id for row in rows} == set(expected_ids)
        messages = list(
            await session.scalars(
                select(Message)
                .where(
                    Message.run_id == world.run_id,
                    Message.message_type.in_(("tool_call", "tool_result")),
                )
                .order_by(Message.created_at, Message.id)
            )
        )
        assert [message.message_type for message in messages] == [
            "tool_call",
            "tool_result",
            "tool_call",
            "tool_result",
        ]
        assert [message.content_json["tool_call_id"] for message in messages] == [
            str(expected_ids[0]),
            str(expected_ids[0]),
            str(expected_ids[1]),
            str(expected_ids[1]),
        ]
        tool_events = list(
            await session.scalars(
                select(RunEvent)
                .where(
                    RunEvent.run_id == world.run_id,
                    RunEvent.event_type == "tool.call",
                )
                .order_by(RunEvent.seq)
            )
        )
        assert [event.payload_json["tool_call_id"] for event in tool_events] == [
            str(expected_ids[0]),
            str(expected_ids[1]),
        ]


@pytest.mark.parametrize(
    ("initial_calls", "retry_calls"),
    [
        ([(_TOOL_NAME, "once")], []),
        ([(_TOOL_NAME, "once")], [(_TOOL_NAME, "once"), (_TOOL_NAME, "added")]),
        ([(_TOOL_NAME, "once")], [(_TOOL_NAME, "changed")]),
        ([(_TOOL_NAME, "once")], [("test.external.changed", "once")]),
        (
            [(_TOOL_NAME, "first"), (_TOOL_NAME, "second")],
            [(_TOOL_NAME, "second"), (_TOOL_NAME, "first")],
        ),
    ],
)
async def test_retry_after_bound_call_set_skips_new_model_output_and_additional_effect(
    phase9_world: _World,
    monkeypatch: pytest.MonkeyPatch,
    initial_calls: list[tuple[str, str]],
    retry_calls: list[tuple[str, str]],
) -> None:
    world = phase9_world
    world.client.responses.extend(
        [
            _calls_response(initial_calls, request_id="manifest-first"),
            _calls_response(retry_calls, request_id="manifest-retry"),
            _calls_response(initial_calls, request_id="manifest-canonical-repair"),
        ]
    )
    original = AgentProjectionActivities._record_gateway_result
    crash_once = True

    def crash_before_bundle(
        self: AgentProjectionActivities, *args: Any, **kwargs: Any
    ) -> int:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise RuntimeError("crash after effects but before bundle")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AgentProjectionActivities, "_record_gateway_result", crash_before_bundle)

    with pytest.raises(RuntimeError, match="before bundle"):
        await world.activities.run_agent_step_activity(world.params)
    effects_after_first_attempt = world.effect.count
    assert effects_after_first_attempt == len(initial_calls)

    repaired = await world.activities.run_agent_step_activity(world.params)

    assert repaired.done is False
    assert len(world.client.requests) == 1
    assert world.effect.count == effects_after_first_attempt
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        assert run.status == RunStatus.RUNNING.value
        assert run.error_code is None
        assert (
            await session.scalar(
                select(func.count(Message.id)).where(Message.run_id == world.run_id)
            )
            == len(initial_calls) * 2
        )
        tool_call_ids = set(
            await session.scalars(select(ToolCall.id).where(ToolCall.run_id == world.run_id))
        )
        assert tool_call_ids == {
            stable_tool_invocation_id(world.run_id, 0, ordinal)
            for ordinal in range(len(initial_calls))
        }
        manifests = list(
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == world.run_id,
                    RunEvent.event_type == "agent.step.tool_manifest",
                )
            )
        )
        assert len(manifests) == 1
        assert manifests[0].payload_json["manifest"]["count"] == len(initial_calls)

    replay = await world.activities.run_agent_step_activity(world.params)
    assert replay == repaired
    assert world.effect.count == effects_after_first_attempt
    async with world.sessions() as session:
        assert (
            await session.scalar(
                select(func.count(RunEvent.id)).where(
                    RunEvent.run_id == world.run_id,
                    RunEvent.event_type == "agent.step.committed",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(Message.id)).where(Message.run_id == world.run_id)
            )
            == len(initial_calls) * 2
        )


@pytest.mark.parametrize("lossy_kind", ["registered_secret", "long_argument", "long_name"])
async def test_nonlossless_manifest_stops_before_persisting_or_executing(
    phase9_world: _World,
    lossy_kind: str,
) -> None:
    world = phase9_world
    secret = 'manifest-secret-with-quote-"-and-newline\nvalue'
    redactor = get_redactor()
    redactor.clear()
    tool_name = _TOOL_NAME
    value = "safe"
    if lossy_kind == "registered_secret":
        redactor.register(secret)
        value = secret
    elif lossy_kind == "long_argument":
        value = "z" * 9_000
    else:
        tool_name = "n" * 201
    world.client.responses.append(
        _calls_response([(tool_name, value)], request_id=f"lossy-{lossy_kind}")
    )

    try:
        with pytest.raises(ApplicationError) as error:
            await world.activities.run_agent_step_activity(world.params)
        assert error.value.type == "tool_step_manifest_not_lossless"
        assert error.value.non_retryable is True
        assert world.effect.count == 0

        async with world.sessions() as session:
            run = await session.get(AgentRun, world.run_id)
            assert run is not None
            assert run.status == RunStatus.FAILED.value
            assert run.error_code == "tool_step_manifest_not_lossless"
            assert await session.scalar(select(func.count(ToolCall.id))) == 0
            assert await session.scalar(select(func.count(Message.id))) == 0
            assert await session.scalar(select(func.count(RunEvent.id))) == 0
            audits = list(await session.scalars(select(AuditEvent)))
            assert len(audits) == 1
            persisted = json.dumps(
                {
                    "run_error": run.error_message,
                    "audit": audits[0].metadata_json,
                },
                ensure_ascii=False,
            )
            assert secret not in persisted
            assert json.dumps(secret, ensure_ascii=False)[1:-1] not in persisted
    finally:
        redactor.clear()


async def test_invalid_json_with_escaped_secret_never_reaches_durable_state(
    phase9_world: _World,
) -> None:
    world = phase9_world
    secret = 'invalid-json-secret-"-line\nnext'
    escaped_secret = json.dumps(secret, ensure_ascii=False)[1:-1]
    redactor = get_redactor()
    redactor.clear()
    redactor.register(secret)
    response = _tool_response("provider-invalid-json", request_id="invalid-json")
    call = response.tool_calls[0]
    world.client.responses.append(
        response.model_copy(
            update={
                "tool_calls": (
                    call.model_copy(update={"arguments_json": f'{{"value":"{escaped_secret}"'}),
                )
            }
        )
    )

    try:
        with pytest.raises(ApplicationError) as error:
            await world.activities.run_agent_step_activity(world.params)
        assert error.value.type == "tool_step_manifest_not_lossless"
        assert world.effect.count == 0

        async with world.sessions() as session:
            assert await session.scalar(select(func.count(ToolCall.id))) == 0
            assert await session.scalar(select(func.count(Message.id))) == 0
            assert await session.scalar(select(func.count(RunEvent.id))) == 0
            run = await session.get(AgentRun, world.run_id)
            audits = list(await session.scalars(select(AuditEvent)))
            assert run is not None
            persisted = json.dumps(
                {
                    "error": run.error_message,
                    "audits": [audit.metadata_json for audit in audits],
                },
                ensure_ascii=False,
            )
            assert secret not in persisted
            assert escaped_secret not in persisted
    finally:
        redactor.clear()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
async def test_nonstandard_json_number_stops_before_manifest_or_effect(
    phase9_world: _World,
    constant: str,
) -> None:
    world = phase9_world
    response = _tool_response("provider-non-finite", request_id="non-finite")
    call = response.tool_calls[0]
    world.client.responses.append(
        response.model_copy(
            update={
                "tool_calls": (
                    call.model_copy(update={"arguments_json": f'{{"value":{constant}}}'}),
                )
            }
        )
    )

    with pytest.raises(ApplicationError) as error:
        await world.activities.run_agent_step_activity(world.params)

    assert error.value.type == "tool_step_manifest_not_lossless"
    assert world.effect.count == 0
    async with world.sessions() as session:
        assert await session.scalar(select(func.count(ToolCall.id))) == 0
        assert await session.scalar(select(func.count(Message.id))) == 0
        assert await session.scalar(select(func.count(RunEvent.id))) == 0


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
async def test_duplicate_or_non_object_arguments_stop_before_manifest_or_effect(
    phase9_world: _World,
    arguments_json: str,
) -> None:
    world = phase9_world
    response = _tool_response("provider-invalid-shape", request_id="invalid-shape")
    call = response.tool_calls[0]
    world.client.responses.append(
        response.model_copy(
            update={"tool_calls": (call.model_copy(update={"arguments_json": arguments_json}),)}
        )
    )

    with pytest.raises(ApplicationError) as error:
        await world.activities.run_agent_step_activity(world.params)

    assert error.value.type == "tool_step_manifest_not_lossless"
    assert world.effect.count == 0
    async with world.sessions() as session:
        assert await session.scalar(select(func.count(ToolCall.id))) == 0
        assert await session.scalar(select(func.count(Message.id))) == 0
        assert await session.scalar(select(func.count(RunEvent.id))) == 0


async def test_nonlossless_first_attempt_permanently_blocks_later_effect(
    phase9_world: _World,
) -> None:
    world = phase9_world
    secret = "first-attempt-manifest-secret"
    redactor = get_redactor()
    redactor.clear()
    redactor.register(secret)
    world.client.responses.extend(
        [
            _calls_response([(_TOOL_NAME, secret)], request_id="lossy-first"),
            _calls_response([(_TOOL_NAME, "safe-later")], request_id="safe-later"),
        ]
    )

    try:
        for _attempt in range(2):
            with pytest.raises(ApplicationError) as error:
                await world.activities.run_agent_step_activity(world.params)
            assert error.value.type == "tool_step_manifest_not_lossless"
            assert error.value.non_retryable is True
        assert world.effect.count == 0

        async with world.sessions() as session:
            assert await session.scalar(select(func.count(ToolCall.id))) == 0
            assert await session.scalar(select(func.count(Message.id))) == 0
            assert await session.scalar(select(func.count(RunEvent.id))) == 0
            assert (
                await session.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.action == "agent.step.manifest_not_lossless"
                    )
                )
                == 1
            )
    finally:
        redactor.clear()


async def test_lossy_retry_cannot_poison_or_reuse_canonical_manifest(
    phase9_world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = phase9_world
    secret = "retry-only-manifest-secret"
    redactor = get_redactor()
    redactor.clear()
    redactor.register(secret)
    world.client.responses.extend(
        [
            _calls_response([(_TOOL_NAME, "canonical")], request_id="canonical-first"),
            _calls_response([(_TOOL_NAME, secret)], request_id="lossy-retry"),
            _calls_response([(_TOOL_NAME, "canonical")], request_id="canonical-repair"),
        ]
    )
    original = AgentProjectionActivities._record_gateway_result
    crash_once = True

    def crash_before_bundle(
        self: AgentProjectionActivities, *args: Any, **kwargs: Any
    ) -> int:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise RuntimeError("canonical bundle crash")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AgentProjectionActivities, "_record_gateway_result", crash_before_bundle)

    try:
        with pytest.raises(RuntimeError, match="bundle crash"):
            await world.activities.run_agent_step_activity(world.params)
        assert world.effect.count == 1

        repaired = await world.activities.run_agent_step_activity(world.params)
        assert repaired.done is False
        assert len(world.client.requests) == 1
        assert world.effect.count == 1
        async with world.sessions() as session:
            run = await session.get(AgentRun, world.run_id)
            assert run is not None
            assert run.status == RunStatus.RUNNING.value
            persisted = json.dumps(
                {
                    "run": {"error_code": run.error_code, "error_message": run.error_message},
                    "events": [
                        event.payload_json
                        for event in await session.scalars(
                            select(RunEvent).where(RunEvent.run_id == world.run_id)
                        )
                    ],
                    "audits": [
                        audit.metadata_json
                        for audit in await session.scalars(
                            select(AuditEvent).where(AuditEvent.workspace_id == world.workspace_id)
                        )
                    ],
                },
                ensure_ascii=False,
            )
            assert secret not in persisted
    finally:
        redactor.clear()


async def test_terminal_gateway_commit_is_repaired_without_a_second_effect(
    phase9_world: _World,
) -> None:
    """A terminal ToolCall without its transcript bundle is replayed, not dispatched."""
    world = phase9_world
    invocation_id = stable_tool_invocation_id(world.run_id, 0, 0)
    async with world.sessions() as session:
        gateway = ToolGateway(
            ToolExecutionContext(
                session=session,
                workspace_id=world.workspace_id,
                task_id=world.task_id,
                run_id=world.run_id,
                agent_id=world.agent_id,
                agent_name="Reliable agent",
                session_factory=world.sessions,
            ),
            world.catalog,
        )
        terminal = await gateway.request(
            _TOOL_NAME,
            '{"value":"once"}',
            provider_call_id="provider-call-before-bundle",
            invocation_id=invocation_id,
        )
        await session.commit()
    assert terminal.status == "executed"
    assert world.effect.count == 1
    assert await _rows(world.sessions, Message, run_id=world.run_id) == []
    assert await _rows(world.sessions, RunEvent, run_id=world.run_id) == []

    world.client.responses.append(
        _tool_response("provider-call-for-repair", request_id="request-for-repair")
    )
    result = await world.activities.run_agent_step_activity(world.params)

    assert result.done is False
    assert world.effect.count == 1
    assert len(world.client.requests) == 1
    await _assert_one_canonical_bundle(world, provider_call_id="provider-call-for-repair")


async def test_lost_activity_response_returns_marker_without_calling_model_again(
    phase9_world: _World,
) -> None:
    """Once the outer bundle commits, a lost response is a pure marker replay."""
    world = phase9_world
    world.client.responses.append(
        _tool_response("provider-call-committed", request_id="request-committed")
    )

    first = await world.activities.run_agent_step_activity(world.params)
    message_count = len(await _rows(world.sessions, Message, run_id=world.run_id))
    event_count = len(await _rows(world.sessions, RunEvent, run_id=world.run_id))
    publish_count = len(world.publisher.events)
    replay = await world.activities.run_agent_step_activity(world.params)

    assert replay == first
    assert world.effect.count == 1
    assert len(world.client.requests) == 1
    assert len(await _rows(world.sessions, Message, run_id=world.run_id)) == message_count
    assert len(await _rows(world.sessions, RunEvent, run_id=world.run_id)) == event_count
    assert len(world.publisher.events) == publish_count
    await _assert_one_canonical_bundle(world, provider_call_id="provider-call-committed")


async def test_execution_unknown_commits_marker_and_stops_retry_before_model(
    phase9_world: _World,
) -> None:
    """An ambiguous external result becomes a durable, non-retryable stop."""
    world = phase9_world
    world.effect.fail_after_effect = True
    world.client.responses.append(
        _calls_response(
            [(_TOOL_NAME, "ambiguous"), (_TOOL_NAME, "must-not-run")],
            request_id="request-unknown",
        )
    )

    with pytest.raises(ApplicationError) as first_error:
        await world.activities.run_agent_step_activity(world.params)
    assert first_error.value.type == "tool_execution_unknown"
    assert first_error.value.non_retryable is True

    expected_invocation_id = stable_tool_invocation_id(world.run_id, 0, 0)
    async with world.sessions() as session:
        tool_call = await session.get(ToolCall, expected_invocation_id)
        assert tool_call is not None
        assert tool_call.status == ToolCallStatus.EXECUTION_UNKNOWN.value
        assert (
            await session.scalar(
                select(func.count(ToolCall.id)).where(ToolCall.run_id == world.run_id)
            )
            == 1
        )
        assert await session.get(ToolCall, stable_tool_invocation_id(world.run_id, 0, 1)) is None
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED.value
        assert run.error_code == "tool_execution_unknown"
        markers = list(
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == world.run_id,
                    RunEvent.event_type == "agent.step.committed",
                )
            )
        )
        assert len(markers) == 1
        assert markers[0].payload_json["result"]["execution_unknown_tool_call_id"] == str(
            expected_invocation_id
        )
        messages = list(
            await session.scalars(
                select(Message).where(
                    Message.run_id == world.run_id,
                    Message.message_type.in_(("tool_call", "tool_result")),
                )
            )
        )
        assert len(messages) == 2
        assert {message.content_json["tool_call_id"] for message in messages} == {
            str(expected_invocation_id)
        }
        result_message = next(
            message for message in messages if message.message_type == "tool_result"
        )
        assert result_message.content_json["status"] == "execution_unknown"

    with pytest.raises(ApplicationError) as retry_error:
        await world.activities.run_agent_step_activity(world.params)
    assert retry_error.value.type == "tool_execution_unknown"
    assert retry_error.value.non_retryable is True
    assert world.effect.count == 1
    assert len(world.client.requests) == 1


async def test_generic_step_failure_finalization_preserves_execution_unknown_diagnosis(
    phase9_world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workflow finalization must not replace a more specific durable stop."""
    world = phase9_world
    world.effect.fail_after_effect = True
    world.client.responses.append(
        _tool_response("provider-call-finalized-unknown", request_id="request-finalized-unknown")
    )

    with pytest.raises(ApplicationError) as activity_error:
        await world.activities.run_agent_step_activity(world.params)
    assert activity_error.value.type == "tool_execution_unknown"
    assert activity_error.value.non_retryable is True

    async def sandbox_cleanup(_workspace: str) -> bool:
        return False

    monkeypatch.setattr(activities_module, "delete_sandbox_workspace", sandbox_cleanup)
    await world.activities.finalize_run_activity(
        FinalizeInput(
            workspace_id=str(world.workspace_id),
            task_id=str(world.task_id),
            run_id=str(world.run_id),
            status=RunStatus.FAILED.value,
            steps_used=0,
            error_code="step_failed",
            error_message="the workflow observed a generic step activity failure",
        )
    )

    expected_invocation_id = stable_tool_invocation_id(world.run_id, 0, 0)
    async with world.sessions() as session:
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        assert run.error_code == "tool_execution_unknown"
        assert run.error_message is not None
        assert str(expected_invocation_id) in run.error_message
        assert "manual reconciliation" in run.error_message.lower()


async def test_tool_call_limit_fails_before_any_gateway_effect(phase9_world: _World) -> None:
    """A provider response above the hard call cap never enters the gateway."""
    world = phase9_world
    tool_calls = tuple(
        ModelToolCall(
            id=f"provider-call-{ordinal}",
            name=_TOOL_NAME,
            arguments_json='{"value":"must-not-run"}',
        )
        for ordinal in range(MAX_TOOL_CALLS_PER_STEP + 1)
    )
    world.client.responses.append(
        ModelResponse(
            text="",
            finish_reason="tool_calls",
            model="phase9-test-model",
            usage=ModelUsage(input_tokens=99, output_tokens=7, cached_tokens=3),
            latency_ms=8,
            provider_request_id="request-over-limit",
            tool_calls=tool_calls,
        )
    )

    with pytest.raises(ApplicationError) as error:
        await world.activities.run_agent_step_activity(world.params)

    assert error.value.type == "tool_call_limit_exceeded"
    assert error.value.non_retryable is True
    assert world.effect.count == 0
    assert len(world.client.requests) == 1
    async with world.sessions() as session:
        assert await session.scalar(select(func.count(ToolCall.id))) == 0
        assert await session.scalar(select(func.count(Message.id))) == 0
        assert await session.scalar(select(func.count(RunEvent.id))) == 0
        run = await session.get(AgentRun, world.run_id)
        assert run is not None
        assert run.input_tokens == 0
        assert run.output_tokens == 0
        assert run.cached_tokens == 0
        assert run.estimated_cost_micros == 0
        assert run.steps_used == 0
