/** Apps page: what the GitHub App handshake and GitHub's own install return
 * leave in the address bar, read once, scrubbed, and turned into the next
 * step — Connect GitHub, open by itself. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AppsPage from "@/app/(app)/apps/page";
import type { CatalogApp, ConnectorInfo, OAuthProbeOut, OAuthRedirectOut } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/apps");
  window.sessionStorage.clear();
});

const GITHUB_CONNECTOR: ConnectorInfo = {
  connector_type: "github",
  display_name: "GitHub",
  icon: "github",
  description: "Source control.",
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
  webhook_events: [],
  canonical_events: [],
  capabilities: [],
  supports_webhooks: false,
  webhook_secret_mode: "none",
  webhook_signature_algorithm: "",
  webhook_setup_help: "",
  docs_url: "",
};

const CATALOG: CatalogApp[] = [
  {
    slug: "github",
    name: "GitHub",
    category: "Developer tools",
    icon: "github",
    description: "Repositories.",
    connector_type: "github",
    mcp_url: null,
    url_unverified: false,
    transport: "unknown",
    auth_hint: "oauth",
    auth_note: "",
    docs_url: "",
    setup_note: "",
    stdio_only: false,
    connector_config: {},
  },
];

const PROBE: OAuthProbeOut = {
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
};

const REDIRECT: OAuthRedirectOut = {
  redirect_uri: "https://jhin.example.com/api/v1/oauth/callback",
  github_app_redirect_uri: "https://jhin.example.com/api/v1/oauth/github-app/callback",
  is_https: true,
  is_loopback: false,
  configured_via: "APP_URL",
  github_app_available: true,
  github_app_permissions: { contents: "write", metadata: "read" },
  preferred_sign_in: "redirect",
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function installServer() {
  const probes: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path === "/api/v1/connectors") return json([GITHUB_CONNECTOR]);
      if (path === "/api/v1/connectors/catalog") return json(CATALOG);
      if (path === "/api/v1/workspaces/workspace-1/connections" && method === "GET") return json([]);
      if (path === "/api/v1/oauth/redirect-uri") return json(REDIRECT);
      if (path === "/api/v1/workspaces/workspace-1/oauth/probe" && method === "POST") {
        probes.push(String(init?.body));
        return json(PROBE);
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );
  return probes;
}

function renderPage(search: string) {
  window.history.replaceState(null, "", `/apps${search}`);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceProvider
        user={{
          id: "user-1",
          email: "owner@example.com",
          display_name: "Owner",
          created_at: "2026-08-18T00:00:00Z",
        }}
        workspace={{
          workspace_id: "workspace-1",
          workspace_name: "Acme",
          workspace_slug: "acme",
          role: "owner",
        }}
      >
        <AppsPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

describe("AppsPage after the GitHub App handshake", () => {
  it("announces the created app, scrubs the flag, and opens Connect GitHub once", async () => {
    const probes = installServer();
    renderPage("?github_app=created");

    expect((await screen.findByTestId("github-app-banner")).textContent).toContain(
      "Your GitHub App was created. Sign in with it to connect GitHub.",
    );
    await waitFor(() => expect(window.location.search).toBe(""));
    // The next step, open by itself: the consent step for the app just made.
    expect(await screen.findByTestId("oauth-consent-step")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Connect GitHub" })).toBeTruthy();
    await waitFor(() => expect(probes).toHaveLength(1));
    expect(probes[0]).toContain('"connector_type":"github"');
  });

  it("does the same after GitHub's own install return, and never reads the installation id", async () => {
    installServer();
    renderPage("?installation_id=123456&setup_action=install");

    expect((await screen.findByTestId("github-app-banner")).textContent).toContain(
      "Now connect GitHub to sign in with the app you installed.",
    );
    expect(await screen.findByTestId("oauth-consent-step")).toBeTruthy();
    await waitFor(() => expect(window.location.search).toBe(""));
    expect(document.body.textContent).not.toContain("123456");
  });

  it("says what to do when GitHub did not finish creating the app", async () => {
    installServer();
    renderPage("?github_app=failed");

    expect(
      await screen.findByText(/GitHub did not finish creating the app/),
    ).toBeTruthy();
    expect(screen.queryByTestId("github-app-banner")).toBeNull();
    expect(screen.queryByTestId("oauth-consent-step")).toBeNull();
  });

  it("explains a callback URL the app does not list", async () => {
    installServer();
    renderPage("?oauth_error=callback_mismatch");

    expect(
      await screen.findByText(/callback URL listed on the app is not this instance's redirect URL/),
    ).toBeTruthy();
    expect(screen.queryByTestId("oauth-consent-step")).toBeNull();
  });
});
