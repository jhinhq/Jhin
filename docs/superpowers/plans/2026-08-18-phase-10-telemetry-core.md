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
- Task 0 intentionally stages the already-tracked amendment to `docs/superpowers/plans/2026-08-18-phase-10-protected-health.md` with this plan so the downstream plan cannot overwrite the interceptor-aware API Temporal provider or telemetry-owned integration harness. No later telemetry task edits or stages that plan.
- The user-owned untracked `orgforge-production-implementation-plan.md` must not be read, edited, staged, renamed, deleted, or committed. Do not stage either Phase 10 design spec unless its owner separately requests that action.
- Every implementation task follows RED → focused GREEN → affected suite → review of the exact diff → scoped commit. Never use `git add .`; stage only the paths named by that task after checking `git status --short` and `git diff --cached --name-only`.

## Shared Interfaces

These names are fixed across every task. Do not invent service-local alternatives.

```python
from typing import Literal, get_args

# packages/observability/src/jhin_observability/registry.py is the one registry;
# context.py and temporal.py import these names and never redeclare them.
TEMPORAL_ACTIVITY_NAMES = (
    "reason_agent_step", "commit_agent_step", "commit_approval_projection",
    "resolve_advertised_tools", "execute_bound_tool", "resolve_bound_tool_approval",
    "sync_external_tool", "cleanup_run_workspace", "resolve_snapshot",
    "run_agent_step", "resolve_approval", "finalize_run", "finalize_run_projection",
    "summarize_delegation", "deliver_delegation_result", "prepare_triggered_task",
    "sync_external", "resolve_engineering_plan", "create_engineering_child_task",
    "finalize_engineering_ticket", "record_beat",
)
SpanName = Literal[
    "http.server.request", "db.operation", "nats.publish", "nats.consume",
    "trigger.dispatch", "temporal.start_workflow", "temporal.signal_workflow",
    "temporal.client.other", "temporal.activity.other",
    "temporal.activity.reason_agent_step", "temporal.activity.commit_agent_step",
    "temporal.activity.commit_approval_projection",
    "temporal.activity.resolve_advertised_tools",
    "temporal.activity.execute_bound_tool",
    "temporal.activity.resolve_bound_tool_approval",
    "temporal.activity.sync_external_tool", "temporal.activity.cleanup_run_workspace",
    "temporal.activity.resolve_snapshot", "temporal.activity.run_agent_step",
    "temporal.activity.resolve_approval", "temporal.activity.finalize_run",
    "temporal.activity.finalize_run_projection",
    "temporal.activity.summarize_delegation",
    "temporal.activity.deliver_delegation_result",
    "temporal.activity.prepare_triggered_task", "temporal.activity.sync_external",
    "temporal.activity.resolve_engineering_plan",
    "temporal.activity.create_engineering_child_task",
    "temporal.activity.finalize_engineering_ticket", "temporal.activity.record_beat",
    "model.request", "agent.reason_step", "tool.gateway.execute",
    "tool.approval.resolve", "connector.http", "connector.database",
    "sandbox.client", "sandbox.server", "sandbox.job.lifecycle",
]
SPAN_NAMES: frozenset[str] = frozenset(get_args(SpanName))
AttributeValue = str | bool | int | float
MetricName = Literal[
    "agent_runs_total", "agent_run_duration_seconds", "agent_run_failures_total",
    "model_requests_total", "model_tokens_total", "model_cost_estimate",
    "tool_calls_total", "tool_call_failures_total", "trigger_invocations_total",
    "trigger_failures_total", "sandbox_jobs_total", "sandbox_job_duration_seconds",
    "nats_consumer_lag", "temporal_activity_failures", "connector_health",
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

```text
docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md

packages/observability/pyproject.toml
packages/observability/src/jhin_observability/__init__.py
packages/observability/src/jhin_observability/bootstrap.py
packages/observability/src/jhin_observability/config.py
packages/observability/src/jhin_observability/context.py
packages/observability/src/jhin_observability/errors.py
packages/observability/src/jhin_observability/events.py
packages/observability/src/jhin_observability/exporters.py
packages/observability/src/jhin_observability/logging.py
packages/observability/src/jhin_observability/metrics.py
packages/observability/src/jhin_observability/redaction.py
packages/observability/src/jhin_observability/registry.py
packages/observability/src/jhin_observability/sqlalchemy.py
packages/observability/src/jhin_observability/temporal.py
packages/observability/tests/conftest.py
packages/observability/tests/test_bootstrap.py
packages/observability/tests/test_context.py
packages/observability/tests/test_errors.py
packages/observability/tests/test_exporters.py
packages/observability/tests/test_log_audit.py
packages/observability/tests/test_logging.py
packages/observability/tests/test_metrics.py
packages/observability/tests/test_noop_metrics.py
packages/observability/tests/test_sqlalchemy.py
packages/observability/tests/test_temporal.py

apps/api/src/jhin_api/deps.py
apps/api/src/jhin_api/health/router.py
apps/api/src/jhin_api/health/service.py
apps/api/src/jhin_api/main.py
apps/api/src/jhin_api/models/router.py
apps/api/src/jhin_api/models/service.py
apps/api/src/jhin_api/seed.py
apps/api/src/jhin_api/settings.py
apps/api/src/jhin_api/temporal.py
apps/api/src/jhin_api/webhooks/service.py
apps/api/tests/test_observability.py
apps/api/tests/test_model_telemetry.py
apps/api/tests/test_temporal_provider.py
apps/web/Dockerfile
apps/web/instrumentation.ts
apps/web/lib/server-logger.ts
apps/web/next.config.ts
apps/web/server-wrapper.cjs
apps/web/tests/server-logger.test.ts
apps/web/tests/server-wrapper.test.ts

packages/db/pyproject.toml
packages/db/src/jhin_db/engine.py
packages/db/tests/test_observability.py
packages/events/pyproject.toml
packages/events/src/jhin_events/consumer.py
packages/events/src/jhin_events/publisher.py
packages/events/src/jhin_events/telemetry.py
packages/events/tests/test_telemetry.py
packages/models/pyproject.toml
packages/models/src/jhin_models/factory.py
packages/models/src/jhin_models/telemetry.py
packages/models/src/jhin_models/testing/fake_openai.py
packages/models/tests/test_fake_openai.py
packages/models/tests/test_telemetry.py
packages/secrets/src/jhin_secrets/crypto.py
packages/tools/pyproject.toml
packages/tools/src/jhin_tools/telemetry.py
packages/tools/tests/test_telemetry.py

packages/connectors/pyproject.toml
packages/connectors/src/jhin_connectors/cli/runner_client.py
packages/connectors/src/jhin_connectors/github/auth.py
packages/connectors/src/jhin_connectors/github/client.py
packages/connectors/src/jhin_connectors/http_client.py
packages/connectors/src/jhin_connectors/linear/client.py
packages/connectors/src/jhin_connectors/registry.py
packages/connectors/src/jhin_connectors/supabase/management_client.py
packages/connectors/src/jhin_connectors/supabase/database_client.py
packages/connectors/src/jhin_connectors/supabase/database_tools.py
packages/connectors/src/jhin_connectors/telemetry.py
packages/connectors/src/jhin_connectors/testing/fake_linear.py
packages/connectors/src/jhin_connectors/vercel/client.py
packages/connectors/tests/test_telemetry.py
packages/connectors/tests/test_http_client.py
packages/connectors/tests/linear/test_fake_linear_admin.py
packages/connectors/tests/supabase/test_database_telemetry.py

services/agent_worker/src/jhin_agent_worker/activities.py
services/agent_worker/src/jhin_agent_worker/engineering_activities.py
services/agent_worker/src/jhin_agent_worker/main.py
services/agent_worker/src/jhin_agent_worker/projections.py
services/agent_worker/src/jhin_agent_worker/reasoning.py
services/agent_worker/src/jhin_agent_worker/resources.py
services/agent_worker/src/jhin_agent_worker/settings.py
services/agent_worker/src/jhin_agent_worker/trigger_activities.py
services/agent_worker/tests/test_telemetry.py
services/tool_worker/pyproject.toml
services/tool_worker/src/jhin_tool_worker/activities.py
services/tool_worker/src/jhin_tool_worker/main.py
services/tool_worker/src/jhin_tool_worker/resources.py
services/tool_worker/src/jhin_tool_worker/settings.py
services/tool_worker/tests/test_telemetry.py
services/tool_worker/tests/test_worker_registration.py
services/event_worker/pyproject.toml
services/event_worker/src/jhin_event_worker/main.py
services/event_worker/src/jhin_event_worker/matcher.py
services/event_worker/src/jhin_event_worker/normalizer.py
services/event_worker/src/jhin_event_worker/processor.py
services/event_worker/src/jhin_event_worker/settings.py
services/event_worker/tests/test_telemetry.py
services/workflow_worker/pyproject.toml
services/workflow_worker/src/jhin_workflow_worker/main.py
services/workflow_worker/src/jhin_workflow_worker/settings.py
services/workflow_worker/tests/test_telemetry.py
services/sandbox_runner/src/jhin_sandbox_runner/jobs.py
services/sandbox_runner/src/jhin_sandbox_runner/main.py
services/sandbox_runner/src/jhin_sandbox_runner/settings.py
services/sandbox_runner/tests/test_telemetry.py
packages/workflows/src/jhin_workflows/heartbeat/activities.py
packages/workflows/src/jhin_workflows/poller_health.py
packages/workflows/tests/test_poller_health.py
packages/workflows/pyproject.toml

docker/monitoring.Dockerfile
ops/observability/collector.yaml
ops/observability/prometheus.yaml
ops/observability/tempo.yaml
ops/observability/grafana/provisioning/datasources/jhin.yaml
ops/observability/grafana/provisioning/dashboards/jhin.yaml
ops/observability/grafana/dashboards/jhin-overview.json
scripts/assert_phase10_observability_compose.py
scripts/assert_phase10_tool_worker_compose.py
scripts/audit_phase10_logging.py
scripts/build_phase10_dashboard.py
scripts/phase10_artifact.py
scripts/record_phase10_telemetry_evidence.py
tests/test_phase10_artifact.py
tests/test_phase10_observability_compose.py
tests/test_phase10_tool_worker_compose.py
tests/test_phase10_telemetry_evidence.py
tests/test_worker_dependency_boundaries.py
tests/integration/conftest.py
tests/integration/emit_phase10_metrics.py
tests/integration/test_phase10_telemetry.py
tests/integration/test_seed.py
tests/test_phase10_telemetry_harness.py
tests/test_web_json_stdout.py

.env.example
.github/workflows/ci.yml
Makefile
compose.dev.yaml
compose.yaml
docs/evidence/phase10-telemetry.md
docs/operations/telemetry.md
pyproject.toml
uv.lock
```

This list is exhaustive. Each task's `Files` block assigns the create/modify action and commit boundary for its subset; implementation must not add a telemetry file outside this map without first amending and reviewing the plan.

---

### Task 0: Check In the Reviewed Telemetry Execution Baseline

**Files:**
- Create: `docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md`
- Modify: `docs/superpowers/plans/2026-08-18-phase-10-protected-health.md`

**Interfaces:**
- Consumes: the corrected Phase 10 design, the committed `50d3261` protected-health baseline and its reviewed telemetry-handoff amendment, and a completed, accepted tool-worker-boundary predecessor.
- Produces: one two-plan checkpoint commit against which every telemetry and protected-health handoff is reviewed.

- [ ] **Step 1: Validate the predecessor's committed acceptance artifact before any telemetry edit**

The predecessor's committed acceptance artifact is its exact Task 10 commit—not an untracked log or
a prose PASS claim. Run from a checkout containing the completed predecessor:

```bash
set -euo pipefail
for path in \
  services/tool_worker/src/jhin_tool_worker/__init__.py \
  services/tool_worker/src/jhin_tool_worker/settings.py \
  services/tool_worker/src/jhin_tool_worker/resources.py \
  services/tool_worker/src/jhin_tool_worker/activities.py \
  services/tool_worker/src/jhin_tool_worker/main.py \
  services/tool_worker/tests/test_worker_registration.py \
  tests/test_worker_dependency_boundaries.py \
  tests/test_phase10_tool_worker_compose.py \
  scripts/assert_phase10_tool_worker_compose.py \
  compose.rootful.yaml compose.rootless.yaml; do
  test -f "$path"
done

mapfile -t acceptance_commits < <(git log --format=%H \
  --fixed-strings --grep='test: verify Phase 10 tool-worker boundary')
test "${#acceptance_commits[@]}" -eq 1
acceptance_commit="${acceptance_commits[0]}"
git merge-base --is-ancestor "$acceptance_commit" HEAD
test "$(git show -s --format=%s "$acceptance_commit")" = \
  "test: verify Phase 10 tool-worker boundary"
