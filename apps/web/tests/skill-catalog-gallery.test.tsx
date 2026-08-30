/** The reviewed-skills gallery: populated by default (no `include_indexed`
 * ever travels), search and category narrow the same server query, install
 * posts the slug and nothing else, and viewers get the library read-only. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SkillCatalogGallery } from "@/components/skill-catalog-gallery";
import type { CatalogEntry } from "@/lib/types";

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

function skillEntry(overrides: Partial<CatalogEntry>): CatalogEntry {
  return {
    slug: "skill-x",
    kind: "skill",
    source: "synced",
    name: "X",
    summary: "",
    category: "Writing",
    icon: "",
    trust_tier: "reviewed",
    default_risk: "elevated",
    popularity: 0,
    connector_type: null,
    mcp_url: null,
    url_unverified: true,
    transport: "unknown",
    auth_hint: "none",
    stdio_only: false,
    deprecated: false,
    connectable: false,
    docs_url: "",
    logo_url: null,
    ...overrides,
  } as CatalogEntry;
}

const ITEMS: CatalogEntry[] = [
  skillEntry({ slug: "release-notes", name: "Release notes", summary: "Write release notes." }),
  skillEntry({ slug: "code-review", name: "Code review", category: "Engineering" }),
];

const FACETS = {
  kind: [{ value: "skill", label: "Skills", count: 520 }],
  category: [
    { value: "Writing", label: "Writing", count: 300 },
    { value: "Engineering", label: "Engineering", count: 220 },
  ],
  trust_tier: [{ value: "reviewed", label: "Reviewed library", count: 520 }],
  transport: [],
  auth_hint: [],
  total: 520,
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Every request the gallery makes, kept for assertions. */
function installServer({ installFails = false } = {}) {
  const entryRequests: string[] = [];
  const installRequests: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path.startsWith("/api/v1/catalog/entries")) {
        entryRequests.push(path);
        const q = new URL(path, "http://test").searchParams.get("q");
        const items = q
          ? ITEMS.filter((item) => item.name.toLowerCase().includes(q.toLowerCase()))
          : ITEMS;
        return json({ items, total: q ? items.length : 520, version: null });
      }
      if (path.startsWith("/api/v1/catalog/facets")) return json(FACETS);
      if (path.endsWith("/skills/install-from-catalog") && method === "POST") {
        installRequests.push({ path, body: JSON.parse(String(init?.body)) });
        if (installFails) {
          return json({ detail: "this skill is not from a reviewed library" }, 403);
        }
        return json({
          skill: { id: "s1", name: "release-notes" },
          status: "installed",
        });
      }
      // A 403 makes the API client try to re-issue its CSRF cookie once; a
      // refusal here keeps the original error as the one that surfaces.
      if (path === "/api/v1/auth/me") return json({ detail: "no session" }, 401);
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );
  return { entryRequests, installRequests };
}

function renderGallery(isAdmin = true, onCatalogEmpty?: (empty: boolean) => void) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <SkillCatalogGallery workspaceId="w1" isAdmin={isAdmin} onCatalogEmpty={onCatalogEmpty} />
    </QueryClientProvider>,
  );
}

function paramOf(path: string, key: string): string | null {
  return new URL(path, "http://test").searchParams.get(key);
}

