/** Component tests: the "Details" panel — what the agent is doing right now
 * and the run controls that go with it. */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContextPanel } from "@/components/chat/context-panel";
import type {
  ActivityCard,
  Conversation,
  ConversationAgentSummary,
  ConversationDetail,
  Task,
} from "@/lib/types";

afterEach(cleanup);

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "c1",
    workspace_id: "ws",
    title: "Weekly summary",
    status: "active",
    pinned: false,
    primary_agent_id: "a1",
    created_by_user_id: "u1",
    last_activity_at: "2026-08-21T10:00:00Z",
    created_at: "",
    updated_at: "",
    active_task_id: null,
    active_task_state: null,
    active_run_status: null,
    last_message_preview: null,
    last_message_sender_type: null,
    agent_name: "Scout",
    agent_role_title: "Analyst",
    task_count: 1,
    ...overrides,
  };
}

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: "t1",
    title: "Summarize the week",
    description: "",
    state: "running",
    priority: "normal",
    assigned_agent_id: "a1",
    temporal_workflow_id: null,
    external_source: null,
    external_id: null,
    trigger_id: null,
    parent_task_id: null,
    metadata_json: {},
    created_at: "2026-08-21T10:00:00Z",
    updated_at: "2026-08-21T10:00:00Z",
    ...overrides,
  };
}

function agent(overrides: Partial<ConversationAgentSummary> = {}): ConversationAgentSummary {
  return {
    id: "a1",
    name: "Scout",
    role_title: "Analyst",
    status: "active",
    availability: "available",
    public_purpose: "Watches the numbers.",
    ...overrides,
  };
}

function detail(overrides: Partial<ConversationDetail> = {}): ConversationDetail {
  return {
    conversation: conversation(),
    agent: agent(),
    tasks: [],
    total_input_tokens: 0,
    total_output_tokens: 0,
    total_cost_micros: 0,
    pending_approvals: [],
    ...overrides,
  };
}

/** A conversation with one task on it, in the state under test. Both halves
 * have to agree: the header line comes from the conversation's cached state
 * and the controls from the task's own. */
function working(state: Task["state"]): ConversationDetail {
  return detail({
    conversation: conversation({ active_task_id: "t1", active_task_state: state }),
    tasks: [task({ state })],
  });
}

function renderPanel(props: Partial<React.ComponentProps<typeof ContextPanel>> = {}) {
  const handlers = { onPause: vi.fn(), onResume: vi.fn(), onCancel: vi.fn() };
  const view = render(
    <ContextPanel detail={detail()} activity={[]} canAct {...handlers} {...props} />,
  );
  return { ...view, ...handlers };
}

describe("ContextPanel agent card", () => {
  it("names the agent, its role, and its purpose, and links to the profile", () => {
    renderPanel();
    expect(screen.getByText("Scout")).toBeTruthy();
    expect(screen.getByText("Analyst")).toBeTruthy();
    expect(screen.getByText("Watches the numbers.")).toBeTruthy();
    expect(screen.getByRole("link", { name: /View profile/ }).getAttribute("href")).toBe(
      "/agents/a1",
    );
  });

  it.each([
    [{ status: "active" as const, availability: "available" as const }, "Available"],
    [{ status: "active" as const, availability: "unavailable" as const }, "Busy right now"],
    [{ status: "paused" as const }, "Paused by an admin"],
    [{ status: "disabled" as const }, "Turned off"],
  ])("explains %o in words, not a status code", (state, label) => {
    // An unavailable agent is the reason a chat goes quiet, so the panel has
    // to say which kind of unavailable it is.
    renderPanel({ detail: detail({ agent: agent(state) }) });
    expect(screen.getByText(label)).toBeTruthy();
  });

  it("says so when the agent has left the workspace", () => {
    renderPanel({ detail: detail({ agent: null }) });
    expect(screen.getByText("This agent is no longer in the workspace.")).toBeTruthy();
  });
});

