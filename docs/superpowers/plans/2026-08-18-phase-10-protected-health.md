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
- Every task follows RED -> focused GREEN -> affected regression -> lint/typecheck -> scoped commit. Never use `git add .`; never edit, stage, rename, delete, or commit `orgforge-production-implementation-plan.md`.

## File Map

```text
docs/superpowers/plans/2026-08-18-phase-10-protected-health.md reviewed execution baseline
packages/db/src/jhin_db/models/operations.py                   heartbeat ORM only
packages/db/src/jhin_db/models/__init__.py                     heartbeat model export
packages/db/src/jhin_db/heartbeat.py                           validated upsert/loop/purge contract
packages/db/src/jhin_db/__init__.py                            heartbeat helper exports
packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py
                                                                additive table and constraints
packages/db/tests/test_migration_graph.py                       one-head 0014 -> 0015 graph proof
packages/db/tests/test_service_instance_heartbeat.py            ORM/constraint proof
packages/db/tests/test_heartbeat.py                             validation/cadence/upsert/purge proof
packages/secrets/src/jhin_secrets/crypto.py                    stable active/supported version accessors
packages/secrets/tests/test_crypto.py                          single-key compatibility proof
packages/events/src/jhin_events/streams.py                     canonical durable consumer names
packages/events/tests/test_streams.py                          consumer-name contract
packages/workflows/src/jhin_workflows/poller_health.py          retained/recent Temporal capability diagnostics
packages/workflows/tests/test_poller_health.py                  timestamp-sanitization/capability CLI tests
apps/api/src/jhin_api/temporal.py                              app-lifetime Temporal provider
apps/api/src/jhin_api/deps.py                                  business dependency reuses provider
apps/api/src/jhin_api/health/schemas.py                         opaque/public + bounded protected DTOs
apps/api/src/jhin_api/health/checks.py                          bounded dependency probes, no HTTP policy
apps/api/src/jhin_api/health/service.py                         readiness and workspace-safe aggregation
apps/api/src/jhin_api/health/router.py                          anonymous and AdminCtx routes
apps/api/src/jhin_api/main.py                                  API provider/heartbeat lifecycle
apps/api/tests/test_temporal_provider.py                        one-client lifecycle/concurrency proof
apps/api/tests/test_health.py                                  opaque public/fresh-degraded probe tests
apps/api/tests/test_operations_health.py                       RBAC/projection/key-rollout/bounds tests
apps/api/tests/conftest.py                                     protected-health fixtures/overrides
services/agent_worker/src/jhin_agent_worker/main.py            agent heartbeat lifecycle
services/event_worker/src/jhin_event_worker/main.py            event heartbeat lifecycle
services/event_worker/src/jhin_event_worker/settings.py        canonical consumer defaults
services/tool_worker/src/jhin_tool_worker/main.py              tool heartbeat lifecycle
services/tool_worker/src/jhin_tool_worker/resources.py         lifetime two-second runner HTTP client
services/tool_worker/pyproject.toml                            direct httpx dependency
services/tool_worker/tests/test_health_heartbeat.py             runner-probe/heartbeat tests
tests/test_service_heartbeat_wiring.py                          ownership and topology static tests
uv.lock                                                        locked tool-worker httpx declaration
apps/web/lib/types.ts                                          exact protected-health mirrors
apps/web/lib/hooks.ts                                          visibility-aware operations polling
apps/web/components/app-shell.tsx                              role-filtered Operations navigation
apps/web/app/(app)/page.tsx                                    opaque overview badge/admin link
apps/web/app/(app)/operations/page.tsx                         allowlisted health cards/tables
apps/web/tests/operations-page.test.tsx                        page/action/opacity contract
apps/web/tests/overview-health.test.tsx                        anonymous overview contract
apps/web/tests/operations-navigation.test.tsx                  role/nav/direct-access contract
tests/integration/test_phase10_protected_health_migration.py    owned two-DB PG fixture/base/reversal proof
tests/integration/test_phase10_protected_health.py              live RBAC/opacity/kill/recovery proof
tests/test_phase10_protected_health_harness.py                  executable Make/helper mode contract
tests/integration/conftest.py                                  rootful/rootless Compose harness
tests/integration/test_stack_health.py                         opaque stack-health assertions
Makefile                                                       render/rootful/rootless health gates
docs/operations/protected-health.md                            operator interpretation/runbook
README.md                                                      opaque endpoint/admin-surface index

Read-only prior-plan inputs: compose.yaml, compose.dev.yaml, compose.rootless.yaml,
compose.rootful.yaml,
services/workflow_worker/src/jhin_workflow_worker/main.py, and
orgforge-production-implementation-plan.md. No task stages or commits those files.
```

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
    def __init__(self, settings: Settings) -> None: ...
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

# apps/web/components/app-shell.tsx
interface NavItem {
  href: string;
  label: string;
  icon: typeof ClipboardList;
  minimumRole?: WorkspaceRole;
}
```

---

### Task 0: Check In the Reviewed Protected-Health Execution Baseline

**Files:**
- Create: `docs/superpowers/plans/2026-08-18-phase-10-protected-health.md`

**Interfaces:**
- Consumes: the corrected Phase 10 design plus completed subprojects 1 and 2.
- Produces: a tracked implementation sequence; no runtime behavior.

- [ ] **Step 1: Stage only this plan and inspect the index**

```bash
git add docs/superpowers/plans/2026-08-18-phase-10-protected-health.md
git diff --cached --name-only
git diff --cached --check
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
```

Expected: the cached-name output is exactly the protected-health plan path and the user-owned production plan remains untracked.

- [ ] **Step 2: Commit the plan baseline**

```bash
git commit -m "docs: add Phase 10 protected health plan"
```

### Task 1: Add the Additive Heartbeat Model and Reversible `0015` Migration

**Files:**
- Create: `packages/db/src/jhin_db/models/operations.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Create: `packages/db/tests/test_service_instance_heartbeat.py`
- Create: `tests/integration/test_phase10_protected_health_migration.py`

**Interfaces:**
- Consumes: Phase 9 revision `0014`, `Base`, `JsonList`, `UtcDateTime`, and UUIDv7 generation.
- Produces: `ServiceInstanceHeartbeat` with the exact persisted fields required by `HeartbeatIdentity` and `HeartbeatState`; head `0015`; and the local `migration_databases() -> AsyncIterator[MigrationDatabases]` two-database test fixture.

- [ ] **Step 1: Write failing ORM, graph, and real-PostgreSQL migration tests**

```python
def test_protected_health_is_the_only_head_and_follows_phase9_head() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = scripts.get_revision("0015")
    assert revision is not None
    assert revision.down_revision == "0014"
    assert scripts.get_heads() == ["0015"]


async def test_heartbeat_model_preserves_sorted_key_versions(session: AsyncSession) -> None:
    row = ServiceInstanceHeartbeat(
        instance_id=new_uuid7(),
        service="tool-worker",
        version="0.1.0",
        started_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        readiness="ok",
        safe_reason_code=None,
        sandbox_reachable=True,
        active_key_version=2,
        supported_key_versions=[1, 2],
    )
    session.add(row)
    await session.commit()
    assert row.supported_key_versions == [1, 2]
```

Also assert the ORM table has primary key `instance_id`, index `(service, last_seen_at)`, and no workspace/user/secret foreign key or free-form detail/error column.

Before creating the ORM or migration, create the complete local harness in `tests/integration/test_phase10_protected_health_migration.py`. It owns two unique databases and does not reference a repository fixture that does not exist:

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from sqlalchemy import insert, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from jhin_db import create_engine
from jhin_db.migrate import alembic_config
from jhin_db.models import ServiceInstanceHeartbeat
from jhin_domain import new_uuid7

from .conftest import POSTGRES_HOST, POSTGRES_PORT

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="module"),
]

PG_USER = "jhin"
PG_PASSWORD = "jhin"
ADMIN_DSN = (
    f"postgresql://{PG_USER}:{PG_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/postgres"
)
NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


@dataclass(frozen=True)
class MigrationDatabase:
    name: str
    url: str
    engine: AsyncEngine


@dataclass(frozen=True)
class MigrationDatabases:
    fresh: MigrationDatabase
    previous_head: MigrationDatabase


def database_url(database_name: str) -> str:
    return (
        f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{database_name}"
    )


@pytest.fixture(scope="module")
async def migration_databases() -> AsyncIterator[MigrationDatabases]:
    suffix = uuid4().hex
    fresh_name = f"jhin_phase10_health_fresh_{suffix}"
    previous_head_name = f"jhin_phase10_health_previous_{suffix}"
    created_names: list[str] = []
    created_engines: list[AsyncEngine] = []
    try:
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            for database_name in (fresh_name, previous_head_name):
                await admin.execute(f'CREATE DATABASE "{database_name}"')
                created_names.append(database_name)
        finally:
            await admin.close()

        fresh_url = database_url(fresh_name)
        previous_head_url = database_url(previous_head_name)
        fresh_engine = create_engine(fresh_url)
        created_engines.append(fresh_engine)
        previous_head_engine = create_engine(previous_head_url)
        created_engines.append(previous_head_engine)
        databases = MigrationDatabases(
            fresh=MigrationDatabase(
                name=fresh_name,
                url=fresh_url,
                engine=fresh_engine,
            ),
            previous_head=MigrationDatabase(
                name=previous_head_name,
                url=previous_head_url,
                engine=previous_head_engine,
            ),
        )
        yield databases
    finally:
        for engine in created_engines:
            await engine.dispose()
        if created_names:
            cleanup_admin = await asyncpg.connect(ADMIN_DSN)
            try:
                for database_name in reversed(created_names):
                    await cleanup_admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = $1 AND pid <> pg_backend_pid()",
                        database_name,
                    )
                    await cleanup_admin.execute(
                        f'DROP DATABASE IF EXISTS "{database_name}"'
                    )
            finally:
                await cleanup_admin.close()


async def table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as connection:
        return bool(
            await connection.scalar(
                text("SELECT to_regclass(:qualified) IS NOT NULL"),
                {"qualified": f"public.{table}"},
            )
        )


