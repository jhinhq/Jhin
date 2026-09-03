"use client";

/** Shared data hooks (TanStack Query) for the authenticated app. */

import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { api, ApiError } from "@/lib/api";
import { ollamaNamePath } from "@/lib/models";
import { devicePollDelayMs, SLOW_DOWN_STEP_MS } from "@/lib/oauth";
import type {
  ActivityList,
  Agent,
  AnswerQuestionIn,
  AnswerQuestionOut,
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
  ApiKeyInfo,
  ApiKeyUsagePage,
  ApprovalList,
  AuditEventPage,
  BootstrapStatus,
  CatalogApp,
  CatalogEntryDetail,
  CatalogFacets,
  CatalogSearchParams,
  CatalogSearchResult,
  CatalogVersion,
  ConnectionInfo,
  ConnectionAccessSummaryOut,
  ConnectionToolsOut,
  ConnectorInfo,
  Grant,
  Invitation,
  Member,
  IdentityResponse,
  ModelProfile,
  ModelProvider,
  GitHubAppManifestOut,
  OAuthClientCreate,
  OAuthClientOut,
  OAuthDevicePollOut,
  OAuthDeviceStartOut,
  OAuthProbeOut,
  OAuthRedirectOut,
  OAuthStartOut,
  OllamaLoaded,
  OllamaModelDetailsResult,
  OllamaModels,
  OnboardingState,
  OnboardingStatus,
  PersonaList,
  ProviderBalance,
  PricingStatus,
  ProviderModels,
  OrgGraph,
  RiskFloorApplied,
  RunEvent,
  RunList,
  ScopeCatalog,
  SecretOut,
  AgentSkillInfo,
  BrowseInstallResult,
  BrowseListResult,
  SkillDetail,
  SkillList,
  SkillSourceInfo,
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
  WorkspaceDeletionSummary,
  WorkspaceSpend,
} from "@/lib/types";

/** Poll cadence for live task/run views. */
const LIVE_POLL_MS = 2000;

/**
 * Who is signed in and where they may act.
 *
 * `/auth/identity` rather than `/auth/me` because the desktop app authenticates
 * with an API key, which `/auth/me` refuses — see docs/architecture/api-keys.md.
 * The response is a superset, so the browser build reads the same call.
 */
export function useIdentity() {
  return useQuery({
    queryKey: ["identity"],
    queryFn: () => api<IdentityResponse>("/api/v1/auth/identity"),
  });
}

export function useBootstrapStatus(enabled = true) {
  return useQuery({
    queryKey: ["bootstrap-status"],
    queryFn: () => api<BootstrapStatus>("/api/v1/auth/bootstrap-status"),
    staleTime: 0,
    enabled,
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

/** Pending and past invitations; admin+ only, so it is opt-in via `enabled`. */
export function useInvitations(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["invitations", workspaceId],
    queryFn: () => api<Invitation[]>(`/api/v1/workspaces/${workspaceId}/invitations`),
    enabled,
  });
}

export function useApiKeys(workspaceId: string) {
  return useQuery({
    queryKey: ["api-keys", workspaceId],
    queryFn: () => api<ApiKeyInfo[]>(`/api/v1/workspaces/${workspaceId}/api-keys`),
  });
}

/** The scope taxonomy, annotated with what the signed-in role may grant. The
 * UI never hard-codes scope strings; it renders whatever this returns. */
export function useScopeCatalog(workspaceId: string) {
  return useQuery({
    queryKey: ["api-key-scopes", workspaceId],
    queryFn: () => api<ScopeCatalog>(`/api/v1/workspaces/${workspaceId}/api-keys/scopes`),
    staleTime: 5 * 60_000,
  });
}

export function useApiKeyUsage(workspaceId: string, apiKeyId?: string) {
  return useQuery({
    queryKey: ["api-key-usage", workspaceId, apiKeyId ?? "all"],
    queryFn: () =>
      api<ApiKeyUsagePage>(`/api/v1/workspaces/${workspaceId}/api-keys/usage`, {
        params: { limit: 100, api_key_id: apiKeyId },
      }),
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

/** Owner-only inventory of what deleting this workspace would destroy. Fetched
 *  only when the confirmation dialog is open — nobody needs twelve COUNTs to
 *  render a Settings page — and never cached, because a stale count on an
 *  irreversible decision is worse than a spinner. */
export function useWorkspaceDeletionSummary(workspaceId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["workspace-deletion-summary", workspaceId],
    queryFn: () =>
      api<WorkspaceDeletionSummary>(`/api/v1/workspaces/${workspaceId}/deletion-summary`),
    enabled,
    gcTime: 0,
    staleTime: 0,
    retry: false,
  });
}

export function useSecrets(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["secrets", workspaceId],
    queryFn: () => api<SecretOut[]>(`/api/v1/workspaces/${workspaceId}/secrets`),
    enabled,
  });
}

