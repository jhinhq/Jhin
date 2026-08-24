# Open-Source AI Organization Platform — Product & Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-ready, self-hosted, open-source platform for creating hierarchical teams of autonomous AI agents that can securely use external systems, react to triggers, delegate work, execute long-running workflows, and expose all activity through a polished web application.

**Architecture:** Temporal is the durable workflow authority, NATS JetStream is the asynchronous event backbone, LangGraph is the agent reasoning/orchestration layer inside individual agent runs, and PostgreSQL is the system of record. A FastAPI control-plane API owns configuration and authorization; isolated execution workers own risky tools such as shell/CLI and repository work; a Next.js frontend provides organization design, agent configuration, runs, approvals, connectors, triggers, audit trails, and observability.

**Tech Stack:** Python 3.13+, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, PostgreSQL 17+, Temporal, NATS + JetStream, LangGraph, Next.js 15+, React, TypeScript, Tailwind CSS, shadcn/ui, TanStack Query, Zod, Docker/Compose, OpenTelemetry, Prometheus-compatible metrics, structured JSON logging, Playwright, pytest, Vitest, optional S3-compatible object storage, and provider adapters for OpenAI/Anthropic/OpenRouter/Ollama-compatible APIs.

---

## 0. Working Project Name

Do **not** hard-code the product name throughout the codebase. Use `APP_NAME`/branding configuration so the project can be renamed before release.

### Product name

1. **Jhin** — strongest overall fit: create and operate an AI organization.
2. **AgentGuild** — emphasizes teams, roles, specialists, and hierarchy.
3. **Workmesh** — emphasizes a network of workers, tools, events, and workflows.
4. **Crewframe** — organizational framework for autonomous crews.
5. **TaskFoundry** — work enters, specialized agents turn it into outcomes.
6. **Operant** — compact, technical, and focused on agents that act.
7. **OrgPilot** — approachable and easy to understand.
8. **AgentWorks** — straightforward open-source positioning.
9. **Guildstack** — good if the developer/open-source audience is primary.
10. **Delegant** — “delegate” + “agent”; memorable but less immediately obvious.

**Decision:** the product is **Jhin**. Keep the user-facing name configurable through `APP_NAME`/branding settings, and complete package-registry, trademark, and domain checks before public launch.

---

# 1. Product Vision

Jhin is a self-hostable operating system for teams of AI workers.

A user should be able to model an organization such as:

```text
Owner / CEO
│
├── Engineering
│   └── CTO Agent
│       ├── Senior Software Engineer Agent
│       ├── QA Engineer Agent
│       └── DevOps Engineer Agent
│
└── Marketing
    └── Marketing Director Agent
        ├── Blogger Agent
        ├── SEO Agent
        └── Social Media Agent
```

Agents are not merely independent chats. They have:

- roles and system instructions;
- reporting relationships;
- team membership;
- scoped permissions;
- tool/connector access;
- optional per-agent model/provider configuration;
- budgets and execution limits;
- durable task state;
- private and shared memory;
- explicit delegation capabilities;
- human approval requirements;
- event subscriptions/triggers;
- auditable messages, decisions, tool calls, and outputs.

The platform must support both:

1. **autonomous event-driven work**, such as a Linear issue entering Todo; and
2. **interactive work**, such as a user messaging the CTO or manually assigning a task.

The platform should make agent activity understandable. A user must be able to answer:

- What is running?
- Why did it start?
- Which agent owns it?
- What has it done?
- What did it spend?
- What credentials/tools did it use?
- Which other agent did it delegate to?
- What is it waiting for?
- Why did it fail?
- Can I pause/cancel/retry/approve it?
- What changed in GitHub/Linear/Vercel/Supabase because of it?

---

# 2. Core Product Principles

## 2.1 Durable work, not long-running HTTP requests

No meaningful autonomous job should depend on an API process staying alive.

Temporal owns durable business workflows. An engineering ticket may run for minutes, days, or weeks and must survive:

- worker restarts;
- host reboots;
- network outages;
- model-provider timeouts;
- connector outages;
- deployments;
- human approval delays.

## 2.2 Events are facts, workflows are decisions

NATS carries facts such as:

```text
connector.linear.issue.updated
task.created
agent.run.started
agent.run.completed
tool.call.completed
approval.requested
github.pull_request.opened
deployment.completed
```

Temporal decides what durable process should happen because of those facts.

Do not encode the company workflow solely as chained NATS messages. That creates implicit distributed state and makes recovery difficult.

## 2.3 PostgreSQL is the source of truth

NATS is not the primary database.

Temporal is not the product database.

LangGraph checkpoints are not the organization database.

Postgres owns persistent product entities such as users, workspaces, teams, agents, connectors, triggers, tasks, runs, permissions, approvals, budgets, audit events, and secrets metadata.

## 2.4 Agents never receive plaintext credentials in prompts

Credentials are resolved at tool-execution time.

The model may see:

```json
{
  "tool": "github.create_pull_request",
  "connection": "engineering-github"
}
```

It must not see:

```text
ghp_xxxxxxxxx
```

Secrets live in an encrypted secret store and are exposed only to the connector executor that needs them.

## 2.5 Least privilege by default

Access is deny-by-default.

An agent receives only:

- explicitly granted tools;
- explicitly granted connector instances;
- allowed repositories/projects/teams;
- allowed operations;
- optional environment scopes;
- optional time/budget limits.

## 2.6 Separate reasoning from execution

The model decides that a tool should be invoked.

A deterministic tool gateway validates:

- schema;
- authorization;
- policy;
- approvals;
- rate limits;
- budget;
- target scope.

Only then is execution allowed.

## 2.7 Human controls are always available

Every long-running workflow supports:

- pause;
- resume;
- cancel;
- retry;
- reassign;
- inject instruction;
- approve/reject;
- inspect logs.

## 2.8 Open-source first

A fresh clone should be runnable with Docker Compose and no proprietary infrastructure dependency.

Managed cloud providers may be supported, but the core product must remain useful when entirely self-hosted.

---

# 3. Scope

## 3.1 Version 1 must support

- multiple workspaces;
- users and local authentication;
- teams;
- nested manager/worker relationships;
- bot/agent creation;
- agent templates;
- per-agent model selection;
- provider configuration;
- tool permissions;
- secure credential storage;
- GitHub integration;
- Linear integration;
- Vercel integration;
- Supabase integration;
- local/sandboxed CLI execution;
- generic HTTP connector;
- inbound webhooks;
- scheduled triggers;
- connector event triggers;
- Linear “issue enters Todo” trigger;
- Temporal-backed durable workflows;
- NATS JetStream event transport;
- agent-to-agent delegation;
- agent messages;
- task records;
- approval gates;
- run timeline;
- audit log;
- cost/token tracking;
- Docker Compose deployment;
- backup/restore documentation;
- public open-source repository quality.

## 3.2 Intentionally out of scope for initial release

- Kubernetes as a requirement;
- arbitrary untrusted third-party plugin code executed inside the control plane;
- a marketplace;
- billing/SaaS metering;
- enterprise SSO;
- multi-region active-active;
- unrestricted browser automation on the host;
- storing credentials in model context;
- agents receiving direct Docker socket access;
- agents receiving host root access;
- autonomous production destructive operations without configurable policy/approval controls.

These can be added later without changing the core domain model.

---

# 4. High-Level Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                            Browser / Web UI                             │
│ Next.js: Dashboard, Org Chart, Agents, Teams, Tasks, Runs, Approvals, │
│ Connectors, Triggers, Secrets, Models, Audit, Settings                 │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ HTTPS / WebSocket or SSE
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         FastAPI Control Plane                           │
│ Auth • RBAC • CRUD • Policy • Trigger config • Tool registry • API    │
└──────────┬─────────────────┬───────────────────┬────────────────────────┘
           │                 │                   │
           ▼                 ▼                   ▼
     PostgreSQL        NATS JetStream       Temporal Client
   system of record     event backbone           │
                                                 ▼
                                      ┌─────────────────────┐
                                      │ Temporal Server     │
                                      └──────────┬──────────┘
                                                 │
                      ┌──────────────────────────┴─────────────────────┐
                      ▼                                                ▼
             Agent Workflow Worker                           Connector/Task Worker
             Temporal + LangGraph                            deterministic activities
                      │                                                │
                      ▼                                                ▼
            Model Provider Gateway                              Tool Gateway
     OpenAI/Anthropic/OpenRouter/Ollama                   authz + policy + secrets
                      │                                                │
                      │                                   ┌────────────┼─────────────┐
                      │                                   ▼            ▼             ▼
                      │                                GitHub       Linear        Vercel
                      │                                   │            │             │
                      │                                   └────────────┼─────────────┘
                      │                                                ▼
                      │                                            Supabase
                      │
                      ▼
              Sandbox Runner Service
              isolated Docker jobs
              CLI / repo / tests
