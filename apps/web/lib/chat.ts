/**
 * Pure helpers for the chat-first experience (/chats). No React here so the
 * logic is unit-testable: relative times, friendly status labels, message
 * labels, and the merged transcript timeline.
 */

import { isWorkRequestMessage, reviewVerdictLabel, workRequestMessageLabel } from "@/lib/coordination";
import type { ActivityCard, ActivityKind, Conversation, ConversationMessage } from "@/lib/types";

export const LAST_AGENT_STORAGE_KEY = "jhin-last-agent";

/** Structured agent message types that render as compact "work cards". */
const WORK_CARD_TYPES = new Set([
  "delegation",
  "review_request",
  "result",
  "review_result",
  "escalation",
  "question",
  "status",
]);

/** "just now", "4m", "2h", "3d", then a short date. */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.round((now.getTime() - then) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d`;
  const date = new Date(iso);
  const sameYear = date.getFullYear() === now.getFullYear();
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

type LiveStatusTone = "accent" | "neutral" | "warn";

export interface LiveStatus {
  label: string;
  tone: LiveStatusTone;
  kind: "working" | "queued" | "review" | "waiting_review" | "paused";
}

/** Small live status for a conversation, or null when nothing is happening. */
export function statusLabelFor(
  conversation: Pick<Conversation, "active_task_state" | "active_run_status">,
): LiveStatus | null {
  if (conversation.active_run_status === "waiting_approval") {
    return { label: "Needs your review", tone: "warn", kind: "review" };
  }
  if (conversation.active_run_status === "waiting_review") {
    // Parked on a work review (a manager/AI reviewer or a person decides).
    return { label: "Waiting for a review", tone: "neutral", kind: "waiting_review" };
  }
  switch (conversation.active_task_state) {
    case "running":
      return { label: "Working…", tone: "accent", kind: "working" };
    case "queued":
      return { label: "Waiting for a free slot", tone: "neutral", kind: "queued" };
    case "paused":
      return { label: "Paused", tone: "warn", kind: "paused" };
    default:
      return null;
  }
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** Who a structured message was aimed at, when the backend recorded it. */
function messageTarget(message: Pick<ConversationMessage, "content_json">): string {
  const content = message.content_json;
  return (
    str(content.target_agent_name) || str(content.to_agent_name) || str(content.agent_name) || ""
  );
}

/** Friendly, plain-language label for a structured agent message. */
export function friendlyMessageLabel(
  message: Pick<ConversationMessage, "message_type" | "content_json" | "sender_type">,
): string {
  const content = message.content_json;
  if (isWorkRequestMessage(message)) return workRequestMessageLabel({ content_json: content, sender_id: null });
  const target = messageTarget(message);
  const from = str(content.from_agent_name);
  switch (message.message_type) {
    case "delegation":
      return target ? `Asked ${target} for help` : "Asked another agent for help";
    case "review_request":
      return target ? `Asked ${target} to review` : "Asked for a review";
    case "result":
      return from ? `${from} reported back` : "Reported back";
    case "review_result": {
      const verdict = reviewVerdictLabel(str(content.verdict));
      const who = from ? `${from}'s review` : "Review";
      return verdict ? `${who}: ${verdict}` : `${who} came back`;
    }
    case "escalation":
      return "Needs help";
    case "question":
      return target ? `Asked ${target} a question` : "Asked a question";
    case "status":
      return "Shared an update";
    case "instruction":
      return message.sender_type === "user" ? "You added an instruction" : "Gave an instruction";
    default:
      return "Message";
  }
}

/** Plain text of a message for previews and bubbles. */
export function messageText(message: Pick<ConversationMessage, "content_json">): string {
  const content = message.content_json;
  return str(content.text) || str(content.summary) || str(content.content) || "";
}

/** Extra lines a work-request card shows under its summary (what was asked
 * for and what came back), without exposing ids. */
