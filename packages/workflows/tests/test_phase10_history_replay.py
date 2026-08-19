from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, cast

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
EXPECTED_PHASE9_REF = "6318781b57692bf39f37cd428d73de115d7458e2"
EXPECTED_TEMPORAL_SDK_VERSION = "1.31.0"
_TERMINAL_WORKFLOW_EVENTS = frozenset(
    {
        "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT",
        "EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW",
    }
)
_EVIDENCE_PATTERN = re.compile(
    r"<!-- phase9-evidence:start -->\s*```json\s*(\{.*\})\s*```\s*"
    r"<!-- phase9-evidence:end -->",
    re.DOTALL,
)

EXPECTED_OLD_ACTIVITIES = {
    "agent-tool-step.json": {"resolve_snapshot", "run_agent_step", "finalize_run"},
    "agent-post-bind-pre-effect.json": {"resolve_snapshot", "run_agent_step"},
    "agent-parked-approval.json": {"resolve_snapshot", "run_agent_step"},
    "agent-finalization.json": {"resolve_snapshot", "run_agent_step", "finalize_run"},
    "triggered-sync.json": {"prepare_triggered_task", "sync_external"},
    "engineering-sync.json": {"prepare_triggered_task", "sync_external"},
}
_PHASE10_PATCH_MARKERS = (
    "phase10-tool-worker-boundary-v1",
    "phase10-trigger-sync-tool-routing-v1",
    "phase10-engineering-sync-tool-routing-v1",
)


def _copy_fixture_root(tmp_path: Path) -> Path:
    destination = tmp_path / "phase9_temporal"
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


def _fixture_history(fixture: Path, *, workflow_id: str) -> WorkflowHistory:
    return WorkflowHistory.from_json(
        workflow_id,
        fixture.read_text(encoding="utf-8"),
    )


def _committed_evidence(root: Path) -> dict[str, Any]:
    match = _EVIDENCE_PATTERN.search((root / "README.md").read_text(encoding="utf-8"))
    if match is None:
        raise ValueError("committed Phase 9 evidence manifest is missing")
    evidence = json.loads(match.group(1))
    if not isinstance(evidence, dict):
        raise ValueError("committed Phase 9 evidence manifest is malformed")
    return cast(dict[str, Any], evidence)


def assert_frozen_history_evidence(root: Path) -> None:
    evidence = _committed_evidence(root)
    source_ref = evidence.get("source_ref")
    if source_ref != EXPECTED_PHASE9_REF:
        raise ValueError("committed source ref does not match the Phase 9 barrier ref")
    if (root / "phase9-ref.txt").read_text(encoding="utf-8") != f"{source_ref}\n":
        raise ValueError("phase9-ref.txt source ref does not match committed evidence")
    sdk_version = evidence.get("temporal_sdk_version")
    if sdk_version != EXPECTED_TEMPORAL_SDK_VERSION:
        raise ValueError("committed Temporal SDK version is incorrect")

    expected_fixtures = evidence.get("fixtures")
    if not isinstance(expected_fixtures, dict):
        raise ValueError("committed fixture evidence is malformed")
    actual_names = {path.name for path in root.glob("*.json")}
    if actual_names != set(expected_fixtures):
        raise ValueError("committed fixture set does not match exact evidence")

    for filename, raw_expected in expected_fixtures.items():
        if not isinstance(filename, str) or not isinstance(raw_expected, dict):
            raise ValueError("committed fixture entry is malformed")
        fixture = root / filename
        raw = fixture.read_bytes()
        document = json.loads(raw)
        if set(document) != {"events"} or not isinstance(document["events"], list):
            raise ValueError(f"{filename} is not an SDK-only history document")
        events = document["events"]
        if not events:
            raise ValueError(f"{filename} has no history events")

        started = events[0].get("workflowExecutionStartedEventAttributes", {})
        workflow_type = started.get("workflowType", {}).get("name")
        task_queue = started.get("taskQueue", {}).get("name")
        if workflow_type != raw_expected.get("workflow_type"):
            raise ValueError(f"{filename} workflow type metadata drifted")
        if task_queue != raw_expected.get("task_queue"):
            raise ValueError(f"{filename} task queue metadata drifted")

        sdk_versions = {
            event.get("workflowTaskCompletedEventAttributes", {})
            .get("sdkMetadata", {})
            .get("sdkVersion")
            for event in events
            if event.get("workflowTaskCompletedEventAttributes", {})
            .get("sdkMetadata", {})
            .get("sdkVersion")
        }
        if sdk_versions != {sdk_version}:
            raise ValueError(f"{filename} SDK version metadata drifted")
        if len(events) != raw_expected.get("event_count"):
            raise ValueError(f"{filename} exact event count drifted")
        if events[-1].get("eventType") != raw_expected.get("last_event_type"):
            raise ValueError(f"{filename} exact end state drifted")
        closed = any(event.get("eventType") in _TERMINAL_WORKFLOW_EVENTS for event in events)
        if closed is not raw_expected.get("closed"):
            raise ValueError(f"{filename} closed/open end state drifted")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != raw_expected.get("sha256"):
            raise ValueError(f"{filename} digest drifted")


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
    commit = CommitAgentStepInput("workspace", "task", "run", "agent", 2)
    assert commit.gateway_tool_call_ids == []
    assert commit.cancelled_after_tool_call_id is None
    approval = ResolveBoundToolApprovalInput("workspace", "task", "run", "agent", "approval")
    assert approval.approval_id == "approval"
    assert (
        CommitApprovalProjectionInput(
            "workspace", "task", "run", "agent", "approval", "tool-call"
        ).tool_call_id
        == "tool-call"
    )
    assert CleanupRunWorkspaceInput("workspace", "run").run_id == "run"
    assert CleanupRunWorkspaceResult(deleted=True).deleted is True


