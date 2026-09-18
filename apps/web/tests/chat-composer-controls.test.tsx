/** The controls on the composer's own row: model, mode, tools and cost.
 * The two agent writes and the bundle toggles are admin-only, and degrade to
 * the current value with a stated reason for everyone else. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatComposerControls } from "@/components/chat/composer-controls";
import { api } from "@/lib/api";
import {
  useAgent,
  useAgentBundles,
  useAgentGrants,
  useAgentPolicy,
  useConnections,
  useModelProfiles,
  useTools,
} from "@/lib/hooks";
import type {
  Agent,
  AgentPolicy,
  BundleStatusOut,
  Conversation,
  ConversationDetail,
  Grant,
  ModelProfile,
  ToolInfo,
} from "@/lib/types";
import { TOOL_PRESETS } from "@/lib/wizard";

const invalidateAccess = vi.fn();

vi.mock("@/lib/hooks", () => ({
  useAgent: vi.fn(),
  useAgentBundles: vi.fn(),
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
  };
}

function detail(overrides: Partial<ConversationDetail> = {}): ConversationDetail {
  const conversation = {
    id: "c1",
    workspace_id: "w1",
    title: "Weekly summary",
    status: "active",
    pinned: false,
    primary_agent_id: "a1",
    created_by_user_id: "u1",
    last_activity_at: "",
    created_at: "",
    updated_at: "",
    active_task_id: null,
    active_task_state: null,
    active_run_status: null,
    active_run_started_at: null,
    last_message_preview: "",
    last_message_sender_type: "agent",
    agent_name: "Scout",
    agent_role_title: "Analyst",
    task_count: 1,
  } as Conversation;
  return {
    conversation,
    agent: {
      id: "a1",
      name: "Scout",
      role_title: "Analyst",
      status: "active",
      availability: "available",
      public_purpose: "",
    },
    tasks: [],
    total_input_tokens: 11_400,
    total_output_tokens: 1_000,
    total_cost_micros: 30_000,
    pending_approvals: [],
    ...overrides,
  };
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

function bundle(overrides: Partial<BundleStatusOut> = {}): BundleStatusOut {
  return {
    id: "github-read",
    label: "GitHub",
    summary: "Read repositories and issues",
    description: "",
    tools: [{ name: "github.repository.read", capability: "github.repository.read", scope: {} }],
    rules: [],
    not_included: [],
    readiness: { state: "ready", needs: [], missing_tools: [] },
    state: "off",
    granted_capabilities: [],
    missing_capabilities: [],
    problems: [],
    ...overrides,
  };
}

/** A catalog entry per tool the collaboration preset wants, so the client
 * loop behind an organization bundle has something to write. */
const COLLABORATION = TOOL_PRESETS.find((preset) => preset.id === "collaboration")!;

function catalogFor(preset = COLLABORATION): ToolInfo[] {
  return Object.keys(preset.tools).map((name) => ({
    name,
    description: "",
    risk: "read",
    required_capability: name,
    supports_approval: true,
    scope_keys: [],
    required_grant_scope_keys: [],
    input_schema: {},
  }));
}

