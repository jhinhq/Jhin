/**
 * Agent creation wizard model (plan 17.6): step definitions, role templates,
 * and pure validation functions (unit-tested).
 */

import type { ApprovalPreset, AutonomyLevel, Grant, ToolInfo } from "@/lib/types";
import {
  buildToolScope,
  missingRequiredScopeKeys,
  type ToolScopeValues,
} from "@/lib/connectors";
import { isCapabilityGranted } from "@/lib/policy";
import { defaultShapeFor } from "@/lib/shapes";

export interface WizardState {
  name: string;
  roleTitle: string;
  description: string;
  /** Public purpose shown to colleagues in the directory and chat context. */
  publicPurpose: string;
  /** Comma-separated expertise tags (parsed with {@link parseExpertise}). */
  expertise: string;
  systemPrompt: string;
  teamId: string;
  managerAgentId: string;
  /** Empty string = use the workspace default profile (plan 15.2). */
  modelProfileId: string;
  /** Free brand-cube avatar; empty strings = derive from the name hash so
   * new agents start as colorful cubes rather than initials. */
  avatarShape: string;
  avatarColor: string;
  /** Capabilities to allow-grant right after creation (plan 12.3). */
  grantToolNames: string[];
  grantScopes: Record<string, ToolScopeValues>;
  /** Who put each granted tool in the list: preset ids and/or
   * {@link MANUAL_GRANT_SOURCE}. Lets a preset be toggled back off without
   * stripping tools another preset (or the user) still wants. */
  grantSources: Record<string, string[]>;
  approvalPreset: ApprovalPreset;
  autonomyLevel: AutonomyLevel;
  /** Run limits (step 7), kept as input strings for the number fields. */
  maxSteps: string;
  maxRunMinutes: string;
  maxConcurrentRuns: string;
  /** Monthly model-spend budget in dollars; blank = no budget. */
  monthlyBudgetDollars: string;
}

export const EMPTY_WIZARD: WizardState = {
  name: "",
  roleTitle: "",
  description: "",
  publicPurpose: "",
  expertise: "",
  systemPrompt: "",
  teamId: "",
  managerAgentId: "",
  modelProfileId: "",
  avatarShape: "",
  avatarColor: "",
  grantToolNames: [],
  grantScopes: {},
  grantSources: {},
  approvalPreset: "balanced",
  autonomyLevel: "supervised",
  maxSteps: "20",
  maxRunMinutes: "30",
  maxConcurrentRuns: "1",
  monthlyBudgetDollars: "",
};

/** The shape avatar the new agent will get: an explicit pick, else a
 * deterministic default derived from the agent's name. */
export function effectiveAvatar(
  state: Pick<WizardState, "name" | "avatarShape" | "avatarColor">,
): { shape: string; color: string } {
  const derived = defaultShapeFor(state.name);
  return {
    shape: state.avatarShape || derived.shape,
    color: state.avatarColor || derived.color,
  };
}

export interface WizardStep {
  id: number;
  title: string;
  /** One line under the title in the step rail. */
  hint: string;
  /** Optional steps have a working default for every field and can be
   * skipped outright — they are marked as such and never block Continue. */
  optional?: boolean;
}

/**
 * Three required steps (identity → capabilities → review) plus one clearly
 * optional "Advanced setup" step that collects everything with a sensible
 * default: instructions, placement, model, approvals, limits and budget.
 */
export const WIZARD_STEPS: WizardStep[] = [
  { id: 1, title: "Identity", hint: "Name, role, avatar" },
  { id: 2, title: "What it can do", hint: "Capabilities & tools" },
  {
    id: 3,
    title: "Advanced setup",
    hint: "Instructions, team, model, limits",
    optional: true,
  },
  { id: 4, title: "Review & create", hint: "Check and confirm" },
];

/** The last step; reaching it is what "finish the wizard" means. */
export const REVIEW_STEP = 4;

