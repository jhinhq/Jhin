/** The detail dialog for one catalog entry.
 *
 * This is the one place in the product where text somebody else wrote is shown
 * to a person next to a button that creates a connection, so the two things
 * being tested are: what gets rendered as a *link* (only https, and always with
 * the full rel set), and what the dialog does on its own (nothing — it never
 * dials `mcp_url`, and issues no mutation until a person presses Connect and
 * then submits the form). The fetch stub throws on any request it was not told
 * to expect, which is what makes "never dials the endpoint" an assertion rather
 * than a hope. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CatalogEntryDialog } from "@/components/catalog-entry-dialog";
import type { CatalogEntryDetail, ConnectorInfo } from "@/lib/types";
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
      secret_fields: [
        { name: "token", label: "Access token", placeholder: "token…", multiline: false, required: true },
      ],
    },
  ],
  config_fields: [
    { name: "server_url", label: "Server URL", required: true, placeholder: "https://…", help: "", kind: "text", auth_types: [], default: null, minimum: null, maximum: null },
    { name: "server_slug", label: "Short name", required: true, placeholder: "", help: "", kind: "text", auth_types: [], default: null, minimum: null, maximum: null },
    { name: "transport", label: "Transport", required: false, placeholder: "auto", help: "", kind: "text", auth_types: [], default: "auto", minimum: null, maximum: null },
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

const CONFIG_SCHEMA = {
  version: 1,
  connector_type: "mcp",
  auth: { type: "bearer", note: "Create a token in Kestrel settings." },
  degraded: [],
  fields: [
    {
      name: "server_url",
      label: "Server URL",
      type: "string",
      required: true,
      secret: false,
      default: "https://mcp.example.com/kestrel",
      enum: [],
      max_length: 512,
      minimum: null,
      maximum: null,
      placeholder: "https://…",
      help: "",
      multiline: false,
    },
    {
      name: "server_slug",
      label: "Short name",
      type: "string",
      required: true,
      secret: false,
      default: "kestrel",
      enum: [],
      max_length: 32,
      minimum: null,
      maximum: null,
      placeholder: "",
      help: "",
      multiline: false,
    },
    {
      name: "transport",
      label: "Transport",
      type: "string",
      required: false,
      secret: false,
      default: "auto",
      enum: ["auto", "streamable_http", "sse"],
      max_length: null,
      minimum: null,
      maximum: null,
      placeholder: "",
      help: "",
      multiline: false,
    },
  ],
} as unknown as CatalogEntryDetail["config_schema"];

function detail(overrides: Partial<CatalogEntryDetail> = {}): CatalogEntryDetail {
  return {
    slug: "kestrel",
    kind: "mcp",
    source: "synced",
    name: "Kestrel",
    summary: "Notes and monitoring.",
    category: "Developer tools",
    icon: "mcp",
    trust_tier: "registry_verified",
    default_risk: "write",
    popularity: 0.75,
    connector_type: null,
    mcp_url: "https://mcp.example.com/kestrel",
    url_unverified: false,
    transport: "streamable_http",
    auth_hint: "bearer",
    stdio_only: false,
    deprecated: false,
    connectable: true,
    docs_url: "https://docs.example.com/kestrel",
    description: "Read and write notes in Kestrel.",
    homepage: "https://kestrel.example.com",
    auth_note: "Create a token in Kestrel settings.",
    setup_note: "",
    license: "MIT",
    tags: ["notes", "monitoring"],
    connector_config: {},
    sources: [
      { source_id: "registry", upstream_id: "io.github.acme/kestrel", url: "https://registry.example.com/kestrel" },
    ],
    config_schema: CONFIG_SCHEMA,
    mcp: {
      tool_count: 12,
      registry_name: "io.github.acme/kestrel",
      npm_package: "@acme/kestrel",
      verified_upstream: true,
      package_identifiers: ["@acme/kestrel"],
      remote_urls: ["https://mcp.example.com/kestrel"],
    },
    skill: null,
    ...overrides,
  };
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Only the two paths the dialog is allowed to touch. Anything else throws,
 * which is how "never dials the catalog's endpoint" gets tested. */
