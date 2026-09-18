/**
 * Pure helpers for the chat-first experience (/chats). No React here so the
 * logic is unit-testable: relative times, friendly status labels, message
 * labels, and the merged transcript timeline.
 */

import { isWorkRequestMessage, reviewVerdictLabel, workRequestMessageLabel } from "@/lib/coordination";
import { mayContainSecret } from "@/lib/private-input";
import type { ConversationItem } from "@/lib/agentic-chat";
import type { ManagedFile } from "@/lib/workspace-files";
import type {
  ActivityCard,
  ActivityKind,
  AgentRenamedContent,
  Conversation,
  ConversationMessage,
  ConversationToolCall,
  FailureNotice,
  MemoryScope,
  MemorySavedContent,
  UserQuestionContent,
  UserQuestionOption,
  UserQuestionStatus,
} from "@/lib/types";

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
  kind:
    | "working"
    | "queued"
    | "review"
    | "waiting_review"
    | "waiting_delegation"
    | "question"
    | "paused";
  /** True when `label` is the agent's actual current step rather than the
   * generic state text, so a surface can show it in place of "… is working"
   * instead of alongside it. */
  specific?: boolean;
  /**
   * When the agent's current stretch of thinking began, for a surface that
   * wants to say how long this has been going. Set only while the agent is
   * genuinely working: the other states are waits — two of them waits on the
   * reader — and a clock on one of those reads as pressure rather than
   * progress.
   *
   * **Three states, and they mean three different things.** A timestamp is a
   * stretch to count from. `null` is the API saying it measured and there is
   * no instant — a turn parked on an approval, question or review that was
   * opened and never closed, a run that has already finished, or a run with
   * no usable start stamp, all of which arrive as the same null, so a surface
   * says *that there is no instant* rather than picking one of them to blame.
   * Worth a word either way, because a number vanishing without one looks
   * like the feature never shipped. Absent (`undefined`) is an API that predates
   * the working clock and never sent the field at all: nothing was measured,
   * so there is nothing to report and a surface says nothing. Collapsing the
   * last two onto each other makes an old API's silence read as a stuck
   * approval, which is a sentence about this workspace that is not true.
   *
   * Note *stretch*, not run: a turn that stopped to ask a question resumes
   * with a new `since`, and the thinking it did before the question is in
   * `worked` rather than counted twice or thrown away.
   */
  since?: string | null;
  /**
   * Whole seconds of thinking already banked before `since`. Add the two: a
   * surface shows `worked + (now - since)` and only the second half ticks.
   *
   * It stands on its own when `since` is `null`: the API measured a real
   * figure for everything the turn did before it parked, and "at least 16s"
   * is both true and more use than refusing to say anything.
   */
  worked?: number;
}

/**
 * What a live status can be read from. `active_activity` is optional because
 * only the open conversation carries it: deriving it costs a lookup per
 * conversation, so the rail keeps the generic label and the thread you are
 * actually watching says what the agent is doing.
 */
export type LiveStatusSource = Pick<Conversation, "active_task_state" | "active_run_status"> &
  Partial<
    Pick<
      Conversation,
      "active_activity" | "active_run_working_since" | "active_run_working_seconds"
    >
  >;

