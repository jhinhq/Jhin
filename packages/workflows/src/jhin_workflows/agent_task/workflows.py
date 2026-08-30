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
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError
from temporalio.workflow import ChildWorkflowHandle, ParentClosePolicy

from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_COMMIT_AGENT_STEP,
    ACTIVITY_COMMIT_APPROVAL_PROJECTION,
    ACTIVITY_COMMIT_REVIEW_PROJECTION,
    ACTIVITY_DELIVER_QUESTION_ANSWER,
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_FINALIZE_RUN,
    ACTIVITY_FINALIZE_RUN_PROJECTION,
    ACTIVITY_MARK_TASK_PAUSED,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_APPROVAL,
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
    ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW,
    ACTIVITY_RESOLVE_SNAPSHOT,
    ACTIVITY_RUN_AGENT_STEP,
    ASK_PERSON_WAIT_PATCH,
    CHAT_LATE_INSTRUCTION_DRAIN_PATCH,
    ORDINARY_TOOL_FAILURE_MESSAGE,
    PAUSE_IS_OBSERVED_PATCH,
    PHASE10_TOOL_WORKER_PATCH,
    SIGNAL_QUESTION_ANSWER,
    SIGNAL_REVIEW_DECISION,
    WORK_REQUEST_REQUESTER_WAIT_PATCH,
    WORK_REQUEST_SIDE_REQUESTER,
    AdvertisedTool,
    AgentTaskInput,
    AgentTaskResult,
    AgentTaskStatus,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    CommitReviewProjectionInput,
    DelegationRequest,
    DeliverQuestionAnswerInput,
    ExecuteBoundToolInput,
    FinalizeInput,
    MarkTaskPausedInput,
    PersonQuestionAsk,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    ResolveApprovalInput,
    ResolveBoundToolApprovalInput,
    ResolveBoundToolReviewInput,
    ReviewDecisionSignal,
    RunStepInput,
    SnapshotResult,
    StepResult,
    WorkRequestStart,
    bound_tool_call_id,
)
from jhin_workflows.delegated_task.shared import (
    ACTIVITY_DELIVER_DELEGATION_RESULT,
    DelegatedTaskInput,
    DelegatedTaskResult,
    DeliverDelegationResultInput,
)
from jhin_workflows.task_queues import AGENT_TASK_QUEUE, TOOL_TASK_QUEUE
from jhin_workflows.work_request_task.shared import (
    ACTIVITY_FINALIZE_WORK_REQUEST,
    ACTIVITY_NOTE_WORK_REQUEST_UNANSWERED,
    FinalizeWorkRequestInput,
    NoteWorkRequestUnansweredInput,
    WorkRequestTaskInput,
    WorkRequestTaskResult,
    work_request_workflow_id,
)

_GENERIC_FAILURE_TEXTS = frozenset({"Activity task failed", "Child workflow execution failed"})

# Failure types raised by activities that the run record should carry verbatim
# (instead of the generic step_failed) so the UI can react to them.
_SPECIFIC_FAILURE_CODES = frozenset(
    {"insufficient_funds", "budget_exceeded", "model_incompatible_request"}
)


def _failure_code(exc: BaseException, default: str) -> str:
    """``default`` unless the cause chain carries one of the specific codes."""
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        failure_type = getattr(current, "type", None)
        if isinstance(failure_type, str) and failure_type in _SPECIFIC_FAILURE_CODES:
            return failure_type
        current = getattr(current, "cause", None) or current.__cause__
    return default


def _is_ordinary_tool_failure(exc: BaseException) -> bool:
    """True when the tool worker reported a durably recorded denied /
    rejected / failed outcome (see ``ORDINARY_TOOL_FAILURE_MESSAGE``)."""
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        if (
            isinstance(current, ApplicationError)
            and current.message == ORDINARY_TOOL_FAILURE_MESSAGE
        ):
            return True
        current = getattr(current, "cause", None) or current.__cause__
    return False


def _failure_message(exc: BaseException) -> str:
    """Human-readable failure text for run records.

    Temporal wraps activity failures in an ``ActivityError`` whose own message
    is the generic "Activity task failed"; the provider or policy error the
    user needs lives in the cause chain (``ApplicationError.message``). Walk
    it and keep the most specific text.
    """
    message = str(exc)
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        text = getattr(current, "message", None) or str(current)
        if text and text not in _GENERIC_FAILURE_TEXTS:
            message = text
        current = getattr(current, "cause", None) or current.__cause__
    return message[:2000]


