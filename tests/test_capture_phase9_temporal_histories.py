from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory

capture = importlib.import_module("scripts.capture_phase9_temporal_histories")


async def test_save_history_uses_real_event_sdk_json_and_caller_workflow_id(
    tmp_path: Path,
) -> None:
    event = HistoryEvent(
        event_id=1,
        event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
    )
    event.workflow_execution_started_event_attributes.workflow_type.name = "AgentTaskWorkflow"
    event.workflow_execution_started_event_attributes.task_queue.name = "jhin-agent-queue"
    fetched = WorkflowHistory("server-returned-id", [event])
    handle = SimpleNamespace(fetch_history=AsyncMock(return_value=fetched))
    destination = tmp_path / "agent-tool-step.json"

    await capture.save_history(
        handle,
        destination,
        workflow_id="caller-supplied-workflow-id",
    )

    raw = json.loads(destination.read_text(encoding="utf-8"))
    assert set(raw) == {"events"}
    assert raw["events"] == [
        {
            "eventId": "1",
            "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED",
            "workflowExecutionStartedEventAttributes": {
                "workflowType": {"name": "AgentTaskWorkflow"},
                "taskQueue": {"name": "jhin-agent-queue"},
            },
        }
    ]
    reconstructed = WorkflowHistory.from_json("caller-supplied-workflow-id", raw)
    assert reconstructed.workflow_id == "caller-supplied-workflow-id"
    assert reconstructed.events[0] == event


def test_capture_defaults_to_the_development_database_port() -> None:
    assert capture.DEFAULT_CAPTURE_DATABASE_URL == (
        "postgresql+asyncpg://jhin:jhin@127.0.0.1:55432/jhin"
    )


async def test_generate_writes_exact_ref_and_reconstructs_each_caller_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = HistoryEvent(
        event_id=1,
        event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
    )
    captures = {
        scenario: capture.CapturedWorkflow(
            workflow_id=f"phase9-{scenario}-workflow",
            handle=SimpleNamespace(
                fetch_history=AsyncMock(return_value=WorkflowHistory("server-id", [event]))
            ),
        )
        for scenario in capture.SCENARIOS
    }
    monkeypatch.setattr(capture, "capture_scenarios", AsyncMock(return_value=captures))
    source_ref = "0123456789abcdef0123456789abcdef01234567"
    await capture.generate(tmp_path, source_ref=source_ref)
    assert (tmp_path / "phase9-ref.txt").read_text(encoding="utf-8") == f"{source_ref}\n"
    for scenario, captured in captures.items():
        restored = WorkflowHistory.from_json(
            captured.workflow_id,
            (tmp_path / f"{scenario}.json").read_text(encoding="utf-8"),
        )
        assert restored.workflow_id == captured.workflow_id
        assert restored.events == [event]