function installServer(entry: CatalogEntryDetail) {
  const writes: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path === `/api/v1/catalog/entries/${entry.slug}` && method === "GET") return json(entry);
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
            created_at: "2026-08-28T00:00:00Z",
            last_verified_at: null,
            last_error: null,
            webhook_secret_configured: false,
          },
          webhook: null,
        });
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );
  return writes;
}

function renderDialog(entry: CatalogEntryDetail, onCreated = vi.fn(), onClose = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CatalogEntryDialog
        slug={entry.slug}
        workspaceId="workspace-1"
        connectors={[MCP_CONNECTOR]}
        onClose={onClose}
        onCreated={onCreated}
      />
    </QueryClientProvider>,
  );
  return { onCreated, onClose };
}

describe("CatalogEntryDialog", () => {
  it("shows the entry with its provenance in plain language", async () => {
    installServer(detail());
    renderDialog(detail());

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    expect(await within(dialog).findByText("Read and write notes in Kestrel.")).toBeDefined();
    expect(within(dialog).getByText("Listed in the official MCP registry")).toBeDefined();
    expect(within(dialog).getByText("Developer tools")).toBeDefined();
    expect(within(dialog).getByText("notes")).toBeDefined();
    expect(within(dialog).getByText("monitoring")).toBeDefined();
  });

  it("leads with the proxied logo tile, never an upstream image URL", async () => {
    const withLogo = detail({ logo_url: "/api/v1/catalog/entries/kestrel/icon" });
    installServer(withLogo);
    renderDialog(withLogo);

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    const img = dialog.querySelector("img");
    expect(img?.getAttribute("src")).toBe("/api/v1/catalog/entries/kestrel/icon");
  });

  it("renders every external link with the full rel set and target", async () => {
    installServer(detail());
    renderDialog(detail());

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    const links = [...dialog.querySelectorAll("a[href]")];
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) {
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("rel")).toBe("noopener noreferrer nofollow ugc");
      expect(link.getAttribute("href")?.startsWith("https://")).toBe(true);
    }
  });

  it("refuses to link a docs URL that is not https", async () => {
    const insecure = detail({
      docs_url: "http://docs.example.com/kestrel",
      homepage: "javascript:alert(1)",
      sources: [{ source_id: "s", upstream_id: "u", url: "data:text/html,<script>" }],
    });
    installServer(insecure);
    renderDialog(insecure);

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    const hrefs = [...dialog.querySelectorAll("a[href]")].map((link) => link.getAttribute("href"));
    expect(hrefs.some((href) => href?.startsWith("http://"))).toBe(false);
    expect(hrefs.some((href) => href?.startsWith("javascript:"))).toBe(false);
    expect(hrefs.some((href) => href?.startsWith("data:"))).toBe(false);
  });

  it("shows a skill's provenance as text, with nothing to connect", async () => {
    const skill = detail({
      slug: "release_notes",
      kind: "skill",
      name: "Release notes",
      connectable: false,
      mcp_url: null,
      config_schema: null,
      mcp: null,
      skill: {
        skill_name: "release-notes",
        source_ref: "acme/skills@main",
        skill_path: "skills/release-notes/SKILL.md",
        commit_sha: "b".repeat(40),
        marketplace: "",
        plugin: "",
        model_invocable: true,
        allowed_tools: ["Read", "Write"],
      },
    });
    installServer(skill);
    renderDialog(skill);

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    expect(await within(dialog).findByText(/acme\/skills@main/)).toBeDefined();
    expect(within(dialog).getByText(/skills\/release-notes\/SKILL\.md/)).toBeDefined();
    expect(within(dialog).queryByRole("button", { name: "Connect" })).toBeNull();
  });

  it("explains a self-hosted server plainly and offers the bring-a-URL path", async () => {
    const stdio = detail({
      slug: "filesystem",
      name: "Filesystem",
      stdio_only: true,
      connectable: false,
      mcp_url: null,
      config_schema: null,
      setup_note: "Jhin does not spawn stdio servers. Host it yourself and connect over HTTPS.",
    });
    installServer(stdio);
    renderDialog(stdio);

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    // The developer-speak note is replaced with a plain line…
    expect(await within(dialog).findByText(/no hosted address/)).toBeDefined();
    expect(within(dialog).queryByText(/stdio/)).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "Connect" })).toBeNull();

    // …and a real action: point the MCP connector at a URL of your own.
    fireEvent.click(within(dialog).getByRole("button", { name: "I have a URL — connect it" }));
    const form = await screen.findByTestId("create-connection-form");
    expect((within(form).getByLabelText("Server URL") as HTMLInputElement).value).toBe("");
    expect((within(form).getByLabelText("Short name") as HTMLInputElement).value).toBe("filesystem");
  });

  it("offers a working Connect for an OAuth entry", async () => {
    // Jhin signs in with OAuth now, and Connect opens the panel that probes
    // the server for real. `auth_hint` is a library label, not a protocol
    // fact — it is wrong far more often than right — so nothing here routes
    // on it. Blocking on it disabled exactly the entries most likely to work.
    const oauth = detail({
      slug: "asana",
      name: "Asana",
      auth_hint: "oauth",
      auth_note: "Asana's remote server uses OAuth sign-in.",
      config_schema: null,
    });
    installServer(oauth);
    renderDialog(oauth);

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    const connect = await within(dialog).findByRole("button", { name: "Connect" });
    expect((connect as HTMLButtonElement).disabled).toBe(false);
    expect(connect.getAttribute("aria-describedby")).toBeNull();

    // An MCP entry keeps the schema-driven form: its server is what gets
    // probed, from the form's own URL, and the entry's contract fills it.
    fireEvent.click(connect);
    expect(await screen.findByTestId("create-connection-form")).toBeDefined();
    expect(screen.queryByTestId("connect-panel")).toBeNull();
  });

  it("connects a native app that signs in through the Connect panel", async () => {
    // GitHub from the catalog is the same GitHub as the library card: the
    // panel asks the server how it signs in, and the API key is the demoted
    // fallback rather than the only door.
    const github: ConnectorInfo = {
      ...MCP_CONNECTOR,
      connector_type: "github",
      display_name: "GitHub",
      icon: "github",
      auth_schemes: [
        { type: "oauth", label: "Sign in with GitHub", description: "", secret_fields: [] },
        {
          type: "pat",
          label: "Personal access token",
          description: "",
          secret_fields: [
            { name: "token", label: "Token", placeholder: "ghp_…", multiline: false, required: true },
          ],
        },
      ],
      config_fields: [],
    };
    const entry = detail({
      slug: "github",
      name: "GitHub",
      connector_type: "github",
      mcp_url: null,
      auth_hint: "oauth",
      config_schema: null,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        if (path === `/api/v1/catalog/entries/${entry.slug}` && method === "GET") return json(entry);
        if (path === "/api/v1/oauth/redirect-uri") {
          return json({
            redirect_uri: "https://jhin.example.com/api/v1/oauth/callback",
            github_app_redirect_uri: "https://jhin.example.com/api/v1/oauth/github-app/callback",
            is_https: true,
            is_loopback: false,
            configured_via: "APP_URL",
            github_app_available: true,
            github_app_permissions: {},
            preferred_sign_in: "redirect",
          });
        }
        if (path === "/api/v1/workspaces/workspace-1/oauth/probe" && method === "POST") {
          return json({
            method: "oauth_static",
            supports_oauth: true,
            supports_dcr: false,
            issuer: "https://github.com",
            authorization_server_display: "github.com",
            scopes: [],
            resource: "",
            client_configured: true,
            requires_client_secret: true,
            reason: "",
            redirect_flow: { available: true, reason: "" },
            device_flow: { available: true, reason: "" },
            app_settings_url: "https://github.com/settings/apps",
          });
        }
        throw new Error(`Unexpected request: ${method} ${path}`);
      }),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <WorkspaceProvider
          user={{
            id: "user-1",
            email: "ada@example.com",
            display_name: "Ada Lovelace",
            created_at: "2026-08-18T00:00:00Z",
          }}
          workspace={{
            workspace_id: "workspace-1",
            workspace_name: "Acme",
            workspace_slug: "acme",
            role: "owner",
          }}
        >
          <CatalogEntryDialog
            slug={entry.slug}
            workspaceId="workspace-1"
            connectors={[MCP_CONNECTOR, github]}
            onClose={vi.fn()}
            onCreated={vi.fn()}
          />
        </WorkspaceProvider>
      </QueryClientProvider>,
    );

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    fireEvent.click(await within(dialog).findByRole("button", { name: "Connect" }));
    expect(await screen.findByTestId("connect-panel")).toBeDefined();
    expect(await screen.findByTestId("oauth-consent-step")).toBeDefined();
    expect(screen.queryByTestId("create-connection-form")).toBeNull();
  });

  it("titles a package-named entry by its friendly name, package underneath", async () => {
    const packaged = detail({
      slug: "notion",
      name: "@notionhq/notion-mcp-server",
      summary: "Official MCP server for Notion API",
    });
    installServer(packaged);
    renderDialog(packaged);

    await screen.findByTestId("catalog-entry-dialog");
    const heading = screen.getByRole("heading", { name: "Notion MCP Server" });
    expect(heading).toBeDefined();
    expect(screen.getByText("@notionhq/notion-mcp-server")).toBeDefined();
  });

  it("shows one link when homepage and docs point at the same page", async () => {
    const same = detail({
      homepage: "https://github.com/makenotion/notion-mcp-server#readme",
      docs_url: "https://github.com/makenotion/notion-mcp-server#readme",
    });
    installServer(same);
    renderDialog(same);

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    await within(dialog).findByText("Docs");
    expect(within(dialog).queryByText("Homepage")).toBeNull();
    const dupes = within(dialog).getAllByRole("link", {
      name: "https://github.com/makenotion/notion-mcp-server#readme",
    });
    expect(dupes).toHaveLength(1);
  });

  it("issues no request beyond reading the entry until Connect is pressed", async () => {
    installServer(detail());
    renderDialog(detail());

    await screen.findByTestId("catalog-entry-dialog");
    const calls = vi.mocked(fetch).mock.calls.map(([input]) => String(input));

    expect(calls).toEqual(["/api/v1/catalog/entries/kestrel"]);
    expect(calls.some((path) => path.includes("mcp.example.com"))).toBe(false);
  });

  it("opens the create form prefilled from the schema, and only then writes", async () => {
    const writes = installServer(detail());
    renderDialog(detail());

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Connect" }));

    const form = await screen.findByTestId("create-connection-form");
    expect(within(form).getByTestId("schema-form")).toBeDefined();
    expect((within(form).getByLabelText("Server URL") as HTMLInputElement).value).toBe(
      "https://mcp.example.com/kestrel",
    );
    expect((within(form).getByLabelText("Short name") as HTMLInputElement).value).toBe("kestrel");
    expect(writes).toHaveLength(0);
  });

  it("posts the schema's values as config and only secrets as credentials", async () => {
    const writes = installServer(detail());
    const { onCreated } = renderDialog(detail());

    fireEvent.click(
      within(await screen.findByTestId("catalog-entry-dialog")).getByRole("button", {
        name: "Connect",
      }),
    );
    const form = await screen.findByTestId("create-connection-form");
    fireEvent.change(within(form).getByPlaceholderText("token…"), {
      target: { value: "kestrel-token" },
    });
    fireEvent.submit(form);

    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({
      connector_type: "mcp",
      name: "Kestrel",
      auth_type: "bearer",
      credentials: { token: "kestrel-token" },
      config: {
        server_url: "https://mcp.example.com/kestrel",
        server_slug: "kestrel",
        transport: "auto",
      },
    });
    expect(Object.keys(writes[0].credentials as object)).toEqual(["token"]);
    expect(writes[0].config).not.toHaveProperty("token");
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
  });

  it("closes when Cancel is pressed, without writing anything", async () => {
    const writes = installServer(detail());
    const { onClose } = renderDialog(detail());

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(onClose).toHaveBeenCalled();
    expect(writes).toHaveLength(0);
  });

  it("survives an entry whose contract the server could not build", async () => {
    const bare = detail({ config_schema: null });
    installServer(bare);
    renderDialog(bare);

    const dialog = await screen.findByTestId("catalog-entry-dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Connect" }));

    // Falls back to the manifest-driven form rather than refusing to open.
    const form = await screen.findByTestId("create-connection-form");
    expect(within(form).queryByTestId("schema-form")).toBeNull();
    expect((within(form).getByLabelText("Server URL") as HTMLInputElement).value).toBe(
      "https://mcp.example.com/kestrel",
    );
  });
});
