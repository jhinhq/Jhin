/** The first-run introduction as a person meets it: it appears once for a
 * brand-new membership, never for someone who already dealt with it, always
 * lets you out, remembers that you left, and can be fetched back on demand. */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OnboardingState, OnboardingStatus } from "@/lib/types";

interface QueryLike<T> {
  data: T | undefined;
  isPending: boolean;
  isError: boolean;
  refetch: () => void;
}

const state = {
  onboarding: undefined as OnboardingState | undefined,
  profiles: [] as { id: string }[],
  agents: [] as { id: string; status: string }[],
  connections: [] as { id: string }[],
  conversations: [] as { id: string }[],
};

const saved: { status: OnboardingStatus; lastStep: string | null }[] = [];

function ready<T>(data: T): QueryLike<T> {
  return { data, isPending: false, isError: false, refetch: vi.fn() };
}

vi.mock("@/lib/hooks", () => ({
  useOnboarding: () => ready(state.onboarding),
  useSaveOnboarding: () => (status: OnboardingStatus, lastStep: string | null) => {
    saved.push({ status, lastStep });
    state.onboarding = { status, last_step: lastStep, updated_at: "now" };
  },
  useModelProfiles: () => ready(state.profiles),
  useAgents: () => ready(state.agents),
  useConnections: () => ready(state.connections),
  useConversations: () => ready({ items: state.conversations, total: state.conversations.length }),
}));

import { OnboardingProvider, useOnboardingTour } from "@/components/onboarding/tour";
import type { WorkspaceRole } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

function ReopenButton() {
  const tour = useOnboardingTour();
  return (
    <button type="button" onClick={tour.openTour}>
      Reopen
    </button>
  );
}

function renderApp(role: WorkspaceRole = "owner") {
  return render(
    <WorkspaceProvider
      user={{
        id: "u1",
        email: "ada@example.com",
        display_name: "Ada Lovelace",
        created_at: "2026-01-01T00:00:00Z",
      }}
      workspace={{
        workspace_id: "w1",
        workspace_name: "Acme",
        workspace_slug: "acme",
        role,
      }}
    >
      <OnboardingProvider>
        <ReopenButton />
      </OnboardingProvider>
    </WorkspaceProvider>,
  );
}

const tour = () => screen.queryByTestId("onboarding-tour");
const dialog = () => screen.getByRole("dialog");

beforeEach(() => {
  state.onboarding = { status: "pending", last_step: null, updated_at: null };
  state.profiles = [];
  state.agents = [];
  state.connections = [];
  state.conversations = [];
  saved.length = 0;
});

afterEach(cleanup);

describe("when it appears", () => {
  it("greets a brand-new workspace by itself", () => {
    renderApp();
    expect(tour()).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Welcome to Acme" })).toBeTruthy();
  });

  it("stays away once it has been skipped", () => {
    state.onboarding = { status: "dismissed", last_step: "model", updated_at: "then" };
    renderApp();
    expect(tour()).toBeNull();
  });

  it("stays away once it has been finished", () => {
    state.onboarding = { status: "completed", last_step: "explore", updated_at: "then" };
    renderApp();
    expect(tour()).toBeNull();
  });

  it("stays away after the person parked it to go and do a step", () => {
    state.onboarding = { status: "in_progress", last_step: "model", updated_at: "then" };
    renderApp();
    expect(tour()).toBeNull();
  });

  it("stays shut if the server never answers", () => {
    state.onboarding = undefined;
    renderApp();
    expect(tour()).toBeNull();
  });

  it("comes back on request, and reopening does not depend on having seen it", () => {
    state.onboarding = { status: "completed", last_step: null, updated_at: "then" };
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    expect(tour()).not.toBeNull();
  });
});

describe("nobody is trapped", () => {
  it("offers a way out on the very first screen, and remembers it", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    await waitFor(() => expect(tour()).toBeNull());
    expect(saved).toEqual([{ status: "dismissed", lastStep: "welcome" }]);
  });

  it("closes on Escape and records that too", async () => {
    renderApp();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(tour()).toBeNull());
    expect(saved[0].status).toBe("dismissed");
  });

  it("having been skipped, does not reappear on the next render", async () => {
    const view = renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    await waitFor(() => expect(tour()).toBeNull());
    view.rerender(<div />);
    expect(tour()).toBeNull();
  });
});

