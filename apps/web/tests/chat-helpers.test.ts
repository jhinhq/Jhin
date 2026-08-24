/** Unit tests: pure chat helpers (lib/chat.ts). */

import { describe, expect, it } from "vitest";
import {
  composerHintFor,
  dayLabel,
  exchangeLabel,
  exchangeSuffix,
  filterConversations,
  friendlyMessageLabel,
  groupExchanges,
  instructionDeliveryState,
  mergeTimeline,
  relativeTime,
  sortByActivity,
  statusLabelFor,
  withDaySeparators,
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


describe("groupExchanges", () => {
  const opts = { primaryAgentId: "a1", primaryAgentName: "Scout" };
  const delegation = message({
    id: "d1",
    message_type: "delegation",
    sender_id: "a1",
    agent_id: "a1",
    sender_name: "Scout",
    content_json: { summary: "Please handle this", target_agent_id: "a2", target_agent_name: "Linus" },
    created_at: "2026-08-21T10:00:00Z",
  });
  const reported = message({
    id: "r1",
    message_type: "result",
    sender_id: "a2",
    agent_id: "a2",
    sender_name: "Linus",
    content_json: { summary: "All done", from_agent_id: "a2", from_agent_name: "Linus" },
    created_at: "2026-08-21T10:05:00Z",
  });

  it("collapses a delegation/result run between the same pair, preserving order", () => {
    const grouped = groupExchanges(mergeTimeline([delegation, reported], []), opts);
    expect(grouped).toHaveLength(1);
    const exchange = grouped[0];
    expect(exchange.kind).toBe("exchange");
    if (exchange.kind !== "exchange") return;
    expect(exchange.count).toBe(2);
    expect(exchange.withName).toBe("Linus");
    expect(exchange.withAgentId).toBe("a2");
    expect(exchange.outcome).toBe("ok");
    expect(exchange.items.map((item) => item.id)).toEqual(["message:d1", "message:r1"]);
    expect(exchangeLabel(exchange)).toBe("2 updates with Linus");
    expect(exchangeSuffix(exchange.outcome)).toBe("");
  });

  it("an interleaved user message breaks the group", () => {
    const user = message({
      id: "u1",
      sender_type: "user",
      sender_id: "person",
      agent_id: null,
      sender_name: "Ada",
      message_type: "text",
      content_json: { text: "How is it going?" },
      created_at: "2026-08-21T10:02:00Z",
    });
    const grouped = groupExchanges(mergeTimeline([delegation, user, reported], []), opts);
    expect(grouped.map((item) => item.kind)).toEqual(["exchange", "message", "exchange"]);
  });

  it("absorbs related progress chips but keeps needs_review chips visible", () => {
    const started = card({
      id: "task:t2:started",
      kind: "started",
      actor_agent_id: "a2",
      actor_agent_name: "Linus",
      task_id: "t2",
      created_at: "2026-08-21T10:01:00Z",
    });
    const review = card({ id: "approval:x", kind: "needs_review", created_at: "2026-08-21T10:06:00Z" });
    const grouped = groupExchanges(mergeTimeline([delegation, reported], [started, review]), opts);
    expect(grouped).toHaveLength(2);
    expect(grouped[0].kind).toBe("exchange");
    if (grouped[0].kind === "exchange") expect(grouped[0].count).toBe(3);
    expect(grouped[1].kind).toBe("activity");
  });

  it("flags a failed outcome from the last item", () => {
    const failed = card({
      id: "task:t2:failed",
      kind: "failed",
      actor_agent_id: "a2",
      task_id: "t2",
      created_at: "2026-08-21T10:06:00Z",
    });
    const grouped = groupExchanges(mergeTimeline([delegation, reported], [failed]), opts);
    expect(grouped).toHaveLength(1);
    if (grouped[0].kind === "exchange") {
      expect(grouped[0].outcome).toBe("problem");
      expect(exchangeSuffix(grouped[0].outcome)).toBe(" · ran into a problem");
    }
  });

  it("keeps plain agent bubbles ungrouped and labels single updates friendly", () => {
    const bubble = message({ id: "m9", message_type: "text", content_json: { text: "hello" } });
    const grouped = groupExchanges(mergeTimeline([bubble], []), opts);
    expect(grouped[0].kind).toBe("message");
    const single = groupExchanges(mergeTimeline([reported], []), opts);
    expect(single[0].kind).toBe("exchange");
    if (single[0].kind === "exchange") {
      expect(single[0].count).toBe(1);
      expect(exchangeLabel(single[0])).toBe("Linus reported back");
    }
  });
});

describe("withDaySeparators", () => {
  // Everything is built from *local* date components so the assertions hold
  // in any timezone (labels derive from the viewer's local date).
  const now = new Date(2026, 7, 21, 12, 0);
  const localIso = (day: number, hour: number, minute = 0) =>
    new Date(2026, 7, day, hour, minute).toISOString();
  const item = (id: string, at: string) => ({ id, at });

  it("inserts one marker per day change with friendly labels", () => {
    const items = [
      item("a", localIso(19, 9, 30)),
      item("b", localIso(19, 11, 0)),
      item("c", localIso(20, 18, 0)),
      item("d", localIso(21, 8, 5)),
    ];
    const result = withDaySeparators(items, now);
    const days = result.filter((entry) => "kind" in entry && entry.kind === "day");
    expect(days).toHaveLength(3);
    expect(result.map((entry) => entry.id)).toEqual([
      "day:2026-08-19",
      "a",
      "b",
      "day:2026-08-20",
      "c",
      "day:2026-08-21",
      "d",
    ]);
    const labels = days.map((day) => ("label" in day ? day.label : ""));
    expect(labels[0]).toMatch(/19/);
    expect(labels[1]).toBe("Yesterday");
    expect(labels[2]).toBe("Today");
    for (const day of days) {
      if ("time" in day) expect(day.time.length).toBeGreaterThan(0);
    }
  });

  it("skips unparseable timestamps and older years keep the year", () => {
    const result = withDaySeparators([item("junk", "nope")], now);
    expect(result.map((entry) => entry.id)).toEqual(["junk"]);
    expect(dayLabel(new Date(2025, 0, 5), now)).toMatch(/2025/);
    expect(dayLabel(new Date(2026, 7, 21, 23, 59), now)).toBe("Today");
    expect(dayLabel(new Date(2026, 7, 20, 0, 0), now)).toBe("Yesterday");
  });
});

describe("mergeTimeline detailed mode", () => {
  const card = (kind: ActivityCard["kind"], id: string): ActivityCard => ({
    id,
    kind,
    label: kind,
    actor_type: "agent",
    actor_agent_id: null,
    actor_agent_name: null,
    target_agent_id: null,
    target_agent_name: null,
    task_id: null,
    task_title: null,
    root_task_id: null,
    conversation_id: null,
    approval_id: null,
    summary: "",
    detail_json: {},
    created_at: "2026-08-22T10:00:00Z",
  });

  it("hides routine progress chips unless detailed, keeping essential ones", () => {
    const activity = [card("started", "a"), card("finished", "b"), card("failed", "c"), card("needs_review", "d")];
    const quiet = mergeTimeline([], activity, { detailed: false }).map((i) => i.id);
    expect(quiet).toEqual(["activity:c", "activity:d"]);
    const full = mergeTimeline([], activity, { detailed: true }).map((i) => i.id);
    expect(full).toEqual(["activity:a", "activity:b", "activity:c", "activity:d"]);
  });
});

describe("instructionDeliveryState", () => {
  const sent = { created_at: "2026-08-21T10:00:00Z", task_id: "t1" };

  it("is queued with no later activity", () => {
    expect(instructionDeliveryState(sent, [])).toBe("queued");
  });

  it("stays queued when the only later-looking item is actually earlier or simultaneous", () => {
    expect(
      instructionDeliveryState(sent, [
        { created_at: "2026-08-21T09:59:00Z", task_id: "t1" },
        { created_at: "2026-08-21T10:00:00Z", task_id: "t1" },
      ]),
    ).toBe("queued");
  });

  it("stays queued when the only later item belongs to a different task", () => {
    expect(instructionDeliveryState(sent, [{ created_at: "2026-08-21T10:01:00Z", task_id: "t2" }])).toBe(
      "queued",
    );
  });

  it("is delivered once an agent message or activity item on the same task lands after it", () => {
    expect(instructionDeliveryState(sent, [{ created_at: "2026-08-21T10:01:00Z", task_id: "t1" }])).toBe(
      "delivered",
    );
  });

  it("treats evidence without a task id as a match (activity cards may omit it)", () => {
    expect(instructionDeliveryState(sent, [{ created_at: "2026-08-21T10:01:00Z", task_id: null }])).toBe(
      "delivered",
    );
  });

  it("is queued for an unparseable timestamp", () => {
    expect(instructionDeliveryState({ created_at: "nope", task_id: "t1" }, [])).toBe("queued");
  });

  it("resolves multiple pending instructions independently", () => {
    const first = { created_at: "2026-08-21T10:00:00Z", task_id: "t1" };
    const second = { created_at: "2026-08-21T10:00:05Z", task_id: "t1" };
    // Only one step has run since the first instruction was sent, landing
    // between the two: it proves the first was delivered but not the second.
    const laterItems = [{ created_at: "2026-08-21T10:00:02Z", task_id: "t1" }];
    expect(instructionDeliveryState(first, laterItems)).toBe("delivered");
    expect(instructionDeliveryState(second, laterItems)).toBe("queued");
  });
});

describe("composerHintFor", () => {
  it("is null when nothing is active", () => {
    expect(composerHintFor(null, "Scout")).toBeNull();
  });

  it("names the agent and is concrete about what sending does while working", () => {
    expect(composerHintFor({ label: "Working…", tone: "accent", kind: "working" }, "Scout")).toBe(
      "Scout is working — this will steer it at the next step.",
    );
  });

  it("has a distinct message while queued", () => {
    const hint = composerHintFor({ label: "Waiting for a free slot", tone: "neutral", kind: "queued" }, "Scout");
    expect(hint).toContain("Scout");
    expect(hint).not.toBe(composerHintFor({ label: "Working…", tone: "accent", kind: "working" }, "Scout"));
  });

  it("is null for review/waiting_review/paused (not steerable by sending a message)", () => {
    expect(composerHintFor({ label: "Needs your review", tone: "warn", kind: "review" }, "Scout")).toBeNull();
    expect(
      composerHintFor({ label: "Waiting for a review", tone: "neutral", kind: "waiting_review" }, "Scout"),
    ).toBeNull();
    expect(composerHintFor({ label: "Paused", tone: "warn", kind: "paused" }, "Scout")).toBeNull();
  });
});

describe("groupExchanges never hides a user/instruction message (regression)", () => {
  const opts = { primaryAgentId: "a1", primaryAgentName: "Scout" };

  it("keeps an ordinary user message ungrouped even between agent↔agent traffic", () => {
    const delegation = message({
      id: "d1",
      message_type: "delegation",
      sender_id: "a1",
      agent_id: "a1",
      sender_name: "Scout",
      content_json: { summary: "Please handle this", target_agent_id: "a2", target_agent_name: "Linus" },
      created_at: "2026-08-21T10:00:00Z",
    });
    const instruction = message({
      id: "u1",
      sender_type: "user",
      sender_id: "person",
      agent_id: null,
      sender_name: "Ada",
      message_type: "instruction",
      content_json: { text: "Also check the staging env" },
      created_at: "2026-08-21T10:01:00Z",
    });
    const reported = message({
      id: "r1",
      message_type: "result",
      sender_id: "a2",
      agent_id: "a2",
      sender_name: "Linus",
      content_json: { summary: "All done", from_agent_id: "a2", from_agent_name: "Linus" },
      created_at: "2026-08-21T10:02:00Z",
    });
    const grouped = groupExchanges(mergeTimeline([delegation, instruction, reported], []), opts);
    // The instruction always breaks the exchange and appears as its own
    // "message" item — groupExchanges only ever collapses agent↔agent runs.
    expect(grouped.map((item) => item.kind)).toEqual(["exchange", "message", "exchange"]);
    const userItem = grouped.find((item) => item.kind === "message");
    expect(userItem && userItem.kind === "message" ? userItem.message.id : null).toBe("u1");
  });

  it("never folds a user message into an exchange even when detailed mode is off", () => {
    const delegation = message({
      id: "d2",
      message_type: "delegation",
      sender_id: "a1",
      agent_id: "a1",
      sender_name: "Scout",
      content_json: { target_agent_id: "a2", target_agent_name: "Linus" },
      created_at: "2026-08-21T10:00:00Z",
    });
    const instruction = message({
      id: "u2",
      sender_type: "user",
      sender_id: "person",
      agent_id: null,
      message_type: "instruction",
      content_json: { text: "Steer it" },
      created_at: "2026-08-21T10:00:30Z",
    });
    const merged = mergeTimeline([delegation, instruction], [], { detailed: false });
    const grouped = groupExchanges(merged, opts);
    const kinds = grouped.map((item) => item.kind);
    expect(kinds).toContain("message");
    expect(grouped.some((item) => item.kind === "message" && item.message.id === "u2")).toBe(true);
  });
});
