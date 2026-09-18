/** Component tests: the Apps library (search, category filter, connect). */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppLibrary } from "@/components/app-library";
import type { CatalogApp, ConnectionInfo } from "@/lib/types";

afterEach(cleanup);

function entry(overrides: Partial<CatalogApp>): CatalogApp {
  return {
    slug: "x",
    name: "X",
    category: "Developer tools",
    icon: "plug",
    description: "",
    connector_type: null,
    sign_in: "auto",
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

const ENTRIES = [
  entry({ slug: "github", name: "GitHub", connector_type: "github", description: "Repositories and pull requests." }),
  entry({ slug: "notion", name: "Notion", category: "Documents & knowledge", mcp_url: "https://mcp.notion.com/mcp", description: "Pages and databases." }),
  entry({ slug: "slack", name: "Slack", category: "Communication", url_unverified: true, description: "Channels and messages." }),
  entry({ slug: "filesystem", name: "Filesystem", category: "Storage", url_unverified: true, stdio_only: true, setup_note: "Needs a self-hosted MCP server; stdio not supported yet." }),
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

describe("AppLibrary", () => {
  it("enables a stdio-only app only after its optional Composio method is selected", () => {
    const app = { ...ENTRIES[3], composio_toolkit: "airtable" };
    const onConnect = vi.fn();
    render(<AppLibrary entries={[app]} connections={[]} canManage onConnect={onConnect} onOpenConnection={() => {}} />);
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Connection method for Filesystem"), { target: { value: "composio" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(onConnect).toHaveBeenCalledWith(app, "composio");
  });
  it("keeps Composio an explicit method on the same app card", () => {
    const onConnect = vi.fn();
    const app = { ...ENTRIES[1], composio_toolkit: "notion", auth_hint: "oauth" as const };
    render(<AppLibrary entries={[app]} connections={[]} canManage onConnect={onConnect} onOpenConnection={() => {}} />);
    expect(screen.getAllByTestId("app-notion")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(onConnect).toHaveBeenLastCalledWith(app, "default");
    fireEvent.change(screen.getByLabelText("Connection method for Notion"), { target: { value: "composio" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(onConnect).toHaveBeenLastCalledWith(app, "composio");
  });
  it("filters by search text and category", () => {
    render(
      <AppLibrary entries={ENTRIES} connections={[]} canManage onConnect={() => {}} onOpenConnection={() => {}} />,
    );
    expect(screen.getByTestId("app-count").textContent).toBe("4 of 4 apps");

    fireEvent.change(screen.getByLabelText("Search apps"), { target: { value: "pages" } });
    expect(screen.getByTestId("app-count").textContent).toBe("1 of 4 apps");
    expect(screen.getByTestId("app-notion")).toBeDefined();
    expect(screen.queryByTestId("app-github")).toBeNull();

    fireEvent.change(screen.getByLabelText("Search apps"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Category"), { target: { value: "Communication" } });
    expect(screen.getByTestId("app-count").textContent).toBe("1 of 4 apps");
    expect(screen.getByTestId("app-slack")).toBeDefined();

    fireEvent.change(screen.getByLabelText("Search apps"), { target: { value: "zzz" } });
    expect(screen.getByText(/No apps match/)).toBeDefined();
  });

  it("shows connected state, manage, and connect actions", () => {
    const onConnect = vi.fn();
    const onOpen = vi.fn();
    render(
      <AppLibrary
        entries={ENTRIES}
        connections={[NOTION_CONNECTION]}
        canManage
        onConnect={onConnect}
        onOpenConnection={onOpen}
      />,
    );
    const notion = within(screen.getByTestId("app-notion"));
    expect(notion.getByText("Connected")).toBeDefined();
    expect(notion.getByText(/Official MCP server/)).toBeDefined();
    fireEvent.click(notion.getByRole("button", { name: "Manage" }));
    expect(onOpen).toHaveBeenCalledWith(NOTION_CONNECTION);
    fireEvent.click(notion.getByRole("button", { name: "Connect another" }));
    expect(onConnect).toHaveBeenCalledWith(ENTRIES[1], "default");

    const github = within(screen.getByTestId("app-github"));
    expect(github.getByText(/Built-in connector/)).toBeDefined();
    fireEvent.click(github.getByRole("button", { name: "Connect" }));
    expect(onConnect).toHaveBeenLastCalledWith(ENTRIES[0], "default");

    const slack = within(screen.getByTestId("app-slack"));
    expect(slack.getByText(/enter its URL/)).toBeDefined();

    const files = within(screen.getByTestId("app-filesystem"));
    expect(files.getByText("Self-hosted")).toBeDefined();
    expect(files.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(files.getByText(/stdio not supported yet/)).toBeDefined();
  });

  it("says how each app connects in the words a person would use", () => {
    // `sign_in` answers directly where the entry has classified itself; "auto"
    // falls back to the shape-of-the-entry guess the library always made.
    const classified = [
      entry({ slug: "supabase", name: "Supabase", connector_type: "supabase", sign_in: "remote_mcp", mcp_url: "https://mcp.supabase.com/mcp" }),
      entry({ slug: "stripe", name: "Stripe", sign_in: "key", mcp_url: "https://mcp.stripe.com" }),
      entry({ slug: "deepwiki", name: "DeepWiki", sign_in: "none", auth_hint: "none", mcp_url: "https://mcp.deepwiki.com/mcp" }),
    ];
    render(
      <AppLibrary entries={classified} connections={[]} canManage onConnect={() => {}} onOpenConnection={() => {}} />,
    );
    expect(within(screen.getByTestId("app-supabase")).getByText(/Sign in with your account/)).toBeDefined();
    // A native connector no longer gets to claim the card when the sign-in
    // happens at the provider's own server.
    expect(within(screen.getByTestId("app-supabase")).queryByText(/Built-in connector/)).toBeNull();
    expect(within(screen.getByTestId("app-stripe")).getByText(/this app has no sign-in/)).toBeDefined();
    expect(within(screen.getByTestId("app-deepwiki")).getByText(/No sign-in needed/)).toBeDefined();
  });

  it("hides connect actions for non-admins", () => {
    render(
      <AppLibrary entries={ENTRIES} connections={[]} canManage={false} onConnect={() => {}} onOpenConnection={() => {}} />,
    );
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
  });
});
