# Phase 10 Production Operations Design

**Status:** Implementation design derived from the approved authoritative plan

## Summary

Phase 10 turns the current Phase 9 stack into a serious self-hosting release
candidate. This design defines how all fourteen Production Operations checklist
items are closed:
OpenTelemetry, metrics, structured logs, a health dashboard, a DLQ UI, retry
controls, backup documentation, restore documentation, master-key rotation, a
database upgrade strategy, reverse-proxy and TLS documentation, a resource
sizing guide, a secret/logging audit, and chaos tests.

The architecture is a hybrid:

- product-visible operations state is sanitized, workspace-scoped, and backed
  by PostgreSQL;
- traces and metrics use vendor-neutral OpenTelemetry and Prometheus
  interfaces;
- an optional Compose profile supplies OpenTelemetry Collector, Prometheus,
  Grafana, and Tempo for operators who want a complete local monitoring stack;
- structured JSON on stdout remains the baseline log interface and works when
  the optional profile is disabled;
- the product UI renders application events and sanitized operational records,
  never raw infrastructure logs or trace payloads.

PostgreSQL remains the product source of truth, Temporal remains the durable
workflow authority, and NATS JetStream remains the event transport. Neither the
monitoring stack nor the browser becomes an alternate authority for work,
events, retries, permissions, or credentials.

## Scope and non-goals

### In scope

- End-to-end tracing of API requests, Temporal activities, model calls,
  connector HTTP calls, NATS publish/consume, sandbox jobs, and useful database
  operations.
- The complete metric set required by implementation-plan section 22.2.
- A versioned JSON log contract shared by every application service.
- Protected, sanitized system health and event-failure operations in the web
  application.
- Durable event dead-lettering and replay after exhausted delivery.
- Safe manual task retry after terminal failure.
- Tested backup, restore, key recovery, application/database upgrade, proxy,
  TLS, and sizing procedures.
- Master-key rotation without re-encrypting secret ciphertext.
- Cross-sink secret-leak auditing, dependency/container scanning, and
  deterministic recovery/chaos tests.
- Production Compose hardening needed for the Phase 10 release candidate.

### Out of scope

- Kubernetes, a hosted control plane, or a managed monitoring service.
- Making Prometheus, Grafana, Tempo, or the Collector mandatory for ordinary
  product operation.
- Showing raw spans, raw logs, SQL, model prompts, hidden reasoning, webhook
  bodies, or secret material in the product UI.
- General arbitrary Temporal workflow reset. Phase 10 exposes a defined task
  retry operation, not a generic workflow-history editor.
- Automatic replay of an event after delivery exhaustion. An administrator
  must remediate the cause and request replay.
- Zero-downtime database or PostgreSQL major-version upgrades. The supported
  self-hosted baseline is a documented maintenance-window procedure.
- Renaming the product, trademark/domain/package-registry research, or changing
  the configurable `APP_NAME`. The current Jhin name is outside Phase 10.
- Creating an inert `tool-worker` service. Phase 10 completes the authoritative
  topology with a real Temporal `tool-worker` that owns deterministic gateway
  and connector activities. Model reasoning remains in `agent-worker`, while
  risky CLI work still crosses the existing `sandbox-runner` boundary.

## Release principles

1. **Operations state is durable and sanitized.** A browser does not inspect
   NATS, Temporal, Docker, Prometheus, or Tempo directly. The API projects safe
   operational state from PostgreSQL and bounded live checks.
2. **Telemetry is diagnostic, not authoritative.** Missing spans or metrics
   must not change task, event, approval, retry, or audit behavior.
3. **Recovery never weakens authorization.** Replay and retry re-check current
   workspace access, capability, approval, budget, concurrency, connection, and
   target state before any new effect.
4. **At-least-once transport, at-most-once durable work.** NATS and command
   dispatch may redeliver. Deterministic IDs, database uniqueness, Temporal
   workflow IDs, and gateway invocation claims prevent duplicate durable work
   and externally visible effects.
5. **No secret depends on a logging processor for safety.** Values are
   sanitized before persistence or export. The JSON logging processor is a
   final defense, not the sole defense.
6. **Optional infrastructure fails open for product availability.** A Collector,
   Prometheus, Grafana, or Tempo outage degrades telemetry and appears in the
   protected operations view; it does not stop API requests or agent work.
7. **Production defaults fail closed.** Non-local deployment refuses known
   development credentials, insecure public URLs, insecure cookies, or a
   missing master-key file.

## Architecture and trust boundaries

### Product plane

The product plane contains web, API, PostgreSQL, NATS, Temporal, workers, and
sandbox runner. Existing `edge`, `control`, `data`, and `runner` networks remain
separate.

- `web` receives browser traffic only through the reverse proxy.
- `api` owns authentication, workspace authorization, operations projections,
  replay/retry requests, and audit creation.
- `postgres` owns application and operations records.
- `nats` carries ingress, canonical, audit, DLQ notification, and replayed
  events. It does not own replay status.
- `temporal` owns workflow histories and activity retry state.
- `agent-worker` decrypts only model-provider secrets, runs reasoning, binds the
  complete ordered tool-call manifest before effect zero, and persists the
  final step projection. It never executes connector or CLI tools.
- `tool-worker` consumes a dedicated Temporal task queue and owns deterministic
  tool-gateway request and approval-resolution activities. It re-resolves
  authorization, policy, connection state, and connector credentials at each
  execution boundary, and it never receives prompts, completions, private
  reasoning, or unrelated conversation history.
- `event-worker` consumes NATS, records delivery failures, dispatches durable
  operational commands, and starts triggered work through Temporal.
- `workflow-worker` owns general workflow execution and does not gain database
  or master-key access solely for observability.
- `sandbox-runner` retains its Docker-socket boundary but runs as a dedicated
  non-root UID/GID. It has neither the application database credential nor the
  master key. Job secrets are per-request, in memory, and registered in the
  per-job redactor. Docker access is either a rootless user socket or an
  explicitly mapped socket GID; there is no privileged or root fallback.

The public liveness and readiness endpoints return only an opaque status. The
workspace operations API requires an authenticated workspace administrator.
No workspace administrator can inspect another workspace's failures, retries,
connection health, or identifiers.

### Monitoring plane

The optional `observability` Compose profile adds:

- `otel-collector`, receiving OTLP from product services;
- `prometheus`, scraping the Collector's Prometheus exporter;
- `tempo`, receiving traces from the Collector;
- `grafana`, provisioned with version-controlled Prometheus and Tempo data
  sources and dashboards.

These services use an internal `monitoring` network. Product services initiate
OTLP connections to the Collector; Prometheus and Tempo do not receive product
database, NATS, Temporal, Docker-socket, master-key, or connector credentials.
Grafana is not embedded in Jhin and is not publicly bound by default. The dev
overlay may bind it to `127.0.0.1`; a remote operator may expose it only behind
separate administrative authentication or a private network.

Tempo and Prometheus are diagnostic stores. Required backup and restore does
not depend on them. Dashboards and data-source configuration are provisioned
from the repository so they can be recreated.

### Operator plane

Host operators control:

- reverse proxy and certificates;
- Compose secrets and the versioned master-key file;
- backup archives and restore destinations;
- database, NATS, Temporal, and image upgrades;
- monitoring retention and administrative access;
- the master-key rotation CLI.

Browser workspace administrators cannot read key files, rotate the master key,
download raw backups, change proxy trust, or run database upgrades. Those
remain host-operator actions.

## Data model

Phase 10 adds additive tables. Every workspace-scoped foreign key includes the
workspace boundary in its lookup and service-layer authorization. UUID primary
keys use the existing UUIDv7 helper. Timestamps are UTC.

### `event_processing_state`

One row durably counts business-handler attempts for one source message before
the terminal failure row exists.

- `origin_stream`, `consumer_name`, and `source_stream_sequence`, forming the
  unique key;
- nullable validated `workspace_id`, `event_id`, and `correlation_id`;
- `handler_attempt_count`, constrained from zero through five;
- `mode`: `handling`, `quarantine_only`, or `completed`;
- nullable closed-enum `last_reason_code`;
- nullable `claim_token` and `claim_expires_at` for a bounded handler lease;
- `first_seen_at`, `last_attempted_at`, and standard timestamps.

