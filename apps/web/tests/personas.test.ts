/** Pure persona helpers: the rendered block (character for character what
 * the agent reads), the caps, filters, the editor draft, and how an API
 * failure lands under a field. */

import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import {
  agentCountText,
  boundedFacet,
  collapseWhitespace,
  draftFrom,
  EMPTY_FACETS,
  facetChars,
  fieldErrorsFrom,
  filterPersonas,
  isValidPersonaName,
  isValidPersonaTag,
  parseTags,
  PERSONA_GUARDRAIL,
  pickablePersonas,
  renderPersonaBlock,
  toCreateInput,
  toUpdateInput,
  validatePersonaForm,
  type PersonaDraft,
} from "@/lib/personas";
import { persona } from "./helpers/personas";

const HEADER = [
  "How you work — Mission Control",
  "This shapes how you say things, never what you may do: tool policy, approvals, safety rules, and your manager's instructions always win.",
];
const VOICE =
  "- Voice: Level, measured, unflappable, with the clipped cadence of a flight director on the loop. Nothing rattles it: a problem is a call to make, not a crisis.";
const STANCE =
  "- Stance: States the call and the reason for it in one breath. Disagreement is a poll of the room: hears the objection, weighs it against the data, makes the call, and says so.";
const PACE =
  "- Pace: Short bursts by default: status, next step, when. Goes long only for a go/no-go decision, and then walks through each system in order.";
const WHEN_UNSURE =
  "- When unsure: Names the unknown and what would resolve it. Holds rather than guesses: asks the person one precise question and works the parts that do not depend on the answer.";
const WITH_PEOPLE =
  "- With people: Calm and clear: what is happening, what it means, what happens next. Translates the loop chatter into plain words for the person it serves.";
const WITH_TEAMMATES =
  "- With teammates: Runs the loop with colleagues: addresses them by role, asks for a status in one line, confirms what it heard. Tight, courteous, no crosstalk.";
const SIGNATURE =
  "- Signature: Opens with 'Flight here:' and the status in one line; closes with the next call and when it is due.";
const NEVER =
  "- Never: Raise its voice, even in text; Report a status it has not confirmed; Wait for a heroic fix instead of naming the problem; Trade the calm for a sense of urgency";

describe("renderPersonaBlock", () => {
  const card = persona();

  it("matches jhin_agents.context.persona_block for a person on the other side", () => {
    expect(renderPersonaBlock(card, "person")).toBe(
      [...HEADER, VOICE, STANCE, PACE, WHEN_UNSURE, WITH_PEOPLE, SIGNATURE, NEVER].join("\n"),
    );
    expect(PERSONA_GUARDRAIL).toBe(HEADER[1]);
  });

  it("swaps the register line for a teammate", () => {
    expect(renderPersonaBlock(card, "teammate")).toBe(
      [...HEADER, VOICE, STANCE, PACE, WHEN_UNSURE, WITH_TEAMMATES, SIGNATURE, NEVER].join("\n"),
    );
  });

  it("shows both registers in the web preview and neither for a run with nobody there", () => {
    expect(renderPersonaBlock(card, "both")).toBe(
      [...HEADER, VOICE, STANCE, PACE, WHEN_UNSURE, WITH_PEOPLE, WITH_TEAMMATES, SIGNATURE, NEVER].join(
        "\n",
      ),
    );
    expect(renderPersonaBlock(card, "none")).toBe(
      [...HEADER, VOICE, STANCE, PACE, WHEN_UNSURE, SIGNATURE, NEVER].join("\n"),
    );
  });

  it("omits empty facets and an empty never list", () => {
    const sparse = persona({ facets: { ...EMPTY_FACETS, voice: "Plain and warm." } });
    expect(renderPersonaBlock(sparse, "both")).toBe([...HEADER, "- Voice: Plain and warm."].join("\n"));
  });

  it("re-applies the caps at render time", () => {
    const long = "x".repeat(300);
    const capped = persona({
      facets: { ...EMPTY_FACETS, voice: long, never: Array.from({ length: 8 }, (_, i) => `item ${i}`) },
    });
    const lines = renderPersonaBlock(capped, "none").split("\n");
    expect(lines[2]).toBe(`- Voice: ${"x".repeat(239)}…`);
    expect(lines[3]).toBe("- Never: item 0; item 1; item 2; item 3; item 4; item 5");
    expect(boundedFacet("y".repeat(121), 120)).toBe(`${"y".repeat(119)}…`);
    expect(boundedFacet("  a   b  ", 120)).toBe("a b");
  });

  it("falls back to the slug when the display name is blank", () => {
    const unnamed = persona({ display_name: "   " });
    expect(renderPersonaBlock(unnamed, "none").split("\n")[0]).toBe("How you work — mission-control");
  });
});

