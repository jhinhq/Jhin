"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, ConfirmDialog, ErrorNote, Spinner } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { scheduleBase, scheduleTime, WEEKDAYS, type WorkSchedule, type ScheduleOccurrence } from "@/lib/schedules";
import { ScheduleForm } from "./schedule-form";

function Occurrences({ workspaceId, schedule }: { workspaceId: string; schedule: WorkSchedule }) {
  const [offset, setOffset] = useState(0);
  const query = useQuery({ queryKey: ["schedule-occurrences", workspaceId, schedule.id, offset], queryFn: () => api<{ items: ScheduleOccurrence[]; total: number }>(`${scheduleBase(workspaceId)}/${schedule.id}/occurrences`, { params: { limit: 50, offset } }), refetchInterval: 15000 });
  return <div className="mt-3 border-t border-line pt-3"><h4 className="text-sm font-medium">Run history</h4>{query.isPending ? <Spinner /> : null}<ErrorNote message={query.isError ? "Couldn't load run history." : null} />{query.data?.total === 0 ? <p className="mt-2 text-sm text-dim">No occurrences yet.</p> : null}<ol className="mt-2 space-y-2">{query.data?.items.map((run) => <li key={run.id} className="flex flex-wrap items-center gap-3 text-xs"><span>{scheduleTime(run.scheduled_for, schedule.timezone)}</span><span>{run.status.replaceAll("_", " ")}</span>{run.error_code ? <span className="text-danger">{run.error_code}</span> : null}{run.task_id ? <Link className="text-accent-strong underline" href={`/tasks/${run.task_id}`}>View task</Link> : null}</li>)}</ol>{query.data && query.data.total > 50 ? <div className="mt-3 flex gap-2"><Button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous runs</Button><Button disabled={offset + 50 >= query.data.total} onClick={() => setOffset(offset + 50)}>More runs</Button></div> : null}</div>;
}
export function SchedulesPanel({ workspaceId, agents, canWrite, agentId }: { workspaceId: string; agents: { id: string; name: string }[]; canWrite: boolean; agentId?: string }) {
  const client = useQueryClient(), [offset, setOffset] = useState(0);
  const [editing, setEditing] = useState<{ item?: WorkSchedule } | null>(null), [deleting, setDeleting] = useState<WorkSchedule | null>(null);
  const [history, setHistory] = useState<string | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const key = ["work-schedules", workspaceId];
  const query = useQuery({ queryKey: [...key, agentId, offset], queryFn: () => api<{ items: WorkSchedule[]; total: number }>(scheduleBase(workspaceId), { params: { agent_id: agentId, limit: 50, offset } }), refetchInterval: 15000 });
  const refresh = () => { void client.invalidateQueries({ queryKey: key }); };
  const change = async (item: WorkSchedule, remove = false) => {
    if (busy) return; setBusy(true); setError(null);
    try { await api(`${scheduleBase(workspaceId)}/${item.id}`, remove ? { method: "DELETE", params: { expected_version: item.version } } : { method: "PATCH", body: { expected_version: item.version, enabled: !item.enabled } }); setDeleting(null); refresh(); }
    catch (failure) { setDeleting(null); setError(failure instanceof ApiError && failure.status === 409 ? "This schedule changed. Reload to use its current version." : "Couldn't update the schedule. Check your access and try again."); }
    finally { setBusy(false); }
  };
  return <section className="space-y-4" aria-label="Schedules"><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-display text-lg font-semibold">Schedules</h2><p className="text-sm text-dim">Recurring work with an explicit time, timezone, and standing brief.</p></div><div className="flex gap-2"><Button size="sm" onClick={refresh}>Reload schedules</Button>{canWrite ? <Button size="sm" disabled={!agents.length} onClick={() => setEditing({})}>New schedule</Button> : null}</div></div>
    <ErrorNote message={error || (query.isError ? "Couldn't load schedules. Reload to try again." : null)} />{query.isPending ? <Spinner label="Loading schedules…" /> : null}{query.data?.total === 0 ? <p className="rounded-xl border border-dashed border-line p-4 text-sm text-dim">No schedules yet. Add one here or ask an agent to set up recurring work.</p> : null}
    <ul className="grid items-start gap-4 lg:grid-cols-2">{query.data?.items.map((item) => <li key={item.id} className="min-w-0 space-y-3 rounded-xl border border-line bg-surface p-4"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="break-words font-medium">{item.name}</h3><Badge tone={item.enabled ? "ok" : "neutral"}>{item.enabled ? "Scheduled" : "Paused"}</Badge></div><p className="text-xs text-dim">{agents.find((agent) => agent.id === item.agent_id)?.name ?? "Agent"} · {item.weekdays.map((day) => WEEKDAYS[day]).join(", ")} · {item.local_time} {item.timezone}</p><p className="whitespace-pre-wrap break-words text-sm">{item.brief}</p><p className="text-xs text-dim">Next: {item.enabled ? scheduleTime(item.next_run_at, item.timezone) : "Paused"}{item.last_status ? ` · Last run: ${item.last_status.replaceAll("_", " ")}` : ""}</p><div className="flex flex-wrap gap-2">{canWrite ? <><Button size="sm" disabled={busy} onClick={() => void change(item)}>{item.enabled ? "Pause schedule" : "Resume schedule"}</Button><Button size="sm" onClick={() => setEditing({ item })}>Edit schedule</Button><Button size="sm" variant="ghost" onClick={() => setDeleting(item)}>Delete schedule</Button></> : null}<Button size="sm" aria-expanded={history === item.id} onClick={() => setHistory(history === item.id ? null : item.id)}>Run history</Button></div>{history === item.id ? <Occurrences workspaceId={workspaceId} schedule={item} /> : null}</li>)}</ul>
    {query.data && query.data.total > 50 ? <div className="flex gap-2"><Button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous schedules</Button><Button disabled={offset + 50 >= query.data.total} onClick={() => setOffset(offset + 50)}>More schedules</Button></div> : null}
    {editing ? <ScheduleForm key={editing.item?.id ?? "new"} workspaceId={workspaceId} agents={agentId ? agents.filter((agent) => agent.id === agentId) : agents} item={editing.item} onClose={() => setEditing((current)=>current===editing ? null : current)} onSaved={refresh} /> : null}
    <ConfirmDialog open={!!deleting} title="Delete schedule?" body="Future runs will stop. Existing run history remains available, and any task already running will continue." confirmLabel="Delete schedule" busy={busy} onClose={() => setDeleting(null)} onConfirm={() => { if (deleting) void change(deleting, true); }} />
  </section>;
}
