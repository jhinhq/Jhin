"use client";

/** Personas library (docs/architecture/personas.md): how your agents sound —
 * voice, pace, and manner. The cast Jhin ships sits next to the workspace's
 * own cards; a persona shapes how an agent says things, never what it may
 * do. Built-ins are duplicated rather than edited. */

import { useMutation } from "@tanstack/react-query";
import { Download, Drama, Plus, Search, Sparkles } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { LoadError } from "@/components/company/bits";
import { PersonaCard } from "@/components/personas/persona-card";
import { PersonaDetailDialog } from "@/components/personas/persona-detail-dialog";
import { PersonaEditorDialog } from "@/components/personas/persona-editor-dialog";
import {
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorNote,
  focusRing,
  Input,
  Select,
  Spinner,
} from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { useInvalidatePersonas, usePersonas } from "@/lib/hooks";
import { filterPersonas } from "@/lib/personas";
import type { InstallBuiltinPersonasResult, Persona, PersonaSource } from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

type EditorState = { mode: "create" } | { mode: "edit"; persona: Persona } | null;

export default function PersonasPage() {
  // `useSearchParams` needs a Suspense boundary for the static export build.
  return (
    <Suspense fallback={<Spinner />}>
      <PersonasView />
    </Suspense>
  );
}

function deletionBody(persona: Persona): string {
  const count = persona.agent_count;
  const lead = `“${persona.display_name}” will be removed from the library.`;
  if (count <= 0) return `${lead} No agent is wearing it.`;
  if (count === 1) {
    return `${lead} The 1 agent wearing it carries on without a persona from its next run.`;
  }
  return `${lead} The ${count} agents wearing it carry on without a persona from their next run.`;
}