async def current_revision(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        value = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert isinstance(value, str)
    return value


async def test_empty_database_upgrades_from_base_to_head(
    migration_databases: MigrationDatabases,
) -> None:
    database = migration_databases.fresh
    config = alembic_config(database.url)
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await current_revision(database.engine) == "0015"
    assert await table_exists(database.engine, "service_instance_heartbeat")
    assert await table_exists(database.engine, "agent_relationship")
```

In the same file, use only `migration_databases.previous_head` throughout the second test so the URL, config, and engine cannot drift between databases:

```python
async def test_previous_head_downgrades_and_reupgrades(
    migration_databases: MigrationDatabases,
) -> None:
    database = migration_databases.previous_head
    config = alembic_config(database.url)
    await asyncio.to_thread(command.upgrade, config, "0014")
    assert not await table_exists(database.engine, "service_instance_heartbeat")
    assert await table_exists(database.engine, "agent_relationship")

    await asyncio.to_thread(command.upgrade, config, "0015")
    assert await current_revision(database.engine) == "0015"
    assert await table_exists(database.engine, "service_instance_heartbeat")
    async with database.engine.begin() as connection:
        await connection.execute(
            insert(ServiceInstanceHeartbeat).values(
                instance_id=new_uuid7(),
                service="tool-worker",
                version="0.1.0",
                started_at=NOW,
                last_seen_at=NOW,
                readiness="ok",
                sandbox_reachable=True,
                active_key_version=1,
                supported_key_versions=[1],
            )
        )
    with pytest.raises(DBAPIError):
        async with database.engine.begin() as connection:
            await connection.execute(
                insert(ServiceInstanceHeartbeat).values(
                    instance_id=new_uuid7(),
                    service="unknown-service",
                    version="0.1.0",
                    started_at=NOW,
                    last_seen_at=NOW,
                    readiness="ok",
                    supported_key_versions=[],
                )
            )
    with pytest.raises(DBAPIError):
        async with database.engine.begin() as connection:
            await connection.execute(
                insert(ServiceInstanceHeartbeat).values(
                    instance_id=new_uuid7(),
                    service="agent-worker",
                    version="0.1.0",
                    started_at=NOW,
                    last_seen_at=NOW,
                    readiness="ok",
                    active_key_version=0,
                    supported_key_versions=[0],
                )
            )

    await asyncio.to_thread(command.downgrade, config, "0014")
    assert await current_revision(database.engine) == "0014"
    assert not await table_exists(database.engine, "service_instance_heartbeat")
    assert await table_exists(database.engine, "agent_relationship")
    await asyncio.to_thread(command.upgrade, config, "0015")
    assert await current_revision(database.engine) == "0015"
    assert await table_exists(database.engine, "service_instance_heartbeat")
```

The generated names contain only the fixed lowercase prefix plus `uuid4().hex`; no user input reaches identifier interpolation. The module loop mark keeps both tests and the module-scoped async engines on one event loop. Fixture teardown first disposes both engines, then uses the explicit admin DSN to terminate remaining sessions and drop every successfully created database in `finally`, including partial-setup failure.

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/db/tests/test_migration_graph.py packages/db/tests/test_service_instance_heartbeat.py -q
uv run pytest -m integration tests/integration/test_phase10_protected_health_migration.py -v
```

Expected: both commands FAIL because `ServiceInstanceHeartbeat` and revision `0015` do not exist; the real-PostgreSQL command must be observed failing before either implementation file is created.

- [ ] **Step 3: Implement the ORM and exact database constraints**

```python
class ServiceInstanceHeartbeat(Base):
    __tablename__ = "service_instance_heartbeat"
    __table_args__ = (
        CheckConstraint(
            "service IN ('api','agent-worker','tool-worker','event-worker')",
            name="service",
        ),
        CheckConstraint("readiness IN ('ok','degraded')", name="readiness"),
        CheckConstraint(
            "safe_reason_code IS NULL OR safe_reason_code IN "
            "('master_key_unavailable','sandbox_unreachable')",
            name="safe_reason_code",
        ),
        CheckConstraint("active_key_version IS NULL OR active_key_version > 0", name="active_key_version"),
        CheckConstraint(
            "(service = 'tool-worker' AND sandbox_reachable IS NOT NULL) OR "
            "(service <> 'tool-worker' AND sandbox_reachable IS NULL)",
            name="sandbox_owner",
        ),
        Index("ix_service_instance_heartbeat_service_seen", "service", "last_seen_at"),
    )

    instance_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    service: Mapped[str] = mapped_column(String(32))
    version: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)
    readiness: Mapped[str] = mapped_column(String(16))
    safe_reason_code: Mapped[str | None] = mapped_column(String(64), default=None)
    sandbox_reachable: Mapped[bool | None] = mapped_column(Boolean, default=None)
    active_key_version: Mapped[int | None] = mapped_column(Integer, default=None)
    supported_key_versions: Mapped[list[int]] = mapped_column(JsonList, default=list)
```

The migration creates only this table/index/check set. `downgrade()` drops only `service_instance_heartbeat`; it does not touch Phase 9's `agent_relationship`, company-identity columns, or any other `0014` object.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run pytest packages/db/tests/test_migration_graph.py packages/db/tests/test_service_instance_heartbeat.py -q
uv run pytest -m integration tests/integration/test_phase10_protected_health_migration.py -v
uv run ruff check packages/db tests/integration/test_phase10_protected_health_migration.py
uv run mypy packages/db/src tests/integration/test_phase10_protected_health_migration.py
git add packages/db/src/jhin_db/models/operations.py packages/db/src/jhin_db/models/__init__.py packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py packages/db/tests/test_migration_graph.py packages/db/tests/test_service_instance_heartbeat.py tests/integration/test_phase10_protected_health_migration.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add durable service heartbeats"
```

Expected before commit: the cached-name output is exactly the six paths in the `git add` command.

### Task 2: Implement Validated Heartbeat Writes, Key Metadata, and Service Lifecycles

**Files:**
- Create: `packages/db/src/jhin_db/heartbeat.py`
- Modify: `packages/db/src/jhin_db/__init__.py`
- Create: `packages/db/tests/test_heartbeat.py`
- Modify: `packages/secrets/src/jhin_secrets/crypto.py`
- Modify: `packages/secrets/tests/test_crypto.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Modify: `services/tool_worker/pyproject.toml`
- Create: `services/tool_worker/tests/test_health_heartbeat.py`
- Create: `tests/test_service_heartbeat_wiring.py`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Task 1 model; API/agent/tool/event session factories; `SecretCrypto`; tool-worker runner URL; prior telemetry initialization.
- Produces: every shared heartbeat interface, single-key compatibility metadata, and one immediate plus 10-second boot-scoped heartbeat loop per database-bearing service.

- [ ] **Step 1: Write all failing validation, cadence, crypto, runner-probe, and wiring tests**

```python
def test_heartbeat_state_requires_exact_service_owned_fields() -> None:
    with pytest.raises(ValueError, match="positive, sorted, unique, and bounded"):
        HeartbeatState(active_key_version=2, supported_key_versions=(2, 1))
    with pytest.raises(ValueError, match="active key version must be supported"):
        HeartbeatState(active_key_version=2, supported_key_versions=(1,))
    with pytest.raises(ValueError, match="bounded release-version syntax"):
        HeartbeatIdentity.new(service="api", version="bad/version")


@pytest.mark.parametrize("value", ["", " has-space", "bad/version", "v1\n", "v" * 65])
def test_service_version_regex_rejects_every_non_full_match(value: str) -> None:
    with pytest.raises(ValueError, match="bounded release-version syntax"):
        HeartbeatIdentity.new(service="api", version=value)


def test_service_version_regex_accepts_the_exact_64_character_boundary() -> None:
    assert HeartbeatIdentity.new(service="api", version="v" * 64).version == "v" * 64


async def test_upsert_refreshes_one_boot_instance_in_a_short_transaction(session_factory) -> None:
    identity = HeartbeatIdentity.new(service="agent-worker", version="0.1.0")
    state = HeartbeatState(active_key_version=1, supported_key_versions=(1,))
    await upsert_heartbeat(session_factory, identity, state, now=FIRST)
    await upsert_heartbeat(session_factory, identity, state, now=SECOND)
    async with session_factory() as session:
        rows = list(await session.scalars(select(ServiceInstanceHeartbeat)))
    assert len(rows) == 1
    assert rows[0].started_at == identity.started_at
    assert rows[0].last_seen_at == SECOND


async def test_purge_is_bounded_and_keeps_the_seven_day_boundary(session_factory) -> None:
    deleted = await purge_expired_heartbeats(session_factory, now=NOW)
    assert deleted == 500
    assert await count_older_than(session_factory, NOW - timedelta(days=7)) == 1
    assert await exists_at_exact_cutoff(session_factory, NOW - timedelta(days=7))


async def test_heartbeat_cadence_is_monotonic_start_to_start(monkeypatch) -> None:
    class FakeHeartbeatClock:
        def __init__(self, start: float) -> None:
            self.value = start

        def monotonic(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

        async def wait_until(self, deadline: float, stop: asyncio.Event) -> bool:
            if stop.is_set():
                return True
            self.value = max(self.value, deadline)
            return False

    clock = FakeHeartbeatClock(start=0.0)
    starts: list[float] = []
    stop = asyncio.Event()
    identity = HeartbeatIdentity(
        instance_id=new_uuid7(),
        service="tool-worker",
        version="0.1.0",
        started_at=NOW,
    )

    async def state_provider() -> HeartbeatState:
        starts.append(clock.monotonic())
        clock.advance(2.0)  # the bounded sandbox probe/write takes two seconds
        if len(starts) == 3:
            stop.set()
        return HeartbeatState(
            sandbox_reachable=True,
            active_key_version=1,
            supported_key_versions=(1,),
        )

    async def no_op_upsert(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(heartbeat, "upsert_heartbeat", no_op_upsert)
    await run_heartbeat_loop(
        cast(async_sessionmaker[AsyncSession], object()),
        identity,
        state_provider,
        stop,
        clock=clock,
    )
    assert starts == [0.0, 10.0, 20.0]
```

Also test cancellation exits the loop, a database write failure logs only `health.heartbeat_write_failed` with service/reason code (not the exception/canary), and the next scheduled start retries without terminating product work. Exercise cross-service validation by calling `upsert_heartbeat`: event-worker with key metadata, API with sandbox state, tool-worker without sandbox state, and a healthy key-bearing service without a key tuple all raise before opening a transaction.

In `test_health_heartbeat.py`, construct the real lifetime-client function with `httpx.MockTransport`: 200/`{"status":"ok","docker":true}` is true; non-200, false/missing Docker readiness, invalid JSON, connect error, and a two-second timeout are false; the request path is `/health`; headers/body contain no runner token, key version, trace baggage, or job input. In `test_crypto.py`, assert `(crypto.active_key_version, crypto.supported_key_versions, crypto.key_version) == (1, (1,), 1)` and existing ciphertext decrypts unchanged.

In `test_service_heartbeat_wiring.py`, define the standard-library AST/render helpers and these exact assertions before any service wiring is changed:

```python
from pathlib import Path
import ast
import json
import os
import subprocess


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def heartbeat_service(path: str) -> str:
    tree = ast.parse(read(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "HeartbeatIdentity"
        ):
            if node.func.attr == "new":
                for keyword in node.keywords:
                    if keyword.arg == "service" and isinstance(keyword.value, ast.Constant):
                        assert isinstance(keyword.value.value, str)
                        return keyword.value.value
    raise AssertionError(f"no HeartbeatIdentity.new(service=...) in {path}")


def compose_service(name: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment["SANDBOX_DOCKER_GID"] = "10001"  # render-only sentinel
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.rootful.yaml",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    model = json.loads(completed.stdout)
    value = model["services"][name]
    assert isinstance(value, dict)
    return value


def test_heartbeat_ownership_and_networks_are_exact() -> None:
    assert heartbeat_service("apps/api/src/jhin_api/main.py") == "api"
    assert heartbeat_service("services/agent_worker/src/jhin_agent_worker/main.py") == "agent-worker"
    assert heartbeat_service("services/tool_worker/src/jhin_tool_worker/main.py") == "tool-worker"
    assert heartbeat_service("services/event_worker/src/jhin_event_worker/main.py") == "event-worker"
    assert "run_heartbeat_loop" not in read("services/workflow_worker/src/jhin_workflow_worker/main.py")
    assert "DATABASE_URL" not in compose_service("workflow-worker")["environment"]
    assert "sandbox_runner_url" not in read("apps/api/src/jhin_api/settings.py")
    assert "sandbox_runner_url" not in read("services/agent_worker/src/jhin_agent_worker/settings.py")
```

- [ ] **Step 2: Run RED and inspect the expected failures**

```bash
uv run pytest packages/db/tests/test_heartbeat.py packages/secrets/tests/test_crypto.py services/tool_worker/tests/test_health_heartbeat.py tests/test_service_heartbeat_wiring.py -q
```

Expected: FAIL because the heartbeat module/accessors/tool probe/wiring do not exist. The cadence test must fail for a sleep-after-work loop. Do not change implementation until these failures are observed.

- [ ] **Step 3: Implement validation, PostgreSQL upsert, and bounded purge**

Use PostgreSQL `insert(ServiceInstanceHeartbeat).on_conflict_do_update(...)` with this exact update set, and a select/update fallback carrying the same fields for SQLite tests:

```python
statement = insert(ServiceInstanceHeartbeat).values(
    instance_id=identity.instance_id,
    service=identity.service,
    version=identity.version,
    started_at=identity.started_at,
    last_seen_at=observed_at,
    readiness=state.readiness,
    safe_reason_code=state.safe_reason_code,
    sandbox_reachable=state.sandbox_reachable,
    active_key_version=state.active_key_version,
    supported_key_versions=list(state.supported_key_versions),
)
statement = statement.on_conflict_do_update(
    index_elements=[ServiceInstanceHeartbeat.instance_id],
    set_={
        "service": statement.excluded.service,
        "version": statement.excluded.version,
        "last_seen_at": statement.excluded.last_seen_at,
        "readiness": statement.excluded.readiness,
        "safe_reason_code": statement.excluded.safe_reason_code,
        "sandbox_reachable": statement.excluded.sandbox_reachable,
        "active_key_version": statement.excluded.active_key_version,
        "supported_key_versions": statement.excluded.supported_key_versions,
    },
)
```

Execute the statement inside a newly acquired `async with session_factory() as session` plus `async with session.begin()` scope and return only after that short transaction closes; never accept a caller's product transaction/session. Never update `started_at` on conflict. Put intrinsic checks in the frozen dataclasses so the Step 1 constructor tests execute the real validation; keep only service/state cross-ownership in the write helper:

```python
@dataclass(frozen=True)
class HeartbeatIdentity:
    instance_id: UUID
    service: ServiceName
    version: str
    started_at: datetime

    @classmethod
    def new(cls, *, service: ServiceName, version: str) -> HeartbeatIdentity:
        return cls(
            instance_id=new_uuid7(),
            service=service,
            version=version,
            started_at=datetime.now(UTC),
        )

    def __post_init__(self) -> None:
        if type(self.instance_id) is not UUID:
            raise ValueError("heartbeat instance_id must be a UUID")
        if type(self.service) is not str or self.service not in {
            "api",
            "agent-worker",
            "tool-worker",
            "event-worker",
        }:
            raise ValueError("unknown heartbeat service")
        if type(self.version) is not str:
            raise ValueError("service version must match the bounded release-version syntax")
        if SERVICE_VERSION_PATTERN.fullmatch(self.version) is None:
            raise ValueError("service version must match the bounded release-version syntax")
        if (
            type(self.started_at) is not datetime
            or self.started_at.tzinfo is None
            or self.started_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("heartbeat started_at must be an aware UTC datetime")


@dataclass(frozen=True)
class HeartbeatState:
    readiness: HeartbeatReadiness = "ok"
    safe_reason_code: HeartbeatReason | None = None
    sandbox_reachable: bool | None = None
    active_key_version: int | None = None
    supported_key_versions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if type(self.readiness) is not str or self.readiness not in {"ok", "degraded"}:
            raise ValueError("unknown heartbeat readiness")
        if self.safe_reason_code is not None and (
            type(self.safe_reason_code) is not str
            or self.safe_reason_code not in {"master_key_unavailable", "sandbox_unreachable"}
        ):
            raise ValueError("unknown heartbeat reason")
        if self.readiness == "ok" and self.safe_reason_code is not None:
            raise ValueError("healthy heartbeat cannot have a reason")
        versions = self.supported_key_versions
        if type(versions) is not tuple:
            raise ValueError(
                "supported key versions must be positive, sorted, unique, and bounded"
            )
        if (
            len(versions) > 32
            or any(type(version) is not int for version in versions)
            or any(
                version <= 0 or version > 2_147_483_647
                for version in versions
                if type(version) is int
            )
            or (
                all(type(version) is int for version in versions)
                and versions != tuple(sorted(set(versions)))
            )
        ):
            raise ValueError(
                "supported key versions must be positive, sorted, unique, and bounded"
            )
        if self.active_key_version is None and versions:
            raise ValueError("supported key versions require an active key version")
        if self.active_key_version is not None and (
            type(self.active_key_version) is not int
            or self.active_key_version <= 0
            or self.active_key_version > 2_147_483_647
        ):
            raise ValueError("active key version must be a positive bounded integer")
        if self.active_key_version is not None and self.active_key_version not in versions:
            raise ValueError("active key version must be supported")
        if self.sandbox_reachable is not None and type(self.sandbox_reachable) is not bool:
            raise ValueError("sandbox reachability must be an exact boolean")


def _validate_service_state(identity: HeartbeatIdentity, state: HeartbeatState) -> None:
    versions = state.supported_key_versions
    key_bearing = identity.service in {"api", "agent-worker", "tool-worker"}
    if key_bearing and state.readiness == "ok":
        if state.active_key_version is None or state.active_key_version not in versions:
            raise ValueError("healthy key-bearing services must report their active supported version")
    if identity.service == "event-worker" and (state.active_key_version is not None or versions):
        raise ValueError("event-worker must not report key metadata")
    if identity.service == "tool-worker" and state.sandbox_reachable is None:
        raise ValueError("tool-worker must report sandbox reachability")
    if identity.service != "tool-worker" and state.sandbox_reachable is not None:
        raise ValueError("only tool-worker reports sandbox reachability")
    if state.safe_reason_code == "master_key_unavailable":
        if not key_bearing or state.active_key_version is not None or versions:
            raise ValueError("master-key reason requires a key-bearing service without metadata")
    if state.safe_reason_code == "sandbox_unreachable":
        if identity.service != "tool-worker" or state.sandbox_reachable is not False:
            raise ValueError("sandbox reason requires an unreachable tool-worker")
```

Implement `AsyncioHeartbeatClock` with event-loop monotonic time and `wait_until`. `run_heartbeat_loop` anchors `next_start` before the first provider call, increments it by exactly 10 seconds after each start, waits only the remaining time, and skips whole missed slots after an overrun; it never sleeps for 10 seconds after work:

```python
next_start = active_clock.monotonic()
while not stop.is_set():
    if await active_clock.wait_until(next_start, stop):
        break
    next_start += HEARTBEAT_INTERVAL_SECONDS
    try:
        state = await state_provider()
        await upsert_heartbeat(session_factory, identity, state)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("health.heartbeat_write_failed", service=identity.service)
    now_mono = active_clock.monotonic()
    if now_mono >= next_start:
        missed = math.floor((now_mono - next_start) / HEARTBEAT_INTERVAL_SECONDS) + 1
        next_start += missed * HEARTBEAT_INTERVAL_SECONDS
```

Run the bounded purge no more than once per monotonic hour per process. A two-second provider therefore starts at 0, 10, and 20 seconds; a 12-second provider skips the missed 10-second slot and next starts at 20 seconds.

- [ ] **Step 4: Add stable single-key metadata accessors**

```python
@property
def active_key_version(self) -> int:
    return self._master.version

@property
def supported_key_versions(self) -> tuple[int, ...]:
    return (self._master.version,)

@property
def key_version(self) -> int:
    return self.active_key_version
```

Tests assert only integers are exposed and existing encryption/decryption remains byte-for-byte compatible. Subproject 5 replaces the backing key container but preserves these accessors.

- [ ] **Step 5: Wire API, agent-worker, and event-worker lifecycle**

Create each identity once per process boot using exact distribution mappings `api -> jhin-api`, `agent-worker -> jhin-agent-worker`, `tool-worker -> jhin-tool-worker`, and `event-worker -> jhin-event-worker` through `importlib.metadata.version()`. API assigns the identity to `app.state.api_heartbeat_identity` before starting its loop so Task 4 can inject the currently serving instance even when its row is missing/stale. Start after the session factory exists and cancel/await it before disposing the engine:

```python
identity = HeartbeatIdentity.new(service="agent-worker", version=version("jhin-agent-worker"))

async def agent_state() -> HeartbeatState:
    return HeartbeatState(
        active_key_version=resources.crypto.active_key_version,
        supported_key_versions=resources.crypto.supported_key_versions,
    )

heartbeat_task = asyncio.create_task(
    run_heartbeat_loop(resources.session_factory, identity, agent_state, stop),
    name="agent-worker-db-heartbeat",
)
```

API reports degraded `master_key_unavailable` with empty version metadata when `app.state.secret_crypto is None`; otherwise it reports the key accessors. Event-worker returns a default `HeartbeatState()` and changes its settings defaults to the shared NATS consumer constants in Task 3. Keep existing process-local Compose heartbeat-file tasks until a later hardening plan explicitly replaces them.

- [ ] **Step 6: Wire tool-worker's bounded runner probe**

Add the direct runtime dependency `"httpx>=0.28,<1"` to `services/tool_worker/pyproject.toml`; do not rely on a transitive connector dependency. Run `uv lock` and stage `uv.lock` in this task.

```python
async def probe_sandbox_reachable(client: httpx.AsyncClient, base_url: str) -> bool:
    try:
        async with asyncio.timeout(2.0):
            response = await client.get(f"{base_url.rstrip('/')}/health")
        payload = response.json()
        return (
            response.status_code == 200
            and type(payload) is dict
            and payload.get("status") == "ok"
            and payload.get("docker") is True
        )
    except (TimeoutError, httpx.HTTPError, ValueError):
        return False

async def tool_state() -> HeartbeatState:
    reachable = await probe_sandbox_reachable(resources.sandbox_http, settings.sandbox_runner_url)
    return HeartbeatState(
        readiness="ok" if reachable else "degraded",
        safe_reason_code=None if reachable else "sandbox_unreachable",
        sandbox_reachable=reachable,
        active_key_version=resources.crypto.active_key_version,
        supported_key_versions=resources.crypto.supported_key_versions,
    )
```

Construct one app-lifetime `httpx.AsyncClient(timeout=httpx.Timeout(2.0))` in tool-worker resources and pass it to `probe_sandbox_reachable`; close it with the resources. The probe sends no runner token, trace baggage, key metadata, or job data. Tests use `httpx.MockTransport`, cover success/status/invalid JSON/timeout, assert the overall call returns within the two-second bound, and assert canary exception text never enters the heartbeat row or log.

- [ ] **Step 7: Satisfy the already-written service-ownership tests**

Keep workflow-worker unchanged and credential-free. Confirm API/agent/tool/event each instantiate exactly one boot identity with the literal service name tested in Step 1. Do not add sandbox-runner settings or the runner network to API/agent-worker, and do not add a heartbeat loop or database environment to workflow-worker. The `10001` in the static renderer remains a non-live rootful render sentinel only.

- [ ] **Step 8: Run GREEN and commit**

```bash
uv lock
uv run pytest packages/db/tests/test_heartbeat.py packages/secrets/tests/test_crypto.py services/tool_worker/tests/test_health_heartbeat.py tests/test_service_heartbeat_wiring.py -q
uv run pytest services/agent_worker/tests services/event_worker/tests services/tool_worker/tests -q
uv run ruff check packages/db packages/secrets apps/api/src/jhin_api/main.py services tests/test_service_heartbeat_wiring.py
uv run mypy packages/db/src packages/secrets/src apps/api/src services/agent_worker/src services/event_worker/src services/tool_worker/src
git add packages/db/src/jhin_db/heartbeat.py packages/db/src/jhin_db/__init__.py packages/db/tests/test_heartbeat.py packages/secrets/src/jhin_secrets/crypto.py packages/secrets/tests/test_crypto.py apps/api/src/jhin_api/main.py services/agent_worker/src/jhin_agent_worker/main.py services/event_worker/src/jhin_event_worker/main.py services/tool_worker/src/jhin_tool_worker/main.py services/tool_worker/src/jhin_tool_worker/resources.py services/tool_worker/pyproject.toml services/tool_worker/tests/test_health_heartbeat.py tests/test_service_heartbeat_wiring.py uv.lock
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: publish sanitized service heartbeats"
```

Expected before commit: the cached-name output is exactly the 14 paths in the `git add` command.

### Task 3: Make Public Health Opaque and Build Bounded Infrastructure Probes

**Files:**
- Modify: `packages/events/src/jhin_events/streams.py`
- Modify: `packages/events/tests/test_streams.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Modify: `packages/workflows/src/jhin_workflows/poller_health.py`
- Modify: `packages/workflows/tests/test_poller_health.py`
- Create: `apps/api/src/jhin_api/temporal.py`
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Create: `apps/api/tests/test_temporal_provider.py`
- Modify: `apps/api/src/jhin_api/health/schemas.py`
- Create: `apps/api/src/jhin_api/health/checks.py`
- Modify: `apps/api/src/jhin_api/health/service.py`
- Modify: `apps/api/src/jhin_api/health/router.py`
- Modify: `apps/api/tests/test_health.py`

**Interfaces:**
- Consumes: the prior tool plan's poller CLI, the existing business `TemporalDep`, canonical task queues/streams, `AsyncEngine`, NATS JetStream info, Alembic packaged head, and safe telemetry logging.
- Produces: one app-lifetime `TemporalClientProvider`, `WorkflowPollerDiagnostics(retained, recently_accessed, invalid_last_access_timestamps)`, opaque public responses, heartbeat-based live-worker readiness, bounded internal database/NATS/Temporal snapshots, and canonical event-consumer constants.

- [ ] **Step 1: Write all public, provider, heartbeat-liveness, poller-diagnostic, and probe tests first**

Replace the legacy dependency-map assertions in `test_health.py` with exact public contracts:

```python
def test_liveness_is_opaque(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"app": "Jhin", "version": "0.1.0", "status": "ok"}


def test_readiness_failure_is_opaque(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failed_connect(*args: object, **kwargs: object) -> NoReturn:
        raise ConnectionError("nats://user:password@host:4222 SECRET_CANARY")

    monkeypatch.setattr(checks.nats, "connect", failed_connect)
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}
    assert "SECRET_CANARY" not in response.text
    assert all(word not in response.text.casefold() for word in ("nats", "host", "4222"))
```

Add the success equality test `status_code == 200` and `json() == {"status": "ok"}`. Add a parameterized canary test for database, schema, NATS, and Temporal failures which scans for latency, exception class, revision, address, port, DSN, and traceback tokens.

In `test_poller_health.py`, use actual Temporal protobuf messages for valid timestamps. The 30-second window is named and tested as a diagnostic only:

```python
def poller(accessed_at: datetime | None) -> PollerInfo:
    value = PollerInfo(identity="ignored-and-never-returned")
    if accessed_at is not None:
        value.last_access_time.FromDatetime(accessed_at)
    return value


@dataclass
class FakeWorkflowService:
    pollers: list[object]
    requests: list[DescribeTaskQueueRequest] = field(default_factory=list)

    async def describe_task_queue(
        self,
        request: DescribeTaskQueueRequest,
        *,
        retry: bool,
        timeout: timedelta,
    ) -> DescribeTaskQueueResponse:
        assert retry is False
        assert timeout == timedelta(seconds=TEMPORAL_POLLER_RPC_TIMEOUT_SECONDS)
        self.requests.append(request)
        return cast(DescribeTaskQueueResponse, SimpleNamespace(pollers=self.pollers))


def fake_temporal_client(pollers: list[object]) -> Client:
    return cast(Client, SimpleNamespace(workflow_service=FakeWorkflowService(pollers)))


@pytest.mark.parametrize(
    ("accessed_at", "recent", "invalid"),
    [
        (NOW - timedelta(seconds=29), 1, 0),
        (NOW - timedelta(seconds=30), 1, 0),
        (NOW - timedelta(seconds=30, microseconds=1), 0, 0),
        (NOW + timedelta(microseconds=1), 0, 1),
        (None, 0, 1),
    ],
)
async def test_workflow_poller_recent_access_is_diagnostic_only(
    accessed_at: datetime | None, recent: int, invalid: int
) -> None:
    client = fake_temporal_client([poller(accessed_at)])
    diagnostics = await workflow_poller_diagnostics(
        client,
        namespace="default",
        queue="jhin-agent-queue",
        checked_at=NOW,
    )
    assert diagnostics == WorkflowPollerDiagnostics(
        retained=1,
        recently_accessed=recent,
        invalid_last_access_timestamps=invalid,
    )
```

The fake captures and asserts `DescribeTaskQueueRequest(namespace="default", task_queue.name="jhin-agent-queue", task_queue_type=WORKFLOW)`. Add this invalid-protobuf regression so both required exception classes execute:

```python
@dataclass
class InvalidTimestamp:
    error: Exception

    def ToDatetime(self, *, tzinfo: timezone) -> datetime:
        raise self.error


@dataclass
class InvalidPoller:
    last_access_time: InvalidTimestamp

    def HasField(self, field: str) -> bool:
        assert field == "last_access_time"
        return True


@pytest.mark.parametrize("error", [ValueError("bad timestamp"), OverflowError("bad timestamp")])
async def test_invalid_protobuf_timestamp_is_counted(error: Exception) -> None:
    diagnostics = await workflow_poller_diagnostics(
        fake_temporal_client([InvalidPoller(InvalidTimestamp(error))]),
        namespace="default",
        queue="jhin-tool-queue",
        checked_at=NOW,
    )
    assert diagnostics == WorkflowPollerDiagnostics(
        retained=1,
        recently_accessed=0,
        invalid_last_access_timestamps=1,
    )
```

Add a retained-old-poller test proving that advancing `checked_at` beyond 30 seconds changes only `recently_accessed` from one to zero: `retained` stays one and no liveness claim changes. Keep the prior standalone CLI as a queue-registration capability check: `queue_has_workflow_poller(...)` is true when `retained > 0`, independent of `last_access_time`. It must never be called by the API's live-worker readiness path.

In `test_health.py`, exercise the pure queue composer with the exact heartbeat/poller combinations before implementing it:

```python
def test_busy_worker_with_old_poller_access_is_live_from_heartbeat() -> None:
    row = temporal_queue_health(
        queue=AGENT_TASK_QUEUE,
        diagnostics=WorkflowPollerDiagnostics(
            retained=1, recently_accessed=0, invalid_last_access_timestamps=0
        ),
        owner_presence=ServiceHeartbeatPresence(fresh=1, fresh_degraded=0, retained=1),
        checked_at=NOW,
    )
    assert row.fresh_owner_instances == 1
    assert row.retained_pollers == 1
    assert row.recently_accessed_pollers == 0
    assert row.component.status == HealthStatus.OK


def test_retained_stale_poller_does_not_keep_dead_owner_live() -> None:
    row = temporal_queue_health(
        queue=AGENT_TASK_QUEUE,
        diagnostics=WorkflowPollerDiagnostics(
            retained=1, recently_accessed=0, invalid_last_access_timestamps=0
        ),
        owner_presence=ServiceHeartbeatPresence(fresh=0, fresh_degraded=0, retained=1),
        checked_at=NOW,
    )
    assert row.retained_pollers == 1
    assert row.fresh_owner_instances == 0
    assert row.component.reason_code == HealthReasonCode.WORKER_STALE


def test_invalid_poller_timestamp_degrades_metadata_not_worker_liveness() -> None:
    row = temporal_queue_health(
        queue=TOOL_TASK_QUEUE,
        diagnostics=WorkflowPollerDiagnostics(
            retained=1, recently_accessed=0, invalid_last_access_timestamps=1
        ),
        owner_presence=ServiceHeartbeatPresence(fresh=1, fresh_degraded=0, retained=1),
        checked_at=NOW,
    )
    assert row.fresh_owner_instances == 1
    assert row.component.reason_code == HealthReasonCode.POLLER_METADATA_INVALID


def test_killed_service_reaches_zero_only_when_heartbeat_expires() -> None:
    presence = classify_service_heartbeats(
        [heartbeat(last_seen_at=NOW - timedelta(seconds=30, microseconds=1))],
        checked_at=NOW,
    )
    row = temporal_queue_health(
        queue=TOOL_TASK_QUEUE,
        diagnostics=WorkflowPollerDiagnostics(
            retained=1, recently_accessed=1, invalid_last_access_timestamps=0
        ),
        owner_presence=presence,
        checked_at=NOW,
    )
    assert row.fresh_owner_instances == 0
    assert row.component.reason_code == HealthReasonCode.WORKER_STALE


def test_presence_counts_fresh_degraded_as_a_bounded_fresh_subset() -> None:
    presence = classify_service_heartbeats(
        [
            heartbeat(last_seen_at=NOW, readiness="ok"),
            heartbeat(last_seen_at=NOW, readiness="degraded"),
            heartbeat(
                last_seen_at=NOW - timedelta(seconds=30, microseconds=1),
                readiness="degraded",
            ),
        ],
        checked_at=NOW,
    )
    assert presence == ServiceHeartbeatPresence(
        fresh=2,
        fresh_degraded=1,
        retained=3,
    )
    with pytest.raises(ValueError):
        ServiceHeartbeatPresence(
            fresh=MAX_SAFE_COUNT + 1,
            fresh_degraded=0,
            retained=MAX_SAFE_COUNT,
        )
    with pytest.raises(ValueError):
        ServiceHeartbeatPresence(fresh=1, fresh_degraded=2, retained=2)


def healthy_worker_probe() -> WorkerHeartbeatProbe:
    return WorkerHeartbeatProbe(
        by_service={
            "api": ServiceHeartbeatPresence(
                fresh=0, fresh_degraded=0, retained=0
            ),
            "agent-worker": ServiceHeartbeatPresence(
                fresh=1, fresh_degraded=0, retained=1
            ),
            "tool-worker": ServiceHeartbeatPresence(
                fresh=1, fresh_degraded=0, retained=1
            ),
            "event-worker": ServiceHeartbeatPresence(
                fresh=1, fresh_degraded=0, retained=1
            ),
        },
        invalid_or_excess_rows=0,
    )


def test_public_worker_readiness_does_not_require_an_api_row() -> None:
    assert worker_heartbeats_ready(healthy_worker_probe()) is True


@pytest.mark.parametrize(
    "service", ["api", "agent-worker", "tool-worker", "event-worker"]
)
def test_public_worker_readiness_rejects_any_fresh_degraded_service(
    service: ServiceName,
) -> None:
    healthy = healthy_worker_probe()
    by_service = dict(healthy.by_service)
    by_service[service] = ServiceHeartbeatPresence(
        fresh=max(1, by_service[service].fresh),
        fresh_degraded=1,
        retained=max(1, by_service[service].retained),
    )
    assert worker_heartbeats_ready(
        WorkerHeartbeatProbe(by_service=by_service, invalid_or_excess_rows=0)
    ) is False


def test_fresh_degraded_owner_degrades_its_temporal_queue() -> None:
    row = temporal_queue_health(
        queue=TOOL_TASK_QUEUE,
        diagnostics=WorkflowPollerDiagnostics(
            retained=1, recently_accessed=1, invalid_last_access_timestamps=0
        ),
        owner_presence=ServiceHeartbeatPresence(
            fresh=1, fresh_degraded=1, retained=1
        ),
        checked_at=NOW,
    )
    assert row.component.reason_code == HealthReasonCode.WORKER_DEGRADED
```

Also assert a workflow-queue row has `fresh_owner_instances is None`: because workflow-worker remains credential-free, retained poller metadata proves queue registration/capability only and is never presented as hard liveness. For agent/tool queues, precedence is zero fresh owner heartbeat (`worker_missing`/`worker_stale`), then any fresh degraded owner (`worker_degraded`), then missing retained capability (`poller_missing`), then invalid poller timestamps (`poller_metadata_invalid`); old-but-valid `last_access_time` never degrades a fresh owner.

In `test_temporal_provider.py`, make concurrent business and health access share one connection:

```python
async def test_provider_connects_once_for_concurrent_callers(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    expected = cast(TemporalClient, object())

    async def connect(address: str, *, namespace: str) -> TemporalClient:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        assert (address, namespace) == (settings.temporal_address, settings.temporal_namespace)
        return expected

    monkeypatch.setattr(TemporalClient, "connect", connect)
    provider = TemporalClientProvider(settings)
    first, second = await asyncio.gather(provider.get(), provider.get())
    assert (first, second, calls) == (expected, expected, 1)


async def test_business_dependency_uses_lifespan_provider() -> None:
    expected = cast(TemporalClient, object())
    get = AsyncMock(return_value=expected)
    app = FastAPI()
    app.state.temporal_provider = SimpleNamespace(get=get)
    request = Request({"type": "http", "app": app})
    assert await get_temporal_client(request) is expected
    get.assert_awaited_once_with()
```

Import `Request` from `starlette.requests`, not from pytest and not as a test parameter. Also test that a failed first connect is not cached and a later call retries, and that app state has no independent `temporal_client` or `temporal_connect_lock` fields.

Add concrete probe tests in `test_health.py`: database reachability and `0015 == 0015`; mismatched/invalid/multiple revision becomes sanitized `schema_mismatch`; fresh/stale heartbeat classification is inclusive at exactly 30 seconds and retained for seven days; `fresh_degraded` is a bounded subset for each service; NATS reports exactly INGRESS/`event-worker-ingress` and EVENTS/`event-worker`; missing consumer, backlog, and redelivery select the closed reasons; Temporal calls the provider once and returns exactly the three queue names. Public readiness is ok with no API row plus fresh non-degraded agent/tool/event rows even when every valid retained poller has old `last_access_time`. It is degraded after any required service heartbeat passes the strict `>30 seconds` boundary, or when any fresh API/agent/tool/event row reports `degraded`, even if Temporal still returns that poller. Feed `-1`, `True`, a float, `MAX_SAFE_COUNT + 1`, an overlong revision, and exception canaries through fake dependency results; assert counts/latency are clamped or zeroed, invalid values degrade their component, strings are omitted rather than echoed, list cardinalities stay exactly 2/3, and `model_dump_json()` succeeds under `extra="forbid"`.

- [ ] **Step 2: Run RED and inspect the expected failures**

```bash
uv run pytest packages/events/tests/test_streams.py packages/workflows/tests/test_poller_health.py apps/api/tests/test_temporal_provider.py apps/api/tests/test_health.py -q
```

Expected: FAIL because the consumer constants, diagnostic-only `workflow_poller_diagnostics`, bounded `fresh_degraded` heartbeat predicate, heartbeat-based queue composer/provider, bounded schemas/probes, and opaque public response do not exist; the old readiness body still exposes dependency detail. Do not edit implementation until these failures have been observed.

- [ ] **Step 3: Implement canonical consumers and diagnostic-only Temporal inspection**

Set `INGRESS_CONSUMER = "event-worker-ingress"` and `EVENTS_CONSUMER = "event-worker"`; event-worker settings import these as defaults. Implement the raw service call once per queue:

```python
async def workflow_poller_diagnostics(
    client: Client,
    *,
    namespace: str,
    queue: str,
    checked_at: datetime,
) -> WorkflowPollerDiagnostics:
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=namespace,
            task_queue=TaskQueue(name=queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        ),
        retry=False,
        timeout=timedelta(seconds=TEMPORAL_POLLER_RPC_TIMEOUT_SECONDS),
    )
    cutoff = checked_at - timedelta(seconds=TEMPORAL_RECENT_ACCESS_DIAGNOSTIC_SECONDS)
    retained = 0
    recently_accessed = 0
    invalid = 0
    for info in response.pollers:
        retained += 1
        if not info.HasField("last_access_time"):
            invalid += 1
            continue
        try:
            accessed_at = info.last_access_time.ToDatetime(tzinfo=UTC)
        except (ValueError, OverflowError):
            invalid += 1
            continue
        if cutoff <= accessed_at <= checked_at:
            recently_accessed += 1
        elif accessed_at > checked_at:
            invalid += 1
    return WorkflowPollerDiagnostics(
        retained=retained,
        recently_accessed=recently_accessed,
        invalid_last_access_timestamps=invalid,
    )
```

Recent access is inclusive at exactly 30 seconds, but it is diagnostic only. Old valid timestamps reduce only `recently_accessed`; missing, future, or protobuf timestamps whose `ToDatetime()` raises `ValueError` or `OverflowError` increment `invalid_last_access_timestamps`. All returned pollers increment `retained`, and none of these fields is a worker lease or kill-to-zero timer. The standalone prior-plan CLI may make its own short-lived connection because it is a separate process, but it delegates to this function with `datetime.now(UTC)`, returns success from `retained > 0` as a queue-registration capability check, and never defines a second API connection path.

- [ ] **Step 4: Replace the API's parallel Temporal cache with one lifespan provider**

```python
class TemporalClientProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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
                )
            return self._client
