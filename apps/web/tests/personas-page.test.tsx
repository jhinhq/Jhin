/** Personas library page: gallery cards with their badges, the filters, the
 * admin actions (install the cast, switch, duplicate, delete), the detail
 * dialog, and the deep link from a persona chip. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PersonasPage from "@/app/(app)/personas/page";
import { api } from "@/lib/api";
import { usePersonas } from "@/lib/hooks";
import type { Persona } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";
import { persona } from "./helpers/personas";

const navigation = vi.hoisted(() => ({ params: "" }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(navigation.params),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/lib/hooks", () => ({
  usePersonas: vi.fn(),
  useInvalidatePersonas: () => () => undefined,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.params = "";
});

const missionControl = persona();
const straightShooter = persona({
  id: "p2",
  name: "the-straight-shooter",
  display_name: "The Straight Shooter",
  description: "Answer first, reasons second, no hedging.",
  tags: ["professional", "direct"],
  source: "custom",
  read_only: false,
  agent_count: 2,
  facets: { ...persona().facets, voice: "Plain, confident, unhurried.", never: [] },
});
const nightOwl = persona({
  id: "p3",
  name: "night-owl",
  display_name: "Night Owl",
  description: "Quiet, late, and precise.",
  tags: [],
  source: "agent",
  read_only: false,
  enabled: false,
  agent_count: 0,
  created_by_agent_id: "a9",
  facets: { ...persona().facets, voice: "Hushed and exact.", never: [] },
});
const CAST = [missionControl, straightShooter, nightOwl];

function renderPage(items: Persona[] = CAST, role: "owner" | "member" = "owner") {
  vi.mocked(usePersonas).mockReturnValue({
    data: { items, total: items.length },
    isPending: false,
    isError: false,
  } as ReturnType<typeof usePersonas>);
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkspaceProvider
        user={{
          id: "u1",
          email: "ada@example.com",
          display_name: "Ada",
          created_at: "2026-01-01T00:00:00Z",
        }}
        workspace={{
          workspace_id: "w1",
          workspace_name: "Acme",
          workspace_slug: "acme",
          role,
        }}
      >
        <PersonasPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

const card = (name: string) => screen.getByTestId(`persona-${name}`);

describe("PersonasPage gallery", () => {
  it("renders every card with its source, tags, wearers, and voice", () => {
    renderPage();
    // The Source filter lists the same labels, so look inside the cards.
    expect(within(card("mission-control")).getByText("By Jhin")).toBeTruthy();
    expect(within(card("the-straight-shooter")).getByText("Yours")).toBeTruthy();
    expect(within(card("night-owl")).getByText("Agent-made")).toBeTruthy();
    expect(within(card("mission-control")).getByText("fun")).toBeTruthy();
    expect(within(card("the-straight-shooter")).queryByText("fun")).toBeNull();
    expect(within(card("mission-control")).getByText("Worn by 2 agents")).toBeTruthy();
    expect(within(card("night-owl")).getByText("Nobody wears this yet")).toBeTruthy();
    expect(within(card("mission-control")).getByText(/Level, measured, unflappable/)).toBeTruthy();
    expect(within(card("night-owl")).getByText("Switched off")).toBeTruthy();
  });

  it("narrows by search, the fun toggle, and the source", () => {
    renderPage();
    fireEvent.change(screen.getByLabelText("Search personas"), { target: { value: "reasons" } });
    expect(screen.queryByTestId("persona-mission-control")).toBeNull();
    expect(card("the-straight-shooter")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search personas"), { target: { value: "" } });

    fireEvent.click(screen.getByRole("button", { name: "Fun" }));
    expect(card("mission-control")).toBeTruthy();
    expect(screen.queryByTestId("persona-the-straight-shooter")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Fun" }));

    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "agent" } });
    expect(card("night-owl")).toBeTruthy();
    expect(screen.queryByTestId("persona-mission-control")).toBeNull();
  });

  it("shows switched-off cards until the box is unticked", () => {
    renderPage();
    expect(card("night-owl")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Show switched-off personas"));
    expect(screen.queryByTestId("persona-night-owl")).toBeNull();
    expect(card("mission-control")).toBeTruthy();
  });

  it("offers to clear the filters when nothing matches", () => {
    renderPage();
    fireEvent.change(screen.getByLabelText("Search personas"), { target: { value: "zzz" } });
    expect(screen.getByText("Nothing matches")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(card("mission-control")).toBeTruthy();
  });

  it("invites an admin to install the cast when the library is empty", () => {
    renderPage([]);
    expect(screen.getByText("No personas yet")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Install the cast/ })).toBeTruthy();
  });
});

describe("PersonasPage admin actions", () => {
  it("installs the missing defaults and says what changed", async () => {
    vi.mocked(api).mockResolvedValue({ installed: 2, refreshed: 1, skipped: 9, names: [] });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /Install missing defaults/ }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/personas/install-builtins", {
        method: "POST",
      }),
    );
    await screen.findByText("Added 2 and refreshed 1.");
  });

  it("switches a persona off", async () => {
    vi.mocked(api).mockResolvedValue(persona({ enabled: false }));
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Disable Mission Control" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/personas/p1/disable", {
        method: "POST",
      }),
    );
  });

  it("confirms a delete by naming who wears the card, then deletes", async () => {
    vi.mocked(api).mockResolvedValue(undefined);
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Delete The Straight Shooter" }));
    const dialog = screen.getByTestId("confirm-dialog");
    expect(dialog.textContent).toContain("“The Straight Shooter” will be removed from the library.");
    expect(dialog.textContent).toContain("The 2 agents wearing it carry on without a persona");
    fireEvent.click(screen.getByRole("button", { name: "Delete persona" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/personas/p2", { method: "DELETE" }),
    );
  });

  it("duplicates a built-in and opens the copy for editing", async () => {
    vi.mocked(api).mockResolvedValue(
      persona({
        id: "p9",
        name: "mission-control-copy",
        display_name: "Mission Control (copy)",
        source: "custom",
        read_only: false,
        agent_count: 0,
      }),
    );
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Duplicate Mission Control" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/personas/p1/duplicate", {
        method: "POST",
        body: {},
      }),
    );
    await screen.findByRole("dialog", { name: "Edit persona" });
    const name = screen.getByLabelText("Name") as HTMLInputElement;
    expect(name.disabled).toBe(true);
    expect(name.value).toBe("mission-control-copy");
  });

  it("never offers Edit or Delete on a built-in", () => {
    renderPage();
    expect(screen.queryByRole("button", { name: "Edit Mission Control" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete Mission Control" })).toBeNull();
    expect(screen.getByRole("button", { name: "Duplicate Mission Control" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit The Straight Shooter" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Delete The Straight Shooter" })).toBeTruthy();
    expect(
      within(card("mission-control")).getByText(/Built-in cards can’t be edited/),
    ).toBeTruthy();
  });

  it("hides every admin action from a member", () => {
    renderPage(CAST, "member");
    expect(screen.queryByRole("button", { name: /Install missing defaults/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /New persona/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Disable Mission Control" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Duplicate Mission Control" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Mission Control" }));
    const dialog = screen.getByRole("dialog", { name: "Mission Control" });
    // The header's X and the footer's Close are both named Close; nothing else is offered.
    const buttons = within(dialog).getAllByRole("button").map((button) => button.textContent);
    expect(buttons.every((label) => label === "Close" || label === "")).toBe(true);
    expect(within(dialog).queryByRole("button", { name: /Duplicate/ })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /Disable/ })).toBeNull();
  });
});

describe("PersonasPage detail", () => {
  it("opens the whole card from its name, with the block the agent reads", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Mission Control" }));
    const dialog = screen.getByRole("dialog", { name: "Mission Control" });
    expect(within(dialog).getByText("When unsure")).toBeTruthy();
    const block = within(dialog).getByTestId("persona-block-preview").textContent ?? "";
    expect(block).toContain("How you work — Mission Control");
    expect(block).toContain("- With people:");
    expect(block).toContain("- With teammates:");
    expect(within(dialog).getByText(/Only one of “With people” and “With teammates”/)).toBeTruthy();
    expect(within(dialog).getByText(/Version 1 · Updated/)).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "Duplicate to make it yours" })).toBeTruthy();
  });

  it("opens the card a chip linked to", () => {
    navigation.params = "persona=p3";
    renderPage();
    const dialog = screen.getByRole("dialog", { name: "Night Owl" });
    expect(within(dialog).getByText("Written by an agent and approved by a person.", { exact: false })).toBeTruthy();
    expect(within(dialog).getByText("Nothing listed")).toBeTruthy();
  });
});
