"use client";

/** Left rail: new chat, search, pinned, and recent conversations. */

import { Pin, Plus, Search, SquarePen } from "lucide-react";
import Link from "next/link";
import { useDeferredValue, useMemo, useState } from "react";
import { Avatar } from "@/components/avatar";
import { LiveStatusPill } from "@/components/chat/status-pill";
import { ErrorNote, Spinner } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { filterConversations, relativeTime, sortByActivity } from "@/lib/chat";
import { useAgentAvatarMap, useConversations } from "@/lib/hooks";
import type { Conversation } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

export function ConversationRailItem({
  conversation,
  selected,
  now,
  avatarUrl,
}: {
  conversation: Conversation;
  selected: boolean;
  now?: Date;
  /** The primary agent's `avatar_url`, when known. */
  avatarUrl?: string | null;
}) {
  const agentName = conversation.agent_name ?? "Agent";
  const preview = conversation.last_message_preview?.trim();
  return (
    <li>
      <Link
        href={`/chats/${conversation.id}`}
        aria-current={selected ? "page" : undefined}
        data-testid={`rail-item-${conversation.id}`}
        className={`flex min-h-[40px] gap-3 rounded-xl px-3 py-2.5 transition-colors ${
          selected ? "bg-accent-soft" : "hover:bg-hover"
        }`}
      >
        <Avatar name={agentName} size="sm" className="mt-0.5" src={avatarUrl} />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline justify-between gap-2">
            <span className="truncate text-sm font-medium text-ink">{conversation.title}</span>
            <span className="shrink-0 text-[11px] tabular-nums text-faint">
              {relativeTime(conversation.last_activity_at, now)}
            </span>
          </span>
          <span className="block truncate text-xs text-dim">
            {agentName}
            {conversation.agent_role_title ? ` · ${conversation.agent_role_title}` : ""}
          </span>
          {preview ? (
            <span className="mt-0.5 block truncate text-xs text-faint">{preview}</span>
          ) : null}
          <LiveStatusPill conversation={conversation} className="mt-1" />
        </span>
        {conversation.pinned ? (
          <Pin size={12} className="mt-1 shrink-0 text-faint" aria-label="Pinned" />
        ) : null}
      </Link>
    </li>
  );
}

export function ChatRail({ selectedId }: { selectedId: string | null }) {
  const { workspace } = useWorkspace();
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const avatars = useAgentAvatarMap(workspace.workspace_id);
  const conversations = useConversations(workspace.workspace_id, {
    q: deferredQuery.trim() || undefined,
    limit: 100,
  });

  const { pinned, recent } = useMemo(() => {
    const items = sortByActivity(filterConversations(conversations.data?.items ?? [], query));
    return {
      pinned: items.filter((item) => item.pinned),
      recent: items.filter((item) => !item.pinned),
    };
  }, [conversations.data, query]);

  const error =
    conversations.error instanceof ApiError
      ? `Couldn't load your chats (${conversations.error.detail}). Try refreshing.`
      : conversations.error
        ? "Couldn't load your chats. Check your connection and try again."
        : null;

  return (
    <nav aria-label="Chats" className="flex h-full min-h-0 flex-col">
      <div className="space-y-3 px-3 pt-4">
        <div className="flex items-center justify-between gap-2 px-1">
          <h1 className="font-display text-lg font-semibold text-ink">Chats</h1>
          <Link
            href="/chats"
            aria-label="New chat"
            title="New chat"
            className="inline-flex h-10 w-10 items-center justify-center rounded-xl text-accent-strong hover:bg-accent-soft"
          >
            <SquarePen size={18} />
          </Link>
        </div>
        <label className="relative block">
          <span className="sr-only">Search chats</span>
          <Search
            size={15}
            aria-hidden
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
          />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search chats"
            className="h-10 w-full rounded-xl border border-line bg-raised pl-9 pr-3 text-sm text-ink placeholder:text-faint outline-none focus:border-accent focus:ring-2 focus:ring-accent/25"
          />
        </label>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-4 pt-2">
        {conversations.isPending ? (
          <div className="px-3 py-6">
            <Spinner label="Loading chats…" />
          </div>
        ) : null}
        {error ? (
          <div className="px-2 py-3">
            <ErrorNote message={error} />
          </div>
        ) : null}

        {pinned.length > 0 ? (
          <section aria-label="Pinned" className="mb-2">
            <h2 className="px-3 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wider text-faint">
              Pinned
            </h2>
            <ul className="space-y-0.5">
              {pinned.map((conversation) => (
                <ConversationRailItem
                  key={conversation.id}
                  conversation={conversation}
                  selected={conversation.id === selectedId}
                  avatarUrl={conversation.primary_agent_id ? avatars[conversation.primary_agent_id] : null}
                />
              ))}
            </ul>
          </section>
        ) : null}

        {recent.length > 0 ? (
          <section aria-label="Recent">
            {pinned.length > 0 ? (
              <h2 className="px-3 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wider text-faint">
                Recent
              </h2>
            ) : null}
            <ul className="space-y-0.5">
              {recent.map((conversation) => (
                <ConversationRailItem
                  key={conversation.id}
                  conversation={conversation}
                  selected={conversation.id === selectedId}
                  avatarUrl={conversation.primary_agent_id ? avatars[conversation.primary_agent_id] : null}
                />
              ))}
            </ul>
          </section>
        ) : null}

        {conversations.data && pinned.length + recent.length === 0 ? (
          <div className="px-3 py-8 text-center text-sm text-dim">
            {query.trim() ? (
              <p>No chats match “{query.trim()}”.</p>
            ) : (
              <>
                <p>No chats yet.</p>
                <Link
                  href="/chats"
                  className="mt-2 inline-flex min-h-[40px] items-center gap-1 text-accent-strong hover:underline"
                >
                  <Plus size={14} /> Start your first chat
                </Link>
              </>
            )}
          </div>
        ) : null}
      </div>
    </nav>
  );
}
