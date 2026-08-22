/** Component tests: the agent directory card. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AgentDirectoryCard } from "@/components/company/agent-directory-card";
import type { Agent } from "@/lib/types";

afterEach(cleanup);

const agent: Agent = {
  id: "agent-1",
  workspace_id: "ws",
  team_id: "eng",
  manager_agent_id: null,
  name: "Senior SWE",
  slug: "senior-swe",
  role_title: "Senior Software Engineer",
  description: "Builds backend services. Reviews pull requests too.",
  system_prompt: "secret instructions",
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
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:00:00Z",
  expertise_json: ["Python", "Postgres"],
};

describe("AgentDirectoryCard", () => {
  it("shows name, role, purpose fallback, expertise, team, status, and actions", () => {
    render(<AgentDirectoryCard agent={agent} teamName="Engineering" />);
    expect(screen.getByText("Senior SWE")).toBeDefined();
    expect(screen.getByText("Senior Software Engineer")).toBeDefined();
    expect(screen.getByText("Builds backend services.")).toBeDefined();
    expect(screen.getByText("Python")).toBeDefined();
    expect(screen.getByText("Postgres")).toBeDefined();
    expect(screen.getByText("Engineering")).toBeDefined();
    expect(screen.getByText("Available")).toBeDefined();
    expect(screen.getByRole("link", { name: /Chat/ }).getAttribute("href")).toBe("/chats?agent=agent-1");
    expect(screen.getByRole("link", { name: /Profile/ }).getAttribute("href")).toBe("/agents/agent-1");
    // Never leaks operational internals.
    expect(screen.queryByText(/secret instructions/)).toBeNull();
    expect(screen.queryByText(/agent-1/)).toBeNull();
  });

  it("prefers the public purpose and reflects working / paused states", () => {
    render(<AgentDirectoryCard agent={{ ...agent, public_purpose: "Keeps the API fast." }} working />);
    expect(screen.getByText("Keeps the API fast.")).toBeDefined();
    expect(screen.getByText("Working now")).toBeDefined();
    cleanup();
    render(<AgentDirectoryCard agent={{ ...agent, status: "paused" }} />);
    expect(screen.getByText("Paused")).toBeDefined();
  });
});
