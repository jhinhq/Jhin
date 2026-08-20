# Phase 10 Protected Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship opaque anonymous liveness/readiness plus a sanitized, workspace-admin-only Operations health view backed by durable service heartbeats and bounded PostgreSQL, Alembic, NATS JetStream, Temporal, sandbox, connector, key-version, and telemetry checks.

**Architecture:** Add one additive `service_instance_heartbeat` table at Alembic revision `0015`; API, agent-worker, tool-worker, and event-worker upsert one boot-scoped row every 10 seconds in a separate short transaction. Fresh database heartbeats are the only live-readiness signal for those services. Temporal's retained poller records remain bounded queue-capability diagnostics only: `PollerInfo.last_access_time` never makes a worker live or dead, and the credential-free workflow-worker is intentionally reported only as Temporal capability rather than hard liveness. The API composes safe closed-enum components and bounded count summaries, never raw exceptions or infrastructure coordinates; the web app polls that protected projection only while visible and keeps anonymous readiness opaque. This subproject consumes the real `jhin-tool-worker`, `jhin-tool-queue`, and `jhin_observability.ObservabilityRuntime.status()` established by subprojects 1 and 2.

**Tech Stack:** Python 3.13, FastAPI, Pydantic 2.13, SQLAlchemy 2.0.52, Alembic, PostgreSQL 17, NATS JetStream (`nats-py` 2.13), Temporal Python SDK 1.31, OpenTelemetry runtime status, Next.js 16.3.1, React 19, TanStack Query 5, TypeScript 5, Vitest, Docker Compose, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-phase-10-production-operations-design.md`, especially “Architecture and trust boundaries,” `service_instance_heartbeat`, “Protected health and operations UI,” sub-project 3, sequencing/migration expectations, and health acceptance evidence.

## Global Constraints

- This plan implements only Phase 10 sub-project 3. It does not create event-failure/replay/outbox/task-retry models or controls, rotate keys, add rate limits, harden production proxy/TLS, or add chaos failpoints. Subproject 4 extends the Operations page with DLQ/retry panels after their durable tables exist; this plan never reports invented DLQ or retry counts.
- PostgreSQL remains product authority, Temporal remains workflow authority, and NATS remains transport. Health and telemetry are diagnostic and never grant authority or change task, event, approval, retry, audit, budget, concurrency, or credential behavior.
- Consume prior-plan names exactly: distribution `jhin-tool-worker`, service `tool-worker`, module `jhin_tool_worker`, `WORKFLOW_TASK_QUEUE = "jhin-workflow-queue"`, `AGENT_TASK_QUEUE = "jhin-agent-queue"`, and `TOOL_TASK_QUEUE = "jhin-tool-queue"`.
- Consume telemetry through `jhin_observability.get_runtime().status() -> TelemetryExporterStatus`; monitoring remains optional and its state is excluded from overall product readiness.
- Preserve telemetry's API-to-Temporal trace path: the single app-lifetime `TemporalClientProvider` receives the initialized `ObservabilityRuntime` and passes `temporal_client_interceptors(runtime)` on its only connect path. Health reuses that provider and must never replace it with an uninstrumented client/cache.
- Use Python `>=3.13`, Temporal SDK `>=1.31` with the lock remaining at 1.31.0, SQLAlchemy `>=2.0.36`, FastAPI `>=0.115`, and Next.js 16.3.1. Do not introduce a package beyond the already locked stack; Task 2 must declare the already-used `httpx` package directly in tool-worker rather than relying on a transitive dependency.
- The tool-worker and telemetry subprojects add no migration: Phase 9 revision `0014` remains their head. Protected health is additive revision `0015`; keep exactly one head and test empty database `base -> head`, `0014 -> 0015`, and `0015 -> 0014 -> 0015` on real PostgreSQL. No existing product column is removed or rewritten.
- Heartbeat interval is exactly 10 seconds, staleness is exactly 30 seconds, and retention is exactly seven days. Heartbeat writes use a separate short transaction and never become a liveness grant.
- Heartbeat services are exactly `api`, `agent-worker`, `tool-worker`, and `event-worker`. Workflow-worker never gains database or master-key access.
- Public readiness requires at least one fresh agent-worker, tool-worker, and event-worker heartbeat and zero fresh `readiness="degraded"` rows across API, agent-worker, tool-worker, and event-worker. It does not require an API heartbeat row because the current request proves one API instance is serving.
- Key-bearing services are exactly `api`, `agent-worker`, and `tool-worker`. They report version numbers only: active version and sorted unique supported versions; no key bytes, paths, fingerprints, nonces, ciphertext, wrapped DEKs, credentials, or secret values.
- Sandbox reachability is probed only by tool-worker over the existing `runner` network with a two-second bound. API and agent-worker do not join that network.
- `GET /api/v1/health` remains anonymous and returns only `app`, `version`, and `status: "ok"`. `GET /api/v1/health/ready` returns only `status: "ok" | "degraded"`; degraded remains HTTP 503.
- `GET /api/v1/workspaces/{workspace_id}/operations/health` requires `AdminCtx`. A non-member receives 404, a viewer/member receives 403, and no response contains another workspace's connection, secret, failure, task, user, or resource identifiers.
- Protected components contain only `name`, `status`, `checked_at`, optional `latency_ms`, closed `reason_code`, and closed `action`. Counts are carried only by the bounded typed summaries defined below. Raw exception text, dependency addresses, ports, DSNs, hostnames, SQL, provider messages, and tracebacks are never returned.
- Overall `down` is reserved for unavailable product-critical PostgreSQL, NATS, or Temporal. Schema mismatch, backlog/redelivery, missing or stale heartbeat-bearing workers, missing/invalid queue capability metadata, sandbox failure, connection failure, or key rollout mismatch is `degraded`. A retained or recently accessed Temporal poller is never a liveness grant. Optional telemetry state never changes overall status.
- Every implementation task follows RED -> focused GREEN -> affected suite -> exact-diff review -> scoped commit. Never use `git add .`. Worktree status/diff queries are always scoped to the task's owned paths. The sole permitted unscoped repository-state query is `git diff --cached --name-only` (or `git diff --cached --quiet`), which reads only tracked index state. Task 0 is a read-only predecessor gate and owns no file.

## File Map

Task 0 owns no path. The exact implementation ownership map is the union below; each
Task 1-7 `Files` block and `taskN_paths` array mirrors its list byte-for-byte.

### Task 1 owned paths (9)

- `Makefile`
- `packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py`
- `packages/db/src/jhin_db/models/__init__.py`
- `packages/db/src/jhin_db/models/operations.py`
- `packages/db/tests/test_migration_graph.py`
- `packages/db/tests/test_service_instance_heartbeat.py`
- `tests/integration/phase10_upgrade_harness.py`
- `tests/integration/test_phase10_protected_health_migration.py`
- `tests/test_phase10_protected_health_harness.py`

### Task 2 owned paths (22)

- `apps/api/src/jhin_api/main.py`
- `apps/api/tests/test_health.py`
- `apps/api/tests/test_observability.py`
- `packages/db/src/jhin_db/__init__.py`
- `packages/db/src/jhin_db/heartbeat.py`
- `packages/db/tests/test_heartbeat.py`
- `packages/secrets/src/jhin_secrets/crypto.py`
- `packages/secrets/tests/test_crypto.py`
- `services/agent_worker/src/jhin_agent_worker/main.py`
- `services/agent_worker/tests/test_telemetry.py`
- `services/event_worker/src/jhin_event_worker/main.py`
- `services/event_worker/tests/test_telemetry.py`
- `services/tool_worker/pyproject.toml`
- `services/tool_worker/src/jhin_tool_worker/main.py`
- `services/tool_worker/src/jhin_tool_worker/resources.py`
- `services/tool_worker/tests/test_advertised_tools.py`
- `services/tool_worker/tests/test_health_heartbeat.py`
- `services/tool_worker/tests/test_telemetry.py`
- `services/tool_worker/tests/test_worker_registration.py`
- `tests/test_service_heartbeat_wiring.py`
- `tests/test_worker_dependency_boundaries.py`
- `uv.lock`

### Task 3 owned paths (11)

- `apps/api/src/jhin_api/health/checks.py`
- `apps/api/src/jhin_api/health/router.py`
- `apps/api/src/jhin_api/health/schemas.py`
- `apps/api/src/jhin_api/health/service.py`
- `apps/api/tests/test_health.py`
- `apps/api/tests/test_temporal_provider.py`
- `packages/events/src/jhin_events/streams.py`
- `packages/events/tests/test_streams.py`
- `packages/workflows/src/jhin_workflows/poller_health.py`
- `packages/workflows/tests/test_poller_health.py`
- `services/event_worker/src/jhin_event_worker/settings.py`

### Task 4 owned paths (4)

- `apps/api/src/jhin_api/health/router.py`
- `apps/api/src/jhin_api/health/service.py`
- `apps/api/tests/conftest.py`
- `apps/api/tests/test_operations_health.py`

### Task 5 owned paths (8)

- `apps/web/app/(app)/operations/page.tsx`
- `apps/web/app/(app)/page.tsx`
- `apps/web/components/app-shell.tsx`
- `apps/web/lib/hooks.ts`
- `apps/web/lib/types.ts`
- `apps/web/tests/operations-navigation.test.tsx`
- `apps/web/tests/operations-page.test.tsx`
- `apps/web/tests/overview-health.test.tsx`

### Task 6 owned paths (7)

- `.github/workflows/ci.yml`
- `Makefile`
- `tests/integration/conftest.py`
- `tests/integration/phase10_upgrade_harness.py`
- `tests/integration/test_phase10_protected_health.py`
- `tests/integration/test_stack_health.py`
- `tests/test_phase10_protected_health_harness.py`

### Task 7 owned paths (2)

- `README.md`
- `docs/operations/protected-health.md`

## Shared Interfaces

These names and fields are fixed across all tasks.

```python
# packages/db/src/jhin_db/heartbeat.py
HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_STALE_SECONDS = 30
HEARTBEAT_RETENTION_DAYS = 7
HEARTBEAT_PURGE_BATCH_SIZE = 500
SERVICE_VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\Z")

ServiceName = Literal["api", "agent-worker", "tool-worker", "event-worker"]
HeartbeatReadiness = Literal["ok", "degraded"]
HeartbeatReason = Literal["master_key_unavailable", "sandbox_unreachable"]

@dataclass(frozen=True)
class HeartbeatIdentity:
    instance_id: UUID
    service: ServiceName
    version: str
    started_at: datetime

    @classmethod
    def new(cls, *, service: ServiceName, version: str) -> HeartbeatIdentity: ...

    def __post_init__(self) -> None: ...  # validates service, version, and aware UTC time

@dataclass(frozen=True)
class HeartbeatState:
    readiness: HeartbeatReadiness = "ok"
    safe_reason_code: HeartbeatReason | None = None
    sandbox_reachable: bool | None = None
    active_key_version: int | None = None
    supported_key_versions: tuple[int, ...] = ()

    def __post_init__(self) -> None: ...  # validates reason/field ownership and exact key tuple

