"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, ErrorNote, Field, Input, Select, Spinner } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { useAgents, useTeams } from "@/lib/hooks";
import { useWorkspace } from "@/lib/workspace-context";

const CLASSES = {
  editorial_style: "Editorial style",
  recurring_preference: "Recurring preferences",
  editorial_lesson: "Approved editorial lessons",
  company_fact: "Company facts",
} as const;
type CaptureClass = keyof typeof CLASSES;
interface CapturePolicy {
  id: string;
  scope: "team" | "workspace";
  scope_id: string;
  actor_ids_json: string[];
  allowed_classes_json: CaptureClass[];
  allowed_source_agent_ids_json: string[];
  source_conversation_id?: string | null;
  effective_from: string;
  expires_at: string | null;
  revoked_at: string | null;
}

/** UI visibility is convenience; the API independently requires a human admin. */
export function MemoryCaptureSettings() {
  const { workspace, can } = useWorkspace();
  return can("admin") ? <CaptureEditor key={workspace.workspace_id} workspaceId={workspace.workspace_id} /> : null;
}

function toggle<T>(items: T[], item: T): T[] {
  return items.includes(item) ? items.filter((value) => value !== item) : [...items, item];
}

function CaptureEditor({ workspaceId }: { workspaceId: string }) {
  const teams = useTeams(workspaceId);
  const agents = useAgents(workspaceId);
  const path = `/api/v1/workspaces/${workspaceId}/memory-capture-policies`;
  const policies = useQuery({ queryKey: ["memory-capture-policies", workspaceId], queryFn: () => api<CapturePolicy[]>(path) });
  const [scope, setScope] = useState<"team" | "workspace">("team");
  const [teamId, setTeamId] = useState("");
  const [actors, setActors] = useState<string[]>([]);
  const [classes, setClasses] = useState<CaptureClass[]>([]);
  const [reviewers, setReviewers] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("");
  const [saved, setSaved] = useState(false);
  const lessons = classes.includes("editorial_lesson");
  const valid = actors.length > 0 && classes.length > 0 && (scope === "workspace" || teamId !== "") && (!lessons || (scope === "team" && reviewers.length > 0));
  const create = useMutation({
    mutationFn: () => api<CapturePolicy>(path, { method: "POST", body: {
      scope, scope_id: scope === "workspace" ? workspaceId : teamId,
      actor_ids: actors, allowed_classes: classes, allowed_source_agent_ids: lessons ? reviewers : [],
      ...(expiry ? { expires_at: new Date(expiry).toISOString() } : {}),
    } }),
    onSuccess: () => { setSaved(true); setClasses([]); void policies.refetch(); },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api<CapturePolicy>(`${path}/${id}/revoke`, { method: "POST" }),
    onSuccess: () => void policies.refetch(),
  });
  const names = new Map((agents.data ?? []).map((agent) => [agent.id, agent.name]));
  const teamNames = new Map((teams.data ?? []).map((team) => [team.id, team.name]));
  const loading = policies.isPending || teams.isPending || agents.isPending;
  const error = policies.error ?? teams.error ?? agents.error ?? create.error ?? revoke.error;

  return <Card as="section">
    <h2 className="mb-1 font-display text-base font-semibold">Shared memory capture</h2>
    <p className="mb-4 text-sm text-dim">Let selected agents remember eligible future information for an exact team or the company, without asking again for each fact. Personal feedback and earlier private conversations stay excluded.</p>
    {error ? <ErrorNote message={errorText(error, "Memory capture settings could not be updated.")} /> : null}
    {loading ? <Spinner /> : <>
      <ul className="mb-5 space-y-3" aria-label="Capture policies">
        {(policies.data ?? []).map((policy) => {
          const expired = policy.expires_at !== null && new Date(policy.expires_at).getTime() <= policies.dataUpdatedAt;
          const state = policy.revoked_at ? "Revoked" : expired ? "Expired" : "Active";
          return <li key={policy.id} className="rounded-xl border border-line p-3 text-sm">
            <p className="font-medium">{policy.scope === "workspace" ? "Company" : teamNames.get(policy.scope_id) ?? policy.scope_id} · {state}</p>
            <p>{policy.allowed_classes_json.map((value) => CLASSES[value]).join(", ")}</p>
            <p className="text-dim">Agents: {policy.actor_ids_json.map((id) => names.get(id) ?? id).join(", ")}</p>
            {policy.allowed_source_agent_ids_json.length ? <p className="text-dim">Reviewers: {policy.allowed_source_agent_ids_json.map((id) => names.get(id) ?? id).join(", ")}</p> : null}
            <p className="text-xs text-faint">From {new Date(policy.effective_from).toLocaleString()}{policy.expires_at ? ` · Until ${new Date(policy.expires_at).toLocaleString()}` : " · Until revoked"}{policy.source_conversation_id ? " · One source conversation" : " · Future eligible conversations"}</p>
            {state === "Active" ? <Button type="button" size="sm" variant="ghost" disabled={revoke.isPending} onClick={() => revoke.mutate(policy.id)}>Revoke policy</Button> : null}
          </li>;
        })}
      </ul>
      <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); if (valid) create.mutate(); }}>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Memory audience"><Select value={scope} onChange={(event) => { setScope(event.target.value as "team" | "workspace"); setSaved(false); }}>
            <option value="team">A specific team</option><option value="workspace">Company</option>
          </Select></Field>
          {scope === "team" ? <Field label="Destination team"><Select value={teamId} onChange={(event) => setTeamId(event.target.value)}>
            <option value="">Choose a team</option>{(teams.data ?? []).map((team) => <option key={team.id} value={team.id}>{team.name}</option>)}
          </Select></Field> : null}
        </div>
        <fieldset><legend className="mb-2 text-sm font-medium">Information to remember</legend>
          <div className="grid gap-2 sm:grid-cols-2">{Object.entries(CLASSES).map(([value, label]) => <label key={value} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={classes.includes(value as CaptureClass)} onChange={() => { setClasses((current) => toggle(current, value as CaptureClass)); setSaved(false); }} />{label}
          </label>)}</div>
        </fieldset>
        <fieldset><legend className="mb-2 text-sm font-medium">Agents who can remember</legend>
          <div className="grid gap-2 sm:grid-cols-2">{(agents.data ?? []).map((agent) => <label key={agent.id} className="flex items-center gap-2 text-sm">
            <input type="checkbox" aria-label={`${agent.name} can remember`} checked={actors.includes(agent.id)} onChange={() => setActors((current) => toggle(current, agent.id))} />{agent.name}
          </label>)}</div>
        </fieldset>
        {lessons ? <fieldset><legend className="mb-2 text-sm font-medium">Approved reviews to learn from</legend>
          <p className="mb-2 text-xs text-dim">Lessons stay within the assignment&apos;s team and require a current approved review by a selected reviewer.</p>
          {scope === "workspace" ? <p role="alert" className="text-sm text-danger">Choose a team audience for editorial lessons.</p> : null}
          <div className="grid gap-2 sm:grid-cols-2">{(agents.data ?? []).map((agent) => <label key={agent.id} className="flex items-center gap-2 text-sm">
            <input type="checkbox" aria-label={`Learn from ${agent.name}'s reviews`} checked={reviewers.includes(agent.id)} onChange={() => setReviewers((current) => toggle(current, agent.id))} />{agent.name}
          </label>)}</div>
        </fieldset> : null}
        <Field label="Expiry (optional)"><Input type="datetime-local" value={expiry} onChange={(event) => setExpiry(event.target.value)} /></Field>
        <p className="text-xs text-dim">Covers your future statements and, when selected, approved editorial reviews. Starts when saved. Existing memories remain available if you revoke future capture.</p>
        <Button type="submit" variant="primary" disabled={!valid || create.isPending}>{create.isPending ? "Saving…" : "Save capture policy"}</Button>
        {saved ? <p role="status" className="text-sm text-dim">Capture policy saved for future information.</p> : null}
      </form>
    </>}
  </Card>;
}
