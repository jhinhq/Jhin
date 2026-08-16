"""Builds the organization graph for one workspace."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents.service import list_agents
from jhin_api.org.schemas import OrgAgentNode, OrgGraph, OrgTeamNode
from jhin_api.teams.service import list_teams
from jhin_domain import AgentStatus


async def get_org_graph(db: AsyncSession, workspace_id: UUID) -> OrgGraph:
    teams = await list_teams(db, workspace_id)
    agents = await list_agents(db, workspace_id)
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
            )
            for team in teams
        ],
        agents=[
            OrgAgentNode(
                id=agent.id,
                name=agent.name,
                slug=agent.slug,
                role_title=agent.role_title,
                status=AgentStatus(agent.status),
                team_id=agent.team_id,
                manager_agent_id=agent.manager_agent_id,
            )
            for agent in agents
        ],
    )
