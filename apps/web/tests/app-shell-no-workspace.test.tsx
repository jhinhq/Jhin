/** The shell's dead end: a signed-in account that belongs to no workspace.
 *
 * Reachable by an invited-then-removed account and by an owner who has just
 * deleted their last workspace from Settings, so it has to offer a way out —
 * no navigation is rendered on this screen to sign out from. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/app-shell";
import { api } from "@/lib/api";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => "/home",
  useRouter: () => ({ replace, push: vi.fn() }),
}));

vi.mock("@/lib/hooks", () => ({
  useIdentity: () => ({
    data: {
      user: {
        id: "u1",
        email: "ada@example.com",
        display_name: "Ada",
        created_at: "2026-01-01T00:00:00Z",
      },
      memberships: [],
      api_key: null,
    },
    isPending: false,
    error: null,
    refetch: vi.fn(),
  }),
  useBootstrapStatus: () => ({ data: { needs_bootstrap: false } }),
  useAttention: () => ({ data: undefined }),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderShell() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AppShell>
        <p>never shown</p>
      </AppShell>
    </QueryClientProvider>,
  );
}

describe("AppShell with no workspace membership", () => {
  it("explains the state instead of rendering an empty app", () => {
    renderShell();
    expect(screen.getByTestId("no-workspace")).toBeDefined();
    expect(screen.getByText(/not a member of any workspace/)).toBeDefined();
    expect(screen.queryByText("never shown")).toBeNull();
  });

  it("offers a way out, so the screen is not a trap", async () => {
    vi.mocked(api).mockResolvedValue(undefined);
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/auth/logout", { method: "POST" }),
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });
});
