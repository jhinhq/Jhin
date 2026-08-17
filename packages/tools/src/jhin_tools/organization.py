"""Built-in organization tools: delegation and structured result reporting
(plan 7.5, 7.6, 29).

``organization.delegate_task`` creates the child *task row* and the
structured delegation message; the durable child workflow is started by the
delegating run's AgentTaskWorkflow (only workflows start child workflows —
plan 8.3). Authorization happens in two policy layers before the executor
runs: the generic gateway pipeline (capability grant, approval rules) and
the registered delegation validator (relationship/cycle/depth model from
:mod:`jhin_policy.delegation`).

``organization.report_result`` is how an agent finishes with structure: a
plan-7.6 standardized summary persisted as a ``result``/``review_result``
message and mirrored into the task metadata, where the delegation
summarizer picks it up for the parent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AuditEvent, Message, Task, Workspace
from jhin_domain import (
    ActorType,
    AgentStatus,
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    artifact,
    new_uuid7,
    structured_content,
)
from jhin_policy import (
    DecisionType,
    DelegationFacts,
    Grant,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
    delegation_settings,
    evaluate_delegation,
)
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator

# Bounds for graph walks; org graphs and task chains are shallow in practice.
_MAX_MANAGER_HOPS = 50
_MAX_TASK_ANCESTORS = 50

_ACTIVE_TASK_STATES = (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=100)
    id: str = Field(default="", max_length=300)
    url_ref: str = Field(default="", max_length=1000)


class DelegateTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_agent_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=20_000)
    expected_output: str = Field(default="", max_length=4_000)
    blocking: bool = True
    # "review_request" marks QA/review handoffs (plan 29); the child's
    # reported verdict then comes back as a review_result.
    kind: Literal["delegation", "review_request"] = "delegation"
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=20)


class DelegateTaskOutput(BaseModel):
    child_task_id: str
    target_agent_id: str
    target_agent_name: str
    blocking: bool
    kind: str
    status: str = "delegated"
    detail: str = ""


class ReportResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "pass", "fail", "blocked"] = "completed"
    summary: str = Field(min_length=1, max_length=4_000)
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)
    recommended_next_action: str = Field(default="", max_length=500)


class ReportResultOutput(BaseModel):
    message_id: str
    message_type: str
    status: str


# --- delegation facts (I/O) ---


async def _is_subordinate(
    session: AsyncSession, workspace_id: UUID, manager_id: UUID, target_id: UUID
) -> bool:
    """Walk the target's manager chain upward looking for the delegator."""
    seen: set[UUID] = set()
    current: UUID | None = target_id
    for _ in range(_MAX_MANAGER_HOPS):
        if current is None or current in seen:
            return False
        seen.add(current)
        agent = await session.scalar(
            select(Agent).where(Agent.id == current, Agent.workspace_id == workspace_id)
        )
        if agent is None or agent.manager_agent_id is None:
            return False
        if agent.manager_agent_id == manager_id:
            return True
        current = agent.manager_agent_id
    return False


async def _lineage(
    session: AsyncSession, workspace_id: UUID, task_id: UUID
) -> tuple[int, tuple[str, ...]]:
    """Depth of the task and the assigned agents of its *active* lineage
    (the task itself plus ancestors still in an active state)."""
    depth = 0
    agents: list[str] = []
    current: UUID | None = task_id
    for hop in range(_MAX_TASK_ANCESTORS):
        if current is None:
            break
        task = await session.scalar(
            select(Task).where(Task.id == current, Task.workspace_id == workspace_id)
        )
        if task is None:
            break
        if hop > 0:
            depth += 1
        if task.assigned_agent_id is not None and task.state in _ACTIVE_TASK_STATES:
            agents.append(str(task.assigned_agent_id))
        current = task.parent_task_id
    return depth, tuple(agents)


async def load_delegation_facts(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    delegator_agent_id: UUID,
    target_agent_id: str,
    task_id: UUID,
) -> DelegationFacts:
    """Resolve everything :func:`jhin_policy.evaluate_delegation` needs."""
    try:
        target_uuid = UUID(target_agent_id)
    except ValueError:
        return DelegationFacts(
            delegator_agent_id=str(delegator_agent_id), target_agent_id=target_agent_id
        )

    target = await session.scalar(
        select(Agent).where(Agent.id == target_uuid, Agent.workspace_id == workspace_id)
    )
    if target is None:
        return DelegationFacts(
            delegator_agent_id=str(delegator_agent_id), target_agent_id=str(target_uuid)
        )

    delegator = await session.scalar(
        select(Agent).where(Agent.id == delegator_agent_id, Agent.workspace_id == workspace_id)
    )
    same_team = (
        delegator is not None
        and delegator.team_id is not None
        and delegator.team_id == target.team_id
    )
    depth, ancestors = await _lineage(session, workspace_id, task_id)
    return DelegationFacts(
        delegator_agent_id=str(delegator_agent_id),
        target_agent_id=str(target_uuid),
        target_exists=True,
        target_active=target.status == AgentStatus.ACTIVE.value,
        target_is_subordinate=await _is_subordinate(
            session, workspace_id, delegator_agent_id, target_uuid
        ),
        target_in_same_team=same_team,
        task_depth=depth,
        ancestor_agent_ids=ancestors,
    )


