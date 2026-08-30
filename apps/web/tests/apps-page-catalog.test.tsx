/** Apps page wired to the synced catalog: the search box is debounced into
 * one request, the chips travel as query parameters, the release the index
 * came from is on screen, and a failing catalog costs nobody the curated
 * library they already had. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AppsPage from "@/app/(app)/apps/page";
import type { CatalogApp, CatalogEntry, ConnectorInfo } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

vi.mock("@/components/catalog-entry-dialog", () => ({
  CatalogEntryDialog: ({ slug, onClose }: { slug: string; onClose: () => void }) => (
    <div data-testid="catalog-entry-dialog">
      {slug}
      <button type="button" onClick={onClose}>
        Close details
      </button>
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const MCP_CONNECTOR: ConnectorInfo = {
  connector_type: "mcp",
  display_name: "Any MCP server",
  icon: "mcp",
  description: "Connect any MCP server.",
  auth_schemes: [{ type: "none", label: "No authentication", description: "", secret_fields: [] }],
  config_fields: [],
  webhook_events: [],
  canonical_events: [],
  capabilities: [],
  supports_webhooks: false,
  webhook_secret_mode: "none",
  webhook_signature_algorithm: "",
  webhook_setup_help: "",
  docs_url: "",
};

const CURATED: CatalogApp[] = [
  {
    slug: "github",
    name: "GitHub",
    category: "Developer tools",
    icon: "github",
    description: "Repositories.",
    connector_type: null,
    mcp_url: "https://api.githubcopilot.com/mcp/",
    url_unverified: false,
    transport: "streamable_http",
    auth_hint: "bearer",
    auth_note: "",
    docs_url: "",
    setup_note: "",
    stdio_only: false,
    connector_config: {},
  },
];

function synced(overrides: Partial<CatalogEntry>): CatalogEntry {
  return {
    slug: "x",
    kind: "mcp",
    source: "synced",
    name: "X",
    summary: "",
    category: "Developer tools",
    icon: "plug",
    trust_tier: "registry_verified",
    default_risk: "write",
    popularity: 0,
    connector_type: null,
    mcp_url: null,
    url_unverified: true,
    transport: "unknown",
    auth_hint: "bearer",
    stdio_only: false,
    deprecated: false,
    connectable: true,
    docs_url: "",
    ...overrides,
  };
}

const ITEMS: CatalogEntry[] = [
  synced({ slug: "github", name: "GitHub", source: "builtin", trust_tier: "curated" }),
  synced({ slug: "sentry", name: "Sentry", summary: "Error tracking." }),
];

const FACETS = {
  kind: [
    { value: "mcp", label: "Apps", count: 812 },
    { value: "skill", label: "Skills", count: 240 },
  ],
  category: [
    { value: "developer", label: "Developer tools", count: 400 },
    { value: "comms", label: "Communication", count: 120 },
  ],
  trust_tier: [
    { value: "curated", label: "Curated by Jhin", count: 50 },
    { value: "indexed", label: "Community indexed", count: 700 },
  ],
  transport: [],
  auth_hint: [],
  total: 1052,
};

const VERSION = {
  release_tag: "2026.08.01",
  source_repo: "jhinhq/jhin-catalog",
  data_sha256: "a".repeat(64),
  entry_count: 1052,
  mcp_count: 812,
  skill_count: 240,
  activated_at: "2026-08-27T12:00:00Z",
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

/** Every request the page makes, with the catalog ones kept for assertions. */
function installServer({ catalogFails = false } = {}) {
  const entryRequests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path === "/api/v1/connectors") return json([MCP_CONNECTOR]);
      if (path === "/api/v1/connectors/catalog") return json(CURATED);
      if (path === "/api/v1/workspaces/workspace-1/connections" && method === "GET") return json([]);
      if (path.startsWith("/api/v1/catalog/entries")) {
        entryRequests.push(path);
        if (catalogFails) return json({ detail: "Not available" }, 503);
        const q = new URL(path, "http://test").searchParams.get("q");
        const items = q ? ITEMS.filter((item) => item.name.toLowerCase().includes(q.toLowerCase())) : ITEMS;
        return json({ items, total: q ? items.length : 1052, version: VERSION });
      }
      if (path.startsWith("/api/v1/catalog/facets")) {
        if (catalogFails) return json({ detail: "Not available" }, 503);
        return json(FACETS);
      }
      if (path === "/api/v1/catalog/version") {
        if (catalogFails) return json({ detail: "Not available" }, 503);
        return json(VERSION);
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );
  return entryRequests;
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceProvider
        user={{ id: "user-1", email: "owner@example.com", display_name: "Owner", created_at: "2026-08-18T00:00:00Z" }}
        workspace={{ workspace_id: "workspace-1", workspace_name: "Acme", workspace_slug: "acme", role: "owner" }}
      >
        <AppsPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

function paramOf(path: string, key: string): string | null {
  return new URL(path, "http://test").searchParams.get(key);
}