/** The step holding everything that already has a working default. */
export const ADVANCED_STEP = 3;

export interface AgentTemplate {
  id: string;
  name: string;
  roleTitle: string;
  systemPrompt: string;
}

export const AGENT_TEMPLATES: AgentTemplate[] = [
  {
    id: "cto",
    name: "CTO",
    roleTitle: "Chief Technology Officer",
    systemPrompt:
      "You are the CTO. You own technical strategy, break incoming engineering work into well-scoped tasks, delegate to the right engineer, and review outcomes before they ship.",
  },
  {
    id: "swe",
    name: "Software Engineer",
    roleTitle: "Senior Software Engineer",
    systemPrompt:
      "You are a senior software engineer. You implement well-tested, production-quality changes, keep diffs small and reviewable, and escalate when requirements are ambiguous.",
  },
  {
    id: "qa",
    name: "QA Engineer",
    roleTitle: "QA Engineer",
    systemPrompt:
      "You are a QA engineer. You verify changes against acceptance criteria, hunt for regressions and edge cases, and report defects with precise reproduction steps.",
  },
  {
    id: "devops",
    name: "DevOps",
    roleTitle: "DevOps Engineer",
    systemPrompt:
      "You are a DevOps engineer. You keep builds, deployments, and infrastructure healthy, automate toil, and treat production access with extreme care.",
  },
  {
    id: "marketing-director",
    name: "Marketing Director",
    roleTitle: "Marketing Director",
    systemPrompt:
      "You are the marketing director. You own positioning and the content calendar, delegate drafts to writers, and review everything for voice and accuracy before publication.",
  },
  {
    id: "blogger",
    name: "Blogger",
    roleTitle: "Content Writer",
    systemPrompt:
      "You are a content writer. You produce clear, useful, well-structured articles for a technical audience and revise quickly on feedback.",
  },
  {
    id: "seo",
    name: "SEO Specialist",
    roleTitle: "SEO Specialist",
    systemPrompt:
      "You are an SEO specialist. You research keywords, audit content for search performance, and propose concrete on-page improvements without keyword stuffing.",
  },
  {
    id: "generic",
    name: "Generic Assistant",
    roleTitle: "Assistant",
    systemPrompt:
      "You are a capable, careful assistant. You complete assigned tasks precisely, state your assumptions, and ask for clarification when a request is ambiguous.",
  },
];

const NAME_MAX = 200;
const ROLE_TITLE_MAX = 200;
const PROMPT_MAX = 100_000;
export const PURPOSE_MAX = 1000;
export const EXPERTISE_MAX_TAGS = 20;
export const EXPERTISE_TAG_MAX = 64;

/** Turn a comma/newline-separated tag string into the unique, trimmed list
 * the API expects for `expertise_json`. */
export function parseExpertise(input: string): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const raw of input.split(/[,\n]/)) {
    const tag = raw.trim();
    if (!tag) continue;
    const key = tag.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    tags.push(tag);
  }
  return tags;
}

/** Validation shared by the wizard and the agent settings form. */
export function validatePublicIdentity(publicPurpose: string, expertise: string): string[] {
  const errors: string[] = [];
  if (publicPurpose.length > PURPOSE_MAX) {
    errors.push(`Purpose must be at most ${PURPOSE_MAX} characters.`);
  }
  const tags = parseExpertise(expertise);
  if (tags.length > EXPERTISE_MAX_TAGS) {
    errors.push(`At most ${EXPERTISE_MAX_TAGS} expertise tags.`);
  }
  if (tags.some((tag) => tag.length > EXPERTISE_TAG_MAX)) {
    errors.push(`Each expertise tag must be at most ${EXPERTISE_TAG_MAX} characters.`);
  }
  return errors;
}

