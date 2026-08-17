"""Gateway pipeline: schema rejection, deny-by-default, execution,
approval staging, and approval resolution — all against in-memory SQLite."""

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import (
    AgentCapabilityGrant,
    Approval,
    AuditEvent,
    Message,
    ToolCall,
)
from jhin_domain import ApprovalStatus, ToolCallStatus
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.gateway import ToolGateway


async def _grant(
    session: AsyncSession, ctx: ToolExecutionContext, capability: str, effect: str = "allow"
) -> None:
    session.add(
        AgentCapabilityGrant(
            workspace_id=ctx.workspace_id,
            agent_id=ctx.agent_id,
            capability=capability,
            scope_json={},
            effect=effect,
        )
    )
    await session.flush()


async def _audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditEvent.action))
    return list(rows)


async def test_unknown_tool_is_recorded_denial(gateway: ToolGateway, session: AsyncSession) -> None:
    outcome = await gateway.request("system.nope", "{}")
    assert outcome.status == "denied"
    assert outcome.decision_code == "tool_not_found"
    row = await session.scalar(select(ToolCall))
    assert row is not None
    assert row.status == ToolCallStatus.DENIED.value
    actions = await _audit_actions(session)
    assert "tool.call.requested" in actions
    assert "tool.call.denied" in actions


async def test_schema_violation_is_denied_before_policy(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.echo")
    for bad in ('{"text": 42, "extra": true}', "not json", '["list"]'):
        outcome = await gateway.request("system.echo", bad)
        assert outcome.status == "denied"
        assert outcome.decision_code == "invalid_input"


async def test_deny_by_default_without_grant(gateway: ToolGateway, session: AsyncSession) -> None:
    outcome = await gateway.request("system.echo", '{"text": "hi"}')
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_grant"
    row = await session.scalar(select(ToolCall))
    assert row is not None
    assert row.status == ToolCallStatus.DENIED.value
    assert row.error_code == "no_grant"


async def test_granted_read_tool_executes_and_persists(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.echo")
    outcome = await gateway.request("system.echo", '{"text": "hello"}')
    assert outcome.status == "executed"
    assert outcome.sanitized_output == {"text": "hello"}
    assert json.loads(outcome.observation_json()) == {"text": "hello"}
    row = await session.scalar(select(ToolCall))
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value
    assert row.duration_ms is not None
    assert "tool.call.executed" in await _audit_actions(session)


async def test_explicit_deny_beats_allow(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.*")
    await _grant(session, context, "system.echo", effect="deny")
    outcome = await gateway.request("system.echo", '{"text": "hi"}')
    assert outcome.status == "denied"
    assert outcome.decision_code == "explicit_deny"
    # The wildcard allow still covers the other read tool.
    outcome_time = await gateway.request("system.time", "{}")
    assert outcome_time.status == "executed"


async def test_note_append_writes_visible_note(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.note.append")
    outcome = await gateway.request("system.note.append", '{"text": "remember this"}')
    assert outcome.status == "executed"
    note = await session.scalar(select(Message))
    assert note is not None
    assert note.content_json == {"text": "remember this"}
    assert note.task_id == context.task_id


async def test_destructive_tool_stages_pending_approval(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.demo.destructive")
    outcome = await gateway.request("system.demo.destructive", '{"label": "wipe"}')
    assert outcome.status == "needs_approval"
    assert outcome.approval_id is not None
    approval = await session.scalar(select(Approval))
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING.value
    assert approval.action_payload_sanitized["risk"] == "destructive"
    row = await session.scalar(select(ToolCall))
    assert row is not None
    assert row.status == ToolCallStatus.PENDING_APPROVAL.value
    assert row.approval_id == approval.id
    assert "approval.requested" in await _audit_actions(session)


async def test_resolve_approved_executes_the_parked_call(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.demo.destructive")
    parked = await gateway.request("system.demo.destructive", '{"label": "go"}')
    assert parked.approval_id is not None

    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    approval.status = ApprovalStatus.APPROVED.value
    approval.decided_at = datetime.now(UTC)
    await session.flush()

    outcome = await gateway.resolve_approved(parked.approval_id)
    assert outcome.status == "executed"
    assert outcome.sanitized_output is not None
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value
    actions = await _audit_actions(session)
    assert "tool.call.approved" in actions
    assert "demo.destructive.marker" in actions  # the inert destructive effect


async def test_resolve_rejected_records_denial(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.demo.destructive")
    parked = await gateway.request("system.demo.destructive", '{"label": "no"}')
    assert parked.approval_id is not None

    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    approval.status = ApprovalStatus.REJECTED.value
    await session.flush()

    outcome = await gateway.resolve_rejected(parked.approval_id)
    assert outcome.status == "rejected"
    assert outcome.error_code == "approval_rejected"
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    assert row.status == ToolCallStatus.REJECTED.value
    observation = json.loads(outcome.observation_json())
    assert observation["error"] == "approval_rejected"


async def test_output_is_redacted_and_size_capped(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    from jhin_secrets.redaction import get_redactor
    from jhin_tools.builtin import build_builtin_catalog

    get_redactor().register("sk-unit-test-secret")
    try:
        gateway = ToolGateway(context, build_builtin_catalog())
        await _grant(session, context, "system.echo")
        outcome = await gateway.request(
            "system.echo", json.dumps({"text": "key=sk-unit-test-secret " + "x" * 9_500})
        )
        assert outcome.status == "executed"
        assert outcome.sanitized_output is not None
        text = outcome.sanitized_output["text"]
        assert "sk-unit-test-secret" not in text
        assert "[REDACTED]" in text
        assert len(text) <= 8_192
    finally:
        get_redactor().clear()
