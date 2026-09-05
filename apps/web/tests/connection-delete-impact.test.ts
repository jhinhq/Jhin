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
    expect(impactSentence({ trigger_count: 0, trigger_invocation_count: 0, grant_count: 0, agent_count: 0 })).toBeNull();
    expect(impactSentence(undefined)).toBeNull();
  });

  it("names the grants that go with the connection, and whose they are", () => {
    expect(
      impactSentence({ trigger_count: 0, trigger_invocation_count: 0, grant_count: 4, agent_count: 1 }),
    ).toBe("Deleting also revokes 4 grants on 1 agent.");
    expect(
      impactSentence({ trigger_count: 1, trigger_invocation_count: 0, grant_count: 1, agent_count: 1 }),
    ).toBe("Deleting also removes 1 automation built on this app and revoke 1 grant on 1 agent.");
    expect(
      impactSentence({ trigger_count: 2, trigger_invocation_count: 3, grant_count: 11, agent_count: 2 }),
    ).toBe(
      "Deleting also removes 2 automations built on this app, and their 3 recorded runs and revoke 11 grants on 2 agents.",
    );
  });
});