Before business handling, the consumer transactionally creates or locks this
row, refuses a live competing lease, and increments the attempt counter. If
PostgreSQL is unavailable, it invokes no business handler and negatively
acknowledges the message. A successful handler marks `completed` before ack.
The fifth handler exception durably moves the row to `quarantine_only` and
clears its handler lease before any failure-notification write is considered
successful. Every delivery in `quarantine_only` performs only the quarantine
transaction: it locks the state row, creates or reuses the unique
`event_processing_failure` and `operations_outbox` rows, and changes the state
to `completed` in that same transaction. If any quarantine write or commit
fails, the whole transaction rolls back and the previously committed state
remains `quarantine_only`; later delivery retries quarantine without invoking
the business handler. A redelivery that finds `completed` also skips the
handler. A completed success needs no failure row and is acknowledged; a
completed quarantine is identified by its terminal `last_reason_code`, must
have the unique failure/outbox records, and is terminated. Worker restart,
lease expiry, or failed quarantine persistence therefore cannot reset the
attempt count or invoke business handling a sixth time. The retention job
purges only `completed` rows after the source retention window plus seven days;
it never purges `handling` or `quarantine_only` rows as if they were finished.

### `event_processing_failure`

One row represents one consumer's terminal handling failure for one source
stream message.

- `id`
- nullable `workspace_id`; null is allowed only when a malformed subject and
  envelope provide no valid workspace
- nullable `event_id` and `correlation_id`
- `origin_stream`: `INGRESS` or `EVENTS`
- `consumer_name`
- bounded `subject`
- `source_stream_sequence`
- nullable `source_consumer_sequence`
- `delivery_count`
- `handler_attempt_count`, capped at the configured processing-attempt limit
- `reason_code`, selected from a closed application enum
- nullable `safe_error_class`
- nullable `safe_error_detail`, already redacted and capped at 2,000
  characters
- `status`: `open`, `replay_requested`, `replayed`, `resolved`, or `expired`
- `first_failed_at`, `last_failed_at`
- nullable `replayed_at`, `resolved_at`, `resolved_by_user_id`
- nullable `latest_replay_request_id`
- standard created/updated timestamps

`(origin_stream, consumer_name, source_stream_sequence)` is unique. No raw
message body, webhook body, provider response, authorization value, cookie,
prompt, tool input, or stack trace is stored. Rows with null workspace are
visible only in host-operator metrics/logs, never through a workspace API.

Open rows remain until resolved. Resolved/replayed/expired rows are retained for
90 days, then a bounded purge records an aggregate audit event before deletion.
The source NATS message remains subject to the existing 7-day INGRESS or 14-day
EVENTS retention, so replay eligibility can expire before the failure record.

### `event_replay_request`

One row is a durable command to replay one failure.

- `id`
- `workspace_id`
- `failure_id`
- `requested_by_user_id`
- `idempotency_key`, supplied by the UI client and unique within the workspace
- `replay_generation`, monotonically increasing per failure
- deterministic `replay_event_id`
- `status`: `requested`, `dispatching`, `published`, `superseded`, or `failed`
- `attempt_count`, `available_at`, nullable `safe_error_code`
- nullable `published_at`
- standard created/updated timestamps

There is at most one nonterminal replay request per failure. The deterministic
replay event ID derives from request ID, not from retry timing.

### `task_retry`

One row is a durable request to start a new attempt for an existing terminal
task.

- `id`
- `workspace_id`, `task_id`, and nullable `source_run_id`
- `requested_by_user_id`
- `idempotency_key`, unique within the workspace
- `attempt_number`, unique per task
- deterministic `temporal_workflow_id`:
  `task-{task_id}-attempt-{attempt_number}`
- `configuration_mode`, fixed to `current` in Phase 10
- `status`: `requested`, `dispatching`, `started`, `rejected`, or `failed`
- nullable `new_run_id`
- nullable `safe_reason_code`
- `attempt_count`, `available_at`, nullable `started_at`
- standard created/updated timestamps

The existing task remains the user-visible work item and may have multiple
runs. A retry moves an eligible failed task to queued only after its durable
request is committed. The current agent/model/grants/policy/budget are resolved
again; Phase 10 does not silently resurrect the original execution snapshot.

### `operations_outbox`

This table closes the database/NATS split for sanitized DLQ notifications.

- `id`
- nullable `workspace_id`
- `kind`, initially `event_failure_dlq`
- `aggregate_id`
- sanitized, size-bounded `payload_json`
- deterministic `message_id`
- `status`: `pending`, `publishing`, `published`, or `failed`
- `attempt_count`, `available_at`, nullable `published_at`, nullable
  `safe_error_code`
- standard timestamps

`(kind, aggregate_id)` and `message_id` are unique. For
`event_failure_dlq`, the failure row, outbox row, required system audit row, and
`event_processing_state.mode = completed` transition commit together. The
original NATS message is terminated only after that commit. A failed commit
leaves the state `quarantine_only`. A crash after commit but before termination
redelivers the source message, observes the completed quarantine, confirms the
unique failure/outbox records, and terminates without handler invocation.
Outbox publication uses `Nats-Msg-Id=message_id`.

### `service_instance_heartbeat`

This table supplies product-native freshness for services that already have a
database connection.

- `instance_id`, generated once per process boot
- `service`: `api`, `agent-worker`, `tool-worker`, or `event-worker`
- `version`
- `started_at`, `last_seen_at`
- `readiness`: `ok` or `degraded`
- nullable closed-enum `safe_reason_code`
- nullable `sandbox_reachable` for tool-worker probes
- nullable `active_key_version` and sorted `supported_key_versions` for the
  key-bearing `api`, `agent-worker`, and `tool-worker` instances

API, agent-worker, tool-worker, and event-worker update their own rows at a
fixed 10-second interval using a separate short transaction. A current API
request proves at least one API instance is serving, while heartbeat rows
expose stale replicas.
Stale rows are diagnostic only; they never grant authority. Workflow-worker
health comes from Temporal task-queue poller data, so it does not gain a
database credential. Sandbox-runner reachability comes from a bounded probe
made by tool-worker over the existing `runner` network, so API and agent-worker
do not join that network.

Rows older than seven days are purged. A service is stale after 30 seconds;
that threshold is three heartbeat intervals and is fixed across API and UI.

### `master_key_rotation`

One row records host-operator rotation progress without key material.

- `id`
- `from_version`, `to_version`
- `status`: `prepared`, `rewrapping`, `verifying`, `completed`, or `aborted`
- nullable `last_secret_id` checkpoint
- `rows_total`, `rows_rewrapped`, `rows_verified`, `rows_failed`
- `started_at`, nullable `completed_at`
- nullable bounded `safe_error_code`

A partial unique constraint permits only one active rotation. No key bytes,
key-file path, plaintext, fingerprint, ciphertext, nonce, or wrapped DEK enters
this table or an audit record.

### `rate_limit_bucket`

Production rate limits become replica-safe PostgreSQL token buckets rather than
process-local counters.

- `scope`: `login`, `webhook`, `manual_task`, `model`, `tool`, or `sandbox`
- nullable `workspace_id`
- `subject_hash`, a SHA-256 hash of the normalized subject tuple
- `tokens_micros`, integer fixed-point remaining capacity
- `refilled_at`, `updated_at`

`(scope, subject_hash)` is unique. Atomic row locking/refill/consume makes API
and worker replicas share one decision. Buckets unused for 24 hours are purged.
The reference limits are login 10 failures per 5 minutes per normalized
email/IP pair, webhook 120 deliveries per minute per connection, manual task
start/retry 30 per minute per workspace, model 60 attempts per minute per
workspace, tool 120 attempts per minute per workspace, and sandbox 10 starts
per minute per workspace. All limits are operator-configurable downward or
upward: capacity must be an integer from 1 through 1,000,000 and window length
from 1 through 86,400 seconds. A denial returns a safe 429 with `Retry-After`,
records a bounded metric, and never logs the unhashed subject.

## Deterministic tool-worker boundary

Phase 10 completes the service boundary required by the authoritative plan
before telemetry and operations work build on service ownership. A new
`jhin-tool-worker` distribution and `TOOL_TASK_QUEUE = "jhin-tool-queue"`
separate model reasoning from deterministic tool execution.

`AgentTaskWorkflow` orchestrates three explicit activity boundaries per model
step:

1. `reason_agent_step` on the agent queue loads the bounded conversation,
   calls the model, and transactionally binds the complete ordered lossless
   tool manifest before returning any calls;
2. for each bound call in order, `execute_bound_tool` on the tool queue loads
   the manifest entry by workspace/run/step/ordinal and invokes `ToolGateway`;
   arguments are never accepted from the activity payload as a second
   authority;
