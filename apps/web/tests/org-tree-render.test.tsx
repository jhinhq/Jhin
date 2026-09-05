/** Component tests: the org tree renders hierarchy and admin actions. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToolsAccessTab } from "@/components/org/tools-access-tab";
import { TeamCard } from "@/components/org/tree";
import { buildOrgTree } from "@/lib/org-tree";
import { grantCovers } from "@/lib/policy";
import type { Agent, OrgAgentNode, OrgTeamNode, ToolInfo } from "@/lib/types";
import { TOOL_PRESETS } from "@/lib/wizard";
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

/** Enough of the Code-editing bundle to switch it on. */
const codeEditingTools: ToolInfo[] = [
  {
    name: "cli.repository.checkout",
    description: "Clone a repository",
    risk: "write",
    required_capability: "cli.repository.checkout",
    supports_approval: true,
    scope_keys: ["connection_id", "repository", "image"],
    required_grant_scope_keys: ["connection_id", "repository"],
    input_schema: {},
  },
  {
    name: "cli.repository.push",
    description: "Push the working branch",
    risk: "elevated",
    required_capability: "cli.repository.push",
    supports_approval: true,
    scope_keys: ["connection_id", "repository", "branch"],
    required_grant_scope_keys: ["connection_id", "repository", "branch"],
    input_schema: {},
  },
  {
    name: "github.repository.read",
    description: "Read a repository",
    risk: "read",
    required_capability: "github.repository.read",
    supports_approval: false,
    scope_keys: ["connection_id", "repository"],
    required_grant_scope_keys: [],
    input_schema: {},
  },
];

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

const connections = [
  { id: "vercel-connection", connector_type: "vercel", name: "Vercel production", status: "active", auth_type: "api_key", config_json: {} },
  {
    id: "cli-connection",
    connector_type: "cli",
    name: "CLI sandbox",
    status: "active",
    auth_type: "none",
    config_json: { git_connection_id: "github-connection", allowed_repositories: ["*"] },
  },
  { id: "web-connection", connector_type: "web", name: "Web search", status: "active", auth_type: "api_key", config_json: {} },
  { id: "github-connection", connector_type: "github", name: "GitHub", status: "active", auth_type: "pat", config_json: {} },
];

/** The bundle catalog as the server describes it, with the fixed scopes
 * the wizard presets carry plus the read-only GitHub bundle. */
const BUNDLE_DEFS: {
  id: string;
  label: string;
  summary: string;
  description: string;
  tools: Record<string, Record<string, string>>;
  rules: { capability: string; risk: null; action: "approval" }[];
}[] = [
  ...TOOL_PRESETS.map((preset) => ({
    id: preset.id,
    label: preset.label,
    summary: preset.summary,
    description: preset.description,
    tools: preset.tools,
    rules: (preset.policyRules ?? []) as { capability: string; risk: null; action: "approval" }[],
  })),
  {
    id: "github-read",
    label: "GitHub (read)",
    summary: "Read code on GitHub",
    description: "Read-only GitHub access",
    tools: { "github.repository.read": { repository: "*" } },
    rules: [],
  },
];

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function grantRecord(
  capability: string,
  scope: Record<string, unknown> = {},
  extra: { problems?: string[]; connection_name?: string | null } = {},
) {
  return {
    id: `grant-${capability}-${JSON.stringify(scope)}`,
    agent_id: "agent-1",
    capability,
    scope_json: scope,
    effect: "allow",
    created_at: "2026-08-18T00:00:00Z",
    problems: extra.problems ?? [],
    connection_name: extra.connection_name ?? null,
  };
}

type Row = ReturnType<typeof grantRecord>;

/** What the server would say about each bundle on this agent, from the
 * catalog, the stored grants and the connections. */
