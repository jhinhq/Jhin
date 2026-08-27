/** An automation whose agent is gone, or paused, has to say so on the card —
 * and never in the words the event worker stores. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AutomationsPage from "@/app/(app)/automations/page";
import type { Trigger } from "@/lib/types";
import { WorkspaceProvider } from "@/lib/workspace-context";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const BASE: Trigger = {
  id: "trigger-1",
  name: "Pick up new tickets",
  enabled: true,
  trigger_type: "connector_event",
  connection_id: null,
  event_type: "connector.linear.issue.updated",
  filter_json: {},
  action_type: "start_agent_task",
  target_agent_id: null,
  target_team_id: null,
  action_config_json: {},
  dedupe_window_seconds: 300,
  workflow_definition: null,
  created_by_user_id: null,
  created_at: "2026-08-20T00:00:00Z",
  updated_at: "2026-08-20T00:00:00Z",
  last_invocation: null,
  target_state: "ok",
  target_warning: null,
};

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function renderWith(trigger: Trigger) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      if (path.endsWith("/triggers")) return json([trigger]);
      if (path.endsWith("/agents")) return json([]);
      if (path.endsWith("/connections")) return json([]);
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceProvider
        user={{
          id: "user-1",
          email: "owner@example.com",
          display_name: "Owner",
          created_at: "2026-08-18T00:00:00Z",
        }}
        workspace={{
          workspace_id: "workspace-1",
          workspace_name: "Acme",
          workspace_slug: "acme",
          role: "owner",
        }}
      >
        <AutomationsPage />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

describe("automations whose target is gone", () => {
  it("marks a switched-off automation as needing an agent and says how to fix it", async () => {
    renderWith({
      ...BASE,
      enabled: false,
      target_state: "agent_deleted",
      target_warning:
        "The agent this automation gave work to was deleted, so the automation was switched off. Edit it to choose another agent, then switch it back on.",
    });

    expect(await screen.findByText("Needs an agent")).toBeTruthy();
    expect(screen.getByText(/Edit it to choose another agent/)).toBeTruthy();
    expect(screen.queryByText("Off")).toBeNull();
  });

  it("explains a paused agent instead of showing the stored error code", async () => {
    renderWith({
      ...BASE,
      target_state: "agent_paused",
      target_warning:
        "The agent this automation gives work to is paused, so nothing will run. Resume that agent, or edit the automation to choose another one.",
      last_invocation: {
        id: "invocation-1",
        trigger_id: "trigger-1",
        status: "failed",
        event_id: "event-1",
        task_id: null,
        workflow_id: null,
        error: "invalid_request",
        error_message:
          "The agent this automation gives work to is paused, so nothing will run. Resume that agent, or edit the automation to choose another one.",
        created_at: "2026-08-20T00:00:00Z",
      },
    });

    await waitFor(() => expect(screen.getAllByText(/is paused/).length).toBeGreaterThan(0));
    expect(screen.queryByText(/invalid_request/)).toBeNull();
    expect(screen.getByText("On, but stuck")).toBeTruthy();
  });
});