describe("caps and slugs", () => {
  it("collapses whitespace the way Python's split/join does", () => {
    expect(collapseWhitespace("  Level,\n\tmeasured   and calm ")).toBe("Level, measured and calm");
    expect(collapseWhitespace("   ")).toBe("");
  });

  it("counts the seven facets plus every never item", () => {
    expect(facetChars(EMPTY_FACETS)).toBe(0);
    expect(facetChars({ ...EMPTY_FACETS, voice: "ab  c", never: ["de", " f "] })).toBe(4 + 2 + 1);
    expect(facetChars(persona().facets)).toBe(
      Object.entries(persona().facets)
        .filter(([key]) => key !== "never")
        .reduce((sum, [, value]) => sum + (value as string).length, 0) +
        persona().facets.never.reduce((sum, item) => sum + item.length, 0),
    );
  });

  it("parses tags: split, trim, lowercase, dedupe, order kept", () => {
    expect(parseTags(" Fun, calm,,OPERATIONS calm\nfun ")).toEqual(["fun", "calm", "operations"]);
    expect(parseTags("")).toEqual([]);
  });

  it("validates names and tags like the API", () => {
    expect(isValidPersonaName("house-style")).toBe(true);
    expect(isValidPersonaName("a")).toBe(true);
    expect(isValidPersonaName("-nope")).toBe(false);
    expect(isValidPersonaName("Nope")).toBe(false);
    expect(isValidPersonaName("a".repeat(64))).toBe(true);
    expect(isValidPersonaName("a".repeat(65))).toBe(false);
    expect(isValidPersonaTag("fun")).toBe(true);
    expect(isValidPersonaTag("-x")).toBe(false);
    expect(isValidPersonaTag("a".repeat(32))).toBe(true);
    expect(isValidPersonaTag("a".repeat(33))).toBe(false);
  });

  it("says who wears a card in plain words", () => {
    expect(agentCountText(0)).toBe("Nobody wears this yet");
    expect(agentCountText(1)).toBe("Worn by 1 agent");
    expect(agentCountText(3)).toBe("Worn by 3 agents");
  });
});

describe("filters", () => {
  const items = [
    persona(),
    persona({
      id: "p2",
      name: "the-straight-shooter",
      display_name: "The Straight Shooter",
      description: "Answer first, reasons second.",
      tags: ["professional", "direct"],
      source: "custom",
      read_only: false,
      agent_count: 0,
    }),
    persona({
      id: "p3",
      name: "night-owl",
      display_name: "Night Owl",
      description: "Quiet and late.",
      tags: [],
      source: "agent",
      read_only: false,
      enabled: false,
    }),
  ];
  const all = { query: "", funOnly: false, source: "" as const, showDisabled: true };

  it("matches the query against name, display name, and description", () => {
    expect(filterPersonas(items, { ...all, query: "REASONS" }).map((p) => p.id)).toEqual(["p2"]);
    expect(filterPersonas(items, { ...all, query: "owl" }).map((p) => p.id)).toEqual(["p3"]);
    expect(filterPersonas(items, { ...all, query: "mission" }).map((p) => p.id)).toEqual(["p1"]);
  });

  it("narrows by fun, source, and switched-off", () => {
    expect(filterPersonas(items, { ...all, funOnly: true }).map((p) => p.id)).toEqual(["p1"]);
    expect(filterPersonas(items, { ...all, source: "agent" }).map((p) => p.id)).toEqual(["p3"]);
    expect(filterPersonas(items, { ...all, showDisabled: false }).map((p) => p.id)).toEqual(["p1", "p2"]);
  });

  it("offers pickers only switched-on cards", () => {
    expect(pickablePersonas(items, "", false).map((p) => p.id)).toEqual(["p1", "p2"]);
    expect(pickablePersonas(items, "", true).map((p) => p.id)).toEqual(["p1"]);
    expect(pickablePersonas(items, "straight", false).map((p) => p.id)).toEqual(["p2"]);
  });
});

