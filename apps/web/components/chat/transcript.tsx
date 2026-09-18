"use client";

/** The chat transcript: bubbles, work cards, system chips, quiet
 * agent-to-agent exchange rows, date separators, inline approvals, and the
 * working indicator. Scroll sticks to the bottom only when the reader is
 * already there. */

import { ArrowDown, ChevronDown, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { Avatar } from "@/components/avatar";
import { FailureCard } from "@/components/chat/failure-card";
import { MemoryCard } from "@/components/chat/memory-card";
import { QuestionCard, type AnswerQuestion } from "@/components/chat/question-card";
import { RenameCard } from "@/components/chat/rename-card";
import { Timestamp } from "@/components/chat/timestamp";
import { SecureInputReceipt } from "@/components/chat/secure-input";
import { ActionCard } from "@/components/chat/action-card";
import { ArtifactCard } from "@/components/chat/artifact-card";
import { SessionActivityCard } from "@/components/chat/session-activity-card";
import type { ManagedFile } from "@/lib/workspace-files";
import type { ActivityDetail } from "@/lib/agentic-chat";
import { TranscriptWorkingTime } from "@/components/chat/working-time";
import { Markdown } from "@/components/markdown";
import { MessageTypeBadge, StructuredMessageBody } from "@/components/task-bits";
import { Spinner } from "@/components/ui";
import {
  exchangeLabel,
  exchangeSuffix,
  friendlyMessageLabel,
  instructionDeliveryState,
  isAgentRenamedMessage,
  isMemorySavedMessage,
  isRunFailureMessage,
  isUserQuestionMessage,
  isWorkCard,
  messageText,
  workRequestDetailLines,
  type DaySeparatorItem,
  type DeliveryEvidence,
  type ExchangeItem,
  type LiveStatus,
  type TimelineItem,
  type TranscriptItem,
} from "@/lib/chat";
import { isWorkRequestMessage } from "@/lib/coordination";
import { avatarProps } from "@/lib/media";
import type {
  ActivityCard,
  AgentAvatar,
  Approval,
  ConversationMessage,
  ConversationResume,
} from "@/lib/types";

function UserBubble({
  message,
  name,
  agentName,
  deliveryState,
}: {
  message: ConversationMessage;
  name: string;
  agentName: string;
  /** For `message_type: "instruction"` turns sent while a task was active:
   * "queued" until later activity on the same task proves it was picked up,
   * then "delivered". Undefined for ordinary messages. */
  deliveryState?: "queued" | "delivered";
}) {
  const text = messageText(message);
  const instruction = message.message_type === "instruction";
  return (
    <div data-testid="user-message" className="flex justify-end">
      <div className="max-w-[min(85%,40rem)]">
        {/* The authoritative, sanitized user text stays literal. Markdown is rendered where an agent is
         * speaking to the reader in prose, not everywhere a string appears:
         * what a person typed is the message, and formatting it would eat the
         * characters they meant — `**not bold**`, a `snake_case` name, a path
         * full of underscores — with no way to escape them from a plain chat
         * box. Work cards follow the same rule for the same reason: they show
         * clamped, truncated field values, and half a fence is not markdown. */}
        <div className="rounded-2xl rounded-br-md bg-accent-soft px-4 py-2.5 text-[15px] leading-relaxed text-ink">
          <p className="whitespace-pre-wrap break-words">{text}</p>
          <SecureInputReceipt content={message.content_json} />
        </div>
        {instruction && deliveryState === "queued" ? (
          <p className="mt-1 flex justify-end">
            <span
              data-testid="instruction-status"
              data-state="queued"
              className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-accent/30 bg-accent-soft px-2.5 py-0.5 text-[11px] font-medium text-accent-strong"
            >
              <span
                aria-hidden
                className="h-1.5 w-1.5 shrink-0 rounded-full bg-current motion-safe:animate-pulse"
              />
              <span className="truncate">Queued — will steer {agentName} at its next step</span>
            </span>
          </p>
        ) : instruction && deliveryState === "delivered" ? (
          <p
            data-testid="instruction-status"
            data-state="delivered"
            className="mt-1 text-right text-[11px] text-faint"
          >
            <span className="sr-only">{name}, </span>
            Steered {agentName} · <Timestamp iso={message.created_at} className="inline" />
          </p>
        ) : (
          <p className="mt-1 text-right">
            <span className="sr-only">{name}, </span>
            <Timestamp iso={message.created_at} />
          </p>
        )}
      </div>
    </div>
  );
}

function MessageActions({ message, onEdit, onBranch }: { message: ConversationMessage; onEdit?: (message: ConversationMessage) => void; onBranch?: (message: ConversationMessage) => void }) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  return <div className="ml-10 mt-1 flex gap-1 text-[11px] text-faint"><button type="button" className="min-h-8 rounded px-2 hover:bg-hover" onClick={() => { if (!navigator.clipboard) { setCopyFailed(true); return; } void navigator.clipboard.writeText(messageText(message)).then(() => setCopied(true)).catch(() => setCopyFailed(true)); }}>{copied ? "Copied" : copyFailed ? "Select text to copy" : "Copy"}</button>{onEdit ? <button type="button" className="min-h-8 rounded px-2 hover:bg-hover" onClick={() => onEdit(message)}>{message.sender_type === "user" ? "Edit and resend" : "Use in new message"}</button> : null}{onBranch ? <button type="button" className="min-h-8 rounded px-2 hover:bg-hover" onClick={() => onBranch(message)}>Branch from here</button> : null}</div>;
}