test "$(git diff-tree --no-commit-id --name-only -r "$acceptance_commit" | LC_ALL=C sort)" = \
  "$(printf '%s\n' \
    .github/workflows/ci.yml \
    Makefile \
    tests/integration/compose.phase10-upgrade.yaml \
    tests/integration/phase10_upgrade_harness.py \
    tests/integration/test_phase10_live_upgrade.py \
    tests/integration/test_phase10_sandbox_socket_modes.py \
    tests/integration/test_phase10_tool_worker_boundary.py \
    tests/integration/test_phase3_exit.py \
    tests/integration/test_phase6_exit.py \
    tests/integration/test_phase7_exit.py \
    tests/integration/test_phase9_exit.py | LC_ALL=C sort)"
git grep -q '^test-tool-worker-boundary-integration:' "$acceptance_commit" -- Makefile
git grep -q '^test-tool-worker-live-upgrade:' "$acceptance_commit" -- Makefile
git grep -q '^test-sandbox-socket-rootful:' "$acceptance_commit" -- Makefile
git grep -q '^test-sandbox-socket-rootless:' "$acceptance_commit" -- Makefile
git grep -q '^test-sandbox-socket-wrong-gid:' "$acceptance_commit" -- Makefile
git grep -q 'phase10-live-upgrade:' "$acceptance_commit" -- .github/workflows/ci.yml
```

Expected: PASS and exactly one ancestor commit with the predecessor Task 10 subject and eleven-file
acceptance diff. Any missing path, target, CI job, extra commit path, or non-ancestor commit exits
nonzero before either plan is staged. Do not substitute an uncommitted worktree test or handwritten
evidence file for this artifact.

- [ ] **Step 2: Execute every predecessor live acceptance target on the supported Linux host**

This is a hard gate, not an optional rerun. The host must provide both the rootful Docker socket and
the predecessor's UID-10001 rootless socket:

```bash
set -euo pipefail
test -S /var/run/docker.sock
socket_gid="$(stat -c %g /var/run/docker.sock)"
test "$socket_gid" -gt 0
test -n "${PHASE10_ROOTLESS_DOCKER_SOCKET:-}"
test -S "$PHASE10_ROOTLESS_DOCKER_SOCKET"

SANDBOX_DOCKER_GID="$socket_gid" PHASE10_SOCKET_MODE=rootful \
  make test-tool-worker-boundary-integration
SANDBOX_DOCKER_GID="$socket_gid" PHASE10_SOCKET_MODE=rootful \
  make test-tool-worker-live-upgrade
SANDBOX_DOCKER_GID="$socket_gid" PHASE10_SOCKET_MODE=rootful \
  make test-sandbox-socket-rootful
env -u SANDBOX_DOCKER_GID \
  PHASE10_ROOTLESS_DOCKER_SOCKET="$PHASE10_ROOTLESS_DOCKER_SOCKET" \
  PHASE10_SOCKET_MODE=rootless make test-sandbox-socket-rootless
SANDBOX_DOCKER_GID="$socket_gid" PHASE10_SOCKET_MODE=wrong-gid \
  make test-sandbox-socket-wrong-gid
```

Expected: all five targets PASS with fresh results: live boundary/crash recovery, true Phase 9→10
upgrade, rootful socket access, rootless socket access, and wrong-GID fail-closed behavior. Because
the shell is `set -euo pipefail`, the first failure stops Task 0; do not stage either plan, skip a
socket mode, reuse render-only GID `10001`, or continue to Task 1.

- [ ] **Step 3: Verify the two-plan handoff and stage exactly those two paths**

Run:

```bash
set -euo pipefail
git merge-base --is-ancestor 50d3261 HEAD
git ls-files --error-unmatch \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
test -f docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md
test "$(git status --short -- \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md)" = \
  " M docs/superpowers/plans/2026-08-18-phase-10-protected-health.md"
test "$(git status --short -- \
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md)" = \
  "?? docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md"
rg -q 'TemporalClientProvider' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
rg -q 'temporal_client_interceptors\(self\._observability\)' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
rg -q 'TelemetryExporterStatus' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
rg -q 'tests/test_phase10_telemetry_harness\.py' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = \
  "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test -z "$(git diff --cached --name-only)"
git add docs/superpowers/plans/2026-08-18-phase-10-protected-health.md \
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md
test "$(git diff --cached --name-only)" = "$(printf '%s\n' \
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md \
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md)"
git diff --cached --check
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
```

Expected: the protected-health plan is the tracked amendment based on `50d3261`, its provider,
interceptor/status, and shared-harness handoffs are present, and the cached-name output is exactly
the two plan paths in the command. The OrgForge file retains its exact hash and 82,118-byte size and
remains untracked and unstaged.

- [ ] **Step 4: Commit the two-plan checkpoint**

```bash
git commit -m "docs: align Phase 10 telemetry and health plans"
```

Expected: one documentation-only commit containing exactly the two plan paths; neither Phase 10
spec nor `orgforge-production-implementation-plan.md` is included. This remains one Task 0 commit,
so the complete telemetry plan still contains 13 commits total.

### Task 1: Enforce the JSON-v1 Log and Safe-Error Boundary

**Files:**
- Create: `packages/observability/src/jhin_observability/events.py`
- Create: `packages/observability/src/jhin_observability/redaction.py`
- Create: `packages/observability/src/jhin_observability/errors.py`
- Modify: `packages/observability/src/jhin_observability/logging.py`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/tests/test_logging.py`
- Create: `packages/observability/tests/test_errors.py`
- Create: `packages/observability/tests/test_log_audit.py`
- Create: `scripts/audit_phase10_logging.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/src/jhin_api/webhooks/service.py`
- Modify: `packages/events/src/jhin_events/consumer.py`
- Modify: `packages/secrets/src/jhin_secrets/crypto.py`
- Modify: `packages/workflows/src/jhin_workflows/heartbeat/activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/engineering_activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/trigger_activities.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/activities.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/matcher.py`
- Modify: `services/event_worker/src/jhin_event_worker/normalizer.py`
- Modify: `services/event_worker/src/jhin_event_worker/processor.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/main.py`

**Interfaces:**
- Consumes: optional service-supplied value redactors with structlog `Processor` signature.
- Produces: `LOG_SCHEMA_VERSION = 1`, exact `EVENT_FIELD_RULES`, `filter_log_event(...)`, `structural_redaction(...)`, `safe_error(...)`, `configure_json_logging(...)`, and `get_logger(...)`; later bootstrap calls the logger before OTel setup.

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
    assert set(("schema_version", "timestamp", "level", "service", "environment", "logger")) <= record


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
    assert filter_log_event(
        {"event": "telemetry.export_failed", "error_code": accepted}
    )["error_code"] == accepted


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
    assert filter_log_event(
        {"event": "api.request_failed", "error": structured}
    )["error"]["type"] == "RuntimeError"
    assert "error" not in filter_log_event(
        {"event": "worker.started", "error": structured}
    )
```

```python
@pytest.mark.parametrize(
    "key",
    [
        "prompt", "completion", "sql", "tool_input", "tool_output",
        "request_body", "response_body", "webhook_payload", "secret_env",
    ],
)
def test_payload_fields_are_always_redacted(key: str) -> None:
    assert structural_redaction({key: "payload-canary"}) == {key: "[REDACTED]"}


def test_redaction_bounds_are_exact() -> None:
    nested: object = "leaf"
    for _ in range(9):
        nested = {"child": nested}
    redacted = structural_redaction(
        {"nested": nested, "mapping": {str(i): i for i in range(65)},
         "items": list(range(65)), "text": "x" * 2_001}
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
        "authorization", "cookie", "password", "secret", "token", "api_key",
        "private_key", "dsn", "prompt", "completion", "sql", "tool_input",
        "tool_output", "request_body", "response_body", "webhook_payload", "secret_env",
    }
)
SENSITIVE_KEY_SUFFIXES = (
    "_authorization", "_cookie", "_password", "_secret", "_token", "_api_key",
    "_private_key", "_dsn",
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
        "error_code": FieldKind.ENUM, "error": FieldKind.ERROR,
    },
    "api.request_finished": {
        "http_method": FieldKind.ENUM, "http_route": FieldKind.ENUM,
        "http_status_class": FieldKind.ENUM,
    },
    "secrets.master_key_unavailable": {"error_code": FieldKind.ENUM},
    "security.master_key_env_source": {},
    "temporal.connect_retry": {
        "error_type": FieldKind.ERROR_TYPE, "retry_in_seconds": FieldKind.SECONDS,
    },
    "temporal.connected": {"task_queue": FieldKind.ENUM},
    "resources.retry": {
        "error_type": FieldKind.ERROR_TYPE, "retry_in_seconds": FieldKind.SECONDS,
    },
    "resources.ready": {},
    "nats.connect_retry": {
        "error_type": FieldKind.ERROR_TYPE, "retry_in_seconds": FieldKind.SECONDS,
    },
    "nats.connected": {"stream": FieldKind.ENUM},
    "worker.started": {"task_queue": FieldKind.ENUM},
    "worker.stopping": {},
    "events.publish_failed": {
        "event_type": FieldKind.ENUM, "error_type": FieldKind.ERROR_TYPE,
    },
    "concurrency.kick_failed": {"error_type": FieldKind.ERROR_TYPE},
    "model.client_close_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox.workspace_cleanup": {"deleted": FieldKind.BOOL},
    "sandbox.network_created": {"network_policy": FieldKind.ENUM},
    "sandbox.network_ensure_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox.job.finished": {
        "job_id": FieldKind.ID,
        "outcome": FieldKind.ENUM, "exit_code": FieldKind.COUNT,
        "network_policy": FieldKind.ENUM,
    },
    "sandbox.reaped_container": {"count": FieldKind.COUNT},
    "sandbox.reaped_workspace": {"count": FieldKind.COUNT},
    "sandbox.reap_containers_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox.reap_volumes_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox_runner.started": {
        "network_policy": FieldKind.ENUM, "token_configured": FieldKind.BOOL,
    },
    "trigger.task_deduped": {},
    "trigger.invoked": {"connector_type": FieldKind.ENUM, "outcome": FieldKind.ENUM},
    "trigger.duplicate_suppressed": {"connector_type": FieldKind.ENUM},
    "trigger.no_agent": {"connector_type": FieldKind.ENUM},
    "trigger.workflow_already_started": {"connector_type": FieldKind.ENUM},
    "webhook.accepted": {"connector_type": FieldKind.ENUM, "outcome": FieldKind.ENUM},
    "webhook.publish_or_commit_failed": {
        "connector_type": FieldKind.ENUM, "error_type": FieldKind.ERROR_TYPE,
    },
    "webhook.rollback_failed": {"connector_type": FieldKind.ENUM},
    "jetstream.consumer_created": {"stream": FieldKind.ENUM, "consumer": FieldKind.ENUM},
    "jetstream.consumer_loop_started": {
        "stream": FieldKind.ENUM, "consumer": FieldKind.ENUM,
    },
    "jetstream.consumer_handler_failed": {
        "stream": FieldKind.ENUM, "consumer": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE, "error_code": FieldKind.ENUM,
        "error": FieldKind.ERROR,
    },
    "heartbeat.recorded": {},
    "ingress.invalid_envelope": {"error_code": FieldKind.ENUM},
    "ingress.unhandled": {"connector_type": FieldKind.ENUM, "event_type": FieldKind.ENUM},
    "ingress.normalized": {
        "connector_type": FieldKind.ENUM, "event_type": FieldKind.ENUM,
        "produced": FieldKind.COUNT,
    },
    "event.invalid_envelope": {"error_code": FieldKind.ENUM},
    "event.duplicate_skipped": {"num_delivered": FieldKind.COUNT},
    "event.processed": {"event_type": FieldKind.ENUM, "num_delivered": FieldKind.COUNT},
    "telemetry.queue_dropped": {"count": FieldKind.COUNT, "queue_capacity": FieldKind.COUNT},
    "telemetry.export_failed": {"error_code": FieldKind.ENUM},
    "telemetry.export_recovered": {},
    "telemetry.nats_lag_probe_failed": {
        "stream": FieldKind.ENUM, "consumer": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
    },
    "telemetry.connector_health_probe_failed": {"error_type": FieldKind.ERROR_TYPE},
    "web.started": {},
    "web.stopping": {"signal": FieldKind.ENUM},
    "web.rewrite_configured": {"http_route": FieldKind.ENUM},
    "web.request_failed": {
        "http_method": FieldKind.ENUM, "http_route": FieldKind.ENUM,
        "error_code": FieldKind.ENUM,
    },
    "web.framework_output_suppressed": {
        "stream": FieldKind.ENUM, "count": FieldKind.COUNT,
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
    "outcome": frozenset({"ok", "accepted", "started", "completed", "failed", "cancelled", "timeout", "duplicate", "other"}),
    "signal": frozenset({"SIGINT", "SIGTERM", "other"}),
    "stream": frozenset({"INGRESS", "EVENTS", "stdout", "stderr", "other"}),
    "task_queue": frozenset({"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue", "other"}),
}
EVENT_FIELD_ENUM_VALUES: dict[tuple[str, str], frozenset[str]] = {
    ("telemetry.export_failed", "error_code"): frozenset(
        {"export_timeout", "export_failed"}
    ),
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
BASE_FIELDS = frozenset({"schema_version", "timestamp", "level", "service", "environment", "event", "logger"})


def normalize_log_field(
    event: str, key: str, value: object, kind: FieldKind
) -> object | None:
    if kind is FieldKind.ID:
        return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None
    if kind is FieldKind.COUNT:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    if kind is FieldKind.SECONDS:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
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
                    "file": filename if _ERROR_TYPE_RE.fullmatch(filename.replace("-", "_")) else "unknown",
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
    event = raw_event if isinstance(raw_event, str) and raw_event in EVENT_FIELD_RULES else "log.event_rejected"
    output = {key: event_dict[key] for key in BASE_FIELDS - {"event"} if key in event_dict}
    output["event"] = event
    rules = {**CONTEXT_FIELD_RULES, **EVENT_FIELD_RULES[event]}
    for key, kind in rules.items():
        if key in event_dict and (
            value := normalize_log_field(event, key, event_dict[key], kind)
        ) is not None:
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
        "dynamic_event", "unregistered_event", "positional_text", "unregistered_field",
        "direct_print", "direct_stream_write", "foreign_logging",
        "unresolved_logger_receiver",
    ]


