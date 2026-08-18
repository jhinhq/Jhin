import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ScopeEditor } from "@/components/scope-editor";
import type { ConnectionInfo, ToolInfo } from "@/lib/types";

afterEach(cleanup);

const TOOL: ToolInfo = {
  name: "vercel.deployment.read",
  description: "Read one deployment.",
  risk: "read",
  required_capability: "vercel.deployment.read",
  supports_approval: false,
  scope_keys: ["connection_id", "project_id", "deployment_id", "environment"],
  required_grant_scope_keys: ["connection_id", "project_id"],
  input_schema: {},
};

const CONNECTION = {
  id: "conn-1",
  connector_type: "vercel",
  name: "Vercel production",
} as ConnectionInfo;

describe("ScopeEditor", () => {
  it("renders exactly the scope keys declared by the selected tool", () => {
    render(
      <ScopeEditor tool={TOOL} connections={[CONNECTION]} values={{}} onChange={() => {}} />,
    );

    expect(screen.getByLabelText(/Connection/)).toBeDefined();
    expect(screen.getByLabelText(/Project ID/)).toBeDefined();
    expect(screen.getByLabelText(/Deployment ID/)).toBeDefined();
    expect(screen.getByLabelText(/Environment/)).toBeDefined();
    expect(screen.queryByLabelText(/Repository/)).toBeNull();
    expect(screen.getAllByText("Required for this tool")).toHaveLength(2);
  });

  it("reports independent value changes without manufacturing undeclared keys", () => {
    const onChange = vi.fn();
    render(
      <ScopeEditor
        tool={TOOL}
        connections={[CONNECTION]}
        values={{ connection_id: "conn-1" }}
        onChange={onChange}
      />,
    );

    fireEvent.change(screen.getByLabelText(/Project ID/), { target: { value: "prj_123" } });
    expect(onChange).toHaveBeenCalledWith({ connection_id: "conn-1", project_id: "prj_123" });
  });
});
