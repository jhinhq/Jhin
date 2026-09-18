import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChatThreadPage from "@/app/(app)/chats/[id]/view";
import type { ConversationItem } from "@/lib/agentic-chat";
import type { ConversationDetail } from "@/lib/types";

vi.mock("@/lib/agentic-features", () => ({ AGENTIC_WORKSPACE_ENABLED: true }));
vi.mock("@/lib/use-route-segment", () => ({ useSegmentAfter: () => "chat" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/workspace-context", () => ({ useWorkspace: () => ({
  workspace: { workspace_id: "workspace" }, user: { id: "owner", display_name: "Owner" }, can: () => true,
}) }));
vi.mock("@/lib/hooks", async (original) => ({
  ...(await original<typeof import("@/lib/hooks")>()), useAgentAvatarMap: () => ({}),
}));
vi.mock("@/components/chat/chat-header", () => ({ ChatHeader: () => null }));
vi.mock("@/components/chat/context-panel", () => ({ ContextPanel: () => null }));
vi.mock("@/components/chat/composer-controls", () => ({ ChatComposerControls: () => null }));
vi.mock("@/components/chat/turn-controls", () => ({ TurnControls: () => null }));
vi.mock("@/components/chat/work-queue", () => ({ WorkQueue: () => null }));

const START = "2026-09-13T12:00:00Z";
const END = "2026-09-13T12:00:30Z";
const BASE = "/api/v1/workspaces/workspace/conversations/chat";
const clients: QueryClient[] = [];

function generation(id: string, text: string, status = "running", step = 0): ConversationItem {
  return {
    id: `generation:${id}`, version: 1, sequence: step + 1, revision: 1, kind: "generation", status,
    actor: { type: "agent", id: "mindy", name: "Mindy" }, task_id: "task", run_id: "run",
    created_at: START, data: { id, text, step },
  };
}

function finalMessage(id: string, text: string): ConversationItem {
  return {
    id: `message:${id}`, version: 1, sequence: 20, revision: 1, kind: "message", status: "completed",
    actor: { type: "agent", id: "mindy", name: "Mindy" }, task_id: "task", run_id: "run", created_at: END,
    data: { id, task_id: "task", run_id: "run", sender_type: "agent", sender_id: "mindy", message_type: "text", content_json: { text }, created_at: END },
  };
}