AUDIT_EXCLUDED_PARTS = frozenset({"tests", "testing", "alembic", "__pycache__"})
AUDIT_EXCLUDED_FILES = frozenset({"seed.py", "migrate.py"})


def application_python_paths(root: Path) -> tuple[Path, ...]:
    source_roots = (root / "apps/api/src", root / "packages", root / "services")
    return tuple(sorted(
        path
        for source_root in source_roots
        for path in source_root.rglob("*.py")
        if not set(path.parts) & AUDIT_EXCLUDED_PARTS
        and path.name not in AUDIT_EXCLUDED_FILES
    ))


LOGGER_METHODS = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"
})


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


LOGGER_FACTORIES = frozenset({
    "structlog.get_logger", "logging.getLogger", "jhin_observability.get_logger",
    "jhin_observability.logging.get_logger",
})


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
        and any(isinstance(target, ast.Name) and target.id == "container" for target in candidate.targets)
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
        ) or (
            function.name == "current_logs" and _assigns_container_lookup(function)
        )
    return (
        relative.endswith(
            "packages/connectors/src/jhin_connectors/supabase/database_tools.py"
        )
        and function.name == "consume_result"
        and receiver == "completed"
        and method == "exception"
        and _has_parameter(
            function, name="completed", annotation="asyncio.Future[Any]"
        )
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
                        path, node.lineno, node.col_offset, method,
                        cast(Literal["logger", "foreign_logging", "unresolved_logger_receiver"], kind),
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
                isinstance(receiver_node, ast.Name) and receiver_node.id in bindings
            ) or receiver_name == "temporalio.activity.logger" or direct_factory:
                kind = "logger"
            elif receiver_name == "logging":
                kind = "foreign_logging"
            else:
                # Fail closed on every logging-method-shaped call. Add an explicit
                # non-logger exemption above only after a repository-wide review.
                kind = "unresolved_logger_receiver"
            calls.append(LoggingCall(
                path, node.lineno, node.col_offset, method,
                cast(Literal["logger", "foreign_logging", "unresolved_logger_receiver"], kind),
            ))
    return tuple(sorted(
        calls,
        key=lambda item: (item.path.as_posix(), item.line, item.column, item.method),
    ))


def audit_paths(paths: Sequence[Path]) -> list[AuditFailure]:
    calls = collect_logging_method_calls(paths)
    by_location = {
        (item.path, item.line, item.column, item.method): item for item in calls
    }
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
    assert [failure.code for failure in audit_paths((source,))] == [
        "unresolved_logger_receiver"
    ]


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
logger.warning(
    "temporal.connect_retry", error_type=type(exc).__name__, retry_in_seconds=delay
)
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
logger.warning(
    "ingress.unhandled", connector_type=connector_type, event_type=event_family
)
logger.info(
    "ingress.normalized",
    connector_type=connector_type,
    event_type=event_family,
    produced=len(normalized),
)
logger.error("event.invalid_envelope", error_code=SafeErrorCode.INVALID_REQUEST.value)
logger.info("event.duplicate_skipped", num_delivered=metadata.num_delivered)
logger.info(
    "event.processed", event_type=event_family, num_delivered=metadata.num_delivered
)
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

```bash
git diff --check
git diff -- packages/observability scripts/audit_phase10_logging.py apps/api/src \
  packages/events/src packages/secrets/src packages/workflows/src services
git add packages/observability/src/jhin_observability/__init__.py \
  packages/observability/src/jhin_observability/events.py \
  packages/observability/src/jhin_observability/errors.py \
  packages/observability/src/jhin_observability/logging.py \
  packages/observability/src/jhin_observability/redaction.py \
  packages/observability/tests/test_errors.py \
  packages/observability/tests/test_log_audit.py \
  packages/observability/tests/test_logging.py scripts/audit_phase10_logging.py \
  apps/api/src/jhin_api/main.py apps/api/src/jhin_api/webhooks/service.py \
  packages/events/src/jhin_events/consumer.py packages/secrets/src/jhin_secrets/crypto.py \
  packages/workflows/src/jhin_workflows/heartbeat/activities.py \
  services/agent_worker/src/jhin_agent_worker/activities.py \
  services/agent_worker/src/jhin_agent_worker/engineering_activities.py \
  services/agent_worker/src/jhin_agent_worker/main.py \
  services/agent_worker/src/jhin_agent_worker/resources.py \
  services/agent_worker/src/jhin_agent_worker/trigger_activities.py \
  services/tool_worker/src/jhin_tool_worker/activities.py \
  services/tool_worker/src/jhin_tool_worker/main.py \
  services/tool_worker/src/jhin_tool_worker/resources.py \
  services/event_worker/src/jhin_event_worker/main.py \
  services/event_worker/src/jhin_event_worker/matcher.py \
  services/event_worker/src/jhin_event_worker/normalizer.py \
  services/event_worker/src/jhin_event_worker/processor.py \
  services/sandbox_runner/src/jhin_sandbox_runner/jobs.py \
  services/sandbox_runner/src/jhin_sandbox_runner/main.py \
  services/workflow_worker/src/jhin_workflow_worker/main.py
git diff --cached --name-only
git commit -m "feat(observability): enforce safe JSON log schema"
```

Expected: only Task 1 files are committed.

### Task 2: Build the Optional No-Op/OTLP Bootstrap and Bounded Exporters

**Files:**
- Create: `packages/observability/src/jhin_observability/config.py`
- Create: `packages/observability/src/jhin_observability/exporters.py`
- Create: `packages/observability/src/jhin_observability/bootstrap.py`
- Create: `packages/observability/src/jhin_observability/context.py`
- Create: `packages/observability/src/jhin_observability/metrics.py`
- Create: `packages/observability/src/jhin_observability/registry.py`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/pyproject.toml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `packages/observability/tests/conftest.py`
- Create: `packages/observability/tests/test_bootstrap.py`
- Create: `packages/observability/tests/test_context.py`
- Create: `packages/observability/tests/test_exporters.py`
- Create: `packages/observability/tests/test_noop_metrics.py`

**Interfaces:**
- Consumes: Task 1 `configure_json_logging`, structlog processors, and OTel API/SDK exporters.
- Produces: every interface in Shared Interfaces, including a dependency-free no-op `JhinMetrics` facade available before OTel/bootstrap configuration; Task 3 replaces only its configured registry internals.

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
def test_empty_endpoint_installs_noop_telemetry_but_json_logging(capsys: CaptureFixture[str]) -> None:
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
        service_name="api", service_version="0.1.0", environment="test",
        otlp_endpoint=endpoint, otlp_insecure=insecure,
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
    assert output["traceparent"].startswith(
        "00-4bf92f3577b34da6a3ce929d0e0e4736-"
    )
    assert "carrier-canary" not in json.dumps(output)


def test_initialize_is_idempotent_only_for_the_same_config() -> None:
    config = ObservabilityConfig(
        service_name="api", service_version="0.1.0", environment="test"
    )
    assert initialize_observability(config) is initialize_observability(config)
    with pytest.raises(ObservabilityConfigurationError, match="already initialized"):
        initialize_observability(replace(config, service_name="agent-worker"))


def test_noop_metrics_is_available_without_bootstrap_or_exporter_imports() -> None:
    metrics = noop_metrics()
    metrics.counter("model_requests_total").add(
        1, provider_type="openai", outcome="ok"
    )
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
    activity_spans = {
        name for name in SPAN_NAMES if name.startswith("temporal.activity.")
    }
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

    config = ObservabilityConfig(
        service_name="api", service_version="0.1.0", environment="test"
    )
    runtime = initialize_observability(config)
    assert runtime.config is config
    assert isinstance(runtime, ObservabilityRuntime)
    assert [field.name for field in fields(TelemetryExporterStatus)] == [
        "configured", "last_success_at", "dropped_items", "last_error_code"
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
            local_cleartext = parsed.hostname in {
                "otel-collector", "localhost", "127.0.0.1", "::1"
            }
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
    otel_traces_sampler: Literal[
        "always_on", "always_off", "parentbased_traceidratio"
    ] = "parentbased_traceidratio"
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
    "agent_runs_total", "agent_run_duration_seconds", "agent_run_failures_total",
    "model_requests_total", "model_tokens_total", "model_cost_estimate",
    "tool_calls_total", "tool_call_failures_total", "trigger_invocations_total",
    "trigger_failures_total", "sandbox_jobs_total", "sandbox_job_duration_seconds",
    "nats_consumer_lag", "temporal_activity_failures", "connector_health",
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
        "http.request.method", "http.route", "http.response.status_code",
        "http.response.status_class", "db.system", "db.operation", "db.table",
        "messaging.system", "jhin.stream", "jhin.consumer", "jhin.subject_family", "jhin.provider_type",
        "jhin.connector_type", "jhin.operation", "jhin.outcome", "jhin.latency_ms",
        "jhin.retry_count", "jhin.tool_family", "jhin.risk", "jhin.network_policy",
        "jhin.request_id", "jhin.correlation_id", "jhin.workspace_id", "jhin.task_id",
        "jhin.run_id", "jhin.job_id", "temporal.workflow_id", "temporal.run_id",
        "temporal.task_queue", "temporal.workflow_type", "temporal.activity_type",
        "temporal.attempt", "error.type", "error.code",
    }
)

SPAN_ID_ATTRIBUTE_KEYS = frozenset({
    "jhin.request_id", "jhin.correlation_id", "jhin.workspace_id", "jhin.task_id",
    "jhin.run_id", "jhin.job_id", "temporal.workflow_id", "temporal.run_id",
})
SPAN_NUMERIC_ATTRIBUTE_KEYS = frozenset({
    "http.response.status_code", "jhin.latency_ms", "jhin.retry_count", "temporal.attempt",
})
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
                value
                if "://" not in value and _SAFE_SPAN_STRING_RE.fullmatch(value)
                else "other"
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
        FailingSpanExporter(), diagnostics=ExportDiagnostics(), max_queue_size=4,
        max_export_batch_size=2, export_timeout_millis=25,
    )
    processor.on_end(readable_span(1))
    assert processor.force_flush(timeout_millis=100) is True
    status = processor.diagnostics.snapshot()
    assert status.last_error_code == "export_failed"
    assert "telemetry.export_failed" in capsys.readouterr().out


def test_force_flush_and_shutdown_obey_deadline_when_exporter_is_blocked() -> None:
    exporter = ReleasableBlockingSpanExporter()
    processor = BoundedBatchSpanProcessor(
        exporter, diagnostics=ExportDiagnostics(), max_queue_size=4,
        max_export_batch_size=1, export_timeout_millis=25,
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

    def record_failure(
        self, code: Literal["export_timeout", "export_failed"]
    ) -> None:
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

```bash
git diff --check
git add pyproject.toml packages/observability/pyproject.toml uv.lock \
  packages/observability/src/jhin_observability/__init__.py \
  packages/observability/src/jhin_observability/bootstrap.py \
  packages/observability/src/jhin_observability/config.py \
  packages/observability/src/jhin_observability/context.py \
  packages/observability/src/jhin_observability/exporters.py \
  packages/observability/src/jhin_observability/metrics.py \
  packages/observability/src/jhin_observability/registry.py \
  packages/observability/tests/conftest.py \
  packages/observability/tests/test_bootstrap.py \
  packages/observability/tests/test_context.py \
  packages/observability/tests/test_exporters.py \
  packages/observability/tests/test_noop_metrics.py
git diff --cached --name-only
git commit -m "feat(observability): add bounded optional OTLP bootstrap"
```

### Task 3: Define Every Required Metric and Enforce Cardinality

**Files:**
- Modify: `packages/observability/src/jhin_observability/metrics.py`
- Modify: `packages/observability/src/jhin_observability/bootstrap.py`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Create: `packages/observability/tests/test_metrics.py`

**Interfaces:**
- Consumes: Task 2 `MeterProvider` and immutable service/environment resource identity.
- Produces: one `JhinMetrics` registry, typed counter/histogram handles, cached observable gauges, label validation, and helpers consumed by Tasks 5–8.

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
        "counter", "{failure}", {"task_queue", "activity", "failure_class"}
    ),
    "connector_health": ("gauge", "1", {"connector_type"}),
    "connector_connections": ("gauge", "{connection}", {"connector_type", "outcome"}),
}


