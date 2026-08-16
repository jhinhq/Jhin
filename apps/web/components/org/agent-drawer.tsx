"use client";

/** Agent profile drawer (plan 17.5): header + Overview / Instructions /
 * Settings tabs. Later-phase tabs are visible but disabled with their phase. */

import { useMutation } from "@tanstack/react-query";
import { Pause, Play, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  ErrorNote,
  Field,
  Input,
  Select,
  Spinner,
  StatusDot,
  Textarea,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useAgent, useInvalidateOrg } from "@/lib/hooks";
import type { Agent, AutonomyLevel, OrgAgentNode, OrgTeamNode } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "instructions", label: "Instructions" },
  { id: "tools", label: "Tools & Access", phase: "Phase 4" },
  { id: "model", label: "Model", phase: "Phase 3" },
  { id: "tasks", label: "Tasks", phase: "Phase 3" },
  { id: "runs", label: "Runs", phase: "Phase 3" },
  { id: "memory", label: "Memory", phase: "Phase 6" },
  { id: "activity", label: "Activity", phase: "Phase 3" },
  { id: "settings", label: "Settings" },
] as const;

type TabId = (typeof TABS)[number]["id"];

function OverviewRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line py-2.5 text-sm last:border-0">
      <span className="shrink-0 text-dim">{label}</span>
      <span className="text-right">{value}</span>
    </div>
  );
}

