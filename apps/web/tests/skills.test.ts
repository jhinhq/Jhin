import { describe, expect, it } from "vitest";
import {
  canReadSkills,
  categoriesOf,
  DEFAULT_CATEGORY,
  groupByCategory,
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
    category: DEFAULT_CATEGORY,
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

describe("categoriesOf", () => {
  it("sorts categories alphabetically with General last", () => {
    expect(
      categoriesOf([
        skill({ category: "Engineering" }),
        skill({ category: "Communication" }),
        skill({ category: DEFAULT_CATEGORY }),
        skill({ category: "Engineering" }),
      ]),
    ).toEqual(["Communication", "Engineering", DEFAULT_CATEGORY]);
  });

  it("omits General entirely when nothing is uncategorized", () => {
    expect(categoriesOf([skill({ category: "Support" })])).toEqual(["Support"]);
  });

  it("is empty for an empty list", () => {
    expect(categoriesOf([])).toEqual([]);
  });
});

describe("groupByCategory", () => {
  it("groups items and sorts General last", () => {
    const groups = groupByCategory([
      skill({ id: "s1", category: DEFAULT_CATEGORY }),
      skill({ id: "s2", category: "Engineering" }),
      skill({ id: "s3", category: "Communication" }),
      skill({ id: "s4", category: "Engineering" }),
    ]);
    expect(groups.map(([category]) => category)).toEqual([
      "Communication",
      "Engineering",
      DEFAULT_CATEGORY,
    ]);
    const engineering = groups.find(([category]) => category === "Engineering");
    expect(engineering?.[1].map((item) => item.id)).toEqual(["s2", "s4"]);
  });

  it("treats a blank category as General", () => {
    const groups = groupByCategory([skill({ category: "" })]);
    expect(groups).toEqual([[DEFAULT_CATEGORY, [skill({ category: "" })]]]);
  });
});
