"""Builds the organization graph for one workspace."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents.schemas import AgentMembershipOut, AgentRelationshipOut
from jhin_api.agents.service import list_agents, list_memberships, list_relationships
from jhin_api.org.schemas import OrgAgentNode, OrgGraph, OrgTeamNode
from jhin_api.teams.service import get_team_memberships, list_teams
from jhin_domain import AgentStatus


async def get_org_graph(db: AsyncSession, workspace_id: UUID) -> OrgGraph:
    teams = await list_teams(db, workspace_id)
    agents = await list_agents(db, workspace_id)
    team_memberships = {
        team.id: await get_team_memberships(db, workspace_id, team.id) for team in teams
    }
    agent_memberships = {
        agent.id: await list_memberships(db, workspace_id, agent.id) for agent in agents
    }
    agent_relationships = {
        agent.id: await list_relationships(db, workspace_id, agent.id) for agent in agents
    }
    return OrgGraph(
        workspace_id=workspace_id,
        teams=[
            OrgTeamNode(
                id=team.id,
                name=team.name,
                description=team.description,
                parent_team_id=team.parent_team_id,
                manager_agent_id=team.manager_agent_id,
                color_token=team.color_token,
                icon=team.icon,
                memberships=team_memberships[team.id],
            )
            for team in teams
        ],
        agents=[
            OrgAgentNode(
                id=agent.id,
                name=agent.name,
                slug=agent.slug,
                role_title=agent.role_title,
                public_purpose=agent.public_purpose,
                expertise_json=agent.expertise_json,
                discoverability=agent.discoverability,
                availability=agent.availability,
                status=AgentStatus(agent.status),
                team_id=agent.team_id,
                manager_agent_id=agent.manager_agent_id,
                memberships=[
                    AgentMembershipOut(
                        id=membership.id,
                        workspace_id=membership.workspace_id,
                        agent_id=membership.agent_id,
                        team_id=membership.team_id,
                        is_primary=membership.is_primary,
                        role_label=membership.role_label,
                        joined_at=membership.joined_at,
                        left_at=membership.left_at,
                        state="active",
                    )
                    for membership in agent_memberships[agent.id]
                ],
                relationships=[
                    AgentRelationshipOut.model_validate(relationship, from_attributes=True)
                    for relationship in agent_relationships[agent.id]
                ],
            )
            for agent in agents
        ],
    )
