/** Independent detail and transcript requests must reconcile at completion. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatThreadPage from "@/app/(app)/chats/[id]/view";
import type { ActivityList, ConversationDetail, ConversationMessage, Task } from "@/lib/types";

vi.mock("@/lib/use-route-segment", () => ({ useSegmentAfter: () => "chat-1" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/workspace-context", () => ({
  useWorkspace: () => ({
    workspace: { workspace_id: "workspace-1" },
    user: { id: "user-1", display_name: "Dev Owner" },
    can: () => true,
  }),
}));
vi.mock("@/lib/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/hooks")>()),
  useAgentAvatarMap: () => ({}),
}));
// Keep the real composer, transcript, timeline, API and query hooks. These
// unrelated controls otherwise request model, grant and agent configuration.
vi.mock("@/components/chat/chat-header", () => ({ ChatHeader: () => null }));
vi.mock("@/components/chat/context-panel", () => ({ ContextPanel: () => null }));
vi.mock("@/components/chat/composer-controls", () => ({ ChatComposerControls: () => null }));
vi.mock("@/components/chat/turn-controls", () => ({ TurnControls: () => null }));

const BASE = "/api/v1/workspaces/workspace-1/conversations/chat-1";
const START = "2026-09-10T20:29:38.000Z";
const END = "2026-09-10T20:30:09.000Z";
const REPLY = "Yes — Supabase is connected and available to me.";
const messageKey = ["conversation-messages", "workspace-1", "chat-1"];
const activityKey = ["conversation-activity", "workspace-1", "chat-1"];

function task(id: string, state: Task["state"]): Task {
  return {
    id, state, title: "Check Supabase", description: "", priority: "normal",
    assigned_agent_id: "bisby", temporal_workflow_id: null, external_source: null,
    external_id: null, trigger_id: null, parent_task_id: null, metadata_json: {},
    created_at: START, updated_at: state === "running" ? START : END,
  };
}

function detail(currentTask: Task | null): ConversationDetail {
  const running = currentTask?.state === "running";
  return {
    conversation: {
      id: "chat-1", workspace_id: "workspace-1", title: "Supabase access", status: "active",
      pinned: false, primary_agent_id: "bisby", created_by_user_id: "user-1",
      // These timestamps deliberately do not advance with the final reply:
      // task completion is also a revision signal for an already-idle chat.
      last_activity_at: START, created_at: START, updated_at: START,
      active_task_id: running ? currentTask.id : null,
      active_task_state: running ? "running" : null,
      active_run_status: running ? "running" : null,
      active_run_started_at: running ? START : null,
      active_activity: null, last_message_preview: "What about now?",
      last_message_sender_type: "user", agent_name: "Bisby", agent_role_title: "Developer",
      task_count: currentTask ? 1 : 0,
    },
    agent: {
      id: "bisby", name: "Bisby", role_title: "Developer", status: "active",
      availability: "available", public_purpose: "Help with development",
    },
    tasks: currentTask ? [currentTask] : [], total_input_tokens: 0,
    total_output_tokens: 0, total_cost_micros: 0, pending_approvals: [], resume: null,
  };
}

function message(id: string, sender: "user" | "agent", text: string): ConversationMessage {
  return {
    id, task_id: "task-1", run_id: sender === "agent" ? "run-1" : null,
    sender_type: sender, sender_id: sender === "agent" ? "bisby" : "user-1",
    message_type: "text", content_json: { text }, created_at: sender === "agent" ? END : START,
    conversation_id: "chat-1", sender_name: sender === "agent" ? "Bisby" : "Dev Owner",
    agent_id: sender === "agent" ? "bisby" : null,
  };
}

function response(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200, headers: { "content-type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

let client: QueryClient;
let serverDetail: ConversationDetail;
let serverMessages: ConversationMessage[];
let serverActivity: ActivityList;
let heldMessages: ReturnType<typeof deferred<Response>> | null;
let heldActivity: ReturnType<typeof deferred<Response>> | null;
let messageRequests: number;
let activityRequests: number;
let detailRequests: number;
let rejectNextSend: boolean;
let sentIds: string[];

async function tick(milliseconds = 1) {
  // Query notifications schedule a zero-delay timer after fetch promises.
  await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(START));
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }));
  window.localStorage.clear();
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  serverDetail = detail(null);
  serverMessages = [];
  serverActivity = { items: [], next_before: null };
  heldMessages = null;
  heldActivity = null;
  messageRequests = 0;
  activityRequests = 0;
  detailRequests = 0;
  rejectNextSend = false;
  sentIds = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === `${BASE}/items`) return new Response(JSON.stringify({ detail: "Journal is not enabled" }), { status: 404 });
    if (url === `${BASE}/files`) return response({ items: [], has_more: false });
    if (url === `${BASE}/tool-calls`) return response({ items: [], has_more: false, limit: 100 });
    if (url === BASE) {
      detailRequests++;
      return response(serverDetail);
    }
    if (url === `${BASE}/messages`) {
      messageRequests++;
      if (heldMessages) {
        const pending = heldMessages;
        heldMessages = null;
        return pending.promise;
      }
      return response(serverMessages);
    }
    if (url === `${BASE}/activity`) {
      activityRequests++;
      if (heldActivity) {
        const pending = heldActivity;
        heldActivity = null;
        return pending.promise;
      }
      return response(serverActivity);
    }
    if (url === `${BASE}/turns` && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { text: string; client_turn_id: string };
      sentIds.push(body.client_turn_id);
      if (rejectNextSend) { rejectNextSend = false; throw new TypeError("Network response lost"); }
      const sent = message("user-turn", "user", body.text);
      sent.content_json.client_turn_id = body.client_turn_id;
      serverMessages = [sent];
      serverDetail = detail(task("task-1", "running"));
      return response({ conversation: serverDetail.conversation, message: sent, task_id: "task-1", mode: "new_task" });
    }
    throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
  }));
});

afterEach(() => {
  cleanup();
  client.clear();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function mountChat() {
  render(<QueryClientProvider client={client}><ChatThreadPage /></QueryClientProvider>);
}

describe("chat polling reconciliation", () => {
  it("renders only the authoritative redacted receipt and never caches a raw submitted credential", async () => {
    mountChat(); await tick();
    const originalFetch = vi.mocked(fetch).getMockImplementation()!;
    const held = deferred<Response>();
    vi.mocked(fetch).mockImplementation(async (url, init) => String(url) === `${BASE}/turns` ? held.promise : originalFetch(url, init));
    const secret = `${"a".repeat(24)}:${"b".repeat(64)}`;
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: `Here is my key ${secret}` } });
    await tick();
    fireEvent.click(screen.getByRole("button", { name: "Send message" })); await tick();
    expect(screen.queryByTestId("user-message")).toBeNull();
    expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("");
    expect(JSON.stringify(client.getMutationCache().getAll())).not.toContain(secret);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(secret);
    expect(localStorage.getItem("jhin-pending-send:workspace-1:chat-1")).toBeNull();
    expect(localStorage.getItem("jhin-draft-v1:workspace-1:chat-1")).not.toContain(secret);
    const receipt = message("safe-message", "user", "Here is my key [secret captured]");
    receipt.content_json.secure_inputs = [{ secret_ref: "ref-1", name: "Ghost Admin key", kind: "ghost_admin" }];
    serverMessages = [receipt];
    held.resolve(response({ message: receipt, conversation: serverDetail.conversation, mode: "new_task", task_id: "task-1" })); await tick();
    expect(screen.getByTestId("user-message").textContent).toContain("[secret captured]");
    expect(screen.getByText(/Secret saved: Ghost Admin key/)).toBeDefined();
    expect(document.body.textContent).not.toContain(secret);
  });
  it("steers the current turn without resubmitting a model or mode override", async () => {
    serverDetail = detail(task("task-1", "running"));
    localStorage.setItem("jhin-draft-v1:workspace-1:chat-1", JSON.stringify({ version: 1, text: "Focus on the report", attachment_ids: [], context_refs: [], execution_mode: "plan", model_profile_id: "selected-model", delivery: "steer" }));
    mountChat(); await tick();
    fireEvent.click(screen.getByRole("button", { name: "Send message" })); await tick();
    const call = vi.mocked(fetch).mock.calls.find(([url, init]) => String(url) === `${BASE}/turns` && init?.method === "POST");
    const payload = JSON.parse(String(call?.[1]?.body));
    expect(payload).toMatchObject({ text: "Focus on the report", delivery: "steer" });
    expect(payload).not.toHaveProperty("execution_mode"); expect(payload).not.toHaveProperty("model_profile_id");
  });
  it("keeps the submission identity when delivery is uncertain and the page reloads", async () => {
    mountChat(); await tick(); rejectNextSend = true;
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Do the work once." } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" })); await tick();
    expect(screen.getByRole("button", { name: "Retry delivery" })).toBeTruthy();
    cleanup(); client.clear(); mountChat(); await tick();
    fireEvent.click(screen.getByRole("button", { name: "Retry delivery" })); await tick();
    expect(sentIds).toHaveLength(2); expect(sentIds[0]).toBe(sentIds[1]);
  });
  it("shows a long-running turn's final reply even when an older transcript request is still in flight", async () => {
    mountChat();
    await tick();
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "What about now?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await tick();
    expect(screen.getByTestId("working-indicator")).toBeTruthy();
    // The optimistic-send grace must have expired before the turn completes.
    await tick(22_000);
    const stale = deferred<Response>();
    const staleActivity = deferred<Response>();
    heldMessages = stale;
    heldActivity = staleActivity;
    const oldRequest = client.refetchQueries({ queryKey: messageKey, exact: true });
    const oldActivityRequest = client.refetchQueries({ queryKey: activityKey, exact: true });
    await tick();
    expect(heldMessages).toBeNull();
    expect(heldActivity).toBeNull();

    const finalReply = message("final-reply", "agent", REPLY);
    serverMessages = [...serverMessages, finalReply];
    serverDetail = detail(task("task-1", "completed"));
    serverActivity = {
      items: [{
        id: "finished-1", kind: "finished", label: "Finished", actor_type: "agent",
        actor_agent_id: "bisby", actor_agent_name: "Bisby", target_agent_id: null,
        target_agent_name: null, task_id: "task-1", task_title: "Check Supabase",
        root_task_id: null, conversation_id: "chat-1", approval_id: null,
        summary: "", detail_json: {}, created_at: END,
      }], next_before: null,
    };
    await tick(2_100);
    expect(screen.queryByTestId("working-indicator")).toBeNull();
    expect(screen.getByText(REPLY)).toBeTruthy();
    expect(client.getQueryData<ActivityList>(activityKey)?.items[0]?.id).toBe("finished-1");

    // A slow response that began before the final reply was committed must
    // not replace the newer transcript when it eventually reaches the browser.
    stale.resolve(response([message("user-turn", "user", "What about now?")]));
    staleActivity.resolve(response({ items: [], next_before: null }));
    await Promise.all([oldRequest, oldActivityRequest]);
    await tick();
    expect(screen.getByText(REPLY)).toBeTruthy();
    expect(client.getQueryData<ConversationMessage[]>(messageKey)?.at(-1)?.id).toBe("final-reply");
    expect(client.getQueryData<ActivityList>(activityKey)?.items[0]?.id).toBe("finished-1");
  });

  it("replaces an initial stale transcript request when the first detail already reports completion", async () => {
    serverDetail = detail(task("task-1", "completed"));
    serverMessages = [message("final-reply", "agent", REPLY)];
    const stale = deferred<Response>();
    heldMessages = stale;
    mountChat();
    await tick();
    expect(heldMessages).toBeNull();
    expect(screen.getByText(REPLY)).toBeTruthy();

    stale.resolve(response([]));
    await tick();
    expect(screen.getByText(REPLY)).toBeTruthy();
    expect(client.getQueryData<ConversationMessage[]>(messageKey)?.at(-1)?.id).toBe("final-reply");
  });

  it("refreshes a turn completed between detail polls without repeatedly fetching unchanged idle transcripts", async () => {
    serverDetail = detail(task("old-task", "completed"));
    serverMessages = [message("old-reply", "agent", "The previous turn is done.")];
    mountChat();
    await tick();
    expect(screen.getByText("The previous turn is done.")).toBeTruthy();

    // Both observed details are idle: a live -> idle transition alone cannot
    // detect a quick turn sent from another tab or another device.
    serverDetail = detail(task("new-task", "completed"));
    serverMessages = [...serverMessages, message("fast-reply", "agent", REPLY)];
    await tick(2_100);
    expect(screen.getByText(REPLY)).toBeTruthy();

    const settledMessages = messageRequests;
    const settledActivity = activityRequests;
    const settledDetails = detailRequests;
    await tick(10_000);
    expect(detailRequests).toBeGreaterThan(settledDetails);
    expect(messageRequests).toBe(settledMessages);
    expect(activityRequests).toBe(settledActivity);
  });
});
