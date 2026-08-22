/** Transcript: work-request and review messages render as friendly cards. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Transcript } from "@/components/chat/transcript";
import { friendlyMessageLabel, mergeTimeline, workRequestDetailLines } from "@/lib/chat";
import type { ConversationMessage } from "@/lib/types";

vi.mock("@/lib/hooks", () => ({}));

afterEach(cleanup);

function message(overrides: Partial<ConversationMessage>): ConversationMessage {
  return {
    id: "m1",
    task_id: "t1",
    run_id: null,
    sender_type: "agent",
    sender_id: "a1",
    message_type: "question",
    content_json: {},
    created_at: "2026-08-21T10:00:00Z",
    conversation_id: "c1",
    sender_name: "CTO",
    agent_id: "a1",
    ...overrides,
  };
}

const asked = message({
  id: "m1",
  message_type: "question",
  content_json: {
    summary: "Check the retry logic",
    kind: "work_request",
    work_request_id: "wr-1",
    status: "pending",
    target_agent_id: "a2",
    target_agent_name: "Senior SWE",
    from_agent_id: "a1",
    from_agent_name: "CTO",
    instructions: "Please look at the retry branch.",
    expected_output: "A yes/no with reasons",
  },
});

const accepted = message({
  id: "m2",
  message_type: "status",
  sender_id: "a2",
  agent_id: "a2",
  sender_name: "Senior SWE",
  created_at: "2026-08-21T10:01:00Z",
  content_json: {
    summary: "Accepted: Check the retry logic",
    kind: "work_request",
    work_request_id: "wr-1",
    status: "accepted",
    target_agent_id: "a2",
    target_agent_name: "Senior SWE",
    from_agent_id: "a2",
    from_agent_name: "Senior SWE",
    created_task_id: "task-9",
  },
});

const reported = message({
  id: "m3",
  message_type: "result",
  sender_id: "a2",
  agent_id: "a2",
  sender_name: "Senior SWE",
  created_at: "2026-08-21T10:05:00Z",
  content_json: {
    summary: "Retries handle timeouts correctly.",
    kind: "work_request",
    work_request_id: "wr-1",
    status: "completed",
    target_agent_name: "Senior SWE",
    from_agent_id: "a2",
    from_agent_name: "Senior SWE",
    risks: ["Backoff is aggressive"],
  },
});

const reviewed = message({
  id: "m4",
  message_type: "review_result",
  created_at: "2026-08-21T10:06:00Z",
  content_json: { summary: "Fine to merge.", verdict: "approve", from_agent_name: "QA" },
});

describe("work-request labels", () => {
  it("follows the request through its lifecycle", () => {
    expect(friendlyMessageLabel(asked)).toBe("Asked Senior SWE for help");
    expect(friendlyMessageLabel(accepted)).toBe("Senior SWE accepted the request");
    expect(friendlyMessageLabel(reported)).toBe("Senior SWE finished and reported back");
    expect(friendlyMessageLabel(reviewed)).toBe("QA's review: looks good");
    expect(friendlyMessageLabel({ ...reviewed, content_json: { verdict: "changes_requested" } })).toBe("Review: needs changes");
  });

  it("lists what was asked for without ids", () => {
    expect(workRequestDetailLines(asked)).toEqual(["Please look at the retry branch.", "Expected: A yes/no with reasons"]);
    expect(workRequestDetailLines(reported)).toEqual(["Risks: Backoff is aggressive"]);
    expect(workRequestDetailLines(asked).join(" ")).not.toContain("wr-1");
  });
});

describe("Transcript work-request cards", () => {
  it("renders the request, the acceptance, and the report as cards with avatars per sender", () => {
    render(
      <Transcript
        items={mergeTimeline([asked, accepted, reported, reviewed], [])}
        agentName="CTO"
        userName="Ada"
        agentAvatars={{ a1: "/m/cto", a2: null }}
      />,
    );
    const cards = screen.getAllByTestId("work-request-card");
    expect(cards).toHaveLength(3);
    expect(cards[0].textContent).toContain("Asked Senior SWE for help");
    expect(cards[0].textContent).toContain("Expected: A yes/no with reasons");
    expect(cards[0].querySelector("img")?.getAttribute("src")).toBe("/m/cto?size=64");
    expect(cards[1].textContent).toContain("Senior SWE accepted the request");
    expect(cards[1].querySelector("img")).toBeNull();
    expect(cards[2].textContent).toContain("Senior SWE finished and reported back");
    expect(cards[2].textContent).toContain("Risks: Backoff is aggressive");
    expect(screen.getByTestId("work-card").textContent).toContain("QA's review: looks good");
    expect(document.body.textContent).not.toContain("wr-1");
    expect(document.body.textContent).not.toContain("task-9");
  });
});
