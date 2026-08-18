"use client";

/** Shared data hooks (TanStack Query) for the authenticated app. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  Agent,
  AgentPolicy,
  ApprovalList,
  AuditEventPage,
  BootstrapStatus,
  ConnectionInfo,
  ConnectionAccessSummaryOut,
  ConnectorInfo,
  Grant,
  Member,
  MeResponse,
  ModelProfile,
  ModelProvider,
  OrgGraph,
  RunEvent,
  RunList,
  SecretOut,
  TaskDetail,
  TaskList,
  TaskMessage,
  TaskTree,
  Team,
  ToolCallRecord,
  ToolInfo,
  Trigger,
  TriggerInvocation,
  WorkspaceDetail,
} from "@/lib/types";

/** Poll cadence for live task/run views until SSE lands (plan 18). */
export const LIVE_POLL_MS = 2000;

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => api<MeResponse>("/api/v1/auth/me"),
  });
}

export function useBootstrapStatus() {
  return useQuery({
    queryKey: ["bootstrap-status"],
    queryFn: () => api<BootstrapStatus>("/api/v1/auth/bootstrap-status"),
    staleTime: 0,
  });
}

export function useOrgGraph(workspaceId: string) {
  return useQuery({
    queryKey: ["org-graph", workspaceId],
    queryFn: () => api<OrgGraph>(`/api/v1/workspaces/${workspaceId}/org-graph`),
  });
}

export function useTeams(workspaceId: string) {
  return useQuery({
    queryKey: ["teams", workspaceId],
    queryFn: () => api<Team[]>(`/api/v1/workspaces/${workspaceId}/teams`),
  });
}

export function useAgents(workspaceId: string) {
  return useQuery({
    queryKey: ["agents", workspaceId],
    queryFn: () => api<Agent[]>(`/api/v1/workspaces/${workspaceId}/agents`),
  });
}