HeartbeatStateProvider = Callable[[], Awaitable[HeartbeatState]]

class HeartbeatClock(Protocol):
    def monotonic(self) -> float: ...
    async def wait_until(self, deadline: float, stop: asyncio.Event) -> bool: ...

async def upsert_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    identity: HeartbeatIdentity,
    state: HeartbeatState,
    *,
    now: datetime | None = None,
) -> None: ...

async def purge_expired_heartbeats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> int: ...

async def run_heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    identity: HeartbeatIdentity,
    state_provider: HeartbeatStateProvider,
    stop: asyncio.Event,
    *,
    clock: HeartbeatClock | None = None,
) -> None: ...

# packages/secrets/src/jhin_secrets/crypto.py
class SecretCrypto:
    @property
    def active_key_version(self) -> int: ...
    @property
    def supported_key_versions(self) -> tuple[int, ...]: ...
    # key_version remains a compatibility alias for active_key_version.

# packages/events/src/jhin_events/streams.py
INGRESS_CONSUMER = "event-worker-ingress"
EVENTS_CONSUMER = "event-worker"

# packages/workflows/src/jhin_workflows/poller_health.py
TEMPORAL_RECENT_ACCESS_DIAGNOSTIC_SECONDS = 30
TEMPORAL_POLLER_RPC_TIMEOUT_SECONDS = 5

@dataclass(frozen=True)
class WorkflowPollerDiagnostics:
    retained: int
    recently_accessed: int
    invalid_last_access_timestamps: int

async def workflow_poller_diagnostics(
    client: temporalio.client.Client,
    *,
    namespace: str,
    queue: str,
    checked_at: datetime,
) -> WorkflowPollerDiagnostics: ...

# apps/api/src/jhin_api/temporal.py
class TemporalClientProvider:
    def __init__(
        self,
        settings: Settings,
        observability: ObservabilityRuntime,
    ) -> None: ...
    async def get(self) -> temporalio.client.Client: ...

# apps/api/src/jhin_api/health/service.py
CONNECTOR_VERIFICATION_FRESH_SECONDS = 300

@dataclass(frozen=True)
class CurrentApiInstance:
    instance_id: UUID
    version: str
    active_key_version: int | None
    supported_key_versions: tuple[int, ...]

class ObservabilityRuntimeProtocol(Protocol):
    config: ObservabilityConfig
    def status(self) -> TelemetryExporterStatus: ...

# apps/api/src/jhin_api/health/schemas.py
MAX_SAFE_COUNT = 9_007_199_254_740_991
MAX_COMPONENTS = 9
MAX_CONNECTOR_TYPES = 32
MAX_SERVICE_VERSIONS = 20
MAX_KEY_VERSIONS = 32
MAX_KEY_DISTRIBUTIONS_PER_SERVICE = 32

BoundedCount = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_COUNT)]
BoundedKeyVersion = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
BoundedLatencyMs = Annotated[
    float,
    Field(strict=True, ge=0, le=60_000, allow_inf_nan=False),
]
BoundedServiceVersion = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$"),
]
BoundedRevision = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=64, pattern=r"^[0-9A-Za-z_-]{1,64}$"),
]
BoundedConnectorType = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=50, pattern=r"^[a-z][a-z0-9_-]{0,49}$"),
]
VersionCountMap = Annotated[
    dict[BoundedServiceVersion, BoundedCount],
    Field(strict=True, max_length=MAX_SERVICE_VERSIONS),
]

class LivenessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app: Annotated[str, Field(strict=True, min_length=1, max_length=64)]
    version: BoundedServiceVersion
    status: Literal["ok"]

class ReadinessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "degraded"]

class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"

class HealthReasonCode(StrEnum):
    DATABASE_UNAVAILABLE = "database_unavailable"
    SCHEMA_MISMATCH = "schema_mismatch"
    NATS_UNAVAILABLE = "nats_unavailable"
    CONSUMER_MISSING = "consumer_missing"
    CONSUMER_BACKLOG = "consumer_backlog"
    CONSUMER_REDELIVERY = "consumer_redelivery"
    TEMPORAL_UNAVAILABLE = "temporal_unavailable"
    POLLER_MISSING = "poller_missing"
    POLLER_METADATA_INVALID = "poller_metadata_invalid"
    WORKER_MISSING = "worker_missing"
    WORKER_STALE = "worker_stale"
    WORKER_DEGRADED = "worker_degraded"
    SANDBOX_UNREACHABLE = "sandbox_unreachable"
    CONNECTION_UNVERIFIED = "connection_unverified"
    CONNECTION_UNHEALTHY = "connection_unhealthy"
    KEY_METADATA_MISSING = "key_metadata_missing"
    KEY_VERSION_UNSUPPORTED = "key_version_unsupported"
    TELEMETRY_NOT_CONFIGURED = "telemetry_not_configured"
    TELEMETRY_NO_RECENT_SUCCESS = "telemetry_no_recent_success"
    TELEMETRY_EXPORT_FAILED = "telemetry_export_failed"

class HealthAction(StrEnum):
    NONE = "none"
    CHECK_DATABASE = "check_database"
    RUN_MIGRATIONS = "run_migrations"
    CHECK_NATS = "check_nats"
    INSPECT_CONSUMER = "inspect_consumer"
    CHECK_TEMPORAL = "check_temporal"
    RESTART_WORKER = "restart_worker"
    CHECK_SANDBOX_RUNNER = "check_sandbox_runner"
    VERIFY_CONNECTIONS = "verify_connections"
    REVIEW_KEY_ROLLOUT = "review_key_rollout"
    CHECK_TELEMETRY_EXPORTER = "check_telemetry_exporter"

class HealthComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(strict=True, min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9.-]{0,63}$")]
    status: HealthStatus
    checked_at: AwareDatetime
    latency_ms: BoundedLatencyMs | None = None
    reason_code: HealthReasonCode | None = None
    action: HealthAction = HealthAction.NONE

class SchemaHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_revision: BoundedRevision | None
    packaged_head: BoundedRevision
    component: HealthComponent

class WorkerHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Literal["api", "agent-worker", "tool-worker", "event-worker"]
    fresh_instances: BoundedCount
    stale_instances: BoundedCount
    versions: VersionCountMap
    invalid_or_excess_versions: BoundedCount
    component: HealthComponent

class EventConsumerHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream: Literal["INGRESS", "EVENTS"]
    consumer: Literal["event-worker-ingress", "event-worker"]
    pending: BoundedCount
    redelivered: BoundedCount
    component: HealthComponent

class TemporalQueueHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queue: Literal["jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue"]
    retained_pollers: BoundedCount  # diagnostic/capability metadata, never liveness
    recently_accessed_pollers: BoundedCount  # 30-second diagnostic only
    invalid_last_access_timestamps: BoundedCount
    fresh_owner_instances: BoundedCount | None  # None for credential-free workflow-worker
    component: HealthComponent

class ConnectorHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_type: BoundedConnectorType
    enabled: BoundedCount
    healthy: BoundedCount
    unhealthy: BoundedCount
    unverified: BoundedCount
    invalid_or_excess_connections: BoundedCount
    component: HealthComponent

class KeyVersionCount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_version: BoundedKeyVersion
    secret_count: BoundedCount

class KeyVersionDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_version: BoundedKeyVersion
    supported_versions: Annotated[
        tuple[BoundedKeyVersion, ...],
        Field(strict=True, min_length=1, max_length=MAX_KEY_VERSIONS),
    ]
    instance_count: BoundedCount

    @model_validator(mode="after")
    def validate_exact_tuple(self) -> Self:
        if self.supported_versions != tuple(sorted(set(self.supported_versions))):
            raise ValueError("supported versions must be sorted and unique")
        if self.active_version not in self.supported_versions:
            raise ValueError("active version must be supported")
        return self

class KeyReporterSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Literal["api", "agent-worker", "tool-worker"]
    fresh_instances: BoundedCount
    distributions: Annotated[
        list[KeyVersionDistribution],
        Field(strict=True, max_length=MAX_KEY_DISTRIBUTIONS_PER_SERVICE),
    ]
    missing_metadata: BoundedCount
    invalid_or_excess_instances: BoundedCount

class KeyHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_version: BoundedKeyVersion | None
    secret_rows_by_version: Annotated[
        list[KeyVersionCount],
        Field(strict=True, max_length=MAX_KEY_VERSIONS),
    ]
    invalid_or_excess_secret_rows: BoundedCount
    reporters: Annotated[
        list[KeyReporterSummary],
        Field(strict=True, min_length=3, max_length=3),
    ]
    component: HealthComponent

class TelemetryHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configured: StrictBool
    recent_success: StrictBool | None
    last_success_at: AwareDatetime | None
    dropped_items: BoundedCount
    component: HealthComponent

class OperationsHealthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "degraded", "down"]
    checked_at: AwareDatetime
    components: Annotated[
        list[HealthComponent],
        Field(strict=True, min_length=MAX_COMPONENTS, max_length=MAX_COMPONENTS),
    ]
    schema: SchemaHealthSummary
    workers: Annotated[
        list[WorkerHealthSummary], Field(strict=True, min_length=4, max_length=4)
    ]
    event_consumers: Annotated[
        list[EventConsumerHealthSummary], Field(strict=True, min_length=2, max_length=2)
    ]
    temporal_queues: Annotated[
        list[TemporalQueueHealthSummary], Field(strict=True, min_length=3, max_length=3)
    ]
    connectors: Annotated[
        list[ConnectorHealthSummary], Field(strict=True, max_length=MAX_CONNECTOR_TYPES)
    ]
    keyring: KeyHealthSummary
    telemetry: TelemetryHealthSummary
