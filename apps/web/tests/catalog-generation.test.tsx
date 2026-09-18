import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useCatalogSearch, useCatalogVersion } from "@/lib/hooks";
import { api } from "@/lib/api";
import type { CatalogVersion } from "@/lib/types";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(), api: vi.fn(),
}));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });

const oldVersion: CatalogVersion = {
  release_tag: "old", data_sha256: "a", activated_at: "2026-09-01T00:00:00Z",
  source_repo: "example/catalog", entry_count: 2, mcp_count: 2, skill_count: 0,
};

it("refreshes the selected catalog query when generation changes without resetting filters or limit", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const params = { q: "notion", kind: "mcp" as const, limit: 80, offset: 0, include_indexed: false };
  client.setQueryData(["catalog-version"], oldVersion);
  client.setQueryData(["catalog-search", params], { items: [{ slug: "old-duplicate" }], total: 1 });
  client.setQueryData(["catalog-facets", { kind: "mcp" }], { total: 2 });
  client.setQueryData(["catalog-entry", "notion"], { slug: "notion" });
  client.setQueryData(["app-catalog"], []);
  let version = oldVersion;
  vi.mocked(api).mockImplementation(async (path) => path === "/api/v1/catalog/version"
    ? version : { items: [{ slug: "notion" }], total: 1 });
  const { result } = renderHook(() => ({ search: useCatalogSearch(params), version: useCatalogVersion() }), {
    wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
  expect(api).not.toHaveBeenCalled();
  version = { ...oldVersion, release_tag: "new", data_sha256: "b" };
  await result.current.version.refetch();
  await waitFor(() => expect(result.current.search.data?.items[0]?.slug).toBe("notion"));
  expect(api).toHaveBeenCalledWith("/api/v1/catalog/entries", { params: expect.objectContaining({ q: "notion", limit: 80, offset: 0, include_indexed: "false" }) });
  for (const key of [["catalog-facets", { kind: "mcp" }], ["catalog-entry", "notion"], ["app-catalog"]]) {
    expect(client.getQueryState(key)?.isInvalidated).toBe(true);
  }
  const requests = vi.mocked(api).mock.calls.filter(([path]) => path === "/api/v1/catalog/entries").length;
  await result.current.version.refetch();
  expect(vi.mocked(api).mock.calls.filter(([path]) => path === "/api/v1/catalog/entries")).toHaveLength(requests);
});

it("does not invalidate catalog queries on the initial version fetch", async () => {
  const client = new QueryClient();
  client.setQueryData(["app-catalog"], []);
  vi.mocked(api).mockResolvedValue(oldVersion);
  const { result } = renderHook(() => useCatalogVersion(), {
    wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(client.getQueryState(["app-catalog"])?.isInvalidated).toBe(false);
  expect(api).toHaveBeenCalledTimes(1);
});
