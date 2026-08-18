"""Request/response contracts for teams (plan 6.4)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from jhin_api.agents.schemas import MembershipState


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    parent_team_id: UUID | None = None
    manager_agent_id: UUID | None = None
    color_token: str = Field(default="slate", max_length=32)
    icon: str = Field(default="users", max_length=64)


class TeamUpdate(BaseModel):
    """PATCH semantics: omitted fields are untouched; explicit nulls clear."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    parent_team_id: UUID | None = None
    manager_agent_id: UUID | None = None
    color_token: str | None = Field(default=None, max_length=32)
    icon: str | None = Field(default=None, max_length=64)


class TeamMemberOut(BaseModel):
    membership_id: UUID
    agent_id: UUID
    name: str
    slug: str
    role_title: str
    is_primary: bool
    role_label: str
    joined_at: datetime
    state: MembershipState = "active"


class TeamMembershipGroups(BaseModel):
    primary: list[TeamMemberOut] = Field(default_factory=list)
    secondary: list[TeamMemberOut] = Field(default_factory=list)


class TeamOut(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    description: str
    parent_team_id: UUID | None
    manager_agent_id: UUID | None
    color_token: str
    icon: str
    memberships: TeamMembershipGroups = Field(default_factory=TeamMembershipGroups)
    created_at: datetime
    updated_at: datetime