```

Create `app.state.temporal_provider = TemporalClientProvider(settings)` once in the API lifespan before heartbeat/readiness work starts. Remove `app.state.temporal_client` and `temporal_connect_lock`. The pinned SDK exposes no public client close operation; on shutdown, cancel/await health and heartbeat tasks first, dispose other owned resources, and let the lifespan release the one provider/client reference. Change `get_temporal_client` to call the provider and translate only `RPCError`/`OSError` to the existing business 503. `probe_temporal` also calls this provider; it catches and sanitizes the error itself. Thus health and product requests can never create competing API clients.

- [ ] **Step 5: Implement strict bounded schemas and dependency probes**

Add `LivenessResponse`/`ReadinessReport` with `extra="forbid"`, the Shared Interfaces models, and one central conversion used before model construction:

```python
def bounded_count(value: object) -> tuple[int, bool]:
    if type(value) is not int or value < 0:  # reject bool and lossy float coercion
        return 0, True
    if value > MAX_SAFE_COUNT:
        return MAX_SAFE_COUNT, True
    return value, False
```

Use the equivalent closed conversion for latency (finite numeric, `0 <= value <= 60_000`) and full-match revision/service/connector strings before response construction. Never depend on permissive Pydantic coercion. Define the internal results exactly:

```python
@dataclass(frozen=True)
class DatabaseProbe:
    database_component: HealthComponent
    schema_component: HealthComponent
    current_revision: str | None
    packaged_head: str