function bundleStatuses(catalog: ToolInfo[], store: Row[]) {
  return BUNDLE_DEFS.map((def) => {
    const present = catalog.filter((tool) => tool.name in def.tools);
    const capabilities = [...new Set(present.map((tool) => tool.required_capability))];
    const missingTools = Object.keys(def.tools).filter((name) => !catalog.some((t) => t.name === name));
    const covered = capabilities.filter((cap) =>
      store.some((g) => g.effect === "allow" && grantCovers(g.capability, cap) && g.problems.length === 0),
    );
    const types = [...new Set(present.map((tool) => tool.name.split(".", 1)[0]))].filter(
      (type) => !["organization", "skills", "memory"].includes(type),
    );
    const needs = types
      .filter((type) => !connections.some((c) => c.connector_type === type && c.status === "active"))
      .map((type) => ({ kind: "connect" as const, connector_type: type, choices: [], detail: "" }));
    return {
      id: def.id,
      label: def.label,
      summary: def.summary,
      description: def.description,
      tools: present.map((tool) => ({ name: tool.name, capability: tool.required_capability, scope: def.tools[tool.name] })),
      rules: def.rules,
      not_included: [],
      readiness: {
        state: missingTools.length === Object.keys(def.tools).length ? "unavailable" : needs.length ? "needs" : "ready",
        needs,
        missing_tools: missingTools,
      },
      state:
        capabilities.length === 0 ? "off" : covered.length === capabilities.length ? "on" : covered.length ? "partial" : "off",
      granted_capabilities: covered,
      missing_capabilities: capabilities.filter((cap) => !covered.includes(cap)),
      problems: [],
    };
  });
}

function plannedRows(def: (typeof BUNDLE_DEFS)[number], catalog: ToolInfo[], body: Record<string, unknown>) {
  const chosen = body.connections as Record<string, string>;
  const rows: Row[] = [];
  for (const tool of catalog.filter((t) => t.name in def.tools)) {
    const type = tool.name.split(".", 1)[0];
    const scope: Record<string, string> = {};
    if (tool.scope_keys.includes("connection_id")) {
      scope.connection_id = chosen[type] ?? (type === "cli" && body.sandbox ? "sandbox-new" : "");
    }
    for (const [key, value] of Object.entries(def.tools[tool.name])) scope[key] = value;
    const row = grantRecord(tool.required_capability, scope, { connection_name: connections.find((c) => c.id === scope.connection_id)?.name });
    if (!rows.some((r) => r.id === row.id)) rows.push(row);
  }
  return rows;
}

