"use client";

/** Organization view (plan 17.4): hierarchical team/agent chart with CRUD.
 * Cycle validation is server-side; 409 errors surface inline in dialogs and
 * the drawer. */

import { useMutation } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { AgentDrawer } from "@/components/org/agent-drawer";
import { TeamDialog } from "@/components/org/team-dialog";
import { AgentCard, TeamCard } from "@/components/org/tree";
import { Button, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useInvalidateOrg, useOrgGraph } from "@/lib/hooks";
import { buildOrgTree } from "@/lib/org-tree";
import type { OrgAgentNode, OrgTeamNode } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export default function OrganizationPage() {
  const router = useRouter();
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const graph = useOrgGraph(workspaceId);
  const invalidate = useInvalidateOrg(workspaceId);

  const [teamDialogOpen, setTeamDialogOpen] = useState(false);
  const [editingTeam, setEditingTeam] = useState<OrgTeamNode | null>(null);
  const [openAgentId, setOpenAgentId] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);

  const deleteTeam = useMutation({
    mutationFn: (teamId: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/teams/${teamId}`, { method: "DELETE" }),
    onSuccess: () => {
      setPageError(null);
      invalidate();
    },
    onError: (error) =>
      setPageError(error instanceof ApiError ? error.detail : "Delete failed"),
  });

  if (graph.isPending) {
    return (
      <>
        <PageHeader title="Organization" />
        <PageBody wide>
          <Spinner label="Loading organization…" />
        </PageBody>
      </>
    );
  }
  if (graph.isError || !graph.data) {
    return (
      <>
        <PageHeader title="Organization" />
        <PageBody wide>
          <ErrorNote message="Could not load the organization graph." />
        </PageBody>
      </>
    );
  }

  const tree = buildOrgTree(graph.data);
  const agentById = new Map(graph.data.agents.map((agent) => [agent.id, agent]));
  const managerNameFor = (agent: OrgAgentNode) =>
    agent.manager_agent_id ? agentById.get(agent.manager_agent_id)?.name : undefined;
  const isAdmin = can("admin");
  const isEmpty = graph.data.teams.length === 0 && graph.data.agents.length === 0;

  return (
    <>
      <PageHeader
        title="Organization"
        description="Your teams, the agents on them, and who reports to whom."
        actions={
          isAdmin ? (
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
          ) : null
        }
      />
      <PageBody wide className="space-y-4">
        <ErrorNote message={pageError} />
        {isEmpty ? (
          <EmptyState
            title="Design your organization"
            description="Create teams like Engineering or Marketing, then add agents with roles and reporting lines — a CTO managing engineers, a director managing writers."
            action={
              isAdmin ? (
                <div className="flex gap-2">
                  <Button
                    onClick={() => {
                      setEditingTeam(null);
                      setTeamDialogOpen(true);
                    }}
                  >
                    <Plus size={14} /> Create first team
                  </Button>
                  <Link href="/agents/new">
                    <Button variant="primary">
                      <Plus size={14} /> Create an agent
                    </Button>
                  </Link>
                </div>
              ) : undefined
            }
          />
        ) : (
          <div className="grid gap-4 xl:grid-cols-2">
            {tree.roots.map((node) => (
              <TeamCard
                key={node.team.id}
                node={node}
                depth={0}
                isAdmin={isAdmin}
                onOpenAgent={(agent) => setOpenAgentId(agent.id)}
                onEditTeam={(team) => {
                  setEditingTeam(team);
                  setTeamDialogOpen(true);
                }}
                onDeleteTeam={(team) => {
                  if (
                    window.confirm(
                      `Delete team “${team.name}”? Its agents and sub-teams are detached, not deleted.`,
                    )
                  ) {
                    deleteTeam.mutate(team.id);
                  }
                }}
                onAddAgent={(teamId) => router.push(`/agents/new?team=${teamId}`)}
                managerNameFor={managerNameFor}
              />
            ))}
            {tree.unassigned.length > 0 ? (
              <section className="rounded-2xl border border-dashed border-line-strong bg-surface/60 px-5 py-4">
                <h3 className="mb-3 font-display text-sm font-semibold text-dim">Unassigned agents</h3>
                <div className="space-y-2">
                  {tree.unassigned.map((agentNode) => (
                    <AgentCard
                      key={agentNode.agent.id}
                      node={agentNode}
                      managerName={managerNameFor(agentNode.agent)}
                      onOpen={(agent) => setOpenAgentId(agent.id)}
                    />
                  ))}
                </div>
              </section>
            ) : null}
          </div>
        )}
      </PageBody>

      <TeamDialog
        open={teamDialogOpen}
        onClose={() => setTeamDialogOpen(false)}
        editing={editingTeam}
        teams={graph.data.teams}
        agents={graph.data.agents}
      />
      {openAgentId ? (
        <AgentDrawer
          key={openAgentId}
          agentId={openAgentId}
          onClose={() => setOpenAgentId(null)}
          teams={graph.data.teams}
          agents={graph.data.agents}
        />
      ) : null}
    </>
  );
}
