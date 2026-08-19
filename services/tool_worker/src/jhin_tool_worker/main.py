"""Process entrypoint for the dedicated tool worker."""

from __future__ import annotations

import asyncio
import logging
import signal

from temporalio.client import Client
from temporalio.worker import Worker

from jhin_connectors import build_default_catalog
from jhin_tool_worker.activities import ToolActivities
from jhin_tool_worker.resources import ToolWorkerResources
from jhin_tool_worker.settings import ToolWorkerSettings
from jhin_workflows import TOOL_TASK_QUEUE

logger = logging.getLogger(__name__)


def configure_current_logging(log_level: str) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


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
                "Temporal connection failed (%s); retrying in %.1f seconds",
                type(error).__name__[:100],
                delay,
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
                "Resource connection failed (%s); retrying in %.1f seconds",
                type(error).__name__[:100],
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def main() -> None:
    settings = ToolWorkerSettings()
    configure_current_logging(settings.log_level)
    client = await connect_with_retry(settings)
    resources = await resources_with_retry(settings)
    catalog = build_default_catalog()
    activities = ToolActivities(resources, catalog)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(handled_signal, stop.set)

    worker = Worker(
        client,
        task_queue=TOOL_TASK_QUEUE,
        activities=[
            activities.resolve_advertised_tools_activity,
            activities.execute_bound_tool_activity,
            activities.resolve_bound_tool_approval_activity,
        ],
    )
    try:
        async with worker:
            logger.info("Tool worker started on task queue %s", TOOL_TASK_QUEUE)
            await stop.wait()
            logger.info("Tool worker stopping")
    finally:
        await resources.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()


__all__ = ["configure_current_logging", "main", "run"]
