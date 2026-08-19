"""Definition-only catalog and live tool advertisement contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import jhin_tool_worker.main as main_module
import jhin_tool_worker.resources as resources_module
from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, Workspace
from jhin_domain import new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tool_worker.activities import ToolActivities
from jhin_tool_worker.resources import ToolWorkerResources
from jhin_tool_worker.settings import ToolWorkerSettings
from jhin_tools import TOOL_AFTER_CLAIM, ToolCatalog, ToolExecutionContext
from jhin_workflows.agent_task.shared import ResolveAdvertisedToolsInput


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _Output(BaseModel):
    value: str


async def _executor(_ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    return _Output(value=str(payload.model_dump()["value"]))


def _definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Definition for {name}",
        risk=RiskLevel.READ,
        input_model=_Input,
        output_model=_Output,
        required_capability=name,
    )


@dataclass
class _Resources:
    session_factory: async_sessionmaker[AsyncSession]
    crypto: None = None
    test_barrier: None = None


@dataclass
class _DisposableEngine:
    dispose_count: int = 0

    async def dispose(self) -> None:
        self.dispose_count += 1


@dataclass
class _DrainableNats:
    drain_count: int = 0
    fail_drain: bool = False

    def jetstream(self) -> object:
        return object()

    async def drain(self) -> None:
        self.drain_count += 1
        if self.fail_drain:
            raise RuntimeError("drain failed")


@dataclass
class _ClosableResources:
    close_count: int = 0

    async def close(self) -> None:
        self.close_count += 1


@pytest.fixture
async def advertised_world() -> AsyncIterator[tuple[ToolActivities, Workspace, Agent]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    catalog = ToolCatalog()
    for name in ("system.echo", "linear.issue.get", "system.time"):
        catalog.register(_definition(name), _executor)

    async with sessions() as session:
        workspace = Workspace(name="Advertise", slug=f"advertise-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Catalog agent", slug="catalog-agent")
        session.add(agent)
        await session.flush()
        for capability in ("system.echo", "linear.issue.get"):
            session.add(
                AgentCapabilityGrant(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    capability=capability,
                    scope_json={},
                    effect="allow",
                )
            )
        await session.commit()

    yield ToolActivities(_Resources(sessions), catalog), workspace, agent  # type: ignore[arg-type]
    await engine.dispose()


async def test_advertisement_uses_live_grants_and_preserves_catalog_order(
    advertised_world: tuple[ToolActivities, Workspace, Agent],
) -> None:
    activities, workspace, agent = advertised_world

    advertised = await activities.resolve_advertised_tools_activity(
        ResolveAdvertisedToolsInput(workspace_id=str(workspace.id), agent_id=str(agent.id))
    )

    assert [tool.name for tool in advertised] == ["system.echo", "linear.issue.get"]
    assert advertised[0].parameters["type"] == "object"


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("JHIN_TEST_CRASH_BARRIER_DIR", "/run/jhin/test-barriers"),
        ("JHIN_TEST_CRASH_BARRIER_NAME", TOOL_AFTER_CLAIM),
        ("JHIN_TEST_CRASH_BARRIER_MATCH", "018f4d52-8b93-7d41-8ac7-7f190f091111"),
    ],
)
def test_tool_worker_rejects_crash_barrier_in_production(
    setting: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(setting, value)

    with pytest.raises(ValidationError, match="test crash barriers are forbidden"):
        ToolWorkerSettings()


def test_tool_worker_settings_ignore_unrelated_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = f"unrelated-{new_uuid7()}"
    monkeypatch.setenv("JHIN_UNRELATED_TEST_SETTING", marker)

    settings = ToolWorkerSettings()

    assert settings.app_env == "development"
    assert marker not in repr(settings)


@pytest.mark.parametrize("failure_stage", ["connect", "ensure_streams", "master_key"])
async def test_partial_resource_creation_closes_every_acquired_resource(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _DisposableEngine()
    nats_connection = _DrainableNats()

    monkeypatch.setattr(resources_module, "create_engine", lambda _url: engine)
    monkeypatch.setattr(resources_module, "create_session_factory", lambda _engine: object())

    async def connect(_url: str) -> _DrainableNats:
        if failure_stage == "connect":
            raise RuntimeError("connect failed")
        return nats_connection

    async def ensure_streams(_jetstream: object) -> None:
        if failure_stage == "ensure_streams":
            raise RuntimeError("ensure streams failed")

    def load_master_key() -> bytes:
        if failure_stage == "master_key":
            raise RuntimeError("master key failed")
        return b"0" * 32

    monkeypatch.setattr(resources_module.nats, "connect", connect)
    monkeypatch.setattr(resources_module, "ensure_streams", ensure_streams)
    monkeypatch.setattr(resources_module, "load_master_key", load_master_key)

    with pytest.raises(RuntimeError, match=failure_stage.replace("_", " ")):
        await ToolWorkerResources.create(ToolWorkerSettings())

    assert engine.dispose_count == 1
    assert nats_connection.drain_count == (0 if failure_stage == "connect" else 1)


async def test_resource_close_disposes_engine_when_nats_drain_fails() -> None:
    engine = _DisposableEngine()
    nats_connection = _DrainableNats(fail_drain=True)
    resources = ToolWorkerResources(
        engine=engine,  # type: ignore[arg-type]
        session_factory=object(),  # type: ignore[arg-type]
        nats_connection=nats_connection,  # type: ignore[arg-type]
        publisher=object(),  # type: ignore[arg-type]
        crypto=object(),  # type: ignore[arg-type]
        test_barrier=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="drain failed"):
        await resources.close()

    assert nats_connection.drain_count == 1
    assert engine.dispose_count == 1


@pytest.mark.parametrize("failure_stage", ["catalog", "worker"])
async def test_main_closes_resources_after_post_acquisition_construction_failure(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _ClosableResources()
    settings = ToolWorkerSettings()

    async def connect_with_retry(_settings: ToolWorkerSettings) -> object:
        return object()

    async def resources_with_retry(_settings: ToolWorkerSettings) -> _ClosableResources:
        return resources

    def build_catalog() -> ToolCatalog:
        if failure_stage == "catalog":
            raise RuntimeError("catalog failed")
        return ToolCatalog()

    def construct_worker(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("worker failed")

    class _SignalLoop:
        def add_signal_handler(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(main_module, "ToolWorkerSettings", lambda: settings)
    monkeypatch.setattr(main_module, "configure_current_logging", lambda _level: None)
    monkeypatch.setattr(main_module, "connect_with_retry", connect_with_retry)
    monkeypatch.setattr(main_module, "resources_with_retry", resources_with_retry)
    monkeypatch.setattr(main_module, "build_default_catalog", build_catalog)
    monkeypatch.setattr(main_module, "Worker", construct_worker)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _SignalLoop())

    with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
        await main_module.main()

    assert resources.close_count == 1