```

## Responsibility boundaries

### `web`
Presentation only. It does not execute tools, decrypt secrets, or directly manipulate Temporal/NATS.

### `api`
Owns product configuration, auth, permissions, validation, and user-facing API.

### `workflow-worker`
Owns Temporal workflow definitions and orchestration activities. It invokes agent runs but does not expose HTTP.

### `agent-worker`
Runs LangGraph-based reasoning for a single agent execution.

### `event-worker`
Consumes durable NATS events and routes validated events to trigger evaluation / Temporal starts or signals.

### `tool-worker`
Executes deterministic connector operations through the tool gateway.

### `sandbox-runner`
Runs untrusted or semi-trusted CLI/repository commands in constrained ephemeral containers.

### `postgres`
Permanent product state.

### `nats`
Event transport and replay.

### `temporal`
Durable workflow execution history.

---

# 5. Recommended Repository Layout

Use a monorepo.

```text
jhin/
├── README.md
├── LICENSE
├── SECURITY.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── CHANGELOG.md
├── .env.example
├── compose.yaml
├── compose.dev.yaml
├── Makefile
├── pyproject.toml
├── pnpm-workspace.yaml
├── package.json
├── docs/
│   ├── architecture/
│   │   ├── overview.md
│   │   ├── security.md
│   │   ├── events.md
│   │   ├── workflows.md
│   │   ├── connectors.md
│   │   └── sandboxing.md
│   ├── deployment/
│   │   ├── docker-compose.md
│   │   ├── reverse-proxy.md
│   │   ├── backups.md
│   │   └── upgrading.md
│   └── development/
├── apps/
│   ├── web/
│   │   ├── app/
│   │   ├── components/
│   │   ├── features/
│   │   ├── lib/
│   │   └── tests/
│   └── api/
│       └── src/jhin_api/
├── services/
│   ├── workflow_worker/
│   ├── agent_worker/
│   ├── event_worker/
│   ├── tool_worker/
│   └── sandbox_runner/
├── packages/
│   ├── domain/
│   ├── db/
│   ├── events/
│   ├── workflows/
│   ├── agents/
│   ├── models/
│   ├── policy/
│   ├── secrets/
│   ├── tools/
│   ├── connectors/
│   │   ├── github/
│   │   ├── linear/
│   │   ├── vercel/
│   │   ├── supabase/
│   │   ├── http/
│   │   └── cli/
│   ├── observability/
│   └── testing/
├── migrations/
├── scripts/
└── .github/
    ├── workflows/
    ├── ISSUE_TEMPLATE/
    └── dependabot.yml
```

### Rule

Do not create a “god package” containing all business logic. Each package must have one clear responsibility and stable public interfaces.

---

# 6. Domain Model

All primary keys should be UUIDv7 where practical. All records should include `created_at`; mutable records include `updated_at`.

## 6.1 Workspace

```text
workspace
- id
- name
- slug
- status
- default_model_profile_id
- default_timezone
- settings_json
```

The workspace is the security and ownership boundary.

## 6.2 User

```text
user
- id
- email
- display_name
- password_hash
- status
```

## 6.3 Workspace membership

```text
workspace_membership
- workspace_id
- user_id
- role: owner | admin | member | viewer
```

## 6.4 Team

```text
team
- id
- workspace_id
- name
- description
- parent_team_id nullable
- manager_agent_id nullable
- color_token
- icon
```

Examples: Engineering, Marketing, Platform, Content.

## 6.5 Agent

```text
agent
- id
- workspace_id
- team_id nullable
- manager_agent_id nullable
- name
- slug
- role_title
- description
- system_prompt
- status: active | paused | disabled
- autonomy_level
- model_profile_id nullable
- temperature nullable
- max_output_tokens nullable
- max_steps
- max_run_minutes
- monthly_budget_cents nullable
- metadata_json
```

`manager_agent_id` creates the reporting hierarchy.

Prevent cycles at write time.

## 6.6 Agent capability grant

```text
agent_capability_grant
- id
- agent_id
- capability
- scope_json
- effect: allow | deny
```

Examples:

```json
{
  "capability": "github.pull_request.create",
  "scope": {
    "connection_id": "...",
    "repositories": ["acme/api"],
    "branches": ["agent/*"]
  }
}
```

## 6.7 Model provider

```text
model_provider
- id
- workspace_id
- type
- display_name
- base_url nullable
- secret_id nullable
- enabled
```

Supported provider types initially:

- OpenAI;
- Anthropic;
- OpenRouter;
- Ollama;
- OpenAI-compatible custom endpoint.

## 6.8 Model profile

```text
model_profile
- id
- workspace_id
- provider_id
- model_name
- display_name
- context_window nullable
- input_cost_micros_per_million nullable
- output_cost_micros_per_million nullable
- supports_tools
- supports_reasoning
- config_json
```

Agent configuration may inherit workspace default or select a model explicitly.

## 6.9 Connection

Represents an authenticated integration instance.

```text
connection
- id
- workspace_id
- connector_type
- name
- auth_type
- status
- encrypted_secret_id nullable
- config_json
- created_by_user_id
- last_verified_at
- last_error
```

Examples:

- `Engineering GitHub`
- `Company Linear`
- `Production Vercel`
- `FanClan Supabase`

## 6.10 Secret

Never put secret plaintext in this table.

```text
secret
- id
- workspace_id
- name
- type
- ciphertext
- nonce
- wrapped_data_key
- key_version
- secret_fingerprint
- created_by_user_id
- last_used_at
- rotated_at nullable
```

See the security section for encryption.

## 6.11 Trigger

```text
trigger
- id
- workspace_id
- name
- enabled
- trigger_type
- connection_id nullable
- event_type nullable
- filter_json
- action_type
- target_agent_id nullable
- target_team_id nullable
- workflow_definition
- dedupe_window_seconds
- created_by_user_id
```

Trigger types:

- webhook connector event;
- schedule;
- manual;
- internal event;
- later: polling.

## 6.12 Task

A task is user-visible work.

```text
task
- id
- workspace_id
- external_source nullable
- external_id nullable
- title
- description
- state
- priority
- assigned_agent_id nullable
- assigned_team_id nullable
- parent_task_id nullable
- trigger_id nullable
- temporal_workflow_id nullable
- correlation_id
- metadata_json
```

## 6.13 Agent run

```text
agent_run
- id
- workspace_id
- agent_id
- task_id nullable
- parent_run_id nullable
- status
- reason
- model_profile_id
- started_at
- completed_at
- input_tokens
- output_tokens
- estimated_cost_micros
- temporal_workflow_id
- temporal_run_id
- langgraph_thread_id nullable
- error_code nullable
- error_message nullable
```

## 6.14 Message

```text
message
- id
- workspace_id
- task_id nullable
- run_id nullable
- sender_type: user | agent | system
- sender_id
- recipient_type: user | agent | team | task
- recipient_id nullable
- message_type
- content_json
- visibility
- created_at
```

## 6.15 Tool call

```text
tool_call
- id
- workspace_id
- run_id
- agent_id
- tool_name
- connection_id nullable
- sanitized_input_json
- sanitized_output_json
- status
- approval_id nullable
- started_at
- completed_at
- duration_ms
- error_code nullable
```

Never persist bearer tokens, Authorization headers, private keys, cookies, or raw connection strings in these JSON fields.

## 6.16 Approval

```text
approval
- id
- workspace_id
- task_id nullable
- run_id nullable
- requested_by_agent_id
- action_type
- action_payload_sanitized
- reason
- status
- requested_at
- decided_at nullable
- decided_by_user_id nullable
```

## 6.17 Audit event

Append-only.

```text
audit_event
- id
- workspace_id
- actor_type
- actor_id
- action
- target_type
- target_id
- metadata_json
- request_id
- ip_hash nullable
- created_at
```

---

# 7. Agent Runtime

## 7.1 Agent definition

An agent is configuration, not a permanently running container.

At runtime resolve an immutable `AgentExecutionSnapshot` containing:

```python
AgentExecutionSnapshot(
    agent_id,
    role_title,
    system_prompt,
    manager_agent_id,
    team_id,
    model_profile,
    allowed_tools,
    allowed_connections,
    policy_snapshot,
    budget_snapshot,
    run_limits,
)
```

Store a hash/version of this snapshot on every run so future audits know exactly what configuration was used.

## 7.2 Prompt composition

The platform layer ships as the versioned `PLATFORM_PREAMBLE` in
`packages/agents/src/jhin_agents/platform_prompt.py` — one default for every
workspace (not configurable per workspace yet); edit it there and bump
`PLATFORM_PREAMBLE_VERSION` to change what every agent is told about being
an AI teammate on Jhin.

Prompt layers, in order:

1. platform safety/execution policy;
2. agent role/system prompt;
3. organization context;
4. team context;
5. manager relationship;
6. task;
7. relevant memory;
8. tool schemas;
9. execution constraints.

Never concatenate secrets.

## 7.3 LangGraph responsibilities

LangGraph should own the within-run cognitive graph:

```text
START
  ↓
load_context
  ↓
reason
  ├── answer/complete ───────────► finalize
  ├── call_tool ─► policy_check ─► execute_tool ─► observe ─► reason
  ├── delegate ──► create_delegation ─────────────► wait/continue
  └── request_approval ──────────► suspend
