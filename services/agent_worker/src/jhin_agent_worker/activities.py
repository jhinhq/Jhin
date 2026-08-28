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
from jhin_agent_worker.reasoning import (
    CONVERSATION_HISTORY_MAX_CHARS as CONVERSATION_HISTORY_MAX_CHARS,
)
from jhin_agent_worker.reasoning import (
    CONVERSATION_HISTORY_MAX_MESSAGES as CONVERSATION_HISTORY_MAX_MESSAGES,
)
from jhin_agent_worker.reasoning import (
    CONVERSATION_HISTORY_OMITTED_MARKER as CONVERSATION_HISTORY_OMITTED_MARKER,
)
from jhin_agent_worker.reasoning import (
    CONVERSATION_UNANSWERED_MARKER as CONVERSATION_UNANSWERED_MARKER,
)
from jhin_agent_worker.reasoning import AgentReasoningActivities
from jhin_agent_worker.resources import Resources
from jhin_agents import resolve_snapshot
from jhin_agents.snapshot import SnapshotError
from jhin_db.budget import budget_denial_message
from jhin_db.models import (
    Agent,
    AgentRun,
    AuditEvent,
    Message,
    RunEvent,
    Task,
    User,
    UserQuestion,
    Workspace,
)
from jhin_domain import (
    RUN_ACTIVE_STATUSES,
    ActorType,
    MemoryScope,
    MessageType,
    MessageVisibility,
    RecipientType,
    RunStatus,
    SenderType,
    TaskState,
    UserQuestionStatus,
    new_uuid7,
    structured_content,
)
from jhin_observability import get_logger
from jhin_secrets.redaction import redact_text
from jhin_tools.ask_person import PERSON_ANSWER_WAIT
from jhin_workflows.agent_task import (
    ACTIVITY_DELIVER_QUESTION_ANSWER,
    ACTIVITY_RESOLVE_SNAPSHOT,
    AgentTaskInput,
    DeliverQuestionAnswerInput,
    SnapshotResult,
)
from jhin_workflows.delegated_task import (
    ACTIVITY_DELIVER_DELEGATION_RESULT,
    ACTIVITY_SUMMARIZE_DELEGATION,
    DelegationSummary,
    DeliverDelegationResultInput,
    SummarizeDelegationInput,
)
from jhin_workflows.memory_maintenance import (
    SOURCE_KIND_MESSAGE,
    MemoryMaintenanceInput,
    start_memory_maintenance,
)

logger = get_logger(__name__)

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


# --- asking a person, and hearing back ---

