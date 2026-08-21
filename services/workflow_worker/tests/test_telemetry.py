"""Workflow-worker observability identity and lifecycle ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

import jhin_workflow_worker.main as main_module
from jhin_observability import ObservabilityRuntime, ObservabilitySettings
from jhin_workflow_worker.settings import Settings
from jhin_workflows import WORKFLOW_TASK_QUEUE
from jhin_workflows.heartbeat import HeartbeatWorkflow, record_beat


class _ImmediateEvent:
    def __init__(self) -> None:
        self.set_count = 0

    def set(self) -> None:
        self.set_count += 1

    async def wait(self) -> None:
        return None


class _Loop:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.removed: list[object] = []

    def add_signal_handler(self, signal: object, _callback: object) -> None:
        self.added.append(signal)

    def remove_signal_handler(self, signal: object) -> bool:
        self.removed.append(signal)
        return True


class _Worker:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> _Worker:
        self.enter_count += 1
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exit_count += 1


def test_workflow_settings_inherit_closed_observability_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert issubclass(Settings, ObservabilitySettings)
    monkeypatch.setenv("APP_ENV", "production")
    assert Settings().app_env == "production"
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.asyncio
async def test_connect_retry_forwards_the_exact_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    runtime = cast(ObservabilityRuntime, object())
    client = object()
    calls: list[tuple[object, object]] = []

    async def connect(received_settings: object, received_runtime: object) -> object:
        calls.append((received_settings, received_runtime))
        return client

    monkeypatch.setattr(main_module, "connect_temporal_client", connect)
    assert await main_module.connect_with_retry(settings, runtime) is client
    assert calls == [(settings, runtime)]


@pytest.mark.asyncio
async def test_connect_retry_preserves_identity_and_attempt_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    runtime = cast(ObservabilityRuntime, object())
    client = object()
    calls: list[tuple[object, object]] = []
    delays: list[float] = []

    async def connect(received_settings: object, received_runtime: object) -> object:
        calls.append((received_settings, received_runtime))
        if len(calls) < 3:
            raise RuntimeError("workflow-connect-retry")
        return client

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(main_module, "connect_temporal_client", connect)
    monkeypatch.setattr(main_module.asyncio, "sleep", sleep)
    assert await main_module.connect_with_retry(settings, runtime) is client
    assert calls == [(settings, runtime), (settings, runtime), (settings, runtime)]
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_workflow_worker_bootstrap_registration_and_cleanup_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_env="test")
    shutdowns: list[int] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            tracer=object(),
            metrics=object(),
            shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
        ),
    )
    client = object()
    worker = _Worker()
    loop = _Loop()
    order: list[str] = []
    captured: dict[str, object] = {}

    def initialize(config: object) -> ObservabilityRuntime:
        order.append("runtime")
        captured["config"] = config
        return runtime

    async def connect(received_settings: object, received_runtime: object) -> object:
        order.append("connect")
        assert received_settings is settings
        assert received_runtime is runtime
        return client

    def build(
        received_client: object,
        *,
        runtime: object,
        task_queue: str,
        workflows: object,
        activities: object,
    ) -> _Worker:
        order.append("worker")
        captured.update(
            client=received_client,
            runtime=runtime,
            task_queue=task_queue,
            workflows=workflows,
            activities=activities,
        )
        cast(Any, worker).workflows = workflows
        cast(Any, worker).activities = activities
        return worker

    async def heartbeat() -> None:
        order.append("heartbeat")

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", initialize)
    monkeypatch.setattr(main_module, "connect_with_retry", connect)
    monkeypatch.setattr(main_module, "build_temporal_worker", build)
    monkeypatch.setattr(main_module, "run_heartbeat", heartbeat)
    monkeypatch.setattr(main_module, "clear_heartbeat", lambda: order.append("clear"))
    monkeypatch.setattr(main_module.asyncio, "Event", _ImmediateEvent)
    monkeypatch.setattr(main_module.asyncio, "get_running_loop", lambda: loop)

    await main_module.main()

    assert order[0] == "runtime"
    assert captured["client"] is client
    assert captured["runtime"] is runtime
    assert captured["task_queue"] == WORKFLOW_TASK_QUEUE
    assert captured["workflows"] == [HeartbeatWorkflow]
    assert captured["activities"] == [record_beat]
    assert cast(Any, worker).workflows is captured["workflows"]
    assert cast(Any, worker).activities is captured["activities"]
    assert worker.enter_count == worker.exit_count == 1
    assert set(loop.removed) == set(loop.added)
    assert "clear" in order
    assert shutdowns == [5_000]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["connect", "worker"])
async def test_startup_failure_preserves_error_and_shuts_runtime(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_env="test")
    shutdowns: list[int] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(shutdown=lambda timeout_millis: shutdowns.append(timeout_millis)),
    )
    failure = RuntimeError(f"{failure_stage}-private-canary")

    async def connect(*_args: object) -> object:
        if failure_stage == "connect":
            raise failure
        return object()

    def build(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "connect_with_retry", connect)
    monkeypatch.setattr(main_module, "build_temporal_worker", build)
    caught: BaseException | None = None
    try:
        await main_module.main()
    except BaseException as exc:
        caught = exc
    assert caught is failure
    assert shutdowns == [5_000]


@pytest.mark.asyncio
async def test_heartbeat_is_cancelled_and_awaited_before_runtime_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_env="test")
    released = asyncio.Event()
    cleanup_order: list[str] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            shutdown=lambda timeout_millis: cleanup_order.append(f"shutdown:{timeout_millis}")
        ),
    )

    async def heartbeat() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_order.append("heartbeat-finished")
            released.set()

    worker = _Worker()
    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "connect_with_retry", lambda *_args: _async_value(object()))
    monkeypatch.setattr(main_module, "build_temporal_worker", lambda *_args, **_kwargs: worker)
    monkeypatch.setattr(main_module, "run_heartbeat", heartbeat)
    monkeypatch.setattr(main_module, "clear_heartbeat", lambda: cleanup_order.append("clear"))
    monkeypatch.setattr(main_module.asyncio, "Event", _ImmediateEvent)
    monkeypatch.setattr(main_module.asyncio, "get_running_loop", lambda: _Loop())
    await main_module.main()
    assert released.is_set()
    assert cleanup_order.index("heartbeat-finished") < cleanup_order.index("shutdown:5000")


@pytest.mark.asyncio
async def test_await_cleanup_records_cancellation_before_done_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = asyncio.CancelledError("workflow-cleanup-race")
    cleanup_error = RuntimeError("workflow-cleanup-error")

    class CompletedCleanup:
        def done(self) -> bool:
            return True

        def result(self) -> BaseException:
            return cleanup_error

    async def cancel_as_cleanup_completes(_task: object) -> None:
        raise cancellation

    monkeypatch.setattr(main_module.asyncio, "shield", cancel_as_cleanup_completes)
    error, caught_cancellation = await main_module._await_cleanup(cast(Any, CompletedCleanup()))
    assert error is cleanup_error
    assert caught_cancellation is cancellation


@pytest.mark.asyncio
async def test_worker_enter_failure_never_calls_exit_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_env="test")
    enter_error = RuntimeError("workflow-enter-error")
    enter_count = 0
    exit_count = 0
    cleanup_order: list[str] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            shutdown=lambda timeout_millis: cleanup_order.append(f"shutdown:{timeout_millis}")
        ),
    )

    class Worker:
        async def __aenter__(self) -> Worker:
            nonlocal enter_count
            enter_count += 1
            raise enter_error

        async def __aexit__(self, *_args: object) -> None:
            nonlocal exit_count
            exit_count += 1

    async def heartbeat() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "connect_with_retry", lambda *_args: _async_value(object()))
    monkeypatch.setattr(main_module, "build_temporal_worker", lambda *_args, **_kwargs: Worker())
    monkeypatch.setattr(main_module, "run_heartbeat", heartbeat)
    monkeypatch.setattr(main_module, "clear_heartbeat", lambda: cleanup_order.append("clear"))
    monkeypatch.setattr(main_module.asyncio, "get_running_loop", lambda: _Loop())

    caught: BaseException | None = None
    try:
        await main_module.main()
    except BaseException as error:
        caught = error
    assert caught is enter_error
    assert enter_count == 1
    assert exit_count == 0
    assert "clear" in cleanup_order
    assert cleanup_order[-1] == "shutdown:5000"


@pytest.mark.asyncio
async def test_cleanup_wait_cancellation_outranks_active_business_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_env="test")
    worker_entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    business_error = RuntimeError("workflow-body-error")
    shutdowns: list[int] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(shutdown=lambda timeout_millis: shutdowns.append(timeout_millis)),
    )

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

    async def heartbeat() -> None:
        return None

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "connect_with_retry", lambda *_args: _async_value(object()))
    monkeypatch.setattr(main_module, "build_temporal_worker", lambda *_args, **_kwargs: Worker())
    monkeypatch.setattr(main_module, "run_heartbeat", heartbeat)
    monkeypatch.setattr(main_module, "clear_heartbeat", lambda: None)
    monkeypatch.setattr(main_module.asyncio, "Event", Stop)
    monkeypatch.setattr(main_module.asyncio, "get_running_loop", lambda: _Loop())

    task = asyncio.create_task(main_module.main(), name="workflow-business-cleanup-test")
    await worker_entered.wait()
    await cleanup_started.wait()
    task.cancel("cleanup-cancellation")
    await asyncio.sleep(0)
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.args == ("cleanup-cancellation",)
    assert shutdowns == [5_000]


@pytest.mark.asyncio
async def test_active_business_error_outranks_inner_cleanup_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_env="test")
    business_error = RuntimeError("workflow-body-error")
    business_origin: list[Any] = []
    cleanup_cancellation = asyncio.CancelledError("workflow-exit-cancellation")
    shutdowns: list[int] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(shutdown=lambda timeout_millis: shutdowns.append(timeout_millis)),
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
            raise cleanup_cancellation

    async def heartbeat() -> None:
        return None

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "connect_with_retry", lambda *_args: _async_value(object()))
    monkeypatch.setattr(main_module, "build_temporal_worker", lambda *_args, **_kwargs: Worker())
    monkeypatch.setattr(main_module, "run_heartbeat", heartbeat)
    monkeypatch.setattr(main_module, "clear_heartbeat", lambda: None)
    monkeypatch.setattr(main_module.asyncio, "Event", Stop)
    monkeypatch.setattr(main_module.asyncio, "get_running_loop", lambda: _Loop())

    caught: BaseException | None = None
    try:
        await main_module.main()
    except BaseException as error:
        caught = error
    assert caught is business_error
    traceback = caught.__traceback__
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback is business_origin[0]
    assert shutdowns == [5_000]


@pytest.mark.asyncio
async def test_heartbeat_cancel_failure_cannot_skip_clear_or_runtime_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    cancel_error = RuntimeError("workflow-heartbeat-cancel-error")

    class Heartbeat:
        def cancel(self) -> None:
            events.append("heartbeat.cancel")
            raise cancel_error

        def __await__(self) -> Any:
            async def finish() -> None:
                events.append("heartbeat.await")

            return finish().__await__()

    class Runtime:
        def shutdown(self, *, timeout_millis: int) -> None:
            events.append(f"runtime.shutdown:{timeout_millis}")

    monkeypatch.setattr(main_module, "clear_heartbeat", lambda: events.append("heartbeat.clear"))
    result = await main_module._cleanup_process(
        loop=None,
        registered_signals=[],
        worker=None,
        worker_exit_needed=False,
        heartbeat_task=cast(Any, Heartbeat()),
        runtime=cast(Any, Runtime()),
        active_error=None,
        active_traceback=None,
    )
    assert result is cancel_error
    assert events == [
        "heartbeat.cancel",
        "heartbeat.await",
        "heartbeat.clear",
        "runtime.shutdown:5000",
    ]


@pytest.mark.asyncio
async def test_workflow_cleanup_is_reawaited_after_second_cancellation_without_survivors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_env="test")
    worker_entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    never_stop = asyncio.Event()
    cleanup_task_names: list[str] = []
    cleanup_order: list[str] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            shutdown=lambda timeout_millis: cleanup_order.append(f"shutdown:{timeout_millis}")
        ),
    )

    class Stop:
        def set(self) -> None:
            return None

        async def wait(self) -> None:
            await never_stop.wait()

    class Worker:
        async def __aenter__(self) -> Worker:
            worker_entered.set()
            return self

        async def __aexit__(self, *_args: object) -> None:
            task = asyncio.current_task()
            assert task is not None
            cleanup_task_names.append(task.get_name())
            cleanup_started.set()
            await release_cleanup.wait()

    async def heartbeat() -> None:
        return None

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "connect_with_retry", lambda *_args: _async_value(object()))
    monkeypatch.setattr(main_module, "build_temporal_worker", lambda *_args, **_kwargs: Worker())
    monkeypatch.setattr(main_module, "run_heartbeat", heartbeat)
    monkeypatch.setattr(main_module, "clear_heartbeat", lambda: cleanup_order.append("clear"))
    monkeypatch.setattr(main_module.asyncio, "Event", Stop)
    monkeypatch.setattr(main_module.asyncio, "get_running_loop", lambda: _Loop())

    task = asyncio.create_task(main_module.main(), name="workflow-repeat-cancel-test")
    await worker_entered.wait()
    task.cancel("first-cancellation")
    await cleanup_started.wait()
    task.cancel("second-cancellation")
    await asyncio.sleep(0)
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.args == ("first-cancellation",)
    assert cleanup_task_names == ["workflow-worker-cleanup"]
    assert cleanup_order == ["clear", "shutdown:5000"]
    assert not [
        candidate
        for candidate in asyncio.all_tasks()
        if candidate is not asyncio.current_task()
        and candidate.get_name() == "workflow-worker-cleanup"
        and not candidate.done()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage",
    [
        "connect",
        "signal",
        "heartbeat",
        "worker_construct",
        "worker_enter",
        "worker_run",
        "worker_exit",
        "heartbeat_clear",
        "runtime_shutdown",
    ],
)
async def test_workflow_failure_matrix_attempts_every_owned_cleanup_step(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_env="test")
    failure = RuntimeError(f"workflow-{failure_stage}-failure")
    events: list[str] = []
    shutdown_count = 0

    class Runtime:
        def shutdown(self, *, timeout_millis: int) -> None:
            nonlocal shutdown_count
            assert timeout_millis == 5_000
            shutdown_count += 1
            events.append("runtime.shutdown")
            if failure_stage == "runtime_shutdown":
                raise failure

    runtime = cast(ObservabilityRuntime, Runtime())

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

    def build(*_args: object, **_kwargs: object) -> Worker:
        events.append("worker.construct")
        if failure_stage == "worker_construct":
            raise failure
        return worker

    async def heartbeat() -> None:
        events.append("heartbeat.run")
        if failure_stage == "heartbeat":
            raise failure

    def clear() -> None:
        events.append("heartbeat.clear")
        if failure_stage == "heartbeat_clear":
            raise failure

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "connect_with_retry", connect)
    monkeypatch.setattr(main_module, "build_temporal_worker", build)
    monkeypatch.setattr(main_module, "run_heartbeat", heartbeat)
    monkeypatch.setattr(main_module, "clear_heartbeat", clear)
    monkeypatch.setattr(main_module.asyncio, "Event", Stop)
    monkeypatch.setattr(main_module.asyncio, "get_running_loop", Loop)

    caught: BaseException | None = None
    try:
        await main_module.main()
    except BaseException as error:
        caught = error
    assert caught is failure
    assert shutdown_count == 1
    if failure_stage == "worker_enter":
        assert "worker.exit" not in events
    elif worker.entered:
        assert events.count("worker.exit") == 1
    assert events.count("heartbeat.clear") == 1
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name() == "workflow-worker-cleanup"
        and not task.done()
    ]


@pytest.mark.asyncio
async def test_runtime_initialization_is_first_effect_after_workflow_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    failure = RuntimeError("workflow-connect-stop")

    def settings_factory() -> Settings:
        events.append("settings")
        return Settings(app_env="test")

    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            shutdown=lambda timeout_millis: events.append(f"shutdown:{timeout_millis}")
        ),
    )

    def initialize(_config: object) -> ObservabilityRuntime:
        events.append("runtime")
        return runtime

    async def connect(_settings: object, received_runtime: object) -> object:
        assert received_runtime is runtime
        events.append("connect")
        raise failure

    monkeypatch.setattr(main_module, "Settings", settings_factory)
    monkeypatch.setattr(main_module, "initialize_observability", initialize)
    monkeypatch.setattr(main_module, "connect_with_retry", connect)
    with pytest.raises(RuntimeError) as raised:
        await main_module.main()
    assert raised.value is failure
    assert events == ["settings", "runtime", "connect", "shutdown:5000"]


async def _async_value(value: object) -> object:
    return value
