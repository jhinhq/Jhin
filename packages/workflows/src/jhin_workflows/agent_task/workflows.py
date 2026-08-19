"""AgentTaskWorkflow (plan 8.2): the durable spine of one agent-owned task.

Temporal owns everything around the run — pause/resume/cancel signals, step
budget, retries, and final persistence. New histories split model reasoning
from deterministic effects across the agent and tool queues; frozen Phase 9
histories retain their recorded agent-worker activity commands. This file must
stay deterministic and free of I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_COMMIT_AGENT_STEP,
    ACTIVITY_COMMIT_APPROVAL_PROJECTION,
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_FINALIZE_RUN,
    ACTIVITY_FINALIZE_RUN_PROJECTION,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_APPROVAL,
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
    ACTIVITY_RESOLVE_SNAPSHOT,
    ACTIVITY_RUN_AGENT_STEP,
    PHASE10_TOOL_WORKER_PATCH,
    AdvertisedTool,
    AgentTaskInput,
    AgentTaskResult,
    AgentTaskStatus,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    DelegationRequest,
    ExecuteBoundToolInput,
    FinalizeInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    ResolveApprovalInput,
    ResolveBoundToolApprovalInput,
    RunStepInput,
    SnapshotResult,
    StepResult,
)
from jhin_workflows.delegated_task.shared import (
    ACTIVITY_DELIVER_DELEGATION_RESULT,
    DelegatedTaskInput,
    DelegatedTaskResult,
    DeliverDelegationResultInput,
)
from jhin_workflows.task_queues import AGENT_TASK_QUEUE, TOOL_TASK_QUEUE

# Queue-admission poll cadence (plan 30). finalize_run signals queued
# workflows when a slot frees (slot_available), so this timer is only the
# correctness backstop against missed kicks.
_QUEUE_POLL_INTERVAL = timedelta(seconds=30)

# Bounded exponential backoff; Temporal adds jitter (plan 8.6).
_SNAPSHOT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=3,
)
_STEP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)
_FINALIZE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)
_RESOLVE_APPROVAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)


@workflow.defn(name="AgentTaskWorkflow")
class AgentTaskWorkflow:
    def __init__(self) -> None:
        self._status = "starting"
        self._waiting_reason: str | None = None
        self._paused = False
        self._cancelled = False
        self._steps_used = 0
        self._pending_instructions: list[str] = []
        # approval_id -> decision ("approved" | "rejected"), delivered by the
        # approval_decision signal from the API.
        self._approval_decisions: dict[str, str] = {}
        # Set by the slot_available signal: a concurrency slot may have freed
        # (plan 30); wakes the queued admission loop early.
        self._slot_kick = False

    # --- Signals (plan 8.2) ---

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.signal
    def cancel(self) -> None:
        self._cancelled = True

    @workflow.signal
    def user_instruction(self, text: str) -> None:
        if text:
            self._pending_instructions.append(text)

    @workflow.signal
    def approval_decision(self, approval_id: str, decision: str) -> None:
        """A human decided an approval. The signal only wakes the workflow;
        the activity re-reads the Postgres approval row as the authority."""
        if approval_id and decision in ("approved", "rejected"):
            self._approval_decisions[approval_id] = decision

    @workflow.signal
    def slot_available(self) -> None:
        """A concurrency slot may have freed (plan 30). Advisory only: the
        admission activity re-checks Postgres, the sole authority."""
        self._slot_kick = True

    # --- Queries (plan 8.2) ---

    @workflow.query
    def status(self) -> AgentTaskStatus:
        return AgentTaskStatus(
            status=self._status,
            waiting_reason=self._waiting_reason,
            steps_used=self._steps_used,
            pending_instructions=len(self._pending_instructions),
        )

    def _cancel_requested(self) -> bool:
        """Re-read signal-owned cancellation state across activity awaits."""
        return self._cancelled

    # --- Run ---

    @workflow.run
    async def run(self, params: AgentTaskInput) -> AgentTaskResult:
        self._status = "resolving"
        totals = AgentTaskResult(run_id=None, status="running", steps_used=0)
        error_code: str | None = None
        error_message: str | None = None

        # Admission + snapshot (plan 30): the activity claims a concurrency
        # slot and creates the run in one transaction, or reports "queued".
        # Queued workflows park durably and re-check on a slot_available kick
        # or the poll timer — surviving worker restarts mid-queue.
        while True:
            try:
                snapshot: SnapshotResult = await workflow.execute_activity(
                    ACTIVITY_RESOLVE_SNAPSHOT,
                    params,
                    result_type=SnapshotResult,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_SNAPSHOT_RETRY,
                )
            except Exception as exc:
                self._status = "failed"
                await self._finalize(
                    params,
                    run_id=None,
                    status="failed",
                    error_code="snapshot_failed",
                    error_message=str(exc)[:2000],
                )
                return AgentTaskResult(run_id=None, status="failed", steps_used=0)
            if not snapshot.queued:
                break
            self._status = "queued"
            self._waiting_reason = f"queued:{snapshot.queue_reason}"
            self._slot_kick = False
            with contextlib.suppress(asyncio.TimeoutError):
                await workflow.wait_condition(
                    lambda: self._slot_kick or self._cancelled, timeout=_QUEUE_POLL_INTERVAL
                )
            if self._cancelled:
                self._status = "cancelled"
                await self._finalize(
                    params, run_id=None, status="cancelled", error_code=None, error_message=None
                )
                return AgentTaskResult(run_id=None, status="cancelled", steps_used=0)
        self._waiting_reason = None

        totals.run_id = snapshot.run_id
        use_tool_worker = workflow.patched(PHASE10_TOOL_WORKER_PATCH)
        done = False

        while not done and self._steps_used < snapshot.max_steps:
            if self._cancelled:
                break
            if self._paused:
                self._status = "paused"
                self._waiting_reason = "paused_by_user"
                await workflow.wait_condition(lambda: not self._paused or self._cancelled)
                self._waiting_reason = None
                continue

            self._status = "running"
            instructions = self._pending_instructions
            self._pending_instructions = []
            try:
                if use_tool_worker:
                    advertised = await workflow.execute_activity(
                        ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
                        ResolveAdvertisedToolsInput(
                            workspace_id=params.workspace_id,
                            agent_id=params.agent_id,
                        ),
                        result_type=list[AdvertisedTool],
                        task_queue=TOOL_TASK_QUEUE,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_STEP_RETRY,
                    )
                    reasoned = await workflow.execute_activity(
                        ACTIVITY_REASON_AGENT_STEP,
                        ReasonAgentStepInput(
                            workspace_id=params.workspace_id,
                            task_id=params.task_id,
                            run_id=snapshot.run_id,
                            agent_id=params.agent_id,
                            snapshot_json=snapshot.snapshot_json,
                            step_index=self._steps_used,
                            instruction=params.instruction,
                            user_instructions=instructions,
                            advertised_tools=advertised,
                        ),
                        result_type=ReasonAgentStepResult,
                        task_queue=AGENT_TASK_QUEUE,
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=_STEP_RETRY,
                    )
                    tool_ids: list[str] = []
                    for ordinal in range(reasoned.call_count):
                        if self._cancel_requested():
                            break
                        bound = await workflow.execute_activity(
                            ACTIVITY_EXECUTE_BOUND_TOOL,
                            ExecuteBoundToolInput(
                                workspace_id=params.workspace_id,
                                run_id=snapshot.run_id,
                                step_index=self._steps_used,
                                ordinal=ordinal,
                            ),
                            result_type=BoundToolResult,
                            task_queue=TOOL_TASK_QUEUE,
                            start_to_close_timeout=timedelta(minutes=10),
                            retry_policy=_STEP_RETRY,
                        )
                        tool_ids.append(bound.tool_call_id)
                        if bound.stop_reason is not None or self._cancel_requested():
                            break
                    step = await workflow.execute_activity(
                        ACTIVITY_COMMIT_AGENT_STEP,
                        CommitAgentStepInput(
                            workspace_id=params.workspace_id,
                            task_id=params.task_id,
                            run_id=snapshot.run_id,
                            agent_id=params.agent_id,
                            step_index=self._steps_used,
                            gateway_tool_call_ids=tool_ids,
                        ),
                        result_type=StepResult,
                        task_queue=AGENT_TASK_QUEUE,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_FINALIZE_RETRY,
                    )
                else:
                    step = await workflow.execute_activity(
                        ACTIVITY_RUN_AGENT_STEP,
                        RunStepInput(
                            workspace_id=params.workspace_id,
                            task_id=params.task_id,
                            run_id=snapshot.run_id,
                            agent_id=params.agent_id,
                            snapshot_json=snapshot.snapshot_json,
                            step_index=self._steps_used,
                            instruction=params.instruction,
                            user_instructions=instructions,
                        ),
                        result_type=StepResult,
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=_STEP_RETRY,
                    )
            except Exception as exc:
                error_code = "step_failed"
                error_message = str(exc)[:2000]
                break
            self._steps_used += 1
            totals.steps_used = self._steps_used
            totals.input_tokens += step.input_tokens
            totals.output_tokens += step.output_tokens
            totals.cost_micros += step.cost_micros
            done = step.done
            if step.execution_unknown_tool_call_id is not None:
                error_code = "tool_execution_unknown"
                error_message = (
                    f"tool call {step.execution_unknown_tool_call_id} execution outcome is "
                    "unknown; manual reconciliation is required"
                )
                break

            # Durable delegation (plan 7.5, 8.3): the tool executor persisted
            # the child task row; the workflow starts the child workflow and
            # parks on blocking ones like it parks on approvals.
            delegation_failed = False
            for request in step.delegations:
                try:
                    await self._run_delegation(params, snapshot.run_id, request)
                except Exception as exc:
                    error_code = "delegation_failed"
                    error_message = str(exc)[:2000]
                    delegation_failed = True
                    break
            if delegation_failed:
                break

            if step.waiting_approval_id is not None:
                # Durable approval wait (plan 8.2, 12.5): the tool call and
                # approval rows are already persisted; park until a human
                # decides. Workflow state survives worker restarts.
                approval_id = step.waiting_approval_id
                self._status = "waiting_approval"
                self._waiting_reason = f"approval:{approval_id}"

                def _decided(pending_id: str = approval_id) -> bool:
                    return pending_id in self._approval_decisions or self._cancelled

                await workflow.wait_condition(_decided)
                if approval_id not in self._approval_decisions:
                    break  # woken by cancel, not by a decision
                decision = self._approval_decisions.pop(approval_id)
                self._waiting_reason = None
                self._status = "running"
                try:
                    if use_tool_worker:
                        resolved = await workflow.execute_activity(
                            ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
                            ResolveBoundToolApprovalInput(
                                workspace_id=params.workspace_id,
                                task_id=params.task_id,
                                run_id=snapshot.run_id,
                                agent_id=params.agent_id,
                                approval_id=approval_id,
                            ),
                            result_type=BoundToolResult,
                            task_queue=TOOL_TASK_QUEUE,
                            start_to_close_timeout=timedelta(minutes=10),
                            retry_policy=_RESOLVE_APPROVAL_RETRY,
                        )
                        await workflow.execute_activity(
                            ACTIVITY_COMMIT_APPROVAL_PROJECTION,
                            CommitApprovalProjectionInput(
                                workspace_id=params.workspace_id,
                                task_id=params.task_id,
                                run_id=snapshot.run_id,
                                agent_id=params.agent_id,
                                approval_id=approval_id,
                                tool_call_id=resolved.tool_call_id,
                            ),
                            result_type=StepResult,
                            task_queue=AGENT_TASK_QUEUE,
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=_FINALIZE_RETRY,
                        )
                    else:
                        await workflow.execute_activity(
                            ACTIVITY_RESOLVE_APPROVAL,
                            ResolveApprovalInput(
                                workspace_id=params.workspace_id,
                                task_id=params.task_id,
                                run_id=snapshot.run_id,
                                agent_id=params.agent_id,
                                approval_id=approval_id,
                                decision=decision,
                            ),
                            result_type=StepResult,
                            start_to_close_timeout=timedelta(minutes=10),
                            retry_policy=_RESOLVE_APPROVAL_RETRY,
                        )
                except Exception as exc:
                    error_code = "approval_resolution_failed"
                    error_message = str(exc)[:2000]
                    break
                # Approved or rejected, the loop continues: the next reason
                # step sees the tool result or the recorded denial.

        if self._cancelled:
            final_status = "cancelled"
        elif error_code is not None:
            final_status = "failed"
        elif done:
            final_status = "completed"
        else:
            final_status = "failed"
            error_code = "max_steps_exceeded"
            error_message = f"run hit the {snapshot.max_steps}-step limit before finishing"

        self._status = "finalizing"
        await self._finalize(
            params,
            run_id=snapshot.run_id,
            status=final_status,
            error_code=error_code,
            error_message=error_message,
            use_tool_worker=use_tool_worker,
        )
        self._status = final_status
        totals.status = final_status
        return totals

    async def _run_delegation(
        self, params: AgentTaskInput, run_id: str, request: DelegationRequest
    ) -> None:
        """Start one DelegatedTaskWorkflow; await it when blocking.

        ABANDON keeps delegated work independent: cancelling this run never
        destroys a child in flight, and the child's summarize activity still
        persists the structured result message on this task either way.
        """
        handle = await workflow.start_child_workflow(
            "DelegatedTaskWorkflow",
            DelegatedTaskInput(
                workspace_id=params.workspace_id,
                parent_task_id=params.task_id,
                child_task_id=request.child_task_id,
                agent_id=request.target_agent_id,
                delegating_agent_id=params.agent_id,
                parent_run_id=run_id,
                kind=request.kind,
                blocking=request.blocking,
            ),
            id=f"delegated-{request.child_task_id}",
            parent_close_policy=ParentClosePolicy.ABANDON,
            result_type=DelegatedTaskResult,
        )
        if not request.blocking:
            return

        # Blocking wait (plan 8.3): park durably like an approval; a cancel
        # signal releases this run while the child continues abandoned.
        self._status = "waiting_delegation"
        self._waiting_reason = f"delegation:{request.child_task_id}"
        child_future: asyncio.Task[DelegatedTaskResult] = asyncio.ensure_future(handle)
        await workflow.wait_condition(lambda: child_future.done() or self._cancelled)
        if not child_future.done():
            return  # woken by cancel; the outer loop finalizes as cancelled
        delegated = child_future.result()
        self._waiting_reason = None
        self._status = "running"
        await workflow.execute_activity(
            ACTIVITY_DELIVER_DELEGATION_RESULT,
            DeliverDelegationResultInput(
                workspace_id=params.workspace_id,
                task_id=params.task_id,
                run_id=run_id,
                agent_id=params.agent_id,
                child_task_id=request.child_task_id,
                provider_call_id=request.provider_call_id,
                kind=request.kind,
                summary=delegated.summary,
                gateway_tool_call_id=request.gateway_tool_call_id,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_FINALIZE_RETRY,
        )

    async def _finalize(
        self,
        params: AgentTaskInput,
        *,
        run_id: str | None,
        status: str,
        error_code: str | None,
        error_message: str | None,
        use_tool_worker: bool = False,
    ) -> None:
        finalize_input = FinalizeInput(
            workspace_id=params.workspace_id,
            task_id=params.task_id,
            run_id=run_id,
            status=status,
            steps_used=self._steps_used,
            error_code=error_code,
            error_message=error_message,
        )
        if use_tool_worker:
            if run_id is not None:
                with contextlib.suppress(Exception):
                    await workflow.execute_activity(
                        ACTIVITY_CLEANUP_RUN_WORKSPACE,
                        CleanupRunWorkspaceInput(
                            workspace_id=params.workspace_id,
                            run_id=run_id,
                        ),
                        task_queue=TOOL_TASK_QUEUE,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_FINALIZE_RETRY,
                    )
            await workflow.execute_activity(
                ACTIVITY_FINALIZE_RUN_PROJECTION,
                finalize_input,
                task_queue=AGENT_TASK_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_FINALIZE_RETRY,
            )
        else:
            await workflow.execute_activity(
                ACTIVITY_FINALIZE_RUN,
                finalize_input,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_FINALIZE_RETRY,
            )