function AgentBubble({
  message,
  name,
  avatar,
}: {
  message: ConversationMessage;
  name: string;
  avatar?: AgentAvatar | null;
}) {
  const text = messageText(message);
  return (
    <div data-testid="agent-message" className="flex items-end gap-2.5">
      <Avatar name={name} size="sm" className="mb-5" {...avatarProps(avatar)} />
      {/* `min-w-0` lets the bubble stay inside its max width even when it holds
       * a code block with an unbreakable line: without it the flex item's
       * automatic minimum size wins over `max-w` and the bubble stretches. */}
      <div className="min-w-0 max-w-[min(85%,40rem)]">
        <div className="rounded-2xl rounded-bl-md border border-line bg-surface px-4 py-2.5 text-[15px] leading-relaxed text-ink shadow-[var(--card-shadow)]">
          {/* An agent writes markdown, so render it. See `UserBubble` for why
           * what a person typed stays literal. */}
          <Markdown source={text} variant="chat" />
        </div>
        <p className="mt-1">
          <span className="text-[11px] text-faint">{name} · </span>
          <Timestamp iso={message.created_at} />
        </p>
      </div>
    </div>
  );
}

function WorkCard({
  message,
  name,
  avatar,
}: {
  message: ConversationMessage;
  name: string;
  avatar?: AgentAvatar | null;
}) {
  const [open, setOpen] = useState(false);
  const label = friendlyMessageLabel(message);
  const summary = messageText(message);
  const short = summary.length > 220 ? `${summary.slice(0, 217)}…` : summary;
  const id = `work-card-${message.id}`;
  const workRequest = isWorkRequestMessage(message);
  const extraLines = workRequest ? workRequestDetailLines(message) : [];
  return (
    <div data-testid={workRequest ? "work-request-card" : "work-card"} className="flex items-start gap-2.5">
      <Avatar name={name} size="sm" {...avatarProps(avatar)} />
      <div className="min-w-0 max-w-[min(85%,40rem)] flex-1 rounded-2xl border border-line bg-raised px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-medium text-ink">{label}</p>
          <Timestamp iso={message.created_at} />
        </div>
        {short && !open ? <p className="mt-1 break-words text-sm text-dim">{short}</p> : null}
        {extraLines.length > 0 && !open ? (
          <ul className="mt-1 space-y-0.5 text-[13px] text-dim">
            {extraLines.map((line) => (
              <li key={line} className="line-clamp-3 whitespace-pre-wrap">{line}</li>
            ))}
          </ul>
        ) : null}
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((value) => !value)}
          className="mt-2 inline-flex min-h-[40px] items-center gap-1 text-xs font-medium text-accent-strong hover:underline"
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          {open ? "Hide details" : "Show details"}
        </button>
        {open ? (
          <div id={id} className="mt-1 space-y-2 border-t border-line pt-2">
            <MessageTypeBadge type={message.message_type} content={message.content_json} />
            <StructuredMessageBody content={message.content_json} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

const CHIP_TONES: Partial<Record<ActivityCard["kind"], string>> = {
  failed: "border-danger/30 bg-danger/10 text-danger",
  needs_review: "border-warn/30 bg-warn/10 text-warn",
  finished: "border-ok/30 bg-ok/10 text-ok",
  paused: "border-warn/30 bg-warn/10 text-warn",
  // Distinct from "failed" (red, "Ran into a problem"): a stop was
  // requested, not an error.
  stopped: "border-line-strong bg-hover text-ink",
};

function ActivityChip({ card }: { card: ActivityCard }) {
  const tone = CHIP_TONES[card.kind] ?? "border-line bg-raised text-dim";
  const showSummary = (card.kind === "failed" || card.kind === "needs_review") && card.summary;
  return (
    <div data-testid="activity-chip" className="flex justify-center">
      <div className="max-w-[80%] text-center">
        <span
          className={`inline-flex min-h-[28px] items-center gap-2 rounded-full border px-3 py-1 text-xs ${tone}`}
        >
          <span>{card.label}</span>
          <Timestamp iso={card.created_at} className="opacity-80" />
        </span>
        {showSummary ? <p className="mt-1 text-xs text-dim">{card.summary}</p> : null}
      </div>
    </div>
  );
}

function SystemChip({ message }: { message: ConversationMessage }) {
  const text = messageText(message);
  if (!text) return null;
  // A long notice gets the wrapping card treatment rather than the one-line
  // chip, which would truncate exactly the part worth reading. Failures are
  // not here at all any more: they have their own card, with the way out on
  // it (see `FailureCard`).
  if (text.length > 120) {
    return (
      <div data-testid="system-card" className="flex justify-center">
        <div className="max-w-[min(90%,36rem)] rounded-2xl border border-line bg-raised px-4 py-3 text-sm text-dim">
          <p className="break-words">{text}</p>
          <p className="mt-1 text-xs">
            <Timestamp iso={message.created_at} />
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="flex justify-center">
      <span className="inline-flex max-w-[80%] items-center gap-2 rounded-full border border-line bg-raised px-3 py-1 text-xs text-dim">
        <span className="truncate">{text}</span>
        <Timestamp iso={message.created_at} />
      </span>
    </div>
  );
}

/** Centered faint "Today · 10:04 AM" marker when the day changes. */
function DaySeparator({ item }: { item: DaySeparatorItem }) {
  return (
    <div data-testid="day-separator" className="flex items-center justify-center py-1">
      <span className="inline-flex items-center gap-1.5 text-[11px] text-faint">
        <span className="font-medium">{item.label}</span>
        <span aria-hidden>·</span>
        <time dateTime={item.at}>{item.time}</time>
      </span>
    </div>
  );
}

/** A collapsed run of agent↔agent traffic: one subtle centered row that
 * expands inline to the full cards. State lives per exchange and is not
 * persisted; `defaultOpen` follows the transcript's "detailed" toggle. */
function ExchangeRow({
  exchange,
  agentAvatars,
  defaultOpen = false,
  renderItem,
}: {
  exchange: ExchangeItem;
  agentAvatars?: Record<string, AgentAvatar | null>;
  defaultOpen?: boolean;
  renderItem: (item: TimelineItem) => React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const regionId = `exchange-${exchange.at}-${exchange.count}`;
  const avatar = exchange.withAgentId ? agentAvatars?.[exchange.withAgentId] : null;
  return (
    <div data-testid="exchange" className="flex flex-col gap-4">
      <div className="flex justify-center">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={regionId}
          onClick={() => setOpen((value) => !value)}
          className="inline-flex min-h-11 max-w-[85%] items-center gap-2 rounded-full px-3 py-1 text-xs text-faint transition-colors hover:bg-hover hover:text-dim md:min-h-[32px]"
        >
          <Avatar name={exchange.withName} size="xs" {...avatarProps(avatar)} />
          <span className="truncate">
            {exchangeLabel(exchange)}
            {exchangeSuffix(exchange.outcome)}
          </span>
          <Timestamp iso={exchange.at} className="opacity-80" />
          {open ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
        </button>
      </div>
      {open ? (
        <div id={regionId} className="flex flex-col gap-4">
          {exchange.items.map(renderItem)}
        </div>
      ) : null}
    </div>
  );
}

function WorkingIndicator({
  status,
  name,
  avatar,
}: {
  status: LiveStatus;
  name: string;
  avatar?: AgentAvatar | null;
}) {
  if (status.kind === "working") {
    return (
      // `aria-live="off"` against the transcript's own polite region: this row
      // is rewritten every few seconds as the agent moves from step to step,
      // and announcing each one would talk over the messages a screen-reader
      // user is actually here to read. The work itself still announces when it
      // lands, as a message.
      <div
        data-testid="working-indicator"
        data-specific={status.specific ? "true" : undefined}
        aria-live="off"
        className="flex items-end gap-2.5 text-sm text-dim"
      >
        <Avatar name={name} size="sm" className="mb-5" {...avatarProps(avatar)} />
        {/* The elapsed line is a sibling of the bubble, not a passenger inside
         * it, and that placement is the point. Set against the sentence the
         * way the header pill has to set it — "Making a change in GitHub ·
         * 1h 12m" — the number reads as that one step's duration; on its own
         * line under the bubble, in the same position a message's timestamp
         * takes, it reads as what it is. */}
        <div className="min-w-0">
          <span className="inline-flex min-w-0 items-center gap-2 rounded-2xl rounded-bl-md border border-line bg-surface px-4 py-2.5">
            <span aria-hidden className="flex shrink-0 items-center gap-1">
              {[0, 1, 2].map((index) => (
                <span
                  key={index}
                  className="h-1.5 w-1.5 rounded-full bg-accent motion-safe:animate-bounce"
                  style={{ animationDelay: `${index * 150}ms` }}
                />
              ))}
            </span>
            {/* The API's sentence stands on its own next to the avatar, the way
             * the header pill shows it. Only the generic state keeps the "…is
             * working" phrasing, so nothing shifts when there is nothing more
             * specific to say. */}
            {status.specific ? (
              <span className="min-w-0 break-words">
                <span className="sr-only">{name}: </span>
                {status.label}
              </span>
            ) : (
              <span className="min-w-0 break-words">{name} is working…</span>
            )}
          </span>
          {/* Absent `since` is a status carrying no clock at all: an API that
           * predates the working clock never sent the field, so nothing was
           * measured and there is nothing to say — no line, no note, no
           * tooltip, which is exactly the "less detail, never a wrong answer"
           * an out-of-order rollout is allowed to cost (`docs/deployment.md`
           * step 6).
           *
           * A `since` of `null` is the opposite fact: the API measured and no
           * instant came out of it, because the turn is parked on an
           * approval, question or review that was opened and never closed.
           * That one is worth a line — with the seconds it banked before it
           * stalled, when it banked any — since the alternative is the
           * feature vanishing without a word.
           *
           * `statusLabelFor` is what keeps the two apart; it reads the field
           * straight rather than through a `??`, which had collapsed the
           * first case onto the second and told every reader on an older API
           * that their agent was stuck behind an approval. */}
          {status.since !== undefined ? (
            <TranscriptWorkingTime since={status.since} worked={status.worked ?? 0} />
          ) : null}
        </div>
      </div>
    );
  }
  // Every wait says who it is on. Delegation is the one where that is not the
  // reader and not this agent either: a colleague has the work, nothing is
  // asked of anybody here, and the honest line names them when the API sent
  // their name and stays vague when it did not. No clock on any of these —
  // see `statusLabelFor`.
  const text =
    status.kind === "queued"
      ? `${name} is waiting for a free slot and will start shortly.`
      : status.kind === "review"
        ? "Waiting for your review — see the request above."
        : status.kind === "question"
          ? "Waiting for your answer — see the question above."
          : status.kind === "waiting_review"
            ? "Waiting for a review of this work."
            : status.kind === "waiting_delegation"
              ? status.specific
                ? `${status.label} — ${name} picks this up again when they reply.`
                : `${name} is waiting for a colleague and picks this up again when they reply.`
              : `${name} is paused. Resume from Details when you're ready.`;
  return (
    <div data-testid="working-indicator" className="flex justify-center">
      <span className="rounded-full border border-line bg-raised px-3 py-1 text-xs text-dim">{text}</span>
    </div>
  );
}

/** Flatten the rendered timeline (unwrapping collapsed exchanges) into the
 * evidence pool `instructionDeliveryState` checks each queued instruction
 * against: every agent message and activity chip, in whatever order they
 * appear. */
function collectDeliveryEvidence(items: readonly TranscriptItem[]): DeliveryEvidence[] {
  const evidence: DeliveryEvidence[] = [];
  const consider = (entry: TimelineItem) => {
    if (entry.kind === "activity") {
      evidence.push({ created_at: entry.card.created_at, task_id: entry.card.task_id });
    } else if (entry.kind === "message" && entry.message.sender_type === "agent") {
      evidence.push({ created_at: entry.message.created_at, task_id: entry.message.task_id });
    }
  };
  for (const item of items) {
    if (item.kind === "day" || item.kind === "file" || item.kind === "runtime" || item.kind === "generation") continue;
    if (item.kind === "exchange") {
      for (const sub of item.items) consider(sub);
      continue;
    }
    consider(item);
  }
  return evidence;
}

export function Transcript({
  items,
  agentName,
  userName,
  pendingApprovals = [],
  canDecide = false,
  deciding = false,
  onApprove,
  onReject,
  canAnswer = false,
  answering = false,
  onAnswer,
  liveStatus = null,
  loading = false,
  agentAvatars,
  agentAvatar,
  expandExchanges = false,
  resume = null,
  canRetry = false,
  retrying = false,
  onRetry,
  onReuse,
  terminalNotice,
  activityDetail = "standard",
  logsBase,
  onEditMessage,
  onBranchMessage,
  beforeItems,
  afterItems,
  liveRevision = "",
  runtimeBase,
  onOpenFile,
  onUseFile,
}: {
  items: TranscriptItem[];
  agentName: string;
  userName: string;
  pendingApprovals?: Approval[];
  canDecide?: boolean;
  deciding?: boolean;
  onApprove?: (id: string) => void;
  onReject?: (id: string) => void;
  /** Member or above: a viewer sees the question but cannot answer it. */
  canAnswer?: boolean;
  answering?: boolean;
  onAnswer?: AnswerQuestion;
  liveStatus?: LiveStatus | null;
  loading?: boolean;
  /** Agent id → avatar visuals for messages from other agents. */
  agentAvatars?: Record<string, AgentAvatar | null>;
  /** The primary agent's avatar (working indicator, unnamed senders). */
  agentAvatar?: AgentAvatar | null;
  /** True (the "detailed" toggle) expands collapsed exchanges by default. */
  expandExchanges?: boolean;
  /** The offer for the newest failed turn, when this chat has one. */
  resume?: ConversationResume | null;
  /** Member or above: a viewer reads a failure but cannot act on it. */
  canRetry?: boolean;
  retrying?: boolean;
  onRetry?: () => void;
  /** Put a failed turn's words back in the composer without sending them. */
  onReuse?: (text: string) => void;
  terminalNotice?: string;
  activityDetail?: ActivityDetail;
  logsBase?: string;
  onEditMessage?: (message: ConversationMessage) => void;
  onBranchMessage?: (message: ConversationMessage) => void;
  beforeItems?: React.ReactNode;
  afterItems?: React.ReactNode;
  liveRevision?: string;
  runtimeBase?: string;
  onOpenFile?: (file: ManagedFile) => void;
  onUseFile?: (file: ManagedFile) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [hasNew, setHasNew] = useState(false);
  const terminalRevision = JSON.stringify(items.filter((item) => item.kind === "tool").map((item) => [
    item.id, item.call.status, item.call.sandbox_job, item.call.sanitized_output_json,
  ]));
  const contentKey = `${items.length}:${pendingApprovals.length}:${liveStatus?.kind ?? ""}:${terminalRevision}:${liveRevision}`;
  const previousKey = useRef<string | null>(null);

  const scrollToBottom = useCallback((smooth: boolean) => {
    const node = scrollRef.current;
    if (!node) return;
    const reduce =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (typeof node.scrollTo === "function") {
      node.scrollTo({ top: node.scrollHeight, behavior: smooth && !reduce ? "smooth" : "auto" });
    } else {
      node.scrollTop = node.scrollHeight;
    }
    setHasNew(false);
  }, []);

  const onScroll = () => {
    const node = scrollRef.current;
    if (!node) return;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    atBottomRef.current = distance < 48;
    if (atBottomRef.current) setHasNew(false);
  };

  useEffect(() => {
    if (previousKey.current === null) {
      previousKey.current = contentKey;
      scrollToBottom(false);
      return;
    }
    if (previousKey.current === contentKey) return;
    previousKey.current = contentKey;
    if (atBottomRef.current) scrollToBottom(true);
    else setHasNew(true);
  }, [contentKey, scrollToBottom]);

  const deliveryEvidence = useMemo(() => collectDeliveryEvidence(items), [items]);

  const renderTimelineItem = (item: TimelineItem) => {
    if (item.kind === "activity") return <ActivityChip key={item.id} card={item.card} />;
    if (item.kind === "tool") return <ActionCard key={item.id} call={item.call} detail={activityDetail} logsUrl={logsBase ? `${logsBase}/${item.call.id}/logs` : undefined} />;
    const message = item.message;
    if (message.sender_type === "user") {
      const deliveryState =
        message.message_type === "instruction"
          ? instructionDeliveryState(message, deliveryEvidence)
          : undefined;
      return (<div key={item.id}>
        <UserBubble
          message={message}
          name={userName}
          agentName={agentName}
          deliveryState={deliveryState}
        /><MessageActions message={message} onEdit={onEditMessage} onBranch={onBranchMessage} />
      </div>);
    }
    if (message.sender_type === "system") {
      // Before the generic chip: a failure is the one system row a person may
      // need to *do* something about, and the chip has nowhere to put that.
      if (isRunFailureMessage(message)) {
        return (
          <FailureCard
            key={item.id}
            message={message}
            agentName={agentName}
            resume={resume}
            canAct={canRetry}
            retrying={retrying}
            onRetry={onRetry}
            onReuse={onReuse}
          />
        );
      }
      return <SystemChip key={item.id} message={message} />;
    }
    const name = message.sender_name ?? agentName;
    const avatar = message.agent_id ? (agentAvatars?.[message.agent_id] ?? null) : agentAvatar;
    // Before the work-card branch: a question to the person shares its
    // `message_type` with agent-to-agent work requests, and it is the one
    // card the thread is actually blocked on.
    if (isUserQuestionMessage(message)) {
      return (
        <QuestionCard
          key={item.id}
          message={message}
          userName={userName}
          avatar={avatar}
          canAnswer={canAnswer}
          answering={answering}
          onAnswer={onAnswer}
        />
      );
    }
    // Also before the work-card branch: a memory receipt is a `status`
    // message, and the generic card would clamp the remembered words into a
    // preview under "Shared an update".
    if (isMemorySavedMessage(message)) {
      return <MemoryCard key={item.id} message={message} name={name} avatar={avatar} />;
    }
    // Same reason as the memory receipt: a rename is a `status` message, and
    // the generic card would file "Now called Bisby" under "Shared an update".
    if (isAgentRenamedMessage(message)) {
      return <RenameCard key={item.id} message={message} name={name} avatar={avatar} />;
    }
    if (isWorkCard(message)) {
      return <WorkCard key={item.id} message={message} name={name} avatar={avatar} />;
    }
    // An agent turn with no text is not an answer — render nothing rather
    // than an empty bubble. The backend no longer writes these, but rows
    // saved before that fix still exist in transcripts.
    if (!messageText(message).trim()) return null;
    return <div key={item.id}><AgentBubble message={message} name={name} avatar={avatar} /><MessageActions message={message} onEdit={onEditMessage} onBranch={onBranchMessage} /></div>;
  };

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        role="log"
        aria-live="polite"
        aria-label="Conversation"
        aria-busy={loading || undefined}
        className="h-full overflow-y-auto px-4 py-6 sm:px-8"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-4">
          {terminalNotice ? <p className="text-center text-xs text-dim">{terminalNotice}</p> : null}
          {beforeItems}
          {items.map((item) => {
            if (item.kind === "day") return <DaySeparator key={item.id} item={item} />;
            if (item.kind === "generation") return (
              <div
                key={item.id}
                className="min-w-0 rounded-2xl border border-line bg-surface p-4 text-sm"
                aria-label={item.item.status === "completed" ? "Agent response" : "Agent response in progress"}
              >
                <Markdown source={String(item.item.data.text)} variant="chat" />
                {item.item.status !== "completed" ? <span className="text-xs text-faint">Responding…</span> : null}
              </div>
            );
            if (item.kind === "runtime") return <SessionActivityCard key={item.id} item={item.item} base={runtimeBase} detail={activityDetail} />;
            if (item.kind === "file") return onOpenFile && onUseFile ? <ArtifactCard key={item.id} file={item.file} onOpen={onOpenFile} onUse={onUseFile} /> : null;
            if (item.kind === "exchange") {
              return (
                <ExchangeRow
                  key={`${item.id}:${expandExchanges ? "open" : "closed"}`}
                  exchange={item}
                  agentAvatars={agentAvatars}
                  defaultOpen={expandExchanges}
                  renderItem={renderTimelineItem}
                />
              );
            }
            return renderTimelineItem(item);
          })}
          {afterItems}

          {pendingApprovals.length > 0 ? (
            <ul className="space-y-3" aria-label="Waiting for your review">
              {pendingApprovals.map((approval) => (
                <ApprovalCard
                  key={approval.id}
                  approval={approval}
                  canDecide={canDecide}
                  deciding={deciding}
                  onApprove={() => onApprove?.(approval.id)}
                  onReject={() => onReject?.(approval.id)}
                />
              ))}
            </ul>
          ) : null}

          {liveStatus ? <WorkingIndicator status={liveStatus} name={agentName} avatar={agentAvatar} /> : null}

          {loading && items.length === 0 ? (
            <div className="flex justify-center py-10">
              <Spinner label="Loading messages…" />
            </div>
          ) : null}

          {!loading && items.length === 0 && !liveStatus ? (
            <p className="py-10 text-center text-sm text-dim">
              Nothing here yet. Say hello to get started.
            </p>
          ) : null}
        </div>
      </div>

      {hasNew ? (
        <button
          type="button"
          onClick={() => scrollToBottom(true)}
          className="absolute bottom-3 left-1/2 inline-flex min-h-[40px] -translate-x-1/2 items-center gap-1.5 rounded-full border border-line bg-surface px-4 text-sm font-medium text-accent-strong shadow-[var(--card-shadow)] hover:bg-hover"
        >
          New messages <ArrowDown size={14} />
        </button>
      ) : null}
    </div>
  );
}
