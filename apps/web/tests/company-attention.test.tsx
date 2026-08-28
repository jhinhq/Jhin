/** Component tests: the attention inbox all-clear and populated states. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AttentionInbox } from "@/components/company/attention-inbox";
import type { Attention } from "@/lib/types";

afterEach(cleanup);

const empty: Attention = {
  pending_approvals: [],
  failed_tasks: [],
  waiting_conversations: [],
  counts: { approvals: 0, failures: 0, total: 0 },
};

describe("AttentionInbox", () => {
  it("shows a calm all-clear state when nothing needs a human", () => {
    render(<AttentionInbox data={empty} canDecide onDecide={vi.fn()} />);
    expect(screen.getByTestId("attention-all-clear")).toBeDefined();
    expect(screen.getByText("You’re all caught up")).toBeDefined();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows the budget notice when spend crosses the warning threshold", () => {
    const data: Attention = {
      ...empty,
      budget: {
        monthly_budget_micros: 1_000_000,
        spent_month_micros: 840_000,
        percent_used: 84,
      },
    };
    render(<AttentionInbox data={data} canDecide onDecide={vi.fn()} />);
    expect(screen.queryByTestId("attention-all-clear")).toBeNull();
    const notice = screen.getByTestId("budget-notice");
    expect(notice.textContent).toContain("84% of this month");
    expect(notice.textContent).toContain("$0.84 of $1.00");
  });

  it("lists approvals, failed work, and waiting chats with the right links", () => {
    const onDecide = vi.fn();
    const data: Attention = {
      pending_approvals: [
        {
          id: "ap1",
          task_id: "task-1",
          run_id: "run-1",
          requested_by_agent_id: "agent-1",
          action_type: "github.pull_request.merge",
          action_payload_sanitized: {},
          reason: "Merging needs a human OK",
          status: "pending",
          requested_at: "2026-08-21T11:00:00Z",
          decided_at: null,
          decided_by_user_id: null,
          agent_name: "Release Engineer",
          task_title: "Release 1.2",
        },
      ],
      failed_tasks: [
        {
          id: "task-2",
          title: "Rotate the webhook secret",
          description: "",
          state: "failed",
          priority: "normal",
          assigned_agent_id: "agent-1",
          temporal_workflow_id: null,
          external_source: null,
          external_id: null,
          trigger_id: null,
          parent_task_id: null,
          metadata_json: { origin: "conversation", conversation_id: "conv-2" },
          created_at: "2026-08-21T10:00:00Z",
          updated_at: "2026-08-21T10:30:00Z",
        },
        {
          id: "task-3",
          title: "Nightly cleanup",
          description: "",
          state: "failed",
          priority: "normal",
          assigned_agent_id: "agent-1",
          temporal_workflow_id: null,
          external_source: null,
          external_id: null,
          trigger_id: null,
          parent_task_id: null,
          metadata_json: {},
          created_at: "2026-08-21T10:00:00Z",
          updated_at: "2026-08-21T10:30:00Z",
        },
      ],
      waiting_conversations: [
        {
          id: "conv-3",
          workspace_id: "ws",
          title: "Plan the launch",
          status: "active",
          pinned: false,
          primary_agent_id: "agent-1",
          created_by_user_id: "u1",
          last_activity_at: "2026-08-21T11:30:00Z",
          created_at: "2026-08-21T09:00:00Z",
          updated_at: "2026-08-21T11:30:00Z",
          active_task_id: "task-4",
          active_task_state: "running",
          active_run_status: "waiting_approval",
          active_activity: null,
          last_message_preview: "Should I announce on Monday?",
          last_message_sender_type: "agent",
          agent_name: "Marketing Lead",
          agent_role_title: "Marketing Lead",
          task_count: 2,
        },
      ],
      counts: { approvals: 1, failures: 2, total: 4 },
    };
    render(<AttentionInbox data={data} canDecide onDecide={onDecide} now={Date.parse("2026-08-21T12:00:00Z")} />);
    expect(screen.queryByTestId("attention-all-clear")).toBeNull();
    expect(screen.getByText("Waiting for your approval (1)")).toBeDefined();
    screen.getByRole("button", { name: /Approve/ }).click();
    expect(onDecide).toHaveBeenCalledWith("ap1", "approve");

    expect(screen.getByText("Ran into a problem (2)")).toBeDefined();
    expect(screen.getByRole("link", { name: /Rotate the webhook secret/ }).getAttribute("href")).toBe("/chats/conv-2");
    expect(screen.getByRole("link", { name: /Nightly cleanup/ }).getAttribute("href")).toBe("/tasks/task-3");

    expect(screen.getByText("Chats waiting on you (1)")).toBeDefined();
    expect(screen.getByRole("link", { name: /Plan the launch/ }).getAttribute("href")).toBe("/chats/conv-3");
  });
});
