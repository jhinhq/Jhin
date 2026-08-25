/** Reorganising the company from the Company page: drop-target feedback,
 * refusal of cycles before any request, optimistic moves that revert on
 * failure, the keyboard menu path, and no affordances for non-admins. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CompanyPage from "@/app/(app)/company/page";
import { api, ApiError } from "@/lib/api";
import { useAgentAvatarMap, useInvalidateOrg, useOrgGraph, useTasks } from "@/lib/hooks";
import type { OrgAgentNode, OrgGraph, OrgTeamNode, WorkspaceRole } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

vi.mock("next/navigation", () => ({
  usePathname: () => "/company",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/lib/hooks", () => ({
  useOrgGraph: vi.fn(),
  useAgentAvatarMap: vi.fn(),
  useInvalidateOrg: vi.fn(),
  useTasks: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

const invalidate = vi.fn();

function team(id: string, name: string): OrgTeamNode {
  return {
    id,
    name,
    description: "",
    parent_team_id: null,
    manager_agent_id: null,
    color_token: "indigo",
    icon: "users",
  };
}

function agent(
  id: string,
  name: string,
  teamId: string | null,
  managerId: string | null,
): OrgAgentNode {
  return {
    id,
    name,
    slug: id,
    role_title: `${name} role`,
    status: "active",
    team_id: teamId,
    manager_agent_id: managerId,
  };
}

/** Engineering holds Ada with Bisby reporting to her; Quill is Independent. */
const GRAPH: OrgGraph = {
  workspace_id: "w1",
  teams: [team("eng", "Engineering"), team("mkt", "Marketing")],
  agents: [
    agent("ada", "Ada", "eng", null),
    agent("bisby", "Bisby", "eng", "ada"),
    agent("quill", "Quill", null, null),
  ],
};

beforeEach(() => {
  // dnd-kit scrolls the picked-up node into view; jsdom has no layout.
  Element.prototype.scrollIntoView = vi.fn();
  vi.mocked(useOrgGraph).mockReturnValue({
    data: GRAPH,
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof useOrgGraph>);
  vi.mocked(useAgentAvatarMap).mockReturnValue({});
  vi.mocked(useInvalidateOrg).mockReturnValue(invalidate);
  vi.mocked(useTasks).mockReturnValue({
    data: { items: [] },
  } as unknown as ReturnType<typeof useTasks>);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
});

function renderPage(view: "map" | "outline", role: WorkspaceRole = "admin") {
  localStorage.setItem("jhin-company-view", view);
  return render(
    <WorkspaceProvider
      user={{ id: "u1", email: "qa@jhin.dev", display_name: "QA", created_at: "2026-01-01" }}
      workspace={{
        workspace_id: "w1",
        workspace_name: "QA Fresh",
        workspace_slug: "qa-fresh",
        role,
      }}
    >
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <CompanyPage />
      </QueryClientProvider>
    </WorkspaceProvider>,
  );
}

const dropState = (testId: string) =>
  document.querySelector(`[data-testid="${testId}"]`)?.getAttribute("data-drop-state");

/** Open one agent's "Move…" menu and return its menu items. */
function openMoveMenu(name: string) {
  fireEvent.click(screen.getByRole("button", { name: `Move ${name}` }));
  const menu = screen.getByRole("menu", { name: `Move ${name}` });
  return { menu, item: (label: RegExp) => within(menu).getByRole("menuitem", { name: label }) };
}

describe("Company page — permissions", () => {
  it("gives a non-admin the structure with no way to move anyone", () => {
    renderPage("map", "member");
    expect(screen.getByText("Engineering")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Move / })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Drag / })).toBeNull();
    expect(document.querySelector("[data-testid^='drop-']")).toBeNull();
  });

  it("gives an admin a drag handle and a Move… menu on every agent", () => {
    renderPage("map");
    for (const name of ["Ada", "Bisby", "Quill"]) {
      expect(screen.getByRole("button", { name: `Drag ${name}` })).toBeTruthy();
      expect(screen.getByRole("button", { name: `Move ${name}` })).toBeTruthy();
    }
  });
});

describe("Company page — drag feedback", () => {
  it("marks valid and invalid drop targets while a drag is in progress", () => {
    renderPage("map");
    expect(dropState("drop-agent-bisby")).toBe("idle");

    // Space on the drag handle is the keyboard sensor's pick-up gesture.
    fireEvent.keyDown(screen.getByRole("button", { name: "Drag Ada" }), {
      code: "Space",
      key: " ",
    });

    // Ada cannot report to herself, nor to Bisby, who reports to her.
    expect(dropState("drop-agent-ada")).toBe("invalid");
    expect(dropState("drop-agent-bisby")).toBe("invalid");
    // Quill is unrelated, and every team's top level is always reachable.
    expect(dropState("drop-agent-quill")).toBe("valid");
    expect(dropState("drop-group-mkt")).toBe("valid");
    expect(dropState("drop-group-none")).toBe("valid");
  });

  it("clears the feedback when the drag is cancelled", async () => {
    renderPage("map");
    const handle = screen.getByRole("button", { name: "Drag Bisby" });
    fireEvent.keyDown(handle, { code: "Space", key: " " });
    expect(dropState("drop-agent-bisby")).toBe("invalid");
    // The sensor attaches its document listener on the next tick.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    fireEvent.keyDown(document, { code: "Escape", key: "Escape" });
    await waitFor(() => expect(dropState("drop-agent-bisby")).toBe("idle"));
    expect(vi.mocked(api)).not.toHaveBeenCalled();
  });

  it("announces the drag with instructions for keyboard users", () => {
    renderPage("map");
    fireEvent.keyDown(screen.getByRole("button", { name: "Drag Ada" }), {
      code: "Space",
      key: " ",
    });
    expect(document.body.textContent).toContain("Picked up Ada");
  });
});

