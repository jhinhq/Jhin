"use client";

/** Apps — the single place connections live. The top level stays friendly (a
 * searchable library plus what is already connected); everything the old
 * Advanced → Connectors route offered (verify, rotate credentials, webhook
 * setup, discovered tools with risk overrides, agent access, enable/disable/
 * delete) opens in the per-connection drawer, with the operational controls
 * behind an "Advanced settings" disclosure. */

import { Plus } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { AppLibrary, type FacetDimension } from "@/components/app-library";
import { PageBody, PageHeader } from "@/components/app-shell";
import { LogoTile } from "@/components/catalog/logo-tile";
import { CatalogEntryDialog } from "@/components/catalog-entry-dialog";
import { Disclosure, LoadError, StatusPill } from "@/components/company/bits";
import { ConnectPanel } from "@/components/connect/connect-panel";
import { OAuthLandingCard } from "@/components/connect/oauth-landing";
import { ReconnectBanner, ReconnectButton } from "@/components/connect/reconnect-banner";
import { ConnectionDetailDialog, WebhookSecretDialog } from "@/components/connection-detail";
import { ConnectorsGallery } from "@/components/connectors-gallery";
import { Button, EmptyState, ErrorNote, Spinner } from "@/components/ui";
import { connectTarget, type ConnectTarget } from "@/lib/apps";
import { formatDateTime } from "@/lib/format";
import {
  consumeReturnRoute,
  type OAuthLandingCopy,
  oauthLanding,
  readGitHubAppLanding,
  readLandingConnector,
} from "@/lib/oauth";
import {
  useAppCatalog,
  useCatalogFacets,
  useCatalogSearch,
  useCatalogVersion,
  useConnections,
  useConnectors,
  useInvalidateConnections,
  useMarkConnectionWebhookConfigured,
} from "@/lib/hooks";
import type {
  CatalogApp,
  CatalogKind,
  CatalogTrustTier,
  ConnectionCreated,
  ConnectionInfo,
  ConnectorInfo,
  WebhookSetup,
} from "@/lib/types";
import { useWorkspace } from "@/lib/workspace-context";
import type { ConnectionPrefill } from "@/components/connection-create-dialog";

function connectionStatus(connection: ConnectionInfo): { label: string; tone: "ok" | "warn" | "neutral" | "danger" | "accent" } {
  if (connection.status === "active") return { label: "Connected", tone: "ok" };
  if (connection.status === "error") return { label: "Needs attention", tone: "danger" };
  // A lapsed sign-in is not a broken connection: the setup is intact and the
  // fix is one click, so it reads as a nudge rather than a failure.
  if (connection.status === "needs_reauth") return { label: "Reconnect needed", tone: "warn" };
  return { label: "Turned off", tone: "neutral" };
}

/** The `?connection=` the OAuth callback sends us back with is a connection's
 * public id and nothing else. Anything that is not 32 hex characters was not
 * written by our API and is ignored rather than looked up. */
const PUBLIC_ID_PATTERN = /^[0-9a-f]{32}$/;

/** What an OAuth round trip left in the address bar on the way back. */
interface OAuthLanding {
  copy: OAuthLandingCopy | null;
  publicId: string | null;
  /** The connector the flow concerned, so the card can offer the retry. */
  connectorType: string | null;
  /** The GitHub App handshake's flag, or GitHub's own install return. */
  githubApp: ReturnType<typeof readGitHubAppLanding>;
}

const NO_LANDING: OAuthLanding = {
  copy: null,
  publicId: null,
  connectorType: null,
  githubApp: null,
};

/**
 * Read the callback's own parameters, once.
 *
 * All are written server-side or matched against a closed set: `?connection=`
 * is a public id built from the row that was just created, `?oauth_error=`
 * and `?github_app=` are constants, `?app=` is a connector type re-matched
 * against the same pattern the API validated it with, and `?setup_action=` is
 * read only for its shape. Nothing a provider wrote reaches this page, and
 * the id is checked against its shape before it is ever used to look
 * anything up.
 */