export function validateIdentity(state: WizardState): string[] {
  const errors: string[] = [];
  if (state.name.trim().length === 0) errors.push("Name is required.");
  if (state.name.length > NAME_MAX) errors.push(`Name must be at most ${NAME_MAX} characters.`);
  if (state.roleTitle.length > ROLE_TITLE_MAX) {
    errors.push(`Role title must be at most ${ROLE_TITLE_MAX} characters.`);
  }
  errors.push(...validatePublicIdentity(state.publicPurpose, state.expertise));
  return errors;
}

function validateInstructions(state: WizardState): string[] {
  const errors: string[] = [];
  if (state.systemPrompt.length > PROMPT_MAX) {
    errors.push("Instructions are too long.");
  }
  return errors;
}

function boundedInt(raw: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(raw.trim())) return null;
  const value = Number(raw.trim());
  return value >= min && value <= max ? value : null;
}

export const MAX_STEPS_MAX = 500;
export const MAX_RUN_MINUTES_MAX = 1440;
export const MAX_CONCURRENT_RUNS_MAX = 50;

/** The budget field as an integer cent amount, or null for "no budget".
 * Returns undefined when the input is not a usable dollar amount. */
export function monthlyBudgetCents(raw: string): number | null | undefined {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return undefined;
  return Math.round(Number(trimmed) * 100);
}

export function validateLimits(state: WizardState): string[] {
  const errors: string[] = [];
  if (boundedInt(state.maxSteps, 1, MAX_STEPS_MAX) === null) {
    errors.push(`Max steps must be a whole number between 1 and ${MAX_STEPS_MAX}.`);
  }
  if (boundedInt(state.maxRunMinutes, 1, MAX_RUN_MINUTES_MAX) === null) {
    errors.push(`Max run minutes must be a whole number between 1 and ${MAX_RUN_MINUTES_MAX}.`);
  }
  if (boundedInt(state.maxConcurrentRuns, 1, MAX_CONCURRENT_RUNS_MAX) === null) {
    errors.push(
      `Max concurrent runs must be a whole number between 1 and ${MAX_CONCURRENT_RUNS_MAX}.`,
    );
  }
  if (monthlyBudgetCents(state.monthlyBudgetDollars) === undefined) {
    errors.push("Monthly budget must be a dollar amount (e.g. 5 or 5.50), or blank for no budget.");
  }
  return errors;
}

export function validateStep(step: number, state: WizardState): string[] {
  switch (step) {
    case 1:
      return validateIdentity(state);
    case ADVANCED_STEP:
      return [...validateInstructions(state), ...validateLimits(state)];
    default:
      return [];
  }
}

/** A step is reachable when every prior step validates. */
export function firstInvalidStep(state: WizardState): number | null {
  for (const step of WIZARD_STEPS) {
    if (step.id === REVIEW_STEP) break;
    if (validateStep(step.id, state).length > 0) return step.id;
  }
  return null;
}

export function canSubmit(state: WizardState): boolean {
  return firstInvalidStep(state) === null;
}

/** Request body for POST /agents from the wizard state. Always includes the
 * free shape avatar (picked or name-derived) so new agents are cubes. */
export function toCreatePayload(state: WizardState): Record<string, unknown> {
  const avatar = effectiveAvatar(state);
  return {
    name: state.name.trim(),
    role_title: state.roleTitle.trim(),
    description: state.description.trim(),
    public_purpose: state.publicPurpose.trim(),
    expertise_json: parseExpertise(state.expertise),
    system_prompt: state.systemPrompt,
    team_id: state.teamId || null,
    manager_agent_id: state.managerAgentId || null,
    model_profile_id: state.modelProfileId || null,
    autonomy_level: state.autonomyLevel,
    avatar_shape: avatar.shape,
    avatar_color: avatar.color,
    max_steps: Number(state.maxSteps),
    max_run_minutes: Number(state.maxRunMinutes),
    max_concurrent_runs: Number(state.maxConcurrentRuns),
    monthly_budget_cents: monthlyBudgetCents(state.monthlyBudgetDollars) ?? null,
  };
}

