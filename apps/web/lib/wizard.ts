/**
 * Agent creation wizard model (plan 17.6): step definitions, role templates,
 * and pure validation functions (unit-tested).
 */

import type { ApprovalPreset, AutonomyLevel, ToolInfo } from "@/lib/types";
import { buildToolScope, type ToolScopeValues } from "@/lib/connectors";
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
}

export const WIZARD_STEPS: WizardStep[] = [
  { id: 1, title: "Identity" },
  { id: 2, title: "Role & instructions" },
  { id: 3, title: "Team & manager" },
  { id: 4, title: "Model" },
  { id: 5, title: "Tools & connections" },
  { id: 6, title: "Autonomy & approvals" },
  { id: 7, title: "Limits & budget" },
  { id: 8, title: "Review" },
];

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
    case 2:
      return validateInstructions(state);
    case 7:
      return validateLimits(state);
    default:
      return [];
  }
}

/** A step is reachable when every prior step validates. */
export function firstInvalidStep(state: WizardState): number | null {
  for (const step of WIZARD_STEPS) {
    if (step.id === 8) break;
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

export function toggleTool(state: WizardState, toolName: string): WizardState {
  const has = state.grantToolNames.includes(toolName);
  return {
    ...state,
    grantToolNames: has
      ? state.grantToolNames.filter((entry) => entry !== toolName)
      : [...state.grantToolNames, toolName],
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
  /** Tool name → scope values (the connection id is filled in separately). */
  tools: Record<string, ToolScopeValues>;
}

export const TOOL_PRESETS: ToolPreset[] = [
  {
    id: "code-editing",
    label: "Code editing",
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
  for (const [toolName, presetScope] of Object.entries(preset.tools)) {
    const tool = catalog.get(toolName);
    if (!tool) continue;
    if (!names.includes(toolName)) names.push(toolName);
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
  return { ...state, grantToolNames: names, grantScopes: scopes };
}

/** Preset tools missing from the catalog (e.g. a connector not installed). */
export function presetMissingTools(preset: ToolPreset, tools: Pick<ToolInfo, "name">[]): string[] {
  const known = new Set(tools.map((tool) => tool.name));
  return Object.keys(preset.tools).filter((name) => !known.has(name));
}
