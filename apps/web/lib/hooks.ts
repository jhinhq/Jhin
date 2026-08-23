"use client";

/** Shared data hooks (TanStack Query) for the authenticated app. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import type {
  ActivityList,
  Agent,
  AgentAvatar,
  AgentPolicy,
  Attention,
  AvatarGenerationOut,
  DirectoryEntry,
  ManagerRollup,
  MemoryList,
  ReviewPolicy,
  WorkRequestList,
  ConversationDetail,
  ConversationList,
  ConversationMessage,
  ApprovalList,
  AuditEventPage,
  BootstrapStatus,
  CatalogApp,
  ConnectionInfo,
  ConnectionAccessSummaryOut,
  ConnectionToolsOut,
  ConnectorInfo,
  Grant,
  Member,
  MeResponse,
  ModelProfile,
  ModelProvider,
  ProviderBalance,
  ProviderModels,
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
  WorkspaceSpend,
} from "@/lib/types";

/** Poll cadence for live task/run views. */
const LIVE_POLL_MS = 2000;

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

/** Model identifiers a provider exposes; empty with a detail when it cannot list. */
export function useProviderModels(workspaceId: string, providerId: string | null) {
  return useQuery({
    queryKey: ["provider-models", workspaceId, providerId],
    queryFn: () =>
      api<ProviderModels>(
        `/api/v1/workspaces/${workspaceId}/model-providers/${providerId}/models`,
      ),
    enabled: providerId !== null && providerId !== "",
    staleTime: 60_000,
    retry: false,
  });
}

/** Balance/spend for one provider; polls every minute (the API caches the
 * provider's billing call for the same interval). */
export function useProviderBalance(workspaceId: string, providerId: string, enabled = true) {
  return useQuery({
    queryKey: ["provider-balance", workspaceId, providerId],
    queryFn: () =>
      api<ProviderBalance>(
        `/api/v1/workspaces/${workspaceId}/model-providers/${providerId}/balance`,
      ),
    enabled,
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: false,
  });
}

export function useWorkspaceSpend(workspaceId: string) {
  return useQuery({
    queryKey: ["workspace-spend", workspaceId],
    queryFn: () => api<WorkspaceSpend>(`/api/v1/workspaces/${workspaceId}/spend`),
    staleTime: 30_000,
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
    void queryClient.invalidateQueries({ queryKey: ["provider-balance", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["workspace-spend", workspaceId] });
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
    void queryClient.invalidateQueries({ queryKey: ["connection-tools", workspaceId] });
    // Discovered MCP tools are part of the workspace tool catalog.
    void queryClient.invalidateQueries({ queryKey: ["tools", workspaceId] });
  };
}

/** The curated Apps library (static public data; docs/architecture/mcp.md). */
export function useAppCatalog() {
  return useQuery({
    queryKey: ["app-catalog"],
    queryFn: () => api<CatalogApp[]>("/api/v1/connectors/catalog"),
    staleTime: 60_000,
  });
}

/** Tools reachable through one connection with their enforced risk (admin). */
export function useConnectionTools(workspaceId: string, connectionId: string | null) {
  return useQuery({
    queryKey: ["connection-tools", workspaceId, connectionId],
    queryFn: () =>
      api<ConnectionToolsOut>(
        `/api/v1/workspaces/${workspaceId}/connections/${connectionId}/tools`,
      ),
    enabled: connectionId !== null,
    retry: false,
  });
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

// --- Conversations, activity, attention (docs/architecture/conversations.md) ---

export function useConversations(
  workspaceId: string,
  params: Record<string, string | number | undefined> = {},
) {
  return useQuery({
    queryKey: ["conversations", workspaceId, params],
    queryFn: () =>
      api<ConversationList>(`/api/v1/workspaces/${workspaceId}/conversations`, { params }),
    placeholderData: (previous) => previous,
    refetchInterval: 5_000,
  });
}

export function useConversation(workspaceId: string, conversationId: string | null, live = true) {
  return useQuery({
    queryKey: ["conversation", workspaceId, conversationId],
    queryFn: () =>
      api<ConversationDetail>(
        `/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`,
      ),
    enabled: conversationId !== null,
    refetchInterval: live ? LIVE_POLL_MS : false,
  });
}

export function useConversationMessages(
  workspaceId: string,
  conversationId: string | null,
  live = true,
) {
  return useQuery({
    queryKey: ["conversation-messages", workspaceId, conversationId],
    queryFn: () =>
      api<ConversationMessage[]>(
        `/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/messages`,
      ),
    enabled: conversationId !== null,
    refetchInterval: live ? LIVE_POLL_MS : false,
  });
}

export function useConversationActivity(
  workspaceId: string,
  conversationId: string | null,
  live = true,
) {
  return useQuery({
    queryKey: ["conversation-activity", workspaceId, conversationId],
    queryFn: () =>
      api<ActivityList>(
        `/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/activity`,
      ),
    enabled: conversationId !== null,
    refetchInterval: live ? LIVE_POLL_MS : false,
  });
}

export function useActivity(
  workspaceId: string,
  params: Record<string, string | number | undefined> = {},
) {
  return useQuery({
    queryKey: ["activity", workspaceId, params],
    queryFn: () => api<ActivityList>(`/api/v1/workspaces/${workspaceId}/activity`, { params }),
    placeholderData: (previous) => previous,
    refetchInterval: 5_000,
  });
}

export function useAttention(workspaceId: string) {
  return useQuery({
    queryKey: ["attention", workspaceId],
    queryFn: () => api<Attention>(`/api/v1/workspaces/${workspaceId}/attention`),
    refetchInterval: 10_000,
  });
}

export function useInvalidateConversations(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["conversations", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["conversation", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["conversation-messages", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["conversation-activity", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["activity", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["attention", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["tasks", workspaceId] });
  };
}

// --- Memory (docs/architecture/memory.md) ---

export type MemoryQuery = Record<string, string | number | undefined>;

export function useMemories(workspaceId: string, params: MemoryQuery = {}, enabled = true) {
  return useQuery({
    queryKey: ["memories", workspaceId, params],
    queryFn: () => api<MemoryList>(`/api/v1/workspaces/${workspaceId}/memories`, { params }),
    placeholderData: (previous) => previous,
    enabled,
  });
}

export function useInvalidateMemories(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["memories", workspaceId] });
  };
}

// --- Avatars (docs/architecture/media.md) ---

/** Latest generation for an agent; polls while queued/running. 404 (never
 * generated) is treated as "nothing" rather than an error. */
export function useAvatarGeneration(workspaceId: string, agentId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["avatar-generation", workspaceId, agentId],
    queryFn: async () => {
      try {
        return await api<AvatarGenerationOut>(
          `/api/v1/workspaces/${workspaceId}/agents/${agentId}/avatar/generation`,
        );
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    enabled: enabled && agentId !== null,
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? LIVE_POLL_MS : false;
    },
    // A generation takes seconds and people switch tabs while they wait;
    // keep polling in the background so the picture lands without a refocus.
    refetchIntervalInBackground: true,
  });
}

/** Invalidate everything that shows an agent's picture. */
export function useInvalidateAvatar(workspaceId: string, agentId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["agent-avatar", workspaceId, agentId] });
    void queryClient.invalidateQueries({ queryKey: ["avatar-generation", workspaceId, agentId] });
    void queryClient.invalidateQueries({ queryKey: ["agent", workspaceId, agentId] });
    void queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["org-graph", workspaceId] });
  };
}

