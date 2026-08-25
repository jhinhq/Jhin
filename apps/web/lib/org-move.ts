/**
 * Pure rules for reorganising the company by moving one agent at a time.
 *
 * The Company page lets an admin drag an agent onto another agent (a
 * reporting line) or onto a group's top level (a team, or the Independent
 * pool). Everything that decides whether a move is legal, what it changes,
 * and how to describe it lives here so it can be unit-tested and reused by
 * both the drag path and the keyboard "Move…" menu.
 *
 * Drop semantics (documented once, applied everywhere):
 *
 * - Drop on **agent X** → the dragged agent reports to X *and joins X's team*.
 *   The org chart only nests an agent under its manager when both sit in the
 *   same group (see `buildAgentForest`), so following the manager into their
 *   team is what makes the reporting line actually render.
 * - Drop on a **group's top level** (a team card's "Top level" strip, or the
 *   Independent strip) → the agent joins that team (or leaves every team) and
 *   its manager is cleared. This is the un-nest affordance.
 *
 * Cycles are refused here, before any request is sent; the server enforces the
 * same rule and returns 409, which the UI still handles.
 */

import type { OrgAgentNode, OrgTeamNode } from "@/lib/types";

export type MoveTarget =
  | { kind: "group"; teamId: string | null }
  | { kind: "agent"; agentId: string };

export interface MoveGraph {
  teams: OrgTeamNode[];
  agents: OrgAgentNode[];
}

/** The two fields a move writes. Both are always sent explicitly so the
 * PATCH's "explicit null clears" semantics apply. */
export interface AgentMove {
  team_id: string | null;
  manager_agent_id: string | null;
}

export interface MoveCheck {
  ok: boolean;
  /** Plain-language sentence shown in a tooltip or next to a menu item. */
  reason?: string;
}

const OK: MoveCheck = { ok: true };

function agentMap(agents: OrgAgentNode[]): Map<string, OrgAgentNode> {
  return new Map(agents.map((agent) => [agent.id, agent]));
}

function nameOf(agents: Map<string, OrgAgentNode>, id: string): string {
  return agents.get(id)?.name ?? "That agent";
}

/**
 * Every agent that reports to `agentId`, directly or through a chain.
 * Walks downward from the given agent and is safe against a pre-existing
 * cycle in the data (each id is visited once).
 */
export function descendantIds(agentId: string, agents: OrgAgentNode[]): Set<string> {
  const reportsByManager = new Map<string, string[]>();
  for (const agent of agents) {
    if (agent.manager_agent_id === null) continue;
    const bucket = reportsByManager.get(agent.manager_agent_id) ?? [];
    bucket.push(agent.id);
    reportsByManager.set(agent.manager_agent_id, bucket);
  }
  const found = new Set<string>();
  const queue = [...(reportsByManager.get(agentId) ?? [])];
  while (queue.length > 0) {
    const id = queue.pop()!;
    if (id === agentId || found.has(id)) continue;
    found.add(id);
    queue.push(...(reportsByManager.get(id) ?? []));
  }
  return found;
}

/**
 * Can `agentId` be dropped on `target`? Group drops always clear the manager
 * so they can never make a loop; only agent-onto-agent drops can.
 */
export function canMove(agentId: string, target: MoveTarget, graph: MoveGraph): MoveCheck {
  const agents = agentMap(graph.agents);
  if (!agents.has(agentId)) {
    return { ok: false, reason: "That agent is no longer in the company. Reload the page." };
  }
  if (target.kind === "group") {
    if (target.teamId !== null && !graph.teams.some((team) => team.id === target.teamId)) {
      return { ok: false, reason: "That team is no longer in the company. Reload the page." };
    }
    return OK;
  }
  if (target.agentId === agentId) {
    return { ok: false, reason: `${nameOf(agents, agentId)} can’t report to themselves.` };
  }
  if (!agents.has(target.agentId)) {
    return { ok: false, reason: "That agent is no longer in the company. Reload the page." };
  }
  if (descendantIds(agentId, graph.agents).has(target.agentId)) {
    return {
      ok: false,
      reason: `${nameOf(agents, target.agentId)} already reports to ${nameOf(agents, agentId)}, so this would loop.`,
    };
  }
  return OK;
}

/** The fields a legal move writes, or `null` when the move is illegal or
 * would change nothing. */
export function resolveMove(
  agentId: string,
  target: MoveTarget,
  graph: MoveGraph,
): AgentMove | null {
  if (!canMove(agentId, target, graph).ok) return null;
  const agents = agentMap(graph.agents);
  const agent = agents.get(agentId);
  if (!agent) return null;
  const next: AgentMove =
    target.kind === "group"
      ? { team_id: target.teamId, manager_agent_id: null }
      : {
          team_id: agents.get(target.agentId)?.team_id ?? null,
          manager_agent_id: target.agentId,
        };
  if (next.team_id === agent.team_id && next.manager_agent_id === agent.manager_agent_id) {
    return null;
  }
  return next;
}