describe("SkillCatalogGallery", () => {
  it("is populated by default and never sends include_indexed", async () => {
    const { entryRequests } = installServer();
    renderGallery();
    expect(await screen.findByTestId("skill-release-notes")).toBeTruthy();
    expect(screen.getByTestId("skill-code-review")).toBeTruthy();
    expect(screen.getByTestId("skill-count").textContent).toBe("2 of 520 skills");
    for (const request of entryRequests) {
      expect(paramOf(request, "include_indexed")).toBeNull();
      expect(paramOf(request, "kind")).toBe("skill");
    }
  });

  it("collapses a burst of typing into one debounced request", async () => {
    const { entryRequests } = installServer();
    renderGallery();
    await screen.findByTestId("skill-release-notes");
    const before = entryRequests.length;

    const box = screen.getByLabelText("Search skills");
    fireEvent.change(box, { target: { value: "r" } });
    fireEvent.change(box, { target: { value: "re" } });
    fireEvent.change(box, { target: { value: "rel" } });

    await waitFor(() => expect(entryRequests.length).toBe(before + 1));
    expect(paramOf(entryRequests[entryRequests.length - 1], "q")).toBe("rel");
    expect(await screen.findByTestId("skill-release-notes")).toBeTruthy();
    await waitFor(() => expect(screen.queryByTestId("skill-code-review")).toBeNull());
  });

  it("installs by slug — the server resolves everything else", async () => {
    const { installRequests } = installServer();
    renderGallery();
    const card = await screen.findByTestId("skill-release-notes");

    fireEvent.click(within(card).getByRole("button", { name: "Install Release notes" }));
    await waitFor(() => expect(installRequests).toHaveLength(1));
    expect(installRequests[0].path).toBe("/api/v1/workspaces/w1/skills/install-from-catalog");
    expect(installRequests[0].body).toEqual({ slug: "release-notes" });
    expect(await within(card).findByText("Installed")).toBeTruthy();
  });

  it("surfaces the server's refusal and offers the button again", async () => {
    installServer({ installFails: true });
    renderGallery();
    const card = await screen.findByTestId("skill-release-notes");
    fireEvent.click(within(card).getByRole("button", { name: "Install Release notes" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("not from a reviewed library");
    // The card does not pretend the install happened.
    expect(within(card).queryByText("Installed")).toBeNull();
    expect(within(card).getByRole("button", { name: "Install Release notes" })).toBeTruthy();
  });

  it("hides Install from viewers but keeps the cards and details", async () => {
    installServer();
    renderGallery(false);
    const card = await screen.findByTestId("skill-release-notes");
    expect(within(card).queryByRole("button", { name: /Install/ })).toBeNull();
    expect(within(card).getByRole("button", { name: "Details for Release notes" })).toBeTruthy();
  });

  it("opens the read-only detail dialog from a card", async () => {
    installServer();
    renderGallery();
    const card = await screen.findByTestId("skill-release-notes");
    fireEvent.click(within(card).getByRole("button", { name: "Details for Release notes" }));
    const dialog = await screen.findByTestId("catalog-entry-dialog");
    expect(dialog.textContent).toContain("release-notes");
    fireEvent.click(within(dialog).getByRole("button", { name: "Close details" }));
    await waitFor(() => expect(screen.queryByTestId("catalog-entry-dialog")).toBeNull());
  });

  it("tells the truth when the gallery itself is empty — no search to blame", async () => {
    // No reviewed skill rows indexed yet: nothing typed, nothing filtered.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const path = String(input);
        if (path.startsWith("/api/v1/catalog/entries")) {
          return json({ items: [], total: 0, version: null });
        }
        if (path.startsWith("/api/v1/catalog/facets")) {
          return json({ ...FACETS, category: [], total: 0 });
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    const onCatalogEmpty = vi.fn();
    renderGallery(true, onCatalogEmpty);
    expect(await screen.findByText("The skills gallery is on its way")).toBeTruthy();
    expect(
      screen.getByText(
        "Reviewed skill libraries are added over time — check back soon. Meanwhile, the Advanced section below browses a GitHub skill library directly.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("No skills match")).toBeNull();
    // "0 of 0 skills" would just restate the empty state; the counter hides.
    expect(screen.queryByTestId("skill-count")).toBeNull();
    // The page is told, so it can open the Advanced GitHub browser.
    expect(onCatalogEmpty).toHaveBeenLastCalledWith(true);
  });

  it("does not report an empty catalog when a search simply missed", async () => {
    const onCatalogEmpty = vi.fn();
    installServer();
    renderGallery(true, onCatalogEmpty);
    await screen.findByTestId("skill-release-notes");
    fireEvent.change(screen.getByLabelText("Search skills"), {
      target: { value: "zzz-nothing" },
    });
    expect(await screen.findByText("No skills match")).toBeTruthy();
    expect(onCatalogEmpty).not.toHaveBeenCalledWith(true);
  });

  it("shows the calm empty state when nothing matches", async () => {
    installServer();
    renderGallery();
    await screen.findByTestId("skill-release-notes");
    fireEvent.change(screen.getByLabelText("Search skills"), {
      target: { value: "zzz-nothing" },
    });
    expect(await screen.findByText("No skills match")).toBeTruthy();
    expect(
      screen.getByText("Try a different search — new libraries are reviewed and added over time."),
    ).toBeTruthy();
  });
});
