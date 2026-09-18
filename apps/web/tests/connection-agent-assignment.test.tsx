import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ConnectionAgentAssignment } from "@/components/connection-agent-assignment";
import type { ConnectionInfo, ToolInfo } from "@/lib/types";
import { ApiError } from "@/lib/api";

const mocks = vi.hoisted(() => ({ api: vi.fn(), tools: [] as ToolInfo[], connectionTools: [] as {name: string; risk?: ToolInfo["risk"]}[], invalidate: vi.fn(), refreshCatalog: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), api: mocks.api }));
vi.mock("@/lib/hooks", () => ({
  useAgents: () => ({ data: [{ id: "bisby", name: "Bisby", status: "active" }], isPending: false }),
  useTools: () => ({ data: mocks.tools, isPending: false, refetch: mocks.refreshCatalog }),
  useConnectionTools: () => ({ data: { tools: mocks.connectionTools }, isPending: false }),
  useInvalidateAgentAccess: () => mocks.invalidate,
}));
const connection: ConnectionInfo = { id: "app-1", name: "Supabase", connector_type: "supabase", auth_type: "management_token", status: "active", public_id: "p", config_json: {}, created_by_user_id: null, created_at: "", last_verified_at: null, last_error: null, webhook_secret_configured: false };
function tool(name: string, risk: ToolInfo["risk"] = "read", required: string[] = ["connection_id"]): ToolInfo {
  return { name, description: name, risk, required_capability: `${name}.capability`, supports_approval: true, scope_keys: ["connection_id", ...required.filter((key) => key !== "connection_id")], required_grant_scope_keys: required, input_schema: {} };
}
function mount(app = connection) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ConnectionAgentAssignment workspaceId="w" connection={app} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Give to an agent…" }));
  fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "bisby" } });
  return client;
}
afterEach(() => { cleanup(); vi.clearAllMocks(); mocks.tools = []; mocks.connectionTools = []; });

it.each(["supabase", "mcp.supabase", "composio.notion"])("assigns only selected read tools for %s pinned to this app", async (namespace) => {
  mocks.tools = [tool(`${namespace}.read`), tool(`${namespace}.write`, "write"), tool("other.read")];
  mocks.connectionTools = mocks.tools.slice(0, 2);
  mocks.api.mockResolvedValue({});
  const client = mount();
  const invalidateQueries = vi.spyOn(client, "invalidateQueries");
  expect((screen.getByRole("button", { name: "Assign to agent" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Select read-only" }));
  fireEvent.click(screen.getByRole("button", { name: "Assign to agent" }));
  await screen.findByText(/1 tool grant saved/);
  expect(mocks.api).toHaveBeenCalledExactlyOnceWith("/api/v1/workspaces/w/agents/bisby/grants", {
    method: "POST", body: { capability: `${namespace}.read.capability`, scope: { connection_id: "app-1" }, effect: "allow" },
  });
  expect(mocks.invalidate).toHaveBeenCalled();
  expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["connection-access-summary", "w", "app-1"] });
});

it("uses this connection's risk overrides for the read-only preset", () => {
  mocks.tools = [tool("app.overridden")]; mocks.connectionTools = [{ name: "app.overridden", risk: "destructive" }];
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Select read-only" }));
  expect((screen.getByRole("checkbox", { name: "app.overridden" }) as HTMLInputElement).checked).toBe(false);
  expect(mocks.api).not.toHaveBeenCalled();
});

it("offers a catalog refresh after tools are discovered before their grant metadata loads", () => {
  mocks.connectionTools = [{ name: "mcp.new.read" }];
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Refresh tool catalog" }));
  expect(mocks.refreshCatalog).toHaveBeenCalledOnce();
  expect(mocks.api).not.toHaveBeenCalled();
});

it("shows actionable server validation without reporting a failed assignment as authorized", async () => {
  mocks.tools = [tool("supabase.read")]; mocks.connectionTools = mocks.tools;
  mocks.api.mockRejectedValueOnce(new ApiError(422, "This project is outside the connection allow-list."));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Select all" }));
  fireEvent.click(screen.getByRole("button", { name: "Assign to agent" }));
  await screen.findByText("This project is outside the connection allow-list.");
  expect(screen.getByRole("status").textContent).toContain("0 tool grants saved; 1 failed");
});

it("blocks assignment to a disabled connection", () => {
  mocks.tools = [tool("supabase.read")]; mocks.connectionTools = mocks.tools;
  mount({ ...connection, status: "disabled" });
  fireEvent.click(screen.getByRole("button", { name: "Select all" }));
  expect((screen.getByRole("button", { name: "Assign to agent" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/Reconnect or enable this app/)).toBeDefined();
  expect(mocks.api).not.toHaveBeenCalled();
});

it("requires per-tool scope fields and never exposes a replaceable connection pin", async () => {
  mocks.tools = [tool("supabase.table.read", "read", ["connection_id", "project_ref"])];
  mocks.connectionTools = mocks.tools;
  mocks.api.mockResolvedValue({});
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Select all" }));
  expect((screen.getByRole("button", { name: "Assign to agent" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByLabelText(/Connection/)).toBeNull();
  fireEvent.change(screen.getByLabelText("Project reference — Required for this tool"), { target: { value: " project-one " } });
  fireEvent.click(screen.getByRole("button", { name: "Assign to agent" }));
  await screen.findByText(/1 tool grant saved/);
  expect(mocks.api.mock.calls[0][1].body.scope).toEqual({ connection_id: "app-1", project_ref: "project-one" });
});

it("reports partial success and retries only failed tools without changing denies or policies", async () => {
  mocks.tools = [tool("app.first"), tool("app.second")]; mocks.connectionTools = mocks.tools;
  mocks.api.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error("private infrastructure detail")).mockResolvedValueOnce({});
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Select all" }));
  fireEvent.click(screen.getByRole("button", { name: "Assign to agent" }));
  await screen.findByText(/1 tool grant saved; 1 failed/);
  expect(screen.queryByText(/private infrastructure/)).toBeNull();
  expect(screen.getByText(/Existing deny grants and approval policies still apply/)).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "Retry failed assignments" }));
  await screen.findByText(/2 tool grants saved/);
  expect(mocks.api).toHaveBeenCalledTimes(3);
  expect(mocks.api.mock.calls.map((call) => call[1].body.capability)).toEqual(["app.first.capability", "app.second.capability", "app.second.capability"]);
  await waitFor(() => expect(mocks.invalidate).toHaveBeenCalledTimes(2));
});
