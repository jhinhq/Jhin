"use client";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "@/lib/api";
import { conversationPath, type ConversationRuntime, type RuntimeSession } from "@/lib/workspace-runtime";
import type { ManagedFile } from "@/lib/workspace-files";
import { ErrorNote } from "@/components/ui";
import { usePreviewReadiness } from "@/lib/preview-readiness";

export function WorkspacePreview({ workspaceId, conversationId, files, runtime, canWrite }: { workspaceId: string; conversationId: string; files: ManagedFile[]; runtime?: ConversationRuntime; canWrite: boolean }) {
  const queryClient = useQueryClient();
  const [fileId, setFileId] = useState(""), [framework, setFramework] = useState("static"), [command, setCommand] = useState(""), [port, setPort] = useState("3000");
  const [url, setUrl] = useState<string | null>(null), [selected, setSelected] = useState<RuntimeSession | null>(null), [error, setError] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const base = conversationPath(workspaceId, conversationId);
  const readiness = usePreviewReadiness(url);
  const open = async (session: RuntimeSession) => {
    const ticket = await api<{ url: string }>(`${base}/previews/${session.id}/ticket`, { method: "POST" });
    const checked = new URL(ticket.url, window.location.href);
    if (checked.origin !== window.location.origin || !checked.pathname.startsWith("/runtime/")) throw new Error("Invalid preview gateway");
    setSelected(session); setUrl(checked.href);
  };
  const action = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await operation(); await queryClient.invalidateQueries({ queryKey: ["conversation-runtime", workspaceId, conversationId] }); }
    catch (failure) { setError(errorText(failure, "Couldn't update the preview.")); }
    finally { setBusy(false); }
  };
  const create = () => action(async () => {
    const file = files.find((item) => item.id === fileId);
    const result = await api<RuntimeSession>(`${base}/previews`, { method: "POST", body: { file_id: fileId, revision_id: file?.current_revision_id, framework, ...(command.trim() ? { command } : {}), ...(framework !== "static" ? { port: Number(port) } : {}) } });
    await open(result);
  });
  return <div className="flex h-full min-h-0 flex-col gap-3 p-3">
    <h3 className="text-sm font-semibold">Interactive preview</h3>
    {canWrite ? <form onSubmit={(event) => { event.preventDefault(); void create(); }} className="flex flex-wrap items-center gap-2 text-xs"><select aria-label="Preview source" value={fileId} onChange={(event) => setFileId(event.target.value)} className="min-w-0 max-w-full flex-1 rounded-lg border border-line bg-surface p-2"><option value="">Choose published source…</option>{files.filter((file) => file.status === "ready").map((file) => <option key={file.id} value={file.id}>{file.name} · v{file.version}</option>)}</select><select aria-label="Preview framework" value={framework} onChange={(event) => setFramework(event.target.value)} className="rounded-lg border border-line bg-surface p-2"><option value="static">HTML / JavaScript</option><option value="vite">Vite / React</option><option value="next">Next.js</option><option value="http">HTTP app</option></select>{framework !== "static" ? <><input aria-label="Development command" placeholder="Development command (optional)" value={command} onChange={(event) => setCommand(event.target.value)} className="min-w-0 flex-1 rounded-lg border border-line bg-surface p-2" /><input aria-label="Development port" type="number" min="1024" max="65535" value={port} onChange={(event) => setPort(event.target.value)} className="w-20 rounded-lg border border-line bg-surface p-2" /></> : null}<button type="submit" disabled={!fileId || busy} className="rounded-lg border border-line px-3 py-2 disabled:opacity-40">Open preview</button></form> : null}
    <ErrorNote message={error} />
    <ErrorNote message={readiness.error} />
    {(runtime?.previews ?? []).length ? <ul className="flex flex-wrap gap-2">{runtime!.previews.map((session) => <li key={session.id}><button type="button" disabled={busy} onClick={() => void action(() => open(session))} className="rounded-lg border border-line p-2 text-xs">{session.kind} · {session.status}</button></li>)}</ul> : null}
    {selected ? <div className="flex flex-wrap items-center gap-2 text-xs"><span className="min-w-0 truncate text-dim">Revision {selected.revision_id ?? "published source"}</span><button type="button" disabled={busy} onClick={() => void action(() => open(selected))} className="rounded border border-line p-2">Reload preview</button>{canWrite ? <><button type="button" disabled={busy} className="rounded border border-line p-2" onClick={() => void action(async () => { const session = await api<RuntimeSession>(`${base}/previews/${selected.id}/refresh`, { method: "POST" }); await open(session); })}>Refresh from changes</button><button type="button" disabled={busy} className="rounded border border-line p-2" onClick={() => void action(async () => { const session = await api<RuntimeSession>(`${base}/previews/${selected.id}/restart`, { method: "POST" }); await open(session); })}>Restart</button><button type="button" disabled={busy} className="rounded border border-line p-2" onClick={() => void action(async () => { await api(`${base}/previews/${selected.id}/stop`, { method: "POST" }); setUrl(null); })}>Stop</button></> : null}</div> : null}
    {readiness.waiting ? <p role="status" className="py-10 text-center text-sm text-dim">Starting preview… Installing dependencies and waiting for the app. You can inspect logs or stop it below.</p> : null}
    {url && readiness.ready ? <iframe title="App preview" src={url} sandbox="allow-scripts allow-forms allow-downloads" referrerPolicy="no-referrer" className="min-h-80 w-full flex-1 rounded-lg border border-line bg-white" /> : !url ? <p className="py-10 text-center text-sm text-dim">Open a published HTML artifact or start an app preview.</p> : null}
    {selected ? <details open={readiness.waiting || undefined} className="text-xs"><summary className="cursor-pointer text-dim">Preview logs</summary><pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-raised p-2">{runtime?.previews.find((item) => item.id === selected.id)?.output || selected.output || "No output yet."}</pre></details> : null}
  </div>;
}
