/** Component tests: the org tree renders hierarchy and admin actions. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToolsAccessTab } from "@/components/org/tools-access-tab";
import { TeamCard } from "@/components/org/tree";
import { buildOrgTree } from "@/lib/org-tree";
import type { Agent, OrgAgentNode, OrgTeamNode, ToolInfo } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const teams: OrgTeamNode[] = [
  {
    id: "eng",
    name: "Engineering",
    description: "Builds the product",
    parent_team_id: null,
    manager_agent_id: "cto",
    color_token: "indigo",
    icon: "wrench",
  },
];

const agents: OrgAgentNode[] = [
  {
    id: "cto",
    name: "CTO",
    slug: "cto",
    role_title: "Chief Technology Officer",
    status: "active",
    team_id: "eng",
    manager_agent_id: null,
  },
  {
    id: "swe",
    name: "Senior SWE",
    slug: "senior-swe",
    role_title: "Senior Software Engineer",
    status: "active",
    team_id: "eng",
    manager_agent_id: "cto",
  },
  {
    id: "qa",
    name: "QA Engineer",
    slug: "qa-engineer",
    role_title: "QA Engineer",
    status: "paused",
    team_id: "eng",
    manager_agent_id: "cto",
  },
];

function renderTeamCard(isAdmin: boolean) {
  const tree = buildOrgTree({ teams, agents });
  const onOpenAgent = vi.fn();
  render(
    <TeamCard
      node={tree.roots[0]}
      depth={0}
      isAdmin={isAdmin}
      onOpenAgent={onOpenAgent}
      onEditTeam={vi.fn()}
      onDeleteTeam={vi.fn()}
      onAddAgent={vi.fn()}
      managerNameFor={(agent) =>
        agent.manager_agent_id === "cto" ? "CTO" : undefined
      }
    />,
  );
  return { onOpenAgent };
}

describe("TeamCard", () => {
  it("renders the team with its agent hierarchy", () => {
    renderTeamCard(false);
    expect(screen.getByText("Engineering")).toBeDefined();
    expect(screen.getByText("3 agents")).toBeDefined();
    expect(screen.getByText("CTO")).toBeDefined();
    expect(screen.getByText("Senior SWE")).toBeDefined();
    expect(screen.getByText("QA Engineer")).toBeDefined();
    // Reporting lines are labeled.
    expect(screen.getAllByText(/reports to CTO/)).toHaveLength(2);
    // Paused agents are badged.
    expect(screen.getByText("paused")).toBeDefined();
  });

  it("opens the agent drawer callback on click", () => {
    const { onOpenAgent } = renderTeamCard(false);
    screen.getByText("Senior SWE").closest("button")!.click();
    expect(onOpenAgent).toHaveBeenCalledTimes(1);
    expect(onOpenAgent.mock.calls[0][0].id).toBe("swe");
  });

  it("hides admin actions from non-admins and shows them to admins", () => {
    renderTeamCard(false);
    expect(screen.queryByTitle("Edit team")).toBeNull();
    cleanup();
    renderTeamCard(true);
    expect(screen.getByTitle("Edit team")).toBeDefined();
    expect(screen.getByTitle("Delete team")).toBeDefined();
    expect(screen.getByTitle("Add agent to this team")).toBeDefined();
  });
});

const accessAgent: Agent = {
  id: "agent-1",
  workspace_id: "workspace-1",
  team_id: null,
  manager_agent_id: null,
  name: "Release Engineer",
  slug: "release-engineer",
  role_title: "Release Engineer",
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
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:00:00Z",
};

const accessTools: ToolInfo[] = [
  {
    name: "vercel.deployment.read",
    description: "Read a deployment",
    risk: "read",
    required_capability: "vercel.deployment.read",
    supports_approval: false,
    scope_keys: ["connection_id", "project_id"],
    required_grant_scope_keys: ["connection_id", "project_id"],
    input_schema: {},
  },
  {
    name: "cli.command.execute",
    description: "Run a sandboxed command",
    risk: "write",
    required_capability: "cli.command.execute",
    supports_approval: true,
    scope_keys: ["connection_id", "command", "image", "network"],
    required_grant_scope_keys: [],
    input_schema: {},
  },
  {
    name: "organization.delegate_task",
    description: "Delegate a task",
    risk: "write",
    required_capability: "organization.delegate",
    supports_approval: false,
    scope_keys: [],
    required_grant_scope_keys: [],
    input_schema: {},
  },
];

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function renderToolsAccess() {
  const grantBodies: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    if (path.endsWith("/agents/agent-1/grants") && method === "GET") return json([]);
    if (path.endsWith("/agents/agent-1/grants") && method === "POST") {
      grantBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return json({});
    }
    if (path.endsWith("/agents/agent-1/policy")) {
      return json({ preset: "balanced", autonomy_level: "supervised", rules: [] });
    }
    if (path.endsWith("/tools")) return json(accessTools);
    if (path.endsWith("/connections")) {
      return json([
        { id: "vercel-connection", connector_type: "vercel", name: "Vercel production" },
        { id: "cli-connection", connector_type: "cli", name: "CLI sandbox" },
      ]);
    }
    throw new Error(`Unexpected request: ${method} ${path}`);
  }));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceProvider
        user={{ id: "user-1", email: "owner@example.com", display_name: "Owner", created_at: "2026-08-18T00:00:00Z" }}
        workspace={{ workspace_id: "workspace-1", workspace_name: "Acme", workspace_slug: "acme", role: "owner" }}
      >
        <ToolsAccessTab agent={accessAgent} canEdit />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
  return grantBodies;
}

describe("ToolsAccessTab", () => {
  it("blocks incomplete required scopes and preserves CLI and delegation semantics", async () => {
    const grantBodies = renderToolsAccess();
    await screen.findByText("vercel.deployment.read");
    const capabilityPicker = screen.getByLabelText("Capability");
    const add = screen.getByRole("button", { name: "Add" });

    fireEvent.change(capabilityPicker, { target: { value: "vercel.deployment.read" } });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Connection — Required for this tool/), {
      target: { value: "vercel-connection" },
    });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Project ID — Required for this tool/), {
      target: { value: "project-1" },
    });
    expect((add as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(add);
    await waitFor(() => expect(grantBodies).toHaveLength(1));
    expect(grantBodies[0]).toEqual({
      capability: "vercel.deployment.read",
      scope: { connection_id: "vercel-connection", project_id: "project-1" },
      effect: "allow",
    });

    await waitFor(() => expect((capabilityPicker as HTMLSelectElement).value).toBe(""));
    fireEvent.change(capabilityPicker, { target: { value: "cli.command.execute" } });
    const cliScope = screen.getByTestId("cli-scope");
    expect(within(cliScope).getByRole("option", { name: "none (isolated)" })).toBeDefined();
    expect(within(cliScope).getByRole("option", { name: "internet (sandbox bridge)" })).toBeDefined();
    fireEvent.change(within(cliScope).getByLabelText("Connection"), { target: { value: "cli-connection" } });
    fireEvent.change(within(cliScope).getByLabelText("Command pattern"), { target: { value: "pnpm test*" } });
    fireEvent.change(within(cliScope).getByLabelText("Network"), { target: { value: "none" } });
    fireEvent.click(add);
    await waitFor(() => expect(grantBodies).toHaveLength(2));
    expect(grantBodies[1]).toEqual({
      capability: "cli.command.execute",
      scope: { connection_id: "cli-connection", command: "pnpm test*", network: "none" },
      effect: "allow",
    });

    await waitFor(() => expect((capabilityPicker as HTMLSelectElement).value).toBe(""));
    fireEvent.change(capabilityPicker, { target: { value: "organization.delegate_task" } });
    const delegation = screen.getByTestId("delegation-scope");
    fireEvent.change(within(delegation).getByRole("combobox"), {
      target: { value: "team" },
    });
    fireEvent.click(add);
    await waitFor(() => expect(grantBodies).toHaveLength(3));
    expect(grantBodies[2]).toEqual({
      capability: "organization.delegate",
      scope: { targets: "team" },
      effect: "allow",
    });
  });
});
