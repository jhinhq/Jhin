/** Component tests: one memory card's content, chips, and actions, plus the
 * panel's admin "Clean up duplicates" action. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryItemCard, MemoryPanel } from "@/components/agents/memory-panel";
import { api } from "@/lib/api";
import { useMemories } from "@/lib/hooks";
import type { Agent, MemoryRecord } from "@/lib/types";

vi.mock("@/lib/hooks", () => ({
  useMemories: vi.fn(),
  useInvalidateMemories: () => () => undefined,
}));

vi.mock("@/lib/api", () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {},
}));

afterEach(cleanup);

function memory(overrides: Partial<MemoryRecord> = {}): MemoryRecord {
  return {
    id: "m1",
    workspace_id: "ws",
    scope: "team",
    scope_id: "t1",
    kind: "procedure",
    subject: "deploy.day",
    content: "We deploy on Tuesdays, never on Fridays.",
    source_conversation_id: "conv-9",
    source_message_id: null,
    source_task_id: null,
    source_event_id: null,
    visibility: "team",
    sensitivity: "normal",
    confidence: 0.9,
    importance: 0.65,
    tags_json: ["deploys"],
    status: "active",
    valid_from: null,
    expires_at: null,
    pinned_at: null,
    forgotten_at: null,
    version: 2,
    supersedes_id: "m0",
    has_embedding: true,
    embedding_model: "x",
    created_by_type: "agent",
    created_by_id: "a1",
    policy_json: {},
    created_at: "2026-08-21T10:00:00Z",
    updated_at: "2026-08-21T10:00:00Z",
    ...overrides,
  };
}

const now = Date.parse("2026-08-21T12:00:00Z");

describe("MemoryItemCard", () => {
  it("shows the content, scope and kind chips, plain words, and the chat link", () => {
    render(
      <ul>
        <MemoryItemCard memory={memory()} canWrite isAdmin={false} onAction={vi.fn()} now={now} />
      </ul>,
    );
    expect(screen.getByText("We deploy on Tuesdays, never on Fridays.")).toBeDefined();
    expect(screen.getByText("Team")).toBeDefined();
    expect(screen.getByText("How-to")).toBeDefined();
    expect(screen.getByText("Very sure")).toBeDefined();
    expect(screen.getByText("Important")).toBeDefined();
    expect(screen.getByText("#deploys")).toBeDefined();
    expect(screen.getByRole("link", { name: /From a chat/ }).getAttribute("href")).toBe("/chats/conv-9");
    expect(screen.getByText(/version 2/)).toBeDefined();
    // No raw ids anywhere.
    expect(document.body.textContent).not.toContain("m0");
    expect(document.body.textContent).not.toContain("deploy.day");
  });

  it("pins, edits as a new version, and forgets after confirming", () => {
    const onAction = vi.fn();
    const item = memory();
    render(
      <ul>
        <MemoryItemCard memory={item} canWrite isAdmin={false} onAction={onAction} now={now} />
      </ul>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Pin" }));
    expect(onAction).toHaveBeenCalledWith(item, { type: "pin", pinned: true });

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Edit memory") as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "We deploy on Wednesdays." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onAction).toHaveBeenCalledWith(item, { type: "edit", content: "We deploy on Wednesdays." });

    fireEvent.click(screen.getByRole("button", { name: "Forget" }));
    expect(screen.getByRole("dialog").textContent).toMatch(/permanent/i);
    fireEvent.click(screen.getByRole("button", { name: "Forget permanently" }));
    expect(onAction).toHaveBeenCalledWith(item, { type: "forget" });
  });

  it("lets admins approve or reject proposed memories and hides edit controls", () => {
    const onAction = vi.fn();
    const item = memory({ status: "proposed", scope: "workspace" });
    render(
      <ul>
        <MemoryItemCard memory={item} canWrite isAdmin onAction={onAction} now={now} />
      </ul>,
    );
    expect(screen.getByText("Waiting for approval")).toBeDefined();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAction).toHaveBeenCalledWith(item, { type: "approve" });
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onAction).toHaveBeenCalledWith(item, { type: "reject" });
  });

  it("shows no controls to viewers", () => {
    render(
      <ul>
        <MemoryItemCard memory={memory()} canWrite={false} isAdmin={false} onAction={vi.fn()} now={now} />
      </ul>,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });
});

function renderPanel(props: { canWrite: boolean; isAdmin: boolean }) {
  vi.mocked(useMemories).mockReturnValue({
    data: { items: [], total: 0 },
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  } as never);
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}
    >
      <MemoryPanel
        workspaceId="ws-1"
        agent={{ id: "a1", name: "Ava", team_id: null } as unknown as Agent}
        canWrite={props.canWrite}
        isAdmin={props.isAdmin}
      />
    </QueryClientProvider>,
  );
}

describe("MemoryPanel duplicates cleanup", () => {
  it("lets an admin merge duplicates and shows the result", async () => {
    vi.mocked(api).mockResolvedValue({ clusters: 1, superseded: 2, remaining_active: 3, adjudicated: 0, llm: false });
    renderPanel({ canWrite: true, isAdmin: true });
    fireEvent.click(screen.getByRole("button", { name: /Clean up duplicates/ }));
    const status = await screen.findByRole("status");
    expect(status.textContent).toBe("Merged 2 duplicates into 1 memory.");
    expect(api).toHaveBeenCalledWith("/api/v1/workspaces/ws-1/memories/deduplicate", { method: "POST" });
  });

  it("mentions smart matching when the model compared wordings", async () => {
    vi.mocked(api).mockResolvedValue({ clusters: 1, superseded: 1, remaining_active: 3, adjudicated: 2, llm: true });
    renderPanel({ canWrite: true, isAdmin: true });
    fireEvent.click(screen.getByRole("button", { name: /Clean up duplicates/ }));
    expect((await screen.findByRole("status")).textContent).toBe(
      "Merged 1 duplicate into 1 memory. Smart matching was used to compare wordings.",
    );
  });

  it("reports when nothing needed merging", async () => {
    vi.mocked(api).mockResolvedValue({ clusters: 0, superseded: 0, remaining_active: 5, adjudicated: 0, llm: false });
    renderPanel({ canWrite: true, isAdmin: true });
    fireEvent.click(screen.getByRole("button", { name: /Clean up duplicates/ }));
    expect((await screen.findByRole("status")).textContent).toBe("No duplicates found.");
  });

  it("hides the cleanup button from non-admins", () => {
    renderPanel({ canWrite: true, isAdmin: false });
    expect(screen.queryByRole("button", { name: /Clean up duplicates/ })).toBeNull();
  });
});
