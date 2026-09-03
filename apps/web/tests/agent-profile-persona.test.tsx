/** The agent profile's persona surfaces: the chip in the header and the
 * Persona tab that sits after Skills. The tab's panel has its own suite
 * (agent-persona-panel.test.tsx) and is stubbed here. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AgentProfilePage from "@/app/(app)/agents/[id]/view";
import { useAgent } from "@/lib/hooks";
import type { Agent } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";
import { personaSummary } from "./helpers/personas";

vi.mock("next/navigation", () => ({
  usePathname: () => "/agents/a1",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/hooks", () => ({
  useAgent: vi.fn(),
  useOrgGraph: () => ({
    data: { workspace_id: "w1", teams: [], agents: [] },
    isPending: false,
    isError: false,
  }),
  useAgentAvatarMap: () => ({}),
  useInvalidateOrg: () => () => undefined,
  useAgentGrants: vi.fn(() => ({ data: [], isPending: false, isError: false })),
  useAgentPolicy: vi.fn(() => ({ data: undefined, isPending: false, isError: false })),
  useTools: vi.fn(() => ({ data: [], isPending: false, isError: false })),
  useConnections: vi.fn(() => ({ data: [], isPending: false, isError: false })),
  useConversations: vi.fn(() => ({
    data: { items: [], total: 0 },
    isPending: false,
    isError: false,
  })),
}));

vi.mock("@/components/company/use-working", () => ({
  useWorkingAgentIds: () => new Set<string>(),
}));

vi.mock("@/components/agents/avatar-dialog", () => ({ AvatarDialog: () => null }));

vi.mock("@/components/agents/persona-panel", () => ({
  PersonaPanel: () => <div data-testid="persona-panel" />,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function agent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "a1",
    workspace_id: "w1",
    team_id: null,
    manager_agent_id: null,
    name: "Scout",
    slug: "scout",
    role_title: "Analyst",
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
    persona_id: null,
    persona: null,
    created_at: "2026-09-01T09:00:00Z",
    updated_at: "2026-09-01T09:00:00Z",
    ...overrides,
  };
}

function renderProfile(subject: Agent) {
  vi.mocked(useAgent).mockReturnValue({
    data: subject,
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useAgent>);
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkspaceProvider
        user={{
          id: "u1",
          email: "ada@example.com",
          display_name: "Ada",
          created_at: "2026-01-01T00:00:00Z",
        }}
        workspace={{
          workspace_id: "w1",
          workspace_name: "Acme",
          workspace_slug: "acme",
          role: "owner",
        }}
      >
        <AgentProfilePage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

describe("AgentProfilePage persona", () => {
  it("shows the worn persona as a chip in the header", () => {
    renderProfile(agent({ persona_id: "p1", persona: personaSummary() }));
    const chip = screen.getByTestId("persona-chip");
    expect(chip.getAttribute("data-state")).toBe("on");
    expect(chip.getAttribute("href")).toBe("/personas?persona=p1");
    expect(chip.textContent).toContain("Mission Control");
  });

  it("marks the chip when the persona is switched off, and shows none without one", () => {
    renderProfile(agent({ persona_id: "p1", persona: personaSummary({ enabled: false }) }));
    expect(screen.getByTestId("persona-chip").getAttribute("data-state")).toBe("off");
    cleanup();
    renderProfile(agent());
    expect(screen.queryByTestId("persona-chip")).toBeNull();
  });

  it("has a Persona tab right after Skills that mounts the panel", () => {
    renderProfile(agent());
    const labels = screen.getAllByRole("tab").map((tab) => tab.textContent);
    expect(labels.indexOf("Persona")).toBe(labels.indexOf("Skills") + 1);
    expect(screen.queryByTestId("persona-panel")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Persona" }));
    expect(screen.getByTestId("persona-panel")).toBeTruthy();
  });
});
