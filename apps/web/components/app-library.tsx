"use client";

/** The Apps library: search + filters over the catalog, connected state per
 * entry, and a Connect action. Presentational (no data fetching) so it is
 * component-testable.
 *
 * Two modes, one component. Without `onQueryChange` it is exactly what it has
 * always been — the 50 curated entries, filtered in the browser. With
 * `onQueryChange` the search box is controlled, the filtering happens on the
 * server, and the grid renders the merged catalog: curated entries first,
 * then everything the last sync indexed. The uncontrolled mode is the
 * fallback the page drops back to when the catalog endpoint is unreachable,
 * so a failed sync costs nobody the library they already had.
 *
 * The catalog mode keeps one filter dimension visible — the category rail —
 * and folds provenance, transport, sign-in, and the unreviewed-community
 * switch behind "More filters". Skills are not here at all any more; the page
 * pins `kind` to `"mcp"` and a single line points at their own library. */

import { ExternalLink, Search } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { CategoryRail } from "@/components/catalog/category-rail";
import { LogoTile } from "@/components/catalog/logo-tile";
import { TrustBadge } from "@/components/catalog/trust-badge";
import { Disclosure, LoadError } from "@/components/company/bits";
import { Badge, Button, EmptyState, focusRing, Input, Select } from "@/components/ui";
import {
  catalogCategories,
  connectionsForApp,
  FACET_VALUE_LABELS,
  filterCatalog,
  isSafeExternalUrl,
} from "@/lib/apps";
import { needsReauth } from "@/lib/oauth";
import type {
  CatalogApp,
  CatalogEntry,
  CatalogFacetBucket,
  CatalogFacets,
  ConnectionInfo,
} from "@/lib/types";

type ConnectShape = Pick<CatalogApp, "connector_type" | "stdio_only" | "mcp_url" | "url_unverified">;

function howItConnects(entry: ConnectShape): string {
  if (entry.connector_type) return "Built-in connector";
  if (entry.stdio_only) return "Needs a self-hosted server";
  if (entry.mcp_url && !entry.url_unverified) return "Official MCP server";
  return "MCP server (enter its URL)";
}

/** The dimensions the server can facet on, in the order they read best. */
export type FacetDimension = "kind" | "category" | "trust_tier" | "transport" | "auth_hint";

/** What "More filters" shows directly: provenance is a question people
 * actually ask, so it stays at the top level of the disclosure. `kind` is
 * pinned to `"mcp"` by the page and `category` has its own rail, so neither
 * renders as a chip row any more. */
const MORE_FILTER_ROWS: { key: FacetDimension; label: string }[] = [
  { key: "trust_tier", label: "Where it came from" },
];

/** Transport and sign-in are protocol trivia — useful to the person hosting a
 * server, noise to everyone else — so they sit one step further back, behind
 * an "Advanced" disclosure inside More filters. */
const ADVANCED_FILTER_ROWS: { key: FacetDimension; label: string }[] = [
  { key: "transport", label: "Transport" },
  { key: "auth_hint", label: "Sign-in" },
];

/** Long tails of one-hit buckets are noise; the server sorts by count, so the
 * head is the useful part. The active value is always kept so a filter can
 * never become unclearable by narrowing itself out of the list. */
const MAX_CHIPS_PER_ROW = 12;

const chipClass = (active: boolean) =>
  `min-h-10 rounded-full border px-3 py-1 text-xs font-medium transition-colors md:min-h-0 ${focusRing} ${
    active
      ? "border-accent bg-accent-soft text-accent-strong"
      : "border-line bg-surface text-dim hover:text-ink"
  }`;

