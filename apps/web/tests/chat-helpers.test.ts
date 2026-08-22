/** Unit tests: pure chat helpers (lib/chat.ts). */

import { describe, expect, it } from "vitest";
import {
  filterConversations,
  friendlyMessageLabel,
  mergeTimeline,
  relativeTime,
  sortByActivity,
  statusLabelFor,
} from "@/lib/chat";
import type { ActivityCard, Conversation, ConversationMessage } from "@/lib/types";

const NOW = new Date("2026-08-21T12:00:00Z");

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

function card(overrides: Partial<ActivityCard>): ActivityCard {
  return {
    id: "task:t1:started",
    kind: "started",
    label: "Started working",
    actor_type: "agent",
    actor_agent_id: "a1",
    actor_agent_name: "Scout",
    target_agent_id: null,
    target_agent_name: null,
    task_id: "t1",
    task_title: "Do the thing",
    root_task_id: "t1",
    conversation_id: "c1",
    approval_id: null,
    summary: "",
    detail_json: {},
    created_at: "2026-08-21T10:00:00Z",
    ...overrides,
  };
}

describe("relativeTime", () => {
  it("buckets by age", () => {
    expect(relativeTime("2026-08-21T11:59:50Z", NOW)).toBe("just now");
    expect(relativeTime("2026-08-21T11:55:00Z", NOW)).toBe("5m");
    expect(relativeTime("2026-08-21T09:00:00Z", NOW)).toBe("3h");
    expect(relativeTime("2026-08-19T12:00:00Z", NOW)).toBe("2d");
    expect(relativeTime("2026-07-01T12:00:00Z", NOW)).toMatch(/Jul/);
  });

  it("returns an empty string for garbage", () => {
    expect(relativeTime("nope", NOW)).toBe("");
  });
});

describe("statusLabelFor", () => {
  it("prefers the approval wait over task state", () => {
    expect(
      statusLabelFor({ active_task_state: "running", active_run_status: "waiting_approval" }),
    ).toMatchObject({ label: "Needs your review", kind: "review" });
  });

  it("shows the parked review wait", () => {
    expect(
      statusLabelFor({ active_task_state: "running", active_run_status: "waiting_review" }),
    ).toMatchObject({ label: "Waiting for a review", kind: "waiting_review", tone: "neutral" });
  });

  it("maps task states to friendly text", () => {
    expect(statusLabelFor({ active_task_state: "running", active_run_status: null })?.label).toBe(
      "Working…",
    );
    expect(statusLabelFor({ active_task_state: "queued", active_run_status: null })?.label).toBe(
      "Waiting for a free slot",
    );
    expect(statusLabelFor({ active_task_state: "paused", active_run_status: null })?.label).toBe(
      "Paused",
    );
    expect(statusLabelFor({ active_task_state: null, active_run_status: null })).toBeNull();
    expect(statusLabelFor({ active_task_state: "completed", active_run_status: null })).toBeNull();
  });
});

describe("friendlyMessageLabel", () => {
  it("names the target for delegations and reviews", () => {
    expect(
      friendlyMessageLabel(
        message({ message_type: "delegation", content_json: { target_agent_name: "QA" } }),
      ),
    ).toBe("Asked QA for help");
    expect(
      friendlyMessageLabel(
        message({ message_type: "review_request", content_json: { target_agent_name: "QA" } }),
      ),
    ).toBe("Asked QA to review");
    expect(friendlyMessageLabel(message({ message_type: "delegation", content_json: {} }))).toBe(
      "Asked another agent for help",
    );
  });

  it("describes results, verdicts, and escalations", () => {
    expect(
      friendlyMessageLabel(message({ message_type: "result", content_json: { from_agent_name: "QA" } })),
    ).toBe("QA reported back");
    expect(
      friendlyMessageLabel(
        message({ message_type: "review_result", content_json: { verdict: "fail" } }),
      ),
    ).toBe("Review: needs changes");
    expect(friendlyMessageLabel(message({ message_type: "escalation" }))).toBe("Needs help");
    expect(friendlyMessageLabel(message({ message_type: "status" }))).toBe("Shared an update");
  });
});

describe("mergeTimeline", () => {
  it("interleaves by time, drops message-backed cards, and dedupes", () => {
    const messages = [
      message({ id: "m1", sender_type: "user", created_at: "2026-08-21T10:00:00Z" }),
      message({ id: "m2", created_at: "2026-08-21T10:05:00Z" }),
      message({ id: "m2", created_at: "2026-08-21T10:05:00Z" }),
    ];
    const activity = [
      card({ id: "task:t1:completed", kind: "finished", created_at: "2026-08-21T10:06:00Z" }),
      card({ id: "task:t1:started", kind: "started", created_at: "2026-08-21T10:00:00Z" }),
      card({ id: "msg:m2", kind: "reported", created_at: "2026-08-21T10:05:00Z" }),
      card({ id: "msg:other", kind: "asked_agent", created_at: "2026-08-21T10:02:00Z" }),
      card({ id: "task:t1:started", kind: "started", created_at: "2026-08-21T10:00:00Z" }),
    ];
    const result = mergeTimeline(messages, activity);
    expect(result.map((item) => item.id)).toEqual([
      "message:m1",
      "activity:task:t1:started",
      "message:m2",
      "activity:task:t1:completed",
    ]);
  });

  it("keeps needs_review cards", () => {
    const result = mergeTimeline([], [card({ id: "approval:x", kind: "needs_review" })]);
    expect(result).toHaveLength(1);
    expect(result[0].kind).toBe("activity");
  });
});

describe("rail helpers", () => {
  const base: Pick<Conversation, "title" | "agent_name" | "last_message_preview" | "last_activity_at"> = {
    title: "Weekly summary",
    agent_name: "Scout",
    last_message_preview: "Here is the summary",
    last_activity_at: "2026-08-21T10:00:00Z",
  };

  it("filters by title, agent, or preview", () => {
    const items = [base, { ...base, title: "Other", agent_name: "Bolt", last_message_preview: null }];
    expect(filterConversations(items, "weekly")).toHaveLength(1);
    expect(filterConversations(items, "bolt")).toHaveLength(1);
    expect(filterConversations(items, "summary")).toHaveLength(1);
    expect(filterConversations(items, "")).toHaveLength(2);
  });

  it("sorts newest first", () => {
    const older = { ...base, last_activity_at: "2026-08-20T10:00:00Z" };
    expect(sortByActivity([older, base])[0]).toBe(base);
  });
});
