/**
 * Pure helpers for coordination screens: work requests, reviews, review
 * policies, and manager rollups. No React; unit-tested in
 * tests/coordination.test.ts.
 */

import type {
  ConversationMessage,
  ReviewCondition,
  ReviewConditionKind,
  ReviewMode,
  ReviewPolicy,
  ReviewPolicyIn,
  ReviewScopeKind,
  ReviewerKind,
  ReviewerSelector,
  RollupItem,
  WorkRequestStatus,
  WorkReview,
  WorkReviewStatus,
} from "@/lib/types";

/* ------------------------------------------------------------------ */
/* Work requests                                                       */
/* ------------------------------------------------------------------ */

export const WORK_REQUEST_STATUS: Record<
  WorkRequestStatus,
  { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" }
> = {
  pending: { label: "Waiting for an answer", tone: "warn" },
  clarification_requested: { label: "Asked for clarification", tone: "warn" },
  accepted: { label: "Accepted — working on it", tone: "accent" },
  declined: { label: "Declined", tone: "neutral" },
  completed: { label: "Done", tone: "ok" },
  failed: { label: "Couldn't finish", tone: "danger" },
};

export function workRequestStatus(status: string) {
  return WORK_REQUEST_STATUS[status as WorkRequestStatus] ?? { label: status.replace(/_/g, " "), tone: "neutral" as const };
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** Structured messages posted by the work-request flow carry `kind`. */
export function isWorkRequestMessage(message: Pick<ConversationMessage, "content_json">): boolean {
  return message.content_json.kind === "work_request";
}

/** Friendly title for a work-request message, by lifecycle status. */
export function workRequestMessageLabel(
  message: Pick<ConversationMessage, "content_json" | "sender_id">,
): string {
  const content = message.content_json;
  const target = str(content.target_agent_name) || "another agent";
  const from = str(content.from_agent_name);
  const fromTarget = content.target_agent_id && content.from_agent_id === content.target_agent_id;
  switch (str(content.status)) {
    case "pending":
      return `Asked ${target} for help`;
    case "clarification_requested":
      return fromTarget || from ? `${from || target} asked for clarification` : "Clarification requested";
    case "accepted":
      return `${from || target} accepted the request`;
    case "declined":
      return `${from || target} declined the request`;
    case "completed":
      return `${from || target} finished and reported back`;
    case "failed":
      return `${from || target} couldn't finish the request`;
    default:
      return `Help request to ${target}`;
  }
}

/* ------------------------------------------------------------------ */
/* Reviews                                                             */
/* ------------------------------------------------------------------ */

export const REVIEW_MODE_LABELS: Record<ReviewMode, string> = {
  pre_action: "Before a risky action",
  before_close: "Before finishing work",
  post_action: "After an action",
  periodic: "On a schedule",
};

export const REVIEW_MODE_HELP: Record<ReviewMode, string> = {
  pre_action: "Pauses the agent right before the action and waits for the reviewer.",
  before_close: "Pauses before the work is marked done so someone can check the result.",
  post_action: "Doesn't pause anything; the reviewer sees it afterwards.",
  periodic: "Checks in on a regular interval regardless of what the agent is doing.",
};

export const REVIEW_STATUS: Record<
  WorkReviewStatus,
  { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" }
> = {
  pending: { label: "Waiting for a decision", tone: "warn" },
  approved: { label: "Looks good", tone: "ok" },
  changes_requested: { label: "Needs changes", tone: "danger" },
  skipped: { label: "Skipped", tone: "neutral" },
  escalated: { label: "Escalated", tone: "warn" },
};

export function reviewStatus(status: string) {
  return REVIEW_STATUS[status as WorkReviewStatus] ?? { label: status.replace(/_/g, " "), tone: "neutral" as const };
}

/** Verdict wording shared by transcript cards and activity summaries. */
export function reviewVerdictLabel(verdict: string): string | null {
  switch (verdict) {
    case "pass":
    case "approve":
    case "approved":
      return "looks good";
    case "fail":
    case "changes_requested":
      return "needs changes";
    case "escalate":
    case "escalated":
      return "escalated";
    default:
      return null;
  }
}

/** One plain sentence describing what a review is about. */
export function reviewSubject(review: Pick<WorkReview, "subject_agent_name" | "task_title" | "mode" | "evidence_json">): string {
  const who = review.subject_agent_name ?? "An agent";
  const evidence = review.evidence_json ?? {};
  const tool = str(evidence.tool_name);
  const summary = str(evidence.summary);
  if (summary) return summary;
  if (review.mode === "pre_action" && tool) return `${who} wants to use ${humanizeToolName(tool)}`;
  if (review.task_title) return `${who} finished “${review.task_title}” and needs a check`;
  return `${who}'s work needs a check`;
}

export function humanizeToolName(tool: string): string {
  return tool.replace(/[._]/g, " ");
}

/** Readable reason list from `evidence_json.matched_conditions`. */
export function matchedConditionLabels(evidence: Record<string, unknown>): string[] {
  const raw = evidence.matched_conditions;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => {
      if (typeof item === "string") return conditionLabel(item);
      if (typeof item === "object" && item !== null) return conditionLabel(str((item as Record<string, unknown>).kind));
      return "";
    })
    .filter(Boolean);
}

/* ------------------------------------------------------------------ */
/* Review policies                                                     */
/* ------------------------------------------------------------------ */

export interface ConditionSpec {
  kind: ReviewConditionKind;
  label: string;
  help: string;
  /** Threshold unit when the condition takes one. */
  unit?: "dollars" | "tokens" | "minutes" | "confidence";
  defaultThreshold?: number;
}

export const CONDITION_SPECS: ConditionSpec[] = [
  { kind: "always", label: "Every time", help: "Review all work covered by this policy." },
  { kind: "elevated_action", label: "Risky actions", help: "Anything marked elevated risk, like sending messages outside the team." },
  { kind: "destructive_action", label: "Destructive actions", help: "Deleting, merging, or anything that can't be undone." },
  { kind: "cost_threshold", label: "Spending above a limit", help: "When a run costs more than this.", unit: "dollars", defaultThreshold: 1 },
  { kind: "token_threshold", label: "Using lots of tokens", help: "When a run uses more tokens than this.", unit: "tokens", defaultThreshold: 100_000 },
  { kind: "time_threshold", label: "Taking too long", help: "When a run takes longer than this.", unit: "minutes", defaultThreshold: 30 },
  { kind: "tool_failure", label: "A tool failed", help: "A tool call returned an error." },
  { kind: "test_failure", label: "Tests failed", help: "The agent reported failing tests." },
  { kind: "approval_denied", label: "A human said no", help: "An approval was rejected." },
  { kind: "policy_denied", label: "Blocked by policy", help: "The agent tried something it isn't allowed to do." },
  { kind: "blocked", label: "The agent is stuck", help: "It reported being blocked." },
  { kind: "low_confidence", label: "Low confidence", help: "The agent wasn't sure about its result.", unit: "confidence", defaultThreshold: 0.5 },
  { kind: "cross_team_request", label: "Help from another team", help: "A work request crossed team lines." },
  { kind: "explicit_request", label: "The agent asked for one", help: "The agent explicitly requested a review." },
];

export function conditionLabel(kind: string): string {
  return CONDITION_SPECS.find((spec) => spec.kind === kind)?.label ?? kind.replace(/_/g, " ");
}

/** Short summary of one stored condition, with its threshold in human units. */
export function describeCondition(condition: ReviewCondition): string {
  const spec = CONDITION_SPECS.find((item) => item.kind === condition.kind);
  if (!spec) return condition.kind.replace(/_/g, " ");
  if (!spec.unit || condition.threshold === null || condition.threshold === undefined) return spec.label;
  return `${spec.label} (${formatThreshold(spec.unit, condition.threshold)})`;
}

export function formatThreshold(unit: NonNullable<ConditionSpec["unit"]>, raw: number): string {
  switch (unit) {
    case "dollars":
      return `over $${(raw / 1_000_000).toFixed(2)}`;
    case "tokens":
      return `over ${raw.toLocaleString()} tokens`;
    case "minutes":
      return `over ${Math.round(raw / 60)} min`;
    case "confidence":
      return `under ${Math.round(raw * 100)}%`;
  }
}

/** Convert the human-units value typed in the form to what the API stores. */
export function thresholdToRaw(unit: NonNullable<ConditionSpec["unit"]>, human: number): number {
  switch (unit) {
    case "dollars":
      return Math.round(human * 1_000_000);
    case "minutes":
      return Math.round(human * 60);
    case "confidence":
      return human > 1 ? human / 100 : human;
    default:
      return Math.round(human);
  }
}

export function rawToThreshold(unit: NonNullable<ConditionSpec["unit"]>, raw: number): number {
  switch (unit) {
    case "dollars":
      return raw / 1_000_000;
    case "minutes":
      return raw / 60;
    default:
      return raw;
  }
}

export const SCOPE_KIND_LABELS: Record<ReviewScopeKind, string> = {
  workspace: "Everyone",
  team: "One team",
  agent: "One agent",
  task_type: "A kind of task",
};

export const REVIEWER_KIND_LABELS: Record<ReviewerKind, string> = {
  reporting_manager: "The agent's manager",
  agent: "A specific agent",
  team_role: "Someone on the team with a role",
  human: "A person",
};

export function describeReviewer(
  selector: ReviewerSelector,
  agentName?: (id: string) => string | undefined,
): string {
  const name = (id: string | null | undefined) => (id ? (agentName?.(id) ?? "an agent") : "an agent");
  let primary: string;
  switch (selector.kind) {
    case "reporting_manager":
      primary = "the agent's manager";
      break;
    case "agent":
      primary = name(selector.agent_id);
      break;
    case "team_role":
      primary = selector.role_label ? `a teammate with the role “${selector.role_label}”` : "a teammate";
      break;
    case "human":
      primary = "a person";
      break;
    default:
      primary = "a reviewer";
  }
  const parts = [primary];
  if (selector.fallback_agent_id) parts.push(`otherwise ${name(selector.fallback_agent_id)}`);
  if (selector.kind !== "human" && selector.fallback_to_human !== false) parts.push("otherwise a person");
  return parts.join(", ");
}

export function describeScope(
  policy: Pick<ReviewPolicy, "scope_kind" | "scope_id" | "scope_key">,
  names?: { agent?: (id: string) => string | undefined; team?: (id: string) => string | undefined },
): string {
  switch (policy.scope_kind) {
    case "workspace":
      return "Everyone";
    case "team":
      return policy.scope_id ? (names?.team?.(policy.scope_id) ?? "One team") : "One team";
    case "agent":
      return policy.scope_id ? (names?.agent?.(policy.scope_id) ?? "One agent") : "One agent";
    case "task_type":
      return policy.scope_key ? `Tasks of kind “${policy.scope_key}”` : "A kind of task";
    default:
      return "";
  }
}

export interface ReviewPolicyDraft {
  name: string;
  scope_kind: ReviewScopeKind;
  scope_id: string;
  scope_key: string;
  enabled: boolean;
  mode: ReviewMode;
  conditions: ReviewCondition[];
  reviewer: ReviewerSelector;
  fail_closed: boolean;
  priority: number;
  /** Minutes, as typed in the form. */
  period_minutes: string;
}

export function emptyPolicyDraft(): ReviewPolicyDraft {
  return {
    name: "",
    scope_kind: "workspace",
    scope_id: "",
    scope_key: "",
    enabled: true,
    mode: "before_close",
    conditions: [],
    reviewer: { kind: "reporting_manager", fallback_to_human: true },
    fail_closed: false,
    priority: 100,
    period_minutes: "60",
  };
}

export function draftFromPolicy(policy: ReviewPolicy): ReviewPolicyDraft {
  return {
    name: policy.name,
    scope_kind: policy.scope_kind,
    scope_id: policy.scope_id ?? "",
    scope_key: policy.scope_key ?? "",
    enabled: policy.enabled,
    mode: policy.mode,
    conditions: policy.conditions_json.map((condition) => ({ ...condition })),
    reviewer: { fallback_to_human: true, ...policy.reviewer_selector_json },
    fail_closed: policy.fail_closed,
    priority: policy.priority,
    period_minutes: policy.period_seconds ? String(Math.round(policy.period_seconds / 60)) : "60",
  };
}

/** Mirrors the API's `_scope_shape` validator so the form fails early with
 * friendly copy. Returns null when the draft is valid. */
export function validatePolicyDraft(draft: ReviewPolicyDraft): string | null {
  if (!draft.name.trim()) return "Give this policy a name so people know what it's for.";
  if (draft.name.trim().length > 200) return "Keep the name under 200 characters.";
  if ((draft.scope_kind === "team" || draft.scope_kind === "agent") && !draft.scope_id) {
    return draft.scope_kind === "team" ? "Choose which team this applies to." : "Choose which agent this applies to.";
  }
  if (draft.scope_kind === "task_type" && !draft.scope_key.trim()) {
    return "Enter the kind of task this applies to.";
  }
  if (draft.conditions.length === 0) return "Pick at least one situation that should trigger a review.";
  if (draft.conditions.length > 20) return "Use at most 20 conditions.";
  for (const condition of draft.conditions) {
    const spec = CONDITION_SPECS.find((item) => item.kind === condition.kind);
    if (spec?.unit && (condition.threshold === null || condition.threshold === undefined || Number.isNaN(condition.threshold) || condition.threshold < 0)) {
      return `Enter a limit for “${spec.label}”.`;
    }
  }
  if (draft.reviewer.kind === "agent" && !draft.reviewer.agent_id) return "Choose which agent should review.";
  if (draft.reviewer.kind === "team_role" && !draft.reviewer.role_label?.trim()) {
    return "Enter the role label of the teammate who should review.";
  }
  if (draft.mode === "periodic") {
    const minutes = Number(draft.period_minutes);
    if (!Number.isFinite(minutes) || minutes < 1) return "Enter how often to check in (at least 1 minute).";
    if (minutes > 30 * 24 * 60) return "Check in at most every 30 days.";
  }
  if (!Number.isInteger(draft.priority) || draft.priority < 0 || draft.priority > 10_000) {
    return "Priority must be a whole number between 0 and 10,000.";
  }
  return null;
}

/** Body for POST /review-policies. Only call after `validatePolicyDraft`. */
export function policyDraftToBody(draft: ReviewPolicyDraft): ReviewPolicyIn {
  const reviewer: ReviewerSelector = {
    kind: draft.reviewer.kind,
    fallback_to_human: draft.reviewer.fallback_to_human ?? true,
  };
  if (draft.reviewer.kind === "agent") reviewer.agent_id = draft.reviewer.agent_id ?? null;
  if (draft.reviewer.kind === "team_role") reviewer.role_label = draft.reviewer.role_label?.trim() ?? null;
  if (draft.reviewer.fallback_agent_id) reviewer.fallback_agent_id = draft.reviewer.fallback_agent_id;

  const body: ReviewPolicyIn = {
    name: draft.name.trim(),
    scope_kind: draft.scope_kind,
    enabled: draft.enabled,
    mode: draft.mode,
    conditions: draft.conditions.map((condition) => {
      const spec = CONDITION_SPECS.find((item) => item.kind === condition.kind);
      return spec?.unit ? { kind: condition.kind, threshold: condition.threshold ?? null } : { kind: condition.kind };
    }),
    reviewer,
    fail_closed: draft.fail_closed,
    priority: draft.priority,
  };
  if (draft.scope_kind === "team" || draft.scope_kind === "agent") body.scope_id = draft.scope_id;
  if (draft.scope_kind === "task_type") body.scope_key = draft.scope_key.trim();
  if (draft.mode === "periodic") body.period_seconds = Math.round(Number(draft.period_minutes) * 60);
  return body;
}

/* ------------------------------------------------------------------ */
/* Rollups                                                             */
/* ------------------------------------------------------------------ */

export function rollupItemTitle(item: RollupItem): string {
  const who = item.agent_name ?? "An agent";
  const title = item.title || item.summary || "";
  switch (item.kind) {
    case "review":
      return title ? `${who}: ${title}` : `${who} is waiting on a review`;
    case "approval":
      return title ? `${who} needs an OK: ${title}` : `${who} needs an OK`;
    case "work_request":
      return title ? `${who} asked: ${title}` : `${who} asked for help`;
    default:
      return title ? `${who} · ${title}` : who;
  }
}

export function rollupStatusText(status: string): { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" } {
  switch (status) {
    case "running":
    case "active":
      return { label: "Working", tone: "accent" };
    case "queued":
      return { label: "Queued", tone: "neutral" };
    case "paused":
    case "waiting_approval":
    case "waiting_review":
    case "waiting_delegation":
    case "pending":
      return { label: "Waiting", tone: "warn" };
    case "completed":
    case "approved":
      return { label: "Done", tone: "ok" };
    case "failed":
    case "changes_requested":
      return { label: "Problem", tone: "danger" };
    case "blocked":
      return { label: "Blocked", tone: "danger" };
    case "cancelled":
    case "declined":
      return { label: "Stopped", tone: "neutral" };
    default:
      return { label: status.replace(/_/g, " ") || "Unknown", tone: "neutral" };
  }
}