@dataclass(frozen=True)
class NatsProbe:
    component: HealthComponent
    consumers: tuple[EventConsumerHealthSummary, EventConsumerHealthSummary]


@dataclass(frozen=True)
class TemporalProbe:
    component: HealthComponent
    queue_diagnostics: tuple[
        tuple[Literal["jhin-workflow-queue"], WorkflowPollerDiagnostics],
        tuple[Literal["jhin-agent-queue"], WorkflowPollerDiagnostics],
        tuple[Literal["jhin-tool-queue"], WorkflowPollerDiagnostics],
    ]


@dataclass(frozen=True)
class ServiceHeartbeatPresence:
    fresh: BoundedCount
    fresh_degraded: BoundedCount
    retained: BoundedCount

    def __post_init__(self) -> None:
        values = (self.fresh, self.fresh_degraded, self.retained)
        if any(
            type(value) is not int or not 0 <= value <= MAX_SAFE_COUNT
            for value in values
        ):
            raise ValueError("heartbeat presence counts must be bounded integers")
        if not self.fresh_degraded <= self.fresh <= self.retained:
            raise ValueError("heartbeat presence counts violate subset ordering")


def classify_service_heartbeats(
    rows: Iterable[ServiceInstanceHeartbeat],
    *,
    checked_at: datetime,
) -> ServiceHeartbeatPresence:
    cutoff = checked_at - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
    retained_rows = tuple(rows)
    fresh_rows = tuple(
        row for row in retained_rows if cutoff <= row.last_seen_at <= checked_at
    )
    fresh, _ = bounded_count(len(fresh_rows))
    fresh_degraded, _ = bounded_count(
        sum(row.readiness == "degraded" for row in fresh_rows)
    )
    retained, _ = bounded_count(len(retained_rows))
    return ServiceHeartbeatPresence(
        fresh=fresh,
        fresh_degraded=fresh_degraded,
        retained=retained,
    )


@dataclass(frozen=True)
class WorkerHeartbeatProbe:
    by_service: Mapping[ServiceName, ServiceHeartbeatPresence]
    invalid_or_excess_rows: BoundedCount


def worker_heartbeats_ready(probe: WorkerHeartbeatProbe) -> bool:
    presence = probe.by_service
    required_live = all(
        presence[service].fresh > 0
        for service in ("agent-worker", "tool-worker", "event-worker")
    )
    no_fresh_degraded = all(
        presence[service].fresh_degraded == 0
        for service in ("api", "agent-worker", "tool-worker", "event-worker")
    )
    return required_live and no_fresh_degraded and probe.invalid_or_excess_rows == 0


async def probe_worker_heartbeats(
    engine: AsyncEngine,
    *,
    checked_at: datetime,
) -> WorkerHeartbeatProbe: ...


def temporal_queue_health(
    *,
    queue: Literal["jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue"],
    diagnostics: WorkflowPollerDiagnostics,
    owner_presence: ServiceHeartbeatPresence | None,
    checked_at: datetime,
) -> TemporalQueueHealthSummary: ...


async def probe_temporal(
    provider: TemporalClientProvider,
    *,
    namespace: str,
    checked_at: datetime,
) -> TemporalProbe:
    async with asyncio.timeout(5.0):
        client = await provider.get()
        diagnostics = await asyncio.gather(*(
            workflow_poller_diagnostics(
                client, namespace=namespace, queue=queue, checked_at=checked_at
            )
            for queue in (WORKFLOW_TASK_QUEUE, AGENT_TASK_QUEUE, TOOL_TASK_QUEUE)
        ))
    return temporal_probe_from_diagnostics(diagnostics, checked_at=checked_at)
```

`probe_database` wraps its connection/query work in `asyncio.timeout(5.0)`, runs `SELECT 1` separately from the Alembic query, measures/clamps connectivity latency, and obtains exactly one current revision plus the packaged head. Only full matches of `[0-9A-Za-z_-]{1,64}` survive; absent, multiple, invalid, or non-`0015` current state is `degraded/schema_mismatch/run_migrations`, while connectivity failure is `down/database_unavailable/check_database`.

`probe_nats` uses the existing five-second outer bound and a three-second connect timeout, checks JetStream account info, and requests `(INGRESS, INGRESS_CONSUMER)` and `(EVENTS, EVENTS_CONSUMER)`. Invalid/excess counts are bounded and degrade the row; redelivery takes precedence over backlog, then missing consumer. NATS connectivity/RPC failure yields critical `down/nats_unavailable/check_nats` plus two zero-valued canonical rows. Always drain/close a probe-owned NATS connection in `finally`.

`probe_worker_heartbeats` reads only rows retained within seven days, validates every row before counting, and classifies `last_seen_at >= checked_at - 30 seconds` as fresh, inclusive. For each service, `fresh` counts all fresh rows, `fresh_degraded` counts the bounded subset whose exact persisted readiness is `degraded`, and `retained` counts all valid retained rows; construction enforces `0 <= fresh_degraded <= fresh <= retained <= MAX_SAFE_COUNT`. It returns exact entries for `api`, `agent-worker`, `tool-worker`, and `event-worker`; invalid/excess rows increment the bounded counter and cannot make a service fresh or healthy. A missing table caused by schema mismatch returns zero presence rather than leaking SQL detail. This read is the live-readiness authority for heartbeat-bearing services.

`temporal_queue_health` uses the fixed owner mapping `jhin-agent-queue -> agent-worker`, `jhin-tool-queue -> tool-worker`, and `jhin-workflow-queue -> None`. For agent/tool, `owner_presence.fresh == 0` selects `worker_missing` when no retained heartbeat exists and `worker_stale` otherwise, regardless of Temporal access timestamps; nonzero `owner_presence.fresh_degraded` next selects `worker_degraded`. Then zero `diagnostics.retained` selects `poller_missing`; then nonzero invalid timestamps selects `poller_metadata_invalid`; otherwise the row is ok. The workflow queue has `fresh_owner_instances=None` and evaluates only capability metadata because the workflow-worker remains credential-free. A valid old `last_access_time` never changes component status.

`probe_temporal` calls the shared provider once, concurrently inspects exactly the three canonical queues, and each raw inspection uses the fixed five-second no-retry RPC timeout. Temporal connectivity/RPC failure yields `down/temporal_unavailable/check_temporal` plus exact canonical zero diagnostics. A successful call leaves liveness composition to `temporal_queue_health`; retained metadata is only a bounded capability observation. Each exception path re-raises cancellation, otherwise logs only a stable event and closed reason code; never pass `str(exc)`, args, address, or traceback into a response/result object.

All four probe entry points re-raise `asyncio.CancelledError` but map every other exception to their closed unavailable/mismatch result. This guarantees a broken dependency adapter cannot turn anonymous readiness or protected health into an exception-text response; tests still assert the stable sanitized log event so failures remain diagnosable.

- [ ] **Step 6: Implement opaque readiness and routes**

```python
async def public_readiness(
    settings: Settings,
    engine: AsyncEngine,
    temporal_provider: TemporalClientProvider,
) -> ReadinessReport:
    checked_at = datetime.now(UTC)
    database, worker_heartbeats, nats_probe, temporal = await asyncio.gather(
        probe_database(engine, database_url=settings.database_url, checked_at=checked_at),
        probe_worker_heartbeats(engine, checked_at=checked_at),
        probe_nats(settings, checked_at=checked_at),
        probe_temporal(
            temporal_provider,
            namespace=settings.temporal_namespace,
            checked_at=checked_at,
        ),
    )
    presence = worker_heartbeats.by_service
    queues = (
        temporal_queue_health(
            queue=WORKFLOW_TASK_QUEUE,
            diagnostics=temporal.queue_diagnostics[0][1],
            owner_presence=None,
            checked_at=checked_at,
        ),
        temporal_queue_health(
            queue=AGENT_TASK_QUEUE,
            diagnostics=temporal.queue_diagnostics[1][1],
            owner_presence=presence["agent-worker"],
            checked_at=checked_at,
        ),
        temporal_queue_health(
            queue=TOOL_TASK_QUEUE,
            diagnostics=temporal.queue_diagnostics[2][1],
            owner_presence=presence["tool-worker"],
            checked_at=checked_at,
        ),
    )
    statuses = (
        database.database_component.status,
        database.schema_component.status,
        nats_probe.component.status,
        temporal.component.status,
        *(row.component.status for row in nats_probe.consumers),
        *(row.component.status for row in queues),
    )
    return ReadinessReport(
        status="ok"
        if worker_heartbeats_ready(worker_heartbeats)
        and all(value == HealthStatus.OK for value in statuses)
        else "degraded"
    )