3. `commit_agent_step` on the agent queue loads gateway outcomes by their
   durable IDs, writes the sanitized transcript/run-event bundle and the
   `agent.step.committed` marker atomically, and returns the step result.

The workflow passes only stable IDs across the queue boundary. The model
response and lossless arguments remain in PostgreSQL's internal manifest, not
Temporal payloads beyond their existing bounded representation. Before each
model step, `resolve_advertised_tools` runs on the tool queue and is the sole
owner of `build_default_catalog`, executable connector registration, and live
grant-to-schema filtering. It returns dependency-light schema DTOs only;
agent-worker converts those DTOs to provider tool schemas and cannot import
`jhin_connectors`.

Approval waits remain in `AgentTaskWorkflow`; after a signal,
`resolve_bound_tool_approval` runs on the tool queue and reuses the existing
approval and gateway invocation identity, then `commit_approval_projection`
on the agent queue writes the sanitized transcript and timeline projection.
Tool activities are idempotent under the bound invocation UUID, and an
`execution_unknown` result is durably projected before the outer workflow
stops without automatic retry.

The boundary includes every existing deterministic effect path, not only
model-requested tools. Trigger comment-back runs as `sync_external_tool` on
the tool queue under its current audited standing-authority contract. Sandbox
workspace deletion runs as best-effort `cleanup_run_workspace` on the tool
queue before agent-side finalization. The agent worker therefore has no
connector registration, sandbox-runner URL, sandbox token, default sandbox
image, or runner-network membership.

Workflow evolution uses
`workflow.patched("phase10-tool-worker-boundary-v1")`; TriggeredTaskWorkflow
and EngineeringTicketWorkflow use separate stable patch IDs for sync routing.
Pre-Phase-10 histories retain their recorded activity names and queue
attributes. Their legacy handlers are compatibility coordinators only: they
call or reattach to deterministically identified compatibility workflows on
`jhin-tool-queue`, then reuse the new commit helpers. They never execute a
connector or contact sandbox-runner locally. Compatibility workflow IDs derive
from the run/step, approval, sync, or cleanup identity, while stable gateway
invocation IDs and database claims remain the at-most-once authority. Patch
deprecation and compatibility-handler removal are forbidden until every
pre-patch history has closed and can no longer be queried.

`tool-worker` owns connector executor registration, connection-secret
decryption, tool-gateway request/approval resolution, trigger sync-back,
sandbox cleanup, and access to PostgreSQL/NATS/Temporal plus sandbox-runner
over the existing runner network. It receives no model-provider configuration
and no prompt/history access. `agent-worker` keeps model-provider secrets and
reasoning/persistence activities but loses connector-executor registration and
runner access. The split is covered by compatibility tests that replay a
pre-Phase-10 normal tool step, parked approval, trigger sync, and finalization
history through versioned workflow code paths, plus at-most-once approval,
crash-gap, and `execution_unknown` integration tests.

## Telemetry core

### Shared initialization

`jhin_observability` becomes the sole service bootstrap for logging, tracing,
and metrics. Each service calls one initialization function before constructing
clients. Configuration includes:

- service name and version;
- environment;
- log level;
- OTLP endpoint and TLS settings;
- trace sampling policy;
- optional redaction processors.

When no OTLP endpoint is configured, tracing and metrics install no-op
providers while JSON logging remains active. Telemetry export is bounded and
nonblocking. Export queues have finite capacity; dropped telemetry increments a
local diagnostic counter/log event and never blocks product work.

The implementation uses supported OTel integrations where they preserve
framework semantics and explicit spans where they do not. In particular,
workflow code never calls nondeterministic clocks or random functions for
telemetry. Temporal context is propagated through SDK interceptors and spans
are created at client/activity boundaries, not by replay-sensitive workflow
logic.

### Trace propagation

The canonical context is:

- OTel `trace_id` and `span_id`;
- Jhin `request_id` for one inbound API request;
- Jhin `correlation_id` for one business chain;
- nullable `task_id` and `run_id`.

Rules:

1. API middleware validates an inbound W3C `traceparent` or creates a root
   context. Arbitrary inbound baggage is discarded; callers cannot inject log
   or metric labels.
2. The generated request ID is returned as `X-Request-ID` and bound into
   structlog contextvars for the request lifetime.
3. NATS publishers inject `traceparent` and `tracestate` into message headers.
   Consumers extract them and create consumer spans. Event-envelope
   `correlation_id` remains the business correlation authority.
4. Temporal clients and workers propagate trace context through SDK headers.
   `task_id`, `run_id`, and `correlation_id` are attached only after validated
   application state supplies them.
5. The reasoning span in agent-worker ends only after the ordered manifest is
   durably bound. Temporal client/activity spans carry the same context across
   `jhin-tool-queue`; tool-worker then creates gateway, connector, approval-
   resolution, and sandbox client spans. Only stable IDs cross the queue, and
   workflow replay does not emit duplicate application spans.
6. Model and connector clients create client spans containing provider or
   connector type, normalized operation, outcome, latency, and retry count.
   They never attach URL query strings, request/response bodies, prompts,
   completions, tool inputs, credentials, or provider error bodies.
7. Sandbox requests propagate trace context in internal HTTP headers, never in
   job environment variables. Sandbox-runner creates a server span and a job
   lifecycle span using job ID only.
8. SQLAlchemy spans use normalized operation/table metadata where useful.
   SQL statement text, bind parameters, DSNs, usernames, database hostnames,
   and result values are disabled.
9. Logs receive trace and span IDs from contextvars. Product application events
   may store trace ID as optional diagnostic metadata, but the user-facing
   timeline continues to be built from `run_event`, messages, tool calls,
   approvals, tasks, and audit records.

Trace IDs, request IDs, correlation IDs, task IDs, and run IDs are allowed as
trace attributes and structured-log fields. They are forbidden as metric
labels. Trace access is operator-only and default Tempo retention is 72 hours.

### Metrics and cardinality policy

Metrics use OTel instruments and OTLP export. The optional Collector exposes a
Prometheus scrape endpoint. External operators can replace the bundled backend
without changing service code.

Required instruments and semantics:

- `agent_runs_total`: counter incremented once when a run row reaches a
  terminal state; labels `service`, `outcome`.
- `agent_run_duration_seconds`: histogram observed once from persisted start
  and completion timestamps; label `outcome`.
- `agent_run_failures_total`: counter for terminal failures; label
  `failure_class` from a closed enum.
- `model_requests_total`: counter per completed provider attempt; labels
  `provider_type`, `outcome`.
- `model_tokens_total`: counter; labels `provider_type`, `direction` where
  direction is `input`, `output`, or `cached`.
- `model_cost_estimate`: monotonic counter with unit USD, derived from committed
  integer micro-dollar cost; label `provider_type`.
- `tool_calls_total`: counter at committed terminal gateway state; labels
  `tool_family`, `risk`, `outcome`.
- `tool_call_failures_total`: counter; labels `tool_family`, `failure_class`.
- `trigger_invocations_total`: counter at durable invocation creation; labels
  `connector_type`, `outcome`.
- `trigger_failures_total`: counter; labels `connector_type`, `failure_class`.
- `sandbox_jobs_total`: counter at terminal sandbox job state; labels
  `outcome`, `network_policy`.
- `sandbox_job_duration_seconds`: terminal histogram; label `outcome`.
- `nats_consumer_lag`: observable gauge read from JetStream consumer info;
  labels `stream`, `consumer`.
- `temporal_activity_failures`: counter from worker interceptors; labels
  `task_queue`, `activity`, `failure_class`. The registered task-queue enum
  includes the general, agent, and `jhin-tool-queue` values; tool activity
  names are normalized registrations rather than manifest- or connector-
  supplied strings.
- `connector_health`: observable gauge by `connector_type`, equal to `1` only
  when every enabled connection of that type has a current healthy state and
  `0` when one or more are unhealthy. Types with zero enabled connections emit
  no health series. A separate connection-count gauge by type/status explains
  the aggregate without a connection-ID label.

Allowed metric labels are limited to:

```text
service, environment, outcome, failure_class, provider_type, connector_type,
tool_family, risk, network_policy, stream, consumer, task_queue, activity,
http_method, http_route, http_status_class, direction
```

Values come from registered closed enums or normalized route/activity names.
Unknown dynamic values map to `other`. The metrics wrapper rejects any label
key outside this list. Metric labels must never contain workspace, user, agent,
team, task, run, event, message, connection, approval, tool-call, sandbox-job,
request, correlation, trace, URL, hostname, repository, project, model-name, or
external-resource identifiers. A unit cardinality test enumerates every
instrument and fails if one request can create an unbounded label value.