describe("Company page — Move… menu", () => {
  it("refuses a move that would loop, in the menu, without calling the API", () => {
    renderPage("outline");
    const { item } = openMoveMenu("Ada");
    const bisby = item(/Bisby/);
    expect(bisby.getAttribute("aria-disabled")).toBe("true");
    expect(bisby.textContent).toContain("loop");
    fireEvent.click(bisby);
    expect(vi.mocked(api)).not.toHaveBeenCalled();
  });

  it("moves an agent onto a team optimistically and PATCHes the right fields", async () => {
    let settle: (() => void) | undefined;
    vi.mocked(api).mockImplementation(
      () => new Promise((resolve) => (settle = () => resolve(undefined))),
    );
    renderPage("outline");
    expect(within(screen.getByRole("list", { name: "Independent agents" })).getByText("Quill"))
      .toBeTruthy();

    const { item } = openMoveMenu("Quill");
    fireEvent.click(item(/Engineering — top level/));

    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/quill", {
        method: "PATCH",
        body: { team_id: "eng", manager_agent_id: null },
      }),
    );
    // The chart moved before the request came back.
    await waitFor(() =>
      expect(
        within(screen.getByRole("list", { name: "Engineering members" })).getByText("Quill"),
      ).toBeTruthy(),
    );
    expect(screen.queryByRole("list", { name: "Independent agents" })).toBeNull();
    expect(screen.getByRole("status").textContent).toContain(
      "Quill moved to the Engineering team, reporting to no one.",
    );

    settle?.();
    await waitFor(() => expect(invalidate).toHaveBeenCalled());
  });

  it("nests one agent under another and says who they now report to", async () => {
    // Left in flight on purpose: the optimistic tree is what we are checking.
    vi.mocked(api).mockImplementation(() => new Promise(() => {}));
    renderPage("outline");
    const { item } = openMoveMenu("Quill");
    fireEvent.click(item(/Bisby/));

    await waitFor(() =>
      expect(vi.mocked(api)).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/quill", {
        method: "PATCH",
        // Following a manager into their team is what makes the line render.
        body: { team_id: "eng", manager_agent_id: "bisby" },
      }),
    );
    await waitFor(() =>
      expect(screen.getByRole("list", { name: "Reports to Bisby" })).toBeTruthy(),
    );
  });

  it("puts the tree back and explains itself when the server refuses", async () => {
    vi.mocked(api).mockRejectedValue(
      new ApiError(409, "This manager would create a cycle in the reporting chain"),
    );
    renderPage("outline");
    const { item } = openMoveMenu("Quill");
    fireEvent.click(item(/Engineering — top level/));

    await waitFor(() =>
      expect(
        screen.getByText(/This manager would create a cycle in the reporting chain/),
      ).toBeTruthy(),
    );
    expect(screen.getByText(/put back the way it was/)).toBeTruthy();
    // Quill is Independent again.
    expect(
      within(screen.getByRole("list", { name: "Independent agents" })).getByText("Quill"),
    ).toBeTruthy();
  });

  it("does nothing when the chosen spot is where the agent already is", () => {
    renderPage("outline");
    const { item } = openMoveMenu("Bisby");
    const current = item(/Ada/);
    expect(current.getAttribute("aria-current")).toBe("true");
    fireEvent.click(current);
    expect(vi.mocked(api)).not.toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toContain("already there");
  });

  it("moves focus into the menu and closes it on Escape", () => {
    renderPage("outline");
    const trigger = screen.getByRole("button", { name: "Move Quill" });
    fireEvent.click(trigger);
    const menu = screen.getByRole("menu", { name: "Move Quill" });
    expect(menu.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(menu.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});

describe("Company page — concurrent moves", () => {
  it("keeps two in-flight moves independent", async () => {
    const settles: (() => void)[] = [];
    vi.mocked(api).mockImplementation(
      () => new Promise((resolve) => settles.push(() => resolve(undefined))),
    );
    renderPage("outline");

    fireEvent.click(openMoveMenu("Quill").item(/Marketing — top level/));
    fireEvent.click(openMoveMenu("Bisby").item(/Engineering — top level/));

    await waitFor(() =>
      expect(
        within(screen.getByRole("list", { name: "Marketing members" })).getByText("Quill"),
      ).toBeTruthy(),
    );
    // Bisby's un-nest is still applied even though Quill's move is in flight.
    const engineering = screen.getByRole("list", { name: "Engineering members" });
    expect(within(engineering).getByText("Bisby")).toBeTruthy();
    expect(screen.queryByRole("list", { name: "Reports to Ada" })).toBeNull();

    // Settling the first move must not disturb the second.
    await waitFor(() => expect(settles.length).toBe(2));
    settles[0]();
    await waitFor(() => expect(invalidate).toHaveBeenCalled());
    expect(
      within(screen.getByRole("list", { name: "Engineering members" })).getByText("Bisby"),
    ).toBeTruthy();
  });
});
