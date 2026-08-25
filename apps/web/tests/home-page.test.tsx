/** Home: the sections render real numbers from the shared hooks, degrade to
 * empty/loading/error states, and only offer the setup checklist while the
 * workspace is incomplete. */

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ActivityCard,
  Attention,
  Conversation,
  ModelProfile,
  OrgAgentNode,
  Task,
  WorkspaceSpend,
} from "@/lib/types";

interface QueryLike<T> {
  data: T | undefined;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  failureCount: number;
  refetch: () => void;
}

interface HomeState {
  attention: QueryLike<Attention>;
  conversations: QueryLike<{ items: Conversation[]; total: number }>;
  running: QueryLike<{ items: Task[]; total: number }>;
  queued: QueryLike<{ items: Task[]; total: number }>;
  activity: QueryLike<{ items: ActivityCard[]; next_before: string | null }>;
  graph: QueryLike<{ workspace_id: string; teams: { id: string }[]; agents: OrgAgentNode[] }>;
  spend: QueryLike<WorkspaceSpend>;
  profiles: QueryLike<ModelProfile[]>;
  agents: QueryLike<{ id: string; status: string }[]>;
  connections: QueryLike<{ id: string }[]>;
}

function ready<T>(data: T): QueryLike<T> {
  return { data, isPending: false, isError: false, error: null, failureCount: 0, refetch: vi.fn() };
}
function pending<T>(): QueryLike<T> {
  return {
    data: undefined,
    isPending: true,
    isError: false,
    error: null,
    failureCount: 0,
    refetch: vi.fn(),
  };
}
function failed<T>(): QueryLike<T> {
  return {
    data: undefined,
    isPending: false,
    isError: true,
    error: new Error("nope"),
    failureCount: 4,
    refetch: vi.fn(),
  };
}

const state = {} as HomeState;

vi.mock("@/lib/hooks", () => ({
  useAttention: () => state.attention,
  useConversations: () => state.conversations,
  useTasks: (_workspaceId: string, params: { state?: string }) =>
    params.state === "queued" ? state.queued : state.running,
  useActivity: () => state.activity,
  useOrgGraph: () => state.graph,
  useWorkspaceSpend: () => state.spend,
  useAgentAvatarMap: () => ({}),
  useModelProfiles: () => state.profiles,
  useAgents: () => state.agents,
  useConnections: () => state.connections,
}));

import HomePage from "@/app/(app)/home/page";
import { WorkspaceProvider } from "@/lib/workspace-context";

function agent(id: string, name: string): OrgAgentNode {
  return {
    id,
    name,
    slug: name.toLowerCase(),
    role_title: "Engineer",
    status: "active",
    team_id: "team-1",
    manager_agent_id: null,
  };
}

function task(id: string, title: string, agentId: string | null): Task {
  return {
    id,
    title,
    description: "",
    state: "running",
    priority: "normal",
    assigned_agent_id: agentId,
    temporal_workflow_id: null,
    external_source: null,
    external_id: null,
    trigger_id: null,
    parent_task_id: null,
    metadata_json: {},
    created_at: "2026-08-24T11:55:00Z",
    updated_at: "2026-08-24T11:59:00Z",
  };
}

function conversation(id: string, title: string): Conversation {
  return {
    id,
    workspace_id: "workspace-1",
    title,
    status: "active",
    pinned: false,
    primary_agent_id: "agent-1",
    created_by_user_id: "user-1",
    last_activity_at: "2026-08-24T11:50:00Z",
    created_at: "2026-08-24T11:00:00Z",
    updated_at: "2026-08-24T11:50:00Z",
    active_task_id: "task-1",
    active_task_state: "running",
    active_run_status: "running",
    last_message_preview: "Looking into the flaky test now",
    last_message_sender_type: "agent",
    agent_name: "Ada",
    agent_role_title: "Engineer",
    task_count: 1,
  };
}

const ATTENTION: Attention = {
  pending_approvals: [],
  failed_tasks: [],
  waiting_conversations: [conversation("conv-9", "Waiting on you")],
  counts: { approvals: 2, failures: 1, reviews: 3, total: 6 },
};

const SPEND: WorkspaceSpend = {
  spent_month_micros: 12_500_000,
  spent_total_micros: 40_000_000,
  period_start: "2026-08-01T00:00:00Z",
  providers: [],
  monthly_budget_micros: null,
  warning_threshold: 0.8,
  fetched_at: "2026-08-24T12:00:00Z",
  untracked: [],
  untracked_runs: 0,
};

/** A fully set-up workspace with work in flight. */
function populated() {
  state.attention = ready(ATTENTION);
  state.conversations = ready({ items: [conversation("conv-1", "Flaky test triage")], total: 1 });
  state.running = ready({ items: [task("task-1", "Fix the flaky test", "agent-1")], total: 1 });
  state.queued = ready({ items: [task("task-2", "Write release notes", "agent-2")], total: 1 });
  state.activity = ready({
    items: [
      {
        id: "act-1",
        kind: "asked_agent",
        label: "Asked",
        actor_type: "agent",
        actor_agent_id: "agent-1",
        actor_agent_name: "Ada",
        target_agent_id: "agent-2",
        target_agent_name: "Grace",
        task_id: "task-1",
        task_title: "Fix the flaky test",
        root_task_id: "task-1",
        conversation_id: "conv-1",
        approval_id: null,
        summary: "reproduce the failure",
        detail_json: {},
        created_at: "2026-08-24T11:58:00Z",
      },
    ],
    next_before: null,
  });
  state.graph = ready({
    workspace_id: "workspace-1",
    teams: [{ id: "team-1" }],
    agents: [agent("agent-1", "Ada"), agent("agent-2", "Grace")],
  });
  state.spend = ready(SPEND);
  state.profiles = ready([{ id: "profile-1" } as unknown as ModelProfile]);
  state.agents = ready([
    { id: "agent-1", status: "active" },
    { id: "agent-2", status: "active" },
  ]);
  state.connections = ready([{ id: "conn-1" }]);
}

