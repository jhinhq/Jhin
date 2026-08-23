"""Agent-side transcript, timeline, and run projections from durable IDs."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.coordination_activities import (
    review_decision_from_output,
    work_request_start_from_output,
)
from jhin_agent_worker.reasoning import (
    AgentStepReasoningRecord,
    ManifestCall,
    load_step_event,
    manifest_calls_from_payload,
)
from jhin_agent_worker.resources import Resources
from jhin_db.budget import MICROS_PER_CENT, month_spend_micros, workspace_budget_settings
from jhin_db.models import (
    Agent,
    AgentRun,
    Approval,
    Message,
    RunEvent,
    Task,
    ToolCall,
    WorkReview,
    Workspace,
)
from jhin_domain import (
    ApprovalStatus,
    MessageVisibility,
    RecipientType,
    RunStatus,
    SenderType,
    TaskState,
    ToolCallStatus,
    WorkReviewStatus,
)
from jhin_events import EventEnvelope, EventSource
from jhin_observability import (
    AttributeValue,
    JhinMetrics,
    MetricName,
    get_logger,
    noop_metrics,
    normalize_event_family,
    normalize_span_attributes,
)
from jhin_secrets.redaction import redact_text
from jhin_tools import stable_tool_invocation_id
from jhin_workflows.agent_task.shared import (
    ACTIVITY_COMMIT_AGENT_STEP,
    ACTIVITY_COMMIT_APPROVAL_PROJECTION,
    ACTIVITY_COMMIT_REVIEW_PROJECTION,
    ACTIVITY_FINALIZE_RUN_PROJECTION,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    CommitReviewProjectionInput,
    DelegationRequest,
    FinalizeInput,
    ReviewDecisionSignal,
    StepResult,
    WorkRequestStart,
)
from jhin_workflows.memory_maintenance import (
    SOURCE_KIND_TASK_OUTCOME,
    MemoryMaintenanceInput,
    start_memory_maintenance,
)

_MAX_ARGUMENTS_CHARS = 8_192
_MAX_PROVIDER_TEXT_CHARS = 200
_MAX_REASON_CHARS = 2_000
_VALID_TRANSITION_NODES = frozenset(
    {
        "load_context",
        "reason",
        "call_tool",
        "policy_check",
        "execute_tool",
        "observe",
        "request_approval",
        "request_review",
        "finalize",
    }
)
_TOOL_STATUS_MAP = {
    ToolCallStatus.COMPLETED.value: "executed",
    ToolCallStatus.FAILED.value: "failed",
    ToolCallStatus.DENIED.value: "denied",
    ToolCallStatus.REJECTED.value: "rejected",
    ToolCallStatus.PENDING_APPROVAL.value: "needs_approval",
    ToolCallStatus.PENDING_REVIEW.value: "needs_review",
    ToolCallStatus.EXECUTION_UNKNOWN.value: "execution_unknown",
}
_SAFE_STATUS_REASONS = {
    "executed": "tool execution completed",
    "failed": "tool execution failed",
    "denied": "tool call denied",
    "rejected": "tool approval was rejected",
    "needs_approval": "tool call requires human approval",
    "needs_review": "tool call is waiting for a work review",
    "execution_unknown": ("tool execution outcome is unknown; manual reconciliation is required"),
}
_PRESERVED_FINAL_ERRORS = frozenset(
    {
        "tool_execution_unknown",
        "tool_invocation_mismatch",
        "tool_step_manifest_not_lossless",
    }
)
_AGENT_RUNS_METRIC = "agent_runs_total"
_AGENT_DURATION_METRIC = "agent_run_duration_seconds"
_AGENT_FAILURES_METRIC = "agent_run_failures_total"
_AGENT_SERVICE_LABEL = "service"
_AGENT_OUTCOME_LABEL = "outcome"
_AGENT_FAILURE_LABEL = "failure_class"
_AGENT_SERVICE_VALUE = "agent-worker"
_AGENT_COMPLETED_VALUE = "completed"
_AGENT_FAILED_VALUE = "failed"
_AGENT_CANCELLED_VALUE = "cancelled"
_AGENT_EXECUTION_UNKNOWN_VALUE = "execution_unknown"
_AGENT_BUDGET_VALUE = "budget"
_AGENT_INTERNAL_VALUE = "internal"
_FINALIZATION_VALIDATION_MEASUREMENT = 0

logger = get_logger(__name__)


@dataclass(frozen=True)
class _ProjectedToolOutcome:
    row: ToolCall
    manifest: ManifestCall
    provider_call_id: str
    status: str
    decision_code: str
    decision_reason: str
    risk: str | None
    approval: Approval | None

    def observation_json(self) -> str:
        if self.status == "executed":
            return json.dumps(
                self.row.sanitized_output_json,
                ensure_ascii=False,
                default=str,
            )
        return json.dumps(
            {
                "error": self.row.error_code or self.status,
                "detail": self.decision_reason,
            },
            ensure_ascii=False,
        )


async def _cancel_pending_run_approvals(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
) -> int:
    pending = list(
        await session.scalars(
            select(Approval)
            .where(
                Approval.run_id == run_id,
                Approval.workspace_id == workspace_id,
                Approval.status == ApprovalStatus.PENDING.value,
            )
            .with_for_update()
        )
    )
    now = datetime.now(UTC)
    for approval in pending:
        approval.status = ApprovalStatus.CANCELLED.value
        approval.decided_at = now
        stale_calls = list(
            await session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.approval_id == approval.id,
                    ToolCall.workspace_id == workspace_id,
                    ToolCall.status == ToolCallStatus.PENDING_APPROVAL.value,
                )
                .with_for_update()
            )
        )
        for stale_call in stale_calls:
            stale_call.status = ToolCallStatus.REJECTED.value
            stale_call.completed_at = now
            stale_call.error_code = "run_ended"
    return len(pending)


def _prevalidate_finalization_telemetry(status: object) -> str:
    for value, expected, field in (
        (_AGENT_RUNS_METRIC, "agent_runs_total", "run metric"),
        (_AGENT_DURATION_METRIC, "agent_run_duration_seconds", "duration metric"),
        (_AGENT_FAILURES_METRIC, "agent_run_failures_total", "failure metric"),
        (_AGENT_SERVICE_LABEL, "service", "service label"),
        (_AGENT_OUTCOME_LABEL, "outcome", "outcome label"),
        (_AGENT_FAILURE_LABEL, "failure_class", "failure label"),
        (_AGENT_SERVICE_VALUE, "agent-worker", "service value"),
        (_AGENT_COMPLETED_VALUE, "completed", "completed outcome"),
        (_AGENT_FAILED_VALUE, "failed", "failed outcome"),
        (_AGENT_CANCELLED_VALUE, "cancelled", "cancelled outcome"),
        (
            _AGENT_EXECUTION_UNKNOWN_VALUE,
            "execution_unknown",
            "execution-unknown failure",
        ),
        (_AGENT_BUDGET_VALUE, "budget", "budget failure"),
        (_AGENT_INTERNAL_VALUE, "internal", "internal failure"),
        (_FINALIZATION_VALIDATION_MEASUREMENT, 0, "validation measurement"),
    ):
        if type(value) is not type(expected) or value != expected:
            raise ValueError(f"invalid fixed telemetry schema: {field}")
    normalized_outcome = cast(
        str,
        normalize_span_attributes({"jhin.outcome": cast(AttributeValue, status)})["jhin.outcome"],
    )
    validator = noop_metrics()
    for outcome in (
        _AGENT_COMPLETED_VALUE,
        _AGENT_FAILED_VALUE,
        _AGENT_CANCELLED_VALUE,
        normalized_outcome,
    ):
        validator.counter(cast(MetricName, _AGENT_RUNS_METRIC)).add(
            _FINALIZATION_VALIDATION_MEASUREMENT,
            **{
                _AGENT_SERVICE_LABEL: _AGENT_SERVICE_VALUE,
                _AGENT_OUTCOME_LABEL: outcome,
            },
        )
        validator.histogram(cast(MetricName, _AGENT_DURATION_METRIC)).record(
            _FINALIZATION_VALIDATION_MEASUREMENT,
            **{_AGENT_OUTCOME_LABEL: outcome},
        )
    for failure_class in (
        _AGENT_EXECUTION_UNKNOWN_VALUE,
        _AGENT_BUDGET_VALUE,
        _AGENT_INTERNAL_VALUE,
    ):
        validator.counter(cast(MetricName, _AGENT_FAILURES_METRIC)).add(
            _FINALIZATION_VALIDATION_MEASUREMENT,
            **{_AGENT_FAILURE_LABEL: failure_class},
        )
    return normalized_outcome


def _run_agent_metric(action: Callable[[], None]) -> None:
    try:
        action()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        pass


def _record_agent_counter(
    metrics: JhinMetrics,
    *,
    name: str,
    amount: int | float,
    labels: Mapping[str, str],
) -> None:
    _run_agent_metric(
        lambda: metrics.counter(cast(MetricName, name)).add(
            amount,
            **dict(labels),
        )
    )


def _record_agent_histogram(
    metrics: JhinMetrics,
    *,
    name: str,
    amount: int | float,
    labels: Mapping[str, str],
) -> None:
    _run_agent_metric(
        lambda: metrics.histogram(cast(MetricName, name)).record(
            amount,
            **dict(labels),
        )
    )


def _persisted_duration_seconds(
    started_at: object,
    completed_at: object,
) -> float | None:
    if type(started_at) is not datetime or type(completed_at) is not datetime:
        return None
    started = started_at
    completed = completed_at
    try:
        if (
            started.tzinfo is None
            or completed.tzinfo is None
            or started.utcoffset() is None
            or completed.utcoffset() is None
        ):
            return None
        duration = (completed - started).total_seconds()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    return duration


def _final_failure_class(status: str, error_code: str | None) -> str | None:
    if status != RunStatus.FAILED.value:
        return None
    if error_code == "tool_execution_unknown":
        return _AGENT_EXECUTION_UNKNOWN_VALUE
    if error_code in ("max_steps_exceeded", "budget_exceeded"):
        return _AGENT_BUDGET_VALUE
    return _AGENT_INTERNAL_VALUE


class AgentProjectionActivities:
    def __init__(
        self,
        resources: Resources,
        temporal_client: TemporalClient | None = None,
    ) -> None:
        self._resources = resources
        self._metrics = resources.runtime.metrics
        self._tracer = resources.runtime.tracer
        self._temporal_client = temporal_client

    async def _publish(
        self,
        workspace_id: UUID,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        try:
            await self._resources.publisher.publish(
                EventEnvelope(
                    event_type=event_type,
                    workspace_id=str(workspace_id),
                    source=EventSource(type="agent_worker"),
                    data=data,
                )
            )
        except Exception as error:
            logger.warning(
                "events.publish_failed",
                event_type=normalize_event_family(event_type),
                error_type=type(error).__name__,
            )

    async def _next_seq(self, session: AsyncSession, run_id: UUID) -> int:
        current = await session.scalar(
            select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        )
        return (current if current is not None else -1) + 1

    def _add_run_event(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        seq: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            RunEvent(
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
                event_type=event_type,
                payload_json=payload,
            )
        )

    def _add_tool_message(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        message_type: str,
        content: dict[str, Any],
    ) -> None:
        session.add(
            Message(
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                sender_type=SenderType.AGENT.value,
                sender_id=agent_id,
                recipient_type=RecipientType.TASK.value,
                recipient_id=task_id,
                message_type=message_type,
                content_json=content,
                visibility=MessageVisibility.INTERNAL.value,
            )
        )

    async def _committed_step_result(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
        step_index: int,
        gateway_tool_call_ids: list[str] | None = None,
        cancelled_after_tool_call_id: str | None = None,
    ) -> StepResult | None:
        event = await load_step_event(
            session,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            step_index=step_index,
            event_type="agent.step.committed",
        )
        if event is None:
            return None
        if gateway_tool_call_ids is not None:
            bound_ids = event.payload_json.get("gateway_tool_call_ids")
            if not isinstance(bound_ids, list) or any(
                not isinstance(tool_call_id, str) for tool_call_id in bound_ids
            ):
                raise ApplicationError(
                    "committed step tool ID binding is malformed",
                    type="step_result_malformed",
                    non_retryable=True,
                )
            canonical_ids = [
                str(stable_tool_invocation_id(run_id, step_index, ordinal))
                for ordinal in range(len(bound_ids))
            ]
            if bound_ids != canonical_ids or gateway_tool_call_ids != bound_ids:
                raise ApplicationError(
                    "step projection retry changed its canonical tool ID binding",
                    type="tool_projection_binding_mismatch",
                    non_retryable=True,
                )
            bound_cancellation = event.payload_json.get("cancelled_after_tool_call_id")
            if bound_cancellation is not None and not isinstance(bound_cancellation, str):
                raise ApplicationError(
                    "committed step cancellation binding is malformed",
                    type="step_result_malformed",
                    non_retryable=True,
                )
            if bound_cancellation is not None and (
                not bound_ids or bound_cancellation != bound_ids[-1]
            ):
                raise ApplicationError(
                    "committed step cancellation binding changed its tool prefix",
                    type="step_result_malformed",
                    non_retryable=True,
                )
            if cancelled_after_tool_call_id != bound_cancellation:
                raise ApplicationError(
                    "step projection retry changed its cancellation binding",
                    type="tool_projection_binding_mismatch",
                    non_retryable=True,
                )
        raw_result = event.payload_json.get("result")
        if not isinstance(raw_result, dict):
            raise ApplicationError(
                "committed step result is malformed",
                type="step_result_malformed",
                non_retryable=True,
            )
        raw_delegations = raw_result.get("delegations", [])
        if not isinstance(raw_delegations, list):
            raise ApplicationError(
                "committed step delegations are malformed",
                type="step_result_malformed",
                non_retryable=True,
            )
        raw_work_requests = raw_result.get("work_request_starts", [])
        if not isinstance(raw_work_requests, list):
            raise ApplicationError(
                "committed step work request starts are malformed",
                type="step_result_malformed",
                non_retryable=True,
            )
        raw_review_decisions = raw_result.get("review_decisions", [])
        if not isinstance(raw_review_decisions, list):
            raise ApplicationError(
                "committed step review decisions are malformed",
                type="step_result_malformed",
                non_retryable=True,
            )
        try:
            result = StepResult(
                done=bool(raw_result["done"]),
                input_tokens=int(raw_result.get("input_tokens", 0)),
                output_tokens=int(raw_result.get("output_tokens", 0)),
                cached_tokens=int(raw_result.get("cached_tokens", 0)),
                cost_micros=int(raw_result.get("cost_micros", 0)),
                waiting_approval_id=raw_result.get("waiting_approval_id"),
                waiting_review_id=raw_result.get("waiting_review_id"),
                delegations=[
                    DelegationRequest(**item) for item in raw_delegations if isinstance(item, dict)
                ],
                work_request_starts=[
                    WorkRequestStart(**item) for item in raw_work_requests if isinstance(item, dict)
                ],
                review_decisions=[
                    ReviewDecisionSignal(**item)
                    for item in raw_review_decisions
                    if isinstance(item, dict)
                ],
                execution_unknown_tool_call_id=raw_result.get("execution_unknown_tool_call_id"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ApplicationError(
                "committed step result is malformed",
                type="step_result_malformed",
                non_retryable=True,
            ) from error
        if result.execution_unknown_tool_call_id is not None:
            raise ApplicationError(
                f"tool call {result.execution_unknown_tool_call_id} execution outcome is "
                "unknown; manual reconciliation is required",
                type="tool_execution_unknown",
                non_retryable=True,
            )
        return result

    async def _load_reasoning_and_manifest(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
        step_index: int,
    ) -> tuple[tuple[ManifestCall, ...], AgentStepReasoningRecord]:
        manifest_event = await load_step_event(
            session,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            step_index=step_index,
            event_type="agent.step.tool_manifest",
        )
        if manifest_event is None:
            raise ApplicationError(
                "agent step tool manifest is missing",
                type="tool_step_manifest_missing",
                non_retryable=True,
            )
        calls = manifest_calls_from_payload(
            manifest_event.payload_json,
            expected_step=step_index,
        )
        reasoning_event = await load_step_event(
            session,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            step_index=step_index,
            event_type="agent.step.reasoning",
        )
        if reasoning_event is None:
            raise ApplicationError(
                "agent step reasoning sidecar is missing",
                type="reasoning_sidecar_missing",
                non_retryable=True,
            )
        reasoning = AgentStepReasoningRecord.from_payload(
            reasoning_event.payload_json,
            expected_step=step_index,
            expected_call_count=len(calls),
        )
        return calls, reasoning

    async def _approval_for_row(
        self,
        session: AsyncSession,
        row: ToolCall,
        *,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
        agent_id: UUID,
    ) -> Approval | None:
        if row.approval_id is None:
            return None
        approval = await session.scalar(
            select(Approval).where(
                Approval.id == row.approval_id,
                Approval.workspace_id == workspace_id,
                Approval.task_id == task_id,
                Approval.run_id == run_id,
                Approval.requested_by_agent_id == agent_id,
            )
        )
        if approval is None:
            raise ApplicationError(
                "tool call approval binding is missing",
                type="tool_projection_binding_mismatch",
                non_retryable=True,
            )
        return approval

    async def _projected_outcome(
        self,
        session: AsyncSession,
        *,
        row: ToolCall,
        manifest: ManifestCall,
        provider_call_id: str,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
        agent_id: UUID,
    ) -> _ProjectedToolOutcome:
        if (
            row.workspace_id != workspace_id
            or row.run_id != run_id
            or row.agent_id != agent_id
            or row.tool_name != manifest.tool_name
        ):
            raise ApplicationError(
                "tool call does not match its canonical manifest entry",
                type="tool_projection_binding_mismatch",
                non_retryable=True,
            )
        status = _TOOL_STATUS_MAP.get(row.status)
        if status is None:
            raise ApplicationError(
                "tool call is not ready for projection",
                type="tool_projection_incomplete",
                non_retryable=True,
            )
        approval = await self._approval_for_row(
            session,
            row,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
        )
        if status == "needs_approval" and approval is None:
            raise ApplicationError(
                "pending tool call has no matching approval evidence",
                type="tool_projection_binding_mismatch",
                non_retryable=True,
            )
        payload = approval.action_payload_sanitized if approval is not None else {}
        risk_value = payload.get("risk")
        risk = (
            redact_text(risk_value)[:_MAX_PROVIDER_TEXT_CHARS]
            if isinstance(risk_value, str)
            else None
        )
        if approval is not None:
            reason = redact_text(approval.reason)[:_MAX_REASON_CHARS]
        else:
            reason = _SAFE_STATUS_REASONS[status]
            # Denials and pre-effect failures carry the gateway's bounded
            # reason / the connector's static retry hint on the row; surface
            # it so the model can correct its call instead of guessing.
            if status in ("failed", "denied"):
                guidance = row.sanitized_output_json.get("hint") or row.sanitized_output_json.get(
                    "reason"
                )
                if isinstance(guidance, str) and guidance:
                    reason = f"{reason}: {redact_text(guidance)[:_MAX_REASON_CHARS]}"
        if status == "needs_approval":
            decision_code = "approval_required"
        elif status == "executed":
            decision_code = "granted"
        else:
            decision_code = row.error_code or status
        return _ProjectedToolOutcome(
            row=row,
            manifest=manifest,
            provider_call_id=provider_call_id,
            status=status,
            decision_code=decision_code,
            decision_reason=reason,
            risk=risk,
            approval=approval,
        )

    def _record_gateway_result(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID,
        seq: int,
        step_index: int,
        result: _ProjectedToolOutcome,
    ) -> int:
        base: dict[str, Any] = {
            "step": step_index,
            "tool_name": result.manifest.tool_name,
            "tool_call_id": str(result.row.id),
            "risk": result.risk,
        }

        def emit(event_type: str, extra: dict[str, Any]) -> None:
            nonlocal seq
            self._add_run_event(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
                event_type=event_type,
                payload={**base, **extra},
            )
            seq += 1

        emit("node.policy_check", {"decision": result.decision_code})
        if result.status in ("executed", "failed"):
            emit(
                "node.execute_tool",
                {"status": result.status, "duration_ms": result.row.duration_ms},
            )
            output = result.row.sanitized_output_json
            if result.manifest.tool_name.startswith("cli.") and output:
                default_status = "completed" if result.status == "executed" else "failed"
                emit(
                    "sandbox.job",
                    {
                        "sandbox_job_id": output.get("sandbox_job_id"),
                        "command": output.get("command"),
                        "job_status": output.get("status", default_status),
                        "exit_code": output.get("exit_code"),
                        "job_duration_ms": output.get("duration_ms"),
                        "stdout": output.get("stdout", ""),
                        "stderr": output.get("stderr", ""),
                    },
                )
            emit("node.observe", {"chars": len(result.observation_json())})
        elif result.status in ("denied", "rejected"):
            emit("node.observe", {"denied": True, "reason": result.decision_reason})
        elif result.status == "execution_unknown":
            emit(
                "node.observe",
                {"execution_unknown": True, "manual_reconciliation_required": True},
            )
        elif result.status == "needs_approval":
            emit(
                "node.request_approval",
                {
                    "approval_id": (
                        str(result.approval.id) if result.approval is not None else None
                    ),
                    "reason": result.decision_reason,
                },
            )
        elif result.status == "needs_review":
            emit(
                "node.request_review",
                {
                    "review_id": str(result.row.review_id) if result.row.review_id else None,
                    "reason": result.decision_reason,
                },
            )
        emit(
            "tool.call",
            {
                "status": result.status,
                "decision": result.decision_code,
                "reason": result.decision_reason,
                "error_code": result.row.error_code,
                "duration_ms": result.row.duration_ms,
                "approval_id": (str(result.approval.id) if result.approval is not None else None),
            },
        )
        return seq

    @staticmethod
    def _delegation_request(result: _ProjectedToolOutcome) -> DelegationRequest | None:
        if result.status != "executed" or result.manifest.tool_name != "organization.delegate_task":
            return None
        output = result.row.sanitized_output_json
        child_task_id = str(output.get("child_task_id", "") or "")
        if not child_task_id:
            return None
        return DelegationRequest(
            child_task_id=child_task_id,
            target_agent_id=str(output.get("target_agent_id", "") or ""),
            blocking=bool(output.get("blocking", True)),
            kind=str(output.get("kind", "") or "delegation"),
            provider_call_id=result.provider_call_id,
            gateway_tool_call_id=str(result.row.id),
        )

    @staticmethod
    def _review_decision(result: _ProjectedToolOutcome) -> ReviewDecisionSignal | None:
        """An executed ``organization.review.submit`` decided a review; the
        workflow signals the source task so a parked run resumes."""
        if result.status != "executed" or result.manifest.tool_name != "organization.review.submit":
            return None
        return review_decision_from_output(result.row.sanitized_output_json)

    @staticmethod
    def _work_request_start(result: _ProjectedToolOutcome) -> WorkRequestStart | None:
        """An executed ``organization.respond_work_request`` accept created
        the task row; the workflow starts its WorkRequestTaskWorkflow."""
        if (
            result.status != "executed"
            or result.manifest.tool_name != "organization.respond_work_request"
        ):
            return None
        return work_request_start_from_output(result.row.sanitized_output_json)

    @activity.defn(name=ACTIVITY_COMMIT_AGENT_STEP)
    async def commit_agent_step_activity(self, params: CommitAgentStepInput) -> StepResult:
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        agent_id = UUID(params.agent_id)

        waiting_approval_id: str | None = None
        waiting_review_id: str | None = None
        blocking_delegation: DelegationRequest | None = None
        projected: list[_ProjectedToolOutcome] = []
        async with self._resources.session_factory() as session:
            with session.no_autoflush:
                run = await session.scalar(
                    select(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.workspace_id == workspace_id,
                        AgentRun.task_id == task_id,
                        AgentRun.agent_id == agent_id,
                    )
                    .with_for_update()
                )
                committed = await self._committed_step_result(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    step_index=params.step_index,
                    gateway_tool_call_ids=params.gateway_tool_call_ids,
                    cancelled_after_tool_call_id=params.cancelled_after_tool_call_id,
                )
            if committed is not None:
                await session.rollback()
                return committed
            if run is None:
                raise ApplicationError(
                    "agent run not found for step projection",
                    type="run_not_found",
                    non_retryable=True,
                )
            calls, reasoning = await self._load_reasoning_and_manifest(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                step_index=params.step_index,
            )
            expected_ids = [
                str(stable_tool_invocation_id(run_id, params.step_index, ordinal))
                for ordinal in range(len(calls))
            ]
            if params.gateway_tool_call_ids != expected_ids[: len(params.gateway_tool_call_ids)]:
                raise ApplicationError(
                    "step projection tool IDs do not match the canonical executed prefix",
                    type="tool_projection_binding_mismatch",
                    non_retryable=True,
                )
            if len(params.gateway_tool_call_ids) > len(calls):
                raise ApplicationError(
                    "step projection has more tool IDs than its canonical manifest",
                    type="tool_projection_binding_mismatch",
                    non_retryable=True,
                )
            if params.cancelled_after_tool_call_id is not None and (
                not params.gateway_tool_call_ids
                or len(params.gateway_tool_call_ids) >= len(calls)
                or params.cancelled_after_tool_call_id != params.gateway_tool_call_ids[-1]
            ):
                raise ApplicationError(
                    "cancellation truncation does not bind the exact canonical prefix",
                    type="tool_projection_binding_mismatch",
                    non_retryable=True,
                )

            for ordinal, raw_id in enumerate(params.gateway_tool_call_ids):
                row = await session.get(ToolCall, UUID(raw_id))
                if row is None:
                    raise ApplicationError(
                        "step projection tool call is missing",
                        type="tool_projection_binding_mismatch",
                        non_retryable=True,
                    )
                projected.append(
                    await self._projected_outcome(
                        session,
                        row=row,
                        manifest=calls[ordinal],
                        provider_call_id=reasoning.provider_call_ids[ordinal],
                        workspace_id=workspace_id,
                        task_id=task_id,
                        run_id=run_id,
                        agent_id=agent_id,
                    )
                )

            for earlier in projected[:-1]:
                delegation = self._delegation_request(earlier)
                if earlier.status in {"needs_approval", "needs_review", "execution_unknown"} or (
                    delegation is not None and delegation.blocking
                ):
                    raise ApplicationError(
                        "step projection contains a tool outcome after a durable stop",
                        type="tool_projection_binding_mismatch",
                        non_retryable=True,
                    )

            if params.cancelled_after_tool_call_id is not None:
                final = projected[-1]
                delegation = self._delegation_request(final)
                if any(result.status != "executed" for result in projected) or (
                    delegation is not None and delegation.blocking
                ):
                    raise ApplicationError(
                        "cancellation truncation requires an ordinary executed prefix",
                        type="tool_projection_binding_mismatch",
                        non_retryable=True,
                    )
                for omitted_id in expected_ids[len(projected) :]:
                    if await session.get(ToolCall, UUID(omitted_id)) is not None:
                        raise ApplicationError(
                            "cancellation truncation would hide a later tool effect",
                            type="tool_projection_binding_mismatch",
                            non_retryable=True,
                        )

            if len(projected) < len(calls):
                if not projected:
                    raise ApplicationError(
                        "step projection stopped without a durable tool outcome",
                        type="tool_projection_incomplete",
                        non_retryable=True,
                    )
                final = projected[-1]
                delegation = self._delegation_request(final)
                permitted_stop = (
                    final.status in {"needs_approval", "needs_review", "execution_unknown"}
                    or (delegation is not None and delegation.blocking)
                    or params.cancelled_after_tool_call_id is not None
                )
                if not permitted_stop:
                    raise ApplicationError(
                        "step projection omitted a canonical tool outcome",
                        type="tool_projection_incomplete",
                        non_retryable=True,
                    )

            delegations: list[DelegationRequest] = []
            work_request_starts: list[WorkRequestStart] = []
            review_decisions: list[ReviewDecisionSignal] = []
            for result in projected:
                delegation = self._delegation_request(result)
                if delegation is not None:
                    delegations.append(delegation)
                    if delegation.blocking:
                        blocking_delegation = delegation
                work_request = self._work_request_start(result)
                if work_request is not None:
                    work_request_starts.append(work_request)
                decided_review = self._review_decision(result)
                if decided_review is not None:
                    review_decisions.append(decided_review)
                if result.status == "needs_approval" and result.approval is not None:
                    waiting_approval_id = str(result.approval.id)
                if result.status == "needs_review":
                    if result.row.review_id is None:
                        raise ApplicationError(
                            "pending tool call has no matching review evidence",
                            type="tool_projection_binding_mismatch",
                            non_retryable=True,
                        )
                    waiting_review_id = str(result.row.review_id)

            execution_unknown_tool_call_id = next(
                (
                    str(result.row.id)
                    for result in projected
                    if result.status == "execution_unknown"
                ),
                None,
            )
            step_result = StepResult(
                done=reasoning.done,
                input_tokens=reasoning.usage.input_tokens,
                output_tokens=reasoning.usage.output_tokens,
                cached_tokens=reasoning.usage.cached_tokens,
                cost_micros=reasoning.usage.cost_micros,
                waiting_approval_id=waiting_approval_id,
                waiting_review_id=waiting_review_id,
                delegations=delegations,
                work_request_starts=work_request_starts,
                review_decisions=review_decisions,
                execution_unknown_tool_call_id=execution_unknown_tool_call_id,
            )

            run.input_tokens += reasoning.usage.input_tokens
            run.output_tokens += reasoning.usage.output_tokens
            run.cached_tokens += reasoning.usage.cached_tokens
            run.estimated_cost_micros += reasoning.usage.cost_micros
            run.steps_used = params.step_index + 1
            if waiting_approval_id is not None:
                run.status = RunStatus.WAITING_APPROVAL.value
            elif waiting_review_id is not None:
                run.status = RunStatus.WAITING_REVIEW.value
            elif blocking_delegation is not None:
                run.status = RunStatus.WAITING_DELEGATION.value
            if execution_unknown_tool_call_id is not None:
                run.status = RunStatus.FAILED.value
                run.error_code = "tool_execution_unknown"
                run.error_message = (
                    f"tool call {execution_unknown_tool_call_id} execution outcome is unknown; "
                    "manual reconciliation is required"
                )

            if not calls:
                session.add(
                    Message(
                        workspace_id=workspace_id,
                        task_id=task_id,
                        run_id=run_id,
                        sender_type=SenderType.AGENT.value,
                        sender_id=agent_id,
                        recipient_type=RecipientType.TASK.value,
                        recipient_id=task_id,
                        message_type="text",
                        content_json={
                            "text": reasoning.completion_sanitized,
                            "finish_reason": reasoning.finish_reason,
                        },
                        visibility=MessageVisibility.VISIBLE.value,
                    )
                )

            seq = await self._next_seq(session, run_id)
            for transition in reasoning.transitions:
                node = transition.get("node")
                detail = transition.get("detail")
                if node not in _VALID_TRANSITION_NODES or not isinstance(detail, str):
                    raise ApplicationError(
                        "agent step reasoning transition is malformed",
                        type="reasoning_sidecar_invalid",
                        non_retryable=True,
                    )
                payload: dict[str, Any] = {
                    "detail": detail,
                    "step": params.step_index,
                }
                if node == "reason":
                    payload.update(
                        {
                            "model": reasoning.model,
                            "input_tokens": reasoning.usage.input_tokens,
                            "output_tokens": reasoning.usage.output_tokens,
                            "cached_tokens": reasoning.usage.cached_tokens,
                            "cost_micros": reasoning.usage.cost_micros,
                            "latency_ms": reasoning.latency_ms,
                        }
                    )
                self._add_run_event(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    seq=seq,
                    event_type=f"node.{node}",
                    payload=payload,
                )
                seq += 1

            for result in projected:
                self._add_tool_message(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    message_type="tool_call",
                    content={
                        "text": reasoning.completion_sanitized,
                        "tool_call_id": str(result.row.id),
                        "provider_call_id": result.provider_call_id,
                        "tool_name": result.manifest.tool_name,
                        "arguments_json": json.dumps(
                            result.row.sanitized_input_json,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )[:_MAX_ARGUMENTS_CHARS],
                    },
                )
                seq = self._record_gateway_result(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    seq=seq,
                    step_index=params.step_index,
                    result=result,
                )
                delegation = self._delegation_request(result)
                if result.status in ("needs_approval", "needs_review") or (
                    delegation is not None and delegation.blocking
                ):
                    continue
                self._add_tool_message(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    message_type="tool_result",
                    content={
                        "tool_call_id": str(result.row.id),
                        "provider_call_id": result.provider_call_id,
                        "tool_name": result.manifest.tool_name,
                        "status": result.status,
                        "result": result.observation_json(),
                    },
                )

            self._add_run_event(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
                event_type="agent.step.committed",
                payload={
                    "step": params.step_index,
                    "result": asdict(step_result),
                    "gateway_tool_call_ids": [str(result.row.id) for result in projected],
                    "cancelled_after_tool_call_id": params.cancelled_after_tool_call_id,
                },
            )
            await session.commit()

        if execution_unknown_tool_call_id is not None:
            raise ApplicationError(
                f"tool call {execution_unknown_tool_call_id} execution outcome is unknown; "
                "manual reconciliation is required",
                type="tool_execution_unknown",
                non_retryable=True,
            )
        if waiting_approval_id is not None:
            parked = projected[-1]
            await self._publish(
                workspace_id,
                "approval.requested",
                {
                    "approval_id": waiting_approval_id,
                    "run_id": params.run_id,
                    "task_id": params.task_id,
                    "agent_id": params.agent_id,
                    "tool_name": parked.manifest.tool_name,
                    "risk": parked.risk,
                },
            )
            await self._publish(
                workspace_id,
                "agent.run.waiting_approval",
                {
                    "run_id": params.run_id,
                    "task_id": params.task_id,
                    "approval_id": waiting_approval_id,
                },
            )
        if waiting_review_id is not None:
            parked = projected[-1]
            await self._publish(
                workspace_id,
                "agent.run.waiting_review",
                {
                    "run_id": params.run_id,
                    "task_id": params.task_id,
                    "agent_id": params.agent_id,
                    "review_id": waiting_review_id,
                    "tool_name": parked.manifest.tool_name,
                    "risk": parked.risk,
                },
            )
        if blocking_delegation is not None:
            await self._publish(
                workspace_id,
                "agent.run.waiting_delegation",
                {
                    "run_id": params.run_id,
                    "task_id": params.task_id,
                    "child_task_id": blocking_delegation.child_task_id,
                    "target_agent_id": blocking_delegation.target_agent_id,
                },
            )
        await self._publish(
            workspace_id,
            "agent.run.step",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "step": params.step_index,
                "done": step_result.done,
            },
        )
        return step_result

    async def _approval_manifest_binding(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
        tool_call_id: UUID,
    ) -> tuple[int, ManifestCall]:
        events = list(
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.workspace_id == workspace_id,
                    RunEvent.task_id == task_id,
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "agent.step.tool_manifest",
                )
            )
        )
        matches: list[tuple[int, ManifestCall]] = []
        for event in events:
            step = event.payload_json.get("step")
            if not isinstance(step, int) or isinstance(step, bool) or step < 0:
                continue
            calls = manifest_calls_from_payload(event.payload_json, expected_step=step)
            for call in calls:
                if stable_tool_invocation_id(run_id, step, call.ordinal) == tool_call_id:
                    matches.append((step, call))
        if len(matches) != 1:
            raise ApplicationError(
                "approval projection has no unique canonical manifest binding",
                type="tool_projection_binding_mismatch",
                non_retryable=True,
            )
        return matches[0]

    @activity.defn(name=ACTIVITY_COMMIT_APPROVAL_PROJECTION)
    async def commit_approval_projection_activity(
        self,
        params: CommitApprovalProjectionInput,
    ) -> StepResult:
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        agent_id = UUID(params.agent_id)
        approval_id = UUID(params.approval_id)
        tool_call_id = UUID(params.tool_call_id)

        async with self._resources.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.task_id == task_id,
                    AgentRun.agent_id == agent_id,
                )
                .with_for_update()
            )
            if run is None:
                raise ApplicationError(
                    "agent run not found for approval projection",
                    type="run_not_found",
                    non_retryable=True,
                )
            approval = await session.scalar(
                select(Approval).where(
                    Approval.id == approval_id,
                    Approval.workspace_id == workspace_id,
                    Approval.task_id == task_id,
                    Approval.run_id == run_id,
                    Approval.requested_by_agent_id == agent_id,
                )
            )
            if approval is None:
                raise ApplicationError(
                    "approval not found",
                    type="approval_not_found",
                    non_retryable=True,
                )
            if approval.status == ApprovalStatus.PENDING.value:
                raise ApplicationError("approval still pending", type="approval_pending")
            if approval.status == ApprovalStatus.CANCELLED.value:
                raise ApplicationError(
                    "cancelled approval cannot resume an agent run",
                    type="approval_cancelled",
                    non_retryable=True,
                )
            if run.status in {
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            }:
                if run.error_code == "tool_execution_unknown":
                    raise ApplicationError(
                        run.error_message
                        or "tool execution outcome is unknown; manual reconciliation is required",
                        type="tool_execution_unknown",
                        non_retryable=True,
                    )
                raise ApplicationError(
                    "approval cannot resume an already-terminal agent run",
                    type="run_already_terminal",
                    non_retryable=True,
                )
            row = await session.get(ToolCall, tool_call_id)
            if row is None or row.approval_id != approval_id:
                raise ApplicationError(
                    "approval tool call binding does not match",
                    type="tool_projection_binding_mismatch",
                    non_retryable=True,
                )
            step_index, manifest = await self._approval_manifest_binding(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
            reasoning_event = await load_step_event(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                step_index=step_index,
                event_type="agent.step.reasoning",
            )
            if reasoning_event is None:
                raw_provider_id = approval.action_payload_sanitized.get("provider_call_id")
                if (
                    not isinstance(raw_provider_id, str)
                    or not raw_provider_id
                    or len(raw_provider_id) > _MAX_PROVIDER_TEXT_CHARS
                    or redact_text(raw_provider_id) != raw_provider_id
                ):
                    raise ApplicationError(
                        "agent step reasoning sidecar is missing",
                        type="reasoning_sidecar_missing",
                        non_retryable=True,
                    )
                provider_call_id = raw_provider_id
            else:
                manifest_event = await load_step_event(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    step_index=step_index,
                    event_type="agent.step.tool_manifest",
                )
                if manifest_event is None:
                    raise ApplicationError(
                        "agent step tool manifest is missing",
                        type="tool_step_manifest_missing",
                        non_retryable=True,
                    )
                calls = manifest_calls_from_payload(
                    manifest_event.payload_json,
                    expected_step=step_index,
                )
                reasoning = AgentStepReasoningRecord.from_payload(
                    reasoning_event.payload_json,
                    expected_step=step_index,
                    expected_call_count=len(calls),
                )
                provider_call_id = reasoning.provider_call_ids[manifest.ordinal]

            result = await self._projected_outcome(
                session,
                row=row,
                manifest=manifest,
                provider_call_id=provider_call_id,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
            )
            if result.status == "needs_approval":
                raise ApplicationError(
                    "approval tool call is still pending",
                    type="approval_pending",
                )

            existing_results = list(
                await session.scalars(
                    select(Message).where(
                        Message.workspace_id == workspace_id,
                        Message.task_id == task_id,
                        Message.run_id == run_id,
                        Message.message_type == "tool_result",
                    )
                )
            )
            bundle_exists = any(
                message.content_json.get("tool_call_id") == str(tool_call_id)
                for message in existing_results
            )
            if bundle_exists:
                if result.status == "execution_unknown":
                    run.status = RunStatus.FAILED.value
                    run.error_code = "tool_execution_unknown"
                    run.error_message = (
                        f"tool call {tool_call_id} execution outcome is unknown; "
                        "manual reconciliation is required"
                    )
                    await session.commit()
                    raise ApplicationError(
                        run.error_message,
                        type="tool_execution_unknown",
                        non_retryable=True,
                    )
                await session.rollback()
                return StepResult(done=False)

            self._add_tool_message(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
                message_type="tool_result",
                content={
                    "tool_call_id": str(tool_call_id),
                    "provider_call_id": provider_call_id,
                    "approval_id": params.approval_id,
                    "tool_name": manifest.tool_name,
                    "status": result.status,
                    "result": result.observation_json(),
                },
            )
            if result.status == "execution_unknown":
                run.status = RunStatus.FAILED.value
                run.error_code = "tool_execution_unknown"
                run.error_message = (
                    f"tool call {tool_call_id} execution outcome is unknown; "
                    "manual reconciliation is required"
                )
            else:
                run.status = RunStatus.RUNNING.value

            seq = await self._next_seq(session, run_id)
            self._add_run_event(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
                event_type=f"approval.{approval.status}",
                payload={
                    "approval_id": params.approval_id,
                    "tool_name": manifest.tool_name,
                    "status": result.status,
                    "decided_by_user_id": (
                        str(approval.decided_by_user_id)
                        if approval.decided_by_user_id is not None
                        else None
                    ),
                },
            )
            seq += 1
            if result.status in ("executed", "failed"):
                self._add_run_event(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    seq=seq,
                    event_type="node.execute_tool",
                    payload={
                        "tool_name": manifest.tool_name,
                        "status": result.status,
                        "duration_ms": row.duration_ms,
                        "after_approval": True,
                    },
                )
                seq += 1
            self._add_run_event(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
                event_type="tool.call",
                payload={
                    "tool_name": manifest.tool_name,
                    "tool_call_id": str(tool_call_id),
                    "risk": result.risk,
                    "status": result.status,
                    "decision": result.decision_code,
                    "reason": result.decision_reason,
                    "error_code": row.error_code,
                    "duration_ms": row.duration_ms,
                    "approval_id": params.approval_id,
                },
            )
            await session.commit()

        if result.status == "execution_unknown":
            raise ApplicationError(
                f"tool call {tool_call_id} execution outcome is unknown; "
                "manual reconciliation is required",
                type="tool_execution_unknown",
                non_retryable=True,
            )
        await self._publish(
            workspace_id,
            "agent.run.resumed",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "approval_id": params.approval_id,
                "decision": approval.status,
                "tool_status": result.status,
            },
        )
        return StepResult(done=False)

    @activity.defn(name=ACTIVITY_COMMIT_REVIEW_PROJECTION)
    async def commit_review_projection_activity(
        self,
        params: CommitReviewProjectionInput,
    ) -> StepResult:
        """Project the resumed review-parked call (mirrors the approval
        projection). Idempotent on the ``review.<status>`` run event for this
        review: a retry after a crash replays the durable row state."""
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        agent_id = UUID(params.agent_id)
        review_id = UUID(params.review_id)
        tool_call_id = UUID(params.tool_call_id)

        async with self._resources.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.task_id == task_id,
                    AgentRun.agent_id == agent_id,
                )
                .with_for_update()
            )
            if run is None:
                raise ApplicationError(
                    "agent run not found for review projection",
                    type="run_not_found",
                    non_retryable=True,
                )
            review = await session.scalar(
                select(WorkReview).where(
                    WorkReview.id == review_id,
                    WorkReview.workspace_id == workspace_id,
                    WorkReview.run_id == run_id,
                    WorkReview.subject_agent_id == agent_id,
                )
            )
            if review is None:
                raise ApplicationError(
                    "review not found", type="review_not_found", non_retryable=True
                )
            if review.status == WorkReviewStatus.PENDING.value:
                raise ApplicationError("review still pending", type="review_pending")
            if run.status in {
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            }:
                if run.error_code == "tool_execution_unknown":
                    raise ApplicationError(
                        run.error_message
                        or "tool execution outcome is unknown; manual reconciliation is required",
                        type="tool_execution_unknown",
                        non_retryable=True,
                    )
                raise ApplicationError(
                    "review cannot resume an already-terminal agent run",
                    type="run_already_terminal",
                    non_retryable=True,
                )
            row = await session.get(ToolCall, tool_call_id)
            if row is None or row.run_id != run_id or row.agent_id != agent_id:
                raise ApplicationError(
                    "review tool call binding does not match",
                    type="tool_projection_binding_mismatch",
                    non_retryable=True,
                )
            step_index, manifest = await self._approval_manifest_binding(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
            reasoning_event = await load_step_event(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                step_index=step_index,
                event_type="agent.step.reasoning",
            )
            if reasoning_event is None:
                raise ApplicationError(
                    "agent step reasoning sidecar is missing",
                    type="reasoning_sidecar_missing",
                    non_retryable=True,
                )
            manifest_event = await load_step_event(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                step_index=step_index,
                event_type="agent.step.tool_manifest",
            )
            if manifest_event is None:
                raise ApplicationError(
                    "agent step tool manifest is missing",
                    type="tool_step_manifest_missing",
                    non_retryable=True,
                )
            calls = manifest_calls_from_payload(
                manifest_event.payload_json, expected_step=step_index
            )
            reasoning = AgentStepReasoningRecord.from_payload(
                reasoning_event.payload_json,
                expected_step=step_index,
                expected_call_count=len(calls),
            )
            provider_call_id = reasoning.provider_call_ids[manifest.ordinal]

            result = await self._projected_outcome(
                session,
                row=row,
                manifest=manifest,
                provider_call_id=provider_call_id,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
            )
            if result.status == "needs_review":
                if row.review_id == review_id:
                    raise ApplicationError(
                        "review tool call is still parked", type="review_pending"
                    )
                # Another matched policy is still pending: park again.
                run.status = RunStatus.WAITING_REVIEW.value
                await session.commit()
                return StepResult(done=False, waiting_review_id=str(row.review_id))

            bundle_exists = (
                await session.scalar(
                    select(func.count(RunEvent.id)).where(
                        RunEvent.workspace_id == workspace_id,
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == f"review.{review.status}",
                        RunEvent.payload_json["review_id"].as_string() == params.review_id,
                    )
                )
                or 0
            ) > 0
            if bundle_exists:
                if result.status == "execution_unknown":
                    run.status = RunStatus.FAILED.value
                    run.error_code = "tool_execution_unknown"
                    run.error_message = (
                        f"tool call {tool_call_id} execution outcome is unknown; "
                        "manual reconciliation is required"
                    )
                    await session.commit()
                    raise ApplicationError(
                        run.error_message,
                        type="tool_execution_unknown",
                        non_retryable=True,
                    )
                replayed_approval_id = (
                    str(result.approval.id)
                    if result.status == "needs_approval" and result.approval is not None
                    else None
                )
                await session.rollback()
                return StepResult(done=False, waiting_approval_id=replayed_approval_id)

            seq = await self._next_seq(session, run_id)
            self._add_run_event(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
                event_type=f"review.{review.status}",
                payload={
                    "review_id": params.review_id,
                    "tool_call_id": str(tool_call_id),
                    "tool_name": manifest.tool_name,
                    "status": result.status,
                    "verdict": review.verdict,
                    "feedback": redact_text(review.feedback)[:_MAX_REASON_CHARS],
                    "decided_by_user_id": (
                        str(review.decided_by_user_id)
                        if review.decided_by_user_id is not None
                        else None
                    ),
                    "decided_by_agent_id": (
                        str(review.decided_by_agent_id)
                        if review.decided_by_agent_id is not None
                        else None
                    ),
                },
            )
            seq += 1
            waiting_approval_id: str | None = None
            if result.status == "needs_approval" and result.approval is not None:
                # The approved review let the call advance into the ordinary
                # human-approval wait; the workflow parks on that next.
                waiting_approval_id = str(result.approval.id)
                self._add_run_event(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    seq=seq,
                    event_type="node.request_approval",
                    payload={
                        "step": step_index,
                        "tool_name": manifest.tool_name,
                        "tool_call_id": str(tool_call_id),
                        "approval_id": waiting_approval_id,
                        "reason": result.decision_reason,
                        "after_review": True,
                    },
                )
                seq += 1
                run.status = RunStatus.WAITING_APPROVAL.value
            else:
                self._add_tool_message(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    message_type="tool_result",
                    content={
                        "tool_call_id": str(tool_call_id),
                        "provider_call_id": provider_call_id,
                        "review_id": params.review_id,
                        "tool_name": manifest.tool_name,
                        "status": result.status,
                        "result": result.observation_json(),
                    },
                )
                if result.status == "execution_unknown":
                    run.status = RunStatus.FAILED.value
                    run.error_code = "tool_execution_unknown"
                    run.error_message = (
                        f"tool call {tool_call_id} execution outcome is unknown; "
                        "manual reconciliation is required"
                    )
                else:
                    run.status = RunStatus.RUNNING.value
                if result.status in ("executed", "failed"):
                    self._add_run_event(
                        session,
                        workspace_id=workspace_id,
                        run_id=run_id,
                        task_id=task_id,
                        seq=seq,
                        event_type="node.execute_tool",
                        payload={
                            "tool_name": manifest.tool_name,
                            "status": result.status,
                            "duration_ms": row.duration_ms,
                            "after_review": True,
                        },
                    )
                    seq += 1
            self._add_run_event(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
                event_type="tool.call",
                payload={
                    "tool_name": manifest.tool_name,
                    "tool_call_id": str(tool_call_id),
                    "risk": result.risk,
                    "status": result.status,
                    "decision": result.decision_code,
                    "reason": result.decision_reason,
                    "error_code": row.error_code,
                    "duration_ms": row.duration_ms,
                    "approval_id": waiting_approval_id,
                    "review_id": params.review_id,
                },
            )
            await session.commit()

        if result.status == "execution_unknown":
            raise ApplicationError(
                f"tool call {tool_call_id} execution outcome is unknown; "
                "manual reconciliation is required",
                type="tool_execution_unknown",
                non_retryable=True,
            )
        if waiting_approval_id is not None:
            await self._publish(
                workspace_id,
                "approval.requested",
                {
                    "approval_id": waiting_approval_id,
                    "run_id": params.run_id,
                    "task_id": params.task_id,
                    "agent_id": params.agent_id,
                    "tool_name": manifest.tool_name,
                    "risk": result.risk,
                },
            )
            await self._publish(
                workspace_id,
                "agent.run.waiting_approval",
                {
                    "run_id": params.run_id,
                    "task_id": params.task_id,
                    "approval_id": waiting_approval_id,
                },
            )
            return StepResult(done=False, waiting_approval_id=waiting_approval_id)
        await self._publish(
            workspace_id,
            "agent.run.resumed",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "review_id": params.review_id,
                "decision": review.status,
                "tool_status": result.status,
            },
        )
        return StepResult(done=False)

    async def _kick_queued(self, workspace_id: UUID, agent_id: UUID | None) -> None:
        if self._temporal_client is None:
            return
        async with self._resources.session_factory() as session:
            rows = (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.workspace_id == workspace_id,
                        Task.state == TaskState.QUEUED.value,
                        Task.temporal_workflow_id.isnot(None),
                    )
                    .order_by(Task.created_at)
                    .limit(20)
                )
            ).all()
        targets: list[str] = []
        for row in rows:
            if agent_id is not None and row.assigned_agent_id == agent_id:
                if row.temporal_workflow_id is not None:
                    targets.append(row.temporal_workflow_id)
                break
        if (
            rows
            and rows[0].temporal_workflow_id is not None
            and rows[0].temporal_workflow_id not in targets
        ):
            targets.append(rows[0].temporal_workflow_id)
        for workflow_id in targets:
            try:
                await self._temporal_client.get_workflow_handle(workflow_id).signal(
                    "slot_available"
                )
            except Exception as error:
                logger.warning(
                    "concurrency.kick_failed",
                    error_type=type(error).__name__,
                )

    @activity.defn(name=ACTIVITY_FINALIZE_RUN_PROJECTION)
    async def finalize_run_projection_activity(self, params: FinalizeInput) -> None:
        normalized_outcome = _prevalidate_finalization_telemetry(params.status)
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        error_message = redact_text(params.error_message) if params.error_message else None
        effective_error_code = params.error_code
        effective_error_message = error_message
        run_totals: dict[str, Any] = {}
        freed_agent_id: UUID | None = None
        owns_run_metrics = False
        persisted_started_at: object = None
        persisted_completed_at: object = None
        persisted_status = params.status
        persisted_error_code: str | None = None
        task_conversation_id: UUID | None = None

        async with self._resources.session_factory() as session:
            if params.run_id is not None:
                run_id = UUID(params.run_id)
                run = await session.scalar(
                    select(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.workspace_id == workspace_id,
                        AgentRun.task_id == task_id,
                    )
                    .with_for_update()
                )
                if run is None:
                    raise ApplicationError(
                        "agent run not found for final projection",
                        type="run_not_found",
                        non_retryable=True,
                    )
                if run.completed_at is not None:
                    await session.rollback()
                    return
                await _cancel_pending_run_approvals(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                )
                freed_agent_id = run.agent_id
                if run.error_code in _PRESERVED_FINAL_ERRORS:
                    effective_error_code = run.error_code
                    effective_error_message = run.error_message
                persisted_started_at = run.started_at
                run.status = params.status
                completed_at = datetime.now(UTC)
                run.completed_at = completed_at
                run.steps_used = max(run.steps_used, params.steps_used)
                run.error_code = effective_error_code
                run.error_message = effective_error_message
                owns_run_metrics = True
                persisted_completed_at = completed_at
                persisted_status = run.status
                persisted_error_code = run.error_code
                run_totals = {
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "cost_micros": run.estimated_cost_micros,
                }
                seq = await self._next_seq(session, run.id)
                self._add_run_event(
                    session,
                    workspace_id=workspace_id,
                    run_id=run.id,
                    task_id=task_id,
                    seq=seq,
                    event_type=f"run.{params.status}",
                    payload={
                        **run_totals,
                        "steps_used": params.steps_used,
                        "error_code": effective_error_code,
                        "error_message": effective_error_message,
                    },
                )

            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is not None:
                task.state = params.status
                task_conversation_id = task.conversation_id
                if effective_error_message:
                    session.add(
                        Message(
                            workspace_id=workspace_id,
                            task_id=task_id,
                            run_id=UUID(params.run_id) if params.run_id else None,
                            sender_type=SenderType.SYSTEM.value,
                            sender_id=None,
                            recipient_type=RecipientType.TASK.value,
                            recipient_id=task_id,
                            message_type="error",
                            content_json={
                                "text": f"Run {params.status}: {effective_error_message}",
                                "error_code": effective_error_code,
                            },
                            visibility=MessageVisibility.VISIBLE.value,
                        )
                    )
            await session.commit()

        if owns_run_metrics:
            _record_agent_counter(
                self._metrics,
                name=_AGENT_RUNS_METRIC,
                amount=1,
                labels={
                    _AGENT_SERVICE_LABEL: _AGENT_SERVICE_VALUE,
                    _AGENT_OUTCOME_LABEL: normalized_outcome,
                },
            )
            duration = _persisted_duration_seconds(
                persisted_started_at,
                persisted_completed_at,
            )
            if duration is not None:
                _record_agent_histogram(
                    self._metrics,
                    name=_AGENT_DURATION_METRIC,
                    amount=duration,
                    labels={_AGENT_OUTCOME_LABEL: normalized_outcome},
                )
            failure_class = _final_failure_class(
                persisted_status,
                persisted_error_code,
            )
            if failure_class is not None:
                _record_agent_counter(
                    self._metrics,
                    name=_AGENT_FAILURES_METRIC,
                    amount=1,
                    labels={_AGENT_FAILURE_LABEL: failure_class},
                )

        await self._publish(
            workspace_id,
            f"agent.run.{params.status}",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "error_code": effective_error_code,
                **run_totals,
            },
        )
        await self._publish(
            workspace_id,
            f"task.{params.status}",
            {"task_id": params.task_id, "run_id": params.run_id},
        )
        if (
            params.run_id is not None
            and owns_run_metrics
            and freed_agent_id is not None
            and persisted_status == RunStatus.COMPLETED.value
        ):
            await self._start_memory_maintenance(
                workspace_id=workspace_id,
                agent_id=freed_agent_id,
                task_id=task_id,
                run_id=params.run_id,
                conversation_id=task_conversation_id,
            )
        if params.run_id is not None:
            await self._kick_queued(workspace_id, freed_agent_id)
        if params.run_id is not None and owns_run_metrics:
            await self._emit_budget_warning(workspace_id, freed_agent_id)

    async def _emit_budget_warning(self, workspace_id: UUID, agent_id: UUID | None) -> None:
        """One registered log line when this month's tracked spend crossed a
        budget's warning threshold (plan 15.5). Best-effort by contract: the
        terminal projection is already committed, and the attention view
        computes the same ratio live, so nothing durable is written here."""
        try:
            async with self._resources.session_factory() as session:
                workspace = await session.get(Workspace, workspace_id)
                budget_micros, threshold = workspace_budget_settings(
                    workspace.settings_json if workspace is not None else None
                )
                if budget_micros:
                    spent = await month_spend_micros(session, workspace_id)
                    if spent >= threshold * budget_micros:
                        logger.info(
                            "budget.warning",
                            scope="workspace",
                            percent_used=min(int(spent * 100 / budget_micros), 10_000),
                        )
                agent = await session.get(Agent, agent_id) if agent_id is not None else None
                if agent is not None and agent.monthly_budget_cents:
                    agent_budget = agent.monthly_budget_cents * MICROS_PER_CENT
                    spent = await month_spend_micros(session, workspace_id, agent_id=agent_id)
                    if spent >= threshold * agent_budget:
                        logger.info(
                            "budget.warning",
                            scope="agent",
                            percent_used=min(int(spent * 100 / agent_budget), 10_000),
                        )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            return

    async def _start_memory_maintenance(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        task_id: UUID,
        run_id: str,
        conversation_id: UUID | None,
    ) -> None:
        """Detached memory maintenance for a completed task
        (docs/architecture/memory.md). Best-effort by contract: the terminal
        projection is already committed, and no failure here may surface."""
        client = self._temporal_client
        if client is None:
            return
        try:
            status, _handle = await start_memory_maintenance(
                client,
                MemoryMaintenanceInput(
                    workspace_id=str(workspace_id),
                    agent_id=str(agent_id),
                    source_kind=SOURCE_KIND_TASK_OUTCOME,
                    source_id=str(task_id),
                    turn_marker=run_id,
                    task_id=str(task_id),
                    conversation_id=str(conversation_id) if conversation_id else "",
                ),
            )
        except Exception as error:
            logger.warning("memory.maintenance_start_failed", error_type=type(error).__name__)
            return
        logger.info("memory.maintenance_start", status=status, task_id=str(task_id))