export function useAgent(workspaceId: string, agentId: string | null) {
  return useQuery({
    queryKey: ["agent", workspaceId, agentId],
    queryFn: () => api<Agent>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}`),
    enabled: agentId !== null,
  });
}

export function useMembers(workspaceId: string) {
  return useQuery({
    queryKey: ["members", workspaceId],
    queryFn: () => api<Member[]>(`/api/v1/workspaces/${workspaceId}/members`),
  });
}

export function useAuditEvents(
  workspaceId: string,
  params: Record<string, string | number | undefined>,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["audit", workspaceId, params],
    queryFn: () =>
      api<AuditEventPage>(`/api/v1/workspaces/${workspaceId}/audit-events`, { params }),
    enabled,
    placeholderData: (previous) => previous,
  });
}

export function useWorkspaceDetail(workspaceId: string) {
  return useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => api<WorkspaceDetail>(`/api/v1/workspaces/${workspaceId}`),
  });
}

export function useSecrets(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["secrets", workspaceId],
    queryFn: () => api<SecretOut[]>(`/api/v1/workspaces/${workspaceId}/secrets`),
    enabled,
  });
}

export function useModelProviders(workspaceId: string) {
  return useQuery({
    queryKey: ["model-providers", workspaceId],
    queryFn: () => api<ModelProvider[]>(`/api/v1/workspaces/${workspaceId}/model-providers`),
  });
}

export function useModelProfiles(workspaceId: string) {
  return useQuery({
    queryKey: ["model-profiles", workspaceId],
    queryFn: () => api<ModelProfile[]>(`/api/v1/workspaces/${workspaceId}/model-profiles`),
  });
}

export function useTasks(
  workspaceId: string,
  params: Record<string, string | number | undefined> = {},
) {
  return useQuery({
    queryKey: ["tasks", workspaceId, params],
    queryFn: () => api<TaskList>(`/api/v1/workspaces/${workspaceId}/tasks`, { params }),
    placeholderData: (previous) => previous,
    refetchInterval: LIVE_POLL_MS,
  });
}

export function useTask(workspaceId: string, taskId: string, live: boolean) {
  return useQuery({
    queryKey: ["task", workspaceId, taskId],
    queryFn: () => api<TaskDetail>(`/api/v1/workspaces/${workspaceId}/tasks/${taskId}`),
    refetchInterval: live ? LIVE_POLL_MS : false,
  });
}

export function useTaskTimeline(workspaceId: string, taskId: string, live: boolean) {
  return useQuery({
    queryKey: ["task-timeline", workspaceId, taskId],
    queryFn: () => api<RunEvent[]>(`/api/v1/workspaces/${workspaceId}/tasks/${taskId}/timeline`),
    refetchInterval: live ? LIVE_POLL_MS : false,
  });
}

/** Delegation chain around a task (Phase 8). */
export function useTaskTree(workspaceId: string, taskId: string, live: boolean) {
  return useQuery({
    queryKey: ["task-tree", workspaceId, taskId],
    queryFn: () => api<TaskTree>(`/api/v1/workspaces/${workspaceId}/tasks/${taskId}/tree`),
    refetchInterval: live ? LIVE_POLL_MS : false,
  });
}

export function useTaskMessages(workspaceId: string, taskId: string, live: boolean) {
  return useQuery({
    queryKey: ["task-messages", workspaceId, taskId],
    queryFn: () =>
      api<TaskMessage[]>(`/api/v1/workspaces/${workspaceId}/tasks/${taskId}/messages`),
    refetchInterval: live ? LIVE_POLL_MS : false,
  });
}

export function useRuns(
  workspaceId: string,
  params: Record<string, string | number | undefined> = {},
) {
  return useQuery({
    queryKey: ["runs", workspaceId, params],
    queryFn: () => api<RunList>(`/api/v1/workspaces/${workspaceId}/runs`, { params }),
    placeholderData: (previous) => previous,
    refetchInterval: LIVE_POLL_MS,
  });
}

// --- Phase 4: tools, grants, policies, approvals ---

export function useTools(workspaceId: string) {
  return useQuery({
    queryKey: ["tools", workspaceId],
    queryFn: () => api<ToolInfo[]>(`/api/v1/workspaces/${workspaceId}/tools`),
    staleTime: 60_000, // the catalog only changes with deploys
  });
}

export function useAgentGrants(workspaceId: string, agentId: string) {
  return useQuery({
    queryKey: ["agent-grants", workspaceId, agentId],
    queryFn: () =>
      api<Grant[]>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/grants`),
  });
}

