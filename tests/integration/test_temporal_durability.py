"""Phase 1 exit test (b): a durable workflow survives a worker restart.

The heartbeat workflow runs activity -> durable timer -> activity. We restart
the workflow worker while the timer is pending; Temporal must redeliver the
remaining work to the restarted worker and the workflow must still complete.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.client import Client

from jhin_workflows import WORKFLOW_TASK_QUEUE
from jhin_workflows.heartbeat import HeartbeatInput, HeartbeatWorkflow
from tests.integration.conftest import TEMPORAL_ADDRESS, compose

pytestmark = pytest.mark.integration

SLEEP_SECONDS = 12.0


async def test_workflow_completes_across_worker_restart() -> None:
    client = await Client.connect(TEMPORAL_ADDRESS)
    handle = await client.start_workflow(
        HeartbeatWorkflow.run,
        HeartbeatInput(note="restart-proof", sleep_seconds=SLEEP_SECONDS),
        id=f"heartbeat-restart-{uuid.uuid4()}",
        task_queue=WORKFLOW_TASK_QUEUE,
    )

    # Let the first activity finish, then kill the worker mid-timer.
    await asyncio.sleep(2)
    await asyncio.to_thread(compose, "restart", "workflow-worker")

    result = await asyncio.wait_for(handle.result(), timeout=120)
    assert result.beats == 2
    assert result.started_note == "begin:restart-proof"
    assert result.finished_note == "end:restart-proof"
