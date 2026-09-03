import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import NewAgentPage from "@/app/(app)/agents/new/page";
import type { Agent, ToolInfo } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";
import {
  ADVANCED_STEP,
  AGENT_TEMPLATES,
  applyTemplate,
  applyToolPreset,
  COLLABORATION_PRESET_ID,
  capabilitySummary,
  effectiveAvatar,
  hasManualGrants,
  isPresetApplied,
  isPresetGranted,
  manualGrantNames,
  MANUAL_GRANT_SOURCE,
  PERSONA_STEP,
  presetCapabilities,
  presetConnectionFor,
  presetGrantsToAdd,
  presetGrantsToRevoke,
  presetMissingTools,
  presetScopeGaps,
  removeToolPreset,
  REVIEW_STEP,
  setToolScope,
  toggleToolPreset,
  TOOL_PRESETS,
  canSubmit,
  EMPTY_WIZARD,
  firstInvalidStep,
  monthlyBudgetCents,
  toCreatePayload,
  grantPayloadsForTools,
  parseExpertise,
  toggleTool,
  validateIdentity,
  validatePublicIdentity,
  validateStep,
  WIZARD_STEPS,
  type ToolPreset,
} from "@/lib/wizard";
import { AVATAR_PALETTE, AVATAR_SHAPES } from "@/lib/shapes";
import { delegationScope } from "@/components/org/tools-access-tab";
import { persona } from "./helpers/personas";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: navigation.push }),
  useSearchParams: () => new URLSearchParams(),
}));

/** Every `POST …/agents` body the rendered wizard sent, newest last. */
const agentBodies: Record<string, unknown>[] = [];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  navigation.push.mockReset();
  agentBodies.length = 0;
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

  it("the advanced step accepts the defaults and rejects out-of-range limits", () => {
    expect(validateStep(ADVANCED_STEP, EMPTY_WIZARD)).toEqual([]);
    expect(validateStep(ADVANCED_STEP, { ...EMPTY_WIZARD, maxSteps: "0" })).toEqual([
      "Max steps must be a whole number between 1 and 500.",
    ]);
    expect(validateStep(ADVANCED_STEP, { ...EMPTY_WIZARD, maxSteps: "501" }).length).toBe(1);
    expect(validateStep(ADVANCED_STEP, { ...EMPTY_WIZARD, maxRunMinutes: "1441" }).length).toBe(1);
    expect(validateStep(ADVANCED_STEP, { ...EMPTY_WIZARD, maxConcurrentRuns: "2.5" }).length).toBe(1);
    expect(firstInvalidStep({ ...EMPTY_WIZARD, name: "SWE", maxSteps: "" })).toBe(ADVANCED_STEP);
  });

  it("budget accepts blank or dollar amounts only", () => {
    expect(monthlyBudgetCents("")).toBeNull();
    expect(monthlyBudgetCents("  ")).toBeNull();
    expect(monthlyBudgetCents("5")).toBe(500);
    expect(monthlyBudgetCents("5.50")).toBe(550);
    expect(monthlyBudgetCents("0.01")).toBe(1);
    expect(monthlyBudgetCents("-1")).toBeUndefined();
    expect(monthlyBudgetCents("abc")).toBeUndefined();
    expect(monthlyBudgetCents("5.555")).toBeUndefined();
    expect(validateStep(ADVANCED_STEP, { ...EMPTY_WIZARD, monthlyBudgetDollars: "abc" })).toEqual([
      "Monthly budget must be a dollar amount (e.g. 5 or 5.50), or blank for no budget.",
    ]);
    expect(validateStep(ADVANCED_STEP, { ...EMPTY_WIZARD, monthlyBudgetDollars: "5.50" })).toEqual([]);
  });

  it("is three required steps plus two optional ones: persona and advanced", () => {
    expect(WIZARD_STEPS.map((s) => s.id)).toEqual([1, 2, 3, 4, 5]);
    expect(WIZARD_STEPS.map((s) => s.title)).toEqual([
      "Identity",
      "What it can do",
      "Persona",
      "Advanced setup",
      "Review & create",
    ]);
    expect(WIZARD_STEPS.filter((s) => s.optional).map((s) => s.id)).toEqual([
      PERSONA_STEP,
      ADVANCED_STEP,
    ]);
    expect(WIZARD_STEPS.at(-1)?.id).toBe(REVIEW_STEP);
    // Every step but review is reachable with defaults only.
    expect(firstInvalidStep({ ...EMPTY_WIZARD, name: "SWE" })).toBeNull();
    expect(validateStep(PERSONA_STEP, EMPTY_WIZARD)).toEqual([]);
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

  it("carries run limits and converts the budget to cents", () => {
    const defaults = toCreatePayload({ ...EMPTY_WIZARD, name: "SWE" });
    expect(defaults).toMatchObject({
      max_steps: 20,
      max_run_minutes: 30,
      max_concurrent_runs: 1,
      monthly_budget_cents: null,
    });
    const custom = toCreatePayload({
      ...EMPTY_WIZARD,
      name: "SWE",
      maxSteps: "40",
      maxRunMinutes: "60",
      maxConcurrentRuns: "3",
      monthlyBudgetDollars: "5.50",
    });
    expect(custom).toMatchObject({
      max_steps: 40,
      max_run_minutes: 60,
      max_concurrent_runs: 3,
      monthly_budget_cents: 550,
    });
  });

  it("carries the chosen autonomy level (defaults to supervised)", () => {
    expect(toCreatePayload({ ...EMPTY_WIZARD, name: "SWE" }).autonomy_level).toBe("supervised");
    expect(
      toCreatePayload({ ...EMPTY_WIZARD, name: "SWE", autonomyLevel: "manual" }).autonomy_level,
    ).toBe("manual");
  });

  it("sends the persona as null by default and as its id when picked", () => {
    expect(EMPTY_WIZARD.personaId).toBe("");
    expect(toCreatePayload({ ...EMPTY_WIZARD, name: "SWE" }).persona_id).toBeNull();
    expect(toCreatePayload({ ...EMPTY_WIZARD, name: "SWE", personaId: "p1" }).persona_id).toBe("p1");
  });
});

