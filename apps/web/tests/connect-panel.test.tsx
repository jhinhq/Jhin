/** The connect panel: one Connect button, five ways it can land, and the
 * rule that an API key is never the first thing anybody is shown. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectPanel } from "@/components/connect/connect-panel";
import type {
  ConnectorInfo,
  OAuthDeviceStartOut,
  OAuthProbeOut,
  OAuthStartOut,
} from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

/** What the panel calls, recorded rather than performed. */
const calls = {
  probe: [] as unknown[],
  start: [] as unknown[],
  deviceStart: [] as unknown[],
  navigate: [] as string[],
};

let probeResult: OAuthProbeOut | null = null;
let probeFails = false;

const startResult: OAuthStartOut = {
  authorization_url: "https://auth.example.com/authorize?client_id=abc&state=xyz",
  state_expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  issuer: "https://auth.example.com",
  scopes: ["read", "write"],
  resource: "https://mcp.example.com/mcp",
  authorized_as_user_id: "user-1",
  client_source: "dcr",
};

const deviceResult: OAuthDeviceStartOut = {
  handle: "handle-1",
  user_code: "wdjbmjht",
  verification_uri: "https://github.com/login/device",
  verification_uri_complete: null,
  expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
  interval_seconds: 5,
};

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useOAuthProbe: () => ({
      mutate: (
        body: unknown,
        handlers?: { onSuccess?: (r: OAuthProbeOut) => void; onError?: (e: unknown) => void },
      ) => {
        calls.probe.push(body);
        if (probeFails || probeResult === null) handlers?.onError?.(new Error("probe failed"));
        else handlers?.onSuccess?.(probeResult);
      },
      isPending: false,
      error: null,
    }),
    useOAuthStart: () => ({
      mutate: (body: unknown, handlers?: { onSuccess?: (r: OAuthStartOut) => void }) => {
        calls.start.push(body);
        handlers?.onSuccess?.(startResult);
      },
      isPending: false,
      error: null,
    }),
    useOAuthDeviceStart: () => ({
      mutate: (body: unknown, handlers?: { onSuccess?: (r: OAuthDeviceStartOut) => void }) => {
        calls.deviceStart.push(body);
        handlers?.onSuccess?.(deviceResult);
      },
      isPending: false,
      error: null,
    }),
    useOAuthDevicePoll: () => ({ data: undefined, dataUpdatedAt: 0, isError: false }),
    useRedirectUri: () => ({
      data: {
        redirect_uri: "https://jhin.example.com/api/v1/oauth/callback",
        github_app_redirect_uri: "https://jhin.example.com/api/v1/oauth/github-app/callback",
        is_https: true,
        is_loopback: false,
        configured_via: "APP_URL",
      },
      isPending: false,
      isError: false,
      refetch: () => undefined,
    }),
    useGitHubAppManifest: () => ({ mutate: () => undefined, isPending: false, error: null }),
    useCreateOAuthClient: () => ({ mutate: () => undefined, isPending: false, error: null }),
  };
});

vi.mock("@/lib/oauth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/oauth")>();
  return {
    ...actual,
    navigateToProvider: (url: string) => calls.navigate.push(url),
  };
});

