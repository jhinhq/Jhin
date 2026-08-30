"use client";

/** One conversational card in the company activity feed. Pure props. */

import { ArrowRight, ExternalLink, MessageSquare } from "lucide-react";
import Link from "next/link";
import { Avatar } from "@/components/avatar";
import { Chip, Disclosure } from "@/components/company/bits";
import { Badge } from "@/components/ui";
import { actorNameOf, detailText, friendlyDetailLines, timeAgo, toneForKind } from "@/lib/activity";
import { formatDateTime } from "@/lib/format";
import { avatarProps } from "@/lib/media";
import type { ActivityCard as ActivityCardData, AgentAvatar } from "@/lib/types";

export function ActivityCard({
  card,
  now,
  avatars,
}: {
  card: ActivityCardData;
  /** Injectable clock for deterministic tests. */
  now?: number;
  /** Agent id → avatar visuals, when the caller has the agent list. */
  avatars?: Record<string, AgentAvatar | null>;
}) {
  const actor = actorNameOf(card);
  const details = detailText(card.detail_json);
  const friendly = friendlyDetailLines(card);
  return (
    <li data-testid={`activity-${card.id}`} className="flex gap-3 rounded-2xl border border-line bg-surface p-4">
      <Avatar
        name={actor}
        size="sm"
        kind={card.actor_type === "user" ? "user" : "agent"}
        {...avatarProps(card.actor_agent_id ? avatars?.[card.actor_agent_id] : null)}
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-sm font-medium">
            {card.actor_agent_id ? (
              <Link href={`/agents/${card.actor_agent_id}`} className="hover:underline">
                {actor}
              </Link>
            ) : (
              actor
            )}
          </span>
          <Badge tone={toneForKind(card.kind)}>{card.label}</Badge>
          {card.target_agent_name ? (
            <span className="inline-flex items-center gap-1 text-[13px] text-dim">
              <ArrowRight size={13} aria-hidden />
              <span className="sr-only">to</span>
              {card.target_agent_id ? (
                <Link href={`/agents/${card.target_agent_id}`} className="hover:underline">
                  {card.target_agent_name}
                </Link>
              ) : (
                card.target_agent_name
              )}
            </span>
          ) : null}
          <time
            dateTime={card.created_at}
            title={formatDateTime(card.created_at)}
            className="ml-auto text-xs text-faint"
          >
            {timeAgo(card.created_at, now)}
          </time>
        </div>
        {card.summary ? <p className="mt-1.5 whitespace-pre-wrap text-sm text-ink/90">{card.summary}</p> : null}
        {card.task_title && !card.summary ? <p className="mt-1.5 text-sm text-dim">{card.task_title}</p> : null}
        {friendly.length > 0 ? (
          <ul data-testid="activity-friendly-detail" className="mt-1.5 space-y-0.5 text-[13px] text-dim">
            {friendly.map((line) => (
              <li key={line} className="whitespace-pre-wrap">{line}</li>
            ))}
          </ul>
        ) : null}
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          {card.conversation_id ? (
            <Chip href={`/chats/${card.conversation_id}`}>
              <MessageSquare size={12} className="mr-1" aria-hidden /> Open chat
            </Chip>
          ) : null}
          {card.task_id ? (
            <Chip href={`/tasks/${card.task_id}`}>
              <ExternalLink size={12} className="mr-1" aria-hidden /> Open in Advanced
            </Chip>
          ) : null}
          {details ? (
            <div className="basis-full pt-1">
              <Disclosure label="Show details" openLabel="Hide details">
                <pre className="max-h-80 overflow-auto rounded-xl border border-line bg-raised px-3 py-2 font-mono text-[12px] leading-relaxed text-dim">
                  {details}
                </pre>
              </Disclosure>
            </div>
          ) : null}
        </div>
      </div>
    </li>
  );
}
