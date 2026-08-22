"use client";

/** Company overview: headline numbers plus an Outline (accessible nested
 * list) or Map (org chart) view of teams and agents. The chosen view is
 * remembered in localStorage["jhin-company-view"]; narrow screens default
 * to Outline. */

import { useMutation } from "@tanstack/react-query";
import { List, Network, Plus, Users } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, useSyncExternalStore } from "react";
import { PageHeader } from "@/components/app-shell";
import { Avatar } from "@/components/avatar";
import { statusTextOf } from "@/components/company/agent-helpers";
import { LoadError, Segmented, StatTile, StatusPill } from "@/components/company/bits";
import { useWorkingAgentIds } from "@/components/company/use-working";
import { TeamDialog } from "@/components/org/team-dialog";
import { AgentCard, TeamCard, TEAM_ICONS } from "@/components/org/tree";
import { Button, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useInvalidateOrg, useOrgGraph } from "@/lib/hooks";
import { buildOrgTree, countTeamAgents, type AgentTreeNode, type TeamTreeNode } from "@/lib/org-tree";
import type { OrgAgentNode, OrgTeamNode } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

type View = "outline" | "map";
const VIEW_KEY = "jhin-company-view";

const VIEW_EVENT = "jhin-company-view-change";

function readStoredView(): View | null {
  try {
    const raw = window.localStorage.getItem(VIEW_KEY);
    return raw === "outline" || raw === "map" ? raw : null;
  } catch {
    return null;
  }
}

function subscribeView(onChange: () => void) {
  window.addEventListener("storage", onChange);
  window.addEventListener(VIEW_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(VIEW_EVENT, onChange);
  };
}

/** Stored preference, else Map on wide screens and Outline on narrow ones.
 * The server snapshot is Outline so hydration never mismatches. */
function currentView(): View {
  return readStoredView() ?? (window.innerWidth >= 768 ? "map" : "outline");
}

function useCompanyView(): [View, (next: View) => void] {
  const view = useSyncExternalStore(subscribeView, currentView, () => "outline" as View);
  const change = (next: View) => {
    try {
      window.localStorage.setItem(VIEW_KEY, next);
    } catch {
      // Private mode or quota: fall through; the event still updates this tab.
    }
    window.dispatchEvent(new Event(VIEW_EVENT));
  };
  return [view, change];
}

