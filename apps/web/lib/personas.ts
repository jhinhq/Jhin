/** Pure helpers for the Personas library page, the agent Persona tab, the
 * wizard's persona step, and the editor (docs/architecture/personas.md).
 * React-free and unit-tested. The caps and the rendered block mirror
 * `jhin_personas.card` and `jhin_agents.context.persona_block` so what a
 * person previews is what the agent reads. */

import { ApiError } from "@/lib/api";
import type {
  AgentPersonaSummary,
  Persona,
  PersonaCreateInput,
  PersonaFacets,
  PersonaSource,
  PersonaUpdateInput,
} from "@/lib/types";

export const PERSONA_SOURCE_LABELS: Record<PersonaSource, string> = {
  built_in: "By Jhin",
  custom: "Yours",
  agent: "Agent-made",
};

export const FUN_TAG = "fun";

/** Mirrors jhin_personas.card. Whitespace is collapsed before a cap applies. */
export const PERSONA_CAPS = {
  name: 64,
  displayName: 80,
  description: 200,
  facet: 240,
  neverItems: 6,
  neverItem: 120,
  card: 1500,
  tags: 8,
  tag: 32,
} as const;

export type TextFacetKey =
  | "voice"
  | "stance"
  | "pace"
  | "when_unsure"
  | "with_people"
  | "with_teammates"
  | "signature";

/** Prompt order. */
export const TEXT_FACET_KEYS: readonly TextFacetKey[] = [
  "voice",
  "stance",
  "pace",
  "when_unsure",
  "with_people",
  "with_teammates",
  "signature",
];

export interface FacetSpec {
  key: TextFacetKey;
  label: string;
  hint: string;
  placeholder: string;
  required?: boolean;
}

export const FACET_SPECS: readonly FacetSpec[] = [
  {
    key: "voice",
    label: "Voice",
    hint: "How they sound, in one or two sentences.",
    placeholder:
      "Plain, confident, unhurried. Sounds like someone who has already done the thinking.",
    required: true,
  },
  {
    key: "stance",
    label: "Stance",
    hint: "How they take positions and handle disagreement.",
    placeholder: "Takes a position in the first sentence and owns it.",
  },
  {
    key: "pace",
    label: "Pace",
    hint: "Brevity versus depth, and when to go long.",
    placeholder: "Short by default: the answer, then two or three reasons.",
  },
  {
    key: "when_unsure",
    label: "When unsure",
    hint: "State assumptions, or ask the person?",
    placeholder: "Says so, states the assumption it is making, and proceeds on it.",
  },
  {
    key: "with_people",
    label: "With people",
    hint: "The register with the person they serve.",
    placeholder: "Direct and respectful. Gives the answer before the context.",
  },
  {
    key: "with_teammates",
    label: "With teammates",
    hint: "The register with colleagues — the other agents.",
    placeholder: "Blunt in the friendly way colleagues are: what it needs, by when.",
  },
  {
    key: "signature",
    label: "Signature",
    hint: "One small recurring flourish.",
    placeholder: "Opens with ‘Short answer:’ and the answer in one line.",
  },
];

export const EMPTY_FACETS: PersonaFacets = {
  voice: "",
  stance: "",
  pace: "",
  when_unsure: "",
  with_people: "",
  with_teammates: "",
  signature: "",
  never: [],
};

/** Python's `" ".join(value.split())`. */
export function collapseWhitespace(value: string): string {
  return value.split(/\s+/).filter(Boolean).join(" ");
}

/** context.py `_bounded_facet`: collapse, then cut to limit-1 + "…" when over. */
export function boundedFacet(value: string, limit: number): string {
  const text = collapseWhitespace(value);
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

/** `PersonaFacets.facet_chars`: seven collapsed facet strings + every collapsed never item. */
export function facetChars(facets: PersonaFacets): number {
  let total = 0;
  for (const key of TEXT_FACET_KEYS) total += collapseWhitespace(facets[key]).length;
  for (const item of facets.never) total += collapseWhitespace(item).length;
  return total;
}

const NAME_RE = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const TAG_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

export function isValidPersonaName(name: string): boolean {
  return NAME_RE.test(name);
}

export function isValidPersonaTag(tag: string): boolean {
  return TAG_RE.test(tag);
}

/** Comma/whitespace separated → trimmed, lowercased, deduped, order kept. */
export function parseTags(input: string): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const raw of input.split(/[,\s]+/)) {
    const tag = raw.trim().toLowerCase();
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    tags.push(tag);
  }
  return tags;
}

export function isFun(card: Pick<Persona, "tags"> | AgentPersonaSummary): boolean {
  return card.tags.includes(FUN_TAG);
}

/** "Nobody wears this yet" | "Worn by 1 agent" | "Worn by 3 agents" */
export function agentCountText(count: number): string {
  if (count <= 0) return "Nobody wears this yet";
  return `Worn by ${count} ${count === 1 ? "agent" : "agents"}`;
}

export type PreviewAudience = "person" | "teammate" | "both" | "none";

