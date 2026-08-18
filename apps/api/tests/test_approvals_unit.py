"""Approval decisions are single-winner, durable transitions."""

import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.approvals import service
from jhin_api.approvals.schemas import ApprovalOut
from jhin_api.deps import WorkspaceContext
from jhin_api.tasks.schemas import RunEventOut, ToolCallOut
from jhin_db.models import Agent, Approval, AuditEvent, Task
from jhin_domain import ApprovalStatus, new_uuid7


class _WorkflowHandle:
    def __init__(self, *, failures: int = 0) -> None:
        self.signals: list[tuple[str, list[str]]] = []
        self.failures = failures

    async def signal(self, name: str, *, args: list[str]) -> None:
        if self.failures:
            self.failures -= 1
            raise OSError("simulated Temporal signal failure")
        self.signals.append((name, args))


class _TemporalClient:
    def __init__(self, *, failures: int = 0) -> None:
        self.handle = _WorkflowHandle(failures=failures)

    def get_workflow_handle(self, _workflow_id: str) -> _WorkflowHandle:
        return self.handle


def test_public_approval_and_tool_call_payloads_omit_function_source_content() -> None:
    marker = "phase9-source-secret-must-not-leave-api"
    source_input = {
        "connection_id": str(new_uuid7()),
        "project_ref": "abcdefghijklmnopqrst",
        "function_slug": "safe-function",
        "entrypoint_path": "index.ts",
        "verify_jwt": True,
        "files": [{"path": "index.ts", "content": marker}],
    }
    now = datetime.now(UTC)
    approval = ApprovalOut.model_validate(
        {
            "id": new_uuid7(),
            "task_id": new_uuid7(),
            "run_id": new_uuid7(),
            "requested_by_agent_id": new_uuid7(),
            "action_type": "supabase.function.deploy",
            "action_payload_sanitized": {"risk": "destructive", "input": source_input},
            "reason": "test",
            "status": "pending",
            "requested_at": now,
            "decided_at": None,
            "decided_by_user_id": None,
        }
    ).model_dump(mode="json")
    tool_call = ToolCallOut.model_validate(
        {
            "id": new_uuid7(),
            "run_id": new_uuid7(),
            "agent_id": new_uuid7(),
            "tool_name": "supabase.function.deploy",
            "sanitized_input_json": source_input,
            "sanitized_output_json": {"slug": "safe-function"},
            "status": "completed",
            "approval_id": new_uuid7(),
            "started_at": now,
            "completed_at": now,
            "duration_ms": 1,
            "error_code": None,
            "created_at": now,
        }
    ).model_dump(mode="json")

    assert marker not in str(approval)
    assert marker not in str(tool_call)
    assert approval["action_payload_sanitized"]["input"]["files"] == [{"path": "index.ts"}]
    assert tool_call["sanitized_input_json"]["files"] == [{"path": "index.ts"}]


def test_public_approval_and_tool_call_payloads_omit_database_sql_and_params() -> None:
    marker = "phase9-sql-secret-must-not-leave-api"
    database_input = {
        "connection_id": str(new_uuid7()),
        "project_ref": "abcdefghijklmnopqrst",
        "schema": "public",
        "sql": f"SELECT '{marker}'",
        "params": [marker],
    }
    now = datetime.now(UTC)
    approval = ApprovalOut.model_validate(
        {
            "id": new_uuid7(),
            "task_id": new_uuid7(),
            "run_id": new_uuid7(),
            "requested_by_agent_id": new_uuid7(),
            "action_type": "supabase.database.read",
            "action_payload_sanitized": {"risk": "read", "input": database_input},
            "reason": "test",
            "status": "pending",
            "requested_at": now,
            "decided_at": None,
            "decided_by_user_id": None,
        }
    ).model_dump(mode="json")
    tool_call = ToolCallOut.model_validate(
        {
            "id": new_uuid7(),
            "run_id": new_uuid7(),
            "agent_id": new_uuid7(),
            "tool_name": "supabase.database.read",
            "sanitized_input_json": database_input,
            "sanitized_output_json": {"columns": ["value"], "rows": [["ok"]]},
            "status": "completed",
            "approval_id": None,
            "started_at": now,
            "completed_at": now,
            "duration_ms": 1,
            "error_code": None,
            "created_at": now,
        }
    ).model_dump(mode="json")

    assert marker not in str(approval)
    assert marker not in str(tool_call)
    assert set(approval["action_payload_sanitized"]["input"]) == {
        "connection_id",
        "project_ref",
        "schema",
    }
    assert set(tool_call["sanitized_input_json"]) == {
        "connection_id",
        "project_ref",
        "schema",
    }