```

```typescript
# apps/web/components/app-shell.tsx
interface NavItem {
  href: string;
  label: string;
  icon: typeof ClipboardList;
  minimumRole?: WorkspaceRole;
}
```

---



#### Consolidated application contract


Rewrite the protected-health tracked plan so that:

1. its Global Constraints adopt sections 2, 3, and 12 of this addendum;
2. its File Map is the exact union of the Task 1-7 arrays in sections 5-11;
3. Task 0 is replaced completely by section 4;
4. Tasks 1-7 are replaced completely by sections 5-11;
5. each task's `Files` block and `taskN_paths` array are exact mirrors;
6. each mutating task uses the common staging invariant in section 12;
7. the final history and CI acceptance text is section 13; and
8. no superseded runner, sentinel, raw-Compose, count-only-history, or Bash-4 command remains.

The checkpoint commit subject is exactly:

```text
docs(observability): checkpoint Phase 10 telemetry execution
```

Its manifest is exactly the two tracked plans:

```text
docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md
```

#### Binding predecessor handoffs


### P1. Accepted telemetry head

Protected Task 0 consumes one unique checkpoint with the exact subject and two-path manifest above.
The final telemetry commit subject is exactly:

```text
docs(observability): record Phase 10 telemetry evidence
```

It owns exactly:

```text
docs/evidence/phase10-telemetry.md
scripts/record_phase10_telemetry_evidence.py
tests/test_phase10_telemetry_evidence.py
```

That commit must be the pushed branch head before protected work begins. Its committed evidence
must pass `record_phase10_telemetry_evidence.py --check`, and a fresh successful required CI run
for that exact pushed head must prove both existing live jobs. Each live job must prove its exact
synthetic-merge checkout and must run `telemetry-base` before `telemetry-observed`, with exact
selected counts 1 and 12, zero skip/xfail/deselection, and the leased authority's post-run resource
and selected-image absence proof. A Task 11 run, a local evidence file, a stale run, or an
unverified synthetic merge is insufficient.

### P2. One runtime and one Temporal provider

Consume corrected telemetry without replacement:

- every ordinary service owns exactly one `ObservabilityRuntime`;
- the heartbeat version is `runtime.config.service_version`, produced by telemetry's one
  `service_version(...)` helper;
- API owns one `app.state.temporal_provider = TemporalClientProvider(settings, runtime)`;
- `TemporalDep`, anonymous readiness, and protected health resolve that same provider and its one
  cached interceptor-aware client;
- `health.service` owns `TemporalHealthUnavailable` and `check_temporal(provider)`; and
- protected routes receive `ObservabilityRuntimeDep`; they never call a global runtime accessor.

### P3. Reserve the sole protected-health log event in telemetry Task 1

Before checkpoint commit, add exactly `health.heartbeat_write_failed` to telemetry Task 1's closed
JSON-v1 event registry and its existing audit tests. It has no event-specific fields. Protected
code may emit only:

```python
logger.warning("health.heartbeat_write_failed")
```

It must not pass a service field, exception/reason text, identity, URL, state, canary, or arbitrary
extra. The fixed JSON base field already names the emitting service. This changes no telemetry
Task 1 manifest because registry and audit paths are already Task 1-owned. Any later event requires
a reviewed predecessor registry/test/manifest amendment.

### P4. Expose one validated sandbox-runner base-URL seam in telemetry Task 8

Before checkpoint commit, telemetry Task 8 must expose the pure public helper
`validated_sandbox_runner_base_url() -> str` from its already-owned runner client. The helper
reads the existing environment/default authority, rejects userinfo, query, fragment, non-root
path, unsupported scheme, malformed host/port, and control characters, and returns one normalized
internal base URL. It returns no token and logs no URL. Telemetry runner calls and the protected
tool heartbeat probe consume this same helper. This changes no telemetry Task 8 manifest.

If P3 or P4 is absent when the checkpoint is about to be committed, stop and regenerate both plan
contracts; do not grow a protected manifest after checkpoint.

### P5. One leased live authority

After telemetry Task 11 there is exactly one live owner:

- `ComposeAuthority(..., observability=...)` owns project, socket, files, profile, ports, private
  lease, process groups, barriers, workspace initializer, sandbox resources, selected images,
  lifecycle, and exhaustive cleanup;
- a narrow `Stack` facade stores only that authority and exposes reviewed typed operations;
- `telemetry-base` and `telemetry-observed` remain separate one-shot scenarios;
- dynamic loopback ports come only from the lease; and
- the only live CI jobs are `phase10-rootful-live` and `phase10-rootless-live`.

Protected Tasks 1 and 6 extend the same authority. They create no resolver, file vector, socket
selector, shell runner, project, port discovery routine, cleanup routine, or CI job.

#### Bash 3.2 fail-closed helpers


All plan shell must run under macOS Bash 3.2. Do not use `mapfile`, `readarray`, associative arrays,
`${value,,}`, `HEAD~N`, count-only history proofs, broad worktree status/diff, `git add .`, or
`rg ... && exit 1 || true`. The sole unscoped repository-state query is index-only
`git diff --cached --name-only` or `git diff --cached --quiet`.

Install and reuse these helpers in protected Task 0 and Task 7:

```bash
set -euo pipefail

line_count() {
  awk 'NF { count += 1 } END { print count + 0 }'
}

unique_commit_with_subject() {
  local subject="$1"
  local matches count resolved_subject
  matches="$(git log --format=%H --fixed-strings --grep="$subject")" || return 1
  count="$(printf '%s\n' "$matches" | line_count)" || return 1
  test "$count" = 1 || return 1
  resolved_subject="$(git show -s --format=%s "$matches")" || return 1
  test "$resolved_subject" = "$subject" || return 1
  printf '%s\n' "$matches"
}

exact_commit_manifest() {
  local commit="$1"
  local expected actual
  shift
  expected="$(printf '%s\n' "$@" | LC_ALL=C sort)" || return 1
  actual="$(git diff-tree --no-commit-id --name-only -r "$commit" | LC_ALL=C sort)" || return 1
  test "$actual" = "$expected" || return 1
}

require_empty_index() {
  local cached_paths
  cached_paths="$(git diff --cached --name-only)" || return 1
  test -z "$cached_paths" || return 1
}

stat_numeric() {
  local field="$1"
  local target="$2"
  local value
  value="$(stat -c "%${field}" "$target" 2>/dev/null)" || \
    value="$(stat -f "%${field}" "$target")" || return 1
  printf '%s\n' "$value"
}