```

Use a strict maximum step count.

The graph must emit structured events after every meaningful node transition.

## 7.4 Temporal responsibilities

Temporal owns processes around and across agent runs:

- triggered task workflow;
- engineering ticket workflow;
- delegated subtask workflow;
- approval wait;
- scheduled work;
- retrying connector actions;
- waiting for external events;
- workflow cancellation;
- workflow timeout;
- compensation where required.

Do not put nondeterministic model calls directly in Temporal workflow code. Perform model calls in Activities.

## 7.5 Delegation

Agents may call:

```text
organization.delegate_task
```

Input:

```json
{
  "target_agent_id": "...",
  "title": "...",
  "instructions": "...",
  "expected_output": "...",
  "blocking": true
}
```

The policy engine validates:

- agent exists;
- relationship permits delegation;
- target is active;
- budget permits work;
- no cycle/deadlock;
- task depth below limit.

The delegation starts a child Temporal workflow.

## 7.6 Manager behavior

Managers should receive standardized completion summaries from subordinates rather than full context by default.

Example:

```json
{
  "task_id": "...",
  "status": "completed",
  "summary": "Implemented token rotation and opened PR #381.",
  "artifacts": [
    {"type": "github_pull_request", "id": "381"}
  ],
  "risks": [],
  "recommended_next_action": "delegate_to_qa"
}
```

Managers may explicitly request detailed run history if necessary.

---

# 8. Temporal Workflow Design

Create typed workflows under `packages/workflows`.

## 8.1 `TriggeredTaskWorkflow`

Purpose: durable entry point for an external trigger.

Pseudo-flow:

```text
receive TriggerInvocation
↓
create/load task
↓
resolve assigned agent
↓
run agent
↓
process outcome
├── completed
├── delegated -> wait for child workflows
├── approval_required -> wait for signal
├── retryable_failure -> retry activity
└── terminal_failure
↓
sync external source when configured
↓
complete
```

## 8.2 `AgentTaskWorkflow`

One durable workflow for a unit of agent-owned work.

Signals:

- `pause`
- `resume`
- `cancel`
- `user_instruction`
- `approval_decision`
- `external_event`

Queries:

- current status;
- active agent;
- waiting reason;
- child tasks.

## 8.3 `DelegatedTaskWorkflow`

Child workflow started when one agent delegates to another.

The parent gets a typed result.

## 8.4 `EngineeringTicketWorkflow`

Template provided with the project:

```text
issue enters Todo
↓
SWE receives task
↓
implementation
↓
PR opened
↓
optional manager review
↓
QA task
├── fail -> SWE fix loop
└── pass
↓
approval policy
↓
merge/transition issue
```

Do not force users to use this exact workflow. It is a built-in template.

## 8.5 `ScheduledAgentWorkflow`

Started from Temporal schedules or a scheduler service, depending on final implementation.

Examples:

- daily SEO report;
- weekly dependency audit;
- nightly QA checks.

## 8.6 Retry policy

Classify failures:

### Retry automatically

- HTTP 408/429;
- temporary 5xx;
- network timeouts;
- NATS availability errors;
- model-provider transient failures.

### Do not blindly retry

- permission denied;
- invalid credentials;
- failed policy;
- invalid tool input;
- destructive-action approval rejected;
- source branch conflict;
- exhausted budget.

Use bounded exponential backoff with jitter for Activities.

---

# 9. NATS + JetStream Design

## 9.1 Why NATS exists in this architecture

NATS decouples event producers from event consumers and gives the platform:

- durable connector event ingestion;
- fan-out to realtime UI, audit, analytics, and trigger evaluation;
- event replay;
- independent worker scaling;
- dead-letter handling.

Temporal remains the workflow authority.

## 9.2 Subject convention

Use versioned subjects:

```text
jhin.v1.<workspace_id>.<domain>.<entity>.<event>
```

Examples:

```text
jhin.v1.ws_123.connector.linear.issue.updated
jhin.v1.ws_123.agent.run.started
jhin.v1.ws_123.agent.run.completed
jhin.v1.ws_123.task.created
jhin.v1.ws_123.tool.call.failed
jhin.v1.ws_123.approval.requested
```

External raw events should first land under:

```text
jhin.v1.<workspace_id>.ingress.<connector>.<event>
```

Normalized events then use the canonical domain subjects.

## 9.3 Event envelope

All events use:

```json
{
  "event_id": "UUIDv7",
  "event_type": "connector.linear.issue.updated",
  "event_version": 1,
  "workspace_id": "UUID",
  "occurred_at": "RFC3339",
  "received_at": "RFC3339",
  "correlation_id": "UUID",
  "causation_id": "UUID|null",
  "source": {
    "type": "linear",
    "connection_id": "UUID"
  },
  "data": {}
}
```

## 9.4 Idempotency

- `event_id` is globally unique.
- Webhook deliveries get a source delivery ID when one exists.
- Store processed source delivery IDs.
- NATS publishers attach a deduplication identifier.
- Trigger execution has a deterministic idempotency key.
- Temporal workflow IDs should be deterministic for externally triggered work when possible.

## 9.5 Streams

Create:

```text
INGRESS
EVENTS
AUDIT
DLQ
```

Use durable pull consumers for workers.

Configure retention deliberately. Do not rely on defaults.

## 9.6 Dead-letter behavior

After configured delivery attempts:

- publish sanitized failure metadata to DLQ;
- mark event processing failure in Postgres;
- surface it in the UI;
- allow replay after remediation.

---

# 10. Trigger Engine

Triggers are a central feature, not connector-specific hard-coded callbacks.

## 10.1 Trigger flow

```text
External service
    ↓
Webhook endpoint
    ↓
signature/auth verification
    ↓
raw event normalization
    ↓
NATS INGRESS
    ↓
event-worker
    ↓
canonical event
    ↓
TriggerMatcher
    ↓
matching enabled triggers
    ↓
Temporal workflow start/signal
```

## 10.2 Filter DSL

Start with safe JSON conditions, not arbitrary Python/JavaScript.

Example:

```json
{
  "all": [
    {"path": "data.team.key", "op": "eq", "value": "ENG"},
    {"path": "data.state.type", "op": "eq", "value": "unstarted"},
    {"path": "data.state.name", "op": "eq", "value": "Todo"}
  ]
}
```

Supported operators initially:

- `eq`
- `neq`
- `in`
- `not_in`
- `contains`
- `exists`
- `gt`
- `gte`
- `lt`
- `lte`

## 10.3 Trigger action

Initial actions:

- start task for agent;
- start workflow template;
- signal existing task/workflow;
- send internal notification.

## 10.4 Linear Todo example

User configures:

```text
Name: Pick up new engineering tickets
Source: Linear
Event: Issue updated/created
Conditions:
  Team = Engineering
  State type = unstarted
  State name = Todo
Action:
  Create/assign task
Agent:
  Senior Software Engineer
```

Runtime:

```text
Linear webhook
  ↓
POST /api/v1/webhooks/linear/{connection_public_id}
  ↓
verify signature
  ↓
normalize issue
  ↓
publish ingress event
  ↓
TriggerMatcher
  ↓
dedupe using connection + issue ID + relevant state transition
  ↓
start TriggeredTaskWorkflow
  ↓
SWE agent begins
```

The matcher must recognize a **transition into** Todo, not repeatedly launch work for every update while the issue remains Todo.

Persist last observed relevant external state or use webhook change metadata to distinguish transition from current-state equality.

## 10.5 Schedules

Allow:

- cron;
- timezone;
- interval;
- enabled/disabled;
- optional jitter.

---

# 11. Connector Framework

Every connector implements a common interface.

```python
class Connector:
    type: str
    auth_schemes: list[AuthScheme]

    async def verify_connection(self, ctx) -> ConnectionHealth: ...
    def tools(self) -> list[ToolDefinition]: ...
    def webhook_handlers(self) -> list[WebhookDefinition]: ...
    def normalize_event(self, raw_event) -> list[DomainEvent]: ...
```

## 11.1 Connector manifest

Each connector declares:

- display name;
- icon identifier;
- auth types;
- required secret fields;
- public config fields;
- tool schemas;
- webhook capabilities;
- permission scopes;
- health check.

## 11.2 GitHub

Prefer **GitHub App authentication** for production installations because it is scopeable and revocable. Support PAT for simple self-hosted setups.

Initial tools:

- repository read;
- branch create;
- file/content read;
- issue read/comment;
- PR create/read/comment;
- check/status read;
- merge behind approval policy;
- workflow dispatch;
- Actions run status.

Webhook events:

- issue;
- pull request;
- check suite;
- workflow run;
- push.

## 11.3 Linear

Prefer OAuth where appropriate; support API key for personal/self-hosted setup.

Initial tools:

- issue read;
- issue search;
- create issue;
- update issue;
- comment;
- project/team/workflow-state discovery.

Webhook events:

- issue created;
- issue updated;
- comment created;
- relevant project events.

## 11.4 Vercel

Tools:

- project list/read;
- deployment list/read;
- create deployment if supported by configured auth/workflow;
- deployment logs;
- environment metadata read;
- redeploy;
- promote/alias only behind elevated permission.

Do not expose all environment-variable values to agents by default.

## 11.5 Supabase

Separate **management-plane** operations from **database-plane** operations.

Management examples:

- project metadata;
- logs if available through configured API;
- function/deployment operations.

Database-plane access should use an explicit connection with its own scope.

For SQL:

- read-only connection option;
- allowlisted schemas;
- statement timeout;
- maximum rows;
- block multi-statement SQL by default;
- destructive DDL/DML requires explicit elevated capability and optional approval.

## 11.6 CLI connector

CLI is not “run anything on host.”

It routes to Sandbox Runner.

Capabilities:

```text
cli.command.execute
cli.repository.checkout
cli.test.run
cli.file.read
cli.file.write
```

Policy can constrain:

- image;
- repository;
- network access;
- command patterns;
- CPU;
- memory;
- PIDs;
- wall-clock time;
- filesystem mounts;
- environment variables.

## 11.7 Generic HTTP

Support defined endpoints through reusable tool definitions.

Do not provide arbitrary unrestricted internal-network HTTP access by default.

Protect against SSRF:

- block loopback;
- block link-local;
- block private ranges unless explicitly allowlisted;
- resolve DNS safely;
- enforce redirect policy;
- limit response size;
- enforce timeout.

---

# 12. Tool Gateway and Authorization

Every tool call goes through one central authorization path.

```text
Agent
 ↓
ToolCallRequest
 ↓
schema validation
 ↓
Agent capability lookup
 ↓
Connection scope validation
 ↓
Policy engine
 ↓
approval check
 ↓
budget/rate limit
 ↓
