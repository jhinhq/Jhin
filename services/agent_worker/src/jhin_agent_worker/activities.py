"""Agent-owned snapshot, reasoning, projection, and delegation activities.

External tool, connector, approval-execution, and sandbox-cleanup effects are
owned by the tool worker. Legacy Phase 9 activity names are implemented by the
stable-ID compatibility coordinators, not by this class.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from temporalio import activity
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.projections import AgentProjectionActivities
from jhin_agent_worker.projections import (
    _cancel_pending_run_approvals as _cancel_pending_run_approvals,
)
from jhin_agent_worker.reasoning import AgentReasoningActivities
from jhin_agent_worker.resources import Resources
from jhin_agents import resolve_snapshot
from jhin_agents.snapshot import SnapshotError
from jhin_db.models import Agent, AgentRun, AuditEvent, Message, RunEvent, Task, Workspace
from jhin_domain import (
    RUN_ACTIVE_STATUSES,
    ActorType,
    MessageType,
    MessageVisibility,
    RecipientType,
    RunStatus,
    SenderType,
    TaskState,
    structured_content,
)
from jhin_secrets.redaction import redact_text
from jhin_workflows.agent_task import (
    ACTIVITY_RESOLVE_SNAPSHOT,
    AgentTaskInput,
    SnapshotResult,
)
from jhin_workflows.delegated_task import (
    ACTIVITY_DELIVER_DELEGATION_RESULT,
    ACTIVITY_SUMMARIZE_DELEGATION,
    DelegationSummary,
    DeliverDelegationResultInput,
    SummarizeDelegationInput,
)

_ACTIVE_RUN_STATUSES = tuple(status.value for status in RUN_ACTIVE_STATUSES)


def _workspace_run_limit(workspace: Workspace | None) -> int | None:
    """Workspace-wide concurrent-run ceiling from settings_json (plan 30).

    ``{"concurrency": {"max_concurrent_runs": N}}`` (or the same key at the
    top level). Missing/invalid means no workspace ceiling.
    """
    if workspace is None:
        return None
    settings = workspace.settings_json or {}
    nested = settings.get("concurrency")
    raw = (nested or {}).get("max_concurrent_runs") if isinstance(nested, dict) else None
    if raw is None:
        raw = settings.get("max_concurrent_runs")
    try:
        limit = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return limit if limit >= 1 else None


class AgentActivities(AgentReasoningActivities, AgentProjectionActivities):
    def __init__(self, resources: Resources, temporal_client: TemporalClient | None = None) -> None:
        AgentReasoningActivities.__init__(self, resources)
        AgentProjectionActivities.__init__(
            self,
            resources,
            temporal_client=temporal_client,
        )

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot_activity(self, params: AgentTaskInput) -> SnapshotResult:
        workspace_id = UUID(params.workspace_id)
        agent_id = UUID(params.agent_id)
        task_id = UUID(params.task_id)
        info = activity.info()

        async with self._resources.session_factory() as session:
            # Concurrency admission (plan 30): claim a slot and create the run
            # in one transaction, or report queued. Row locks (workspace then
            # agent, consistent order) serialize concurrent admissions on
            # Postgres; SQLite ignores FOR UPDATE, which is fine for tests.
            workspace = await session.scalar(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            agent = await session.scalar(
                select(Agent)
                .where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
                .with_for_update()
            )
            queue_reason = ""
            if agent is not None:
                agent_limit = max(1, agent.max_concurrent_runs)
                active_agent = (
                    await session.scalar(
                        select(func.count())
                        .select_from(AgentRun)
                        .where(
                            AgentRun.workspace_id == workspace_id,
                            AgentRun.agent_id == agent_id,
                            AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                        )
                    )
                    or 0
                )
                if active_agent >= agent_limit:
                    queue_reason = "agent_concurrency"
            workspace_limit = _workspace_run_limit(workspace)
            if not queue_reason and workspace_limit is not None:
                active_workspace = (
                    await session.scalar(
                        select(func.count())
                        .select_from(AgentRun)
                        .where(
                            AgentRun.workspace_id == workspace_id,
                            AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                        )
                    )
                    or 0
                )
                if active_workspace >= workspace_limit:
                    queue_reason = "workspace_concurrency"

            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if queue_reason:
                if task is not None:
                    # Audit only the first transition into queued — the
                    # admission check re-runs on every kick/backstop poll
                    # while the task waits, and those are not new decisions.
                    if "queue" not in task.metadata_json:
                        session.add(
                            AuditEvent(
                                workspace_id=workspace_id,
                                actor_type="system",
                                actor_id=None,
                                action="task.queued",
                                target_type="task",
                                target_id=task_id,
                                metadata_json={
                                    "agent_id": params.agent_id,
                                    "reason": queue_reason,
                                },
                            )
                        )
                    task.state = TaskState.QUEUED.value
                    existing = task.metadata_json.get("queue")
                    since = (
                        existing.get("since", "")
                        if isinstance(existing, dict)
                        else datetime.now(UTC).isoformat()
                    ) or datetime.now(UTC).isoformat()
                    task.metadata_json = {
                        **task.metadata_json,
                        "queue": {
                            "reason": queue_reason,
                            "agent_id": params.agent_id,
                            "since": since,
                        },
                    }
                await session.commit()
                await self._publish(
                    workspace_id,
                    "task.queued",
                    {
                        "task_id": params.task_id,
                        "agent_id": params.agent_id,
                        "reason": queue_reason,
                    },
                )
                return SnapshotResult(
                    run_id="",
                    snapshot_json="",
                    snapshot_hash="",
                    max_steps=0,
                    queued=True,
                    queue_reason=queue_reason,
                )

            try:
                snapshot = await resolve_snapshot(session, workspace_id, agent_id)
            except SnapshotError as exc:
                raise ApplicationError(str(exc), type=exc.code, non_retryable=True) from exc

            run = AgentRun(
                workspace_id=workspace_id,
                agent_id=agent_id,
                task_id=task_id,
                status=RunStatus.RUNNING.value,
                model_profile_id=snapshot.model_profile.profile_id,
                snapshot_hash=snapshot.snapshot_hash(),
                started_at=datetime.now(UTC),
                temporal_workflow_id=info.workflow_id,
                temporal_run_id=info.workflow_run_id,
            )
            session.add(run)
            if task is not None:
                task.state = TaskState.RUNNING.value
                if "queue" in task.metadata_json:
                    task.metadata_json = {
                        key: value for key, value in task.metadata_json.items() if key != "queue"
                    }
            await session.flush()
            self._add_run_event(
                session,
                workspace_id=workspace_id,
                run_id=run.id,
                task_id=task_id,
                seq=0,
                event_type="run.started",
                payload={
                    "agent_name": snapshot.name,
                    "model_profile": snapshot.model_profile.display_name,
                    "model_name": snapshot.model_profile.model_name,
                    "snapshot_hash": snapshot.snapshot_hash(),
                },
            )
            await session.commit()
            run_id = run.id

        await self._publish(
            workspace_id,
            "agent.run.started",
            {"run_id": str(run_id), "task_id": params.task_id, "agent_id": params.agent_id},
        )
        return SnapshotResult(
            run_id=str(run_id),
            snapshot_json=snapshot.model_dump_json(),
            snapshot_hash=snapshot.snapshot_hash(),
            max_steps=snapshot.run_limits.max_steps,
        )

    @activity.defn(name=ACTIVITY_SUMMARIZE_DELEGATION)
    async def summarize_delegation_activity(
        self, params: SummarizeDelegationInput
    ) -> DelegationSummary:
        """Build the plan-7.6 standardized summary of a finished child task
        and persist it as a structured result message on the parent task.

        The summary comes from deterministic evidence — the child's
        organization.report_result record (mirrored into task metadata), with
        the child's last visible text as fallback. For review requests the
        verdict is deny-by-default: only an explicitly reported "pass"
        passes (plan 52 — never inferred from free-form model text)."""
        workspace_id = UUID(params.workspace_id)
        child_task_id = UUID(params.child_task_id)
        parent_task_id = UUID(params.parent_task_id)
        agent_id = UUID(params.agent_id)

        async with self._resources.session_factory() as session:
            child = await session.scalar(
                select(Task).where(Task.id == child_task_id, Task.workspace_id == workspace_id)
            )
            agent = await session.scalar(
                select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
            )
            agent_name = agent.name if agent is not None else "agent"

            reported = child.metadata_json.get("reported_result") if child is not None else None
            artifacts: list[Any] = []
            risks: list[str] = []
            next_action = ""
            if isinstance(reported, dict) and str(reported.get("summary", "")).strip():
                was_reported = True
                status = str(reported.get("status", "") or params.run_status)
                summary_text = str(reported.get("summary", ""))[:4_000]
                if isinstance(reported.get("artifacts"), list):
                    artifacts = reported["artifacts"]
                if isinstance(reported.get("risks"), list):
                    risks = [str(risk) for risk in reported["risks"]]
                next_action = str(reported.get("recommended_next_action", "") or "")
            else:
                was_reported = False
                status = params.run_status
                fallback = await session.scalar(
                    select(Message)
                    .where(
                        Message.task_id == child_task_id,
                        Message.workspace_id == workspace_id,
                        Message.visibility == MessageVisibility.VISIBLE.value,
                        Message.message_type == MessageType.TEXT.value,
                        Message.sender_type == SenderType.AGENT.value,
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )
                text = str((fallback.content_json.get("text") if fallback else "") or "")
                summary_text = (
                    text
                    or f"Delegated task ended with run status '{params.run_status}' "
                    "and no explicit report."
                )[:4_000]

            verdict = ""
            if params.kind == "review_request":
                verdict = "pass" if (was_reported and status == "pass") else "fail"

            content = structured_content(
                summary_text,
                artifacts=artifacts,
                risks=risks,
                recommended_next_action=next_action,
                child_task_id=params.child_task_id,
                status=status,
                verdict=verdict,
                kind=params.kind,
                run_status=params.run_status,
                reported=was_reported,
                from_agent_id=params.agent_id,
                from_agent_name=agent_name,
                **({"delivered": "observation"} if params.blocking else {}),
            )
            message_type = (
                MessageType.REVIEW_RESULT if params.kind == "review_request" else MessageType.RESULT
            )
            session.add(
                Message(
                    workspace_id=workspace_id,
                    task_id=parent_task_id,
                    run_id=UUID(params.parent_run_id) if params.parent_run_id else None,
                    sender_type=SenderType.AGENT.value,
                    sender_id=agent_id,
                    recipient_type=RecipientType.AGENT.value,
                    recipient_id=UUID(params.delegating_agent_id),
                    message_type=message_type.value,
                    content_json=content,
                    visibility=MessageVisibility.VISIBLE.value,
                )
            )
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type=ActorType.AGENT.value,
                    actor_id=agent_id,
                    action=(
                        "task.review_completed"
                        if params.kind == "review_request"
                        else "task.delegation_completed"
                    ),
                    target_type="task",
                    target_id=child_task_id,
                    metadata_json={
                        "parent_task_id": params.parent_task_id,
                        "status": status,
                        "verdict": verdict,
                        "run_status": params.run_status,
                        "reported": was_reported,
                    },
                )
            )
            await session.commit()

        await self._publish(
            workspace_id,
            "task.delegation_completed",
            {
                "parent_task_id": params.parent_task_id,
                "child_task_id": params.child_task_id,
                "kind": params.kind,
                "status": status,
                "verdict": verdict,
            },
        )
        return DelegationSummary(
            task_id=params.child_task_id,
            status=status,
            summary=summary_text,
            artifacts=[dict(item) for item in artifacts if isinstance(item, dict)],
            risks=risks,
            recommended_next_action=next_action,
            verdict=verdict,
            reported=was_reported,
        )

    @activity.defn(name=ACTIVITY_DELIVER_DELEGATION_RESULT)
    async def deliver_delegation_result_activity(
        self, params: DeliverDelegationResultInput
    ) -> None:
        """Resume a run parked on a blocking delegation: stitch the child's
        summary into the transcript as the delegate_task call's observation
        (plan 7.6 — the summary, never the child's transcript)."""
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        summary = asdict(params.summary)
        safe_provider_call_id = redact_text(params.provider_call_id)[:200]
        legacy_binding = not params.gateway_tool_call_id
        if params.gateway_tool_call_id:
            try:
                tool_call_id = str(UUID(params.gateway_tool_call_id))
            except ValueError:
                raise ApplicationError(
                    "delegation result has an invalid canonical tool call identity",
                    type="delegation_tool_call_binding_invalid",
                    non_retryable=True,
                ) from None
        else:
            tool_call_id = safe_provider_call_id
        if not tool_call_id:
            raise ApplicationError(
                "delegation result is missing its canonical tool call identity",
                type="delegation_tool_call_binding_missing",
                non_retryable=True,
            )

        async with self._resources.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.task_id == task_id,
                    AgentRun.agent_id == UUID(params.agent_id),
                )
                .with_for_update()
            )
            if run is None:
                raise ApplicationError(
                    "delegation result does not match its parent run",
                    type="delegation_run_binding_mismatch",
                    non_retryable=True,
                )
            existing_results = await session.scalars(
                select(Message).where(
                    Message.workspace_id == workspace_id,
                    Message.task_id == task_id,
                    Message.run_id == run_id,
                    Message.message_type == "tool_result",
                )
            )
            existing = next(
                (
                    message
                    for message in existing_results
                    if message.content_json.get("tool_call_id") == tool_call_id
                ),
                None,
            )
            if existing is not None:
                matching_events = await session.scalars(
                    select(RunEvent).where(
                        RunEvent.workspace_id == workspace_id,
                        RunEvent.task_id == task_id,
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == "delegation.result",
                    )
                )
                existing_event = next(
                    (
                        event
                        for event in matching_events
                        if event.payload_json.get("tool_call_id") == tool_call_id
                    ),
                    None,
                )
                if existing_event is None or (
                    existing_event.payload_json.get("child_task_id") != params.child_task_id
                    or existing_event.payload_json.get("kind") != params.kind
                ):
                    raise ApplicationError(
                        "delegation result does not match its canonical tool call bundle",
                        type="delegation_result_binding_mismatch",
                        non_retryable=True,
                    )
            run.status = RunStatus.RUNNING.value
            if existing is None:
                self._add_tool_message(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=UUID(params.agent_id),
                    message_type="tool_result",
                    content={
                        "tool_call_id": tool_call_id,
                        "provider_call_id": safe_provider_call_id,
                        "tool_name": "organization.delegate_task",
                        "status": "executed",
                        "result": json.dumps(summary, ensure_ascii=False),
                    },
                )
                seq = await self._next_seq(session, run_id)
                self._add_run_event(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    seq=seq,
                    event_type="delegation.result",
                    payload={
                        "child_task_id": params.child_task_id,
                        "kind": params.kind,
                        "status": params.summary.status,
                        "verdict": params.summary.verdict,
                        "reported": params.summary.reported,
                        "tool_call_id": tool_call_id,
                    },
                )
                if legacy_binding:
                    session.add(
                        AuditEvent(
                            workspace_id=workspace_id,
                            actor_type=ActorType.AGENT.value,
                            actor_id=UUID(params.agent_id),
                            action="delegation.legacy_tool_call_binding",
                            target_type="agent_run",
                            target_id=run_id,
                            metadata_json={
                                "task_id": params.task_id,
                                "child_task_id": params.child_task_id,
                            },
                        )
                    )
            await session.commit()

        await self._publish(
            workspace_id,
            "agent.run.resumed",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "child_task_id": params.child_task_id,
                "delegation_status": params.summary.status,
            },
        )