describe("ContextPanel current work", () => {
  it("shows the live state and, beside it, the task it belongs to", () => {
    renderPanel({ detail: working("running") });
    // Scoped to the card: the same title also appears in the episode list
    // below, so an unscoped query would pass even with nothing named here.
    const card = screen.getByText("Working…").parentElement!;
    expect(within(card).getByText("Summarize the week")).toBeTruthy();
  });

  it("says nothing is in progress when there is no active task", () => {
    renderPanel({ detail: detail({ tasks: [task({ state: "completed" })] }) });
    expect(screen.getByText("Nothing in progress. Send a message to start something new.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pause work" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop work" })).toBeNull();
  });

  it("offers Resume, and only Resume, while the task is paused", () => {
    const { onResume, onPause } = renderPanel({ detail: working("paused") });
    expect(screen.queryByRole("button", { name: "Pause work" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Resume work" }));
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(onPause).not.toHaveBeenCalled();
  });

  it.each([["running"], ["queued"]] as const)("offers Pause while the task is %s", (state) => {
    const { onPause, onResume } = renderPanel({ detail: working(state) });
    expect(screen.queryByRole("button", { name: "Resume work" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Pause work" }));
    expect(onPause).toHaveBeenCalledTimes(1);
    expect(onResume).not.toHaveBeenCalled();
  });

  it("keys the Resume control off the task, not the conversation's cached state", () => {
    // `active_task_state` is denormalized onto the conversation row and lags
    // a pause; the task is the record that says whether resuming is the move.
    renderPanel({
      detail: detail({
        conversation: conversation({ active_task_id: "t1", active_task_state: "running" }),
        tasks: [task({ state: "paused" })],
      }),
    });
    expect(screen.getByRole("button", { name: "Resume work" })).toBeTruthy();
  });

  it("always offers Stop alongside, and calls back once", () => {
    const { onCancel } = renderPanel({ detail: working("running") });
    fireEvent.click(screen.getByRole("button", { name: "Stop work" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("hides every run control from a viewer but keeps the status readable", () => {
    renderPanel({ detail: working("paused"), canAct: false });
    // Scoped: "Paused" also labels the episode in the list below.
    const section = screen.getByText("Current work").parentElement!;
    expect(within(section).getByText("Paused")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Resume work" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop work" })).toBeNull();
  });

  it("disables the controls while a pause/resume/stop is in flight", () => {
    renderPanel({ detail: working("running"), acting: true });
    for (const label of ["Pause work", "Stop work"]) {
      expect((screen.getByRole("button", { name: label }) as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it("ignores tasks that are not the active one", () => {
    // The panel lists every episode, but "Current work" is about one task.
    renderPanel({
      detail: detail({
        conversation: conversation({ active_task_id: "t1", active_task_state: "running" }),
        tasks: [task({ id: "t9", title: "An older episode", state: "completed" })],
      }),
    });
    expect(screen.getByText("Nothing in progress. Send a message to start something new.")).toBeTruthy();
  });
});

describe("ContextPanel episodes, usage, and activity", () => {
  it("lists each work episode in plain language with a link to the advanced view", () => {
    renderPanel({
      detail: detail({
        tasks: [
          task({ id: "t1", title: "Summarize the week", state: "completed" }),
          task({ id: "t2", title: "Pull the numbers", state: "failed" }),
        ],
      }),
    });
    expect(screen.getByText("Finished")).toBeTruthy();
    expect(screen.getByText("Ran into a problem")).toBeTruthy();
    const links = screen.getAllByRole("link", { name: /Open in Advanced/ });
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/tasks/t1", "/tasks/t2"]);
  });

  it("shows the empty states before anything has run", () => {
    renderPanel();
    expect(screen.getByText("No work yet.")).toBeTruthy();
    expect(screen.getByText("No activity yet.")).toBeTruthy();
  });

  it("reports spend and tokens for the whole thread", () => {
    renderPanel({
      detail: detail({
        total_cost_micros: 1_240_000,
        total_input_tokens: 12_400,
        total_output_tokens: 800,
      }),
    });
    expect(screen.getByText("$1.24")).toBeTruthy();
    expect(screen.getByText("12.4k")).toBeTruthy();
    expect(screen.getByText("800")).toBeTruthy();
  });

  it("attributes each activity row to its actor and target", () => {
    const card: ActivityCard = {
      id: "task:t1:asked",
      kind: "asked_agent",
      label: "Asked for help",
      actor_type: "agent",
      actor_agent_id: "a1",
      actor_agent_name: "Scout",
      target_agent_id: "a2",
      target_agent_name: "Linus",
      task_id: "t1",
      task_title: "Summarize the week",
      root_task_id: "t1",
      conversation_id: "c1",
      approval_id: null,
      summary: "Needs the raw numbers.",
      detail_json: {},
      created_at: "2026-08-21T10:00:00Z",
    };
    renderPanel({ activity: [card] });
    expect(screen.getByText("Scout · Asked for help (Linus)")).toBeTruthy();
    expect(screen.getByText("Needs the raw numbers.")).toBeTruthy();
  });
});
