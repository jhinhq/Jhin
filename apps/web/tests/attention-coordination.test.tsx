/** Component tests: the attention inbox's review, help-request, and
 * proposed-memory sections with their decision dialogs. */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AttentionInbox } from "@/components/company/attention-inbox";
import type { Attention, MemoryRecord, WorkRequest, WorkReview } from "@/lib/types";

afterEach(cleanup);

const now = Date.parse("2026-08-21T12:00:00Z");

const review: WorkReview = {
  id: "rv1",
  workspace_id: "ws",
  policy_id: "p1",
  task_id: "task-1",
  run_id: "run-1",
  tool_call_id: "tc1",
  work_request_id: null,
  subject_agent_id: "agent-1",
  trigger_key: "pre_action:tc1:p1",
  mode: "pre_action",
  evidence_json: { tool_name: "github.pull_request.merge", matched_conditions: ["destructive_action"], fail_closed: true },
  reviewer_type: "human",
  reviewer_agent_id: null,
  reviewer_user_id: null,
  status: "pending",
  verdict: null,
  feedback: "",
  requested_at: "2026-08-21T11:40:00Z",
  decided_at: null,
  decided_by_user_id: null,
  decided_by_agent_id: null,
  created_at: "2026-08-21T11:40:00Z",
  subject_agent_name: "Release Engineer",
  reviewer_agent_name: null,
  task_title: "Release 1.2",
};

const request: WorkRequest = {
  id: "wr1",
  workspace_id: "ws",
  conversation_id: "conv-5",
  requester_agent_id: "agent-2",
  requester_task_id: "task-2",
  requester_run_id: null,
  root_task_id: "task-2",
  requested_by_user_id: null,
  target_agent_id: "agent-3",
  title: "Check the retry logic",
  description: "Please look at the retry branch and confirm it handles timeouts.",
  expected_output: "A short yes/no with reasons",
  status: "pending",
  idempotency_key: "k",
  depth: 1,
  created_task_id: null,
  response: "",
  responded_at: null,
  completed_at: null,
  created_at: "2026-08-21T11:00:00Z",
  updated_at: "2026-08-21T11:00:00Z",
  requester_agent_name: "CTO",
  target_agent_name: "Senior SWE",
};

const proposed: MemoryRecord = {
  id: "mem1",
  workspace_id: "ws",
  scope: "workspace",
  scope_id: "ws",
  kind: "decision",
  subject: null,
  content: "The company standardises on Postgres.",
  source_conversation_id: "conv-7",
  source_message_id: null,
  source_task_id: null,
  source_event_id: null,
  visibility: "workspace",
  sensitivity: "normal",
  confidence: 0.8,
  importance: 0.9,
  tags_json: [],
  status: "proposed",
  valid_from: null,
  expires_at: null,
  pinned_at: null,
  forgotten_at: null,
  version: 1,
  supersedes_id: null,
  has_embedding: false,
  embedding_model: null,
  created_by_type: "agent",
  created_by_id: "agent-1",
  policy_json: {},
  created_at: "2026-08-21T10:30:00Z",
  updated_at: "2026-08-21T10:30:00Z",
};

const data: Attention = {
  pending_approvals: [],
  failed_tasks: [],
  waiting_conversations: [],
  pending_reviews: [review],
  counts: { approvals: 0, failures: 0, reviews: 1, total: 1 },
};

describe("AttentionInbox coordination sections", () => {
  it("lists reviews and sends the verdict with feedback from the dialog", () => {
    const onReviewDecide = vi.fn();
    render(<AttentionInbox data={data} canDecide onDecide={vi.fn()} onReviewDecide={onReviewDecide} now={now} />);
    expect(screen.getByText("Reviews waiting on you (1)")).toBeDefined();
    const card = screen.getByTestId("review-rv1");
    expect(card.textContent).toContain("Release Engineer wants to use github pull request merge");
    expect(card.textContent).toContain("Before a risky action");
    expect(card.textContent).toContain("Destructive actions");
    expect(card.textContent).not.toContain("pre_action:tc1:p1");

    fireEvent.click(screen.getByRole("button", { name: "Decide" }));
    // Changes need feedback.
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    expect(screen.getByRole("alert").textContent).toMatch(/what to change/);
    expect(onReviewDecide).not.toHaveBeenCalled();

    fireEvent.change(screen.getByPlaceholderText(/What looks good/), { target: { value: "Wait for QA first." } });
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    expect(onReviewDecide).toHaveBeenCalledWith("rv1", "changes_requested", "Wait for QA first.");
  });

  it("approves straight away from the dialog", () => {
    const onReviewDecide = vi.fn();
    render(<AttentionInbox data={data} canDecide onDecide={vi.fn()} onReviewDecide={onReviewDecide} now={now} />);
    fireEvent.click(screen.getByRole("button", { name: "Decide" }));
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    expect(onReviewDecide).toHaveBeenCalledWith("rv1", "approve", "");
  });

  it("shows help requests with admin actions, and clarification goes through a dialog", () => {
    const onWorkRequest = vi.fn();
    render(
      <AttentionInbox
        data={{ ...data, pending_reviews: [], counts: { approvals: 0, failures: 0, reviews: 0, total: 0 } }}
        canDecide
        isAdmin
        onDecide={vi.fn()}
        workRequests={[request]}
        onWorkRequest={onWorkRequest}
        now={now}
      />,
    );
    expect(screen.getByText("Help requests (1)")).toBeDefined();
    const card = screen.getByTestId("work-request-wr1");
    expect(card.textContent).toContain("CTO asked Senior SWE: Check the retry logic");
    expect(card.textContent).toContain("Expected: A short yes/no with reasons");
    expect(screen.getByRole("link", { name: /Open chat/ }).getAttribute("href")).toBe("/chats/conv-5");

    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(onWorkRequest).toHaveBeenCalledWith("wr1", "accept", "");

    fireEvent.click(screen.getByRole("button", { name: "Ask for clarification" }));
    const send = screen.getByRole("button", { name: "Send question" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Which branch?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send question" }));
    expect(onWorkRequest).toHaveBeenCalledWith("wr1", "clarify", "Which branch?");
  });

  it("hides help-request actions from non-admins", () => {
    render(
      <AttentionInbox
        data={{ ...data, pending_reviews: [] }}
        canDecide
        isAdmin={false}
        onDecide={vi.fn()}
        workRequests={[request]}
        onWorkRequest={vi.fn()}
        now={now}
      />,
    );
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
  });

  it("lets admins approve proposed company memories", () => {
    const onMemoryDecide = vi.fn();
    render(
      <AttentionInbox
        data={{ ...data, pending_reviews: [] }}
        canDecide
        isAdmin
        onDecide={vi.fn()}
        proposedMemories={[proposed]}
        onMemoryDecide={onMemoryDecide}
        now={now}
      />,
    );
    expect(screen.getByText("Memories waiting for approval (1)")).toBeDefined();
    expect(screen.getByText("The company standardises on Postgres.")).toBeDefined();
    expect(screen.getByText("Company")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onMemoryDecide).toHaveBeenCalledWith("mem1", "approve");
  });

  it("stays all-clear when every list is empty", () => {
    render(
      <AttentionInbox
        data={{ pending_approvals: [], failed_tasks: [], waiting_conversations: [], pending_reviews: [], counts: { approvals: 0, failures: 0, reviews: 0, total: 0 } }}
        canDecide
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByTestId("attention-all-clear")).toBeDefined();
  });
});