/** Small live status for a conversation, or null when nothing is happening. */
export function statusLabelFor(conversation: LiveStatusSource): LiveStatus | null {
  if (conversation.active_run_status === "waiting_person") {
    // The whole thread is blocked on a question in the transcript. Saying
    // "Working…" here would be a lie the reader can act on: they would wait
    // for an agent that is waiting for them.
    return { label: "Needs your answer", tone: "warn", kind: "question" };
  }
  if (conversation.active_run_status === "waiting_approval") {
    return { label: "Needs your review", tone: "warn", kind: "review" };
  }
  if (conversation.active_run_status === "waiting_review") {
    // Parked on a work review (a manager/AI reviewer or a person decides).
    return { label: "Waiting for a review", tone: "neutral", kind: "waiting_review" };
  }
  if (conversation.active_run_status === "waiting_delegation") {
    // Parked on a colleague. The run handed the work to another agent and
    // resumes when that agent's summary arrives, so this agent is not
    // thinking — and the clock below counts thinking. Without this branch the
    // status falls through to `running` and the turn says "Working…" with a
    // number ticking beside it, which is the same lie an approval wait would
    // tell, told about a colleague instead of a person.
    //
    // No clock, for the reason every other wait has none: there is no stretch
    // of *this* agent's work in progress to count. The colleague's stretch is
    // still work on this reply, and it stays inside the banked total the turn
    // shows once it resumes — see `WORKING_TIME_TITLE`, which promises the
    // whole reply's work minus only the time it spent waiting on *you*.
    //
    // The API already writes the sentence that names who ("Waiting for
    // Linus", `jhin_domain.activity.waiting_for_colleague_phrase`), so use it
    // when the detail endpoint sent one and fall back to the anonymous
    // version on the rail, which does not pay for activity per row.
    const colleague = conversation.active_activity?.trim() ?? "";
    if (colleague) {
      return { label: colleague, tone: "neutral", kind: "waiting_delegation", specific: true };
    }
    return { label: "Waiting for a colleague", tone: "neutral", kind: "waiting_delegation" };
  }
  switch (conversation.active_task_state) {
    case "running": {
      // "Working…" says only that the agent has not stopped. When the API can
      // say which step it is on, say that instead — same tone and kind, so
      // every surface that keys off `kind` (the pill's dot, the composer hint)
      // behaves exactly as before. The four waits above still win: two are
      // things the person has to act on and two are somebody else's move —
      // none of them is this agent's progress to watch.
      //
      // `since` rides along here and nowhere else, which is the whole rule
      // about where an elapsed timer may appear.
      //
      // It is the API's *working* clock, never the run's `started_at`. A run
      // keeps one `started_at` across the whole turn, including the hours it
      // spent parked on a person's approval, so counting from it prints the
      // reader's own deliberation back at them as the agent's thinking.
      //
      // Read straight through, with no `??`. The field has three states and
      // `?? null` had two: it turned "this API never sent the field" into
      // "the API measured and found no instant", so a conversation served by
      // an API that predates the working clock rendered "Working time
      // unavailable" under a tooltip blaming an approval nobody had opened.
      // See `LiveStatus.since`.
      const since = conversation.active_run_working_since;
      const worked = conversation.active_run_working_seconds ?? 0;
      const activity = conversation.active_activity?.trim() ?? "";
      if (activity) {
        return { label: activity, tone: "accent", kind: "working", specific: true, since, worked };
      }
      return { label: "Working…", tone: "accent", kind: "working", since, worked };
    }
    case "queued":
      return { label: "Waiting for a free slot", tone: "neutral", kind: "queued" };
    case "paused":
      return { label: "Paused", tone: "warn", kind: "paused" };
    default:
      return null;
  }
}

/* ------------------------------------------------------------------ */
/* How long the agent has been at it                                    */
/* ------------------------------------------------------------------ */

/**
 * Whole seconds between a server timestamp and now, or null when there is
 * nothing to count.
 *
 * `now` is passed in rather than read here, so the caller decides which clock
 * this is measured against and this stays a pure function two lines of test
 * can pin down.
 *
 * Negative is clamped to zero rather than shown. The timestamp comes from the
 * server and the clock from the browser, and those are two different clocks:
 * a machine a few seconds ahead would otherwise open every timer at "-4s".
 * Zero is the honest reading of "this started about when you asked". Nothing
 * here can correct a badly skewed clock in the other direction — no more than
 * "4m ago" anywhere else in the app can — but it can refuse to print
 * nonsense.
 */
export function elapsedSeconds(
  startedAt: string | null | undefined,
  now: number = Date.now(),
): number | null {
  if (!startedAt) return null;
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return null;
  return Math.max(0, Math.floor((now - started) / 1000));
}

