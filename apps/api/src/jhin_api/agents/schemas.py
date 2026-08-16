"""Request/response contracts for agents (plan 6.5)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from jhin_domain import AgentStatus, AutonomyLevel


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role_title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)
    system_prompt: str = Field(default="", max_length=100_000)
    team_id: UUID | None = None
    manager_agent_id: UUID | None = None
    status: AgentStatus = AgentStatus.ACTIVE
    autonomy_level: AutonomyLevel = AutonomyLevel.SUPERVISED
    # Null = inherit the workspace default profile (plan 15.2).
    model_profile_id: UUID | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_steps: int = Field(default=20, ge=1, le=500)
    max_run_minutes: int = Field(default=30, ge=1, le=24 * 60)
    monthly_budget_cents: int | None = Field(default=None, ge=0)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    """PATCH semantics: omitted fields are untouched; explicit nulls clear."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    role_title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    system_prompt: str | None = Field(default=None, max_length=100_000)
    team_id: UUID | None = None
    manager_agent_id: UUID | None = None
    status: AgentStatus | None = None
    autonomy_level: AutonomyLevel | None = None
    model_profile_id: UUID | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_steps: int | None = Field(default=None, ge=1, le=500)
    max_run_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    monthly_budget_cents: int | None = Field(default=None, ge=0)
    metadata_json: dict[str, Any] | None = None


class AgentOut(BaseModel):
    id: UUID
    workspace_id: UUID
    team_id: UUID | None
    manager_agent_id: UUID | None
    name: str
    slug: str
    role_title: str
    description: str
    system_prompt: str
    status: AgentStatus
    autonomy_level: AutonomyLevel
    model_profile_id: UUID | None
    temperature: float | None
    max_output_tokens: int | None
    max_steps: int
    max_run_minutes: int
    monthly_budget_cents: int | None
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
