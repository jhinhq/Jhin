from __future__ import annotations

import re
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskWorkflow
from jhin_workflows.agent_task.shared import (
    AdvertisedTool,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    ExecuteBoundToolInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    ResolveBoundToolApprovalInput,
    RunStepInput,
)
from jhin_workflows.engineering_ticket import EngineeringTicketWorkflow
from jhin_workflows.triggered_task import TriggeredTaskWorkflow

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "phase9_temporal"

EXPECTED_OLD_ACTIVITIES = {
    "agent-tool-step.json": {"resolve_snapshot", "run_agent_step", "finalize_run"},
    "agent-post-bind-pre-effect.json": {"resolve_snapshot", "run_agent_step"},
    "agent-parked-approval.json": {"resolve_snapshot", "run_agent_step"},
    "agent-finalization.json": {"resolve_snapshot", "run_agent_step", "finalize_run"},
    "triggered-sync.json": {"prepare_triggered_task", "sync_external"},
    "engineering-sync.json": {"prepare_triggered_task", "sync_external"},
}

WORKFLOW_TYPES = {
    "agent-tool-step.json": AgentTaskWorkflow,
    "agent-post-bind-pre-effect.json": AgentTaskWorkflow,
    "agent-parked-approval.json": AgentTaskWorkflow,
    "agent-finalization.json": AgentTaskWorkflow,
    "triggered-sync.json": TriggeredTaskWorkflow,
    "engineering-sync.json": EngineeringTicketWorkflow,
}


def test_tool_queue_name_is_stable() -> None:
    assert TOOL_TASK_QUEUE == "jhin-tool-queue"


def test_tool_worker_contracts_are_dependency_light_and_preserve_caller_fields() -> None:
    advertised = AdvertisedTool(
        name="linear.issue.get",
        description="Fetch one issue",
        parameters={"type": "object"},
    )
    base = RunStepInput(
        workspace_id="workspace",
        task_id="task",
        run_id="run",
        agent_id="agent",
        snapshot_json="{}",
        step_index=2,
    )

    assert ResolveAdvertisedToolsInput("workspace", "agent").agent_id == "agent"
    assert ReasonAgentStepInput(**vars(base), advertised_tools=[advertised]).advertised_tools == [
        advertised
    ]
    assert ReasonAgentStepResult(call_count=1).call_count == 1
    assert ExecuteBoundToolInput("workspace", "run", 2, 0).ordinal == 0
    assert BoundToolResult("tool-call", "completed").approval_id is None
    assert CommitAgentStepInput("workspace", "task", "run", "agent", 2).gateway_tool_call_ids == []
    approval = ResolveBoundToolApprovalInput("workspace", "task", "run", "agent", "approval")
    assert approval.approval_id == "approval"
    assert CommitApprovalProjectionInput(
        "workspace", "task", "run", "agent", "approval", "tool-call"
    ).tool_call_id == "tool-call"
    assert CleanupRunWorkspaceInput("workspace", "run").run_id == "run"
    assert CleanupRunWorkspaceResult(deleted=True).deleted is True


def test_frozen_histories_have_only_phase9_commands() -> None:
    for filename, names in EXPECTED_OLD_ACTIVITIES.items():
        text = (FIXTURE_ROOT / filename).read_text(encoding="utf-8")
        recorded = set(
            re.findall(r'"activityType"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"', text)
        )
        assert names.issubset(recorded)
        assert "phase10-tool-worker-boundary-v1" not in text


@pytest.mark.parametrize(("filename", "workflow_type"), WORKFLOW_TYPES.items())
async def test_frozen_phase9_history_replays(filename: str, workflow_type: type) -> None:
    workflow_id = f"phase9-replay-{filename.removesuffix('.json')}"
    history = WorkflowHistory.from_json(
        workflow_id,
        (FIXTURE_ROOT / filename).read_text(encoding="utf-8"),
    )

    assert history.workflow_id == workflow_id
    await Replayer(workflows=[workflow_type]).replay_workflow(history)
