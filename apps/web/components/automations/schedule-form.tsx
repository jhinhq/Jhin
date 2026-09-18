"use client";
import { useState } from "react";
import { Button, Dialog, ErrorNote, Field, Input, Select, Textarea } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { scheduleBase, WEEKDAYS, type WorkSchedule } from "@/lib/schedules";

export function ScheduleForm({ workspaceId, agents, item, onClose, onSaved }: { workspaceId: string; agents: { id: string; name: string }[]; item?: WorkSchedule; onClose: () => void; onSaved: () => void }) {
  const [days, setDays] = useState(item?.weekdays ?? [0,1,2,3,4,5,6]);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null), [stale, setStale] = useState(false);
  const [requestId] = useState(() => crypto.randomUUID());
  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (busy || stale) return;
    const form = new FormData(event.currentTarget);
    const timezone = String(form.get("timezone") ?? "").trim();
    try { new Intl.DateTimeFormat("en", { timeZone: timezone }).format(); } catch { setError("Enter an IANA timezone such as America/Los_Angeles."); return; }
    if (!days.length) { setError("Choose at least one day."); return; }
    const body = { name: String(form.get("name")).trim(), brief: String(form.get("brief")).trim(), timezone, local_time: String(form.get("local_time")), weekdays: days, ...(item ? { expected_version: item.version } : { agent_id: String(form.get("agent_id")), enabled: true, idempotency_key: requestId }) };
    setBusy(true); setError(null);
    try { await api(`${scheduleBase(workspaceId)}${item ? `/${item.id}` : ""}`, { method: item ? "PATCH" : "POST", body }); onSaved(); onClose(); }
    catch (failure) { const conflict = failure instanceof ApiError && failure.status === 409; setStale(conflict); setError(conflict ? "This schedule changed. Close and reload before editing again." : failure instanceof ApiError ? failure.detail : "Couldn't save this schedule. Try again."); }
    finally { setBusy(false); }
  };
  return <Dialog open wide title={item ? "Edit schedule" : "New schedule"} onClose={onClose} footer={<div className="flex justify-end gap-2"><Button onClick={onClose}>Cancel</Button><Button form="schedule-form" type="submit" variant="primary" disabled={busy || stale}>{busy ? "Saving…" : "Save schedule"}</Button></div>}>
    <form id="schedule-form" className="space-y-4" onSubmit={(event) => void save(event)}>
      <ErrorNote message={error} />
      <Field label="Name"><Input name="name" required maxLength={200} defaultValue={item?.name} /></Field>
      {!item ? <Field label="Agent"><Select name="agent_id" required defaultValue={agents[0]?.id}>{agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}</Select></Field> : null}
      <Field label="Standing brief" hint="Include the audience, required output, and any approval or publishing limits. Every run receives this brief."><Textarea aria-label="Standing brief" name="brief" rows={5} required defaultValue={item?.brief} /></Field>
      <div className="grid gap-4 sm:grid-cols-2"><Field label="Local time"><Input name="local_time" type="time" required defaultValue={item?.local_time ?? "09:00"} /></Field><Field label="Timezone"><Input name="timezone" required placeholder="America/Los_Angeles" defaultValue={item?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone} /></Field></div>
      <fieldset><legend className="mb-2 text-sm font-medium">Days</legend><div className="flex flex-wrap gap-2">{WEEKDAYS.map((day, index) => <label key={day} className="flex min-h-10 items-center gap-2 rounded-lg border border-line px-3 text-sm"><input type="checkbox" checked={days.includes(index)} onChange={(event) => setDays((current) => event.target.checked ? [...current, index].sort() : current.filter((value) => value !== index))} />{day}</label>)}</div></fieldset>
      <p className="text-xs leading-relaxed text-dim">Daylight-saving gaps skip that date; repeated local times run once. A new occurrence is skipped while the preceding run is still active. Pausing or editing does not stop a task already running.</p>
    </form>
  </Dialog>;
}
