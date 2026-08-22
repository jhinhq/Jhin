"use client";

/** Attention inbox body: pending approvals, failed tasks, and chats waiting
 * on the user. Pure props so the all-clear and populated states are
 * component-testable. */

import { AlertTriangle, CheckCircle2, MessageSquare } from "lucide-react";
import Link from "next/link";
import { ApprovalCard } from "@/components/approval-card";
import { Avatar } from "@/components/avatar";
import { SectionCard } from "@/components/company/bits";
import { timeAgo } from "@/lib/activity";
import { formatDateTime } from "@/lib/format";
import type { Attention } from "@/lib/types";

export function AttentionAllClear() {
  return (
    <div
      data-testid="attention-all-clear"
      className="flex flex-col items-center gap-2 rounded-2xl border border-line bg-surface px-8 py-16 text-center"
    >
      <CheckCircle2 className="text-ok" size={28} aria-hidden />
      <p className="font-display text-lg font-semibold tracking-tight">You’re all caught up</p>
      <p className="max-w-sm text-sm text-dim">
        Nothing needs your decision right now. We’ll list approvals, problems, and chats waiting on you here.
      </p>
    </div>
  );
}

export function AttentionInbox({
  data,
  canDecide,
  decidingId,
  onDecide,
  now,
}: {
  data: Attention;
  canDecide: boolean;
  decidingId?: string | null;
  onDecide: (approvalId: string, decision: "approve" | "reject") => void;
  now?: number;
}) {
  const { pending_approvals, failed_tasks, waiting_conversations } = data;
  const empty =
    pending_approvals.length === 0 && failed_tasks.length === 0 && waiting_conversations.length === 0;
  if (empty) return <AttentionAllClear />;

  return (
    <div className="space-y-5">
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

      {failed_tasks.length > 0 ? (
        <SectionCard
          title={`Ran into a problem (${failed_tasks.length})`}
          description="These pieces of work stopped. Open the chat to see what happened and ask the agent to try again."
        >
          <ul className="space-y-2">
            {failed_tasks.map((task) => {
              const conversationId =
                typeof task.metadata_json?.conversation_id === "string"
                  ? task.metadata_json.conversation_id
                  : null;
              const href = conversationId ? `/chats/${conversationId}` : `/tasks/${task.id}`;
              return (
                <li key={task.id} data-testid={`failed-${task.id}`}>
                  <Link
                    href={href}
                    className="flex items-start gap-3 rounded-xl border border-danger/30 bg-danger-soft px-4 py-3 transition-colors hover:border-danger/60"
                  >
                    <AlertTriangle size={16} className="mt-0.5 shrink-0 text-danger" aria-hidden />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{task.title}</span>
                      <span className="block text-xs text-dim">
                        Stopped {timeAgo(task.updated_at, now)} · {conversationId ? "Open the chat" : "Open in Advanced"}
                      </span>
                    </span>
                  </Link>
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
                  <Avatar name={conversation.agent_name ?? "Agent"} size="sm" />
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
    </div>
  );
}
