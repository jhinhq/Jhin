"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ActivityDetail, ConversationItem } from "@/lib/agentic-chat";
import type { RuntimeSession } from "@/lib/workspace-runtime";

/** Human terminal work is kept distinct from the agent's tool executions. */
export function SessionActivityCard({ item, base, detail }: { item: ConversationItem; base?: string; detail: ActivityDetail }) {
  const failed = item.status === "failed" || !!item.data.error;
  const [open, setOpen] = useState(detail === "detailed" || failed);
  const terminal = item.data.kind === "terminal";
  const session = useQuery({ queryKey: ["runtime-session-log", base, item.id, item.revision], queryFn: () => api<RuntimeSession>(`${base}/terminals/${item.data.id}`), enabled: !!base && terminal && open, refetchInterval: ["running", "starting", "stopping"].includes(item.status) && open ? 3000 : false });
  return <article className={`min-w-0 overflow-hidden rounded-xl border bg-surface ${failed ? "border-danger/30" : "border-line"}`} aria-label={terminal ? "Human terminal session" : "Preview session"}>
    <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 p-3 text-left text-xs"><span className="min-w-0 flex-1"><span className="font-semibold">{terminal ? "Interactive terminal" : "App preview"}</span><span className="ml-2 text-dim">{item.actor.name ?? "Workspace member"}</span></span><span className={failed ? "text-danger" : "text-dim"}>{item.status}</span></button>
    {open ? <div className="space-y-2 border-t border-line p-3 text-xs"><p className="text-dim">Opened by {item.actor.name ?? "a workspace member"} · {item.data.network === "internet" ? "Internet enabled" : "Internet blocked"}</p>{typeof item.data.command === "string" && item.data.command ? <pre className="overflow-auto whitespace-pre-wrap font-mono">{item.data.command}</pre> : null}{typeof item.data.error === "string" ? <p className="text-danger">{item.data.error}</p> : null}{session.data ? <><p className="font-mono text-dim">{session.data.cwd}{session.data.exit_code !== null ? ` · Exit ${session.data.exit_code}` : ""}</p><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-[#15141b] p-3 text-[#eeeaf8]">{session.data.output || "No retained output."}</pre><p className="text-faint">Retained session output is bounded; earlier output may have expired.</p></> : terminal ? <p className="text-dim">{session.isError ? "Retained output is currently unavailable." : "Loading retained output…"}</p> : <p className="text-dim">Open Workspace → Preview for controls and logs.</p>}</div> : null}
  </article>;
}
