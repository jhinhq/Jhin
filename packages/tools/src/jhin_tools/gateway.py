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

import json
import time
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select

from jhin_db.models import Agent, AgentCapabilityGrant, Approval, AuditEvent, ToolCall
from jhin_domain import ActorType, ApprovalStatus, ToolCallStatus, new_uuid7
from jhin_policy import (
    DecisionType,
    Grant,
    GrantEffect,
    PolicyRule,
    ToolDefinition,
    evaluate,
)
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.sanitize import MAX_DOCUMENT_BYTES, sanitize_payload

GatewayStatus = Literal["executed", "failed", "denied", "needs_approval", "rejected"]


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
    error_code: str | None = None
    duration_ms: int | None = None

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

    def _audit(self, action: str, target_id: UUID, metadata: dict[str, Any]) -> None:
        self._ctx.session.add(
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
    ) -> tuple[ToolCall, GatewayOutcome]:
        now = datetime.now(UTC)
        # Ids are assigned eagerly (not at flush) because they go into the
        # outcome and audit rows before the caller commits.
        row = ToolCall(
            id=new_uuid7(),
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

        started = time.monotonic()
        row.started_at = datetime.now(UTC)
        try:
            output_model = await executor(self._ctx, validated_input)
            output = self._sanitize(output_model.model_dump(mode="json"))
            status: GatewayStatus = "executed"
            error_code = None
        except Exception as exc:
            output = self._sanitize({"error": f"{type(exc).__name__}: {exc}"})
            status = "failed"
            error_code = "execution_error"
        duration_ms = int((time.monotonic() - started) * 1000)

        row.completed_at = datetime.now(UTC)
        row.duration_ms = duration_ms
        row.sanitized_output_json = output
        row.status = (
            ToolCallStatus.COMPLETED.value if status == "executed" else ToolCallStatus.FAILED.value
        )
        row.error_code = error_code

        self._audit(
            "tool.call.executed" if status == "executed" else "tool.call.failed",
            row.id,
            {"tool_name": definition.name, "risk": definition.risk.value, "status": row.status},
        )
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
        self, tool_name: str, arguments_json: str, *, provider_call_id: str = ""
    ) -> GatewayOutcome:
        """Authorize (and, when allowed, execute) one structured tool call.

        ``provider_call_id`` is the model's tool_call id; it rides along so a
        later approval resume can stitch the result back into the transcript.
        """
        # 1. Registry lookup: unknown tools are recorded denials.
        entry = self._catalog.get(tool_name)
        if entry is None:
            raw = self._sanitize({"_raw_arguments": arguments_json[:2000]})
            row, outcome = self._denied(
                tool_name[:200] or "unknown",
                code="tool_not_found",
                reason=f"no registered tool named '{tool_name[:200]}'",
                sanitized_input=raw,
                risk=None,
            )
            self._audit("tool.call.requested", row.id, {"tool_name": row.tool_name})
            self._audit("tool.call.denied", row.id, {"code": "tool_not_found"})
            return outcome
        definition, _ = entry

        # 2. Strict schema validation of the structured arguments (plan 21.4).
        parse_error: str | None = None
        validated: BaseModel | None = None
        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                parse_error = "arguments must be a JSON object"
            else:
                validated = definition.input_model.model_validate(arguments)
        except json.JSONDecodeError:
            parse_error = "arguments are not valid JSON"
        except ValidationError as exc:
            parse_error = f"arguments do not match the tool schema: {exc.error_count()} error(s)"

        if validated is None:
            raw = self._sanitize({"_raw_arguments": arguments_json[:2000]})
            row, outcome = self._denied(
                definition.name,
                code="invalid_input",
                reason=parse_error or "invalid input",
                sanitized_input=raw,
                risk=definition.risk.value,
            )
            self._audit("tool.call.requested", row.id, {"tool_name": definition.name})
            self._audit("tool.call.denied", row.id, {"code": "invalid_input"})
            return outcome

        # Scope values come from the JSON-mode dump so UUIDs and other rich
        # types compare as the same strings stored in grant scope_json.
        dumped = validated.model_dump(mode="json")
        sanitized_input = self._sanitize(dumped)
        requested_scope = {
            key: dumped[key] for key in definition.scope_keys if dumped.get(key) is not None
        }
        connection_id = _connection_uuid(dumped)

        # 3-5. Grants, scope, and policy — live from Postgres, so a revoked
        # grant takes effect immediately even mid-run.
        grants = await self._load_grants()
        rules = await self._load_rules()
        decision = evaluate(definition, grants=grants, rules=rules, requested_scope=requested_scope)

        if decision.decision is DecisionType.DENY:
            row, outcome = self._denied(
                definition.name,
                code=decision.code,
                reason=decision.reason,
                sanitized_input=sanitized_input,
                risk=definition.risk.value,
                connection_id=connection_id,
            )
            self._audit("tool.call.requested", row.id, {"tool_name": definition.name})
            self._audit(
                "tool.call.denied",
                row.id,
                {"code": decision.code, "reason": decision.reason, "risk": definition.risk.value},
            )
            return outcome

        if decision.decision is DecisionType.REQUIRE_APPROVAL:
            now = datetime.now(UTC)
            approval = Approval(
                id=new_uuid7(),
                workspace_id=self._ctx.workspace_id,
                task_id=self._ctx.task_id,
                run_id=self._ctx.run_id,
                requested_by_agent_id=self._ctx.agent_id,
                action_type=definition.name,
                action_payload_sanitized={
                    "tool_name": definition.name,
                    "capability": definition.required_capability,
                    "risk": definition.risk.value,
                    "input": sanitized_input,
                    "provider_call_id": provider_call_id,
                },
                reason=decision.reason,
                status=ApprovalStatus.PENDING.value,
                requested_at=now,
            )
            self._ctx.session.add(approval)
            row = ToolCall(
                id=new_uuid7(),
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
            return GatewayOutcome(
                status="needs_approval",
                tool_call_id=row.id,
                tool_name=definition.name,
                risk=definition.risk.value,
                decision_code=decision.code,
                decision_reason=decision.reason,
                sanitized_input=sanitized_input,
                approval_id=approval.id,
            )

        # 6. Budget / rate limiting.
        # TODO(Phase 10): enforce per-agent budget and rate limits here
        # (plan 15.5). Phase 4 deliberately ships without enforcement.

        # 7-8. Execute and sanitize.
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

    async def resolve_approved(self, approval_id: UUID) -> GatewayOutcome:
        """Execute a tool call whose approval row says APPROVED.

        The Postgres approval row is the authority — never a workflow signal
        payload and never model output (plan 52).
        """
        approval, row = await self._load_approval_pair(approval_id)
        if approval.status != ApprovalStatus.APPROVED.value:
            raise GatewayStateError(f"approval {approval_id} is '{approval.status}', not approved")
        entry = self._catalog.get(row.tool_name)
        if entry is None:
            row.status = ToolCallStatus.FAILED.value
            row.completed_at = datetime.now(UTC)
            row.error_code = "tool_not_found"
            return GatewayOutcome(
                status="failed",
                tool_call_id=row.id,
                tool_name=row.tool_name,
                risk=None,
                decision_code="tool_not_found",
                decision_reason="tool disappeared from the registry before execution",
                sanitized_input=row.sanitized_input_json,
                approval_id=approval_id,
                error_code="tool_not_found",
            )
        definition, _ = entry
        # The persisted sanitized input is re-validated before execution;
        # system tools carry no secrets so sanitized == original.
        try:
            validated = definition.input_model.model_validate(row.sanitized_input_json)
        except ValidationError:
            row.status = ToolCallStatus.FAILED.value
            row.completed_at = datetime.now(UTC)
            row.error_code = "invalid_input"
            return GatewayOutcome(
                status="failed",
                tool_call_id=row.id,
                tool_name=definition.name,
                risk=definition.risk.value,
                decision_code="invalid_input",
                decision_reason="stored input no longer matches the tool schema",
                sanitized_input=row.sanitized_input_json,
                approval_id=approval_id,
                error_code="invalid_input",
            )
        self._audit(
            "tool.call.approved",
            row.id,
            {"approval_id": str(approval_id), "tool_name": definition.name},
        )
        return await self._execute(definition, row, validated)

    async def resolve_rejected(self, approval_id: UUID) -> GatewayOutcome:
        """Record the human rejection on the pending tool call."""
        approval, row = await self._load_approval_pair(approval_id)
        if approval.status != ApprovalStatus.REJECTED.value:
            raise GatewayStateError(f"approval {approval_id} is '{approval.status}', not rejected")
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
            risk=None,
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
            raise GatewayStateError(f"approval {approval_id} not found in workspace")
        row = await self._ctx.session.scalar(
            select(ToolCall).where(
                ToolCall.approval_id == approval_id,
                ToolCall.workspace_id == self._ctx.workspace_id,
            )
        )
        if row is None:
            raise GatewayStateError(f"no tool_call row references approval {approval_id}")
        return approval, row


class GatewayStateError(Exception):
    """The persisted approval/tool-call state does not permit the operation."""
