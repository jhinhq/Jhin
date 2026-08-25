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

function grantRecord(capability: string, scope: Record<string, unknown> = {}) {
  return {
    id: `grant-${capability}`,
    agent_id: "agent-1",
    capability,
    scope_json: scope,
    effect: "allow",
    created_at: "2026-08-18T00:00:00Z",
  };
}

function renderToolsAccess(
  toolCatalog: ToolInfo[] = accessTools,
  initialGrants: ReturnType<typeof grantRecord>[] = [],
) {
  const grantBodies: Record<string, unknown>[] = [];
  const revoked: string[] = [];
  const store = [...initialGrants];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    if (path.endsWith("/agents/agent-1/grants") && method === "GET") return json(store);
    if (path.endsWith("/agents/agent-1/grants") && method === "POST") {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      grantBodies.push(body);
      store.push(grantRecord(String(body.capability), body.scope as Record<string, unknown>));
      return json({});
    }
    if (path.includes("/agents/agent-1/grants/") && method === "DELETE") {
      const id = path.split("/").pop()!;
      revoked.push(id);
      const index = store.findIndex((grant) => grant.id === id);
      if (index >= 0) store.splice(index, 1);
      return json({});
    }
    if (path.endsWith("/agents/agent-1/policy")) {
      return json({ preset: "balanced", autonomy_level: "supervised", rules: [] });
    }
    if (path.endsWith("/tools")) return json(toolCatalog);
    if (path.endsWith("/connections")) {
      return json([
        { id: "vercel-connection", connector_type: "vercel", name: "Vercel production", status: "active" },
        { id: "cli-connection", connector_type: "cli", name: "CLI sandbox", status: "active" },
        { id: "web-connection", connector_type: "web", name: "Web search", status: "active" },
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
  return { grantBodies, revoked };
}

describe("ToolsAccessTab", () => {
  it("blocks incomplete required scopes and preserves CLI and delegation semantics", async () => {
    const { grantBodies } = renderToolsAccess();
    fireEvent.click(await screen.findByTestId("advanced-access-toggle"));
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
    fireEvent.change(within(delegation).getAllByRole("combobox")[0], {
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

const webAccessTools: ToolInfo[] = [
  {
    name: "web.search",
    description: "Search the web",
    risk: "read",
    required_capability: "web.search",
    supports_approval: false,
    scope_keys: ["connection_id"],
    required_grant_scope_keys: ["connection_id"],
    input_schema: {},
  },
  {
    name: "web.fetch",
    description: "Fetch a page",
    risk: "read",
    required_capability: "web.fetch",
    supports_approval: false,
    scope_keys: ["connection_id", "domain"],
    required_grant_scope_keys: ["connection_id"],
    input_schema: {},
  },
];

describe("ToolsAccessTab capabilities", () => {
  it("keeps the tool catalog and the grant editor behind an advanced disclosure", async () => {
    renderToolsAccess();
    const toggle = await screen.findByTestId("advanced-access-toggle");
    expect(screen.queryByTestId("advanced-access")).toBeNull();
    expect(screen.queryByTestId("tool-vercel.deployment.read")).toBeNull();
    expect(screen.queryByLabelText("Capability")).toBeNull();
    expect(toggle.textContent).toContain("3 tools");
    fireEvent.click(toggle);
    expect(screen.getByTestId("tool-vercel.deployment.read")).toBeDefined();
    expect(screen.getByLabelText("Capability")).toBeDefined();
  });

  it("auto-expands the advanced editor when a grant was made by hand", async () => {
    renderToolsAccess(accessTools, [
      grantRecord("vercel.deployment.read", { connection_id: "vercel-connection", project_id: "p" }),
    ]);
    await screen.findByTestId("advanced-access");
    expect(screen.getByTestId("advanced-access-toggle").textContent).toContain(
      "1 outside the capabilities above",
    );
  });

  it("turns a capability on and back off by adding and revoking its grants", async () => {
    const { grantBodies, revoked } = renderToolsAccess(webAccessTools);
    const button = await screen.findByTestId("capability-preset-web-access");
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByTestId("no-grants")).toBeDefined();

    fireEvent.click(button);
    await waitFor(() => expect(grantBodies).toHaveLength(2));
    expect(grantBodies).toEqual([
      { capability: "web.search", scope: { connection_id: "web-connection" }, effect: "allow" },
      {
        capability: "web.fetch",
        scope: { connection_id: "web-connection", domain: "*" },
        effect: "allow",
      },
    ]);
    await waitFor(() =>
      expect(
        screen.getByTestId("capability-preset-web-access").getAttribute("aria-pressed"),
      ).toBe("true"),
    );
    // Everything granted is accounted for by the capability, so the raw
    // editor stays collapsed.
    expect(screen.queryByTestId("advanced-access")).toBeNull();

    fireEvent.click(screen.getByTestId("capability-preset-web-access"));
    await waitFor(() => expect(revoked).toHaveLength(2));
    await waitFor(() => expect(screen.getByTestId("no-grants")).toBeDefined());
    expect(
      screen.getByTestId("capability-preset-web-access").getAttribute("aria-pressed"),
    ).toBe("false");
  });

  it("disables a capability whose tools this workspace does not offer", async () => {
    renderToolsAccess(webAccessTools);
    const codeEditing = await screen.findByTestId("capability-preset-code-editing");
    expect((codeEditing as HTMLButtonElement).disabled).toBe(true);
  });
});
