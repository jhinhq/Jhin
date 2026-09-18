import type { ConnectionInfo, ConnectionToolsOut, ToolInfo } from "../lib/types";

export const assignmentWorkspaceId = "layout-workspace";
export const assignmentAgentId = "bisby-layout-agent";
export const assignmentConnection: ConnectionInfo = {
  id: "22222222-2222-4222-8222-222222222222",
  name: "Supabase",
  connector_type: "mcp",
  auth_type: "oauth",
  status: "active",
  public_id: "layout-supabase",
  config_json: { server_slug: "supabase" },
  created_by_user_id: null,
  created_at: "2026-09-10T00:00:00Z",
  last_verified_at: "2026-09-10T00:00:00Z",
  last_error: null,
  webhook_secret_configured: false,
};

export const assignmentTools: ToolInfo[] = [
  { name: "mcp.supabase.list_projects", risk: "read", description: "List the projects available to this connection." },
  { name: "mcp.supabase.get_project_security_advisors", risk: "read", description: "Check security recommendations for a Supabase project." },
  { name: "mcp.supabase.execute_sql", risk: "write", description: "Run a database query under the agent's existing approval policy." },
].map((tool) => ({
  ...tool,
  risk: tool.risk as ToolInfo["risk"],
  required_capability: tool.name,
  supports_approval: true,
  scope_keys: ["connection_id", "tool"],
  required_grant_scope_keys: [],
  input_schema: {},
}));

export const assignmentConnectionTools: ConnectionToolsOut = {
  connection_id: assignmentConnection.id,
  connector_type: "mcp",
  dynamic: true,
  capability_pattern: "mcp.supabase.*",
  discovered_at: "2026-09-10T00:00:00Z",
  tools: assignmentTools.map((tool) => ({
    ...tool,
    provider_name: tool.name.split(".").at(-1)!,
    derived_risk: tool.risk,
    risk_override: null,
    annotations: {},
    schema_truncated: false,
  })),
};
