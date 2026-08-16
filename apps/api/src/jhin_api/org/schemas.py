"""Organization graph contract, shaped for the org chart UI (plan 17.4)."""

from uuid import UUID

from pydantic import BaseModel

from jhin_domain import AgentStatus


class OrgTeamNode(BaseModel):
    id: UUID
    name: str
    description: str
    parent_team_id: UUID | None
    manager_agent_id: UUID | None
    color_token: str
    icon: str


class OrgAgentNode(BaseModel):
    id: UUID
    name: str
    slug: str
    role_title: str
    status: AgentStatus
    team_id: UUID | None
    manager_agent_id: UUID | None


class OrgGraph(BaseModel):
    """Teams + agents + manager edges (edges are the *_id fields on nodes)."""

    workspace_id: UUID
    teams: list[OrgTeamNode]
    agents: list[OrgAgentNode]