const MCP_CONNECTOR: ConnectorInfo = {
  connector_type: "mcp",
  display_name: "Any MCP server",
  icon: "mcp",
  description: "Connect any MCP server.",
  auth_schemes: [
    { type: "none", label: "No authentication", description: "", secret_fields: [] },
    // The manifest's own sign-in scheme is what makes this connector worth
    // probing at all; a connector without one never asks the server.
    { type: "oauth", label: "Sign in", description: "", secret_fields: [] },
    {
      type: "bearer",
      label: "Bearer token",
      description: "",
      secret_fields: [
        {
          name: "token",
          label: "Access token",
          placeholder: "token…",
          multiline: false,
          required: true,
        },
      ],
    },
  ],
  config_fields: [
    {
      name: "server_url",
      label: "Server URL",
      required: true,
      placeholder: "https://…",
      help: "",
      kind: "text",
      auth_types: [],
      default: null,
      minimum: null,
      maximum: null,
    },
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

function probe(overrides: Partial<OAuthProbeOut> = {}): OAuthProbeOut {
  return {
    method: "oauth_discovery",
    supports_oauth: true,
    supports_dcr: true,
    issuer: "https://auth.example.com",
    authorization_server_display: "auth.example.com",
    scopes: ["read", "write"],
    resource: "https://mcp.example.com/mcp",
    client_configured: false,
    requires_client_secret: false,
    reason: "",
    ...overrides,
  };
}

function renderPanel(onConnected = vi.fn(), onClose = vi.fn(), connector = MCP_CONNECTOR) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
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
        <ConnectPanel
          workspaceId="workspace-1"
          connector={connector}
          prefill={{
            name: "Linear",
            authType: "bearer",
            config: { server_url: "https://mcp.example.com/mcp", server_slug: "linear" },
          }}
          onClose={onClose}
          onConnected={onConnected}
        />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
  return { onConnected, onClose };
}

beforeEach(() => {
  calls.probe = [];
  calls.start = [];
  calls.deviceStart = [];
  calls.navigate = [];
  probeResult = probe();
  probeFails = false;
});

afterEach(() => {
  cleanup();
});

describe("ConnectPanel", () => {
  it("asks the server how the app signs in rather than guessing", async () => {
    renderPanel();
    await waitFor(() => expect(calls.probe).toHaveLength(1));
    expect(calls.probe[0]).toEqual({
      connector_type: "mcp",
      server_url: "https://mcp.example.com/mcp",
    });
  });

  it("goes straight to consent when discovery and registration are both available", async () => {
    renderPanel();
    const consent = await screen.findByTestId("oauth-consent-step");
    expect(consent.textContent).toContain("auth.example.com");
    expect(consent.textContent).toContain("read and write");
    // The permission is a person's, and the card says whose.
    expect(consent.textContent).toContain("Ada Lovelace");
    expect(screen.queryByTestId("create-connection-form")).toBeNull();
  });

  it("hands the browser to the provider, and only to the URL the API built", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /Continue to auth.example.com/ }));
    await waitFor(() => expect(calls.navigate).toEqual([startResult.authorization_url]));
    expect(calls.start[0]).toMatchObject({ connector_type: "mcp", name: "Linear" });
    // Where we were is remembered so the callback can bring us back.
    expect(window.sessionStorage.getItem("jhin.oauth.return")).not.toBeNull();
  });

  it("offers the API key as a demoted link, never as the default", async () => {
    renderPanel();
    await screen.findByTestId("oauth-consent-step");
    const link = screen.getByRole("button", { name: "Use an API key instead" });
    // Present, but plainly not the primary action on the card.
    expect(link.className).toContain("text-faint");
    expect(screen.queryByTestId("create-connection-form")).toBeNull();

    fireEvent.click(link);
    const form = await screen.findByTestId("create-connection-form");
    expect(form).toBeTruthy();
    // Taking the link anyway says what it costs.
    expect(screen.getByTestId("api-key-fallback-note").textContent).toContain(
      "supports connecting without a key",
    );
    // And it is reversible.
    expect(screen.getByRole("button", { name: "Back" })).toBeTruthy();
  });

  it("asks for a client id once when the server will not register one", async () => {
    probeResult = probe({
      method: "oauth_needs_client",
      supports_dcr: false,
      requires_client_secret: true,
    });
    renderPanel();
    const form = await screen.findByTestId("oauth-client-form");
    expect(form.textContent).toContain("one-time setup for your whole workspace");
    // The redirect URL a provider will demand, verbatim and copyable.
    expect(form.textContent).toContain("https://jhin.example.com/api/v1/oauth/callback");
    expect(screen.getByRole("button", { name: "Copy Redirect URL to paste there" })).toBeTruthy();
  });

  it("asks for a client id first when the device flow has none", async () => {
    // `/oauth/device/start` answers 409 without a registered client, so the
    // panel asks for it up front rather than after a sign-in method is picked.
    probeResult = probe({
      method: "device_code",
      supports_dcr: false,
      client_configured: false,
      scopes: ["repo"],
    });
    renderPanel();
    expect(await screen.findByTestId("oauth-client-form")).toBeTruthy();
    expect(screen.queryByTestId("device-code-panel")).toBeNull();
  });

  it("warns a GitHub app that the device flow starts switched off, and only GitHub", async () => {
    probeResult = probe({
      method: "device_code",
      supports_dcr: false,
      client_configured: true,
      scopes: ["repo"],
    });
    renderPanel(vi.fn(), vi.fn(), {
      ...MCP_CONNECTOR,
      connector_type: "github",
      display_name: "GitHub",
    });
    await screen.findByRole("button", { name: "Connect Linear" });
    expect(screen.getByTestId("device-github-hint").textContent).toContain("Enable Device Flow");
  });

  it("says nothing about GitHub's checkbox for any other app", async () => {
    probeResult = probe({
      method: "device_code",
      supports_dcr: false,
      client_configured: true,
      scopes: ["repo"],
    });
    renderPanel();
    await screen.findByRole("button", { name: "Connect Linear" });
    expect(screen.queryByTestId("device-github-hint")).toBeNull();
  });

  it("explains the device flow before starting it, then shows the code", async () => {
    probeResult = probe({
      method: "device_code",
      supports_dcr: false,
      client_configured: true,
      scopes: ["repo"],
    });
    renderPanel();
    const startButton = await screen.findByRole("button", { name: "Connect Linear" });
    expect(screen.queryByTestId("device-code-panel")).toBeNull();

    fireEvent.click(startButton);
    await waitFor(() => expect(calls.deviceStart).toHaveLength(1));
    expect((await screen.findByTestId("device-user-code")).textContent).toContain("WDJB-MJHT");
    // A code, not a redirect: nothing left the tab.
    expect(calls.navigate).toEqual([]);
  });

  it("falls back to the API key form when the app has no sign-in flow", async () => {
    probeResult = probe({ method: "api_key", supports_oauth: false, supports_dcr: false });
    renderPanel();
    expect(await screen.findByTestId("create-connection-form")).toBeTruthy();
    // Nothing to go back to, so nothing pretends there is.
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    expect(screen.queryByTestId("api-key-fallback-note")).toBeNull();
  });

  it("never costs somebody the connection when the probe itself fails", async () => {
    probeFails = true;
    renderPanel();
    expect(await screen.findByTestId("create-connection-form")).toBeTruthy();
  });

  it("never asks the server about a connector that cannot sign in", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
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
          <ConnectPanel
            workspaceId="workspace-1"
            connector={{
              ...MCP_CONNECTOR,
              auth_schemes: MCP_CONNECTOR.auth_schemes.filter(
                (scheme) => scheme.type !== "oauth",
              ),
            }}
            prefill={{ name: "Legacy", config: { server_url: "https://mcp.example.com/mcp" } }}
            onClose={vi.fn()}
            onConnected={vi.fn()}
          />
        </WorkspaceProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("create-connection-form")).toBeTruthy();
    expect(calls.probe).toHaveLength(0);
  });

  it("skips the probe entirely when there is no server to ask", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
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
          <ConnectPanel
            workspaceId="workspace-1"
            connector={MCP_CONNECTOR}
            onClose={vi.fn()}
            onConnected={vi.fn()}
          />
        </WorkspaceProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("create-connection-form")).toBeTruthy();
    expect(calls.probe).toHaveLength(0);
  });
});
