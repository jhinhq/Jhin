from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import jhin_agent_worker.reasoning as reasoning_module
from jhin_agent_worker.reasoning import AgentReasoningActivities
from jhin_agent_worker.settings import Settings
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, Task, Workspace
from jhin_domain import RunStatus, new_uuid7
from jhin_models import ModelRequest, ModelResponse, ModelToolCall, ModelUsage
from jhin_tools import (
    AGENT_BEFORE_BIND,
    PHASE9_AFTER_MANIFEST,
    PHASE9_CLEANUP_BEFORE_EFFECT,
    PHASE9_SYNC_BEFORE_EFFECT,
    TOOL_AFTER_CLAIM,
    TOOL_AFTER_EFFECT,
    TOOL_BEFORE_CLAIM,
    CrashBarrier,
    CrashBarrierConfig,
    CrashBarrierName,
    release_barrier,
)
from jhin_workflows.agent_task.shared import AdvertisedTool, ReasonAgentStepInput

_TOOL_NAME = "test.phase9.barrier_effect"
_IDENTITY = UUID("018f4d52-8b93-7d41-8ac7-7f190f091111")
_BARRIER_ENVIRONMENT = (
    "APP_ENV",
    "JHIN_TEST_CRASH_BARRIER_DIR",
    "JHIN_TEST_CRASH_BARRIER_NAME",
    "JHIN_TEST_CRASH_BARRIER_MATCH",
)


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,  # noqa: ASYNC109
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def clear_barrier_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _BARRIER_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("app_env", ["production", "prod"])
@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("JHIN_TEST_CRASH_BARRIER_DIR", "/run/jhin/test-barriers"),
        ("JHIN_TEST_CRASH_BARRIER_NAME", PHASE9_AFTER_MANIFEST),
        ("JHIN_TEST_CRASH_BARRIER_MATCH", "018f4d52-8b93-7d41-8ac7-7f190f091111"),
    ],
)
def test_agent_rejects_test_barrier_configuration_in_production(
    app_env: str, setting: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValidationError, match="test crash barriers are forbidden"):
        Settings()


def test_agent_maps_test_barrier_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JHIN_TEST_CRASH_BARRIER_DIR", str(tmp_path))
    monkeypatch.setenv("JHIN_TEST_CRASH_BARRIER_NAME", TOOL_AFTER_CLAIM)
    monkeypatch.setenv("JHIN_TEST_CRASH_BARRIER_MATCH", str(_IDENTITY))

    settings = Settings()

    assert settings.test_crash_barrier_dir == tmp_path
    assert settings.test_crash_barrier_name == TOOL_AFTER_CLAIM
    assert settings.test_crash_barrier_match == _IDENTITY


def test_agent_normalizes_empty_test_barrier_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JHIN_TEST_CRASH_BARRIER_DIR", "")
    monkeypatch.setenv("JHIN_TEST_CRASH_BARRIER_NAME", "")
    monkeypatch.setenv("JHIN_TEST_CRASH_BARRIER_MATCH", "")

    settings = Settings()

    assert settings.test_crash_barrier_dir is None
    assert settings.test_crash_barrier_name is None
    assert settings.test_crash_barrier_match is None


class _Publisher:
    async def publish(self, envelope: Any) -> None:
        assert envelope


class _ModelClient:
    def __init__(self) -> None:
        self.responses: list[ModelResponse] = []
        self.order: list[str] | None = None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        assert request
        if self.order is not None:
            self.order.append("model_returned")
        if not self.responses:
            raise AssertionError("a model response was not configured")
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


@dataclass
class _Resources:
    session_factory: async_sessionmaker[AsyncSession]
    test_barrier: Any
    publisher: _Publisher
    crypto: None = None


class _RecordingBarrier:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def arrive_and_wait(self, name: CrashBarrierName, identity: UUID) -> None:
        assert identity
        if name in {AGENT_BEFORE_BIND, PHASE9_AFTER_MANIFEST}:
            self._order.append(name)


class _BarrierReasoningActivities(AgentReasoningActivities):
    def __init__(self, resources: Any) -> None:
        super().__init__(resources)
        self.commit_order: list[str] | None = None

    async def _after_reasoning_bind_commit(self) -> None:
        if self.commit_order is not None:
            self.commit_order.append("manifest_committed")


