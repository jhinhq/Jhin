"""Sandbox app-factory and CLI observability ownership contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

import jhin_sandbox_runner.main as main_module
from jhin_observability import ObservabilityRuntime, ObservabilitySettings
from jhin_sandbox_runner.settings import Settings


def _settings() -> Settings:
    return Settings(
        app_env="test",
        sandbox_runner_token="token",
        sandbox_docker_mode="rootless",
        sandbox_docker_transport_url="http://rootless-docker-transport:2375",
    )


class _Manager:
    def __init__(self) -> None:
        self.start_count = 0
        self.close_count = 0

    async def start(self) -> None:
        self.start_count += 1

    async def close(self) -> None:
        self.close_count += 1


def _runtime(shutdowns: list[int]) -> ObservabilityRuntime:
    return cast(
        ObservabilityRuntime,
        SimpleNamespace(
            tracer=object(),
            metrics=object(),
            shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
        ),
    )


def test_sandbox_settings_inherit_closed_observability_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert issubclass(Settings, ObservabilitySettings)
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(ValueError):
        Settings(
            sandbox_runner_token="token",
            sandbox_docker_mode="rootless",
            sandbox_docker_transport_url="http://rootless-docker-transport:2375",
        )


@pytest.mark.asyncio
async def test_factory_owned_runtime_is_visible_and_shut_after_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)
    manager = _Manager()
    order: list[str] = []
    original_close = manager.close

    async def close() -> None:
        await original_close()
        order.append("manager-close")

    manager.close = close  # type: ignore[method-assign]
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    app = main_module.create_app(_settings())
    assert app.state.observability is runtime
    assert app.state.manager is manager
    async with app.router.lifespan_context(app):
        assert manager.start_count == 1
    order.extend(f"runtime:{value}" for value in shutdowns)
    assert manager.close_count == 1
    assert order == ["manager-close", "runtime:5000"]


@pytest.mark.asyncio
async def test_injected_runtime_is_never_shut_by_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)
    manager = _Manager()
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    monkeypatch.setattr(
        main_module,
        "initialize_observability",
        lambda _config: (_ for _ in ()).throw(AssertionError("must not initialize")),
    )
    app = main_module.create_app(_settings(), runtime=runtime)
    async with app.router.lifespan_context(app):
        assert app.state.observability is runtime
    assert manager.start_count == manager.close_count == 1
    assert shutdowns == []


@pytest.mark.asyncio
async def test_falsey_injected_runtime_preserves_identity_without_init_or_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []

    class FalseyRuntime:
        tracer = object()
        metrics = object()

        def __bool__(self) -> bool:
            return False

        def shutdown(self, *, timeout_millis: int) -> None:
            shutdowns.append(timeout_millis)

    runtime = cast(ObservabilityRuntime, FalseyRuntime())
    manager = _Manager()
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    monkeypatch.setattr(
        main_module,
        "initialize_observability",
        lambda _config: (_ for _ in ()).throw(AssertionError("must not initialize")),
    )

    app = main_module.create_app(_settings(), runtime=runtime)
    assert app.state.observability is runtime
    async with app.router.lifespan_context(app):
        pass
    assert manager.start_count == manager.close_count == 1
    assert shutdowns == []


@pytest.mark.parametrize("stage", ["manager", "fastapi", "routes"])
def test_factory_failure_shuts_only_factory_owned_runtime(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)
    failure = RuntimeError(f"{stage}-private-canary")
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    if stage == "manager":
        monkeypatch.setattr(
            main_module,
            "JobManager",
            lambda _settings: (_ for _ in ()).throw(failure),
        )
    elif stage == "fastapi":
        monkeypatch.setattr(
            main_module,
            "FastAPI",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
        )
    else:
        monkeypatch.setattr(main_module, "JobManager", lambda _settings: _Manager())
        monkeypatch.setattr(
            main_module,
            "install_existing_runner_routes",
            lambda *_args: (_ for _ in ()).throw(failure),
        )
    caught: BaseException | None = None
    try:
        main_module.create_app(_settings())
    except BaseException as exc:
        caught = exc
    assert caught is failure
    assert shutdowns == [5_000]


def test_app_state_assignment_failure_preserves_error_and_shuts_owned_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)
    failure = RuntimeError("state-assignment-private-canary")
    manager = _Manager()

    class RejectingState:
        def __setattr__(self, _name: str, _value: object) -> None:
            raise failure

    fake_app = SimpleNamespace(state=RejectingState())
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    monkeypatch.setattr(main_module, "FastAPI", lambda *_args, **_kwargs: fake_app)

    caught: BaseException | None = None
    try:
        main_module.create_app(_settings())
    except BaseException as error:
        caught = error
    assert caught is failure
    assert shutdowns == [5_000]


def test_factory_shutdown_failure_never_masks_the_factory_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_error = RuntimeError("sandbox-factory-error")
    shutdown_error = RuntimeError("sandbox-factory-shutdown-error")
    shutdown_count = 0

    class Runtime:
        def shutdown(self, *, timeout_millis: int) -> None:
            nonlocal shutdown_count
            assert timeout_millis == 5_000
            shutdown_count += 1
            raise shutdown_error

    monkeypatch.setattr(
        main_module,
        "initialize_observability",
        lambda _config: cast(ObservabilityRuntime, Runtime()),
    )
    monkeypatch.setattr(
        main_module,
        "JobManager",
        lambda _settings: (_ for _ in ()).throw(factory_error),
    )
    caught: BaseException | None = None
    try:
        main_module.create_app(_settings())
    except BaseException as error:
        caught = error
    assert caught is factory_error
    assert shutdown_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["start", "close"])
async def test_lifespan_failure_preserves_error_and_still_shuts_owned_runtime(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)
    failure = RuntimeError(f"{stage}-private-canary")

    class Manager(_Manager):
        async def start(self) -> None:
            await super().start()
            if stage == "start":
                raise failure

        async def close(self) -> None:
            await super().close()
            if stage == "close":
                raise failure

    manager = Manager()
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    app = main_module.create_app(_settings())
    caught: BaseException | None = None
    try:
        async with app.router.lifespan_context(app):
            pass
    except BaseException as exc:
        caught = exc
    assert caught is failure
    assert manager.close_count == 1
    assert shutdowns == [5_000]


@pytest.mark.asyncio
async def test_await_cleanup_records_cancellation_before_done_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = asyncio.CancelledError("sandbox-cleanup-race")
    cleanup_error = RuntimeError("sandbox-cleanup-error")

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
@pytest.mark.parametrize("cancellation_stage", ["manager", "runtime"])
async def test_cleanup_cancellation_outranks_ordinary_error_after_all_steps(
    cancellation_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = asyncio.CancelledError(f"{cancellation_stage}-cleanup-cancellation")
    ordinary_error = RuntimeError("ordinary-cleanup-error")
    shutdown_count = 0

    class Runtime:
        def shutdown(self, *, timeout_millis: int) -> None:
            nonlocal shutdown_count
            assert timeout_millis == 5_000
            shutdown_count += 1
            raise cancellation if cancellation_stage == "runtime" else ordinary_error

    class Manager(_Manager):
        async def close(self) -> None:
            await super().close()
            raise cancellation if cancellation_stage == "manager" else ordinary_error

    runtime = cast(ObservabilityRuntime, Runtime())
    manager = Manager()
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    app = main_module.create_app(_settings())

    with pytest.raises(asyncio.CancelledError) as raised:
        async with app.router.lifespan_context(app):
            pass
    assert raised.value is cancellation
    assert manager.close_count == 1
    assert shutdown_count == 1


@pytest.mark.asyncio
async def test_active_body_error_outranks_inner_cleanup_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = asyncio.CancelledError("manager-cleanup-cancellation")
    body_error = RuntimeError("sandbox-body-error")
    body_origin: list[Any] = []
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)

    class Manager(_Manager):
        async def close(self) -> None:
            await super().close()
            raise cancellation

    manager = Manager()
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    app = main_module.create_app(_settings())

    async def fail_body() -> None:
        try:
            raise body_error
        except RuntimeError as error:
            body_origin.append(error.__traceback__)
            raise

    caught: BaseException | None = None
    try:
        async with app.router.lifespan_context(app):
            await fail_body()
    except BaseException as error:
        caught = error
    assert caught is body_error
    traceback = caught.__traceback__
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback is body_origin[0]
    assert manager.close_count == 1
    assert shutdowns == [5_000]


@pytest.mark.asyncio
async def test_lifespan_cleanup_is_reawaited_through_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)

    class Manager(_Manager):
        async def close(self) -> None:
            await super().close()
            cleanup_started.set()
            await release_cleanup.wait()

    manager = Manager()
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    app = main_module.create_app(_settings())

    async def serve() -> None:
        async with app.router.lifespan_context(app):
            body_started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(serve(), name="sandbox-main-test")
    await body_started.wait()
    task.cancel("body-cancellation")
    await cleanup_started.wait()
    task.cancel("cleanup-cancellation-one")
    await asyncio.sleep(0)
    task.cancel("cleanup-cancellation-two")
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.args == ("body-cancellation",)
    assert manager.close_count == 1
    assert shutdowns == [5_000]
    assert not [
        candidate
        for candidate in asyncio.all_tasks()
        if candidate is not asyncio.current_task()
        and candidate.get_name() == "sandbox-runner-cleanup"
        and not candidate.done()
    ]


def test_cli_owns_one_runtime_through_uvicorn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)
    failure = RuntimeError("uvicorn-private-canary")
    settings = _settings()
    captured: dict[str, object] = {}

    def create(settings_arg: object, *, runtime: object) -> object:
        captured["settings"] = settings_arg
        captured["runtime"] = runtime
        return object()

    def run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs
        raise failure

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "create_app", create)
    monkeypatch.setattr(main_module.uvicorn, "run", run)
    with pytest.raises(RuntimeError) as raised:
        main_module.run()
    assert raised.value is failure
    assert captured["settings"] is settings
    assert captured["runtime"] is runtime
    assert cast(dict[str, object], captured["kwargs"])["log_config"] is None
    assert shutdowns == [5_000]


def test_cli_factory_failure_preserves_error_and_shuts_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    runtime = _runtime(shutdowns)
    failure = RuntimeError("sandbox-factory-private-canary")
    settings = _settings()
    uvicorn_calls = 0

    def fail_factory(_settings: object, *, runtime: object) -> object:
        assert runtime is runtime_value
        raise failure

    def reject_uvicorn(*_args: object, **_kwargs: object) -> None:
        nonlocal uvicorn_calls
        uvicorn_calls += 1

    runtime_value = runtime
    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "create_app", fail_factory)
    monkeypatch.setattr(main_module.uvicorn, "run", reject_uvicorn)

    with pytest.raises(RuntimeError) as raised:
        main_module.run()
    assert raised.value is failure
    assert uvicorn_calls == 0
    assert shutdowns == [5_000]


@pytest.mark.parametrize("failure_stage", ["factory", "uvicorn"])
def test_cli_shutdown_failure_never_masks_authoritative_body_error(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_error = RuntimeError(f"{failure_stage}-body-error")
    shutdown_error = RuntimeError("sandbox-shutdown-error")
    shutdown_count = 0
    settings = _settings()

    class Runtime:
        def shutdown(self, *, timeout_millis: int) -> None:
            nonlocal shutdown_count
            assert timeout_millis == 5_000
            shutdown_count += 1
            raise shutdown_error

    runtime = cast(ObservabilityRuntime, Runtime())

    def create(_settings: object, *, runtime: object) -> object:
        assert runtime is runtime_value
        if failure_stage == "factory":
            raise body_error
        return object()

    def run(_app: object, **_kwargs: object) -> None:
        raise body_error

    runtime_value = runtime
    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "create_app", create)
    monkeypatch.setattr(main_module.uvicorn, "run", run)

    caught: BaseException | None = None
    try:
        main_module.run()
    except BaseException as error:
        caught = error
    assert caught is body_error
    assert shutdown_count == 1