def test_frozen_histories_have_only_phase9_commands() -> None:
    for filename, names in EXPECTED_OLD_ACTIVITIES.items():
        text = (FIXTURE_ROOT / filename).read_text(encoding="utf-8")
        recorded = set(re.findall(r'"activityType"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"', text))
        assert names.issubset(recorded)
        assert all(marker not in text for marker in _PHASE10_PATCH_MARKERS)


def test_committed_frozen_history_evidence_matches_exact_bytes_and_metadata() -> None:
    assert_frozen_history_evidence(FIXTURE_ROOT)


def test_committed_evidence_rejects_mutated_fixture(tmp_path: Path) -> None:
    root = _copy_fixture_root(tmp_path)
    fixture = root / "agent-tool-step.json"
    fixture.write_text(
        fixture.read_text(encoding="utf-8").replace('"eventId": "1"', '"eventId": "99"', 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="digest"):
        assert_frozen_history_evidence(root)


def test_committed_evidence_rejects_replaced_fixture(tmp_path: Path) -> None:
    root = _copy_fixture_root(tmp_path)
    (root / "agent-tool-step.json").write_bytes((root / "agent-finalization.json").read_bytes())

    with pytest.raises(ValueError):
        assert_frozen_history_evidence(root)


def test_committed_evidence_rejects_mutated_phase9_ref(tmp_path: Path) -> None:
    root = _copy_fixture_root(tmp_path)
    (root / "phase9-ref.txt").write_text("0" * 40 + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source ref"):
        assert_frozen_history_evidence(root)


def test_committed_evidence_rejects_mutated_sdk_metadata(tmp_path: Path) -> None:
    root = _copy_fixture_root(tmp_path)
    fixture = root / "agent-tool-step.json"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    completed = next(
        event
        for event in document["events"]
        if "sdkMetadata" in event.get("workflowTaskCompletedEventAttributes", {})
    )
    completed["workflowTaskCompletedEventAttributes"]["sdkMetadata"]["sdkVersion"] = "9.9.9"
    fixture.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SDK version"):
        assert_frozen_history_evidence(root)


@pytest.mark.parametrize("fixture", sorted(FIXTURE_ROOT.glob("*.json")), ids=lambda p: p.name)
async def test_phase9_history_replays_with_phase10_workflows(fixture: Path) -> None:
    workflow_id = f"phase9-replay-{fixture.stem}"
    history = _fixture_history(fixture, workflow_id=workflow_id)

    assert history.workflow_id == workflow_id
    await Replayer(
        workflows=[AgentTaskWorkflow, TriggeredTaskWorkflow, EngineeringTicketWorkflow]
    ).replay_workflow(history)
