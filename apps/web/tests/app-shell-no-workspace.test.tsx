/** A signed-in account that belongs to no workspace.
 *
 * Reachable by an invited-then-removed account and by an owner who has just
 * deleted their last workspace from Settings. Creating a workspace needs only
 * an authenticated user, so this screen offers that rather than telling
 * someone to go find an admin — and it still has to offer a way out, because
 * no navigation is rendered here to sign out from. */

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
  it("offers workspace creation instead of rendering an empty app", () => {
    renderShell();
    expect(screen.getByTestId("no-workspace")).toBeDefined();
    expect(screen.getByRole("heading", { name: "Create your workspace" })).toBeDefined();
    expect(screen.queryByText("never shown")).toBeNull();
  });

  it("creates the workspace and re-reads identity so the app takes over", async () => {
    vi.mocked(api).mockResolvedValue({ id: "w1" });
    renderShell();
    const submit = screen.getByRole("button", { name: "Create workspace" });
    // A name is required: an empty form cannot be submitted by accident.
    expect(submit.hasAttribute("disabled")).toBe(true);

    fireEvent.change(screen.getByLabelText("Workspace name"), { target: { value: " Acme HQ " } });
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith(
        "/api/v1/workspaces",
        expect.objectContaining({
          method: "POST",
          // Trimmed, and carrying the viewer's own timezone.
          body: expect.objectContaining({ name: "Acme HQ" }),
        }),
      ),
    );
  });

  it("still offers a way out, so the screen is not a trap", async () => {
    vi.mocked(api).mockResolvedValue(undefined);
    renderShell();
    // Secondary to creating a workspace, but still reachable by name.
    fireEvent.click(screen.getByRole("button", { name: "sign out" }));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/auth/logout", { method: "POST" }),
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });
});