/** Providers are an admin-only listing server-side; pass `enabled: false` for
 * everyone else so the page doesn't manufacture a 403 it then reports as a
 * load failure. */
export function useModelProviders(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["model-providers", workspaceId],
    queryFn: () => api<ModelProvider[]>(`/api/v1/workspaces/${workspaceId}/model-providers`),
    enabled,
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

/** Models installed on an Ollama provider's host, each merged with whether
 * it is resident. Reads never fail the query for a host that is up: an
 * unreadable host answers an empty list with a `detail`. `retry: false`
 * because the API already waited on the host once; asking again only
 * delays the "can't reach it" message.
 *
 * The options stand on their own (not only inside the hook) because the
 * Models page subscribes to every Ollama host at once through `useQueries`
 * — see lib/ollama-host.ts — and both forms must describe the same query. */
export function ollamaModelsQuery(workspaceId: string, providerId: string) {
  return queryOptions({
    queryKey: ["ollama-models", workspaceId, providerId],
    queryFn: () =>
      api<OllamaModels>(
        `/api/v1/workspaces/${workspaceId}/model-providers/${providerId}/ollama/models`,
      ),
    staleTime: 30_000,
    retry: false,
  });
}

export function useOllamaModels(workspaceId: string, providerId: string, enabled = true) {
  return useQuery({ ...ollamaModelsQuery(workspaceId, providerId), enabled });
}

/** What the Ollama host holds in memory right now. Polled every ten seconds
 * (the trigger/attention cadence) because a load the API handed off after its
 * response budget only ever shows up here, and Ollama's own keep-alive timer
 * unloads models without telling anyone. Every reader of one host must share
 * one subscription (lib/ollama-host.ts): each extra observer is an extra
 * timer against the same endpoint. */
export function ollamaLoadedQuery(workspaceId: string, providerId: string) {
  return queryOptions({
    queryKey: ["ollama-loaded", workspaceId, providerId],
    queryFn: () =>
      api<OllamaLoaded>(
        `/api/v1/workspaces/${workspaceId}/model-providers/${providerId}/ollama/loaded`,
      ),
    refetchInterval: 10_000,
    retry: false,
  });
}

export function useOllamaLoaded(workspaceId: string, providerId: string, enabled = true) {
  return useQuery({ ...ollamaLoadedQuery(workspaceId, providerId), enabled });
}

/** Metadata for one installed model (capabilities, license, context). The
 * name goes through `ollamaNamePath`: colons are encoded, slashes stay real
 * separators for the API's `{name:path}` route. */
export function useOllamaModelDetails(
  workspaceId: string,
  providerId: string,
  name: string | null,
) {
  return useQuery({
    queryKey: ["ollama-model-details", workspaceId, providerId, name],
    queryFn: () =>
      api<OllamaModelDetailsResult>(
        `/api/v1/workspaces/${workspaceId}/model-providers/${providerId}/ollama/models/${ollamaNamePath(name ?? "")}`,
      ),
    enabled: name !== null,
    staleTime: 60_000,
    retry: false,
  });
}

/** Refresh the local-models panel after a load or unload. */
export function useInvalidateOllama(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["ollama-models", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["ollama-loaded", workspaceId] });
  };
}

