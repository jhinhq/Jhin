import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryCaptureSettings } from "@/components/settings/memory-capture";
import { api } from "@/lib/api";
import { WorkspaceProvider } from "@/lib/workspace-context";
import type { WorkspaceRole } from "@/lib/types";

vi.mock("@/lib/api", async () => ({
  ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), api: vi.fn(),
}));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function mount(role: WorkspaceRole = "admin", policies: unknown[] = []) {
  vi.mocked(api).mockImplementation(async (path) => {
    if (path.endsWith("/teams")) return [{ id: "marketing", name: "Marketing" }, { id: "engineering", name: "Engineering" }];
    if (path.endsWith("/agents")) return [{ id: "writer", name: "Mindy", team_id: "engineering" }, { id: "reviewer", name: "Ashley", team_id: "marketing" }];
    return policies;
  });
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceProvider user={{ id: "u", email: "u@example.test", display_name: "Owner", created_at: "2026-01-01" }}
      workspace={{ workspace_id: "workspace", workspace_name: "Company", workspace_slug: "company", role }}>
      <MemoryCaptureSettings />
    </WorkspaceProvider>
  </QueryClientProvider>);
}

it("does not expose policy controls or fetch authority for members", () => {
  mount("member");
  expect(screen.queryByText("Shared memory capture")).toBeNull();
  expect(api).not.toHaveBeenCalled();
});

it("submits the exact selected non-primary team with no backdated authority", async () => {
  mount();
  await screen.findByLabelText("Mindy can remember");
  fireEvent.change(screen.getByLabelText("Destination team"), { target: { value: "marketing" } });
  fireEvent.click(screen.getByLabelText("Mindy can remember"));
  fireEvent.click(screen.getByLabelText("Editorial style"));
  fireEvent.click(screen.getByRole("button", { name: "Save capture policy" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith(
    "/api/v1/workspaces/workspace/memory-capture-policies",
    { method: "POST", body: { scope: "team", scope_id: "marketing", actor_ids: ["writer"],
      allowed_classes: ["editorial_style"], allowed_source_agent_ids: [] } },
  ));
});

it("maps company to the current workspace and lets admins revoke an exact policy", async () => {
  mount("owner", [{ id: "policy", scope: "workspace", scope_id: "workspace",
    actor_ids_json: ["writer"], allowed_classes_json: ["company_fact"], allowed_source_agent_ids_json: [],
    effective_from: "2026-09-16T01:00:00Z", expires_at: null, revoked_at: null }]);
  await screen.findByRole("button", { name: "Revoke policy" });
  fireEvent.click(screen.getByRole("button", { name: "Revoke policy" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith(
    "/api/v1/workspaces/workspace/memory-capture-policies/policy/revoke", { method: "POST" },
  ));
  fireEvent.change(screen.getByLabelText("Memory audience"), { target: { value: "workspace" } });
  fireEvent.click(screen.getByLabelText("Mindy can remember"));
  fireEvent.click(screen.getByLabelText("Company facts"));
  fireEvent.click(screen.getByRole("button", { name: "Save capture policy" }));
  await waitFor(() => expect(api).toHaveBeenCalledWith(
    "/api/v1/workspaces/workspace/memory-capture-policies",
    { method: "POST", body: { scope: "workspace", scope_id: "workspace", actor_ids: ["writer"],
      allowed_classes: ["company_fact"], allowed_source_agent_ids: [] } },
  ));
});
