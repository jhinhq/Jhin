"""Small deterministic workflows that preserve Phase 9 activity effects."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
    AdvertisedTool,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    ExecuteBoundToolInput,
    ResolveAdvertisedToolsInput,
)
from jhin_workflows.tool_compat.shared import (
    AdvertisedCompatibilityInput,
    ApprovalCompatibilityInput,
    SyncExternalToolInput,
    ToolStepCompatibilityInput,
)
from jhin_workflows.triggered_task.shared import (
    ACTIVITY_SYNC_EXTERNAL_TOOL,
    SyncExternalResult,
)

_READ_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)
_EFFECT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)
_CLEANUP_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn(name="AdvertisedToolsCompatibilityWorkflow")
class AdvertisedToolsCompatibilityWorkflow:
    @workflow.run
    async def run(self, params: AdvertisedCompatibilityInput) -> list[AdvertisedTool]:
        tools: list[AdvertisedTool] = await workflow.execute_activity(
            ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
            ResolveAdvertisedToolsInput(
                workspace_id=params.workspace_id,
                agent_id=params.agent_id,
                task_id=params.task_id,
            ),
            result_type=list[AdvertisedTool],
            task_queue=TOOL_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_READ_RETRY,
        )
        return tools


@workflow.defn(name="ToolStepCompatibilityWorkflow")
class ToolStepCompatibilityWorkflow:
    @workflow.run
    async def run(self, params: ToolStepCompatibilityInput) -> list[str]:
        tool_call_ids: list[str] = []
        for ordinal in range(params.call_count):
            result: BoundToolResult = await workflow.execute_activity(
                ACTIVITY_EXECUTE_BOUND_TOOL,
                ExecuteBoundToolInput(
                    workspace_id=params.workspace_id,
                    run_id=params.run_id,
                    step_index=params.step_index,
                    ordinal=ordinal,
                ),
                result_type=BoundToolResult,
                task_queue=TOOL_TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_EFFECT_RETRY,
            )
            tool_call_ids.append(result.tool_call_id)
            if result.stop_reason is not None:
                break
        return tool_call_ids


@workflow.defn(name="ApprovalCompatibilityWorkflow")
class ApprovalCompatibilityWorkflow:
    @workflow.run
    async def run(self, params: ApprovalCompatibilityInput) -> BoundToolResult:
        result: BoundToolResult = await workflow.execute_activity(
            ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
            params,
            result_type=BoundToolResult,
            task_queue=TOOL_TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_EFFECT_RETRY,
        )
        return result


@workflow.defn(name="SyncExternalCompatibilityWorkflow")
class SyncExternalCompatibilityWorkflow:
    @workflow.run
    async def run(self, params: SyncExternalToolInput) -> SyncExternalResult:
        result: SyncExternalResult = await workflow.execute_activity(
            ACTIVITY_SYNC_EXTERNAL_TOOL,
            params,
            result_type=SyncExternalResult,
            task_queue=TOOL_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_EFFECT_RETRY,
        )
        return result


@workflow.defn(name="CleanupCompatibilityWorkflow")
class CleanupCompatibilityWorkflow:
    @workflow.run
    async def run(
        self,
        params: CleanupRunWorkspaceInput,
    ) -> CleanupRunWorkspaceResult:
        result: CleanupRunWorkspaceResult = await workflow.execute_activity(
            ACTIVITY_CLEANUP_RUN_WORKSPACE,
            params,
            result_type=CleanupRunWorkspaceResult,
            task_queue=TOOL_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_CLEANUP_RETRY,
        )
        return result


__all__ = [
    "AdvertisedToolsCompatibilityWorkflow",
    "ApprovalCompatibilityWorkflow",
    "CleanupCompatibilityWorkflow",
    "SyncExternalCompatibilityWorkflow",
    "ToolStepCompatibilityWorkflow",
]
