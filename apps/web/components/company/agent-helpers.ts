/**
 * Pure helpers for the agent directory, profiles, and company overview:
 * plain-language status, purpose fallbacks, grant descriptions, and
 * relationship wording. React-free and unit-tested.
 */

import type {
  AgentIdentity,
  AgentPolicy,
  AgentRelationship,
  AgentStatus,
  Grant,
  ToolInfo,
} from "@/lib/types";

export interface AgentLike extends AgentIdentity {
  id: string;
  name: string;
  role_title: string;
  status: AgentStatus;
  description?: string;
  team_id: string | null;
  manager_agent_id: string | null;
}

export type StatusTone = "ok" | "warn" | "neutral" | "accent" | "danger";

export interface StatusText {
  label: string;
  tone: StatusTone;
}

/** Plain-language availability for an agent. `working` wins when the agent
 * currently has a running task. */
export function statusTextOf(agent: Pick<AgentLike, "status" | "availability">, working = false): StatusText {
  if (agent.status === "disabled") return { label: "Turned off", tone: "neutral" };
  if (agent.status === "paused") return { label: "Paused", tone: "warn" };
  if (working) return { label: "Working now", tone: "accent" };
  if (agent.availability === "unavailable") return { label: "Away", tone: "warn" };
  return { label: "Available", tone: "ok" };
}

/** Public purpose, falling back to the first sentence of the description. */
export function purposeOf(agent: Pick<AgentLike, "public_purpose" | "description">): string {
  const purpose = (agent.public_purpose ?? "").trim();
  if (purpose) return purpose;
  const description = (agent.description ?? "").trim();
  if (!description) return "";
  const match = /^(.+?[.!?])(\s|$)/.exec(description);
  return match ? match[1] : description;
}

export function expertiseOf(agent: Pick<AgentLike, "expertise_json">): string[] {
  return (agent.expertise_json ?? []).filter((tag) => tag.trim().length > 0);
}

/** Case-insensitive search over name, role, and expertise. */
export function matchesSearch(agent: AgentLike, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const haystack = [agent.name, agent.role_title, purposeOf(agent), ...expertiseOf(agent)]
    .join(" ")
    .toLowerCase();
  return haystack.includes(needle);
}

export type AvailabilityFilter = "all" | "available" | "working" | "paused";

export function matchesAvailability(
  agent: AgentLike,
  filter: AvailabilityFilter,
  working: boolean,
): boolean {
  switch (filter) {
    case "all":
      return true;
    case "available":
      return agent.status === "active" && agent.availability !== "unavailable";
    case "working":
      return working;
    case "paused":
      return agent.status !== "active";
  }
}

/** Relationship sentence from `agentId`'s point of view. Collaborators are
 * symmetric; advisor and reviewer links are directed source → target. */
export function relationshipLabel(relationship: AgentRelationship, agentId: string): string {
  const isSource = relationship.source_agent_id === agentId;
  switch (relationship.kind) {
    case "close_collaborator":
      return "Works closely with";
    case "advisor":
      return isSource ? "Gets advice from" : "Advises";
    case "preferred_reviewer":
      return isSource ? "Prefers reviews from" : "Preferred reviewer for";
  }
}

export function relationshipOtherId(relationship: AgentRelationship, agentId: string): string {
  return relationship.source_agent_id === agentId
    ? relationship.target_agent_id
    : relationship.source_agent_id;
}

export const PRESET_FRIENDLY: Record<string, string> = {
  autonomous: "Acts on its own; only destructive actions wait for you",
  balanced: "Asks before risky actions",
  restricted: "Asks before changing anything; never runs destructive actions",
};

export function policySummary(policy: AgentPolicy | undefined): string {
  if (!policy) return "Asks before risky actions";
  if (policy.preset && PRESET_FRIENDLY[policy.preset]) return PRESET_FRIENDLY[policy.preset];
  if (policy.rules.length === 0) return "Uses the workspace defaults";
  const needsApproval = policy.rules.some((rule) => rule.action === "approval");
  const forbids = policy.rules.some((rule) => rule.action === "forbid");
  if (forbids) return "Custom rules; some actions are never allowed";
  return needsApproval ? "Custom rules; asks before some actions" : "Custom rules; runs without asking";
}