export function AgentDrawer({
  agentId,
  onClose,
  teams,
  agents,
}: {
  agentId: string;
  onClose: () => void;
  teams: OrgTeamNode[];
  agents: OrgAgentNode[];
}) {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const agentQuery = useAgent(workspaceId, agentId);
  const invalidate = useInvalidateOrg(workspaceId);
  // The drawer is mounted with key={agentId}, so tab state resets per agent.
  const [tab, setTab] = useState<TabId>("overview");

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const agent = agentQuery.data;
  const team = teams.find((t) => t.id === agent?.team_id);
  const manager = agents.find((a) => a.id === agent?.manager_agent_id);

  const toggle = useMutation({
    mutationFn: () =>
      api<Agent>(
        `/api/v1/workspaces/${workspaceId}/agents/${agentId}/${
          agent?.status === "paused" ? "resume" : "pause"
        }`,
        { method: "POST" },
      ),
    onSuccess: invalidate,
  });

  const remove = useMutation({
    mutationFn: () =>
      api<void>(`/api/v1/workspaces/${workspaceId}/agents/${agentId}`, { method: "DELETE" }),
    onSuccess: () => {
      invalidate();
      onClose();
    },
  });

  return (
    <div className="fixed inset-0 z-40">
      <button aria-label="Close drawer" className="absolute inset-0 bg-black/50" onClick={onClose} />
      <aside className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col border-l border-line bg-surface shadow-2xl shadow-black/60">
        {agentQuery.isPending || !agent ? (
          <div className="flex flex-1 items-center justify-center">
            <Spinner label="Loading agent…" />
          </div>
        ) : (
          <>
            <header className="border-b border-line px-6 pb-0 pt-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="text-lg font-semibold tracking-tight">{agent.name}</h2>
                  <p className="mt-0.5 text-sm text-dim">
                    {agent.role_title || "No role title"}
                    {team ? ` · ${team.name}` : " · No team"}
                    {manager ? ` · Reports to ${manager.name}` : ""}
                  </p>
                  <div className="mt-2 flex items-center gap-2">
                    <Badge
                      tone={
                        agent.status === "active"
                          ? "ok"
                          : agent.status === "paused"
                            ? "warn"
                            : "neutral"
                      }
                    >
                      <StatusDot status={agent.status} /> {agent.status}
                    </Badge>
                    <Badge tone="neutral">{agent.autonomy_level}</Badge>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {can("member") ? (
                    <Button size="sm" onClick={() => toggle.mutate()} disabled={toggle.isPending}>
                      {agent.status === "paused" ? (
                        <>
                          <Play size={13} /> Resume
                        </>
                      ) : (
                        <>
                          <Pause size={13} /> Pause
                        </>
                      )}
                    </Button>
                  ) : null}
                  <Button size="sm" variant="ghost" onClick={onClose} aria-label="Close">
                    <X size={15} />
                  </Button>
                </div>
              </div>
              <nav className="mt-4 flex gap-1 overflow-x-auto">
                {TABS.map((entry) => {
                  const disabled = "phase" in entry;
                  return (
                    <button
                      key={entry.id}
                      disabled={disabled}
                      title={disabled ? `Arrives in ${entry.phase}` : undefined}
                      onClick={() => setTab(entry.id)}
                      className={`whitespace-nowrap border-b-2 px-2.5 pb-2 text-[13px] transition-colors ${
                        tab === entry.id
                          ? "border-accent font-medium text-ink"
                          : disabled
                            ? "cursor-not-allowed border-transparent text-faint"
                            : "border-transparent text-dim hover:text-ink"
                      }`}
                    >
                      {entry.label}
                      {disabled ? <span className="ml-1 text-[10px]">({entry.phase})</span> : null}
                    </button>
                  );
                })}
              </nav>
            </header>

            <div className="flex-1 overflow-y-auto px-6 py-5">
              {tab === "overview" ? (
                <div>
                  {agent.description ? (
                    <p className="mb-4 text-sm text-dim">{agent.description}</p>
                  ) : null}
                  <OverviewRow label="Team" value={team?.name ?? "—"} />
                  <OverviewRow label="Manager" value={manager?.name ?? "—"} />
                  <OverviewRow label="Autonomy" value={agent.autonomy_level} />
                  <OverviewRow label="Max steps per run" value={agent.max_steps} />
                  <OverviewRow label="Max run minutes" value={agent.max_run_minutes} />
                  <OverviewRow
                    label="Monthly budget"
                    value={
                      agent.monthly_budget_cents !== null
                        ? `$${(agent.monthly_budget_cents / 100).toFixed(2)}`
                        : "Unlimited"
                    }
                  />
                  <OverviewRow
                    label="Model"
                    value={<span className="text-faint">assign in Phase 3</span>}
                  />
                  <OverviewRow label="Slug" value={<code className="text-xs">{agent.slug}</code>} />
                </div>
              ) : null}

              {tab === "instructions" ? (
                <InstructionsTab agent={agent} canEdit={can("admin")} onSaved={invalidate} />
              ) : null}

              {tab === "settings" ? (
                can("admin") ? (
                  <SettingsTab
                    agent={agent}
                    teams={teams}
                    agents={agents}
                    onSaved={invalidate}
                    onDelete={() => {
                      if (window.confirm(`Delete agent “${agent.name}”? This cannot be undone.`)) {
                        remove.mutate();
                      }
                    }}
                    deleteError={
                      remove.error instanceof ApiError ? remove.error.detail : null
                    }
                  />
                ) : (
                  <p className="text-sm text-dim">
                    Agent settings can be changed by workspace admins.
                  </p>
                )
              ) : null}
            </div>
          </>
        )}
      </aside>
    </div>
  );
}

