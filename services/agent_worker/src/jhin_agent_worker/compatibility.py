"""Phase 9 activity names as stable coordinators over tool-queue workflows.

These handlers intentionally contain no connector catalog or sandbox client.
They validate durable identities, reattach idempotent compatibility workflows,
and delegate model reasoning/projection to the split agent-only helpers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from jhin_agent_worker.projections import AgentProjectionActivities
from jhin_agent_worker.reasoning import AgentReasoningActivities
from jhin_agent_worker.resources import Resources
from jhin_db.models import AgentRun, Task
from jhin_tools import MAX_TOOL_CALLS_PER_STEP, MAX_TOOL_STEP_INDEX, stable_tool_invocation_id
from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.agent_task.shared import (
    ACTIVITY_FINALIZE_RUN,
    ACTIVITY_RESOLVE_APPROVAL,
    ACTIVITY_RUN_AGENT_STEP,
    AdvertisedTool,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    FinalizeInput,
    ReasonAgentStepInput,
    ResolveApprovalInput,
    RunStepInput,
    StepResult,
)
from jhin_workflows.tool_compat import (
    AdvertisedCompatibilityInput,
    AdvertisedToolsCompatibilityWorkflow,
    ApprovalCompatibilityInput,
    ApprovalCompatibilityWorkflow,
    CleanupCompatibilityWorkflow,
    ToolStepCompatibilityInput,
    ToolStepCompatibilityWorkflow,
    compatibility_workflow_id,
)


def _invalid_identity(message: str) -> ApplicationError:
    return ApplicationError(
        message,
        type="compatibility_identity_invalid",
        non_retryable=True,
    )


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise _invalid_identity(f"legacy {field} is not a UUID") from error


async def compatibility_result(
    client: Client,
    workflow_run: Callable[..., Awaitable[Any]],
    arg: Any,
    *,
    workflow_id: str,
) -> Any:
    """Start one stable compatibility workflow or reattach its prior run."""
    try:
        handle = await client.start_workflow(
            workflow_run,
            arg,
            id=workflow_id,
            task_queue=TOOL_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle_for(workflow_run, workflow_id)
    return await handle.result()


class AgentCompatibilityActivities:
    """Implement the three Phase 9 agent activity names as coordinators."""

    def __init__(
        self,
        resources: Resources,
        temporal_client: Client,
        *,
        reasoning: AgentReasoningActivities | None = None,
        projections: AgentProjectionActivities | None = None,
    ) -> None:
        self._resources = resources
        self._client = temporal_client
        self._reasoning = reasoning or AgentReasoningActivities(resources)
        self._projections = projections or AgentProjectionActivities(
            resources,
            temporal_client=temporal_client,
        )

    @activity.defn(name=ACTIVITY_RUN_AGENT_STEP)
    async def run_agent_step_activity(self, params: RunStepInput) -> StepResult:
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        task_id = _uuid(params.task_id, field="task_id")
        run_id = _uuid(params.run_id, field="run_id")
        agent_id = _uuid(params.agent_id, field="agent_id")
        if not 0 <= params.step_index <= MAX_TOOL_STEP_INDEX:
            raise _invalid_identity("legacy step_index is outside the supported range")

        advertised_id = compatibility_workflow_id(
            "advertised",
            run_id,
            step_index=params.step_index,
        )
        advertised_raw = await compatibility_result(
            self._client,
            AdvertisedToolsCompatibilityWorkflow.run,
            AdvertisedCompatibilityInput(
                workspace_id=workspace_id,
                agent_id=agent_id,
                task_id=task_id,
            ),
            workflow_id=advertised_id,
        )
        if not isinstance(advertised_raw, list) or any(
            not isinstance(tool, AdvertisedTool) for tool in advertised_raw
        ):
            raise ApplicationError(
                "advertised compatibility result is malformed",
                type="compatibility_result_invalid",
                non_retryable=True,
            )

        reasoned = await self._reasoning.reason_agent_step(
            ReasonAgentStepInput(
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
                snapshot_json=params.snapshot_json,
                step_index=params.step_index,
                instruction=params.instruction,
                user_instructions=list(params.user_instructions),
                advertised_tools=advertised_raw,
            ),
            legacy_sidecar_repair=True,
        )
        if not 0 <= reasoned.call_count <= MAX_TOOL_CALLS_PER_STEP:
            raise ApplicationError(
                "legacy reasoning call count is outside the supported range",
                type="compatibility_result_invalid",
                non_retryable=True,
            )

        tool_step_id = compatibility_workflow_id(
            "tool-step",
            run_id,
            step_index=params.step_index,
        )
        tool_ids_raw = await compatibility_result(
            self._client,
            ToolStepCompatibilityWorkflow.run,
            ToolStepCompatibilityInput(
                workspace_id=workspace_id,
                run_id=run_id,
                step_index=params.step_index,
                call_count=reasoned.call_count,
            ),
            workflow_id=tool_step_id,
        )
        if (
            not isinstance(tool_ids_raw, list)
            or any(not isinstance(tool_id, str) for tool_id in tool_ids_raw)
            or len(tool_ids_raw) > reasoned.call_count
            or (reasoned.call_count > 0 and not tool_ids_raw)
        ):
            raise ApplicationError(
                "tool-step compatibility result is malformed",
                type="compatibility_result_invalid",
                non_retryable=True,
            )
        canonical_ids = [
            str(stable_tool_invocation_id(UUID(run_id), params.step_index, ordinal))
            for ordinal in range(len(tool_ids_raw))
        ]
        if tool_ids_raw != canonical_ids:
            raise ApplicationError(
                "tool-step compatibility result changed its stable identities",
                type="compatibility_result_invalid",
                non_retryable=True,
            )

        return await self._projections.commit_agent_step_activity(
            CommitAgentStepInput(
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
                step_index=params.step_index,
                gateway_tool_call_ids=tool_ids_raw,
            )
        )

    @activity.defn(name=ACTIVITY_RESOLVE_APPROVAL)
    async def resolve_approval_activity(self, params: ResolveApprovalInput) -> StepResult:
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        task_id = _uuid(params.task_id, field="task_id")
        run_id = _uuid(params.run_id, field="run_id")
        agent_id = _uuid(params.agent_id, field="agent_id")
        approval_id = _uuid(params.approval_id, field="approval_id")
        result = await compatibility_result(
            self._client,
            ApprovalCompatibilityWorkflow.run,
            ApprovalCompatibilityInput(
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
                approval_id=approval_id,
            ),
            workflow_id=compatibility_workflow_id("approval", approval_id),
        )
        if not isinstance(result, BoundToolResult):
            raise ApplicationError(
                "approval compatibility result is malformed",
                type="compatibility_result_invalid",
                non_retryable=True,
            )
        tool_call_id = _uuid(result.tool_call_id, field="tool_call_id")
        return await self._projections.commit_approval_projection_activity(
            CommitApprovalProjectionInput(
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
                approval_id=approval_id,
                tool_call_id=tool_call_id,
            )
        )

    @activity.defn(name=ACTIVITY_FINALIZE_RUN)
    async def finalize_run_activity(self, params: FinalizeInput) -> None:
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        task_id = _uuid(params.task_id, field="task_id")
        run_id = _uuid(params.run_id, field="run_id") if params.run_id is not None else None
        if run_id is not None:
            async with self._resources.session_factory() as session:
                bound_run_id = await session.scalar(
                    select(AgentRun.id)
                    .join(Task, AgentRun.task_id == Task.id)
                    .where(
                        AgentRun.id == UUID(run_id),
                        AgentRun.workspace_id == UUID(workspace_id),
                        AgentRun.task_id == UUID(task_id),
                        Task.id == UUID(task_id),
                        Task.workspace_id == UUID(workspace_id),
                    )
                )
            if bound_run_id is None:
                raise ApplicationError(
                    "legacy finalize context does not bind the workspace, task, and run",
                    type="compatibility_context_invalid",
                    non_retryable=True,
                )
            await compatibility_result(
                self._client,
                CleanupCompatibilityWorkflow.run,
                CleanupRunWorkspaceInput(
                    workspace_id=workspace_id,
                    run_id=run_id,
                ),
                workflow_id=compatibility_workflow_id("cleanup", run_id),
            )
        await self._projections.finalize_run_projection_activity(
            FinalizeInput(
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                status=params.status,
                steps_used=params.steps_used,
                error_code=params.error_code,
                error_message=params.error_message,
            )
        )


__all__ = ["AgentCompatibilityActivities", "compatibility_result"]