secret lease
 ↓
connector executor
 ↓
sanitizer
 ↓
result
```

## 12.1 Tool definition

```python
ToolDefinition(
    name="github.pull_request.create",
    risk="write",
    input_schema=...,
    output_schema=...,
    required_capability="github.pull_request.create",
    supports_approval=True,
)
```

## 12.2 Risk levels

```text
read
write
elevated
destructive
```

Default policies:

- read: may run automatically if granted;
- write: may run automatically if explicitly granted;
- elevated: configurable approval;
- destructive: human approval by default.

## 12.3 Capability hierarchy

Examples:

```text
github.repository.read
github.branch.create
github.pull_request.create
github.pull_request.comment
github.pull_request.merge

linear.issue.read
linear.issue.create
linear.issue.update
linear.comment.create

vercel.deployment.read
vercel.deployment.create
vercel.deployment.promote

supabase.database.read
supabase.database.write
supabase.database.ddl

cli.execute
```

Do not use a single boolean like `github_access=true`.

---

# 13. Secrets and Credential Security

This section is mandatory for production readiness.

## 13.1 Threat model

Protect against:

- database dump exposure;
- logs exposing tokens;
- frontend exposure;
- one agent reading another agent's credentials;
- prompt injection requesting secrets;
- connector error bodies containing secrets;
- compromised low-privilege user;
- accidental export/backup leakage.

## 13.2 Envelope encryption

Use envelope encryption.

At minimum:

- generate a random data-encryption key (DEK) per secret;
- encrypt secret plaintext using AES-256-GCM or XChaCha20-Poly1305;
- wrap the DEK using a master key;
- store ciphertext, nonce, wrapped DEK, and key version;
- never store the master key in Postgres.

For self-hosted Docker Compose:

```text
/run/secrets/jhin_master_key
```

is mounted only into services that require decryption.

The web container never receives it.

## 13.3 Key rotation

Support key versions.

Rotation process:

1. install new master key version;
2. new secrets use newest key;
3. background administrative job re-wraps DEKs;
4. old key remains until migration completes;
5. retire old key.

## 13.4 Secret APIs

Create:

```text
POST   /api/v1/secrets
GET    /api/v1/secrets
PATCH  /api/v1/secrets/{id}
POST   /api/v1/secrets/{id}/rotate
DELETE /api/v1/secrets/{id}
```

`GET` never returns plaintext after creation.

UI displays:

```text
OpenAI API Key
sk-••••••••••7A2F
Last used: 4 minutes ago
```

## 13.5 Secret access

Agents reference a `connection_id`.

The tool worker:

1. validates authorization;
2. retrieves/decrypts required credential;
3. uses it in process memory;
4. executes request;
5. redacts it from logs/errors;
6. discards plaintext.

Do not inject long-lived credentials into LLM prompts.

## 13.6 Sandbox credentials

Prefer short-lived credentials.

When unavoidable:

- inject only credentials necessary for that single job;
- inject as temporary files or environment variables;
- ensure they are absent from image layers;
- destroy sandbox after job;
- redact stdout/stderr.

For GitHub, prefer short-lived GitHub App installation tokens for repository jobs.

---

# 14. Sandbox Runner

The CLI/repository executor is one of the highest-risk components.

## 14.1 Isolation

Each job runs in a fresh ephemeral container.

Never mount:

```text
/var/run/docker.sock
/
~/.ssh
host Docker config
control-plane secrets
```

into agent jobs.

## 14.2 Runner API

Internal-only API/message interface:

```python
SandboxJobRequest(
    job_id,
    image,
    command,
    workspace_archive_or_repo,
    env_secret_refs,
    network_policy,
    cpu_limit,
    memory_limit,
    pids_limit,
    timeout_seconds,
)
```

## 14.3 Recommended constraints

Default:

```text
CPU: 2
Memory: 4 GiB
PIDs: 256
Timeout: 30 min
Root filesystem: read-only where possible
No privileged mode
No host network
Drop Linux capabilities
no-new-privileges
Non-root user
Ephemeral writable workspace
```

The user's Docker LXC environment must support nested Docker securely. Document required LXC configuration separately and clearly warn about its security implications.

## 14.4 Network policy

Start with two modes:

- `none`
- `internet`

Then add allowlisted egress.

Do not permit access to control-plane internal service DNS names from arbitrary sandbox jobs unless explicitly needed.

## 14.5 Repository workflow

For software agents:

```text
create sandbox
↓
obtain short-lived repository credential
↓
clone repository
↓
create branch agent/<task-id>-slug
↓
perform changes
↓
run tests
↓
commit
↓
push branch
↓
open PR through GitHub connector
↓
destroy sandbox
```

Prefer PRs over direct default-branch writes.

---

# 15. Model Provider Layer

Create provider-independent interfaces.

```python
class ModelClient:
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def stream(self, request: ModelRequest): ...
```

## 15.1 Provider adapters

Initial:

- OpenAI;
- Anthropic;
- OpenRouter;
- Ollama;
- generic OpenAI-compatible endpoint.

## 15.2 Per-agent model assignment

Agent editor provides:

```text
Model
○ Workspace default
● Custom
  Provider: OpenAI
  Model: ...
```

Also allow task/run override by authorized users.

## 15.3 Fallbacks

Model profile may optionally contain ordered fallbacks.

Example:

```text
Primary: provider-A/model-X
Fallback: provider-B/model-Y
```

Do not silently switch to a semantically weaker model if the user disables fallback.

## 15.4 Usage accounting

Persist:

- provider;
- model;
- input tokens;
- output tokens;
- cached tokens where reported;
- provider request ID when safe;
- latency;
- estimated cost.

Allow agent/team/workspace monthly budgets.

## 15.5 Budget enforcement

Before a run or expensive model call:

- estimate remaining budget;
- fail or require approval if exhausted;
- allow owner-configured soft/hard limits.

---

# 16. Memory and Context

Do not confuse durable product state with LLM memory.

## 16.1 Types

### Run memory
Current LangGraph state.

### Agent memory
Long-lived agent-specific learned information.

### Team memory
Shared knowledge available to a team.

### Workspace knowledge
Documents/instructions available across agents.

## 16.2 V1 storage

Start with Postgres.

Use:

- structured memory records;
- optional `pgvector` for semantic retrieval;
- source attribution;
- timestamps;
- visibility.

Do not introduce a second vector database until needed.

## 16.3 Memory writes

Model-proposed memory must pass a deterministic write policy.

Never allow the model to persist:

- credentials;
- access tokens;
- private keys;
- raw Authorization headers.

---

# 17. Frontend Product Design

The UI should feel like a polished operations product, not an admin template.

## 17.1 Design direction

Visual characteristics:

- high-information-density but calm;
- generous spacing;
- strong typography;
- subtle borders/surfaces;
- minimal gratuitous gradients;
- dark and light themes;
- fast keyboard navigation;
- responsive desktop-first layout;
- realtime status indicators;
- excellent empty states;
- consistent iconography.

Use shadcn/ui primitives but create a distinct visual system rather than shipping default component-library styling.

## 17.2 Main navigation

```text
Overview
Organization
Tasks
Runs
Approvals
Connectors
Triggers
Models
Audit
Settings
```

## 17.3 Overview

Cards:

- active agents;
- tasks running;
- tasks waiting;
- approvals;
- errors;
- spend today/month;
- connector health.

Visualizations:

- activity timeline;
- task status distribution;
- cost by team/agent;
- recent trigger invocations.

## 17.4 Organization view

Interactive org chart.

Example:

```text
                         CTO
                  ┌───────┼───────┐
                Senior    QA     DevOps
                 SWE
```

Capabilities:

- zoom/pan;
- drag agents between teams;
- set manager;
- add subordinate;
- open agent drawer;
- status indicator;
- active-task badge.

Cycle validation is server-side even if frontend prevents obvious invalid drops.

## 17.5 Agent profile

Tabs:

```text
Overview
Instructions
Tools & Access
Model
Tasks
Runs
Memory
Activity
Settings
```

Header:

```text
Senior Software Engineer
Engineering · Reports to CTO
Active

[Message] [Assign Task] [Pause]
```

## 17.6 Agent creation wizard

Steps:

1. Identity
2. Role & instructions
3. Team/manager
4. Model
5. Tools & connections
6. Autonomy/approvals
7. Limits/budget
8. Review

Include templates:

- CTO;
- Software Engineer;
- QA Engineer;
- DevOps;
- Marketing Director;
- Blogger;
- SEO Specialist;
- Generic Assistant.

## 17.7 Task detail

The most important operational screen.

Layout:

```text
Title / status / owner / source
────────────────────────────────────
Timeline
  Linear trigger received
  SWE started
  GitHub repository read
  CLI sandbox created
  Commit produced
  PR #381 opened
  QA delegated
  QA failed test
  SWE resumed
  ...
────────────────────────────────────
Conversation / Events / Artifacts / Tool Calls / Cost
```

Actions:

- pause;
- cancel;
- reassign;
- send instruction;
- approve;
- retry failed action.

## 17.8 Live run view

Show:

- current graph node;
- current status;
- model;
- elapsed duration;
- token/cost totals;
- sanitized tool calls;
- child/delegated runs;
- workflow waiting reason.

Never display hidden chain-of-thought. Display concise model-provided action summaries and structured execution events.

## 17.9 Connectors

Connector gallery:

```text
GitHub
Linear
Vercel
Supabase
CLI
HTTP
```

Connection detail:

- health;
- authentication method;
- scopes;
- agents allowed;
- recent usage;
- errors;
- rotate/reconnect;
- webhooks.

## 17.10 Trigger builder

Human-readable builder:

```text
WHEN
  Linear → Issue changes