/** Set up, but nothing has happened yet. */
function emptyButReady() {
  populated();
  state.attention = ready({
    pending_approvals: [],
    failed_tasks: [],
    waiting_conversations: [],
    counts: { approvals: 0, failures: 0, total: 0 },
  });
  state.conversations = ready({ items: [], total: 0 });
  state.running = ready({ items: [], total: 0 });
  state.queued = ready({ items: [], total: 0 });
  state.activity = ready({ items: [], next_before: null });
}

function renderHome(role: "owner" | "member" = "owner") {
  render(
    <WorkspaceProvider
      user={{
        id: "user-1",
        email: "qa@jhin.dev",
        display_name: "Quinn Ash",
        created_at: "2026-08-01T00:00:00Z",
      }}
      workspace={{
        workspace_id: "workspace-1",
        workspace_name: "QA Fresh",
        workspace_slug: "qa-fresh",
        role,
      }}
    >
      <HomePage />
    </WorkspaceProvider>,
  );
}

beforeEach(() => populated());
afterEach(cleanup);

describe("HomePage", () => {
  it("greets by first name and shows what needs a person", () => {
    renderHome();
    expect(screen.getByRole("heading", { name: "Hi Quinn" })).toBeTruthy();

    const needs = screen.getByTestId("needs-you-items");
    expect(within(needs).getByTestId("need-waiting-for-your-approval").textContent).toContain("2");
    expect(within(needs).getByTestId("need-waiting-for-your-review").textContent).toContain("3");
    expect(within(needs).getByTestId("need-ran-into-a-problem").textContent).toContain("1");
    expect(within(needs).getByTestId("need-chats-waiting-on-you").textContent).toContain("1");
    expect(
      within(needs).getByTestId("need-waiting-for-your-approval").getAttribute("href"),
    ).toBe("/attention");
  });

  it("names the agent behind each running task and counts the queue", () => {
    renderHome();
    const running = screen.getByTestId("running-tasks");
    expect(within(running).getByText("Fix the flaky test")).toBeTruthy();
    expect(within(running).getByText(/Ada · started/)).toBeTruthy();
    expect(within(running).getByText(/1 more task is queued/)).toBeTruthy();
    // The handoff strip reads as a sentence, not as ids.
    expect(screen.getByText("Ada asked Grace to reproduce the failure")).toBeTruthy();
  });

  it("lists recent chats with their preview and live status", () => {
    renderHome();
    const chats = screen.getByTestId("recent-chats");
    expect(within(chats).getByText("Flaky test triage")).toBeTruthy();
    expect(within(chats).getByText("Looking into the flaky test now")).toBeTruthy();
    expect(within(chats).getByTestId("live-status")).toBeTruthy();
  });

  it("summarises the team and this month's spend", () => {
    renderHome();
    const stats = screen.getByTestId("team-stats");
    expect(within(stats).getByText("agents").previousSibling?.textContent).toBe("2");
    expect(within(stats).getByText("working now").previousSibling?.textContent).toBe("1");
    expect(screen.getByTestId("spend-tile")).toBeTruthy();
    expect(screen.getByText("$12.50")).toBeTruthy();
  });

  it("hides the setup checklist once the workspace is set up", () => {
    renderHome();
    expect(screen.queryByText("Getting started")).toBeNull();
  });

  it("offers the setup checklist while a piece is missing", () => {
    populated();
    state.profiles = ready([]);
    renderHome();
    expect(screen.getByText("Getting started")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Set up a model" })).toBeTruthy();
    expect(screen.getByTestId("setup-step-3").textContent).toContain("Connect an app");
  });

  it("never offers the setup checklist to non-admins", () => {
    populated();
    state.profiles = ready([]);
    renderHome("member");
    expect(screen.queryByText("Getting started")).toBeNull();
  });

  it("explains each empty section instead of showing blank space", () => {
    emptyButReady();
    renderHome();
    expect(screen.getByText(/Nothing needs you right now/)).toBeTruthy();
    expect(screen.getByText(/Nothing is running right now/)).toBeTruthy();
    expect(screen.getByText(/No chats yet/)).toBeTruthy();
  });

  it("shows a spinner per section while its data loads", () => {
    populated();
    state.attention = pending();
    state.running = pending();
    renderHome();
    expect(screen.getByText("Checking what needs you…")).toBeTruthy();
    expect(screen.getByText("Checking what's running…")).toBeTruthy();
  });

  it("says what failed and offers a retry when a section errors", () => {
    populated();
    state.attention = failed();
    state.spend = failed();
    state.activity = failed();
    renderHome();
    expect(screen.getByText(/We couldn’t load what needs you/)).toBeTruthy();
    expect(screen.getByText(/We couldn’t load this month's spend/)).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Retry" }).length).toBeGreaterThan(1);
    // A broken feed degrades to one line; the running list stays usable.
    expect(screen.getByText(/Recent activity could not be loaded/)).toBeTruthy();
    expect(screen.getByTestId("running-tasks")).toBeTruthy();
  });
});