/** Direct reports that stay behind when `agentId` leaves its current team.
 * Reported honestly in the confirmation message instead of silently dropping
 * the reporting lines from the chart. */
export function reportsLeftBehind(
  agentId: string,
  move: AgentMove,
  graph: MoveGraph,
): OrgAgentNode[] {
  const agent = graph.agents.find((candidate) => candidate.id === agentId);
  if (!agent || agent.team_id === move.team_id) return [];
  return graph.agents.filter(
    (candidate) => candidate.manager_agent_id === agentId && candidate.team_id === agent.team_id,
  );
}

function teamName(graph: MoveGraph, teamId: string | null): string {
  if (teamId === null) return "Independent";
  return graph.teams.find((team) => team.id === teamId)?.name ?? "a team";
}

/** One plain sentence describing what a move did, for the live region and
 * the inline confirmation. */
export function describeMove(agentId: string, move: AgentMove, graph: MoveGraph): string {
  const agents = agentMap(graph.agents);
  const who = nameOf(agents, agentId);
  const where =
    move.team_id === null ? "Independent (no team)" : `the ${teamName(graph, move.team_id)} team`;
  const line =
    move.manager_agent_id === null
      ? "reporting to no one"
      : `reporting to ${nameOf(agents, move.manager_agent_id)}`;
  const stranded = reportsLeftBehind(agentId, move, graph);
  let tail = "";
  if (stranded.length > 0) {
    const from = teamName(graph, agents.get(agentId)?.team_id ?? null);
    const noun = stranded.length === 1 ? "report" : "reports";
    tail = ` ${stranded.length} direct ${noun} stayed in ${from}.`;
  }
  return `${who} moved to ${where}, ${line}.${tail}`;
}

export interface MoveOption {
  /** Stable id for React keys and menu item identity. */
  id: string;
  label: string;
  target: MoveTarget;
  check: MoveCheck;
  /** True when picking this option would change nothing. */
  current: boolean;
}

export interface MoveOptionGroup {
  label: string;
  options: MoveOption[];
}

/**
 * Menu contents for the keyboard path: every team's top level, Independent,
 * and every possible manager. Invalid managers stay in the list carrying
 * their reason so the refusal is explained rather than hidden.
 */
export function moveOptions(agentId: string, graph: MoveGraph): MoveOptionGroup[] {
  const agent = graph.agents.find((candidate) => candidate.id === agentId);
  const teams = [...graph.teams].sort((a, b) => a.name.localeCompare(b.name));
  const groupOptions: MoveOption[] = [
    ...teams.map((team) => ({
      id: `group:${team.id}`,
      label: `${team.name} — top level`,
      target: { kind: "group", teamId: team.id } as MoveTarget,
      check: canMove(agentId, { kind: "group", teamId: team.id }, graph),
      current: agent?.team_id === team.id && agent?.manager_agent_id === null,
    })),
    {
      id: "group:none",
      label: "Independent — no team, no manager",
      target: { kind: "group", teamId: null },
      check: canMove(agentId, { kind: "group", teamId: null }, graph),
      current: agent?.team_id === null && agent?.manager_agent_id === null,
    },
  ];

  const managerOptions: MoveOption[] = [...graph.agents]
    .filter((candidate) => candidate.id !== agentId)
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((candidate) => ({
      id: `agent:${candidate.id}`,
      label: `${candidate.name}${candidate.role_title ? ` · ${candidate.role_title}` : ""}`,
      target: { kind: "agent", agentId: candidate.id } as MoveTarget,
      check: canMove(agentId, { kind: "agent", agentId: candidate.id }, graph),
      current: agent?.manager_agent_id === candidate.id,
    }));

  return [
    { label: "Move to a team", options: groupOptions },
    { label: "Report to an agent", options: managerOptions },
  ];
}

/** Pending optimistic moves, keyed by agent id. `seq` lets a settled request
 * clear only its own entry, so a newer move for the same agent survives. */
export interface PendingMove extends AgentMove {
  seq: number;
}

/** Overlay pending moves on the fetched graph so the chart updates the
 * instant the drop lands. Pure, so rapid moves compose predictably. */
export function applyPendingMoves(
  agents: OrgAgentNode[],
  pending: Record<string, PendingMove>,
): OrgAgentNode[] {
  if (Object.keys(pending).length === 0) return agents;
  return agents.map((agent) => {
    const move = pending[agent.id];
    if (!move) return agent;
    return { ...agent, team_id: move.team_id, manager_agent_id: move.manager_agent_id };
  });
}