@pytest.mark.parametrize(
    ("tool_name", "arguments", "marker"),
    [
        (
            "supabase.function.deploy",
            {"files": [{"path": "index.ts", "content": "timeline-source-marker"}]},
            "timeline-source-marker",
        ),
        (
            "supabase.database.read",
            {"sql": "SELECT 'timeline-sql-marker'", "params": ["timeline-sql-marker"]},
            "timeline-sql-marker",
        ),
    ],
)
def test_public_tool_manifest_timeline_omits_lossless_arguments(
    tool_name: str,
    arguments: dict[str, object],
    marker: str,
) -> None:
    now = datetime.now(UTC)
    event = RunEventOut.model_validate(
        {
            "id": new_uuid7(),
            "run_id": new_uuid7(),
            "seq": 2,
            "event_type": "agent.step.tool_manifest",
            "payload_json": {
                "step": 4,
                "manifest": {
                    "count": 1,
                    "calls": [
                        {
                            "ordinal": 0,
                            "lossless": True,
                            "tool_name": tool_name,
                            "arguments_json": json.dumps(arguments),
                        }
                    ],
                },
            },
            "created_at": now,
        }
    ).model_dump(mode="json")

    assert marker not in str(event)
    assert event["payload_json"] == {
        "step": 4,
        "manifest": {
            "count": 1,
            "calls": [{"ordinal": 0, "lossless": True, "tool_name": tool_name}],
        },
    }


def test_public_timeline_preserves_unrelated_event_payloads() -> None:
    now = datetime.now(UTC)
    event = RunEventOut.model_validate(
        {
            "id": new_uuid7(),
            "run_id": new_uuid7(),
            "seq": 3,
            "event_type": "node.reason",
            "payload_json": {"text": "public reasoning summary", "done": False},
            "created_at": now,
        }
    ).model_dump(mode="json")

    assert event["payload_json"] == {"text": "public reasoning summary", "done": False}


async def _pending_approval(session: AsyncSession, ctx: WorkspaceContext) -> Approval:
    agent = Agent(
        workspace_id=ctx.workspace_id,
        name="Approval agent",
        slug=f"approval-agent-{new_uuid7().hex[:8]}",
    )
    session.add(agent)
    await session.flush()
    task = Task(
        workspace_id=ctx.workspace_id,
        title="Approval task",
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
        temporal_workflow_id=f"approval-workflow-{new_uuid7()}",
    )
    session.add(task)
    await session.flush()
    approval = Approval(
        workspace_id=ctx.workspace_id,
        task_id=task.id,
        requested_by_agent_id=agent.id,
        action_type="test.approval",
        action_payload_sanitized={},
        reason="test",
        status=ApprovalStatus.PENDING.value,
        requested_at=datetime.now(UTC),
    )
    session.add(approval)
    await session.commit()
    return approval


async def test_only_pending_transition_signals_and_records_one_decision(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
) -> None:
    approval = await _pending_approval(session, admin_ctx)
    temporal = _TemporalClient()

    decided = await service.decide(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        approval.id,
        decision=ApprovalStatus.APPROVED.value,
        request_id=new_uuid7(),
        ip_hash="unit-test",
    )
    replay = await service.decide(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        approval.id,
        decision=ApprovalStatus.APPROVED.value,
        request_id=new_uuid7(),
        ip_hash="unit-test",
    )
    with pytest.raises(HTTPException) as conflict:
        await service.decide(
            session,
            admin_ctx,
            temporal,  # type: ignore[arg-type]
            approval.id,
            decision=ApprovalStatus.REJECTED.value,
            request_id=new_uuid7(),
            ip_hash="unit-test",
        )

    assert decided.status == replay.status == ApprovalStatus.APPROVED.value
    assert conflict.value.status_code == 409
    assert temporal.handle.signals == [
        ("approval_decision", [str(approval.id), ApprovalStatus.APPROVED.value]),
        ("approval_decision", [str(approval.id), ApprovalStatus.APPROVED.value]),
    ]
    assert (
        await session.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "approval.approved")
        )
        == 1
    )


async def test_same_decision_retry_repairs_commit_to_signal_failure(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
) -> None:
    approval = await _pending_approval(session, admin_ctx)
    temporal = _TemporalClient(failures=1)

    with pytest.raises(HTTPException) as first:
        await service.decide(
            session,
            admin_ctx,
            temporal,  # type: ignore[arg-type]
            approval.id,
            decision=ApprovalStatus.APPROVED.value,
            request_id=new_uuid7(),
            ip_hash="unit-test",
        )
    replay = await service.decide(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        approval.id,
        decision=ApprovalStatus.APPROVED.value,
        request_id=new_uuid7(),
        ip_hash="unit-test",
    )

    assert first.value.status_code == 409
    assert replay.status == ApprovalStatus.APPROVED.value
    assert temporal.handle.signals == [
        ("approval_decision", [str(approval.id), ApprovalStatus.APPROVED.value])
    ]
    assert (
        await session.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "approval.approved")
        )
        == 1
    )
