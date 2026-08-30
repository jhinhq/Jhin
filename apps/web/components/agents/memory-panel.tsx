"use client";

/** Agent profile "Memory" tab: what the agent remembers, at which scope, and
 * the human controls over it (pin, edit, contest, forget, approve/reject,
 * remember something). `MemoryItemCard` is pure props for tests. */

import { useMutation } from "@tanstack/react-query";
import { Merge, MessageSquare, Plus, Star } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Chip, LoadError, SectionCard, Segmented, StatusPill } from "@/components/company/bits";
import { Badge, Button, Dialog, EmptyState, ErrorNote, Field, Select, Spinner, Textarea } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { timeAgo } from "@/lib/activity";
import { useInvalidateMemories, useMemories } from "@/lib/hooks";
import {
  confidenceWord,
  filterByStatus,
  importanceWord,
  KIND_LABELS,
  kindLabel,
  MEMORY_MAX_CHARS,
  memoryErrorMessage,
  memoryListParams,
  parseTags,
  SCOPE_LABELS,
  scopeLabel,
  sortMemories,
  STATUS_FILTERS,
  STATUS_LABELS,
  validateMemoryDraft,
  type MemoryDraft,
  type MemoryStatusFilter,
} from "@/lib/memory";
import type { Agent, MemoryKind, MemoryRecord, MemoryScope } from "@/lib/types";

export type MemoryAction =
  | { type: "pin"; pinned: boolean }
  | { type: "edit"; content: string }
  | { type: "contest"; reason: string }
  | { type: "forget" }
  | { type: "approve" }
  | { type: "reject" };

