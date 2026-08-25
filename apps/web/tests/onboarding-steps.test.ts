/** What the guided introduction offers, and to whom.
 *
 * The step list is pure data, so the rules that matter — role, dependency
 * order, and not asking twice for something already done — are testable
 * without rendering anything. */

import { describe, expect, it } from "vitest";
import {
  buildOnboardingSteps,
  startingStepIndex,
  stepProgress,
  type OnboardingFacts,
} from "@/components/onboarding/steps";

function facts(overrides: Partial<OnboardingFacts> = {}): OnboardingFacts {
  return {
    workspaceName: "Acme",
    canConfigure: true,
    canChat: true,
    hasModel: false,
    hasAgents: false,
    hasChats: false,
    hasApps: false,
    ...overrides,
  };
}

const ids = (input: OnboardingFacts) => buildOnboardingSteps(input).map((step) => step.id);

describe("who sees what", () => {
  it("walks an admin from a model through to the rest of the product", () => {
    expect(ids(facts())).toEqual([
      "welcome",
      "model",
      "agent",
      "chat",
      "apps",
      "teamwork",
      "explore",
    ]);
  });

  it("never asks a member to do the setup they are not allowed to do", () => {
    const member = ids(facts({ canConfigure: false }));
    expect(member).toEqual(["welcome", "chat", "teamwork", "explore"]);
    expect(member).not.toContain("model");
    expect(member).not.toContain("apps");
  });

  it("orients a member to chats, teamwork, and where decisions land", () => {
    const steps = buildOnboardingSteps(facts({ canConfigure: false, hasAgents: true }));
    const chat = steps.find((step) => step.id === "chat");
    expect(chat?.action?.href).toBe("/chats");
    const teamwork = steps.find((step) => step.id === "teamwork");
    expect(teamwork?.highlights?.map((item) => item.href)).toEqual([
      "/company",
      "/activity",
      "/attention",
    ]);
  });

  it("gives a member different words from an admin, not just fewer steps", () => {
    const admin = buildOnboardingSteps(facts())[0];
    const member = buildOnboardingSteps(facts({ canConfigure: false }))[0];
    expect(member.title).toBe(admin.title);
    expect(member.body).not.toBe(admin.body);
  });

  it("does not invite a viewer to start a chat they cannot start", () => {
    const steps = buildOnboardingSteps(
      facts({ canConfigure: false, canChat: false, hasAgents: true }),
    );
    const chat = steps.find((step) => step.id === "chat");
    expect(chat?.action).toBeUndefined();
    expect(chat?.highlights?.length).toBeGreaterThan(0);
  });
});

describe("honesty about prerequisites", () => {
  it("does not send anyone to create an agent that could not run", () => {
    const steps = buildOnboardingSteps(facts());
    const agent = steps.find((step) => step.id === "agent");
    expect(agent?.blocked).toMatch(/model/i);
  });

  it("clears the way once a model exists", () => {
    const steps = buildOnboardingSteps(facts({ hasModel: true }));
    expect(steps.find((step) => step.id === "agent")?.blocked).toBeUndefined();
  });

  it("holds the first chat back until somebody has been hired", () => {
    const blocked = buildOnboardingSteps(facts({ hasModel: true }));
    expect(blocked.find((step) => step.id === "chat")?.blocked).toMatch(/teammate/i);
    const ready = buildOnboardingSteps(facts({ hasModel: true, hasAgents: true }));
    expect(ready.find((step) => step.id === "chat")?.blocked).toBeUndefined();
  });

  it("tells a member to wait for an admin rather than to fix it themselves", () => {
    const steps = buildOnboardingSteps(facts({ canConfigure: false }));
    expect(steps.find((step) => step.id === "chat")?.blocked).toMatch(/admin/i);
  });
});

describe("steps already satisfied", () => {
  it("marks the model and agent steps done from real state", () => {
    const steps = buildOnboardingSteps(facts({ hasModel: true, hasAgents: true }));
    expect(steps.find((step) => step.id === "model")?.done).toBe(true);
    expect(steps.find((step) => step.id === "agent")?.done).toBe(true);
    expect(steps.find((step) => step.id === "apps")?.done).toBe(false);
  });

  it("changes a done step from an instruction into a way back in", () => {
    const before = buildOnboardingSteps(facts()).find((step) => step.id === "model");
    const after = buildOnboardingSteps(facts({ hasModel: true })).find(
      (step) => step.id === "model",
    );
    expect(before?.action?.label).toMatch(/set up/i);
    expect(after?.action?.label).toMatch(/review/i);
    expect(after?.body).not.toBe(before?.body);
  });

  it("counts only the steps that can actually be finished", () => {
    expect(stepProgress(buildOnboardingSteps(facts()))).toEqual({ done: 0, total: 4 });
    expect(
      stepProgress(buildOnboardingSteps(facts({ hasModel: true, hasAgents: true }))),
    ).toEqual({ done: 2, total: 4 });
  });
});

describe("where the tour opens", () => {
  it("starts at the beginning for someone who has never seen it", () => {
    const steps = buildOnboardingSteps(facts({ hasModel: true }));
    expect(startingStepIndex(steps, true)).toBe(0);
  });

  it("resumes on the first thing still outstanding", () => {
    const steps = buildOnboardingSteps(facts({ hasModel: true }));
    expect(steps[startingStepIndex(steps, false)].id).toBe("agent");
  });

  it("leaves the optional step for last", () => {
    const steps = buildOnboardingSteps(
      facts({ hasModel: true, hasAgents: true, hasChats: true }),
    );
    expect(steps[startingStepIndex(steps, false)].id).toBe("apps");
  });

  it("falls back to the start when nothing is outstanding", () => {
    const steps = buildOnboardingSteps(
      facts({ hasModel: true, hasAgents: true, hasChats: true, hasApps: true }),
    );
    expect(startingStepIndex(steps, false)).toBe(0);
  });
});