def test_registry_exactly_matches_required_contract() -> None:
    assert instrument_contracts() == EXPECTED


@pytest.mark.parametrize(
    "forbidden",
    [
        "workspace_id", "user_id", "agent_id", "team_id", "task_id", "run_id",
        "event_id", "message_id", "connection_id", "approval_id", "tool_call_id",
        "sandbox_job_id", "request_id", "correlation_id", "trace_id", "url",
        "hostname", "repository", "project", "model_name",
    ],
)
def test_every_identifier_label_is_rejected(forbidden: str) -> None:
    metrics = test_metrics()
    with pytest.raises(MetricLabelError, match=forbidden):
        metrics.counter("agent_runs_total").add(1, service="agent-worker", outcome="completed", **{forbidden: "x"})


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
        "service", "environment", "outcome", "failure_class", "provider_type",
        "connector_type", "tool_family", "risk", "network_policy", "stream",
        "consumer", "task_queue", "activity", "http_method", "http_route",
        "http_status_class", "direction",
    }
)
FORBIDDEN_IDENTIFIER_LABELS = frozenset(
    {
        "workspace_id", "user_id", "agent_id", "team_id", "task_id", "run_id",
        "event_id", "message_id", "connection_id", "approval_id", "tool_call_id",
        "sandbox_job_id", "request_id", "correlation_id", "trace_id", "url", "hostname",
        "repository", "project", "model_name",
    }
)
LABEL_VALUES: dict[str, frozenset[str]] = {
    "service": frozenset(
        {"api", "agent-worker", "tool-worker", "event-worker", "workflow-worker", "sandbox-runner", "web"}
    ),
    "environment": frozenset({"dev", "test", "staging", "production"}),
    "outcome": frozenset(
        {"ok", "started", "completed", "failed", "cancelled", "timeout", "denied", "rejected", "duplicate", "execution_unknown", "healthy", "unhealthy", "other"}
    ),
    "failure_class": frozenset(
        {"authentication", "authorization", "validation", "rate_limit", "timeout", "transport", "dispatch", "target", "provider", "policy", "budget", "execution_unknown", "internal", "other"}
    ),
    "provider_type": frozenset(
        {"openai", "anthropic", "openrouter", "ollama", "openai_compatible", "other"}
    ),
    "connector_type": frozenset({"github", "linear", "vercel", "supabase", "cli", "other"}),
    "tool_family": frozenset({"system", "organization", "github", "linear", "vercel", "supabase", "cli", "other"}),
    "risk": frozenset({"read", "write", "elevated", "destructive", "other"}),
    "network_policy": frozenset({"none", "internet", "other"}),
    "stream": frozenset({"INGRESS", "EVENTS", "other"}),
    "consumer": frozenset({"event-worker-ingress", "event-worker", "other"}),
    "task_queue": frozenset({"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue", "other"}),
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


METRIC_SPECS: Mapping[MetricName, MetricSpec] = MappingProxyType({
    "agent_runs_total": _spec("counter", "{run}", "service", "outcome"),
    "agent_run_duration_seconds": _spec("histogram", "s", "outcome"),
    "agent_run_failures_total": _spec("counter", "{failure}", "failure_class"),
    "model_requests_total": _spec("counter", "{request}", "provider_type", "outcome"),
    "model_tokens_total": _spec("counter", "{token}", "provider_type", "direction"),
    "model_cost_estimate": _spec("counter", "USD", "provider_type"),
    "tool_calls_total": _spec("counter", "{call}", "tool_family", "risk", "outcome"),
    "tool_call_failures_total": _spec(
        "counter", "{failure}", "tool_family", "failure_class"
    ),
    "trigger_invocations_total": _spec(
        "counter", "{invocation}", "connector_type", "outcome"
    ),
    "trigger_failures_total": _spec(
        "counter", "{failure}", "connector_type", "failure_class"
    ),
    "sandbox_jobs_total": _spec("counter", "{job}", "outcome", "network_policy"),
    "sandbox_job_duration_seconds": _spec("histogram", "s", "outcome"),
    "nats_consumer_lag": _spec("gauge", "{message}", "stream", "consumer"),
    "temporal_activity_failures": _spec(
        "counter", "{failure}", "task_queue", "activity", "failure_class"
    ),
    "connector_health": _spec("gauge", "1", "connector_type"),
    "connector_connections": _spec(
        "gauge", "{connection}", "connector_type", "outcome"
    ),
})
ROUTE_LABEL_VALUES = frozenset({"/api/:path*", "other"})


def instrument_contracts() -> dict[str, tuple[InstrumentKind, str, set[str]]]:
    return {
        name: (spec.kind, spec.unit, set(spec.labels))
        for name, spec in METRIC_SPECS.items()
    }


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
    def add(
        self, amount: int | float, attributes: Mapping[str, str] | None = None
    ) -> None:
        """Record one monotonic counter point."""


class RecordInstrument(Protocol):
    def record(
        self, amount: int | float, attributes: Mapping[str, str] | None = None
    ) -> None:
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
        return [
            OTelObservation(value.value, attributes=dict(value.attributes))
            for value in values
        ]


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

```bash
git diff --check
git add packages/observability/src/jhin_observability/__init__.py \
  packages/observability/src/jhin_observability/bootstrap.py \
  packages/observability/src/jhin_observability/metrics.py \
  packages/observability/tests/test_metrics.py
git diff --cached --name-only
git commit -m "feat(observability): enforce telemetry metric cardinality"
```

### Task 4: Trace API Requests and Useful Database Operations Safely

**Files:**
- Create: `packages/observability/src/jhin_observability/sqlalchemy.py`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Create: `packages/observability/tests/test_sqlalchemy.py`
- Modify: `packages/db/pyproject.toml`
- Modify: `packages/db/src/jhin_db/engine.py`
- Create: `packages/db/tests/test_observability.py`
- Modify: `apps/api/src/jhin_api/settings.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/src/jhin_api/seed.py`
- Create: `apps/api/tests/test_observability.py`
- Modify: `tests/integration/test_seed.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Tasks 1–3 bootstrap, trace-only context, safe spans, and `ObservabilitySettings`.
- Produces: API server spans and request-scoped log context; `create_engine(..., trace_sql=True)` installs statement-free normalized SQL spans for every database-using service.

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


async def test_invalid_traceparent_creates_new_root(client: AsyncClient, spans: InMemorySpanExporter) -> None:
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
        failed.headers["X-Request-ID"], succeeded.headers["X-Request-ID"],
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
    return normalized if normalized in {
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"
    } else "other"


def normalize_http_route(request: Request) -> str:
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return "other"
    template = route.path
    return template if 1 <= len(template) <= 200 and template.startswith("/") else "other"


def set_http_span_result(
    span: Span, *, method: str, route: str, status_code: int
) -> None:
    status = status_code if 100 <= status_code <= 599 else 500
    for key, value in normalize_span_attributes({
        "http.request.method": normalize_http_method(method),
        "http.route": route,
        "http.response.status_code": status,
        "http.response.status_class": f"{status // 100}xx",
    }).items():
        span.set_attribute(key, value)


@app.middleware("http")
async def observability_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = new_uuid7()
    request.state.request_id = request_id
    parent = extract_trace_context(request.headers)
    with bind_context(request_id=request_id), safe_span(
        "http.server.request", kind=SpanKind.SERVER, context=parent
    ) as span:
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
        await connection.execute(text("SELECT password FROM secret_canary WHERE token=:token"), {"token": "bind-canary"})
    rendered = json.dumps([dict(span.attributes or {}) for span in spans.get_finished_spans()])
    assert "db.operation" in rendered
    assert "SELECT password" not in rendered
    assert "secret-user" not in rendered and "secret-pass" not in rendered
    assert "db-canary" not in rendered and "bind-canary" not in rendered


def test_unknown_table_is_other() -> None:
    assert normalized_sql_metadata("SELECT * FROM attacker_supplied") == {
        "db.system": "postgresql", "db.operation": "SELECT", "db.table": "other"
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
        node for node in ast.walk(tree)
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

```bash
git diff --check
git add apps/api/src/jhin_api/main.py apps/api/src/jhin_api/settings.py \
  apps/api/tests/test_observability.py packages/db/pyproject.toml \
  packages/db/src/jhin_db/engine.py packages/db/tests/test_observability.py \
  apps/api/src/jhin_api/seed.py tests/integration/test_seed.py \
  packages/observability/src/jhin_observability/__init__.py \
  packages/observability/src/jhin_observability/sqlalchemy.py \
  packages/observability/tests/test_sqlalchemy.py uv.lock
git diff --cached --name-only
git commit -m "feat(observability): trace API and database boundaries"
```

### Task 5: Propagate Context Through NATS and Export Consumer Lag

**Files:**
- Modify: `packages/events/pyproject.toml`
- Modify: `packages/events/src/jhin_events/publisher.py`
- Modify: `packages/events/src/jhin_events/consumer.py`
- Create: `packages/events/src/jhin_events/telemetry.py`
- Create: `packages/events/tests/test_telemetry.py`
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/webhooks/service.py`
- Modify: `services/event_worker/src/jhin_event_worker/processor.py`
- Modify: `services/event_worker/src/jhin_event_worker/normalizer.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Create: `services/event_worker/tests/test_telemetry.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Task 2 trace-only carrier and Task 3 `nats_consumer_lag` gauge.
- Produces: `EventPublisher.publish(envelope, *, headers=None)`, trace-aware generic `publish_jetstream(...)`, trace-aware `run_pull_consumer(...)`, and `poll_nats_consumer_lag(...)`.

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

TRACEPARENT_RE = re.compile(
    r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$"
)
VALID_TRACEPARENT = (
    "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
)
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
        "messaging.system": "nats", "jhin.stream": "EVENTS",
        "jhin.consumer": "event-worker", "jhin.subject_family": "task",
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
        "INGRESS", "ingress"
    )
    with pytest.raises(ValueError, match="stream/subject mismatch"):
        validate_stream_subject("INGRESS", subject)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_stream", ["INGRESS", "EVENTS"])
