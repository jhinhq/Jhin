"use client";

/** Runs page: every agent run with status, tokens, cost, and its task. */

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { PageHeader } from "@/components/app-shell";
import { StateBadge } from "@/components/task-bits";
import { EmptyState, Select, Spinner } from "@/components/ui";
import { formatCostMicros, formatDateTime, formatTokens, shortId } from "@/lib/format";
import { useAgents, useRuns } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

const STATUS_FILTERS = ["", "running", "completed", "failed", "cancelled"];

export default function RunsPage() {
  return (
    <Suspense
      fallback={
        <div className="px-8 py-6">
          <Spinner />
        </div>
      }
    >
      <RunsPageInner />
    </Suspense>
  );
}

function RunsPageInner() {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const agentFilter = useSearchParams().get("agent") ?? undefined;
  const [statusFilter, setStatusFilter] = useState("");

  const runs = useRuns(workspaceId, {
    status: statusFilter || undefined,
    agent_id: agentFilter,
    limit: 100,
  });
  const agents = useAgents(workspaceId);
  const agentName = (id: string) => agents.data?.find((a) => a.id === id)?.name ?? shortId(id);

  return (
    <>
      <PageHeader title="Runs" description="Agent executions with token usage and cost" />
      <div className="space-y-4 px-8 py-6">
        <div className="flex items-center gap-2">
          <Select
            className="w-44"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            aria-label="Filter by status"
          >
            {STATUS_FILTERS.map((status) => (
              <option key={status} value={status}>
                {status || "All statuses"}
              </option>
            ))}
          </Select>
          {runs.data ? <span className="text-xs text-dim">{runs.data.total} total</span> : null}
        </div>

        {runs.isPending ? (
          <Spinner label="Loading runs…" />
        ) : (runs.data?.items.length ?? 0) === 0 ? (
          <EmptyState
            title="No runs yet"
            description="Runs appear when agents execute tasks. Assign a task or message an agent to start one."
          />
        ) : (
          <div className="overflow-hidden rounded-xl border border-line">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line bg-raised text-left text-xs uppercase tracking-wider text-dim">
                  <th className="px-4 py-2.5 font-medium">Run</th>
                  <th className="px-4 py-2.5 font-medium">Agent</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 font-medium">Tokens (in · out)</th>
                  <th className="px-4 py-2.5 font-medium">Cost</th>
                  <th className="px-4 py-2.5 font-medium">Steps</th>
                  <th className="px-4 py-2.5 font-medium">Started</th>
                  <th className="px-4 py-2.5 font-medium">Task</th>
                </tr>
              </thead>
              <tbody>
                {(runs.data?.items ?? []).map((run) => (
                  <tr key={run.id} className="border-b border-line last:border-0 hover:bg-hover/50">
                    <td className="px-4 py-2.5">
                      <code className="text-xs">{shortId(run.id)}</code>
                    </td>
                    <td className="px-4 py-2.5">{agentName(run.agent_id)}</td>
                    <td className="px-4 py-2.5">
                      <StateBadge state={run.status} />
                    </td>
                    <td className="px-4 py-2.5 tabular-nums text-dim">
                      {formatTokens(run.input_tokens)} · {formatTokens(run.output_tokens)}
                    </td>
                    <td className="px-4 py-2.5 tabular-nums text-dim">
                      {formatCostMicros(run.estimated_cost_micros)}
                    </td>
                    <td className="px-4 py-2.5 tabular-nums text-dim">{run.steps_used}</td>
                    <td className="px-4 py-2.5 text-dim">
                      {run.started_at ? formatDateTime(run.started_at) : "—"}
                    </td>
                    <td className="px-4 py-2.5">
                      {run.task_id ? (
                        <Link
                          href={`/tasks/${run.task_id}`}
                          className="text-accent-strong hover:underline"
                        >
                          view task
                        </Link>
                      ) : (
                        <span className="text-faint">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
