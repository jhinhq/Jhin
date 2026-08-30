"use client";

/** The reviewed-skills gallery on the Skills page (docs/architecture/catalog.md):
 * a catalog-backed search over the skill libraries the Jhin team reviewed,
 * populated by default — the server's own trust filter already keeps the
 * unreviewed community rows out, so no `include_indexed` flag travels from
 * here. Install resolves the skill server-side by slug and reuses the same
 * GitHub fetch/parse path a browse install takes. */

import { BookOpen, Download, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { CatalogEntryDialog } from "@/components/catalog-entry-dialog";
import { CategoryRail } from "@/components/catalog/category-rail";
import { LogoTile } from "@/components/catalog/logo-tile";
import { TrustBadge } from "@/components/catalog/trust-badge";
import { LoadError } from "@/components/company/bits";
import { Badge, Button, EmptyState, ErrorNote, Input } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { useCatalogFacets, useCatalogSearch, useInstallCatalogSkill } from "@/lib/hooks";
import type { CatalogEntry } from "@/lib/types";

/** Debounce a fast-changing value (the search box) before it drives a query. */
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

function SkillCard({
  entry,
  isAdmin,
  installing,
  installed,
  onInstall,
  onOpenDetail,
}: {
  entry: CatalogEntry;
  isAdmin: boolean;
  installing: boolean;
  installed: boolean;
  onInstall: () => void;
  onOpenDetail: () => void;
}) {
  return (
    <li
      data-testid={`skill-${entry.slug}`}
      className="flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
    >
      <header className="flex items-center gap-3">
        <LogoTile name={entry.name} icon={entry.icon} logoUrl={entry.logo_url} size={40} />
        <h3 className="min-w-0 flex-1 truncate font-display text-sm font-semibold text-ink">
          {entry.name}
        </h3>
        <TrustBadge tier={entry.trust_tier} deprecated={entry.deprecated} />
      </header>
      <p className={`line-clamp-2 text-sm leading-relaxed ${entry.summary ? "text-dim" : "text-faint"}`}>
        {entry.summary || "The index carries no description for this one."}
      </p>
      <footer className="mt-auto flex items-center justify-between gap-2 border-t border-line pt-3">
        <span className="truncate text-xs text-faint">{entry.category}</span>
        <div className="flex shrink-0 items-center gap-2">
          <Button size="sm" variant="ghost" onClick={onOpenDetail} aria-label={`Details for ${entry.name}`}>
            Details
          </Button>
          {isAdmin ? (
            installed ? (
              <Badge tone="ok">Installed</Badge>
            ) : (
              <Button
                size="sm"
                variant="primary"
                disabled={installing}
                onClick={onInstall}
                aria-label={`Install ${entry.name}`}
              >
                <Download size={13} /> {installing ? "Installing…" : "Install"}
              </Button>
            )
          ) : null}
        </div>
      </footer>
    </li>
  );
}

export function SkillCatalogGallery({
  workspaceId,
  isAdmin,
  onCatalogEmpty,
}: {
  workspaceId: string;
  isAdmin: boolean;
  /** Fires with `true` when the catalog itself has nothing to show — no
   * search typed, no filter set. The page uses it to open the Advanced
   * GitHub browser so the tab is never two-thirds blank. */
  onCatalogEmpty?: (empty: boolean) => void;
}) {
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebounced(query, 300);
  const [category, setCategory] = useState<string | null>(null);
  const [detailSlug, setDetailSlug] = useState<string | null>(null);
  const [installError, setInstallError] = useState<string | null>(null);
  const [installingSlug, setInstallingSlug] = useState<string | null>(null);
  const [installedSlugs, setInstalledSlugs] = useState<ReadonlySet<string>>(new Set());

  // Reviewed skills carry a tier the default filter keeps, so no
  // `include_indexed` here — the gallery is populated out of the box.
  const search = useCatalogSearch({
    kind: "skill",
    q: debouncedQuery || undefined,
    category: category ?? undefined,
    limit: 40,
  });
  const facets = useCatalogFacets({ kind: "skill" });
  const install = useInstallCatalogSkill(workspaceId);

  const items = search.data?.items ?? [];
  const total = search.data?.total ?? 0;

  // "Empty catalog" is a data state (no reviewed release indexed yet), not a
  // failed search: nothing typed, nothing filtered, and the query resolved.
  const emptyCatalog =
    Boolean(search.data) && items.length === 0 && debouncedQuery === "" && category === null;
  useEffect(() => {
    onCatalogEmpty?.(emptyCatalog);
  }, [emptyCatalog, onCatalogEmpty]);

  const installSkill = (entry: CatalogEntry) => {
    setInstallError(null);
    setInstallingSlug(entry.slug);
    install.mutate(entry.slug, {
      onSuccess: () => {
        setInstalledSlugs((current) => new Set(current).add(entry.slug));
      },
      onError: (error) => {
        setInstallError(
          error instanceof ApiError ? error.detail : "Installing the skill failed.",
        );
      },
      onSettled: () => setInstallingSlug(null),
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative min-w-0 flex-1">
          <Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
            aria-hidden
          />
          <Input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search skills — release notes, code review, PDFs…"
            aria-label="Search skills"
            className="pl-8"
          />
        </div>
        {emptyCatalog ? null : (
          <span className="text-xs text-faint sm:ml-auto" data-testid="skill-count">
            {items.length} of {total} skills
          </span>
        )}
      </div>

      {(facets.data?.category ?? []).length > 0 ? (
        <CategoryRail
          categories={(facets.data?.category ?? []).map((bucket) => ({
            value: bucket.value,
            label: bucket.label,
            count: bucket.count,
          }))}
          active={category}
          onChange={setCategory}
        />
      ) : null}

      <ErrorNote message={installError} />

      {search.isPending ? (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-hidden>
          {Array.from({ length: 6 }, (_, index) => (
            <li key={index} className="h-28 rounded-2xl bg-raised animate-pulse" />
          ))}
        </ul>
      ) : search.isError ? (
        <LoadError what="the skills library" onRetry={() => void search.refetch()} />
      ) : items.length === 0 ? (
        debouncedQuery === "" && category === null ? (
          // Nothing was searched and nothing was filtered: the gallery itself
          // is empty. That is a data state (no reviewed release indexed yet),
          // and blaming a search nobody typed would be a lie.
          <EmptyState
            icon={<BookOpen size={20} aria-hidden />}
            title="The skills gallery is on its way"
            description="Reviewed skill libraries are added over time — check back soon. Meanwhile, the Advanced section below browses a GitHub skill library directly."
          />
        ) : (
          <EmptyState
            icon={<Search size={20} aria-hidden />}
            title="No skills match"
            description="Try a different search — new libraries are reviewed and added over time."
          />
        )
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {items.map((entry) => (
            <SkillCard
              key={entry.slug}
              entry={entry}
              isAdmin={isAdmin}
              installing={installingSlug === entry.slug}
              installed={installedSlugs.has(entry.slug)}
              onInstall={() => installSkill(entry)}
              onOpenDetail={() => setDetailSlug(entry.slug)}
            />
          ))}
        </ul>
      )}

      {detailSlug ? (
        <CatalogEntryDialog
          slug={detailSlug}
          workspaceId={workspaceId}
          // Skills are read-only here: nothing to connect, so no connector
          // manifests are needed and the dialog's Connect path never opens.
          connectors={[]}
          onClose={() => setDetailSlug(null)}
          onCreated={() => setDetailSlug(null)}
        />
      ) : null}
    </div>
  );
}
