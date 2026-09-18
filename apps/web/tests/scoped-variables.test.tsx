import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { VariablesPanel } from "@/components/variables/variables-panel";
import { api, ApiError } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), api: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear(); });
const row = { id: "v1", workspace_id: "ws", name: "GHOST_ADMIN_KEY", scope: "agent", scope_id: "a1", sensitive: true, configured: true, version: 3, description: "Publishing credential", created_by_type: "user", created_by_id: "u1", created_at: "2026-09-12T00:00:00Z", updated_at: "2026-09-12T00:00:00Z" };
function setup(scope: "agent" | "team" | "company" = "agent", canWrite = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><VariablesPanel workspaceId="ws" scope={scope} scopeId={scope === "agent" ? "a1" : scope === "team" ? "t1" : "ws"} scopeName={scope} canWrite={canWrite} /></QueryClientProvider>);
  return client;
}
it.each(["agent", "team", "company"] as const)("loads the exact %s scope and shows ordinary values", async (scope) => {
  vi.mocked(api).mockResolvedValue({ items: [{ ...row, name: "SITE_URL", sensitive: false, value: "https://site.example" }], total: 1 });
  setup(scope);
  expect(await screen.findByText("https://site.example")).toBeDefined();
  expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws/variables", expect.objectContaining({ params: expect.objectContaining({ scope, scope_id: scope === "agent" ? "a1" : scope === "team" ? "t1" : "ws" }) }));
});
it("keeps sensitive values out of the display and query/mutation caches, clears replacement immediately", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api).mockImplementation(async (_path, options) => options?.method ? new Promise((resolve) => { finish = resolve; }) : { items: [{ ...row, value: "unexpected-secret" }], total: 1 });
  const client = setup();
  await screen.findByText("GHOST_ADMIN_KEY");
  expect(document.body.textContent).not.toContain("unexpected-secret");
  expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain("unexpected-secret");
  fireEvent.click(screen.getByRole("button", { name: "Replace secret" }));
  const input = screen.getByLabelText("New secret value") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "test-only-secret-value" } });
  fireEvent.click(screen.getByRole("button", { name: "Save secret" }));
  expect(input.value).toBe("");
  expect(client.getMutationCache().getAll()).toHaveLength(0);
  expect(localStorage.length).toBe(0);
  finish({ ...row, version: 4 });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(document.body.textContent).not.toContain("test-only-secret-value");
});
it("requires explicit reload after a stale replacement and never restores the secret", async () => {
  vi.mocked(api).mockImplementation(async (_path, options) => { if (options?.method) throw new ApiError(409, "Stale version"); return { items: [row], total: 1 }; });
  setup(); await screen.findByText("GHOST_ADMIN_KEY");
  fireEvent.click(screen.getByRole("button", { name: "Replace secret" }));
  fireEvent.change(screen.getByLabelText("New secret value"), { target: { value: "ephemeral" } });
  fireEvent.click(screen.getByRole("button", { name: "Save secret" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringMatching(/changed.*reload/i));
  expect((screen.getByLabelText("New secret value") as HTMLInputElement).value).toBe("");
  expect((screen.getByRole("button", { name: "Save secret" }) as HTMLButtonElement).disabled).toBe(true);
});
it("offers no write or reveal controls to viewers", async () => {
  vi.mocked(api).mockResolvedValue({ items: [row], total: 1 }); setup("agent", false);
  await screen.findByText("GHOST_ADMIN_KEY");
  expect(screen.queryByRole("button", { name: /Add variable|Replace secret|Delete|Reveal/ })).toBeNull();
});
it("keeps ordinary edit fields named independently of their saved values", async () => {
  vi.mocked(api).mockResolvedValue({items:[{...row,sensitive:false,value:"https://example.com/saved"}],total:1});
  setup(); await screen.findByText("GHOST_ADMIN_KEY"); fireEvent.click(screen.getByRole("button",{name:"Edit"}));
  expect(screen.getByLabelText("Value",{exact:true})).toHaveProperty("value","https://example.com/saved");
  expect(screen.getByLabelText("Description",{exact:true})).toHaveProperty("value","Publishing credential");
});
it("creates an ordinary value and removes it using the observed version", async () => {
  const ordinary = {...row,name:"SITE_URL",sensitive:false,value:"https://site.example"};
  let items: unknown[] = [];
  vi.mocked(api).mockImplementation(async (_path, options) => { if(options?.method === "POST") {items=[ordinary];return ordinary;} if(options?.method === "DELETE") {items=[];return undefined;} return {items,total:items.length}; });
  setup("team"); await screen.findByText(/No variables saved/);
  fireEvent.click(screen.getByRole("button", {name:"Add variable"}));
  fireEvent.change(screen.getByLabelText("Name"),{target:{value:"SITE_URL"}});
  fireEvent.change(screen.getByLabelText("Value"),{target:{value:"https://site.example"}});
  fireEvent.click(screen.getByRole("button",{name:"Save variable"}));
  await screen.findByText("https://site.example");
  expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws/variables",expect.objectContaining({method:"POST",body:expect.objectContaining({scope:"team",scope_id:"t1",sensitive:false,name:"SITE_URL"})}));
  fireEvent.click(screen.getByRole("button",{name:"Delete"}));fireEvent.click(screen.getByRole("button",{name:"Delete variable"}));
  await screen.findByText(/No variables saved/);
  expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws/variables/v1",expect.objectContaining({method:"DELETE",params:{expected_version:3}}));
});
it("copies a secret to an explicit team scope without requesting or sending its value", async () => {
  vi.mocked(api).mockImplementation(async (path) => path.endsWith("/org-graph") ? {teams:[{id:"t1",name:"Marketing"}],agents:[{id:"a1",name:"Blogger"}]} : path.endsWith("/copy") ? {...row,id:"copied",scope:"team",scope_id:"t1"} : {items:[row],total:1});
  setup(); await screen.findByText("GHOST_ADMIN_KEY"); fireEvent.click(screen.getByRole("button",{name:"Copy to scope"}));
  const destination = await screen.findByRole("combobox",{name:"Destination"});
  await screen.findByRole("option",{name:"Team: Marketing"});
  fireEvent.change(destination,{target:{value:"team:t1"}});fireEvent.click(screen.getByRole("button",{name:"Copy variable"}));
  await waitFor(()=>expect(screen.queryByRole("dialog")).toBeNull());
  expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws/variables/v1/copy",expect.objectContaining({method:"POST",body:{expected_version:3,scope:"team",scope_id:"t1",name:"GHOST_ADMIN_KEY"}}));
});