async function setup(items: ConversationItem[]) {
  let stream!: ReadableStreamDefaultController<Uint8Array>;
  let sequence = 100;
  const detail: ConversationDetail = {
    conversation: {
      id: "chat", workspace_id: "workspace", title: "Mindy chat", status: "active", pinned: false,
      primary_agent_id: "mindy", created_by_user_id: "owner", last_activity_at: START, created_at: START,
      updated_at: START, active_task_id: "task", active_task_state: "running", active_run_status: "running",
      active_run_started_at: START, active_activity: null, last_message_preview: "Hello",
      last_message_sender_type: "user", agent_name: "Mindy", agent_role_title: "Assistant", task_count: 1,
    },
    agent: { id: "mindy", name: "Mindy", role_title: "Assistant", status: "active", availability: "available", public_purpose: "Help" },
    tasks: [], total_input_tokens: 0, total_output_tokens: 0, total_cost_micros: 0, pending_approvals: [],
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith(`${BASE}/events?`)) return new Response(new ReadableStream({ start(controller) { stream = controller; } }), { headers: { "content-type": "text/event-stream" } });
    let data: unknown;
    if (url === BASE) data = detail;
    else if (url === `${BASE}/items`) data = { items, cursor: 100, next_before: null, has_more: false, version: 1 };
    else if (url === `${BASE}/activity`) data = { items: [], next_before: null };
    else if (url.startsWith(`${BASE}/tool-calls`) || url.startsWith(`${BASE}/files`)) data = { items: [] };
    else throw new Error(`Unexpected request: ${url}`);
    return new Response(JSON.stringify(data), { headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><ChatThreadPage /></QueryClientProvider>);
  await waitFor(() => expect(stream).toBeDefined());
  return {
    detail,
    emit: async (item: ConversationItem) => {
      await act(async () => {
        const updated = { ...item, sequence: ++sequence, revision: sequence };
        stream.enqueue(new TextEncoder().encode(`id: ${sequence}\nevent: item\ndata: ${JSON.stringify(updated)}\n\n`));
      });
    },
  };
}

afterEach(() => {
  cleanup(); clients.splice(0).forEach((client) => client.clear());
  vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear();
});

describe("public generation handoff", () => {
  it("keeps a completed streamed reply visible until its authoritative message arrives, including after the task becomes idle", async () => {
    const draft = generation("draft", "I have checked the current setup.");
    const { emit, detail } = await setup([draft]);
    expect(screen.getByText("I have checked the current setup.")).toBeTruthy();

    await emit({ ...draft, status: "completed" });
    expect(screen.getByText("I have checked the current setup.")).toBeTruthy();
    detail.conversation.active_task_id = null;
    detail.conversation.active_task_state = null;
    detail.conversation.active_run_status = null;
    await emit({ ...draft, id: "task:task", kind: "task", status: "completed", data: { id: "task", state: "completed" } });
    await waitFor(() => expect(screen.queryByTestId("working-indicator")).toBeNull());
    expect(screen.getByText("I have checked the current setup.")).toBeTruthy();

    // The authoritative projection can carry stronger redaction or a corrected
    // final response. Its content wins, without leaving a second draft bubble.
    await emit(finalMessage("saved", "The saved result is [redacted]."));
    expect(screen.getAllByText("The saved result is [redacted].")).toHaveLength(1);
    expect(screen.queryByText("I have checked the current setup.")).toBeNull();
  });

  it("keeps completed public commentary while later tool steps run", async () => {
    const commentary = generation("commentary", "I will check the project configuration.");
    const { emit } = await setup([commentary]);
    expect(screen.getByText("I will check the project configuration.")).toBeTruthy();
    await emit({ ...commentary, status: "completed" });
    await emit({ ...generation("answer", "The check is complete.", "running", 1), created_at: "2026-09-13T12:00:20Z" });
    expect(screen.getByText("I will check the project configuration.")).toBeTruthy();
    expect(screen.getByText("The check is complete.")).toBeTruthy();
    await emit(finalMessage("saved", "The check is complete."));
    expect(screen.getByText("I will check the project configuration.")).toBeTruthy();
    expect(screen.getAllByText("The check is complete.")).toHaveLength(1);
  });

  it("reconciles separate replies within a steered run without hiding its next generation", async () => {
    const first = generation("first", "The first reply.", "completed");
    const { emit } = await setup([first, finalMessage("saved-first", "The first reply.")]);
    expect(screen.getAllByText("The first reply.")).toHaveLength(1);
    const second = { ...generation("second", "The second reply.", "running", 1), created_at: "2026-09-13T12:01:00Z" };
    await emit(second);
    expect(screen.getByText("The first reply.")).toBeTruthy();
    expect(screen.getByText("The second reply.")).toBeTruthy();
    await emit({ ...second, status: "completed" });
    const saved = finalMessage("saved-second", "The second reply.");
    await emit({ ...saved, created_at: "2026-09-13T12:01:30Z", data: { ...saved.data, created_at: "2026-09-13T12:01:30Z" } });
    expect(screen.getAllByText("The first reply.")).toHaveLength(1);
    expect(screen.getAllByText("The second reply.")).toHaveLength(1);
  });

  it.each(["failed", "cancelled", "superseded", "removed"])("withdraws a %s attempt instead of retaining an invalid draft", async (status) => {
    const draft = generation("draft", "An unverified draft that must be withdrawn.");
    const { emit } = await setup([draft]);
    expect(screen.getByText("An unverified draft that must be withdrawn.")).toBeTruthy();
    await emit({ ...draft, status });
    expect(screen.queryByText("An unverified draft that must be withdrawn.")).toBeNull();
  });

  it.each([false, true])("never restores a generation after its saved message is withdrawn (snapshot: %s)", async (snapshot) => {
    const draft = generation("draft", "The earlier draft must not return.", "completed");
    const saved = finalMessage("saved", "The authoritative reply.");
    const removed = { ...saved, status: "removed", data: { message_type: "text" } };
    const { emit } = await setup([draft, snapshot ? removed : saved]);
    if (!snapshot) {
      expect(screen.getByText("The authoritative reply.")).toBeTruthy();
      await emit(removed);
    }
    expect(screen.queryByText("The authoritative reply.")).toBeNull();
    expect(screen.queryByText("The earlier draft must not return.")).toBeNull();
  });
});
