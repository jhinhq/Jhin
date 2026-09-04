/** The recovery card: what happened, and the one click that fixes it.
 *
 * The point of this component is that somebody who was bounced back from a
 * provider does not have to go and find the app they were connecting. So the
 * assertions here are mostly about the *control*, not the sentence: which
 * button is offered, that it is never disabled, and that a connection that
 * already exists gets the product's one Reconnect implementation rather than
 * a second copy of it. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OAuthLandingCard } from "@/components/connect/oauth-landing";
import { oauthLanding } from "@/lib/oauth";
import type { ConnectionInfo, ConnectorInfo } from "@/lib/types";

afterEach(cleanup);

const CONNECTOR: ConnectorInfo = {
  connector_type: "github",
  display_name: "GitHub",
  icon: "github",
  description: "Source control.",
  auth_schemes: [],
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

const CONNECTION = {
  id: "connection-1",
  public_id: "a".repeat(32),
  workspace_id: "workspace-1",
  connector_type: "github",
  name: "GitHub",
  status: "needs_reauth",
  auth_type: "oauth",
  config_json: {},
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
  last_verified_at: null,
  last_error: null,
  needs_reauth: true,
} as unknown as ConnectionInfo;

function renderCard(overrides: Partial<Parameters<typeof OAuthLandingCard>[0]> = {}) {
  const props = {
    copy: oauthLanding("expired")!,
    connector: null,
    connection: null,
    workspaceId: "workspace-1",
    onRetry: vi.fn(),
    onBrowse: vi.fn(),
    onDismiss: vi.fn(),
    ...overrides,
  };
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <OAuthLandingCard {...props} />
    </QueryClientProvider>,
  );
  return props;
}

describe("OAuthLandingCard", () => {
  it("offers the library when it does not know which app was being connected", () => {
    const props = renderCard();

    expect(screen.getByTestId("oauth-landing")).toBeTruthy();
    expect(screen.getByTestId("oauth-landing").getAttribute("role")).toBe("status");
    fireEvent.click(screen.getByTestId("oauth-landing-browse"));
    expect(props.onBrowse).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("oauth-landing-retry")).toBeNull();
  });

  it("offers the app by name when the callback said which one", () => {
    const props = renderCard({ connector: CONNECTOR });

    const retry = screen.getByTestId("oauth-landing-retry");
    expect(retry.textContent).toContain("Connect GitHub again");
    fireEvent.click(retry);
    expect(props.onRetry).toHaveBeenCalledWith(CONNECTOR);
  });

  it("uses the product's one Reconnect button when the connection already exists", () => {
    renderCard({ connector: CONNECTOR, connection: CONNECTION });

    expect(screen.getByTestId("reconnect-GitHub")).toBeTruthy();
    // Not a second implementation of the same mutation beside it.
    expect(screen.queryByTestId("oauth-landing-retry")).toBeNull();
    expect(screen.getByText(/is still set up — only its sign-in needs redoing/)).toBeTruthy();
  });

  it("names the log event an operator would grep, and not for a declined request", () => {
    renderCard();
    expect(screen.getByText("oauth.callback_refused")).toBeTruthy();
    cleanup();

    renderCard({ copy: oauthLanding("denied")! });
    expect(screen.queryByText("oauth.callback_refused")).toBeNull();
  });

  it("can be dismissed", () => {
    const props = renderCard();
    fireEvent.click(screen.getByTestId("oauth-landing-dismiss"));
    expect(props.onDismiss).toHaveBeenCalledTimes(1);
  });

  it("never renders a disabled button in any state", () => {
    for (const overrides of [
      {},
      { connector: CONNECTOR },
      { connector: CONNECTOR, connection: CONNECTION },
    ]) {
      renderCard(overrides);
      for (const button of screen.getAllByRole("button")) {
        expect((button as HTMLButtonElement).disabled).toBe(false);
      }
      cleanup();
    }
  });
});