/**
 * Compact duration for a pill in a tight row: `4s`, `1m 12s`, `14m`, `2h 5m`,
 * `3d 4h`.
 *
 * One rule, applied at every scale: the smaller unit is dropped past ten of
 * the larger one, and the larger unit rolls over at its own boundary. So
 * seconds go at ten minutes, minutes at ten hours, hours at ten days — by
 * then the reader is asking "is this still going and roughly how long" rather
 * than timing it, and a digit that changes every second in the corner of the
 * eye costs more attention than it returns.
 *
 * A day is a day and not twenty-four more hours, because `relativeTime` and
 * `timeAgo` roll over to days everywhere else in the app and a pill that
 * alone says "72h" reads as a stuck counter rather than as three days.
 *
 * Never more than six characters, which is what the pill has room for at
 * 320px beside its own label.
 */
export function elapsedLabel(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 10) {
    const rest = total % 60;
    return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
  }
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rest = minutes % 60;
    if (hours >= 10 || rest === 0) return `${hours}h`;
    return `${hours}h ${rest}m`;
  }
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  if (days >= 10 || rest === 0) return `${days}d`;
  return `${days}d ${rest}h`;
}

/**
 * The same duration in words, coarse enough to say out loud.
 *
 * `elapsedLabel` is written for the corner of an eye and changes every second
 * under ten minutes; read aloud it is a machine reciting "four s, five s, six
 * s" over the conversation. This is the version a screen reader gets: it
 * changes at most once a minute, so a reader who lands on the indicator hears
 * a useful answer and a reader who is elsewhere hears nothing.
 *
 * **It floors, because the number beside it floors.** A low-vision reader
 * running magnification with a screen reader gets both at once, and they are
 * one fact: rounding here printed "Working 1h 12m" while saying "about 1 hour
 * 13 minutes", and at the top of the scale "23h" against "about 23 hours 59
 * minutes" — a disagreement big enough to look like two different clocks. So
 * this walks the same units `elapsedLabel` does and drops the same ones (the
 * minutes past ten hours, the hours past ten days), and the two now name the
 * same quantity in different words.
 *
 * The one deliberate gap is seconds, which are never spoken: under a minute
 * the digits say "44s" and this says "under a minute", which contains the
 * visible number rather than contradicting it. Speaking the seconds is the
 * thing this function exists to avoid.
 *
 * "About" is honest as well as coarse — the number is a live count from a
 * server timestamp against the browser's own clock, and the second it lands
 * on was never the point.
 */
export function coarseElapsedLabel(seconds: number): string {
  const plural = (count: number, unit: string) => `${count} ${unit}${count === 1 ? "" : "s"}`;
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return "under a minute";
  const minutes = Math.floor(total / 60);
  if (minutes === 1) return "about a minute";
  if (minutes < 60) return `about ${plural(minutes, "minute")}`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 24) {
    if (hours >= 10 || restMinutes === 0) {
      return hours === 1 ? "about an hour" : `about ${plural(hours, "hour")}`;
    }
    return `about ${plural(hours, "hour")} ${plural(restMinutes, "minute")}`;
  }
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  if (days >= 10 || restHours === 0) {
    return days === 1 ? "about a day" : `about ${plural(days, "day")}`;
  }
  return `about ${plural(days, "day")} ${plural(restHours, "hour")}`;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/* ------------------------------------------------------------------ */
/* Questions an agent asked the person                                  */
/* ------------------------------------------------------------------ */

/** A `question` message the agent addressed to the person, not the
 * agent-to-agent work request that shares the same `message_type`. */
export function isUserQuestionMessage(
  message: Pick<ConversationMessage, "content_json">,
): boolean {
  return message.content_json.kind === "user_question";
}

const QUESTION_STATUSES: ReadonlySet<string> = new Set([
  "pending",
  "answered",
  "expired",
  "cancelled",
]);

function questionOptions(raw: unknown): UserQuestionOption[] {
  if (!Array.isArray(raw)) return [];
  const options: UserQuestionOption[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) continue;
    const record = entry as Record<string, unknown>;
    const value = str(record.value);
    if (!value) continue;
    options.push({ value, label: str(record.label) || value, detail: str(record.detail) });
  }
  return options;
}

