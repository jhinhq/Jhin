"use client";

/**
 * The API reference.
 *
 * Rendered from `GET /api/v1/openapi.json` — the document this install
 * generates from its own routes — so it describes the API that is running, not
 * the API somebody last wrote a page about. Adding an endpoint publishes it
 * here; changing a scope changes the badge here.
 *
 * The presentation is built for navigation, not scrolling: a persistent table
 * of contents down the side (with scroll-spy), a search that narrows both the
 * nav and the list at once, and operations that stay collapsed until opened so
 * the default view is a scannable index of 183 endpoints rather than a wall of
 * tables. The data source is untouched — this is a reader of the live document.
 *
 * Why not link to FastAPI's `/docs`: `EXPOSE_API_DOCS` follows `APP_ENV`, so
 * Swagger, ReDoc and the anonymous `/openapi.json` are all switched off in
 * staging and production — exactly where a self-hoster's integrators need a
 * reference. The API also sends `X-Frame-Options: DENY`, so embedding it is
 * out, and the app's `default-src 'self'` CSP rules out a CDN renderer.
 */

import { ArrowUp, BookOpen, ChevronDown, ListTree, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { METHOD_TONE } from "@/components/api-reference";
import { Markdown } from "@/components/markdown";
import { DocsNav } from "@/components/api-docs/docs-nav";
import { OperationRow } from "@/components/api-docs/operation-row";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Card, EmptyState, ErrorNote, Input, Spinner, focusRing } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { groupByTag, matchesQuery, useApiOrigin, useApiSpec } from "@/lib/openapi";
import type { TagGroup } from "@/lib/openapi";
import { useWorkspace } from "@/lib/workspace-context";

/** The methods the reference shows, in the order the legend reads them. */
const LEGEND_METHODS = ["get", "post", "patch", "delete"] as const;

function MethodLegend() {
  return (
    <div className="flex flex-wrap items-center gap-1.5" aria-hidden>
      {LEGEND_METHODS.map((method) => (
        <span
          key={method}
          className={`rounded-md px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase ${METHOD_TONE[method]}`}
        >
          {method}
        </span>
      ))}
    </div>
  );
}