Counters attach to committed state transitions or deterministic invocation
records, not activity entry, so activity retry cannot double-count cost or
effects. Attempt-level metrics such as model HTTP attempts are explicitly named
and documented as attempts.

### Structured JSON logs

Every application service writes one JSON object per line to stdout. Python
services route structlog and intercepted stdlib loggers through the shared
renderer. The Next.js server uses a small TypeScript logger with the same field
contract for application startup, shutdown, rewrite, and unexpected server
errors; ordinary browser request access logging remains disabled unless the
same sanitizer and schema are used. The versioned contract contains:

- `schema_version`, fixed to `1` for Phase 10;
- ISO-8601 UTC `timestamp`;
- `level`, `service`, `environment`, `event`, and `logger`;
- optional `trace_id`, `span_id`, `request_id`, `correlation_id`, `workspace_id`,
  `task_id`, and `run_id`;
- bounded operation-specific fields;
- optional `error.type` and closed-enum `error.code`;
- a redacted structured traceback for unexpected internal failures.

The event name is a stable dotted identifier, not an arbitrary error message.
No raw request body, response body, prompt, completion, SQL, tool input/output,
webhook payload, sandbox secret env, complete command env, Authorization header,
cookie, private key, API key, DSN password, or master-key material may enter a
record.

All services install both value-based known-secret redaction and structural
redaction. Structural redaction removes values under credential-bearing keys
such as `authorization`, `cookie`, `password`, `secret`, `token`, `api_key`,
`private_key`, and `dsn`, and strips URL userinfo/query/fragment. Values are
redacted before durable error/audit storage and again before JSON rendering.
Unknown objects are safely stringified only inside the redaction processor.

Docker JSON-file logging uses `max-size: 20m` and `max-file: 5` per service by
default. Operators may replace the Docker log driver, but the application
contract remains JSON stdout. Logs are not exported through OTel in Phase 10;
this avoids duplicate retention and keeps the security audit bounded.

## Protected health and operations UI

### Public endpoints

- `GET /api/v1/health` remains an unauthenticated liveness check and returns
  app name, version, and `status: ok` only.
- `GET /api/v1/health/ready` remains suitable for a local orchestrator but
  returns only `status: ok|degraded`. It does not return dependency names,
  latency, exception type, host, port, or error text.

Compose healthchecks use these opaque endpoints or process-local checks.

### Protected API

`GET /api/v1/workspaces/{workspace_id}/operations/health` requires `AdminCtx`
and returns a bounded snapshot with these components:

- API/database connectivity and latency;
- current Alembic revision versus packaged head;
- NATS JetStream availability and INGRESS/EVENTS consumer pending/redelivery
  counts;
- Temporal availability and pollers for general, agent, and tool task queues;
- fresh agent-worker/tool-worker/event-worker instance counts;
- sandbox reachability reported by fresh tool workers;
- enabled connection health aggregated by connector type;
- open workspace DLQ count and oldest failure age;
- master-key active version, count of secret rows by key version, and the
  active/supported version sets reported by fresh API, agent-worker, and
  tool-worker instances, without key material or fingerprints;
- telemetry exporter configured/recent-success status, which is never included
  in overall product readiness.

Each component contains only `name`, `status: ok|degraded|down|unknown`,
`checked_at`, optional `latency_ms`, a closed `reason_code`, and a safe operator
action. Raw exceptions are logged after redaction and are not returned.

Overall status is `down` only for a product-critical unavailable dependency,
`degraded` for stale/missing workers, lag, schema mismatch, sandbox failure, or
connection failures, and `ok` otherwise. An unavailable optional telemetry
backend is a separate warning.

### UI

An admin-only Operations page contains:

1. system health summary and per-component cards;
2. agent/tool/event worker freshness, general/agent/tool Temporal pollers, and
   queue/consumer lag;
3. connector-type health summary;
4. event failures with filters and replay controls;
5. recent task retry requests and outcomes;
6. master-key version distribution and a link to the host-operator runbook;
7. safe remediation text and last-check timestamps.

The ordinary Overview page keeps an opaque stack badge and links administrators
to Operations. Members/viewers do not receive protected health data. The page
polls while visible; it does not query Prometheus, Tempo, Docker, NATS, or
Temporal from the browser.

Every failure follows implementation-plan section 31: what failed, the step,
whether automatic retry remains, when and why it stopped, safe details, the
recommended action, and a retry/replay control only when valid. Python
tracebacks are never primary or expandable UI content.

## DLQ and event replay

### Delivery exhaustion

The durable pull-consumer loop owns final-delivery behavior. JetStream consumer
`max_deliver` is unlimited for these application consumers; Jhin enforces a
separate fixed `processing_max_attempts` of 5. This distinction ensures a
temporary PostgreSQL outage cannot cause JetStream to abandon the fifth
delivery before Jhin durably records its failure. For handler exceptions:

1. handler attempts below `processing_max_attempts` are negatively acknowledged
   with bounded backoff after the durable `event_processing_state` attempt and
   lease update commits;
2. on the fifth failed handler attempt, the worker durably switches the state
   to `quarantine_only`, clears the handler lease, stops invoking business
   handling, and constructs a safe failure record from message metadata and a
   classified error;
3. the quarantine transaction locks that state row, creates or reuses
   `event_processing_failure`, `operations_outbox`, and the required system
   audit row, and transitions the state from `quarantine_only` to `completed`
   in one Postgres commit;
4. only after commit does the worker terminate the source message;
5. the outbox dispatcher publishes the sanitized notification to
   `jhin.dlq.<origin_stream>` and marks it published;
6. if any failure, outbox, or state-transition write fails, the transaction
   rolls back, later NATS deliveries observe the durable `quarantine_only`
   state, and they retry only the quarantine transaction, never the business
   handler;
7. if termination fails after commit, redelivery observes `completed`, confirms
   the unique failure/outbox rows, and terminates without invoking the handler;
   and
8. retries of any step reuse deterministic unique keys.

Malformed envelopes are terminal immediately and follow the same
`quarantine_only` to atomic failure/outbox/`completed` path. Semantically
unhandled ingress events use reason
`unsupported_ingress_event` and are also recorded rather than silently
terminated. The DLQ notification contains IDs, source stream/sequence,
consumer, subject, delivery count, reason code, and timestamps only.

### Replay API and dispatcher

The workspace-admin API exposes only sanitized PostgreSQL records:

- `GET /api/v1/workspaces/{workspace_id}/operations/event-failures` with
  cursor pagination and bounded status/origin/reason/date filters;
- `GET /api/v1/workspaces/{workspace_id}/operations/event-failures/{id}`;
- `POST /api/v1/workspaces/{workspace_id}/operations/event-failures/{id}/replay`;
- `POST /api/v1/workspaces/{workspace_id}/operations/event-failures/{id}/resolve`
  with a required, redacted plain-text note of at most 1,000 characters; and
- `GET /api/v1/workspaces/{workspace_id}/operations/task-retries` with cursor
  pagination for the Operations history panel.

There is no endpoint for raw NATS message bytes, raw DLQ payloads, bulk replay,
or deletion. List/detail responses use closed reason/status enums and the safe
fields defined above.

`POST /api/v1/workspaces/{workspace_id}/operations/event-failures/{id}/replay`
requires `AdminCtx`, CSRF, and an idempotency key. It is allowed only when:

- the failure belongs to the workspace and is `open`;
- the source stream still retains the exact source sequence;
- no replay is currently nonterminal;
- the failure reason is replayable after operator remediation;
- workspace trigger/rate/budget controls permit subsequent work.

The API commits an `event_replay_request`, marks the failure
`replay_requested`, and records `event.replay_requested`. It may attempt an
immediate dispatch, but the event-worker dispatcher is the durable reconciler.

The dispatcher loads the exact original stream message by stream and sequence,
validates it again, and publishes a new envelope with:

- deterministic new `event_id = replay_event_id` so JetStream's duplicate
  window does not suppress the intended replay;
- unchanged `occurred_at`, `workspace_id`, `source`, and application data;
- the original correlation ID;
- `causation_id` and `replay_of_event_id` set to the original event ID.

The envelope schema gains optional `replay_of_event_id`. Trigger idempotency
continues to derive from trigger, connection, external entity, transition
evidence, and occurrence bucket rather than the transport event ID. Therefore
duplicate replay dispatch can redeliver transport but cannot duplicate durable
triggered work. Other event handlers must retain their existing idempotency
contract before they are marked replayable.

