"use client";

/** Company overview: headline numbers plus an Outline (accessible nested
 * list) or Map (org chart) view of teams and agents. The chosen view is
 * remembered in localStorage["jhin-company-view"]; narrow screens default
 * to Outline.
 *
 * Admins can reorganise the company in place. The Map view supports drag and
 * drop (@dnd-kit, so it works with a keyboard and announces itself); both
 * views give every agent a "Move…" menu, which is the primary path on the
 * Outline view because that view is the mobile default and dragging inside a
 * nested, scrolling list on a touch screen is fiddly and error-prone.
 * Moves apply optimistically and revert with a plain-language message if the
 * server refuses them. See lib/org-move.ts for the drop semantics. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { List, Network, Plus, Users } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState, useSyncExternalStore } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Avatar } from "@/components/avatar";
import { statusTextOf } from "@/components/company/agent-helpers";
import { identityAvatarProps } from "@/lib/media";
import { LoadError, Segmented, StatTile, StatusPill } from "@/components/company/bits";
import { menuOnlyMoveApi, OrgDnd } from "@/components/company/org-dnd";
import { useWorkingAgentIds } from "@/components/company/use-working";
import { TeamDialog } from "@/components/org/team-dialog";
import { AgentCard, TeamCard, TEAM_ICONS, type OrgMoveApi } from "@/components/org/tree";
import { Button, ButtonLink, ConfirmDialog, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useAgentAvatarMap, useInvalidateOrg, useOrgGraph } from "@/lib/hooks";
import {
  applyPendingMoves,
  canMove,
  describeMove,
  resolveMove,
  type MoveGraph,
  type MoveTarget,
  type PendingMove,
} from "@/lib/org-move";
import { buildOrgTree, countTeamAgents, type AgentTreeNode, type TeamTreeNode } from "@/lib/org-tree";
import type { OrgAgentNode, OrgGraph, OrgTeamNode } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

/** Monotonic token per move. Module scope, not a ref, so the callbacks that
 * read it stay safe to call while rendering. */
let moveSeqCounter = 0;
const nextMoveSeq = () => (moveSeqCounter += 1);

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

