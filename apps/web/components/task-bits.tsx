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
  waiting_delegation: "warn",
};

export function StateBadge({ state }: { state: TaskState | string }) {
  return <Badge tone={STATE_TONES[state] ?? "neutral"}>{state}</Badge>;
}

export function isActiveState(state: string): boolean {
  return state === "queued" || state === "running" || state === "paused" || state === "pending";
}

/** Structured agent-to-agent message types (plan 29). */
export const AGENT_MESSAGE_TYPES = new Set([
  "instruction",
  "question",
  "status",
  "result",
  "delegation",
  "review_request",
  "review_result",
  "escalation",
]);

const MESSAGE_TYPE_TONES: Record<string, "neutral" | "ok" | "warn" | "danger" | "accent"> = {
  delegation: "accent",
  review_request: "warn",
  result: "ok",
  escalation: "danger",
  question: "warn",
  status: "neutral",
  instruction: "neutral",
};

export function MessageTypeBadge({
  type,
  content,
}: {
  type: string;
  content: Record<string, unknown>;
}) {
  let tone = MESSAGE_TYPE_TONES[type] ?? "neutral";
  let label = type.replace("_", " ");
  if (type === "review_result") {
    const verdict = typeof content.verdict === "string" ? content.verdict : "";
    tone = verdict === "pass" ? "ok" : verdict === "fail" ? "danger" : "neutral";
    if (verdict) label = `review: ${verdict}`;
  }
  return <Badge tone={tone}>{label}</Badge>;
}

interface Artifact {
  type: string;
  id: string;
  url_ref: string;
}

function artifactsOf(content: Record<string, unknown>): Artifact[] {
  if (!Array.isArray(content.artifacts)) return [];
  return content.artifacts.flatMap((raw) => {
    if (typeof raw !== "object" || raw === null) return [];
    const item = raw as Record<string, unknown>;
    return [
      {
        type: typeof item.type === "string" ? item.type : "artifact",
        id: typeof item.id === "string" ? item.id : "",
        url_ref: typeof item.url_ref === "string" ? item.url_ref : "",
      },
    ];
  });
}

function stringsOf(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item !== "");
}

/** Conversational rendering of a structured agent message (plan 29): the
 * backend keeps the JSON; the UI shows summary, artifacts, risks, and the
 * recommended next action. */
export function StructuredMessageBody({ content }: { content: Record<string, unknown> }) {
  const summary =
    typeof content.summary === "string"
      ? content.summary
      : typeof content.text === "string"
        ? content.text
        : "";
  const artifacts = artifactsOf(content);
  const risks = stringsOf(content.risks);
  const nextAction =
    typeof content.recommended_next_action === "string" ? content.recommended_next_action : "";

  return (
    <div data-testid="structured-message" className="mt-1 space-y-2">
      {summary ? <p className="whitespace-pre-wrap text-sm text-ink">{summary}</p> : null}
      {artifacts.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {artifacts.map((artifact, index) => (
            <li key={index}>
              {artifact.url_ref ? (
                <a
                  href={artifact.url_ref}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 rounded-md border border-line bg-raised px-2 py-0.5 text-xs text-accent hover:underline"
                >
                  {artifact.type}
                  {artifact.id ? ` ${artifact.id}` : ""}
                </a>
              ) : (
                <span className="inline-flex items-center gap-1 rounded-md border border-line bg-raised px-2 py-0.5 text-xs text-dim">
                  {artifact.type}
                  {artifact.id ? ` ${artifact.id}` : ""}
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
      {risks.length > 0 ? (
        <ul className="space-y-0.5">
          {risks.map((risk, index) => (
            <li key={index} className="text-xs text-warn">
              risk: {risk}
            </li>
          ))}
        </ul>
      ) : null}
      {nextAction ? <p className="text-xs text-dim">next: {nextAction}</p> : null}
    </div>
  );
}
