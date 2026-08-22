"use client";

/** The chat transcript: bubbles, work cards, system chips, inline approvals,
 * and the working indicator. Scroll sticks to the bottom only when the reader
 * is already there. */

import { ArrowDown, ChevronDown, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { Avatar } from "@/components/avatar";
import { MessageTypeBadge, StructuredMessageBody } from "@/components/task-bits";
import {
  friendlyMessageLabel,
  isWorkCard,
  messageText,
  relativeTime,
  workRequestDetailLines,
  type LiveStatus,
  type TimelineItem,
} from "@/lib/chat";
import { isWorkRequestMessage } from "@/lib/coordination";
import { formatDateTime } from "@/lib/format";
import type { ActivityCard, Approval, ConversationMessage } from "@/lib/types";

function Timestamp({ iso, className = "" }: { iso: string; className?: string }) {
  return (
    <time dateTime={iso} title={formatDateTime(iso)} className={`text-[11px] text-faint ${className}`}>
      {relativeTime(iso)}
    </time>
  );
}

function UserBubble({ message, name }: { message: ConversationMessage; name: string }) {
  const text = messageText(message);
  const instruction = message.message_type === "instruction";
  return (
    <div data-testid="user-message" className="flex justify-end">
      <div className="max-w-[min(85%,40rem)]">
        {instruction ? (
          <p className="mb-1 text-right text-[11px] text-faint">Added while the agent was working</p>
        ) : null}
        <div className="rounded-2xl rounded-br-md bg-accent-soft px-4 py-2.5 text-[15px] leading-relaxed text-ink">
          <p className="whitespace-pre-wrap break-words">{text}</p>
        </div>
        <p className="mt-1 text-right">
          <span className="sr-only">{name}, </span>
          <Timestamp iso={message.created_at} />
        </p>
      </div>
    </div>
  );
}

function AgentBubble({
  message,
  name,
  avatarUrl,
}: {
  message: ConversationMessage;
  name: string;
  avatarUrl?: string | null;
}) {
  const text = messageText(message);
  return (
    <div data-testid="agent-message" className="flex items-end gap-2.5">
      <Avatar name={name} size="sm" className="mb-5" src={avatarUrl} />
      <div className="max-w-[min(85%,40rem)]">
        <div className="rounded-2xl rounded-bl-md border border-line bg-surface px-4 py-2.5 text-[15px] leading-relaxed text-ink shadow-[var(--card-shadow)]">
          <p className="whitespace-pre-wrap break-words">{text}</p>
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
  avatarUrl,
}: {
  message: ConversationMessage;
  name: string;
  avatarUrl?: string | null;
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
      <Avatar name={name} size="sm" src={avatarUrl} />
      <div className="min-w-0 max-w-[min(85%,40rem)] flex-1 rounded-2xl border border-line bg-raised px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-medium text-ink">{label}</p>
          <Timestamp iso={message.created_at} />
        </div>
        {short && !open ? <p className="mt-1 text-sm text-dim">{short}</p> : null}
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
  return (
    <div className="flex justify-center">
      <span className="inline-flex max-w-[80%] items-center gap-2 rounded-full border border-line bg-raised px-3 py-1 text-xs text-dim">
        <span className="truncate">{text}</span>
        <Timestamp iso={message.created_at} />
      </span>
    </div>
  );
}

function WorkingIndicator({
  status,
  name,
  avatarUrl,
}: {
  status: LiveStatus;
  name: string;
  avatarUrl?: string | null;
}) {
  if (status.kind === "working") {
    return (
      <div data-testid="working-indicator" className="flex items-center gap-2.5 text-sm text-dim">
        <Avatar name={name} size="sm" src={avatarUrl} />
        <span className="inline-flex items-center gap-2 rounded-2xl rounded-bl-md border border-line bg-surface px-4 py-2.5">
          <span aria-hidden className="flex items-center gap-1">
            {[0, 1, 2].map((index) => (
              <span
                key={index}
                className="h-1.5 w-1.5 rounded-full bg-accent motion-safe:animate-bounce"
                style={{ animationDelay: `${index * 150}ms` }}
              />
            ))}
          </span>
          {name} is working…
        </span>
      </div>
    );
  }
  const text =
    status.kind === "queued"
      ? `${name} is waiting for a free slot and will start shortly.`
      : status.kind === "review"
        ? "Waiting for your review — see the request above."
        : `${name} is paused. Resume from Details when you're ready.`;
  return (
    <div data-testid="working-indicator" className="flex justify-center">
      <span className="rounded-full border border-line bg-raised px-3 py-1 text-xs text-dim">{text}</span>
    </div>
  );
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
  liveStatus = null,
  loading = false,
  agentAvatars,
  agentAvatarUrl,
}: {
  items: TimelineItem[];
  agentName: string;
  userName: string;
  pendingApprovals?: Approval[];
  canDecide?: boolean;
  deciding?: boolean;
  onApprove?: (id: string) => void;
  onReject?: (id: string) => void;
  liveStatus?: LiveStatus | null;
  loading?: boolean;
  /** Agent id → avatar url for messages from other agents. */
  agentAvatars?: Record<string, string | null>;
  /** The primary agent's avatar (working indicator, unnamed senders). */
  agentAvatarUrl?: string | null;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [hasNew, setHasNew] = useState(false);
  const contentKey = `${items.length}:${pendingApprovals.length}:${liveStatus?.kind ?? ""}`;
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
          {items.map((item) => {
            if (item.kind === "activity") return <ActivityChip key={item.id} card={item.card} />;
            const message = item.message;
            if (message.sender_type === "user") {
              return <UserBubble key={item.id} message={message} name={userName} />;
            }
            if (message.sender_type === "system") {
              return <SystemChip key={item.id} message={message} />;
            }
            const name = message.sender_name ?? agentName;
            const avatarUrl = message.agent_id ? (agentAvatars?.[message.agent_id] ?? null) : agentAvatarUrl;
            if (isWorkCard(message)) {
              return <WorkCard key={item.id} message={message} name={name} avatarUrl={avatarUrl} />;
            }
            return <AgentBubble key={item.id} message={message} name={name} avatarUrl={avatarUrl} />;
          })}

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

          {liveStatus ? <WorkingIndicator status={liveStatus} name={agentName} avatarUrl={agentAvatarUrl} /> : null}

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
