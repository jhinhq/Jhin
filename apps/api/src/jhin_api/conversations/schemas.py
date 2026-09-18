"""Schemas for conversations, the company activity feed, and attention."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer, model_validator

from jhin_api.approvals.schemas import ApprovalOut
from jhin_api.coordination.schemas import WorkReviewOut
from jhin_api.personas.schemas import AgentPersonaSummary
from jhin_api.tasks.schemas import MessageOut, TaskOut, ToolCallOut
from jhin_domain import ActivityKind, ConversationStatus
from jhin_secrets.intake import redact_legacy_text


class ConversationToolCallOut(ToolCallOut):
    task_id: UUID
    agent_name: str | None = None


class ConversationToolCallListOut(BaseModel):
    items: list[ConversationToolCallOut]
    has_more: bool
    limit: int
    next_before: UUID | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("title", "last_message_preview")
    def serialize_legacy_text(self, value: str | None) -> str | None:
        return redact_legacy_text(value) if value is not None else None

    id: UUID
    workspace_id: UUID
    project_id: UUID | None = None
    workspace_version: int = 0
    source_conversation_id: UUID | None = None
    source_message_id: UUID | None = None
    source_checkpoint_id: UUID | None = None
    title: str
    status: str
    pinned: bool
    primary_agent_id: UUID | None
    created_by_user_id: UUID | None
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime
    # Most recent task in the conversation that is queued/running/paused.
    active_task_id: UUID | None = None
    active_task_state: str | None = None
    active_run_status: str | None = None
    # When the run now carrying that task began. The run's own start, waits
    # included — what an audit means by "this run began at". It is *not* how
    # long the agent has been thinking, because a run parked on an approval
    # keeps this stamp while a person sleeps on it: see the two fields below,
    # which are what a "thinking for…" display counts.
    active_run_started_at: datetime | None = None
    # Thinking time, with the person's own waiting taken out of it
    # (:mod:`jhin_domain.timing`). ``working_seconds`` is what is already
    # banked and ``working_since`` is where the current stretch of thinking
    # started, so a client shows ``working_seconds + (now - working_since)``
    # and ticks it locally — one timestamp, no request per second.
    #
    # ``working_since`` is None whenever no stretch of work is in progress to
    # count, and a client with no instant to count from must show no clock
    # rather than a wrong one. Several different turns send that same None —
    # one parked on an approval, question or review nobody closed; one whose
    # run has already finished; one whose run never got a usable start stamp;
    # and one whose task says ``running`` with no run behind it at all — and
    # this field does not say which. A surface that names a cause in words is
    # therefore naming a guess: see ``WORKING_TIME_UNAVAILABLE_TITLE`` in
    # ``apps/web/components/chat/working-time.tsx``, which offers the causes
    # as possibilities rather than asserting one.
    active_run_working_since: datetime | None = None
    active_run_working_seconds: int = 0
    # What the agent is doing right now, as a finished sentence ("Saving this
    # to memory"). Rendered by the API from the tool *name* alone — never by
    # the browser, and never from tool arguments. Only the conversation
    # detail carries it: the chat list polls every row, and this costs a
    # query. See ``_active_activity``.
    active_activity: str | None = None
    last_message_preview: str | None = None
    last_message_sender_type: str | None = None
    agent_name: str | None = None
    agent_role_title: str | None = None
    task_count: int = 0


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    total: int


class SecureInputIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    value: SecretStr = Field(min_length=1, max_length=8192)

    def transient_value(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value.get_secret_value()}


class ConversationCreate(BaseModel):
    agent_id: UUID
    project_id: UUID | None = None
    execution_mode: Literal["ask", "plan", "act"] = "act"
    model_profile_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    # When present, the first turn is sent immediately.
    text: str | None = Field(default=None, min_length=1, max_length=20_000)
    client_turn_id: str | None = Field(default=None, max_length=64)
    secure_inputs: list[SecureInputIn] = Field(default_factory=list, max_length=10)


class ConversationUpdate(BaseModel):
    project_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    status: ConversationStatus | None = None


class TurnIn(BaseModel):
    text: str = Field(default="", max_length=20_000)
    client_turn_id: str | None = Field(default=None, max_length=64)
    execution_mode: Literal["ask", "plan", "act"] | None = None
    delivery: Literal["auto", "steer", "queue"] = "auto"
    model_profile_id: UUID | None = None
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=10)
    context_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    secure_inputs: list[SecureInputIn] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def require_content(self) -> TurnIn:
        if (
            not self.text.strip()
            and not self.attachment_ids
            and not self.context_refs
            and not self.secure_inputs
        ):
            raise ValueError("A message or file is required")
        return self


class ConversationControlIn(BaseModel):
    action: Literal["pause", "resume", "stop"]


class QueuedTurnUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    secure_inputs: list[SecureInputIn] = Field(default_factory=list, max_length=10)


class ConversationBranchIn(BaseModel):
    message_id: UUID
    checkpoint_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)


class FailureNoticeOut(BaseModel):
    """A run failure as a person reads it (:mod:`jhin_domain.failures`).

    Set on the ``error`` message a failed run leaves in the transcript, and
    nowhere else. The raw ``content_json`` keeps its original text so nothing
    a client already renders changes underneath it; this is the same failure
    said in the product's own voice.
    """

    #: The internal code. For support and for clients that special-case one
    #: class — never the headline.
    code: str
    #: One sentence, always present, never containing an identifier.
    summary: str
    #: The failure's own words where they add something (a provider message,
    #: a command's stderr tail). Empty when the summary already says it all.
    detail: str
    #: The id support would ask for, lifted out of the prose. Often empty.
    reference: str


class ConversationMessageOut(MessageOut):
    conversation_id: UUID | None = None
    # Agent name, user display name, or "System".
    sender_name: str | None = None
    # Set when sender_type == "agent".
    agent_id: UUID | None = None
    # Set only on the system ``error`` row that records a failed run.
    failure: FailureNoticeOut | None = None


TurnMode = Literal["new_task", "instruction"]


class TurnOut(BaseModel):
    conversation: ConversationOut
    message: ConversationMessageOut
    task_id: UUID
    mode: TurnMode


class ConversationAgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    role_title: str
    status: str
    availability: str
    public_purpose: str
    # The persona the agent wears, for the chat header. Shown even when the
    # persona is switched off (``enabled: false``) so the header can say so.
    persona: AgentPersonaSummary | None = None


ResumeState = Literal["ready", "blocked", "unavailable"]


class ConversationResumeOut(BaseModel):
    """Whether the last thing that happened here can be picked up again.

    Present only when the newest turn in the conversation failed and nothing
    is running now; ``None`` otherwise, which is the client's signal that
    there is nothing to offer rather than something to grey out.
    """

    #: The failed turn this describes. Clients match it against the ``task_id``
    #: of the failure message so the control lands on the right card.
    task_id: UUID
    run_id: UUID | None = None
    #: ``ready`` — press it. ``blocked`` — a call from that turn was never
    #: accounted for, so re-running it could repeat something that already
    #: happened. ``unavailable`` — safe in itself, but something else is in
    #: the way right now (the agent is paused, turned off, or gone).
    state: ResumeState
    #: One sentence saying what pressing does, or why it cannot happen.
    #: Always present, always in the product's voice.
    reason: str
    #: What was asked, so a client can put the words back in the composer
    #: instead of making the person retype them.
    instruction: str = ""
    #: Set when ``state == "blocked"``: the call nobody can account for, for
    #: the support conversation. The tool's *name* is deliberately not here —
    #: it reaches a person only through the sentence in ``reason``, rendered
    #: by the API (see :mod:`jhin_domain.activity`).
    unreconciled_tool_call_id: UUID | None = None


class ConversationDetailOut(BaseModel):
    conversation: ConversationOut
    agent: ConversationAgentOut | None
    tasks: list[TaskOut]  # newest first
    total_input_tokens: int
    total_output_tokens: int
    total_cost_micros: int
    pending_approvals: list[ApprovalOut]
    resume: ConversationResumeOut | None = None


class ResumeOut(BaseModel):
    """The result of picking a failed turn back up."""

    conversation: ConversationOut
    #: The work episode now carrying the turn.
    task_id: UUID
    #: The failed turn it took over from.
    resumed_task_id: UUID
    #: False when an earlier press already started this and nothing new was
    #: created — the whole point of the endpoint being safe to press twice.
    created: bool


class ActivityCardOut(BaseModel):
    # Stable: "msg:<uuid>" | "task:<uuid>:<state>" | "approval:<uuid>" |
    # "work_request:<uuid>:<asked|reported>" | "review:<uuid>"
    id: str
    kind: ActivityKind
    label: str
    actor_type: Literal["agent", "user", "system"]
    actor_agent_id: UUID | None = None
    actor_agent_name: str | None = None
    target_agent_id: UUID | None = None
    target_agent_name: str | None = None
    task_id: UUID | None = None
    task_title: str | None = None
    root_task_id: UUID | None = None
    conversation_id: UUID | None = None
    approval_id: UUID | None = None
    work_request_id: UUID | None = None
    review_id: UUID | None = None
    summary: str
    detail_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ActivityListOut(BaseModel):
    items: list[ActivityCardOut]
    next_before: datetime | None = None


class AttentionCounts(BaseModel):
    approvals: int
    failures: int
    reviews: int = 0
    # Reviews an AI colleague is handling; informational, never in ``total``.
    reviews_in_progress: int = 0
    # Workspace budget warning notices; informational, never in ``total``.
    budget_warnings: int = 0
    total: int


class BudgetNoticeOut(BaseModel):
    """Workspace model spend crossed the budget warning threshold (plan 15.5)."""

    monthly_budget_micros: int
    spent_month_micros: int
    percent_used: int
    warning_threshold: float


class AttentionOut(BaseModel):
    pending_approvals: list[ApprovalOut]
    failed_tasks: list[TaskOut]
    waiting_conversations: list[ConversationOut]
    # Work reviews waiting on a human decision (coordination release).
    pending_reviews: list[WorkReviewOut] = Field(default_factory=list)
    # Pending reviews assigned to an AI reviewer. A person can still step in
    # (``POST /reviews/{id}/decide``, admin for AI-assigned reviews).
    reviews_in_progress: list[WorkReviewOut] = Field(default_factory=list)
    # Set once (workspace-level) when tracked model spend crossed the
    # budget warning threshold.
    budget: BudgetNoticeOut | None = None
    counts: AttentionCounts


class AcknowledgeFailuresOut(BaseModel):
    acknowledged: int
    task_ids: list[UUID]
