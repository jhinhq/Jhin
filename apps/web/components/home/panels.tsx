"use client";

/** The Home screen's panels. Presentational: every one takes already-fetched
 * data plus its loading/error state, so the page owns the hooks and these
 * stay component-testable. */

import { AlertTriangle, CheckCircle2 } from "lucide-react";
import Link from "next/link";
import { Avatar } from "@/components/avatar";
import { ConversationRailItem } from "@/components/chat/chat-rail";
import { LoadError, SectionCard } from "@/components/company/bits";
import { Spinner } from "@/components/ui";
import { actorNameOf, friendlyHandoff, timeAgo } from "@/lib/activity";
import { avatarProps } from "@/lib/media";
import type {
  ActivityCard,
  AgentAvatar,
  Attention,
  Conversation,
  OrgAgentNode,
  Task,
} from "@/lib/types";

interface Async {
  isPending: boolean;
  isError: boolean;
  onRetry?: () => void;
}

/** A "3 waiting for you" tile. Zero-count items are never rendered. */
function NeedTile({
  href,
  count,
  label,
  tone,
}: {
  href: string;
  count: number;
  label: string;
  tone: "warn" | "danger" | "accent";
}) {
  const tones = {
    warn: "border-warn/30 bg-warn-soft text-warn",
    danger: "border-danger/30 bg-danger-soft text-danger",
    accent: "border-accent/30 bg-accent-soft text-accent-strong",
  } as const;
  return (
    <li>
      <Link
        href={href}
        data-testid={`need-${label.replace(/\s+/g, "-").toLowerCase()}`}
        className={`flex h-full flex-col justify-between gap-1 rounded-xl border px-4 py-3 transition-opacity hover:opacity-80 ${tones[tone]}`}
      >
        <span className="font-display text-2xl font-semibold tabular-nums">{count}</span>
        <span className="text-[13px] font-medium">{label}</span>
      </Link>
    </li>
  );
}

export function NeedsYouPanel({
  attention,
  isPending,
  isError,
  onRetry,
}: Async & { attention: Attention | undefined }) {
  const counts = attention?.counts;
  const items = attention
    ? [
        { key: "approvals", href: "/attention", count: counts?.approvals ?? 0, label: "waiting for your approval", tone: "warn" as const },
        { key: "reviews", href: "/attention", count: counts?.reviews ?? 0, label: "waiting for your review", tone: "warn" as const },
        { key: "failures", href: "/attention", count: counts?.failures ?? 0, label: "ran into a problem", tone: "danger" as const },
        {
          key: "chats",
          href: "/chats",
          count: attention.waiting_conversations.length,
          label: "chats waiting on you",
          tone: "accent" as const,
        },
      ].filter((item) => item.count > 0)
    : [];

  return (
    <SectionCard
      title="Needs you"
      description="Everything that stopped and is waiting for a person."
      action={
        <Link href="/attention" className="text-[13px] font-medium text-accent-strong hover:underline">
          Open inbox
        </Link>
      }
    >
      {isPending ? (
        <Spinner label="Checking what needs you…" />
      ) : isError || !attention ? (
        <LoadError what="what needs you" onRetry={onRetry} />
      ) : items.length === 0 ? (
        <p className="flex items-center gap-2 text-sm text-dim">
          <CheckCircle2 size={16} className="shrink-0 text-ok" aria-hidden />
          Nothing needs you right now.
        </p>
      ) : (
        <ul data-testid="needs-you-items" className="grid gap-2 sm:grid-cols-2">
          {items.map((item) => (
            <NeedTile key={item.key} href={item.href} count={item.count} label={item.label} tone={item.tone} />
          ))}
        </ul>
      )}
      {attention?.budget ? (
        <Link
          href="/models"
          className="mt-3 flex items-center gap-2 rounded-xl border border-warn/30 bg-warn-soft px-3.5 py-2.5 text-[13px] text-warn"
        >
          <AlertTriangle size={15} className="shrink-0" aria-hidden />
          This month&apos;s model spend is at {attention.budget.percent_used}% of the budget.
        </Link>
      ) : null}
    </SectionCard>
  );
}

