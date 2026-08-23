"""Request/response contracts for workspaces and memberships."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from jhin_domain import WorkspaceRole


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    default_timezone: str = Field(default="UTC", max_length=64)


class DelegationSettingsIn(BaseModel):
    """Delegation knobs (plan 7.5): the structural depth guard."""

    model_config = ConfigDict(extra="forbid")

    max_task_depth: int = Field(default=5, ge=1, le=20)


class ConcurrencySettingsIn(BaseModel):
    """Workspace-wide concurrent-run ceiling (plan 30); null = no ceiling."""

    model_config = ConfigDict(extra="forbid")

    max_concurrent_runs: int | None = Field(default=None, ge=1, le=200)


class BudgetSettingsIn(BaseModel):
    """Monthly model-spend budget (micro-dollars); null = no budget. The
    warning threshold is the fraction of the budget at which the UI warns."""

    model_config = ConfigDict(extra="forbid")

    monthly_budget_micros: int | None = Field(default=None, ge=0, le=10**15)
    warning_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class WorkspaceSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delegation: DelegationSettingsIn | None = None
    concurrency: ConcurrencySettingsIn | None = None
    budget: BudgetSettingsIn | None = None


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    default_timezone: str | None = Field(default=None, max_length=64)
    default_model_profile_id: UUID | None = None
    settings: WorkspaceSettingsIn | None = None


class WorkspaceOut(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str
    default_timezone: str
    default_model_profile_id: UUID | None
    settings_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class MemberCreate(BaseModel):
    email: EmailStr
    role: WorkspaceRole


class MemberUpdate(BaseModel):
    role: WorkspaceRole


class MemberOut(BaseModel):
    id: UUID
    user_id: UUID
    email: str
    display_name: str
    role: WorkspaceRole
    created_at: datetime