```

The readiness route passes `request.app.state.temporal_provider`, returns only `ReadinessReport`, and chooses 200/503 from its aggregate. `worker_heartbeats_ready` requires fresh agent/tool/event database heartbeats and requires `fresh_degraded == 0` for API, agent, tool, and event; API does not require a row because the request itself proves one serving instance. It combines agent/tool queue capability metadata with those same owner counts; `recently_accessed_pollers` is never in a readiness predicate. Thus a fresh tool-worker heartbeat reporting `sandbox_unreachable` makes anonymous readiness 503 until a later fresh `ok` heartbeat replaces it. The liveness route returns only `LivenessResponse(app=settings.app_name, version=__version__, status="ok")`. Compose continues using liveness for the serving API so a deliberately stopped worker does not restart API.

- [ ] **Step 7: Run GREEN, affected regressions, and exact staging audit**

```bash
uv run pytest packages/events/tests/test_streams.py packages/workflows/tests/test_poller_health.py apps/api/tests/test_temporal_provider.py apps/api/tests/test_health.py -q
uv run pytest apps/api/tests/test_tasks_unit.py apps/api/tests/test_approvals_unit.py -q
uv run ruff check packages/events packages/workflows apps/api/src/jhin_api/temporal.py apps/api/src/jhin_api/deps.py apps/api/src/jhin_api/main.py apps/api/src/jhin_api/health apps/api/tests/test_temporal_provider.py apps/api/tests/test_health.py
uv run mypy packages/events/src packages/workflows/src apps/api/src
git add packages/events/src/jhin_events/streams.py packages/events/tests/test_streams.py services/event_worker/src/jhin_event_worker/settings.py packages/workflows/src/jhin_workflows/poller_health.py packages/workflows/tests/test_poller_health.py apps/api/src/jhin_api/temporal.py apps/api/src/jhin_api/deps.py apps/api/src/jhin_api/main.py apps/api/tests/test_temporal_provider.py apps/api/src/jhin_api/health/schemas.py apps/api/src/jhin_api/health/checks.py apps/api/src/jhin_api/health/service.py apps/api/src/jhin_api/health/router.py apps/api/tests/test_health.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: make public readiness opaque"
```

Expected before commit: the cached-name output is exactly the fourteen paths in the `git add` command.

### Task 4: Add the Workspace-Admin Protected Health Projection

**Files:**
- Modify: `apps/api/src/jhin_api/health/service.py`
- Modify: `apps/api/src/jhin_api/health/router.py`
- Create: `apps/api/tests/test_operations_health.py`
- Modify: `apps/api/tests/conftest.py`

**Interfaces:**
- Consumes: Tasks 1-3, `AdminCtx`, workspace-filtered `Connection`/`Secret`, `TemporalClientProvider`, diagnostic-only Temporal poller metadata, `ObservabilityRuntimeProtocol`, and the currently serving API identity/key-version metadata.
- Produces: `build_operations_health(...) -> OperationsHealthSnapshot` and the only protected health route.

The service signature is fixed:

```python
async def build_operations_health(
    db: AsyncSession,
    *,
    engine: AsyncEngine,
    settings: Settings,
    temporal_provider: TemporalClientProvider,
    workspace_id: UUID,
    current_api: CurrentApiInstance,
    telemetry_runtime: ObservabilityRuntimeProtocol,
    now: datetime | None = None,
) -> OperationsHealthSnapshot: ...
```

- [ ] **Step 1: Write all failing RBAC, projection, key-rollout, freshness, and bounds tests**

First add HTTP tests using real memberships and dependency overrides: anonymous 401; non-member 404; viewer/member 403; admin/owner 200. The 404/403 order must come from `AdminCtx`, not route logic. Seed a foreign workspace with unique connection/secret values and assert no foreign connector type, count, workspace ID, record ID, or canary appears at any depth.

In `apps/api/tests/conftest.py`, add an `OperationsHealthWorld` fixture owning the test session, engine, settings, workspace, fake Temporal provider, and fake telemetry runtime. Its `seed_heartbeat(...)` inserts/commits the exact Task 1 model, `snapshot(current_api, now)` calls the fixed `build_operations_health` signature below, and `key_snapshot(active, supported, secret_versions)` seeds one matching fresh persisted API replica plus agent/tool reporters and only workspace-owned `Secret` rows before delegating to `snapshot`; it never stubs worker/key/connector aggregation. Seed fresh/stale heartbeats, mock only the Task 3 live probes, and call the exact service signature:

```python
@dataclass(frozen=True)
class FakeObservabilityRuntime:
    config: ObservabilityConfig
    _status: TelemetryExporterStatus
    def status(self) -> TelemetryExporterStatus:
        return self._status


async def test_projection_contains_only_scoped_bounded_summaries(
    db: AsyncSession,
    engine: AsyncEngine,
    settings: Settings,
    workspace_a: Workspace,
    fake_temporal_provider: TemporalClientProvider,
) -> None:
    snapshot = await build_operations_health(
        db,
        engine=engine,
        settings=settings,
        temporal_provider=fake_temporal_provider,
        workspace_id=workspace_a.id,
        current_api=CurrentApiInstance(
            instance_id=CURRENT_API_ID,
            version="0.1.0",
            active_key_version=1,
            supported_key_versions=(1,),
        ),
        telemetry_runtime=FakeObservabilityRuntime(
            config=ObservabilityConfig(
                service_name="api",
                service_version="0.1.0",
                environment="test",
                metric_export_interval_millis=60_000,
            ),
            _status=TelemetryExporterStatus(
                configured=True,
                last_success_at=NOW - timedelta(seconds=20),
                dropped_items=3,
                last_error_code=None,
            ),
        ),
        now=NOW,
    )
    assert snapshot.status == "degraded"
    workers = {row.service: row for row in snapshot.workers}
    assert workers["agent-worker"].fresh_instances == 1
    assert workers["tool-worker"].stale_instances == 1
    assert snapshot.keyring.secret_rows_by_version == [
        KeyVersionCount(key_version=1, secret_count=2)
    ]
    assert {row.connector_type for row in snapshot.connectors} == {"github", "linear"}
    assert "foreign-connector" not in snapshot.model_dump_json()
    assert "FOREIGN_SECRET_CANARY" not in snapshot.model_dump_json()
```

Prove current-request injection independently of the heartbeat table:

```python
async def test_current_request_is_fresh_and_other_api_replicas_remain_stale(
    operations_world: OperationsHealthWorld,
) -> None:
    await operations_world.seed_heartbeat(
        instance_id=CURRENT_API_ID,
        service="api",
        version="0.1.0",
        last_seen_at=NOW - timedelta(hours=1),
    )
    await operations_world.seed_heartbeat(
        instance_id=OTHER_API_ID,
        service="api",
        version="0.1.0",
        last_seen_at=NOW - timedelta(seconds=31),
    )
    snapshot = await operations_world.snapshot(
        current_api=CurrentApiInstance(
            instance_id=CURRENT_API_ID,
            version="0.1.0",
            active_key_version=1,
            supported_key_versions=(1,),
        ),
        now=NOW,
    )
    api = next(row for row in snapshot.workers if row.service == "api")
    assert api.fresh_instances == 1
    assert api.stale_instances == 1
    assert api.versions == {"0.1.0": 2}
```

The same-ID persisted row is replaced by the request-time synthetic record rather than double-counted; no heartbeat row is required. A different API instance remains stale and visible.

Repeat the Task 3 liveness cases through `build_operations_health`, not only the pure composer. A fresh agent heartbeat plus a retained poller last accessed more than 30 seconds ago remains healthy (`fresh_owner_instances == 1`, `recently_accessed_pollers == 0`). A retained old poller plus only a stale agent heartbeat is `worker_stale`. A fresh degraded tool heartbeat keeps `fresh_owner_instances == 1` but makes both `worker.tool-worker` and `temporal.jhin-tool-queue` `worker_degraded`; a separate invalid protobuf timestamp case degrades queue metadata without changing that owner count. Moving a killed service heartbeat from exactly 30 seconds old to 30 seconds plus one microsecond changes its queue's `fresh_owner_instances` from one to zero even if the same retained Temporal record remains. Assert the credential-free workflow queue always emits `fresh_owner_instances is None` and never calls a heartbeat row a workflow-worker liveness signal.

Parameterize exact forward-compatible key rollout states. In each case seed one fresh API replica plus fresh agent/tool reporters with the same tuple as `current_api`, and assert each `KeyReporterSummary.distributions` contains the exact tuple and instance count:

```python
@pytest.mark.parametrize(
    ("active", "supported", "secret_versions"),
    [
        (1, (1, 2), [1, 1]),       # rollout stage 3: old writer, both readers
        (2, (1, 2), [1, 2]),       # rollout stage 4: new writer, mixed rows
        (2, (2,), [2, 2]),          # retirement: only the new reader remains
    ],
)
async def test_exact_key_distribution_rollout_states_are_healthy(
    operations_world: OperationsHealthWorld,
    active: int,
    supported: tuple[int, ...],
    secret_versions: list[int],
) -> None:
    snapshot = await operations_world.key_snapshot(
        active=active,
        supported=supported,
        secret_versions=secret_versions,
    )
    assert snapshot.keyring.component.status == HealthStatus.OK
    for reporter in snapshot.keyring.reporters:
        assert reporter.distributions == [
            KeyVersionDistribution(
                active_version=active,
                supported_versions=supported,
                instance_count=reporter.fresh_instances,
            )
        ]