export function useWorkspaceSpend(workspaceId: string) {
  return useQuery({
    queryKey: ["workspace-spend", workspaceId],
    queryFn: () => api<WorkspaceSpend>(`/api/v1/workspaces/${workspaceId}/spend`),
    staleTime: 30_000,
  });
}

/** Every profile's price, where it came from, and what could improve it. */
export function usePricingStatus(workspaceId: string) {
  return useQuery({
    queryKey: ["pricing-status", workspaceId],
    queryFn: () => api<PricingStatus>(`/api/v1/workspaces/${workspaceId}/model-profiles/pricing-status`),
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
    // A provider edit can change the base URL, which is the whole identity of
    // an Ollama host: the local-models panel must not keep showing the old one.
    void queryClient.invalidateQueries({ queryKey: ["ollama-models", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["ollama-loaded", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["workspace-spend", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["pricing-status", workspaceId] });
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

// --- The synced app/skill catalog (docs/architecture/catalog.md) ---

/**
 * Query-string form of the catalog filters.
 *
 * `api` drops `undefined` and empty strings, so an unset facet simply never
 * reaches the wire; booleans are stringified because a `false` that matters
 * (`include_indexed`) has to survive that same filter.
 */
function catalogParams(
  params: CatalogSearchParams,
): Record<string, string | number | undefined> {
  return {
    q: params.q || undefined,
    kind: params.kind,
    category: params.category || undefined,
    trust_tier: params.trust_tier,
    transport: params.transport || undefined,
    auth_hint: params.auth_hint || undefined,
    connectable: params.connectable === undefined ? undefined : String(params.connectable),
    include_indexed:
      params.include_indexed === undefined ? undefined : String(params.include_indexed),
    limit: params.limit,
    offset: params.offset,
  };
}

/**
 * One page of catalog hits: the curated entries first, then whatever the last
 * sync indexed. The previous page stays on screen while the next one loads so
 * typing in the search box never blanks the grid.
 */
export function useCatalogSearch(params: CatalogSearchParams) {
  return useQuery({
    queryKey: ["catalog-search", params],
    queryFn: () =>
      api<CatalogSearchResult>("/api/v1/catalog/entries", { params: catalogParams(params) }),
    placeholderData: (previous) => previous,
    staleTime: 60_000,
  });
}

/** Bucket counts for the filter chips, under the same filters as the search. */
export function useCatalogFacets(params: Omit<CatalogSearchParams, "limit" | "offset">) {
  return useQuery({
    queryKey: ["catalog-facets", params],
    queryFn: () =>
      api<CatalogFacets>("/api/v1/catalog/facets", { params: catalogParams(params) }),
    placeholderData: (previous) => previous,
    staleTime: 60_000,
  });
}

/** Everything one entry knows, including the Connect form's render contract. */
export function useCatalogEntry(slug: string | null) {
  return useQuery({
    queryKey: ["catalog-entry", slug],
    queryFn: () => api<CatalogEntryDetail>(`/api/v1/catalog/entries/${slug}`),
    enabled: slug !== null,
  });
}

/** Which catalog release is live, or null before the first sync lands. */
export function useCatalogVersion() {
  return useQuery({
    queryKey: ["catalog-version"],
    queryFn: () => api<CatalogVersion | null>("/api/v1/catalog/version"),
    staleTime: 300_000,
  });
}

/**
 * Raise a connection's discovered tools to the risk floor its catalog entry
 * justifies. Only ever raises: a tool an admin already set higher is left
 * alone, so pressing it twice changes nothing.
 */
export function useApplyRiskFloor(workspaceId: string) {
  const invalidate = useInvalidateConnections(workspaceId);
  return useMutation({
    mutationFn: (body: { connection_id: string; slug: string }) =>
      api<RiskFloorApplied>(`/api/v1/workspaces/${workspaceId}/catalog/apply-risk-floor`, {
        method: "POST",
        body,
      }),
    onSuccess: () => invalidate(),
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

// --- OAuth connect (docs/architecture/oauth.md) ---

/**
 * The one callback URL this instance registers with every provider.
 *
 * Computed by the server from `OAUTH_REDIRECT_BASE_URL` or `APP_URL` and
 * shown verbatim wherever an admin has to paste it into a provider's app
 * settings. Cached hard: it only changes when the deployment does.
 */
export function useRedirectUri() {
  return useQuery({
    queryKey: ["oauth-redirect-uri"],
    queryFn: () => api<OAuthRedirectOut>("/api/v1/oauth/redirect-uri"),
    staleTime: 300_000,
    retry: false,
  });
}

/**
 * Ask the server how this app actually signs in.
 *
 * A mutation rather than a query because it is an action with a side effect
 * on the far side — the server dials the app's endpoint — and because the
 * connect panel wants it once per Connect, not on a cache schedule.
 */
export function useOAuthProbe(workspaceId: string) {
  return useMutation({
    mutationFn: (body: { connector_type: string; server_url?: string | null }) =>
      api<OAuthProbeOut>(`/api/v1/workspaces/${workspaceId}/oauth/probe`, {
        method: "POST",
        body,
      }),
  });
}

/** Begin an authorization-code flow. The response's URL is where the browser
 * goes next; nothing is connected until the provider redirects back. */
export function useOAuthStart(workspaceId: string) {
  return useMutation({
    mutationFn: (body: {
      connector_type: string;
      name: string;
      config?: Record<string, unknown>;
      connection_id?: string;
      provider_key?: string;
    }) =>
      api<OAuthStartOut>(`/api/v1/workspaces/${workspaceId}/oauth/start`, {
        method: "POST",
        body,
      }),
  });
}

/** Re-authorize an existing connection in place: same row, same grants, new
 * tokens. This is what the Reconnect buttons call. */
export function useReauthorizeConnection(workspaceId: string) {
  return useMutation({
    mutationFn: (connectionId: string) =>
      api<OAuthStartOut>(
        `/api/v1/workspaces/${workspaceId}/connections/${connectionId}/reauthorize`,
        { method: "POST" },
      ),
  });
}

/** Start the device flow: the server holds the device code, the browser gets
 * a display code and an opaque poll handle. */
export function useOAuthDeviceStart(workspaceId: string) {
  return useMutation({
    mutationFn: (body: {
      connector_type: string;
      name: string;
      config?: Record<string, unknown>;
    }) =>
      api<OAuthDeviceStartOut>(`/api/v1/workspaces/${workspaceId}/oauth/device/start`, {
        method: "POST",
        body,
      }),
  });
}

/**
 * Poll a device authorization until it lands.
 *
 * `intervalMs` is a floor, not the cadence: the server can raise it mid-flow,
 * and each `slow_down` adds five seconds that are never given back — backing
 * off and then quietly speeding up again is how a client gets rate-limited
 * out of the flow entirely. The backoff lives in a ref rather than in state
 * because it changes the poll timer, not the picture on screen, and it is
 * applied once per response rather than once per render.
 *
 * Polling stops itself on any terminal status: a connected, denied, or
 * expired authorization is never asked about twice.
 */
export function useOAuthDevicePoll(
  workspaceId: string,
  handle: string | null,
  intervalMs: number,
) {
  const backoffMs = useRef(0);
  const handledAt = useRef(0);
  return useQuery({
    queryKey: ["oauth-device-poll", workspaceId, handle],
    queryFn: () =>
      api<OAuthDevicePollOut>(`/api/v1/workspaces/${workspaceId}/oauth/device/poll`, {
        method: "POST",
        body: { handle },
      }),
    enabled: handle !== null,
    gcTime: 0,
    retry: false,
    refetchInterval: (query) => {
      const { data, dataUpdatedAt } = query.state;
      if (data?.status === "slow_down" && dataUpdatedAt !== handledAt.current) {
        handledAt.current = dataUpdatedAt;
        backoffMs.current += SLOW_DOWN_STEP_MS;
      }
      return devicePollDelayMs(intervalMs, data?.status, data?.interval_seconds, backoffMs.current);
    },
  });
}

/** The OAuth apps registered for this workspace, one per authorization
 * server. Admin-only on the server; the page gates on the same role. */
export function useOAuthClients(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["oauth-clients", workspaceId],
    queryFn: () => api<OAuthClientOut[]>(`/api/v1/workspaces/${workspaceId}/oauth/clients`),
    enabled,
  });
}

export function useCreateOAuthClient(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: OAuthClientCreate) =>
      api<OAuthClientOut>(`/api/v1/workspaces/${workspaceId}/oauth/clients`, {
        method: "POST",
        body,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["oauth-clients", workspaceId] });
    },
  });
}

export function useDeleteOAuthClient(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (registrationId: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/oauth/clients/${registrationId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["oauth-clients", workspaceId] });
    },
  });
}