IF
  Team is Engineering
  AND State changes to Todo

THEN
  Assign task to Senior Software Engineer

[Save Trigger]
```

Include a “Test trigger” function against a sample event.

## 17.11 Approvals

Inbox experience:

```text
QA Agent wants to merge PR #381
Risk: Elevated
Repository: acme/api
Reason: All tests passed

[Reject] [Approve once]
```

Later: “approve similar actions” policy authoring, but not required for first release.

## 17.12 Audit

Filter by:

- actor;
- agent;
- user;
- connector;
- action;
- task;
- time;
- result.

Audit records are append-only.

---

# 18. Realtime UI

Use SSE initially unless bidirectional realtime semantics require WebSockets.

Flow:

```text
workers → NATS event
            ↓
      realtime gateway
            ↓
           SSE
            ↓
          browser
```

The browser must always be able to refresh from Postgres-backed APIs; realtime events are an enhancement, not the source of truth.

---

# 19. API Design

Prefix:

```text
/api/v1
```

## Core resources

```text
/auth
/workspaces
/users
/teams
/agents
/model-providers
/model-profiles
/connections
/secrets
/triggers
/tasks
/runs
/messages
/approvals
/audit-events
```

## Action endpoints

Prefer explicit actions:

```text
POST /agents/{id}/message
POST /agents/{id}/assign-task
POST /agents/{id}/pause
POST /agents/{id}/resume

POST /tasks/{id}/pause
POST /tasks/{id}/resume
POST /tasks/{id}/cancel
POST /tasks/{id}/instruction

POST /approvals/{id}/approve
POST /approvals/{id}/reject

POST /connections/{id}/verify
POST /triggers/{id}/test
```

## Webhooks

```text
POST /api/v1/webhooks/linear/{public_connection_id}
POST /api/v1/webhooks/github/{public_connection_id}
POST /api/v1/webhooks/vercel/{public_connection_id}
```

Webhook endpoints must not use normal session authentication. They use provider-specific validation.

---

# 20. Authentication and Authorization

## 20.1 Initial auth

Support self-hosted email/password authentication.

Requirements:

- Argon2id password hashing;
- secure HttpOnly session cookie;
- SameSite policy;
- CSRF protection where applicable;
- session revocation;
- rate-limited login;
- secure password reset flow when email is configured.

Optional OIDC can follow.

## 20.2 RBAC

Workspace roles:

### Owner
Everything including deleting workspace and managing master settings.

### Admin
Manage agents, teams, connectors, triggers, models, users except ownership-sensitive actions.

### Member
Operate agents/tasks based on grants.

### Viewer
Read-only operational access; secret values never visible.

## 20.3 Agent authorization

User RBAC and agent capabilities are separate.

A workspace admin may grant a QA agent GitHub read access without granting that QA agent merge access.

---

# 21. Prompt-Injection and Agent Security

Treat all external content as untrusted.

Examples:

- GitHub issue descriptions;
- PR comments;
- Linear tickets;
- webpages;
- command output;
- documentation;
- repository files.

## Controls

1. External content is labeled as untrusted data in context.
2. Tool authorization is never delegated to model text.
3. Credentials are inaccessible to the model.
4. Tool calls use strict structured schemas.
5. High-risk calls require policy/approval.
6. Sandboxes isolate code execution.
7. Outbound HTTP is constrained.
8. Tool outputs are size-limited.
9. Secret-redaction middleware runs on logs/results.
10. Agent cannot modify its own capability grants.
11. Agent cannot modify its own system prompt unless explicitly allowed through a special administrative workflow.
12. Agent cannot grant permissions to another agent.

---

# 22. Observability

## 22.1 OpenTelemetry

Instrument:

- API requests;
- Temporal activities;
- model calls;
- connector HTTP calls;
- NATS publish/consume;
- sandbox jobs;
- DB operations where useful.

Propagate:

```text
trace_id
request_id
correlation_id
task_id
run_id
```

## 22.2 Metrics

At minimum:

```text
agent_runs_total
agent_run_duration_seconds
agent_run_failures_total
model_requests_total
model_tokens_total
model_cost_estimate
tool_calls_total
tool_call_failures_total
trigger_invocations_total
trigger_failures_total
sandbox_jobs_total
sandbox_job_duration_seconds
nats_consumer_lag
temporal_activity_failures
connector_health
```

## 22.3 Logs

Structured JSON.

Never log:

- secret plaintext;
- complete Authorization headers;
- cookies;
- private keys;
- model provider API keys;
- database passwords.

## 22.4 Product-visible traces

The UI should expose a sanitized execution timeline built from application events, not raw infrastructure logs.

---

# 23. Auditability

Every sensitive operation creates an audit event.

Examples:

```text
agent.created
agent.updated
agent.permission.granted
agent.permission.revoked
secret.created
secret.rotated
connection.created
connection.verified
trigger.created
trigger.enabled
task.started
tool.call.requested
tool.call.approved
tool.call.executed
approval.approved
workflow.cancelled
```

Audit records should be append-only at the application layer.

---

# 24. Docker Compose Deployment

The production Compose deployment should include:

```text
web
api
workflow-worker
agent-worker
event-worker
tool-worker
sandbox-runner
postgres
nats
temporal
temporal-ui
```

Optional profile:

```text
otel-collector
prometheus
grafana
minio
```

## 24.1 Networks

Separate networks:

```text
edge
control
data
runner
```

Principle:

- web talks to API;
- API talks to Postgres, NATS, Temporal;
- workers talk to necessary data/control services;
- sandbox runner is isolated;
- databases are not published publicly.

## 24.2 Ports

Only publish:

- web/API entrypoint through reverse proxy;
- optionally Temporal UI on an admin-only binding.

Never publish Postgres/NATS/Temporal database/service ports to the public internet by default.

## 24.3 Reverse proxy

Document both:

- Caddy;
- Traefik/Nginx-compatible configuration guidance.

TLS is required for any non-local deployment.

## 24.4 Healthchecks

Every service must have a meaningful healthcheck.

`depends_on` is not a substitute for retry logic.

## 24.5 Persistent volumes

Persist:

```text
postgres_data
nats_data
temporal_data if using bundled persistence topology
object_data if MinIO enabled
```

Sandbox workspaces are ephemeral.

## 24.6 Docker images

Publish multi-architecture images where practical:

```text
linux/amd64
linux/arm64
```

Use non-root users.

Use multi-stage builds.

Pin base image digests in release builds if maintainable.

---

# 25. Temporal Self-Hosting

For easy Compose installation, provide a supported self-hosted Temporal setup.

The application must treat Temporal as an external service through configuration so users can later point it at:

- bundled Temporal;
- another self-hosted cluster;
- Temporal Cloud.

Configuration:

```text
TEMPORAL_ADDRESS
TEMPORAL_NAMESPACE
TEMPORAL_TLS_*
```

Do not couple application code to bundled deployment assumptions.

---

# 26. Linear Ticket Trigger — Detailed Acceptance Flow

This is the first showcase workflow and must be implemented early.

## Setup

User:

1. creates Linear connection;
2. verifies connection;
3. installs/configures webhook;
4. creates Senior Software Engineer agent;
5. grants:
   - Linear issue read/comment/update;
   - GitHub repository read/branch/PR;
   - CLI sandbox;
6. creates trigger:
   - event = issue state transition;
   - destination state = Todo;
   - team = selected engineering team;
   - assign to SWE agent.

## Runtime

Given Linear issue `ENG-142`:

```text
Backlog → Todo
```

Expected:

1. webhook is authenticated;
2. event is accepted quickly;
3. raw event persisted/published;
4. normalized canonical event created;
5. trigger matcher detects transition;
6. idempotency key generated;
7. one Temporal workflow starts;
8. Task record links to Linear issue;
9. SWE run starts;
10. agent reads ticket;
11. agent may inspect repository;
12. sandbox performs implementation;
13. branch/PR produced;
14. agent comments/updates Linear if configured;
15. downstream QA delegation can occur;
16. all steps appear in Task timeline.

Duplicate Linear webhook delivery must **not** create a second task/workflow.

---

# 27. Engineering Team Template

Ship a built-in template users can instantiate.

## CTO

Default capabilities:

- read Linear/GitHub;
- create/delegate internal tasks;
- review artifacts;
- comment;
- request approvals.

No CLI by default.

## Senior Software Engineer

Default capabilities:

- read assigned Linear tickets;
- GitHub repo read;
- create branches;
- sandbox CLI;
- push agent branch;
- create PR;
- comment on Linear.

No merge to protected branch by default.

## QA Engineer

Default capabilities:

- GitHub read;
- checkout PR in sandbox;
- run tests;
- read preview deployment;
- comment;
- report pass/fail.

No source modifications by default.

## Example lifecycle

```text
CTO receives project-level objective
↓
CTO delegates implementation
↓
SWE opens PR
↓
QA validates
├── failure → SWE child task
│             ↓
│           updated PR
│             ↓
│           QA retest
└── pass → CTO/approval policy
              ↓
            merge
