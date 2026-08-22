/** Component tests: activity cards and the feed's empty/loaded states with
 * mocked data hooks. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActivityCard } from "@/components/activity/activity-card";
import type { ActivityCard as ActivityCardData } from "@/lib/types";

const hooks = vi.hoisted(() => ({
  useActivity: vi.fn(),
  useAgents: vi.fn(),
}));

vi.mock("@/lib/hooks", () => ({
  useActivity: hooks.useActivity,
  useAgents: hooks.useAgents,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const card: ActivityCardData = {
  id: "msg:1",
  kind: "asked_agent",
  label: "Asked another agent",
  actor_type: "agent",
  actor_agent_id: "cto",
  actor_agent_name: "CTO",
  target_agent_id: "qa",
  target_agent_name: "QA Engineer",
  task_id: "task-1",
  task_title: "Ship the login page",
  root_task_id: "task-0",
  conversation_id: "conv-1",
  approval_id: null,
  summary: "Please test the new login flow on staging.",
  detail_json: { message_type: "delegation", child_task_id: "task-9" },
  created_at: "2026-08-21T11:55:00Z",
};

const NOW = Date.parse("2026-08-21T12:00:00Z");

describe("ActivityCard", () => {
  it("renders actor, label, target, summary, time, and links", () => {
    render(
      <ul>
        <ActivityCard card={card} now={NOW} />
      </ul>,
    );
    expect(screen.getByRole("link", { name: "CTO" }).getAttribute("href")).toBe("/agents/cto");
    expect(screen.getByText("Asked another agent")).toBeDefined();
    expect(screen.getByRole("link", { name: "QA Engineer" }).getAttribute("href")).toBe("/agents/qa");
    expect(screen.getByText("Please test the new login flow on staging.")).toBeDefined();
    expect(screen.getByText("5m ago")).toBeDefined();
    expect(screen.getByRole("link", { name: /Open chat/ }).getAttribute("href")).toBe("/chats/conv-1");
    expect(screen.getByRole("link", { name: /Open in Advanced/ }).getAttribute("href")).toBe("/tasks/task-1");
  });

  it("keeps raw detail JSON behind the Show details disclosure", () => {
    render(
      <ul>
        <ActivityCard card={card} now={NOW} />
      </ul>,
    );
    expect(screen.queryByText(/child_task_id/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText(/child_task_id/)).toBeDefined();
    expect(screen.getByRole("button", { name: "Hide details" })).toBeDefined();
  });

  it("omits chips and details when the card has none", () => {
    render(
      <ul>
        <ActivityCard
          card={{ ...card, conversation_id: null, task_id: null, detail_json: {}, target_agent_name: null }}
          now={NOW}
        />
      </ul>,
    );
    expect(screen.queryByRole("link", { name: /Open chat/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Open in Advanced/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Show details" })).toBeNull();
  });
});

describe("ActivityFeed", () => {
  it("shows the friendly empty copy when nothing has happened", async () => {
    hooks.useAgents.mockReturnValue({ data: [] });
    hooks.useActivity.mockReturnValue({
      isPending: false,
      isError: false,
      data: { items: [], next_before: null },
      refetch: vi.fn(),
    });
    const { ActivityFeed } = await import("@/components/activity/activity-feed");
    const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ActivityFeed workspaceId="ws" />
      </QueryClientProvider>,
    );
    expect(screen.getByText("When your agents talk to each other, it shows up here.")).toBeDefined();
    expect(hooks.useActivity).toHaveBeenCalledWith("ws", { agent_id: undefined, kinds: undefined, limit: 30 });
  });

  it("renders cards and a Load more button when a cursor is present", async () => {
    hooks.useAgents.mockReturnValue({ data: [{ id: "cto", name: "CTO" }] });
    hooks.useActivity.mockReturnValue({
      isPending: false,
      isError: false,
      data: { items: [card], next_before: "2026-08-21T11:00:00Z" },
      refetch: vi.fn(),
    });
    const { ActivityFeed } = await import("@/components/activity/activity-feed");
    const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ActivityFeed workspaceId="ws" />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Please test the new login flow on staging.")).toBeDefined();
    expect(screen.getByRole("button", { name: "Load more" })).toBeDefined();
    // Picking the Handoffs group sends the grouped kinds to the API.
    fireEvent.click(screen.getByRole("radio", { name: "Handoffs" }));
    expect(hooks.useActivity).toHaveBeenLastCalledWith("ws", {
      agent_id: undefined,
      kinds: "asked_agent,reported,escalated",
      limit: 30,
    });
  });
});
