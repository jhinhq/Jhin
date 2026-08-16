"use client";

/** Shared data hooks (TanStack Query) for the authenticated app. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  Agent,
  AuditEventPage,
  BootstrapStatus,
  Member,
  MeResponse,
  OrgGraph,
  Team,
} from "@/lib/types";

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
