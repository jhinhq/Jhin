"""MemoryMaintenanceWorkflow: extract → policy → persist, durably and
independently of the originating chat turn or task.

Started best-effort (``start_memory_maintenance``) with a deterministic
workflow id so retries from the API or the worker can never double-apply.
The workflow itself never raises: every failure is returned as a typed
result, because memory maintenance failure must never fail the origin.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows.memory_maintenance.shared import (
    ACTIVITY_APPLY_MEMORY_CANDIDATES,
    ACTIVITY_EXTRACT_MEMORY_CANDIDATES,
    ApplyMemoryCandidatesInput,
    ApplyMemoryCandidatesResult,
    ExtractMemoryCandidatesInput,
    ExtractMemoryCandidatesResult,
    MemoryMaintenanceInput,
    MemoryMaintenanceResult,
)

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


@workflow.defn(name="MemoryMaintenanceWorkflow")
class MemoryMaintenanceWorkflow:
    @workflow.run
    async def run(self, params: MemoryMaintenanceInput) -> MemoryMaintenanceResult:
        workflow_id = workflow.info().workflow_id
        try:
            extraction: ExtractMemoryCandidatesResult = await workflow.execute_activity(
                ACTIVITY_EXTRACT_MEMORY_CANDIDATES,
                ExtractMemoryCandidatesInput(
                    workspace_id=params.workspace_id,
                    agent_id=params.agent_id,
                    source_kind=params.source_kind,
                    source_id=params.source_id,
                    task_id=params.task_id,
                    conversation_id=params.conversation_id,
                ),
                result_type=ExtractMemoryCandidatesResult,
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=_RETRY,
            )
        except Exception as exc:
            return MemoryMaintenanceResult(
                status="extraction_failed",
                workflow_id=workflow_id,
                extraction_error=f"{type(exc).__name__}"[:200],
            )
        if not extraction.ok:
            return MemoryMaintenanceResult(
                status="extraction_failed",
                workflow_id=workflow_id,
                extraction_error=extraction.error[:200],
            )
        if not extraction.candidates_json:
            return MemoryMaintenanceResult(status="nothing_to_remember", workflow_id=workflow_id)

        try:
            applied: ApplyMemoryCandidatesResult = await workflow.execute_activity(
                ACTIVITY_APPLY_MEMORY_CANDIDATES,
                ApplyMemoryCandidatesInput(
                    workspace_id=params.workspace_id,
                    agent_id=params.agent_id,
                    source_kind=params.source_kind,
                    source_id=params.source_id,
                    candidates_json=extraction.candidates_json,
                    task_id=params.task_id,
                    conversation_id=params.conversation_id,
                    remember_enabled=params.remember_enabled,
                    requested_scope=params.requested_scope,
                    actor_user_id=params.actor_user_id,
                    actor_authority=params.actor_authority,
                    idempotency_key=workflow_id,
                ),
                result_type=ApplyMemoryCandidatesResult,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=_RETRY,
            )
        except Exception as exc:
            return MemoryMaintenanceResult(
                status="apply_failed",
                workflow_id=workflow_id,
                candidate_count=len(extraction.candidates_json),
                extraction_error=f"{type(exc).__name__}"[:200],
            )
        return MemoryMaintenanceResult(
            status="applied" if applied.ok else "apply_failed",
            workflow_id=workflow_id,
            candidate_count=len(extraction.candidates_json),
            apply=applied,
        )
