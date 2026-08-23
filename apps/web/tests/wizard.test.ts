import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import NewAgentPage from "@/app/(app)/agents/new/page";
import type { Agent, ToolInfo } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";
import {
  AGENT_TEMPLATES,
  applyTemplate,
  canSubmit,
  EMPTY_WIZARD,
  firstInvalidStep,
  toCreatePayload,
  grantPayloadsForTools,
  toggleTool,
  validateIdentity,
  validateStep,
  WIZARD_STEPS,
} from "@/lib/wizard";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: navigation.push }),
  useSearchParams: () => new URLSearchParams(),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  navigation.push.mockReset();
});

describe("wizard validation", () => {
  it("requires a name in the identity step", () => {
    expect(validateIdentity(EMPTY_WIZARD)).toEqual(["Name is required."]);
    expect(validateIdentity({ ...EMPTY_WIZARD, name: "   " })).toEqual(["Name is required."]);
    expect(validateIdentity({ ...EMPTY_WIZARD, name: "CTO" })).toEqual([]);
  });

  it("rejects overlong names and role titles", () => {
    const long = "x".repeat(201);
    expect(validateIdentity({ ...EMPTY_WIZARD, name: long })).toContain(
      "Name must be at most 200 characters.",
    );
    expect(
      validateIdentity({ ...EMPTY_WIZARD, name: "ok", roleTitle: long }),
    ).toContain("Role title must be at most 200 characters.");
  });

  it("firstInvalidStep points at the identity step for an empty wizard", () => {
    expect(firstInvalidStep(EMPTY_WIZARD)).toBe(1);
    expect(canSubmit(EMPTY_WIZARD)).toBe(false);
  });

  it("a named agent can be submitted (other steps are optional)", () => {
    const state = { ...EMPTY_WIZARD, name: "QA Engineer" };
    expect(firstInvalidStep(state)).toBeNull();
    expect(canSubmit(state)).toBe(true);
  });

  it("disabled phase steps never validate-block", () => {
    for (const step of WIZARD_STEPS.filter((s) => s.disabledPhase)) {
      expect(validateStep(step.id, EMPTY_WIZARD)).toEqual([]);
    }
  });

  it("only step 7 (budget) remains stubbed — tools & autonomy are live in Phase 4", () => {
    const disabled = WIZARD_STEPS.filter((s) => s.disabledPhase).map((s) => s.id);
    expect(disabled).toEqual([7]);
  });
});

describe("applyTemplate", () => {
  const generic = AGENT_TEMPLATES.find((t) => t.id === "generic") ?? AGENT_TEMPLATES[0];
  const cto = AGENT_TEMPLATES[0];

  it("fills an empty role title and name", () => {
    const next = applyTemplate({ name: "", roleTitle: "" }, generic);
    expect(next.roleTitle).toBe(generic.roleTitle);
    expect(next.name).toBe(generic.name);
    expect(next.systemPrompt).toBe(generic.systemPrompt);
  });

  it("keeps a role title and name the user typed", () => {
    const next = applyTemplate({ name: "Bisby", roleTitle: "Friendly assistant" }, generic);
    expect(next.roleTitle).toBe("Friendly assistant");
    expect(next.name).toBe("Bisby");
    expect(next.systemPrompt).toBe(generic.systemPrompt);
  });

  it("replaces a role title that came from another template", () => {
    const next = applyTemplate({ name: "Bisby", roleTitle: cto.roleTitle }, generic);
    expect(next.roleTitle).toBe(generic.roleTitle);
  });
});

describe("wizard payload", () => {
  it("maps empty selects to nulls and trims text", () => {
    const payload = toCreatePayload({
      ...EMPTY_WIZARD,
      name: "  CTO  ",
      roleTitle: " Chief ",
      teamId: "",
      managerAgentId: "",
    });
    expect(payload).toMatchObject({
      name: "CTO",
      role_title: "Chief",
      team_id: null,
      manager_agent_id: null,
      model_profile_id: null,
    });
  });

  it("passes through team, manager, and model profile ids", () => {
    const payload = toCreatePayload({
      ...EMPTY_WIZARD,
      name: "SWE",
      teamId: "team-1",
      managerAgentId: "agent-1",
      modelProfileId: "profile-1",
    });
    expect(payload.team_id).toBe("team-1");
    expect(payload.manager_agent_id).toBe("agent-1");
    expect(payload.model_profile_id).toBe("profile-1");
  });

  it("carries the chosen autonomy level (defaults to supervised)", () => {
    expect(toCreatePayload({ ...EMPTY_WIZARD, name: "SWE" }).autonomy_level).toBe("supervised");
    expect(
      toCreatePayload({ ...EMPTY_WIZARD, name: "SWE", autonomyLevel: "manual" }).autonomy_level,
    ).toBe("manual");
  });
});

