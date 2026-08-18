"""The tool gateway: every agent tool call goes through here (plan 12).

Pipeline: registry lookup → schema validation → capability grant lookup →
scope validation → policy evaluation → approval check → (budget/rate stub)
→ execute → sanitize → persist ``tool_call`` row → result.

Invariants enforced here:

- model output is never authorization (plan 52): only the structured tool
  name and arguments enter, and both are validated against registered
  schemas;
- deny-by-default: unknown tools, schema violations, missing grants, and
  scope mismatches are all recorded denials;
- everything persisted is sanitized first (plan 6.15, 21.8-9);
- every attempt is audited (plan 23): tool.call.requested plus the outcome.

The gateway stages rows in the caller's session; the caller (a Temporal
activity) owns the commit so a crash cannot persist half a decision.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    AuditEvent,
    Connection,
    Secret,
    ToolCall,
)
from jhin_domain import ActorType, ApprovalStatus, ToolCallStatus, new_uuid7
from jhin_policy import (
    DecisionType,
    Grant,
    GrantEffect,
    PolicyRule,
    ToolDefinition,
    evaluate,
)
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext, ToolExecutor
from jhin_tools.invocation import TOOL_INVOCATION_FORMAT_VERSION
from jhin_tools.sanitize import (
    MAX_DOCUMENT_BYTES,
    StrictJSONError,
    sanitize_payload,
    strict_json_loads,
)

GatewayStatus = Literal[
    "executed",
    "failed",
    "denied",
    "needs_approval",
    "rejected",
    "execution_unknown",
]

_APPROVAL_FORMAT_VERSION = 2
_TERMINAL_TOOL_STATUSES = frozenset(
    {
        ToolCallStatus.COMPLETED.value,
        ToolCallStatus.FAILED.value,
        ToolCallStatus.DENIED.value,
        ToolCallStatus.REJECTED.value,
    }
)
_PROCESS_INVOCATION_LOCKS: dict[UUID, asyncio.Lock] = {}
_PROCESS_INVOCATION_LOCKS_GUARD = asyncio.Lock()


@dataclass(frozen=True)
class _ValidatedApprovalBinding:
    definition: ToolDefinition
    validated_input: BaseModel
    dumped: dict[str, Any]
    connection_id: UUID | None
    connection_digest: str | None


def _connection_uuid(dumped: dict[str, Any]) -> UUID | None:
    """The connection a connector tool call targets, when the validated input
    carries one — persisted on the tool_call row for per-connection usage
    views (plan 17.9)."""
    raw = dumped.get("connection_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


class GatewayOutcome(BaseModel):
    """What happened to one tool call, ready for run events and observations."""

    model_config = ConfigDict(frozen=True)

    status: GatewayStatus
    tool_call_id: UUID
    tool_name: str
    risk: str | None
    decision_code: str
    decision_reason: str
    sanitized_input: dict[str, Any]
    sanitized_output: dict[str, Any] | None = None
    approval_id: UUID | None = None
    provider_call_id: str | None = None
    error_code: str | None = None
    duration_ms: int | None = None
    # True when a retry observed an already-persisted terminal outcome. The
    # activity layer uses its own durable bundle key to avoid duplicate
    # transcript messages and run events while still repairing a crash gap.
    replayed: bool = False

    def observation_json(self) -> str:
        """The sanitized observation fed back to the model as the tool
        result. Never raw output — always the persisted, sanitized form."""
        if self.status == "executed" and self.sanitized_output is not None:
            return json.dumps(self.sanitized_output, ensure_ascii=False, default=str)
        return json.dumps(
            {
                "error": self.error_code or self.status,
                "detail": self.decision_reason,
            },
            ensure_ascii=False,
        )


class ToolGateway:
    def __init__(
        self,
        context: ToolExecutionContext,
        catalog: ToolCatalog,
        *,
        max_output_bytes: int = MAX_DOCUMENT_BYTES,
    ) -> None:
        self._ctx = context
        self._catalog = catalog
        self._max_output_bytes = max_output_bytes

    # --- helpers ---

    def _fresh_sessions(self) -> async_sessionmaker[AsyncSession] | None:
        return self._ctx.session_factory

    @asynccontextmanager
    async def _invocation_lifecycle_lock(self, invocation_id: UUID) -> AsyncIterator[ToolGateway]:
        """Serialize claim through terminal persistence for one invocation.

        PostgreSQL session-level advisory locks survive the transaction
        commits which make the claim and terminal outcome durable.  The same
        dedicated connection also backs a cloned gateway session, avoiding a
        pool-exhausting lock/claim/executor connection fan-out.
        """
        bind = self._ctx.session.bind
        if isinstance(bind, AsyncEngine) and bind.dialect.name == "postgresql":
            advisory_key = int.from_bytes(invocation_id.bytes[:8], "big", signed=True)
            async with bind.connect() as lock_connection:
                await lock_connection.scalar(select(func.pg_advisory_lock(advisory_key)))
                await lock_connection.commit()
                try:
                    async with AsyncSession(
                        bind=lock_connection,
                        expire_on_commit=False,
                    ) as lifecycle_session:
                        gateway = copy.copy(self)
                        gateway._ctx = replace(self._ctx, session=lifecycle_session)
                        yield gateway
                finally:
                    await lock_connection.scalar(select(func.pg_advisory_unlock(advisory_key)))
                    await lock_connection.commit()
            return

        # SQLite and unusual test bindings have no advisory locks. A keyed
        # in-process lock preserves the same lifecycle contract for portable
        # tests; PostgreSQL integration remains the multi-process authority.
        async with _PROCESS_INVOCATION_LOCKS_GUARD:
            lock = _PROCESS_INVOCATION_LOCKS.setdefault(invocation_id, asyncio.Lock())
        async with lock:
            yield self

    def _audit_on(
        self,
        session: AsyncSession,
        action: str,
        target_id: UUID,
        metadata: dict[str, Any],
    ) -> None:
        session.add(
            AuditEvent(
                workspace_id=self._ctx.workspace_id,
                actor_type=ActorType.AGENT.value,
                actor_id=self._ctx.agent_id,
                action=action,
                target_type="tool_call",
                target_id=target_id,
                metadata_json={
                    "run_id": str(self._ctx.run_id),
                    "task_id": str(self._ctx.task_id),
                    **metadata,
                },
            )
        )

    def _audit(self, action: str, target_id: UUID, metadata: dict[str, Any]) -> None:
        self._audit_on(self._ctx.session, action, target_id, metadata)

    async def _load_grants(self) -> list[Grant]:
        rows = await self._ctx.session.scalars(
            select(AgentCapabilityGrant).where(
                AgentCapabilityGrant.agent_id == self._ctx.agent_id,
                AgentCapabilityGrant.workspace_id == self._ctx.workspace_id,
            )
        )
        grants: list[Grant] = []
        for row in rows:
            try:
                grants.append(
                    Grant(
                        capability=row.capability,
                        scope=row.scope_json,
                        effect=GrantEffect(row.effect),
                    )
                )
            except (ValueError, ValidationError):
                continue  # malformed rows never widen access
        return grants

    async def _load_rules(self) -> list[PolicyRule]:
        agent = await self._ctx.session.scalar(
            select(Agent).where(
                Agent.id == self._ctx.agent_id, Agent.workspace_id == self._ctx.workspace_id
            )
        )
        if agent is None:
            return []
        rules: list[PolicyRule] = []
        for raw in agent.approval_policy_json:
            try:
                rules.append(PolicyRule.model_validate(raw))
            except ValidationError:
                continue  # malformed rules are skipped; defaults still apply
        return rules

    def _sanitize(self, payload: dict[str, Any]) -> dict[str, Any]:
        return sanitize_payload(payload, max_document_bytes=self._max_output_bytes)

    def _denied(
        self,
        tool_name: str,
        *,
        code: str,
        reason: str,
        sanitized_input: dict[str, Any],
        risk: str | None,
        status: ToolCallStatus = ToolCallStatus.DENIED,
        connection_id: UUID | None = None,
        tool_call_id: UUID | None = None,
    ) -> tuple[ToolCall, GatewayOutcome]:
        now = datetime.now(UTC)
        # Ids are assigned eagerly (not at flush) because they go into the
        # outcome and audit rows before the caller commits.
        row = ToolCall(
            id=tool_call_id or new_uuid7(),
            workspace_id=self._ctx.workspace_id,
            run_id=self._ctx.run_id,
            agent_id=self._ctx.agent_id,
            tool_name=tool_name,
            connection_id=connection_id,
            sanitized_input_json=sanitized_input,
            sanitized_output_json={},
            status=status.value,
            started_at=now,
            completed_at=now,
            error_code=code,
        )
        self._ctx.session.add(row)
        outcome = GatewayOutcome(
            status="denied",
            tool_call_id=row.id,
            tool_name=tool_name,
            risk=risk,
            decision_code=code,
            decision_reason=reason,
            sanitized_input=sanitized_input,
            error_code=code,
        )
        return row, outcome

    def _finish_parked_call(
        self,
        row: ToolCall,
        approval_id: UUID,
        *,
        status: Literal["denied", "failed"],
        code: str,
        reason: str,
        risk: str | None,
    ) -> GatewayOutcome:
        """Finalize every pre-executor parked-call failure with one audit."""
        row.status = (
            ToolCallStatus.DENIED.value if status == "denied" else ToolCallStatus.FAILED.value
        )
        row.completed_at = datetime.now(UTC)
        row.error_code = code
        self._audit(
            f"tool.call.{status}",
            row.id,
            {"code": code, "reason": reason, "approval_id": str(approval_id)},
        )
        return GatewayOutcome(
            status=status,
            tool_call_id=row.id,
            tool_name=row.tool_name,
            risk=risk,
            decision_code=code,
            decision_reason=reason,
            sanitized_input=row.sanitized_input_json,
            approval_id=approval_id,
            error_code=code,
        )

    def _replayed_outcome(
        self,
        row: ToolCall,
        *,
        approval: Approval | None = None,
        risk: str | None = None,
    ) -> GatewayOutcome:
        status_map: dict[str, GatewayStatus] = {
            ToolCallStatus.COMPLETED.value: "executed",
            ToolCallStatus.FAILED.value: "failed",
            ToolCallStatus.DENIED.value: "denied",
            ToolCallStatus.REJECTED.value: "rejected",
        }
        status = status_map[row.status]
        payload = approval.action_payload_sanitized if approval is not None else {}
        error_code = row.error_code
        decision_code = "granted" if status == "executed" else (error_code or status)
        return GatewayOutcome(
            status=status,
            tool_call_id=row.id,
            tool_name=row.tool_name,
            risk=(payload.get("risk") if isinstance(payload.get("risk"), str) else risk),
            decision_code=decision_code,
            decision_reason="replayed the persisted terminal tool-call outcome",
            sanitized_input=row.sanitized_input_json,
            sanitized_output=(
                row.sanitized_output_json if status in ("executed", "failed") else None
            ),
            approval_id=approval.id if approval is not None else None,
            provider_call_id=(
                payload.get("provider_call_id")
                if isinstance(payload.get("provider_call_id"), str)
                else None
            ),
            error_code=error_code,
            duration_ms=row.duration_ms,
            replayed=True,
        )

    def _execution_unknown_outcome(
        self,
        row: ToolCall,
        *,
        approval: Approval | None = None,
        risk: str | None = None,
        replayed: bool = True,
    ) -> GatewayOutcome:
        payload = approval.action_payload_sanitized if approval is not None else {}
        return GatewayOutcome(
            status="execution_unknown",
            tool_call_id=row.id,
            tool_name=row.tool_name,
            risk=(payload.get("risk") if isinstance(payload.get("risk"), str) else risk),
            decision_code="execution_outcome_unknown",
            decision_reason=(
                "the tool may have produced an external effect, but its terminal "
                "outcome could not be proven; manual reconciliation is required"
            ),
            sanitized_input=row.sanitized_input_json,
            approval_id=approval.id if approval is not None else row.approval_id,
            provider_call_id=(
                payload.get("provider_call_id")
                if isinstance(payload.get("provider_call_id"), str)
                else None
            ),
            error_code="execution_outcome_unknown",
            replayed=replayed,
        )

    async def _persist_execution_unknown(
        self,
        tool_call_id: UUID,
        *,
        risk: str | None,
    ) -> GatewayOutcome:
        unknown_session = self._ctx.session
        row = await unknown_session.scalar(
            select(ToolCall)
            .where(
                ToolCall.id == tool_call_id,
                ToolCall.workspace_id == self._ctx.workspace_id,
            )
            .with_for_update()
        )
        if row is None:
            raise GatewayStateError(f"tool call {tool_call_id} disappeared after executor dispatch")
        approval = (
            await unknown_session.get(Approval, row.approval_id)
            if row.approval_id is not None
            else None
        )
        if row.status in _TERMINAL_TOOL_STATUSES:
            return self._replayed_outcome(row, approval=approval, risk=risk)
        if row.status == ToolCallStatus.EXECUTING.value:
            row.status = ToolCallStatus.EXECUTION_UNKNOWN.value
            row.completed_at = datetime.now(UTC)
            row.error_code = "execution_outcome_unknown"
            metadata = {"code": "execution_outcome_unknown"}
            if approval is not None:
                metadata["approval_id"] = str(approval.id)
            self._audit_on(
                unknown_session,
                "tool.call.execution_unknown",
                row.id,
                metadata,
            )
            await unknown_session.commit()
        elif row.status != ToolCallStatus.EXECUTION_UNKNOWN.value:
            raise GatewayStateError(
                f"tool call {tool_call_id} has unexpected status '{row.status}'"
            )
        return self._execution_unknown_outcome(
            row,
            approval=approval,
            risk=risk,
            replayed=False,
        )

    async def _require_durable_execution_context(self) -> AgentRun:
        run = await self._ctx.session.scalar(
            select(AgentRun).where(
                AgentRun.id == self._ctx.run_id,
                AgentRun.workspace_id == self._ctx.workspace_id,
                AgentRun.agent_id == self._ctx.agent_id,
                AgentRun.task_id == self._ctx.task_id,
            )
        )
        if run is None:
            raise GatewayStateError(
                "runtime tool invocation does not match its original invocation or "
                "a durable workspace, agent, run, and task"
            )
        return run

    async def _existing_invocation_outcome(
        self,
        invocation_id: UUID,
        definition: ToolDefinition,
        dumped: dict[str, Any],
    ) -> GatewayOutcome | None:
        """Replay or fail closed for a deterministic runtime invocation.

        The deterministic primary key is the cross-retry claim key. Reuse is
        accepted only for the exact original tool, canonical input, and
        durable workspace/agent/run/task context.
        """
        row = await self._ctx.session.scalar(
            select(ToolCall).where(ToolCall.id == invocation_id).with_for_update()
        )
        if row is None:
            return None
        run = await self._ctx.session.get(AgentRun, row.run_id)
        if (
            row.workspace_id != self._ctx.workspace_id
            or row.agent_id != self._ctx.agent_id
            or row.run_id != self._ctx.run_id
            or run is None
            or run.workspace_id != self._ctx.workspace_id
            or run.agent_id != self._ctx.agent_id
            or run.task_id != self._ctx.task_id
            or row.tool_name != definition.name
            or row.sanitized_input_json != dumped
            or row.connection_id != _connection_uuid(dumped)
        ):
            return await self._invocation_mismatch(
                invocation_id,
                definition,
                dumped,
            )

        approval: Approval | None = None
        if row.approval_id is not None:
            approval = await self._ctx.session.scalar(
                select(Approval).where(
                    Approval.id == row.approval_id,
                    Approval.workspace_id == self._ctx.workspace_id,
                )
            )
            if approval is None:
                return await self._invocation_mismatch(
                    invocation_id,
                    definition,
                    dumped,
                )
            payload = approval.action_payload_sanitized
            expected = {
                "approval_format_version": _APPROVAL_FORMAT_VERSION,
                "workspace_id": str(self._ctx.workspace_id),
                "agent_id": str(self._ctx.agent_id),
                "run_id": str(self._ctx.run_id),
                "task_id": str(self._ctx.task_id),
                "tool_name": definition.name,
                "capability": definition.required_capability,
                "risk": definition.risk.value,
                "input": dumped,
                "tool_call_id": str(row.id),
                "invocation_format_version": TOOL_INVOCATION_FORMAT_VERSION,
                "invocation_id": str(row.id),
            }
            if (
                approval.task_id != self._ctx.task_id
                or approval.run_id != self._ctx.run_id
                or approval.requested_by_agent_id != self._ctx.agent_id
                or approval.action_type != definition.name
                or any(payload.get(key) != value for key, value in expected.items())
            ):
                return await self._invocation_mismatch(
                    invocation_id,
                    definition,
                    dumped,
                )

        if row.status in _TERMINAL_TOOL_STATUSES:
            return self._replayed_outcome(
                row,
                approval=approval,
                risk=definition.risk.value,
            )
        if row.status in (ToolCallStatus.EXECUTING.value,):
            row.status = ToolCallStatus.EXECUTION_UNKNOWN.value
            row.completed_at = datetime.now(UTC)
            row.error_code = "execution_outcome_unknown"
            self._audit(
                "tool.call.execution_unknown",
                row.id,
                {
                    "code": "execution_outcome_unknown",
                    **({"approval_id": str(approval.id)} if approval is not None else {}),
                },
            )
            await self._ctx.session.commit()
            return self._execution_unknown_outcome(
                row,
                approval=approval,
                risk=definition.risk.value,
                replayed=False,
            )
        if row.status == ToolCallStatus.EXECUTION_UNKNOWN.value:
            return self._execution_unknown_outcome(
                row,
                approval=approval,
                risk=definition.risk.value,
            )
        if row.status == ToolCallStatus.PENDING_APPROVAL.value and approval is not None:
            return GatewayOutcome(
                status="needs_approval",
                tool_call_id=row.id,
                tool_name=row.tool_name,
                risk=definition.risk.value,
                decision_code="approval_required",
                decision_reason=approval.reason,
                sanitized_input=row.sanitized_input_json,
                approval_id=approval.id,
                provider_call_id=(
                    payload.get("provider_call_id")
                    if isinstance(payload.get("provider_call_id"), str)
                    else None
                ),
                replayed=True,
            )
        raise GatewayStateError(f"tool call {invocation_id} has unexpected status '{row.status}'")

    async def _invocation_mismatch(
        self,
        invocation_id: UUID,
        definition: ToolDefinition,
        dumped: dict[str, Any],
    ) -> GatewayOutcome:
        return await self._invocation_mismatch_outcome(
            invocation_id,
            tool_name=definition.name,
            risk=definition.risk.value,
            sanitized_input=self._sanitize(dumped),
        )

    async def _invocation_mismatch_outcome(
        self,
        invocation_id: UUID,
        *,
        tool_name: str,
        risk: str | None,
        sanitized_input: dict[str, Any],
    ) -> GatewayOutcome:
        reason = (
            "runtime invocation key is already bound to a different tool, input, "
            "or execution context"
        )
        metadata = {
            "code": "invocation_mismatch",
            "tool_name": tool_name,
            "reason": reason,
        }
        self._audit("tool.call.denied", invocation_id, metadata)
        await self._ctx.session.commit()
        return GatewayOutcome(
            status="denied",
            tool_call_id=invocation_id,
            tool_name=tool_name,
            risk=risk,
            decision_code="invocation_mismatch",
            decision_reason=reason,
            sanitized_input=sanitized_input,
            error_code="invocation_mismatch",
        )

    async def _existing_unvalidated_invocation(
        self,
        invocation_id: UUID,
        *,
        tool_name: str,
        sanitized_input: dict[str, Any],
        canonical_input: dict[str, Any] | None = None,
    ) -> GatewayOutcome | None:
        row = await self._ctx.session.scalar(
            select(ToolCall).where(ToolCall.id == invocation_id).with_for_update()
        )
        if row is None:
            return None
        run = await self._ctx.session.get(AgentRun, row.run_id)
        if (
            row.workspace_id != self._ctx.workspace_id
            or row.agent_id != self._ctx.agent_id
            or row.run_id != self._ctx.run_id
            or run is None
            or run.workspace_id != self._ctx.workspace_id
            or run.agent_id != self._ctx.agent_id
            or run.task_id != self._ctx.task_id
            or row.tool_name != tool_name
        ):
            return await self._invocation_mismatch_outcome(
                invocation_id,
                tool_name=tool_name,
                risk=None,
                sanitized_input=sanitized_input,
            )
        expected_input = (
            sanitized_input if row.status == ToolCallStatus.DENIED.value else canonical_input
        )
        if expected_input is None or row.sanitized_input_json != expected_input:
            return await self._invocation_mismatch_outcome(
                invocation_id,
                tool_name=tool_name,
                risk=None,
                sanitized_input=sanitized_input,
            )
        approval = (
            await self._ctx.session.get(Approval, row.approval_id)
            if row.approval_id is not None
            else None
        )
        if row.status in _TERMINAL_TOOL_STATUSES:
            return self._replayed_outcome(row, approval=approval)
        if row.status == ToolCallStatus.EXECUTION_UNKNOWN.value:
            return self._execution_unknown_outcome(row, approval=approval)
        if row.status == ToolCallStatus.EXECUTING.value:
            row.status = ToolCallStatus.EXECUTION_UNKNOWN.value
            row.completed_at = datetime.now(UTC)
            row.error_code = "execution_outcome_unknown"
            self._audit(
                "tool.call.execution_unknown",
                row.id,
                {"code": "execution_outcome_unknown"},
            )
            await self._ctx.session.commit()
            return self._execution_unknown_outcome(
                row,
                approval=approval,
                replayed=False,
            )
        if row.status == ToolCallStatus.PENDING_APPROVAL.value:
            # A vanished or schema-invalid tool definition cannot prove that
            # the currently supplied input is the exact operation a human was
            # shown. Only already-terminal outcomes are repairable through
            # this definition-independent path.
            return await self._invocation_mismatch_outcome(
                invocation_id,
                tool_name=tool_name,
                risk=None,
                sanitized_input=sanitized_input,
            )
        raise GatewayStateError(f"tool call {invocation_id} has unexpected status '{row.status}'")

    async def _persist_denial(
        self,
        *,
        invocation_id: UUID | None,
        tool_name: str,
        code: str,
        reason: str,
        sanitized_input: dict[str, Any],
        risk: str | None,
        connection_id: UUID | None = None,
    ) -> GatewayOutcome:
        if invocation_id is None:
            row, outcome = self._denied(
                tool_name,
                code=code,
                reason=reason,
                sanitized_input=sanitized_input,
                risk=risk,
                connection_id=connection_id,
            )
            self._audit("tool.call.requested", row.id, {"tool_name": tool_name})
            self._audit(
                "tool.call.denied",
                row.id,
                {"code": code, "reason": reason, "risk": risk},
            )
            return outcome

        if self._fresh_sessions() is None:
            raise GatewayStateError(
                "deterministic runtime denial requires an isolated session factory"
            )
        now = datetime.now(UTC)
        try:
            denial_session = self._ctx.session
            row = ToolCall(
                id=invocation_id,
                workspace_id=self._ctx.workspace_id,
                run_id=self._ctx.run_id,
                agent_id=self._ctx.agent_id,
                tool_name=tool_name,
                connection_id=connection_id,
                sanitized_input_json=sanitized_input,
                sanitized_output_json={},
                status=ToolCallStatus.DENIED.value,
                started_at=now,
                completed_at=now,
                error_code=code,
            )
            denial_session.add(row)
            self._audit_on(
                denial_session,
                "tool.call.requested",
                row.id,
                {"tool_name": tool_name},
            )
            self._audit_on(
                denial_session,
                "tool.call.denied",
                row.id,
                {"code": code, "reason": reason, "risk": risk},
            )
            await denial_session.commit()
        except IntegrityError:
            await self._ctx.session.rollback()
            self._ctx.session.expire_all()
            replay = await self._existing_unvalidated_invocation(
                invocation_id,
                tool_name=tool_name,
                sanitized_input=sanitized_input,
                canonical_input=None,
            )
            if replay is None:
                raise GatewayStateError(
                    f"tool call {invocation_id} denial could not be reloaded"
                ) from None
            return replay
        return GatewayOutcome(
            status="denied",
            tool_call_id=invocation_id,
            tool_name=tool_name,
            risk=risk,
            decision_code=code,
            decision_reason=reason,
            sanitized_input=sanitized_input,
            error_code=code,
        )

    async def _claim_direct_call(
        self,
        definition: ToolDefinition,
        *,
        invocation_id: UUID,
        sanitized_input: dict[str, Any],
        dumped: dict[str, Any],
        connection_id: UUID | None,
    ) -> tuple[ToolCall | None, GatewayOutcome | None]:
        """Insert the deterministic claim and commit it before execution."""
        if self._fresh_sessions() is None:
            raise GatewayStateError(
                "deterministic runtime execution requires an isolated session factory"
            )
        try:
            claim_session = self._ctx.session
            row = ToolCall(
                id=invocation_id,
                workspace_id=self._ctx.workspace_id,
                run_id=self._ctx.run_id,
                agent_id=self._ctx.agent_id,
                tool_name=definition.name,
                connection_id=connection_id,
                sanitized_input_json=sanitized_input,
                sanitized_output_json={},
                status=ToolCallStatus.EXECUTING.value,
                started_at=datetime.now(UTC),
            )
            claim_session.add(row)
            self._audit_on(
                claim_session,
                "tool.call.requested",
                row.id,
                {"tool_name": definition.name},
            )
            self._audit_on(
                claim_session,
                "tool.call.claimed",
                row.id,
                {
                    "tool_name": definition.name,
                    "claim_kind": "runtime_invocation",
                },
            )
            await claim_session.commit()
        except IntegrityError:
            await self._ctx.session.rollback()
            self._ctx.session.expire_all()
            replay = await self._existing_invocation_outcome(invocation_id, definition, dumped)
            if replay is None:
                raise GatewayStateError(
                    f"tool call {invocation_id} could not be claimed or reloaded"
                ) from None
            return None, replay

        claimed = await self._ctx.session.get(ToolCall, invocation_id)
        if claimed is None:
            raise GatewayStateError(f"tool call {invocation_id} claim was not persisted")
        return claimed, None

    async def _connection_authorization_digest(
        self, connection_id: UUID, *, lock: bool = False
    ) -> str | None:
        connection_query = select(Connection).where(
            Connection.id == connection_id,
            Connection.workspace_id == self._ctx.workspace_id,
        )
        if lock:
            connection_query = connection_query.with_for_update(read=True).execution_options(
                populate_existing=True
            )
        connection = await self._ctx.session.scalar(connection_query)
        if connection is None or connection.encrypted_secret_id is None:
            return None

        secret_query = select(Secret).where(
            Secret.id == connection.encrypted_secret_id,
            Secret.workspace_id == self._ctx.workspace_id,
        )
        if lock:
            secret_query = secret_query.with_for_update(read=True).execution_options(
                populate_existing=True
            )
        secret = await self._ctx.session.scalar(secret_query)
        if secret is None:
            return None

        canonical = json.dumps(
            {
                "connection_id": str(connection.id),
                "auth_type": connection.auth_type,
                "status": connection.status,
                "config": connection.config_json,
                "credential_fingerprint": secret.secret_fingerprint,
                "credential_key_version": secret.key_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def _claim_parked_call(self, approval: Approval, row: ToolCall) -> GatewayOutcome | None:
        """Atomically claim the call and durably commit before side effects."""
        approval_id = approval.id
        row_id = row.id
        tool_name = row.tool_name
        claim_session = self._ctx.session
        claimed_id = await claim_session.scalar(
            update(ToolCall)
            .where(
                ToolCall.id == row_id,
                ToolCall.workspace_id == self._ctx.workspace_id,
                ToolCall.status == ToolCallStatus.PENDING_APPROVAL.value,
            )
            .values(status=ToolCallStatus.EXECUTING.value)
            .returning(ToolCall.id)
            .execution_options(synchronize_session=False)
        )
        if claimed_id is not None:
            self._audit(
                "tool.call.claimed",
                row_id,
                {"approval_id": str(approval_id), "tool_name": tool_name},
            )
            await self._ctx.session.commit()
        else:
            await self._ctx.session.rollback()
        if claimed_id is not None:
            await self._ctx.session.refresh(row)
            return None

        fresh_approval = await self._ctx.session.scalar(
            select(Approval).where(
                Approval.id == approval_id,
                Approval.workspace_id == self._ctx.workspace_id,
            )
        )
        current = await self._ctx.session.scalar(
            select(ToolCall)
            .where(
                ToolCall.id == row_id,
                ToolCall.workspace_id == self._ctx.workspace_id,
            )
            .with_for_update()
        )
        if current is None or fresh_approval is None:
            raise GatewayStateError(f"tool call {row_id} disappeared while claiming approval")
        if current.status in _TERMINAL_TOOL_STATUSES:
            return self._replayed_outcome(current, approval=fresh_approval)
        if current.status == ToolCallStatus.EXECUTING.value:
            current.status = ToolCallStatus.EXECUTION_UNKNOWN.value
            current.completed_at = datetime.now(UTC)
            current.error_code = "execution_outcome_unknown"
            self._audit(
                "tool.call.execution_unknown",
                current.id,
                {
                    "code": "execution_outcome_unknown",
                    "approval_id": str(fresh_approval.id),
                },
            )
            await self._ctx.session.commit()
            return self._execution_unknown_outcome(
                current,
                approval=fresh_approval,
                replayed=False,
            )
        if current.status == ToolCallStatus.EXECUTION_UNKNOWN.value:
            return self._execution_unknown_outcome(
                current,
                approval=fresh_approval,
            )
        raise GatewayStateError(
            f"tool call {row_id} changed to unexpected status '{current.status}'"
        )

    async def _execute(
        self,
        definition: ToolDefinition,
        row: ToolCall,
        validated_input: BaseModel,
    ) -> GatewayOutcome:
        """Run the executor and finalize the (already staged) tool_call row."""
        executor_entry = self._catalog.get(definition.name)
        assert executor_entry is not None  # caller already resolved it
        _, executor = executor_entry
        durably_claimed = row.status == ToolCallStatus.EXECUTING.value

        if durably_claimed:
            execution_session = self._ctx.session
            execution_row = await execution_session.scalar(
                select(ToolCall)
                .where(
                    ToolCall.id == row.id,
                    ToolCall.workspace_id == self._ctx.workspace_id,
                    ToolCall.status == ToolCallStatus.EXECUTING.value,
                )
                .with_for_update()
            )
            if execution_row is None:
                raise GatewayStateError(
                    f"tool call {row.id} lost its executable claim before dispatch"
                )
            return await self._run_executor(
                definition,
                execution_row,
                validated_input,
                executor=executor,
                session=execution_session,
                commit_terminal=True,
            )

        return await self._run_executor(
            definition,
            row,
            validated_input,
            executor=executor,
            session=self._ctx.session,
            commit_terminal=durably_claimed,
        )

    async def _run_executor(
        self,
        definition: ToolDefinition,
        row: ToolCall,
        validated_input: BaseModel,
        *,
        executor: ToolExecutor,
        session: AsyncSession,
        commit_terminal: bool,
    ) -> GatewayOutcome:
        """Dispatch one claimed executor and durably record its outcome."""

        # The executor sees which tool_call row it is serving so records it
        # creates (sandbox jobs) can link back to it (plan 14).
        execution_ctx = replace(self._ctx, session=session, tool_call_id=row.id)
        tool_call_id = row.id

        started = time.monotonic()
        row.started_at = datetime.now(UTC)
        try:
            output_model = await executor(execution_ctx, validated_input)
            output = self._sanitize(output_model.model_dump(mode="json"))
            status: GatewayStatus = "executed"
            error_code = None
        except Exception as exc:
            if commit_terminal:
                # Once a claimed mutation was dispatched, a timeout or
                # ordinary executor exception cannot prove that no external
                # effect happened. Roll back any partial internal DB writes,
                # then persist uncertainty in a separate transaction.
                await session.rollback()
                return await self._persist_execution_unknown(
                    tool_call_id,
                    risk=definition.risk.value,
                )
            output = self._sanitize({"error": f"{type(exc).__name__}: {exc}"})
            status = "failed"
            error_code = "execution_error"
        except BaseException:
            # Cancellation or process shutdown can occur after an external
            # side effect but before a normal terminal outcome is known.
            await session.rollback()
            if commit_terminal:
                await self._persist_execution_unknown(
                    tool_call_id,
                    risk=definition.risk.value,
                )
            raise
        duration_ms = int((time.monotonic() - started) * 1000)

        row.completed_at = datetime.now(UTC)
        row.duration_ms = duration_ms
        row.sanitized_output_json = output
        row.status = (
            ToolCallStatus.COMPLETED.value if status == "executed" else ToolCallStatus.FAILED.value
        )
        row.error_code = error_code

        self._audit_on(
            session,
            "tool.call.executed" if status == "executed" else "tool.call.failed",
            row.id,
            {"tool_name": definition.name, "risk": definition.risk.value, "status": row.status},
        )
        if commit_terminal:
            # Persist the executor's terminal result (and any same-database
            # side effect) separately from the activity's transcript bundle.
            # A crash between those commits can then repair the bundle by
            # replaying this exact outcome without invoking the executor.
            await session.commit()
            await session.refresh(row)
        return GatewayOutcome(
            status=status,
            tool_call_id=row.id,
            tool_name=definition.name,
            risk=definition.risk.value,
            decision_code="granted",
            decision_reason="executed through the tool gateway",
            sanitized_input=row.sanitized_input_json,
            sanitized_output=output,
            approval_id=row.approval_id,
            error_code=error_code,
            duration_ms=duration_ms,
        )

    # --- entry points ---

    async def request(
        self,
        tool_name: str,
        arguments_json: str,
        *,
        provider_call_id: str = "",
        invocation_id: UUID | None = None,
    ) -> GatewayOutcome:
        if invocation_id is None:
            return await self._request_once(
                tool_name,
                arguments_json,
                provider_call_id=provider_call_id,
                invocation_id=None,
            )
        async with self._invocation_lifecycle_lock(invocation_id) as gateway:
            return await gateway._request_once(
                tool_name,
                arguments_json,
                provider_call_id=provider_call_id,
                invocation_id=invocation_id,
            )

    async def _request_once(
        self,
        tool_name: str,
        arguments_json: str,
        *,
        provider_call_id: str = "",
        invocation_id: UUID | None = None,
    ) -> GatewayOutcome:
        """Authorize (and, when allowed, execute) one structured tool call.

        ``provider_call_id`` is the model's tool_call id; it rides along so a
        later approval resume can stitch the result back into the transcript.
        """
        sanitized_provider = self._sanitize({"provider_call_id": provider_call_id}).get(
            "provider_call_id", ""
        )
        safe_provider_call_id = (
            sanitized_provider[:200] if isinstance(sanitized_provider, str) else ""
        )
        canonical_unvalidated_input: dict[str, Any] | None = None
        try:
            unvalidated = strict_json_loads(arguments_json)
        except (json.JSONDecodeError, StrictJSONError):
            pass
        else:
            if isinstance(unvalidated, dict) and self._sanitize(unvalidated) == unvalidated:
                canonical_unvalidated_input = unvalidated
        # 1. Registry lookup: unknown tools are recorded denials.
        entry = self._catalog.get(tool_name)
        if entry is None:
            raw = self._sanitize({"_raw_arguments": arguments_json})
            raw_arguments = raw.get("_raw_arguments")
            if isinstance(raw_arguments, str):
                raw["_raw_arguments"] = raw_arguments[:2_000]
            sanitized_name = self._sanitize({"tool_name": tool_name}).get("tool_name", "")
            persisted_name = (
                sanitized_name[:200] if isinstance(sanitized_name, str) else ""
            ) or "unknown"
            if invocation_id is not None:
                replay = await self._existing_unvalidated_invocation(
                    invocation_id,
                    tool_name=persisted_name,
                    sanitized_input=raw,
                    canonical_input=canonical_unvalidated_input,
                )
                if replay is not None:
                    return replay
                await self._require_durable_execution_context()
            return await self._persist_denial(
                invocation_id=invocation_id,
                tool_name=persisted_name,
                code="tool_not_found",
                reason=f"no registered tool named '{persisted_name}'",
                sanitized_input=raw,
                risk=None,
            )
        definition, _ = entry

        # 2. Strict schema validation of the structured arguments (plan 21.4).
        parse_error: str | None = None
        validated: BaseModel | None = None
        try:
            arguments = strict_json_loads(arguments_json)
            if not isinstance(arguments, dict):
                parse_error = "arguments must be a JSON object"
            else:
                validated = definition.input_model.model_validate(arguments)
        except (json.JSONDecodeError, StrictJSONError):
            parse_error = "arguments are not valid JSON"
        except ValidationError as exc:
            parse_error = f"arguments do not match the tool schema: {exc.error_count()} error(s)"

        if validated is None:
            raw = self._sanitize({"_raw_arguments": arguments_json})
            raw_arguments = raw.get("_raw_arguments")
            if isinstance(raw_arguments, str):
                raw["_raw_arguments"] = raw_arguments[:2_000]
            if invocation_id is not None:
                replay = await self._existing_unvalidated_invocation(
                    invocation_id,
                    tool_name=definition.name,
                    sanitized_input=raw,
                    canonical_input=canonical_unvalidated_input,
                )
                if replay is not None:
                    return replay
                await self._require_durable_execution_context()
            return await self._persist_denial(
                invocation_id=invocation_id,
                tool_name=definition.name,
                code="invalid_input",
                reason=parse_error or "invalid input",
                sanitized_input=raw,
                risk=definition.risk.value,
            )

        # Scope values come from the JSON-mode dump so UUIDs and other rich
        # types compare as the same strings stored in grant scope_json.
        dumped = validated.model_dump(mode="json")
        sanitized_input = self._sanitize(dumped)
        requested_scope = {
            key: dumped[key] for key in definition.scope_keys if dumped.get(key) is not None
        }
        connection_id = _connection_uuid(dumped)

        # Agent activities derive this key from run + step + call ordinal,
        # rather than from a provider-generated call id. A retried model call
        # can therefore regenerate provider ids without repeating a durable
        # external mutation.
        if invocation_id is not None:
            replay = await self._existing_invocation_outcome(invocation_id, definition, dumped)
            if replay is not None:
                return replay
            await self._require_durable_execution_context()
            if sanitized_input != dumped:
                code = "invocation_input_not_lossless"
                reason = (
                    "runtime invocation input changed during sanitization and cannot be "
                    "bound safely to an exact retry"
                )
                return await self._persist_denial(
                    invocation_id=invocation_id,
                    tool_name=definition.name,
                    code=code,
                    reason=reason,
                    sanitized_input=sanitized_input,
                    risk=definition.risk.value,
                    connection_id=connection_id,
                )

        # 3-5. Grants, scope, and policy — live from Postgres, so a revoked
        # grant takes effect immediately even mid-run.
        grants = await self._load_grants()
        rules = await self._load_rules()
        decision = evaluate(definition, grants=grants, rules=rules, requested_scope=requested_scope)

        if decision.decision is DecisionType.DENY:
            return await self._persist_denial(
                invocation_id=invocation_id,
                tool_name=definition.name,
                code=decision.code,
                reason=decision.reason,
                sanitized_input=sanitized_input,
                risk=definition.risk.value,
                connection_id=connection_id,
            )

        # Tool-specific policy validator (plan 7.5): e.g. the delegation
        # relationship/cycle/depth model. Runs before approval staging so
        # humans are never asked to approve a call policy already forbids.
        validator = self._catalog.validator_for(definition.name)
        if validator is not None:
            veto = await validator(self._ctx, validated, grants)
            if veto is not None and veto.decision is DecisionType.DENY:
                return await self._persist_denial(
                    invocation_id=invocation_id,
                    tool_name=definition.name,
                    code=veto.code,
                    reason=veto.reason,
                    sanitized_input=sanitized_input,
                    risk=definition.risk.value,
                    connection_id=connection_id,
                )

        if decision.decision is DecisionType.REQUIRE_APPROVAL:
            # An approval must replay the exact arguments a human inspected.
            # Redaction or truncation is safe for storage but changes the
            # operation, so such a call cannot be parked for later execution.
            if sanitized_input != dumped:
                code = "approval_input_not_lossless"
                reason = (
                    "tool input changed during required sanitization and cannot be "
                    "replayed safely after approval"
                )
                return await self._persist_denial(
                    invocation_id=invocation_id,
                    tool_name=definition.name,
                    code=code,
                    reason=reason,
                    sanitized_input=sanitized_input,
                    risk=definition.risk.value,
                    connection_id=connection_id,
                )
            connection_digest: str | None = None
            if connection_id is not None:
                connection_digest = await self._connection_authorization_digest(connection_id)
                if connection_digest is None:
                    code = "approval_connection_unavailable"
                    reason = "connection authorization state is unavailable for approval binding"
                    return await self._persist_denial(
                        invocation_id=invocation_id,
                        tool_name=definition.name,
                        code=code,
                        reason=reason,
                        sanitized_input=sanitized_input,
                        risk=definition.risk.value,
                        connection_id=connection_id,
                    )
            now = datetime.now(UTC)
            tool_call_id = invocation_id or new_uuid7()
            approval = Approval(
                id=new_uuid7(),
                workspace_id=self._ctx.workspace_id,
                task_id=self._ctx.task_id,
                run_id=self._ctx.run_id,
                requested_by_agent_id=self._ctx.agent_id,
                action_type=definition.name,
                action_payload_sanitized={
                    "approval_format_version": _APPROVAL_FORMAT_VERSION,
                    "workspace_id": str(self._ctx.workspace_id),
                    "agent_id": str(self._ctx.agent_id),
                    "run_id": str(self._ctx.run_id),
                    "task_id": str(self._ctx.task_id),
                    "tool_name": definition.name,
                    "capability": definition.required_capability,
                    "risk": definition.risk.value,
                    "input": sanitized_input,
                    "tool_call_id": str(tool_call_id),
                    "invocation_format_version": TOOL_INVOCATION_FORMAT_VERSION,
                    "invocation_id": str(tool_call_id),
                    "connection_authorization_digest": connection_digest,
                    "provider_call_id": safe_provider_call_id,
                },
                reason=decision.reason,
                status=ApprovalStatus.PENDING.value,
                requested_at=now,
            )
            self._ctx.session.add(approval)
            row = ToolCall(
                id=tool_call_id,
                workspace_id=self._ctx.workspace_id,
                run_id=self._ctx.run_id,
                agent_id=self._ctx.agent_id,
                tool_name=definition.name,
                connection_id=connection_id,
                sanitized_input_json=sanitized_input,
                sanitized_output_json={},
                status=ToolCallStatus.PENDING_APPROVAL.value,
                approval_id=approval.id,
                started_at=now,
            )
            self._ctx.session.add(row)
            self._audit("tool.call.requested", row.id, {"tool_name": definition.name})
            self._audit(
                "approval.requested",
                row.id,
                {
                    "approval_id": str(approval.id),
                    "tool_name": definition.name,
                    "risk": definition.risk.value,
                    "reason": decision.reason,
                },
            )
            if invocation_id is not None:
                try:
                    await self._ctx.session.commit()
                except IntegrityError:
                    await self._ctx.session.rollback()
                    self._ctx.session.expire_all()
                    replay = await self._existing_invocation_outcome(
                        invocation_id,
                        definition,
                        dumped,
                    )
                    if replay is None:
                        raise GatewayStateError(
                            f"tool call {invocation_id} approval could not be reloaded"
                        ) from None
                    return replay
            return GatewayOutcome(
                status="needs_approval",
                tool_call_id=row.id,
                tool_name=definition.name,
                risk=definition.risk.value,
                decision_code=decision.code,
                decision_reason=decision.reason,
                sanitized_input=sanitized_input,
                approval_id=approval.id,
                provider_call_id=safe_provider_call_id,
            )

        # 6. Budget / rate limiting.
        # TODO(Phase 10): enforce per-agent budget and rate limits here
        # (plan 15.5). Phase 4 deliberately ships without enforcement.

        # 7-8. Execute and sanitize.
        if invocation_id is not None:
            claimed_row, replay = await self._claim_direct_call(
                definition,
                invocation_id=invocation_id,
                sanitized_input=sanitized_input,
                dumped=dumped,
                connection_id=connection_id,
            )
            if replay is not None:
                return replay
            assert claimed_row is not None
            row = claimed_row
        else:
            row = ToolCall(
                id=new_uuid7(),
                workspace_id=self._ctx.workspace_id,
                run_id=self._ctx.run_id,
                agent_id=self._ctx.agent_id,
                tool_name=definition.name,
                connection_id=connection_id,
                sanitized_input_json=sanitized_input,
                sanitized_output_json={},
                status=ToolCallStatus.FAILED.value,  # overwritten by _execute
            )
            self._ctx.session.add(row)
            self._audit("tool.call.requested", row.id, {"tool_name": definition.name})
        return await self._execute(definition, row, validated)

    async def _validate_parked_approval_binding(
        self,
        approval: Approval,
        row: ToolCall,
    ) -> tuple[_ValidatedApprovalBinding | None, GatewayOutcome | None]:
        """Validate the exact format, identity, tool, input, and connection shown."""
        approval_id = approval.id
        payload = approval.action_payload_sanitized
        payload_risk = payload.get("risk") if isinstance(payload.get("risk"), str) else None
        if payload.get("approval_format_version") != _APPROVAL_FORMAT_VERSION:
            return None, self._finish_parked_call(
                row,
                approval_id,
                status="failed",
                code="approval_format_unsupported",
                reason="approval payload is missing the supported format version",
                risk=payload_risk,
            )
        expected_binding = {
            "workspace_id": str(self._ctx.workspace_id),
            "agent_id": str(self._ctx.agent_id),
            "run_id": str(self._ctx.run_id),
            "task_id": str(self._ctx.task_id),
            "tool_name": row.tool_name,
            "input": row.sanitized_input_json,
            "tool_call_id": str(row.id),
        }
        if any(payload.get(key) != value for key, value in expected_binding.items()):
            return None, self._finish_parked_call(
                row,
                approval_id,
                status="failed",
                code="approval_binding_mismatch",
                reason="approval payload does not match its workspace, run, tool, or input",
                risk=payload_risk,
            )
        if not (
            payload.get("invocation_id") == str(row.id)
            and payload.get("invocation_format_version") == TOOL_INVOCATION_FORMAT_VERSION
        ):
            return None, self._finish_parked_call(
                row,
                approval_id,
                status="failed",
                code="approval_binding_mismatch",
                reason="approval payload has an invalid runtime invocation binding",
                risk=payload_risk,
            )

        entry = self._catalog.get(row.tool_name)
        if entry is None:
            return None, self._finish_parked_call(
                row,
                approval_id,
                status="failed",
                code="tool_not_found",
                reason="tool disappeared from the registry before resolution",
                risk=payload_risk,
            )
        definition, _ = entry
        if (
            payload.get("capability") != definition.required_capability
            or payload.get("risk") != definition.risk.value
        ):
            return None, self._finish_parked_call(
                row,
                approval_id,
                status="failed",
                code="approval_definition_changed",
                reason="tool capability or risk changed after approval was requested",
                risk=definition.risk.value,
            )

        try:
            validated = definition.input_model.model_validate(row.sanitized_input_json)
        except ValidationError:
            return None, self._finish_parked_call(
                row,
                approval_id,
                status="failed",
                code="invalid_input",
                reason="stored input no longer matches the tool schema",
                risk=definition.risk.value,
            )
        dumped = validated.model_dump(mode="json")
        if dumped != row.sanitized_input_json:
            return None, self._finish_parked_call(
                row,
                approval_id,
                status="failed",
                code="approval_input_not_lossless",
                reason="stored input no longer round-trips through the tool schema",
                risk=definition.risk.value,
            )

        connection_id = _connection_uuid(dumped)
        if connection_id != row.connection_id:
            return None, self._finish_parked_call(
                row,
                approval_id,
                status="failed",
                code="approval_binding_mismatch",
                reason="stored connection target does not match the parked tool call",
                risk=definition.risk.value,
            )
        expected_connection_digest = payload.get("connection_authorization_digest")
        if connection_id is None:
            if expected_connection_digest is not None:
                return None, self._finish_parked_call(
                    row,
                    approval_id,
                    status="failed",
                    code="approval_binding_mismatch",
                    reason="approval has unexpected connection authorization state",
                    risk=definition.risk.value,
                )
            connection_digest = None
        else:
            current_digest = await self._connection_authorization_digest(connection_id)
            if not isinstance(expected_connection_digest, str) or (
                current_digest != expected_connection_digest
            ):
                return None, self._finish_parked_call(
                    row,
                    approval_id,
                    status="denied",
                    code="approval_connection_changed",
                    reason="connection authorization changed; request a new approval",
                    risk=definition.risk.value,
                )
            connection_digest = expected_connection_digest
        return (
            _ValidatedApprovalBinding(
                definition=definition,
                validated_input=validated,
                dumped=dumped,
                connection_id=connection_id,
                connection_digest=connection_digest,
            ),
            None,
        )

    async def resolve_approved(self, approval_id: UUID) -> GatewayOutcome:
        """Resolve one approval under the call's full lifecycle lock."""
        _approval, row = await self._load_approval_pair(approval_id)
        invocation_id = row.id
        # The PostgreSQL lifecycle uses its own connection/session and must
        # reload after waiting. Approval decisions are durable before this
        # activity starts, so no caller-owned write is discarded here.
        bind = self._ctx.session.bind
        if isinstance(bind, AsyncEngine) and bind.dialect.name == "postgresql":
            await self._ctx.session.rollback()
        async with self._invocation_lifecycle_lock(invocation_id) as gateway:
            try:
                outcome = await gateway._resolve_approved_once(approval_id)
                await gateway._ctx.session.commit()
                return outcome
            except BaseException:
                await gateway._ctx.session.rollback()
                raise

    async def _resolve_approved_once(self, approval_id: UUID) -> GatewayOutcome:
        """Execute a tool call whose approval row says APPROVED.

        The Postgres approval row is the authority — never a workflow signal
        payload and never model output (plan 52).
        """
        approval, row = await self._load_approval_pair(approval_id)
        if approval.status != ApprovalStatus.APPROVED.value:
            raise GatewayStateError(f"approval {approval_id} is '{approval.status}', not approved")
        if row.status in _TERMINAL_TOOL_STATUSES:
            return self._replayed_outcome(row, approval=approval)
        if row.status in (
            ToolCallStatus.EXECUTING.value,
            ToolCallStatus.EXECUTION_UNKNOWN.value,
        ):
            payload_risk = approval.action_payload_sanitized.get("risk")
            return await self._persist_execution_unknown(
                row.id,
                risk=payload_risk if isinstance(payload_risk, str) else None,
            )
        if row.status != ToolCallStatus.PENDING_APPROVAL.value:
            raise GatewayStateError(f"tool call {row.id} is in unexpected status '{row.status}'")

        binding, binding_failure = await self._validate_parked_approval_binding(approval, row)
        if binding_failure is not None:
            return binding_failure
        assert binding is not None
        definition = binding.definition
        validated = binding.validated_input
        dumped = binding.dumped
        connection_id = binding.connection_id
        expected_connection_digest = binding.connection_digest

        # Grants and policy are live authorization state, not a snapshot from
        # when the request was parked. A revocation or newly-forbidden policy
        # therefore takes effect before any executor side effect.
        requested_scope = {
            key: dumped[key] for key in definition.scope_keys if dumped.get(key) is not None
        }
        grants = await self._load_grants()
        decision = evaluate(
            definition,
            grants=grants,
            rules=await self._load_rules(),
            requested_scope=requested_scope,
        )
        if decision.decision is DecisionType.DENY:
            return self._finish_parked_call(
                row,
                approval_id,
                status="denied",
                code=decision.code,
                reason=decision.reason,
                risk=definition.risk.value,
            )

        # REQUIRE_APPROVAL is satisfied only by the exact approved row loaded
        # and identity-bound above. Re-run the tool-specific validator too:
        # org graph, task lineage, or other scoped facts may have changed.
        validator = self._catalog.validator_for(definition.name)
        if validator is not None:
            veto = await validator(self._ctx, validated, grants)
            if veto is not None and veto.decision is DecisionType.DENY:
                return self._finish_parked_call(
                    row,
                    approval_id,
                    status="denied",
                    code=veto.code,
                    reason=veto.reason,
                    risk=definition.risk.value,
                )

        replay = await self._claim_parked_call(approval, row)
        if replay is not None:
            return replay

        # Reacquire shared locks after the durable claim and keep them through
        # the outer activity commit. This closes the approval/executor TOCTOU
        # window for credential rotation, config, auth type, and status.
        if connection_id is not None:
            locked_digest = await self._connection_authorization_digest(connection_id, lock=True)
            if locked_digest != expected_connection_digest:
                return self._finish_parked_call(
                    row,
                    approval_id,
                    status="denied",
                    code="approval_connection_changed",
                    reason="connection authorization changed; request a new approval",
                    risk=definition.risk.value,
                )
        self._audit(
            "tool.call.approved",
            row.id,
            {"approval_id": str(approval_id), "tool_name": definition.name},
        )
        return await self._execute(definition, row, validated)

    async def resolve_rejected(self, approval_id: UUID) -> GatewayOutcome:
        """Resolve one rejection under the call's full lifecycle lock."""
        _approval, row = await self._load_approval_pair(approval_id)
        invocation_id = row.id
        bind = self._ctx.session.bind
        if isinstance(bind, AsyncEngine) and bind.dialect.name == "postgresql":
            await self._ctx.session.rollback()
        async with self._invocation_lifecycle_lock(invocation_id) as gateway:
            try:
                outcome = await gateway._resolve_rejected_once(approval_id)
                await gateway._ctx.session.commit()
                return outcome
            except BaseException:
                await gateway._ctx.session.rollback()
                raise

    async def _resolve_rejected_once(self, approval_id: UUID) -> GatewayOutcome:
        """Record the human rejection on the pending tool call."""
        approval, row = await self._load_approval_pair(approval_id)
        if approval.status != ApprovalStatus.REJECTED.value:
            raise GatewayStateError(f"approval {approval_id} is '{approval.status}', not rejected")
        if row.status in _TERMINAL_TOOL_STATUSES:
            return self._replayed_outcome(row, approval=approval)
        if row.status in (
            ToolCallStatus.EXECUTING.value,
            ToolCallStatus.EXECUTION_UNKNOWN.value,
        ):
            payload_risk = approval.action_payload_sanitized.get("risk")
            return await self._persist_execution_unknown(
                row.id,
                risk=payload_risk if isinstance(payload_risk, str) else None,
            )
        if row.status != ToolCallStatus.PENDING_APPROVAL.value:
            raise GatewayStateError(f"tool call {row.id} is '{row.status}', not pending approval")
        binding, binding_failure = await self._validate_parked_approval_binding(approval, row)
        if binding_failure is not None:
            return binding_failure
        assert binding is not None
        row.status = ToolCallStatus.REJECTED.value
        row.completed_at = datetime.now(UTC)
        row.error_code = "approval_rejected"
        self._audit(
            "tool.call.rejected",
            row.id,
            {"approval_id": str(approval_id), "tool_name": row.tool_name},
        )
        return GatewayOutcome(
            status="rejected",
            tool_call_id=row.id,
            tool_name=row.tool_name,
            risk=binding.definition.risk.value,
            decision_code="approval_rejected",
            decision_reason="a human rejected this tool call",
            sanitized_input=row.sanitized_input_json,
            approval_id=approval_id,
            error_code="approval_rejected",
        )

    async def _load_approval_pair(self, approval_id: UUID) -> tuple[Approval, ToolCall]:
        approval = await self._ctx.session.scalar(
            select(Approval).where(
                Approval.id == approval_id,
                Approval.workspace_id == self._ctx.workspace_id,
            )
        )
        if approval is None:
            await self._audit_approval_resolution_failure(approval_id, "approval_not_found")
            raise GatewayStateError(f"approval {approval_id} not found in workspace")
        if (
            approval.requested_by_agent_id != self._ctx.agent_id
            or approval.run_id != self._ctx.run_id
            or approval.task_id != self._ctx.task_id
        ):
            await self._audit_approval_resolution_failure(
                approval_id, "approval_execution_context_mismatch"
            )
            raise GatewayStateError(
                f"approval {approval_id} does not belong to this execution context"
            )
        rows = list(
            await self._ctx.session.scalars(
                select(ToolCall).where(
                    ToolCall.approval_id == approval_id,
                    ToolCall.workspace_id == self._ctx.workspace_id,
                )
            )
        )
        if not rows:
            await self._audit_approval_resolution_failure(approval_id, "approval_tool_call_missing")
            raise GatewayStateError(f"no tool_call row references approval {approval_id}")
        if len(rows) != 1:
            await self._audit_approval_resolution_failure(
                approval_id, "approval_tool_call_ambiguous"
            )
            raise GatewayStateError(f"multiple tool_call rows reference approval {approval_id}")
        row = rows[0]
        if (
            row.agent_id != approval.requested_by_agent_id
            or row.run_id != approval.run_id
            or row.tool_name != approval.action_type
        ):
            await self._audit_approval_resolution_failure(
                approval_id, "approval_tool_call_mismatch"
            )
            raise GatewayStateError(f"tool call {row.id} does not match its approval")
        return approval, row

    async def _audit_approval_resolution_failure(self, approval_id: UUID, code: str) -> None:
        self._ctx.session.add(
            AuditEvent(
                workspace_id=self._ctx.workspace_id,
                actor_type=ActorType.AGENT.value,
                actor_id=self._ctx.agent_id,
                action="tool.call.failed",
                target_type="approval",
                target_id=approval_id,
                metadata_json={
                    "run_id": str(self._ctx.run_id),
                    "task_id": str(self._ctx.task_id),
                    "code": code,
                },
            )
        )
        await self._ctx.session.commit()


class GatewayStateError(Exception):
    """The persisted approval/tool-call state does not permit the operation."""
