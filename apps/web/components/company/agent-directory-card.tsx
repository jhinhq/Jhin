"use client";

/** One card in the /agents directory. Pure props so it renders in vitest
 * without providers: links only, no router or data hooks. */

import { MessageSquare, UserRound } from "lucide-react";
import Link from "next/link";
import { Avatar } from "@/components/avatar";
import {
  expertiseOf,
  purposeOf,
  statusTextOf,
  type AgentLike,
} from "@/components/company/agent-helpers";
import { Chip, StatusPill } from "@/components/company/bits";

export function chatHref(agentId: string): string {
  return `/chats?agent=${encodeURIComponent(agentId)}`;
}

export function AgentDirectoryCard({
  agent,
  teamName,
  working = false,
}: {
  agent: AgentLike;
  teamName?: string;
  working?: boolean;
}) {
  const status = statusTextOf(agent, working);
  const purpose = purposeOf(agent);
  const expertise = expertiseOf(agent);
  return (
    <article
      data-testid={`agent-card-${agent.id}`}
      className="flex flex-col gap-3 rounded-2xl border border-line bg-surface p-5 transition-colors hover:border-line-strong"
    >
      <div className="flex items-start gap-3">
        <Avatar name={agent.name} size="lg" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-display text-base font-semibold tracking-tight">
            <Link href={`/agents/${agent.id}`} className="hover:underline">
              {agent.name}
            </Link>
          </h3>
          <p className="truncate text-[13px] text-dim">{agent.role_title || "Agent"}</p>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
            <StatusPill status={status} />
            {teamName ? <span className="text-xs text-faint">{teamName}</span> : null}
          </div>
        </div>
      </div>
      {purpose ? <p className="line-clamp-3 text-sm text-ink/90">{purpose}</p> : null}
      {expertise.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5" aria-label="Expertise">
          {expertise.slice(0, 6).map((tag) => (
            <li key={tag}>
              <Chip>{tag}</Chip>
            </li>
          ))}
          {expertise.length > 6 ? (
            <li>
              <Chip>+{expertise.length - 6} more</Chip>
            </li>
          ) : null}
        </ul>
      ) : null}
      <div className="mt-auto flex gap-2 pt-1">
        <Link
          href={chatHref(agent.id)}
          className="inline-flex h-10 flex-1 items-center justify-center gap-1.5 rounded-xl bg-accent px-3 text-sm font-semibold text-white transition-transform hover:-translate-y-px"
          style={{ background: "linear-gradient(120deg, var(--accent), var(--accent-2, var(--accent)))" }}
        >
          <MessageSquare size={15} /> Chat
        </Link>
        <Link
          href={`/agents/${agent.id}`}
          className="inline-flex h-10 items-center justify-center gap-1.5 rounded-xl border border-line-strong px-3 text-sm text-ink transition-colors hover:bg-hover"
        >
          <UserRound size={15} /> Profile
        </Link>
      </div>
    </article>
  );
}
