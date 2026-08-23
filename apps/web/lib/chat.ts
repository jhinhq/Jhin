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
/** Chips shown even when the transcript is not in detailed mode: things the
 * reader must act on or understand, never routine progress. */
const ESSENTIAL_ACTIVITY_KINDS: ReadonlySet<ActivityKind> = new Set<ActivityKind>([
  "queued",
  "failed",
  "paused",
  "stopped",
  "needs_review",
]);

export const CHAT_DETAILED_STORAGE_KEY = "jhin-chat-detailed";

export function mergeTimeline(
  messages: readonly ConversationMessage[],
  activity: readonly ActivityCard[],
  options: { detailed?: boolean } = {},
): TimelineItem[] {
  const detailed = options.detailed ?? true;
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
    if (!detailed && !ESSENTIAL_ACTIVITY_KINDS.has(card.kind)) continue;
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

/* ------------------------------------------------------------------ */
/* Quiet agent-to-agent exchanges and day separators                    */
/* ------------------------------------------------------------------ */

/** A run of agent↔agent traffic collapsed into one subtle row. */
export interface ExchangeItem {
  kind: "exchange";
  id: string;
  /** Timestamp of the first item in the exchange. */
  at: string;
  items: TimelineItem[];
  /** The colleague on the other side of the exchange. */
  withName: string;
  withAgentId: string | null;
  count: number;
  outcome: "ok" | "needs_review" | "problem";
}

/** A centered, faint date marker inserted when the day changes. */
export interface DaySeparatorItem {
  kind: "day";
  id: string;
  at: string;
  /** "Today", "Yesterday", or a short date like "Tue, Aug 18". */
  label: string;
  /** Localized time of the first item that day, like "10:04 AM". */
  time: string;
}

export type TranscriptItem = TimelineItem | ExchangeItem | DaySeparatorItem;

/** Progress chips that may be folded into a surrounding exchange. Chips the
 * reader must act on (needs_review, paused, stopped) always stay visible. */
const EXCHANGE_ACTIVITY_KINDS: ReadonlySet<ActivityKind> = new Set<ActivityKind>([
  "started",
  "queued",
  "finished",
  "failed",
]);

function partyKey(id: string | null, name: string): string {
  return id ?? `name:${name.toLowerCase()}`;
}

interface ExchangeParty {
  id: string | null;
  name: string;
}

function messageExchangeInfo(
  message: ConversationMessage,
  primary: { id: string | null; name: string | null },
): { key: string; ids: string[]; other: ExchangeParty; taskIds: string[] } | null {
  if (!isWorkCard(message)) return null;
  const content = message.content_json;
  const sender: ExchangeParty = { id: message.agent_id, name: message.sender_name ?? "" };
  const target: ExchangeParty = {
    id: str(content.target_agent_id) || null,
    name: str(content.target_agent_name),
  };
  const from: ExchangeParty = {
    id: str(content.from_agent_id) || null,
    name: str(content.from_agent_name),
  };

  const exists = (party: ExchangeParty) => party.id !== null || party.name !== "";
  const differsFromSender = (party: ExchangeParty) =>
    exists(party) && partyKey(party.id, party.name) !== partyKey(sender.id, sender.name);
  const isPrimary = (party: ExchangeParty) =>
    (primary.id !== null && party.id === primary.id) ||
    (primary.name !== null && primary.name !== "" && party.name === primary.name);

  let counterpart: ExchangeParty | null = null;
  if (differsFromSender(target)) counterpart = target;
  else if (differsFromSender(from)) counterpart = from;

  if (counterpart === null) {
    // No named counterpart: this is quiet agent↔agent traffic only when it
    // came from someone other than the primary agent.
    if (!exists(sender) || isPrimary(sender)) return null;
    counterpart = { id: primary.id, name: primary.name ?? "" };
  }

  const other = isPrimary(counterpart) && !isPrimary(sender) ? sender : counterpart;
  const keys = [partyKey(sender.id, sender.name), partyKey(counterpart.id, counterpart.name)].sort();
  const ids = [sender.id, counterpart.id].filter((id): id is string => id !== null);
  const taskIds = [message.task_id, str(content.created_task_id), str(content.task_id)].filter(
    (id): id is string => Boolean(id),
  );
  return { key: `pair:${keys.join("|")}`, ids, other, taskIds };
}

function exchangeOutcome(items: readonly TimelineItem[]): ExchangeItem["outcome"] {
  const last = items[items.length - 1];
  if (last.kind === "activity") return last.card.kind === "failed" ? "problem" : "ok";
  const message = last.message;
  const content = message.content_json;
  if (message.message_type === "escalation" || str(content.status) === "failed") return "problem";
  const verdict = str(content.verdict);
  if (
    message.message_type === "review_result" &&
    ["fail", "changes_requested", "escalate", "escalated"].includes(verdict)
  ) {
    return "needs_review";
  }
  return "ok";
}

/** Collapsed-row text: a friendly one-liner for single updates, a count for
 * longer exchanges ("3 updates with Linus"). */
export function exchangeLabel(exchange: ExchangeItem): string {
  if (exchange.count === 1) {
    const only = exchange.items[0];
    return only.kind === "message" ? friendlyMessageLabel(only.message) : only.card.label;
  }
  return `${exchange.count} updates with ${exchange.withName}`;
}

/** Short outcome suffix appended to a collapsed exchange row. */
export function exchangeSuffix(outcome: ExchangeItem["outcome"]): string {
  switch (outcome) {
    case "needs_review":
      return " · needs your review";
    case "problem":
      return " · ran into a problem";
    default:
      return "";
  }
}

/**
 * Collapse consecutive agent↔agent work cards (delegations, results,
 * reviews, questions, statuses between the same pair of agents) plus their
 * related progress chips into `exchange` items. The user↔primary dialogue,
 * system chips, and act-on-me chips (needs_review, paused, stopped) are
 * never grouped; an interleaved user message always breaks a group.
 * Ordering is preserved. Pure and unit-tested.
 */
export function groupExchanges(
  items: readonly TimelineItem[],
  options: { primaryAgentId?: string | null; primaryAgentName?: string | null } = {},
): (TimelineItem | ExchangeItem)[] {
  const primary = { id: options.primaryAgentId ?? null, name: options.primaryAgentName ?? null };
  const result: (TimelineItem | ExchangeItem)[] = [];
  let open: {
    key: string;
    items: TimelineItem[];
    partyIds: Set<string>;
    taskIds: Set<string>;
    other: ExchangeParty;
  } | null = null;

  const flush = () => {
    if (open === null) return;
    const group = open;
    open = null;
    const first = group.items[0];
    result.push({
      kind: "exchange",
      id: `exchange:${first.id}`,
      at: first.at,
      items: group.items,
      withName: group.other.name || "another agent",
      withAgentId: group.other.id,
      count: group.items.length,
      outcome: exchangeOutcome(group.items),
    });
  };

  for (const item of items) {
    if (item.kind === "message") {
      const info = messageExchangeInfo(item.message, primary);
      if (info === null) {
        flush();
        result.push(item);
        continue;
      }
      if (open !== null && open.key === info.key) {
        open.items.push(item);
        for (const id of info.ids) open.partyIds.add(id);
        for (const id of info.taskIds) open.taskIds.add(id);
      } else {
        flush();
        open = {
          key: info.key,
          items: [item],
          partyIds: new Set(info.ids),
          taskIds: new Set(info.taskIds),
          other: info.other,
        };
      }
      continue;
    }
    // Activity chips never start an exchange; they join one in progress when
    // they clearly belong to it (same agents, or the delegated task).
    const card = item.card;
    const joinable =
      open !== null &&
      EXCHANGE_ACTIVITY_KINDS.has(card.kind) &&
      ((card.actor_agent_id !== null && open.partyIds.has(card.actor_agent_id)) ||
        (card.target_agent_id !== null && open.partyIds.has(card.target_agent_id)) ||
        (card.task_id !== null && open.taskIds.has(card.task_id)));
    if (joinable && open !== null) {
      open.items.push(item);
      if (card.task_id !== null) open.taskIds.add(card.task_id);
    } else {
      flush();
      result.push(item);
    }
  }
  flush();
  return result;
}

function dayKey(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

/** "Today", "Yesterday", "Tue, Aug 18", or "Tue, Aug 18, 2025" — always in
 * the viewer's local timezone. Exported for tests. */
export function dayLabel(date: Date, now: Date = new Date()): string {
  const key = dayKey(date);
  if (key === dayKey(now)) return "Today";
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (key === dayKey(yesterday)) return "Yesterday";
  const sameYear = date.getFullYear() === now.getFullYear();
  return date.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

/** Insert a centered date marker whenever the (viewer-local) day changes
 * between items. Items with unparseable timestamps never produce markers. */
export function withDaySeparators<T extends { id: string; at: string }>(
  items: readonly T[],
  now: Date = new Date(),
): (T | DaySeparatorItem)[] {
  const result: (T | DaySeparatorItem)[] = [];
  let lastKey: string | null = null;
  for (const item of items) {
    const date = new Date(item.at);
    if (!Number.isNaN(date.getTime())) {
      const key = dayKey(date);
      if (key !== lastKey) {
        lastKey = key;
        result.push({
          kind: "day",
          id: `day:${key}`,
          at: item.at,
          label: dayLabel(date, now),
          time: date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }),
        });
      }
    }
    result.push(item);
  }
  return result;
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
