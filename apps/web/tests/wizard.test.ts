import { describe, expect, it } from "vitest";
import {
  AGENT_TEMPLATES,
  canSubmit,
  EMPTY_WIZARD,
  firstInvalidStep,
  toCreatePayload,
  toggleCapability,
  validateIdentity,
  validateStep,
  WIZARD_STEPS,
} from "@/lib/wizard";

describe("wizard validation", () => {
  it("requires a name in the identity step", () => {
    expect(validateIdentity(EMPTY_WIZARD)).toEqual(["Name is required."]);
    expect(validateIdentity({ ...EMPTY_WIZARD, name: "   " })).toEqual(["Name is required."]);
    expect(validateIdentity({ ...EMPTY_WIZARD, name: "CTO" })).toEqual([]);
  });

  it("rejects overlong names and role titles", () => {
    const long = "x".repeat(201);
    expect(validateIdentity({ ...EMPTY_WIZARD, name: long })).toContain(
      "Name must be at most 200 characters.",
    );
    expect(
      validateIdentity({ ...EMPTY_WIZARD, name: "ok", roleTitle: long }),
    ).toContain("Role title must be at most 200 characters.");
  });

  it("firstInvalidStep points at the identity step for an empty wizard", () => {
    expect(firstInvalidStep(EMPTY_WIZARD)).toBe(1);
    expect(canSubmit(EMPTY_WIZARD)).toBe(false);
  });

  it("a named agent can be submitted (other steps are optional)", () => {
    const state = { ...EMPTY_WIZARD, name: "QA Engineer" };
    expect(firstInvalidStep(state)).toBeNull();
    expect(canSubmit(state)).toBe(true);
  });

  it("disabled phase steps never validate-block", () => {
    for (const step of WIZARD_STEPS.filter((s) => s.disabledPhase)) {
      expect(validateStep(step.id, EMPTY_WIZARD)).toEqual([]);
    }
  });

  it("only step 7 (budget) remains stubbed — tools & autonomy are live in Phase 4", () => {
    const disabled = WIZARD_STEPS.filter((s) => s.disabledPhase).map((s) => s.id);
    expect(disabled).toEqual([7]);
  });
});

describe("wizard payload", () => {
  it("maps empty selects to nulls and trims text", () => {
    const payload = toCreatePayload({
      ...EMPTY_WIZARD,
      name: "  CTO  ",
      roleTitle: " Chief ",
      teamId: "",
      managerAgentId: "",
    });
    expect(payload).toMatchObject({
      name: "CTO",
      role_title: "Chief",
      team_id: null,
      manager_agent_id: null,
      model_profile_id: null,
    });
  });

  it("passes through team, manager, and model profile ids", () => {
    const payload = toCreatePayload({
      ...EMPTY_WIZARD,
      name: "SWE",
      teamId: "team-1",
      managerAgentId: "agent-1",
      modelProfileId: "profile-1",
    });
    expect(payload.team_id).toBe("team-1");
    expect(payload.manager_agent_id).toBe("agent-1");
    expect(payload.model_profile_id).toBe("profile-1");
  });

  it("carries the chosen autonomy level (defaults to supervised)", () => {
    expect(toCreatePayload({ ...EMPTY_WIZARD, name: "SWE" }).autonomy_level).toBe("supervised");
    expect(
      toCreatePayload({ ...EMPTY_WIZARD, name: "SWE", autonomyLevel: "manual" }).autonomy_level,
    ).toBe("manual");
  });
});

describe("wizard tool grants", () => {
  it("toggles capabilities on and off", () => {
    let state = { ...EMPTY_WIZARD, name: "SWE" };
    state = toggleCapability(state, "system.echo");
    state = toggleCapability(state, "system.time");
    expect(state.grantCapabilities).toEqual(["system.echo", "system.time"]);
    state = toggleCapability(state, "system.echo");
    expect(state.grantCapabilities).toEqual(["system.time"]);
  });

  it("starts with no grants (deny-by-default) and the balanced preset", () => {
    expect(EMPTY_WIZARD.grantCapabilities).toEqual([]);
    expect(EMPTY_WIZARD.approvalPreset).toBe("balanced");
  });
});

describe("templates", () => {
  it("ships the eight templates from the plan", () => {
    expect(AGENT_TEMPLATES.map((t) => t.name)).toEqual([
      "CTO",
      "Software Engineer",
      "QA Engineer",
      "DevOps",
      "Marketing Director",
      "Blogger",
      "SEO Specialist",
      "Generic Assistant",
    ]);
    for (const template of AGENT_TEMPLATES) {
      expect(template.systemPrompt.length).toBeGreaterThan(20);
      expect(template.roleTitle.length).toBeGreaterThan(0);
    }
  });
});