/** Toggle a capability in the wizard's grant list (pure, unit-tested). */
/** Fields a template fills in. A role title the user typed themselves is
 * kept; only an empty one or one set by another template is replaced. */
export function applyTemplate(
  state: Pick<WizardState, "name" | "roleTitle">,
  template: AgentTemplate,
): Pick<WizardState, "name" | "roleTitle" | "systemPrompt"> {
  const ownTitle =
    state.roleTitle.trim() !== "" && !AGENT_TEMPLATES.some((t) => t.roleTitle === state.roleTitle);
  return {
    name: state.name || template.name,
    roleTitle: ownTitle ? state.roleTitle : template.roleTitle,
    systemPrompt: template.systemPrompt,
  };
}

/** Source id recorded for a grant the user picked or edited themselves. */
export const MANUAL_GRANT_SOURCE = "manual";

/** Who contributed a granted tool: preset ids, {@link MANUAL_GRANT_SOURCE},
 * or — for state that predates source tracking — nothing at all. */
export function grantSourcesFor(state: WizardState, toolName: string): string[] {
  return state.grantSources[toolName] ?? [];
}

function withSource(
  sources: Record<string, string[]>,
  toolName: string,
  source: string,
): Record<string, string[]> {
  const current = sources[toolName] ?? [];
  if (current.includes(source)) return sources;
  return { ...sources, [toolName]: [...current, source] };
}

/** A grant the user picked or edited by hand — including any grant with no
 * recorded source, which can only have come from a hand edit. */
export function isManualGrant(state: WizardState, toolName: string): boolean {
  if (!state.grantToolNames.includes(toolName)) return false;
  const sources = grantSourcesFor(state, toolName);
  return sources.length === 0 || sources.includes(MANUAL_GRANT_SOURCE);
}

/** Granted tools that no preset alone accounts for (in grant order). */
export function manualGrantNames(state: WizardState): string[] {
  return state.grantToolNames.filter((name) => isManualGrant(state, name));
}

export function hasManualGrants(state: WizardState): boolean {
  return manualGrantNames(state).length > 0;
}

/** Check/uncheck one tool by hand. Unchecking always wins: the tool leaves
 * the grant list even if a preset put it there, and the presets that did are
 * forgotten so their buttons stop reading as applied. */
export function toggleTool(state: WizardState, toolName: string): WizardState {
  const has = state.grantToolNames.includes(toolName);
  if (has) {
    const grantSources = { ...state.grantSources };
    delete grantSources[toolName];
    return {
      ...state,
      grantToolNames: state.grantToolNames.filter((entry) => entry !== toolName),
      grantSources,
    };
  }
  return {
    ...state,
    grantToolNames: [...state.grantToolNames, toolName],
    grantSources: withSource(state.grantSources, toolName, MANUAL_GRANT_SOURCE),
  };
}

/** Edit one tool's grant scope by hand. The edit marks the grant manual so
 * turning a preset off later keeps the tool and the values the user typed. */
export function setToolScope(
  state: WizardState,
  toolName: string,
  values: ToolScopeValues,
): WizardState {
  return {
    ...state,
    grantScopes: { ...state.grantScopes, [toolName]: values },
    grantSources: state.grantToolNames.includes(toolName)
      ? withSource(state.grantSources, toolName, MANUAL_GRANT_SOURCE)
      : state.grantSources,
  };
}

export function grantPayloadsForTools(
  state: WizardState,
  tools: ToolInfo[],
): { capability: string; scope: Record<string, string>; effect: "allow" }[] {
  const selected = new Set(state.grantToolNames);
  const seen = new Set<string>();
  return tools.flatMap((tool) => {
    if (!selected.has(tool.name)) return [];
    const scope = buildToolScope(tool, state.grantScopes[tool.name] ?? {});
    const key = `${tool.required_capability}\0${JSON.stringify(Object.entries(scope).sort())}`;
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ capability: tool.required_capability, scope, effect: "allow" as const }];
  });
}

