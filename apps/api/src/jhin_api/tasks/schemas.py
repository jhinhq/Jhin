"""Request/response schemas for tasks, runs, messages, and run timelines."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jhin_domain import TaskPriority


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=20_000)
    priority: TaskPriority = TaskPriority.NORMAL
    agent_id: UUID | None = None  # assign + start immediately when set


class TaskAssign(BaseModel):
    """Body of POST /agents/{id}/assign-task (agent comes from the path)."""

    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=20_000)
    priority: TaskPriority = TaskPriority.NORMAL


class AgentMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class InstructionIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str
    state: str
    priority: str
    assigned_agent_id: UUID | None
    temporal_workflow_id: str | None
    # Trigger origin (plan 6.12, 17.10): where this task came from, when it
    # was started by a trigger. metadata_json carries trigger_name and
    # external_url for the "Started by trigger X from Linear ENG-142" banner.
    external_source: str | None = None
    external_id: str | None = None
    trigger_id: UUID | None = None
    # Delegation lineage (plan 6.12): set when this task is a child created
    # by organization.delegate_task or a workflow template hop.
    parent_task_id: UUID | None = None
    metadata_json: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID | None
    agent_id: UUID
    status: str
    model_profile_id: UUID | None
    snapshot_hash: str
    started_at: datetime | None
    completed_at: datetime | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated_cost_micros: int
    steps_used: int
    error_code: str | None
    error_message: str | None
    created_at: datetime


class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    seq: int
    event_type: str
    payload_json: dict[str, Any]
    created_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID | None
    run_id: UUID | None
    sender_type: str
    sender_id: UUID | None
    message_type: str
    content_json: dict[str, Any]
    created_at: datetime


class ToolCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    agent_id: UUID
    tool_name: str
    sanitized_input_json: dict[str, Any]
    sanitized_output_json: dict[str, Any]
    status: str
    approval_id: UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_code: str | None
    created_at: datetime


class TaskTreeNodeOut(BaseModel):
    """One node of a delegation chain (plan 45 'Task parent/child display')."""

    task: TaskOut
    agent_name: str | None = None
    latest_run_status: str | None = None
    children: list[TaskTreeNodeOut] = Field(default_factory=list)


class TaskTreeOut(BaseModel):
    root: TaskTreeNodeOut
    # The task the caller asked about, so the UI can highlight it in the tree.
    focus_task_id: UUID


class TaskDetailOut(BaseModel):
    task: TaskOut
    runs: list[RunOut]
    total_input_tokens: int
    total_output_tokens: int
    total_cost_micros: int


class TaskListOut(BaseModel):
    items: list[TaskOut]
    total: int


class RunListOut(BaseModel):
    items: list[RunOut]
    total: int
