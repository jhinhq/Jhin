/** Agent profile Skills tab: enablement checkboxes and the skills.read
 * grant hint. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SkillsPanel } from "@/components/agents/skills-panel";
import { api } from "@/lib/api";
import { useAgentGrants, useAgentSkills } from "@/lib/hooks";
import type { AgentSkillInfo, Grant } from "@/lib/types";

vi.mock("@/lib/hooks", () => ({
  useAgentSkills: vi.fn(),
  useAgentGrants: vi.fn(),
  useInvalidateSkills: () => () => undefined,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function item(overrides: Partial<AgentSkillInfo> = {}): AgentSkillInfo {
  return {
    skill_id: "s1",
    name: "release-notes",
    description: "Write release notes.",
    source: "built_in",
    enabled: true,
    enabled_for_agent: false,
    ...overrides,
  };
}

function grant(capability: string): Grant {
  return {
    id: `g-${capability}`,
    agent_id: "a1",
    capability,
    scope_json: {},
    effect: "allow",
    created_at: "2026-08-23T00:00:00Z",
  };
}

function renderPanel(items: AgentSkillInfo[], grants: Grant[], isAdmin = true) {
  vi.mocked(useAgentSkills).mockReturnValue({
    data: items,
    isPending: false,
    isError: false,
  } as ReturnType<typeof useAgentSkills>);
  vi.mocked(useAgentGrants).mockReturnValue({
    data: grants,
    isPending: false,
    isError: false,
  } as ReturnType<typeof useAgentGrants>);
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <SkillsPanel workspaceId="w1" agentId="a1" isAdmin={isAdmin} />
    </QueryClientProvider>,
  );
}

describe("SkillsPanel", () => {
  it("lists workspace skills with their enablement state", () => {
    renderPanel(
      [item(), item({ skill_id: "s2", name: "bug-report-triage", enabled_for_agent: true })],
      [grant("skills.read")],
    );
    expect(screen.getByRole("checkbox", { name: "Use release-notes" })).toBeTruthy();
    const active = screen.getByRole("checkbox", {
      name: "Use bug-report-triage",
    }) as HTMLInputElement;
    expect(active.checked).toBe(true);
  });

  it("saves the new set when a skill is toggled on", async () => {
    vi.mocked(api).mockResolvedValue([]);
    renderPanel([item()], [grant("skills.read")]);
    fireEvent.click(screen.getByRole("checkbox", { name: "Use release-notes" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1/skills", {
        method: "PUT",
        body: { skill_ids: ["s1"] },
      }),
    );
  });

  it("hints when the agent lacks a skills.read grant", () => {
    renderPanel([item()], [grant("memory.read")]);
    expect(screen.getByTestId("skills-grant-hint")).toBeTruthy();
  });

  it("does not hint when a wildcard grant covers skills.read", () => {
    renderPanel([item()], [grant("*")]);
    expect(screen.queryByTestId("skills-grant-hint")).toBeNull();
  });

  it("disables editing for non-admins", () => {
    renderPanel([item()], [], false);
    const checkbox = screen.getByRole("checkbox", { name: "Use release-notes" });
    expect((checkbox as HTMLInputElement).disabled).toBe(true);
  });
});
