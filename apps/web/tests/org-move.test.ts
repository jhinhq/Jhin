/** Pure rules behind reorganising the company: reachability, cycle refusal,
 * what each drop target writes, and the optimistic overlay. */

import { describe, expect, it } from "vitest";
import {
  applyPendingMoves,
  canMove,
  descendantIds,
  describeMove,
  moveOptions,
  reportsLeftBehind,
  resolveMove,
  type MoveGraph,
} from "@/lib/org-move";
import type { OrgAgentNode, OrgTeamNode } from "@/lib/types";

function team(id: string, name: string): OrgTeamNode {
  return {
    id,
    name,
    description: "",
    parent_team_id: null,
    manager_agent_id: null,
    color_token: "indigo",
    icon: "users",
  };
}

function agent(
  id: string,
  name: string,
  teamId: string | null,
  managerId: string | null,
): OrgAgentNode {
  return {
    id,
    name,
    slug: id,
    role_title: `${name} role`,
    status: "active",
    team_id: teamId,
    manager_agent_id: managerId,
  };
}

/** Engineering: Ada → Bisby → Connie. Marketing is empty. Quill is loose. */
const graph: MoveGraph = {
  teams: [team("eng", "Engineering"), team("mkt", "Marketing")],
  agents: [
    agent("ada", "Ada", "eng", null),
    agent("bisby", "Bisby", "eng", "ada"),
    agent("connie", "Connie", "eng", "bisby"),
    agent("quill", "Quill", null, null),
  ],
};

describe("descendantIds", () => {
  it("collects the whole reporting subtree, not just direct reports", () => {
    expect(descendantIds("ada", graph.agents)).toEqual(new Set(["bisby", "connie"]));
    expect(descendantIds("bisby", graph.agents)).toEqual(new Set(["connie"]));
    expect(descendantIds("connie", graph.agents)).toEqual(new Set());
  });

  it("terminates on data that already contains a cycle", () => {
    const looped: OrgAgentNode[] = [
      agent("a", "A", "eng", "c"),
      agent("b", "B", "eng", "a"),
      agent("c", "C", "eng", "b"),
    ];
    expect(descendantIds("a", looped)).toEqual(new Set(["b", "c"]));
  });
});

describe("canMove", () => {
  it("refuses dropping an agent on itself", () => {
    const check = canMove("ada", { kind: "agent", agentId: "ada" }, graph);
    expect(check.ok).toBe(false);
    expect(check.reason).toContain("themselves");
  });

  it("refuses dropping an agent onto its own subordinate", () => {
    const direct = canMove("ada", { kind: "agent", agentId: "bisby" }, graph);
    expect(direct.ok).toBe(false);
    expect(direct.reason).toContain("loop");
    // Indirect reports are refused for the same reason.
    const indirect = canMove("ada", { kind: "agent", agentId: "connie" }, graph);
    expect(indirect.ok).toBe(false);
    expect(indirect.reason).toContain("Connie");
  });

  it("allows dropping onto an unrelated agent, and onto your own manager's peer", () => {
    expect(canMove("quill", { kind: "agent", agentId: "connie" }, graph).ok).toBe(true);
    expect(canMove("connie", { kind: "agent", agentId: "ada" }, graph).ok).toBe(true);
  });

  it("always allows a group drop: it clears the manager, so no loop is possible", () => {
    expect(canMove("ada", { kind: "group", teamId: "mkt" }, graph).ok).toBe(true);
    expect(canMove("ada", { kind: "group", teamId: null }, graph).ok).toBe(true);
  });

  it("refuses targets that have gone away", () => {
    expect(canMove("ada", { kind: "group", teamId: "ghost" }, graph).ok).toBe(false);
    expect(canMove("ada", { kind: "agent", agentId: "ghost" }, graph).ok).toBe(false);
    expect(canMove("ghost", { kind: "group", teamId: "eng" }, graph).ok).toBe(false);
  });
});

