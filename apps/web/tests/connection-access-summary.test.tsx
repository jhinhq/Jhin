import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ConnectionAccessSummary } from "@/components/connection-access-summary";
import type { ConnectionAccessSummaryOut } from "@/lib/types";

afterEach(cleanup);

describe("ConnectionAccessSummary", () => {
  it("shows authorized agents and preserves exact relevant grant scopes", () => {
    const summary: ConnectionAccessSummaryOut = {
      connection_id: "conn-1",
      agents: [
        {
          agent_id: "agent-1",
          agent_name: "Release Engineer",
          authorized: true,
          authorized_tool_names: ["vercel.deployment.read"],
          grants: [
            {
              grant_id: "grant-1",
              capability: "vercel.deployment.*",
              effect: "allow",
              scope: { connection_id: "conn-1", project_id: "prj_1" },
              eligible_tool_names: ["vercel.deployment.read"],
              eligibility_reason: null,
            },
          ],
        },
      ],
    };

    render(<ConnectionAccessSummary summary={summary} />);
    expect(screen.getByText("Release Engineer")).toBeDefined();
    expect(screen.getByText("Authorized")).toBeDefined();
    expect(screen.getByText("vercel.deployment.read")).toBeDefined();
    expect(screen.getByText(/connection_id=conn-1/)).toBeDefined();
    expect(screen.getByText(/project_id=prj_1/)).toBeDefined();
    expect(screen.getByText(/does not bypass the agent's current approval policy/i)).toBeDefined();
  });

  it("labels denied and incomplete grants and renders a clear empty state", () => {
    const summary: ConnectionAccessSummaryOut = {
      connection_id: "conn-1",
      agents: [
        {
          agent_id: "agent-2",
          agent_name: "QA",
          authorized: false,
          authorized_tool_names: [],
          grants: [
            {
              grant_id: "grant-2",
              capability: "supabase.database.read",
              effect: "deny",
              scope: { connection_id: "conn-1" },
              eligible_tool_names: [],
              eligibility_reason: "Missing required scope keys: project_ref, schema",
            },
          ],
        },
      ],
    };

    const { rerender } = render(<ConnectionAccessSummary summary={summary} />);
    expect(screen.getByText("Not authorized")).toBeDefined();
    expect(screen.getByText("deny")).toBeDefined();
    expect(screen.getByText(/Missing required scope keys/)).toBeDefined();

    rerender(<ConnectionAccessSummary summary={{ connection_id: "conn-1", agents: [] }} />);
    expect(screen.getByText(/No agents have grants relevant to this connection/)).toBeDefined();
  });
});
