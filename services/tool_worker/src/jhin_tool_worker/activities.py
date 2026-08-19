"""Tool-worker activities: discovery, bound execution, and approval resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_db.models import Agent, AgentCapabilityGrant, AgentRun, Approval, RunEvent, ToolCall
from jhin_domain import ApprovalStatus
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
    allowed_tool_definitions,
    stable_tool_invocation_id,
)
from jhin_workflows.agent_task.shared import (
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
    AdvertisedTool,
    BoundToolResult,
    ExecuteBoundToolInput,
    ResolveAdvertisedToolsInput,
    ResolveBoundToolApprovalInput,
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
    elif outcome.status == "execution_unknown":
        stop_reason = "execution_unknown"
    elif (
        outcome.status == "executed"
        and outcome.tool_name == "organization.delegate_task"
        and bool((outcome.sanitized_output or {}).get("blocking", True))
    ):
        stop_reason = "blocking_delegation"
    return BoundToolResult(
        tool_call_id=str(outcome.tool_call_id),
        status=outcome.status,
        approval_id=str(outcome.approval_id) if outcome.approval_id is not None else None,
        stop_reason=stop_reason,
    )


def _raise_ordinary_failure(outcome: GatewayOutcome) -> None:
    if outcome.decision_code == "invocation_mismatch":
        raise _non_retryable(
            "runtime tool call changed across an activity retry",
            error_type="tool_invocation_mismatch",
        )
    if outcome.status in {"denied", "failed", "rejected"}:
        raise _non_retryable(
            "bound tool execution was rejected before a usable outcome",
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
) -> None:
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
                return
            break
    raise _non_retryable(
        "approval does not match a canonical bound tool call",
        error_type="approval_binding_mismatch",
    )


class ToolActivities:
    def __init__(self, resources: ToolWorkerResources, catalog: ToolCatalog) -> None:
        self._resources = resources
        self._catalog = catalog

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def resolve_advertised_tools_activity(
        self,
        params: ResolveAdvertisedToolsInput,
    ) -> list[AdvertisedTool]:
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        agent_id = _uuid(params.agent_id, field="agent_id")
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
        return [
            AdvertisedTool(
                name=definition.name,
                description=definition.description,
                parameters=definition.input_json_schema(),
            )
            for definition in allowed_tool_definitions(self._catalog, grants)
        ]

    @activity.defn(name=ACTIVITY_EXECUTE_BOUND_TOOL)
    async def execute_bound_tool_activity(
        self,
        params: ExecuteBoundToolInput,
    ) -> BoundToolResult:
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
                self._catalog,
            )
            try:
                outcome = await gateway.request(
                    entry.tool_name,
                    entry.arguments_json,
                    invocation_id=invocation_id,
                )
            except GatewayStateError as error:
                await session.rollback()
                raise _non_retryable(
                    "bound tool gateway state is invalid",
                    error_type="bound_tool_state_invalid",
                ) from error
            await session.commit()
            if outcome.tool_call_id != invocation_id:
                raise _non_retryable(
                    "runtime tool call identity did not match its bound invocation",
                    error_type="tool_invocation_mismatch",
                )
            _raise_ordinary_failure(outcome)
            return _bound_result(outcome)

    @activity.defn(name=ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL)
    async def resolve_bound_tool_approval_activity(
        self,
        params: ResolveBoundToolApprovalInput,
    ) -> BoundToolResult:
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        task_id = _uuid(params.task_id, field="task_id")
        run_id = _uuid(params.run_id, field="run_id")
        agent_id = _uuid(params.agent_id, field="agent_id")
        approval_id = _uuid(params.approval_id, field="approval_id")
        async with self._resources.session_factory() as session:
            durable = (
                await session.execute(
                    select(Approval, ToolCall, AgentRun, Agent)
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
                    .where(
                        Approval.id == approval_id,
                        Approval.workspace_id == workspace_id,
                        Approval.task_id == task_id,
                        Approval.run_id == run_id,
                        Approval.requested_by_agent_id == agent_id,
                        ToolCall.run_id == run_id,
                        ToolCall.agent_id == agent_id,
                        AgentRun.task_id == task_id,
                        AgentRun.agent_id == agent_id,
                    )
                    .limit(2)
                )
            ).one_or_none()
            if durable is None:
                raise _non_retryable(
                    "approval execution context not found",
                    error_type="approval_context_not_found",
                )
            approval, tool_call, run, agent = durable
            expected_tool_call_id = tool_call.id
            await _validate_approval_manifest_binding(
                session,
                workspace_id=workspace_id,
                run_id=run_id,
                tool_call=tool_call,
                catalog=self._catalog,
            )
            if approval.status == ApprovalStatus.PENDING.value:
                raise ApplicationError("approval still pending", type="approval_pending")
            gateway = ToolGateway(
                ToolExecutionContext(
                    session=session,
                    workspace_id=workspace_id,
                    task_id=run.task_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    agent_name=agent.name,
                    crypto=self._resources.crypto,
                    session_factory=self._resources.session_factory,
                    test_barrier=self._resources.test_barrier,
                ),
                self._catalog,
            )
            try:
                if approval.status == ApprovalStatus.APPROVED.value:
                    outcome = await gateway.resolve_approved(approval_id)
                elif approval.status == ApprovalStatus.REJECTED.value:
                    outcome = await gateway.resolve_rejected(approval_id)
                else:
                    raise _non_retryable(
                        "approval has no executable decision",
                        error_type="approval_state_invalid",
                    )
            except GatewayStateError as error:
                await session.rollback()
                raise _non_retryable(
                    "approval gateway state is invalid",
                    error_type="approval_state_invalid",
                ) from error
            await session.commit()
            if outcome.tool_call_id != expected_tool_call_id:
                raise _non_retryable(
                    "approval tool identity changed during resolution",
                    error_type="tool_invocation_mismatch",
                )
            return _bound_result(outcome)


__all__ = [
    "BoundManifestEntry",
    "ToolActivities",
    "bound_manifest_entry_statement",
]
