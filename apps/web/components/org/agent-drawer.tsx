"use client";

/** Agent profile drawer (plan 17.5): header + Overview / Instructions /
 * Settings tabs. Memory lives on the agent profile page. */

import { useMutation } from "@tanstack/react-query";
import { MessageSquare, Pause, Play, Trash2, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  Dialog,
  ErrorNote,
  Field,
  Input,
  Select,
  focusRing,
  Spinner,
  StatusDot,
  Textarea,
} from "@/components/ui";
import { ToolsAccessTab } from "@/components/org/tools-access-tab";
import { api, ApiError } from "@/lib/api";
import { parseExpertise } from "@/lib/wizard";
import {
  useAgent,
  useInvalidateOrg,
  useInvalidateTasks,
  useModelProfiles,
  useWorkspaceDetail,
} from "@/lib/hooks";
import type { Agent, AutonomyLevel, OrgAgentNode, OrgTeamNode, Task } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "instructions", label: "Instructions" },
  { id: "model", label: "Model" },
  { id: "tools", label: "Tools & Access" },
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
  const profiles = useModelProfiles(workspaceId);
  const workspaceDetail = useWorkspaceDetail(workspaceId);
  const invalidate = useInvalidateOrg(workspaceId);
  // The drawer is mounted with key={agentId}, so tab state resets per agent.
  const [tab, setTab] = useState<TabId>("overview");
  const [messageOpen, setMessageOpen] = useState(false);

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
      <button
        aria-label="Close drawer"
        className="absolute inset-0 cursor-default bg-[#221e38]/40 backdrop-blur-sm dark:bg-black/60"
        onClick={onClose}
      />
      <aside className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col border-l border-line bg-surface shadow-card">
        {agentQuery.isPending || !agent ? (
          <div className="flex flex-1 items-center justify-center">
            <Spinner label="Loading agent…" />
          </div>
        ) : (
          <>
            <header className="border-b border-line px-6 pb-0 pt-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="font-display text-lg font-semibold tracking-tight">{agent.name}</h2>
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
                  {can("member") && agent.status === "active" ? (
                    <Button size="sm" variant="primary" onClick={() => setMessageOpen(true)}>
                      <MessageSquare size={13} /> Message
                    </Button>
                  ) : null}
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
                {TABS.map((entry) => (
                  <button
                    key={entry.id}
                    onClick={() => setTab(entry.id)}
                    className={`whitespace-nowrap rounded-t-lg border-b-2 px-2.5 pb-2 text-[13px] transition-colors ${focusRing} ${
                      tab === entry.id
                        ? "border-accent font-medium text-ink"
                        : "border-transparent text-dim hover:text-ink"
                    }`}
                  >
                    {entry.label}
                  </button>
                ))}
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
                    label="Max concurrent runs"
                    value={agent.max_concurrent_runs}
                  />
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
                    value={(() => {
                      const profile = profiles.data?.find(
                        (p) => p.id === agent.model_profile_id,
                      );
                      if (profile) return `${profile.display_name} (${profile.model_name})`;
                      const fallback = profiles.data?.find(
                        (p) => p.id === workspaceDetail.data?.default_model_profile_id,
                      );
                      return fallback
                        ? `Workspace default — ${fallback.display_name}`
                        : "Workspace default (none set)";
                    })()}
                  />
                  <OverviewRow label="Slug" value={<code className="font-mono text-xs">{agent.slug}</code>} />
                  <div className="mt-4 flex gap-2">
                    <Link href={`/tasks?agent=${agent.id}`}>
                      <Button size="sm" variant="ghost">
                        View tasks
                      </Button>
                    </Link>
                    <Link href={`/runs?agent=${agent.id}`}>
                      <Button size="sm" variant="ghost">
                        View runs
                      </Button>
                    </Link>
                  </div>
                </div>
              ) : null}

              {tab === "model" ? (
                <ModelTab agent={agent} canEdit={can("admin")} onSaved={invalidate} />
              ) : null}

              {tab === "instructions" ? (
                <InstructionsTab agent={agent} canEdit={can("admin")} onSaved={invalidate} />
              ) : null}

              {tab === "tools" ? (
                <ToolsAccessTab agent={agent} canEdit={can("admin")} />
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
      {messageOpen && agent ? (
        <MessageDialog agent={agent} onClose={() => setMessageOpen(false)} />
      ) : null}
    </div>
  );
}

/** Message an agent (plan 17.5): the message becomes a task + run and the
 * user lands in the task's conversation view. */
