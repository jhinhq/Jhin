"use client";
import { lazy, Suspense, useEffect, useRef, useState, type CSSProperties } from "react";
import { ArrowLeft, File, X } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "@/lib/api";
import { useConversationRuntime, conversationPath } from "@/lib/workspace-runtime";
import { fileSize, type ManagedFile } from "@/lib/workspace-files";
import { ErrorNote, Spinner } from "@/components/ui";
import { WorkspaceBrowser } from "./workspace-browser";
import { WorkspaceProjectControl } from "./workspace-project";
const WorkspaceFileView = lazy(() => import("./workspace-file-view").then((module) => ({ default: module.WorkspaceFileView })));
const WorkspaceTerminal = lazy(() => import("./workspace-terminal").then((module) => ({ default: module.WorkspaceTerminal })));
const WorkspacePreview = lazy(() => import("./workspace-preview").then((module) => ({ default: module.WorkspacePreview })));
const WorkspaceChanges = lazy(() => import("./workspace-changes").then((module) => ({ default: module.WorkspaceChanges })));
type Tab = "files" | "preview" | "changes" | "terminal";

export function WorkspacePane({ workspaceId, conversationId, files, isAdmin, canWrite, userId, onUse, onFeedback, onClose, initialFileId, projectId, hasMoreFiles, loadingMoreFiles, onLoadMoreFiles }: { workspaceId: string; conversationId: string; files: ManagedFile[]; isAdmin: boolean; canWrite: boolean; userId: string; onUse: (file: ManagedFile, revisionId?: string) => void; onFeedback: (text: string) => void; onClose: () => void; initialFileId?: string | null; projectId?: string | null; hasMoreFiles?: boolean; loadingMoreFiles?: boolean; onLoadMoreFiles?: () => void }) {
  const [tab, setTab] = useState<Tab>("files"), [fileId, setFileId] = useState<string | null>(initialFileId ?? null), [width, setWidth] = useState(560), [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const resize = useRef<{ x: number; width: number } | null>(null);
  const paneRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => { closeRef.current = onClose; }, [onClose]);
  useEffect(() => {
    if (window.matchMedia("(min-width: 1280px)").matches) return;
    const previous = document.activeElement as HTMLElement | null;
    paneRef.current?.querySelector<HTMLElement>("button")?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); closeRef.current(); }
      if (event.key !== "Tab") return;
      const controls = Array.from(paneRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href],[tabindex="0"]') ?? []).filter((node) => node.offsetParent !== null);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("keydown", keydown); previous?.focus(); };
  }, []);
  const runtime = useConversationRuntime(workspaceId, conversationId);
  const queryClient = useQueryClient();
  const owns = isAdmin && runtime.data?.owner === "user" && runtime.data.owner_user_id === userId;
  const selected = files.find((file) => file.id === fileId);
  const changeControl = async () => {
    setBusy(true); setError(null);
    try { await api(`${conversationPath(workspaceId, conversationId)}/runtime/control`, { method: "POST", body: { action: owns ? "return" : "take" } }); await runtime.refetch(); }
    catch (failure) { setError(errorText(failure, "Couldn't transfer workspace control.")); }
    finally { setBusy(false); }
  };
  const importLegacy = async () => {
    setBusy(true); setError(null);
    try { await api(`${conversationPath(workspaceId, conversationId)}/runtime/import-legacy`, { method: "POST", body: {} }); await runtime.refetch(); await queryClient.invalidateQueries({ queryKey: ["conversation-files", workspaceId, conversationId] }); }
    catch (failure) { setError(errorText(failure, "Couldn't copy the existing workspace. Source files remain unchanged.")); }
    finally { setBusy(false); }
  };
  const publish = async (requestedPath = path) => {
    setBusy(true); setError(null);
    try { const file = await api<ManagedFile>(`${conversationPath(workspaceId, conversationId)}/files/publish`, { method: "POST", body: { path: requestedPath } }); setPath(""); setFileId(file.id); await queryClient.invalidateQueries({ queryKey: ["conversation-files", workspaceId, conversationId] }); }
    catch (failure) { setError(errorText(failure, "Couldn't publish this workspace file.")); }
    finally { setBusy(false); }
  };
  return <aside ref={paneRef} aria-label="Chat workspace" className="fixed inset-0 z-40 flex min-h-0 w-full flex-col border-l border-line bg-surface xl:relative xl:z-auto xl:w-[var(--workspace-width)] xl:max-w-[calc(100%_-_18rem)] xl:shrink-0" style={{ "--workspace-width": `${width}px` } as CSSProperties}>
    <div role="separator" aria-label="Resize workspace" aria-orientation="vertical" tabIndex={0} className="absolute -left-1 top-0 hidden h-full w-2 cursor-col-resize touch-none xl:block" onKeyDown={(event) => { if (event.key === "ArrowLeft") setWidth((old) => Math.min(900, old + 40)); if (event.key === "ArrowRight") setWidth((old) => Math.max(360, old - 40)); }} onPointerDown={(event) => { resize.current = { x: event.clientX, width }; event.currentTarget.setPointerCapture(event.pointerId); }} onPointerMove={(event) => { if (resize.current) setWidth(Math.max(360, Math.min(900, resize.current.width + resize.current.x - event.clientX))); }} onPointerUp={() => { resize.current = null; }} />
    <div className="flex items-center gap-2 border-b border-line px-3 py-2"><h2 className="mr-auto font-semibold">Workspace</h2>{isAdmin && !runtime.data?.legacy ? <button type="button" disabled={busy || !runtime.data} onClick={() => void changeControl()} className="rounded-lg border border-line p-2 text-xs disabled:opacity-40">{owns ? "Return to agent" : "Take control"}</button> : null}<button type="button" aria-label="Close workspace" className="flex h-10 w-10 items-center justify-center rounded-lg hover:bg-hover" onClick={onClose}><X size={18} /></button></div>
    <WorkspaceProjectControl workspaceId={workspaceId} conversationId={conversationId} projectId={projectId} canWrite={canWrite} />
    {runtime.data?.legacy ? <div className="border-b border-line bg-raised p-3 text-xs"><p>This older chat uses the agent’s shared workspace. Copy its files into a separate workspace for this chat.</p>{isAdmin ? <button type="button" disabled={busy} onClick={() => void importLegacy()} className="mt-2 rounded-lg border border-line bg-surface p-2">Copy files into this chat</button> : null}</div> : null}
    <div role="tablist" aria-label="Workspace tools" className="flex border-b border-line p-1">{(["files", "preview", "changes", "terminal"] as Tab[]).map((name) => <button key={name} type="button" role="tab" aria-selected={tab === name} aria-controls={`workspace-tab-${name}`} onClick={() => setTab(name)} className={`min-h-10 flex-1 rounded-lg px-2 text-xs capitalize ${tab === name ? "bg-accent-soft font-medium text-accent-strong" : "text-dim hover:bg-hover"}`}>{name}</button>)}</div>
    <div className="px-3 pt-2"><ErrorNote message={error || (runtime.isError ? "Workspace connection unavailable. Saved files remain accessible." : null)} /></div>
    <div role="tabpanel" id={`workspace-tab-${tab}`} className="min-h-0 flex-1 overflow-auto"><Suspense fallback={<div className="p-8"><Spinner label="Opening workspace…" /></div>}>
      {tab === "files" ? selected ? <div className="flex h-full min-h-0 flex-col"><button type="button" className="flex items-center gap-1 p-3 text-xs text-dim" onClick={() => setFileId(null)}><ArrowLeft size={13} />All files</button><WorkspaceFileView key={selected.id} file={selected} workspaceId={workspaceId} runtime={runtime.data} canEdit={!!owns} canWrite={canWrite} onUse={onUse} onFeedback={onFeedback} /></div> : <div className="p-3"><p className="mb-3 break-all font-mono text-xs text-dim">{runtime.data?.cwd ?? "/workspace"}</p><WorkspaceBrowser workspaceId={workspaceId} conversationId={conversationId} canPublish={canWrite} onOpen={publish} /><h3 className="mb-2 text-xs font-semibold text-dim">Saved files</h3><ul className="space-y-2">{files.map((file) => <li key={file.id}><button type="button" onClick={() => setFileId(file.id)} className="flex w-full items-center gap-3 rounded-lg border border-line p-3 text-left hover:bg-hover"><File size={18} className="shrink-0 text-dim" /><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{file.name}</span><span className="text-xs text-dim">v{file.version} · {fileSize(file.size_bytes)} · {file.kind}</span>{file.error ? <span className="block text-xs text-danger">{file.error}</span> : null}</span></button></li>)}</ul>{hasMoreFiles ? <button type="button" disabled={loadingMoreFiles} onClick={onLoadMoreFiles} className="mt-3 w-full rounded-lg border border-line p-2 text-xs">{loadingMoreFiles ? "Loading…" : "Load more saved files"}</button> : null}{!files.length ? <p className="py-8 text-center text-sm text-dim">Attach a file or publish one from this chat’s workspace.</p> : null}{canWrite ? <form className="mt-4 flex gap-2" onSubmit={(event) => { event.preventDefault(); void publish(); }}><input aria-label="Workspace file path" value={path} onChange={(event) => setPath(event.target.value)} placeholder="path/to/file" className="min-w-0 flex-1 rounded-lg border border-line bg-surface p-2 text-xs" /><button type="submit" disabled={busy || !path.trim()} className="rounded-lg border border-line p-2 text-xs disabled:opacity-40">Publish file</button></form> : null}</div>
        : tab === "terminal" ? <WorkspaceTerminal workspaceId={workspaceId} conversationId={conversationId} runtime={runtime.data} isAdmin={isAdmin} userId={userId} />
          : tab === "preview" ? <WorkspacePreview workspaceId={workspaceId} conversationId={conversationId} runtime={runtime.data} files={files} canWrite={canWrite} />
            : <WorkspaceChanges workspaceId={workspaceId} conversationId={conversationId} runtime={runtime.data} canEdit={!!owns} onFeedback={onFeedback} />}
    </Suspense></div>
  </aside>;
}
