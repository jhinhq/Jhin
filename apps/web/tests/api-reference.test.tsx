/** The API reference: the readers that turn an OpenAPI document into rows a
 * table can show, and the page that renders them. The fixture below is a
 * miniature of the real document — same `$ref` indirection, same `anyOf`
 * nullability, same `x-jhin-scope` — so the helpers are exercised on the shape
 * the API actually emits. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ApiDocsPage from "@/app/(app)/api-docs/page";
import { api } from "@/lib/api";
import {
  authOf,
  bodySchema,
  curlFor,
  fieldRows,
  groupByTag,
  matchesQuery,
  parseInline,
  parseMarkdown,
  prettifyTitle,
  successResponse,
  typeName,
} from "@/lib/openapi";
import type { Spec } from "@/lib/openapi";
import { WorkspaceProvider } from "@/lib/workspace-context";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const SPEC: Spec = {
  openapi: "3.1.0",
  info: {
    title: "Jhin",
    version: "0.1.0",
    summary: "Run an AI company.",
    description: "## Authenticating\n\nSend `Authorization: Bearer jhin_...`.",
    license: { name: "Apache License 2.0" },
    "x-api-version": "v1",
  },
  servers: [{ url: "/" }],
  tags: [
    { name: "agents", description: "Create and configure agents." },
    { name: "secrets", description: "Workspace secrets." },
    { name: "health", description: "Liveness." },
  ],
  paths: {
    "/api/v1/workspaces/{workspace_id}/agents": {
      get: {
        operationId: "list_agents",
        summary: "List Agents",
        tags: ["agents"],
        "x-jhin-scope": "agents:read",
        security: [{ SessionCookie: [] }, { ApiKeyBearer: ["agents:read"] }],
        description: "**Scope.** An API key needs `agents:read`.",
        parameters: [
          {
            name: "workspace_id",
            in: "path",
            required: true,
            schema: { type: "string", format: "uuid" },
          },
        ],
        responses: {
          "200": {
            content: {
              "application/json": {
                schema: { type: "array", items: { $ref: "#/components/schemas/Agent" } },
              },
            },
          },
        },
      },
      post: {
        operationId: "create_agent",
        summary: "Create Agent",
        tags: ["agents"],
        "x-jhin-scope": "agents:write",
        security: [{ SessionCookie: [] }, { ApiKeyBearer: ["agents:write"] }],
        description: "**Scope.** An API key needs `agents:write`.",
        requestBody: {
          content: { "application/json": { schema: { $ref: "#/components/schemas/AgentCreate" } } },
        },
        responses: {
          "201": {
            content: { "application/json": { schema: { $ref: "#/components/schemas/Agent" } } },
          },
        },
      },
    },
    "/api/v1/workspaces/{workspace_id}/secrets": {
      get: {
        operationId: "list_secrets",
        summary: "List Secrets",
        tags: ["secrets"],
        security: [{ SessionCookie: [] }],
        description: "**Session only.** Sealed against API keys.",
        responses: {},
      },
    },
    "/api/v1/health": {
      get: {
        operationId: "health",
        summary: "Liveness",
        tags: ["health"],
        security: [],
        description: "**Auth.** None.",
        responses: {},
      },
    },
  },
  components: {
    schemas: {
      Agent: {
        type: "object",
        properties: {
          id: { type: "string", format: "uuid" },
          name: { type: "string", description: "What the agent is called." },
          status: { $ref: "#/components/schemas/AgentStatus" },
          manager: { anyOf: [{ $ref: "#/components/schemas/Agent" }, { type: "null" }] },
          note: { anyOf: [{ type: "string" }, { type: "null" }] },
        },
        required: ["id", "name", "status"],
      },
      AgentCreate: {
        type: "object",
        properties: { name: { type: "string" }, title: { type: "string" } },
        required: ["name"],
      },
      AgentStatus: { type: "string", enum: ["active", "paused"] },
    },
    securitySchemes: {
      ApiKeyBearer: { type: "http", scheme: "bearer", bearerFormat: "jhin_<prefix>_<secret>" },
      SessionCookie: { type: "apiKey", in: "cookie", name: "jhin_session" },
    },
  },
};

/* ---------------------------------------------------------------- helpers */