async def validate_delegate_task(
    ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
) -> PolicyDecision | None:
    """The delegation validator the gateway runs before approval/execution.

    Returns None when the delegation is permitted; a DENY decision otherwise.
    """
    data = cast(DelegateTaskInput, payload)
    facts = await load_delegation_facts(
        ctx.session,
        workspace_id=ctx.workspace_id,
        delegator_agent_id=ctx.agent_id,
        target_agent_id=data.target_agent_id,
        task_id=ctx.task_id,
    )
    workspace = await ctx.session.get(Workspace, ctx.workspace_id)
    settings = delegation_settings(workspace.settings_json if workspace is not None else None)
    decision = evaluate_delegation(grants, facts, max_task_depth=settings.max_task_depth)
    if decision.allowed:
        return None
    return PolicyDecision(decision=DecisionType.DENY, code=decision.code, reason=decision.reason)


# --- executors ---


def _artifact_dicts(artifacts: Sequence[ArtifactRef]) -> list[dict[str, str]]:
    return [artifact(a.type, id=a.id, url_ref=a.url_ref) for a in artifacts]


async def create_delegated_task(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    parent: Task,
    target: Agent,
    delegated_by_agent_id: UUID,
    delegated_by_agent_name: str,
    delegated_by_run_id: UUID | None,
    title: str,
    instructions: str,
    expected_output: str = "",
    blocking: bool = True,
    kind: str = "delegation",
    artifacts: list[dict[str, str]] | None = None,
    origin: str = "delegation",
) -> Task:
    """Create one delegated child task with its structured message + audit row.

    Shared by the organization.delegate_task executor (agent-driven, gateway
    authorized) and the EngineeringTicketWorkflow activities (template-driven,
    where authority derives from the human-configured trigger definition —
    plan 8.4). Callers own authorization; this only persists the rows.
    """
    artifact_list = artifacts or []
    description = instructions
    if expected_output:
        description += f"\n\nExpected output: {expected_output}"

    child = Task(
        id=new_uuid7(),
        workspace_id=workspace_id,
        title=title[:500],
        description=description,
        state=TaskState.QUEUED.value,
        priority=parent.priority,
        assigned_agent_id=target.id,
        parent_task_id=parent.id,
        correlation_id=parent.correlation_id,
        metadata_json={
            "origin": origin,
            "delegation": {
                "kind": kind,
                "blocking": blocking,
                "delegated_by_agent_id": str(delegated_by_agent_id),
                "delegated_by_agent_name": delegated_by_agent_name,
                "delegated_by_run_id": str(delegated_by_run_id) if delegated_by_run_id else "",
                "parent_task_id": str(parent.id),
                "expected_output": expected_output,
                "artifacts": artifact_list,
            },
        },
    )
    # The child AgentTaskWorkflow runs under the API's id convention so
    # pause/resume/cancel/approval signals work on delegated tasks too.
    child.temporal_workflow_id = f"task-{child.id}"
    session.add(child)

    message_type = (
        MessageType.REVIEW_REQUEST if kind == "review_request" else MessageType.DELEGATION
    )
    session.add(
        Message(
            workspace_id=workspace_id,
            task_id=parent.id,
            run_id=delegated_by_run_id,
            sender_type=SenderType.AGENT.value,
            sender_id=delegated_by_agent_id,
            recipient_type=RecipientType.AGENT.value,
            recipient_id=target.id,
            message_type=message_type.value,
            content_json=structured_content(
                title,
                artifacts=artifact_list,
                recommended_next_action="await_result" if blocking else "",
                child_task_id=str(child.id),
                target_agent_id=str(target.id),
                target_agent_name=target.name,
                from_agent_id=str(delegated_by_agent_id),
                from_agent_name=delegated_by_agent_name,
                blocking=blocking,
                instructions=instructions[:2_000],
                expected_output=expected_output,
            ),
            visibility=MessageVisibility.VISIBLE.value,
        )
    )
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=delegated_by_agent_id,
            action="task.delegated",
            target_type="task",
            target_id=child.id,
            metadata_json={
                "parent_task_id": str(parent.id),
                "run_id": str(delegated_by_run_id) if delegated_by_run_id else None,
                "target_agent_id": str(target.id),
                "kind": kind,
                "blocking": blocking,
                "title": title[:500],
                "origin": origin,
            },
        )
    )
    await session.flush()
    return child


