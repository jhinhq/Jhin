"""Workflow worker: registers Jhin workflows/activities on the named task queue.

Health visibility: once connected to Temporal, a heartbeat file is touched
every few seconds; the compose healthcheck (``jhin-health-check``) verifies
its freshness.
"""

from __future__ import annotations

import asyncio
import signal
from types import TracebackType
from typing import Any

from temporalio.client import Client

from jhin_observability import (
    ObservabilityRuntime,
    build_temporal_worker,
    connect_temporal_client,
    get_logger,
    initialize_observability,
    service_version,
)
from jhin_observability.healthfile import clear_heartbeat, run_heartbeat
from jhin_secrets.redaction import redact_event_dict
from jhin_workflow_worker.settings import Settings
from jhin_workflows import WORKFLOW_TASK_QUEUE
from jhin_workflows.heartbeat import HeartbeatWorkflow, record_beat

logger = get_logger(__name__)


async def connect_with_retry(
    settings: Settings,
    runtime: ObservabilityRuntime,
) -> Client:
    delay = 1.0
    while True:
        try:
            return await connect_temporal_client(settings, runtime)
        except Exception as exc:
            logger.warning(
                "temporal.connect_retry",
                error_type=type(exc).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def _cleanup_process(
    *,
    loop: asyncio.AbstractEventLoop | None,
    registered_signals: list[signal.Signals],
    worker: Any | None,
    worker_exit_needed: bool,
    heartbeat_task: asyncio.Task[None] | None,
    runtime: ObservabilityRuntime,
    active_error: BaseException | None,
    active_traceback: TracebackType | None,
) -> BaseException | None:
    first_cancellation: asyncio.CancelledError | None = None
    first_error: BaseException | None = None

    def remember(error: BaseException) -> None:
        nonlocal first_cancellation, first_error
        if isinstance(error, asyncio.CancelledError):
            if first_cancellation is None:
                first_cancellation = error
        elif first_error is None:
            first_error = error

    if loop is not None:
        for handled_signal in registered_signals:
            try:
                loop.remove_signal_handler(handled_signal)
            except BaseException as error:
                remember(error)
    if worker is not None and worker_exit_needed:
        try:
            await worker.__aexit__(
                type(active_error) if active_error is not None else None,
                active_error,
                active_traceback,
            )
        except BaseException as error:
            remember(error)
    if heartbeat_task is not None:
        cancel_succeeded = False
        try:
            heartbeat_task.cancel()
            cancel_succeeded = True
        except BaseException as error:
            remember(error)
        try:
            await heartbeat_task
        except asyncio.CancelledError as error:
            if not cancel_succeeded:
                remember(error)
        except BaseException as error:
            remember(error)
    try:
        clear_heartbeat()
    except BaseException as error:
        remember(error)
    try:
        runtime.shutdown(timeout_millis=5_000)
    except BaseException as error:
        remember(error)
    return first_cancellation or first_error


async def _await_cleanup(
    task: asyncio.Task[BaseException | None],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
            if task.done():
                return task.result(), cancellation


async def main() -> None:
    settings = Settings()
    heartbeat_task: asyncio.Task[None] | None = None
    worker: Any | None = None
    worker_exit_needed = False
    loop: asyncio.AbstractEventLoop | None = None
    registered_signals: list[signal.Signals] = []
    active_error: BaseException | None = None
    active_traceback: TracebackType | None = None
    runtime = initialize_observability(
        settings.observability_config(
            service_name="workflow-worker",
            service_version=service_version("jhin-workflow-worker"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    try:
        client = await connect_with_retry(settings, runtime)
        logger.info("temporal.connected", task_queue=WORKFLOW_TASK_QUEUE)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for handled_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(handled_signal, stop.set)
            registered_signals.append(handled_signal)
        workflows = [HeartbeatWorkflow]
        activities = [record_beat]
        heartbeat_task = asyncio.create_task(run_heartbeat())
        worker = build_temporal_worker(
            client,
            runtime=runtime,
            task_queue=WORKFLOW_TASK_QUEUE,
            workflows=workflows,
            activities=activities,
        )
        await worker.__aenter__()
        worker_exit_needed = True
        logger.info("worker.started", task_queue=WORKFLOW_TASK_QUEUE)
        await stop.wait()
        logger.info("worker.stopping")
    except BaseException as error:
        active_error = error
        active_traceback = error.__traceback__

    cleanup_task = asyncio.create_task(
        _cleanup_process(
            loop=loop,
            registered_signals=registered_signals,
            worker=worker,
            worker_exit_needed=worker_exit_needed,
            heartbeat_task=heartbeat_task,
            runtime=runtime,
            active_error=active_error,
            active_traceback=active_traceback,
        ),
        name="workflow-worker-cleanup",
    )
    cleanup_error, cleanup_cancellation = await _await_cleanup(cleanup_task)
    if isinstance(active_error, asyncio.CancelledError):
        raise active_error.with_traceback(active_traceback)
    if cleanup_cancellation is not None:
        raise cleanup_cancellation
    if active_error is not None:
        raise active_error.with_traceback(active_traceback)
    if cleanup_error is not None:
        raise cleanup_error


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
