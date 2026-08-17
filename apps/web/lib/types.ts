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

// --- Phase 3: secrets, models, tasks, runs ---

export type ModelProviderType =
  | "openai"
  | "anthropic"
  | "openrouter"
  | "ollama"
  | "openai_compatible";

export interface SecretOut {
  id: string;
  workspace_id: string;
  name: string;
  type: string;
  masked_hint: string;
  key_version: number;
  last_used_at: string | null;
  rotated_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelProvider {
  id: string;
  workspace_id: string;
  type: ModelProviderType;
  display_name: string;
  base_url: string | null;
  secret_id: string | null;
  enabled: boolean;
  last_verified_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelProfile {
  id: string;
  workspace_id: string;
  provider_id: string;
  model_name: string;
  display_name: string;
  context_window: number | null;
  input_cost_micros_per_million: number | null;
  output_cost_micros_per_million: number | null;
  supports_tools: boolean;
  supports_reasoning: boolean;
  config_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceDetail extends Workspace {
  default_model_profile_id: string | null;
}

export type TaskState =
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface Task {
  id: string;
  title: string;
  description: string;
  state: TaskState;
  priority: string;
  assigned_agent_id: string | null;
  temporal_workflow_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface Run {
  id: string;
  task_id: string | null;
  agent_id: string;
  status: string;
  model_profile_id: string | null;
  snapshot_hash: string;
  started_at: string | null;
  completed_at: string | null;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  estimated_cost_micros: number;
  steps_used: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
}

export interface RunEvent {
  id: string;
  run_id: string;
  seq: number;
  event_type: string;
  payload_json: Record<string, unknown>;
  created_at: string;
}

export interface TaskMessage {
  id: string;
  task_id: string | null;
  run_id: string | null;
  sender_type: string;
  sender_id: string | null;
  message_type: string;
  content_json: Record<string, unknown>;
  created_at: string;
}

export interface TaskDetail {
  task: Task;
  runs: Run[];
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_micros: number;
}

export interface TaskList {
  items: Task[];
  total: number;
}

export interface RunList {
  items: Run[];
  total: number;
}

// --- Phase 4: tools, grants, policies, approvals ---

export type RiskLevel = "read" | "write" | "elevated" | "destructive";
export type RuleAction = "auto" | "approval" | "forbid";
export type GrantEffect = "allow" | "deny";
export type ApprovalPreset = "autonomous" | "balanced" | "restricted";
export type ApprovalStatus = "pending" | "approved" | "rejected" | "cancelled";

export interface ToolInfo {
  name: string;
  description: string;
  risk: RiskLevel;
  required_capability: string;
  supports_approval: boolean;
  input_schema: Record<string, unknown>;
}

export interface Grant {
  id: string;
  agent_id: string;
  capability: string;
  scope_json: Record<string, unknown>;
  effect: GrantEffect;
  created_at: string;
}

export interface PolicyRule {
  capability: string;
  risk: RiskLevel | null;
  action: RuleAction;
}

export interface AgentPolicy {
  rules: PolicyRule[];
  preset: ApprovalPreset | null;
  autonomy_level: string;
}

export interface Approval {
  id: string;
  task_id: string | null;
  run_id: string | null;
  requested_by_agent_id: string | null;
  action_type: string;
  action_payload_sanitized: Record<string, unknown>;
  reason: string;
  status: ApprovalStatus;
  requested_at: string;
  decided_at: string | null;
  decided_by_user_id: string | null;
  agent_name?: string | null;
  task_title?: string | null;
}

export interface ApprovalList {
  items: Approval[];
  total: number;
  pending_count: number;
}

export interface ToolCallRecord {
  id: string;
  run_id: string;
  agent_id: string;
  tool_name: string;
  sanitized_input_json: Record<string, unknown>;
  sanitized_output_json: Record<string, unknown>;
  status: string;
  approval_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  error_code: string | null;
  created_at: string;
}
