/** TypeScript mirrors of the API's Pydantic contracts. */

export type WorkspaceRole = "viewer" | "member" | "admin" | "owner";
export type AgentStatus = "active" | "paused" | "disabled";
export type AutonomyLevel = "manual" | "supervised" | "autonomous";

export interface UserOut {
  id: string;
  email: string;
  display_name: string;
  created_at: string;
}

export interface MembershipOut {
  workspace_id: string;
  workspace_name: string;
  workspace_slug: string;
  role: WorkspaceRole;
}

export interface MeResponse {
  user: UserOut;
  memberships: MembershipOut[];
}

export interface BootstrapStatus {
  needs_bootstrap: boolean;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  status: string;
  default_timezone: string;
  created_at: string;
  updated_at: string;
}

export interface Member {
  id: string;
  user_id: string;
  email: string;
  display_name: string;
  role: WorkspaceRole;
  created_at: string;
}

export interface Team {
  id: string;
  workspace_id: string;
  name: string;
  description: string;
  parent_team_id: string | null;
  manager_agent_id: string | null;
  color_token: string;
  icon: string;
  created_at: string;
  updated_at: string;
}

export interface Agent {
  id: string;
  workspace_id: string;
  team_id: string | null;
  manager_agent_id: string | null;
  name: string;
  slug: string;
  role_title: string;
  description: string;
  system_prompt: string;
  status: AgentStatus;
  autonomy_level: AutonomyLevel;
  model_profile_id: string | null;
  temperature: number | null;
  max_output_tokens: number | null;
  max_steps: number;
  max_run_minutes: number;
  monthly_budget_cents: number | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface OrgTeamNode {
  id: string;
  name: string;
  description: string;
  parent_team_id: string | null;
  manager_agent_id: string | null;
  color_token: string;
  icon: string;
}

export interface OrgAgentNode {
  id: string;
  name: string;
  slug: string;
  role_title: string;
  status: AgentStatus;
  team_id: string | null;
  manager_agent_id: string | null;
}

export interface OrgGraph {
  workspace_id: string;
  teams: OrgTeamNode[];
  agents: OrgAgentNode[];
}

export interface AuditEvent {
  id: string;
  workspace_id: string | null;
  actor_type: string;
  actor_id: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  metadata_json: Record<string, unknown>;
  request_id: string | null;
  created_at: string;
}

export interface AuditEventPage {
  events: AuditEvent[];
  total: number;
}