async def test_dlq_helper_is_closed_traced_and_payload_free(
    origin_stream: DlqOriginStream, spans: InMemorySpanExporter,
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
        "messaging.system": "nats", "jhin.stream": "DLQ",
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
    assert set(document) == {
        "schema_version", "reason", "origin_stream", "error_count"
    }
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
    {"ingress", "task", "agent", "tool", "approval", "connector", "trigger", "workflow", "system", "dlq"}
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
            "messaging.system": "nats", "jhin.stream": stream,
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
    safe_consumer = (
        durable if durable in {"event-worker-ingress", "event-worker"} else "other"
    )
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
            record_span_error(
                span, safe_error(exc, code=SafeErrorCode.INTERNAL_ERROR)
            )
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


def recording_observables(
) -> tuple[JhinMetrics, dict[MetricName, tuple[Observation, ...]]]:
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

```bash
git diff --check
git add apps/api/src/jhin_api/deps.py apps/api/src/jhin_api/webhooks/service.py \
  packages/events/pyproject.toml packages/events/src/jhin_events/consumer.py \
  packages/events/src/jhin_events/publisher.py packages/events/src/jhin_events/telemetry.py \
  packages/events/tests/test_telemetry.py \
  services/event_worker/src/jhin_event_worker/main.py \
  services/event_worker/src/jhin_event_worker/normalizer.py \
  services/event_worker/src/jhin_event_worker/processor.py \
  services/event_worker/tests/test_telemetry.py uv.lock
git diff --cached --name-only
git commit -m "feat(observability): propagate traces through NATS"
```

### Task 6: Propagate Context Through Temporal and Bootstrap Every Python Service

**Files:**
- Modify: `packages/observability/pyproject.toml`
- Create: `packages/observability/src/jhin_observability/temporal.py`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/src/jhin_observability/logging.py`
- Create: `packages/observability/tests/test_temporal.py`
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/health/router.py`
- Modify: `apps/api/src/jhin_api/health/service.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Create: `apps/api/src/jhin_api/temporal.py`
- Create: `apps/api/tests/test_temporal_provider.py`
- Modify: `services/event_worker/pyproject.toml`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/pyproject.toml`
- Modify: `services/tool_worker/tests/test_worker_registration.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/settings.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/main.py`
- Modify: `services/workflow_worker/pyproject.toml`
- Create: `services/workflow_worker/tests/test_telemetry.py`
- Modify: `packages/workflows/pyproject.toml`
- Modify: `packages/workflows/src/jhin_workflows/poller_health.py`
- Modify: `packages/workflows/tests/test_poller_health.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/test_worker_dependency_boundaries.py`

**Interfaces:**
- Consumes: `TOOL_TASK_QUEUE`, fixed activity names, Task 2 runtime, and Task 3 `temporal_activity_failures`.
- Produces: `SafeTemporalTracingInterceptor`, `TemporalActivityMetricsInterceptor`, trace-aware Temporal clients/workers, and exactly one runtime per service.

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
        "temporal.start_workflow", "temporal.activity.reason_agent_step",
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
    assert metric_sum(
        "temporal_activity_failures",
        task_queue="jhin-tool-queue",
        activity="execute_bound_tool",
        failure_class="internal",
    ) == 2


def test_client_and_worker_interceptor_lists_have_exact_roles(runtime: ObservabilityRuntime) -> None:
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
    spans: InMemorySpanExporter, runtime: ObservabilityRuntime,
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
        "api", "agent-worker", "tool-worker", "event-worker", "workflow-worker"
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
    runtime = initialize_observability(ObservabilityConfig(
        service_name="api", service_version="0.1.0", environment="test"
    ))
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
    settings = cast(Settings, SimpleNamespace(
        temporal_address="temporal:7233", temporal_namespace="default"
    ))
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
        "create_engine", "nats.connect", "Client.connect", "TemporalClient.connect",
        "httpx.AsyncClient", "JobManager", "Worker", "Resources.create",
        "connect_with_retry", "resources_with_retry", "temporal_with_retry",
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
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"main", "run", "lifespan", "create_app"}
        ):
            calls = sorted(
                (
                    node for node in ast.walk(function)
                    if isinstance(node, ast.Call) and owner(node) is function
                ),
                key=lambda node: (node.lineno, node.col_offset),
            )
            resources = [
                call for call in calls
                if ast.unparse(call.func).endswith(resource_suffixes)
            ]
            if not resources:
                continue
            resource_owners += 1
            initialization = [
                call for call in calls
                if ast.unparse(call.func).endswith("initialize_observability")
            ]
            assert initialization, (path, function.name)
            assert initialization[0].lineno < resources[0].lineno, (
                path, function.name, ast.unparse(resources[0].func)
            )
        assert resource_owners > 0, path


def test_long_lived_database_calls_inject_initialized_tracer() -> None:
    roots = (REPO_ROOT / "apps/api/src", REPO_ROOT / "services")
    failures: list[str] = []
    for path in (file for root in roots for file in root.rglob("*.py")):
        if path.name == "seed.py" or "tests" in path.parts:
            continue
        for call in (
            node for node in ast.walk(ast.parse(path.read_text()))
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
        tomllib.loads((REPO_ROOT / "services/tool_worker/pyproject.toml").read_text())
        ["project"]["dependencies"]
    )
    assert any(item.startswith("jhin-observability") for item in tool_dependencies)
    assert not any(item.startswith("jhin-models") for item in tool_dependencies)
    assert not any(item.startswith("jhin-agents") for item in tool_dependencies)
    assert not imports_under("services/tool_worker/src", "jhin_models")
    assert not imports_under("services/tool_worker/src", "jhin_agents")
    workflow_dependencies = set(
        tomllib.loads((REPO_ROOT / "packages/workflows/pyproject.toml").read_text())
        ["project"]["dependencies"]
    )
    assert any(item.startswith("jhin-observability") for item in workflow_dependencies)


# services/tool_worker/tests/test_worker_registration.py
def test_tool_worker_bootstraps_before_resources_and_registers_interceptors() -> None:
    source = (
        REPO_ROOT / "services/tool_worker/src/jhin_tool_worker/main.py"
    ).read_text()
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

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
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

    def _completed_workflow_span(
        self, params: CompletedWorkflowSpanParams
    ) -> CarrierDict | None:
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
_TASK_QUEUES = frozenset(
    {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue"}
)


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
                input_with_headers.headers = root._context_to_headers(
                    input_with_headers.headers
                )
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

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
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
    active_runtime = runtime or initialize_observability(ObservabilityConfig(
        service_name="temporal-poller-check",
        service_version=service_version("jhin-workflows"),
        environment=environment,
    ))
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

    assert await queue_has_workflow_poller(
        "temporal.test:7233",
        "default",
        "jhin-workflow-queue",
        runtime=active_runtime,
    ) is True
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
        presented = (
            header.removeprefix("Bearer ").strip()
            if header.startswith("Bearer ")
            else ""
        )
        if (
            not configured
            or not presented
            or not secrets.compare_digest(presented, configured)
        ):
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
    return value.func.id if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) else None


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
        frozenset(clients), frozenset(workers), tuple(bad_clients), tuple(bad_workers),
        tuple(health_connects), tuple(api_outside_provider),
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

```bash
git diff --check
git add pyproject.toml apps/api/src/jhin_api/deps.py \
  apps/api/src/jhin_api/health/router.py apps/api/src/jhin_api/health/service.py \
  apps/api/src/jhin_api/main.py apps/api/src/jhin_api/temporal.py \
  apps/api/tests/test_temporal_provider.py packages/observability/pyproject.toml \
  packages/observability/src/jhin_observability/__init__.py \
  packages/observability/src/jhin_observability/logging.py \
  packages/observability/src/jhin_observability/temporal.py \
  packages/observability/tests/test_temporal.py \
  services/agent_worker/src/jhin_agent_worker/main.py \
  services/agent_worker/src/jhin_agent_worker/settings.py \
  services/tool_worker/src/jhin_tool_worker/main.py \
  services/tool_worker/src/jhin_tool_worker/settings.py \
  services/tool_worker/pyproject.toml services/tool_worker/tests/test_worker_registration.py \
  services/event_worker/pyproject.toml \
  services/event_worker/src/jhin_event_worker/main.py \
  services/event_worker/src/jhin_event_worker/settings.py \
  services/workflow_worker/src/jhin_workflow_worker/main.py \
  services/workflow_worker/src/jhin_workflow_worker/settings.py \
  services/workflow_worker/tests/test_telemetry.py services/workflow_worker/pyproject.toml \
  packages/workflows/pyproject.toml \
  packages/workflows/src/jhin_workflows/poller_health.py \
  packages/workflows/tests/test_poller_health.py \
  services/sandbox_runner/src/jhin_sandbox_runner/main.py \
  services/sandbox_runner/src/jhin_sandbox_runner/settings.py \
  tests/test_worker_dependency_boundaries.py uv.lock
git diff --cached --name-only
git commit -m "feat(observability): trace Temporal service boundaries"
```

### Task 7: Instrument Agent, Model, Tool, and Trigger Commit Boundaries

**Files:**
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/models/router.py`
- Modify: `apps/api/src/jhin_api/models/service.py`
- Create: `apps/api/tests/test_model_telemetry.py`
- Modify: `packages/models/pyproject.toml`
- Create: `packages/models/src/jhin_models/telemetry.py`
- Modify: `packages/models/src/jhin_models/factory.py`
- Create: `packages/models/tests/test_telemetry.py`
- Modify: `packages/tools/pyproject.toml`
- Create: `packages/tools/src/jhin_tools/telemetry.py`
- Create: `packages/tools/tests/test_telemetry.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/reasoning.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/projections.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Create: `services/agent_worker/tests/test_telemetry.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/activities.py`
- Create: `services/tool_worker/tests/test_telemetry.py`
- Modify: `services/event_worker/src/jhin_event_worker/matcher.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Task 3 metric handles, Task 6 fixed activity names, Phase 10 sub-project-1 bound-manifest/tool outcome contracts, persisted `AgentRun` timestamps/totals, `GatewayOutcome.replayed`, and durable `TriggerInvocation` rows.
- Produces: exact run/model/tool/trigger spans and metrics at the authoritative transition points.

- [ ] **Step 1: Write failing provider-attempt/body-exclusion tests**

```python
@pytest.mark.asyncio
async def test_model_attempt_records_safe_metadata_only(
    metrics: JhinMetrics, spans: InMemorySpanExporter, tracer: Tracer,
) -> None:
    prompt_canary = "prompt-canary-must-not-export"
    completion_canary = "completion-canary-must-not-export"
    raw = FakeModelClient(response=ModelResponse(text=completion_canary, latency_ms=17))
    client = InstrumentedModelClient(
        raw, provider_type="openai", metrics=metrics, tracer=tracer
    )
    await client.generate(
        ModelRequest(model="private-model-name", messages=(ModelMessage(role="user", content=prompt_canary),))
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
    metrics: JhinMetrics, spans: InMemorySpanExporter, tracer: Tracer,
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
    assert observed == [(
        api_app.state.observability.metrics,
        api_app.state.observability.tracer,
    )]
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


ObservabilityRuntimeDep = Annotated[
    ObservabilityRuntime, Depends(get_observability_runtime)
]


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
        db, crypto, ctx, provider_id, runtime.metrics, runtime.tracer,
        request_id=req_id(request), ip_hash=ip_hash(request),
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
    reasoning: AgentReasoningActivities, session_factory: SessionFactory,
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
    params = committed_reason_step(input_tokens=11, output_tokens=7, cached_tokens=3, cost_micros=250_000)
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
        ("input", input_tokens), ("output", output_tokens), ("cached", cached_tokens)
    ):
        if value > 0:
            metrics.counter("model_tokens_total").add(value, provider_type=provider, direction=direction)
    if cost_micros > 0:
        metrics.counter("model_cost_estimate").add(
            cost_micros / 1_000_000, provider_type=provider
        )
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
    assert metric_sum(
        "tool_calls_total", tool_family="github", risk="elevated", outcome="completed"
    ) == 1


@pytest.mark.asyncio
async def test_execution_unknown_records_terminal_failure_without_identifier_label() -> None:
    await activities.execute_bound_tool_activity(bound_call_that_becomes_unknown())
    assert metric_sum(
        "tool_call_failures_total", tool_family="github", failure_class="execution_unknown"
    ) == 1
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
    return prefix if prefix in {"system", "organization", "github", "linear", "vercel", "supabase", "cli"} else "other"


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
    matcher: TriggerMatcher, trigger_case: TriggerTelemetryCase,
    session_factory: SessionFactory
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
    assert metric_sum(
        "trigger_invocations_total", connector_type="github", outcome="started"
    ) == 1
    assert metric_sum(
        "trigger_invocations_total", connector_type="github", outcome="duplicate"
    ) == 1
    assert metric_sum(
        "trigger_invocations_total", connector_type="github", outcome="failed"
    ) == 1
    assert metric_sum(
        "trigger_failures_total", connector_type="github", failure_class="target"
    ) == 1


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
            await matcher.handle_event(
                trigger_case.event_for("linear", external_id="issue-3")
            )
    async with session_factory() as session:
        statuses = list(
            await session.scalars(
                select(TriggerInvocation.status).order_by(TriggerInvocation.created_at)
            )
        )
    assert statuses == ["failed", "failed"]
    assert metric_sum(
        "trigger_invocations_total", connector_type="linear", outcome="started"
    ) == 2
    assert metric_sum(
        "trigger_invocations_total", connector_type="linear", outcome="failed"
    ) == 2
    assert metric_sum(
        "trigger_failures_total", connector_type="linear", failure_class="dispatch"
    ) == 2
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

    def event_for_missing_agent(
        self, connector_type: str, *, external_id: str
    ) -> EventEnvelope:
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

```bash
git diff --check
git add apps/api/src/jhin_api/deps.py apps/api/src/jhin_api/models/router.py \
  apps/api/src/jhin_api/models/service.py apps/api/tests/test_model_telemetry.py \
  packages/models/pyproject.toml packages/models/src/jhin_models/factory.py \
  packages/models/src/jhin_models/telemetry.py packages/models/tests/test_telemetry.py \
  packages/tools/pyproject.toml packages/tools/src/jhin_tools/telemetry.py \
  packages/tools/tests/test_telemetry.py \
  services/agent_worker/src/jhin_agent_worker/reasoning.py \
  services/agent_worker/src/jhin_agent_worker/projections.py \
  services/agent_worker/src/jhin_agent_worker/activities.py \
  services/agent_worker/tests/test_telemetry.py \
  services/tool_worker/src/jhin_tool_worker/activities.py \
  services/tool_worker/tests/test_telemetry.py \
  services/event_worker/src/jhin_event_worker/matcher.py \
  services/event_worker/src/jhin_event_worker/main.py \
  services/event_worker/tests/test_telemetry.py uv.lock
git diff --cached --name-only
git commit -m "feat(observability): record committed agent and tool metrics"
```

### Task 8: Instrument Connector HTTP, Connection Health, and Sandbox Lifecycles

