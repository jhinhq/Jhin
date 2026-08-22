/** Pure memory helpers: words for numbers, filters, params, validation. */

import { describe, expect, it } from "vitest";
import {
  confidenceWord,
  filterByStatus,
  importanceWord,
  memoryErrorMessage,
  memoryListParams,
  parseTags,
  sortMemories,
  validateMemoryDraft,
} from "@/lib/memory";
import type { MemoryRecord } from "@/lib/types";

function memory(overrides: Partial<MemoryRecord>): MemoryRecord {
  return {
    id: "m1",
    workspace_id: "ws",
    scope: "agent",
    scope_id: "a1",
    kind: "fact",
    subject: null,
    content: "We deploy on Tuesdays.",
    source_conversation_id: null,
    source_message_id: null,
    source_task_id: null,
    source_event_id: null,
    visibility: "agent",
    sensitivity: "normal",
    confidence: 0.9,
    importance: 0.6,
    tags_json: [],
    status: "active",
    valid_from: null,
    expires_at: null,
    pinned_at: null,
    forgotten_at: null,
    version: 1,
    supersedes_id: null,
    has_embedding: false,
    embedding_model: null,
    created_by_type: "agent",
    created_by_id: "a1",
    policy_json: {},
    created_at: "2026-08-20T10:00:00Z",
    updated_at: "2026-08-20T10:00:00Z",
    ...overrides,
  };
}

describe("memory words", () => {
  it("turns confidence and importance into plain words", () => {
    expect(confidenceWord(0.95)).toBe("Very sure");
    expect(confidenceWord(0.7)).toBe("Fairly sure");
    expect(confidenceWord(0.4)).toBe("Not sure");
    expect(confidenceWord(0.1)).toBe("A guess");
    expect(importanceWord(0.9)).toBe("Essential");
    expect(importanceWord(0.2)).toBe("Minor");
  });
});

describe("filters and params", () => {
  it("filters by status group and hides forgotten records", () => {
    const items = [
      memory({ id: "a", status: "active" }),
      memory({ id: "b", status: "proposed" }),
      memory({ id: "c", status: "contested" }),
      memory({ id: "d", status: "forgotten", content: "" }),
    ];
    expect(filterByStatus(items, "active").map((m) => m.id)).toEqual(["a"]);
    expect(filterByStatus(items, "review").map((m) => m.id)).toEqual(["b", "c"]);
    expect(filterByStatus(items, "all").map((m) => m.id)).toEqual(["a", "b", "c"]);
  });

  it("maps each scope to one API query", () => {
    const ids = { agentId: "a1", teamId: "t1" };
    expect(memoryListParams("agent", ids, "active")).toMatchObject({ scope: "agent", agent_id: "a1", status: "active" });
    expect(memoryListParams("team", ids, "all")).toMatchObject({ scope: "team", team_id: "t1", status: undefined });
    expect(memoryListParams("workspace", ids, "review")).toMatchObject({ scope: "workspace", agent_id: undefined, team_id: undefined });
  });

  it("sorts pinned first, then newest", () => {
    const items = [
      memory({ id: "old", created_at: "2026-08-01T00:00:00Z" }),
      memory({ id: "new", created_at: "2026-08-20T00:00:00Z" }),
      memory({ id: "pinned", created_at: "2026-07-01T00:00:00Z", pinned_at: "2026-08-02T00:00:00Z" }),
    ];
    expect(sortMemories(items).map((m) => m.id)).toEqual(["pinned", "new", "old"]);
  });
});

describe("validateMemoryDraft", () => {
  const base = { content: "Ship on Tuesdays", scope: "agent" as const, kind: "fact" as const, tags: "" };

  it("accepts a plain agent memory from a member", () => {
    expect(validateMemoryDraft(base, { isAdmin: false, hasTeam: true })).toBeNull();
  });

  it("needs content and respects the length cap", () => {
    expect(validateMemoryDraft({ ...base, content: "  " }, { isAdmin: true, hasTeam: true })).toMatch(/Write what/);
    expect(validateMemoryDraft({ ...base, content: "x".repeat(2001) }, { isAdmin: true, hasTeam: true })).toMatch(/2,000/);
  });

  it("keeps team and company scopes to admins and teams", () => {
    expect(validateMemoryDraft({ ...base, scope: "workspace" }, { isAdmin: false, hasTeam: true })).toMatch(/admins/);
    expect(validateMemoryDraft({ ...base, scope: "team" }, { isAdmin: true, hasTeam: false })).toMatch(/team/);
    expect(validateMemoryDraft({ ...base, scope: "team" }, { isAdmin: true, hasTeam: true })).toBeNull();
  });

  it("parses tags and caps them", () => {
    expect(parseTags("Deploys, process,,deploys\nops")).toEqual(["deploys", "process", "ops"]);
    const many = Array.from({ length: 11 }, (_, i) => `t${i}`).join(",");
    expect(validateMemoryDraft({ ...base, tags: many }, { isAdmin: true, hasTeam: true })).toMatch(/10 tags/);
  });

  it("explains API failures without jargon", () => {
    expect(memoryErrorMessage(409, "duplicate")).toMatch(/already remembers/);
    expect(memoryErrorMessage(422, "secret")).toMatch(/password, key, or token/);
    expect(memoryErrorMessage(500, "Boom")).toBe("Boom. Try again in a moment.");
  });
});