function readOAuthLanding(): OAuthLanding {
  if (typeof window === "undefined") return NO_LANDING;
  const params = new URLSearchParams(window.location.search);
  const connected = params.get("connection");
  return {
    copy: oauthLanding(params.get("oauth_error")),
    publicId: connected !== null && PUBLIC_ID_PATTERN.test(connected) ? connected : null,
    connectorType: readLandingConnector(params),
    githubApp: readGitHubAppLanding(params),
  };
}

/** Put the cursor in the library's search box, for "Choose an app". */
function focusLibrarySearch(): void {
  const box = document.querySelector<HTMLInputElement>('input[aria-label="Search apps"]');
  if (box === null) return;
  if (typeof box.scrollIntoView === "function") {
    box.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  box.focus();
}

const GITHUB_APP_STATUS: Record<"created" | "installed", string> = {
  created: "Your GitHub App was created. Sign in with it to connect GitHub.",
  installed: "Now connect GitHub to sign in with the app you installed.",
};
const GITHUB_APP_FAILED =
  "GitHub did not finish creating the app. If it did create one, open it on github.com, generate a client secret, and paste both under Apps → Connect GitHub; otherwise start again from Connect GitHub.";

interface CreateTarget {
  connector: ConnectorInfo;
  prefill?: ConnectionPrefill;
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

/** One screenful of the library, and how much more "Show more" asks for. */
const PAGE_STEP = 40;
/**
 * The largest `limit` the catalog endpoint accepts (`Query(ge=1, le=100)` on
 * `list_catalog_entries`). Growing the page past it is not a bigger page, it
 * is a 422: that sets `catalogSearch.isError`, which flips `catalogMode` off
 * and drops the whole gallery back to the curated-only library under the
 * "wider app index is unavailable" banner. Clamping here keeps the last
 * "Show more" honest, and `hasMore` stops offering one at the ceiling.
 */
const MAX_PAGE_SIZE = 100;

/** How many Connected cards show before the expander — one row at the widest
 * breakpoint. The Library below is the page's point; it must stay reachable. */
const CONNECTED_PREVIEW = 3;

export default function AppsPage() {
  const { workspace, can } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const isAdmin = can("admin");
  const connections = useConnections(workspaceId, isAdmin);
  const connectors = useConnectors();
  const catalog = useAppCatalog();
  const invalidate = useInvalidateConnections(workspaceId);
  const markWebhookConfigured = useMarkConnectionWebhookConfigured(workspaceId);

  const [createFor, setCreateFor] = useState<CreateTarget | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  /** The connection returned by POST, so the drawer opens before the list refetches. */
  const [created, setCreated] = useState<ConnectionInfo | null>(null);
  const [webhookOnce, setWebhookOnce] = useState<{
    connection: ConnectionInfo;
    webhook: WebhookSetup;
  } | null>(null);
  const [unsupported, setUnsupported] = useState<string | null>(null);
  /** The Connected grid is capped so the Library stays near the first
   * screenful — a workspace with many connections gets a row and an
   * expander, never a wall that buries the part people came for. */
  const [showAllConnections, setShowAllConnections] = useState(false);
  /** The OAuth callback's own parameters, read once on the way in. */
  const [landing, setLanding] = useState<OAuthLanding>(readOAuthLanding);
  /** "Not now" on the recovery card. The one event that ends it. */
  const [landingDismissed, setLandingDismissed] = useState(false);

  /**
   * Tidy up after an OAuth round trip.
   *
   * Scrub the callback's parameters out of the address bar so a refresh or a
   * shared link cannot replay the moment, then put the person back where they
   * started if they began somewhere other than this page. Both are the effect
   * talking to the browser, not to React: the landing itself is state read
   * once at mount and derived from thereafter.
   */
  useEffect(() => {
    if (landing.copy === null && landing.publicId === null && landing.githubApp === null) return;
    window.history.replaceState(null, "", window.location.pathname);
    const back = consumeReturnRoute();
    if (back !== null && back !== window.location.pathname) window.location.assign(back);
  }, [landing]);

  /**
   * A GitHub App was just created, or just installed: the next step is to
   * sign in with it, so Connect GitHub is open from the moment the connector
   * list can name the connector. Derived, not set: the person closing it is
   * the one event that ends it, and a refetch of the list cannot reopen it.
   */
  const [githubConnectDismissed, setGitHubConnectDismissed] = useState(false);
  const githubConnector = connectors.data?.find(
    (connector) => connector.connector_type === "github",
  );
  const autoOpenedGitHub: CreateTarget | null =
    !githubConnectDismissed &&
    (landing.githubApp === "created" || landing.githubApp === "installed") &&
    githubConnector
      ? { connector: githubConnector, prefill: { name: "GitHub", config: {} } }
      : null;
  const connectTargetOpen = createFor ?? autoOpenedGitHub;
  const closeConnect = () => {
    setCreateFor(null);
    setGitHubConnectDismissed(true);
  };

  const githubAppStatus =
    landing.githubApp === "created" || landing.githubApp === "installed"
      ? GITHUB_APP_STATUS[landing.githubApp]
      : null;
  const githubAppFailure = landing.githubApp === "failed" ? GITHUB_APP_FAILED : null;

  // --- The synced catalog behind the curated library ---
  const [catalogQuery, setCatalogQuery] = useState("");
  const debouncedQuery = useDebounced(catalogQuery, 300);
  const [facetState, setFacetState] = useState<Partial<Record<FacetDimension, string>>>({});
  const [includeIndexed, setIncludeIndexed] = useState(false);
  const [pageSize, setPageSize] = useState(PAGE_STEP);
  const [detailSlug, setDetailSlug] = useState<string | null>(null);

  // Facet values only ever come back out of the server's own buckets, so the
  // narrowing here restates what the API already guaranteed. `kind` is pinned:
  // skills have their own library on /skills now, so this page only ever asks
  // the catalog for MCP servers.
  const catalogFilters = {
    q: debouncedQuery || undefined,
    kind: "mcp" as CatalogKind,
    category: facetState.category,
    trust_tier: facetState.trust_tier as CatalogTrustTier | undefined,
    transport: facetState.transport,
    auth_hint: facetState.auth_hint,
    include_indexed: includeIndexed,
  };
  const catalogSearch = useCatalogSearch({ ...catalogFilters, limit: pageSize, offset: 0 });
  const catalogFacets = useCatalogFacets(catalogFilters);
  const catalogVersion = useCatalogVersion();

  /**
   * Server-side search runs only while the catalog endpoint answers. When it
   * does not — an API that predates the catalog, a sync that never landed —
   * the library falls back to the curated entries filtered in the browser.
   * A missing index must never cost somebody the apps they already had.
   */
  const catalogMode = !catalogSearch.isError;
  const catalogItems = catalogSearch.data?.items ?? [];
  const catalogTotal = catalogSearch.data?.total ?? 0;

  const resetPaging = () => setPageSize(PAGE_STEP);
  const onQueryChange = (value: string) => {
    setCatalogQuery(value);
    resetPaging();
  };
  const onFacetChange = (dimension: string, value: string | undefined) => {
    setFacetState((current) => ({ ...current, [dimension]: value }));
    resetPaging();
  };
  const onIncludeIndexedChange = (value: boolean) => {
    setIncludeIndexed(value);
    resetPaging();
  };

  const catalogProps = catalogMode
    ? {
        catalogEntries: catalogItems,
        catalogTotal,
        facets: catalogFacets.data,
        query: catalogQuery,
        onQueryChange,
        activeFacets: facetState,
        onFacetChange,
        includeIndexed,
        onIncludeIndexedChange,
        loading: catalogSearch.isPending,
        loadError: catalogSearch.isError,
        onRetry: () => void catalogSearch.refetch(),
        onLoadMore: () => setPageSize((size) => Math.min(size + PAGE_STEP, MAX_PAGE_SIZE)),
        hasMore: catalogItems.length < catalogTotal && pageSize < MAX_PAGE_SIZE,
        onOpenDetail: (slug: string) => setDetailSlug(slug),
      }
    : {};

  const connectorList = connectors.data ?? [];
  const connectionList = connections.data ?? [];
  const connectorFor = (type: string) => connectorList.find((connector) => connector.connector_type === type);

  // Logos for the Connected cards, keyed by catalog slug and by native
  // connector type so both halves of a connection can find theirs. Curated
  // entries win; anything the lookup misses falls down LogoTile's own chain.
  const logoBySlug = new Map<string, string>();
  for (const item of catalogItems) {
    if (item.logo_url) logoBySlug.set(item.slug, item.logo_url);
  }
  for (const app of catalog.data ?? []) {
    if (!app.logo_url) continue;
    logoBySlug.set(app.slug, app.logo_url);
    if (app.connector_type) logoBySlug.set(app.connector_type, app.logo_url);
  }
  const logoForConnection = (connection: ConnectionInfo): string | null => {
    const key =
      connection.connector_type === "mcp"
        ? String(connection.config_json.server_slug ?? "")
        : connection.connector_type;
    return logoBySlug.get(key) ?? null;
  };

  /**
   * The connection an OAuth callback named, once the list has caught up with
   * it. Derived rather than stored: the drawer opens by itself when the
   * refetch lands, and there is no moment where the page has an id it has not
   * acted on yet.
   */
  const landedConnection =
    landing.publicId === null
      ? undefined
      : connectionList.find((connection) => connection.public_id === landing.publicId);

  /**
   * The drawer celebrates a connection; the card recovers from a failure.
   * Both can name the same connection — a reconnect that was refused carries
   * `?connection=` beside its flag so the card can offer Reconnect — so the
   * drawer opens only when the round trip actually succeeded.
   */
  const drawerConnection = landing.copy === null ? landedConnection : undefined;

  const detail =
    connectionList.find((connection) => connection.id === detailId) ??
    (created && created.id === detailId ? created : null) ??
    drawerConnection ??
    null;
  const justConnected =
    (created !== null && created.id === detailId) ||
    (detailId === null && drawerConnection !== undefined);

  const closeDetail = () => {
    setDetailId(null);
    setCreated(null);
    setLanding((current) => ({ ...current, publicId: null }));
  };

  /** Both create paths — the library card and the catalog sheet — land here. */
  const handleCreated = (result: ConnectionCreated) => {
    invalidate();
    closeConnect();
    setDetailSlug(null);
    if (result.webhook) {
      // The one-time secret is the moment for webhook connectors; the drawer
      // is one click away on the new card.
      setWebhookOnce({ connection: result.connection, webhook: result.webhook });
    } else {
      setCreated(result.connection);
      setDetailId(result.connection.id);
    }
  };

  const openConnection = (connection: ConnectionInfo) => {
    setCreated(null);
    setDetailId(connection.id);
  };

  const onConnect = (entry: CatalogApp) => {
    const resolved: ConnectTarget = connectTarget(entry, connectorList);
    if (resolved.kind === "unsupported") {
      setUnsupported(resolved.reason);
      return;
    }
    setUnsupported(null);
    setCreateFor({
      connector: resolved.connector,
      prefill:
        resolved.kind === "mcp"
          ? {
              name: resolved.prefill.name,
              authType: resolved.prefill.authType,
              config: resolved.prefill.config,
              hint: resolved.prefill.hint,
            }
          : {
              name: resolved.prefill.name,
              config: resolved.prefill.config,
              hint: resolved.prefill.hint,
            },
    });
  };

  const mcpConnector = connectorFor("mcp");
  const addApp =
    isAdmin && mcpConnector ? (
      <Button variant="primary" onClick={() => setCreateFor({ connector: mcpConnector })}>
        <Plus size={14} /> Add a custom app
      </Button>
    ) : null;

  return (
    <>
      <PageHeader
        title="Apps"
        description="Connect the apps your agents work with — GitHub, Notion, Slack, Stripe, and any app with an MCP server"
        actions={addApp}
      />
      <PageBody className="space-y-8">
        {!isAdmin ? (
          <EmptyState
            title="Apps are managed by admins"
            description="Ask a workspace admin to connect the apps your agents need. You can still see what each agent can use on its profile."
          />
        ) : (
          <>
            <section className="space-y-3">
              <h2 className="font-display text-base font-semibold tracking-tight text-ink">Connected</h2>
              <ErrorNote message={githubAppFailure} />
              {landing.copy !== null && !landingDismissed ? (
                <OAuthLandingCard
                  copy={landing.copy}
                  connector={
                    landing.connectorType !== null
                      ? connectorFor(landing.connectorType) ?? null
                      : null
                  }
                  connection={landedConnection ?? null}
                  workspaceId={workspaceId}
                  onRetry={(connector) => setCreateFor({ connector })}
                  onBrowse={focusLibrarySearch}
                  onDismiss={() => setLandingDismissed(true)}
                />
              ) : null}
              {githubAppStatus ? (
                <p
                  role="status"
                  data-testid="github-app-banner"
                  className="rounded-2xl border border-ok/30 bg-ok-soft px-4 py-3 text-sm text-ok"
                >
                  {githubAppStatus}
                </p>
              ) : null}
              <ReconnectBanner workspaceId={workspaceId} connections={connectionList} />
              {connections.isPending ? (
                <Spinner label="Loading apps…" />
              ) : connections.isError ? (
                <LoadError what="your connected apps" onRetry={() => void connections.refetch()} />
              ) : connectionList.length === 0 ? (
                <p className="rounded-2xl border border-dashed border-line-strong px-4 py-5 text-sm text-dim">
                  Nothing connected yet. Pick an app from the library below.
                </p>
              ) : (
                <>
                <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {(showAllConnections ? connectionList : connectionList.slice(0, CONNECTED_PREVIEW)).map((connection) => {
                    const connector = connectorFor(connection.connector_type);
                    const status = connectionStatus(connection);
                    const subtitle =
                      connection.connector_type === "mcp"
                        ? `MCP server · ${String(connection.config_json.server_slug ?? "")}`
                        : connector?.display_name ?? connection.connector_type;
                    return (
                      <li
                        key={connection.id}
                        data-testid={`connection-${connection.name}`}
                        className="flex flex-col gap-3 rounded-2xl border border-line bg-surface p-5"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex min-w-0 items-center gap-2.5">
                            <LogoTile
                              name={connection.name}
                              icon={connector?.icon ?? connection.connector_type}
                              logoUrl={logoForConnection(connection)}
                              size={36}
                            />
                            <div className="min-w-0">
                              <h3 className="truncate font-display text-base font-semibold tracking-tight">{connection.name}</h3>
                              <p className="truncate text-xs text-dim">{subtitle}</p>
                            </div>
                          </div>
                          <StatusPill status={status} className="shrink-0" />
                        </div>
                        {connection.status === "error" && connection.last_error ? (
                          <p className="rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
                            {connection.last_error}. Open it to re-check or replace the credentials.
                          </p>
                        ) : connection.status === "needs_reauth" ? (
                          <p className="rounded-xl border border-warn/30 bg-warn-soft px-3 py-2 text-[13px] text-ink">
                            Its sign-in lapsed. Reconnect it above — the setup and every grant stay
                            exactly as they are.
                          </p>
                        ) : null}
                        <div className="mt-auto flex flex-wrap items-center justify-between gap-2 pt-1 text-xs text-faint">
                          <span>
                            {connection.last_verified_at
                              ? `Checked ${formatDateTime(connection.last_verified_at)}`
                              : "Not checked yet"}
                          </span>
                          <span className="flex items-center gap-2">
                            {connection.status === "needs_reauth" ? (
                              <ReconnectButton
                                workspaceId={workspaceId}
                                connection={connection}
                              />
                            ) : null}
                            <Button
                              size="sm"
                              data-testid={`manage-${connection.name}`}
                              onClick={() => openConnection(connection)}
                            >
                              Manage
                            </Button>
                          </span>
                        </div>
                      </li>
                    );
                  })}
                </ul>
                {connectionList.length > CONNECTED_PREVIEW ? (
                  <div className="mt-3">
                    <Button size="sm" onClick={() => setShowAllConnections((value) => !value)}>
                      {showAllConnections
                        ? "Show fewer"
                        : `Show all ${connectionList.length} connections`}
                    </Button>
                  </div>
                ) : null}
                </>
              )}
            </section>

            <section>
              <h2 className="mb-1 font-display text-base font-semibold tracking-tight text-ink">Library</h2>
              <p className="mb-3 text-sm text-dim">
                Everything here is connect-on-request — an agent only gets access you grant.
              </p>
              <ErrorNote message={unsupported} />
              {catalog.isPending || connectors.isPending ? (
                <Spinner label="Loading the library…" />
              ) : catalog.isError || !catalog.data ? (
                <LoadError what="the app library" onRetry={() => void catalog.refetch()} />
              ) : (
                <AppLibrary
                  entries={catalog.data}
                  connections={connectionList}
                  canManage={isAdmin}
                  onConnect={onConnect}
                  onOpenConnection={openConnection}
                  {...catalogProps}
                />
              )}
              {catalogMode && catalogVersion.data ? (
                <p className="mt-3 text-[13px] text-faint">
                  {`Indexed ${catalogVersion.data.entry_count.toLocaleString()} apps and skills · ${catalogVersion.data.release_tag}`}
                </p>
              ) : !catalogMode ? (
                <p className="mt-3 text-[13px] text-faint">
                  The wider app index is unavailable right now — showing the built-in apps.
                </p>
              ) : null}
            </section>

            <section>
              <Disclosure
                label="Connect by service type instead"
                openLabel="Hide service types"
              >
                <div className="space-y-3">
                  <p className="text-sm text-dim">
                    The built-in connectors, including the ones with no library entry — a command-line
                    sandbox, plain HTTP, web search, and any MCP server.
                  </p>
                  {connectors.isPending ? (
                    <Spinner label="Loading service types…" />
                  ) : connectors.isError ? (
                    <LoadError what="the service types" onRetry={() => void connectors.refetch()} />
                  ) : (
                    <ConnectorsGallery
                      connectors={connectorList}
                      canManage={isAdmin}
                      onConnect={(connector) => setCreateFor({ connector })}
                    />
                  )}
                </div>
              </Disclosure>
              {/* Almost nobody needs this, and the people who do need it once:
                * the callback URL a provider demands, and the apps Jhin has
                * registered on this workspace's behalf. */}
              <p className="mt-3 text-[13px] text-faint">
                <Link href="/settings/oauth" className="text-accent-strong hover:underline">
                  Sign-in setup
                </Link>{" "}
                — this instance&rsquo;s redirect URL and the apps it has registered.
              </p>
            </section>
          </>
        )}
      </PageBody>

      {connectTargetOpen ? (
        <ConnectPanel
          workspaceId={workspaceId}
          connector={connectTargetOpen.connector}
          prefill={connectTargetOpen.prefill}
          onClose={closeConnect}
          onCreated={handleCreated}
          onConnected={(connection) => {
            invalidate();
            closeConnect();
            setCreated(connection);
            setDetailId(connection.id);
          }}
        />
      ) : null}

      {detailSlug ? (
        <CatalogEntryDialog
          slug={detailSlug}
          workspaceId={workspaceId}
          connectors={connectorList}
          onClose={() => setDetailSlug(null)}
          onCreated={handleCreated}
        />
      ) : null}

      {webhookOnce ? (
        <WebhookSecretDialog
          workspaceId={workspaceId}
          connectionId={webhookOnce.connection.id}
          connectionName={webhookOnce.connection.name}
          webhook={webhookOnce.webhook}
          onClose={() => setWebhookOnce(null)}
          onStored={() => {
            markWebhookConfigured(webhookOnce.connection);
            invalidate();
          }}
        />
      ) : null}

      {detail ? (
        <ConnectionDetailDialog
          workspaceId={workspaceId}
          connection={detail}
          connector={connectorFor(detail.connector_type)}
          canManage={isAdmin}
          title={justConnected ? `${detail.name} is connected` : undefined}
          intro={
            justConnected
              ? "Here is what it offers. Give an agent access from its profile under Tools & Access."
              : undefined
          }
          initialTab={justConnected ? "tools" : "overview"}
          onClose={closeDetail}
          onChanged={() => invalidate()}
          onRemoved={() => {
            invalidate();
            closeDetail();
          }}
        />
      ) : null}
    </>
  );
}
