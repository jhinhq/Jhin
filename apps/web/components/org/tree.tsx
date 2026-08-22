"use client";

/** Presentational org-chart pieces: nested team cards and manager-rooted
 * agent hierarchies (plan 17.4). Pure props, unit-testable. */

import {
  Megaphone,
  Pencil,
  PenLine,
  Rocket,
  Shield,
  Trash2,
  UserPlus,
  Users,
  Wrench,
} from "lucide-react";
import { Badge, Button, focusRing, StatusDot } from "@/components/ui";
import { countTeamAgents, type AgentTreeNode, type TeamTreeNode } from "@/lib/org-tree";
import type { OrgAgentNode, OrgTeamNode } from "@/lib/types";

export const TEAM_ICONS: Record<string, typeof Users> = {
  users: Users,
  wrench: Wrench,
  megaphone: Megaphone,
  rocket: Rocket,
  shield: Shield,
  pen: PenLine,
};

export function AgentCard({
  node,
  managerName,
  onOpen,
}: {
  node: AgentTreeNode;
  managerName?: string;
  onOpen: (agent: OrgAgentNode) => void;
}) {
  const { agent } = node;
  return (
    <div>
      <button
        onClick={() => onOpen(agent)}
        className={`group flex w-full items-center gap-3 rounded-xl border border-line bg-raised px-3.5 py-2.5 text-left transition-colors hover:border-accent hover:bg-hover ${focusRing}`}
      >
        <StatusDot status={agent.status} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{agent.name}</span>
          <span className="block truncate text-xs text-dim">
            {agent.role_title || "Agent"}
            {managerName ? ` · reports to ${managerName}` : ""}
          </span>
        </span>
        {agent.status !== "active" ? (
          <Badge tone={agent.status === "paused" ? "warn" : "neutral"}>{agent.status}</Badge>
        ) : null}
      </button>
      {node.reports.length > 0 ? (
        <div className="ml-4 mt-2 space-y-2 border-l border-line-strong pl-4">
          {node.reports.map((report) => (
            <AgentCard
              key={report.agent.id}
              node={report}
              managerName={agent.name}
              onOpen={onOpen}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function TeamCard({
  node,
  depth,
  isAdmin,
  onOpenAgent,
  onEditTeam,
  onDeleteTeam,
  onAddAgent,
  managerNameFor,
}: {
  node: TeamTreeNode;
  depth: number;
  isAdmin: boolean;
  onOpenAgent: (agent: OrgAgentNode) => void;
  onEditTeam: (team: OrgTeamNode) => void;
  onDeleteTeam: (team: OrgTeamNode) => void;
  onAddAgent: (teamId: string) => void;
  managerNameFor: (agent: OrgAgentNode) => string | undefined;
}) {
  const Icon = TEAM_ICONS[node.team.icon] ?? Users;
  const agentCount = countTeamAgents(node);
  return (
    <section
      className={`team-accent-${node.team.color_token} rounded-2xl border border-line bg-surface shadow-card`}
      style={{ borderLeft: "3px solid var(--team, var(--line-strong))" }}
    >
      <header className="flex items-center justify-between gap-3 px-5 py-4">
        <div className="flex min-w-0 items-center gap-2.5">
          <Icon size={15} style={{ color: "var(--team)" }} strokeWidth={1.8} />
          <h3 className="truncate font-display text-base font-semibold">{node.team.name}</h3>
          <span className="text-xs tabular-nums text-faint">
            {agentCount} agent{agentCount === 1 ? "" : "s"}
          </span>
        </div>
        {isAdmin ? (
          <div className="flex shrink-0 items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              title="Add agent to this team"
              onClick={() => onAddAgent(node.team.id)}
            >
              <UserPlus size={13} />
            </Button>
            <Button
              size="sm"
              variant="ghost"
              title="Edit team"
              onClick={() => onEditTeam(node.team)}
            >
              <Pencil size={13} />
            </Button>
            <Button
              size="sm"
              variant="ghost"
              title="Delete team"
              onClick={() => onDeleteTeam(node.team)}
            >
              <Trash2 size={13} />
            </Button>
          </div>
        ) : null}
      </header>
      {node.team.description ? (
        <p className="-mt-1 px-5 pb-2 text-sm text-dim">{node.team.description}</p>
      ) : null}
      <div className="space-y-2 px-5 pb-4">
        {node.agents.length === 0 && node.children.length === 0 ? (
          <p className="rounded-xl border border-dashed border-line-strong px-3 py-3 text-center text-sm text-faint">
            No agents yet.
          </p>
        ) : (
          node.agents.map((agentNode) => (
            <AgentCard
              key={agentNode.agent.id}
              node={agentNode}
              managerName={managerNameFor(agentNode.agent)}
              onOpen={onOpenAgent}
            />
          ))
        )}
        {node.children.length > 0 ? (
          <div className={`space-y-3 ${depth < 3 ? "ml-3 border-l border-line-strong pl-3" : ""}`}>
            {node.children.map((child) => (
              <TeamCard
                key={child.team.id}
                node={child}
                depth={depth + 1}
                isAdmin={isAdmin}
                onOpenAgent={onOpenAgent}
                onEditTeam={onEditTeam}
                onDeleteTeam={onDeleteTeam}
                onAddAgent={onAddAgent}
                managerNameFor={managerNameFor}
              />
            ))}
          </div>
        ) : null}
      </div>
    </section>
  );
}