function setup({
  isAdmin = true,
  modelProfileId = "m1" as string | null,
  preset = "balanced" as AgentPolicy["preset"],
  rules = [] as AgentPolicy["rules"],
  grants = [grant("g1", "github.repository.read")],
  bundles = [bundle()],
  tools = [] as ToolInfo[],
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
    data: { rules, preset, autonomy_level: "supervised" },
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useAgentPolicy>);
  vi.mocked(useAgentGrants).mockReturnValue({
    data: grants,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useAgentGrants>);
  vi.mocked(useAgentBundles).mockReturnValue({
    data: bundles,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useAgentBundles>);
  vi.mocked(useTools).mockReturnValue({
    data: tools,
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
      <ChatComposerControls workspaceId="w1" detail={detail()} isAdmin={isAdmin} />
    </QueryClientProvider>,
  );
}

const openChip = (name: string) => fireEvent.click(screen.getByTestId(`composer-${name}-chip`));

describe("ChatComposerControls chips", () => {
  it("names the current value on every chip without opening anything", () => {
    setup({ bundles: [bundle({ state: "on" })] });
    expect(screen.getByTestId("composer-model-chip").textContent).toContain("Sonnet");
    expect(screen.getByTestId("composer-mode-chip").textContent).toContain("Balanced");
    expect(screen.getByTestId("composer-tools-chip").textContent).toContain("GitHub");
    expect(screen.getByTestId("composer-usage-chip").textContent).toContain("12.4k");
    expect(screen.queryByTestId("composer-model-panel")).toBeNull();
  });

  it("opens one popover at a time, upwards, and closes it on Escape", () => {
    setup();
    openChip("model");
    const panel = screen.getByTestId("composer-model-panel");
    // Anchored above the composer so it can't cover the field it belongs to.
    expect(panel.className).toContain("bottom-full");
    expect(panel.getAttribute("aria-modal")).toBeNull();

    openChip("mode");
    expect(screen.queryByTestId("composer-model-panel")).toBeNull();
    expect(screen.getByTestId("composer-mode-panel")).toBeTruthy();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("composer-mode-panel")).toBeNull();
  });

  it("breaks this chat's usage down behind the cost chip", () => {
    setup();
    openChip("usage");
    expect(screen.getByTestId("composer-usage-total").textContent).toBe("12.4k tokens · $0.03");
    expect(screen.getByText("11.4k in · 1.0k out")).toBeTruthy();
  });
});

describe("ChatComposerControls model", () => {
  it("switches the model through the agents PATCH endpoint and invalidates", async () => {
    vi.mocked(api).mockResolvedValue({});
    setup();
    openChip("model");
    expect(screen.getByTestId("composer-model-m1").getAttribute("aria-checked")).toBe("true");

    fireEvent.click(screen.getByTestId("composer-model-m2"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1", {
        method: "PATCH",
        body: { model_profile_id: "m2" },
      }),
    );
    await waitFor(() => expect(invalidateAccess).toHaveBeenCalled());
    // Picking one is the end of the errand: the popover gets out of the way.
    await waitFor(() => expect(screen.queryByTestId("composer-model-panel")).toBeNull());
  });

  it("clears the model back to the workspace default with an explicit null", async () => {
    vi.mocked(api).mockResolvedValue({});
    setup();
    openChip("model");
    fireEvent.click(screen.getByTestId("composer-model-default"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1", {
        method: "PATCH",
        body: { model_profile_id: null },
      }),
    );
  });

  it("says the model is the workspace default when none is pinned", () => {
    setup({ modelProfileId: null });
    expect(screen.getByTestId("composer-model-chip").textContent).toContain("Default model");
  });
});

describe("ChatComposerControls mode", () => {
  it("changes the mode through the policy endpoint and marks the current one", async () => {
    vi.mocked(api).mockResolvedValue({});
    setup({ preset: "balanced" });
    openChip("mode");
    expect(screen.getByTestId("composer-mode-balanced").getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("Asks before risky actions")).toBeTruthy();

    fireEvent.click(screen.getByTestId("composer-mode-restricted"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1/policy", {
        method: "PUT",
        body: { preset: "restricted" },
      }),
    );
    await waitFor(() => expect(invalidateAccess).toHaveBeenCalled());
  });

  it("says which rule the mode will not change", () => {
    // The gate on pushing code is a decision of its own: the server keeps it
    // across a preset change, and the panel says so rather than leaving the
    // modes looking like they replace everything.
    setup({
      preset: "autonomous",
      rules: [{ capability: "cli.repository.push", risk: null, action: "approval" }],
    });
    openChip("mode");
    expect(screen.getByTestId("composer-mode-kept").textContent).toContain("cli.repository.push");
    expect(screen.getByTestId("composer-mode-kept").textContent).toContain("needs approval");
  });
});

