"""Closed-output Temporal workflow-poller health check."""

from __future__ import annotations

import asyncio
import os
import sys

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

_READY_OUTPUT = "workflow-poller-ready"
_UNAVAILABLE_OUTPUT = "workflow-poller-unavailable"


async def queue_has_workflow_poller(address: str, namespace: str, queue: str) -> bool:
    """Return whether Temporal reports a workflow poller for one task queue."""
    client = await Client.connect(address, namespace=namespace)
    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=namespace,
            task_queue=TaskQueue(name=queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        )
    )
    return bool(response.pollers)


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
