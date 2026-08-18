"""Organization graph contract, shaped for the org chart UI (plan 17.4)."""

from uuid import UUID

from pydantic import BaseModel, Field

from jhin_api.agents.schemas import (
    AgentMembershipOut,
    AgentRelationshipOut,
    Availability,
    Discoverability,
)
from jhin_api.teams.schemas import TeamMembershipGroups
from jhin_domain import AgentStatus


class OrgTeamNode(BaseModel):
    id: UUID
    name: str
    description: str
    parent_team_id: UUID | None
    manager_agent_id: UUID | None
    color_token: str
    icon: str
    memberships: TeamMembershipGroups = Field(default_factory=TeamMembershipGroups)


class OrgAgentNode(BaseModel):
    id: UUID
    name: str
    slug: str
    role_title: str
    public_purpose: str
    expertise_json: list[str]
    discoverability: Discoverability
    availability: Availability
    status: AgentStatus
    team_id: UUID | None
    manager_agent_id: UUID | None
    memberships: list[AgentMembershipOut] = Field(default_factory=list)
    relationships: list[AgentRelationshipOut] = Field(default_factory=list)


class OrgGraph(BaseModel):
    """Teams + agents + manager edges (edges are the *_id fields on nodes)."""

    workspace_id: UUID
    teams: list[OrgTeamNode]
    agents: list[OrgAgentNode]
