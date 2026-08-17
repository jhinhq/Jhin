/**
 * Agent creation wizard model (plan 17.6): step definitions, role templates,
 * and pure validation functions (unit-tested).
 */

import type { ApprovalPreset, AutonomyLevel } from "@/lib/types";

export interface WizardState {
  name: string;
  roleTitle: string;
  description: string;
  systemPrompt: string;
  teamId: string;
  managerAgentId: string;
  /** Empty string = use the workspace default profile (plan 15.2). */
  modelProfileId: string;
  /** Capabilities to allow-grant right after creation (plan 12.3). */
  grantCapabilities: string[];
  /** Scope applied to connector-capability grants (plan 11): restrict to one
   * connection and optional repository glob. Empty = unscoped. */
  grantConnectionId: string;
  grantRepository: string;
  approvalPreset: ApprovalPreset;
  autonomyLevel: AutonomyLevel;
}

export const EMPTY_WIZARD: WizardState = {
  name: "",
  roleTitle: "",
  description: "",
  systemPrompt: "",
  teamId: "",
  managerAgentId: "",
  modelProfileId: "",
  grantCapabilities: [],
  grantConnectionId: "",
  grantRepository: "",
  approvalPreset: "balanced",
  autonomyLevel: "supervised",
};

export interface WizardStep {
  id: number;
  title: string;
  /** Steps delivered by a later phase are visible but not editable. */
  disabledPhase?: string;
}

export const WIZARD_STEPS: WizardStep[] = [
  { id: 1, title: "Identity" },
  { id: 2, title: "Role & instructions" },
  { id: 3, title: "Team & manager" },
  { id: 4, title: "Model" },
  { id: 5, title: "Tools & connections" },
  { id: 6, title: "Autonomy & approvals" },
  // Step/time limits are editable in the agent drawer today; budget
  // *enforcement* arrives in Phase 10.
  { id: 7, title: "Limits & budget", disabledPhase: "Phase 10" },
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

export const NAME_MAX = 200;
export const ROLE_TITLE_MAX = 200;
export const PROMPT_MAX = 100_000;

export function validateIdentity(state: WizardState): string[] {
  const errors: string[] = [];
  if (state.name.trim().length === 0) errors.push("Name is required.");
  if (state.name.length > NAME_MAX) errors.push(`Name must be at most ${NAME_MAX} characters.`);
  if (state.roleTitle.length > ROLE_TITLE_MAX) {
    errors.push(`Role title must be at most ${ROLE_TITLE_MAX} characters.`);
  }
  return errors;
}

export function validateInstructions(state: WizardState): string[] {
  const errors: string[] = [];
  if (state.systemPrompt.length > PROMPT_MAX) {
    errors.push("Instructions are too long.");
  }
  return errors;
}

export function validateStep(step: number, state: WizardState): string[] {
  switch (step) {
    case 1:
      return validateIdentity(state);
    case 2:
      return validateInstructions(state);
    default:
      return [];
  }
}

/** A step is reachable when every prior editable step validates. */
export function firstInvalidStep(state: WizardState): number | null {
  for (const step of WIZARD_STEPS) {
    if (step.disabledPhase) continue;
    if (step.id === 8) break;
    if (validateStep(step.id, state).length > 0) return step.id;
  }
  return null;
}

export function canSubmit(state: WizardState): boolean {
  return firstInvalidStep(state) === null;
}

/** Request body for POST /agents from the wizard state. */
export function toCreatePayload(state: WizardState): Record<string, unknown> {
  return {
    name: state.name.trim(),
    role_title: state.roleTitle.trim(),
    description: state.description.trim(),
    system_prompt: state.systemPrompt,
    team_id: state.teamId || null,
    manager_agent_id: state.managerAgentId || null,
    model_profile_id: state.modelProfileId || null,
    autonomy_level: state.autonomyLevel,
  };
}

/** Toggle a capability in the wizard's grant list (pure, unit-tested). */
export function toggleCapability(state: WizardState, capability: string): WizardState {
  const has = state.grantCapabilities.includes(capability);
  return {
    ...state,
    grantCapabilities: has
      ? state.grantCapabilities.filter((entry) => entry !== capability)
      : [...state.grantCapabilities, capability],
  };
}
