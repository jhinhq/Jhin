"""Closed-output Temporal workflow-poller health check."""

from __future__ import annotations

import asyncio
import os
import sys
from types import TracebackType

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

from jhin_observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
    initialize_observability,
    normalize_environment,
    service_version,
    temporal_client_interceptors,
)

_READY_OUTPUT = "workflow-poller-ready"
_UNAVAILABLE_OUTPUT = "workflow-poller-unavailable"


async def queue_has_workflow_poller(
    address: str,
    namespace: str,
    queue: str,
    *,
    runtime: ObservabilityRuntime | None = None,
) -> bool:
    """Return whether Temporal reports a workflow poller for one task queue."""
    owned_runtime = runtime is None
    if runtime is None:
        active_runtime = initialize_observability(
            ObservabilityConfig(
                service_name="temporal-poller-check",
                service_version=service_version("jhin-workflows"),
                environment=normalize_environment(os.environ.get("APP_ENV", "production")),
            )
        )
    else:
        active_runtime = runtime
    result = False
    active_error: BaseException | None = None
    active_traceback: TracebackType | None = None
    try:
        client = await Client.connect(
            address,
            namespace=namespace,
            interceptors=temporal_client_interceptors(active_runtime),
        )
        response = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=namespace,
                task_queue=TaskQueue(name=queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            )
        )
        result = bool(response.pollers)
    except BaseException as error:
        active_error = error
        active_traceback = error.__traceback__
    if owned_runtime:
        try:
            active_runtime.shutdown(timeout_millis=5_000)
        except BaseException:
            if active_error is None:
                raise
    if active_error is not None:
        raise active_error.with_traceback(active_traceback)
    return result


async def main(queue: str) -> int:
    """Check one queue using only the worker's standard Temporal environment."""
    try:
        ready = await queue_has_workflow_poller(
            os.environ["TEMPORAL_ADDRESS"],
            os.environ["TEMPORAL_NAMESPACE"],
            queue,
        )
    except Exception:
        ready = False
    print(_READY_OUTPUT if ready else _UNAVAILABLE_OUTPUT)
    return 0 if ready else 1


def run() -> None:
    """Console entrypoint accepting exactly one positional task queue."""
    if len(sys.argv) != 2 or not sys.argv[1]:
        print(_UNAVAILABLE_OUTPUT)
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main(sys.argv[1])))


if __name__ == "__main__":
    run()


__all__ = ["main", "queue_has_workflow_poller", "run"]