export const PERSONA_GUARDRAIL =
  "This shapes how you say things, never what you may do: tool policy, approvals, safety rules, and your manager's instructions always win.";

/** Byte-for-byte what jhin_agents.context.persona_block renders. "both" is the
 * web preview: it shows With people AND With teammates (a real run gets one). */
export function renderPersonaBlock(
  card: { name: string; display_name: string; facets: PersonaFacets },
  audience: PreviewAudience,
): string {
  const title = boundedFacet(card.display_name, PERSONA_CAPS.displayName) || card.name;
  const lines = [`How you work — ${title}`, PERSONA_GUARDRAIL];
  const facets = card.facets;
  const rows: [string, string][] = [
    ["Voice", facets.voice],
    ["Stance", facets.stance],
    ["Pace", facets.pace],
    ["When unsure", facets.when_unsure],
  ];
  if (audience === "person" || audience === "both") rows.push(["With people", facets.with_people]);
  if (audience === "teammate" || audience === "both") {
    rows.push(["With teammates", facets.with_teammates]);
  }
  rows.push(["Signature", facets.signature]);
  for (const [label, value] of rows) {
    const text = boundedFacet(value, PERSONA_CAPS.facet);
    if (text) lines.push(`- ${label}: ${text}`);
  }
  const never = facets.never
    .slice(0, PERSONA_CAPS.neverItems)
    .map((item) => boundedFacet(item, PERSONA_CAPS.neverItem))
    .filter(Boolean);
  if (never.length > 0) lines.push(`- Never: ${never.join("; ")}`);
  return lines.join("\n");
}

/* ------------------------------------------------------------------ */
/* Filters                                                             */
/* ------------------------------------------------------------------ */

export interface PersonaFilters {
  query: string;
  funOnly: boolean;
  source: PersonaSource | "";
  showDisabled: boolean;
}

function matchesQuery(persona: Persona, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [persona.name, persona.display_name, persona.description].some((text) =>
    text.toLowerCase().includes(needle),
  );
}

/** Same match as the server's `q`: name, display name, description, case-insensitive substring. */
export function filterPersonas(items: Persona[], filters: PersonaFilters): Persona[] {
  return items.filter(
    (persona) =>
      (filters.showDisabled || persona.enabled) &&
      (!filters.source || persona.source === filters.source) &&
      (!filters.funOnly || isFun(persona)) &&
      matchesQuery(persona, filters.query),
  );
}

/** For pickers: enabled cards only, then query + fun. */
export function pickablePersonas(items: Persona[], query: string, funOnly: boolean): Persona[] {
  return filterPersonas(items, { query, funOnly, source: "", showDisabled: false });
}

/* ------------------------------------------------------------------ */
/* Editor draft                                                        */
/* ------------------------------------------------------------------ */

export interface PersonaDraft {
  name: string;
  display_name: string;
  description: string;
  /** Raw comma-separated text; parsed with {@link parseTags}. */
  tagsInput: string;
  /** `never` = the editor's rows, which may hold blanks while typing. */
  facets: PersonaFacets;
}

export function draftFrom(persona: Persona | null): PersonaDraft {
  if (persona === null) {
    return {
      name: "",
      display_name: "",
      description: "",
      tagsInput: "",
      facets: { ...EMPTY_FACETS, never: [""] },
    };
  }
  return {
    name: persona.name,
    display_name: persona.display_name,
    description: persona.description,
    tagsInput: persona.tags.join(", "),
    facets: {
      ...persona.facets,
      never: persona.facets.never.length > 0 ? [...persona.facets.never] : [""],
    },
  };
}

/** Trim/collapse strings, drop blank never rows. */
export function draftFacets(draft: PersonaDraft): PersonaFacets {
  const facets = draft.facets;
  return {
    voice: collapseWhitespace(facets.voice),
    stance: collapseWhitespace(facets.stance),
    pace: collapseWhitespace(facets.pace),
    when_unsure: collapseWhitespace(facets.when_unsure),
    with_people: collapseWhitespace(facets.with_people),
    with_teammates: collapseWhitespace(facets.with_teammates),
    signature: collapseWhitespace(facets.signature),
    never: facets.never.map(collapseWhitespace).filter(Boolean),
  };
}

export function toCreateInput(draft: PersonaDraft): PersonaCreateInput {
  return {
    name: draft.name.trim(),
    display_name: collapseWhitespace(draft.display_name),
    description: collapseWhitespace(draft.description),
    tags: parseTags(draft.tagsInput),
    facets: draftFacets(draft),
  };
}

/** Everything but `name` (immutable) and `enabled` (its own switch). */
export function toUpdateInput(draft: PersonaDraft): PersonaUpdateInput {
  return {
    display_name: collapseWhitespace(draft.display_name),
    description: collapseWhitespace(draft.description),
    tags: parseTags(draft.tagsInput),
    facets: draftFacets(draft),
  };
}

export type PersonaFieldKey =
  | "name"
  | "display_name"
  | "description"
  | "tags"
  | TextFacetKey
  | "never"
  | "facets";

