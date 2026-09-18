"use client";
import { useState } from "react";
import { api, errorText } from "@/lib/api";
import type { Task } from "@/lib/types";
import { ErrorNote } from "@/components/ui";
import { mayContainSecret } from "@/lib/private-input";

export function WorkQueue({ workspaceId, conversationId, tasks, canWrite, onChange }: { workspaceId: string; conversationId: string; tasks: Task[]; canWrite: boolean; onChange: () => void }) {
  const [editing, setEditing] = useState<string | null>(null), [text, setText] = useState(""), [error, setError] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const queued = tasks.filter((task) => task.state === "queued"), delegated = tasks.filter((task) => task.parent_task_id);
  if (!queued.length && !delegated.length) return null;
  const change = async (taskId: string, method: "PATCH" | "DELETE") => {
    const submitted = text;
    if (mayContainSecret(submitted)) setText("");
    setBusy(true); setError(null);
    try { await api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/queued/${taskId}`, { method, ...(method === "PATCH" ? { body: { text: submitted } } : {}) }); setText(""); setEditing(null); onChange(); }
    catch (failure) { setError(mayContainSecret(submitted) ? "Couldn't confirm this queued update. Refresh its status before retrying." : errorText(failure, "This queued turn may have started. Refresh its status.")); }
    finally { setBusy(false); }
  };
  return <details className="border-b border-line bg-raised px-4 py-2 text-xs"><summary className="cursor-pointer text-dim">{queued.length ? `${queued.length} queued turn${queued.length === 1 ? "" : "s"}` : ""}{queued.length && delegated.length ? " · " : ""}{delegated.length ? `${delegated.length} colleague task${delegated.length === 1 ? "" : "s"}` : ""}</summary><ErrorNote message={error} /><ul className="mt-2 space-y-2">{[...new Map([...queued, ...delegated].map((task) => [task.id, task])).values()].map((task) => <li key={task.id} className="rounded-lg border border-line bg-surface p-2"><div className="flex flex-wrap items-center gap-2"><a href={`/tasks/${task.id}`} className="min-w-0 flex-1 truncate text-accent-strong">{task.title}</a><span>{task.state.replaceAll("_", " ")}</span>{canWrite && task.state === "queued" ? <><button type="button" disabled={busy} onClick={() => { setEditing(task.id); setText(task.description || task.title); }} className="rounded border border-line px-2 py-1">Edit</button><button type="button" disabled={busy} onClick={() => void change(task.id, "DELETE")} className="rounded border border-line px-2 py-1">Remove</button></> : null}</div>{editing === task.id ? <form className="mt-2 flex gap-2" onSubmit={(event) => { event.preventDefault(); void change(task.id, "PATCH"); }}><textarea aria-label="Queued message" value={text} onChange={(event) => setText(event.target.value)} className="min-w-0 flex-1 rounded border border-line bg-surface p-2" /><button type="submit" disabled={busy || !text.trim()} className="rounded border border-line px-2">Save</button></form> : null}</li>)}</ul></details>;
}
