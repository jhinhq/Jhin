/**
 * Pure helpers for tools, grants, and approval policies (plan 12, 42).
 * Kept free of React so the grants-tab logic is unit-testable.
 */

import type { ApprovalPreset, Grant, PolicyRule, RiskLevel, RuleAction } from "@/lib/types";

const RISK_TONES: Record<RiskLevel, "neutral" | "ok" | "warn" | "danger" | "accent"> = {
  read: "ok",
  write: "accent",
  elevated: "warn",
  destructive: "danger",
};

export function riskTone(risk: string | null | undefined) {
  return RISK_TONES[(risk ?? "") as RiskLevel] ?? "neutral";
}

/** Mirrors the server's preset expansion (jhin_policy.approvals.PRESET_RULES).
 * Shown in the UI so admins see exactly which rules a preset persists. */
export const PRESET_RULES: Record<ApprovalPreset, PolicyRule[]> = {
  autonomous: [
    { capability: "*", risk: "read", action: "auto" },
    { capability: "*", risk: "write", action: "auto" },
    { capability: "*", risk: "elevated", action: "auto" },
    { capability: "*", risk: "destructive", action: "approval" },
  ],
  balanced: [
    { capability: "*", risk: "read", action: "auto" },
    { capability: "*", risk: "write", action: "auto" },
    { capability: "*", risk: "elevated", action: "approval" },
    { capability: "*", risk: "destructive", action: "approval" },
  ],
  restricted: [
    { capability: "*", risk: "read", action: "auto" },
    { capability: "*", risk: "write", action: "approval" },
    { capability: "*", risk: "elevated", action: "approval" },
    { capability: "*", risk: "destructive", action: "forbid" },
  ],
};

/**
 * The rules a preset does not speak for: decisions about one capability, such
 * as the approval the Code-editing bundle keeps on `cli.repository.push`.
 *
 * A preset expands to risk-level rules only, so the server keeps these when
 * one is chosen (`jhin_policy.capability_rules`) and reports the preset as
 * selected regardless. The UI shows them separately for the same reason: they
 * did not come from the mode and do not leave with it.
 */
export function keptRules(rules: PolicyRule[]): PolicyRule[] {
  return rules.filter((rule) => rule.capability !== "*");
}

export const PRESET_DESCRIPTIONS: Record<ApprovalPreset, string> = {
  autonomous: "Runs read/write/elevated tools automatically; destructive actions still need a human.",
  balanced: "Runs read/write tools automatically; elevated and destructive actions need approval.",
  restricted: "Only reads run automatically; writes need approval; destructive actions are forbidden.",
};

const ACTION_LABELS: Record<RuleAction, string> = {
  auto: "runs automatically",
  approval: "needs approval",
  forbid: "forbidden",
};

export function describeRule(rule: PolicyRule): string {
  const target = rule.risk ? `${rule.risk}-risk` : "all";
  const scope = rule.capability === "*" ? "" : ` (${rule.capability})`;
  return `${target} calls${scope}: ${ACTION_LABELS[rule.action]}`;
}

export function formatScope(scope: Record<string, unknown>): string {
  const entries = Object.entries(scope);
  if (entries.length === 0) return "any scope";
  return entries.map(([key, value]) => `${key}=${String(value)}`).join(", ");
}

/** Sort grants for display: denies first (they win), then by capability. */
export function sortGrants(grants: Grant[]): Grant[] {
  return [...grants].sort((a, b) => {
    if (a.effect !== b.effect) return a.effect === "deny" ? -1 : 1;
    return a.capability.localeCompare(b.capability);
  });
}

/** Would this capability be covered by an existing grant with the same effect? */
export function grantCovers(pattern: string, capability: string): boolean {
  if (pattern === "*") return true;
  if (pattern.endsWith(".*")) return capability.startsWith(pattern.slice(0, -1));
  return pattern === capability;
}

export function isCapabilityGranted(grants: Grant[], capability: string): boolean {
  const denied = grants.some(
    (grant) => grant.effect === "deny" && grantCovers(grant.capability, capability),
  );
  if (denied) return false;
  return grants.some(
    (grant) => grant.effect === "allow" && grantCovers(grant.capability, capability),
  );
}
