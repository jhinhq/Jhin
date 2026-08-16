"""Workflow logic test using Temporal's time-skipping test environment.

Downloads a local test server binary on first run (cached afterwards); no
running Temporal cluster is required.
"""

import uuid

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.heartbeat import HeartbeatInput, HeartbeatWorkflow, record_beat


async def test_heartbeat_workflow_completes_with_time_skipping() -> None:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[HeartbeatWorkflow],
            activities=[record_beat],
        ):
            result = await env.client.execute_workflow(
                HeartbeatWorkflow.run,
                HeartbeatInput(note="unit", sleep_seconds=3600),
                id=f"heartbeat-{uuid.uuid4()}",
                task_queue=task_queue,
            )
        assert result.beats == 2
        assert result.started_note == "begin:unit"
        assert result.finished_note == "end:unit"
    finally:
        await env.shutdown()
