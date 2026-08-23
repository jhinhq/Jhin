/** AvatarDialog: Shape-first tabs, saving a free shape avatar, the paid
 * Generate disclosure, and Reset behavior. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AvatarDialog } from "@/components/agents/avatar-dialog";
import type { Agent } from "@/lib/types";

const apiMock = vi.hoisted(() => vi.fn(async () => ({})));

vi.mock("@/lib/api", () => ({
  api: apiMock,
  apiUpload: vi.fn(async () => ({})),
  ApiError: class ApiError extends Error {
    code: string | null = null;
    detail = "";
    status = 400;
  },
}));

vi.mock("@/lib/hooks", () => ({
  useAvatarGeneration: () => ({ data: null, refetch: vi.fn() }),
  useInvalidateAvatar: () => () => {},
}));

afterEach(() => {
  cleanup();
  apiMock.mockClear();
});

function agentFixture(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "agent-1",
    workspace_id: "ws",
    team_id: null,
    manager_agent_id: null,
    name: "Bisby",
    slug: "bisby",
    role_title: "Helper",
    description: "",
    system_prompt: "",
    status: "active",
    autonomy_level: "supervised",
    model_profile_id: null,
    temperature: null,
    max_output_tokens: null,
    max_steps: 20,
    max_run_minutes: 30,
    max_concurrent_runs: 1,
    monthly_budget_cents: null,
    metadata_json: {},
    avatar_kind: "initials",
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

function renderDialog(agent: Agent) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AvatarDialog workspaceId="ws" agent={agent} open onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe("AvatarDialog", () => {
  it("opens on the free Shape tab and saves the chosen shape and color", async () => {
    renderDialog(agentFixture());
    expect(screen.getByRole("tab", { name: "Shape" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("radiogroup", { name: "Shape" })).toBeTruthy();
    expect(screen.getByRole("radiogroup", { name: "Color" })).toBeTruthy();

    fireEvent.click(screen.getByRole("radio", { name: "Quad" }));
    fireEvent.click(screen.getByRole("radio", { name: "Mint" }));
    fireEvent.click(screen.getByRole("button", { name: "Use this shape" }));

    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(apiMock).toHaveBeenCalledWith("/api/v1/workspaces/ws/agents/agent-1/avatar/shape", {
      method: "PUT",
      body: { shape: "quad", color: "#3ecf8e" },
    });
  });

  it("labels Generate as paid while shapes stay free", () => {
    renderDialog(agentFixture());
    fireEvent.click(screen.getByRole("tab", { name: "Generate" }));
    expect(screen.getByText(/This costs money/)).toBeTruthy();
    expect(screen.getByText(/shapes and uploads are free/)).toBeTruthy();
  });

  it("keeps Reset disabled for initials and enables it for a shape avatar", () => {
    renderDialog(agentFixture());
    fireEvent.click(screen.getByRole("tab", { name: "Reset" }));
    expect(
      (screen.getByRole("button", { name: /Remove and use initials/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
    cleanup();

    renderDialog(agentFixture({ avatar_kind: "shape", avatar_shape: "jay", avatar_color: "#7371fc" }));
    expect(screen.getByText("Using a free shape avatar.")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Reset" }));
    expect(
      (screen.getByRole("button", { name: /Remove and use initials/ }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });
});
