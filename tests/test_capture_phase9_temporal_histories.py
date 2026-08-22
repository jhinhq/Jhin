from __future__ import annotations

import hashlib
import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


def test_fixture_evidence_rejects_non_phase9_temporal_sdk(tmp_path: Path) -> None:
    fixture = tmp_path / "wrong-sdk.json"
    fixture.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "eventType": "EVENT_TYPE_WORKFLOW_TASK_COMPLETED",
                        "workflowTaskCompletedEventAttributes": {
                            "sdkMetadata": {"sdkVersion": "9.9.9"}
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="SDK version"):
        capture._fixture_evidence(fixture)


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
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"<!-- phase9-evidence:start -->\s*```json\s*(\{.*\})\s*```\s*"
        r"<!-- phase9-evidence:end -->",
        readme,
        re.DOTALL,
    )
    assert match is not None
    evidence = json.loads(match.group(1))
    assert evidence["source_ref"] == source_ref
    assert evidence["temporal_sdk_version"] == "1.31.0"
    for scenario, captured in captures.items():
        fixture = tmp_path / f"{scenario}.json"
        restored = WorkflowHistory.from_json(
            captured.workflow_id,
            fixture.read_text(encoding="utf-8"),
        )
        assert restored.workflow_id == captured.workflow_id
        assert restored.events == [event]
        committed = evidence["fixtures"][f"{scenario}.json"]
        assert committed["sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert committed["event_count"] == 1
        assert committed["last_event_type"] == ("EVENT_TYPE_WORKFLOW_EXECUTION_STARTED")


@pytest.mark.parametrize(
    "dirty_path",
    [
        "services/agent_worker/src/jhin_agent_worker/activities.py",
        "packages/tools/src/jhin_tools/test_barriers.py",
    ],
)
async def test_dirty_capture_critical_source_rejects_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_path: str,
) -> None:
    def fake_git(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return str(capture.TASK0_PHASE9_REF)
        if args == ("status", "--porcelain", "--untracked-files=all"):
            return f" M {dirty_path}"
        raise AssertionError(f"unexpected git arguments: {args!r}")

    migrate = Mock()
    generate = AsyncMock()
    monkeypatch.setattr(capture, "_git", fake_git)
    monkeypatch.setattr(capture, "upgrade_to_head", migrate)
    monkeypatch.setattr(capture, "generate", generate)

    with pytest.raises(RuntimeError, match=dirty_path):
        await capture._run_capture(tmp_path, capture.DEFAULT_CAPTURE_DATABASE_URL)

    migrate.assert_not_called()
    generate.assert_not_awaited()


def test_capture_cleanliness_allows_only_intentional_task1_paths() -> None:
    intentional = "\n".join(
        [
            "?? scripts/capture_phase9_temporal_histories.py",
            "?? tests/test_capture_phase9_temporal_histories.py",
            "?? packages/workflows/tests/test_phase10_history_replay.py",
            "?? packages/workflows/tests/fixtures/phase9_temporal/agent-tool-step.json",
            " M apps/api/tests/test_approvals_unit.py",
        ]
    )
    assert capture.unexpected_capture_dirty_paths(intentional) == ()


@pytest.mark.parametrize(
    "dirty_path",
    [
        "pyproject.toml",
        "uv.lock",
        "services/agent_worker/src/jhin_agent_worker/trigger_activities.py",
        "packages/agents/src/jhin_agents/runtime.py",
        "packages/connectors/src/jhin_connectors/linear/tools.py",
        "packages/db/src/jhin_db/models/work.py",
        "packages/domain/src/jhin_domain/enums.py",
        "packages/events/src/jhin_events/envelope.py",
        "packages/models/src/jhin_models/factory.py",
        "packages/observability/src/jhin_observability/logging.py",
        "packages/policy/src/jhin_policy/evaluator.py",
        "packages/secrets/src/jhin_secrets/crypto.py",
        "packages/tools/src/jhin_tools/gateway.py",
        "packages/workflows/src/jhin_workflows/agent_task/workflows.py",
    ],
)
def test_capture_cleanliness_rejects_every_capture_critical_source_root(
    dirty_path: str,
) -> None:
    assert capture.unexpected_capture_dirty_paths(f" M {dirty_path}") == (dirty_path,)


def _exact_post_bind_state() -> dict[str, object]:
    return {
        "manifest_payloads": [
            {
                "step": 0,
                "manifest": {
                    "count": 1,
                    "calls": [
                        {
                            "ordinal": 0,
                            "lossless": True,
                            "tool_name": "capture.phase9.read",
                            "arguments_json": '{"value":"phase9-canonical-argument"}',
                        }
                    ],
                },
            }
        ],
        "reasoning_count": 0,
        "tool_count": 0,
        "stable_tool_present": False,
        "effect_started": False,
    }


def test_exact_post_bind_state_accepts_only_the_canonical_boundary() -> None:
    capture.assert_exact_post_bind_state(**_exact_post_bind_state())


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("step", 1),
        ("count", 2),
        ("ordinal", 1),
        ("lossless", False),
        ("tool_name", "capture.phase9.other"),
        ("arguments_json", '{"value":"changed"}'),
        ("reasoning_count", 1),
        ("tool_count", 1),
        ("stable_tool_present", True),
        ("effect_started", True),
    ],
)
def test_exact_post_bind_state_rejects_every_drift(
    field: str,
    replacement: object,
) -> None:
    state = _exact_post_bind_state()
    if field in {"step", "count", "ordinal", "lossless", "tool_name", "arguments_json"}:
        payload = state["manifest_payloads"][0]  # type: ignore[index]
        if field == "step":
            payload["step"] = replacement
        elif field == "count":
            payload["manifest"]["count"] = replacement
        else:
            payload["manifest"]["calls"][0][field] = replacement
    else:
        state[field] = replacement

    with pytest.raises(RuntimeError, match="post-bind"):
        capture.assert_exact_post_bind_state(**state)
