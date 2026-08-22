/**
 * Pure helpers for the curated-memory screens (agent profile "Memory" tab
 * and the Attention "Needs review" group). No React; unit-tested in
 * tests/memory-helpers.test.ts.
 */

import type { MemoryKind, MemoryRecord, MemoryScope, MemoryStatus } from "@/lib/types";

export const MEMORY_MAX_CHARS = 2000;
export const MEMORY_MAX_TAGS = 10;

export const SCOPE_LABELS: Record<MemoryScope, string> = {
  agent: "Agent",
  team: "Team",
  workspace: "Company",
};

export const KIND_LABELS: Record<MemoryKind, string> = {
  fact: "Fact",
  preference: "Preference",
  decision: "Decision",
  procedure: "How-to",
  context: "Context",
  other: "Note",
};

export const STATUS_LABELS: Record<MemoryStatus, { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" }> = {
  active: { label: "Remembered", tone: "ok" },
  proposed: { label: "Waiting for approval", tone: "warn" },
  contested: { label: "Conflicts with another memory", tone: "warn" },
  superseded: { label: "Replaced by a newer version", tone: "neutral" },
  rejected: { label: "Not accepted", tone: "neutral" },
  forgotten: { label: "Forgotten", tone: "neutral" },
};

/** Filters shown in the Memory tab. "review" covers what a human should look at. */
export type MemoryStatusFilter = "active" | "review" | "all";

export const STATUS_FILTERS: { id: MemoryStatusFilter; label: string }[] = [
  { id: "active", label: "Remembered" },
  { id: "review", label: "Needs a look" },
  { id: "all", label: "Everything" },
];

export function scopeLabel(scope: MemoryScope | string): string {
  return SCOPE_LABELS[scope as MemoryScope] ?? "Memory";
}

export function kindLabel(kind: MemoryKind | string): string {
  return KIND_LABELS[kind as MemoryKind] ?? "Note";
}

/** 0..1 → plain words for confidence. */
export function confidenceWord(value: number): string {
  if (value >= 0.85) return "Very sure";
  if (value >= 0.6) return "Fairly sure";
  if (value >= 0.35) return "Not sure";
  return "A guess";
}

/** 0..1 → plain words for importance. */
export function importanceWord(value: number): string {
  if (value >= 0.85) return "Essential";
  if (value >= 0.6) return "Important";
  if (value >= 0.35) return "Useful";
  return "Minor";
}

/** Apply a status filter client-side (the API lists non-forgotten records). */
export function filterByStatus(
  items: readonly MemoryRecord[],
  filter: MemoryStatusFilter,
): MemoryRecord[] {
  switch (filter) {
    case "active":
      return items.filter((item) => item.status === "active");
    case "review":
      return items.filter((item) => item.status === "proposed" || item.status === "contested");
    default:
      return items.filter((item) => item.status !== "forgotten");
  }
}

/** Query params for `useMemories` given the tab's filters. The API's
 * `agent_id` / `team_id` filters select agent-scope / team-scope records, so
 * each scope maps to exactly one query. Proposed and contested are fetched
 * together (no status filter) so the "Needs a look" filter works locally. */
export function memoryListParams(
  scope: MemoryScope,
  ids: { agentId: string; teamId: string | null },
  filter: MemoryStatusFilter,
): Record<string, string | number | undefined> {
  return {
    scope,
    agent_id: scope === "agent" ? ids.agentId : undefined,
    team_id: scope === "team" ? (ids.teamId ?? undefined) : undefined,
    status: filter === "active" ? "active" : undefined,
    limit: 100,
  };
}

/** Pinned first, then newest. */
export function sortMemories(items: readonly MemoryRecord[]): MemoryRecord[] {
  return [...items].sort((a, b) => {
    const pinDelta = Number(Boolean(b.pinned_at)) - Number(Boolean(a.pinned_at));
    if (pinDelta !== 0) return pinDelta;
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
  });
}

export interface MemoryDraft {
  content: string;
  scope: MemoryScope;
  kind: MemoryKind;
  tags: string;
}

/** Validate the "Remember something" composer. Returns a user-facing message
 * or null when the draft can be sent. */
export function validateMemoryDraft(
  draft: MemoryDraft,
  options: { isAdmin: boolean; hasTeam: boolean },
): string | null {
  const content = draft.content.trim();
  if (!content) return "Write what you want the agent to remember.";
  if (content.length > MEMORY_MAX_CHARS) {
    return `Keep it under ${MEMORY_MAX_CHARS.toLocaleString()} characters (this is ${content.length.toLocaleString()}).`;
  }
  if (draft.scope !== "agent" && !options.isAdmin) {
    return "Only admins can save team or company memories. Save it for this agent instead.";
  }
  if (draft.scope === "team" && !options.hasTeam) {
    return "This agent isn't on a team yet, so a team memory has nowhere to go.";
  }
  if (parseTags(draft.tags).length > MEMORY_MAX_TAGS) {
    return `Use at most ${MEMORY_MAX_TAGS} tags.`;
  }
  return null;
}

export function parseTags(raw: string): string[] {
  return Array.from(
    new Set(
      raw
        .split(/[,\n]/)
        .map((tag) => tag.trim().toLowerCase())
        .filter(Boolean),
    ),
  );
}

/** Plain-language reason for a failed memory write. */
export function memoryErrorMessage(status: number, detail: string): string {
  if (status === 409) return "The agent already remembers this (or it was just replaced). Refresh to see the latest version.";
  if (status === 422) return "That can't be saved: it looks like it contains a password, key, or token. Remove the secret and try again.";
  if (status === 403) return "You don't have permission to change this memory. Ask an admin.";
  return `${detail || "Saving failed"}. Try again in a moment.`;
}
