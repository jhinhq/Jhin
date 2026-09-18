"use client";

/**
 * The receipt an agent leaves when it changed its own name.
 *
 * The bug this closes was an agent saying "you can call me Bisby for this
 * chat" while nothing at all changed. Now that the name really is the agent
 * row, the opposite risk appears: a change to how an agent is identified
 * everywhere, made mid-conversation, that a person could scroll past. So a
 * rename is a card and not a sentence — what it was called, what it is called
 * now, and the handle that did *not* move — with a link to the place a human
 * can put it back.
 *
 * Renders `AgentRenamedContent` (see lib/types.ts). Only a rename that
 * actually wrote the row gets one; a refused call writes no message at all.
 */

import { ExternalLink, IdCard } from "lucide-react";
import Link from "next/link";
import { Avatar } from "@/components/avatar";
import { Timestamp } from "@/components/chat/timestamp";
import { readAgentRenamed } from "@/lib/chat";
import { avatarProps } from "@/lib/media";
import type { AgentAvatar, ConversationMessage } from "@/lib/types";

export function RenameCard({
  message,
  name,
  avatar,
}: {
  message: ConversationMessage;
  /** The agent that renamed itself — the sender, not always the primary agent. */
  name: string;
  avatar?: AgentAvatar | null;
}) {
  const renamed = readAgentRenamed(message);
  if (renamed === null) return null;

  const href = message.agent_id ? `/agents/${message.agent_id}` : null;

  return (
    <div data-testid="rename-card" className="flex items-start gap-2.5">
      <Avatar name={name} size="sm" {...avatarProps(avatar)} />
      <div className="min-w-0 max-w-[min(85%,40rem)] flex-1 rounded-2xl border border-line bg-raised px-4 py-3">
        <div className="flex items-start justify-between gap-2">
          <p className="flex min-w-0 items-baseline gap-1.5 text-sm text-ink">
            <IdCard size={14} aria-hidden className="shrink-0 translate-y-0.5 text-accent-strong" />
            <span className="min-w-0 break-words">
              <span className="font-medium">Now called {renamed.name}</span>
              {renamed.previous_name ? (
                <span className="text-dim"> — was {renamed.previous_name}</span>
              ) : null}
            </span>
          </p>
          <Timestamp iso={message.created_at} className="shrink-0" />
        </div>

        {/* Said plainly, because it is the question a rename raises: nothing
         * that points at this agent has moved. */}
        {renamed.slug ? (
          <p data-testid="rename-slug" className="mt-1.5 text-[13px] leading-relaxed text-dim">
            Its handle <span className="font-mono text-ink">{renamed.slug}</span> is unchanged, so
            existing links still work.
          </p>
        ) : null}

        {href ? (
          <div className="mt-1 flex flex-wrap items-center gap-x-4">
            <Link
              href={href}
              className="inline-flex min-h-[40px] items-center gap-1 text-[13px] font-medium text-accent-strong hover:underline"
            >
              Review or change this <ExternalLink size={13} aria-hidden />
            </Link>
          </div>
        ) : null}
      </div>
    </div>
  );
}
