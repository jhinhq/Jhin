"use client";

/** "Team status" tab for managers: a friendly rendering of the manager
 * rollup (reports, what's active, what's stuck, what's waiting on a
 * decision). `TeamStatusView` is pure props for tests. */

import { ExternalLink, MessageSquare } from "lucide-react";
import Link from "next/link";
import { Avatar } from "@/components/avatar";
import { Chip, LoadError, SectionCard, StatTile, StatusPill } from "@/components/company/bits";
import { Spinner } from "@/components/ui";
import { timeAgo } from "@/lib/activity";
import { rollupItemTitle, rollupStatusText } from "@/lib/coordination";
import { useAgentRollup } from "@/lib/hooks";
import type { ManagerRollup, RollupItem } from "@/lib/types";

function ItemRow({
  item,
  now,
  avatars,
  showAdvanced,
}: {
  item: RollupItem;
  now?: number;
  avatars?: Record<string, string | null>;
  showAdvanced: boolean;
}) {
  const status = rollupStatusText(item.status);
  return (
    <li className="flex items-start gap-3 rounded-xl border border-line bg-raised px-3 py-2.5">
      <Avatar name={item.agent_name ?? "Agent"} size="sm" src={item.agent_id ? avatars?.[item.agent_id] : null} />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-ink">{rollupItemTitle(item)}</p>
        {item.summary && item.summary !== item.title ? (
          <p className="mt-0.5 line-clamp-2 text-[13px] text-dim">{item.summary}</p>
        ) : null}
        {item.risks.length > 0 ? <p className="mt-0.5 text-[13px] text-warn">Risks: {item.risks.join("; ")}</p> : null}
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-faint">
          <StatusPill status={status} />
          <time dateTime={item.occurred_at}>{timeAgo(item.occurred_at, now)}</time>
          {item.conversation_id ? (
            <Chip href={`/chats/${item.conversation_id}`}>
              <MessageSquare size={12} className="mr-1" aria-hidden /> Open chat
            </Chip>
          ) : null}
          {showAdvanced && item.task_id ? (
            <Chip href={`/tasks/${item.task_id}`}>
              <ExternalLink size={12} className="mr-1" aria-hidden /> Open in Advanced
            </Chip>
          ) : null}
        </div>
      </div>
    </li>
  );
}

function ItemSection({
  title,
  description,
  items,
  empty,
  now,
  avatars,
  showAdvanced,
}: {
  title: string;
  description?: string;
  items: RollupItem[];
  empty: string;
  now?: number;
  avatars?: Record<string, string | null>;
  showAdvanced: boolean;
}) {
  return (
    <SectionCard title={items.length ? `${title} (${items.length})` : title} description={description}>
      {items.length === 0 ? (
        <p className="text-sm text-dim">{empty}</p>
      ) : (
        <ul className="space-y-2">
          {items.map((item) => (
            <ItemRow key={`${item.kind}:${item.source_id}`} item={item} now={now} avatars={avatars} showAdvanced={showAdvanced} />
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

export function TeamStatusView({
  rollup,
  now,
  avatars,
  showAdvanced = true,
}: {
  rollup: ManagerRollup;
  now?: number;
  avatars?: Record<string, string | null>;
  showAdvanced?: boolean;
}) {
  const { queue } = rollup;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="Working now" value={queue.active_runs} hint="runs in progress" />
        <StatTile label="Queued" value={queue.queued_tasks} hint="waiting for a free slot" />
        <StatTile label="Waiting on a person" value={queue.waiting_approval} hint="approvals" />
        <StatTile label="Open help requests" value={queue.open_work_requests} />
      </div>

      <SectionCard title={`Reports (${rollup.reports.length})`} description="Who reports here, directly or through someone else.">
        {rollup.reports.length === 0 ? (
          <p className="text-sm text-dim">No one reports to this agent yet.</p>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2">
            {rollup.reports.map((report) => (
              <li key={report.agent_id}>
                <Link
                  href={`/agents/${report.agent_id}`}
                  className="flex items-center gap-3 rounded-xl border border-line bg-raised px-3 py-2.5 transition-colors hover:border-line-strong"
                >
                  <Avatar name={report.name} size="sm" src={avatars?.[report.agent_id]} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{report.name}</span>
                    <span className="block truncate text-xs text-dim">
                      {report.role_title || "Agent"}
                      {report.depth > 1 ? " · indirect" : ""}
                    </span>
                  </span>
                  <span className="shrink-0 text-right text-xs text-dim">
                    {report.active_tasks > 0 ? `${report.active_tasks} active` : "idle"}
                    {report.queued_tasks > 0 ? `, ${report.queued_tasks} queued` : ""}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </SectionCard>

      <ItemSection title="Blocked or failed" items={rollup.blocked_or_failed} empty="Nothing is stuck." now={now} avatars={avatars} showAdvanced={showAdvanced} />
      <ItemSection title="Waiting for a review" items={rollup.pending_reviews} empty="No reviews waiting." now={now} avatars={avatars} showAdvanced={showAdvanced} />
      <ItemSection title="Waiting for an OK" items={rollup.pending_approvals} empty="No approvals waiting." now={now} avatars={avatars} showAdvanced={showAdvanced} />
      <ItemSection title="Working on now" items={rollup.active_work} empty="Nobody is working on anything right now." now={now} avatars={avatars} showAdvanced={showAdvanced} />
      <ItemSection title="Help requests" items={rollup.open_work_requests} empty="No open help requests." now={now} avatars={avatars} showAdvanced={showAdvanced} />
      <ItemSection title="Recent outcomes" description="The last week." items={rollup.outcomes} empty="Nothing finished in the last week." now={now} avatars={avatars} showAdvanced={showAdvanced} />
      {rollup.truncated ? <p className="text-xs text-faint">This is a trimmed view; the team is busy enough that not everything fits.</p> : null}
      <p className="text-xs text-faint">Updated {timeAgo(rollup.generated_at, now)}. Shows status only — never chats, instructions, or memory.</p>
    </div>
  );
}

export function TeamStatus({
  workspaceId,
  agentId,
  avatars,
  showAdvanced,
}: {
  workspaceId: string;
  agentId: string;
  avatars?: Record<string, string | null>;
  showAdvanced: boolean;
}) {
  const rollup = useAgentRollup(workspaceId, agentId);
  if (rollup.isPending) return <Spinner label="Checking in on the team…" />;
  if (rollup.isError || !rollup.data) return <LoadError what="the team status" onRetry={() => void rollup.refetch()} />;
  return <TeamStatusView rollup={rollup.data} avatars={avatars} showAdvanced={showAdvanced} />;
}
