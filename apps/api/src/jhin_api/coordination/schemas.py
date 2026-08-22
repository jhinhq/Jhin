"""Request/response contracts for work requests, review policies, reviews,
and manager rollups."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jhin_domain import ReviewMode, ReviewScopeKind, ReviewVerdict
from jhin_policy import ReviewCondition, ReviewerSelector


class WorkRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    conversation_id: UUID | None
    requester_agent_id: UUID
    requester_task_id: UUID | None
    requester_run_id: UUID | None
    root_task_id: UUID | None
    requested_by_user_id: UUID | None
    target_agent_id: UUID
    title: str
    description: str
    expected_output: str
    status: str
    idempotency_key: str
    depth: int
    created_task_id: UUID | None
    response: str
    responded_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    requester_agent_name: str | None = None
    target_agent_name: str | None = None


class WorkRequestListOut(BaseModel):
    items: list[WorkRequestOut]
    total: int


class WorkRequestCreate(BaseModel):
    """A human opens a request on behalf of ``requester_agent_id``."""

    requester_agent_id: UUID
    target_agent_id: UUID
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20_000)
    expected_output: str = Field(default="", max_length=4_000)
    requester_task_id: UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class WorkRequestResponseIn(BaseModel):
    response: str = Field(default="", max_length=4_000)


class ReviewPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    scope_kind: ReviewScopeKind = ReviewScopeKind.WORKSPACE
    scope_id: UUID | None = None
    scope_key: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool = True
    mode: ReviewMode = ReviewMode.BEFORE_CLOSE
    conditions: list[ReviewCondition] = Field(default_factory=list, max_length=20)
    reviewer: ReviewerSelector = Field(default_factory=ReviewerSelector)
    fail_closed: bool = False
    priority: int = Field(default=100, ge=0, le=10_000)
    period_seconds: int | None = Field(default=None, ge=60, le=30 * 24 * 3600)

    @model_validator(mode="after")
    def _scope_shape(self) -> ReviewPolicyIn:
        if self.scope_kind is ReviewScopeKind.WORKSPACE:
            if self.scope_id is not None or self.scope_key is not None:
                raise ValueError("workspace scope takes neither scope_id nor scope_key")
        elif self.scope_kind in (ReviewScopeKind.TEAM, ReviewScopeKind.AGENT):
            if self.scope_id is None or self.scope_key is not None:
                raise ValueError(f"{self.scope_kind.value} scope requires scope_id only")
        elif self.scope_id is not None or self.scope_key is None:
            raise ValueError("task_type scope requires scope_key only")
        if self.mode is ReviewMode.PERIODIC and self.period_seconds is None:
            raise ValueError("periodic mode requires period_seconds")
        return self


class ReviewPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    mode: ReviewMode | None = None
    conditions: list[ReviewCondition] | None = Field(default=None, max_length=20)
    reviewer: ReviewerSelector | None = None
    fail_closed: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)
    period_seconds: int | None = Field(default=None, ge=60, le=30 * 24 * 3600)


class ReviewPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    scope_kind: str
    scope_id: UUID | None
    scope_key: str | None
    enabled: bool
    mode: str
    conditions_json: list[Any]
    reviewer_selector_json: dict[str, Any]
    fail_closed: bool
    priority: int
    period_seconds: int | None
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


class WorkReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    policy_id: UUID | None
    task_id: UUID | None
    run_id: UUID | None
    tool_call_id: UUID | None
    work_request_id: UUID | None
    subject_agent_id: UUID | None
    trigger_key: str
    mode: str
    evidence_json: dict[str, Any]
    reviewer_type: str
    reviewer_agent_id: UUID | None
    reviewer_user_id: UUID | None
    status: str
    verdict: str | None
    feedback: str
    requested_at: datetime
    decided_at: datetime | None
    decided_by_user_id: UUID | None
    decided_by_agent_id: UUID | None
    created_at: datetime
    subject_agent_name: str | None = None
    reviewer_agent_name: str | None = None
    task_title: str | None = None


class WorkReviewListOut(BaseModel):
    items: list[WorkReviewOut]
    total: int
    pending_count: int


class ReviewDecisionIn(BaseModel):
    verdict: ReviewVerdict
    feedback: str = Field(default="", max_length=4_000)
