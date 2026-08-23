"use client";

/** Attention inbox body: pending approvals, reviews waiting on a person,
 * help requests between agents, memories waiting for approval, failed
 * tasks, and chats waiting on the user. Pure props so the all-clear and
 * populated states are component-testable. */

import { AlertTriangle, CheckCircle2, ExternalLink, MessageSquare, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { Avatar } from "@/components/avatar";
import { Chip, SectionCard, StatusPill } from "@/components/company/bits";
import { Button, Dialog, Field, Textarea } from "@/components/ui";
import { timeAgo } from "@/lib/activity";
import { humanizeToolName, matchedConditionLabels, REVIEW_MODE_LABELS, reviewSubject } from "@/lib/coordination";
import { formatDateTime } from "@/lib/format";
import { avatarProps } from "@/lib/media";
import { kindLabel, scopeLabel } from "@/lib/memory";
import type { AgentAvatar, Attention, MemoryRecord, ReviewVerdict, WorkRequest, WorkReview } from "@/lib/types";

export type WorkRequestAction = "accept" | "decline" | "clarify";

function AttentionAllClear() {
  return (
    <div
      data-testid="attention-all-clear"
      className="flex flex-col items-center gap-2 rounded-2xl border border-line bg-surface px-8 py-16 text-center"
    >
      <CheckCircle2 className="text-ok" size={28} aria-hidden />
      <p className="font-display text-lg font-semibold tracking-tight">You’re all caught up</p>
      <p className="max-w-sm text-sm text-dim">
        Nothing needs your decision right now. We’ll list approvals, reviews, help requests, problems, and chats
        waiting on you here.
      </p>
    </div>
  );
}

/** Approve / request changes with feedback. */
function ReviewDecisionDialog({
  review,
  onClose,
  onSubmit,
  busy = false,
}: {
  review: WorkReview | null;
  onClose: () => void;
  onSubmit: (reviewId: string, verdict: ReviewVerdict, feedback: string) => void;
  busy?: boolean;
}) {
  const [feedback, setFeedback] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const open = review !== null;
  const submit = (verdict: ReviewVerdict) => {
    if (!review) return;
    const text = feedback.trim();
    if (verdict === "changes_requested" && !text) {
      setProblem("Tell the agent what to change so it can act on it.");
      return;
    }
    setProblem(null);
    onSubmit(review.id, verdict, text);
    setFeedback("");
  };
  return (
    <Dialog title="Review this work" description={review ? reviewSubject(review) : undefined} open={open} onClose={onClose}>
      {review ? (
        <div className="space-y-3">
          <Field label="Feedback" hint="Required when asking for changes; optional otherwise.">
            <Textarea
              value={feedback}
              rows={4}
              maxLength={4000}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="What looks good, or what should change?"
            />
          </Field>
          {problem ? (
            <p role="alert" className="text-sm text-danger">
              {problem}
            </p>
          ) : null}
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Not now
            </Button>
            <Button onClick={() => submit("changes_requested")} disabled={busy}>
              Request changes
            </Button>
            <Button variant="primary" onClick={() => submit("approve")} disabled={busy}>
              Looks good
            </Button>
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}

/** Decline with a reason or ask the requester to clarify. */
function WorkRequestReplyDialog({
  request,
  action,
  onClose,
  onSubmit,
  busy = false,
}: {
  request: WorkRequest | null;
  action: Exclude<WorkRequestAction, "accept"> | null;
  onClose: () => void;
  onSubmit: (requestId: string, action: WorkRequestAction, response: string) => void;
  busy?: boolean;
}) {
  const [text, setText] = useState("");
  const open = request !== null && action !== null;
  const clarify = action === "clarify";
  return (
    <Dialog
      title={clarify ? "Ask for clarification" : "Decline this request"}
      description={request ? `“${request.title}” from ${request.requester_agent_name ?? "an agent"}` : undefined}
      open={open}
      onClose={onClose}
    >
      {request && action ? (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (clarify && !text.trim()) return;
            onSubmit(request.id, action, text.trim());
            setText("");
          }}
        >
          <Field label={clarify ? "What do you need to know?" : "Reason (optional)"}>
            <Textarea value={text} rows={3} maxLength={4000} onChange={(event) => setText(event.target.value)} autoFocus />
          </Field>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" type="button" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button variant={clarify ? "primary" : "danger"} type="submit" disabled={busy || (clarify && !text.trim())}>
              {clarify ? "Send question" : "Decline"}
            </Button>
          </div>
        </form>
      ) : null}
    </Dialog>
  );
}

export function AttentionInbox({
  data,
  canDecide,
  isAdmin = false,
  decidingId,
  onDecide,
  onReviewDecide,
  onDismissFailure,
  onDismissAllFailures,
  workRequests = [],
  onWorkRequest,
  proposedMemories = [],
  onMemoryDecide,
  avatars,
  now,
}: {
  data: Attention;
  canDecide: boolean;
  isAdmin?: boolean;
  decidingId?: string | null;
  onDecide: (approvalId: string, decision: "approve" | "reject") => void;
  onReviewDecide?: (reviewId: string, verdict: ReviewVerdict, feedback: string) => void;
  /** Dismiss one failed task / every listed failure from the inbox. */
  onDismissFailure?: (taskId: string) => void;
  onDismissAllFailures?: () => void;
  /** Pending work requests (admins see accept/decline/clarify). */
  workRequests?: WorkRequest[];
  onWorkRequest?: (requestId: string, action: WorkRequestAction, response: string) => void;
  /** Proposed team/company memories waiting for an admin. */
  proposedMemories?: MemoryRecord[];
  onMemoryDecide?: (memoryId: string, decision: "approve" | "reject") => void;
  avatars?: Record<string, AgentAvatar | null>;
  now?: number;
}) {
  const { pending_approvals, failed_tasks, waiting_conversations } = data;
  const pending_reviews = data.pending_reviews ?? [];
  const reviews_in_progress = data.reviews_in_progress ?? [];
  const budget = data.budget ?? null;
  const [reviewing, setReviewing] = useState<WorkReview | null>(null);
  const [replying, setReplying] = useState<{ request: WorkRequest; action: "decline" | "clarify" } | null>(null);
  const empty =
    pending_approvals.length === 0 &&
    pending_reviews.length === 0 &&
    reviews_in_progress.length === 0 &&
    workRequests.length === 0 &&
    proposedMemories.length === 0 &&
    failed_tasks.length === 0 &&
    waiting_conversations.length === 0 &&
    budget === null;
  if (empty) return <AttentionAllClear />;

  return (
    <div className="space-y-5">
      {budget ? (
        <SectionCard
          title="Budget"
          description="Model spend is approaching this month's cap."
        >
          <p data-testid="budget-notice" className="text-sm text-ink">
            Model spend is at {budget.percent_used}% of this month&apos;s budget ($
            {(budget.spent_month_micros / 1_000_000).toFixed(2)} of $
            {(budget.monthly_budget_micros / 1_000_000).toFixed(2)}).{" "}
            <Link href="/settings" className="text-accent underline underline-offset-2">
              Adjust it in Settings
            </Link>
            .
          </p>
        </SectionCard>
      ) : null}
      {pending_approvals.length > 0 ? (
        <SectionCard
          title={`Waiting for your approval (${pending_approvals.length})`}
          description="An agent wants to do something that needs a human OK."
        >
          <ul className="space-y-3">
            {pending_approvals.map((approval) => (
              <ApprovalCard
                key={approval.id}
                approval={approval}
                canDecide={canDecide}
                deciding={decidingId === approval.id}
                onApprove={() => onDecide(approval.id, "approve")}
                onReject={() => onDecide(approval.id, "reject")}
              />
            ))}
          </ul>
        </SectionCard>
      ) : null}

      {pending_reviews.length > 0 ? (
        <SectionCard
          title={`Reviews waiting on you (${pending_reviews.length})`}
          description="A review policy asked for a person to check this work before it goes further."
        >
          <ul className="space-y-2">
            {pending_reviews.map((review) => {
              const reasons = matchedConditionLabels(review.evidence_json ?? {});
              return (
                <li key={review.id} data-testid={`review-${review.id}`} className="rounded-xl border border-warn/30 bg-warn-soft/60 px-4 py-3">
                  <div className="flex items-start gap-3">
                    <Avatar name={review.subject_agent_name ?? "Agent"} size="sm" {...avatarProps(review.subject_agent_id ? avatars?.[review.subject_agent_id] : null)} />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-ink">{reviewSubject(review)}</p>
                      <p className="mt-0.5 text-xs text-dim">
                        {REVIEW_MODE_LABELS[review.mode] ?? review.mode}
                        {reasons.length ? ` · ${reasons.join(", ")}` : ""}
                        {" · "}
                        <time dateTime={review.requested_at} title={formatDateTime(review.requested_at)}>
                          {timeAgo(review.requested_at, now)}
                        </time>
                      </p>
                      {review.tool_call_id ? (
                        <p className="mt-1 text-xs text-warn" data-testid={`review-parked-${review.id}`}>
                          {review.parked_tool_name
                            ? `The agent is waiting for this review before it can use ${humanizeToolName(review.parked_tool_name)}.`
                            : "The agent is waiting for this review before it continues."}
                        </p>
                      ) : review.mode === "pre_action" || review.mode === "before_close" ? (
                        <p className="mt-1 text-xs text-warn">The agent is paused until someone decides.</p>
                      ) : null}
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        {review.task_id ? (
                          <Chip href={`/tasks/${review.task_id}`}>
                            <ExternalLink size={12} className="mr-1" aria-hidden /> Open in Advanced
                          </Chip>
                        ) : null}
                      </div>
                    </div>
                    {canDecide && onReviewDecide ? (
                      <Button size="sm" variant="primary" onClick={() => setReviewing(review)} disabled={decidingId === review.id}>
                        Decide
                      </Button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        </SectionCard>
      ) : null}

      {reviews_in_progress.length > 0 ? (
        <SectionCard
          title={`Being reviewed by an agent (${reviews_in_progress.length})`}
          description={
            isAdmin
              ? "A colleague is reviewing this work. Nothing is needed from you unless you want to decide it yourself."
              : "A colleague is reviewing this work. An admin can step in and decide instead."
          }
        >
          <ul className="space-y-2">
            {reviews_in_progress.map((review) => {
              const reasons = matchedConditionLabels(review.evidence_json ?? {});
              return (
                <li key={review.id} data-testid={`review-in-progress-${review.id}`} className="rounded-xl border border-line bg-raised px-4 py-3">
                  <div className="flex items-start gap-3">
                    <Avatar
                      name={review.reviewer_agent_name ?? "Reviewer"}
                      size="sm"
                      {...avatarProps(review.reviewer_agent_id ? avatars?.[review.reviewer_agent_id] : null)}
                    />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-ink">
                        {review.reviewer_agent_name ?? "An agent"} is reviewing: {reviewSubject(review)}
                      </p>
                      <p className="mt-0.5 text-xs text-dim">
                        {REVIEW_MODE_LABELS[review.mode] ?? review.mode}
                        {reasons.length ? ` · ${reasons.join(", ")}` : ""}
                        {" · waiting "}
                        <time dateTime={review.requested_at} title={formatDateTime(review.requested_at)}>
                          {timeAgo(review.requested_at, now)}
                        </time>
                      </p>
                      {review.tool_call_id ? (
                        <p className="mt-1 text-xs text-dim" data-testid={`review-in-progress-parked-${review.id}`}>
                          {review.parked_tool_name
                            ? `${review.subject_agent_name ?? "The agent"} is paused until ${humanizeToolName(review.parked_tool_name)} is reviewed.`
                            : `${review.subject_agent_name ?? "The agent"} is paused until this is reviewed.`}
                        </p>
                      ) : null}
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        {review.reviewer_agent_id ? <Chip href={`/agents/${review.reviewer_agent_id}`}>{review.reviewer_agent_name ?? "Reviewer"}</Chip> : null}
                        {review.task_id ? (
                          <Chip href={`/tasks/${review.task_id}`}>
                            <ExternalLink size={12} className="mr-1" aria-hidden /> Open in Advanced
                          </Chip>
                        ) : null}
                      </div>
                    </div>
                    {isAdmin && onReviewDecide ? (
                      <Button size="sm" onClick={() => setReviewing(review)} disabled={decidingId === review.id}>
                        Decide instead
                      </Button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        </SectionCard>
      ) : null}

      {workRequests.length > 0 ? (
        <SectionCard
          title={`Help requests (${workRequests.length})`}
          description={isAdmin ? "One agent asked another for help. You can answer on the colleague's behalf." : "One agent asked another for help and is waiting for an answer."}
        >
          <ul className="space-y-2">
            {workRequests.map((request) => (
              <li key={request.id} data-testid={`work-request-${request.id}`} className="rounded-xl border border-line bg-raised px-4 py-3">
                <div className="flex items-start gap-3">
                  <Avatar name={request.requester_agent_name ?? "Agent"} size="sm" {...avatarProps(avatars?.[request.requester_agent_id])} />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-ink">
                      {request.requester_agent_name ?? "An agent"} asked {request.target_agent_name ?? "a colleague"}: {request.title}
                    </p>
                    {request.description ? <p className="mt-0.5 line-clamp-3 whitespace-pre-wrap text-[13px] text-dim">{request.description}</p> : null}
                    {request.expected_output ? <p className="mt-1 text-[13px] text-dim">Expected: {request.expected_output}</p> : null}
                    <p className="mt-1 text-xs text-faint">
                      <StatusPill status={{ label: request.status === "clarification_requested" ? "Clarification asked" : "Waiting for an answer", tone: "warn" }} />{" "}
                      · <time dateTime={request.created_at}>{timeAgo(request.created_at, now)}</time>
                    </p>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      {request.conversation_id ? (
                        <Chip href={`/chats/${request.conversation_id}`}>
                          <MessageSquare size={12} className="mr-1" aria-hidden /> Open chat
                        </Chip>
                      ) : null}
                      <Chip href={`/agents/${request.target_agent_id}`}>{request.target_agent_name ?? "Colleague"}</Chip>
                    </div>
                  </div>
                </div>
                {isAdmin && onWorkRequest ? (
                  <div className="mt-2 flex flex-wrap gap-1.5 pl-11">
                    <Button size="sm" variant="primary" disabled={decidingId === request.id} onClick={() => onWorkRequest(request.id, "accept", "")}>
                      Accept
                    </Button>
                    <Button size="sm" disabled={decidingId === request.id} onClick={() => setReplying({ request, action: "clarify" })}>
                      Ask for clarification
                    </Button>
                    <Button size="sm" variant="ghost" disabled={decidingId === request.id} onClick={() => setReplying({ request, action: "decline" })}>
                      Decline
                    </Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </SectionCard>
      ) : null}

      {proposedMemories.length > 0 ? (
        <SectionCard
          title={`Memories waiting for approval (${proposedMemories.length})`}
          description="An agent learned something it thinks the whole team or company should remember. Approve it to make it stick."
        >
          <ul className="space-y-2">
            {proposedMemories.map((memory) => (
              <li key={memory.id} data-testid={`proposed-memory-${memory.id}`} className="rounded-xl border border-line bg-raised px-4 py-3">
                <p className="whitespace-pre-wrap text-sm text-ink">{memory.content}</p>
                <p className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-dim">
                  <Chip>{scopeLabel(memory.scope)}</Chip>
                  <Chip>{kindLabel(memory.kind)}</Chip>
                  <time dateTime={memory.created_at}>{timeAgo(memory.created_at, now)}</time>
                  {memory.source_conversation_id ? (
                    <Link href={`/chats/${memory.source_conversation_id}`} className="text-accent-strong hover:underline">
                      From a chat
                    </Link>
                  ) : null}
                </p>
                {isAdmin && onMemoryDecide ? (
                  <div className="mt-2 flex gap-1.5">
                    <Button size="sm" variant="primary" disabled={decidingId === memory.id} onClick={() => onMemoryDecide(memory.id, "approve")}>
                      Approve
                    </Button>
                    <Button size="sm" disabled={decidingId === memory.id} onClick={() => onMemoryDecide(memory.id, "reject")}>
                      Reject
                    </Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </SectionCard>
      ) : null}

      {failed_tasks.length > 0 ? (
        <SectionCard
          title={`Ran into a problem (${failed_tasks.length})`}
          description="These pieces of work stopped. Open the chat to see what happened and ask the agent to try again, or dismiss the ones you have dealt with."
          action={
            canDecide && onDismissAllFailures ? (
              <Button size="sm" variant="ghost" onClick={onDismissAllFailures} disabled={decidingId === "failures:all"} data-testid="dismiss-all-failures">
                Dismiss all
              </Button>
            ) : undefined
          }
        >
          <ul className="space-y-2">
            {failed_tasks.map((task) => {
              const conversationId =
                typeof task.metadata_json?.conversation_id === "string"
                  ? task.metadata_json.conversation_id
                  : null;
              const href = conversationId ? `/chats/${conversationId}` : `/tasks/${task.id}`;
              return (
                <li
                  key={task.id}
                  data-testid={`failed-${task.id}`}
                  className="flex items-start gap-3 rounded-xl border border-danger/30 bg-danger-soft px-4 py-3"
                >
                  <AlertTriangle size={16} className="mt-0.5 shrink-0 text-danger" aria-hidden />
                  <Link href={href} className="min-w-0 flex-1 transition-colors hover:text-accent-strong">
                    <span className="block truncate text-sm font-medium">{task.title}</span>
                    <span className="block text-xs text-dim">
                      Stopped {timeAgo(task.updated_at, now)} · {conversationId ? "Open the chat" : "Open in Advanced"}
                    </span>
                  </Link>
                  {canDecide && onDismissFailure ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`Dismiss “${task.title}”`}
                      title="Dismiss from this list"
                      onClick={() => onDismissFailure(task.id)}
                      disabled={decidingId === task.id || decidingId === "failures:all"}
                    >
                      <X size={14} aria-hidden /> Dismiss
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </SectionCard>
      ) : null}

      {waiting_conversations.length > 0 ? (
        <SectionCard
          title={`Chats waiting on you (${waiting_conversations.length})`}
          description="The agent paused and is waiting for your answer."
        >
          <ul className="space-y-2">
            {waiting_conversations.map((conversation) => (
              <li key={conversation.id}>
                <Link
                  href={`/chats/${conversation.id}`}
                  className="flex items-center gap-3 rounded-xl border border-line bg-raised px-4 py-3 transition-colors hover:border-line-strong"
                >
                  <Avatar
                    name={conversation.agent_name ?? "Agent"}
                    size="sm"
                    {...avatarProps(conversation.primary_agent_id ? avatars?.[conversation.primary_agent_id] : null)}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{conversation.title}</span>
                    <span className="block truncate text-xs text-dim">
                      {conversation.agent_name ?? "Agent"}
                      {conversation.last_message_preview ? ` · ${conversation.last_message_preview}` : ""}
                    </span>
                  </span>
                  <time
                    dateTime={conversation.last_activity_at}
                    title={formatDateTime(conversation.last_activity_at)}
                    className="shrink-0 text-xs text-faint"
                  >
                    {timeAgo(conversation.last_activity_at, now)}
                  </time>
                  <MessageSquare size={15} className="shrink-0 text-dim" aria-hidden />
                </Link>
              </li>
            ))}
          </ul>
        </SectionCard>
      ) : null}

      <ReviewDecisionDialog
        review={reviewing}
        onClose={() => setReviewing(null)}
        busy={reviewing ? decidingId === reviewing.id : false}
        onSubmit={(id, verdict, feedback) => {
          setReviewing(null);
          onReviewDecide?.(id, verdict, feedback);
        }}
      />
      <WorkRequestReplyDialog
        request={replying?.request ?? null}
        action={replying?.action ?? null}
        onClose={() => setReplying(null)}
        busy={replying ? decidingId === replying.request.id : false}
        onSubmit={(id, action, response) => {
          setReplying(null);
          onWorkRequest?.(id, action, response);
        }}
      />
    </div>
  );
}
