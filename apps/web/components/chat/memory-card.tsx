"use client";

/**
 * The receipt an agent leaves when it actually wrote something to memory.
 *
 * An agent saying "I recorded that company-wide" is prose: it reads the same
 * whether the write landed, was refused, or landed at a narrower scope than
 * the person believed. This card is the write itself — the stored words, who
 * they are stored for, and what they replaced — so the claim can be checked
 * instead of trusted. It is deliberately quiet: a receipt, not a celebration.
 * The one moment a person is likely to catch a wrong memory is when it
 * appears, so everything they would need to catch it is on the card, and the
 * link takes them to where they can change it.
 *
 * Renders `MemorySavedContent` (see lib/types.ts). Only a stored record gets
 * one of these; a refused proposal writes no message at all.
 */

import { Brain, ExternalLink } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Avatar } from "@/components/avatar";
import { Timestamp } from "@/components/chat/timestamp";
import { readMemorySaved } from "@/lib/chat";
import { avatarProps } from "@/lib/media";
import type { AgentAvatar, ConversationMessage } from "@/lib/types";

/** Memories can run to 2,000 characters. Past this the card folds them, so a
 * long one cannot push the rest of the transcript off the screen — but the
 * fold is a button, never a truncation the reader can't undo. */
const CLAMP_CHARS = 320;

function clamp(text: string, expanded: boolean): string {
  if (expanded || text.length <= CLAMP_CHARS) return text;
  // Drop trailing space and punctuation before the ellipsis: a cut landing
  // after a full stop would otherwise read "first.…", four dots that look
  // like part of the remembered words rather than like a fold.
  return `${text.slice(0, CLAMP_CHARS - 1).replace(/[\s.,;:]+$/, "")}…`;
}

export function MemoryCard({
  message,
  name,
  avatar,
}: {
  message: ConversationMessage;
  /** The agent that wrote it — the sender, not always the primary agent. */
  name: string;
  avatar?: AgentAvatar | null;
}) {
  const [expanded, setExpanded] = useState(false);

  const memory = readMemorySaved(message);
  if (memory === null) return null;
  // A receipt with no words is not evidence of anything. Showing "Remembered"
  // over an empty card would be the same unbacked assertion the card exists to
  // replace, so render nothing and let the agent's own message stand.
  if (!memory.content.trim()) return null;

  const updated = memory.action === "updated";
  const foldable = memory.content.length > CLAMP_CHARS ||
    memory.superseded.length > CLAMP_CHARS ||
    memory.still_standing.length > CLAMP_CHARS;
  // Deep link into the agent's Memory tab at the scope this record lives in,
  // so "change it" is one click rather than a hunt. Without a sender we have
  // no page to point at, and a link that lands somewhere else is another
  // small lie — so it simply isn't rendered.
  const href = message.agent_id
    ? `/agents/${message.agent_id}?tab=memory${memory.scope ? `&memory_scope=${memory.scope}` : ""}`
    : null;

  return (
    <div
      data-testid="memory-card"
      data-action={memory.action}
      data-scope={memory.scope ?? ""}
      className="flex items-start gap-2.5"
    >
      <Avatar name={name} size="sm" {...avatarProps(avatar)} />
      <div className="min-w-0 max-w-[min(85%,40rem)] flex-1 rounded-2xl border border-line bg-raised px-4 py-3">
        {/* No wrapping onto a second row: the audience is long enough on a
         * phone that a wrapped timestamp would strand itself under it. The
         * heading takes the column and wraps inside it — which people a memory
         * is shared with is the part most worth double-checking, and half of
         * it is worse than none. */}
        <div className="flex items-start justify-between gap-2">
          <p className="flex min-w-0 items-baseline gap-1.5 text-sm text-ink">
            <Brain size={14} aria-hidden className="shrink-0 translate-y-0.5 text-accent-strong" />
            <span className="min-w-0 break-words">
              <span className="font-medium">{updated ? "Memory updated" : "Remembered"}</span>
              <span className="text-dim"> for {memory.scope_label}</span>
            </span>
          </p>
          <Timestamp iso={message.created_at} className="shrink-0" />
        </div>

        {/* Verbatim, like every other card that shows stored field values: this
         * is what the memory says, character for character, and formatting it
         * would show something other than what was saved. */}
        <p
          data-testid="memory-content"
          className="mt-1.5 whitespace-pre-wrap break-words text-[15px] leading-relaxed text-ink"
        >
          {clamp(memory.content, expanded)}
        </p>

        {memory.superseded ? (
          <p
            data-testid="memory-superseded"
            className="mt-1.5 whitespace-pre-wrap break-words text-[13px] leading-relaxed text-dim"
          >
            <span className="text-faint">Replaced: </span>
            {clamp(memory.superseded, expanded)}
          </p>
        ) : null}

        {memory.still_standing ? (
          // A correction that did not land as one: both are live, so the agent
          // will recall both. Said here because this is the moment the person
          // is looking, and the only moment they can cheaply put it right.
          <p
            data-testid="memory-still-standing"
            className="mt-2 rounded-lg border border-warn/30 bg-warn/10 px-2.5 py-1.5 text-[13px] leading-relaxed text-ink"
          >
            <span className="font-medium text-warn">Still remembered too: </span>
            {clamp(memory.still_standing, expanded)}
          </p>
        ) : null}

        <div className="mt-1 flex flex-wrap items-center gap-x-4">
          {foldable ? (
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpanded((value) => !value)}
              className="inline-flex min-h-[40px] items-center text-[13px] font-medium text-accent-strong hover:underline"
            >
              {expanded ? "Show less" : "Show all of it"}
            </button>
          ) : null}
          {href ? (
            <Link
              href={href}
              className="inline-flex min-h-[40px] items-center gap-1 text-[13px] font-medium text-accent-strong hover:underline"
            >
              Review or change this <ExternalLink size={13} aria-hidden />
            </Link>
          ) : null}
        </div>
      </div>
    </div>
  );
}
