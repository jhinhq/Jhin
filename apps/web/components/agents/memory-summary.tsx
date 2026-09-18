"use client";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Button, ErrorNote, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { MemoryScope } from "@/lib/types";

export interface MemorySummaryResult {
  scope: MemoryScope; scope_id: string; version: string; summary: string;
  coverage_count: number; source_count: number; generated_at: string | null; stale: boolean;
  items: { id: string; version: number; content: string; source_conversation_id: string | null; source_message_id: string | null; source_task_id: string | null }[];
}
export function MemorySummary({ workspaceId, scope, scopeId, canRebuild }: { workspaceId: string; scope: MemoryScope; scopeId: string; canRebuild: boolean }) {
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const base = `/api/v1/workspaces/${workspaceId}/memories/summary`;
  const query = useQuery({ queryKey: ["memories", workspaceId, "summary", scope, scopeId], queryFn: () => api<MemorySummaryResult>(base, { params: { scope, scope_id: scopeId } }), enabled: open, refetchInterval: open ? 30000 : false });
  const refresh = async () => { setBusy(true); setError(null); try { await api(`${base}/rebuild`, {method:"POST",params:{scope,scope_id:scopeId}}); await query.refetch(); } catch { setError("Couldn't refresh this summary. Reload to try again."); } finally { setBusy(false); } };
  return <details className="mb-4 rounded-xl border border-line bg-raised p-4" onToggle={(event) => setOpen(event.currentTarget.open)}><summary className="cursor-pointer text-sm font-medium">Current memory summary</summary>
    <div className="mt-3 space-y-3"><p className="text-xs text-dim">A current, bounded summary of supported active facts. It changes when its source memories change.</p><ErrorNote message={error || (query.isError ? "Couldn't load this summary." : null)} />{query.isPending && open ? <Spinner label="Loading memory summary…" /> : null}
      {query.data ? <><p className="whitespace-pre-wrap break-words text-sm">{query.data.summary || "No supported memories in this scope yet."}</p><p className="text-xs text-dim">{query.data.coverage_count} supported memories · {query.data.source_count} sources · {query.data.stale ? "Needs refresh" : "Current"}{query.data.generated_at ? ` · Updated ${formatDateTime(query.data.generated_at)}` : ""}</p><details><summary className="cursor-pointer text-xs text-accent-strong">Sources and versions</summary><p className="mt-2 break-all text-xs text-faint">Summary revision {query.data.version}</p><ul className="mt-2 space-y-3">{query.data.items.map((item) => <li key={item.id} className="border-l border-line pl-3 text-sm"><p className="whitespace-pre-wrap break-words">{item.content}</p><div className="mt-1 flex flex-wrap gap-3 text-xs text-dim"><span>Memory version {item.version}</span>{item.source_conversation_id ? <Link className="text-accent-strong underline" href={`/chats/${item.source_conversation_id}`}>Source chat</Link> : null}{item.source_task_id ? <Link className="text-accent-strong underline" href={`/tasks/${item.source_task_id}`}>Source task</Link> : null}</div></li>)}</ul></details></> : null}
      {canRebuild ? <Button size="sm" disabled={busy} onClick={() => void refresh()}>{busy ? "Refreshing…" : "Refresh summary"}</Button> : <Button size="sm" onClick={() => void query.refetch()}>Reload summary</Button>}
    </div>
  </details>;
}
