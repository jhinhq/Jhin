/** Component tests: approvals inbox cards (plan 17.11). */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApprovalCard } from "@/components/approval-card";
import type { Approval } from "@/lib/types";

afterEach(cleanup);

const pendingApproval: Approval = {
  id: "ap1",
  task_id: "task-1",
  run_id: "run-1",
  requested_by_agent_id: "agent-1",
  action_type: "system.demo.destructive",
  action_payload_sanitized: {
    tool_name: "system.demo.destructive",
    capability: "system.demo.destructive",
    risk: "destructive",
    input: { marker: "wipe-staging" },
  },
  reason: "destructive-risk calls require human approval",
  status: "pending",
  requested_at: "2026-08-16T12:00:00Z",
  decided_at: null,
  decided_by_user_id: null,
  agent_name: "DevOps",
  task_title: "Clean up staging",
};

describe("ApprovalCard", () => {
  it("renders agent, action, risk badge, reason, payload, and task link", () => {
    render(
      <ul>
        <ApprovalCard
          approval={pendingApproval}
          canDecide
          onApprove={() => {}}
          onReject={() => {}}
        />
      </ul>,
    );
    expect(screen.getByText("DevOps")).toBeDefined();
    expect(screen.getByText("system.demo.destructive")).toBeDefined();
    expect(screen.getByText("destructive")).toBeDefined();
    expect(screen.getByText("destructive-risk calls require human approval")).toBeDefined();
    expect(screen.getByText(/wipe-staging/)).toBeDefined();
    const link = screen.getByRole("link", { name: /Clean up staging/ });
    expect(link.getAttribute("href")).toBe("/tasks/task-1");
  });

  it("fires the approve and reject callbacks", () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(
      <ul>
        <ApprovalCard
          approval={pendingApproval}
          canDecide
          onApprove={onApprove}
          onReject={onReject}
        />
      </ul>,
    );
    screen.getByRole("button", { name: /Approve/ }).click();
    screen.getByRole("button", { name: /Reject/ }).click();
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it("hides decision buttons for viewers", () => {
    render(
      <ul>
        <ApprovalCard
          approval={pendingApproval}
          canDecide={false}
          onApprove={() => {}}
          onReject={() => {}}
        />
      </ul>,
    );
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
  });

  it("shows a name + short content preview for skills.create, not the raw payload", () => {
    const longBody = "x".repeat(500);
    const skillApproval: Approval = {
      ...pendingApproval,
      action_type: "skills.create",
      action_payload_sanitized: {
        tool_name: "skills.create",
        capability: "skills.manage",
        risk: "elevated",
        input: {
          name: "team-standup-notes",
          description: "Write a crisp daily standup summary.",
          content: longBody,
        },
      },
    };
    render(
      <ul>
        <ApprovalCard
          approval={skillApproval}
          canDecide
          onApprove={() => {}}
          onReject={() => {}}
        />
      </ul>,
    );
    expect(screen.getByText("team-standup-notes")).toBeDefined();
    // Truncated: the 500-char body is not shown in full.
    expect(screen.queryByText(longBody)).toBeNull();
    expect(screen.queryByText(/"description"/)).toBeNull();
  });

  it("hides decision buttons once decided and shows the status", () => {
    render(
      <ul>
        <ApprovalCard
          approval={{
            ...pendingApproval,
            status: "approved",
            decided_at: "2026-08-16T12:05:00Z",
          }}
          canDecide
          onApprove={() => {}}
          onReject={() => {}}
        />
      </ul>,
    );
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
    expect(screen.getByText("approved")).toBeDefined();
  });
});
