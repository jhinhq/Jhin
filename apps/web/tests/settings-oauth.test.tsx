/** Settings → OAuth: the redirect URL, how GitHub signs in, and the
 * registrations — with no secret shown and no one-off banner replayed. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import OAuthSettingsPage from "@/app/(app)/settings/oauth/page";
import type { OAuthClientOut, OAuthRedirectOut } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/settings/oauth");
});

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

function client(overrides: Partial<OAuthClientOut> = {}): OAuthClientOut {
  return {
    id: "reg-1",
    issuer: "https://github.com",
    redirect_uri: REDIRECT.redirect_uri,
    client_id: "Iv23lixxxxxxxxxxxxxx",
    client_secret_configured: true,
    token_endpoint_auth_method: "client_secret_post",
    source: "manual",
    scopes: "",
    created_at: "2026-08-29T12:00:00Z",
    last_used_at: null,
    connection_count: 0,
    ...overrides,
  };
}

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function installServer(redirect: OAuthRedirectOut, clients: OAuthClientOut[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      if (path === "/api/v1/oauth/redirect-uri") return json(redirect);
      if (path === "/api/v1/workspaces/workspace-1/oauth/clients" && method === "GET") {
        return json(clients);
      }
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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
        <OAuthSettingsPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

describe("OAuthSettingsPage", () => {
  it("says the browser sign-in comes first and needs nothing on GitHub", async () => {
    installServer(REDIRECT, [client()]);
    renderPage();
    const card = await screen.findByTestId("github-sign-in-order");
    expect(card.textContent).toContain("In the browser first.");
    expect(card.textContent).toContain("no setting on GitHub is needed");
    expect(card.textContent).toContain("the browser sign-in is offered in return");
    // The operator's registration reads exactly as before.
    expect(screen.getByText("Set up by an admin")).toBeTruthy();
    expect(screen.getByText("Secret stored")).toBeTruthy();
  });

  it("says when the operator put the code first, and what that needs on GitHub", async () => {
    installServer({ ...REDIRECT, preferred_sign_in: "device_code" }, []);
    renderPage();
    const card = await screen.findByTestId("github-sign-in-order");
    expect(card.textContent).toContain("With a sign-in code first, set by OAUTH_PREFER_DEVICE_CODE");
    expect(card.textContent).toContain("Enable Device Flow");
    expect(card.textContent).toContain("The browser sign-in stays available");
  });

  it("marks a GitHub registration with no secret as code-only", async () => {
    installServer(REDIRECT, [
      client({ client_secret_configured: false, token_endpoint_auth_method: "none" }),
      client({
        id: "reg-2",
        issuer: "https://auth.example.com",
        client_secret_configured: false,
        token_endpoint_auth_method: "none",
        source: "dcr",
      }),
    ]);
    renderPage();
    expect(await screen.findByText("No secret — sign-in code only")).toBeTruthy();
    // Every other public client keeps the plain wording.
    expect(screen.getByText("No secret needed")).toBeTruthy();
  });

  it("explains a loopback instance without telling it to give up on the browser", async () => {
    installServer(
      {
        ...REDIRECT,
        redirect_uri: "http://localhost:3000/api/v1/oauth/callback",
        is_https: false,
        is_loopback: true,
        github_app_available: false,
      },
      [],
    );
    renderPage();
    const note = await screen.findByText(/Plain HTTP is fine here because it is a loopback address/);
    expect(note.textContent).toContain("can still send it back to this machine");
    expect(note.textContent).toContain("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS");
    expect(note.textContent).not.toContain("best connected with a sign-in code");
    expect(
      screen.getByText(/Nothing registered yet\. Connect GitHub from Apps/),
    ).toBeTruthy();
  });

  it("no longer announces a created app here — that moment belongs to Apps", async () => {
    window.history.replaceState(null, "", "/settings/oauth?github_app=created");
    installServer(REDIRECT, []);
    renderPage();
    await screen.findByTestId("github-sign-in-order");
    expect(screen.queryByRole("status")).toBeNull();
    expect(document.body.textContent).not.toContain("Your GitHub App was created");
  });
});