Successful publication marks the request `published`, the failure `replayed`,
and records `event.replayed`. If the source sequence has aged out, the request
is `failed` with `source_event_expired` and the failure becomes `expired`.
Operators can resolve an unreplayable row with a required bounded note;
resolution is audited but does not claim that work ran.

Transient NATS/Temporal/database dispatch errors return the request to
`requested` with capped exponential backoff and do not mark the failure
terminal. `failed` is reserved for nonretryable validation, authorization, or
source-expiry outcomes. A non-expiry terminal request failure atomically returns
the parent failure to `open`, clears `latest_replay_request_id`, and records the
safe reason so remediation can be followed by a new generation. Source expiry
alone moves the failure to `expired`. Claim leases expire, so a dispatcher crash
cannot leave `dispatching` stuck forever.

### Operations audit events

Failure recording commits `event.processing_failed` as a system actor in the
same transaction as the failure/outbox rows and the processing-state transition
to `completed`. Replay and resolution commit
`event.replay_requested`, `event.replayed`, `event.replay_failed`, and
`event.failure_resolved`. Task retry emits the events defined below. Master-key
rotation emits `master_key.rotation_started`, `master_key.rotation_completed`,
or `master_key.rotation_aborted` with versions and counts only. These records
use the existing append-only application audit service. No update or retry
rewrites a prior audit row.

## Task retry controls and at-most-once rules

### Automatic versus manual retry

Temporal activity retry remains the first response to transient failure. The
UI shows its retry state but does not create a manual retry while a task or
workflow is active. Manual retry applies only after a terminal failed task.

`POST /api/v1/workspaces/{workspace_id}/tasks/{task_id}/retry` requires
`MemberCtx`, CSRF, and an idempotency key. The API evaluates eligibility inside
a transaction and either returns a durable `task_retry` request or a stable
reason why retry is unavailable.

### Retry safety classification

Every registered tool definition gains a closed `retry_safety` value:

- `pure`: no external mutation;
- `idempotent`: mutation is protected by a provider idempotency key or Jhin's
  durable invocation identity and can safely return the prior result;
- `non_idempotent`: a fresh invocation may repeat an external effect.

Risk and retry safety remain separate. A low-risk tool may still be
non-idempotent.

A task is manually retryable only when all of these are true:

- task state is `failed` and no workflow attempt is open;
- no tool call is `executing` or `execution_unknown`;
- no external mutation reached a committed or ambiguous execution state in the
  failed attempt. A provider-idempotent write is still a prior effect and is
  not sufficient for Phase 10 manual retry because a fresh model run could
  choose a different business operation or idempotency key;
- the failure is not authorization, policy, invalid configuration, exhausted
  budget, max steps, or explicit rejection until the operator has changed the
  relevant condition;
- assigned agent is active and current workspace/agent concurrency and budget
  admission can queue the work;
- there is no nonterminal retry request.

Phase 10 does not offer an override for any committed/ambiguous prior mutation.
The UI directs the operator to reconcile the external system and create a new
explicit task. The `idempotent` classification continues to govern automatic
activity retry within the same durable attempt, where the canonical invocation
identity is preserved; it does not authorize a fresh manual attempt. This is
stricter than a confirmation dialog and preserves at-most-once safety.

### Durable start

The retry request's deterministic Temporal workflow ID makes repeated starts
idempotent. After the database commit, an immediate dispatcher may start
`AgentTaskWorkflow`; event-worker also reconciles requested/dispatching rows.
Temporal `WorkflowAlreadyStarted` counts as success for that exact retry ID.

Transient dispatch failures return the retry to `requested` with capped
exponential backoff; a leased `dispatching` claim expires after worker loss.
Before every dispatch attempt the reconciler repeats the terminal-state,
retry-safety, authorization, budget, and agent checks. A now-invalid request is
durably `rejected` and no workflow starts.

The workflow receives retry ID and attempt number, resolves a current snapshot,
creates one new `AgentRun`, and links it back to `task_retry`. Gateway invocation
claims remain scoped to each durable attempt; the eligibility rule guarantees
the previous attempt produced no committed/ambiguous external mutation that a
new attempt could repeat. Admission, capabilities, connection scopes,
approvals, secrets, and budgets are read fresh. A second HTTP request with the
same idempotency key returns the same retry; a distinct request while one is
nonterminal receives 409.

The initial request atomically changes the task from failed to queued. A
nonretryable dispatch rejection atomically returns it to failed and records the
safe rejection reason; transient dispatch leaves it visibly queued with retry
request status. A successful workflow admission owns subsequent task/run state
through the existing activities.

The task detail page shows attempt history, original failure, automatic retry
status, manual eligibility, safety reason, and the new run. Actions create
`task.retry_requested`, `task.retry_started`, `task.retry_rejected`, or
`task.retry_failed` audit events.

## Master-key rotation and recovery

### Versioned keyring

The mounted key file changes from one encoded key to a versioned JSON keyring:

```json
{
  "active_version": 2,
  "keys": {
    "1": "base64-encoded-32-byte-key",
    "2": "base64-encoded-32-byte-key"
  }
}
```

The parser rejects duplicate/nonpositive versions, missing active key, unknown
fields, invalid encoding, keys not exactly 32 bytes, group/world-readable
files, and inline environment keyrings in production. A legacy single-key file
is read as version 1 only during the first keyring-capable release.

`SecretCrypto` encrypts with the active version and decrypts by the row's
version. API, agent-worker, and tool-worker load the complete keyring at
startup. They expose only active version and supported-version numbers to
protected health.

### Rotation protocol

The supported protocol is staged:

1. Back up PostgreSQL and the current key file separately and verify both.
2. Deploy keyring-capable code everywhere with only version 1 active.
3. Generate a new key offline, add it to every API/agent-worker/tool-worker
   keyring, keep version 1 active, and restart; every fresh key-bearing instance
   heartbeat must report the exact old active version and both supported
   versions before the rollout gate opens.
4. Set the new version active everywhere and restart; the rotation command
   refuses to begin until every fresh key-bearing instance reports the new
   active version and both supported versions. All new writes then use the new
   version.
5. Run the host-only `jhin-master-key-rotate --from 1 --to 2` command. It takes
   a database advisory lock, creates/resumes `master_key_rotation`, and processes
   bounded primary-key batches with row locks.
6. For each row, unwrap the DEK with the old key and rewrap the same DEK with
   the new key. Secret ciphertext and nonce remain unchanged. The plaintext is
   decrypted only long enough to recompute the master-key-HMAC fingerprint,
   registered with the redactor, and never logged or persisted elsewhere.
7. Verify every secret decrypts, has the target key version, and has the
   expected fingerprint under the target key. Record counts, not values.
8. Take and verify a new backup. Remove the old key from every keyring only
   after zero rows use it and every fresh instance has reported target-version
   verification; restart, require every fresh heartbeat to report only the new
   supported version, and run a credential-use smoke test.

The command is idempotent, resumable from `last_secret_id`, and refuses
`from == to`, an active conflicting rotation, missing key versions, rows with
unexpected versions, or retirement with remaining rows. Aborting stops future
batches but does not reverse already rewrapped rows; both keys continue to read
the mixed set. Rollback before old-key retirement means selecting the prior
active writer while retaining both readers. After retirement, rollback requires
the protected old-key backup.

Credential rotation through existing APIs remains distinct from master-key
rotation. Both are audited, but master-key audit metadata contains versions and
counts only.

## Backup and restore design

### Required backup set

The supported baseline is a maintenance-window, logically consistent backup.
The runbook covers:

- PostgreSQL globals/roles and every database in the bundled cluster,
  including Jhin, Temporal, and Temporal visibility databases;
- NATS JetStream state, including stream/consumer configuration and retained
  messages;
- the master-key keyring as a separately encrypted, separately access-controlled
  artifact;
- operator-owned Compose environment and proxy configuration with secret
  values handled separately;
- a manifest containing Jhin image tag/digest, Alembic revision, PostgreSQL,
  NATS, Temporal, and keyring active/supported versions;
- cryptographic checksums for every archive component.

Sandbox workspaces, ephemeral containers, Prometheus samples, Tempo traces, and
Grafana runtime state are not required product backups. Dashboards and data
sources are repository-provisioned. Optional object storage becomes part of the
required set when enabled.

### Backup procedure

