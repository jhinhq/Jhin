"""Workflow worker: registers Jhin workflows/activities on the named task queue.

Health visibility: once connected to Temporal, a heartbeat file is touched
every few seconds; the compose healthcheck (``jhin-health-check``) verifies
its freshness.
"""

from __future__ import annotations

import asyncio
import signal

from temporalio.client import Client
from temporalio.worker import Worker

from jhin_observability import configure_json_logging, get_logger, normalize_environment
from jhin_observability.healthfile import clear_heartbeat, run_heartbeat
from jhin_workflow_worker.settings import Settings
from jhin_workflows import WORKFLOW_TASK_QUEUE
from jhin_workflows.heartbeat import HeartbeatWorkflow, record_beat

logger = get_logger(__name__)


async def connect_with_retry(settings: Settings) -> Client:
    delay = 1.0
    while True:
        try:
            return await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
        except Exception as exc:
            logger.warning(
                "temporal.connect_retry",
                error_type=type(exc).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def main() -> None:
    settings = Settings()
    configure_json_logging(
        service="workflow-worker",
        environment=normalize_environment(settings.app_env),
        level=settings.log_level,
    )

    client = await connect_with_retry(settings)
    logger.info(
        "temporal.connected",
        task_queue=WORKFLOW_TASK_QUEUE,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    heartbeat_task = asyncio.create_task(run_heartbeat())
    worker = Worker(
        client,
        task_queue=WORKFLOW_TASK_QUEUE,
        workflows=[HeartbeatWorkflow],
        activities=[record_beat],
    )
    try:
        async with worker:
            logger.info("worker.started", task_queue=WORKFLOW_TASK_QUEUE)
            await stop.wait()
            logger.info("worker.stopping")
    finally:
        heartbeat_task.cancel()
        clear_heartbeat()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