describe("AppsPage with the synced catalog", () => {
  it("renders the merged index and names the release it came from", async () => {
    installServer();
    renderPage();
    // The curated entry keeps its own card; the synced one gets a catalog card.
    expect(await screen.findByTestId("app-github")).toBeDefined();
    expect(await screen.findByTestId("catalog-sentry")).toBeDefined();
    expect(screen.getByTestId("app-count").textContent).toBe("2 of 1052 apps");
    expect((await screen.findByText(/apps and skills · 2026\.08\.01/)).textContent).toContain("Indexed");
  });

  it("collapses a burst of typing into a single request", async () => {
    const entryRequests = installServer();
    renderPage();
    await screen.findByTestId("catalog-sentry");
    const before = entryRequests.length;

    const box = screen.getByLabelText("Search apps");
    fireEvent.change(box, { target: { value: "s" } });
    fireEvent.change(box, { target: { value: "se" } });
    fireEvent.change(box, { target: { value: "sen" } });

    await waitFor(() => expect(entryRequests.length).toBe(before + 1));
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(entryRequests).toHaveLength(before + 1);
    expect(paramOf(entryRequests[entryRequests.length - 1], "q")).toBe("sen");
    expect(await screen.findByTestId("catalog-sentry")).toBeDefined();
    expect(screen.queryByTestId("app-github")).toBeNull();
  });

  it("pins every request to apps and never asks for skills", async () => {
    const entryRequests = installServer();
    renderPage();
    await screen.findByTestId("catalog-sentry");
    expect(paramOf(entryRequests[0], "kind")).toBe("mcp");
    // The skills library gets a pointer instead of a facet.
    expect(screen.getByRole("link", { name: "Browse skills" }).getAttribute("href")).toBe("/skills");
    expect(screen.queryByRole("group", { name: "Type" })).toBeNull();
  });

  it("sends a clicked category as a query parameter and clears it again", async () => {
    const entryRequests = installServer();
    renderPage();
    await screen.findByTestId("catalog-sentry");

    const rail = within(await screen.findByTestId("category-rail"));
    fireEvent.click(rail.getByRole("button", { name: /Communication/ }));
    await waitFor(() =>
      expect(paramOf(entryRequests[entryRequests.length - 1], "category")).toBe("comms"),
    );

    // Clearing goes back to a page already in cache, so the assertion is the
    // pill's own state rather than another round trip.
    fireEvent.click(rail.getByRole("button", { name: /Communication/ }));
    await waitFor(() =>
      expect(rail.getByRole("button", { name: /Communication/ }).getAttribute("aria-pressed")).toBe("false"),
    );
    expect(await screen.findByTestId("catalog-sentry")).toBeDefined();
  });

  it("asks for the community-indexed rows only when told to", async () => {
    const entryRequests = installServer();
    renderPage();
    await screen.findByTestId("catalog-sentry");
    expect(paramOf(entryRequests[0], "include_indexed")).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "More filters" }));
    fireEvent.click(screen.getByRole("button", { name: "Show unreviewed community apps" }));
    await waitFor(() =>
      expect(paramOf(entryRequests[entryRequests.length - 1], "include_indexed")).toBe("true"),
    );
  });

  it("opens the detail sheet from a catalog card without dialling the server", async () => {
    installServer();
    renderPage();
    const card = await screen.findByTestId("catalog-sentry");
    fireEvent.click(within(card).getByRole("button", { name: "Connect" }));
    const dialog = await screen.findByTestId("catalog-entry-dialog");
    expect(dialog.textContent).toContain("sentry");

    fireEvent.click(within(dialog).getByRole("button", { name: "Close details" }));
    await waitFor(() => expect(screen.queryByTestId("catalog-entry-dialog")).toBeNull());
  });

  it("never grows the page past the limit the endpoint accepts", async () => {
    // `list_catalog_entries` declares `limit: Query(ge=1, le=100)`. A third
    // click used to ask for 120, and FastAPI rejects that before the
    // service's own clamp runs -- which set `isError`, which dropped the
    // whole gallery back to the curated-only library.
    const entryRequests = installServer();
    renderPage();
    await screen.findByTestId("catalog-sentry");

    for (let click = 0; click < 4; click += 1) {
      const more = screen.queryByRole("button", { name: "Show more" });
      if (!more) break;
      fireEvent.click(more);
      await waitFor(() => expect(entryRequests.length).toBeGreaterThan(0));
    }

    const limits = entryRequests.map((path) => Number(paramOf(path, "limit")));
    expect(Math.max(...limits)).toBeLessThanOrEqual(100);
    // And once it is pinned at the ceiling it stops offering a click that
    // cannot do anything.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Show more" })).toBeNull(),
    );
  });

  it("falls back to the curated library when the index is unavailable", async () => {
    installServer({ catalogFails: true });
    renderPage();
    expect(await screen.findByTestId("app-github")).toBeDefined();
    expect(screen.getByLabelText("Category")).toBeDefined();
    expect(screen.getByText(/wider app index is unavailable/)).toBeDefined();
    // The old one-click Connect is exactly what it was before the catalog.
    fireEvent.click(within(screen.getByTestId("app-github")).getByRole("button", { name: "Connect" }));
    expect(await screen.findByTestId("create-connection-form")).toBeDefined();
  });
});