The baseline procedure enters maintenance mode, rejects new mutations and
webhooks with retryable 503 responses, stops application workers after their
in-flight operations reach safe boundaries, and then stops the bundled Temporal
server before capture. With API mutations, workers, and Temporal quiesced,
PostgreSQL logical dumps cover application and Temporal databases without
history/timer drift between dumps. NATS is stopped cleanly before its JetStream
data volume is snapshotted or archived. JetStream state is always part of the
supported backup set. Filesystem copies of a live Postgres or NATS data
directory are not documented as valid backups.

Archives are encrypted off-host. The reference retention policy is seven daily,
four weekly, and twelve monthly backups, plus one verified backup immediately
before every application, database, NATS, Temporal, or master-key upgrade.
Operators may increase retention, but the documented minimum remains.

### Restore procedure

Restore always targets a fresh isolated Compose project first:

1. verify checksums, encryption, and manifest compatibility;
2. restore the keyring with owner-only permissions without printing it;
3. start the manifest-matched PostgreSQL image and restore roles/databases;
4. restore NATS JetStream state while NATS is stopped, then start it;
5. start the manifest-matched Temporal version and verify namespaces/history,
   including that restored in-flight waits and timers resume after workers
   return;
6. run the packaged Alembic revision check, but do not migrate until the
   manifest-matched application can read the restored state;
7. start API and workers, verify protected health, key-version distribution,
   stream consumers, Temporal pollers, and audit/task counts;
8. use stored credentials through their normal connector/model path without
   exposing them;
9. complete one durable task and verify one pending event redelivers at most
   once;
10. only then perform a documented upgrade or production cutover.

The runbook includes full-disaster key recovery, a lost-key failure explanation,
and an explicit statement that database restoration without the matching
keyring cannot recover encrypted credentials. A restore drill runs before each
release candidate and at least quarterly for a maintained deployment.

## Database and service upgrade strategy

### Application schema migrations

Alembic retains exactly one linear head. Each release migration is additive
first, deterministic from an empty database and from the previous tagged
release, and tested against real PostgreSQL. Migrations use bounded backfills,
explicit server defaults, and stable ordering. Long backfills are resumable
application commands rather than one unbounded DDL transaction.

The supported self-host procedure is:

1. read release notes and compatibility matrix;
2. take and verify the complete pre-upgrade backup;
3. enter maintenance mode and stop workers/API mutations;
4. run migrations once from the new immutable API image;
5. verify exact Alembic head and invariants;
6. start services and run health plus smoke acceptance;
7. leave maintenance mode.

Production rollback normally means restoring the previous application image
while leaving additive schema in place. A migration downgrade is supported
only when its automated previous-release downgrade/re-upgrade test passes and
no new-version data would be lost. Destructive contract migrations are deferred
until at least one tagged release no longer reads the old shape. Phase 10's
operations tables and additive fields remain readable by the previous app and
may safely remain after an application rollback.

### PostgreSQL major upgrades

The baseline Compose procedure is logical dump/restore into a fresh volume
using the target supported major version. Merely changing the Postgres image
major against an existing volume is forbidden. The runbook verifies extensions,
roles, database ownership, encodings/locales, restored row counts, constraints,
Alembic head, Temporal databases, and application queries before cutover.
`pg_upgrade` may be documented as an advanced alternative only with its own
version-specific rehearsal; it is not the baseline.

### Temporal and NATS upgrades

Image versions are pinned. Release notes declare tested Jhin/PostgreSQL/NATS/
Temporal combinations. Temporal server/schema upgrades follow upstream
supported ordering and are rehearsed against a restored copy of real workflow
history. NATS upgrades verify stream/consumer configuration and pending
redelivery. Operators upgrade one infrastructure component at a time and run
health/recovery smoke tests between components.

CI maintains a previous-tag fixture once tags exist. Before the first tag, the
current Phase 9 schema/image set is the previous-release fixture for Phase 10.

### External Temporal configuration

Application code remains independent of the bundled Temporal topology. Every
Temporal client supports address, namespace, server name, CA bundle, client
certificate, and client private-key file configuration. The bundled Temporal
service may use plaintext only on the isolated internal `control` network. Any
external self-hosted cluster or Temporal Cloud connection requires TLS and
rejects inline private-key environment values in production. The runbook covers
namespace creation, certificate rotation, connectivity verification, and
switching from bundled to external Temporal without changing workflow IDs.

## Reverse proxy, TLS, and production Compose hardening

### Entry point

Non-local deployment has exactly one published HTTP(S) entrypoint through a
reverse proxy. Web and API container ports are not host-published. The proxy
routes the web application and same-origin `/api/*` traffic, preserving the
existing cookie/CSRF model. PostgreSQL, NATS, Temporal, sandbox-runner, Collector,
Prometheus, and Tempo have no public bindings. Temporal UI and Grafana are
disabled or localhost/private-network/admin-authenticated only.

The runbook supplies a complete Caddy example and equivalent requirements for
Traefik/Nginx-compatible proxies: certificate issuance/renewal, HTTP-to-HTTPS
redirect, request/body/time limits compatible with webhooks, streaming and
WebSocket headers where used, upstream health behavior, forwarded headers, and
administrative UI isolation.

API trusts forwarded scheme/client information only from configured exact proxy
addresses. Untrusted `Forwarded`/`X-Forwarded-*` input is ignored. `APP_URL` must
be the exact public HTTPS origin and CORS remains exact-origin.

### Browser security

Production sets Secure, HttpOnly session cookies, SameSite=Lax, CSRF protection,
and no wildcard authenticated CORS. The application/proxy combination emits:

- HSTS `max-age=31536000`; `includeSubDomains` is enabled only when the
  operator confirms all subdomains are HTTPS; preload is never automatic;