export function MemoryItemCard({
  memory,
  canWrite,
  isAdmin,
  busy = false,
  onAction,
  now,
}: {
  memory: MemoryRecord;
  canWrite: boolean;
  isAdmin: boolean;
  busy?: boolean;
  onAction: (memory: MemoryRecord, action: MemoryAction) => void;
  now?: number;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(memory.content);
  const [confirm, setConfirm] = useState<"forget" | "contest" | null>(null);
  const [reason, setReason] = useState("");
  const pinned = Boolean(memory.pinned_at);
  const status = STATUS_LABELS[memory.status];
  const proposed = memory.status === "proposed";
  const editable = canWrite && (memory.status === "active" || memory.status === "contested");

  return (
    <li
      data-testid={`memory-${memory.id}`}
      className={`rounded-2xl border px-4 py-3 ${pinned ? "border-accent/40 bg-accent-soft/40" : "border-line bg-raised"}`}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          {editing ? (
            <form
              className="space-y-2"
              onSubmit={(event) => {
                event.preventDefault();
                const next = draft.trim();
                if (!next || next === memory.content) {
                  setEditing(false);
                  return;
                }
                onAction(memory, { type: "edit", content: next });
                setEditing(false);
              }}
            >
              <Textarea
                aria-label="Edit memory"
                value={draft}
                rows={3}
                maxLength={MEMORY_MAX_CHARS}
                onChange={(event) => setDraft(event.target.value)}
              />
              <p className="text-xs text-faint">Saving creates a new version; the old wording is kept as history.</p>
              <div className="flex gap-2">
                <Button size="sm" variant="primary" type="submit" disabled={busy}>
                  Save
                </Button>
                <Button size="sm" variant="ghost" type="button" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </div>
            </form>
          ) : (
            <p className="whitespace-pre-wrap text-sm text-ink">{memory.content || <span className="italic text-faint">Forgotten</span>}</p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-dim">
            <Chip>{scopeLabel(memory.scope)}</Chip>
            <Chip>{kindLabel(memory.kind)}</Chip>
            {memory.status !== "active" ? <StatusPill status={status} /> : null}
            <span>{confidenceWord(memory.confidence)}</span>
            <span aria-hidden>·</span>
            <span>{importanceWord(memory.importance)}</span>
            {memory.sensitivity === "redacted" ? <Badge tone="warn">Secret removed</Badge> : null}
            {memory.tags_json.map((tag) => (
              <span key={tag} className="text-faint">
                #{tag}
              </span>
            ))}
          </div>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-2 text-xs text-faint">
            <span>
              {memory.created_by_type === "user" ? "Added by a person" : memory.created_by_type === "agent" ? "Learned by the agent" : "Added automatically"}{" "}
              {timeAgo(memory.created_at, now)}
            </span>
            {memory.version > 1 ? <span>· version {memory.version}</span> : null}
            {memory.source_conversation_id ? (
              <Link href={`/chats/${memory.source_conversation_id}`} className="inline-flex items-center gap-1 text-accent-strong hover:underline">
                <MessageSquare size={12} aria-hidden /> From a chat
              </Link>
            ) : null}
            {memory.expires_at ? <span>· expires {timeAgo(memory.expires_at, now).replace(" ago", "")}</span> : null}
          </p>
        </div>
        {canWrite && (memory.status === "active" || memory.status === "contested") ? (
          <button
            type="button"
            aria-label={pinned ? "Unpin" : "Pin"}
            aria-pressed={pinned}
            title={pinned ? "Unpin" : "Pin so it's always recalled"}
            disabled={busy}
            onClick={() => onAction(memory, { type: "pin", pinned: !pinned })}
            className={`inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors hover:bg-hover md:h-9 md:w-9 ${pinned ? "text-accent-strong" : "text-faint"}`}
          >
            <Star size={16} fill={pinned ? "currentColor" : "none"} aria-hidden />
          </button>
        ) : null}
      </div>

      {!editing && (editable || (isAdmin && proposed)) ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {isAdmin && proposed ? (
            <>
              <Button size="sm" variant="primary" disabled={busy} onClick={() => onAction(memory, { type: "approve" })}>
                Approve
              </Button>
              <Button size="sm" disabled={busy} onClick={() => onAction(memory, { type: "reject" })}>
                Reject
              </Button>
            </>
          ) : null}
          {editable ? (
            <>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => { setDraft(memory.content); setEditing(true); }}>
                Edit
              </Button>
              {memory.status === "active" ? (
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => { setReason(""); setConfirm("contest"); }}>
                  That’s wrong
                </Button>
              ) : null}
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirm("forget")} className="text-danger">
                Forget
              </Button>
            </>
          ) : null}
        </div>
      ) : null}

      <Dialog
        title="Forget this memory?"
        open={confirm === "forget"}
        onClose={() => setConfirm(null)}
        description="This is permanent."
      >
        <p className="text-sm text-dim">
          The agent stops recalling it right away. The wording is erased from every version, leaving only a
          content-free note that something was forgotten.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setConfirm(null)}>
            Keep it
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              setConfirm(null);
              onAction(memory, { type: "forget" });
            }}
          >
            Forget permanently
          </Button>
        </div>
      </Dialog>

      <Dialog title="Mark as wrong" open={confirm === "contest"} onClose={() => setConfirm(null)}>
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            setConfirm(null);
            onAction(memory, { type: "contest", reason: reason.trim() });
          }}
        >
          <p className="text-sm text-dim">
            The agent keeps this in mind but treats it as disputed until someone edits or forgets it.
          </p>
          <Field label="Why is it wrong? (optional)">
            <Textarea value={reason} rows={3} maxLength={500} onChange={(event) => setReason(event.target.value)} />
          </Field>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" type="button" onClick={() => setConfirm(null)}>
              Cancel
            </Button>
            <Button variant="primary" type="submit">
              Mark as wrong
            </Button>
          </div>
        </form>
      </Dialog>
    </li>
  );
}