/** Ask the server to describe a GitHub App this instance would own. The
 * browser posts the returned manifest to GitHub as an ordinary form. */
export function useGitHubAppManifest(workspaceId: string) {
  return useMutation({
    mutationFn: (body: { app_name: string; organization?: string | null }) =>
      api<GitHubAppManifestOut>(`/api/v1/workspaces/${workspaceId}/oauth/github-app/manifest`, {
        method: "POST",
        body,
      }),
  });
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
  enabled = true,
) {
  return useQuery({
    queryKey: ["conversations", workspaceId, params],
    queryFn: () =>
      api<ConversationList>(`/api/v1/workspaces/${workspaceId}/conversations`, { params }),
    placeholderData: (previous) => previous,
    refetchInterval: 5_000,
    enabled,
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

/**
 * Answer a question an agent asked in a chat (ask-user contract §2.1).
 *
 * One invalidation of the whole conversation family on success, not a
 * targeted cache patch: answering rewrites the question message in place,
 * flips the conversation off `waiting_person`, and clears an attention
 * badge, and those three have to move together or the thread reads as
 * still-blocked after the person has already answered.
 */
export function useAnswerQuestion(workspaceId: string) {
  const invalidate = useInvalidateConversations(workspaceId);
  return useMutation({
    mutationFn: ({ questionId, body }: { questionId: string; body: AnswerQuestionIn }) =>
      api<AnswerQuestionOut>(
        `/api/v1/workspaces/${workspaceId}/questions/${questionId}/answer`,
        { method: "POST", body },
      ),
    onSuccess: () => invalidate(),
  });
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

// --- Skills (docs/architecture/skills.md) ---

export function useSkills(workspaceId: string, q = "", category = "") {
  return useQuery({
    queryKey: ["skills", workspaceId, q, category],
    queryFn: () =>
      api<SkillList>(`/api/v1/workspaces/${workspaceId}/skills`, {
        params: { q: q || undefined, category: category || undefined, limit: 100 },
      }),
    placeholderData: (previous) => previous,
  });
}

export function useSkill(workspaceId: string, skillId: string | null) {
  return useQuery({
    queryKey: ["skill", workspaceId, skillId],
    queryFn: () => api<SkillDetail>(`/api/v1/workspaces/${workspaceId}/skills/${skillId}`),
    enabled: skillId !== null,
  });
}

export function useAgentSkills(workspaceId: string, agentId: string) {
  return useQuery({
    queryKey: ["agent-skills", workspaceId, agentId],
    queryFn: () =>
      api<AgentSkillInfo[]>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}/skills`),
  });
}

/** The default catalog plus this workspace's own custom additions. */
export function useSkillSources(workspaceId: string) {
  return useQuery({
    queryKey: ["skill-sources", workspaceId],
    queryFn: () => api<SkillSourceInfo[]>(`/api/v1/workspaces/${workspaceId}/skill-sources`),
    staleTime: 5 * 60_000,
  });
}

/** One source's parsed skill listing, filtered by `q`; server-cached ~10 min. */
export function useBrowseSkills(workspaceId: string, source: string, q: string) {
  return useQuery({
    queryKey: ["skills-browse", workspaceId, source, q],
    queryFn: () =>
      api<BrowseListResult>(`/api/v1/workspaces/${workspaceId}/skills/browse`, {
        params: { source, q: q || undefined },
      }),
    enabled: source !== "",
    placeholderData: (previous) => previous,
  });
}

/**
 * Install one reviewed catalog skill by slug. The server resolves the slug to
 * a repository itself and refuses anything outside the reviewed tier, so the
 * client never gets to pick where the bytes come from. Success refreshes the
 * installed list, the browse gallery's `installed` flags, and the catalog
 * search pages that render the Install button's state.
 */
export function useInstallCatalogSkill(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) =>
      api<BrowseInstallResult>(`/api/v1/workspaces/${workspaceId}/skills/install-from-catalog`, {
        method: "POST",
        body: { slug },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["skills", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["skills-browse", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["catalog-search"] });
    },
  });
}

export function useInvalidateSkills(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["skills", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["skill", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["agent-skills", workspaceId] });
  };
}

export function useInvalidateSkillSources(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["skill-sources", workspaceId] });
  };
}

// --- Personas (docs/architecture/personas.md) ---

/** The whole library in one call: a workspace holds a few dozen cards and a
 * card is at most 1.5 KB, so filtering happens client-side (lib/personas.ts). */
export function usePersonas(workspaceId: string) {
  return useQuery({
    queryKey: ["personas", workspaceId],
    queryFn: () =>
      api<PersonaList>(`/api/v1/workspaces/${workspaceId}/personas`, { params: { limit: 100 } }),
  });
}

export function useInvalidatePersonas(workspaceId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["personas", workspaceId] });
    // The persona summary rides on the agent and on the chat header.
    void queryClient.invalidateQueries({ queryKey: ["agent", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["conversation", workspaceId] });
  };
}

/**
 * Where the signed-in user got to in this workspace's guided introduction.
 *
 * Server-side state, not local storage: skipping the tour on a laptop has to
 * skip it on a phone too, and clearing a browser must not resurrect it. Never
 * retried — if this call fails, the tour simply stays shut, which is the safe
 * way for it to fail.
 */
export function useOnboarding(workspaceId: string) {
  return useQuery({
    queryKey: ["onboarding", workspaceId],
    queryFn: () => api<OnboardingState>(`/api/v1/workspaces/${workspaceId}/onboarding`),
    staleTime: Infinity,
    retry: false,
  });
}

/**
 * Record that the tour was skipped, paused, or finished.
 *
 * The cache is written first and the request is not awaited: closing an
 * overlay must never wait on the network. The worst case if the write fails is
 * that the introduction offers itself once more.
 */
export function useSaveOnboarding(workspaceId: string) {
  const queryClient = useQueryClient();
  return (status: OnboardingStatus, lastStep: string | null) => {
    queryClient.setQueryData<OnboardingState>(["onboarding", workspaceId], {
      status,
      last_step: lastStep,
      updated_at: new Date().toISOString(),
    });
    void api<OnboardingState>(`/api/v1/workspaces/${workspaceId}/onboarding`, {
      method: "PUT",
      body: { status, last_step: lastStep },
    }).catch(() => {
      void queryClient.invalidateQueries({ queryKey: ["onboarding", workspaceId] });
    });
  };
}
