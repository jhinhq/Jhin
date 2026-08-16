/** Component tests: the org tree renders hierarchy and admin actions. */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TeamCard } from "@/components/org/tree";
import { buildOrgTree } from "@/lib/org-tree";
import type { OrgAgentNode, OrgTeamNode } from "@/lib/types";

afterEach(cleanup);

const teams: OrgTeamNode[] = [
  {
    id: "eng",
    name: "Engineering",
    description: "Builds the product",
    parent_team_id: null,
    manager_agent_id: "cto",
    color_token: "indigo",
    icon: "wrench",
  },
];

const agents: OrgAgentNode[] = [
  {
    id: "cto",
    name: "CTO",
    slug: "cto",
    role_title: "Chief Technology Officer",
    status: "active",
    team_id: "eng",
    manager_agent_id: null,
  },
  {
    id: "swe",
    name: "Senior SWE",
    slug: "senior-swe",
    role_title: "Senior Software Engineer",
    status: "active",
    team_id: "eng",
    manager_agent_id: "cto",
  },
  {
    id: "qa",
    name: "QA Engineer",
    slug: "qa-engineer",
    role_title: "QA Engineer",
    status: "paused",
    team_id: "eng",
    manager_agent_id: "cto",
  },
];

function renderTeamCard(isAdmin: boolean) {
  const tree = buildOrgTree({ teams, agents });
  const onOpenAgent = vi.fn();
  render(
    <TeamCard
      node={tree.roots[0]}
      depth={0}
      isAdmin={isAdmin}
      onOpenAgent={onOpenAgent}
      onEditTeam={vi.fn()}
      onDeleteTeam={vi.fn()}
      onAddAgent={vi.fn()}
      managerNameFor={(agent) =>
        agent.manager_agent_id === "cto" ? "CTO" : undefined
      }
    />,
  );
  return { onOpenAgent };
}

describe("TeamCard", () => {
  it("renders the team with its agent hierarchy", () => {
    renderTeamCard(false);
    expect(screen.getByText("Engineering")).toBeDefined();
    expect(screen.getByText("3 agents")).toBeDefined();
    expect(screen.getByText("CTO")).toBeDefined();
    expect(screen.getByText("Senior SWE")).toBeDefined();
    expect(screen.getByText("QA Engineer")).toBeDefined();
    // Reporting lines are labeled.
    expect(screen.getAllByText(/reports to CTO/)).toHaveLength(2);
    // Paused agents are badged.
    expect(screen.getByText("paused")).toBeDefined();
  });

  it("opens the agent drawer callback on click", () => {
    const { onOpenAgent } = renderTeamCard(false);
    screen.getByText("Senior SWE").closest("button")!.click();
    expect(onOpenAgent).toHaveBeenCalledTimes(1);
    expect(onOpenAgent.mock.calls[0][0].id).toBe("swe");
  });

  it("hides admin actions from non-admins and shows them to admins", () => {
    renderTeamCard(false);
    expect(screen.queryByTitle("Edit team")).toBeNull();
    cleanup();
    renderTeamCard(true);
    expect(screen.getByTitle("Edit team")).toBeDefined();
    expect(screen.getByTitle("Delete team")).toBeDefined();
    expect(screen.getByTitle("Add agent to this team")).toBeDefined();
  });
});
