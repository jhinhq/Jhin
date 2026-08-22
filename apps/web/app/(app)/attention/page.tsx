"use client";

/** Attention inbox: everything that is waiting on a human. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PageHeader } from "@/components/app-shell";
import { AttentionInbox } from "@/components/company/attention-inbox";
import { LoadError } from "@/components/company/bits";
import { ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useAttention, useInvalidateApprovals } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

export default function AttentionPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const attention = useAttention(workspaceId);
  const invalidateApprovals = useInvalidateApprovals(workspaceId);
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      api(`/api/v1/workspaces/${workspaceId}/approvals/${id}/${decision}`, { method: "POST" }),
    onSuccess: () => {
      setError(null);
      invalidateApprovals();
      void queryClient.invalidateQueries({ queryKey: ["attention", workspaceId] });
    },
    onError: (err) =>
      setError(
        `${err instanceof ApiError ? err.detail : "Saving your decision failed"}. The request is still pending — try again.`,
      ),
  });

  const total = attention.data?.counts.total ?? 0;

  return (
    <>
      <PageHeader
        title="Attention"
        description={total > 0 ? `${total} ${total === 1 ? "thing needs" : "things need"} you` : "Things that need a human"}
      />
      <div className="mx-auto max-w-3xl space-y-4 px-4 py-5 sm:px-8 sm:py-6">
        <ErrorNote message={error} />
        {attention.isPending ? (
          <Spinner label="Checking what needs you…" />
        ) : attention.isError || !attention.data ? (
          <LoadError what="your inbox" onRetry={() => void attention.refetch()} />
        ) : (
          <AttentionInbox
            data={attention.data}
            canDecide={can("member")}
            decidingId={decide.isPending ? decide.variables?.id : null}
            onDecide={(id, decision) => decide.mutate({ id, decision })}
          />
        )}
      </div>
    </>
  );
}
