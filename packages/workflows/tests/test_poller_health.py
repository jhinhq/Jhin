"""Temporal task-queue poller health contract."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
import structlog
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.client import Client

import jhin_workflows.poller_health as poller_health
from jhin_observability import (
    ObservabilityNotInitializedError,
    ObservabilityRuntime,
    get_runtime,
)


@pytest.fixture(autouse=True)
def restore_logging_globals() -> Iterator[None]:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_disabled = root.disabled
    original_named = {
        candidate: (list(candidate.handlers), candidate.level, candidate.propagate)
        for candidate in logging.root.manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    }
    original_structlog_config = cast(
        dict[str, Any],
        {
            key: list(value) if key == "processors" else value
            for key, value in structlog.get_config().items()
        },
    )
    try:
        yield
    finally:
        installed_handlers = [
            handler for handler in root.handlers if handler not in original_handlers
        ]
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        root.disabled = original_disabled
        for named, (handlers, level, propagate) in original_named.items():
            named.handlers[:] = handlers
            named.setLevel(level)
            named.propagate = propagate
        structlog.configure(**original_structlog_config)
        for handler in installed_handlers:
            handler.close()


class _WorkflowService:
    def __init__(self, pollers: list[object], *, failure: Exception | None = None) -> None:
        self.pollers = pollers
        self.failure = failure
        self.requests: list[Any] = []

    async def describe_task_queue(self, request: Any) -> object:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(pollers=self.pollers)


@pytest.mark.parametrize(("pollers", "expected"), [([], False), ([object()], True)])
async def test_poller_health_uses_raw_workflow_queue_description(
    pollers: list[object],
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkflowService(pollers)

    async def connect(address: str, *, namespace: str, interceptors: object) -> object:
        assert address == "temporal.internal:7233"
        assert namespace == "tenant-a"
        assert isinstance(interceptors, list)
        return SimpleNamespace(workflow_service=service)

    monkeypatch.setattr(Client, "connect", connect)

    assert (
        await poller_health.queue_has_workflow_poller(
            "temporal.internal:7233", "tenant-a", "jhin-tool-queue"
        )
        is expected
    )
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.namespace == "tenant-a"
    assert request.task_queue.name == "jhin-tool-queue"
    assert request.task_queue_type == TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()


@pytest.mark.parametrize(
    ("result", "expected_code", "expected_output"),
    [
        (True, 0, "workflow-poller-ready\n"),
        (False, 1, "workflow-poller-unavailable\n"),
    ],
)
async def test_cli_reads_exact_environment_and_emits_closed_status(
    result: bool,
    expected_code: int,
    expected_output: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal.private:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "private-namespace")

    async def check(address: str, namespace: str, queue: str) -> bool:
        assert (address, namespace, queue) == (
            "temporal.private:7233",
            "private-namespace",
            "jhin-agent-queue",
        )
        return result

    monkeypatch.setattr(poller_health, "queue_has_workflow_poller", check)

    assert await poller_health.main("jhin-agent-queue") == expected_code
    assert capsys.readouterr() == (expected_output, "")


@pytest.mark.parametrize("failure_stage", ["connect", "rpc"])
async def test_cli_maps_temporal_failures_to_one_closed_output(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "do-not-print.example:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "do-not-print-namespace")
    service = _WorkflowService([], failure=RuntimeError("do-not-print-rpc"))
    shutdown = Mock()
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(tracer=object(), shutdown=shutdown),
    )
    initialize = Mock(return_value=runtime)

    async def connect(_address: str, *, namespace: str, interceptors: object) -> object:
        assert namespace == "do-not-print-namespace"
        assert isinstance(interceptors, list)
        if failure_stage == "connect":
            raise RuntimeError("do-not-print-connect")
        return SimpleNamespace(workflow_service=service)

    monkeypatch.setattr(poller_health, "initialize_observability", initialize)
    monkeypatch.setattr(Client, "connect", connect)

    assert await poller_health.main("do-not-print-queue") == 1
    captured = capsys.readouterr()
    assert captured == ("workflow-poller-unavailable\n", "")
    assert "do-not-print" not in captured.out + captured.err
    initialize.assert_called_once()
    shutdown.assert_called_once_with(timeout_millis=5_000)


@pytest.mark.asyncio
async def test_poller_forwards_the_builders_exact_interceptor_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_runtime = cast(ObservabilityRuntime, object())
    expected_interceptors = [object()]
    interceptor_builder = Mock(return_value=expected_interceptors)
    connected: list[tuple[tuple[object, ...], dict[str, object]]] = []
    service = _WorkflowService([object()])

    async def connect(*args: object, **kwargs: object) -> object:
        connected.append((args, dict(kwargs)))
        return SimpleNamespace(workflow_service=service)

    monkeypatch.setattr(
        poller_health,
        "temporal_client_interceptors",
        interceptor_builder,
    )
    monkeypatch.setattr(Client, "connect", connect)
    assert await poller_health.queue_has_workflow_poller(
        "temporal.test:7233",
        "default",
        "jhin-workflow-queue",
        runtime=active_runtime,
    )
    assert len(connected) == 1
    args, kwargs = connected[0]
    assert args == ("temporal.test:7233",)
    assert kwargs["namespace"] == "default"
    assert kwargs["interceptors"] is expected_interceptors
    interceptor_builder.assert_called_once_with(active_runtime)


@pytest.mark.asyncio
async def test_falsey_injected_runtime_preserves_identity_and_is_never_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = Mock()

    class FalseyRuntime:
        tracer = object()

        def __bool__(self) -> bool:
            return False

        def shutdown(self, *, timeout_millis: int) -> None:
            shutdown(timeout_millis=timeout_millis)

    runtime = cast(ObservabilityRuntime, FalseyRuntime())
    expected_interceptors = [object()]
    interceptor_builder = Mock(return_value=expected_interceptors)
    service = _WorkflowService([object()])

    async def connect(_address: str, **kwargs: object) -> object:
        assert kwargs["interceptors"] is expected_interceptors
        return SimpleNamespace(workflow_service=service)

    monkeypatch.setattr(
        poller_health,
        "initialize_observability",
        lambda _config: (_ for _ in ()).throw(AssertionError("must not initialize")),
    )
    monkeypatch.setattr(
        poller_health,
        "temporal_client_interceptors",
        interceptor_builder,
    )
    monkeypatch.setattr(Client, "connect", connect)

    assert await poller_health.queue_has_workflow_poller(
        "temporal.test:7233",
        "default",
        "queue",
        runtime=runtime,
    )
    interceptor_builder.assert_called_once_with(runtime)
    shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_owned_poller_runtime_uses_closed_environment_and_shuts_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            tracer=object(),
            shutdown=lambda timeout_millis=5_000: shutdowns.append(timeout_millis),
        ),
    )
    configs: list[object] = []
    service = _WorkflowService([])

    def initialize(config: object) -> ObservabilityRuntime:
        configs.append(config)
        return runtime

    async def connect(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(workflow_service=service)

    monkeypatch.setenv("APP_ENV", "invalid-private-canary")
    monkeypatch.setattr(poller_health, "initialize_observability", initialize)
    monkeypatch.setattr(Client, "connect", connect)
    assert not await poller_health.queue_has_workflow_poller("temporal", "default", "queue")
    assert len(configs) == 1
    assert configs[0].environment == "production"
    assert configs[0].service_name == "temporal-poller-check"
    assert shutdowns == [5_000]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["connect", "rpc"])
@pytest.mark.parametrize("failure_kind", ["business", "cancellation"])
async def test_owned_runtime_shutdown_failure_never_masks_active_authority(
    failure_stage: str,
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_error: BaseException
    if failure_kind == "cancellation":
        active_error = asyncio.CancelledError(f"{failure_stage}-cancelled")
    else:
        active_error = RuntimeError(f"{failure_stage}-failed")
    shutdown_error = RuntimeError("shutdown-failed")
    shutdowns: list[int] = []

    class Runtime:
        tracer = object()

        def shutdown(self, *, timeout_millis: int) -> None:
            shutdowns.append(timeout_millis)
            raise shutdown_error

    class Service:
        async def describe_task_queue(self, _request: object) -> object:
            raise active_error

    async def connect(*_args: object, **_kwargs: object) -> object:
        if failure_stage == "connect":
            raise active_error
        return SimpleNamespace(workflow_service=Service())

    monkeypatch.setattr(
        poller_health,
        "initialize_observability",
        lambda _config: cast(ObservabilityRuntime, Runtime()),
    )
    monkeypatch.setattr(Client, "connect", connect)
    caught: BaseException | None = None
    try:
        await poller_health.queue_has_workflow_poller("temporal", "default", "queue")
    except BaseException as error:
        caught = error
    assert caught is active_error
    assert shutdowns == [5_000]
