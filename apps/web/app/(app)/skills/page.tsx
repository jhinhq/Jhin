"use client";

/** Skills library (docs/architecture/skills.md): reusable instruction packs
 * agents read while they work. Install the shipped starters, write your
 * own, or import from GitHub / a zip — imports stay off until reviewed. */

import { useMutation } from "@tanstack/react-query";
import { BookOpen, Download, Pencil, Plus, Search, Trash2, Upload, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Disclosure } from "@/components/company/bits";
import { SkillCatalogGallery } from "@/components/skill-catalog-gallery";
import { SkillsBrowseGallery } from "@/components/skills-browse-gallery";
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorNote,
  Field,
  focusRing,
  Input,
  Select,
  Spinner,
  Tabs,
  Textarea,
} from "@/components/ui";
import { api, ApiError, apiUpload } from "@/lib/api";
import {
  useBrowseSkills,
  useInvalidateSkillSources,
  useInvalidateSkills,
  useSkill,
  useSkills,
  useSkillSources,
} from "@/lib/hooks";
import {
  categoriesOf,
  DEFAULT_CATEGORY,
  groupByCategory,
  isValidGithubRef,
  isValidSkillName,
  needsReviewCount,
  SOURCE_LABELS,
} from "@/lib/skills";
import type {
  BrowseInstallResult,
  BrowseSkillEntry,
  InstallBuiltinsResult,
  Skill,
  SkillDetail,
  SkillImportResult,
  SkillSourceInfo,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";

/** A row of toggleable category chips; `null` category means "All". */
function CategoryChips({
  categories,
  active,
  onChange,
}: {
  categories: string[];
  active: string | null;
  onChange: (category: string | null) => void;
}) {
  if (categories.length === 0) return null;
  const chip = (label: string, value: string | null) => {
    const isActive = active === value;
    return (
      <button
        key={label}
        type="button"
        aria-pressed={isActive}
        onClick={() => onChange(isActive ? null : value)}
        className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${focusRing} ${
          isActive
            ? "border-accent bg-accent-soft text-accent-strong"
            : "border-line bg-surface text-dim hover:text-ink"
        }`}
      >
        {label}
      </button>
    );
  };
  return (
    <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by category">
      {chip("All", null)}
      {categories.map((category) => chip(category, category))}
    </div>
  );
}

function SkillRow({
  skill,
  isAdmin,
  onToggle,
  toggling,
  onEdit,
  onDelete,
  removing,
}: {
  skill: Skill;
  isAdmin: boolean;
  onToggle: () => void;
  toggling: boolean;
  onEdit: () => void;
  onDelete: () => void;
  removing: boolean;
}) {
  return (
    <li className="flex flex-wrap items-start gap-3 rounded-2xl border border-line bg-raised px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <code className="font-mono text-sm font-medium">{skill.name}</code>
          <Badge>{SOURCE_LABELS[skill.source]}</Badge>
          {skill.source === "imported" && !skill.enabled ? (
            <Badge tone="warn">Review and enable</Badge>
          ) : !skill.enabled ? (
            <Badge tone="neutral">Off</Badge>
          ) : null}
          {skill.file_count > 0 ? (
            <span className="text-xs text-faint">
              {skill.file_count} {skill.file_count === 1 ? "file" : "files"}
            </span>
          ) : null}
        </div>
        <p className="mt-1 text-sm text-dim">{skill.description}</p>
      </div>
      {isAdmin ? (
        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            size="sm"
            onClick={onToggle}
            disabled={toggling}
            aria-label={`${skill.enabled ? "Disable" : "Enable"} ${skill.name}`}
          >
            {skill.enabled ? "Disable" : "Enable"}
          </Button>
          <Button size="sm" onClick={onEdit} aria-label={`Edit ${skill.name}`}>
            <Pencil size={14} />
          </Button>
          <Button
            size="sm"
            onClick={onDelete}
            disabled={removing}
            aria-label={`Delete ${skill.name}`}
          >
            <Trash2 size={14} />
          </Button>
        </div>
      ) : null}
    </li>
  );
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}

/** Debounce a fast-changing value (the search box) before it drives a query. */
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

export default function SkillsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const [tab, setTab] = useState<"library" | "browse">("library");
  const skills = useSkills(workspaceId);
  const invalidate = useInvalidateSkills(workspaceId);

  const [importOpen, setImportOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [categoryFilter, setCategoryFilter] = useState<string | null>(null);
  // When the reviewed gallery has nothing to show, the Advanced GitHub
  // browser opens itself so the tab isn't two-thirds blank. Sticky: a later
  // search in the gallery never yanks the browser shut again.
  const [advancedBrowseOpen, setAdvancedBrowseOpen] = useState(false);

  const install = useMutation({
    mutationFn: () =>
      api<InstallBuiltinsResult>(`/api/v1/workspaces/${workspaceId}/skills/install-builtins`, {
        method: "POST",
      }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) => setActionError(errorText(error, "Installing the starter skills failed.")),
  });

  const toggle = useMutation({
    mutationFn: (skill: Skill) =>
      api<SkillDetail>(`/api/v1/workspaces/${workspaceId}/skills/${skill.id}`, {
        method: "PATCH",
        body: { enabled: !skill.enabled },
      }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) => setActionError(errorText(error, "Changing the skill failed.")),
  });

  const remove = useMutation({
    mutationFn: (skill: Skill) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/skills/${skill.id}`, { method: "DELETE" }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
    onError: (error) => setActionError(errorText(error, "Deleting the skill failed.")),
  });

  const items = skills.data?.items ?? [];
  const reviewCount = needsReviewCount(items);
  const categories = categoriesOf(items);
  const visibleItems = categoryFilter ? items.filter((item) => item.category === categoryFilter) : items;
  const groups = groupByCategory(visibleItems);

  return (
    <>
      <PageHeader
        title="Skills"
        description="Reusable playbooks your agents can read while they work — how you write updates, review code, or ship releases."
        actions={
          isAdmin ? (
            <>
              <Button size="sm" onClick={() => install.mutate()} disabled={install.isPending}>
                <Download size={14} /> {install.isPending ? "Installing…" : "Install starter skills"}
              </Button>
              <Button size="sm" onClick={() => setImportOpen(true)}>
                <Upload size={14} /> Import
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => {
                  setEditingId(null);
                  setEditorOpen(true);
                }}
              >
                <Plus size={14} /> New skill
              </Button>
            </>
          ) : null
        }
      />
      <PageBody className="space-y-4">
        <Tabs
          label="Skills sections"
          tabs={[
            { id: "library", label: "Your skills" },
            { id: "browse", label: "Find new skills" },
          ]}
          value={tab}
          onChange={(id) => setTab(id as "library" | "browse")}
        />

        {tab === "library" ? (
          <div className="space-y-4">
            {reviewCount > 0 ? (
              <p
                data-testid="review-banner"
                className="rounded-xl border border-warn/30 bg-warn-soft px-3.5 py-2.5 text-sm text-warn"
              >
                {reviewCount} imported {reviewCount === 1 ? "skill is" : "skills are"} waiting for
                review. Read {reviewCount === 1 ? "it" : "them"} below and turn{" "}
                {reviewCount === 1 ? "it" : "them"} on when you trust the content.
              </p>
            ) : null}
            <ErrorNote message={actionError} />
            {skills.isPending ? <Spinner label="Loading skills…" /> : null}
            {skills.isError ? <ErrorNote message="Could not load the skills library." /> : null}
            {skills.data && items.length === 0 ? (
              <EmptyState
                icon={<BookOpen size={20} aria-hidden />}
                title="No skills yet"
                description="Skills are short instruction packs agents read on demand. Install the starters to see how they look, then write your own."
                action={
                  isAdmin ? (
                    <Button variant="primary" size="sm" onClick={() => install.mutate()}>
                      <Download size={14} /> Install starter skills
                    </Button>
                  ) : undefined
                }
              />
            ) : null}
            {items.length > 0 ? (
              <CategoryChips categories={categories} active={categoryFilter} onChange={setCategoryFilter} />
            ) : null}
            <div className="space-y-3">
              {groups.map(([category, group]) => (
                <details
                  key={category}
                  open
                  className="rounded-2xl border border-line bg-surface"
                  data-testid={`skill-category-${category}`}
                >
                  <summary className="cursor-pointer list-none px-4 py-2.5 text-sm font-semibold text-ink">
                    {category} <span className="font-normal text-faint">({group.length})</span>
                  </summary>
                  <ul className="space-y-2 border-t border-line px-4 py-3">
                    {group.map((skill) => (
                      <SkillRow
                        key={skill.id}
                        skill={skill}
                        isAdmin={isAdmin}
                        onToggle={() => toggle.mutate(skill)}
                        toggling={toggle.isPending}
                        onEdit={() => {
                          setEditingId(skill.id);
                          setEditorOpen(true);
                        }}
                        onDelete={() => {
                          if (
                            window.confirm(
                              `Delete the skill “${skill.name}”? Agents lose it immediately.`,
                            )
                          ) {
                            remove.mutate(skill);
                          }
                        }}
                        removing={remove.isPending}
                      />
                    ))}
                  </ul>
                </details>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-6">
            <SkillCatalogGallery
              workspaceId={workspaceId}
              isAdmin={isAdmin}
              onCatalogEmpty={(empty) => {
                if (empty) setAdvancedBrowseOpen(true);
              }}
            />
            <Disclosure
              key={advancedBrowseOpen ? "auto-open" : "folded"}
              defaultOpen={advancedBrowseOpen}
              label="Advanced — browse a GitHub library directly"
              openLabel="Hide the direct GitHub browser"
            >
              <BrowseLibrarySection
                workspaceId={workspaceId}
                isAdmin={isAdmin}
                onInstalled={invalidate}
              />
            </Disclosure>
          </div>
        )}
      </PageBody>

      <ImportDialog
        workspaceId={workspaceId}
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={invalidate}
      />
      {editorOpen ? (
        <EditorDialog
          workspaceId={workspaceId}
          skillId={editingId}
          existingCategories={categories}
          onClose={() => setEditorOpen(false)}
          onSaved={invalidate}
        />
      ) : null}
    </>
  );
}

function BrowseLibrarySection({
  workspaceId,
  isAdmin,
  onInstalled,
}: {
  workspaceId: string;
  isAdmin: boolean;
  onInstalled: () => void;
}) {
  const sources = useSkillSources(workspaceId);
  const invalidateSources = useInvalidateSkillSources(workspaceId);
  const [source, setSource] = useState("");
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebounced(query, 300);
  const [browseCategory, setBrowseCategory] = useState<string | null>(null);
  const [installError, setInstallError] = useState<string | null>(null);
  const [installingPath, setInstallingPath] = useState<string | null>(null);
  const [addSourceOpen, setAddSourceOpen] = useState(false);

  const activeSource = source || sources.data?.[0]?.source || "";
  const browse = useBrowseSkills(workspaceId, activeSource, debouncedQuery);

  const install = useMutation({
    mutationFn: (entry: BrowseSkillEntry) =>
      api<BrowseInstallResult>(`/api/v1/workspaces/${workspaceId}/skills/browse/install`, {
        method: "POST",
        body: { source: entry.source, skill_path: entry.path },
      }),
    onMutate: (entry: BrowseSkillEntry) => setInstallingPath(entry.path),
    onSuccess: () => {
      setInstallError(null);
      onInstalled();
      void browse.refetch();
    },
    onError: (error) => setInstallError(errorText(error, "Installing the skill failed.")),
    onSettled: () => setInstallingPath(null),
  });

  const removeSource = useMutation({
    mutationFn: (target: string) =>
      api<void>(`/api/v1/workspaces/${workspaceId}/skill-sources/${target}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      setInstallError(null);
      setSource("");
      invalidateSources();
    },
    onError: (error) => setInstallError(errorText(error, "Removing the source failed.")),
  });

  const activeEntry = sources.data?.find((entry) => entry.source === activeSource);
  const activeLabel = activeEntry?.label ?? activeSource;
  const allSkills = browse.data?.skills ?? [];
  const skillCategories = categoriesOf(allSkills);
  const visibleSkills = browseCategory
    ? allSkills.filter((entry) => entry.category === browseCategory)
    : allSkills;

  return (
    <div className="space-y-4">
      <p className="text-sm text-dim">
        Search public skill libraries and install what you need with one click.
      </p>
      {sources.isError ? (
        <ErrorNote message="Could not reach the skill sources catalog." />
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {sources.data && sources.data.length > 1 ? (
          <Select
            aria-label="Skill source"
            value={activeSource}
            onChange={(event) => {
              setSource(event.target.value);
              setBrowseCategory(null);
            }}
          >
            {sources.data.map((entry) => (
              <option key={entry.source} value={entry.source}>
                {entry.label}
                {entry.custom ? " (custom)" : ""}
              </option>
            ))}
          </Select>
        ) : sources.data?.[0] ? (
          <Badge>{sources.data[0].label}</Badge>
        ) : null}
        {isAdmin && activeEntry?.custom ? (
          <Button
            size="sm"
            onClick={() => {
              if (window.confirm(`Remove the source “${activeLabel}”?`)) {
                removeSource.mutate(activeSource);
              }
            }}
            disabled={removeSource.isPending}
            aria-label={`Remove source ${activeLabel}`}
          >
            <X size={14} /> Remove source
          </Button>
        ) : null}
        <div className="relative min-w-[220px] flex-1">
          <Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
            aria-hidden
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search skills by name or description…"
            className="pl-8"
            aria-label="Search skills"
          />
        </div>
        {isAdmin ? (
          <Button size="sm" onClick={() => setAddSourceOpen(true)}>
            <Plus size={14} /> Add a source
          </Button>
        ) : null}
      </div>
      {skillCategories.length > 0 ? (
        <CategoryChips categories={skillCategories} active={browseCategory} onChange={setBrowseCategory} />
      ) : null}
      <ErrorNote message={installError} />
      {browse.isPending && activeSource ? <Spinner label="Loading skills…" /> : null}
      {browse.isError ? (
        <ErrorNote
          message={errorText(browse.error, "Could not reach GitHub for this source right now.")}
        />
      ) : null}
      {browse.data ? (
        <SkillsBrowseGallery
          entries={visibleSkills}
          sourceLabel={activeLabel}
          canInstall={isAdmin}
          installingPath={installingPath}
          onInstall={(entry) => install.mutate(entry)}
        />
      ) : null}
      <AddSourceDialog
        workspaceId={workspaceId}
        open={addSourceOpen}
        onClose={() => setAddSourceOpen(false)}
        onAdded={(addedSource) => {
          invalidateSources();
          setSource(addedSource);
          setAddSourceOpen(false);
        }}
      />
    </div>
  );
}

function AddSourceDialog({
  workspaceId,
  open,
  onClose,
  onAdded,
}: {
  workspaceId: string;
  open: boolean;
  onClose: () => void;
  onAdded: (source: string) => void;
}) {
  const [ref, setRef] = useState("");
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);

  const add = useMutation({
    mutationFn: () =>
      api<SkillSourceInfo>(`/api/v1/workspaces/${workspaceId}/skill-sources`, {
        method: "POST",
        body: { source: ref.trim(), label: label.trim() },
      }),
    onSuccess: (created) => {
      setError(null);
      setRef("");
      setLabel("");
      onAdded(created.source);
    },
    onError: (mutationError) =>
      setError(
        errorText(
          mutationError,
          "Could not add this source — check it's a public repo with at least one SKILL.md.",
        ),
      ),
  });

  const close = () => {
    setError(null);
    onClose();
  };

  return (
    <Dialog
      title="Add a source"
      description="A public GitHub repository of skill folders. It's checked live before it's added — the same way an import is."
      open={open}
      onClose={close}
    >
      <div className="space-y-4">
        <Field label="Repository" hint="owner/repo, optionally /path — e.g. obra/superpowers">
          <Input
            value={ref}
            onChange={(event) => setRef(event.target.value)}
            placeholder="owner/repo or owner/repo/path"
          />
        </Field>
        <Field label="Label (optional)" hint="A friendly name shown in the source picker.">
          <Input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="" />
        </Field>
        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!isValidGithubRef(ref) || add.isPending}
            onClick={() => add.mutate()}
          >
            {add.isPending ? "Checking…" : "Add source"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

function ImportDialog({
  workspaceId,
  open,
  onClose,
  onImported,
}: {
  workspaceId: string;
  open: boolean;
  onClose: () => void;
  onImported: () => void;
}) {
  const [ref, setRef] = useState("");
  const [result, setResult] = useState<SkillImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const fromGithub = useMutation({
    mutationFn: () =>
      api<SkillImportResult>(`/api/v1/workspaces/${workspaceId}/skills/import`, {
        method: "POST",
        body: { github: ref.trim() },
      }),
    onSuccess: (data) => {
      setError(null);
      setResult(data);
      onImported();
    },
    onError: (mutationError) => setError(errorText(mutationError, "The import failed.")),
  });

  const fromZip = useMutation({
    mutationFn: (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      return apiUpload<SkillImportResult>(
        `/api/v1/workspaces/${workspaceId}/skills/import-zip`,
        formData,
      );
    },
    onSuccess: (data) => {
      setError(null);
      setResult(data);
      onImported();
    },
    onError: (mutationError) => setError(errorText(mutationError, "The upload failed.")),
  });

  const close = () => {
    setResult(null);
    setError(null);
    onClose();
  };

  return (
    <Dialog
      title="Import skills"
      description="Imported skills arrive turned off. Review each one, then enable it."
      open={open}
      onClose={close}
    >
      <div className="space-y-4">
        <Field
          label="From GitHub"
          hint="A public repository of skill folders, e.g. anthropics/skills — optionally a folder inside it."
        >
          <div className="flex gap-2">
            <Input
              value={ref}
              onChange={(event) => setRef(event.target.value)}
              placeholder="owner/repo or owner/repo/path"
            />
            <Button
              variant="primary"
              disabled={!isValidGithubRef(ref) || fromGithub.isPending}
              onClick={() => fromGithub.mutate()}
            >
              {fromGithub.isPending ? "Importing…" : "Import"}
            </Button>
          </div>
        </Field>
        <Field label="From a zip file" hint="A zip holding one or more skill folders (each with a SKILL.md). At most 5 MB.">
          <input
            ref={fileInput}
            type="file"
            accept=".zip"
            aria-label="Skill zip file"
            className="block w-full text-sm text-dim file:mr-3 file:rounded-xl file:border file:border-line file:bg-raised file:px-3 file:py-2 file:text-sm file:text-ink"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) fromZip.mutate(file);
              event.target.value = "";
            }}
          />
        </Field>
        <ErrorNote message={error} />
        {result ? (
          <div className="rounded-xl border border-line bg-raised px-3.5 py-2.5 text-sm">
            <p className="font-medium">
              {result.created} imported for review, {result.skipped} skipped.
            </p>
            <ul className="mt-1.5 space-y-0.5 text-xs text-dim">
              {result.skills.map((entry) => (
                <li key={entry.name}>
                  <code>{entry.name}</code> —{" "}
                  {entry.status === "proposed" ? "ready to review" : entry.reason}
                </li>
              ))}
              {result.warnings.map((warning) => (
                <li key={warning} className="text-warn">
                  {warning}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </Dialog>
  );
}

function EditorDialog({
  workspaceId,
  skillId,
  existingCategories,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  skillId: string | null;
  existingCategories: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const detail = useSkill(workspaceId, skillId);
  const creating = skillId === null;
  if (!creating && !detail.data) {
    return (
      <Dialog title="Edit skill" open onClose={onClose} wide>
        {detail.isError ? (
          <ErrorNote message="Could not load this skill." />
        ) : (
          <Spinner label="Loading skill…" />
        )}
      </Dialog>
    );
  }
  return (
    <EditorForm
      workspaceId={workspaceId}
      skillId={skillId}
      initial={detail.data ?? null}
      existingCategories={existingCategories}
      onClose={onClose}
      onSaved={onSaved}
    />
  );
}

function EditorForm({
  workspaceId,
  skillId,
  initial,
  existingCategories,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  skillId: string | null;
  initial: SkillDetail | null;
  existingCategories: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [content, setContent] = useState(initial?.content ?? "");
  const [category, setCategory] = useState(initial?.category ?? DEFAULT_CATEGORY);
  const [error, setError] = useState<string | null>(null);
  const creating = skillId === null;

  const save = useMutation({
    mutationFn: () =>
      creating
        ? api<SkillDetail>(`/api/v1/workspaces/${workspaceId}/skills`, {
            method: "POST",
            body: {
              name: name.trim(),
              description: description.trim(),
              content,
              category: category.trim() || undefined,
            },
          })
        : api<SkillDetail>(`/api/v1/workspaces/${workspaceId}/skills/${skillId}`, {
            method: "PATCH",
            body: {
              description: description.trim(),
              content,
              category: category.trim() || undefined,
            },
          }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (mutationError) => setError(errorText(mutationError, "Saving the skill failed.")),
  });

  const nameOk = creating ? isValidSkillName(name.trim()) : true;

  return (
    <Dialog title={creating ? "New skill" : "Edit skill"} open onClose={onClose} wide>
      <div className="space-y-4">
          <Field
            label="Name"
            hint="Lowercase letters, digits, and hyphens — this is how agents refer to the skill. It can’t change later."
          >
            <Input
              value={name}
              disabled={!creating}
              onChange={(event) => setName(event.target.value)}
              placeholder="release-notes"
            />
          </Field>
          <Field label="Description" hint="One or two sentences agents see in their prompt. Say what the skill does and when to use it.">
            <Textarea
              rows={2}
              maxLength={500}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Write user-facing release notes from a list of changes. Use when announcing a release."
            />
          </Field>
          <Field label="Category" hint="Groups this skill on the library page. Pick an existing one or type a new one.">
            <Input
              list="skill-category-options"
              value={category}
              onChange={(event) => setCategory(event.target.value)}
              placeholder={DEFAULT_CATEGORY}
            />
            <datalist id="skill-category-options">
              {existingCategories.map((option) => (
                <option key={option} value={option} />
              ))}
            </datalist>
          </Field>
          <Field label="Instructions (markdown)" hint="The full playbook the agent reads on demand.">
            <Textarea
              rows={16}
              value={content}
              onChange={(event) => setContent(event.target.value)}
              className="font-mono text-[13px] leading-relaxed"
              placeholder={"# My skill\n\nStep-by-step guidance…"}
            />
          </Field>
          <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!nameOk || description.trim().length === 0 || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : creating ? "Create skill" : "Save changes"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
