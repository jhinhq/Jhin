"use client";

/** Approvals inbox (plan 17.11): pending-first cards with the requesting
 * agent, action, risk badge, reason, sanitized payload, and a task link. */

import { useMutation } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { useState } from "react";
import { PageHeader } from "@/components/app-shell";
import { ApprovalCard } from "@/components/approval-card";
import { Badge, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useApprovals, useInvalidateApprovals } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

const FILTERS = [
  { id: "pending", label: "Pending" },
  { id: "all", label: "All" },
] as const;

export default function ApprovalsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["id"]>("pending");
  const [error, setError] = useState<string | null>(null);

  const approvals = useApprovals(workspaceId, {
    status: filter === "pending" ? "pending" : undefined,
    limit: 50,
  });
  const invalidate = useInvalidateApprovals(workspaceId);

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      api(`/api/v1/workspaces/${workspaceId}/approvals/${id}/${decision}`, { method: "POST" }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.detail : "Submitting the decision failed."),
  });

  const items = approvals.data?.items ?? [];
  const pendingCount = approvals.data?.pending_count ?? 0;

  return (
    <>
      <PageHeader
        title="Approvals"
        description="High-risk agent actions awaiting human sign-off"
        actions={
          pendingCount > 0 ? <Badge tone="warn">{pendingCount} pending</Badge> : undefined
        }
      />
      <div className="space-y-4 px-8 py-6">
        <nav className="flex gap-1">
          {FILTERS.map((entry) => (
            <button
              key={entry.id}
              onClick={() => setFilter(entry.id)}
              className={`rounded-md px-3 py-1.5 text-[13px] transition-colors ${
                filter === entry.id
                  ? "bg-accent-soft font-medium text-accent-strong"
                  : "text-dim hover:bg-hover hover:text-ink"
              }`}
            >
              {entry.label}
            </button>
          ))}
        </nav>

        <ErrorNote message={error} />

        {approvals.isPending ? (
          <Spinner label="Loading approvals…" />
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-line-strong px-6 py-16 text-center">
            <Inbox size={20} className="text-faint" />
            <p className="text-sm font-medium">
              {filter === "pending" ? "No pending approvals" : "No approvals yet"}
            </p>
            <p className="max-w-sm text-sm text-dim">
              When an agent&apos;s tool call needs sign-off, it appears here and the run waits
              durably until someone decides.
            </p>
          </div>
        ) : (
          <ul className="space-y-3">
            {items.map((approval) => (
              <ApprovalCard
                key={approval.id}
                approval={approval}
                canDecide={can("member")}
                deciding={decide.isPending}
                onApprove={() => decide.mutate({ id: approval.id, decision: "approve" })}
                onReject={() => decide.mutate({ id: approval.id, decision: "reject" })}
              />
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
