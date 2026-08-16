"""Start the sample durable heartbeat workflow and wait for its result.

Usage (dev stack must be running with Temporal reachable):

    uv run python scripts/start_sample_workflow.py [--sleep-seconds 15]

Used both manually (`make sample-workflow`) and by the Phase 1 durability
exit test: start with a long timer, restart the workflow worker mid-sleep,
and the workflow must still complete.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from temporalio.client import Client

from jhin_workflows import WORKFLOW_TASK_QUEUE
from jhin_workflows.heartbeat import HeartbeatInput, HeartbeatWorkflow


async def start(sleep_seconds: float, note: str, wait: bool) -> str:
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    workflow_id = f"heartbeat-{uuid.uuid4()}"
    handle = await client.start_workflow(
        HeartbeatWorkflow.run,
        HeartbeatInput(note=note, sleep_seconds=sleep_seconds),
        id=workflow_id,
        task_queue=WORKFLOW_TASK_QUEUE,
    )
    print(f"started workflow_id={workflow_id} run_id={handle.result_run_id}")
    if wait:
        result = await handle.result()
        print(f"completed: beats={result.beats} start={result.started_note!r}")
    return workflow_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sleep-seconds", type=float, default=5.0)
    parser.add_argument("--note", default="sample")
    parser.add_argument("--no-wait", action="store_true", help="start without awaiting the result")
    args = parser.parse_args()
    asyncio.run(start(args.sleep_seconds, args.note, wait=not args.no_wait))


if __name__ == "__main__":
    main()