function OutlineAgent({ node, working }: { node: AgentTreeNode; working: Set<string> }) {
  const { agent } = node;
  return (
    <li>
      <Link
        href={`/agents/${agent.id}`}
        className="flex items-center gap-3 rounded-xl px-2 py-2 transition-colors hover:bg-hover"
      >
        <Avatar name={agent.name} size="sm" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{agent.name}</span>
          <span className="block truncate text-xs text-dim">{agent.role_title || "Agent"}</span>
        </span>
        <StatusPill status={statusTextOf(agent, working.has(agent.id))} className="shrink-0" />
      </Link>
      {node.reports.length > 0 ? (
        <ul className="ml-5 border-l border-line pl-3" aria-label={`Reports to ${agent.name}`}>
          {node.reports.map((report) => (
            <OutlineAgent key={report.agent.id} node={report} working={working} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function OutlineTeam({
  node,
  working,
  managerName,
  isAdmin,
  onEdit,
}: {
  node: TeamTreeNode;
  working: Set<string>;
  managerName: (id: string | null) => string | undefined;
  isAdmin: boolean;
  onEdit: (team: OrgTeamNode) => void;
}) {
  const Icon = TEAM_ICONS[node.team.icon] ?? Users;
  const count = countTeamAgents(node);
  const manager = managerName(node.team.manager_agent_id);
  return (
    <li className={`team-accent-${node.team.color_token} rounded-2xl border border-line bg-surface p-4`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <Icon size={16} style={{ color: "var(--team)" }} strokeWidth={1.8} aria-hidden />
          <div className="min-w-0">
            <h3 className="truncate font-display text-base font-semibold tracking-tight">{node.team.name}</h3>
            <p className="text-xs text-dim">
              {count} {count === 1 ? "member" : "members"}
              {manager ? ` · led by ${manager}` : ""}
            </p>
          </div>
        </div>
        {isAdmin ? (
          <Button size="sm" variant="ghost" onClick={() => onEdit(node.team)}>
            Edit
          </Button>
        ) : null}
      </div>
      {node.team.description ? <p className="mt-1.5 text-[13px] text-dim">{node.team.description}</p> : null}
      {node.agents.length > 0 ? (
        <ul className="mt-3 space-y-0.5" aria-label={`${node.team.name} members`}>
          {node.agents.map((agentNode) => (
            <OutlineAgent key={agentNode.agent.id} node={agentNode} working={working} />
          ))}
        </ul>
      ) : node.children.length === 0 ? (
        <p className="mt-3 text-sm text-faint">No one here yet.</p>
      ) : null}
      {node.children.length > 0 ? (
        <ul className="mt-3 space-y-3 border-l border-line pl-3" aria-label={`Teams inside ${node.team.name}`}>
          {node.children.map((child) => (
            <OutlineTeam key={child.team.id} node={child} working={working} managerName={managerName} isAdmin={isAdmin} onEdit={onEdit} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export default function CompanyPage() {
  const router = useRouter();
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const graph = useOrgGraph(workspaceId);
  const working = useWorkingAgentIds(workspaceId);
  const invalidate = useInvalidateOrg(workspaceId);
  const isAdmin = can("admin");

  const [view, changeView] = useCompanyView();
  const [teamDialogOpen, setTeamDialogOpen] = useState(false);
  const [editingTeam, setEditingTeam] = useState<OrgTeamNode | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);

  const deleteTeam = useMutation({
    mutationFn: (teamId: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/teams/${teamId}`, { method: "DELETE" }),
    onSuccess: () => {
      setPageError(null);
      invalidate();
    },
    onError: (error) =>
      setPageError(
        `${error instanceof ApiError ? error.detail : "Deleting the team failed"}. Try again, or check Advanced → Audit for details.`,
      ),
  });

  const tree = useMemo(() => (graph.data ? buildOrgTree(graph.data) : null), [graph.data]);
  const agentById = useMemo(() => new Map((graph.data?.agents ?? []).map((agent) => [agent.id, agent])), [graph.data]);
  const managerName = (id: string | null) => (id ? agentById.get(id)?.name : undefined);
  const managerNameFor = (agent: OrgAgentNode) => managerName(agent.manager_agent_id);
  const openAgent = (agent: OrgAgentNode) => router.push(`/agents/${agent.id}`);

  const actions = isAdmin ? (
    <>
      <Button
        onClick={() => {
          setEditingTeam(null);
          setTeamDialogOpen(true);
        }}
      >
        <Plus size={14} /> New team
      </Button>
      <Link href="/agents/new">
        <Button variant="primary">
          <Plus size={14} /> New agent
        </Button>
      </Link>
    </>
  ) : null;

  return (
    <>
      <PageHeader title="Company" description="Teams, who leads them, and who reports to whom" actions={actions} />
      <div className="space-y-5 px-4 py-5 sm:px-8 sm:py-6">
        <ErrorNote message={pageError} />
        {graph.isPending ? (
          <Spinner label="Loading your company…" />
        ) : graph.isError || !graph.data || !tree ? (
          <LoadError what="the company structure" onRetry={() => void graph.refetch()} />
        ) : graph.data.agents.length === 0 && graph.data.teams.length === 0 ? (
          <EmptyState
            title="Build your company"
            description="Create teams like Engineering or Marketing, then add agents with roles and reporting lines."
            action={isAdmin ? <div className="flex gap-2">{actions}</div> : undefined}
          />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <StatTile label="Agents" value={graph.data.agents.length} />
              <StatTile label="Teams" value={graph.data.teams.length} />
              <StatTile label="Working now" value={working.size} hint="Agents with a task in progress" />
            </div>

            <div className="flex items-center justify-between gap-3">
              <h2 className="font-display text-lg font-semibold tracking-tight">Structure</h2>
              <Segmented
                label="View"
                value={view}
                onChange={changeView}
                options={[
                  { id: "outline", label: "Outline", icon: <List size={14} aria-hidden /> },
                  { id: "map", label: "Map", icon: <Network size={14} aria-hidden /> },
                ]}
              />
            </div>

            {view === "outline" ? (
              <ul className="grid gap-4 lg:grid-cols-2" aria-label="Teams">
                {tree.roots.map((node) => (
                  <OutlineTeam
                    key={node.team.id}
                    node={node}
                    working={working}
                    managerName={managerName}
                    isAdmin={isAdmin}
                    onEdit={(team) => {
                      setEditingTeam(team);
                      setTeamDialogOpen(true);
                    }}
                  />
                ))}
                {tree.unassigned.length > 0 ? (
                  <li className="rounded-2xl border border-dashed border-line-strong bg-surface/60 p-4">
                    <h3 className="font-display text-base font-semibold tracking-tight">Independent</h3>
                    <p className="text-xs text-dim">
                      {tree.unassigned.length} {tree.unassigned.length === 1 ? "agent" : "agents"} not on a team
                    </p>
                    <ul className="mt-3 space-y-0.5" aria-label="Independent agents">
                      {tree.unassigned.map((node) => (
                        <OutlineAgent key={node.agent.id} node={node} working={working} />
                      ))}
                    </ul>
                  </li>
                ) : null}
              </ul>
            ) : (
              <div className="overflow-x-auto rounded-2xl border border-line bg-raised/40 p-4">
                <div className="flex min-w-max items-start gap-4">
                  {tree.roots.map((node) => (
                    <div key={node.team.id} className="w-[22rem] shrink-0">
                      <TeamCard
                        node={node}
                        depth={0}
                        isAdmin={isAdmin}
                        onOpenAgent={openAgent}
                        onEditTeam={(team) => {
                          setEditingTeam(team);
                          setTeamDialogOpen(true);
                        }}
                        onDeleteTeam={(team) => {
                          if (
                            window.confirm(
                              `Delete team “${team.name}”? Its agents and sub-teams are kept, just detached.`,
                            )
                          ) {
                            deleteTeam.mutate(team.id);
                          }
                        }}
                        onAddAgent={(teamId) => router.push(`/agents/new?team=${teamId}`)}
                        managerNameFor={managerNameFor}
                      />
                    </div>
                  ))}
                  {tree.unassigned.length > 0 ? (
                    <section className="w-[22rem] shrink-0 rounded-xl border border-dashed border-line-strong bg-surface/50 px-5 py-4">
                      <h3 className="mb-3 text-sm font-semibold text-dim">Independent</h3>
                      <div className="space-y-2">
                        {tree.unassigned.map((node) => (
                          <AgentCard key={node.agent.id} node={node} managerName={managerNameFor(node.agent)} onOpen={openAgent} />
                        ))}
                      </div>
                    </section>
                  ) : null}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {graph.data ? (
        <TeamDialog
          open={teamDialogOpen}
          onClose={() => setTeamDialogOpen(false)}
          editing={editingTeam}
          teams={graph.data.teams}
          agents={graph.data.agents}
        />
      ) : null}
    </>
  );
}
