"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAgents, useConnections, useModelProfiles } from "@/lib/hooks";
import type { ChatDraft, ContextReference } from "@/lib/agentic-chat";
import type { ManagedFile, WorkspaceProject } from "@/lib/workspace-files";
import { AttachButton } from "./attachments";

export function TurnControls({ workspaceId, draft, onChange, live, disabled, onFiles, files, onUseFile }: { workspaceId: string; draft: ChatDraft; onChange: (change: Partial<ChatDraft>) => void; live: boolean; disabled: boolean; onFiles: (files: FileList) => void; files: ManagedFile[]; onUseFile: (file: ManagedFile) => void }) {
  const profiles = useModelProfiles(workspaceId);
  const [contextOpen, setContextOpen] = useState(false), [search, setSearch] = useState("");
  const [dismissedMention, setDismissedMention] = useState<string | null>(null);
  const mention = /(?:^|\s)@([^\s]*)$/.exec(draft.text);
  const contextVisible = contextOpen || (!!mention && dismissedMention !== draft.text);
  const searchTerm = contextOpen ? search : mention?.[1] ?? "";
  const withoutMention = mention ? draft.text.slice(0, draft.text.length - mention[1].length - 1) : draft.text;
  const closeContext = () => { setContextOpen(false); setDismissedMention(draft.text); };
  const agents = useAgents(workspaceId), apps = useConnections(workspaceId);
  const projects = useQuery({ queryKey: ["workspace-projects", workspaceId], queryFn: () => api<WorkspaceProject[]>(`/api/v1/workspaces/${workspaceId}/projects`), enabled: contextVisible });
  const references: ContextReference[] = [
    ...(agents.data ?? []).map((item) => ({ kind: "agent", id: item.id, label: item.name })),
    ...(apps.data ?? []).map((item) => ({ kind: "app", id: item.id, label: item.name })),
    ...(projects.data ?? []).map((item) => ({ kind: "project", id: item.id, label: item.name })),
  ].filter((item) => item.label?.toLowerCase().includes(searchTerm.toLowerCase()));
  const selectReference = (reference: ContextReference) => { onChange({ text: withoutMention, context_refs: [...draft.context_refs.filter((item) => !(item.id === reference.id && item.kind === reference.kind)), reference] }); closeContext(); };
  return <div className="relative flex min-w-0 flex-1 flex-wrap items-center gap-1">
    <AttachButton onFiles={onFiles} disabled={disabled} />
    <button type="button" title="Add context" aria-label="Add context" aria-expanded={contextVisible} onClick={() => { if (contextVisible) closeContext(); else { setSearch(""); setContextOpen(true); } }} className="h-9 w-9 shrink-0 rounded-lg text-sm text-dim hover:bg-hover">@</button>
    <select aria-label="This turn's mode" title="Mode for this turn" value={draft.execution_mode} onChange={(event) => onChange({ execution_mode: event.target.value as ChatDraft["execution_mode"] })} disabled={disabled || (live && draft.delivery !== "queue")} className="h-9 max-w-24 rounded-lg bg-transparent px-1 text-xs text-dim disabled:opacity-50"><option value="ask">Ask</option><option value="plan">Plan</option><option value="act">Act</option></select>
    <select aria-label="This turn's model" value={draft.model_profile_id ?? ""} onChange={(event) => onChange({ model_profile_id: event.target.value || undefined })} disabled={disabled || (live && draft.delivery !== "queue")} className="h-9 min-w-0 max-w-36 rounded-lg bg-transparent px-1 text-xs text-dim disabled:opacity-50"><option value="">Agent’s model</option>{profiles.data?.map((profile) => <option key={profile.id} value={profile.id}>{profile.display_name}</option>)}</select>
    {live ? <select aria-label="Message delivery" value={draft.delivery === "auto" ? "steer" : draft.delivery} onChange={(event) => onChange({ delivery: event.target.value as ChatDraft["delivery"] })} className="h-9 max-w-28 rounded-lg bg-transparent px-1 text-xs text-dim"><option value="steer">Steer now</option><option value="queue">Queue next</option></select> : null}
    {contextVisible ? <div role="dialog" aria-label="Choose context" onKeyDown={(event) => { if (event.key === "Escape") closeContext(); }} className="absolute bottom-full left-0 z-30 mb-2 max-h-80 w-[min(22rem,80vw)] overflow-auto rounded-xl border border-line bg-surface p-2 shadow-lg"><input autoFocus={contextOpen} aria-label="Search context" placeholder="Files, projects, apps, agents…" value={searchTerm} onChange={(event) => { setContextOpen(true); setSearch(event.target.value); }} className="mb-2 w-full rounded-lg border border-line bg-surface p-2 text-sm" />{files.filter((file) => file.name.toLowerCase().includes(searchTerm.toLowerCase())).map((file) => <button type="button" key={file.id} className="block w-full rounded p-2 text-left text-xs hover:bg-hover" onClick={() => { onChange({ text: withoutMention }); onUseFile(file); closeContext(); }}>File · {file.name} · v{file.version}</button>)}{references.map((item) => <button type="button" key={`${item.kind}:${item.id}`} className="block w-full rounded p-2 text-left text-xs hover:bg-hover" onClick={() => selectReference(item)}><span className="capitalize text-faint">{item.kind}</span> · {item.label}</button>)}<button type="button" onClick={closeContext} className="mt-1 w-full rounded p-2 text-xs text-dim">Close</button></div> : null}
  </div>;
}
