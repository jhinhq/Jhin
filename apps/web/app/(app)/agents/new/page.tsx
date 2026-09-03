"use client";

/** Agent creation wizard (plan 17.6). Three required steps — identity, what
 * the agent can do, review — plus one optional "Advanced setup" step holding
 * everything that already has a working default. */

import { useMutation } from "@tanstack/react-query";
import { Check, ChevronDown, ChevronLeft, ChevronRight, SlidersHorizontal } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Avatar } from "@/components/avatar";
import { Disclosure as InlineDisclosure, LoadError } from "@/components/company/bits";
import { PersonaPicker } from "@/components/personas/persona-picker";
import { ScopeEditor } from "@/components/scope-editor";
import { ShapeAvatar } from "@/components/shape-avatar";
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
  usePersonas,
  useTools,
  useWorkspaceDetail,
} from "@/lib/hooks";
import { PRESET_DESCRIPTIONS, PRESET_RULES, describeRule, riskTone } from "@/lib/policy";
import { AVATAR_PALETTE, AVATAR_SHAPES } from "@/lib/shapes";
import type { Agent, ApprovalPreset, AutonomyLevel } from "@/lib/types";
import {
  ADVANCED_STEP,
  AGENT_TEMPLATES,
  applyTemplate,
  applyToolPreset,
  COLLABORATION_PRESET_ID,
  capabilitySummary,
  effectiveAvatar,
  firstInvalidStep,
  hasManualGrants,
  isPresetApplied,
  PERSONA_STEP,
  presetMissingTools,
  REVIEW_STEP,
  setToolScope,
  toggleToolPreset,
  TOOL_PRESETS,
  canSubmit,
  EMPTY_WIZARD,
  monthlyBudgetCents,
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
        return (
          <li key={step.id}>
            <button
              onClick={() => onSelect(step.id)}
              data-testid={`wizard-step-${step.id}`}
              aria-current={active ? "step" : undefined}
              className={`flex w-full items-start gap-2.5 rounded-xl px-2.5 py-2 text-left text-[13px] transition-colors ${focusRing} ${
                active
                  ? "bg-accent-soft font-medium text-accent-strong"
                  : "text-dim hover:bg-hover hover:text-ink"
              }`}
            >
              <span
                className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-xs tabular-nums ${
                  active ? "border-accent text-accent-strong" : "border-line-strong text-faint"
                }`}
              >
                {step.id}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1.5">
                  <span className="truncate">{step.title}</span>
                  {step.optional ? (
                    <span className="shrink-0 text-[10px] uppercase tracking-wider text-faint">
                      optional
                    </span>
                  ) : null}
                </span>
                <span className="mt-0.5 block truncate text-[11px] font-normal text-faint">
                  {step.hint}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
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

/** A collapsible block: header always visible with a one-line summary of the
 * current value, body revealed on click. */
function Disclosure({
  id,
  title,
  summary,
  defaultOpen = false,
  icon,
  children,
}: {
  id: string;
  title: string;
  summary: string;
  defaultOpen?: boolean;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-2xl border border-line bg-surface shadow-card">
      <button
        type="button"
        data-testid={`disclosure-${id}`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className={`flex w-full items-center gap-3 rounded-2xl px-4 py-3 text-left ${focusRing}`}
      >
        {icon ?? null}
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-medium">{title}</span>
          <span className="mt-0.5 block truncate text-xs text-dim">{summary}</span>
        </span>
        <ChevronDown
          size={15}
          className={`shrink-0 text-faint transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open ? (
        <div data-testid={`disclosure-body-${id}`} className="space-y-4 border-t border-line px-4 py-4">
          {children}
        </div>
      ) : null}
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
  const personas = usePersonas(workspaceId);
  const invalidate = useInvalidateOrg(workspaceId);

  const [step, setStep] = useState(1);
  const [state, setState] = useState<WizardState>({
    ...EMPTY_WIZARD,
    teamId: searchParams.get("team") ?? "",
  });
  const [attempted, setAttempted] = useState(false);
  /** null = follow the state (open when tools were hand-picked). */
  const [advancedToolsOverride, setAdvancedToolsOverride] = useState<boolean | null>(null);

  // Collaboration is on by default (the creator can toggle it off on the
  // capabilities step): a new teammate should be able to find colleagues and
  // ask them for help out of the box. Applied once, when the tool catalog
  // first loads, against the live catalog so it registers as the preset — the
  // render-phase "adjust state when data arrives" pattern (not an effect), so
  // a workspace whose catalog lacks the tools simply gets nothing added.
  const [collaborationSeeded, setCollaborationSeeded] = useState(false);
  if (!collaborationSeeded && tools.data) {
    const preset = TOOL_PRESETS.find((entry) => entry.id === COLLABORATION_PRESET_ID);
    setCollaborationSeeded(true);
    if (preset) {
      setState((previous) =>
        applyToolPreset(previous, preset, tools.data ?? [], connections.data ?? []),
      );
    }
  }

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

  if (graph.isError) {
    return (
      <PageBody>
        <LoadError what="the setup form" onRetry={() => void graph.refetch()} />
      </PageBody>
    );
  }
  if (graph.isPending || !graph.data) {
    return (
      <PageBody>
        <Spinner />
      </PageBody>
    );
  }

  const teams = graph.data.teams;
  const agents = graph.data.agents;
  const errors = attempted ? validateStep(step, state) : [];
  const team = teams.find((t) => t.id === state.teamId);
  const manager = agents.find((a) => a.id === state.managerAgentId);
  const profileList = profiles.data ?? [];
  const defaultProfile = profileList.find(
    (p) => p.id === workspaceDetail.data?.default_model_profile_id,
  );
  const chosenProfile = profileList.find((p) => p.id === state.modelProfileId);
  const chosenPersona = personas.data?.items.find((p) => p.id === state.personaId);
  const toolList = tools.data ?? [];
  const summary = capabilitySummary(state, toolList);
  const advancedToolsOpen = advancedToolsOverride ?? hasManualGrants(state);
  const incompleteScopeTools = toolList.filter(
    (tool) =>
      state.grantToolNames.includes(tool.name) &&
      missingRequiredScopeKeys(tool, state.grantScopes[tool.name] ?? {}).length > 0,
  );

  const avatarPreview = effectiveAvatar(state);

  const invalidStep = firstInvalidStep(state);
  const invalidStepTitle = WIZARD_STEPS.find((entry) => entry.id === invalidStep)?.title;

  const goTo = (next: number) => {
    setAttempted(false);
    setStep(Math.min(Math.max(next, 1), REVIEW_STEP));
  };

  const goNext = () => {
    const validation = validateStep(step, state);
    if (validation.length > 0) {
      setAttempted(true);
      return;
    }
    goTo(step + 1);
  };

  return (
    <PageBody className="flex flex-col gap-6 md:flex-row md:gap-8">
      <aside className="w-full shrink-0 md:w-56">
        <StepRail current={step} onSelect={goTo} />
      </aside>
      <div className="max-w-2xl flex-1 space-y-5">
        {step === 1 ? (
          <div className="space-y-4">
            <Field
              label="Start from a template"
              hint="Fills in the role and a starting set of instructions. You can change everything after."
            >
              <div className="grid grid-cols-2 gap-2 pt-1 sm:grid-cols-4">
                {AGENT_TEMPLATES.map((template) => (
                  <button
                    key={template.id}
                    type="button"
                    data-testid={`agent-template-${template.id}`}
                    onClick={() => patch(applyTemplate(state, template))}
                    className={`rounded-xl border border-line bg-raised px-2 py-2 text-xs text-dim transition-colors hover:border-accent/50 hover:text-ink ${focusRing}`}
                  >
                    {template.name}
                  </button>
                ))}
              </div>
            </Field>
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
            <Field
              label="Avatar"
              hint="A free brand-cube avatar, picked from the name. You can upload a picture or generate an illustration later."
            >
              <div className="flex flex-wrap items-start gap-4 pt-1">
                <Avatar
                  name={state.name || "Agent"}
                  size="lg"
                  shape={avatarPreview.shape}
                  color={avatarPreview.color}
                  label="Avatar preview"
                />
                <div className="min-w-0 flex-1 space-y-2">
                  <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Avatar shape">
                    {AVATAR_SHAPES.map((spec) => {
                      const active = avatarPreview.shape === spec.id;
                      return (
                        <button
                          key={spec.id}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          aria-label={spec.label}
                          title={spec.label}
                          onClick={() => patch({ avatarShape: spec.id })}
                          className={`flex h-10 w-10 items-center justify-center rounded-xl border transition-colors ${focusRing} ${
                            active ? "border-accent bg-accent-soft" : "border-line bg-raised hover:border-line-strong"
                          }`}
                        >
                          <ShapeAvatar shape={spec.id} color={avatarPreview.color} className="h-6 w-6" />
                        </button>
                      );
                    })}
                  </div>
                  <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Avatar color">
                    {AVATAR_PALETTE.map((color) => {
                      const active = avatarPreview.color === color.hex;
                      return (
                        <button
                          key={color.hex}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          aria-label={color.label}
                          title={color.label}
                          onClick={() => patch({ avatarColor: color.hex })}
                          className={`h-10 w-10 rounded-full border transition-transform md:h-7 md:w-7 ${focusRing} ${
                            active ? "scale-110 border-ink" : "border-line"
                          }`}
                          style={{ backgroundColor: color.hex }}
                        />
                      );
                    })}
                  </div>
                </div>
              </div>
            </Field>
          </div>
        ) : step === 2 && tools.isPending ? (
          <Spinner label="Loading the tool catalog…" />
        ) : step === 2 && tools.isError ? (
          <LoadError what="the tool catalog" onRetry={() => void tools.refetch()} />
        ) : step === 2 ? (
          <div className="space-y-5">
            <Field
              label="Capabilities"
              hint="Pick what this agent should be able to do. Click again to take a capability away. Everything else stays blocked."
            >
              <div className="grid gap-2 pt-1 sm:grid-cols-2">
                {TOOL_PRESETS.map((preset) => {
                  const missing = presetMissingTools(preset, toolList);
                  const applied = isPresetApplied(state, preset, toolList);
                  const unavailable = missing.length === Object.keys(preset.tools).length;
                  return (
                    <button
                      key={preset.id}
                      type="button"
                      data-testid={`tool-preset-${preset.id}`}
                      aria-pressed={applied}
                      title={preset.description}
                      disabled={unavailable}
                      onClick={() =>
                        setState(
                          toggleToolPreset(state, preset, toolList, connections.data ?? []),
                        )
                      }
                      className={`rounded-xl border px-3 py-2.5 text-left text-sm transition-colors disabled:opacity-50 ${focusRing} ${
                        applied ? "border-accent bg-accent-soft" : "border-line bg-raised hover:border-line-strong"
                      }`}
                    >
                      <span className="flex items-center gap-2">
                        <span
                          aria-hidden
                          className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-[5px] border ${
                            applied ? "border-accent bg-accent text-white" : "border-line-strong"
                          }`}
                        >
                          {applied ? <Check size={11} strokeWidth={3} /> : null}
                        </span>
                        <span className="font-medium">{preset.label}</span>
                      </span>
                      <span className="mt-1 block text-xs leading-snug text-dim">
                        {preset.summary}
                      </span>
                    </button>
                  );
                })}
              </div>
            </Field>

            <div
              data-testid="capability-summary"
              className="rounded-2xl border border-line bg-surface px-4 py-3 shadow-card"
            >
              <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">
                This agent will be able to
              </p>
              {summary.presets.length === 0 && summary.manualToolNames.length === 0 ? (
                <p className="text-sm text-dim">
                  Nothing yet — it can chat, but it cannot use any tool. Pick a capability above,
                  or add tools yourself. You can change this any time after creating the agent.
                </p>
              ) : (
                <ul className="space-y-1 text-sm">
                  {summary.presets.map((preset) => (
                    <li key={preset.id} className="flex items-start gap-2">
                      <Check size={14} className="mt-0.5 shrink-0 text-ok" />
                      <span>{preset.summary}</span>
                    </li>
                  ))}
                  {summary.manualToolNames.length > 0 ? (
                    <li className="flex items-start gap-2">
                      <Check size={14} className="mt-0.5 shrink-0 text-ok" />
                      <span>
                        Use {summary.manualToolNames.length} individually chosen{" "}
                        {summary.manualToolNames.length === 1 ? "tool" : "tools"}
                      </span>
                    </li>
                  ) : null}
                </ul>
              )}
            </div>

            <div className="rounded-2xl border border-line bg-surface shadow-card">
              <button
                type="button"
                data-testid="advanced-tools-toggle"
                aria-expanded={advancedToolsOpen}
                onClick={() => setAdvancedToolsOverride(!advancedToolsOpen)}
                className={`flex w-full items-center gap-3 rounded-2xl px-4 py-3 text-left ${focusRing}`}
              >
                <SlidersHorizontal size={15} className="shrink-0 text-faint" />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium">
                    Advanced: choose individual tools
                  </span>
                  <span className="mt-0.5 block text-xs text-dim">
                    {state.grantToolNames.length > 0
                      ? `${state.grantToolNames.length} ${state.grantToolNames.length === 1 ? "tool" : "tools"} selected · edit scopes and pin connections`
                      : `Pick from the ${toolList.length} tools in this workspace and scope each grant`}
                  </span>
                </span>
                <ChevronDown
                  size={15}
                  className={`shrink-0 text-faint transition-transform ${advancedToolsOpen ? "rotate-180" : ""}`}
                />
              </button>
              {advancedToolsOpen ? (
                <div
                  data-testid="advanced-tools"
                  className="space-y-4 border-t border-line px-4 py-4"
                >
                  <Field
                    label="Tool access"
                    hint="Deny-by-default: the agent can only call tools you grant here."
                  >
                    <div className="space-y-2 pt-1">
                      {toolList.map((tool) => {
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
                              <span className="flex flex-wrap items-center gap-2">
                                <code className="min-w-0 truncate font-mono text-[13px] font-medium" title={tool.name}>{tool.name}</code>
                                <Badge tone={riskTone(tool.risk)}>{tool.risk}</Badge>
                              </span>
                              <span className="mt-0.5 block text-xs text-dim">{tool.description}</span>
                              {tool.required_capability !== tool.name ? (
                                <code
                                  className="mt-1 block truncate font-mono text-xs text-faint"
                                  title={tool.required_capability}
                                >
                                  {tool.required_capability}
                                </code>
                              ) : null}
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  </Field>
                  {toolList
                    .filter((tool) => state.grantToolNames.includes(tool.name))
                    .map((tool) =>
                      tool.scope_keys.length > 0 ? (
                        <div
                          key={tool.name}
                          className="space-y-2 rounded-2xl border border-line bg-raised px-4 py-3"
                        >
                          <p className="text-xs font-medium">
                            <code>{tool.name}</code> scope
                          </p>
                          <ScopeEditor
                            tool={tool}
                            connections={(connections.data ?? []).filter(
                              (connection) =>
                                connection.connector_type === tool.name.split(".", 1)[0],
                            )}
                            values={state.grantScopes[tool.name] ?? {}}
                            onChange={(values) =>
                              setState(setToolScope(state, tool.name, values))
                            }
                          />
                        </div>
                      ) : null,
                    )}
                </div>
              ) : null}
            </div>
          </div>
        ) : step === PERSONA_STEP ? (
          <div className="space-y-4">
            <p className="text-sm text-dim">
              Optional. A persona shapes how this agent says things — its voice, pace, and manner.
              It never changes what the agent may do.
            </p>
            {personas.isPending ? (
              <Spinner label="Loading personas…" />
            ) : personas.isError || !personas.data ? (
              <LoadError what="the personas library" onRetry={() => void personas.refetch()} />
            ) : (
              <PersonaPicker
                allowNone
                personas={personas.data.items}
                value={state.personaId || null}
                onChange={(id) => patch({ personaId: id ?? "" })}
              />
            )}
          </div>
        ) : step === ADVANCED_STEP ? (
          <div className="space-y-3">
            <p className="text-sm text-dim">
              Everything here already has a working default — skip it unless you need to change
              something. You can change all of it later from the agent&apos;s settings.
            </p>
            <Disclosure
              id="instructions"
              title="System instructions"
              summary={
                state.systemPrompt
                  ? `${state.systemPrompt.length} characters`
                  : "Empty — the agent runs on its role and workspace context"
              }
              defaultOpen={state.systemPrompt.length > 0}
            >
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
            </Disclosure>

            <Disclosure
              id="placement"
              title="Team & manager"
              summary={`${team?.name ?? "No team"} · ${manager ? `reports to ${manager.name}` : "no manager"}`}
              defaultOpen={Boolean(state.teamId || state.managerAgentId)}
            >
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
              <Field label="Manager" hint="Who this agent reports to on the org chart.">
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
            </Disclosure>

            <Disclosure
              id="model"
              title="Model"
              summary={
                chosenProfile
                  ? `${chosenProfile.display_name} (${chosenProfile.model_name})`
                  : defaultProfile
                    ? `Workspace default — ${defaultProfile.display_name}`
                    : "Workspace default (none set yet)"
              }
              defaultOpen={Boolean(state.modelProfileId) || profileList.length === 0}
            >
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
            </Disclosure>

            <Disclosure
              id="approvals"
              title="Autonomy & approvals"
              summary={`${state.autonomyLevel} · ${state.approvalPreset} approvals`}
              defaultOpen={
                state.autonomyLevel !== EMPTY_WIZARD.autonomyLevel ||
                state.approvalPreset !== EMPTY_WIZARD.approvalPreset
              }
            >
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
              <div className="rounded-2xl border border-line bg-raised px-4 py-3">
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
            </Disclosure>

            <Disclosure
              id="limits"
              title="Limits & budget"
              summary={`${state.maxSteps} steps · ${state.maxRunMinutes} min · ${state.maxConcurrentRuns} concurrent · ${
                state.monthlyBudgetDollars.trim() ? `$${state.monthlyBudgetDollars} / month` : "no budget"
              }`}
              defaultOpen={
                state.maxSteps !== EMPTY_WIZARD.maxSteps ||
                state.maxRunMinutes !== EMPTY_WIZARD.maxRunMinutes ||
                state.maxConcurrentRuns !== EMPTY_WIZARD.maxConcurrentRuns ||
                state.monthlyBudgetDollars.trim() !== ""
              }
            >
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Field label="Max steps" hint="Model steps per run.">
                  <Input
                    type="number"
                    min={1}
                    max={500}
                    required
                    value={state.maxSteps}
                    onChange={(e) => patch({ maxSteps: e.target.value })}
                  />
                </Field>
                <Field label="Max run minutes" hint="Wall-clock cap per run.">
                  <Input
                    type="number"
                    min={1}
                    max={1440}
                    required
                    value={state.maxRunMinutes}
                    onChange={(e) => patch({ maxRunMinutes: e.target.value })}
                  />
                </Field>
                <Field label="Max concurrent runs" hint="Extra tasks queue until a run finishes.">
                  <Input
                    type="number"
                    min={1}
                    max={50}
                    required
                    value={state.maxConcurrentRuns}
                    onChange={(e) => patch({ maxConcurrentRuns: e.target.value })}
                  />
                </Field>
              </div>
              <Field
                label="Monthly budget ($)"
                hint="Model spend this agent may use per calendar month. New runs are blocked and in-flight runs stop once the budget is reached. Blank = no budget."
              >
                <Input
                  type="number"
                  min={0}
                  step="0.01"
                  placeholder="No budget"
                  value={state.monthlyBudgetDollars}
                  onChange={(e) => patch({ monthlyBudgetDollars: e.target.value })}
                />
              </Field>
            </Disclosure>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="rounded-2xl border border-line bg-surface px-5 py-2 shadow-card">
              <ReviewRow
                label="Avatar"
                value={
                  <Avatar
                    name={state.name || "Agent"}
                    size="sm"
                    shape={avatarPreview.shape}
                    color={avatarPreview.color}
                    label="Chosen avatar"
                  />
                }
              />
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
              <ReviewRow label="Persona" value={chosenPersona?.display_name ?? "None"} />
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
                label="Can do"
                value={
                  summary.presets.length > 0 || summary.manualToolNames.length > 0 ? (
                    <span
                      data-testid="review-capabilities"
                      className="flex flex-col items-end gap-0.5 text-xs"
                    >
                      {summary.presets.map((preset) => (
                        <span key={preset.id}>{preset.summary}</span>
                      ))}
                      {summary.manualToolNames.length > 0 ? (
                        <span>
                          {summary.manualToolNames.length} individually chosen{" "}
                          {summary.manualToolNames.length === 1 ? "tool" : "tools"}
                        </span>
                      ) : null}
                    </span>
                  ) : (
                    <span className="text-faint">nothing — no tools granted</span>
                  )
                }
              />
              {state.grantToolNames.length > 0 ? (
                <div className="border-b border-line py-2.5">
                  <InlineDisclosure label="Show tool details">
                    <ul className="flex flex-wrap gap-x-2 gap-y-1">
                      {state.grantToolNames.map((toolName) => (
                        <li key={toolName}>
                          <code className="font-mono text-xs text-dim">{toolName}</code>
                        </li>
                      ))}
                    </ul>
                  </InlineDisclosure>
                </div>
              ) : null}
              <ReviewRow label="Autonomy" value={state.autonomyLevel} />
              <ReviewRow
                label="Approval policy"
                value={<span className="capitalize">{state.approvalPreset}</span>}
              />
              <ReviewRow
                label="Limits"
                value={`${state.maxSteps} steps · ${state.maxRunMinutes} min · ${state.maxConcurrentRuns} concurrent`}
              />
              <ReviewRow
                label="Budget"
                value={
                  typeof monthlyBudgetCents(state.monthlyBudgetDollars) === "number" ? (
                    `$${((monthlyBudgetCents(state.monthlyBudgetDollars) as number) / 100).toFixed(2)} / month`
                  ) : (
                    <span className="text-faint">no budget</span>
                  )
                }
              />
            </div>
            {incompleteScopeTools.length > 0 ? (
              <ErrorNote
                message={`Finish the tool scopes in “What it can do” before creating: ${incompleteScopeTools
                  .map((tool) => tool.name)
                  .join(", ")}.`}
              />
            ) : null}
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

        <footer className="flex flex-wrap items-center justify-between gap-y-2 border-t border-line pt-4">
          <Button variant="ghost" onClick={() => goTo(step - 1)} disabled={step === 1}>
            <ChevronLeft size={14} /> Back
          </Button>
          {step < REVIEW_STEP ? (
            <div className="flex flex-wrap items-center justify-end gap-2">
              {WIZARD_STEPS.some((entry) => entry.id === step + 1 && entry.optional) ? (
                <Button variant="ghost" data-testid="wizard-skip" onClick={() => goTo(REVIEW_STEP)}>
                  {step + 1 === PERSONA_STEP ? "Skip optional steps" : "Skip advanced setup"}
                </Button>
              ) : null}
              <Button variant="primary" onClick={goNext}>
                Continue <ChevronRight size={14} />
              </Button>
            </div>
          ) : (
            <div className="flex items-center justify-end gap-3">
              {invalidStep !== null ? (
                <p className="text-right text-[13px] text-dim">
                  The{" "}
                  <button
                    type="button"
                    onClick={() => {
                      // Land on the offending step with its errors showing,
                      // rather than goTo, which clears them.
                      setStep(invalidStep);
                      setAttempted(true);
                    }}
                    className={`font-medium text-accent-strong underline ${focusRing}`}
                  >
                    {invalidStepTitle}
                  </button>{" "}
                  step needs a fix before this agent can be created.
                </p>
              ) : null}
              <Button
                variant="primary"
                disabled={!canSubmit(state) || create.isPending || incompleteScopeTools.length > 0}
                onClick={() => create.mutate()}
              >
                <Check size={14} /> {create.isPending ? "Creating…" : "Create agent"}
              </Button>
            </div>
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