/** Agent id → avatar visuals (image url, shape, color) for screens whose
 * payloads only carry agent names (conversations, messages, activity cards).
 * Backed by the cached agent list; spread into `Avatar` via `avatarProps`. */
export function useAgentAvatarMap(workspaceId: string): Record<string, AgentAvatar> {
  const agents = useAgents(workspaceId);
  const map: Record<string, AgentAvatar> = {};
  for (const agent of agents.data ?? []) {
    map[agent.id] = {
      url: agent.avatar_url ?? null,
      shape: agent.avatar_shape ?? null,
      color: agent.avatar_color ?? null,
    };
  }
  return map;
}

// --- Coordination (docs/architecture/coordination.md) ---

export function useDirectory(
  workspaceId: string,
  params: Record<string, string | number | undefined> = {},
  enabled = true,
) {
  return useQuery({
    queryKey: ["directory", workspaceId, params],
    queryFn: async () => {
      // The API pages the directory as `{items, has_more}`; callers only
      // need the entries.
      const page = await api<{ items: DirectoryEntry[]; has_more: boolean }>(
        `/api/v1/workspaces/${workspaceId}/directory`,
        { params },
      );
      return page.items;
    },
    placeholderData: (previous) => previous,
    enabled,
  });
}

export function useWorkRequests(
  workspaceId: string,
  params: Record<string, string | number | undefined> = {},
  enabled = true,
) {
  return useQuery({
    queryKey: ["work-requests", workspaceId, params],
    queryFn: () =>
      api<WorkRequestList>(`/api/v1/workspaces/${workspaceId}/work-requests`, { params }),
    placeholderData: (previous) => previous,
    refetchInterval: 10_000,
    enabled,
  });
}

export function useReviewPolicies(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["review-policies", workspaceId],
    queryFn: () => api<ReviewPolicy[]>(`/api/v1/workspaces/${workspaceId}/review-policies`),
    enabled,
  });
}

export function useAgentRollup(workspaceId: string, agentId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["agent-rollup", workspaceId, agentId],
    queryFn: () =>
      api<ManagerRollup>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/rollup`),
    enabled: enabled && agentId !== null,
    refetchInterval: 10_000,
  });
}

/** After deciding a review or answering a help request. */
export function useInvalidateCoordination(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["work-requests", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["reviews", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["review-policies", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["agent-rollup", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["attention", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["activity", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["tasks", workspaceId] });
  };
}
