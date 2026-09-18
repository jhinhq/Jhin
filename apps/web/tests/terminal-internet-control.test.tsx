import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TerminalInternetControl, type TerminalInternetStatus } from "@/components/org/terminal-internet-control";
import { ApiError } from "@/lib/api";

const mocks = vi.hoisted(() => ({ api: vi.fn(), updated: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), api: mocks.api }));
const endpoint = "/api/v1/workspaces/w/agents/bisby/terminal-internet";
const initial: TerminalInternetStatus = {
  enabled: false, status: "off", connection_id: null,
  connections: [{ id: "cli-1", name: "Local sandbox" }, { id: "cli-2", name: "Second sandbox" }],
  has_custom_grants: false,
};
function mount(status = initial) {
  mocks.api.mockResolvedValue(status);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><TerminalInternetControl workspaceId="w" agentId="bisby" onUpdated={mocks.updated} /></QueryClientProvider>);
  return client;
}
async function open() {
  fireEvent.click(screen.getByRole("button", { name: "Configure Internet access" }));
  return screen.findByRole("combobox", { name: "CLI sandbox" });
}
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it("loads only when opened and enables the explicitly selected sandbox", async () => {
  mount();
  expect(mocks.api).not.toHaveBeenCalled();
  const select = await open();
  expect((screen.getByRole("button", { name: "Enable Internet" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(select, { target: { value: "cli-2" } });
  expect(mocks.api).toHaveBeenCalledTimes(1);
  mocks.api.mockResolvedValueOnce({ ...initial, status: "enabled", enabled: true, connection_id: "cli-2" });
  fireEvent.click(screen.getByRole("button", { name: "Enable Internet" }));
  await screen.findByText("On");
  expect(mocks.api).toHaveBeenLastCalledWith(endpoint, { method: "PUT", body: { enabled: true, connection_id: "cli-2" } });
  expect(mocks.updated).toHaveBeenCalledOnce();
});

it("recognizes an existing Internet grant and sends an explicit off request", async () => {
  mount({ ...initial, status: "enabled", enabled: true, connection_id: "cli-1" });
  await open();
  expect(screen.getByText("On")).toBeDefined();
  expect((screen.getByRole("button", { name: "Enable Internet" }) as HTMLButtonElement).disabled).toBe(true);
  mocks.api.mockResolvedValueOnce({ ...initial, status: "blocked" });
  fireEvent.click(screen.getByRole("button", { name: "Turn off Internet" }));
  await screen.findByText("Off — explicitly blocked");
  expect(mocks.api).toHaveBeenLastCalledWith(endpoint, { method: "PUT", body: { enabled: false } });
});

it("distinguishes custom policy from off and explains individual restrictions", async () => {
  mount({ ...initial, status: "custom", has_custom_grants: true });
  await open();
  expect(screen.getByText("Custom permissions")).toBeDefined();
  expect(screen.getByText(/Advanced grants may allow Internet access or restrict individual commands/)).toBeDefined();
  expect(screen.queryByText("Off")).toBeNull();
});

it("keeps the real status when saving fails and permits retry", async () => {
  mount({ ...initial, connections: initial.connections.slice(0, 1) });
  await open();
  mocks.api.mockRejectedValueOnce(new ApiError(422, "This sandbox is no longer active."));
  fireEvent.click(screen.getByRole("button", { name: "Enable Internet" }));
  await screen.findByText("This sandbox is no longer active.");
  expect(screen.queryByText("On")).toBeNull();
  expect(mocks.updated).not.toHaveBeenCalled();
  await waitFor(() => expect((screen.getByRole("button", { name: "Enable Internet" }) as HTMLButtonElement).disabled).toBe(false));
});

it("requires a usable sandbox and explains how to create one", async () => {
  mount({ ...initial, connections: [] });
  fireEvent.click(screen.getByRole("button", { name: "Configure Internet access" }));
  await screen.findByText(/Add or enable a CLI Sandbox in Apps/);
  expect((screen.getByRole("button", { name: "Enable Internet" }) as HTMLButtonElement).disabled).toBe(true);
  expect(mocks.api).toHaveBeenCalledTimes(1);
});

it("shows a loading error without presenting it as denied access", async () => {
  mount();
  mocks.api.mockRejectedValue(new ApiError(503, "Temporarily unavailable."));
  fireEvent.click(screen.getByRole("button", { name: "Configure Internet access" }));
  await screen.findByText("Temporarily unavailable.");
  expect(screen.queryByRole("button", { name: "Enable Internet" })).toBeNull();
  expect(screen.queryByText("Off")).toBeNull();
});