export interface PersonaFormErrors {
  fields: Partial<Record<PersonaFieldKey, string>>;
  general: string | null;
}

/** Client-side caps only (content rules are the server's; they arrive as 422). */
export function validatePersonaForm(
  draft: PersonaDraft,
  creating: boolean,
): Partial<Record<PersonaFieldKey, string>> {
  const errors: Partial<Record<PersonaFieldKey, string>> = {};

  if (creating) {
    const name = draft.name.trim();
    if (!name) errors.name = "Give the persona a name.";
    else if (!isValidPersonaName(name)) {
      errors.name = "Use lowercase letters, digits, and hyphens (up to 64 characters).";
    }
  }

  const displayName = collapseWhitespace(draft.display_name);
  if (!displayName) errors.display_name = "Give it a display name.";
  else if (displayName.length > PERSONA_CAPS.displayName) {
    errors.display_name = `Keep the display name to ${PERSONA_CAPS.displayName} characters.`;
  }

  const description = collapseWhitespace(draft.description);
  if (!description) errors.description = "Add a one-line description.";
  else if (description.length > PERSONA_CAPS.description) {
    errors.description = `Keep the description to ${PERSONA_CAPS.description} characters.`;
  }

  const tags = parseTags(draft.tagsInput);
  if (tags.length > PERSONA_CAPS.tags) errors.tags = `Up to ${PERSONA_CAPS.tags} tags.`;
  else {
    const bad = tags.find((tag) => !isValidPersonaTag(tag));
    if (bad !== undefined) {
      errors.tags = `Tags use lowercase letters, digits, and hyphens (up to ${PERSONA_CAPS.tag} characters): “${bad}”.`;
    }
  }

  for (const spec of FACET_SPECS) {
    const text = collapseWhitespace(draft.facets[spec.key]);
    if (spec.required && !text) {
      errors[spec.key] = "Voice is the one facet every persona needs.";
    } else if (text.length > PERSONA_CAPS.facet) {
      errors[spec.key] = `Keep ${spec.label} to ${PERSONA_CAPS.facet} characters.`;
    }
  }

  const never = draft.facets.never.map(collapseWhitespace).filter(Boolean);
  if (never.length > PERSONA_CAPS.neverItems) errors.never = "Up to six never items.";
  else if (never.some((item) => item.length > PERSONA_CAPS.neverItem)) {
    errors.never = `Keep each never item to ${PERSONA_CAPS.neverItem} characters.`;
  } else {
    const seen = new Set<string>();
    for (const item of never) {
      const key = item.toLowerCase();
      if (seen.has(key)) {
        errors.never = `Never items must be distinct — “${item}” repeats.`;
        break;
      }
      seen.add(key);
    }
  }

  const total = facetChars(draftFacets(draft));
  if (total > PERSONA_CAPS.card) {
    errors.facets = `The card runs to ${total} characters across its facets; the limit is 1,500.`;
  }

  return errors;
}

const TEXT_FACET_SET: ReadonlySet<string> = new Set(TEXT_FACET_KEYS);

/** Where a FastAPI `loc` (already stripped of its leading "body") lands. */
function fieldKeyFor(path: (string | number)[]): PersonaFieldKey | null {
  const [head, second] = path;
  if (head === "name" || head === "display_name" || head === "description") return head;
  if (head === "tags") return "tags";
  if (head === "facets") {
    if (second === undefined) return "facets";
    if (second === "never") return "never";
    if (typeof second === "string" && TEXT_FACET_SET.has(second)) return second as TextFacetKey;
  }
  return null;
}

/** Pydantic prefixes a validator's own words with the error class. */
function stripPydanticPrefix(message: string): string {
  return message.replace(/^(Value error|Assertion failed), /, "");
}

const DOTTED_DETAIL_RE = /^([a-z_][a-z0-9_.]*): ([\s\S]+)$/;

/** Put an API failure under the field it names. */
export function fieldErrorsFrom(error: unknown, fallback: string): PersonaFormErrors {
  if (!(error instanceof ApiError)) return { fields: {}, general: fallback };

  const fields: Partial<Record<PersonaFieldKey, string>> = {};
  let general: string | null = null;

  if (Array.isArray(error.errors) && error.errors.length > 0) {
    for (const item of error.errors) {
      const path = item.loc[0] === "body" ? item.loc.slice(1) : item.loc;
      const key = fieldKeyFor(path);
      const message = stripPydanticPrefix(item.msg);
      if (key === null) general ??= message;
      else fields[key] ??= message;
    }
    return { fields, general };
  }

  const dotted = DOTTED_DETAIL_RE.exec(error.detail);
  if (dotted) {
    const key = fieldKeyFor(dotted[1].split("."));
    const message = stripPydanticPrefix(dotted[2]);
    if (key === null) return { fields, general: message };
    fields[key] = message;
    return { fields, general: null };
  }

  if (error.status === 409 && /already exists/.test(error.detail)) {
    fields.name = error.detail;
    return { fields, general: null };
  }

  return { fields, general: error.detail || fallback };
}
