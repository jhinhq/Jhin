"""Tool-worker activities: discovery, bound execution, and approval/review
resolution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from opentelemetry import context as otel_context
from opentelemetry.context import Context
from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

import jhin_tools.telemetry as tool_telemetry
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    Connection,
    RunEvent,
    Task,
    ToolCall,
    WorkReview,
)
from jhin_domain import ApprovalStatus, ConnectionStatus, ToolCallStatus, WorkReviewStatus
from jhin_observability import (
    MetricName,  # noqa: F401 - referenced by metric literal type comments
    SafeError,
    SafeErrorCode,
    normalize_span_attributes,
    record_span_error,
    set_span_attributes,
)
from jhin_policy import Grant, GrantEffect
from jhin_tool_worker.resources import ToolWorkerResources
from jhin_tools import (
    MAX_TOOL_CALLS_PER_STEP,
    MAX_TOOL_STEP_INDEX,
    GatewayOutcome,
    GatewayStateError,
    ToolCatalog,
    ToolExecutionContext,
    ToolGateway,
    advertised_description,
    allowed_tool_definitions,
    stable_tool_invocation_id,
    task_scoped_tool_definitions,
)
from jhin_tools.ask_person import asked_question_id
from jhin_tools.telemetry import (
    ToolTelemetryDescription,
    _tool_status_authority,
    describe_tool_telemetry,
)
from jhin_workflows.agent_task.shared import (
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
    ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW,
    ORDINARY_TOOL_FAILURE_MESSAGE,
    AdvertisedTool,
    BoundToolResult,
    ExecuteBoundToolInput,
    ResolveAdvertisedToolsInput,
    ResolveBoundToolApprovalInput,
    ResolveBoundToolReviewInput,
)


@dataclass(frozen=True)
class BoundManifestEntry:
    ordinal: int
    lossless: bool
    tool_name: str
    arguments_json: str


@dataclass(frozen=True)
class _RuntimeContext:
    task_id: UUID
    agent_id: UUID
    agent_name: str


@dataclass(frozen=True)
class _ToolTelemetryAuthority:
    outcome_tool_call_id: UUID
    outcome_status: str
    outcome_tool_name: str
    outcome_sanitized_input_json: dict[str, Any]
    outcome_replayed: bool
    outcome_decision_code: str
    tool_call_id: UUID
    workspace_id: UUID
    run_id: UUID
    agent_id: UUID
    tool_name: str
    sanitized_input_json: dict[str, Any]
    task_id: UUID | None
    agent_run_id: UUID
    agent_run_workspace_id: UUID
    agent_run_agent_id: UUID
    agent_run_task_id: UUID | None
    manifest_workspace_id: UUID
    manifest_run_id: UUID
    manifest_step_index: int
    manifest_ordinal: int
    manifest_lossless: bool
    manifest_tool_name: str
    manifest_arguments_json: str
    manifest_canonical_input_json: dict[str, Any]
    derived_tool_call_id: UUID
    row_status: str
    approval_id: UUID | None
    approval_workspace_id: UUID | None
    approval_task_id: UUID | None
    approval_run_id: UUID | None
    approval_requested_by_agent_id: UUID | None
    approval_action_type: str | None
    approval_status: str | None


_TOOL_EXECUTE_SPAN_NAME = "tool.gateway.execute"
_TOOL_APPROVAL_SPAN_NAME = "tool.approval.resolve"
_TOOL_REVIEW_SPAN_NAME = "tool.review.resolve"
_TOOL_FAMILY_ATTRIBUTE = "jhin.tool_family"
_TOOL_RISK_ATTRIBUTE = "jhin.risk"
_TOOL_OUTCOME_ATTRIBUTE = "jhin.outcome"
_TOOL_CALLS_METRIC = "tool_calls_total"  # type: MetricName
_TOOL_FAILURES_METRIC = "tool_call_failures_total"  # type: MetricName
_TOOL_FAMILY_LABEL = "tool_family"
_TOOL_RISK_LABEL = "risk"
_TOOL_OUTCOME_LABEL = "outcome"
_TOOL_FAILURE_LABEL = "failure_class"
_TOOL_MEASUREMENT = 1
_TOOL_OUTCOME_CANCELLED = "cancelled"
_TOOL_ERROR_TYPE_ATTRIBUTE = "error.type"
_TOOL_ERROR_CODE_ATTRIBUTE = "error.code"
_TOOL_ERROR_TYPE_VALUE = "ToolActivityError"
_TOOL_INTERNAL_ERROR_CODE = SafeErrorCode.INTERNAL_ERROR.value
_TOOL_POLICY_ERROR_CODE = SafeErrorCode.AUTHORIZATION_FAILED.value
_TOOL_EXECUTION_UNKNOWN_ERROR_CODE = SafeErrorCode.EXECUTION_UNKNOWN.value


def _prevalidate_tool_telemetry_schema() -> None:
    exact_local_values = (
        (_TOOL_EXECUTE_SPAN_NAME, "tool.gateway.execute"),
        (_TOOL_APPROVAL_SPAN_NAME, "tool.approval.resolve"),
        (_TOOL_FAMILY_ATTRIBUTE, "jhin.tool_family"),
        (_TOOL_RISK_ATTRIBUTE, "jhin.risk"),
        (_TOOL_OUTCOME_ATTRIBUTE, "jhin.outcome"),
        (_TOOL_CALLS_METRIC, "tool_calls_total"),
        (_TOOL_FAILURES_METRIC, "tool_call_failures_total"),
        (_TOOL_FAMILY_LABEL, "tool_family"),
        (_TOOL_RISK_LABEL, "risk"),
        (_TOOL_OUTCOME_LABEL, "outcome"),
        (_TOOL_FAILURE_LABEL, "failure_class"),
        (_TOOL_OUTCOME_CANCELLED, "cancelled"),
        (_TOOL_ERROR_TYPE_ATTRIBUTE, "error.type"),
        (_TOOL_ERROR_CODE_ATTRIBUTE, "error.code"),
        (_TOOL_ERROR_TYPE_VALUE, "ToolActivityError"),
        (_TOOL_INTERNAL_ERROR_CODE, SafeErrorCode.INTERNAL_ERROR.value),
        (_TOOL_POLICY_ERROR_CODE, SafeErrorCode.AUTHORIZATION_FAILED.value),
        (
            _TOOL_EXECUTION_UNKNOWN_ERROR_CODE,
            SafeErrorCode.EXECUTION_UNKNOWN.value,
        ),
    )
    exact_package_values = (
        (tool_telemetry._TOOL_ROW_COMPLETED, "completed"),
        (tool_telemetry._TOOL_ROW_FAILED, "failed"),
        (tool_telemetry._TOOL_ROW_DENIED, "denied"),
        (tool_telemetry._TOOL_ROW_REJECTED, "rejected"),
        (tool_telemetry._TOOL_ROW_EXECUTION_UNKNOWN, "execution_unknown"),
        (tool_telemetry._TOOL_ROW_PENDING_APPROVAL, "pending_approval"),
        (tool_telemetry._TOOL_OUTCOME_COMPLETED, "completed"),
        (tool_telemetry._TOOL_OUTCOME_ACCEPTED, "accepted"),
        (tool_telemetry._TOOL_OUTCOME_FAILED, "failed"),
        (tool_telemetry._TOOL_OUTCOME_DENIED, "denied"),
        (tool_telemetry._TOOL_OUTCOME_REJECTED, "rejected"),
        (tool_telemetry._TOOL_OUTCOME_EXECUTION_UNKNOWN, "execution_unknown"),
        (tool_telemetry._TOOL_OUTCOME_OTHER, "other"),
        (tool_telemetry._TOOL_FAILURE_INTERNAL, "internal"),
        (tool_telemetry._TOOL_FAILURE_POLICY, "policy"),
        (tool_telemetry._TOOL_FAILURE_EXECUTION_UNKNOWN, "execution_unknown"),
    )
    if any(
        type(actual) is not str or actual != expected for actual, expected in exact_local_values
    ):
        raise ValueError("invalid fixed tool telemetry schema")
    if any(
        type(actual) is not str or actual != expected for actual, expected in exact_package_values
    ):
        raise ValueError("invalid fixed tool telemetry description schema")
    if type(_TOOL_MEASUREMENT) is not int or _TOOL_MEASUREMENT != 1:
        raise ValueError("invalid fixed tool telemetry measurement")
    normalized = normalize_span_attributes(
        {
            _TOOL_FAMILY_ATTRIBUTE: "other",
            _TOOL_RISK_ATTRIBUTE: "other",
            _TOOL_OUTCOME_ATTRIBUTE: _TOOL_OUTCOME_CANCELLED,
        }
    )
    if normalized != {
        "jhin.tool_family": "other",
        "jhin.risk": "other",
        "jhin.outcome": "cancelled",
    }:
        raise ValueError("invalid fixed tool span schema")


_FATAL_AUTHORITY_TYPES = (KeyboardInterrupt, SystemExit)


def _run_tool_diagnostic(operation: Callable[[], None], *, secondary: bool = False) -> None:
    """Run one telemetry operation as a diagnostic.

    Ordinary failures never escape. Fatal authority (``KeyboardInterrupt``,
    ``SystemExit``) always escapes. Cancellation escapes only while it is the
    primary authority: when product work is already unwinding (``secondary``),
    a telemetry cancellation may not replace the active product exception.
    """
    try:
        operation()
    except BaseException as error:
        if isinstance(error, _FATAL_AUTHORITY_TYPES):
            raise
        if not secondary and not isinstance(error, Exception):
            raise


def _current_otel_context() -> Context | None:
    try:
        return otel_context.get_current()
    except Exception:
        return None


def _best_effort_restore_context(entry_context: Context | None) -> None:
    if entry_context is None:
        return
    current = _current_otel_context()
    if current is None or current is entry_context:
        return
    try:
        otel_context.attach(entry_context)
    except Exception:
        return


class _ToolSpanScope:
    """One tool span whose setup and teardown are diagnostic-only.

    Setup failures leave ``span`` as ``None`` and restore the entry context
    before any product work runs, so a diagnostic never enters the product
    exception chain. Teardown restores the entry context even when a hostile
    manager leaves its token attached. Fatal authority always propagates;
    cancellation propagates only while it is primary.
    """

    __slots__ = ("_entry_context", "_manager", "span")

    def __init__(self, tracer: Any, name: str) -> None:
        self.span: Any | None = None
        self._manager: Any | None = None
        self._entry_context = _current_otel_context()
        if self._entry_context is None:
            return
        manager: Any | None = None
        try:
            manager = tracer.start_as_current_span(
                name,
                attributes={
                    _TOOL_FAMILY_ATTRIBUTE: "other",
                    _TOOL_RISK_ATTRIBUTE: "other",
                    _TOOL_OUTCOME_ATTRIBUTE: "other",
                },
                record_exception=False,
                set_status_on_exception=False,
            )
            span = manager.__enter__()
        except BaseException as setup_error:
            if not isinstance(setup_error, Exception):
                _best_effort_restore_context(self._entry_context)
                raise
            if manager is not None and _current_otel_context() is not self._entry_context:
                try:
                    manager.__exit__(type(setup_error), setup_error, setup_error.__traceback__)
                except BaseException as recovery_error:
                    if not isinstance(recovery_error, Exception):
                        _best_effort_restore_context(self._entry_context)
                        raise
            _best_effort_restore_context(self._entry_context)
            return
        self.span = span
        self._manager = manager

    def finish(self, active_error: BaseException | None) -> None:
        manager = self._manager
        if manager is None:
            return
        self._manager = None
        try:
            if active_error is None:
                manager.__exit__(None, None, None)
            else:
                manager.__exit__(type(active_error), active_error, active_error.__traceback__)
        except BaseException as teardown_error:
            if isinstance(teardown_error, _FATAL_AUTHORITY_TYPES):
                raise
            if active_error is None and not isinstance(teardown_error, Exception):
                raise
        finally:
            _best_effort_restore_context(self._entry_context)


def _set_tool_span_description(
    span: Any | None,
    description: ToolTelemetryDescription,
    *,
    secondary: bool = False,
) -> None:
    if span is None:
        return
    _run_tool_diagnostic(
        lambda: set_span_attributes(
            span,
            {
                _TOOL_FAMILY_ATTRIBUTE: description.tool_family,
                _TOOL_RISK_ATTRIBUTE: description.risk,
                _TOOL_OUTCOME_ATTRIBUTE: description.outcome,
            },
        ),
        secondary=secondary,
    )
    error_code: str | None = None
    if description.failure_class == tool_telemetry._TOOL_FAILURE_INTERNAL:
        error_code = _TOOL_INTERNAL_ERROR_CODE
    elif description.failure_class == tool_telemetry._TOOL_FAILURE_POLICY:
        error_code = _TOOL_POLICY_ERROR_CODE
    elif description.failure_class == tool_telemetry._TOOL_FAILURE_EXECUTION_UNKNOWN:
        error_code = _TOOL_EXECUTION_UNKNOWN_ERROR_CODE
    if error_code is not None:
        safe_error = SafeError(
            type=_TOOL_ERROR_TYPE_VALUE,
            code=SafeErrorCode(error_code),
        )
        _run_tool_diagnostic(lambda: record_span_error(span, safe_error), secondary=secondary)


def _cancelled_tool_description(
    description: ToolTelemetryDescription | None,
) -> ToolTelemetryDescription:
    return ToolTelemetryDescription(
        tool_family="other" if description is None else description.tool_family,
        risk="other" if description is None else description.risk,
        expected_row_status=None,
        outcome=_TOOL_OUTCOME_CANCELLED,
        failure_class=None,
        terminal_countable=False,
    )


def _non_retryable(message: str, *, error_type: str) -> ApplicationError:
    return ApplicationError(message, type=error_type, non_retryable=True)


def _uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as error:
        raise _non_retryable(
            f"bound tool {field} is invalid",
            error_type="bound_tool_invalid",
        ) from error


def bound_manifest_entry_statement(
    params: ExecuteBoundToolInput,
) -> Select[tuple[int | None, bool | None, str | None, str | None]]:
    requested = RunEvent.payload_json["manifest"]["calls"][params.ordinal]
    return (
        select(
            requested["ordinal"].as_integer().label("ordinal"),
            requested["lossless"].as_boolean().label("lossless"),
            requested["tool_name"].as_string().label("tool_name"),
            requested["arguments_json"].as_string().label("arguments_json"),
        )
        .where(
            RunEvent.workspace_id == UUID(params.workspace_id),
            RunEvent.run_id == UUID(params.run_id),
            RunEvent.event_type == "agent.step.tool_manifest",
            RunEvent.payload_json["step"].as_integer() == params.step_index,
        )
        .limit(2)
    )


async def _load_bound_call(
    session: AsyncSession,
    params: ExecuteBoundToolInput,
) -> BoundManifestEntry:
    rows = (await session.execute(bound_manifest_entry_statement(params))).tuples().all()
    if len(rows) != 1:
        raise _non_retryable(
            "bound tool manifest entry not found",
            error_type="bound_tool_not_found",
        )
    ordinal, lossless, tool_name, arguments_json = rows[0]
    if (
        ordinal != params.ordinal
        or lossless is not True
        or not isinstance(tool_name, str)
        or len(tool_name) > 200
        or not isinstance(arguments_json, str)
        or len(arguments_json) > 8_192
    ):
        raise _non_retryable(
            "bound tool manifest entry is malformed",
            error_type="bound_tool_invalid",
        )
    return BoundManifestEntry(ordinal, lossless, tool_name, arguments_json)


async def _load_runtime_context(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
) -> _RuntimeContext:
    row = (
        await session.execute(
            select(AgentRun.task_id, AgentRun.agent_id, Agent.name)
            .join(
                Agent,
                (Agent.id == AgentRun.agent_id) & (Agent.workspace_id == AgentRun.workspace_id),
            )
            .join(
                Task,
                (Task.id == AgentRun.task_id)
                & (Task.workspace_id == AgentRun.workspace_id)
                & (Task.assigned_agent_id == AgentRun.agent_id),
            )
            .where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == workspace_id,
            )
            .limit(2)
        )
    ).one_or_none()
    if row is None or row.task_id is None:
        raise _non_retryable(
            "bound tool execution context not found",
            error_type="bound_tool_context_not_found",
        )
    return _RuntimeContext(task_id=row.task_id, agent_id=row.agent_id, agent_name=row.name)


def _bound_result(outcome: GatewayOutcome) -> BoundToolResult:
    stop_reason: str | None = None
    if outcome.status == "needs_approval":
        stop_reason = "needs_approval"
    elif outcome.status == "needs_review":
        stop_reason = "needs_review"
    elif outcome.status == "execution_unknown":
        stop_reason = SafeErrorCode.EXECUTION_UNKNOWN.value
    elif (
        outcome.status == "executed"
        and outcome.tool_name == "organization.delegate_task"
        and bool((outcome.sanitized_output or {}).get("blocking", True))
    ):
        stop_reason = "blocking_delegation"
    elif asked_question_id(outcome.sanitized_output, tool_name=outcome.tool_name):
        # A question is on somebody's screen and the run is about to park on
        # it. Executing the rest of the manifest would run work whose premise
        # is the answer nobody has given yet.
        stop_reason = "awaiting_person"
    return BoundToolResult(
        tool_call_id=str(outcome.tool_call_id),
        status=outcome.status,
        approval_id=str(outcome.approval_id) if outcome.approval_id is not None else None,
        stop_reason=stop_reason,
        review_id=str(outcome.review_id) if outcome.review_id is not None else None,
    )


def _raise_ordinary_failure(outcome: GatewayOutcome) -> None:
    if outcome.decision_code == "invocation_mismatch":
        raise _non_retryable(
            "runtime tool call changed across an activity retry",
            error_type="tool_invocation_mismatch",
        )
    if outcome.status in {"denied", "failed", "rejected"}:
        raise _non_retryable(
            ORDINARY_TOOL_FAILURE_MESSAGE,
            error_type=outcome.error_code or "bound_tool_execution_failed",
        )


def _approval_manifest_steps_statement(
    *,
    workspace_id: UUID,
    run_id: UUID,
) -> Select[tuple[int | None, int | None]]:
    return select(
        RunEvent.payload_json["step"].as_integer().label("step_index"),
        RunEvent.payload_json["manifest"]["count"].as_integer().label("call_count"),
    ).where(
        RunEvent.workspace_id == workspace_id,
        RunEvent.run_id == run_id,
        RunEvent.event_type == "agent.step.tool_manifest",
    )


async def _validate_approval_manifest_binding(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
    tool_call: ToolCall,
    catalog: ToolCatalog,
) -> tuple[int, BoundManifestEntry]:
    rows = (
        (
            await session.execute(
                _approval_manifest_steps_statement(workspace_id=workspace_id, run_id=run_id)
            )
        )
        .tuples()
        .all()
    )
    for step_index, call_count in rows:
        if (
            type(step_index) is not int
            or not 0 <= step_index <= MAX_TOOL_STEP_INDEX
            or type(call_count) is not int
            or not 0 <= call_count <= MAX_TOOL_CALLS_PER_STEP
        ):
            continue
        for ordinal in range(call_count):
            if stable_tool_invocation_id(run_id, step_index, ordinal) != tool_call.id:
                continue
            entry = await _load_bound_call(
                session,
                ExecuteBoundToolInput(
                    workspace_id=str(workspace_id),
                    run_id=str(run_id),
                    step_index=step_index,
                    ordinal=ordinal,
                ),
            )
            try:
                arguments = json.loads(entry.arguments_json)
                catalog_entry = catalog.get(entry.tool_name)
                if catalog_entry is None or not isinstance(arguments, dict):
                    break
                definition, _executor = catalog_entry
                canonical_input = definition.input_model.model_validate(arguments).model_dump(
                    mode="json"
                )
            except (json.JSONDecodeError, ValidationError):
                break
            if (
                entry.tool_name == tool_call.tool_name
                and canonical_input == tool_call.sanitized_input_json
            ):
                return step_index, entry
            break
    raise _non_retryable(
        "approval does not match a canonical bound tool call",
        error_type="approval_binding_mismatch",
    )


class ToolActivities:
    def __init__(self, resources: ToolWorkerResources, catalog: ToolCatalog) -> None:
        self._resources = resources
        self._catalog = catalog
        self._metrics = resources.runtime.metrics
        self._tracer = resources.runtime.tracer

    async def _load_tool_telemetry_authority(
        self,
        *,
        outcome: GatewayOutcome,
        step_index: int,
        ordinal: int,
    ) -> _ToolTelemetryAuthority | None:
        async with self._resources.session_factory() as session:
            rows = (
                await session.execute(
                    select(ToolCall, AgentRun, RunEvent, Approval)
                    .join(
                        AgentRun,
                        (AgentRun.id == ToolCall.run_id)
                        & (AgentRun.workspace_id == ToolCall.workspace_id),
                    )
                    .join(
                        RunEvent,
                        (RunEvent.workspace_id == ToolCall.workspace_id)
                        & (RunEvent.run_id == ToolCall.run_id)
                        & (RunEvent.event_type == "agent.step.tool_manifest")
                        & (RunEvent.payload_json["step"].as_integer() == step_index),
                    )
                    .outerjoin(
                        Approval,
                        (Approval.id == ToolCall.approval_id)
                        & (Approval.workspace_id == ToolCall.workspace_id),
                    )
                    .where(ToolCall.id == outcome.tool_call_id)
                    .limit(2)
                )
            ).all()
        if len(rows) != 1:
            return None
        tool_call, agent_run, manifest_event, approval = rows[0]
        payload = manifest_event.payload_json
        if type(payload) is not dict:
            return None
        persisted_step_index = payload.get("step")
        manifest = payload.get("manifest")
        if type(persisted_step_index) is not int or type(manifest) is not dict:
            return None
        count = manifest.get("count")
        calls = manifest.get("calls")
        if (
            type(count) is not int
            or type(calls) is not list
            or count != len(calls)
            or not 0 <= ordinal < len(calls)
        ):
            return None
        call = calls[ordinal]
        if type(call) is not dict:
            return None
        persisted_ordinal = call.get("ordinal")
        lossless = call.get("lossless")
        manifest_tool_name = call.get("tool_name")
        manifest_arguments_json = call.get("arguments_json")
        if (
            type(persisted_ordinal) is not int
            or type(lossless) is not bool
            or type(manifest_tool_name) is not str
            or type(manifest_arguments_json) is not str
        ):
            return None
        try:
            parsed_arguments = json.loads(manifest_arguments_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if type(parsed_arguments) is not dict:
            return None
        sanitized_input = tool_call.sanitized_input_json
        outcome_input = outcome.sanitized_input
        if type(sanitized_input) is not dict or type(outcome_input) is not dict:
            return None
        derived_tool_call_id = stable_tool_invocation_id(
            manifest_event.run_id,
            persisted_step_index,
            persisted_ordinal,
        )
        return _ToolTelemetryAuthority(
            outcome_tool_call_id=outcome.tool_call_id,
            outcome_status=outcome.status,
            outcome_tool_name=outcome.tool_name,
            outcome_sanitized_input_json=outcome_input,
            outcome_replayed=outcome.replayed,
            outcome_decision_code=outcome.decision_code,
            tool_call_id=tool_call.id,
            workspace_id=tool_call.workspace_id,
            run_id=tool_call.run_id,
            agent_id=tool_call.agent_id,
            tool_name=tool_call.tool_name,
            sanitized_input_json=sanitized_input,
            task_id=manifest_event.task_id,
            agent_run_id=agent_run.id,
            agent_run_workspace_id=agent_run.workspace_id,
            agent_run_agent_id=agent_run.agent_id,
            agent_run_task_id=agent_run.task_id,
            manifest_workspace_id=manifest_event.workspace_id,
            manifest_run_id=manifest_event.run_id,
            manifest_step_index=persisted_step_index,
            manifest_ordinal=persisted_ordinal,
            manifest_lossless=lossless,
            manifest_tool_name=manifest_tool_name,
            manifest_arguments_json=manifest_arguments_json,
            manifest_canonical_input_json=sanitized_input,
            derived_tool_call_id=derived_tool_call_id,
            row_status=tool_call.status,
            approval_id=tool_call.approval_id,
            approval_workspace_id=None if approval is None else approval.workspace_id,
            approval_task_id=None if approval is None else approval.task_id,
            approval_run_id=None if approval is None else approval.run_id,
            approval_requested_by_agent_id=(
                None if approval is None else approval.requested_by_agent_id
            ),
            approval_action_type=None if approval is None else approval.action_type,
            approval_status=None if approval is None else approval.status,
        )

    @staticmethod
    def _tool_telemetry_authority_matches(
        authority: _ToolTelemetryAuthority,
        *,
        outcome: GatewayOutcome,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        step_index: int,
        ordinal: int,
        manifest_tool_name: str,
        manifest_arguments_json: str,
        approval_id: UUID | None,
        expected_row_status: str | None,
        expected_approval_status: str | None,
    ) -> bool:
        if (
            authority.outcome_tool_call_id != outcome.tool_call_id
            or authority.outcome_status != outcome.status
            or authority.outcome_tool_name != outcome.tool_name
            or authority.outcome_sanitized_input_json != outcome.sanitized_input
            or authority.outcome_replayed is not outcome.replayed
            or authority.outcome_decision_code != outcome.decision_code
            or authority.outcome_decision_code == "invocation_mismatch"
            or authority.tool_call_id != outcome.tool_call_id
            or authority.workspace_id != workspace_id
            or authority.run_id != run_id
            or authority.agent_id != agent_id
            or authority.tool_name != manifest_tool_name
            or authority.tool_name != outcome.tool_name
            or authority.sanitized_input_json != outcome.sanitized_input
            or authority.task_id != task_id
            or authority.agent_run_id != run_id
            or authority.agent_run_workspace_id != workspace_id
            or authority.agent_run_agent_id != agent_id
            or authority.agent_run_task_id != task_id
            or authority.manifest_workspace_id != workspace_id
            or authority.manifest_run_id != run_id
            or authority.manifest_step_index != step_index
            or authority.manifest_ordinal != ordinal
            or authority.manifest_lossless is not True
            or authority.manifest_tool_name != manifest_tool_name
            or authority.manifest_arguments_json != manifest_arguments_json
            or authority.manifest_canonical_input_json != outcome.sanitized_input
            or authority.derived_tool_call_id
            != stable_tool_invocation_id(run_id, step_index, ordinal)
            or authority.derived_tool_call_id != authority.tool_call_id
            or authority.approval_id != approval_id
            or outcome.approval_id != approval_id
            or expected_row_status is None
            or authority.row_status != expected_row_status
        ):
            return False
        if approval_id is None:
            return (
                authority.approval_workspace_id is None
                and authority.approval_task_id is None
                and authority.approval_run_id is None
                and authority.approval_requested_by_agent_id is None
                and authority.approval_action_type is None
                and authority.approval_status is None
            )
        return (
            authority.approval_workspace_id == workspace_id
            and authority.approval_task_id == task_id
            and authority.approval_run_id == run_id
            and authority.approval_requested_by_agent_id == agent_id
            and authority.approval_action_type == manifest_tool_name
            and expected_approval_status is not None
            and authority.approval_status == expected_approval_status
        )

    def _record_tool_metrics(self, description: ToolTelemetryDescription) -> None:
        _run_tool_diagnostic(
            lambda: self._metrics.counter(_TOOL_CALLS_METRIC).add(
                _TOOL_MEASUREMENT,
                **{
                    _TOOL_FAMILY_LABEL: description.tool_family,
                    _TOOL_RISK_LABEL: description.risk,
                    _TOOL_OUTCOME_LABEL: description.outcome,
                },
            )
        )
        failure_class = description.failure_class
        if failure_class is not None:
            _run_tool_diagnostic(
                lambda: self._metrics.counter(_TOOL_FAILURES_METRIC).add(
                    _TOOL_MEASUREMENT,
                    **{
                        _TOOL_FAMILY_LABEL: description.tool_family,
                        _TOOL_FAILURE_LABEL: failure_class,
                    },
                )
            )

    async def _record_committed_tool_telemetry(
        self,
        span: Any | None,
        *,
        outcome: GatewayOutcome,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        step_index: int,
        ordinal: int,
        manifest_tool_name: str,
        manifest_arguments_json: str,
        approval_id: UUID | None,
        suppress_terminal_metrics: bool = False,
    ) -> None:
        """Describe and count one committed tool transition.

        Runs only after the activity's own commit, while nothing is unwinding:
        ordinary diagnostic failures leave the span at its ``other`` defaults and
        record nothing, while cancellation and fatal authority propagate. The
        durable row status and approval state are proved from a fresh load
        before the catalog is consulted for family/risk labels.
        """
        try:
            authority = await self._load_tool_telemetry_authority(
                outcome=outcome,
                step_index=step_index,
                ordinal=ordinal,
            )
        except Exception:
            return
        if type(authority) is not _ToolTelemetryAuthority:
            return
        try:
            (
                expected_row_status,
                expected_approval_status,
                _expected_outcome,
                _expected_failure_class,
                _expected_terminal_countable,
            ) = _tool_status_authority(outcome.status)
            matched = self._tool_telemetry_authority_matches(
                authority,
                outcome=outcome,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
                step_index=step_index,
                ordinal=ordinal,
                manifest_tool_name=manifest_tool_name,
                manifest_arguments_json=manifest_arguments_json,
                approval_id=approval_id,
                expected_row_status=expected_row_status,
                expected_approval_status=expected_approval_status,
            )
        except Exception:
            return
        if matched is not True:
            return
        try:
            description = self._describe_tool(outcome.tool_name, outcome.status)
        except Exception:
            return
        if (
            type(description) is not ToolTelemetryDescription
            or description.expected_row_status != authority.row_status
        ):
            return
        _set_tool_span_description(span, description)
        if (
            description.terminal_countable
            and not outcome.replayed
            and not suppress_terminal_metrics
        ):
            self._record_tool_metrics(description)

    def _record_cancelled_tool_span(self, span: Any | None, tool_name: str) -> None:
        """Secondary diagnostics while product cancellation is unwinding."""
        description: ToolTelemetryDescription | None = None
        try:
            candidate = self._describe_tool(tool_name, _TOOL_OUTCOME_CANCELLED)
            if type(candidate) is ToolTelemetryDescription:
                description = candidate
        except BaseException as error:
            if isinstance(error, _FATAL_AUTHORITY_TYPES):
                raise
        _set_tool_span_description(span, _cancelled_tool_description(description), secondary=True)

    def _describe_tool(self, tool_name: object, gateway_status: object) -> ToolTelemetryDescription:
        return describe_tool_telemetry(self._catalog, tool_name, gateway_status)

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def resolve_advertised_tools_activity(
        self,
        params: ResolveAdvertisedToolsInput,
    ) -> list[AdvertisedTool]:
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        agent_id = _uuid(params.agent_id, field="agent_id")
        task_id = _uuid(params.task_id, field="task_id") if params.task_id else None
        async with self._resources.session_factory() as session:
            rows = await session.scalars(
                select(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.workspace_id == workspace_id,
                    AgentCapabilityGrant.agent_id == agent_id,
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
                    continue
            # Connector tools need a workspace connection id the model cannot
            # guess; label the ones the agent's grants pin so the description
            # can spell them out (prompt context only — never authorization).
            pinned_ids: set[UUID] = set()
            for grant in grants:
                raw = grant.scope.get("connection_id")
                if isinstance(raw, str):
                    try:
                        pinned_ids.add(UUID(raw))
                    except ValueError:
                        continue
            connection_labels: dict[str, str] = {}
            if pinned_ids:
                connections = await session.scalars(
                    select(Connection).where(
                        Connection.workspace_id == workspace_id,
                        Connection.id.in_(pinned_ids),
                        Connection.status == ConnectionStatus.ACTIVE.value,
                    )
                )
                connection_labels = {
                    str(connection.id): f"{connection.name} ({connection.connector_type})"
                    for connection in connections
                }
            # Workspace-scoped view: static tools plus tools discovered from
            # this workspace's MCP connections (docs/architecture/mcp.md).
            catalog = await self._catalog.for_workspace(session, workspace_id)
            # Task-kind scoping: some tools only mean something for assigned
            # work (reporting a result back to a delegator). On a plain chat
            # turn they are withheld so the model answers the person instead
            # of filing a report at them. Never widens the grant set.
            task = (
                await session.scalar(
                    select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
                )
                if task_id is not None
                else None
            )
            # A grant pinned to a connection that is not ACTIVE — deleted,
            # disabled, lapsed — advertises nothing: the labels above are
            # exactly the live pins, so they double as the allow-list.
            definitions = task_scoped_tool_definitions(
                allowed_tool_definitions(
                    catalog, grants, live_connection_ids=set(connection_labels)
                ),
                task,
            )
        return [
            AdvertisedTool(
                name=definition.name,
                description=advertised_description(definition, grants, connection_labels),
                parameters=definition.input_json_schema(),
            )
            for definition in definitions
        ]

    @activity.defn(name=ACTIVITY_EXECUTE_BOUND_TOOL)
    async def execute_bound_tool_activity(
        self,
        params: ExecuteBoundToolInput,
    ) -> BoundToolResult:
        _prevalidate_tool_telemetry_schema()
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        run_id = _uuid(params.run_id, field="run_id")
        if not 0 <= params.step_index <= MAX_TOOL_STEP_INDEX or not (
            0 <= params.ordinal < MAX_TOOL_CALLS_PER_STEP
        ):
            raise _non_retryable(
                "bound tool position is outside the supported range",
                error_type="bound_tool_invalid",
            )
        async with self._resources.session_factory() as session:
            entry = await _load_bound_call(session, params)
            context = await _load_runtime_context(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
            )
            invocation_id = stable_tool_invocation_id(run_id, params.step_index, params.ordinal)
            catalog = await self._catalog.for_workspace(session, workspace_id)
            scope = _ToolSpanScope(self._tracer, _TOOL_EXECUTE_SPAN_NAME)
            try:
                gateway = ToolGateway(
                    ToolExecutionContext(
                        session=session,
                        workspace_id=workspace_id,
                        task_id=context.task_id,
                        run_id=run_id,
                        agent_id=context.agent_id,
                        agent_name=context.agent_name,
                        crypto=self._resources.crypto,
                        session_factory=self._resources.session_factory,
                        test_barrier=self._resources.test_barrier,
                    ),
                    catalog,
                )
                try:
                    outcome = await gateway.request(
                        entry.tool_name,
                        entry.arguments_json,
                        invocation_id=invocation_id,
                    )
                    await session.commit()
                except asyncio.CancelledError:
                    self._record_cancelled_tool_span(scope.span, entry.tool_name)
                    raise
                except GatewayStateError as error:
                    await session.rollback()
                    raise _non_retryable(
                        "bound tool gateway state is invalid",
                        error_type="bound_tool_state_invalid",
                    ) from error
                await self._record_committed_tool_telemetry(
                    scope.span,
                    outcome=outcome,
                    workspace_id=workspace_id,
                    task_id=context.task_id,
                    run_id=run_id,
                    agent_id=context.agent_id,
                    step_index=params.step_index,
                    ordinal=params.ordinal,
                    manifest_tool_name=entry.tool_name,
                    manifest_arguments_json=entry.arguments_json,
                    approval_id=outcome.approval_id,
                )
                if outcome.tool_call_id != invocation_id:
                    raise _non_retryable(
                        "runtime tool call identity did not match its bound invocation",
                        error_type="tool_invocation_mismatch",
                    )
                _raise_ordinary_failure(outcome)
                result = _bound_result(outcome)
            except BaseException as active_error:
                scope.finish(active_error)
                raise
            scope.finish(None)
            return result

    @activity.defn(name=ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL)
    async def resolve_bound_tool_approval_activity(
        self,
        params: ResolveBoundToolApprovalInput,
    ) -> BoundToolResult:
        _prevalidate_tool_telemetry_schema()
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        task_id = _uuid(params.task_id, field="task_id")
        run_id = _uuid(params.run_id, field="run_id")
        agent_id = _uuid(params.agent_id, field="agent_id")
        approval_id = _uuid(params.approval_id, field="approval_id")
        async with self._resources.session_factory() as session:
            durable = (
                await session.execute(
                    select(Approval, ToolCall, AgentRun, Agent, Task)
                    .join(
                        ToolCall,
                        (ToolCall.approval_id == Approval.id)
                        & (ToolCall.workspace_id == Approval.workspace_id),
                    )
                    .join(
                        AgentRun,
                        (AgentRun.id == ToolCall.run_id)
                        & (AgentRun.workspace_id == ToolCall.workspace_id),
                    )
                    .join(
                        Agent,
                        (Agent.id == ToolCall.agent_id)
                        & (Agent.workspace_id == ToolCall.workspace_id),
                    )
                    .join(
                        Task,
                        (Task.id == AgentRun.task_id)
                        & (Task.workspace_id == AgentRun.workspace_id)
                        & (Task.assigned_agent_id == AgentRun.agent_id),
                    )
                    .where(
                        Approval.id == approval_id,
                        Approval.workspace_id == workspace_id,
                        Approval.task_id == task_id,
                        Approval.run_id == run_id,
                        Approval.requested_by_agent_id == agent_id,
                        ToolCall.run_id == run_id,
                        ToolCall.agent_id == agent_id,
                        ToolCall.workspace_id == workspace_id,
                        AgentRun.workspace_id == workspace_id,
                        AgentRun.task_id == task_id,
                        AgentRun.agent_id == agent_id,
                        Agent.id == agent_id,
                        Task.id == task_id,
                    )
                    .limit(2)
                )
            ).one_or_none()
            if durable is None:
                raise _non_retryable(
                    "approval execution context not found",
                    error_type="approval_context_not_found",
                )
            approval, tool_call, _run, agent, _task = durable
            expected_tool_call_id = tool_call.id
            catalog = await self._catalog.for_workspace(session, workspace_id)
            manifest_step_index, entry = await _validate_approval_manifest_binding(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                tool_call=tool_call,
                catalog=catalog,
            )
            if approval.status == ApprovalStatus.PENDING.value:
                raise ApplicationError("approval still pending", type="approval_pending")
            approval_status = approval.status
            if approval_status not in {
                ApprovalStatus.APPROVED.value,
                ApprovalStatus.REJECTED.value,
            }:
                raise _non_retryable(
                    "approval has no executable decision",
                    error_type="approval_state_invalid",
                )
            pre_gateway_tool_call_status = tool_call.status
            scope = _ToolSpanScope(self._tracer, _TOOL_APPROVAL_SPAN_NAME)
            try:
                gateway = ToolGateway(
                    ToolExecutionContext(
                        session=session,
                        workspace_id=workspace_id,
                        task_id=task_id,
                        run_id=run_id,
                        agent_id=agent_id,
                        agent_name=agent.name,
                        crypto=self._resources.crypto,
                        session_factory=self._resources.session_factory,
                        test_barrier=self._resources.test_barrier,
                    ),
                    catalog,
                )
                try:
                    if approval_status == ApprovalStatus.APPROVED.value:
                        outcome = await gateway.resolve_approved(approval_id)
                    else:
                        outcome = await gateway.resolve_rejected(approval_id)
                    await session.commit()
                except asyncio.CancelledError:
                    self._record_cancelled_tool_span(scope.span, entry.tool_name)
                    raise
                except GatewayStateError as error:
                    await session.rollback()
                    raise _non_retryable(
                        "approval gateway state is invalid",
                        error_type="approval_state_invalid",
                    ) from error
                await self._record_committed_tool_telemetry(
                    scope.span,
                    outcome=outcome,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    step_index=manifest_step_index,
                    ordinal=entry.ordinal,
                    manifest_tool_name=entry.tool_name,
                    manifest_arguments_json=entry.arguments_json,
                    approval_id=approval_id,
                    suppress_terminal_metrics=(
                        pre_gateway_tool_call_status == ToolCallStatus.EXECUTION_UNKNOWN.value
                        and outcome.status == "execution_unknown"
                    ),
                )
                if outcome.tool_call_id != expected_tool_call_id:
                    raise _non_retryable(
                        "approval tool identity changed during resolution",
                        error_type="tool_invocation_mismatch",
                    )
                result = _bound_result(outcome)
            except BaseException as active_error:
                scope.finish(active_error)
                raise
            scope.finish(None)
            return result

    @activity.defn(name=ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW)
    async def resolve_bound_tool_review_activity(
        self,
        params: ResolveBoundToolReviewInput,
    ) -> BoundToolResult:
        """Resume one review-parked call (mirrors approval resolution).

        Reloads the durable ``work_review``/``tool_call``/run/agent/task
        context and the canonical manifest binding, then hands the existing
        claim to ``ToolGateway.resolve_review``: approved reviews continue
        through fresh authorization, approval staging, or the stable
        execution claim; ``changes_requested``/``escalated`` record a denial
        carrying the reviewer's feedback. A still-pending review is a
        retryable error, exactly like a pending approval.
        """
        _prevalidate_tool_telemetry_schema()
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        task_id = _uuid(params.task_id, field="task_id")
        run_id = _uuid(params.run_id, field="run_id")
        agent_id = _uuid(params.agent_id, field="agent_id")
        review_id = _uuid(params.review_id, field="review_id")
        async with self._resources.session_factory() as session:
            durable = (
                await session.execute(
                    select(WorkReview, ToolCall, AgentRun, Agent, Task)
                    .join(
                        ToolCall,
                        (ToolCall.review_id == WorkReview.id)
                        & (ToolCall.workspace_id == WorkReview.workspace_id),
                    )
                    .join(
                        AgentRun,
                        (AgentRun.id == ToolCall.run_id)
                        & (AgentRun.workspace_id == ToolCall.workspace_id),
                    )
                    .join(
                        Agent,
                        (Agent.id == ToolCall.agent_id)
                        & (Agent.workspace_id == ToolCall.workspace_id),
                    )
                    .join(
                        Task,
                        (Task.id == AgentRun.task_id)
                        & (Task.workspace_id == AgentRun.workspace_id)
                        & (Task.assigned_agent_id == AgentRun.agent_id),
                    )
                    .where(
                        WorkReview.id == review_id,
                        WorkReview.workspace_id == workspace_id,
                        WorkReview.run_id == run_id,
                        WorkReview.subject_agent_id == agent_id,
                        ToolCall.run_id == run_id,
                        ToolCall.agent_id == agent_id,
                        ToolCall.workspace_id == workspace_id,
                        AgentRun.workspace_id == workspace_id,
                        AgentRun.task_id == task_id,
                        AgentRun.agent_id == agent_id,
                        Agent.id == agent_id,
                        Task.id == task_id,
                    )
                    .limit(2)
                )
            ).one_or_none()
            if durable is None:
                raise _non_retryable(
                    "review execution context not found",
                    error_type="review_context_not_found",
                )
            review, tool_call, _run, agent, _task = durable
            expected_tool_call_id = tool_call.id
            catalog = await self._catalog.for_workspace(session, workspace_id)
            manifest_step_index, entry = await _validate_approval_manifest_binding(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                tool_call=tool_call,
                catalog=catalog,
            )
            if review.status == WorkReviewStatus.PENDING.value:
                raise ApplicationError("review still pending", type="review_pending")
            pre_gateway_tool_call_status = tool_call.status
            scope = _ToolSpanScope(self._tracer, _TOOL_REVIEW_SPAN_NAME)
            try:
                gateway = ToolGateway(
                    ToolExecutionContext(
                        session=session,
                        workspace_id=workspace_id,
                        task_id=task_id,
                        run_id=run_id,
                        agent_id=agent_id,
                        agent_name=agent.name,
                        crypto=self._resources.crypto,
                        session_factory=self._resources.session_factory,
                        test_barrier=self._resources.test_barrier,
                    ),
                    catalog,
                )
                try:
                    outcome = await gateway.resolve_review(review_id)
                    await session.commit()
                except asyncio.CancelledError:
                    self._record_cancelled_tool_span(scope.span, entry.tool_name)
                    raise
                except GatewayStateError as error:
                    await session.rollback()
                    raise _non_retryable(
                        "review gateway state is invalid",
                        error_type="review_state_invalid",
                    ) from error
                await self._record_committed_tool_telemetry(
                    scope.span,
                    outcome=outcome,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    step_index=manifest_step_index,
                    ordinal=entry.ordinal,
                    manifest_tool_name=entry.tool_name,
                    manifest_arguments_json=entry.arguments_json,
                    approval_id=outcome.approval_id,
                    suppress_terminal_metrics=(
                        pre_gateway_tool_call_status == ToolCallStatus.EXECUTION_UNKNOWN.value
                        and outcome.status == "execution_unknown"
                    ),
                )
                if outcome.tool_call_id != expected_tool_call_id:
                    raise _non_retryable(
                        "review tool identity changed during resolution",
                        error_type="tool_invocation_mismatch",
                    )
                result = _bound_result(outcome)
            except BaseException as active_error:
                scope.finish(active_error)
                raise
            scope.finish(None)
            return result


__all__ = [
    "BoundManifestEntry",
    "ToolActivities",
    "bound_manifest_entry_statement",
]