_ASK_PERSON_TOOL = "organization.ask_person"
_ASK_WAIT_MINUTES = int(PERSON_ANSWER_WAIT.total_seconds() // 60)

_DETAIL_QUESTION_TIMED_OUT = (
    f"Nobody answered within {_ASK_WAIT_MINUTES} minutes. Say plainly that you asked and "
    "did not hear back, state the assumption you are going with, and carry on. "
    "Do not ask this again in this run."
)
_DETAIL_GRANT_FREE_TEXT = (
    "They typed their own answer rather than picking a scope, so nothing wider "
    "than your own memory is authorised. Remember it at 'agent' scope, quote "
    "what they said, and offer to ask again with the right options."
)


def _grant_denied_detail(reason: str, *, who: str, answer: str) -> str:
    if reason == "free_text_answer":
        return _DETAIL_GRANT_FREE_TEXT
    if reason == "insufficient_authority":
        return (
            f"{who} chose {answer}, but recording memory at that scope needs an "
            "admin. Remember it at 'agent' scope, tell them plainly that an "
            "admin has to record it more widely, and point them at the "
            "Memories page."
        )
    return (
        "Their answer did not authorise a wider memory. Remember it at 'agent' "
        "scope and say who could record it more widely."
    )


def _memory_scope_grant(question: UserQuestion, *, who: str) -> dict[str, Any]:
    """What the answer authorises, in the model's next-step vocabulary.

    Read from the row's ``granted_*`` columns, which only the API writes and
    only from the answering person's RBAC. The model is handed a question id
    to cite, never a scope it may assert.
    """
    if question.granted_scope:
        scope = MemoryScope(question.granted_scope)
        return {
            "granted_scope": scope.value,
            "authorized_by_question_id": str(question.id),
            "detail": (
                f"{who} authorised a {scope.value}-scoped memory. Call "
                f"memory.propose once with requested_scope '{scope.value}' and "
                "authorized_by_question_id set to this question id."
            ),
        }
    return {
        "granted_scope": "",
        "denied_reason": question.grant_denied_reason or "scope_not_authorized",
        "detail": _grant_denied_detail(
            question.grant_denied_reason, who=who, answer=question.answer_text or "that scope"
        ),
    }


def _question_observation(question: UserQuestion, *, who: str) -> dict[str, Any]:
    """The JSON the model reads back as the ask's result.

    It re-enters the prompt behind ``UNTRUSTED_LABEL``, which is exactly
    right: a person's typed words are data, and the authority they carry
    lives in the row, not in this text.
    """
    if question.status != UserQuestionStatus.ANSWERED.value:
        return {
            "status": "timed_out",
            "question_id": str(question.id),
            "detail": _DETAIL_QUESTION_TIMED_OUT,
        }
    if question.answer_kind == "other":
        detail = f"{who} answered in their own words: {question.answer_text}"
    else:
        detail = f"{who} answered: {question.answer_text}. Use this and reply to them yourself."
    observation: dict[str, Any] = {
        "status": "answered",
        "question_id": str(question.id),
        "answer_kind": question.answer_kind,
        "option_value": question.answer_option_value,
        "answer": question.answer_text,
        "answered_by": who,
        "detail": detail,
    }
    # Only a scope question has a scope to grant. Attaching one to an open
    # question would invite the model to remember something nobody discussed.
    if question.kind == "memory_scope":
        observation["memory_scope_grant"] = _memory_scope_grant(question, who=who)
    return observation


def _question_tool_call_id(params: DeliverQuestionAnswerInput) -> str:
    if params.gateway_tool_call_id:
        try:
            return str(UUID(params.gateway_tool_call_id))
        except ValueError:
            raise ApplicationError(
                "question answer has an invalid canonical tool call identity",
                type="question_tool_call_binding_invalid",
                non_retryable=True,
            ) from None
    fallback: str = redact_text(params.provider_call_id)[:200]
    if not fallback:
        raise ApplicationError(
            "question answer is missing its canonical tool call identity",
            type="question_tool_call_binding_missing",
            non_retryable=True,
        )
    return fallback


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
                # Budget admission (plan 15.5): a spent monthly budget is a
                # hard stop, checked before any concurrency slot is claimed.
                # The workflow fails the task visibly — budgets never queue.
                denial = await budget_denial_message(
                    session,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    agent_name=agent.name,
                    agent_budget_cents=agent.monthly_budget_cents,
                    workspace_settings_json=(
                        workspace.settings_json if workspace is not None else None
                    ),
                )
                if denial is not None:
                    await session.rollback()  # release the admission row locks
                    return SnapshotResult(
                        run_id="",
                        snapshot_json="",
                        snapshot_hash="",
                        max_steps=0,
                        denied_code="budget_exceeded",
                        denied_message=denial,
                    )
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
                    if task.state != TaskState.PAUSED.value:
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
                # A pause asked for while the task was still waiting is already
                # on the row; the workflow parks on it the moment the step loop
                # starts, so admission must not advertise it as running.
                if task.state != TaskState.PAUSED.value:
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
            parent = await session.scalar(
                select(Task).where(Task.id == parent_task_id, Task.workspace_id == workspace_id)
            )
            parent_conversation_id = parent.conversation_id if parent is not None else None

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
            result_message_id = new_uuid7()
            session.add(
                Message(
                    id=result_message_id,
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
        # The delegating (parent/manager) agent learns from the reported
        # result: detached maintenance keyed to the result message id.
        await self._start_result_memory_maintenance(
            workspace_id=params.workspace_id,
            agent_id=params.delegating_agent_id,
            message_id=str(result_message_id),
            task_id=params.parent_task_id,
            conversation_id=str(parent_conversation_id) if parent_conversation_id else "",
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

    async def _start_result_memory_maintenance(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        message_id: str,
        task_id: str,
        conversation_id: str,
    ) -> None:
        """Detached memory maintenance over a structured result message so
        the receiving agent learns from what was reported to it. Best-effort
        by contract: the summarize projection is already committed and no
        failure here may surface into the delegation."""
        client = self._temporal_client
        if client is None or not agent_id or not message_id:
            return
        try:
            status, _handle = await start_memory_maintenance(
                client,
                MemoryMaintenanceInput(
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    source_kind=SOURCE_KIND_MESSAGE,
                    source_id=message_id,
                    task_id=task_id,
                    conversation_id=conversation_id,
                ),
            )
        except Exception as error:
            logger.warning("memory.maintenance_start_failed", error_type=type(error).__name__)
            return
        logger.info("memory.maintenance_start", status=status, message_id=message_id)

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

    @activity.defn(name=ACTIVITY_DELIVER_QUESTION_ANSWER)
    async def deliver_question_answer_activity(self, params: DeliverQuestionAnswerInput) -> None:
        """Resume a run parked on a question: write the person's answer — or
        a plain "nobody answered" — as the ask's one and only observation.

        The step projection deliberately suppressed the ask's ``tool_result``
        (two rows for one call become two ``tool_use_id``-matched blocks and
        the provider rejects the request), so this is where it lands, and it
        is idempotent on the tool call id across an activity retry.
        """
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        agent_id = UUID(params.agent_id)
        question_id = UUID(params.question_id)
        tool_call_id = _question_tool_call_id(params)

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
                    "question answer does not match its run",
                    type="question_run_binding_mismatch",
                    non_retryable=True,
                )
            question = await session.scalar(
                select(UserQuestion)
                .where(
                    UserQuestion.id == question_id,
                    UserQuestion.workspace_id == workspace_id,
                    UserQuestion.run_id == run_id,
                    UserQuestion.agent_id == agent_id,
                )
                .with_for_update()
            )
            if question is None:
                raise ApplicationError(
                    "question answer does not match the question that was asked",
                    type="question_binding_mismatch",
                    non_retryable=True,
                )
            # The timer fired — but a question answered while it was firing is
            # answered, and Postgres is the authority. Only a still-pending row
            # becomes expired.
            pending = question.status == UserQuestionStatus.PENDING.value
            if params.outcome == "timed_out" and pending:
                question.status = UserQuestionStatus.EXPIRED.value

            who = "They"
            if question.answered_by_user_id is not None:
                answerer = await session.get(User, question.answered_by_user_id)
                if answerer is not None and answerer.display_name:
                    who = answerer.display_name
            observation = _question_observation(question, who=who)

            existing_results = await session.scalars(
                select(Message).where(
                    Message.workspace_id == workspace_id,
                    Message.task_id == task_id,
                    Message.run_id == run_id,
                    Message.message_type == "tool_result",
                )
            )
            delivered = any(
                message.content_json.get("tool_call_id") == tool_call_id
                for message in existing_results
            )
            run.status = RunStatus.RUNNING.value
            if not delivered:
                self._add_tool_message(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    message_type="tool_result",
                    content={
                        "tool_call_id": tool_call_id,
                        "provider_call_id": redact_text(params.provider_call_id)[:200],
                        "tool_name": _ASK_PERSON_TOOL,
                        "status": "executed",
                        "result": json.dumps(observation, ensure_ascii=False),
                    },
                )
                seq = await self._next_seq(session, run_id)
                self._add_run_event(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    seq=seq,
                    event_type=(
                        "question.answered"
                        if question.status == UserQuestionStatus.ANSWERED.value
                        else "question.expired"
                    ),
                    # Never the question or the answer text: those live in the
                    # message and the question row, where forgetting a
                    # conversation removes them.
                    payload={
                        "question_id": str(question.id),
                        "tool_call_id": tool_call_id,
                        "answer_kind": question.answer_kind,
                        "option_value": question.answer_option_value,
                        "granted_scope": question.granted_scope,
                    },
                )
            # Repair path only: the API stamps the card in the same
            # transaction as the answer. This catches a commit whose own
            # message update raced, and is otherwise a no-op.
            if question.message_id is not None:
                card = await session.get(Message, question.message_id)
                if card is not None and card.content_json.get("status") != question.status:
                    card.content_json = {
                        **card.content_json,
                        "status": question.status,
                    }
            await session.commit()

        logger.info(
            "question.delivered",
            question_id=params.question_id,
            outcome=params.outcome,
            status=observation["status"],
        )
        await self._publish(
            workspace_id,
            "agent.run.resumed",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "question_id": params.question_id,
                "question_status": observation["status"],
            },
        )
