/** In-chat quick controls: model / mode / tools / cost, with the two
 * admin-only mutations degrading to a stated reason for everyone else. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatQuickControls } from "@/components/chat/quick-controls";
import { api } from "@/lib/api";
import {
  useAgent,
  useAgentGrants,
  useAgentPolicy,
  useConnections,
  useModelProfiles,
  useTools,
} from "@/lib/hooks";
import type {
  Agent,
  AgentPolicy,
  Conversation,
  ConversationDetail,
  Grant,
  ModelProfile,
} from "@/lib/types";

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

function setup({
  isAdmin = true,
  modelProfileId = "m1" as string | null,
  preset = "balanced" as AgentPolicy["preset"],
  grants = [grant("g1", "github.repository.read")],
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
    data: [],
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useTools>);
  vi.mocked(useConnections).mockReturnValue({
    data: [],
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useConnections>);

  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <ChatQuickControls workspaceId="w1" detail={detail()} isAdmin={isAdmin} />
    </QueryClientProvider>,
  );
  return view;
}

function openPanel() {
  fireEvent.click(screen.getByTestId("quick-controls-trigger"));
}

describe("ChatQuickControls", () => {
  it("stays closed until asked, then opens a non-modal popover", () => {
    setup();
    expect(screen.queryByTestId("quick-controls-panel")).toBeNull();
    openPanel();
    const panel = screen.getByTestId("quick-controls-panel");
    expect(panel.getAttribute("aria-modal")).toBeNull();
    // Anchored to the trigger so opening it can't push the transcript around.
    expect(panel.className).toContain("absolute");
    expect(screen.getByTestId("quick-controls-trigger").getAttribute("aria-expanded")).toBe("true");
  });

  it("shows the token and cost totals for this chat", () => {
    setup();
    openPanel();
    expect(screen.getByTestId("quick-controls-usage").textContent).toBe("12.4k tokens · $0.03");
  });

  it("switches the agent's model through the agents PATCH endpoint and invalidates", async () => {
    vi.mocked(api).mockResolvedValue({});
    setup();
    openPanel();
    const select = screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement;
    expect(select.value).toBe("m1");

    fireEvent.change(select, { target: { value: "m2" } });
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1", {
        method: "PATCH",
        body: { model_profile_id: "m2" },
      }),
    );
    await waitFor(() => expect(invalidateAccess).toHaveBeenCalled());
  });

  it("clears the model back to the workspace default with an explicit null", async () => {
    vi.mocked(api).mockResolvedValue({});
    setup();
    openPanel();
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "" } });
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1", {
        method: "PATCH",
        body: { model_profile_id: null },
      }),
    );
  });

  it("changes the mode through the policy endpoint and marks the current one", async () => {
    vi.mocked(api).mockResolvedValue({});
    setup({ preset: "balanced" });
    openPanel();
    expect(screen.getByTestId("quick-mode-balanced").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("Asks before risky actions")).toBeTruthy();

    fireEvent.click(screen.getByTestId("quick-mode-restricted"));
    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1/policy", {
        method: "PUT",
        body: { preset: "restricted" },
      }),
    );
    await waitFor(() => expect(invalidateAccess).toHaveBeenCalled());
  });

  it("shows non-admins the values with a plain reason instead of controls", () => {
    setup({ isAdmin: false });
    openPanel();
    expect(screen.queryByRole("combobox", { name: "Model" })).toBeNull();
    expect(screen.getByTestId("quick-model-readonly").textContent).toBe("Sonnet");
    expect(screen.getByTestId("quick-mode-readonly").textContent).toBe(
      "Asks before risky actions",
    );
    expect(screen.queryByTestId("quick-mode-balanced")).toBeNull();
    expect(screen.getAllByText(/Only admins can change this\./).length).toBe(2);
    // The cost is not privileged, so it still shows.
    expect(screen.getByTestId("quick-controls-usage")).toBeTruthy();
  });

  it("describes the tools in plain language and links to the full editor", () => {
    setup();
    openPanel();
    expect(screen.getByText("GitHub: read repositories")).toBeTruthy();
    const link = screen.getByRole("link", { name: /Manage tools and access/ });
    expect(link.getAttribute("href")).toBe("/agents/a1");
  });

  it("says so plainly when the agent has no tools yet", () => {
    setup({ grants: [] });
    openPanel();
    expect(screen.getByText(/No apps or tools yet/)).toBeTruthy();
  });

  it("falls back to the workspace default label when no profile is pinned", () => {
    setup({ isAdmin: false, modelProfileId: null });
    openPanel();
    expect(screen.getByTestId("quick-model-readonly").textContent).toBe("Workspace default");
  });
});
