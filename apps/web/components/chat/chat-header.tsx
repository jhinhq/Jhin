"use client";

/** Thread header: agent identity, editable title, pin/archive/details. */

import { Archive, ArchiveRestore, ArrowLeft, ListChecks, PanelRight, Pencil, Pin, PinOff } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Avatar } from "@/components/avatar";
import { LiveStatusPill } from "@/components/chat/status-pill";
import type { Conversation, ConversationAgentSummary } from "@/lib/types";

function IconButton({
  label,
  active = false,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string; active?: boolean }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={`inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors disabled:opacity-50 ${
        active ? "bg-accent-soft text-accent-strong" : "text-dim hover:bg-hover hover:text-ink"
      }`}
      {...props}
    >
      {props.children}
    </button>
  );
}

export function ChatHeader({
  conversation,
  agent,
  avatarUrl,
  canEdit,
  detailsOpen,
  onToggleDetails,
  detailed,
  onToggleDetailed,
  onRename,
  onTogglePin,
  onToggleArchive,
  busy = false,
}: {
  conversation: Conversation;
  agent: ConversationAgentSummary | null;
  avatarUrl?: string | null;
  canEdit: boolean;
  detailsOpen: boolean;
  onToggleDetails: () => void;
  /** Show routine progress chips (Started working / Finished) in the transcript. */
  detailed: boolean;
  onToggleDetailed: () => void;
  onRename: (title: string) => void;
  onTogglePin: () => void;
  onToggleArchive: () => void;
  busy?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(conversation.title);
  const inputRef = useRef<HTMLInputElement>(null);
  const titleButtonRef = useRef<HTMLButtonElement>(null);
  const agentName = agent?.name ?? conversation.agent_name ?? "Agent";
  const role = agent?.role_title ?? conversation.agent_role_title ?? "";
  const archived = conversation.status === "archived";

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const startEditing = () => {
    setDraft(conversation.title);
    setEditing(true);
  };

  const finish = (save: boolean) => {
    setEditing(false);
    const next = draft.trim();
    if (save && next && next !== conversation.title) onRename(next.slice(0, 200));
    requestAnimationFrame(() => titleButtonRef.current?.focus());
  };

  return (
    <header className="flex items-center gap-3 border-b border-line bg-surface/80 px-3 py-2.5 backdrop-blur sm:px-5">
      <Link
        href="/chats"
        aria-label="Back to chats"
        className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-dim hover:bg-hover hover:text-ink lg:hidden"
      >
        <ArrowLeft size={18} />
      </Link>
      <Avatar name={agentName} size="md" src={avatarUrl} />
      <div className="min-w-0 flex-1">
        {editing ? (
          <input
            ref={inputRef}
            aria-label="Chat title"
            value={draft}
            maxLength={200}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => finish(true)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                finish(true);
              } else if (event.key === "Escape") {
                event.preventDefault();
                finish(false);
              }
            }}
            className="h-9 w-full rounded-lg border border-accent bg-raised px-2 font-display text-base font-semibold text-ink outline-none ring-2 ring-accent/25"
          />
        ) : (
          <button
            ref={titleButtonRef}
            type="button"
            onClick={canEdit ? startEditing : undefined}
            disabled={!canEdit}
            aria-label={canEdit ? `Rename chat: ${conversation.title}` : conversation.title}
            title={canEdit ? "Click to rename" : undefined}
            className="group flex min-h-[40px] max-w-full items-center gap-1.5 rounded-lg text-left font-display text-base font-semibold text-ink disabled:cursor-default"
          >
            <span className="truncate">{conversation.title}</span>
            {canEdit ? (
              <Pencil
                size={13}
                aria-hidden
                className="shrink-0 text-faint opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
              />
            ) : null}
          </button>
        )}
        <p className="flex min-w-0 items-center gap-2 text-xs text-dim">
          <span className="truncate">
            {agentName}
            {role ? ` · ${role}` : ""}
          </span>
          {archived ? <span className="shrink-0 text-faint">· Archived</span> : null}
          <LiveStatusPill conversation={conversation} />
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-0.5">
        {canEdit ? (
          <>
            <IconButton
              label={conversation.pinned ? "Unpin chat" : "Pin chat"}
              active={conversation.pinned}
              disabled={busy}
              onClick={onTogglePin}
            >
              {conversation.pinned ? <PinOff size={17} /> : <Pin size={17} />}
            </IconButton>
            <IconButton
              label={archived ? "Restore chat" : "Archive chat"}
              disabled={busy}
              onClick={onToggleArchive}
            >
              {archived ? <ArchiveRestore size={17} /> : <Archive size={17} />}
            </IconButton>
          </>
        ) : null}
        <IconButton
          label={detailed ? "Hide progress updates" : "Show progress updates"}
          active={detailed}
          aria-pressed={detailed}
          onClick={onToggleDetailed}
        >
          <ListChecks size={16} aria-hidden />
        </IconButton>
        <IconButton
          label={detailsOpen ? "Hide details" : "Show details"}
          active={detailsOpen}
          aria-expanded={detailsOpen}
          onClick={onToggleDetails}
        >
          <PanelRight size={17} />
        </IconButton>
      </div>
    </header>
  );
}
