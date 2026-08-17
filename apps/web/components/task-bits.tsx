"use client";

/** Small shared pieces for task/run views. */

import { Badge } from "@/components/ui";
import type { TaskState } from "@/lib/types";

const STATE_TONES: Record<string, "neutral" | "ok" | "warn" | "danger" | "accent"> = {
  queued: "neutral",
  running: "accent",
  paused: "warn",
  completed: "ok",
  failed: "danger",
  cancelled: "neutral",
  pending: "neutral",
  waiting_approval: "warn",
};

export function StateBadge({ state }: { state: TaskState | string }) {
  return <Badge tone={STATE_TONES[state] ?? "neutral"}>{state}</Badge>;
}

export function isActiveState(state: string): boolean {
  return state === "queued" || state === "running" || state === "paused" || state === "pending";
}