function InstructionsTab({
  agent,
  canEdit,
  onSaved,
}: {
  agent: Agent;
  canEdit: boolean;
  onSaved: () => void;
}) {
  const { workspace } = useWorkspace();
  const [prompt, setPrompt] = useState(agent.system_prompt);

  const save = useMutation({
    mutationFn: () =>
      api<Agent>(`/api/v1/workspaces/${workspace.workspace_id}/agents/${agent.id}`, {
        method: "PATCH",
        body: { system_prompt: prompt },
      }),
    onSuccess: onSaved,
  });

  return (
    <div className="space-y-3">
      <p className="text-xs text-dim">
        The system prompt composed into every run for this agent (plan 7.2). Role, team, and
        reporting context are appended automatically at runtime.
      </p>
      <Textarea
        rows={16}
        value={prompt}
        readOnly={!canEdit}
        onChange={(event) => setPrompt(event.target.value)}
        placeholder="You are the CTO of this organization…"
        className="font-mono text-[13px] leading-relaxed"
      />
      {canEdit ? (
        <div className="flex items-center justify-end gap-3">
          {save.error instanceof ApiError ? (
            <span className="text-xs text-danger">{save.error.detail}</span>
          ) : null}
          {save.isSuccess ? <span className="text-xs text-ok">Saved</span> : null}
          <Button
            variant="primary"
            size="sm"
            disabled={save.isPending || prompt === agent.system_prompt}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save instructions"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function SettingsTab({
  agent,
  teams,
  agents,
  onSaved,
  onDelete,
  deleteError,
}: {
  agent: Agent;
  teams: OrgTeamNode[];
  agents: OrgAgentNode[];
  onSaved: () => void;
  onDelete: () => void;
  deleteError: string | null;
}) {
  const { workspace } = useWorkspace();
  const [name, setName] = useState(agent.name);
  const [roleTitle, setRoleTitle] = useState(agent.role_title);
  const [description, setDescription] = useState(agent.description);
  const [teamId, setTeamId] = useState(agent.team_id ?? "");
  const [managerId, setManagerId] = useState(agent.manager_agent_id ?? "");
  const [autonomy, setAutonomy] = useState<AutonomyLevel>(agent.autonomy_level);
  const [maxSteps, setMaxSteps] = useState(String(agent.max_steps));
  const [maxRunMinutes, setMaxRunMinutes] = useState(String(agent.max_run_minutes));

  const save = useMutation({
    mutationFn: () =>
      api<Agent>(`/api/v1/workspaces/${workspace.workspace_id}/agents/${agent.id}`, {
        method: "PATCH",
        body: {
          name,
          role_title: roleTitle,
          description,
          team_id: teamId || null,
          manager_agent_id: managerId || null,
          autonomy_level: autonomy,
          max_steps: Number(maxSteps),
          max_run_minutes: Number(maxRunMinutes),
        },
      }),
    onSuccess: onSaved,
  });

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <Field label="Name">
        <Input required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Role title">
        <Input
          maxLength={200}
          value={roleTitle}
          onChange={(e) => setRoleTitle(e.target.value)}
        />
      </Field>
      <Field label="Description">
        <Textarea
          rows={2}
          maxLength={4000}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Team">
          <Select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
            <option value="">No team</option>
            {teams.map((team) => (
              <option key={team.id} value={team.id}>
                {team.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Manager" hint="Cycles are rejected server-side.">
          <Select value={managerId} onChange={(e) => setManagerId(e.target.value)}>
            <option value="">No manager</option>
            {agents
              .filter((candidate) => candidate.id !== agent.id)
              .map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.name}
                </option>
              ))}
          </Select>
        </Field>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <Field label="Autonomy">
          <Select
            value={autonomy}
            onChange={(e) => setAutonomy(e.target.value as AutonomyLevel)}
          >
            <option value="manual">manual</option>
            <option value="supervised">supervised</option>
            <option value="autonomous">autonomous</option>
          </Select>
        </Field>
        <Field label="Max steps">
          <Input
            type="number"
            min={1}
            max={500}
            required
            value={maxSteps}
            onChange={(e) => setMaxSteps(e.target.value)}
          />
        </Field>
        <Field label="Max minutes">
          <Input
            type="number"
            min={1}
            max={1440}
            required
            value={maxRunMinutes}
            onChange={(e) => setMaxRunMinutes(e.target.value)}
          />
        </Field>
      </div>
      <ErrorNote
        message={save.error instanceof ApiError ? save.error.detail : null}
      />
      <div className="flex items-center justify-end gap-3">
        {save.isSuccess ? <span className="text-xs text-ok">Saved</span> : null}
        <Button type="submit" variant="primary" size="sm" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save settings"}
        </Button>
      </div>

      <div className="mt-6 rounded-lg border border-danger/25 bg-danger/5 p-4">
        <p className="text-sm font-medium text-danger">Danger zone</p>
        <p className="mb-3 mt-1 text-xs text-dim">
          Deleting an agent detaches its reports and managed teams (they are not deleted).
        </p>
        <ErrorNote message={deleteError} />
        <Button type="button" variant="danger" size="sm" onClick={onDelete}>
          <Trash2 size={13} /> Delete agent
        </Button>
      </div>
    </form>
  );
}