```

---

# 28. Marketing Team Template

## Marketing Director

- creates campaigns/tasks;
- delegates;
- reviews;
- sees analytics integrations;
- approves publishing depending on policy.

## Blogger

- research tools;
- CMS connector later;
- writing;
- no deployment/engineering access by default.

## SEO Agent

- analytics/search connectors later;
- keyword/task recommendations;
- may delegate content requests to manager depending on policy.

The hierarchy implementation must be generic. No engineering-specific assumptions in the core.

---

# 29. Agent-to-Agent Communication

Treat messages as structured records.

Types:

```text
instruction
question
status
result
delegation
review_request
review_result
escalation
```

Example:

```json
{
  "type": "review_request",
  "from_agent_id": "swe",
  "to_agent_id": "qa",
  "task_id": "...",
  "summary": "PR ready for QA",
  "artifacts": [
    {"type": "github_pr", "url_ref": "..."}
  ]
}
```

The UI renders these conversationally, but the backend retains structure.

---

# 30. Workflow/Agent Concurrency

Configure:

- workspace maximum concurrent runs;
- agent maximum concurrent runs;
- connector rate limits;
- model-provider concurrency;
- sandbox concurrency.

An agent with `max_concurrency=1` should not silently work two incompatible coding tickets simultaneously.

Queue additional work visibly.

---

# 31. Error Handling UX

Every failure shown to users should include:

- what failed;
- which step;
- whether work will retry;
- when/why it stopped;
- safe error details;
- suggested operator action;
- retry control if valid.

Example:

```text
GitHub authentication failed
Connection: Engineering GitHub
Action: Create branch
Automatic retry: No

[Reconnect GitHub] [Retry Task]
```

Never dump a Python traceback as the primary user-facing error.

---

# 32. Testing Strategy

## 32.1 Unit tests

Test:

- trigger matcher;
- filter DSL;
- capability evaluation;
- policy decisions;
- secret encryption/decryption;
- redaction;
- event serialization;
- idempotency keys;
- provider adapters;
- workflow pure logic;
- connector normalization.

## 32.2 Integration tests

Use real local:

- Postgres;
- NATS;
- Temporal.

Use fake external provider servers for:

- GitHub;
- Linear;
- Vercel;
- Supabase;
- model APIs.

Do not make normal CI depend on third-party APIs.

## 32.3 Temporal tests

Use Temporal testing environment/time skipping where supported.

Test:

- crash/retry;
- approval waits;
- cancellation;
- activity timeout;
- child workflow results;
- duplicate workflow-start behavior.

## 32.4 NATS tests

Verify:

- durable consumer recovery;
- redelivery;
- dedupe;
- DLQ;
- malformed event rejection.

## 32.5 Security tests

Test:

- agent denied unauthorized tool;
- agent cannot access another connection;
- secret never returned by GET;
- redactor strips known secret;
- webhook bad signature rejected;
- SSRF private target blocked;
- sandbox lacks Docker socket;
- sandbox has no host root;
- destructive action approval enforced;
- workspace isolation.

## 32.6 E2E tests

Playwright scenarios:

1. first-run setup;
2. create provider;
3. add agent;
4. create team/manager hierarchy;
5. add mock Linear connection;
6. create Todo trigger;
7. deliver webhook;
8. observe task begin;
9. inspect timeline;
10. approve protected action;
11. task completes.

## 32.7 Chaos/recovery test

Automated CI/nightly scenario:

1. start task;
2. kill agent worker;
3. restart;
4. verify Temporal resumes;
5. kill event worker during NATS delivery;
6. verify event redelivers/idempotency holds.

---

# 33. API and Schema Contracts

Generate OpenAPI from FastAPI.

Generate frontend TypeScript client/types from OpenAPI or maintain a strict generated contract.

No handwritten duplicate API types.

Use Pydantic schemas for:

- domain commands;
- event envelopes;
- tool calls;
- workflow inputs/results.

Version NATS event schemas.

---

# 34. Database Migration Discipline

Use Alembic.

Rules:

- every schema change is a migration;
- migrations are forward-tested in CI;
- destructive migrations require staged release strategy;
- no application startup auto-creating tables in production;
- backups documented before major upgrade.

---

# 35. Backups and Restore

Document backups for:

- Postgres;
- NATS JetStream data where operationally necessary;
- Temporal persistence;
- object storage;
- master encryption key.

The **master key backup is critical**. A database backup without the encryption key cannot recover credentials.

Provide a restore drill.

Do not store the master key inside the same backup archive by default.

---

# 36. Open-Source Repository Requirements

## 36.1 License

Choose a permissive license unless product strategy dictates otherwise.

Recommended default: **Apache-2.0**.

Reasons:

- clear patent grant;
- contributor-friendly;
- common for infrastructure projects.

## 36.2 Required files

- `README.md`
- `LICENSE`
- `SECURITY.md`
- `CONTRIBUTING.md`
- `CODE_OF_CONDUCT.md`
- `CHANGELOG.md`
- `.env.example`

## 36.3 README quick start

Target:

```bash
git clone ...
cd jhin
cp .env.example .env
docker compose up -d
```

Then browser onboarding handles remaining configuration.

Avoid a 30-step installation.

## 36.4 Security reporting

`SECURITY.md` must clearly state that security vulnerabilities involving secret leakage, sandbox escapes, auth bypass, or workspace isolation should not be posted publicly before coordinated disclosure.

## 36.5 Contribution architecture

Connector packages should make contribution straightforward.

A contributor should be able to add:

```text
packages/connectors/example/
  manifest.py
  connector.py
  tools.py
  webhook.py
  schemas.py
  tests/
```

without modifying ten unrelated services.

---

# 37. CI/CD

GitHub Actions pipelines:

## `ci.yml`

- Python lint;
- Python type check;
- Python unit tests;
- frontend lint;
- frontend type check;
- Vitest;
- build;
- Compose integration tests.

## `security.yml`

- dependency audit;
- secret scanning;
- container scan;
- static analysis;
- SBOM generation for release.

## `e2e.yml`

- boot full stack;
- run Playwright.

## `release.yml`

- tag validation;
- build multi-arch images;
- generate SBOM;
- sign images/artifacts if practical;
- publish GitHub release;
- publish container images.

Use Renovate or Dependabot.

---

# 38. Development Experience

Provide:

```bash
make dev
make test
make test-unit
make test-integration
make test-e2e
make lint
make typecheck
make migrate
make seed
make compose-up
make compose-down
```

Seed development data with:

```text
Engineering
  CTO
    Senior SWE
    QA

Marketing
  Marketing Director
    Blogger
```

Include a fake connector mode so contributors can see workflows without external accounts.

---

# 39. Production Configuration

Environment variables should reference infrastructure configuration, not store every user credential.

Examples:

```text
APP_ENV
APP_URL
API_URL

DATABASE_URL
NATS_URL
TEMPORAL_ADDRESS
TEMPORAL_NAMESPACE

MASTER_KEY_FILE
SESSION_SECRET_FILE

OTEL_EXPORTER_OTLP_ENDPOINT

SANDBOX_RUNNER_URL
SANDBOX_DEFAULT_IMAGE
```

User-added GitHub/Linear/etc. credentials live in encrypted application storage, not `.env`.

---

# 40. Security Headers and Browser Security

Production API/web:

- HSTS when TLS enabled;
- CSP;
- frame-ancestors;
- `X-Content-Type-Options`;
- secure cookies;
- explicit CORS;
- no wildcard authenticated CORS;
- CSRF protections appropriate to auth scheme.

---

# 41. Rate Limits and Abuse Controls

Rate limit:

- login;
- webhook endpoints per connection;
- manual agent starts;
- model calls per workspace;
- tool calls;
- sandbox starts.

A trigger storm from an external service must not produce unlimited model spend.

Use workspace concurrency and trigger dedupe/cooldown.

---

# 42. Approval Policies

V1 can provide policy presets:

```text
Autonomous
Balanced
Restricted
```

But persist explicit underlying rules.

Example Balanced:

```text
Read tools: automatic
Write to feature branch: automatic
Create PR: automatic
Linear comment/update: automatic
Merge PR: approval
Vercel production promotion: approval
Supabase write: approval
Destructive SQL: approval/disabled
```

Users can customize per agent.

---

# 43. Product Onboarding

First-run wizard:

1. Create owner account.
2. Name workspace.
3. Configure model provider.
4. Test model.
5. Create first team or use template.
6. Create agents.
7. Connect Linear/GitHub.
8. Create sample trigger.
9. Run test task.

Offer:

```text
Engineering Starter
Marketing Starter
Blank Organization
```

---

# 44. Example End-to-End Demo

This should become a documented demo and E2E fixture.

## User configuration

```text
Team: Engineering
Manager: CTO
Worker: Senior SWE
Worker: QA

Linear connection: Acme Linear
GitHub connection: Acme Engineering