const VERB_WORDS: Record<string, string> = {
  read: "read",
  list: "read",
  get: "read",
  search: "read",
  create: "create",
  write: "write",
  update: "update",
  delete: "delete",
  execute: "run",
  run: "run",
  checkout: "check out",
  delegate: "delegate work",
  delegate_task: "delegate work",
};

function titleCase(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

function humanizeSegment(segment: string): string {
  return segment.replace(/_/g, " ");
}

function pluralize(noun: string): string {
  if (/[^aeiou]y$/.test(noun)) return `${noun.slice(0, -1)}ies`;
  if (/(s|x|z|ch|sh)$/.test(noun)) return `${noun}es`;
  return `${noun}s`;
}

/** Plain-language grant, e.g. "GitHub: read repositories octo/*" or
 * "Everything (all tools)". Never shows raw capability strings; callers put
 * those under Advanced. */
export function describeGrant(
  grant: Grant,
  tools: ToolInfo[],
  connectionNames: Record<string, string> = {},
): string {
  const capability = grant.capability;
  if (capability === "*") return grant.effect === "deny" ? "Nothing (all tools blocked)" : "Everything (all tools)";
  const [app, ...rest] = capability.split(".");
  const appLabel = APP_LABELS[app] ?? titleCase(humanizeSegment(app));
  const matching = tools.filter((tool) => tool.required_capability === capability);
  const nouns = rest.slice(0, -1).map(humanizeSegment);
  const verbRaw = rest.length ? rest[rest.length - 1] : "*";
  let action: string;
  if (verbRaw === "*") {
    action = nouns.length ? `use ${nouns.map(pluralize).join(" ")}` : "use everything";
  } else {
    const verb = VERB_WORDS[verbRaw];
    if (verb) {
      action = nouns.length ? `${verb} ${nouns.map(pluralize).join(" ")}` : verb;
    } else if (matching.length === 1 && matching[0].description) {
      action = matching[0].description.replace(/\.$/, "").toLowerCase();
    } else {
      action = `${humanizeSegment(verbRaw)}${nouns.length ? ` ${nouns.join(" ")}` : ""}`;
    }
  }
  const scopeBits: string[] = [];
  const scope = grant.scope_json ?? {};
  for (const [key, value] of Object.entries(scope)) {
    if (value === undefined || value === null || value === "") continue;
    if (key === "connection_id") {
      const name = connectionNames[String(value)];
      if (name) scopeBits.push(`via ${name}`);
      continue;
    }
    // "repository octo/*" reads better as "repositories octo/*" when the noun already appears.
    if (nouns.includes(humanizeSegment(key))) {
      action = action.replace(pluralize(humanizeSegment(key)), `${pluralize(humanizeSegment(key))} ${String(value)}`);
      continue;
    }
    scopeBits.push(`${humanizeSegment(key)} ${String(value)}`);
  }
  const prefix = grant.effect === "deny" ? "Not allowed to " : "";
  const body = `${prefix}${action}${scopeBits.length ? ` ${scopeBits.join(", ")}` : ""}`;
  return `${appLabel}: ${body}`;
}

export const APP_LABELS: Record<string, string> = {
  github: "GitHub",
  linear: "Linear",
  cli: "Command line",
  vercel: "Vercel",
  organization: "Company",
  system: "System",
  http: "HTTP",
  slack: "Slack",
};

export const TRIGGER_FRIENDLY_EVENTS: Record<string, string> = {
  "connector.linear.issue.created": "a Linear issue is created",
  "connector.linear.issue.updated": "a Linear issue changes",
  "connector.github.pull_request.opened": "a GitHub pull request opens",
  "connector.github.pull_request.merged": "a GitHub pull request merges",
  "connector.github.push": "code is pushed to GitHub",
  "connector.github.issue.opened": "a GitHub issue opens",
};

/** "When … → then …" sentence for an automation card. */
export function triggerWhen(eventType: string | null, connectionName: string | undefined): string {
  if (!eventType) return connectionName ? `anything happens in ${connectionName}` : "anything happens";
  const known = TRIGGER_FRIENDLY_EVENTS[eventType];
  if (known) return known;
  const parts = eventType.replace(/^connector\./, "").split(".");
  const app = APP_LABELS[parts[0]] ?? titleCase(parts[0] ?? "");
  const what = parts.slice(1).map(humanizeSegment).join(" ");
  return what ? `${what} in ${app}` : `something happens in ${app}`;
}
