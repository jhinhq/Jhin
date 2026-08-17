/** Pure helpers for the trigger builder (plan 17.10). Kept out of the page
 * component so the row↔DSL round-trip and the summary sentence are unit-
 * testable. */

import type { Agent, ConnectionInfo, Trigger, TriggerCondition } from "@/lib/types";

export const TRIGGER_OPS = [
  "eq",
  "neq",
  "in",
  "not_in",
  "contains",
  "exists",
  "gt",
  "gte",
  "lt",
  "lte",
  "transitioned_to",
] as const;

export const OP_LABELS: Record<string, string> = {
  eq: "equals",
  neq: "does not equal",
  in: "is one of",
  not_in: "is not one of",
  contains: "contains",
  exists: "exists",
  gt: ">",
  gte: "≥",
  lt: "<",
  lte: "≤",
  transitioned_to: "changes to",
};

export interface ConditionRow {
  path: string;
  op: string;
  value: string;
}

function parseScalar(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/** UI rows → filter DSL. Values parse as JSON when possible ("2" → 2,
 * "true" → true, otherwise plain string); in/not_in split on commas. */
export function rowsToFilter(rows: ConditionRow[]): { all: TriggerCondition[] } {
  return {
    all: rows.map((row) => {
      const condition: TriggerCondition = { path: row.path.trim(), op: row.op };
      if (row.op === "exists") return condition;
      if (row.op === "in" || row.op === "not_in") {
        condition.value = row.value.split(",").map((item) => parseScalar(item.trim()));
      } else {
        condition.value = parseScalar(row.value);
      }
      return condition;
    }),
  };
}

/** Filter DSL → editable rows. Only flat top-level `all` conditions are
 * editable in the builder; nested groups are preserved server-side but not
 * rendered (the builder always writes a flat `all`). */
export function filterToRows(filter: Trigger["filter_json"]): ConditionRow[] {
  const children = filter.all ?? [];
  const rows: ConditionRow[] = [];
  for (const child of children) {
    if (typeof child !== "object" || child === null || !("path" in child)) continue;
    const condition = child as TriggerCondition;
    const value = condition.value;
    rows.push({
      path: condition.path,
      op: condition.op,
      value: Array.isArray(value)
        ? value.map((item) => (typeof item === "string" ? item : JSON.stringify(item))).join(", ")
        : typeof value === "string"
          ? value
          : value === undefined
            ? ""
            : JSON.stringify(value),
    });
  }
  return rows;
}

/** Human-readable summary of the WHEN/IF/THEN clauses (plan 17.10). */
export function summarySentence(
  eventType: string,
  rows: ConditionRow[],
  agent: Agent | undefined,
  connection: ConnectionInfo | undefined,
): string {
  const source = connection ? connection.name : "any connection";
  const clauses = rows
    .filter((row) => row.path)
    .map((row) => {
      const field = row.path.replace(/^data\./, "").replace(/\.name$/, "");
      return `${field} ${OP_LABELS[row.op] ?? row.op}${row.op === "exists" ? "" : ` “${row.value}”`}`;
    });
  const condition = clauses.length ? ` where ${clauses.join(" and ")}` : "";
  const target = agent ? `assign a task to ${agent.name}` : "assign a task";
  return `When a ${eventType || "…"} event arrives from ${source}${condition}, ${target}.`;
}

/** Editable sample payload for the test panel, shaped like a normalized
 * Linear issue event. */
export function sampleEventFor(eventType: string, teamKey: string): string {
  const type = eventType || "connector.linear.issue.updated";
  return JSON.stringify(
    {
      event_type: type,
      data: {
        external_id: "ENG-142",
        title: "Fix the failing test",
        description: "scripts/run_tests.sh must pass.",
        url: "https://linear.example/issue/ENG-142",
        team: { id: "team-1", key: teamKey || "ENG", name: "Engineering" },
        state: { id: "state-todo", name: "Todo", type: "unstarted" },
        changed_from: { state: { id: "state-backlog" } },
      },
    },
    null,
    2,
  );
}