function PersonasView() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const search = useSearchParams();
  const personas = usePersonas(workspaceId);
  const invalidate = useInvalidatePersonas(workspaceId);

  const [query, setQuery] = useState("");
  const [funOnly, setFunOnly] = useState(false);
  const [source, setSource] = useState<PersonaSource | "">("");
  // On by default: a card you just switched off must not vanish from under you.
  const [showDisabled, setShowDisabled] = useState(true);
  // Deep link from a persona chip: `/personas?persona=<id>` opens the card.
  const [detailId, setDetailId] = useState<string | null>(() => search.get("persona"));
  const [editor, setEditor] = useState<EditorState>(null);
  const [pendingDelete, setPendingDelete] = useState<Persona | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNote, setActionNote] = useState<string | null>(null);

  const install = useMutation({
    mutationFn: () =>
      api<InstallBuiltinPersonasResult>(
        `/api/v1/workspaces/${workspaceId}/personas/install-builtins`,
        { method: "POST" },
      ),
    onSuccess: (result) => {
      setActionError(null);
      setActionNote(
        result.installed + result.refreshed === 0
          ? "Nothing to add — the cast is complete and up to date."
          : `Added ${result.installed} and refreshed ${result.refreshed}.`,
      );
      invalidate();
    },
    onError: (error) => setActionError(errorText(error, "Installing the defaults failed.")),
  });

  const toggle = useMutation({
    mutationFn: (persona: Persona) =>
      api<Persona>(
        `/api/v1/workspaces/${workspaceId}/personas/${persona.id}/${persona.enabled ? "disable" : "enable"}`,
        { method: "POST" },
      ),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) => setActionError(errorText(error, "Changing the persona failed.")),
  });

  const remove = useMutation({
    mutationFn: (persona: Persona) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/personas/${persona.id}`, { method: "DELETE" }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) => setActionError(errorText(error, "Deleting the persona failed.")),
  });

  const duplicate = useMutation({
    mutationFn: (persona: Persona) =>
      api<Persona>(`/api/v1/workspaces/${workspaceId}/personas/${persona.id}/duplicate`, {
        method: "POST",
        body: {},
      }),
    onSuccess: (created) => {
      setActionError(null);
      invalidate();
      // The copy is yours: open it for editing straight away.
      setDetailId(null);
      setEditor({ mode: "edit", persona: created });
    },
    onError: (error) => setActionError(errorText(error, "Duplicating the persona failed.")),
  });

  const items = personas.data?.items ?? [];
  const visible = filterPersonas(items, { query, funOnly, source, showDisabled });
  const detail = detailId ? items.find((persona) => persona.id === detailId) : undefined;
  const busy = toggle.isPending || duplicate.isPending || remove.isPending;

  const clearFilters = () => {
    setQuery("");
    setFunOnly(false);
    setSource("");
    setShowDisabled(true);
  };

  return (
    <>
      <PageHeader
        title="Personas"
        description="How your agents sound — voice, pace, and manner. A persona shapes how an agent says things, never what it may do."
        actions={
          isAdmin ? (
            <>
              <Button size="sm" onClick={() => install.mutate()} disabled={install.isPending}>
                <Download size={14} /> {install.isPending ? "Installing…" : "Install missing defaults"}
              </Button>
              <Button variant="primary" size="sm" onClick={() => setEditor({ mode: "create" })}>
                <Plus size={14} /> New persona
              </Button>
            </>
          ) : null
        }
      />
      <PageBody className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search
              size={14}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
              aria-hidden
            />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search by name or description…"
              className="pl-8"
              aria-label="Search personas"
            />
          </div>
          <button
            type="button"
            aria-pressed={funOnly}
            onClick={() => setFunOnly((current) => !current)}
            className={`inline-flex min-h-10 items-center gap-1 rounded-full border px-3 py-1 text-xs font-medium transition-colors md:min-h-0 ${focusRing} ${
              funOnly
                ? "border-accent bg-accent-soft text-accent-strong"
                : "border-line bg-surface text-dim hover:text-ink"
            }`}
          >
            <Sparkles size={12} aria-hidden /> Fun
          </button>
          <Select
            aria-label="Source"
            value={source}
            onChange={(event) => setSource(event.target.value as PersonaSource | "")}
          >
            <option value="">All sources</option>
            <option value="built_in">By Jhin</option>
            <option value="custom">Yours</option>
            <option value="agent">Agent-made</option>
          </Select>
          <label className="inline-flex items-center gap-2 text-sm text-dim">
            <input
              type="checkbox"
              checked={showDisabled}
              onChange={(event) => setShowDisabled(event.target.checked)}
              aria-label="Show switched-off personas"
              className="accent-[var(--accent)]"
            />
            Show switched off
          </label>
        </div>

        <ErrorNote message={actionError} />
        {actionNote ? (
          <p role="status" className="text-sm text-dim">
            {actionNote}
          </p>
        ) : null}

        {personas.isPending ? <Spinner label="Loading personas…" /> : null}
        {personas.isError ? (
          <LoadError what="the personas library" onRetry={() => void personas.refetch()} />
        ) : null}

        {personas.data && items.length === 0 ? (
          <EmptyState
            icon={<Drama size={20} aria-hidden />}
            title="No personas yet"
            description="Personas shape how an agent sounds — never what it may do. Install the cast Jhin ships to see how they read, then write your own."
            action={
              isAdmin ? (
                <Button variant="primary" size="sm" onClick={() => install.mutate()} disabled={install.isPending}>
                  <Download size={14} /> Install the cast
                </Button>
              ) : undefined
            }
          />
        ) : null}

        {items.length > 0 && visible.length === 0 ? (
          <EmptyState
            title="Nothing matches"
            description="Try another search, or clear the filters."
            action={
              <Button size="sm" onClick={clearFilters}>
                Clear filters
              </Button>
            }
          />
        ) : null}

        {visible.length > 0 ? (
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {visible.map((persona) => (
              <PersonaCard
                key={persona.id}
                persona={persona}
                isAdmin={isAdmin}
                onOpen={() => setDetailId(persona.id)}
                onEdit={() => setEditor({ mode: "edit", persona })}
                onDuplicate={() => duplicate.mutate(persona)}
                onToggle={() => toggle.mutate(persona)}
                onDelete={() => setPendingDelete(persona)}
                toggling={toggle.isPending && toggle.variables?.id === persona.id}
                duplicating={duplicate.isPending && duplicate.variables?.id === persona.id}
                removing={remove.isPending && remove.variables?.id === persona.id}
              />
            ))}
          </ul>
        ) : null}
      </PageBody>

      {detail ? (
        <PersonaDetailDialog
          persona={detail}
          isAdmin={isAdmin}
          busy={busy}
          onClose={() => setDetailId(null)}
          onEdit={() => {
            setDetailId(null);
            setEditor({ mode: "edit", persona: detail });
          }}
          onDuplicate={() => duplicate.mutate(detail)}
          onToggle={() => toggle.mutate(detail)}
        />
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this persona?"
        body={pendingDelete ? deletionBody(pendingDelete) : null}
        confirmLabel="Delete persona"
        busy={remove.isPending}
        onConfirm={() => {
          if (pendingDelete === null) return;
          remove.mutate(pendingDelete, { onSettled: () => setPendingDelete(null) });
        }}
        onClose={() => setPendingDelete(null)}
      />

      {editor ? (
        <PersonaEditorDialog
          workspaceId={workspaceId}
          initial={editor.mode === "edit" ? editor.persona : null}
          onClose={() => setEditor(null)}
          onSaved={invalidate}
        />
      ) : null}
    </>
  );
}
