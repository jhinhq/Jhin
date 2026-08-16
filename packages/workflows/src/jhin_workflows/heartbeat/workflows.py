"""Sample durable workflow: activity → durable timer → activity.

Used by the Phase 1 exit test to prove a workflow survives a worker restart:
the timer fires on the Temporal server, so killing the worker mid-sleep and
restarting it must still produce a completed workflow.
"""

from datetime import timedelta

from temporalio import workflow

from jhin_workflows.heartbeat.shared import HeartbeatInput, HeartbeatResult

with workflow.unsafe.imports_passed_through():
    from jhin_workflows.heartbeat.activities import record_beat

_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class HeartbeatWorkflow:
    @workflow.run
    async def run(self, params: HeartbeatInput) -> HeartbeatResult:
        started_note = await workflow.execute_activity(
            record_beat,
            f"begin:{params.note}",
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
        await workflow.sleep(params.sleep_seconds)
        finished_note = await workflow.execute_activity(
            record_beat,
            f"end:{params.note}",
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
        return HeartbeatResult(started_note=started_note, finished_note=finished_note, beats=2)
