"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "@/lib/api";
import type { Checkpoint, FileChange } from "@/lib/workspace-files";
import { conversationPath, type ConversationRuntime } from "@/lib/workspace-runtime";
import { ConfirmDialog, ErrorNote } from "@/components/ui";

export function WorkspaceChanges({ workspaceId, conversationId, runtime, canEdit, onFeedback }: { workspaceId: string; conversationId: string; runtime?: ConversationRuntime; canEdit: boolean; onFeedback: (text: string) => void }) {
  const base = conversationPath(workspaceId, conversationId), queryClient = useQueryClient();
  const changes = useQuery({ queryKey: ["workspace-changes", workspaceId, conversationId], queryFn: () => api<{ items: FileChange[]; excluded: string[]; checkpoint_id?: string }>(`${base}/changes`), refetchInterval: 5000 });
  const checkpoints = useQuery({ queryKey: ["workspace-checkpoints", workspaceId, conversationId], queryFn: () => api<{ items: Checkpoint[] }>(`${base}/checkpoints`) });
  const [selected, setSelected] = useState<string[]>([]), [checkpoint, setCheckpoint] = useState(""), [error, setError] = useState<string | null>(null), [busy, setBusy] = useState(false), [confirm, setConfirm] = useState(false);
  const [feedback, setFeedback] = useState("");
  const mutate = async (path: string, body?: unknown) => {
    setBusy(true); setError(null);
    try { await api(`${base}${path}`, { method: "POST", body }); await Promise.all([queryClient.invalidateQueries({ queryKey: ["workspace-changes", workspaceId, conversationId] }), queryClient.invalidateQueries({ queryKey: ["workspace-checkpoints", workspaceId, conversationId] }), queryClient.invalidateQueries({ queryKey: ["conversation-files", workspaceId, conversationId] })]); setSelected([]); }
    catch (failure) { setError(errorText(failure, "Couldn't update the checkpoint. No conflicting files were overwritten.")); }
    finally { setBusy(false); setConfirm(false); }
  };
  return <div className="flex h-full min-h-0 flex-col gap-3 p-3"><div className="flex flex-wrap items-center gap-2"><h3 className="mr-auto text-sm font-semibold">Changes</h3>{canEdit ? <button type="button" disabled={busy} className="rounded-lg border border-line p-2 text-xs" onClick={() => void mutate("/checkpoints", { label: "Manual checkpoint" })}>Create checkpoint</button> : null}</div><ErrorNote message={error || (changes.isError ? "Couldn't load changes." : null)} /><div className="min-h-0 flex-1 overflow-auto">
    {(changes.data?.items ?? []).map((change) => <details key={change.path} className="mb-2 overflow-hidden rounded-lg border border-line"><summary className="flex cursor-pointer items-center gap-2 bg-raised p-2 text-xs">{canEdit ? <input aria-label={`Select ${change.path}`} type="checkbox" checked={selected.includes(change.path)} onClick={(event) => event.stopPropagation()} onChange={(event) => setSelected((old) => event.target.checked ? [...old, change.path] : old.filter((path) => path !== change.path))} /> : null}<span className="min-w-0 flex-1 break-all font-mono">{change.path}</span><span className="text-dim">{change.status}</span></summary><pre className="max-h-96 overflow-auto p-2 font-mono text-xs leading-relaxed">{change.diff.split("\n").map((line, index) => <span key={index} className={`block ${line.startsWith("+") ? "bg-ok/10 text-ok" : line.startsWith("-") ? "bg-danger/10 text-danger" : "text-dim"}`}>{line || " "}</span>)}</pre><button type="button" className="p-2 text-xs text-accent-strong" onClick={() => onFeedback(`Please review the changes to ${change.path}:\n`)}>Comment on this file</button></details>)}
    {!changes.isPending && !changes.data?.items.length ? <p className="py-8 text-center text-sm text-dim">No changes since the checkpoint.</p> : null}
    {changes.data?.excluded.length ? <details className="text-xs text-dim"><summary>Excluded files</summary><pre className="whitespace-pre-wrap">{changes.data.excluded.join("\n")}</pre></details> : null}
  </div>
    {canEdit ? <div className="flex flex-wrap gap-2 border-t border-line pt-3"><select aria-label="Restore checkpoint" value={checkpoint} onChange={(event) => setCheckpoint(event.target.value)} className="min-w-0 flex-1 rounded-lg border border-line bg-surface p-2 text-xs"><option value="">Choose checkpoint…</option>{checkpoints.data?.items.map((item) => <option key={item.id} value={item.id}>{item.label || new Date(item.created_at).toLocaleString()}</option>)}</select><button type="button" disabled={busy || !checkpoint || !selected.length} className="rounded-lg border border-line p-2 text-xs disabled:opacity-40" onClick={() => setConfirm(true)}>Restore selected ({selected.length})</button></div> : null}
    <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); onFeedback(feedback); setFeedback(""); }}><input aria-label="Change review feedback" placeholder="Feedback on these changes…" value={feedback} onChange={(event) => setFeedback(event.target.value)} className="min-w-0 flex-1 rounded-lg border border-line bg-surface p-2 text-xs" /><button type="submit" disabled={!feedback.trim()} className="rounded-lg border border-line p-2 text-xs disabled:opacity-40">Add to message</button></form>
    <ConfirmDialog open={confirm} title="Restore selected files?" body={`Restore ${selected.length} selected file(s) from this checkpoint. External app actions are not reversed.`} confirmLabel="Restore files" cancelLabel="Keep current files" busy={busy} onClose={() => setConfirm(false)} onConfirm={() => { if (runtime) void mutate(`/checkpoints/${checkpoint}/restore`, { paths: selected, expected_revisions: Object.fromEntries((changes.data?.items ?? []).filter((item) => selected.includes(item.path)).map((item) => [item.path, item.current_sha256])), lease_generation: runtime.lease_generation }); }} />
  </div>;
}