```

Add separate degradation tests for: one fresh reporter with a different active version; one with a different supported tuple; missing metadata; active not in supported; non-integer/negative/duplicate/unsorted/33-item supported lists inserted directly; more than 32 distinct valid `(active_version, supported_versions)` tuples; a secret version unsupported by any fresh reporter; and an old-version secret remaining at retirement. Assert `distributions` retains at most the first 32 lexicographically sorted exact tuples, never a flattened union, and `invalid_or_excess_instances` counts invalid/overflow reporter instances while the key component degrades. A stale mismatching reporter is excluded from agreement but remains in the worker stale count. Assert secret distributions retain at most 32 sorted versions and put the bounded number of rows with invalid/excess versions in `invalid_or_excess_secret_rows`.

For every reporter assert the accounting invariant `fresh_instances == sum(distribution.instance_count) + missing_metadata + invalid_or_excess_instances` until `MAX_SAFE_COUNT` saturation; when saturation occurs all fields clamp deterministically and health degrades rather than wrapping or violating the schema.

For connectors, parameterize the inclusive freshness boundary: `last_verified_at == NOW - 300 seconds` with active/no error is healthy; 300 seconds plus one microsecond old, missing, naive/future, or otherwise invalid is unverified; `status == "error"` or any persisted `last_error` is unhealthy. Unknown/invalid/overlong connector strings map to `other`. More than 32 response groups collapse safely into the bounded `other` group, increment `invalid_or_excess_connections`, and degrade without echoing the raw value.

Finally inject negative, boolean, float, and `MAX_SAFE_COUNT + 1` values through fake counts/status, 21 service versions, 33 connector groups, 33 key versions/distributions, overlong/invalid strings, non-finite latency, and hostile dict/list extras. Assert every numeric value is within `0..MAX_SAFE_COUNT`, every string/list/dict obeys Shared Interfaces bounds, excess/invalid input increments a bounded counter and degrades, and serialization contains none of the hostile values. Test aggregation precedence: critical database/NATS/Temporal down > any product degradation > ok; telemetry failure alone leaves overall `ok`. Every component object has exactly `name,status,checked_at,latency_ms,reason_code,action` (with nullable fields present under the response model). Assert top-level names/order are exactly `api,database,schema,nats,temporal,sandbox,connectors,master-key,telemetry`; worker names are `worker.<closed service>`; consumers are `nats.ingress|nats.events`; queues are `temporal.<closed queue>`; connector rows use `connector.<validated type>`.

- [ ] **Step 2: Run RED and inspect the expected failures**

```bash
uv run pytest apps/api/tests/test_operations_health.py -q
```

Expected: FAIL because the AdminCtx route/projection do not exist, the service does not inject the current API request, connector verification has no 300-second rule, and key reporter output cannot express exact tuple distributions or invalid/excess counts. Do not implement until the complete test module fails for these reasons.

- [ ] **Step 3: Implement request-time worker, version, and sandbox aggregation**

Query retained heartbeats once with `last_seen_at >= now - 7 days`. Before grouping, remove a persisted API row whose `instance_id == current_api.instance_id`, then add one in-memory API record with `last_seen_at=now`, `readiness="ok"`, and the request's version/key metadata. This record is fresh even if the database write is missing or stale. Do not remove or refresh any other API row.

Classify persisted/injected records fresh at `last_seen_at >= now - 30 seconds`, inclusive. A service with zero fresh instances is `worker_missing` when no retained rows exist and `worker_stale` otherwise; any fresh degraded row is `worker_degraded`. Build the version map from all retained records (fresh plus stale) so stale replicas stay visible. Validate version strings with the exact shared regex, sort valid values, retain at most 20 dictionary keys, aggregate invalid/overflow values under only `other`, increment `invalid_or_excess_versions`, and degrade. Clamp every count with `bounded_count`.

Pass the same classified `ServiceHeartbeatPresence` values for agent-worker and tool-worker into Task 3's `temporal_queue_health`; do not recompute liveness from `PollerInfo.last_access_time`. The returned `retained_pollers`, `recently_accessed_pollers`, and `invalid_last_access_timestamps` are diagnostics. Agent/tool `fresh_owner_instances` must exactly equal the corresponding worker summary's fresh count. Workflow queue passes `owner_presence=None`, because its credential-free worker has no database heartbeat, and is labeled as capability-only. Event-worker liveness comes from its worker heartbeat row while NATS consumer metadata remains transport capability.

The sandbox component reads only fresh tool-worker rows: no fresh row is `unknown/worker_missing`; any missing/false/invalid reachability is `degraded/sandbox_unreachable`; all exact `True` is ok. API never joins `runner` or contacts sandbox-runner.

- [ ] **Step 4: Implement 300-second workspace connector aggregation and exact key tuples**

Filter connections by `Connection.workspace_id == workspace_id` and `status != "disabled"`. Build allowed connector types from `default_registry().types()`, sort them, admit at most 31 valid types plus literal `other`, and never return a persisted arbitrary type. At `checked_at = now`, classify:

```python
cutoff = checked_at - timedelta(seconds=CONNECTOR_VERIFICATION_FRESH_SECONDS)
verified_at_is_fresh = (
    type(verified_at) is datetime
    and verified_at.tzinfo is not None
    and verified_at.utcoffset() is not None
    and cutoff <= verified_at <= checked_at
)
healthy = status == "active" and last_error is None and verified_at_is_fresh
unhealthy = status == "error" or last_error is not None
unverified = not healthy and not unhealthy
```

Missing/stale verification is always unverified. Unhealthy takes precedence, then unverified. Invalid status/time/type or any capped value increments that group's `invalid_or_excess_connections`, makes its component non-ok, and makes the aggregate connector component degraded. Zero enabled rows emits an empty connector list and an ok aggregate.

For secrets, execute a workspace-filtered `SELECT Secret.key_version, count(*) GROUP BY Secret.key_version`; never select ciphertext, nonce, wrapped key, fingerprint, name, or ID. Validate count and key type without coercion. Retain at most 32 valid sorted versions and sum invalid/overflow row counts into bounded `invalid_or_excess_secret_rows`.

For each key-bearing service, group only fresh records by the exact immutable key `(active_key_version, tuple(supported_key_versions))`. A valid tuple has an integer active version in `1..2_147_483_647`, a sorted unique tuple of 1..32 integers in that range, and includes active. Keep at most 32 lexicographically sorted distributions and their instance counts; count missing fields in `missing_metadata`, invalid tuples or overflow-distribution instances in `invalid_or_excess_instances`. Never replace distributions with active/supported unions.

Build reporter counts from one partition of the fresh rows so valid distributions, missing metadata, and invalid/excess instances are mutually exclusive and exhaustive. Use saturating addition at `MAX_SAFE_COUNT`; any saturation degrades.

Use the validated current API tuple as the rollout expectation. Degrade key health if it is missing/invalid, if any fresh reporter metadata is missing/invalid/excess, if any fresh valid tuple differs from it, if a key-bearing service has no fresh reporter, if a secret version is outside any fresh reporter's supported tuple, or if any secret row/distribution was capped. Stage 3 `(1,(1,2))`, stage 4 `(2,(1,2))`, and retirement `(2,(2,))` therefore remain distinguishable and healthy only under their exact matching row-version conditions.

Select `key_metadata_missing` for absent/invalid/excess reporter metadata and `key_version_unsupported` for tuple disagreement, unsupported secret versions, or retirement violations; both use `review_key_rollout`. These closed reasons never include a version rendered as text outside the bounded integer fields.

- [ ] **Step 5: Implement telemetry/status composition and the AdminCtx route**

Call `telemetry_runtime.status()` once. Recent success means `last_success_at >= now - max(180 seconds, 3 * telemetry_runtime.config.metric_export_interval_millis / 1000)`. Unconfigured is `unknown/telemetry_not_configured/none`; configured with a current failure code is `degraded/telemetry_export_failed/check_telemetry_exporter`; configured without recent success is `degraded/telemetry_no_recent_success/check_telemetry_exporter`; otherwise ok. Pass `dropped_items` through the same strict `0..MAX_SAFE_COUNT` conversion; invalid/capped exporter values degrade telemetry but never product readiness. Do not return OTLP endpoints, TLS paths, queue capacities, or error text.

Return the sanitized current/packaged revisions from Task 3. Build top-level `components` in this exact order: `api`, `database`, `schema`, `nats`, `temporal`, `sandbox`, `connectors`, `master-key`, `telemetry`. The serving request supplies `api=ok`; telemetry is present but excluded from overall calculation. Overall is down only if database, NATS, or Temporal is down; otherwise it is degraded if any non-telemetry top-level/worker/consumer/queue/connector/key component is not ok; otherwise ok.

```python
@router.get(
    "/workspaces/{workspace_id}/operations/health",
    response_model=OperationsHealthSnapshot,
)
async def operations_health(
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> OperationsHealthSnapshot:
    identity: HeartbeatIdentity = request.app.state.api_heartbeat_identity
    crypto: SecretCrypto | None = request.app.state.secret_crypto
    current_api = CurrentApiInstance(
        instance_id=identity.instance_id,
        version=identity.version,
        active_key_version=None if crypto is None else crypto.active_key_version,
        supported_key_versions=() if crypto is None else crypto.supported_key_versions,
    )
    return await service.build_operations_health(
        db,
        engine=request.app.state.engine,
        settings=request.app.state.settings,
        temporal_provider=request.app.state.temporal_provider,
        workspace_id=ctx.workspace_id,
        current_api=current_api,
        telemetry_runtime=get_runtime(),
    )
```

The route body does no permission branching; `AdminCtx` owns 401/404/403. Assert the response has no `detail`, `exception`, `host`, `port`, `dsn`, `traceback`, connection ID, secret ID, user ID, or workspace ID key at any depth.

- [ ] **Step 6: Run GREEN, affected authorization regressions, and exact staging audit**

```bash
uv run pytest apps/api/tests/test_operations_health.py apps/api/tests/test_health.py -q
uv run pytest apps/api/tests/test_connections_unit.py apps/api/tests/test_policy_rbac.py -q
uv run ruff check apps/api/src/jhin_api/health apps/api/tests/test_operations_health.py apps/api/tests/conftest.py
uv run mypy apps/api/src/jhin_api/health apps/api/tests/test_operations_health.py
git add apps/api/src/jhin_api/health/service.py apps/api/src/jhin_api/health/router.py apps/api/tests/test_operations_health.py apps/api/tests/conftest.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: expose admin protected health"
```

Expected before commit: the cached-name output is exactly the four paths in the `git add` command.

### Task 5: Build the Admin-Only Operations Health UI and Opaque Overview Badge

**Files:**
- Modify: `apps/web/lib/types.ts`
- Modify: `apps/web/lib/hooks.ts`
- Modify: `apps/web/components/app-shell.tsx`
- Modify: `apps/web/app/(app)/page.tsx`
- Create: `apps/web/app/(app)/operations/page.tsx`
- Create: `apps/web/tests/operations-page.test.tsx`
- Create: `apps/web/tests/overview-health.test.tsx`
- Create: `apps/web/tests/operations-navigation.test.tsx`

**Interfaces:**
- Consumes: Task 4 JSON, `WorkspaceContext.can`, existing `Badge`, `Spinner`, `EmptyState`, and same-origin `api<T>()`.
- Produces: TypeScript mirrors, `useOperationsHealth(workspaceId, enabled)`, admin-only nav/page, health cards/tables, and an opaque overview badge.

- [ ] **Step 1: Write all failing type, hook, page, navigation, and overview tests first**

Render under `WorkspaceProvider` with an admin and a complete fixture. Assert headings `System health`, `Workers`, `Temporal pollers`, `Event consumers`, `Connector health`, `Master-key versions`, and `Telemetry`; exact current/packaged Alembic revisions, agent/tool/event freshness, all three exact queue names, INGRESS/EVENTS pending/redelivery, connector-type counts, version-only key data, last-check timestamps, and safe mapped action copy are visible.

```typescript
expect(screen.getByText("jhin-tool-queue")).toBeDefined();
expect(screen.getByText("tool-worker")).toBeDefined();
expect(screen.getByText("Check the sandbox runner and its internal network.")).toBeDefined();
expect(screen.queryByText("internal-secret-canary")).toBeNull();
```

Make the returned JSON fixture include hostile unknown top-level, component, connector, reporter, and distribution keys plus ID/DSN/traceback canaries; assert none renders. Render viewer/member direct access and assert `Admins only` and zero protected fetches. Add a fake-timer visibility test with an exact request count:

```typescript
expect(fetchMock).toHaveBeenCalledTimes(1);
await vi.advanceTimersByTimeAsync(OPERATIONS_POLL_MS);
expect(fetchMock).toHaveBeenCalledTimes(2);
setVisibility("hidden");
await vi.advanceTimersByTimeAsync(3 * OPERATIONS_POLL_MS);
expect(fetchMock).toHaveBeenCalledTimes(2);
setVisibility("visible");
await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
await vi.advanceTimersByTimeAsync(OPERATIONS_POLL_MS);
expect(fetchMock).toHaveBeenCalledTimes(4);
```

Navigation tests render owner/admin/member/viewer roles: only owner/admin see the Operations link. Exercise direct page access separately so nav hiding is never mistaken for authorization. Overview tests return exactly `{"status":"ok"}` or `{"status":"degraded"}`, assert the opaque badge, show `View Operations` only for admin/owner, and prove no protected request for member/viewer. Type compilation imports and constructs an exact `OperationsHealthSnapshot` fixture including `retained_pollers`, diagnostic `recently_accessed_pollers`/`invalid_last_access_timestamps`, nullable `fresh_owner_instances`, exact key `distributions`, all invalid/excess counters, and no optional undeclared fields.

- [ ] **Step 2: Run RED and inspect the expected failures**

```bash
pnpm --filter jhin-web test -- operations-page.test.tsx overview-health.test.tsx operations-navigation.test.tsx
pnpm --filter jhin-web typecheck
```

Expected: FAIL because the types/hook/page do not exist, the current nav has no role metadata/filter, and Overview still expects the legacy readiness detail. Do not change implementation until these failures are observed.

- [ ] **Step 3: Add exact TypeScript mirrors and visibility-aware polling**

Mirror every Shared Interfaces field and union; the key and connector portions must be structurally exact:

```typescript
export type HealthStatus = "ok" | "degraded" | "down" | "unknown";
export type HealthReasonCode =
  | "database_unavailable"
  | "schema_mismatch"
  | "nats_unavailable"
  | "consumer_missing"
  | "consumer_backlog"
  | "consumer_redelivery"
  | "temporal_unavailable"
  | "poller_missing"
  | "poller_metadata_invalid"
  | "worker_missing"
  | "worker_stale"
  | "worker_degraded"
  | "sandbox_unreachable"
  | "connection_unverified"
  | "connection_unhealthy"
  | "key_metadata_missing"
  | "key_version_unsupported"
  | "telemetry_not_configured"
  | "telemetry_no_recent_success"
  | "telemetry_export_failed";
export type HealthAction =
  | "none"
  | "check_database"
  | "run_migrations"
  | "check_nats"
  | "inspect_consumer"
  | "check_temporal"
  | "restart_worker"
  | "check_sandbox_runner"
  | "verify_connections"
  | "review_key_rollout"
  | "check_telemetry_exporter";

export interface HealthComponent {
  name: string;
  status: HealthStatus;
  checked_at: string;
  latency_ms: number | null;
  reason_code: HealthReasonCode | null;
  action: HealthAction;
}
export interface KeyVersionDistribution {
  active_version: number;
  supported_versions: number[];
  instance_count: number;
}
export interface KeyReporterSummary {
  service: "api" | "agent-worker" | "tool-worker";
  fresh_instances: number;
  distributions: KeyVersionDistribution[];
  missing_metadata: number;
  invalid_or_excess_instances: number;
}
export interface ConnectorHealthSummary {
  connector_type: string;
  enabled: number;
  healthy: number;
  unhealthy: number;
  unverified: number;
  invalid_or_excess_connections: number;
  component: HealthComponent;
}
export interface SchemaHealthSummary {
  current_revision: string | null;
  packaged_head: string;
  component: HealthComponent;
}
export interface WorkerHealthSummary {
  service: "api" | "agent-worker" | "tool-worker" | "event-worker";
  fresh_instances: number;
  stale_instances: number;
  versions: Record<string, number>;
  invalid_or_excess_versions: number;
  component: HealthComponent;
}
export interface EventConsumerHealthSummary {
  stream: "INGRESS" | "EVENTS";
  consumer: "event-worker-ingress" | "event-worker";
  pending: number;
  redelivered: number;
  component: HealthComponent;
}
export interface TemporalQueueHealthSummary {
  queue: "jhin-workflow-queue" | "jhin-agent-queue" | "jhin-tool-queue";
  retained_pollers: number;
  recently_accessed_pollers: number;
  invalid_last_access_timestamps: number;
  fresh_owner_instances: number | null;
  component: HealthComponent;
}
export interface KeyVersionCount {
  key_version: number;
  secret_count: number;
}
export interface KeyHealthSummary {
  active_version: number | null;
  secret_rows_by_version: KeyVersionCount[];
  invalid_or_excess_secret_rows: number;
  reporters: KeyReporterSummary[];
  component: HealthComponent;
}
export interface TelemetryHealthSummary {
  configured: boolean;
  recent_success: boolean | null;
  last_success_at: string | null;
  dropped_items: number;
  component: HealthComponent;
}
export interface OperationsHealthSnapshot {
  status: "ok" | "degraded" | "down";
  checked_at: string;
  components: HealthComponent[];
  schema: SchemaHealthSummary;
  workers: WorkerHealthSummary[];
  event_consumers: EventConsumerHealthSummary[];
  temporal_queues: TemporalQueueHealthSummary[];
  connectors: ConnectorHealthSummary[];
  keyring: KeyHealthSummary;
  telemetry: TelemetryHealthSummary;
}
```

Keep these definitions one-for-one with Shared Interfaces. `Record<string, number>` is permitted only for the bounded server-owned `versions` map; do not use `any`, index signatures elsewhere, or a catch-all display-field record.

Implement document visibility as hook state, not a one-time read:

```typescript
export const OPERATIONS_POLL_MS = 10_000;

function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState === "visible",
  );
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