Trigger:
When ENG issue enters Todo
Assign to Senior SWE
```

## Event

`ENG-142` moves to Todo.

## Expected timeline

```text
12:00:00 Linear webhook received
12:00:00 Event verified
12:00:01 Trigger matched
12:00:01 Task ENG-142 created
12:00:01 Temporal workflow started
12:00:02 Senior SWE started
12:00:04 Linear issue read
12:00:08 Repository inspected
12:00:10 Sandbox started
...
12:08:12 Tests passed
12:08:30 PR #381 opened
12:08:31 QA delegated
12:08:34 QA started
...
12:11:02 QA passed
12:11:03 Merge approval requested
```

The UI should make this understandable without looking at container logs.

---

# 45. Implementation Phases

Each phase must end in independently testable software. Do not build all infrastructure before demonstrating a vertical slice.

## Phase 1 — Foundation + Full Local Stack

**Deliverable:** repository boots via Docker Compose with web, API, Postgres, NATS, Temporal, and workers.

- [ ] Create monorepo structure.
- [ ] Configure Python and frontend workspaces.
- [ ] Add FastAPI health API.
- [ ] Add Next.js shell.
- [ ] Add Postgres.
- [ ] Add Alembic.
- [ ] Add NATS with JetStream enabled.
- [ ] Add Temporal service and UI.
- [ ] Add workflow worker with health visibility.
- [ ] Add event worker.
- [ ] Add Compose healthchecks.
- [ ] Add CI lint/type/test/build.
- [ ] Add `make` commands.
- [ ] Prove a sample Temporal workflow survives worker restart.
- [ ] Prove a NATS JetStream test message survives consumer restart.

**Exit test:** `docker compose up -d` produces a healthy stack and integration tests pass.

## Phase 2 — Auth, Workspaces, Teams, Agents

**Deliverable:** user can log in and create the organization graph.

- [ ] Implement owner bootstrap.
- [ ] Implement sessions/auth.
- [ ] Workspace CRUD.
- [ ] Team CRUD.
- [ ] Agent CRUD.
- [ ] Manager relation.
- [ ] Cycle prevention.
- [ ] Organization graph endpoint.
- [ ] Organization UI.
- [ ] Agent detail UI.
- [ ] Agent creation wizard.
- [ ] Audit basic configuration changes.

**Exit test:** create Engineering → CTO → SWE + QA from browser and reload without losing hierarchy.

## Phase 3 — Models + Basic Agent Runs

**Deliverable:** user can configure a provider/model and message an agent.

- [ ] Implement model provider interface.
- [ ] Implement encrypted secret infrastructure before storing API keys.
- [ ] Implement OpenAI-compatible adapter.
- [ ] Add OpenAI/Anthropic/OpenRouter/Ollama adapters.
- [ ] Model profile UI.
- [ ] Per-agent model selection.
- [ ] Agent execution snapshot.
- [ ] Minimal LangGraph run.
- [ ] Temporal `AgentTaskWorkflow`.
- [ ] Manual task assignment.
- [ ] Run timeline.
- [ ] Token/cost tracking.

**Exit test:** create two agents with different models and run each through a Temporal-backed task.

## Phase 4 — Policy, Permissions, Tool Gateway

**Deliverable:** agents can call tools only when explicitly authorized.

- [ ] Define capability registry.
- [ ] Agent capability grants.
- [ ] Policy engine.
- [ ] Risk levels.
- [ ] Approval primitives.
- [ ] Tool gateway.
- [ ] Sanitization/redaction.
- [ ] Tool-call audit records.
- [ ] Agent tools/access UI.
- [ ] Approval UI.

**Exit test:** same tool succeeds for granted agent and deterministically fails for ungranted agent; approval-gated call waits durably.

## Phase 5 — Connector Framework + GitHub

**Deliverable:** secure real GitHub connection and repository workflow.

- [ ] Connector SDK/interfaces.
- [ ] Connection CRUD.
- [ ] Secret-backed connector auth.
- [ ] Connection verification.
- [ ] GitHub App path.
- [ ] PAT fallback.
- [ ] GitHub read tools.
- [ ] branch/PR tools.
- [ ] GitHub webhook verification.
- [ ] Connector UI.

**Exit test:** granted agent reads configured repo and creates a PR branch; ungranted repo access fails.

## Phase 6 — Sandbox CLI

**Deliverable:** coding agent can safely work in an ephemeral repository sandbox.

- [ ] Sandbox runner service.
- [ ] No Docker socket inside jobs.
- [ ] Resource limits.
- [ ] Timeouts.
- [ ] Network modes.
- [ ] Secret injection/redaction.
- [ ] Repository checkout job.
- [ ] Command execution.
- [ ] stdout/stderr streaming as sanitized events.
- [ ] Automatic cleanup.
- [ ] Security integration tests.

**Exit test:** SWE checks out test repo, edits file, runs tests, pushes branch, opens PR; sandbox is destroyed afterward.

## Phase 7 — Linear Connector + Trigger Engine

**Deliverable:** Linear Todo transition automatically starts the SWE.

- [ ] Linear connection.
- [ ] Linear tools.
- [ ] Linear webhook endpoint.
- [ ] Signature verification.
- [ ] Event normalization.
- [ ] NATS ingress.
- [ ] Trigger data model.
- [ ] JSON filter engine.
- [ ] Transition matching.
- [ ] Trigger builder UI.
- [ ] Trigger test UI.
- [ ] Event idempotency.
- [ ] Start `TriggeredTaskWorkflow`.
- [ ] Link task to Linear issue.
- [ ] Duplicate-delivery test.

**Exit test:** moving one fixture issue into Todo starts exactly one SWE task.

## Phase 8 — Delegation + Teams

**Deliverable:** CTO/SWE/QA style hierarchical work.

- [x] Structured agent messages.
- [x] Delegation tool.
- [x] Child Temporal workflows.
- [x] Delegation permissions.
- [x] Manager result summaries.
- [x] Task parent/child display.
- [x] Engineering template.
- [x] QA workflow.
- [x] failure/fix/retest loop.
- [x] Concurrency controls.

**Exit test:** SWE can delegate QA or manager can route QA; failed QA returns work and retest completes.

## Phase 9 — Vercel + Supabase

**Deliverable:** useful production integrations with tight scopes.

- [x] Vercel connector.
- [x] Vercel tools and scopes.
- [x] deployment events where supported.
- [x] Supabase connector.
- [x] management-plane tools.
- [x] SQL read-only path.
- [x] SQL write policy.
- [x] statement timeout/max rows.
- [x] elevated/destructive approvals.
- [x] connector-specific tests.

**Exit test:** agent can inspect deployment and read allowed DB data but is blocked from unauthorized production changes.

## Phase 10 — Production Operations

**Deliverable:** serious self-hosting release candidate.

- [ ] OpenTelemetry.
- [ ] metrics.
- [ ] structured logs.
- [ ] health dashboard.
- [ ] DLQ UI.
- [ ] retry controls.
- [ ] backups docs.
- [ ] restore docs.
- [ ] key rotation.
- [ ] database upgrade strategy.
- [ ] reverse proxy/TLS docs.
- [ ] resource sizing guide.
- [ ] secret/logging audit.
- [ ] chaos tests.

**Exit test:** kill/restart workers during a live task; workflow recovers and UI accurately reflects state.

## Phase 11 — Open-Source Release

**Deliverable:** public project ready for external users/contributors.

- [ ] Choose final name.
- [ ] Apache-2.0 license.
- [ ] README.
- [ ] architecture docs.
- [ ] deployment guide.
- [ ] contributor guide.
- [ ] security policy.
- [ ] code of conduct.
- [ ] screenshots/demo.
- [ ] seeded starter templates.
- [ ] fake/demo connector mode.
- [ ] issue templates.
- [ ] release automation.
- [ ] container images.
- [ ] SBOM/security scanning.
- [ ] first tagged release.

---

# 46. Suggested Detailed File Ownership

## API

```text
apps/api/src/jhin_api/main.py
apps/api/src/jhin_api/auth/
apps/api/src/jhin_api/routes/agents.py
apps/api/src/jhin_api/routes/teams.py
apps/api/src/jhin_api/routes/tasks.py
apps/api/src/jhin_api/routes/runs.py
apps/api/src/jhin_api/routes/connections.py
apps/api/src/jhin_api/routes/triggers.py
apps/api/src/jhin_api/routes/secrets.py
apps/api/src/jhin_api/routes/webhooks/
```

## Domain

```text
packages/domain/jhin_domain/agents.py
packages/domain/jhin_domain/teams.py
packages/domain/jhin_domain/tasks.py
packages/domain/jhin_domain/connections.py
packages/domain/jhin_domain/triggers.py
packages/domain/jhin_domain/runs.py
```

## Events

```text
packages/events/jhin_events/envelope.py
packages/events/jhin_events/subjects.py
packages/events/jhin_events/publisher.py
packages/events/jhin_events/consumer.py
packages/events/jhin_events/idempotency.py
```

## Workflows

```text
packages/workflows/jhin_workflows/triggered_task.py
packages/workflows/jhin_workflows/agent_task.py
packages/workflows/jhin_workflows/delegated_task.py
packages/workflows/jhin_workflows/engineering_ticket.py
packages/workflows/jhin_workflows/activities/
```

## Agents

```text
packages/agents/jhin_agents/runtime.py
packages/agents/jhin_agents/snapshot.py
packages/agents/jhin_agents/context.py
packages/agents/jhin_agents/graph.py
packages/agents/jhin_agents/nodes/
packages/agents/jhin_agents/delegation.py
```

## Policy

```text
packages/policy/jhin_policy/capabilities.py
packages/policy/jhin_policy/evaluator.py
packages/policy/jhin_policy/risk.py
packages/policy/jhin_policy/approvals.py
```

## Secrets

```text
packages/secrets/jhin_secrets/crypto.py
packages/secrets/jhin_secrets/store.py
packages/secrets/jhin_secrets/redaction.py
packages/secrets/jhin_secrets/rotation.py
```

## Models

```text
packages/models/jhin_models/base.py
packages/models/jhin_models/router.py
packages/models/jhin_models/providers/openai.py
packages/models/jhin_models/providers/anthropic.py
packages/models/jhin_models/providers/openrouter.py
packages/models/jhin_models/providers/ollama.py
packages/models/jhin_models/providers/openai_compatible.py
```

---

# 47. Coding Standards

Python:

- Ruff;
- mypy strict where practical;
- pytest;
- async I/O for network/database operations;
- explicit typed DTOs;
- no giant service classes;
- dependency injection at boundaries;
- no hidden global clients.

TypeScript:

- strict mode;
- ESLint;
- Vitest;
- Playwright;
- Zod only where runtime validation is needed;
- generated API types.

General:

- functions/modules should be small enough to reason about;
- explicit interfaces between packages;
- no business logic in route handlers;
- no connector-specific logic in the generic trigger engine;
- no model-provider-specific logic in agent orchestration.

---

# 48. Required Security Invariants

These are release blockers.

1. A model cannot retrieve plaintext stored secrets.
2. An agent cannot invoke a tool without a matching capability grant.
3. An agent cannot use a connection it was not granted.
4. A user in Workspace A cannot read or execute Workspace B resources.
5. Webhook authenticity is verified before event processing.
6. Duplicate external events do not duplicate durable work.
7. Sandbox jobs do not mount Docker socket or host root.
8. Destructive/elevated actions follow configured approval policy.
9. Logs and audit metadata redact known credentials.
10. Secrets are encrypted at rest using a master key not stored in Postgres.
11. The UI never receives stored plaintext credentials.
12. Workflow state survives worker restarts.
13. NATS event processing is idempotent.
14. All externally supplied content is treated as untrusted.
15. Agents cannot alter their own permissions.

---

# 49. Definition of Production Ready

Do not label the first release production-ready until all of the following are true:

- full Compose stack restarts cleanly;
- database migrations are deterministic;
- documented backup and restore has been tested;
- master-key recovery procedure tested;
- Temporal workflow recovery test passes;
- NATS redelivery/idempotency tests pass;
- workspace isolation tests pass;
- webhook signature tests pass;
- secret-redaction tests pass;
- sandbox escape-risk review completed;
- no Docker socket exposed to agents;
- rate limits exist;
- approval gates exist;
- health endpoints exist;
- structured logs exist;
- critical metrics exist;
- dependency/container scanning is enabled;
- a fresh machine can follow README and reach working onboarding;
- at least one real GitHub + Linear end-to-end scenario passes;
- upgrade path from previous tagged version is tested once releases begin.

---

# 50. Architecture Decision Records to Create

Create ADRs as implementation begins:

```text
ADR-001 Temporal as durable workflow authority
ADR-002 NATS JetStream as event backbone
ADR-003 PostgreSQL as product source of truth
ADR-004 LangGraph scope limited to agent reasoning
ADR-005 Envelope encryption for user secrets
ADR-006 Ephemeral sandbox execution
ADR-007 Capability-based agent authorization
ADR-008 Connector SDK boundary
ADR-009 Event envelope and idempotency strategy
ADR-010 SSE for initial realtime UI
```

---

# 51. First Vertical Slice

The coding agent should resist building every connector first.

The first compelling production-shaped vertical slice is:

```text
Login
↓
Create Engineering team
↓
Create Senior SWE agent
↓
Configure model
↓
Connect GitHub
↓
Connect Linear
↓
Grant scoped permissions
↓
Create “Linear → Todo” trigger
↓
Move issue to Todo
↓
Temporal starts task
↓
Agent reasons
↓
Sandbox edits repository
↓
Tests run
↓
PR created
↓
Timeline updates live
```

Only after that works should the implementation expand to delegation/QA/Vercel/Supabase.

This proves the architecture instead of producing disconnected infrastructure.

---

# 52. Non-Negotiable Implementation Guidance for the Coding Agent

- Do not bypass Temporal by running long agent jobs in FastAPI background tasks.
- Do not use NATS as the canonical task database.
- Do not put external API keys in prompts.
- Do not grant sandbox jobs access to the Docker daemon socket.
- Do not use unrestricted shell execution in the API container.
- Do not expose internal infrastructure ports by default.
- Do not permit arbitrary Python/JS in trigger filters.
- Do not silently retry non-retryable authorization failures.
- Do not make connector-specific assumptions in core agent/team models.
- Do not treat model output as authorization.
- Do not render hidden chain-of-thought in the UI.
- Do not allow a webhook retry to create duplicate tasks.
- Do not build Kubernetes before Docker Compose is excellent.
- Do not add Redis unless a concrete need appears; Temporal + NATS + Postgres already cover the required durable workflow, event, and persistent-data responsibilities.
- Do not introduce a second vector database in V1; use Postgres/pgvector if semantic memory is needed.
- Prefer simple deterministic code over using an LLM for operations that do not require reasoning.

---

# 53. Suggested Implementation Order for Agentic Development

The implementing coding agent should work in small reviewable branches/commits.

For each task:

1. write contract/types;
2. write failing unit/integration test;
3. implement smallest working behavior;
4. run focused test;
5. run affected package suite;
6. run lint/type check;
7. commit;
8. continue.

At the end of each phase:

1. boot fresh Compose environment;
2. run migrations from empty database;
3. run phase acceptance scenario;
4. run full CI suite;
5. update docs;
6. create checkpoint tag/branch if desired.

Avoid a single giant “initial implementation” commit.

---

# 54. Initial Milestone Issues

Create these as repository issues after scaffolding:

```text
M1-01 Bootstrap monorepo and Docker Compose
M1-02 Add Postgres and migration framework
M1-03 Add Temporal and durable sample workflow
M1-04 Add NATS JetStream and event package
M1-05 Add authentication and owner bootstrap
M1-06 Add workspace/team/agent domain
M1-07 Build organization UI
M1-08 Implement encrypted secret store
M1-09 Implement model-provider abstraction
M1-10 Run first Temporal-backed agent task
M1-11 Implement capability policy engine
M1-12 Implement approvals
M1-13 Implement connector SDK
M1-14 Implement GitHub connector
M1-15 Implement sandbox runner
M1-16 Implement Linear connector/webhooks
M1-17 Implement trigger engine
M1-18 Complete Linear Todo → SWE → GitHub PR vertical slice
M1-19 Implement agent delegation
M1-20 Implement QA loop
M1-21 Implement Vercel connector
M1-22 Implement Supabase connector
M1-23 Add observability and operational UI
M1-24 Security hardening
M1-25 Open-source release readiness
```

---

# 55. Success Criteria

The project succeeds when a new user can self-host it, open the browser, configure an organization, and achieve this without editing source code:

```text
1. Add OpenAI/Anthropic/OpenRouter/Ollama-compatible model credentials.
2. Create Engineering team.
3. Create CTO, Senior SWE, and QA agents.
4. Assign models individually.
5. Connect a GitHub installation.
6. Connect Linear.
7. Restrict SWE to a specific repository.
8. Restrict QA to read/test access.
9. Configure a Linear Todo trigger.
10. Move a ticket to Todo.
11. Watch SWE pick it up.
12. Watch code execute in an isolated sandbox.
13. Watch a PR appear.
14. Watch QA receive delegated work.
15. Approve a protected final action when policy requires it.
16. Inspect a complete audit/timeline afterward.
17. Restart the host during a task and see the task recover rather than disappear.
```

That experience is the product.

---

# 56. Final Architecture Summary

```text
                            ┌─────────────────────┐
                            │      Next.js UI     │
                            └──────────┬──────────┘
                                       │
                            ┌──────────▼──────────┐
                            │   FastAPI Control   │
                            │       Plane         │
                            └───┬──────┬──────┬───┘
                                │      │      │
                    ┌───────────┘      │      └────────────┐
                    ▼                  ▼                   ▼
              PostgreSQL          NATS JetStream       Temporal
            source of truth       event backbone        durable
                                                         workflows
                                                            │
                                            ┌───────────────┴──────────────┐
                                            ▼                              ▼
                                      Agent Worker                    Tool Worker
                                      LangGraph                          │
                                            │                             │
                                     Model Providers                 Policy Gate
                                            │                             │
                                            │                         Secret Store
                                            │                             │
                                            │             ┌───────────────┼─────────────┐
                                            │             ▼               ▼             ▼
                                            │           GitHub          Linear        Vercel
                                            │                             │
                                            │                          Supabase
                                            │
                                            ▼
                                      Sandbox Runner
                                   ephemeral containers
