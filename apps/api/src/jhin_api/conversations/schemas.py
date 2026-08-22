"""Schemas for conversations, the company activity feed, and attention."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jhin_api.approvals.schemas import ApprovalOut
from jhin_api.coordination.schemas import WorkReviewOut
from jhin_api.tasks.schemas import MessageOut, TaskOut
from jhin_domain import ActivityKind, ConversationStatus


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
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
    last_message_preview: str | None = None
    last_message_sender_type: str | None = None
    agent_name: str | None = None
    agent_role_title: str | None = None
    task_count: int = 0


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    total: int


class ConversationCreate(BaseModel):
    agent_id: UUID
    title: str | None = Field(default=None, max_length=200)
    # When present, the first turn is sent immediately.
    text: str | None = Field(default=None, min_length=1, max_length=20_000)
    client_turn_id: str | None = Field(default=None, max_length=64)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    status: ConversationStatus | None = None


class TurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    client_turn_id: str | None = Field(default=None, max_length=64)


class ConversationMessageOut(MessageOut):
    conversation_id: UUID | None = None
    # Agent name, user display name, or "System".
    sender_name: str | None = None
    # Set when sender_type == "agent".
    agent_id: UUID | None = None


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


class ConversationDetailOut(BaseModel):
    conversation: ConversationOut
    agent: ConversationAgentOut | None
    tasks: list[TaskOut]  # newest first
    total_input_tokens: int
    total_output_tokens: int
    total_cost_micros: int
    pending_approvals: list[ApprovalOut]


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
    total: int


class AttentionOut(BaseModel):
    pending_approvals: list[ApprovalOut]
    failed_tasks: list[TaskOut]
    waiting_conversations: list[ConversationOut]
    # Work reviews waiting on a human decision (coordination release).
    pending_reviews: list[WorkReviewOut] = Field(default_factory=list)
    counts: AttentionCounts
