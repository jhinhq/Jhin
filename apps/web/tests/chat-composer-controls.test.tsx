/** The controls on the chat bar: model / mode / tools / cost, with the
 * admin-only mutations degrading to a stated reason for everyone else. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatComposerControls } from "@/components/chat/composer-controls";
import { api } from "@/lib/api";
import {
  useAgent,
  useAgentGrants,
  useAgentPolicy,
  useConnections,
  useModelProfiles,
  useTools,
} from "@/lib/hooks";
import type { Agent, AgentPolicy, Grant, ModelProfile, ToolInfo } from "@/lib/types";

const invalidateAccess = vi.fn();

vi.mock("@/lib/hooks", () => ({
  useAgent: vi.fn(),
  useAgentGrants: vi.fn(),
  useAgentPolicy: vi.fn(),
  useConnections: vi.fn(),
  useModelProfiles: vi.fn(),
  useTools: vi.fn(),
  useInvalidateAgentAccess: () => invalidateAccess,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function profile(id: string, name: string): ModelProfile {
  return {
    id,
    workspace_id: "w1",
    provider_id: "p1",
    model_name: name,
    display_name: name,
    context_window: null,
    input_cost_micros_per_million: null,
    output_cost_micros_per_million: null,
    price_source: null,
    supports_tools: true,
    supports_reasoning: false,
    config_json: {},
    created_at: "",
    updated_at: "",
  } as ModelProfile;
}

function grant(id: string, capability: string): Grant {
  return {
    id,
    agent_id: "a1",
    capability,
    scope_json: {},
    effect: "allow",
    created_at: "2026-08-23T00:00:00Z",
  };
}

function tool(name: string, scopeKeys: string[] = []): ToolInfo {
  return {
    name,
    description: "",
    risk: "read",
    required_capability: name,
    supports_approval: true,
    scope_keys: scopeKeys,
    required_grant_scope_keys: [],
    input_schema: {},
  };
}

/** Only the web tools are in the catalog, so "Web search & browsing" is the
 * one capability that can be switched on from here. */
const CATALOG: ToolInfo[] = [tool("web.search"), tool("web.fetch", ["domain"])];

