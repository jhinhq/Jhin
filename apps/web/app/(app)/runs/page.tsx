"use client";

/** Runs page: every agent run with status, tokens, cost, and its task. */

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { LoadError } from "@/components/company/bits";
import { StateBadge } from "@/components/task-bits";
import { EmptyState, Select, Spinner, focusRing } from "@/components/ui";
import { formatCostMicros, formatDateTime, formatTokens, shortId } from "@/lib/format";
import { useAgents, useRuns } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

const STATUS_FILTERS = ["", "running", "completed", "failed", "cancelled"];

export default function RunsPage() {
  return (
    <Suspense
      fallback={
        <PageBody>
          <Spinner />
        </PageBody>
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
      <PageHeader
        title="Runs"
        description="Each time an agent did some work, with the tokens it used and what it cost"
      />
      <PageBody className="space-y-4">
        <div className="flex items-center gap-3">
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
        ) : runs.isError ? (
          <LoadError what="runs" onRetry={() => void runs.refetch()} />
        ) : (runs.data?.items.length ?? 0) === 0 ? (
          <EmptyState
            title="No runs yet"
            description="Runs appear when agents execute tasks. Assign a task or message an agent to start one."
          />
        ) : (
          <div className="overflow-x-auto rounded-2xl border border-line bg-surface shadow-card">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="text-left text-xs font-medium uppercase tracking-wider text-faint">
                <tr>
                  <th className="px-4 py-3">Run</th>
                  <th className="px-4 py-3">Agent</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Tokens (in · out)</th>
                  <th className="px-4 py-3">Cost</th>
                  <th className="px-4 py-3">Steps</th>
                  <th className="px-4 py-3">Started</th>
                  <th className="px-4 py-3">Task</th>
                </tr>
              </thead>
              <tbody>
                {(runs.data?.items ?? []).map((run) => (
                  <tr key={run.id} className="border-t border-line hover:bg-hover">
                    <td className="px-4 py-3">
                      <code className="font-mono text-xs text-dim">{shortId(run.id)}</code>
                    </td>
                    <td className="px-4 py-3 text-ink">{agentName(run.agent_id)}</td>
                    <td className="px-4 py-3">
                      <StateBadge state={run.status} />
                    </td>
                    <td className="px-4 py-3 tabular-nums text-dim">
                      {formatTokens(run.input_tokens)} · {formatTokens(run.output_tokens)}
                    </td>
                    <td className="px-4 py-3 tabular-nums text-dim">
                      {formatCostMicros(run.estimated_cost_micros)}
                    </td>
                    <td className="px-4 py-3 tabular-nums text-dim">{run.steps_used}</td>
                    <td className="px-4 py-3 text-dim">
                      {run.started_at ? formatDateTime(run.started_at) : "—"}
                    </td>
                    <td className="px-4 py-3">
                      {run.task_id ? (
                        <Link
                          href={`/tasks/${run.task_id}`}
                          className={`rounded-lg font-medium text-accent-strong hover:underline ${focusRing}`}
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
      </PageBody>
    </>
  );
}
