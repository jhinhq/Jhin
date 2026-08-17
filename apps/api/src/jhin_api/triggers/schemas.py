"""Request/response schemas for triggers (plan 6.11, 10.3, 17.10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jhin_domain import TriggerActionType, TriggerType


class TriggerCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    trigger_type: TriggerType = TriggerType.CONNECTOR_EVENT
    connection_id: UUID | None = None
    event_type: str | None = Field(default=None, max_length=200)
    # The safe JSON filter DSL (plan 10.2); validated before storing.
    filter: dict[str, Any] = Field(default_factory=dict)
    action_type: TriggerActionType = TriggerActionType.START_AGENT_TASK
    target_agent_id: UUID | None = None
    target_team_id: UUID | None = None
    action_config: dict[str, Any] = Field(default_factory=dict)
    dedupe_window_seconds: int = Field(default=300, ge=0, le=86_400)


class TriggerUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    connection_id: UUID | None = None
    event_type: str | None = Field(default=None, max_length=200)
    filter: dict[str, Any] | None = None
    target_agent_id: UUID | None = None
    target_team_id: UUID | None = None
    action_config: dict[str, Any] | None = None
    dedupe_window_seconds: int | None = Field(default=None, ge=0, le=86_400)


class TriggerInvocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    trigger_id: UUID
    status: str
    event_id: UUID
    task_id: UUID | None
    workflow_id: str | None
    error: str | None
    created_at: datetime


class TriggerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    enabled: bool
    trigger_type: str
    connection_id: UUID | None
    event_type: str | None
    filter_json: dict[str, Any]
    action_type: str
    target_agent_id: UUID | None
    target_team_id: UUID | None
    action_config_json: dict[str, Any]
    dedupe_window_seconds: int
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime
    last_invocation: TriggerInvocationOut | None = None


class TriggerTestRequest(BaseModel):
    """Dry-run a trigger's filter against a sample event (plan 10.3)."""

    event: dict[str, Any] = Field(default_factory=dict)


class ConditionExplanation(BaseModel):
    path: str
    op: str
    value: Any = None
    passed: bool
    actual: Any = None
    actual_present: bool = False
    previous: Any = None
    previous_present: bool = False
    detail: str = ""


class TriggerTestResult(BaseModel):
    matched: bool
    event_type_matches: bool
    filter_matches: bool
    conditions: list[ConditionExplanation] = []
