/** Component tests: rail items, transcript rendering, and the composer. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConversationRailItem } from "@/components/chat/chat-rail";
import { Composer } from "@/components/chat/composer";
import { Transcript } from "@/components/chat/transcript";
import { mergeTimeline } from "@/lib/chat";
import type { ActivityCard, Conversation, ConversationMessage } from "@/lib/types";

vi.mock("@/lib/hooks", () => ({
  useConversations: () => ({ data: { items: [], total: 0 }, isPending: false, error: null }),
}));

vi.mock("@/lib/workspace-context", () => ({
  useWorkspace: () => ({
    workspace: { workspace_id: "ws", workspace_name: "Acme", workspace_slug: "acme", role: "member" },
    user: { id: "u1", email: "a@b.c", display_name: "Ada", created_at: "" },
    role: "member",
    can: () => true,
  }),
}));

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
    last_activity_at: new Date().toISOString(),
    created_at: "",
    updated_at: "",
    active_task_id: null,
    active_task_state: null,
    active_run_status: null,
    last_message_preview: "Here is what happened",
    last_message_sender_type: "agent",
    agent_name: "Scout",
    agent_role_title: "Analyst",
    task_count: 1,
    ...overrides,
  };
}

function message(overrides: Partial<ConversationMessage>): ConversationMessage {
  return {
    id: "m1",
    task_id: "t1",
    run_id: null,
    sender_type: "agent",
    sender_id: "a1",
    message_type: "text",
    content_json: { text: "hi" },
    created_at: "2026-08-21T10:00:00Z",
    conversation_id: "c1",
    sender_name: "Scout",
    agent_id: "a1",
    ...overrides,
  };
}

describe("ConversationRailItem", () => {
  it("shows the agent, title, preview, and no pill when idle", () => {
    render(
      <ul>
        <ConversationRailItem conversation={conversation()} selected={false} />
      </ul>,
    );
    expect(screen.getByText("Weekly summary")).toBeTruthy();
    expect(screen.getByText("Scout · Analyst")).toBeTruthy();
    expect(screen.getByText("Here is what happened")).toBeTruthy();
    expect(screen.queryByTestId("live-status")).toBeNull();
  });

  it.each([
    [{ active_task_state: "running" as const, active_run_status: null }, "Working…"],
    [{ active_task_state: "queued" as const, active_run_status: null }, "Waiting for a free slot"],
    [{ active_task_state: "paused" as const, active_run_status: null }, "Paused"],
    [
      { active_task_state: "running" as const, active_run_status: "waiting_approval" },
      "Needs your review",
    ],
  ])("renders the live pill for %o", (state, label) => {
    render(
      <ul>
        <ConversationRailItem conversation={conversation(state)} selected />
      </ul>,
    );
    expect(screen.getByTestId("live-status").textContent).toContain(label);
    expect(screen.getByRole("link").getAttribute("aria-current")).toBe("page");
  });
});

describe("Transcript", () => {
  it("renders user bubbles, agent bubbles, work cards, and activity chips", () => {
    const messages = [
      message({ id: "m1", sender_type: "user", content_json: { text: "Summarize the week" } }),
      message({ id: "m2", content_json: { text: "On it." }, created_at: "2026-08-21T10:01:00Z" }),
      message({
        id: "m3",
        message_type: "delegation",
        content_json: { summary: "Handing the data pull to QA.", target_agent_name: "QA" },
        created_at: "2026-08-21T10:02:00Z",
      }),
    ];
    const activity: ActivityCard[] = [
      {
        id: "task:t1:started",
        kind: "started",
        label: "Started working",
        actor_type: "agent",
        actor_agent_id: "a1",
        actor_agent_name: "Scout",
        target_agent_id: null,
        target_agent_name: null,
        task_id: "t1",
        task_title: "Summarize the week",
        root_task_id: "t1",
        conversation_id: "c1",
        approval_id: null,
        summary: "",
        detail_json: {},
        created_at: "2026-08-21T10:00:30Z",
      },
    ];
    render(
      <Transcript items={mergeTimeline(messages, activity)} agentName="Scout" userName="Ada" />,
    );
    expect(screen.getByRole("log")).toBeTruthy();
    expect(screen.getByTestId("user-message").textContent).toContain("Summarize the week");
    expect(screen.getByTestId("agent-message").textContent).toContain("On it.");
    expect(screen.getByTestId("activity-chip").textContent).toContain("Started working");

    const workCard = screen.getByTestId("work-card");
    expect(workCard.textContent).toContain("Asked QA for help");
    expect(workCard.textContent).toContain("Handing the data pull to QA.");
    expect(screen.queryByTestId("structured-message")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByTestId("structured-message")).toBeTruthy();
    expect(screen.getByText("delegation")).toBeTruthy();
  });

  it("shows the working indicator", () => {
    render(
      <Transcript
        items={[]}
        agentName="Scout"
        userName="Ada"
        liveStatus={{ label: "Working…", tone: "accent", kind: "working" }}
      />,
    );
    expect(screen.getByTestId("working-indicator").textContent).toContain("Scout is working…");
  });
});

describe("Composer", () => {
  it("sends on Enter and inserts a newline on Shift+Enter", () => {
    const onSend = vi.fn();
    const onChange = vi.fn();
    render(<Composer value="hello" onChange={onChange} onSend={onSend} />);
    const box = screen.getByRole("textbox", { name: "Message" });

    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("does not send when empty or disabled and explains why", () => {
    const onSend = vi.fn();
    const { rerender } = render(<Composer value="   " onChange={() => {}} onSend={onSend} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Send message" }) as HTMLButtonElement).disabled).toBe(true);

    rerender(
      <Composer
        value="hello"
        onChange={() => {}}
        onSend={onSend}
        disabled
        disabledReason="This chat is archived."
      />,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getAllByText("This chat is archived.").length).toBeGreaterThan(0);
  });
});