describe("walking through it", () => {
  it("moves forward and back with the keyboard, and finishes at the end", async () => {
    renderApp();
    const panel = dialog();
    // Every control is a real button, so tabbing and Enter both work.
    fireEvent.click(within(panel).getByRole("button", { name: "Next" }));
    expect(screen.getByRole("heading", { name: "Connect a model provider" })).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Back" }));
    expect(screen.getByRole("heading", { name: "Welcome to Acme" })).toBeTruthy();

    // Jump straight to the last step from the strip, then finish.
    fireEvent.click(screen.getByTestId("tour-step-explore"));
    fireEvent.click(within(panel).getByRole("button", { name: "Finish" }));
    await waitFor(() => expect(tour()).toBeNull());
    expect(saved).toEqual([{ status: "completed", lastStep: "explore" }]);
  });

  it("moves focus to the step it just opened", () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(document.activeElement?.textContent).toBe("Connect a model provider");
  });

  it("announces the position for a screen reader", () => {
    renderApp();
    expect(screen.getByText("Step 1 of 7: Welcome to Acme")).toBeTruthy();
  });

  it("leaves a finished tour finished when it is reopened for a link", async () => {
    state.onboarding = { status: "completed", last_step: null, updated_at: "then" };
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    fireEvent.click(screen.getByTestId("tour-step-model"));
    fireEvent.click(screen.getByRole("link", { name: /Set up a model/ }));
    await waitFor(() => expect(tour()).toBeNull());
    expect(saved).toEqual([{ status: "completed", lastStep: "model" }]);
  });

  it("parks the tour rather than skipping it when you leave to do a step", async () => {
    renderApp();
    fireEvent.click(screen.getByTestId("tour-step-model"));
    fireEvent.click(screen.getByRole("link", { name: /Set up a model/ }));
    await waitFor(() => expect(tour()).toBeNull());
    expect(saved).toEqual([{ status: "in_progress", lastStep: "model" }]);
  });
});

describe("it reads the workspace it is in", () => {
  it("shows a step already satisfied as done rather than asking again", () => {
    state.profiles = [{ id: "p1" }];
    renderApp();
    fireEvent.click(screen.getByTestId("tour-step-model"));
    expect(within(dialog()).getByText("Done")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Review your models/ })).toBeTruthy();
  });

  it("resumes a returning admin on the first thing still outstanding", () => {
    state.profiles = [{ id: "p1" }];
    state.onboarding = { status: "dismissed", last_step: "model", updated_at: "then" };
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    expect(screen.getByRole("heading", { name: "Create your first teammate" })).toBeTruthy();
  });

  it("refuses to send someone to create an agent with no model behind it", () => {
    renderApp();
    fireEvent.click(screen.getByTestId("tour-step-agent"));
    expect(screen.getByTestId("tour-blocked").textContent).toMatch(/model/i);
    const cta = screen.getByRole("button", { name: /Create a teammate/ });
    expect(cta.hasAttribute("disabled")).toBe(true);
  });
});

describe("an invited member gets a different tour", () => {
  it("skips the setup steps entirely", () => {
    renderApp("member");
    expect(screen.queryByTestId("tour-step-model")).toBeNull();
    expect(screen.queryByTestId("tour-step-apps")).toBeNull();
    expect(screen.getByTestId("tour-step-chat")).toBeTruthy();
    expect(screen.getByTestId("tour-step-teamwork")).toBeTruthy();
    expect(screen.getByText("Step 1 of 4: Welcome to Acme")).toBeTruthy();
  });

  it("points at an admin instead of asking a member to fix the workspace", () => {
    renderApp("member");
    fireEvent.click(screen.getByTestId("tour-step-chat"));
    expect(screen.getByTestId("tour-blocked").textContent).toMatch(/admin/i);
  });
});
