/** The reconnect banner: shown only when a sign-in has lapsed, and wired to
 * the endpoint that re-authorizes the existing connection in place. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserSignInButton, ReconnectBanner } from "@/components/connect/reconnect-banner";
import { api } from "@/lib/api";
import type { ConnectionInfo, ConnectorInfo } from "@/lib/types";

const navigated: string[] = [];

vi.mock("@/lib/oauth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/oauth")>();
  return { ...actual, navigateToProvider: (url: string) => navigated.push(url) };
});

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: vi.fn() };
});

function connection(overrides: Partial<ConnectionInfo> = {}): ConnectionInfo {
  return {
    id: "connection-1",
    connector_type: "linear",
    name: "Linear",
    auth_type: "oauth",
    status: "active",
    public_id: "a".repeat(32),
    config_json: {},
    created_by_user_id: "user-1",
    created_at: "2026-08-29T12:00:00Z",
    last_verified_at: null,
    last_error: null,
    webhook_secret_configured: false,
    ...overrides,
  };
}

function renderBanner(connections: ConnectionInfo[]) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReconnectBanner workspaceId="workspace-1" connections={connections} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  navigated.length = 0;
  vi.mocked(api).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("ReconnectBanner", () => {
  const managedConnector = {
    connector_type: "supabase",
    managed_auth: { provider: "composio", configured: true, toolkit: "supabase", auth_type: "management_token" },
    config_fields: [{ name: "project_ref", auth_types: ["management_token"] }],
  } as ConnectorInfo;

  function renderMigration(overrides: Partial<ConnectionInfo> = {}) {
    return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
      <BrowserSignInButton workspaceId="workspace-1" connector={managedConnector} connection={connection({
        connector_type: "supabase", auth_type: "management_token", auth_provider: "local",
        config_json: { project_ref: "project-one", unrelated_bookkeeping: "omit" }, ...overrides,
      })} />
    </QueryClientProvider>);
  }

  it("migrates the existing native connection using only declared public configuration", async () => {
    vi.mocked(api).mockResolvedValue({ authorization_url: "https://connect.composio.dev/link" });
    renderMigration();
    fireEvent.click(screen.getByRole("button", { name: "Use Composio sign-in" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/api/v1/workspaces/workspace-1/oauth/start", {
      method: "POST", body: { connector_type: "supabase", connection_id: "connection-1", name: "Linear", provider_key: "composio", config: { project_ref: "project-one" } },
    }));
    await waitFor(() => expect(navigated).toEqual(["https://connect.composio.dev/link"]));
  });

  it("does not offer migration for PostgreSQL connections", () => {
    renderMigration({ auth_type: "postgres" });
    expect(screen.queryByRole("button", { name: "Use Composio sign-in" })).toBeNull();
  });

  it("keeps migration errors safe and stays on the current connection", async () => {
    vi.mocked(api).mockRejectedValue(new Error("private upstream details"));
    renderMigration();
    fireEvent.click(screen.getByRole("button", { name: "Use Composio sign-in" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Starting browser sign-in failed. Try again.");
    expect(navigated).toEqual([]);
  });
  it("stays out of the way while every sign-in is good", () => {
    const { container } = renderBanner([connection(), connection({ id: "c2", status: "error" })]);
    expect(container.firstChild).toBeNull();
  });

  it("names every connection whose sign-in lapsed, and who it belonged to", () => {
    renderBanner([
      connection(),
      connection({
        id: "c2",
        name: "Notion",
        status: "needs_reauth",
        authorized_by: { user_id: "user-9", display_name: "Ada Lovelace" },
      }),
      connection({ id: "c3", name: "Slack", status: "needs_reauth" }),
    ]);
    const banner = screen.getByTestId("reconnect-banner");
    expect(banner.textContent).toContain("2 apps need to be reconnected");
    expect(banner.textContent).toContain("Notion");
    expect(banner.textContent).toContain("Slack");
    // The provider permission is a person's, and the banner says whose.
    expect(banner.textContent).toContain("Ada Lovelace");
    expect(within(banner).getAllByRole("button", { name: /Reconnect/ })).toHaveLength(2);
  });

  it("re-authorizes the existing connection rather than making a new one", async () => {
    vi.mocked(api).mockResolvedValue({
      authorization_url: "https://auth.example.com/authorize?client_id=abc",
      state_expires_at: "2026-08-29T12:10:00Z",
      issuer: "https://auth.example.com",
      scopes: ["read"],
      resource: "https://mcp.example.com/mcp",
      authorized_as_user_id: "user-1",
      client_source: "static",
    });
    renderBanner([connection({ status: "needs_reauth" })]);

    fireEvent.click(screen.getByTestId("reconnect-Linear"));

    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api).mock.calls[0][0]).toBe(
      "/api/v1/workspaces/workspace-1/connections/connection-1/reauthorize",
    );
    expect(vi.mocked(api).mock.calls[0][1]).toEqual({ method: "POST" });
    await waitFor(() =>
      expect(navigated).toEqual(["https://auth.example.com/authorize?client_id=abc"]),
    );
    // Where we were, so the callback can bring us back.
    expect(window.sessionStorage.getItem("jhin.oauth.return")).not.toBeNull();
  });

  it("says so in place when the re-authorization cannot be started", async () => {
    vi.mocked(api).mockRejectedValue(new Error("boom"));
    renderBanner([connection({ status: "needs_reauth" })]);

    fireEvent.click(screen.getByTestId("reconnect-Linear"));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Starting that reconnection failed.",
    );
    expect(navigated).toEqual([]);
  });
});