/** A one-click bundle of tool grants for a common job (plan 12.3). Scopes
 * that need a connection are filled from the workspace's connections when
 * exactly one matching connection exists; the user can still edit them. */
export interface ToolPreset {
  id: string;
  label: string;
  description: string;
  /** One plain-language line for the "what this agent will be able to do"
   * summary and the review step — no tool names, no jargon. */
  summary: string;
  /** Tool name → scope values (the connection id is filled in separately). */
  tools: Record<string, ToolScopeValues>;
}

/** The id of the safe-by-default collaboration preset (applied to new
 * agents unless the creator toggles it off). */
export const COLLABORATION_PRESET_ID = "collaboration";

export const TOOL_PRESETS: ToolPreset[] = [
  {
    id: COLLABORATION_PRESET_ID,
    label: "Collaboration",
    summary:
      "Work with teammates: find colleagues, see what they are working on, ask them for help, and answer their requests",
    description:
      "Let this agent look colleagues up in the directory, see what a teammate is working on right now, ask any teammate for help with a piece of work (they decide whether to accept), and respond to requests addressed to it. This is safe by default: it can only read public work status — never a colleague's instructions, permissions, notes, or conversations — and a request only asks, so it can never make a colleague do anything they are not already allowed to do. It is on by default for new agents. It does NOT include delegation, which transfers authority and stays off unless an admin grants it.",
    tools: {
      "organization.directory.search": {},
      "organization.colleague_status": {},
      "organization.request_work": { targets: "any" },
      "organization.respond_work_request": {},
    },
  },
  {
    id: "code-editing",
    label: "Code editing",
    summary: "Write code: check out a repo, edit files, run tests, and open pull requests",
    description:
      "Clone a repository into the sandbox, read and edit files, run tests, commit and push with git, and open pull requests. Needs a CLI Sandbox connection and a GitHub connection.",
    tools: {
      "cli.repository.checkout": { repository: "*" },
      "cli.file.read": { path: "*" },
      "cli.file.write": { path: "*" },
      "cli.test.run": { command: "*" },
      "cli.command.execute": { command: "git *" },
      "github.repository.read": { repository: "*" },
      "github.pull_request.read": { repository: "*" },
      "github.pull_request.create": { repository: "*" },
    },
  },
  {
    id: "team-building",
    label: "Team building",
    summary: "Hire teammates: create agents and teams (each new hire needs your approval)",
    description:
      "Create new AI teammates and teams, update teammate profiles, and look up colleagues in the directory. A human must approve each new teammate, and new teammates start with no tool access.",
    tools: {
      "organization.create_agent": {},
      "organization.update_agent_profile": {},
      "organization.create_team": {},
      "organization.directory.search": {},
    },
  },
  {
    id: "web-access",
    label: "Web search & browsing",
    summary: "Research online: search the web and read public pages",
    description:
      "Search the web and read public pages through a Web connection. Everything that comes back is untrusted external content; fetch can be limited to specific domains.",
    tools: {
      "web.search": {},
      "web.fetch": { domain: "*" },
    },
  },
  {
    id: "skills",
    label: "Skills",
    summary: "Follow playbooks: read the skills your workspace publishes",
    description:
      "Let this agent read the workspace skills library — the playbooks your admins curate. Grants the skills.read tool for every skill.",
    tools: { "skills.read": { name: "*" } },
  },
  {
    id: "skill-authoring",
    label: "Skill authoring",
    summary: "Write playbooks: add and revise skills it wrote itself (with approval)",
    description:
      "Let this agent write new playbooks to the skills library and revise ones it wrote, through skills.create and skills.update. Every call needs your approval first, and it can only ever touch skills it authored itself — never anyone else's.",
    tools: { "skills.create": {}, "skills.update": {} },
  },
];

/** The slice of a workspace connection a preset needs. */
export interface PresetConnection {
  id: string;
  connector_type: string;
  status: string;
}

