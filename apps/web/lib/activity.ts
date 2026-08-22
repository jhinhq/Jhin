/**
 * Pure helpers for the company activity feed and attention inbox. No React,
 * fully unit-tested (tests/activity-helpers.test.ts).
 */

import { conditionLabel, REVIEW_MODE_LABELS, reviewVerdictLabel, workRequestStatus } from "@/lib/coordination";
import type { ActivityCard, ActivityKind, ReviewMode } from "@/lib/types";

export type ActivityGroup = "handoffs" | "progress" | "review";

/** Friendly filter groups for the feed. `status_update` rides along with
 * progress so a plain "what happened" view never hides an agent's update. */
export const ACTIVITY_GROUPS: Record<ActivityGroup, ActivityKind[]> = {
  handoffs: ["asked_agent", "reported", "escalated"],
  progress: ["started", "queued", "finished", "failed", "paused", "stopped", "status_update"],
  review: ["needs_review"],
};

export const ACTIVITY_GROUP_LABELS: Record<ActivityGroup, string> = {
  handoffs: "Handoffs",
  progress: "Progress",
  review: "Needs review",
};

export function groupForKind(kind: ActivityKind): ActivityGroup {
  for (const [group, kinds] of Object.entries(ACTIVITY_GROUPS) as [ActivityGroup, ActivityKind[]][]) {
    if (kinds.includes(kind)) return group;
  }
  return "progress";
}

/** Comma list for the API's `kinds` query param, or undefined for "all". */
export function kindsParam(group: ActivityGroup | "all"): string | undefined {
  return group === "all" ? undefined : ACTIVITY_GROUPS[group].join(",");
}

/** Color tone for a kind so every status pairs color with text. */
export function toneForKind(kind: ActivityKind): "neutral" | "ok" | "warn" | "danger" | "accent" {
  switch (kind) {
    case "finished":
    case "reported":
      return "ok";
    case "failed":
    case "escalated":
      return "danger";
    case "needs_review":
    case "paused":
    case "queued":
      return "warn";
    case "asked_agent":
    case "started":
      return "accent";
    default:
      return "neutral";
  }
}

export function actorNameOf(card: Pick<ActivityCard, "actor_type" | "actor_agent_name">): string {
  if (card.actor_agent_name) return card.actor_agent_name;
  if (card.actor_type === "user") return "You";
  return "Jhin";
}

function firstSentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  const match = /^(.+?[.!?])(\s|$)/.exec(trimmed);
  return (match ? match[1] : trimmed).replace(/[.!?]+$/, "");
}

function asRequest(text: string): string {
  let clause = firstSentence(text);
  clause = clause.replace(/^(please|can you|could you|would you)\s+/i, "");
  if (!clause) return "";
  return clause.charAt(0).toLowerCase() + clause.slice(1);
}

/** One-line plain-language sentence for a card, e.g.
 * "CTO asked Senior SWE to add retry logic" or "QA Engineer finished". */
export function friendlyHandoff(card: ActivityCard): string {
  const actor = actorNameOf(card);
  const target = card.target_agent_name;
  switch (card.kind) {
    case "asked_agent": {
      const request = asRequest(card.summary) || (card.task_title ? `work on “${card.task_title}”` : "help");
      return target ? `${actor} asked ${target} to ${request}` : `${actor} asked for ${request}`;
    }
    case "reported":
      return target ? `${actor} reported back to ${target}` : `${actor} reported back`;
    case "escalated":
      return target ? `${actor} needs help from ${target}` : `${actor} needs help`;
    case "needs_review":
      return `${actor} needs your review`;
    case "status_update":
      return `${actor} shared an update`;
    case "started":
      return card.task_title ? `${actor} started working on “${card.task_title}”` : `${actor} started working`;
    case "queued":
      return `${actor} is waiting for a free slot`;
    case "finished":
      return card.task_title ? `${actor} finished “${card.task_title}”` : `${actor} finished`;
    case "failed":
      return `${actor} ran into a problem`;
    case "paused":
      return `${actor} paused`;
    case "stopped":
      return `${actor} stopped`;
    default:
      return `${actor} · ${card.label}`;
  }
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Relative time for feeds: "just now", "5m ago", "3h ago", "2d ago", then a
 * short date. Future timestamps (clock skew) read as "just now". */
export function timeAgo(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = now - then;
  if (diff < MINUTE) return "just now";
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}m ago`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h ago`;
  if (diff < 7 * DAY) return `${Math.floor(diff / DAY)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function detailStr(detail: Record<string, unknown>, key: string): string {
  const value = detail[key];
  return typeof value === "string" ? value : "";
}

/** Plain-language lines for the structured `detail_json` of work-request
 * and review cards (coordination release). Empty for other shapes so the
 * caller falls back to the raw "Show details" disclosure. */
export function friendlyDetailLines(card: Pick<ActivityCard, "kind" | "detail_json" | "work_request_id" | "review_id">): string[] {
  const detail = card.detail_json ?? {};
  const lines: string[] = [];
  if (card.work_request_id || detailStr(detail, "expected_output") || detailStr(detail, "response")) {
    const status = detailStr(detail, "status");
    if (status) lines.push(`Status: ${workRequestStatus(status).label}`);
    const expected = detailStr(detail, "expected_output");
    if (expected) lines.push(`Expected: ${expected}`);
    const response = detailStr(detail, "response");
    if (response) lines.push(`Reply: ${response}`);
    if (detail.created_task_id) lines.push("Accepted as a separate piece of work.");
    return lines;
  }
  if (card.review_id || card.kind === "needs_review") {
    const mode = detailStr(detail, "mode");
    if (mode) lines.push(`When: ${REVIEW_MODE_LABELS[mode as ReviewMode] ?? mode.replace(/_/g, " ")}`);
    const reviewer = detailStr(detail, "reviewer_type");
    if (reviewer === "human") lines.push("Reviewer: a person");
    else if (reviewer === "agent") lines.push("Reviewer: another agent");
    const conditions = Array.isArray(detail.matched_conditions)
      ? detail.matched_conditions
          .map((item) => (typeof item === "string" ? item : typeof item === "object" && item !== null ? detailStr(item as Record<string, unknown>, "kind") : ""))
          .filter(Boolean)
          .map(conditionLabel)
      : [];
    if (conditions.length) lines.push(`Why: ${conditions.join(", ")}`);
    const tool = detailStr(detail, "tool_name");
    if (tool) lines.push(`Action: ${tool.replace(/[._]/g, " ")}`);
    const verdict = reviewVerdictLabel(detailStr(detail, "verdict"));
    if (verdict) lines.push(`Outcome: ${verdict}`);
    return lines;
  }
  return lines;
}

/** Pretty JSON for the "Show details" disclosure. */
export function detailText(detail: Record<string, unknown>): string | null {
  if (!detail || Object.keys(detail).length === 0) return null;
  return JSON.stringify(detail, null, 2);
}
