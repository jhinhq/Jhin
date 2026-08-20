"""Process entrypoint for the dedicated tool worker."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from jhin_connectors import build_default_catalog
from jhin_observability import configure_json_logging, get_logger, normalize_environment
from jhin_secrets.redaction import redact_event_dict
from jhin_tool_worker.activities import ToolActivities
from jhin_tool_worker.cleanup_activities import CleanupActivities
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


async def connect_with_retry(settings: ToolWorkerSettings) -> Client:
    delay = 1.0
    while True:
        try:
            return await Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
            )
        except Exception as error:
            logger.warning(
                "temporal.connect_retry",
                error_type=type(error).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def resources_with_retry(settings: ToolWorkerSettings) -> ToolWorkerResources:
    delay = 1.0
    while True:
        try:
            return await ToolWorkerResources.create(settings)
        except Exception as error:
            logger.warning(
                "resources.retry",
                error_type=type(error).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def main() -> None:
    settings = ToolWorkerSettings()
    configure_json_logging(
        service="tool-worker",
        environment=normalize_environment(settings.app_env),
        level=settings.log_level,
        extra_processors=(redact_event_dict,),
    )
    client = await connect_with_retry(settings)
    resources = await resources_with_retry(settings)
    try:
        catalog = build_default_catalog()
        tools = ToolActivities(resources, catalog)
        triggers = TriggerToolActivities(resources, catalog)
        cleanup = CleanupActivities(resources)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for handled_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(handled_signal, stop.set)

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
            triggers.sync_external_tool_activity,
            cleanup.cleanup_run_workspace_activity,
        ]
        worker = Worker(
            client,
            task_queue=TOOL_TASK_QUEUE,
            workflows=tool_workflows,
            activities=tool_activities,
        )
        async with worker:
            logger.info("worker.started", task_queue=TOOL_TASK_QUEUE)
            await stop.wait()
            logger.info("worker.stopping")
    finally:
        await resources.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()


__all__ = ["main", "run"]