function renderToolsAccess(
  toolCatalog: ToolInfo[] = accessTools,
  initialGrants: Row[] = [],
  initialRules: { capability: string; risk: string | null; action: string }[] = [],
) {
  const grantBodies: Record<string, unknown>[] = [];
  const policyBodies: Record<string, unknown>[] = [];
  const bundleBodies: Record<string, unknown>[] = [];
  const bundleDeletes: string[] = [];
  const revoked: string[] = [];
  const store = [...initialGrants];
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    if (path.endsWith("/api/v1/connectors")) return json([]);
    if (path.endsWith("/agents/agent-1/grants") && method === "GET") return json(store);
    if (path.endsWith("/agents/agent-1/grants") && method === "POST") {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      grantBodies.push(body);
      store.push(grantRecord(String(body.capability), body.scope as Record<string, unknown>));
      return json({});
    }
    if (path.includes("/agents/agent-1/grants/") && method === "DELETE") {
      const id = decodeURIComponent(path.split("/").pop()!);
      revoked.push(id);
      const index = store.findIndex((grant) => grant.id === id);
      if (index >= 0) store.splice(index, 1);
      return json({});
    }
    if (path.endsWith("/agents/agent-1/bundles") && method === "GET") {
      return json(bundleStatuses(toolCatalog, store));
    }
    const bundleMatch = path.match(/\/agents\/agent-1\/bundles\/([a-z-]+)(\?dry_run=true)?$/);
    if (bundleMatch && method === "POST") {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      bundleBodies.push(body);
      const def = BUNDLE_DEFS.find((candidate) => candidate.id === bundleMatch[1])!;
      const rows = plannedRows(def, toolCatalog, body);
      const created = rows.filter((row) => !store.some((existing) => existing.id === row.id));
      const existing = rows.filter((row) => store.some((existingRow) => existingRow.id === row.id));
      if (!body.dry_run) store.push(...created);
      return json({
        bundle_id: def.id,
        dry_run: body.dry_run,
        created_connection: body.sandbox ? { id: "sandbox-new", name: (body.sandbox as { name: string }).name } : null,
        grants_created: created,
        grants_existing: existing,
        rules_added: def.rules,
        rules_kept: [],
        callable_tools: rows.map((row) => row.capability),
        needs: [],
        warnings: [],
      });
    }
    if (bundleMatch && method === "DELETE") {
      const dryRun = Boolean(bundleMatch[2]);
      bundleDeletes.push(`${bundleMatch[1]}${dryRun ? "?dry_run=true" : ""}`);
      const def = BUNDLE_DEFS.find((candidate) => candidate.id === bundleMatch[1])!;
      const capabilities = new Set(
        toolCatalog.filter((tool) => tool.name in def.tools).map((tool) => tool.required_capability),
      );
      const targets = store.filter((row) => row.effect === "allow" && capabilities.has(row.capability));
      const handMade = targets.filter((row) => {
        const tool = toolCatalog.find((t) => t.required_capability === row.capability && t.name in def.tools)!;
        const fixed = def.tools[tool.name];
        return Object.entries(row.scope_json).some(([key, value]) => key !== "connection_id" && fixed[key] !== value);
      });
      if (!dryRun) {
        for (const row of targets) store.splice(store.indexOf(row), 1);
      }
      return json({ bundle_id: def.id, dry_run: dryRun, revoked: targets, hand_made: handMade });
    }
    if (path.endsWith("/agents/agent-1/policy")) {
      if (method === "PUT") {
        policyBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return json({ preset: null, autonomy_level: "supervised", rules: initialRules });
      }
      return json({ preset: "balanced", autonomy_level: "supervised", rules: initialRules });
    }
    if (path.endsWith("/tools")) return json(toolCatalog);
    if (path.endsWith("/connections")) return json(connections);
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
  return { grantBodies, policyBodies, bundleBodies, bundleDeletes, revoked };
}