describe("reading the document", () => {
  it("groups endpoints into the sections the document declares, in its order", () => {
    const groups = groupByTag(SPEC);
    expect(groups.map((group) => group.name)).toEqual(["agents", "secrets", "health"]);
    expect(groups[0].endpoints).toHaveLength(2);
    expect(groups[0].description).toBe("Create and configure agents.");
  });

  it("reads which credential each endpoint takes straight from its security list", () => {
    expect(authOf(SPEC.paths["/api/v1/health"].get)).toBe("public");
    expect(authOf(SPEC.paths["/api/v1/workspaces/{workspace_id}/secrets"].get)).toBe("session");
    expect(authOf(SPEC.paths["/api/v1/workspaces/{workspace_id}/agents"].get)).toBe(
      "key-or-session",
    );
  });

  it("tidies the titles FastAPI derives from handler names", () => {
    expect(prettifyTitle({ summary: "List Api Keys" }, "get", "/x")).toBe("List API Keys");
    expect(prettifyTitle({}, "get", "/x")).toBe("GET /x");
  });

  it("names types through refs, arrays, and nullable unions", () => {
    const agent = SPEC.components!.schemas!.Agent;
    expect(typeName(agent.properties!.id, SPEC)).toBe("string · uuid");
    expect(typeName(agent.properties!.status, SPEC)).toBe("AgentStatus");
    expect(typeName(agent.properties!.note, SPEC)).toBe("string | null");
    expect(typeName({ type: "array", items: { $ref: "#/components/schemas/Agent" } }, SPEC)).toBe(
      "Agent[]",
    );
  });

  it("flattens a response body into rows, resolving refs and marking required", () => {
    const [, body] = successResponse(SPEC.paths["/api/v1/workspaces/{workspace_id}/agents"].get)!;
    const rows = fieldRows(bodySchema(body), SPEC);
    const byName = Object.fromEntries(rows.map((row) => [row.name, row]));
    expect(byName.id.required).toBe(true);
    expect(byName.name.description).toBe("What the agent is called.");
    expect(byName.note.required).toBe(false);
    expect(byName.status.enum).toEqual(['"active"', '"paused"']);
  });

  it("does not loop forever on a schema that contains itself", () => {
    const rows = fieldRows({ $ref: "#/components/schemas/Agent" }, SPEC, { maxDepth: 6 });
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThan(200);
  });

  it("writes a curl line with the caller's own workspace already substituted", () => {
    const endpoint = groupByTag(SPEC)[0].endpoints.find((e) => e.method === "get")!;
    const curl = curlFor(endpoint, { origin: "https://jhin.example", workspaceId: "w-42" });
    expect(curl).toContain("curl -X GET");
    expect(curl).toContain('-H "Authorization: Bearer jhin_');
    expect(curl).toContain("https://jhin.example/api/v1/workspaces/w-42/agents");
  });

  it("leaves the auth header off a public endpoint", () => {
    const endpoint = groupByTag(SPEC).find((g) => g.name === "health")!.endpoints[0];
    expect(curlFor(endpoint, { origin: "https://jhin.example" })).not.toContain("Authorization");
  });

  it("searches by path, name, and scope", () => {
    const [agents] = groupByTag(SPEC);
    const list = agents.endpoints[0];
    expect(matchesQuery(list, "agents:read")).toBe(true);
    expect(matchesQuery(list, "workspaces")).toBe(true);
    expect(matchesQuery(list, "triggers")).toBe(false);
    expect(matchesQuery(list, "")).toBe(true);
  });
});

describe("the small markdown reader", () => {
  it("keeps code, bold, and links apart from prose", () => {
    expect(parseInline("Send `Bearer x` **now** [docs](https://d)")).toEqual([
      { kind: "text", text: "Send " },
      { kind: "code", text: "Bearer x" },
      { kind: "text", text: " " },
      { kind: "strong", text: "now" },
      { kind: "text", text: " " },
      { kind: "link", text: "docs", href: "https://d" },
    ]);
  });

  it("reads headings, paragraphs, lists, and fenced code", () => {
    const blocks = parseMarkdown("## Auth\n\nUse a key.\n\n- one\n- two\n\n```\ncurl x\n```\n");
    expect(blocks.map((block) => block.kind)).toEqual([
      "heading",
      "paragraph",
      "list",
      "code",
    ]);
    expect(blocks[3]).toEqual({ kind: "code", text: "curl x" });
  });
});

/* ------------------------------------------------------------------- page */

function renderPage() {
  vi.mocked(api).mockResolvedValue(SPEC);
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkspaceProvider
        user={{
          id: "u-1",
          email: "qa@jhin.dev",
          display_name: "QA",
          created_at: "2026-01-01T00:00:00Z",
        }}
        workspace={{
          workspace_id: "w-42",
          workspace_name: "QA Fresh",
          workspace_slug: "qa-fresh",
          role: "owner",
        }}
      >
        <ApiDocsPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}