/**
 * Normalize a question message's `content_json` into something the card can
 * render without guarding every field. The keys are guaranteed by the API
 * contract, but this row is mutated in place as the question is answered or
 * closed, and a half-written or older shape must degrade to a readable card
 * rather than throw in the middle of a transcript. Returns null when the
 * message is not a question at all.
 */
export function readUserQuestion(
  message: Pick<ConversationMessage, "content_json">,
): UserQuestionContent | null {
  if (!isUserQuestionMessage(message)) return null;
  const content = message.content_json;
  const status = str(content.status);
  const answerKind = str(content.answer_kind);
  return {
    kind: "user_question",
    question_id: str(content.question_id),
    question: str(content.question),
    context: str(content.context),
    question_kind: str(content.question_kind) === "memory_scope" ? "memory_scope" : "open",
    required: content.required === true,
    input_key: str(content.input_key),
    value_type: ["url", "timezone", "time"].includes(str(content.value_type)) ? content.value_type as "url" | "timezone" | "time" : "text",
    options: questionOptions(content.options),
    allow_other: content.allow_other !== false,
    other_label: str(content.other_label) || "Something else",
    other_placeholder: str(content.other_placeholder) || "Tell me in your own words…",
    // An unrecognized status is treated as still open: showing the buttons on
    // a question that turns out to be closed costs one refused POST, while
    // hiding them on a live one strands the run until it times out.
    status: (QUESTION_STATUSES.has(status) ? status : "pending") as UserQuestionStatus,
    expires_at: str(content.expires_at),
    asked_by_agent_name: str(content.asked_by_agent_name),
    ...(answerKind === "option" || answerKind === "other" ? { answer_kind: answerKind } : {}),
    answer_option_value: str(content.answer_option_value),
    answer: str(content.answer),
    answered_by_name: str(content.answered_by_name),
    answered_at: str(content.answered_at),
  };
}

/* ------------------------------------------------------------------ */
/* Memories the agent wrote during the conversation                     */
/* ------------------------------------------------------------------ */

/** A `status` message the memory tool wrote because a record was stored. */
export function isMemorySavedMessage(
  message: Pick<ConversationMessage, "content_json" | "sender_type" | "message_type">,
): boolean {
  // Sender and type as well as the key. This is the one card whose entire
  // purpose is to be evidence rather than a claim, so it must not render for
  // anything a person could have written. No client can set content_json
  // today -- the turn endpoints take only text -- but the guard belongs on the
  // card that would be worth forging.
  return (
    message.content_json.kind === "memory_saved" &&
    message.sender_type === "agent" &&
    message.message_type === "status"
  );
}

const MEMORY_SCOPES: ReadonlySet<string> = new Set(["agent", "team", "workspace"]);

/** Last resort when the payload has no `scope_label`. Deliberately vague
 * about *which* team: the platform owns the real name, and inventing one here
 * would be the mislabelling this card exists to catch. */
const SCOPE_FALLBACKS: Record<MemoryScope, string> = {
  agent: "just you and me",
  team: "your team",
  workspace: "everyone in the workspace",
};

/**
 * Normalize a `memory_saved` receipt into something the card can render
 * without guarding every field. Returns null when the message is not one.
 *
 * `action` decides the heading; the replaced words are shown whenever they
 * are there, so a payload that forgot to say "updated" still shows the change
 * rather than quietly dropping what was overwritten.
 */
export function readMemorySaved(
  message: Pick<ConversationMessage, "content_json" | "sender_type" | "message_type">,
): MemorySavedContent | null {
  if (!isMemorySavedMessage(message)) return null;
  const content = message.content_json;
  const rawScope = str(content.scope);
  const scope = MEMORY_SCOPES.has(rawScope) ? (rawScope as MemoryScope) : null;
  return {
    kind: "memory_saved",
    memory_id: str(content.memory_id),
    action: str(content.action) === "updated" ? "updated" : "saved",
    scope,
    scope_label: str(content.scope_label) || (scope ? SCOPE_FALLBACKS[scope] : "this agent"),
    content: str(content.content),
    superseded: str(content.superseded),
    still_standing: str(content.still_standing),
  };
}

