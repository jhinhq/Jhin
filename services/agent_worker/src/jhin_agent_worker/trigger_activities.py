"""Agent-owned trigger preparation and legacy sync coordination.

New and compatibility sync effects execute on the tool worker. This module
keeps task preparation local to the agent worker and retains the recorded
Phase 9 sync_external name only as an IDs-only coordinator.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.compatibility import compatibility_result
from jhin_agent_worker.resources import Resources
from jhin_db.models import AuditEvent, Message, Task, Trigger, TriggerInvocation
from jhin_domain import (
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    new_uuid7,
)
from jhin_events import EventEnvelope, EventSource
from jhin_observability import get_logger, normalize_event_family
from jhin_workflows.tool_compat import (
    SyncExternalCompatibilityWorkflow,
    SyncExternalToolInput,
    compatibility_workflow_id,
)
from jhin_workflows.triggered_task import (
    ACTIVITY_PREPARE_TRIGGERED_TASK,
    ACTIVITY_SYNC_EXTERNAL,
    PreparedTask,
    SyncExternalInput,
    SyncExternalResult,
    TriggeredTaskInput,
)

logger = get_logger(__name__)

_ACTIVE_TASK_STATES = (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value)


def _compatibility_uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ApplicationError(
            f"legacy {field} is not a UUID",
            type="compatibility_identity_invalid",
            non_retryable=True,
        ) from error


class TriggerCompatibilityActivities:
    """Phase 9 ``sync_external`` name as an IDs-only tool-queue coordinator."""

    def __init__(self, temporal_client: TemporalClient) -> None:
        self._client = temporal_client

    @activity.defn(name=ACTIVITY_SYNC_EXTERNAL)
    async def sync_external_activity(self, params: SyncExternalInput) -> SyncExternalResult:
        workspace_id = _compatibility_uuid(params.workspace_id, field="workspace_id")
        task_id = _compatibility_uuid(params.task_id, field="task_id")
        run_id = _compatibility_uuid(params.run_id, field="run_id")
        result = await compatibility_result(
            self._client,
            SyncExternalCompatibilityWorkflow.run,
            SyncExternalToolInput(
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
            ),
            workflow_id=compatibility_workflow_id("sync", run_id),
        )
        if not isinstance(result, SyncExternalResult):
            raise ApplicationError(
                "sync compatibility result is malformed",
                type="compatibility_result_invalid",
                non_retryable=True,
            )
        return result


class TriggerActivities:
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

    @activity.defn(name=ACTIVITY_PREPARE_TRIGGERED_TASK)
    async def prepare_triggered_task_activity(self, params: TriggeredTaskInput) -> PreparedTask:
        workspace_id = UUID(params.workspace_id)
        trigger_id = UUID(params.trigger_id)
        agent_id = UUID(params.agent_id)

        async with self._resources.session_factory() as session:
            trigger = await session.scalar(
                select(Trigger).where(
                    Trigger.id == trigger_id, Trigger.workspace_id == workspace_id
                )
            )
            if trigger is None:
                raise ApplicationError(
                    "trigger no longer exists", type="trigger_not_found", non_retryable=True
                )

            # Task-level dedupe (plan 26.8): one *active* task per external
            # entity. A finished task does not block a re-trigger later.
            existing = await session.scalar(
                select(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.external_source == params.external_source,
                    Task.external_id == params.external_id,
                    Task.state.in_(_ACTIVE_TASK_STATES),
                )
            )
            if existing is not None:
                await self._link_invocation(session, params, existing.id)
                await session.commit()
                logger.info(
                    "trigger.task_deduped",
                    task_id=str(existing.id),
                )
                return PreparedTask(task_id=str(existing.id), created=False)

            title = params.title or f"{params.external_source}:{params.external_id}"
            task = Task(
                workspace_id=workspace_id,
                external_source=params.external_source,
                external_id=params.external_id,
                title=title[:500],
                description=params.description,
                state=TaskState.QUEUED.value,
                assigned_agent_id=agent_id,
                trigger_id=trigger_id,
                correlation_id=new_uuid7(),
                metadata_json={
                    "origin": "trigger",
                    "trigger_id": params.trigger_id,
                    "trigger_name": params.trigger_name,
                    "external_source": params.external_source,
                    "external_id": params.external_id,
                    "external_url": params.external_url,
                    "event_id": params.event_id,
                },
            )
            session.add(task)
            await session.flush()
            # The child AgentTaskWorkflow runs under the API's id convention
            # so existing signal endpoints work on triggered tasks.
            task.temporal_workflow_id = f"task-{task.id}"

            origin = (
                f"Started by trigger \u201c{params.trigger_name}\u201d from "
                f"{params.external_source} {params.external_id}"
            )
            if params.external_url:
                origin += f" ({params.external_url})"
            session.add(
                Message(
                    workspace_id=workspace_id,
                    task_id=task.id,
                    sender_type=SenderType.SYSTEM.value,
                    sender_id=None,
                    recipient_type=RecipientType.TASK.value,
                    recipient_id=task.id,
                    message_type="text",
                    content_json={"text": origin},
                    visibility=MessageVisibility.VISIBLE.value,
                )
            )
            await self._link_invocation(session, params, task.id)
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type="system",
                    actor_id=None,
                    action="task.created",
                    target_type="task",
                    target_id=task.id,
                    metadata_json={
                        "origin": "trigger",
                        "trigger_id": params.trigger_id,
                        "trigger_name": params.trigger_name,
                        "external_id": params.external_id,
                    },
                )
            )
            await session.commit()
            task_id = task.id

        await self._publish(
            workspace_id,
            "task.created",
            {
                "task_id": str(task_id),
                "trigger_id": params.trigger_id,
                "external_source": params.external_source,
                "external_id": params.external_id,
                "agent_id": params.agent_id,
            },
        )
        return PreparedTask(task_id=str(task_id), created=True)

    async def _link_invocation(
        self, session: AsyncSession, params: TriggeredTaskInput, task_id: UUID
    ) -> None:
        invocation = await session.scalar(
            select(TriggerInvocation).where(
                TriggerInvocation.id == UUID(params.invocation_id),
                TriggerInvocation.workspace_id == UUID(params.workspace_id),
            )
        )
        if invocation is not None:
            invocation.task_id = task_id
