import { describe, expect, it } from "vitest";
import { deletePrompt, impactSentence } from "@/components/connection-detail";

describe("deleting a connection", () => {
  it("names the automations and history the delete takes with it", () => {
    const prompt = deletePrompt("Linear", { trigger_count: 2, trigger_invocation_count: 41 });

    expect(prompt).toContain("Linear");
    expect(prompt).toContain("2 automations");
    expect(prompt).toContain("41 recorded runs");
    expect(prompt).toContain("cannot be undone");
  });

  it("says it in the singular for a single automation", () => {
    expect(impactSentence({ trigger_count: 1, trigger_invocation_count: 1 })).toBe(
      "Deleting also removes 1 automation built on this app, and their 1 recorded run.",
    );
  });

  it("leaves out run history an automation never accumulated", () => {
    const sentence = impactSentence({ trigger_count: 1, trigger_invocation_count: 0 });

    expect(sentence).toBe("Deleting also removes 1 automation built on this app.");
  });

  it("says nothing extra when nothing depends on the connection", () => {
    expect(impactSentence({ trigger_count: 0, trigger_invocation_count: 0 })).toBeNull();
    expect(impactSentence(undefined)).toBeNull();
    expect(deletePrompt("Linear", undefined)).not.toContain("automation");
  });
});