export function RecentChatsPanel({
  conversations,
  avatars,
  isPending,
  isError,
  onRetry,
}: Async & {
  conversations: Conversation[];
  avatars: Record<string, AgentAvatar>;
}) {
  return (
    <SectionCard
      title="Your recent chats"
      description="Pick up where you left off."
      action={
        <Link href="/chats" className="text-[13px] font-medium text-accent-strong hover:underline">
          All chats
        </Link>
      }
    >
      {isPending ? (
        <Spinner label="Loading your chats…" />
      ) : isError ? (
        <LoadError what="your chats" onRetry={onRetry} />
      ) : conversations.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line-strong px-4 py-5 text-sm text-dim">
          No chats yet.{" "}
          <Link href="/chats" className="font-medium text-accent-strong hover:underline">
            Ask an agent for something
          </Link>{" "}
          and it shows up here.
        </p>
      ) : (
        <ul data-testid="recent-chats" className="-mx-2 space-y-0.5">
          {conversations.map((conversation) => (
            <ConversationRailItem
              key={conversation.id}
              conversation={conversation}
              selected={false}
              avatar={
                conversation.primary_agent_id ? avatars[conversation.primary_agent_id] ?? null : null
              }
            />
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

export function RightNowPanel({
  running,
  queued,
  activity,
  activityFailed = false,
  agentNameById,
  avatars,
  now,
  isPending,
  isError,
  onRetry,
}: Async & {
  running: Task[];
  queued: Task[];
  activity: ActivityCard[];
  /** The feed is a secondary signal: a failure there is a line, not a wipeout. */
  activityFailed?: boolean;
  agentNameById: (id: string | null) => string;
  avatars: Record<string, AgentAvatar>;
  /** Injectable clock so tests get stable relative times. */
  now?: number;
}) {
  return (
    <SectionCard
      title="What your company is doing"
      description="Work in progress right now, and the latest thing each agent did."
      action={
        <Link href="/activity" className="text-[13px] font-medium text-accent-strong hover:underline">
          All activity
        </Link>
      }
    >
      {isPending ? (
        <Spinner label="Checking what's running…" />
      ) : isError ? (
        <LoadError what="what your agents are doing" onRetry={onRetry} />
      ) : running.length === 0 && queued.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line-strong px-4 py-5 text-sm text-dim">
          Nothing is running right now.{" "}
          <Link href="/chats" className="font-medium text-accent-strong hover:underline">
            Ask an agent to get something done
          </Link>
          .
        </p>
      ) : (
        <ul data-testid="running-tasks" className="space-y-2">
          {running.map((task) => (
            <li key={task.id}>
              <Link
                href={`/tasks/${task.id}`}
                className="flex items-center gap-3 rounded-xl border border-line bg-raised px-3.5 py-2.5 transition-colors hover:border-line-strong"
              >
                <Avatar
                  name={agentNameById(task.assigned_agent_id)}
                  size="sm"
                  {...avatarProps(task.assigned_agent_id ? avatars[task.assigned_agent_id] : null)}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-ink">{task.title}</span>
                  <span className="block truncate text-xs text-dim">
                    {agentNameById(task.assigned_agent_id)} · started {timeAgo(task.created_at, now)}
                  </span>
                </span>
                <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-accent/30 bg-accent-soft px-2 py-0.5 text-[11px] font-medium text-accent-strong">
                  <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-current motion-safe:animate-pulse" />
                  Working
                </span>
              </Link>
            </li>
          ))}
          {queued.length > 0 ? (
            <li className="text-[13px] text-dim">
              {queued.length} more {queued.length === 1 ? "task is" : "tasks are"} queued and will start
              as agents free up.
            </li>
          ) : null}
        </ul>
      )}

      {activity.length === 0 && activityFailed ? (
        <p className="mt-4 border-t border-line pt-3 text-[13px] text-dim">
          Recent activity could not be loaded.{" "}
          <Link href="/activity" className="font-medium text-accent-strong hover:underline">
            Open Activity
          </Link>{" "}
          to try again.
        </p>
      ) : activity.length > 0 ? (
        <div className="mt-4 border-t border-line pt-3">
          <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-faint">
            Latest activity
          </h3>
          <ul data-testid="activity-strip" className="space-y-1.5">
            {activity.map((card) => {
              const href = card.conversation_id
                ? `/chats/${card.conversation_id}`
                : card.task_id
                  ? `/tasks/${card.task_id}`
                  : "/activity";
              return (
                <li key={card.id}>
                  <Link href={href} className="flex items-center gap-2 rounded-lg px-1 py-0.5 hover:bg-hover">
                    <Avatar
                      name={actorNameOf(card)}
                      size="xs"
                      kind={card.actor_type === "user" ? "user" : "agent"}
                      {...avatarProps(card.actor_agent_id ? avatars[card.actor_agent_id] : null)}
                    />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-dim">
                      {friendlyHandoff(card)}
                    </span>
                    <time
                      dateTime={card.created_at}
                      className="shrink-0 text-[11px] tabular-nums text-faint"
                    >
                      {timeAgo(card.created_at, now)}
                    </time>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </SectionCard>
  );
}

export function TeamGlancePanel({
  agents,
  teamCount,
  workingIds,
  avatars,
  isPending,
  isError,
  onRetry,
}: Async & {
  agents: OrgAgentNode[];
  teamCount: number;
  workingIds: Set<string>;
  avatars: Record<string, AgentAvatar>;
}) {
  const active = agents.filter((agent) => agent.status === "active");
  const working = active.filter((agent) => workingIds.has(agent.id));
  const availableCount = active.length - working.length;

  return (
    <SectionCard
      title="Your team"
      description="Who you have, and who is busy."
      action={
        <Link href="/company" className="text-[13px] font-medium text-accent-strong hover:underline">
          Company
        </Link>
      }
    >
      {isPending ? (
        <Spinner label="Loading your team…" />
      ) : isError ? (
        <LoadError what="your team" onRetry={onRetry} />
      ) : agents.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line-strong px-4 py-5 text-sm text-dim">
          No agents yet.{" "}
          <Link href="/agents/new" className="font-medium text-accent-strong hover:underline">
            Create your first one
          </Link>
          .
        </p>
      ) : (
        <div className="space-y-3">
          <dl data-testid="team-stats" className="grid grid-cols-3 gap-2 text-center">
            {[
              { label: agents.length === 1 ? "agent" : "agents", value: agents.length },
              { label: teamCount === 1 ? "team" : "teams", value: teamCount },
              { label: "working now", value: working.length },
            ].map((stat) => (
              <div key={stat.label} className="rounded-xl border border-line bg-raised px-2 py-3">
                <dd className="font-display text-xl font-semibold tabular-nums text-ink">{stat.value}</dd>
                <dt className="text-xs text-dim">{stat.label}</dt>
              </div>
            ))}
          </dl>
          <ul className="flex flex-wrap gap-2">
            {(working.length > 0 ? working : active).slice(0, 8).map((agent) => (
              <li key={agent.id}>
                <Link
                  href={`/agents/${agent.id}`}
                  className="flex items-center gap-2 rounded-full border border-line bg-raised py-1 pl-1 pr-3 text-xs text-dim hover:border-line-strong hover:text-ink"
                >
                  <Avatar name={agent.name} size="xs" {...avatarProps(avatars[agent.id])} />
                  <span className="max-w-[9rem] truncate">{agent.name}</span>
                </Link>
              </li>
            ))}
          </ul>
          <p className="text-xs text-faint">
            {working.length === 0
              ? `Everyone is free — ${availableCount} ${availableCount === 1 ? "agent is" : "agents are"} ready for work.`
              : `${availableCount} ${availableCount === 1 ? "agent is" : "agents are"} free right now.`}
          </p>
        </div>
      )}
    </SectionCard>
  );
}

export function SpendPanelFrame({
  isPending,
  isError,
  onRetry,
  children,
}: Async & { children: React.ReactNode }) {
  return (
    <SectionCard
      title="Spend"
      description="What your agents' thinking cost this month."
      action={
        <Link href="/models" className="text-[13px] font-medium text-accent-strong hover:underline">
          Models
        </Link>
      }
    >
      {isPending ? (
        <Spinner label="Loading spend…" />
      ) : isError ? (
        <LoadError what="this month's spend" onRetry={onRetry} />
      ) : (
        children
      )}
    </SectionCard>
  );
}
