/** Pure coordination helpers: policy validation and body shape, review and
 * work-request wording, activity detail summaries. */

import { describe, expect, it } from "vitest";
import { friendlyDetailLines } from "@/lib/activity";
import {
  describeCondition,
  describeReviewer,
  describeScope,
  emptyPolicyDraft,
  matchedConditionLabels,
  policyDraftToBody,
  reviewSubject,
  rollupStatusText,
  thresholdToRaw,
  validatePolicyDraft,
  workRequestStatus,
} from "@/lib/coordination";

describe("validatePolicyDraft", () => {
  const valid = { ...emptyPolicyDraft(), name: "Check merges", conditions: [{ kind: "destructive_action" as const }] };

  it("accepts a complete workspace policy", () => {
    expect(validatePolicyDraft(valid)).toBeNull();
  });

  it("requires a name and at least one condition", () => {
    expect(validatePolicyDraft({ ...valid, name: " " })).toMatch(/name/);
    expect(validatePolicyDraft({ ...valid, conditions: [] })).toMatch(/at least one situation/);
  });

  it("mirrors the API's scope shape rules", () => {
    expect(validatePolicyDraft({ ...valid, scope_kind: "team" })).toMatch(/which team/);
    expect(validatePolicyDraft({ ...valid, scope_kind: "agent" })).toMatch(/which agent/);
    expect(validatePolicyDraft({ ...valid, scope_kind: "task_type" })).toMatch(/kind of task/);
    expect(validatePolicyDraft({ ...valid, scope_kind: "team", scope_id: "t1" })).toBeNull();
  });

  it("needs thresholds for limit conditions and a period for periodic mode", () => {
    expect(validatePolicyDraft({ ...valid, conditions: [{ kind: "cost_threshold", threshold: NaN }] })).toMatch(/limit/);
    expect(validatePolicyDraft({ ...valid, conditions: [{ kind: "cost_threshold", threshold: 5 }] })).toBeNull();
    expect(validatePolicyDraft({ ...valid, mode: "periodic", period_minutes: "0" })).toMatch(/how often/);
    expect(validatePolicyDraft({ ...valid, mode: "periodic", period_minutes: "15" })).toBeNull();
  });

  it("needs reviewer details for agent and team-role reviewers", () => {
    expect(validatePolicyDraft({ ...valid, reviewer: { kind: "agent" } })).toMatch(/which agent should review/);
    expect(validatePolicyDraft({ ...valid, reviewer: { kind: "team_role" } })).toMatch(/role label/);
  });
});

describe("policyDraftToBody", () => {
  it("sends only the scope field the API expects and converts units", () => {
    const body = policyDraftToBody({
      ...emptyPolicyDraft(),
      name: " Costly runs ",
      scope_kind: "task_type",
      scope_key: "deploy",
      mode: "periodic",
      period_minutes: "30",
      conditions: [{ kind: "cost_threshold", threshold: thresholdToRaw("dollars", 2.5) }, { kind: "always" }],
      reviewer: { kind: "agent", agent_id: "a1", fallback_to_human: false },
    });
    expect(body.name).toBe("Costly runs");
    expect(body.scope_key).toBe("deploy");
    expect(body).not.toHaveProperty("scope_id");
    expect(body.period_seconds).toBe(1800);
    expect(body.conditions).toEqual([{ kind: "cost_threshold", threshold: 2_500_000 }, { kind: "always" }]);
    expect(body.reviewer).toEqual({ kind: "agent", fallback_to_human: false, agent_id: "a1" });
  });

  it("omits scope fields for workspace scope", () => {
    const body = policyDraftToBody({ ...emptyPolicyDraft(), name: "x", conditions: [{ kind: "always" }] });
    expect(body).not.toHaveProperty("scope_id");
    expect(body).not.toHaveProperty("scope_key");
    expect(body).not.toHaveProperty("period_seconds");
  });
});

describe("plain-language descriptions", () => {
  it("describes conditions with human units", () => {
    expect(describeCondition({ kind: "cost_threshold", threshold: 1_500_000 })).toBe("Spending above a limit (over $1.50)");
    expect(describeCondition({ kind: "time_threshold", threshold: 600 })).toBe("Taking too long (over 10 min)");
    expect(describeCondition({ kind: "low_confidence", threshold: 0.4 })).toBe("Low confidence (under 40%)");
    expect(describeCondition({ kind: "always" })).toBe("Every time");
  });

  it("describes reviewers with fallbacks and scope by name", () => {
    const names = (id: string) => ({ a1: "Ada", a2: "Grace" })[id];
    expect(describeReviewer({ kind: "reporting_manager", fallback_to_human: true })).toBe("the agent's manager, otherwise a person");
    expect(describeReviewer({ kind: "agent", agent_id: "a1", fallback_agent_id: "a2", fallback_to_human: false }, names)).toBe("Ada, otherwise Grace");
    expect(describeReviewer({ kind: "human" })).toBe("a person");
    expect(describeScope({ scope_kind: "agent", scope_id: "a1", scope_key: null }, { agent: names })).toBe("Ada");
    expect(describeScope({ scope_kind: "task_type", scope_id: null, scope_key: "deploy" })).toBe("Tasks of kind “deploy”");
  });

  it("summarises reviews and statuses", () => {
    expect(
      reviewSubject({ subject_agent_name: "Bot", task_title: null, mode: "pre_action", evidence_json: { tool_name: "github.merge" } }),
    ).toBe("Bot wants to use github merge");
    expect(reviewSubject({ subject_agent_name: "Bot", task_title: "Ship it", mode: "before_close", evidence_json: {} })).toBe(
      "Bot finished “Ship it” and needs a check",
    );
    expect(matchedConditionLabels({ matched_conditions: ["blocked", { kind: "always" }] })).toEqual(["The agent is stuck", "Every time"]);
    expect(workRequestStatus("clarification_requested").label).toBe("Asked for clarification");
    expect(rollupStatusText("waiting_approval")).toEqual({ label: "Waiting", tone: "warn" });
  });
});

describe("friendlyDetailLines", () => {
  it("summarises work-request detail without ids", () => {
    const lines = friendlyDetailLines({
      kind: "reported",
      work_request_id: "wr1",
      review_id: null,
      detail_json: { status: "completed", title: "Check", expected_output: "Yes/no", response: "Yes.", created_task_id: "t9", depth: 1 },
    });
    expect(lines).toEqual(["Status: Done", "Expected: Yes/no", "Reply: Yes.", "Accepted as a separate piece of work."]);
    expect(lines.join(" ")).not.toContain("t9");
  });

  it("summarises review detail", () => {
    expect(
      friendlyDetailLines({
        kind: "needs_review",
        review_id: "r1",
        work_request_id: null,
        detail_json: { mode: "pre_action", status: "pending", reviewer_type: "human", matched_conditions: ["destructive_action"], tool_name: "repo.delete" },
      }),
    ).toEqual(["When: Before a risky action", "Reviewer: a person", "Why: Destructive actions", "Action: repo delete"]);
  });

  it("is empty for ordinary cards", () => {
    expect(friendlyDetailLines({ kind: "started", detail_json: { message_type: "text" } })).toEqual([]);
  });
});