describe("ChatComposerControls tools", () => {
  it("counts what is on, and turns a ready bundle on through the bundle endpoint", async () => {
    vi.mocked(api).mockResolvedValue({ needs: [] });
    setup({ bundles: [bundle(), bundle({ id: "web-access", label: "Web" })] });
    expect(screen.getByTestId("composer-tools-chip").textContent).toContain("1 tool");

    openChip("tools");
    fireEvent.click(screen.getByTestId("composer-bundle-toggle-github-read"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith(
        "/api/v1/workspaces/w1/agents/a1/bundles/github-read",
        {
          method: "POST",
          body: { connections: {}, repositories: [], base: null, dry_run: false },
        },
      ),
    );
    await waitFor(() => expect(invalidateAccess).toHaveBeenCalled());
  });

  it("says so, and writes nothing, when the server hands back a question", async () => {
    // The apply is not a refusal: the server answers with what it still needs
    // and writes nothing, and those questions are answered on the agent.
    vi.mocked(api).mockResolvedValue({ needs: [{ kind: "connect", connector_type: "github" }] });
    setup();
    openChip("tools");
    fireEvent.click(screen.getByTestId("composer-bundle-toggle-github-read"));
    await waitFor(() =>
      expect(screen.getByTestId("composer-bundle-needs-github-read").textContent).toContain(
        "needs an app connected first",
      ),
    );
    expect(invalidateAccess).not.toHaveBeenCalled();
  });

  it("names what turning a bundle off revokes before it revokes anything", async () => {
    vi.mocked(api).mockResolvedValue({
      revoked: [grant("g1", "github.repository.read"), grant("g2", "github.issue.read")],
      hand_made: [grant("g2", "github.issue.read")],
    });
    setup({ bundles: [bundle({ state: "on" })] });
    openChip("tools");
    fireEvent.click(screen.getByTestId("composer-bundle-toggle-github-read"));

    // The first call is the dry run behind the confirmation, and nothing else.
    await waitFor(() =>
      expect(screen.getByTestId("composer-bundle-confirm-github-read").textContent).toContain(
        "revokes 2 grants, including 1 added by hand",
      ),
    );
    expect(vi.mocked(api)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api)).toHaveBeenCalledWith(
      "/api/v1/workspaces/w1/agents/a1/bundles/github-read",
      { method: "DELETE", params: { dry_run: "true" } },
    );

    fireEvent.click(screen.getByTestId("composer-bundle-confirm-yes-github-read"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith(
        "/api/v1/workspaces/w1/agents/a1/bundles/github-read",
        { method: "DELETE", params: {} },
      ),
    );
    await waitFor(() => expect(invalidateAccess).toHaveBeenCalled());
  });

  it("writes an organization bundle as grants, the way the access tab does", async () => {
    vi.mocked(api).mockResolvedValue({});
    const tools = catalogFor();
    setup({
      grants: [],
      tools,
      bundles: [bundle({ id: COLLABORATION.id, label: COLLABORATION.label, tools: [] })],
    });
    openChip("tools");
    fireEvent.click(screen.getByTestId(`composer-bundle-toggle-${COLLABORATION.id}`));

    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalled());
    const [url, options] = vi.mocked(api).mock.calls[0];
    expect(url).toBe("/api/v1/workspaces/w1/agents/a1/grants");
    expect((options as { method: string }).method).toBe("POST");
  });

  it("sends a bundle that still needs setting up to the agent instead of guessing", () => {
    setup({
      bundles: [
        bundle({
          readiness: { state: "needs", needs: [], missing_tools: [] },
        }),
      ],
    });
    openChip("tools");
    expect(screen.queryByTestId("composer-bundle-toggle-github-read")).toBeNull();
    expect(
      screen.getByTestId("composer-bundle-setup-github-read").getAttribute("href"),
    ).toBe("/agents/a1?tab=access");
  });

  it("lists grants no bundle accounts for, in plain language", () => {
    setup({
      grants: [grant("g1", "github.repository.read"), grant("g2", "web.page.read")],
      bundles: [bundle({ state: "on" })],
    });
    openChip("tools");
    expect(screen.getByText("Also allowed")).toBeTruthy();
    expect(screen.getByText(/^Web:/)).toBeTruthy();
  });
});

describe("ChatComposerControls permissions", () => {
  it("shows a non-admin the values with a plain reason instead of controls", () => {
    setup({ isAdmin: false, bundles: [bundle({ state: "on" })] });

    openChip("model");
    expect(screen.getByTestId("composer-model-readonly").textContent).toBe("Sonnet");
    expect(screen.queryByTestId("composer-model-m2")).toBeNull();

    openChip("mode");
    expect(screen.getByTestId("composer-mode-readonly").textContent).toBe(
      "Asks before risky actions",
    );
    expect(screen.queryByTestId("composer-mode-balanced")).toBeNull();

    openChip("tools");
    expect(screen.queryByTestId("composer-bundle-toggle-github-read")).toBeNull();
    expect(screen.getByTestId("composer-bundle-github-read").textContent).toContain("On");

    // The cost is nobody's privilege.
    openChip("usage");
    expect(screen.getByTestId("composer-usage-total")).toBeTruthy();
  });

  it("shows only the cost when the agent has left the workspace", () => {
    setup();
    cleanup();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ChatComposerControls workspaceId="w1" detail={detail({ agent: null })} isAdmin />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("composer-model-chip")).toBeNull();
    expect(screen.getByTestId("composer-usage-chip")).toBeTruthy();
  });
});