describe("wizard tool grants", () => {
  it("toggles tool names on and off", () => {
    let state = { ...EMPTY_WIZARD, name: "SWE" };
    state = toggleTool(state, "system.echo");
    state = toggleTool(state, "system.time");
    expect(state.grantToolNames).toEqual(["system.echo", "system.time"]);
    state = toggleTool(state, "system.echo");
    expect(state.grantToolNames).toEqual(["system.time"]);
  });

  it("starts with no grants (deny-by-default) and the balanced preset", () => {
    expect(EMPTY_WIZARD.grantToolNames).toEqual([]);
    expect(EMPTY_WIZARD.grantScopes).toEqual({});
    expect(EMPTY_WIZARD.approvalPreset).toBe("balanced");
  });

  it("keeps tools sharing a capability as distinct scoped grant payloads", () => {
    const tools = [
      { name: "supabase.database.select", required_capability: "supabase.database.read", scope_keys: ["connection_id", "project_ref", "schema"], required_grant_scope_keys: ["connection_id", "project_ref", "schema"] },
      { name: "supabase.database.explain", required_capability: "supabase.database.read", scope_keys: ["connection_id", "project_ref", "schema"], required_grant_scope_keys: ["connection_id", "project_ref", "schema"] },
    ] as import("@/lib/types").ToolInfo[];
    const state = {
      ...EMPTY_WIZARD,
      grantToolNames: tools.map((tool) => tool.name),
      grantScopes: {
        "supabase.database.select": { connection_id: "conn-1", project_ref: "one", schema: "public" },
        "supabase.database.explain": { connection_id: "conn-1", project_ref: "two", schema: "audit" },
      },
    };
    expect(grantPayloadsForTools(state, tools)).toEqual([
      { capability: "supabase.database.read", scope: { connection_id: "conn-1", project_ref: "one", schema: "public" }, effect: "allow" },
      { capability: "supabase.database.read", scope: { connection_id: "conn-1", project_ref: "two", schema: "audit" }, effect: "allow" },
    ]);
  });

  it("collapses only exact capability-and-scope duplicates", () => {
    const tools = [
      {
        name: "first",
        description: "First reader",
        risk: "read",
        required_capability: "system.read",
        supports_approval: false,
        scope_keys: [],
        required_grant_scope_keys: [],
        input_schema: {},
      },
      {
        name: "second",
        description: "Second reader",
        risk: "read",
        required_capability: "system.read",
        supports_approval: false,
        scope_keys: [],
        required_grant_scope_keys: [],
        input_schema: {},
      },
    ] as import("@/lib/types").ToolInfo[];
    expect(grantPayloadsForTools({
      ...EMPTY_WIZARD,
      grantToolNames: ["first", "second"],
    }, tools)).toEqual([{ capability: "system.read", scope: {}, effect: "allow" }]);
  });
});

const wizardTools: ToolInfo[] = [
  {
    name: "supabase.database.select",
    description: "Select rows",
    risk: "read",
    required_capability: "supabase.database.read",
    supports_approval: false,
    scope_keys: ["connection_id", "project_ref", "schema"],
    required_grant_scope_keys: ["connection_id", "project_ref", "schema"],
    input_schema: {},
  },
  {
    name: "supabase.database.explain",
    description: "Explain a query",
    risk: "read",
    required_capability: "supabase.database.read",
    supports_approval: false,
    scope_keys: ["connection_id", "project_ref", "schema"],
    required_grant_scope_keys: ["connection_id", "project_ref", "schema"],
    input_schema: {},
  },
];

