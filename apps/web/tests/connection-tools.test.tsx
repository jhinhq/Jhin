/** Component tests: per-connection tools with risk badges and overrides. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConnectionTools } from "@/components/connection-tools";
import type { ConnectionToolsOut } from "@/lib/types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const DATA: ConnectionToolsOut = {
  connection_id: "conn-1",
  connector_type: "mcp",
  dynamic: true,
  capability_pattern: "mcp.fake.*",
  discovered_at: "2026-08-20T10:00:00Z",
  tools: [
    {
      name: "mcp.fake.echo",
      provider_name: "echo",
      description: "[MCP: fake] Return the text.",
      risk: "read",
      derived_risk: "read",
      risk_override: null,
      annotations: { read_only_hint: true, idempotent_hint: true },
      input_schema: {},
      schema_truncated: false,
      supports_approval: true,
      scope_keys: ["connection_id", "tool"],
    },
    {
      name: "mcp.fake.delete_everything",
      provider_name: "delete_everything",
      description: "[MCP: fake] Delete every note.",
      risk: "write",
      derived_risk: "destructive",
      risk_override: "write",
      annotations: { destructive_hint: true },
      input_schema: {},
      schema_truncated: false,
      supports_approval: true,
      scope_keys: ["connection_id", "tool"],
    },
  ],
};

function renderTools(props: Partial<React.ComponentProps<typeof ConnectionTools>> = {}) {
  const onChanged = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
      <ConnectionTools
        workspaceId="workspace-1"
        connectionId="conn-1"
        data={DATA}
        isPending={false}
        error={null}
        canManage
        onChanged={onChanged}
        {...props}
      />
    </QueryClientProvider>,
  );
  return onChanged;
}

describe("ConnectionTools", () => {
  it("lists tools with risk badges, annotations, and override markers", () => {
    renderTools();
    const echo = within(screen.getByTestId("connection-tool-mcp.fake.echo"));
    expect(echo.getAllByText("read").some((el) => el.tagName === "SPAN")).toBe(true);
    expect(echo.getByText("read-only")).toBeDefined();
    expect(echo.getByText("Return the text.")).toBeDefined();
    const destructive = within(screen.getByTestId("connection-tool-mcp.fake.delete_everything"));
    expect(destructive.getAllByText("write").some((el) => el.tagName === "SPAN")).toBe(true);
    expect(destructive.getByText("admin override")).toBeDefined();
    expect(screen.getByText("mcp.fake.*")).toBeDefined();
    expect(screen.getByText(/2 tools/)).toBeDefined();
  });

  it("sends a risk override and reports the server's answer", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      expect(String(input)).toBe("/api/v1/workspaces/workspace-1/connections/conn-1/tools");
      expect(init?.method).toBe("PATCH");
      expect(JSON.parse(String(init?.body))).toEqual({ tool_risk_overrides: { echo: "destructive" } });
      return new Response(JSON.stringify({ ...DATA, tools: [] }), { status: 200, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = renderTools();
    fireEvent.change(screen.getByLabelText("Risk level for mcp.fake.echo"), { target: { value: "destructive" } });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(onChanged.mock.calls[0][0].tools).toEqual([]);
  });

  it("hides overrides for read-only viewers and static connectors", () => {
    renderTools({ canManage: false });
    expect(screen.queryByLabelText(/Risk level for/)).toBeNull();
    cleanup();
    renderTools({ data: { ...DATA, dynamic: false, capability_pattern: "github.*" } });
    expect(screen.queryByLabelText(/Risk level for/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Re-check tools/ })).toBeNull();
  });
});
