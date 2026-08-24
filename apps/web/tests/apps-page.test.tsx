/** Apps page: the library opens the connection dialog pre-filled for MCP
 * apps and routes native apps to their own connector. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AppsPage from "@/app/(app)/apps/page";
import type { CatalogApp, ConnectorInfo } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const MCP_CONNECTOR: ConnectorInfo = {
  connector_type: "mcp",
  display_name: "Any MCP server",
  icon: "mcp",
  description: "Connect any MCP server.",
  auth_schemes: [
    { type: "none", label: "No authentication", description: "", secret_fields: [] },
    {
      type: "bearer",
      label: "Bearer token",
      description: "",
      secret_fields: [{ name: "token", label: "Access token", placeholder: "token…", multiline: false, required: true }],
    },
    {
      type: "header",
      label: "Custom header",
      description: "",
      secret_fields: [{ name: "token", label: "Header value (secret)", placeholder: "", multiline: false, required: true }],
    },
  ],
  config_fields: [
    { name: "server_url", label: "Server URL", required: true, placeholder: "https://…", help: "", kind: "text", auth_types: [], default: null, minimum: null, maximum: null },
    { name: "server_slug", label: "Short name", required: true, placeholder: "", help: "", kind: "text", auth_types: [], default: null, minimum: null, maximum: null },
    { name: "transport", label: "Transport", required: false, placeholder: "auto", help: "", kind: "text", auth_types: [], default: "auto", minimum: null, maximum: null },
    { name: "header_name", label: "Header name", required: true, placeholder: "X-API-Key", help: "", kind: "text", auth_types: ["header"], default: null, minimum: null, maximum: null },
  ],
  webhook_events: [],
  canonical_events: [],
  capabilities: [],
  supports_webhooks: false,
  webhook_secret_mode: "none",
  webhook_signature_algorithm: "",
  webhook_setup_help: "",
  docs_url: "",
};

const GITHUB_CONNECTOR: ConnectorInfo = {
  ...MCP_CONNECTOR,
  connector_type: "github",
  display_name: "GitHub",
  icon: "github",
  auth_schemes: [
    {
      type: "pat",
      label: "Personal access token",
      description: "",
      secret_fields: [{ name: "token", label: "Token", placeholder: "ghp_…", multiline: false, required: true }],
    },
  ],
  config_fields: [],
};

const CATALOG: CatalogApp[] = [
  {
    slug: "github",
    name: "GitHub",
    category: "Developer tools",
    icon: "github",
    description: "Repositories.",
    connector_type: "github",
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
  {
    slug: "fake",
    name: "Fake MCP (dev)",
    category: "Developer tools",
    icon: "flask",
    description: "The dev stack's fake MCP server.",
    connector_type: null,
    mcp_url: "https://mcp.fake.test/mcp",
    url_unverified: false,
    transport: "streamable_http",
    auth_hint: "bearer",
    auth_note: "Dev-only token: fake-mcp-token",
    docs_url: "",
    setup_note: "",
    stdio_only: false,
    connector_config: {},
  },
  {
    slug: "slack",
    name: "Slack",
    category: "Communication",
    icon: "message-square",
    description: "Channels.",
    connector_type: null,
    mcp_url: null,
    url_unverified: true,
    transport: "unknown",
    auth_hint: "bearer",
    auth_note: "",
    docs_url: "",
    setup_note: "Self-host a Slack MCP server.",
    stdio_only: false,
    connector_config: {},
  },
];

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

function installServer() {
  const writes: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path === "/api/v1/connectors") return json([GITHUB_CONNECTOR, MCP_CONNECTOR]);
      if (path === "/api/v1/connectors/catalog") return json(CATALOG);
      if (path === "/api/v1/workspaces/workspace-1/connections" && method === "GET") return json([]);
      if (path === "/api/v1/workspaces/workspace-1/connections" && method === "POST") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        writes.push(body);
        return json({
          connection: {
            id: "conn-1",
            connector_type: body.connector_type,
            name: body.name,
            auth_type: body.auth_type,
            status: "active",
            public_id: "p",
            config_json: body.config,
            created_by_user_id: null,
            created_at: "2026-08-20T00:00:00Z",
            last_verified_at: null,
            last_error: null,
            webhook_secret_configured: false,
          },
          webhook: null,
        });
      }
      if (path.endsWith("/connections/conn-1/tools")) {
        return json({
          connection_id: "conn-1",
          connector_type: "mcp",
          dynamic: true,
          capability_pattern: "mcp.fake.*",
          discovered_at: "2026-08-20T00:00:00Z",
          tools: [
            {
              name: "mcp.fake.echo",
              provider_name: "echo",
              description: "[MCP: fake] Return the text.",
              risk: "read",
              derived_risk: "read",
              risk_override: null,
              annotations: { read_only_hint: true },
              input_schema: {},
              schema_truncated: false,
              supports_approval: true,
              scope_keys: ["connection_id", "tool"],
            },
          ],
        });
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );
  return writes;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
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

describe("AppsPage", () => {
  it("pre-fills the MCP dialog from a catalog entry and shows the tools after connecting", async () => {
    const writes = installServer();
    renderPage();
    const card = await screen.findByTestId("app-fake");
    fireEvent.click(within(card).getByRole("button", { name: "Connect" }));

    const form = await screen.findByTestId("create-connection-form");
    expect(screen.getByRole("heading", { name: "Connect Fake MCP (dev)" })).toBeDefined();
    expect((within(form).getByLabelText("Server URL") as HTMLInputElement).value).toBe("https://mcp.fake.test/mcp");
    expect((within(form).getByLabelText("Short name") as HTMLInputElement).value).toBe("fake");
    expect((within(form).getByLabelText("Transport") as HTMLInputElement).value).toBe("auto");
    expect(within(form).getByText(/Dev-only token/)).toBeDefined();
    const auth = within(form).getByDisplayValue("Bearer token") as HTMLSelectElement;
    expect(auth.value).toBe("bearer");

    fireEvent.change(within(form).getByPlaceholderText("token…"), { target: { value: "fake-mcp-token" } });
    fireEvent.submit(form);

    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({
      connector_type: "mcp",
      name: "Fake MCP (dev)",
      auth_type: "bearer",
      credentials: { token: "fake-mcp-token" },
      config: { server_url: "https://mcp.fake.test/mcp", server_slug: "fake", transport: "auto" },
    });
    expect(await screen.findByRole("heading", { name: "Fake MCP (dev) is connected" })).toBeDefined();
    const tool = await screen.findByTestId("connection-tool-mcp.fake.echo");
    expect(within(tool).getAllByText("read").some((el) => el.tagName === "SPAN")).toBe(true);
    expect(within(tool).getByText("Reads information only")).toBeDefined();
  });

  it("asks for the server URL when the catalog endpoint is unverified", async () => {
    installServer();
    renderPage();
    const card = await screen.findByTestId("app-slack");
    fireEvent.click(within(card).getByRole("button", { name: "Connect" }));
    const form = await screen.findByTestId("create-connection-form");
    expect((within(form).getByLabelText("Server URL") as HTMLInputElement).value).toBe("");
    expect(within(form).getByText(/Enter the server URL from the provider's docs/)).toBeDefined();
    expect(within(form).getByText(/Self-host a Slack MCP server/)).toBeDefined();
  });

  it("routes native apps to their own connector", async () => {
    installServer();
    renderPage();
    const card = await screen.findByTestId("app-github");
    fireEvent.click(within(card).getByRole("button", { name: "Connect" }));
    const form = await screen.findByTestId("create-connection-form");
    expect(screen.getByRole("heading", { name: "Connect GitHub" })).toBeDefined();
    expect(within(form).getByDisplayValue("Personal access token")).toBeDefined();
    expect(within(form).queryByLabelText("Server URL")).toBeNull();
  });
});
