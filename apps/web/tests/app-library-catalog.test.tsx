/** The Apps library in its second mode: server-side search over the synced
 * catalog. The first test pins the promise that matters most — without the
 * new props the component is byte-for-byte the library it has always been. */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppLibrary } from "@/components/app-library";
import type {
  CatalogApp,
  CatalogEntry,
  CatalogFacets,
  ConnectionInfo,
} from "@/lib/types";

afterEach(cleanup);

function app(overrides: Partial<CatalogApp>): CatalogApp {
  return {
    slug: "x",
    name: "X",
    category: "Developer tools",
    icon: "plug",
    description: "",
    connector_type: null,
    mcp_url: null,
    url_unverified: false,
    transport: "unknown",
    auth_hint: "bearer",
    auth_note: "",
    docs_url: "https://example.com/docs",
    setup_note: "",
    stdio_only: false,
    connector_config: {},
    ...overrides,
  };
}

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

const CURATED: CatalogApp[] = [
  app({ slug: "github", name: "GitHub", connector_type: "github", description: "Repositories and pull requests." }),
  app({ slug: "notion", name: "Notion", category: "Documents & knowledge", mcp_url: "https://mcp.notion.com/mcp", description: "Pages and databases." }),
];

const NOTION_CONNECTION: ConnectionInfo = {
  id: "conn-notion",
  connector_type: "mcp",
  name: "Notion team",
  auth_type: "bearer",
  status: "active",
  public_id: "p",
  config_json: { server_slug: "notion" },
  created_by_user_id: null,
  created_at: "2026-08-20T00:00:00Z",
  last_verified_at: null,
  last_error: null,
  webhook_secret_configured: false,
};

const FACETS: CatalogFacets = {
  kind: [
    { value: "mcp", label: "Apps", count: 812 },
    { value: "skill", label: "Skills", count: 240 },
  ],
  category: [
    { value: "Developer tools", label: "Developer tools", count: 400 },
    { value: "Communication", label: "Communication", count: 120 },
  ],
  trust_tier: [
    { value: "curated", label: "Curated by Jhin", count: 50 },
    { value: "indexed", label: "Community indexed", count: 700 },
  ],
  transport: [{ value: "sse", label: "Sse", count: 12 }],
  auth_hint: [
    { value: "bearer", label: "Bearer", count: 300 },
    { value: "oauth", label: "Oauth", count: 60 },
  ],
  total: 1052,
};

/** Every catalog-mode prop, so each test can override just what it needs. */
function catalogProps(overrides: Record<string, unknown> = {}) {
  return {
    entries: CURATED,
    connections: [] as ConnectionInfo[],
    canManage: true,
    onConnect: vi.fn(),
    onOpenConnection: vi.fn(),
    catalogEntries: [] as CatalogEntry[],
    catalogTotal: 0,
    query: "",
    onQueryChange: vi.fn(),
    activeFacets: {},
    onFacetChange: vi.fn(),
    includeIndexed: false,
    onIncludeIndexedChange: vi.fn(),
    onOpenDetail: vi.fn(),
    ...overrides,
  };
}

describe("AppLibrary without the catalog props", () => {
  it("still filters locally over the curated entries", () => {
    render(
      <AppLibrary entries={CURATED} connections={[]} canManage onConnect={() => {}} onOpenConnection={() => {}} />,
    );
    expect(screen.getByTestId("app-count").textContent).toBe("2 of 2 apps");
    expect(screen.getByLabelText("Category")).toBeDefined();

    fireEvent.change(screen.getByLabelText("Search apps"), { target: { value: "pages" } });
    expect(screen.getByTestId("app-count").textContent).toBe("1 of 2 apps");
    expect(screen.getByTestId("app-notion")).toBeDefined();
    expect(screen.queryByTestId("app-github")).toBeNull();
  });

  it("renders no facet chips and no trust badges", () => {
    render(
      <AppLibrary entries={CURATED} connections={[]} canManage onConnect={() => {}} onOpenConnection={() => {}} />,
    );
    expect(screen.queryByRole("group", { name: "Category" })).toBeNull();
    expect(screen.queryByText("Reviewed by Jhin")).toBeNull();
  });
});

