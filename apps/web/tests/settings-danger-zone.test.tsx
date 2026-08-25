/** The delete-workspace danger zone: who sees it, what the confirmation says,
 * the typed-name gate, and where the user lands afterwards. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DangerZone, describeDeletion } from "@/components/settings/danger-zone";
import { api } from "@/lib/api";
import { useWorkspaceDeletionSummary } from "@/lib/hooks";
import type { WorkspaceDeletionSummary, WorkspaceRole } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
}));

vi.mock("@/lib/hooks", () => ({
  useWorkspaceDeletionSummary: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const NAME = "Acme Robotics";

function summary(overrides: Partial<WorkspaceDeletionSummary> = {}): WorkspaceDeletionSummary {
  return {
    workspace_id: "w1",
    name: NAME,
    agents: 12,
    teams: 2,
    tasks: 7,
    conversations: 34,
    messages: 501,
    memories: 18,
    skills: 5,
    connections: 3,
    triggers: 0,
    api_keys: 1,
    secrets: 0,
    members: 4,
    ...overrides,
  };
}

function renderZone(
  role: WorkspaceRole,
  state: Partial<ReturnType<typeof useWorkspaceDeletionSummary>> = {},
) {
  vi.mocked(useWorkspaceDeletionSummary).mockReturnValue({
    data: summary(),
    isPending: false,
    error: null,
    ...state,
  } as unknown as ReturnType<typeof useWorkspaceDeletionSummary>);

  return render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkspaceProvider
        user={{
          id: "u1",
          email: "owner@example.com",
          display_name: "Owner",
          created_at: "2026-01-01T00:00:00Z",
        }}
        workspace={{
          workspace_id: "w1",
          workspace_name: NAME,
          workspace_slug: "acme-robotics",
          role,
        }}
      >
        <DangerZone />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

function openDialog() {
  fireEvent.click(screen.getByRole("button", { name: "Delete this workspace" }));
}

function confirmButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /Delete workspace permanently/ }) as HTMLButtonElement;
}

describe("describeDeletion", () => {
  it("names only what is actually there, with singular and plural right", () => {
    expect(describeDeletion(summary({ agents: 1, teams: 0, conversations: 2 }))).toEqual([
      "1 agent",
      "2 chats",
      "501 messages",
      "7 tasks",
      "18 memories",
      "5 skills",
      "3 connected apps",
      "1 API key",
    ]);
  });

  it("says nothing at all about an empty workspace", () => {
    const empty = summary({
      agents: 0,
      teams: 0,
      tasks: 0,
      conversations: 0,
      messages: 0,
      memories: 0,
      skills: 0,
      connections: 0,
      triggers: 0,
      api_keys: 0,
      secrets: 0,
    });
    expect(describeDeletion(empty)).toEqual([]);
  });
});

describe("DangerZone visibility", () => {
  it("shows for the owner", () => {
    renderZone("owner");
    expect(screen.getByTestId("danger-zone")).toBeDefined();
  });

  for (const role of ["admin", "member", "viewer"] as const) {
    it(`renders nothing at all for a ${role}`, () => {
      renderZone(role);
      expect(screen.queryByTestId("danger-zone")).toBeNull();
      expect(screen.queryByText(/Danger zone/)).toBeNull();
      expect(screen.queryByRole("button", { name: /Delete this workspace/ })).toBeNull();
    });
  }
});

describe("the confirmation dialog", () => {
  it("counts what will be destroyed and says whose data it is", () => {
    renderZone("owner");
    openDialog();
    expect(screen.getByText("12 agents")).toBeDefined();
    expect(screen.getByText("34 chats")).toBeDefined();
    expect(screen.getByText("3 connected apps")).toBeDefined();
    // Zero categories are left out rather than padded in.
    expect(screen.queryByText(/automations/)).toBeNull();
    expect(screen.getByText(/4 people lose access/)).toBeDefined();
    expect(screen.getByText(/Their Jhin accounts are not deleted/)).toBeDefined();
    expect(screen.getByText(/every member/)).toBeDefined();
    expect(screen.getByText(/cannot be undone/)).toBeDefined();
  });

  it("does not talk about a lone owner in the third person", () => {
    renderZone("owner", { data: summary({ members: 1 }) });
    openDialog();
    expect(
      screen.getByText(/You are its only member\. Your Jhin account is not deleted/),
    ).toBeDefined();
  });

  it("admits it when the counts could not be loaded rather than inventing them", () => {
    renderZone("owner", { data: undefined, isPending: false, error: new Error("boom") });
    openDialog();
    expect(screen.getByText(/could not be counted/)).toBeDefined();
    expect(screen.queryByText(/12 agents/)).toBeNull();
  });

  it("keeps the confirm button disabled until the name is typed exactly", () => {
    renderZone("owner");
    openDialog();
    const field = screen.getByLabelText(`Type ${NAME} to confirm deletion`);

    expect(confirmButton().disabled).toBe(true);

    fireEvent.change(field, { target: { value: "Acme" } });
    expect(confirmButton().disabled).toBe(true);

    fireEvent.change(field, { target: { value: "acme robotics" } });
    expect(confirmButton().disabled).toBe(true);

    fireEvent.change(field, { target: { value: `  ${NAME}  ` } });
    expect(confirmButton().disabled).toBe(false);

    fireEvent.change(field, { target: { value: NAME } });
    expect(confirmButton().disabled).toBe(false);
  });

  it("deletes the workspace and leaves the dead workspace behind", async () => {
    vi.mocked(api).mockResolvedValue(undefined);
    renderZone("owner");
    openDialog();
    fireEvent.change(screen.getByLabelText(`Type ${NAME} to confirm deletion`), {
      target: { value: NAME },
    });
    fireEvent.click(confirmButton());

    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1", { method: "DELETE" }),
    );
    // Home, not the settings page it was deleted from: the shell re-derives the
    // current workspace from a fresh /auth/me, or shows its no-membership screen.
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/home"));
  });

  it("shows the API's refusal instead of navigating away", async () => {
    const { ApiError } = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    vi.mocked(api).mockRejectedValue(new ApiError(403, "Only an owner can delete a workspace"));
    renderZone("owner");
    openDialog();
    fireEvent.change(screen.getByLabelText(`Type ${NAME} to confirm deletion`), {
      target: { value: NAME },
    });
    fireEvent.click(confirmButton());

    await waitFor(() =>
      expect(screen.getByText("Only an owner can delete a workspace")).toBeDefined(),
    );
    expect(replace).not.toHaveBeenCalled();
  });
});