export function useOperationsHealth(workspaceId: string, enabled: boolean) {
  const visible = useDocumentVisible();
  const shouldFetch = enabled && visible;
  return useQuery({
    queryKey: ["operations-health", workspaceId],
    queryFn: () => api<OperationsHealthSnapshot>(
      `/api/v1/workspaces/${workspaceId}/operations/health`,
    ),
    enabled: shouldFetch,
    refetchInterval: shouldFetch ? OPERATIONS_POLL_MS : false,
    refetchIntervalInBackground: false,
    retry: false,
  });
}
```

The browser endpoint is same-origin API only; no hook or page URL references Prometheus, Tempo, Grafana, Docker, NATS, or Temporal.

- [ ] **Step 4: Implement explicit role-filtered navigation and allowlisted Operations presentation**

Change the inferred nav array to the explicit interface:

```typescript
interface NavItem {
  href: string;
  label: string;
  icon: typeof ClipboardList;
  minimumRole?: WorkspaceRole;
}

const NAV: readonly NavItem[] = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/organization", label: "Organization", icon: Building2 },
  { href: "/tasks", label: "Tasks", icon: ListTodo },
  { href: "/runs", label: "Runs", icon: Activity },
  { href: "/approvals", label: "Approvals", icon: CheckSquare },
  { href: "/connectors", label: "Connectors", icon: Plug },
  { href: "/triggers", label: "Triggers", icon: Zap },
  { href: "/models", label: "Models", icon: Cpu },
  { href: "/audit", label: "Audit", icon: ScrollText },
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/operations", label: "Operations", icon: HeartPulse, minimumRole: "admin" },
];