function MessageDialog({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const router = useRouter();
  const { workspace } = useWorkspace();
  const invalidateTasks = useInvalidateTasks(workspace.workspace_id);
  const [text, setText] = useState("");

  const send = useMutation({
    mutationFn: () =>
      api<Task>(
        `/api/v1/workspaces/${workspace.workspace_id}/agents/${agent.id}/message`,
        { method: "POST", body: { text: text.trim() } },
      ),
    onSuccess: (task) => {
      invalidateTasks();
      onClose();
      router.push(`/tasks/${task.id}`);
    },
  });

  return (
    <Dialog title={`Message ${agent.name}`} open onClose={onClose} wide>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (text.trim()) send.mutate();
        }}
      >
        <Field
          label="Message"
          hint="Starts a conversational task; the agent replies in the task view."
        >
          <Textarea
            autoFocus
            rows={5}
            maxLength={20000}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={`Ask ${agent.name} to do something…`}
          />
        </Field>
        <ErrorNote
          message={
            send.error instanceof ApiError
              ? send.error.detail
              : send.error
                ? "Sending the message failed."
                : null
          }
        />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={send.isPending || !text.trim()}>
            {send.isPending ? "Sending…" : "Send message"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function ModelTab({
  agent,
  canEdit,
  onSaved,
}: {
  agent: Agent;
  canEdit: boolean;
  onSaved: () => void;
}) {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const profiles = useModelProfiles(workspaceId);
  const workspaceDetail = useWorkspaceDetail(workspaceId);
  const [profileId, setProfileId] = useState(agent.model_profile_id ?? "");
  const [temperature, setTemperature] = useState(
    agent.temperature !== null ? String(agent.temperature) : "",
  );
  const [maxTokens, setMaxTokens] = useState(
    agent.max_output_tokens !== null ? String(agent.max_output_tokens) : "",
  );

  const save = useMutation({
    mutationFn: () =>
      api<Agent>(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}`, {
        method: "PATCH",
        body: {
          model_profile_id: profileId || null,
          temperature: temperature === "" ? null : Number(temperature),
          max_output_tokens: maxTokens === "" ? null : Number(maxTokens),
        },
      }),
    onSuccess: onSaved,
  });

  const defaultProfile = profiles.data?.find(
    (p) => p.id === workspaceDetail.data?.default_model_profile_id,
  );

  return (
    <div className="space-y-4">
      <Field
        label="Model profile"
        hint={
          defaultProfile
            ? `Workspace default: ${defaultProfile.display_name} (${defaultProfile.model_name}).`
            : "No workspace default is set — configure one on the Models page."
        }
      >
        <Select
          value={profileId}
          disabled={!canEdit}
          onChange={(e) => setProfileId(e.target.value)}
        >
          <option value="">Workspace default</option>
          {(profiles.data ?? []).map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.display_name} — {profile.model_name}
            </option>
          ))}
        </Select>
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Temperature" hint="Empty = provider default.">
          <Input
            type="number"
            min="0"
            max="2"
            step="0.1"
            disabled={!canEdit}
            value={temperature}
            onChange={(e) => setTemperature(e.target.value)}
            placeholder="—"
          />
        </Field>
        <Field label="Max output tokens" hint="Empty = provider default.">
          <Input
            type="number"
            min="1"
            disabled={!canEdit}
            value={maxTokens}
            onChange={(e) => setMaxTokens(e.target.value)}
            placeholder="—"
          />
        </Field>
      </div>
      {canEdit ? (
        <div className="flex items-center justify-end gap-3">
          {save.error instanceof ApiError ? (
            <span className="text-xs text-danger">{save.error.detail}</span>
          ) : null}
          {save.isSuccess ? <span className="text-xs text-ok">Saved</span> : null}
          <Button variant="primary" size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save model settings"}
          </Button>
        </div>
      ) : (
        <p className="text-xs text-dim">Model settings can be changed by workspace admins.</p>
      )}
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
        The system prompt composed into every run for this agent. Role, team, and
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
  const [publicPurpose, setPublicPurpose] = useState(agent.public_purpose ?? "");
  const [expertise, setExpertise] = useState((agent.expertise_json ?? []).join(", "));
  const [teamId, setTeamId] = useState(agent.team_id ?? "");
  const [managerId, setManagerId] = useState(agent.manager_agent_id ?? "");
  const [autonomy, setAutonomy] = useState<AutonomyLevel>(agent.autonomy_level);
  const [maxSteps, setMaxSteps] = useState(String(agent.max_steps));
  const [maxRunMinutes, setMaxRunMinutes] = useState(String(agent.max_run_minutes));
  const [maxConcurrent, setMaxConcurrent] = useState(String(agent.max_concurrent_runs));

  const save = useMutation({
    mutationFn: () =>
      api<Agent>(`/api/v1/workspaces/${workspace.workspace_id}/agents/${agent.id}`, {
        method: "PATCH",
        body: {
          name,
          role_title: roleTitle,
          description,
          public_purpose: publicPurpose.trim(),
          expertise_json: parseExpertise(expertise),
          team_id: teamId || null,
          manager_agent_id: managerId || null,
          autonomy_level: autonomy,
          max_steps: Number(maxSteps),
          max_run_minutes: Number(maxRunMinutes),
          max_concurrent_runs: Number(maxConcurrent),
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
      <Field
        label="Purpose"
        hint="What colleagues see when they look this agent up. Falls back to the description."
      >
        <Textarea
          rows={2}
          maxLength={1000}
          value={publicPurpose}
          onChange={(e) => setPublicPurpose(e.target.value)}
        />
      </Field>
      <Field label="Expertise" hint="Comma-separated tags other agents search by.">
        <Input
          value={expertise}
          onChange={(e) => setExpertise(e.target.value)}
          placeholder="python, github, testing"
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
        <Field label="Manager" hint="Who this agent reports to. An agent can't report to one of its own reports.">
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
      <Field
        label="Max concurrent runs"
        hint="Additional tasks queue and start automatically when a run finishes."
      >
        <Input
          type="number"
          min={1}
          max={50}
          required
          value={maxConcurrent}
          onChange={(e) => setMaxConcurrent(e.target.value)}
        />
      </Field>
      <ErrorNote
        message={save.error instanceof ApiError ? save.error.detail : null}
      />
      <div className="flex items-center justify-end gap-3">
        {save.isSuccess ? <span className="text-xs text-ok">Saved</span> : null}
        <Button type="submit" variant="primary" size="sm" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save settings"}
        </Button>
      </div>

      <div className="mt-6 rounded-2xl border border-danger/30 bg-danger-soft p-4">
        <p className="font-display text-sm font-semibold text-danger">Danger zone</p>
        <p className="mb-3 mt-1 text-sm text-dim">
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
