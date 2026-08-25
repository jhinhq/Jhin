"use client";

/**
 * The API reference.
 *
 * Rendered from `GET /api/v1/openapi.json` — the document this install
 * generates from its own routes — so it describes the API that is running,
 * not the API somebody last wrote a page about. Adding an endpoint publishes
 * it here; changing a scope changes the badge here.
 *
 * Why not link to FastAPI's `/docs`: `EXPOSE_API_DOCS` follows `APP_ENV`, so
 * Swagger, ReDoc and the anonymous `/openapi.json` are all switched off in
 * staging and production — exactly where a self-hoster's integrators need a
 * reference. The API also sends `X-Frame-Options: DENY`, so embedding it is
 * out, and the app's `default-src 'self'` CSP rules out a CDN renderer.
 * Rendering the document here costs one page and answers all three.
 */

import { BookOpen, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { EndpointCard, Markdown } from "@/components/api-reference";
import { PageBody, PageHeader } from "@/components/app-shell";
import { Card, EmptyState, ErrorNote, Input, Spinner, focusRing } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { groupByTag, matchesQuery, useApiOrigin, useApiSpec } from "@/lib/openapi";
import { useWorkspace } from "@/lib/workspace-context";

export default function ApiDocsPage() {
  const { workspace } = useWorkspace();
  const spec = useApiSpec();
  const [query, setQuery] = useState("");
  const origin = useApiOrigin();

  const groups = useMemo(() => (spec.data ? groupByTag(spec.data) : []), [spec.data]);
  const filtered = useMemo(
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

  return (
    <>
      <PageHeader
        eyebrow="Advanced"
        title="API reference"
        description="Generated from this install's own OpenAPI document, so it always describes the API that is running."
      />
      <PageBody className="max-w-5xl space-y-6">
        {spec.isPending ? (
          <Spinner label="Reading the API description" />
        ) : spec.error ? (
          <ErrorNote
            message={
              spec.error instanceof ApiError
                ? spec.error.detail
                : "The API description could not be loaded."
            }
          />
        ) : spec.data ? (
          <>
            <Card as="section">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="font-display text-base font-semibold">
                  {spec.data.info.title} API{" "}
                  <span className="font-mono text-sm font-normal text-faint">
                    {spec.data.info["x-api-version"] ?? "v1"}
                  </span>
                </h2>
                <p className="text-xs text-faint" data-testid="spec-version">
                  {total} endpoints · app {spec.data.info.version}
                  {spec.data.info.license ? ` · ${spec.data.info.license.name}` : ""}
                </p>
              </div>
              {spec.data.info.description ? (
                <Markdown source={spec.data.info.description} className="mt-3" />
              ) : null}
            </Card>

            <div className="sticky top-0 z-10 -mx-1 bg-bg/95 px-1 py-2 backdrop-blur">
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
                  placeholder="Search by path, name, or scope — try “agents” or “tasks:write”"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </label>
              {query ? (
                <p className="mt-1 px-1 text-xs text-faint" data-testid="result-count">
                  {shown} of {total} endpoints
                </p>
              ) : null}
            </div>

            {filtered.length === 0 ? (
              <EmptyState
                icon={<BookOpen size={20} aria-hidden />}
                title="Nothing matches"
                description="No endpoint has that path, name, or scope."
              />
            ) : (
              <>
                <nav aria-label="Sections" className="flex flex-wrap gap-1.5">
                  {filtered.map((group) => (
                    <a
                      key={group.name}
                      href={`#tag-${group.name}`}
                      className={`rounded-lg border border-line px-2 py-1 text-xs text-dim transition-colors hover:border-accent hover:text-ink ${focusRing}`}
                    >
                      {group.name}
                      <span className="ml-1 text-faint">{group.endpoints.length}</span>
                    </a>
                  ))}
                </nav>

                {filtered.map((group) => (
                  <section
                    key={group.name}
                    id={`tag-${group.name}`}
                    className="scroll-mt-4 space-y-3"
                    data-testid="tag-section"
                  >
                    <div className="pt-2">
                      <h2 className="font-display text-lg font-semibold text-ink">{group.name}</h2>
                      {group.description ? (
                        <p className="mt-1 max-w-3xl text-sm text-dim">{group.description}</p>
                      ) : null}
                    </div>
                    {group.endpoints.map((endpoint) => (
                      <EndpointCard
                        key={endpoint.id}
                        endpoint={endpoint}
                        spec={spec.data}
                        origin={origin}
                        workspaceId={workspace.workspace_id}
                      />
                    ))}
                  </section>
                ))}
              </>
            )}
          </>
        ) : null}
      </PageBody>
    </>
  );
}
