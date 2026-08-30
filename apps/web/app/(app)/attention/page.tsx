"use client";

/** Attention inbox: everything that is waiting on a human — approvals,
 * reviews, help requests between agents, memories to approve, failures,
 * and chats waiting for an answer. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { AttentionInbox, type WorkRequestAction } from "@/components/company/attention-inbox";
import { LoadError } from "@/components/company/bits";
import { ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import {
  useAgentAvatarMap,
  useAttention,
  useInvalidateApprovals,
  useInvalidateCoordination,
  useInvalidateMemories,
  useMemories,
  useWorkRequests,
} from "@/lib/hooks";
import { memoryErrorMessage } from "@/lib/memory";
import type { AcknowledgeFailuresResult, Attention, ReviewVerdict } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export default function AttentionPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const attention = useAttention(workspaceId);
  const workRequests = useWorkRequests(workspaceId, { status: "pending", limit: 50 });
  const proposed = useMemories(workspaceId, { status: "proposed", limit: 50 }, isAdmin);
  const avatars = useAgentAvatarMap(workspaceId);
  const invalidateApprovals = useInvalidateApprovals(workspaceId);
  const invalidateCoordination = useInvalidateCoordination(workspaceId);
  const invalidateMemories = useInvalidateMemories(workspaceId);
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const failed = (err: unknown, what: string) =>
    setError(`${err instanceof ApiError ? err.detail : what}. It's still waiting — try again.`);

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      api(`/api/v1/workspaces/${workspaceId}/approvals/${id}/${decision}`, { method: "POST" }),
    onMutate: ({ id }) => setBusyId(id),
    onSettled: () => setBusyId(null),
    onSuccess: () => {
      setError(null);
      invalidateApprovals();
      void queryClient.invalidateQueries({ queryKey: ["attention", workspaceId] });
    },
    onError: (err) => failed(err, "Saving your decision failed"),
  });

  const decideReview = useMutation({
    mutationFn: ({ id, verdict, feedback }: { id: string; verdict: ReviewVerdict; feedback: string }) =>
      api(`/api/v1/workspaces/${workspaceId}/reviews/${id}/decide`, { method: "POST", body: { verdict, feedback } }),
    onMutate: ({ id }) => setBusyId(id),
    onSettled: () => setBusyId(null),
    onSuccess: () => {
      setError(null);
      invalidateCoordination();
    },
    onError: (err) => failed(err, "Saving your review failed"),
  });

  const answerRequest = useMutation({
    mutationFn: ({ id, action, response }: { id: string; action: WorkRequestAction; response: string }) =>
      api(`/api/v1/workspaces/${workspaceId}/work-requests/${id}/${action}`, { method: "POST", body: { response } }),
    onMutate: ({ id }) => setBusyId(id),
    onSettled: () => setBusyId(null),
    onSuccess: () => {
      setError(null);
      invalidateCoordination();
    },
    onError: (err) => failed(err, "Answering the request failed"),
  });

  const decideMemory = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      api(`/api/v1/workspaces/${workspaceId}/memories/${id}/${decision}`, { method: "POST" }),
    onMutate: ({ id }) => setBusyId(id),
    onSettled: () => setBusyId(null),
    onSuccess: () => {
      setError(null);
      invalidateMemories();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? memoryErrorMessage(err.status, err.detail) : "Saving your decision failed. Try again."),
  });

  const attentionKey = ["attention", workspaceId] as const;
  /** Optimistically drop failures from the cached inbox so the card and the
   * badge counts update before the server answers. */
  const removeFailures = (ids: string[] | "all") =>
    queryClient.setQueryData<Attention>(attentionKey, (current) => {
      if (!current) return current;
      const kept = ids === "all" ? [] : current.failed_tasks.filter((task) => !ids.includes(task.id));
      const removed = current.failed_tasks.length - kept.length;
      return {
        ...current,
        failed_tasks: kept,
        counts: {
          ...current.counts,
          failures: Math.max(0, current.counts.failures - removed),
          total: Math.max(0, current.counts.total - removed),
        },
      };
    });

  const dismissFailure = useMutation({
    mutationFn: ({ id }: { id: string }) =>
      api(`/api/v1/workspaces/${workspaceId}/tasks/${id}/acknowledge`, { method: "POST" }),
    onMutate: async ({ id }) => {
      setBusyId(id);
      await queryClient.cancelQueries({ queryKey: attentionKey });
      const previous = queryClient.getQueryData<Attention>(attentionKey);
      removeFailures([id]);
      return { previous };
    },
    onError: (err, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(attentionKey, context.previous);
      failed(err, "Dismissing the problem failed");
    },
    onSuccess: () => setError(null),
    onSettled: () => {
      setBusyId(null);
      void queryClient.invalidateQueries({ queryKey: attentionKey });
    },
  });

  const dismissAllFailures = useMutation({
    mutationFn: () =>
      api<AcknowledgeFailuresResult>(`/api/v1/workspaces/${workspaceId}/attention/acknowledge-failures`, { method: "POST" }),
    onMutate: async () => {
      setBusyId("failures:all");
      await queryClient.cancelQueries({ queryKey: attentionKey });
      const previous = queryClient.getQueryData<Attention>(attentionKey);
      removeFailures("all");
      return { previous };
    },
    onError: (err, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(attentionKey, context.previous);
      failed(err, "Dismissing the problems failed");
    },
    onSuccess: () => setError(null),
    onSettled: () => {
      setBusyId(null);
      void queryClient.invalidateQueries({ queryKey: attentionKey });
    },
  });

  const pendingRequests = workRequests.data?.items ?? [];
  const proposedMemories = proposed.data?.items ?? [];
  const total = (attention.data?.counts.total ?? 0) + pendingRequests.length + proposedMemories.length;

  return (
    <>
      <PageHeader
        title="Attention"
        description={total > 0 ? `${total} ${total === 1 ? "thing needs" : "things need"} you` : "Things that need a human"}
      />
      <PageBody narrow className="space-y-4">
        <ErrorNote message={error} />
        {/* The main inbox never waits on, or is replaced because of, the two
            secondary feeds: they poll, most users have zero items in them, and
            one failed poll must not blank a loaded page of approvals. They get
            inline notices below instead. */}
        {attention.isPending ? (
          <Spinner label="Checking what needs you…" />
        ) : attention.isError || !attention.data ? (
          <LoadError what="your inbox" onRetry={() => void attention.refetch()} />
        ) : (
          <>
          {workRequests.isError && !workRequests.data ? (
            <LoadError what="help requests" onRetry={() => void workRequests.refetch()} />
          ) : null}
          {isAdmin && proposed.isError && !proposed.data ? (
            <LoadError what="memories waiting for approval" onRetry={() => void proposed.refetch()} />
          ) : null}
          <AttentionInbox
            data={attention.data}
            canDecide={can("member")}
            isAdmin={isAdmin}
            decidingId={busyId}
            avatars={avatars}
            onDecide={(id, decision) => decide.mutate({ id, decision })}
            onReviewDecide={(id, verdict, feedback) => decideReview.mutateAsync({ id, verdict, feedback })}
            onDismissFailure={(id) => dismissFailure.mutate({ id })}
            onDismissAllFailures={() => dismissAllFailures.mutate()}
            workRequests={pendingRequests}
            onWorkRequest={(id, action, response) => answerRequest.mutateAsync({ id, action, response })}
            proposedMemories={proposedMemories}
            onMemoryDecide={(id, decision) => decideMemory.mutate({ id, decision })}
          />
          </>
        )}
      </PageBody>
    </>
  );
}