function setup({
  isAdmin = true,
  modelProfileId = "m1" as string | null,
  preset = "balanced" as AgentPolicy["preset"],
  grants = [grant("g1", "github.repository.read")],
  usage = { inputTokens: 11_400, outputTokens: 1_000, costMicros: 30_000 } as
    | { inputTokens: number; outputTokens: number; costMicros: number }
    | null,
} = {}) {
  vi.mocked(useAgent).mockReturnValue({
    data: { id: "a1", name: "Scout", model_profile_id: modelProfileId } as Agent,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useAgent>);
  vi.mocked(useModelProfiles).mockReturnValue({
    data: [profile("m1", "Sonnet"), profile("m2", "Opus")],
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useModelProfiles>);
  vi.mocked(useAgentPolicy).mockReturnValue({
    data: { rules: [], preset, autonomy_level: "supervised" },
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useAgentPolicy>);
  vi.mocked(useAgentGrants).mockReturnValue({
    data: grants,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useAgentGrants>);
  vi.mocked(useTools).mockReturnValue({
    data: CATALOG,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useTools>);
  vi.mocked(useConnections).mockReturnValue({
    data: [],
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useConnections>);

  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ChatComposerControls
        workspaceId="w1"
        agentId="a1"
        agentName="Scout"
        isAdmin={isAdmin}
        usage={usage}
      />
    </QueryClientProvider>,
  );
}

function open(chip: "model" | "mode" | "tools" | "usage") {
  fireEvent.click(screen.getByTestId(`composer-${chip}-trigger`));
  return screen.getByTestId(`composer-${chip}-panel`);
}

describe("ChatComposerControls", () => {
  it("puts the current model, mode, tool count and cost on the bar", () => {
    setup();
    expect(screen.getByTestId("composer-model-trigger").getAttribute("aria-label")).toBe(
      "Model: Sonnet",
    );
    expect(screen.getByTestId("composer-mode-trigger").getAttribute("aria-label")).toBe(
      "Mode: Balanced",
    );
    expect(screen.getByTestId("composer-tools-trigger").getAttribute("aria-label")).toBe(
      "Tools: 1 tool",
    );
    expect(screen.getByTestId("composer-usage-trigger").getAttribute("aria-label")).toBe(
      "This chat: 12.4k · $0.03",
    );
  });

  it("opens each menu upward, without a modal, so the composer stays usable", () => {
    setup();
    expect(screen.queryByTestId("composer-model-panel")).toBeNull();
    const panel = open("model");
    expect(panel.getAttribute("aria-modal")).toBeNull();
    // Anchored above the bar: opening it can neither shift the transcript nor
    // cover the box you are typing in.
    expect(panel.className).toContain("absolute");
    expect(panel.className).toContain("bottom-full");
    expect(screen.getByTestId("composer-model-trigger").getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("composer-model-panel")).toBeNull();
  });

  it("switches model from the bar and closes the menu", async () => {
    setup();
    open("model");
    expect(screen.getByTestId("composer-model-m1").getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByTestId("composer-model-m2"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1", {
        method: "PATCH",
        body: { model_profile_id: "m2" },
      }),
    );
    await waitFor(() => expect(screen.queryByTestId("composer-model-panel")).toBeNull());
  });

  it("keeps the menu open and says so when a change is refused", async () => {
    vi.mocked(api).mockRejectedValueOnce(new Error("nope"));
    setup();
    open("model");
    fireEvent.click(screen.getByTestId("composer-model-m2"));
    expect(await screen.findByText("Couldn't change the model. Try again.")).toBeTruthy();
    expect(screen.getByTestId("composer-model-panel")).toBeTruthy();
  });

  it("offers the workspace default as its own choice", async () => {
    setup();
    open("model");
    fireEvent.click(screen.getByTestId("composer-model-default"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1", {
        method: "PATCH",
        body: { model_profile_id: null },
      }),
    );
  });

  it("switches how cautious the agent is", async () => {
    setup();
    open("mode");
    expect(screen.getByTestId("composer-mode-balanced").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("Asks before risky actions")).toBeTruthy();
    fireEvent.click(screen.getByTestId("composer-mode-autonomous"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1/policy", {
        method: "PUT",
        body: { preset: "autonomous" },
      }),
    );
  });

  it("gives non-admins the value and the reason instead of a dead control", () => {
    setup({ isAdmin: false });
    open("model");
    expect(screen.queryByTestId("composer-model-m2")).toBeNull();
    expect(screen.getByText(/Only admins can change this\. Scout runs on Sonnet\./)).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    open("mode");
    expect(screen.queryByTestId("composer-mode-autonomous")).toBeNull();
    expect(screen.getByText(/Only admins can change this\. Asks before risky actions\./)).toBeTruthy();
  });

  it("grants a capability from the bar, and disables the ones this workspace can't serve", async () => {
    setup();
    open("tools");
    // What it can reach now, in plain language rather than capability strings.
    expect(screen.getByText(/read repositories/i)).toBeTruthy();
    expect(screen.getByRole("link", { name: /Manage tools and access/ }).getAttribute("href")).toBe(
      "/agents/a1",
    );

    const webAccess = screen.getByTestId("composer-tools-web-access") as HTMLButtonElement;
    expect(webAccess.disabled).toBe(false);
    expect(webAccess.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(webAccess);
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1/grants", {
        method: "POST",
        body: { capability: "web.search", scope: {}, effect: "allow" },
      }),
    );

    // No connector for code editing in this workspace, so the toggle says so
    // rather than failing when it is pressed.
    expect((screen.getByTestId("composer-tools-code-editing") as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("revokes a capability that is already on", async () => {
    setup({ grants: [grant("g1", "web.search"), grant("g2", "web.fetch")] });
    open("tools");
    const webAccess = screen.getByTestId("composer-tools-web-access");
    expect(webAccess.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(webAccess);
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1/grants/g1", {
        method: "DELETE",
      }),
    );
  });

  it("shows non-admins what the agent can reach without capability toggles", () => {
    setup({ isAdmin: false });
    open("tools");
    expect(screen.queryByTestId("composer-tools-web-access")).toBeNull();
    expect(screen.getByText(/read repositories/i)).toBeTruthy();
  });

  it("breaks this chat's usage into what went in and what came out", () => {
    setup();
    open("usage");
    expect(screen.getByTestId("composer-usage-total").textContent).toContain("12.4k tokens");
    expect(screen.getByTestId("composer-usage-total").textContent).toContain("$0.03");
    expect(screen.getByText("11.4k in · 1.0k out")).toBeTruthy();
  });

  it("drops the agent chips when the agent has left, keeping the chat's cost", () => {
    vi.mocked(useAgent).mockReturnValue({ data: undefined } as unknown as ReturnType<typeof useAgent>);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ChatComposerControls
          workspaceId="w1"
          agentId={null}
          agentName="Scout"
          isAdmin
          usage={{ inputTokens: 10, outputTokens: 2, costMicros: 0 }}
        />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("composer-model-trigger")).toBeNull();
    expect(screen.getByTestId("composer-usage-trigger")).toBeTruthy();
  });
});
