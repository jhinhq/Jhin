/** Component test: the "who they can ask for help" directory reads the
 * API's paged `{items, has_more}` response. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HelpDirectory } from "@/components/agents/help-directory";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("HelpDirectory", () => {
  it("lists colleagues from the paged directory response, excluding the agent itself", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        expect(String(input)).toContain("/directory");
        return json({
          items: [
            {
              id: "agent-1",
              name: "Bisby",
              slug: "bisby",
              role_title: "Assistant",
              public_purpose: "",
              expertise: [],
              availability: "available",
              primary_team_id: null,
              primary_team_name: null,
              manager_agent_id: null,
            },
            {
              id: "agent-2",
              name: "Quill",
              slug: "quill",
              role_title: "Writer",
              public_purpose: "",
              expertise: ["copy"],
              availability: "available",
              primary_team_id: null,
              primary_team_name: "Content",
              manager_agent_id: "agent-1",
            },
          ],
          has_more: false,
        });
      }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      createElement(
        QueryClientProvider,
        { client: queryClient },
        createElement(HelpDirectory, { workspaceId: "ws", agentId: "agent-1", agentName: "Bisby" }),
      ),
    );
    expect(await screen.findByText("Quill")).toBeTruthy();
    expect(screen.queryByText("Bisby")).toBeNull();
    expect(screen.getByText("Writer · Content")).toBeTruthy();
  });
});