# Queue-admission poll cadence (plan 30). finalize_run signals queued
# workflows when a slot frees (slot_available), so this timer is only the
# correctness backstop against missed kicks.
_QUEUE_POLL_INTERVAL = timedelta(seconds=30)

# How long a requester holds its turn open waiting for a colleague's answer
# (see ``_await_work_request_answer``). Short on purpose: a person is usually
# sitting in a chat watching for the reply, and a waiting requester keeps its
# own concurrency slot, so an unbounded wait would trade one silent promise
# for one silent hang. The colleague's task keeps its own six-hour ceiling —
# this is only how long the conversation waits for it, and when it elapses
# the requester says so and the answer still lands in the conversation later.
_WORK_REQUEST_ANSWER_WAIT = timedelta(minutes=2)

# How long a run holds its turn open waiting for a person to answer a
# question it asked. Long enough to survive a meeting or a coffee break;
# short enough that a question nobody noticed does not hold the agent's
# concurrency slot for a working day. When it elapses the agent is told
# plainly that nobody answered, and the box stays on screen.
_PERSON_ANSWER_WAIT = timedelta(minutes=30)

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
_CLEANUP_SCHEDULE_TO_CLOSE_TIMEOUT = timedelta(seconds=30)
_RESOLVE_APPROVAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)


