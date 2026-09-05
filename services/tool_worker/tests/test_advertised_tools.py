"""Definition-only catalog and live tool advertisement contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import jhin_tool_worker.main as main_module
import jhin_tool_worker.resources as resources_module
from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, Connection, Task, Workspace
from jhin_db.models.connection import new_public_id
from jhin_domain import ConnectionStatus, new_uuid7
from jhin_observability import ObservabilityRuntime, noop_metrics, noop_tracer
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
    runtime: object = field(
        default_factory=lambda: SimpleNamespace(metrics=noop_metrics(), tracer=noop_tracer())
    )
    crypto: None = None
    test_barrier: None = None


@dataclass
class _DisposableEngine:
    dispose_count: int = 0
    events: list[str] | None = None
    failure: BaseException | None = None

    async def dispose(self) -> None:
        self.dispose_count += 1
        if self.events is not None:
            self.events.append("engine.dispose")
        if self.failure is not None:
            raise self.failure


@dataclass
class _DrainableNats:
    drain_count: int = 0
    fail_drain: bool = False
    events: list[str] | None = None
    failure: BaseException | None = None

    def jetstream(self) -> object:
        if self.events is not None:
            self.events.append("nats.jetstream")
        return object()

    async def drain(self) -> None:
        self.drain_count += 1
        if self.events is not None:
            self.events.append("nats.drain")
        if self.fail_drain:
            raise RuntimeError("drain failed")
        if self.failure is not None:
            raise self.failure


@dataclass
class _ClosableResources:
    runtime: ObservabilityRuntime
    close_count: int = 0

    async def close(self) -> None:
        self.close_count += 1


@dataclass
class _AdvertisedWorld:
    activities: ToolActivities
    workspace: Workspace
    agent: Agent
    sessions: async_sessionmaker[AsyncSession]

    async def add_task(self, **values: Any) -> Task:
        async with self.sessions() as session:
            task = Task(
                workspace_id=self.workspace.id,
                title="Task",
                assigned_agent_id=self.agent.id,
                correlation_id=new_uuid7(),
                **values,
            )
            session.add(task)
            await session.commit()
            return task

    async def advertised(self, task: Task | None = None) -> list[str]:
        tools = await self.activities.resolve_advertised_tools_activity(
            ResolveAdvertisedToolsInput(
                workspace_id=str(self.workspace.id),
                agent_id=str(self.agent.id),
                task_id="" if task is None else str(task.id),
            )
        )
        return [tool.name for tool in tools]


@pytest.fixture
async def advertised_world() -> AsyncIterator[_AdvertisedWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    catalog = ToolCatalog()
    for name in ("system.echo", "linear.issue.get", "system.time"):
        catalog.register(_definition(name), _executor)
    catalog.register(_definition("organization.report_result"), _executor)

    async with sessions() as session:
        workspace = Workspace(name="Advertise", slug=f"advertise-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Catalog agent", slug="catalog-agent")
        session.add(agent)
        await session.flush()
        for capability in ("system.echo", "linear.issue.get", "organization.report_result"):
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

    yield _AdvertisedWorld(
        activities=ToolActivities(_Resources(sessions), catalog),  # type: ignore[arg-type]
        workspace=workspace,
        agent=agent,
        sessions=sessions,
    )
    await engine.dispose()


async def test_advertisement_uses_live_grants_and_preserves_catalog_order(
    advertised_world: _AdvertisedWorld,
) -> None:
    tools = await advertised_world.activities.resolve_advertised_tools_activity(
        ResolveAdvertisedToolsInput(
            workspace_id=str(advertised_world.workspace.id),
            agent_id=str(advertised_world.agent.id),
        )
    )

    assert [tool.name for tool in tools] == [
        "system.echo",
        "linear.issue.get",
        "organization.report_result",
    ]
    assert tools[0].parameters["type"] == "object"


async def test_reporting_is_advertised_for_delegated_and_work_request_children(
    advertised_world: _AdvertisedWorld,
) -> None:
    """Phase 8 delegation/QA depends on the child reporting back, so the tool
    stays advertised there — including when the child hangs off a chat."""
    parent = await advertised_world.add_task()
    delegated = await advertised_world.add_task(
        parent_task_id=parent.id,
        metadata_json={"origin": "delegation", "delegation": {"kind": "review_request"}},
    )
    work_request = await advertised_world.add_task(
        conversation_id=None,
        metadata_json={"origin": "work_request", "work_request": {"id": str(new_uuid7())}},
    )
    standalone = await advertised_world.add_task(metadata_json={})

    for task in (delegated, work_request, standalone):
        assert "organization.report_result" in await advertised_world.advertised(task)


async def test_reporting_is_withheld_from_a_plain_conversation_turn(
    advertised_world: _AdvertisedWorld,
) -> None:
    """In a chat with a person the reply is the deliverable: offering a
    "report your result" tool invites the model to file a report instead of
    answering. The grant is untouched — only the advertisement narrows."""
    conversation_turn = await advertised_world.add_task(
        metadata_json={"origin": "conversation", "conversation_id": str(new_uuid7())},
    )

    names = await advertised_world.advertised(conversation_turn)

    assert "organization.report_result" not in names
    assert names == ["system.echo", "linear.issue.get"]
    async with advertised_world.sessions() as session:
        grants = list(
            await session.scalars(
                select(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == advertised_world.agent.id,
                    AgentCapabilityGrant.capability == "organization.report_result",
                )
            )
        )
    assert [grant.effect for grant in grants] == ["allow"]


async def test_an_unknown_task_keeps_every_granted_tool(
    advertised_world: _AdvertisedWorld,
) -> None:
    missing = ResolveAdvertisedToolsInput(
        workspace_id=str(advertised_world.workspace.id),
        agent_id=str(advertised_world.agent.id),
        task_id=str(new_uuid7()),
    )

    tools = await advertised_world.activities.resolve_advertised_tools_activity(missing)

    assert "organization.report_result" in [tool.name for tool in tools]


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

    assert settings.app_env == "dev"
    assert marker not in repr(settings)


async def test_partial_resource_creation_closes_every_acquired_resource() -> None:
    # Loop-folded from a 3x11 parametrize cross-product (failure_kind x
    # failure_stage): the full matrix still runs, each case inside its own
    # MonkeyPatch context so patches (including the stage-specific logger and
    # dataclass patches) are undone between cases exactly as the fixture did;
    # every failure message names the (stage, kind) case.
    for failure_kind in ("error", "base", "cancel"):
        for failure_stage in (
            "engine",
            "session",
            "connect",
            "jetstream",
            "ensure_streams",
            "master_key",
            "crypto",
            "barrier",
            "publisher",
            "dataclass",
            "logger",
        ):
            with pytest.MonkeyPatch.context() as monkeypatch:
                await _assert_partial_creation_closes_every_acquired_resource(
                    failure_stage, failure_kind, monkeypatch
                )


async def _assert_partial_creation_closes_every_acquired_resource(
    failure_stage: str,
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = f"[failure_stage={failure_stage} failure_kind={failure_kind}]"
    events: list[str] = []
    engine = _DisposableEngine(events=events)
    nats_connection = _DrainableNats(events=events)
    tracer = object()
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(tracer=tracer, metrics=object()),
    )
    engine_tracers: list[object] = []
    failure: BaseException
    if failure_kind == "error":
        failure = RuntimeError(f"{failure_stage} failed")
    elif failure_kind == "base":
        failure = BaseException(f"{failure_stage} failed")
    else:
        failure = asyncio.CancelledError(f"{failure_stage} failed")
    settings = ToolWorkerSettings()
    settings_before = settings.model_dump()

    def create_engine(_url: str, *, tracer: object) -> _DisposableEngine:
        engine_tracers.append(tracer)
        events.append("engine.create")
        if failure_stage == "engine":
            raise failure
        return engine

    monkeypatch.setattr(resources_module, "create_engine", create_engine)

    def create_sessions(_engine: object) -> object:
        events.append("session.create")
        if failure_stage == "session":
            raise failure
        return object()

    monkeypatch.setattr(resources_module, "create_session_factory", create_sessions)

    async def connect(_url: str) -> _DrainableNats:
        events.append("nats.connect")
        if failure_stage == "connect":
            raise failure
        return nats_connection

    original_jetstream = nats_connection.jetstream

    def jetstream() -> object:
        if failure_stage == "jetstream":
            events.append("nats.jetstream")
            raise failure
        return original_jetstream()

    nats_connection.jetstream = jetstream  # type: ignore[method-assign]

    async def ensure_streams(_jetstream: object) -> None:
        events.append("streams.ensure")
        if failure_stage == "ensure_streams":
            raise failure

    def load_master_key() -> bytes:
        events.append("key.load")
        if failure_stage == "master_key":
            raise failure
        return b"0" * 32

    class Crypto:
        def __init__(self, _key: bytes) -> None:
            events.append("crypto.create")
            if failure_stage == "crypto":
                raise failure

    class Barrier:
        def __init__(self, _config: object) -> None:
            events.append("barrier.create")
            if failure_stage == "barrier":
                raise failure

    class Publisher:
        def __init__(self, _js: object, *, tracer: object) -> None:
            events.append("publisher.create")
            assert tracer is runtime.tracer, case
            if failure_stage == "publisher":
                raise failure

    monkeypatch.setattr(resources_module.nats, "connect", connect)
    monkeypatch.setattr(resources_module, "ensure_streams", ensure_streams)
    monkeypatch.setattr(resources_module, "load_master_key", load_master_key)
    monkeypatch.setattr(resources_module, "SecretCrypto", Crypto)
    monkeypatch.setattr(resources_module, "CrashBarrier", Barrier)
    monkeypatch.setattr(resources_module, "EventPublisher", Publisher)
    if failure_stage == "logger":
        monkeypatch.setattr(
            resources_module.logger,
            "info",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
        )
    if failure_stage == "dataclass":
        monkeypatch.setattr(
            resources_module.ToolWorkerResources,
            "__init__",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
        )

    caught: BaseException | None = None
    try:
        await ToolWorkerResources.create(settings, runtime=runtime)
    except BaseException as error:
        caught = error

    assert caught is failure, f"{case} caught={caught!r}"
    assert settings.model_dump() == settings_before, case
    assert runtime.tracer is tracer, case
    assert engine.dispose_count == (0 if failure_stage == "engine" else 1), (
        f"{case} dispose_count={engine.dispose_count}"
    )
    assert nats_connection.drain_count == (
        0 if failure_stage in {"engine", "session", "connect"} else 1
    ), f"{case} drain_count={nats_connection.drain_count}"
    if failure_stage not in {"engine", "session", "connect"}:
        assert events[-2:] == ["nats.drain", "engine.dispose"], f"{case} events={events}"
    assert engine_tracers == [tracer], case


@pytest.mark.asyncio
async def test_partial_factory_cleanup_failures_never_mask_the_active_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    acquisition_error = RuntimeError("publisher failed")
    engine = _DisposableEngine(
        events=events,
        failure=asyncio.CancelledError("dispose cleanup failed"),
    )
    nats_connection = _DrainableNats(
        events=events,
        failure=BaseException("drain cleanup failed"),
    )
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(tracer=object(), metrics=object()),
    )
    monkeypatch.setattr(resources_module, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(resources_module, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(
        resources_module.nats,
        "connect",
        lambda _url: _async_value(nats_connection),
    )
    monkeypatch.setattr(resources_module, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(resources_module, "load_master_key", lambda: b"0" * 32)
    monkeypatch.setattr(
        resources_module,
        "EventPublisher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(acquisition_error),
    )

    caught: BaseException | None = None
    try:
        await ToolWorkerResources.create(ToolWorkerSettings(), runtime=runtime)
    except BaseException as error:
        caught = error
    assert caught is acquisition_error
    assert events[-2:] == ["nats.drain", "engine.dispose"]


async def test_resource_close_disposes_engine_when_nats_drain_fails() -> None:
    engine = _DisposableEngine()
    nats_connection = _DrainableNats(fail_drain=True)
    resources = ToolWorkerResources(
        runtime=cast(ObservabilityRuntime, object()),
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
    settings = ToolWorkerSettings()
    shutdowns: list[int] = []
    tracer = object()
    metrics = object()
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            tracer=tracer,
            metrics=metrics,
            shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
        ),
    )
    resources = _ClosableResources(runtime=runtime)
    construction_calls: list[str] = []
    construction_error = RuntimeError(f"{failure_stage} failed")

    async def connect_with_retry(_settings: ToolWorkerSettings, received_runtime: object) -> object:
        assert received_runtime is runtime
        return object()

    async def resources_with_retry(
        _settings: ToolWorkerSettings, received_runtime: object
    ) -> _ClosableResources:
        assert received_runtime is runtime
        assert resources.runtime is runtime
        assert resources.runtime.metrics is metrics
        assert resources.runtime.tracer is tracer
        return resources

    def build_catalog() -> ToolCatalog:
        construction_calls.append("catalog")
        if failure_stage == "catalog":
            raise construction_error
        return ToolCatalog()

    def construct_worker(*_args: object, **_kwargs: object) -> object:
        construction_calls.append("worker")
        raise construction_error

    class _SignalLoop:
        def add_signal_handler(self, *_args: object) -> None:
            return None

        def remove_signal_handler(self, *_args: object) -> bool:
            return True

    monkeypatch.setattr(main_module, "ToolWorkerSettings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "connect_with_retry", connect_with_retry)
    monkeypatch.setattr(main_module, "resources_with_retry", resources_with_retry)
    monkeypatch.setattr(main_module, "build_default_catalog", build_catalog)
    monkeypatch.setattr(main_module, "build_temporal_worker", construct_worker)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _SignalLoop())

    with pytest.raises(RuntimeError, match=f"{failure_stage} failed") as caught:
        await main_module.main()

    assert caught.value is construction_error
    assert construction_calls == (
        ["catalog"] if failure_stage == "catalog" else ["catalog", "worker"]
    )
    assert resources.runtime is runtime
    assert resources.runtime.metrics is metrics
    assert resources.runtime.tracer is tracer
    assert resources.close_count == 1
    assert shutdowns == [5_000]


@pytest.mark.asyncio
async def test_resource_graph_retains_runtime_and_tracer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _DisposableEngine()
    nats_connection = _DrainableNats()
    tracer = object()
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(tracer=tracer, metrics=object()),
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        resources_module,
        "create_engine",
        lambda _url, *, tracer: captured.update(engine_tracer=tracer) or engine,
    )
    monkeypatch.setattr(resources_module, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(
        resources_module.nats, "connect", lambda _url: _async_value(nats_connection)
    )
    monkeypatch.setattr(resources_module, "ensure_streams", lambda _js: _async_value(None))
    monkeypatch.setattr(resources_module, "load_master_key", lambda: b"0" * 32)

    class Publisher:
        def __init__(self, _js: object, *, tracer: object) -> None:
            captured["publisher_tracer"] = tracer
            self._tracer = tracer

    monkeypatch.setattr(resources_module, "EventPublisher", Publisher)
    resources = await ToolWorkerResources.create(ToolWorkerSettings(), runtime=runtime)
    assert resources.runtime is runtime
    assert captured == {"engine_tracer": tracer, "publisher_tracer": tracer}
    assert cast(Any, resources.publisher)._tracer is tracer
    await resources.close()


async def _async_value(value: object) -> object:
    return value


async def test_a_grant_pinned_to_a_missing_or_disabled_connection_is_not_advertised(
    advertised_world: _AdvertisedWorld,
) -> None:
    """The engineer's four linear rows pointed at a connection that no longer
    existed and were still offered. Only an ACTIVE pin advertises; the grant
    row itself is untouched, so re-enabling the app brings the tool back."""
    async with advertised_world.sessions() as session:
        connection = Connection(
            workspace_id=advertised_world.workspace.id,
            connector_type="linear",
            name="Linear",
            auth_type="api_key",
            public_id=new_public_id(),
            config_json={},
        )
        session.add(connection)
        await session.flush()
        pinned = await session.scalar(
            select(AgentCapabilityGrant).where(
                AgentCapabilityGrant.agent_id == advertised_world.agent.id,
                AgentCapabilityGrant.capability == "linear.issue.get",
            )
        )
        assert pinned is not None
        pinned.scope_json = {"connection_id": str(connection.id)}
        deleted = await session.scalar(
            select(AgentCapabilityGrant).where(
                AgentCapabilityGrant.agent_id == advertised_world.agent.id,
                AgentCapabilityGrant.capability == "system.echo",
            )
        )
        assert deleted is not None
        deleted.scope_json = {"connection_id": str(new_uuid7())}
        await session.commit()
        connection_id = connection.id

    names = await advertised_world.advertised()
    assert "linear.issue.get" in names
    assert "system.echo" not in names

    async with advertised_world.sessions() as session:
        row = await session.get(Connection, connection_id)
        assert row is not None
        row.status = ConnectionStatus.DISABLED.value
        await session.commit()
    assert "linear.issue.get" not in await advertised_world.advertised()

    async with advertised_world.sessions() as session:
        row = await session.get(Connection, connection_id)
        assert row is not None
        row.status = ConnectionStatus.ACTIVE.value
        await session.commit()
    assert "linear.issue.get" in await advertised_world.advertised()
    async with advertised_world.sessions() as session:
        grants = list(
            await session.scalars(
                select(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == advertised_world.agent.id
                )
            )
        )
    assert len(grants) == 3  # advertisement narrowed; nothing was revoked