```

### Mental model

- **Postgres** remembers the company.
- **Temporal** remembers the work.
- **NATS** carries what happened.
- **LangGraph** decides what an agent should do next.
- **Tool Gateway** decides what an agent is actually allowed to do.
- **Secret Store** supplies credentials without exposing them to the model.
- **Sandbox Runner** gives coding/CLI agents somewhere safe to execute.
- **Next.js/FastAPI** make the whole system understandable and controllable.

Keep those boundaries intact and the platform can scale from a single self-hosted user with five agents to a much larger organization without requiring a rewrite of the core model.

---

# 57. Reference Documentation the Implementer Should Consult

Use current official documentation at implementation time for:

- Temporal Python SDK, workflows, Activities, signals, child workflows, cancellation, retries, testing, and self-hosting.
- NATS JetStream streams, durable pull consumers, deduplication, acknowledgments, and replay.
- LangGraph persistence, interrupts, tool execution, and checkpointing.
- GitHub Apps, installation tokens, webhook signatures, REST/GraphQL APIs.
- Linear OAuth/API keys, GraphQL API, workflow states, and webhook signatures/events.
- Vercel REST API and authentication.
- Supabase Management API and PostgreSQL security guidance.
- FastAPI security/session guidance.
- Docker Engine security, rootless/container isolation, resource limits, and Compose.
- OWASP ASVS and prompt-injection/SSRF guidance.

Do not copy examples blindly if upstream APIs have changed. Pin and document dependency versions in the repository lockfiles.

---

# 58. Handoff Instruction

Treat this document as the product/architecture plan. Before implementing each major subsystem, write the focused technical spec and tests for that subsystem. Preserve the architectural boundaries and security invariants above.

For agentic execution, use a fresh implementation context for each independently reviewable task and perform a review gate before moving to the next task. The first priority is the **Linear Todo → SWE → isolated repository work → GitHub PR** vertical slice; everything else should support that path or wait until it works.
