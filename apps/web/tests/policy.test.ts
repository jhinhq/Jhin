/** Grants-tab and approval-policy logic (plan 12.3, 42). */

import { describe, expect, it } from "vitest";
import {
  describeRule,
  formatScope,
  grantCovers,
  isCapabilityGranted,
  keptRules,
  PRESET_RULES,
  riskTone,
  sortGrants,
} from "@/lib/policy";
import type { Grant } from "@/lib/types";

function grant(overrides: Partial<Grant>): Grant {
  return {
    id: "g1",
    agent_id: "a1",
    capability: "system.echo",
    scope_json: {},
    effect: "allow",
    created_at: "2026-08-16T00:00:00Z",
    ...overrides,
  };
}

describe("grant pattern matching", () => {
  it("matches exact capabilities", () => {
    expect(grantCovers("system.echo", "system.echo")).toBe(true);
    expect(grantCovers("system.echo", "system.time")).toBe(false);
  });

  it("matches wildcard patterns", () => {
    expect(grantCovers("system.*", "system.echo")).toBe(true);
    expect(grantCovers("system.*", "system.note.append")).toBe(true);
    expect(grantCovers("system.*", "github.repo.read")).toBe(false);
    expect(grantCovers("*", "anything.at.all")).toBe(true);
  });

  it("deny-by-default: nothing granted without an allow", () => {
    expect(isCapabilityGranted([], "system.echo")).toBe(false);
  });

  it("explicit deny beats allow", () => {
    const grants = [
      grant({ id: "g1", capability: "system.*", effect: "allow" }),
      grant({ id: "g2", capability: "system.echo", effect: "deny" }),
    ];
    expect(isCapabilityGranted(grants, "system.echo")).toBe(false);
    expect(isCapabilityGranted(grants, "system.time")).toBe(true);
  });
});

describe("grant display", () => {
  it("sorts denies first, then by capability", () => {
    const sorted = sortGrants([
      grant({ id: "1", capability: "system.time", effect: "allow" }),
      grant({ id: "2", capability: "system.echo", effect: "allow" }),
      grant({ id: "3", capability: "system.demo.destructive", effect: "deny" }),
    ]);
    expect(sorted.map((g) => g.capability)).toEqual([
      "system.demo.destructive",
      "system.echo",
      "system.time",
    ]);
  });

  it("formats scopes as key=value pairs", () => {
    expect(formatScope({})).toBe("any scope");
    expect(formatScope({ repository: "jhin", branch: "main" })).toBe(
      "repository=jhin, branch=main",
    );
  });

  it("maps risk levels to badge tones", () => {
    expect(riskTone("read")).toBe("ok");
    expect(riskTone("destructive")).toBe("danger");
    expect(riskTone("unknown")).toBe("neutral");
    expect(riskTone(null)).toBe("neutral");
  });
});

describe("approval presets", () => {
  it("restricted forbids destructive actions", () => {
    const destructive = PRESET_RULES.restricted.find((rule) => rule.risk === "destructive");
    expect(destructive?.action).toBe("forbid");
  });

  it("describes rules in plain language", () => {
    expect(describeRule({ capability: "*", risk: "elevated", action: "approval" })).toBe(
      "elevated-risk calls: needs approval",
    );
    expect(describeRule({ capability: "system.echo", risk: null, action: "auto" })).toBe(
      "all calls (system.echo): runs automatically",
    );
  });
});

describe("rules a preset does not speak for", () => {
  const gate = { capability: "cli.repository.push", risk: null, action: "approval" } as const;

  it("separates per-capability rules from the preset's risk rules", () => {
    expect(keptRules([gate, ...PRESET_RULES.autonomous])).toEqual([gate]);
    expect(keptRules(PRESET_RULES.autonomous)).toEqual([]);
    expect(keptRules([])).toEqual([]);
  });
});
