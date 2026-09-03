/** The API-key dialog: only schemes a person can fill in are offered. A
 * sign-in scheme has no fields, and a form that stored an empty credential
 * for one would call it a connection. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CreateConnectionDialog } from "@/components/connection-create-dialog";
import type { ConnectorInfo } from "@/lib/types";

afterEach(() => {
  cleanup();
});

const GITHUB: ConnectorInfo = {
  connector_type: "github",
  display_name: "GitHub",
  icon: "github",
  description: "Source control.",
  auth_schemes: [
    { type: "oauth", label: "Sign in with GitHub", description: "", secret_fields: [] },
    { type: "device", label: "Sign in with a device code", description: "", secret_fields: [] },
    {
      type: "github_app",
      label: "GitHub App",
      description: "",
      secret_fields: [
        { name: "app_id", label: "App ID", placeholder: "", multiline: false, required: true },
        {
          name: "private_key",
          label: "Private key",
          placeholder: "",
          multiline: true,
          required: true,
        },
      ],
    },
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

function renderDialog(connector: ConnectorInfo, prefillAuthType?: string) {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <CreateConnectionDialog
        workspaceId="workspace-1"
        connector={connector}
        prefill={prefillAuthType ? { authType: prefillAuthType } : undefined}
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

describe("CreateConnectionDialog", () => {
  it("lists only the schemes with something to type, and defaults to the first", () => {
    renderDialog(GITHUB);
    const form = screen.getByTestId("create-connection-form");
    const select = within(form).getByDisplayValue("GitHub App") as HTMLSelectElement;
    expect(select.value).toBe("github_app");
    const labels = Array.from(select.options).map((option) => option.textContent);
    expect(labels).toEqual(["GitHub App", "Personal access token"]);
    expect(labels).not.toContain("Sign in with GitHub");
    expect(labels).not.toContain("Sign in with a device code");
  });

  it("ignores a prefilled sign-in scheme rather than selecting one it cannot show", () => {
    renderDialog(GITHUB, "oauth");
    const form = screen.getByTestId("create-connection-form");
    expect((within(form).getByDisplayValue("GitHub App") as HTMLSelectElement).value).toBe(
      "github_app",
    );
  });

  it("falls back to the full list for a connector that declares nothing else", () => {
    const signInOnly: ConnectorInfo = {
      ...GITHUB,
      auth_schemes: GITHUB.auth_schemes.filter((scheme) => scheme.type === "oauth"),
    };
    renderDialog(signInOnly);
    const form = screen.getByTestId("create-connection-form");
    expect(within(form).getByDisplayValue("Sign in with GitHub")).toBeTruthy();
  });
});
