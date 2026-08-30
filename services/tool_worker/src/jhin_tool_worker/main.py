"""Process entrypoint for the dedicated tool worker."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from types import TracebackType
from typing import Any

from temporalio.client import Client

from jhin_connectors import build_default_catalog
from jhin_observability import (
    ObservabilityRuntime,
    build_temporal_worker,
    connect_temporal_client,
    get_logger,
    initialize_observability,
    service_version,
)
from jhin_secrets.redaction import redact_event_dict
from jhin_tool_worker.activities import ToolActivities
from jhin_tool_worker.cleanup_activities import CleanupActivities
from jhin_tool_worker.oauth_refresh import install_refresh_on_use
from jhin_tool_worker.resources import ToolWorkerResources
from jhin_tool_worker.settings import ToolWorkerSettings
from jhin_tool_worker.trigger_activities import TriggerToolActivities
from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.tool_compat import (
    AdvertisedToolsCompatibilityWorkflow,
    ApprovalCompatibilityWorkflow,
    CleanupCompatibilityWorkflow,
    SyncExternalCompatibilityWorkflow,
    ToolStepCompatibilityWorkflow,
)

logger = get_logger(__name__)


async def connect_with_retry(
    settings: ToolWorkerSettings,
    runtime: ObservabilityRuntime,
) -> Client:
    delay = 1.0
    while True:
        try:
            return await connect_temporal_client(settings, runtime)
        except Exception as error:
            logger.warning(
                "temporal.connect_retry",
                error_type=type(error).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def resources_with_retry(
    settings: ToolWorkerSettings,
    runtime: ObservabilityRuntime,
) -> ToolWorkerResources:
    delay = 1.0
    while True:
        try:
            return await ToolWorkerResources.create(settings, runtime=runtime)
        except Exception as error:
            logger.warning(
                "resources.retry",
                error_type=type(error).__name__,
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
    resources: ToolWorkerResources | None,
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
    if resources is not None:
        try:
            await resources.close()
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
    settings = ToolWorkerSettings()
    resources: ToolWorkerResources | None = None
    worker: Any | None = None
    worker_exit_needed = False
    loop: asyncio.AbstractEventLoop | None = None
    registered_signals: list[signal.Signals] = []
    active_error: BaseException | None = None
    active_traceback: TracebackType | None = None
    runtime = initialize_observability(
        settings.observability_config(
            service_name="tool-worker",
            service_version=service_version("jhin-tool-worker"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    try:
        client = await connect_with_retry(settings, runtime)
        resources = await resources_with_retry(settings, runtime)
        catalog = build_default_catalog()
        tools = ToolActivities(resources, catalog)
        triggers = TriggerToolActivities(resources, catalog)
        cleanup = CleanupActivities(resources)
        # This process runs the connector tools and holds a master key, so a
        # tool call reaching a nearly-stale OAuth token can renew it in the
        # moment rather than waiting for the next sweep.
        install_refresh_on_use()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for handled_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(handled_signal, stop.set)
            registered_signals.append(handled_signal)

        tool_workflows = [
            AdvertisedToolsCompatibilityWorkflow,
            ToolStepCompatibilityWorkflow,
            ApprovalCompatibilityWorkflow,
            SyncExternalCompatibilityWorkflow,
            CleanupCompatibilityWorkflow,
        ]
        tool_activities: list[Callable[..., Any]] = [
            tools.resolve_advertised_tools_activity,
            tools.execute_bound_tool_activity,
            tools.resolve_bound_tool_approval_activity,
            tools.resolve_bound_tool_review_activity,
            triggers.sync_external_tool_activity,
            cleanup.cleanup_run_workspace_activity,
        ]
        worker = build_temporal_worker(
            client,
            runtime=runtime,
            task_queue=TOOL_TASK_QUEUE,
            workflows=tool_workflows,
            activities=tool_activities,
        )
        await worker.__aenter__()
        worker_exit_needed = True
        logger.info("worker.started", task_queue=TOOL_TASK_QUEUE)
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
            resources=resources,
            runtime=runtime,
            active_error=active_error,
            active_traceback=active_traceback,
        ),
        name="tool-worker-cleanup",
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


__all__ = ["main", "run"]
