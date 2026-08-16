/**
 * Pure org-chart shaping: turns the flat org-graph payload (teams, agents,
 * manager edges) into nested trees for rendering. No React, fully unit-tested.
 */

import type { OrgAgentNode, OrgTeamNode } from "@/lib/types";

export interface AgentTreeNode {
  agent: OrgAgentNode;
  reports: AgentTreeNode[];
}

export interface TeamTreeNode {
  team: OrgTeamNode;
  children: TeamTreeNode[];
  /** Manager-rooted agent hierarchy for agents assigned to this team. */
  agents: AgentTreeNode[];
}

export interface OrgTree {
  roots: TeamTreeNode[];
  /** Agents without a team, still shaped by manager relationships. */
  unassigned: AgentTreeNode[];
}

function sortAgents(nodes: AgentTreeNode[]): AgentTreeNode[] {
  return nodes.sort((a, b) => a.agent.name.localeCompare(b.agent.name));
}

/**
 * Build the manager hierarchy for one group of agents (a team or the
 * unassigned pool). An agent is a root when its manager is missing or outside
 * the group. Defensive against cycles even though the server rejects them.
 */
export function buildAgentForest(agents: OrgAgentNode[]): AgentTreeNode[] {
  const inGroup = new Map(agents.map((agent) => [agent.id, agent]));
  const nodes = new Map<string, AgentTreeNode>(
    agents.map((agent) => [agent.id, { agent, reports: [] }]),
  );
  const roots: AgentTreeNode[] = [];

  for (const agent of agents) {
    const node = nodes.get(agent.id)!;
    const managerInGroup =
      agent.manager_agent_id !== null && inGroup.has(agent.manager_agent_id);
    if (managerInGroup) {
      nodes.get(agent.manager_agent_id!)!.reports.push(node);
    } else {
      roots.push(node);
    }
  }

  // Cycle guard: nodes caught in a loop are unreachable from any root.
  // Promote one node per loop to a root and sever the back-edge so the chart
  // never drops agents or recurses forever (the server rejects cycles anyway).
  const reachable = new Set<string>();
  const prune = (node: AgentTreeNode) => {
    reachable.add(node.agent.id);
    node.reports = node.reports.filter((report) => !reachable.has(report.agent.id));
    node.reports.forEach(prune);
  };
  roots.forEach(prune);
  for (const agent of agents) {
    if (!reachable.has(agent.id)) {
      const node = nodes.get(agent.id)!;
      roots.push(node);
      prune(node);
    }
  }

  for (const node of nodes.values()) sortAgents(node.reports);
  return sortAgents(roots);
}

export function buildOrgTree(graph: {
  teams: OrgTeamNode[];
  agents: OrgAgentNode[];
}): OrgTree {
  const teamNodes = new Map<string, TeamTreeNode>(
    graph.teams.map((team) => [team.id, { team, children: [], agents: [] }]),
  );

  const agentsByTeam = new Map<string | null, OrgAgentNode[]>();
  for (const agent of graph.agents) {
    const key = agent.team_id !== null && teamNodes.has(agent.team_id) ? agent.team_id : null;
    const bucket = agentsByTeam.get(key) ?? [];
    bucket.push(agent);
    agentsByTeam.set(key, bucket);
  }

  for (const [teamId, node] of teamNodes) {
    node.agents = buildAgentForest(agentsByTeam.get(teamId) ?? []);
  }

  const roots: TeamTreeNode[] = [];
  for (const node of teamNodes.values()) {
    const parentId = node.team.parent_team_id;
    if (parentId !== null && parentId !== node.team.id && teamNodes.has(parentId)) {
      teamNodes.get(parentId)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  const sortTeams = (nodes: TeamTreeNode[]) => {
    nodes.sort((a, b) => a.team.name.localeCompare(b.team.name));
    nodes.forEach((node) => sortTeams(node.children));
  };
  sortTeams(roots);

  return { roots, unassigned: buildAgentForest(agentsByTeam.get(null) ?? []) };
}

/** Count of all agents in the subtree of a team node (for badges). */
export function countTeamAgents(node: TeamTreeNode): number {
  const own = countForest(node.agents);
  return own + node.children.reduce((sum, child) => sum + countTeamAgents(child), 0);
}

function countForest(nodes: AgentTreeNode[]): number {
  return nodes.reduce((sum, node) => sum + 1 + countForest(node.reports), 0);
}
