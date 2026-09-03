/** The personas query and its invalidation fan-out (the persona summary rides
 * on the agent and the chat header, so those refresh too). */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { useInvalidatePersonas, usePersonas } from "@/lib/hooks";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function wrapperFor(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("usePersonas", () => {
  it("asks for the whole library in one page", async () => {
    vi.mocked(api).mockResolvedValue({ items: [], total: 0 });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => usePersonas("w1"), { wrapper: wrapperFor(queryClient) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/personas", { params: { limit: 100 } });
    expect(result.current.data).toEqual({ items: [], total: 0 });
  });
});

describe("useInvalidatePersonas", () => {
  it("refreshes the library, the agents, and the open chat", () => {
    const queryClient = new QueryClient();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useInvalidatePersonas("w1"), {
      wrapper: wrapperFor(queryClient),
    });
    result.current();
    const keys = spy.mock.calls.map(([filters]) => filters?.queryKey);
    expect(keys).toEqual([
      ["personas", "w1"],
      ["agent", "w1"],
      ["agents", "w1"],
      ["conversation", "w1"],
    ]);
  });
});