describe("ToolsAccessTab", () => {
  it("blocks incomplete required scopes and preserves CLI and delegation semantics", async () => {
    const { grantBodies } = renderToolsAccess();
    fireEvent.click(await screen.findByTestId("advanced-access-toggle"));
    const capabilityPicker = screen.getByLabelText("Capability");
    const add = screen.getByRole("button", { name: "Add" });

    fireEvent.change(capabilityPicker, { target: { value: "vercel.deployment.read" } });
    // The only Vercel connection is filled in; the project id is not guessed.
    expect((screen.getByLabelText(/Connection — Required for this tool/) as HTMLSelectElement).value).toBe(
      "vercel-connection",
    );
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
    expect((within(cliScope).getByLabelText("Connection") as HTMLSelectElement).value).toBe("cli-connection");
    expect((within(cliScope).getByLabelText("Command pattern") as HTMLInputElement).value).toBe("*");
    expect(within(cliScope).getAllByText("Default: any — narrow it if you want").length).toBeGreaterThan(0);
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

  it("refuses a malformed repository before the server sees it", async () => {
    const { grantBodies } = renderToolsAccess(codeEditingTools);
    fireEvent.click(await screen.findByTestId("advanced-access-toggle"));
    fireEvent.change(screen.getByLabelText("Capability"), { target: { value: "github.repository.read" } });
    const repository = screen.getByLabelText(/^Repository/);
    expect((repository as HTMLInputElement).value).toBe("*");
    fireEvent.change(repository, { target: { value: "../x" } });
    expect(screen.getByRole("alert").textContent).toBe("Use owner/name, owner/*, or * for every repository.");
    expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
    expect(grantBodies).toEqual([]);
  });
});

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

  it("turns a connector capability on through the dialog and off after a preview", async () => {
    const { bundleBodies, bundleDeletes, grantBodies } = renderToolsAccess(webAccessTools);
    const button = await screen.findByTestId("capability-preset-web-access");
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByTestId("no-grants")).toBeDefined();

    fireEvent.click(button);
    const dialog = await screen.findByTestId("bundle-setup-dialog");
    expect((within(dialog).getByLabelText("Connection") as HTMLSelectElement).value).toBe("web-connection");
    fireEvent.click(within(dialog).getByRole("button", { name: "Next" }));
    await waitFor(() => expect(bundleBodies).toHaveLength(1));
    expect(bundleBodies[0]).toEqual({
      connections: { web: "web-connection" },
      repositories: ["*"],
      base: "*",
      dry_run: true,
    });
    await within(dialog).findByText("Show the 2 grants and 0 rules this writes");
    fireEvent.click(within(dialog).getByRole("button", { name: "Turn on Web search & browsing" }));
    await waitFor(() => expect(bundleBodies).toHaveLength(2));
    expect(bundleBodies[1]).toEqual({ ...bundleBodies[0], dry_run: false });
    // One POST per apply; the grants endpoint is never touched.
    expect(grantBodies).toEqual([]);
    await waitFor(() =>
      expect(screen.getByTestId("capability-preset-web-access").getAttribute("aria-pressed")).toBe("true"),
    );
    expect(screen.getByTestId("bundle-notice").textContent).toBe(
      "Web search & browsing is on: 2 grants written, 0 approval rules added.",
    );
    expect(screen.queryByTestId("advanced-access")).toBeNull();

    fireEvent.click(screen.getByTestId("capability-preset-web-access"));
    await waitFor(() => expect(bundleDeletes).toEqual(["web-access?dry_run=true"]));
    const confirm = await screen.findByTestId("confirm-dialog");
    expect(screen.getByRole("heading", { name: "Turn off Web search & browsing?" })).toBeDefined();
    expect(confirm.textContent).toContain(
      "This revokes 2 grants. Anything else the agent can do stays as it is.",
    );
    fireEvent.click(within(confirm).getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(bundleDeletes).toEqual(["web-access?dry_run=true", "web-access"]));
    await waitFor(() => expect(screen.getByTestId("no-grants")).toBeDefined());
  });

  it("names hand-made rows when turning a bundle off", async () => {
    renderToolsAccess(webAccessTools, [
      grantRecord("web.search", { connection_id: "web-connection" }),
      grantRecord("web.fetch", { connection_id: "web-connection", domain: "docs.example.com" }),
    ]);
    const button = await screen.findByTestId("capability-preset-web-access");
    await waitFor(() => expect(button.getAttribute("aria-pressed")).toBe("true"));
    fireEvent.click(button);
    const confirm = await screen.findByTestId("confirm-dialog");
    expect(confirm.textContent).toContain(
      "This revokes 2 grants, including 1 you added by hand: web.fetch. Anything else the agent can do stays as it is.",
    );
    fireEvent.click(within(confirm).getByRole("button", { name: "Keep" }));
    await waitFor(() => expect(screen.queryByTestId("confirm-dialog")).toBeNull());
  });

  it("disables a capability whose tools this workspace does not offer", async () => {
    renderToolsAccess(webAccessTools);
    const codeEditing = await screen.findByTestId("capability-preset-code-editing");
    expect((codeEditing as HTMLButtonElement).disabled).toBe(true);
    expect(codeEditing.getAttribute("title")).toContain("This workspace's catalog does not include:");
  });

  it("opens the dialog for a capability that still needs a connection", async () => {
    // No Linear-style gap here: GitHub (read) needs a GitHub connection, and
    // the harness has one, so remove it for this case.
    const withoutGithub = connections.splice(connections.findIndex((c) => c.connector_type === "github"), 1);
    try {
      renderToolsAccess(codeEditingTools);
      const tile = await screen.findByTestId("capability-preset-github-read");
      expect((tile as HTMLButtonElement).disabled).toBe(false);
      expect(tile.textContent).toContain("Needs a GitHub connection — set it up here");
      fireEvent.click(tile);
      const dialog = await screen.findByTestId("bundle-setup-dialog");
      expect(within(dialog).getByText(/No active GitHub connection/)).toBeDefined();
    } finally {
      connections.push(...withoutGithub);
    }
  });

  it("sends code editing through the server with the sandbox it already has", async () => {
    // The approval rule the bundle promises is the server's to write now;
    // the client posts one body and never touches the policy endpoint.
    const { bundleBodies, policyBodies } = renderToolsAccess(codeEditingTools);
    fireEvent.click(await screen.findByTestId("capability-preset-code-editing"));
    const dialog = await screen.findByTestId("bundle-setup-dialog");
    expect((within(dialog).getByLabelText("Connection") as HTMLSelectElement).value).toBe("github-connection");
    fireEvent.click(within(dialog).getByRole("button", { name: "Next" }));
    expect((within(dialog).getByLabelText("Use an existing sandbox") as HTMLInputElement).checked).toBe(true);
    fireEvent.click(within(dialog).getByRole("button", { name: "Next" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Next" }));
    await within(dialog).findByText("Show the 3 grants and 1 rules this writes");
    fireEvent.click(within(dialog).getByRole("button", { name: "Turn on Code editing" }));
    await waitFor(() => expect(bundleBodies).toHaveLength(2));
    expect(bundleBodies[1]).toEqual({
      connections: { github: "github-connection", cli: "cli-connection" },
      repositories: ["*"],
      base: "*",
      dry_run: false,
    });
    expect(policyBodies).toEqual([]);
    await waitFor(() =>
      expect(screen.getByTestId("capability-preset-code-editing").getAttribute("aria-pressed")).toBe("true"),
    );
  });

  it("marks a partial bundle and a grant that cannot work as written", async () => {
    renderToolsAccess(codeEditingTools, [
      grantRecord("github.repository.read", { connection_id: "github-connection" }, { connection_name: "GitHub" }),
      grantRecord(
        "cli.repository.checkout",
        { connection_id: "gone" },
        { problems: ["Connection no longer exists."] },
      ),
    ]);
    const tile = await screen.findByTestId("capability-preset-code-editing");
    expect(tile.getAttribute("data-state")).toBe("partial");
    expect(tile.textContent).toContain("1 of 3 capabilities granted · Finish setup");
    expect(screen.getByText("needs attention")).toBeDefined();
    expect(screen.getByText("Connection no longer exists.")).toBeDefined();
    expect(screen.getByTestId("grant-problems-note").textContent).toContain(
      "1 grants cannot work as written.",
    );
    expect(screen.getByText("connection_id=GitHub")).toBeDefined();
    // Both rows sit outside a bundle that is on, so the editor is already open.
    if (screen.getByTestId("advanced-access-toggle").getAttribute("aria-expanded") !== "true") {
      fireEvent.click(screen.getByTestId("advanced-access-toggle"));
    }
    expect(screen.getByTestId("tool-cli.repository.checkout").textContent).toContain("granted, needs attention");
    expect(screen.getByTestId("tool-github.repository.read").textContent).toContain("granted");
  });

  it("shows which rules survive a change of mode", async () => {
    renderToolsAccess(codeEditingTools, [], [
      { capability: "cli.repository.push", risk: null, action: "approval" },
    ]);
    expect(await screen.findByTestId("kept-rules-note")).toBeDefined();
    expect(screen.getByText("kept when the mode changes")).toBeDefined();
  });
});
