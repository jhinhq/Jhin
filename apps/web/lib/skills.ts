/** Pure helpers for the Skills library page and the agent Skills tab
 * (docs/architecture/skills.md). React-free and unit-tested. */

import { grantCovers } from "@/lib/policy";
import type { Grant, Skill, SkillSource } from "@/lib/types";

export const SOURCE_LABELS: Record<SkillSource, string> = {
  built_in: "Starter",
  imported: "Imported",
  custom: "Custom",
  agent_authored: "Agent-authored",
};

/** Every creation path defaults an unset category to this (docs/architecture/skills.md). */
export const DEFAULT_CATEGORY = "General";

/** The distinct categories present, "General" sorted last, everything else
 * alphabetically — for populating a filter chip row or select. */
export function categoriesOf(items: { category: string }[]): string[] {
  const seen = new Set(items.map((item) => item.category || DEFAULT_CATEGORY));
  const rest = [...seen].filter((category) => category !== DEFAULT_CATEGORY).sort();
  return seen.has(DEFAULT_CATEGORY) ? [...rest, DEFAULT_CATEGORY] : rest;
}

/** Group items by category — "General" sorted last, everything else
 * alphabetically — for the library page's collapsible sections. */
export function groupByCategory<T extends { category: string }>(items: T[]): [string, T[]][] {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const key = item.category || DEFAULT_CATEGORY;
    const bucket = groups.get(key);
    if (bucket) bucket.push(item);
    else groups.set(key, [item]);
  }
  return [...groups.entries()].sort(([a], [b]) => {
    if (a === b) return 0;
    if (a === DEFAULT_CATEGORY) return 1;
    if (b === DEFAULT_CATEGORY) return -1;
    return a.localeCompare(b);
  });
}

const GITHUB_REF_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(\/[A-Za-z0-9_./-]+)?$/;
const SKILL_NAME_RE = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;

/** Client-side mirror of the API's `owner/repo[/path]` validation. */
export function isValidGithubRef(ref: string): boolean {
  const trimmed = ref.trim().replace(/^\/+|\/+$/g, "");
  if (!GITHUB_REF_RE.test(trimmed)) return false;
  return !trimmed.split("/").some((part) => part === "..");
}

/** Client-side mirror of the skill name slug rule. */
export function isValidSkillName(name: string): boolean {
  return SKILL_NAME_RE.test(name);
}

/** Whether any allow grant lets the agent call the skills.read tool. */
export function canReadSkills(grants: Grant[]): boolean {
  return grants.some(
    (grant) => grant.effect === "allow" && grantCovers(grant.capability, "skills.read"),
  );
}

/** Imported skills still waiting for an admin to review and enable. */
export function needsReviewCount(skills: Skill[]): number {
  return skills.filter((skill) => skill.source === "imported" && !skill.enabled).length;
}