export function workRequestDetailLines(message: Pick<ConversationMessage, "content_json">): string[] {
  const content = message.content_json;
  if (!isWorkRequestMessage(message)) return [];
  const lines: string[] = [];
  const instructions = str(content.instructions);
  const expected = str(content.expected_output);
  const response = str(content.response);
  if (instructions && instructions !== messageText(message)) lines.push(instructions);
  if (expected) lines.push(`Expected: ${expected}`);
  if (response && response !== messageText(message)) lines.push(`Reply: ${response}`);
  const risks = Array.isArray(content.risks) ? content.risks.filter((r): r is string => typeof r === "string") : [];
  if (risks.length) lines.push(`Risks: ${risks.join("; ")}`);
  return lines;
}

export function isWorkCard(
  message: Pick<ConversationMessage, "message_type" | "sender_type">,
): boolean {
  return message.sender_type === "agent" && WORK_CARD_TYPES.has(message.message_type);
}

/** Activity kinds that show as system chips in the transcript. The other
 * kinds are projections of structured messages the transcript already shows. */
const TRANSCRIPT_ACTIVITY_KINDS: ReadonlySet<ActivityKind> = new Set<ActivityKind>([
  "started",
  "queued",
  "finished",
  "failed",
  "paused",
  "stopped",
  "needs_review",
]);

export type TimelineItem =
  | { kind: "message"; id: string; at: string; message: ConversationMessage }
  | { kind: "activity"; id: string; at: string; card: ActivityCard };

/** Merge messages and activity cards into one ascending timeline. Cards that
 * project a message already in the transcript (`msg:<id>`) and cards of
 * non-transcript kinds are dropped; duplicate ids are collapsed. On equal
 * timestamps messages come before activity so "Started working" follows the
 * user's request. */
export function mergeTimeline(
  messages: readonly ConversationMessage[],
  activity: readonly ActivityCard[],
): TimelineItem[] {
  const seen = new Set<string>();
  const items: TimelineItem[] = [];
  const messageIds = new Set(messages.map((message) => message.id));

  for (const message of messages) {
    const id = `message:${message.id}`;
    if (seen.has(id)) continue;
    seen.add(id);
    items.push({ kind: "message", id, at: message.created_at, message });
  }

  for (const card of activity) {
    if (!TRANSCRIPT_ACTIVITY_KINDS.has(card.kind)) continue;
    if (card.id.startsWith("msg:") && messageIds.has(card.id.slice(4))) continue;
    const id = `activity:${card.id}`;
    if (seen.has(id)) continue;
    seen.add(id);
    items.push({ kind: "activity", id, at: card.created_at, card });
  }

  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      const delta = new Date(a.item.at).getTime() - new Date(b.item.at).getTime();
      if (delta !== 0) return delta;
      if (a.item.kind !== b.item.kind) return a.item.kind === "message" ? -1 : 1;
      return a.index - b.index;
    })
    .map(({ item }) => item);
}

/** Client-side rail filter: title, agent name, or preview. */
export function filterConversations<T extends Pick<Conversation, "title" | "agent_name" | "last_message_preview">>(
  conversations: readonly T[],
  query: string,
): T[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...conversations];
  return conversations.filter((conversation) =>
    [conversation.title, conversation.agent_name ?? "", conversation.last_message_preview ?? ""]
      .join("\n")
      .toLowerCase()
      .includes(needle),
  );
}

/** Newest activity first; pinned conversations are sectioned by the caller. */
export function sortByActivity<T extends Pick<Conversation, "last_activity_at">>(
  conversations: readonly T[],
): T[] {
  return [...conversations].sort(
    (a, b) => new Date(b.last_activity_at).getTime() - new Date(a.last_activity_at).getTime(),
  );
}

export const STARTER_PROMPTS = [
  "Summarize what happened this week and what needs my attention.",
  "Draft a short status update I can send to the team.",
  "Look into the open issues and suggest what to tackle first.",
  "Review the latest pull request and tell me if it's safe to merge.",
];

/** Build the body for a new turn. */
export function newTurn(text: string): { text: string; client_turn_id: string } {
  return { text: text.trim(), client_turn_id: crypto.randomUUID() };
}