async def _delegate_task(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    """Create the child task + structured delegation message (authorized
    upstream by the gateway pipeline and the delegation validator)."""
    data = cast(DelegateTaskInput, payload)
    target_id = UUID(data.target_agent_id)
    target = await ctx.session.scalar(
        select(Agent).where(Agent.id == target_id, Agent.workspace_id == ctx.workspace_id)
    )
    parent = await ctx.session.scalar(
        select(Task).where(Task.id == ctx.task_id, Task.workspace_id == ctx.workspace_id)
    )
    if target is None or parent is None:
        raise ValueError("delegation target or parent task disappeared before execution")

    child = await create_delegated_task(
        ctx.session,
        workspace_id=ctx.workspace_id,
        parent=parent,
        target=target,
        delegated_by_agent_id=ctx.agent_id,
        delegated_by_agent_name=ctx.agent_name,
        delegated_by_run_id=ctx.run_id,
        title=data.title,
        instructions=data.instructions,
        expected_output=data.expected_output,
        blocking=data.blocking,
        kind=data.kind,
        artifacts=_artifact_dicts(data.artifacts),
    )
    return DelegateTaskOutput(
        child_task_id=str(child.id),
        target_agent_id=str(target_id),
        target_agent_name=target.name,
        blocking=data.blocking,
        kind=data.kind,
        detail=(
            "child task created; this run pauses until the result arrives"
            if data.blocking
            else "child task created; the result will arrive as a message when it finishes"
        ),
    )


async def _report_result(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    """Persist the plan-7.6 standardized completion summary for this task."""
    data = cast(ReportResultInput, payload)
    task = await ctx.session.scalar(
        select(Task).where(Task.id == ctx.task_id, Task.workspace_id == ctx.workspace_id)
    )
    if task is None:
        raise ValueError("task disappeared before the result could be reported")

    delegation = task.metadata_json.get("delegation", {})
    is_review = isinstance(delegation, dict) and delegation.get("kind") == "review_request"
    message_type = MessageType.REVIEW_RESULT if is_review else MessageType.RESULT

    recipient_id: UUID | None = None
    if isinstance(delegation, dict):
        raw = str(delegation.get("delegated_by_agent_id", "") or "")
        try:
            recipient_id = UUID(raw) if raw else None
        except ValueError:
            recipient_id = None

    content = structured_content(
        data.summary,
        artifacts=[artifact(a.type, id=a.id, url_ref=a.url_ref) for a in data.artifacts],
        risks=list(data.risks),
        recommended_next_action=data.recommended_next_action,
        task_id=str(task.id),
        status=data.status,
        from_agent_id=str(ctx.agent_id),
        from_agent_name=ctx.agent_name,
    )
    message = Message(
        id=new_uuid7(),
        workspace_id=ctx.workspace_id,
        task_id=task.id,
        run_id=ctx.run_id,
        sender_type=SenderType.AGENT.value,
        sender_id=ctx.agent_id,
        recipient_type=(
            RecipientType.AGENT.value if recipient_id is not None else RecipientType.TASK.value
        ),
        recipient_id=recipient_id if recipient_id is not None else task.id,
        message_type=message_type.value,
        content_json=content,
        visibility=MessageVisibility.VISIBLE.value,
    )
    ctx.session.add(message)
    # Mirror into task metadata: the deterministic place the delegation
    # summarizer reads (plan 7.6 — managers get summaries, not transcripts).
    task.metadata_json = {**task.metadata_json, "reported_result": content}
    ctx.session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="task.review_reported" if is_review else "task.result_reported",
            target_type="task",
            target_id=task.id,
            metadata_json={"status": data.status, "run_id": str(ctx.run_id)},
        )
    )
    await ctx.session.flush()
    return ReportResultOutput(
        message_id=str(message.id), message_type=message_type.value, status=data.status
    )


# --- registration (consumed by jhin_tools.builtin.build_builtin_catalog) ---

ORGANIZATION_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="organization.delegate_task",
            description=(
                "Delegate a sub-task to another agent in your organization. "
                "Set blocking=true to pause until the result comes back as a "
                "standardized summary; blocking=false runs it in the "
                "background and delivers the result as a message. Use "
                "kind='review_request' when asking for a QA/code review with "
                "a pass/fail verdict."
            ),
            risk=RiskLevel.WRITE,
            input_model=DelegateTaskInput,
            output_model=DelegateTaskOutput,
            required_capability="organization.delegate",
            supports_approval=True,
            defers_scope=True,
        ),
        _delegate_task,
        validate_delegate_task,
    ),
    (
        ToolDefinition(
            name="organization.report_result",
            description=(
                "Report the structured final result of your current task: a "
                "short summary, artifacts you produced, risks, and a "
                "recommended next action. For review tasks report "
                "status='pass' or 'fail'. Call this once, when your work is "
                "done."
            ),
            risk=RiskLevel.WRITE,
            input_model=ReportResultInput,
            output_model=ReportResultOutput,
            required_capability="organization.report_result",
            supports_approval=True,
        ),
        _report_result,
        None,
    ),
)
