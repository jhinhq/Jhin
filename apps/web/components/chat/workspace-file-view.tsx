"use client";
/* eslint-disable @next/next/no-img-element -- Authenticated file previews require the browser's session. */
import { lazy, Suspense, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "@/lib/api";
import { fileSize, type FileContent, type FileRevision, type ManagedFile } from "@/lib/workspace-files";
import type { ConversationRuntime } from "@/lib/workspace-runtime";
import { ErrorNote, Spinner } from "@/components/ui";
const WorkspaceEditor = lazy(() => import("./workspace-editor"));
function restoreBuffer(workspaceId: string, file: ManagedFile): { revision: string; text: string | null } {
  try {
    const saved = JSON.parse(localStorage.getItem(`jhin-file-draft:${workspaceId}:${file.id}`) ?? "null");
    if (saved && typeof saved.revision === "string" && typeof saved.text === "string") return saved;
    return { revision: file.current_revision_id, text: localStorage.getItem(`jhin-file-buffer:${workspaceId}:${file.id}:${file.current_revision_id}`) };
  } catch { return { revision: file.current_revision_id, text: null }; }
}

export function WorkspaceFileView({ file, workspaceId, runtime, canEdit, canWrite = true, onUse, onFeedback }: { file: ManagedFile; workspaceId: string; runtime?: ConversationRuntime; canEdit: boolean; canWrite?: boolean; onUse: (file: ManagedFile, revisionId?: string) => void; onFeedback?: (text: string) => void }) {
  const base = `/api/v1/workspaces/${workspaceId}/files/${file.id}`;
  const queryClient = useQueryClient();
  const [restored] = useState(() => restoreBuffer(workspaceId, file));
  const [revision, setRevision] = useState(restored.revision);
  const textPreview = ["code", "text"].includes(file.preview_kind) || ["text/csv", "text/tab-separated-values"].includes(file.mime_type);
  const versions = useQuery({ queryKey: ["file-versions", workspaceId, file.id, file.current_revision_id], queryFn: () => api<{ items: FileRevision[] }>(`${base}/versions`) });
  const content = useQuery({ queryKey: ["file-content", workspaceId, file.id, revision], queryFn: () => api<FileContent>(`${base}/content`, { params: { revision_id: revision } }), enabled: !["image", "pdf"].includes(file.preview_kind) });
  const [buffer, setBuffer] = useState<string | null>(restored.text);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null), [annotation, setAnnotation] = useState("");
  const [comparison, setComparison] = useState<FileContent | null>(null);
  const text = buffer ?? content.data?.content ?? "";
  const dirty = buffer !== null && buffer !== content.data?.content;
  const editable = canEdit && content.data?.editable && revision === file.current_revision_id;
  const persistBuffer = (value: string, baseRevision = revision) => { try { localStorage.setItem(`jhin-file-draft:${workspaceId}:${file.id}`, JSON.stringify({ revision: baseRevision, text: value })); } catch { /* Retain the buffer in memory. */ } };
  const updateBuffer = (value: string) => { setBuffer(value); persistBuffer(value); };
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  const save = async () => {
    if (!runtime || !editable) return;
    setBusy(true); setError(null);
    try {
      const updated = await api<ManagedFile>(`${base}/content`, { method: "PUT", body: { content: text, expected_revision_id: revision, lease_generation: runtime.lease_generation } });
      try { localStorage.removeItem(`jhin-file-draft:${workspaceId}:${file.id}`); localStorage.removeItem(`jhin-file-buffer:${workspaceId}:${file.id}:${revision}`); } catch { /* non-persistent browser */ }
      setBuffer(null); setRevision(updated.current_revision_id); setComparison(null);
      await queryClient.invalidateQueries({ queryKey: ["conversation-files", workspaceId] });
      await queryClient.invalidateQueries({ queryKey: ["file-versions", workspaceId, file.id] });
    } catch (failure) { setError(errorText(failure, "Couldn't save. Your edit is retained; reload the latest version before resolving a conflict.")); }
    finally { setBusy(false); }
  };
  const addAnnotation = async () => {
    setBusy(true); setError(null);
    try { await api(`${base}/annotations`, { method: "POST", body: { revision_id: revision, text: annotation, location: {} } }); onUse(file, revision); onFeedback?.(`Please revise ${file.name} using the selected version:\n${annotation}`); setAnnotation(""); }
    catch (failure) { setError(errorText(failure, "Couldn't save feedback.")); }
    finally { setBusy(false); }
  };
  const compareLatest = async () => {
    setBusy(true);
    try {
      const latest = await api<ManagedFile>(`/api/v1/workspaces/${workspaceId}/conversations/${file.conversation_id}/files/publish`, { method: "POST", body: { path: file.path } });
      const latestContent = await api<FileContent>(`${base}/content`, { params: { revision_id: latest.current_revision_id } });
      setComparison(latestContent);
      await queryClient.invalidateQueries({ queryKey: ["conversation-files", workspaceId] });
    } catch (failure) { setError(errorText(failure, "Couldn't load the latest file. Your edits are retained.")); }
    finally { setBusy(false); }
  };
  const revisionQuery = `?revision_id=${encodeURIComponent(revision)}`;
  return <div className="flex h-full min-h-0 flex-col gap-3 p-3">
    <div className="flex flex-wrap items-start gap-2"><div className="min-w-0 flex-1"><h3 className="break-all text-sm font-semibold">{file.name}</h3><p className="text-xs text-dim">{file.mime_type} · {fileSize(file.size_bytes)}</p></div><a href={`${base}/download${revisionQuery}`} download className="rounded-lg border border-line px-3 py-2 text-xs">Download</a><button type="button" onClick={() => onUse(file, revision)} className="rounded-lg border border-line px-3 py-2 text-xs">Use in next message</button></div>
    <div className="flex flex-wrap items-center gap-2 text-xs"><label className="flex items-center gap-2">Version<select value={revision} disabled={dirty} onChange={(event) => { setRevision(event.target.value); setBuffer(null); }} className="rounded border border-line bg-surface p-1.5">{(versions.data?.items ?? [{ id: file.current_revision_id, version: file.version }]).map((version) => <option key={version.id} value={version.id}>v{version.version}</option>)}{revision !== file.current_revision_id && !(versions.data?.items ?? []).some((version) => version.id === revision) ? <option value={revision}>Retained version</option> : null}</select></label>{dirty ? <span className="text-warn">Unsaved edits retained</span> : null}{editable ? <button type="button" onClick={() => void save()} disabled={!dirty || busy} className="ml-auto rounded-lg bg-accent px-3 py-2 text-white disabled:opacity-40">Save changes</button> : null}</div>
    <ErrorNote message={error || file.error || (content.isError ? "Couldn't load file content." : null)} />
    {dirty && revision !== file.current_revision_id ? <p className="text-xs text-warn">The saved file changed while you were editing. Compare it before saving your retained edit.</p> : null}
    {(error || revision !== file.current_revision_id) && dirty && canEdit ? <button type="button" disabled={busy} onClick={() => void compareLatest()} className="self-start rounded-lg border border-line px-3 py-2 text-xs">Compare with current workspace file</button> : null}
    {comparison ? <details open className="rounded-lg border border-warn/30 p-2 text-xs"><summary className="cursor-pointer font-medium">Current workspace version · your edit is retained below</summary><pre className="my-2 max-h-48 overflow-auto whitespace-pre-wrap break-all">{comparison.content}</pre><button type="button" onClick={() => { persistBuffer(text, comparison.revision_id); setRevision(comparison.revision_id); setError(null); setComparison(null); }} className="rounded-lg border border-line px-3 py-2">Use this version as save base</button><p className="mt-2 text-dim">Merge any changes you want to keep into your edit before saving.</p></details> : null}
    {file.preview_kind === "image" ? <div className="min-h-0 flex-1 overflow-auto"><img src={`${base}/preview${revisionQuery}`} alt={file.name} className="max-h-full max-w-full object-contain" /></div>
      : file.preview_kind === "pdf" ? <iframe title={`Preview ${file.name}`} src={`${base}/preview${revisionQuery}`} sandbox="" className="min-h-80 w-full flex-1 rounded-lg border border-line bg-white" />
        : textPreview ? content.isPending ? <Spinner label="Loading file…" /> : <Suspense fallback={<pre className="overflow-auto whitespace-pre-wrap text-xs">{text}</pre>}><WorkspaceEditor value={text} onChange={updateBuffer} readOnly={!canEdit || busy || !content.data?.editable || (!dirty && revision !== file.current_revision_id)} path={file.path} /></Suspense>
          : <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-line p-3">{content.isPending ? <Spinner label="Loading version preview…" /> : <pre className="whitespace-pre-wrap break-words font-sans text-sm">{content.data?.content || "Download this file to view it in its native application."}</pre>}</div>}
    {content.data?.truncated ? <p className="text-xs text-warn">This content preview is truncated. Download the full file.</p> : null}
    {canWrite ? <form onSubmit={(event) => { event.preventDefault(); void addAnnotation(); }} className="flex flex-wrap gap-2 border-t border-line pt-3"><input aria-label="Feedback on this version" placeholder="Ask for a revision to this version…" value={annotation} onChange={(event) => setAnnotation(event.target.value)} className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-3 py-2 text-xs" /><button type="submit" disabled={busy || !annotation.trim()} className="rounded-lg border border-line px-3 py-2 text-xs disabled:opacity-40">Add feedback to message</button></form> : null}
  </div>;
}