function OutlineAgent({
  node,
  working,
  move,
}: {
  node: AgentTreeNode;
  working: Set<string>;
  move?: OrgMoveApi;
}) {
  const { agent } = node;
  return (
    <li>
      <div className="flex items-center gap-1">
        <Link
          href={`/agents/${agent.id}`}
          className="flex min-w-0 flex-1 items-center gap-3 rounded-xl px-2 py-2 transition-colors hover:bg-hover"
        >
          <Avatar name={agent.name} size="sm" {...identityAvatarProps(agent)} />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium">{agent.name}</span>
            <span className="block truncate text-xs text-dim">{agent.role_title || "Agent"}</span>
          </span>
          <StatusPill status={statusTextOf(agent, working.has(agent.id))} className="shrink-0" />
        </Link>
        {move?.agentMenu(agent)}
      </div>
      {node.reports.length > 0 ? (
        <ul className="ml-5 border-l border-line pl-3" aria-label={`Reports to ${agent.name}`}>
          {node.reports.map((report) => (
            <OutlineAgent key={report.agent.id} node={report} working={working} move={move} />
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
  move,
}: {
  node: TeamTreeNode;
  working: Set<string>;
  managerName: (id: string | null) => string | undefined;
  isAdmin: boolean;
  onEdit: (team: OrgTeamNode) => void;
  move?: OrgMoveApi;
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
            <OutlineAgent key={agentNode.agent.id} node={agentNode} working={working} move={move} />
          ))}
        </ul>
      ) : node.children.length === 0 ? (
        <p className="mt-3 text-sm text-faint">No one here yet.</p>
      ) : null}
      {node.children.length > 0 ? (
        <ul className="mt-3 space-y-3 border-l border-line pl-3" aria-label={`Teams inside ${node.team.name}`}>
          {node.children.map((child) => (
            <OutlineTeam
              key={child.team.id}
              node={child}
              working={working}
              managerName={managerName}
              isAdmin={isAdmin}
              onEdit={onEdit}
              move={move}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export default function CompanyPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const graph = useOrgGraph(workspaceId);
  const working = useWorkingAgentIds(workspaceId);
  const invalidate = useInvalidateOrg(workspaceId);
  const isAdmin = can("admin");

  const [view, changeView] = useCompanyView();
  const [teamDialogOpen, setTeamDialogOpen] = useState(false);
  const [editingTeam, setEditingTeam] = useState<OrgTeamNode | null>(null);
  const [deletingTeam, setDeletingTeam] = useState<OrgTeamNode | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  /** In-flight moves, keyed by agent. Drawn on top of the fetched graph so a
   * drop lands instantly; removed when the request settles. Keying by agent
   * (rather than snapshotting the whole graph) is what keeps several rapid
   * moves from clobbering one another. */
  const [pendingMoves, setPendingMoves] = useState<Record<string, PendingMove>>({});
  const [moveNote, setMoveNote] = useState<string | null>(null);

  const deleteTeam = useMutation({
    mutationFn: (teamId: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/teams/${teamId}`, { method: "DELETE" }),
    onSuccess: () => {
      setPageError(null);
      setDeletingTeam(null);
      invalidate();
    },
    onError: (error) => {
      // Close the dialog: the error note renders behind its backdrop.
      setDeletingTeam(null);
      setPageError(
        `${error instanceof ApiError ? error.detail : "Deleting the team failed"}. Try again, or check Advanced → Audit for details.`,
      );
    },
  });

  // Org-graph nodes carry no avatar; merge the directory's avatars so the
  // outline and map show the same picture as the profile page.
  const avatars = useAgentAvatarMap(workspaceId);
  const agents = useMemo(
    () =>
      applyPendingMoves(
        (graph.data?.agents ?? []).map((agent) => ({
          ...agent,
          avatar_url: avatars[agent.id]?.url ?? agent.avatar_url ?? null,
          avatar_shape: avatars[agent.id]?.shape ?? agent.avatar_shape ?? null,
          avatar_color: avatars[agent.id]?.color ?? agent.avatar_color ?? null,
        })),
        pendingMoves,
      ),
    [graph.data, avatars, pendingMoves],
  );
  const teams = useMemo(() => graph.data?.teams ?? [], [graph.data]);
  const moveGraph = useMemo<MoveGraph>(() => ({ teams, agents }), [teams, agents]);
  const tree = useMemo(
    () => (graph.data ? buildOrgTree({ teams, agents }) : null),
    [graph.data, teams, agents],
  );
  const agentById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);
  const managerName = (id: string | null) => (id ? agentById.get(id)?.name : undefined);
  const managerNameFor = (agent: OrgAgentNode) => managerName(agent.manager_agent_id);
  const openAgent = (agent: OrgAgentNode) => router.push(`/agents/${agent.id}`);

  const clearPending = useCallback((agentId: string, seq: number) => {
    setPendingMoves((prev) => {
      // A newer move for the same agent owns the entry now; leave it alone.
      if (prev[agentId]?.seq !== seq) return prev;
      const next = { ...prev };
      delete next[agentId];
      return next;
    });
  }, []);

  const handleMove = useCallback(
    (agentId: string, target: MoveTarget) => {
      const check = canMove(agentId, target, moveGraph);
      if (!check.ok) {
        setPageError(`${check.reason} Nothing was changed.`);
        setMoveNote(null);
        return;
      }
      const move = resolveMove(agentId, target, moveGraph);
      if (!move) {
        setPageError(null);
        setMoveNote("Nothing changed — they were already there.");
        return;
      }
      const summary = describeMove(agentId, move, moveGraph);
      const seq = nextMoveSeq();
      setPageError(null);
      setMoveNote(summary);
      setPendingMoves((prev) => ({ ...prev, [agentId]: { ...move, seq } }));
      // Deliberately not a shared useMutation: react-query drops a call's
      // own callbacks when a later mutate() supersedes it, which would strand
      // the first of two rapid moves in its optimistic state forever.
      api<unknown>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}`, {
        method: "PATCH",
        // Both fields go out explicitly: the API clears on an explicit null
        // and leaves omitted fields (including secondary teams) untouched.
        body: { team_id: move.team_id, manager_agent_id: move.manager_agent_id },
      }).then(
        () => {
          // Write the confirmed shape into the cache before dropping the
          // optimistic entry, so the chart never flickers back while the
          // refetch is in flight.
          queryClient.setQueryData(["org-graph", workspaceId], (old: OrgGraph | undefined) =>
            old
              ? {
                  ...old,
                  agents: old.agents.map((agent) =>
                    agent.id === agentId
                      ? { ...agent, team_id: move.team_id, manager_agent_id: move.manager_agent_id }
                      : agent,
                  ),
                }
              : old,
          );
          clearPending(agentId, seq);
          invalidate();
        },
        (error: unknown) => {
          clearPending(agentId, seq);
          // 409 carries the server's own words ("This manager would create a
          // cycle in the reporting chain"), which are already plain English.
          const detail = error instanceof ApiError ? error.detail : "The move didn’t save";
          setMoveNote(null);
          setPageError(`${detail}. The chart has been put back the way it was.`);
        },
      );
    },
    [clearPending, invalidate, moveGraph, queryClient, workspaceId],
  );

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
      <ButtonLink href="/agents/new" variant="primary">
        <Plus size={14} /> New agent
      </ButtonLink>
    </>
  ) : null;

  const outlineMove = useMemo(
    () => (isAdmin ? menuOnlyMoveApi(moveGraph, handleMove) : undefined),
    [isAdmin, moveGraph, handleMove],
  );

  const renderOutline = (move?: OrgMoveApi) => (
    <ul className="grid gap-4 lg:grid-cols-2" aria-label="Teams">
      {tree!.roots.map((node) => (
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
          move={move}
        />
      ))}
      {tree!.unassigned.length > 0 ? (
        <li className="rounded-2xl border border-dashed border-line-strong bg-surface/60 p-4">
          <h3 className="font-display text-base font-semibold tracking-tight">Independent</h3>
          <p className="text-xs text-dim">
            {tree!.unassigned.length} {tree!.unassigned.length === 1 ? "agent" : "agents"} not on a team
          </p>
          <ul className="mt-3 space-y-0.5" aria-label="Independent agents">
            {tree!.unassigned.map((node) => (
              <OutlineAgent key={node.agent.id} node={node} working={working} move={move} />
            ))}
          </ul>
        </li>
      ) : null}
    </ul>
  );

  const renderMap = (move?: OrgMoveApi) => {
    // Admins always see the Independent column, even when it is empty: it is
    // the drop target for taking someone off every team.
    const independent =
      tree!.unassigned.length > 0 || move ? (
        <section className="w-[22rem] shrink-0 rounded-xl border border-dashed border-line-strong bg-surface/50 px-5 py-4">
          <h3 className="mb-3 text-sm font-semibold text-dim">Independent</h3>
          <div className="space-y-2">
            {tree!.unassigned.length === 0 ? (
              <p className="rounded-xl border border-dashed border-line-strong px-3 py-3 text-center text-sm text-faint">
                Everyone is on a team. Drop an agent here to take them off theirs.
              </p>
            ) : null}
            {tree!.unassigned.map((node) => (
              <AgentCard
                key={node.agent.id}
                node={node}
                managerName={managerNameFor(node.agent)}
                onOpen={openAgent}
                move={move}
              />
            ))}
          </div>
        </section>
      ) : null;
    return (
      <div className="overflow-x-auto rounded-2xl border border-line bg-raised/40 p-4">
        <div className="flex min-w-max items-start gap-4">
          {tree!.roots.map((node) => (
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
                onDeleteTeam={setDeletingTeam}
                onAddAgent={(teamId) => router.push(`/agents/new?team=${teamId}`)}
                managerNameFor={managerNameFor}
                move={move}
              />
            </div>
          ))}
          {independent && move ? (
            <div className="w-[22rem] shrink-0">{move.groupWrapper(null, "Independent", independent)}</div>
          ) : (
            independent
          )}
        </div>
      </div>
    );
  };

  return (
    <>
      <PageHeader title="Company" description="Teams, who leads them, and who reports to whom" actions={actions} />
      <PageBody className="space-y-5">
        <ErrorNote message={pageError} />
        {graph.isPending ? (
          <Spinner label="Loading your company…" />
        ) : graph.isError || !graph.data || !tree ? (
          <LoadError what="the company structure" onRetry={() => void graph.refetch()} />
        ) : agents.length === 0 && teams.length === 0 ? (
          <EmptyState
            title="Build your company"
            description="Create teams like Engineering or Marketing, then add agents with roles and reporting lines."
            action={isAdmin ? <div className="flex gap-2">{actions}</div> : undefined}
          />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <StatTile label="Agents" value={agents.length} />
              <StatTile label="Teams" value={teams.length} />
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

            {isAdmin ? (
              <p className="text-[13px] text-dim">
                {view === "map"
                  ? "Drag any agent card onto a team to move them, or onto another agent to make that agent their manager. Every card also has a Move… menu, and the grip on the left picks a card up with the keyboard."
                  : "Use the Move… button on an agent to put them on a team or under a manager."}
              </p>
            ) : null}
            <p role="status" aria-live="polite" className="min-h-[1.25rem] text-[13px] text-accent-strong">
              {moveNote}
            </p>

            {view === "outline" ? (
              renderOutline(outlineMove)
            ) : isAdmin ? (
              <OrgDnd graph={moveGraph} onMove={handleMove}>
                {(api) => renderMap(api)}
              </OrgDnd>
            ) : (
              renderMap()
            )}
          </>
        )}
      </PageBody>

      {graph.data ? (
        <TeamDialog
          open={teamDialogOpen}
          onClose={() => setTeamDialogOpen(false)}
          editing={editingTeam}
          teams={teams}
          agents={agents}
        />
      ) : null}
      <ConfirmDialog
        open={deletingTeam !== null}
        title={`Delete team “${deletingTeam?.name ?? ""}”?`}
        body="Its agents and sub-teams are kept, just detached."
        confirmLabel="Delete team"
        busy={deleteTeam.isPending}
        onConfirm={() => {
          if (deletingTeam) deleteTeam.mutate(deletingTeam.id);
        }}
        onClose={() => setDeletingTeam(null)}
      />
    </>
  );
}
