import { useInfiniteQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "./api";
export interface ManagedFile {
  id: string; workspace_id: string; conversation_id: string; name: string; path: string;
  kind: "upload" | "artifact" | "working"; status: "ready" | "failed"; error: string | null;
  mime_type: string; size_bytes: number; preview_kind: "text" | "code" | "image" | "pdf" | "document" | "slides" | "spreadsheet";
  current_revision_id: string; version: number; sha256: string; extracted_text: string; extraction_truncated: boolean;
  created_at: string; updated_at: string; download_url: string; preview_url: string;
}
export interface FileRevision { id: string; file_id: string; version: number; sha256: string; size_bytes: number; mime_type: string; preview_kind: ManagedFile["preview_kind"]; created_at: string; }
export interface FileContent { file_id: string; revision_id: string; path: string; content: string; truncated: boolean; editable: boolean; sha256: string; }
export interface WorkspaceProject { id: string; name: string; description: string; repository_url: string | null; source_revision: string | null; context: string; }
export interface Checkpoint { id: string; label: string; manifest_json: Record<string, string>; excluded_json: string[]; created_at: string; }
export interface FileChange { path: string; status: "added" | "modified" | "deleted"; before_revision_id: string | null; after_revision_id: string | null; current_sha256: string | null; diff: string; }
export function useConversationFiles(workspaceId: string, conversationId: string | null, enabled = true) {
  const query = useInfiniteQuery({ queryKey: ["conversation-files", workspaceId, conversationId], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api<{ items: ManagedFile[]; has_more: boolean }>(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/files`, { params: pageParam ? { cursor: pageParam } : undefined }),
    getNextPageParam: (page) => page.has_more ? page.items.at(-1)?.id : undefined,
    enabled: !!conversationId && enabled, refetchInterval: 5000 });
  const data = useMemo(() => query.data ? { items: [...new Map(query.data.pages.flatMap((page) => page.items).map((file) => [file.id, file])).values()], has_more: query.hasNextPage } : undefined, [query.data, query.hasNextPage]);
  return { ...query, data };
}
export function fileSize(bytes: number): string { return bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
