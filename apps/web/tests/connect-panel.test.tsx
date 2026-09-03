/** The connect panel: one Connect button, five ways it can land, and the
 * rule that an API key is never the first thing anybody is shown. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectPanel } from "@/components/connect/connect-panel";
import { ApiError } from "@/lib/api";
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
/** What the *second* probe answers — the one a saved registration triggers. */
let probeResultAfterSave: OAuthProbeOut | null = null;
let probeFails = false;
/** When set, the device start is refused with this, the way the API refuses. */
let deviceStartError: Error | null = null;
/** What `/oauth/redirect-uri` says about one-click GitHub App creation. */
let githubAppAvailable = true;

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
  const { useState } = await import("react");
  return {
    ...actual,
    useOAuthProbe: () => ({
      mutate: (
        body: unknown,
        handlers?: { onSuccess?: (r: OAuthProbeOut) => void; onError?: (e: unknown) => void },
      ) => {
        calls.probe.push(body);
        const answer =
          calls.probe.length > 1 && probeResultAfterSave !== null
            ? probeResultAfterSave
            : probeResult;
        if (probeFails || answer === null) handlers?.onError?.(new Error("probe failed"));
        else handlers?.onSuccess?.(answer);
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
    useOAuthDeviceStart: () => {
      // A real mutation re-renders its caller with `error` set after a
      // refusal; this one does the same, so the panel's refused layout is
      // exercised the way it happens.
      const [error, setError] = useState<Error | null>(null);
      return {
        mutate: (
          body: unknown,
          handlers?: {
            onSuccess?: (r: OAuthDeviceStartOut) => void;
            onError?: (e: unknown) => void;
          },
        ) => {
          calls.deviceStart.push(body);
          if (deviceStartError) {
            setError(deviceStartError);
            handlers?.onError?.(deviceStartError);
          } else {
            handlers?.onSuccess?.(deviceResult);
          }
        },
        isPending: false,
        error,
        isError: error !== null,
      };
    },
    useOAuthDevicePoll: () => ({ data: undefined, dataUpdatedAt: 0, isError: false, error: null }),
    useRedirectUri: () => ({
      data: {
        redirect_uri: "https://jhin.example.com/api/v1/oauth/callback",
        github_app_redirect_uri: "https://jhin.example.com/api/v1/oauth/github-app/callback",
        is_https: true,
        is_loopback: false,
        configured_via: "APP_URL",
        github_app_available: githubAppAvailable,
        github_app_permissions: { contents: "write", metadata: "read" },
        preferred_sign_in: "redirect",
      },
      isPending: false,
      isError: false,
      refetch: () => undefined,
    }),
    useGitHubAppManifest: () => ({ mutate: () => undefined, isPending: false, error: null }),
    useCreateOAuthClient: () => ({
      mutate: (_body: unknown, handlers?: { onSuccess?: (r: unknown) => void }) => {
        handlers?.onSuccess?.({});
      },
      isPending: false,
      error: null,
    }),
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
    redirect_flow: { available: true, reason: "" },
    device_flow: { available: false, reason: "no_device_endpoint" },
    app_settings_url: "",
    ...overrides,
  };
}

/** GitHub as the probe describes it: a static provider with both flows. */
function githubProbe(overrides: Partial<OAuthProbeOut> = {}): OAuthProbeOut {
  return probe({
    method: "oauth_static",
    supports_dcr: false,
    issuer: "https://github.com",
    authorization_server_display: "github.com",
    scopes: [],
    resource: "",
    client_configured: true,
    requires_client_secret: true,
    redirect_flow: { available: true, reason: "" },
    device_flow: { available: true, reason: "" },
    app_settings_url: "https://github.com/settings/apps",
    ...overrides,
  });
}

const GITHUB_CONNECTOR: ConnectorInfo = {
  ...MCP_CONNECTOR,
  connector_type: "github",
  display_name: "GitHub",
  docs_url: "https://docs.github.com/apps",
};

const DEVICE_REFUSED =
  "GitHub has device sign-in turned off for this app. Use the browser sign-in instead — it needs no change on GitHub.";

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
  probeResultAfterSave = null;
  probeFails = false;
  deviceStartError = null;
  githubAppAvailable = true;
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

  it("tells a GitHub app the code may be off, and where the working sign-in is", async () => {
    probeResult = githubProbe({ method: "device_code", scopes: ["repo"] });
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    await screen.findByRole("button", { name: "Connect Linear" });
    const hint = screen.getByTestId("device-github-hint").textContent ?? "";
    expect(hint).toContain("Enable Device Flow");
    expect(hint).toContain("the browser sign-in needs no change on GitHub");
    expect(hint).not.toContain("A GitHub App starts with it off");
  });

  it("names GitHub's checkbox only when no browser sign-in is possible", async () => {
    probeResult = githubProbe({
      method: "device_code",
      reason: "needs_client_secret",
      redirect_flow: { available: false, reason: "needs_client_secret" },
      scopes: ["repo"],
    });
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    await screen.findByRole("button", { name: "Connect Linear" });
    const hint = screen.getByTestId("device-github-hint").textContent ?? "";
    expect(hint).toContain("A GitHub App starts with it off");
    expect(hint).not.toContain("needs no change on GitHub");
  });

  it("goes to consent for a registered GitHub app, with the code one link away", async () => {
    probeResult = githubProbe();
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    const consent = await screen.findByTestId("oauth-consent-step");
    // A GitHub App has permissions, not scopes; the sentence says so.
    expect(consent.textContent).toContain("the permissions the app was registered with");
    expect(screen.getByRole("button", { name: /Continue to github.com/ })).toBeTruthy();
    // Where the app has to be installed, as a link the person may open.
    const hint = screen.getByTestId("github-install-hint");
    expect(hint.textContent).toContain("Jhin cannot see where the app is installed");
    const link = within(hint).getByRole("link", { name: /Open your GitHub Apps/ });
    expect(link.getAttribute("href")).toBe("https://github.com/settings/apps");
    expect(link.getAttribute("target")).toBe("_blank");
    // Nothing on GitHub needs changing, so nothing says it does.
    expect(consent.textContent).not.toContain("Enable Device Flow");

    fireEvent.click(screen.getByTestId("use-device-link"));
    expect(await screen.findByText("Sign in with a code")).toBeTruthy();
    expect(calls.navigate).toEqual([]);
  });

  it("offers no code link when the provider has no device endpoint", async () => {
    probeResult = githubProbe({ device_flow: { available: false, reason: "no_device_endpoint" } });
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    await screen.findByTestId("oauth-consent-step");
    expect(screen.queryByTestId("use-device-link")).toBeNull();
  });

  it("renders the install hint without a link when the address is not https", async () => {
    probeResult = githubProbe({ app_settings_url: "http://evil.example" });
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    await screen.findByTestId("oauth-consent-step");
    const hint = screen.getByTestId("github-install-hint");
    expect(hint.textContent).toContain("Open your GitHub Apps");
    expect(within(hint).queryByRole("link")).toBeNull();
  });

  it("offers the browser sign-in as a link beside the code when both work", async () => {
    probeResult = githubProbe({ method: "device_code" });
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    await screen.findByRole("button", { name: "Connect Linear" });
    fireEvent.click(screen.getByTestId("use-redirect-link"));
    expect(await screen.findByTestId("oauth-consent-step")).toBeTruthy();
    expect(screen.queryByTestId("add-secret-link")).toBeNull();
  });

  it("offers to add the secret when that is what the browser sign-in is missing", async () => {
    probeResult = githubProbe({
      method: "device_code",
      reason: "needs_client_secret",
      redirect_flow: { available: false, reason: "needs_client_secret" },
    });
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    await screen.findByRole("button", { name: "Connect Linear" });
    expect(screen.queryByTestId("use-redirect-link")).toBeNull();
    fireEvent.click(screen.getByTestId("add-secret-link"));
    const form = await screen.findByTestId("oauth-client-form");
    expect(screen.getByTestId("oauth-client-form-intro").textContent).toContain(
      "github.com needs a client secret for the browser sign-in and none is stored",
    );
    expect(form.textContent).toContain("Save replaces the stored pair");
  });

  it("offers neither browser link when no client is registered at all", async () => {
    probeResult = githubProbe({
      method: "device_code",
      redirect_flow: { available: false, reason: "needs_client_credentials" },
    });
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    await screen.findByRole("button", { name: "Connect Linear" });
    expect(screen.queryByTestId("use-redirect-link")).toBeNull();
    expect(screen.queryByTestId("add-secret-link")).toBeNull();
  });

  it("turns a refused code into the browser sign-in, one click, when that works", async () => {
    probeResult = githubProbe({ method: "device_code" });
    deviceStartError = new ApiError(400, DEVICE_REFUSED);
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    fireEvent.click(await screen.findByRole("button", { name: "Connect Linear" }));
    await waitFor(() => expect(calls.deviceStart).toHaveLength(1));

    // The API's sentence, verbatim — and the way out beside it.
    expect(await screen.findByText(DEVICE_REFUSED)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try the code again" })).toBeTruthy();
    fireEvent.click(screen.getByTestId("device-refused-use-redirect"));
    expect(await screen.findByTestId("oauth-consent-step")).toBeTruthy();
    // Nobody was sent to a checkbox.
    expect(document.body.textContent).not.toContain("Enable Device Flow");
  });

  it("keeps the retry, not the browser button, when no browser sign-in is possible", async () => {
    probeResult = githubProbe({
      method: "device_code",
      reason: "needs_client_secret",
      redirect_flow: { available: false, reason: "needs_client_secret" },
    });
    deviceStartError = new ApiError(
      400,
      'GitHub has device sign-in turned off for this app. In the app\'s settings on GitHub, turn on "Enable Device Flow", save, and try again.',
    );
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    fireEvent.click(await screen.findByRole("button", { name: "Connect Linear" }));
    await screen.findByRole("button", { name: "Try the code again" });
    expect(screen.queryByTestId("device-refused-use-redirect")).toBeNull();
    expect(screen.getByTestId("add-secret-link")).toBeTruthy();
  });

  it("asks the server again after a registration is saved, rather than deciding itself", async () => {
    probeResult = githubProbe({
      method: "oauth_needs_client",
      client_configured: false,
      reason: "needs_client_credentials",
      redirect_flow: { available: false, reason: "needs_client_credentials" },
      device_flow: { available: false, reason: "needs_client_credentials" },
    });
    probeResultAfterSave = githubProbe();
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    const form = await screen.findByTestId("oauth-client-form");
    // One-click creation is on, so the card comes first; the paste form is below it.
    expect(screen.getByTestId("github-app-manifest-card")).toBeTruthy();
    // Step 1 opens the person's own GitHub Apps page.
    expect(within(form).getByRole("link", { name: /app settings/ }).getAttribute("href")).toBe(
      "https://github.com/settings/apps",
    );
    expect(screen.getByTestId("oauth-client-form-permissions").textContent).toContain(
      "Contents (read & write), Metadata (read)",
    );

    fireEvent.change(within(form).getByPlaceholderText("Client ID from the provider"), {
      target: { value: "Iv23lixxxxxxxxxxxxxx" },
    });
    fireEvent.submit(form);

    await waitFor(() => expect(calls.probe).toHaveLength(2));
    expect(await screen.findByTestId("oauth-consent-step")).toBeTruthy();
  });

  it("explains the by-hand setup when one-click creation is off for this origin", async () => {
    githubAppAvailable = false;
    probeResult = githubProbe({
      method: "oauth_needs_client",
      client_configured: false,
      reason: "needs_client_credentials",
      redirect_flow: { available: false, reason: "needs_client_credentials" },
      device_flow: { available: false, reason: "needs_client_credentials" },
    });
    renderPanel(vi.fn(), vi.fn(), GITHUB_CONNECTOR);
    const note = await screen.findByTestId("github-app-by-hand");
    expect(note.textContent).toContain("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS");
    expect(note.textContent).toContain("Contents (read & write)");
    expect(screen.queryByTestId("github-app-manifest-card")).toBeNull();
    expect(screen.getByTestId("oauth-client-form")).toBeTruthy();
  });

  it("has no API-key link, and no API-key fallback, for an app that only signs in", async () => {
    const signInOnly: ConnectorInfo = {
      ...GITHUB_CONNECTOR,
      auth_schemes: GITHUB_CONNECTOR.auth_schemes.filter((scheme) => scheme.type === "oauth"),
    };
    probeResult = githubProbe();
    renderPanel(vi.fn(), vi.fn(), signInOnly);
    await screen.findByTestId("oauth-consent-step");
    expect(screen.queryByRole("button", { name: "Use an API key instead" })).toBeNull();
    cleanup();

    probeFails = true;
    renderPanel(vi.fn(), vi.fn(), signInOnly);
    expect(await screen.findByText(/offers no other way to connect/)).toBeTruthy();
    expect(screen.queryByTestId("create-connection-form")).toBeNull();
    expect(screen.queryByRole("button", { name: "Use an API key instead" })).toBeNull();
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
