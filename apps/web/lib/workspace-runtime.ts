import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
export interface RuntimeSession { id: string; kind: "terminal" | "preview"; status: string; cwd: string; network: string; exit_code: number | null; output: string; output_offset: number; lease_generation: number; created_at: string; revision_id?: string; url?: string; user_id?: string; }
export interface ConversationRuntime { workspace_key: string; cwd: string; owner: "agent" | "user" | null; owner_user_id: string | null; lease_generation: number; terminal: RuntimeSession | null; previews: RuntimeSession[]; legacy?: boolean; legacy_workspace_key?: string | null; }
export function conversationPath(workspaceId: string, conversationId: string) { return `/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`; }
export function useConversationRuntime(workspaceId: string, conversationId: string, enabled = true) {
  return useQuery({ queryKey: ["conversation-runtime", workspaceId, conversationId], queryFn: () => api<ConversationRuntime>(`${conversationPath(workspaceId, conversationId)}/runtime`), enabled, refetchInterval: enabled ? 3000 : false });
}