/* ------------------------------------------------------------------ */
/* Runs that failed                                                     */
/* ------------------------------------------------------------------ */

/** The system row a failed run leaves in the transcript. */
export function isRunFailureMessage(
  message: Pick<ConversationMessage, "sender_type" | "message_type">,
): boolean {
  return message.sender_type === "system" && message.message_type === "error";
}

/**
 * What the card says when the API sent no notice at all: the same generic
 * sentence `jhin_domain.failures` falls back to, so the two ends agree.
 */
const NO_NOTICE_SUMMARY = "The run stopped before it finished.";

/**
 * The failure a person reads, or null when this row is not one.
 *
 * The notice is written by the API (`jhin_domain.failures`) so that the chat,
 * the activity feed and the inbox describe the same failure in the same
 * words.
 *
 * **A missing notice never falls back to the row's own text.** That text is
 * the run's note to whoever owns the incident — "Run failed: tool call
 * a34dd1dc-… execution outcome is unknown; manual reconciliation is
 * required" — and rendering it is the exact regression this card was built to
 * end. There are two ways to meet a row without a notice and neither is worth
 * that: a message cached from before the deploy, which revalidation replaces
 * within the minute, and a new web served by an API that predates the field,
 * which is a deploy done in the wrong order (`docs/deployment.md` step 6:
 * `api` before `web`). Both get the generic sentence, which says the true
 * thing and says nothing internal — so the ordering requirement cannot cost a
 * person a screenful of vocabulary written for somebody else, only some
 * detail for as long as it takes to finish the rollout.
 *
 * `code` is still read off the row, because that is a fixed vocabulary rather
 * than prose and it is what keeps the "out of credit → Models" link working
 * on a stale card.
 */
export function readFailure(
  message: Pick<
    ConversationMessage,
    "failure" | "sender_type" | "message_type" | "content_json"
  >,
): FailureNotice | null {
  if (!isRunFailureMessage(message)) return null;
  const notice = message.failure;
  if (notice && notice.summary.trim()) return notice;
  return {
    code: str(message.content_json.error_code),
    summary: NO_NOTICE_SUMMARY,
    detail: "",
    reference: "",
  };
}

/* ------------------------------------------------------------------ */
/* The name an agent gave itself                                        */
/* ------------------------------------------------------------------ */

/** A `status` message `organization.identity.set_name` wrote because the
 * agent row actually changed. Sender and type are checked as well as the key,
 * for the same reason the memory receipt checks them: this card is evidence,
 * so it must not render for anything a person could have written. */
export function isAgentRenamedMessage(
  message: Pick<ConversationMessage, "content_json" | "sender_type" | "message_type">,
): boolean {
  return (
    message.content_json.kind === "agent_renamed" &&
    message.sender_type === "agent" &&
    message.message_type === "status"
  );
}

/** Normalize an `agent_renamed` receipt. Returns null when the message is not
 * one, or when it carries no new name — a rename card that cannot say what
 * the agent is called now is not evidence of anything. */
