/** Pure helpers for the trigger builder (plan 17.10). Kept out of the page
 * component so the row↔DSL round-trip and the summary sentence are unit-
 * testable. */

import { APP_LABELS, humanizeSegment, titleCase } from "@/lib/humanize";
import type { Agent, ConnectionInfo, Trigger, TriggerCondition } from "@/lib/types";

const TRIGGER_FRIENDLY_EVENTS: Record<string, string> = {
  "connector.linear.issue.created": "a Linear issue is created",
  "connector.linear.issue.updated": "a Linear issue changes",
  "connector.github.pull_request.opened": "a GitHub pull request opens",
  "connector.github.pull_request.merged": "a GitHub pull request merges",
  "connector.github.push": "code is pushed to GitHub",
  "connector.github.issue.opened": "a GitHub issue opens",
};

/** The "When …" half of an automation, in plain words. Shared by the cards,
 * the builder's event picker, and the summary sentence. */
export function triggerWhen(eventType: string | null, connectionName: string | undefined): string {
  if (!eventType) return connectionName ? `anything happens in ${connectionName}` : "anything happens";
  const known = TRIGGER_FRIENDLY_EVENTS[eventType];
  if (known) return known;
  const parts = eventType.replace(/^connector\./, "").split(".");
  const app = APP_LABELS[parts[0]] ?? titleCase(parts[0] ?? "");
  const what = parts.slice(1).map(humanizeSegment).join(" ");
  return what ? `${what} in ${app}` : `something happens in ${app}`;
}

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
  // Same friendly event wording as the automation cards and the builder's
  // event dropdown; triggerWhen already folds the connection in when there is
  // no event type.
  const when = triggerWhen(eventType || null, connection?.name);
  const source = eventType && connection ? ` via ${connection.name}` : "";
  const clauses = rows
    .filter((row) => row.path)
    .map((row) => {
      const field = row.path.replace(/^data\./, "").replace(/\.name$/, "");
      return `${field} ${OP_LABELS[row.op] ?? row.op}${row.op === "exists" ? "" : ` “${row.value}”`}`;
    });
  const condition = clauses.length ? ` where ${clauses.join(" and ")}` : "";
  const target = agent ? `assign a task to ${agent.name}` : "assign a task";
  return `When ${when}${source}${condition}, ${target}.`;
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
