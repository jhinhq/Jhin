/** Component tests: the thread header — identity, inline rename, and the
 * controls that only a member may see. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatHeader } from "@/components/chat/chat-header";
import type { Conversation, ConversationAgentSummary } from "@/lib/types";

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
    active_activity: null,
    last_message_preview: null,
    last_message_sender_type: null,
    agent_name: "Scout",
    agent_role_title: "Analyst",
    task_count: 1,
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

/** The header takes ten props; only the ones under test vary. */
function renderHeader(props: Partial<React.ComponentProps<typeof ChatHeader>> = {}) {
  const handlers = {
    onToggleDetails: vi.fn(),
    onToggleDetailed: vi.fn(),
    onRename: vi.fn(),
    onTogglePin: vi.fn(),
    onToggleArchive: vi.fn(),
  };
  const view = render(
    <ChatHeader
      conversation={conversation()}
      agent={agent()}
      canEdit
      detailsOpen={false}
      detailed={false}
      {...handlers}
      {...props}
    />,
  );
  return { ...view, ...handlers };
}

describe("ChatHeader identity", () => {
  it("shows the title, the agent, and its role", () => {
    renderHeader();
    expect(screen.getByText("Weekly summary")).toBeTruthy();
    expect(screen.getByText("Scout · Analyst")).toBeTruthy();
  });

  it("falls back to the conversation's denormalized agent when the summary is missing", () => {
    // /chats/[id] renders before the agent summary resolves, and keeps
    // rendering after the agent leaves the workspace.
    renderHeader({ agent: null });
    expect(screen.getByText("Scout · Analyst")).toBeTruthy();
  });

  it("says the chat is archived and offers to restore it", () => {
    renderHeader({ conversation: conversation({ status: "archived" }) });
    expect(screen.getByText("· Archived")).toBeTruthy();
    expect(screen.getByLabelText("Restore chat")).toBeTruthy();
    expect(screen.queryByLabelText("Archive chat")).toBeNull();
  });

  it("carries the live status pill", () => {
    renderHeader({ conversation: conversation({ active_task_state: "running" }) });
    expect(screen.getByTestId("live-status").textContent).toContain("Working…");
  });
});

describe("ChatHeader rename", () => {
  const titleButton = () => screen.getByRole("button", { name: "Rename chat: Weekly summary" });
  const titleInput = () => screen.getByRole("textbox", { name: "Chat title" }) as HTMLInputElement;

  it("swaps the title for an input seeded with the current title", () => {
    renderHeader();
    fireEvent.click(titleButton());
    expect(titleInput().value).toBe("Weekly summary");
    expect(titleInput().maxLength).toBe(200);
  });

  it("saves on Enter", () => {
    const { onRename } = renderHeader();
    fireEvent.click(titleButton());
    fireEvent.change(titleInput(), { target: { value: "Q3 numbers" } });
    fireEvent.keyDown(titleInput(), { key: "Enter" });
    expect(onRename).toHaveBeenCalledWith("Q3 numbers");
    expect(screen.queryByRole("textbox", { name: "Chat title" })).toBeNull();
  });

  it("discards the edit on Escape", () => {
    const { onRename } = renderHeader();
    fireEvent.click(titleButton());
    fireEvent.change(titleInput(), { target: { value: "Typed then regretted" } });
    fireEvent.keyDown(titleInput(), { key: "Escape" });
    expect(onRename).not.toHaveBeenCalled();
    expect(titleButton()).toBeTruthy();
  });

  it("reopens the editor with the saved title, not the abandoned draft", () => {
    renderHeader();
    fireEvent.click(titleButton());
    fireEvent.change(titleInput(), { target: { value: "Typed then regretted" } });
    fireEvent.keyDown(titleInput(), { key: "Escape" });
    fireEvent.click(titleButton());
    expect(titleInput().value).toBe("Weekly summary");
  });

  it("saves on blur so clicking away does not lose the edit", () => {
    const { onRename } = renderHeader();
    fireEvent.click(titleButton());
    fireEvent.change(titleInput(), { target: { value: "Q3 numbers" } });
    fireEvent.blur(titleInput());
    expect(onRename).toHaveBeenCalledWith("Q3 numbers");
  });

  it("does not fire a rename for an unchanged or emptied title", () => {
    // A no-op PATCH would still bump the conversation and refetch the thread.
    const { onRename } = renderHeader();
    fireEvent.click(titleButton());
    fireEvent.keyDown(titleInput(), { key: "Enter" });
    expect(onRename).not.toHaveBeenCalled();

    fireEvent.click(titleButton());
    fireEvent.change(titleInput(), { target: { value: "   " } });
    fireEvent.keyDown(titleInput(), { key: "Enter" });
    expect(onRename).not.toHaveBeenCalled();
  });

  it("trims and clamps what it sends to the API", () => {
    const { onRename } = renderHeader();
    fireEvent.click(titleButton());
    fireEvent.change(titleInput(), { target: { value: `  ${"x".repeat(250)}  ` } });
    fireEvent.keyDown(titleInput(), { key: "Enter" });
    expect(onRename).toHaveBeenCalledWith("x".repeat(200));
  });
});

