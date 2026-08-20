"""Activities behind EngineeringTicketWorkflow (plan 8.4, 27).

Role resolution, child-task creation, and final ticket bookkeeping. The
template's delegations derive their authority from the trigger definition a
human configured (standing authorization, like comment-back sync — plan
26.14); the child tasks themselves are ordinary delegated tasks, so the
agents running them still pass every tool call through the gateway and the
approval policy (plan 52).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_db.models import Agent, AuditEvent, Message, Task
from jhin_domain import (
    AgentStatus,
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    structured_content,
)
from jhin_events import EventEnvelope, EventSource
from jhin_observability import get_logger, normalize_event_family
from jhin_tools.organization import create_delegated_task
from jhin_workflows.engineering_ticket import (
    ACTIVITY_CREATE_ENGINEERING_CHILD_TASK,
    ACTIVITY_FINALIZE_ENGINEERING_TICKET,
    ACTIVITY_RESOLVE_ENGINEERING_PLAN,
    CreatedEngineeringChildTask,
    CreateEngineeringChildTaskInput,
    EngineeringPlan,
    EngineeringPlanInput,
    FinalizeEngineeringTicketInput,
)

logger = get_logger(__name__)

_FINAL_TASK_STATE = {
    "completed": TaskState.COMPLETED.value,
    "review_failed": TaskState.FAILED.value,
    "implementation_failed": TaskState.FAILED.value,
}

_FINAL_LINES = {
    "completed": "Engineering ticket completed: implementation done and review passed.",
    "review_failed": "Engineering ticket failed: review still failing after the retest limit.",
    "implementation_failed": "Engineering ticket failed: implementation did not complete.",
}


class EngineeringActivities:
    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    async def _publish(self, workspace_id: UUID, event_type: str, data: dict[str, Any]) -> None:
        try:
            await self._resources.publisher.publish(
                EventEnvelope(
                    event_type=event_type,
                    workspace_id=str(workspace_id),
                    source=EventSource(type="agent_worker"),
                    data=data,
                )
            )
        except Exception as exc:
            logger.warning(
                "events.publish_failed",
                event_type=normalize_event_family(event_type),
                error_type=type(exc).__name__,
            )

    @activity.defn(name=ACTIVITY_RESOLVE_ENGINEERING_PLAN)
    async def resolve_engineering_plan_activity(
        self, params: EngineeringPlanInput
    ) -> EngineeringPlan:
        """Resolve and validate the template roles against the org graph.

        Deterministic lookups only (plan 52): configured agent ids are
        verified active-in-workspace; a missing QA config falls back to an
        active teammate of the implementer whose slug or name contains "qa"."""
        workspace_id = UUID(params.workspace_id)

        async with self._resources.session_factory() as session:

            async def active_agent(raw_id: str) -> Agent | None:
                try:
                    agent_id = UUID(raw_id)
                except ValueError:
                    return None
                row: Agent | None = await session.scalar(
                    select(Agent).where(
                        Agent.id == agent_id,
                        Agent.workspace_id == workspace_id,
                        Agent.status == AgentStatus.ACTIVE.value,
                    )
                )
                return row

            coordinator = await active_agent(params.coordinator_agent_id)
            if coordinator is None:
                raise ApplicationError(
                    "trigger target agent is not active",
                    type="coordinator_not_active",
                    non_retryable=True,
                )

            implementer = coordinator
            if params.implementer_agent_id:
                configured = await active_agent(params.implementer_agent_id)
                if configured is None:
                    raise ApplicationError(
                        "configured implementer agent is not active",
                        type="implementer_not_active",
                        non_retryable=True,
                    )
                implementer = configured
            coordinator_mode = implementer.id != coordinator.id

            qa: Agent | None = None
            if params.qa_agent_id:
                qa = await active_agent(params.qa_agent_id)
            elif implementer.team_id is not None:
                candidates = await session.scalars(
                    select(Agent)
                    .where(
                        Agent.workspace_id == workspace_id,
                        Agent.team_id == implementer.team_id,
                        Agent.status == AgentStatus.ACTIVE.value,
                        Agent.id != implementer.id,
                        Agent.id != coordinator.id,
                    )
                    .order_by(Agent.created_at)
                )
                for candidate in candidates:
                    if "qa" in candidate.slug.lower() or "qa" in candidate.name.lower():
                        qa = candidate
                        break

            manager: Agent | None = None
            if params.manager_review and implementer.manager_agent_id is not None:
                manager = await active_agent(str(implementer.manager_agent_id))

            return EngineeringPlan(
                implementer_agent_id=str(implementer.id),
                coordinator_mode=coordinator_mode,
                qa_agent_id=str(qa.id) if qa is not None else "",
                manager_agent_id=str(manager.id) if manager is not None else "",
            )

    @activity.defn(name=ACTIVITY_CREATE_ENGINEERING_CHILD_TASK)
    async def create_engineering_child_task_activity(
        self, params: CreateEngineeringChildTaskInput
    ) -> CreatedEngineeringChildTask:
        """One delegated child-task row for a template hop — identical shape
        to agent-driven delegation so lineage/messages/UI are uniform."""
        workspace_id = UUID(params.workspace_id)

        async with self._resources.session_factory() as session:
            parent = await session.scalar(
                select(Task).where(
                    Task.id == UUID(params.parent_task_id), Task.workspace_id == workspace_id
                )
            )
            target = await session.scalar(
                select(Agent).where(
                    Agent.id == UUID(params.target_agent_id),
                    Agent.workspace_id == workspace_id,
                )
            )
            delegator = await session.scalar(
                select(Agent).where(
                    Agent.id == UUID(params.delegated_by_agent_id),
                    Agent.workspace_id == workspace_id,
                )
            )
            if parent is None or target is None or delegator is None:
                raise ApplicationError(
                    "template task/agent disappeared",
                    type="engineering_rows_missing",
                    non_retryable=True,
                )

            artifacts = [
                {
                    "type": str(item.get("type", "") or ""),
                    "id": str(item.get("id", "") or ""),
                    "url_ref": str(item.get("url_ref", "") or ""),
                }
                for item in params.artifacts
                if isinstance(item, dict)
            ]
            child = await create_delegated_task(
                session,
                workspace_id=workspace_id,
                parent=parent,
                target=target,
                delegated_by_agent_id=delegator.id,
                delegated_by_agent_name=delegator.name,
                delegated_by_run_id=None,
                title=params.title,
                instructions=params.instructions,
                expected_output=params.expected_output,
                blocking=False,
                kind=params.kind,
                artifacts=artifacts,
                origin="engineering_template",
            )
            if params.cycle:
                child.metadata_json = {
                    **child.metadata_json,
                    "engineering": {"cycle": params.cycle},
                }
            await session.commit()
            child_id = child.id

        await self._publish(
            workspace_id,
            "task.delegated",
            {
                "parent_task_id": params.parent_task_id,
                "child_task_id": str(child_id),
                "target_agent_id": params.target_agent_id,
                "kind": params.kind,
                "origin": "engineering_template",
            },
        )
        return CreatedEngineeringChildTask(child_task_id=str(child_id))

    @activity.defn(name=ACTIVITY_FINALIZE_ENGINEERING_TICKET)
    async def finalize_engineering_ticket_activity(
        self, params: FinalizeEngineeringTicketInput
    ) -> None:
        """Record the ticket outcome on the main task: final state, a
        structured status message, metadata for the UI, and an audit row."""
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)

        async with self._resources.session_factory() as session:
            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is None:
                raise ApplicationError(
                    "task disappeared", type="task_not_found", non_retryable=True
                )
            task.state = _FINAL_TASK_STATE.get(params.status, TaskState.FAILED.value)
            task.metadata_json = {
                **task.metadata_json,
                "engineering_result": {
                    "status": params.status,
                    "verdict": params.verdict,
                    "cycles_used": params.cycles_used,
                },
            }
            line = _FINAL_LINES.get(params.status, params.status)
            if params.cycles_used:
                line += f" ({params.cycles_used} review cycle(s), final verdict: "
                line += f"{params.verdict or 'none'})"
            session.add(
                Message(
                    workspace_id=workspace_id,
                    task_id=task_id,
                    sender_type=SenderType.SYSTEM.value,
                    sender_id=None,
                    recipient_type=RecipientType.TASK.value,
                    recipient_id=task_id,
                    message_type=MessageType.STATUS.value,
                    content_json=structured_content(
                        line,
                        status=params.status,
                        verdict=params.verdict,
                        cycles_used=params.cycles_used,
                        template="engineering_ticket",
                    ),
                    visibility=MessageVisibility.VISIBLE.value,
                )
            )
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type="system",
                    actor_id=None,
                    action="task.engineering_finished",
                    target_type="task",
                    target_id=task_id,
                    metadata_json={
                        "status": params.status,
                        "verdict": params.verdict,
                        "cycles_used": params.cycles_used,
                    },
                )
            )
            await session.commit()

        final_state = _FINAL_TASK_STATE.get(params.status, TaskState.FAILED.value)
        await self._publish(
            workspace_id,
            f"task.{final_state}",
            {"task_id": params.task_id, "template": "engineering_ticket", "status": params.status},
        )
