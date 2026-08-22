"""Final agent/tool worker ownership and registration contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast

import pytest

import jhin_agent_worker.activities as agent_activities_module
import jhin_agent_worker.main as agent_main
import jhin_tool_worker.main as tool_main
from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.compatibility import AgentCompatibilityActivities
from jhin_agent_worker.projections import _cancel_pending_run_approvals
from jhin_agent_worker.resources import Resources as AgentResources
from jhin_agent_worker.trigger_activities import TriggerCompatibilityActivities
from jhin_tool_worker.activities import ToolActivities
from jhin_tool_worker.cleanup_activities import CleanupActivities
from jhin_tool_worker.resources import ToolWorkerResources
from jhin_tool_worker.settings import ToolWorkerSettings
from jhin_tool_worker.trigger_activities import TriggerToolActivities
from jhin_tools import ToolCatalog
from jhin_workflows.agent_task import AgentTaskWorkflow
from jhin_workflows.avatar_generation import AvatarGenerationWorkflow
from jhin_workflows.delegated_task import DelegatedTaskWorkflow
from jhin_workflows.engineering_ticket import EngineeringTicketWorkflow
from jhin_workflows.memory_maintenance import MemoryMaintenanceWorkflow
from jhin_workflows.periodic_review import PeriodicReviewWorkflow
from jhin_workflows.tool_compat import (
    AdvertisedToolsCompatibilityWorkflow,
    ApprovalCompatibilityWorkflow,
    CleanupCompatibilityWorkflow,
    SyncExternalCompatibilityWorkflow,
    ToolStepCompatibilityWorkflow,
)
from jhin_workflows.triggered_task import TriggeredTaskWorkflow
from jhin_workflows.work_request_task import WorkRequestTaskWorkflow

TOOL_ACTIVITY_NAMES = {
    "resolve_advertised_tools",
    "execute_bound_tool",
    "resolve_bound_tool_approval",
    "resolve_bound_tool_review",
    "sync_external_tool",
    "cleanup_run_workspace",
}
TOOL_ACTIVITY_ORDER = [
    "resolve_advertised_tools",
    "execute_bound_tool",
    "resolve_bound_tool_approval",
    "resolve_bound_tool_review",
    "sync_external_tool",
    "cleanup_run_workspace",
]

AGENT_ACTIVITY_NAMES = {
    "resolve_snapshot",
    "reason_agent_step",
    "commit_agent_step",
    "commit_approval_projection",
    "commit_review_projection",
    "finalize_run_projection",
    "summarize_delegation",
    "deliver_delegation_result",
    "prepare_triggered_task",
    "resolve_engineering_plan",
    "create_engineering_child_task",
    "finalize_engineering_ticket",
    "run_agent_step",
    "resolve_approval",
    "finalize_run",
    "sync_external",
    "extract_memory_candidates",
    "apply_memory_candidates",
    "generate_avatar",
    "fail_avatar_generation",
    "finalize_work_request",
    "load_periodic_review_policy",
    "open_periodic_review",
}
AGENT_ACTIVITY_ORDER = [
    "resolve_snapshot",
    "reason_agent_step",
    "commit_agent_step",
    "commit_approval_projection",
    "commit_review_projection",
    "finalize_run_projection",
    "summarize_delegation",
    "deliver_delegation_result",
    "run_agent_step",
    "resolve_approval",
    "finalize_run",
    "prepare_triggered_task",
    "sync_external",
    "resolve_engineering_plan",
    "create_engineering_child_task",
    "finalize_engineering_ticket",
    "extract_memory_candidates",
    "apply_memory_candidates",
    "generate_avatar",
    "fail_avatar_generation",
    "finalize_work_request",
    "load_periodic_review_policy",
    "open_periodic_review",
]


class _Resources:
    def __init__(self, runtime: object) -> None:
        self.runtime = runtime
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class _ImmediateEvent:
    def set(self) -> None:
        return None

    async def wait(self) -> None:
        return None


class _SignalLoop:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.removed: list[object] = []

    def add_signal_handler(self, signal: object, *_args: object) -> None:
        self.added.append(signal)

    def remove_signal_handler(self, signal: object) -> bool:
        self.removed.append(signal)
        return True


async def _wait_for_event_or_early_task_failure(
    event: asyncio.Event,
    task: asyncio.Task[None],
) -> None:
    event_waiter = asyncio.create_task(event.wait())
    try:
        done, _pending = await asyncio.wait(
            {event_waiter, task},
            timeout=1,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done, "worker lifecycle event timed out"
        if task in done:
            await task
        assert event_waiter in done
    finally:
        if not event_waiter.done():
            event_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await event_waiter


def _activity_map(activities: list[Callable[..., Any]]) -> dict[str, Any]:
    registered: dict[str, Any] = {}
    for registered_activity in activities:
        definition = getattr(registered_activity, "__temporal_activity_definition", None)
        assert definition is not None
        assert definition.name not in registered
        registered[definition.name] = registered_activity
    return registered


def _assert_resource_runtime_identity(resources: object, runtime: object) -> None:
    assert cast(Any, resources).runtime is runtime
    assert cast(Any, resources).runtime.metrics is cast(Any, runtime).metrics
    assert cast(Any, resources).runtime.tracer is cast(Any, runtime).tracer


async def _capture_agent_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], _Resources]:
    captured: dict[str, Any] = {}
    shutdowns: list[int] = []
    runtime = SimpleNamespace(
        tracer=object(),
        metrics=object(),
        shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
    )
    resources = _Resources(runtime)
    _assert_resource_runtime_identity(resources, runtime)

    class _Worker:
        async def __aenter__(self) -> _Worker:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    worker = _Worker()

    client = object()

    async def connect_with_retry(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        return client

    async def resources_with_retry(_settings: object, received_runtime: object) -> _Resources:
        assert received_runtime is runtime
        return resources

    def build_temporal_worker(
        received_client: object,
        *,
        runtime: object,
        task_queue: object,
        workflows: object,
        activities: object,
    ) -> _Worker:
        captured.update(
            client=received_client,
            runtime=runtime,
            task_queue=task_queue,
            workflows=workflows,
            activities=activities,
            worker=worker,
        )
        cast(Any, worker).workflows = workflows
        cast(Any, worker).activities = activities
        return worker

    async def completed_heartbeat() -> None:
        return None

    monkeypatch.setattr(agent_main, "connect_with_retry", connect_with_retry)
    monkeypatch.setattr(agent_main, "resources_with_retry", resources_with_retry)
    monkeypatch.setattr(
        agent_main,
        "initialize_observability",
        lambda config: captured.update(config=config) or runtime,
    )
    monkeypatch.setattr(agent_main, "build_temporal_worker", build_temporal_worker)
    monkeypatch.setattr(agent_main, "run_heartbeat", completed_heartbeat)
    monkeypatch.setattr(agent_main, "clear_heartbeat", lambda: None)
    monkeypatch.setattr(asyncio, "Event", _ImmediateEvent)
    monkeypatch.setattr(asyncio, "get_running_loop", _SignalLoop)

    await agent_main.main()
    captured["shutdowns"] = shutdowns
    return captured, resources


async def _capture_tool_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], _Resources]:
    captured: dict[str, Any] = {}
    shutdowns: list[int] = []
    runtime = SimpleNamespace(
        tracer=object(),
        metrics=object(),
        shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
    )
    resources = _Resources(runtime)
    _assert_resource_runtime_identity(resources, runtime)

    class _Worker:
        async def __aenter__(self) -> _Worker:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    client = object()

    async def connect_with_retry(_settings: ToolWorkerSettings, received_runtime: object) -> object:
        assert received_runtime is runtime
        return client

    async def resources_with_retry(
        _settings: ToolWorkerSettings, received_runtime: object
    ) -> _Resources:
        assert received_runtime is runtime
        return resources

    worker = _Worker()

    def build_temporal_worker(
        received_client: object,
        *,
        runtime: object,
        task_queue: object,
        workflows: object,
        activities: object,
    ) -> _Worker:
        captured.update(
            client=received_client,
            runtime=runtime,
            task_queue=task_queue,
            workflows=workflows,
            activities=activities,
            worker=worker,
        )
        cast(Any, worker).workflows = workflows
        cast(Any, worker).activities = activities
        return worker

    monkeypatch.setattr(tool_main, "connect_with_retry", connect_with_retry)
    monkeypatch.setattr(tool_main, "resources_with_retry", resources_with_retry)
    monkeypatch.setattr(tool_main, "build_default_catalog", ToolCatalog)
    monkeypatch.setattr(
        tool_main,
        "initialize_observability",
        lambda config: captured.update(config=config) or runtime,
    )
    monkeypatch.setattr(tool_main, "build_temporal_worker", build_temporal_worker)
    monkeypatch.setattr(asyncio, "Event", _ImmediateEvent)
    monkeypatch.setattr(asyncio, "get_running_loop", _SignalLoop)

    await tool_main.main()
    captured["shutdowns"] = shutdowns
    return captured, resources


async def test_agent_worker_registration_uses_only_agent_and_legacy_coordinators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, resources = await _capture_agent_registration(monkeypatch)

    assert captured["workflows"] == [
        AgentTaskWorkflow,
        TriggeredTaskWorkflow,
        DelegatedTaskWorkflow,
        EngineeringTicketWorkflow,
        AvatarGenerationWorkflow,
        WorkRequestTaskWorkflow,
        MemoryMaintenanceWorkflow,
        PeriodicReviewWorkflow,
    ]
    assert cast(Any, captured["worker"]).workflows is captured["workflows"]
    assert cast(Any, captured["worker"]).activities is captured["activities"]
    registered = _activity_map(captured["activities"])
    assert set(registered) == AGENT_ACTIVITY_NAMES
    assert list(registered) == AGENT_ACTIVITY_ORDER
    assert set(registered).isdisjoint(TOOL_ACTIVITY_NAMES)
    for name in ("run_agent_step", "resolve_approval", "finalize_run"):
        assert isinstance(registered[name].__self__, AgentCompatibilityActivities)
    assert isinstance(
        registered["sync_external"].__self__,
        TriggerCompatibilityActivities,
    )
    assert resources.close_count == 1
    _assert_resource_runtime_identity(resources, captured["runtime"])
    assert captured["client"] is not None
    assert captured["runtime"] is not None
    assert captured["config"].service_name == "agent-worker"
    assert captured["config"].environment == "dev"
    assert captured["config"].extra_log_processors == (agent_main.redact_event_dict,)
    assert captured["shutdowns"] == [5_000]


def test_agent_activity_class_no_longer_defines_legacy_effect_handlers() -> None:
    assert "run_agent_step_activity" not in AgentActivities.__dict__
    assert "resolve_approval_activity" not in AgentActivities.__dict__
    assert "finalize_run_activity" not in AgentActivities.__dict__


def test_legacy_approval_helper_is_a_projection_reexport() -> None:
    assert agent_activities_module._cancel_pending_run_approvals is _cancel_pending_run_approvals


async def test_tool_worker_registration_is_exactly_the_effect_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, resources = await _capture_tool_registration(monkeypatch)

    assert captured["workflows"] == [
        AdvertisedToolsCompatibilityWorkflow,
        ToolStepCompatibilityWorkflow,
        ApprovalCompatibilityWorkflow,
        SyncExternalCompatibilityWorkflow,
        CleanupCompatibilityWorkflow,
    ]
    assert cast(Any, captured["worker"]).workflows is captured["workflows"]
    assert cast(Any, captured["worker"]).activities is captured["activities"]
    registered = _activity_map(captured["activities"])
    assert set(registered) == TOOL_ACTIVITY_NAMES
    assert list(registered) == TOOL_ACTIVITY_ORDER
    assert isinstance(registered["resolve_advertised_tools"].__self__, ToolActivities)
    assert isinstance(registered["execute_bound_tool"].__self__, ToolActivities)
    assert isinstance(registered["resolve_bound_tool_approval"].__self__, ToolActivities)
    assert isinstance(registered["resolve_bound_tool_review"].__self__, ToolActivities)
    assert isinstance(registered["sync_external_tool"].__self__, TriggerToolActivities)
    assert isinstance(registered["cleanup_run_workspace"].__self__, CleanupActivities)
    assert resources.close_count == 1
    _assert_resource_runtime_identity(resources, captured["runtime"])
    assert captured["config"].service_name == "tool-worker"
    assert captured["config"].environment == "dev"
    assert captured["config"].extra_log_processors == (tool_main.redact_event_dict,)
    assert captured["shutdowns"] == [5_000]


def test_tool_worker_settings_default_to_closed_environment() -> None:
    assert ToolWorkerSettings().app_env == "dev"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
@pytest.mark.parametrize("operation", ["connect", "resources"])
async def test_retry_helpers_preserve_identity_and_attempt_order(
    kind: str,
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    settings = (
        agent_main.Settings(app_env="test")
        if kind == "agent"
        else ToolWorkerSettings(app_env="test")
    )
    runtime = object()
    result = object()
    attempts: list[tuple[object, object]] = []
    delays: list[float] = []

    async def fail_twice(
        received_settings: object,
        received_runtime: object,
    ) -> object:
        attempts.append((received_settings, received_runtime))
        if len(attempts) < 3:
            raise RuntimeError(f"{kind}-{operation}-retry")
        return result

    async def create_fail_twice(
        _resource_type: object,
        received_settings: object,
        *,
        runtime: object,
    ) -> object:
        return await fail_twice(received_settings, runtime)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", record_sleep)
    if operation == "connect":
        monkeypatch.setattr(module, "connect_temporal_client", fail_twice)
        received = await module.connect_with_retry(settings, cast(Any, runtime))
    else:
        resource_type = module.Resources if kind == "agent" else module.ToolWorkerResources
        monkeypatch.setattr(resource_type, "create", classmethod(create_fail_twice))
        received = await module.resources_with_retry(settings, cast(Any, runtime))

    assert received is result
    assert attempts == [(settings, runtime), (settings, runtime), (settings, runtime)]
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
async def test_worker_cleanup_is_reawaited_through_repeated_cancellation(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    settings = (
        agent_main.Settings(app_env="test")
        if kind == "agent"
        else ToolWorkerSettings(app_env="test")
    )
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    worker_entered = asyncio.Event()
    shutdowns: list[int] = []
    cleanup_task_names: list[str] = []
    runtime = SimpleNamespace(
        tracer=object(),
        metrics=object(),
        shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
    )

    class Resources:
        def __init__(self, received_runtime: object) -> None:
            self.runtime = received_runtime

        async def close(self) -> None:
            task = asyncio.current_task()
            assert task is not None
            cleanup_task_names.append(task.get_name())
            cleanup_started.set()
            await release_cleanup.wait()

    class Worker:
        async def __aenter__(self) -> Worker:
            worker_entered.set()
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def connect(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        return object()

    acquired_resources = Resources(runtime)
    _assert_resource_runtime_identity(acquired_resources, runtime)

    async def resources(_settings: object, received_runtime: object) -> Resources:
        assert received_runtime is runtime
        return acquired_resources

    def build(*_args: object, **_kwargs: object) -> Worker:
        return Worker()

    monkeypatch.setattr(module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(module, "connect_with_retry", connect)
    monkeypatch.setattr(module, "resources_with_retry", resources)
    monkeypatch.setattr(module, "build_temporal_worker", build)
    monkeypatch.setattr(asyncio, "get_running_loop", _SignalLoop)
    if kind == "agent":
        monkeypatch.setattr(module, "Settings", lambda: settings)
        monkeypatch.setattr(module, "run_heartbeat", lambda: _wait_forever())
        monkeypatch.setattr(module, "clear_heartbeat", lambda: None)
    else:
        monkeypatch.setattr(module, "ToolWorkerSettings", lambda: settings)
        monkeypatch.setattr(module, "build_default_catalog", ToolCatalog)

    task = asyncio.create_task(module.main(), name=f"{kind}-main-test")
    await _wait_for_event_or_early_task_failure(worker_entered, task)
    task.cancel("body-cancellation")
    await _wait_for_event_or_early_task_failure(cleanup_started, task)
    task.cancel("cleanup-cancellation-one")
    await asyncio.sleep(0)
    task.cancel("cleanup-cancellation-two")
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.args == ("body-cancellation",)
    _assert_resource_runtime_identity(acquired_resources, runtime)
    assert len(cleanup_task_names) == 1
    assert cleanup_task_names[0].endswith("worker-cleanup")
    assert shutdowns == [5_000]
    assert not [
        candidate
        for candidate in asyncio.all_tasks()
        if candidate is not asyncio.current_task()
        and candidate.get_name() == cleanup_task_names[0]
        and not candidate.done()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
async def test_await_cleanup_records_cancellation_before_done_race(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    cancellation = asyncio.CancelledError(f"{kind}-cleanup-race")
    cleanup_error = RuntimeError(f"{kind}-cleanup-error")

    class CompletedCleanup:
        def done(self) -> bool:
            return True

        def result(self) -> BaseException:
            return cleanup_error

    async def cancel_as_cleanup_completes(_task: object) -> None:
        raise cancellation

    monkeypatch.setattr(module.asyncio, "shield", cancel_as_cleanup_completes)
    error, caught_cancellation = await module._await_cleanup(cast(Any, CompletedCleanup()))
    assert error is cleanup_error
    assert caught_cancellation is cancellation


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
async def test_worker_enter_failure_never_calls_exit_and_still_cleans_up(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    settings = (
        agent_main.Settings(app_env="test")
        if kind == "agent"
        else ToolWorkerSettings(app_env="test")
    )
    enter_error = RuntimeError(f"{kind}-enter-error")
    enter_count = 0
    exit_count = 0
    shutdowns: list[int] = []
    runtime = SimpleNamespace(
        tracer=object(),
        metrics=object(),
        shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
    )
    resources = _Resources(runtime)
    _assert_resource_runtime_identity(resources, runtime)

    class Worker:
        async def __aenter__(self) -> Worker:
            nonlocal enter_count
            enter_count += 1
            raise enter_error

        async def __aexit__(self, *_args: object) -> None:
            nonlocal exit_count
            exit_count += 1

    async def connect(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        return object()

    async def acquire_resources(_settings: object, received_runtime: object) -> _Resources:
        assert received_runtime is runtime
        return resources

    monkeypatch.setattr(module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(module, "connect_with_retry", connect)
    monkeypatch.setattr(module, "resources_with_retry", acquire_resources)
    monkeypatch.setattr(module, "build_temporal_worker", lambda *_args, **_kwargs: Worker())
    monkeypatch.setattr(module.asyncio, "get_running_loop", lambda: _SignalLoop())
    if kind == "agent":
        monkeypatch.setattr(module, "Settings", lambda: settings)
        monkeypatch.setattr(module, "run_heartbeat", _wait_forever)
        monkeypatch.setattr(module, "clear_heartbeat", lambda: None)
    else:
        monkeypatch.setattr(module, "ToolWorkerSettings", lambda: settings)
        monkeypatch.setattr(module, "build_default_catalog", ToolCatalog)

    caught: BaseException | None = None
    try:
        await module.main()
    except BaseException as error:
        caught = error
    assert caught is enter_error
    assert enter_count == 1
    assert exit_count == 0
    assert resources.close_count == 1
    _assert_resource_runtime_identity(resources, runtime)
    assert shutdowns == [5_000]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
async def test_cleanup_wait_cancellation_outranks_active_business_error(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    settings = (
        agent_main.Settings(app_env="test")
        if kind == "agent"
        else ToolWorkerSettings(app_env="test")
    )
    worker_entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    business_error = RuntimeError(f"{kind}-body-error")
    shutdowns: list[int] = []
    runtime = SimpleNamespace(
        tracer=object(),
        metrics=object(),
        shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
    )
    resources = _Resources(runtime)
    _assert_resource_runtime_identity(resources, runtime)

    class Stop:
        def set(self) -> None:
            return None

        async def wait(self) -> None:
            raise business_error

    class Worker:
        async def __aenter__(self) -> Worker:
            worker_entered.set()
            return self

        async def __aexit__(self, *_args: object) -> None:
            cleanup_started.set()
            await release_cleanup.wait()

    async def connect(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        return object()

    async def acquire_resources(_settings: object, received_runtime: object) -> _Resources:
        assert received_runtime is runtime
        return resources

    async def completed_heartbeat() -> None:
        return None

    monkeypatch.setattr(module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(module, "connect_with_retry", connect)
    monkeypatch.setattr(module, "resources_with_retry", acquire_resources)
    monkeypatch.setattr(module, "build_temporal_worker", lambda *_args, **_kwargs: Worker())
    monkeypatch.setattr(module.asyncio, "Event", Stop)
    monkeypatch.setattr(module.asyncio, "get_running_loop", lambda: _SignalLoop())
    if kind == "agent":
        monkeypatch.setattr(module, "Settings", lambda: settings)
        monkeypatch.setattr(module, "run_heartbeat", completed_heartbeat)
        monkeypatch.setattr(module, "clear_heartbeat", lambda: None)
    else:
        monkeypatch.setattr(module, "ToolWorkerSettings", lambda: settings)
        monkeypatch.setattr(module, "build_default_catalog", ToolCatalog)

    task = asyncio.create_task(module.main(), name=f"{kind}-business-cleanup-test")
    await _wait_for_event_or_early_task_failure(worker_entered, task)
    await _wait_for_event_or_early_task_failure(cleanup_started, task)
    task.cancel("cleanup-cancellation")
    await asyncio.sleep(0)
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.args == ("cleanup-cancellation",)
    assert resources.close_count == 1
    _assert_resource_runtime_identity(resources, runtime)
    assert shutdowns == [5_000]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
@pytest.mark.parametrize("cancellation_stage", ["drain", "dispose"])
async def test_resource_close_cancellation_outranks_ordinary_error_after_both_steps(
    kind: str,
    cancellation_stage: str,
) -> None:
    order: list[str] = []
    cancellation = asyncio.CancelledError(f"{kind}-{cancellation_stage}-cancellation")
    ordinary_error = RuntimeError(f"{kind}-ordinary-cleanup-error")

    class Nats:
        async def drain(self) -> None:
            order.append("drain")
            raise cancellation if cancellation_stage == "drain" else ordinary_error

    class Engine:
        async def dispose(self) -> None:
            order.append("dispose")
            raise cancellation if cancellation_stage == "dispose" else ordinary_error

    resource_type = AgentResources if kind == "agent" else ToolWorkerResources
    resources = resource_type(
        runtime=cast(Any, object()),
        engine=cast(Any, Engine()),
        session_factory=cast(Any, object()),
        nats_connection=cast(Any, Nats()),
        publisher=cast(Any, object()),
        crypto=cast(Any, object()),
        test_barrier=cast(Any, object()),
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        await resources.close()
    assert raised.value is cancellation
    assert order == ["drain", "dispose"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
async def test_process_cleanup_cancellation_outranks_earlier_errors_and_runs_every_step(
    kind: str,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    events: list[str] = []
    exit_error = RuntimeError(f"{kind}-exit-error")
    cleanup_cancellation = asyncio.CancelledError(f"{kind}-resource-cancellation")

    class Loop:
        def remove_signal_handler(self, handled_signal: object) -> bool:
            events.append(f"signal:{handled_signal}")
            return True

    class Worker:
        async def __aexit__(self, *_args: object) -> None:
            events.append("worker.exit")
            raise exit_error

    class Resources:
        async def close(self) -> None:
            events.append("resources.close")
            raise cleanup_cancellation

    class Runtime:
        def shutdown(self, *, timeout_millis: int) -> None:
            events.append(f"runtime.shutdown:{timeout_millis}")
            raise BaseException(f"{kind}-shutdown-error")

    kwargs: dict[str, object] = {
        "loop": Loop(),
        "registered_signals": [cast(Any, "first")],
        "worker": Worker(),
        "worker_exit_needed": True,
        "resources": Resources(),
        "runtime": Runtime(),
        "active_error": RuntimeError(f"{kind}-body"),
        "active_traceback": None,
    }
    if kind == "agent":
        kwargs["heartbeat_task"] = None
    result = await module._cleanup_process(**cast(Any, kwargs))
    assert result is cleanup_cancellation
    assert events == [
        "signal:first",
        "worker.exit",
        "resources.close",
        "runtime.shutdown:5000",
    ]


@pytest.mark.asyncio
async def test_agent_heartbeat_cancel_failure_cannot_skip_later_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    cancel_error = RuntimeError("heartbeat-cancel-error")

    class Heartbeat:
        def cancel(self) -> None:
            events.append("heartbeat.cancel")
            raise cancel_error

        def __await__(self) -> Any:
            async def finish() -> None:
                events.append("heartbeat.await")

            return finish().__await__()

    class Resources:
        async def close(self) -> None:
            events.append("resources.close")

    class Runtime:
        def shutdown(self, *, timeout_millis: int) -> None:
            events.append(f"runtime.shutdown:{timeout_millis}")

    monkeypatch.setattr(agent_main, "clear_heartbeat", lambda: events.append("heartbeat.clear"))
    result = await agent_main._cleanup_process(
        loop=None,
        registered_signals=[],
        worker=None,
        worker_exit_needed=False,
        heartbeat_task=cast(Any, Heartbeat()),
        resources=cast(Any, Resources()),
        runtime=cast(Any, Runtime()),
        active_error=None,
        active_traceback=None,
    )
    assert result is cancel_error
    assert events == [
        "heartbeat.cancel",
        "heartbeat.await",
        "heartbeat.clear",
        "resources.close",
        "runtime.shutdown:5000",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
async def test_active_business_error_outranks_inner_cleanup_cancellation(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    settings = (
        agent_main.Settings(app_env="test")
        if kind == "agent"
        else ToolWorkerSettings(app_env="test")
    )
    business_error = RuntimeError(f"{kind}-body-error")
    business_origin: list[Any] = []
    cleanup_cancellation = asyncio.CancelledError(f"{kind}-cleanup-cancellation")
    shutdowns: list[int] = []
    runtime = SimpleNamespace(
        tracer=object(),
        metrics=object(),
        shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
    )

    class Stop:
        def set(self) -> None:
            return None

        async def wait(self) -> None:
            try:
                raise business_error
            except RuntimeError as error:
                business_origin.append(error.__traceback__)
                raise

    class Worker:
        async def __aenter__(self) -> Worker:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Resources:
        def __init__(self, received_runtime: object) -> None:
            self.runtime = received_runtime

        async def close(self) -> None:
            raise cleanup_cancellation

    acquired_resources = Resources(runtime)
    _assert_resource_runtime_identity(acquired_resources, runtime)

    async def connect(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        return object()

    async def resources(_settings: object, received_runtime: object) -> Resources:
        assert received_runtime is runtime
        return acquired_resources

    monkeypatch.setattr(module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(module, "connect_with_retry", connect)
    monkeypatch.setattr(module, "resources_with_retry", resources)
    monkeypatch.setattr(module, "build_temporal_worker", lambda *_args, **_kwargs: Worker())
    monkeypatch.setattr(module.asyncio, "Event", Stop)
    monkeypatch.setattr(module.asyncio, "get_running_loop", lambda: _SignalLoop())
    if kind == "agent":
        monkeypatch.setattr(module, "Settings", lambda: settings)

        async def heartbeat() -> None:
            return None

        monkeypatch.setattr(module, "run_heartbeat", heartbeat)
        monkeypatch.setattr(module, "clear_heartbeat", lambda: None)
    else:
        monkeypatch.setattr(module, "ToolWorkerSettings", lambda: settings)
        monkeypatch.setattr(module, "build_default_catalog", ToolCatalog)

    caught: BaseException | None = None
    try:
        await module.main()
    except BaseException as error:
        caught = error
    assert caught is business_error
    _assert_resource_runtime_identity(acquired_resources, runtime)
    traceback = caught.__traceback__
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback is business_origin[0]
    assert shutdowns == [5_000]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "failure_stage"),
    [
        ("agent", "connect"),
        ("agent", "resources"),
        ("agent", "signal"),
        ("agent", "heartbeat"),
        ("agent", "worker_construct"),
        ("agent", "worker_enter"),
        ("agent", "worker_run"),
        ("agent", "worker_exit"),
        ("agent", "resources_close"),
        ("agent", "runtime_shutdown"),
        ("tool", "connect"),
        ("tool", "resources"),
        ("tool", "signal"),
        ("tool", "worker_construct"),
        ("tool", "worker_enter"),
        ("tool", "worker_run"),
        ("tool", "worker_exit"),
        ("tool", "resources_close"),
        ("tool", "runtime_shutdown"),
    ],
)
async def test_worker_failure_matrix_attempts_every_owned_cleanup_step(
    kind: str,
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    settings = (
        agent_main.Settings(app_env="test")
        if kind == "agent"
        else ToolWorkerSettings(app_env="test")
    )
    failure = RuntimeError(f"{kind}-{failure_stage}-failure")
    events: list[str] = []
    shutdown_count = 0

    class Runtime:
        tracer = object()
        metrics = object()

        def shutdown(self, *, timeout_millis: int) -> None:
            nonlocal shutdown_count
            assert timeout_millis == 5_000
            shutdown_count += 1
            events.append("runtime.shutdown")
            if failure_stage == "runtime_shutdown":
                raise failure

    runtime = Runtime()

    class Resources:
        def __init__(self, received_runtime: object) -> None:
            self.runtime = received_runtime

        async def close(self) -> None:
            events.append("resources.close")
            if failure_stage == "resources_close":
                raise failure

    resources = Resources(runtime)
    _assert_resource_runtime_identity(resources, runtime)

    class Loop:
        def add_signal_handler(self, handled_signal: object, *_args: object) -> None:
            events.append(f"signal.add:{handled_signal}")
            if failure_stage == "signal":
                raise failure

        def remove_signal_handler(self, handled_signal: object) -> bool:
            events.append(f"signal.remove:{handled_signal}")
            return True

    class Stop:
        def set(self) -> None:
            return None

        async def wait(self) -> None:
            await asyncio.sleep(0)
            if failure_stage == "worker_run":
                raise failure

    class Worker:
        entered = False

        async def __aenter__(self) -> Worker:
            events.append("worker.enter")
            if failure_stage == "worker_enter":
                raise failure
            self.entered = True
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append("worker.exit")
            if failure_stage == "worker_exit":
                raise failure

    worker = Worker()

    async def connect(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        events.append("connect")
        if failure_stage == "connect":
            raise failure
        return object()

    async def acquire_resources(_settings: object, received_runtime: object) -> Resources:
        assert received_runtime is runtime
        events.append("resources.acquire")
        if failure_stage == "resources":
            raise failure
        return resources

    def build(*_args: object, **_kwargs: object) -> Worker:
        events.append("worker.construct")
        if failure_stage == "worker_construct":
            raise failure
        return worker

    async def heartbeat() -> None:
        events.append("heartbeat.run")
        if failure_stage == "heartbeat":
            raise failure

    monkeypatch.setattr(module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(module, "connect_with_retry", connect)
    monkeypatch.setattr(module, "resources_with_retry", acquire_resources)
    monkeypatch.setattr(module, "build_temporal_worker", build)
    monkeypatch.setattr(module.asyncio, "Event", Stop)
    monkeypatch.setattr(module.asyncio, "get_running_loop", Loop)
    if kind == "agent":
        monkeypatch.setattr(module, "Settings", lambda: settings)
        monkeypatch.setattr(module, "run_heartbeat", heartbeat)
        monkeypatch.setattr(module, "clear_heartbeat", lambda: events.append("heartbeat.clear"))
    else:
        monkeypatch.setattr(module, "ToolWorkerSettings", lambda: settings)
        monkeypatch.setattr(module, "build_default_catalog", ToolCatalog)

    caught: BaseException | None = None
    try:
        await module.main()
    except BaseException as error:
        caught = error
    assert caught is failure
    _assert_resource_runtime_identity(resources, runtime)
    assert shutdown_count == 1
    if failure_stage not in {"connect", "resources"}:
        assert events.count("resources.close") == 1
    else:
        assert "resources.close" not in events
    if failure_stage == "worker_enter":
        assert "worker.exit" not in events
    elif worker.entered:
        assert events.count("worker.exit") == 1
    if kind == "agent" and failure_stage not in {"connect", "resources", "signal"}:
        assert events.count("heartbeat.clear") == 1
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name() == f"{kind}-worker-cleanup"
        and not task.done()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "tool"])
async def test_runtime_initialization_is_first_effect_after_settings(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = agent_main if kind == "agent" else tool_main
    events: list[str] = []
    failure = RuntimeError(f"{kind}-connect-stop")
    settings_value = (
        agent_main.Settings(app_env="test")
        if kind == "agent"
        else ToolWorkerSettings(app_env="test")
    )

    class SettingsFactory:
        def __call__(self) -> object:
            events.append("settings")
            return settings_value

    runtime = SimpleNamespace(
        shutdown=lambda timeout_millis: events.append(f"shutdown:{timeout_millis}")
    )

    def initialize(_config: object) -> object:
        events.append("runtime")
        return runtime

    async def connect(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        events.append("connect")
        raise failure

    monkeypatch.setattr(module, "initialize_observability", initialize)
    monkeypatch.setattr(module, "connect_with_retry", connect)
    if kind == "agent":
        monkeypatch.setattr(module, "Settings", SettingsFactory())
    else:
        monkeypatch.setattr(module, "ToolWorkerSettings", SettingsFactory())

    with pytest.raises(RuntimeError) as raised:
        await module.main()
    assert raised.value is failure
    assert events == ["settings", "runtime", "connect", "shutdown:5000"]


async def _wait_forever() -> None:
    await asyncio.Event().wait()
