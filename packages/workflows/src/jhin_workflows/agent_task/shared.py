"""Typed contracts between AgentTaskWorkflow and the agent worker activities.

Deliberately dependency-light (stdlib dataclasses only) so the API can import
this module to start/signal workflows without pulling agent-runtime
dependencies. Activities are referenced by name: the workflow definition
lives here, the implementations live in the agent worker service.

The snapshot travels as its JSON serialization (``snapshot_json``): it is
resolved once, hashed, and never mutated mid-run (plan 7.1). It contains no
credentials — activities decrypt provider secrets at the moment of use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid5

ACTIVITY_RESOLVE_SNAPSHOT = "resolve_snapshot"
ACTIVITY_RUN_AGENT_STEP = "run_agent_step"
ACTIVITY_RESOLVE_APPROVAL = "resolve_approval"
ACTIVITY_FINALIZE_RUN = "finalize_run"

PHASE10_TOOL_WORKER_PATCH = "phase10-tool-worker-boundary-v1"
ACTIVITY_RESOLVE_ADVERTISED_TOOLS = "resolve_advertised_tools"
ACTIVITY_REASON_AGENT_STEP = "reason_agent_step"
ACTIVITY_EXECUTE_BOUND_TOOL = "execute_bound_tool"
ACTIVITY_COMMIT_AGENT_STEP = "commit_agent_step"

# The tool worker raises this (non-retryable) when the gateway durably
# recorded a denied / rejected / failed outcome for a bound call. The
# outcome row already exists, so the workflow treats it as the call's
# observation (the model reads it on the next step) rather than a run failure.
ORDINARY_TOOL_FAILURE_MESSAGE = "bound tool execution was rejected before a usable outcome"

# Mirror of ``jhin_tools.stable_tool_invocation_id`` (kept stdlib-only so the
# workflow sandbox never imports the tool runtime); a test pins equality.
_TOOL_INVOCATION_NAMESPACE = UUID("4f0ac960-eab4-5f17-9b65-9f9bcbf3e0a8")


def bound_tool_call_id(run_id: str, step_index: int, ordinal: int) -> str:
    """The canonical tool_call id the gateway used for one step ordinal."""
    return str(uuid5(_TOOL_INVOCATION_NAMESPACE, f"v1:{UUID(run_id).hex}:{step_index}:{ordinal}"))


ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL = "resolve_bound_tool_approval"
ACTIVITY_COMMIT_APPROVAL_PROJECTION = "commit_approval_projection"
ACTIVITY_CLEANUP_RUN_WORKSPACE = "cleanup_run_workspace"
ACTIVITY_FINALIZE_RUN_PROJECTION = "finalize_run_projection"
# Durable review parking (coordination release): the tool worker resumes a
# ``pending_review`` call after the review is decided; the agent worker
# commits the sanitized projection. Same shape as the approval pair.
ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW = "resolve_bound_tool_review"
ACTIVITY_COMMIT_REVIEW_PROJECTION = "commit_review_projection"
# Signal name delivered by ``decide_review`` (API) or lifted from an executed
# ``organization.review.submit`` call into the source task workflow.
SIGNAL_REVIEW_DECISION = "review_decision"


@dataclass
class AgentTaskInput:
    workspace_id: str
    task_id: str
    agent_id: str
    instruction: str = ""


@dataclass
class SnapshotResult:
    run_id: str
    snapshot_json: str
    snapshot_hash: str
    max_steps: int
    # Concurrency admission (plan 30): when no run slot is free the activity
    # creates no run, marks the task queued, and returns queued=True. The
    # workflow parks and retries — queue, don't reject.
    queued: bool = False
    queue_reason: str = ""


@dataclass
class DelegationRequest:
    """One organization.delegate_task execution surfaced by the step activity.

    The tool executor already created the child *task row* and the structured
    delegation message; the workflow starts the durable child workflow
    (plan 8.3 — only workflows start child workflows).
    """

    child_task_id: str
    target_agent_id: str
    blocking: bool
    kind: str  # "delegation" | "review_request"
    # The model's tool-call id, so the blocking result can be stitched back
    # into the transcript as this call's observation.
    provider_call_id: str = ""
    # Canonical gateway invocation id used for durable transcript pairing.
    gateway_tool_call_id: str = ""


@dataclass
class ReviewDecisionSignal:
    """One decided review surfaced by the step activity (the assigned AI
    reviewer executed ``organization.review.submit``): the workflow forwards
    it as a ``review_decision`` signal to the source task workflow so a run
    parked on that review resumes (duplicate signals are idempotent)."""

    review_id: str
    status: str  # WorkReviewStatus value after the decision
    source_workflow_id: str


@dataclass
class WorkRequestStart:
    """One accepted work request surfaced by the step activity (the
    ``organization.respond_work_request`` executor created the task row);
    the workflow starts the durable, non-blocking WorkRequestTaskWorkflow."""

    work_request_id: str
    task_id: str
    agent_id: str


@dataclass
class RunStepInput:
    workspace_id: str
    task_id: str
    run_id: str
    agent_id: str
    snapshot_json: str
    step_index: int
    instruction: str = ""
    user_instructions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdvertisedTool:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ResolveAdvertisedToolsInput:
    workspace_id: str
    agent_id: str


@dataclass
class ReasonAgentStepInput(RunStepInput):
    advertised_tools: list[AdvertisedTool] = field(default_factory=list)


@dataclass
class ReasonAgentStepResult:
    call_count: int


@dataclass
class ExecuteBoundToolInput:
    workspace_id: str
    run_id: str
    step_index: int
    ordinal: int


@dataclass
class BoundToolResult:
    tool_call_id: str
    status: str
    approval_id: str | None = None
    stop_reason: str | None = None
    # Set when the gateway parked the call on a pending work review.
    review_id: str | None = None


@dataclass
class CommitAgentStepInput:
    workspace_id: str
    task_id: str
    run_id: str
    agent_id: str
    step_index: int
    gateway_tool_call_ids: list[str] = field(default_factory=list)
    # A cancellation signal arrived after this canonical call completed but
    # before the remaining manifest calls were scheduled. The projection
    # validates the marker against the exact prefix and rejects any omitted
    # call that already has a durable ToolCall row.
    cancelled_after_tool_call_id: str | None = None


@dataclass
class ResolveBoundToolApprovalInput:
    workspace_id: str
    task_id: str
    run_id: str
    agent_id: str
    approval_id: str


@dataclass
class CommitApprovalProjectionInput(ResolveBoundToolApprovalInput):
    tool_call_id: str = ""


@dataclass
class ResolveBoundToolReviewInput:
    workspace_id: str
    task_id: str
    run_id: str
    agent_id: str
    review_id: str


@dataclass
class CommitReviewProjectionInput(ResolveBoundToolReviewInput):
    tool_call_id: str = ""


@dataclass
class CleanupRunWorkspaceInput:
    workspace_id: str
    run_id: str


@dataclass
class CleanupRunWorkspaceResult:
    deleted: bool


@dataclass
class StepResult:
    done: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_micros: int = 0
    # Set when the gateway parked a tool call pending human approval: the
    # workflow suspends on the approval_decision signal for this id.
    waiting_approval_id: str | None = None
    # Set when the gateway parked a tool call pending a work review: the
    # workflow suspends on the review_decision signal for this id, then
    # resumes the call through the normal approval/claim/effect path.
    waiting_review_id: str | None = None
    # Delegations executed this step (plan 7.5): the workflow starts one
    # DelegatedTaskWorkflow per entry and awaits the blocking one (at most
    # one — the step parks immediately after a blocking delegation).
    delegations: list[DelegationRequest] = field(default_factory=list)
    # Work requests this agent accepted during the step (coordination
    # release): each starts one abandoned WorkRequestTaskWorkflow child.
    work_request_starts: list[WorkRequestStart] = field(default_factory=list)
    # Reviews this agent decided during the step as the assigned AI
    # reviewer: the workflow signals each source task workflow.
    review_decisions: list[ReviewDecisionSignal] = field(default_factory=list)
    # A claimed mutation whose terminal outcome cannot be proven. The
    # activity persists this before failing non-retryably so the workflow
    # cannot advance to another model step and repeat the effect.
    execution_unknown_tool_call_id: str | None = None


@dataclass
class ResolveApprovalInput:
    """Resume a run after a human decided a parked approval.

    ``decision`` is advisory routing only — the activity re-reads the
    approval row in Postgres, which is the sole authority (plan 52).
    """

    workspace_id: str
    task_id: str
    run_id: str
    agent_id: str
    approval_id: str
    decision: str  # "approved" | "rejected"


@dataclass
class FinalizeInput:
    workspace_id: str
    task_id: str
    run_id: str | None
    status: str  # RunStatus value: completed | failed | cancelled
    steps_used: int
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class AgentTaskResult:
    run_id: str | None
    status: str
    steps_used: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0


@dataclass
class AgentTaskStatus:
    """Query response: current status and why the workflow is waiting."""

    status: str
    waiting_reason: str | None
    steps_used: int
    pending_instructions: int