export function readAgentRenamed(
  message: Pick<ConversationMessage, "content_json" | "sender_type" | "message_type">,
): AgentRenamedContent | null {
  if (!isAgentRenamedMessage(message)) return null;
  const content = message.content_json;
  const name = str(content.name);
  if (!name.trim()) return null;
  return {
    kind: "agent_renamed",
    previous_name: str(content.previous_name),
    name,
    slug: str(content.slug),
  };
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
  const memory = readMemorySaved(message);
  if (memory !== null) {
    return memory.action === "updated" ? "Updated a memory" : "Remembered something";
  }
  const renamed = readAgentRenamed(message);
  if (renamed !== null) return `Now called ${renamed.name}`;
  const question = readUserQuestion(message);
  if (question !== null) {
    const agent = question.asked_by_agent_name || "Your agent";
    if (question.status === "answered") return `You answered ${agent}`;
    if (question.status === "pending") return `${agent} needs an answer`;
    return `${agent} stopped waiting`;
  }
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
  message: Pick<ConversationMessage, "message_type" | "sender_type" | "content_json">,
): boolean {
  // A question addressed to the person shares `message_type: "question"` with
  // agent-to-agent work requests, and it is not a work card: it has its own
  // card, with the controls the whole thread is blocked on. Explicit rather
  // than incidental, so a later change to WORK_CARD_TYPES cannot bury it.
  if (isUserQuestionMessage(message)) return false;
  // Same story for a memory receipt: it arrives as a `status` message, and the
  // generic work card would render it as "Shared an update" with the
  // remembered words clamped into a preview. It has its own card.
  if (isMemorySavedMessage(message)) return false;
  // And a rename receipt, for the same reason: "Now called Bisby" is not
  // an update to share, it is a change to how this agent is identified
  // everywhere.
  if (isAgentRenamedMessage(message)) return false;
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
  | { kind: "activity"; id: string; at: string; card: ActivityCard }
  | { kind: "tool"; id: string; at: string; call: ConversationToolCall };

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
  options: { detailed?: boolean; toolCalls?: readonly ConversationToolCall[] } = {},
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

  for (const call of options.toolCalls ?? []) {
    const id = `tool:${call.id}`;
    if (seen.has(id)) continue;
    seen.add(id);
    items.push({ kind: "tool", id, at: call.created_at, call });
  }

  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      const delta = new Date(a.item.at).getTime() - new Date(b.item.at).getTime();
      if (delta !== 0) return delta;
      const priority = { message: 0, tool: 1, activity: 2 };
      if (a.item.kind !== b.item.kind) return priority[a.item.kind] - priority[b.item.kind];
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

export type TranscriptItem = TimelineItem | ExchangeItem | DaySeparatorItem
  | { kind: "runtime"; id: string; at: string; item: ConversationItem }
  | { kind: "generation"; id: string; at: string; item: ConversationItem }
  | { kind: "file"; id: string; at: string; file: ManagedFile };

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
  // A question the person has to answer is never quiet background traffic —
  // folding it into a collapsed exchange row would hide the one control the
  // conversation is waiting on behind a disclosure triangle.
  if (isUserQuestionMessage(message)) return null;
  // Nor is a memory receipt, wherever it came from. A memory written while a
  // colleague was doing the work is still a memory in this workspace, and the
  // moment the person is most likely to catch a wrong one is when it appears
  // — not behind a disclosure triangle they have no reason to open.
  if (isMemorySavedMessage(message)) return null;
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

  // Work cards are always exchange traffic. A plain turn is too when it comes
  // from a colleague rather than the agent the user is talking to: the user
  // asked their own agent, so a colleague's reply belongs inside the exchange
  // they can expand, not loose in their dialogue as if it were addressed to
  // them.
  if (!isWorkCard(message)) {
    if (message.sender_type !== "agent") return null;
    if (!exists(sender) || isPrimary(sender)) return null;
  }

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
  if (last.kind === "tool") return last.call.status === "failed" ? "problem" : "ok";
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
    if (only.kind === "tool") return "Terminal";
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
    if (item.kind === "tool") {
      // Actual execution evidence stays visible, including a colleague's
      // commands; a quiet exchange must not hide the running terminal.
      flush();
      result.push(item);
      continue;
    }
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

/** Composer hint shown while a task is active on this conversation: concrete
 * about what sending now actually does, not a vague status echo. Null when
 * nothing is active (the caller falls back to the default "Enter to
 * send…" hint). */
export function composerHintFor(liveStatus: LiveStatus | null, agentName: string): string | null {
  if (!liveStatus) return null;
  if (liveStatus.kind === "working") {
    return `${agentName} is working — this will steer it at the next step.`;
  }
  if (liveStatus.kind === "queued") {
    return `${agentName} hasn't started yet — this will steer it once it does.`;
  }
  if (liveStatus.kind === "waiting_delegation") {
    // Live, but not this agent's move. The turn still picks a queued
    // instruction up at its next step, which is after the colleague replies.
    return `${agentName} is waiting on a colleague — this will steer it once they reply.`;
  }
  return null;
}

/** Build the body for a new turn. */
export function newTurn(text: string): { text: string; client_turn_id: string } {
  return { text: text.trim(), client_turn_id: crypto.randomUUID() };
}

/* ------------------------------------------------------------------ */
/* Queued-instruction delivery state                                    */
/* ------------------------------------------------------------------ */

/** Enough of an agent message or activity card to serve as evidence that a
 * queued instruction was actually delivered to the running workflow. */
export interface DeliveryEvidence {
  created_at: string;
  task_id?: string | null;
}

/**
 * Whether a mid-run "instruction" turn has already been picked up by the
 * workflow. The mechanism (packages/workflows agent_task) delivers all
 * pending instructions as text at the start of the *next* step, so there is
 * no direct "delivered" event — instead, any agent message or activity item
 * on the same task that landed strictly after the instruction was sent is
 * treated as proof a step ran and included it. Pure and unit-tested.
 */
export function instructionDeliveryState(
  message: Pick<ConversationMessage, "created_at" | "task_id"> & Partial<Pick<ConversationMessage, "content_json">>,
  laterItems: readonly DeliveryEvidence[],
): "queued" | "delivered" {
  // New runs persist the exact step that consumed an instruction. Legacy
  // records retain the existing activity-based fallback below.
  if (message.content_json?.delivery === "consumed") return "delivered";
  if (message.content_json?.delivery === "queued" || message.content_json?.delivery === "pending") return "queued";
  const sentAt = new Date(message.created_at).getTime();
  if (Number.isNaN(sentAt)) return "queued";
  const delivered = laterItems.some((item) => {
    const at = new Date(item.created_at).getTime();
    if (Number.isNaN(at) || at <= sentAt) return false;
    if (message.task_id && item.task_id && item.task_id !== message.task_id) return false;
    return true;
  });
  return delivered ? "delivered" : "queued";
}

/* ------------------------------------------------------------------ */
/* Carrying a half-typed message across the new-chat redirect          */
/* ------------------------------------------------------------------ */

const CARRIED_DRAFT_PREFIX = "jhin-chat-carry:";
const privateCarriedDrafts = new Map<string, string>();

/** Hand a draft to the conversation page the first turn is redirecting to.
 * Starting a chat navigates from /chats to /chats/{id}, and anything typed
 * in that window would otherwise die with the unmounted page. */
export function stashCarriedDraft(conversationId: string, text: string): void {
  if (typeof window === "undefined" || text.trim() === "") return;
  if (mayContainSecret(text)) { privateCarriedDrafts.set(conversationId, text); window.sessionStorage.removeItem(`${CARRIED_DRAFT_PREFIX}${conversationId}`); return; }
  privateCarriedDrafts.delete(conversationId);
  try {
    window.sessionStorage.setItem(`${CARRIED_DRAFT_PREFIX}${conversationId}`, text);
  } catch {
    // Private mode or quota: losing the carry-over is no worse than today.
  }
}

/** Read and clear a draft handed over by the new-chat page. */
export function takeCarriedDraft(conversationId: string): string {
  if (typeof window === "undefined") return "";
  const privateText = privateCarriedDrafts.get(conversationId);
  if (privateText !== undefined) { privateCarriedDrafts.delete(conversationId); return privateText; }
  const key = `${CARRIED_DRAFT_PREFIX}${conversationId}`;
  try {
    const value = window.sessionStorage.getItem(key) ?? "";
    if (value) window.sessionStorage.removeItem(key);
    return mayContainSecret(value) ? "" : value;
  } catch {
    return "";
  }
}