function validDraft(overrides: Partial<PersonaDraft> = {}): PersonaDraft {
  return {
    name: "house-style",
    display_name: "House Style",
    description: "How we sound.",
    tagsInput: "professional, direct",
    facets: { ...EMPTY_FACETS, voice: "Plain and warm.", never: ["Ramble", ""] },
    ...overrides,
  };
}

describe("editor draft", () => {
  it("starts empty with one blank never row, and mirrors an existing card", () => {
    const empty = draftFrom(null);
    expect(empty).toEqual({
      name: "",
      display_name: "",
      description: "",
      tagsInput: "",
      facets: { ...EMPTY_FACETS, never: [""] },
    });
    const existing = draftFrom(persona());
    expect(existing.name).toBe("mission-control");
    expect(existing.tagsInput).toBe("fun, calm, operations");
    expect(existing.facets.never).toEqual(persona().facets.never);
  });

  it("builds the create body: trimmed, collapsed, blank never rows dropped", () => {
    expect(
      toCreateInput(validDraft({ display_name: "  House   Style ", tagsInput: "Fun, fun, direct" })),
    ).toEqual({
      name: "house-style",
      display_name: "House Style",
      description: "How we sound.",
      tags: ["fun", "direct"],
      facets: { ...EMPTY_FACETS, voice: "Plain and warm.", never: ["Ramble"] },
    });
  });

  it("builds the update body without the name or the switch", () => {
    const body = toUpdateInput(validDraft());
    expect(Object.keys(body).sort()).toEqual(["description", "display_name", "facets", "tags"]);
    expect(body.facets?.never).toEqual(["Ramble"]);
  });
});

describe("validatePersonaForm", () => {
  it("passes a complete draft", () => {
    expect(validatePersonaForm(validDraft(), true)).toEqual({});
  });

  it("checks the name only when creating", () => {
    expect(validatePersonaForm(validDraft({ name: "" }), true).name).toBe("Give the persona a name.");
    expect(validatePersonaForm(validDraft({ name: "Bad Name" }), true).name).toBe(
      "Use lowercase letters, digits, and hyphens (up to 64 characters).",
    );
    expect(validatePersonaForm(validDraft({ name: "a".repeat(65) }), true).name).toBe(
      "Use lowercase letters, digits, and hyphens (up to 64 characters).",
    );
    expect(validatePersonaForm(validDraft({ name: "" }), false).name).toBeUndefined();
  });

  it("checks the display name and description", () => {
    expect(validatePersonaForm(validDraft({ display_name: " " }), true).display_name).toBe(
      "Give it a display name.",
    );
    expect(validatePersonaForm(validDraft({ display_name: "x".repeat(81) }), true).display_name).toBe(
      "Keep the display name to 80 characters.",
    );
    expect(validatePersonaForm(validDraft({ description: "" }), true).description).toBe(
      "Add a one-line description.",
    );
    expect(validatePersonaForm(validDraft({ description: "x".repeat(201) }), true).description).toBe(
      "Keep the description to 200 characters.",
    );
  });

  it("checks the tags", () => {
    const nine = Array.from({ length: 9 }, (_, i) => `t${i}`).join(", ");
    expect(validatePersonaForm(validDraft({ tagsInput: nine }), true).tags).toBe("Up to 8 tags.");
    expect(validatePersonaForm(validDraft({ tagsInput: "fun, -bad" }), true).tags).toBe(
      "Tags use lowercase letters, digits, and hyphens (up to 32 characters): “-bad”.",
    );
  });

  it("requires voice and caps every facet", () => {
    expect(validatePersonaForm(validDraft({ facets: { ...EMPTY_FACETS } }), true).voice).toBe(
      "Voice is the one facet every persona needs.",
    );
    const long = "x".repeat(241);
    expect(
      validatePersonaForm(validDraft({ facets: { ...EMPTY_FACETS, voice: long } }), true).voice,
    ).toBe("Keep Voice to 240 characters.");
    expect(
      validatePersonaForm(
        validDraft({ facets: { ...EMPTY_FACETS, voice: "ok", when_unsure: long } }),
        true,
      ).when_unsure,
    ).toBe("Keep When unsure to 240 characters.");
  });

  it("checks the never list", () => {
    const facets = validDraft().facets;
    expect(
      validatePersonaForm(
        validDraft({ facets: { ...facets, never: Array.from({ length: 7 }, (_, i) => `n${i}`) } }),
        true,
      ).never,
    ).toBe("Up to six never items.");
    expect(
      validatePersonaForm(validDraft({ facets: { ...facets, never: ["x".repeat(121)] } }), true).never,
    ).toBe("Keep each never item to 120 characters.");
    expect(
      validatePersonaForm(validDraft({ facets: { ...facets, never: ["Ramble", "ramble"] } }), true)
        .never,
    ).toBe("Never items must be distinct — “ramble” repeats.");
    // Blank rows are the editor's, not items.
    expect(
      validatePersonaForm(validDraft({ facets: { ...facets, never: ["", "", "", "", "", "", ""] } }), true)
        .never,
    ).toBeUndefined();
  });

  it("caps the card as a whole", () => {
    const facets = {
      ...EMPTY_FACETS,
      voice: "v".repeat(240),
      stance: "s".repeat(240),
      pace: "p".repeat(240),
      when_unsure: "w".repeat(240),
      with_people: "h".repeat(240),
      with_teammates: "t".repeat(240),
      signature: "g".repeat(240),
      never: [],
    };
    expect(validatePersonaForm(validDraft({ facets }), true).facets).toBe(
      "The card runs to 1680 characters across its facets; the limit is 1,500.",
    );
  });
});