function FacetChips({
  label,
  buckets,
  active,
  onChange,
}: {
  label: string;
  buckets: CatalogFacetBucket[];
  active: string | undefined;
  onChange: (value: string | undefined) => void;
}) {
  const head = buckets.slice(0, MAX_CHIPS_PER_ROW);
  const shown =
    active !== undefined && !head.some((bucket) => bucket.value === active)
      ? [...head, buckets.find((bucket) => bucket.value === active) ?? { value: active, label: active, count: 0 }]
      : head;
  if (shown.length < 2 && active === undefined) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label={label}>
      <span className="text-xs text-faint">{label}</span>
      {shown.map((bucket) => {
        const isActive = active === bucket.value;
        return (
          <button
            key={bucket.value}
            type="button"
            aria-pressed={isActive}
            onClick={() => onChange(isActive ? undefined : bucket.value)}
            className={chipClass(isActive)}
          >
            {FACET_VALUE_LABELS[bucket.value] ?? bucket.label}
            {bucket.count > 0 ? <span className="ml-1 text-faint">{bucket.count}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

const EMPTY_MESSAGE =
  "No apps match. Any app with an MCP server can still be added with “Add a custom app” at the top of this page.";

function CuratedCard({
  entry,
  connections,
  canManage,
  onConnect,
  onOpenConnection,
  onOpenDetail,
}: {
  entry: CatalogApp;
  connections: ConnectionInfo[];
  canManage: boolean;
  onConnect: (entry: CatalogApp) => void;
  onOpenConnection: (connection: ConnectionInfo) => void;
  onOpenDetail?: (slug: string) => void;
}) {
  const connected = connectionsForApp(entry, connections);
  // A lapsed sign-in has to be visible on the card people look at, not only
  // in the banner at the top of the page.
  const stale = needsReauth(connected);
  const connectable = Boolean(entry.connector_type) || !entry.stdio_only;
  return (
    <li
      data-testid={`app-${entry.slug}`}
      className="flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
    >
      <header className="flex items-center gap-3">
        <LogoTile name={entry.name} icon={entry.icon} logoUrl={entry.logo_url} size={36} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-display text-sm font-semibold text-ink">{entry.name}</h3>
          <p className="truncate text-xs text-dim">{entry.category} · {howItConnects(entry)}</p>
        </div>
        {stale.length > 0 ? (
          <Badge tone="warn">Reconnect needed</Badge>
        ) : connected.length > 0 ? (
          <Badge tone="ok">Connected</Badge>
        ) : entry.stdio_only ? (
          <Badge tone="neutral">Self-hosted</Badge>
        ) : null}
      </header>
      <p className="text-sm leading-relaxed text-dim">{entry.description}</p>
      {entry.stdio_only && entry.setup_note ? (
        <p className="text-xs text-faint">{entry.setup_note}</p>
      ) : null}
      <footer className="mt-auto flex items-center justify-between gap-2 border-t border-line pt-3">
        {entry.docs_url ? (
          <a
            href={entry.docs_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-accent-strong hover:underline"
          >
            <ExternalLink size={12} aria-hidden /> Docs
          </a>
        ) : (
          <span />
        )}
        <div className="flex items-center gap-2">
          {connected.length > 0 ? (
            <Button
              size="sm"
              variant={stale.length > 0 ? "primary" : "outline"}
              onClick={() => onOpenConnection(stale[0] ?? connected[0])}
            >
              {stale.length > 0 ? "Reconnect" : "Manage"}
            </Button>
          ) : null}
          {onOpenDetail ? (
            <Button size="sm" onClick={() => onOpenDetail(entry.slug)} aria-label={`Details for ${entry.name}`}>
              Details
            </Button>
          ) : null}
          {canManage && connectable ? (
            <Button size="sm" variant="primary" onClick={() => onConnect(entry)}>
              {connected.length > 0 ? "Connect another" : "Connect"}
            </Button>
          ) : null}
        </div>
      </footer>
    </li>
  );
}

/** One card in the catalog grid, builtin or synced. A builtin row carries its
 * curated `CatalogApp` and keeps the one-click Connect; a synced row's Connect
 * opens the detail sheet first, so nothing is dialled sight unseen. Nothing
 * here dials the server — the URL is only ever a form value. */
function CatalogCard({
  entry,
  builtin,
  connections,
  canManage,
  onConnect,
  onOpenConnection,
  onOpenDetail,
}: {
  entry: CatalogEntry;
  /** The curated row behind a builtin entry, when the page has it. */
  builtin?: CatalogApp;
  connections: ConnectionInfo[];
  canManage: boolean;
  onConnect: (entry: CatalogApp) => void;
  onOpenConnection: (connection: ConnectionInfo) => void;
  onOpenDetail?: (slug: string) => void;
}) {
  const connected = connectionsForApp(entry, connections);
  const stale = needsReauth(connected);
  const openDetail = onOpenDetail ? () => onOpenDetail(entry.slug) : undefined;
  const summary = builtin?.description || entry.summary;
  const docsUrl = builtin?.docs_url || entry.docs_url;
  const logoUrl = builtin?.logo_url ?? entry.logo_url;

  const builtinConnectable =
    builtin !== undefined && (Boolean(builtin.connector_type) || !builtin.stdio_only);
  const connect = builtin
    ? canManage && builtinConnectable
      ? () => onConnect(builtin)
      : undefined
    : canManage && entry.connectable && openDetail !== undefined
      ? openDetail
      : undefined;

  return (
    <li
      data-testid={entry.source === "builtin" ? `app-${entry.slug}` : `catalog-${entry.slug}`}
      className="flex flex-col gap-3 rounded-2xl border border-line bg-surface px-5 py-4 shadow-card"
    >
      <header className="flex items-center gap-3">
        <LogoTile name={entry.name} icon={entry.icon} logoUrl={logoUrl} size={40} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-medium text-ink">{entry.name}</h3>
          <div className="mt-1">
            <TrustBadge tier={entry.trust_tier} deprecated={entry.deprecated} />
          </div>
        </div>
        {stale.length > 0 ? (
          <Badge tone="warn">Reconnect needed</Badge>
        ) : connected.length > 0 ? (
          <Badge tone="ok">Connected</Badge>
        ) : entry.stdio_only ? (
          <Badge tone="neutral">Self-hosted</Badge>
        ) : null}
      </header>
      <p className={`line-clamp-2 text-sm leading-relaxed ${summary ? "text-dim" : "text-faint"}`}>
        {summary || "The index carries no description for this one."}
      </p>
      {/* No category here: it truncated badly next to the buttons, and the
        * rail plus the detail sheet already carry it. */}
      <footer className="mt-auto flex items-center justify-end gap-2 border-t border-line pt-3">
        <div className="flex shrink-0 items-center gap-2">
          {isSafeExternalUrl(docsUrl) ? (
            <a
              href={docsUrl}
              target="_blank"
              rel="noopener noreferrer nofollow ugc"
              className="inline-flex items-center gap-1 text-xs text-accent-strong hover:underline"
            >
              <ExternalLink size={12} aria-hidden /> Docs
            </a>
          ) : null}
          {connected.length > 0 ? (
            <Button
              size="sm"
              variant={stale.length > 0 ? "primary" : "outline"}
              onClick={() => onOpenConnection(stale[0] ?? connected[0])}
            >
              {stale.length > 0 ? "Reconnect" : "Manage"}
            </Button>
          ) : null}
          {openDetail && (builtin !== undefined || connect === undefined) ? (
            <Button size="sm" onClick={openDetail} aria-label={`Details for ${entry.name}`}>
              Details
            </Button>
          ) : null}
          {connect ? (
            <Button size="sm" variant="primary" onClick={connect}>
              {connected.length > 0 ? "Connect another" : "Connect"}
            </Button>
          ) : null}
        </div>
      </footer>
    </li>
  );
}

export interface AppLibraryProps {
  entries: CatalogApp[];
  connections: ConnectionInfo[];
  canManage: boolean;
  onConnect: (entry: CatalogApp) => void;
  onOpenConnection: (connection: ConnectionInfo) => void;
  /** Present only in catalog mode: the merged page the server returned. */
  catalogEntries?: CatalogEntry[];
  catalogTotal?: number;
  facets?: CatalogFacets;
  query?: string;
  /** Supplying this switches the whole component to server-side filtering. */
  onQueryChange?: (value: string) => void;
  activeFacets?: Partial<Record<FacetDimension, string>>;
  onFacetChange?: (dimension: string, value: string | undefined) => void;
  includeIndexed?: boolean;
  onIncludeIndexedChange?: (value: boolean) => void;
  loading?: boolean;
  loadError?: boolean;
  /** Retry for the error state; without it the error box is a dead end. */
  onRetry?: () => void;
  onLoadMore?: () => void;
  hasMore?: boolean;
  onOpenDetail?: (slug: string) => void;
}

/** The library as it has always been: local search, a category dropdown, and
 * the curated entries. Untouched by the catalog work on purpose. */
function CuratedLibrary({
  entries,
  connections,
  canManage,
  onConnect,
  onOpenConnection,
}: AppLibraryProps) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("All");
  const categories = catalogCategories(entries);
  const visible = filterCatalog(entries, query, category);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Input
          type="search"
          aria-label="Search apps"
          placeholder="Search apps — Notion, Stripe, Slack…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          className="sm:max-w-sm"
        />
        <Select
          aria-label="Category"
          value={category}
          onChange={(event) => setCategory(event.target.value)}
          className="sm:w-56"
        >
          {categories.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </Select>
        <span className="text-xs text-faint sm:ml-auto" data-testid="app-count">
          {visible.length} of {entries.length} apps
        </span>
      </div>
      {visible.length === 0 ? (
        <p className="rounded-2xl border border-dashed border-line-strong px-4 py-8 text-center text-sm text-dim">
          {EMPTY_MESSAGE}
        </p>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {visible.map((entry) => (
            <CuratedCard
              key={entry.slug}
              entry={entry}
              connections={connections}
              canManage={canManage}
              onConnect={onConnect}
              onOpenConnection={onOpenConnection}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

/** The labeled row that used to be a chip: community-indexed rows stay out of
 * the library until somebody deliberately asks for them. */
function UnreviewedSwitch({
  value,
  onChange,
}: {
  value: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={value}
      onClick={() => onChange(!value)}
      className={`flex w-full items-center justify-between gap-3 rounded-xl border border-line bg-surface px-3.5 py-2.5 text-left text-sm transition-colors hover:border-line-strong ${focusRing}`}
    >
      <span className="text-dim">Show unreviewed community apps</span>
      <span
        aria-hidden
        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
          value ? "bg-accent" : "bg-line-strong"
        }`}
      >
        <span
          className={`inline-block h-4 w-4 rounded-full bg-surface shadow transition-transform ${
            value ? "translate-x-[18px]" : "translate-x-0.5"
          }`}
        />
      </span>
    </button>
  );
}

/** The catalog-backed library: the search box, the category rail, and the
 * rows behind "More filters" all drive a request, and the grid renders
 * whatever page came back. */
function CatalogLibrary({
  entries,
  connections,
  canManage,
  onConnect,
  onOpenConnection,
  catalogEntries = [],
  catalogTotal = 0,
  facets,
  query = "",
  onQueryChange,
  activeFacets = {},
  onFacetChange,
  includeIndexed = false,
  onIncludeIndexedChange,
  loading = false,
  loadError = false,
  onRetry,
  onLoadMore,
  hasMore = false,
  onOpenDetail,
}: AppLibraryProps & { onQueryChange: (value: string) => void }) {
  const curated = new Map(entries.map((entry) => [entry.slug, entry]));

  const activeCategory = activeFacets.category ?? null;
  const categories: { value: string; label: string; count?: number }[] = (
    facets?.category ?? []
  ).map((bucket) => ({ value: bucket.value, label: bucket.label, count: bucket.count }));
  // An active category always stays on the rail, so it can never become
  // unclearable by narrowing itself out of the buckets.
  if (activeCategory !== null && !categories.some((item) => item.value === activeCategory)) {
    categories.push({ value: activeCategory, label: activeCategory });
  }

  return (
    <div className="space-y-4">
      <Input
        type="search"
        aria-label="Search apps"
        placeholder="Search apps — Notion, Stripe, Slack…"
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
      />

      <p className="text-sm text-dim">
        Looking for agent skills? They have their own library now —{" "}
        <Link href="/skills" className="text-accent-strong hover:underline">
          Browse skills
        </Link>
        .
      </p>

      {facets && onFacetChange ? (
        <CategoryRail
          categories={categories}
          active={activeCategory}
          onChange={(value) => onFacetChange("category", value ?? undefined)}
        />
      ) : null}

      {onFacetChange || onIncludeIndexedChange ? (
        <Disclosure label="More filters" openLabel="Hide filters">
          <div className="space-y-3">
            {facets && onFacetChange
              ? MORE_FILTER_ROWS.map((row) => (
                  <FacetChips
                    key={row.key}
                    label={row.label}
                    buckets={facets[row.key]}
                    active={activeFacets[row.key]}
                    onChange={(value) => onFacetChange(row.key, value)}
                  />
                ))
              : null}
            {onIncludeIndexedChange ? (
              <UnreviewedSwitch value={includeIndexed} onChange={onIncludeIndexedChange} />
            ) : null}
            {facets && onFacetChange ? (
              <Disclosure
                label="Advanced"
                openLabel="Hide advanced"
                // An active advanced filter must never hide from the person
                // who set it, so the disclosure starts open while one is on.
                defaultOpen={ADVANCED_FILTER_ROWS.some((row) => activeFacets[row.key] !== undefined)}
              >
                <div className="space-y-3">
                  {ADVANCED_FILTER_ROWS.map((row) => (
                    <FacetChips
                      key={row.key}
                      label={row.label}
                      // "Unknown" is not a transport or sign-in anybody
                      // filters for — it is the index admitting it has no
                      // idea. Hide the bucket; the entries themselves stay in
                      // every unfiltered view.
                      buckets={facets[row.key].filter((bucket) => bucket.value !== "unknown")}
                      active={activeFacets[row.key]}
                      onChange={(value) => onFacetChange(row.key, value)}
                    />
                  ))}
                </div>
              </Disclosure>
            ) : null}
          </div>
        </Disclosure>
      ) : null}

      {/* Left-aligned with the grid it counts — never pressed against the
        * layout's right edge, where it read as clipped. */}
      <p className="text-xs text-faint" data-testid="app-count">
        {catalogEntries.length} of {catalogTotal} apps
      </p>

      {loading ? (
        <div
          role="status"
          aria-label="Loading the library…"
          className="grid gap-3 md:grid-cols-2 xl:grid-cols-3"
        >
          {[0, 1, 2, 3, 4, 5].map((slot) => (
            <div key={slot} className="h-28 animate-pulse rounded-2xl bg-raised" />
          ))}
        </div>
      ) : loadError ? (
        <LoadError what="the app library" onRetry={onRetry} />
      ) : catalogEntries.length === 0 ? (
        <EmptyState
          icon={<Search size={22} aria-hidden />}
          title="No apps match"
          description="Try a different name, or use “Add a custom app” at the top of the page."
        />
      ) : (
        <>
          <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {catalogEntries.map((entry) => (
              <CatalogCard
                key={`${entry.kind}-${entry.slug}`}
                entry={entry}
                builtin={entry.source === "builtin" ? curated.get(entry.slug) : undefined}
                connections={connections}
                canManage={canManage}
                onConnect={onConnect}
                onOpenConnection={onOpenConnection}
                onOpenDetail={onOpenDetail}
              />
            ))}
          </ul>
          {hasMore && onLoadMore ? (
            <div className="flex justify-center pt-1">
              <Button onClick={onLoadMore}>Show more</Button>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

export function AppLibrary(props: AppLibraryProps) {
  const { onQueryChange } = props;
  if (onQueryChange === undefined) return <CuratedLibrary {...props} />;
  return <CatalogLibrary {...props} onQueryChange={onQueryChange} />;
}