/** The connection a preset scope should pin for a tool: the only active
 * connection of the tool's connector type, else nothing (left to the user). */
export function presetConnectionFor(
  toolName: string,
  connections: PresetConnection[],
): string {
  const type = toolName.split(".", 1)[0];
  const matching = connections.filter((c) => c.connector_type === type && c.status === "active");
  return matching.length === 1 ? matching[0].id : "";
}

export function applyToolPreset(
  state: WizardState,
  preset: ToolPreset,
  tools: Pick<ToolInfo, "name" | "scope_keys">[],
  connections: PresetConnection[],
): WizardState {
  const catalog = new Map(tools.map((tool) => [tool.name, tool]));
  const names = [...state.grantToolNames];
  const scopes: Record<string, ToolScopeValues> = { ...state.grantScopes };
  let sources = state.grantSources;
  for (const [toolName, presetScope] of Object.entries(preset.tools)) {
    const tool = catalog.get(toolName);
    if (!tool) continue;
    if (!names.includes(toolName)) names.push(toolName);
    sources = withSource(sources, toolName, preset.id);
    const values: ToolScopeValues = { ...(scopes[toolName] ?? {}) };
    for (const key of tool.scope_keys) {
      if (values[key]) continue;
      if (key === "connection_id") {
        const id = presetConnectionFor(toolName, connections);
        if (id) values[key] = id;
      } else if (presetScope[key]) {
        values[key] = presetScope[key];
      }
    }
    scopes[toolName] = values;
  }
  return { ...state, grantToolNames: names, grantScopes: scopes, grantSources: sources };
}

/**
 * The inverse of {@link applyToolPreset}: drops this preset's claim on every
 * tool it contributed. A tool another preset still needs — or one the user
 * checked or scoped by hand — stays, with its scope untouched.
 */
export function removeToolPreset(state: WizardState, preset: ToolPreset): WizardState {
  const names = [...state.grantToolNames];
  const scopes: Record<string, ToolScopeValues> = { ...state.grantScopes };
  const sources: Record<string, string[]> = { ...state.grantSources };
  for (const toolName of Object.keys(preset.tools)) {
    const remaining = (sources[toolName] ?? []).filter((source) => source !== preset.id);
    if (remaining.length > 0) {
      sources[toolName] = remaining;
      continue;
    }
    delete sources[toolName];
    // Only the preset wanted this tool: drop the grant and its scope. A tool
    // with no recorded source at all was never this preset's to remove.
    if ((state.grantSources[toolName] ?? []).includes(preset.id)) {
      const index = names.indexOf(toolName);
      if (index >= 0) names.splice(index, 1);
      delete scopes[toolName];
    }
  }
  return { ...state, grantToolNames: names, grantScopes: scopes, grantSources: sources };
}

/** True when this preset is the recorded source of every tool it can grant
 * in this workspace's catalog. Source-based, so a leftover tool the user
 * kept by hand does not make a removed preset look applied again. */
export function isPresetApplied(
  state: WizardState,
  preset: ToolPreset,
  tools: Pick<ToolInfo, "name">[],
): boolean {
  const known = new Set(tools.map((tool) => tool.name));
  const available = Object.keys(preset.tools).filter((name) => known.has(name));
  if (available.length === 0) return false;
  return available.every(
    (name) =>
      state.grantToolNames.includes(name) && grantSourcesFor(state, name).includes(preset.id),
  );
}

/** Turn a preset on when it is off and off when it is on. */
export function toggleToolPreset(
  state: WizardState,
  preset: ToolPreset,
  tools: Pick<ToolInfo, "name" | "scope_keys">[],
  connections: PresetConnection[],
): WizardState {
  return isPresetApplied(state, preset, tools)
    ? removeToolPreset(state, preset)
    : applyToolPreset(state, preset, tools, connections);
}

/** What the agent will be able to do, in plain language: the presets that
 * are on, plus the tools the user picked individually. */
