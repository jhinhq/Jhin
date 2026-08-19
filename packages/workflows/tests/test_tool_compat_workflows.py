"""Stable-ID tool-queue compatibility workflow contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

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
    ResolveBoundToolApprovalInput,
)
from jhin_workflows.tool_compat import (
    AdvertisedCompatibilityInput,
    AdvertisedToolsCompatibilityWorkflow,
    ApprovalCompatibilityInput,
    ApprovalCompatibilityWorkflow,
    CleanupCompatibilityWorkflow,
    SyncExternalCompatibilityWorkflow,
    SyncExternalToolInput,
    ToolStepCompatibilityInput,
    ToolStepCompatibilityWorkflow,
    compatibility_workflow_id,
)
from jhin_workflows.triggered_task.shared import (
    ACTIVITY_SYNC_EXTERNAL_TOOL,
    SyncExternalResult,
)

WORKSPACE_ID = "018f4d52-8b93-7d41-8ac7-7f190f090001"
AGENT_ID = "018f4d52-8b93-7d41-8ac7-7f190f090002"
TASK_ID = "018f4d52-8b93-7d41-8ac7-7f190f090003"
RUN_ID = "018f4d52-8b93-7d41-8ac7-7f190f090004"
APPROVAL_ID = "018f4d52-8b93-7d41-8ac7-7f190f090005"


def test_compatibility_ids_are_exact() -> None:
    assert compatibility_workflow_id("advertised", RUN_ID, step_index=4) == (
        f"phase10-compat-advertised-{RUN_ID}-4"
    )
    assert compatibility_workflow_id("tool-step", RUN_ID, step_index=4) == (
        f"phase10-compat-tool-step-{RUN_ID}-4"
    )
    assert compatibility_workflow_id("approval", APPROVAL_ID) == (
        f"phase10-compat-approval-{APPROVAL_ID}"
    )
    assert compatibility_workflow_id("sync", RUN_ID) == f"phase10-compat-sync-{RUN_ID}"
    assert compatibility_workflow_id("cleanup", RUN_ID) == f"phase10-compat-cleanup-{RUN_ID}"


@dataclass
class _Stubs:
    calls: list[tuple[str, str, Any]] = field(default_factory=list)

    def _record(self, name: str, params: Any) -> None:
        self.calls.append((name, activity.info().task_queue, params))

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def advertised(self, params: ResolveAdvertisedToolsInput) -> list[AdvertisedTool]:
        self._record(ACTIVITY_RESOLVE_ADVERTISED_TOOLS, params)
        return [
            AdvertisedTool(
                name="system.echo",
                description="Echo one value",
                parameters={"type": "object"},
            )
        ]

    @activity.defn(name=ACTIVITY_EXECUTE_BOUND_TOOL)
    async def execute(self, params: ExecuteBoundToolInput) -> BoundToolResult:
        self._record(ACTIVITY_EXECUTE_BOUND_TOOL, params)
        stop = "needs_approval" if params.ordinal == 1 else None
        return BoundToolResult(
            tool_call_id=str(UUID(int=params.ordinal + 1)),
            status="needs_approval" if stop else "executed",
            approval_id=APPROVAL_ID if stop else None,
            stop_reason=stop,
        )

    @activity.defn(name=ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL)
    async def approval(self, params: ResolveBoundToolApprovalInput) -> BoundToolResult:
        self._record(ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL, params)
        return BoundToolResult(tool_call_id=str(UUID(int=9)), status="executed")

    @activity.defn(name=ACTIVITY_SYNC_EXTERNAL_TOOL)
    async def sync(self, params: SyncExternalToolInput) -> SyncExternalResult:
        self._record(ACTIVITY_SYNC_EXTERNAL_TOOL, params)
        return SyncExternalResult(synced=True, detail="https://linear.test/comment/1")

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup(self, params: CleanupRunWorkspaceInput) -> CleanupRunWorkspaceResult:
        self._record(ACTIVITY_CLEANUP_RUN_WORKSPACE, params)
        return CleanupRunWorkspaceResult(deleted=True)


async def _run(
    workflow_run: Any,
    arg: Any,
    stubs: _Stubs,
    *,
    workflow_id: str,
) -> Any:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        async with Worker(
            environment.client,
            task_queue=TOOL_TASK_QUEUE,
            workflows=[
                AdvertisedToolsCompatibilityWorkflow,
                ToolStepCompatibilityWorkflow,
                ApprovalCompatibilityWorkflow,
                SyncExternalCompatibilityWorkflow,
                CleanupCompatibilityWorkflow,
            ],
            activities=[
                stubs.advertised,
                stubs.execute,
                stubs.approval,
                stubs.sync,
                stubs.cleanup,
            ],
        ):
            return await environment.client.execute_workflow(
                workflow_run,
                arg,
                id=workflow_id,
                task_queue=TOOL_TASK_QUEUE,
            )
    finally:
        await environment.shutdown()


async def test_advertised_compatibility_routes_only_ids_to_the_tool_queue() -> None:
    stubs = _Stubs()
    params = AdvertisedCompatibilityInput(workspace_id=WORKSPACE_ID, agent_id=AGENT_ID)

    result = await _run(
        AdvertisedToolsCompatibilityWorkflow.run,
        params,
        stubs,
        workflow_id=compatibility_workflow_id("advertised", RUN_ID, step_index=0),
    )

    assert [tool.name for tool in result] == ["system.echo"]
    assert stubs.calls == [
        (
            ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
            TOOL_TASK_QUEUE,
            ResolveAdvertisedToolsInput(workspace_id=WORKSPACE_ID, agent_id=AGENT_ID),
        )
    ]


async def test_tool_step_compatibility_executes_ordered_prefix_on_the_tool_queue() -> None:
    stubs = _Stubs()
    params = ToolStepCompatibilityInput(
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_index=7,
        call_count=3,
    )

    result = await _run(
        ToolStepCompatibilityWorkflow.run,
        params,
        stubs,
        workflow_id=compatibility_workflow_id("tool-step", RUN_ID, step_index=7),
    )

    assert result == [str(UUID(int=1)), str(UUID(int=2))]
    assert [call[2].ordinal for call in stubs.calls] == [0, 1]
    assert all(call[0] == ACTIVITY_EXECUTE_BOUND_TOOL for call in stubs.calls)
    assert all(call[1] == TOOL_TASK_QUEUE for call in stubs.calls)


async def test_approval_sync_and_cleanup_compatibility_stay_on_the_tool_queue() -> None:
    cases = [
        (
            ApprovalCompatibilityWorkflow.run,
            ApprovalCompatibilityInput(
                workspace_id=WORKSPACE_ID,
                task_id=TASK_ID,
                run_id=RUN_ID,
                agent_id=AGENT_ID,
                approval_id=APPROVAL_ID,
            ),
            compatibility_workflow_id("approval", APPROVAL_ID),
            ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
        ),
        (
            SyncExternalCompatibilityWorkflow.run,
            SyncExternalToolInput(
                workspace_id=WORKSPACE_ID,
                task_id=TASK_ID,
                run_id=RUN_ID,
            ),
            compatibility_workflow_id("sync", RUN_ID),
            ACTIVITY_SYNC_EXTERNAL_TOOL,
        ),
        (
            CleanupCompatibilityWorkflow.run,
            CleanupRunWorkspaceInput(workspace_id=WORKSPACE_ID, run_id=RUN_ID),
            compatibility_workflow_id("cleanup", RUN_ID),
            ACTIVITY_CLEANUP_RUN_WORKSPACE,
        ),
    ]

    for workflow_run, params, workflow_id, expected_activity in cases:
        stubs = _Stubs()
        await _run(workflow_run, params, stubs, workflow_id=workflow_id)
        assert [(name, queue) for name, queue, _params in stubs.calls] == [
            (expected_activity, TOOL_TASK_QUEUE)
        ]
