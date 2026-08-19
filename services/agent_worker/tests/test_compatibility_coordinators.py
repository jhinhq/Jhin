"""Phase 9 activity names reattach stable tool workflows without local effects."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from jhin_agent_worker.compatibility import (
    AgentCompatibilityActivities,
    compatibility_result,
)
from jhin_agent_worker.trigger_activities import TriggerCompatibilityActivities
from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.agent_task.shared import (
    AdvertisedTool,
    BoundToolResult,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    FinalizeInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveApprovalInput,
    RunStepInput,
    StepResult,
)
from jhin_workflows.tool_compat import (
    CleanupCompatibilityWorkflow,
    SyncExternalCompatibilityWorkflow,
    SyncExternalToolInput,
    compatibility_workflow_id,
)
from jhin_workflows.triggered_task.shared import SyncExternalInput, SyncExternalResult

WORKSPACE_ID = "018f4d52-8b93-7d41-8ac7-7f190f091001"
TASK_ID = "018f4d52-8b93-7d41-8ac7-7f190f091002"
RUN_ID = "018f4d52-8b93-7d41-8ac7-7f190f091003"
AGENT_ID = "018f4d52-8b93-7d41-8ac7-7f190f091004"
APPROVAL_ID = "018f4d52-8b93-7d41-8ac7-7f190f091005"
TOOL_CALL_ID = "bde966e2-384b-5429-adcd-b7a81fde775e"


@dataclass
class _Handle:
    value: Any

    async def result(self) -> Any:
        return self.value


@dataclass
class _Client:
    results: dict[str, Any]
    already_started: set[str] = field(default_factory=set)
    starts: list[tuple[Any, Any, str, str]] = field(default_factory=list)
    reattachments: list[str] = field(default_factory=list)

    async def start_workflow(
        self,
        workflow_run: Any,
        arg: Any,
        *,
        id: str,
        task_queue: str,
    ) -> _Handle:
        self.starts.append((workflow_run, arg, id, task_queue))
        if id in self.already_started:
            raise WorkflowAlreadyStartedError(id, "compatibility-workflow")
        return _Handle(self.results[id])

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        self.reattachments.append(workflow_id)
        return _Handle(self.results[workflow_id])


@dataclass
class _Reasoning:
    call_count: int
    calls: list[tuple[ReasonAgentStepInput, bool]] = field(default_factory=list)

    async def reason_agent_step(
        self,
        params: ReasonAgentStepInput,
        *,
        legacy_sidecar_repair: bool = False,
    ) -> ReasonAgentStepResult:
        self.calls.append((params, legacy_sidecar_repair))
        return ReasonAgentStepResult(call_count=self.call_count)


@dataclass
class _Projections:
    step_calls: list[CommitAgentStepInput] = field(default_factory=list)
    approval_calls: list[CommitApprovalProjectionInput] = field(default_factory=list)
    finalize_calls: list[FinalizeInput] = field(default_factory=list)

    async def commit_agent_step_activity(self, params: CommitAgentStepInput) -> StepResult:
        self.step_calls.append(params)
        return StepResult(done=False)

    async def commit_approval_projection_activity(
        self, params: CommitApprovalProjectionInput
    ) -> StepResult:
        self.approval_calls.append(params)
        return StepResult(done=False)

    async def finalize_run_projection_activity(self, params: FinalizeInput) -> None:
        self.finalize_calls.append(params)


def _run_step() -> RunStepInput:
    return RunStepInput(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        run_id=RUN_ID,
        agent_id=AGENT_ID,
        snapshot_json="{}",
        step_index=4,
        instruction="continue",
        user_instructions=["keep the bound call unchanged"],
    )


def _compatibility_results() -> dict[str, Any]:
    return {
        compatibility_workflow_id("advertised", RUN_ID, step_index=4): [
            AdvertisedTool(
                name="system.echo",
                description="Echo one value",
                parameters={"type": "object"},
            )
        ],
        compatibility_workflow_id("tool-step", RUN_ID, step_index=4): [TOOL_CALL_ID],
        compatibility_workflow_id("approval", APPROVAL_ID): BoundToolResult(
            tool_call_id=TOOL_CALL_ID,
            status="executed",
        ),
        compatibility_workflow_id("cleanup", RUN_ID): object(),
        compatibility_workflow_id("sync", RUN_ID): SyncExternalResult(
            synced=True, detail="https://linear.test/comment/1"
        ),
    }


async def test_legacy_step_repairs_reasoning_then_uses_stable_tool_workflows() -> None:
    client = _Client(_compatibility_results())
    reasoning = _Reasoning(call_count=1)
    projections = _Projections()
    coordinator = AgentCompatibilityActivities(
        resources=object(),  # type: ignore[arg-type]
        temporal_client=client,  # type: ignore[arg-type]
        reasoning=reasoning,  # type: ignore[arg-type]
        projections=projections,  # type: ignore[arg-type]
    )

    result = await coordinator.run_agent_step_activity(_run_step())

    assert result == StepResult(done=False)
    assert reasoning.calls[0][1] is True
    assert [tool.name for tool in reasoning.calls[0][0].advertised_tools] == ["system.echo"]
    assert projections.step_calls == [
        CommitAgentStepInput(
            workspace_id=WORKSPACE_ID,
            task_id=TASK_ID,
            run_id=RUN_ID,
            agent_id=AGENT_ID,
            step_index=4,
            gateway_tool_call_ids=[TOOL_CALL_ID],
        )
    ]
    assert [(workflow_id, queue) for _run, _arg, workflow_id, queue in client.starts] == [
        (compatibility_workflow_id("advertised", RUN_ID, step_index=4), TOOL_TASK_QUEUE),
        (compatibility_workflow_id("tool-step", RUN_ID, step_index=4), TOOL_TASK_QUEUE),
    ]


async def test_legacy_retry_reattaches_tool_step_without_any_local_effect() -> None:
    tool_workflow_id = compatibility_workflow_id("tool-step", RUN_ID, step_index=4)
    client = _Client(_compatibility_results(), already_started={tool_workflow_id})
    projections = _Projections()
    coordinator = AgentCompatibilityActivities(
        resources=object(),  # type: ignore[arg-type]
        temporal_client=client,  # type: ignore[arg-type]
        reasoning=_Reasoning(call_count=1),  # type: ignore[arg-type]
        projections=projections,  # type: ignore[arg-type]
    )

    result = await coordinator.run_agent_step_activity(_run_step())

    assert result.done is False
    assert client.reattachments == [tool_workflow_id]
    assert projections.step_calls[0].gateway_tool_call_ids == [TOOL_CALL_ID]


async def test_legacy_approval_and_finalize_reattach_by_stable_identity() -> None:
    client = _Client(_compatibility_results())
    projections = _Projections()
    coordinator = AgentCompatibilityActivities(
        resources=object(),  # type: ignore[arg-type]
        temporal_client=client,  # type: ignore[arg-type]
        reasoning=_Reasoning(call_count=0),  # type: ignore[arg-type]
        projections=projections,  # type: ignore[arg-type]
    )
    approval = ResolveApprovalInput(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        run_id=RUN_ID,
        agent_id=AGENT_ID,
        approval_id=APPROVAL_ID,
        decision="tampered-advisory-value",
    )
    finalize = FinalizeInput(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        run_id=RUN_ID,
        status="completed",
        steps_used=4,
    )

    approval_result = await coordinator.resolve_approval_activity(approval)
    await coordinator.finalize_run_activity(finalize)

    assert approval_result == StepResult(done=False)
    assert projections.approval_calls == [
        CommitApprovalProjectionInput(
            workspace_id=WORKSPACE_ID,
            task_id=TASK_ID,
            run_id=RUN_ID,
            agent_id=AGENT_ID,
            approval_id=APPROVAL_ID,
            tool_call_id=TOOL_CALL_ID,
        )
    ]
    assert projections.finalize_calls == [finalize]
    assert [workflow_id for _run, _arg, workflow_id, _queue in client.starts] == [
        compatibility_workflow_id("approval", APPROVAL_ID),
        compatibility_workflow_id("cleanup", RUN_ID),
    ]


async def test_legacy_trigger_sync_ignores_advisory_payload_and_uses_ids_only() -> None:
    client = _Client(_compatibility_results())
    coordinator = TriggerCompatibilityActivities(client)  # type: ignore[arg-type]
    params = SyncExternalInput(
        workspace_id=WORKSPACE_ID,
        connection_id="advisory-connection-is-ignored",
        external_source="advisory-source-is-ignored",
        external_id="advisory-id-is-ignored",
        task_id=TASK_ID,
        run_id=RUN_ID,
        agent_id="advisory-agent-is-ignored",
        run_status="advisory-status-is-ignored",
        trigger_name="advisory-name-is-ignored",
    )

    result = await coordinator.sync_external_activity(params)

    assert result == SyncExternalResult(synced=True, detail="https://linear.test/comment/1")
    workflow_run, arg, workflow_id, queue = client.starts[0]
    assert workflow_run is SyncExternalCompatibilityWorkflow.run
    assert arg == SyncExternalToolInput(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        run_id=RUN_ID,
    )
    assert workflow_id == compatibility_workflow_id("sync", RUN_ID)
    assert queue == TOOL_TASK_QUEUE


async def test_invalid_legacy_identity_stops_before_temporal_io() -> None:
    client = _Client(_compatibility_results())
    coordinator = TriggerCompatibilityActivities(client)  # type: ignore[arg-type]
    params = SyncExternalInput(
        workspace_id="not-a-uuid",
        connection_id="ignored",
        external_source="ignored",
        external_id="ignored",
        task_id=TASK_ID,
        run_id=RUN_ID,
        agent_id="ignored",
        run_status="ignored",
        trigger_name="ignored",
    )

    with pytest.raises(ApplicationError) as error:
        await coordinator.sync_external_activity(params)

    assert error.value.type == "compatibility_identity_invalid"
    assert error.value.non_retryable is True
    assert client.starts == []


async def test_compatibility_result_reattaches_an_existing_workflow() -> None:
    workflow_id = compatibility_workflow_id("cleanup", RUN_ID)
    expected = object()
    client = _Client({workflow_id: expected}, already_started={workflow_id})

    result = await compatibility_result(
        client,  # type: ignore[arg-type]
        CleanupCompatibilityWorkflow.run,
        object(),
        workflow_id=workflow_id,
    )

    assert result is expected
    assert client.reattachments == [workflow_id]


def test_agent_compatibility_module_has_no_connector_or_runner_import() -> None:
    path = Path(__file__).parents[1] / "src" / "jhin_agent_worker" / "compatibility.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert all(not name.startswith("jhin_connectors") for name in imported)
    assert all("runner" not in name for name in imported)
    source = path.read_text(encoding="utf-8")
    assert "build_default_catalog" not in source
    assert "delete_workspace" not in source
