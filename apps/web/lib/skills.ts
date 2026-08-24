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
