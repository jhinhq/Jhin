"""Approval decisions are single-winner, durable transitions."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.approvals import service
from jhin_api.deps import WorkspaceContext
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