export function capabilitySummary(
  state: WizardState,
  tools: Pick<ToolInfo, "name">[],
  presets: ToolPreset[] = TOOL_PRESETS,
): { presets: ToolPreset[]; manualToolNames: string[] } {
  const applied = presets.filter((preset) => isPresetApplied(state, preset, tools));
  const fromPresets = new Set(
    applied.flatMap((preset) => Object.keys(preset.tools)),
  );
  return {
    presets: applied,
    manualToolNames: state.grantToolNames.filter((name) => !fromPresets.has(name)),
  };
}

/* -------------------------------------------------------------------------
 * Presets on the agent *edit* surface, where saved grants — not wizard state —
 * are the source of truth, so a preset's state is read back from the grants.
 * ---------------------------------------------------------------------- */

/** The capabilities a preset needs, given this workspace's tool catalog. */
export function presetCapabilities(
  preset: ToolPreset,
  tools: Pick<ToolInfo, "name" | "required_capability">[],
): string[] {
  const wanted = new Set(Object.keys(preset.tools));
  return [
    ...new Set(
      tools.filter((tool) => wanted.has(tool.name)).map((tool) => tool.required_capability),
    ),
  ];
}

/** True when every capability the preset needs is already allow-granted. */
export function isPresetGranted(
  grants: Grant[],
  preset: ToolPreset,
  tools: Pick<ToolInfo, "name" | "required_capability">[],
): boolean {
  const capabilities = presetCapabilities(preset, tools);
  if (capabilities.length === 0) return false;
  return capabilities.every((capability) => isCapabilityGranted(grants, capability));
}

/** The grant bodies to POST so a preset's capabilities are all covered.
 * Capabilities that are already granted are left alone. */
export function presetGrantsToAdd(
  grants: Grant[],
  preset: ToolPreset,
  tools: ToolInfo[],
  connections: PresetConnection[],
): { capability: string; scope: Record<string, string>; effect: "allow" }[] {
  const applied = applyToolPreset(EMPTY_WIZARD, preset, tools, connections);
  return grantPayloadsForTools(applied, tools).filter(
    (payload) => !isCapabilityGranted(grants, payload.capability),
  );
}

/** The grants to revoke when a preset is switched off: every allow grant for
 * a capability this preset owns, minus the capabilities another preset that
 * is still on also needs. Turning a capability off means off — a narrower
 * hand-made grant for the same capability would silently keep it alive. */
export function presetGrantsToRevoke(
  grants: Grant[],
  preset: ToolPreset,
  tools: Pick<ToolInfo, "name" | "required_capability">[],
  keepPresets: ToolPreset[],
): Grant[] {
  const owned = new Set(presetCapabilities(preset, tools));
  for (const other of keepPresets) {
    if (other.id === preset.id) continue;
    for (const capability of presetCapabilities(other, tools)) owned.delete(capability);
  }
  return grants.filter((grant) => grant.effect === "allow" && owned.has(grant.capability));
}

/** Preset tools whose required grant scope keys cannot be filled in from the
 * preset and the workspace's connections (e.g. no unambiguous connection). */
export function presetScopeGaps(
  preset: ToolPreset,
  tools: ToolInfo[],
  connections: PresetConnection[],
): string[] {
  const applied = applyToolPreset(EMPTY_WIZARD, preset, tools, connections);
  return tools
    .filter(
      (tool) =>
        applied.grantToolNames.includes(tool.name) &&
        missingRequiredScopeKeys(tool, applied.grantScopes[tool.name] ?? {}).length > 0,
    )
    .map((tool) => tool.name);
}

/** Preset tools missing from the catalog (e.g. a connector not installed). */
export function presetMissingTools(preset: ToolPreset, tools: Pick<ToolInfo, "name">[]): string[] {
  const known = new Set(tools.map((tool) => tool.name));
  return Object.keys(preset.tools).filter((name) => !known.has(name));
}