describe("wizard shape avatar", () => {
  it("derives a deterministic default from the name and honors explicit picks", () => {
    const derived = effectiveAvatar({ name: "Bisby", avatarShape: "", avatarColor: "" });
    expect(derived).toEqual(effectiveAvatar({ name: "Bisby", avatarShape: "", avatarColor: "" }));
    expect(AVATAR_SHAPES.some((shape) => shape.id === derived.shape)).toBe(true);
    expect(AVATAR_PALETTE.some((color) => color.hex === derived.color)).toBe(true);
    expect(effectiveAvatar({ name: "Bisby", avatarShape: "quad", avatarColor: "#3ecf8e" })).toEqual({
      shape: "quad",
      color: "#3ecf8e",
    });
  });

  it("always includes a shape avatar in the create payload", () => {
    const explicit = toCreatePayload({
      ...EMPTY_WIZARD,
      name: "Bisby",
      avatarShape: "tee",
      avatarColor: "#b44351",
    });
    expect(explicit.avatar_shape).toBe("tee");
    expect(explicit.avatar_color).toBe("#b44351");
    const derived = toCreatePayload({ ...EMPTY_WIZARD, name: "Bisby" });
    expect(AVATAR_SHAPES.some((shape) => shape.id === derived.avatar_shape)).toBe(true);
    expect(AVATAR_PALETTE.some((color) => color.hex === derived.avatar_color)).toBe(true);
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

const webTools: ToolInfo[] = [
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

const collaborationTools: ToolInfo[] = [
  {
    name: "organization.directory.search",
    description: "Find colleagues",
    risk: "read",
    required_capability: "organization.directory.read",
    supports_approval: false,
    scope_keys: [],
    required_grant_scope_keys: [],
    input_schema: {},
  },
  {
    name: "organization.request_work",
    description: "Ask a colleague for help",
    risk: "write",
    required_capability: "organization.work.request",
    supports_approval: true,
    scope_keys: ["targets"],
    required_grant_scope_keys: [],
    input_schema: {},
  },
  {
    name: "organization.respond_work_request",
    description: "Respond to a request",
    risk: "write",
    required_capability: "organization.work.respond",
    supports_approval: true,
    scope_keys: [],
    required_grant_scope_keys: [],
    input_schema: {},
  },
];

describe("collaboration preset", () => {
  const preset = TOOL_PRESETS.find((p) => p.id === COLLABORATION_PRESET_ID)!;

  it("is the safe-by-default baseline granting find/ask/respond, never delegation", () => {
    expect(preset).toBeDefined();
    const applied = applyToolPreset(EMPTY_WIZARD, preset, collaborationTools, []);
    expect(new Set(applied.grantToolNames)).toEqual(
      new Set([
        "organization.directory.search",
        "organization.request_work",
        "organization.respond_work_request",
      ]),
    );
    expect(isPresetApplied(applied, preset, collaborationTools)).toBe(true);
    const payloads = grantPayloadsForTools(applied, collaborationTools);
    expect(payloads).toContainEqual({
      capability: "organization.work.request",
      scope: { targets: "any" },
      effect: "allow",
    });
    expect(payloads).toContainEqual({
      capability: "organization.directory.read",
      scope: {},
      effect: "allow",
    });
    expect(payloads).toContainEqual({
      capability: "organization.work.respond",
      scope: {},
      effect: "allow",
    });
    expect(payloads.some((p) => p.capability === "organization.delegate")).toBe(false);
  });

  it("toggles off cleanly, leaving no grants or scopes behind", () => {
    const on = applyToolPreset(EMPTY_WIZARD, preset, collaborationTools, []);
    const off = removeToolPreset(on, preset);
    expect(off.grantToolNames).toEqual([]);
    expect(off.grantScopes).toEqual({});
    expect(isPresetApplied(off, preset, collaborationTools)).toBe(false);
  });
});

function renderWizard(
  toolCatalog: ToolInfo[] = wizardTools,
  connectionList: Record<string, unknown>[] = [
    { id: "supabase-connection", connector_type: "supabase", name: "Supabase production", status: "active" },
  ],
) {
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
    if (path.endsWith("/tools")) return response(toolCatalog);
    if (path.endsWith("/connections")) return response(connectionList);
    // The persona step's library (the path carries `?limit=100`).
    if (path.includes("/personas")) return response({ items: [persona()], total: 1 });
    if (path === "/api/v1/workspaces/workspace-1" && method === "GET") {
      return response({ id: "workspace-1", default_model_profile_id: null });
    }
    if (path.endsWith("/agents") && method === "POST") {
      agentBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return response(createdAgent);
    }
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
    fireEvent.click(screen.getByTestId("wizard-step-2"));
    fireEvent.click(screen.getByTestId("advanced-tools-toggle"));

    for (const tool of wizardTools) {
      const row = screen.getByText(tool.name).closest("label")!;
      fireEvent.click(row.querySelector("input[type=checkbox]")!);
    }
    fireEvent.click(screen.getByTestId(`wizard-step-${REVIEW_STEP}`));
    expect((screen.getByRole("button", { name: "Create agent" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByTestId("wizard-step-2"));
    const connections = screen.getAllByLabelText(/Connection — Required for this tool/);
    const projects = screen.getAllByLabelText(/Project reference — Required for this tool/);
    const schemas = screen.getAllByLabelText(/Schema — Required for this tool/);
    fireEvent.change(connections[0], { target: { value: "supabase-connection" } });
    fireEvent.change(projects[0], { target: { value: "project-one" } });
    fireEvent.change(schemas[0], { target: { value: "public" } });
    fireEvent.change(connections[1], { target: { value: "supabase-connection" } });
    fireEvent.change(projects[1], { target: { value: "project-two" } });
    fireEvent.change(schemas[1], { target: { value: "audit" } });

    fireEvent.click(screen.getByTestId(`wizard-step-${REVIEW_STEP}`));
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

  it("applies the collaboration baseline to a new agent by default", async () => {
    const grantBodies = renderWizard(collaborationTools, []);
    const name = await screen.findByLabelText("Agent name");
    fireEvent.change(name, { target: { value: "Bisby" } });
    fireEvent.click(screen.getByTestId(`wizard-step-${REVIEW_STEP}`));
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() => expect(grantBodies).toHaveLength(3));
    expect(new Set(grantBodies.map((g) => g.capability))).toEqual(
      new Set([
        "organization.directory.read",
        "organization.work.request",
        "organization.work.respond",
      ]),
    );
    const request = grantBodies.find((g) => g.capability === "organization.work.request");
    expect(request?.scope).toEqual({ targets: "any" });
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

describe("public identity (purpose & expertise)", () => {
  it("parses comma/newline separated tags, trimming and de-duplicating", () => {
    expect(parseExpertise(" python, GitHub ,,\n testing, python ")).toEqual([
      "python",
      "GitHub",
      "testing",
    ]);
    expect(parseExpertise("")).toEqual([]);
  });

  it("sends purpose and expertise in the create payload", () => {
    const payload = toCreatePayload({
      ...EMPTY_WIZARD,
      name: "SWE",
      publicPurpose: "  Builds things  ",
      expertise: "python, github",
    });
    expect(payload).toMatchObject({
      public_purpose: "Builds things",
      expertise_json: ["python", "github"],
    });
  });

  it("rejects oversized purpose and tags on the identity step", () => {
    expect(validatePublicIdentity("x".repeat(1001), "")).toEqual([
      "Purpose must be at most 1000 characters.",
    ]);
    expect(validatePublicIdentity("", "y".repeat(65))).toEqual([
      "Each expertise tag must be at most 64 characters.",
    ]);
    expect(
      validatePublicIdentity("", Array.from({ length: 21 }, (_, i) => `t${i}`).join(",")),
    ).toEqual(["At most 20 expertise tags."]);
    expect(validateIdentity({ ...EMPTY_WIZARD, name: "ok", expertise: "a, b" })).toEqual([]);
  });
});

describe("delegationScope", () => {
  it("adds the pin only when a colleague is chosen", () => {
    expect(delegationScope("subordinates", "")).toEqual({ targets: "subordinates" });
    expect(delegationScope("team", "agent-1")).toEqual({
      targets: "team",
      target_agent_id: "agent-1",
    });
  });
});

describe("wizard tool presets", () => {
  const catalog = [
    { name: "cli.repository.checkout", scope_keys: ["connection_id", "repository", "image"] },
    { name: "cli.file.read", scope_keys: ["connection_id", "path"] },
    { name: "cli.file.write", scope_keys: ["connection_id", "path"] },
    { name: "cli.test.run", scope_keys: ["connection_id", "command", "image"] },
    { name: "cli.command.execute", scope_keys: ["connection_id", "command", "image", "network"] },
    { name: "github.repository.read", scope_keys: ["connection_id", "repository"] },
    { name: "github.pull_request.read", scope_keys: ["connection_id", "repository"] },
    { name: "github.pull_request.create", scope_keys: ["connection_id", "repository"] },
    { name: "github.pull_request.merge", scope_keys: ["connection_id", "repository"] },
  ];
  const connections = [
    { id: "cli-1", connector_type: "cli", status: "active" },
    { id: "gh-1", connector_type: "github", status: "active" },
    { id: "gh-old", connector_type: "github", status: "disabled" },
  ];
  const preset = TOOL_PRESETS.find((entry) => entry.id === "code-editing")!;

  it("grants the code-editing bundle with least-privilege scopes and pinned connections", () => {
    const next = applyToolPreset(EMPTY_WIZARD, preset, catalog, connections);
    expect(next.grantToolNames).toEqual([
      "cli.repository.checkout",
      "cli.file.read",
      "cli.file.write",
      "cli.test.run",
      "cli.command.execute",
      "github.repository.read",
      "github.pull_request.read",
      "github.pull_request.create",
    ]);
    expect(next.grantScopes["cli.command.execute"]).toEqual({ connection_id: "cli-1", command: "git *" });
    expect(next.grantScopes["cli.file.write"]).toEqual({ connection_id: "cli-1", path: "*" });
    expect(next.grantScopes["github.pull_request.create"]).toEqual({ connection_id: "gh-1", repository: "*" });
    // Merge is deliberately not part of editing.
    expect(next.grantToolNames).not.toContain("github.pull_request.merge");
    // The payloads are complete grants.
    const payloads = grantPayloadsForTools(next, catalog.map((tool) => ({
      ...tool,
      description: "",
      risk: "write" as const,
      required_capability: tool.name,
      supports_approval: true,
      required_grant_scope_keys: [],
      input_schema: {},
    })));
    expect(payloads).toHaveLength(8);
    expect(payloads.find((p) => p.capability === "cli.repository.checkout")?.scope).toEqual({
      connection_id: "cli-1",
      repository: "*",
    });
  });

  it("keeps scopes the user already typed and is idempotent", () => {
    const seeded = {
      ...EMPTY_WIZARD,
      grantToolNames: ["cli.file.write"],
      grantScopes: { "cli.file.write": { connection_id: "cli-1", path: "docs/*" } },
    };
    const once = applyToolPreset(seeded, preset, catalog, connections);
    expect(once.grantScopes["cli.file.write"]).toEqual({ connection_id: "cli-1", path: "docs/*" });
    const twice = applyToolPreset(once, preset, catalog, connections);
    expect(twice.grantToolNames).toEqual(once.grantToolNames);
    expect(twice.grantScopes).toEqual(once.grantScopes);
  });

  it("leaves the connection blank when it is ambiguous or missing", () => {
    expect(presetConnectionFor("cli.file.read", [])).toBe("");
    expect(
      presetConnectionFor("github.repository.read", [
        { id: "a", connector_type: "github", status: "active" },
        { id: "b", connector_type: "github", status: "active" },
      ]),
    ).toBe("");
    const next = applyToolPreset(EMPTY_WIZARD, preset, catalog, []);
    expect(next.grantScopes["cli.file.read"]).toEqual({ path: "*" });
  });

  it("skips tools the catalog does not offer and reports them", () => {
    const partial = catalog.filter((tool) => tool.name.startsWith("cli."));
    expect(presetMissingTools(preset, partial)).toEqual([
      "github.repository.read",
      "github.pull_request.read",
      "github.pull_request.create",
    ]);
    const next = applyToolPreset(EMPTY_WIZARD, preset, partial, connections);
    expect(next.grantToolNames.every((name) => name.startsWith("cli."))).toBe(true);
  });
});

describe("team building preset", () => {
  const orgCatalog = [
    { name: "organization.create_agent", scope_keys: [] as string[] },
    { name: "organization.update_agent_profile", scope_keys: [] as string[] },
    { name: "organization.create_team", scope_keys: [] as string[] },
    { name: "organization.directory.search", scope_keys: [] as string[] },
  ];
  const preset = TOOL_PRESETS.find((entry) => entry.id === "team-building")!;

  it("exists with a plain-language, approval-forward description", () => {
    expect(preset.label).toBe("Team building");
    expect(preset.description).toContain("human must approve");
    expect(preset.description).toContain("no tool access");
  });

  it("grants exactly the four organization tools with empty scopes", () => {
    const next = applyToolPreset(EMPTY_WIZARD, preset, orgCatalog, []);
    expect(next.grantToolNames).toEqual([
      "organization.create_agent",
      "organization.update_agent_profile",
      "organization.create_team",
      "organization.directory.search",
    ]);
    expect(next.grantScopes["organization.create_agent"]).toEqual({});
  });

  it("dedupes to the two underlying capabilities in the grant payloads", () => {
    const next = applyToolPreset(EMPTY_WIZARD, preset, orgCatalog, []);
    const tools = orgCatalog.map((tool) => ({
      ...tool,
      description: "",
      risk: "elevated" as const,
      required_capability: tool.name.startsWith("organization.directory")
        ? "organization.directory.read"
        : "organization.manage_agents",
      supports_approval: true,
      required_grant_scope_keys: [] as string[],
      input_schema: {},
    }));
    const payloads = grantPayloadsForTools(next, tools);
    expect(payloads.map((payload) => payload.capability).sort()).toEqual([
      "organization.directory.read",
      "organization.manage_agents",
    ]);
    expect(payloads.every((payload) => payload.effect === "allow")).toBe(true);
  });
});

describe("web access preset", () => {
  const catalog = [
    { name: "web.search", scope_keys: ["connection_id"] },
    { name: "web.fetch", scope_keys: ["connection_id", "domain"] },
  ];
  const preset = TOOL_PRESETS.find((entry) => entry.id === "web-access")!;

  it("grants search and fetch with the connection pinned and a broad domain", () => {
    const connections = [{ id: "web-1", connector_type: "web", status: "active" }];
    const next = applyToolPreset(EMPTY_WIZARD, preset, catalog, connections);
    expect(next.grantToolNames).toEqual(["web.search", "web.fetch"]);
    expect(next.grantScopes["web.search"]).toEqual({ connection_id: "web-1" });
    expect(next.grantScopes["web.fetch"]).toEqual({ connection_id: "web-1", domain: "*" });
  });

  it("leaves the connection blank when no web connection exists", () => {
    const next = applyToolPreset(EMPTY_WIZARD, preset, catalog, []);
    expect(next.grantScopes["web.fetch"]).toEqual({ domain: "*" });
  });
});

describe("skill authoring preset", () => {
  const catalog = [
    { name: "skills.create", scope_keys: [] },
    { name: "skills.update", scope_keys: [] },
  ];
  const preset = TOOL_PRESETS.find((entry) => entry.id === "skill-authoring")!;

  it("grants both skills.create and skills.update", () => {
    const next = applyToolPreset(EMPTY_WIZARD, preset, catalog, []);
    expect(next.grantToolNames).toEqual(["skills.create", "skills.update"]);
  });

  it("does not also grant skills.read", () => {
    const next = applyToolPreset(EMPTY_WIZARD, preset, catalog, []);
    expect(next.grantToolNames).not.toContain("skills.read");
  });

  it("is distinct from the read-only skills preset", () => {
    const readPreset = TOOL_PRESETS.find((entry) => entry.id === "skills")!;
    expect(preset.id).not.toBe(readPreset.id);
    expect(Object.keys(preset.tools)).not.toEqual(Object.keys(readPreset.tools));
  });
});

describe("preset attribution (grantSources)", () => {
  const catalog = [
    { name: "skills.read", scope_keys: [] as string[] },
    { name: "skills.create", scope_keys: [] as string[] },
    { name: "web.fetch", scope_keys: ["connection_id", "domain"] },
  ];
  const reader: ToolPreset = {
    id: "reader",
    label: "Reader",
    summary: "Read the skills library",
    description: "",
    tools: { "skills.read": {} },
  };
  const author: ToolPreset = {
    id: "author",
    label: "Author",
    summary: "Write skills",
    description: "",
    // Deliberately overlaps `reader` on skills.read.
    tools: { "skills.read": {}, "skills.create": {} },
  };

  it("records the preset that contributed each grant", () => {
    const next = applyToolPreset(EMPTY_WIZARD, author, catalog, []);
    expect(next.grantSources).toEqual({ "skills.read": ["author"], "skills.create": ["author"] });
    expect(isPresetApplied(next, author, catalog)).toBe(true);
    expect(isPresetApplied(next, reader, catalog)).toBe(false);
    expect(hasManualGrants(next)).toBe(false);
  });

  it("removing a preset takes back exactly what it granted", () => {
    const applied = applyToolPreset(EMPTY_WIZARD, author, catalog, []);
    const removed = removeToolPreset(applied, author);
    expect(removed.grantToolNames).toEqual([]);
    expect(removed.grantScopes).toEqual({});
    expect(removed.grantSources).toEqual({});
    expect(isPresetApplied(removed, author, catalog)).toBe(false);
  });

  it("keeps a shared tool another preset still needs", () => {
    let state = applyToolPreset(EMPTY_WIZARD, reader, catalog, []);
    state = applyToolPreset(state, author, catalog, []);
    expect(state.grantSources["skills.read"]).toEqual(["reader", "author"]);
    const removed = removeToolPreset(state, author);
    expect(removed.grantToolNames).toEqual(["skills.read"]);
    expect(removed.grantSources).toEqual({ "skills.read": ["reader"] });
    expect(isPresetApplied(removed, reader, catalog)).toBe(true);
    expect(isPresetApplied(removed, author, catalog)).toBe(false);
    // …and removing the second preset finally clears it.
    expect(removeToolPreset(removed, reader).grantToolNames).toEqual([]);
  });

  it("keeps a tool the user checked by hand before the preset", () => {
    const manual = toggleTool(EMPTY_WIZARD, "skills.read");
    expect(manual.grantSources["skills.read"]).toEqual([MANUAL_GRANT_SOURCE]);
    const applied = applyToolPreset(manual, author, catalog, []);
    expect(applied.grantSources["skills.read"]).toEqual([MANUAL_GRANT_SOURCE, "author"]);
    const removed = removeToolPreset(applied, author);
    expect(removed.grantToolNames).toEqual(["skills.read"]);
    expect(manualGrantNames(removed)).toEqual(["skills.read"]);
    expect(hasManualGrants(removed)).toBe(true);
  });

  it("never clobbers a scope the user edited by hand", () => {
    const connections = [{ id: "web-1", connector_type: "web", status: "active" }];
    const webPreset: ToolPreset = {
      id: "web",
      label: "Web",
      summary: "Browse",
      description: "",
      tools: { "web.fetch": { domain: "*" } },
    };
    const applied = applyToolPreset(EMPTY_WIZARD, webPreset, catalog, connections);
    expect(applied.grantScopes["web.fetch"]).toEqual({ connection_id: "web-1", domain: "*" });
    const edited = setToolScope(applied, "web.fetch", {
      connection_id: "web-1",
      domain: "docs.example.com",
    });
    expect(edited.grantSources["web.fetch"]).toEqual(["web", MANUAL_GRANT_SOURCE]);
    const removed = removeToolPreset(edited, webPreset);
    expect(removed.grantToolNames).toEqual(["web.fetch"]);
    expect(removed.grantScopes["web.fetch"]).toEqual({
      connection_id: "web-1",
      domain: "docs.example.com",
    });
    expect(isPresetApplied(removed, webPreset, catalog)).toBe(false);
  });

  it("unchecking a preset-granted tool by hand wins and un-applies the preset", () => {
    const applied = applyToolPreset(EMPTY_WIZARD, author, catalog, []);
    const unchecked = toggleTool(applied, "skills.create");
    expect(unchecked.grantToolNames).toEqual(["skills.read"]);
    expect(unchecked.grantSources["skills.create"]).toBeUndefined();
    expect(isPresetApplied(unchecked, author, catalog)).toBe(false);
  });

  it("removing a preset ignores a same-named tool it never granted", () => {
    const manual = toggleTool(EMPTY_WIZARD, "skills.read");
    const removed = removeToolPreset(manual, reader);
    expect(removed.grantToolNames).toEqual(["skills.read"]);
    expect(removed.grantSources["skills.read"]).toEqual([MANUAL_GRANT_SOURCE]);
  });

  it("toggleToolPreset round-trips connection pinning and scopes", () => {
    const connections = [
      { id: "cli-1", connector_type: "cli", status: "active" },
      { id: "gh-1", connector_type: "github", status: "active" },
    ];
    const codeCatalog = [
      { name: "cli.file.write", scope_keys: ["connection_id", "path"] },
      { name: "github.pull_request.create", scope_keys: ["connection_id", "repository"] },
    ];
    const preset = TOOL_PRESETS.find((entry) => entry.id === "code-editing")!;
    const on = toggleToolPreset(EMPTY_WIZARD, preset, codeCatalog, connections);
    expect(on.grantToolNames).toEqual(["cli.file.write", "github.pull_request.create"]);
    expect(on.grantScopes["github.pull_request.create"]).toEqual({
      connection_id: "gh-1",
      repository: "*",
    });
    const off = toggleToolPreset(on, preset, codeCatalog, connections);
    expect(off.grantToolNames).toEqual([]);
    expect(off.grantScopes).toEqual({});
    const again = toggleToolPreset(off, preset, codeCatalog, connections);
    expect(again.grantScopes).toEqual(on.grantScopes);
  });

  it("treats grants with no recorded source as hand-picked", () => {
    const legacy = { ...EMPTY_WIZARD, grantToolNames: ["skills.read"], grantSources: {} };
    expect(hasManualGrants(legacy)).toBe(true);
    expect(isPresetApplied(legacy, reader, catalog)).toBe(false);
  });

  it("summarises capabilities in plain language", () => {
    const state = applyToolPreset(toggleTool(EMPTY_WIZARD, "web.fetch"), author, catalog, []);
    const summary = capabilitySummary(state, catalog, [reader, author]);
    expect(summary.presets.map((preset) => preset.id)).toEqual(["author"]);
    expect(summary.presets[0].summary).toBe("Write skills");
    expect(summary.manualToolNames).toEqual(["web.fetch"]);
    expect(capabilitySummary(EMPTY_WIZARD, catalog, [reader, author])).toEqual({
      presets: [],
      manualToolNames: [],
    });
  });

  it("every shipped preset has a plain-language summary", () => {
    for (const preset of TOOL_PRESETS) {
      expect(preset.summary.length).toBeGreaterThan(10);
      expect(preset.summary).not.toContain(".");
    }
  });
});

describe("presets on the agent edit surface", () => {
  const catalog: ToolInfo[] = [
    {
      name: "skills.read",
      description: "Read a skill",
      risk: "read",
      required_capability: "skills.read",
      supports_approval: false,
      scope_keys: ["name"],
      required_grant_scope_keys: [],
      input_schema: {},
    },
    {
      name: "web.search",
      description: "Search",
      risk: "read",
      required_capability: "web.search",
      supports_approval: false,
      scope_keys: ["connection_id"],
      required_grant_scope_keys: ["connection_id"],
      input_schema: {},
    },
    {
      name: "web.fetch",
      description: "Fetch",
      risk: "read",
      required_capability: "web.fetch",
      supports_approval: false,
      scope_keys: ["connection_id", "domain"],
      required_grant_scope_keys: ["connection_id"],
      input_schema: {},
    },
  ];
  const webPreset = TOOL_PRESETS.find((entry) => entry.id === "web-access")!;
  const skillsPreset = TOOL_PRESETS.find((entry) => entry.id === "skills")!;
  const webConnections = [{ id: "web-1", connector_type: "web", status: "active" }];

  function grant(id: string, capability: string, scope: Record<string, string> = {}) {
    return {
      id,
      agent_id: "agent-1",
      capability,
      scope_json: scope,
      effect: "allow" as const,
      created_at: "2026-08-18T00:00:00Z",
    };
  }

  it("reads a preset back from the saved grants", () => {
    expect(presetCapabilities(webPreset, catalog).sort()).toEqual(["web.fetch", "web.search"]);
    expect(isPresetGranted([], webPreset, catalog)).toBe(false);
    expect(isPresetGranted([grant("g1", "web.search")], webPreset, catalog)).toBe(false);
    expect(
      isPresetGranted([grant("g1", "web.search"), grant("g2", "web.fetch")], webPreset, catalog),
    ).toBe(true);
    // A preset whose tools this workspace does not offer is never "granted".
    expect(isPresetGranted([grant("g1", "skills.read")], webPreset, [])).toBe(false);
  });

  it("adds only the capabilities that are missing", () => {
    const existing = [grant("g1", "web.search", { connection_id: "web-1" })];
    const toAdd = presetGrantsToAdd(existing, webPreset, catalog, webConnections);
    expect(toAdd).toEqual([
      { capability: "web.fetch", scope: { connection_id: "web-1", domain: "*" }, effect: "allow" },
    ]);
    expect(presetGrantsToAdd([], webPreset, catalog, webConnections)).toHaveLength(2);
  });

  it("revokes a preset's grants but keeps what another live preset needs", () => {
    const shared: ToolPreset = {
      id: "shared",
      label: "Shared",
      summary: "Also fetches pages",
      description: "",
      tools: { "web.fetch": { domain: "*" } },
    };
    const grants = [
      grant("g1", "web.search", { connection_id: "web-1" }),
      grant("g2", "web.fetch", { connection_id: "web-1", domain: "docs.example.com" }),
      grant("g3", "skills.read"),
    ];
    expect(presetGrantsToRevoke(grants, webPreset, catalog, []).map((g) => g.id)).toEqual([
      "g1",
      "g2",
    ]);
    // `shared` still needs web.fetch, so only web.search goes.
    expect(presetGrantsToRevoke(grants, webPreset, catalog, [shared]).map((g) => g.id)).toEqual([
      "g1",
    ]);
    // Unrelated grants are never touched.
    expect(
      presetGrantsToRevoke(grants, skillsPreset, catalog, []).map((g) => g.id),
    ).toEqual(["g3"]);
  });

  it("reports preset tools whose required scope cannot be filled in", () => {
    expect(presetScopeGaps(webPreset, catalog, webConnections)).toEqual([]);
    expect(presetScopeGaps(webPreset, catalog, [])).toEqual(["web.search", "web.fetch"]);
    expect(presetScopeGaps(skillsPreset, catalog, [])).toEqual([]);
  });
});

describe("streamlined wizard flow", () => {
  const webConnections = [
    { id: "web-1", connector_type: "web", name: "Web search", status: "active" },
  ];

  it("keeps the tool list collapsed and toggles a capability on and off", async () => {
    renderWizard(webTools, webConnections);
    await screen.findByLabelText("Agent name");
    fireEvent.click(screen.getByTestId("wizard-step-2"));

    expect(screen.queryByTestId("advanced-tools")).toBeNull();
    expect(screen.queryByText("web.fetch")).toBeNull();
    expect(screen.getByTestId("capability-summary").textContent).toContain("Nothing yet");

    const preset = screen.getByTestId("tool-preset-web-access");
    fireEvent.click(preset);
    expect(preset.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("capability-summary").textContent).toContain("Research online");
    expect(screen.getByTestId("advanced-tools-toggle").textContent).toContain("2 tools selected");
    // Preset-granted tools are not hand-picked, so the editor stays collapsed.
    expect(screen.queryByTestId("advanced-tools")).toBeNull();

    fireEvent.click(preset);
    expect(preset.getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByTestId("capability-summary").textContent).toContain("Nothing yet");
    expect(screen.getByTestId("advanced-tools-toggle").textContent).not.toContain("selected");
  });

  it("expands the advanced editor on demand with scopes and connection pinning", async () => {
    renderWizard(webTools, webConnections);
    await screen.findByLabelText("Agent name");
    fireEvent.click(screen.getByTestId("wizard-step-2"));
    fireEvent.click(screen.getByTestId("advanced-tools-toggle"));

    const advanced = screen.getByTestId("advanced-tools");
    expect(advanced.textContent).toContain("web.fetch");
    const row = screen.getByText("web.fetch").closest("label")!;
    fireEvent.click(row.querySelector("input[type=checkbox]")!);
    fireEvent.change(screen.getByLabelText(/Connection — Required for this tool/), {
      target: { value: "web-1" },
    });
    fireEvent.change(screen.getByLabelText("domain"), { target: { value: "docs.example.com" } });
    expect(screen.getByTestId("advanced-tools-toggle").textContent).toContain("1 tool selected");
    expect(screen.getByTestId("capability-summary").textContent).toContain(
      "1 individually chosen tool",
    );
  });

  it("creates an agent from presets and defaults without opening advanced setup", async () => {
    const grantBodies = renderWizard(webTools, webConnections);
    fireEvent.change(await screen.findByLabelText("Agent name"), {
      target: { value: "Researcher" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Continue/ }));
    fireEvent.click(screen.getByTestId("tool-preset-web-access"));
    fireEvent.click(screen.getByTestId("wizard-skip"));

    expect(screen.getByTestId("review-capabilities").textContent).toContain("Research online");
    const create = screen.getByRole("button", { name: "Create agent" });
    expect((create as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(create);
    await waitFor(() => expect(grantBodies).toHaveLength(2));
    expect(grantBodies).toEqual([
      { capability: "web.search", scope: { connection_id: "web-1" }, effect: "allow" },
      { capability: "web.fetch", scope: { connection_id: "web-1", domain: "*" }, effect: "allow" },
    ]);
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/agents/agent-new"));
    // Skipping the optional steps leaves the agent without a persona.
    expect(agentBodies[0]?.persona_id).toBeNull();
  });

  it("picks a persona on its optional step and sends it with the create", async () => {
    renderWizard(webTools, webConnections);
    fireEvent.change(await screen.findByLabelText("Agent name"), { target: { value: "Flight" } });
    fireEvent.click(screen.getByRole("button", { name: /Continue/ }));
    // From "What it can do" the skip covers both optional steps.
    expect(screen.getByTestId("wizard-skip").textContent).toContain("Skip optional steps");
    fireEvent.click(screen.getByRole("button", { name: /Continue/ }));

    const option = await screen.findByTestId("persona-option-mission-control");
    expect(screen.getByTestId("persona-option-none").getAttribute("aria-checked")).toBe("true");
    fireEvent.click(option);
    expect(option.getAttribute("aria-checked")).toBe("true");
    // From the persona step only advanced setup is left to skip.
    expect(screen.getByTestId("wizard-skip").textContent).toContain("Skip advanced setup");
    fireEvent.click(screen.getByTestId("wizard-skip"));

    expect(screen.getByText("Mission Control")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() => expect(agentBodies).toHaveLength(1));
    expect(agentBodies[0].persona_id).toBe("p1");
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/agents/agent-new"));
  });
});
