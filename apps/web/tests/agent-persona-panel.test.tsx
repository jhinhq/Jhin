/** Agent profile Persona tab: what the agent wears, the picker, clearing,
 * the next-run note, and the block it reads. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PersonaPanel } from "@/components/agents/persona-panel";
import { api } from "@/lib/api";
import { usePersonas } from "@/lib/hooks";
import type { Agent, Persona } from "@/lib/types";
import { persona, personaSummary } from "./helpers/personas";

vi.mock("@/lib/hooks", () => ({
  usePersonas: vi.fn(),
  useInvalidatePersonas: () => () => undefined,
  useInvalidateOrg: () => () => undefined,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function agent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "a1",
    workspace_id: "w1",
    team_id: null,
    manager_agent_id: null,
    name: "Scout",
    slug: "scout",
    role_title: "Analyst",
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
    persona_id: null,
    persona: null,
    created_at: "2026-09-01T09:00:00Z",
    updated_at: "2026-09-01T09:00:00Z",
    ...overrides,
  };
}

const library = (): Persona[] => [
  persona(),
  persona({
    id: "p2",
    name: "the-straight-shooter",
    display_name: "The Straight Shooter",
    description: "Answer first, reasons second.",
    tags: ["professional"],
    source: "custom",
    read_only: false,
    agent_count: 0,
  }),
];

function renderPanel(subject: Agent, items: Persona[] = library(), isAdmin = true) {
  vi.mocked(usePersonas).mockReturnValue({
    data: { items, total: items.length },
    isPending: false,
    isError: false,
  } as ReturnType<typeof usePersonas>);
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <PersonaPanel workspaceId="w1" agent={subject} isAdmin={isAdmin} />
    </QueryClientProvider>,
  );
}

describe("PersonaPanel", () => {
  it("says when the agent wears no persona", () => {
    renderPanel(agent());
    expect(screen.getByTestId("current-persona").textContent).toContain(
      "No persona. Scout speaks in its own default way.",
    );
    expect(screen.getByText(/Without a persona, Scout gets no “How you work” block/)).toBeTruthy();
    expect(screen.queryByTestId("persona-block-preview")).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear persona" })).toBeNull();
  });

  it("shows the worn card with its tags, source, and the block the agent reads", () => {
    renderPanel(agent({ persona_id: "p1", persona: personaSummary() }));
    const current = screen.getByTestId("current-persona");
    expect(within(current).getByText("Mission Control")).toBeTruthy();
    expect(within(current).getByText("fun")).toBeTruthy();
    expect(within(current).getByText("By Jhin")).toBeTruthy();
    expect(within(current).getByText(/Calm flight-director cadence/)).toBeTruthy();
    expect(screen.getByTestId("persona-block-preview").textContent).toContain(
      "How you work — Mission Control",
    );
    expect(screen.getByRole("radio", { name: /Mission Control/ }).getAttribute("aria-checked")).toBe(
      "true",
    );
  });

  it("says when the worn persona is switched off and points at the library", () => {
    renderPanel(
      agent({ persona_id: "p1", persona: personaSummary({ enabled: false }) }),
      [persona({ enabled: false }), ...library().slice(1)],
    );
    const current = screen.getByTestId("current-persona");
    expect(within(current).getByText("Switched off")).toBeTruthy();
    expect(current.textContent).toContain(
      "Mission Control is switched off in the library, so Scout runs without a persona until it’s turned back on.",
    );
    expect(within(current).getByRole("link", { name: "Open the Personas page" }).getAttribute("href")).toBe(
      "/personas",
    );
    expect(screen.getByText("Not rendered while the persona is switched off.")).toBeTruthy();
    expect(screen.getByTestId("persona-block-preview")).toBeTruthy();
  });

  it("assigns a persona from the picker, with the next-run note", async () => {
    vi.mocked(api).mockResolvedValue(agent({ persona_id: "p2" }));
    renderPanel(agent());
    expect(screen.getByText("Takes effect on Scout’s next run, never in the middle of one.")).toBeTruthy();
    fireEvent.click(screen.getByTestId("persona-option-the-straight-shooter"));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1", {
        method: "PATCH",
        body: { persona_id: "p2" },
      }),
    );
  });

  it("clears the persona with an explicit null", async () => {
    vi.mocked(api).mockResolvedValue(agent());
    renderPanel(agent({ persona_id: "p1", persona: personaSummary() }));
    fireEvent.click(screen.getByRole("button", { name: "Clear persona" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/api/v1/workspaces/w1/agents/a1", {
        method: "PATCH",
        body: { persona_id: null },
      }),
    );
  });

  it("shows the API's refusal", async () => {
    const { ApiError } = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    vi.mocked(api).mockRejectedValue(
      new ApiError(422, "persona_id does not reference an enabled persona in this workspace"),
    );
    renderPanel(agent());
    fireEvent.click(screen.getByTestId("persona-option-the-straight-shooter"));
    expect(
      (await screen.findByText("persona_id does not reference an enabled persona in this workspace"))
        .textContent,
    ).toBeTruthy();
  });

  it("lets a non-admin look but not change", () => {
    renderPanel(agent({ persona_id: "p1", persona: personaSummary() }), library(), false);
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear persona" })).toBeNull();
    expect(screen.getByText("Only admins can change which persona an agent wears.")).toBeTruthy();
    expect(screen.getByTestId("persona-block-preview")).toBeTruthy();
  });
});