**Files:**
- Modify: `packages/connectors/pyproject.toml`
- Modify: `packages/connectors/src/jhin_connectors/http_client.py`
- Create: `packages/connectors/src/jhin_connectors/telemetry.py`
- Modify: `packages/connectors/src/jhin_connectors/github/client.py`
- Modify: `packages/connectors/src/jhin_connectors/github/auth.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/client.py`
- Modify: `packages/connectors/src/jhin_connectors/registry.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/client.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/management_client.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/database_client.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/database_tools.py`
- Modify: `packages/connectors/src/jhin_connectors/cli/runner_client.py`
- Create: `packages/connectors/tests/test_telemetry.py`
- Modify: `packages/connectors/tests/test_http_client.py`
- Create: `packages/connectors/tests/supabase/test_database_telemetry.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- Create: `services/sandbox_runner/tests/test_telemetry.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: trace/context headers, metrics, tool-worker database access, existing bounded connector HTTP helper, and sandbox job DTOs.
- Produces: normalized connector/sandbox spans, connector health/count gauges, and terminal sandbox metrics.

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


def one_finished_span(
    exporter: InMemorySpanExporter, name: str
) -> ReadableSpan:
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
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(provider_failure("error-body-canary"))
    )
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
    for canary in (
        "query-canary", "auth-canary", "body-canary", "error-body-canary"
    ):
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
        assert kwargs["dsn"] == (
            "postgresql://dsn-user-canary:dsn-pass-canary@127.0.0.1:65433/db"
        )
        return connection

    async def fake_verify_live_role(
        selected: TelemetryDatabaseConnection, allowed_schemas: tuple[str, ...]
    ) -> None:
        assert selected is connection and allowed_schemas == ("public",)
        assert await selected.fetchrow(
            "SELECT secret_canary WHERE value=$1", "bind-canary"
        ) == {"value": "result-canary"}

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
    matches = [
        span for span in exporter.get_finished_spans()
        if span.name == "connector.database"
    ]
    assert len(matches) == 1
    span = matches[0]
    assert dict(span.attributes) == {
        "jhin.connector_type": "supabase",
        "jhin.operation": "verify",
        "jhin.outcome": "ok",
    }
    rendered = json.dumps({"name": span.name, "attributes": dict(span.attributes)})
    for canary in (
        "dsn-user-canary", "dsn-pass-canary",
        "SELECT secret_canary", "bind-canary", "result-canary",
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
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 200 <= status < 300
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
connector_type="github",
operation="verify",
tracer=noop_tracer(),
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
    roots = (
        REPO_ROOT / "packages/connectors/src/jhin_connectors",
    )
    failures: list[str] = []
    for path in (candidate for root in roots for candidate in root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = ast.unparse(call.func).rsplit(".", 1)[-1]
            if name not in {
                "send_bounded_json", "trace_connector_database", "_transport_request"
            }:
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
        node for node in ast.walk(tree)
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
            record_span_error(
                span, safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE)
            )
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
    return {
        tuple(item.attributes[key] for key in label_keys): int(item.value)
        for item in values
    }


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
    return [ConnectorHealthRow(type_, status, verified, has_error) for type_, status, verified, has_error in rows]


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
            logger.warning(
                "telemetry.connector_health_probe_failed", error_type=type(exc).__name__
            )
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
    spans: InMemorySpanExporter, metrics: JhinMetrics, tracer: Tracer,
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
        "/v1/jobs", "/v1/jobs/018f0000-0000-7000-8000-000000000010",
        "/v1/jobs/018f0000-0000-7000-8000-000000000010",
    ]
    assert all(0 < request.timeout_seconds <= 30 for request in transport.requests)
    assert "sandbox-secret-canary" not in export_payload(spans, metrics)


@pytest.mark.asyncio
async def test_runner_terminal_job_metrics_emit_once(
    metrics: JhinMetrics, monkeypatch: pytest.MonkeyPatch,
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
    await manager._finish_terminal(
        terminal, "completed", finished_at=terminal.finished_at
    )
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
    metrics: JhinMetrics, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = JobManager(test_settings(), metrics=metrics)
    async def no_container_work(_record: JobRecord) -> None:
        return None
    monkeypatch.setattr(manager, "_run", no_container_work)
    first = job_request(
        job_id="018f0000-0000-7000-8000-000000000011", command=["/bin/true"]
    )
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
        assert client.post(
            "/v1/jobs", headers=headers, json=request.model_dump(mode="json")
        ).status_code == 202
        response = client.post(
            "/v1/jobs", headers=headers, json=request.model_dump(mode="json")
        )
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
                method, path, headers=headers, json_body=json_body,
                timeout_seconds=timeout_seconds,
            )
            outcome = "ok" if 200 <= response.status_code < 300 else "failed"
            return response
        except Exception as exc:
            record_span_error(
                span, safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE)
            )
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

    async def wait_terminal(
        self, job_id: str, *, timeout_seconds: float
    ) -> JobRecord:
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

```bash
git diff --check
git add packages/connectors/pyproject.toml \
  packages/connectors/src/jhin_connectors/http_client.py \
  packages/connectors/src/jhin_connectors/telemetry.py \
  packages/connectors/src/jhin_connectors/github/client.py \
  packages/connectors/src/jhin_connectors/github/auth.py \
  packages/connectors/src/jhin_connectors/linear/client.py \
  packages/connectors/src/jhin_connectors/registry.py \
  packages/connectors/src/jhin_connectors/vercel/client.py \
  packages/connectors/src/jhin_connectors/supabase/management_client.py \
  packages/connectors/src/jhin_connectors/supabase/database_client.py \
  packages/connectors/src/jhin_connectors/supabase/database_tools.py \
  packages/connectors/src/jhin_connectors/cli/runner_client.py \
  packages/connectors/tests/test_telemetry.py packages/connectors/tests/test_http_client.py \
  packages/connectors/tests/supabase/test_database_telemetry.py \
  services/tool_worker/src/jhin_tool_worker/main.py \
  services/tool_worker/src/jhin_tool_worker/resources.py \
  services/sandbox_runner/src/jhin_sandbox_runner/main.py \
  services/sandbox_runner/src/jhin_sandbox_runner/jobs.py \
  services/sandbox_runner/tests/test_telemetry.py uv.lock
git diff --cached --name-only
git commit -m "feat(observability): trace connector and sandbox boundaries"
```

### Task 9: Give the Next.js Server the Same JSON-v1 Contract

**Files:**
- Create: `apps/web/lib/server-logger.ts`
- Create: `apps/web/instrumentation.ts`
- Create: `apps/web/server-wrapper.cjs`
- Modify: `apps/web/Dockerfile`
- Modify: `apps/web/next.config.ts`
- Create: `apps/web/tests/server-logger.test.ts`
- Create: `apps/web/tests/server-wrapper.test.ts`
- Create: `tests/test_web_json_stdout.py`

**Interfaces:**
- Consumes: Task 1 field names, sensitive-key list, size/depth limits, known-value semantics, and stable dotted-event rule.
- Produces: `registerServerLogSecret(...)`, `serverLog(...)`, `serverError(...)`, a process wrapper that converts/suppresses every framework stdout/stderr line, graceful child shutdown, and no browser request access logger.

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
        cwd=REPO_ROOT, check=True, timeout=600,
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
        "schema_version", "timestamp", "level", "service", "environment",
        "event", "logger",
    }
    allowed_events = {
        "web.started", "web.stopping", "web.rewrite_configured",
        "web.request_failed", "web.framework_output_suppressed",
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
        capture_output=True, text=True, check=True, timeout=10,
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
            capture_output=True, text=True, check=True, timeout=20,
        ).stdout.strip()
        created.append(container_id)
        return container_id

    def cleanup() -> None:
        for container_id in created:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True, text=True, check=False, timeout=20,
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

```bash
git diff --check
git add apps/web/Dockerfile apps/web/instrumentation.ts apps/web/lib/server-logger.ts \
  apps/web/next.config.ts apps/web/server-wrapper.cjs \
  apps/web/tests/server-logger.test.ts apps/web/tests/server-wrapper.test.ts \
  tests/test_web_json_stdout.py
git diff --cached --name-only
git commit -m "feat(web): emit safe versioned server logs"
```

### Task 10: Add the Optional Collector/Prometheus/Tempo/Grafana Profile

**Files:**
- Create: `docker/monitoring.Dockerfile`
- Create: `ops/observability/collector.yaml`
- Create: `ops/observability/prometheus.yaml`
- Create: `ops/observability/tempo.yaml`
- Create: `ops/observability/grafana/provisioning/datasources/jhin.yaml`
- Create: `ops/observability/grafana/provisioning/dashboards/jhin.yaml`
- Create: `scripts/build_phase10_dashboard.py`
- Create: `ops/observability/grafana/dashboards/jhin-overview.json`
- Modify: `compose.yaml`
- Modify: `compose.dev.yaml`
- Modify: `.env.example`
- Modify: `Makefile`
- Create: `scripts/assert_phase10_observability_compose.py`
- Create: `tests/test_phase10_observability_compose.py`
- Modify: `scripts/assert_phase10_tool_worker_compose.py`
- Modify: `tests/test_phase10_tool_worker_compose.py`

