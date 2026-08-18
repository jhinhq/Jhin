/** Component tests: connectors gallery (plan 17.9). */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConnectorsGallery } from "@/components/connectors-gallery";
import type { ConnectorInfo } from "@/lib/types";

afterEach(cleanup);

const GITHUB: ConnectorInfo = {
  connector_type: "github",
  display_name: "GitHub",
  icon: "github",
  description: "Repositories, branches, pull requests, issues, and Actions.",
  auth_schemes: [
    { type: "pat", label: "Personal access token", description: "", secret_fields: [] },
    { type: "github_app", label: "GitHub App", description: "", secret_fields: [] },
  ],
  config_fields: [],
  webhook_events: ["issues", "pull_request", "push"],
  canonical_events: [],
  capabilities: ["github.repository.read", "github.branch.create"],
  supports_webhooks: true,
  webhook_secret_mode: "generated",
  webhook_signature_algorithm: "hmac-sha256",
  webhook_setup_help: "Store the generated secret.",
  docs_url: "",
};

describe("ConnectorsGallery", () => {
  it("renders live connectors with a connect action for admins", () => {
    const onConnect = vi.fn();
    render(<ConnectorsGallery connectors={[GITHUB]} canManage onConnect={onConnect} />);

    expect(screen.getByText("GitHub")).toBeDefined();
    expect(screen.getByText("live")).toBeDefined();
    expect(screen.getByText(/Personal access token · GitHub App/)).toBeDefined();
    expect(screen.getByText(/2 capabilities · webhooks/)).toBeDefined();

    screen.getByRole("button", { name: "Connect" }).click();
    expect(onConnect).toHaveBeenCalledWith(GITHUB);
  });

  it("hides the connect action for non-admins", () => {
    render(<ConnectorsGallery connectors={[GITHUB]} canManage={false} onConnect={() => {}} />);
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
  });

  it("shows upcoming connectors greyed out with their phase", () => {
    render(<ConnectorsGallery connectors={[GITHUB]} canManage onConnect={() => {}} />);
    expect(screen.getByText("HTTP")).toBeDefined();
    // CLI is live since Phase 6 — no longer in the upcoming list.
    expect(screen.queryByText("CLI")).toBeNull();
    expect(screen.getAllByText(/Future work/).length).toBe(1);
  });

  it("renders Linear, Vercel, and Supabase exactly once when supplied live", () => {
    const live = ["linear", "vercel", "supabase"].map((connector_type) => ({
      ...GITHUB,
      connector_type,
      display_name: connector_type[0].toUpperCase() + connector_type.slice(1),
    }));
    render(<ConnectorsGallery connectors={live} canManage onConnect={() => {}} />);
    for (const name of ["Linear", "Vercel", "Supabase"]) {
      expect(screen.getAllByText(name)).toHaveLength(1);
    }
  });
});