export default function ApiDocsPage() {
  const { workspace } = useWorkspace();
  const spec = useApiSpec();
  const origin = useApiOrigin();

  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [showTop, setShowTop] = useState(false);

  const data = spec.data;
  const groups = useMemo(() => (data ? groupByTag(data) : []), [data]);
  const filtered: TagGroup[] = useMemo(
    () =>
      groups
        .map((group) => ({
          ...group,
          endpoints: group.endpoints.filter((endpoint) => matchesQuery(endpoint, query)),
        }))
        .filter((group) => group.endpoints.length > 0),
    [groups, query],
  );

  const total = groups.reduce((count, group) => count + group.endpoints.length, 0);
  const shown = filtered.reduce((count, group) => count + group.endpoints.length, 0);

  const toggleOne = useCallback((id: string, next: boolean) => {
    setExpanded((prev) => {
      const draft = new Set(prev);
      if (next) draft.add(id);
      else draft.delete(id);
      return draft;
    });
  }, []);

  const setGroupExpanded = useCallback((group: TagGroup, next: boolean) => {
    setExpanded((prev) => {
      const draft = new Set(prev);
      for (const endpoint of group.endpoints) {
        if (next) draft.add(endpoint.id);
        else draft.delete(endpoint.id);
      }
      return draft;
    });
  }, []);

  // Deep links: a `#endpoint-id` in the URL (a shared link, or the browser back
  // button returning to one) opens that operation and scrolls it into view.
  useEffect(() => {
    if (!data) return;
    const openFromHash = () => {
      const id = decodeURIComponent(window.location.hash.slice(1));
      if (!id) return;
      const isEndpoint = groups.some((group) => group.endpoints.some((e) => e.id === id));
      if (!isEndpoint) return;
      setExpanded((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));
      requestAnimationFrame(() => {
        document.getElementById(id)?.scrollIntoView({ block: "start" });
      });
    };
    openFromHash();
    window.addEventListener("hashchange", openFromHash);
    return () => window.removeEventListener("hashchange", openFromHash);
  }, [data, groups]);

  // Scroll-spy: light up the tag whose section is nearest the top of the
  // viewport, so the reader always knows where they are in the document.
  useEffect(() => {
    if (filtered.length === 0 || typeof IntersectionObserver === "undefined") return;
    const sections = Array.from(
      document.querySelectorAll<HTMLElement>("section[data-tag]"),
    );
    if (sections.length === 0) return;
    const tops = new Map<string, number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const tag = entry.target.getAttribute("data-tag");
          if (!tag) continue;
          if (entry.isIntersecting) tops.set(tag, entry.boundingClientRect.top);
          else tops.delete(tag);
        }
        if (tops.size > 0) {
          const nearest = [...tops.entries()].sort((a, b) => a[1] - b[1])[0][0];
          setActiveTag(nearest);
        }
      },
      { rootMargin: "-88px 0px -55% 0px", threshold: 0 },
    );
    sections.forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, [filtered]);

  // A "back to top" affordance once the reader has scrolled past the fold.
  useEffect(() => {
    const onScroll = () => setShowTop(window.scrollY > 640);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const searchField = (idSuffix: string) => (
    <label className="relative block">
      <span className="sr-only">Search endpoints</span>
      <Search
        size={15}
        aria-hidden
        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
      />
      <Input
        className="pl-9"
        value={query}
        data-testid={`search-${idSuffix}`}
        placeholder="Search path, name, or scope"
        onChange={(event) => setQuery(event.target.value)}
      />
    </label>
  );

  return (
    <>
      <PageHeader
        eyebrow="Advanced"
        title="API reference"
        description="Generated from this install's own OpenAPI document, so it always describes the API that is running."
        compact
      />
      <PageBody wide className="!py-0">
        {spec.isPending ? (
          <div className="py-8">
            <Spinner label="Reading the API description" />
          </div>
        ) : spec.error ? (
          <div className="py-8">
            <ErrorNote
              message={
                spec.error instanceof ApiError
                  ? spec.error.detail
                  : "The API description could not be loaded."
              }
            />
          </div>
        ) : data ? (
          <div className="lg:grid lg:grid-cols-[240px_minmax(0,1fr)] lg:gap-8">
            {/* Persistent table of contents (desktop) */}
            <aside className="hidden lg:block">
              <div className="sticky top-20 flex max-h-[calc(100vh-6rem)] flex-col gap-3 py-6">
                {searchField("desktop")}
                {query ? (
                  <p className="px-1 text-xs text-faint" data-testid="result-count">
                    {shown} of {total} endpoints
                  </p>
                ) : (
                  <MethodLegend />
                )}
                <nav
                  aria-label="Endpoint groups"
                  className="-mr-1 min-h-0 flex-1 overflow-y-auto pr-1"
                >
                  <DocsNav groups={filtered} activeTag={activeTag} />
                </nav>
              </div>
            </aside>

            {/* Content */}
            <div className="min-w-0 py-6">
              {/* Search + section jump (mobile / tablet) */}
              <div className="mb-4 space-y-2 lg:hidden">
                {searchField("mobile")}
                <button
                  type="button"
                  aria-expanded={drawerOpen}
                  aria-controls="docs-drawer"
                  onClick={() => setDrawerOpen((open) => !open)}
                  className={`flex w-full items-center justify-between gap-2 rounded-xl border border-line bg-surface px-3 py-2 text-sm text-dim ${focusRing}`}
                >
                  <span className="inline-flex items-center gap-2">
                    <ListTree size={15} aria-hidden />
                    Jump to section
                    {query ? (
                      <span className="text-xs text-faint">
                        ({shown} of {total})
                      </span>
                    ) : null}
                  </span>
                  <ChevronDown
                    size={16}
                    aria-hidden
                    className={`transition-transform ${drawerOpen ? "rotate-180" : ""}`}
                  />
                </button>
                {drawerOpen ? (
                  <div
                    id="docs-drawer"
                    data-testid="docs-drawer"
                    className="max-h-[50vh] overflow-y-auto rounded-xl border border-line bg-surface p-2"
                  >
                    <DocsNav
                      groups={filtered}
                      activeTag={activeTag}
                      onNavigate={() => setDrawerOpen(false)}
                    />
                  </div>
                ) : null}
              </div>

              <Card as="section" className="mb-6">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h2 className="font-display text-base font-semibold">
                    {data.info.title} API{" "}
                    <span className="font-mono text-sm font-normal text-faint">
                      {data.info["x-api-version"] ?? "v1"}
                    </span>
                  </h2>
                  <p className="text-xs text-faint" data-testid="spec-version">
                    {total} endpoints · app {data.info.version}
                    {data.info.license ? ` · ${data.info.license.name}` : ""}
                  </p>
                </div>
                {data.info.description ? (
                  <Markdown source={data.info.description} className="mt-3" />
                ) : null}
              </Card>

              {filtered.length === 0 ? (
                <EmptyState
                  icon={<BookOpen size={20} aria-hidden />}
                  title="Nothing matches"
                  description="No endpoint has that path, name, or scope."
                />
              ) : (
                <div className="space-y-8">
                  {filtered.map((group) => {
                    const allOpen = group.endpoints.every((e) => expanded.has(e.id));
                    return (
                      <section
                        key={group.name}
                        id={`tag-${group.name}`}
                        data-tag={group.name}
                        data-testid="tag-section"
                        className="scroll-mt-[7.75rem] md:scroll-mt-20"
                      >
                        <div className="sticky top-[6.75rem] z-[5] -mx-1 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-line bg-bg/95 px-1 py-2 backdrop-blur md:top-16">
                          <div className="flex items-baseline gap-2">
                            <h2 className="font-display text-lg font-semibold text-ink">
                              {group.name}
                            </h2>
                            <span className="text-xs text-faint">
                              {group.endpoints.length}
                            </span>
                          </div>
                          <button
                            type="button"
                            onClick={() => setGroupExpanded(group, !allOpen)}
                            className={`rounded-md px-1.5 py-0.5 text-xs text-dim hover:bg-hover hover:text-ink ${focusRing}`}
                          >
                            {allOpen ? "Collapse all" : "Expand all"}
                          </button>
                        </div>
                        {group.description ? (
                          <p className="mb-2 mt-2 max-w-3xl text-sm text-dim">{group.description}</p>
                        ) : null}
                        <div className="mt-2 space-y-2">
                          {group.endpoints.map((endpoint) => (
                            <OperationRow
                              key={endpoint.id}
                              endpoint={endpoint}
                              spec={data}
                              origin={origin}
                              workspaceId={workspace.workspace_id}
                              expanded={expanded.has(endpoint.id)}
                              onToggle={toggleOne}
                            />
                          ))}
                        </div>
                      </section>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        ) : null}
      </PageBody>

      {showTop ? (
        <button
          type="button"
          onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
          aria-label="Back to top"
          className={`fixed bottom-20 right-4 z-30 inline-flex h-10 w-10 items-center justify-center rounded-full border border-line bg-surface text-dim shadow-card hover:text-ink md:bottom-6 ${focusRing}`}
        >
          <ArrowUp size={18} aria-hidden />
        </button>
      ) : null}
    </>
  );
}
