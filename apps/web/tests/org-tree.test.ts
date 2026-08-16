import { describe, expect, it } from "vitest";
import { buildAgentForest, buildOrgTree, countTeamAgents } from "@/lib/org-tree";
import type { OrgAgentNode, OrgTeamNode } from "@/lib/types";

function team(id: string, name: string, parent: string | null = null): OrgTeamNode {
  return {
    id,
    name,
    description: "",
    parent_team_id: parent,
    manager_agent_id: null,
    color_token: "slate",
    icon: "users",
  };
}

function agent(
  id: string,
  name: string,
  teamId: string | null,
  managerId: string | null = null,
): OrgAgentNode {
  return {
    id,
    name,
    slug: name.toLowerCase().replace(/\s+/g, "-"),
    role_title: name,
    status: "active",
    team_id: teamId,
    manager_agent_id: managerId,
  };
}

describe("buildAgentForest", () => {
  it("nests reports under their manager", () => {
    const forest = buildAgentForest([
      agent("cto", "CTO", "eng"),
      agent("swe", "Senior SWE", "eng", "cto"),
      agent("qa", "QA Engineer", "eng", "cto"),
    ]);
    expect(forest).toHaveLength(1);
    expect(forest[0].agent.id).toBe("cto");
    expect(forest[0].reports.map((r) => r.agent.id).sort()).toEqual(["qa", "swe"]);
  });

  it("treats agents whose manager is outside the group as roots", () => {
    const forest = buildAgentForest([agent("swe", "SWE", "eng", "cto-elsewhere")]);
    expect(forest).toHaveLength(1);
    expect(forest[0].agent.id).toBe("swe");
  });

  it("survives a defensive cycle without dropping agents", () => {
    // The server rejects cycles; the UI must still render if one sneaks in.
    const forest = buildAgentForest([
      agent("a", "A", null, "b"),
      agent("b", "B", null, "a"),
    ]);
    const rendered = new Set<string>();
    const walk = (nodes: typeof forest) =>
      nodes.forEach((node) => {
        rendered.add(node.agent.id);
        walk(node.reports);
      });
    walk(forest);
    expect(rendered).toEqual(new Set(["a", "b"]));
  });
});

describe("buildOrgTree", () => {
  const graph = {
    teams: [team("eng", "Engineering"), team("platform", "Platform", "eng"), team("mkt", "Marketing")],
    agents: [
      agent("cto", "CTO", "eng"),
      agent("swe", "Senior SWE", "eng", "cto"),
      agent("qa", "QA Engineer", "eng", "cto"),
      agent("sre", "SRE", "platform", "cto"),
      agent("dir", "Marketing Director", "mkt"),
      agent("blog", "Blogger", "mkt", "dir"),
      agent("solo", "Freelancer", null),
    ],
  };

  it("nests teams by parent and sorts roots by name", () => {
    const tree = buildOrgTree(graph);
    expect(tree.roots.map((r) => r.team.name)).toEqual(["Engineering", "Marketing"]);
    const engineering = tree.roots[0];
    expect(engineering.children.map((c) => c.team.name)).toEqual(["Platform"]);
  });

  it("builds the manager hierarchy inside each team", () => {
    const tree = buildOrgTree(graph);
    const engineering = tree.roots[0];
    expect(engineering.agents).toHaveLength(1);
    expect(engineering.agents[0].agent.id).toBe("cto");
    expect(engineering.agents[0].reports.map((r) => r.agent.id).sort()).toEqual(["qa", "swe"]);
    // SRE is in Platform, managed by the CTO from another team -> root there.
    const platform = engineering.children[0];
    expect(platform.agents.map((a) => a.agent.id)).toEqual(["sre"]);
  });

  it("collects teamless agents into the unassigned pool", () => {
    const tree = buildOrgTree(graph);
    expect(tree.unassigned.map((n) => n.agent.id)).toEqual(["solo"]);
  });

  it("counts agents across nested teams", () => {
    const tree = buildOrgTree(graph);
    expect(countTeamAgents(tree.roots[0])).toBe(4); // cto, swe, qa + sre in Platform
    expect(countTeamAgents(tree.roots[1])).toBe(2);
  });

  it("promotes teams with dangling parents to roots", () => {
    const tree = buildOrgTree({
      teams: [team("orphan", "Orphan", "missing-parent")],
      agents: [],
    });
    expect(tree.roots.map((r) => r.team.id)).toEqual(["orphan"]);
  });
});
