/** Unit tests: plain-language agent helpers for the directory and profile. */

import { describe, expect, it } from "vitest";
import {
  describeGrant,
  firstSentence,
  matchesAvailability,
  matchesSearch,
  policySummary,
  purposeOf,
  relationshipLabel,
  statusTextOf,
  triggerWhen,
  type AgentLike,
} from "@/components/company/agent-helpers";
import type { AgentRelationship, Grant, ToolInfo } from "@/lib/types";

const agent: AgentLike = {
  id: "swe",
  name: "Senior SWE",
  role_title: "Senior Software Engineer",
  status: "active",
  description: "Builds backend services. Also reviews pull requests.",
  team_id: "eng",
  manager_agent_id: "cto",
  expertise_json: ["Python", "Postgres"],
};

describe("statusTextOf", () => {
  it("pairs every state with text", () => {
    expect(statusTextOf(agent)).toEqual({ label: "Available", tone: "ok" });
    expect(statusTextOf(agent, true)).toEqual({ label: "Working now", tone: "accent" });
    expect(statusTextOf({ ...agent, availability: "unavailable" })).toEqual({ label: "Away", tone: "warn" });
    expect(statusTextOf({ ...agent, status: "paused" }, true)).toEqual({ label: "Paused", tone: "warn" });
    expect(statusTextOf({ ...agent, status: "disabled" })).toEqual({ label: "Turned off", tone: "neutral" });
  });
});

describe("purposeOf and search", () => {
  it("prefers the public purpose and falls back to the first sentence", () => {
    expect(purposeOf(agent)).toBe("Builds backend services.");
    expect(purposeOf({ ...agent, public_purpose: "Keeps the API fast." })).toBe("Keeps the API fast.");
    expect(purposeOf({ description: "" })).toBe("");
  });
  it("searches name, role, and expertise case-insensitively", () => {
    expect(matchesSearch(agent, "postgres")).toBe(true);
    expect(matchesSearch(agent, "engineer")).toBe(true);
    expect(matchesSearch(agent, "senior swe")).toBe(true);
    expect(matchesSearch(agent, "marketing")).toBe(false);
    expect(matchesSearch(agent, "  ")).toBe(true);
  });
  it("filters by availability", () => {
    expect(matchesAvailability(agent, "available", false)).toBe(true);
    expect(matchesAvailability(agent, "working", false)).toBe(false);
    expect(matchesAvailability(agent, "working", true)).toBe(true);
    expect(matchesAvailability({ ...agent, status: "paused" }, "paused", false)).toBe(true);
    expect(matchesAvailability({ ...agent, status: "paused" }, "available", false)).toBe(false);
  });
});

describe("relationshipLabel", () => {
  const base: AgentRelationship = {
    id: "r1",
    workspace_id: "w",
    source_agent_id: "swe",
    target_agent_id: "cto",
    kind: "advisor",
    purpose: "",
    status: "active",
    created_at: "",
    updated_at: "",
  };
  it("words directed links from each side and collaborators symmetrically", () => {
    expect(relationshipLabel(base, "swe")).toBe("Gets advice from");
    expect(relationshipLabel(base, "cto")).toBe("Advises");
    expect(relationshipLabel({ ...base, kind: "preferred_reviewer" }, "swe")).toBe("Prefers reviews from");
    expect(relationshipLabel({ ...base, kind: "close_collaborator" }, "cto")).toBe("Works closely with");
  });
});

describe("firstSentence", () => {
  it("keeps only the sentence that describes the capability", () => {
    expect(
      firstSentence(
        'Find out what a colleague is doing right now. Pass their name (agent_name) — for example "what is the CTO working on?" Returns their availability.',
      ),
    ).toBe("Find out what a colleague is doing right now");
  });

  it("leaves abbreviations and a single sentence alone", () => {
    expect(firstSentence("Read a file, e.g. a config, from the workspace.")).toBe(
      "Read a file, e.g. a config, from the workspace",
    );
  });

  it("caps a long opening sentence", () => {
    expect(firstSentence("a".repeat(200)).length).toBe(140);
  });
});

describe("describeGrant and policySummary", () => {
  const tools: ToolInfo[] = [
    {
      name: "github.repository.read",
      description: "Read repository contents",
      risk: "read",
      required_capability: "github.repository.read",
      supports_approval: false,
      scope_keys: ["connection_id", "repository"],
      required_grant_scope_keys: ["connection_id"],
      input_schema: {},
    },
  ];
  const grant = (overrides: Partial<Grant>): Grant => ({
    id: "g1",
    agent_id: "swe",
    capability: "github.repository.read",
    scope_json: {},
    effect: "allow",
    created_at: "",
    ...overrides,
  });

  it("turns capability strings into plain language without leaking them", () => {
    const text = describeGrant(grant({ scope_json: { repository: "octo/*", connection_id: "c1" } }), tools, { c1: "GitHub main" });
    expect(text).toBe("GitHub: read repositories octo/* via GitHub main");
    expect(text).not.toContain("github.repository.read");
    expect(describeGrant(grant({ capability: "*" }), tools)).toBe("Everything (all tools)");
    expect(describeGrant(grant({ capability: "cli.command.execute", effect: "deny" }), tools)).toBe(
      "Command line: Not allowed to run commands",
    );
    expect(describeGrant(grant({ capability: "organization.delegate" }), tools)).toBe("Company: delegate work");
  });

  it("summarises presets in plain words", () => {
    expect(policySummary(undefined)).toBe("Asks before risky actions");
    expect(policySummary({ preset: "balanced", rules: [], autonomy_level: "supervised" })).toBe("Asks before risky actions");
    expect(policySummary({ preset: "restricted", rules: [], autonomy_level: "supervised" })).toMatch(/never runs destructive/);
    expect(
      policySummary({ preset: null, rules: [{ capability: "*", risk: "write", action: "approval" }], autonomy_level: "supervised" }),
    ).toBe("Custom rules; asks before some actions");
  });
});

describe("triggerWhen", () => {
  it("describes events in plain words", () => {
    expect(triggerWhen("connector.linear.issue.updated", undefined)).toBe("a Linear issue changes");
    expect(triggerWhen("connector.github.release.published", undefined)).toBe("release published in GitHub");
    expect(triggerWhen(null, "Acme GitHub")).toBe("anything happens in Acme GitHub");
  });
});