async def _work_request_finished(handle: ChildWorkflowHandle[Any, Any]) -> bool:
    """Await one WorkRequestTaskWorkflow, reporting only whether it finished.

    Deliberately never raises. A colleague whose run died is not the
    requester's failure, and the requester may stop waiting before the child
    is done: an exception left unretrieved on an abandoned wait would surface
    long after anyone cared about it.
    """
    try:
        await handle
    except Exception:
        return False
    return True


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
        # review_id -> decided status ("approved" | "changes_requested" |
        # "escalated"), delivered by the review_decision signal. Stored even
        # when it arrives before the run parks, so a decision that races the
        # park still resumes the workflow.
        self._review_decisions: dict[str, str] = {}
        # question_id -> True, delivered by the question_answer signal from
        # the API. Stored even when it arrives before the run parks, so an
        # answer that races the park still resumes the workflow (same
        # reasoning as _review_decisions).
        self._question_answers: dict[str, bool] = {}
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

    @workflow.signal(name=SIGNAL_REVIEW_DECISION)
    def review_decision(self, review_id: str, status: str) -> None:
        """A reviewer (human via the API, or the assigned AI reviewer via
        ``organization.review.submit``) decided a work review. The signal
        only wakes the workflow; the tool worker re-reads the Postgres
        ``work_review`` row as the authority before resuming the call."""
        if review_id and status in ("approved", "changes_requested", "escalated"):
            self._review_decisions[review_id] = status

    @workflow.signal(name=SIGNAL_QUESTION_ANSWER)
    def question_answer(self, question_id: str) -> None:
        """A person answered. The signal carries an id and nothing else: the
        activity re-reads the Postgres user_question row as the authority."""
        if question_id:
            self._question_answers[question_id] = True

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
                    error_message=_failure_message(exc),
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

        # Budget denial (plan 15.5): no run was created; fail the task with
        # the activity's message so the chat card explains which budget was
        # hit. Purely data-driven — old histories never set denied_code, so
        # replays take the same path they always did.
        if snapshot.denied_code:
            self._status = "failed"
            await self._finalize(
                params,
                run_id=None,
                status="failed",
                error_code=snapshot.denied_code,
                error_message=snapshot.denied_message or "a monthly budget was reached",
            )
            return AgentTaskResult(run_id=None, status="failed", steps_used=0)

        totals.run_id = snapshot.run_id
        use_tool_worker = workflow.patched(PHASE10_TOOL_WORKER_PATCH)
        # A turn signalled while the run is wrapping up must still be
        # answered: the queue is checked again at every late boundary below,
        # and anything found re-enters the step loop instead of being
        # stranded — the run used to complete past it and the person's
        # message was never answered.
        drain_late_instructions = workflow.patched(CHAT_LATE_INSTRUCTION_DRAIN_PATCH)
        done = False

        while True:
            done, error_code, error_message = await self._run_step_loop(
                params,
                snapshot,
                totals,
                use_tool_worker=use_tool_worker,
                drain_late_instructions=drain_late_instructions,
                done=done,
                error_code=error_code,
                error_message=error_message,
            )

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

            if drain_late_instructions and final_status == "completed":
                # The workspace-cleanup hop crosses task queues and can sit
                # scheduled for seconds — and the person's reply has already
                # rendered by then, so this is exactly when their next turn
                # tends to arrive. Run the hop first, then look one last time
                # before the terminal projection: anything pending re-enters
                # the step loop. (A signal during the projection itself can
                # still slip through; once the state is terminal the API
                # starts a fresh task instead of signalling.)
                await self._cleanup_run_workspace(
                    params, snapshot.run_id, use_tool_worker=use_tool_worker
                )
                if self._pending_instructions and self._steps_used < snapshot.max_steps:
                    done = False
                    continue
                self._status = "finalizing"
                await self._finalize(
                    params,
                    run_id=snapshot.run_id,
                    status=final_status,
                    error_code=error_code,
                    error_message=error_message,
                    use_tool_worker=use_tool_worker,
                    skip_cleanup=True,
                )
                break

            self._status = "finalizing"
            await self._finalize(
                params,
                run_id=snapshot.run_id,
                status=final_status,
                error_code=error_code,
                error_message=error_message,
                use_tool_worker=use_tool_worker,
            )
            break

        self._status = final_status
        totals.status = final_status
        return totals

    async def _run_step_loop(
        self,
        params: AgentTaskInput,
        snapshot: SnapshotResult,
        totals: AgentTaskResult,
        *,
        use_tool_worker: bool,
        drain_late_instructions: bool,
        done: bool,
        error_code: str | None,
        error_message: str | None,
    ) -> tuple[bool, str | None, str | None]:
        """One pass of the reason/act loop. The caller decides whether a
        completed pass is really the end, or whether instructions that
        arrived late send it around again."""
        while (
            not done or (drain_late_instructions and len(self._pending_instructions) > 0)
        ) and self._steps_used < snapshot.max_steps:
            if self._cancelled:
                break
            if self._paused:
                self._status = "paused"
                self._waiting_reason = "paused_by_user"
                # Only now is the run genuinely stopped. The API used to write
                # "paused" the moment the signal was accepted, which was a
                # promise it could not keep: a run with no further step
                # boundary -- a single long generation, the case where pausing
                # matters most -- never reaches here, so the person was shown
                # a paused task and a Resume button for a run that carried on
                # and billed.
                await self._mark_paused(params, paused=True)
                await workflow.wait_condition(lambda: not self._paused or self._cancelled)
                self._waiting_reason = None
                await self._mark_paused(params, paused=False)
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
                            task_id=params.task_id,
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
                    stopped_for_durable_outcome = False
                    for ordinal in range(reasoned.call_count):
                        if self._cancel_requested():
                            break
                        try:
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
                        except ActivityError as exc:
                            if not _is_ordinary_tool_failure(exc):
                                raise
                            # The gateway durably recorded a denied / rejected /
                            # failed outcome for this ordinal. That is a usable
                            # observation for the model (its instructions say to
                            # explain and carry on), not a run failure: bind the
                            # canonical id so the commit step projects it, and
                            # keep executing the rest of the manifest.
                            tool_ids.append(
                                bound_tool_call_id(snapshot.run_id, self._steps_used, ordinal)
                            )
                            continue
                        tool_ids.append(bound.tool_call_id)
                        if bound.stop_reason is not None:
                            stopped_for_durable_outcome = True
                            break
                        if self._cancel_requested():
                            break
                    cancellation_truncation_id: str | None = None
                    if (
                        self._cancel_requested()
                        and len(tool_ids) < reasoned.call_count
                        and not stopped_for_durable_outcome
                    ):
                        if not tool_ids:
                            break
                        cancellation_truncation_id = tool_ids[-1]
                    step = await workflow.execute_activity(
                        ACTIVITY_COMMIT_AGENT_STEP,
                        CommitAgentStepInput(
                            workspace_id=params.workspace_id,
                            task_id=params.task_id,
                            run_id=snapshot.run_id,
                            agent_id=params.agent_id,
                            step_index=self._steps_used,
                            gateway_tool_call_ids=tool_ids,
                            cancelled_after_tool_call_id=cancellation_truncation_id,
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
                error_code = _failure_code(exc, "step_failed")
                error_message = _failure_message(exc)
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
                    error_message = _failure_message(exc)
                    delegation_failed = True
                    break
            if delegation_failed:
                break

            # A question the agent put to the person it is talking to. It
            # comes before the work-request starts so a step that asks both a
            # person and a colleague settles the person first — they are the
            # one sitting in the chat.
            for ask in step.person_questions[:1]:
                await self._await_person_answer(params, snapshot.run_id, ask)

            # Accepted work requests (coordination release) run as abandoned
            # children; a duplicate start (retry) is a no-op. The requester
            # then waits a bounded while for the answer so it can report it
            # itself — the responder, holding the same record for the task it
            # accepted, does not. Two asks in one step are waited on one after
            # the other: rare enough not to justify racing them, and each hop
            # is bounded, so the worst case is still finite.
            for accepted in step.work_request_starts:
                await self._start_work_request_task(params, accepted)

            # Reviews this agent decided as the assigned AI reviewer wake the
            # source task workflow (same signal the human API sends).
            for decided in step.review_decisions:
                await self._forward_review_decision(params, decided)

            waiting_approval_id = step.waiting_approval_id
            if step.waiting_review_id is not None:
                # Durable review wait (coordination release): the tool call is
                # persisted as pending_review and the run holds its slot as
                # waiting_review; park until the review_decision signal, then
                # resume the very same call through the tool worker, which
                # re-runs authorization and may stage a human approval.
                review_id = step.waiting_review_id
                if not use_tool_worker:
                    error_code = "review_wait_unsupported"
                    error_message = "legacy histories cannot park on a work review"
                    break
                self._status = "waiting_review"
                self._waiting_reason = f"review:{review_id}"

                def _reviewed(pending_id: str = review_id) -> bool:
                    return pending_id in self._review_decisions or self._cancelled

                await workflow.wait_condition(_reviewed)
                if review_id not in self._review_decisions:
                    break  # woken by cancel, not by a decision
                self._review_decisions.pop(review_id)
                self._waiting_reason = None
                self._status = "running"
                try:
                    reviewed_call = await workflow.execute_activity(
                        ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW,
                        ResolveBoundToolReviewInput(
                            workspace_id=params.workspace_id,
                            task_id=params.task_id,
                            run_id=snapshot.run_id,
                            agent_id=params.agent_id,
                            review_id=review_id,
                        ),
                        result_type=BoundToolResult,
                        task_queue=TOOL_TASK_QUEUE,
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=_RESOLVE_APPROVAL_RETRY,
                    )
                    reviewed = await workflow.execute_activity(
                        ACTIVITY_COMMIT_REVIEW_PROJECTION,
                        CommitReviewProjectionInput(
                            workspace_id=params.workspace_id,
                            task_id=params.task_id,
                            run_id=snapshot.run_id,
                            agent_id=params.agent_id,
                            review_id=review_id,
                            tool_call_id=reviewed_call.tool_call_id,
                        ),
                        result_type=StepResult,
                        task_queue=AGENT_TASK_QUEUE,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_FINALIZE_RETRY,
                    )
                except Exception as exc:
                    error_code = "review_resolution_failed"
                    error_message = _failure_message(exc)
                    break
                if reviewed.execution_unknown_tool_call_id is not None:
                    error_code = "tool_execution_unknown"
                    error_message = (
                        f"tool call {reviewed.execution_unknown_tool_call_id} execution "
                        "outcome is unknown; manual reconciliation is required"
                    )
                    break
                # An approved review may have staged the call for human
                # approval: fall through into the ordinary approval wait.
                waiting_approval_id = reviewed.waiting_approval_id

            if waiting_approval_id is not None:
                # Durable approval wait (plan 8.2, 12.5): the tool call and
                # approval rows are already persisted; park until a human
                # decides. Workflow state survives worker restarts.
                approval_id = waiting_approval_id
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
                    error_message = _failure_message(exc)
                    break
                # Approved or rejected, the loop continues: the next reason
                # step sees the tool result or the recorded denial.

        return done, error_code, error_message

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

    async def _forward_review_decision(
        self, params: AgentTaskInput, decided: ReviewDecisionSignal
    ) -> None:
        """Signal the source task workflow that its review was decided. The
        decision is already durable in Postgres; a closed or missing target
        workflow is not an error for the reviewer's own run."""
        if not decided.source_workflow_id or not decided.review_id:
            return
        if decided.source_workflow_id == f"task-{params.task_id}":
            self.review_decision(decided.review_id, decided.status)
            return
        handle = workflow.get_external_workflow_handle(decided.source_workflow_id)
        with contextlib.suppress(Exception):
            await handle.signal(SIGNAL_REVIEW_DECISION, args=[decided.review_id, decided.status])

    async def _start_work_request_task(
        self, params: AgentTaskInput, accepted: WorkRequestStart
    ) -> None:
        try:
            handle = await workflow.start_child_workflow(
                "WorkRequestTaskWorkflow",
                WorkRequestTaskInput(
                    workspace_id=params.workspace_id,
                    work_request_id=accepted.work_request_id,
                    task_id=accepted.task_id,
                    agent_id=accepted.agent_id,
                ),
                id=work_request_workflow_id(accepted.work_request_id),
                parent_close_policy=ParentClosePolicy.ABANDON,
                result_type=WorkRequestTaskResult,
            )
        except WorkflowAlreadyStartedError:
            # A retried tool call reported the same accepted request. The
            # first start owns the wait and Temporal offers no way to attach
            # to a child already running, so this one only stays a no-op.
            return
        except Exception:
            # The request row is already `accepted` with a task that will now
            # never run. Close it instead of leaving a colleague's ask stuck
            # forever, and do not fail this run over it — the requester's own
            # work is unrelated. A failure to finalize is swallowed for the
            # same reason; the row is still visible as accepted.
            with contextlib.suppress(Exception):
                await workflow.execute_activity(
                    ACTIVITY_FINALIZE_WORK_REQUEST,
                    FinalizeWorkRequestInput(
                        workspace_id=params.workspace_id,
                        work_request_id=accepted.work_request_id,
                        task_id=accepted.task_id,
                        run_status="failed",
                    ),
                    result_type=str,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_FINALIZE_RETRY,
                )
            return
        await self._await_work_request_answer(params, accepted, handle)

    async def _await_work_request_answer(
        self,
        params: AgentTaskInput,
        accepted: WorkRequestStart,
        handle: ChildWorkflowHandle[Any, Any],
    ) -> None:
        """Hold the REQUESTER's turn open until the colleague answers.

        A person asked their agent to ask somebody. Firing the request off and
        ending the turn left the person with a promise and the colleague's
        reply arriving beside it, addressed to nobody; the agent has to come
        back with the answer itself. So the requester parks here, and its next
        model step reads the colleague's ``result`` message — which
        ``finalize_work_request`` commits on *this* task before the child
        workflow completes — and composes one reply that carries the answer.

        Only the requester may park. The responder reaches this same code with
        the task it just accepted *for itself*: waiting there would be waiting
        on its own work, and the run would never move again. ``side`` states
        which one this is, and the agent-id check is the second lock on that
        door — the created task always belongs to the request's target, so an
        agent that owns it cannot be the one asking.
        """
        if accepted.side != WORK_REQUEST_SIDE_REQUESTER:
            return
        if accepted.agent_id == params.agent_id:
            return
        if not workflow.patched(WORK_REQUEST_REQUESTER_WAIT_PATCH):
            return  # replaying a run recorded while requests never parked
        answered = asyncio.ensure_future(_work_request_finished(handle))
        self._status = "waiting_work_request"
        self._waiting_reason = f"work_request:{accepted.work_request_id}"
        # Bounded, and released early by a cancel signal — the wait must
        # never be the reason a run cannot be stopped. The child is
        # ABANDONed either way, so giving up here costs the colleague
        # nothing: their answer still reaches the conversation.
        with contextlib.suppress(asyncio.TimeoutError):
            await workflow.wait_condition(
                lambda: answered.done() or self._cancelled,
                timeout=_WORK_REQUEST_ANSWER_WAIT,
            )
        self._waiting_reason = None
        self._status = "running"
        if self._cancelled:
            return  # the outer loop finalizes as cancelled
        if answered.done() and answered.result():
            return  # the colleague's result message is already on this task
        # Nothing to read yet: say so on the task, so the next step tells the
        # person plainly rather than inventing an answer or promising one.
        # Best-effort by contract — a missing courtesy note must not fail a
        # run whose own work is unrelated to the colleague's.
        with contextlib.suppress(Exception):
            await workflow.execute_activity(
                ACTIVITY_NOTE_WORK_REQUEST_UNANSWERED,
                NoteWorkRequestUnansweredInput(
                    workspace_id=params.workspace_id,
                    work_request_id=accepted.work_request_id,
                ),
                result_type=str,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_FINALIZE_RETRY,
            )

    async def _await_person_answer(
        self, params: AgentTaskInput, run_id: str, ask: PersonQuestionAsk
    ) -> None:
        """Hold the turn open until the person answers, or 30 minutes pass.

        Bounded and cancel-releasable by contract: this wait must never be
        the reason a run cannot be stopped. The observation is written either
        way, so the next model step reads an answer or a plain "nobody
        answered" rather than inventing one.
        """
        if not workflow.patched(ASK_PERSON_WAIT_PATCH):
            return  # replaying a run recorded before agents could ask
        self._status = "waiting_person"
        self._waiting_reason = f"question:{ask.question_id}"
        timed_out = False
        try:
            await workflow.wait_condition(
                lambda: ask.question_id in self._question_answers or self._cancelled,
                timeout=_PERSON_ANSWER_WAIT,
            )
        except TimeoutError:
            timed_out = True
        self._waiting_reason = None
        self._status = "running"
        if self._cancelled:
            return  # the outer loop finalizes as cancelled; finalize closes the row
        self._question_answers.pop(ask.question_id, None)
        # Best-effort by contract, like the work-request courtesy note: the
        # tool call already has a durable outcome row, so a failed delivery
        # costs the model an observation, not the run.
        with contextlib.suppress(Exception):
            await workflow.execute_activity(
                ACTIVITY_DELIVER_QUESTION_ANSWER,
                DeliverQuestionAnswerInput(
                    workspace_id=params.workspace_id,
                    task_id=params.task_id,
                    run_id=run_id,
                    agent_id=params.agent_id,
                    question_id=ask.question_id,
                    provider_call_id=ask.provider_call_id,
                    gateway_tool_call_id=ask.gateway_tool_call_id,
                    outcome="timed_out" if timed_out else "answered",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_FINALIZE_RETRY,
            )

    async def _mark_paused(self, params: AgentTaskInput, *, paused: bool) -> None:
        """Persist the pause the run is actually observing.

        Best-effort by contract: the workflow's own flag is the authority, and
        a failed projection must not strand a run that is otherwise fine.
        """
        if not workflow.patched(PAUSE_IS_OBSERVED_PATCH):
            return  # replaying a run recorded while the API wrote the state
        with contextlib.suppress(Exception):
            await workflow.execute_activity(
                ACTIVITY_MARK_TASK_PAUSED,
                MarkTaskPausedInput(
                    workspace_id=params.workspace_id,
                    task_id=params.task_id,
                    paused=paused,
                ),
                result_type=str,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_FINALIZE_RETRY,
            )

    async def _cleanup_run_workspace(
        self, params: AgentTaskInput, run_id: str | None, *, use_tool_worker: bool
    ) -> None:
        if not use_tool_worker or run_id is None:
            return
        with contextlib.suppress(Exception):
            await workflow.execute_activity(
                ACTIVITY_CLEANUP_RUN_WORKSPACE,
                CleanupRunWorkspaceInput(
                    workspace_id=params.workspace_id,
                    run_id=run_id,
                ),
                task_queue=TOOL_TASK_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=_CLEANUP_SCHEDULE_TO_CLOSE_TIMEOUT,
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
        skip_cleanup: bool = False,
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
            if not skip_cleanup:
                await self._cleanup_run_workspace(params, run_id, use_tool_worker=use_tool_worker)
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