describe("resolveMove", () => {
  it("dropping onto an agent sets that manager and follows them into their team", () => {
    expect(resolveMove("quill", { kind: "agent", agentId: "bisby" }, graph)).toEqual({
      team_id: "eng",
      manager_agent_id: "bisby",
    });
  });

  it("dropping onto a team's top level joins the team and clears the manager", () => {
    expect(resolveMove("quill", { kind: "group", teamId: "eng" }, graph)).toEqual({
      team_id: "eng",
      manager_agent_id: null,
    });
    // Same target used as the un-nest affordance.
    expect(resolveMove("connie", { kind: "group", teamId: "eng" }, graph)).toEqual({
      team_id: "eng",
      manager_agent_id: null,
    });
  });

  it("dropping onto Independent clears both the team and the manager", () => {
    expect(resolveMove("connie", { kind: "group", teamId: null }, graph)).toEqual({
      team_id: null,
      manager_agent_id: null,
    });
  });

  it("moves an agent across teams when they follow a manager", () => {
    const crossTeam: MoveGraph = {
      ...graph,
      agents: [...graph.agents, agent("mona", "Mona", "mkt", null)],
    };
    expect(resolveMove("connie", { kind: "agent", agentId: "mona" }, crossTeam)).toEqual({
      team_id: "mkt",
      manager_agent_id: "mona",
    });
  });

  it("returns null for a move that changes nothing", () => {
    expect(resolveMove("bisby", { kind: "agent", agentId: "ada" }, graph)).toBeNull();
    expect(resolveMove("quill", { kind: "group", teamId: null }, graph)).toBeNull();
  });

  it("returns null for an illegal move rather than a doomed payload", () => {
    expect(resolveMove("ada", { kind: "agent", agentId: "connie" }, graph)).toBeNull();
  });
});

describe("reportsLeftBehind", () => {
  it("names the direct reports that stay in the old team", () => {
    const left = reportsLeftBehind("ada", { team_id: "mkt", manager_agent_id: null }, graph);
    expect(left.map((node) => node.id)).toEqual(["bisby"]);
  });

  it("is empty when the team does not change", () => {
    expect(reportsLeftBehind("ada", { team_id: "eng", manager_agent_id: null }, graph)).toEqual([]);
  });
});

describe("describeMove", () => {
  it("says who moved, where, and who they now report to", () => {
    expect(describeMove("quill", { team_id: "eng", manager_agent_id: "bisby" }, graph)).toBe(
      "Quill moved to the Engineering team, reporting to Bisby.",
    );
    expect(describeMove("connie", { team_id: null, manager_agent_id: null }, graph)).toContain(
      "Independent (no team), reporting to no one.",
    );
  });

  it("owns up to reports that were left behind", () => {
    expect(describeMove("ada", { team_id: "mkt", manager_agent_id: null }, graph)).toContain(
      "1 direct report stayed in Engineering.",
    );
  });
});

describe("moveOptions", () => {
  it("offers every team's top level plus Independent, flagging where they are now", () => {
    const [groups] = moveOptions("bisby", graph);
    expect(groups.options.map((option) => option.id)).toEqual([
      "group:eng",
      "group:mkt",
      "group:none",
    ]);
    // Bisby reports to Ada, so no group option is their current position.
    expect(groups.options.every((option) => !option.current)).toBe(true);
    expect(moveOptions("quill", graph)[0].options.at(-1)?.current).toBe(true);
  });

  it("keeps illegal managers in the list, disabled, with the reason", () => {
    const managers = moveOptions("ada", graph)[1];
    expect(managers.options.map((option) => option.id)).not.toContain("agent:ada");
    const bisby = managers.options.find((option) => option.id === "agent:bisby");
    expect(bisby?.check.ok).toBe(false);
    expect(bisby?.check.reason).toContain("loop");
    const quill = managers.options.find((option) => option.id === "agent:quill");
    expect(quill?.check.ok).toBe(true);
  });

  it("marks the current manager", () => {
    const managers = moveOptions("connie", graph)[1];
    expect(managers.options.find((option) => option.id === "agent:bisby")?.current).toBe(true);
  });
});

describe("applyPendingMoves", () => {
  it("returns the same array when nothing is pending", () => {
    expect(applyPendingMoves(graph.agents, {})).toBe(graph.agents);
  });

  it("overlays in-flight moves without touching anyone else", () => {
    const next = applyPendingMoves(graph.agents, {
      quill: { team_id: "eng", manager_agent_id: "ada", seq: 1 },
    });
    expect(next.find((node) => node.id === "quill")).toMatchObject({
      team_id: "eng",
      manager_agent_id: "ada",
    });
    expect(next.find((node) => node.id === "ada")).toEqual(
      graph.agents.find((node) => node.id === "ada"),
    );
  });

  it("keeps several concurrent moves independent", () => {
    const next = applyPendingMoves(graph.agents, {
      quill: { team_id: "eng", manager_agent_id: null, seq: 1 },
      connie: { team_id: "mkt", manager_agent_id: null, seq: 2 },
    });
    expect(next.find((node) => node.id === "quill")?.team_id).toBe("eng");
    expect(next.find((node) => node.id === "connie")?.team_id).toBe("mkt");
  });
});