describe("ChatHeader permissions", () => {
  it("hides pin and archive from a viewer and locks the title", () => {
    renderHeader({ canEdit: false });
    expect(screen.queryByLabelText("Pin chat")).toBeNull();
    expect(screen.queryByLabelText("Archive chat")).toBeNull();
    const title = screen.getByRole("button", { name: "Weekly summary" }) as HTMLButtonElement;
    expect(title.disabled).toBe(true);
    fireEvent.click(title);
    expect(screen.queryByRole("textbox", { name: "Chat title" })).toBeNull();
  });

  it("keeps the read-only view controls for a viewer", () => {
    // Details and progress chips are display state, not workspace edits.
    renderHeader({ canEdit: false });
    expect(screen.getByLabelText("Show details")).toBeTruthy();
    expect(screen.getByLabelText("Show progress updates")).toBeTruthy();
  });

  it("gives a member pin and archive, wired to their handlers", () => {
    const { onTogglePin, onToggleArchive } = renderHeader();
    fireEvent.click(screen.getByLabelText("Pin chat"));
    fireEvent.click(screen.getByLabelText("Archive chat"));
    expect(onTogglePin).toHaveBeenCalledTimes(1);
    expect(onToggleArchive).toHaveBeenCalledTimes(1);
  });

  it("labels the pin control by what clicking it will do", () => {
    renderHeader({ conversation: conversation({ pinned: true }) });
    expect(screen.getByLabelText("Unpin chat")).toBeTruthy();
    expect(screen.queryByLabelText("Pin chat")).toBeNull();
  });

  it("disables the mutating controls while an update is in flight", () => {
    renderHeader({ busy: true });
    expect((screen.getByLabelText("Pin chat") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText("Archive chat") as HTMLButtonElement).disabled).toBe(true);
    // The view toggles are local state, so they stay usable.
    expect((screen.getByLabelText("Show details") as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("ChatHeader view toggles", () => {
  it("reports and flips the details panel", () => {
    const { onToggleDetails, rerender, onToggleDetailed, onRename, onTogglePin, onToggleArchive } =
      renderHeader();
    const closed = screen.getByLabelText("Show details");
    expect(closed.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(closed);
    expect(onToggleDetails).toHaveBeenCalledTimes(1);

    rerender(
      <ChatHeader
        conversation={conversation()}
        agent={agent()}
        canEdit
        detailsOpen
        detailed={false}
        onToggleDetails={onToggleDetails}
        onToggleDetailed={onToggleDetailed}
        onRename={onRename}
        onTogglePin={onTogglePin}
        onToggleArchive={onToggleArchive}
      />,
    );
    expect(screen.getByLabelText("Hide details").getAttribute("aria-expanded")).toBe("true");
  });

  it("reports the progress-chip toggle as a pressed state", () => {
    const { onToggleDetailed } = renderHeader({ detailed: true });
    const toggle = screen.getByLabelText("Hide progress updates");
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(toggle);
    expect(onToggleDetailed).toHaveBeenCalledTimes(1);
  });

  it("renders the quick-controls slot before the header's own buttons", () => {
    renderHeader({ quickControls: <button type="button">Model</button> });
    const quick = screen.getByRole("button", { name: "Model" });
    const details = screen.getByLabelText("Show details");
    expect(quick.parentElement).toBe(details.parentElement);
    expect(quick.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

/* The title has to survive a phone-width header. There is no layout in jsdom,
 * so what is asserted is the mechanism that produces the behaviour: the title
 * column may shrink below its content and clips, and the controls drop to
 * their own line instead of squeezing it. */
describe("ChatHeader narrow width", () => {
  it("lets the title column shrink and clip rather than be pushed out", () => {
    renderHeader({ conversation: conversation({ title: "A very long chat title ".repeat(10) }) });
    const label = screen.getByText(/A very long chat title/);
    const column = screen.getByRole("button", { name: /^Rename chat:/ }).parentElement!;

    expect(label.className).toContain("truncate");
    // Without `min-w-0` a flex item refuses to shrink past its content, and
    // the header grows instead of the title clipping.
    expect(column.className).toContain("min-w-0");
    expect(column.className).toContain("flex-1");
  });

  it("wraps the controls onto their own row below the small breakpoint", () => {
    renderHeader();
    const header = screen.getByRole("banner");
    const controls = screen.getByLabelText("Show details").parentElement!;
    expect(header.className).toContain("flex-wrap");
    expect(header.className).toContain("sm:flex-nowrap");
    expect(controls.className).toContain("w-full");
    expect(controls.className).toContain("sm:w-auto");
    expect(controls.className).toContain("shrink-0");
  });
});