function response(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function renderWizard() {
  const grantBodies: Record<string, unknown>[] = [];
  const createdAgent: Agent = {
    id: "agent-new",
    workspace_id: "workspace-1",
    team_id: null,
    manager_agent_id: null,
    name: "Database Analyst",
    slug: "database-analyst",
    role_title: "",
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
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    if (path.endsWith("/org-graph")) {
      return response({ workspace_id: "workspace-1", teams: [], agents: [] });
    }
    if (path.endsWith("/model-profiles")) return response([]);
    if (path.endsWith("/tools")) return response(wizardTools);
    if (path.endsWith("/connections")) {
      return response([{ id: "supabase-connection", connector_type: "supabase", name: "Supabase production" }]);
    }
    if (path === "/api/v1/workspaces/workspace-1" && method === "GET") {
      return response({ id: "workspace-1", default_model_profile_id: null });
    }
    if (path.endsWith("/agents") && method === "POST") return response(createdAgent);
    if (path.endsWith("/agents/agent-new/grants") && method === "POST") {
      grantBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return response({});
    }
    if (path.endsWith("/agents/agent-new/policy") && method === "PUT") return response({});
    throw new Error(`Unexpected request: ${method} ${path}`);
  }));
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const workspaceProps = {
    user: { id: "user-1", email: "owner@example.com", display_name: "Owner", created_at: "2026-08-18T00:00:00Z" },
    workspace: { workspace_id: "workspace-1", workspace_name: "Acme", workspace_slug: "acme", role: "owner" as const },
  } as Parameters<typeof WorkspaceProvider>[0];
  render(createElement(
    QueryClientProvider,
    { client: queryClient },
    createElement(
      WorkspaceProvider,
      workspaceProps,
      createElement(NewAgentPage),
    ),
  ));
  return grantBodies;
}

describe("agent wizard rendered grants", () => {
  it("blocks incomplete per-tool scopes and submits distinct scopes for a shared capability", async () => {
    const grantBodies = renderWizard();
    const name = await screen.findByLabelText("Agent name");
    fireEvent.change(name, { target: { value: "Database Analyst" } });
    fireEvent.click(screen.getByRole("button", { name: /Tools & connections/ }));

    for (const tool of wizardTools) {
      const row = screen.getByText(tool.name).closest("label")!;
      fireEvent.click(row.querySelector("input[type=checkbox]")!);
    }
    fireEvent.click(screen.getByText("Review").closest("button")!);
    expect((screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /Tools & connections/ }));
    const connections = screen.getAllByLabelText(/Connection — Required for this tool/);
    const projects = screen.getAllByLabelText(/Project reference — Required for this tool/);
    const schemas = screen.getAllByLabelText(/Schema — Required for this tool/);
    fireEvent.change(connections[0], { target: { value: "supabase-connection" } });
    fireEvent.change(projects[0], { target: { value: "project-one" } });
    fireEvent.change(schemas[0], { target: { value: "public" } });
    fireEvent.change(connections[1], { target: { value: "supabase-connection" } });
    fireEvent.change(projects[1], { target: { value: "project-two" } });
    fireEvent.change(schemas[1], { target: { value: "audit" } });

    fireEvent.click(screen.getByText("Review").closest("button")!);
    const create = screen.getByRole("button", { name: "Create agent" });
    expect((create as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(create);
    await waitFor(() => expect(grantBodies).toHaveLength(2));
    expect(grantBodies).toEqual([
      {
        capability: "supabase.database.read",
        scope: { connection_id: "supabase-connection", project_ref: "project-one", schema: "public" },
        effect: "allow",
      },
      {
        capability: "supabase.database.read",
        scope: { connection_id: "supabase-connection", project_ref: "project-two", schema: "audit" },
        effect: "allow",
      },
    ]);
  });
});

describe("templates", () => {
  it("ships the eight templates from the plan", () => {
    expect(AGENT_TEMPLATES.map((t) => t.name)).toEqual([
      "CTO",
      "Software Engineer",
      "QA Engineer",
      "DevOps",
      "Marketing Director",
      "Blogger",
      "SEO Specialist",
      "Generic Assistant",
    ]);
    for (const template of AGENT_TEMPLATES) {
      expect(template.systemPrompt.length).toBeGreaterThan(20);
      expect(template.roleTitle.length).toBeGreaterThan(0);
    }
  });
});
