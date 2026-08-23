"use client";

/** Agent creation wizard (plan 17.6). Steps 1-6 (identity, instructions,
 * placement, model, tools, autonomy) and 8 (review) are live; step 7
 * (budget enforcement) arrives in Phase 10. */

import { useMutation } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, Lock } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { ScopeEditor } from "@/components/scope-editor";
import {
  Badge,
  Button,
  ErrorNote,
  Field,
  Input,
  Select,
  focusRing,
  Spinner,
  Textarea,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { missingRequiredScopeKeys } from "@/lib/connectors";
import {
  useConnections,
  useInvalidateOrg,
  useModelProfiles,
  useOrgGraph,
  useTools,
  useWorkspaceDetail,
} from "@/lib/hooks";
import { PRESET_DESCRIPTIONS, PRESET_RULES, describeRule, riskTone } from "@/lib/policy";
import type { Agent, ApprovalPreset, AutonomyLevel } from "@/lib/types";
import {
  AGENT_TEMPLATES,
  applyTemplate,
  applyToolPreset,
  presetMissingTools,
  TOOL_PRESETS,
  canSubmit,
  EMPTY_WIZARD,
  toCreatePayload,
  grantPayloadsForTools,
  parseExpertise,
  toggleTool,
  validateStep,
  WIZARD_STEPS,
  type WizardState,
} from "@/lib/wizard";
import { useWorkspace } from "@/lib/workspace-context";

function StepRail({ current, onSelect }: { current: number; onSelect: (id: number) => void }) {
  return (
    <ol className="space-y-1">
      {WIZARD_STEPS.map((step) => {
        const active = step.id === current;
        const disabled = Boolean(step.disabledPhase);
        return (
          <li key={step.id}>
            <button
              onClick={() => onSelect(step.id)}
              aria-current={active ? "step" : undefined}
              className={`flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left text-[13px] transition-colors ${focusRing} ${
                active
                  ? "bg-accent-soft font-medium text-accent-strong"
                  : "text-dim hover:bg-hover hover:text-ink"
              }`}
            >
              <span
                className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-xs tabular-nums ${
                  active ? "border-accent text-accent-strong" : "border-line-strong text-faint"
                }`}
              >
                {step.id}
              </span>
              <span className="flex-1 truncate">{step.title}</span>
              {disabled ? <Lock size={11} className="shrink-0 text-faint" /> : null}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function DisabledStep({ title }: { title: string }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-line-strong bg-surface/60 px-6 py-16 text-center">
      <Lock size={18} className="text-faint" />
      <div className="flex items-center gap-2">
        <p className="text-sm font-medium">{title}</p>
        <Badge tone="accent">Coming soon</Badge>
      </div>
      <p className="max-w-sm text-sm text-dim">
        This step isn&apos;t available yet. The agent is created with safe defaults you can
        change afterwards.
      </p>
    </div>
  );
}

function ReviewRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-6 border-b border-line py-2.5 text-sm last:border-0">
      <span className="shrink-0 text-dim">{label}</span>
      <span className="min-w-0 text-right">{value}</span>
    </div>
  );
}

function WizardInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const graph = useOrgGraph(workspaceId);
  const profiles = useModelProfiles(workspaceId);
  const tools = useTools(workspaceId);
  const connections = useConnections(workspaceId);
  const workspaceDetail = useWorkspaceDetail(workspaceId);
  const invalidate = useInvalidateOrg(workspaceId);

  const [step, setStep] = useState(1);
  const [state, setState] = useState<WizardState>({
    ...EMPTY_WIZARD,
    teamId: searchParams.get("team") ?? "",
  });
  const [attempted, setAttempted] = useState(false);

  const patch = (changes: Partial<WizardState>) =>
    setState((previous) => ({ ...previous, ...changes }));

  const create = useMutation({
    mutationFn: async () => {
      const agent = await api<Agent>(`/api/v1/workspaces/${workspaceId}/agents`, {
        method: "POST",
        body: toCreatePayload(state),
      });
      for (const grant of grantPayloadsForTools(state, tools.data ?? [])) {
        await api(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}/grants`, {
          method: "POST",
          body: grant,
        });
      }
      await api(`/api/v1/workspaces/${workspaceId}/agents/${agent.id}/policy`, {
        method: "PUT",
        body: { preset: state.approvalPreset },
      });
      return agent;
    },
    onSuccess: (agent) => {
      invalidate();
      router.push(`/agents/${agent.id}`);
    },
  });

  if (graph.isPending || !graph.data) {
    return (
      <PageBody>
        <Spinner />
      </PageBody>
    );
  }

  const teams = graph.data.teams;
  const agents = graph.data.agents;
  const stepMeta = WIZARD_STEPS.find((s) => s.id === step)!;
  const errors = attempted ? validateStep(step, state) : [];
  const team = teams.find((t) => t.id === state.teamId);
  const manager = agents.find((a) => a.id === state.managerAgentId);
  const profileList = profiles.data ?? [];
  const defaultProfile = profileList.find(
    (p) => p.id === workspaceDetail.data?.default_model_profile_id,
  );
  const chosenProfile = profileList.find((p) => p.id === state.modelProfileId);

  const goNext = () => {
    const validation = validateStep(step, state);
    if (validation.length > 0) {
      setAttempted(true);
      return;
    }
    setAttempted(false);
    setStep(Math.min(step + 1, 8));
  };

  return (
    <PageBody className="flex flex-col gap-6 md:flex-row md:gap-8">
      <aside className="w-full shrink-0 md:w-56">
        <StepRail current={step} onSelect={setStep} />
      </aside>
      <div className="max-w-2xl flex-1 space-y-5">
        {stepMeta.disabledPhase ? (
          <DisabledStep title={stepMeta.title} />
        ) : step === 1 ? (
          <div className="space-y-4">
            <Field label="Agent name">
              <Input
                required
                maxLength={200}
                value={state.name}
                onChange={(e) => patch({ name: e.target.value })}
                placeholder="Senior Software Engineer"
              />
            </Field>
            <Field label="Role title" hint="Shown on the org chart and agent profile.">
              <Input
                maxLength={200}
                value={state.roleTitle}
                onChange={(e) => patch({ roleTitle: e.target.value })}
                placeholder="Senior Software Engineer"
              />
            </Field>
            <Field label="Description">
              <Textarea
                rows={3}
                maxLength={4000}
                value={state.description}
                onChange={(e) => patch({ description: e.target.value })}
                placeholder="What this agent is responsible for"
              />
            </Field>
            <Field
              label="Purpose"
              hint="What colleagues see when they look this agent up. Falls back to the description."
            >
              <Textarea
                rows={2}
                maxLength={1000}
                value={state.publicPurpose}
                onChange={(e) => patch({ publicPurpose: e.target.value })}
                placeholder="Builds features and fixes bugs in the product repos"
              />
            </Field>
            <Field
              label="Expertise"
              hint="Comma-separated tags other agents search by (e.g. python, github, testing)."
            >
              <Input
                value={state.expertise}
                onChange={(e) => patch({ expertise: e.target.value })}
                placeholder="python, github, testing"
              />
            </Field>
          </div>
        ) : step === 2 ? (
          <div className="space-y-4">
            <Field label="Start from a template" hint="Templates prefill role and instructions.">
              <div className="grid grid-cols-2 gap-2 pt-1 sm:grid-cols-4">
                {AGENT_TEMPLATES.map((template) => (
                  <button
                    key={template.id}
                    type="button"
                    onClick={() => patch(applyTemplate(state, template))}
                    className={`rounded-xl border border-line bg-raised px-2 py-2 text-xs text-dim transition-colors hover:border-accent/50 hover:text-ink ${focusRing}`}
                  >
                    {template.name}
                  </button>
                ))}
              </div>
            </Field>
            <Field
              label="System instructions"
              hint="Composed into every run. Team and reporting context are appended automatically."
            >
              <Textarea
                rows={12}
                value={state.systemPrompt}
                onChange={(e) => patch({ systemPrompt: e.target.value })}
                placeholder="You are…"
                className="font-mono text-[13px] leading-relaxed"
              />
            </Field>
          </div>
        ) : step === 3 ? (
          <div className="space-y-4">
            <Field label="Team">
              <Select value={state.teamId} onChange={(e) => patch({ teamId: e.target.value })}>
                <option value="">No team</option>
                {teams.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label="Manager"
              hint="Who this agent reports to on the org chart."
            >
              <Select
                value={state.managerAgentId}
                onChange={(e) => patch({ managerAgentId: e.target.value })}
              >
                <option value="">No manager</option>
                {agents.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                    {a.role_title ? ` — ${a.role_title}` : ""}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        ) : step === 4 ? (
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
                value={state.modelProfileId}
                onChange={(e) => patch({ modelProfileId: e.target.value })}
              >
                <option value="">Workspace default</option>
                {profileList.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.display_name} — {profile.model_name}
                  </option>
                ))}
              </Select>
            </Field>
            {profileList.length === 0 ? (
              <p className="rounded-xl border border-warn/30 bg-warn-soft px-3.5 py-2.5 text-sm text-warn">
                No model profiles exist yet. The agent can be created, but it cannot run tasks
                until a profile is assigned or a workspace default is set (Models page).
              </p>
            ) : null}
          </div>
        ) : step === 5 ? (
          <div className="space-y-4">
            <Field label="Presets" hint="One click grants a bundle of tools with sensible scopes. You can adjust every grant below.">
              <div className="flex flex-wrap gap-2 pt-1">
                {TOOL_PRESETS.map((preset) => {
                  const missing = presetMissingTools(preset, tools.data ?? []);
                  const applied = Object.keys(preset.tools).every((name) => state.grantToolNames.includes(name));
                  return (
                    <button
                      key={preset.id}
                      type="button"
                      data-testid={`tool-preset-${preset.id}`}
                      title={preset.description}
                      disabled={tools.isPending || missing.length === Object.keys(preset.tools).length}
                      onClick={() => setState(applyToolPreset(state, preset, tools.data ?? [], connections.data ?? []))}
                      className={`rounded-xl border px-3 py-2 text-left text-sm transition-colors disabled:opacity-50 ${
                        applied ? "border-accent bg-accent-soft" : "border-line bg-raised hover:border-line-strong"
                      }`}
                    >
                      <span className="block font-medium">{preset.label}</span>
                      <span className="block text-xs text-dim">{preset.description}</span>
                    </button>
                  );
                })}
              </div>
            </Field>
            <Field
              label="Tool access"
              hint="Deny-by-default: the agent can only call tools you grant here."
            >
              <div className="space-y-2 pt-1">
                {(tools.data ?? []).map((tool) => {
                  const checked = state.grantToolNames.includes(tool.name);
                  return (
                    <label
                      key={tool.name}
                      className={`flex cursor-pointer items-start gap-3 rounded-xl border px-3 py-2.5 transition-colors ${
                        checked ? "border-accent bg-accent-soft" : "border-line bg-raised"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => setState(toggleTool(state, tool.name))}
                        className="mt-0.5 accent-[var(--accent)]"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <code className="font-mono text-[13px] font-medium">{tool.name}</code>
                          <Badge tone={riskTone(tool.risk)}>{tool.risk}</Badge>
                        </span>
                        <span className="mt-0.5 block text-xs text-dim">{tool.description}</span>
                        {tool.required_capability !== tool.name ? (
                          <code className="mt-1 block font-mono text-xs text-faint">
                            {tool.required_capability}
                          </code>
                        ) : null}
                      </span>
                    </label>
                  );
                })}
                {tools.isPending ? <Spinner /> : null}
              </div>
            </Field>
            {(tools.data ?? []).filter((tool) => state.grantToolNames.includes(tool.name)).map((tool) => (
              tool.scope_keys.length > 0 ? (
                <div key={tool.name} className="space-y-2 rounded-2xl border border-line bg-surface px-4 py-3 shadow-card">
                  <p className="text-xs font-medium">
                    <code>{tool.name}</code> scope
                  </p>
                  <ScopeEditor
                    tool={tool}
                    connections={(connections.data ?? []).filter(
                      (connection) => connection.connector_type === tool.name.split(".", 1)[0],
                    )}
                    values={state.grantScopes[tool.name] ?? {}}
                    onChange={(values) => patch({
                      grantScopes: { ...state.grantScopes, [tool.name]: values },
                    })}
                  />
                </div>
              ) : null
            ))}
          </div>
        ) : step === 6 ? (
          <div className="space-y-4">
            <Field label="Autonomy level" hint="How independently the agent operates.">
              <Select
                value={state.autonomyLevel}
                onChange={(e) => patch({ autonomyLevel: e.target.value as AutonomyLevel })}
              >
                <option value="manual">manual — acts only when messaged</option>
                <option value="supervised">supervised — works tasks, humans approve risk</option>
                <option value="autonomous">autonomous — minimal human involvement</option>
              </Select>
            </Field>
            <Field
              label="Approval policy"
              hint="A preset expands to the explicit rules below, which are stored on the agent."
            >
              <div className="grid gap-2 pt-1 sm:grid-cols-3">
                {(["autonomous", "balanced", "restricted"] as ApprovalPreset[]).map((preset) => {
                  const active = state.approvalPreset === preset;
                  return (
                    <button
                      key={preset}
                      type="button"
                      onClick={() => patch({ approvalPreset: preset })}
                      aria-pressed={active}
                      className={`rounded-xl border px-3 py-2.5 text-left transition-colors ${focusRing} ${
                        active
                          ? "border-accent bg-accent-soft"
                          : "border-line bg-raised hover:border-line-strong"
                      }`}
                    >
                      <p className="text-[13px] font-medium capitalize">{preset}</p>
                      <p className="mt-1 text-xs leading-snug text-dim">
                        {PRESET_DESCRIPTIONS[preset]}
                      </p>
                    </button>
                  );
                })}
              </div>
            </Field>
            <div className="rounded-2xl border border-line bg-surface px-4 py-3 shadow-card">
              <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">
                Rules this preset sets
              </p>
              <ul className="space-y-1">
                {PRESET_RULES[state.approvalPreset].map((rule, index) => (
                  <li key={index} className="flex items-center gap-2 text-xs">
                    <Badge tone={riskTone(rule.risk)}>{rule.risk ?? "any"}</Badge>
                    <span>{describeRule(rule)}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="rounded-2xl border border-line bg-surface px-5 py-2 shadow-card">
              <ReviewRow label="Name" value={state.name || "—"} />
              <ReviewRow label="Role title" value={state.roleTitle || "—"} />
              <ReviewRow label="Purpose" value={state.publicPurpose.trim() || "—"} />
              <ReviewRow
                label="Expertise"
                value={
                  parseExpertise(state.expertise).length > 0 ? (
                    <span className="flex flex-wrap justify-end gap-1">
                      {parseExpertise(state.expertise).map((tag) => (
                        <Badge key={tag}>{tag}</Badge>
                      ))}
                    </span>
                  ) : (
                    "—"
                  )
                }
              />
              <ReviewRow label="Team" value={team?.name ?? "No team"} />
              <ReviewRow label="Manager" value={manager?.name ?? "No manager"} />
              <ReviewRow
                label="Instructions"
                value={
                  state.systemPrompt ? (
                    <span className="text-xs text-dim">
                      {state.systemPrompt.length} characters
                    </span>
                  ) : (
                    "—"
                  )
                }
              />
              <ReviewRow
                label="Model"
                value={
                  chosenProfile
                    ? `${chosenProfile.display_name} (${chosenProfile.model_name})`
                    : defaultProfile
                      ? `Workspace default — ${defaultProfile.display_name}`
                      : "Workspace default (none set yet)"
                }
              />
              <ReviewRow
                label="Tools"
                value={
                  state.grantToolNames.length > 0 ? (
                    <span className="flex flex-wrap justify-end gap-x-1.5 gap-y-0.5 text-xs">
                      {state.grantToolNames.map((toolName) => (
                        <code key={toolName} className="font-mono text-xs">
                          {toolName}
                        </code>
                      ))}
                    </span>
                  ) : (
                    <span className="text-faint">none (deny-by-default)</span>
                  )
                }
              />
              <ReviewRow label="Autonomy" value={state.autonomyLevel} />
              <ReviewRow
                label="Approval policy"
                value={<span className="capitalize">{state.approvalPreset}</span>}
              />
              <ReviewRow
                label="Budget"
                value={<span className="text-faint">unlimited · limits coming soon</span>}
              />
            </div>
            <ErrorNote
              message={
                create.error instanceof ApiError
                  ? create.error.detail
                  : create.error
                    ? "Creating the agent failed."
                    : null
              }
            />
          </div>
        )}

        {errors.length > 0 ? <ErrorNote message={errors.join(" ")} /> : null}

        <footer className="flex items-center justify-between border-t border-line pt-4">
          <Button variant="ghost" onClick={() => setStep(Math.max(1, step - 1))} disabled={step === 1}>
            <ChevronLeft size={14} /> Back
          </Button>
          {step < 8 ? (
            <Button variant="primary" onClick={goNext}>
              Continue <ChevronRight size={14} />
            </Button>
          ) : (
            <Button
              variant="primary"
              disabled={
                !canSubmit(state) ||
                create.isPending ||
                (tools.data ?? []).some(
                  (tool) => state.grantToolNames.includes(tool.name) &&
                    missingRequiredScopeKeys(tool, state.grantScopes[tool.name] ?? {}).length > 0,
                )
              }
              onClick={() => create.mutate()}
            >
              <Check size={14} /> {create.isPending ? "Creating…" : "Create agent"}
            </Button>
          )}
        </footer>
      </div>
    </PageBody>
  );
}

export default function NewAgentPage() {
  return (
    <>
      <PageHeader title="New agent" description="Set up a new agent and choose where it sits in your organization." />
      <Suspense
        fallback={
          <PageBody>
            <Spinner />
          </PageBody>
        }
      >
        <WizardInner />
      </Suspense>
    </>
  );
}