@dataclass
class Phase9BarrierWorld:
    root: Path
    resources: _Resources
    reasoning: _BarrierReasoningActivities
    client: _ModelClient
    effects: dict[CrashBarrierName, int]
    run_id: UUID
    params: ReasonAgentStepInput
    order: list[str]

    def _configure(self, name: CrashBarrierName) -> None:
        self.resources.test_barrier = CrashBarrier(
            CrashBarrierConfig(root=self.root, selected=name, match_identity=self.run_id)
        )

    def _model_response(self) -> ModelResponse:
        return ModelResponse(
            text="perform the effect",
            finish_reason="tool_calls",
            model="barrier-test-model",
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            latency_ms=1,
            provider_request_id="barrier-request",
            tool_calls=(
                ModelToolCall(
                    id="barrier-provider-call",
                    name=_TOOL_NAME,
                    arguments_json='{"value":"once"}',
                ),
            ),
        )

    async def invoke(self, name: CrashBarrierName) -> None:
        self._configure(name)
        if name == PHASE9_AFTER_MANIFEST:
            self.client.responses.append(self._model_response())
            await self.reasoning.reason_agent_step_activity(self.params)
        elif name in {PHASE9_SYNC_BEFORE_EFFECT, PHASE9_CLEANUP_BEFORE_EFFECT}:
            # These two historical effects moved to the tool worker.  The frozen
            # upgrade harness retains their ordering proof with a test-local effect.
            await self.resources.test_barrier.arrive_and_wait(name, self.run_id)
        else:
            raise AssertionError(f"unsupported Phase 9 barrier {name}")
        self.effects[name] += 1

    async def wait_arrived(self, name: CrashBarrierName) -> None:
        marker = self.root / name / f"{self.run_id}.arrived"
        await wait_until(marker.exists)

    def release(self, name: CrashBarrierName) -> None:
        release_barrier(self.root, name, self.run_id)

    def effect_count(self, name: CrashBarrierName) -> int:
        return self.effects[name]

    async def invoke_reasoning_with_recording_barrier(self) -> None:
        self.order.clear()
        self.client.order = self.order
        self.client.responses.append(self._model_response())
        self.resources.test_barrier = _RecordingBarrier(self.order)
        self.reasoning.commit_order = self.order
        try:
            await self.reasoning.reason_agent_step_activity(self.params)
        finally:
            self.reasoning.commit_order = None
            self.client.order = None
        self.order.append("activity_returned")


@pytest.fixture
async def phase9_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Phase9BarrierWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    resources = _Resources(
        session_factory=sessions,
        test_barrier=CrashBarrier(CrashBarrierConfig()),
        publisher=_Publisher(),
    )
    reasoning = _BarrierReasoningActivities(cast(Any, resources))
    client = _ModelClient()
    monkeypatch.setattr(reasoning_module, "build_model_client", lambda *_a, **_kw: client)

    async with sessions() as session:
        workspace = Workspace(name="Barrier workspace", slug=f"barrier-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Barrier agent", slug="barrier-agent")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Exercise Phase 9 boundaries",
            description="Exercise Phase 9 boundaries",
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
            provider_type="barrier-test",
            base_url=None,
            secret_id=None,
            model_name="barrier-test-model",
            display_name="Barrier test model",
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
                description="A deterministic barrier effect.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            )
        ],
    )
    yield Phase9BarrierWorld(
        root=tmp_path,
        resources=resources,
        reasoning=reasoning,
        client=client,
        effects={
            PHASE9_AFTER_MANIFEST: 0,
            PHASE9_SYNC_BEFORE_EFFECT: 0,
            PHASE9_CLEANUP_BEFORE_EFFECT: 0,
        },
        run_id=run.id,
        params=params,
        order=[],
    )
    await engine.dispose()


@pytest.mark.parametrize(
    "name",
    [PHASE9_AFTER_MANIFEST, PHASE9_SYNC_BEFORE_EFFECT, PHASE9_CLEANUP_BEFORE_EFFECT],
)
async def test_phase9_barrier_arrives_before_effect(
    phase9_world: Phase9BarrierWorld, name: CrashBarrierName
) -> None:
    waiting = asyncio.create_task(phase9_world.invoke(name))
    try:
        await phase9_world.wait_arrived(name)
        assert phase9_world.effect_count(name) == 0
        phase9_world.release(name)
        await waiting
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    assert phase9_world.effect_count(name) == 1


async def test_agent_bind_barriers_bracket_only_the_manifest_commit(
    phase9_world: Phase9BarrierWorld,
) -> None:
    await phase9_world.invoke_reasoning_with_recording_barrier()
    assert phase9_world.order == [
        "model_returned",
        AGENT_BEFORE_BIND,
        "manifest_committed",
        PHASE9_AFTER_MANIFEST,
        "activity_returned",
    ]


def test_all_phase10_boundary_names_are_accepted_by_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        AGENT_BEFORE_BIND,
        PHASE9_AFTER_MANIFEST,
        PHASE9_SYNC_BEFORE_EFFECT,
        PHASE9_CLEANUP_BEFORE_EFFECT,
        TOOL_BEFORE_CLAIM,
        TOOL_AFTER_CLAIM,
        TOOL_AFTER_EFFECT,
    )

    for name in names:
        monkeypatch.setenv("JHIN_TEST_CRASH_BARRIER_NAME", name)
        assert Settings().test_crash_barrier_name == name
