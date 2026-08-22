"use client";

/** Approvals inbox (plan 17.11): pending-first cards with the requesting
 * agent, action, risk badge, reason, sanitized payload, and a task link. */

import { useMutation } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { ApprovalCard } from "@/components/approval-card";
import { Badge, EmptyState, ErrorNote, Spinner, Tabs } from "@/components/ui";
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
        description="Risky actions that need a person to say yes before an agent goes ahead"
        actions={
          pendingCount > 0 ? <Badge tone="warn">{pendingCount} pending</Badge> : undefined
        }
      />
      <PageBody className="space-y-4">
        <Tabs
          label="Approval filter"
          tabs={FILTERS.map((entry) => ({ id: entry.id, label: entry.label }))}
          value={filter}
          onChange={(id) => setFilter(id as (typeof FILTERS)[number]["id"])}
        />

        <ErrorNote message={error} />

        {approvals.isPending ? (
          <Spinner label="Loading approvals…" />
        ) : items.length === 0 ? (
          <EmptyState
            icon={<Inbox size={20} aria-hidden />}
            title={filter === "pending" ? "No pending approvals" : "No approvals yet"}
            description="When an agent's tool call needs sign-off, it appears here and the run waits until someone decides."
          />
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
      </PageBody>
    </>
  );
}