- CSP with per-response nonces, `object-src 'none'`, `base-uri 'self'`, and
  `frame-ancestors 'none'`; no `unsafe-eval` in production;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: same-origin`;
- a least-privilege `Permissions-Policy`;
- cache prevention on authenticated and secret/write-once responses.

### Fail-closed configuration

When `APP_ENV=production`, startup or Compose validation rejects:

- HTTP `APP_URL` or `COOKIE_SECURE=false`;
- default/blank Postgres passwords;
- default/blank sandbox-runner token;
- missing/unreadable/group-readable/world-readable master-key file;
- published database, NATS, Temporal, sandbox-runner, Collector, Prometheus, or
  Tempo ports;
- dev connector allowlists or fake services;
- enabled telemetry exporters using cleartext outside internal exact Compose
  endpoints.

All application images, including sandbox-runner, run non-root. Sandbox-runner
uses a dedicated unprivileged UID/GID and one of two supported Docker access
modes: preferably a rootless Docker socket owned by that UID, or a rootful host
socket whose numeric group is supplied explicitly as `SANDBOX_DOCKER_GID` and
added only as a supplemental Compose `group_add` entry. Startup validates that
the mounted socket is a Unix socket, its owner/group matches the configured
mode, and the process can connect. A mismatch or unreadable socket is a fatal
configuration error. The image and entrypoint never use `user: "0:0"`, sudo,
chmod on the socket, `privileged`, or a root fallback. Spawned job containers
remain UID 1000, never receive the Docker socket, and never share the runner's
supplemental socket group.

The base Compose topology builds and starts a real `jhin-tool-worker` process
on the dedicated `jhin-tool-queue`. It joins only `control`, `data`, and
`runner`; has PostgreSQL, NATS, Temporal, master-key, connector, and runner-
token configuration; has no model-provider configuration; and publishes no
port. Agent-worker joins `control` and `data`, has model-provider but no
connector-executor or runner configuration, and cannot reach the runner
network. Sandbox-runner joins only `runner`. Compose validation asserts these
network, secret, queue, user, group, and port boundaries, and its healthchecks
prove both agent and tool task queues have pollers before the stack is ready.
Acceptance inspects the rendered Compose model and container metadata to prove
sandbox-runner's effective UID is nonzero, `Privileged=false`, its only extra
group is the configured socket GID in rootful mode, and the job-container
template has neither the socket mount nor that group. A connection probe must
pass in each documented mode; an incorrect GID must make runner startup fail
rather than change permissions or identity.

Images are multi-stage and pinned for release. CI produces multi-arch build
evidence and enables dependency plus container vulnerability scanning.
Critical findings fail; high findings require a repository allowlist entry with
an owner, explanation, and expiry date.

Every base and optional-profile service has a meaningful healthcheck. Product
services check their serving loop or bounded dependency readiness; PostgreSQL,
NATS, and Temporal use their native readiness commands; Collector, Prometheus,
Tempo, and Grafana check their own ready endpoints. `depends_on` controls boot
ordering only and never replaces runtime reconnect/backoff. Persistent product
volumes are limited to PostgreSQL, NATS, bundled Temporal data when it is not in
PostgreSQL, and configured object storage. Prometheus, Tempo, and Grafana may
use replaceable diagnostic volumes. Sandbox workspaces remain ephemeral.

### Production abuse controls

The `rate_limit_bucket` service replaces the current per-process login limiter
and is also called at webhook acceptance, manual task start/retry, model
attempt, tool attempt, and sandbox start boundaries. Rate limiting supplements
trigger dedupe, workspace/agent concurrency, connector limits, budgets, and
approval policy; it never converts a denied action into an authorized one.
Webhook rate-limit responses are retryable and do not create a delivery/event
row. Worker-side limit failures become safe visible run failures or durable
queue waits according to the operation contract, never silent drops.

## Resource sizing guide

The guide distinguishes application capacity from external model-provider
limits and includes the optional monitoring footprint. It publishes measured
profiles from the Phase 10 load test:

| Profile | Host baseline | Intended load |
| --- | --- | --- |
| Development | 4 vCPU, 8 GiB RAM, 40 GiB SSD | one developer, one active sandbox, monitoring disabled |
| Small production | 8 vCPU, 16 GiB RAM, 100 GiB SSD | up to 10 active agents, two concurrent runs, one concurrent sandbox |
| Small + bundled monitoring | 12 vCPU, 24 GiB RAM, 150 GiB SSD | small production plus 15-day metrics and 72-hour traces |
| Medium production | 16 vCPU, 32 GiB RAM, 250 GiB SSD | up to 50 active agents, ten concurrent runs, four concurrent sandboxes |
| Medium + bundled monitoring | 24 vCPU, 48 GiB RAM, 400 GiB SSD | medium production plus local monitoring retention |

These are admission baselines, not guarantees about LLM latency or arbitrary
repository builds. The guide tells operators to reduce concurrency when
sandbox memory caps plus service reservations leave less than 20% host RAM or
disk free.

Sizing formulas and thresholds:

- reserve each concurrent sandbox's configured memory cap in full;
- retain at least 20% RAM and disk as headroom;
- size agent workers from measured concurrent activity slots, not total agent
  count;
- size tool workers independently from measured connector/tool activity and
  sandbox-start concurrency; increasing reasoning capacity must not implicitly
  increase external-effect concurrency;
- scale event workers only while preserving one durable consumer name and
  database idempotency;
- alert when NATS consumer lag grows for five minutes, disk free falls below
  20%, Postgres connection use exceeds 70%, or p95 queue wait exceeds the
  documented service objective;
- include Postgres task/audit/tool growth, Temporal history growth, NATS
  retention, Docker logs, backups, Prometheus retention, and Tempo retention in
  disk estimates;
- place backups off-host, so backup retention does not consume the production
  disk budget.

Compose defines service reservations/limits for the validated small profile
and leaves documented overrides for larger hosts. Sandbox caps remain hard
application validation and Docker limits. The load test reports throughput,
p50/p95 task queue wait, API latency, CPU, RSS, Postgres connections, NATS lag,
Temporal activity latency, sandbox concurrency, and disk growth. A profile is
published only when it stays below 80% CPU, 80% RAM, 70% Postgres connections,
and has no sustained consumer lag or failed health check for a 30-minute run.

## Secret and logging audit

Phase 10 records a threat-model/data-flow review covering:

- secret creation, encryption, decryption, rotation, backup, and recovery;
- model and connector HTTP request/error paths;
- webhook ingress and DLQ;
- Temporal payloads/history and activity exceptions;
- tool-worker manifest lookup, gateway/approval resolution, connector, and
  sandbox-call paths;
- NATS headers/envelopes;
- sandbox request env, stdout/stderr, Docker errors, and orphan cleanup;
- structured logs, traces, metrics, audit metadata, run events, public API
  errors, UI error cards, backups, and restore output.

The audit asserts all section 48 invariants remain true. In particular, neither
telemetry nor operations endpoints can retrieve plaintext secrets, authorize a
tool, cross a workspace boundary, bypass webhook validation, repeat durable
work, expose the Docker socket to jobs, bypass approval, or let an agent alter
permissions.

Automated canary tests create unique values for API keys, Authorization
headers, cookies, private-key fragments, DSN users/passwords, webhook secrets,
master-key-like material, sandbox secret env, and nested/URL-encoded forms.
They drive those canaries through successful and failing model, connector,
webhook, sandbox, retry, replay, health, logging, audit, trace, and DLQ paths.
The test scans:

- captured JSON stdout from every service;
- exported trace attributes/events;
- metrics exposition;
- public and protected API bodies;
- workspace-visible database fields and audit metadata;
- Temporal failure/history payloads used by Jhin;
- NATS DLQ messages;
- sandbox status/log output;
- rendered UI fixtures.

Any exact or encoded canary match fails CI. Tests also prove structural
redaction for unknown values under credential-bearing keys and prove that
sanitization occurs before persistence, not only during log rendering.

The release audit includes dependency and container scanning, the existing
sandbox escape-risk review, Compose port/network inspection, non-root image
inspection, master-key file permissions, production-default validation, and a
negative search for development credentials in rendered production config.

## Chaos and recovery tests

### Deterministic test controls

Chaos runs only in a uniquely named isolated Compose project with disposable
volumes and fake external providers. Test-only failpoints are process-local
environment settings supplied by a chaos overlay; they match one exact test
event/job ID and terminate the process at a named boundary. Production startup
rejects any chaos setting. There is no public fault-injection endpoint.

### Required scenarios

1. Start a real agent task, block reasoning before and after the transactional
   manifest bind, hard-kill agent-worker with SIGKILL, verify the UI remains
   running/retrying rather than falsely terminal, restart, and prove one bound
   manifest and one terminal run.
2. Execute a bound tool call and hard-kill tool-worker at the pre-claim,
   post-claim/pre-effect, and post-effect/pre-commit failpoints in separate
   runs. After restart, prove Temporal resumes the dedicated tool queue and the
   gateway records one terminal invocation: one external effect at most, or a
   durable nonretryable `execution_unknown` result when the effect cannot be
   proven absent.
3. Deliver a canonical event, terminate event-worker after durable handler
   work but before NATS ack, restart, and prove redelivery plus database/Temporal
   idempotency creates one task/workflow.
4. Force a handler to fail through its maximum attempts, fail the first
   quarantine commit, and prove the state remains `quarantine_only` while
   redelivery never invokes the handler a sixth time. Allow the next quarantine
   transaction to commit and prove the state, exactly one failure row, and one
   outbox intent commit atomically; the state is then `completed`, one sanitized
   DLQ notification is published, and a delivery-before-termination crash still
   does not re-enter the handler. Remediate, replay twice with the same
   idempotency key, and prove one replay request and at most one durable outcome.
5. Restart workflow-worker while a general workflow timer is pending and while
   an approval wait is open; both histories resume.
6. Restart NATS during publish and Temporal during workflow/activity dispatch;
   clients reconnect with bounded backoff and durable commands reconcile.
7. Restart Postgres during an activity commit; the transaction either commits
   once or retries without a duplicate durable bundle/effect.
8. Hard-kill sandbox-runner during a tool-worker job; tool-worker reports a safe
   failed or execution-unknown state as appropriate, restart reaps the orphan
   container, and no job container receives Docker socket, host-root, or the
   runner's supplemental socket-group access.
9. Rotate the master key during active ordinary reads, complete rewrap, restart
   API/agent-worker/tool-worker, and verify old/new rows throughout the
   supported staged protocol.
10. Restore a fresh project from the release backup and run the worker-restart
   exit scenario against the restored state.

Every scenario asserts product UI/API state, Postgres rows, Temporal workflow
count/status, NATS consumer state, fake-provider external effects, audit events,
and absence of secret canaries. Waiting uses bounded polling with diagnostic
artifacts on failure.

### CI schedule

Pull-request CI runs deterministic unit tests, Temporal time-skipping tests,
DLQ/retry integration, secret-canary tests, migration tests, production Compose
validation, and one agent/tool/event worker recovery scenario. A scheduled
nightly workflow runs the full ten-scenario matrix, backup/restore drill,
previous release upgrade test, dependency/container scans, and uploads
sanitized Compose status, health snapshots, test reports, and service logs on
failure. Normal CI never calls third-party APIs.

## Sub-project decomposition

Phase 10 is implemented as seven independently reviewable specifications/plans.
Each sub-project delivers tests and documentation and can merge without an
incomplete public control.

### 1. Deterministic tool-worker boundary

Owns the `jhin-tool-worker` distribution, dedicated task queue, versioned
workflow/activity split, transactional ordered manifest, gateway ownership,
advertised-tool catalog resolution, ordinary and approval-time tool execution,
trigger sync-back, sandbox cleanup, agent/tool secret and network separation,
non-root runner access contract, pre-Phase-10 history replay compatibility,
and crash-gap/at-most-once tests.
It introduces no product operations control and lands before telemetry so all
later health, trace, rotation, Compose, and chaos evidence observes the final
service topology.

### 2. Telemetry core

Owns shared OTel/log bootstrap, context propagation, metric instruments,
cardinality enforcement, JSON schema, safe-error/redaction helpers, optional
Collector/Prometheus/Grafana/Tempo profile, dashboards, retention, and focused
tests. It does not expose product UI.

### 3. Protected health

Owns opaque public health, `service_instance_heartbeat`, protected health API,
Temporal/NATS/schema/key/connector checks, Operations health UI, permissions,
and health/recovery tests. It consumes telemetry status but does not depend on
the monitoring profile.

### 4. DLQ and retry

Owns event failure/replay/outbox/task-retry models and migrations, delivery
exhaustion, command dispatch/reconciliation, tool retry-safety classification,
processing-state lifecycle and retention, admin DLQ UI, task retry UI, auditing,
idempotency, and at-most-once tests.

### 5. Master-key rotation

Owns keyring parsing, multi-version crypto, rotation state/CLI, rewrap and
fingerprint migration, staged rollout/recovery documentation, protected key
health projection, and mixed-version tests.

### 6. Runbooks and hardening

Owns production-default validation, reverse proxy/TLS/security headers, Compose
ports/networks/resource/log limits, backup/restore, application/PostgreSQL/
Temporal/NATS upgrade strategy, replica-safe rate limits, sizing/load evidence,
multi-arch build proof, and dependency/container scanning.

### 7. Secret audit and chaos

Owns the cross-sink threat model, canary harness, release security evidence,
isolated failpoints, worker/dependency recovery matrix, scheduled CI, restored
environment exit test, and final Phase 10 checklist evidence.

## Sequencing and migration expectations

The implementation order is fixed:

1. Deterministic tool-worker establishes the real service/queue, activity
   ownership, durable manifest, and runner boundary used by every later
   operations check.
2. Telemetry core establishes context, safe errors, and metrics used by later
   evidence.
3. Protected health adds additive heartbeat schema and the admin operations
   surface.
4. DLQ/retry adds durable operational command schema and controls.
5. Master-key rotation adds keyring compatibility before any key is changed.
6. Runbooks/hardening validates the complete deployment and generates sizing,
   backup, restore, and upgrade evidence.
7. Secret-audit/chaos tests the integrated system and closes the phase.

Each schema-bearing sub-project uses a separate additive Alembic revision,
extends the one-head graph test, upgrades from the immediately previous head,
and performs real-PostgreSQL downgrade/re-upgrade tests. No Phase 10 migration
removes or rewrites existing product columns. Previous application images may
ignore new tables/fields, enabling application rollback without schema
downgrade. Key retirement and destructive infrastructure cutover occur only
after their explicit backup/recovery gates.

Each sub-project follows contract/types → failing unit/integration test →
smallest implementation → focused suite → affected suite → lint/typecheck →
Compose acceptance. Public UI controls appear only in the same sub-project as
their durable backend and authorization.

## Acceptance evidence

Phase 10 is complete only when repository documentation records commands,
versions, dates, and actual results for all of the following:

### Telemetry and logs

- A webhook-to-task-to-agent-worker manifest handoff-to-tool-worker-to-
  connector/sandbox trace is connected end to end and includes the required
  correlation fields without prompt, argument, or credential payloads.
- Prometheus receives every required metric with the exact cardinality policy;
  retries do not double-count committed cost/effects.
- Every application service emits parseable schema-versioned JSON stdout.
- Monitoring profile absence and monitoring backend failure do not stop product
  work.

### Health, DLQ, and retry

- Anonymous health is opaque; workspace administrators receive only their
  sanitized detailed health; all cross-workspace attempts fail.
- Agent-, tool-, and event-worker kill/restart changes protected health within
  30 seconds and recovers; the tool task queue independently loses and regains
  a poller.
- Poison delivery leaves `quarantine_only` after an injected quarantine-commit
  failure, never invokes the handler beyond the configured attempt count, then
  atomically commits one DB failure, one outbox intent, and `completed` on
  recovery. Redelivery after that commit creates no duplicate, and the
  retention test deletes the completed processing-state row after its
  documented cutoff while retaining `handling` and `quarantine_only` controls.
- Admin replay is durable/idempotent, expires safely with NATS retention, and
  cannot duplicate durable triggered work.
- Eligible task retry starts one new attempt; duplicate HTTP requests do not;
  execution-unknown and non-idempotent prior effects have no retry button.

### Key, backup, restore, and upgrades

- A staged master-key rotation completes during active ordinary work, survives
  API/agent-worker/tool-worker restart, verifies all rows, and proves old-key
  retirement only after zero rows use it and every fresh key-bearing instance
  reports the target keyring.
- Backup archives contain the required database/NATS/config manifest, keep the
  key separately encrypted, and pass checksums.
- A fresh isolated restore decrypts stored credentials, preserves Temporal
  history and NATS pending work, and completes a live task once.
- Fresh database migration, previous Phase 9 schema upgrade, downgrade/re-upgrade,
  and previous-image application rollback pass.
- PostgreSQL logical major-version rehearsal and pinned Temporal/NATS upgrade
  rehearsals pass on restored state.

### Deployment and security

- Rendered production Compose exposes exactly one reverse-proxy entrypoint and
  no internal infrastructure port.
- Rendered production Compose contains a healthy real tool-worker on only the
  dedicated tool queue and permitted networks; agent-worker cannot reach the
  runner, and sandbox-runner is non-root with only validated rootless or exact-
  GID socket access and no privileged/root fallback.
- Ordinary tool calls, approval resolution, trigger sync-back, and sandbox
  cleanup all cross the tool queue; agent-worker cannot import executable
  connectors, and tool-worker cannot import model-provider or agent-runtime
  packages.
- Restored Phase 9 Temporal histories for a normal tool step, parked approval,
  trigger sync, and finalization replay and finish under the Phase 10 binaries
  without executing a connector or contacting sandbox-runner in agent-worker.
- Caddy and Traefik/Nginx-compatible guidance produce valid TLS, secure cookies,
  proxy trust, CSP, HSTS, and required headers.
- Development passwords/tokens, insecure production URLs/cookies, dev
  allowlists, fake services, and chaos flags fail production validation.
- Replica-safe login, webhook, manual-task, model, tool, and sandbox rate-limit
  boundary tests return the documented `Retry-After` and do not create work
  after denial.
- All five sizing profiles—developer, small, small with bundled monitoring,
  medium, and medium with bundled monitoring—pass their documented 30-minute
  measured load gates.
- Secret canaries are absent from every audited sink.
- Dependency/container scans, sandbox risk review, workspace isolation,
  webhook signature, approval, rate-limit, and existing security invariant
  tests pass.
- The full production Compose stack restarts cleanly with every required and
  enabled optional-profile service healthy.
- A clean supported machine follows the README from clone through owner
  onboarding without source edits.
- A release-gated manual test using real GitHub and Linear completes the signed
  Linear-event to isolated repository work to pull-request path. Normal CI uses
  the repository's fake providers and never depends on third-party uptime.

### Final recovery exit test

In a fresh Compose environment restored or migrated from the supported previous
state, start a real live task, SIGKILL agent-worker during an activity, confirm
the UI accurately reports running/retrying state, restart it, SIGKILL
tool-worker at a deterministic tool failpoint, restart it, SIGKILL event-worker
after delivery-before-ack, restart it, and verify:

- the workflow recovers to a correct terminal state;
- the UI matches PostgreSQL and Temporal throughout;
- one durable task/run outcome exists;
- each externally visible effect occurs at most once or is durably marked
  execution-unknown without automatic repetition;
- NATS lag returns to zero and no duplicate task/workflow appears;
- protected health returns to `ok`;
- logs, traces, metrics, audit, DLQ, and UI contain no secret canary.

Only after this evidence exists may the fourteen Phase 10 checkboxes and the
production-readiness items in implementation-plan section 49 be marked
complete.
