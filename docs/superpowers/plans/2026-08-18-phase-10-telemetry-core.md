# Phase 10 Telemetry Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one safe, schema-versioned observability bootstrap to every Jhin application service, connect traces and bounded-cardinality metrics across API, NATS, Temporal, agent, tool, connector, database, and sandbox boundaries, and ship an optional reproducible local monitoring profile without making product work depend on it.

**Architecture:** `jhin_observability` remains a dependency-light shared distribution and becomes the only Python entry point for JSON logging, W3C trace-context handling, OTel provider/exporter lifecycle, Temporal interceptors, and metric construction. Product packages emit only allowlisted attributes and closed-enum metric labels at durable transition boundaries; an absent OTLP endpoint installs no-op providers, and a saturated or failed exporter drops diagnostic data without delaying authoritative work. The optional Compose `observability` profile runs an internal Collector → Prometheus/Tempo → Grafana plane with repository-provisioned configuration and dashboards; the Next.js server emits the same log schema but does not export OTel logs.

**Tech Stack:** Python 3.13, structlog 26, OpenTelemetry Python API/SDK/OTLP, Temporal Python SDK 1.31 tracing and worker interceptors, FastAPI/ASGI, SQLAlchemy 2 async, NATS JetStream, httpx, Next.js 16.3.1, TypeScript, Vitest, Docker Compose, OpenTelemetry Collector Contrib 0.135.0, Prometheus 3.5.0, Tempo 2.8.2, Grafana 12.1.0, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-phase-10-production-operations-design.md`, especially “Release principles,” “Monitoring plane,” “Telemetry core,” “Secret and logging audit,” “Sequencing and migration expectations,” and “Acceptance evidence / Telemetry and logs.”

## Global Constraints

- This is Phase 10 sub-project 2 and has a hard execution predecessor: complete `docs/superpowers/plans/2026-08-18-phase-10-tool-worker-boundary.md` (including its acceptance task) before Task 0 or any telemetry implementation. The `jhin_observability` API is architecturally reusable by sub-project 1, but this plan must not create or edit tool-worker paths before that predecessor has established `TOOL_TASK_QUEUE = "jhin-tool-queue"`, `services/tool_worker/src/jhin_tool_worker/{__init__,settings,resources,activities,main}.py`, agent activities `reason_agent_step`, `commit_agent_step`, `commit_approval_projection`, and tool activities `resolve_advertised_tools`, `execute_bound_tool`, `resolve_bound_tool_approval`, `sync_external_tool`, and `cleanup_run_workspace`. Task 0 proves the predecessor tree exists; Tasks 1, 6, 8, and 10 then deliberately true up its dependency-boundary, registration, and Compose assertions for observability.
- PostgreSQL remains the product source of truth, Temporal remains the durable workflow authority, and NATS JetStream remains the event transport. Telemetry is diagnostic and must never change task, event, approval, retry, audit, budget, concurrency, or authorization behavior.
- `jhin_observability.initialize_observability(config)` is the sole Python service bootstrap for logging, tracing, and metrics and is called before constructing a database engine, NATS/Temporal/httpx client, connector registry, sandbox manager, or worker.
- No configured OTLP endpoint means OTel no-op trace and metric providers while schema-versioned JSON stdout remains active. Collector, Prometheus, Tempo, Grafana, DNS, TLS, queue saturation, or export failure must not prevent startup or block product work.
- All export work is off the product coroutine path, uses finite memory, has bounded connect/export timeouts, and counts/logs drops locally. No application log is exported through OTel in Phase 10.
- Every application log is one JSON object per stdout line with `schema_version: 1`, ISO-8601 UTC `timestamp`, `level`, `service`, `environment`, stable dotted `event`, and `logger`; optional context is limited to `trace_id`, `span_id`, `request_id`, `correlation_id`, `workspace_id`, `task_id`, and `run_id` plus bounded operation-specific fields and a redacted structured `error` object.
- Structural redaction removes credential-bearing keys including `authorization`, `cookie`, `password`, `secret`, `token`, `api_key`, `private_key`, and `dsn`; URL userinfo, query, and fragment are stripped. Every Python service installs the value-based `jhin_secrets.redaction.redact_event_dict` processor, even when its process-local registry is initially empty, and every decrypted secret continues to register with that redactor. The web logger installs the equivalent server-only known-value registry. Sanitization occurs before persistence/export and again before JSON rendering.
- Raw request/response bodies, prompts, completions, SQL, bind values, model/tool inputs or outputs, webhook bodies, sandbox secret environment, complete command environment, Authorization/cookie values, provider error bodies, private/API/master keys, DSNs, hostnames, URL query strings, and tracebacks containing unredacted values are forbidden from logs, spans, metrics, evidence, and dashboards.
- API accepts only valid W3C `traceparent`/`tracestate`; arbitrary inbound baggage is discarded. It always creates a Jhin request ID, returns it in `X-Request-ID`, and binds it for the complete request lifetime.
- The only canonical context fields are OTel `trace_id`/`span_id`, Jhin `request_id`/`correlation_id`, and nullable `task_id`/`run_id`. These IDs may appear in safe trace attributes and logs and must never be metric labels.
- NATS injects/extracts `traceparent` and `tracestate`; the envelope `correlation_id` remains the business-chain authority. Temporal propagation uses SDK interceptors and trace-only carriers, and application spans are emitted at client/activity boundaries, never from replay-sensitive workflow code.
- Model/connector/sandbox spans use normalized provider/connector/operation/outcome/latency/retry metadata only. SQL spans contain a normalized operation and allowlisted table name only—never statement text, parameters, DSN, database/user/host names, or results.
- The exact allowed metric label keys are `service, environment, outcome, failure_class, provider_type, connector_type, tool_family, risk, network_policy, stream, consumer, task_queue, activity, http_method, http_route, http_status_class, direction`. Each instrument further restricts its own subset; unknown values normalize to `other`, and every other key raises `MetricLabelError` before reaching OTel.
- Metric labels never contain workspace, user, agent, team, task, run, event, message, connection, approval, tool-call, sandbox-job, request, correlation, trace, URL, hostname, repository, project, model-name, or external-resource identifiers.
- Required metrics attach to committed transitions or deterministic records, not activity entry. Replayed Temporal activities do not double-count terminal run, committed token/cost, tool outcome, trigger invocation, or sandbox terminal metrics. `model_requests_total` is explicitly attempt-level.
- All required instruments and exact names in the design are implemented: `agent_runs_total`, `agent_run_duration_seconds`, `agent_run_failures_total`, `model_requests_total`, `model_tokens_total`, `model_cost_estimate`, `tool_calls_total`, `tool_call_failures_total`, `trigger_invocations_total`, `trigger_failures_total`, `sandbox_jobs_total`, `sandbox_job_duration_seconds`, `nats_consumer_lag`, `temporal_activity_failures`, `connector_health`, and the explanatory `connector_connections` gauge.
- Tempo retention is exactly `72h`; Prometheus retention is exactly `15d`; Docker JSON-file logging defaults to `max-size: 20m` and `max-file: 5` for every application service.
- The monitoring plane uses only an internal `monitoring` network. Collector/Prometheus/Tempo/Grafana receive no PostgreSQL, NATS, Temporal, Docker-socket, master-key, model-provider, connector, or sandbox-runner credentials. Grafana has no production host binding; the dev overlay may bind only `127.0.0.1`.
- Prometheus/Tempo/Grafana volumes are replaceable diagnostics and are excluded from required product backup. Grafana data sources and dashboards are provisioned from version-controlled repository files.
- Product UI work is out of scope. This plan provides only `ObservabilityRuntime.status()` for the later protected-health sub-project and does not add public or protected operations endpoints.
- Task 0 modifies and stages the two already-tracked plan files together so the downstream plan cannot overwrite the interceptor-aware API Temporal provider or telemetry-owned integration harness. No later telemetry task edits or stages either plan.
- The protected external user-owned plan path is outside this plan. No task or command may open, read, hash, stat, search, edit, stage, rename, delete, commit, or otherwise target it. Status, diff, and staging checks must be explicitly scoped to plan-owned paths. Do not stage either Phase 10 design spec unless its owner separately requests that action.
- Every implementation task follows RED -> focused GREEN -> affected suite -> exact-diff review -> scoped commit. Never use `git add .`. Worktree status/diff queries are always scoped to the task's owned paths. The sole permitted unscoped repository-state query is `git diff --cached --name-only` (or `git diff --cached --quiet`): it reads only tracked index state and is required to fail closed on an unrelated staged path. No task may use an unscoped worktree-status query.

## Shared Interfaces

These names are fixed across every task. Do not invent service-local alternatives.

```python
from typing import Literal, get_args

# packages/observability/src/jhin_observability/registry.py is the one registry;
# context.py and temporal.py import these names and never redeclare them.
TEMPORAL_ACTIVITY_NAMES = (
    "reason_agent_step",
    "commit_agent_step",
    "commit_approval_projection",
    "resolve_advertised_tools",
    "execute_bound_tool",
    "resolve_bound_tool_approval",
    "sync_external_tool",
    "cleanup_run_workspace",
    "resolve_snapshot",
    "run_agent_step",
    "resolve_approval",
    "finalize_run",
    "finalize_run_projection",
    "summarize_delegation",
    "deliver_delegation_result",
    "prepare_triggered_task",
    "sync_external",
    "resolve_engineering_plan",
    "create_engineering_child_task",
    "finalize_engineering_ticket",
    "record_beat",
)
SpanName = Literal[
    "http.server.request",
    "db.operation",
    "nats.publish",
    "nats.consume",
    "trigger.dispatch",
    "temporal.start_workflow",
    "temporal.signal_workflow",
    "temporal.client.other",
    "temporal.activity.other",
    "temporal.activity.reason_agent_step",
    "temporal.activity.commit_agent_step",
    "temporal.activity.commit_approval_projection",
    "temporal.activity.resolve_advertised_tools",
    "temporal.activity.execute_bound_tool",
    "temporal.activity.resolve_bound_tool_approval",
    "temporal.activity.sync_external_tool",
    "temporal.activity.cleanup_run_workspace",
    "temporal.activity.resolve_snapshot",
    "temporal.activity.run_agent_step",
    "temporal.activity.resolve_approval",
    "temporal.activity.finalize_run",
    "temporal.activity.finalize_run_projection",
    "temporal.activity.summarize_delegation",
    "temporal.activity.deliver_delegation_result",
    "temporal.activity.prepare_triggered_task",
    "temporal.activity.sync_external",
    "temporal.activity.resolve_engineering_plan",
    "temporal.activity.create_engineering_child_task",
    "temporal.activity.finalize_engineering_ticket",
    "temporal.activity.record_beat",
    "model.request",
    "agent.reason_step",
    "tool.gateway.execute",
    "tool.approval.resolve",
    "connector.http",
    "connector.database",
    "sandbox.client",
    "sandbox.server",
    "sandbox.job.lifecycle",
]
SPAN_NAMES: frozenset[str] = frozenset(get_args(SpanName))
AttributeValue = str | bool | int | float
MetricName = Literal[
    "agent_runs_total",
    "agent_run_duration_seconds",
    "agent_run_failures_total",
    "model_requests_total",
    "model_tokens_total",
    "model_cost_estimate",
    "tool_calls_total",
    "tool_call_failures_total",
    "trigger_invocations_total",
    "trigger_failures_total",
    "sandbox_jobs_total",
    "sandbox_job_duration_seconds",
    "nats_consumer_lag",
    "temporal_activity_failures",
    "connector_health",
    "connector_connections",
]
```

The remainder of the public contract is fixed by these exact, fully qualified signatures. Tasks
1–3 provide the executable definitions and tests shown below; this table is deliberately not a
second set of stub implementations:

```text
jhin_observability.config.ObservabilitySettings.observability_config(*, service_name: str, service_version: str, extra_log_processors: tuple[Processor, ...] = ()) -> ObservabilityConfig
jhin_observability.config.service_version(distribution_name: str) -> str
jhin_observability.bootstrap.initialize_observability(config: ObservabilityConfig) -> ObservabilityRuntime
jhin_observability.bootstrap.get_runtime() -> ObservabilityRuntime
jhin_observability.context.noop_tracer() -> Tracer
jhin_observability.bootstrap.ObservabilityRuntime.status() -> TelemetryExporterStatus
jhin_observability.bootstrap.ObservabilityRuntime.shutdown(timeout_millis: int = 5_000) -> None
jhin_observability.context.bind_context(*, request_id: str | UUID | None = None, correlation_id: str | UUID | None = None, workspace_id: str | UUID | None = None, task_id: str | UUID | None = None, run_id: str | UUID | None = None) -> AbstractContextManager[None]
jhin_observability.context.inject_trace_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]
jhin_observability.context.extract_trace_context(headers: Mapping[str, str]) -> Context
jhin_observability.context.normalize_span_attributes(attributes: Mapping[str, AttributeValue] | None) -> dict[str, AttributeValue]
jhin_observability.context.safe_span(name: SpanName, *, tracer: Tracer | None = None, kind: SpanKind = SpanKind.INTERNAL, attributes: Mapping[str, str | bool | int | float] | None = None, context: Context | None = None) -> AbstractContextManager[Span]
jhin_observability.context.record_span_error(span: Span, error: SafeError) -> None
jhin_observability.metrics.JhinMetrics.counter(name: MetricName) -> BoundCounter
jhin_observability.metrics.JhinMetrics.histogram(name: MetricName) -> BoundHistogram
jhin_observability.metrics.JhinMetrics.set_observable(name: MetricName, observations: Sequence[Observation]) -> None
jhin_observability.metrics.noop_metrics() -> JhinMetrics
jhin_observability.errors.safe_error(exc: BaseException, *, code: SafeErrorCode) -> SafeError
```

`SafeErrorCode` is the closed enum `internal_error | invalid_request |
authentication_failed | authorization_failed | rate_limited | upstream_unavailable | timeout |
conflict | execution_unknown`. `SafeError` contains exactly `type: str` and `code: SafeErrorCode`;
`safe_error` derives only the bounded exception class name and caller-selected closed code, never
exception text.

## File Map

This is the exact implementation ownership map. Each task's `Files` block and
`taskN_paths` array below mirror the corresponding list byte-for-byte.

### Task 1 owned paths (51)

- `apps/api/src/jhin_api/main.py`
- `apps/api/src/jhin_api/webhooks/service.py`
- `compose.rootless.yaml`
- `compose.yaml`
- `packages/events/pyproject.toml`
- `packages/events/src/jhin_events/consumer.py`
- `packages/observability/src/jhin_observability/__init__.py`
- `packages/observability/src/jhin_observability/errors.py`
- `packages/observability/src/jhin_observability/events.py`
- `packages/observability/src/jhin_observability/logging.py`
- `packages/observability/src/jhin_observability/redaction.py`
- `packages/observability/tests/test_errors.py`
- `packages/observability/tests/test_log_audit.py`
- `packages/observability/tests/test_logging.py`
- `packages/secrets/pyproject.toml`
- `packages/secrets/src/jhin_secrets/crypto.py`
- `packages/secrets/tests/test_crypto.py`
- `packages/workflows/src/jhin_workflows/heartbeat/activities.py`
- `scripts/assert_phase10_tool_worker_compose.py`
- `scripts/audit_phase10_logging.py`
- `services/agent_worker/src/jhin_agent_worker/activities.py`
- `services/agent_worker/src/jhin_agent_worker/engineering_activities.py`
- `services/agent_worker/src/jhin_agent_worker/main.py`
- `services/agent_worker/src/jhin_agent_worker/projections.py`
- `services/agent_worker/src/jhin_agent_worker/reasoning.py`
- `services/agent_worker/src/jhin_agent_worker/resources.py`
- `services/agent_worker/src/jhin_agent_worker/settings.py`
- `services/agent_worker/src/jhin_agent_worker/trigger_activities.py`
- `services/event_worker/src/jhin_event_worker/main.py`
- `services/event_worker/src/jhin_event_worker/matcher.py`
- `services/event_worker/src/jhin_event_worker/normalizer.py`
- `services/event_worker/src/jhin_event_worker/processor.py`
- `services/event_worker/src/jhin_event_worker/settings.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- `services/sandbox_runner/tests/test_rootless_transport.py`
- `services/tool_worker/pyproject.toml`
- `services/tool_worker/src/jhin_tool_worker/activities.py`
- `services/tool_worker/src/jhin_tool_worker/main.py`
- `services/tool_worker/src/jhin_tool_worker/resources.py`
- `services/tool_worker/src/jhin_tool_worker/settings.py`
- `services/tool_worker/src/jhin_tool_worker/trigger_activities.py`
- `services/tool_worker/tests/test_advertised_tools.py`
- `services/tool_worker/tests/test_worker_registration.py`
- `services/workflow_worker/src/jhin_workflow_worker/main.py`
- `services/workflow_worker/src/jhin_workflow_worker/settings.py`
- `tests/test_phase10_tool_worker_compose.py`
- `tests/test_worker_dependency_boundaries.py`
- `uv.lock`

### Task 2 owned paths (15)

- `packages/observability/pyproject.toml`
- `packages/observability/src/jhin_observability/__init__.py`
- `packages/observability/src/jhin_observability/bootstrap.py`
- `packages/observability/src/jhin_observability/config.py`
- `packages/observability/src/jhin_observability/context.py`
- `packages/observability/src/jhin_observability/exporters.py`
- `packages/observability/src/jhin_observability/metrics.py`
- `packages/observability/src/jhin_observability/registry.py`
- `packages/observability/tests/conftest.py`
- `packages/observability/tests/test_bootstrap.py`
- `packages/observability/tests/test_context.py`
- `packages/observability/tests/test_exporters.py`
- `packages/observability/tests/test_noop_metrics.py`
- `pyproject.toml`
- `uv.lock`

### Task 3 owned paths (5)

- `packages/observability/src/jhin_observability/__init__.py`
- `packages/observability/src/jhin_observability/bootstrap.py`
- `packages/observability/src/jhin_observability/metrics.py`
- `packages/observability/tests/test_bootstrap.py`
- `packages/observability/tests/test_metrics.py`

### Task 4 owned paths (15)

- `apps/api/pyproject.toml`
- `apps/api/src/jhin_api/main.py`
- `apps/api/src/jhin_api/seed.py`
- `apps/api/src/jhin_api/settings.py`
- `apps/api/tests/test_health.py`
- `apps/api/tests/test_observability.py`
- `packages/db/pyproject.toml`
- `packages/db/src/jhin_db/engine.py`
- `packages/db/tests/test_observability.py`
- `packages/observability/pyproject.toml`
- `packages/observability/src/jhin_observability/__init__.py`
- `packages/observability/src/jhin_observability/sqlalchemy.py`
- `packages/observability/tests/test_sqlalchemy.py`
- `tests/integration/test_phase2_api.py`
- `uv.lock`

### Task 5 owned paths (15)

- `apps/api/src/jhin_api/webhooks/router.py`
- `apps/api/src/jhin_api/webhooks/service.py`
- `apps/api/tests/test_webhooks_unit.py`
- `packages/events/pyproject.toml`
- `packages/events/src/jhin_events/consumer.py`
- `packages/events/src/jhin_events/publisher.py`
- `packages/events/src/jhin_events/telemetry.py`
- `packages/events/tests/test_telemetry.py`
- `services/event_worker/pyproject.toml`
- `services/event_worker/src/jhin_event_worker/main.py`
- `services/event_worker/src/jhin_event_worker/normalizer.py`
- `services/event_worker/src/jhin_event_worker/processor.py`
- `services/event_worker/src/jhin_event_worker/settings.py`
- `services/event_worker/tests/test_telemetry.py`
- `uv.lock`

### Task 6 owned paths (39)

- `apps/api/src/jhin_api/deps.py`
- `apps/api/src/jhin_api/health/router.py`
- `apps/api/src/jhin_api/health/service.py`
- `apps/api/src/jhin_api/main.py`
- `apps/api/src/jhin_api/temporal.py`
- `apps/api/tests/test_health.py`
- `apps/api/tests/test_temporal_provider.py`
- `packages/observability/pyproject.toml`
- `packages/observability/src/jhin_observability/__init__.py`
- `packages/observability/src/jhin_observability/logging.py`
- `packages/observability/src/jhin_observability/temporal.py`
- `packages/observability/tests/test_log_audit.py`
- `packages/observability/tests/test_logging.py`
- `packages/observability/tests/test_temporal.py`
- `packages/workflows/pyproject.toml`
- `packages/workflows/src/jhin_workflows/poller_health.py`
- `packages/workflows/tests/test_phase10_history_replay.py`
- `packages/workflows/tests/test_poller_health.py`
- `pyproject.toml`
- `services/agent_worker/src/jhin_agent_worker/main.py`
- `services/agent_worker/src/jhin_agent_worker/resources.py`
- `services/agent_worker/src/jhin_agent_worker/settings.py`
- `services/event_worker/src/jhin_event_worker/main.py`
- `services/event_worker/tests/test_telemetry.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- `services/sandbox_runner/tests/test_api_auth.py`
- `services/sandbox_runner/tests/test_telemetry.py`
- `services/tool_worker/src/jhin_tool_worker/main.py`
- `services/tool_worker/src/jhin_tool_worker/resources.py`
- `services/tool_worker/src/jhin_tool_worker/settings.py`
- `services/tool_worker/tests/test_advertised_tools.py`
- `services/tool_worker/tests/test_worker_registration.py`
- `services/workflow_worker/pyproject.toml`
- `services/workflow_worker/src/jhin_workflow_worker/main.py`
- `services/workflow_worker/src/jhin_workflow_worker/settings.py`
- `services/workflow_worker/tests/test_telemetry.py`
- `tests/test_worker_dependency_boundaries.py`
- `uv.lock`

### Task 7 owned paths (32)

- `apps/api/src/jhin_api/deps.py`
- `apps/api/src/jhin_api/models/router.py`
- `apps/api/src/jhin_api/models/service.py`
- `apps/api/tests/test_model_telemetry.py`
- `apps/api/tests/test_webhooks_unit.py`
- `packages/models/pyproject.toml`
- `packages/models/src/jhin_models/factory.py`
- `packages/models/src/jhin_models/telemetry.py`
- `packages/models/tests/test_factory.py`
- `packages/models/tests/test_telemetry.py`
- `packages/tools/pyproject.toml`
- `packages/tools/src/jhin_tools/telemetry.py`
- `packages/tools/tests/test_telemetry.py`
- `services/agent_worker/src/jhin_agent_worker/activities.py`
- `services/agent_worker/src/jhin_agent_worker/projections.py`
- `services/agent_worker/src/jhin_agent_worker/reasoning.py`
- `services/agent_worker/tests/test_delegation_activities.py`
- `services/agent_worker/tests/test_phase9_invocation_activity.py`
- `services/agent_worker/tests/test_reasoning_manifest.py`
- `services/agent_worker/tests/test_step_projection.py`
- `services/agent_worker/tests/test_telemetry.py`
- `services/agent_worker/tests/test_upgrade_crash_barriers.py`
- `services/event_worker/src/jhin_event_worker/main.py`
- `services/event_worker/src/jhin_event_worker/matcher.py`
- `services/event_worker/tests/test_matcher.py`
- `services/event_worker/tests/test_telemetry.py`
- `services/tool_worker/src/jhin_tool_worker/activities.py`
- `services/tool_worker/tests/test_advertised_tools.py`
- `services/tool_worker/tests/test_bound_approval.py`
- `services/tool_worker/tests/test_bound_tool_execution.py`
- `services/tool_worker/tests/test_telemetry.py`
- `uv.lock`

### Task 8 owned paths (44)

- `apps/api/src/jhin_api/connections/router.py`
- `apps/api/src/jhin_api/connections/service.py`
- `apps/api/tests/test_connections_unit.py`
- `apps/api/tests/test_connector_telemetry.py`
- `packages/connectors/pyproject.toml`
- `packages/connectors/src/jhin_connectors/base.py`
- `packages/connectors/src/jhin_connectors/cli/runner_client.py`
- `packages/connectors/src/jhin_connectors/cli/tools.py`
- `packages/connectors/src/jhin_connectors/github/auth.py`
- `packages/connectors/src/jhin_connectors/github/client.py`
- `packages/connectors/src/jhin_connectors/github/connector.py`
- `packages/connectors/src/jhin_connectors/github/tools.py`
- `packages/connectors/src/jhin_connectors/http_client.py`
- `packages/connectors/src/jhin_connectors/linear/client.py`
- `packages/connectors/src/jhin_connectors/linear/connector.py`
- `packages/connectors/src/jhin_connectors/linear/tools.py`
- `packages/connectors/src/jhin_connectors/supabase/connector.py`
- `packages/connectors/src/jhin_connectors/supabase/database_client.py`
- `packages/connectors/src/jhin_connectors/supabase/database_tools.py`
- `packages/connectors/src/jhin_connectors/supabase/management_client.py`
- `packages/connectors/src/jhin_connectors/supabase/management_tools.py`
- `packages/connectors/src/jhin_connectors/telemetry.py`
- `packages/connectors/src/jhin_connectors/vercel/client.py`
- `packages/connectors/src/jhin_connectors/vercel/connector.py`
- `packages/connectors/src/jhin_connectors/vercel/tools.py`
- `packages/connectors/tests/supabase/test_database_telemetry.py`
- `packages/connectors/tests/test_http_client.py`
- `packages/connectors/tests/test_telemetry.py`
- `packages/tools/pyproject.toml`
- `packages/tools/src/jhin_tools/builtin.py`
- `packages/tools/tests/test_telemetry.py`
- `services/sandbox_runner/pyproject.toml`
- `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/schemas.py`
- `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- `services/sandbox_runner/tests/test_job_config.py`
- `services/sandbox_runner/tests/test_telemetry.py`
- `services/tool_worker/src/jhin_tool_worker/activities.py`
- `services/tool_worker/src/jhin_tool_worker/cleanup_activities.py`
- `services/tool_worker/src/jhin_tool_worker/main.py`
- `services/tool_worker/src/jhin_tool_worker/trigger_activities.py`
- `services/tool_worker/tests/test_telemetry.py`
- `uv.lock`

### Task 9 owned paths (12)

- `apps/web/Dockerfile`
- `apps/web/instrumentation.ts`
- `apps/web/lib/server-log-contract.json`
- `apps/web/lib/server-logger.ts`
- `apps/web/next.config.ts`
- `apps/web/server-wrapper.cjs`
- `apps/web/tests/instrumentation.test.ts`
- `apps/web/tests/server-logger.test.ts`
- `apps/web/tests/server-only-stub.ts`
- `apps/web/tests/server-wrapper.test.ts`
- `apps/web/vitest.config.ts`
- `tests/test_web_json_stdout.py`

### Task 10 owned paths (18)

- `.env.example`
- `Makefile`
- `compose.dev.yaml`
- `compose.rootless.yaml`
- `compose.yaml`
- `docker/monitoring.Dockerfile`
- `ops/observability/collector.yaml`
- `ops/observability/grafana/dashboards/jhin-overview.json`
- `ops/observability/grafana/provisioning/dashboards/jhin.yaml`
- `ops/observability/grafana/provisioning/datasources/jhin.yaml`
- `ops/observability/prometheus.yaml`
- `ops/observability/tempo.yaml`
- `scripts/assert_phase10_observability_compose.py`
- `scripts/assert_phase10_tool_worker_compose.py`
- `scripts/build_phase10_dashboard.py`
- `tests/integration/phase10_upgrade_harness.py`
- `tests/test_phase10_observability_compose.py`
- `tests/test_phase10_tool_worker_compose.py`

### Task 11 owned paths (14)

- `.github/workflows/ci.yml`
- `Makefile`
- `docs/operations/telemetry.md`
- `packages/connectors/src/jhin_connectors/testing/fake_linear.py`
- `packages/connectors/tests/linear/test_fake_linear_admin.py`
- `packages/models/src/jhin_models/testing/fake_openai.py`
- `packages/models/tests/test_fake_openai.py`
- `scripts/phase10_artifact.py`
- `tests/integration/conftest.py`
- `tests/integration/emit_phase10_metrics.py`
- `tests/integration/phase10_upgrade_harness.py`
- `tests/integration/test_phase10_telemetry.py`
- `tests/test_phase10_artifact.py`
- `tests/test_phase10_telemetry_harness.py`

### Task 12 owned paths (3)

- `docs/evidence/phase10-telemetry.md`
- `scripts/record_phase10_telemetry_evidence.py`
- `tests/test_phase10_telemetry_evidence.py`

### Task 0: Check In the Reviewed Telemetry Execution Baseline

**Files:**
- Modify: `docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md`
- Modify: `docs/superpowers/plans/2026-08-18-phase-10-protected-health.md`

**Interfaces:**
- Consumes: tracked two-plan checkpoint `6ef08f41d678c478746071d1c996f8ca4ffa254a`, tool-worker-boundary base `8838905e405dba85807a0a87ffb3e73524c41860`, its exact 30-commit accepted tip `0439fb2c92075ee5cdd5adf9bc54d2805de6670e` with an exact 36-path union, and exact-head rootful/rootless PR acceptance evidence.
- Produces: one two-plan checkpoint commit against which every telemetry and protected-health handoff is reviewed.

- [ ] **Step 1: Validate the tracked checkpoint and exact predecessor range before any telemetry edit**

Run from the clean accepted predecessor checkout. The complete reviewed range, every per-commit
path set, and the 36-path union are part of the gate; a subject-only match is insufficient:

```bash
set -euo pipefail
tracked_checkpoint=6ef08f41d678c478746071d1c996f8ca4ffa254a
predecessor_base=8838905e405dba85807a0a87ffb3e73524c41860
accepted_tip=0439fb2c92075ee5cdd5adf9bc54d2805de6670e
test "$(git rev-parse HEAD)" = "$accepted_tip"
git merge-base --is-ancestor "$tracked_checkpoint" "$predecessor_base"
test "$(git rev-list --count "$predecessor_base".."$accepted_tip")" = 30 || exit 1

for path in \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md \
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md; do
  git ls-files --error-unmatch "$path"
  test "$(git log -1 --format=%H "$accepted_tip" -- "$path")" = "$tracked_checkpoint"
done


assert_commit_paths() {
  local commit="$1"
  shift
  local actual expected
  actual="$(git diff-tree --no-commit-id --name-only -r "$commit" | LC_ALL=C sort)" || return 1
  expected="$(printf '%s
' "$@" | LC_ALL=C sort)" || return 1
  test "$actual" = "$expected" || return 1
}

assert_commit_paths bf6ea4b60a8a62a50083620164a2379254bd7e9f \
  .github/workflows/ci.yml Makefile tests/integration/compose.phase10-upgrade.yaml \
  tests/integration/conftest.py tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_live_upgrade.py \
  tests/integration/test_phase10_sandbox_socket_modes.py \
  tests/integration/test_phase10_tool_worker_boundary.py \
  tests/integration/test_phase6_exit.py tests/test_phase10_tool_worker_compose.py \
  tests/test_phase9_production_compose.py
assert_commit_paths 4b09d720c993883e896a83dba2a4c1ceec52bfd9 \
  packages/tools/tests/test_crash_barriers.py pyproject.toml \
  scripts/capture_phase9_temporal_histories.py \
  services/agent_worker/src/jhin_agent_worker/projections.py \
  services/agent_worker/src/jhin_agent_worker/reasoning.py \
  services/agent_worker/tests/test_legacy_manifest_sidecar.py \
  services/agent_worker/tests/test_reasoning_manifest.py \
  services/tool_worker/src/jhin_tool_worker/activities.py \
  services/tool_worker/tests/test_bound_approval.py \
  services/tool_worker/tests/test_bound_tool_execution.py \
  tests/test_capture_phase9_temporal_histories.py
assert_commit_paths 660518ae26bcb1afa4e4ef553fd3cf42007bc6bf \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_live_upgrade.py \
  tests/integration/test_phase10_tool_worker_boundary.py
for commit in \
  41d7210c19a34fa8e92640cb1c48f923c254538d \
  786a3e26430ccaeac72eecf8f40293460e46d9a9 \
  696fd3e27f62395732148f46ed47f0b31d5e2751 \
  2b636cdd4ae08b7452a2e04fe03d21f32d9bd067 \
  ac46404c710f6da2b9fae9a4901df128780d3441 \
  ed866b8c6962a6b523ec716666ef857e39fea514 \
  80babf31f8d0b870b994ff1a81031ab738102830 \
  4c156215138f9301a7a65933c71b6102d3029ae9; do
  assert_commit_paths "$commit" tests/integration/phase10_upgrade_harness.py \
    tests/integration/test_phase10_tool_worker_boundary.py
done
assert_commit_paths 5f3644ab1f0bf7e9b24ee51d796696febaff378d \
  .github/workflows/ci.yml apps/web/Dockerfile \
  packages/connectors/tests/vercel/test_webhook.py \
  tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths 579c72e8c78ada0ab830ad205d590de286d73275 \
  .github/workflows/ci.yml tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths 0dc2827bd8527e649cecfa3bf6a13229cd7d5f16 \
  .github/workflows/ci.yml tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths 9d7fb7a5c2f5b5c96ab8f1289ef7b65790b3bbd7 \
  .github/workflows/ci.yml packages/tools/src/jhin_tools/test_barriers.py \
  packages/tools/tests/test_crash_barriers.py tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py
for commit in \
  5f2b8c97c787710871d458313a1948db12f2c5b1 \
  05f00ca5eee3af43673384386e21519f514a2644 \
  d5781aefe14d1feaa50bf9a1368178aa233db36b; do
  assert_commit_paths "$commit" .github/workflows/ci.yml \
    tests/integration/test_phase10_tool_worker_boundary.py
done
assert_commit_paths 74ae79a9c1b8cec2c28c9789f5966633043ff832 \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths bcf2252c3f210fa10967f26de9e08960cff3c7f9 \
  docker/sandbox.Dockerfile tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths f2a22853f630dfde4912b7f0af26945c08c32e41 \
  compose.dev.yaml compose.yaml tests/test_compose_supabase_db_fixture.py
assert_commit_paths 3fb42b63290d761f21aa5dbfb7b8c2e4eefa9090 \
  services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py \
  services/sandbox_runner/tests/test_rootless_transport.py \
  tests/integration/test_phase10_sandbox_socket_modes.py


git grep -q '^test-tool-worker-boundary-integration:' "$accepted_tip" -- Makefile
git grep -q '^test-tool-worker-live-upgrade:' "$accepted_tip" -- Makefile
git grep -q '^test-sandbox-socket-rootful:' "$accepted_tip" -- Makefile
git grep -q '^test-sandbox-socket-rootless:' "$accepted_tip" -- Makefile
git grep -q '^test-sandbox-socket-wrong-gid:' "$accepted_tip" -- Makefile
git grep -q 'phase10-rootful-live:' "$accepted_tip" -- .github/workflows/ci.yml
git grep -q 'phase10-rootless-live:' "$accepted_tip" -- .github/workflows/ci.yml
```

##### Step 1 continued: Verify the exact ordered candidate ledger

```bash
actual_commits="$(git rev-list --reverse "$predecessor_base".."$accepted_tip")" || exit 1
expected_commits="$(printf '%s\n' \
  bf6ea4b60a8a62a50083620164a2379254bd7e9f \
  4b09d720c993883e896a83dba2a4c1ceec52bfd9 \
  660518ae26bcb1afa4e4ef553fd3cf42007bc6bf \
  41d7210c19a34fa8e92640cb1c48f923c254538d \
  786a3e26430ccaeac72eecf8f40293460e46d9a9 \
  696fd3e27f62395732148f46ed47f0b31d5e2751 \
  2b636cdd4ae08b7452a2e04fe03d21f32d9bd067 \
  ac46404c710f6da2b9fae9a4901df128780d3441 \
  ed866b8c6962a6b523ec716666ef857e39fea514 \
  80babf31f8d0b870b994ff1a81031ab738102830 \
  4c156215138f9301a7a65933c71b6102d3029ae9 \
  5f3644ab1f0bf7e9b24ee51d796696febaff378d \
  579c72e8c78ada0ab830ad205d590de286d73275 \
  0dc2827bd8527e649cecfa3bf6a13229cd7d5f16 \
  9d7fb7a5c2f5b5c96ab8f1289ef7b65790b3bbd7 \
  5f2b8c97c787710871d458313a1948db12f2c5b1 \
  05f00ca5eee3af43673384386e21519f514a2644 \
  d5781aefe14d1feaa50bf9a1368178aa233db36b \
  74ae79a9c1b8cec2c28c9789f5966633043ff832 \
  bcf2252c3f210fa10967f26de9e08960cff3c7f9 \
  f2a22853f630dfde4912b7f0af26945c08c32e41 \
  3fb42b63290d761f21aa5dbfb7b8c2e4eefa9090 \
  3deb7da456ebbcdd904d1e873270097edc0a7ed4 \
  41bc0f44033785f77c20d0fa5ddcaed1792dfab9 \
  ee66c588014acf8e448352a7e5e458aca63d37fe \
  639cf43d1d1189971b26c6d9809f0ed89c52eabd \
  7d8f6b14466047404b3face0c98211310995dc47 \
  bee85b90e69dbbd79ce0576d25f0e95efac2b09f \
  a430ccd8d6054f32f7e959abde85aa1f78c4a6a8 \
  0439fb2c92075ee5cdd5adf9bc54d2805de6670e)" || exit 1
test "$actual_commits" = "$expected_commits" || exit 1

actual_paths="$(git diff --name-only "$predecessor_base".."$accepted_tip" | LC_ALL=C sort)" || exit 1
expected_paths="$(printf '%s\n' \
  .github/workflows/ci.yml \
  Makefile \
  apps/web/Dockerfile \
  compose.dev.yaml \
  compose.yaml \
  docker/sandbox.Dockerfile \
  packages/connectors/tests/vercel/test_webhook.py \
  packages/tools/src/jhin_tools/test_barriers.py \
  packages/tools/tests/test_crash_barriers.py \
  pyproject.toml \
  scripts/capture_phase9_temporal_histories.py \
  services/agent_worker/src/jhin_agent_worker/projections.py \
  services/agent_worker/src/jhin_agent_worker/reasoning.py \
  services/agent_worker/tests/test_legacy_manifest_sidecar.py \
  services/agent_worker/tests/test_reasoning_manifest.py \
  services/sandbox_runner/src/jhin_sandbox_runner/jobs.py \
  services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py \
  services/sandbox_runner/tests/test_job_config.py \
  services/sandbox_runner/tests/test_job_lifecycle.py \
  services/sandbox_runner/tests/test_rootless_transport.py \
  services/tool_worker/src/jhin_tool_worker/activities.py \
  services/tool_worker/tests/test_bound_approval.py \
  services/tool_worker/tests/test_bound_tool_execution.py \
  tests/integration/compose.phase10-upgrade.yaml \
  tests/integration/conftest.py \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_live_upgrade.py \
  tests/integration/test_phase10_sandbox_socket_modes.py \
  tests/integration/test_phase10_tool_worker_boundary.py \
  tests/integration/test_phase3_exit.py \
  tests/integration/test_phase6_exit.py \
  tests/integration/test_phase7_exit.py \
  tests/test_capture_phase9_temporal_histories.py \
  tests/test_compose_supabase_db_fixture.py \
  tests/test_phase10_tool_worker_compose.py \
  tests/test_phase9_production_compose.py)" || exit 1
actual_path_count="$(printf '%s\n' "$actual_paths" | \
  awk 'NF { count++ } END { print count + 0 }')" || exit 1
test "$actual_path_count" = 36 || exit 1
test "$actual_paths" = "$expected_paths" || exit 1

assert_commit_paths 3deb7da456ebbcdd904d1e873270097edc0a7ed4 \
  services/agent_worker/tests/test_reasoning_manifest.py \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths 41bc0f44033785f77c20d0fa5ddcaed1792dfab9 \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths ee66c588014acf8e448352a7e5e458aca63d37fe \
  services/sandbox_runner/src/jhin_sandbox_runner/jobs.py \
  services/sandbox_runner/tests/test_job_config.py \
  services/sandbox_runner/tests/test_job_lifecycle.py \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_sandbox_socket_modes.py \
  tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths 639cf43d1d1189971b26c6d9809f0ed89c52eabd \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py
assert_commit_paths 7d8f6b14466047404b3face0c98211310995dc47 \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py

assert_commit_paths bee85b90e69dbbd79ce0576d25f0e95efac2b09f \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_tool_worker_boundary.py \
  tests/integration/test_phase3_exit.py \
  tests/integration/test_phase7_exit.py
assert_commit_paths a430ccd8d6054f32f7e959abde85aa1f78c4a6a8 \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_live_upgrade.py \
  tests/integration/test_phase10_tool_worker_boundary.py
test "$(git show -s --format=%s a430ccd8d6054f32f7e959abde85aa1f78c4a6a8)" = \
  "fix: close Phase 10 live acceptance probes" || exit 1
assert_commit_paths 0439fb2c92075ee5cdd5adf9bc54d2805de6670e \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_live_upgrade.py \
  tests/integration/test_phase10_tool_worker_boundary.py
test "$(git show -s --format=%s 0439fb2c92075ee5cdd5adf9bc54d2805de6670e)" = \
  "fix: stabilize Phase 10 upgrade acceptance" || exit 1
```

Expected: PASS at the exact accepted tip, with the plans still tracked at `6ef08f41…`, exactly 30
ordered commits after `8838905…`, every per-commit path set exact, and exactly the literal 36-path
union above. Any mismatch stops before either plan is edited or staged.

- [ ] **Step 2: Bind the accepted PR run to the exact head and synthetic merge tree**

The accepted exact-head run, job, synthetic-merge, and shared-tree values are final:

```bash
set -euo pipefail
accepted_run_id=32404319465
rootful_job_id=96539679313
rootless_job_id=96539679660
pr_number=1
pr_base=9fae5f63888f7b43f1eb6a0b008dd079ae7cd85a
pr_head=0439fb2c92075ee5cdd5adf9bc54d2805de6670e
synthetic_merge=5e7373aa9413f4500fde1f0f87c520eb14ba62b3
shared_tree=7b50d34bfd0db0d30e4ab68589c55ef853acd40d

pr_json="$(gh api "repos/jhinhq/Jhin/pulls/$pr_number")"
test "$(jq -r .base.sha <<<"$pr_json")" = "$pr_base"
test "$(jq -r .head.sha <<<"$pr_json")" = "$pr_head"

run_json="$(gh api "repos/jhinhq/Jhin/actions/runs/$accepted_run_id")"
test "$(jq -r .event <<<"$run_json")" = "pull_request"
test "$(jq -r .conclusion <<<"$run_json")" = "success"
# The run API names the PR head; checkout logs prove the merge SHA.
test "$(jq -r .head_sha <<<"$run_json")" = "$pr_head"
test "$(jq -r '.pull_requests[0].number' <<<"$run_json")" = "$pr_number"

git fetch --no-tags origin "refs/pull/$pr_number/merge"
test "$(git rev-parse FETCH_HEAD)" = "$synthetic_merge"
merge_parents="$(git show -s --format=%P "$synthetic_merge")"
head_tree="$(git show -s --format=%T "$pr_head")"
merge_tree="$(git show -s --format=%T "$synthetic_merge")"
test "$pr_head" = "0439fb2c92075ee5cdd5adf9bc54d2805de6670e"
test "$merge_parents" = "$pr_base $pr_head"
test "$head_tree" = "$shared_tree"
test "$merge_tree" = "$shared_tree"
test "$head_tree" = "$merge_tree"

jobs_json="$(gh api "repos/jhinhq/Jhin/actions/runs/$accepted_run_id/jobs?per_page=100")"
rootful_conclusion="$(jq -r --arg id "$rootful_job_id" \
  '.jobs[] | select((.id | tostring) == $id and .name == "Phase 10 rootful live boundary") | .conclusion' \
  <<<"$jobs_json")"
rootless_conclusion="$(jq -r --arg id "$rootless_job_id" \
  '.jobs[] | select((.id | tostring) == $id and .name == "Phase 10 rootless live boundary") | .conclusion' \
  <<<"$jobs_json")"
test "$rootful_conclusion" = success
test "$rootless_conclusion" = success
test "$(jq -r --arg id "$rootful_job_id" \
  '[.jobs[] | select((.id | tostring) == $id) | .steps[] | select(.name == "Rootful wrong-GID failure" and .conclusion == "success")] | length' \
  <<<"$jobs_json")" = "1"
test "$(jq -r --arg id "$rootless_job_id" \
  '[.jobs[] | select((.id | tostring) == $id) | .steps[] | select(.name == "Rootful wrong-GID failure")] | length' \
  <<<"$jobs_json")" = "0"
```

##### Step 2 continued: Prove both jobs checked out the synthetic merge

```bash
checkout_log_dir="$(mktemp -d "${TMPDIR:-/tmp}/jhin-phase10-checkout.XXXXXX")"
cleanup_checkout_logs() {
  rm -f -- "$checkout_log_dir/rootful.log" "$checkout_log_dir/rootless.log"
  rmdir -- "$checkout_log_dir"
}
trap cleanup_checkout_logs EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

checkout_log_has_sha() {
  log_path="$1"
  expected_sha="$2"
  awk -v sha="$expected_sha" '
    index($0, "/usr/bin/git log -1 --format") && index($0, "%H") {
      remaining = 4
      next
    }
    remaining > 0 {
      if (index($0, sha)) {
        found = 1
        exit
      }
      remaining--
    }
    END { exit(found ? 0 : 1) }
  ' "$log_path"
}

gh run view "$accepted_run_id" --repo jhinhq/Jhin \
  --job "$rootful_job_id" --log >"$checkout_log_dir/rootful.log"
gh run view "$accepted_run_id" --repo jhinhq/Jhin \
  --job "$rootless_job_id" --log >"$checkout_log_dir/rootless.log"
checkout_log_has_sha "$checkout_log_dir/rootful.log" "$synthetic_merge"
checkout_log_has_sha "$checkout_log_dir/rootless.log" "$synthetic_merge"
rg -F -q -- '/pull/1/merge' "$checkout_log_dir/rootful.log" || exit 1
rg -F -q -- '/pull/1/merge' "$checkout_log_dir/rootless.log" || exit 1

trap - EXIT HUP INT TERM
cleanup_checkout_logs

```

Expected: the accepted PR run, PR head, PR base, synthetic merge, and shared tree are literal and
mutually consistent. Both distinct Linux live jobs succeeded at that tree. The wrong-GID proof is
one successful step inside the rootful job and is absent from the rootless job.

- [ ] **Step 3: Verify the two-plan handoff and stage exactly those two paths**

Run:

```bash
set -euo pipefail
plan_paths=(
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md
)
for path in "${plan_paths[@]}"; do
  git ls-files --error-unmatch "$path"
  test "$(git log -1 --format=%H "$accepted_tip" -- "$path")" = \
    "6ef08f41d678c478746071d1c996f8ca4ffa254a"
  test "$(git status --short --untracked-files=no -- "$path")" = " M $path"
  git diff --cached --quiet -- "$path"
done
rg -q 'TemporalClientProvider' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
rg -q 'temporal_client_interceptors\(self\._observability\)' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
rg -q 'TelemetryExporterStatus' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
rg -q 'tests/test_phase10_telemetry_harness\.py' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
cached_paths="$(git diff --cached --name-only)" || exit 1
test -z "$cached_paths" || exit 1
git add -- "${plan_paths[@]}"
expected_index="$(printf '%s\n' "${plan_paths[@]}" | LC_ALL=C sort)" || exit 1
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)" || exit 1
test "$actual_index" = "$expected_index" || exit 1
git diff --cached --check -- "${plan_paths[@]}" || exit 1
```

Expected: both plans are tracked descendants of checkpoint `6ef08f41…`, the provider,
interceptor/status, and shared-harness handoffs are present, and only the two explicitly scoped plan
paths are staged by this task. No command targets the user-owned production-plan path.

- [ ] **Step 4: Commit the two-plan checkpoint**

```bash
plan_paths=(
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md
)
git commit --only "${plan_paths[@]}" \
  -m "docs(observability): checkpoint Phase 10 telemetry execution"
test "$(git show -s --format=%s HEAD)" = \
  "docs(observability): checkpoint Phase 10 telemetry execution"
expected_commit_paths="$(printf '%s\n' "${plan_paths[@]}" | LC_ALL=C sort)" || exit 1
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)" || exit 1
test "$actual_commit_paths" = "$expected_commit_paths" || exit 1
cached_paths="$(git diff --cached --name-only)" || exit 1
test -z "$cached_paths" || exit 1
```

Expected: one documentation-only commit with the exact subject and the two plan paths only. This
remains one Task 0 commit, so the complete telemetry plan still contains 13 commits total.



### Task 1: Enforce the JSON-v1 Log and Safe-Error Boundary

**Files:**
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/src/jhin_api/webhooks/service.py`
- Modify: `compose.rootless.yaml`
- Modify: `compose.yaml`
- Modify: `packages/events/pyproject.toml`
- Modify: `packages/events/src/jhin_events/consumer.py`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/src/jhin_observability/errors.py`
- Modify: `packages/observability/src/jhin_observability/events.py`
- Modify: `packages/observability/src/jhin_observability/logging.py`
- Modify: `packages/observability/src/jhin_observability/redaction.py`
- Modify: `packages/observability/tests/test_errors.py`
- Modify: `packages/observability/tests/test_log_audit.py`
- Modify: `packages/observability/tests/test_logging.py`
- Modify: `packages/secrets/pyproject.toml`
- Modify: `packages/secrets/src/jhin_secrets/crypto.py`
- Modify: `packages/secrets/tests/test_crypto.py`
- Modify: `packages/workflows/src/jhin_workflows/heartbeat/activities.py`
- Modify: `scripts/assert_phase10_tool_worker_compose.py`
- Modify: `scripts/audit_phase10_logging.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/engineering_activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/projections.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/reasoning.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/trigger_activities.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/matcher.py`
- Modify: `services/event_worker/src/jhin_event_worker/normalizer.py`
- Modify: `services/event_worker/src/jhin_event_worker/processor.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- Modify: `services/sandbox_runner/tests/test_rootless_transport.py`
- Modify: `services/tool_worker/pyproject.toml`
- Modify: `services/tool_worker/src/jhin_tool_worker/activities.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/trigger_activities.py`
- Modify: `services/tool_worker/tests/test_advertised_tools.py`
- Modify: `services/tool_worker/tests/test_worker_registration.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/main.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/settings.py`
- Modify: `tests/test_phase10_tool_worker_compose.py`
- Modify: `tests/test_worker_dependency_boundaries.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes the accepted Task 0 handoff and produces the exact Task 1 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing schema, structural-redaction, and stdlib-capture tests**

Add these exact assertions (retain and adapt the current known-secret tests rather than deleting them):

```python
import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime

import pytest

from jhin_observability import (
    EVENT_FIELD_RULES,
    SafeErrorCode,
    configure_json_logging,
    filter_log_event,
    get_logger,
    safe_error,
    structural_redaction,
)
from jhin_secrets.redaction import get_redactor, redact_event_dict


class _SecretRepr:
    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


@pytest.fixture(autouse=True)
def clear_process_secret_registry() -> Iterator[None]:
    redactor = get_redactor()
    redactor.clear()
    try:
        yield
    finally:
        redactor.clear()


@pytest.mark.parametrize("logger_kind", ["structlog", "stdlib"])
def test_every_record_has_exact_v1_required_fields(
    capsys: pytest.CaptureFixture[str], logger_kind: str
) -> None:
    configure_json_logging(service="api", environment="test", level="INFO")
    if logger_kind == "structlog":
        get_logger("jhin.test").info("api.started", request_id="req-1")
    else:
        logging.getLogger("uvicorn.error").warning("server booted on private-host-canary")
    record = json.loads(capsys.readouterr().out)
    assert record["schema_version"] == 1
    assert record["service"] == "api"
    assert record["environment"] == "test"
    assert record["level"] in {"info", "warning"}
    assert record["event"] in {"api.started", "stdlib.message"}
    assert record["logger"] in {"jhin.test", "uvicorn.error"}
    assert datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).tzinfo
    assert "private-host-canary" not in json.dumps(record)


def test_structural_redaction_removes_nested_keys_and_url_parts() -> None:
    value = {
        "authorization": "Bearer exact-canary",
        "nested": {"api_key": "key-canary", "safe": "kept"},
        "target": "https://user:pass@example.test/path?token=query-canary#fragment-canary",
    }
    redacted = structural_redaction(value)
    rendered = json.dumps(redacted)
    assert "exact-canary" not in rendered
    assert "key-canary" not in rendered
    assert "user" not in rendered and "pass" not in rendered
    assert "query-canary" not in rendered and "fragment-canary" not in rendered
    assert redacted["nested"]["safe"] == "kept"
    assert redacted["target"] == "https://example.test/path"


def test_unknown_object_is_stringified_only_inside_redaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "unknown-object-canary"
    get_redactor().register(canary)
    configure_json_logging(
        service="tool-worker",
        environment="test",
        level="INFO",
        extra_processors=(redact_event_dict,),
    )
    get_logger(__name__).info("api.started", request_id=_SecretRepr(canary))
    rendered = capsys.readouterr().out
    assert json.loads(rendered)["event"] == "api.started"
    assert canary not in rendered


def test_exception_becomes_bounded_redacted_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    get_redactor().register("trace-canary")
    configure_json_logging(
        service="api",
        environment="test",
        level="INFO",
        extra_processors=(redact_event_dict,),
    )
    try:
        raise RuntimeError("request failed with password=trace-canary")
    except RuntimeError:
        get_logger(__name__).exception(
            "api.request_failed",
            error_code=SafeErrorCode.INTERNAL_ERROR.value,
        )
    record = json.loads(capsys.readouterr().out)
    assert record["error"]["type"] == "RuntimeError"
    assert record["error"]["code"] == "internal_error"
    assert len(record["error"]["traceback"]) <= 32
    assert "trace-canary" not in json.dumps(record)


def test_safe_error_never_contains_exception_text_or_arguments() -> None:
    error = safe_error(
        RuntimeError("provider response body canary", {"token": "nested-canary"}),
        code=SafeErrorCode.UPSTREAM_UNAVAILABLE,
    )
    assert asdict(error) == {
        "type": "RuntimeError",
        "code": SafeErrorCode.UPSTREAM_UNAVAILABLE,
    }


@pytest.mark.parametrize(
    "key",
    ["apiKey", "privateKey", "accessToken", "clientSecret", "Authorization", "set-cookie"],
)
def test_credential_key_normalization_redacts_camel_case_and_hyphenated_keys(key: str) -> None:
    assert structural_redaction({key: "credential-canary"}) == {key: "[REDACTED]"}


def test_event_filter_discards_unregistered_fields_and_foreign_text() -> None:
    filtered = filter_log_event(
        {
            "event": "worker.started",
            "task_queue": "jhin-agent-queue",
            "message": "foreign-free-text-canary",
            "detail": "foreign-detail-canary",
        }
    )
    assert filtered == {"event": "worker.started", "task_queue": "jhin-agent-queue"}


def test_unknown_event_is_replaced_without_preserving_original_text() -> None:
    filtered = filter_log_event({"event": "attacker supplied free text", "safe": "canary"})
    assert filtered == {"event": "log.event_rejected"}


@pytest.mark.parametrize("event", sorted(EVENT_FIELD_RULES))
def test_every_registered_event_rejects_an_unknown_canary_field(event: str) -> None:
    filtered = filter_log_event({"event": event, "unregistered": "runtime-canary"})
    assert "runtime-canary" not in json.dumps(filtered)


@pytest.mark.parametrize("event", sorted(EVENT_FIELD_RULES))
def test_every_registered_event_survives_the_runtime_renderer(
    event: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_json_logging(service="api", environment="test", level="INFO")
    get_logger("jhin.contract").info(event)
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == event
    assert (
        set(("schema_version", "timestamp", "level", "service", "environment", "logger")) <= record
    )


def test_job_id_is_allowed_only_on_sandbox_job_finished() -> None:
    valid = filter_log_event(
        {"event": "sandbox.job.finished", "job_id": "0123456789abcdef", "outcome": "completed"}
    )
    foreign = filter_log_event({"event": "worker.started", "job_id": "0123456789abcdef"})
    assert valid["job_id"] == "0123456789abcdef"
    assert "job_id" not in foreign
    assert "job_id" not in CONTEXT_FIELD_RULES


@pytest.mark.parametrize("accepted", ["export_timeout", "export_failed"])
def test_export_failure_codes_are_event_and_field_specific(accepted: str) -> None:
    assert (
        filter_log_event({"event": "telemetry.export_failed", "error_code": accepted})["error_code"]
        == accepted
    )


@pytest.mark.parametrize("rejected", ["internal_error", "timeout", "attacker-code"])
def test_export_failure_rejects_non_export_error_codes(rejected: str) -> None:
    assert "error_code" not in filter_log_event(
        {"event": "telemetry.export_failed", "error_code": rejected}
    )


def test_export_failure_accepts_no_structured_error_or_foreign_fields() -> None:
    assert filter_log_event(
        {
            "event": "telemetry.export_failed",
            "error_code": "export_failed",
            "error": {"type": "RuntimeError", "code": "internal_error"},
            "endpoint": "https://collector-user:collector-pass@example.test",
        }
    ) == {"event": "telemetry.export_failed", "error_code": "export_failed"}


def test_structured_error_is_allowed_only_by_its_event_registry() -> None:
    structured = {"type": "RuntimeError", "code": "internal_error", "traceback": []}
    assert (
        filter_log_event({"event": "api.request_failed", "error": structured})["error"]["type"]
        == "RuntimeError"
    )
    assert "error" not in filter_log_event({"event": "worker.started", "error": structured})
```

```python
@pytest.mark.parametrize(
    "key",
    [
        "prompt",
        "completion",
        "sql",
        "tool_input",
        "tool_output",
        "request_body",
        "response_body",
        "webhook_payload",
        "secret_env",
    ],
)
def test_payload_fields_are_always_redacted(key: str) -> None:
    assert structural_redaction({key: "payload-canary"}) == {key: "[REDACTED]"}


def test_redaction_bounds_are_exact() -> None:
    nested: object = "leaf"
    for _ in range(9):
        nested = {"child": nested}
    redacted = structural_redaction(
        {
            "nested": nested,
            "mapping": {str(i): i for i in range(65)},
            "items": list(range(65)),
            "text": "x" * 2_001,
        }
    )
    assert "[TRUNCATED]" in json.dumps(redacted)
    assert len(redacted["mapping"]) == 64
    assert len(redacted["items"]) == 64
    assert len(redacted["text"]) == 2_000
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
uv run pytest packages/observability/tests/test_logging.py packages/observability/tests/test_errors.py -q
```

Expected: FAIL because `configure_json_logging`, `structural_redaction`, `SafeErrorCode`, and the schema-v1 processors do not exist.

- [ ] **Step 3: Implement the bounded structural redactor and safe error contract**

Implement these constants and recursive order; the structural processor must run before unknown-object stringification and service value processors must run before final JSON rendering:

```python
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

LOG_SCHEMA_VERSION = 1
REDACTED = "[REDACTED]"
MAX_LOG_DEPTH = 8
MAX_LOG_ITEMS = 64
MAX_LOG_STRING = 2_000
MAX_TRACEBACK_FRAMES = 32
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "api_key",
        "private_key",
        "dsn",
        "prompt",
        "completion",
        "sql",
        "tool_input",
        "tool_output",
        "request_body",
        "response_body",
        "webhook_payload",
        "secret_env",
    }
)
SENSITIVE_KEY_SUFFIXES = (
    "_authorization",
    "_cookie",
    "_password",
    "_secret",
    "_token",
    "_api_key",
    "_private_key",
    "_dsn",
)


def is_sensitive_key(key: str) -> bool:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key.strip())
    normalized = re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")
    return normalized in SENSITIVE_KEYS or normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def sanitize_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return value[:MAX_LOG_STRING]
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))[:MAX_LOG_STRING]


def structural_redaction(value: object, *, _depth: int = 0) -> object:
    if _depth >= MAX_LOG_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in list(value.items())[:MAX_LOG_ITEMS]:
            safe_key = str(key)[:128]
            result[safe_key] = (
                REDACTED
                if is_sensitive_key(safe_key)
                else structural_redaction(item, _depth=_depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [structural_redaction(item, _depth=_depth + 1) for item in value[:MAX_LOG_ITEMS]]
    if isinstance(value, str):
        candidate = sanitize_url(value) if "://" in value else value
        return candidate[:MAX_LOG_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return str(value)[:MAX_LOG_STRING]
    except Exception:
        return "[UNSUPPORTED]"
```

Create `events.py` with the fail-closed field registry. These are the only non-contract keys accepted from application calls; `filter_log_event` validates values instead of truncating arbitrary strings:

```python
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path

from jhin_observability.errors import SafeErrorCode
from jhin_observability.redaction import MAX_TRACEBACK_FRAMES


class FieldKind(StrEnum):
    ID = "id"
    COUNT = "count"
    SECONDS = "seconds"
    BOOL = "bool"
    ENUM = "enum"
    ERROR_TYPE = "error_type"
    ERROR = "error"


CONTEXT_FIELD_RULES = {
    "request_id": FieldKind.ID,
    "correlation_id": FieldKind.ID,
    "workspace_id": FieldKind.ID,
    "task_id": FieldKind.ID,
    "run_id": FieldKind.ID,
    "trace_id": FieldKind.ID,
    "span_id": FieldKind.ID,
}
EVENT_FIELD_RULES: dict[str, dict[str, FieldKind]] = {
    "api.started": {},
    "api.stopped": {},
    "api.request_failed": {
        "error_code": FieldKind.ENUM,
        "error": FieldKind.ERROR,
    },
    "api.request_finished": {
        "http_method": FieldKind.ENUM,
        "http_route": FieldKind.ENUM,
        "http_status_class": FieldKind.ENUM,
    },
    "secrets.master_key_unavailable": {"error_code": FieldKind.ENUM},
    "security.master_key_env_source": {},
    "temporal.connect_retry": {
        "error_type": FieldKind.ERROR_TYPE,
        "retry_in_seconds": FieldKind.SECONDS,
    },
    "temporal.connected": {"task_queue": FieldKind.ENUM},
    "resources.retry": {
        "error_type": FieldKind.ERROR_TYPE,
        "retry_in_seconds": FieldKind.SECONDS,
    },
    "resources.ready": {},
    "nats.connect_retry": {
        "error_type": FieldKind.ERROR_TYPE,
        "retry_in_seconds": FieldKind.SECONDS,
    },
    "nats.connected": {"stream": FieldKind.ENUM},
    "worker.started": {"task_queue": FieldKind.ENUM},
    "worker.stopping": {},
    "events.publish_failed": {
        "event_type": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
    },
    "concurrency.kick_failed": {"error_type": FieldKind.ERROR_TYPE},
    "model.client_close_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox.workspace_cleanup": {"deleted": FieldKind.BOOL},
    "sandbox.network_created": {"network_policy": FieldKind.ENUM},
    "sandbox.network_ensure_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox.job.finished": {
        "job_id": FieldKind.ID,
        "outcome": FieldKind.ENUM,
        "exit_code": FieldKind.COUNT,
        "network_policy": FieldKind.ENUM,
    },
    "sandbox.reaped_container": {"count": FieldKind.COUNT},
    "sandbox.reaped_workspace": {"count": FieldKind.COUNT},
    "sandbox.reap_containers_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox.reap_volumes_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox_runner.started": {
        "network_policy": FieldKind.ENUM,
        "token_configured": FieldKind.BOOL,
    },
    "trigger.task_deduped": {},
    "trigger.invoked": {"connector_type": FieldKind.ENUM, "outcome": FieldKind.ENUM},
    "trigger.duplicate_suppressed": {"connector_type": FieldKind.ENUM},
    "trigger.no_agent": {"connector_type": FieldKind.ENUM},
    "trigger.workflow_already_started": {"connector_type": FieldKind.ENUM},
    "webhook.accepted": {"connector_type": FieldKind.ENUM, "outcome": FieldKind.ENUM},
    "webhook.publish_or_commit_failed": {
        "connector_type": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
    },
    "webhook.rollback_failed": {"connector_type": FieldKind.ENUM},
    "jetstream.consumer_created": {"stream": FieldKind.ENUM, "consumer": FieldKind.ENUM},
    "jetstream.consumer_loop_started": {
        "stream": FieldKind.ENUM,
        "consumer": FieldKind.ENUM,
    },
    "jetstream.consumer_handler_failed": {
        "stream": FieldKind.ENUM,
        "consumer": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
        "error_code": FieldKind.ENUM,
        "error": FieldKind.ERROR,
    },
    "heartbeat.recorded": {},
    "ingress.invalid_envelope": {"error_code": FieldKind.ENUM},
    "ingress.unhandled": {"connector_type": FieldKind.ENUM, "event_type": FieldKind.ENUM},
    "ingress.normalized": {
        "connector_type": FieldKind.ENUM,
        "event_type": FieldKind.ENUM,
        "produced": FieldKind.COUNT,
    },
    "event.invalid_envelope": {"error_code": FieldKind.ENUM},
    "event.duplicate_skipped": {"num_delivered": FieldKind.COUNT},
    "event.processed": {"event_type": FieldKind.ENUM, "num_delivered": FieldKind.COUNT},
    "telemetry.queue_dropped": {"count": FieldKind.COUNT, "queue_capacity": FieldKind.COUNT},
    "telemetry.export_failed": {"error_code": FieldKind.ENUM},
    "telemetry.export_recovered": {},
    "telemetry.nats_lag_probe_failed": {
        "stream": FieldKind.ENUM,
        "consumer": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
    },
    "telemetry.connector_health_probe_failed": {"error_type": FieldKind.ERROR_TYPE},
    "web.started": {},
    "web.stopping": {"signal": FieldKind.ENUM},
    "web.rewrite_configured": {"http_route": FieldKind.ENUM},
    "web.request_failed": {
        "http_method": FieldKind.ENUM,
        "http_route": FieldKind.ENUM,
        "error_code": FieldKind.ENUM,
    },
    "web.framework_output_suppressed": {
        "stream": FieldKind.ENUM,
        "count": FieldKind.COUNT,
    },
    "stdlib.message": {},
    "log.event_rejected": {},
}

FIELD_ENUM_VALUES: dict[str, frozenset[str]] = {
    "connector_type": frozenset({"github", "linear", "vercel", "supabase", "cli", "other"}),
    "consumer": frozenset({"event-worker", "event-worker-ingress", "other"}),
    "error_code": frozenset(code.value for code in SafeErrorCode),
    "event_type": frozenset({"connector", "task", "run", "tool", "approval", "other"}),
    "http_method": frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"}),
    "http_route": frozenset({"/api/:path*", "other"}),
    "http_status_class": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "other"}),
    "network_policy": frozenset({"none", "internet", "other"}),
    "outcome": frozenset(
        {
            "ok",
            "accepted",
            "started",
            "completed",
            "failed",
            "cancelled",
            "timeout",
            "duplicate",
            "other",
        }
    ),
    "signal": frozenset({"SIGINT", "SIGTERM", "other"}),
    "stream": frozenset({"INGRESS", "EVENTS", "stdout", "stderr", "other"}),
    "task_queue": frozenset(
        {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue", "other"}
    ),
}
EVENT_FIELD_ENUM_VALUES: dict[tuple[str, str], frozenset[str]] = {
    ("telemetry.export_failed", "error_code"): frozenset({"export_timeout", "export_failed"}),
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
BASE_FIELDS = frozenset(
    {"schema_version", "timestamp", "level", "service", "environment", "event", "logger"}
)


def normalize_log_field(event: str, key: str, value: object, kind: FieldKind) -> object | None:
    if kind is FieldKind.ID:
        return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None
    if kind is FieldKind.COUNT:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )
    if kind is FieldKind.SECONDS:
        return (
            value
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            else None
        )
    if kind is FieldKind.BOOL:
        return value if isinstance(value, bool) else None
    if kind is FieldKind.ERROR_TYPE:
        return value if isinstance(value, str) and _ERROR_TYPE_RE.fullmatch(value) else None
    if kind is FieldKind.ERROR:
        return filter_structured_error(value) if isinstance(value, Mapping) else None
    exact = EVENT_FIELD_ENUM_VALUES.get((event, key))
    if exact is not None:
        return value if isinstance(value, str) and value in exact else None
    allowed = FIELD_ENUM_VALUES.get(key, frozenset({"other"}))
    if isinstance(value, str) and value in allowed:
        return value
    return "other" if "other" in allowed else None


def filter_structured_error(value: Mapping[str, object]) -> dict[str, object]:
    error_type = normalize_log_field(
        "structured.error", "error_type", value.get("type"), FieldKind.ERROR_TYPE
    )
    error_code = normalize_log_field(
        "structured.error", "error_code", value.get("code"), FieldKind.ENUM
    )
    frames: list[dict[str, object]] = []
    raw_frames = value.get("traceback")
    if isinstance(raw_frames, Sequence) and not isinstance(raw_frames, (str, bytes)):
        for raw in raw_frames[:MAX_TRACEBACK_FRAMES]:
            if not isinstance(raw, Mapping):
                continue
            filename = Path(str(raw.get("file", "unknown"))).name[:128]
            function = str(raw.get("function", "unknown"))[:128]
            line = raw.get("line", 0)
            frames.append(
                {
                    "file": filename
                    if _ERROR_TYPE_RE.fullmatch(filename.replace("-", "_"))
                    else "unknown",
                    "function": function if _ERROR_TYPE_RE.fullmatch(function) else "unknown",
                    "line": line if isinstance(line, int) and line >= 0 else 0,
                }
            )
    return {
        "type": error_type or "Error",
        "code": error_code or SafeErrorCode.INTERNAL_ERROR.value,
        "traceback": frames,
    }


def filter_log_event(event_dict: Mapping[str, object]) -> dict[str, object]:
    raw_event = event_dict.get("event")
    event = (
        raw_event
        if isinstance(raw_event, str) and raw_event in EVENT_FIELD_RULES
        else "log.event_rejected"
    )
    output = {key: event_dict[key] for key in BASE_FIELDS - {"event"} if key in event_dict}
    output["event"] = event
    rules = {**CONTEXT_FIELD_RULES, **EVENT_FIELD_RULES[event]}
    for key, kind in rules.items():
        if (
            key in event_dict
            and (value := normalize_log_field(event, key, event_dict[key], kind)) is not None
        ):
            output[key] = value
    return output
```

`safe_error` must expose only the bounded exception class name and caller-selected closed code. It never stores or exports `str(exc)`, exception arguments, causes, or context, and it must not infer codes from arbitrary exception messages. The logger's separate structured traceback path is subjected to both known-value and structural passes before rendering.

Implement `errors.py` with the complete closed contract (and export all three public names):

```python
import re
from dataclasses import dataclass
from enum import StrEnum


class SafeErrorCode(StrEnum):
    INTERNAL_ERROR = "internal_error"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    TIMEOUT = "timeout"
    CONFLICT = "conflict"
    EXECUTION_UNKNOWN = "execution_unknown"


@dataclass(frozen=True)
class SafeError:
    type: str
    code: SafeErrorCode


_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


def safe_error(exc: BaseException, *, code: SafeErrorCode) -> SafeError:
    error_type = type(exc).__name__
    return SafeError(
        type=error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "Error",
        code=code,
    )
```

- [ ] **Step 4: Replace the logger pipeline with schema-v1 rendering**

Rename `configure_logging` to `configure_json_logging` and keep `configure_logging = configure_json_logging` for one migration task only. Use this processor order:

```python
import structlog
from structlog.typing import Processor


shared_processors: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    add_contract_fields(service=service, environment=environment),
    add_current_trace_ids,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
]

formatter = structlog.stdlib.ProcessorFormatter(
    foreign_pre_chain=shared_processors,
    processors=[
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        normalize_exception(max_frames=MAX_TRACEBACK_FRAMES),
        structural_redaction_processor,
        *extra_processors,
        structural_redaction_processor,
        filter_log_event_processor,
        structlog.processors.JSONRenderer(sort_keys=True),
    ],
)
```

`add_contract_fields` sets `schema_version`, `service`, and `environment`. A foreign stdlib record is assigned `event="stdlib.message"`; its message, positional arguments, and formatted text are discarded. An unregistered structlog event becomes `log.event_rejected`, with no copy of the original event. `normalize_exception` emits only `error.type`, the caller-supplied closed `error.code`, and traceback frame objects `{file basename, function identifier, line integer}`; it removes exception messages, source lines, locals, arguments, causes, and the raw `exception` value.

- [ ] **Step 5: Add the AST logger audit and migrate every current unsafe call**

`scripts/audit_phase10_logging.py` exports `audit_paths(paths: Sequence[Path]) -> list[AuditFailure]`. For each AST it resolves imports, records every local assigned from `get_logger(...)` or `logging.getLogger(...)`, and audits every call on those bindings plus `activity.logger`. Methods `debug|info|warning|error|exception|critical` must have one literal registered event, no additional positional arguments, and keyword names contained by that event's rules, context rules, or `exc_info`. A logger binding with a dynamic receiver or a logging-method call that cannot be resolved is itself an `unresolved_logger_receiver` failure, so renaming `logger` cannot escape the audit. The audit also rejects `print(...)` in application Python, `traceback.print_*`, direct `sys.stdout/sys.stderr.write`, and stdlib `logging` calls outside `jhin_observability`.

```python
from __future__ import annotations

import ast
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from jhin_observability.events import CONTEXT_FIELD_RULES, EVENT_FIELD_RULES

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AuditFailure:
    path: Path
    line: int
    code: Literal[
        "dynamic_event",
        "unregistered_event",
        "positional_text",
        "unregistered_field",
        "direct_print",
        "direct_stream_write",
        "foreign_logging",
        "unresolved_logger_receiver",
    ]


AUDIT_EXCLUDED_PARTS = frozenset({"tests", "testing", "alembic", "__pycache__"})
AUDIT_EXCLUDED_FILES = frozenset({"seed.py", "migrate.py"})


def application_python_paths(root: Path) -> tuple[Path, ...]:
    source_roots = (root / "apps/api/src", root / "packages", root / "services")
    return tuple(
        sorted(
            path
            for source_root in source_roots
            for path in source_root.rglob("*.py")
            if not set(path.parts) & AUDIT_EXCLUDED_PARTS and path.name not in AUDIT_EXCLUDED_FILES
        )
    )


LOGGER_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
)


@dataclass(frozen=True)
class LoggingCall:
    path: Path
    line: int
    column: int
    method: str
    receiver: Literal["logger", "foreign_logging", "unresolved_logger_receiver"]


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _qualified_name(node: ast.expr, aliases: Mapping[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value, aliases)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


LOGGER_FACTORIES = frozenset(
    {
        "structlog.get_logger",
        "logging.getLogger",
        "jhin_observability.get_logger",
        "jhin_observability.logging.get_logger",
    }
)


def _logger_bindings(tree: ast.AST, aliases: Mapping[str, str]) -> frozenset[str]:
    bindings: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        if _qualified_name(value.func, aliases) not in LOGGER_FACTORIES:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        bindings.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(bindings)


def _enclosing_function(
    node: ast.AST, parents: Mapping[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent
        parent = parents.get(parent)
    return None


def _has_parameter(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    name: str,
    annotation: str,
) -> bool:
    return any(
        argument.arg == name
        and argument.annotation is not None
        and ast.unparse(argument.annotation) == annotation
        for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    )


def _assigns_container_lookup(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    return any(
        isinstance(candidate, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "container"
            for target in candidate.targets
        )
        and isinstance(candidate.value, ast.Call)
        and ast.unparse(candidate.value.func) == "self.docker.containers.container"
        for candidate in ast.walk(function)
    )


def _is_reviewed_non_logger_call(
    path: Path,
    node: ast.Call,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    """Recognize only the two audited APIs whose method names resemble loggers."""
    if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
        return False
    receiver = node.func.value.id
    method = node.func.attr
    function = _enclosing_function(node, parents)
    if function is None:
        return False
    relative = path.as_posix()
    if (
        relative.endswith("services/sandbox_runner/src/jhin_sandbox_runner/jobs.py")
        and receiver == "container"
        and method == "log"
    ):
        return (
            function.name == "_collect_logs"
            and _has_parameter(function, name="container", annotation="Any")
        ) or (function.name == "current_logs" and _assigns_container_lookup(function))
    return (
        relative.endswith("packages/connectors/src/jhin_connectors/supabase/database_tools.py")
        and function.name == "consume_result"
        and receiver == "completed"
        and method == "exception"
        and _has_parameter(function, name="completed", annotation="asyncio.Future[Any]")
    )


def collect_logging_method_calls(
    paths: Sequence[Path],
) -> tuple[LoggingCall, ...]:
    calls: list[LoggingCall] = []
    for path in paths:
        tree = ast.parse(path.read_text())
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        aliases = _import_aliases(tree)
        bindings = _logger_bindings(tree, aliases)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            qualified_call = _qualified_name(node.func, aliases)
            method = qualified_call.rsplit(".", 1)[-1]
            if method not in LOGGER_METHODS:
                continue
            if isinstance(node.func, ast.Name):
                kind = (
                    "foreign_logging"
                    if qualified_call.startswith("logging.")
                    else "unresolved_logger_receiver"
                )
                calls.append(
                    LoggingCall(
                        path,
                        node.lineno,
                        node.col_offset,
                        method,
                        cast(
                            Literal["logger", "foreign_logging", "unresolved_logger_receiver"], kind
                        ),
                    )
                )
                continue
            assert isinstance(node.func, ast.Attribute)
            if _is_reviewed_non_logger_call(path, node, parents):
                continue
            receiver_node = node.func.value
            receiver_name = _qualified_name(receiver_node, aliases)
            if receiver_name == "temporalio.activity" and method == "info":
                continue  # SDK metadata lookup, not a log write.
            direct_factory = (
                isinstance(receiver_node, ast.Call)
                and _qualified_name(receiver_node.func, aliases) in LOGGER_FACTORIES
            )
            if (
                (isinstance(receiver_node, ast.Name) and receiver_node.id in bindings)
                or receiver_name == "temporalio.activity.logger"
                or direct_factory
            ):
                kind = "logger"
            elif receiver_name == "logging":
                kind = "foreign_logging"
            else:
                # Fail closed on every logging-method-shaped call. Add an explicit
                # non-logger exemption above only after a repository-wide review.
                kind = "unresolved_logger_receiver"
            calls.append(
                LoggingCall(
                    path,
                    node.lineno,
                    node.col_offset,
                    method,
                    cast(Literal["logger", "foreign_logging", "unresolved_logger_receiver"], kind),
                )
            )
    return tuple(
        sorted(
            calls,
            key=lambda item: (item.path.as_posix(), item.line, item.column, item.method),
        )
    )


def audit_paths(paths: Sequence[Path]) -> list[AuditFailure]:
    calls = collect_logging_method_calls(paths)
    by_location = {(item.path, item.line, item.column, item.method): item for item in calls}
    failures: list[AuditFailure] = []
    for path in paths:
        tree = ast.parse(path.read_text())
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            qualified = _qualified_name(node.func, aliases)
            if qualified == "print" or qualified.startswith("traceback.print_"):
                failures.append(AuditFailure(path, node.lineno, "direct_print"))
                continue
            if qualified in {"sys.stdout.write", "sys.stderr.write"}:
                failures.append(AuditFailure(path, node.lineno, "direct_stream_write"))
                continue
            method = qualified.rsplit(".", 1)[-1]
            found = by_location.get((path, node.lineno, node.col_offset, method))
            if found is None:
                continue
            if found.receiver != "logger":
                failures.append(AuditFailure(path, node.lineno, found.receiver))
                continue
            if len(node.args) != 1:
                failures.append(AuditFailure(path, node.lineno, "positional_text"))
                continue
            event_arg = node.args[0]
            if not isinstance(event_arg, ast.Constant) or not isinstance(event_arg.value, str):
                failures.append(AuditFailure(path, node.lineno, "dynamic_event"))
                continue
            event = event_arg.value
            if event not in EVENT_FIELD_RULES:
                failures.append(AuditFailure(path, node.lineno, "unregistered_event"))
                continue
            allowed = set(CONTEXT_FIELD_RULES) | set(EVENT_FIELD_RULES[event]) | {"exc_info"}
            for keyword in node.keywords:
                if keyword.arg is None or keyword.arg not in allowed:
                    failures.append(AuditFailure(path, node.lineno, "unregistered_field"))
    return failures


def test_repository_logger_calls_are_all_registered() -> None:
    paths = application_python_paths(REPO_ROOT)
    failures = audit_paths(paths)
    assert failures == []
    assert collect_logging_method_calls(paths)


def test_reviewed_non_logger_receivers_are_semantically_narrow(tmp_path: Path) -> None:
    jobs = tmp_path / "services/sandbox_runner/src/jhin_sandbox_runner/jobs.py"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(
        "from typing import Any\n"
        "class C:\n"
        " async def _collect_logs(self, container: Any):\n"
        "  await container.log(stdout=True)\n"
        " async def current_logs(self):\n"
        "  container = self.docker.containers.container('id')\n"
        "  await container.log(stderr=True)\n"
    )
    database = tmp_path / "packages/connectors/src/jhin_connectors/supabase/database_tools.py"
    database.parent.mkdir(parents=True)
    database.write_text(
        "import asyncio\nfrom typing import Any\n"
        "def consume_result(completed: asyncio.Future[Any]):\n"
        " return completed.exception()\n"
    )
    assert collect_logging_method_calls((jobs, database)) == ()

    jobs.write_text("def current_logs(logger):\n logger.log('raw')\n")
    calls = collect_logging_method_calls((jobs,))
    assert [(call.method, call.receiver) for call in calls] == [
        ("log", "unresolved_logger_receiver")
    ]


def test_unresolved_actual_logger_shape_still_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text("def emit(audit_logger):\n audit_logger.exception('raw')\n")
    assert [failure.code for failure in audit_paths((source,))] == ["unresolved_logger_receiver"]


def main() -> int:
    failures = audit_paths(application_python_paths(REPO_ROOT))
    for failure in failures:
        relative = failure.path.relative_to(REPO_ROOT)
        sys.stderr.write(f"{relative}:{failure.line}: {failure.code}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`audit_paths` iterates exactly the sorted `(path, line, column, method)` records returned by `collect_logging_method_calls`; the nonempty assertion plus per-event renderer parameterization is the executable AST/runtime coverage proof. `testing/` fake-provider processes and the one-shot `seed.py`/`migrate.py` command interfaces are not any of the seven application services and intentionally retain their CLI protocols.

Run the audit before migration:

```bash
uv run pytest packages/observability/tests/test_log_audit.py -q
```

Expected: FAIL on the free-text master-key warning, formatted heartbeat activity log, retry `error` strings, provider addresses/URLs, subjects, Docker error text, and other fields absent from `EVENT_FIELD_RULES`.

Migrate every file in this task's `Files` block to these canonical call shapes; the same shape replaces each repeated call in agent/event/workflow/tool workers:

```python
logger.warning("temporal.connect_retry", error_type=type(exc).__name__, retry_in_seconds=delay)
logger.warning("resources.retry", error_type=type(exc).__name__, retry_in_seconds=delay)
logger.warning("nats.connect_retry", error_type=type(exc).__name__, retry_in_seconds=delay)
logger.info("temporal.connected", task_queue=task_queue)
logger.info("nats.connected", stream=stream)
logger.info("resources.ready")
logger.info("worker.started", task_queue=task_queue)
logger.info("worker.stopping")
logger.info("api.started")
logger.info("api.stopped")
logger.warning(
    "secrets.master_key_unavailable",
    error_code=SafeErrorCode.INTERNAL_ERROR.value,
)
logger.warning("events.publish_failed", event_type=event_family, error_type=type(exc).__name__)
logger.warning("concurrency.kick_failed", error_type=type(exc).__name__)
logger.warning("model.client_close_failed", error_type=type(exc).__name__)
logger.info("trigger.task_deduped", task_id=str(existing.id))
logger.warning("trigger.no_agent", connector_type=normalize_connector_type(envelope.source.type))
logger.info(
    "trigger.workflow_already_started",
    connector_type=normalize_connector_type(envelope.source.type),
)
logger.info(
    "trigger.invoked",
    connector_type=normalize_connector_type(envelope.source.type),
    outcome="started",
)
logger.info("sandbox.workspace_cleanup", run_id=params.run_id, deleted=deleted)
logger.info("security.master_key_env_source")
activity.logger.info("heartbeat.recorded")
logger.info("jetstream.consumer_created", stream=stream, consumer=durable)
logger.info("jetstream.consumer_loop_started", stream=stream, consumer=durable)
logger.exception(
    "jetstream.consumer_handler_failed",
    stream=stream,
    consumer=durable,
    error_type=type(exc).__name__,
    error_code=SafeErrorCode.INTERNAL_ERROR.value,
)
logger.info("webhook.accepted", connector_type=connector_type, outcome="accepted")
logger.error(
    "webhook.publish_or_commit_failed",
    connector_type=connector_type,
    error_type=type(exc).__name__,
)
logger.error("webhook.rollback_failed", connector_type=connector_type)
logger.error("ingress.invalid_envelope", error_code=SafeErrorCode.INVALID_REQUEST.value)
logger.warning("ingress.unhandled", connector_type=connector_type, event_type=event_family)
logger.info(
    "ingress.normalized",
    connector_type=connector_type,
    event_type=event_family,
    produced=len(normalized),
)
logger.error("event.invalid_envelope", error_code=SafeErrorCode.INVALID_REQUEST.value)
logger.info("event.duplicate_skipped", num_delivered=metadata.num_delivered)
logger.info("event.processed", event_type=event_family, num_delivered=metadata.num_delivered)
logger.warning("sandbox.network_ensure_failed", error_type=type(exc).__name__)
logger.warning("sandbox.reap_containers_failed", error_type=type(exc).__name__)
logger.warning("sandbox.reap_volumes_failed", error_type=type(exc).__name__)
logger.info("sandbox.network_created")
logger.info(
    "sandbox_runner.started",
    token_configured=bool(active_settings.sandbox_runner_token),
)
logger.info(
    "sandbox.job.finished",
    job_id=request.job_id,
    outcome=normalize_sandbox_outcome(record.status),
    exit_code=max(0, record.exit_code or 0),
    network_policy=request.network_policy,
)
```

The sandbox reaper aggregates successful deletions and emits one `sandbox.reaped_container`/`sandbox.reaped_workspace` record with `count`, never one identifier-bearing record per object. Do not add a registry entry merely to preserve an unsafe call.

- [ ] **Step 6: Run focused GREEN, the repository audit, and secret-redaction tests**

Run:

```bash
uv run pytest packages/observability/tests packages/secrets/tests/test_redaction.py -q
uv run python scripts/audit_phase10_logging.py
uv run ruff check packages/observability packages/secrets/tests/test_redaction.py
uv run mypy packages/observability/src packages/observability/tests
```

Expected: PASS; captured stdout is valid JSONL and no canary survives either redaction pass.

- [ ] **Step 7: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

Expected: only Task 1 files are committed.

#### Binding protected-health registry reservation

The Task 1 contract reserves exactly `health.heartbeat_write_failed` in the closed JSON-v1
event registry and its existing audit tests. It has no event-specific fields. Protected-health
code may emit only:

```python
logger.warning("health.heartbeat_write_failed")
```

It must not pass a service field, exception/reason text, identity, URL, state, canary, or arbitrary
extra. The fixed JSON base field already names the emitting service. This reservation changes no
Task 1 manifest because the registry and audit paths are already Task 1-owned.

#### Final executable contract for Task 1


The following changes are mandatory before dispatching Task 1. They resolve the dependency
contradictions, incomplete File Map, false-positive AST audit, missing environment identity, and
insufficient GREEN suite found by preflight.

### 6.1 Exhaustive File Map additions

Add these currently absent paths to the global File Map:

```text
packages/secrets/pyproject.toml
packages/secrets/tests/test_crypto.py
services/tool_worker/src/jhin_tool_worker/trigger_activities.py
services/tool_worker/tests/test_advertised_tools.py
services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py
services/sandbox_runner/tests/test_rootless_transport.py
compose.rootless.yaml
```

The following paths already appear in the File Map but must be assigned to Task 1 as modifications:

```text
packages/events/pyproject.toml
services/tool_worker/pyproject.toml
uv.lock
tests/test_worker_dependency_boundaries.py
services/tool_worker/tests/test_worker_registration.py
services/agent_worker/src/jhin_agent_worker/projections.py
services/agent_worker/src/jhin_agent_worker/reasoning.py
services/agent_worker/src/jhin_agent_worker/settings.py
services/event_worker/src/jhin_event_worker/settings.py
services/workflow_worker/src/jhin_workflow_worker/settings.py
services/sandbox_runner/src/jhin_sandbox_runner/settings.py
services/tool_worker/src/jhin_tool_worker/settings.py
compose.yaml
scripts/assert_phase10_tool_worker_compose.py
tests/test_phase10_tool_worker_compose.py
```

Add every path in both lists to Task 1's `Files` block and exact staging manifest. Do not leave an
implementation path implied only by the repository-wide audit.

### 6.2 Bind the dependency decision in Task 1

Task 1, not Task 6, owns the first usable JSON-v1 imports:

- add `jhin-observability` plus its workspace source to `services/tool_worker/pyproject.toml`;
- add `jhin-observability` plus its workspace source to `packages/secrets/pyproject.toml`;
- add `jhin-observability` plus its workspace source to `packages/events/pyproject.toml` so the
  consumer imports the canonical `SafeErrorCode` rather than duplicating a string literal; and
- regenerate `uv.lock` in Task 1, then require `uv lock --check` in GREEN.

Update `tests/test_worker_dependency_boundaries.py` in Task 1: require the tool-worker dependency
and source, require its `jhin_observability` import, and preserve unchanged the prohibitions on
agent/model dependencies and imports. The older observability-negative assertions are deleted;
the authority-boundary assertions are not.

Remove `configure_current_logging` in Task 1. Update both
`services/tool_worker/tests/test_worker_registration.py` and
`services/tool_worker/tests/test_advertised_tools.py` to patch/assert the JSON-v1 bootstrap with
`service="tool-worker"`, normalized environment, and configured level. Do not retain a helper that
hides the environment parameter. The temporary public `configure_logging = configure_json_logging`
alias may remain in `jhin_observability` until Task 6, but no production entrypoint may call it after
Task 1.

### 6.3 Define the closed normalizers instead of referencing nonexistent APIs

Add and export one authoritative implementation in `jhin_observability.events`:

```python
ENVIRONMENTS = frozenset({"dev", "test", "staging", "production"})
CONNECTOR_TYPES = frozenset({"github", "linear", "vercel", "supabase", "cli"})
EVENT_FAMILIES = frozenset({"connector", "task", "run", "tool", "approval"})
SANDBOX_OUTCOMES = frozenset({
    "ok", "accepted", "started", "completed", "failed",
    "cancelled", "timeout", "duplicate",
})


def normalize_environment(raw: object) -> str:
    value = getattr(raw, "value", raw)
    text = value.strip().lower() if isinstance(value, str) else ""
    aliases = {"development": "dev", "prod": "production"}
    normalized = aliases.get(text, text)
    return normalized if normalized in ENVIRONMENTS else "production"


def normalize_connector_type(raw: object) -> str:
    value = getattr(raw, "value", raw)
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in CONNECTOR_TYPES else "other"


def normalize_event_family(raw: object) -> str:
    value = getattr(raw, "value", raw)
    text = value.strip().lower() if isinstance(value, str) else ""
    family = text.split(".", 1)[0]
    return family if family in EVENT_FAMILIES else "other"


def normalize_sandbox_outcome(raw: object) -> str:
    value = getattr(raw, "value", raw)
    text = value.strip().lower() if isinstance(value, str) else ""
    aliases = {"running": "started"}
    normalized = aliases.get(text, text)
    return normalized if normalized in SANDBOX_OUTCOMES else "other"
```

Test every admitted value, both aliases, enum-like `.value` inputs, whitespace/case handling, and
unknown/non-string fallback. Replace the plan's bare `event_family` placeholder with
`normalize_event_family(event_type)` at each source call.

Also import `CONTEXT_FIELD_RULES` explicitly from `jhin_observability.events` in the Step 1 test that
asserts `job_id` is not a context field. Do not rely on an accidental package-level re-export.

### 6.4 Register and bootstrap the standalone rootless adapter

Add exact event contracts:

```python
"rootless_transport.ready": {},
"rootless_transport.failed": {"error_code": FieldKind.ENUM},
```

and exact event-specific failure values:

```python
("rootless_transport.failed", "error_code"): frozenset({
    "configuration_error", "upstream_unavailable",
}),
```

Migrate `packages/secrets/.../crypto.py`, tool-worker `main.py`, `resources.py`,
`trigger_activities.py`, and sandbox `rootless_transport.py` from bound stdlib loggers to
`jhin_observability.get_logger`. The adapter calls `configure_json_logging` before validation or any
log write, with service `rootless-docker-transport`, normalized `APP_ENV`, and `LOG_LEVEL`; it emits
only the two registered events and their closed code. It never emits socket paths, payload bytes,
exception text, or arbitrary `extra` fields.

Give the adapter's private configuration exception a closed code field: identity/argument/socket-
shape validation uses `configuration_error`, while probe/relay loss uses `upstream_unavailable`.
`main()` logs that field directly; it never derives a code by inspecting exception text.

Update `services/sandbox_runner/tests/test_rootless_transport.py` to capture stdout and assert valid
JSON-v1 for ready/failure records plus absence of socket/error/payload canaries.

Replace `packages/secrets/tests/test_crypto.py`'s stale `caplog` assertion for the
free-text `MASTER_KEY environment variable` warning. The test must configure JSON-v1
logging explicitly, capture and parse the emitted stdout line, assert
`event == "security.master_key_env_source"` with the required schema/service/environment
base fields and no event-specific fields, and assert that neither the environment variable name,
key material, nor any free-text warning survives anywhere in the serialized record. Restore any
logging globals the test changes so it is order-independent.

### 6.5 Make environment identity real for every process

Add `app_env` to event-worker, workflow-worker, and sandbox-runner settings. Change the agent/tool
defaults from `development` to the shared closed value `dev`; tests assert the stored defaults are
closed. Task 1's temporary direct bootstrap may normalize the two documented legacy aliases, while
Task 6's `ObservabilitySettings` inheritance rejects every non-closed stored value. Use the
authoritative normalizer at all six
ordinary Python entrypoints; API/agent/tool already have a source setting but must also normalize
it. Pass `APP_ENV: ${APP_ENV:-production}` to workflow-worker, event-worker, and sandbox-runner in
`compose.yaml`; pass both `APP_ENV` and `LOG_LEVEL` to
`rootless-docker-transport` in `compose.rootless.yaml`.

Extend `scripts/assert_phase10_tool_worker_compose.py` and its tests so production/dev/rootful/
rootless renders assert the exact `APP_ENV` on API, agent-worker, tool-worker, event-worker,
workflow-worker, sandbox-runner, and (when present) rootless-docker-transport. Add an AST bootstrap
regression to `packages/observability/tests/test_log_audit.py` for the seven entrypoints: each must
call `configure_json_logging` with its exact service and a normalized environment before its first
application log/resource action.

### 6.6 Correct the AST audit and its semantic exemptions

Change `_logger_bindings` from an undifferentiated set to a binding-kind map. Bindings and direct
factories from `logging.getLogger` are always `foreign_logging`; only structlog/Jhin factories are
contract loggers. Therefore `logging.getLogger(...).warning("api.started")` must fail even though
the literal event is registered. Retain the external-stdlib runtime-renderer test; it proves foreign
library records are safely captured, not that application code may use stdlib logging.

Add only these two non-log protocol exemptions:

1. In `packages/workflows/.../poller_health.py`, permit only the current closed-token `print` shapes
   inside `main` and `run`: the conditional `_READY_OUTPUT`/`_UNAVAILABLE_OUTPUT` expression and the
   single `_UNAVAILABLE_OUTPUT` argument. Any extra/dynamic argument, f-string, different constant,
   or print in another function remains `direct_print`.
2. In `JobManager.start`, permit only the awaited `client.system.info()` where `client` is the local
   name assigned from `aiodocker.Docker(url=validated_url)`. Any other `.info()` receiver, owner
   function, assignment source, or unawaited shape remains `unresolved_logger_receiver`.

Add mutation regressions for each rejected near miss, plus a regression proving a bound stdlib
logger is `foreign_logging`. Run the existing closed-output
`packages/workflows/tests/test_poller_health.py` suite unchanged.

Migrate the four accepted-predecessor sources that the old Task 1 omitted:

- projections: normalize event family, remove workflow IDs, use `error_type`;
- reasoning: remove redacted/free text and use only `error_type`;
- tool trigger activities: registered literal plus only `error_type`; and
- rootless transport: the two exact adapter events above.

### 6.7 Correct Task 1 RED/GREEN and audit coverage

Step 1/RED must include the dependency-boundary, crypto warning, tool registration/advertised-tools,
rootless JSON, environment-render, stdlib-binding, print-exemption, and Docker-info-exemption
regressions. The expected RED paragraph must name those failures; a `NameError` from a missing
`CONTEXT_FIELD_RULES` import is not an acceptable RED.

After `uv lock`, replace Task 1 GREEN with:

```bash
uv lock --check
uv run python scripts/audit_phase10_logging.py
uv run pytest \
  packages/observability/tests \
  packages/secrets/tests \
  packages/events/tests \
  packages/workflows/tests \
  apps/api/tests/test_webhooks_unit.py \
  services/agent_worker/tests \
  services/tool_worker/tests \
  services/event_worker/tests \
  services/sandbox_runner/tests \
  tests/test_worker_dependency_boundaries.py \
  tests/test_phase10_tool_worker_compose.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

This intentionally uses full Ruff/mypy because the root configuration includes the audit script
and every modified source tree. Do not restore the previous observability-only static gates.

### 6.8 Exact Task 1 staging manifest

Replace Task 1's free-form `git add` block with this exact indexed-array manifest and fail-closed
pattern:

```bash
task1_paths=(
  apps/api/src/jhin_api/main.py
  apps/api/src/jhin_api/webhooks/service.py
  compose.rootless.yaml
  compose.yaml
  packages/events/pyproject.toml
  packages/events/src/jhin_events/consumer.py
  packages/observability/src/jhin_observability/__init__.py
  packages/observability/src/jhin_observability/errors.py
  packages/observability/src/jhin_observability/events.py
  packages/observability/src/jhin_observability/logging.py
  packages/observability/src/jhin_observability/redaction.py
  packages/observability/tests/test_errors.py
  packages/observability/tests/test_log_audit.py
  packages/observability/tests/test_logging.py
  packages/secrets/pyproject.toml
  packages/secrets/src/jhin_secrets/crypto.py
  packages/secrets/tests/test_crypto.py
  packages/workflows/src/jhin_workflows/heartbeat/activities.py
  scripts/assert_phase10_tool_worker_compose.py
  scripts/audit_phase10_logging.py
  services/agent_worker/src/jhin_agent_worker/activities.py
  services/agent_worker/src/jhin_agent_worker/engineering_activities.py
  services/agent_worker/src/jhin_agent_worker/main.py
  services/agent_worker/src/jhin_agent_worker/projections.py
  services/agent_worker/src/jhin_agent_worker/reasoning.py
  services/agent_worker/src/jhin_agent_worker/resources.py
  services/agent_worker/src/jhin_agent_worker/settings.py
  services/agent_worker/src/jhin_agent_worker/trigger_activities.py
  services/event_worker/src/jhin_event_worker/main.py
  services/event_worker/src/jhin_event_worker/matcher.py
  services/event_worker/src/jhin_event_worker/normalizer.py
  services/event_worker/src/jhin_event_worker/processor.py
  services/event_worker/src/jhin_event_worker/settings.py
  services/sandbox_runner/src/jhin_sandbox_runner/jobs.py
  services/sandbox_runner/src/jhin_sandbox_runner/main.py
  services/sandbox_runner/src/jhin_sandbox_runner/rootless_transport.py
  services/sandbox_runner/src/jhin_sandbox_runner/settings.py
  services/sandbox_runner/tests/test_rootless_transport.py
  services/tool_worker/pyproject.toml
  services/tool_worker/src/jhin_tool_worker/activities.py
  services/tool_worker/src/jhin_tool_worker/main.py
  services/tool_worker/src/jhin_tool_worker/resources.py
  services/tool_worker/src/jhin_tool_worker/settings.py
  services/tool_worker/src/jhin_tool_worker/trigger_activities.py
  services/tool_worker/tests/test_advertised_tools.py
  services/tool_worker/tests/test_worker_registration.py
  services/workflow_worker/src/jhin_workflow_worker/main.py
  services/workflow_worker/src/jhin_workflow_worker/settings.py
  tests/test_phase10_tool_worker_compose.py
  tests/test_worker_dependency_boundaries.py
  uv.lock
)
test -z "$(git diff --cached --name-only)"
git add -- "${task1_paths[@]}"
expected_index="$(printf '%s\n' "${task1_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task1_paths[@]}"
git commit --only "${task1_paths[@]}" \
  -m "feat(observability): enforce safe JSON log schema"
test "$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)" = \
  "$expected_index"
test -z "$(git diff --cached --name-only)"
```

The revised `Files` block and this array must remain exact mirrors. A real implementation choice
that adds a compatibility test/file must first amend both the exhaustive File Map and Task 1
`Files`; it cannot be staged opportunistically.

### 6.9 Rebalance Task 6 after Task 1 owns JSON bootstrap

Task 6 still replaces Task 1's direct JSON bootstrap with the full optional OTel runtime, adds
Temporal interceptors, and updates tool-worker registration tests. Make these exact plan changes:

- remove `services/tool_worker/pyproject.toml` from Task 6 `Files` and staging; its observability
  dependency was committed in Task 1 and Task 6 has no further manifest edit;
- keep `tests/test_worker_dependency_boundaries.py` in Task 6, but change its inline instruction to
  retain Task 1's positive tool-observability and negative agent/model assertions, then add only the
  new workflows-observability assertion;
- keep `services/tool_worker/tests/test_worker_registration.py` in Task 6 because it gains runtime
  ordering and Temporal-interceptor assertions;
- keep event/workflow manifests in Task 6 because that task adds their secret-redaction/runtime
  dependencies, and keep `uv.lock` for those changes;
- keep event/workflow/sandbox settings in Task 6, but describe their change as extending the
  already-present Task 1 environment field through `ObservabilitySettings`, not introducing
  `app_env`; and
- state explicitly that the rootless adapter retains Task 1's direct JSON-v1 bootstrap and is not
  given an OTLP runtime or credentials in Task 6.

In Task 6 Step 5 replace “remove all service calls to the compatibility alias” with “replace every
Task 1 `configure_json_logging` call in the six ordinary services with
`initialize_observability`, then remove the compatibility alias; retain the rootless adapter's
direct JSON bootstrap.” Run `tests/test_worker_dependency_boundaries.py` in Task 6 GREEN even though
only its workflows assertion is new there.

### Task 2: Build the Optional No-Op/OTLP Bootstrap and Bounded Exporters

**Files:**
- Modify: `packages/observability/pyproject.toml`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/src/jhin_observability/bootstrap.py`
- Modify: `packages/observability/src/jhin_observability/config.py`
- Modify: `packages/observability/src/jhin_observability/context.py`
- Modify: `packages/observability/src/jhin_observability/exporters.py`
- Modify: `packages/observability/src/jhin_observability/metrics.py`
- Modify: `packages/observability/src/jhin_observability/registry.py`
- Modify: `packages/observability/tests/conftest.py`
- Modify: `packages/observability/tests/test_bootstrap.py`
- Modify: `packages/observability/tests/test_context.py`
- Modify: `packages/observability/tests/test_exporters.py`
- Modify: `packages/observability/tests/test_noop_metrics.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes the accepted Task 1 handoff and produces the exact Task 2 contract, subject, manifest, and gates below.

- [ ] **Step 1: Add dependencies and write failing config/no-op/context tests**

Add compatible bounded ranges:

```toml
dependencies = [
    "structlog>=25.1",
    "pydantic-settings>=2.7",
    "opentelemetry-api>=1.38,<2",
    "opentelemetry-sdk>=1.38,<2",
    "opentelemetry-exporter-otlp-proto-grpc>=1.38,<2",
]
```

Add `"packages/observability/tests"` to root `tool.pytest.ini_options.testpaths` so `uv run pytest` cannot omit this security boundary.

Then add:

```python
def test_empty_endpoint_installs_noop_telemetry_but_json_logging(
    capsys: CaptureFixture[str],
) -> None:
    runtime = initialize_observability(
        ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    )
    with runtime.tracer.start_as_current_span("test.noop") as span:
        assert span.is_recording() is False
    get_logger(__name__).info("api.started")
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1
    assert runtime.status().configured is False


@pytest.mark.parametrize(
    ("endpoint", "insecure", "ok"),
    [
        ("https://collector.example.test:4317", False, True),
        ("http://otel-collector:4317", True, True),
        ("http://collector.example.test:4317", False, False),
    ],
)
def test_otlp_transport_configuration_is_explicit(endpoint: str, insecure: bool, ok: bool) -> None:
    kwargs = dict(
        service_name="api",
        service_version="0.1.0",
        environment="test",
        otlp_endpoint=endpoint,
        otlp_insecure=insecure,
    )
    if ok:
        assert ObservabilityConfig(**kwargs).otlp_endpoint == endpoint
    else:
        with pytest.raises(ValueError, match="cleartext OTLP"):
            ObservabilityConfig(**kwargs)


def test_traceparent_is_validated_and_baggage_is_discarded() -> None:
    ctx = extract_trace_context(
        {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "vendor=value",
            "baggage": "workspace_id=attacker,metric_label=attacker",
        }
    )
    span_context = trace.get_current_span(ctx).get_span_context()
    assert format(span_context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert baggage.get_all(context=ctx) == {}


def test_bind_context_clears_all_values_after_exit() -> None:
    with bind_context(request_id="r", correlation_id="c", task_id="t", run_id="run"):
        assert structlog.contextvars.get_contextvars()["request_id"] == "r"
    assert structlog.contextvars.get_contextvars() == {}


def test_mixed_case_trace_carrier_is_rebuilt_with_canonical_keys() -> None:
    parent = extract_trace_context(
        {
            "TraceParent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "TraceState": "vendor=value",
            "BaGgAgE": "password=carrier-canary",
        }
    )
    token = attach(parent)
    try:
        output = inject_trace_headers(
            {
                "X-Safe": "kept",
                "TRACEPARENT": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
                "traceState": "attacker=value",
                "BAGGAGE": "secret=carrier-canary",
            }
        )
    finally:
        detach(token)
    lowered = [key.lower() for key in output]
    assert output["X-Safe"] == "kept"
    assert lowered.count("traceparent") == 1
    assert lowered.count("tracestate") <= 1
    assert "baggage" not in lowered
    assert output["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert "carrier-canary" not in json.dumps(output)


def test_initialize_is_idempotent_only_for_the_same_config() -> None:
    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    assert initialize_observability(config) is initialize_observability(config)
    with pytest.raises(ObservabilityConfigurationError, match="already initialized"):
        initialize_observability(replace(config, service_name="agent-worker"))


def test_noop_metrics_is_available_without_bootstrap_or_exporter_imports() -> None:
    metrics = noop_metrics()
    metrics.counter("model_requests_total").add(1, provider_type="openai", outcome="ok")
    metrics.histogram("agent_run_duration_seconds").record(1.25, outcome="completed")
    metrics.set_observable("connector_health", ())
    assert metrics.is_noop is True


def test_safe_span_requires_runtime_unless_caller_explicitly_supplies_noop() -> None:
    with pytest.raises(ObservabilityNotInitializedError):
        with safe_span("model.request"):
            pass
    with safe_span("model.request", tracer=noop_tracer()) as span:
        assert span.is_recording() is False


def test_single_span_registry_covers_exactly_every_registered_activity() -> None:
    activity_spans = {name for name in SPAN_NAMES if name.startswith("temporal.activity.")}
    assert activity_spans == {
        *(f"temporal.activity.{name}" for name in TEMPORAL_ACTIVITY_NAMES),
        "temporal.activity.other",
    }


def test_runtime_config_and_protected_health_status_are_public_and_exact() -> None:
    from dataclasses import fields

    from jhin_observability import (
        ObservabilityConfig,
        ObservabilityRuntime,
        TelemetryExporterStatus,
    )

    config = ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    runtime = initialize_observability(config)
    assert runtime.config is config
    assert isinstance(runtime, ObservabilityRuntime)
    assert [field.name for field in fields(TelemetryExporterStatus)] == [
        "configured",
        "last_success_at",
        "dropped_items",
        "last_error_code",
    ]
    assert runtime.status() == TelemetryExporterStatus(
        configured=False,
        last_success_at=None,
        dropped_items=0,
        last_error_code=None,
    )
```

- [ ] **Step 2: Run RED**

```bash
uv lock
uv run pytest packages/observability/tests/test_bootstrap.py \
  packages/observability/tests/test_context.py \
  packages/observability/tests/test_noop_metrics.py -q
```

Expected: FAIL because the config, bootstrap, and trace-only propagator do not exist.

- [ ] **Step 3: Implement validated settings, trace-only context, and no-op setup**

Implement Shared Interfaces exactly. Initialization is protected by one process-local lock: an equal frozen config returns the existing runtime, while a different config raises `ObservabilityConfigurationError("observability already initialized")` without echoing either config. `ObservabilityRuntime.shutdown()` is idempotent and clears the global only when it owns it, permitting a later test/app lifecycle to initialize afresh. A private `_reset_observability_for_test()` shuts down and clears the runtime in an autouse fixture under `packages/observability/tests/conftest.py`; it is not exported or called by product code. `ObservabilitySettings.observability_config` converts a blank endpoint to `None`, forwards the sampling settings, and accepts service-specific known-secret processors. `service_version(name)` returns `importlib.metadata.version(name)` and raises a safe startup configuration error if a packaged service has no distribution metadata; it never invents a version. `extract_trace_context` and `inject_trace_headers` use only `TraceContextTextMapPropagator`, never the global composite baggage propagator:

The protected-health handoff relies on concrete, stable dataclasses—not a duck-typed private
object. Define and export all three names from `jhin_observability.__init__`:

```python
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from opentelemetry.metrics import Meter
from opentelemetry.trace import Tracer
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.types import Processor

from jhin_observability.exporters import ExportDiagnostics
from jhin_observability.metrics import JhinMetrics


class ObservabilityConfigurationError(RuntimeError):
    """The process received an invalid or conflicting observability configuration."""


class ObservabilityNotInitializedError(RuntimeError):
    """A long-lived process attempted instrumentation before bootstrap."""


@dataclass(frozen=True)
class ObservabilityConfig:
    service_name: str
    service_version: str
    environment: str
    otlp_endpoint: str | None = None
    otlp_insecure: bool = False
    otlp_ca_file: Path | None = None
    otlp_client_certificate_file: Path | None = None
    otlp_client_key_file: Path | None = None
    trace_sampler: Literal["always_on", "always_off", "parentbased_traceidratio"] = (
        "parentbased_traceidratio"
    )
    trace_sample_ratio: float = 0.10
    span_queue_size: int = 2_048
    span_export_batch_size: int = 512
    export_timeout_millis: int = 5_000
    metric_export_interval_millis: int = 60_000
    extra_log_processors: tuple[Processor, ...] = ()

    def __post_init__(self) -> None:
        if not self.service_name or not self.service_version or not self.environment:
            raise ValueError("service name, version, and environment are required")
        if self.otlp_endpoint is not None:
            parsed = urlsplit(self.otlp_endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("OTLP endpoint must be an absolute HTTP(S) URL")
            local_cleartext = parsed.hostname in {"otel-collector", "localhost", "127.0.0.1", "::1"}
            if parsed.scheme == "http" and (not self.otlp_insecure or not local_cleartext):
                raise ValueError("cleartext OTLP is allowed only for local collectors")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("OTLP endpoint must not contain credentials, query, or fragment")
        certificate_pair = (
            self.otlp_client_certificate_file is not None,
            self.otlp_client_key_file is not None,
        )
        if certificate_pair[0] != certificate_pair[1]:
            raise ValueError("OTLP client certificate and key must be configured together")
        if not math.isfinite(self.trace_sample_ratio) or not 0 <= self.trace_sample_ratio <= 1:
            raise ValueError("trace sample ratio must be between zero and one")
        for name, value in (
            ("span_queue_size", self.span_queue_size),
            ("span_export_batch_size", self.span_export_batch_size),
            ("export_timeout_millis", self.export_timeout_millis),
            ("metric_export_interval_millis", self.metric_export_interval_millis),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.span_export_batch_size > self.span_queue_size:
            raise ValueError("span export batch size cannot exceed queue size")


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: Literal["dev", "test", "staging", "production"] = "dev"
    log_level: str = "INFO"
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_insecure: bool = False
    otel_exporter_otlp_certificate: Path | None = None
    otel_exporter_otlp_client_certificate: Path | None = None
    otel_exporter_otlp_client_key: Path | None = None
    otel_traces_sampler: Literal["always_on", "always_off", "parentbased_traceidratio"] = (
        "parentbased_traceidratio"
    )
    otel_traces_sampler_arg: float = 0.10
    otel_bsp_max_queue_size: int = 2_048
    otel_bsp_max_export_batch_size: int = 512
    otel_exporter_otlp_timeout_millis: int = 5_000
    otel_metric_export_interval_millis: int = 60_000

    def observability_config(
        self,
        *,
        service_name: str,
        service_version: str,
        extra_log_processors: tuple[Processor, ...] = (),
    ) -> ObservabilityConfig:
        endpoint = (self.otel_exporter_otlp_endpoint or "").strip() or None
        return ObservabilityConfig(
            service_name=service_name,
            service_version=service_version,
            environment=self.app_env,
            otlp_endpoint=endpoint,
            otlp_insecure=self.otel_exporter_otlp_insecure,
            otlp_ca_file=self.otel_exporter_otlp_certificate,
            otlp_client_certificate_file=self.otel_exporter_otlp_client_certificate,
            otlp_client_key_file=self.otel_exporter_otlp_client_key,
            trace_sampler=self.otel_traces_sampler,
            trace_sample_ratio=self.otel_traces_sampler_arg,
            span_queue_size=self.otel_bsp_max_queue_size,
            span_export_batch_size=self.otel_bsp_max_export_batch_size,
            export_timeout_millis=self.otel_exporter_otlp_timeout_millis,
            metric_export_interval_millis=self.otel_metric_export_interval_millis,
            extra_log_processors=extra_log_processors,
        )


@dataclass(frozen=True)
class TelemetryExporterStatus:
    configured: bool
    last_success_at: datetime | None
    dropped_items: int
    last_error_code: Literal["export_timeout", "export_failed"] | None


@dataclass
class ObservabilityRuntime:
    config: ObservabilityConfig
    tracer: Tracer
    meter: Meter
    metrics: JhinMetrics
    _diagnostics: ExportDiagnostics
    _owns_providers: bool
    _shutdown_callbacks: tuple[Callable[[int], None], ...] = ()
    _shutdown_lock: threading.Lock = field(default_factory=threading.Lock)
    _shutdown_complete: bool = False

    def status(self) -> TelemetryExporterStatus:
        snapshot = self._diagnostics.snapshot()
        return TelemetryExporterStatus(
            configured=self.config.otlp_endpoint is not None,
            last_success_at=snapshot.last_success_at,
            dropped_items=snapshot.dropped_items,
            last_error_code=snapshot.last_error_code,
        )

    def shutdown(self, timeout_millis: int = 5_000) -> None:
        if timeout_millis < 0:
            raise ValueError("timeout_millis must not be negative")
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True
        deadline = time.monotonic() + timeout_millis / 1_000
        if self._owns_providers:
            for callback in self._shutdown_callbacks:
                remaining = max(0, int((deadline - time.monotonic()) * 1_000))
                callback(remaining)
        _clear_runtime_if_owner(self)
```

`ObservabilityConfig.__post_init__` implements the endpoint/TLS/numeric validation specified
below. Each `_shutdown_callbacks` member is owned by the bounded exporter/provider adapter and
must honor its supplied remaining deadline; `test_shutdown_releases_blocked_exporter_within_deadline`
proves the contract. `_clear_runtime_if_owner` takes the bootstrap module lock and clears the global
only when identity matches `self`.

```python
TRACE_PROPAGATOR = TraceContextTextMapPropagator()
TRACE_CARRIER_KEYS = frozenset({"traceparent", "tracestate", "baggage"})


def extract_trace_context(headers: Mapping[str, str]) -> Context:
    carrier: dict[str, str] = {}
    for key, value in headers.items():
        normalized = key.lower()
        if normalized in {"traceparent", "tracestate"} and normalized not in carrier:
            carrier[normalized] = value
    return TRACE_PROPAGATOR.extract(carrier=carrier)


def inject_trace_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    preserved = {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() not in TRACE_CARRIER_KEYS
    }
    canonical: dict[str, str] = {}
    TRACE_PROPAGATOR.inject(carrier=canonical)
    return {**preserved, **canonical}
```

Create the no-op facade in `metrics.py` before `bootstrap.py` imports it:

```python
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


MetricName = Literal[
    "agent_runs_total",
    "agent_run_duration_seconds",
    "agent_run_failures_total",
    "model_requests_total",
    "model_tokens_total",
    "model_cost_estimate",
    "tool_calls_total",
    "tool_call_failures_total",
    "trigger_invocations_total",
    "trigger_failures_total",
    "sandbox_jobs_total",
    "sandbox_job_duration_seconds",
    "nats_consumer_lag",
    "temporal_activity_failures",
    "connector_health",
    "connector_connections",
]


@dataclass(frozen=True)
class Observation:
    value: int | float
    attributes: Mapping[str, str]


class BoundCounter(Protocol):
    def add(self, amount: int | float, **labels: str) -> None:
        """Record through the validated metric-label boundary."""


class BoundHistogram(Protocol):
    def record(self, amount: int | float, **labels: str) -> None:
        """Record through the validated metric-label boundary."""


class _NoopCounter:
    def add(self, amount: int | float, **labels: str) -> None:
        return None


class _NoopHistogram:
    def record(self, value: int | float, **labels: str) -> None:
        return None


@dataclass(frozen=True)
class JhinMetrics:
    _counter_getter: Callable[[MetricName], BoundCounter]
    _histogram_getter: Callable[[MetricName], BoundHistogram]
    _observable_setter: Callable[[MetricName, Sequence[Observation]], None]
    is_noop: bool = False

    def counter(self, name: MetricName) -> BoundCounter:
        return self._counter_getter(name)

    def histogram(self, name: MetricName) -> BoundHistogram:
        return self._histogram_getter(name)

    def set_observable(self, name: MetricName, values: Sequence[Observation]) -> None:
        self._observable_setter(name, values)


_NOOP_COUNTER = _NoopCounter()
_NOOP_HISTOGRAM = _NoopHistogram()
_NOOP_METRICS = JhinMetrics(
    _counter_getter=lambda _name: _NOOP_COUNTER,
    _histogram_getter=lambda _name: _NOOP_HISTOGRAM,
    _observable_setter=lambda _name, _values: None,
    is_noop=True,
)


def noop_metrics() -> JhinMetrics:
    return _NOOP_METRICS
```

`safe_span` imports `SpanName` and `SPAN_NAMES` from the single `registry.py` module created in this task; `temporal.py` imports `TEMPORAL_ACTIVITY_NAMES` from that same module. It accepts only those names and attributes from the exact safe-key set, applies Task 1 structural redaction, maps unsupported enum-like values to `other`, and never accepts arbitrary `**kwargs`:

```python
import math
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.noop import NoOpTracerProvider

from jhin_observability.bootstrap import get_runtime
from jhin_observability.errors import SafeError
from jhin_observability.registry import SPAN_NAMES, AttributeValue, SpanName

_NOOP_TRACER = NoOpTracerProvider().get_tracer("jhin-observability.package-noop")


def noop_tracer() -> Tracer:
    """Explicit tracer for package/seed/host code that has no process bootstrap."""
    return _NOOP_TRACER


SAFE_SPAN_ATTRIBUTE_KEYS = frozenset(
    {
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "http.response.status_class",
        "db.system",
        "db.operation",
        "db.table",
        "messaging.system",
        "jhin.stream",
        "jhin.consumer",
        "jhin.subject_family",
        "jhin.provider_type",
        "jhin.connector_type",
        "jhin.operation",
        "jhin.outcome",
        "jhin.latency_ms",
        "jhin.retry_count",
        "jhin.tool_family",
        "jhin.risk",
        "jhin.network_policy",
        "jhin.request_id",
        "jhin.correlation_id",
        "jhin.workspace_id",
        "jhin.task_id",
        "jhin.run_id",
        "jhin.job_id",
        "temporal.workflow_id",
        "temporal.run_id",
        "temporal.task_queue",
        "temporal.workflow_type",
        "temporal.activity_type",
        "temporal.attempt",
        "error.type",
        "error.code",
    }
)

SPAN_ID_ATTRIBUTE_KEYS = frozenset(
    {
        "jhin.request_id",
        "jhin.correlation_id",
        "jhin.workspace_id",
        "jhin.task_id",
        "jhin.run_id",
        "jhin.job_id",
        "temporal.workflow_id",
        "temporal.run_id",
    }
)
SPAN_NUMERIC_ATTRIBUTE_KEYS = frozenset(
    {
        "http.response.status_code",
        "jhin.latency_ms",
        "jhin.retry_count",
        "temporal.attempt",
    }
)
_SAFE_SPAN_STRING_RE = re.compile(r"^[A-Za-z0-9_./:{}*-]{1,200}$")
_SAFE_SPAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def normalize_span_attributes(
    attributes: Mapping[str, AttributeValue] | None,
) -> dict[str, AttributeValue]:
    output: dict[str, AttributeValue] = {}
    for key, value in (attributes or {}).items():
        if key not in SAFE_SPAN_ATTRIBUTE_KEYS:
            raise ValueError(f"unregistered span attribute key: {key}")
        if key in SPAN_ID_ATTRIBUTE_KEYS:
            if isinstance(value, str) and _SAFE_SPAN_ID_RE.fullmatch(value):
                output[key] = value
            continue
        if key in SPAN_NUMERIC_ATTRIBUTE_KEYS:
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and 0 <= value <= 1_000_000_000
            ):
                output[key] = value
            continue
        if isinstance(value, bool):
            output[key] = value
        elif isinstance(value, str):
            output[key] = (
                value if "://" not in value and _SAFE_SPAN_STRING_RE.fullmatch(value) else "other"
            )
    return output


@contextmanager
def bind_context(
    *,
    request_id: str | UUID | None = None,
    correlation_id: str | UUID | None = None,
    workspace_id: str | UUID | None = None,
    task_id: str | UUID | None = None,
    run_id: str | UUID | None = None,
) -> Iterator[None]:
    supplied = {
        "request_id": request_id,
        "correlation_id": correlation_id,
        "workspace_id": workspace_id,
        "task_id": task_id,
        "run_id": run_id,
    }
    values: dict[str, str] = {}
    for key, value in supplied.items():
        if value is None:
            continue
        rendered = str(value)
        if not _SAFE_SPAN_ID_RE.fullmatch(rendered):
            raise ValueError(f"invalid {key}")
        values[key] = rendered
    tokens = structlog.contextvars.bind_contextvars(**values)
    span = trace.get_current_span()
    for key, value in values.items():
        if span.is_recording():
            span.set_attribute(f"jhin.{key}", value)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


@contextmanager
def safe_span(
    name: SpanName,
    *,
    tracer: Tracer | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, AttributeValue] | None = None,
    context: Context | None = None,
) -> Iterator[Span]:
    if name not in SPAN_NAMES:
        raise ValueError("unregistered span name")
    selected_tracer = tracer if tracer is not None else get_runtime().tracer
    with selected_tracer.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=normalize_span_attributes(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


def record_span_error(span: Span, error: SafeError) -> None:
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("error.type", error.type)
    span.set_attribute("error.code", error.code.value)
```

`jhin.workspace_id`, job/workflow IDs, and other canonical identifiers are allowed only on traces/logs and are rejected by Task 3's independent metric-label validator. `safe_span` never accepts URL, hostname, statement, headers, or payload keys.

When `otlp_endpoint is None`, call `configure_json_logging`, construct tracers/meters from `NoOpTracerProvider`/`NoOpMeterProvider`, and do not import or instantiate an exporter. When configured, use an OTel `Resource` containing only `service.name`, `service.version`, and `deployment.environment.name`; choose `AlwaysOnSampler`, `AlwaysOffSampler`, or `ParentBased(TraceIdRatioBased(ratio))` from the closed config.

- [ ] **Step 4: Write the failing queue-saturation/nonblocking tests**

```python
def test_full_span_queue_only_increments_atomic_drop_counter_on_product_thread(
    capsys: CaptureFixture[str],
) -> None:
    exporter = ReleasableBlockingSpanExporter()
    diagnostics = ExportDiagnostics()
    processor = BoundedBatchSpanProcessor(
        exporter,
        diagnostics=diagnostics,
        max_queue_size=2,
        max_export_batch_size=1,
        export_timeout_millis=25,
    )
    started = time.perf_counter()
    for index in range(50):
        processor.on_end(readable_span(index))
    elapsed = time.perf_counter() - started
    assert elapsed < 0.050
    assert diagnostics.snapshot().dropped_items >= 47
    assert capsys.readouterr().out == ""
    exporter.release.set()
    assert diagnostics.drop_event_emitted.wait(timeout=1.0)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    dropped = [record for record in records if record["event"] == "telemetry.queue_dropped"]
    assert len(dropped) == 1
    assert dropped[0]["count"] >= 47
    assert dropped[0]["queue_capacity"] == 2


def test_export_failure_is_safe_and_does_not_raise_to_caller(capsys: CaptureFixture[str]) -> None:
    processor = BoundedBatchSpanProcessor(
        FailingSpanExporter(),
        diagnostics=ExportDiagnostics(),
        max_queue_size=4,
        max_export_batch_size=2,
        export_timeout_millis=25,
    )
    processor.on_end(readable_span(1))
    assert processor.force_flush(timeout_millis=100) is True
    status = processor.diagnostics.snapshot()
    assert status.last_error_code == "export_failed"
    assert "telemetry.export_failed" in capsys.readouterr().out


def test_force_flush_and_shutdown_obey_deadline_when_exporter_is_blocked() -> None:
    exporter = ReleasableBlockingSpanExporter()
    processor = BoundedBatchSpanProcessor(
        exporter,
        diagnostics=ExportDiagnostics(),
        max_queue_size=4,
        max_export_batch_size=1,
        export_timeout_millis=25,
    )
    processor.on_end(readable_span(1))
    assert exporter.entered.wait(timeout=1.0)
    started = time.monotonic()
    assert processor.force_flush(timeout_millis=30) is False
    processor.shutdown(timeout_millis=30)
    assert time.monotonic() - started < 0.100
    exporter.release.set()
```

- [ ] **Step 5: Implement finite exporters and configured runtime lifecycle**

`BoundedBatchSpanProcessor.on_end` must use `queue.Queue(maxsize=config.span_queue_size).put_nowait`. On `queue.Full`, it performs exactly `diagnostics.increment_dropped_atomic()` and returns; it does not acquire the exporter lock, format a log, call structlog, allocate an event dictionary, or notify a condition. Only the exporter thread calls `take_unreported_drop_delta()`: it may emit the first aggregated `telemetry.queue_dropped` record immediately, then emits no more than one aggregate per 30-second monotonic window, carrying all intervening drops in the next count. One daemon thread drains at most `config.span_export_batch_size`; it invokes the exporter with the configured timeout, converts failures to closed codes, and never rethrows into product code. `shutdown` and `force_flush` wait no longer than their explicit deadlines; a stuck exporter may leave the daemon thread alive but cannot hold process exit or product work.

```python
class ReleasableBlockingSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.entered.set()
        self.release.wait()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.release.set()


class ExportDiagnostics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dropped_total = 0
        self._dropped_reported = 0
        self._last_success_at: datetime | None = None
        self._last_error_code: Literal["export_timeout", "export_failed"] | None = None
        self.drop_event_emitted = threading.Event()

    def increment_dropped_atomic(self) -> None:
        with self._lock:
            self._dropped_total += 1

    def take_unreported_drop_delta(self) -> int:
        with self._lock:
            delta = self._dropped_total - self._dropped_reported
            self._dropped_reported = self._dropped_total
            return delta

    def record_success(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("export success timestamp must be timezone-aware")
        with self._lock:
            recovered = self._last_error_code is not None
            self._last_success_at = at.astimezone(UTC)
            self._last_error_code = None
            return recovered

    def record_failure(self, code: Literal["export_timeout", "export_failed"]) -> None:
        if code not in {"export_timeout", "export_failed"}:
            raise ValueError("unregistered telemetry export error code")
        with self._lock:
            self._last_error_code = code

    def snapshot(self) -> "ExportDiagnosticsSnapshot":
        with self._lock:
            return ExportDiagnosticsSnapshot(
                last_success_at=self._last_success_at,
                dropped_items=self._dropped_total,
                last_error_code=self._last_error_code,
            )
```

Emit stable events `telemetry.queue_dropped`, `telemetry.export_failed`, and `telemetry.export_recovered` with counts/codes only—never exporter exception text or endpoint.

Config validation requires the client certificate and private-key file together, reads those files only during bootstrap, and rejects unreadable files with a safe configuration error. Construct `grpc.ssl_channel_credentials(root_certificates=ca_bytes, private_key=key_bytes, certificate_chain=cert_bytes)` for TLS OTLP; pass no credential bytes to logs/status. Cleartext `http://` is accepted only with `otlp_insecure=True` and host exactly `otel-collector`, `localhost`, `127.0.0.1`, or `::1`; every other endpoint requires `https://`. Both OTLP exporters receive `timeout=config.export_timeout_millis / 1_000`.

Wrap `OTLPSpanExporter` and `OTLPMetricExporter` with `DiagnosticSpanExporter`/`DiagnosticMetricExporter` that update only these fields:

```python
@dataclass(frozen=True)
class ExportDiagnosticsSnapshot:
    last_success_at: datetime | None
    dropped_items: int
    last_error_code: Literal["export_timeout", "export_failed"] | None
```

Use `PeriodicExportingMetricReader` for metrics. Its periodic worker is the only metric export in flight; set `export_timeout_millis=5_000` and `export_interval_millis=60_000`, so recording a metric never performs network I/O and there is no unbounded metric export queue.

- [ ] **Step 6: Run focused tests and the package quality gates**

```bash
uv run pytest packages/observability/tests/test_bootstrap.py \
  packages/observability/tests/test_context.py \
  packages/observability/tests/test_exporters.py \
  packages/observability/tests/test_noop_metrics.py -q
uv run ruff check packages/observability
uv run mypy packages/observability/src packages/observability/tests
```

Expected: PASS, including a measured nonblocking saturated queue and fail-open exporter.

- [ ] **Step 7: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 2


The Task 2 preflight supersedes the earlier ledger PASS. No new Task 2 path is required, but the
existing Task 2 interfaces, tests, implementation rules, GREEN command, and staging block must be
corrected as follows before its brief is generated.

### 7.1 Make exporter diagnostics source-aware without changing protected health

Keep `TelemetryExporterStatus` public and exactly four fields in this exact order:

```python
configured: bool
last_success_at: datetime | None
dropped_items: int
last_error_code: Literal["export_timeout", "export_failed"] | None
```

Change only the internal diagnostics API:

```python
ExportSignal = Literal["traces", "metrics"]

ExportDiagnostics(active_signals: frozenset[ExportSignal])
ExportDiagnostics.record_success(source: ExportSignal, at: datetime) -> bool
ExportDiagnostics.record_failure(
    source: ExportSignal,
    code: Literal["export_timeout", "export_failed"],
) -> None
```

The implementation keeps one last-success timestamp and one current error per active signal.
Aggregate behavior is fixed:

- `last_error_code` is `export_timeout` if any active signal currently has that code, otherwise
  `export_failed` if any active signal is failed, otherwise `None`;
- aggregate `last_success_at` is `None` until every active signal has succeeded at least once, then
  the minimum of the active signals' most recent success timestamps, so one stale signal cannot be
  hidden by a fresh one;
- a success clears only its source's failure;
- `record_success(...)` returns `True` only for the one transition from “at least one active signal
  failed” to “no active signal failed”; and
- `telemetry.export_recovered` is emitted only on that final aggregate transition. Failure/recovery
  events retain their existing closed fields and never add endpoint, exception, or signal text.

Configured bootstrap constructs diagnostics with both `traces` and `metrics` active. No-export
bootstrap uses an empty active set and retains the exact unconfigured public status.

Add interleaving tests for trace-fail/metric-success, metric-fail/trace-success, both-fail/one-
recovers, and final recovery. Each intermediate snapshot must remain failed, and exactly one final
recovery event may be emitted.

### 7.2 Correct dependencies, imports, and exporter signatures

Add this direct dependency because production source imports `grpc`:

```toml
"grpcio>=1.63.2,<2",
```

Keep the existing bounded OTel 1.x dependencies. Import the no-op tracer provider through the
public API:

```python
from opentelemetry.trace import NoOpTracerProvider
```

Do not use `opentelemetry.trace.noop`. Import `UTC` from `datetime` wherever success timestamps call
`astimezone(UTC)`.

Export the Shared Interfaces from `jhin_observability.__init__` with their exact documented
signatures: config/settings/runtime/status, initialize/get-runtime, context helpers, no-op
tracer/metrics, safe span/error helpers, and registries. Repository consumers import those public
names; no consumer may depend on a private bootstrap singleton or diagnostic signal map.

`DiagnosticSpanExporter.export` and every test span exporter must implement the resolved public
signature `export(spans)` only. Do not pass `timeout_millis` to `SpanExporter.export`; configure the
underlying `OTLPSpanExporter(timeout=config.export_timeout_millis / 1_000)` once, then bound the
processor's wait/flush/shutdown deadline around its daemon worker. The metric wrapper must match the
resolved `MetricExporter.export` signature exactly, including only parameters present on that base
class. Both OTLP exporters receive their timeout at construction.

After `uv lock`, add an import/signature smoke test in `test_bootstrap.py` that imports the exact
resolved `NoOpTracerProvider`, `grpc`, span exporter, metric exporter, providers, and reader; uses
`inspect.signature` to prove the diagnostic wrappers are substitutable; and proves
`grpc.ssl_channel_credentials(...)` returns the credential type accepted by the OTLP constructors.
This test is a lock/API compatibility gate, not a version-string assertion copied from prose.

### 7.3 Make bootstrap and shutdown ownership transactional and race-safe

Construct `TracerProvider` and `MeterProvider` with `shutdown_on_exit=False`. No Task 2 provider,
reader, processor, or wrapper may register an atexit shutdown that can re-enter a stuck exporter
after the runtime deadline.

Use one bootstrap lock and an explicit runtime state. The required transitions are:

1. `initialize_observability` serializes construction, returns only a fully initialized RUNNING
   runtime for an equal config, and raises the closed configuration error for a different config.
2. Provider/reader/processor objects remain local and unpublished until all construction succeeds.
   On any intermediate exception, run the already-created bounded cleanup callbacks in reverse
   order and leave no global runtime, provider owner, reader worker, or processor worker.
3. The first `shutdown` caller changes the runtime from RUNNING to SHUTTING_DOWN and detaches the
   global owner under the same bootstrap lock **before** invoking callbacks. Thus a concurrent equal-
   config initialize can never receive a shutting-down runtime.
4. `_shutdown_complete` becomes true only after callbacks finish or their shared deadline expires.
   A second shutdown caller observes the state through a condition/event, never performs callbacks
   twice, and cannot race ahead of owner detachment. It may wait only through its own supplied
   deadline.
5. Finalization sets COMPLETE and notifies waiters even when one cleanup callback raises; cleanup
   exceptions are contained and never restore the detached runtime.

Add deterministic barrier-based tests for concurrent equal initialize, shutdown plus reinitialize,
two concurrent shutdown callers, callback failure, and injected failure after each partial bootstrap
stage. After releasing any test blocker, join the daemon worker and assert zero owned survivors.

### 7.4 Close the endpoint/TLS/numeric validation matrix

Define and test these reviewed ceilings:

```python
MAX_SPAN_QUEUE_SIZE = 65_536
MAX_SPAN_EXPORT_BATCH_SIZE = 8_192
MAX_EXPORT_TIMEOUT_MILLIS = 30_000
MAX_METRIC_EXPORT_INTERVAL_MILLIS = 300_000
```

The validation contract is exact:

- a missing endpoint rejects `otlp_insecure=True` and every CA/client credential path;
- HTTP is accepted only with `otlp_insecure=True`, a root path (`""` or `"/"`), and authority
  exactly one of `otel-collector:4317`, `localhost:4317`, `127.0.0.1:4317`, or `[::1]:4317`;
- HTTP rejects every CA/client credential path;
- HTTPS requires `otlp_insecure=False`, a root path, no credentials/query/fragment, and a valid
  host; TLS CA is optional and client certificate/key remain an all-or-none pair;
- HTTPS plus `otlp_insecure=True` is invalid rather than silently creating cleartext gRPC;
- non-root paths, including `/v1/traces` and `/v1/metrics`, are invalid for the shared gRPC endpoint;
- client certificate/key mismatch is invalid in every mode;
- sample ratio rejects `bool`, non-numeric values, NaN/infinity, and values outside `[0, 1]`;
- queue, batch, timeout, and interval reject `bool`, non-integers, nonpositive values, and values
  above their exact ceilings; batch must also be no larger than queue; and
- `ObservabilitySettings` uses before-validators that accept only canonical decimal environment
  strings (environment variables are necessarily strings), convert them once, and reject booleans,
  names such as `true`/`false`, exponent tricks where not explicitly allowed, and malformed values.
  Direct `ObservabilityConfig` construction still rejects `bool` and every non-numeric type.

TLS files are read only during transactional bootstrap. Missing, non-regular, or unreadable files
raise `ObservabilityConfigurationError` without including the path or underlying exception text,
and trigger partial-bootstrap cleanup.

Add every valid/invalid edge above to `test_bootstrap.py`, including blank endpoint after settings
normalization and both direct-dataclass and environment/settings construction.

### 7.5 Replace regex-permissive span attributes with per-key contracts

Retain the stable ID keys as the deliberate bounded exception: they accept only the existing
128-character identifier regex. Retain finite nonnegative numeric handling for the numeric keys.
Every other string key is normalized through a per-key closed registry; a generic 200-character
regex is not an authorization boundary.

Create one immutable `MappingProxyType` of `frozenset` values in `registry.py` with at least these
exact families (the set notation below is abbreviated plan notation, not permission to leave the
runtime registry mutable):

```python
SPAN_ATTRIBUTE_VALUES = {
    "http.request.method": {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"},
    "http.route": {"/api/:path*", "other"},
    "http.response.status_class": {"1xx", "2xx", "3xx", "4xx", "5xx", "other"},
    "db.system": {"postgresql", "other"},
    "db.operation": {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "ALTER", "DROP", "other"},
    "messaging.system": {"nats", "other"},
    "jhin.stream": {"INGRESS", "EVENTS", "DLQ", "other"},
    "jhin.consumer": {"event-worker", "event-worker-ingress", "other"},
    "jhin.subject_family": {"connector", "task", "run", "tool", "approval", "dlq", "other"},
    "jhin.provider_type": {"openai", "anthropic", "openrouter", "ollama", "openai_compatible", "other"},
    "jhin.connector_type": {"github", "linear", "vercel", "supabase", "cli", "other"},
    "jhin.operation": {"generate", "verify", "issue_comment_create", "execute_read", "execute_write", "submit", "cancel", "status", "cleanup", "other"},
    "jhin.outcome": {"ok", "accepted", "started", "completed", "failed", "cancelled", "timeout", "denied", "rejected", "duplicate", "execution_unknown", "healthy", "unhealthy", "other"},
    "jhin.tool_family": {"system", "organization", "github", "linear", "vercel", "supabase", "cli", "other"},
    "jhin.risk": {"read", "write", "elevated", "destructive", "other"},
    "jhin.network_policy": {"none", "internet", "other"},
    "temporal.task_queue": {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue", "other"},
}
```

Add `db.table` as a closed set of the reviewed SQLAlchemy table names plus `other`; add
`temporal.activity_type` from `TEMPORAL_ACTIVITY_NAMES` plus `other`; and add
`temporal.workflow_type` as the exact registered workflow names plus `other`. The central database
set must include the later protected-health `service_instance_heartbeat` handoff so the later plan
does not invent a second normalizer. `error.type` and `error.code` are not accepted from arbitrary
`safe_span(..., attributes=...)` mappings; `record_span_error(SafeError)` is their sole writer.

The exact additional registries are:

```python
DB_TABLE_VALUES = frozenset({
    "agent", "agent_capability_grant", "agent_relationship", "agent_run",
    "agent_team_membership", "approval", "audit_event", "connection", "message",
    "model_profile", "model_provider", "run_event", "sandbox_job", "secret",
    "service_instance_heartbeat", "task", "team", "tool_call", "trigger",
    "trigger_invocation", "user", "user_session", "webhook_delivery", "workspace",
    "workspace_membership", "other",
})
TEMPORAL_WORKFLOW_TYPE_VALUES = frozenset({
    "AdvertisedToolsCompatibilityWorkflow", "AgentTaskWorkflow",
    "ApprovalCompatibilityWorkflow", "CleanupCompatibilityWorkflow",
    "DelegatedTaskWorkflow", "EngineeringTicketWorkflow", "HeartbeatWorkflow",
    "SyncExternalCompatibilityWorkflow", "ToolStepCompatibilityWorkflow",
    "TriggeredTaskWorkflow", "other",
})
TEMPORAL_ACTIVITY_TYPE_VALUES = frozenset((*TEMPORAL_ACTIVITY_NAMES, "other"))
```

Before registry lookup, apply Task 1 structural redaction to the candidate key/value and reject or
normalize to `other` if it changes, truncates, redacts, contains a URL/host form, or is payload-
shaped. Unknown enum-like values become `other`; they are never preserved because they happen to
match a regex. Task 4 must normalize all registered API route templates to `/api/:path*` before
calling `safe_span`, preserving the already-frozen metric route contract.

Add parameterized tests proving that arbitrary alphanumeric/customer/secret strings, URL and host
forms, credential/payload-shaped values, unknown enums, oversized values, booleans in numeric
fields, and unregistered keys do not reach an exported span. Test the stable ID exception
separately and scan the complete serialized span attributes for every canary.

### 7.6 Use an exact resource and the configured metric timings

Construct the resource directly, not through default detectors:

```python
resource = Resource({
    "service.name": config.service_name,
    "service.version": config.service_version,
    "deployment.environment.name": config.environment,
})
```

Do not use `Resource.create(...)`. Both providers use that exact object, and arbitrary
`OTEL_RESOURCE_ATTRIBUTES` is ignored. A test sets process/env detector canaries and asserts the
exported resource key set is exactly those three keys and contains no canary.

Construct `PeriodicExportingMetricReader` with
`export_timeout_millis=config.export_timeout_millis` and
`export_interval_millis=config.metric_export_interval_millis`; remove the hard-coded
`5_000`/`60_000`. Assert those exact configured values through a constructor seam. The protected-
health freshness calculation must continue to use the same configured interval rather than a
separate default.

### 7.7 Make package tests own and restore logging/thread globals

Extend the package autouse fixture to snapshot and restore root handlers, root level/disabled
state, and the complete `structlog.get_config()` configuration in addition to resetting the
observability runtime. Restoration runs in `finally` even after a failing test.

Every exporter test that asserts JSON stdout calls `configure_json_logging(...)` itself; test order
must not provide logging configuration. Blocking-exporter tests release their blocker, wait for the
processor's stopped event, and join its daemon worker before fixture teardown. Assert no Task 2
worker remains alive after every normal, failure, timeout, or partial-bootstrap case.

Replace Task 2 GREEN with:

```bash
uv lock --check
uv run pytest packages/observability/tests -q
uv run ruff check packages/observability
uv run ruff format --check packages/observability
uv run mypy packages/observability/src packages/observability/tests
```

The whole directory is mandatory because Task 2 changes package `__init__` and installs an autouse
fixture over Task 1 logging/error/audit tests.

### 7.8 Carry the closed environment across later tasks

Task 1 now owns the source/Compose correction: agent/tool defaults are `dev`; event/workflow/sandbox
gain the closed field; every production Compose Python service receives
`APP_ENV=${APP_ENV:-production}`; and the rootless adapter receives/normalizes the same value. Task 2
must make `ObservabilitySettings.app_env` the exact `dev|test|staging|production` literal and must
not reintroduce `development`.

Task 6 extends those existing settings through `ObservabilitySettings`; it does not introduce a new
default. Task 10's rendered observability profile assertions must cover APP_ENV for every Python
product service and the rootless adapter, and Task 12's evidence render must preserve the same
environment. Add this as a cross-task ledger ruling so later tasks cannot regress production labels
to `dev` or create a service-local environment normalizer.

### 7.9 Make Task 2 staging exact

Replace Task 2's staging block with:

```bash
set -euo pipefail
task2_paths=(
  packages/observability/pyproject.toml
  packages/observability/src/jhin_observability/__init__.py
  packages/observability/src/jhin_observability/bootstrap.py
  packages/observability/src/jhin_observability/config.py
  packages/observability/src/jhin_observability/context.py
  packages/observability/src/jhin_observability/exporters.py
  packages/observability/src/jhin_observability/metrics.py
  packages/observability/src/jhin_observability/registry.py
  packages/observability/tests/conftest.py
  packages/observability/tests/test_bootstrap.py
  packages/observability/tests/test_context.py
  packages/observability/tests/test_exporters.py
  packages/observability/tests/test_noop_metrics.py
  pyproject.toml
  uv.lock
)
test -z "$(git diff --cached --name-only)"
git add -- "${task2_paths[@]}"
expected_index="$(printf '%s\n' "${task2_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task2_paths[@]}"
git commit --only "${task2_paths[@]}" \
  -m "feat(observability): add bounded optional OTLP bootstrap"
test "$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)" = \
  "$expected_index"
test -z "$(git diff --cached --name-only)"
```

No additional Task 2 path is authorized by these corrections. If implementation needs another
source or test file, amend the exhaustive File Map and Task 2 `Files` before touching it.

### Task 3: Define Every Required Metric and Enforce Cardinality

**Files:**
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/src/jhin_observability/bootstrap.py`
- Modify: `packages/observability/src/jhin_observability/metrics.py`
- Modify: `packages/observability/tests/test_bootstrap.py`
- Modify: `packages/observability/tests/test_metrics.py`

**Interfaces:**
- Consumes the accepted Task 2 handoff and produces the exact Task 3 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write the failing registry and forbidden-cardinality tests**

```python
EXPECTED = {
    "agent_runs_total": ("counter", "{run}", {"service", "outcome"}),
    "agent_run_duration_seconds": ("histogram", "s", {"outcome"}),
    "agent_run_failures_total": ("counter", "{failure}", {"failure_class"}),
    "model_requests_total": ("counter", "{request}", {"provider_type", "outcome"}),
    "model_tokens_total": ("counter", "{token}", {"provider_type", "direction"}),
    "model_cost_estimate": ("counter", "USD", {"provider_type"}),
    "tool_calls_total": ("counter", "{call}", {"tool_family", "risk", "outcome"}),
    "tool_call_failures_total": ("counter", "{failure}", {"tool_family", "failure_class"}),
    "trigger_invocations_total": ("counter", "{invocation}", {"connector_type", "outcome"}),
    "trigger_failures_total": ("counter", "{failure}", {"connector_type", "failure_class"}),
    "sandbox_jobs_total": ("counter", "{job}", {"outcome", "network_policy"}),
    "sandbox_job_duration_seconds": ("histogram", "s", {"outcome"}),
    "nats_consumer_lag": ("gauge", "{message}", {"stream", "consumer"}),
    "temporal_activity_failures": (
        "counter",
        "{failure}",
        {"task_queue", "activity", "failure_class"},
    ),
    "connector_health": ("gauge", "1", {"connector_type"}),
    "connector_connections": ("gauge", "{connection}", {"connector_type", "outcome"}),
}


def test_registry_exactly_matches_required_contract() -> None:
    assert instrument_contracts() == EXPECTED


@pytest.mark.parametrize(
    "forbidden",
    [
        "workspace_id",
        "user_id",
        "agent_id",
        "team_id",
        "task_id",
        "run_id",
        "event_id",
        "message_id",
        "connection_id",
        "approval_id",
        "tool_call_id",
        "sandbox_job_id",
        "request_id",
        "correlation_id",
        "trace_id",
        "url",
        "hostname",
        "repository",
        "project",
        "model_name",
    ],
)
def test_every_identifier_label_is_rejected(forbidden: str) -> None:
    metrics = test_metrics()
    with pytest.raises(MetricLabelError, match=forbidden):
        metrics.counter("agent_runs_total").add(
            1, service="agent-worker", outcome="completed", **{forbidden: "x"}
        )


def test_dynamic_values_map_to_other() -> None:
    labels = normalize_labels(
        "model_requests_total", {"provider_type": "attacker-created-provider", "outcome": "ok"}
    )
    assert labels == {"provider_type": "other", "outcome": "ok"}


def test_noop_facade_still_rejects_forbidden_labels() -> None:
    with pytest.raises(MetricLabelError, match="workspace_id"):
        noop_metrics().counter("agent_runs_total").add(
            1, service="agent-worker", outcome="completed", workspace_id="forbidden"
        )


def test_one_request_cannot_create_unbounded_series() -> None:
    metrics, reader = in_memory_metrics()
    for index in range(10_000):
        metrics.counter("model_requests_total").add(
            1, provider_type=f"dynamic-{index}", outcome=f"dynamic-{index}"
        )
    assert series_for(reader, "model_requests_total") == {
        (("outcome", "other"), ("provider_type", "other"))
    }
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/observability/tests/test_metrics.py -q
```

Expected: FAIL because the registry and label validator do not exist.

- [ ] **Step 3: Implement exact per-instrument contracts and closed value registries**

Define an immutable `MetricSpec` map with the exact names, kinds, units, and label subsets above. Define closed values:

```python
from jhin_observability.registry import TEMPORAL_ACTIVITY_NAMES


ALLOWED_METRIC_LABELS = frozenset(
    {
        "service",
        "environment",
        "outcome",
        "failure_class",
        "provider_type",
        "connector_type",
        "tool_family",
        "risk",
        "network_policy",
        "stream",
        "consumer",
        "task_queue",
        "activity",
        "http_method",
        "http_route",
        "http_status_class",
        "direction",
    }
)
FORBIDDEN_IDENTIFIER_LABELS = frozenset(
    {
        "workspace_id",
        "user_id",
        "agent_id",
        "team_id",
        "task_id",
        "run_id",
        "event_id",
        "message_id",
        "connection_id",
        "approval_id",
        "tool_call_id",
        "sandbox_job_id",
        "request_id",
        "correlation_id",
        "trace_id",
        "url",
        "hostname",
        "repository",
        "project",
        "model_name",
    }
)
LABEL_VALUES: dict[str, frozenset[str]] = {
    "service": frozenset(
        {
            "api",
            "agent-worker",
            "tool-worker",
            "event-worker",
            "workflow-worker",
            "sandbox-runner",
            "web",
        }
    ),
    "environment": frozenset({"dev", "test", "staging", "production"}),
    "outcome": frozenset(
        {
            "ok",
            "started",
            "completed",
            "failed",
            "cancelled",
            "timeout",
            "denied",
            "rejected",
            "duplicate",
            "execution_unknown",
            "healthy",
            "unhealthy",
            "other",
        }
    ),
    "failure_class": frozenset(
        {
            "authentication",
            "authorization",
            "validation",
            "rate_limit",
            "timeout",
            "transport",
            "dispatch",
            "target",
            "provider",
            "policy",
            "budget",
            "execution_unknown",
            "internal",
            "other",
        }
    ),
    "provider_type": frozenset(
        {"openai", "anthropic", "openrouter", "ollama", "openai_compatible", "other"}
    ),
    "connector_type": frozenset({"github", "linear", "vercel", "supabase", "cli", "other"}),
    "tool_family": frozenset(
        {"system", "organization", "github", "linear", "vercel", "supabase", "cli", "other"}
    ),
    "risk": frozenset({"read", "write", "elevated", "destructive", "other"}),
    "network_policy": frozenset({"none", "internet", "other"}),
    "stream": frozenset({"INGRESS", "EVENTS", "other"}),
    "consumer": frozenset({"event-worker-ingress", "event-worker", "other"}),
    "task_queue": frozenset(
        {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue", "other"}
    ),
    "activity": frozenset((*TEMPORAL_ACTIVITY_NAMES, "other")),
    "direction": frozenset({"input", "output", "cached"}),
    "http_method": frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"}),
    "http_status_class": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "other"}),
}
```

Implement the registry and facade with these concrete definitions in the same module (the
`EXPECTED` test above remains an independent literal contract):

```python
from __future__ import annotations

import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from opentelemetry.metrics import Meter, Observation as OTelObservation


InstrumentKind = Literal["counter", "histogram", "gauge"]


class MetricLabelError(ValueError):
    """A metric name, label key, or label set is outside the frozen registry."""


@dataclass(frozen=True)
class MetricSpec:
    kind: InstrumentKind
    unit: str
    labels: frozenset[str]


def _spec(kind: InstrumentKind, unit: str, *labels: str) -> MetricSpec:
    return MetricSpec(kind, unit, frozenset(labels))


METRIC_SPECS: Mapping[MetricName, MetricSpec] = MappingProxyType(
    {
        "agent_runs_total": _spec("counter", "{run}", "service", "outcome"),
        "agent_run_duration_seconds": _spec("histogram", "s", "outcome"),
        "agent_run_failures_total": _spec("counter", "{failure}", "failure_class"),
        "model_requests_total": _spec("counter", "{request}", "provider_type", "outcome"),
        "model_tokens_total": _spec("counter", "{token}", "provider_type", "direction"),
        "model_cost_estimate": _spec("counter", "USD", "provider_type"),
        "tool_calls_total": _spec("counter", "{call}", "tool_family", "risk", "outcome"),
        "tool_call_failures_total": _spec("counter", "{failure}", "tool_family", "failure_class"),
        "trigger_invocations_total": _spec("counter", "{invocation}", "connector_type", "outcome"),
        "trigger_failures_total": _spec("counter", "{failure}", "connector_type", "failure_class"),
        "sandbox_jobs_total": _spec("counter", "{job}", "outcome", "network_policy"),
        "sandbox_job_duration_seconds": _spec("histogram", "s", "outcome"),
        "nats_consumer_lag": _spec("gauge", "{message}", "stream", "consumer"),
        "temporal_activity_failures": _spec(
            "counter", "{failure}", "task_queue", "activity", "failure_class"
        ),
        "connector_health": _spec("gauge", "1", "connector_type"),
        "connector_connections": _spec("gauge", "{connection}", "connector_type", "outcome"),
    }
)
ROUTE_LABEL_VALUES = frozenset({"/api/:path*", "other"})


def instrument_contracts() -> dict[str, tuple[InstrumentKind, str, set[str]]]:
    return {name: (spec.kind, spec.unit, set(spec.labels)) for name, spec in METRIC_SPECS.items()}


def _metric_spec(name: MetricName) -> MetricSpec:
    try:
        return METRIC_SPECS[name]
    except KeyError as exc:
        raise MetricLabelError("unregistered metric name") from exc


def normalize_labels(name: MetricName, labels: Mapping[str, str]) -> dict[str, str]:
    supplied = set(labels)
    unknown = supplied - ALLOWED_METRIC_LABELS
    if unknown:
        raise MetricLabelError(f"forbidden metric label: {sorted(unknown)[0]}")
    spec = _metric_spec(name)
    if supplied != set(spec.labels):
        missing = sorted(spec.labels - supplied)
        extra = sorted(supplied - spec.labels)
        raise MetricLabelError(f"metric label contract mismatch; missing={missing}; extra={extra}")
    normalized: dict[str, str] = {}
    for key in sorted(spec.labels):
        value = labels[key]
        if not isinstance(value, str):
            raise MetricLabelError(f"metric label {key} must be a string")
        allowed = ROUTE_LABEL_VALUES if key == "http_route" else LABEL_VALUES[key]
        normalized[key] = value if value in allowed else "other"
    return normalized


def _finite_nonnegative(value: int | float, *, instrument: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{instrument} measurement must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"{instrument} measurement must be finite and non-negative")
    return numeric


class AddInstrument(Protocol):
    def add(self, amount: int | float, attributes: Mapping[str, str] | None = None) -> None:
        """Record one monotonic counter point."""


class RecordInstrument(Protocol):
    def record(self, amount: int | float, attributes: Mapping[str, str] | None = None) -> None:
        """Record one histogram point."""


@dataclass(frozen=True)
class _BoundCounter:
    name: MetricName
    instrument: AddInstrument

    def add(self, amount: int | float, **labels: str) -> None:
        numeric = _finite_nonnegative(amount, instrument=self.name)
        self.instrument.add(numeric, normalize_labels(self.name, labels))


@dataclass(frozen=True)
class _BoundHistogram:
    name: MetricName
    instrument: RecordInstrument

    def record(self, amount: int | float, **labels: str) -> None:
        numeric = _finite_nonnegative(amount, instrument=self.name)
        self.instrument.record(numeric, normalize_labels(self.name, labels))


class _ObservableState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[MetricName, tuple[Observation, ...]] = {}

    def replace(self, name: MetricName, values: Sequence[Observation]) -> None:
        spec = _metric_spec(name)
        if spec.kind != "gauge":
            raise MetricLabelError("set_observable requires a gauge")
        if len(values) > 128:
            raise MetricLabelError("observable metric exceeds 128 samples")
        normalized = tuple(
            Observation(
                _finite_nonnegative(value.value, instrument=name),
                normalize_labels(name, value.attributes),
            )
            for value in values
        )
        with self._lock:
            self._values[name] = normalized

    def observe(self, name: MetricName) -> list[OTelObservation]:
        with self._lock:
            values = self._values.get(name, ())
        return [OTelObservation(value.value, attributes=dict(value.attributes)) for value in values]


def build_jhin_metrics(meter: Meter) -> JhinMetrics:
    counters = {
        name: _BoundCounter(name, meter.create_counter(name, unit=spec.unit))
        for name, spec in METRIC_SPECS.items()
        if spec.kind == "counter"
    }
    histograms = {
        name: _BoundHistogram(name, meter.create_histogram(name, unit=spec.unit))
        for name, spec in METRIC_SPECS.items()
        if spec.kind == "histogram"
    }
    state = _ObservableState()
    for name, spec in METRIC_SPECS.items():
        if spec.kind == "gauge":
            meter.create_observable_gauge(
                name,
                callbacks=[lambda _options, selected=name: state.observe(selected)],
                unit=spec.unit,
            )

    def counter(name: MetricName) -> BoundCounter:
        if name not in counters:
            raise MetricLabelError("counter requested for non-counter metric")
        return counters[name]

    def histogram(name: MetricName) -> BoundHistogram:
        if name not in histograms:
            raise MetricLabelError("histogram requested for non-histogram metric")
        return histograms[name]

    return JhinMetrics(counter, histogram, state.replace)


@dataclass(frozen=True)
class _ValidatedNoopCounter:
    name: MetricName

    def add(self, amount: int | float, **labels: str) -> None:
        _finite_nonnegative(amount, instrument=self.name)
        normalize_labels(self.name, labels)


@dataclass(frozen=True)
class _ValidatedNoopHistogram:
    name: MetricName

    def record(self, amount: int | float, **labels: str) -> None:
        _finite_nonnegative(amount, instrument=self.name)
        normalize_labels(self.name, labels)


_VALIDATED_NOOP_COUNTERS = {
    name: _ValidatedNoopCounter(name)
    for name, spec in METRIC_SPECS.items()
    if spec.kind == "counter"
}
_VALIDATED_NOOP_HISTOGRAMS = {
    name: _ValidatedNoopHistogram(name)
    for name, spec in METRIC_SPECS.items()
    if spec.kind == "histogram"
}
_VALIDATED_NOOP_STATE = _ObservableState()


def _noop_counter(name: MetricName) -> BoundCounter:
    try:
        return _VALIDATED_NOOP_COUNTERS[name]
    except KeyError as exc:
        raise MetricLabelError("counter requested for non-counter metric") from exc


def _noop_histogram(name: MetricName) -> BoundHistogram:
    try:
        return _VALIDATED_NOOP_HISTOGRAMS[name]
    except KeyError as exc:
        raise MetricLabelError("histogram requested for non-histogram metric") from exc


_VALIDATED_NOOP_METRICS = JhinMetrics(
    _noop_counter,
    _noop_histogram,
    _VALIDATED_NOOP_STATE.replace,
    is_noop=True,
)


def noop_metrics() -> JhinMetrics:
    return _VALIDATED_NOOP_METRICS
```

No reflection or arbitrary instrument creation is permitted.

`http_route` values are accepted only from a frozen registry generated from FastAPI/Next route templates at service startup; unknown/unmatched paths map to `other`. No current required instrument uses HTTP labels, but the validator reserves the exact design-approved keys without permitting arbitrary route values.

HTTP method/route/status values are normalized separately by the API middleware and are not used by the required metric set. `normalize_labels` first rejects keys not in the global allowlist, then rejects keys not declared by that instrument, then requires every declared key exactly once. Dynamic values become `other`; never truncate a dynamic value into a new label.

Export `MetricName`, `Observation`, `instrument_contracts`, and
`FORBIDDEN_IDENTIFIER_LABELS` from `jhin_observability.__init__` for product code and the live
translation test; no test imports a private registry.

`JhinMetrics.set_observable` replaces one bounded tuple for a named gauge under a lock. Gauge callbacks return only the latest tuple, capped at 128 observations, so a polling bug cannot grow memory without bound.

- [ ] **Step 4: Run focused tests and verify no identifier reaches the exporter**

```bash
uv run pytest packages/observability/tests/test_metrics.py -q
uv run ruff check packages/observability/src/jhin_observability/metrics.py \
  packages/observability/tests/test_metrics.py
uv run mypy packages/observability/src/jhin_observability/metrics.py \
  packages/observability/tests/test_metrics.py
```

Expected: PASS and exactly the 16 required/explanatory instruments are registered.

- [ ] **Step 5: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 3


The Task 3 preflight supersedes the ledger's earlier PASS. Apply all of this section before
dispatching Task 3. Task 2 is still pending, so the single-authority `MetricName` correction
belongs in Task 2; Task 3 must consume it and must not create a second registry.

### 8.1 Wire the validated metrics facade into bootstrap

Add `packages/observability/tests/test_bootstrap.py` to Task 3's `Files` block,
RED/GREEN commands, and exact staging manifest.

In Task 3 Step 3, bind both bootstrap branches:

- The configured branch obtains the package meter from Task 2's resource-bound
  `MeterProvider`, calls `build_jhin_metrics(meter)` exactly once, and installs
  that exact returned object as `ObservabilityRuntime.metrics` before publishing the
  runtime. It may not leave Task 2's initial no-op facade installed.
- The endpoint-absent branch installs Task 3's validated `noop_metrics()` singleton,
  reports `is_noop is True`, and does not construct an SDK metric exporter, reader, or
  provider merely to obtain label validation.
- Task 2's transactional initialization/shutdown ownership still applies: no runtime containing
  the configured facade becomes visible until all providers, processors, and metric instruments
  are initialized, and reset/shutdown releases the provider that owns that meter.

Add bootstrap regressions using Task 2's in-memory/provider-construction seam:

1. A configured runtime exposes a non-noop facade, records a representative counter through that
   facade, and the point is visible through `InMemoryMetricReader`.
2. The configured metric resource has exactly
   `service.name`, `service.version`, and
   `deployment.environment.name` with the configured values; no default detector or
   process environment attribute is present.
3. A spy proves one builder call and object identity with `runtime.metrics`; shutdown and
   fixture reset release the owning provider and leave no Task 2 worker alive.
4. With no endpoint, `runtime.metrics is noop_metrics()`,
   `runtime.metrics.is_noop is True`, no provider seam is called, and an invalid label
   still raises `MetricLabelError`.

The focused RED includes both `test_metrics.py` and `test_bootstrap.py`. Missing
registry/validator/bootstrap wiring is the expected failure; a missing helper, bad import, or
fixture leak is not an acceptable RED.

### 8.2 Make the metric tests and implementation snippets executable

In `test_metrics.py`, define a lifecycle-owned fixture/helper named
`in_memory_metrics`, never `test_metrics`. It must:

- create an `InMemoryMetricReader` and SDK `MeterProvider`;
- call `build_jhin_metrics(provider.get_meter(...))`;
- yield the facade and reader;
- define `series_for(reader, name)` by traversing
  `reader.get_metrics_data()` rather than relying on an undefined test utility; and
- call `provider.shutdown()` in `finally` on success or failure.

Replace every planned `test_metrics()` call with that fixture/helper. Do not add a product
API whose name begins with `test_`. In the Step 3 implementation imports, add
`Protocol`, remove unused `cast`, and retain Task 2's
`Callable` import because `JhinMetrics` still uses it. The literal snippets,
Ruff, and mypy must all agree without collection-time or undefined-name failures.

### 8.3 Keep one `MetricName` authority

Amend Task 2's no-op-facade instruction and implementation snippet:

- `registry.py` is the only file that defines the complete
  `MetricName = Literal[...]` list.
- `metrics.py` imports `MetricName` from
  `jhin_observability.registry` and re-exports that exact object for compatibility; it
  does not import `Literal` solely to repeat the names.
- Package `__init__.py` re-exports the same canonical alias.
- Task 3 imports `MetricName` from `jhin_observability.registry` alongside
  `TEMPORAL_ACTIVITY_NAMES`.

Add this invariant regression:

```python
from typing import get_args

from jhin_observability import MetricName as PublicMetricName
from jhin_observability.metrics import MetricName as MetricsMetricName
from jhin_observability.registry import MetricName as RegistryMetricName


def test_metric_name_has_one_authority() -> None:
    assert PublicMetricName is RegistryMetricName
    assert MetricsMetricName is RegistryMetricName
    assert set(get_args(RegistryMetricName)) == set(instrument_contracts())
```

Because Task 2 already owns and stages `registry.py`, its 15-path manifest does not
change. Task 3 does not modify or stage `registry.py` unless Task 2 was actually committed
without this correction; that unexpected state requires amending Task 3 `Files` and
staging before implementation, not opportunistic staging.

### 8.4 Prove cardinality and validation for all sixteen instruments

Keep `EXPECTED` as an independent sixteen-entry literal. Replace the single-instrument
cardinality example with table-driven tests over every entry:

1. For every counter, histogram, and gauge, construct one valid complete baseline label set. For
   each label declared by that instrument, vary that one dimension over many distinct unregistered
   strings and prove the emitted/collected canonical attribute set contains at most the single
   `other` series for that varying dimension.
2. Exercise the public handle for its declared kind:
   `counter(...).add(...)`, `histogram(...).record(...)`, or
   `set_observable(...)` followed by collection. Do not infer runtime behavior merely
   from `instrument_contracts()`. Counters and histograms may record the dynamic values
   sequentially; gauges must replace and collect one dynamic observation at a time so this
   normalization proof does not conflict with the duplicate-in-one-replacement rejection below.
3. Across every instrument, parameterize every forbidden identifier key, every globally allowed
   key that is extra for that instrument, each missing required key, non-string labels, and the
   wrong requested instrument kind. Across counter, histogram, and observable paths, reject bool,
   negative, NaN, infinite, and non-numeric measurements. Recorder spies must prove every failure
   occurs before an SDK `add`, `record`, or callback-visible state change.
4. Run the same validation matrix against configured and validated-noop facades. No-op means no
   export, not weaker validation.
5. Directly prove the 128-observation boundary: 128 succeeds, 129 fails; a successful replacement
   removes every stale prior tuple; and any failed replacement preserves the complete prior tuple.

The independent contract equality still proves exact names/kinds/units/label subsets, while these
tests prove every actual public recording path goes through the validator.

### 8.5 Reject duplicate normalized observable identities atomically

Change `_ObservableState.replace` to normalize and validate the complete candidate
sequence before taking the lock. For each point, derive a canonical identity in stable spec-label
order, concretely:

```python
identity = tuple((key, normalized_attributes[key]) for key in sorted(spec.labels))
```

Track those identities for the candidate replacement. A repeated identity raises
`MetricLabelError("duplicate normalized observable identity")`; the error must not include
either raw caller value. Do not use generic last-write-wins. Aggregation is allowed only if a
future metric-specific contract explicitly defines it; no current Task 3 gauge does.

Only after the cap, every measurement, every label set, and uniqueness all pass may one lock
acquisition swap the immutable tuple. A failure at any point preserves the prior complete tuple.

Add configured and validated-noop regressions using `connector_health`: first install a
valid prior observation, then submit two distinct unknown connector values that both normalize to
`connector_type="other"`. Both facades must reject the duplicate. Collection through the
in-memory reader must show the configured prior tuple unchanged; exercise the validated-noop
facade's shared `_ObservableState` through a module-local state unit test to prove the
same atomic preservation without adding a public read API.

### 8.6 Replace Task 3 GREEN and make staging exact

Replace Step 4 with:

```bash
uv lock --check
uv run pytest packages/observability/tests -q
uv run ruff check packages/observability
uv run ruff format --check packages/observability
uv run mypy packages/observability/src packages/observability/tests
```

The whole package suite is mandatory because Task 3 changes bootstrap and public exports and must
rerun Task 1 logging and Task 2 exporter/lifecycle tests.

Replace Task 3 Step 5 with:

```bash
set -euo pipefail
task3_paths=(
  packages/observability/src/jhin_observability/__init__.py
  packages/observability/src/jhin_observability/bootstrap.py
  packages/observability/src/jhin_observability/metrics.py
  packages/observability/tests/test_bootstrap.py
  packages/observability/tests/test_metrics.py
)
test -z "$(git diff --cached --name-only)"
git add -- "${task3_paths[@]}"
expected_index="$(printf '%s\n' "${task3_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task3_paths[@]}"
git commit --only "${task3_paths[@]}" \
  -m "feat(observability): enforce telemetry metric cardinality"
test "$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)" = \
  "$expected_index"
test -z "$(git diff --cached --name-only)"
```

The revised Task 3 `Files` block and this exact five-path array must be mirrors. Any
additional implementation path requires a reviewed plan amendment before that path is touched.

### Task 4: Trace API Requests and Useful Database Operations Safely

**Files:**
- Modify: `apps/api/pyproject.toml`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/src/jhin_api/seed.py`
- Modify: `apps/api/src/jhin_api/settings.py`
- Modify: `apps/api/tests/test_health.py`
- Modify: `apps/api/tests/test_observability.py`
- Modify: `packages/db/pyproject.toml`
- Modify: `packages/db/src/jhin_db/engine.py`
- Modify: `packages/db/tests/test_observability.py`
- Modify: `packages/observability/pyproject.toml`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/src/jhin_observability/sqlalchemy.py`
- Modify: `packages/observability/tests/test_sqlalchemy.py`
- Modify: `tests/integration/test_phase2_api.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes the accepted Task 3 handoff and produces the exact Task 4 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing API trace/request-ID/baggage tests**

Use an in-memory span exporter and the existing app test client:

```python
async def test_api_uses_valid_parent_returns_request_id_and_discards_baggage(
    client: AsyncClient, spans: InMemorySpanExporter
) -> None:
    response = await client.get(
        "/api/v1/health",
        headers={
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "baggage": "workspace_id=foreign,metric_label=foreign",
        },
    )
    assert response.status_code == 200
    assert UUID(response.headers["X-Request-ID"])
    server = next(span for span in spans.get_finished_spans() if span.name == "http.server.request")
    assert format(server.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert server.attributes == {
        "http.request.method": "GET",
        "http.route": "/api/v1/health",
        "http.response.status_code": 200,
        "http.response.status_class": "2xx",
    }
    assert "foreign" not in json.dumps(server.attributes)


async def test_invalid_traceparent_creates_new_root(
    client: AsyncClient, spans: InMemorySpanExporter
) -> None:
    response = await client.get("/api/v1/health", headers={"traceparent": "attacker-value"})
    server = next(span for span in spans.get_finished_spans() if span.name == "http.server.request")
    assert response.status_code == 200
    assert server.parent is None


async def test_request_context_is_cleared_after_exception(
    app: FastAPI, client: AsyncClient, capsys: CaptureFixture[str]
) -> None:
    app.add_api_route("/_test/fail", lambda: 1 / 0)
    failed = await client.get("/_test/fail")
    assert failed.status_code == 500
    assert UUID(failed.headers["X-Request-ID"])
    succeeded = await client.get("/api/v1/health")
    assert succeeded.status_code == 200
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    finished = [record for record in records if record["event"] == "api.request_finished"]
    assert [record["request_id"] for record in finished[-2:]] == [
        failed.headers["X-Request-ID"],
        succeeded.headers["X-Request-ID"],
    ]
    assert finished[-2]["request_id"] != finished[-1]["request_id"]


def test_import_and_app_factory_do_not_initialize_process_global_runtime() -> None:
    importlib.reload(jhin_api.main)
    app = jhin_api.main.create_app(test_settings())
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    assert not hasattr(app.state, "observability")


@pytest.mark.asyncio
async def test_lifespan_owns_exactly_one_runtime() -> None:
    app = create_app(test_settings())
    async with app.router.lifespan_context(app):
        runtime = app.state.observability
        assert get_runtime() is runtime
        assert app.state.engine is not None
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
```

Assert that raw path parameters and query strings never become `http.route`, and unmatched routes normalize to `other`.

- [ ] **Step 2: Run API RED**

```bash
uv run pytest apps/api/tests/test_observability.py -q
```

Expected: FAIL because the API still uses logging-only bootstrap and has no server span.

- [ ] **Step 3: Install the bootstrap before FastAPI resources and implement middleware**

Change `Settings` to extend `ObservabilitySettings`. `create_app()` constructs no runtime, secret crypto, engine, NATS client, or Temporal client. The first executable statement in the lifespan initializes a runtime for that app, and the same lifespan shuts it down after every owned resource:

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = initialize_observability(
            settings.observability_config(
                service_name="api",
                service_version=__version__,
                extra_log_processors=(redact_event_dict,),
            )
        )
        app.state.observability = runtime
        try:
            app.state.secret_crypto = _load_secret_crypto()
            app.state.engine = create_engine(
                settings.database_url, trace_sql=True, tracer=runtime.tracer
            )
            app.state.session_factory = create_session_factory(app.state.engine)
            app.state.temporal_client = None
            app.state.temporal_connect_lock = asyncio.Lock()
            app.state.nats_client = None
            app.state.nats_connect_lock = asyncio.Lock()
            logger.info("api.started")
            yield
        finally:
            try:
                nats = getattr(app.state, "nats_client", None)
                if nats is not None and not nats.is_closed:
                    await nats.close()
            finally:
                try:
                    engine = getattr(app.state, "engine", None)
                    if engine is not None:
                        await engine.dispose()
                finally:
                    logger.info("api.stopped")
                    runtime.shutdown(timeout_millis=5_000)

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.login_limiter = LoginRateLimiter(
        settings.login_max_attempts, settings.login_window_seconds
    )
```

There is no module-global runtime and no `initialize_observability(...)` call at module import or
app-factory time. Export `normalize_span_attributes` from `jhin_observability.__init__`, add these
imports to `main.py`, and replace `request_id_middleware` with:

```python
from fastapi.routing import APIRoute
from opentelemetry.trace import Span, SpanKind
from starlette.responses import JSONResponse

from jhin_observability import (
    SafeErrorCode,
    bind_context,
    extract_trace_context,
    normalize_span_attributes,
    record_span_error,
    safe_error,
    safe_span,
)


def normalize_http_method(method: str) -> str:
    normalized = method.upper()
    return (
        normalized
        if normalized in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
        else "other"
    )


def normalize_http_route(request: Request) -> str:
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return "other"
    template = route.path
    return template if 1 <= len(template) <= 200 and template.startswith("/") else "other"


def set_http_span_result(span: Span, *, method: str, route: str, status_code: int) -> None:
    status = status_code if 100 <= status_code <= 599 else 500
    for key, value in normalize_span_attributes(
        {
            "http.request.method": normalize_http_method(method),
            "http.route": route,
            "http.response.status_code": status,
            "http.response.status_class": f"{status // 100}xx",
        }
    ).items():
        span.set_attribute(key, value)


@app.middleware("http")
async def observability_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = new_uuid7()
    request.state.request_id = request_id
    parent = extract_trace_context(request.headers)
    with (
        bind_context(request_id=request_id),
        safe_span("http.server.request", kind=SpanKind.SERVER, context=parent) as span,
    ):
        response: Response | None = None
        try:
            response = await call_next(request)
        except Exception as exc:
            record_span_error(span, safe_error(exc, code=SafeErrorCode.INTERNAL_ERROR))
            logger.exception("api.request_failed", error_code="internal_error")
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal server error"},
            )
        finally:
            normalized_route = normalize_http_route(request)
            set_http_span_result(
                span,
                method=request.method,
                route=normalized_route,
                status_code=response.status_code if response is not None else 500,
            )
        assert response is not None
        response.headers["X-Request-ID"] = str(request_id)
        logger.info(
            "api.request_finished",
            http_method=normalize_http_method(request.method),
            http_route=normalized_route,
            http_status_class=f"{response.status_code // 100}xx",
        )
        return response
```

Do not log client IP, user agent, URL, query, body, headers, cookies, or exception message. `APIRoute.path` is the registered template and therefore bounded; cap it at 200 characters and normalize methods/status classes.

- [ ] **Step 4: Write failing statement-free SQL span tests**

```python
@pytest.mark.asyncio
async def test_sql_span_has_operation_and_known_table_but_no_statement_or_dsn(
    spans: InMemorySpanExporter,
) -> None:
    dsn_canary = "postgresql+asyncpg://secret-user:secret-pass@db-canary/jhin"
    engine = create_test_engine_with_visible_dsn(dsn_canary)
    async with engine.connect() as connection:
        await connection.execute(
            text("SELECT password FROM secret_canary WHERE token=:token"), {"token": "bind-canary"}
        )
    rendered = json.dumps([dict(span.attributes or {}) for span in spans.get_finished_spans()])
    assert "db.operation" in rendered
    assert "SELECT password" not in rendered
    assert "secret-user" not in rendered and "secret-pass" not in rendered
    assert "db-canary" not in rendered and "bind-canary" not in rendered


def test_unknown_table_is_other() -> None:
    assert normalized_sql_metadata("SELECT * FROM attacker_supplied") == {
        "db.system": "postgresql",
        "db.operation": "SELECT",
        "db.table": "other",
    }


@pytest.mark.asyncio
async def test_database_package_is_safe_before_observability_bootstrap() -> None:
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    engine = create_engine("sqlite+aiosqlite:///:memory:", trace_sql=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def test_seed_explicitly_selects_package_noop_tracer_before_bootstrap() -> None:
    tree = ast.parse((REPO_ROOT / "apps/api/src/jhin_api/seed.py").read_text())
    create_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "create_engine"
    ]
    assert len(create_calls) == 1
    tracer = next(keyword for keyword in create_calls[0].keywords if keyword.arg == "tracer")
    assert ast.unparse(tracer.value) == "noop_tracer()"
```

Run database RED:

```bash
uv run pytest packages/observability/tests/test_sqlalchemy.py \
  packages/db/tests/test_observability.py -q
```

Expected: FAIL because statement-free SQL tracing does not exist.

- [ ] **Step 5: Implement SQLAlchemy hooks without OTel auto-instrumentation**

Add `jhin-observability` to `packages/db`. Do not add `opentelemetry-instrumentation-sqlalchemy`, because its default statement capture violates the spec. Implement `install_sqlalchemy_tracing(sync_engine, known_tables)` with SQLAlchemy `before_cursor_execute`, `after_cursor_execute`, and `handle_error` listeners. Parse only the leading operation from `SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP` and a table token after `FROM|INTO|UPDATE|TABLE`; emit the table only when it is in `Base.metadata.tables`, otherwise `other`. The raw statement and parameters are used only in the callback and are never attached, logged, returned, or stored.

```python
def create_engine(
    database_url: str,
    *,
    echo: bool = False,
    trace_sql: bool = True,
    tracer: Tracer | None = None,
) -> AsyncEngine:
    engine = create_async_engine(database_url, echo=echo, pool_pre_ping=True)
    if trace_sql:
        install_sqlalchemy_tracing(
            engine.sync_engine,
            known_tables=frozenset(Base.metadata.tables),
            tracer=tracer if tracer is not None else noop_tracer(),
        )
    return engine
```

`install_sqlalchemy_tracing(sync_engine, known_tables, *, tracer: Tracer)` creates
`safe_span("db.operation", tracer=tracer, kind=SpanKind.CLIENT, attributes=metadata)` and stores only
the context-manager handle on SQLAlchemy's execution context. `handle_error` records
`SafeErrorCode.INTERNAL_ERROR` without recording exception text. The API and all long-lived workers
pass their initialized `runtime.tracer`; the one-shot seed command imports `noop_tracer` and calls
`create_engine(database_url, tracer=noop_tracer())`. Thus package and seed callers remain usable
before bootstrap, while no service can silently fall back to no-op tracing.

- [ ] **Step 6: Run focused and affected suites**

```bash
uv lock
uv run pytest packages/observability/tests/test_sqlalchemy.py \
  packages/db/tests/test_observability.py apps/api/tests/test_observability.py \
  apps/api/tests/test_health.py -q
uv run ruff check packages/observability packages/db apps/api/src/jhin_api/main.py \
  apps/api/src/jhin_api/settings.py apps/api/tests/test_observability.py
uv run mypy packages/observability/src packages/db/src apps/api/src
```

Expected: PASS; no test span contains SQL or any DSN component.

- [ ] **Step 7: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 4


The Task 4 preflight is binding and supersedes the existing NO-GO ledger sentence. Preserve every
Task 2 route/resource/redaction contract and every Task 3 metric contract while applying all of
this section before Task 4 is dispatched.

### 9.1 Correct the exhaustive File Map, dependencies, and ownership

Add these paths to the global File Map and Task 4 `Files`:

```text
apps/api/pyproject.toml
apps/api/tests/test_health.py
tests/integration/test_phase2_api.py
```

Remove `tests/integration/test_seed.py` from Task 4 and from its otherwise-unused global
File Map entry. The seed test continues importing the Phase 2 fixture and remains byte-for-byte
unchanged; Task 4 repairs the fixture at its actual owner,
`tests/integration/test_phase2_api.py`.

Make Task 4's `Files` block the exact 15 paths in section 9.8. Bind these direct
dependencies and regenerate `uv.lock`:

- `packages/observability/pyproject.toml` adds
  `sqlalchemy>=2.0.36,<3` because production
  `jhin_observability.sqlalchemy` imports SQLAlchemy.
- `packages/db/pyproject.toml` adds `jhin-observability` with its workspace
  source and `opentelemetry-api>=1.38,<2` because
  `jhin_db.engine` imports both the shared hook and `Tracer`.
- `apps/api/pyproject.toml` adds
  `opentelemetry-api>=1.38,<2`. The pure-ASGI implementation below imports
  `starlette.types` and `starlette.responses` directly, so also declare
  `starlette>=1.6,<2` rather than relying on FastAPI's transitive dependency.

Do not add SQLAlchemy auto-instrumentation. Its statement-capture defaults violate this task's
privacy boundary.

### 9.2 Replace decorator middleware with one pure-ASGI owner

Delete the `@app.middleware("http")` implementation and install one
`HttpObservabilityMiddleware` pure-ASGI class as the outer request middleware. Its
`__call__(scope, receive, send)` contract is exact:

1. If `scope["type"] != "http"`, call the downstream app unchanged and do no telemetry
   or request-ID work.
2. Create one UUIDv7 request ID, store that UUID in
   `scope.setdefault("state", {})["request_id"]`, extract only W3C trace context from
   the request headers, and obtain the tracer from
   `scope["app"].state.observability.tracer`. Open exactly one
   `safe_span("http.server.request", tracer=that_exact_tracer,
   kind=SpanKind.SERVER, context=parent)` inside
   `bind_context(request_id=request_id)`. Never call
   `get_runtime()` from middleware.
3. Wrap `send`. On the first `http.response.start`, capture its status, remove
   every downstream header whose lowercased name is `x-request-id`, append exactly one
   ASCII `X-Request-ID` containing the generated UUID, and forward the copied message.
   Forward body/trailer messages without inspecting or retaining their payloads.
4. Keep both context managers open while awaiting the complete downstream ASGI application. They
   close only after every response-body send and downstream background action has returned, or
   after an exception/cancellation has unwound.
5. If an ordinary exception occurs before response start, call
   `record_span_error(span, safe_error(exc,
   code=SafeErrorCode.INTERNAL_ERROR))`, emit the registered safe failure log, and run a
   generic `JSONResponse(status_code=500,
   content={"detail": "Internal server error"})` through the wrapped sender. Do not serialize
   or log the exception message.
6. If an ordinary exception occurs after response start, record/log the same closed safe error and
   re-raise the original exception; do not attempt a second response. Cancellation and any send
   failure likewise propagate after `finally` cleanup.
7. In one finalization path, resolve `scope.get("route")` only after downstream routing
   has run, normalize method/route/status/status-class, set the four server-span attributes once,
   and emit exactly one `api.request_finished` record using the same bounded values.
   The safe span must continue to disable automatic OTel exception events/descriptions; only
   `record_span_error(SafeError)` writes error metadata.

This middleware owns no response body, header, query, URL, client address, user agent, cookie,
exception, or traceback value. All success, ordinary failure, streaming failure, and cancellation
paths detach both OTel and structlog request context.

Add direct-ASGI and HTTPX regressions for:

- ordinary 2xx and 404 responses;
- a downstream duplicate `X-Request-ID` being replaced by exactly one canonical header;
- an exception before response start producing one generic JSON 500;
- an exception after response start producing no second
  `http.response.start` and re-raising;
- a streaming response whose iterator observes the active server span and request ID on every
  chunk, while the exporter remains empty until the final chunk completes;
- a streaming iterator failure with only safe error type/code, no OTel exception event or status
  description, exactly one span end, and no context leak; and
- cancellation at each of the pre-start and body-send boundaries.

### 9.3 Collapse every API template to the Task 2 route registry

Replace the old concrete-template normalizer with:

```python
def normalize_http_route(scope: Scope) -> str:
    route = scope.get("route")
    if not isinstance(route, APIRoute):
        return "other"
    template = route.path
    if not isinstance(template, str) or not 1 <= len(template) <= 200:
        return "other"
    return "/api/:path*" if template.startswith("/api/") else "other"
```

No registered concrete template is exported. Both `http.route` on the span and
`http_route` in `api.request_finished` use that exact result. Change the
health-span expectation from `/api/v1/health` to `/api/:path*`.

Add one table-driven test over every registered `APIRoute`, plus unmatched, oversized,
invalid, and non-API routes. Add path-parameter, query-string, header, baggage, and exception-message
canaries. For every case, scan the complete JSON log objects and complete serialized span
name/resource/attributes/events/status, not a selected attribute subset. The only route values
permitted anywhere are `/api/:path*` and `other`.

Inside a request, a probe route must assert extracted baggage is empty. After every request, assert
`structlog.contextvars.get_contextvars() == {}` and that there is no current recording
span. Different successive request IDs alone are not a leak test.

### 9.4 Define real, function-scoped API fixtures

Define `test_settings`, `app`, `client`, and
`spans` locally in `apps/api/tests/test_observability.py`. Do not add an
API-wide autouse runtime fixture.

Binding fixture behavior:

- `test_settings()` returns deterministic
  `Settings(app_env="test", otel_exporter_otlp_endpoint=None,
  otel_exporter_otlp_insecure=False, ...)` values, including a local SQLite URL and fixed log
  level, and never inherits a live OTLP endpoint.
- A function-scoped `TracerProvider` with
  `SimpleSpanProcessor` and `InMemorySpanExporter` owns the test tracer. Its
  resource has exactly the three Task 2 keys with test values. The fixture shuts down the provider
  and exporter in `finally` even after assertion failure.
- Before monkeypatching, retain the real package `safe_span`. Patch only
  `jhin_api.main.safe_span` with a narrow wrapper that records the caller-supplied
  `tracer`, asserts it is not omitted, and delegates to the real helper with the
  function-scoped test tracer. After each request assert the recorded argument is exactly
  `app.state.observability.tracer`. Do not mutate Task 2's private bootstrap singleton.
- Patch only external resource seams needed to keep the test deterministic; do not bypass lifespan
  or middleware.
- Enter `AsyncClient(ASGITransport(app=app), ...)` inside
  `async with app.router.lifespan_context(app)`. On exit, prove the exact app runtime was
  shut down, `get_runtime()` raises
  `ObservabilityNotInitializedError`, the function-scoped provider/exporter is stopped,
  and no context remains.

Use `raise_app_exceptions=False` only for the pre-start generic-500 HTTP test. Use a
direct ASGI sender recorder for post-start streaming failure so the test can prove the exact
message sequence and propagated exception.

The exception test uses a unique message canary and asserts it is absent from response bytes, JSON
logs, span/resource serialization, events, status description, and stderr. Its server span contains
only the four bounded HTTP attributes plus safe `error.type` and
`error.code`.

### 9.5 Replace the SQL RED with local executable proofs

Remove `create_test_engine_with_visible_dsn` and every network/DNS-dependent SQL test.
Define lifecycle-owned tracer/exporter fixtures in the SQL test modules and use the real
`jhin_db.create_engine` with `sqlite+aiosqlite:///:memory:`:

1. Create a minimal `secret` table, clear all setup spans, and execute a parameterized
   `SELECT` containing distinct SQL-token and bind-value canaries. Assert exactly one
   child `db.operation` span with exactly
   `db.system="postgresql"`, `db.operation="SELECT"`, and
   `db.table="secret"`. Scan the complete serialized span, logs, stdout, and stderr for
   both canaries.
2. In a separate unit test, patch `create_async_engine` and the listener-install seam.
   Assert the exact database URL is passed only as the first SQLAlchemy constructor argument and is
   never passed to span metadata, listener state, or a logger. This test makes no connection.
3. Execute/query an unregistered table name and assert `db.table="other"`; never call that
   case a known-table proof.
4. In the seed AST test define
   `REPO_ROOT = Path(__file__).resolve().parents[3]`, prove exactly one
   `create_engine` call exists in `seed.py`, and prove its explicit tracer is
   exactly `noop_tracer()`.

`normalized_sql_metadata` accepts a statement only when
`isinstance(statement, str)`. It inspects at most a fixed 4,096-character prefix, parses
only the closed leading operation/table grammar, uppercases the operation, lowercases the table,
and intersects the table with the supplied immutable known-table set and Task 2's
`DB_TABLE_VALUES`. Unknown/malformed values become `other`. It never invokes
`str()`, `repr()`, formatting, or regex over an arbitrary object. Add an object
whose `__str__` raises/contains a canary and prove it is never called.

### 9.6 Make the SQL listener atomic, fail-open, and echo-free

Use exactly
`_SQL_STATE_ATTR = "_jhin_observability_sql_span_state"` as the namespaced
execution-context attribute. Its value contains only:

```python
@dataclass
class _SQLSpanState:
    manager: AbstractContextManager[Span]
    span: Span
    closed: bool = False
```

The state may not retain statement, parameters, URL, engine, exception, or traceback. The
`before_cursor_execute` listener computes closed metadata, enters
`safe_span("db.operation", tracer=tracer, kind=SpanKind.CLIENT,
attributes=metadata)`, and publishes the state only after enter succeeds. Any instrumentation
failure is contained and the database call proceeds without state.

Both success and error paths call one helper that removes the namespaced attribute before doing
anything else and flips `closed` before record/end work. Missing
`execution_context` or state is a no-op. Success exits the stored manager exactly once
with `(None, None, None)`. Error first calls
`record_span_error(stored_span,
safe_error(original_exception, code=SafeErrorCode.INTERNAL_ERROR))` and then also exits with
`(None, None, None)`; the raw SQLAlchemy exception is never forwarded to OTel. A listener,
tracer, attribute, error-recording, or end failure is swallowed and may neither fail a successful
query nor replace/mask the original database exception. `handle_error` always returns
`None`.

Add tests for success, database error, a tracer/manager that fails at start/record/end, a successful
query after an error, nested and concurrent async executions, correct outer-span parentage, one and
only one end, missing execution context, no current-span leak, no raw exception event/status
description/text, and zero retained raw SQL/bind/URL values.

Remove the public `echo` parameter from `jhin_db.create_engine`. The wrapper
always calls `create_async_engine(database_url, echo=False,
pool_pre_ping=True)`. Assert `echo` is absent from the public signature, an attempted
`echo=True` call raises before SQLAlchemy construction, and successful/error SQL canaries
never appear on stdout or stderr. SQL echo cannot be enabled through the Jhin API.

Correct Task 4's Produces sentence: it installs a safe statement-free package hook, wires the API,
and keeps seed explicitly no-op. It does not yet claim that every long-lived service passes a
configured tracer.

### 9.7 Repair API lifespan and bind Task 5/6/11 handoffs

Task 4 API bootstrap is fixed:

- Use `service_version("jhin-api")` in
  `settings.observability_config(...)`. Do not duplicate
  `jhin_api.__version__` as the telemetry resource authority.
- `initialize_observability(...)` is the first resource-owning action inside lifespan and
  precedes secret crypto, engine/session construction, NATS/Temporal clients, and locks. The engine
  receives that exact `runtime.tracer`.
- Keep runtime, secret crypto, engine, and connection clients out of module import and
  `create_app()`. Store the runtime only on app state during lifespan.
- Nested `finally` blocks attempt NATS close, engine dispose, the registered stopped log,
  and the exact runtime shutdown in order even when an earlier cleanup raises. Failure injected
  immediately after runtime creation still shuts it down and detaches the package-global owner.
  Add order/failure tests for NATS-close and engine-dispose exceptions.

Modify `apps/api/tests/test_health.py` to pass deterministic Task 4 settings and exercise
the real lifespan; do not let host environment or another test own its runtime.

In `tests/integration/test_phase2_api.py`, delete the manual engine/session wiring. The
`api` fixture must:

```python
app = create_app(settings)
async with app.router.lifespan_context(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield ApiHarness(
            client=client,
            transport=transport,
            engine=app.state.engine,
            session_factory=app.state.session_factory,
        )
```

Every `ApiHarness.new_client()` is created/entered only while that outer lifespan is
active. Lifespan alone owns engine disposal and runtime shutdown. After fixture exit, assert the
runtime owner is detached. Keep `tests/integration/test_seed.py` unchanged.

Bind the downstream plan text:

- **Task 5:** add `services/event_worker/src/jhin_event_worker/settings.py` to Task 5
  `Files` and staging. Its NATS/metric implementation upgrades event-worker to the Task
  2/3 runtime before NATS, Temporal, or engine construction, uses
  `service_version("jhin-event-worker")`, calls
  `create_engine(settings.database_url, tracer=runtime.tracer)`, supplies
  `runtime.metrics` to the lag sampler, and shuts the runtime down after its engine and
  clients. Add a call-order/identity regression. Task 6 later preserves this runtime while adding
  Temporal interceptors; it does not initialize a second event-worker runtime.
- **Task 6:** add
  `services/agent_worker/src/jhin_agent_worker/resources.py` and
  `services/tool_worker/src/jhin_tool_worker/resources.py` to Task 6
  `Files` and exact staging. Pass the process runtime into each resource constructor and
  require each long-lived `create_engine` call to use
  `tracer=runtime.tracer`. Preserve Task 4's pure-ASGI middleware, route collapse,
  `service_version("jhin-api")`, Phase 2 lifespan harness, and cleanup order when Task 6
  adds the API Temporal provider. The recursive AST gate scans all production API/service Python
  files, exempts only `seed.py`, and fails any long-lived engine call without that exact
  tracer keyword.
- **Task 11:** retain `db.operation` in `REQUIRED_SPANS`. The connected
  real-asyncpg acceptance must prove at least one database span is a child in the known API trace,
  has only the closed DB attributes, and contains no SQL, bind, DSN, URL, or exception canary in
  its complete serialized form. The Task 4 SQLite suite is not a substitute for this live proof.

### 9.8 Replace Task 4 GREEN and make its 15 paths exact

Keep the focused API and SQL RED/GREEN commands, then replace Task 4 Step 6 with:

```bash
uv lock
uv lock --check
uv run pytest packages/observability/tests packages/db/tests apps/api/tests -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The root pytest gate must collect the corrected Phase 2/seed integration modules while the marked
live cases remain deselected. Task 11 remains the connected asyncpg/OTLP gate.

Replace Task 4 Step 7 with:

```bash
set -euo pipefail
task4_paths=(
  apps/api/pyproject.toml
  apps/api/src/jhin_api/main.py
  apps/api/src/jhin_api/seed.py
  apps/api/src/jhin_api/settings.py
  apps/api/tests/test_health.py
  apps/api/tests/test_observability.py
  packages/db/pyproject.toml
  packages/db/src/jhin_db/engine.py
  packages/db/tests/test_observability.py
  packages/observability/pyproject.toml
  packages/observability/src/jhin_observability/__init__.py
  packages/observability/src/jhin_observability/sqlalchemy.py
  packages/observability/tests/test_sqlalchemy.py
  tests/integration/test_phase2_api.py
  uv.lock
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task4_paths[@]}"
git diff --check -- "${task4_paths[@]}"
git add -- "${task4_paths[@]}"
expected_index="$(printf '%s\n' "${task4_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task4_paths[@]}"
git commit --only "${task4_paths[@]}" \
  -m "feat(observability): trace API and database boundaries"
test "$(git show -s --format=%s HEAD)" = \
  "feat(observability): trace API and database boundaries"
test "$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)" = \
  "$expected_index"
test -z "$(git diff --cached --name-only)"
```

The revised Task 4 `Files` block and this array are exact mirrors. No other Task 4 path is
authorized.

### Task 5: Propagate Context Through NATS and Export Consumer Lag

**Files:**
- Modify: `apps/api/src/jhin_api/webhooks/router.py`
- Modify: `apps/api/src/jhin_api/webhooks/service.py`
- Modify: `apps/api/tests/test_webhooks_unit.py`
- Modify: `packages/events/pyproject.toml`
- Modify: `packages/events/src/jhin_events/consumer.py`
- Modify: `packages/events/src/jhin_events/publisher.py`
- Modify: `packages/events/src/jhin_events/telemetry.py`
- Modify: `packages/events/tests/test_telemetry.py`
- Modify: `services/event_worker/pyproject.toml`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/normalizer.py`
- Modify: `services/event_worker/src/jhin_event_worker/processor.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes the accepted Task 4 handoff and produces the exact Task 5 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing publisher/consumer propagation tests**

```python
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import pytest
import structlog
from nats.aio.msg import Msg
from nats.js.api import PubAck
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from jhin_events import EventEnvelope, EventPublisher, EventSource
from jhin_events.consumer import dispatch_message
from jhin_events.telemetry import (
    DlqOriginStream,
    classify_subject,
    publish_invalid_envelope_dlq,
    validate_stream_subject,
)
from jhin_event_worker.normalizer import IngressNormalizer
from jhin_event_worker.processor import EventProcessor
from jhin_observability import bind_context, get_runtime

TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$")
VALID_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
VALID_PARENT_SPAN_ID = "00f067aa0ba902b7"
KNOWN_CORRELATION = UUID("018f0000-0000-7000-8000-000000000001")


def event_envelope(*, correlation_id: UUID) -> EventEnvelope:
    return EventEnvelope(
        event_id=UUID("018f0000-0000-7000-8000-000000000002"),
        event_type="task.created",
        workspace_id="018f0000-0000-7000-8000-000000000003",
        correlation_id=correlation_id,
        source=EventSource(type="system"),
        data={},
    )


@dataclass(frozen=True)
class PublishedMessage:
    subject: str
    payload: bytes
    headers: dict[str, str]


class RecordingJetStream:
    def __init__(self) -> None:
        self.published: list[PublishedMessage] = []

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> PubAck:
        self.published.append(PublishedMessage(subject, payload, dict(headers or {})))
        return cast(PubAck, object())


class RecordingMessage:
    def __init__(
        self,
        *,
        subject: str,
        data: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = dict(headers or {})
        self.termed = False

    async def term(self) -> None:
        self.termed = True


@pytest.mark.asyncio
async def test_publisher_merges_msg_id_and_w3c_headers() -> None:
    js = RecordingJetStream()
    envelope = event_envelope(correlation_id=UUID("018f0000-0000-7000-8000-000000000001"))
    with get_runtime().tracer.start_as_current_span("test.parent"):
        await EventPublisher(js).publish(envelope, headers={"safe-header": "safe"})
    headers = js.published[0].headers
    assert headers["Nats-Msg-Id"] == str(envelope.event_id)
    assert TRACEPARENT_RE.fullmatch(headers["traceparent"])
    assert "baggage" not in headers
    assert headers["safe-header"] == "safe"


@pytest.mark.asyncio
async def test_consumer_extracts_parent_and_binds_business_correlation(
    spans: InMemorySpanExporter,
) -> None:
    seen: dict[str, str] = {}
    message = RecordingMessage(
        subject="jhin.v1.workspace-canary.task.created",
        headers={"traceparent": VALID_TRACEPARENT, "baggage": "workspace_id=attacker"},
        data=event_envelope(correlation_id=KNOWN_CORRELATION).to_bytes(),
    )

    async def handler(msg: Msg) -> None:
        envelope = EventEnvelope.from_bytes(msg.data)
        with bind_context(correlation_id=envelope.correlation_id):
            seen.update(structlog.contextvars.get_contextvars())

    await dispatch_message(  # type: ignore[arg-type]
        message, stream="EVENTS", durable="event-worker", handler=handler
    )
    consumer = next(span for span in spans.get_finished_spans() if span.name == "nats.consume")
    assert consumer.parent is not None
    assert format(consumer.parent.span_id, "016x") == VALID_PARENT_SPAN_ID
    assert dict(consumer.attributes) == {
        "messaging.system": "nats",
        "jhin.stream": "EVENTS",
        "jhin.consumer": "event-worker",
        "jhin.subject_family": "task",
        "jhin.outcome": "ok",
    }
    assert seen["correlation_id"] == str(KNOWN_CORRELATION)
    assert "attacker" not in json.dumps(seen)
```

```python
def test_subject_stream_and_family_are_closed_and_workspace_free() -> None:
    subject = "jhin.v1.workspace-canary.task.created"
    assert classify_subject(subject) == ("EVENTS", "task")
    assert classify_subject("jhin.v1.workspace-canary.ingress.github.issue.updated") == (
        "INGRESS",
        "ingress",
    )
    with pytest.raises(ValueError, match="stream/subject mismatch"):
        validate_stream_subject("INGRESS", subject)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_stream", ["INGRESS", "EVENTS"])
async def test_dlq_helper_is_closed_traced_and_payload_free(
    origin_stream: DlqOriginStream,
    spans: InMemorySpanExporter,
) -> None:
    js = RecordingJetStream()
    await publish_invalid_envelope_dlq(js, origin_stream=origin_stream, error_count=2)
    published = js.published[0]
    assert published.subject == f"jhin.dlq.{origin_stream.lower()}"
    assert json.loads(published.payload) == {
        "schema_version": 1,
        "reason": "invalid_envelope",
        "origin_stream": origin_stream,
        "error_count": 2,
    }
    span = next(span for span in spans.get_finished_spans() if span.name == "nats.publish")
    assert dict(span.attributes) == {
        "messaging.system": "nats",
        "jhin.stream": "DLQ",
        "jhin.subject_family": "dlq",
    }
    rendered = json.dumps({"name": span.name, "attributes": dict(span.attributes)})
    assert "raw-payload-canary" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_type", "subject", "origin"),
    [
        (EventProcessor, "jhin.v1.workspace-canary.task.created", "EVENTS"),
        (IngressNormalizer, "jhin.v1.workspace-canary.ingress.linear", "INGRESS"),
    ],
)
async def test_existing_invalid_handlers_publish_only_closed_dlq_document(
    handler_type: type[EventProcessor] | type[IngressNormalizer],
    subject: str,
    origin: str,
) -> None:
    js = RecordingJetStream()
    message = RecordingMessage(
        subject=subject,
        data=b'{"raw":"raw-payload-canary","authorization":"auth-canary"}',
    )
    await handler_type(js).handle(message)
    assert message.termed is True
    assert len(js.published) == 1
    published = js.published[0]
    assert published.subject == f"jhin.dlq.{origin.lower()}"
    document = json.loads(published.payload)
    assert set(document) == {"schema_version", "reason", "origin_stream", "error_count"}
    assert document["origin_stream"] == origin
    assert "raw-payload-canary" not in published.payload.decode()
    assert "auth-canary" not in published.payload.decode()
```

- [ ] **Step 2: Run NATS RED**

```bash
uv run pytest packages/events/tests/test_telemetry.py \
  services/event_worker/tests/test_telemetry.py -q
```

Expected: FAIL because NATS headers are currently only the JetStream dedupe ID and no consumer span exists.

- [ ] **Step 3: Implement trace-aware publish and consume helpers**

Replace direct `structlog` use in `consumer.py` with `jhin_observability.get_logger`. Add:

```python
StreamName = Literal["INGRESS", "EVENTS", "DLQ"]
DlqOriginStream = Literal["INGRESS", "EVENTS"]
ConsumerName = Literal["event-worker-ingress", "event-worker"]
SUBJECT_FAMILIES = frozenset(
    {
        "ingress",
        "task",
        "agent",
        "tool",
        "approval",
        "connector",
        "trigger",
        "workflow",
        "system",
        "dlq",
    }
)


def classify_subject(subject: str) -> tuple[StreamName, str]:
    parts = subject.split(".")
    if len(parts) == 3 and parts[:2] == ["jhin", "dlq"] and parts[2] in {"ingress", "events"}:
        return "DLQ", "dlq"
    if len(parts) < 5 or parts[:2] != ["jhin", "v1"]:
        raise ValueError("unsupported Jhin subject")
    family = parts[3]
    if family == "ingress":
        return "INGRESS", "ingress"
    if family in SUBJECT_FAMILIES - {"ingress"}:
        return "EVENTS", family
    raise ValueError("unsupported Jhin subject family")


def validate_stream_subject(stream: StreamName, subject: str) -> str:
    actual_stream, family = classify_subject(subject)
    if actual_stream != stream:
        raise ValueError("stream/subject mismatch")
    return family


async def publish_jetstream(
    js: JetStreamContext,
    subject: str,
    payload: bytes,
    *,
    headers: Mapping[str, str] | None = None,
    message_id: str | None = None,
    stream: StreamName,
) -> PubAck:
    family = validate_stream_subject(stream, subject)
    safe_headers = dict(headers or {})
    if message_id is not None:
        safe_headers[MSG_ID_HEADER] = message_id
    safe_headers = inject_trace_headers(safe_headers)
    with safe_span(
        "nats.publish",
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "nats",
            "jhin.stream": stream,
            "jhin.subject_family": family,
        },
    ):
        return await js.publish(subject, payload, headers=safe_headers)


class EventPublisher:
    async def publish(
        self,
        envelope: EventEnvelope,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> PubAck:
        return await publish_jetstream(
            self._js,
            event_subject(envelope.workspace_id, envelope.event_type),
            envelope.to_bytes(),
            headers=headers,
            message_id=str(envelope.event_id),
            stream="EVENTS",
        )


async def publish_invalid_envelope_dlq(
    js: JetStreamContext,
    *,
    origin_stream: DlqOriginStream,
    error_count: int,
) -> PubAck:
    if error_count < 0:
        raise ValueError("error_count must be non-negative")
    body = json.dumps(
        {
            "schema_version": 1,
            "reason": "invalid_envelope",
            "origin_stream": origin_stream,
            "error_count": error_count,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return await publish_jetstream(
        js,
        dlq_subject(origin_stream),
        body,
        stream="DLQ",
    )
```

Factor one fetched message through this exact helper; `safe_span` owns/detaches the extracted OTel
context even when the handler raises. The generic consumer never parses an envelope:

```python
from opentelemetry.trace import SpanKind

from jhin_observability import (
    SafeErrorCode,
    extract_trace_context,
    record_span_error,
    safe_error,
    safe_span,
)


async def dispatch_message(
    message: Msg,
    *,
    stream: str,
    durable: str,
    handler: MessageHandler,
) -> None:
    safe_stream = stream if stream in {"INGRESS", "EVENTS"} else "other"
    safe_consumer = durable if durable in {"event-worker-ingress", "event-worker"} else "other"
    try:
        _actual_stream, family = classify_subject(message.subject)
    except ValueError:
        family = "other"
    parent = extract_trace_context(message.headers or {})
    outcome = "failed"
    with safe_span(
        "nats.consume",
        kind=SpanKind.CONSUMER,
        context=parent,
        attributes={
            "messaging.system": "nats",
            "jhin.stream": safe_stream,
            "jhin.consumer": safe_consumer,
            "jhin.subject_family": family,
        },
    ) as span:
        try:
            await handler(message)
            outcome = "ok"
        except Exception as exc:
            record_span_error(span, safe_error(exc, code=SafeErrorCode.INTERNAL_ERROR))
            raise
        finally:
            span.set_attribute("jhin.outcome", outcome)


async def dispatch_or_nak(
    message: Msg,
    *,
    stream: str,
    durable: str,
    handler: MessageHandler,
) -> None:
    """One fetch-loop iteration; the handler still owns normal acknowledgment."""
    try:
        await dispatch_message(
            message,
            stream=stream,
            durable=durable,
            handler=handler,
        )
    except Exception as exc:
        logger.exception(
            "jetstream.consumer_handler_failed",
            stream=stream,
            consumer=durable,
            error_type=type(exc).__name__,
            error_code=SafeErrorCode.INTERNAL_ERROR.value,
        )
        await message.nak(delay=2)
```

The existing fetch loop performs exactly `await dispatch_or_nak(message, stream=stream,
durable=durable, handler=handler)` for each fetched message; no second handler call or
acknowledgment path remains.

`EventProcessor` and `IngressNormalizer` validate the envelope, then wrap their business handling
in `bind_context(workspace_id=..., correlation_id=...)`; invalid envelopes never bind unvalidated
identifiers.

Change webhook ingress to call `publish_jetstream(..., stream="INGRESS")` so the API request context reaches event-worker. Replace both direct invalid-envelope DLQ publishes in `processor.py` and `normalizer.py` with `publish_invalid_envelope_dlq`; this deliberately omits the raw NATS subject and payload. Keep term/ack, dedupe/publish/commit, and normalizer behavior unchanged.

- [ ] **Step 4: Write the failing bounded async lag sampler test**

Add this complete test (the local recorder makes the asserted gauge state explicit instead of
depending on an undefined test helper):

```python
from types import SimpleNamespace

import pytest

from jhin_events.publisher import StreamName
from jhin_event_worker.main import (
    CONSUMERS,
    ConsumerName,
    sample_nats_consumer_lag_once,
)
from jhin_observability import JhinMetrics, MetricName, Observation, noop_metrics


class LagJetStream:
    def __init__(self) -> None:
        self.pending = {
            ("INGRESS", "event-worker-ingress"): 4,
            ("EVENTS", "event-worker"): 7,
        }
        self.fail_consumer_info = False

    async def consumer_info(self, stream: str, consumer: str) -> SimpleNamespace:
        if self.fail_consumer_info:
            raise RuntimeError("probe failed")
        return SimpleNamespace(num_pending=self.pending[(stream, consumer)])


def recording_observables() -> tuple[JhinMetrics, dict[MetricName, tuple[Observation, ...]]]:
    recorded: dict[MetricName, tuple[Observation, ...]] = {}
    noops = noop_metrics()
    return JhinMetrics(
        noops.counter,
        noops.histogram,
        lambda name, values: recorded.__setitem__(name, tuple(values)),
    ), recorded


def observed_lag(
    recorded: dict[MetricName, tuple[Observation, ...]],
) -> dict[tuple[str, str], int]:
    return {
        (item.attributes["stream"], item.attributes["consumer"]): int(item.value)
        for item in recorded["nats_consumer_lag"]
    }


@pytest.mark.asyncio
async def test_lag_sampler_keeps_last_good_values_after_probe_failure() -> None:
    js = LagJetStream()
    metrics, recorded = recording_observables()
    last_values: dict[tuple[StreamName, ConsumerName], int] = {}
    await sample_nats_consumer_lag_once(js, metrics, CONSUMERS, last_values)
    assert observed_lag(recorded) == {
        ("INGRESS", "event-worker-ingress"): 4,
        ("EVENTS", "event-worker"): 7,
    }

    js.fail_consumer_info = True
    await sample_nats_consumer_lag_once(js, metrics, CONSUMERS, last_values)
    assert observed_lag(recorded) == {
        ("INGRESS", "event-worker-ingress"): 4,
        ("EVENTS", "event-worker"): 7,
    }
```

```bash
uv run pytest services/event_worker/tests/test_telemetry.py -q
```

Expected: FAIL because event-worker has no lag sampler.

- [ ] **Step 5: Implement the bounded async lag sampler**

Implement:

```python
CONSUMERS: tuple[tuple[StreamName, ConsumerName], ...] = (
    ("INGRESS", "event-worker-ingress"),
    ("EVENTS", "event-worker"),
)


async def sample_nats_consumer_lag_once(
    js: JetStreamContext,
    metrics: JhinMetrics,
    consumers: tuple[tuple[StreamName, ConsumerName], ...],
    last_values: dict[tuple[StreamName, ConsumerName], int],
) -> None:
    observations: list[Observation] = []
    for stream, consumer in consumers:
        key = (stream, consumer)
        try:
            info = await asyncio.wait_for(js.consumer_info(stream, consumer), timeout=2.0)
            last_values[key] = max(0, int(info.num_pending))
        except Exception as exc:
            logger.warning(
                "telemetry.nats_lag_probe_failed",
                stream=stream,
                consumer=consumer,
                error_type=type(exc).__name__,
            )
        if key in last_values:
            observations.append(
                Observation(last_values[key], {"stream": stream, "consumer": consumer})
            )
    metrics.set_observable("nats_consumer_lag", observations)


async def poll_nats_consumer_lag(
    js: JetStreamContext,
    metrics: JhinMetrics,
    consumers: tuple[tuple[StreamName, ConsumerName], ...],
    stop: asyncio.Event,
    *,
    interval_seconds: float = 10.0,
) -> None:
    last_values: dict[tuple[StreamName, ConsumerName], int] = {}
    while not stop.is_set():
        await sample_nats_consumer_lag_once(js, metrics, consumers, last_values)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
```

Start/cancel this task in event-worker beside its health heartbeat; polling failure changes diagnostics only and never stops consuming.

- [ ] **Step 6: Run focused GREEN and existing event suites**

```bash
uv lock
uv run pytest packages/events/tests services/event_worker/tests \
  apps/api/tests/test_webhooks_unit.py -q
uv run ruff check packages/events services/event_worker apps/api/src/jhin_api/deps.py \
  apps/api/src/jhin_api/webhooks/service.py
uv run mypy packages/events/src services/event_worker/src apps/api/src/jhin_api
```

Expected: PASS; existing message ID and webhook idempotency behavior is unchanged.

- [ ] **Step 7: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 5


The Task 5 preflight is binding and supersedes the coarser Task 5 handoff in section 9.7. Apply
these Task 2, Task 5, Task 6, and Task 11 corrections before dispatching Task 5. Task 5 must be a
self-contained green commit: its event worker exports NATS spans and lag immediately, while
deliberate package/standalone and pre-Task-6 agent/tool compatibility remains explicit no-op.

### 10.1 Move the event-worker runtime and database tracer into Task 5

Add `services/event_worker/src/jhin_event_worker/settings.py` and
`services/event_worker/pyproject.toml` to Task 5 as shown in the exact manifest below.

`Settings` extends Task 2's `ObservabilitySettings`. Close its durable names:

```python
consumer_durable_name: Literal["event-worker"] = "event-worker"
ingress_durable_name: Literal["event-worker-ingress"] = "event-worker-ingress"
```

Do not pair hard-coded sampler consumers with independently configurable durable names.

In event-worker `main()`, settings construction is followed immediately by:

```python
runtime = initialize_observability(
    settings.observability_config(
        service_name="event-worker",
        service_version=service_version("jhin-event-worker"),
        extra_log_processors=(redact_event_dict,),
    )
)
```

This occurs before NATS, Temporal, engine, heartbeat, lag, matcher, handler, or consumer
construction. Establish a `try/finally` immediately after the runtime exists; initialize
all later resource/task locals to `None` so partial startup is cleanable. Production
wiring is exact:

- `create_engine(settings.database_url, trace_sql=True,
  tracer=runtime.tracer)`;
- both `run_pull_consumer` calls receive `tracer=runtime.tracer`;
- `EventProcessor(..., tracer=runtime.tracer)` and
  `IngressNormalizer(..., tracer=runtime.tracer)`;
- every event-worker `EventPublisher` receives that same tracer; and
- the one lag poller receives the exact `runtime.metrics`.

The heartbeat, lag poller, and both product consumers are named, owned tasks. Run the two product
consumers in one `asyncio.TaskGroup`: normal stop lets both return, while failure of one
cancels and awaits the peer. The lag poller is not in that failure-coupled scope. Teardown cancels
and **awaits** every remaining task, consuming
each terminal result; clears the heartbeat; closes NATS; disposes the engine; and then shuts down
that exact runtime. Use nested cleanup so a cancellation, consumer failure, startup failure after
runtime/NATS/engine creation, task failure, NATS-close failure, or engine-dispose failure cannot
skip a later cleanup or package-global runtime detachment. No task or exception may remain
pending/unretrieved.

Add behavioral and AST regressions proving initialization is the first resource-owning action,
every injected object is identical to the runtime field, the engine receives the exact tracer, and
failure injected after runtime, NATS, Temporal, engine, heartbeat, and lag construction leaves zero
owned resource/task survivors.

### 10.2 Make every NATS tracer seam explicit and compatibility-safe

Put the generic APIs in `jhin_events.telemetry` and use these ownership rules:

- `publish_jetstream(..., *, tracer: Tracer, ...)`,
  `dispatch_or_nak(..., *, tracer: Tracer, ...)`, and
  `run_pull_consumer(..., *, tracer: Tracer, ...)` require an explicit tracer.
- `EventPublisher.__init__(js, *, tracer: Tracer | None = None)` stores
  `tracer if tracer is not None else noop_tracer()`. Its public
  `publish(envelope, *, headers=None)` signature remains fixed and uses the stored
  tracer.
- `EventProcessor` and `IngressNormalizer` accept optional constructor
  tracers only for package/test compatibility; their default is the public
  `noop_tracer()`. Event-worker production always supplies
  `runtime.tracer`.
- API `process_delivery(..., *, tracer: Tracer | None = None)` defaults deliberately to
  `noop_tracer()` for direct unit callers. The actual webhook router passes
  `request.app.state.observability.tracer` from Task 4.
- Task 6 supplies its agent/tool process tracer when their resource containers construct
  `EventPublisher`. Standalone package/live durability tests either pass an owned test
  tracer or deliberately exercise the explicit no-op default.

Add a binding-aware recursive AST test over production API/service sources. It rejects:

- an actual webhook route call missing `tracer=request.app.state.observability.tracer`;
- an event-worker generic publish/consumer/handler missing the exact runtime tracer;
- an agent/tool resource `EventPublisher` without its resource-owned runtime tracer after
  Task 6; and
- any direct production call to strict global `safe_span()` in place of these injected
  seams.

The test must recognize constructor fields passed onward (for example
`EventPublisher(js, tracer=self._tracer)`) rather than requiring a literal
`runtime.tracer` at every nested call. Default no-op is not permission for a product
entrypoint to omit injection.

### 10.3 Make Task 2's carrier provably trace-only

Keep one Task 2 `TRACE_CARRIER_KEYS` set and make
`inject_trace_headers` obey this exact order:

1. Copy the caller mapping; never mutate it.
2. Remove every supplied key whose case-insensitive spelling is
   `traceparent`, `tracestate`, or `baggage`.
3. Ask `TraceContextTextMapPropagator` to inject into a new empty mapping.
4. Merge back only its canonical lower-case `traceparent` and
   `tracestate`. With no valid current span, inject neither and never revive a stale
   caller carrier.
5. Preserve every validated non-propagation header exactly once.

Task 2 context tests and Task 5 transport tests cover lower-, mixed-, and upper-case baggage/stale
trace keys; a valid current tracestate; no current span; invalid current context; caller mapping
immutability; and absence of duplicate case variants.

Before Task 5 calls that helper, validate ordinary NATS headers through one bounded implementation:

```python
MAX_NATS_HEADERS = 32
MAX_NATS_HEADER_NAME_BYTES = 64
MAX_NATS_HEADER_VALUE_BYTES = 1_024
MAX_NATS_HEADER_TOTAL_BYTES = 8_192
```

Task 1 must export its existing sensitive-key-family predicate as
`is_sensitive_key_name(value: object) -> bool`; Task 5 reuses it rather than duplicating
the secret/authorization/cookie/token/password/API-key/DSN families.

Header validation is binding:

- keys and values are real strings; names are ASCII and match
  `[A-Za-z0-9][A-Za-z0-9-]*` within the byte cap;
- names/values containing CR, LF, NUL, invalid Unicode, or a sensitive key family are rejected;
- case-insensitive duplicate ordinary names are rejected;
- all caller spellings of `Nats-Msg-Id` and trace-carrier keys are stripped before
  canonical values are added;
- callers set dedupe identity only through the explicit `message_id` argument, which
  emits exactly one `Nats-Msg-Id`;
- the final header count and aggregate encoded bytes, including injected carrier/dedupe fields,
  remain within the exact caps; and
- every validation error is a closed `UnsafeNatsHeaderError("invalid NATS header")` that
  echoes no key or value.

The planned ordinary `safe-header` behavior and exact message ID remain unchanged. Tests
must use the resolved NATS encoding behavior and prove the input mapping is unchanged.

### 10.4 Use one subject authority and correct Task 2's family registry

The Task 2 `jhin.subject_family` closed registry is replaced with exactly:

```python
frozenset({
    "ingress", "task", "agent", "tool", "approval", "connector",
    "trigger", "workflow", "system", "dlq", "other",
})
```

Remove `run`; add every actual NATS family above. This correction supersedes the older
abbreviated registry in section 7.5.

Task 5 may not declare `SUBJECT_FAMILIES`. It imports the canonical
`jhin_events.subjects.EVENT_DOMAINS`. Define only
`StreamName`, `DlqOriginStream`, `ConsumerName`, narrow structural
publish/consumer-info Protocols, and classifier helpers in
`jhin_events.telemetry`; `publisher.py` imports from there so there is no
`publisher <-> telemetry` cycle.

The classifier accepts only:

- EVENTS: `jhin.v1.<nonempty-workspace>.<EVENT_DOMAINS member>.<one-or-more nonempty event
  tokens>`;
- INGRESS: `jhin.v1.<nonempty-workspace>.ingress.<nonempty-connector>.<one-or-more nonempty
  event tokens>`; and
- DLQ: exactly `jhin.dlq.ingress` or `jhin.dlq.events`.

Every token must satisfy the existing canonical subject-token grammar, and the classified stream
must equal the caller's expected stream. Errors never contain the workspace or raw subject. No
span/log attribute receives either. Add an invariant:

```python
assert set(SPAN_ATTRIBUTE_VALUES["jhin.subject_family"]) == (
    set(EVENT_DOMAINS) | {"ingress", "dlq", "other"}
)
```

Test empty/extra/missing/reserved tokens, every canonical domain, both ingress/DLQ forms,
stream/subject mismatches, and workspace/subject canaries.

### 10.5 Keep handler, failure log, and settlement inside one consumer span

Remove the split span ownership between `dispatch_message` and
`dispatch_or_nak`. One fail-open `dispatch_or_nak` owns the full operation:

1. Validate only closed stream/consumer/family metadata, extract the trace-only parent, and enter
   `safe_span("nats.consume", tracer=tracer, kind=SpanKind.CONSUMER,
   context=parent, attributes=...)`.
2. Await the handler exactly once. Its ACK/TERM is therefore inside the span. After it returns,
   set `jhin.outcome="ok"`.
3. On an ordinary handler exception, set `jhin.outcome="failed"`, record only
   `SafeErrorCode.INTERNAL_ERROR`, emit the registered
   `jetstream.consumer_handler_failed` JSON record while the consumer trace/span context
   is current, and await exactly one delayed NAK before leaving the span.
4. Cancellation propagates immediately and is never converted to an ordinary failure or NAK.
5. In every case, detach extracted OTel and structlog context after settlement/unwind.

Error recording/logging failure is contained so it cannot replace the handler failure or skip the
NAK. If NAK also fails, preserve the original handler exception as authoritative after consuming/
recording the settlement failure safely. On a successful NAK the fetch loop continues without a
second handler or settlement path.

Instrumentation is fail-open. If extraction/injection, span construction, attribute setting,
error recording, or span end fails, the code still invokes the handler and its one settlement path
exactly once. Add success, handler-failure, instrumentation-failure, NAK-failure, and cancellation
tests proving call counts, parent trace/span, closed status/error attributes, active trace/span IDs
in the failure log, duration through settlement, and empty contexts.

Apply the same rule to `publish_jetstream`:

- validated caller input errors stop before transport;
- trace injection/span setup failures fall back to sanitized non-carrier headers and still perform
  exactly one JetStream publish;
- publish success sets `jhin.outcome="ok"`;
- a real JetStream failure records closed safe error/outcome while possible and re-raises the exact
  transport exception; and
- no instrumentation failure may duplicate the publish or replace the transport exception.

### 10.6 Bind only context-safe workspace IDs without changing business behavior

Task 2 must expose and package-export one validator using its existing central grammar:

```python
def is_safe_context_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))
```

`bind_context` continues to reject invalid explicit values. Task 5 handlers use the
public validator; they never copy `_ID_RE`.

After an envelope is schema-valid, both service handlers always bind its UUID correlation ID.
They add `workspace_id` only when `is_safe_context_id(envelope.workspace_id)` is
true. Otherwise they omit that diagnostic field and still invoke the existing business handler
exactly once with the original envelope; existing ACK/TERM/NAK, dedupe, normalization, and matcher
behavior remains authoritative.

Add URL-, payload-, oversized-, whitespace-, and control-character workspace canaries. Each
business stub is invoked once, no canary reaches a log/span/header/error, correlation remains bound
inside handling, and both context fields are empty afterward. A valid workspace regression must
prove both fields are bound and that `jhin.correlation_id` appears on the active consumer
span as required by Task 2; do not assert the older four-attribute dict that omitted it.

### 10.7 Make tests lifecycle-owned, end-to-end, and privacy-complete

Split tests by owner:

- shared publisher/carrier/header/classifier/consumer tests live in
  `packages/events/tests/test_telemetry.py`; and
- event-worker handler/runtime/lag tests live in
  `services/event_worker/tests/test_telemetry.py`.

Each file that records spans owns a function-scoped SDK `TracerProvider`,
`SimpleSpanProcessor`, and `InMemorySpanExporter`, passes that exact tracer
through the public seam, and shuts the provider/exporter down in `finally`. There is no
package-global OTel provider mutation, strict `get_runtime()` call outside owned bootstrap,
test-order dependency, or fixture imported from another package tree. Stubs implement the narrow
Task 5 Protocols rather than pretending to be concrete `JetStreamContext` objects.

Add one deterministic in-process two-hop test:

```text
Task 4 API server span
  -> webhook nats.publish
  -> INGRESS nats.consume
  -> normalized nats.publish
  -> EVENTS nats.consume
```

All five spans share one trace ID with exact parent/child edges. The outgoing
`traceparent` span ID equals the actual producer span ID, not merely a valid regex.
Valid `tracestate` propagates; baggage does not; malformed/missing carriers create a safe
root; all contexts detach.

Preserve and test the canonical `Nats-Msg-Id`, webhook publish-before-commit, rollback,
dedupe, ACK/TERM, and in-memory redelivery-dedupe behavior. The actual webhook router test proves
it passes its app runtime tracer.

Invalid-envelope DLQ is exactly:

```python
{
    "schema_version": 1,
    "reason": "invalid_envelope",
    "origin_stream": origin_stream,
    "error_count": error_count,
}
```

`error_count` must have `type(error_count) is int` and
`0 <= error_count <= 1_000`. Bool, negative, oversized, float, string, and arbitrary
objects fail before publish. Complete JSON logs and complete serialized
span/resource/attribute/event/status data must exclude raw payload, raw subject, non-carrier input
header names/complete values, unsafe workspace, authorization/cookie/secret, exception, and
error-body canaries. Trace/span IDs derived from one valid trace carrier and the deliberately
bounded valid workspace/correlation context fields remain the only intentional derivatives.

Valid `EventProcessor` handling must bind workspace/correlation through the real matcher
seam, preserve its dedupe and ACK behavior, and clear both bindings after return/error.

### 10.8 Close lag values, timeouts, replacement, and task supervision

For `ConsumerInfo.num_pending`, accept only
`type(value) is int and value >= 0`. Never call `int(value)`. `None`,
bool, negative, float, string, and a hostile `__int__` retain the last good value.

`sample_nats_consumer_lag_once` samples each canonical consumer independently under an
injectable, finite, positive `probe_timeout_seconds`. A timeout/failure for one never
skips the other. First-sample failure emits no fabricated zero; after prior success it preserves
only the last good value.

After a successful two-consumer sample, one atomic
`metrics.set_observable("nats_consumer_lag", observations)` replacement contains exactly:

```text
(INGRESS, event-worker-ingress)
(EVENTS, event-worker)
```

There is no third/stale/duplicate normalized identity. A metric-set failure is logged safely and
does not terminate either product consumer or leave an unretrieved task exception.

Before entering `poll_nats_consumer_lag`, require
`interval_seconds` and `probe_timeout_seconds` to be non-bool finite positive
numbers. Each interval waits on `stop.wait()` with a timeout, so `stop.set()`
wakes immediately. Cancellation propagates. Event-worker never includes this background task in a
gather whose ordinary failure would cancel product consumers; it owns, names, cancels, and awaits
it during teardown.

Add tests for first/prior/partial failure, timeout cancellation, every invalid value above,
hostile conversion, exact full replacement, Task 3 duplicate-identity compatibility, immediate
stop, metric-set failure isolation, explicit cancellation, and zero pending tasks.

### 10.9 Bind dependencies, File Map, and Task 6/11 ownership

Amend the global File Map with:

```text
apps/api/src/jhin_api/webhooks/router.py
apps/api/tests/test_webhooks_unit.py
```

Remove `apps/api/src/jhin_api/deps.py` from Task 5; no Task 5 change is specified there.
Task 5 owns exactly the 15 paths in section 10.10. Generic helpers remain module-qualified under
`jhin_events.telemetry`, so `jhin_events/__init__.py` is not modified or staged.

Dependency ownership is exact:

- `packages/events/pyproject.toml` adds
  `opentelemetry-api>=1.38,<2` and preserves Task 1's existing
  `jhin-observability` dependency/workspace source.
- `services/event_worker/pyproject.toml` adds
  `jhin-secrets` and its workspace source for
  `redact_event_dict`. Its existing events/observability dependencies remain.
- Regenerate `uv.lock` once in Task 5 and then require `uv lock --check`.

Rebalance Task 6:

- event-worker `main.py` remains because Task 6 adds Temporal interceptors to its existing
  runtime;
- remove event-worker settings/manifest from Task 6 `Files` and staging: their current
  planned runtime-settings/redaction changes are now committed by Task 5, so no Task 6 delta
  remains in either path;
- retain the section 9.7 additions of agent/tool
  `resources.py`, pass each process runtime to both
  `create_engine(..., tracer=runtime.tracer)` and
  `EventPublisher(..., tracer=runtime.tracer)`; and
- extend Task 6's recursive wiring audit to check both long-lived database tracer and NATS publisher
  tracer identity. It must preserve tool-worker's agent/model authority prohibitions.

Task 11 retains the real connected trace across webhook producer, both consumers, normalized
producer, Temporal, agent, tool, connector, and sandbox spans. It must also prove exported
`nats_consumer_lag` has exactly the two canonical series and no workspace/subject/header
label. These live NATS/OTLP assertions supplement, not replace, Task 5's deterministic tests.

### 10.10 Use executable RED/GREEN and exact 15-path staging

After the Task 2 carrier/registry corrections and Tasks 1-4, Task 5 RED is:

```bash
uv run pytest packages/events/tests/test_telemetry.py -q
uv run pytest \
  services/event_worker/tests/test_telemetry.py \
  apps/api/tests/test_webhooks_unit.py -q
```

Expected RED names absent trace-aware helpers, explicit tracer/runtime wiring, lag sampler, or the
new behavioral assertions. Missing fixtures/imports, an import cycle, `NameError`,
collection/type errors, or an accidental `ObservabilityNotInitializedError` is invalid
RED evidence.

After implementation run:

```bash
uv lock
uv lock --check
uv run pytest \
  packages/events/tests \
  services/event_worker/tests \
  apps/api/tests/test_webhooks_unit.py -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Root collection must preserve the standalone/live NATS test and existing API/agent/tool publisher
regressions. Webhook dedupe/rollback and consumer settlement semantics remain authoritative.

Replace Task 5 staging with:

```bash
set -euo pipefail
task5_paths=(
  apps/api/src/jhin_api/webhooks/router.py
  apps/api/src/jhin_api/webhooks/service.py
  apps/api/tests/test_webhooks_unit.py
  packages/events/pyproject.toml
  packages/events/src/jhin_events/consumer.py
  packages/events/src/jhin_events/publisher.py
  packages/events/src/jhin_events/telemetry.py
  packages/events/tests/test_telemetry.py
  services/event_worker/pyproject.toml
  services/event_worker/src/jhin_event_worker/main.py
  services/event_worker/src/jhin_event_worker/normalizer.py
  services/event_worker/src/jhin_event_worker/processor.py
  services/event_worker/src/jhin_event_worker/settings.py
  services/event_worker/tests/test_telemetry.py
  uv.lock
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task5_paths[@]}"
git diff --check -- "${task5_paths[@]}"
git add -- "${task5_paths[@]}"
expected_index="$(printf '%s\n' "${task5_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task5_paths[@]}"
git commit --only "${task5_paths[@]}" \
  -m "feat(observability): propagate traces through NATS"
test "$(git show -s --format=%s HEAD)" = \
  "feat(observability): propagate traces through NATS"
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)"
test "$actual_commit_paths" = "$expected_index"
test -z "$(git diff --cached --name-only)"
```

The revised Task 5 `Files` block and this array are exact mirrors. No additional Task 5
path is authorized. If package-root exports are later desired,
`jhin_events/__init__.py` must first be added to File Map/Files/staging in a reviewed plan
amendment.

### Task 6: Propagate Context Through Temporal and Bootstrap Every Python Service

**Files:**
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/health/router.py`
- Modify: `apps/api/src/jhin_api/health/service.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/src/jhin_api/temporal.py`
- Modify: `apps/api/tests/test_health.py`
- Modify: `apps/api/tests/test_temporal_provider.py`
- Modify: `packages/observability/pyproject.toml`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/src/jhin_observability/logging.py`
- Modify: `packages/observability/src/jhin_observability/temporal.py`
- Modify: `packages/observability/tests/test_log_audit.py`
- Modify: `packages/observability/tests/test_logging.py`
- Modify: `packages/observability/tests/test_temporal.py`
- Modify: `packages/workflows/pyproject.toml`
- Modify: `packages/workflows/src/jhin_workflows/poller_health.py`
- Modify: `packages/workflows/tests/test_phase10_history_replay.py`
- Modify: `packages/workflows/tests/test_poller_health.py`
- Modify: `pyproject.toml`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- Modify: `services/sandbox_runner/tests/test_api_auth.py`
- Modify: `services/sandbox_runner/tests/test_telemetry.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Modify: `services/tool_worker/tests/test_advertised_tools.py`
- Modify: `services/tool_worker/tests/test_worker_registration.py`
- Modify: `services/workflow_worker/pyproject.toml`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/main.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/settings.py`
- Modify: `services/workflow_worker/tests/test_telemetry.py`
- Modify: `tests/test_worker_dependency_boundaries.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes the accepted Task 5 handoff and produces the exact Task 6 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing Temporal propagation/replay/failure-metric tests**

Use `WorkflowEnvironment.start_time_skipping()` and in-memory exporters:

```python
@pytest.mark.asyncio
async def test_trace_crosses_agent_and_tool_queues_without_payload_ids_in_labels() -> None:
    async with temporal_test_environment() as env:
        result = await run_cross_queue_probe(
            env,
            client_interceptors=[SafeTemporalTracingInterceptor(tracer, role="client")],
            worker_interceptors=[
                SafeTemporalTracingInterceptor(tracer, role="worker"),
                TemporalActivityMetricsInterceptor(metrics, task_queue="jhin-tool-queue"),
            ],
        )
    assert result.activity_trace_id == result.client_trace_id
    assert {span.name for span in finished_spans()} >= {
        "temporal.start_workflow",
        "temporal.activity.reason_agent_step",
        "temporal.activity.execute_bound_tool",
    }
    assert all("workspace" not in dict(point.attributes) for point in metric_points())


@pytest.mark.asyncio
async def test_workflow_replay_does_not_emit_duplicate_application_spans() -> None:
    first_history = await execute_and_capture_history()
    before = len(finished_spans())
    await replay_history(
        first_history,
        interceptors=[SafeTemporalTracingInterceptor(tracer, role="worker")],
    )
    assert len(finished_spans()) == before


@pytest.mark.asyncio
async def test_failed_activity_increments_closed_failure_metric_once() -> None:
    await execute_failing_activity(name="execute_bound_tool", attempts=2)
    assert (
        metric_sum(
            "temporal_activity_failures",
            task_queue="jhin-tool-queue",
            activity="execute_bound_tool",
            failure_class="internal",
        )
        == 2
    )


def test_client_and_worker_interceptor_lists_have_exact_roles(
    runtime: ObservabilityRuntime,
) -> None:
    client = temporal_client_interceptors(runtime)
    worker = temporal_worker_interceptors(runtime, task_queue="jhin-agent-queue")
    assert [(type(item), item.role) for item in client] == [
        (SafeTemporalTracingInterceptor, "client")
    ]
    assert [(type(item), getattr(item, "role", None)) for item in worker] == [
        (SafeTemporalTracingInterceptor, "worker"),
        (TemporalActivityMetricsInterceptor, None),
    ]


def test_temporal_error_has_no_exception_event_description_or_dynamic_name(
    spans: InMemorySpanExporter,
    runtime: ObservabilityRuntime,
) -> None:
    interceptor = SafeTemporalTracingInterceptor(runtime.tracer, role="client")
    with pytest.raises(RuntimeError, match="temporal-payload-canary"):
        with interceptor._start_as_current_span(
            "StartWorkflow:temporal-payload-canary",
            attributes={"temporalWorkflowID": "workflow-1"},
            kind=SpanKind.CLIENT,
        ):
            raise RuntimeError("temporal-payload-canary")
    span = spans.get_finished_spans()[0]
    assert span.name == "temporal.start_workflow"
    assert span.events == ()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert dict(span.attributes) == {
        "temporal.workflow_id": "workflow-1",
        "error.type": "RuntimeError",
        "error.code": "internal_error",
    }
    assert "temporal-payload-canary" not in json.dumps(dict(span.attributes))


def test_every_temporal_connect_and_worker_uses_shared_interceptor_helpers() -> None:
    wiring = audit_temporal_wiring(REPO_ROOT)
    assert wiring.client_connect_calls >= {
        "api",
        "agent-worker",
        "tool-worker",
        "event-worker",
        "workflow-worker",
    }
    assert wiring.worker_calls == {"agent-worker", "tool-worker", "workflow-worker"}
    assert wiring.uninstrumented_client_connect_calls == []
    assert wiring.uninstrumented_worker_calls == []
    assert wiring.direct_health_connect_calls == ()
    assert wiring.api_connect_outside_provider_calls == ()


def test_temporal_literal_has_required_runtime_imports() -> None:
    tree = ast.parse(
        (REPO_ROOT / "packages/observability/src/jhin_observability/temporal.py").read_text()
    )
    imports = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for alias in node.names
    }
    assert ("temporalio", "activity") in imports
    assert ("temporalio.worker", "ExecuteActivityInput") in imports


# apps/api/tests/test_temporal_provider.py
from unittest.mock import Mock

from temporalio.client import Interceptor


@pytest.fixture
def api_temporal_runtime() -> Iterator[ObservabilityRuntime]:
    runtime = initialize_observability(
        ObservabilityConfig(service_name="api", service_version="0.1.0", environment="test")
    )
    try:
        yield runtime
    finally:
        runtime.shutdown()


@pytest.mark.asyncio
async def test_api_business_and_health_share_one_interceptor_aware_client(
    monkeypatch: pytest.MonkeyPatch,
    api_temporal_runtime: ObservabilityRuntime,
) -> None:
    runtime = api_temporal_runtime
    settings = cast(
        Settings, SimpleNamespace(temporal_address="temporal:7233", temporal_namespace="default")
    )
    connected: list[tuple[tuple[object, ...], dict[str, object]]] = []
    client = cast(TemporalClient, SimpleNamespace())
    expected_interceptors = [cast(Interceptor, object())]
    interceptor_builder = Mock(return_value=expected_interceptors)

    async def connect(*args: object, **kwargs: object) -> TemporalClient:
        connected.append((args, dict(kwargs)))
        return client

    monkeypatch.setattr(
        "jhin_api.temporal.temporal_client_interceptors",
        interceptor_builder,
    )
    monkeypatch.setattr(TemporalClient, "connect", connect)
    provider = TemporalClientProvider(settings, runtime)
    business, health = await asyncio.gather(provider.get(), provider.get())
    assert business is health is client
    assert len(connected) == 1
    args, kwargs = connected[0]
    assert args == ("temporal:7233",)
    assert kwargs["namespace"] == "default"
    assert kwargs["interceptors"] is expected_interceptors
    interceptor_builder.assert_called_once_with(runtime)


def test_protected_health_handoff_keeps_the_same_provider_contract() -> None:
    signature = inspect.signature(TemporalClientProvider)
    assert tuple(signature.parameters) == ("settings", "observability")
    assert get_type_hints(TemporalClientProvider.get)["return"] is TemporalClient


def test_bootstrap_ast_contract_is_red_before_service_rewiring() -> None:
    entrypoints = (
        REPO_ROOT / "apps/api/src/jhin_api/main.py",
        REPO_ROOT / "services/agent_worker/src/jhin_agent_worker/main.py",
        REPO_ROOT / "services/tool_worker/src/jhin_tool_worker/main.py",
        REPO_ROOT / "services/event_worker/src/jhin_event_worker/main.py",
        REPO_ROOT / "services/workflow_worker/src/jhin_workflow_worker/main.py",
        REPO_ROOT / "services/sandbox_runner/src/jhin_sandbox_runner/main.py",
    )
    resource_suffixes = (
        "create_engine",
        "nats.connect",
        "Client.connect",
        "TemporalClient.connect",
        "httpx.AsyncClient",
        "JobManager",
        "Worker",
        "Resources.create",
        "connect_with_retry",
        "resources_with_retry",
        "temporal_with_retry",
    )
    for path in entrypoints:
        tree = ast.parse(path.read_text())
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }

        def owner(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return parent
                parent = parents.get(parent)
            return None

        resource_owners = 0
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"main", "run", "lifespan", "create_app"}
        ):
            calls = sorted(
                (
                    node
                    for node in ast.walk(function)
                    if isinstance(node, ast.Call) and owner(node) is function
                ),
                key=lambda node: (node.lineno, node.col_offset),
            )
            resources = [
                call for call in calls if ast.unparse(call.func).endswith(resource_suffixes)
            ]
            if not resources:
                continue
            resource_owners += 1
            initialization = [
                call
                for call in calls
                if ast.unparse(call.func).endswith("initialize_observability")
            ]
            assert initialization, (path, function.name)
            assert initialization[0].lineno < resources[0].lineno, (
                path,
                function.name,
                ast.unparse(resources[0].func),
            )
        assert resource_owners > 0, path


def test_long_lived_database_calls_inject_initialized_tracer() -> None:
    roots = (REPO_ROOT / "apps/api/src", REPO_ROOT / "services")
    failures: list[str] = []
    for path in (file for root in roots for file in root.rglob("*.py")):
        if path.name == "seed.py" or "tests" in path.parts:
            continue
        for call in (
            node
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("create_engine")
        ):
            values = {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}
            if values.get("tracer") != "runtime.tracer":
                failures.append(f"{path}:{call.lineno}")
    assert failures == []


# tests/test_worker_dependency_boundaries.py: replace only the predecessor's
# temporary "jhin-observability not in tool dependencies/imports" assertions.
def test_tool_worker_keeps_authority_boundary_with_shared_observability() -> None:
    tool_dependencies = set(
        tomllib.loads((REPO_ROOT / "services/tool_worker/pyproject.toml").read_text())["project"][
            "dependencies"
        ]
    )
    assert any(item.startswith("jhin-observability") for item in tool_dependencies)
    assert not any(item.startswith("jhin-models") for item in tool_dependencies)
    assert not any(item.startswith("jhin-agents") for item in tool_dependencies)
    assert not imports_under("services/tool_worker/src", "jhin_models")
    assert not imports_under("services/tool_worker/src", "jhin_agents")
    workflow_dependencies = set(
        tomllib.loads((REPO_ROOT / "packages/workflows/pyproject.toml").read_text())["project"][
            "dependencies"
        ]
    )
    assert any(item.startswith("jhin-observability") for item in workflow_dependencies)


# services/tool_worker/tests/test_worker_registration.py
def test_tool_worker_bootstraps_before_resources_and_registers_interceptors() -> None:
    source = (REPO_ROOT / "services/tool_worker/src/jhin_tool_worker/main.py").read_text()
    tree = ast.parse(source)
    calls = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    lines = {ast.unparse(call.func): call.lineno for call in calls}
    assert lines["initialize_observability"] < lines["ToolWorkerResources.create"]
    worker = next(call for call in calls if ast.unparse(call.func).endswith("Worker"))
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in worker.keywords}
    assert keywords["interceptors"] == (
        "temporal_worker_interceptors(runtime, task_queue=TOOL_TASK_QUEUE)"
    )
```

Place the provider concurrency/handoff tests in `apps/api/tests/test_temporal_provider.py`; place the
interceptor, import, recursive wiring, bootstrap-order, and dependency-boundary tests in the other
files named by their inline comments. The final failure-metric assertion is attempt-level by design;
terminal tool/run metrics remain commit-level and are tested later.

`test_temporal_provider.py` imports `asyncio`, `inspect`, `Iterator` from `collections.abc`, `SimpleNamespace` from `types`, `Mock` from
`unittest.mock`, `cast` and `get_type_hints` from `typing`, `pytest`, `Interceptor`, `TemporalClient`, `Settings`, `TemporalClientProvider`,
`ObservabilityConfig`, `ObservabilityRuntime`, `initialize_observability`, and
no direct interceptor-builder alias. Its local `api_temporal_runtime` fixture owns and shuts down
the exact runtime used by the provider test; monkeypatching the provider module's builder proves
that its single returned list object is the exact value forwarded to `TemporalClient.connect`.

- [ ] **Step 2: Run Temporal RED**

```bash
uv run pytest packages/observability/tests/test_temporal.py \
  apps/api/tests/test_temporal_provider.py \
  packages/workflows/tests/test_poller_health.py \
  services/workflow_worker/tests/test_telemetry.py \
  services/tool_worker/tests/test_worker_registration.py \
  tests/test_worker_dependency_boundaries.py -q
```

Expected: FAIL because no safe Temporal interceptors exist.

This RED also fails the two AST bootstrap/injection contracts against the predecessor tree before
any production entrypoint is rewired. Do not begin Step 3 until the failure output names the missing
interceptor module and the pre-bootstrap resource calls.

- [ ] **Step 3: Implement trace-only, redacted Temporal tracing**

Add `temporalio>=1.31,<1.32` to `jhin-observability`; the narrow bound makes the audited interceptor hooks explicit rather than silently accepting an incompatible SDK minor. Subclass the SDK's supported `TracingInterceptor(always_create_workflow_spans=False)` and `TracingWorkflowInboundInterceptor`. Set both `text_map_propagator = TraceContextTextMapPropagator()` so baggage is never serialized. Override `_completed_workflow_span(params)` to return `params.context` unchanged: this preserves the SDK's deterministic header propagation to outbound activities but emits no workflow span at all. Therefore Jhin emits spans only in client/activity interceptors, and workflow replay cannot create an application span.

```python
import dataclasses
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import temporalio.client
import temporalio.exceptions
from opentelemetry import context as otel_context
from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes, AttributeValue
from temporalio import activity
from temporalio.client import OutboundInterceptor
from temporalio.contrib.opentelemetry import (
    TracingInterceptor,
    TracingWorkflowInboundInterceptor as TemporalTracingWorkflowInboundInterceptor,
)
from temporalio.contrib.opentelemetry._interceptor import (
    _CarrierDict as CarrierDict,
    _CompletedWorkflowSpanParams as CompletedWorkflowSpanParams,
    _InputWithHeaders as InputWithHeaders,
    _InputWithOperationContext as InputWithOperationContext,
)
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    NexusOperationInboundInterceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from jhin_observability.context import inject_trace_headers
from jhin_observability.errors import SafeErrorCode, safe_error

TemporalInterceptorRole = Literal["client", "worker"]


class TracingWorkflowInboundInterceptor(TemporalTracingWorkflowInboundInterceptor):
    def __init__(self, next: WorkflowInboundInterceptor) -> None:
        super().__init__(next)
        self.text_map_propagator = TraceContextTextMapPropagator()


class _PassthroughWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        return await self.next.execute_workflow(input)


class SafeTemporalTracingInterceptor(TracingInterceptor):
    def __init__(self, tracer: Tracer, *, role: TemporalInterceptorRole) -> None:
        super().__init__(tracer=tracer, always_create_workflow_spans=False)
        self.role = role
        self.text_map_propagator = TraceContextTextMapPropagator()

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return super().intercept_client(next) if self.role == "client" else next

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return super().intercept_activity(next) if self.role == "worker" else next

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:  # type: ignore[override]
        if self.role != "worker":
            return _PassthroughWorkflowInboundInterceptor
        super().workflow_interceptor_class(input)
        return TracingWorkflowInboundInterceptor

    def intercept_nexus_operation(
        self, next: NexusOperationInboundInterceptor
    ) -> NexusOperationInboundInterceptor:
        return super().intercept_nexus_operation(next) if self.role == "worker" else next

    def _completed_workflow_span(self, params: CompletedWorkflowSpanParams) -> CarrierDict | None:
        return params.context

    @contextmanager
    def _start_as_current_span(
        self,
        name: str,
        *,
        attributes: Attributes,
        input_with_headers: InputWithHeaders | None = None,
        input_with_ctx: InputWithOperationContext[Any] | None = None,
        kind: SpanKind,
        context: Context | None = None,
    ) -> Iterator[None]:
        with _safe_temporal_current_span(
            self,
            name,
            attributes=attributes,
            input_with_headers=input_with_headers,
            input_with_ctx=input_with_ctx,
            kind=kind,
            context=context,
        ):
            yield
```

Override the SDK 1.31 `_start_as_current_span` hook with the same explicit signature. Normalize the name and attributes before creating the span, inject only the trace-only carrier, and on failure set `StatusCode.ERROR`, `error.type`, and `error.code` without a status description or exception event:

```python
def safe_temporal_span_name(raw: str) -> str:
    operation, _, dynamic = raw.partition(":")
    if operation in {"StartWorkflow", "SignalWithStartWorkflow"}:
        return "temporal.start_workflow"
    if operation == "SignalWorkflow":
        return "temporal.signal_workflow"
    if operation == "RunActivity":
        return f"temporal.activity.{temporal_activity_name(dynamic)}"
    return "temporal.client.other"


_TEMPORAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TASK_QUEUES = frozenset({"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue"})


def normalize_task_queue(value: str) -> str:
    return value if value in _TASK_QUEUES else "other"


def normalize_temporal_attributes(attributes: Attributes) -> dict[str, AttributeValue]:
    output: dict[str, AttributeValue] = {}
    for source, destination in (
        ("temporalWorkflowID", "temporal.workflow_id"),
        ("temporalRunID", "temporal.run_id"),
    ):
        value = attributes.get(source)
        if isinstance(value, str) and _TEMPORAL_ID_RE.fullmatch(value):
            output[destination] = value
    activity_type = attributes.get("temporalActivityType")
    if isinstance(activity_type, str):
        output["temporal.activity_type"] = temporal_activity_name(activity_type)
    return output


def classify_safe_error_code(exc: BaseException) -> SafeErrorCode:
    if isinstance(exc, (TimeoutError, temporalio.exceptions.TimeoutError)):
        return SafeErrorCode.TIMEOUT
    if isinstance(exc, PermissionError):
        return SafeErrorCode.AUTHORIZATION_FAILED
    if isinstance(exc, (ConnectionError, OSError, temporalio.client.RPCError)):
        return SafeErrorCode.UPSTREAM_UNAVAILABLE
    if isinstance(exc, ValueError):
        return SafeErrorCode.INVALID_REQUEST
    return SafeErrorCode.INTERNAL_ERROR


def classify_failure(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, temporalio.exceptions.TimeoutError)):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "authorization"
    if isinstance(exc, (ConnectionError, OSError, temporalio.client.RPCError)):
        return "transport"
    if isinstance(exc, ValueError):
        return "validation"
    return "internal"


@contextmanager
def _safe_temporal_current_span(
    root: SafeTemporalTracingInterceptor,
    name: str,
    *,
    attributes: Attributes,
    input_with_headers: InputWithHeaders | None = None,
    input_with_ctx: InputWithOperationContext[Any] | None = None,
    kind: SpanKind,
    context: Context | None = None,
) -> Iterator[None]:
    safe_name = safe_temporal_span_name(name)
    safe_attributes = normalize_temporal_attributes(attributes)
    token = otel_context.attach(context) if context is not None else None
    try:
        with root.tracer.start_as_current_span(
            safe_name,
            attributes=safe_attributes,
            kind=kind,
            context=context,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            if input_with_headers is not None:
                input_with_headers.headers = root._context_to_headers(input_with_headers.headers)
            if input_with_ctx is not None:
                input_with_ctx.ctx = dataclasses.replace(
                    input_with_ctx.ctx,
                    headers=inject_trace_headers(input_with_ctx.ctx.headers),
                )
            try:
                yield
            except Exception as exc:
                error = safe_error(exc, code=classify_safe_error_code(exc))
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", error.type)
                span.set_attribute("error.code", error.code.value)
                raise
    finally:
        if token is not None:
            otel_context.detach(token)
```

Nexus is not registered by any Jhin worker. `_start_as_current_span` handles `input_with_ctx is not None` by injecting the same trace-only propagator into a copied Nexus context and normalizing the span name to `temporal.client.other`; it never includes Nexus service/operation strings. The SDK compatibility test exercises start-workflow, signal, activity, and a synthetic Nexus context under Temporal 1.31 and scans exported span names, attributes, status, events, and serialized headers for exception/payload canaries.

Normalize SDK-generated names through the one stable registry created in Task 2; do not declare a second tuple or set in this module:

```python
from jhin_observability.registry import TEMPORAL_ACTIVITY_NAMES


def temporal_activity_name(raw: str) -> str:
    return raw if raw in TEMPORAL_ACTIVITY_NAMES else "other"
```

Client/activity span attributes may include validated `task_id`, `run_id`, `correlation_id`, Temporal workflow/run IDs, task queue, registered workflow/activity name, attempt, and outcome. They must not include payloads, headers, arbitrary activity names, exception messages, or workflow search attributes.

- [ ] **Step 4: Implement the activity failure interceptor**

```python
class TemporalActivityMetricsInterceptor(temporalio.worker.Interceptor):
    def __init__(self, metrics: JhinMetrics, *, task_queue: str) -> None:
        self._metrics = metrics
        self._task_queue = normalize_task_queue(task_queue)

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _ActivityMetricsInbound(next, self._metrics, self._task_queue)


class _ActivityMetricsInbound(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, metrics: JhinMetrics, task_queue: str
    ) -> None:
        super().__init__(next)
        self._metrics = metrics
        self._task_queue = task_queue

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        try:
            return await self.next.execute_activity(input)
        except Exception as exc:
            self._metrics.counter("temporal_activity_failures").add(
                1,
                task_queue=self._task_queue,
                activity=temporal_activity_name(activity.info().activity_type),
                failure_class=classify_failure(exc),
            )
            raise
```

`classify_failure` returns only the closed `failure_class` values from Task 3 and never inspects arbitrary message text.

- [ ] **Step 5: Replace every service-local logging setup with shared bootstrap**

Each service `Settings` extends `ObservabilitySettings`. Use these two helpers at every call site—never instantiate the interceptors ad hoc:

```python
from collections.abc import Callable, Sequence
from typing import Protocol


class ObservabilityTemporalSettings(Protocol):
    temporal_address: str
    temporal_namespace: str


def temporal_client_interceptors(
    runtime: ObservabilityRuntime,
) -> list[temporalio.client.Interceptor]:
    return [SafeTemporalTracingInterceptor(runtime.tracer, role="client")]


def temporal_worker_interceptors(
    runtime: ObservabilityRuntime, *, task_queue: str
) -> list[temporalio.worker.Interceptor]:
    return [
        SafeTemporalTracingInterceptor(runtime.tracer, role="worker"),
        TemporalActivityMetricsInterceptor(runtime.metrics, task_queue=task_queue),
    ]


async def connect_temporal_client(
    settings: ObservabilityTemporalSettings,
    runtime: ObservabilityRuntime,
) -> Client:
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        interceptors=temporal_client_interceptors(runtime),
    )


def build_temporal_worker(
    client: Client,
    *,
    runtime: ObservabilityRuntime,
    task_queue: str,
    workflows: Sequence[type],
    activities: Sequence[Callable[..., object]],
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
        interceptors=temporal_worker_interceptors(runtime, task_queue=task_queue),
    )
```

API, event-worker, agent-worker, tool-worker, and workflow-worker use
`connect_temporal_client`; the API provider is the sole deliberate direct-connect exception because
it owns API client lifetime and still calls `temporal_client_interceptors(runtime)`. Agent-worker,
tool-worker, and workflow-worker use `build_temporal_worker`. Do not add worker instrumentation
inside workflow definitions.

The predecessor's `jhin-temporal-poller-check` is also production code under the recursive audit.
Add `jhin-observability` to `packages/workflows` and keep its existing public arguments while routing
its one connection through the same helper:

`poller_health.py` imports `os`, `Client`, `DescribeTaskQueueRequest`, `TaskQueue`, `TaskQueueType`,
and the public `ObservabilityConfig`, `ObservabilityRuntime`, `initialize_observability`,
`service_version`, and `temporal_client_interceptors` names explicitly.

```python
async def queue_has_workflow_poller(
    address: str,
    namespace: str,
    queue: str,
    *,
    runtime: ObservabilityRuntime | None = None,
) -> bool:
    owns_runtime = runtime is None
    raw_environment = os.environ.get("APP_ENV", "production")
    environment = (
        raw_environment
        if raw_environment in {"dev", "test", "staging", "production"}
        else "production"
    )
    active_runtime = runtime or initialize_observability(
        ObservabilityConfig(
            service_name="temporal-poller-check",
            service_version=service_version("jhin-workflows"),
            environment=environment,
        )
    )
    try:
        client = await Client.connect(
            address,
            namespace=namespace,
            interceptors=temporal_client_interceptors(active_runtime),
        )
        response = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=namespace,
                task_queue=TaskQueue(name=queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            )
        )
        return bool(response.pollers)
    finally:
        if owns_runtime:
            active_runtime.shutdown()
```

Existing package/host tests can omit `runtime` and receive an owned no-export runtime; a
long-lived caller must pass its initialized runtime. Add this exact injected-runtime regression to
`test_poller_health.py`; it patches the builder where `poller_health.py` imports it, so a fresh helper
call in the assertion cannot accidentally compare two unequal interceptor instances:

```python
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from temporalio.client import Client, Interceptor

from jhin_observability import ObservabilityRuntime
from jhin_workflows.poller_health import queue_has_workflow_poller


@pytest.mark.asyncio
async def test_poller_forwards_the_builders_exact_interceptor_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_runtime = cast(ObservabilityRuntime, object())
    expected_interceptors = [cast(Interceptor, object())]
    interceptor_builder = Mock(return_value=expected_interceptors)
    connected: list[tuple[tuple[object, ...], dict[str, object]]] = []
    requests: list[object] = []

    class WorkflowService:
        async def describe_task_queue(self, request: object) -> SimpleNamespace:
            requests.append(request)
            return SimpleNamespace(pollers=[object()])

    client = cast(Client, SimpleNamespace(workflow_service=WorkflowService()))

    async def connect(*args: object, **kwargs: object) -> Client:
        connected.append((args, dict(kwargs)))
        return client

    monkeypatch.setattr(
        "jhin_workflows.poller_health.temporal_client_interceptors",
        interceptor_builder,
    )
    monkeypatch.setattr(Client, "connect", connect)

    assert (
        await queue_has_workflow_poller(
            "temporal.test:7233",
            "default",
            "jhin-workflow-queue",
            runtime=active_runtime,
        )
        is True
    )
    assert len(connected) == 1
    args, kwargs = connected[0]
    assert args == ("temporal.test:7233",)
    assert kwargs["namespace"] == "default"
    assert kwargs["interceptors"] is expected_interceptors
    interceptor_builder.assert_called_once_with(active_runtime)
    assert len(requests) == 1
```

Retain the predecessor's live-poller test and the owned-runtime regression that leaves
`get_runtime()` strict again after return. The recursive audit includes this package file, so no
health/CLI connection is exempted.

The API owns one `TemporalClientProvider(settings, observability)` in `app.state`; business
dependencies, current readiness, and the later protected-health probes share it. The provider lives
in `apps/api/src/jhin_api/temporal.py`, which is the only API module allowed to call
`TemporalClient.connect`:

```python
class TemporalClientProvider:
    def __init__(
        self,
        settings: Settings,
        observability: ObservabilityRuntime,
    ) -> None:
        self._settings = settings
        self._observability = observability
        self._client: TemporalClient | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> TemporalClient:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                self._client = await TemporalClient.connect(
                    self._settings.temporal_address,
                    namespace=self._settings.temporal_namespace,
                    interceptors=temporal_client_interceptors(self._observability),
                )
            return self._client


async def get_temporal_client(request: Request) -> TemporalClient:
    try:
        return await request.app.state.temporal_provider.get()
    except (RPCError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task orchestration is unavailable (cannot reach Temporal)",
        ) from exc


def get_temporal_provider(request: Request) -> TemporalClientProvider:
    return request.app.state.temporal_provider


async def check_temporal(provider: TemporalClientProvider) -> None:
    client = await provider.get()
    if not await client.service_client.check_health():
        raise TemporalHealthUnavailable("Temporal workflow service is unavailable")
```

Define `TemporalHealthUnavailable(RuntimeError)` in `health/service.py`; current readiness maps it to
its closed unavailable result and never serializes its message. `health/router.py` obtains
`request.app.state.temporal_provider` and passes it to `readiness(settings, engine, provider)`;
`health/service.py` removes its Temporal import and direct `connect`. The protected-health plan must
import this exact provider, retain its constructor and `get()` contract, and add probes around
`await provider.get()` rather than replacing the file or client cache. Its handoff regression is
`test_protected_health_handoff_keeps_the_same_provider_contract` plus the recursive wiring audit.

Replace API lifespan resource ownership with this exact order, and remove the old `configure_logging(...)` and `app.state.secret_crypto = _load_secret_crypto()` calls from `create_app`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = initialize_observability(
        settings.observability_config(
            service_name="api",
            service_version=service_version("jhin-api"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    app.state.observability = runtime
    app.state.temporal_provider = TemporalClientProvider(settings, runtime)
    try:
        app.state.secret_crypto = _load_secret_crypto()
        app.state.engine = create_engine(
            settings.database_url, trace_sql=True, tracer=runtime.tracer
        )
        app.state.session_factory = create_session_factory(app.state.engine)
        app.state.nats_client = None
        app.state.nats_connect_lock = asyncio.Lock()
        logger.info("api.started")
        yield
    finally:
        try:
            nats = getattr(app.state, "nats_client", None)
            if nats is not None and not nats.is_closed:
                await nats.close()
        finally:
            try:
                engine = getattr(app.state, "engine", None)
                if engine is not None:
                    await engine.dispose()
            finally:
                logger.info("api.stopped")
                runtime.shutdown(timeout_millis=5_000)
```

`runtime = initialize_observability(...)` is the first executed statement inside lifespan; the same app owns and shuts down that exact runtime. There is no module-global runtime, import-time initialization, or pre-bootstrap secret/client/resource construction.

Ensure root pytest `testpaths` contains `services/workflow_worker/tests` and `services/tool_worker/tests` in addition to `packages/observability/tests`; sub-project 1 may already have added tool-worker, so preserve it rather than duplicating it.

The first side-effecting lines are exactly:

```python
settings = Settings()
runtime = initialize_observability(
    settings.observability_config(
        service_name="agent-worker",  # exact service changes per process
        service_version=service_version("jhin-agent-worker"),
        extra_log_processors=(redact_event_dict,),
    )
)
```

Use exact service names `api`, `agent-worker`, `tool-worker`, `event-worker`, `workflow-worker`, and `sandbox-runner`. Add the dependency-light `jhin-secrets` redaction processor to event-worker and workflow-worker so all six Python processes install known-value redaction; this does not grant either worker database, master-key, or secret-file access. Store runtime in FastAPI app state where applicable, and call bounded `runtime.shutdown()` after clients/resources stop. Remove all service calls to the compatibility `configure_logging` alias, then remove that alias from Task 1 logging.

Sandbox-runner has both a CLI and directly tested app-factory path. Give `create_app` an optional
already-initialized runtime; either path initializes before constructing `JobManager`, and exactly
the path that created the runtime shuts it down:

```python
def create_app(
    settings: Settings | None = None,
    *,
    runtime: ObservabilityRuntime | None = None,
) -> FastAPI:
    active_settings = settings if settings is not None else Settings()
    owns_runtime = runtime is None
    active_runtime = runtime or initialize_observability(
        active_settings.observability_config(
            service_name="sandbox-runner",
            service_version=service_version("jhin-sandbox-runner"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    manager = JobManager(active_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.observability = active_runtime
        app.state.manager = manager
        try:
            await manager.start()
            logger.info(
                "sandbox_runner.started",
                token_configured=bool(active_settings.sandbox_runner_token),
            )
            yield
        finally:
            try:
                await manager.close()
            finally:
                if owns_runtime:
                    active_runtime.shutdown(timeout_millis=5_000)

    app = FastAPI(
        title="Jhin Sandbox Runner",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.observability = active_runtime
    app.state.manager = manager
    return install_existing_runner_routes(app, active_settings, manager)


def run() -> None:
    settings = Settings()
    runtime = initialize_observability(
        settings.observability_config(
            service_name="sandbox-runner",
            service_version=service_version("jhin-sandbox-runner"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    try:
        uvicorn.run(
            create_app(settings, runtime=runtime),
            host="0.0.0.0",
            port=settings.sandbox_runner_port,
            log_config=None,
        )
    finally:
        runtime.shutdown(timeout_millis=5_000)


def install_existing_runner_routes(
    app: FastAPI,
    settings: Settings,
    manager: JobManager,
) -> FastAPI:
    def require_token(request: Request) -> None:
        configured = settings.sandbox_runner_token
        header = request.headers.get("authorization", "")
        presented = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not configured or not presented or not secrets.compare_digest(presented, configured):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid runner token",
            )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "docker": await manager.ping()}

    @app.post(
        "/v1/jobs",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    async def submit_job(request: SandboxJobRequest) -> SandboxJobStatusResponse:
        try:
            record = await manager.submit(request)
        except JobValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        return record.to_response()

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
    async def job_status(job_id: str) -> SandboxJobStatusResponse:
        record = manager.get(job_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="job not found",
            )
        return record.to_response()

    @app.get("/v1/jobs/{job_id}/logs", dependencies=[Depends(require_token)])
    async def job_logs(job_id: str) -> SandboxLogsResponse:
        record = manager.get(job_id)
        logs = await manager.current_logs(job_id)
        if record is None or logs is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="job not found",
            )
        stdout, stderr, stdout_truncated, stderr_truncated = logs
        return SandboxLogsResponse(
            job_id=job_id,
            status=record.status,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_job(job_id: str) -> SandboxJobStatusResponse:
        record = await manager.cancel(job_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="job not found",
            )
        return record.to_response()

    @app.delete(
        "/v1/workspaces/{workspace_key}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_token)],
    )
    async def delete_workspace(workspace_key: str) -> None:
        await manager.delete_workspace(workspace_key)

    return app
```

This literal extraction does not change routes, status codes, authentication, or response DTOs.
The app-factory test path owns and shuts down its runtime in lifespan; the CLI passes its runtime
and shuts it down after Uvicorn returns. Both construct the manager only after the shared
logger/trace/metric bootstrap exists.

- [ ] **Step 6: Implement the remaining Step 1 wiring helper and run AST GREEN**

The bootstrap-order and injection tests were already written and observed RED in Steps 1–2.
Implement the recursive wiring auditor used by those tests and add the deterministic-workflow half
of the same test module now; do not introduce a new post-rewiring bootstrap test:

```python
@dataclass(frozen=True)
class TemporalWiringAudit:
    client_connect_calls: frozenset[str]
    worker_calls: frozenset[str]
    uninstrumented_client_connect_calls: tuple[str, ...]
    uninstrumented_worker_calls: tuple[str, ...]
    direct_health_connect_calls: tuple[str, ...]
    api_connect_outside_provider_calls: tuple[str, ...]


def _keyword_call_name(call: ast.Call, keyword: str) -> str | None:
    value = next((item.value for item in call.keywords if item.arg == keyword), None)
    return (
        value.func.id if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) else None
    )


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _qualified_name(node: ast.expr, aliases: Mapping[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value, aliases)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _temporal_production_files(root: Path) -> tuple[tuple[str, Path], ...]:
    roots = (root / "apps/api/src", root / "services", root / "packages")
    files: list[tuple[str, Path]] = []
    for source_root in roots:
        for path in source_root.rglob("*.py"):
            if {"tests", "testing", "__pycache__"} & set(path.parts):
                continue
            if "apps/api" in path.as_posix():
                service = "api"
            else:
                worker = next((part for part in path.parts if part.endswith("_worker")), None)
                service = worker.replace("_", "-") if worker else "package"
            files.append((service, path))
    return tuple(sorted(files, key=lambda item: item[1].as_posix()))


def audit_temporal_wiring(root: Path) -> TemporalWiringAudit:
    clients: set[str] = set()
    workers: set[str] = set()
    bad_clients: list[str] = []
    bad_workers: list[str] = []
    health_connects: list[str] = []
    api_outside_provider: list[str] = []
    for service, path in _temporal_production_files(root):
        tree = ast.parse(path.read_text())
        aliases = _import_aliases(tree)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            qualified = _qualified_name(call.func, aliases)
            if qualified == "temporalio.client.Client.connect":
                clients.add(service)
                if _keyword_call_name(call, "interceptors") != "temporal_client_interceptors":
                    bad_clients.append(f"{path}:{call.lineno}")
                if path == root / "apps/api/src/jhin_api/health/service.py":
                    health_connects.append(f"{path}:{call.lineno}")
                if service == "api" and path != root / "apps/api/src/jhin_api/temporal.py":
                    api_outside_provider.append(f"{path}:{call.lineno}")
            if qualified == "temporalio.worker.Worker":
                workers.add(service)
                if _keyword_call_name(call, "interceptors") != "temporal_worker_interceptors":
                    bad_workers.append(f"{path}:{call.lineno}")
    return TemporalWiringAudit(
        frozenset(clients),
        frozenset(workers),
        tuple(bad_clients),
        tuple(bad_workers),
        tuple(health_connects),
        tuple(api_outside_provider),
    )


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        prefix = call.func.value.id if isinstance(call.func.value, ast.Name) else ""
        return f"{prefix}.{call.func.attr}".strip(".")
    return ""


def workflow_imports() -> set[str]:
    names: set[str] = set()
    for path in (REPO_ROOT / "packages/workflows/src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
    return names


def workflow_calls(name: str) -> list[str]:
    matches: list[str] = []
    for path in (REPO_ROOT / "packages/workflows/src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and _call_name(node) == name:
                matches.append(f"{path}:{node.lineno}")
    return matches


FORBIDDEN_WORKFLOW_IMPORTS = {"jhin_observability", "opentelemetry", "random", "time"}
assert not workflow_imports() & FORBIDDEN_WORKFLOW_IMPORTS
assert not workflow_calls("datetime.now")
```

Temporal's SDK interceptor is allowed to use deterministic `workflow.time_ns()` internally and suppresses replay spans; Jhin workflow source itself does not call telemetry clocks/randomness.

- [ ] **Step 7: Run focused and affected worker suites**

```bash
uv lock
uv run pytest packages/observability/tests/test_temporal.py \
  packages/workflows/tests/test_poller_health.py \
  services/workflow_worker/tests services/agent_worker/tests \
  services/tool_worker/tests services/event_worker/tests \
  services/sandbox_runner/tests -q
uv run ruff check packages/observability apps/api/src services
uv run mypy
```

Expected: PASS; tool queue activity context is connected and workflow replay creates no duplicate application spans.

- [ ] **Step 8: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 6


After applying the draft and the binding Task 1-5 corrections above, replace Task 6 from its
`Files` block through its staging/commit block with the rulings in this section. The Task 6
snippets are illustrative only where this section does not give an exact public signature,
ownership rule, test assertion, path list, or command. Those exact requirements are binding.

### 11.1 Write the complete test seams first and audit the architecture actually prescribed

Before the first RED run, create every Task 6 test helper, fixture, and auditor used by that run,
including the Temporal time-skipping environment, cross-queue probe, history capture/replay,
two-attempt failure helper, in-memory span/metric accessors, and repository wiring audit. Test-only
helpers are part of the test-first step, not deferred production implementation. A missing fixture,
`NameError`, collection/type error, import cycle, strict-global runtime leak, or auditor that
cannot recognize the prescribed helper calls is invalid RED evidence.

Replace the direct-constructor text search with one import-aware semantic audit. It resolves
qualified names through imports/aliases and enforces all of these exact sets:

- `jhin_observability.connect_temporal_client` is called by exactly
  `agent-worker`, `tool-worker`, `event-worker`, and `workflow-worker`.
- `jhin_observability.build_temporal_worker` is called by exactly
  `agent-worker`, `tool-worker`, and `workflow-worker`.
- Direct `Client.connect` is allowed only in
  `jhin_observability/temporal.py`, `jhin_api/temporal.py`, and
  `jhin_workflows/poller_health.py`. Each allowed call forwards the exact list returned by
  `temporal_client_interceptors(runtime)`. Health service and every other API/product module
  contain no direct connect.
- Direct `Worker` construction is allowed only inside
  `jhin_observability.build_temporal_worker`. Each service call forwards, by identity, its local
  initialized `runtime`, its canonical task-queue constant, and its exact workflow and activity
  lists.
- Production code constructs neither `SafeTemporalTracingInterceptor` nor
  `TemporalActivityMetricsInterceptor` outside
  `temporal_client_interceptors` and `temporal_worker_interceptors`.

The audit must report the resolved helper owner, not a generic `package` label, and it must reject
extra/missing services, a local alias that bypasses identity checks, ad-hoc interceptor lists,
direct health connections, and an uninstrumented allowed direct connect.

Rewrite agent/tool registration regressions to patch and capture the shared helper and existing
retry seams. Assert exact runtime, task queue, workflow/activity-list, interceptor-list, and returned
client/worker identity. Compare ordering with the real `connect_with_retry`,
`resources_with_retry`, and `temporal_with_retry` control flow; do not search for nonexistent
direct `Resources.create`, `ToolWorkerResources.create`, or `Worker(...)` calls in
`main.py`.

Update the predecessor bootstrap audit in
`packages/observability/tests/test_log_audit.py` in the same test-first step:

- exactly API, agent worker, tool worker, event worker, workflow worker, and sandbox runner use
  `initialize_observability` before their first side effect/resource;
- exactly the standalone rootless adapter calls `configure_json_logging`, and it has no
  runtime/OTLP initialization; and
- no ordinary service calls `configure_json_logging` or the removed compatibility alias.

`packages/observability/tests/test_logging.py` exercises `configure_json_logging` directly and
asserts `configure_logging` is no longer exported by `jhin_observability`. Preserve the Task 1
rootless event/log-format behavior while removing that one-task alias.

### 11.2 Audit only the deterministic workflow import closure

Do not recursively classify every file below `packages/workflows/src` as replay-sensitive.
Build the deterministic audit from the workflow types actually registered by the worker and walk
only their workflow-side import closure: registered workflow modules, their shared workflow code,
task-queue constants, and relevant package initializers. Explicitly exclude
`poller_health.py` and activity-only implementations; those execute outside replay.

Independently fail if any workflow definition or its replay-sensitive closure imports
`jhin_observability`, OpenTelemetry, `random`, `time`, or a telemetry clock, or calls
`datetime.now`. This exclusion does not relax the production wiring audit for the poller CLI.
Run `packages/workflows/tests/test_phase10_history_replay.py` with the real production worker
interceptor and the frozen Phase 9 history. Normal execution and replay emit no workflow
application span and replay emits no duplicate span.

### 11.3 Reserve, bound, and sanitize Temporal's `_tracer-data` carrier

Keep `TraceContextTextMapPropagator`, but do not rely on the SDK 1.31 default header-copy
behavior. Define one shared
`MAX_TEMPORAL_TRACER_DATA_BYTES = 1_024` limit, measured as
`len(payload.SerializeToString())` for the reserved Temporal protobuf `Payload`. Injection and
extraction use the SDK data-converter seam; no code hand-builds undocumented protobuf bytes.

Every client, workflow-outbound, activity-inbound, and Nexus carrier follows the same rules:

1. Shallow-copy the caller mapping. Never mutate the caller mapping or any unrelated payload.
2. Remove the copied mapping's existing `_tracer-data` before attempting injection.
3. If the current span context is absent or invalid, return the copy without the reserved header;
   never forward stale trace data.
4. Serialize only a canonical `traceparent` plus optional `tracestate`. Reject/drop every other
   carrier key, including `baggage`.
5. Before decoding, require the reserved value to be the expected Temporal `Payload`, require its
   serialized size to be at most 1,024 bytes, and contain decoder/type errors locally. After
   decoding, require a string-to-string mapping with exactly `traceparent` and optional
   `tracestate`, then accept only context that the trace-context propagator validates.
6. Missing, wrong-type, oversized, malformed, decoder-hostile, unknown-key, or invalid trace data
   yields an empty/root parent and never prevents authoritative Temporal work.

Unrelated Temporal application headers remain byte-for-byte authoritative and retain their key,
value object, and serialized payload. Privacy tests inspect the decoded reserved telemetry payload
separately; they must not demand removal of legitimate business headers.

Add regressions for valid `traceparent` plus `tracestate`, baggage removal, invalid/no-current
span, stale reserved data, missing and wrong-type values, the 1,024-byte boundary and one byte over,
malformed/decoder-hostile values, invalid trace syntax, unknown carrier keys, caller immutability,
unrelated-header byte equality, and the complete serialized reserved contents on all four carrier
paths.

### 11.4 Make tracing, context cleanup, and failure metrics strictly fail-open

The tracing wrapper is a diagnostic shell around exactly one downstream invocation:

- an ordinary tracer/context-manager/start/inject failure calls downstream exactly once without
  instrumentation;
- a successful downstream result is returned unchanged even if attribute, status, span-end, or
  detach telemetry fails;
- an original downstream exception is re-raised as the exact same exception with its original
  traceback even if error classification, attribute/status recording, span end, metric lookup, or
  detach fails;
- `asyncio.CancelledError` and other cancellation remain cancellation, are not translated, and do
  not increment `temporal_activity_failures`; and
- attach/detach is tracked per invocation and detach is guarded/best-effort. Never detach an absent,
  foreign, already-detached, or mismatched-async-context token.

`normalize_temporal_attributes` accepts `Attributes | None` and treats `None` as empty before
any `.get`. The activity-metrics interceptor contains counter lookup, failure classification, and
`add()`; a hostile metric backend cannot replace the activity result or exact original failure.
An attempt that reaches an authoritative ordinary activity exception contributes one failure
attempt when metrics are healthy. A telemetry setup failure does not duplicate the activity call.

Use hostile tracer, context manager, propagator, span, detach, counter lookup, classifier, and
counter-add doubles. Prove exact invocation count/result/exception identity and no context leak for
ordinary success, ordinary failure, cancellation, telemetry setup failure, and telemetry teardown
failure.

### 11.5 Pin and test the exact Temporal 1.31 private compatibility surface

Task 6 deliberately keeps the SDK-private tracing hooks, so
`packages/observability/pyproject.toml` depends on exactly
`temporalio==1.31.0`, not `>=1.31,<1.32`. Regenerate `uv.lock` once, assert both the lock and
runtime import version are exactly 1.31.0, and run `uv lock --check`.

Keep these public contracts exact:

~~~python
TemporalInterceptorRole = Literal["client", "worker"]

class SafeTemporalTracingInterceptor(TracingInterceptor):
    def __init__(self, tracer: Tracer, *, role: TemporalInterceptorRole) -> None: ...

class TemporalActivityMetricsInterceptor(temporalio.worker.Interceptor):
    def __init__(self, metrics: JhinMetrics, *, task_queue: str) -> None: ...

def temporal_client_interceptors(
    runtime: ObservabilityRuntime,
) -> list[temporalio.client.Interceptor]: ...

def temporal_worker_interceptors(
    runtime: ObservabilityRuntime,
    *,
    task_queue: str,
) -> list[temporalio.worker.Interceptor]: ...

async def connect_temporal_client(
    settings: ObservabilityTemporalSettings,
    runtime: ObservabilityRuntime,
) -> Client: ...

def build_temporal_worker(
    client: Client,
    *,
    runtime: ObservabilityRuntime,
    task_queue: str,
    workflows: Sequence[type[Any]],
    activities: Sequence[Callable[..., Any]],
) -> Worker: ...
~~~

Validate `role` at runtime and reject anything except `client` or `worker` with a fixed,
non-input-bearing `ValueError`. Use the SDK-compatible callable type accepted by the resolved
`Worker` constructor if its 1.31 annotation is narrower than the shorthand above; do not use bare
`Sequence[type]` or weaken strict typing with unbounded ignores.

Export the two interceptors, two list builders, `connect_temporal_client`,
`build_temporal_worker`, and their public protocol/type names from
`jhin_observability.__init__`. Keep the Task 2 runtime/config public names intact.

Compatibility tests inspect the actual 1.31 objects, not AST import spelling. Assert the resolved
signatures/fields of `TracingInterceptor.__init__`, its overridden
`_start_as_current_span`, `_CompletedWorkflowSpanParams`, the workflow/activity/Nexus
interceptor inputs used here, worker interceptor inheritance and prepend/order behavior, and
`Replayer(..., interceptors=...)`. Exercise start-workflow, signal, activity, and synthetic
Nexus paths with the exact production classes.

### 11.6 Preserve signal/update context without creating workflow spans

Choose the propagation contract, not the narrower terminal-signal alternative. Under SDK 1.31,
workflow start context comes from `params.context`, while signal/update incoming context is
carried by `params.link_context`. Validate that incoming reserved trace-only carrier with section
11.3's safe extractor and run only the signal/update handler's outbound scheduling work under that
context. Emit no workflow span and make no OTel/time/random call from workflow code.

An activity scheduled by a signal/update is parented to that valid incoming signal/update trace,
not the original workflow-start trace. A missing/malformed/oversized signal carrier schedules the
activity from a safe root and still performs work. Add an actual signal-to-activity trace-ID
regression, a distinct-start-versus-signal trace regression, malformed-signal safe-root coverage,
and replay proof with zero workflow spans.

### 11.7 Make all six ordinary service lifecycles transactional

Each ordinary service owns exactly one runtime named with the exact closed service identity:
`api`, `agent-worker`, `tool-worker`, `event-worker`, `workflow-worker`, and
`sandbox-runner`. Establish the enclosing cleanup region immediately after the exact runtime is
created, before provider/resource/client/manager/worker construction. Nested `finally` blocks
attempt every later cleanup and the bounded exact runtime shutdown even when an earlier
construction, run, cancellation, or cleanup step fails.

Every service `Settings` continues to inherit Task 2's `ObservabilitySettings`; no Task 6
service-local environment field or normalizer is allowed. Construct settings first, then initialize
the runtime before the first external side effect with
`settings.observability_config(..., extra_log_processors=(redact_event_dict,))` and the exact
package version: `jhin-api`, `jhin-agent-worker`, `jhin-tool-worker`,
`jhin-event-worker`, `jhin-workflow-worker`, or `jhin-sandbox-runner` as applicable.
Preserve Task 1/2's closed `dev|test|staging|production` environment and Task 1's production
Compose normalization. Every owned runtime shutdown uses the bounded
`timeout_millis=5_000` call. Rootless remains the sole JSON-only process and creates no runtime.

For API, retain every Task 4 invariant: pure-ASGI middleware; the collapsed `"/api/:path*"`
registry; `service_version("jhin-api")`; the Phase 2 lifespan harness; runtime initialization as
the first lifespan side effect; runtime stored in app state; and cleanup order NATS, engine,
`api.stopped`, runtime. Move creation/storage of
`TemporalClientProvider(settings, runtime)` inside the cleanup `try` immediately after runtime
creation; assignment of `app.state.observability` is inside that same `try`. Provider
construction or any app-state assignment failure must still reach runtime shutdown. It is not
authority to move secret/engine construction or replace their exact runtime tracer identity.

For agent, tool, and workflow workers:

- initialize the exact runtime before all resources;
- put client/resource/heartbeat/worker construction inside the cleanup region;
- on exit or startup failure, stop/cancel and await every started worker/heartbeat task, close
  client-owned resources, then shut down the exact runtime;
- keep attempting later cleanup when worker/heartbeat/resource cleanup raises; and
- prove ordinary run, construction failure at every acquisition boundary, cancellation, and
  cleanup failure leave no task/resource/runtime survivor.

`Resources.create(..., runtime=runtime)` and
`ToolWorkerResources.create(..., runtime=runtime)` receive the exact process runtime. Agent
`Resources.create` becomes transactional: if any step after engine acquisition fails, dispose
the engine; if NATS or a publisher was acquired, close it before the engine, even on retry.
Tool resources retain their existing partial-cleanup guarantees while adding exact runtime
identity. Both engines receive `tracer=runtime.tracer`; both product
`EventPublisher` instances receive that same tracer. Recursively prove the engine, publisher,
forwarding constructor, and returned resource graph all retain the exact tracer identity.

Workflow worker establishes cleanup before starting the heartbeat or constructing the worker.
Worker/interceptor construction failure cancels and awaits an already-started heartbeat, then
shuts the runtime. Normal and exceptional teardown cancel **and await** heartbeat/worker tasks;
there is no fire-and-forget cancellation or unretrieved exception.

Task 5 remains sole owner of event-worker runtime initialization, settings inheritance, redactor,
engine/NATS tracers, lag metrics/task, and cleanup. Task 6 passes that one exact existing runtime
through `temporal_with_retry` into `connect_temporal_client`; it creates no second runtime and
does not re-own the event manifest or settings. Tests assert one initialization, runtime identity,
retry identity, and Task 5's consumer/lag teardown order.

### 11.8 Make sandbox app ownership explicit and leak-free

Keep the public factory signature:

~~~python
def create_app(
    settings: Settings | None = None,
    *,
    runtime: ObservabilityRuntime | None = None,
) -> FastAPI: ...
~~~

The factory records whether it owns the runtime. If it initializes one, every
`JobManager`/app/route-install construction failure shuts that runtime immediately, and an
entered lifespan discharges ownership exactly once after manager cleanup. If a caller injects a
runtime, the app never shuts it down on success or failure. There is no module/package-global
runtime owner.

The CLI owns one runtime around the entire `create_app(settings, runtime=runtime)` plus
`uvicorn.run(..., log_config=None)` call and shuts it in `finally`, including factory or server
failure. Existing no-lifespan auth tests create a fresh function-scoped caller-owned runtime,
inject it, and shut it in fixture `finally`; they do not accidentally test an app-owned runtime
without entering lifespan.

In `services/sandbox_runner/tests/test_api_auth.py` and `test_telemetry.py`, cover factory
construction/route-install failure, manager-start failure, manager-close failure, normal
app-owned lifespan, injected ownership, CLI factory/Uvicorn failure, unused/no-lifespan injected
construction, exact-once shutdown, and absence of a reusable global owner after every path.

### 11.9 Keep one API provider and close Temporal health details

Preserve the protected-health handoff exactly:

~~~python
class TemporalClientProvider:
    def __init__(
        self,
        settings: Settings,
        observability: ObservabilityRuntime,
    ) -> None: ...

    async def get(self) -> TemporalClient: ...
~~~

`jhin_api.temporal` owns this provider and the business dependency seam. Its lock produces one
cached interceptor-aware client under concurrency. `health.service` owns
`TemporalHealthUnavailable` and `check_temporal(provider)`; `health.router` reads the one
`app.state.temporal_provider`. Do not introduce a temporal/health circular import.

Add an app-level test that invokes the real `jhin_api.deps.TemporalDep`, readiness route, and the
later protected-health dependency seam and proves all three resolve the exact app-state provider
and one cached client. Calling `provider.get()` twice directly is not sufficient.

`check_temporal` re-raises cancellation but translates connect/RPC/check failures into the fixed
`TemporalHealthUnavailable` before generic `_timed` can serialize `str(exc)`, or otherwise
returns the same closed fixed detail. A unique address/RPC/message canary is absent from the
readiness body, structured JSON logs, span names, attributes, events, and status descriptions.
Keep the deterministic Task 4 lifespan fixtures and later protected-health provider access.

### 11.10 Correct dependencies, File Map, and cross-task handoffs

After Tasks 1-5, Task 6 dependency ownership is exactly:

- `packages/observability/pyproject.toml`: `temporalio==1.31.0`;
- `packages/workflows/pyproject.toml`: direct `jhin-observability` dependency and workspace
  source for the process-local poller CLI;
- `services/workflow_worker/pyproject.toml`: direct `jhin-secrets` dependency and workspace
  source for its redaction processor;
- `uv.lock`: exact Temporal 1.31.0 after one regeneration and `uv lock --check`;
- no Task 6 tool-worker manifest delta because Task 1 already owns observability there; and
- no Task 6 event-worker manifest/settings delta because Task 5 already owns secrets, settings,
  redaction, runtime, tracer/metrics, and lag lifecycle there.

`tests/test_worker_dependency_boundaries.py` preserves Task 1's positive
tool-observability dependency and negative tool agent/model dependency/import assertions. Task 6
adds only the workflows-observability dependency assertion.

Keep the poller CLI's public contract exact:

~~~python
async def queue_has_workflow_poller(
    address: str,
    namespace: str,
    queue: str,
    *,
    runtime: ObservabilityRuntime | None = None,
) -> bool: ...
~~~

An injected runtime is forwarded by identity and never shut down by the helper. Without one, the
helper initializes an owned `temporal-poller-check` runtime with
`service_version("jhin-workflows")` and
`normalize_environment(os.environ.get("APP_ENV", "production"))`, establishes cleanup before
connecting, forwards the exact `temporal_client_interceptors(active_runtime)` list to its one
allowed direct `Client.connect`, and always shuts the owned runtime. Preserve its existing public
arguments, request semantics, live-poller test, injected-list identity regression, and owned
runtime regression that leaves strict `get_runtime()` uninitialized afterward.

In root `pyproject.toml`, retain prior test roots and include
`packages/observability/tests`, `services/tool_worker/tests`, and
`services/workflow_worker/tests` exactly once so root collection exercises the shared boundaries.

Amend the global File Map for these previously absent affected paths:

~~~text
apps/api/tests/test_health.py
services/tool_worker/tests/test_advertised_tools.py
packages/workflows/tests/test_phase10_history_replay.py
services/sandbox_runner/tests/test_api_auth.py
~~~

The first two remain owned by the corrected Task 4 and Task 1 descriptions respectively and are
also affected Task 6 regressions. Task 6 preserves the Task 4 API lifecycle/SQL/middleware
handoff, Task 5's event runtime/NATS lifecycle, and the later protected-health provider contract.
Task 11 must exercise a real connected trace through Temporal into agent/tool work and must retain
the privacy, normalized-name, and no-workflow-span assertions here.

### 11.11 Own lifecycle-scoped tests and run broad gates

`packages/observability/tests/test_temporal.py` defines function-scoped
`TracerProvider`/`SimpleSpanProcessor`/`InMemorySpanExporter` and
`MeterProvider`/reader/`JhinMetrics` fixtures. Every fixture shuts down what it owns in
`finally`, passes the exact tracer/metrics through public seams, and never relies on a global OTel
provider, Task 2's private reset, test ordering, or an unconfigured no-op runtime.

The test-first suite covers all of the following before production rewiring:

- API/client -> workflow -> agent activity -> tool activity trace continuity;
- zero workflow application spans in normal execution and replay;
- exact registered span/activity names plus complete attribute/event/status/carrier privacy scans;
- exactly two attempt-level failure increments for a real two-attempt activity retry;
- exact client/worker interceptor list order, role, tracer/metrics identity, and closed task queue;
- invalid, missing, stale, malformed, oversized, baggage-bearing, and detach-hostile carriers;
- signal/update-to-activity trace propagation and malformed safe-root behavior;
- hostile tracing/propagation/metric implementations preserving exact business behavior;
- frozen Phase 9 replay with the production worker interceptor;
- deterministic import-closure enforcement;
- semantic helper wiring, recursive engine/publisher tracer identity, provider identity/privacy;
- all six service startup/cleanup/cancellation paths and the one Task 5 event runtime; and
- the predecessor log-audit/rootless and tool registration/advertisement regressions.

After the corrected Tasks 1-5 commits exist and all helpers/fixtures/auditors above have been
written, Task 6 RED is:

~~~bash
uv run pytest \
  packages/observability/tests/test_temporal.py \
  packages/observability/tests/test_log_audit.py \
  packages/observability/tests/test_logging.py \
  apps/api/tests/test_temporal_provider.py \
  apps/api/tests/test_health.py \
  packages/workflows/tests/test_poller_health.py \
  packages/workflows/tests/test_phase10_history_replay.py \
  services/workflow_worker/tests/test_telemetry.py \
  services/tool_worker/tests/test_worker_registration.py \
  services/tool_worker/tests/test_advertised_tools.py \
  services/event_worker/tests/test_telemetry.py \
  services/sandbox_runner/tests/test_telemetry.py \
  services/sandbox_runner/tests/test_api_auth.py \
  tests/test_worker_dependency_boundaries.py -q
~~~

Expected RED names absent Temporal interfaces/runtime wiring or failing behavioral assertions.
Undefined helpers/fixtures, unrelated collection/type failures, strict-global leakage, or a
helper/auditor contradiction is invalid RED.

After implementation run:

~~~bash
uv lock
uv lock --check
uv run pytest \
  packages/observability/tests \
  apps/api/tests \
  packages/workflows/tests \
  services/agent_worker/tests \
  services/tool_worker/tests \
  services/event_worker/tests \
  services/workflow_worker/tests \
  services/sandbox_runner/tests \
  tests/test_worker_dependency_boundaries.py -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
~~~

All commands pass. The affected suite is intentionally broad because bootstrap, provider,
interceptor, and service-lifecycle helpers are shared process boundaries.

### 11.12 Make Task 6's 39 paths and committed tree exact

Replace Task 6 `Files` and staging with this exact mirrored array. Remove
`services/tool_worker/pyproject.toml`, `services/event_worker/pyproject.toml`, and
`services/event_worker/src/jhin_event_worker/settings.py`; they have no post-Task-1/5 delta.

~~~bash
set -euo pipefail
task6_paths=(
  apps/api/src/jhin_api/deps.py
  apps/api/src/jhin_api/health/router.py
  apps/api/src/jhin_api/health/service.py
  apps/api/src/jhin_api/main.py
  apps/api/src/jhin_api/temporal.py
  apps/api/tests/test_health.py
  apps/api/tests/test_temporal_provider.py
  packages/observability/pyproject.toml
  packages/observability/src/jhin_observability/__init__.py
  packages/observability/src/jhin_observability/logging.py
  packages/observability/src/jhin_observability/temporal.py
  packages/observability/tests/test_log_audit.py
  packages/observability/tests/test_logging.py
  packages/observability/tests/test_temporal.py
  packages/workflows/pyproject.toml
  packages/workflows/src/jhin_workflows/poller_health.py
  packages/workflows/tests/test_phase10_history_replay.py
  packages/workflows/tests/test_poller_health.py
  pyproject.toml
  services/agent_worker/src/jhin_agent_worker/main.py
  services/agent_worker/src/jhin_agent_worker/resources.py
  services/agent_worker/src/jhin_agent_worker/settings.py
  services/event_worker/src/jhin_event_worker/main.py
  services/event_worker/tests/test_telemetry.py
  services/sandbox_runner/src/jhin_sandbox_runner/main.py
  services/sandbox_runner/src/jhin_sandbox_runner/settings.py
  services/sandbox_runner/tests/test_api_auth.py
  services/sandbox_runner/tests/test_telemetry.py
  services/tool_worker/src/jhin_tool_worker/main.py
  services/tool_worker/src/jhin_tool_worker/resources.py
  services/tool_worker/src/jhin_tool_worker/settings.py
  services/tool_worker/tests/test_advertised_tools.py
  services/tool_worker/tests/test_worker_registration.py
  services/workflow_worker/pyproject.toml
  services/workflow_worker/src/jhin_workflow_worker/main.py
  services/workflow_worker/src/jhin_workflow_worker/settings.py
  services/workflow_worker/tests/test_telemetry.py
  tests/test_worker_dependency_boundaries.py
  uv.lock
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task6_paths[@]}"
git diff --check -- "${task6_paths[@]}"
git add -- "${task6_paths[@]}"
expected_index="$(printf '%s\n' "${task6_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task6_paths[@]}"
git commit --only "${task6_paths[@]}" \
  -m "feat(observability): trace Temporal service boundaries"
test "$(git show -s --format=%s HEAD)" = \
  "feat(observability): trace Temporal service boundaries"
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)"
test "$actual_commit_paths" = "$expected_index"
test -z "$(git diff --cached --name-only)"
~~~

The Task 6 `Files` block and array are exact mirrors. No additional Task 6 path is authorized.
The global index-only exception still applies: pre-existing unstaged work outside these 39 paths
may remain, but any pre-staged path, unexpected staged path, missing expected path, path outside
`Files`, commit-tree mismatch, or non-empty post-commit index fails closed.

### Task 7: Instrument Agent, Model, Tool, and Trigger Commit Boundaries

**Files:**
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/models/router.py`
- Modify: `apps/api/src/jhin_api/models/service.py`
- Modify: `apps/api/tests/test_model_telemetry.py`
- Modify: `apps/api/tests/test_webhooks_unit.py`
- Modify: `packages/models/pyproject.toml`
- Modify: `packages/models/src/jhin_models/factory.py`
- Modify: `packages/models/src/jhin_models/telemetry.py`
- Modify: `packages/models/tests/test_factory.py`
- Modify: `packages/models/tests/test_telemetry.py`
- Modify: `packages/tools/pyproject.toml`
- Modify: `packages/tools/src/jhin_tools/telemetry.py`
- Modify: `packages/tools/tests/test_telemetry.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/projections.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/reasoning.py`
- Modify: `services/agent_worker/tests/test_delegation_activities.py`
- Modify: `services/agent_worker/tests/test_phase9_invocation_activity.py`
- Modify: `services/agent_worker/tests/test_reasoning_manifest.py`
- Modify: `services/agent_worker/tests/test_step_projection.py`
- Modify: `services/agent_worker/tests/test_telemetry.py`
- Modify: `services/agent_worker/tests/test_upgrade_crash_barriers.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/matcher.py`
- Modify: `services/event_worker/tests/test_matcher.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/activities.py`
- Modify: `services/tool_worker/tests/test_advertised_tools.py`
- Modify: `services/tool_worker/tests/test_bound_approval.py`
- Modify: `services/tool_worker/tests/test_bound_tool_execution.py`
- Modify: `services/tool_worker/tests/test_telemetry.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes the accepted Task 6 handoff and produces the exact Task 7 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing provider-attempt/body-exclusion tests**

```python
@pytest.mark.asyncio
async def test_model_attempt_records_safe_metadata_only(
    metrics: JhinMetrics,
    spans: InMemorySpanExporter,
    tracer: Tracer,
) -> None:
    prompt_canary = "prompt-canary-must-not-export"
    completion_canary = "completion-canary-must-not-export"
    raw = FakeModelClient(response=ModelResponse(text=completion_canary, latency_ms=17))
    client = InstrumentedModelClient(raw, provider_type="openai", metrics=metrics, tracer=tracer)
    await client.generate(
        ModelRequest(
            model="private-model-name", messages=(ModelMessage(role="user", content=prompt_canary),)
        )
    )
    assert metric_sum("model_requests_total", provider_type="openai", outcome="ok") == 1
    span = next(span for span in spans.get_finished_spans() if span.name == "model.request")
    assert dict(span.attributes) == {
        "jhin.provider_type": "openai",
        "jhin.operation": "generate",
        "jhin.retry_count": 0,
        "jhin.outcome": "ok",
        "jhin.latency_ms": 17,
    }
    rendered = export_payload(spans, metrics)
    assert prompt_canary not in rendered
    assert completion_canary not in rendered
    assert "private-model-name" not in rendered


@pytest.mark.asyncio
async def test_failed_completed_attempt_counts_once_without_provider_body(
    metrics: JhinMetrics,
    spans: InMemorySpanExporter,
    tracer: Tracer,
) -> None:
    client = InstrumentedModelClient(
        FakeModelClient(error=ModelProviderError("HTTP 500 provider-body-canary", retryable=True)),
        provider_type="anthropic",
        metrics=metrics,
        tracer=tracer,
    )
    with pytest.raises(ModelProviderError):
        await client.generate(model_request())
    assert metric_sum("model_requests_total", provider_type="anthropic", outcome="failed") == 1
    assert "provider-body-canary" not in export_payload(spans, metrics)


@pytest.mark.asyncio
async def test_model_package_default_is_explicit_noop_before_bootstrap() -> None:
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    client = InstrumentedModelClient(
        FakeModelClient(response=ModelResponse(text="ok", latency_ms=1)),
        provider_type="openai",
        metrics=noop_metrics(),
    )
    assert (await client.generate(model_request())).text == "ok"


@pytest.mark.asyncio
async def test_api_provider_verification_uses_app_owned_metrics(
    api_app: FastAPI, admin_client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    observed: list[tuple[JhinMetrics, Tracer]] = []

    def fake_build_model_client(
        *args: object, metrics: JhinMetrics, tracer: Tracer, **kwargs: object
    ) -> FakeModelClient:
        observed.append((metrics, tracer))
        return FakeModelClient(verify_result="ok")

    monkeypatch.setattr(model_service, "build_model_client", fake_build_model_client)
    response = await admin_client.post(provider_verify_path())
    assert response.status_code == 200
    assert observed == [
        (
            api_app.state.observability.metrics,
            api_app.state.observability.tracer,
        )
    ]
```

Run RED:

```bash
uv run pytest packages/models/tests/test_telemetry.py -q
```

Expected: FAIL because `InstrumentedModelClient` and model instruments do not exist.

- [ ] **Step 2: Implement provider-neutral model instrumentation**

Add `jhin-observability` to `jhin-models`. `build_model_client` constructs the raw adapter exactly as now, then returns:

```python
class InstrumentedModelClient(ModelClient):
    def __init__(
        self,
        wrapped: ModelClient,
        *,
        provider_type: str,
        metrics: JhinMetrics,
        tracer: Tracer | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._provider_type = normalize_provider_type(provider_type)
        self._metrics = metrics
        self._tracer = tracer if tracer is not None else noop_tracer()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        with safe_span(
            "model.request",
            tracer=self._tracer,
            kind=SpanKind.CLIENT,
            attributes={
                "jhin.provider_type": self._provider_type,
                "jhin.operation": "generate",
                "jhin.retry_count": 0,
            },
        ) as span:
            try:
                response = await self._wrapped.generate(request)
            except Exception as exc:
                self._metrics.counter("model_requests_total").add(
                    1, provider_type=self._provider_type, outcome="failed"
                )
                span.set_attribute("jhin.outcome", "failed")
                record_span_error(span, safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE))
                raise
            self._metrics.counter("model_requests_total").add(
                1, provider_type=self._provider_type, outcome="ok"
            )
            span.set_attribute("jhin.outcome", "ok")
            span.set_attribute("jhin.latency_ms", max(0, response.latency_ms))
            return response
```

Extend the factory with `metrics: JhinMetrics | None = None` and `tracer: Tracer | None = None`;
`None` uses `noop_metrics()`/`noop_tracer()` and never reads global bootstrap state or creates a
provider/exporter, preserving package-only and host tests. Apply the same safe attempt semantics to
`stream` and `verify`; never traverse `ModelRequest`, `ModelResponse.text`, tool calls, `extra`, model
name, provider request ID, endpoint, or exception text for telemetry. Every API/worker call site
passes both `runtime.metrics` and `runtime.tracer` explicitly. Add an AST regression over
`apps/api/src` and `services/*/src` that rejects a production `build_model_client` call missing either
keyword; `test_model_package_default_is_explicit_noop_before_bootstrap` proves the standalone path.

```python
def test_every_long_lived_model_factory_call_supplies_metrics_and_tracer() -> None:
    roots = (REPO_ROOT / "apps/api/src", REPO_ROOT / "services")
    failures: list[str] = []
    for path in (candidate for root in roots for candidate in root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not ast.unparse(call.func).endswith("build_model_client"):
                continue
            keywords = {keyword.arg for keyword in call.keywords}
            if not {"metrics", "tracer"} <= keywords:
                failures.append(f"{path}:{call.lineno}")
    assert failures == []
```

Place this test in `packages/models/tests/test_telemetry.py` with explicit `ast` and `Path` imports.

The API never calls `get_runtime()`. Add an app-state dependency and thread it through provider verification:

```python
def get_observability_runtime(request: Request) -> ObservabilityRuntime:
    runtime = getattr(request.app.state, "observability", None)
    if not isinstance(runtime, ObservabilityRuntime):
        raise RuntimeError("API observability runtime is unavailable")
    return runtime


ObservabilityRuntimeDep = Annotated[ObservabilityRuntime, Depends(get_observability_runtime)]


def _build_verification_client(
    provider: ModelProvider,
    api_key: str | None,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> ModelClient:
    return build_model_client(
        provider.type,
        base_url=provider.base_url,
        api_key=api_key,
        metrics=metrics,
        tracer=tracer,
    )


@providers_router.post("/{provider_id}/verify")
async def verify_provider_route(
    provider_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    crypto: SecretCryptoDep,
    runtime: ObservabilityRuntimeDep,
) -> ProviderVerifyResult:
    ok, detail = await service.verify_provider(
        db,
        crypto,
        ctx,
        provider_id,
        runtime.metrics,
        runtime.tracer,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return ProviderVerifyResult(ok=ok, detail=detail)
```

Add `metrics: JhinMetrics, tracer: Tracer` immediately after `provider_id` in the existing
`service.verify_provider` signature and replace its existing `build_model_client(...)` expression
with `_build_verification_client(provider, api_key, metrics, tracer)`; all existing lookup,
decryption, verification, audit, and commit statements stay byte-for-byte unchanged.

- [ ] **Step 3: Write failing durable run/token/cost metric tests**

```python
@pytest.mark.asyncio
async def test_reasoning_span_ends_after_manifest_commit(
    reasoning: AgentReasoningActivities,
    session_factory: SessionFactory,
) -> None:
    result = await reasoning.reason_agent_step_activity(reason_input_with_two_tools())
    spans = finished_spans_named("agent.reason_step")
    assert len(spans) == 1
    async with session_factory() as session:
        rows = await bound_manifest_rows(session, result.run_id, result.step_index)
    assert [row.ordinal for row in rows] == [0, 1]
    assert spans[0].end_time >= max(row.created_at for row in rows).timestamp() * 1_000_000_000


@pytest.mark.asyncio
async def test_committed_usage_and_cost_are_not_double_counted_on_activity_replay() -> None:
    params = committed_reason_step(
        input_tokens=11, output_tokens=7, cached_tokens=3, cost_micros=250_000
    )
    first = await reasoning.reason_agent_step_activity(params)
    replay = await reasoning.reason_agent_step_activity(params)
    assert replay == first
    assert metric_sum("model_tokens_total", provider_type="openai", direction="input") == 11
    assert metric_sum("model_tokens_total", provider_type="openai", direction="output") == 7
    assert metric_sum("model_tokens_total", provider_type="openai", direction="cached") == 3
    assert metric_sum("model_cost_estimate", provider_type="openai") == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_terminal_run_metrics_use_persisted_timestamps_once() -> None:
    await projections.finalize_run_projection_activity(finalize_params(status="failed"))
    await projections.finalize_run_projection_activity(finalize_params(status="failed"))
    assert metric_sum("agent_runs_total", service="agent-worker", outcome="failed") == 1
    assert metric_sum("agent_run_failures_total", failure_class="internal") == 1
    assert histogram_count("agent_run_duration_seconds", outcome="failed") == 1
```

Run RED:

```bash
uv run pytest services/agent_worker/tests/test_telemetry.py -q
```

Expected: FAIL because reasoning/projection commit boundaries do not record telemetry.

- [ ] **Step 4: Emit reasoning and committed run metrics after transaction success**

Start `agent.reason_step` in `AgentReasoningActivities.reason_agent_step_activity` after validated workspace/task/run/correlation state is loaded and bound. End it only after the single transaction that binds the complete ordered lossless manifest has committed. On a pre-bind/model failure, record only a safe code; never attach prompt, completion, arguments, advertised schemas, model/provider request ID, or manifest contents.

Capture `already_committed` before returning a durable step result. Only the invocation that performs the successful commit records token/cost metrics:

```python
def record_committed_model_usage(
    metrics: JhinMetrics,
    *,
    provider_type: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_micros: int,
) -> None:
    provider = normalize_provider_type(provider_type)
    for direction, value in (
        ("input", input_tokens),
        ("output", output_tokens),
        ("cached", cached_tokens),
    ):
        if value > 0:
            metrics.counter("model_tokens_total").add(
                value, provider_type=provider, direction=direction
            )
    if cost_micros > 0:
        metrics.counter("model_cost_estimate").add(cost_micros / 1_000_000, provider_type=provider)
```

In `AgentProjectionActivities.finalize_run_projection_activity`, lock/read the row and set `transitioned_to_terminal = current_status not in RUN_TERMINAL_STATUSES`. Commit the persisted timestamps/status first. Only when that boolean is true, record `agent_runs_total`, duration from persisted `started_at`/`completed_at`, and failure class. A crash after commit may lose diagnostic data; a retry must not duplicate it. Telemetry never introduces a durable outbox or changes the transaction.

- [ ] **Step 5: Write failing terminal tool/replay metric tests**

```python
@pytest.mark.asyncio
async def test_committed_tool_outcome_counts_once_across_replay() -> None:
    first = await activities.execute_bound_tool_activity(
        bound_call("github.issue.comment", risk="elevated")
    )
    replay = await activities.execute_bound_tool_activity(
        bound_call("github.issue.comment", risk="elevated")
    )
    assert first.tool_call_id == replay.tool_call_id
    assert (
        metric_sum("tool_calls_total", tool_family="github", risk="elevated", outcome="completed")
        == 1
    )


@pytest.mark.asyncio
async def test_execution_unknown_records_terminal_failure_without_identifier_label() -> None:
    await activities.execute_bound_tool_activity(bound_call_that_becomes_unknown())
    assert (
        metric_sum(
            "tool_call_failures_total", tool_family="github", failure_class="execution_unknown"
        )
        == 1
    )
    assert "tool_call_id" not in exported_metric_attributes()
```

Run RED:

```bash
uv run pytest packages/tools/tests/test_telemetry.py \
  services/tool_worker/tests/test_telemetry.py -q
```

Expected: FAIL because post-commit tool recording and replay suppression are absent.

- [ ] **Step 6: Implement tool-family normalization and post-commit recording**

Add `jhin-observability` to `jhin-tools` and implement:

```python
def tool_family(tool_name: str) -> str:
    prefix = tool_name.partition(".")[0]
    return (
        prefix
        if prefix in {"system", "organization", "github", "linear", "vercel", "supabase", "cli"}
        else "other"
    )


def record_committed_tool_outcome(metrics: JhinMetrics, outcome: GatewayOutcome) -> None:
    if outcome.replayed:
        return
    family = tool_family(outcome.tool_name)
    normalized_outcome = normalize_tool_outcome(outcome.status)
    metrics.counter("tool_calls_total").add(
        1, tool_family=family, risk=outcome.risk or "other", outcome=normalized_outcome
    )
    if normalized_outcome in {"failed", "denied", "rejected", "execution_unknown"}:
        metrics.counter("tool_call_failures_total").add(
            1, tool_family=family, failure_class=tool_failure_class(outcome)
        )
```

Call this inside `ToolActivities.execute_bound_tool_activity` or `resolve_bound_tool_approval_activity` only after the internal `GatewayOutcome` has committed/reloaded the terminal `ToolCall`; the internal `GatewayOutcome.replayed` flag suppresses the second metric even though the public `BoundToolResult` remains unchanged. `needs_approval` and `executing` are nonterminal and emit neither counter. Create safe spans `tool.gateway.execute` and `tool.approval.resolve` using family/risk/outcome and validated task/run/correlation IDs only. Never attach tool name when it is not a registered catalog name, manifest arguments, sanitized input/output (sanitized product data still does not belong in traces), approval payload, connector response, or error detail.

- [ ] **Step 7: Write failing durable trigger metric tests**

```python
@pytest.mark.asyncio
async def test_handle_event_counts_started_duplicate_and_failed_after_commits(
    matcher: TriggerMatcher, trigger_case: TriggerTelemetryCase, session_factory: SessionFactory
) -> None:
    started_event = trigger_case.event_for("github", external_id="issue-1")
    await matcher.handle_event(started_event)
    await matcher.handle_event(started_event)
    await matcher.handle_event(
        trigger_case.event_for_missing_agent("github", external_id="issue-2")
    )

    async with session_factory() as session:
        statuses = list(
            await session.scalars(
                select(TriggerInvocation.status).order_by(TriggerInvocation.created_at)
            )
        )
    assert statuses == ["started", "duplicate", "failed"]
    assert metric_sum("trigger_invocations_total", connector_type="github", outcome="started") == 1
    assert (
        metric_sum("trigger_invocations_total", connector_type="github", outcome="duplicate") == 1
    )
    assert metric_sum("trigger_invocations_total", connector_type="github", outcome="failed") == 1
    assert (
        metric_sum("trigger_failures_total", connector_type="github", failure_class="target") == 1
    )


@pytest.mark.asyncio
async def test_two_failed_deliveries_create_and_count_two_fresh_invocations(
    matcher: TriggerMatcher,
    trigger_case: TriggerTelemetryCase,
    temporal: FailingTemporalClient,
    session_factory: SessionFactory,
) -> None:
    temporal.fail_with = ConnectionError("provider-body-canary")
    for _delivery in range(2):
        with pytest.raises(ConnectionError):
            await matcher.handle_event(trigger_case.event_for("linear", external_id="issue-3"))
    async with session_factory() as session:
        statuses = list(
            await session.scalars(
                select(TriggerInvocation.status).order_by(TriggerInvocation.created_at)
            )
        )
    assert statuses == ["failed", "failed"]
    assert metric_sum("trigger_invocations_total", connector_type="linear", outcome="started") == 2
    assert metric_sum("trigger_invocations_total", connector_type="linear", outcome="failed") == 2
    assert (
        metric_sum("trigger_failures_total", connector_type="linear", failure_class="dispatch") == 2
    )
```

Define the test seam in `services/event_worker/tests/test_telemetry.py` rather than using
module globals or an implied factory:

```python
@dataclass(frozen=True)
class TriggerTelemetryCase:
    workspace_id: UUID
    valid_trigger_id: UUID
    missing_agent_trigger_id: UUID

    def _event(self, connector_type: str, external_id: str, trigger_id: UUID) -> EventEnvelope:
        return EventEnvelope(
            event_id=new_uuid7(),
            event_type=f"connector.{connector_type}.issue.updated",
            workspace_id=str(self.workspace_id),
            correlation_id=new_uuid7(),
            source=EventSource(type=connector_type),
            data={
                "external_id": external_id,
                "trigger_test_id": str(trigger_id),
                "state": {"name": "Todo"},
                "changed_from": {"state": {"name": "Backlog"}},
            },
        )

    def event_for(self, connector_type: str, *, external_id: str) -> EventEnvelope:
        return self._event(connector_type, external_id, self.valid_trigger_id)

    def event_for_missing_agent(self, connector_type: str, *, external_id: str) -> EventEnvelope:
        return self._event(connector_type, external_id, self.missing_agent_trigger_id)


class FailingTemporalClient:
    def __init__(self) -> None:
        self.fail_with: Exception | None = None

    async def start_workflow(self, *args: object, **kwargs: object) -> object:
        if self.fail_with is not None:
            raise self.fail_with
        return object()
```

The `trigger_case` fixture inserts two enabled trigger rows whose JSON filters match the
corresponding `trigger_test_id`: one targets an active agent and one targets a missing agent.
It commits those rows before constructing `TriggerMatcher`; the `temporal` fixture passes its
single `FailingTemporalClient` instance to that constructor. This makes every name used above
concrete and ensures all three outcomes exercise `TriggerMatcher.handle_event` itself.

- [ ] **Step 8: Run trigger RED**

```bash
uv run pytest services/event_worker/tests/test_telemetry.py -q
```

Expected: FAIL because committed trigger transitions do not emit metrics.

- [ ] **Step 9: Implement durable trigger metrics**

Inject metrics at construction and pass the service-owned facade from event-worker:

```python
class TriggerMatcher:
    def __init__(
        self,
        session_factory: SessionFactory,
        temporal: TemporalClient,
        *,
        metrics: JhinMetrics,
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._temporal = temporal
        self._metrics = metrics
        self._cache_ttl_seconds = cache_ttl_seconds

    def _record_invocation(self, connector_type: str, outcome: str) -> None:
        self._metrics.counter("trigger_invocations_total").add(
            1,
            connector_type=normalize_connector_type(connector_type),
            outcome=outcome,
        )

    def _record_failure(self, connector_type: str, failure_class: str) -> None:
        self._metrics.counter("trigger_failures_total").add(
            1,
            connector_type=normalize_connector_type(connector_type),
            failure_class=failure_class,
        )


matcher = TriggerMatcher(
    session_factory,
    temporal,
    metrics=runtime.metrics,
    cache_ttl_seconds=settings.trigger_cache_ttl_seconds,
)
```

Add this method to `TriggerMatcher` and call it at the four exact commit sites below. It makes the
durability-before-metric ordering executable in one place:

```python
async def _commit_invocation_transition(
    self,
    session: AsyncSession,
    *,
    connector_type: str,
    outcome: Literal["started", "duplicate", "failed"],
    failure_class: Literal["target", "dispatch"] | None = None,
) -> None:
    await session.commit()
    self._record_invocation(connector_type, outcome)
    if failure_class is not None:
        if outcome != "failed":
            raise AssertionError("failure_class requires failed outcome")
        self._record_failure(connector_type, failure_class)
```

Replace the duplicate branch commit with
`await self._commit_invocation_transition(session, connector_type=envelope.source.type,
outcome="duplicate")` immediately before `return None`. Replace the missing-agent commit with the
same call using `outcome="failed", failure_class="target"` immediately before `return`. Replace the
normal started commit with the same call using `outcome="started"`. In the Temporal dispatch
exception transaction, require
`assert row is not None and row.status == TriggerInvocationStatus.STARTED.value`, assign
`row.status = TriggerInvocationStatus.FAILED.value`, assign
`row.error = safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE).code.value`, then call the
helper with `outcome="failed", failure_class="dispatch"`. Import `Literal` from `typing`; no other
commit or metric call remains in those branches.

The failure row persists only the closed error code. Each redelivery invokes `TriggerMatcher.handle_event` again; because the preceding row is now failed, the existing started-row partial uniqueness rule permits a fresh invocation. Do not add once-per-idempotency-key telemetry state or suppress metrics across two genuine deliveries. Create `trigger.dispatch` spans around the Temporal client start with connector type/outcome only. Do not attach trigger ID, idempotency key, external ID, workflow ID, event ID, workspace ID as a metric label, event data, or provider URL.

- [ ] **Step 10: Run focused GREEN and affected tests**

```bash
uv lock
uv run pytest packages/models/tests/test_telemetry.py packages/tools/tests/test_telemetry.py \
  apps/api/tests/test_model_telemetry.py \
  services/agent_worker/tests/test_telemetry.py services/tool_worker/tests/test_telemetry.py \
  services/event_worker/tests/test_telemetry.py -q
uv run pytest packages/models/tests packages/tools/tests services/agent_worker/tests \
  services/tool_worker/tests services/event_worker/tests -q
uv run ruff check packages/models packages/tools services/agent_worker \
  services/tool_worker services/event_worker apps/api/src/jhin_api/models \
  apps/api/tests/test_model_telemetry.py
uv run mypy
```

Expected: PASS; repeated activity execution reuses durable results without duplicate committed counters.

- [ ] **Step 11: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 7


After applying the draft and corrected Tasks 1-6, replace Task 7 from its `Files` block through
its staging/commit block with this section. Task 7 consumes the exact predecessor-owned runtimes
and durable transactions. It may add diagnostics around those authorities, but it may not
rediscover a runtime, weaken an existing product contract, or turn a diagnostic failure into a
product failure.

### 12.1 Define every lifecycle-owned test seam before RED

Before any Task 7 RED command, define every helper and fixture named by that command in its owning
test file:

- `packages/models/tests/test_telemetry.py` owns concrete `metrics`, `spans`, `tracer`,
  `metric_sum`, complete-export serialization, fake model clients/iterators, and model-request
  builders.
- `apps/api/tests/test_model_telemetry.py` owns its real app, authentication/database/crypto
  overrides, and admin HTTP client; it does not rely on Task 4's file-local fixtures.
- `services/agent_worker/tests/test_telemetry.py` owns reasoning/projection objects, session
  factory, complete bound-manifest rows, span/metric/histogram readers, committed-step inputs, and
  finalization parameters.
- package/tool-worker telemetry tests own activity/resource/catalog/bound-call, replay,
  execution-unknown, metric, span, and complete-export fixtures.
- event-worker telemetry/matcher tests own the database, matching trigger rows, app-runtime
  metrics/tracer, one identity-preserving fake Temporal client, post-commit barrier, exporter, and
  safe serialization helpers.

Each span-recording file owns one function-scoped
`TracerProvider` + `SimpleSpanProcessor` + `InMemorySpanExporter`. Each metric-recording file
owns one function-scoped `MeterProvider` + `InMemoryMetricReader` + Task 3
`JhinMetrics`. Teardown is in `finally`, closes providers/readers/runtimes/iterators it owns,
and proves there is no surviving current context, provider, task, or runtime. Tests may not import
a sibling tree's package-local conftest, replace a process-global OTel provider, depend on test
order, or call strict `get_runtime()` without owning initialization.

The API regression constructs the real Task 4 app with deterministic exporter-endpoint-absent
settings, enters `app.router.lifespan_context(app)`, then enters its HTTP client inside that
lifespan. Override only the route's authentication, database, and crypto seams. Invoke the real
provider-verification route and prove its dependency passes the exact
`app.state.observability.metrics` and `.tracer`; direct route-function invocation is
insufficient. Lifespan exit must detach and shut down that exact app runtime.

Expected RED is an absent production wrapper/runtime binding/committed-transition helper,
reconciliation gap, or failing behavioral/privacy assertion. `NameError`, missing fixture,
abstract-class construction, import/collection/type error, global runtime/provider leakage, or an
undefined auditor is invalid RED.

### 12.2 Implement the complete `ModelClient` protocol behind exactly one wrapper

`InstrumentedModelClient` must be concrete against the real abstract base:

~~~python
class InstrumentedModelClient(ModelClient):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    def stream(self, request: ModelRequest) -> AsyncIterator[str]: ...
    async def verify(self) -> str: ...
    async def close(self) -> None: ...
~~~

Use these exact behavioral contracts:

1. `generate` and `verify` invoke the wrapped method exactly once. After the attempt begins,
   normal return records one `ok` provider attempt and returns the exact object/value. Ordinary
   failure records one `failed` attempt and re-raises the exact exception with its traceback.
   Cancellation records only the closed `cancelled` outcome and remains the same cancellation.
2. `stream` remains a synchronous `def` returning one internal async generator. Creating but
   never iterating it creates neither wrapped iterator, span, nor metric. Span/attempt ownership
   begins on first iteration, stays current across every yielded chunk, and never buffers,
   inspects, joins, or re-traverses chunks. Normal exhaustion records one `ok`; ordinary iterator
   failure records one `failed`; early `aclose`/`GeneratorExit` and cancellation record one
   `cancelled`. Every started path closes the wrapped iterator exactly once in `finally`.
3. `close` delegates exactly once without telemetry and preserves the wrapped close result,
   cancellation, and exception exactly.
4. `KeyboardInterrupt` and `SystemExit` are never converted to model failures. A tracer,
   span, normalizer, or metric diagnostic failure cannot mask a result or the original
   exception/cancellation. If wrapped-iterator cleanup is the only product failure, propagate it
   exactly; if work is already unwinding an exception/cancellation, cleanup may not replace that
   original authority.

Tests cover generate success/failure/cancellation; verify success/failure/cancellation; stream
full exhaustion, ordinary failure, early close, cancellation, never-consumed behavior, current
context during yields, and exact wrapped-iterator cleanup; plus close success/failure/cancellation.
Complete span, resource, event, status, and metric serialization must exclude prompt, completion,
every chunk, model name, tool schema/call/arguments, request ID, endpoint/base URL, provider body,
response metadata, and exception canaries.

`build_model_client(..., metrics: JhinMetrics | None = None, tracer: Tracer | None = None)`
selects the same raw adapter as before and always returns exactly one
`InstrumentedModelClient`, including the explicit package-only no-op default. `None` resolves
only to `noop_metrics()`/`noop_tracer()`; it never reads global runtime state or constructs a
provider/exporter. Every production caller supplies both exact handles.

Update `packages/models/tests/test_factory.py`: assert the correct one of all five raw adapters
is behind exactly one wrapper, package defaults are explicit no-op, no nested wrapper exists, and
`close` delegates. Remove exact-type assertions that require the raw adapter itself to be the
factory return.

`packages/models/pyproject.toml` declares both the `jhin-observability` workspace dependency
and direct `opentelemetry-api>=1.38,<2` dependency used by production `Tracer`/`SpanKind`
imports. A transitive OTel import is not acceptable.

### 12.3 Bind every production handle to the predecessor-owned runtime

API model verification obtains only `request.app.state.observability` through the one
`ObservabilityRuntimeDep`; it never calls `get_runtime()`. Preserve
`get_observability_runtime(request) -> ObservabilityRuntime` as the app-state validation seam.
Thread `runtime.metrics` and `runtime.tracer` through the real route and service helper into
`build_model_client` while leaving provider lookup/decryption, audit, commit, and returned
verification detail unchanged.

In the existing `service.verify_provider` signature, insert
`metrics: JhinMetrics, tracer: Tracer` immediately after `provider_id`. Keep request/audit
keywords and every existing statement otherwise intact. The one
`_build_verification_client(provider, api_key, metrics, tracer) -> ModelClient` helper forwards
both exact objects to the package factory.

Task 6's `Resources.runtime` is the sole agent source. Composite `AgentActivities`,
`AgentReasoningActivities`, and `AgentProjectionActivities` receive/bind the exact
`runtime.metrics` and `runtime.tracer`; their model factory call uses those objects by identity.
Task 6's `ToolWorkerResources.runtime` is the sole tool source, and
`ToolActivities` binds its exact handles. Narrow existing resource doubles in the affected tests
gain explicit validated no-op test telemetry; production code gets no
`getattr(resource, ..., noop_*)` fallback.

Task 5's one event-worker runtime remains sole owner. Change the public matcher constructor to
require both handles:

~~~python
class TriggerMatcher:
    def __init__(
        self,
        session_factory: SessionFactory,
        temporal: TemporalClient,
        *,
        metrics: JhinMetrics,
        tracer: Tracer,
        cache_ttl_seconds: float = 5.0,
    ) -> None: ...
~~~

Event-worker main passes `runtime.metrics` and `runtime.tracer` by identity. Matcher code does
not call strict global `safe_span()`, initialize a runtime, or shut one down. Preserve Task 5's
NATS consumer/lag lifetime and Task 6's Temporal interceptor/client identity.

Replace the suffix AST search with an import-aware semantic audit that resolves only
`jhin_models.build_model_client` and
`jhin_models.factory.build_model_client`. Exact production owners are
`jhin_api.models.service` and `jhin_agent_worker.reasoning`; each passes the exact local
runtime-owned metrics/tracer. Reject an alias used to omit/default/swap a handle, an extra product
caller, and any missing handle. Do not classify the agent's local
`self._build_model_client(...)` test seam as the package factory. Add mutation tests for imported
aliasing, omitted metrics/tracer, swapped handles, an extra caller, and the valid local seam.

### 12.4 Make predecessor and Task 7 telemetry failures diagnostic-only

Before Task 7 implementation, strengthen Task 2 `safe_span` and Task 3 bound metrics inside their
already-owned source/test paths; they are not extra Task 7 paths:

- Task 2 validates fixed span name/key/value schemas before product invocation. A developer-invalid
  telemetry schema remains a deterministic validation failure in tests. Once valid, contain tracer
  and span enter, late normalized set, error recording, end, and context detach backend failures.
  Invoke downstream exactly once and preserve its exact result, exception/traceback, or
  cancellation.
- Task 3 validates instrument name/kind, measurement, and complete labels before recording. Invalid
  caller data remains strict. Contain only the SDK/backend `add`/`record` failure after
  validation so exporter/recorder outages are diagnostic.

Task 7 helpers add their own containment around injected facade/tracer protocol doubles. Hostile
tracer/span/metric/normalizer failures at model, agent, tool, and trigger boundaries cannot skip or
repeat model, database, gateway, or Temporal work and cannot change durable rows, returned product
values, public errors, retry/approval/authorization behavior, or cancellation.

Outside the model wrapper's explicitly closed `cancelled` attempt outcome, cancellation is never
counted as an ordinary Task 7 failure. Every boundary re-raises it unchanged.

For every boundary, test hostile construction/enter/set/error/end/detach and metric
lookup/add/record behavior on success, ordinary failure, and cancellation. Assert exact downstream
call count, exact result/exception identity and traceback, the same durable business state as the
no-telemetry control, and an empty context afterward.

In trigger dispatch failure handling, never use an `assert` that can replace the Temporal error.
Update the row only when the exact expected authoritative started row is present. Contain
diagnostic or secondary update failures and re-raise the exact original Temporal exception with
its traceback.

### 12.5 Record model usage and terminal run metrics only for the commit owner

`agent.reason_step` starts only after workspace/task/run/correlation state is loaded, validated,
and bound. `model.request` is its child. The reason span ends only after the complete ordered,
lossless reasoning/manifest pair commit, or after a safe failure occurring once identity is bound.
Keep the Phase 9/10 sidecar repair, ordering, losslessness, and post-commit crash hooks unchanged.

Committed token/cost metrics follow this exhaustive branch table:

- an already-complete reasoning/manifest pair returns without a provider attempt or token/cost;
- a concurrent winner found after this invocation called the provider suppresses this invocation's
  token/cost because it did not commit the pair;
- a successful legacy-sidecar repair records once;
- a fresh complete-pair commit records once;
- commit failure or non-lossless manifest failure records none; and
- replay after either successful commit records none.

The exact invocation that commits a fresh pair or successful repair records immediately after that
commit and before returning, without moving/removing the existing post-commit crash barriers.
Document the deliberate at-most-once diagnostic boundary: a hard crash after commit may lose the
metric; retry never duplicates it. Task 7 adds no telemetry outbox and changes no transaction.

Finalization retains the persisted once-guard. Under the row lock:

1. `completed_at is not None` means another invocation owns finalization; roll back/return and
   emit no terminal metric.
2. `completed_at is None` means this invocation owns finalization even when current status is
   already `failed` with `error_code="tool_execution_unknown"`.
3. Set final status and exactly one persisted `completed_at`, commit, then only that owner records
   `agent_runs_total`, optional duration, and optional closed failure metric.

Duration uses only persisted `started_at` and committed `completed_at`. Record the histogram
only when both are timezone-aware valid datetimes and the difference is finite and nonnegative.
A missing/naive/malformed/future legacy start fabricates no zero, does not fail finalization, and
does not suppress the run counter.

The closed failure mapping is exact:

~~~text
tool_execution_unknown -> execution_unknown
max_steps_exceeded      -> budget
all other failed runs   -> internal
cancelled/completed     -> no failure counter
~~~

Never label with raw `error_code`. Add the already-failed/uncompleted regression, count it once,
then replay finalization and prove every terminal metric remains unchanged.

### 12.6 Require catalog authority and a matching durable terminal tool call

A prefix is not authority. Compute a known `tool_family` only after exact tool-name membership in
the activity's current `ToolCatalog`; every attacker-supplied, unregistered, or removed name is
`other`, even when its prefix is `github`, `linear`, or another registered family. Never
export the tool name.

Map durable gateway states exactly:

~~~text
GatewayOutcome executed          -> metric/span outcome completed
GatewayOutcome needs_approval    -> no counter; span outcome accepted
GatewayOutcome failed            -> metric/span outcome failed
GatewayOutcome denied            -> metric/span outcome denied
GatewayOutcome rejected          -> metric/span outcome rejected
GatewayOutcome execution_unknown -> metric/span outcome execution_unknown
GatewayOutcome executing         -> no counter
~~~

Failure classes are closed and independent of error text/code:

~~~text
failed            -> internal
denied/rejected   -> policy
execution_unknown -> execution_unknown
~~~

`GatewayOutcome.replayed` suppresses an ordinary terminal reload, but is not sufficient evidence
for every path. Before recording, require that the outcome matches the exact durable
`ToolCall` identity and terminal transition/reload owned by this activity. The
`_invocation_mismatch_outcome` path commits its existing audit but remains
`replayed=False`; it is not a new terminal tool call and emits no terminal counter.

Record only after the authoritative terminal commit/reload and exact tool-call identity check, but
before `_raise_ordinary_failure`, so genuine committed failures count while the existing
`GatewayOutcome`, public `BoundToolResult`, `ApplicationError`, gateway transactions,
approval flow, and replay flag remain unchanged. Telemetry failure changes none of them.

Test first and replay behavior for executed, failed, denied, rejected, execution-unknown,
needs-approval, executing, unknown-tool, removed-tool, and invocation-mismatch paths. Prove exact
catalog-derived family/risk/outcome/failure labels, no identifier/name leakage, no mismatch/replay
duplicate, and exact existing public return/error behavior under hostile telemetry.

### 12.7 Reconcile the trigger commit-to-dispatch crash gap before claiming durability

`TriggerMatcher` keeps transition-based at-most-once diagnostics, but product dispatch must
survive a crash after the authoritative `started` commit and before Temporal:

1. Commit the new authoritative `started` invocation.
2. A duplicate delivery still commits its one `duplicate` history row. If it finds the
   authoritative `started` row with no linked `task_id`, return that original authority to the
   dispatch path and reissue the same deterministic Temporal workflow ID derived from the original
   invocation ID.
3. An authoritative started row with a linked `task_id` permits immediate suppression.
4. Treat `WorkflowAlreadyStarted` for that deterministic ID as dispatch success. Concurrent
   reconcilers are safe because Temporal owns duplicate-start idempotency.
5. On a real dispatch failure, fail only the exact authoritative started row with the fixed
   persisted `upstream_unavailable` code, contain secondary update/diagnostic failures, and
   re-raise the exact original Temporal error. A later delivery may then create a fresh started
   authority.

Missing-target failure persists only `invalid_request`. Trigger failure metric classes are
exactly `target` and `dispatch`. Each invocation/duplicate/failure metric is attempted only
after its exact row commit; a crash may lose diagnostics but may not lose/suppress product work or
create a metric-driven redelivery.

`trigger_invocations_total` uses central
`normalize_connector_type(envelope.source.type)` and only `started`, `duplicate`, or
`failed`; `trigger_failures_total` uses the same connector plus only `target` or
`dispatch`. A normal start counts once after its started commit, a duplicate counts once after
its history-row commit, and missing-target/real-dispatch failures count once after their failed-row
commit. Two genuine failed deliveries may create and count two fresh authorities; reconciliation
of one unfinished authority may not fabricate a second started count.

Add an injectable post-started-commit/pre-dispatch barrier. Crash there, redeliver, and use a fake
Temporal client that enforces duplicate workflow IDs. Prove at most one workflow, one authoritative
started row, one duplicate history row, and no lost work. Preserve canonical-event redelivery and
existing `WorkflowAlreadyStarted` regressions. Also cover linked-task suppression, concurrent
reconcilers, real dispatch failure followed by a fresh authority, and hostile metrics/tracer/update
failures.

`trigger.dispatch` uses the explicitly injected Task 5 tracer and stays a child of the active
Task 5 NATS consumer span through Temporal settlement. Never attach trigger/target/workflow/event/
workspace/idempotency/external IDs, URL, exception text, or event data to spans or labels.

### 12.8 Normalize every late attribute through the closed registry

No Task 7 path calls `span.set_attribute` with a late raw value. Use one best-effort setter that
runs Task 2's per-key normalizer, or compute the complete normalized final attribute mapping before
span end. Clamp model latency to the exact inclusive `0..300_000` millisecond range. Never call
`str()` or `repr()` on an arbitrary provider, gateway, trigger, or exception value.

Provider type comes from the validated `ModelProviderType`; tool family requires current catalog
membership; connector type uses Task 1's central `normalize_connector_type`; all
operation/outcome/failure/risk/direction values use the closed registry. Tests include arbitrary
regex-safe strings and prove they normalize to `other` rather than surviving merely because they
match a character regex.

Agent/tool/trigger late outcomes obey the same rule. Tool spans start only after bound
manifest/runtime/tool-call identity is validated. No prompt, completion, model/chunk/tool name,
provider request ID, schema, tool input/output, approval payload, manifest, error detail, or
connector/event payload is attached.

### 12.9 Preserve predecessor contracts, dependencies, and cross-task handoffs

The Task 7 changes preserve all of these contracts:

- API verification's lookup/decryption/audit/commit/detail behavior, Task 4 middleware/lifespan,
  and Task 6 app-runtime/provider ownership;
- agent reasoning's ordered lossless two-record manifest, sidecar repair, Phase 9/10 crash
  barriers, and absence of connector/tool-worker authority;
- tool `GatewayOutcome`, `BoundToolResult`, transactions, approvals, and replay semantics;
- event-worker's Task 5 singleton runtime, NATS context/span lifetime, lag task, TaskGroup, engine
  tracer/cleanup, and Task 6 Temporal interceptor identity; and
- Task 11's connected `model.request`, `agent.reason_step`,
  `tool.gateway.execute`, and `trigger.dispatch` spans, complete canary scan, replay checks, and
  real corrected trigger reconciliation barrier.

Dependency ownership is exact:

- `packages/models/pyproject.toml` adds `jhin-observability` with its workspace source and
  direct `opentelemetry-api>=1.38,<2`;
- `packages/tools/pyproject.toml` adds `jhin-observability` with its workspace source;
- no service relies on a new transitive import; and
- regenerate `uv.lock` once, then require `uv lock --check`.

Amend the exhaustive global File Map for these currently absent affected paths:

~~~text
apps/api/tests/test_webhooks_unit.py
packages/models/tests/test_factory.py
services/agent_worker/tests/test_delegation_activities.py
services/agent_worker/tests/test_phase9_invocation_activity.py
services/agent_worker/tests/test_reasoning_manifest.py
services/agent_worker/tests/test_step_projection.py
services/agent_worker/tests/test_upgrade_crash_barriers.py
services/event_worker/tests/test_matcher.py
services/tool_worker/tests/test_advertised_tools.py
services/tool_worker/tests/test_bound_approval.py
services/tool_worker/tests/test_bound_tool_execution.py
~~~

The webhook and advertised-tools tests were introduced by earlier binding corrections and are
modified again here to pass explicit Task 7 test telemetry. Every other listed test gains only the
fixture/contract assertions required by the Task 7 production changes. No silent production no-op
fallback is authorized.

### 12.10 Run four independent executable RED groups

After corrected Tasks 1-6 exist and every Task 7 helper/fixture/auditor above is complete, run:

~~~bash
uv run pytest packages/models/tests/test_telemetry.py \
  packages/models/tests/test_factory.py \
  apps/api/tests/test_model_telemetry.py -q
uv run pytest services/agent_worker/tests/test_telemetry.py -q
uv run pytest packages/tools/tests/test_telemetry.py \
  services/tool_worker/tests/test_telemetry.py -q
uv run pytest services/event_worker/tests/test_telemetry.py \
  services/event_worker/tests/test_matcher.py \
  apps/api/tests/test_webhooks_unit.py -q
~~~

Expected RED names absent wrapper/runtime wiring, committed-transition recording, exact
normalization, trigger reconciliation, or hostile-telemetry containment. Implement in that same
order and make each focused group GREEN before proceeding. Undefined seams, abstract wrapper
construction, unrelated collection/type failures, or leaked provider/runtime state are invalid
RED.

### 12.11 Run focused, affected, root, and static GREEN gates

After the four groups are GREEN, run exactly:

~~~bash
uv lock
uv lock --check
uv run pytest \
  packages/models/tests packages/tools/tests apps/api/tests \
  services/agent_worker/tests services/tool_worker/tests \
  services/event_worker/tests -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
~~~

All commands pass. Root collection includes integration modules while live-marked cases remain
deselected. These broad gates are mandatory because Task 7 changes two shared distributions, API
dependencies, three workers, and durable trigger/tool transactions.

### 12.12 Make Task 7's 32 paths and committed tree exact

Replace Task 7 `Files` and staging with this exact mirrored array:

~~~bash
set -euo pipefail
task7_paths=(
  apps/api/src/jhin_api/deps.py
  apps/api/src/jhin_api/models/router.py
  apps/api/src/jhin_api/models/service.py
  apps/api/tests/test_model_telemetry.py
  apps/api/tests/test_webhooks_unit.py
  packages/models/pyproject.toml
  packages/models/src/jhin_models/factory.py
  packages/models/src/jhin_models/telemetry.py
  packages/models/tests/test_factory.py
  packages/models/tests/test_telemetry.py
  packages/tools/pyproject.toml
  packages/tools/src/jhin_tools/telemetry.py
  packages/tools/tests/test_telemetry.py
  services/agent_worker/src/jhin_agent_worker/activities.py
  services/agent_worker/src/jhin_agent_worker/projections.py
  services/agent_worker/src/jhin_agent_worker/reasoning.py
  services/agent_worker/tests/test_delegation_activities.py
  services/agent_worker/tests/test_phase9_invocation_activity.py
  services/agent_worker/tests/test_reasoning_manifest.py
  services/agent_worker/tests/test_step_projection.py
  services/agent_worker/tests/test_telemetry.py
  services/agent_worker/tests/test_upgrade_crash_barriers.py
  services/event_worker/src/jhin_event_worker/main.py
  services/event_worker/src/jhin_event_worker/matcher.py
  services/event_worker/tests/test_matcher.py
  services/event_worker/tests/test_telemetry.py
  services/tool_worker/src/jhin_tool_worker/activities.py
  services/tool_worker/tests/test_advertised_tools.py
  services/tool_worker/tests/test_bound_approval.py
  services/tool_worker/tests/test_bound_tool_execution.py
  services/tool_worker/tests/test_telemetry.py
  uv.lock
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task7_paths[@]}"
git diff --check -- "${task7_paths[@]}"
git add -- "${task7_paths[@]}"
expected_index="$(printf '%s\n' "${task7_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task7_paths[@]}"
git commit --only "${task7_paths[@]}" \
  -m "feat(observability): record committed agent and tool metrics"
test "$(git show -s --format=%s HEAD)" = \
  "feat(observability): record committed agent and tool metrics"
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)"
test "$actual_commit_paths" = "$expected_index"
test -z "$(git diff --cached --name-only)"
~~~

`activities.py` remains because composite `AgentActivities` binds Task 6 runtime handles. The
Task 7 `Files` block and array are exact mirrors. No other Task 7 path is authorized. The global
index-only exception remains the sole exception: pre-existing unstaged work outside these paths
may remain, but a pre-staged/unexpected/missing path, path outside `Files`, commit-tree mismatch,
or non-empty post-commit index fails closed. Any newly discovered affected path requires a
reviewed File Map/Files/manifest amendment before it is touched.

### Task 8: Instrument Connector HTTP, Connection Health, and Sandbox Lifecycles

**Files:**
- Modify: `apps/api/src/jhin_api/connections/router.py`
- Modify: `apps/api/src/jhin_api/connections/service.py`
- Modify: `apps/api/tests/test_connections_unit.py`
- Modify: `apps/api/tests/test_connector_telemetry.py`
- Modify: `packages/connectors/pyproject.toml`
- Modify: `packages/connectors/src/jhin_connectors/base.py`
- Modify: `packages/connectors/src/jhin_connectors/cli/runner_client.py`
- Modify: `packages/connectors/src/jhin_connectors/cli/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/github/auth.py`
- Modify: `packages/connectors/src/jhin_connectors/github/client.py`
- Modify: `packages/connectors/src/jhin_connectors/github/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/github/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/http_client.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/client.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/database_client.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/database_tools.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/management_client.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/management_tools.py`
- Modify: `packages/connectors/src/jhin_connectors/telemetry.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/client.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/tools.py`
- Modify: `packages/connectors/tests/supabase/test_database_telemetry.py`
- Modify: `packages/connectors/tests/test_http_client.py`
- Modify: `packages/connectors/tests/test_telemetry.py`
- Modify: `packages/tools/pyproject.toml`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Modify: `packages/tools/tests/test_telemetry.py`
- Modify: `services/sandbox_runner/pyproject.toml`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/schemas.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- Modify: `services/sandbox_runner/tests/test_job_config.py`
- Modify: `services/sandbox_runner/tests/test_telemetry.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/activities.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/cleanup_activities.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/trigger_activities.py`
- Modify: `services/tool_worker/tests/test_telemetry.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes the accepted Task 7 handoff and produces the exact Task 8 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing connector URL/body/error exclusion tests**

```python
import ast
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode, Tracer

from jhin_connectors.http_client import ProviderHTTPError, send_bounded_json
from jhin_observability import (
    ObservabilityNotInitializedError,
    get_runtime,
    noop_tracer,
)

ConnectorTraceHarness = tuple[Tracer, InMemorySpanExporter]
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def connector_trace_harness() -> Iterator[ConnectorTraceHarness]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield provider.get_tracer("jhin-connectors-test"), exporter
    finally:
        provider.shutdown()


def provider_failure(canary: str) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            content=canary.encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

    return respond


def one_finished_span(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    matches = [span for span in exporter.get_finished_spans() if span.name == name]
    assert len(matches) == 1
    return matches[0]


def exported_span_payload(exporter: InMemorySpanExporter) -> str:
    return json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [event.name for event in span.events],
                "status": span.status.status_code.name,
                "status_description": span.status.description,
            }
            for span in exporter.get_finished_spans()
        ],
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_credential_bearing_url_is_rejected_before_request_or_span(
    connector_trace_harness: ConnectorTraceHarness,
) -> None:
    tracer, exporter = connector_trace_harness
    requested = False

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, json={"unexpected": True}, request=request)

    request = httpx.Request(
        "POST",
        "https://user-canary:pass-canary@example.test/items?token=query-canary",
        headers={"Authorization": "Bearer auth-canary"},
        json={"secret": "body-canary"},
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request))
    try:
        with pytest.raises(
            ProviderHTTPError, match="Provider request URL must not contain credentials"
        ):
            await send_bounded_json(
                client,
                request,
                connector_type="github",
                operation="issue_comment_create",
                retry_count=0,
                tracer=tracer,
            )
    finally:
        await client.aclose()
    assert requested is False
    assert exporter.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_in_span_provider_failure_exports_only_safe_connector_metadata(
    connector_trace_harness: ConnectorTraceHarness,
) -> None:
    tracer, exporter = connector_trace_harness
    request = httpx.Request(
        "POST",
        "https://example.test/items?token=query-canary",
        headers={"Authorization": "Bearer auth-canary"},
        json={"secret": "body-canary"},
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(provider_failure("error-body-canary")))
    try:
        with pytest.raises(ProviderHTTPError):
            await send_bounded_json(
                client,
                request,
                connector_type="github",
                operation="issue_comment_create",
                retry_count=0,
                tracer=tracer,
            )
    finally:
        await client.aclose()

    rendered = exported_span_payload(exporter)
    for canary in ("query-canary", "auth-canary", "body-canary", "error-body-canary"):
        assert canary not in rendered
    span = one_finished_span(exporter, "connector.http")
    attributes = dict(span.attributes or {})
    latency_ms = attributes.pop("jhin.latency_ms")
    assert isinstance(latency_ms, int) and 0 <= latency_ms <= 300_000
    assert attributes == {
        "jhin.connector_type": "github",
        "jhin.operation": "issue_comment_create",
        "jhin.outcome": "failed",
        "jhin.retry_count": 0,
        "error.type": "ProviderHTTPError",
        "error.code": "upstream_unavailable",
    }
    assert span.events == ()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None


@pytest.mark.asyncio
async def test_connector_package_uses_explicit_noop_before_bootstrap() -> None:
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    )
    try:
        assert await send_bounded_json(
            client,
            httpx.Request("GET", "https://example.test/ping"),
            connector_type="github",
            operation="verify",
            tracer=noop_tracer(),
        ) == {"ok": True}
    finally:
        await client.aclose()
```

Require `operation` to match a closed registry per connector; unknown names become `other`. Do not infer operation from the URL.

Add a Supabase asyncpg-specific RED test. Patch `asyncpg.connect` with the existing fake connection, invoke `verify_database_connection`, and assert the connector-client span contains no DSN, SQL, binding, result, or asyncpg exception text:

```python
import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Tracer

from jhin_connectors.supabase import database_client
from jhin_connectors.supabase.database_client import verify_database_connection

DatabaseTraceHarness = tuple[Tracer, InMemorySpanExporter]


@pytest.fixture
def database_trace_harness() -> Iterator[DatabaseTraceHarness]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield provider.get_tracer("jhin-supabase-test"), exporter
    finally:
        provider.shutdown()


class TelemetryDatabaseConnection:
    def __init__(self) -> None:
        self.closed = False

    async def execute(self, query: str, *args: object) -> str:
        assert query == "SET search_path TO pg_catalog"
        assert args == ()
        return "SET"

    async def fetchrow(self, query: str, *args: object) -> dict[str, str]:
        assert query == "SELECT secret_canary WHERE value=$1"
        assert args == ("bind-canary",)
        return {"value": "result-canary"}

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_supabase_asyncpg_span_excludes_dsn_sql_bindings_and_results(
    monkeypatch: pytest.MonkeyPatch,
    database_trace_harness: DatabaseTraceHarness,
) -> None:
    tracer, exporter = database_trace_harness
    connection = TelemetryDatabaseConnection()

    async def fake_connect(**kwargs: Any) -> TelemetryDatabaseConnection:
        assert kwargs["dsn"] == ("postgresql://dsn-user-canary:dsn-pass-canary@127.0.0.1:65433/db")
        return connection

    async def fake_verify_live_role(
        selected: TelemetryDatabaseConnection, allowed_schemas: tuple[str, ...]
    ) -> None:
        assert selected is connection and allowed_schemas == ("public",)
        assert await selected.fetchrow("SELECT secret_canary WHERE value=$1", "bind-canary") == {
            "value": "result-canary"
        }

    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(database_client, "verify_live_role", fake_verify_live_role)
    await verify_database_connection(
        "postgresql://dsn-user-canary:dsn-pass-canary@127.0.0.1:65433/db",
        project_ref="abcdefghijklmnopqrst",
        allowed_schemas=("public",),
        app_database_url="postgresql://app:app-canary@127.0.0.1:5432/jhin",
        tracer=tracer,
    )
    assert connection.closed is True
    matches = [span for span in exporter.get_finished_spans() if span.name == "connector.database"]
    assert len(matches) == 1
    span = matches[0]
    assert dict(span.attributes) == {
        "jhin.connector_type": "supabase",
        "jhin.operation": "verify",
        "jhin.outcome": "ok",
    }
    rendered = json.dumps({"name": span.name, "attributes": dict(span.attributes)})
    for canary in (
        "dsn-user-canary",
        "dsn-pass-canary",
        "SELECT secret_canary",
        "bind-canary",
        "result-canary",
    ):
        assert canary not in rendered
```

Run RED:

```bash
uv run pytest packages/connectors/tests/test_telemetry.py \
  packages/connectors/tests/test_http_client.py \
  packages/connectors/tests/supabase/test_database_telemetry.py -q
```

Expected: both new telemetry test files are collected and FAIL before implementation because the
bounded connector client has no normalized HTTP contract or tracer argument and the Supabase
asyncpg boundary has no connector-database span. The pre-validation URL case must already prove no
transport call and no span; do not move credential validation inside `safe_span` merely to satisfy
the in-span failure assertion.

- [ ] **Step 2: Implement connector spans at the bounded client**

Add `jhin-observability` to connectors and extend the signature:

```python
async def send_bounded_json(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    connector_type: str,
    operation: str,
    retry_count: int = 0,
    tracer: Tracer | None = None,
    max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    expected_status_codes: tuple[int, ...] | None = None,
) -> Any:
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
        raise ValueError("max_response_bytes must be an integer")
    if max_response_bytes < 0:
        raise ValueError("max_response_bytes must not be negative")
    if expected_status_codes is not None and (
        not expected_status_codes
        or any(
            isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300
            for status in expected_status_codes
        )
    ):
        raise ValueError("expected_status_codes must contain 2xx integer status codes")
    if request.url.username or request.url.password:
        raise ProviderHTTPError("Provider request URL must not contain credentials")

    connector = normalize_connector_type(connector_type)
    safe_operation = normalize_connector_operation(connector, operation)
    started = time.monotonic()
    outcome = "failed"
    with safe_span(
        "connector.http",
        tracer=tracer if tracer is not None else noop_tracer(),
        kind=SpanKind.CLIENT,
        attributes={
            "jhin.connector_type": connector,
            "jhin.operation": safe_operation,
            "jhin.retry_count": max(0, min(retry_count, 10)),
        },
    ) as span:
        try:
            try:
                response = await client.send(request, stream=True, follow_redirects=False)
            except Exception as exc:
                record_span_error(
                    span,
                    safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE),
                )
                raise ProviderHTTPError("Provider request failed") from None
            try:
                document = await _parse_bounded_response(
                    response,
                    max_response_bytes=max_response_bytes,
                    expected_status_codes=expected_status_codes,
                )
            except ProviderHTTPError as exc:
                record_span_error(
                    span,
                    safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE),
                )
                raise
            except Exception as exc:
                record_span_error(span, safe_error(exc, code=SafeErrorCode.INTERNAL_ERROR))
                raise ProviderHTTPError(
                    "Provider response handling failed", status_code=response.status_code
                ) from None
            finally:
                active_exception = sys.exc_info()[0] is not None
                try:
                    await response.aclose()
                except Exception as exc:
                    if not active_exception:
                        record_span_error(
                            span,
                            safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE),
                        )
                        raise ProviderHTTPError(
                            "Provider response could not be closed",
                            status_code=response.status_code,
                        ) from None
            outcome = "ok"
            return document
        finally:
            span.set_attribute("jhin.outcome", outcome)
            span.set_attribute(
                "jhin.latency_ms", min(300_000, int((time.monotonic() - started) * 1_000))
            )
```

Keep the existing `_parse_bounded_response` byte cap unchanged. Never pass `request.url`, method
path, headers, request content, response content, transport exception, connection ID, or external
resource IDs. Update every call site with an explicit connector type and operation constant.

In `packages/connectors/tests/test_http_client.py`, preserve all eight existing bounded-response
cases byte-for-byte except for adding these three keyword arguments to every
`send_bounded_json(...)` call:

```python
connector_type = ("github",)
operation = ("verify",)
tracer = (noop_tracer(),)
```

Append this regression to that same file so a later required-argument change cannot silently drop a
bounded-response case:

```python
def test_all_eight_bounded_response_cases_supply_explicit_telemetry_contract() -> None:
    path = Path(__file__)
    tree = ast.parse(path.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "send_bounded_json"
    ]
    assert len(calls) == 8
    for call in calls:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert ast.literal_eval(keywords["connector_type"]) == "github"
        assert ast.literal_eval(keywords["operation"]) == "verify"
        assert ast.unparse(keywords["tracer"]) == "noop_tracer()"
```

The test file adds exact imports `import ast`, `from pathlib import Path`, and
`from jhin_observability import noop_tracer`. Production connector factories receive
`runtime.tracer` from tool-worker/API app state and pass it to every HTTP/database wrapper; only
package tests and standalone host callers select `noop_tracer()`.

Make that injection concrete in `jhin_connectors.telemetry` and in every connector constructor:

```python
@dataclass(frozen=True)
class ConnectorTelemetry:
    tracer: Tracer

    @classmethod
    def standalone(cls) -> "ConnectorTelemetry":
        return cls(noop_tracer())


def connector_telemetry(tracer: Tracer | None = None) -> ConnectorTelemetry:
    return ConnectorTelemetry(tracer) if tracer is not None else ConnectorTelemetry.standalone()
```

`GitHubClient`, `LinearClient`, `VercelClient`, both Supabase clients, and `RunnerClient` each accept
`telemetry: ConnectorTelemetry | None = None`, store `telemetry or ConnectorTelemetry.standalone()`,
and pass `tracer=self._telemetry.tracer` to every `send_bounded_json` or
`trace_connector_database` call; `RunnerClient` passes the same tracer to every
`_transport_request`. Extend `build_default_catalog(..., tracer: Tracer | None = None)`
to construct one `connector_telemetry(tracer)` and pass it to all clients. Tool-worker calls
`build_default_catalog(..., tracer=runtime.tracer)`; package/host tests omit it deliberately.

Add this binding-aware production audit to `packages/connectors/tests/test_telemetry.py`:

```python
def test_every_connector_boundary_call_uses_the_owned_tracer() -> None:
    roots = (REPO_ROOT / "packages/connectors/src/jhin_connectors",)
    failures: list[str] = []
    for path in (candidate for root in roots for candidate in root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = ast.unparse(call.func).rsplit(".", 1)[-1]
            if name not in {"send_bounded_json", "trace_connector_database", "_transport_request"}:
                continue
            keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}
            if keywords.get("tracer") not in {"self._telemetry.tracer", "tracer"}:
                failures.append(f"{path}:{call.lineno}")
    assert failures == []


def test_tool_worker_catalog_injects_process_tracer() -> None:
    tree = ast.parse(
        (REPO_ROOT / "services/tool_worker/src/jhin_tool_worker/resources.py").read_text()
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("build_default_catalog")
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords["tracer"] == "runtime.tracer"
```

Wrap only the `asyncpg.connect` plus transaction/verification lifetime in `database_client.py` and `database_tools.py` through this concrete helper from `telemetry.py`; no caller hand-writes a second span shape:

```python
DatabaseOperation = Literal["verify", "execute_read", "execute_write"]
T = TypeVar("T")


async def trace_connector_database(
    operation: DatabaseOperation,
    action: Callable[[], Awaitable[T]],
    *,
    tracer: Tracer | None = None,
) -> T:
    outcome = "failed"
    with safe_span(
        "connector.database",
        tracer=tracer if tracer is not None else noop_tracer(),
        kind=SpanKind.CLIENT,
        attributes={
            "jhin.connector_type": "supabase",
            "jhin.operation": operation,
        },
    ) as span:
        try:
            result = await action()
            outcome = "ok"
            return result
        except Exception as exc:
            record_span_error(span, safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE))
            raise
        finally:
            span.set_attribute("jhin.outcome", outcome)
```

Pass the closed operation from the caller (`verify`, `execute_read`, or `execute_write`). Never attach or log `validated_url`, project ref, relation/schema name, statement, params, result, server exception, host, database, or user. This wrapper must not alter endpoint validation, least-privilege checks, transaction ordering, timeout, commit/rollback, or close behavior.

- [ ] **Step 3: Write failing connection-health gauge aggregation tests**

Use an exact enabled-row sample with healthy, stale/error, and zero-enabled connector types; the
loader implementation below excludes disabled rows in SQL. Assert returned observations directly:

```python
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from jhin_connectors.telemetry import (
    ConnectorHealthRow,
    connector_health_observations,
)
from jhin_domain import ConnectionStatus
from jhin_observability import Observation


def observation_values(
    values: Sequence[Observation], *label_keys: str
) -> dict[tuple[str, ...], int]:
    return {tuple(item.attributes[key] for key in label_keys): int(item.value) for item in values}


def test_connector_health_aggregates_only_enabled_rows() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    rows = [
        ConnectorHealthRow("github", ConnectionStatus.ACTIVE.value, now, False),
        ConnectorHealthRow("github", ConnectionStatus.ACTIVE.value, now, False),
        ConnectorHealthRow(
            "linear", ConnectionStatus.ACTIVE.value, now - timedelta(seconds=301), True
        ),
    ]
    health, connections = connector_health_observations(
        rows,
        now=now,
        freshness_seconds=300,
    )
    assert observation_values(health, "connector_type") == {
        ("github",): 1,
        ("linear",): 0,
    }
    assert observation_values(connections, "connector_type", "outcome") == {
        ("github", "healthy"): 2,
        ("linear", "unhealthy"): 1,
    }
    assert ("vercel",) not in observation_values(health, "connector_type")
```

```bash
uv run pytest packages/connectors/tests/test_telemetry.py -q
```

Expected: FAIL because no connector health sampler publishes aggregate gauges.

- [ ] **Step 4: Implement connection-health gauge aggregation**

Query only the four approved columns and publish a complete replacement sample after a successful
query. A failed query leaves the prior tuple installed:

```python
@dataclass(frozen=True)
class ConnectorHealthRow:
    connector_type: str
    status: str
    last_verified_at: datetime | None
    has_error: bool


async def load_connector_health_rows(session_factory: SessionFactory) -> list[ConnectorHealthRow]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Connection.connector_type,
                    Connection.status,
                    Connection.last_verified_at,
                    Connection.last_error.is_not(None),
                ).where(Connection.status != ConnectionStatus.DISABLED.value)
            )
        ).all()
    return [
        ConnectorHealthRow(type_, status, verified, has_error)
        for type_, status, verified, has_error in rows
    ]


def connector_health_observations(
    rows: Sequence[ConnectorHealthRow], *, now: datetime, freshness_seconds: int
) -> tuple[tuple[Observation, ...], tuple[Observation, ...]]:
    cutoff = now - timedelta(seconds=freshness_seconds)
    grouped: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        connector = normalize_connector_type(row.connector_type)
        healthy = (
            row.status == ConnectionStatus.ACTIVE.value
            and not row.has_error
            and row.last_verified_at is not None
            and row.last_verified_at >= cutoff
        )
        grouped[connector].append(healthy)
    health = tuple(
        Observation(1 if all(values) else 0, {"connector_type": connector})
        for connector, values in sorted(grouped.items())
    )
    counts = tuple(
        Observation(
            sum(1 for value in values if value is expected),
            {"connector_type": connector, "outcome": outcome},
        )
        for connector, values in sorted(grouped.items())
        for outcome, expected in (("healthy", True), ("unhealthy", False))
        if any(value is expected for value in values)
    )
    return health, counts


async def poll_connector_health(
    session_factory: SessionFactory,
    metrics: JhinMetrics,
    stop: asyncio.Event,
    *,
    interval_seconds: float = 30.0,
    freshness_seconds: int = 300,
) -> None:
    while not stop.is_set():
        try:
            health, counts = connector_health_observations(
                await load_connector_health_rows(session_factory),
                now=datetime.now(UTC),
                freshness_seconds=freshness_seconds,
            )
            metrics.set_observable("connector_health", health)
            metrics.set_observable("connector_connections", counts)
        except Exception as exc:
            logger.warning("telemetry.connector_health_probe_failed", error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
```

The tool-worker creates the sampler after its database resource and cancels/awaits it before that
resource closes. It never decrypts credentials, calls a connector, or grants authority.

Start/cancel the sampler in tool-worker using its existing database resource. It does not decrypt credentials, call connectors, or grant authority.

- [ ] **Step 5: Write failing sandbox propagation/terminal metric tests**

```python
def test_settings() -> Settings:
    return Settings(
        sandbox_runner_token="test-token",
        sandbox_default_image="jhin-sandbox:test",
    )


def job_request(
    *,
    job_id: str = "018f0000-0000-7000-8000-000000000010",
    network_policy: Literal["none", "internet"] = "none",
    command: list[str] | None = None,
) -> SandboxJobRequest:
    return SandboxJobRequest(
        job_id=job_id,
        command=command or ["/bin/true"],
        network_policy=network_policy,
    )


@pytest.mark.asyncio
async def test_runner_receives_trace_headers_but_job_env_does_not(
    spans: InMemorySpanExporter,
    metrics: JhinMetrics,
    tracer: Tracer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_RUNNER_TOKEN", "test-token")
    transport = RecordingRunnerTransport(terminal_status="completed")
    payload = {
        "job_id": "018f0000-0000-7000-8000-000000000010",
        "env": {"SAFE": "value"},
        "secret_env": {"TOKEN": "sandbox-secret-canary"},
        "network_policy": "none",
    }
    with recording_span("tool-parent"):
        result = await run_sandbox_job(
            payload,
            job_timeout_seconds=2,
            transport=transport,
            tracer=tracer,
        )
    assert TRACEPARENT_RE.fullmatch(transport.requests[0].headers["traceparent"])
    assert "TRACEPARENT" not in payload["env"] and "TRACEPARENT" not in payload["secret_env"]
    assert result["status"] == "completed" and result["duration_ms"] == 7
    assert [request.path for request in transport.requests] == [
        "/v1/jobs",
        "/v1/jobs/018f0000-0000-7000-8000-000000000010",
        "/v1/jobs/018f0000-0000-7000-8000-000000000010",
    ]
    assert all(0 < request.timeout_seconds <= 30 for request in transport.requests)
    assert "sandbox-secret-canary" not in export_payload(spans, metrics)


@pytest.mark.asyncio
async def test_runner_terminal_job_metrics_emit_once(
    metrics: JhinMetrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = JobManager(test_settings(), metrics=metrics)

    async def complete_without_docker(record: JobRecord) -> None:
        record.started_at = datetime(2026, 8, 18, tzinfo=UTC)
        await manager._finish_terminal(
            record,
            "completed",
            finished_at=datetime(2026, 8, 18, 0, 0, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(manager, "_run", complete_without_docker)
    request = job_request(network_policy="internet", command=["/bin/true"])
    first = await manager.submit(request)
    terminal = await manager.wait_terminal(first.request.job_id, timeout_seconds=2.0)
    assert terminal.status == "completed"
    assert terminal.duration_ms is not None and terminal.duration_ms >= 0
    await manager._finish_terminal(terminal, "completed", finished_at=terminal.finished_at)
    stored = manager.get(request.job_id)
    assert stored is terminal and stored.to_response().status == "completed"
    first_cancel = await manager.cancel(request.job_id)
    second_cancel = await manager.cancel(request.job_id)
    assert first_cancel is terminal and first_cancel.status == "completed"
    assert second_cancel is terminal and second_cancel.status == "completed"
    assert metric_sum("sandbox_jobs_total", outcome="completed", network_policy="internet") == 1
    assert histogram_count("sandbox_job_duration_seconds", outcome="completed") == 1


@pytest.mark.asyncio
async def test_every_duplicate_job_id_keeps_existing_validation_behavior(
    metrics: JhinMetrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = JobManager(test_settings(), metrics=metrics)

    async def no_container_work(_record: JobRecord) -> None:
        return None

    monkeypatch.setattr(manager, "_run", no_container_work)
    first = job_request(job_id="018f0000-0000-7000-8000-000000000011", command=["/bin/true"])
    await manager.submit(first)
    for duplicate in (
        first.model_copy(deep=True),
        first.model_copy(update={"command": ["/bin/false"]}),
    ):
        with pytest.raises(
            JobValidationError,
            match=r"job 018f0000-0000-7000-8000-000000000011 already exists",
        ):
            await manager.submit(duplicate)


def test_duplicate_submit_http_contract_remains_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(test_settings())
    manager = app.state.manager
    monkeypatch.setattr(manager, "start", AsyncMock())
    monkeypatch.setattr(manager, "close", AsyncMock())
    monkeypatch.setattr(manager, "_run", AsyncMock())
    request = job_request(job_id="018f0000-0000-7000-8000-000000000012")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-token"}
        assert (
            client.post(
                "/v1/jobs", headers=headers, json=request.model_dump(mode="json")
            ).status_code
            == 202
        )
        response = client.post("/v1/jobs", headers=headers, json=request.model_dump(mode="json"))
    assert response.status_code == 422
    assert response.json()["detail"] == f"job {request.job_id} already exists"
```

Run RED:

```bash
uv run pytest services/sandbox_runner/tests/test_telemetry.py \
  packages/connectors/tests/test_telemetry.py -q
```

Expected: FAIL because runner context propagation and terminal lifecycle metrics are absent.

- [ ] **Step 6: Instrument tool-worker→runner HTTP and runner server/job spans**

Define the runner transport seam in `runner_client.py`; tests use a recording implementation and production uses the bounded httpx implementation:

```python
@dataclass(frozen=True)
class RunnerResponse:
    status_code: int
    document: Mapping[str, object]


class SandboxTransport(Protocol):
    async def request(
        self,
        method: Literal["GET", "POST", "DELETE"],
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float,
    ) -> RunnerResponse:
        """Send one bounded runner request and return its closed response document."""

    async def aclose(self) -> None:
        """Release transport-owned resources; implementations make this idempotent."""


class HttpxSandboxTransport:
    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds)

    async def request(
        self,
        method: Literal["GET", "POST", "DELETE"],
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float,
    ) -> RunnerResponse:
        response = await self._client.request(
            method,
            path,
            headers=headers,
            json=json_body,
            timeout=httpx.Timeout(timeout_seconds),
        )
        document = response.json() if response.content else {}
        if not isinstance(document, Mapping):
            raise SandboxRunnerError("sandbox runner returned an unexpected status shape")
        return RunnerResponse(response.status_code, dict(document))

    async def aclose(self) -> None:
        await self._client.aclose()


SandboxClientOperation = Literal["submit", "status", "cancel", "cleanup"]


def normalize_network_policy(value: object) -> str:
    return value if value in {"none", "internet"} else "other"


async def _transport_request(
    transport: SandboxTransport,
    method: Literal["GET", "POST", "DELETE"],
    path: str,
    *,
    headers: Mapping[str, str],
    operation: SandboxClientOperation,
    network_policy: str,
    timeout_seconds: float,
    tracer: Tracer,
    json_body: Mapping[str, object] | None = None,
) -> RunnerResponse:
    started = time.monotonic()
    outcome = "failed"
    with safe_span(
        "sandbox.client",
        tracer=tracer,
        kind=SpanKind.CLIENT,
        attributes={
            "jhin.operation": operation,
            "jhin.network_policy": normalize_network_policy(network_policy),
        },
    ) as span:
        try:
            response = await transport.request(
                method,
                path,
                headers=headers,
                json_body=json_body,
                timeout_seconds=timeout_seconds,
            )
            outcome = "ok" if 200 <= response.status_code < 300 else "failed"
            return response
        except Exception as exc:
            record_span_error(span, safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE))
            raise
        finally:
            span.set_attribute("jhin.outcome", outcome)
            span.set_attribute(
                "jhin.latency_ms",
                min(300_000, int((time.monotonic() - started) * 1_000)),
            )


async def run_sandbox_job(
    request_payload: dict[str, object],
    *,
    job_timeout_seconds: int,
    transport: SandboxTransport | None = None,
    tracer: Tracer | None = None,
) -> dict[str, object]:
    owned = transport is None
    _base_url, token = runner_config()
    if not token:
        raise SandboxRunnerError("SANDBOX_RUNNER_TOKEN is not configured in this worker")
    if transport is None:
        transport = HttpxSandboxTransport(_base_url)
    try:
        return await _submit_and_await_terminal(
            transport,
            request_payload,
            token=token,
            job_timeout_seconds=job_timeout_seconds,
            tracer=tracer if tracer is not None else noop_tracer(),
        )
    finally:
        if owned:
            await transport.aclose()


async def _submit_and_await_terminal(
    transport: SandboxTransport,
    request_payload: Mapping[str, object],
    *,
    token: str,
    job_timeout_seconds: int,
    tracer: Tracer,
) -> dict[str, object]:
    job_id = str(request_payload.get("job_id", ""))
    network_policy = normalize_network_policy(request_payload.get("network_policy"))
    headers = _headers(token)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + job_timeout_seconds + _DEADLINE_GRACE_SECONDS

    def remaining() -> float:
        return max(0.0, deadline - loop.time())

    submitted = await _transport_request(
        transport,
        "POST",
        "/v1/jobs",
        headers=headers,
        operation="submit",
        network_policy=network_policy,
        json_body=request_payload,
        timeout_seconds=min(30.0, max(0.001, remaining())),
        tracer=tracer,
    )
    if submitted.status_code not in {200, 202}:
        raise SandboxRunnerError(f"sandbox runner rejected the job ({submitted.status_code})")
    while True:
        if remaining() <= 0:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(1.0):
                    await _transport_request(
                        transport,
                        "POST",
                        f"/v1/jobs/{job_id}/cancel",
                        headers=headers,
                        operation="cancel",
                        network_policy=network_policy,
                        timeout_seconds=1.0,
                        tracer=tracer,
                    )
            raise SandboxRunnerError("sandbox job did not reach a terminal state before deadline")
        status = await _transport_request(
            transport,
            "GET",
            f"/v1/jobs/{job_id}",
            headers=headers,
            operation="status",
            network_policy=network_policy,
            timeout_seconds=min(30.0, max(0.001, remaining())),
            tracer=tracer,
        )
        if status.status_code != 200:
            raise SandboxRunnerError(f"sandbox job status lookup failed ({status.status_code})")
        outcome = status.document.get("status")
        if outcome in _TERMINAL_STATUSES:
            return dict(status.document)
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining()))


async def delete_workspace(
    workspace_key: str,
    *,
    transport: SandboxTransport | None = None,
    tracer: Tracer | None = None,
) -> bool:
    owned = transport is None
    base_url, token = runner_config()
    if not token:
        return False
    if transport is None:
        transport = HttpxSandboxTransport(base_url)
    try:
        response = await _transport_request(
            transport,
            "DELETE",
            f"/v1/workspaces/{workspace_key}",
            headers=_headers(token),
            operation="cleanup",
            network_policy="none",
            timeout_seconds=30.0,
            tracer=tracer if tracer is not None else noop_tracer(),
        )
        return response.status_code in {204, 404}
    except (httpx.HTTPError, SandboxRunnerError):
        return False
    finally:
        if owned:
            await transport.aclose()
```

`RecordingRunnerTransport` is a complete socket-free implementation in the test file:

```python
@dataclass(frozen=True)
class RecordedRunnerRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    json_body: Mapping[str, object] | None
    timeout_seconds: float


class RecordingRunnerTransport:
    def __init__(self, *, terminal_status: str) -> None:
        self.terminal_status = terminal_status
        self.requests: list[RecordedRunnerRequest] = []
        self.status_reads = 0
        self.closed = False

    async def request(
        self,
        method: Literal["GET", "POST", "DELETE"],
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        timeout_seconds: float,
    ) -> RunnerResponse:
        self.requests.append(
            RecordedRunnerRequest(
                method,
                path,
                MappingProxyType(dict(headers)),
                MappingProxyType(copy.deepcopy(dict(json_body))) if json_body is not None else None,
                timeout_seconds,
            )
        )
        if method == "POST" and path == "/v1/jobs":
            return RunnerResponse(202, {"status": "running"})
        if method == "GET" and path.startswith("/v1/jobs/"):
            self.status_reads += 1
            status = "running" if self.status_reads == 1 else self.terminal_status
            return RunnerResponse(
                200,
                {
                    "job_id": path.rsplit("/", 1)[1],
                    "status": status,
                    "duration_ms": 7 if status in _TERMINAL_STATUSES else None,
                },
            )
        if method == "POST" and path.endswith("/cancel"):
            return RunnerResponse(200, {"status": "cancelled"})
        raise AssertionError(f"unexpected runner request: {method} {path}")

    async def aclose(self) -> None:
        self.closed = True
```

`_headers(token)` becomes:

```python
def _headers(token: str) -> dict[str, str]:
    return inject_trace_headers({"Authorization": f"Bearer {token}"})
```

Start `sandbox.client` spans for submit, status, cancel, and cleanup with normalized operation/outcome/network policy; do not attach base URL, token, request JSON, command, env, secret env, output, workspace key, or Docker errors. Sandbox-runner middleware extracts trace headers and creates `sandbox.server` spans. `JobManager._run` creates one `sandbox.job.lifecycle` span whose only identifier is validated `job_id`; other attributes are outcome and normalized network policy. Job ID is permitted on traces/logs but not metrics.

Add `metrics: JhinMetrics`, a registry lock, a side-effect-free exact terminal waiter, and one idempotent terminal-finalization method. Do not change duplicate submission semantics or introduce a new API error class:

In the Task 6 sandbox app factory, replace `JobManager(active_settings)` with
`JobManager(active_settings, metrics=active_runtime.metrics)` in this task, after the constructor
below exists.

```python
@dataclass
class JobRecord:
    request: SandboxJobRequest
    image: str
    cpu_limit: float
    memory_mb: int
    pids_limit: int
    timeout_seconds: int
    status: str = "running"
    container_id: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None
    cancel_requested: bool = False
    task: asyncio.Task[None] | None = None
    redactor: SecretRedactor = field(default_factory=SecretRedactor)
    telemetry_recorded: bool = False

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1_000))

    def to_response(self) -> SandboxJobStatusResponse:
        return SandboxJobStatusResponse(
            job_id=self.request.job_id,
            status=self.status,
            image=self.image,
            network_policy=self.request.network_policy,
            exit_code=self.exit_code,
            started_at=self.started_at.isoformat() if self.started_at else None,
            finished_at=self.finished_at.isoformat() if self.finished_at else None,
            duration_ms=self.duration_ms,
            stdout=self.stdout,
            stderr=self.stderr,
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            error=self.error,
        )


class JobManager:
    def __init__(self, settings: Settings, *, metrics: JhinMetrics) -> None:
        self._settings = settings
        self._metrics = metrics
        self._jobs: dict[str, JobRecord] = {}
        self._jobs_lock = asyncio.Lock()

    async def submit(self, request: SandboxJobRequest) -> JobRecord:
        async with self._jobs_lock:
            if request.job_id in self._jobs:
                raise JobValidationError(f"job {request.job_id} already exists")
            cpu, memory, pids, timeout = resolve_limits(request, self._settings)
            record = JobRecord(
                request=request,
                image=request.image or self._settings.sandbox_default_image,
                cpu_limit=cpu,
                memory_mb=memory,
                pids_limit=pids,
                timeout_seconds=timeout,
            )
            for value in request.secret_env.values():
                record.redactor.register(value)
            self._jobs[request.job_id] = record
            record.task = asyncio.create_task(self._run(record))
            return record

    async def wait_terminal(self, job_id: str, *, timeout_seconds: float) -> JobRecord:
        async with self._jobs_lock:
            record = self._jobs.get(job_id)
            task = record.task if record is not None else None
        if record is None or task is None:
            raise JobValidationError("unknown sandbox job")
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        if record.status not in {"completed", "failed", "timeout", "cancelled"}:
            raise RuntimeError("sandbox job task ended without a terminal status")
        return record

    async def _finish_terminal(
        self,
        record: JobRecord,
        outcome: str,
        *,
        finished_at: datetime | None = None,
    ) -> None:
        should_record = False
        async with self._jobs_lock:
            if record.status not in {"completed", "failed", "timeout", "cancelled"}:
                record.status = outcome
                record.finished_at = finished_at or datetime.now(UTC)
            if not record.telemetry_recorded:
                record.telemetry_recorded = True
                should_record = True
        if not should_record:
            return
        normalized = normalize_sandbox_outcome(record.status)
        self._metrics.counter("sandbox_jobs_total").add(
            1, outcome=normalized, network_policy=record.request.network_policy
        )
        self._metrics.histogram("sandbox_job_duration_seconds").record(
            (record.duration_ms or 0) / 1_000, outcome=normalized
        )
```

The existing `_run` `finally` block calls `await self._finish_terminal(record, terminal_status)` instead of assigning `finished_at/status` directly. `get`, `to_response`, `current_logs`, and `cancel` never invoke telemetry recording; cancelling an already terminal job preserves its terminal status exactly as today. A repeated `_finish_terminal`, repeated status read, or repeated terminal cancel cannot record a second metric. Every second submit with the same job ID—whether the body is identical or different—still raises the existing `JobValidationError` and still maps to HTTP 422.

- [ ] **Step 7: Run focused GREEN and affected suites**

```bash
uv lock
uv run pytest packages/connectors/tests/test_telemetry.py \
  packages/connectors/tests/supabase/test_database_telemetry.py \
  packages/connectors/tests/test_http_client.py \
  services/sandbox_runner/tests/test_telemetry.py \
  services/sandbox_runner/tests/test_job_lifecycle.py \
  services/tool_worker/tests/test_telemetry.py -q
uv run ruff check packages/connectors services/tool_worker services/sandbox_runner
uv run mypy
```

Expected: PASS; connector/sandbox payload canaries do not appear in spans or metrics.

- [ ] **Step 8: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Binding protected-health runner URL seam

The Task 8 contract exposes the pure public helper
`validated_sandbox_runner_base_url() -> str` from its already-owned runner client. The helper
reads the existing environment/default authority; rejects userinfo, query, fragment, non-root
path, unsupported scheme, malformed host/port, and control characters; and returns one normalized
internal base URL. It returns no token and logs no URL. Telemetry runner calls and the protected
tool-heartbeat probe consume this same helper. This reservation changes no Task 8 manifest.

#### Final executable contract for Task 8


After applying the draft and corrected Tasks 1-7, replace Task 8 from its `Files` block through
its staging/commit block with this section. Task 8 consumes predecessor-owned runtimes,
registries, fail-open helpers, and durable once-guards by identity. It does not create a second
registry/provider, move a Task 7 commit point, or turn transport telemetry into product authority.

### 13.1 Keep connector registration stateless and pass request-owned tracers explicitly

There is no tracer-owning `GitHubClient`, `LinearClient`, or `RunnerClient`; the real
connector/runner surface is module-level functions behind stateless global executor tuples. Keep
`ConnectorRegistry`, `default_registry()`, `build_default_catalog()`, and every static tool
tuple stateless. Do not modify or stage
`packages/connectors/src/jhin_connectors/registry.py`, and introduce no mutable global tracer.

Add only these package/direct-call defaults:

~~~python
@dataclass(frozen=True)
class VerifyContext:
    auth_type: str
    credentials: dict[str, str]
    config: dict[str, Any] = field(default_factory=dict)
    tracer: Tracer | None = None

@dataclass(...)
class ToolExecutionContext:
    # Preserve every existing field and default.
    tracer: Tracer | None = None
~~~

The optional values are deliberate package/direct-test no-op boundaries, never production
fallbacks. Connector verification and metadata methods forward `VerifyContext.tracer` through
every concrete outbound client boundary. Every connector/CLI executor forwards
`ToolExecutionContext.tracer` through the concrete HTTP, database, or runner operation.

API verification and metadata routes receive Task 7's exact `ObservabilityRuntimeDep`. Their
service functions require a `tracer: Tracer` argument and populate
`VerifyContext(..., tracer=tracer)`; production service code has no optional/no-op fallback.
Update direct service tests to pass an explicit recording or no-op tracer while preserving lookup,
decryption, audit, commit, response, and error behavior.

The three production tool contexts—two in
`services/tool_worker/src/jhin_tool_worker/activities.py` and one in
`trigger_activities.py`—receive the exact
`self._resources.runtime.tracer`. `CleanupActivities` retains its injectable one-argument
cleanup callable, while tool-worker main injects a tracer-bound callable such as
`functools.partial(delete_sandbox_workspace, tracer=resources.runtime.tracer)`.

Add an import/binding-aware semantic audit over the real context constructors and outbound
module-level functions. It resolves aliases, distinguishes package functions from local test
seams, requires the exact API/tool runtime owner at every API verification/metadata, three
tool-context, and tracer-bound cleanup point, and rejects an omitted/swapped/no-op handle or extra
product caller. A textual suffix match is not evidence. Mutation tests cover imported aliases, all
omissions/swaps, an extra caller, and valid local/package-only seams.

### 13.2 Close Task 2's one operation registry over the real surface

Before Task 2 implementation, extend its sole central `jhin.operation` registry with these exact
constants. Task 8 consumes this registry and declares no second list/set:

~~~text
github:
  verify, installation_token_create, repository_read, branch_list, file_read,
  branch_create, issue_read, issue_comment_create, pull_request_create,
  pull_request_read, pull_request_comment, pull_request_merge, check_read,
  workflow_dispatch, workflow_run_read

linear:
  verify, metadata_read, issue_read, issue_search, issue_create, issue_update,
  comment_create

vercel:
  verify, project_list, project_read, deployment_list, deployment_read,
  deployment_logs_read, environment_metadata_read, deployment_preview_create,
  deployment_redeploy, deployment_promote, deployment_alias_assign

supabase:
  verify, project_read, logs_read, function_list, function_deploy,
  function_delete, execute_read, execute_write

sandbox/model shared:
  generate, submit, status, cancel, cleanup, other
~~~

`ConnectorTelemetry` applies a connector-specific subset after central normalization. A globally
valid Vercel operation paired with GitHub, or any other cross-connector mismatch, becomes
`other`. Every operation comes from a fixed registered function/tool mapping. Unknown, removed,
aliased, malformed, and arbitrary regex-safe strings become `other`; Supabase destructive
database work is always `execute_write`; runner operations are only
`submit|status|cancel|cleanup`.

`retry_count` accepts only a real non-boolean integer and clamps to the exact inclusive
`0..10` range. Current clients have no retry layer and pass exactly `0`. Late
outcome/latency/status values use Task 2's same per-key normalizer and Task 7's fail-open setter;
regex shape alone never authorizes an enum value.

This is a predecessor Task 2 registry amendment inside Task 2's already-authorized registry/tests,
not an extra Task 8 implementation path.

### 13.3 Apply the superseding fail-open shell at every Task 8 boundary

For connector HTTP/database, health sampling, runner client/server, and sandbox job lifecycle:

1. Validate the fixed developer telemetry schema before the authoritative call.
2. If tracer/span setup or enter fails, invoke authoritative work exactly once without a span.
3. Return the exact successful result even if late normalization/attributes/events/status,
   detach/end, metric recording, or owned diagnostic cleanup fails.
4. Preserve the existing public exception type, value, and traceback and exact cancellation
   identity when authoritative work fails. `CancelledError`, `KeyboardInterrupt`, and
   `SystemExit` are not ordinary provider failures.
5. Preserve intentional product mappings such as fixed
   `ProviderHTTPError`/`SandboxRunnerError`; instrumentation may not add, remove, or change one.
6. Attempt sandbox terminal counter and histogram independently and contain either backend
   failure.

Tests inject hostile tracer, context manager, span, propagator, normalizer, metric facade,
counter/histogram, response close, and diagnostic cleanup implementations. Prove one authoritative
call, exact result/error/cancellation, unchanged durable state, and no surviving context,
provider, session, response, sampler/job task, or runtime after success, failure, cancellation, or
early app shutdown.

Task 7's post-commit metrics/spans remain once-only and fail-open. Task 8 does not wrap them in a
larger failure domain, retry them, or record a second terminal tool/trigger metric.

### 13.4 Preserve the one bounded connector HTTP authority

`send_bounded_json` remains the sole authoritative provider boundary. Preserve its existing
client/request/return contract and add keyword-only required `connector_type` and `operation`,
`retry_count: int = 0`, and package-only `tracer: Tracer | None = None`.

Before span creation or transport, retain response-cap validation, nonempty 2xx expected-status
validation, and URL-userinfo rejection. The request remains one
`client.send(..., stream=True, follow_redirects=False)`; enforce declared and streamed byte
caps, strict UTF-8 JSON/shape/status rules, fixed safe public errors, and response closure on every
success/failure/cancellation path. Preserve the existing transport call count and all eight bounded
response behaviors.

The `connector.http` client span contains only normalized connector type, the connector-owned
operation, clamped retry count, closed outcome, bounded latency, and response status. It never
receives or stringifies URL/host/path/query/userinfo, request/response/trace headers, request or
response body, credentials, provider error/exception arguments/causes/context, connection/
workspace/external IDs, SQL/DSN, or tool input/output. Do not call `str()` or `repr()` on
untrusted request/response/exception/provider values for telemetry.

Every production call in GitHub auth/client, Linear client, Vercel client, and Supabase management
client forwards the request-owned tracer plus fixed connector/operation constants. Their connector
and tool module-level functions carry those exact handles; no static registry state is changed.
Update the eight existing direct bounded-response tests only with explicit
connector/operation/no-op tracer arguments and preserve their assertions.

An import-aware audit finds exactly those eight direct test calls plus all real production calls,
resolves aliases, and rejects a bypass or missing/mismatched constant. Tests compare complete span
name/kind/parent/resource/attributes/events/status description/end state, scan raw and encoded
canaries across spans/metrics, and use hostile tracer/span/close doubles to prove one transport
call with unchanged product behavior.

### 13.5 Wrap exactly the two real asyncpg authorities

Keep this public Task 11 contract exact:

~~~python
async def trace_connector_database(
    operation: Literal["verify", "execute_read", "execute_write"],
    action: Callable[[], Awaitable[T]],
    *,
    tracer: Tracer | None = None,
) -> T: ...
~~~

Endpoint, schema, SQL, bind-count, read/write, least-privilege, and destructive validation remains
outside the span and before asyncpg. The one `connector.database` span surrounds exactly one
action that owns connect, verification or transaction, commit/rollback, and close/cleanup.
Preserve timeouts, transaction/rollback authority, cancellation/error mapping, connection closure,
SQL result, and destructive behavior. Destructive execution maps to `execute_write`.

No SQL verb/text/hash, statement argument, DSN/user/password/host, schema/resource ID, row/result,
exception text, or arbitrary object serialization enters telemetry. An import-aware audit proves
the wrapper exists at exactly the verification and execution asyncpg boundaries, remains outside
prevalidation, and cannot be bypassed by aliasing.

Tests cover success, connect failure, transaction/commit/rollback/close failure, cancellation,
hostile diagnostics, one action call, exact business result/error, and complete privacy
serialization.

### 13.6 Give connection-health sampling a finite, leak-free lifecycle

The query selects only connector type, status, last-verified timestamp, and
`last_error IS NOT NULL`, excludes disabled rows in SQL, and never loads/decrypts a credential,
ID, config, or error text or calls a connector.

Normalize connector type before grouping. Emit no series for a type with zero enabled rows. A type
is healthy only if every enabled row is active, fresh, and has no error.
`connector_health` emits one `0|1` observation for each present normalized connector type.
`connector_connections` emits only positive healthy/unhealthy counts. All unknown types coalesce
into one `other` family. Invalid, naive, future, missing, or malformed timestamps are
conservatively unhealthy without arbitrary stringification.

Compute and validate both complete tuples before calling either setter. Query/normalization
failure calls neither setter and retains both prior tuples. Task 3 replaces each gauge separately,
so do not claim cross-gauge atomicity: if either backend setter fails, contain it and still attempt
the other according to the fixed order
`connector_health` then `connector_connections`.

Use exact defaults and bounds:

~~~python
CONNECTOR_HEALTH_INTERVAL_SECONDS = 30.0
CONNECTOR_HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
MAX_CONNECTOR_HEALTH_PROBE_TIMEOUT_SECONDS = 30.0
CONNECTOR_HEALTH_FRESHNESS_SECONDS = 300
~~~

`interval_seconds` is a finite positive non-boolean number.
`probe_timeout_seconds` is finite, positive, non-boolean, and no greater than 30 seconds.
`freshness_seconds` is a positive non-boolean integer. Each query is time-bounded. Interval wait
uses `stop.wait()` so `stop.set()` wakes immediately.

Tool-worker starts exactly one named sampler only after resources/runtime exist. It is independent
of the Temporal worker and product TaskGroup; query, normalization, timeout, logging, or telemetry
failure cannot cancel product work. On worker construction/start/run/stop/cancellation or resource
cleanup failure, set stop, cancel if necessary, await and prove the sampler done, then close
resources and finally shut down Task 6's exact runtime. No sampler task/session/context survives.

Use fake session/query/clock/metrics only—no Docker/network/live database. Cover empty rows, all
healthy, stale/error/disabled, multiple rows per type, unknown coalescing, exact freshness
boundary, malformed/future timestamps, query/normalization failure retaining prior tuples,
independent setter failures, query timeout, every invalid timing input, startup/worker/cleanup
failure, immediate stop, cancellation, and zero pending tasks.

### 13.7 Parent runner propagation inside a bounded streaming transport

Keep `_headers(token)` authorization-only. Each submit, every status poll, cancel, and workspace
cleanup enters one `sandbox.client` span first, then copies the base headers and injects trace
context into that per-request copy before its single transport call. The outgoing
`traceparent` parent span ID must equal the actual client span ID. Do not mutate caller
header/payload/env/secret-env mappings, and never put trace headers into job environment data.

Replace buffering `AsyncClient.request().json()` behavior with one redirect-free streaming
transport that:

- validates the internal base URL before constructing a client and rejects userinfo, query,
  fragment, or non-root path;
- validates job/workspace identifiers with `fullmatch` before path construction;
- validates every timeout as a finite positive non-boolean value;
- enforces one exact positive response byte cap;
- reads/decode strict JSON only within that cap, accepts only a mapping, and closes the response
  on success, ordinary failure, and cancellation;
- maps transport/status/parse/close failures to fixed safe `SandboxRunnerError` values without
  URL/response/identifier/exception text; and
- never lets owned transport closure replace an already authoritative business exception.

Validate `sandbox_max_output_bytes` in settings/schema/config as a real integer in the exact
inclusive `1..65_536` range. The fixed runner JSON response cap is exactly
`MAX_SANDBOX_RUNNER_RESPONSE_BYTES = 851_968`
(`2 * 6 * 65_536 + 65_536`). Test exactly 851,968 bytes and one byte over. The client cap is not
derived from an unreviewed deployment override.

Validate job ID before computing deadline/path and validate caller job timeout before creating a
client. Use `asyncio.get_running_loop().time()`; retain fixed poll interval, grace, cancellation,
and deadline behavior, and do not let diagnostic setup/recording extend the product deadline.
Standalone/direct calls use the explicit no-op tracer default. Production CLI executors and the
tracer-bound cleanup callable pass Task 6's exact tracer. Cleanup stays best-effort and idempotent.

### 13.8 Keep sandbox server and terminal-job ownership exact

After Task 6 installs all existing routes, wrap them once with a route-bound pure-ASGI owner. Match
the registered method/template—never exported raw path text—to this exact operation table:

~~~text
POST   /v1/jobs                          -> submit
GET    /v1/jobs/{job_id}                 -> status
GET    /v1/jobs/{job_id}/logs            -> status
POST   /v1/jobs/{job_id}/cancel          -> cancel
DELETE /v1/workspaces/{workspace_key}    -> cleanup
all other registered routes              -> other
~~~

The wrapper safely extracts only trace context, creates one `sandbox.server` span with the
explicit app runtime tracer, and covers the complete ASGI response lifetime. Fixed
route/operation/status-class/outcome/latency attributes use the central normalizer. It never reads,
attaches, or logs request body/output, Authorization, trace header, raw path, Docker error,
env/secret-env, or workspace key. Invalid inbound context becomes a safe root. Downstream,
send/receive, span, and detach failures preserve the ASGI result/error/cancellation and close the
context exactly once.

`JobManager` accepts optional package/direct-test `metrics` and `tracer`; Task 6's real
`create_app` passes the exact `active_runtime.metrics` and `.tracer`. Manager construction
and middleware/route installation stay within Task 6's cleanup-protected factory block. No global
runtime is introduced. No-lifespan tests inject and close their own runtime; app-owned and injected
shutdown identities remain exact under factory/start/close failure.

Validate job IDs using `fullmatch` with exact length `8..64`; reject the newline-suffix edge.
`JobManager._run` starts `sandbox.job.lifecycle` only after a validated `JobRecord` exists.
Keep `asyncio.create_task` inside the submit server span so its copied context makes lifecycle the
exact child of that submit span. The only identifier attribute is the validated job ID; no metric
contains it. Network policy/outcome use closed registries. Never inspect command, image, output,
error, env/secret, or workspace data for telemetry.

Do not add `_jobs_lock`. The current duplicate-submit and terminal once-decisions have no
suspension before mutation and are atomic in one event loop; prove concurrent identical/different
duplicate submit and concurrent terminal finalize. Preserve duplicate HTTP 422.

`wait_terminal` is side-effect-free, shielded, and time-bounded. `_finish_terminal` preserves
the first terminal status/timestamp, sets `telemetry_recorded=True` before diagnostics, and only
that owner attempts terminal metrics. Counter and histogram are attempted independently,
fail-open, and never retried. Duration is `max(0, finished_at - started_at)` only for valid aware
timestamps; malformed/missing timestamps omit the histogram without suppressing the counter.
Status/log reads, replay finalize, duplicate submit, and repeated cancel emit no terminal metric
and do not mutate terminal state.

Test completed, failed, timeout, cancelled, concurrent finalize, identical/different duplicate
submit, repeated cancel/status/log, malformed/missing timestamps, hostile metrics/tracer,
job-task cancellation, runner/app factory/start/close failure, and zero pending job/context.
Container deletion remains before terminal publication on every path.

### 13.9 Make every Task 8 RED fixture and dependency explicit

Every new/affected telemetry test file owns function-scoped tracer/provider/exporter and
metric-provider/reader fixtures, observation and full-serialization helpers, fake
session/query/clock/transport objects, and `finally` teardown. Delay imports of a not-yet-created
production module until test execution so RED reports a named missing behavior/assertion rather
than collection `ImportError`, `NameError`, abstract fixture, leaked global provider, or a
Docker/network dependency.

Direct dependency ownership is exact:

- `packages/connectors/pyproject.toml` adds workspace `jhin-observability` and direct
  `opentelemetry-api>=1.38,<2`;
- `packages/tools/pyproject.toml` preserves Task 7's workspace `jhin-observability` and adds
  direct `opentelemetry-api>=1.38,<2` for `ToolExecutionContext.tracer`;
- `services/sandbox_runner/pyproject.toml` preserves Task 6's workspace
  `jhin-observability` and adds direct `opentelemetry-api>=1.38,<2`; and
- no production distribution imports the OTel SDK.

Regenerate `uv.lock` once after all three manifest edits, then require `uv lock --check`.

### 13.10 Run exact socket-free RED and broad GREEN gates

After corrected Tasks 1-7 exist, create every Task 8 helper/test and affected contract update
first. Run these three independent RED groups:

~~~bash
uv run pytest \
  packages/connectors/tests/test_http_client.py \
  packages/connectors/tests/test_telemetry.py \
  packages/connectors/tests/supabase/test_database_telemetry.py \
  apps/api/tests/test_connector_telemetry.py \
  apps/api/tests/test_connections_unit.py -q

uv run pytest \
  packages/tools/tests/test_telemetry.py \
  services/tool_worker/tests/test_telemetry.py -q

uv run pytest \
  services/sandbox_runner/tests/test_telemetry.py \
  services/sandbox_runner/tests/test_job_config.py \
  services/sandbox_runner/tests/test_job_lifecycle.py \
  services/sandbox_runner/tests/test_api_auth.py -q
~~~

Expected RED names missing explicit runtime wiring, connector-specific operation normalization,
bounded/fail-open transport/span behavior, client/server/job parentage, health lifecycle, terminal
once-recording, or hard caps. Implement in that order and make each group GREEN. Collection
imports, undefined fixtures, global leakage, or Docker/network/live-database failures are invalid
RED.

Then run:

~~~bash
uv lock
uv lock --check
uv run pytest \
  packages/observability/tests \
  packages/tools/tests \
  packages/connectors/tests \
  apps/api/tests \
  services/tool_worker/tests \
  services/sandbox_runner/tests -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
~~~

All commands pass. Root collection includes live-marked integration modules while their live cases
remain deselected. Task 8 unit gates use no Docker daemon, network service, or live database.

### 13.11 Preserve Task 6/7 ownership and bind Task 11/12 evidence

- **Task 6:** preserve exact runtime identity/ownership, sandbox
  `create_app(settings, runtime=None)` factory/lifespan cleanup, worker resource shutdown,
  Temporal interceptor, and absence of a global provider. Task 8 consumes only its tracer/metrics.
- **Task 7:** preserve `completed_at is None` as agent finalization once-guard, catalog-member
  tool family, no invocation-mismatch metric, trigger started-row reconciliation, and every
  post-commit fail-open call. Task 8's tracer field is transport-only and cannot affect durable
  outcome accounting.
- **Task 11:** retain the exact `trace_connector_database` signature and exact health-gauge
  series. The connected trace asserts parent **edges**, not only a shared trace ID: connector span
  is a child of its active API/tool span; each `sandbox.server` is the exact child of its matching
  `sandbox.client`; `sandbox.job.lifecycle` is the exact child of the submit server span.
  Exercise Task 7's trigger barrier, sandbox terminal replay/once behavior, and complete raw plus
  encoded canary scans over spans, events/status, metrics, and logs.
- **Task 12:** rerun lock, full pytest collection, Ruff check/format, and mypy. Live monitoring and
  connected infrastructure remain Task 11/12 work, never a Task 8 unit dependency.

### 13.12 Make Task 8's 44 paths and committed tree exact

Replace Task 8 `Files`, the corresponding global File Map entries, and staging with this exact
mirrored array. `registry.py` and tool-worker `resources.py` are deliberately absent because
the catalog stays stateless and Task 6 owns the runtime graph.

~~~bash
set -euo pipefail
task8_paths=(
  apps/api/src/jhin_api/connections/router.py
  apps/api/src/jhin_api/connections/service.py
  apps/api/tests/test_connections_unit.py
  apps/api/tests/test_connector_telemetry.py
  packages/connectors/pyproject.toml
  packages/connectors/src/jhin_connectors/base.py
  packages/connectors/src/jhin_connectors/cli/runner_client.py
  packages/connectors/src/jhin_connectors/cli/tools.py
  packages/connectors/src/jhin_connectors/github/auth.py
  packages/connectors/src/jhin_connectors/github/client.py
  packages/connectors/src/jhin_connectors/github/connector.py
  packages/connectors/src/jhin_connectors/github/tools.py
  packages/connectors/src/jhin_connectors/http_client.py
  packages/connectors/src/jhin_connectors/linear/client.py
  packages/connectors/src/jhin_connectors/linear/connector.py
  packages/connectors/src/jhin_connectors/linear/tools.py
  packages/connectors/src/jhin_connectors/supabase/connector.py
  packages/connectors/src/jhin_connectors/supabase/database_client.py
  packages/connectors/src/jhin_connectors/supabase/database_tools.py
  packages/connectors/src/jhin_connectors/supabase/management_client.py
  packages/connectors/src/jhin_connectors/supabase/management_tools.py
  packages/connectors/src/jhin_connectors/telemetry.py
  packages/connectors/src/jhin_connectors/vercel/client.py
  packages/connectors/src/jhin_connectors/vercel/connector.py
  packages/connectors/src/jhin_connectors/vercel/tools.py
  packages/connectors/tests/supabase/test_database_telemetry.py
  packages/connectors/tests/test_http_client.py
  packages/connectors/tests/test_telemetry.py
  packages/tools/pyproject.toml
  packages/tools/src/jhin_tools/builtin.py
  packages/tools/tests/test_telemetry.py
  services/sandbox_runner/pyproject.toml
  services/sandbox_runner/src/jhin_sandbox_runner/jobs.py
  services/sandbox_runner/src/jhin_sandbox_runner/main.py
  services/sandbox_runner/src/jhin_sandbox_runner/schemas.py
  services/sandbox_runner/src/jhin_sandbox_runner/settings.py
  services/sandbox_runner/tests/test_job_config.py
  services/sandbox_runner/tests/test_telemetry.py
  services/tool_worker/src/jhin_tool_worker/activities.py
  services/tool_worker/src/jhin_tool_worker/cleanup_activities.py
  services/tool_worker/src/jhin_tool_worker/main.py
  services/tool_worker/src/jhin_tool_worker/trigger_activities.py
  services/tool_worker/tests/test_telemetry.py
  uv.lock
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task8_paths[@]}"
git diff --check -- "${task8_paths[@]}"
git add -- "${task8_paths[@]}"
expected_index="$(printf '%s\n' "${task8_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task8_paths[@]}"
git commit --only "${task8_paths[@]}" \
  -m "feat(observability): trace connector and sandbox boundaries"
test "$(git show -s --format=%s HEAD)" = \
  "feat(observability): trace connector and sandbox boundaries"
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)"
test "$actual_commit_paths" = "$expected_index"
test -z "$(git diff --cached --name-only)"
~~~

The Task 8 `Files`, File Map, and array are exact mirrors. No other Task 8 path is authorized.
Any discovered affected path requires a reviewed amendment before it is touched. The Task 2
operation-registry retrofix remains in its predecessor-owned paths and is not a Task 8 path. The
global index-only exception remains sole: any pre-staged/unexpected/missing path, path outside
`Files`, commit-tree mismatch, or non-empty post-commit index fails closed.

### Task 9: Give the Next.js Server the Same JSON-v1 Contract

**Files:**
- Modify: `apps/web/Dockerfile`
- Modify: `apps/web/instrumentation.ts`
- Modify: `apps/web/lib/server-log-contract.json`
- Modify: `apps/web/lib/server-logger.ts`
- Modify: `apps/web/next.config.ts`
- Modify: `apps/web/server-wrapper.cjs`
- Modify: `apps/web/tests/instrumentation.test.ts`
- Modify: `apps/web/tests/server-logger.test.ts`
- Modify: `apps/web/tests/server-only-stub.ts`
- Modify: `apps/web/tests/server-wrapper.test.ts`
- Modify: `apps/web/vitest.config.ts`
- Modify: `tests/test_web_json_stdout.py`

**Interfaces:**
- Consumes the accepted Task 8 handoff and produces the exact Task 9 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing TypeScript contract and redaction tests**

```typescript
it("emits the Python-compatible JSON v1 shape", () => {
  const write = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  serverLog("info", "web.started", { request_id: "req-1" });
  const record = JSON.parse(String(write.mock.calls[0][0]));
  expect(record).toMatchObject({
    schema_version: 1,
    level: "info",
    service: "web",
    environment: "test",
    event: "web.started",
    logger: "jhin.web",
    request_id: "req-1",
  });
  expect(new Date(record.timestamp).toISOString()).toBe(record.timestamp);
});


it("redacts structural secrets and strips URL userinfo/query/fragment", () => {
  const line = capture(() =>
    serverError("web.request_failed", new Error("error-canary"), {
      authorization: "Bearer auth-canary",
      nested: { private_key: "key-canary" },
      target: "https://user:pass@example.test/p?token=query-canary#fragment-canary",
    }),
  );
  for (const canary of ["auth-canary", "key-canary", "user", "pass", "query-canary", "fragment-canary", "error-canary"]) {
    expect(line).not.toContain(canary);
  }
});


it("discards unknown events and fields without retaining free text", () => {
  const line = capture(() =>
    serverLog("warning", "attacker free text", { detail: "foreign-detail-canary" }),
  );
  expect(JSON.parse(line).event).toBe("log.event_rejected");
  expect(line).not.toContain("attacker free text");
  expect(line).not.toContain("foreign-detail-canary");
});


it("redacts a registered known value under an otherwise safe key", () => {
  registerServerLogSecret("web-known-secret-canary");
  const line = capture(() =>
    serverLog("error", "web.request_failed", {
      request_id: "web-known-secret-canary",
      error_code: "internal_error",
    }),
  );
  expect(line).not.toContain("web-known-secret-canary");
  expect(line).toContain("[REDACTED]");
});
```

- [ ] **Step 2: Run web RED**

```bash
pnpm --filter jhin-web test -- server-logger.test.ts
```

Expected: FAIL because no server logger exists.

- [ ] **Step 3: Implement a server-only logger with matching caps**

Begin `server-logger.ts` with `import "server-only"`. Mirror Task 1 constants and implement:

```typescript
export type LogLevel = "debug" | "info" | "warning" | "error";
export type LogFields = Record<string, unknown>;

const WEB_EVENT_FIELDS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  "web.started": [],
  "web.stopping": ["signal"],
  "web.rewrite_configured": ["http_route"],
  "web.request_failed": ["http_method", "http_route", "error_code"],
  "web.framework_output_suppressed": ["stream", "count"],
  "log.event_rejected": [],
});
const CONTEXT_FIELDS = new Set([
  "request_id", "correlation_id", "workspace_id", "task_id", "run_id", "trace_id", "span_id",
]);
const SENSITIVE = new Set([
  "authorization", "cookie", "password", "secret", "token", "api_key", "private_key",
  "dsn", "prompt", "completion", "sql", "tool_input", "tool_output", "request_body",
  "response_body", "webhook_payload", "secret_env",
]);
const SENSITIVE_SUFFIXES = [
  "_authorization", "_cookie", "_password", "_secret", "_token", "_api_key",
  "_private_key", "_dsn",
];
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const WEB_ENUMS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  signal: ["SIGINT", "SIGTERM", "other"],
  http_method: ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"],
  http_route: ["/api/:path*", "other"],
  error_code: ["internal_error"],
  stream: ["stdout", "stderr"],
});

const knownSecretValues = new Set<string>();

export function registerServerLogSecret(value: string): void {
  if (value.length >= 6) knownSecretValues.add(value);
}

function normalizedKey(key: string): string {
  return key.replace(/([a-z0-9])([A-Z])/g, "$1_$2").replace(/[^A-Za-z0-9]+/g, "_").toLowerCase();
}

function isSensitiveKey(key: string): boolean {
  const normalized = normalizedKey(key);
  return SENSITIVE.has(normalized)
    || SENSITIVE_SUFFIXES.some((suffix) => normalized.endsWith(suffix));
}

function structuralRedact(value: unknown, depth = 0): unknown {
  if (depth >= 8) return "[TRUNCATED]";
  if (typeof value === "string") {
    if (value.includes("://")) {
      try {
        const parsed = new URL(value);
        return `${parsed.protocol}//${parsed.host}${parsed.pathname}`.slice(0, 2000);
      } catch { /* treat as a bounded ordinary string */ }
    }
    return value.slice(0, 2000);
  }
  if (typeof value === "number" || typeof value === "boolean" || value === null) return value;
  if (Array.isArray(value)) return value.slice(0, 64).map((item) => structuralRedact(item, depth + 1));
  if (typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).slice(0, 64).map(
      ([key, item]) => [key.slice(0, 128), isSensitiveKey(key)
        ? "[REDACTED]" : structuralRedact(item, depth + 1)],
    ));
  }
  return "[UNSUPPORTED]";
}

function redactKnownValues(value: unknown): unknown {
  if (typeof value === "string") {
    let output = value;
    for (const secret of [...knownSecretValues].sort((a, b) => b.length - a.length)) {
      output = output.replaceAll(secret, "[REDACTED]");
    }
    return output;
  }
  if (Array.isArray(value)) return value.map(redactKnownValues);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, redactKnownValues(item)]),
    );
  }
  return value;
}

function filterFields(event: string, fields: LogFields): LogFields {
  const acceptedEvent = Object.hasOwn(WEB_EVENT_FIELDS, event) ? event : "log.event_rejected";
  const allowed = new Set([...CONTEXT_FIELDS, ...WEB_EVENT_FIELDS[acceptedEvent]]);
  const output: LogFields = { event: acceptedEvent };
  for (const [key, value] of Object.entries(fields)) {
    if (!allowed.has(key)) continue;
    if (CONTEXT_FIELDS.has(key) && (typeof value !== "string" || !ID.test(value))) continue;
    if (key === "count") {
      if (!Number.isSafeInteger(value) || Number(value) < 0) continue;
      output[key] = value;
      continue;
    }
    if (Object.hasOwn(WEB_ENUMS, key)) {
      output[key] = typeof value === "string" && WEB_ENUMS[key].includes(value) ? value : "other";
    } else {
      output[key] = value;
    }
  }
  return output;
}

export function serverLog(level: LogLevel, event: string, fields: LogFields = {}): void {
  const filtered = filterFields(event, fields);
  const rawEnvironment = process.env.APP_ENV ?? process.env.NODE_ENV ?? "production";
  const environment = ["dev", "test", "staging", "production"].includes(rawEnvironment)
    ? rawEnvironment : "production";
  const record = redactKnownValues(structuralRedact({
    schema_version: 1,
    timestamp: new Date().toISOString(),
    level,
    service: "web",
    environment,
    logger: "jhin.web",
    ...filtered,
  }));
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

export function serverError(event: string, error: unknown, fields: LogFields = {}): void {
  void error;
  serverLog("error", event, {
    ...fields,
    error_code: "internal_error",
  });
}
```

`serverError` deliberately does not inspect `error`; add `void error` to satisfy lint. It never serializes messages, stacks, request/response objects, headers, URL, cookies, bodies, React props, or cause objects.

- [ ] **Step 4: Register only lifecycle/rewrite/unexpected-error events**

```typescript
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    serverLog("info", "web.started");
    serverLog("info", "web.rewrite_configured", { http_route: "/api/:path*" });
  }
}

export const onRequestError: Instrumentation.onRequestError = async (
  error,
  request,
  context,
) => {
  serverError("web.request_failed", error, {
    http_method: request.method,
    http_route: normalizeNextRoute(context.routePath),
  });
};
```

`normalizeNextRoute` is exact and never examines `request.path`:

```typescript
const NEXT_ROUTES = new Set(["/api/:path*"]);
export function normalizeNextRoute(route: string | undefined): string {
  return route !== undefined && NEXT_ROUTES.has(route) ? route : "other";
}
```

Replace `next.config.ts` with this complete config (there is no conditional or version-dependent logging setting):

```typescript
import type { NextConfig } from "next";

const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
const nextConfig: NextConfig = {
  output: "standalone",
  logging: { fetches: { fullUrl: false } },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiInternalUrl}/api/:path*` }];
  },
};
export default nextConfig;
```

- [ ] **Step 5: Add the standalone server process wrapper and its unit test**

Create `server-wrapper.test.ts` first with the socket-free `FakeChild` tests shown below and this import assertion, before creating the wrapper:

```typescript
it("loads the bounded standalone wrapper", () => {
  expect(wrapper.startWrapper).toBeTypeOf("function");
  expect(wrapper.lineGuard).toBeTypeOf("function");
});
```

Run the wrapper-specific RED:

```bash
pnpm --filter jhin-web test -- server-wrapper.test.ts
```

Expected: FAIL with `Cannot find module '../server-wrapper.cjs'`; only then create the wrapper below.

`server-wrapper.cjs` is plain CommonJS so the runtime image executes it without TypeScript tooling. It is the only parent of the generated standalone server and the only writer inherited by Docker:

```javascript
"use strict";
const { spawn } = require("node:child_process");
const { StringDecoder } = require("node:string_decoder");

const MAX_FRAME_BYTES = 64 * 1024;
const INPUT_CHUNK_BYTES = 16 * 1024;
const MAX_SUPPRESSED_COUNT = 1_000_000;
const SUPPRESSION_FLUSH_MILLIS = 1_000;

const rawWrite = (chunk) => process.stdout.write(chunk);
const BASE_KEYS = new Set([
  "schema_version", "timestamp", "level", "service", "environment", "event", "logger",
  "request_id", "correlation_id", "workspace_id", "task_id", "run_id", "trace_id", "span_id",
]);
const EVENT_KEYS = {
  "web.started": new Set(),
  "web.stopping": new Set(["signal"]),
  "web.rewrite_configured": new Set(["http_route"]),
  "web.request_failed": new Set(["http_method", "http_route", "error_code"]),
  "web.framework_output_suppressed": new Set(["stream", "count"]),
  "log.event_rejected": new Set(),
};
const WRAPPER_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const WRAPPER_CONTEXT_KEYS = new Set([
  "request_id", "correlation_id", "workspace_id", "task_id", "run_id", "trace_id", "span_id",
]);
const WRAPPER_ENUMS = {
  signal: new Set(["SIGINT", "SIGTERM", "other"]),
  http_method: new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"]),
  http_route: new Set(["/api/:path*", "other"]),
  error_code: new Set(["internal_error"]),
  stream: new Set(["stdout", "stderr"]),
};
function emit(event, fields = {}) {
  const rawEnvironment = process.env.APP_ENV || process.env.NODE_ENV || "production";
  const environment = ["dev", "test", "staging", "production"].includes(rawEnvironment)
    ? rawEnvironment : "production";
  rawWrite(`${JSON.stringify({
    schema_version: 1,
    timestamp: new Date().toISOString(),
    level: event === "web.request_failed" ? "error" : "info",
    service: "web",
    environment,
    event,
    logger: "jhin.web.wrapper",
    ...fields,
  })}\n`);
}

function validApplicationJson(line) {
  try {
    const value = JSON.parse(line);
    const shape = value && value.schema_version === 1 && value.service === "web"
      && typeof value.timestamp === "string" && !Number.isNaN(Date.parse(value.timestamp))
      && ["debug", "info", "warning", "error"].includes(value.level)
      && ["dev", "test", "staging", "production"].includes(value.environment)
      && typeof value.event === "string" && value.logger === "jhin.web"
      && Object.hasOwn(EVENT_KEYS, value.event)
      && Object.keys(value).every((key) => BASE_KEYS.has(key) || EVENT_KEYS[value.event].has(key));
    if (!shape) return false;
    for (const key of WRAPPER_CONTEXT_KEYS) {
      if (Object.hasOwn(value, key) && (typeof value[key] !== "string" || !WRAPPER_ID.test(value[key]))) {
        return false;
      }
    }
    for (const [key, allowed] of Object.entries(WRAPPER_ENUMS)) {
      if (Object.hasOwn(value, key) && !allowed.has(value[key])) return false;
    }
    return !Object.hasOwn(value, "count")
      || (Number.isSafeInteger(value.count) && value.count >= 0);
  } catch {
    return false;
  }
}

function lineGuard(stream) {
  let decoder = new StringDecoder("utf8");
  let parts = [];
  let bufferedBytes = 0;
  let droppingOversized = false;
  let suppressed = 0;
  let suppressionTimer;

  const flushSuppressed = () => {
    if (suppressionTimer) clearTimeout(suppressionTimer);
    suppressionTimer = undefined;
    if (suppressed > 0) {
      emit("web.framework_output_suppressed", { stream, count: suppressed });
      suppressed = 0;
    }
  };
  const noteSuppressed = () => {
    suppressed = Math.min(MAX_SUPPRESSED_COUNT, suppressed + 1);
    if (!suppressionTimer) {
      suppressionTimer = setTimeout(flushSuppressed, SUPPRESSION_FLUSH_MILLIS);
      suppressionTimer.unref?.();
    }
  };
  const finishLine = () => {
    if (droppingOversized) noteSuppressed();
    else {
      const line = parts.join("");
      if (validApplicationJson(line)) rawWrite(`${line}\n`);
      else if (bufferedBytes > 0) noteSuppressed();
    }
    parts = [];
    bufferedBytes = 0;
    droppingOversized = false;
  };
  const consume = (text) => {
    let cursor = 0;
    for (;;) {
      const newline = text.indexOf("\n", cursor);
      const end = newline < 0 ? text.length : newline;
      const segment = text.slice(cursor, end);
      if (!droppingOversized && segment.length > 0) {
        const segmentBytes = Buffer.byteLength(segment, "utf8");
        if (bufferedBytes + segmentBytes > MAX_FRAME_BYTES) {
          parts = [];
          bufferedBytes = 0;
          droppingOversized = true;
        } else {
          parts.push(segment);
          bufferedBytes += segmentBytes;
        }
      }
      if (newline < 0) return;
      finishLine();
      cursor = newline + 1;
    }
  };
  return {
    push(chunk) {
      const input = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      for (let offset = 0; offset < input.length; offset += INPUT_CHUNK_BYTES) {
        consume(decoder.write(input.subarray(offset, offset + INPUT_CHUNK_BYTES)));
      }
    },
    flush() {
      consume(decoder.end());
      if (droppingOversized || bufferedBytes > 0) finishLine();
      flushSuppressed();
      decoder = new StringDecoder("utf8");
    },
    bufferedByteLength() { return bufferedBytes; },
  };
}

function startWrapper(spawnImpl = spawn) {
  const child = spawnImpl(process.execPath, ["apps/web/server.js"], {
    cwd: __dirname,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stdout = lineGuard("stdout");
  const stderr = lineGuard("stderr");
  child.stdout.on("data", (chunk) => stdout.push(chunk));
  child.stderr.on("data", (chunk) => stderr.push(chunk));

  let stopping = false;
  let requestedSignal;
  let killTimer;
  const signalHandlers = new Map();
  const stop = (signal) => {
    if (stopping) return;
    stopping = true;
    requestedSignal = signal;
    emit("web.stopping", { signal });
    child.kill(signal);
    killTimer = setTimeout(() => child.kill("SIGKILL"), 10_000);
    killTimer.unref();
  };
  for (const signal of ["SIGINT", "SIGTERM"]) {
    const handler = () => stop(signal);
    signalHandlers.set(signal, handler);
    process.once(signal, handler);
  }
  child.once("error", () => {
    emit("web.request_failed", { error_code: "internal_error" });
    process.exitCode = 1;
  });
  child.once("exit", (code, signal) => {
    stdout.flush();
    stderr.flush();
    if (killTimer) clearTimeout(killTimer);
    for (const [registeredSignal, handler] of signalHandlers) {
      process.removeListener(registeredSignal, handler);
    }
    const graceful = stopping && signal !== "SIGKILL"
      && (signal === requestedSignal || code === 0);
    process.exitCode = graceful ? 0 : (code ?? 1);
  });
  return { child, stop };
}

if (require.main === module) startWrapper();
module.exports = { lineGuard, startWrapper, validApplicationJson };
```

The Vitest test uses the exported constructor and a socket-free child:

```typescript
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { afterEach, describe, expect, it, vi } from "vitest";

const wrapper = require("../server-wrapper.cjs") as {
  startWrapper: (spawnImpl: () => FakeChild) => {
    child: FakeChild;
    stop: (signal: "SIGINT" | "SIGTERM") => void;
  };
  lineGuard: (stream: "stdout" | "stderr") => {
    push: (chunk: Buffer | string) => void;
    flush: () => void;
    bufferedByteLength: () => number;
  };
};

class FakeChild extends EventEmitter {
  stdout = new PassThrough();
  stderr = new PassThrough();
  kill = vi.fn<(signal: NodeJS.Signals) => boolean>(() => true);
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  process.exitCode = undefined;
});

it("passes only strict application JSON and exits zero after graceful SIGTERM", () => {
  vi.useFakeTimers();
  const write = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  const child = new FakeChild();
  const control = wrapper.startWrapper(() => child);
  const valid = JSON.stringify({
    schema_version: 1,
    timestamp: "2026-08-18T00:00:00.000Z",
    level: "info",
    service: "web",
    environment: "test",
    event: "web.started",
    logger: "jhin.web",
  });
  child.stdout.write(valid.slice(0, 20));
  child.stdout.write(`${valid.slice(20)}\nframework stdout canary\n`);
  child.stderr.write("framework stderr canary\n");
  control.stop("SIGTERM");
  expect(child.kill).toHaveBeenCalledWith("SIGTERM");
  child.emit("exit", 0, null);
  expect(process.exitCode).toBe(0);
  const output = write.mock.calls.map(([chunk]) => String(chunk)).join("");
  expect(output).toContain(valid);
  expect(output).not.toContain("framework stdout canary");
  expect(output).not.toContain("framework stderr canary");
  for (const line of output.trim().split("\n")) expect(() => JSON.parse(line)).not.toThrow();
});

it("kills and exits nonzero when the child ignores the graceful deadline", () => {
  vi.useFakeTimers();
  vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  const child = new FakeChild();
  const control = wrapper.startWrapper(() => child);
  control.stop("SIGTERM");
  vi.advanceTimersByTime(10_001);
  expect(child.kill).toHaveBeenLastCalledWith("SIGKILL");
  child.emit("exit", null, "SIGKILL");
  expect(process.exitCode).toBe(1);
});

it("bounds a huge unterminated framework line and aggregates suppression", () => {
  const write = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  const guard = wrapper.lineGuard("stderr");
  const canary = "huge-no-newline-canary";
  guard.push(Buffer.from(canary.repeat(700_000), "utf8"));
  expect(guard.bufferedByteLength()).toBeLessThanOrEqual(64 * 1024);
  guard.flush();
  const output = write.mock.calls.map(([chunk]) => String(chunk)).join("");
  expect(output).not.toContain(canary);
  const records = output.trim().split("\n").map((line) => JSON.parse(line));
  expect(records).toEqual([
    expect.objectContaining({
      event: "web.framework_output_suppressed", stream: "stderr", count: 1,
    }),
  ]);
});

it("aggregates many suppressed lines into one bounded count", () => {
  const write = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  const guard = wrapper.lineGuard("stdout");
  guard.push(Buffer.from("one-canary\ntwo-canary\nthree-canary\n"));
  guard.flush();
  const records = write.mock.calls
    .map(([chunk]) => String(chunk).trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
  expect(records).toEqual([
    expect.objectContaining({event: "web.framework_output_suppressed", count: 3}),
  ]);
});

it("saturates the aggregate instead of overflowing on hostile line volume", () => {
  const write = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  const guard = wrapper.lineGuard("stderr");
  guard.push(Buffer.from("x\n".repeat(1_000_005)));
  guard.flush();
  const records = write.mock.calls
    .map(([chunk]) => String(chunk).trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
  expect(records).toEqual([
    expect.objectContaining({
      event: "web.framework_output_suppressed", stream: "stderr", count: 1_000_000,
    }),
  ]);
});

it("uses StringDecoder across split UTF-8 framework chunks", () => {
  const write = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  const guard = wrapper.lineGuard("stdout");
  const line = Buffer.from("framework-💥-canary\n", "utf8");
  const emojiStart = Buffer.from("framework-", "utf8").length;
  guard.push(line.subarray(0, emojiStart + 2));
  guard.push(line.subarray(emojiStart + 2));
  guard.flush();
  const output = write.mock.calls.map(([chunk]) => String(chunk)).join("");
  expect(output).not.toContain("framework");
  expect(output).not.toContain("�");
  expect(JSON.parse(output).count).toBe(1);
});
```

- [ ] **Step 6: Write and run the built-container RED before changing the Dockerfile**

Create this integration test first; `docker_container` registers cleanup in a
`request.addfinalizer` before returning the container ID:

```python
import json
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_built_web_container_stdout_is_complete_json_v1(
    docker_container: Callable[[str], str], tmp_path: Path
) -> None:
    image = "jhin-web-telemetry-test:local"
    subprocess.run(
        ["docker", "build", "-f", "apps/web/Dockerfile", "-t", image, "."],
        cwd=REPO_ROOT,
        check=True,
        timeout=600,
    )
    container_id = docker_container(image)
    wait_http(f"http://127.0.0.1:{published_port(container_id, 3000)}/", timeout=60)
    subprocess.run(["docker", "stop", "--time", "12", container_id], check=True, timeout=20)
    logs = subprocess.run(
        ["docker", "logs", container_id], capture_output=True, text=True, check=True
    )
    assert logs.stderr == ""
    records = [json.loads(line) for line in logs.stdout.splitlines()]
    assert records
    required = {
        "schema_version",
        "timestamp",
        "level",
        "service",
        "environment",
        "event",
        "logger",
    }
    allowed_events = {
        "web.started",
        "web.stopping",
        "web.rewrite_configured",
        "web.request_failed",
        "web.framework_output_suppressed",
        "log.event_rejected",
    }
    for record in records:
        assert required <= set(record)
        assert record["schema_version"] == 1
        assert record["service"] == "web"
        assert record["environment"] in {"dev", "test", "staging", "production"}
        assert record["level"] in {"debug", "info", "warning", "error"}
        assert record["event"] in allowed_events
        assert record["logger"] in {"jhin.web", "jhin.web.wrapper"}
        assert record["timestamp"].endswith("Z")
    inspect = json.loads(
        subprocess.run(
            ["docker", "inspect", container_id], capture_output=True, text=True, check=True
        ).stdout
    )[0]
    assert inspect["State"]["ExitCode"] == 0


def published_port(container_id: str, container_port: int) -> int:
    output = subprocess.run(
        ["docker", "port", container_id, f"{container_port}/tcp"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    return int(output.rsplit(":", 1)[1])


def wait_http(url: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError("web container did not become ready")


@pytest.fixture
def docker_container(request: pytest.FixtureRequest) -> Callable[[str], str]:
    created: list[str] = []

    def start(image: str) -> str:
        container_id = subprocess.run(
            ["docker", "run", "-d", "-P", image],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout.strip()
        created.append(container_id)
        return container_id

    def cleanup() -> None:
        for container_id in created:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )

    request.addfinalizer(cleanup)
    return start
```

Run before editing `apps/web/Dockerfile`:

```bash
uv run pytest -m integration tests/test_web_json_stdout.py -q
```

Expected: FAIL because the built runtime still starts Next directly, does not contain
`server-wrapper.cjs`, and its complete stdout is not guaranteed JSON-v1.

- [ ] **Step 7: Wire the wrapper into the runtime image and rerun GREEN**

Only after observing that RED, add to the runtime stage of `apps/web/Dockerfile`:

```dockerfile
COPY --from=build --chown=jhin:jhin /repo/apps/web/server-wrapper.cjs ./server-wrapper.cjs
CMD ["node", "server-wrapper.cjs"]
```

- [ ] **Step 8: Run all web gates**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web build
uv run pytest -m integration tests/test_web_json_stdout.py -q
```

Expected: PASS; server logger is absent from client bundles and output records parse as JSON.

- [ ] **Step 9: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 9


After applying the draft and corrected Tasks 1-8, replace Task 9 from its `Files` block through
its staging/commit block with this section. Task 9 is JSON-v1 stdout only. It consumes Task 1's
data contract by projection and introduces no Node/Python OTel runtime, metric, provider,
monitoring-network attachment, or second looser registry.

### 14.1 Use one immutable JSON contract for logger, wrapper, and Python parity

Create the only web data authority at
`apps/web/lib/server-log-contract.json`. It contains closed non-secret data only, and both
`server-logger.ts` and `server-wrapper.cjs` load this exact file and recursively deep-freeze
their in-process view.

Its exact base contract is:

- `schema_version: 1`, `service: "web"`, application logger `"jhin.web"`, and wrapper
  logger `"jhin.web.wrapper"`;
- environments `dev|test|staging|production`, aliases
  `development -> dev` and `prod -> production`, with trim/case normalization and unknown/
  empty/non-string fallback to `production`;
- levels `debug|info|warning|error`;
- context fields `request_id`, `correlation_id`, `workspace_id`, `task_id`, `run_id`,
  `trace_id`, and `span_id`, each using Task 1's exact full-match grammar and 128-character
  maximum;
- `MAX_LOG_DEPTH=8`, `MAX_LOG_ITEMS=64`, `MAX_LOG_STRING=2000`, maximum key length 128,
  minimum known-secret length 6, and exact sentinels
  `"[REDACTED]"`, `"[TRUNCATED]"`, and `"[UNSUPPORTED]"`;
- the exact Task 1 sensitive keys
  `authorization|cookie|password|secret|token|api_key|private_key|dsn|prompt|completion|sql|`
  `tool_input|tool_output|request_body|response_body|webhook_payload|secret_env` and exact
  suffixes
  `_authorization|_cookie|_password|_secret|_token|_api_key|_private_key|_dsn`; and
- wrapper limits: 64 KiB frame, 16 KiB input slice, suppression count 1,000,000, and suppression
  flush 1,000 ms.

The event/field projection is exact:

~~~text
web.started                     -> []
web.stopping                    -> [signal]
web.rewrite_configured          -> [http_route]
web.request_failed              -> [http_method, http_route, error_code]
web.framework_output_suppressed -> [stream, count]
log.event_rejected              -> []
~~~

Per-field enums and invalid behavior are exact:

~~~text
signal      = SIGINT|SIGTERM|other                       invalid -> other
http_method = GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|other invalid -> other
http_route  = /api/:path*|other                          invalid -> other
error_code  = internal_error                             invalid -> omit
stream      = stdout|stderr                              invalid -> omit
~~~

`count` is the separate finite integer contract in section 14.2. The Docker runtime copies the
JSON beside the wrapper explicitly; do not rely on Next output-file tracing for this raw PID 1
dependency.

The unmarked test in `tests/test_web_json_stdout.py` loads this JSON and compares schema, caps,
sensitive keys/suffixes, context fields, ID grammar, event-field projection, enums, aliases, and
fallback policy against Task 1's actual Python constants. Web event-specific enum sets are
nonempty subsets of the corresponding Python registry, and `other` is allowed only when the
Python field permits it. It must not merely compare the JSON file with itself.

Implement one exact-ID helper used by both modules. It proves the regex consumes the full input
and rejects empty/non-string input; length 129; URL/whitespace/control values; and CR, LF, NUL,
U+2028, or U+2029 anywhere, including a suffix. Test valid lengths 1 and 128 plus every rejection.
Test environment normalization for every closed value, both aliases, case/whitespace, empty,
unknown, and non-string values.

### 14.2 Redact before validation without inspecting hostile values

The logger pipeline order is binding:

1. Normalize a literal registered event or use `log.event_rejected`, without stringifying input.
2. Build the finite candidate-name set from only that event's permitted fields plus the seven
   contexts. Read only own **data** property descriptors for those names. Never invoke an accessor,
   iterator, proxy stringification, `toJSON`, `toString`, or equivalent. A hostile
   descriptor/proxy operation produces a safe rejected/base-only record, not an exception.
3. Apply one bounded structural pass, longest-known-secret-first replacement, then the second
   bounded structural pass. Both structural passes normalize sensitive key spelling and strip URL
   userinfo, query, and fragment before anything can be emitted.
4. Validate only the redacted value against its exact field kind. Omit invalid IDs and exact enums;
   map to `other` only for fields whose reviewed contract permits it.
5. Add fixed base fields after caller processing and serialize once so caller fields cannot
   override schema, timestamp, level, service, environment, event, or logger.

`count` accepts only a real safe integer in the inclusive `0..1_000_000` range. Boolean,
float, negative, overflow, coercible, accessor, and proxy values are omitted.

`serverError` spreads or inspects neither argument. It invokes the same private pipeline with
trusted forced `error_code="internal_error"`; the Error object is never dereferenced, including
message, stack, cause, or custom getters. Public `serverLog` and `serverError` contain all
diagnostic preparation and synchronous stdout-write failures and return `void`. Recovery never
echoes an exception or caller value.

The process-lifetime known-secret registry ignores values shorter than six and replaces longest
first, including overlaps and multiple occurrences. Unit tests reset module state between cases;
do not add a production clear API.

Cover a short ignored secret, overlaps, repeated values, secrets under every allowed context,
registered values that become invalid after replacement, camel/snake/punctuated/suffix sensitive
keys, URL userinfo/query/fragment, cyclic/deep/large structures, throwing descriptors/accessors/
proxies/stdout, and an Error with hostile message/stack/cause getters. Scan the complete line for
raw plus URL/base64-encoded canaries.

### 14.3 Canonicalize accepted child records and never relay input bytes

Replace the boolean wrapper validator with a parser/canonicalizer returning a newly constructed
safe record/canonical JSON string or `null`. Never emit the original child line.

For an accepted application record:

- require an ordinary non-array JSON object and exact base key/value types;
- require canonical UTC timestamp shape and exact
  `new Date(timestamp).toISOString() === timestamp`, not merely `Date.parse`;
- require schema 1, service `web`, logger `jhin.web`, and closed level/environment/event;
- reject every unknown key;
- validate each optional context/event field with the shared exact-ID/enum/count contract; and
- reconstruct fixed keys in deterministic order, then call `JSON.stringify` once on that new
  object.

The line framer may remove the one `\r` belonging to valid CRLF; otherwise leading/trailing
whitespace is noncanonical and rejected. Duplicate-key shadow values, Unicode-escaped shadow
values, noncanonical escapes/whitespace, invalid/noncanonical timestamps, arrays/primitives,
extra keys, wrong logger/service, newline-suffixed IDs, enum edges, and malformed JSON are rejected
or canonicalized without relaying their bytes. Accepted CRLF input is re-encoded once. Every
rejected nonempty/oversized line contributes only to the bounded suppression summary.

Wrapper-owned records use the same contract and encoder with fixed logger
`jhin.web.wrapper`. Call sites supply only closed signal/stream/error/count values. Its private
emitter uses the shared environment aliases and contains write/cleanup failure without printing a
raw Node error.

### 14.4 Finalize the child only after both pipes drain and release every owner

Use one idempotent wrapper finalizer:

- spawn with exactly ignored stdin and piped stdout/stderr and attach all data/end/error/close
  handlers before returning control;
- each line guard flushes idempotently at its stream `end`; child `close`, never `exit`, is
  terminal authority and performs only a final idempotent flush after both pipes close;
- synchronous spawn throw and asynchronous child `error` emit only the closed
  `web.request_failed` wrapper record, set nonzero exit, clear owned timers, and remove every
  installed signal listener without serializing the error;
- a stdout/stderr stream `error` is bounded diagnostic failure: suppress the raw error, stop that
  stream safely, do not kill/duplicate the child, and preserve the eventual child result;
- first SIGINT/SIGTERM emits one `web.stopping`, forwards that exact signal once, and creates one
  unref'd 10-second kill deadline; repeat signals/stops/close are idempotent;
- requested graceful signal or code 0 after stop exits zero; SIGKILL/deadline, spawn failure, or
  other abnormal child termination exits nonzero; and
- every startup/error/close path clears kill/suppression timers, removes only listeners installed
  by this wrapper, clears decoder buffers, and leaves no live handle.

Diagnostic emit/finalization/cleanup failure cannot replace child authority or print free text.
The finite line guards retain split UTF-8 correctness, huge-line dropping, saturated capped count,
and aggregation behavior.

Socket-free unit tests use `FakeChild`, explicitly end both streams, emit `close`, compare
process listener counts before/after, and use fake timers. Cover normal code/signal stop,
unexpected termination, async child error, synchronous spawn throw, stream error, ignored graceful
deadline, double signal/close, final partial line before close, late data rejection, huge/saturated
input, split UTF-8, and zero owned timer/listener/buffer state.

### 14.5 Wire and test the installed Next 16.3.1 surface exactly

Start `apps/web/instrumentation.ts` with:

~~~typescript
import type { Instrumentation } from "next";

import { serverError, serverLog } from "@/lib/server-logger";
~~~

`register()` is Node-only and emits exactly `web.started` plus
`web.rewrite_configured` with collapsed route `/api/:path*`.
`normalizeNextRoute` consumes only typed `context.routePath` and returns exactly
`/api/:path*|other`. `onRequestError` reads only `request.method` and
`context.routePath`; it never reads concrete path, URL/query, headers, cookies, body, React
props, Error, cause, message, or stack.

Preserve standalone output and the same-origin rewrite, and make Next logging exact:

~~~typescript
logging: {
  fetches: { fullUrl: false },
  incomingRequests: false,
},
~~~

This disables framework access lines at their source. Do not add a second access logger; the
wrapper remains the fail-closed guard for unexpected framework output.

`apps/web/tests/instrumentation.test.ts` covers node/non-node registration, exact route
projection, every method/route fallback, exact `onRequestError` call, and proxy request/error
objects whose forbidden getters throw. It imports the real Next config and asserts standalone
output, disabled incoming requests, bounded fetch logging, and unchanged rewrite source/
destination without logging the internal URL.

### 14.6 Make all Vitest RED tests Node-only, resolvable, and isolated

Create `apps/web/tests/server-only-stub.ts` with only an empty export. In
`apps/web/vitest.config.ts`, alias the exact module ID `server-only` to that test-only file.
Do not add a package dependency. Put `// @vitest-environment node` at the first line of
`server-logger.test.ts`, `instrumentation.test.ts`, and `server-wrapper.test.ts`; the Next
build continues using its pinned compiler alias and real server-only boundary.

Until each production file exists, dynamically import it inside a named test/helper. Define every
capture, environment snapshot/restore, stdout spy, module reset, fake child/timer, and complete
record helper before RED. In `afterEach`/`finally`, restore `APP_ENV`, `NODE_ENV`,
`NEXT_RUNTIME`, `process.exitCode`, stdout spies, timers, listeners, and module state, including
the known-secret singleton.

Do not use TypeScript `require(...)`. Load the deliberate CommonJS wrapper through a locally
named function returned by `createRequire(import.meta.url)` with a narrow local interface.
`server-wrapper.cjs` gets one documented file-level ESLint exemption for its deliberate
CommonJS built-in/JSON loads. Do not relax repository-wide lint or add an ESLint config.

### 14.7 Make the Docker proof unique, localhost-only, and self-cleaning

`tests/test_web_json_stdout.py` contains:

1. the unmarked Task 1 cross-language contract test from section 14.1, which runs in the ordinary
   root suite; and
2. exactly one individually `@pytest.mark.integration` built-image test.

The Docker test creates a UUID-suffixed image tag and registers best-effort image removal before
build. Once a validated container ID exists, immediately register container removal so fixture
teardown removes container before image after success, failure, timeout, or assertion error.
Publish the container only on `127.0.0.1` with a dynamically selected host port; never use
unrestricted `-P`. Pass `APP_ENV=test` explicitly.

After readiness, send one request containing unique URL-query, Authorization, and cookie canaries.
Stop via Docker's bounded SIGTERM path, require exact zero exit, and capture stdout/stderr. Require:

- stderr is empty and every nonempty stdout line is exactly one JSON object;
- every record satisfies the shared allowed-key projection, canonical UTC timestamp, fixed
  service, closed environment/level/event/logger, exact IDs/enums/count, and canonical
  re-encoding with no duplicate semantic key;
- `web.started`, `web.rewrite_configured`, and exactly one graceful `web.stopping` exist;
- no framework/application free-text line exists; and
- no raw canary or URL/base64 form appears in stdout, stderr, the inspect output selected for the
  assertion, or parsed records.

The pre-Dockerfile integration RED is valid only after all TypeScript/unit/parity groups are GREEN.
It must fail because the old image starts `apps/web/server.js` directly. Then copy both
`server-wrapper.cjs` and `lib/server-log-contract.json` with `--chown=jhin:jhin` into the
runtime stage and make `node server-wrapper.cjs` the sole runtime command. Rerun the same isolated
test GREEN.

### 14.8 Keep dependency, bundle, and cardinality ownership exact

Task 9 changes no package or lock:

- `next@16.3.1` is already direct and supplies the instrumentation type plus production
  `server-only` compiler boundary;
- Vitest, TypeScript, and `@types/node` are already direct development dependencies;
- the wrapper imports only Node built-ins and the repository-owned JSON contract; and
- the stub resolves `server-only` only under Vitest.

`apps/web/package.json` and `pnpm-lock.yaml` are absent from Task 9. A real dependency
discovery requires a reviewed File Map/Files/manifest amendment before either changes. Compose,
CI, and Task 11's harness are also later-owner paths.

Task 9 creates no metric, trace label, OTel provider/exporter, or monitoring-network connection.
Log cardinality is bounded by six events, their closed fields/enums, seven 128-character contexts,
finite count, finite strings/items/depth, and capped suppression aggregation. No URL/host,
provider/model/tool/connector name, external ID, request path, or arbitrary error becomes an event
name or field key.

Keep `import "server-only"` as the client-bundle guard. The production Next build is mandatory;
any Client Component import of the logger must fail the build rather than shipping contract/
redactor/known-secret state to the browser.

### 14.9 Run exact RED, Docker transition, and final GREEN gates

After corrected Tasks 1-8 exist, first create the complete test-only Vitest alias/stub, all three
server test files, and Python helpers. Do not create the production logger, instrumentation,
wrapper, or JSON contract yet. Run:

~~~bash
pnpm --filter jhin-web exec vitest run tests/server-logger.test.ts
pnpm --filter jhin-web exec vitest run tests/instrumentation.test.ts
pnpm --filter jhin-web exec vitest run tests/server-wrapper.test.ts
uv run pytest -m 'not integration' tests/test_web_json_stdout.py -q
~~~

Expected RED names the absent production file from inside a named test. Undefined helpers,
top-level collection import, unresolved test-only `server-only`, jsdom accident, singleton/env
leak, or lint/type error is invalid RED.

Implement shared contract/logger first, instrumentation/config second, wrapper third. Run each
focused group and Python parity GREEN. Before the Dockerfile change run:

~~~bash
uv run pytest -m integration tests/test_web_json_stdout.py -q
~~~

Expected RED: the old image starts `apps/web/server.js` directly and fails the complete
stdout/privacy/lifecycle/command assertions. Apply only the runtime-stage copy/CMD change and
rerun the same integration test GREEN.

Final Task 9 gates are:

~~~bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web build
uv run pytest -m integration tests/test_web_json_stdout.py -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
~~~

All commands pass. The root pytest run executes the unmarked parity test and
collects/deselects only the Docker case. No focused unit/parity gate uses Docker, network, browser,
or a live service. The explicit integration test owns and removes only its unique image/container.

### 14.10 Preserve Task 10-12 handoffs exactly

- **Tasks 1-8:** keep every Python registry/runtime/metric/span/lifecycle authority unchanged.
  Task 9 consumes only the JSON-v1 projection and adds no cross-service registry or product
  behavior.
- **Task 10:** because it owns `compose.yaml`, `compose.dev.yaml`, observability Compose guards,
  and `.env.example`, it adds exact web `APP_ENV`: production default `production`, dev
  default `dev`, and rendered explicit `test` override in both socket modes. Preserve Task 9's
  wrapper CMD/copies, keep web off `monitoring`/OTLP, and add only bounded Docker JSON-file
  logging.
- **Task 11:** keep `docker compose logs --no-log-prefix` completely unfiltered. Validate web
  lines with the Task 9 contract, closed allowed keys, canonical timestamp, and exact logger.
  Route at least one URL/header/cookie-canary request through web, scan raw and encoded forms, and
  reject every framework/application free-text line. The isolated Task 9 container test does not
  replace rootful/rootless observed-stack evidence, and Task 11 may not bypass/reimplement wrapper.
- **Task 12:** retain ordinary Python parity, all web test/lint/typecheck/build gates, both
  clean-stack live gates, and complete canary/evidence scan. Add no Task 9 dependency version to
  evidence. Final commit/path sequence includes this exact 12-path commit; Task 12 stages only its
  own three evidence paths.

### 14.11 Make Task 9's 12 paths and committed tree exact

Replace Task 9 `Files`, matching global File Map entries, and staging with:

~~~bash
set -euo pipefail
task9_paths=(
  apps/web/Dockerfile
  apps/web/instrumentation.ts
  apps/web/lib/server-log-contract.json
  apps/web/lib/server-logger.ts
  apps/web/next.config.ts
  apps/web/server-wrapper.cjs
  apps/web/tests/instrumentation.test.ts
  apps/web/tests/server-logger.test.ts
  apps/web/tests/server-only-stub.ts
  apps/web/tests/server-wrapper.test.ts
  apps/web/vitest.config.ts
  tests/test_web_json_stdout.py
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task9_paths[@]}"
git diff --check -- "${task9_paths[@]}"
git add -- "${task9_paths[@]}"
expected_index="$(printf '%s\n' "${task9_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task9_paths[@]}"
git commit --only "${task9_paths[@]}" \
  -m "feat(web): emit safe versioned server logs"
test "$(git show -s --format=%s HEAD)" = \
  "feat(web): emit safe versioned server logs"
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)"
test "$actual_commit_paths" = "$expected_index"
test -z "$(git diff --cached --name-only)"
~~~

The Task 9 `Files`, File Map, and array are exact mirrors. No package/lock, Compose, CI, or Task
11 harness path is authorized. Any discovered affected path requires a reviewed amendment before
it is touched. The global index-only exception remains sole: a pre-staged/unexpected/missing path,
path outside `Files`, commit-tree mismatch, or non-empty post-commit index fails closed.

#### Consolidated validation context


Section 20 is the sole combined validation after Tasks 10-12 corrections. Replace the draft's
earlier validation block with section 20 rather than retaining both. Its helpers use explicit
`return 1` / `exit 1`, exact subject equality, all twelve manifests, the accepted predecessor
tip handoff, and the protected final-head CI marker. The five exact acceptance values are filled from the mutually verified successful run;
section 20 now requires those literal values and fails closed on any drift.

#### Resolved implementation decisions


Only these decisions remain legitimate at execution time:

1. **Checkout log availability:** accept the completed run only if both logs contain the exact
   full-SHA proof described above; otherwise amend CI and rerun.
2. **Additional affected test discovered by implementation:** amend File Map/Files/staging first,
   review the plan delta, then implement it. Do not silently expand the corresponding task commit.
3. **Platform socket ownership command:** retain the two-branch GNU/BSD `stat_numeric` helper; do
   not choose one platform and make the other harness non-executable.
4. **Late span-attribute mechanism:** Task 7 may use either one best-effort setter backed by Task
   2's closed per-key registry or construct the complete normalized final mapping before span end.
   The same normalization, fail-open behavior, latency clamp, and privacy assertions apply to
   either implementation.
5. **Wrapper canonicalizer return form:** Task 9 may return either the newly constructed safe
   record or its one canonical JSON encoding from the private parser. In either form the wrapper
   must reconstruct fixed keys and stringify the new object exactly once; it may never relay the
   child input bytes.

The dependency, normalizer, compatibility-helper, environment, AST-exemption, rootless-event,
`MetricName` authority, bootstrap metrics installation, normalized observable identity,
pure-ASGI lifecycle, route collapse, test seam, SQL parser/listener, echo removal, Phase 2 fixture,
trace-only NATS headers, subject authority, settlement lifetime, safe context binding, lag
supervision, downstream tracer handoffs, and Task 3/4/5 test/staging choices are fixed by this
addendum and are not left to the implementer. Task 6 likewise leaves no architecture decision
open: the serialized reserved-carrier ceiling is 1,024 bytes; signal/update propagation uses
validated SDK 1.31 `link_context`; private-hook use requires exact Temporal 1.31.0; the semantic
wiring/import audits, six service ownership models, public signatures, provider/privacy contract,
dependencies, 39-path manifest, and gates are fixed. Internal helper decomposition is an
implementation choice only insofar as it preserves every observable contract and test above.
Task 7 likewise fixes the complete model protocol, explicit runtime identity, semantic factory
audit, diagnostic containment, committed-usage/finalization guards, catalog/durable-tool authority,
trigger reconciliation and closed errors, dependencies, four-stage RED, broad GREEN, and exact
32-path manifest/commit. The internal late-attribute mechanism in item 4 and ordinary private
helper decomposition are the only Task 7 implementation choices; neither may change an observable
contract. Task 8 fixes stateless request-bound tracer injection, the central connector-specific
operation registry, fail-open HTTP/database/sampler/runner/job boundaries, exact HTTP and runner
caps, health series/lifecycle, client-server-job parent edges, terminal once-recording,
dependencies, Task 11/12 handoffs, RED/GREEN gates, and the exact 44-path manifest/commit. It adds
no new open architecture choice; item 4's already-bounded late-attribute mechanism and internal
helper decomposition remain the only implementation freedom. Task 9 fixes the immutable JSON
authority, Task 1 parity, ordered fail-open redaction, exact enum/ID/environment policy, canonical
child re-encoding, drained child lifecycle, Next/Vitest isolation, unique localhost-only Docker
proof, dependency/bundle/cardinality decisions, Task 10-12 handoffs, gates, and exact 12-path
manifest/commit. Item 5 is its sole explicit representation choice and cannot change output.

### Task 10: Add the Optional Collector/Prometheus/Tempo/Grafana Profile

**Files:**
- Modify: `.env.example`
- Modify: `Makefile`
- Modify: `compose.dev.yaml`
- Modify: `compose.rootless.yaml`
- Modify: `compose.yaml`
- Modify: `docker/monitoring.Dockerfile`
- Modify: `ops/observability/collector.yaml`
- Modify: `ops/observability/grafana/dashboards/jhin-overview.json`
- Modify: `ops/observability/grafana/provisioning/dashboards/jhin.yaml`
- Modify: `ops/observability/grafana/provisioning/datasources/jhin.yaml`
- Modify: `ops/observability/prometheus.yaml`
- Modify: `ops/observability/tempo.yaml`
- Modify: `scripts/assert_phase10_observability_compose.py`
- Modify: `scripts/assert_phase10_tool_worker_compose.py`
- Modify: `scripts/build_phase10_dashboard.py`
- Modify: `tests/integration/phase10_upgrade_harness.py`
- Modify: `tests/test_phase10_observability_compose.py`
- Modify: `tests/test_phase10_tool_worker_compose.py`

**Interfaces:**
- Consumes the accepted Task 9 handoff and produces the exact Task 10 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing rendered-Compose topology and retention tests**

```python
ROOT = Path(__file__).resolve().parents[1]


SocketMode = Literal["rootful", "rootless"]


def render_compose(
    *, dev: bool = False, observability: bool = True, socket_mode: SocketMode
) -> dict[str, Any]:
    argv = ["docker", "compose", "-f", "compose.yaml"]
    if dev:
        argv.extend(("-f", "compose.dev.yaml"))
    argv.extend(("-f", f"compose.{socket_mode}.yaml"))
    if observability:
        argv.extend(("--profile", "observability"))
    argv.extend(("config", "--format", "json"))
    environment = os.environ.copy()
    if socket_mode == "rootful":
        environment["SANDBOX_DOCKER_GID"] = "10001"
    else:
        environment.pop("SANDBOX_DOCKER_GID", None)
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    document = json.loads(completed.stdout)
    assert isinstance(document, dict)
    return document


@pytest.fixture(params=("rootful", "rootless"))
def rendered(request: pytest.FixtureRequest) -> dict[str, Any]:
    return render_compose(socket_mode=cast(SocketMode, request.param))


def test_profile_is_optional_internal_and_credential_free(rendered: dict[str, Any]) -> None:
    monitoring = {"otel-collector", "prometheus", "tempo", "grafana"}
    assert monitoring <= rendered["services"].keys()
    for name in monitoring:
        service = rendered["services"][name]
        assert service["profiles"] == ["observability"]
        assert set(service["networks"]) == {"monitoring"}
        assert "ports" not in service
        assert service["healthcheck"]["test"][0:2] == ["CMD", "/bin/busybox"]
        serialized = json.dumps(service).lower()
        for forbidden in (
            "database_url",
            "nats_url",
            "temporal_address",
            "master_key",
            "docker.sock",
            "sandbox_runner_token",
            "authorization",
            "api_key",
        ):
            assert forbidden not in serialized
    assert rendered["networks"]["monitoring"]["internal"] is True


def test_monitoring_retention_is_exact() -> None:
    rendered = render_compose(socket_mode="rootless")
    assert "--storage.tsdb.retention.time=15d" in rendered["services"]["prometheus"]["command"]
    tempo = yaml.safe_load((ROOT / "ops/observability/tempo.yaml").read_text())
    assert tempo["compactor"]["compaction"]["block_retention"] == "72h"


@pytest.mark.parametrize(
    "service",
    [
        "web",
        "api",
        "workflow-worker",
        "agent-worker",
        "tool-worker",
        "event-worker",
        "sandbox-runner",
    ],
)
def test_every_application_service_has_bounded_json_file_logs(
    rendered: dict[str, Any], service: str
) -> None:
    assert rendered["services"][service]["logging"] == {
        "driver": "json-file",
        "options": {"max-file": "5", "max-size": "20m"},
    }
```

Also assert product services with OTLP join `monitoring`, web does not need OTLP access, Collector is the only OTLP receiver, Prometheus scrapes only Collector, Tempo receives only Collector, Grafana reads only Prometheus/Tempo, monitoring volumes are distinct from product volumes, base Compose still renders with an empty OTLP endpoint, and dev host bindings are exactly `127.0.0.1`.

True up the predecessor's tool-worker Compose test and assertion script rather than weakening their
socket/authority checks. In both rootful and rootless renders assert agent networks are exactly
`{"control", "data", "monitoring"}`, tool-worker networks are exactly
`{"control", "data", "runner", "monitoring"}`, and sandbox-runner networks are exactly
`{"runner", "monitoring"}`. Preserve every exact queue, credential, Docker-socket, `depends_on`, and
UID/GID assertion; monitoring services themselves join only `monitoring` and have no socket mount.
Both scripts require
`--mode {rootful,rootless}` and build their Compose vector with exactly that overlay. Run these
predecessor tests in this task's initial RED so the old exact-network assertion fails before
`compose.yaml` is changed.

- [ ] **Step 2: Run Compose RED**

```bash
uv run pytest tests/test_phase10_observability_compose.py -q
uv run pytest tests/test_phase10_tool_worker_compose.py -q
```

Expected: FAIL because no monitoring profile/configuration exists.

- [ ] **Step 3: Create pinned monitoring images with a static health probe**

Use one Dockerfile so even distroless upstream images have a meaningful in-container readiness check:

```dockerfile
ARG BASE_IMAGE
FROM busybox:1.37.0-uclibc AS probe

FROM ${BASE_IMAGE}
COPY --from=probe /bin/busybox /bin/busybox
```

Compose passes exact `BASE_IMAGE` values:

```text
otel/opentelemetry-collector-contrib:0.135.0
prom/prometheus:v3.5.0
grafana/tempo:2.8.2
grafana/grafana:12.1.0
```

No `latest` tag or floating major tag is allowed. The probe uses BusyBox `wget` only against each process's loopback ready endpoint.

- [ ] **Step 4: Add complete Collector, Prometheus, and Tempo configs**

Create `collector.yaml`:

```yaml
extensions:
  health_check:
    endpoint: 0.0.0.0:13133

receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 256
    spike_limit_mib: 64
  batch:
    send_batch_size: 512
    send_batch_max_size: 1024
    timeout: 5s

exporters:
  prometheus:
    endpoint: 0.0.0.0:9464
    enable_open_metrics: true
    translation_strategy: UnderscoreEscapingWithoutSuffixes
    include_scope_info: false
    resource_to_telemetry_conversion:
      enabled: false
  otlp/tempo:
    endpoint: tempo:4317
    tls:
      insecure: true

service:
  extensions: [health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [prometheus]
```

Create `prometheus.yaml`:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
scrape_configs:
  - job_name: jhin-otel-collector
    static_configs:
      - targets: [otel-collector:9464]
```

Create `tempo.yaml`:

```yaml
server:
  http_listen_port: 3200
distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318
ingester:
  max_block_duration: 5m
compactor:
  compaction:
    block_retention: 72h
storage:
  trace:
    backend: local
    wal:
      path: /var/tempo/wal
    local:
      path: /var/tempo/blocks
```

Prometheus command includes `--config.file=/etc/prometheus/prometheus.yml`, `--storage.tsdb.path=/prometheus`, `--storage.tsdb.retention.time=15d`, and `--web.enable-lifecycle`. Collector exposes no log pipeline.

Extend the rendered config test with:

```python
collector = yaml.safe_load((ROOT / "ops/observability/collector.yaml").read_text())
prometheus_exporter = collector["exporters"]["prometheus"]
assert prometheus_exporter["translation_strategy"] == "UnderscoreEscapingWithoutSuffixes"
assert "logs" not in collector["service"]["pipelines"]
```

Task 11 performs the live `/metrics` exact-name assertion after emitting all instruments.

- [ ] **Step 5: Provision Grafana data sources and a generated deterministic dashboard**

Create data sources:

```yaml
apiVersion: 1
deleteDatasources:
  - {name: Prometheus, orgId: 1}
  - {name: Tempo, orgId: 1}
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
    jsonData:
      exemplarTraceIdDestinations:
        - {name: trace_id, datasourceUid: tempo}
  - name: Tempo
    uid: tempo
    type: tempo
    access: proxy
    url: http://tempo:3200
    editable: false
```

Create the dashboard provider:

```yaml
apiVersion: 1
providers:
  - name: Jhin
    orgId: 1
    folder: Jhin
    type: file
    disableDeletion: true
    editable: false
    options:
      path: /var/lib/grafana/dashboards
```

`build_phase10_dashboard.py` deterministically writes dashboard UID `jhin-phase10-overview`, schema version `41`, refresh `30s`, time range `now-6h` to `now`, and one panel for every tuple below:

```python
PANELS = (
    ("Agent runs", "sum by (outcome) (rate(agent_runs_total[5m]))", "ops"),
    (
        "Agent run p95",
        "histogram_quantile(0.95, sum by (le, outcome) (rate(agent_run_duration_seconds_bucket[5m])))",
        "s",
    ),
    ("Agent failures", "sum by (failure_class) (rate(agent_run_failures_total[5m]))", "ops"),
    ("Model attempts", "sum by (provider_type, outcome) (rate(model_requests_total[5m]))", "ops"),
    ("Model tokens", "sum by (provider_type, direction) (rate(model_tokens_total[5m]))", "ops"),
    (
        "Estimated model cost",
        "sum by (provider_type) (increase(model_cost_estimate[1h]))",
        "currencyUSD",
    ),
    ("Tool calls", "sum by (tool_family, risk, outcome) (rate(tool_calls_total[5m]))", "ops"),
    (
        "Tool failures",
        "sum by (tool_family, failure_class) (rate(tool_call_failures_total[5m]))",
        "ops",
    ),
    (
        "Trigger invocations",
        "sum by (connector_type, outcome) (rate(trigger_invocations_total[5m]))",
        "ops",
    ),
    (
        "Trigger failures",
        "sum by (connector_type, failure_class) (rate(trigger_failures_total[5m]))",
        "ops",
    ),
    ("Sandbox jobs", "sum by (outcome, network_policy) (rate(sandbox_jobs_total[5m]))", "ops"),
    (
        "Sandbox job p95",
        "histogram_quantile(0.95, sum by (le, outcome) (rate(sandbox_job_duration_seconds_bucket[5m])))",
        "s",
    ),
    ("NATS consumer lag", "max by (stream, consumer) (nats_consumer_lag)", "short"),
    (
        "Temporal activity failures",
        "sum by (task_queue, activity, failure_class) (rate(temporal_activity_failures[5m]))",
        "ops",
    ),
    ("Connector health", "min by (connector_type) (connector_health)", "short"),
    ("Connector counts", "sum by (connector_type, outcome) (connector_connections)", "short"),
)


def panel(index: int, title: str, expression: str, unit: str) -> dict[str, object]:
    return {
        "id": index + 1,
        "type": "timeseries",
        "title": title,
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "gridPos": {
            "h": 8,
            "w": 12,
            "x": (index % 2) * 12,
            "y": (index // 2) * 8,
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line",
                    "lineInterpolation": "linear",
                    "lineWidth": 1,
                    "fillOpacity": 10,
                    "showPoints": "never",
                    "spanNulls": False,
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [
            {
                "refId": "A",
                "expr": expression,
                "legendFormat": "{{__name__}} {{outcome}} {{failure_class}} {{connector_type}}",
                "range": True,
            }
        ],
    }


def dashboard_document() -> dict[str, object]:
    return {
        "id": None,
        "uid": "jhin-phase10-overview",
        "title": "Jhin Phase 10 Overview",
        "schemaVersion": 41,
        "version": 1,
        "editable": False,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "tags": ["jhin", "phase10", "observability"],
        "timezone": "browser",
        "templating": {"list": []},
        "annotations": {"list": []},
        "panels": [panel(index, *definition) for index, definition in enumerate(PANELS)],
    }


def render_dashboard() -> str:
    return json.dumps(dashboard_document(), indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    destination = ROOT / "ops/observability/grafana/dashboards/jhin-overview.json"
    rendered = render_dashboard()
    if args.check:
        return 0 if destination.exists() and destination.read_text() == rendered else 1
    destination.write_text(rendered)
    return 0
```

Run `uv run python scripts/build_phase10_dashboard.py`, then `--check`; commit the script and byte-identical generated JSON.

- [ ] **Step 6: Wire the optional profile and application log caps**

Add an `x-application-logging` anchor:

```yaml
x-application-logging: &application-logging
  driver: json-file
  options:
    max-size: 20m
    max-file: "5"
```

Apply it to web, API, all four workers including tool-worker, event-worker, and sandbox-runner. Add OTLP settings and the `monitoring` network to Python product services, with an empty default:

```yaml
OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_EXPORTER_OTLP_ENDPOINT:-}
OTEL_EXPORTER_OTLP_INSECURE: ${OTEL_EXPORTER_OTLP_INSECURE:-false}
OTEL_TRACES_SAMPLER: ${OTEL_TRACES_SAMPLER:-parentbased_traceidratio}
OTEL_TRACES_SAMPLER_ARG: ${OTEL_TRACES_SAMPLER_ARG:-0.10}
```

Add these complete base-profile services; none has a `ports`, `secrets`, or product-network entry:

```yaml
  otel-collector:
    profiles: [observability]
    build:
      context: .
      dockerfile: docker/monitoring.Dockerfile
      args: {BASE_IMAGE: "otel/opentelemetry-collector-contrib:0.135.0"}
    command: ["--config=/etc/otelcol-contrib/config.yaml"]
    volumes:
      - ./ops/observability/collector.yaml:/etc/otelcol-contrib/config.yaml:ro
    expose: ["4317", "4318", "9464", "13133"]
    networks: [monitoring]
    healthcheck:
      test: ["CMD", "/bin/busybox", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:13133/"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 15s

  prometheus:
    profiles: [observability]
    build:
      context: .
      dockerfile: docker/monitoring.Dockerfile
      args: {BASE_IMAGE: "prom/prometheus:v3.5.0"}
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=15d
      - --web.enable-lifecycle
    volumes:
      - ./ops/observability/prometheus.yaml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    expose: ["9090"]
    networks: [monitoring]
    healthcheck:
      test: ["CMD", "/bin/busybox", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:9090/-/ready"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 15s

  tempo:
    profiles: [observability]
    build:
      context: .
      dockerfile: docker/monitoring.Dockerfile
      args: {BASE_IMAGE: "grafana/tempo:2.8.2"}
    command: ["-config.file=/etc/tempo.yaml"]
    volumes:
      - ./ops/observability/tempo.yaml:/etc/tempo.yaml:ro
      - tempo_data:/var/tempo
    expose: ["3200", "4317", "4318"]
    networks: [monitoring]
    healthcheck:
      test: ["CMD", "/bin/busybox", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:3200/ready"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 15s

  grafana:
    profiles: [observability]
    build:
      context: .
      dockerfile: docker/monitoring.Dockerfile
      args: {BASE_IMAGE: "grafana/grafana:12.1.0"}
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "false"
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - ./ops/observability/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./ops/observability/grafana/dashboards:/var/lib/grafana/dashboards:ro
      - grafana_data:/var/lib/grafana
    expose: ["3000"]
    networks: [monitoring]
    healthcheck:
      test: ["CMD", "/bin/busybox", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:3000/api/health"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 15s

networks:
  monitoring: {internal: true}
volumes:
  prometheus_data: {}
  tempo_data: {}
  grafana_data: {}
```

In `compose.dev.yaml` bind Grafana `127.0.0.1:${GRAFANA_DEV_PORT:-3300}:3000`, Prometheus `127.0.0.1:${PROMETHEUS_DEV_PORT:-9090}:9090`, and Tempo `127.0.0.1:${TEMPO_DEV_PORT:-3200}:3200`; do not bind Collector. Grafana anonymous access remains disabled and its base profile has no public entrypoint.

Add Make targets whose environment explicitly enables export only when requested:

```make
PHASE10_SOCKET_MODE ?= rootless
ifeq ($(PHASE10_SOCKET_MODE),rootful)
  ifeq ($(strip $(SANDBOX_DOCKER_GID)),)
    $(error SANDBOX_DOCKER_GID is required for PHASE10_SOCKET_MODE=rootful)
  endif
  PHASE10_SOCKET_OVERLAY := compose.rootful.yaml
else ifeq ($(PHASE10_SOCKET_MODE),rootless)
  ifneq ($(origin SANDBOX_DOCKER_GID),undefined)
    $(error SANDBOX_DOCKER_GID must be unset for PHASE10_SOCKET_MODE=rootless)
  endif
  PHASE10_SOCKET_OVERLAY := compose.rootless.yaml
else
  $(error PHASE10_SOCKET_MODE must be rootful or rootless)
endif

COMPOSE_BASE := docker compose -f compose.yaml -f $(PHASE10_SOCKET_OVERLAY)
COMPOSE_DEV := docker compose -f compose.yaml -f compose.dev.yaml -f $(PHASE10_SOCKET_OVERLAY)

observability-up: master-key sandbox-image
	OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
	OTEL_EXPORTER_OTLP_INSECURE=true \
	$(COMPOSE_DEV) --profile observability up -d --build

observability-down:
	$(COMPOSE_DEV) --profile observability down --remove-orphans
```

These definitions replace the predecessor's shorter `COMPOSE_DEV`; every Make target that invokes
Compose, including legacy `up`, `down`, `test-integration`, `sandbox-image`, telemetry targets, and
cleanup, uses `COMPOSE_BASE` or `COMPOSE_DEV`. No recipe spells its own file vector. Local rootless
usage runs with `SANDBOX_DOCKER_GID` absent; rootful usage supplies the socket's real nonzero numeric
group and `PHASE10_SOCKET_MODE=rootful` to the whole `make` process.

Document the endpoint/TLS/sampler and dev ports in `.env.example`; do not add credentials.

- [ ] **Step 7: Implement and run the rendered-profile assertion**

`assert_phase10_observability_compose.py --mode {rootful,rootless}` loads `docker compose -f
compose.yaml -f compose.<mode>.yaml config --format json`, passing the verified rootful socket's
actual nonzero GID for rootful and the verified socket path for rootless, then performs the same
topology/retention/logging assertions as the unit test and scans the rendered monitoring services
for forbidden credential keys. It must not start containers.

Run:

```bash
rootful_socket="${PHASE10_ROOTFUL_DOCKER_SOCKET:-/var/run/docker.sock}"
rootless_socket="${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}"
test -S "$rootful_socket"
socket_gid="$(stat -c %g "$rootful_socket")"
test "$socket_gid" -gt 0
uv run python scripts/build_phase10_dashboard.py --check
uv run pytest tests/test_phase10_observability_compose.py -q
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  uv run python scripts/assert_phase10_observability_compose.py --mode rootless
SANDBOX_DOCKER_SOCKET_HOST="$rootful_socket" SANDBOX_DOCKER_GID="$socket_gid" \
  uv run python scripts/assert_phase10_observability_compose.py --mode rootful
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootless
SANDBOX_DOCKER_SOCKET_HOST="$rootful_socket" SANDBOX_DOCKER_GID="$socket_gid" \
  uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootful
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  docker compose -f compose.yaml -f compose.rootless.yaml config --quiet
SANDBOX_DOCKER_SOCKET_HOST="$rootful_socket" SANDBOX_DOCKER_GID="$socket_gid" \
  docker compose -f compose.yaml -f compose.dev.yaml \
  -f compose.rootful.yaml --profile observability config --quiet
```

Expected: PASS; the optional profile is valid and internal, with exact retention and log caps.

- [ ] **Step 8: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 10


After applying the draft and corrected Tasks 1-9, replace Task 10 from its `Files` block through
its staging/commit block with this section. Task 10 adds an optional local diagnostics plane. It
does not create a second Docker lifecycle, give products a new product-to-product path, initialize
a second telemetry runtime, or claim live readiness from rendered YAML.

### 17.1 Preserve product isolation with one backend bridge and six ingress bridges

An internal Docker bridge is still full-mesh among its members. Therefore no product joins one
shared `monitoring` bridge. Define exactly these seven project-scoped networks, each with the exact
mapping `driver: bridge`, `external: false`, and `internal: true`:

~~~text
monitoring             otel-collector, prometheus, tempo, grafana
telemetry-api          api, otel-collector
telemetry-workflow     workflow-worker, otel-collector
telemetry-agent        agent-worker, otel-collector
telemetry-tool         tool-worker, otel-collector
telemetry-event        event-worker, otel-collector
telemetry-sandbox      sandbox-runner, otel-collector
~~~

The complete product network sets after Task 10 are exact:

~~~text
web                 edge
api                 edge, control, data, telemetry-api
workflow-worker     control, telemetry-workflow
agent-worker        control, data, telemetry-agent
tool-worker         control, data, runner, telemetry-tool
event-worker        control, data, telemetry-event
sandbox-runner      runner, telemetry-sandbox                 (rootful)
sandbox-runner      runner, engine, telemetry-sandbox         (rootless)
rootless adapter    engine                                    (rootless only)
otel-collector      monitoring plus all six telemetry-* ingress bridges
prometheus          monitoring
tempo               monitoring
grafana             monitoring
~~~

No product service joins backend `monitoring`; no two products share a telemetry ingress; web and
the rootless adapter join none; and the rootless sandbox runner retains `engine`. Static and live
tests mutation-reject every extra/missing attachment, any product on `monitoring`, any backend on
a product network, web/adapter telemetry membership, a shared product ingress, Collector missing
or adding an ingress, and rootless loss of `engine`.

Revise every contradictory global/interface sentence in the telemetry plan to this model. The
monitoring bridge is backend-only; the six producer bridges are ingress-only.

### 17.2 Extend the sole leased Compose authority

Delete the proposed `PHASE10_SOCKET_MODE`, `COMPOSE_BASE`, and `COMPOSE_DEV` Make/raw-Compose
authority. Preserve `PHASE10_MODE` and extend
`tests/integration/phase10_upgrade_harness.py`:

- `ComposeAuthority.create(..., observability: bool = False)` and
  `select_live_authority(..., observability: bool = False)` install one immutable selection in
  the already-sanitized authority environment.
- Base selection installs an empty endpoint and false insecure flag. Observed selection installs
  exactly `http://otel-collector:4317` and `true`, retaining Task 2's reviewed sampler/queue/
  batch/timeout/interval defaults.
- `observability_enabled` is derived only from that closed environment. The private lease schema
  remains backward-compatible: an old lease without OTel keys means base.
- Only observed selection adds `--profile observability`, the exact four monitoring services,
  exactly three dynamically allocated loopback ports named `PROMETHEUS_DEV_PORT`,
  `TEMPO_DEV_PORT`, and `GRAFANA_DEV_PORT`, and four unique-project monitoring image tags.
  Collector has no host binding.
- `expected_services`, published-port validation/resolution, readiness, child environment,
  diagnostics, `down -v --remove-orphans --rmi local`, second-pass container/network/volume/
  sandbox proof, process-group proof, and image-absence proof all consume that same immutable
  selection.
- Ambient `OTEL_*`, `COMPOSE_PROFILES`, `.env`, project/port/socket/mode/crash selectors, Docker
  context, and host state remain scrubbed. Rootful GID, verified rootless socket, master key,
  recovery lease, barriers, signals, full canonical container IDs, workspace initializer labels,
  direct-workspace ledger, and exact cleanup from predecessor tip
  `ee66c588014acf8e448352a7e5e458aca63d37fe` remain authoritative.

Make owns only these delegating targets and adds both to `.PHONY`:

~~~make
observability-up: ## Start one leased stack with the optional monitoring profile
	$(PHASE10_HARNESS) up --mode $(PHASE10_MODE) --observability

observability-down: ## Exhaustively clean the leased observed stack
	$(PHASE10_HARNESS) down
~~~

Every existing live target remains harness-owned. Unit tests use an injected recorder and prove
exact commands, immutable selection, service/port/image inventories, lease round-trip and old-
lease compatibility, successful cleanup, and fail-closed survivor behavior without Docker.

### 17.3 Use one poison-resistant render authority and the complete matrix

Extend the accepted tool-worker Compose renderer; do not create a looser parallel renderer. Its
contract is exact:

- file order is base, optional dev, then exactly one socket-mode overlay;
- profile selection is explicit and ambient `COMPOSE_PROFILES` is ignored;
- `COMPOSE_DISABLE_ENV_FILE=1`; all competing Compose, Docker, mode, crash, and OTel state is
  removed;
- rootful render input includes an absolute socket source and positive
  `SANDBOX_DOCKER_GID`; rootless includes an absolute verified socket source and omits the GID;
- execution uses an argv with `shell=False`, 30-second timeout, captured text, checked exit, and
  JSON-object validation; and
- a placeholder socket exists only in a named unit-test render seam. The executable authority
  gate always requires the caller's real selected socket and never describes a placeholder as
  verification.

Render and assert the Cartesian product:

~~~text
rootful/rootless x production/dev x profile absent/present
default APP_ENV and explicit APP_ENV=test
empty endpoint and exact bundled endpoint
~~~

Profile-absent renders contain no monitoring service, empty endpoint, false insecure, and
unchanged product behavior. Profile-present renders contain exactly the four services. Merely
enabling the profile with an empty endpoint still leaves every application no-op. Only the leased
`observability-up` selection binds the profile to the internal endpoint. Retain every predecessor
queue, dependency, token, secret-recipient, socket, UID/GID, crash, sandbox-network, rootless
transport, and port assertion; change only the network sets in section 17.1.

Every six ordinary Python services receive exactly this non-secret settings projection:

~~~text
OTEL_EXPORTER_OTLP_ENDPOINT                default empty
OTEL_EXPORTER_OTLP_INSECURE                default false
OTEL_TRACES_SAMPLER                        default parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG                    default 0.10
OTEL_BSP_MAX_QUEUE_SIZE                    default 2048
OTEL_BSP_MAX_EXPORT_BATCH_SIZE             default 512
OTEL_EXPORTER_OTLP_TIMEOUT_MILLIS           default 5000
OTEL_METRIC_EXPORT_INTERVAL_MILLIS          default 60000
~~~

Do not inject blank CA/client-certificate/client-key paths. External TLS uses deployment-override,
container-visible, read-only paths; key/certificate bytes never enter Compose environment or image
layers. Add web `APP_ENV: ${APP_ENV:-production}` in base and `${APP_ENV:-dev}` in dev. Explicit
`APP_ENV=test` reaches web, all six Python services, and the rootless adapter in both modes. Web
gets no OTel key/network. The adapter retains only Task 1 `APP_ENV`/`LOG_LEVEL`, remains
`engine`-only, and gets no OTel runtime, key, or backend network.

### 17.4 Make monitoring images, configuration, security, and bounds closed

Every monitoring image uses the narrow build contract:

~~~yaml
build:
  context: docker
  dockerfile: monitoring.Dockerfile
  args:
    BASE_IMAGE: <one exact reviewed version tag>
~~~

The Dockerfile accepts exactly the four pinned `BASE_IMAGE` values, pins the exact BusyBox
version, and copies its probe executable with mode `0555`. Reject `latest`, floating tags, extra
build args, repository-root context, or repository-context `COPY`/`ADD`.

Retain the four exact monitoring image tag strings and BusyBox tag already specified by the
tracked Task 10 draft; this addendum does not authorize choosing newer/floating replacements at
implementation time. If any of those five exact tags is absent from the applied draft, stop for a
reviewed plan amendment before writing Docker or Compose files.

Collector configuration is exact and bounded: only OTLP/gRPC on `0.0.0.0:4317`; loopback-only
health at `127.0.0.1:13133`; `memory_limiter` before `batch` in both pipelines with exact 256/64
MiB limits; exact batch `512/1024/5s`; finite Tempo exporter queue and retry elapsed time;
Prometheus translation `UnderscoreEscapingWithoutSuffixes`; no resource-to-label conversion; no
log pipeline; structured Collector diagnostics; and no product-payload receiver/exporter.

Prometheus has one Collector target, exact `15d` retention, no remote write/admin/lifecycle
mutation endpoint. Tempo is local, single-tenant, exact `72h` replaceable retention and exposes
only the gRPC receiver Collector needs. Grafana disables anonymous access and sign-up, has no base
host binding, uses only repository provisioning, and exposes only Prometheus/Tempo datasources.
Disable supported call-home/update checks. No backend receives a product secret/env key, Docker
socket, product network, or host path except reviewed read-only config/dashboard binds.

Every healthcheck is a complete exec-form map with exact local executable/URL, interval, timeout,
retries, and start period. Reject `CMD-SHELL`, remote targets, omitted bounds, or probes against
another service. Every optional backend has `restart: unless-stopped`, `cap_drop: [ALL]`,
`no-new-privileges:true`, and explicit finite process/memory bounds. Any selected read-only-rootfs
or tmpfs setting must be proven against the pinned image in Task 11; YAML does not establish
compatibility.

Apply Docker `json-file` logging with exact string options `max-size: "20m"` and
`max-file: "5"` to every long-running base/dev/profile service, including the rootless adapter,
dev-only fakes, and four backends. `sandbox-image` is the sole build-only exclusion. Tests enumerate
each rendered active set and reject missing/changed/numeric/unbounded mappings. Infrastructure
stdout is not thereby granted application JSON-v1 status.

### 17.5 Generate and independently validate the sixteen-panel dashboard

`scripts/build_phase10_dashboard.py` defines every import/helper before RED, writes atomically,
and makes output byte-identical, sorted, and newline-terminated. Tests independently require:

- exactly sixteen panels, one per canonical Task 3 instrument;
- exact UID, schema version, range, refresh, deterministic IDs/grid, datasource UID, PromQL, and
  unit;
- metric names from the one Task 3 registry, with only histogram-derived suffixes and no second
  counter `_total` suffix;
- no forbidden identifier label, query, or variable;
- fixed, edit-disabled, repository-only provisioning and only internal
  `http://prometheus:9090` / `http://tempo:3200` URLs; and
- exact mapping keys in datasource/dashboard YAML and dashboard JSON.

Legends use only each panel's owned labels. Workspace/task/run/request/correlation/trace/
connection/tool-call/URL/host/repository/project/model identifiers are forbidden in dashboard
queries, labels, and variables. `trace_id` is allowed only in the one reviewed Prometheus
datasource exemplar link, never in a dashboard query/variable/label.

### 17.6 Use executable RED/GREEN and defer live claims to Task 11

Before RED, install all delayed-import helpers so missing production paths fail inside named tests.
Run independent socket-free RED groups:

~~~bash
uv run pytest tests/test_phase10_observability_compose.py -q
uv run pytest tests/test_phase10_tool_worker_compose.py -q
~~~

Expected RED names the absent monitoring/profile/dashboard/authority selection or deliberately
changed network/log/environment assertions. Collection/import/helper failure, ambient `.env`, a
live socket, or daemon access is invalid RED.

After implementation run:

~~~bash
uv run python scripts/build_phase10_dashboard.py --check
uv run pytest \
  tests/test_phase10_observability_compose.py \
  tests/test_phase10_tool_worker_compose.py \
  tests/test_phase9_production_compose.py \
  tests/integration/test_phase10_tool_worker_boundary.py -q
uv run ruff check \
  scripts/assert_phase10_observability_compose.py \
  scripts/assert_phase10_tool_worker_compose.py \
  scripts/build_phase10_dashboard.py \
  tests/test_phase10_observability_compose.py \
  tests/test_phase10_tool_worker_compose.py \
  tests/integration/phase10_upgrade_harness.py
uv run ruff format --check \
  scripts/assert_phase10_observability_compose.py \
  scripts/assert_phase10_tool_worker_compose.py \
  scripts/build_phase10_dashboard.py \
  tests/test_phase10_observability_compose.py \
  tests/test_phase10_tool_worker_compose.py \
  tests/integration/phase10_upgrade_harness.py
uv run mypy
~~~

Run rootful/rootless `docker compose ... config --quiet` only through the verified socket
authorities with every exact required input. Task 10 starts, pulls, or builds no live container.
Task 11 owns the first two-mode pinned-image/config/security/readiness acceptance; incompatibility
is returned to Task 10 and is not weakened in Task 11.

### 17.7 Make Task 10's 18 paths and committed tree exact

Replace Task 10 `Files`, global File Map ownership, and staging with this exact mirrored array:

~~~bash
set -euo pipefail
task10_paths=(
  .env.example
  Makefile
  compose.dev.yaml
  compose.rootless.yaml
  compose.yaml
  docker/monitoring.Dockerfile
  ops/observability/collector.yaml
  ops/observability/grafana/dashboards/jhin-overview.json
  ops/observability/grafana/provisioning/dashboards/jhin.yaml
  ops/observability/grafana/provisioning/datasources/jhin.yaml
  ops/observability/prometheus.yaml
  ops/observability/tempo.yaml
  scripts/assert_phase10_observability_compose.py
  scripts/assert_phase10_tool_worker_compose.py
  scripts/build_phase10_dashboard.py
  tests/integration/phase10_upgrade_harness.py
  tests/test_phase10_observability_compose.py
  tests/test_phase10_tool_worker_compose.py
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task10_paths[@]}"
git diff --check -- "${task10_paths[@]}"
git add -- "${task10_paths[@]}"
expected_index="$(printf '%s\n' "${task10_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task10_paths[@]}"
git commit --only "${task10_paths[@]}" \
  -m "feat(observability): add optional monitoring profile"
test "$(git show -s --format=%s HEAD)" = \
  "feat(observability): add optional monitoring profile"
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)"
test "$actual_commit_paths" = "$expected_index"
test -z "$(git diff --cached --name-only)"
~~~

Task 10 owns no CI, Task 11 integration test/artifact, dependency/lock, web source, or Task 12
evidence path. A new path requires a reviewed File Map/Files/manifest amendment before editing.

### 17.8 Bind Task 11 and Task 12 handoffs

- **Task 11** consumes the observed selection on the same `ComposeAuthority`, dynamic monitoring
  ports, exact service/image/network inventory, process groups, recovery lease, and exhaustive
  cleanup. It proves live image IDs/build args, supported security settings, config parsing,
  readiness, retention, dashboard/datasource load, isolated runtime networks, and Collector
  stop/start recovery in both accepted modes. Application logs remain unfiltered; backend logs
  remain diagnostics only.
- **Task 12** discovers monitoring image versions through the same poison-resistant rootless
  observed renderer and verified socket, reruns dashboard and both Compose authorities, consumes
  Task 11's live success/cleanup proof, and stages no Task 10 path. No evidence row may call static
  YAML a live readiness result.

### Task 11: Prove End-to-End Telemetry, Fail-Open Operation, and Secret Exclusion

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`
- Modify: `docs/operations/telemetry.md`
- Modify: `packages/connectors/src/jhin_connectors/testing/fake_linear.py`
- Modify: `packages/connectors/tests/linear/test_fake_linear_admin.py`
- Modify: `packages/models/src/jhin_models/testing/fake_openai.py`
- Modify: `packages/models/tests/test_fake_openai.py`
- Modify: `scripts/phase10_artifact.py`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/emit_phase10_metrics.py`
- Modify: `tests/integration/phase10_upgrade_harness.py`
- Modify: `tests/integration/test_phase10_telemetry.py`
- Modify: `tests/test_phase10_artifact.py`
- Modify: `tests/test_phase10_telemetry_harness.py`

**Interfaces:**
- Consumes the accepted Task 10 handoff and produces the exact Task 11 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write the harness/mode/legacy contract tests and run RED before rewriting it**

Create `tests/test_phase10_telemetry_harness.py` first:

```python
from pathlib import Path

import pytest

from tests.integration.conftest import compose_files, resolve_stack_contract


def test_legacy_integration_keeps_project_fallback_and_required_services() -> None:
    contract = resolve_stack_contract({})
    assert contract.project == "jhin"
    assert contract.telemetry_mode is None
    assert contract.socket_mode == "rootless"
    assert contract.required_services == frozenset(
        {
            "api",
            "web",
            "workflow-worker",
            "event-worker",
            "postgres",
            "nats",
            "temporal",
        }
    )


def test_telemetry_contract_is_strict_and_selects_exactly_one_overlay() -> None:
    with pytest.raises(ValueError, match="JHIN_TEST_COMPOSE_PROJECT"):
        resolve_stack_contract({"JHIN_TELEMETRY_MODE": "base"})
    rootful = resolve_stack_contract(
        {
            "JHIN_TELEMETRY_MODE": "observed",
            "JHIN_TEST_COMPOSE_PROJECT": "jhin-phase10-observed",
            "PHASE10_SOCKET_MODE": "rootful",
            "SANDBOX_DOCKER_GID": "998",
        }
    )
    assert rootful.socket_mode == "rootful"
    assert compose_files(rootful.socket_mode).count("compose.rootful.yaml") == 1
    assert "compose.rootless.yaml" not in compose_files(rootful.socket_mode)
    with pytest.raises(ValueError, match="must be unset"):
        resolve_stack_contract(
            {
                "JHIN_TELEMETRY_MODE": "base",
                "JHIN_TEST_COMPOSE_PROJECT": "jhin-phase10-base",
                "PHASE10_SOCKET_MODE": "rootless",
                "SANDBOX_DOCKER_GID": "998",
            }
        )


def test_legacy_make_gate_still_invokes_unscoped_integration_suite() -> None:
    makefile = Path("Makefile").read_text()
    recipe = makefile.split("test-integration:", 1)[1].split("\n\n", 1)[0]
    assert "uv run pytest -m integration tests/integration -v" in recipe
    assert "JHIN_TELEMETRY_MODE" not in recipe
    assert "JHIN_TEST_COMPOSE_PROJECT" not in recipe


def test_scenario_driver_contract_exists_before_harness_rewrite() -> None:
    path = Path("tests/integration/test_phase10_telemetry.py")
    assert path.is_file()
    source = path.read_text()
    tree = ast.parse(source)
    fixtures = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "scenario_driver" in fixtures
    for required in (
        "prompt",
        "completion",
        "connector_response",
        "connector_error",
        "authorization",
        "cookie",
        "dsn_user",
        "dsn_password",
        "webhook_body",
        "tool_output",
        "sandbox_secret_env",
    ):
        assert f"canaries.{required}" in source
```

Add `import ast` to this test file.

Run:

```bash
uv run pytest tests/test_phase10_telemetry_harness.py -q
```

Expected: FAIL because `compose_files` and `resolve_stack_contract` do not exist. This RED occurs
before either the shared autouse fixture or telemetry scenario is rewritten.

- [ ] **Step 2: Implement the project-bound harness without changing legacy behavior**

Now replace the implicit `Stack` name with this complete harness in
`tests/integration/conftest.py`. `scenario_driver` is implemented in
`test_phase10_telemetry.py` by moving the already-working connection/agent/grant/trigger builders
from `test_phase7_exit.py` unchanged; its concrete signature and return value are fixed here, so
no dynamic fixture lookup or hidden global is permitted:

```python
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import secrets
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest

APPLICATION_SERVICES = (
    "web",
    "api",
    "workflow-worker",
    "agent-worker",
    "tool-worker",
    "event-worker",
    "sandbox-runner",
)
SocketMode = Literal["rootful", "rootless"]


def compose_files(mode: SocketMode) -> tuple[str, ...]:
    return (
        "-f",
        "compose.yaml",
        "-f",
        "compose.dev.yaml",
        "-f",
        f"compose.{mode}.yaml",
    )


LEGACY_REQUIRED_SERVICES = frozenset(
    {
        "api",
        "web",
        "workflow-worker",
        "event-worker",
        "postgres",
        "nats",
        "temporal",
    }
)
TELEMETRY_REQUIRED_SERVICES = frozenset(
    {
        "api",
        "web",
        "workflow-worker",
        "agent-worker",
        "tool-worker",
        "event-worker",
        "sandbox-runner",
        "postgres",
        "nats",
        "temporal",
        "fake-provider",
        "fake-linear",
    }
)


@dataclass(frozen=True)
class StackContract:
    project: str
    telemetry_mode: Literal["base", "observed"] | None
    socket_mode: SocketMode
    required_services: frozenset[str]


def resolve_stack_contract(environ: Mapping[str, str]) -> StackContract:
    raw_mode = environ.get("JHIN_TELEMETRY_MODE")
    if raw_mode is not None and raw_mode not in {"base", "observed"}:
        raise ValueError("JHIN_TELEMETRY_MODE must be base or observed")
    raw_project = environ.get("JHIN_TEST_COMPOSE_PROJECT")
    if raw_mode is not None and raw_project is None:
        raise ValueError("JHIN_TEST_COMPOSE_PROJECT is required for telemetry integration")
    project = validate_compose_project(raw_project or "jhin")
    raw_socket_mode = environ.get("PHASE10_SOCKET_MODE", "rootless")
    if raw_socket_mode not in {"rootful", "rootless"}:
        raise ValueError("PHASE10_SOCKET_MODE must be rootful or rootless")
    if raw_socket_mode == "rootful":
        raw_gid = environ.get("SANDBOX_DOCKER_GID", "")
        if not raw_gid.isdecimal() or int(raw_gid) == 0:
            raise ValueError("rootful mode requires the socket's nonzero SANDBOX_DOCKER_GID")
    elif "SANDBOX_DOCKER_GID" in environ:
        raise ValueError("SANDBOX_DOCKER_GID must be unset in rootless mode")
    required = TELEMETRY_REQUIRED_SERVICES if raw_mode is not None else LEGACY_REQUIRED_SERVICES
    if raw_mode == "observed":
        required |= {"otel-collector", "prometheus", "tempo", "grafana"}
    return StackContract(
        project=project,
        telemetry_mode=cast(Literal["base", "observed"] | None, raw_mode),
        socket_mode=cast(SocketMode, raw_socket_mode),
        required_services=frozenset(required),
    )


@dataclass(frozen=True)
class ScenarioResult:
    trace_id: str
    workspace_id: str
    task_id: str
    run_id: str
    state: str


@dataclass(frozen=True)
class TelemetryCanaries:
    prompt: str
    completion: str
    connector_response: str
    connector_error: str
    authorization: str
    cookie: str
    api_key: str
    private_key: str
    dsn_user: str
    dsn_password: str
    webhook_secret: str
    webhook_body: str
    tool_output: str
    sandbox_secret_env: str

    def values(self) -> tuple[str, ...]:
        return tuple(getattr(self, field.name) for field in dataclasses.fields(self))


ScenarioDriver = Callable[["Stack", str, TelemetryCanaries | None], Awaitable[ScenarioResult]]


@dataclass
class Stack:
    project: str
    mode: Literal["base", "observed"]
    socket_mode: SocketMode
    api: httpx.AsyncClient
    tempo_url: str
    scenario_driver: ScenarioDriver
    last_result: ScenarioResult | None = None

    def _compose_sync(self, *args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if self.socket_mode == "rootless":
            environment.pop("SANDBOX_DOCKER_GID", None)
        return subprocess.run(
            ["docker", "compose", "-p", self.project, *compose_files(self.socket_mode), *args],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )

    async def compose(self, *args: str, timeout: float = 180.0) -> str:
        result = await asyncio.to_thread(self._compose_sync, *args, timeout=timeout)
        return result.stdout

    async def services(self) -> set[str]:
        return set((await self.compose("ps", "--services", "--filter", "status=running")).split())

    async def logs(self, service: str) -> list[str]:
        if service not in APPLICATION_SERVICES:
            raise ValueError(f"not an application service: {service}")
        output = await self.compose("logs", "--no-color", "--no-log-prefix", service)
        return [line for line in output.splitlines() if line]

    async def logs_all_application_services(self) -> str:
        return "\n".join(
            line for service in APPLICATION_SERVICES for line in await self.logs(service)
        )

    async def stop(self, service: str) -> None:
        await self.compose("stop", "--timeout", "10", service)

    async def start(self, service: str) -> None:
        await self.compose("start", service)
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            value = json.loads(await self.compose("ps", service, "--format", "json"))
            rows = value if isinstance(value, list) else [value]
            if rows and all(row.get("Health") in {"", "healthy"} for row in rows):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{service} did not become healthy")

    async def fire_linear_engineering_webhook(self, *, traceparent: str) -> str:
        self.last_result = await self.scenario_driver(self, traceparent, None)
        return self.last_result.trace_id

    async def drive_telemetry_canaries(self) -> tuple[str, ...]:
        manifest_name = os.environ.get("JHIN_TELEMETRY_CANARY_FILE")
        if manifest_name:
            manifest = json.loads(Path(manifest_name).read_text())
            expected = {field.name for field in dataclasses.fields(TelemetryCanaries)}
            if (
                set(manifest) != {"schema_version", "kind", "values"}
                or manifest["schema_version"] != 1
                or manifest["kind"] != "telemetry_canaries"
                or not isinstance(manifest["values"], dict)
                or set(manifest["values"]) != expected
                or not all(
                    isinstance(value, str) and value for value in manifest["values"].values()
                )
            ):
                raise ValueError("telemetry canary manifest schema mismatch")
            canaries = TelemetryCanaries(**manifest["values"])
        else:
            canaries = TelemetryCanaries(
                **{
                    field.name: f"phase10-{field.name}-{secrets.token_hex(12)}"
                    for field in dataclasses.fields(TelemetryCanaries)
                }
            )
        traceparent = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
        self.last_result = await self.scenario_driver(self, traceparent, canaries)
        return canaries.values()

    async def wait_for_task_terminal(self, *, timeout: float) -> dict[str, object]:
        if self.last_result is None:
            raise AssertionError("no scenario has been started")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            response = await self.api.get(
                f"/api/v1/workspaces/{self.last_result.workspace_id}/tasks/{self.last_result.task_id}"
            )
            response.raise_for_status()
            document = response.json()
            if document["task"]["state"] in {"completed", "failed", "cancelled"}:
                return cast(dict[str, object], document["task"])
            await asyncio.sleep(0.5)
        raise TimeoutError("task did not reach a terminal state")

    async def run_fake_task(self, *, timeout: float) -> dict[str, object]:
        await self.fire_linear_engineering_webhook(
            traceparent=f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
        )
        return await self.wait_for_task_terminal(timeout=timeout)

    async def wait_for_tempo_trace(
        self,
        trace_id: str,
        *,
        timeout: float,
        required_names: Collection[str] = (),
    ) -> list[dict[str, object]]:
        deadline = asyncio.get_running_loop().time() + timeout
        required = frozenset(required_names)
        async with httpx.AsyncClient(base_url=self.tempo_url, timeout=5) as client:
            while asyncio.get_running_loop().time() < deadline:
                response = await client.get(f"/api/traces/{trace_id}")
                if response.status_code == 200:
                    batches = response.json().get("batches", [])
                    spans = [
                        {**span, "trace_id": str(span.get("traceId", "")).lower()}
                        for batch in batches
                        for scope in batch.get("scopeSpans", [])
                        for span in scope.get("spans", [])
                    ]
                    if spans and required <= {str(span.get("name", "")) for span in spans}:
                        return spans
                elif response.status_code != 404:
                    response.raise_for_status()
                await asyncio.sleep(0.5)
        raise TimeoutError(f"Tempo did not return trace {trace_id}")

    async def collector_metrics(self) -> str:
        return await self.compose(
            "exec",
            "-T",
            "otel-collector",
            "/bin/busybox",
            "wget",
            "-qO-",
            "http://127.0.0.1:9464/metrics",
        )

    async def emit_metric_fixtures(self) -> None:
        await self.compose(
            "cp",
            "tests/integration/emit_phase10_metrics.py",
            "api:/tmp/emit_phase10_metrics.py",
        )
        await self.compose("exec", "-T", "api", "python", "/tmp/emit_phase10_metrics.py")

    async def emit_database_canary_probe(
        self, traceparent: str, canaries: TelemetryCanaries
    ) -> None:
        descriptor, local_name = tempfile.mkstemp(prefix="phase10-sink-", suffix=".json")
        local_path = Path(local_name)
        container_path = "/tmp/phase10-sink-canaries.json"
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(
                    {"dsn_user": canaries.dsn_user, "dsn_password": canaries.dsn_password},
                    stream,
                )
            await self.compose(
                "cp",
                "tests/integration/emit_phase10_metrics.py",
                "api:/tmp/emit_phase10_metrics.py",
            )
            await self.compose("cp", str(local_path), f"api:{container_path}")
            await self.compose(
                "exec",
                "-T",
                "api",
                "python",
                "/tmp/emit_phase10_metrics.py",
                "--database-canary-file",
                container_path,
                "--traceparent",
                traceparent,
            )
        finally:
            local_path.unlink(missing_ok=True)
            with contextlib.suppress(subprocess.CalledProcessError):
                await self.compose(
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-c",
                    "from pathlib import Path; Path('/tmp/phase10-sink-canaries.json').unlink(missing_ok=True)",
                )

    async def collect_telemetry_sinks(self) -> str:
        if self.last_result is None:
            raise AssertionError("no scenario has been started")
        spans = await self.wait_for_tempo_trace(
            self.last_result.trace_id,
            timeout=30,
            required_names={
                "model.request",
                "connector.http",
                "connector.database",
                "sandbox.job.lifecycle",
            },
        )
        return "\n".join(
            (
                await self.logs_all_application_services(),
                json.dumps(spans, sort_keys=True),
                await self.collector_metrics(),
            )
        )


@pytest.fixture
async def telemetry_stack(scenario_driver: ScenarioDriver) -> AsyncIterator[Stack]:
    contract = resolve_stack_contract(os.environ)
    if contract.telemetry_mode is None:
        raise ValueError("telemetry_stack requires JHIN_TELEMETRY_MODE")
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as api:
        yield Stack(
            contract.project,
            contract.telemetry_mode,
            contract.socket_mode,
            api,
            os.environ.get("JHIN_TEMPO_URL", "http://127.0.0.1:3200"),
            scenario_driver,
        )


@pytest.fixture
def base_stack(telemetry_stack: Stack) -> Stack:
    assert telemetry_stack.mode == "base"
    return telemetry_stack


@pytest.fixture
def observed_stack(telemetry_stack: Stack) -> Stack:
    assert telemetry_stack.mode == "observed"
    return telemetry_stack
```

Replace the existing `_require_stack` body with an exact contract-aware check. The strict project
and expanded service set apply only when `JHIN_TELEMETRY_MODE` is present; the ordinary
`make test-integration` path retains its `jhin` fallback and original seven-service gate:

```python
@pytest.fixture(scope="session", autouse=True)
def _require_stack() -> None:
    try:
        contract = resolve_stack_contract(os.environ)
        result = compose(
            "ps",
            "--services",
            "--filter",
            "status=running",
            project=contract.project,
            socket_mode=contract.socket_mode,
        )
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.fail(f"docker compose stack not reachable: {exc}")
    running = set(result.stdout.split())
    missing = contract.required_services - running
    if missing:
        if contract.telemetry_mode is None:
            pytest.fail(
                "integration tests need the dev stack running (make dev); "
                f"missing: {sorted(missing)}"
            )
        pytest.fail(f"compose project {contract.project} missing services: {sorted(missing)}")
```

Extend `compose` with the keyword-only project argument using this complete replacement; explicit telemetry callers never use the legacy fallback, while older suites retain it:

```python
def compose(
    *args: str,
    timeout: float = 120.0,
    project: str | None = None,
    socket_mode: SocketMode | None = None,
) -> subprocess.CompletedProcess[str]:
    contract = resolve_stack_contract(os.environ)
    selected = validate_compose_project(project or contract.project)
    selected_mode = socket_mode or contract.socket_mode
    environment = os.environ.copy()
    if selected_mode == "rootless":
        environment.pop("SANDBOX_DOCKER_GID", None)
    return subprocess.run(
        ["docker", "compose", "-p", selected, *compose_files(selected_mode), *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
```

Implement the fixture itself in `test_phase10_telemetry.py`; it never imports another pytest
module and every HTTP target is either the local API or the in-Compose fake:

```python
import asyncio
import base64
import dataclasses
import json
import re
import shlex
import time
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD
from jhin_connectors.linear.webhook import sign_payload
from jhin_observability import FORBIDDEN_IDENTIFIER_LABELS, instrument_contracts
from tests.integration.conftest import (
    FAKE_LINEAR_URL,
    ScenarioDriver,
    ScenarioResult,
    Stack,
    TelemetryCanaries,
)


@pytest.fixture
def scenario_driver() -> ScenarioDriver:
    async def drive(
        stack: Stack, traceparent: str, canaries: TelemetryCanaries | None
    ) -> ScenarioResult:
        login = await stack.api.post(
            "/api/v1/auth/login",
            json={"email": DEV_OWNER_EMAIL, "password": DEV_OWNER_PASSWORD},
        )
        if login.status_code != 200:
            await stack.compose("run", "--rm", "--no-deps", "api", "jhin-seed-dev")
            login = await stack.api.post(
                "/api/v1/auth/login",
                json={"email": DEV_OWNER_EMAIL, "password": DEV_OWNER_PASSWORD},
            )
        login.raise_for_status()
        workspace = str(login.json()["memberships"][0]["workspace_id"])
        csrf = {"x-csrf-token": str(stack.api.cookies["jhin_csrf"])}

        async def post(path: str, value: Mapping[str, object]) -> dict[str, Any]:
            response = await stack.api.post(path, json=value, headers=csrf)
            assert response.status_code == 201, (path, response.status_code, response.text)
            return cast(dict[str, Any], response.json())

        tag = uuid4().hex[:10]
        linear_created = await post(
            f"/api/v1/workspaces/{workspace}/connections",
            {
                "connector_type": "linear",
                "name": f"Telemetry Linear {tag}",
                "auth_type": "api_key",
                "credentials": {"api_key": "fake-linear-api-key"},
                "config": {"base_url": "http://fake-linear:8080"},
            },
        )
        linear = linear_created["connection"]
        github: Mapping[str, object] | None = None
        if canaries is not None:
            github = (
                await post(
                    f"/api/v1/workspaces/{workspace}/connections",
                    {
                        "connector_type": "github",
                        "name": f"Telemetry GitHub {tag}",
                        "auth_type": "token",
                        "credentials": {"token": canaries.sandbox_secret_env},
                        "config": {"base_url": "http://fake-github:8080"},
                    },
                )
            )["connection"]
        cli = (
            await post(
                f"/api/v1/workspaces/{workspace}/connections",
                {
                    "connector_type": "cli",
                    "name": f"Telemetry CLI {tag}",
                    "auth_type": "none",
                    "credentials": {},
                    "config": {
                        "default_network": "internet" if github is not None else "none",
                        **({"git_connection_id": github["id"]} if github is not None else {}),
                    },
                },
            )
        )["connection"]
        provider = await post(
            f"/api/v1/workspaces/{workspace}/model-providers",
            {
                "type": "openai_compatible",
                "display_name": f"Telemetry provider {tag}",
                "base_url": "http://fake-provider:8080/v1",
            },
        )
        profile = await post(
            f"/api/v1/workspaces/{workspace}/model-profiles",
            {
                "provider_id": provider["id"],
                "model_name": "fake-mini",
                "display_name": f"Telemetry profile {tag}",
            },
        )
        agent = await post(
            f"/api/v1/workspaces/{workspace}/agents",
            {
                "name": f"Telemetry agent {tag}",
                "system_prompt": "Use both requested tools.",
                "model_profile_id": profile["id"],
            },
        )
        for capability, scope in (
            ("linear.issue.read", {"connection_id": linear["id"], "issue": "ENG-*"}),
            ("cli.command.execute", {"connection_id": cli["id"], "command": "printf *"}),
        ):
            await post(
                f"/api/v1/workspaces/{workspace}/agents/{agent['id']}/grants",
                {"capability": capability, "scope": scope, "effect": "allow"},
            )
        title = f"Telemetry {tag}"
        trigger = await post(
            f"/api/v1/workspaces/{workspace}/triggers",
            {
                "name": title,
                "connection_id": linear["id"],
                "event_type": "connector.linear.issue.updated",
                "filter": {
                    "all": [
                        {"path": "data.team.key", "op": "eq", "value": "ENG"},
                        {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
                        {"path": "data.title", "op": "eq", "value": title},
                    ]
                },
                "target_agent_id": agent["id"],
                "action_config": {"comment_back": False},
                "dedupe_window_seconds": 3600,
            },
        )
        prompt_text = canaries.prompt if canaries is not None else "telemetry prompt"
        completion_marker = (
            "[[telemetry_completion_b64:"
            + base64.b64encode(canaries.completion.encode()).decode()
            + "]]"
            if canaries is not None
            else ""
        )
        command = (
            f"printf %s {shlex.quote(canaries.tool_output)}"
            if canaries is not None
            else "printf telemetry-ok"
        )
        cli_arguments: dict[str, object] = {
            "connection_id": cli["id"],
            "command": command,
        }
        markers = " ".join(
            (
                f'[[tool:linear.issue.read {{"connection_id":"{linear["id"]}","issue":"ENG-142"}}]]',
                f"[[tool:cli.command.execute {json.dumps(cli_arguments, separators=(',', ':'))}]]",
                prompt_text,
                completion_marker,
            )
        )
        async with httpx.AsyncClient(base_url=FAKE_LINEAR_URL, timeout=15) as fake:
            edited = await fake.post(
                "/_admin/issues/ENG-142/edit",
                json={
                    "title": title,
                    "description": canaries.connector_response
                    if canaries is not None
                    else "telemetry connector response",
                },
            )
            edited.raise_for_status()
            fake_state = (await fake.get("/_state")).json()
        issue = fake_state["issues"]["ENG-142"]
        team = fake_state["teams"]["ENG"]
        backlog = next(row for row in team["states"] if row["name"] == "Backlog")
        todo = next(row for row in team["states"] if row["name"] == "Todo")
        payload = {
            "action": "update",
            "type": "Issue",
            "organizationId": "telemetry-org",
            "webhookId": f"telemetry-{tag}",
            "webhookTimestamp": int(time.time() * 1000),
            "url": issue["url"],
            "updatedFrom": {"stateId": backlog["id"]},
            "data": {
                "id": issue["id"],
                "identifier": "ENG-142",
                "title": title,
                "description": f"Run: {markers}",
                "priority": 0,
                "team": {"id": team["id"], "key": "ENG", "name": "Engineering"},
                "state": {"id": todo["id"], "name": "Todo", "type": todo["type"]},
                "labels": [],
                "url": issue["url"],
            },
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        webhook = linear_created["webhook"]
        await asyncio.sleep(6.0)
        response = await stack.api.post(
            webhook["url_path"],
            content=body,
            headers={
                "content-type": "application/json",
                "linear-event": "Issue",
                "linear-delivery": f"telemetry-{tag}",
                "linear-signature": sign_payload(webhook["secret"], body),
                "traceparent": traceparent,
            },
        )
        assert response.status_code == 202, response.text
        if canaries is not None:
            rejected = await stack.api.post(
                "/api/v1/auth/login",
                json={"email": "missing@example.invalid", "password": "invalid"},
                headers={
                    "Authorization": f"Bearer {canaries.authorization}",
                    "Cookie": f"jhin_session={canaries.cookie}",
                    "traceparent": traceparent,
                },
            )
            assert rejected.status_code == 401
            bad_body = json.dumps(
                {
                    "apiKey": canaries.api_key,
                    "privateKey": canaries.private_key,
                    "payload": canaries.webhook_body,
                },
                separators=(",", ":"),
            ).encode()
            bad_webhook = await stack.api.post(
                webhook["url_path"],
                content=bad_body,
                headers={
                    "content-type": "application/json",
                    "linear-event": "Issue",
                    "linear-delivery": f"telemetry-bad-{tag}",
                    "linear-signature": canaries.webhook_secret,
                    "traceparent": traceparent,
                },
            )
            assert bad_webhook.status_code == 401
            async with httpx.AsyncClient(base_url=FAKE_LINEAR_URL, timeout=15) as fake:
                configured = await fake.post(
                    "/_admin/telemetry/next-error",
                    json={"body": canaries.connector_error},
                )
                configured.raise_for_status()
            failed_verify = await stack.api.post(
                f"/api/v1/workspaces/{workspace}/connections/{linear['id']}/verify",
                headers={**csrf, "traceparent": traceparent},
            )
            assert failed_verify.status_code == 200
            assert failed_verify.json()["ok"] is False
            await stack.emit_database_canary_probe(traceparent, canaries)
        deadline = asyncio.get_running_loop().time() + 120
        while asyncio.get_running_loop().time() < deadline:
            rows_response = await stack.api.get(
                f"/api/v1/workspaces/{workspace}/triggers/{trigger['id']}/invocations"
            )
            rows_response.raise_for_status()
            rows = rows_response.json()
            if rows and rows[0].get("task_id"):
                task_id = str(rows[0]["task_id"])
                break
            await asyncio.sleep(0.5)
        else:
            raise TimeoutError("trigger did not bind a task")
        stack.last_result = ScenarioResult(traceparent.split("-")[1], workspace, task_id, "", "")
        terminal = await stack.wait_for_task_terminal(timeout=120)
        detail = (await stack.api.get(f"/api/v1/workspaces/{workspace}/tasks/{task_id}")).json()
        return ScenarioResult(
            traceparent.split("-")[1],
            workspace,
            task_id,
            str(detail["runs"][0]["id"]),
            str(terminal["state"]),
        )

    return drive
```

The canary path above is exhaustive: the prompt enters the webhook/task description; completion is decoded only by the fake model into its response; connector response comes from the fake Linear issue; connector error comes from the one-shot fake Linear error body; Authorization/cookie enter a rejected login; camelCase `apiKey`/`privateKey`, webhook secret, and webhook body enter a rejected webhook; tool output is emitted by the CLI sandbox command; the sandbox secret enters `secret_env` through the fake-GitHub git credential; and DSN user/password enter the async connector failure probe. It never calls a hosted service.

- [ ] **Harness RED: add deterministic fake-only completion and connector-error seams**

Write these tests before editing either fake:

```python
def test_telemetry_completion_marker_is_decoded_only_into_fake_response() -> None:
    canary = "completion-response-canary"
    encoded = base64.b64encode(canary.encode()).decode()
    status, response = build_completion(
        {
            "model": "fake-mini",
            "messages": [{"role": "user", "content": f"[[telemetry_completion_b64:{encoded}]]"}],
        }
    )
    assert status == 200
    assert response["choices"][0]["message"]["content"] == canary


def test_fake_linear_one_shot_error_body_is_consumed_once() -> None:
    state = FakeLinearState()
    assert handle_request(
        state, "POST", "/_admin/telemetry/next-error", {}, {"body": "connector-error-canary"}
    ) == (200, {"configured": True})
    first = handle_request(
        state,
        "POST",
        "/graphql",
        {"Authorization": "fake-linear-api-key"},
        {"query": "{ viewer { id } }"},
    )
    second = handle_request(
        state,
        "POST",
        "/graphql",
        {"Authorization": "fake-linear-api-key"},
        {"query": "{ viewer { id } }"},
    )
    assert first == (500, {"errors": [{"message": "connector-error-canary"}]})
    assert second[0] == 200
```

Run:

```bash
uv run pytest packages/models/tests/test_fake_openai.py \
  packages/connectors/tests/linear/test_fake_linear_admin.py -q
```

Expected: FAIL because neither closed test seam exists. Then add exactly:

```python
# fake_openai.py
TELEMETRY_COMPLETION_RE = re.compile(r"\[\[telemetry_completion_b64:([A-Za-z0-9+/=]+)\]\]")


def _telemetry_completion(messages: list[dict[str, Any]]) -> str | None:
    for message in messages:
        match = TELEMETRY_COMPLETION_RE.search(str(message.get("content", "")))
        if match is not None:
            try:
                value = base64.b64decode(match.group(1), validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                return None
            return value[:2_000]
    return None


def _completion_reply(model: str, messages: list[dict[str, Any]]) -> str:
    tool_results = [
        str(message.get("content", "")) for message in messages if message.get("role") == "tool"
    ]
    last_user = next(
        (
            str(message.get("content", ""))
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )
    override = _telemetry_completion(messages)
    if override is not None:
        return override
    if tool_results:
        return (
            f"[{model}] Done after {len(tool_results)} tool call(s). Last result: "
            + tool_results[-1][:200].strip()
        )
    return f"[{model}] Completed: {last_user[:200].strip() or 'no instruction given'}"


# fake_linear.py
def _configure_next_telemetry_error(
    state: FakeLinearState,
    method: str,
    path: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    if method != "POST" or path != "/_admin/telemetry/next-error":
        return None
    value = body.get("body")
    if not isinstance(value, str) or not 1 <= len(value) <= 2_000:
        return 400, {"error": "body must be a non-empty bounded string"}
    with state.lock:
        state.next_telemetry_error = value
    return 200, {"configured": True}


def _consume_next_telemetry_error(state: FakeLinearState) -> str | None:
    with state.lock:
        injected = state.next_telemetry_error
        state.next_telemetry_error = None
    return injected
```

Import `Any` in both fake modules and `base64`, `binascii`, and `re` in `fake_openai.py`. In
`build_completion`, replace the ordinary reply-selection block with
`reply = _completion_reply(model, messages)`. In `FakeLinearState.__init__`, initialize
`self.next_telemetry_error: str | None = None`. The first `_admin` statements call
`telemetry = _configure_next_telemetry_error(state, method, path, body)` and return it when non-null.
In the authenticated `/graphql` branch, call `_consume_next_telemetry_error(state)` immediately
after authorization; return `(500, {"errors": [{"message": injected}]})` when non-null, otherwise
return the existing `_graphql(state, body)` result.

These are dev fake-provider controls only; production packages never read their marker/admin route.

- [ ] **Step 3: Write the failing connected-trace acceptance test**

Use the concrete `Stack.wait_for_tempo_trace`, `Stack.collector_metrics`, and `Stack.logs` methods
above. The exact `scenario_driver` drives a signed fake Linear webhook that starts a real task
whose fake model requests one ordinary connector tool and one sandbox tool. Assert this connected
span subset shares one trace ID:

```python
KNOWN_TRACE_ID = "0123456789abcdef0123456789abcdef"
KNOWN_TRACEPARENT = f"00-{KNOWN_TRACE_ID}-0123456789abcdef-01"

REQUIRED_SPANS = {
    "http.server.request",
    "db.operation",
    "nats.publish",
    "nats.consume",
    "trigger.dispatch",
    "temporal.start_workflow",
    "temporal.activity.resolve_advertised_tools",
    "temporal.activity.reason_agent_step",
    "agent.reason_step",
    "model.request",
    "temporal.activity.execute_bound_tool",
    "tool.gateway.execute",
    "connector.http",
    "sandbox.client",
    "sandbox.server",
    "sandbox.job.lifecycle",
    "temporal.activity.commit_agent_step",
}


@pytest.mark.integration
async def test_webhook_agent_tool_connector_and_sandbox_trace_is_connected(
    observed_stack: Stack,
) -> None:
    trace_id = await observed_stack.fire_linear_engineering_webhook(traceparent=KNOWN_TRACEPARENT)
    terminal = await observed_stack.wait_for_task_terminal(timeout=120)
    spans = await observed_stack.wait_for_tempo_trace(
        trace_id, timeout=30, required_names=REQUIRED_SPANS
    )
    assert terminal["state"] == "completed"
    assert REQUIRED_SPANS <= {span["name"] for span in spans}
    assert all(span["trace_id"] == KNOWN_TRACE_ID for span in spans)
    starts = {span["name"]: int(span["startTimeUnixNano"]) for span in spans}
    assert starts["agent.reason_step"] <= starts["temporal.activity.execute_bound_tool"]
```

The fixture uses only fake providers and deterministic seeded data. It never calls GitHub, Linear, model, or hosted telemetry APIs.

- [ ] **Step 4: Add cross-sink telemetry canaries and exact metric checks**

Generate one unique value for every field in `TelemetryCanaries`: prompt, model completion, connector response/error, Authorization, cookie, camelCase API/private keys, DSN user/password, webhook secret/body, tool output, and sandbox secret environment. Drive the exact successful/failing injection path registered below and scan JSON service logs, Tempo span names/attributes/events/status, and Prometheus labels/exemplars:

```python
CANARY_INJECTION_POINTS = {
    "prompt": "signed webhook data.description -> model request",
    "completion": "fake OpenAI telemetry_completion_b64 -> model response",
    "connector_response": "fake Linear issue description -> connector response",
    "connector_error": "fake Linear one-shot GraphQL error -> connector error",
    "authorization": "rejected login Authorization header",
    "cookie": "rejected login Cookie header",
    "api_key": "rejected webhook apiKey field",
    "private_key": "rejected webhook privateKey field",
    "dsn_user": "connector.database async failure text DSN username",
    "dsn_password": "connector.database async failure text DSN password",
    "webhook_secret": "rejected webhook signature header",
    "webhook_body": "rejected webhook body payload field",
    "tool_output": "CLI sandbox stdout",
    "sandbox_secret_env": "fake GitHub token -> sandbox secret_env GIT_TOKEN",
}


@pytest.mark.integration
async def test_no_telemetry_sink_contains_raw_or_encoded_canary(
    observed_stack: Stack,
) -> None:
    assert set(CANARY_INJECTION_POINTS) == {
        field.name for field in dataclasses.fields(TelemetryCanaries)
    }
    canaries = await observed_stack.drive_telemetry_canaries()
    sinks = await observed_stack.collect_telemetry_sinks()
    for canary in canaries:
        raw = canary.encode()
        for encoded in (
            canary,
            quote(canary, safe=""),
            base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
        ):
            assert encoded not in sinks
    assert observed_stack.last_result is not None
    spans = await observed_stack.wait_for_tempo_trace(
        observed_stack.last_result.trace_id,
        timeout=30,
        required_names={
            "model.request",
            "connector.http",
            "connector.database",
            "sandbox.job.lifecycle",
        },
    )
    assert {"model.request", "connector.http", "connector.database", "sandbox.job.lifecycle"} <= {
        span["name"] for span in spans
    }
```

Assert Prometheus exposes every Task 3 instrument name after fixtures create a relevant observation, and label-name sets match its exact contract. Explicitly fail on any forbidden identifier label. Verify replaying `reason_agent_step`/`execute_bound_tool` leaves committed token/cost/tool counters unchanged while `model_requests_total` increases only when a new provider attempt actually occurred.

Make that assertion against the Collector Prometheus exporter's actual `/metrics` response. The
test copies `emit_phase10_metrics.py` into the running API container, executes it, and its public
`JhinMetrics` calls add/record one valid point for every counter/histogram and install one
`Observation` for every gauge before `runtime.shutdown(timeout_millis=10_000)`. Implement this
complete executable module:

```python
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from opentelemetry.context import attach, detach

from jhin_connectors.telemetry import trace_connector_database
from jhin_observability import (
    MetricName,
    Observation,
    ObservabilityRuntime,
    ObservabilitySettings,
    extract_trace_context,
    initialize_observability,
)

FIXTURES = {
    "agent_runs_total": {"service": "agent-worker", "outcome": "completed"},
    "agent_run_failures_total": {"failure_class": "internal"},
    "model_requests_total": {"provider_type": "openai", "outcome": "ok"},
    "model_tokens_total": {"provider_type": "openai", "direction": "input"},
    "model_cost_estimate": {"provider_type": "openai"},
    "tool_calls_total": {"tool_family": "linear", "risk": "read", "outcome": "completed"},
    "tool_call_failures_total": {"tool_family": "linear", "failure_class": "internal"},
    "trigger_invocations_total": {"connector_type": "linear", "outcome": "started"},
    "trigger_failures_total": {"connector_type": "linear", "failure_class": "dispatch"},
    "sandbox_jobs_total": {"outcome": "completed", "network_policy": "none"},
    "temporal_activity_failures": {
        "task_queue": "jhin-tool-queue",
        "activity": "execute_bound_tool",
        "failure_class": "internal",
    },
}
HISTOGRAMS = {
    "agent_run_duration_seconds": {"outcome": "completed"},
    "sandbox_job_duration_seconds": {"outcome": "completed"},
}
GAUGES = {
    "nats_consumer_lag": (0, {"stream": "EVENTS", "consumer": "event-worker"}),
    "connector_health": (1, {"connector_type": "linear"}),
    "connector_connections": (1, {"connector_type": "linear", "outcome": "healthy"}),
}


def _runtime(version: str) -> ObservabilityRuntime:
    settings = ObservabilitySettings()
    return initialize_observability(
        settings.observability_config(service_name="api", service_version=version)
    )


def emit_metric_fixtures() -> None:
    runtime = _runtime("integration-fixture")
    try:
        for name, labels in FIXTURES.items():
            runtime.metrics.counter(cast(MetricName, name)).add(1, **labels)
        for name, labels in HISTOGRAMS.items():
            runtime.metrics.histogram(cast(MetricName, name)).record(0.1, **labels)
        for name, (value, labels) in GAUGES.items():
            runtime.metrics.set_observable(cast(MetricName, name), (Observation(value, labels),))
    finally:
        runtime.shutdown(timeout_millis=10_000)


async def emit_database_canary(path: Path, traceparent: str) -> None:
    document = json.loads(path.read_text())
    if set(document) != {"dsn_user", "dsn_password"} or not all(
        isinstance(document[key], str) and document[key] for key in document
    ):
        raise ValueError("database canary schema mismatch")
    runtime = _runtime("integration-canary")
    parent = extract_trace_context({"traceparent": traceparent})
    token = attach(parent)
    try:

        async def fail_like_asyncpg() -> None:
            raise RuntimeError(
                "postgresql://"
                + document["dsn_user"]
                + ":"
                + document["dsn_password"]
                + "@db.telemetry.invalid/jhin"
            )

        with contextlib.suppress(RuntimeError):
            await trace_connector_database("verify", fail_like_asyncpg, tracer=runtime.tracer)
    finally:
        detach(token)
        runtime.shutdown(timeout_millis=10_000)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-canary-file", type=Path)
    parser.add_argument("--traceparent")
    args = parser.parse_args(argv)
    if (args.database_canary_file is None) != (args.traceparent is None):
        parser.error("database canary file and traceparent must be supplied together")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.database_canary_file is None:
        emit_metric_fixtures()
    else:
        asyncio.run(emit_database_canary(args.database_canary_file, args.traceparent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

The script unlinks neither input nor artifacts; the `Stack` method owns both container and host cleanup in `finally`.

Parse every non-comment OpenMetrics sample with
`^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{([^}]*)\})?\s`. For each Task 3 counter/gauge assert the
actual sample name equals the instrument name and its label-name set equals the registry contract.
For each histogram assert `<name>_bucket` has the contract plus `le`, while `<name>_count` and
`<name>_sum` have exactly the contract. Explicitly assert names such as
`agent_runs_total_total` are absent. This is the GREEN proof that
`UnderscoreEscapingWithoutSuffixes` is active, not a YAML-only assertion.

The test calls `await observed_stack.emit_metric_fixtures()` before reading the endpoint. It
imports `instrument_contracts()` for the expected types/labels and rejects every label in Task 3's
exported `FORBIDDEN_IDENTIFIER_LABELS`; it does not maintain a second metric registry.

```python
OPENMETRICS_SAMPLE_RE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{([^}]*)\})?\s")
OPENMETRICS_LABEL_RE = re.compile(r'(?:^|,)([A-Za-z_][A-Za-z0-9_]*)="')


def openmetrics_label_sets(text: str) -> dict[str, set[frozenset[str]]]:
    samples: dict[str, set[frozenset[str]]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = OPENMETRICS_SAMPLE_RE.match(line)
        if match is None:
            raise AssertionError(f"invalid OpenMetrics sample: {line[:120]!r}")
        labels = frozenset(OPENMETRICS_LABEL_RE.findall(match.group(2) or ""))
        samples.setdefault(match.group(1), set()).add(labels)
    return samples


def required_sample_names() -> frozenset[str]:
    names: set[str] = set()
    for name, (kind, _unit, _labels) in instrument_contracts().items():
        if kind == "histogram":
            names.update((f"{name}_bucket", f"{name}_count", f"{name}_sum"))
        else:
            names.add(name)
    return frozenset(names)


async def wait_for_required_metrics(
    stack: Stack, *, timeout: float = 75.0
) -> tuple[str, dict[str, set[frozenset[str]]]]:
    deadline = asyncio.get_running_loop().time() + timeout
    required = required_sample_names()
    latest = ""
    while asyncio.get_running_loop().time() < deadline:
        latest = await stack.collector_metrics()
        parsed = openmetrics_label_sets(latest)
        if required <= parsed.keys():
            return latest, parsed
        await asyncio.sleep(0.5)
    missing = sorted(required - openmetrics_label_sets(latest).keys())
    raise AssertionError(f"Collector did not expose required metrics: {missing}")


@pytest.mark.integration
async def test_collector_exposes_exact_metric_names_and_label_sets(
    observed_stack: Stack,
) -> None:
    await observed_stack.emit_metric_fixtures()
    raw, samples = await wait_for_required_metrics(observed_stack)
    for name, (kind, _unit, contract_labels) in instrument_contracts().items():
        expected = frozenset(contract_labels)
        if kind == "histogram":
            assert samples[f"{name}_bucket"] == {expected | {"le"}}
            assert samples[f"{name}_count"] == {expected}
            assert samples[f"{name}_sum"] == {expected}
        else:
            assert samples[name] == {expected}
        emitted_names = (
            {f"{name}_bucket", f"{name}_count", f"{name}_sum"} if kind == "histogram" else {name}
        )
        for emitted in emitted_names:
            assert all(
                FORBIDDEN_IDENTIFIER_LABELS.isdisjoint(label_set) for label_set in samples[emitted]
            )
        if kind == "counter":
            assert f"{name}_total" not in samples
    assert "agent_runs_total" in raw
    assert "agent_runs_total_total" not in raw
```

- [ ] **Step 5: Add JSONL checks for every application service**

```python
@pytest.mark.integration
@pytest.mark.parametrize(
    "service",
    [
        "web",
        "api",
        "workflow-worker",
        "agent-worker",
        "tool-worker",
        "event-worker",
        "sandbox-runner",
    ],
)
async def test_application_stdout_is_schema_v1_jsonl(observed_stack: Stack, service: str) -> None:
    lines = await observed_stack.logs(service)
    assert lines
    for line in lines:
        record = json.loads(line)
        assert record["schema_version"] == 1
        assert record["service"] == service
        assert set(("timestamp", "level", "environment", "event", "logger")) <= record.keys()
```

`docker compose logs --no-log-prefix` returns only container output. Do not filter, regex-ignore, or
waive any returned application line.

- [ ] **Step 6: Prove profile absence and Collector failure do not stop product work**

```python
@pytest.mark.integration
async def test_product_completes_work_with_profile_absent(base_stack: Stack) -> None:
    assert "otel-collector" not in await base_stack.services()
    assert (await base_stack.run_fake_task(timeout=180))["state"] == "completed"


@pytest.mark.integration
async def test_product_completes_work_while_collector_is_stopped(observed_stack: Stack) -> None:
    await observed_stack.stop("otel-collector")
    started = time.monotonic()
    assert (await observed_stack.run_fake_task(timeout=180))["state"] == "completed"
    assert time.monotonic() - started < 180
    await observed_stack.start("otel-collector")
    await observed_stack.emit_metric_fixtures()
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        if "agent_runs_total" in await observed_stack.collector_metrics():
            break
        await asyncio.sleep(0.5)
    else:
        raise AssertionError("telemetry export did not recover")
```

Do not make task completion depend on spans/metrics produced during the outage. Task 2 owns the
safe aggregated exporter-failure event assertion; this live test owns product availability and
recovery.

- [ ] **Step 7: Run scenario RED against the existing stack**

```bash
uv run pytest -m integration tests/integration/test_phase10_telemetry.py -q
```

Expected: FAIL until the profile and complete instrumentation are running.

- [ ] **Step 8: Add isolated Make/CI execution with diagnostic cleanup**

Add:

```make
test-telemetry-base: master-key
	@set -eu; \
	project=jhin-phase10-base; \
	cleanup() { $(COMPOSE_DEV) -p $$project down -v --remove-orphans >/dev/null 2>&1 || true; }; \
	trap cleanup EXIT INT TERM; \
	$(COMPOSE_DEV) -p $$project --profile build build sandbox-image; \
	OTEL_EXPORTER_OTLP_ENDPOINT= $(COMPOSE_DEV) -p $$project up -d --build --wait --wait-timeout 240; \
	JHIN_TEST_COMPOSE_PROJECT=$$project JHIN_TELEMETRY_MODE=base \
	  PHASE10_SOCKET_MODE=$(PHASE10_SOCKET_MODE) \
	  uv run pytest -m integration \
	  tests/integration/test_phase10_telemetry.py::test_product_completes_work_with_profile_absent -v; \
	$(COMPOSE_DEV) -p $$project down -v --remove-orphans

test-telemetry-observed: master-key
	@set -eu; \
	project=jhin-phase10-observed; \
	cleanup() { $(COMPOSE_DEV) -p $$project --profile observability down -v --remove-orphans >/dev/null 2>&1 || true; }; \
	trap cleanup EXIT INT TERM; \
	$(COMPOSE_DEV) -p $$project --profile build build sandbox-image; \
	OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
	OTEL_EXPORTER_OTLP_INSECURE=true OTEL_TRACES_SAMPLER=always_on \
	  $(COMPOSE_DEV) -p $$project --profile observability up -d --build --wait --wait-timeout 240; \
	JHIN_TEST_COMPOSE_PROJECT=$$project JHIN_TELEMETRY_MODE=observed \
	  PHASE10_SOCKET_MODE=$(PHASE10_SOCKET_MODE) \
	  uv run pytest -m integration tests/integration/test_phase10_telemetry.py \
	  -k 'not profile_absent' -v; \
	$(COMPOSE_DEV) -p $$project --profile observability down -v --remove-orphans

test-telemetry-integration:
	$(MAKE) test-telemetry-base
	$(MAKE) test-telemetry-observed
```

Each invocation uses one explicit project consistently through sandbox-image build, up, test, and down. `test-telemetry-integration` runs the base invocation to completion before starting the observed invocation, and each trap removes that project and its volumes on every failure or signal.

Add a `telemetry` CI job after Python/web unit jobs. It generates a test master key, derives the
rootful socket GID, starts uniquely named Compose projects with `compose.yaml`, `compose.dev.yaml`,
and `compose.rootful.yaml`, waits with bounded polling for health, runs the telemetry integration
file, and in `if: always()` uploads only artifacts that first pass the canary scanner. Cleanup uses
that same three-file vector with `down -v --remove-orphans`. Do not upload raw logs before the
scanner passes. Normal CI uses only fake providers.

```yaml
  telemetry:
    name: Telemetry profile acceptance
    needs: [python, web]
    runs-on: ubuntu-latest
    timeout-minutes: 35
    env:
      MASTER_KEY_FILE_HOST: ./secrets/dev/jhin_master_key
    steps:
      - uses: actions/checkout@v4
      - name: Derive exact rootful Docker socket group
        run: |
          test -S /var/run/docker.sock
          socket_gid="$(stat -c '%g' /var/run/docker.sock)"
          test "$socket_gid" -gt 0
          echo "SANDBOX_DOCKER_GID=$socket_gid" >> "$GITHUB_ENV"
          echo "PHASE10_SOCKET_MODE=rootful" >> "$GITHUB_ENV"
      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
      - name: Sync workspace
        run: uv sync --frozen --all-packages
      - name: Create isolated master key
        run: uv run python scripts/generate_master_key.py secrets/dev/jhin_master_key
      - name: Create private artifact canary manifest
        run: >-
          uv run python scripts/phase10_artifact.py canaries
          --destination "$RUNNER_TEMP/phase10-canaries.json"
      - name: Prove rootless render with GID absent
        run: |
          env -u SANDBOX_DOCKER_GID PHASE10_SOCKET_MODE=rootless \
            PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest \
            tests/test_phase10_tool_worker_compose.py \
            tests/test_phase10_observability_compose.py -q
          env -u SANDBOX_DOCKER_GID PHASE10_SOCKET_MODE=rootless \
            PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python \
            scripts/assert_phase10_tool_worker_compose.py --mode rootless
          env -u SANDBOX_DOCKER_GID PHASE10_SOCKET_MODE=rootless \
            PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python \
            scripts/assert_phase10_observability_compose.py --mode rootless
      - name: Build sandbox image for base project
        run: >-
          docker compose -p jhin-phase10-base -f compose.yaml -f compose.dev.yaml
          -f compose.rootful.yaml --profile build build sandbox-image
      - name: Start base stack without monitoring
        run: >-
          env OTEL_EXPORTER_OTLP_ENDPOINT=
          docker compose -p jhin-phase10-base -f compose.yaml -f compose.dev.yaml
          -f compose.rootful.yaml up -d --build --wait --wait-timeout 240
      - name: Test profile absence
        env:
          JHIN_TEST_COMPOSE_PROJECT: jhin-phase10-base
          JHIN_TELEMETRY_MODE: base
        run: >-
          uv run pytest -m integration
          tests/integration/test_phase10_telemetry.py::test_product_completes_work_with_profile_absent -v
      - name: Stop base stack before observed ports bind
        if: always()
        run: >-
          docker compose -p jhin-phase10-base -f compose.yaml -f compose.dev.yaml
          -f compose.rootful.yaml down -v --remove-orphans
      - name: Build sandbox image for observed project
        run: >-
          docker compose -p jhin-phase10-observed
          -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml
          --profile build build sandbox-image
      - name: Start observed stack with monitoring
        env:
          OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
          OTEL_EXPORTER_OTLP_INSECURE: "true"
          OTEL_TRACES_SAMPLER: always_on
        run: >-
          docker compose -p jhin-phase10-observed
          -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml
          --profile observability up -d --build --wait --wait-timeout 240
      - name: Run telemetry acceptance
        env:
          JHIN_TEST_COMPOSE_PROJECT: jhin-phase10-observed
          JHIN_TELEMETRY_MODE: observed
          JHIN_TELEMETRY_CANARY_FILE: ${{ runner.temp }}/phase10-canaries.json
        run: >-
          uv run pytest -m integration tests/integration/test_phase10_telemetry.py
          -k 'not profile_absent' -v
      - name: Capture closed-schema status
        id: capture_status
        if: failure()
        run: >-
          uv run python scripts/phase10_artifact.py capture
          --project jhin-phase10-base --project jhin-phase10-observed
          --socket-mode rootful
          --destination telemetry-compose-status.json
          --canary-file "$RUNNER_TEMP/phase10-canaries.json"
      - name: Validate status schema and canaries
        id: validate_status
        if: failure() && steps.capture_status.outcome == 'success'
        run: >-
          uv run python scripts/phase10_artifact.py validate
          --input telemetry-compose-status.json
          --canary-file "$RUNNER_TEMP/phase10-canaries.json"
      - name: Upload validated safe status
        if: failure() && steps.validate_status.outcome == 'success'
        uses: actions/upload-artifact@v4
        with:
          name: telemetry-compose-status
          path: telemetry-compose-status.json
      - name: Tear down both isolated stacks
        if: always()
        run: |
          docker compose -p jhin-phase10-base -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml down -v --remove-orphans
          docker compose -p jhin-phase10-observed -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml --profile observability down -v --remove-orphans
```

The earlier private-manifest step writes the generated canary registry to
`$RUNNER_TEMP/phase10-canaries.json` with mode `0600`; that file is never uploaded. The validator
accepts only an object with `schema_version=1`, `kind="compose_status"`, and a `services` array of
closed-schema rows; each row has the exact keys
`project,service,state,health`. Project, service, state, and health come from closed enums; unknown
values reject the artifact. It scans raw, percent-encoded, base64, and URL-safe-base64 canary forms
before the atomic artifact write and again in the separate validation step. `tests/test_phase10_artifact.py`
parses the workflow and asserts every `actions/upload-artifact` step is conditioned on a named,
successful schema/canary validation step. Raw service logs, traces, metric snapshots, image names,
commands, ports, mounts, and environments are never uploaded.

Write these artifact tests first:

```python
import json
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from scripts.phase10_artifact import (
    ArtifactRejected,
    CANARY_KINDS,
    capture_status,
    write_canary_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def test_canary_manifest_is_private_and_has_the_exact_sink_registry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canaries.json"
    write_canary_manifest(path)
    document = json.loads(path.read_text())
    assert set(document) == {"schema_version", "kind", "values"}
    assert document["schema_version"] == 1
    assert document["kind"] == "telemetry_canaries"
    assert tuple(sorted(document["values"])) == tuple(sorted(CANARY_KINDS))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ArtifactRejected, match="already exists"):
        write_canary_manifest(path)


@pytest.mark.parametrize("as_list", [False, True])
def test_capture_projects_only_closed_compose_status(
    tmp_path: Path,
    as_list: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_DOCKER_GID", raising=False)
    manifest = tmp_path / "canaries.json"
    write_canary_manifest(manifest)
    source = {
        "Project": "jhin-phase10-observed",
        "Service": "api",
        "State": "running",
        "Health": "healthy",
        "Command": "must-not-be-copied",
        "Environment": ["SECRET=must-not-be-copied"],
    }
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps([source] if as_list else source),
        stderr="",
    )
    destination = tmp_path / "status.json"
    invocations: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        invocations.append((argv, kwargs))
        return completed

    capture_status(
        ["jhin-phase10-observed"],
        socket_mode="rootless",
        destination=destination,
        canary_file=manifest,
        runner=runner,
    )
    assert invocations[0][0].count("compose.rootless.yaml") == 1
    assert "compose.rootful.yaml" not in invocations[0][0]
    assert "SANDBOX_DOCKER_GID" not in cast(Mapping[str, str], invocations[0][1]["env"])
    assert json.loads(destination.read_text()) == {
        "schema_version": 1,
        "kind": "compose_status",
        "services": [
            {
                "project": "jhin-phase10-observed",
                "service": "api",
                "state": "running",
                "health": "healthy",
            }
        ],
    }


def test_every_ci_upload_depends_on_a_schema_and_canary_validator() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    step_blocks = re.split(r"(?m)(?=^      - (?:name|uses):)", workflow)
    by_id = {
        match.group(1): block
        for block in step_blocks
        if (match := re.search(r"(?m)^        id: ([a-z0-9_]+)$", block))
    }
    uploads = [block for block in step_blocks if "uses: actions/upload-artifact@" in block]
    assert uploads
    for upload in uploads:
        condition = re.search(
            r"(?m)^        if: .*steps\.([a-z0-9_]+)\.outcome == 'success'.*$",
            upload,
        )
        assert condition is not None
        validator = by_id[condition.group(1)]
        assert "phase10_artifact.py validate" in validator
        assert "--canary-file" in validator
        validated = re.search(r"--input\s+([^\s]+)", validator)
        uploaded = re.search(r"(?m)^\s+path:\s+([^\s]+)$", upload)
        assert validated is not None and uploaded is not None
        assert validated.group(1).strip('"') == uploaded.group(1).strip('"')
```

Run `uv run pytest tests/test_phase10_artifact.py -q` and confirm RED because the closed artifact module does not exist.

Implement that contract, rather than an open-ended scanner:

```python
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

CANARY_KINDS = (
    "prompt",
    "completion",
    "connector_response",
    "connector_error",
    "authorization",
    "cookie",
    "api_key",
    "private_key",
    "dsn_user",
    "dsn_password",
    "webhook_secret",
    "webhook_body",
    "tool_output",
    "sandbox_secret_env",
)
PROJECTS = frozenset({"jhin-phase10-base", "jhin-phase10-observed"})
SERVICES = frozenset(
    {
        "web",
        "api",
        "workflow-worker",
        "agent-worker",
        "tool-worker",
        "event-worker",
        "sandbox-runner",
        "postgres",
        "nats",
        "temporal",
        "temporal-ui",
        "sandbox-image",
        "fake-provider",
        "fake-github",
        "fake-linear",
        "fake-vercel",
        "fake-supabase",
        "fake-supabase-db",
        "otel-collector",
        "prometheus",
        "tempo",
        "grafana",
    }
)
STATES = frozenset({"running", "exited", "paused", "restarting", "created"})
HEALTH = frozenset({"healthy", "unhealthy", "starting", "none"})


class ArtifactRejected(RuntimeError):
    """Raised before a telemetry diagnostic artifact can be written."""


def write_canary_manifest(destination: Path) -> None:
    if destination.exists():
        raise ArtifactRejected("canary manifest already exists")
    values = {kind: f"phase10-{kind}-{secrets.token_urlsafe(24)}" for kind in CANARY_KINDS}
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(
            {"schema_version": 1, "kind": "telemetry_canaries", "values": values},
            stream,
            sort_keys=True,
        )
        stream.write("\n")


def read_canary_manifest(path: Path) -> tuple[str, ...]:
    document = json.loads(path.read_text())
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "kind", "values"}
        or document["schema_version"] != 1
        or document["kind"] != "telemetry_canaries"
        or not isinstance(document["values"], dict)
        or tuple(document["values"].keys()) != tuple(sorted(CANARY_KINDS))
        or not all(isinstance(value, str) and value for value in document["values"].values())
    ):
        raise ArtifactRejected("canary manifest schema mismatch")
    return tuple(document["values"][kind] for kind in CANARY_KINDS)


def encoded_canaries(values: Sequence[str]) -> frozenset[str]:
    output: set[str] = set()
    for value in values:
        raw = value.encode()
        output.update(
            (
                value,
                quote(value, safe=""),
                base64.b64encode(raw).decode(),
                base64.urlsafe_b64encode(raw).decode(),
            )
        )
    return frozenset(output)


def validate_document(document: object, canaries: Sequence[str]) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {"schema_version", "kind", "services"}:
        raise ArtifactRejected("top-level schema mismatch")
    if document["schema_version"] != 1 or document["kind"] != "compose_status":
        raise ArtifactRejected("schema discriminator mismatch")
    rows = document["services"]
    if not isinstance(rows, list):
        raise ArtifactRejected("services must be a list")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"project", "service", "state", "health"}:
            raise ArtifactRejected("service row schema mismatch")
        if (
            row["project"] not in PROJECTS
            or row["service"] not in SERVICES
            or row["state"] not in STATES
            or row["health"] not in HEALTH
        ):
            raise ArtifactRejected("unregistered status value")
    rendered = json.dumps(document, sort_keys=True, separators=(",", ":"))
    if any(value and value in rendered for value in encoded_canaries(canaries)):
        raise ArtifactRejected("sensitive telemetry canary in artifact")
    return cast(dict[str, object], document)


def write_validated(document: object, destination: Path, canaries: Sequence[str]) -> None:
    safe = validate_document(document, canaries)
    if destination.exists():
        raise ArtifactRejected("artifact destination already exists")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, destination)


def parse_compose_status(project: str, stdout: str) -> list[dict[str, str]]:
    if project not in PROJECTS:
        raise ArtifactRejected("unregistered Compose project")
    try:
        decoded = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ArtifactRejected("Compose status is not JSON") from exc
    source_rows = decoded if isinstance(decoded, list) else [decoded]
    rows: list[dict[str, str]] = []
    for source in source_rows:
        if not isinstance(source, Mapping):
            raise ArtifactRejected("Compose status row is not an object")
        if source.get("Project", project) != project:
            raise ArtifactRejected("Compose status project mismatch")
        service = source.get("Service")
        state = source.get("State")
        health_value = source.get("Health", "")
        health = "none" if health_value in {"", None} else health_value
        row = {
            "project": project,
            "service": service,
            "state": state,
            "health": health,
        }
        if not all(isinstance(value, str) for value in row.values()):
            raise ArtifactRejected("Compose status value is not a string")
        rows.append(cast(dict[str, str], row))
    return rows


def capture_status(
    projects: Sequence[str],
    *,
    socket_mode: Literal["rootful", "rootless"],
    destination: Path,
    canary_file: Path,
    runner: CommandRunner = subprocess.run,
) -> None:
    if not projects or len(set(projects)) != len(projects):
        raise ArtifactRejected("projects must be unique and non-empty")
    canaries = read_canary_manifest(canary_file)
    rows: list[dict[str, str]] = []
    environment = os.environ.copy()
    if socket_mode == "rootless":
        environment.pop("SANDBOX_DOCKER_GID", None)
    else:
        raw_gid = environment.get("SANDBOX_DOCKER_GID", "")
        if not raw_gid.isdecimal() or int(raw_gid) == 0:
            raise ArtifactRejected("rootful capture requires nonzero SANDBOX_DOCKER_GID")
    for project in projects:
        if project not in PROJECTS:
            raise ArtifactRejected("unregistered Compose project")
        completed = runner(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                "compose.yaml",
                "-f",
                "compose.dev.yaml",
                "-f",
                f"compose.{socket_mode}.yaml",
                "ps",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=environment,
            shell=False,
            capture_output=True,
            text=True,
            check=True,
        )
        rows.extend(parse_compose_status(project, completed.stdout))
    write_validated(
        {"schema_version": 1, "kind": "compose_status", "services": rows},
        destination,
        canaries,
    )


def validate_file(input_path: Path, canary_file: Path) -> None:
    validate_document(json.loads(input_path.read_text()), read_canary_manifest(canary_file))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    canaries = subparsers.add_parser("canaries")
    canaries.add_argument("--destination", required=True, type=Path)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--project", action="append", required=True)
    capture.add_argument("--socket-mode", choices=("rootful", "rootless"), required=True)
    capture.add_argument("--destination", required=True, type=Path)
    capture.add_argument("--canary-file", required=True, type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--input", required=True, type=Path)
    validate.add_argument("--canary-file", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "canaries":
        write_canary_manifest(args.destination)
    elif args.command == "capture":
        capture_status(
            args.project,
            socket_mode=args.socket_mode,
            destination=args.destination,
            canary_file=args.canary_file,
        )
    else:
        validate_file(args.input, args.canary_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

The `canaries` subcommand produces the exact closed schema above in a new `0600` file. `Stack.drive_telemetry_canaries` consumes that same file when `JHIN_TELEMETRY_CANARY_FILE` is set, so CI validates the canaries actually injected rather than an unrelated set. RED/GREEN is `uv run pytest tests/test_phase10_artifact.py -q`; tests inject the command runner and cover array/single-row Compose JSON plus every rejection branch.

- [ ] **Step 9: Write the operator runbook with exact commands and trust boundaries**

`docs/operations/telemetry.md` must state:

- `make dev` runs with an empty OTLP endpoint and no monitoring services;
- `make observability-up` enables the profile and exports to the internal Collector;
- Grafana/Prometheus/Tempo dev URLs are loopback only; no remote exposure is supported without separate admin authentication/private networking;
- external Collector configuration requires endpoint/TLS CA/client certificate/private-key file settings, and production private keys are file paths rather than inline values;
- trace sampling modes/ratio and their bounds;
- Prometheus `15d`, Tempo `72h`, and Docker `20m × 5` retention;
- monitoring volumes are replaceable diagnostics and not part of product backup;
- dashboards/config are recreated from `ops/observability`;
- no raw logs/traces/SQL/prompts/payloads are shown in Jhin UI;
- how to disable export by clearing `OTEL_EXPORTER_OTLP_ENDPOINT` and restart product services without deleting product data;
- how to query one trace by operator-known trace ID and how to validate all service JSONL without exposing it publicly.

- [ ] **Step 10: Run integration and security gates**

```bash
uv run pytest tests/test_phase10_artifact.py -q
uv run ruff check scripts/phase10_artifact.py tests/integration/conftest.py \
  tests/integration/emit_phase10_metrics.py tests/integration/test_phase10_telemetry.py \
  tests/test_phase10_artifact.py
uv run mypy scripts/phase10_artifact.py tests/integration/conftest.py \
  tests/integration/emit_phase10_metrics.py tests/integration/test_phase10_telemetry.py \
  tests/test_phase10_artifact.py
make test-telemetry-integration
```

Expected: all checks pass; the rootful/rootless-selected cleanup vector used by each target returns
an empty `ps -q` for both explicit projects because teardown ran.

- [ ] **Step 11: Review and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

#### Final executable contract for Task 11


After corrected Tasks 1-10 exist, replace Task 11 from its `Files` block through its staging/
commit block with this section. Delete the draft's fixed project, raw Compose lifecycle, parallel
socket selector, rootful-only CI job, shared-name trace test, gauge-overwriting fixture, predictable
artifact temporary path, and post-cleanup status capture.

Task 11 consumes the accepted Phase 10 lifecycle rooted at exact predecessor tip
`ee66c588014acf8e448352a7e5e458aca63d37fe`; it may not silently fall back to an earlier range.

### 18.1 Add exactly two scenarios to the sole leased authority

Do not add `StackContract`, `resolve_stack_contract`, a `jhin` fallback, caller-supplied project/
files/profile/port/socket, or a second `PHASE10_SOCKET_MODE`. Keep integration conftest strict and
wrap only its exact leased `ComposeAuthority` in a small typed `Stack` facade.

After Task 10 adds `LiveScenario.observability: bool = False`, Task 11 adds exactly:

~~~python
"telemetry-base": LiveScenario(
    nodes=(
        "tests/integration/test_phase10_telemetry.py::"
        "test_product_completes_work_with_profile_absent",
    ),
    expected_tests=1,
    observability=False,
),
"telemetry-observed": LiveScenario(
    nodes=(
        "tests/integration/test_phase10_telemetry.py::test_observed_runtime_contract",
        "tests/integration/test_phase10_telemetry.py::"
        "test_connected_trace_metrics_canaries_and_replay_contract",
        "tests/integration/test_phase10_telemetry.py::"
        "test_application_stdout_is_schema_v1_jsonl",
        "tests/integration/test_phase10_telemetry.py::"
        "test_product_completes_while_collector_is_stopped_and_recovers",
        "tests/integration/test_phase10_telemetry.py::"
        "test_trigger_started_commit_is_reconciled_once",
        "tests/integration/test_phase10_telemetry.py::"
        "test_sandbox_terminal_reads_and_cancel_do_not_recount",
    ),
    # 1 runtime + 1 connected + 7 JSONL + 1 outage + 1 trigger + 1 sandbox.
    expected_tests=12,
    observability=True,
),
~~~

The harness test compares exact node IDs and exact positive collection counts. Never use a `-k`
filter: skips, xfails, deselection, or count drift fail. Base and observed run as two sequential,
unique one-shot invocations. Each invocation owns build/up/test, pre-cleanup failure capture,
down, exact second-pass resources, process groups, lease/barriers, full-ID sandbox/workspace-init
cleanup, and selected image absence. Scenario success is returned only after all cleanup proves
absence.

### 18.2 Expose only typed telemetry operations from the leased authority

`Stack` stores only `authority: ComposeAuthority`. Base/observed fixtures assert the immutable
lease selection and use only `authority.published_ports` and its sanitized child environment.
They guess no conventional port or project.

Add narrow authority operations for:

- unfiltered `logs --no-color --no-log-prefix <application-service>` with both streams;
- raw Collector exposition via exact in-container
  `exec -T otel-collector /bin/busybox wget -q -O - http://127.0.0.1:9464/metrics`;
- Tempo, Prometheus, and Grafana requests through lease-resolved dynamic loopback ports;
- exact rendered/runtime service, image, config, security, health, mount, log, and network
  inspection;
- bounded stop/start/readiness of exactly `otel-collector`;
- a fixed metric fixture inside `api` through typed `exec -T`, never `compose cp`;
- exact existing sandbox submit/status/log/cancel/terminal-safe operations; and
- raw `ps --all --format json` used only by in-owner failure projection.

Every method derives project, files, profile, socket, environment, allowlist, and timeout from the
lease. Generic fixture Compose remains narrow and rejects lifecycle, raw `exec`/logs,
`-p/-f/--profile`, and socket/context/host overrides. Direct sandbox work reuses the accepted
ledgered operations so job/workspace intent is durable before the runner request and initializer/
volume cleanup remains exact. Unit tests inject a recorder, prove every argv/environment, and
reject ambient `COMPOSE_*`, `DOCKER_*`, `OTEL_*`, project, port, socket, or GID substitution
without Docker.

### 18.3 Make the first live monitoring acceptance exhaustive

`test_observed_runtime_contract` must prove in rootful and rootless:

- exact mode/profile service inventory, including the adapter only in rootless and exactly four
  backends only when observed;
- all four rendered pinned `BASE_IMAGE` args, unique-project image tags, resolved image IDs, and
  running containers using those exact IDs;
- reviewed non-root identities, effective cap drop/no-new-privileges, finite process/memory
  bounds, supported read-only/tmpfs choices, exact `20m x 5` JSON-file logs, reviewed read-only
  binds, and absence of product secrets/sockets;
- Collector parse and exact gRPC/memory/batch/queue/retry/no-log/no-suffix configuration;
- Prometheus one-target/15d, Tempo local single-tenant/72h, Grafana disabled anonymous/sign-up,
  exact provisioned datasources, and byte-identical generated dashboard;
- every exact local ready endpoint after Compose readiness; and
- stop Collector, complete product work, start the same Collector, reassert readiness, emit and
  observe a fresh metric within finite monotonic deadlines.

Inspect real containers/endpoints/config behavior. Rendered YAML, a failure artifact, or a static
file is not live proof. Runtime network membership must equal section 17.1 exactly and reject any
extra/missing edge.

### 18.4 Assert the connected parent graph, not a shared bag of names

Query the complete Tempo trace and index trace ID, span ID, parent span ID, name, kind, and closed
attributes. Require this actual edge graph, disambiguating repeated names with the closed route,
stream, consumer, activity, and operation attributes:

~~~text
API webhook http.server.request
  -> db.operation (at least one real asyncpg operation)
  -> nats.publish (INGRESS)
     -> nats.consume (INGRESS, event-worker-ingress)
        -> nats.publish (EVENTS)
           -> nats.consume (EVENTS, event-worker)
              -> trigger.dispatch
                 -> temporal.start_workflow
                    -> temporal.activity.prepare_triggered_task
                    -> temporal.activity.resolve_advertised_tools
                    -> temporal.activity.reason_agent_step
                       -> agent.reason_step
                          -> model.request
                    -> temporal.activity.execute_bound_tool
                       -> tool.gateway.execute
                          -> connector.http
                       -> tool.gateway.execute
                          -> sandbox.client (submit)
                             -> matching sandbox.server (submit)
                                -> sandbox.job.lifecycle
                    -> temporal.activity.commit_agent_step
                    -> temporal.activity.cleanup_run_workspace
                    -> temporal.activity.finalize_run_projection
~~~

The connector span is a child of its active API/tool span; each sandbox server is the exact child
of the matching client; lifecycle is the submit-server child. Every span uses only registered
attributes/events/status. No `temporal.workflow.*` application span exists. The DB canary uses the
real API connection create/verify flow, a rejected fake-Supabase database endpoint, and the known
traceparent; it yields closed `connector.database`/`db.operation` beneath an API server span and
never copies a DSN file or exports SQL, bind data, DSN, URL, host, exception, or canary.

### 18.5 Use one exact canary manifest and scan every complete sink

The private manifest contains exactly these fifteen kinds:

~~~text
prompt, completion, connector_response, connector_error, authorization,
cookie, url, api_key, private_key, dsn_user, dsn_password, webhook_secret,
webhook_body, tool_output, sandbox_secret_env
~~~

Use the same validated manifest for injection and validation. GitHub connection creation uses
`auth_type="pat"`. Task 11 owns its builders while preserving the Phase 7 Linear/agent/grant/
trigger semantics. Apply the same known traceparent to successful work, rejected login/webhook,
failed connector verification, failing DB verification, and a dynamic-web request whose query,
Authorization header, and Cookie carry their canaries.

Before selective parsing, scan:

1. complete unfiltered stdout and stderr of all seven application services;
2. the complete Tempo response, including resources, spans, events, links, status, and values;
3. the complete raw Collector Prometheus response, including labels and exemplars.

For every canary reject raw, percent-encoded, standard-base64, and URL-safe-base64 forms, padded
and unpadded, in raw bytes/text and deterministic decoded serialization. Backend logs are
diagnostic and are not application JSON-v1 evidence.

### 18.6 Prove all sixteen metric contracts without replacing real gauges

The fixed in-container fixture emits only counters/histograms needed to expose missing
instruments. It never registers or replaces a gauge. It receives only closed stdin if needed,
owns runtime flush/shutdown, creates no host payload/temp file, and exits nonzero on failure.

Parse Compose object/array/NDJSON separately from OpenMetrics. For all sixteen instruments:

- counters use the exact registered name, never a translated `_total` or double suffix;
- histogram buckets add only `le`, while count/sum use exactly the contract labels;
- each series has exactly its Task 3 labels, values from closed registries, and no identifier
  label; and
- every expected kind is actually present.

The real event worker exports exactly the two lag series
`(INGRESS,event-worker-ingress)` and `(EVENTS,event-worker)` with no third/stale or workspace/
subject/header dimension. Seed real connections and wait for real `connector_health` and positive
`connector_connections`; never fabricate those gauges. Poll with finite monotonic deadlines that
account for the configured metric interval.

### 18.7 Prove trigger, reasoning/tool, and sandbox once contracts live

Consume Task 7's test-only post-started-commit/pre-dispatch callback against leased Postgres and
Temporal: park after the authoritative `started` commit, cancel that invocation, redeliver through
a fresh matcher, and prove one authoritative started row, one duplicate history row, one
deterministic workflow ID, at most one Temporal workflow, a linked/completed task, and no lost
work. The production callback defaults to no-op; Task 11 adds no control route and edits no Task 7
source.

For reasoning/tool retry, use accepted Phase 10 crash barriers and snapshot series before the
barrier, after first committed transition, and after retry/reload. Provider attempts count only
real calls; committed reasoning replay does not duplicate token/cost; durable terminal tool reload
does not duplicate terminal/failure counters. Assert exact deltas.

For sandbox once, submit one completed `network_policy=none` job, wait for one counter and one
histogram-count delta, snapshot full terminal state, then repeat status, logs, cancel, and safe
terminal-finalize paths. State/timestamps remain unchanged and both subsequent metric deltas are
zero. This is separate from the connected sandbox trace.

### 18.8 Capture closed failure status inside the owner before cleanup

Remove Docker execution from `scripts/phase10_artifact.py` and CI. On child failure,
`_execute_selected_one_shot` asks its exact live authority for `ps --all --format json`, projects
and validates the safe document, atomically writes it, then always performs exhaustive cleanup.
Preserve the primary test error and group capture/cleanup failures without masking it.

The exact schema is:

~~~json
{
  "schema_version": 1,
  "kind": "compose_status",
  "scenario": "telemetry-base",
  "socket_mode": "rootful | rootless",
  "observability": false,
  "project": "jhin-p10-<8..16 lowercase hex>",
  "services": [
    {"service": "<selected closed service>", "state": "<closed state>",
     "health": "<closed health>"}
  ]
}
~~~

The scenario/selection discriminant permits exactly these two pairs and no cross-product:

~~~text
telemetry-base     -> observability: false
telemetry-observed -> observability: true
~~~

The observed document has the same schema but uses the second exact pair. Unit tests accept both
valid pairs and reject `telemetry-base` with `true` and `telemetry-observed` with `false` before
writing or upload. Services are unique, sorted, and a subset of the exact selected inventory.
States are only
`created|restarting|running|removing|paused|exited|dead`; health is only
`none|starting|healthy|unhealthy`, with absent/empty normalized to `none`. Accept Compose object,
array, or NDJSON; reject blank/malformed/oversize, duplicate/unknown service, foreign project,
unknown state/health, key drift, mode/profile mismatch, or canary content. Never include logs,
traces, metrics, images, commands, ports, mounts, IDs, environment, or raw rows.

The artifact service registries exactly mirror authority inventories:

~~~text
rootful base = agent-worker, api, event-worker, fake-github, fake-linear,
               fake-provider, fake-supabase, fake-supabase-db, fake-vercel,
               nats, postgres, sandbox-runner, temporal, temporal-ui,
               tool-worker, web, workflow-worker
rootless base = rootful base + rootless-docker-transport
observed      = selected base + grafana, otel-collector, prometheus, tempo
~~~

`sandbox-image` is build-only and forbidden from status. A pure equality test binds every
mode/profile registry to `ComposeAuthority.expected_services`.

The outer interface is exact:

~~~text
JHIN_PHASE10_SAFE_ARTIFACT_DIR     owner-only outer-harness directory
JHIN_TELEMETRY_CANARY_FILE         owner-only child/validator manifest, never Compose
telemetry-compose-status-<socket_mode>-<scenario>.json
~~~

The first variable never enters lease/child/container environment. The second is added only to the
sanitized pytest child and validator. CLI operations are only `canaries --destination` and
`validate --input --canary-file`; projection is an in-process pure owner function.

The manifest has exact closed schema
`{"schema_version":1,"kind":"telemetry_canaries","values":{...}}`. Create its verified
owner-only `0700` directory and new regular `0600` file with
`O_CREAT|O_EXCL|O_NOFOLLOW`; reject wrong owner/mode/type, links, duplicate/unknown/missing kinds,
empty/oversize values, and oversize documents. Never upload it.

Safe status writing uses a unique new `0600` regular temporary file in the same verified
directory, complete write, file fsync, validation of actual bytes, absent destination, atomic
install, and parent fsync. On failure remove only the exact owned inode. Cover hostile parent/
manifest/destination links, preexisting files, short writes, fsync/replace failure, parser forms,
duplicates, canary encodings, and cleanup after every exception.

### 18.9 Run both scenarios in both existing CI authorities

The exact Make interface is:

~~~make
test-telemetry-base: ## Run profile-absent telemetry acceptance through one lease
	$(PHASE10_HARNESS) run --mode $(PHASE10_MODE) --scenario telemetry-base

test-telemetry-observed: ## Run observed telemetry acceptance through one lease
	$(PHASE10_HARNESS) run --mode $(PHASE10_MODE) --scenario telemetry-observed

test-telemetry-integration: ## Run base then observed as separate one-shots
	$(MAKE) test-telemetry-base PHASE10_MODE="$(PHASE10_MODE)"
	$(MAKE) test-telemetry-observed PHASE10_MODE="$(PHASE10_MODE)"
~~~

Add all three to `.PHONY`. They do not depend on `master-key`; the authority owns its private
nonprinting key. Add `make test-telemetry-integration` to the existing
`phase10-rootful-live` and `phase10-rootless-live` jobs. Do not create a third telemetry job. The
rootless invocation remains in `/tmp/jhin-phase10-rootless-workspace`, runs as UID 10001 against
the already verified rootless socket, carries the private uv/runtime environment, and omits
`SANDBOX_DOCKER_GID`.

In each existing live job, name the one step that invokes this target exactly
`Telemetry base and observed acceptance`. The step runs `make test-telemetry-integration` through
that job's already-verified authority and must conclude `success`; this exact shared name is the
protected Task 0 final-head CI handoff.

Each job creates its private canary/artifact directory under its owning UID. On failure, validate
status under the same owner, copy only already-validated safe status to a runner-readable upload
directory, and gate upload on the named successful validator step. Never copy/upload the manifest.
Use exact step IDs `validate_telemetry_status_rootful` and
`validate_telemetry_status_rootless`, and upload names
`phase10-telemetry-status-rootful` and `phase10-telemetry-status-rootless`. Each upload directory
contains at most the two exact scenario status names. The harness has already cleaned its exact
project before the job returns.

### 18.10 Validate every application stdout line under its owning schema

After every application service has been exercised, collect unfiltered logs. Every nonempty line
must be one JSON object; no prefix stripping, filtering, tailing, or ignored plaintext is allowed.
Six Python services use Task 1's complete JSON-v1 allowed-key/event/closed-field contract. Web uses
Task 9's stronger exact allowed keys, canonical UTC timestamp, `service="web"`, and logger only
`jhin.web` or `jhin.web.wrapper`. Fake/infrastructure/backend services receive no application
schema status. Every line passes the complete canary scan; a banner, traceback, multiline error,
or free text fails.

Fake-only seams stay deterministic, authenticated, bounded, and thread-safe. Fake OpenAI accepts
one bounded strict-base64 final completion only after every requested tool result; malformed,
duplicate, or oversize markers retain deterministic safe behavior and never log data. Fake Linear
installs one bounded error under its existing lock, authenticates before consuming it exactly once
under the same lock, and exposes no error/secret via reset/state. Preserve every predecessor fake
byte contract and test concurrent one-shot consumption.

`docs/operations/telemetry.md` distinguishes leased dev teardown from production disablement:
bundled diagnostics use dynamic loopback ports and disabled Grafana anonymous access;
`observability-down` deletes only the leased dev stack including replaceable monitoring volumes;
production disablement clears the endpoint and rolls product services without deleting product
volumes; external TLS paths are container-visible read-only files, never inline bytes; monitoring
data is replaceable/excluded from product backup while product data is not.

### 18.11 Use executable RED/GREEN and exact two-mode live gates

Install complete delayed-import helpers before RED. The socket-free RED group is:

~~~bash
uv run pytest \
  tests/test_phase10_telemetry_harness.py \
  tests/test_phase10_artifact.py \
  packages/models/tests/test_fake_openai.py \
  packages/connectors/tests/linear/test_fake_linear_admin.py -q
~~~

Expected RED names missing leased scenarios/selection, typed operations, object/array/NDJSON safe
projection, atomic filesystem defenses, workflow gating, final fake completion, or one-shot fake
error. A collection error or Docker/network call is invalid RED.

Focused/static GREEN is exact:

~~~bash
uv lock --check
uv run pytest \
  tests/test_phase10_telemetry_harness.py \
  tests/test_phase10_artifact.py \
  packages/models/tests/test_fake_openai.py \
  packages/connectors/tests/linear/test_fake_linear_admin.py -q
uv run pytest -m "not integration" -q
uv run ruff check \
  scripts/phase10_artifact.py \
  tests/integration/conftest.py \
  tests/integration/emit_phase10_metrics.py \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_telemetry.py \
  tests/test_phase10_artifact.py \
  tests/test_phase10_telemetry_harness.py \
  packages/models/src/jhin_models/testing/fake_openai.py \
  packages/models/tests/test_fake_openai.py \
  packages/connectors/src/jhin_connectors/testing/fake_linear.py \
  packages/connectors/tests/linear/test_fake_linear_admin.py
uv run ruff format --check \
  scripts/phase10_artifact.py \
  tests/integration/conftest.py \
  tests/integration/emit_phase10_metrics.py \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_telemetry.py \
  tests/test_phase10_artifact.py \
  tests/test_phase10_telemetry_harness.py \
  packages/models/src/jhin_models/testing/fake_openai.py \
  packages/models/tests/test_fake_openai.py \
  packages/connectors/src/jhin_connectors/testing/fake_linear.py \
  packages/connectors/tests/linear/test_fake_linear_admin.py
uv run mypy
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web build
git diff --check -- \
  .github/workflows/ci.yml \
  Makefile \
  docs/operations/telemetry.md \
  packages/connectors/src/jhin_connectors/testing/fake_linear.py \
  packages/connectors/tests/linear/test_fake_linear_admin.py \
  packages/models/src/jhin_models/testing/fake_openai.py \
  packages/models/tests/test_fake_openai.py \
  scripts/phase10_artifact.py \
  tests/integration/conftest.py \
  tests/integration/emit_phase10_metrics.py \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_telemetry.py \
  tests/test_phase10_artifact.py \
  tests/test_phase10_telemetry_harness.py
~~~

Root collection loads the live module without daemon access. On a verified rootful socket:

~~~bash
test -S /var/run/docker.sock
socket_gid="$(stat -c %g /var/run/docker.sock)"
test "$socket_gid" -gt 0
SANDBOX_DOCKER_GID="$socket_gid" \
PHASE10_MODE=rootful \
PHASE10_ROOTFUL_DOCKER_SOCKET=/var/run/docker.sock \
make test-telemetry-integration
~~~

On the verified UID-10001 rootless owner:

~~~bash
sudo -u phase10rootless -H env -u SANDBOX_DOCKER_GID \
  PATH="$PATH" \
  XDG_RUNTIME_DIR=/run/user/10001 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10001/bus \
  UV_CACHE_DIR=/tmp/jhin-phase10-uv-cache \
  PHASE10_MODE=rootless \
  PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock \
  make -C /tmp/jhin-phase10-rootless-workspace test-telemetry-integration
~~~

Both pass only with exact positive counts, zero skip/xfail/deselect, and each invocation's full
second-pass resource/image absence. Failure status is captured before that cleanup, independently
validated, canary-free, and the only possible upload.

### 18.12 Make Task 11's 14 paths and committed tree exact

Replace Task 11 `Files`, global File Map ownership, and staging with:

~~~bash
set -euo pipefail
task11_paths=(
  .github/workflows/ci.yml
  Makefile
  docs/operations/telemetry.md
  packages/connectors/src/jhin_connectors/testing/fake_linear.py
  packages/connectors/tests/linear/test_fake_linear_admin.py
  packages/models/src/jhin_models/testing/fake_openai.py
  packages/models/tests/test_fake_openai.py
  scripts/phase10_artifact.py
  tests/integration/conftest.py
  tests/integration/emit_phase10_metrics.py
  tests/integration/phase10_upgrade_harness.py
  tests/integration/test_phase10_telemetry.py
  tests/test_phase10_artifact.py
  tests/test_phase10_telemetry_harness.py
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task11_paths[@]}"
git diff --check -- "${task11_paths[@]}"
git add -- "${task11_paths[@]}"
expected_index="$(printf '%s\n' "${task11_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task11_paths[@]}"
git commit --only "${task11_paths[@]}" \
  -m "test(observability): prove end-to-end telemetry safety"
test "$(git show -s --format=%s HEAD)" = \
  "test(observability): prove end-to-end telemetry safety"
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)"
test "$actual_commit_paths" = "$expected_index"
test -z "$(git diff --cached --name-only)"
~~~

Task 11 adds no dependency/lock, Compose/profile, monitoring config/dashboard, or Task 1-10
production telemetry path. A discovered need first amends File Map/Files/manifest.

### 18.13 Bind Task 12's exact evidence handoff

Task 12 consumes only scenario names `telemetry-base` and `telemetry-observed`; Make targets
`test-telemetry-base`, `test-telemetry-observed`, and `test-telemetry-integration`; existing jobs
`phase10-rootful-live` and `phase10-rootless-live`; the exact fifteen-kind private manifest; the
closed failure-only `compose_status`; and the harness's post-run resource/image absence proof. It
must cite both mode/scenario successes. Static YAML, raw Compose, a failure artifact, a skipped
step, rootful-only execution, or prose is not readiness/cleanup evidence.

### Task 12: Run Release Gates, Record Actual Evidence, and Stage Only Telemetry

**Files:**
- Modify: `docs/evidence/phase10-telemetry.md`
- Modify: `scripts/record_phase10_telemetry_evidence.py`
- Modify: `tests/test_phase10_telemetry_evidence.py`

**Interfaces:**
- Consumes the accepted Task 11 handoff and produces the exact Task 12 contract, subject, manifest, and gates below.

- [ ] **Step 1: Write failing evidence refusal and rendering tests**

Import the script module without executing `main()` and test it with an injected command runner and fixed clock/commit. The test must prove failed gates, raw or encoded canaries, missing acceptance checks, and blank result cells all refuse to write, while safe results render deterministically:

```python
import base64
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from scripts.record_phase10_telemetry_evidence import (
    EvidenceRefused,
    GateResult,
    REQUIRED_VERSION_COMPONENTS,
    record_evidence,
)


REQUIRED_ACCEPTANCE_CHECKS = (
    "Connected webhook-agent-tool trace",
    "Exact metric/cardinality contract",
    "JSON-v1 all application services",
    "Profile-absent product work",
    "Collector-outage product work",
    "Cross-sink canary absence",
)


def all_gate_results_passed() -> list[GateResult]:
    return [
        GateResult("Python unit", "uv run pytest", True, 1.2),
        GateResult("Profile-absent acceptance", "make test-telemetry-base", True, 2.0),
        GateResult("Observed telemetry acceptance", "make test-telemetry-observed", True, 3.0),
    ]


def pinned_test_versions() -> dict[str, str]:
    return {
        "package:jhin-observability": "0.1.0",
        "lock:next": "16.3.1",
        "lock:asyncpg": "0.31.0",
        "lock:fastapi": "0.141.1",
        "lock:httpx": "0.28.1",
        "lock:opentelemetry-api": "1.38.0",
        "lock:opentelemetry-exporter-otlp-proto-grpc": "1.38.0",
        "lock:opentelemetry-sdk": "1.38.0",
        "lock:pydantic-settings": "2.15.0",
        "lock:sqlalchemy": "2.0.52",
        "lock:structlog": "26.1.0",
        "lock:temporalio": "1.31.0",
        "image:otel-collector": "otel/opentelemetry-collector-contrib:0.135.0",
        "image:prometheus": "prom/prometheus:v3.5.0",
        "image:tempo": "grafana/tempo:2.8.2",
        "image:grafana": "grafana/grafana:12.1.0",
    }


def test_record_evidence_refuses_failed_or_leaking_results(tmp_path: Path) -> None:
    destination = tmp_path / "evidence.md"
    failed = [GateResult(name="Python unit", command="uv run pytest", passed=False, elapsed_s=1.2)]
    with pytest.raises(EvidenceRefused, match="gate failed"):
        record_evidence(failed, destination=destination)
    assert not destination.exists()

    canary = "prompt canary/+?"
    raw = canary.encode()
    for leaked_form in (
        canary,
        quote(canary, safe=""),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
    ):
        with pytest.raises(EvidenceRefused, match="sensitive telemetry value"):
            record_evidence(
                all_gate_results_passed(),
                destination=destination,
                captured_output=f"prefix:{leaked_form}:suffix",
                forbidden_values=(canary,),
            )
        assert not destination.exists()


def test_record_evidence_is_deterministic_and_complete(tmp_path: Path) -> None:
    destination = tmp_path / "evidence.md"
    record_evidence(
        all_gate_results_passed(),
        destination=destination,
        recorded_at=datetime(2026, 8, 18, tzinfo=UTC),
        git_commit="0123456789abcdef",
        versions=pinned_test_versions(),
    )
    first = destination.read_text()
    record_evidence(
        all_gate_results_passed(),
        destination=destination,
        recorded_at=datetime(2026, 8, 18, tzinfo=UTC),
        git_commit="0123456789abcdef",
        versions=pinned_test_versions(),
    )
    assert destination.read_text() == first
    assert "| FAIL |" not in first
    assert "PENDING RESULT" not in first
    assert all(name in first for name in REQUIRED_ACCEPTANCE_CHECKS)
    assert set(pinned_test_versions()) == REQUIRED_VERSION_COMPONENTS


def test_profile_absent_row_cannot_be_supplied_without_actual_gate_result(
    tmp_path: Path,
) -> None:
    results = [row for row in all_gate_results_passed() if row.name != "Profile-absent acceptance"]
    with pytest.raises(EvidenceRefused, match="acceptance gate missing"):
        record_evidence(results, destination=tmp_path / "evidence.md")


def test_version_provenance_must_cover_every_promised_lock_and_image(
    tmp_path: Path,
) -> None:
    incomplete = pinned_test_versions()
    incomplete.pop("lock:temporalio")
    with pytest.raises(EvidenceRefused, match="version provenance registry mismatch"):
        record_evidence(
            all_gate_results_passed(),
            destination=tmp_path / "evidence.md",
            git_commit="0123456789abcdef",
            versions=incomplete,
        )
```

- [ ] **Step 2: Run evidence RED**

```bash
uv run pytest tests/test_phase10_telemetry_evidence.py -q
```

Expected: FAIL because the evidence module and types do not exist.

- [ ] **Step 3: Implement the evidence generator**

The script executes each concrete `argv` from `GATES` with `subprocess.run(argv, shell=False,
capture_output=True, text=True, check=False)`, records UTC time, `git rev-parse HEAD`, the `uv lock
--check` gate, exact Python versions parsed from `uv.lock`, Next from both `package.json` and
`pnpm-lock.yaml`, monitoring image values from rendered Compose, and pass/fail/elapsed time for each
command. It refuses to write evidence if any gate fails or if captured output contains a telemetry
canary. It writes deterministic Markdown with this populated schema—no blank result cells:

```python
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import quote

if __package__:
    from .phase10_artifact import read_canary_manifest, write_canary_manifest
else:
    from phase10_artifact import read_canary_manifest, write_canary_manifest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_LOCK_PACKAGES = (
    "asyncpg",
    "fastapi",
    "httpx",
    "opentelemetry-api",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-sdk",
    "pydantic-settings",
    "sqlalchemy",
    "structlog",
    "temporalio",
)
REQUIRED_VERSION_COMPONENTS = frozenset(
    {
        "package:jhin-observability",
        "lock:next",
        *(f"lock:{name}" for name in REQUIRED_LOCK_PACKAGES),
        "image:otel-collector",
        "image:prometheus",
        "image:tempo",
        "image:grafana",
    }
)


@dataclass(frozen=True)
class GateResult:
    name: str
    command: str
    passed: bool
    elapsed_s: float


class EvidenceRefused(RuntimeError):
    """Raised before incomplete or unsafe release evidence can be written."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def run_gate(
    name: str,
    argv: Sequence[str],
    *,
    runner: CommandRunner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> tuple[GateResult, str]:
    started = time.monotonic()
    completed = runner(
        list(argv),
        shell=False,
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        env=env,
    )
    output = completed.stdout + completed.stderr
    return (
        GateResult(
            name=name,
            command=shlex.join(argv),
            passed=completed.returncode == 0,
            elapsed_s=time.monotonic() - started,
        ),
        output,
    )


def discover_versions() -> dict[str, str]:
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    locked_versions = {
        str(package["name"]): str(package["version"])
        for package in locked["package"]
        if "version" in package
    }
    missing = set(REQUIRED_LOCK_PACKAGES) - locked_versions.keys()
    if missing:
        raise EvidenceRefused(f"required uv.lock package missing: {sorted(missing)}")
    web_package = json.loads((ROOT / "apps/web/package.json").read_text())
    next_version = str(web_package["dependencies"]["next"])
    if f"next@{next_version}:" not in (ROOT / "pnpm-lock.yaml").read_text():
        raise EvidenceRefused("Next package and pnpm lock disagree")
    observability_project = tomllib.loads(
        (ROOT / "packages/observability/pyproject.toml").read_text()
    )["project"]
    compose_environment = os.environ.copy()
    compose_environment.pop("SANDBOX_DOCKER_GID", None)
    compose = json.loads(
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "compose.yaml",
                "-f",
                "compose.dev.yaml",
                "-f",
                "compose.rootless.yaml",
                "--profile",
                "observability",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=compose_environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    services = compose["services"]
    images = {
        f"image:{name}": str(services[name]["build"]["args"]["BASE_IMAGE"])
        for name in ("otel-collector", "prometheus", "tempo", "grafana")
    }
    return {
        "package:jhin-observability": str(observability_project["version"]),
        "lock:next": next_version,
        **{f"lock:{name}": locked_versions[name] for name in REQUIRED_LOCK_PACKAGES},
        **images,
    }


REQUIRED_ACCEPTANCE_CHECKS = (
    "Connected webhook-agent-tool trace",
    "Exact metric/cardinality contract",
    "JSON-v1 all application services",
    "Profile-absent product work",
    "Collector-outage product work",
    "Cross-sink canary absence",
)
ACCEPTANCE_GATE = {
    "Connected webhook-agent-tool trace": "Observed telemetry acceptance",
    "Exact metric/cardinality contract": "Observed telemetry acceptance",
    "JSON-v1 all application services": "Observed telemetry acceptance",
    "Profile-absent product work": "Profile-absent acceptance",
    "Collector-outage product work": "Observed telemetry acceptance",
    "Cross-sink canary absence": "Observed telemetry acceptance",
}


def record_evidence(
    results: Sequence[GateResult],
    *,
    destination: Path,
    captured_output: str = "",
    forbidden_values: Sequence[str] = (),
    recorded_at: datetime | None = None,
    git_commit: str | None = None,
    versions: Mapping[str, str] | None = None,
) -> None:
    if not results or any(not result.passed for result in results):
        raise EvidenceRefused("gate failed")
    result_by_name = {result.name: result for result in results}
    if len(result_by_name) != len(results):
        raise EvidenceRefused("duplicate gate name")
    if set(ACCEPTANCE_GATE) != set(REQUIRED_ACCEPTANCE_CHECKS):
        raise EvidenceRefused("acceptance registry mismatch")
    for check, gate_name in ACCEPTANCE_GATE.items():
        gate = result_by_name.get(gate_name)
        if gate is None:
            raise EvidenceRefused(f"acceptance gate missing: {check}")
        if not gate.passed:
            raise EvidenceRefused(f"acceptance gate failed: {check}")
    if any(not result.name.strip() or not result.command.strip() for result in results):
        raise EvidenceRefused("blank gate cell")
    for value in forbidden_values:
        raw = value.encode()
        forms = (
            value,
            quote(value, safe=""),
            base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
        )
        if any(form and form in captured_output for form in forms):
            raise EvidenceRefused("sensitive telemetry value")
    stamp = recorded_at or datetime.now(UTC)
    commit = (
        git_commit
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    version_map = dict(versions or discover_versions())
    if set(version_map) != REQUIRED_VERSION_COMPONENTS:
        raise EvidenceRefused("version provenance registry mismatch")
    if not commit or any(
        not key.strip() or not value.strip() for key, value in version_map.items()
    ):
        raise EvidenceRefused("blank provenance cell")

    def cell(value: object) -> str:
        rendered = str(value).replace("|", "\\|").replace("\n", " ").strip()
        if not rendered:
            raise EvidenceRefused("blank result cell")
        return rendered

    lines = [
        "# Phase 10 Telemetry Evidence",
        "",
        f"- Recorded at: `{stamp.astimezone(UTC).isoformat()}`",
        f"- Git commit: `{cell(commit)}`",
        "",
        "## Gates",
        "",
        "| Gate | Command | Result | Elapsed seconds |",
        "|---|---|---:|---:|",
    ]
    lines.extend(
        f"| {cell(result.name)} | `{cell(result.command)}` | PASS | {result.elapsed_s:.3f} |"
        for result in results
    )
    lines.extend(("", "## Acceptance", "", "| Check | Result |", "|---|---:|"))
    lines.extend(f"| {cell(name)} | PASS |" for name in REQUIRED_ACCEPTANCE_CHECKS)
    lines.extend(("", "## Versions", "", "| Component | Version |", "|---|---|"))
    lines.extend(
        f"| {cell(name)} | `{cell(value)}` |" for name, value in sorted(version_map.items())
    )
    rendered = "\n".join(lines) + "\n"
    all_forbidden = tuple(
        form
        for value in forbidden_values
        for form in (
            value,
            quote(value, safe=""),
            base64.b64encode(value.encode()).decode(),
            base64.urlsafe_b64encode(value.encode()).decode(),
        )
    )
    if any(form and form in rendered for form in all_forbidden):
        raise EvidenceRefused("sensitive telemetry value")
    if any(marker in rendered for marker in ("FAIL", "PENDING RESULT", "INCOMPLETE")):
        raise EvidenceRefused("non-pass marker")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(rendered)
    os.replace(temporary, destination)


GATES = (
    ("Locked dependencies", ["uv", "lock", "--check"]),
    ("Python unit", ["uv", "run", "pytest", "-m", "not integration"]),
    ("Ruff lint", ["uv", "run", "ruff", "check", "."]),
    ("Ruff format", ["uv", "run", "ruff", "format", "--check", "."]),
    ("mypy", ["uv", "run", "mypy"]),
    ("Web test", ["pnpm", "--filter", "jhin-web", "test"]),
    ("Web lint", ["pnpm", "--filter", "jhin-web", "lint"]),
    ("Web typecheck", ["pnpm", "--filter", "jhin-web", "typecheck"]),
    ("Web build", ["pnpm", "--filter", "jhin-web", "build"]),
    (
        "Compose model",
        [
            "uv",
            "run",
            "python",
            "scripts/assert_phase10_observability_compose.py",
            "--mode",
            "rootless",
        ],
    ),
    (
        "Tool-worker Compose model",
        [
            "uv",
            "run",
            "python",
            "scripts/assert_phase10_tool_worker_compose.py",
            "--mode",
            "rootless",
        ],
    ),
    (
        "Dashboard generated",
        ["uv", "run", "python", "scripts/build_phase10_dashboard.py", "--check"],
    ),
    ("Logging audit", ["uv", "run", "python", "scripts/audit_phase10_logging.py"]),
    ("Profile-absent acceptance", ["make", "test-telemetry-base"]),
    ("Observed telemetry acceptance", ["make", "test-telemetry-observed"]),
)


def main() -> int:
    destination = ROOT / "docs/evidence/phase10-telemetry.md"
    with tempfile.TemporaryDirectory(prefix="phase10-evidence-") as directory:
        canary_path = Path(directory) / "canaries.json"
        write_canary_manifest(canary_path)
        forbidden_values = read_canary_manifest(canary_path)
        gate_env = {
            **os.environ,
            "JHIN_TELEMETRY_CANARY_FILE": str(canary_path),
            "PHASE10_SOCKET_MODE": "rootless",
        }
        gate_env.pop("SANDBOX_DOCKER_GID", None)
        results: list[GateResult] = []
        output: list[str] = []
        for name, argv in GATES:
            result, captured = run_gate(name, argv, env=gate_env)
            results.append(result)
            output.append(captured)
        record_evidence(
            results,
            destination=destination,
            captured_output="\n".join(output),
            forbidden_values=forbidden_values,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

The evidence command iterates `GATES` with `run_gate`; therefore the profile-absent evidence row can exist only after `make test-telemetry-base` actually built the sandbox image, started the base Compose project without the profile, ran the black-box gate, and tore it down successfully. The observed acceptance result similarly backs the other five rows through `ACCEPTANCE_GATE`; callers cannot inject a prose boolean map. It records the actual trace ID only if it is test-generated and contains no workspace/resource identity.

Run focused GREEN before release gates:

```bash
uv run pytest tests/test_phase10_telemetry_evidence.py -q
```

Expected: PASS; a refused run leaves no evidence file, and a valid run is deterministic.

- [ ] **Step 4: Run all release gates from a clean index**

```bash
test -z "$(git diff --cached --name-only)"
rootless_socket="${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}"
uv lock --check
uv run pytest -m 'not integration'
uv run ruff check .
uv run ruff format --check .
uv run mypy
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web build
uv run python scripts/build_phase10_dashboard.py --check
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" uv run python \
  scripts/assert_phase10_observability_compose.py --mode rootless
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" uv run python \
  scripts/assert_phase10_tool_worker_compose.py --mode rootless
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  docker compose -f compose.yaml \
  -f compose.rootless.yaml config --quiet
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  docker compose -f compose.yaml -f compose.dev.yaml \
  -f compose.rootless.yaml --profile observability config --quiet
```

Expected: every command exits zero. Fix any failure with a new RED/GREEN cycle in the owning task; do not weaken a test or cardinality/redaction rule.

- [ ] **Step 5: Run the final live acceptance twice**

First with no profile/export endpoint, then with the full profile:

```bash
rootless_socket="${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}"
test -S "$rootless_socket"
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  docker compose -p jhin-phase10-base \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile build build sandbox-image
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  OTEL_EXPORTER_OTLP_ENDPOINT= \
  docker compose -p jhin-phase10-base -f compose.yaml -f compose.dev.yaml \
  -f compose.rootless.yaml up -d --build --wait --wait-timeout 240
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  JHIN_TEST_COMPOSE_PROJECT=jhin-phase10-base \
  JHIN_TELEMETRY_MODE=base PHASE10_SOCKET_MODE=rootless \
  uv run pytest -m integration \
  tests/integration/test_phase10_telemetry.py::test_product_completes_work_with_profile_absent -v
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  docker compose -p jhin-phase10-base \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml down -v --remove-orphans

env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  docker compose -p jhin-phase10-observed \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile build build sandbox-image
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
  OTEL_EXPORTER_OTLP_INSECURE=true docker compose -p jhin-phase10-observed \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile observability up -d --build --wait --wait-timeout 240
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  JHIN_TEST_COMPOSE_PROJECT=jhin-phase10-observed \
  JHIN_TELEMETRY_MODE=observed PHASE10_SOCKET_MODE=rootless \
  uv run pytest -m integration tests/integration/test_phase10_telemetry.py \
  -k 'not profile_absent' -v
env -u SANDBOX_DOCKER_GID PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  docker compose -p jhin-phase10-observed \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile observability down -v --remove-orphans
```

Expected: both clean-stack runs pass and both explicit projects are torn down. The observed stack completes the connected trace, metric, JSONL, backend-failure/recovery, and canary assertions.

- [ ] **Step 6: Generate evidence from actual current results**

```bash
uv run python scripts/record_phase10_telemetry_evidence.py
test -s docs/evidence/phase10-telemetry.md
# Superseded by the fail-closed evidence parser below.
rootless_socket="${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}"
test -z "$(env -u SANDBOX_DOCKER_GID \
  PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" docker compose -p jhin-phase10-base \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml ps -q)"
test -z "$(env -u SANDBOX_DOCKER_GID \
  PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" docker compose -p jhin-phase10-observed \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile observability ps -q)"
```

Expected: the generator itself reruns both live Compose acceptance gates from `GATES`, records their actual results, and leaves both projects down. The evidence contains actual dated PASS results plus parsed package/lock/image versions; no raw logs, prompts, tool arguments, URLs, credentials, or canaries are embedded.

- [ ] **Step 7: Run the final secret/cardinality/static security gates**

```bash
uv run pytest packages/observability/tests packages/models/tests/test_telemetry.py \
  packages/connectors/tests/test_telemetry.py packages/tools/tests/test_telemetry.py \
  services/agent_worker/tests/test_telemetry.py \
  services/tool_worker/tests/test_telemetry.py \
  services/event_worker/tests/test_telemetry.py \
  services/sandbox_runner/tests/test_telemetry.py \
  apps/api/tests/test_observability.py tests/test_phase10_telemetry_evidence.py -q
if rg -n --glob '!docs/superpowers/plans/**' \
  'workspace_id|user_id|agent_id|task_id|run_id|request_id|correlation_id|trace_id|connection_id|tool_call_id' \
  ops/observability/grafana/dashboards; then
  echo "identifier-like dashboard label or variable found"
  exit 1
fi
```

Expected: focused leak/cardinality tests pass and dashboards contain no identifier variables/labels. Trace/log context field names are tested in code; this static dashboard gate is intentionally stricter.

- [ ] **Step 8: Stage only evidence-generator/evidence files and commit**

The task's sole staging and commit gate is the exact manifest-owned gate in the final executable contract below.

Expected: exactly the three Task 12 files are in the final commit; design specs remain unstaged.

- [ ] **Step 9: Verify final repository state and commit sequence**

```bash
# The final history and manifest gate runs below.
```


Expected: the named checkpoint through `HEAD` is exactly the 13 scoped commits (Task 0 plus Tasks
1–12), while the exact subject sequence after the checkpoint is the 12 implementation/evidence
commits. The path-scoped diff contains no unrelated file.

#### Binding rootless evidence socket


The draft propagates the socket into several release commands but not into the evidence generator.
Revise Task 12 Steps 1 and 3 to define and test:

```python
def require_rootless_socket(environ: Mapping[str, str]) -> str:
    raw = environ.get("PHASE10_ROOTLESS_DOCKER_SOCKET", "")
    path = Path(raw)
    if not raw or not path.is_absolute() or not path.is_socket():
        raise EvidenceRefused(
            "PHASE10_ROOTLESS_DOCKER_SOCKET must name the verified Unix socket"
        )
    return raw
```

Binding behavior:

- `main()` calls `require_rootless_socket(os.environ)` before creating a canary or running a gate.
- `gate_env` contains that exact path and removes `SANDBOX_DOCKER_GID`.
- `discover_versions` accepts/inherits the same environment, validates the same socket, and passes
  the exact path to its rootless Compose render.
- Every `GATES` command, including both Compose-model scripts and both live Make targets, receives
  that exact `gate_env` through `run_gate`.
- `record_evidence` receives explicit versions discovered under that environment; it does not
  silently rerender Compose under a different process environment.

Add tests that create a temporary Unix socket and prove the injected runner sees the same absolute
path for every gate; missing, relative, regular-file, and nonexistent paths must raise
`EvidenceRefused` before the destination or temporary canary file is created.

Add `test -S "$rootless_socket"` immediately after every Task 12 assignment of
`rootless_socket`. Replace Step 6's generator invocation with:

```bash
rootless_socket="${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}"
test -S "$rootless_socket"
env -u SANDBOX_DOCKER_GID \
  PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  uv run python scripts/record_phase10_telemetry_evidence.py
```

The evidence row is invalid if the generator saw only a conventional path string rather than a
socket that passed `-S`/`Path.is_socket()` at execution time.

#### Final executable contract for Task 12


Task 12 begins only after corrected Tasks 1-11 exist and the pushed Task 11 head has one accepted
exact-head Actions run whose two existing live jobs both passed base then observed. Replace the
Task 12 brief with this section. Task 12 owns only evidence generation/validation; it does not
invent a lifecycle, restage a predecessor, or turn a failure artifact/static render into success.

Its accepted lifecycle baseline is the same exact predecessor tip
`ee66c588014acf8e448352a7e5e458aca63d37fe` named by Tasks 0, 10, and 11.

### 19.1 Route all live work through the sole authority

Delete manual raw-Compose live/config commands and fixed-project cleanup checks. Task 12 invokes
live work only through:

~~~text
Profile-absent acceptance -> make test-telemetry-base
Observed telemetry acceptance -> make test-telemetry-observed
~~~

The sanitized generator environment contains exactly one selected live mode:

~~~text
PHASE10_MODE=rootless
PHASE10_ROOTLESS_DOCKER_SOCKET=$rootless_socket
DOCKER_HOST=unix://$rootless_socket
COMPOSE_DISABLE_ENV_FILE=1
APP_ENV=test
JHIN_TELEMETRY_CANARY_FILE=$telemetry_canary_file
~~~

The generator creates the manifest once with Task 11's validated factory in its private run
directory. It removes `SANDBOX_DOCKER_GID` and all competing Docker/Compose/profile/mode/crash/
OTel selectors. It never sets a caller-owned `PHASE10_SOCKET_MODE`. A zero live gate means the
strict scenario count passed and leased second-pass resource/image cleanup completed; no fixed-
project `ps` inference is allowed.

### 19.2 Use one immutable, exhaustive gate registry

Replace mutable command lists with an immutable `GateSpec(name, argv, timeout_seconds,
backs_acceptance)` registry in this exact order:

~~~text
Locked dependencies              |  300 | uv lock --check
Python ordinary collection       | 3600 | uv run pytest
Ruff lint                        |  900 | uv run ruff check .
Ruff format                      |  900 | uv run ruff format --check .
mypy                             | 1800 | uv run mypy
Web test                         | 1200 | pnpm --filter jhin-web test
Web lint                         |  900 | pnpm --filter jhin-web lint
Web typecheck                    | 1200 | pnpm --filter jhin-web typecheck
Web build                        | 1800 | pnpm --filter jhin-web build
Dashboard/provisioning contract  |  600 | uv run python scripts/build_phase10_dashboard.py --check
Logging audit                    |  600 | uv run python scripts/audit_phase10_logging.py
Observability Compose model      |  600 | uv run python scripts/assert_phase10_observability_compose.py --mode rootless
Tool-worker Compose model        |  600 | uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootless
Profile-absent acceptance        | 6600 | make test-telemetry-base
Observed telemetry acceptance    | 6600 | make test-telemetry-observed
~~~

`record_evidence` accepts only results originating from this registry, in exact order, canonical
argv, finite elapsed time, and zero return code. Reject missing/extra/reordered/duplicate names,
altered commands, nonzero return, blank cells, negative/non-finite durations, and bool-as-number.
The test fixture supplies all gates; a three-row fake is invalid.

The six acceptance rows map exactly:

~~~text
Connected webhook-agent-tool trace -> Observed telemetry acceptance
Exact metric/cardinality contract   -> Observed telemetry acceptance
JSON-v1 all application services    -> Observed telemetry acceptance
Profile-absent product work         -> Profile-absent acceptance
Collector-outage product work       -> Observed telemetry acceptance
Cross-sink canary absence           -> Observed telemetry acceptance
~~~

Canary absence requires the exact nonempty fifteen-kind manifest read through Task 11's validated
reader. An empty/short/duplicate/ad-hoc value collection cannot create a PASS.

### 19.3 Cite exact-head rootful and rootless CI provenance

With an injected GitHub command runner, verify and record only closed provenance:

- exact Task 11 PR head commit;
- fetched synthetic-merge parents/tree and exact synthetic-merge checkout SHA in both retained
  job logs using Task 0's checkout proof;
- distinct successful `phase10-rootful-live` and `phase10-rootless-live` job IDs;
- the named telemetry integration step in each job succeeded rather than skipped;
- each job's workflow ran `telemetry-base` followed by `telemetry-observed`; and
- rootful socket/GID authority and rootless UID-10001 authority steps succeeded.

Evidence may contain run ID, job IDs, commit, mode, scenario, and `PASS`. It contains no raw job
log, log URL, environment, mount, port, project, socket path, canary, or product identifier. Local
verified-rootless execution supplements but never replaces two-mode CI. Failure-only status is
never success or cleanup evidence.

The final Task 12 commit cannot embed its own run without self-reference. After pushing Task 12,
obtain a fresh successful exact-head required CI run before protected-health work. Do not amend the
evidence solely to include that later run.

### 19.4 Bind one socket, sanitized environment, renderer, and version registry

Reuse `SocketMetadata.capture` and `validate_socket_metadata(..., mode="rootless")`: absolute,
non-symlink Unix socket, host UID 10001, no rootful GID. Capture immutable metadata before gates,
revalidate after each socket-consuming gate and at the end. The live harness independently proves
rootless daemon identity/security options, cgroup v2, and systemd cgroup driver.

`discover_versions` receives that sanitized gate environment and socket snapshot and uses Task
10's poison-resistant rootless observed renderer. It never starts raw Compose with
`os.environ.copy()`. Require exactly the four monitoring services and exact `BASE_IMAGE` args.
The exact version registry is:

~~~text
package:jhin-observability
lock:asyncpg
lock:fastapi
lock:grpcio
lock:httpx
lock:opentelemetry-api
lock:opentelemetry-exporter-otlp-proto-grpc
lock:opentelemetry-sdk
lock:pydantic-settings
lock:sqlalchemy
lock:structlog
lock:temporalio
image:otel-collector
image:prometheus
image:tempo
image:grafana
~~~

There is no `lock:next`. Parse `uv.lock` structurally and require exactly one versioned entry for
each named package; reconcile the workspace package version with its lock entry. Inject files/
renderer in unit tests and reject missing/duplicate/malformed/blank entries, extra/missing
service, absent profile, wrong socket, or altered image without Docker.

### 19.5 Make the run revision-stable and non-self-referential

Before the first gate, snapshot and validate:

1. unique Task 11 commit and exact subject/path manifest;
2. empty entire index;
3. no worktree override in the exact Task 1-11 owned-path union;
4. SHA-256 of the Task 12 generator and test source that will be committed; and
5. validated rootless socket metadata.

After every gate and before writing, require unchanged `HEAD`, index, predecessor path state,
generator/test hashes, and socket metadata. A concurrent commit/edit/socket replacement refuses
evidence. The Markdown calls this revision the **audited Task 11 product commit** and records the
two source digests, not a nonexistent final Task 12 commit.

After commit require `HEAD^` equals that audited commit; committed generator/test digests equal
the evidence; Task 12 subject/three paths are exact; and the parser accepts committed Markdown.

### 19.6 Prove the exact ordered task history without revision arithmetic

Use section 4's Bash-3.2 `unique_commit_with_subject` helper. Locate the unique checkpoint and
final evidence subjects, require the final subject commit equals `HEAD`, and require these twelve
subjects in exactly this order after the checkpoint with no intervening commit:

~~~text
feat(observability): enforce safe JSON log schema
feat(observability): add bounded optional OTLP bootstrap
feat(observability): enforce telemetry metric cardinality
feat(observability): trace API and database boundaries
feat(observability): propagate traces through NATS
feat(observability): trace Temporal service boundaries
feat(observability): record committed agent and tool metrics
feat(observability): trace connector and sandbox boundaries
feat(web): emit safe versioned server logs
feat(observability): add optional monitoring profile
test(observability): prove end-to-end telemetry safety
docs(observability): record Phase 10 telemetry evidence
~~~

For each resolved commit, compare `git diff-tree` with that corrected task's exact path manifest:
51, 15, 5, 15, 15, 39, 32, 44, 12, 18, 14, and 3 paths respectively. A count/stat, path union,
subset, subject-only proof, or revision arithmetic is not a substitute. Use no `mapfile`,
`readarray`, associative array, or case-conversion expansion.

The executable history proof is Bash-3.2 compatible:

~~~bash
set -euo pipefail
telemetry_plan=docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md

unique_commit_with_subject() {
  local subject="$1"
  local matches count resolved_subject
  matches="$(git log --format=%H --fixed-strings --grep="$subject")" || return 1
  count="$(printf '%s\n' "$matches" | \
    awk 'NF { count++ } END { print count + 0 }')" || return 1
  test "$count" = 1 || return 1
  resolved_subject="$(git show -s --format=%s "$matches")" || return 1
  test "$resolved_subject" = "$subject" || return 1
  printf '%s\n' "$matches"
}

task_manifest() {
  local task_number="$1"
  local marker marker_count
  marker="task${task_number}_paths=("
  marker_count="$(rg -F -c -x -- "$marker" "$telemetry_plan")" || return 1
  test "$marker_count" -eq 1 || return 1
  awk -v marker="$marker" '
    $0 == marker { found = 1; inside = 1; next }
    inside && /^\)$/ { complete = 1; exit }
    inside && /^  [^ ]/ { print substr($0, 3) }
    END { if (!found || !complete) exit 1 }
  ' "$telemetry_plan" | LC_ALL=C sort || return 1
}

ordered_subjects=(
  "feat(observability): enforce safe JSON log schema"
  "feat(observability): add bounded optional OTLP bootstrap"
  "feat(observability): enforce telemetry metric cardinality"
  "feat(observability): trace API and database boundaries"
  "feat(observability): propagate traces through NATS"
  "feat(observability): trace Temporal service boundaries"
  "feat(observability): record committed agent and tool metrics"
  "feat(observability): trace connector and sandbox boundaries"
  "feat(web): emit safe versioned server logs"
  "feat(observability): add optional monitoring profile"
  "test(observability): prove end-to-end telemetry safety"
  "docs(observability): record Phase 10 telemetry evidence"
)

checkpoint_commit="$(unique_commit_with_subject \
  'docs(observability): checkpoint Phase 10 telemetry execution')" || exit 1
final_commit="$(unique_commit_with_subject \
  'docs(observability): record Phase 10 telemetry evidence')" || exit 1
head_commit="$(git rev-parse HEAD)" || exit 1
test "$final_commit" = "$head_commit" || exit 1

expected_commits="$(
  task_number=1
  while test "$task_number" -le 12; do
    index_number=$((task_number - 1))
    task_commit="$(unique_commit_with_subject \
      "${ordered_subjects[$index_number]}")" || exit 1
    expected_paths="$(task_manifest "$task_number")" || exit 1
    actual_paths="$(git diff-tree --no-commit-id --name-only -r \
      "$task_commit" | LC_ALL=C sort)" || exit 1
    test "$actual_paths" = "$expected_paths" || exit 1
    printf '%s\n' "$task_commit" || exit 1
    task_number=$((task_number + 1))
  done
)" || exit 1
actual_commits="$(git rev-list --reverse "$checkpoint_commit"..HEAD)" || exit 1
test "$actual_commits" = "$expected_commits" || exit 1
cached_paths="$(git diff --cached --name-only)" || exit 1
test -z "$cached_paths" || exit 1
~~~

Add `test_bash32_history_audit_fails_closed` to
`tests/test_phase10_telemetry_evidence.py`. It creates a temporary Git repository and temporary
telemetry-plan fixture, invokes the exact audit above with `/bin/bash`, and first proves the valid
twelve-commit fixture succeeds. It then runs three independent mutations and requires a nonzero
exit each time:

1. a second commit whose body or subject contains one requested subject, creating two grep hits;
2. a duplicate exact `task6_paths=(` marker in the plan fixture; and
3. one committed Task 8 path replaced by a wrong path while the ordered commit hashes remain
   otherwise valid.

Also mutate one commit body so it contains a requested subject while its actual subject differs;
the explicit `git show --format=%s` equality must reject it. Capture stdout/stderr only for safe
test diagnostics. No test may accept `set -e` as the failure mechanism; the nonzero result must
come from the explicit `return 1` / `exit 1` branches above. Run this named test with the system
Bash 3.2 compatibility gate before Task 12 records evidence.

### 19.7 Bound execution, canary scanning, and atomic evidence writing

Every gate has its registry timeout. The outer live timeout is longer than the inner scenario and
cannot leave a child process group. Capture output in a private `0700` directory with new `0600`
regular files, bounded per-gate and aggregate bytes, incremental cross-chunk canary scanning, and
owned-inode-only cleanup. Do not retain both streams twice in memory. Timeout, signal, spawn,
decoder, output-cap, or cleanup uncertainty fails and refuses evidence. Run fail-fast in registry
order; a static failure starts no live mutation, while a live failure still lets its harness finish
failure capture and cleanup.

Render evidence in memory, validate the full closed schema, and install only through a unique new
`0600` regular file in a verified destination directory: complete write, file fsync, absent
destination, atomic install, directory fsync. Reject links, foreign/wrong mode, preexisting
destination, predictable temporary name, short writes, replace/fsync failure, invalid UTC,
non-40-hex commit, Markdown control/backtick injection, and non-finite duration.

`--check` parses the committed Markdown and proves exact sections, registry/acceptance/version
rows, local rootless plus two-mode CI provenance, no blank/FAIL/pending cell, and no encoded canary.
Search helpers distinguish no-match from search failure. Task 10's structural dashboard test owns
the one datasource exemplar exception; no broad scan may reject it or miss provisioning.

### 19.8 Use executable RED/GREEN and run the expensive matrix once

Create all fake runner, clock, socket, renderer, GitHub, lock, filesystem, and scanner helpers
before RED; delay missing production import inside named tests. RED is:

~~~bash
uv run pytest tests/test_phase10_telemetry_evidence.py -q
~~~

Named groups cover registry/refusal/rendering, socket/environment/revision stability, structural
versions, two-mode CI, bounded runner/canary scanning, and atomic writer/parser/CLI. Collection
import, undefined fixture, Docker/network/GitHub, or unrelated failure is invalid RED.

Focused GREEN is:

~~~bash
uv lock --check
uv run pytest \
  tests/test_phase10_telemetry_evidence.py \
  tests/test_phase10_artifact.py \
  tests/test_phase10_observability_compose.py \
  tests/test_phase10_tool_worker_compose.py \
  tests/test_web_json_stdout.py -q
uv run ruff check \
  scripts/record_phase10_telemetry_evidence.py \
  tests/test_phase10_telemetry_evidence.py
uv run ruff format --check \
  scripts/record_phase10_telemetry_evidence.py \
  tests/test_phase10_telemetry_evidence.py
uv run mypy
~~~

Resolve the accepted Task 11 exact-head run and numeric rootful/rootless job IDs with Task 0's
PR-head, synthetic-merge parent/tree, and two-checkout-log proof. The generator independently
requeries them. Then run the exhaustive registry exactly once:

~~~bash
test -n "${accepted_run_id:?}"
test -n "${rootful_job_id:?}"
test -n "${rootless_job_id:?}"
rootless_socket="${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}"
test -S "$rootless_socket"
env -u SANDBOX_DOCKER_GID \
  PHASE10_MODE=rootless \
  PHASE10_ROOTLESS_DOCKER_SOCKET="$rootless_socket" \
  uv run python scripts/record_phase10_telemetry_evidence.py \
    --accepted-run-id "$accepted_run_id" \
    --rootful-job-id "$rootful_job_id" \
    --rootless-job-id "$rootless_job_id"
test -s docs/evidence/phase10-telemetry.md
uv run python scripts/record_phase10_telemetry_evidence.py \
  --check docs/evidence/phase10-telemetry.md
~~~

The registry runs lock, full ordinary collection, Ruff check/format, mypy, all four web gates,
dashboard, logging audit, both rootless Compose model authorities, and leased base/observed. The
one validated private manifest spans the run and reaches only the two scenarios/validator. Install
evidence only after every local gate, two-mode CI check, canary scan, cleanup attestation,
provenance snapshot, and version check passes. After generation rerun only the fast parser,
scoped-diff, staging, ordered-history, and post-commit checks.

### 19.9 Make Task 12's three paths and committed tree exact

Task 12 owns exactly:

~~~text
docs/evidence/phase10-telemetry.md
scripts/record_phase10_telemetry_evidence.py
tests/test_phase10_telemetry_evidence.py
~~~

Use this exact staging block:

~~~bash
set -euo pipefail
task12_paths=(
  docs/evidence/phase10-telemetry.md
  scripts/record_phase10_telemetry_evidence.py
  tests/test_phase10_telemetry_evidence.py
)
test -z "$(git diff --cached --name-only)"
git status --short -- "${task12_paths[@]}"
git add -- "${task12_paths[@]}"
expected_index="$(printf '%s\n' "${task12_paths[@]}" | LC_ALL=C sort)"
actual_index="$(git diff --cached --name-only | LC_ALL=C sort)"
test "$actual_index" = "$expected_index"
git diff --cached --check -- "${task12_paths[@]}"
git commit --only "${task12_paths[@]}" \
  -m "docs(observability): record Phase 10 telemetry evidence"
test "$(git show -s --format=%s HEAD)" = \
  "docs(observability): record Phase 10 telemetry evidence"
actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)"
test "$actual_commit_paths" = "$expected_index"
test -z "$(git diff --cached --name-only)"
~~~

Task 12 consumes but never restages Make, CI, Compose, harness, artifact, dashboard, integration,
or predecessor production paths.

### 19.10 Bind protected-health and release handoffs

The final Markdown is accepted only if every gate/claim/version/provenance row is exact and PASS;
both CI modes/scenarios and local rootless work are present; exact fifteen-kind raw/percent/base64
variants are absent from complete sinks and gate output; harness success proves cleanup; and no raw
log, trace, metric, config, port, mount, environment, socket path, project, URL, credential,
canary, or product identifier is embedded.

Protected health begins only after the unique Task 12 evidence commit is pushed and receives its
fresh exact-head required CI run. It verifies checkpoint blob equality and exact ordered telemetry
history. Evidence is documentation/verification, not authorization to merge, tag, release, deploy,
or delete production data.

#### Combined two-plan structural validation


This section extends and supersedes section 15 after sections 17-19 are applied. Run it against
only the two tracked plans before staging the checkpoint. It is Bash-3.2 compatible and verifies
that every telemetry task's `Files` block, exact staging array, count, and commit subject agree.
It also validates Task 0's two-plan ownership and the exact Task 10-12 manifests.

~~~bash
set -euo pipefail
plan_paths=(
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md
)
telemetry_plan="${plan_paths[1]}"
protected_plan="${plan_paths[0]}"

git diff --check -- "${plan_paths[@]}" || exit 1
cached_paths="$(git diff --cached --name-only)" || exit 1
test -z "$cached_paths" || exit 1

executable_violations="$(
  awk '
    FNR == 1 { in_shell = 0; stop_scan = 0 }
    /^#### Combined two-plan structural validation$/ {
      stop_scan = 1
      next
    }
    stop_scan { next }
    /^(```|~~~)(bash|sh)$/ {
      in_shell = 1
      next
    }
    in_shell && /^(```|~~~)$/ {
      in_shell = 0
      next
    }
    in_shell && /^[[:space:]]*(mapfile|readarray)([[:space:]]|$)/ {
      print FILENAME ":" FNR ":" $0
      next
    }
    in_shell && /^[[:space:]]*git[[:space:]].*HEAD~[0-9]+/ {
      print FILENAME ":" FNR ":" $0
    }
  ' "${plan_paths[@]}"
)" || exit 1
test -z "$executable_violations" || exit 1

acceptance_variables=(
  accepted_run_id
  rootful_job_id
  rootless_job_id
  synthetic_merge
  shared_tree
)
expected_assignment_values=(
  32404319465
  96539679313
  96539679660
  5e7373aa9413f4500fde1f0f87c520eb14ba62b3
  7b50d34bfd0db0d30e4ab68589c55ef853acd40d
)
assignment_index=0
for acceptance_variable in "${acceptance_variables[@]}"; do
  assignment_values="$(
    awk -F= -v key="$acceptance_variable" '
      $1 == key { print substr($0, length(key) + 2) }
    ' "$telemetry_plan"
  )" || exit 1
  assignment_count="$(printf '%s\n' "$assignment_values" | \
    awk 'NF { count += 1 } END { print count + 0 }')" || exit 1
  test "$assignment_count" -eq 1 || exit 1
  test "$assignment_values" = \
    "${expected_assignment_values[$assignment_index]}" || exit 1
  assignment_index=$((assignment_index + 1))
done

task_files() {
  local task_number="$1"
  awk -v task_number="$task_number" '
    $0 ~ ("^### Task " task_number ":") {
      if (task_seen) exit 2
      task_seen = 1
      in_task = 1
      next
    }
    in_task && /^### Task [0-9]+:/ { exit }
    in_task && /^\*\*Files:\*\*/ { in_files = 1; next }
    in_files && /^\*\*Interfaces:\*\*/ { complete = 1; exit }
    in_files && /^- (Create|Modify): `/ {
      line = $0
      sub(/^- (Create|Modify): `/, "", line)
      sub(/`$/, "", line)
      print line
    }
    END {
      if (!task_seen || !in_files || !complete) exit 1
    }
  ' "$telemetry_plan" | LC_ALL=C sort || return 1
}

task_index() {
  local task_number="$1"
  local marker marker_count
  marker="task${task_number}_paths=("
  marker_count="$(rg -F -c -x -- "$marker" "$telemetry_plan")" || return 1
  test "$marker_count" -eq 1 || return 1
  awk -v marker="$marker" '
    $0 == marker { found = 1; inside = 1; next }
    inside && /^\)$/ { complete = 1; exit }
    inside && /^  [^ ]/ { print substr($0, 3) }
    END { if (!found || !complete) exit 1 }
  ' "$telemetry_plan" | LC_ALL=C sort || return 1
}

task_section() {
  local task_number="$1"
  awk -v task_number="$task_number" '
    $0 ~ ("^### Task " task_number ":") { found = 1 }
    found && $0 ~ /^### Task [0-9]+:/ &&
      $0 !~ ("^### Task " task_number ":") { exit }
    found { print }
    END { if (!found) exit 1 }
  ' "$telemetry_plan" || return 1
}

line_count() {
  awk 'NF { count += 1 } END { print count + 0 }' || return 1
}

task0_expected="$(printf '%s\n' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md \
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md | LC_ALL=C sort)" || exit 1
task0_actual="$(task_files 0)" || exit 1
task0_count="$(printf '%s\n' "$task0_actual" | line_count)" || exit 1
test "$task0_count" -eq 2 || exit 1
test "$task0_actual" = "$task0_expected" || exit 1
task0_checkpoint_text="$(task_section 0)" || exit 1
rg -F -q -- 'docs(observability): checkpoint Phase 10 telemetry execution' \
  <<<"$task0_checkpoint_text" || exit 1

expected_candidate_tip=0439fb2c92075ee5cdd5adf9bc54d2805de6670e
expected_handoff_tip=ee66c588014acf8e448352a7e5e458aca63d37fe
task0_text="$(task_section 0)" || exit 1
task0_tip="$(printf '%s\n' "$task0_text" | \
  awk -F= '$1 == "accepted_tip" { print $2 }')" || exit 1
task0_tip_count="$(printf '%s\n' "$task0_tip" | line_count)" || exit 1
test "$task0_tip_count" -eq 1 || exit 1
test "$task0_tip" = "$expected_candidate_tip" || exit 1
task0_pr_head="$(printf '%s\n' "$task0_text" | \
  awk -F= '$1 == "pr_head" { print $2 }')" || exit 1
task0_pr_head_count="$(printf '%s\n' "$task0_pr_head" | line_count)" || exit 1
test "$task0_pr_head_count" -eq 1 || exit 1
test "$task0_pr_head" = "$expected_candidate_tip" || exit 1
rg -F -q -- \
  'test "$(git rev-list --count "$predecessor_base".."$accepted_tip")" = 30' \
  <<<"$task0_text" || exit 1
rg -F -q -- 'test "$actual_path_count" = 36' <<<"$task0_text" || exit 1
for candidate_commit in \
  3deb7da456ebbcdd904d1e873270097edc0a7ed4 \
  41bc0f44033785f77c20d0fa5ddcaed1792dfab9 \
  ee66c588014acf8e448352a7e5e458aca63d37fe \
  639cf43d1d1189971b26c6d9809f0ed89c52eabd \
  7d8f6b14466047404b3face0c98211310995dc47 \
  bee85b90e69dbbd79ce0576d25f0e95efac2b09f \
  a430ccd8d6054f32f7e959abde85aa1f78c4a6a8 \
  0439fb2c92075ee5cdd5adf9bc54d2805de6670e; do
  rg -F -q -- "$candidate_commit" <<<"$task0_text" || exit 1
done

for handoff_task in 10 11 12; do
  handoff_text="$(task_section "$handoff_task")" || exit 1
  rg -F -q -- "$expected_handoff_tip" <<<"$handoff_text" || exit 1
done

task_counts=(51 15 5 15 15 39 32 44 12 18 14 3)
task_subjects=(
  "feat(observability): enforce safe JSON log schema"
  "feat(observability): add bounded optional OTLP bootstrap"
  "feat(observability): enforce telemetry metric cardinality"
  "feat(observability): trace API and database boundaries"
  "feat(observability): propagate traces through NATS"
  "feat(observability): trace Temporal service boundaries"
  "feat(observability): record committed agent and tool metrics"
  "feat(observability): trace connector and sandbox boundaries"
  "feat(web): emit safe versioned server logs"
  "feat(observability): add optional monitoring profile"
  "test(observability): prove end-to-end telemetry safety"
  "docs(observability): record Phase 10 telemetry evidence"
)
test "${#task_counts[@]}" -eq 12 || exit 1
test "${#task_subjects[@]}" -eq 12 || exit 1

task_number=1
while test "$task_number" -le 12; do
  index_number=$((task_number - 1))
  files="$(task_files "$task_number")" || exit 1
  index="$(task_index "$task_number")" || exit 1
  expected_count="${task_counts[$index_number]}"
  subject="${task_subjects[$index_number]}"
  files_count="$(printf '%s\n' "$files" | line_count)" || exit 1
  index_count="$(printf '%s\n' "$index" | line_count)" || exit 1
  test "$files_count" -eq "$expected_count" || exit 1
  test "$index_count" -eq "$expected_count" || exit 1
  test "$files" = "$index" || exit 1
  duplicate_paths="$(printf '%s\n' "$index" | LC_ALL=C sort | uniq -d)" || exit 1
  test -z "$duplicate_paths" || exit 1
  task_text="$(task_section "$task_number")" || exit 1
  rg -F -q -- "$subject" <<<"$task_text" || exit 1
  task_number=$((task_number + 1))
done

task10_expected="$(printf '%s\n' \
  .env.example \
  Makefile \
  compose.dev.yaml \
  compose.rootless.yaml \
  compose.yaml \
  docker/monitoring.Dockerfile \
  ops/observability/collector.yaml \
  ops/observability/grafana/dashboards/jhin-overview.json \
  ops/observability/grafana/provisioning/dashboards/jhin.yaml \
  ops/observability/grafana/provisioning/datasources/jhin.yaml \
  ops/observability/prometheus.yaml \
  ops/observability/tempo.yaml \
  scripts/assert_phase10_observability_compose.py \
  scripts/assert_phase10_tool_worker_compose.py \
  scripts/build_phase10_dashboard.py \
  tests/integration/phase10_upgrade_harness.py \
  tests/test_phase10_observability_compose.py \
  tests/test_phase10_tool_worker_compose.py | LC_ALL=C sort)" || exit 1
task10_actual="$(task_index 10)" || exit 1
test "$task10_actual" = "$task10_expected" || exit 1

task11_expected="$(printf '%s\n' \
  .github/workflows/ci.yml \
  Makefile \
  docs/operations/telemetry.md \
  packages/connectors/src/jhin_connectors/testing/fake_linear.py \
  packages/connectors/tests/linear/test_fake_linear_admin.py \
  packages/models/src/jhin_models/testing/fake_openai.py \
  packages/models/tests/test_fake_openai.py \
  scripts/phase10_artifact.py \
  tests/integration/conftest.py \
  tests/integration/emit_phase10_metrics.py \
  tests/integration/phase10_upgrade_harness.py \
  tests/integration/test_phase10_telemetry.py \
  tests/test_phase10_artifact.py \
  tests/test_phase10_telemetry_harness.py | LC_ALL=C sort)" || exit 1
task11_actual="$(task_index 11)" || exit 1
test "$task11_actual" = "$task11_expected" || exit 1

task12_expected="$(printf '%s\n' \
  docs/evidence/phase10-telemetry.md \
  scripts/record_phase10_telemetry_evidence.py \
  tests/test_phase10_telemetry_evidence.py | LC_ALL=C sort)" || exit 1
task12_actual="$(task_index 12)" || exit 1
test "$task12_actual" = "$task12_expected" || exit 1

rg -q 'head_sha.*pr_head' "$telemetry_plan" || exit 1
rg -q 'checkout_log_has_sha' "$telemetry_plan" || exit 1
rg -q 'git diff --quiet "\$checkpoint_commit" HEAD' "$protected_plan" || exit 1
rg -q 'require_rootless_socket' "$telemetry_plan" || exit 1
rg -q 'SocketMetadata\.capture' "$telemetry_plan" || exit 1
rg -q 'ComposeAuthority\.create.*observability' "$telemetry_plan" || exit 1
rg -q 'telemetry-api' "$telemetry_plan" || exit 1
rg -q 'telemetry-sandbox' "$telemetry_plan" || exit 1
rg -q 'UnderscoreEscapingWithoutSuffixes' "$telemetry_plan" || exit 1
rg -q 'task10_paths=' "$telemetry_plan" || exit 1
rg -q 'telemetry-base' "$telemetry_plan" || exit 1
rg -q 'telemetry-observed' "$telemetry_plan" || exit 1
rg -q 'expected_tests=12' "$telemetry_plan" || exit 1
rg -q 'validate_telemetry_status_rootful' "$telemetry_plan" || exit 1
rg -q 'validate_telemetry_status_rootless' "$telemetry_plan" || exit 1
rg -q 'sandbox_secret_env' "$telemetry_plan" || exit 1
rg -q 'task11_paths=' "$telemetry_plan" || exit 1
rg -q 'lock:grpcio' "$telemetry_plan" || exit 1
rg -q 'Dashboard/provisioning contract' "$telemetry_plan" || exit 1
rg -q 'phase10-rootful-live' "$telemetry_plan" || exit 1
rg -q 'phase10-rootless-live' "$telemetry_plan" || exit 1
rg -q 'unique_commit_with_subject' "$telemetry_plan" || exit 1
rg -q 'test_bash32_history_audit_fails_closed' "$telemetry_plan" || exit 1
rg -q 'task12_paths=' "$telemetry_plan" || exit 1
rg -q 'verify_final_telemetry_ci' "$protected_plan" || exit 1
rg -F -q -- 'health.heartbeat_write_failed' "$telemetry_plan" || exit 1
rg -F -q -- 'validated_sandbox_runner_base_url() -> str' "$telemetry_plan" || exit 1
cached_paths="$(git diff --cached --name-only)" || exit 1
test -z "$cached_paths" || exit 1
~~~

The five acceptance values are fixed to the mutually verified exact-head PR run, jobs, synthetic
merge, and shared tree. This combined structure gate, exact
two-plan staging equality, checkpoint subject/path equality, and empty-index postcondition must all
pass before committing the checkpoint.

## Completion Checklist

- [ ] `jhin_observability` is the sole Python logging/tracing/metric bootstrap and all services initialize it before clients/resources.
- [ ] Empty OTLP configuration is a true no-op for traces/metrics while JSON-v1 stdout remains active.
- [ ] Span/metric export is bounded, nonblocking, fail-open, and reports safe local drop/failure diagnostics.
- [ ] API, SQL, NATS, Temporal, model, gateway, connector, sandbox client/server/job, and durable commit boundaries are traced without forbidden payloads.
- [ ] Inbound baggage is discarded; W3C trace context and Jhin request/correlation/task/run context propagate only through the defined channels.
- [ ] Every required metric exists with exact semantics and per-instrument labels; retry/replay tests prove committed counters do not double-count.
- [ ] Every Python service and the Next.js server emit parseable schema-version-1 JSON stdout; stdlib logs use the same renderer.
- [ ] Known-value plus structural redaction occurs before export/persistence where applicable and again at JSON rendering.
- [ ] Optional Collector/Prometheus/Tempo/Grafana services are internal, credential-free, healthchecked, pinned, reconstructable, and absent from ordinary product requirements.
- [ ] Prometheus retains `15d`, Tempo retains `72h`, and application Docker logs retain `20m × 5`.
- [ ] Version-controlled Grafana data sources/dashboard cover every required metric and expose no high-cardinality identifier variable.
- [ ] Profile absence and Collector outage/recovery tests prove product availability and authority are unchanged.
- [ ] The connected webhook→task→agent manifest→tool-worker→connector/sandbox trace and exact Prometheus metrics pass with all telemetry canaries absent.
- [ ] CI runs deterministic unit/security/profile acceptance with fake providers only and uploads no unsanitized failure artifact.
- [ ] Dated evidence records exact commands, versions, results, and commit; product UI remains untouched for protected-health sub-project 3.
