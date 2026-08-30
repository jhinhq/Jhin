import { describe, expect, it } from "vitest";
import { impactSentence } from "@/components/connection-detail";

describe("deleting a connection", () => {
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
  });
});