const visibleItems = NAV.filter(
  (item) => item.minimumRole === undefined || can(item.minimumRole),
);
```

`SidebarNav` obtains both `workspace` and `can` from `useWorkspace()` and maps `visibleItems`. The direct Operations page always calls `useOperationsHealth(workspace.workspace_id, can("admin"))`; unauthorized roles render `EmptyState` with `Admins only` and therefore make no request.

Map closed action codes locally; never render backend-provided arbitrary text:

```typescript
const ACTION_COPY: Record<HealthAction, string> = {
  none: "No action required.",
  check_database: "Check PostgreSQL availability and credentials.",
  run_migrations: "Run the packaged database migration procedure.",
  check_nats: "Check NATS JetStream availability.",
  inspect_consumer: "Inspect the named durable consumer and event worker.",
  check_temporal: "Check Temporal availability and namespace configuration.",
  restart_worker: "Restart the missing worker after checking its sanitized logs.",
  check_sandbox_runner: "Check the sandbox runner and its internal network.",
  verify_connections: "Verify or repair the affected connector type.",
  review_key_rollout: "Review key-version rollout state before changing any key file.",
  check_telemetry_exporter: "Check the optional telemetry exporter; product work can continue.",
};
```

Status tones are exact: ok green, degraded amber, down red, unknown neutral. Render only explicit component fields and typed summary fields. Show schema revisions; fresh/stale heartbeat-bearing worker counts; Temporal retained capability, recent-access diagnostic, invalid-timestamp, and fresh-owner fields (with workflow queue labeled `capability only`); consumer pending/redelivery; connector healthy/unhealthy/unverified and invalid/excess counts; each exact `(active_version, supported_versions, instance_count)` key distribution; secret version counts; telemetry state; safe actions; and timestamps. Never label `recently_accessed_pollers` as worker liveness. Never recursively render objects, use `Object.entries` on unknown response data, show raw IDs, or expose expandable tracebacks. The master-key runbook link appears only when subproject 5 creates its documented target; until then render safe action copy without a broken URL.

- [ ] **Step 5: Make Overview consume only anonymous opacity**

Change its local readiness type to `{status: "ok" | "degraded"}` and remove dependency names/latencies entirely. Keep the `stack ok|degraded` badge. Admin/owner sees `View Operations` linking to `/operations`; viewer/member sees no link and receives no protected query.

- [ ] **Step 6: Run GREEN, full web regression, and exact staging audit**

```bash
pnpm --filter jhin-web test -- operations-page.test.tsx overview-health.test.tsx operations-navigation.test.tsx
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
git add apps/web/lib/types.ts apps/web/lib/hooks.ts apps/web/components/app-shell.tsx 'apps/web/app/(app)/page.tsx' 'apps/web/app/(app)/operations/page.tsx' apps/web/tests/operations-page.test.tsx apps/web/tests/overview-health.test.tsx apps/web/tests/operations-navigation.test.tsx
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add protected operations health view"
```

Expected before commit: the cached-name output is exactly the eight paths in the `git add` command.

### Task 6: Prove Opacity, Workspace Isolation, and Worker/Queue Recovery in Compose

**Files:**
- Create: `tests/integration/test_phase10_protected_health.py`
- Create: `tests/test_phase10_protected_health_harness.py`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/test_stack_health.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: complete Tasks 1-5 plus prior-plan `compose.rootful.yaml`, `compose.rootless.yaml`, actual Docker socket modes, and tool/telemetry topology.
- Produces: `phase10_compose_files(...)`, an executable Make/live-harness contract, and live evidence for anonymous opacity, workspace isolation, dependency failure/recovery, heartbeat freshness, diagnostic Temporal capability, consumer visibility, sandbox reporting, and agent/tool/event kill/recovery in both supported socket modes.

- [ ] **Step 1: Write the failing live acceptance and harness-contract tests first**

Add strict public equality and authenticated role cases. Create owner/admin/member/viewer/nonmember sessions for two workspaces; assert protected health status 200/200/403/403/404. Seed unique foreign connection/secret canaries and assert no body contains them, raw infrastructure coordinates, or record UUIDs. Assert canonical four services, two stream/consumer pairs, and three queues; all count fields satisfy `type(value) is int` and `0 <= value <= MAX_SAFE_COUNT`; all list/dict/string cardinalities remain within Shared Interfaces.

Use one bounded convergence helper whose failure output is already-sanitized JSON only:

```python
async def wait_health(
    client: httpx.AsyncClient,
    workspace_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 65.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/workspaces/{workspace_id}/operations/health")
        assert response.status_code == 200, response.text
        last = response.json()
        if predicate(last):
            return last
        await asyncio.sleep(1.0)
    pytest.fail(f"protected health did not converge; sanitized snapshot={last}")


async def wait_public_readiness(
    client: httpx.AsyncClient,
    *,
    status_code: Literal[200, 503],
    status: Literal["ok", "degraded"],
    timeout: float = 30.0,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    last_code = 0
    last_body: dict[str, str] = {}
    while time.monotonic() < deadline:
        response = await client.get("/api/v1/health/ready")
        last_code = response.status_code
        last_body = response.json()
        if last_code == status_code and last_body == {"status": status}:
            return last_body
        await asyncio.sleep(1.0)
    pytest.fail(
        f"opaque readiness did not converge; code={last_code}, body={last_body}"
    )
```

Diagnostics may append `docker compose ps --format json`, but never `docker inspect`, environments, secret mounts, database URLs, or raw service logs.

Test every failure in `try/finally` and require return to the prior healthy state:

```python
try:
    compose("kill", "-s", "SIGKILL", "agent-worker")
    await wait_health(client, workspace_id, lambda h: (
        worker(h, "agent-worker")["fresh_instances"] == 0
        and queue(h, "jhin-agent-queue")["fresh_owner_instances"] == 0
        and queue(h, "jhin-agent-queue")["retained_pollers"] >= 1
    ), timeout=50)
finally:
    compose("up", "-d", "agent-worker")
await wait_health(client, workspace_id, lambda h: (
    worker(h, "agent-worker")["fresh_instances"] >= 1
    and queue(h, "jhin-agent-queue")["fresh_owner_instances"] >= 1
    and queue(h, "jhin-agent-queue")["retained_pollers"] >= 1
), timeout=65)
```

Repeat independently for tool-worker/`jhin-tool-queue`; its recovery must restore a fresh `sandbox_reachable=True` report. In both kill cases, explicitly prove a retained Temporal poller can remain while the fresh owner count reaches zero; this is the live regression against using `last_access_time` as liveness. Kill/restart event-worker and require its fresh count to reach zero only after the strict `>30 seconds` heartbeat boundary, then recover; INGRESS/EVENTS rows remain canonical and drain to their prior counts. The general workflow queue retains capability metadata through each worker-only case and continues to expose `fresh_owner_instances: null`, never a liveness claim.

Add dependency/reporter recovery cases:

- Stop sandbox-runner with this dedicated anonymous-readiness recovery test. Within one 10-second tool probe cycle the fresh tool heartbeat becomes degraded and sandbox is `sandbox_unreachable`; anonymous readiness must become exact 503 and return to exact 200 only after the runner and a fresh non-degraded tool heartbeat recover:

```python
async def test_sandbox_failure_degrades_and_recovers_anonymous_readiness(
    anonymous_client: httpx.AsyncClient,
    admin_client: httpx.AsyncClient,
    workspace_id: str,
) -> None:
    assert await wait_public_readiness(
        anonymous_client, status_code=200, status="ok"
    ) == {"status": "ok"}
    try:
        compose("stop", "sandbox-runner")
        assert await wait_public_readiness(
            anonymous_client,
            status_code=503,
            status="degraded",
            timeout=25.0,
        ) == {"status": "degraded"}
        liveness = await anonymous_client.get("/api/v1/health")
        assert liveness.status_code == 200
        assert liveness.json() == {"app": "Jhin", "version": "0.1.0", "status": "ok"}
        await wait_health(
            admin_client,
            workspace_id,
            lambda health: (
                component(health, "sandbox")["reason_code"]
                == "sandbox_unreachable"
                and worker(health, "tool-worker")["component"]["reason_code"]
                == "worker_degraded"
            ),
            timeout=25.0,
        )
    finally:
        compose("up", "-d", "--wait", "sandbox-runner")

    assert await wait_public_readiness(
        anonymous_client, status_code=200, status="ok", timeout=30.0
    ) == {"status": "ok"}
    await wait_health(
        admin_client,
        workspace_id,
        lambda health: (
            component(health, "sandbox")["status"] == "ok"
            and worker(health, "tool-worker")["component"]["status"] == "ok"
        ),
        timeout=30.0,
    )
```

- Kill NATS; liveness remains exact/ok, public readiness becomes exact/503 degraded, protected NATS becomes down without exception detail; restart and wait for NATS plus both consumers to recover.
- Kill Temporal; liveness remains exact/ok, readiness becomes exact/503 degraded, protected Temporal becomes down; restart and wait for all three queue descriptions/retained capability rows to recover. Agent/tool live readiness continues to come from fresh heartbeats. The provider is reused and SDK reconnect behavior is observed; the test must not restart API to manufacture recovery.
- Stop PostgreSQL only after retaining an anonymous client; liveness stays exact/ok and readiness becomes exact/503 degraded. Restart PostgreSQL and require readiness recovery. The authenticated protected route is not expected to authorize while its product authority is unavailable.
- Make one owned workspace connection verification older than 300 seconds, observe unverified/degraded, update only `last_verified_at` and clear the error, then observe healthy. Use `finally` to restore its prior values.
- Pause the chosen reporter container with `docker compose pause`, update its still-fresh row in a direct test transaction to a mismatching exact tuple, observe key degradation/distribution, restore the prior tuple, and unpause in `finally`; then observe recovery. Finish this case within 30 seconds so it tests tuple agreement rather than worker staleness.

`test_stack_health.py` changes legacy dependency-map assertions to exact opaque public equality, requires real tool-worker, and asserts application healthchecks use only opaque HTTP endpoints or process-local commands. No browser route directly addresses infrastructure.

In the same test-first step, create `tests/test_phase10_protected_health_harness.py` with an executable contract for the helper and both live Make recipes:

```python
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.integration.conftest import phase10_compose_files

ROOT = Path(__file__).resolve().parents[1]
ROOTFUL_FILES = "compose.yaml:compose.dev.yaml:compose.rootful.yaml"
ROOTLESS_FILES = "compose.yaml:compose.dev.yaml:compose.rootless.yaml"


def recipe(name: str) -> str:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{re.escape(name)}:[^\n]*\n((?:\t.*\n)+)", text)
    assert match is not None, f"missing Make target: {name}"
    return match.group(1)


def test_compose_files_are_explicit_and_mode_bounded() -> None:
    assert phase10_compose_files(
        {"PHASE10_COMPOSE_FILES": ROOTFUL_FILES}
    ) == ("compose.yaml", "compose.dev.yaml", "compose.rootful.yaml")
    assert phase10_compose_files(
        {"PHASE10_COMPOSE_FILES": ROOTLESS_FILES}
    ) == ("compose.yaml", "compose.dev.yaml", "compose.rootless.yaml")
    with pytest.raises(ValueError):
        phase10_compose_files({"PHASE10_COMPOSE_FILES": "compose.yaml"})
    with pytest.raises(ValueError):
        phase10_compose_files({"PHASE10_COMPOSE_FILES": f"{ROOTFUL_FILES}:"})


def test_live_targets_start_the_exact_mode_before_the_socket_probe() -> None:
    rootful = recipe("test-protected-health-integration-rootful")
    rootless = recipe("test-protected-health-integration-rootless")
    assert rootful.index(" up -d --build --wait") < rootful.index(
        "$(MAKE) test-sandbox-socket-rootful"
    )
    assert rootless.index(" up -d --build --wait") < rootless.index(
        "$(MAKE) test-sandbox-socket-rootless"
    )
    assert (
        "docker compose -f compose.yaml -f compose.dev.yaml "
        "-f compose.rootful.yaml up -d --build --wait"
    ) in rootful
    assert (
        "docker compose -f compose.yaml -f compose.dev.yaml "
        "-f compose.rootless.yaml up -d --build --wait"
    ) in rootless
    rootful_env = (
        'SANDBOX_DOCKER_SOCKET_HOST="$$socket" SANDBOX_DOCKER_GID="$$gid" '
        'PHASE10_COMPOSE_FILES="$$files"'
    )
    rootless_env = (
        'env -u SANDBOX_DOCKER_GID SANDBOX_DOCKER_SOCKET_HOST="$$socket" '
        'PHASE10_ROOTLESS_DOCKER_SOCKET="$$socket" '
        'PHASE10_COMPOSE_FILES="$$files"'
    )
    assert rootful.count(rootful_env) == 3  # up, nested probe, pytest
    assert rootless.count(rootless_env) == 3
    assert "SANDBOX_DOCKER_GID=10001" not in rootful
```

- [ ] **Step 2: Run the executable RED harness contract before adding helper/Make support**

```bash
uv run pytest tests/test_phase10_protected_health_harness.py -q
```

Expected: FAIL during ordinary pytest execution because `phase10_compose_files` and/or the two protected-health live targets do not exist. Do not implement the helper or Make recipes before observing it. The live acceptance module is also already present from Step 1, but is run only after the correct-mode stack is started in Step 4.

- [ ] **Step 3: Implement mode-aware helpers and focused Make targets**

Extend `tests/integration/conftest.py` with the exact helper under test, and make `compose(...)` build its `-f` arguments from it:

```python
_PHASE10_COMPOSE_FILE_SETS = {
    ("compose.yaml", "compose.dev.yaml"),
    ("compose.yaml", "compose.dev.yaml", "compose.rootful.yaml"),
    ("compose.yaml", "compose.dev.yaml", "compose.rootless.yaml"),
}


def phase10_compose_files(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    source = os.environ if environ is None else environ
    raw = source.get("PHASE10_COMPOSE_FILES")
    files = (
        ("compose.yaml", "compose.dev.yaml")
        if raw is None
        else tuple(raw.split(":"))
    )
    if files not in _PHASE10_COMPOSE_FILE_SETS:
        raise ValueError("PHASE10_COMPOSE_FILES must select one supported exact mode")
    return files


def compose(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    project = validate_compose_project(os.environ.get("JHIN_TEST_COMPOSE_PROJECT", "jhin"))
    file_args = [item for path in phase10_compose_files() for item in ("-f", path)]
    return subprocess.run(
        ["docker", "compose", "-p", project, *file_args, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
```

Import `Mapping` from `collections.abc`. Remove the fixed module-level `COMPOSE` list so there is one mode source. Add a context fixture around ordinary `docker compose pause/unpause` for the reporter mismatch test. Every teardown restarts or unpauses changed services and waits for health; production Compose receives no test-only heartbeat control.

Add render-only and live targets. The literal `SANDBOX_DOCKER_GID=10001` is permitted only to render the rootful model; no live target uses it:

Append `test-protected-health`, `test-protected-health-render`, `test-protected-health-integration-rootful`, and `test-protected-health-integration-rootless` to `.PHONY` before adding these recipes.

```make
test-protected-health:
	uv run pytest packages/db/tests/test_service_instance_heartbeat.py packages/db/tests/test_heartbeat.py apps/api/tests/test_health.py apps/api/tests/test_operations_health.py tests/test_phase10_protected_health_harness.py -q
	pnpm --filter jhin-web test -- operations-page.test.tsx overview-health.test.tsx operations-navigation.test.tsx

test-protected-health-render:
	SANDBOX_DOCKER_GID=10001 docker compose -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml config --quiet
	env -u SANDBOX_DOCKER_GID docker compose -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml config --quiet

test-protected-health-integration-rootful:
	@socket="$${SANDBOX_DOCKER_SOCKET_HOST:-/var/run/docker.sock}"; \
	gid="$$(uv run python -c 'import os,stat,sys; value=os.stat(sys.argv[1]); assert stat.S_ISSOCK(value.st_mode), "not a socket"; assert value.st_gid > 0, "rootful socket group must be nonzero"; print(value.st_gid)' "$$socket")"; \
	files="compose.yaml:compose.dev.yaml:compose.rootful.yaml"; \
	SANDBOX_DOCKER_SOCKET_HOST="$$socket" SANDBOX_DOCKER_GID="$$gid" PHASE10_COMPOSE_FILES="$$files" docker compose -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml up -d --build --wait; \
	SANDBOX_DOCKER_SOCKET_HOST="$$socket" SANDBOX_DOCKER_GID="$$gid" PHASE10_COMPOSE_FILES="$$files" $(MAKE) test-sandbox-socket-rootful; \
	SANDBOX_DOCKER_SOCKET_HOST="$$socket" SANDBOX_DOCKER_GID="$$gid" PHASE10_COMPOSE_FILES="$$files" uv run pytest -m integration tests/integration/test_phase10_protected_health_migration.py tests/integration/test_phase10_protected_health.py tests/integration/test_stack_health.py -v

test-protected-health-integration-rootless:
	@socket="$${PHASE10_ROOTLESS_DOCKER_SOCKET:-}"; \
	test -n "$$socket" || (echo "PHASE10_ROOTLESS_DOCKER_SOCKET is required" >&2; exit 2); \
	uv run python -c 'import os,stat,sys; value=os.stat(sys.argv[1]); assert stat.S_ISSOCK(value.st_mode), "not a socket"; assert value.st_uid == 10001, "rootless socket must be owned by UID 10001"' "$$socket"; \
	files="compose.yaml:compose.dev.yaml:compose.rootless.yaml"; \
	env -u SANDBOX_DOCKER_GID SANDBOX_DOCKER_SOCKET_HOST="$$socket" PHASE10_ROOTLESS_DOCKER_SOCKET="$$socket" PHASE10_COMPOSE_FILES="$$files" docker compose -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml up -d --build --wait; \
	env -u SANDBOX_DOCKER_GID SANDBOX_DOCKER_SOCKET_HOST="$$socket" PHASE10_ROOTLESS_DOCKER_SOCKET="$$socket" PHASE10_COMPOSE_FILES="$$files" $(MAKE) test-sandbox-socket-rootless; \
	env -u SANDBOX_DOCKER_GID SANDBOX_DOCKER_SOCKET_HOST="$$socket" PHASE10_ROOTLESS_DOCKER_SOCKET="$$socket" PHASE10_COMPOSE_FILES="$$files" uv run pytest -m integration tests/integration/test_phase10_protected_health_migration.py tests/integration/test_phase10_protected_health.py tests/integration/test_stack_health.py -v
```

Both live targets validate only the mode inputs needed to start, run the correct overlay's `up -d --build --wait`, and only then invoke the prior plan's corresponding live socket connection/job probe. Rootful must discover the exact mounted Unix-socket GID with `os.stat` before Compose can render `group_add`, reject group 0, and pass the identical `PHASE10_COMPOSE_FILES`, socket, and actual GID environment to `up`, the nested probe, and pytest; it never chmods/chowns. Rootless requires an already-running socket owned by UID 10001, passes the identical compose-file/socket environment to all three commands, explicitly unsets `SANDBOX_DOCKER_GID` for all three, uses `compose.rootless.yaml`, and never infers or changes ownership. The pre-up `stat` calls validate mode inputs; the nested connection/job probe is the live socket probe and is ordered after `up --wait`.

- [ ] **Step 4: Run focused/render tests, then each available live mode**

```bash
uv run pytest tests/test_phase10_protected_health_harness.py -q
make test-protected-health
make test-protected-health-render
make test-protected-health-integration-rootful
PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock make test-protected-health-integration-rootless
```

Run the rootful target only on a host with a nonzero-group rootful socket and the rootless target only on a host with the required UID-10001 daemon. CI may schedule them on separate compatible hosts; neither mode is silently skipped or emulated by the other. Expected: public health remains opaque, authorization/isolation passes, each induced failure is observed after its exact freshness bound, and every component returns to its prior healthy state.

- [ ] **Step 5: Commit integration evidence with an exact staging audit**

```bash
git add tests/integration/test_phase10_protected_health.py tests/test_phase10_protected_health_harness.py tests/integration/conftest.py tests/integration/test_stack_health.py Makefile
git diff --cached --name-only
git diff --cached --check
git commit -m "test: verify protected health recovery"
```

Expected before commit: the cached-name output is exactly the five paths in the `git add` command.

### Task 7: Document the Safe Health Contract and Run the Complete Gate

**Files:**
- Create: `docs/operations/protected-health.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: implemented endpoints, roles, staleness, status/action enums, queues, consumers, and recovery tests.
- Produces: operator guidance and final verified/staged subproject evidence.

- [ ] **Step 1: Write the operator contract**

Document exact curl shapes (without credentials), monotonic start-to-start 10-second heartbeat cadence, inclusive 30-second heartbeat freshness and seven-day retention, current-request API injection plus stale-replica visibility, API/agent/tool/event ownership, and workflow-worker's credential-free capability-only visibility. State the opaque public predicate exactly: agent/tool/event each need a fresh heartbeat; API need not have a row; and every fresh API/agent/tool/event row must report non-degraded readiness. Include sandbox-runner failure as the concrete 503-to-200 recovery example. Explain that Temporal retains `PollerInfo` records, `last_access_time` is a bounded recent-access diagnostic only, and neither retained nor recent pollers grant worker liveness. Document the three task queues, agent/tool heartbeat-owner mapping, the two durable consumers, overall status precedence, safe action-code meanings, 300-second connection-verification freshness, workspace-only secret counts, exact active/supported key tuple distributions, invalid/excess bounds, and telemetry's exclusion from readiness. State explicitly that heartbeats do not grant authority, public readiness never exposes dependency detail, and browser code never contacts infrastructure.

Explain recovery: wait past the strict 30-second heartbeat boundary after a hard heartbeat-bearing worker loss, inspect the sanitized component/action, repair the service, and verify its fresh instance plus queue/consumer capability recovery. Warn that a retained Temporal poller can outlive a killed worker and is not recovery evidence by itself. Point master-key operators to the Phase 10 rotation runbook once subproject 5 lands; do not invent a broken link before that file exists. Explain that event-failure and task-retry panels arrive with subproject 4.

- [ ] **Step 2: Update README without widening access**

Add Operations to the admin surface, document `/api/v1/health` and `/api/v1/health/ready` as opaque orchestrator endpoints, and link `docs/operations/protected-health.md`. Do not document the protected endpoint as anonymously callable and do not add raw NATS/Temporal/Grafana URLs.

- [ ] **Step 3: Run focused Python and web gates**

```bash
uv run pytest packages/db/tests/test_migration_graph.py packages/db/tests/test_service_instance_heartbeat.py packages/db/tests/test_heartbeat.py packages/secrets/tests/test_crypto.py packages/events/tests/test_streams.py packages/workflows/tests/test_poller_health.py apps/api/tests/test_temporal_provider.py apps/api/tests/test_health.py apps/api/tests/test_operations_health.py services/tool_worker/tests/test_health_heartbeat.py tests/test_service_heartbeat_wiring.py tests/test_phase10_protected_health_harness.py -q
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web build
```

- [ ] **Step 4: Run full repository and live regression gates**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
pnpm test
pnpm lint
pnpm typecheck
pnpm build
make test-protected-health-render
make test-protected-health-integration-rootful
PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock make test-protected-health-integration-rootless
uv run pytest -m integration tests/integration/test_phase9_authorization.py -v
git diff --check
```

Expected: PASS from fresh results on the corresponding Linux socket-mode hosts; record rootful and rootless results separately when CI supplies separate hosts. The only literal rootful `SANDBOX_DOCKER_GID=10001` execution is inside the render target. If live queues contain ordinary pending work, drain the test stack and rerun; do not weaken backlog status assertions.

- [ ] **Step 5: Scan for response leaks and stale public contracts**

```bash
rg -n 'dependencies|detail|exception|traceback|hostname|\bhost\b|\bport\b|\bdsn\b' apps/api/src/jhin_api/health apps/web/app/'(app)'/operations apps/web/lib/types.ts
rg -n '/api/v1/health/ready' apps/web tests apps/api/tests
rg -n 'jhin-workflow-queue|jhin-agent-queue|jhin-tool-queue|event-worker-ingress|event-worker' docs/operations/protected-health.md apps/api/src/jhin_api/health apps/web/app/'(app)'/operations
```

Review every first-search match: internal probe variable names are allowed, but response models/page output may contain none of the forbidden raw-detail fields. Every readiness consumer must expect status only. The final search must find all exact queue/consumer names.

- [ ] **Step 6: Commit docs, then perform exact allowlisted final staging/cleanliness audit**

```bash
git add docs/operations/protected-health.md README.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: explain protected health operations"
git diff --cached --quiet
git status --short -- docs/superpowers/plans/2026-08-18-phase-10-protected-health.md packages/db/src/jhin_db/models/operations.py packages/db/src/jhin_db/models/__init__.py packages/db/src/jhin_db/heartbeat.py packages/db/src/jhin_db/__init__.py packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py packages/db/tests/test_migration_graph.py packages/db/tests/test_service_instance_heartbeat.py packages/db/tests/test_heartbeat.py packages/secrets/src/jhin_secrets/crypto.py packages/secrets/tests/test_crypto.py packages/events/src/jhin_events/streams.py packages/events/tests/test_streams.py packages/workflows/src/jhin_workflows/poller_health.py packages/workflows/tests/test_poller_health.py apps/api/src/jhin_api/temporal.py apps/api/src/jhin_api/deps.py apps/api/src/jhin_api/health/schemas.py apps/api/src/jhin_api/health/checks.py apps/api/src/jhin_api/health/service.py apps/api/src/jhin_api/health/router.py apps/api/src/jhin_api/main.py apps/api/tests/test_temporal_provider.py apps/api/tests/test_health.py apps/api/tests/test_operations_health.py apps/api/tests/conftest.py services/agent_worker/src/jhin_agent_worker/main.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/src/jhin_event_worker/settings.py services/tool_worker/src/jhin_tool_worker/main.py services/tool_worker/src/jhin_tool_worker/resources.py services/tool_worker/pyproject.toml services/tool_worker/tests/test_health_heartbeat.py tests/test_service_heartbeat_wiring.py tests/test_phase10_protected_health_harness.py uv.lock apps/web/lib/types.ts apps/web/lib/hooks.ts apps/web/components/app-shell.tsx 'apps/web/app/(app)/page.tsx' 'apps/web/app/(app)/operations/page.tsx' apps/web/tests/operations-page.test.tsx apps/web/tests/overview-health.test.tsx apps/web/tests/operations-navigation.test.tsx tests/integration/test_phase10_protected_health_migration.py tests/integration/test_phase10_protected_health.py tests/integration/conftest.py tests/integration/test_stack_health.py Makefile docs/operations/protected-health.md README.md > /tmp/jhin-phase10-protected-health-status
test ! -s /tmp/jhin-phase10-protected-health-status
git status --short -- compose.yaml compose.dev.yaml compose.rootless.yaml compose.rootful.yaml services/workflow_worker/src/jhin_workflow_worker/main.py > /tmp/jhin-phase10-protected-health-readonly-status
test ! -s /tmp/jhin-phase10-protected-health-readonly-status
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
```

Expected before the docs commit: exactly the two documentation paths are staged. Expected after it: both temporary status files are empty, so every created/modified File Map path is committed, the prior-plan Compose/workflow inputs are untouched, the index is empty, and `orgforge-production-implementation-plan.md` remains the sole explicitly preserved user-owned path. Do not mark Phase 10 subprojects 4-7 or the fourteen Phase 10 production checkboxes complete.

## Execution Notes

- `service_instance_heartbeat` is product-visible diagnostic state, not an authorization, lease, election, or workflow liveness table. Never gate effects on it.
- A current protected request proves one API instance is serving. Stale API rows remain visible in worker/key summaries so replicas are not hidden.
- NATS pending/redelivery and Temporal retained/recent/invalid poller diagnostics are safe global operational counts explicitly allowed by the spec; only fresh database heartbeats establish liveness for API/agent/tool/event, while connector and secret counts remain workspace-filtered.
- If the later keyring implementation changes crypto internals, it must preserve `active_key_version` and sorted `supported_key_versions` exactly. If DLQ/retry work extends the Operations response, it adds separately typed sections without widening `HealthComponent` or returning raw payloads.
- Any response containing an exception string, dependency coordinate, DSN, SQL, secret material, record identifier, or foreign-workspace aggregate is a release blocker.
