import { describe, expect, it } from "vitest";
import {
  canReadSkills,
  isValidGithubRef,
  isValidSkillName,
  needsReviewCount,
  SOURCE_LABELS,
} from "@/lib/skills";
import type { Grant, Skill } from "@/lib/types";

function grant(capability: string, effect: "allow" | "deny" = "allow"): Grant {
  return {
    id: `g-${capability}-${effect}`,
    agent_id: "a1",
    capability,
    scope_json: {},
    effect,
    created_at: "2026-08-23T00:00:00Z",
  };
}

function skill(overrides: Partial<Skill> = {}): Skill {
  return {
    id: "s1",
    workspace_id: "w1",
    name: "release-notes",
    description: "Write release notes.",
    source: "custom",
    source_url: "",
    enabled: true,
    version: 1,
    file_count: 0,
    created_by_agent_id: null,
    created_at: "2026-08-23T00:00:00Z",
    updated_at: "2026-08-23T00:00:00Z",
    ...overrides,
  };
}

describe("isValidGithubRef", () => {
  it("accepts owner/repo and owner/repo/path", () => {
    expect(isValidGithubRef("anthropics/skills")).toBe(true);
    expect(isValidGithubRef("anthropics/skills/document-skills")).toBe(true);
    expect(isValidGithubRef("  anthropics/skills/  ")).toBe(true);
  });

  it("rejects malformed references", () => {
    expect(isValidGithubRef("")).toBe(false);
    expect(isValidGithubRef("justonepart")).toBe(false);
    expect(isValidGithubRef("owner/repo/../up")).toBe(false);
    expect(isValidGithubRef("own er/repo")).toBe(false);
  });
});

describe("isValidSkillName", () => {
  it("mirrors the API slug rule", () => {
    expect(isValidSkillName("release-notes")).toBe(true);
    expect(isValidSkillName("a")).toBe(true);
    expect(isValidSkillName("Has Space")).toBe(false);
    expect(isValidSkillName("-leading")).toBe(false);
    expect(isValidSkillName("UPPER")).toBe(false);
    expect(isValidSkillName("a".repeat(65))).toBe(false);
  });
});

describe("canReadSkills", () => {
  it("recognizes exact, subtree, and wildcard allow grants", () => {
    expect(canReadSkills([grant("skills.read")])).toBe(true);
    expect(canReadSkills([grant("skills.*")])).toBe(true);
    expect(canReadSkills([grant("*")])).toBe(true);
  });

  it("ignores unrelated or deny grants", () => {
    expect(canReadSkills([])).toBe(false);
    expect(canReadSkills([grant("memory.read")])).toBe(false);
    expect(canReadSkills([grant("skills.read", "deny")])).toBe(false);
  });
});

describe("needsReviewCount", () => {
  it("counts only disabled imports", () => {
    expect(
      needsReviewCount([
        skill(),
        skill({ id: "s2", source: "imported", enabled: false }),
        skill({ id: "s3", source: "imported", enabled: true }),
        skill({ id: "s4", source: "built_in", enabled: false }),
      ]),
    ).toBe(1);
  });
});

describe("SOURCE_LABELS", () => {
  it("has a friendly label for every source", () => {
    expect(SOURCE_LABELS.built_in).toBe("Starter");
    expect(SOURCE_LABELS.imported).toBe("Imported");
    expect(SOURCE_LABELS.custom).toBe("Custom");
  });
});