describe("AppLibrary in catalog mode", () => {
  it("hands typing to the page instead of filtering in the browser", () => {
    const onQueryChange = vi.fn();
    render(
      <AppLibrary
        {...catalogProps({
          onQueryChange,
          catalogEntries: [
            synced({ slug: "sentry", name: "Sentry", summary: "Error tracking." }),
            synced({ slug: "linear", name: "Linear", summary: "Issues." }),
          ],
          catalogTotal: 812,
        })}
      />,
    );
    fireEvent.change(screen.getByLabelText("Search apps"), { target: { value: "sentry" } });
    expect(onQueryChange).toHaveBeenCalledWith("sentry");
    // The grid is whatever the server sent; nothing was filtered out locally.
    expect(screen.getByTestId("catalog-sentry")).toBeDefined();
    expect(screen.getByTestId("catalog-linear")).toBeDefined();
    expect(screen.getByTestId("app-count").textContent).toBe("2 of 812 apps");
    expect(screen.queryByLabelText("Category")).toBeNull();
  });

  it("renders facet chips that toggle and clear", () => {
    const onFacetChange = vi.fn();
    render(
      <AppLibrary
        {...catalogProps({
          facets: FACETS,
          onFacetChange,
          activeFacets: { category: "Developer tools" },
          catalogEntries: [synced({ slug: "sentry", name: "Sentry" })],
          catalogTotal: 1,
        })}
      />,
    );
    const categories = within(screen.getByRole("group", { name: "Category" }));
    const active = categories.getByRole("button", { name: /Developer tools/ });
    const other = categories.getByRole("button", { name: /Communication/ });
    expect(active.getAttribute("aria-pressed")).toBe("true");
    expect(other.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(other);
    expect(onFacetChange).toHaveBeenLastCalledWith("category", "Communication");

    fireEvent.click(active);
    expect(onFacetChange).toHaveBeenLastCalledWith("category", undefined);
  });

  it("renders the category rail with All first", () => {
    const onFacetChange = vi.fn();
    render(<AppLibrary {...catalogProps({ facets: FACETS, onFacetChange })} />);
    const rail = within(screen.getByTestId("category-rail"));
    const all = rail.getAllByRole("button")[0];
    expect(all.textContent).toBe("All");
    expect(all.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(rail.getByRole("button", { name: /Communication/ }));
    expect(onFacetChange).toHaveBeenCalledWith("category", "Communication");
  });

  it("keeps trust behind More filters, and transport and sign-in behind Advanced", () => {
    render(<AppLibrary {...catalogProps({ facets: FACETS })} />);
    expect(screen.queryByRole("group", { name: "Where it came from" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "More filters" }));
    expect(screen.getByRole("group", { name: "Where it came from" })).toBeDefined();
    // Transport and sign-in are protocol trivia; they sit one step further
    // back, behind the Advanced disclosure.
    expect(screen.queryByRole("group", { name: "Sign-in" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(screen.getByRole("group", { name: "Sign-in" })).toBeDefined();
    // transport has a single bucket and no active value, so its row hides.
    expect(screen.queryByRole("group", { name: "Transport" })).toBeNull();
    // The kind row is gone for good: this page is apps only now.
    expect(screen.queryByRole("group", { name: "Type" })).toBeNull();
  });

  it("spells the advanced chips like a person would, and drops Unknown", () => {
    render(
      <AppLibrary
        {...catalogProps({
          facets: {
            ...FACETS,
            transport: [
              { value: "unknown", label: "Unknown", count: 655 },
              { value: "streamable_http", label: "Streamable Http", count: 364 },
              { value: "sse", label: "Sse", count: 8 },
            ],
            auth_hint: [
              { value: "bearer", label: "Bearer", count: 701 },
              { value: "oauth", label: "Oauth", count: 15 },
              { value: "unknown", label: "Unknown", count: 12 },
            ],
          },
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "More filters" }));
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));

    const transport = within(screen.getByRole("group", { name: "Transport" }));
    expect(transport.getByRole("button", { name: /SSE/ })).toBeDefined();
    expect(transport.getByRole("button", { name: /Streamable HTTP/ })).toBeDefined();
    const signIn = within(screen.getByRole("group", { name: "Sign-in" }));
    expect(signIn.getByRole("button", { name: /OAuth/ })).toBeDefined();
    expect(signIn.getByRole("button", { name: /Bearer token/ })).toBeDefined();
    // "Unknown" is the index admitting ignorance, not a filter to offer.
    expect(screen.queryByText("Unknown")).toBeNull();
  });

  it("keeps an active facet visible even when it narrows itself out", () => {
    render(
      <AppLibrary
        {...catalogProps({
          facets: { ...FACETS, transport: [] },
          activeFacets: { transport: "sse" },
        })}
      />,
    );
    // The active filter lives behind Advanced, so Advanced opens itself.
    fireEvent.click(screen.getByRole("button", { name: "More filters" }));
    const transport = within(screen.getByRole("group", { name: "Transport" }));
    expect(transport.getByRole("button", { name: /SSE/ }).getAttribute("aria-pressed")).toBe("true");
  });

  it("toggles unreviewed community apps from inside More filters", () => {
    const onIncludeIndexedChange = vi.fn();
    render(<AppLibrary {...catalogProps({ onIncludeIndexedChange })} />);
    expect(screen.queryByRole("button", { name: "Show unreviewed community apps" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "More filters" }));
    const toggle = screen.getByRole("button", { name: "Show unreviewed community apps" });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);
    expect(onIncludeIndexedChange).toHaveBeenCalledWith(true);
  });

  it("shows one short provenance badge with the full story on title", () => {
    render(
      <AppLibrary
        {...catalogProps({
          catalogEntries: [
            synced({ slug: "sentry", name: "Sentry", trust_tier: "registry_verified" }),
            synced({ slug: "randomsrv", name: "Random server", trust_tier: "indexed", deprecated: true, connectable: false }),
          ],
          catalogTotal: 2,
        })}
      />,
    );
    const verified = within(screen.getByTestId("catalog-sentry"));
    const badge = verified.getByText("Verified");
    expect(badge.closest("[title]")?.getAttribute("title")).toBe(
      "Listed in the official MCP registry",
    );
    // The provenance chip is the card's one classifier; the footer no longer
    // repeats the category next to the buttons, where it truncated.
    expect(verified.queryByText("Developer tools")).toBeNull();

    const indexed = within(screen.getByTestId("catalog-randomsrv"));
    expect(indexed.getByText("Community").className).toContain("text-warn");
    expect(indexed.getByText("Deprecated")).toBeDefined();
    // Not connectable: the card offers a look, not a Connect.
    expect(indexed.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(indexed.getByRole("button", { name: "Details for Random server" })).toBeDefined();
  });

  it("hands a card's proxied logo to its tile", () => {
    render(
      <AppLibrary
        {...catalogProps({
          catalogEntries: [
            synced({ slug: "sentry", name: "Sentry", logo_url: "/api/v1/catalog/entries/sentry/icon" }),
            synced({ slug: "plain", name: "Plain" }),
          ],
          catalogTotal: 2,
        })}
      />,
    );
    const logo = screen.getByTestId("catalog-sentry").querySelector("img");
    expect(logo?.getAttribute("src")).toBe("/api/v1/catalog/entries/sentry/icon");
    // No logo, no <img>: the tile falls straight through to glyph/monogram.
    expect(screen.getByTestId("catalog-plain").querySelector("img")).toBeNull();
  });

  it("points skill seekers at their own library and badges no card as one", () => {
    render(<AppLibrary {...catalogProps({})} />);
    const link = screen.getByRole("link", { name: "Browse skills" });
    expect(link.getAttribute("href")).toBe("/skills");
    expect(screen.queryByText("Skill")).toBeNull();
  });

  it("opens the detail sheet rather than dialling anything", () => {
    const onOpenDetail = vi.fn();
    render(
      <AppLibrary
        {...catalogProps({
          onOpenDetail,
          catalogEntries: [synced({ slug: "sentry", name: "Sentry" })],
          catalogTotal: 1,
        })}
      />,
    );
    fireEvent.click(within(screen.getByTestId("catalog-sentry")).getByRole("button", { name: "Connect" }));
    expect(onOpenDetail).toHaveBeenCalledWith("sentry");
  });

  it("keeps the curated card and its one-click Connect for built-in rows", () => {
    const onConnect = vi.fn();
    const onOpenDetail = vi.fn();
    render(
      <AppLibrary
        {...catalogProps({
          onConnect,
          onOpenDetail,
          connections: [NOTION_CONNECTION],
          catalogEntries: [
            synced({ slug: "notion", name: "Notion", source: "builtin", trust_tier: "curated" }),
          ],
          catalogTotal: 1,
        })}
      />,
    );
    const card = within(screen.getByTestId("app-notion"));
    expect(screen.queryByTestId("catalog-notion")).toBeNull();
    expect(card.getByText("Connected")).toBeDefined();
    expect(card.getByText("Pages and databases.")).toBeDefined();
    fireEvent.click(card.getByRole("button", { name: "Connect another" }));
    expect(onConnect).toHaveBeenCalledWith(CURATED[1]);
    fireEvent.click(card.getByRole("button", { name: "Details for Notion" }));
    expect(onOpenDetail).toHaveBeenCalledWith("notion");
  });

  it("only links a docs URL the browser can be trusted with", () => {
    render(
      <AppLibrary
        {...catalogProps({
          catalogEntries: [
            synced({ slug: "safe", name: "Safe", docs_url: "https://docs.example.com/" }),
            synced({ slug: "plain", name: "Plain", docs_url: "http://docs.example.com/" }),
          ],
          catalogTotal: 2,
        })}
      />,
    );
    const link = within(screen.getByTestId("catalog-safe")).getByRole("link", { name: "Docs" });
    expect(link.getAttribute("rel")).toBe("noopener noreferrer nofollow ugc");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(within(screen.getByTestId("catalog-plain")).queryByRole("link")).toBeNull();
  });

  it("shows loading, error, and empty states without losing the search box", () => {
    const onRetry = vi.fn();
    const { rerender } = render(<AppLibrary {...catalogProps({ loading: true })} />);
    // Loading is a grid of quiet card-shaped skeletons, not a spinner.
    expect(screen.getByRole("status", { name: "Loading the library…" })).toBeDefined();
    expect(screen.getByLabelText("Search apps")).toBeDefined();

    rerender(<AppLibrary {...catalogProps({ loadError: true, onRetry })} />);
    expect(screen.getByRole("alert").textContent).toContain("the app library");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();

    rerender(<AppLibrary {...catalogProps({})} />);
    expect(screen.getByText("No apps match")).toBeDefined();
    expect(screen.getByText(/use “Add a custom app” at the top of the page/)).toBeDefined();
  });

  it("asks for more only while there is more", () => {
    const onLoadMore = vi.fn();
    const props = {
      catalogEntries: [synced({ slug: "sentry", name: "Sentry" })],
      catalogTotal: 40,
      onLoadMore,
    };
    const { rerender } = render(<AppLibrary {...catalogProps({ ...props, hasMore: true })} />);
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);

    rerender(<AppLibrary {...catalogProps({ ...props, hasMore: false })} />);
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });

  it("hides Connect from people who cannot manage apps", () => {
    render(
      <AppLibrary
        {...catalogProps({
          canManage: false,
          catalogEntries: [synced({ slug: "sentry", name: "Sentry" })],
          catalogTotal: 1,
        })}
      />,
    );
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(screen.getByRole("button", { name: "Details for Sentry" })).toBeDefined();
  });
});
