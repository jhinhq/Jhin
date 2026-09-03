/** TypeScript mirrors of the API's Pydantic contracts. */

import type { ConfigSchema } from "@/lib/config-schema";

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

/** The key a caller presented, as `/auth/identity` reports it back. */
export interface ApiKeyIdentity {
  id: string;
  name: string;
  prefix: string;
  workspace_id: string;
  role_ceiling: WorkspaceRole;
  /** Effective scopes: already capped by the ceiling and the creator's role today. */
  scopes: string[];
}

/**
 * `GET /auth/identity` — the one boot call, for either credential. A session
 * lists every workspace and leaves `api_key` null; an API key lists only the
 * workspace it is bound to and describes itself.
 */
export interface IdentityResponse {
  user: UserOut;
  memberships: MembershipOut[];
  api_key: ApiKeyIdentity | null;
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

/** What deleting a workspace would destroy, counted live by the API. Every
 *  field is a real row count, so the confirmation dialog can name what is at
 *  stake instead of warning in the abstract. */
export interface WorkspaceDeletionSummary {
  workspace_id: string;
  name: string;
  agents: number;
  teams: number;
  tasks: number;
  conversations: number;
  messages: number;
  memories: number;
  skills: number;
  personas: number;
  connections: number;
  triggers: number;
  api_keys: number;
  secrets: number;
  members: number;
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

type Availability = "available" | "unavailable";
type RelationshipKind = "close_collaborator" | "advisor" | "preferred_reviewer";

/** Directed/symmetric link between two agents (company identity, plan 2026-08-17). */
export interface AgentRelationship {
  id: string;
  workspace_id: string;
  source_agent_id: string;
  target_agent_id: string;
  kind: RelationshipKind;
  purpose: string;
  status: "active" | "inactive";
  created_at: string;
  updated_at: string;
}

/** Public identity fields shared by Agent and OrgAgentNode. Optional on the
 * client so older payloads (and fixtures) keep type-checking. */
export interface AgentIdentity {
  public_purpose?: string;
  expertise_json?: string[];
  discoverability?: "discoverable" | "hidden";
  availability?: Availability;
  relationships?: AgentRelationship[];
  /** Relative authenticated media path (append `?size=64|128|256`) or null
   * for initials. Optional so org-graph nodes and fixtures still type-check. */
  avatar_url?: string | null;
  /** Free brand-cube avatar (avatar_kind === "shape"): a fixed shape id and
   * palette hex. Both null/absent unless a shape avatar is set. */
  avatar_shape?: string | null;
  avatar_color?: string | null;
}

/** Everything needed to draw one agent's avatar (image, shape, or initials).
 * The value type of `useAgentAvatarMap`. */
export interface AgentAvatar {
  url: string | null;
  shape: string | null;
  color: string | null;
}

type AvatarKind = "initials" | "upload" | "generated" | "shape";

export interface Agent extends AgentIdentity {
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
  avatar_kind?: AvatarKind;
  active_avatar_asset_id?: string | null;
  /** The persona it wears (docs/architecture/personas.md). Optional for the
   * same reason as the `AgentIdentity` fields: fixtures keep type-checking. */
  persona_id?: string | null;
  persona?: AgentPersonaSummary | null;
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

export interface OrgAgentNode extends AgentIdentity {
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

interface AuditEvent {
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
  /** Prepaid credit the admin entered, in micro-dollars (null = not set). */
  credits_loaded_micros: number | null;
  /** Whether a billing/admin credential is attached (its value is never shown). */
  has_admin_key: boolean;
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
  /** Which source last wrote the price; null means unknown provenance, which
   *  is treated as user-entered so nothing automatic overwrites it. */
  price_source: PriceSourceName | null;
  /** True only on a self-hosted provider (Ollama, an OpenAI-compatible
   *  endpoint) with no stored price: the profile then resolves to $0 with
   *  source `self_hosted` instead of counting as unpriced. Nothing is written
   *  to the row. Optional only for fixtures: the API always sends it. */
  assumed_free?: boolean;
  supports_tools: boolean;
  supports_reasoning: boolean;
  config_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type PriceSource = "provider" | "catalog" | null;

/**
 * Where a price came from, highest authority first:
 * user-entered > measured from spend > live from the provider >
 * refreshed catalog > built-in catalog > assumed free on a self-hosted host.
 *
 * `self_hosted` is never stored on a row: it is what an unpriced profile on
 * Ollama or an OpenAI-compatible endpoint resolves to when its price is read,
 * so any real source beats it and clearing a price falls back to it.
 */
export type PriceSourceName =
  | "user"
  | "observed"
  | "provider"
  | "refreshed_catalog"
  | "catalog"
  | "self_hosted";

export interface ProviderModelEntry {
  id: string;
  input_cost_micros_per_million: number | null;
  output_cost_micros_per_million: number | null;
  context_window: number | null;
  source: PriceSource;
}

export interface ProviderModels {
  models: ProviderModelEntry[];
  detail: string | null;
  /** Year-month marker of the static price catalog (e.g. "2026-01"). */
  catalog_updated: string | null;
}

export type BalanceSource = "openrouter" | "openai_admin" | "tracked";

export interface ProviderBalance {
  tracked_spent_month_micros: number;
  tracked_spent_total_micros: number;
  provider_spent_month_micros: number | null;
  provider_remaining_micros: number | null;
  credits_loaded_micros: number | null;
  estimated_remaining_micros: number | null;
  source: BalanceSource;
  detail: string | null;
  fetched_at: string;
}

/** One model installed on an Ollama host, merged with whether it is resident
 *  right now. `loaded` and the VRAM/expiry facts are a snapshot from the same
 *  listing call; the `/ollama/loaded` poll is the fresher source. */
export interface OllamaModel {
  name: string;
  size_bytes: number;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
  modified_at: string | null;
  /** The model's maximum context from its metadata, not the runtime num_ctx. */
  context_length: number | null;
  capabilities: string[];
  loaded: boolean;
  size_vram_bytes: number | null;
  expires_at: string | null;
  /** Derived server-side from a keep_alive of -1, so the UI never has to
   *  render Ollama's "expires in centuries" sentinel. */
  keeps_loaded: boolean;
}

export interface OllamaModels {
  models: OllamaModel[];
  /** Why the list is incomplete or empty when the host could not be read. */
  detail: string | null;
  fetched_at: string;
  /** Ollama's own version when the API reports it; absent on builds that
   *  do not ask the host for it. */
  version?: string | null;
}

export interface OllamaLoadedModel {
  name: string;
  size_bytes: number;
  /** 0 on a CPU-only host: the model is resident in RAM, not VRAM. */
  size_vram_bytes: number;
  expires_at: string | null;
  keeps_loaded: boolean;
  context_length: number | null;
}

export interface OllamaLoaded {
  models: OllamaLoadedModel[];
  detail: string | null;
  fetched_at: string;
}

export interface OllamaModelDetails {
  name: string;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
  context_length: number | null;
  capabilities: string[];
  license: string | null;
}

export interface OllamaModelDetailsResult {
  model: OllamaModelDetails | null;
  detail: string | null;
}

/** The keep_alive values the UI offers: a short lease, a long one, or
 *  "-1" for Ollama's keep-forever sentinel. Unloading sends "0" on its own
 *  route and is never a menu choice. */
export type OllamaKeepAlive = "5m" | "1h" | "-1";

/** "loading" means the host is still reading the weights after the API's
 *  response budget ran out; the loaded poll flips the row when it lands. */
export type OllamaLoadStatus = "loaded" | "loading" | "unloaded" | "failed";

export interface OllamaLoadResult {
  ok: boolean;
  status: OllamaLoadStatus;
  model: string;
  keep_alive: string | null;
  detail: string;
}

export interface ProviderSpend {
  provider_id: string;
  display_name: string;
  type: ModelProviderType;
  spent_month_micros: number;
  spent_total_micros: number;
}

export interface UntrackedModel {
  model_name: string;
  runs: number;
  input_tokens: number;
  output_tokens: number;
}

export interface WorkspaceSpend {
  spent_month_micros: number;
  spent_total_micros: number;
  period_start: string;
  providers: ProviderSpend[];
  monthly_budget_micros: number | null;
  warning_threshold: number;
  fetched_at: string;
  /** Models that ran with no price set: their real cost is missing from the
   *  totals above, so the tile has to say so rather than imply completeness. */
  untracked: UntrackedModel[];
  untracked_runs: number;
  /** Spend from models that have since been deleted. It stays in the totals,
   *  so the provider breakdown needs it as its own line or it stops adding up.
   *  Optional only for fixtures: the API always sends both. */
  deleted_model_month_micros?: number;
  deleted_model_total_micros?: number;
}

export interface ProfilePricingRefresh {
  updated: boolean;
  source: PriceSourceName | null;
  detail: string;
  profile: ModelProfile;
}

export type PriceDerivation =
  | "provider_quantity"
  | "split"
  | "catalog_ratio"
  | "blended";

export type PriceConfidence = "high" | "medium" | "low";

export interface PriceCandidate {
  source: PriceSourceName;
  input_cost_micros_per_million: number | null;
  output_cost_micros_per_million: number | null;
  context_window: number | null;
  detail: string;
}

/** A rate measured from the provider's own invoice, with its evidence. */
export interface ObservedRate {
  model_key: string;
  input_cost_micros_per_million: number | null;
  output_cost_micros_per_million: number | null;
  /** Filled instead of the pair above when the provider reported one
   *  undifferentiated cost and no list price existed to split it. */
  blended_cost_micros_per_million: number | null;
  derivation: PriceDerivation;
  confidence: PriceConfidence;
  note: string;
  sample_runs: number;
  sample_input_tokens: number;
  sample_output_tokens: number;
  computed_at: string;
}

export interface ProfilePricing {
  profile_id: string;
  display_name: string;
  model_name: string;
  provider_id: string;
  provider_type: ModelProviderType;
  input_cost_micros_per_million: number | null;
  output_cost_micros_per_million: number | null;
  price_source: PriceSourceName | null;
  price_source_label: string;
  priced: boolean;
  /** Mirrors `ModelProfile.assumed_free`: priced at $0 by virtue of the
   *  provider, not by anything stored. Optional only for fixtures. */
  assumed_free?: boolean;
  pricing_page_url: string | null;
  runs_this_month: number;
  suggestion: PriceCandidate | null;
  suggestion_label: string | null;
  observed: ObservedRate | null;
}

export interface PricingStatus {
  catalog_updated: string;
  catalog_stale: boolean;
  refreshed_source: string | null;
  refreshed_fetched_at: string | null;
  refreshed_entry_count: number;
  /** MIT notice for the cached community catalog; shown wherever one of its
   *  prices is used, as the licence requires of a redistributed copy. */
  refreshed_attribution: string | null;
  refreshed_project_url: string;
  profiles: ProfilePricing[];
  untracked: UntrackedModel[];
  untracked_runs: number;
  reconcile_available: boolean;
  reconcile_detail: string;
  pricing_pages: Record<string, string>;
}

export interface AppliedPrice {
  profile_id: string;
  display_name: string;
  model_name: string;
  from_input_micros_per_million: number | null;
  from_output_micros_per_million: number | null;
  from_source: PriceSourceName | null;
  to_input_micros_per_million: number;
  to_output_micros_per_million: number;
  to_source: PriceSourceName;
  detail: string;
}

export interface DerivedRate {
  model_key: string;
  derivation: PriceDerivation;
  confidence: PriceConfidence;
  note: string;
  input_micros_per_million: number | null;
  output_micros_per_million: number | null;
  blended_micros_per_million: number | null;
  input_tokens: number;
  output_tokens: number;
  runs: number;
  cost_micros: number;
}

export interface ProviderReconcile {
  provider_id: string;
  display_name: string;
  provider_type: ModelProviderType;
  derived: DerivedRate[];
  skipped: { model_key: string; reason: string }[];
  applied: AppliedPrice[];
  period_start: string;
  period_end: string;
  billed_micros: number;
  unattributed_micros: number;
  unattributed_labels: string[];
  detail: string;
}

export interface ReconcilePricingResult {
  providers: ProviderReconcile[];
  skipped_providers: {
    provider_id: string;
    display_name: string;
    reason: string;
  }[];
  computed_at: string;
  detail: string;
}

export interface CatalogRefreshResult {
  updated: boolean;
  entry_count: number;
  fetched_at: string | null;
  source: string;
  source_url: string;
  attribution: string;
  detail: string;
  repriced: AppliedPrice[];
}

export interface WorkspaceDetail extends Workspace {
  default_model_profile_id: string | null;
  settings_json?: Record<string, unknown>;
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

interface Run {
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
type ApprovalStatus = "pending" | "approved" | "rejected" | "cancelled";

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

interface SecretFieldSpec {
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

/** `needs_reauth` is an OAuth connection whose tokens can no longer be
 * refreshed: the setup is intact, the permission is not. */
type ConnectionStatus = "active" | "error" | "disabled" | "needs_reauth";

/** Whose provider account an OAuth connection acts with. Every agent granted
 * the connection inherits that person's permissions, so the name is shown. */
export interface UserSummary {
  /** Mirrors the API's `ConnectionAuthorizedByOut.user_id`, not `id`. */
  user_id: string;
  display_name: string;
}

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
  /** OAuth only, and absent on an API that predates the OAuth work. */
  authorized_by?: UserSummary | null;
  oauth_expires_at?: string | null;
  needs_reauth?: boolean;
}

export interface WebhookSetup {
  url_path: string;
  secret: string | null;
  secret_mode: "generated" | "provider_supplied";
  signature_algorithm: string;
  help: string;
}

interface ConnectionGrantSummaryOut {
  grant_id: string;
  capability: string;
  effect: GrantEffect;
  scope: Record<string, string>;
  eligible_tool_names: string[];
  eligibility_reason: string | null;
}

interface ConnectionAgentAccessOut {
  agent_id: string;
  agent_name: string;
  authorized: boolean;
  authorized_tool_names: string[];
  grants: ConnectionGrantSummaryOut[];
}

/** What deleting the connection would take with it, so the confirmation
 * can name the cost. Triggers and their run history cascade off the row. */
export interface ConnectionDeleteImpact {
  trigger_count: number;
  trigger_invocation_count: number;
}

export interface ConnectionAccessSummaryOut {
  connection_id: string;
  agents: ConnectionAgentAccessOut[];
  /** Absent only when an older API is still serving this route. */
  delete_impact?: ConnectionDeleteImpact;
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

// --- OAuth connect (docs/architecture/oauth.md) ---

/**
 * How this app should be connected, as the server's probe decided it.
 *
 * The probe asks the server rather than trusting the catalog's `auth_hint`,
 * so this is the one signal the connect flow routes on.
 */
export type OAuthConnectMethod =
  | "oauth_discovery"
  | "oauth_static"
  | "device_code"
  | "oauth_needs_client"
  | "api_key";

/** `POST /oauth/probe`. Carries no credential material of any kind: `reason`
 * is a Jhin-authored constant, never text the provider wrote. */
export interface OAuthProbeOut {
  method: OAuthConnectMethod;
  supports_oauth: boolean;
  supports_dcr: boolean;
  issuer: string;
  /** Host only — what the consent sentence names. */
  authorization_server_display: string;
  scopes: string[];
  resource: string;
  /** A usable client registration already exists for this workspace. */
  client_configured: boolean;
  requires_client_secret: boolean;
  reason: string;
}

/** `POST /oauth/start` and `POST /connections/{id}/reauthorize`. The URL is
 * the provider's authorization endpoint; it holds no secret. */
export interface OAuthStartOut {
  authorization_url: string;
  state_expires_at: string;
  issuer: string;
  scopes: string[];
  resource: string;
  authorized_as_user_id: string;
  client_source: "dcr" | "manual" | "static";
}

/** `POST /oauth/device/start`. `handle` is the opaque poll handle — never the
 * device code, which the browser is not trusted with. */
export interface OAuthDeviceStartOut {
  handle: string;
  user_code: string;
  verification_uri: string;
  verification_uri_complete: string | null;
  expires_at: string;
  interval_seconds: number;
}

export interface OAuthDevicePollOut {
  status: "pending" | "slow_down" | "connected" | "denied" | "expired";
  interval_seconds: number | null;
  connection: ConnectionInfo | null;
}

/** One registered OAuth app, per workspace per authorization server. The
 * secret is never returned — only whether one is stored. */
export interface OAuthClientOut {
  id: string;
  issuer: string;
  redirect_uri: string;
  client_id: string;
  client_secret_configured: boolean;
  token_endpoint_auth_method: string;
  source: "dcr" | "manual" | "static";
  scopes: string;
  created_at: string;
  last_used_at: string | null;
  connection_count: number;
}

export interface OAuthClientCreate {
  issuer: string;
  client_id: string;
  client_secret?: string | null;
  token_endpoint_auth_method?: "none" | "client_secret_post" | "client_secret_basic";
  scopes?: string;
}

/** `GET /oauth/redirect-uri` — the one callback URL this instance registers
 * with every provider, computed from settings. */
export interface OAuthRedirectOut {
  redirect_uri: string;
  github_app_redirect_uri: string;
  is_https: boolean;
  is_loopback: boolean;
  configured_via: "OAUTH_REDIRECT_BASE_URL" | "APP_URL";
}

/** `POST /oauth/github-app/manifest`. The browser POSTs `manifest` and
 * `state` to `post_url` as an ordinary form; GitHub creates the app. */
export interface GitHubAppManifestOut {
  post_url: string;
  manifest: Record<string, unknown>;
  state: string;
  expires_at: string;
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

type TriggerInvocationStatus = "started" | "duplicate" | "failed";

export interface TriggerCondition {
  path: string;
  op: string;
  value?: unknown;
}

interface TriggerFilter {
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
  /** The same outcome in words. `error` is an internal code; show this. */
  error_message: string | null;
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
  /** "ok", "agent_deleted", "agent_paused" or "team_unstaffed". */
  target_state: string;
  /** Why this automation cannot run, and what to do about it. */
  target_warning: string | null;
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

// --- Conversations, activity, attention (docs/architecture/conversations.md) ---

type ConversationStatus = "active" | "archived";

export interface Conversation {
  id: string;
  workspace_id: string;
  title: string;
  status: ConversationStatus;
  pinned: boolean;
  primary_agent_id: string | null;
  created_by_user_id: string | null;
  last_activity_at: string;
  created_at: string;
  updated_at: string;
  active_task_id: string | null;
  active_task_state: TaskState | null;
  active_run_status: string | null;
  /**
   * A finished sentence for what the agent is doing right now ("Saving this to
   * memory"), written by the API from the newest tool call — never assembled
   * here, so the tool vocabulary lives in one place and no tool argument can
   * reach a label. Null between steps, and on the conversation *list*, which
   * does not pay for it per row.
   */
  active_activity: string | null;
  last_message_preview: string | null;
  last_message_sender_type: string | null;
  agent_name: string | null;
  agent_role_title: string | null;
  task_count: number;
}

export interface ConversationList {
  items: Conversation[];
  total: number;
}

export interface ConversationUpdate {
  title?: string;
  pinned?: boolean;
  status?: ConversationStatus;
}

export interface ConversationMessage extends TaskMessage {
  conversation_id: string | null;
  sender_name: string | null;
  agent_id: string | null;
}

export interface TurnOut {
  conversation: Conversation;
  message: ConversationMessage;
  task_id: string;
  mode: "new_task" | "instruction";
}

// --- Questions an agent asks the person (ask-user contract §5) ---

export type UserQuestionStatus = "pending" | "answered" | "expired" | "cancelled";

export interface UserQuestionOption {
  value: string;
  label: string;
  detail: string;
}

/**
 * The `content_json` of a `message_type: "question"` row whose `kind` is
 * `"user_question"`. It arrives on the ordinary messages poll and is
 * **mutated in place** when the question is answered, expires, or is
 * cancelled: same `Message.id`, different content. Nothing that reads it may
 * memoise on the id alone.
 */
export interface UserQuestionContent {
  kind: "user_question";
  question_id: string;
  question: string;
  context: string;
  question_kind: "open" | "memory_scope";
  options: UserQuestionOption[];
  allow_other: boolean;
  other_label: string;
  other_placeholder: string;
  status: UserQuestionStatus;
  expires_at: string;
  asked_by_agent_name: string;
  /** Present only once `status === "answered"`. */
  answer_kind?: "option" | "other";
  answer_option_value?: string;
  answer?: string;
  answered_by_name?: string;
  answered_at?: string;
}

/** Exactly one of the two, never both, never neither. */
export type AnswerQuestionIn = { option_value: string } | { other_text: string };

/**
 * The `content_json` of the `message_type: "status"` row a memory tool writes
 * when a record was **actually stored** — the receipt behind an agent saying
 * it remembered something. A refused proposal writes no such row, and neither
 * does the automatic post-run extraction: this shape only ever describes a
 * write the agent made deliberately, in the conversation, where the person can
 * see it and correct it.
 *
 * `scope_label` is written by the platform from the scope and the real team
 * name, never by the model — mislabelling the audience of a memory is the
 * exact bug this card exists to catch.
 */
export interface MemorySavedContent {
  kind: "memory_saved";
  memory_id: string;
  /** "updated" means it superseded an earlier record. */
  action: "saved" | "updated";
  /** Null when the payload carried a scope this build doesn't know. */
  scope: MemoryScope | null;
  /** "just you and me" | "the Platform team" | "everyone in the workspace". */
  scope_label: string;
  /** The remembered words, as stored. */
  content: string;
  /** The words it replaced, or "". */
  superseded: string;
  /** An older memory on the same subject this one did NOT replace, so the
   * agent will now recall both. Empty when there is no conflict. */
  still_standing: string;
}

export interface QuestionOut {
  id: string;
  workspace_id: string;
  conversation_id: string | null;
  task_id: string | null;
  message_id: string | null;
  agent_id: string;
  agent_name: string | null;
  kind: "open" | "memory_scope";
  question: string;
  context: string;
  options: UserQuestionOption[];
  allow_other: boolean;
  status: UserQuestionStatus;
  asked_at: string;
  expires_at: string;
  answered_at: string | null;
  answered_by_user_id: string | null;
  answered_by_name: string | null;
  answer_kind: "" | "option" | "other";
  answer_option_value: string;
  answer_text: string;
  granted_scope: "" | "agent" | "team" | "workspace";
  grant_denied_reason: string;
}

export interface AnswerQuestionOut {
  question: QuestionOut;
  /** False when the answer was recorded but the run had already stopped
   * waiting — the person is told to send it as a message instead. */
  resumed: boolean;
}

export interface ConversationAgentSummary {
  id: string;
  name: string;
  role_title: string;
  status: AgentStatus;
  availability: Availability;
  public_purpose: string;
  /** The persona it wears, for the chat header; present even when switched off. */
  persona?: AgentPersonaSummary | null;
}

export interface ConversationDetail {
  conversation: Conversation;
  agent: ConversationAgentSummary | null;
  tasks: Task[];
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_micros: number;
  pending_approvals: Approval[];
}

export type ActivityKind =
  | "started"
  | "asked_agent"
  | "reported"
  | "escalated"
  | "status_update"
  | "needs_review"
  | "finished"
  | "failed"
  | "paused"
  | "stopped"
  | "queued";

export interface ActivityCard {
  id: string;
  kind: ActivityKind;
  label: string;
  actor_type: "agent" | "user" | "system";
  actor_agent_id: string | null;
  actor_agent_name: string | null;
  target_agent_id: string | null;
  target_agent_name: string | null;
  task_id: string | null;
  task_title: string | null;
  root_task_id: string | null;
  conversation_id: string | null;
  approval_id: string | null;
  work_request_id?: string | null;
  review_id?: string | null;
  summary: string;
  detail_json: Record<string, unknown>;
  created_at: string;
}

export interface ActivityList {
  items: ActivityCard[];
  next_before: string | null;
}

export interface Attention {
  pending_approvals: Approval[];
  failed_tasks: Task[];
  waiting_conversations: Conversation[];
  /** Work reviews assigned to a human (coordination release). Optional so
   * older payloads keep working. */
  pending_reviews?: WorkReview[];
  /** Pending reviews an AI colleague is handling; a person can step in. */
  reviews_in_progress?: WorkReview[];
  /** Workspace model spend crossed the budget warning threshold. */
  budget?: {
    monthly_budget_micros: number;
    spent_month_micros: number;
    percent_used: number;
  } | null;
  counts: {
    approvals: number;
    failures: number;
    reviews?: number;
    reviews_in_progress?: number;
    budget_warnings?: number;
    total: number;
  };
}

export interface AcknowledgeFailuresResult {
  acknowledged: number;
  task_ids: string[];
}

// --- Memory (docs/architecture/memory.md) ---

export type MemoryScope = "agent" | "team" | "workspace";
export type MemoryKind = "fact" | "preference" | "decision" | "procedure" | "context" | "other";
export type MemoryStatus =
  | "proposed"
  | "active"
  | "contested"
  | "superseded"
  | "rejected"
  | "forgotten";
type MemorySensitivity = "normal" | "sensitive" | "redacted";

export interface MemoryRecord {
  id: string;
  workspace_id: string;
  scope: MemoryScope;
  scope_id: string;
  kind: MemoryKind;
  subject: string | null;
  content: string;
  source_conversation_id: string | null;
  source_message_id: string | null;
  source_task_id: string | null;
  source_event_id: string | null;
  visibility: string;
  sensitivity: MemorySensitivity;
  confidence: number;
  importance: number;
  tags_json: string[];
  status: MemoryStatus;
  valid_from: string | null;
  expires_at: string | null;
  pinned_at: string | null;
  forgotten_at: string | null;
  version: number;
  supersedes_id: string | null;
  has_embedding: boolean;
  embedding_model: string | null;
  created_by_type: "user" | "agent" | "system" | string;
  created_by_id: string | null;
  policy_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface MemoryList {
  items: MemoryRecord[];
  total: number;
}

// --- Media / avatars (docs/architecture/media.md) ---

export type AvatarVariantSize = 64 | 128 | 256;

export interface AvatarOut {
  agent_id: string;
  workspace_id: string;
  avatar_kind: AvatarKind;
  active_avatar_asset_id: string | null;
  avatar_url: string | null;
  avatar_shape: string | null;
  avatar_color: string | null;
  initials: string;
}

export interface ProviderDisclosure {
  provider_type: string;
  provider_display_name: string;
  model_profile_id: string | null;
  model_name: string;
  image_size: string;
  /** Micro-dollars per image; null = unknown. */
  estimated_cost_micros: number | null;
  sends_public_identity: boolean;
}

type AvatarGenerationStatus = "queued" | "running" | "succeeded" | "failed";

export interface AvatarGenerationOut {
  id: string;
  workspace_id: string;
  agent_id: string;
  status: AvatarGenerationStatus;
  prompt: string;
  prompt_hint: string;
  disclosure: ProviderDisclosure;
  error: string | null;
  error_code: string | null;
  result_asset_id: string | null;
  result_avatar_url: string | null;
  temporal_workflow_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

// --- Coordination (docs/architecture/coordination.md) ---

/** Public identity allowlist returned by GET /directory. */
export interface DirectoryEntry {
  id: string;
  name: string;
  slug: string;
  role_title: string;
  public_purpose: string;
  expertise: string[];
  availability: Availability | string;
  primary_team_id: string | null;
  primary_team_name: string | null;
  manager_agent_id: string | null;
}

export type WorkRequestStatus =
  | "pending"
  | "clarification_requested"
  | "accepted"
  | "declined"
  | "completed"
  | "failed";

export interface WorkRequest {
  id: string;
  workspace_id: string;
  conversation_id: string | null;
  requester_agent_id: string;
  requester_task_id: string | null;
  requester_run_id: string | null;
  root_task_id: string | null;
  requested_by_user_id: string | null;
  target_agent_id: string;
  title: string;
  description: string;
  expected_output: string;
  status: WorkRequestStatus;
  idempotency_key: string;
  depth: number;
  created_task_id: string | null;
  response: string;
  responded_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  requester_agent_name?: string | null;
  target_agent_name?: string | null;
}

export interface WorkRequestList {
  items: WorkRequest[];
  total: number;
}

export type ReviewMode = "pre_action" | "before_close" | "post_action" | "periodic";
export type ReviewScopeKind = "workspace" | "team" | "agent" | "task_type";
export type ReviewConditionKind =
  | "elevated_action"
  | "destructive_action"
  | "cost_threshold"
  | "token_threshold"
  | "time_threshold"
  | "tool_failure"
  | "test_failure"
  | "approval_denied"
  | "policy_denied"
  | "blocked"
  | "low_confidence"
  | "cross_team_request"
  | "explicit_request"
  | "always";
export type ReviewerKind = "reporting_manager" | "agent" | "team_role" | "human";

export interface ReviewCondition {
  kind: ReviewConditionKind;
  /** cost: micro-dollars; tokens: count; time: seconds; confidence: 0..1. */
  threshold?: number | null;
}

export interface ReviewerSelector {
  kind: ReviewerKind;
  agent_id?: string | null;
  role_label?: string | null;
  fallback_agent_id?: string | null;
  fallback_to_human?: boolean;
}

export interface ReviewPolicy {
  id: string;
  workspace_id: string;
  name: string;
  scope_kind: ReviewScopeKind;
  scope_id: string | null;
  scope_key: string | null;
  enabled: boolean;
  mode: ReviewMode;
  conditions_json: ReviewCondition[];
  reviewer_selector_json: ReviewerSelector;
  fail_closed: boolean;
  priority: number;
  period_seconds: number | null;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReviewPolicyIn {
  name: string;
  scope_kind: ReviewScopeKind;
  scope_id?: string | null;
  scope_key?: string | null;
  enabled: boolean;
  mode: ReviewMode;
  conditions: ReviewCondition[];
  reviewer: ReviewerSelector;
  fail_closed: boolean;
  priority: number;
  period_seconds?: number | null;
}

type WorkReviewStatus = "pending" | "approved" | "changes_requested" | "skipped" | "escalated";
export type ReviewVerdict = "approve" | "changes_requested" | "escalate";

export interface WorkReview {
  id: string;
  workspace_id: string;
  policy_id: string | null;
  task_id: string | null;
  run_id: string | null;
  tool_call_id: string | null;
  work_request_id: string | null;
  subject_agent_id: string | null;
  trigger_key: string;
  mode: ReviewMode;
  evidence_json: Record<string, unknown>;
  reviewer_type: "agent" | "human" | "none";
  reviewer_agent_id: string | null;
  reviewer_user_id: string | null;
  status: WorkReviewStatus;
  verdict: ReviewVerdict | null;
  feedback: string;
  requested_at: string;
  decided_at: string | null;
  decided_by_user_id: string | null;
  decided_by_agent_id: string | null;
  created_at: string;
  subject_agent_name?: string | null;
  reviewer_agent_name?: string | null;
  task_title?: string | null;
  /** The tool call parked on this review (pre-action gates), if any. */
  parked_tool_name?: string | null;
  parked_tool_call_status?: string | null;
}

interface RollupReport {
  agent_id: string;
  name: string;
  role_title: string;
  depth: number;
  status: AgentStatus | string;
  availability: Availability | string;
  active_tasks: number;
  queued_tasks: number;
  active_runs: number;
  max_concurrent_runs: number;
}

export interface RollupItem {
  kind: "task" | "run" | "approval" | "review" | "work_request" | string;
  source_id: string;
  agent_id: string | null;
  agent_name: string | null;
  title: string;
  status: string;
  summary: string;
  occurred_at: string;
  task_id: string | null;
  conversation_id: string | null;
  artifacts: Record<string, unknown>[];
  risks: string[];
}

interface RollupQueue {
  active_runs: number;
  queued_tasks: number;
  waiting_approval: number;
  waiting_delegation: number;
  open_work_requests: number;
}

export interface ManagerRollup {
  manager_agent_id: string;
  generated_at: string;
  window_start: string;
  reports: RollupReport[];
  active_work: RollupItem[];
  recent_work: RollupItem[];
  blocked_or_failed: RollupItem[];
  pending_reviews: RollupItem[];
  pending_approvals: RollupItem[];
  outcomes: RollupItem[];
  open_work_requests: RollupItem[];
  queue: RollupQueue;
  source_ids: string[];
  truncated: boolean;
}

// --- Apps library and per-connection tools (docs/architecture/mcp.md) ---

export type CatalogAuthHint = "none" | "bearer" | "header" | "oauth";

export interface CatalogApp {
  slug: string;
  name: string;
  category: string;
  icon: string;
  description: string;
  /** Native Jhin connector type when one exists (github, linear, …). */
  connector_type: string | null;
  /** Official remote MCP endpoint when known. */
  mcp_url: string | null;
  url_unverified: boolean;
  transport: "streamable_http" | "sse" | "unknown";
  auth_hint: CatalogAuthHint;
  auth_note: string;
  docs_url: string;
  setup_note: string;
  stdio_only: boolean;
  /** Non-secret connection config pre-filled for a native connector. */
  connector_config: Record<string, string>;
  /** Same-origin icon-proxy path when a real logo exists; never an upstream URL. */
  logo_url?: string | null;
}

// --- The synced app/skill catalog (docs/architecture/catalog.md) ---
//
// A second, much larger index sitting behind the 50 curated entries above:
// MCP servers and agent skills discovered from public registries, refreshed
// by the `jhin-catalog-sync` job. Everything here is an index entry, not a
// connection — nothing is dialled until somebody presses Connect. `source`
// says which half a row came from, so a synced entry can never pass itself
// off as a curated one.

export type CatalogKind = "mcp" | "skill";
export type CatalogSource = "builtin" | "synced";
export type CatalogTrustTier =
  | "curated"
  | "registry_verified"
  | "smithery_verified"
  | "reviewed"
  | "indexed";

export interface CatalogEntry {
  slug: string;
  kind: CatalogKind;
  source: CatalogSource;
  name: string;
  summary: string;
  category: string;
  icon: string;
  trust_tier: CatalogTrustTier;
  /** The floor this entry's provenance justifies, not an observed behaviour. */
  default_risk: RiskLevel;
  popularity: number;
  connector_type: string | null;
  mcp_url: string | null;
  url_unverified: boolean;
  transport: "streamable_http" | "sse" | "unknown";
  auth_hint: CatalogAuthHint;
  stdio_only: boolean;
  deprecated: boolean;
  connectable: boolean;
  docs_url: string;
  /** Same-origin icon-proxy path when a real logo exists; never an upstream URL. */
  logo_url?: string | null;
}

export interface CatalogMcpDetail {
  tool_count: number | null;
  registry_name: string;
  npm_package: string;
  verified_upstream: boolean;
  package_identifiers: string[];
  remote_urls: string[];
}

export interface CatalogSkillDetail {
  skill_name: string;
  source_ref: string;
  skill_path: string;
  commit_sha: string;
  marketplace: string;
  plugin: string;
  model_invocable: boolean;
  allowed_tools: string[];
}

export interface CatalogEntryDetail extends CatalogEntry {
  description: string;
  homepage: string;
  auth_note: string;
  setup_note: string;
  license: string;
  tags: string[];
  connector_config: Record<string, string>;
  sources: { source_id: string; upstream_id: string; url: string }[];
  /** The safe render contract for the Connect form; null when unavailable. */
  config_schema: ConfigSchema | null;
  mcp: CatalogMcpDetail | null;
  skill: CatalogSkillDetail | null;
}

export interface CatalogFacetBucket {
  value: string;
  label: string;
  count: number;
}

export interface CatalogFacets {
  kind: CatalogFacetBucket[];
  category: CatalogFacetBucket[];
  trust_tier: CatalogFacetBucket[];
  transport: CatalogFacetBucket[];
  auth_hint: CatalogFacetBucket[];
  total: number;
}

export interface CatalogVersion {
  release_tag: string;
  source_repo: string;
  data_sha256: string;
  entry_count: number;
  mcp_count: number;
  skill_count: number;
  activated_at: string | null;
}

export interface CatalogSearchResult {
  items: CatalogEntry[];
  total: number;
  /** Null until the first catalog release has been synced and activated. */
  version: CatalogVersion | null;
}

export interface CatalogSearchParams {
  q?: string;
  kind?: CatalogKind;
  category?: string;
  trust_tier?: CatalogTrustTier;
  transport?: string;
  auth_hint?: string;
  connectable?: boolean;
  include_indexed?: boolean;
  limit?: number;
  offset?: number;
}

export interface RiskFloorApplied {
  connection_id: string;
  floor: RiskLevel;
  tools_raised: number;
  tools_unchanged: number;
}

export interface ConnectionToolInfo {
  name: string;
  provider_name: string | null;
  description: string;
  risk: RiskLevel;
  derived_risk: RiskLevel | null;
  risk_override: RiskLevel | null;
  annotations: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  schema_truncated: boolean;
  supports_approval: boolean;
  scope_keys: string[];
}

export interface ConnectionToolsOut {
  connection_id: string;
  connector_type: string;
  /** True when tools are discovered per connection (MCP). */
  dynamic: boolean;
  capability_pattern: string | null;
  discovered_at: string | null;
  tools: ConnectionToolInfo[];
}

// --- Agent Skills (docs/architecture/skills.md) ---

export type SkillSource = "built_in" | "imported" | "custom" | "agent_authored";

export interface SkillFileEntry {
  path: string;
  content: string;
}

export interface Skill {
  id: string;
  workspace_id: string;
  name: string;
  description: string;
  source: SkillSource;
  source_url: string;
  enabled: boolean;
  version: number;
  file_count: number;
  // The API always coalesces a missing/null category to "General" before
  // returning it, so this is never blank in a response.
  category: string;
  created_by_agent_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface SkillDetail extends Skill {
  content: string;
  files: SkillFileEntry[];
}

export interface SkillList {
  items: Skill[];
  total: number;
}

export interface ImportedSkill {
  name: string;
  description: string;
  status: "proposed" | "skipped";
  reason: string;
}

export interface SkillImportResult {
  created: number;
  skipped: number;
  skills: ImportedSkill[];
  warnings: string[];
}

export interface InstallBuiltinsResult {
  installed: number;
  skipped: number;
  names: string[];
}

export interface AgentSkillInfo {
  skill_id: string;
  name: string;
  category: string;
  description: string;
  source: SkillSource;
  enabled: boolean;
  enabled_for_agent: boolean;
}

// --- Skills browse gallery (docs/architecture/skills.md) ---

export interface SkillSourceInfo {
  source: string;
  label: string;
  description: string;
  url: string;
  // False for the hardcoded defaults; true for a workspace admin's own
  // addition (only a custom entry can be removed).
  custom: boolean;
}

export interface SkillSourceCreateInput {
  source: string;
  label?: string;
  description?: string;
}

export interface BrowseSkillEntry {
  source: string;
  name: string;
  description: string;
  path: string;
  installed: boolean;
  // Computed the same way an install would derive it — display/filter only.
  category: string;
}

export interface BrowseListResult {
  source: string;
  skills: BrowseSkillEntry[];
}

export interface BrowseInstallResult {
  skill: Skill;
  status: "installed" | "already_installed";
}

/** Installing from the reviewed catalog reuses the browse-install response. */
export type CatalogInstallResult = BrowseInstallResult;

// --- Personas (docs/architecture/personas.md) ---

export type PersonaSource = "built_in" | "custom" | "agent";

/** The eight facets as the API stores them. Only `voice` is required by the
 * API; the rest come back as "" when unset. */
export interface PersonaFacets {
  voice: string;
  stance: string;
  pace: string;
  when_unsure: string;
  with_people: string;
  with_teammates: string;
  signature: string;
  never: string[];
}

export interface Persona {
  id: string;
  workspace_id: string;
  name: string;
  display_name: string;
  description: string;
  tags: string[];
  source: PersonaSource;
  facets: PersonaFacets;
  enabled: boolean;
  version: number;
  /** True for the shipped cast: Duplicate instead of Edit, never Delete. */
  read_only: boolean;
  /** Agents wearing it (counted by the API on every list/detail call). */
  agent_count: number;
  created_by_user_id: string | null;
  created_by_agent_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface PersonaList {
  items: Persona[];
  total: number;
}

export interface PersonaCreateInput {
  name: string;
  display_name: string;
  description: string;
  tags: string[];
  facets: PersonaFacets;
}

/** PATCH body: omitted = unchanged; `facets` replaces the whole object. */
export interface PersonaUpdateInput {
  display_name?: string;
  description?: string;
  tags?: string[];
  facets?: PersonaFacets;
  enabled?: boolean;
}

export interface PersonaDuplicateInput {
  name?: string;
  display_name?: string;
}

export interface InstallBuiltinPersonasResult {
  installed: number;
  refreshed: number;
  skipped: number;
  names: string[];
}

/** The persona an agent wears, as `Agent.persona` and the chat header's agent
 * summary carry it. Present even when `enabled` is false so the UI can say
 * "worn but switched off". */
export interface AgentPersonaSummary {
  id: string;
  name: string;
  display_name: string;
  tags: string[];
  enabled: boolean;
}

/* --- People, invitations, and API keys (docs/architecture/rbac.md) --- */

export type InvitationStatus = "pending" | "accepted" | "revoked" | "expired";

export interface Invitation {
  id: string;
  email: string;
  role: WorkspaceRole;
  status: InvitationStatus;
  invited_by_user_id: string | null;
  invited_by_name: string | null;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

/** The invite link comes back exactly once, at creation. */
export interface InvitationCreated {
  invitation: Invitation;
  invite_url: string;
  token: string;
}

export interface InvitationPreview {
  workspace_name: string;
  email: string;
  role: WorkspaceRole;
  expires_at: string;
}

export interface ScopeInfo {
  key: string;
  category: string;
  action: string;
  label: string;
  description: string;
  min_role: WorkspaceRole;
  /** False when your own role may not grant this scope. */
  available: boolean;
}

export interface ScopeCategoryInfo {
  key: string;
  label: string;
  description: string;
  scopes: ScopeInfo[];
}

export interface ScopeCatalog {
  your_role: WorkspaceRole;
  categories: ScopeCategoryInfo[];
}

export type ApiKeyStatus = "active" | "revoked" | "expired";
export type ExpiryUnit = "minutes" | "hours" | "days" | "never";

export interface ApiKeyInfo {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  role_ceiling: WorkspaceRole;
  created_by_user_id: string | null;
  created_by_name: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
  status: ApiKeyStatus;
}

/** `key` is the only time the secret exists outside the caller's clipboard. */
export interface ApiKeyCreated {
  api_key: ApiKeyInfo;
  key: string;
}

export interface ApiKeyUsageEntry {
  id: string;
  api_key_id: string;
  api_key_name: string | null;
  api_key_prefix: string | null;
  acting_user_id: string | null;
  acting_user_name: string | null;
  method: string;
  path: string;
  status_code: number;
  created_at: string;
}

export interface ApiKeyUsagePage {
  items: ApiKeyUsageEntry[];
  total: number;
}

/**
 * How far one person got through this workspace's first-run introduction.
 * Only `pending` opens the tour by itself; every other state leaves it
 * available on demand but silent.
 */
export type OnboardingStatus = "pending" | "in_progress" | "dismissed" | "completed";

export interface OnboardingState {
  status: OnboardingStatus;
  last_step: string | null;
  updated_at: string | null;
}
