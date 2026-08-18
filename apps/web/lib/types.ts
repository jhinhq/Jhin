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
  max_concurrent_runs: number;
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
  /** Trigger origin (Phase 7): set when a trigger started this task. */
  external_source: string | null;
  external_id: string | null;
  trigger_id: string | null;
  /** Delegation lineage (Phase 8): set on child tasks created by delegation. */
  parent_task_id: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

/** One node of a delegation chain (Phase 8). */
export interface TaskTreeNode {
  task: Task;
  agent_name: string | null;
  latest_run_status: string | null;
  children: TaskTreeNode[];
}

export interface TaskTree {
  root: TaskTreeNode;
  focus_task_id: string;
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
  scope_keys: string[];
  required_grant_scope_keys: string[];
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

// --- Phase 5: connectors and connections ---

export interface SecretFieldSpec {
  name: string;
  label: string;
  placeholder: string;
  multiline: boolean;
  required: boolean;
}

export interface AuthSchemeSpec {
  type: string;
  label: string;
  description: string;
  secret_fields: SecretFieldSpec[];
}

export interface ConfigFieldSpec {
  name: string;
  label: string;
  required: boolean;
  placeholder: string;
  help: string;
  kind: "text" | "integer" | "boolean" | "string_list";
  auth_types: string[];
  default: string | number | boolean | string[] | null;
  minimum: number | null;
  maximum: number | null;
}

export interface ConnectorInfo {
  connector_type: string;
  display_name: string;
  icon: string;
  description: string;
  auth_schemes: AuthSchemeSpec[];
  config_fields: ConfigFieldSpec[];
  webhook_events: string[];
  canonical_events: string[];
  capabilities: string[];
  supports_webhooks: boolean;
  webhook_secret_mode: "none" | "generated" | "provider_supplied";
  webhook_signature_algorithm: string;
  webhook_setup_help: string;
  docs_url: string;
}

export type ConnectionStatus = "active" | "error" | "disabled";

export interface ConnectionInfo {
  id: string;
  connector_type: string;
  name: string;
  auth_type: string;
  status: ConnectionStatus;
  public_id: string;
  config_json: Record<string, unknown>;
  created_by_user_id: string | null;
  created_at: string;
  last_verified_at: string | null;
  last_error: string | null;
  webhook_secret_configured: boolean;
}

export interface WebhookSetup {
  url_path: string;
  secret: string | null;
  secret_mode: "generated" | "provider_supplied";
  signature_algorithm: string;
  help: string;
}

export interface ConnectionGrantSummaryOut {
  grant_id: string;
  capability: string;
  effect: GrantEffect;
  scope: Record<string, string>;
  eligible_tool_names: string[];
  eligibility_reason: string | null;
}

export interface ConnectionAgentAccessOut {
  agent_id: string;
  agent_name: string;
  authorized: boolean;
  authorized_tool_names: string[];
  grants: ConnectionGrantSummaryOut[];
}

export interface ConnectionAccessSummaryOut {
  connection_id: string;
  agents: ConnectionAgentAccessOut[];
}

export interface ConnectionCreated {
  connection: ConnectionInfo;
  webhook: WebhookSetup | null;
}

export interface VerifyResult {
  ok: boolean;
  message: string;
  status: string;
  details: Record<string, string>;
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

// --- Phase 7: triggers ---

export type TriggerInvocationStatus = "started" | "duplicate" | "failed";

export interface TriggerCondition {
  path: string;
  op: string;
  value?: unknown;
}

export interface TriggerFilter {
  all?: (TriggerCondition | TriggerFilter)[];
  any?: (TriggerCondition | TriggerFilter)[];
}

export interface TriggerInvocation {
  id: string;
  trigger_id: string;
  status: TriggerInvocationStatus;
  event_id: string;
  task_id: string | null;
  workflow_id: string | null;
  error: string | null;
  created_at: string;
}

export interface Trigger {
  id: string;
  name: string;
  enabled: boolean;
  trigger_type: string;
  connection_id: string | null;
  event_type: string | null;
  filter_json: TriggerFilter;
  action_type: string;
  target_agent_id: string | null;
  target_team_id: string | null;
  action_config_json: Record<string, unknown>;
  dedupe_window_seconds: number;
  /** Workflow template selection (Phase 8): null = plain triggered flow. */
  workflow_definition: Record<string, unknown> | null;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
  last_invocation: TriggerInvocation | null;
}

export interface ConditionExplanation {
  path: string;
  op: string;
  value: unknown;
  passed: boolean;
  actual: unknown;
  actual_present: boolean;
  previous: unknown;
  previous_present: boolean;
  detail: string;
}

export interface TriggerTestResult {
  matched: boolean;
  event_type_matches: boolean;
  filter_matches: boolean;
  conditions: ConditionExplanation[];
}

/** Connector metadata for pickers, e.g. Linear teams + workflow states. */
export interface LinearTeamMetadata {
  id: string;
  key: string;
  name: string;
  states: { id: string; name: string; type: string }[];
}