export function MemoryPanel({
  workspaceId,
  agent,
  canWrite,
  isAdmin,
  initialScope,
}: {
  workspaceId: string;
  agent: Agent;
  canWrite: boolean;
  isAdmin: boolean;
  /** Which scope to open on, when the person arrived from a link to one
   * particular memory. Ignored for a team scope on an agent with no team,
   * whose segmented control has no such option to land on. */
  initialScope?: MemoryScope;
}) {
  const hasTeam = Boolean(agent.team_id);
  const [scope, setScope] = useState<MemoryScope>(
    initialScope && (initialScope !== "team" || hasTeam) ? initialScope : "agent",
  );
  const [filter, setFilter] = useState<MemoryStatusFilter>("active");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const invalidate = useInvalidateMemories(workspaceId);
  const query = useMemories(
    workspaceId,
    memoryListParams(scope, { agentId: agent.id, teamId: agent.team_id }, filter),
    scope !== "team" || hasTeam,
  );

  const act = useMutation({
    mutationFn: async ({ memory, action }: { memory: MemoryRecord; action: MemoryAction }) => {
      const base = `/api/v1/workspaces/${workspaceId}/memories/${memory.id}`;
      switch (action.type) {
        case "pin":
          return api<MemoryRecord>(`${base}/pin`, { method: "POST", body: { pinned: action.pinned } });
        case "edit":
          return api<MemoryRecord>(base, { method: "PATCH", body: { content: action.content } });
        case "contest":
          return api<MemoryRecord>(`${base}/contest`, { method: "POST", body: { reason: action.reason } });
        case "forget":
          return api<MemoryRecord>(`${base}/forget`, { method: "POST" });
        case "approve":
          return api<MemoryRecord>(`${base}/approve`, { method: "POST" });
        case "reject":
          return api<MemoryRecord>(`${base}/reject`, { method: "POST" });
      }
    },
    onMutate: ({ memory }) => setBusyId(memory.id),
    onSettled: () => setBusyId(null),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? memoryErrorMessage(err.status, err.detail) : "Saving failed. Check your connection and try again."),
  });

  const dedupe = useMutation({
    mutationFn: () =>
      api<{ clusters: number; superseded: number; remaining_active: number; adjudicated: number; llm: boolean }>(
        `/api/v1/workspaces/${workspaceId}/memories/deduplicate`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setError(null);
      const smart = result.llm ? " Smart matching was used to compare wordings." : "";
      setNotice(
        result.superseded === 0
          ? `No duplicates found.${smart}`
          : `Merged ${result.superseded} duplicate${result.superseded === 1 ? "" : "s"} into ${result.clusters} ${result.clusters === 1 ? "memory" : "memories"}.${smart}`,
      );
      invalidate();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? memoryErrorMessage(err.status, err.detail) : "Cleanup failed. Check your connection and try again."),
  });

  // Agent names run up to 200 chars; the segmented control never wraps, so a
  // long name would push the whole page sideways on a phone.
  const shortName =
    agent.name.length > 24 ? `${agent.name.slice(0, 24).trimEnd()}…` : agent.name;
  const scopeOptions: { id: MemoryScope; label: string }[] = [
    { id: "agent", label: `${SCOPE_LABELS.agent} (just ${shortName})` },
    ...(hasTeam ? [{ id: "team" as const, label: SCOPE_LABELS.team }] : []),
    { id: "workspace", label: SCOPE_LABELS.workspace },
  ];

  const items = query.data ? sortMemories(filterByStatus(query.data.items, filter)) : [];

  return (
    <SectionCard
      title="Memory"
      description="Things this agent keeps in mind across chats. Nothing here is a raw transcript — only curated notes."
      action={
        canWrite || isAdmin ? (
          <div className="flex flex-wrap justify-end gap-2">
            {isAdmin ? (
              <Button
                size="sm"
                variant="ghost"
                disabled={dedupe.isPending}
                onClick={() => dedupe.mutate()}
                title="Merge memories that say the same thing in different words"
              >
                <Merge size={14} /> {dedupe.isPending ? "Cleaning up…" : "Clean up duplicates"}
              </Button>
            ) : null}
            {canWrite ? (
              <Button size="sm" onClick={() => setComposing(true)}>
                <Plus size={14} /> Remember something
              </Button>
            ) : null}
          </div>
        ) : null
      }
    >
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Segmented label="Who shares this memory" options={scopeOptions} value={scope} onChange={setScope} />
        <Segmented label="Status" options={STATUS_FILTERS} value={filter} onChange={setFilter} />
      </div>
      <ErrorNote message={error} />
      {notice ? (
        <p role="status" className="mb-3 text-xs text-dim">
          {notice}
        </p>
      ) : null}
      {query.isPending ? (
        <Spinner label="Loading memory…" />
      ) : query.isError ? (
        <LoadError what="this agent’s memory" onRetry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title={filter === "review" ? "Nothing waiting for a look" : "Nothing remembered yet"}
          description={
            filter === "review"
              ? "Proposed and disputed memories show up here."
              : scope === "agent"
                ? `${agent.name} learns as it works and you can add notes with “Remember something”.`
                : `No ${scopeLabel(scope).toLowerCase()}-wide memories yet.`
          }
        />
      ) : (
        <ul className="space-y-2">
          {items.map((memory) => (
            <MemoryItemCard
              key={memory.id}
              memory={memory}
              canWrite={canWrite}
              isAdmin={isAdmin}
              busy={busyId === memory.id}
              onAction={(target, action) => act.mutate({ memory: target, action })}
            />
          ))}
        </ul>
      )}
      {query.data && query.data.total > query.data.items.length ? (
        <p className="mt-3 text-xs text-faint">Showing the newest {query.data.items.length} of {query.data.total}.</p>
      ) : null}

      <RememberDialog
        workspaceId={workspaceId}
        agent={agent}
        isAdmin={isAdmin}
        open={composing}
        onClose={() => setComposing(false)}
        onSaved={() => {
          setComposing(false);
          invalidate();
        }}
      />
    </SectionCard>
  );
}

