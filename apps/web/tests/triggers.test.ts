import { describe, expect, it } from "vitest";
import { filterToRows, rowsToFilter, sampleEventFor, summarySentence } from "@/lib/triggers";
import type { Agent, ConnectionInfo, Trigger } from "@/lib/types";

describe("rowsToFilter", () => {
  it("builds a flat all-group with parsed scalar values", () => {
    const filter = rowsToFilter([
      { path: "data.team.key", op: "eq", value: "ENG" },
      { path: "data.priority", op: "gte", value: "2" },
      { path: "data.state.name", op: "transitioned_to", value: "Todo" },
    ]);
    expect(filter).toEqual({
      all: [
        { path: "data.team.key", op: "eq", value: "ENG" },
        { path: "data.priority", op: "gte", value: 2 },
        { path: "data.state.name", op: "transitioned_to", value: "Todo" },
      ],
    });
  });

  it("splits in/not_in values on commas and omits value for exists", () => {
    const filter = rowsToFilter([
      { path: "data.state.name", op: "in", value: "Todo, In Progress" },
      { path: "data.assignee", op: "exists", value: "ignored" },
    ]);
    expect(filter.all[0]).toEqual({
      path: "data.state.name",
      op: "in",
      value: ["Todo", "In Progress"],
    });
    expect(filter.all[1]).toEqual({ path: "data.assignee", op: "exists" });
  });
});

describe("filterToRows", () => {
  it("round-trips rows through the DSL", () => {
    const rows = [
      { path: "data.team.key", op: "eq", value: "ENG" },
      { path: "data.state.name", op: "in", value: "Todo, Done" },
    ];
    const roundTripped = filterToRows(rowsToFilter(rows) as Trigger["filter_json"]);
    expect(roundTripped).toEqual(rows);
  });

  it("skips nested groups it cannot edit", () => {
    const rows = filterToRows({
      all: [{ any: [{ path: "a", op: "exists" }] }, { path: "b", op: "eq", value: "x" }],
    } as Trigger["filter_json"]);
    expect(rows).toEqual([{ path: "b", op: "eq", value: "x" }]);
  });
});

describe("summarySentence", () => {
  it("describes the WHEN/IF/THEN clauses in plain language", () => {
    const sentence = summarySentence(
      "connector.linear.issue.updated",
      [
        { path: "data.team.key", op: "eq", value: "ENG" },
        { path: "data.state.name", op: "transitioned_to", value: "Todo" },
      ],
      { name: "Senior Software Engineer" } as Agent,
      { name: "Linear (fake)" } as ConnectionInfo,
    );
    expect(sentence).toBe(
      "When a connector.linear.issue.updated event arrives from Linear (fake) where " +
        "team.key equals “ENG” and state changes to “Todo”, assign a task to " +
        "Senior Software Engineer.",
    );
  });

  it("falls back gracefully when nothing is selected", () => {
    expect(summarySentence("", [], undefined, undefined)).toBe(
      "When a … event arrives from any connection, assign a task.",
    );
  });
});

describe("sampleEventFor", () => {
  it("produces valid JSON shaped like a normalized transition event", () => {
    const parsed = JSON.parse(sampleEventFor("connector.linear.issue.updated", "OPS")) as {
      event_type: string;
      data: { team: { key: string }; state: { name: string }; changed_from: unknown };
    };
    expect(parsed.event_type).toBe("connector.linear.issue.updated");
    expect(parsed.data.team.key).toBe("OPS");
    expect(parsed.data.state.name).toBe("Todo");
    expect(parsed.data.changed_from).toBeTruthy();
  });
});