export function useAgentPolicy(workspaceId: string, agentId: string) {
  return useQuery({
    queryKey: ["agent-policy", workspaceId, agentId],
    queryFn: () =>
      api<AgentPolicy>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/policy`),
  });
}

export function useApprovals(
  workspaceId: string,
  params: Record<string, string | number | undefined> = {},
) {
  return useQuery({
    queryKey: ["approvals", workspaceId, params],
    queryFn: () =>
      api<ApprovalList>(`/api/v1/workspaces/${workspaceId}/approvals`, { params }),
    placeholderData: (previous) => previous,
    refetchInterval: LIVE_POLL_MS,
  });
}

/** Lightweight pending count for the nav badge and overview card. */
export function usePendingApprovalCount(workspaceId: string) {
  return useQuery({
    queryKey: ["approvals-pending-count", workspaceId],
    queryFn: () =>
      api<ApprovalList>(`/api/v1/workspaces/${workspaceId}/approvals`, {
        params: { status: "pending", limit: 1 },
      }),
    refetchInterval: 10_000,
    select: (data) => data.pending_count,
  });
}

export function useInvalidateApprovals(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["approvals", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["approvals-pending-count", workspaceId] });
  };
}

export function useInvalidateAgentAccess(workspaceId: string, agentId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["agent-grants", workspaceId, agentId] });
    void queryClient.invalidateQueries({ queryKey: ["agent-policy", workspaceId, agentId] });
    void queryClient.invalidateQueries({ queryKey: ["agent", workspaceId, agentId] });
  };
}

/** Invalidate task/run views after a task mutation. */
export function useInvalidateTasks(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["tasks", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["task", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["task-timeline", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["task-messages", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["task-tree", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["runs", workspaceId] });
  };
}

/** Invalidate model configuration views after a mutation. */
export function useInvalidateModels(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["model-providers", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["model-profiles", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["secrets", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
  };
}

// --- Phase 5: connectors and connections ---

export function useConnectors() {
  return useQuery({
    queryKey: ["connectors"],
    queryFn: () => api<ConnectorInfo[]>("/api/v1/connectors"),
    staleTime: 60_000, // static manifests; only change with deploys
  });
}

export function useConnections(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["connections", workspaceId],
    queryFn: () => api<ConnectionInfo[]>(`/api/v1/workspaces/${workspaceId}/connections`),
    enabled,
  });
}

export function useConnectionToolCalls(workspaceId: string, connectionId: string | null) {
  return useQuery({
    queryKey: ["connection-tool-calls", workspaceId, connectionId],
    queryFn: () =>
      api<ToolCallRecord[]>(
        `/api/v1/workspaces/${workspaceId}/connections/${connectionId}/tool-calls`,
      ),
    enabled: connectionId !== null,
  });
}

export function useConnectionAccessSummary(workspaceId: string, connectionId: string | null) {
  return useQuery({
    queryKey: ["connection-access-summary", workspaceId, connectionId],
    queryFn: () =>
      api<ConnectionAccessSummaryOut>(
        `/api/v1/workspaces/${workspaceId}/connections/${connectionId}/access-summary`,
      ),
    enabled: connectionId !== null,
  });
}

export function useInvalidateConnections(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["connections", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["connection-tool-calls", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["connection-access-summary", workspaceId] });
  };
}

/** Apply the write response synchronously so UI state never waits on refetch. */
export function useMarkConnectionWebhookConfigured(workspaceId: string) {
  const queryClient = useQueryClient();
  return (connection: ConnectionInfo) => {
    const configured = { ...connection, webhook_secret_configured: true };
    queryClient.setQueryData<ConnectionInfo[]>(
      ["connections", workspaceId],
      (current) => {
        if (!current) return [configured];
        return current.some((item) => item.id === connection.id)
          ? current.map((item) => item.id === connection.id
            ? { ...item, webhook_secret_configured: true }
            : item)
          : [...current, configured];
      },
    );
  };
}

// --- Phase 7: triggers ---

export function useTriggers(workspaceId: string) {
  return useQuery({
    queryKey: ["triggers", workspaceId],
    queryFn: () => api<Trigger[]>(`/api/v1/workspaces/${workspaceId}/triggers`),
    refetchInterval: 10_000, // keep last-invocation status reasonably live
  });
}

export function useTriggerInvocations(workspaceId: string, triggerId: string | null) {
  return useQuery({
    queryKey: ["trigger-invocations", workspaceId, triggerId],
    queryFn: () =>
      api<TriggerInvocation[]>(
        `/api/v1/workspaces/${workspaceId}/triggers/${triggerId}/invocations`,
      ),
    enabled: triggerId !== null,
    refetchInterval: 10_000,
  });
}

/** Provider metadata for builder pickers (admin-only endpoint). */
export function useConnectionMetadata(
  workspaceId: string,
  connectionId: string | null,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["connection-metadata", workspaceId, connectionId],
    queryFn: () =>
      api<Record<string, unknown>>(
        `/api/v1/workspaces/${workspaceId}/connections/${connectionId}/metadata`,
      ),
    enabled: enabled && connectionId !== null,
    staleTime: 60_000,
    retry: false,
  });
}

export function useInvalidateTriggers(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["triggers", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["trigger-invocations", workspaceId] });
  };
}

/** Invalidate everything that renders org structure after a mutation. */
export function useInvalidateOrg(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["org-graph", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["teams", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["agent", workspaceId] });
  };
}
