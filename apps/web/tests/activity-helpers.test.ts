/** Unit tests: pure activity-feed helpers. */

import { describe, expect, it } from "vitest";
import {
  actorNameOf,
  detailText,
  friendlyHandoff,
  kindsParam,
  timeAgo,
} from "@/lib/activity";
import type { ActivityCard } from "@/lib/types";

function card(overrides: Partial<ActivityCard> = {}): ActivityCard {
  return {
    id: "msg:1",
    kind: "asked_agent",
    label: "Asked another agent",
    actor_type: "agent",
    actor_agent_id: "cto",
    actor_agent_name: "CTO",
    target_agent_id: "swe",
    target_agent_name: "Senior SWE",
    task_id: "task-1",
    task_title: "Ship the login page",
    root_task_id: "task-0",
    conversation_id: "conv-1",
    approval_id: null,
    summary: "Please add retry logic to the webhook handler. It keeps timing out.",
    detail_json: {},
    created_at: "2026-08-21T12:00:00Z",
    ...overrides,
  };
}

describe("kindsParam", () => {
  it("builds the kinds query param", () => {
    expect(kindsParam("all")).toBeUndefined();
    expect(kindsParam("handoffs")).toBe("asked_agent,reported,escalated");
    expect(kindsParam("review")).toBe("needs_review");
  });
});

describe("friendlyHandoff", () => {
  it("phrases a delegation as a request between agents", () => {
    expect(friendlyHandoff(card())).toBe("CTO asked Senior SWE to add retry logic to the webhook handler");
  });

  it("falls back to the task title when there is no summary", () => {
    expect(friendlyHandoff(card({ summary: "" }))).toBe("CTO asked Senior SWE to work on “Ship the login page”");
  });

  it("covers reports, escalations, reviews, and lifecycle kinds", () => {
    expect(friendlyHandoff(card({ kind: "reported" }))).toBe("CTO reported back to Senior SWE");
    expect(friendlyHandoff(card({ kind: "reported", target_agent_name: null }))).toBe("CTO reported back");
    expect(friendlyHandoff(card({ kind: "escalated" }))).toBe("CTO needs help from Senior SWE");
    expect(friendlyHandoff(card({ kind: "needs_review" }))).toBe("CTO needs your review");
    expect(friendlyHandoff(card({ kind: "started" }))).toBe("CTO started working on “Ship the login page”");
    expect(friendlyHandoff(card({ kind: "finished", task_title: null }))).toBe("CTO finished");
    expect(friendlyHandoff(card({ kind: "failed" }))).toBe("CTO ran into a problem");
    expect(friendlyHandoff(card({ kind: "queued" }))).toBe("CTO is waiting for a free slot");
  });

  it("names users and the system when there is no agent actor", () => {
    expect(actorNameOf({ actor_type: "user", actor_agent_name: null })).toBe("You");
    expect(actorNameOf({ actor_type: "system", actor_agent_name: null })).toBe("Jhin");
    expect(friendlyHandoff(card({ actor_type: "user", actor_agent_name: null, kind: "status_update" }))).toBe(
      "You shared an update",
    );
  });
});

describe("timeAgo", () => {
  const now = Date.parse("2026-08-21T12:00:00Z");
  it("renders relative buckets", () => {
    expect(timeAgo("2026-08-21T11:59:40Z", now)).toBe("just now");
    expect(timeAgo("2026-08-21T11:55:00Z", now)).toBe("5m ago");
    expect(timeAgo("2026-08-21T09:00:00Z", now)).toBe("3h ago");
    expect(timeAgo("2026-08-19T12:00:00Z", now)).toBe("2d ago");
  });
  it("treats future timestamps as just now and bad input as empty", () => {
    expect(timeAgo("2026-08-21T12:05:00Z", now)).toBe("just now");
    expect(timeAgo("not a date", now)).toBe("");
  });
  it("falls back to a short date after a week", () => {
    expect(timeAgo("2026-07-01T12:00:00Z", now)).toMatch(/Jul/);
  });
});

describe("detailText", () => {
  it("hides empty details and pretty-prints the rest", () => {
    expect(detailText({})).toBeNull();
    expect(detailText({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
});