**Interfaces:**
- Consumes: OTLP/gRPC from product services and exact Task 3 metric names.
- Produces: optional `observability` Compose services, 15-day metrics, 72-hour traces, provisioned data sources/dashboard, internal-only topology, application Docker log rotation, and pure topology guards.

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
        argv, cwd=ROOT, env=environment, capture_output=True, text=True,
        check=True, timeout=30,
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
            "database_url", "nats_url", "temporal_address", "master_key", "docker.sock",
            "sandbox_runner_token", "authorization", "api_key",
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
    ["web", "api", "workflow-worker", "agent-worker", "tool-worker", "event-worker", "sandbox-runner"],
)
def test_every_application_service_has_bounded_json_file_logs(
    rendered: dict[str, Any], service: str
) -> None:
    assert rendered["services"][service]["logging"] == {
        "driver": "json-file", "options": {"max-file": "5", "max-size": "20m"}
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
    ("Agent run p95", "histogram_quantile(0.95, sum by (le, outcome) (rate(agent_run_duration_seconds_bucket[5m])))", "s"),
    ("Agent failures", "sum by (failure_class) (rate(agent_run_failures_total[5m]))", "ops"),
    ("Model attempts", "sum by (provider_type, outcome) (rate(model_requests_total[5m]))", "ops"),
    ("Model tokens", "sum by (provider_type, direction) (rate(model_tokens_total[5m]))", "ops"),
    ("Estimated model cost", "sum by (provider_type) (increase(model_cost_estimate[1h]))", "currencyUSD"),
    ("Tool calls", "sum by (tool_family, risk, outcome) (rate(tool_calls_total[5m]))", "ops"),
    ("Tool failures", "sum by (tool_family, failure_class) (rate(tool_call_failures_total[5m]))", "ops"),
    ("Trigger invocations", "sum by (connector_type, outcome) (rate(trigger_invocations_total[5m]))", "ops"),
    ("Trigger failures", "sum by (connector_type, failure_class) (rate(trigger_failures_total[5m]))", "ops"),
    ("Sandbox jobs", "sum by (outcome, network_policy) (rate(sandbox_jobs_total[5m]))", "ops"),
    ("Sandbox job p95", "histogram_quantile(0.95, sum by (le, outcome) (rate(sandbox_job_duration_seconds_bucket[5m])))", "s"),
    ("NATS consumer lag", "max by (stream, consumer) (nats_consumer_lag)", "short"),
    ("Temporal activity failures", "sum by (task_queue, activity, failure_class) (rate(temporal_activity_failures[5m]))", "ops"),
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
compose.yaml -f compose.<mode>.yaml config --format json`, passing render-only GID `10001` only for
rootful, then performs the same topology/retention/logging assertions as the unit test and scans the
rendered monitoring services for forbidden credential keys. It must not start containers.

Run:

```bash
uv run python scripts/build_phase10_dashboard.py --check
uv run pytest tests/test_phase10_observability_compose.py -q
env -u SANDBOX_DOCKER_GID uv run python scripts/assert_phase10_observability_compose.py --mode rootless
SANDBOX_DOCKER_GID=10001 uv run python scripts/assert_phase10_observability_compose.py --mode rootful
env -u SANDBOX_DOCKER_GID uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootless
SANDBOX_DOCKER_GID=10001 uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootful
env -u SANDBOX_DOCKER_GID docker compose -f compose.yaml -f compose.rootless.yaml config --quiet
SANDBOX_DOCKER_GID=10001 docker compose -f compose.yaml -f compose.dev.yaml \
  -f compose.rootful.yaml --profile observability config --quiet
```

Expected: PASS; the optional profile is valid and internal, with exact retention and log caps.

- [ ] **Step 8: Review and commit**

```bash
git diff --check
git add .env.example Makefile compose.yaml compose.dev.yaml docker/monitoring.Dockerfile \
  ops/observability/collector.yaml ops/observability/prometheus.yaml \
  ops/observability/tempo.yaml \
  ops/observability/grafana/provisioning/datasources/jhin.yaml \
  ops/observability/grafana/provisioning/dashboards/jhin.yaml \
  ops/observability/grafana/dashboards/jhin-overview.json \
  scripts/build_phase10_dashboard.py scripts/assert_phase10_observability_compose.py \
  scripts/assert_phase10_tool_worker_compose.py \
  tests/test_phase10_observability_compose.py tests/test_phase10_tool_worker_compose.py
git diff --cached --name-only
git commit -m "feat(observability): add optional monitoring profile"
```

### Task 11: Prove End-to-End Telemetry, Fail-Open Operation, and Secret Exclusion

**Files:**
- Create: `tests/integration/test_phase10_telemetry.py`
- Create: `tests/integration/emit_phase10_metrics.py`
- Modify: `tests/integration/conftest.py`
- Create: `tests/test_phase10_telemetry_harness.py`
- Create: `scripts/phase10_artifact.py`
- Create: `tests/test_phase10_artifact.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`
- Create: `docs/operations/telemetry.md`
- Modify: `packages/models/src/jhin_models/testing/fake_openai.py`
- Modify: `packages/models/tests/test_fake_openai.py`
- Modify: `packages/connectors/src/jhin_connectors/testing/fake_linear.py`
- Modify: `packages/connectors/tests/linear/test_fake_linear_admin.py`

**Interfaces:**
- Consumes: the complete instrumented stack, dev fake providers, Collector Prometheus exporter, Tempo query API, and versioned dashboard.
- Produces: two isolated black-box Compose invocations, a complete project-bound stack fixture,
  exact Collector exposition assertions, a schema/canary-gated CI artifact, and an operator
  runbook; no product UI/API.

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
    assert contract.required_services == frozenset({
        "api", "web", "workflow-worker", "event-worker", "postgres", "nats",
        "temporal",
    })


def test_telemetry_contract_is_strict_and_selects_exactly_one_overlay() -> None:
    with pytest.raises(ValueError, match="JHIN_TEST_COMPOSE_PROJECT"):
        resolve_stack_contract({"JHIN_TELEMETRY_MODE": "base"})
    rootful = resolve_stack_contract({
        "JHIN_TELEMETRY_MODE": "observed",
        "JHIN_TEST_COMPOSE_PROJECT": "jhin-phase10-observed",
        "PHASE10_SOCKET_MODE": "rootful",
        "SANDBOX_DOCKER_GID": "998",
    })
    assert rootful.socket_mode == "rootful"
    assert compose_files(rootful.socket_mode).count("compose.rootful.yaml") == 1
    assert "compose.rootless.yaml" not in compose_files(rootful.socket_mode)
    with pytest.raises(ValueError, match="must be unset"):
        resolve_stack_contract({
            "JHIN_TELEMETRY_MODE": "base",
            "JHIN_TEST_COMPOSE_PROJECT": "jhin-phase10-base",
            "PHASE10_SOCKET_MODE": "rootless",
            "SANDBOX_DOCKER_GID": "998",
        })


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
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "scenario_driver" in fixtures
    for required in (
        "prompt", "completion", "connector_response", "connector_error",
        "authorization", "cookie", "dsn_user", "dsn_password", "webhook_body",
        "tool_output", "sandbox_secret_env",
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
    "web", "api", "workflow-worker", "agent-worker", "tool-worker",
    "event-worker", "sandbox-runner",
)
SocketMode = Literal["rootful", "rootless"]


def compose_files(mode: SocketMode) -> tuple[str, ...]:
    return (
        "-f", "compose.yaml", "-f", "compose.dev.yaml",
        "-f", f"compose.{mode}.yaml",
    )


LEGACY_REQUIRED_SERVICES = frozenset({
    "api", "web", "workflow-worker", "event-worker", "postgres", "nats", "temporal",
})
TELEMETRY_REQUIRED_SERVICES = frozenset({
    "api", "web", "workflow-worker", "agent-worker", "tool-worker", "event-worker",
    "sandbox-runner", "postgres", "nats", "temporal", "fake-provider", "fake-linear",
})


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


ScenarioDriver = Callable[
    ["Stack", str, TelemetryCanaries | None], Awaitable[ScenarioResult]
]


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
            cwd=REPO_ROOT, env=environment, capture_output=True, text=True,
            check=True, timeout=timeout,
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
                    isinstance(value, str) and value
                    for value in manifest["values"].values()
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
                        for batch in batches for scope in batch.get("scopeSpans", [])
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
            "exec", "-T", "otel-collector", "/bin/busybox", "wget", "-qO-",
            "http://127.0.0.1:9464/metrics",
        )

    async def emit_metric_fixtures(self) -> None:
        await self.compose(
            "cp", "tests/integration/emit_phase10_metrics.py",
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
                "cp", "tests/integration/emit_phase10_metrics.py",
                "api:/tmp/emit_phase10_metrics.py",
            )
            await self.compose("cp", str(local_path), f"api:{container_path}")
            await self.compose(
                "exec", "-T", "api", "python", "/tmp/emit_phase10_metrics.py",
                "--database-canary-file", container_path,
                "--traceparent", traceparent,
            )
        finally:
            local_path.unlink(missing_ok=True)
            with contextlib.suppress(subprocess.CalledProcessError):
                await self.compose(
                    "exec", "-T", "api", "python", "-c",
                    "from pathlib import Path; Path('/tmp/phase10-sink-canaries.json').unlink(missing_ok=True)",
                )

    async def collect_telemetry_sinks(self) -> str:
        if self.last_result is None:
            raise AssertionError("no scenario has been started")
        spans = await self.wait_for_tempo_trace(
            self.last_result.trace_id,
            timeout=30,
            required_names={
                "model.request", "connector.http", "connector.database",
                "sandbox.job.lifecycle",
            },
        )
        return "\n".join((
            await self.logs_all_application_services(),
            json.dumps(spans, sort_keys=True), await self.collector_metrics(),
        ))


@pytest.fixture
async def telemetry_stack(scenario_driver: ScenarioDriver) -> AsyncIterator[Stack]:
    contract = resolve_stack_contract(os.environ)
    if contract.telemetry_mode is None:
        raise ValueError("telemetry_stack requires JHIN_TELEMETRY_MODE")
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as api:
        yield Stack(
            contract.project, contract.telemetry_mode, contract.socket_mode, api,
            os.environ.get("JHIN_TEMPO_URL", "http://127.0.0.1:3200"), scenario_driver,
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
            "ps", "--services", "--filter", "status=running",
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
        pytest.fail(
            f"compose project {contract.project} missing services: {sorted(missing)}"
        )
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
            {"connector_type": "linear", "name": f"Telemetry Linear {tag}",
             "auth_type": "api_key", "credentials": {"api_key": "fake-linear-api-key"},
             "config": {"base_url": "http://fake-linear:8080"}},
        )
        linear = linear_created["connection"]
        github: Mapping[str, object] | None = None
        if canaries is not None:
            github = (await post(
                f"/api/v1/workspaces/{workspace}/connections",
                {"connector_type": "github", "name": f"Telemetry GitHub {tag}",
                 "auth_type": "token",
                 "credentials": {"token": canaries.sandbox_secret_env},
                 "config": {"base_url": "http://fake-github:8080"}},
            ))["connection"]
        cli = (await post(
            f"/api/v1/workspaces/{workspace}/connections",
            {"connector_type": "cli", "name": f"Telemetry CLI {tag}",
             "auth_type": "none", "credentials": {},
             "config": {
                 "default_network": "internet" if github is not None else "none",
                 **({"git_connection_id": github["id"]} if github is not None else {}),
             }},
        ))["connection"]
        provider = await post(
            f"/api/v1/workspaces/{workspace}/model-providers",
            {"type": "openai_compatible", "display_name": f"Telemetry provider {tag}",
             "base_url": "http://fake-provider:8080/v1"},
        )
        profile = await post(
            f"/api/v1/workspaces/{workspace}/model-profiles",
            {"provider_id": provider["id"], "model_name": "fake-mini",
             "display_name": f"Telemetry profile {tag}"},
        )
        agent = await post(
            f"/api/v1/workspaces/{workspace}/agents",
            {"name": f"Telemetry agent {tag}", "system_prompt": "Use both requested tools.",
             "model_profile_id": profile["id"]},
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
            {"name": title, "connection_id": linear["id"],
             "event_type": "connector.linear.issue.updated",
             "filter": {"all": [
                 {"path": "data.team.key", "op": "eq", "value": "ENG"},
                 {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
                 {"path": "data.title", "op": "eq", "value": title},
             ]}, "target_agent_id": agent["id"], "action_config": {"comment_back": False},
             "dedupe_window_seconds": 3600},
        )
        prompt_text = canaries.prompt if canaries is not None else "telemetry prompt"
        completion_marker = (
            "[[telemetry_completion_b64:"
            + base64.b64encode(canaries.completion.encode()).decode()
            + "]]"
            if canaries is not None else ""
        )
        command = (
            f"printf %s {shlex.quote(canaries.tool_output)}"
            if canaries is not None else "printf telemetry-ok"
        )
        cli_arguments: dict[str, object] = {
            "connection_id": cli["id"], "command": command,
        }
        markers = " ".join((
            f'[[tool:linear.issue.read {{"connection_id":"{linear["id"]}","issue":"ENG-142"}}]]',
            f'[[tool:cli.command.execute {json.dumps(cli_arguments, separators=(",", ":"))}]]',
            prompt_text,
            completion_marker,
        ))
        async with httpx.AsyncClient(base_url=FAKE_LINEAR_URL, timeout=15) as fake:
            edited = await fake.post(
                "/_admin/issues/ENG-142/edit",
                json={
                    "title": title,
                    "description": canaries.connector_response
                    if canaries is not None else "telemetry connector response",
                },
            )
            edited.raise_for_status()
            fake_state = (await fake.get("/_state")).json()
        issue = fake_state["issues"]["ENG-142"]
        team = fake_state["teams"]["ENG"]
        backlog = next(row for row in team["states"] if row["name"] == "Backlog")
        todo = next(row for row in team["states"] if row["name"] == "Todo")
        payload = {
            "action": "update", "type": "Issue", "organizationId": "telemetry-org",
            "webhookId": f"telemetry-{tag}", "webhookTimestamp": int(time.time() * 1000),
            "url": issue["url"], "updatedFrom": {"stateId": backlog["id"]},
            "data": {"id": issue["id"], "identifier": "ENG-142", "title": title,
                     "description": f"Run: {markers}", "priority": 0,
                     "team": {"id": team["id"], "key": "ENG", "name": "Engineering"},
                     "state": {"id": todo["id"], "name": "Todo", "type": todo["type"]},
                     "labels": [], "url": issue["url"]},
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        webhook = linear_created["webhook"]
        await asyncio.sleep(6.0)
        response = await stack.api.post(
            webhook["url_path"], content=body,
            headers={"content-type": "application/json", "linear-event": "Issue",
                     "linear-delivery": f"telemetry-{tag}",
                     "linear-signature": sign_payload(webhook["secret"], body),
                     "traceparent": traceparent},
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
            traceparent.split("-")[1], workspace, task_id,
            str(detail["runs"][0]["id"]), str(terminal["state"]),
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
        {"model": "fake-mini", "messages": [
            {"role": "user", "content": f"[[telemetry_completion_b64:{encoded}]]"}
        ]}
    )
    assert status == 200
    assert response["choices"][0]["message"]["content"] == canary


def test_fake_linear_one_shot_error_body_is_consumed_once() -> None:
    state = FakeLinearState()
    assert handle_request(
        state, "POST", "/_admin/telemetry/next-error", {}, {"body": "connector-error-canary"}
    ) == (200, {"configured": True})
    first = handle_request(
        state, "POST", "/graphql", {"Authorization": "fake-linear-api-key"}, {"query": "{ viewer { id } }"}
    )
    second = handle_request(
        state, "POST", "/graphql", {"Authorization": "fake-linear-api-key"}, {"query": "{ viewer { id } }"}
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
TELEMETRY_COMPLETION_RE = re.compile(
    r"\[\[telemetry_completion_b64:([A-Za-z0-9+/=]+)\]\]"
)


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
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "tool"
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
            canary, quote(canary, safe=""), base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
        ):
            assert encoded not in sinks
    assert observed_stack.last_result is not None
    spans = await observed_stack.wait_for_tempo_trace(
        observed_stack.last_result.trace_id,
        timeout=30,
        required_names={
            "model.request", "connector.http", "connector.database",
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
        "task_queue": "jhin-tool-queue", "activity": "execute_bound_tool",
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
            runtime.metrics.set_observable(
                cast(MetricName, name), (Observation(value, labels),)
            )
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
                + document["dsn_user"] + ":" + document["dsn_password"]
                + "@db.telemetry.invalid/jhin"
            )

        with contextlib.suppress(RuntimeError):
            await trace_connector_database(
                "verify", fail_like_asyncpg, tracer=runtime.tracer
            )
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
OPENMETRICS_SAMPLE_RE = re.compile(
    r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{([^}]*)\})?\s"
)
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
            {f"{name}_bucket", f"{name}_count", f"{name}_sum"}
            if kind == "histogram" else {name}
        )
        for emitted in emitted_names:
            assert all(
                FORBIDDEN_IDENTIFIER_LABELS.isdisjoint(label_set)
                for label_set in samples[emitted]
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
    ["web", "api", "workflow-worker", "agent-worker", "tool-worker", "event-worker", "sandbox-runner"],
)
async def test_application_stdout_is_schema_v1_jsonl(
    observed_stack: Stack, service: str
) -> None:
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
          env -u SANDBOX_DOCKER_GID PHASE10_SOCKET_MODE=rootless uv run pytest \
            tests/test_phase10_tool_worker_compose.py \
            tests/test_phase10_observability_compose.py -q
          env -u SANDBOX_DOCKER_GID PHASE10_SOCKET_MODE=rootless uv run python \
            scripts/assert_phase10_tool_worker_compose.py --mode rootless
          env -u SANDBOX_DOCKER_GID PHASE10_SOCKET_MODE=rootless uv run python \
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
    tmp_path: Path, as_list: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_DOCKER_GID", raising=False)
    manifest = tmp_path / "canaries.json"
    write_canary_manifest(manifest)
    source = {
        "Project": "jhin-phase10-observed", "Service": "api",
        "State": "running", "Health": "healthy",
        "Command": "must-not-be-copied", "Environment": ["SECRET=must-not-be-copied"],
    }
    completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps([source] if as_list else source), stderr="",
    )
    destination = tmp_path / "status.json"
    invocations: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        invocations.append((argv, kwargs))
        return completed

    capture_status(
        ["jhin-phase10-observed"], socket_mode="rootless", destination=destination,
        canary_file=manifest, runner=runner,
    )
    assert invocations[0][0].count("compose.rootless.yaml") == 1
    assert "compose.rootful.yaml" not in invocations[0][0]
    assert "SANDBOX_DOCKER_GID" not in cast(
        Mapping[str, str], invocations[0][1]["env"]
    )
    assert json.loads(destination.read_text()) == {
        "schema_version": 1,
        "kind": "compose_status",
        "services": [{
            "project": "jhin-phase10-observed", "service": "api",
            "state": "running", "health": "healthy",
        }],
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
    "prompt", "completion", "connector_response", "connector_error",
    "authorization", "cookie", "api_key", "private_key", "dsn_user",
    "dsn_password", "webhook_secret", "webhook_body", "tool_output",
    "sandbox_secret_env",
)
PROJECTS = frozenset({"jhin-phase10-base", "jhin-phase10-observed"})
SERVICES = frozenset({
    "web", "api", "workflow-worker", "agent-worker", "tool-worker", "event-worker",
    "sandbox-runner", "postgres", "nats", "temporal", "temporal-ui", "sandbox-image",
    "fake-provider", "fake-github", "fake-linear", "fake-vercel", "fake-supabase",
    "fake-supabase-db", "otel-collector", "prometheus", "tempo", "grafana",
})
STATES = frozenset({"running", "exited", "paused", "restarting", "created"})
HEALTH = frozenset({"healthy", "unhealthy", "starting", "none"})


class ArtifactRejected(RuntimeError):
    """Raised before a telemetry diagnostic artifact can be written."""


def write_canary_manifest(destination: Path) -> None:
    if destination.exists():
        raise ArtifactRejected("canary manifest already exists")
    values = {
        kind: f"phase10-{kind}-{secrets.token_urlsafe(24)}"
        for kind in CANARY_KINDS
    }
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
        output.update((value, quote(value, safe=""), base64.b64encode(raw).decode(),
                       base64.urlsafe_b64encode(raw).decode()))
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
        if (row["project"] not in PROJECTS or row["service"] not in SERVICES
                or row["state"] not in STATES or row["health"] not in HEALTH):
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
            ["docker", "compose", "-p", project, "-f", "compose.yaml",
             "-f", "compose.dev.yaml", "-f", f"compose.{socket_mode}.yaml",
             "ps", "--format", "json"],
            cwd=ROOT, env=environment, shell=False, capture_output=True,
            text=True, check=True,
        )
        rows.extend(parse_compose_status(project, completed.stdout))
    write_validated(
        {"schema_version": 1, "kind": "compose_status", "services": rows},
        destination,
        canaries,
    )


def validate_file(input_path: Path, canary_file: Path) -> None:
    validate_document(
        json.loads(input_path.read_text()), read_canary_manifest(canary_file)
    )


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

```bash
git diff --check
git add .github/workflows/ci.yml Makefile docs/operations/telemetry.md \
  packages/models/src/jhin_models/testing/fake_openai.py \
  packages/models/tests/test_fake_openai.py \
  packages/connectors/src/jhin_connectors/testing/fake_linear.py \
  packages/connectors/tests/linear/test_fake_linear_admin.py \
  scripts/phase10_artifact.py tests/test_phase10_artifact.py \
  tests/test_phase10_telemetry_harness.py \
  tests/integration/conftest.py tests/integration/emit_phase10_metrics.py \
  tests/integration/test_phase10_telemetry.py
git diff --cached --name-only
git commit -m "test(observability): prove end-to-end telemetry safety"
```

### Task 12: Run Release Gates, Record Actual Evidence, and Stage Only Telemetry

**Files:**
- Create: `tests/test_phase10_telemetry_evidence.py`
- Create: `scripts/record_phase10_telemetry_evidence.py`
- Create: `docs/evidence/phase10-telemetry.md`

**Interfaces:**
- Consumes: every prior focused/affected/Compose gate and live acceptance output.
- Produces: dated repository evidence containing commands, exact image/package versions, git commit, results, required metric/trace/log checks, and fail-open checks.

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
    "Connected webhook-agent-tool trace", "Exact metric/cardinality contract",
    "JSON-v1 all application services", "Profile-absent product work",
    "Collector-outage product work", "Cross-sink canary absence",
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
    results = [
        row for row in all_gate_results_passed()
        if row.name != "Profile-absent acceptance"
    ]
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
    "asyncpg", "fastapi", "httpx", "opentelemetry-api",
    "opentelemetry-exporter-otlp-proto-grpc", "opentelemetry-sdk",
    "pydantic-settings", "sqlalchemy", "structlog", "temporalio",
)
REQUIRED_VERSION_COMPONENTS = frozenset({
    "package:jhin-observability", "lock:next",
    *(f"lock:{name}" for name in REQUIRED_LOCK_PACKAGES),
    "image:otel-collector", "image:prometheus", "image:tempo", "image:grafana",
})


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
        list(argv), shell=False, capture_output=True, text=True, check=False,
        cwd=ROOT, env=env,
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
            ["docker", "compose", "-f", "compose.yaml", "-f", "compose.dev.yaml",
             "-f", "compose.rootless.yaml", "--profile", "observability",
             "config", "--format", "json"],
            cwd=ROOT, env=compose_environment, capture_output=True, text=True, check=True,
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
            value, quote(value, safe=""), base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
        )
        if any(form and form in captured_output for form in forms):
            raise EvidenceRefused("sensitive telemetry value")
    stamp = recorded_at or datetime.now(UTC)
    commit = git_commit or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT,
        capture_output=True, text=True, check=True
    ).stdout.strip()
    version_map = dict(versions or discover_versions())
    if set(version_map) != REQUIRED_VERSION_COMPONENTS:
        raise EvidenceRefused("version provenance registry mismatch")
    if not commit or any(not key.strip() or not value.strip() for key, value in version_map.items()):
        raise EvidenceRefused("blank provenance cell")
    def cell(value: object) -> str:
        rendered = str(value).replace("|", "\\|").replace("\n", " ").strip()
        if not rendered:
            raise EvidenceRefused("blank result cell")
        return rendered

    lines = [
        "# Phase 10 Telemetry Evidence", "",
        f"- Recorded at: `{stamp.astimezone(UTC).isoformat()}`",
        f"- Git commit: `{cell(commit)}`", "", "## Gates", "",
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
    ("Compose model", ["uv", "run", "python", "scripts/assert_phase10_observability_compose.py", "--mode", "rootless"]),
    ("Tool-worker Compose model", ["uv", "run", "python", "scripts/assert_phase10_tool_worker_compose.py", "--mode", "rootless"]),
    ("Dashboard generated", ["uv", "run", "python", "scripts/build_phase10_dashboard.py", "--check"]),
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
env -u SANDBOX_DOCKER_GID uv run python \
  scripts/assert_phase10_observability_compose.py --mode rootless
env -u SANDBOX_DOCKER_GID uv run python \
  scripts/assert_phase10_tool_worker_compose.py --mode rootless
env -u SANDBOX_DOCKER_GID docker compose -f compose.yaml \
  -f compose.rootless.yaml config --quiet
env -u SANDBOX_DOCKER_GID docker compose -f compose.yaml -f compose.dev.yaml \
  -f compose.rootless.yaml --profile observability config --quiet
```

Expected: every command exits zero. Fix any failure with a new RED/GREEN cycle in the owning task; do not weaken a test or cardinality/redaction rule.

- [ ] **Step 5: Run the final live acceptance twice**

First with no profile/export endpoint, then with the full profile:

```bash
env -u SANDBOX_DOCKER_GID docker compose -p jhin-phase10-base \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile build build sandbox-image
env -u SANDBOX_DOCKER_GID OTEL_EXPORTER_OTLP_ENDPOINT= \
  docker compose -p jhin-phase10-base -f compose.yaml -f compose.dev.yaml \
  -f compose.rootless.yaml up -d --build --wait --wait-timeout 240
env -u SANDBOX_DOCKER_GID JHIN_TEST_COMPOSE_PROJECT=jhin-phase10-base \
  JHIN_TELEMETRY_MODE=base PHASE10_SOCKET_MODE=rootless \
  uv run pytest -m integration \
  tests/integration/test_phase10_telemetry.py::test_product_completes_work_with_profile_absent -v
env -u SANDBOX_DOCKER_GID docker compose -p jhin-phase10-base \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml down -v --remove-orphans

env -u SANDBOX_DOCKER_GID docker compose -p jhin-phase10-observed \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile build build sandbox-image
env -u SANDBOX_DOCKER_GID OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
OTEL_EXPORTER_OTLP_INSECURE=true docker compose -p jhin-phase10-observed \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile observability up -d --build --wait --wait-timeout 240
env -u SANDBOX_DOCKER_GID JHIN_TEST_COMPOSE_PROJECT=jhin-phase10-observed \
  JHIN_TELEMETRY_MODE=observed PHASE10_SOCKET_MODE=rootless \
  uv run pytest -m integration tests/integration/test_phase10_telemetry.py \
  -k 'not profile_absent' -v
env -u SANDBOX_DOCKER_GID docker compose -p jhin-phase10-observed \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml \
  --profile observability down -v --remove-orphans
```

Expected: both clean-stack runs pass and both explicit projects are torn down. The observed stack completes the connected trace, metric, JSONL, backend-failure/recovery, and canary assertions.

- [ ] **Step 6: Generate evidence from actual current results**

```bash
uv run python scripts/record_phase10_telemetry_evidence.py
test -s docs/evidence/phase10-telemetry.md
rg -n 'FAIL|INCOMPLETE|PENDING RESULT|not run' docs/evidence/phase10-telemetry.md && exit 1 || true
test -z "$(env -u SANDBOX_DOCKER_GID docker compose -p jhin-phase10-base \
  -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml ps -q)"
test -z "$(env -u SANDBOX_DOCKER_GID docker compose -p jhin-phase10-observed \
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

```bash
git status --short
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
git add tests/test_phase10_telemetry_evidence.py \
  scripts/record_phase10_telemetry_evidence.py docs/evidence/phase10-telemetry.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs(observability): record Phase 10 telemetry evidence"
```

Expected: exactly the three Task 12 files are in the final commit; the user-owned production plan and design specs remain unstaged.

- [ ] **Step 9: Verify final repository state and commit sequence**

```bash
git status --short
git log --oneline --decorate -13
test "$(git rev-list --count HEAD~13..HEAD)" = "13"
test "$(git rev-list --count HEAD~12..HEAD)" = "12"
git diff HEAD~13..HEAD --stat
```

Expected: `HEAD~13..HEAD` is exactly the 13 scoped commits (Task 0 plus Tasks 1–12), while
`HEAD~12..HEAD` is exactly the 12 implementation/evidence commits after the Task 0 baseline. The
13-commit diff contains no unrelated user file. Any remaining untracked Phase 10 specs or
`orgforge-production-implementation-plan.md` are reported but untouched.

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