describe("ApiDocsPage", () => {
  it("reads the session-authenticated document, not the anonymous one", async () => {
    renderPage();
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalled());
    expect(vi.mocked(api).mock.calls[0][0]).toBe("/api/v1/openapi.json");
  });

  it("renders a table of contents with every group the document declares", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByTestId("docs-nav")).toBeDefined());
    const nav = screen.getByTestId("docs-nav");
    for (const name of ["agents", "secrets", "health"]) {
      expect(within(nav).getByRole("link", { name: new RegExp(name) })).toBeDefined();
    }
  });

  it("gives each group nav entry an anchor to a section that exists on the page", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByTestId("docs-nav")).toBeDefined());
    const link = within(screen.getByTestId("docs-nav")).getByRole("link", { name: /agents/ });
    expect(link.getAttribute("href")).toBe("#tag-agents");
    // The anchor resolves: a section with that id is actually rendered.
    expect(document.getElementById("tag-agents")).not.toBeNull();
    fireEvent.click(link); // wired up, does not throw
  });

  it("lists every endpoint collapsed, showing method, path, and scope up front", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("endpoint")).toHaveLength(4));
    // Collapsed: the detail body (its tables) is not rendered yet.
    expect(screen.queryByTestId("endpoint-detail")).toBeNull();
    expect(screen.getAllByText("/api/v1/workspaces/{workspace_id}/agents")).toHaveLength(2);
    expect(screen.getAllByText("agents:read").length).toBeGreaterThan(0);
    expect(screen.getByText("List Agents")).toBeDefined();
  });

  it("colour-codes the method with a badge on every row", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("endpoint")).toHaveLength(4));
    const inRows = (method: string) =>
      screen.getAllByText(method).filter((el) => el.closest('[data-testid="endpoint"]'));
    // Three GETs (agents, secrets, health) and one POST (create agent).
    expect(inRows("get")).toHaveLength(3);
    expect(inRows("post")).toHaveLength(1);
  });

  it("expands an operation to reveal its parameter and response tables", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("endpoint")).toHaveLength(4));
    fireEvent.click(screen.getByRole("button", { name: /List Agents/ }));
    await waitFor(() =>
      expect(screen.getAllByText("What the agent is called.").length).toBeGreaterThan(0),
    );
    expect(screen.getAllByText("AgentStatus").length).toBeGreaterThan(0);
    // The curl example points at the caller's own workspace.
    expect(screen.getAllByText(/workspaces\/w-42\/agents/).length).toBeGreaterThan(0);
  });

  it("opens the operation named by a deep link and shows its detail", async () => {
    window.location.hash = "#get-api-v1-workspaces-workspace-id-agents";
    renderPage();
    await waitFor(() => expect(screen.getByTestId("endpoint-detail")).toBeDefined());
    window.location.hash = "";
  });

  it("filters both the nav and the list, keeping the scope filter power", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("endpoint")).toHaveLength(4));
    fireEvent.change(screen.getByTestId("search-desktop"), {
      target: { value: "agents:write" },
    });
    await waitFor(() => expect(screen.getAllByTestId("endpoint")).toHaveLength(1));
    // The nav narrows too: only the agents group survives.
    const nav = screen.getByTestId("docs-nav");
    expect(within(nav).getByRole("link", { name: /agents/ })).toBeDefined();
    expect(within(nav).queryByRole("link", { name: /secrets/ })).toBeNull();
    expect(screen.getByTestId("result-count").textContent).toBe("1 of 4 endpoints");
  });

  it("says nothing matches when the filter empties the document", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("endpoint")).toHaveLength(4));
    fireEvent.change(screen.getByTestId("search-desktop"), {
      target: { value: "no-such-endpoint" },
    });
    await waitFor(() => expect(screen.getByText("Nothing matches")).toBeDefined());
  });

  it("has a mobile drawer that reveals the group nav on demand", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("endpoint")).toHaveLength(4));
    // Closed by default: only the (desktop) sidebar nav is in the tree.
    expect(screen.queryByTestId("docs-drawer")).toBeNull();
    expect(screen.getAllByTestId("docs-nav")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /Jump to section/ }));
    const drawer = await screen.findByTestId("docs-drawer");
    expect(within(drawer).getByRole("link", { name: /agents/ })).toBeDefined();
    expect(screen.getAllByTestId("docs-nav")).toHaveLength(2);
  });

  it("reports the API version and the app version it is describing", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByTestId("spec-version")).toBeDefined());
    expect(screen.getByTestId("spec-version").textContent).toContain("0.1.0");
    expect(screen.getByTestId("spec-version").textContent).toContain("4 endpoints");
  });

  it("says so plainly when the document cannot be read", async () => {
    vi.mocked(api).mockRejectedValue(new Error("nope"));
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <WorkspaceProvider
          user={{
            id: "u-1",
            email: "qa@jhin.dev",
            display_name: "QA",
            created_at: "2026-01-01T00:00:00Z",
          }}
          workspace={{
            workspace_id: "w-42",
            workspace_name: "QA Fresh",
            workspace_slug: "qa-fresh",
            role: "owner",
          }}
        >
          <ApiDocsPage />
        </WorkspaceProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText(/could not be loaded/)).toBeDefined());
  });
});
