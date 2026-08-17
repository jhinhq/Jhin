"""Agent run activities (plan 8.2): snapshot resolution, one reasoning step,
and final persistence.

Postgres is written first and is the source of truth; NATS publishes are
best-effort transport for live UI (plan 2.3, 9.1). Model credentials are
decrypted here — inside the activity, at the moment of use — and exist only
in process memory (plan 13.5). Error strings pass through the secret
redactor before persisting or raising.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_agents import AgentExecutionSnapshot, resolve_snapshot
from jhin_agents.context import ConversationTurn, TaskContext
from jhin_agents.runtime import estimate_cost_micros, execute_step
from jhin_agents.snapshot import SnapshotError
from jhin_connectors import build_default_catalog
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    Message,
    RunEvent,
    Task,
    ToolCall,
)
from jhin_domain import (
    ApprovalStatus,
    MessageVisibility,
    RecipientType,
    RunStatus,
    SenderType,
    TaskState,
    ToolCallStatus,
)
from jhin_events import EventEnvelope, EventSource
from jhin_models import ModelProviderError, ModelToolCall, ToolSchema, build_model_client
from jhin_models.factory import ProviderConfigError
from jhin_observability import get_logger
from jhin_policy import Grant, GrantEffect
from jhin_secrets import SecretStore
from jhin_secrets.redaction import redact_text
from jhin_tools import (
    GatewayOutcome,
    ToolExecutionContext,
    ToolGateway,
    allowed_tool_definitions,
)
from jhin_workflows.agent_task import (
    ACTIVITY_FINALIZE_RUN,
    ACTIVITY_RESOLVE_APPROVAL,
    ACTIVITY_RESOLVE_SNAPSHOT,
    ACTIVITY_RUN_AGENT_STEP,
    AgentTaskInput,
    FinalizeInput,
    ResolveApprovalInput,
    RunStepInput,
    SnapshotResult,
    StepResult,
)

# Cap on persisted model-produced tool arguments (they re-enter the prompt).
_MAX_ARGUMENTS_CHARS = 8_192

logger = get_logger(__name__)


class AgentActivities:
    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    # --- Helpers ---

    async def _publish(self, workspace_id: UUID, event_type: str, data: dict[str, Any]) -> None:
        """Best-effort NATS publish; the database already holds the fact."""
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
                "events.publish_failed", event_type=event_type, error=f"{type(exc).__name__}"
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

    async def _advertised_tools(
        self, session: AsyncSession, workspace_id: UUID, agent_id: UUID
    ) -> tuple[ToolSchema, ...]:
        """Tool schemas for the model request, filtered by live allow grants.

        Advertising is prompt economy only — authorization happens in the
        gateway on every call (plan 52)."""
        rows = await session.scalars(
            select(AgentCapabilityGrant).where(
                AgentCapabilityGrant.agent_id == agent_id,
                AgentCapabilityGrant.workspace_id == workspace_id,
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
            except ValueError:
                continue
        definitions = allowed_tool_definitions(build_default_catalog(), grants)
        return tuple(
            ToolSchema(
                name=definition.name,
                description=definition.description,
                parameters=definition.input_json_schema(),
            )
            for definition in definitions
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
        """Internal transcript row for the tool-calling exchange; rebuilt into
        provider messages on the next reasoning step."""
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

    def _record_gateway_result(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID,
        seq: int,
        step_index: int,
        call: ModelToolCall,
        result: GatewayOutcome,
    ) -> int:
        """Emit the run events for one gateway decision (plan 7.3 nodes)."""
        base: dict[str, Any] = {
            "step": step_index,
            "tool_name": result.tool_name,
            "tool_call_id": str(result.tool_call_id),
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
                {"status": result.status, "duration_ms": result.duration_ms},
            )
            emit("node.observe", {"chars": len(result.observation_json())})
        elif result.status == "denied":
            emit("node.observe", {"denied": True, "reason": result.decision_reason})
        elif result.status == "needs_approval":
            emit(
                "node.request_approval",
                {"approval_id": str(result.approval_id), "reason": result.decision_reason},
            )
        emit(
            "tool.call",
            {
                "status": result.status,
                "decision": result.decision_code,
                "reason": result.decision_reason,
                "error_code": result.error_code,
                "duration_ms": result.duration_ms,
                "approval_id": str(result.approval_id) if result.approval_id else None,
            },
        )
        return seq

    # --- Activities ---

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot_activity(self, params: AgentTaskInput) -> SnapshotResult:
        workspace_id = UUID(params.workspace_id)
        agent_id = UUID(params.agent_id)
        task_id = UUID(params.task_id)
        info = activity.info()

        async with self._resources.session_factory() as session:
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
            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is not None:
                task.state = TaskState.RUNNING.value
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

    @activity.defn(name=ACTIVITY_RUN_AGENT_STEP)
    async def run_agent_step_activity(self, params: RunStepInput) -> StepResult:
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        snapshot = AgentExecutionSnapshot.model_validate_json(params.snapshot_json)

        async with self._resources.session_factory() as session:
            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is None:
                raise ApplicationError("task not found", type="task_not_found", non_retryable=True)

            history = await self._load_history(session, task)

            # Decrypt the provider credential at the moment of use (13.5).
            api_key: str | None = None
            if snapshot.model_profile.secret_id is not None:
                api_key = await SecretStore(session, self._resources.crypto).reveal(
                    workspace_id, snapshot.model_profile.secret_id
                )

            try:
                client = build_model_client(
                    snapshot.model_profile.provider_type,
                    base_url=snapshot.model_profile.base_url,
                    api_key=api_key,
                )
            except ProviderConfigError as exc:
                raise ApplicationError(
                    redact_text(str(exc)), type="provider_config", non_retryable=True
                ) from exc
            del api_key  # plaintext lives only until the client holds it

            tools = await self._advertised_tools(session, workspace_id, UUID(params.agent_id))

            try:
                outcome = await execute_step(
                    client,
                    snapshot,
                    TaskContext(
                        title=task.title,
                        description=task.description,
                        history=history,
                        user_instructions=tuple(params.user_instructions),
                    ),
                    tools=tools,
                )
            except ModelProviderError as exc:
                raise ApplicationError(
                    redact_text(str(exc)),
                    type="model_provider_error",
                    non_retryable=not exc.retryable,
                ) from exc
            finally:
                await client.close()

            cost_micros = estimate_cost_micros(
                outcome.usage,
                snapshot.model_profile.input_cost_micros_per_million,
                snapshot.model_profile.output_cost_micros_per_million,
            )

            # Persist everything in one transaction; commit is the final
            # failable step so activity retries cannot double-write.
            run = await session.get(AgentRun, run_id)
            if run is not None:
                run.input_tokens += outcome.usage.input_tokens
                run.output_tokens += outcome.usage.output_tokens
                run.cached_tokens += outcome.usage.cached_tokens
                run.estimated_cost_micros += cost_micros
                run.steps_used = params.step_index + 1

            if not outcome.tool_calls:
                session.add(
                    Message(
                        workspace_id=workspace_id,
                        task_id=task_id,
                        run_id=run_id,
                        sender_type=SenderType.AGENT.value,
                        sender_id=UUID(params.agent_id),
                        recipient_type=RecipientType.TASK.value,
                        recipient_id=task_id,
                        message_type="text",
                        content_json={
                            "text": outcome.text,
                            "finish_reason": outcome.finish_reason,
                        },
                        visibility=MessageVisibility.VISIBLE.value,
                    )
                )

            seq = await self._next_seq(session, run_id)
            for transition in outcome.transitions:
                payload: dict[str, Any] = {"detail": transition.detail, "step": params.step_index}
                if transition.node == "reason":
                    payload.update(
                        {
                            "model": outcome.model,
                            "input_tokens": outcome.usage.input_tokens,
                            "output_tokens": outcome.usage.output_tokens,
                            "cached_tokens": outcome.usage.cached_tokens,
                            "cost_micros": cost_micros,
                            "latency_ms": outcome.latency_ms,
                            "provider_request_id": outcome.provider_request_id,
                        }
                    )
                self._add_run_event(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    seq=seq,
                    event_type=f"node.{transition.node}",
                    payload=payload,
                )
                seq += 1

            # Tool branch: every requested call goes through the gateway —
            # the single authorization path (plan 12). Unprocessed calls
            # after an approval park simply vanish from the transcript; the
            # model may re-request them once the run resumes.
            waiting_approval_id: str | None = None
            parked: GatewayOutcome | None = None
            if outcome.tool_calls:
                gateway = ToolGateway(
                    ToolExecutionContext(
                        session=session,
                        workspace_id=workspace_id,
                        task_id=task_id,
                        run_id=run_id,
                        agent_id=UUID(params.agent_id),
                        agent_name=snapshot.name,
                        crypto=self._resources.crypto,
                    ),
                    build_default_catalog(),
                )
                for call in outcome.tool_calls:
                    self._add_tool_message(
                        session,
                        workspace_id=workspace_id,
                        task_id=task_id,
                        run_id=run_id,
                        agent_id=UUID(params.agent_id),
                        message_type="tool_call",
                        content={
                            "text": outcome.text,
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "arguments_json": redact_text(call.arguments_json)[
                                :_MAX_ARGUMENTS_CHARS
                            ],
                        },
                    )
                    result = await gateway.request(
                        call.name, call.arguments_json, provider_call_id=call.id
                    )
                    seq = self._record_gateway_result(
                        session,
                        workspace_id=workspace_id,
                        run_id=run_id,
                        task_id=task_id,
                        seq=seq,
                        step_index=params.step_index,
                        call=call,
                        result=result,
                    )
                    if result.status == "needs_approval":
                        waiting_approval_id = str(result.approval_id)
                        parked = result
                        if run is not None:
                            run.status = RunStatus.WAITING_APPROVAL.value
                        break
                    self._add_tool_message(
                        session,
                        workspace_id=workspace_id,
                        task_id=task_id,
                        run_id=run_id,
                        agent_id=UUID(params.agent_id),
                        message_type="tool_result",
                        content={
                            "tool_call_id": call.id,
                            "tool_name": call.name,
                            "status": result.status,
                            "result": result.observation_json(),
                        },
                    )
            await session.commit()

        if waiting_approval_id is not None and parked is not None:
            await self._publish(
                workspace_id,
                "approval.requested",
                {
                    "approval_id": waiting_approval_id,
                    "run_id": params.run_id,
                    "task_id": params.task_id,
                    "agent_id": params.agent_id,
                    "tool_name": parked.tool_name,
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
        await self._publish(
            workspace_id,
            "agent.run.step",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "step": params.step_index,
                "done": outcome.done,
            },
        )
        return StepResult(
            done=outcome.done,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            cached_tokens=outcome.usage.cached_tokens,
            cost_micros=cost_micros,
            waiting_approval_id=waiting_approval_id,
        )

    async def _load_history(
        self, session: AsyncSession, task: Task
    ) -> tuple[ConversationTurn, ...]:
        """Visible conversation plus the internal tool transcript, in order,
        so each reasoning step rebuilds the exact provider message sequence."""
        rows = await session.scalars(
            select(Message)
            .where(
                Message.task_id == task.id,
                or_(
                    Message.visibility == MessageVisibility.VISIBLE.value,
                    Message.message_type.in_(("tool_call", "tool_result")),
                ),
            )
            .order_by(Message.created_at, Message.id)
        )
        turns: list[ConversationTurn] = []
        for message in rows:
            content = message.content_json
            if message.message_type == "tool_call":
                turns.append(
                    ConversationTurn(
                        role="agent",
                        text=str(content.get("text", "") or ""),
                        kind="tool_call",
                        tool_call_id=str(content.get("tool_call_id", "")),
                        tool_name=str(content.get("tool_name", "")),
                        arguments_json=str(content.get("arguments_json", "{}")),
                    )
                )
                continue
            if message.message_type == "tool_result":
                turns.append(
                    ConversationTurn(
                        role="agent",
                        text=str(content.get("result", "")),
                        kind="tool_result",
                        tool_call_id=str(content.get("tool_call_id", "")),
                        tool_name=str(content.get("tool_name", "")),
                    )
                )
                continue
            text = str(content.get("text", ""))
            if not text:
                continue
            role = "agent" if message.sender_type == SenderType.AGENT.value else "user"
            # The initial user message often duplicates the task description;
            # skip it so the prompt does not repeat itself.
            if not turns and role == "user" and text.strip() == task.description.strip():
                continue
            turns.append(ConversationTurn(role=role, text=text))
        return tuple(turns)

    @activity.defn(name=ACTIVITY_RESOLVE_APPROVAL)
    async def resolve_approval_activity(self, params: ResolveApprovalInput) -> StepResult:
        """Execute or record the outcome of a human-decided approval.

        The Postgres approval row is the authority; the signal's decision is
        routing advice only (plan 52)."""
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        agent_id = UUID(params.agent_id)
        approval_id = UUID(params.approval_id)

        async with self._resources.session_factory() as session:
            approval = await session.scalar(
                select(Approval).where(
                    Approval.id == approval_id, Approval.workspace_id == workspace_id
                )
            )
            if approval is None:
                raise ApplicationError(
                    "approval not found", type="approval_not_found", non_retryable=True
                )
            if approval.status == ApprovalStatus.PENDING.value:
                # The API commits the decision before signaling; a pending row
                # here is a transient read race — retry.
                raise ApplicationError("approval still pending", type="approval_pending")

            agent = await session.scalar(
                select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
            )
            gateway = ToolGateway(
                ToolExecutionContext(
                    session=session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    agent_name=agent.name if agent is not None else "agent",
                    crypto=self._resources.crypto,
                ),
                build_default_catalog(),
            )
            if approval.status == ApprovalStatus.APPROVED.value:
                result = await gateway.resolve_approved(approval_id)
            else:
                result = await gateway.resolve_rejected(approval_id)

            provider_call_id = str(
                approval.action_payload_sanitized.get("provider_call_id", "") or ""
            )
            self._add_tool_message(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
                agent_id=agent_id,
                message_type="tool_result",
                content={
                    "tool_call_id": provider_call_id,
                    "tool_name": result.tool_name,
                    "status": result.status,
                    "result": result.observation_json(),
                },
            )

            run = await session.get(AgentRun, run_id)
            if run is not None and run.workspace_id == workspace_id:
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
                    "tool_name": result.tool_name,
                    "status": result.status,
                    "decided_by_user_id": (
                        str(approval.decided_by_user_id) if approval.decided_by_user_id else None
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
                        "tool_name": result.tool_name,
                        "status": result.status,
                        "duration_ms": result.duration_ms,
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
                    "tool_name": result.tool_name,
                    "tool_call_id": str(result.tool_call_id),
                    "risk": result.risk,
                    "status": result.status,
                    "decision": result.decision_code,
                    "reason": result.decision_reason,
                    "error_code": result.error_code,
                    "duration_ms": result.duration_ms,
                    "approval_id": params.approval_id,
                },
            )
            await session.commit()

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

    @activity.defn(name=ACTIVITY_FINALIZE_RUN)
    async def finalize_run_activity(self, params: FinalizeInput) -> None:
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        error_message = redact_text(params.error_message) if params.error_message else None
        run_totals: dict[str, Any] = {}

        async with self._resources.session_factory() as session:
            if params.run_id is not None:
                # A run that ends while an approval is still pending orphans
                # it: mark it cancelled so the inbox stays truthful.
                pending = await session.scalars(
                    select(Approval).where(
                        Approval.run_id == UUID(params.run_id),
                        Approval.workspace_id == workspace_id,
                        Approval.status == ApprovalStatus.PENDING.value,
                    )
                )
                for approval in pending:
                    approval.status = ApprovalStatus.CANCELLED.value
                    approval.decided_at = datetime.now(UTC)
                    stale_call = await session.scalar(
                        select(ToolCall).where(
                            ToolCall.approval_id == approval.id,
                            ToolCall.workspace_id == workspace_id,
                        )
                    )
                    if stale_call is not None:
                        stale_call.status = ToolCallStatus.REJECTED.value
                        stale_call.completed_at = datetime.now(UTC)
                        stale_call.error_code = "run_ended"

                run = await session.get(AgentRun, UUID(params.run_id))
                if run is not None and run.workspace_id == workspace_id:
                    run.status = params.status
                    run.completed_at = datetime.now(UTC)
                    run.steps_used = params.steps_used
                    run.error_code = params.error_code
                    run.error_message = error_message
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
                            "error_code": params.error_code,
                            "error_message": error_message,
                        },
                    )

            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is not None:
                task.state = params.status
                if error_message:
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
                                "text": f"Run {params.status}: {error_message}",
                                "error_code": params.error_code,
                            },
                            visibility=MessageVisibility.VISIBLE.value,
                        )
                    )
            await session.commit()

        await self._publish(
            workspace_id,
            f"agent.run.{params.status}",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "error_code": params.error_code,
                **run_totals,
            },
        )
        await self._publish(
            workspace_id,
            f"task.{params.status}",
            {"task_id": params.task_id, "run_id": params.run_id},
        )