validate_local_phase10_mode() {
  local verified_mode="$1"
  local socket_path socket_uid socket_gid current_uid
  case "$verified_mode" in
    rootful)
      socket_path="${PHASE10_ROOTFUL_DOCKER_SOCKET:-}"
      case "$socket_path" in /*) ;; *) return 1 ;; esac
      test -S "$socket_path" && test ! -L "$socket_path" || return 1
      socket_gid="$(stat_numeric g "$socket_path")" || return 1
      case "$socket_gid" in ''|*[!0-9]*) return 1 ;; esac
      test "$socket_gid" -gt 0 || return 1
      test "${SANDBOX_DOCKER_GID:-}" = "$socket_gid" || return 1
      ;;
    rootless)
      socket_path="${PHASE10_ROOTLESS_DOCKER_SOCKET:-}"
      case "$socket_path" in /*) ;; *) return 1 ;; esac
      test -S "$socket_path" && test ! -L "$socket_path" || return 1
      socket_uid="$(stat_numeric u "$socket_path")" || return 1
      test "$socket_uid" = 10001 || return 1
      current_uid="$(id -u)" || return 1
      test "$current_uid" = 10001 || return 1
      test -z "${SANDBOX_DOCKER_GID:-}" || return 1
      ;;
    *) return 1 ;;
  esac
}
```

Every `rg` used as a no-match gate must distinguish exit 1 from exit 2. Missing or unreadable
paths fail. Every worktree query is scoped to an explicit task array or exact owned-path union.

### Task 0: no-write predecessor and acceptance gate

**Files:**

**Interfaces:**
- Consumes the exact telemetry checkpoint/tip and produces a no-write fail-closed predecessor gate.

Task 0 owns zero paths, creates no commit, and requires an empty index at entry and exit.

The telemetry task manifest counts and ordered subjects are exact:

```bash
telemetry_counts=(51 15 5 15 15 39 32 44 12 18 14 3)
telemetry_subjects=(
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
```

Replace Task 0's checkpoint/history/evidence steps with this exact executable block. It retains
the literal blob comparison and complete telemetry `Files`/array/subject/count proof required by
telemetry section 20:

```bash
set -euo pipefail
plan_paths=(
  docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
  docs/superpowers/plans/2026-08-18-phase-10-telemetry-core.md
)
telemetry_plan="${plan_paths[1]}"
require_empty_index || exit 1

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
    END { if (!task_seen || !in_files || !complete) exit 1 }
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

checkpoint_commit="$(unique_commit_with_subject \
  'docs(observability): checkpoint Phase 10 telemetry execution')" || exit 1
final_telemetry_commit="$(unique_commit_with_subject \
  'docs(observability): record Phase 10 telemetry evidence')" || exit 1
head_commit="$(git rev-parse HEAD)" || exit 1
test "$head_commit" = "$final_telemetry_commit" || exit 1
git merge-base --is-ancestor "$checkpoint_commit" "$head_commit" || exit 1

checkpoint_expected_paths="$(printf '%s\n' "${plan_paths[@]}" | LC_ALL=C sort)" || exit 1
checkpoint_actual_paths="$(git diff-tree --no-commit-id --name-only -r \
  "$checkpoint_commit" | LC_ALL=C sort)" || exit 1
test "$checkpoint_actual_paths" = "$checkpoint_expected_paths" || exit 1
for path in "${plan_paths[@]}"; do
  git ls-files --error-unmatch "$path" >/dev/null || exit 1
  git diff --quiet "$checkpoint_commit" HEAD -- "$path" || exit 1
  git diff --quiet -- "$path" || exit 1
  git diff --cached --quiet -- "$path" || exit 1
done

expected_telemetry_commits="$(
  task_number=1
  while test "$task_number" -le 12; do
    index_number=$((task_number - 1))
    files="$(task_files "$task_number")" || exit 1
    index="$(task_index "$task_number")" || exit 1
    expected_count="${telemetry_counts[$index_number]}"
    subject="${telemetry_subjects[$index_number]}"
    files_count="$(printf '%s\n' "$files" | line_count)" || exit 1
    index_count="$(printf '%s\n' "$index" | line_count)" || exit 1
    test "$files_count" -eq "$expected_count" || exit 1
    test "$index_count" -eq "$expected_count" || exit 1
    test "$files" = "$index" || exit 1
    duplicate_paths="$(printf '%s\n' "$index" | LC_ALL=C sort | uniq -d)" || exit 1
    test -z "$duplicate_paths" || exit 1
    section_text="$(task_section "$task_number")" || exit 1
    printf '%s\n' "$section_text" | rg -F -q -- "$subject" || exit 1
    task_commit="$(unique_commit_with_subject "$subject")" || exit 1
    actual_paths="$(git diff-tree --no-commit-id --name-only -r \
      "$task_commit" | LC_ALL=C sort)" || exit 1
    test "$actual_paths" = "$index" || exit 1
    printf '%s\n' "$task_commit" || exit 1
    task_number=$((task_number + 1))
  done
)" || exit 1
actual_telemetry_commits="$(git rev-list --reverse \
  "$checkpoint_commit".."$final_telemetry_commit")" || exit 1
test "$actual_telemetry_commits" = "$expected_telemetry_commits" || exit 1

final_expected_paths="$(printf '%s\n' \
  docs/evidence/phase10-telemetry.md \
  scripts/record_phase10_telemetry_evidence.py \
  tests/test_phase10_telemetry_evidence.py | LC_ALL=C sort)" || exit 1
final_actual_paths="$(git diff-tree --no-commit-id --name-only -r \
  "$final_telemetry_commit" | LC_ALL=C sort)" || exit 1
test "$final_actual_paths" = "$final_expected_paths" || exit 1

uv run python scripts/record_phase10_telemetry_evidence.py \
  --check docs/evidence/phase10-telemetry.md || exit 1
uv run pytest tests/test_phase10_telemetry_evidence.py \
  apps/api/tests/test_temporal_provider.py \
  tests/test_phase10_telemetry_harness.py -q || exit 1
require_empty_index || exit 1
```

Then define and invoke this exact fresh final-telemetry-head CI verifier. Its fixed step name is
`Telemetry base and observed acceptance`; successful return consumes the Task 11 strict scenario
selection/count and post-cleanup contract rather than a status artifact.

```bash
checkout_log_has_sha() {
  local log_path="$1"
  local expected_sha="$2"
  awk -v sha="$expected_sha" '
    index($0, "/usr/bin/git log -1 --format") && index($0, "%H") {
      remaining = 4
      next
    }
    remaining > 0 {
      if (index($0, sha)) { found = 1; exit }
      remaining--
    }
    END { exit(found ? 0 : 1) }
  ' "$log_path"
}

cleanup_final_ci_logs() {
  local directory="$1"
  local rootful_log="$2"
  local rootless_log="$3"
  rm -f -- "$rootful_log" "$rootless_log" || return 1
  rmdir -- "$directory" || return 1
}

verify_final_telemetry_ci() {
  local final_tip="$1"
  local repo pr_number pr_json pr_head pr_base synthetic_merge
  local head_json merge_json head_tree merge_tree merge_parents commit_time
  local runs_json final_run_id run_json run_head run_event run_status
  local run_conclusion run_path run_pr_number run_started_at jobs_json
  local rootful_job_id rootless_job_id log_dir rootful_log rootless_log
  repo=jhinhq/Jhin
  pr_number=1

  pr_json="$(gh api "repos/$repo/pulls/$pr_number")" || return 1
  pr_head="$(jq -er .head.sha <<<"$pr_json")" || return 1
  pr_base="$(jq -er .base.sha <<<"$pr_json")" || return 1
  synthetic_merge="$(jq -er .merge_commit_sha <<<"$pr_json")" || return 1
  test "$pr_head" = "$final_tip" || return 1
  printf '%s\n' "$pr_base" "$synthetic_merge" | \
    awk 'length($0) == 40 && $0 ~ /^[0-9a-f]+$/ { valid++ }
      END { exit(valid == 2 ? 0 : 1) }' || return 1

  head_json="$(gh api "repos/$repo/git/commits/$final_tip")" || return 1
  merge_json="$(gh api "repos/$repo/git/commits/$synthetic_merge")" || return 1
  head_tree="$(jq -er .tree.sha <<<"$head_json")" || return 1
  merge_tree="$(jq -er .tree.sha <<<"$merge_json")" || return 1
  merge_parents="$(jq -er '.parents | map(.sha) | join(" ")' \
    <<<"$merge_json")" || return 1
  commit_time="$(jq -er .committer.date <<<"$head_json")" || return 1
  test "$merge_parents" = "$pr_base $final_tip" || return 1
  test "$merge_tree" = "$head_tree" || return 1

  runs_json="$(gh api \
    "repos/$repo/actions/runs?event=pull_request&head_sha=$final_tip&per_page=100")" || return 1
  final_run_id="$(jq -er --arg sha "$final_tip" '
    [.workflow_runs[] |
      select(.head_sha == $sha and .event == "pull_request" and
        .status == "completed" and .conclusion == "success" and
        .path == ".github/workflows/ci.yml" and .run_started_at != null)] |
    sort_by(.run_started_at, .id) | last | .id
  ' <<<"$runs_json")" || return 1

  run_json="$(gh api "repos/$repo/actions/runs/$final_run_id")" || return 1
  run_head="$(jq -er .head_sha <<<"$run_json")" || return 1
  run_event="$(jq -er .event <<<"$run_json")" || return 1
  run_status="$(jq -er .status <<<"$run_json")" || return 1
  run_conclusion="$(jq -er .conclusion <<<"$run_json")" || return 1
  run_path="$(jq -er .path <<<"$run_json")" || return 1
  run_pr_number="$(jq -er '.pull_requests | if length == 1 then .[0].number else error("PR mismatch") end' \
    <<<"$run_json")" || return 1
  run_started_at="$(jq -er .run_started_at <<<"$run_json")" || return 1
  test "$run_head" = "$final_tip" || return 1
  test "$run_event" = pull_request || return 1
  test "$run_status" = completed || return 1
  test "$run_conclusion" = success || return 1
  test "$run_path" = .github/workflows/ci.yml || return 1
  test "$run_pr_number" = "$pr_number" || return 1
  jq -en --arg run_started_at "$run_started_at" --arg commit_time "$commit_time" \
    '$run_started_at >= $commit_time' >/dev/null || return 1

  jobs_json="$(gh api \
    "repos/$repo/actions/runs/$final_run_id/jobs?per_page=100")" || return 1
  rootful_job_id="$(jq -er '
    [.jobs[] | select(.name == "Phase 10 rootful live boundary" and
      .status == "completed" and .conclusion == "success")] |
    if length == 1 then .[0].id else error("rootful job mismatch") end
  ' <<<"$jobs_json")" || return 1
  rootless_job_id="$(jq -er '
    [.jobs[] | select(.name == "Phase 10 rootless live boundary" and
      .status == "completed" and .conclusion == "success")] |
    if length == 1 then .[0].id else error("rootless job mismatch") end
  ' <<<"$jobs_json")" || return 1
  test "$rootful_job_id" != "$rootless_job_id" || return 1
  for job_id in "$rootful_job_id" "$rootless_job_id"; do
    jq -e --argjson id "$job_id" '
      [.jobs[] | select(.id == $id) | .steps[] |
        select(.name == "Telemetry base and observed acceptance" and
          .status == "completed" and .conclusion == "success")] | length == 1
    ' <<<"$jobs_json" >/dev/null || return 1
  done

  log_dir="$(mktemp -d "${TMPDIR:-/tmp}/jhin-final-telemetry-ci.XXXXXX")" || return 1
  rootful_log="$log_dir/rootful.log"
  rootless_log="$log_dir/rootless.log"
  if ! gh run view "$final_run_id" --repo "$repo" \
    --job "$rootful_job_id" --log >"$rootful_log"; then
    cleanup_final_ci_logs "$log_dir" "$rootful_log" "$rootless_log"
    return 1
  fi
  if ! gh run view "$final_run_id" --repo "$repo" \
    --job "$rootless_job_id" --log >"$rootless_log"; then
    cleanup_final_ci_logs "$log_dir" "$rootful_log" "$rootless_log"
    return 1
  fi
  if ! checkout_log_has_sha "$rootful_log" "$synthetic_merge"; then
    cleanup_final_ci_logs "$log_dir" "$rootful_log" "$rootless_log"
    return 1
  fi
  if ! checkout_log_has_sha "$rootless_log" "$synthetic_merge"; then
    cleanup_final_ci_logs "$log_dir" "$rootful_log" "$rootless_log"
    return 1
  fi
  cleanup_final_ci_logs "$log_dir" "$rootful_log" "$rootless_log" || return 1
}

final_telemetry_subject="$(git show -s --format=%s \
  "$final_telemetry_commit")" || exit 1
test "$final_telemetry_subject" = \
  'docs(observability): record Phase 10 telemetry evidence' || exit 1
verify_final_telemetry_ci "$final_telemetry_commit" || exit 1
require_empty_index || exit 1
```

Any missing/duplicate subject, plan-blob drift, path drift, intervening commit, stale/missing CI,
failed/skipped live job, checkout mismatch, parser/test failure, or nonempty index stops before
Task 1. No command may name the forbidden path.

### Task 1: durable model and leased migration proof

**Files:**
- Modify: `Makefile`
- Modify: `packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Modify: `packages/db/src/jhin_db/models/operations.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Modify: `packages/db/tests/test_service_instance_heartbeat.py`
- Modify: `tests/integration/phase10_upgrade_harness.py`
- Modify: `tests/integration/test_phase10_protected_health_migration.py`
- Modify: `tests/test_phase10_protected_health_harness.py`

**Interfaces:**
- Consumes the accepted Task 0 handoff and produces the exact Task 1 subject, manifest, and gates below.

Subject: `feat: add durable service heartbeats`

```bash
task1_paths=(
  Makefile
  packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py
  packages/db/src/jhin_db/models/__init__.py
  packages/db/src/jhin_db/models/operations.py
  packages/db/tests/test_migration_graph.py
  packages/db/tests/test_service_instance_heartbeat.py
  tests/integration/phase10_upgrade_harness.py
  tests/integration/test_phase10_protected_health_migration.py
  tests/test_phase10_protected_health_harness.py
)
task1_subject="feat: add durable service heartbeats"
```

Required behavior:

- add `ServiceInstanceHeartbeat` and reversible `0015 -> 0014`; `0015` is the sole head;
- retain only fixed diagnostic fields and `(service,last_seen_at)` index; no authority, lease,
  election, effect, workspace/user/secret FK, or free-form error/detail;
- close SQL service/readiness/reason/key/sandbox ownership, while strict application validation
  rejects hostile legacy JSON, bool-as-int, invalid timestamps/versions, and inconsistent tuples;
- prove base-to-head and `0014 -> 0015 -> 0014 -> 0015` on two unique databases, preservation of an
  `0014` object, invalid-row rejection, and partial-failure cleanup; and
- add exactly `protected-health-migration`, selecting the migration module, `expected_tests=2`,
  `observability=False`, plus one delegating `test-protected-health-migration` Make target.
- the socket-free harness test must prove that scenario's exact node and count, immutable
  profile/observability selection, sanitized child environment, absence of guessed
  port/project/socket values, and exhaustive container/network/volume/process-group/lease/barrier/
  workspace-initializer/sandbox/selected-image cleanup, using an injected recorder and no daemon.

RED/GREEN and acceptance:

```bash
uv run pytest packages/db/tests/test_migration_graph.py packages/db/tests/test_service_instance_heartbeat.py tests/test_phase10_protected_health_harness.py -q
verified_mode="${PHASE10_MODE:?set the already-verified local mode}"
validate_local_phase10_mode "$verified_mode" || exit 1
PHASE10_MODE="$verified_mode" make test-protected-health-migration
uv run ruff check packages/db tests/integration/test_phase10_protected_health_migration.py tests/integration/phase10_upgrade_harness.py tests/test_phase10_protected_health_harness.py
uv run ruff format --check packages/db tests/integration/test_phase10_protected_health_migration.py tests/integration/phase10_upgrade_harness.py tests/test_phase10_protected_health_harness.py
uv run mypy
```

`verified_mode` must be exactly `rootful` or `rootless` and must name the mode whose socket
authority has already passed the telemetry validation; an unset, inferred, or substituted mode
fails before Make.

The socket-free RED must fail only for named model/migration/scenario behavior. The real PostgreSQL
gate runs only through the verified lease; direct pytest with guessed PostgreSQL values is invalid.
Its sole staging invocation is
`stage_exact_task "$task1_subject" "${task1_paths[@]}" || exit 1`.

### Task 2: validated writes and subordinate lifecycles

**Files:**
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/tests/test_health.py`
- Modify: `apps/api/tests/test_observability.py`
- Modify: `packages/db/src/jhin_db/__init__.py`
- Modify: `packages/db/src/jhin_db/heartbeat.py`
- Modify: `packages/db/tests/test_heartbeat.py`
- Modify: `packages/secrets/src/jhin_secrets/crypto.py`
- Modify: `packages/secrets/tests/test_crypto.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/agent_worker/tests/test_telemetry.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `services/tool_worker/pyproject.toml`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Modify: `services/tool_worker/tests/test_advertised_tools.py`
- Modify: `services/tool_worker/tests/test_health_heartbeat.py`
- Modify: `services/tool_worker/tests/test_telemetry.py`
- Modify: `services/tool_worker/tests/test_worker_registration.py`
- Modify: `tests/test_service_heartbeat_wiring.py`
- Modify: `tests/test_worker_dependency_boundaries.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes the accepted Task 1 handoff and produces the exact Task 2 subject, manifest, and gates below.

Subject: `feat: publish sanitized service heartbeats`

```bash
task2_paths=(
  apps/api/src/jhin_api/main.py
  apps/api/tests/test_health.py
  apps/api/tests/test_observability.py
  packages/db/src/jhin_db/__init__.py
  packages/db/src/jhin_db/heartbeat.py
  packages/db/tests/test_heartbeat.py
  packages/secrets/src/jhin_secrets/crypto.py
  packages/secrets/tests/test_crypto.py
  services/agent_worker/src/jhin_agent_worker/main.py
  services/agent_worker/tests/test_telemetry.py
  services/event_worker/src/jhin_event_worker/main.py
  services/event_worker/tests/test_telemetry.py
  services/tool_worker/pyproject.toml
  services/tool_worker/src/jhin_tool_worker/main.py
  services/tool_worker/src/jhin_tool_worker/resources.py
  services/tool_worker/tests/test_advertised_tools.py
  services/tool_worker/tests/test_health_heartbeat.py
  services/tool_worker/tests/test_telemetry.py
  services/tool_worker/tests/test_worker_registration.py
  tests/test_service_heartbeat_wiring.py
  tests/test_worker_dependency_boundaries.py
  uv.lock
)
task2_subject="feat: publish sanitized service heartbeats"
```

Required behavior:

- validate complete identity/state and fixed service-owned field matrix before any transaction;
- use one short upsert transaction, preserve boot UUID/`started_at`, and a separate purge capped at
  500 rows strictly older than seven days, at most once per monotonic hour;
- write immediately on monotonic start-to-start ten-second deadlines, skip overruns without
  overlap/catch-up, wake on stop, and re-raise cancellation after owned cleanup;
- emit only P3's registered zero-extra-field event on write failure;
- use `runtime.config.service_version` for API/agent/tool/event; no duplicate package discovery;
- expose immutable crypto active/supported version accessors and preserve compatibility alias;
- use one application-lifetime `httpx.AsyncClient` with `trust_env=False`, redirects disabled,
  finite connection limits, and a strict two-second overall timeout; create/close it inside the
  tool resource owner on normal, startup-failure, cancellation, and cleanup-failure paths;
- probe only unauthenticated `GET /health` against P4's sole validated base URL; send no runner
  token, Authorization, Cookie, trace/baggage carrier, key metadata, job/workspace data, or other
  identifying header/body;
- stream and cap the response body at exactly 1,024 bytes, decode strict UTF-8 and strict JSON,
  require a mapping with exact HTTP 200, `status == "ok"`, and `docker is True`, and reject
  redirect, cap+one, invalid encoding/JSON/type/status, timeout/transport error, and close failure;
- return only a Boolean from the sandbox probe; URL, response bytes/text, exception data, headers,
  and raw/percent/base64 canaries enter neither heartbeat state nor any log;
- start exactly one named heartbeat task inside each telemetry-owned API/agent/tool/event cleanup
  region and stop/await it before providers, clients, engines, resources, and runtime close; and
- add no workflow-worker/rootless-adapter heartbeat, runtime, database, key, or runner authority.

RED/GREEN and acceptance:

```text
uv run pytest packages/db/tests/test_heartbeat.py packages/secrets/tests/test_crypto.py apps/api/tests/test_health.py apps/api/tests/test_observability.py services/agent_worker/tests/test_telemetry.py services/event_worker/tests/test_telemetry.py services/tool_worker/tests/test_health_heartbeat.py services/tool_worker/tests/test_advertised_tools.py services/tool_worker/tests/test_telemetry.py services/tool_worker/tests/test_worker_registration.py tests/test_service_heartbeat_wiring.py tests/test_worker_dependency_boundaries.py -q
uv lock
uv lock --check
uv run pytest apps/api/tests services/agent_worker/tests services/event_worker/tests services/tool_worker/tests packages/db/tests packages/secrets/tests -q
uv run python scripts/audit_phase10_logging.py
uv run ruff check apps/api/src/jhin_api/main.py apps/api/tests/test_health.py apps/api/tests/test_observability.py packages/db/src/jhin_db/__init__.py packages/db/src/jhin_db/heartbeat.py packages/db/tests/test_heartbeat.py packages/secrets/src/jhin_secrets/crypto.py packages/secrets/tests/test_crypto.py services/agent_worker/src/jhin_agent_worker/main.py services/agent_worker/tests/test_telemetry.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/tests/test_telemetry.py services/tool_worker/src/jhin_tool_worker/main.py services/tool_worker/src/jhin_tool_worker/resources.py services/tool_worker/tests/test_advertised_tools.py services/tool_worker/tests/test_health_heartbeat.py services/tool_worker/tests/test_telemetry.py services/tool_worker/tests/test_worker_registration.py tests/test_service_heartbeat_wiring.py tests/test_worker_dependency_boundaries.py
uv run ruff format --check apps/api/src/jhin_api/main.py apps/api/tests/test_health.py apps/api/tests/test_observability.py packages/db/src/jhin_db/__init__.py packages/db/src/jhin_db/heartbeat.py packages/db/tests/test_heartbeat.py packages/secrets/src/jhin_secrets/crypto.py packages/secrets/tests/test_crypto.py services/agent_worker/src/jhin_agent_worker/main.py services/agent_worker/tests/test_telemetry.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/tests/test_telemetry.py services/tool_worker/src/jhin_tool_worker/main.py services/tool_worker/src/jhin_tool_worker/resources.py services/tool_worker/tests/test_advertised_tools.py services/tool_worker/tests/test_health_heartbeat.py services/tool_worker/tests/test_telemetry.py services/tool_worker/tests/test_worker_registration.py tests/test_service_heartbeat_wiring.py tests/test_worker_dependency_boundaries.py
uv run mypy
```

RED is valid only for named validation/cadence/probe/lifecycle behavior, never collection,
undefined fixture, network, or daemon failure.
`services/tool_worker/tests/test_health_heartbeat.py` must use `httpx.MockTransport` to prove the
exact request method/path and absence rules, 1,024/cap+one handling, strict UTF-8/JSON and exact
success predicate, Boolean-only failures, finite timeout, all-path client closure, and canary-free
heartbeat/log output.
Its sole staging invocation is
`stage_exact_task "$task2_subject" "${task2_paths[@]}" || exit 1`.

### Task 3: opaque public health and bounded probes

**Files:**
- Modify: `apps/api/src/jhin_api/health/checks.py`
- Modify: `apps/api/src/jhin_api/health/router.py`
- Modify: `apps/api/src/jhin_api/health/schemas.py`
- Modify: `apps/api/src/jhin_api/health/service.py`
- Modify: `apps/api/tests/test_health.py`
- Modify: `apps/api/tests/test_temporal_provider.py`
- Modify: `packages/events/src/jhin_events/streams.py`
- Modify: `packages/events/tests/test_streams.py`
- Modify: `packages/workflows/src/jhin_workflows/poller_health.py`
- Modify: `packages/workflows/tests/test_poller_health.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`

**Interfaces:**
- Consumes the accepted Task 2 handoff and produces the exact Task 3 subject, manifest, and gates below.

Subject: `fix: make public readiness opaque`

```bash
task3_paths=(
  apps/api/src/jhin_api/health/checks.py
  apps/api/src/jhin_api/health/router.py
  apps/api/src/jhin_api/health/schemas.py
  apps/api/src/jhin_api/health/service.py
  apps/api/tests/test_health.py
  apps/api/tests/test_temporal_provider.py
  packages/events/src/jhin_events/streams.py
  packages/events/tests/test_streams.py
  packages/workflows/src/jhin_workflows/poller_health.py
  packages/workflows/tests/test_poller_health.py
  services/event_worker/src/jhin_event_worker/settings.py
)
task3_subject="fix: make public readiness opaque"
```

Required behavior:

- liveness is exactly `{"app":"Jhin","version":"<runtime version>","status":"ok"}`;
- readiness is exactly `{"status":"ok"}`/200 or `{"status":"degraded"}`/503;
- use `HEALTH_PROBE_TIMEOUT_SECONDS=5`, `MAX_HEARTBEAT_SCAN_ROWS=4096`, and
  `MAX_TEMPORAL_POLLERS_PER_QUEUE=4096`, with deterministic cap+one degradation;
- database/schema uses the existing engine, finite timeouts, one closed head, no URL argument;
- NATS uses one short-lived no-reconnect client, exact two stream/consumer pairs, finite
  connect/probe/close bounds, and all-path cleanup;
- Temporal uses only the injected provider and extends `check_temporal(provider)`; no second client;
- poller diagnostics use exactly three queues, bounded protobuf UTC validation, and are capability
  diagnostics only;
- freshness is inclusive at 30 seconds; agent/tool/event need a fresh row; API does not; every
  fresh API/agent/tool/event row must be non-degraded; workflow remains capability-only; and
- ordinary errors become closed results, while cancellation/control-flow exceptions propagate.

RED/GREEN and acceptance:

```text
uv run pytest packages/events/tests/test_streams.py packages/workflows/tests/test_poller_health.py apps/api/tests/test_temporal_provider.py apps/api/tests/test_health.py -q
uv run pytest apps/api/tests/test_tasks_unit.py apps/api/tests/test_approvals_unit.py -q
uv run python scripts/audit_phase10_logging.py
uv run ruff check apps/api/src/jhin_api/health/checks.py apps/api/src/jhin_api/health/router.py apps/api/src/jhin_api/health/schemas.py apps/api/src/jhin_api/health/service.py apps/api/tests/test_health.py apps/api/tests/test_temporal_provider.py packages/events/src/jhin_events/streams.py packages/events/tests/test_streams.py packages/workflows/src/jhin_workflows/poller_health.py packages/workflows/tests/test_poller_health.py services/event_worker/src/jhin_event_worker/settings.py
uv run ruff format --check apps/api/src/jhin_api/health/checks.py apps/api/src/jhin_api/health/router.py apps/api/src/jhin_api/health/schemas.py apps/api/src/jhin_api/health/service.py apps/api/tests/test_health.py apps/api/tests/test_temporal_provider.py packages/events/src/jhin_events/streams.py packages/events/tests/test_streams.py packages/workflows/src/jhin_workflows/poller_health.py packages/workflows/tests/test_poller_health.py services/event_worker/src/jhin_event_worker/settings.py
uv run mypy
```

Tests include cap+one, exact freshness, invalid protobuf time, provider identity/one cached client,
cancellation, exception canaries, and exact public body equality.
Its sole staging invocation is
`stage_exact_task "$task3_subject" "${task3_paths[@]}" || exit 1`.

### Task 4: workspace-admin projection

**Files:**
- Modify: `apps/api/src/jhin_api/health/router.py`
- Modify: `apps/api/src/jhin_api/health/service.py`
- Modify: `apps/api/tests/conftest.py`
- Modify: `apps/api/tests/test_operations_health.py`

**Interfaces:**
- Consumes the accepted Task 3 handoff and produces the exact Task 4 subject, manifest, and gates below.

Subject: `feat: expose admin protected health`

```bash
task4_paths=(
  apps/api/src/jhin_api/health/router.py
  apps/api/src/jhin_api/health/service.py
  apps/api/tests/conftest.py
  apps/api/tests/test_operations_health.py
)
task4_subject="feat: expose admin protected health"
```

Required behavior:

- `AdminCtx` alone owns 401/404/403 and workspace resolution; route has no permission branching;
- inject `ObservabilityRuntimeDep`, current API identity, existing engine/settings/provider; never
  call global `get_runtime()` or create a client/runtime;
- overall deadline 15 seconds; query/probe deadline 5 seconds; cap heartbeats/connections at 4096
  and raw aggregate groups at 256, always with cap+one/total accounting and degradation;
- do not concurrently use one `AsyncSession`;
- connection SQL is workspace-scoped, excludes disabled rows, and selects only type, status,
  verification time, and SQL Boolean `last_error IS NOT NULL`; never load `last_error`;
- secret SQL is workspace-scoped and selects only key version/count; never load secret material,
  names, IDs, ciphertext, nonce, wrapped key, or fingerprint;
- replace only the same-ID persisted API row with current request; preserve other stale replicas;
- connector freshness is inclusive at 300 seconds, unhealthy precedes unverified, unknown collapses
  to `other`;
- healthy secret versions lie in the intersection of every fresh valid reporter tuple, and every
  API/agent/tool reporter agrees exactly with current API active/supported tuple;
- telemetry status is called once, sanitized on failure, present but excluded from product overall;
  and critical database/NATS/Temporal down outranks other product degradation.

RED/GREEN and acceptance:

```text
uv run pytest apps/api/tests/test_operations_health.py -q
uv run pytest apps/api/tests/test_operations_health.py apps/api/tests/test_health.py apps/api/tests/test_temporal_provider.py -q
uv run pytest apps/api/tests/test_connections_unit.py apps/api/tests/test_policy_rbac.py -q
uv run python scripts/audit_phase10_logging.py
uv run ruff check apps/api/src/jhin_api/health apps/api/tests/test_operations_health.py apps/api/tests/conftest.py
uv run ruff format --check apps/api/src/jhin_api/health apps/api/tests/test_operations_health.py apps/api/tests/conftest.py
uv run mypy
```

Tests prove full-depth foreign-workspace absence, no raw error load, caps, tuple intersection,
runtime failure sanitation, current API injection, 30-second boundary, and hostile serialization.
Its sole staging invocation is
`stage_exact_task "$task4_subject" "${task4_paths[@]}" || exit 1`.

### Task 5: admin-only UI and opaque overview

**Files:**
- Modify: `apps/web/app/(app)/operations/page.tsx`
- Modify: `apps/web/app/(app)/page.tsx`
- Modify: `apps/web/components/app-shell.tsx`
- Modify: `apps/web/lib/hooks.ts`
- Modify: `apps/web/lib/types.ts`
- Modify: `apps/web/tests/operations-navigation.test.tsx`
- Modify: `apps/web/tests/operations-page.test.tsx`
- Modify: `apps/web/tests/overview-health.test.tsx`

**Interfaces:**
- Consumes the accepted Task 4 handoff and produces the exact Task 5 subject, manifest, and gates below.

Subject: `feat: add protected operations health view`

```bash
task5_paths=(
  'apps/web/app/(app)/operations/page.tsx'
  'apps/web/app/(app)/page.tsx'
  apps/web/components/app-shell.tsx
  apps/web/lib/hooks.ts
  apps/web/lib/types.ts
  apps/web/tests/operations-navigation.test.tsx
  apps/web/tests/operations-page.test.tsx
  apps/web/tests/overview-health.test.tsx
)
task5_subject="feat: add protected operations health view"
```

Required behavior:

- canonical `WorkspaceRole`, `HeartPulse`, and explicit typed `NavItem`;
- Operations nav/query only for admin/owner and visible document;
- `retry:false`, `refetchOnWindowFocus:false`, `refetchOnReconnect:false`;
- exactly one visibility listener, cleanup, and exactly one hidden-to-visible refetch;
- role/workspace changes never reveal cached protected data; unauthorized branches make zero
  protected requests;
- overview consumes only anonymous readiness status; and
- presentation is a closed field/action allowlist, never recursive rendering, raw identifiers,
  unknown objects, infrastructure links, or a future runbook link.

RED/GREEN and acceptance:

```text
pnpm --filter jhin-web test -- operations-page.test.tsx overview-health.test.tsx operations-navigation.test.tsx
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web build
```

Tests prove request counts, no focus/reconnect fetch, one visibility refetch/listener cleanup,
role/workspace isolation, opaque overview, safe actions, and hostile extra-field non-rendering.
Its sole staging invocation is
`stage_exact_task "$task5_subject" "${task5_paths[@]}" || exit 1`.

### Task 6: leased two-mode failure/recovery evidence

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/phase10_upgrade_harness.py`
- Modify: `tests/integration/test_phase10_protected_health.py`
- Modify: `tests/integration/test_stack_health.py`
- Modify: `tests/test_phase10_protected_health_harness.py`

**Interfaces:**
- Consumes the accepted Task 5 handoff and produces the exact Task 6 subject, manifest, and gates below.

Subject: `test: verify protected health recovery`

```bash
task6_paths=(
  .github/workflows/ci.yml
  Makefile
  tests/integration/conftest.py
  tests/integration/phase10_upgrade_harness.py
  tests/integration/test_phase10_protected_health.py
  tests/integration/test_stack_health.py
  tests/test_phase10_protected_health_harness.py
)
task6_subject="test: verify protected health recovery"
```

No protected shell runner exists. Delete every tracked-plan instruction for a runner,
`StackContract`, resolver, `JHIN_PHASE10_SUITE`, raw Compose, fixed/manual project/port/lifecycle,
manual key fixture/cleanup, separate socket resolver, or new CI job. No Compose/profile/telemetry
artifact path changes.

Add exactly one scenario:

```python
"protected-health": LiveScenario(
    nodes=(
        "tests/integration/test_phase10_protected_health_migration.py",
        "tests/integration/test_phase10_protected_health.py::test_rbac_opacity_and_workspace_isolation",
        "tests/integration/test_phase10_protected_health.py::test_agent_loss_and_recovery",
        "tests/integration/test_phase10_protected_health.py::test_tool_loss_and_recovery",
        "tests/integration/test_phase10_protected_health.py::test_event_loss_and_recovery",
        "tests/integration/test_phase10_protected_health.py::test_sandbox_loss_and_recovery",
        "tests/integration/test_phase10_protected_health.py::test_nats_loss_and_recovery",
        "tests/integration/test_phase10_protected_health.py::test_temporal_loss_and_recovery",
        "tests/integration/test_phase10_protected_health.py::test_postgres_loss_and_recovery",
        "tests/integration/test_phase10_protected_health.py::test_connector_verification_recovery",
        "tests/integration/test_phase10_protected_health.py::test_key_reporter_mismatch_recovery",
    ),
    expected_tests=12,
    observability=False,
)
```

The migration module contributes two tests and the ten explicit nodes are non-parameterized. Any
node/count split change is a reviewed plan amendment. Never use `-k`, deselection, prose counts,
skips, or xfails. The harness test binds this exact list/count and proves telemetry scenarios are
unchanged.

Extend only the leased authority/`Stack` with typed bounded API/web calls; exact service lifecycle
operations; NATS/Temporal capability inspection; accepted fake/test-data controls; and a fixed
row-scoped heartbeat/key mismatch operation that restores in `finally`. All authority values come
from the immutable lease. Do not widen generic Compose to arbitrary lifecycle/log/exec/file/
profile/project/socket/environment control. All waits are monotonic and finite; HTTP is capped,
strictly projected, and never reports response text.

Every induced failure restores the exact previous healthy predicate in `try/finally`. Agent/tool/
event loss becomes absent only after strict `>30s`; retained pollers never grant liveness. Critical
Postgres/NATS/Temporal/sandbox failure makes anonymous readiness exactly 503/degraded and recovery
200/ok. RBAC/workspace, connector, and key tests expose only bounded sanitized aggregates. Base
profile is intentional; missing optional telemetry does not degrade product health.

Make targets are exactly:

```make
test-protected-health: ## Run socket-free protected-health unit/web gates
	uv run pytest packages/db/tests/test_service_instance_heartbeat.py packages/db/tests/test_heartbeat.py apps/api/tests/test_health.py apps/api/tests/test_operations_health.py tests/test_phase10_protected_health_harness.py -q
	pnpm --filter jhin-web test -- operations-page.test.tsx overview-health.test.tsx operations-navigation.test.tsx

test-protected-health-integration: ## Run protected health through one lease
	env -u JHIN_PHASE10_SAFE_ARTIFACT_DIR -u JHIN_TELEMETRY_CANARY_FILE $(PHASE10_HARNESS) run --mode $(PHASE10_MODE) --scenario protected-health
```

Add the integration target after telemetry base/observed acceptance in both existing live jobs,
under each job's existing owner/workspace/socket/cache/GID or no-GID environment. Do not add a
third job or mode substitute.

The existing telemetry step name remains exactly `Telemetry base and observed acceptance`. Add
one immediately following step in each job named exactly `Protected health acceptance`; it invokes
only `make test-protected-health-integration` inside that job's already-verified authority context
and must complete rather than skip. The jobs API step number for telemetry must be lower than the
protected step number.

After each one-shot's strict collection result and exhaustive second-pass cleanup/image-absence
proof succeed, the sole harness emits exactly one safe line; it emits none on test, capture, or
cleanup uncertainty:

```text
phase10-scenario-accepted scenario=telemetry-base expected_tests=1 passed=1 skipped=0 xfailed=0 deselected=0 resources=absent images=absent
phase10-scenario-accepted scenario=telemetry-observed expected_tests=12 passed=12 skipped=0 xfailed=0 deselected=0 resources=absent images=absent
phase10-scenario-accepted scenario=protected-health expected_tests=12 passed=12 skipped=0 xfailed=0 deselected=0 resources=absent images=absent
```

The first two lines arise in the telemetry step and the third in the following protected step.
`tests/test_phase10_protected_health_harness.py` must inject success/failure/cleanup recorders and
prove exact-once ordered emission after cleanup, no emission before cleanup or on any failure, and
the exact fixed fields/counts above. These lines contain no project, socket, port, container/image
ID, environment, canary, or product identifier.

RED/GREEN and live acceptance:

```bash
uv run pytest tests/test_phase10_protected_health_harness.py tests/integration/test_stack_health.py -q
make test-protected-health
uv run pytest -m "not integration" -q
uv run python scripts/audit_phase10_logging.py
uv run ruff check tests/integration/conftest.py tests/integration/phase10_upgrade_harness.py tests/integration/test_phase10_protected_health.py tests/integration/test_stack_health.py tests/test_phase10_protected_health_harness.py
uv run ruff format --check tests/integration/conftest.py tests/integration/phase10_upgrade_harness.py tests/integration/test_phase10_protected_health.py tests/integration/test_stack_health.py tests/test_phase10_protected_health_harness.py
uv run mypy
verified_mode="${PHASE10_MODE:?set the already-verified local mode}"
validate_local_phase10_mode "$verified_mode" || exit 1
PHASE10_MODE="$verified_mode" make test-protected-health-integration
```

A local machine runs only a mode whose socket authority it verifies. Final pushed CI must run both;
neither mode may be skipped, emulated, or substituted.
Its sole staging invocation is
`stage_exact_task "$task6_subject" "${task6_paths[@]}" || exit 1`.

### Task 7: documentation, full gate, history, fresh CI

**Files:**
- Modify: `README.md`
- Modify: `docs/operations/protected-health.md`

**Interfaces:**
- Consumes the accepted Task 6 handoff and produces the exact Task 7 subject, manifest, and gates below.

Subject: `docs: explain protected health operations`

```bash
task7_paths=(
  README.md
  docs/operations/protected-health.md
)
task7_subject="docs: explain protected health operations"
```

Documentation must explain one telemetry runtime/provider, one leased authority, monotonic
start-to-start cadence, inclusive 30-second freshness, seven-day retention, current-request API
injection plus stale replica visibility, diagnostic-only heartbeats/pollers, exact opaque public
bodies, admin-only projection, queues/consumers, workspace-scoped connector/secret aggregation,
key tuple intersection, caps/overflow degradation, status precedence, closed actions, telemetry
exclusion, sandbox 503-to-200 recovery, and strict worker staleness. It must contain no raw
infrastructure URL/port, credential/sensitive example, invented future link, or anonymous example
of the protected route.

Run fresh, in order, before the docs commit:

```bash
uv lock --check
uv run pytest packages/db/tests/test_migration_graph.py packages/db/tests/test_service_instance_heartbeat.py packages/db/tests/test_heartbeat.py packages/secrets/tests/test_crypto.py packages/events/tests/test_streams.py packages/workflows/tests/test_poller_health.py apps/api/tests/test_temporal_provider.py apps/api/tests/test_health.py apps/api/tests/test_observability.py apps/api/tests/test_operations_health.py services/agent_worker/tests/test_telemetry.py services/event_worker/tests/test_telemetry.py services/tool_worker/tests/test_health_heartbeat.py services/tool_worker/tests/test_advertised_tools.py services/tool_worker/tests/test_telemetry.py services/tool_worker/tests/test_worker_registration.py tests/test_service_heartbeat_wiring.py tests/test_worker_dependency_boundaries.py tests/test_phase10_protected_health_harness.py tests/integration/test_stack_health.py -q
uv run pytest -m "not integration" -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/audit_phase10_logging.py
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web build
uv run python scripts/build_phase10_dashboard.py --check
uv run pytest tests/test_phase10_observability_compose.py tests/test_phase10_tool_worker_compose.py tests/test_phase9_production_compose.py -q
verified_mode="${PHASE10_MODE:?set the already-verified local mode}"
validate_local_phase10_mode "$verified_mode" || exit 1
PHASE10_MODE="$verified_mode" make test-protected-health-integration
```

Run the response-leak review over exactly these paths:

```bash
leak_scan_paths=(
  apps/api/src/jhin_api/health
  apps/api/tests/test_health.py
  apps/api/tests/test_operations_health.py
  'apps/web/app/(app)/operations'
  'apps/web/app/(app)/page.tsx'
  apps/web/components/app-shell.tsx
  apps/web/lib/hooks.ts
  apps/web/lib/types.ts
  README.md
  docs/operations/protected-health.md
)
for path in "${leak_scan_paths[@]}"; do
  test -e "$path" && test -r "$path" || exit 1
done
set +e
rg -n 'dependencies|detail|exception|traceback|hostname|\bhost\b|\bport\b|\bdsn\b' -- \
  "${leak_scan_paths[@]}"
leak_scan_status=$?
set -e
case "$leak_scan_status" in
  0) ;; # every printed match requires explicit review before continuing
  1) ;; # no match
  *) exit "$leak_scan_status" ;;
esac
```

An unreviewed match, missing/unreadable path, or search error fails. This word scan never
substitutes for strict schema, full-depth foreign-workspace, or canary tests.

The docs commit's sole staging invocation is
`stage_exact_task "$task7_subject" "${task7_paths[@]}" || exit 1`.

After that two-path docs commit, require empty index, no worktree override on the exact Task 1-7
owned-path union, both plan blobs equal the checkpoint, exact telemetry tip, seven unique subjects
and manifests, and no intervening commit. Then push exact Task 7 head and require a new successful
exact-head required CI run. Both existing live jobs must execute telemetry base then observed then
protected, with strict counts and exhaustive cleanup. Do not amend docs to embed its own run ID.

#### Common exact staging invariant


The exact manifest counts are `0/9/22/11/4/8/7/2` for Tasks 0-7. The plan's global File Map,
each `Files` block, and each array must be exact mirrors. Define this helper once in the plan's
staging contract, then invoke it from each mutating task with that task's literal numbered array
and subject variable:

```bash
set -euo pipefail

stage_exact_task() {
  local task_subject="$1"
  local duplicate_paths expected_index actual_index committed_subject
  local actual_commit_paths resolved_task_commit current_head
  shift
  test "$#" -gt 0 || return 1
  require_empty_index || return 1
  git status --short -- "$@" || return 1
  git diff --check -- "$@" || return 1
  duplicate_paths="$(printf '%s\n' "$@" | LC_ALL=C sort | uniq -d)" || return 1
  test -z "$duplicate_paths" || return 1
  git add -- "$@" || return 1
  expected_index="$(printf '%s\n' "$@" | LC_ALL=C sort)" || return 1
  actual_index="$(git diff --cached --name-only | LC_ALL=C sort)" || return 1
  test "$actual_index" = "$expected_index" || return 1
  git diff --cached --check -- "$@" || return 1
  git commit --only "$@" -m "$task_subject" || return 1
  committed_subject="$(git show -s --format=%s HEAD)" || return 1
  test "$committed_subject" = "$task_subject" || return 1
  actual_commit_paths="$(git diff-tree --no-commit-id --name-only -r HEAD | LC_ALL=C sort)" || return 1
  test "$actual_commit_paths" = "$expected_index" || return 1
  resolved_task_commit="$(unique_commit_with_subject "$task_subject")" || return 1
  current_head="$(git rev-parse HEAD)" || return 1
  test "$resolved_task_commit" = "$current_head" || return 1
  require_empty_index || return 1
}
```

Sections 5-11 give the one exact invocation for each task; do not duplicate an invocation in the
global helper section.

Any no-delta expected path, unexpected staged path, path outside the task manifest, duplicate path
or subject, commit-tree mismatch, or nonempty post-commit index fails closed. Never stage either
tracked plan, a predecessor telemetry path not in the current task array, Compose/profile source,
workflow-worker, rootless adapter, telemetry artifact, or any forbidden path.

#### Exact history and release acceptance


Immediately after the accepted telemetry Task 12 commit, the protected subjects are exactly:

```bash
protected_subjects=(
  "feat: add durable service heartbeats"
  "feat: publish sanitized service heartbeats"
  "fix: make public readiness opaque"
  "feat: expose admin protected health"
  "feat: add protected operations health view"
  "test: verify protected health recovery"
  "docs: explain protected health operations"
)
protected_counts=(9 22 11 4 8 7 2)
```

Resolve each subject with `unique_commit_with_subject`, compare each full `diff-tree` with its exact
array, and require the newline-preserving result of
`git rev-list --reverse "$telemetry_tip"..HEAD` to equal those seven commit IDs in order with no
intervening commit. The checkpoint-through-protected sequence is twenty commits only as a secondary
sanity count: checkpoint + twelve telemetry + seven protected. Counts never replace path equality.

Acceptance has two distinct fresh-CI gates:

1. **Before protected Task 1:** pushed telemetry Task 12 exact head has a new successful required
   CI run whose two existing live jobs pass telemetry base then observed with exact checkout,
   counts, and cleanup.
2. **After protected Task 7:** pushed protected Task 7 exact head has a newer successful required
   CI run whose two existing live jobs pass telemetry base then observed then protected, with exact
   checkout, counts `1/12/12`, zero skip/xfail/deselection, and exhaustive cleanup/image absence.

After pushing Task 7, run this exact verifier. It proves the final run is later than the accepted
telemetry-tip run, binds the PR head and fetched synthetic merge, requires the two unique live
jobs and ordered named steps, checks both checkout logs, and accepts only the three exact
post-cleanup scenario lines from Task 6:

```bash
protected_checkout_log_has_sha() {
  local log_path="$1"
  local expected_sha="$2"
  awk -v sha="$expected_sha" '
    index($0, "/usr/bin/git log -1 --format") && index($0, "%H") {
      remaining = 4
      next
    }
    remaining > 0 {
      if (index($0, sha)) { found = 1; exit }
      remaining--
    }
    END { exit(found ? 0 : 1) }
  ' "$log_path"
}

protected_log_has_ordered_acceptance() {
  local log_path="$1"
  awk '
    BEGIN {
      base = "phase10-scenario-accepted scenario=telemetry-base expected_tests=1 passed=1 skipped=0 xfailed=0 deselected=0 resources=absent images=absent"
      observed = "phase10-scenario-accepted scenario=telemetry-observed expected_tests=12 passed=12 skipped=0 xfailed=0 deselected=0 resources=absent images=absent"
      protected = "phase10-scenario-accepted scenario=protected-health expected_tests=12 passed=12 skipped=0 xfailed=0 deselected=0 resources=absent images=absent"
    }
    index($0, base) {
      base_count++
      if (state != 0) invalid = 1
      state = 1
      next
    }
    index($0, observed) {
      observed_count++
      if (state != 1) invalid = 1
      state = 2
      next
    }
    index($0, protected) {
      protected_count++
      if (state != 2) invalid = 1
      state = 3
      next
    }
    END {
      exit(!invalid && state == 3 && base_count == 1 &&
        observed_count == 1 && protected_count == 1 ? 0 : 1)
    }
  ' "$log_path"
}

cleanup_protected_ci_logs() {
  local directory="$1"
  local rootful_log="$2"
  local rootless_log="$3"
  rm -f -- "$rootful_log" "$rootless_log" || return 1
  rmdir -- "$directory" || return 1
}

verify_final_protected_ci() {
  local telemetry_tip="$1"
  local final_tip="$2"
  local repo pr_number pr_json pr_head pr_base synthetic_merge
  local head_json merge_json head_tree merge_tree merge_parents commit_time
  local telemetry_runs_json telemetry_run_started_at
  local runs_json final_run_id run_json run_head run_event run_status
  local run_conclusion run_path run_pr_number run_started_at jobs_json
  local rootful_job_id rootless_job_id job_id log_dir rootful_log rootless_log
  repo=jhinhq/Jhin
  pr_number=1

  printf '%s\n' "$telemetry_tip" "$final_tip" | \
    awk 'length($0) == 40 && $0 ~ /^[0-9a-f]+$/ { valid++ }
      END { exit(valid == 2 ? 0 : 1) }' || return 1
  test "$telemetry_tip" != "$final_tip" || return 1

  pr_json="$(gh api "repos/$repo/pulls/$pr_number")" || return 1
  pr_head="$(jq -er .head.sha <<<"$pr_json")" || return 1
  pr_base="$(jq -er .base.sha <<<"$pr_json")" || return 1
  synthetic_merge="$(jq -er .merge_commit_sha <<<"$pr_json")" || return 1
  test "$pr_head" = "$final_tip" || return 1
  printf '%s\n' "$pr_base" "$synthetic_merge" | \
    awk 'length($0) == 40 && $0 ~ /^[0-9a-f]+$/ { valid++ }
      END { exit(valid == 2 ? 0 : 1) }' || return 1

  head_json="$(gh api "repos/$repo/git/commits/$final_tip")" || return 1
  merge_json="$(gh api "repos/$repo/git/commits/$synthetic_merge")" || return 1
  head_tree="$(jq -er .tree.sha <<<"$head_json")" || return 1
  merge_tree="$(jq -er .tree.sha <<<"$merge_json")" || return 1
  merge_parents="$(jq -er '.parents | map(.sha) | join(" ")' \
    <<<"$merge_json")" || return 1
  commit_time="$(jq -er .committer.date <<<"$head_json")" || return 1
  test "$merge_parents" = "$pr_base $final_tip" || return 1
  test "$merge_tree" = "$head_tree" || return 1

  telemetry_runs_json="$(gh api \
    "repos/$repo/actions/runs?event=pull_request&head_sha=$telemetry_tip&per_page=100")" || return 1
  telemetry_run_started_at="$(jq -er --arg sha "$telemetry_tip" '
    [.workflow_runs[] |
      select(.head_sha == $sha and .event == "pull_request" and
        .status == "completed" and .conclusion == "success" and
        .path == ".github/workflows/ci.yml" and .run_started_at != null)] |
    sort_by(.run_started_at, .id) | last | .run_started_at
  ' <<<"$telemetry_runs_json")" || return 1

  runs_json="$(gh api \
    "repos/$repo/actions/runs?event=pull_request&head_sha=$final_tip&per_page=100")" || return 1
  final_run_id="$(jq -er --arg sha "$final_tip" '
    [.workflow_runs[] |
      select(.head_sha == $sha and .event == "pull_request" and
        .status == "completed" and .conclusion == "success" and
        .path == ".github/workflows/ci.yml" and .run_started_at != null)] |
    sort_by(.run_started_at, .id) | last | .id
  ' <<<"$runs_json")" || return 1

  run_json="$(gh api "repos/$repo/actions/runs/$final_run_id")" || return 1
  run_head="$(jq -er .head_sha <<<"$run_json")" || return 1
  run_event="$(jq -er .event <<<"$run_json")" || return 1
  run_status="$(jq -er .status <<<"$run_json")" || return 1
  run_conclusion="$(jq -er .conclusion <<<"$run_json")" || return 1
  run_path="$(jq -er .path <<<"$run_json")" || return 1
  run_pr_number="$(jq -er '.pull_requests | if length == 1 then .[0].number else error("PR mismatch") end' \
    <<<"$run_json")" || return 1
  run_started_at="$(jq -er .run_started_at <<<"$run_json")" || return 1
  test "$run_head" = "$final_tip" || return 1
  test "$run_event" = pull_request || return 1
  test "$run_status" = completed || return 1
  test "$run_conclusion" = success || return 1
  test "$run_path" = .github/workflows/ci.yml || return 1
  test "$run_pr_number" = "$pr_number" || return 1
  jq -en --arg final_run "$run_started_at" --arg telemetry_run "$telemetry_run_started_at" \
    --arg commit_time "$commit_time" \
    '$final_run > $telemetry_run and $final_run >= $commit_time' >/dev/null || return 1

  jobs_json="$(gh api \
    "repos/$repo/actions/runs/$final_run_id/jobs?per_page=100")" || return 1
  rootful_job_id="$(jq -er '
    [.jobs[] | select(.name == "Phase 10 rootful live boundary")] |
    if length != 1 then error("rootful job mismatch") else .[0] end |
    if .status == "completed" and .conclusion == "success" then .id
    else error("rootful job failed") end
  ' <<<"$jobs_json")" || return 1
  rootless_job_id="$(jq -er '
    [.jobs[] | select(.name == "Phase 10 rootless live boundary")] |
    if length != 1 then error("rootless job mismatch") else .[0] end |
    if .status == "completed" and .conclusion == "success" then .id
    else error("rootless job failed") end
  ' <<<"$jobs_json")" || return 1
  test "$rootful_job_id" != "$rootless_job_id" || return 1

  for job_id in "$rootful_job_id" "$rootless_job_id"; do
    jq -e --argjson id "$job_id" '
      [.jobs[] | select(.id == $id)] |
      if length != 1 then error("job mismatch") else .[0] end |
      ([.steps[] | select(.name == "Telemetry base and observed acceptance")] |
        if length != 1 then error("telemetry step mismatch") else .[0] end) as $telemetry |
      ([.steps[] | select(.name == "Protected health acceptance")] |
        if length != 1 then error("protected step mismatch") else .[0] end) as $protected |
      ($telemetry.status == "completed" and $telemetry.conclusion == "success" and
        $protected.status == "completed" and $protected.conclusion == "success" and
        $telemetry.number < $protected.number)
    ' <<<"$jobs_json" >/dev/null || return 1
  done

  log_dir="$(mktemp -d "${TMPDIR:-/tmp}/jhin-final-protected-ci.XXXXXX")" || return 1
  rootful_log="$log_dir/rootful.log"
  rootless_log="$log_dir/rootless.log"
  if ! gh run view "$final_run_id" --repo "$repo" \
    --job "$rootful_job_id" --log >"$rootful_log"; then
    cleanup_protected_ci_logs "$log_dir" "$rootful_log" "$rootless_log"
    return 1
  fi
  if ! gh run view "$final_run_id" --repo "$repo" \
    --job "$rootless_job_id" --log >"$rootless_log"; then
    cleanup_protected_ci_logs "$log_dir" "$rootful_log" "$rootless_log"
    return 1
  fi
  for log_path in "$rootful_log" "$rootless_log"; do
    if ! protected_checkout_log_has_sha "$log_path" "$synthetic_merge"; then
      cleanup_protected_ci_logs "$log_dir" "$rootful_log" "$rootless_log"
      return 1
    fi
    if ! protected_log_has_ordered_acceptance "$log_path"; then
      cleanup_protected_ci_logs "$log_dir" "$rootful_log" "$rootless_log"
      return 1
    fi
  done
  cleanup_protected_ci_logs "$log_dir" "$rootful_log" "$rootless_log" || return 1
}

telemetry_tip="$(unique_commit_with_subject \
  'docs(observability): record Phase 10 telemetry evidence')" || exit 1
final_protected_tip="$(unique_commit_with_subject \
  'docs: explain protected health operations')" || exit 1
head_commit="$(git rev-parse HEAD)" || exit 1
test "$head_commit" = "$final_protected_tip" || exit 1
require_empty_index || exit 1
verify_final_protected_ci "$telemetry_tip" "$final_protected_tip" || exit 1
require_empty_index || exit 1
```

Local GREEN, a committed evidence file, a failure artifact, a synthetic-merge run without PR-head
proof, or a stale exact-head run cannot satisfy either gate. Until the second fresh run succeeds,
the seven commits may exist but protected health is not accepted.

#### Binding release blockers


Stop execution or acceptance on any of:

- missing P3 or P4 at checkpoint;
- Task 0 checkpoint/evidence/history/fresh-CI failure;
- subject, manifest, array, File Map, or commit-tree drift;
- nonempty index or out-of-scope worktree override;
- raw/parallel Compose, second socket/project/file/profile/port authority, or protected shell runner;
- protected scenario absent from either existing live job;
- skip, xfail, deselection, count drift, timeout, incomplete cleanup, or uncertain survivor state;
- plan blob drift after checkpoint;
- unregistered/non-JSON application logging or privacy/canary leakage;
- unbounded network/RPC/DB/body/row/group/poller/wait behavior;
- heartbeat used as authority, workflow liveness, lease, election, or effect gate;
- any access to the forbidden protected external path; or
- missing fresh exact-head required CI after the final pushed protected Task 7 head.

#### Required combined structural validation


After applying the final telemetry addendum and this addendum to the two tracked plans, run a
socket-free, network-free structural validator that fails unless:

- only the two tracked plans are changed and the entire index is empty before staging;
- protected Task 0 has no `Files` path and no commit;
- protected Task 1-7 `Files` blocks and arrays equal the exact manifests above with counts
  `9/22/11/4/8/7/2` and no duplicates;
- all seven exact subjects appear once in their task sections;
- telemetry Task 1 contains `health.heartbeat_write_failed` in its closed registry contract;
- telemetry Task 8 contains the validated sandbox-runner base-URL helper contract;
- telemetry Task 1-12 counts/subjects/manifests satisfy telemetry section 20's combined validator;
- protected Task 0 contains the literal guarded checkpoint comparison
  `git diff --quiet "$checkpoint_commit" HEAD -- "$path"`, the exact checkpoint and final telemetry
  subjects/manifests, the evidence parser plus focused tests, the ordered twelve-commit history
  proof, and the executable `verify_final_telemetry_ci` fresh-run verifier required by telemetry
  section 20;
- the protected plan contains both fresh-CI gates, checkpoint blob equality, exact history/path
  proof, `unique_commit_with_subject`, `validate_local_phase10_mode`, and the single leased
  `protected-health` scenario;
- the protected final gate contains executable `verify_final_protected_ci`, exact named steps
  `Telemetry base and observed acceptance` then `Protected health acceptance`, and exactly one
  ordered post-cleanup acceptance marker for each strict expected count `1/12/12` in each of the
  two existing successful live-job logs;
- every shell command substitution that supplies a gate value has an explicit `|| return 1` or
  `|| exit 1`, every duplicate-path pipeline runs under `pipefail` and is checked separately, and
  every cached/worktree diff is scoped to the literal manifest except the documented whole-index
  emptiness query;
- the protected plan contains neither Bash-4 constructs nor a shell runner/parallel stack/raw
  Compose/sentinel exception; and
- neither plan contains a command or literal that names the forbidden protected external path.

Then stage exactly the two tracked plans, commit them once with the checkpoint subject, prove the
exact two-path commit tree, and require an empty post-commit index. Do not execute implementation
work from a partially rewritten or structurally invalid plan.