function RememberDialog({
  workspaceId,
  agent,
  isAdmin,
  open,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  agent: Agent;
  isAdmin: boolean;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<MemoryDraft>({ content: "", scope: "agent", kind: "fact", tags: "" });
  const [error, setError] = useState<string | null>(null);
  const hasTeam = Boolean(agent.team_id);

  const save = useMutation({
    mutationFn: () =>
      api<MemoryRecord>(`/api/v1/workspaces/${workspaceId}/memories`, {
        method: "POST",
        body: {
          content: draft.content.trim(),
          scope: draft.scope,
          agent_id: draft.scope === "agent" ? agent.id : undefined,
          team_id: draft.scope === "team" ? agent.team_id : undefined,
          kind: draft.kind,
          tags: parseTags(draft.tags),
        },
      }),
    onSuccess: () => {
      setDraft({ content: "", scope: "agent", kind: "fact", tags: "" });
      setError(null);
      onSaved();
    },
    onError: (err) =>
      setError(err instanceof ApiError ? memoryErrorMessage(err.status, err.detail) : "Saving failed. Check your connection and try again."),
  });

  return (
    <Dialog title="Remember something" description={`${agent.name} will keep this in mind from now on.`} open={open} onClose={onClose}>
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          const problem = validateMemoryDraft(draft, { isAdmin, hasTeam });
          if (problem) {
            setError(problem);
            return;
          }
          save.mutate();
        }}
      >
        <Field label="What to remember" hint={`${draft.content.length.toLocaleString()} / ${MEMORY_MAX_CHARS.toLocaleString()}`}>
          <Textarea
            value={draft.content}
            rows={4}
            maxLength={MEMORY_MAX_CHARS}
            autoFocus
            placeholder="e.g. We deploy on Tuesdays; never on Fridays."
            onChange={(event) => setDraft({ ...draft, content: event.target.value })}
          />
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Who should know it" hint={isAdmin ? undefined : "Team and company memories need an admin."}>
            <Select value={draft.scope} onChange={(event) => setDraft({ ...draft, scope: event.target.value as MemoryScope })}>
              <option value="agent">Just {agent.name}</option>
              <option value="team" disabled={!isAdmin || !hasTeam}>
                The whole team
              </option>
              <option value="workspace" disabled={!isAdmin}>
                The whole company
              </option>
            </Select>
          </Field>
          <Field label="Kind">
            <Select value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value as MemoryKind })}>
              {(Object.keys(KIND_LABELS) as MemoryKind[]).map((kind) => (
                <option key={kind} value={kind}>
                  {KIND_LABELS[kind]}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Tags (optional)" hint="Comma-separated, up to 10.">
          <Textarea rows={1} value={draft.tags} onChange={(event) => setDraft({ ...draft, tags: event.target.value })} placeholder="deploys, process" />
        </Field>
        <p className="text-xs text-faint">Passwords, keys, and tokens are never stored — they’re rejected or blanked out automatically.</p>
        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Remember"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