describe("fieldErrorsFrom", () => {
  it("puts a FastAPI 422 item under the facet it names, without the pydantic prefix", () => {
    const error = new ApiError(422, "facets.voice: Value error, voice must not name a tool", null, [
      {
        loc: ["body", "facets", "voice"],
        msg: "Value error, voice must not name a tool; a persona shapes how an agent sounds, not what it calls ('skills.read')",
        type: "value_error",
      },
      { loc: ["body", "facets"], msg: "Value error, the card runs to 1600 characters", type: "value_error" },
      { loc: ["body", "display_name"], msg: "String should have at most 80 characters", type: "string_too_long" },
      { loc: ["body", "tags", 2], msg: "Value error, tag '-x' is invalid", type: "value_error" },
      { loc: ["body", "elsewhere"], msg: "Assertion failed, something else", type: "assertion_error" },
    ]);
    expect(fieldErrorsFrom(error, "Saving failed.")).toEqual({
      fields: {
        voice: "voice must not name a tool; a persona shapes how an agent sounds, not what it calls ('skills.read')",
        facets: "the card runs to 1600 characters",
        display_name: "String should have at most 80 characters",
        tags: "tag '-x' is invalid",
      },
      general: "something else",
    });
  });

  it("reads the dotted detail string when the item list is missing", () => {
    expect(
      fieldErrorsFrom(new ApiError(422, "facets.never: Value error, never items must be distinct"), "x"),
    ).toEqual({ fields: { never: "never items must be distinct" }, general: null });
    expect(fieldErrorsFrom(new ApiError(422, "name: use lowercase letters"), "x")).toEqual({
      fields: { name: "use lowercase letters" },
      general: null,
    });
  });

  it("puts a taken name under the name field", () => {
    expect(
      fieldErrorsFrom(new ApiError(409, "a persona named 'house-style' already exists"), "x"),
    ).toEqual({ fields: { name: "a persona named 'house-style' already exists" }, general: null });
  });

  it("falls back to the detail, then to the caller's words", () => {
    expect(fieldErrorsFrom(new ApiError(403, "Admins only"), "Saving failed.")).toEqual({
      fields: {},
      general: "Admins only",
    });
    expect(fieldErrorsFrom(new ApiError(500, ""), "Saving failed.")).toEqual({
      fields: {},
      general: "Saving failed.",
    });
    expect(fieldErrorsFrom(new Error("boom"), "Saving failed.")).toEqual({
      fields: {},
      general: "Saving failed.",
    });
  });
});
