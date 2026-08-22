# Phase 10 Runbooks and Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the remaining Phase 10 production hardening: replica-safe shared rate limits, a single TLS reverse-proxy entry point, fail-closed production configuration, complete encrypted backup/restore and component-upgrade drills, measured five-profile sizing evidence, multi-architecture image and vulnerability gates, and executable operator runbooks.

**Architecture:** PostgreSQL revision `0018` adds one additive fixed-point token-bucket table; every API or worker replica uses short primary-only row-lock transactions and database time, while a physical streaming-replica drill proves the DDL and bucket state are WAL-safe. Production Compose exposes only Caddy, keeps product authority in PostgreSQL/Temporal/NATS, and layers exact proxy trust, nonce CSP, resource/log bounds, rootful socket or private rootless transport modes, encrypted maintenance-window backup/restore, and isolated upgrade/load/security harnesses around the five completed predecessor subprojects. Evidence is generated only by bounded commands against unique projects, ports, volumes, namespaces, keys, and fake providers; secret-audit/chaos remains the next separate plan.

**Tech Stack:** Python 3.13, FastAPI, Pydantic 2, SQLAlchemy 2.0.52, Alembic 1.19.1, PostgreSQL 17, Temporal Python SDK 1.31.0 and bundled server 1.29.7, NATS JetStream 2.12, Next.js 16.3.1, React 19.2.8, Caddy 2, Docker Compose/Buildx, age, Trivy, pip-audit, pnpm audit, pytest, Vitest, Ruff, and mypy.

**Spec:** `docs/superpowers/specs/2026-08-18-phase-10-production-operations-design.md`, especially “Backup and restore design,” “Database and service upgrade strategy,” “Reverse proxy, TLS, and production Compose hardening,” “Production abuse controls,” “Resource sizing guide,” sub-project 6, sequencing, and acceptance evidence.

## Global Constraints

- This is Phase 10 sub-project 6. Execute it only after the tool-worker, telemetry, protected-health, DLQ/retry, and master-key-rotation plans have completed their acceptance tasks. The starting Alembic head is exactly `0017`; this plan adds exactly `packages/db/src/jhin_db/alembic/versions/20260818_0018_rate_limit_bucket.py` with `revision = "0018"` and `down_revision = "0017"`.
- Do not reimplement or weaken predecessor internals. Consume `jhin-tool-queue`, `jhin_observability`, protected health/heartbeats, DLQ/retry reconciliation, and the versioned keyring/rotation CLI through their settled interfaces. The in-progress DLQ plan is an interface authority only and is never edited by this plan.
- This plan ends before sub-project 7. It adds no broad secret-canary matrix, production chaos endpoint, deterministic worker failpoint, ten-scenario chaos suite, or final Phase 10 checkbox closure. Its final evidence explicitly hands those items to the separate secret-audit/chaos plan.
- PostgreSQL remains product and command authority, Temporal remains workflow-history authority, and NATS JetStream remains at-least-once transport. Backups capture all three authorities; telemetry volumes remain replaceable diagnostics. If operator configuration enables persistent object storage, that store becomes a required state component in the same backup/restore drill.
- Production and restore use PostgreSQL `17.11-alpine` or a later reviewed security-fixed PostgreSQL 17 patch; the component-major source rehearsal uses `16.15-alpine` or a later reviewed security-fixed PostgreSQL 16 patch. This plan pins exact multi-architecture child digests and never accepts the vulnerable `17.10`, `16.14`, or older patch lines.
- All six rate-limit capacities are integers in `1..1_000_000`; all windows are seconds in `1..86_400`. Defaults are login `10/300`, webhook `120/60`, manual task `30/60`, model `60/60`, tool `120/60`, and sandbox `10/60`.
- A rate-limit subject tuple is reduced immediately to a domain-separated SHA-256 digest. Raw email, IP, connection, task, run, tool-call, or sandbox identifiers never enter the bucket table, metric labels, log fields, spans, evidence, or public errors. The schema-required nullable `workspace_id` foreign key may enter its dedicated column for ownership/cascade only; it never becomes a metric/log/span/evidence/error value or part of the public bucket interface.
- Bucket mutation uses PostgreSQL `clock_timestamp()`, one `(scope, subject_hash)` row lock, `lock_timeout = 5000ms`, `statement_timeout = 30000ms`, a 35-second client deadline, integer microtokens, and a separate short transaction. No password hash, HTTP call, Temporal/NATS call, connector call, model call, or sandbox call occurs while that transaction is open.
- Rate-limit retention deletes only rows whose `updated_at` is older than 24 hours, in ordered batches of at most 500 with `FOR UPDATE SKIP LOCKED`. All code paths acquire one bucket lock at a time, so there is no cross-bucket lock cycle.
- Application services write only to the PostgreSQL primary. The physical replica is read-only evidence: migrations and committed bucket decisions must reach it through WAL before the test passes; no application silently retries a write against the replica.
- Non-local production exposes exactly one HTTP(S) entry point through Caddy. Web, API, PostgreSQL, NATS, Temporal, Temporal UI, sandbox-runner, Collector, Prometheus, Tempo, and Grafana have no public production binding. Dev/admin overlays may publish only `127.0.0.1` dynamic or explicitly configured ports.
- Forwarded scheme and client IP are accepted only from the configured exact proxy address set resolved once at startup. Caddy overwrites, rather than appends, `X-Forwarded-For`, `X-Forwarded-Proto`, and `X-Forwarded-Host`; untrusted `Forwarded` and `X-Forwarded-*` values are ignored.
- Production requires exact HTTPS `APP_URL`, secure cookies, exact-origin credentialed CORS, no development credentials/fakes/allowlists, a safe keyring, and cleartext OTLP only to the exact internal Collector endpoint. CSP uses a fresh per-response nonce, includes `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`, and never includes `unsafe-eval` in production.
- HSTS is exactly `max-age=31536000` by default. `includeSubDomains` is permitted only through the validated value `max-age=31536000; includeSubDomains`; `preload` is always rejected.
- Maintenance mode returns retryable HTTP 503 plus `Retry-After: 60` for every mutation and webhook, while preserving opaque liveness/readiness and read-only operator access. Backup orchestration proves maintenance before stopping workers and does not reopen writes until encrypted artifacts and checksums pass.
- Required backup components are PostgreSQL globals plus every non-template database, stopped NATS JetStream state, separately encrypted operator configuration, and a separately encrypted keyring under a distinct recipient set. Temporal history/visibility are explicitly verified among the PostgreSQL databases. No live PostgreSQL or live NATS data-directory copy is valid.
- Backup plaintext is streamed between bounded subprocesses and never written to disk. State, operator configuration, and keyring artifacts use separate age recipient files; key material is never placed in argv, environment, manifest, stdout, logs, or evidence.
- Backup consistency comes from an explicit closed inventory of every product writer and database role, proved maintenance at ingress, stopped API/worker/Temporal writer containers, stopped-and-flushed NATS, two all-database session snapshots with no unapproved writer, no prepared transaction, and private PostgreSQL WAL flush-LSN fences surrounding the logical dumps. PostgreSQL background WAL is permitted and recorded; no cross-product sequence counter is consulted or claimed as an authority.
- Restore targets a fresh unique Compose project and empty project-labelled volumes first. It verifies age authentication, SHA-256, manifest compatibility, key permissions, exact image digests, Alembic revision, Temporal history/timers, NATS stream/consumer state, credential use, and one durable task before cutover.
- Baseline application rollback keeps additive schema. PostgreSQL major-version downgrade, a Temporal schema-bearing rollback, or an uncertain NATS storage downgrade means restoring the verified pre-upgrade backup into fresh volumes with the previous pinned images; changing a major image against an existing volume is forbidden.
- Bundled Temporal plaintext is allowed only at exact internal address `temporal:7233`. Every external Temporal address requires TLS, CA validation, and server name; when client-certificate authentication is configured, certificate/private-key files are an inseparable pair. Private-key bytes are forbidden from environment values and output.
- Bundled Temporal uses pinned `temporalio/server` plus a matching, one-shot pinned `temporalio/admin-tools`; `temporalio/auto-setup` is forbidden. Schema setup/upgrade is an explicit pre-start operator step with separate schema-admin credentials, and the long-running server cannot mutate schema at startup.
- Rootful live commands use only a validated `/var/run/docker.sock` snapshot and its exact positive numeric GID. Rootless host orchestration requires an already-running host-UID-10001 rootless daemon at `PHASE10_ROOTLESS_DOCKER_SOCKET`; containerized operations and sandbox-runner receive no socket mount and use only the corrected predecessor's private `rootless-docker-transport` on an internal engine network. The adapter alone sees the socket as UID 0 inside the verified rootless user namespace, remains unprivileged/capability-free, and maps to an unprivileged host identity. The runner and operations processes remain UID 10001 with no group, direct socket, privileged mode, or root fallback. Neither mode changes socket ownership, permissions, sysctls, or cgroup delegation.
- Every live gate sets `COMPOSE_DISABLE_ENV_FILE=1`, scrubs inherited Docker/Compose authority, uses a unique validated `-p` project, project-derived network/volume names, dynamic loopback ports, a unique Temporal namespace, a new keyring fixture, and a private empty Docker config. Teardown is bounded to 60 seconds, then one bounded project-scoped kill/down retry; it never targets a default project or unrelated resource.
- Every live runner exposes paired optional `--resolved-images PATH --runtime-image-env PATH` flags and passes them to `isolated_project`; providing only one is invalid. Tasks 5–11 omit them and use privately resolved local test IDs. Tasks 12–13 use only the mode-`0600` runtime env generated for that exact Docker authority by `build_phase10_images.py prepare-runtime`, and prove every local content fingerprint equals the declared platform child before service start.
- The validated small production profile keeps at least 20% host RAM/disk free after reserving every concurrent sandbox cap. Alerts fire at 70% PostgreSQL connections, five minutes of growing NATS lag, p95 queue-wait objective breach, or less than 20% RAM/disk free.
- Five sizing profiles each have a 300-second warm-up followed by exactly 1,800 measured seconds. Small and medium monitored variants use the identical product workload as their unmonitored pair; only the observability profile changes.
- A sizing profile passes only when every rolling 60-second CPU aggregate is below 80%, RAM stays below 80%, every resolved backing filesystem stays below 80% and meets its assigned byte headroom, PostgreSQL connections stay below 70%, health never fails, NATS lag does not grow for five minutes and returns to zero, and its exact API/queue objectives pass. Resolution covers the selected daemon data root, writable container layers and log roots, every named-volume mountpoint, every bind-backed state/config path, configured object storage, and PostgreSQL tablespaces; `/` is checked only when one of those actually resolves there. Public evidence contains path-free aggregates only.
- Release image evidence covers `linux/amd64` and `linux/arm64`. Critical dependency/container findings always fail. A high finding passes only through a repository allowance naming exact finding/component/architecture, owner, rationale, and unexpired ISO date.
- Public/security outputs are allowlisted summaries. Never upload `.env`, keyrings, age identities, backups, database dumps, NATS archives, DSNs, proxy certificates, Docker inspection blobs, raw service logs, raw vulnerability reports, resource identifiers, or absolute temporary paths.
- Every fenced `bash` block authored in the nine Phase 10 operations documents begins with exactly one `# phase10-command: static`, `# phase10-command: drill`, or `# phase10-command: operator` line. Static blocks run in a scrubbed fixture, drill blocks map to a tested isolated target, and operator blocks use only the Task 5 command inventory with defined/validated variables and explicit confirmation before destructive or cutover work. Tasks 6–12 establish this contract as they create their runbooks; Task 13 audits all nine without retroactively editing predecessor commits.
- Every implementation task follows strict RED -> inspect expected failure -> minimal GREEN -> affected regression -> lint/typecheck -> exact scoped staging -> commit. Documentation/evidence-only tasks first fail an executable contract test. Never use `git add .`.
- The user-owned untracked `orgforge-production-implementation-plan.md` remains exactly 82,118 bytes with SHA-256 `ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513`. It may be touched only by silent path-scoped `git status --short`, `stat -c %s`, and `shasum -a 256` guards; never open, print, search, parse, edit, stage, rename, delete, or commit it.

## Shared Interfaces

These names and values are fixed. Later tasks consume them exactly.

```python
# packages/rate_limits/src/jhin_rate_limits/contracts.py
TOKEN_SCALE = 1_000_000
RATE_LIMIT_RETENTION_HOURS = 24
RATE_LIMIT_PURGE_BATCH_SIZE = 500
RATE_LIMIT_LOCK_TIMEOUT_MS = 5_000
RATE_LIMIT_STATEMENT_TIMEOUT_MS = 30_000
RATE_LIMIT_CLIENT_TIMEOUT_SECONDS = 35.0


class RateLimitScope(StrEnum):
    LOGIN = "login"
    WEBHOOK = "webhook"
    MANUAL_TASK = "manual_task"
    MODEL = "model"
    TOOL = "tool"
    SANDBOX = "sandbox"


@dataclass(frozen=True)
class RateLimitRule:
    capacity: int
    window_seconds: int

    def __post_init__(self) -> None:
        if not 1 <= self.capacity <= 1_000_000:
            raise ValueError("rate-limit capacity must be in 1..1000000")
        if not 1 <= self.window_seconds <= 86_400:
            raise ValueError("rate-limit window must be in 1..86400")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int
    remaining_tokens_micros: int

    def __post_init__(self) -> None:
        if self.retry_after_seconds < 0:
            raise ValueError("retry-after cannot be negative")
        if self.remaining_tokens_micros < 0:
            raise ValueError("remaining tokens cannot be negative")


class RateLimitDeferred(Exception):
    def __init__(self, scope: RateLimitScope, retry_after_seconds: int) -> None:
        super().__init__("rate_limited")
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


def digest_subject(scope: RateLimitScope, *normalized_parts: str) -> bytes:
    payload = bytearray(b"jhin-rate-limit-v1\0")
    payload.extend(scope.value.encode("ascii"))
    payload.append(0)
    for part in normalized_parts:
        encoded = part.encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, "big"))
        payload.extend(encoded)
    return hashlib.sha256(payload).digest()


def login_subject(email: str, ip: str) -> bytes:
    raw_ip = ip.strip()
    canonical_ip = "unknown" if raw_ip == "unknown" else ipaddress.ip_address(raw_ip).compressed
    return digest_subject(
        RateLimitScope.LOGIN,
        email.strip().casefold(),
        canonical_ip,
    )


def workspace_subject(scope: RateLimitScope, workspace_id: UUID) -> bytes:
    return digest_subject(scope, str(workspace_id))


def connection_subject(workspace_id: UUID, connection_id: UUID) -> bytes:
    return digest_subject(RateLimitScope.WEBHOOK, str(workspace_id), str(connection_id))
```

Task 1 includes the required `hashlib` and `ipaddress` imports and tests these concrete domain-separated, length-prefixed SHA-256 bodies.

```python
# packages/rate_limits/src/jhin_rate_limits/settings.py
DEFAULT_RULES: Final[dict[RateLimitScope, RateLimitRule]] = {
    RateLimitScope.LOGIN: RateLimitRule(10, 300),
    RateLimitScope.WEBHOOK: RateLimitRule(120, 60),
    RateLimitScope.MANUAL_TASK: RateLimitRule(30, 60),
    RateLimitScope.MODEL: RateLimitRule(60, 60),
    RateLimitScope.TOOL: RateLimitRule(120, 60),
    RateLimitScope.SANDBOX: RateLimitRule(10, 60),
}


class RateLimitSettings(BaseModel):
    login_capacity: int = 10
    login_window_seconds: int = 300
    webhook_capacity: int = 120
    webhook_window_seconds: int = 60
    manual_task_capacity: int = 30
    manual_task_window_seconds: int = 60
    model_capacity: int = 60
    model_window_seconds: int = 60
    tool_capacity: int = 120
    tool_window_seconds: int = 60
    sandbox_capacity: int = 10
    sandbox_window_seconds: int = 60

    @model_validator(mode="after")
    def validate_all_rules(self) -> Self:
        for scope in RateLimitScope:
            self.rule(scope)
        return self

    def rule(self, scope: RateLimitScope) -> RateLimitRule:
        values = {
            RateLimitScope.LOGIN: (self.login_capacity, self.login_window_seconds),
            RateLimitScope.WEBHOOK: (self.webhook_capacity, self.webhook_window_seconds),
            RateLimitScope.MANUAL_TASK: (
                self.manual_task_capacity,
                self.manual_task_window_seconds,
            ),
            RateLimitScope.MODEL: (self.model_capacity, self.model_window_seconds),
            RateLimitScope.TOOL: (self.tool_capacity, self.tool_window_seconds),
            RateLimitScope.SANDBOX: (
                self.sandbox_capacity,
                self.sandbox_window_seconds,
            ),
        }
        capacity, window_seconds = values[scope]
        return RateLimitRule(capacity, window_seconds)
```

```text
# packages/rate_limits/src/jhin_rate_limits/service.py
DatabaseClock.now(session: AsyncSession) -> awaitable datetime
PostgresDatabaseClock.now(session: AsyncSession) -> awaitable datetime
RateLimiter.inspect(scope: RateLimitScope, subject_hash: bytes, rule: RateLimitRule, *, workspace_id: UUID | None = None) -> awaitable RateLimitDecision
RateLimiter.consume(scope: RateLimitScope, subject_hash: bytes, rule: RateLimitRule, *, workspace_id: UUID | None = None) -> awaitable RateLimitDecision
RateLimiter.reset(scope: RateLimitScope, subject_hash: bytes) -> awaitable None
PostgresRateLimiter(session_factory: async_sessionmaker[AsyncSession], metrics: JhinMetrics, *, service: Literal["api", "agent-worker", "tool-worker"], clock: DatabaseClock | None = None)
purge_expired_buckets(session_factory: async_sessionmaker[AsyncSession], *, limit: int = RATE_LIMIT_PURGE_BATCH_SIZE) -> awaitable int
```

Every production method enters `asyncio.timeout(35.0)`, begins one transaction, installs `SET LOCAL lock_timeout = '5000ms'` and `SET LOCAL statement_timeout = '30000ms'`, reads PostgreSQL time, takes at most one bucket lock, commits, and exits before the caller performs external work. `inspect` does not create a missing row. `consume` creates a full bucket and consumes one token on first use; an empty bucket remains at zero and returns a ceiling `Retry-After` in `1..window_seconds`. `reset` deletes only the exact digest. Denials increment `rate_limit_denials_total` with the single closed `service` label.

```text
# packages/workflows/src/jhin_workflows/temporal_connection.py
MAX_TEMPORAL_TLS_FILE_BYTES = 1_048_576
BUNDLED_TEMPORAL_ADDRESS = "temporal:7233"
TemporalTopology = Literal["bundled", "external"]

TemporalConnectionConfig(address: str, namespace: str, app_env: str, topology: TemporalTopology, tls_enabled: bool, server_name: str | None, ca_file: Path | None, client_cert_file: Path | None, client_key_file: Path | None)
TemporalConnectionConfig.build_tls_config() -> TLSConfig | bool
connect_temporal(config: TemporalConnectionConfig, runtime: ObservabilityRuntime) -> awaitable temporalio.client.Client
```

The connector accepts no caller-supplied interceptor parameter. It delegates through telemetry's
`connect_temporal_client(config, runtime, tls=...)`, which owns the exact
`temporal_client_interceptors(runtime)` list. The concrete loader rejects inline PEM, symlinks,
nonregular/oversized files, incomplete client-cert/key pairs, a private-key mode other than
`0400`/`0600`, missing server name, or production plaintext to any address except
`temporal:7233`. Errors are closed codes and never include addresses or paths.

```text
# scripts/phase10_compose.py
SocketMode = Literal["rootful", "rootless"]

DockerAuthority(mode: SocketMode, socket_path: Path, socket_device: int, socket_inode: int, socket_uid: int, socket_gid: int | None, daemon_data_root_device: int)
IsolatedComposeProject(name: str, files: Sequence[Path], env: dict[str, str], authority: DockerAuthority)
IsolatedComposeProject.argv(*args: str) -> list[str]
IsolatedComposeProject.run(*args: str, timeout_seconds: float = 300.0) -> CompletedProcess[str]
IsolatedComposeProject.host_port(service: str, container_port: int) -> int
IsolatedComposeProject.teardown() -> None
isolated_project(purpose: str, *, mode: SocketMode, files: Sequence[Path], resolved_images: Path | None = None, runtime_image_env: Path | None = None) -> AbstractContextManager[IsolatedComposeProject]
```

The host harness validates and re-stats the socket before every Docker command, supplies the same minimal environment to build/up/exec/inspect/down, forces every published port variable to `0`, and bounds teardown. Rootful records the exact positive GID. Rootless requires host owner UID 10001 and `name=rootless` daemon security, starts the corrected predecessor's isolated transport first through the host-owned socket, then gives containerized operations and sandbox-runner only `http://rootless-docker-transport:2375`; neither container has a socket bind. Before Task 12 both image arguments are omitted and the harness privately resolves its test builds to immutable local IDs. After Task 12 they are an inseparable pair generated only by `build_phase10_images.py prepare-runtime`: the harness requires mode `0600`, exact closed image keys, local `sha256:` IDs present on the selected daemon, and content fingerprints equal to the corresponding resolved platform children. Caller image variables and a lone/mismatched path are rejected. Test-only local IDs never render production Compose, whose digest-only contract is checked separately. The harness never invokes a shell and never includes a secret-bearing subprocess result in an exception.

```python
# scripts/phase10_backup_manifest.py
BACKUP_FORMAT_VERSION = 1
BACKUP_RETENTION_DAILY = 7
BACKUP_RETENTION_WEEKLY = 4
BACKUP_RETENTION_MONTHLY = 12


@dataclass(frozen=True)
class EncryptedComponent:
    name: str
    ciphertext_sha256: str
    ciphertext_bytes: int
    recipient_class: Literal["state", "operator_config", "master_key"]


@dataclass(frozen=True)
class BackupManifest:
    format_version: Literal[1]
    created_at: datetime
    application_image_digests: Mapping[str, str]
    alembic_revision: Literal["0017", "0018"]
    postgres_version: str
    nats_version: str
    temporal_version: str
    key_active_version: int
    key_supported_versions: Sequence[int]
    database_count: int
    components: Sequence[EncryptedComponent]
```

`BackupManifest.canonical_json() -> bytes` emits UTF-8 JSON with sorted keys, compact separators, one trailing newline, and no nonfinite or non-schema value.

Public manifest/evidence includes versions, counts, ciphertext hashes/sizes, durations, and pass/fail only. The encrypted PostgreSQL index owns database names. It never includes a DSN, role password, key path/material, config value, host path, resource ID, or plaintext hash.

## Complete File Map

The union of this map is exactly the union of every task `Files` block and every `git add` path. Repeated task paths appear once here.

```text
.env.example
.github/workflows/ci.yml
.github/workflows/phase10-operations.yml
.github/workflows/phase10-sizing.yml
.github/workflows/release-security.yml
Makefile
README.md
apps/api/pyproject.toml
apps/api/src/jhin_api/auth/router.py
apps/api/src/jhin_api/auth/service.py
apps/api/src/jhin_api/deps.py
apps/api/src/jhin_api/main.py
apps/api/src/jhin_api/security/middleware.py
apps/api/src/jhin_api/security/rate_limit.py
apps/api/src/jhin_api/security/trusted_proxy.py
apps/api/src/jhin_api/settings.py
apps/api/src/jhin_api/tasks/retry.py
apps/api/src/jhin_api/tasks/router.py
apps/api/src/jhin_api/tasks/service.py
apps/api/src/jhin_api/temporal.py
apps/api/src/jhin_api/webhooks/router.py
apps/api/src/jhin_api/webhooks/service.py
apps/api/tests/conftest.py
apps/api/tests/test_maintenance.py
apps/api/tests/test_production_settings.py
apps/api/tests/test_rate_limits.py
apps/api/tests/test_security.py
apps/api/tests/test_task_retry.py
apps/api/tests/test_temporal_provider.py
apps/api/tests/test_trusted_proxy.py
apps/api/tests/test_webhooks_unit.py
apps/web/Dockerfile
apps/web/lib/security-headers.ts
apps/web/proxy.ts
apps/web/tests/security-headers.test.ts
compose.dev.yaml
compose.operations.yaml
compose.phase10-backup-test.yaml
compose.phase10-rate-limit-test.yaml
compose.phase10-restore-test.yaml
compose.phase10-sizing.yaml
compose.phase10-upgrade-test.yaml
compose.rootful.yaml
compose.rootless.yaml
compose.yaml
docker/monitoring.Dockerfile
docker/operations.Dockerfile
docker/python.Dockerfile
docker/sandbox.Dockerfile
docs/evidence/phase10-backup.json
docs/evidence/phase10-hardening.md
docs/evidence/phase10-image-security.json
docs/evidence/phase10-rate-limits.json
docs/evidence/phase10-restore.json
docs/evidence/phase10-sizing.json
docs/evidence/phase10-upgrades.json
docs/operations/backup.md
docs/operations/external-temporal.md
docs/operations/image-security.md
docs/operations/production-deployment.md
docs/operations/production-readiness.md
docs/operations/rate-limits.md
docs/operations/resource-sizing.md
docs/operations/restore.md
docs/operations/upgrades.md
docs/superpowers/plans/2026-08-18-phase-10-runbooks-hardening.md
docs/superpowers/plans/2026-08-18-phase-10-tool-worker-boundary.md
ops/caddy/Caddyfile
ops/caddy/Caddyfile.test
ops/images/release-images.json
ops/images/resolved-images.json
ops/security/vulnerability-allowlist.json
ops/sizing/profiles.json
ops/versions.json
packages/db/src/jhin_db/alembic/versions/20260818_0018_rate_limit_bucket.py
packages/db/src/jhin_db/engine.py
packages/db/src/jhin_db/models/__init__.py
packages/db/src/jhin_db/models/rate_limit.py
packages/db/tests/test_engine.py
packages/db/tests/test_migration_graph.py
packages/db/tests/test_rate_limit_model.py
packages/observability/src/jhin_observability/__init__.py
packages/observability/src/jhin_observability/metrics.py
packages/observability/src/jhin_observability/registry.py
packages/observability/src/jhin_observability/temporal.py
packages/observability/tests/test_metrics.py
packages/observability/tests/test_temporal.py
packages/rate_limits/pyproject.toml
packages/rate_limits/src/jhin_rate_limits/__init__.py
packages/rate_limits/src/jhin_rate_limits/contracts.py
packages/rate_limits/src/jhin_rate_limits/service.py
packages/rate_limits/src/jhin_rate_limits/settings.py
packages/rate_limits/tests/test_contracts.py
packages/rate_limits/tests/test_service.py
packages/rate_limits/tests/test_settings.py
packages/tools/pyproject.toml
packages/tools/src/jhin_tools/builtin.py
packages/tools/src/jhin_tools/gateway.py
packages/tools/tests/test_gateway.py
packages/workflows/pyproject.toml
packages/workflows/src/jhin_workflows/__init__.py
packages/workflows/src/jhin_workflows/poller_health.py
packages/workflows/src/jhin_workflows/temporal_connection.py
packages/workflows/src/jhin_workflows/temporal_connection_cli.py
packages/workflows/tests/test_poller_health.py
packages/workflows/tests/test_temporal_connection.py
pyproject.toml
scripts/assert_phase10_command_inventory.py
scripts/assert_phase10_production_compose.py
scripts/build_phase10_images.py
scripts/evaluate_phase10_vulnerabilities.py
scripts/phase10_backup.py
scripts/phase10_backup_manifest.py
scripts/phase10_compose.py
scripts/phase10_prune_backups.py
scripts/phase10_restore.py
scripts/phase10_upgrade.py
scripts/record_phase10_hardening_evidence.py
scripts/record_phase10_rate_limit_evidence.py
scripts/run_phase10_rate_limit.py
scripts/run_phase10_sizing.py
services/agent_worker/pyproject.toml
services/agent_worker/src/jhin_agent_worker/main.py
services/agent_worker/src/jhin_agent_worker/reasoning.py
services/agent_worker/src/jhin_agent_worker/resources.py
services/agent_worker/src/jhin_agent_worker/settings.py
services/agent_worker/tests/test_rate_limits.py
services/agent_worker/tests/test_temporal_connection.py
services/event_worker/pyproject.toml
services/event_worker/src/jhin_event_worker/main.py
services/event_worker/src/jhin_event_worker/retention.py
services/event_worker/src/jhin_event_worker/settings.py
services/event_worker/tests/test_rate_limit_retention.py
services/event_worker/tests/test_temporal_connection.py
services/sandbox_runner/src/jhin_sandbox_runner/jobs.py
services/sandbox_runner/src/jhin_sandbox_runner/settings.py
services/sandbox_runner/tests/test_job_config.py
services/tool_worker/pyproject.toml
services/tool_worker/src/jhin_tool_worker/activities.py
services/tool_worker/src/jhin_tool_worker/main.py
services/tool_worker/src/jhin_tool_worker/resources.py
services/tool_worker/src/jhin_tool_worker/settings.py
services/tool_worker/tests/test_rate_limits.py
services/tool_worker/tests/test_temporal_connection.py
services/workflow_worker/src/jhin_workflow_worker/main.py
services/workflow_worker/src/jhin_workflow_worker/settings.py
services/workflow_worker/tests/test_temporal_connection.py
tests/integration/phase10_rate_limit_harness.py
tests/integration/test_phase10_api_rate_limits.py
tests/integration/test_phase10_backup.py
tests/integration/test_phase10_maintenance.py
tests/integration/test_phase10_proxy_security.py
tests/integration/test_phase10_rate_limit_migration.py
tests/integration/test_phase10_rate_limit_replication.py
tests/integration/test_phase10_restore.py
tests/integration/test_phase10_sizing_smoke.py
tests/integration/test_phase10_upgrade.py
tests/load/__init__.py
tests/load/phase10_sizing.py
tests/test_phase10_backup.py
tests/test_phase10_command_inventory.py
tests/test_phase10_compose.py
tests/test_phase10_hardening_evidence.py
tests/test_phase10_image_matrix.py
tests/test_phase10_operations_docs.py
tests/test_phase10_production_compose.py
tests/test_phase10_rate_limit_harness.py
tests/test_phase10_restore.py
tests/test_phase10_runbook_commands.py
tests/test_phase10_sizing_config.py
tests/test_phase10_upgrade.py
tests/test_phase10_vulnerability_policy.py
tests/test_production_configuration.py
tests/test_rate_limit_service_boundaries.py
uv.lock
```

---

### Prerequisite Task P0: Correct the Accepted Rootless Sandbox Transport Contract

**Files:**
- Modify: `docs/superpowers/plans/2026-08-18-phase-10-tool-worker-boundary.md`

**Interfaces:**
- Consumes: the accepted tool-worker plan's Tasks 7, 8, and 10, its UID-10001 sandbox-runner boundary, and an already-running host-UID-10001 rootless Docker daemon.
- Produces: a separately committed plan-only correction in which rootless sandbox-runner has no socket or group, a private fixed TCP-to-Unix adapter alone mounts the socket as UID 0 inside the verified rootless user namespace, and live acceptance proves rootless privileged-port and cgroup-v2 support without a rootful or runner-root fallback.

- [ ] **Step 1: Run the correction contract against the accepted plan and inspect RED**

```bash
set -euo pipefail
boundary=docs/superpowers/plans/2026-08-18-phase-10-tool-worker-boundary.md
accepted="$(mktemp)"
trap 'rm -f "$accepted"' EXIT
git show HEAD:"$boundary" >"$accepted"
if uv run python - "$accepted" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
required = (
    "rootless-docker-transport",
    "rootless_transport.py",
    "http://rootless-docker-transport:2375",
    "memory.max == 67108864",
    "bind port 80",
)
assert all(marker in text for marker in required)
assert "Rootless asserts the mounted socket owner equals the runner's effective UID" not in text
PY
then
  echo 'accepted rootless contract unexpectedly passed' >&2
  exit 1
fi
```

Expected: the Python assertion fails because the accepted plan directly bind-mounts a socket that appears as UID 0 inside rootless containers and therefore cannot be used by sandbox-runner UID 10001. Confirm this is the expected contract failure before continuing.

- [ ] **Step 2: Narrowly amend Tasks 7, 8, and 10 and run GREEN**

Task 7 adds a fixed-upstream, payload-blind bounded `asyncio` relay in the existing sandbox-runner distribution and changes runner validation to accept either the exact rootful Unix authority or the exact private rootless URL. Task 8 makes the base runner socket-free, adds the adapter only in `compose.rootless.yaml`, and keeps tool-worker/jobs off the internal `engine` network. Task 10 proves the adapter is unprivileged and root-in-user-namespace only, the runner remains `10001:10001`, the host socket metadata is unchanged, the daemon reports `name=rootless`, an unprivileged cap-drop-ALL probe binds port 80, and actual cgroup-v2 memory/CPU/PID limits are enforced. Update only the corresponding global constraint and complete-file-map entry outside those tasks.

```bash
set -euo pipefail
boundary=docs/superpowers/plans/2026-08-18-phase-10-tool-worker-boundary.md
test -z "$(git diff --cached --name-only)"
test "$(git diff --name-only -- "$boundary")" = "$boundary"
uv run python - "$boundary" <<'PY'
from pathlib import Path
import re
import shlex
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
required = (
    "rootless-docker-transport",
    "rootless_transport.py",
    "http://rootless-docker-transport:2375",
    'transport["user"] == "0:0"',
    'runner["Config"]["User"] != "0:0"',
    "memory.max == 67108864",
    "binds port 80",
    "name=rootless",
    "create_host_path: false",
)
assert all(marker in text for marker in required)
for forbidden in (
    "Rootless asserts the mounted socket owner equals the runner's effective UID",
    "def test_rootless_socket_requires_process_ownership",
    'SANDBOX_DOCKER_SOCKET_HOST="$(PHASE10_ROOTLESS_DOCKER_SOCKET)"',
):
    assert forbidden not in text
for number in (7, 8, 10):
    match = re.search(
        rf"^### Task {number}:.*?(?=^### Task \d+:|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    task = match.group(0)
    files = re.findall(r"^- (?:Create|Modify): `([^`]+)`$", task, re.MULTILINE)
    adds: list[str] = []
    for command in re.findall(r"^git add .+$", task, re.MULTILINE):
        adds.extend(shlex.split(command)[2:])
    assert len(files) == len(set(files))
    assert set(files) == set(adds)
PY
git diff --check -- "$boundary"
```

Expected: PASS. No production code is changed by this prerequisite; the corrected plan now makes the previously impossible rootless identity boundary executable and acceptance-tested.

- [ ] **Step 3: Stage and commit the predecessor correction alone**

```bash
set -euo pipefail
boundary=docs/superpowers/plans/2026-08-18-phase-10-tool-worker-boundary.md
git add docs/superpowers/plans/2026-08-18-phase-10-tool-worker-boundary.md
test "$(git diff --cached --name-only)" = "$boundary"
git diff --cached --check
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git commit -m "docs: correct rootless sandbox transport plan"
```

Expected: commit 1 of 15 contains exactly the tracked predecessor-plan correction. The new runbooks plan remains untracked and is not staged in this commit.

### Task 0: Check In the Reviewed Hardening Plan After All Five Predecessors and the Boundary Correction

**Files:**
- Create: `docs/superpowers/plans/2026-08-18-phase-10-runbooks-hardening.md`

**Interfaces:**
- Consumes: accepted completion commits for tool-worker, telemetry, protected health, DLQ/retry, and master-key rotation; Prerequisite P0's separately committed rootless correction; Alembic head `0017`; the unchanged OrgForge sentinel metadata.
- Produces: one runbooks-plan-only checkpoint commit and an execution tree in which sub-project 6 may begin without further predecessor-plan edits.

- [ ] **Step 1: Prove all predecessor acceptance commits are ancestors**

Run on the Linux implementation host:

```bash
set -euo pipefail
for command_name in git uv rg stat shasum; do
  command -v "$command_name" >/dev/null
done
subjects=(
  "test: verify Phase 10 tool-worker boundary"
  "docs: correct rootless sandbox transport plan"
  "docs(observability): record Phase 10 telemetry evidence"
  "docs: explain protected health operations"
  "docs: explain dlq and retry recovery"
  "docs: publish master key rotation runbook"
)
for subject in "${subjects[@]}"; do
  commit="$(git log -1 --format=%H --fixed-strings --grep="$subject")"
  test -n "$commit"
  test "$(git show -s --format=%s "$commit")" = "$subject"
  git merge-base --is-ancestor "$commit" HEAD
done
test -f packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py
test -f packages/db/src/jhin_db/alembic/versions/20260818_0016_dlq_retry.py
test -f packages/db/src/jhin_db/alembic/versions/20260818_0017_master_key_rotation.py
uv run python -c 'from alembic.script import ScriptDirectory; from jhin_db.migrate import alembic_config; s=ScriptDirectory.from_config(alembic_config("sqlite://")); assert s.get_heads()==["0017"]'
```

Expected: PASS. Any missing/non-ancestor completion commit or non-`0017` head stops execution before staging.

- [ ] **Step 2: Validate scope, structure, and the metadata-only sentinel**

```bash
set -euo pipefail
plan=docs/superpowers/plans/2026-08-18-phase-10-runbooks-hardening.md
test -f "$plan"
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
test "$(rg -c '^### Task [0-9]+:' "$plan")" = "14"
test "$(rg -c '^### Prerequisite Task P0:' "$plan")" = "1"
test "$(rg -c '^git commit -m ' "$plan")" = "15"
! rg -n 'T[B]D|T[O]DO|implement[ ]later|fill[ ]in|similar[ ]to Task|git add[ ]+\.$' "$plan"
uv run python - "$plan" <<'PY'
from pathlib import Path
import re
import shlex
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
map_match = re.search(
    r"^## Complete File Map\n.*?^```text\n(.*?)^```$", text, re.MULTILINE | re.DOTALL
)
assert map_match is not None
map_paths = [line for line in map_match.group(1).splitlines() if line]
assert map_paths == sorted(set(map_paths))

all_files: set[str] = set()
all_adds: set[str] = set()
sections = re.split(
    r"(?=^### (?:Prerequisite Task P0|Task \d+):)", text, flags=re.MULTILINE
)[1:]
assert len(sections) == 15
assert sections[0].startswith("### Prerequisite Task P0:")
tasks = sections[1:]
for expected_number, task in enumerate(tasks):
    heading = re.match(r"### Task (\d+):", task)
    assert heading is not None and int(heading.group(1)) == expected_number
    files = re.findall(r"^- (?:Create|Modify): `([^`]+)`$", task, re.MULTILINE)
    adds: list[str] = []
    for command in re.findall(r"^git add .+$", task, re.MULTILINE):
        adds.extend(shlex.split(command)[2:])
    assert len(files) == len(set(files))
    assert len(adds) == len(set(adds))
    assert set(files) == set(adds)
    assert len(re.findall(r"^\*\*Interfaces:\*\*$", task, re.MULTILINE)) == 1
    assert len(re.findall(r"^git commit -m ", task, re.MULTILINE)) == 1
    all_files.update(files)
    all_adds.update(adds)

prerequisite = sections[0]
files = re.findall(r"^- (?:Create|Modify): `([^`]+)`$", prerequisite, re.MULTILINE)
adds = []
for command in re.findall(r"^git add .+$", prerequisite, re.MULTILINE):
    adds.extend(shlex.split(command)[2:])
assert set(files) == set(adds)
assert len(re.findall(r"^\*\*Interfaces:\*\*$", prerequisite, re.MULTILINE)) == 1
assert len(re.findall(r"^git commit -m ", prerequisite, re.MULTILINE)) == 1
all_files.update(files)
all_adds.update(adds)

assert set(map_paths) == all_files == all_adds
PY
```

Expected: PASS with one prerequisite plus 14 numbered tasks and 15 exact commits. The sentinel commands are silent assertions; no command prints or searches its contents.

- [ ] **Step 3: Stage and commit only this plan**

```bash
set -euo pipefail
git add docs/superpowers/plans/2026-08-18-phase-10-runbooks-hardening.md
test "$(git diff --cached --name-only)" = "docs/superpowers/plans/2026-08-18-phase-10-runbooks-hardening.md"
git diff --cached --check
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git commit -m "docs: plan Phase 10 runbooks and hardening"
```

Expected: commit 2 of 15 is documentation-only and contains only this runbooks plan. The design spec, predecessor plans, DLQ plan, and OrgForge sentinel are absent from the commit.

### Task 1: Add the Shared PostgreSQL Token Bucket and Revision `0018`

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `docker/python.Dockerfile`
- Create: `packages/rate_limits/pyproject.toml`
- Create: `packages/rate_limits/src/jhin_rate_limits/__init__.py`
- Create: `packages/rate_limits/src/jhin_rate_limits/contracts.py`
- Create: `packages/rate_limits/src/jhin_rate_limits/service.py`
- Create: `packages/rate_limits/src/jhin_rate_limits/settings.py`
- Create: `packages/rate_limits/tests/test_contracts.py`
- Create: `packages/rate_limits/tests/test_service.py`
- Create: `packages/rate_limits/tests/test_settings.py`
- Create: `packages/db/src/jhin_db/models/rate_limit.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260818_0018_rate_limit_bucket.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Create: `packages/db/tests/test_rate_limit_model.py`
- Create: `tests/integration/test_phase10_rate_limit_migration.py`
- Modify: `packages/observability/src/jhin_observability/__init__.py`
- Modify: `packages/observability/src/jhin_observability/metrics.py`
- Modify: `packages/observability/src/jhin_observability/registry.py`
- Modify: `packages/observability/tests/test_metrics.py`

**Interfaces:**
- Consumes: SQLAlchemy session factories, `JhinMetrics`, UUID workspace identity, migration head `0017`, and the predecessor JSON/redaction contract.
- Produces: `jhin_rate_limits` Shared Interfaces, ORM `RateLimitBucket`, exact migration `0018`, bounded bucket purge, and counter `rate_limit_denials_total` with only label `service`.

- [ ] **Step 1: Write RED contract, schema, arithmetic, timeout, and metric tests**

Create tests that exercise the concrete contracts rather than an in-memory substitute:

```python
def test_default_rules_are_exact_and_bounded() -> None:
    assert DEFAULT_RULES == {
        RateLimitScope.LOGIN: RateLimitRule(10, 300),
        RateLimitScope.WEBHOOK: RateLimitRule(120, 60),
        RateLimitScope.MANUAL_TASK: RateLimitRule(30, 60),
        RateLimitScope.MODEL: RateLimitRule(60, 60),
        RateLimitScope.TOOL: RateLimitRule(120, 60),
        RateLimitScope.SANDBOX: RateLimitRule(10, 60),
    }
    for invalid in (0, 1_000_001):
        with pytest.raises(ValueError):
            RateLimitRule(invalid, 60)
    for invalid in (0, 86_401):
        with pytest.raises(ValueError):
            RateLimitRule(10, invalid)


def test_subject_digest_is_normalized_domain_separated_and_value_free() -> None:
    first = login_subject("  Owner@Example.COM ", "2001:0db8::1")
    second = login_subject("owner@example.com", "2001:db8::1")
    assert first == second and len(first) == 32
    assert first != workspace_subject(
        RateLimitScope.MODEL, UUID("018f0000-0000-7000-8000-000000000001")
    )
    assert b"owner" not in first and b"2001" not in first


async def test_concurrent_consumers_share_exact_capacity(
    postgres_limiter: PostgresRateLimiter,
) -> None:
    subject = digest_subject(RateLimitScope.TOOL, "shared-test-subject")
    decisions = await asyncio.gather(
        *[
            postgres_limiter.consume(RateLimitScope.TOOL, subject, RateLimitRule(30, 60))
            for _ in range(120)
        ]
    )
    assert sum(decision.allowed for decision in decisions) == 30
    assert all(
        1 <= decision.retry_after_seconds <= 60 for decision in decisions if not decision.allowed
    )


async def test_bucket_does_not_hold_lock_across_caller_work(
    postgres_limiter: PostgresRateLimiter,
) -> None:
    subject = digest_subject(RateLimitScope.MODEL, "lock-release")
    decision = await postgres_limiter.consume(RateLimitScope.MODEL, subject, RateLimitRule(1, 60))
    assert decision.allowed
    async with postgres_limiter.session_factory() as session:
        async with asyncio.timeout(1):
            await session.execute(
                select(RateLimitBucket)
                .where(RateLimitBucket.subject_hash == subject)
                .with_for_update()
            )


def test_rate_limit_metric_has_no_subject_label(metrics: JhinMetrics) -> None:
    instrument = metrics.counter("rate_limit_denials_total")
    instrument.add(1, {"service": "api"})
    with pytest.raises(MetricLabelError):
        instrument.add(1, {"service": "api", "workspace_id": "forbidden"})
```

Run:

```bash
uv run pytest packages/rate_limits/tests packages/db/tests/test_rate_limit_model.py packages/db/tests/test_migration_graph.py packages/observability/tests/test_metrics.py -q
```

Expected: FAIL on missing package, model, migration `0018`, and metric registration.

- [ ] **Step 2: Implement the exact model and reversible additive migration**

Use lowercase names and database constraints in both ORM and migration:

```python
class RateLimitBucket(Base):
    __tablename__ = "rate_limit_bucket"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('login','webhook','manual_task','model','tool','sandbox')",
            name="ck_rate_limit_bucket_scope",
        ),
        CheckConstraint(
            "octet_length(subject_hash) = 32",
            name="ck_rate_limit_bucket_subject_hash_length",
        ),
        CheckConstraint(
            "tokens_micros >= 0 AND tokens_micros <= 1000000000000",
            name="ck_rate_limit_bucket_tokens_micros",
        ),
        CheckConstraint(
            "(scope = 'login' AND workspace_id IS NULL) OR "
            "(scope <> 'login' AND workspace_id IS NOT NULL)",
            name="ck_rate_limit_bucket_workspace_scope",
        ),
        Index("ix_rate_limit_bucket_workspace_id", "workspace_id"),
        Index("ix_rate_limit_bucket_updated_at", "updated_at", "scope", "subject_hash"),
    )

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    subject_hash: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    workspace_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=True,
    )
    tokens_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    refilled_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
```

Migration header and graph are exact:

```python
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```

On PostgreSQL, both directions first install transaction-local `lock_timeout = '5000ms'` and `statement_timeout = '30000ms'`. `upgrade()` creates only `rate_limit_bucket`, its named constraints, foreign key, composite primary key, `ix_rate_limit_bucket_workspace_id`, and the retention-covering `(updated_at, scope, subject_hash)` index `ix_rate_limit_bucket_updated_at`. The foreign-key index prevents workspace deletion/cascade from scanning unrelated buckets. `downgrade()` drops both indexes and the table only. It does not scan, lock, rewrite, or add a column to any existing table; timeout failure aborts the whole transactional migration instead of waiting indefinitely.

- [ ] **Step 3: Implement digesting, validated settings, and fixed-point decisions**

Use the following concrete digest and refill math:

```python
def digest_subject(scope: RateLimitScope, *normalized_parts: str) -> bytes:
    digest = hashlib.sha256()
    digest.update(b"jhin-rate-limit-v1\x00")
    digest.update(scope.value.encode("ascii"))
    digest.update(b"\x00")
    for part in normalized_parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.digest()


def _refill(
    *,
    tokens_micros: int,
    elapsed_micros: int,
    rule: RateLimitRule,
) -> int:
    earned = (
        max(0, elapsed_micros) * rule.capacity * TOKEN_SCALE // (rule.window_seconds * 1_000_000)
    )
    return min(rule.capacity * TOKEN_SCALE, tokens_micros + earned)


def _decision(tokens_micros: int, rule: RateLimitRule, *, consume: bool) -> RateLimitDecision:
    if tokens_micros >= TOKEN_SCALE:
        remaining = tokens_micros - TOKEN_SCALE if consume else tokens_micros
        return RateLimitDecision(True, 0, remaining)
    deficit = TOKEN_SCALE - tokens_micros
    denominator = rule.capacity * TOKEN_SCALE
    retry_after = max(
        1,
        min(
            rule.window_seconds,
            (deficit * rule.window_seconds + denominator - 1) // denominator,
        ),
    )
    return RateLimitDecision(False, retry_after, tokens_micros)
```

`login_subject` uses `email.strip().casefold()` and canonical `ipaddress.ip_address(ip).compressed`, falling back to the exact string `unknown` only when no peer exists. UUID subjects use canonical lowercase UUID strings. No returned object retains normalized inputs.

- [ ] **Step 4: Implement the one-row transaction and bounded retention**

For `consume`, reject a non-null workspace on `LOGIN` and a null workspace on every other scope before opening a session. `inspect` applies the same scope/workspace validation. Install local timeouts before the first authority query; insert the candidate row using PostgreSQL `clock_timestamp()` with `ON CONFLICT DO NOTHING`; select exactly one row `FOR UPDATE`; then query a fresh `clock_timestamp()` after acquiring that lock. Validate an existing row's workspace binding: a mismatch is an invariant error, never a reassignment. Calculate elapsed microseconds from `timedelta.days/seconds/microseconds` with integer arithmetic only, update `tokens_micros`, `refilled_at`, and `updated_at`, commit, then increment the denial metric only after the transaction closes.

Retention uses this single statement inside the same timeout wrapper:

```sql
WITH due AS (
  SELECT scope, subject_hash
  FROM rate_limit_bucket
  WHERE updated_at < clock_timestamp() - INTERVAL '24 hours'
  ORDER BY updated_at, scope, subject_hash
  LIMIT :limit
  FOR UPDATE SKIP LOCKED
)
DELETE FROM rate_limit_bucket AS bucket
USING due
WHERE bucket.scope = due.scope
  AND bucket.subject_hash = due.subject_hash
RETURNING bucket.scope
```

Do not catch a timeout and retry inside the same request. Map SQLSTATE `55P03`, `57014`, and client expiry to closed `RateLimitStorageError` codes; callers fail closed with retryable service behavior.

- [ ] **Step 5: Run focused GREEN and real-PostgreSQL migration paths**

```bash
uv lock
uv run pytest packages/rate_limits/tests packages/db/tests/test_rate_limit_model.py packages/db/tests/test_migration_graph.py packages/observability/tests/test_metrics.py -q
uv run pytest -m integration tests/integration/test_phase10_rate_limit_migration.py -v
uv run ruff check packages/rate_limits packages/db/src/jhin_db/models/rate_limit.py packages/db/src/jhin_db/alembic/versions/20260818_0018_rate_limit_bucket.py packages/observability
uv run mypy packages/rate_limits/src packages/db/src packages/observability/src
```

The integration test creates two fresh PostgreSQL databases. It proves `base -> 0018`, `0017 -> 0018`, `0018 -> 0017 -> 0018`, all named constraints/indexes, exact defaults, UTC timestamps, failed oversized/negative values, and no changed table other than `rate_limit_bucket` across the `0017 -> 0018` boundary.

- [ ] **Step 6: Guard, stage exactly Task 1, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add pyproject.toml uv.lock docker/python.Dockerfile packages/rate_limits/pyproject.toml packages/rate_limits/src/jhin_rate_limits/__init__.py packages/rate_limits/src/jhin_rate_limits/contracts.py packages/rate_limits/src/jhin_rate_limits/service.py packages/rate_limits/src/jhin_rate_limits/settings.py packages/rate_limits/tests/test_contracts.py packages/rate_limits/tests/test_service.py packages/rate_limits/tests/test_settings.py packages/db/src/jhin_db/models/rate_limit.py packages/db/src/jhin_db/models/__init__.py packages/db/src/jhin_db/alembic/versions/20260818_0018_rate_limit_bucket.py packages/db/tests/test_migration_graph.py packages/db/tests/test_rate_limit_model.py tests/integration/test_phase10_rate_limit_migration.py packages/observability/src/jhin_observability/__init__.py packages/observability/src/jhin_observability/metrics.py packages/observability/src/jhin_observability/registry.py packages/observability/tests/test_metrics.py
git diff --cached --check
git commit -m "feat: add replica-safe PostgreSQL rate limits"
```

Expected: commit 3 of 15; Alembic has one head, `0018`.

### Task 2: Enforce Login, Webhook, and Manual-Task Limits in the API

**Files:**
- Modify: `apps/api/pyproject.toml`
- Modify: `uv.lock`
- Modify: `apps/api/src/jhin_api/security/rate_limit.py`
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/src/jhin_api/settings.py`
- Modify: `apps/api/src/jhin_api/auth/service.py`
- Modify: `apps/api/src/jhin_api/auth/router.py`
- Modify: `apps/api/src/jhin_api/webhooks/service.py`
- Modify: `apps/api/src/jhin_api/webhooks/router.py`
- Modify: `apps/api/src/jhin_api/tasks/service.py`
- Modify: `apps/api/src/jhin_api/tasks/router.py`
- Modify: `apps/api/src/jhin_api/tasks/retry.py`
- Modify: `apps/api/tests/conftest.py`
- Modify: `apps/api/tests/test_security.py`
- Create: `apps/api/tests/test_rate_limits.py`
- Modify: `apps/api/tests/test_webhooks_unit.py`
- Modify: `apps/api/tests/test_task_retry.py`
- Create: `tests/integration/test_phase10_api_rate_limits.py`

**Interfaces:**
- Consumes: `PostgresRateLimiter`, validated rules, trusted `client_ip()` seam, existing durable ordinary/manual start authorization, idempotency, telemetry runtime, and DLQ/retry APIs.
- Produces: `RateLimiterDep`; safe HTTP 429 with integer `Retry-After`; failure-only login accounting; denial-before-write webhook/manual-task behavior across API replicas.

- [ ] **Step 1: Write RED API tests with a recording limiter**

```python
async def test_login_failure_limit_returns_retry_after_without_raw_subject(
    api: ApiHarness,
    recording_limiter: RecordingRateLimiter,
) -> None:
    await api.bootstrap_owner()
    recording_limiter.set_denied(RateLimitScope.LOGIN, retry_after_seconds=37)
    response = await api.client.post(
        "/api/v1/auth/login",
        json={"email": " Owner@Example.com ", "password": "wrong"},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "37"
    assert response.json() == {"detail": "Too many requests; retry later"}
    assert recording_limiter.calls[0].subject_hash == login_subject(
        "owner@example.com", "127.0.0.1"
    )
    assert "owner@example.com" not in recording_limiter.rendered_calls()


async def test_successful_login_resets_only_after_bucket_allows(
    api: ApiHarness,
    recording_limiter: RecordingRateLimiter,
) -> None:
    await api.bootstrap_owner()
    response = await api.login_owner()
    assert response.status_code == 200
    assert [call.operation for call in recording_limiter.calls] == ["inspect", "reset"]


async def test_webhook_denial_creates_no_delivery_event_or_publish(
    webhook_world: WebhookWorld,
) -> None:
    webhook_world.limiter.set_denied(RateLimitScope.WEBHOOK, retry_after_seconds=2)
    response = await webhook_world.post_signed_delivery()
    assert response.status_code == 429 and response.headers["Retry-After"] == "2"
    assert await webhook_world.delivery_count() == 0
    assert webhook_world.published == []


async def test_manual_task_and_retry_share_workspace_bucket(
    api: ApiHarness,
    recording_limiter: RecordingRateLimiter,
) -> None:
    workspace_id, task_id = await api.failed_retryable_task()
    recording_limiter.set_denied(RateLimitScope.MANUAL_TASK, retry_after_seconds=11)
    create = await api.create_assigned_task(workspace_id)
    retry = await api.retry_task(workspace_id, task_id, idempotency_key="retry-rate-limit-0001")
    assert (create.status_code, retry.status_code) == (429, 429)
    assert create.headers["Retry-After"] == retry.headers["Retry-After"] == "11"
    assert await api.task_count(workspace_id) == 1
    assert await api.task_retry_count(workspace_id) == 0
```

Run:

```bash
uv run pytest apps/api/tests/test_rate_limits.py apps/api/tests/test_security.py apps/api/tests/test_webhooks_unit.py apps/api/tests/test_task_retry.py -q
```

Expected: FAIL because API state still uses the process-local login counter and other boundaries have no limiter.

- [ ] **Step 2: Replace the process-local object with one application-lifetime database limiter**

Add `jhin-rate-limits` as a direct API dependency. After observability and the session factory exist, construct exactly one:

```python
app.state.rate_limiter = PostgresRateLimiter(
    app.state.session_factory,
    runtime.metrics,
    service="api",
)
```

Expose it only through:

```python
def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def rate_limit_http_error(decision: RateLimitDecision) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests; retry later",
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )
```

Remove every mutable dictionary/thread lock/monotonic clock from `apps/api/src/jhin_api/security/rate_limit.py`. It becomes the thin FastAPI adapter above and re-exports no compatibility `LoginRateLimiter`.

Define `client_ip(request)` as a narrow seam: by default it canonicalizes only the ASGI socket peer and never reads `Forwarded` or `X-Forwarded-*`. It may read Task 4's typed resolved-client request-state value only when that middleware has also set its private trusted-proxy marker. Thus Task 2 is safe before proxy support exists and Task 4 can add exact proxy trust without changing authentication semantics.

- [ ] **Step 3: Apply exact login failure semantics**

Before password verification, call `inspect(LOGIN, login_subject(normalized_email, ip), login_rule)` in its closed short transaction. If the existing failure bucket is empty, return 429 before Argon2. Otherwise perform the real or timing-equalized dummy hash with no limiter transaction open, then:

1. For an invalid/disabled/unknown user, call `consume` for the same digest. Return 429 if a concurrent failure exhausted it; otherwise write the existing safe audit and return 401.
2. For a valid user, call `reset`, then create/commit the session. A correct password does not consume capacity, but it cannot bypass a bucket that was already empty at the initial inspection.
3. Storage failure returns safe retryable 503. No limiter transaction shares the authentication database transaction or Argon2 work.

This serializes concurrent failure accounting without holding a bucket lock during password verification and keeps a correct password from bypassing an already empty bucket.

- [ ] **Step 4: Apply webhook and manual-start admission before durable work**

For webhooks, resolve and verify the connection/signature first, then consume `WEBHOOK` using `connection_subject(workspace_id, connection_id)` immediately before inserting `WebhookDelivery` or publishing NATS. A denial returns 429, creates no row/envelope/audit, and is provider-retryable.

For assigned-task creation, agent messaging/assignment, ordinary durable start authorization, and `request_task_retry`, consume `MANUAL_TASK` with `workspace_subject` before allocating a Task/TaskRetry or changing task state. Unassigned task drafting is not a start and does not consume. Idempotent replay of an already-created TaskRetry re-queries the existing idempotency binding before consuming another token.

- [ ] **Step 5: Run GREEN, replica-shaped API integration, and regressions**

```bash
uv lock
uv run pytest apps/api/tests/test_rate_limits.py apps/api/tests/test_security.py apps/api/tests/test_webhooks_unit.py apps/api/tests/test_task_retry.py apps/api/tests/test_idempotency.py -q
uv run pytest -m integration tests/integration/test_phase10_api_rate_limits.py -v
uv run pytest apps/api/tests -q
uv run ruff check apps/api tests/integration/test_phase10_api_rate_limits.py
uv run mypy apps/api/src
```

The integration test starts two API processes against one PostgreSQL primary. Exactly 10 invalid logins, 120 verified webhook deliveries, and 30 manual starts are admitted per configured window across both replicas; the next request receives bounded 429, and denial creates no work. It also asserts no unhashed subject appears in captured JSON logs or HTTP bodies.

- [ ] **Step 6: Guard, stage exactly Task 2, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add apps/api/pyproject.toml uv.lock apps/api/src/jhin_api/security/rate_limit.py apps/api/src/jhin_api/deps.py apps/api/src/jhin_api/main.py apps/api/src/jhin_api/settings.py apps/api/src/jhin_api/auth/service.py apps/api/src/jhin_api/auth/router.py apps/api/src/jhin_api/webhooks/service.py apps/api/src/jhin_api/webhooks/router.py apps/api/src/jhin_api/tasks/service.py apps/api/src/jhin_api/tasks/router.py apps/api/src/jhin_api/tasks/retry.py apps/api/tests/conftest.py apps/api/tests/test_security.py apps/api/tests/test_rate_limits.py apps/api/tests/test_webhooks_unit.py apps/api/tests/test_task_retry.py tests/integration/test_phase10_api_rate_limits.py
git diff --cached --check
git commit -m "feat: enforce shared API rate limits"
```

Expected: commit 4 of 15; every API denial occurs before durable work.

### Task 3: Enforce Model, Tool, and Sandbox Limits at Effect Boundaries

**Files:**
- Modify: `uv.lock`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/reasoning.py`
- Create: `services/agent_worker/tests/test_rate_limits.py`
- Modify: `services/tool_worker/pyproject.toml`
- Modify: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/activities.py`
- Create: `services/tool_worker/tests/test_rate_limits.py`
- Modify: `packages/tools/pyproject.toml`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Modify: `packages/tools/src/jhin_tools/gateway.py`
- Modify: `packages/tools/tests/test_gateway.py`
- Modify: `services/event_worker/pyproject.toml`
- Modify: `services/event_worker/src/jhin_event_worker/retention.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Create: `services/event_worker/tests/test_rate_limit_retention.py`
- Create: `tests/test_rate_limit_service_boundaries.py`

**Interfaces:**
- Consumes: stable model/tool invocation identities, `reason_agent_step`, the `ToolExecutionContext` definition in `jhin_tools.builtin`, tool gateway pre-claim paths, exact CLI/sandbox tool registry, event-worker retention loop, telemetry metrics, and `RateLimitDeferred`.
- Produces: a direct `jhin-tools -> jhin-rate-limits` workspace/runtime dependency, `ToolAdmission`, `ToolExecutionContext.admission`, actual-call-only model/tool admission, pre-effect sandbox admission, retryable Temporal delays without duplicate effect authority, and 24-hour bounded bucket retention.

- [ ] **Step 1: Write RED boundary and replay tests**

```python
async def test_model_limit_is_after_reasoning_replay_check_before_provider(
    reasoning_world: ReasoningWorld,
) -> None:
    reasoning_world.limiter.deny(RateLimitScope.MODEL, retry_after_seconds=9)
    with pytest.raises(ApplicationError) as caught:
        await reasoning_world.reason_agent_step()
    assert caught.value.type == "rate_limited"
    assert caught.value.next_retry_delay == timedelta(seconds=9)
    assert reasoning_world.provider_requests == 0
    await reasoning_world.install_committed_reasoning()
    await reasoning_world.reason_agent_step()
    assert reasoning_world.limiter.call_count(RateLimitScope.MODEL) == 1


async def test_terminal_tool_replay_does_not_consume_again(tool_world: ToolWorld) -> None:
    first = await tool_world.execute_bound_tool()
    second = await tool_world.execute_bound_tool()
    assert first == second
    assert tool_world.limiter.call_count(RateLimitScope.TOOL) == 1


async def test_sandbox_denial_occurs_before_gateway_claim_or_runner_call(
    gateway_world: GatewayWorld,
) -> None:
    gateway_world.limiter.deny(RateLimitScope.SANDBOX, retry_after_seconds=4)
    with pytest.raises(RateLimitDeferred) as caught:
        await gateway_world.request("cli.test.run")
    assert caught.value.retry_after_seconds == 4
    assert await gateway_world.tool_call_count() == 0
    assert gateway_world.external_effects == 0


async def test_approved_sandbox_denial_preserves_pending_identity(
    gateway_world: GatewayWorld,
) -> None:
    approval_id = await gateway_world.request_approved_cli_call()
    gateway_world.limiter.deny(RateLimitScope.SANDBOX, retry_after_seconds=6)
    with pytest.raises(RateLimitDeferred):
        await gateway_world.resolve_approval(approval_id)
    assert await gateway_world.pending_approval_identity(approval_id) == gateway_world.invocation_id
    assert gateway_world.external_effects == 0


def test_tools_declares_and_locks_rate_limit_runtime_dependency() -> None:
    project = tomllib.loads(Path("packages/tools/pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"].count("jhin-rate-limits") == 1
    assert project["tool"]["uv"]["sources"]["jhin-rate-limits"] == {"workspace": True}
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    tools = next(package for package in lock["package"] if package["name"] == "jhin-tools")
    assert {"name": "jhin-rate-limits"} in tools["dependencies"]


async def test_execution_context_admission_uses_shared_runtime_scope(
    context: ToolExecutionContext,
) -> None:
    calls: list[tuple[RateLimitScope, UUID]] = []

    async def admission(scope: RateLimitScope, workspace_id: UUID) -> None:
        calls.append((scope, workspace_id))

    guarded = replace(context, admission=admission)
    assert ToolExecutionContext.__dataclass_fields__["admission"].kw_only is True
    assert guarded.admission is not None
    await guarded.admission(RateLimitScope.TOOL, guarded.workspace_id)
    assert calls == [(RateLimitScope.TOOL, context.workspace_id)]
```

Place the metadata assertion in `tests/test_rate_limit_service_boundaries.py` and the runtime
dataclass assertion in `packages/tools/tests/test_gateway.py`; the latter imports
`RateLimitScope` from the installed `jhin_rate_limits` distribution rather than redeclaring a
string literal.

Run:

```bash
uv run pytest services/agent_worker/tests/test_rate_limits.py services/tool_worker/tests/test_rate_limits.py packages/tools/tests/test_gateway.py services/event_worker/tests/test_rate_limit_retention.py tests/test_rate_limit_service_boundaries.py -q
```

Expected: FAIL because resources have no shared limiter, `jhin-tools` neither declares nor locks
`jhin-rate-limits`, `builtin.py` has no admission field, and gateway has no pre-claim admission
seam. Inspect all four failures before implementation.

- [ ] **Step 2: Construct one limiter per key-bearing worker**

Add direct `jhin-rate-limits` dependencies to agent-worker, tool-worker, and `jhin-tools`, plus one retention-only dependency to event-worker. In `packages/tools/pyproject.toml`, add the exact dependency string `jhin-rate-limits` once and `jhin-rate-limits = { workspace = true }` under `[tool.uv.sources]`; regenerate `uv.lock` and require the `jhin-tools` lock entry to contain that runtime edge. `Resources.create` builds `PostgresRateLimiter` from the existing session factory and observability runtime. Settings embed the exact validated rule fields; they do not redeclare validation.

Agent-worker consumes `MODEL` only after it proves no committed reasoning result exists and immediately before an actual provider HTTP attempt. A denial becomes:

```python
raise ApplicationError(
    "rate_limited",
    type="rate_limited",
    non_retryable=False,
    next_retry_delay=timedelta(seconds=deferred.retry_after_seconds),
) from None
```

The error contains no workspace or subject. Temporal retry keeps the same workflow/activity identity.

- [ ] **Step 3: Add gateway admissions without changing durable invocation authority**

In `packages/tools/src/jhin_tools/builtin.py`, import the shared `RateLimitScope` from
`jhin_rate_limits` and extend `ToolExecutionContext` with:

```python
ToolAdmission = Callable[[RateLimitScope, UUID], Awaitable[None]]
```

Add the field as
`admission: ToolAdmission | None = field(default=None, kw_only=True)` after the context's current
required fields; preserve every current field and constructor behavior. Do not shadow the runtime
dependency with a second local enum or a `Literal` alias.

The concrete gateway behavior is ordered:

1. Validate tool/policy/connection and load an existing deterministic invocation outcome.
2. If an existing pending/terminal identity is being replayed, do not consume `TOOL` again.
3. Before a new `ToolCall`/Approval claim, call `admission(TOOL, workspace_id)`.
4. For the exact reviewed `SANDBOX_TOOL_NAMES` set, call `admission(SANDBOX, workspace_id)` immediately before either direct claim or approved execution transition, while no gateway row lock is held.
5. On denial, persist no new ToolCall, change no approval status, and invoke no executor. Tool-worker maps the exception to retryable Temporal delay.

The catalog test compares `SANDBOX_TOOL_NAMES` exactly with registered `cli.*` tools that submit runner jobs, so a new sandbox tool cannot omit the second admission.

- [ ] **Step 4: Add retention to the existing event-worker maintenance cadence**

Call `purge_expired_buckets` from the predecessor retention loop after its recovery-state batch and before sleeping. A timeout logs only event `rate_limit.retention_failed` plus safe error type/code. It never blocks delivery or causes event failure. Test two concurrent purge workers delete disjoint rows through `SKIP LOCKED` and leave fresh rows.

- [ ] **Step 5: Run GREEN and affected worker/gateway suites**

```bash
uv lock
uv lock --check
uv run pytest services/agent_worker/tests/test_rate_limits.py services/tool_worker/tests/test_rate_limits.py packages/tools/tests/test_gateway.py services/event_worker/tests/test_rate_limit_retention.py tests/test_rate_limit_service_boundaries.py -q
uv run python -c 'from jhin_rate_limits import RateLimitScope; from jhin_tools.builtin import ToolExecutionContext; assert RateLimitScope.TOOL.value == "tool"; assert ToolExecutionContext.__dataclass_fields__["admission"].kw_only is True'
uv run pytest services/agent_worker/tests services/tool_worker/tests packages/tools/tests services/event_worker/tests -q
uv run ruff check services/agent_worker services/tool_worker services/event_worker packages/tools tests/test_rate_limit_service_boundaries.py
uv run mypy services/agent_worker/src services/tool_worker/src services/event_worker/src packages/tools/src
```

Expected: PASS. The dependency metadata, lock entry, and runtime imports agree; replayed terminal
identities do not consume again; rate denial causes zero model/connector/sandbox effect.

- [ ] **Step 6: Guard, stage exactly Task 3, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add uv.lock services/agent_worker/pyproject.toml services/agent_worker/src/jhin_agent_worker/settings.py services/agent_worker/src/jhin_agent_worker/resources.py services/agent_worker/src/jhin_agent_worker/reasoning.py services/agent_worker/tests/test_rate_limits.py services/tool_worker/pyproject.toml services/tool_worker/src/jhin_tool_worker/settings.py services/tool_worker/src/jhin_tool_worker/resources.py services/tool_worker/src/jhin_tool_worker/activities.py services/tool_worker/tests/test_rate_limits.py packages/tools/pyproject.toml packages/tools/src/jhin_tools/builtin.py packages/tools/src/jhin_tools/gateway.py packages/tools/tests/test_gateway.py services/event_worker/pyproject.toml services/event_worker/src/jhin_event_worker/retention.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/tests/test_rate_limit_retention.py tests/test_rate_limit_service_boundaries.py
git diff --cached --check
git commit -m "feat: enforce worker effect rate limits"
```

Expected: commit 5 of 15; every model/tool/sandbox call crosses the intended admission boundary.

### Task 4: Fail Closed on Production Security, Proxy Trust, and Maintenance Mode

**Files:**
- Modify: `apps/api/src/jhin_api/settings.py`
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Create: `apps/api/src/jhin_api/security/middleware.py`
- Create: `apps/api/src/jhin_api/security/trusted_proxy.py`
- Create: `apps/api/tests/test_maintenance.py`
- Create: `apps/api/tests/test_production_settings.py`
- Modify: `apps/api/tests/test_security.py`
- Create: `apps/api/tests/test_trusted_proxy.py`
- Create: `apps/web/lib/security-headers.ts`
- Create: `apps/web/proxy.ts`
- Create: `apps/web/tests/security-headers.test.ts`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/settings.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- Create: `tests/test_production_configuration.py`

**Interfaces:**
- Consumes: `APP_ENV`, exact public `APP_URL`, predecessor keyring/provider/OTLP validation, authenticated operator identity, and exact Caddy proxy addresses.
- Produces: `ProductionSecuritySettings`, `TrustedProxyResolver`, API `MaintenanceMiddleware`, shared fail-closed service validators, and Next.js 16 `proxy(request)` with one per-response nonce and closed headers.

- [ ] **Step 1: Write RED validation, proxy-spoofing, maintenance, and CSP tests**

Add table-driven tests for every invalid production combination. Pin these security examples exactly:

```python
def test_untrusted_forwarding_headers_never_change_client_identity() -> None:
    resolver = TrustedProxyResolver.from_addresses(("10.20.0.8",))
    identity = resolver.resolve(
        peer_ip="203.0.113.10",
        headers={
            "forwarded": "for=192.0.2.1;proto=https",
            "x-forwarded-for": "192.0.2.2",
            "x-forwarded-proto": "https",
        },
    )
    assert identity.client_ip == "203.0.113.10"
    assert identity.scheme == "http"


def test_trusted_proxy_uses_first_valid_overwritten_address() -> None:
    resolver = TrustedProxyResolver.from_addresses(("10.20.0.8",))
    identity = resolver.resolve(
        peer_ip="10.20.0.8",
        headers={"x-forwarded-for": "2001:db8::7", "x-forwarded-proto": "https"},
    )
    assert identity.client_ip == "2001:db8::7"
    assert identity.scheme == "https"
```

```typescript
import { describe, expect, it } from "vitest";
import { buildSecurityHeaders } from "../lib/security-headers";

describe("production security headers", () => {
  it("binds a fresh nonce and rejects executable relaxations", () => {
    const headers = buildSecurityHeaders({ nonce: "fixed-test-nonce", production: true });
    const csp = headers.get("content-security-policy");
    expect(csp).toContain("script-src 'self' 'nonce-fixed-test-nonce'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("base-uri 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain("'unsafe-eval'");
    expect(headers.get("strict-transport-security")).toBe("max-age=31536000");
  });
});
```

Run:

```bash
uv run pytest apps/api/tests/test_production_settings.py apps/api/tests/test_trusted_proxy.py apps/api/tests/test_maintenance.py tests/test_production_configuration.py -q
pnpm --filter jhin-web exec vitest run tests/security-headers.test.ts
```

Expected: FAIL because production settings do not reject unsafe combinations, forwarding is not peer-gated, maintenance does not close mutations, and the web proxy/header builder does not exist.

- [ ] **Step 2: Add one shared fail-closed production contract**

In API settings, require all of the following when `APP_ENV=production`: an `https` URL with no userinfo/query/fragment; session cookies fixed to `Secure`, `HttpOnly`, and `SameSite=Lax`; exact single-origin credentialed CORS with the existing CSRF check enabled; nondevelopment database/NATS/Temporal/provider credentials; a valid nonlegacy keyring; explicit trusted-proxy addresses; safe HSTS; and OTLP either disabled or exactly `http://otel-collector:4317`. Resolve configured proxy hostnames once at startup into an immutable nonempty set of exact IPv4/IPv6 addresses. Resolution failure aborts startup; there is no trust-by-subnet or trust-all mode.

The resolved set is intentionally not refreshed inside a request. Initial deployment creates and attaches the stopped Caddy container first so its exact network address exists before API startup, proves that address remains unchanged when Caddy starts, and publishes no traffic until upstream readiness. A later Caddy container/network replacement requires the ordered `proxy-roll` operation to enter and prove API maintenance, recreate/attach Caddy, resolve its new internal address, recreate API with maintenance still enabled, pass readiness, start/validate Caddy, and only then leave maintenance; a standalone proxy recreate is rejected by the operations command. During the short address mismatch, the old API still returns its pre-authentication maintenance 503. Tests change the proxy address, prove the old API ignores the new forwarding headers, then prove the ordered API recreate accepts them.

Each worker and sandbox-runner settings class imports or mirrors the same closed predicates relevant to that process. `tests/test_production_configuration.py` introspects every production settings class and proves that development keys, fake providers, permissive tool allowlists, inline Temporal keys, wildcard origins, nonsecure cookies, and external cleartext telemetry are rejected with stable safe error codes.

- [ ] **Step 3: Install trusted-client and maintenance middleware before routers**

`TrustedProxyResolver.resolve` accepts only canonical single IP values because Caddy overwrites its forwarding headers. Reject comma-separated chains, malformed addresses, non-`http`/`https` schemes, duplicate forwarding headers, or a forwarded host not exactly equal to `APP_URL`'s normalized host/port. Untrusted peers use only the socket IP/scheme and a separately validated exact Host header; they cannot assert the public scheme/host. Store the resolved client IP/scheme/host plus a private trusted marker on request state; never log or attach them as metric labels.

`MaintenanceMiddleware` reads the validated process-start flag. When enabled, it returns this exact response for `POST`, `PUT`, `PATCH`, and `DELETE`, including every webhook route, before authentication or rate-limit mutation:

```python
return JSONResponse(
    status_code=503,
    content={"detail": {"code": "maintenance", "retryable": True}},
    headers={"Retry-After": "60", "Cache-Control": "no-store"},
)
```

`GET /api/v1/health`, `GET /api/v1/health/ready`, and authenticated read-only operator routes remain available and opaque. All authentication, secret-bearing, and mutation responses receive `Cache-Control: no-store`; middleware order is asserted in `test_security.py`.

- [ ] **Step 4: Add the Next.js nonce boundary**

Implement Next.js 16 `proxy.ts` using Web Crypto and pass the nonce in both request and response headers. `buildSecurityHeaders` returns CSP, HSTS, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and `Permissions-Policy: camera=(), microphone=(), geolocation=()`. The proxy adds `Cache-Control: no-store` to authentication routes and every response whose request carries the validated session-cookie name; public static assets retain framework cache policy. Production refuses a CSP containing `unsafe-eval`, HSTS preload, or an HSTS value other than the two permitted exact strings. Tests mock only `crypto.getRandomValues`, never a hard-coded production nonce.

- [ ] **Step 5: Run GREEN and the affected security suites**

```bash
uv run pytest apps/api/tests/test_production_settings.py apps/api/tests/test_trusted_proxy.py apps/api/tests/test_maintenance.py apps/api/tests/test_security.py tests/test_production_configuration.py -q
pnpm --filter jhin-web exec vitest run tests/security-headers.test.ts
uv run pytest apps/api/tests services/agent_worker/tests services/event_worker/tests services/tool_worker/tests services/workflow_worker/tests -q
uv run ruff check apps/api services tests/test_production_configuration.py
uv run mypy apps/api/src services/agent_worker/src services/event_worker/src services/tool_worker/src services/workflow_worker/src services/sandbox_runner/src
pnpm --filter jhin-web lint
```

Expected: PASS. Spoofed headers cannot affect login subjects, production misconfiguration aborts startup, and maintenance rejects writes without durable side effects.

- [ ] **Step 6: Guard, stage exactly Task 4, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add apps/api/src/jhin_api/settings.py apps/api/src/jhin_api/deps.py apps/api/src/jhin_api/main.py apps/api/src/jhin_api/security/middleware.py apps/api/src/jhin_api/security/trusted_proxy.py apps/api/tests/test_maintenance.py apps/api/tests/test_production_settings.py apps/api/tests/test_security.py apps/api/tests/test_trusted_proxy.py apps/web/lib/security-headers.ts apps/web/proxy.ts apps/web/tests/security-headers.test.ts services/agent_worker/src/jhin_agent_worker/settings.py services/event_worker/src/jhin_event_worker/settings.py services/tool_worker/src/jhin_tool_worker/settings.py services/workflow_worker/src/jhin_workflow_worker/settings.py services/sandbox_runner/src/jhin_sandbox_runner/settings.py tests/test_production_configuration.py
git diff --cached --check
git commit -m "feat: harden production request boundaries"
```

Expected: commit 6 of 15; security behavior is testable without starting Compose.

### Task 5: Harden Production Compose, Caddy, Ports, Resources, and Logs

**Files:**
- Modify: `.env.example`
- Modify: `Makefile`
- Modify: `compose.yaml`
- Modify: `compose.dev.yaml`
- Create: `compose.operations.yaml`
- Modify: `compose.rootful.yaml`
- Modify: `compose.rootless.yaml`
- Create: `docker/operations.Dockerfile`
- Create: `ops/caddy/Caddyfile`
- Create: `ops/caddy/Caddyfile.test`
- Create: `scripts/phase10_compose.py`
- Create: `scripts/assert_phase10_production_compose.py`
- Create: `scripts/assert_phase10_command_inventory.py`
- Create: `tests/test_phase10_compose.py`
- Create: `tests/test_phase10_production_compose.py`
- Create: `tests/test_phase10_command_inventory.py`
- Create: `tests/integration/test_phase10_proxy_security.py`
- Create: `tests/integration/test_phase10_maintenance.py`
- Modify: `packages/db/src/jhin_db/engine.py`
- Create: `packages/db/tests/test_engine.py`
- Modify: `apps/api/src/jhin_api/settings.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- Modify: `services/sandbox_runner/tests/test_job_config.py`

**Interfaces:**
- Consumes: Task 4 headers/maintenance behavior, current Compose services and health checks, Prerequisite P0's corrected rootless adapter, Docker Engine API authority, predecessor sandbox job create payload, and predecessor observability/health overlays.
- Produces: one production Caddy entry point, explicit PostgreSQL `17.11-alpine` and Temporal server/admin-tools topology, collision-free dev/admin overlays, rootful socket and private rootless transport operations modes, exact writer identities, service resource/log policy, operations image command inventory, and `IsolatedComposeProject`.

- [ ] **Step 1: Write RED static Compose, Caddy, socket, and command-inventory tests**

The static validator must parse merged Compose models through `docker compose config --format json`; it must not grep YAML. It also requires every base and optional-profile service to retain a meaningful process/native readiness healthcheck (one-shot schema tooling instead requires `restart: "no"` and bounded completion), limits persistent product volumes to PostgreSQL/NATS/provider-declared object storage plus Temporal only when configured outside PostgreSQL, classifies monitoring volumes as replaceable diagnostics, and reasserts the predecessor agent/tool/runner network and secret boundaries after every merge. It requires PostgreSQL `17.11-alpine` or a later policy-approved 17 patch, exact digest syntax, `temporalio/server:1.29.7` and matching `temporalio/admin-tools:1.29.7` digest identities, and zero `temporalio/auto-setup` reference in the rendered model. Engine tests inspect SQLAlchemy pool arguments, closed `application_name` values, and asyncpg connection options rather than opening fake connections. Assert:

```python
def test_production_publishes_only_caddy(compose_model: dict[str, object]) -> None:
    services = compose_model["services"]
    published = {
        name: service.get("ports", []) for name, service in services.items() if service.get("ports")
    }
    assert set(published) == {"caddy"}
    assert {entry["target"] for entry in published["caddy"]} == {80, 443}


def test_every_long_lived_service_is_bounded(compose_model: dict[str, object]) -> None:
    for name, service in compose_model["services"].items():
        assert service["deploy"]["resources"]["limits"]["cpus"]
        assert service["deploy"]["resources"]["limits"]["memory"].endswith(("M", "G"))
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-file": "5", "max-size": "20m"},
        }, name
```

The Caddy parser/test adapter asserts `/api/*` routes only to API, all other routes only to web, Collector metrics and internal admin/readiness ports have no proxy route, forwarded headers are overwritten, upstream ports are internal, response headers match Task 4, and test TLS uses generated ephemeral certificates only. `services/sandbox_runner/tests/test_job_config.py` asserts every ephemeral job's Engine `HostConfig.LogConfig` is exactly `{"Type": "json-file", "Config": {"max-size": "20m", "max-file": "5"}}`; the hardening must not change its existing authority, networks, mounts, user, or resource caps. The command inventory builds `docker/operations.Dockerfile` and executes `--version`/`command -v` for `bash`, `python`, `git`, `rg`, `find`, `stat`, `mktemp`, `age`, `tar`, `zstd`, `sha256sum`, `pg_dump`, `pg_dumpall`, `pg_restore`, `psql`, `nats`, `temporal`, `docker`, and the Compose plugin. It executes `psql -X --version` and separately runs `temporal-sql-tool --help` inside the pinned admin-tools service. It also runs a private-file round trip proving the installed GNU tar/coreutils/findutils support exact `--sort=name`, `--mtime=@0`, `--numeric-owner`, `--zstd`, `stat -c`, `sha256sum`, and `find -delete` forms used later; a same-name BusyBox fallback is a failure.

The same tests inspect both Docker modes. Rootless must contain the predecessor `rootless-docker-transport` for sandbox-runner plus a separate `rootless-operations-transport`; each adapter alone bind-mounts the host socket, and each joins a different internal engine network. Sandbox-runner and operations remain `10001:10001`, have no socket bind/group/privilege, and receive only their exact private HTTP URL. Rootful retains the exact socket-GID contract. A static or live model containing rootless direct socket access, `user: "0:0"` on either consumer, shared adapter network, host networking, privileged mode, added capability, published adapter port, or daemon permission mutation fails.

Run:

```bash
uv run pytest tests/test_phase10_compose.py tests/test_phase10_production_compose.py tests/test_phase10_command_inventory.py packages/db/tests/test_engine.py services/sandbox_runner/tests/test_job_config.py -q
```

Expected: FAIL because overlays, Caddy configuration, operations image, and harness do not exist, production still publishes internal services, and sandbox jobs do not carry an explicit bounded log configuration.

- [ ] **Step 2: Split production from loopback-only development/admin exposure**

Make `compose.yaml` the production base. It publishes Caddy `80`/`443` only, uses internal networks for data/control/observability, and does not expose Caddy's admin endpoint. Move developer ports to `compose.dev.yaml`, bound only to `127.0.0.1` and variables whose test defaults are `0`; no literal host port is shared with live gates.

Introduce validated digest-only `*_IMAGE` slots for every production image; Task 5's isolated harness resolves its exact test images to private immutable digests before rendering, while Task 12 later commits the reviewed two-architecture `ops/images/resolved-images.json`. PostgreSQL's human-readable source tag is at least `17.11-alpine`, and its resolved child must be freshly recorded; `17.10`/older fails. A tag-only production render fails in both tasks. Set `read_only: true`, `tmpfs`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, and a nonroot user where the image supports it. Document and narrowly test unavoidable write/capability exceptions for PostgreSQL, NATS, Temporal, and Caddy data volumes.

Use this exact small-profile reservation/limit/PID table; values are CPU cores and MiB, and Temporal UI is disabled unless the private admin profile is selected:

| Service | CPU reserve / limit | RAM reserve / limit | PID limit |
| --- | ---: | ---: | ---: |
| Caddy | 0.10 / 0.25 | 128 / 256 | 128 |
| web | 0.25 / 0.50 | 256 / 512 | 256 |
| API | 0.50 / 1.00 | 512 / 1024 | 256 |
| workflow-worker | 0.25 / 0.50 | 256 / 512 | 256 |
| event-worker | 0.25 / 0.50 | 256 / 512 | 256 |
| agent-worker | 0.75 / 1.25 | 768 / 1280 | 256 |
| tool-worker | 0.50 / 1.00 | 512 / 1024 | 256 |
| sandbox-runner | 0.10 / 0.25 | 128 / 256 | 256 |
| PostgreSQL | 0.75 / 1.50 | 1536 / 2560 | 512 |
| NATS | 0.25 / 0.50 | 512 / 768 | 256 |
| Temporal | 0.50 / 1.00 | 1024 / 1536 | 512 |
| Temporal UI, private profile | 0.10 / 0.25 | 128 / 256 | 128 |

Without UI, reservations plus one current hard-capped 2-CPU/4096-MiB sandbox are 6.20 CPU and 9,984 MiB, below 80% of the 8-vCPU/16-GiB host. The validator recomputes rather than trusting those totals and rejects a higher sandbox cap/concurrency unless headroom still passes. Set every Compose container's JSON logs to `20m` x 5. Add that identical bound to the existing sandbox-runner job create payload so ephemeral jobs cannot inherit an unbounded daemon default; do not otherwise alter predecessor-owned job behavior. PostgreSQL, NATS, and Temporal log to stderr only, so no unbounded second on-volume log set exists.

Set bundled PostgreSQL `max_connections=150`. Use shared engine defaults `pool_pre_ping=true`, `pool_use_lifo=true`, `pool_timeout=30`, `pool_recycle=1800`, and server `idle_in_transaction_session_timeout=30000ms`. Small-profile pool size/overflow pairs are API `5/5`, agent-worker `5/3`, tool-worker `5/3`, and event-worker `3/2`; reserve 20 Temporal connections and 10 migration/backup/operator connections. The rendered worst-case total is 61, below the 105-connection alert boundary. The validator rejects a profile unless `sum(replicas * (pool_size + max_overflow)) + temporal + operator_reserve < floor(max_connections * 0.70)`. External transaction pooler mode is explicit and sets asyncpg's prepared-statement cache size to zero; direct mode keeps driver defaults. Pool checkout failure is a bounded safe 503/retryable activity error, never an unbounded wait.

Every SQLAlchemy engine sets a closed `application_name` of `jhin-api`, `jhin-agent-worker`, `jhin-tool-worker`, `jhin-event-worker`, `jhin-workflow-worker`, or an exact operations command name; arbitrary caller values fail validation. Compose labels every product writer service with `com.jhin.writer-class` from the closed set `api`, `worker`, `temporal`, `migration`, `rotation`, or `backup`. The bundled Temporal SQL configuration sets an exact Temporal application name. Tasks 7–10 consume this closed inventory to prove no writer exists during a backup or restore boundary.

Use pinned `temporalio/server:1.29.7` for the long-running server and `temporalio/admin-tools:1.29.7` for a profile-gated one-shot `temporal-admin-tools` service. The server entry point receives no `autosetup` argument, sets `SKIP_SCHEMA_SETUP=true`, has no schema-admin credentials, and only checks compatible history/visibility schema before serving. The admin service runs as UID 10001 with a read-only root, tmpfs, all capabilities dropped, and no port, restart, health authority, or product-worker network; it is the only container permitted to run `temporal-sql-tool` and uses a mounted mode-`0400` `PGPASSFILE`, never `--pw`, for the separate schema-admin role. Initial deployment explicitly runs, in order, `create`, `setup-schema -v 0.0`, and `update-schema -d /etc/temporal/schema/postgresql/v12/{temporal,visibility}/versioned` for the two databases before starting the server. Existing deployments run only the two idempotent version-aware `update-schema` commands after a verified backup and maintenance. Static tests reject server start before that one-shot command succeeds, runtime-role DDL, admin-tools remaining alive, and any startup-created schema object.

- [ ] **Step 3: Implement exact Caddy and maintenance routing**

`ops/caddy/Caddyfile` obtains publicly trusted certificates for the configured hostname, permits only TLS 1.2/1.3, keeps automatic HTTP-to-HTTPS redirect, does not log headers/cookies/query strings, and overwrites proxy headers:

```caddyfile
{
  admin off
  servers {
    timeouts {
      read_body 30s
      idle 60s
    }
  }
}

{$APP_HOSTNAME} {
  tls {
    protocols tls1.2 tls1.3
  }
  header {
    Strict-Transport-Security "{$HSTS_VALUE}"
    X-Content-Type-Options "nosniff"
    Referrer-Policy "same-origin"
    Permissions-Policy "camera=(), microphone=(), geolocation=()"
  }

  @sensitive_connections path /api/v1/workspaces/*/connections*
  handle @sensitive_connections {
    request_body {
      max_size 65536
    }
    reverse_proxy api:8000 {
      header_up X-Forwarded-For {http.request.remote.host}
      header_up X-Forwarded-Proto {http.request.scheme}
      header_up X-Forwarded-Host {http.request.host}
      header_up -Forwarded
      lb_try_duration 0s
      fail_duration 30s
      max_fails 2
      flush_interval -1
      transport http {
        dial_timeout 5s
        response_header_timeout 30s
      }
    }
  }

  @api path /api/*
  handle @api {
    request_body {
      max_size 1048576
    }
    reverse_proxy api:8000 {
      header_up X-Forwarded-For {http.request.remote.host}
      header_up X-Forwarded-Proto {http.request.scheme}
      header_up X-Forwarded-Host {http.request.host}
      header_up -Forwarded
      lb_try_duration 0s
      fail_duration 30s
      max_fails 2
      flush_interval -1
      transport http {
        dial_timeout 5s
        response_header_timeout 30s
      }
    }
  }

  handle {
    request_body {
      max_size 1048576
    }
    reverse_proxy web:3000 {
      header_up X-Forwarded-For {http.request.remote.host}
      header_up X-Forwarded-Proto {http.request.scheme}
      header_up X-Forwarded-Host {http.request.host}
      header_up -Forwarded
      lb_try_duration 0s
      fail_duration 30s
      max_fails 2
      flush_interval -1
      transport http {
        dial_timeout 5s
        response_header_timeout 30s
      }
    }
  }
}
```

The API route matcher precedes the web fallback. Enforce 65,536 bytes on sensitive connection-management requests and 1,048,576 bytes on webhooks, other API bodies, and web-origin bodies, matching the application readers; reject larger/chunked bodies as 413 without upstream work. Use a 5-second upstream dial timeout, 30-second response-header timeout, 30-second request-body read timeout, and 60-second idle timeout. Preserve streaming flush and WebSocket upgrade behavior without buffering, but configure no current public admin/metrics stream. Do not automatically retry `POST`, `PUT`, `PATCH`, or `DELETE`. Caddy sets transport-wide HSTS/nosniff/referrer/permissions headers but never synthesizes or duplicates the nonce CSP emitted by Next; tests prove the unique nonce arrives intact. `Caddyfile.test` imports the same route/header/limit snippets but uses a test-only local CA and dynamic loopback binding.

- [ ] **Step 4: Build rootful/rootless operations overlays and the isolated harness**

`compose.operations.yaml` adds one operations consumer on internal product networks. Rootful mode snapshots `/var/run/docker.sock`, requires an exact positive numeric `PHASE10_DOCKER_SOCKET_GID`, mounts the socket without changing ownership/mode, and adds exactly that GID. Rootless mode requires an already-running host socket owned by UID 10001 at an absolute `PHASE10_ROOTLESS_DOCKER_SOCKET`, but mounts it only into `rootless-operations-transport`. That adapter reuses Prerequisite P0's fixed relay entry point, runs as UID 0 only inside the verified rootless user namespace, is unprivileged/read-only/cap-drop-ALL/no-new-privileges, and joins only `operations-engine`. Operations remains `10001:10001`, has no socket/group, joins `operations-engine`, depends on the adapter's bounded Docker `/_ping` healthcheck, and sets Docker CLI's exact `DOCKER_HOST=tcp://rootless-operations-transport:2375`. It cannot reach the sandbox adapter's distinct `engine` network. Both adapters have fixed upstream/listen values and no public port.

Preserve the corrected predecessor sandbox-runner contract. From one re-statted authority snapshot, rootful harnesses set both `PHASE10_DOCKER_SOCKET_GID` and `SANDBOX_DOCKER_GID` to the same exact GID and set `SANDBOX_DOCKER_SOCKET_HOST=/var/run/docker.sock`; rootless harnesses remove both GID variables, pass `PHASE10_ROOTLESS_DOCKER_SOCKET` only as the two adapters' bind source, and require the exact private URLs. Caller-provided conflicting socket/GID/URL values are rejected, not inherited. Neither socket nor adapter network enters a sandbox job container.

Implement `IsolatedComposeProject` with `subprocess.run(shell=False, check=True, capture_output=True, text=True)`. It creates a project matching `phase10-[a-z0-9-]{1,24}-[0-9a-f]{12}`, private `DOCKER_CONFIG`, generated nonrepository env file, dynamic loopback ports, unique Temporal namespace, and unique synthetic keyring. Its inherited-environment allowlist is `PATH`, `HOME`, `TMPDIR`, and the validated host socket authority only; it forces `COMPOSE_DISABLE_ENV_FILE=1`. In rootless mode the host process uses the validated Unix socket only to bootstrap/inspect/tear down the private transports; all in-project Docker commands use the operations transport. Before starting any service it verifies host socket owner/inode/mode, daemon `name=rootless`, `net.ipv4.ip_unprivileged_port_start <= 80`, and real cgroup-v2 delegation by launching a disposable UID-10001/cap-drop-ALL port-80 probe plus an exact `64m`, `0.25 CPU`, `32 PID` probe that reads `memory.max`, `cpu.max`, and `pids.max`. It changes no sysctl, cgroup, ownership, or permission and fails closed if any value is unsupported or unenforced. Unit tests cover the optional paired `resolved_images`/`runtime_image_env` contract with synthetic exact IDs, mode checks, closed keys, daemon lookup, fingerprint match, and hostile inherited/caller image variables; this forward-compatible seam is dormant until Task 12 creates its sole generator. Teardown invokes the fully materialized argv `docker compose -p PROJECT -f FILE down --volumes --remove-orphans --timeout 30` with a 60-second process timeout, then a project-label-filtered kill followed by one bounded down retry. It validates labels before removing any resource and re-stats the rootless socket unchanged.

- [ ] **Step 5: Run GREEN, static inspection, and bounded live proxy checks in both modes**

```bash
uv run pytest tests/test_phase10_compose.py tests/test_phase10_production_compose.py tests/test_phase10_command_inventory.py packages/db/tests/test_engine.py services/sandbox_runner/tests/test_job_config.py -q
uv run python scripts/assert_phase10_production_compose.py
uv run python scripts/assert_phase10_command_inventory.py
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run pytest tests/integration/test_phase10_proxy_security.py tests/integration/test_phase10_maintenance.py -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest tests/integration/test_phase10_proxy_security.py tests/integration/test_phase10_maintenance.py -q
uv run ruff check packages/db/src/jhin_db/engine.py packages/db/tests/test_engine.py services/sandbox_runner/src/jhin_sandbox_runner/jobs.py services/sandbox_runner/src/jhin_sandbox_runner/settings.py services/sandbox_runner/tests/test_job_config.py scripts/phase10_compose.py scripts/assert_phase10_production_compose.py scripts/assert_phase10_command_inventory.py tests/test_phase10_compose.py tests/test_phase10_production_compose.py tests/test_phase10_command_inventory.py
uv run mypy packages/db/src/jhin_db/engine.py services/sandbox_runner/src/jhin_sandbox_runner/jobs.py services/sandbox_runner/src/jhin_sandbox_runner/settings.py scripts/phase10_compose.py scripts/assert_phase10_production_compose.py scripts/assert_phase10_command_inventory.py
```

Expected: PASS on hosts providing both documented socket modes. `assert_phase10_production_compose.py` renders base, rootful, and rootless with privately resolved test digests in Task 5 and the checked-in Task 12 digest set once present. Each live invocation creates and tears down its own project; skipped mode is a failure in release CI, not a pass.

- [ ] **Step 6: Guard, stage exactly Task 5, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add .env.example Makefile compose.yaml compose.dev.yaml compose.operations.yaml compose.rootful.yaml compose.rootless.yaml docker/operations.Dockerfile ops/caddy/Caddyfile ops/caddy/Caddyfile.test scripts/phase10_compose.py scripts/assert_phase10_production_compose.py scripts/assert_phase10_command_inventory.py tests/test_phase10_compose.py tests/test_phase10_production_compose.py tests/test_phase10_command_inventory.py tests/integration/test_phase10_proxy_security.py tests/integration/test_phase10_maintenance.py packages/db/src/jhin_db/engine.py packages/db/tests/test_engine.py apps/api/src/jhin_api/settings.py apps/api/src/jhin_api/main.py services/agent_worker/src/jhin_agent_worker/settings.py services/agent_worker/src/jhin_agent_worker/resources.py services/tool_worker/src/jhin_tool_worker/settings.py services/tool_worker/src/jhin_tool_worker/resources.py services/event_worker/src/jhin_event_worker/settings.py services/event_worker/src/jhin_event_worker/main.py services/sandbox_runner/src/jhin_sandbox_runner/jobs.py services/sandbox_runner/src/jhin_sandbox_runner/settings.py services/sandbox_runner/tests/test_job_config.py
git diff --cached --check
git commit -m "ops: harden production compose boundary"
```

Expected: commit 7 of 15; production has one network entry point and both operator Docker authority modes are explicit.

### Task 6: Prove Revision `0018` and Token Buckets Across Physical WAL Replication

**Files:**
- Modify: `Makefile`
- Modify: `.github/workflows/ci.yml`
- Create: `compose.phase10-rate-limit-test.yaml`
- Create: `scripts/run_phase10_rate_limit.py`
- Create: `scripts/record_phase10_rate_limit_evidence.py`
- Create: `tests/test_phase10_rate_limit_harness.py`
- Create: `tests/integration/phase10_rate_limit_harness.py`
- Modify: `tests/integration/test_phase10_rate_limit_migration.py`
- Create: `tests/integration/test_phase10_rate_limit_replication.py`
- Create: `docs/operations/rate-limits.md`
- Create: `docs/evidence/phase10-rate-limits.json`

**Interfaces:**
- Consumes: migration `0018`, `PostgresRateLimiter`, Task 5 isolated project harness, freshly resolved PostgreSQL `17.11-alpine` image authority, and real PostgreSQL physical streaming replication.
- Produces: `phase10-rate-limit` bounded drill, PostgreSQL-17.11 primary/standby WAL proof, downgrade/re-upgrade proof, replica-safe contention evidence, and a public redacted evidence schema aggregating both socket modes.

- [ ] **Step 1: Write RED harness, replication, evidence, and runbook contracts**

Unit-test the runner command graph and safe evidence before starting Docker. The integration test must verify actual recovery state and WAL positions, not merely two database URLs:

```python
primary_lsn = await primary.scalar(text("SELECT pg_current_wal_flush_lsn()"))
assert await standby.scalar(text("SELECT pg_is_in_recovery()")) is True
await wait_until(
    lambda: standby.scalar(
        text("SELECT pg_last_wal_replay_lsn() >= CAST(:lsn AS pg_lsn)"),
        {"lsn": str(primary_lsn)},
    ),
    timeout_seconds=30,
)
assert (
    await standby.scalar(text("SELECT to_regclass('public.rate_limit_bucket')"))
    == "rate_limit_bucket"
)
```

Also assert primary and standby system identifiers match, standby writes fail with SQLSTATE `25006`, the application DSN resolves only to primary, and evidence rejects DSNs, IPs, container/volume names, raw digests, subject hashes, and absolute paths.

Run:

```bash
uv run pytest tests/test_phase10_rate_limit_harness.py tests/integration/test_phase10_rate_limit_replication.py -q
```

Expected: FAIL because the isolated primary/standby topology, runner, and evidence recorder do not exist.

- [ ] **Step 2: Add a genuine streaming-replica test topology**

`compose.phase10-rate-limit-test.yaml` uses the same freshly resolved PostgreSQL 17.11 manifest child from Task 5's private resolver for two containers on project-derived volumes; the runner rejects 17.10/older or unequal primary/standby content before volume creation. The primary initializes one test-only replication role from an ephemeral secret file; `pg_basebackup -R` seeds the standby over the private project network. Neither database publishes a fixed host port. The harness waits for streaming state in `pg_stat_replication`, applies `0018` only to the primary, captures the primary flush LSN, and waits for replay to that LSN. Replication credentials and `pg_stat_replication.client_addr` never leave the harness.

- [ ] **Step 3: Exercise migration reversibility and cross-replica concurrency**

Within one isolated project, execute this ordered drill:

1. Assert head `0017`, upgrade to `0018`, and wait for the standby replay LSN.
2. Run four independent API/worker limiter processes against the primary for the same synthetic digest and a capacity of 37; exactly 37 of 400 attempts pass.
3. Wait for WAL replay; query only counts, min/max token bounds, and constraints from the standby. Prove no digest is emitted.
4. Assert direct standby consumption fails read-only and the limiter does not retry there.
5. Downgrade to `0017`, wait until the standby table disappears, re-upgrade to `0018`, and prove a new bucket decision replicates.
6. Kill/restart one client during contention and prove the total does not exceed capacity and no transaction remains `idle in transaction`.

All waits use monotonic deadlines no longer than 60 seconds. The runner's `finally` block tears down volumes in both success and failure.

- [ ] **Step 4: Record only allowlisted public evidence and write the operator runbook**

`record_phase10_rate_limit_evidence.py` accepts the harness's in-memory result object, schema-validates it, and atomically writes:

```json
{
  "schema_version": 1,
  "socket_modes": {"rootful": "pass", "rootless": "pass"},
  "runtime": {"image_set_sha256": "79545f0a05813196ecf3721fa156d5e4c87b4c493bc8f33309724e9cff0e5ab6"},
  "postgres": {"major": 17, "minimum_patch": 11, "primary_standby_digest_equal": true},
  "migration": {"from": "0017", "to": "0018", "downgrade": true, "reupgrade": true},
  "replication": {"physical": true, "standby_read_only": true, "ddl_replayed": true},
  "contention": {"attempts": 400, "capacity": 37, "allowed": 37, "oversubscribed": false},
  "redaction": {"forbidden_fields": 0},
  "status": "pass"
}
```

Each drill invocation writes one schema-valid shard containing exactly one closed `socket_mode` and the harness's canonical runtime-content set hash. `record_phase10_rate_limit_evidence.py aggregate` accepts exactly one rootful and one rootless shard, requires all non-mode results and runtime hash equal, rejects duplicates/unknown modes, and atomically emits the aggregate above. The displayed hash is the exact SHA-256 of schema-fixture bytes `phase10-test-runtime-v1\n`; live Tasks 6 and 13 overwrite it with the measured set. The runbook documents config keys/defaults, normalization, denial semantics, metric/alert, retention, and recovery from lock timeout without bypassing the limiter. `run_phase10_rate_limit.py digest`, `inspect`, and `reset` are real host-operator subcommands: `digest` reads raw subject parts from protected stdin and emits one 64-lowercase-hex digest; `inspect`/`reset` read that digest from protected stdin, so neither form enters argv/environment. `inspect` emits only scope/allowed/remaining/retry; `reset` requires `--confirm-reset`, uses the same bounded one-row transaction, emits safe structured event `rate_limit.reset` with scope/count only, and prints only `reset: 0|1`. Unit tests capture argv/environment/stdout/stderr and reject raw values or use outside an authenticated operations container.

- [ ] **Step 5: Run GREEN and both mandatory socket modes**

```bash
uv run pytest tests/test_phase10_rate_limit_harness.py tests/integration/test_phase10_rate_limit_migration.py tests/integration/test_phase10_rate_limit_replication.py -q
phase10_rate_tmp="$(mktemp -d)"
trap 'find "$phase10_rate_tmp" -type f -delete; rmdir "$phase10_rate_tmp"' EXIT
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/run_phase10_rate_limit.py --evidence "$phase10_rate_tmp/rootful.json"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_rate_limit.py --evidence "$phase10_rate_tmp/rootless.json"
uv run python scripts/record_phase10_rate_limit_evidence.py aggregate --rootful "$phase10_rate_tmp/rootful.json" --rootless "$phase10_rate_tmp/rootless.json" --output docs/evidence/phase10-rate-limits.json
uv run python scripts/record_phase10_rate_limit_evidence.py --check docs/evidence/phase10-rate-limits.json
uv run ruff check scripts/run_phase10_rate_limit.py scripts/record_phase10_rate_limit_evidence.py tests/test_phase10_rate_limit_harness.py tests/integration/phase10_rate_limit_harness.py tests/integration/test_phase10_rate_limit_migration.py tests/integration/test_phase10_rate_limit_replication.py
uv run mypy scripts/run_phase10_rate_limit.py scripts/record_phase10_rate_limit_evidence.py tests/integration/phase10_rate_limit_harness.py
```

Expected: PASS twice with real WAL replay and one deterministic aggregate proving both modes. CI retains neither project's raw logs nor volumes.

- [ ] **Step 6: Guard, stage exactly Task 6, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add Makefile .github/workflows/ci.yml compose.phase10-rate-limit-test.yaml scripts/run_phase10_rate_limit.py scripts/record_phase10_rate_limit_evidence.py tests/test_phase10_rate_limit_harness.py tests/integration/phase10_rate_limit_harness.py tests/integration/test_phase10_rate_limit_migration.py tests/integration/test_phase10_rate_limit_replication.py docs/operations/rate-limits.md docs/evidence/phase10-rate-limits.json
git diff --cached --check
git commit -m "test: prove rate limits across wal replication"
```

Expected: commit 8 of 15; migration revision/down-revision, physical replay, concurrency, rollback, and redaction are reviewable in one commit.

### Task 7: Create Complete Encrypted Maintenance-Window Backups and Retention

**Files:**
- Modify: `Makefile`
- Create: `compose.phase10-backup-test.yaml`
- Create: `scripts/phase10_backup_manifest.py`
- Create: `scripts/phase10_backup.py`
- Create: `scripts/phase10_prune_backups.py`
- Create: `tests/test_phase10_backup.py`
- Create: `tests/integration/test_phase10_backup.py`
- Create: `docs/operations/backup.md`
- Create: `docs/evidence/phase10-backup.json`

**Interfaces:**
- Consumes: Task 5 operations image/harness, writer labels/application names and maintenance endpoint, PostgreSQL globals and all non-template databases, stopped JetStream volume, predecessor `PostgresRotationLease`/active-state/retirement-fence authority, versioned keyring, operator configuration, three nonoverlapping age recipient files, three separately supplied verifier identity classes, and pinned image/version metadata.
- Produces: `BackupManifest`, private `QuiescenceProof`, `restore_globals_into_scratch`, `phase10_backup create|verify|resume`, a manifest-bound verification receipt, minimum `7 daily / 4 weekly / 12 monthly` pruning, separately encrypted state/config/key artifacts, and secret-free dual-mode RPO/duration/integrity evidence.

- [ ] **Step 1: Write RED manifest, subprocess, redaction, retention, and live-backup tests**

Use fake executables in unit tests to record argv/stdin/stdout wiring without receiving real secrets. Assert there is no `shell=True`, plaintext temporary file, secret-bearing environment key, or decryption output capture. Prove `create` rejects identity inputs, `verify` accepts identities only through fixed mounts/descriptors, and `resume` rejects a missing, failed, stale, or manifest-mismatched receipt. The manifest test fixes canonical serialization:

```python
def test_public_manifest_is_canonical_and_value_free(sample_manifest: BackupManifest) -> None:
    encoded = sample_manifest.canonical_json()
    assert encoded.endswith(b"\n")
    assert (
        encoded
        == json.dumps(json.loads(encoded), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    forbidden = (b"postgresql://", b"AGE-SECRET-KEY", b"password", b"/tmp/", b"workspace_id")
    assert all(value not in encoded for value in forbidden)


def test_retention_selects_minimum_generations() -> None:
    selected = retention_set(daily_fixture(now=UTC_NOW))
    assert bucket_counts(selected) == {"daily": 7, "weekly": 4, "monthly": 12}


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("rogue-writer-session-before-dump", "quiescence_lost"),
        ("writer-service-restarted", "quiescence_lost"),
        ("prepared-transaction", "quiescence_lost"),
        ("nats-unclean-exit", "nats_not_quiescent"),
    ],
)
def test_backup_aborts_before_artifact_acceptance_when_quiescence_breaks(
    backup_fixture: BackupFixture,
    fault: str,
    code: str,
) -> None:
    result = backup_fixture.create_with_fault(fault)
    assert result.code == code
    assert result.complete_marker is False
    assert result.resume_authorized is False
```

The live test seeds all non-template databases, a Temporal workflow with a pending timer, one NATS stream/consumer with retained and pending messages, one encrypted credential, and a synthetic keyring. It captures authoritative counts before maintenance, then asserts the backup covers globals, every database count, NATS, config, and keyring without exposing names or values publicly. A second process holding the predecessor rotation advisory lease, any active rotation row, or any armed retirement fence must fail preflight with `key_rotation_busy` before maintenance or artifact creation.

Run:

```bash
uv run pytest tests/test_phase10_backup.py tests/integration/test_phase10_backup.py -q
```

Expected: FAIL because the manifest, orchestrator, retention command, topology, runbook, and evidence do not exist.

- [ ] **Step 2: Implement strict preflight and quiescence ordering**

`phase10_backup.py create` requires absolute owner-readable recipient files for three distinct recipient classes, an empty owner-only destination, at least 20% free destination space after a conservative estimate, a validated project, and matching healthy image/version metadata. Production mode requires PostgreSQL 17 patch >= 11 and Alembic `0018`, and persists the exact fresh resolved digest privately plus safe version/head publicly; 17.10/older or a different head aborts before maintenance. Isolated Task 10 pre-upgrade mode accepts exact head `0017`; its PostgreSQL component-major source mode additionally accepts exactly the matrix-resolved PostgreSQL 16 patch >= 15. Those compatibility purposes are private, validated against the closed command graph, and cannot target a production project. It never accepts an age identity. Production destination is rooted at the separately mounted `/mnt/jhin-backups`, must be its own mount with a different device from repository/Docker product data, and must contain the root-owned mode-`0444` marker `.jhin-off-host-backup-target`; only `drill` accepts a generated disposable local target. It refuses symlinks, FIFOs as input, recipient reuse between classes, inline recipient/key values, or an existing incomplete destination directory. The bundled topology permits only `pg_default` and `pg_global`; a nondefault tablespace fails preflight with `unsupported_tablespace` rather than producing a dump whose fresh host cannot recreate its storage. Before entering maintenance, acquire the predecessor's exact session advisory lease on one dedicated nonpooled connection, use a bounded lease transaction to reject every active rotation or armed retirement fence, and keep the session lease—but no open transaction—until all database/key artifacts and manifest checks finish. Release and dispose it in `finally` on success or failure; a busy/lost lease aborts with `key_rotation_busy` and never races rotation.

Execute this exact state transition with a monotonic deadline per step:

1. Record backup start time; resolve the rendered Compose model into a closed inventory of every service whose `com.jhin.writer-class` is `api`, `worker`, `temporal`, `migration`, `rotation`, or `backup`; and resolve the closed PostgreSQL role/application-name pairs. Reject a missing known writer, an unknown label/value, a published PostgreSQL/NATS/Temporal port, or a session identity not represented by that inventory.
2. Enable maintenance by recreating every API replica with the validated flag. Prove a synthetic mutation and webhook return 503 with `Retry-After: 60`; wait for API and worker in-flight gauges to reach zero at their safe boundaries.
3. Stop every API replica, agent-worker, tool-worker, workflow-worker, event-worker, and sandbox-runner. Verify every inventoried application/worker container is stopped, no queue poller remains, Caddy cannot reach an API writer, and the persisted maintenance setting still prevents a writer from being restarted unguarded.
4. Stop bundled Temporal server/UI and prove all Temporal runtime sessions have exited. Reject a running `temporal-admin-tools`, migration, seed, rotation, or other one-shot writer container.
5. Cleanly drain/flush and stop NATS, require exit code zero, record the server's last acknowledged JetStream state counters in the private proof, and prove no NATS listener or producer remains before accessing its project-labelled volume.
6. Keep PostgreSQL healthy. From the cluster primary, take two bounded `pg_stat_activity` snapshots separated by a fresh transaction and require that the only non-background sessions are the exact dedicated rotation-lease session plus the exact read-only backup sessions. Require zero prepared transactions and no application/Temporal/migration/seed/rotation writer session. Capture `pg_current_wal_flush_lsn()` as the private start fence, run all logical dumps, capture the private end fence, and require `end >= start`; PostgreSQL background/checkpoint WAL may advance and is not interpreted as a product mutation. Reassert the advisory lease, active/fence rows, stopped-container inventory, zero unexpected sessions, zero prepared transactions, NATS stopped state, and JetStream archive hash immediately after the dumps. Store the two LSNs only in the encrypted database index.

`QuiescenceProof` is valid only when all six checks pass under one capture UUID: maintenance was proven before API shutdown, all writer containers are stopped, all writer sessions are absent, prepared transactions are zero, NATS stopped cleanly, the WAL fence is ordered, and the rotation lease/fence state is unchanged. No global cross-product sequence counter exists or is introduced. The zero-gap claim derives from the closed writer/session authority plus stopped NATS/Temporal processes; WAL LSNs are PostgreSQL durability boundaries, not a cross-product ordering surrogate. Fault tests open a correctly credentialed rogue application session after the first snapshot, restart each writer class, prepare a transaction, and corrupt the NATS clean-stop boundary; every case aborts before `COMPLETE`.

On any error, remain in maintenance, restart no stopped component automatically, write no success marker, and return a closed code. A successful `create` also remains in maintenance after writing the capture-complete marker; only a successful, manifest-bound `verify` receipt can authorize `resume`. After diagnosing and verifying health, the operator explicitly invokes `resume` with the privately validated manifest and receipt paths.

- [ ] **Step 3: Stream every plaintext component directly into separate age encryption**

Enumerate non-template databases with `psql -AtX` and stable OID order, but write names only into an age-encrypted index stream. The same encrypted index records exactly one configured cluster-admin role, after querying that it is `LOGIN`/`SUPERUSER`; only a role count/valid boolean is public. The backup adapter runs `psql`, `pg_dumpall`, and `pg_dump` from the exact manifest-matched PostgreSQL image, requires each reported client major to equal the source server major before reading data, and connects only over the isolated project network; the operations image orchestrates pipes but is never an implicit cross-major dump client. The client container overrides to the inspected positive PostgreSQL process UID/GID, has no Docker socket or unrelated mounts, and is removed in `finally`. Stream `pg_dumpall --globals-only`, including role password hashes only inside that encrypted stream, then `pg_dump --format=custom --create` once for every enumerated database, directly to state-recipient ciphertext files named by ordinal. Do not use `--no-owner` or `--no-privileges`: the complete backup must retain database creation metadata, ownership, ACLs, encodings/locales, extensions, and data after globals recreate the referenced roles. Never use `--dbname` with a password-bearing URI; inject the password from a mode-`0400` Compose secret file through libpq's protected passfile.

With NATS still stopped, inspect the pinned NATS runtime UID/GID and start a networkless, read-only-root, capability-free one-shot container from the pinned operations image under that exact identity, with only the validated project-labelled JetStream volume mounted read-only and no Docker socket. Stream deterministic `tar --sort=name --mtime=@0 --numeric-owner -cf - . | zstd -T1` output directly from that helper into `age`; Task 5's command inventory proves all helper commands exist. If persistent object storage is configured, quiesce its writers, stream its validated labelled volume/provider export through the same state-recipient path, and fail completeness if the adapter is missing; the test overlay covers both disabled and enabled cases. Separately stream an allowlisted operator-config archive to the operator-config recipients. Open the raw keyring with the predecessor's `O_NOFOLLOW`/regular-file/mode/size checks, compare its active/supported versions with the lease-fenced database and fresh service reports, stream that already-open descriptor to the master-key recipients, then re-`fstat` the descriptor and path identity before accepting the artifact. Each pipeline is constructed as connected `Popen` argv arrays; close inherited pipe ends, wait for every process, and treat any nonzero stage as failure. Plaintext stdout/stderr is redirected to `/dev/null` or a bounded safe-code adapter, never captured.

After each artifact, `create` hashes only ciphertext and checks every producer/encryptor exit; it cannot claim age authentication. The separate `verify` command runs on a designated isolated verifier, accepts three nonoverlapping owner-only identity files through fixed read-only secret mounts or inherited descriptors only, and rejects identity paths/material in argv, environment, output, or receipt. It rechecks canonical manifest/component hashes, then verifies age authentication by decrypting into the appropriate parser over a pipe: `pg_restore --list` for database dumps; a shared `restore_globals_into_scratch` helper; and `tar --zstd -tf -` with output discarded for archives. The globals helper starts its disposable cluster with a collision-checked `jhin_restore_bootstrap_` plus 24 random lowercase hex characters as the only initial database superuser, streams globals through `psql -X --set ON_ERROR_STOP=1`, reconnects as the authenticated restored cluster admin, reassigns all bootstrap-owned template/maintenance objects, closes bootstrap sessions, drops the temporary role, and proves it absent before declaring the parser valid. Task 8 reuses the same lifecycle for full restore; unit faults cover a role collision, hostile `.psqlrc`, invalid restored admin, and retained bootstrap. The keyring verifier parses version/counts from a mutable in-memory buffer, overwrites that buffer before release, and emits only active/supported version numbers. On success it atomically writes mode-`0600` `VERIFIED.json` containing only schema version, manifest SHA-256, component count, closed parser booleans, and `status="pass"`; a verifier error writes no receipt and leaves maintenance closed.

- [ ] **Step 4: Finalize the manifest, RPO evidence, resume, and safe pruning**

Write `manifest.json` atomically only after all create-time component checks pass, then write a zero-byte `COMPLETE` marker and fsync the backup directory. `resume` rehashes the manifest, requires an exact successful `VERIFIED.json` binding, revalidates both files without symlinks, and consumes neither age identity nor plaintext. Before restarting anything it reacquires the exact predecessor advisory lease on a fresh bounded dedicated connection, rejects an active rotation or armed retirement fence, rechecks the live keyring/database active-supported compatibility, and releases in `finally`; it never assumes the capture-time lease survived the independent verifier. The public evidence schema is emitted only after `create -> verify -> resume` and contains only:

```json
{
  "schema_version": 1,
  "backup_format": 1,
  "runtime": {"image_set_sha256": "79545f0a05813196ecf3721fa156d5e4c87b4c493bc8f33309724e9cff0e5ab6"},
  "database_count": 4,
  "components": {"state": 7, "operator_config": 1, "master_key": 1},
  "integrity": {"ciphertext_sha256": true, "age_authenticated": true, "parsers": true},
  "rpo": {"maintenance_window": true, "product_writer_gap": 0, "objective_seconds": 86400},
  "socket_modes": {
    "rootful": {"duration_seconds": 0, "last_complete_backup_age_seconds": 0, "status": "pass"},
    "rootless": {"duration_seconds": 0, "last_complete_backup_age_seconds": 0, "status": "pass"}
  },
  "plaintext_files_written": 0,
  "status": "pass"
}
```

Each drill shard carries one closed mode, the canonical runtime-content set hash, and measured nonnegative `duration_seconds` and `last_complete_backup_age_seconds`; the checked-in fixture uses `0` and the same exact fixture hash described in Task 6 only as schema-test data, and live CI overwrites them. `aggregate-evidence` requires exactly one passing rootful and rootless shard, equal runtime/topology/integrity/RPO results, and atomically emits the aggregate above. It proves the maintained deployment's prior verified daily backup is no more than the 86,400-second objective: the disaster RPO objective is 24 hours, while this quiesced capture has zero product-writer gap at its consistency point. Private evidence retains the capture UUID, closed writer/session results, NATS counters, and WAL fences inside encrypted operator state; public evidence exposes only `product_writer_gap: 0`. `resume` starts NATS, Temporal, the maintenance-mode API, workflow/event/tool/agent workers, then sandbox-runner; after protected readiness it re-proves every writer identity, disables maintenance, and proves one mutation succeeds.

Pruning operates only on direct child directories with valid canonical manifests, `COMPLETE`, a manifest-bound passing `VERIFIED.json`, matching ciphertext hashes, and no symlinks. `--dry-run` is mandatory before `--apply`; selection keeps seven daily, four distinct ISO weeks, twelve distinct calendar months, and every `pre-upgrade`/legal-hold backup. Deletion is backup-root scoped and public output records counts by retention class only.

- [ ] **Step 5: Run GREEN, live backup, integrity, and retention drills in both modes**

```bash
uv run pytest tests/test_phase10_backup.py tests/integration/test_phase10_backup.py -q
phase10_backup_tmp="$(mktemp -d)"
trap 'find "$phase10_backup_tmp" -type f -delete; rmdir "$phase10_backup_tmp"' EXIT
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/phase10_backup.py drill --compose-file compose.phase10-backup-test.yaml --evidence "$phase10_backup_tmp/rootful.json"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/phase10_backup.py drill --compose-file compose.phase10-backup-test.yaml --evidence "$phase10_backup_tmp/rootless.json"
uv run python scripts/phase10_backup.py aggregate-evidence --rootful "$phase10_backup_tmp/rootful.json" --rootless "$phase10_backup_tmp/rootless.json" --output docs/evidence/phase10-backup.json
uv run python scripts/phase10_backup.py validate-evidence docs/evidence/phase10-backup.json
uv run python scripts/phase10_prune_backups.py test-fixtures --dry-run
uv run ruff check scripts/phase10_backup_manifest.py scripts/phase10_backup.py scripts/phase10_prune_backups.py tests/test_phase10_backup.py tests/integration/test_phase10_backup.py
uv run mypy scripts/phase10_backup_manifest.py scripts/phase10_backup.py scripts/phase10_prune_backups.py
```

Expected: PASS in both socket modes. The integration test's temporary off-host stand-in is destroyed after ciphertext verification; no identities, encrypted backups, dumps, or NATS archives are checked in or uploaded.

- [ ] **Step 6: Guard, stage exactly Task 7, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add Makefile compose.phase10-backup-test.yaml scripts/phase10_backup_manifest.py scripts/phase10_backup.py scripts/phase10_prune_backups.py tests/test_phase10_backup.py tests/integration/test_phase10_backup.py docs/operations/backup.md docs/evidence/phase10-backup.json
git diff --cached --check
git commit -m "ops: add complete encrypted backups"
```

Expected: commit 9 of 15; backup completeness, encryption separation, order, integrity, RPO, and retention are independently reviewable.

### Task 8: Restore Into Fresh Volumes and Prove Full Product Recovery

**Files:**
- Modify: `Makefile`
- Create: `compose.phase10-restore-test.yaml`
- Create: `scripts/phase10_restore.py`
- Create: `tests/test_phase10_restore.py`
- Create: `tests/integration/test_phase10_restore.py`
- Create: `docs/operations/restore.md`
- Create: `docs/evidence/phase10-restore.json`

**Interfaces:**
- Consumes: a Task 7 complete backup directory, all three age identity classes from owner-only files, exact manifest image digests, empty project-labelled volumes, protected health, key-version distribution, DLQ/pending-event interfaces, and durable task APIs.
- Produces: `phase10_restore preflight|drill|cutover-check`, full-disaster key recovery, ordered PostgreSQL/NATS/Temporal/application recovery, dual-mode measured RTO evidence, and a cutover-ready decision that never performs production cutover itself.

- [ ] **Step 1: Write RED preflight, empty-target, lost-key, ordering, and recovery tests**

Tests build a synthetic complete backup through Task 7 rather than handcrafting decrypted fixtures. Pin these failures:

```python
@pytest.mark.parametrize(
    "fault,code",
    [
        ("missing-master-identity", "master_key_unavailable"),
        ("changed-ciphertext", "ciphertext_checksum_mismatch"),
        ("occupied-volume", "restore_target_not_empty"),
        ("wrong-image-digest", "manifest_image_mismatch"),
        ("symlink-component", "unsafe_backup_component"),
        ("bootstrap-role-collision", "bootstrap_role_collision"),
        ("restored-admin-not-superuser", "restored_admin_invalid"),
        ("bootstrap-role-survives", "bootstrap_role_not_removed"),
    ],
)
def test_preflight_fails_closed_without_starting_services(
    restore_fixture: RestoreFixture,
    fault: str,
    code: str,
) -> None:
    result = restore_fixture.preflight_with_fault(fault)
    assert result.code == code
    assert result.started_services == ()
    assert result.created_plaintext_files == ()
```

The command-graph test enforces keyring -> PostgreSQL -> NATS -> Temporal -> Alembic/read compatibility -> app/workers -> credential path -> durable task/pending redelivery. The live test begins with a waiting workflow timer, retained/pending JetStream message, encrypted credential, audit/task rows, and active/supported key versions.

Run:

```bash
uv run pytest tests/test_phase10_restore.py tests/integration/test_phase10_restore.py -q
```

Expected: FAIL because restore orchestration, fresh topology, recovery assertions, and runbook do not exist.

- [ ] **Step 2: Preflight every artifact before starting a service**

Require a canonical `manifest.json`, `COMPLETE`, a passing `VERIFIED.json` whose manifest hash matches, exact supported format, exact ciphertext files and sizes/hashes, all three nonoverlapping identity files, and locally available images whose inspected repo digest equals the manifest. Independently authenticate and parse each encrypted stream with output discarded or in bounded memory rather than trusting the receipt alone. Verify the encrypted database index has exactly `database_count` unique safe names and includes the maintenance, configured Jhin, Temporal, and Temporal visibility databases without publishing those names. The encrypted index also names exactly one restored cluster-admin role that appears in the globals stream with `LOGIN` and `SUPERUSER`; no public manifest/evidence contains it. If the manifest declares object storage, require its exact supported adapter and ciphertext before creating the target.

Create a unique project matching `phase10-restore-[0-9a-f]{12}` only after preflight. For the production/Phase 10 baseline, require the manifest PostgreSQL version to be 17 patch >= 11 and the locally re-resolved manifest/child digest to match; component-major recovery separately requires 16 patch >= 15. Inspect all project-labelled target volumes: they must be newly created, empty, and unmounted. Refuse any caller-supplied project matching production or any volume lacking the exact new project label. The restore runner has no production Compose project name or socket resource-removal authority.

- [ ] **Step 3: Restore in the binding order without exposing plaintext**

Execute exactly:

1. Decrypt the keyring through a pipe to an owner-created `0600` file on a private tmpfs secret mount, fsync it, validate versions, then tighten final mounted mode to `0400`; never print or return bytes.
2. Reuse Task 7's tested `restore_globals_into_scratch` lifecycle: generate `jhin_restore_bootstrap_` plus 24 lowercase hex characters with `secrets.token_hex(12)`, reject it if it appears in the authenticated globals role set, and start the manifest-matched PostgreSQL image with that unique name as the temporary bootstrap superuser and a separately generated mode-`0400` passfile secret. A custom `POSTGRES_USER` ensures the fresh cluster does not precreate a dump-owned `postgres` database role. Require the server and pinned client to report the same major, then stream globals to `psql -X --set ON_ERROR_STOP=1 --dbname=template1` as the bootstrap role; `-X` is mandatory so a hostile `.psqlrc` cannot change restore behavior. Reconnect with a protected passfile as the authenticated index's restored cluster admin, re-query and require its `LOGIN`/`SUPERUSER` attributes, run `REASSIGN OWNED BY :"bootstrap" TO :"restored_admin"` with psql identifier variables in each connectable fresh database, change ownership of `postgres`, `template1`, and `template0` to the restored admin, close every bootstrap connection, and execute `DROP ROLE :"bootstrap"` as the restored admin. Record the temporary role OID before drop and require the role absent from `pg_roles` plus zero `pg_shdepend.refobjid` references to that OID before continuing; failure leaves the isolated target stopped. Remove the bootstrap passfile immediately. Only then validate each custom archive against the encrypted index in stable order and stream it to `pg_restore --exit-on-error --clean --if-exists --create --dbname=template1` as the restored admin. No dumped database is `template1`, so the neutral maintenance connection stays valid while each archive recreates its own database with the original owner, ACLs, encoding/locale, extensions, and data. Every later `psql` call also uses `-X`.
3. With NATS stopped, pre-scan the decrypted JetStream archive and reject absolute/overlong paths, `..`, links, devices, sparse entries, duplicate entries, a count/uncompressed total above the manifest/preflight disk bound, or numeric owners that differ from the inspected manifest-matched NATS runtime UID/GID. Start a networkless, read-only-root, capability-free one-shot container from the pinned operations image, override it to that exact nonroot NATS UID/GID, mount only the empty project JetStream volume, and stream `age` -> `zstd -d` -> `tar --extract --numeric-owner --no-same-owner` over its stdin; because extraction runs as the pinned runtime identity, no host/root ownership mutation is required and every command comes from Task 5's inventory. Remove the helper, start the normal manifest-matched NATS service, and verify stream/consumer configuration, retained count, pending count, and advisories.
4. Restore any declared persistent object-storage archive/export into its own empty labelled target and verify object count/checksum totals without names. Verify the restored Temporal history/visibility schema versions with the manifest-matched `temporalio/admin-tools` image in read-only inspection mode, then start the manifest-matched `temporalio/server` with `SKIP_SCHEMA_SETUP=true`; do not run setup/update during a same-version restore. Verify namespace, restored workflow histories, one in-flight wait, and one timer while all application workers remain stopped.
5. Run packaged `alembic current` and assert exactly the authenticated manifest head; do not migrate. Normal production restore requires `0018`; Task 10's isolated pre-upgrade recovery alone may restore `0017` before its separately authorized `0018` migration. Start the manifest-matched API read-only, prove it can read restored rows, then start workflow-, event-, tool-, and agent-worker plus sandbox-runner.
6. Wait for protected readiness, expected Temporal pollers, key-version distribution, JetStream consumers, and authoritative task/audit/tool counts.
7. Exercise one stored synthetic credential through its normal fake connector/model decrypt-and-use path; capture only success and key version.
8. Complete one durable task and prove the seeded pending event is applied once despite a forced single redelivery.

Database restore without the matching master-key artifact must fail at step 1 with `master_key_unavailable`; the runbook explicitly states that encrypted credentials are unrecoverable without it. No recovery mode invents or replaces a missing key.

- [ ] **Step 4: Produce RTO/integrity evidence and an operator-controlled cutover check**

Start RTO at invocation and stop only after step 8. Each drill emits a one-mode shard with the canonical runtime-content set hash, integer phase durations, total seconds, restored database/component counts, matched versions, boolean integrity/recovery assertions, and `cutover_ready`; emit no names, IDs, paths, DSNs, credentials, workflow histories, messages, logs, or keys. `aggregate-evidence` requires exactly one passing rootful and rootless shard, equal runtime/topology/version/recovery assertions, and preserves each mode's measured phases/total in the checked artifact. Both live totals must satisfy `total_seconds <= 7200`, `target_was_fresh=true`, and `migration_performed=false`.

`cutover-check` re-runs read-only protected health, image digest, head, counts, timer/pending-event, and credential-use assertions, then prints only `cutover-ready` or a closed failure code. It never changes DNS, ports, volumes, routes, or the production project. The runbook assigns cutover/rollback authority to the operator, requires a restore drill before every release candidate and at least quarterly, and records the measured RTO against a declared two-hour objective.

- [ ] **Step 5: Run GREEN and live full recovery in both socket modes**

```bash
uv run pytest tests/test_phase10_restore.py tests/integration/test_phase10_restore.py -q
phase10_restore_tmp="$(mktemp -d)"
trap 'find "$phase10_restore_tmp" -type f -delete; rmdir "$phase10_restore_tmp"' EXIT
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/phase10_restore.py drill --compose-file compose.phase10-restore-test.yaml --evidence "$phase10_restore_tmp/rootful.json"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/phase10_restore.py drill --compose-file compose.phase10-restore-test.yaml --evidence "$phase10_restore_tmp/rootless.json"
uv run python scripts/phase10_restore.py aggregate-evidence --rootful "$phase10_restore_tmp/rootful.json" --rootless "$phase10_restore_tmp/rootless.json" --output docs/evidence/phase10-restore.json
uv run python scripts/phase10_restore.py validate-evidence docs/evidence/phase10-restore.json
uv run ruff check scripts/phase10_restore.py tests/test_phase10_restore.py tests/integration/test_phase10_restore.py
uv run mypy scripts/phase10_restore.py
```

Expected: PASS in fresh projects with recovered PostgreSQL, Temporal history/timer, NATS state, credential use, durable task, bounded RTO, and guaranteed teardown.

- [ ] **Step 6: Guard, stage exactly Task 8, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add Makefile compose.phase10-restore-test.yaml scripts/phase10_restore.py tests/test_phase10_restore.py tests/integration/test_phase10_restore.py docs/operations/restore.md docs/evidence/phase10-restore.json
git diff --cached --check
git commit -m "ops: prove full product restore"
```

Expected: commit 10 of 15; restore safety, order, integrity, key recovery, product authority, and RTO are reviewable independently of upgrades.

### Task 9: Support Bundled Plaintext and External mTLS Temporal Connections

**Files:**
- Modify: `.env.example`
- Modify: `compose.yaml`
- Modify: `uv.lock`
- Modify: `packages/observability/src/jhin_observability/temporal.py`
- Modify: `packages/observability/tests/test_temporal.py`
- Modify: `packages/workflows/pyproject.toml`
- Modify: `packages/workflows/src/jhin_workflows/__init__.py`
- Modify: `packages/workflows/src/jhin_workflows/poller_health.py`
- Create: `packages/workflows/src/jhin_workflows/temporal_connection.py`
- Create: `packages/workflows/src/jhin_workflows/temporal_connection_cli.py`
- Modify: `packages/workflows/tests/test_poller_health.py`
- Create: `packages/workflows/tests/test_temporal_connection.py`
- Modify: `apps/api/src/jhin_api/settings.py`
- Modify: `apps/api/src/jhin_api/temporal.py`
- Modify: `apps/api/tests/test_temporal_provider.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Create: `services/agent_worker/tests/test_temporal_connection.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Create: `services/tool_worker/tests/test_temporal_connection.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Create: `services/event_worker/tests/test_temporal_connection.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/settings.py`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/main.py`
- Create: `services/workflow_worker/tests/test_temporal_connection.py`
- Create: `docs/operations/external-temporal.md`

**Interfaces:**
- Consumes: Temporal Python SDK `1.31.x` `Client.connect`/`TLSConfig`, telemetry-owned `connect_temporal_client` and `temporal_client_interceptors`, existing agent/tool connection retry loops, `jhin-temporal-poller-check`, Compose secrets, exact bundled endpoint `temporal:7233`, service settings, and existing workflow/task IDs.
- Produces: shared `TemporalConnectionConfig`, `connect_temporal`, a telemetry-helper SDK boundary with unchanged interceptors and retry ownership, safe connectivity/poller CLIs, file-backed external TLS/mTLS configuration for every Temporal client, an updated recursive Temporal AST audit, and a namespace/certificate-rotation/cutover runbook.

- [ ] **Step 1: Write RED TLS-file, plaintext-boundary, client-wiring, and safe-CLI tests**

Use real mode-controlled temporary regular files and a mocked `Client.connect` to assert the exact SDK arguments:

```python
async def test_external_temporal_builds_mtls_without_leaking_paths(
    cert_files: TemporalCertificateFiles,
) -> None:
    config = TemporalConnectionConfig(
        address="temporal.example.test:7233",
        namespace="jhin-production",
        app_env="production",
        topology="external",
        tls_enabled=True,
        server_name="temporal.example.test",
        ca_file=cert_files.ca,
        client_cert_file=cert_files.certificate,
        client_key_file=cert_files.private_key,
    )
    tls = config.build_tls_config()
    assert isinstance(tls, TLSConfig)
    assert tls.server_root_ca_cert == cert_files.ca.read_bytes()
    assert tls.domain == config.server_name == "temporal.example.test"
    assert tls.verification_server_name is None
    assert tls.client_cert == cert_files.certificate.read_bytes()
    assert tls.client_private_key == cert_files.private_key.read_bytes()
    assert tuple(field.name for field in fields(TLSConfig)) == (
        "server_root_ca_cert",
        "domain",
        "client_cert",
        "client_private_key",
        "verification_server_name",
    )
    assert "PRIVATE KEY" not in repr(config)


def test_production_plaintext_is_bundled_only() -> None:
    assert bundled_config().build_tls_config() is False
    with pytest.raises(TemporalConnectionError, match="temporal_tls_required"):
        external_plaintext_config().build_tls_config()


def test_every_temporal_client_delegates_to_the_tls_connector() -> None:
    audit = audit_temporal_wiring(REPO_ROOT)
    assert audit.direct_client_connect_calls == (
        "packages/observability/src/jhin_observability/temporal.py",
    )
    assert audit.connector_callers == {
        "apps/api/src/jhin_api/temporal.py",
        "packages/workflows/src/jhin_workflows/poller_health.py",
        "packages/workflows/src/jhin_workflows/temporal_connection.py",
        "packages/workflows/src/jhin_workflows/temporal_connection_cli.py",
        "services/agent_worker/src/jhin_agent_worker/main.py",
        "services/event_worker/src/jhin_event_worker/main.py",
        "services/tool_worker/src/jhin_tool_worker/main.py",
        "services/workflow_worker/src/jhin_workflow_worker/main.py",
    }
    assert audit.uninstrumented_connector_calls == ()
    assert audit.direct_health_connect_calls == ()
```

Test symlink, directory, missing file, over-1-MiB file, group/world-readable private key, inline PEM-like env value, missing server name, incomplete client pair, invalid namespace, and address containing scheme/path. Test the full topology/address/TLS cross-product: only `topology="bundled"` plus exact `temporal:7233` plus TLS disabled is plaintext; external topology requires TLS even if its DNS name or address string resembles the bundled endpoint; bundled topology rejects every other address and every TLS-file setting. Parameterize every API/worker factory and prove each calls the shared connector. Capture CLI stdout/stderr and assert only `temporal-connectivity: ok` or a closed code appears.

In `packages/observability/tests/test_temporal.py`, evolve the predecessor's alias-aware recursive
AST audit rather than replacing it with text search. It resolves imports; permits the sole
`TemporalClient.connect` call only in telemetry's `connect_temporal_client`; requires that call to
forward `tls` and `temporal_client_interceptors(runtime)`; recognizes `connect_temporal` callers;
and reports any direct service/package/health call or connector call without the active runtime.
In `packages/workflows/tests/test_poller_health.py`, retain the live-poller and owned-runtime tests
and assert the exact interceptor list still reaches the telemetry helper through the new connector.
The agent/tool connection tests inject two failed connector attempts followed by success, record
`asyncio.sleep`, and assert the unchanged delays `[1.0, 2.0]`, 15-second cap, normalized safe log
fields, one fresh connector call per attempt, and the same `ObservabilityRuntime` object throughout.

Run:

```bash
uv run pytest packages/observability/tests/test_temporal.py packages/workflows/tests/test_temporal_connection.py packages/workflows/tests/test_poller_health.py apps/api/tests/test_temporal_provider.py services/agent_worker/tests/test_temporal_connection.py services/tool_worker/tests/test_temporal_connection.py services/event_worker/tests/test_temporal_connection.py services/workflow_worker/tests/test_temporal_connection.py -q
```

Expected: FAIL because the shared TLS connector does not exist, the installed SDK-name mapping is
unasserted, the poller and agent/tool entrypoints retain direct/telemetry-only connection paths,
and the predecessor AST audit does not yet enforce the new delegation graph. Inspect these distinct
failures before implementation.

- [ ] **Step 2: Implement one file-backed connection loader**

Read certificate files only after `lstat` and open each validated `path` with `os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)`, then `fstat` the opened descriptor to prevent replacement races. Require regular files at most 1,048,576 bytes; require the private key owned by the process UID and mode `0400` or `0600`; reject NULs and malformed PEM. Keep private key bytes only in the short-lived SDK TLS object and never log/configure `repr` with values.

The locked runtime is Temporal Python SDK `1.31.x`. Its installed `TLSConfig.domain` field controls
SNI, HTTP/2 authority, and the default certificate name; `verification_server_name` is a distinct
verification-only override. `TemporalConnectionConfig.build_tls_config()` therefore returns
`TLSConfig(server_root_ca_cert=..., domain=self.server_name, client_cert=...,
client_private_key=..., verification_server_name=None)` for external topology. There is no second
verification-only setting. The unit test introspects the installed dataclass fields and proves the
configured `server_name` reaches `domain` byte-for-byte.

Keep telemetry dependency direction intact: `jhin_workflows` already depends on
`jhin_observability`, never the reverse. Extend telemetry's existing `connect_temporal_client`
helper with one required keyword-only `tls: bool | TLSConfig` parameter; it remains the sole direct
`Client.connect` caller and still passes `temporal_client_interceptors(runtime)` as the exact list
object. `TemporalConnectionConfig` supplies read-only `temporal_address` and
`temporal_namespace` protocol properties for that helper. `connect_temporal(config, runtime)`
validates the closed topology, builds TLS, and delegates to
`connect_temporal_client(config, runtime, tls=tls)`; it never constructs or substitutes telemetry
interceptors itself.

The connector passes `tls=False` only when topology is `bundled`, address equals
`temporal:7233`, and TLS/files/server-name are absent. `bundled` with any other address or TLS
material is invalid. `external` always requires TLS, CA, and server name—even if configured with
the literal bundled address—and permits either both client certificate/key or neither when the
external provider authenticates separately. Connection errors are normalized to
`temporal_configuration_invalid`, `temporal_tls_required`, `temporal_certificate_invalid`, or
`temporal_unavailable`; safe error codes do not contain addresses, paths, certificate content, or
SDK exception text.

- [ ] **Step 3: Route every client through the shared connector and Compose secrets**

Replace direct or predecessor-helper connection calls in the API provider, agent-worker main,
tool-worker main, event-worker main, workflow-worker main, poller-health command, and connectivity
CLI with `connect_temporal`. Agent `connect_with_retry` and the tool worker's corresponding retry
wrapper retain their existing `while True`, one-second initial delay, doubling, 15-second cap,
cancellation behavior, safe logging, and one attempt per loop; only the call inside the loop changes
to `connect_temporal(config, runtime)`. Event/workflow retry ownership is likewise unchanged. Do not
move retry loops into the shared connector and do not add an unbounded retry to the connectivity or
poller CLI.

Preserve namespace, task queues, workflow/task IDs, SDK defaults, and the telemetry runtime object.
All `Worker(...)` construction continues to use the predecessor's exact
`temporal_worker_interceptors(runtime, task_queue=...)` list; the connection helper continues to
own the exact client interceptor list. The API provider retains its single-client lock/concurrency
contract. `poller_health.queue_has_workflow_poller(address, namespace, queue, *, runtime=None)`
retains those public arguments and owned-runtime shutdown behavior, derives the remaining closed
TLS fields from the same environment loader, and delegates before issuing
`describe_task_queue`. The updated alias-aware AST audit rejects every direct SDK call outside the
telemetry helper, every bypass of `connect_temporal`, and every missing runtime handoff.

Compose injects the closed topology into every client: the base fixes `bundled` with
`temporal:7233`, while the mutually exclusive external profile fixes `external` and mounts
CA/certificate/key as read-only secrets at `/run/secrets/temporal-*`. A production render with a
caller override, mixed topologies across clients, or both bundled/external service definitions fails
static validation. `.env.example` contains topology, paths, and public server metadata only, never
PEM or key values.

- [ ] **Step 4: Write the namespace, certificate rotation, and no-ID-change cutover runbook**

Document exact Temporal CLI commands using the operations image:

```bash
# phase10-command: operator
set -euo pipefail
phase10_compose_env=/etc/jhin/compose.env
phase10_docker_config=/etc/jhin/docker-config
phase10_temporal_namespace=jhin-production
test -r "$phase10_compose_env"
test -d "$phase10_docker_config"
test -S /run/user/10001/docker.sock
DOCKER_HOST=unix:///run/user/10001/docker.sock DOCKER_CONFIG="$phase10_docker_config" COMPOSE_DISABLE_ENV_FILE=1 PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock docker compose -p jhin-production --env-file "$phase10_compose_env" -f compose.yaml -f compose.operations.yaml -f compose.rootless.yaml run --rm --no-deps operations temporal operator namespace describe --namespace "$phase10_temporal_namespace"
DOCKER_HOST=unix:///run/user/10001/docker.sock DOCKER_CONFIG="$phase10_docker_config" COMPOSE_DISABLE_ENV_FILE=1 PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock docker compose -p jhin-production --env-file "$phase10_compose_env" -f compose.yaml -f compose.rootless.yaml run --rm --no-deps workflow-worker python -m jhin_workflows.temporal_connection_cli --check-env
```

The leading Unix `DOCKER_HOST` in these two commands is consumed only by the UID-10001 host user's Compose client to create the `run` container. The rendered rootless operations container still has no socket bind and, when it needs Docker, receives only `tcp://rootless-operations-transport:2375`; workflow-worker receives neither authority. The runbook requires creating/verifying the target namespace, loading CA and client files via the operator secret store, checking file ownership/modes, validating connectivity from every service network, pausing new task starts, waiting for safe boundaries, switching all clients together without changing workflow/task IDs, proving histories/pollers/timers, then resuming. It states Tasks 7–8 are the supported bundled-Temporal backup baseline; an external cluster is production-admissible only when its operator/provider supplies an independently rehearsed history/visibility backup and fresh-namespace restore meeting the same 86,400-second RPO and 7,200-second RTO, and readiness records that evidence without provider resource identifiers. Absent that evidence, external connectivity may be tested but production readiness fails. Certificate rotation stages trust for old+new CA where supported, rotates client certificate/key atomically, recreates clients, verifies, and only then removes old trust. Failure reverts file mounts/address and recreates clients; it never falls back to plaintext.

- [ ] **Step 5: Run GREEN and all Temporal client regressions**

```bash
uv lock
uv lock --check
uv run python -c 'from dataclasses import fields; from temporalio.service import TLSConfig; assert tuple(field.name for field in fields(TLSConfig)) == ("server_root_ca_cert", "domain", "client_cert", "client_private_key", "verification_server_name")'
uv run pytest packages/observability/tests/test_temporal.py packages/workflows/tests/test_temporal_connection.py packages/workflows/tests/test_poller_health.py apps/api/tests/test_temporal_provider.py services/agent_worker/tests/test_temporal_connection.py services/tool_worker/tests/test_temporal_connection.py services/event_worker/tests/test_temporal_connection.py services/workflow_worker/tests/test_temporal_connection.py -q
uv run pytest packages/workflows/tests apps/api/tests services/agent_worker/tests services/tool_worker/tests services/event_worker/tests services/workflow_worker/tests -q
uv run ruff check packages/observability packages/workflows apps/api services
uv run mypy packages/observability/src packages/workflows/src apps/api/src services/agent_worker/src services/tool_worker/src services/event_worker/src services/workflow_worker/src
uv run python scripts/assert_phase10_production_compose.py
```

Expected: PASS. Bundled semantics and all existing retry/interceptor behavior remain unchanged;
every active client and poller delegates through the shared TLS connector, every external
connection is CA-validated with `server_name == TLSConfig.domain`, and no service has a private-key
environment value.

- [ ] **Step 6: Guard, stage exactly Task 9, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add .env.example compose.yaml uv.lock packages/observability/src/jhin_observability/temporal.py packages/observability/tests/test_temporal.py packages/workflows/pyproject.toml packages/workflows/src/jhin_workflows/__init__.py packages/workflows/src/jhin_workflows/poller_health.py packages/workflows/src/jhin_workflows/temporal_connection.py packages/workflows/src/jhin_workflows/temporal_connection_cli.py packages/workflows/tests/test_poller_health.py packages/workflows/tests/test_temporal_connection.py apps/api/src/jhin_api/settings.py apps/api/src/jhin_api/temporal.py apps/api/tests/test_temporal_provider.py services/agent_worker/src/jhin_agent_worker/settings.py services/agent_worker/src/jhin_agent_worker/main.py services/agent_worker/src/jhin_agent_worker/resources.py services/agent_worker/tests/test_temporal_connection.py services/tool_worker/src/jhin_tool_worker/settings.py services/tool_worker/src/jhin_tool_worker/main.py services/tool_worker/src/jhin_tool_worker/resources.py services/tool_worker/tests/test_temporal_connection.py services/event_worker/src/jhin_event_worker/settings.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/tests/test_temporal_connection.py services/workflow_worker/src/jhin_workflow_worker/settings.py services/workflow_worker/src/jhin_workflow_worker/main.py services/workflow_worker/tests/test_temporal_connection.py docs/operations/external-temporal.md
git diff --cached --check
git commit -m "feat: secure external temporal connections"
```

Expected: commit 11 of 15; all Temporal clients share one auditable TLS boundary before upgrade procedures are added.

### Task 10: Rehearse Application, PostgreSQL, Temporal, and NATS Upgrade and Recovery

**Files:**
- Modify: `Makefile`
- Create: `ops/versions.json`
- Create: `compose.phase10-upgrade-test.yaml`
- Create: `scripts/phase10_upgrade.py`
- Create: `tests/test_phase10_upgrade.py`
- Create: `tests/integration/test_phase10_upgrade.py`
- Create: `docs/operations/upgrades.md`
- Create: `docs/evidence/phase10-upgrades.json`
- Create: `.github/workflows/phase10-operations.yml`

**Interfaces:**
- Consumes: verified Task 7 pre-upgrade backup, Task 8 fresh restore, predecessor-owned `packages/workflows/tests/fixtures/phase9_temporal/phase9-ref.txt`, the exact Task 9 keyring-capable checkpoint, predecessor keyring rollback rules, Task 9 Temporal connector, exact image digests, migration `0018`, and existing health/reconciliation interfaces.
- Produces: a validated matrix separating the actual Phase 9 application fixture on current infrastructure from independent PostgreSQL/NATS/Temporal component-major rehearsals; `phase10_upgrade app|postgres|temporal|nats|all`; one-component-at-a-time rehearsal; state-compatible app rollback before and after key retirement; restore-based database/Temporal/NATS downgrade recovery; and dual-mode compatibility evidence.

- [ ] **Step 1: Write RED version-matrix, command-graph, failure, downgrade, and live tests**

Pin the initial matrix in `ops/versions.json` and make tests reject floating tags, missing architecture digests, unsupported jumps, or reused major-version volumes. The runner validates the one-line full SHA in `packages/workflows/tests/fixtures/phase9_temporal/phase9-ref.txt` as an ancestor, exports that tree with `git archive` into its private temporary build context, and never edits or stages the predecessor fixture:

```json
{
  "schema_version": 1,
  "current_release": "phase10",
  "application_rehearsal": {
    "phase9": {
      "native_schema": "0014",
      "deployed_database_head": "0017",
      "key_format": "legacy-v1",
      "postgres": "17.11-alpine",
      "nats": "2.12.11-alpine",
      "temporal_server": "1.29.7",
      "temporal_admin_tools": "1.29.7"
    },
    "phase10": {
      "database_head": "0018",
      "postgres": "17.11-alpine",
      "nats": "2.12.11-alpine",
      "temporal_server": "1.29.7",
      "temporal_admin_tools": "1.29.7"
    },
    "initial_rollback_fixture": "phase9-legacy-v1",
    "post_retirement_rollback_fixture": "task9-keyring-capable"
  },
  "component_rehearsals": {
    "postgres": {"source": "16.15-alpine", "target": "17.11-alpine"},
    "nats": {"source": "2.11.0-alpine", "target": "2.12.11-alpine"},
    "temporal": {
      "source_server": "1.28.1",
      "source_admin_tools": "1.28.1",
      "target_server": "1.29.7",
      "target_admin_tools": "1.29.7"
    }
  }
}
```

Tags and named fixtures are human-readable compatibility declarations only. Before any container starts, Task 10 freshly resolves every listed PostgreSQL/NATS/Temporal server/admin-tools tag against the registry; validates the Phase 9 fixture's one-line full SHA; resolves exactly one ancestor whose subject is `feat: secure external temporal connections` as the Task 9 keyring-capable checkpoint; snapshots all application image IDs; and records those immutable values in private drill state. Every container reference must match `[^@]+@sha256:[0-9a-f]{64}` or an exact locally inspected image ID. PostgreSQL source/target must be at least 16.15/17.11, respectively; older patches fail even if a digest resolves. A missing, ambiguous, nonancestor, or capability-mismatched application checkpoint fails before backup or maintenance. Task 12 later creates the two-architecture production manifest without being a prerequisite for this single-host rehearsal. Unit tests assert the ordered graph includes `backup.verify` before maintenance, exactly one changing component, smoke between components, a rollback image compatible with the mounted key format, and restore into fresh source-version volumes for PostgreSQL/Temporal/NATS recovery. They also prove the Phase 9 application rehearsal uses PostgreSQL 17.11, NATS 2.12.11, and Temporal server/admin-tools 1.29.7 on both sides; no component-major source image may be mislabeled as the previous application release.

Run:

```bash
uv run pytest tests/test_phase10_upgrade.py tests/integration/test_phase10_upgrade.py -q
```

Expected: FAIL because the matrix, isolated previous/current topology, runner, runbook, and evidence do not exist.

- [ ] **Step 2: Implement application migration, rollback, and isolated downgrade rehearsal**

Seed the actual Phase 9 application fixture—whose native release schema was `0014` but which runs against the additive predecessor-complete database head `0017`—with the predecessor's exact legacy-v1 file on PostgreSQL 17.11, NATS 2.12.11, and explicitly initialized Temporal server/admin-tools 1.29.7. Take/verify a full pre-upgrade `0017` backup, enable maintenance, quiesce writers through Task 7's proof, and run `alembic upgrade 0018` exactly once from the immutable candidate API image with `lock_timeout=5000ms`, `statement_timeout=30000ms`, and a 35-second client deadline. Assert that no infrastructure image changed, a single linear head, all migration invariants, protected readiness, durable task completion, and ordinary credential/tool use before disabling maintenance.

First restore the frozen Phase 9 image while retaining additive schema `0018` and the unchanged legacy-v1 file; prove it starts, reads/writes all previous-release shapes, ignores the new rate-limit table safely, and processes a previous workflow history. Assert that this image is never given versioned JSON key material. Return to the candidate, then use the settled master-key drill to create a synthetic post-retirement state and roll back only to the immutable Task 9 checkpoint. Prove that checkpoint advertises keyring capability, loads the exact active/supported versions, reads/writes credentials, and processes the same workflow history against `0018`. The matrix rejects Phase 9 as a post-retirement target; if no reviewed keyring-capable prior digest exists, key retirement remains blocked rather than silently removing rollback.

Separately, from a legacy-v1 snapshot containing no post-upgrade product writes and exactly zero `rate_limit_bucket` rows, run `alembic downgrade 0017`, assert the bucket table/index disappear and the Phase 9 app remains functional, then re-upgrade to `0018`. The runner refuses downgrade when any bucket or other new-version-only data exists; it never deletes buckets merely to satisfy the precondition. If the exact downgrade/re-upgrade contract fails, recovery is the full pre-upgrade restore, not an ad hoc downgrade. The runbook states both state-specific application rollback tracks and the predecessor rule that post-retirement data/key rollback requires the separately protected old ring matched to its database backup.

- [ ] **Step 3: Implement PostgreSQL 16-to-17 logical upgrade and 16 recovery**

Never point PostgreSQL 17 at a PostgreSQL 16 data volume. Independently of the Phase 9 application rehearsal, seed the candidate-compatible component fixture on an isolated, freshly resolved PostgreSQL 16.15 volume while NATS 2.12.11 and Temporal 1.29.7 remain fixed; take/verify the pre-upgrade backup through Task 7's exact PostgreSQL 16 client adapter, then create a second empty PostgreSQL 17.11 volume. Restore globals through Task 8's collision-free temporary bootstrap and every database logically through its exact PostgreSQL 17 client adapter. Before either stream, execute the pinned client's `--version` and compare its parsed major with the connected server; a mismatch is `compatibility_failed` before plaintext. Verify fresh source/target digests, extensions, role attributes without passwords, ownership, encodings/locales, constraints, per-table row counts, Alembic `0018`, Temporal history/visibility, NATS-independent application queries, and a durable smoke before selecting the 17 endpoint.

Downgrade/recovery creates a third empty PostgreSQL 16.15 volume and restores the same PostgreSQL-16-client backup through an exact PostgreSQL 16 restore client, while Temporal 1.29.7 and NATS 2.12.11 remain fixed, using the matrix-selected application image compatible with that backup's key format. It proves the matching component-fixture smoke and leaves both old and upgraded volumes untouched for operator disposition. A vulnerable PostgreSQL patch, a newer dump client creating the rollback archive, a client/server major mismatch, or merely changing an image major on either volume is a hard test failure. `pg_upgrade` is explicitly outside the baseline and cannot satisfy this gate.

- [ ] **Step 4: Implement Temporal and NATS one-at-a-time upgrades plus restore recovery**

For Temporal, independently seed real waiting/timer/completed histories in the source `temporalio/server:1.28.1` fixture while PostgreSQL 17.11 and NATS 2.12.11 remain fixed; its schema must have been initialized explicitly by matching `temporalio/admin-tools:1.28.1`, never auto-setup. Keep all clients quiesced and run `temporal-sql-tool --help`, then the history and visibility `update-schema` commands in upstream versioned order from matching target `temporalio/admin-tools:1.29.7`. The server image never supplies or runs schema tooling. Start pinned `temporalio/server:1.29.7` with schema mutation disabled, verify namespace, histories, pending timers, search/visibility, pollers, and one new durable workflow. Application rollback against the upgraded supported Temporal schema is tested with the matrix-selected image for the backup's key format. A schema-bearing server downgrade is recovered by Task 8 into fresh volumes using server/admin-tools `1.28.1` and its matching database backup; it never runs old binaries against upgraded schema or leaves admin-tools running.

For NATS, independently seed streams, consumers, retained/pending messages, advisories, and dedupe IDs on `2.11.0` while PostgreSQL 17.11 and Temporal 1.29.7 remain fixed; verify backup; stop clients and NATS cleanly; copy no live files; start `2.12.11` against the rehearsal volume only after the compatibility preflight; verify stream/consumer configs, message counts, redelivery, dedupe, reconnect, and lag-to-zero. Because storage downgrade compatibility is not assumed, recovery restores the verified backup into an empty volume under `2.11.0`, then proves the same checks. No runner invokes two infrastructure upgrades without an intervening full smoke.

- [ ] **Step 5: Bound failures and record only compatibility/RTO outcomes**

Every phase has a fixed deadline and a `finally` teardown. Failure keeps maintenance enabled and returns one of `backup_invalid`, `migration_failed`, `compatibility_failed`, `health_failed`, `restore_required`, or `recovery_failed`. It never resumes automatically after an ambiguous schema/storage failure. Each shard contains one closed socket mode, the canonical runtime-content set hash, versions, architecture, booleans, integer durations, schema heads, count equality, `pre_upgrade_backup_verified`, exact booleans `app_rollback_legacy_v1`, `app_rollback_keyring_capable`, and `incompatible_key_format_rejected`, plus status only; reject paths, DSNs, role/database/stream/workflow names, messages, secrets, container IDs, volume IDs, and logs. `aggregate-evidence` requires exactly one passing shard per mode, identical runtime/compatibility/recovery results, and preserves the separate measured durations.

The runbook covers release-note/security-patch review, capacity/disk preflight, backup, maintenance, exact order, validation, normal application rollback, PostgreSQL logical upgrade, Temporal schema ordering, NATS storage verification, restore-based downgrade recovery, cutover authority, abort points, and component-specific escalation. It states that Phase 9 remains the previous application release on PostgreSQL 17.11/NATS 2.12.11/Temporal 1.29.7 until a reviewed release changes that fixture; the PostgreSQL 16.15, NATS 2.11.0, and Temporal 1.28.1 inputs are independent component-major sources and never redefine the previous application release.

- [ ] **Step 6: Run GREEN and the full isolated matrix in both socket modes**

```bash
uv run pytest tests/test_phase10_upgrade.py tests/integration/test_phase10_upgrade.py -q
phase10_upgrade_tmp="$(mktemp -d)"
trap 'find "$phase10_upgrade_tmp" -type f -delete; rmdir "$phase10_upgrade_tmp"' EXIT
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/phase10_upgrade.py all --compose-file compose.phase10-upgrade-test.yaml --evidence "$phase10_upgrade_tmp/rootful.json"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/phase10_upgrade.py all --compose-file compose.phase10-upgrade-test.yaml --evidence "$phase10_upgrade_tmp/rootless.json"
uv run python scripts/phase10_upgrade.py aggregate-evidence --rootful "$phase10_upgrade_tmp/rootful.json" --rootless "$phase10_upgrade_tmp/rootless.json" --output docs/evidence/phase10-upgrades.json
uv run python scripts/phase10_upgrade.py validate-evidence docs/evidence/phase10-upgrades.json
uv run ruff check scripts/phase10_upgrade.py tests/test_phase10_upgrade.py tests/integration/test_phase10_upgrade.py
uv run mypy scripts/phase10_upgrade.py
```

Expected: PASS for application upgrade/rollback/downgrade rehearsal, PostgreSQL logical major upgrade/recovery, Temporal ordered schema/server upgrade/recovery, and NATS upgrade/recovery. All projects and volumes are unique and removed within the Task 5 bound.

- [ ] **Step 7: Guard, stage exactly Task 10, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add Makefile ops/versions.json compose.phase10-upgrade-test.yaml scripts/phase10_upgrade.py tests/test_phase10_upgrade.py tests/integration/test_phase10_upgrade.py docs/operations/upgrades.md docs/evidence/phase10-upgrades.json .github/workflows/phase10-operations.yml
git diff --cached --check
git commit -m "ops: rehearse component upgrades and recovery"
```

Expected: commit 12 of 15; all four upgrade domains and their supported downgrade/recovery boundaries are green before load or release work.

### Task 11: Measure All Five 30-Minute Capacity Profiles

**Files:**
- Modify: `Makefile`
- Create: `ops/sizing/profiles.json`
- Create: `compose.phase10-sizing.yaml`
- Create: `tests/load/__init__.py`
- Create: `tests/load/phase10_sizing.py`
- Create: `scripts/run_phase10_sizing.py`
- Create: `tests/test_phase10_sizing_config.py`
- Create: `tests/integration/test_phase10_sizing_smoke.py`
- Create: `docs/operations/resource-sizing.md`
- Create: `docs/evidence/phase10-sizing.json`
- Create: `.github/workflows/phase10-sizing.yml`

**Interfaces:**
- Consumes: Task 5 resource/log limits and isolated harness, predecessor optional observability profile/metrics, protected health, deterministic fake model/connector/webhook/sandbox providers, database/NATS/Temporal telemetry, and dedicated hosts matching each declared baseline.
- Produces: five immutable sizing profiles, `StorageInventory`, `resolve_storage_inventory`, `evaluate_storage`, `public_storage_evidence`, fixed-seed workload generator, 300-second warm-up plus 1,800-second measurement per profile and socket mode, threshold evaluator, redacted evidence, and admission/headroom guidance.

- [ ] **Step 1: Write RED profile-schema, workload-equality, threshold, and bounded smoke tests**

`tests/test_phase10_sizing_config.py` asserts exactly these profile identities and baselines:

| ID | Host baseline | Workload | Monitoring |
| --- | --- | --- | --- |
| `development` | 4 vCPU / 8 GiB / 40 GiB | `development-v1` | off |
| `small` | 8 vCPU / 16 GiB / 100 GiB | `small-v1` | off |
| `small-monitored` | 12 vCPU / 24 GiB / 150 GiB | `small-v1` | 15d metrics / 72h traces |
| `medium` | 16 vCPU / 32 GiB / 250 GiB | `medium-v1` | off |
| `medium-monitored` | 24 vCPU / 48 GiB / 400 GiB | `medium-v1` | 15d metrics / 72h traces |

Every profile has `warmup_seconds=300`, `measure_seconds=1800`, `sample_seconds=10`, and `drain_timeout_seconds=300`. Tests compare the canonical workload object byte-for-byte between each monitored/unmonitored pair, reject fewer/more profiles, and prove rolling-window logic catches boundary failures:

```python
def test_exact_limit_is_not_below_threshold() -> None:
    samples = [ResourceSample(cpu_percent=80.0, ram_percent=79.0, disk_percent=79.0)] * 6
    result = evaluate_rolling_minute(samples, sample_seconds=10)
    assert result.passed is False
    assert result.safe_code == "cpu_threshold_exceeded"


def test_five_minutes_of_growing_lag_fails() -> None:
    lag = tuple(range(30))
    result = evaluate_lag(lag, sample_seconds=10, final_lag=0)
    assert result.passed is False
    assert result.safe_code == "nats_lag_sustained"


def test_full_root_does_not_hide_exhausted_daemon_backing_store() -> None:
    inventory = fake_storage_inventory(
        root=FakeFilesystem(total_gib=400, free_gib=300, device=1),
        docker_root=FakeFilesystem(total_gib=100, free_gib=10, device=2),
        named_volumes_device=2,
    )
    result = evaluate_storage(inventory, required_free_gib=20)
    assert result.passed is False
    assert result.safe_code == "backing_filesystem_headroom"


def test_public_storage_evidence_has_no_path_or_device(
    passing_storage_inventory: StorageInventory,
) -> None:
    public = public_storage_evidence(passing_storage_inventory)
    encoded = json.dumps(public, sort_keys=True)
    assert set(public) == {
        "backing_filesystem_count",
        "all_backings_resolved",
        "minimum_free_percent",
        "maximum_used_percent",
        "maximum_growth_bytes",
    }
    assert "/" not in encoded
    assert "device" not in encoded
```

Run:

```bash
uv run pytest tests/test_phase10_sizing_config.py tests/integration/test_phase10_sizing_smoke.py -q
```

Expected: FAIL because the profile schema, generator, runner, bounded smoke topology, runbook, and evidence do not exist.

- [ ] **Step 2: Define exact deterministic workloads and objectives**

Put reusable workload objects in `ops/sizing/profiles.json`; profiles reference them by ID so monitored variants cannot drift. All traffic uses synthetic workspaces and local deterministic providers with fixed seed `20260818`, model latency 250 ms, connector latency 100 ms, sandbox duration 2 seconds, zero injected failures, and no external network or billing.

`development-v1` has 1 active agent/workspace, 1 concurrent run, 1 concurrent sandbox, 120 API reads, 2 task creates, 4 nonsecret metadata mutations, 1 prepared manual retry, and 6 signed webhooks per minute. `small-v1` has 10 active agent/workspaces, 2 concurrent runs, 1 concurrent sandbox, and 600/6/30/3/30 of those operations per minute. `medium-v1` has 50 active agent/workspaces, 10 concurrent runs, 4 concurrent sandboxes, and 3,000/30/150/15/120 per minute. Subject-bearing traffic is round-robin over those synthetic workspaces/connections, so the test measures service capacity without unintentionally tripping the separately tested default abuse limits.

For each workload, API reads are exactly 45% task list, 25% task detail, 15% agent list, 10% protected operations summary, and 5% authenticated session read over every 100 requests. Task creates are the declared 2/6/30 arrivals per minute; other mutation and webhook streams retain their separate exact rates. Webhooks are valid signed unique deliveries, and prepared retries use distinct existing retryable fixtures. Each arriving task performs exactly three fake model activities and two local no-side-effect tool activities; every fifth task starts one constrained sandbox job. The open-loop scheduler uses integer nanosecond deadlines and fixed seed `20260818`; overload is measured, never hidden by slowing arrivals.

Profile resource inputs are equally exact. Development runs one replica of every product service and reserves per container: Caddy `0.05 CPU/64 MiB`, web `0.10/128`, API `0.20/256`, workflow-worker `0.10/128`, event-worker `0.10/128`, agent-worker `0.30/256`, tool-worker `0.25/256`, sandbox-runner `0.05/64`, PostgreSQL `0.25/640`, NATS `0.10/256`, and Temporal `0.25/384`. That totals `1.75 CPU / 2,560 MiB`; its one sandbox is capped at `1 CPU / 1,024 MiB`. API/agent/tool/event pool size/overflow pairs are respectively `3/2`, `3/2`, `3/2`, and `2/1`; with Temporal reserve 8 and operator reserve 10 the rendered maximum is 36 of `max_connections=100`, below the 70 boundary. Small and small-monitored use Task 5's one-replica `4.20 CPU / 5,888 MiB` product reservations, a `2 CPU / 4,096 MiB` sandbox cap, `max_connections=150`, and 61 rendered connections. Small monitoring adds Collector `0.25 CPU/256 MiB`, Prometheus `0.50/1,024`, Tempo `0.50/1,024`, and Grafana `0.25/512`.

Medium and medium-monitored use these exact **per-container** reservations: Caddy `0.10 CPU/128 MiB`, web `0.25/256`, API `0.25/320`, workflow-worker `0.10/128`, event-worker `0.10/128`, agent-worker `0.45/448`, tool-worker `0.45/320`, sandbox-runner `0.05/64`, PostgreSQL `1.25/2,048`, NATS `0.50/512`, and Temporal `1.00/1,536`. Medium uses two API, workflow, event, and sandbox-runner replicas plus five agent and five tool workers, so the rendered product reservation is exactly `8.60 CPU / 9,600 MiB`. Four sandboxes are each capped at `1 CPU / 4,096 MiB`; product plus all four caps is `12.60 CPU / 25,984 MiB`, below 80% of the 16-vCPU/32-GiB host. With pool pairs API `5/5`, agent `5/3`, tool `5/3`, event `3/2`, Temporal reserve 40, and operator reserve 20, the rendered maximum is 170 of `max_connections=300`, below the 210 alert boundary. Medium monitoring adds Collector `0.50/512`, Prometheus `0.75/2,048`, Tempo `0.75/2,048`, and Grafana `0.50/1,024`. Service limits are the exact Task 5 limits for small and exactly twice each per-container reservation for development/medium, except no limit may exceed host capacity.

Disk admission budgets are development 20 GiB product + 5 GiB logs/ephemeral; small 55 + 15; small-monitored 55 + 15 + 30 diagnostics; medium 150 + 45; and medium-monitored 150 + 45 + 100. `profiles.json` assigns each byte claim to the closed storage classes `product-volume`, `container-layer-log`, `bind-state`, `monitoring-volume`, `rootless-runtime`, and optional `object-store`; it never assumes those classes share a device. Backups are excluded only because the preflight proves their target is off-host. The 40/100/150/250/400-GiB baseline applies to the selected daemon's primary data filesystem, while every additional resolved filesystem must independently retain its assigned bytes plus 20% headroom. Deduplicated classes on the same filesystem are summed once; capacities on different filesystems are never added to disguise a full device. Measured growth must preserve that per-filesystem headroom.

Objectives are exact: development/small API p95 <= 500 ms, queue-wait p95 <= 5,000 ms, and Temporal activity p95 <= 2,000 ms; medium API p95 <= 750 ms, queue-wait p95 <= 10,000 ms, and activity p95 <= 3,000 ms. Every profile must accept at least 99.0% of scheduled API requests excluding expected prepared-retry responses, finish every 60/180/900 generated task respectively within the drain bound, preserve one outcome per idempotency key, and return NATS lag to zero.

- [ ] **Step 3: Collect resource and product measurements without cardinality or secrets**

`run_phase10_sizing.py` rejects a host below the profile's visible cgroup CPU/RAM or storage baseline before building. It reserves the configured sandbox memory cap times maximum concurrent sandboxes and proves service reservations plus that reserve leave at least 20% RAM. The runner does not fake host capacity through Docker limits.

Before admission, resolve a closed `StorageInventory` from the selected daemon and fully rendered profile. Record Docker Engine's `DockerRootDir`; its writable-layer and JSON-log backing; the mountpoint of every named volume; the canonical source of every writable or state/config bind; the rootless daemon data and runtime/socket roots when selected; every configured object-store capacity root or provider quota; and every PostgreSQL tablespace returned by `pg_tablespace_location`. Join each container path back to exactly one rendered mount and reject an unknown path, symlink, inaccessible `statvfs`, unsupported network filesystem without a quota adapter, unbounded external object store, undeclared tablespace, or daemon data root outside the inventory. Telemetry volumes participate only in monitored variants. The off-host backup target is checked separately and cannot satisfy product headroom.

Resolve a private stable filesystem identity from canonical realpath, `st_dev`, and `statvfs` for each backing, deduplicate equal filesystems, sum all assigned class bytes on each, and require both `(free_bytes - assigned_bytes) / total_bytes >= 0.20` and projected used percent strictly below 80. A filesystem is sampled even if it is not `/`; `/` is sampled only when an actual backing resolves there. Rootful and rootless runs resolve independently because their daemon data roots can differ. Tests cover plentiful `/` with an exhausted Docker data root, a low-space bind on a second filesystem, two named volumes deduplicating to one filesystem, an unmapped tablespace, an unavailable daemon root, and hostile absolute paths that must never reach evidence.

The runner records an immutable runtime-content fingerprint for every measured service. Before Task 12 it uses harness-resolved local test image IDs. With paired `--resolved-images ops/images/resolved-images.json --runtime-image-env PATH`, it invokes the Task 5 validation seam, requires every selected local ID to match the resolved host-platform child, and records the canonical resolved image-set hash rather than local daemon IDs. Task 12 must rerun every profile after pinning Dockerfiles so final evidence cannot describe different image content.

At ten-second monotonic intervals, collect aggregate service CPU and RSS from the Docker stats API; used/free/growth totals for every privately resolved backing filesystem; PostgreSQL active/max connections and database size totals; NATS durable consumer lag; Temporal scheduled-to-start/activity latency; running sandbox count; and protected health. Request histograms record throughput and API/queue p50/p95. Queries return counts/totals only. Public storage evidence contains only backing-filesystem count, all-resolved boolean, minimum free percent, maximum used percent, and maximum growth bytes across the inventory—never paths, mount names, filesystem/device IDs, daemon roots, container names/IDs, database/stream/workflow/workspace IDs, URLs, logs, labels, traces, or raw samples.

After warm-up, reset all histograms/counters used for evaluation, sample for exactly 1,800 seconds, stop arrivals, drain for at most 300 seconds, then evaluate. Every rolling six-sample CPU aggregate must be strictly below 80%; RAM and every backing-filesystem maximum must be below 80%; every backing must retain its assigned byte reserve and 20% free; PostgreSQL connection ratio must be below 70%; health failures must be zero; lag must not grow monotonically/nondecreasingly for 300 seconds and final lag must be zero; objectives and headroom must pass. A killed collector, unresolved backing, or missing sample fails rather than shortening the run.

- [ ] **Step 4: Build isolated profile overlays and dual-mode measurement CI**

`compose.phase10-sizing.yaml` takes only validator-owned resource variables selected from the canonical profile. Monitoring profiles enable the predecessor `observability` services and exact 15-day Prometheus/72-hour Tempo settings; unmonitored profiles do not create their containers or volumes. No profile publishes an internal port. A profile uses a new project, dynamic proxy ports, unique namespace/keyring, empty volumes, and its own fake-provider state; teardown follows Task 5.

`.github/workflows/phase10-sizing.yml` has a ten-cell matrix: the five exact profile IDs crossed with `rootful` and `rootless`. Each cell targets a dedicated self-hosted runner labelled for the profile baseline and socket mode, has a 60-minute job timeout, and disables cancellation overlap by profile/mode. The workflow sets `PHASE10_PROFILE` and `PHASE10_SOCKET_MODE` from the closed matrix. A rootful cell validates `/var/run/docker.sock`, sets `PHASE10_DOCKER_SOCKET=/var/run/docker.sock`, derives the exact positive `PHASE10_DOCKER_SOCKET_GID`, and removes rootless variables; a rootless cell validates UID-10001 ownership, sets `PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock`, and removes both GID variables. Every cell creates a private empty `DOCKER_CONFIG`, uses the runner-provided `RUNNER_TEMP`, and runs exactly:

```bash
uv sync --frozen --all-packages
uv run python scripts/run_phase10_sizing.py measure --profile "$PHASE10_PROFILE" --socket-mode "$PHASE10_SOCKET_MODE" --evidence "$RUNNER_TEMP/phase10-sizing.json"
uv run python scripts/run_phase10_sizing.py validate-evidence "$RUNNER_TEMP/phase10-sizing.json"
```

Only the validated allowlisted JSON shard is uploaded. An aggregation job requires all ten shards, verifies matching git/image/profile hashes and exactly 1,800 measured seconds each, and generates `docs/evidence/phase10-sizing.json` containing five profile entries with separate rootful/rootless outcomes. A release fails on a skipped, cancelled, undersized, stale, or duplicate cell.

- [ ] **Step 5: Write measured guidance and run GREEN**

The runbook publishes measured p50/p95, throughput, maxima, path-free backing-filesystem growth, and pass/fail for all five profiles; distinguishes provider limits from application capacity; reserves each sandbox cap in full; retains 20% RAM and 20% on every resolved backing filesystem; scales agent and tool activity slots independently; preserves one durable event consumer identity; and covers PostgreSQL/Temporal/NATS/product/log/backup/metrics/trace disk growth. Backups remain off-host. It defines alerts at 70% PostgreSQL connections, five minutes growing NATS lag, queue p95 objective breach, under 20% RAM, or under 20% on any backing filesystem.

Run the short contract/smoke first, then the mandatory full measurements:

```bash
uv run pytest tests/test_phase10_sizing_config.py tests/integration/test_phase10_sizing_smoke.py -q
phase10_sizing_tmp="$(mktemp -d)"
trap 'find "$phase10_sizing_tmp" -type f -delete; rmdir "$phase10_sizing_tmp"' EXIT
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootful --evidence "$phase10_sizing_tmp/${profile}-rootful.json"
done
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootless --evidence "$phase10_sizing_tmp/${profile}-rootless.json"
done
uv run python scripts/run_phase10_sizing.py aggregate --input-dir "$phase10_sizing_tmp" --output docs/evidence/phase10-sizing.json
uv run python scripts/run_phase10_sizing.py validate-evidence docs/evidence/phase10-sizing.json
uv run ruff check tests/load scripts/run_phase10_sizing.py tests/test_phase10_sizing_config.py tests/integration/test_phase10_sizing_smoke.py
uv run mypy tests/load/phase10_sizing.py scripts/run_phase10_sizing.py
```

Expected: PASS only after ten real 300+1,800-second runs on matching hosts. The unique temporary directory contains allowlisted JSON only and is removed by the trap; Compose projects/volumes/keys are gone before aggregation.

- [ ] **Step 6: Guard, stage exactly Task 11, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add Makefile ops/sizing/profiles.json compose.phase10-sizing.yaml tests/load/__init__.py tests/load/phase10_sizing.py scripts/run_phase10_sizing.py tests/test_phase10_sizing_config.py tests/integration/test_phase10_sizing_smoke.py docs/operations/resource-sizing.md docs/evidence/phase10-sizing.json .github/workflows/phase10-sizing.yml
git diff --cached --check
git commit -m "perf: publish measured capacity profiles"
```

Expected: commit 13 of 15; workload identity, five measured profiles, monitored deltas, objectives, raw-runtime bounds, and safe evidence are one reviewable unit.

### Task 12: Build and Scan Every Release Image on AMD64 and ARM64

**Files:**
- Modify: `.env.example`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `compose.yaml`
- Modify: `docker/python.Dockerfile`
- Modify: `docker/sandbox.Dockerfile`
- Modify: `docker/operations.Dockerfile`
- Modify: `docker/monitoring.Dockerfile`
- Modify: `apps/web/Dockerfile`
- Create: `ops/images/release-images.json`
- Create: `ops/images/resolved-images.json`
- Create: `ops/security/vulnerability-allowlist.json`
- Create: `scripts/build_phase10_images.py`
- Create: `scripts/evaluate_phase10_vulnerabilities.py`
- Modify: `scripts/assert_phase10_command_inventory.py`
- Create: `scripts/record_phase10_hardening_evidence.py`
- Create: `tests/test_phase10_image_matrix.py`
- Create: `tests/test_phase10_vulnerability_policy.py`
- Modify: `tests/test_phase10_command_inventory.py`
- Create: `tests/test_phase10_hardening_evidence.py`
- Create: `docs/operations/image-security.md`
- Create: `docs/evidence/phase10-image-security.json`
- Modify: `docs/evidence/phase10-sizing.json`
- Modify: `.github/workflows/phase10-sizing.yml`
- Create: `.github/workflows/release-security.yml`

**Interfaces:**
- Consumes: all repository Dockerfiles, Task 10 compatibility versions, Task 11 runtime fingerprints, locked Python/pnpm graphs, Docker Buildx/QEMU, pinned pip-audit/Trivy scanner versions, and private temporary OCI archives.
- Produces: exact release-image manifest, per-platform resolved digests, multi-architecture OCI/SBOM/provenance proof, dependency/container scan policy, expiring exact high-severity allowances, digest-only production Compose inputs, final hardening-evidence aggregator CLI, refreshed sizing evidence for the pinned images, and safe release evidence.

- [ ] **Step 1: Write RED inventory, pinning, architecture, nonroot, and vulnerability-policy tests**

The image inventory must list exactly thirteen repository-owned builds: API, workflow-worker, event-worker, agent-worker, tool-worker, sandbox-runner, web, sandbox-job, operations, Collector, Prometheus, Tempo, and Grafana. The two rootless transports deliberately reuse the sandbox-runner image and do not create a fourteenth build. Each entry specifies Dockerfile, target/build args, expected nonzero UID, and platforms exactly `linux/amd64` and `linux/arm64`. Tests also derive every `build:`/repo image from rendered Compose and fail on missing or extra inventory entries. The resolved external inventory separately includes exactly ten versioned roles: Caddy; PostgreSQL current `17.11-alpine` and component source `16.15-alpine`; NATS current `2.12.11-alpine` and component source `2.11.0-alpine`; Temporal current server and matching admin-tools `1.29.7`; Temporal component-source server and matching admin-tools `1.28.1`; and Temporal UI `2.53.3`. All ten must resolve fresh manifest/child digests and pass scans for both exact platforms before an ARM64 release is supported. Production uses the six current/UI roles; the other four are retained solely for reviewed upgrade/recovery rehearsals.

Pin the allowance schema with no wildcard behavior:

```python
def test_allowance_requires_exact_unexpired_identity() -> None:
    allowance = VulnerabilityAllowance.model_validate(
        {
            "finding_id": "CVE-2099-12345",
            "component": "pkg:pypi/example@1.2.3",
            "architecture": "linux/arm64",
            "owner": "security@example.com",
            "rationale": "Upstream fix is unavailable; the vulnerable path is unreachable.",
            "expires_on": "2099-09-30",
        }
    )
    assert allowance.matches(
        finding_id="CVE-2099-12345",
        component="pkg:pypi/example@1.2.3",
        architecture="linux/arm64",
        evaluated_on=date(2099, 9, 1),
    )
    assert not allowance.matches(
        finding_id="CVE-2099-12345",
        component="pkg:pypi/example@1.2.4",
        architecture="linux/arm64",
        evaluated_on=date(2099, 9, 1),
    )
```

Also assert critical findings can never be allowed, a high finding needs exactly one matching entry, expiry is strictly after evaluation date, scanner errors/unknown severity fail, and public evidence contains no raw scanner description/path/layer/history/environment.

Run:

```bash
uv run pytest tests/test_phase10_image_matrix.py tests/test_phase10_vulnerability_policy.py tests/test_phase10_hardening_evidence.py -q
```

Expected: FAIL because inventory/resolution, pinned Dockerfiles, builders, policy, runbook, workflow, and evidence do not exist.

- [ ] **Step 2: Pin multi-stage Dockerfiles and exact release inputs**

Replace floating `FROM`/`COPY --from` references with manifest-digest build arguments whose safe defaults are the reviewed values in `ops/images/resolved-images.json`. Record exact release epoch `SOURCE_DATE_EPOCH=1787011200` (2026-08-18 00:00:00 UTC) in `release-images.json` and make the image config/layer build reproducible across the multi-platform and later native-only paths; a future release changes that value only in a reviewed inventory update. Keep build stages network-independent after locked dependency fetch, remove package-manager caches, and retain the existing nonroot runtime identities. Python images copy only the selected service and runtime dependencies; the operations target copies only its locked tools, required packages, and allowlisted executable scripts and explicitly excludes docs, evidence, tests, Makefile, README, and workflows. Web copies Next standalone output; sandbox-job remains UID/GID 1000 without daemon socket; Jhin services and operations remain UID 10001. Monitoring wrappers preserve predecessor health commands and nonroot identity where supported.

`release-images.json` fixes schema version, thirteen image keys, Dockerfile/context/target/build args, expected UID, command probe, and both platforms. `resolved-images.json` records each base, tool, scanner, all ten third-party runtime/rehearsal roles, and every resulting repository image as a manifest digest plus distinct amd64/arm64 child digests. It rejects PostgreSQL current/source patches below 17.11/16.15, mismatched Temporal server/admin-tools versions, and any production reference to auto-setup. The command contract is deliberately two-phase inside Task 12: `resolve` atomically writes freshly validated external/base inputs with `state="inputs_resolved"` and no repository result entries; `build` accepts only that state, supplies those exact inputs to all Dockerfiles, derives the thirteen OCI manifest/child digests, and atomically rewrites the same path with `state="complete"` and the exact result set. Production Compose, evidence, staging, and the final validator accept only `complete`; a failed build leaves no apparently complete file. A resolver rejects tag-only values, duplicate platform entries, any digest outside `sha256:[0-9a-f]{64}`, a manifest whose children differ from current recorded registry content, or a digest not valid for the declared upstream version.

Production Compose accepts only validated `*_IMAGE` variables rendered by `build_phase10_images.py compose-env --resolved ops/images/resolved-images.json --output "$phase10_compose_env"` into a mode-`0600` ephemeral file after the caller defines `phase10_compose_env`; every value matches `[^@]+@sha256:[0-9a-f]{64}`. Production startup rejects a tag or missing value. Development may use locally built names only through `compose.dev.yaml`.

Task 12 adds the pinned Trivy binary and `record_phase10_hardening_evidence.py` to the nonroot operations image, then extends `assert_phase10_command_inventory.py`/its test to execute `trivy --version` and the recorder's `--help` as UID 10001 on both architectures. The existing Task 5 inventory remains the authority for every other backup/restore/upgrade/runbook command.

- [ ] **Step 3: Build both architectures and inspect the OCI results**

For each inventory entry, execute Buildx with argv, no shell:

```bash
set -euo pipefail
phase10_api_oci="$(mktemp -d)"
trap 'find "$phase10_api_oci" -type f -delete; rmdir "$phase10_api_oci"' EXIT
docker buildx build --platform linux/amd64,linux/arm64 --provenance=mode=max --sbom=true --output "type=oci,dest=$phase10_api_oci/jhin-api.oci.tar" --file docker/python.Dockerfile --build-arg SERVICE_PACKAGE=jhin-api .
```

The shown API invocation is one concrete inventory expansion; the script supplies a unique private temporary destination for all thirteen and removes it in `finally`. It parses the OCI index/config/attestation blobs directly with bounded `tarfile` reads, verifies one runnable manifest per exact platform, expected nonzero user, command probe, no Docker socket mount metadata, no secret-like environment names/values, and both SBOM/provenance attestations. It then executes each architecture under QEMU for the inventory's harmless command probe; an unsupported/emulated-start failure fails the build. Only after all thirteen pass does `build` promote `resolved-images.json` from `inputs_resolved` to `complete`; tests kill the builder after image 12 and prove the final-state promotion did not occur.

`prepare-runtime` takes the complete resolved file, exact socket mode, and output path. With `--oci-dir`, it imports only the selected host-platform child of each repository OCI archive into that validated daemon. Without `--oci-dir`, it performs a native-only rebuild from the reviewed tree and exact resolved inputs/release epoch, then requires its config/layer/child digest to equal the recorded child; this is Task 13's clean-host path. In both forms it pulls each external runtime by manifest digest, verifies its selected child, and writes a mode-`0600` closed-key Compose image env whose values are immutable local `sha256:` IDs. It rejects an architecture mismatch, nonreproducible build, unknown/preexisting mismatched tag, caller image override, wrong daemon, or partial import and deletes the env on failure. The file is test-only, authority-specific, contains no registry credential, is never checked in/uploaded, and is the sole source accepted by Task 5's paired runtime seam.

The builder never uploads OCI archives, layer lists, configs, SBOMs, provenance, or raw inspection output. Public evidence records only image key, manifest digest, two child digests, expected UID matched, SBOM/provenance booleans, probe boolean, and status.

- [ ] **Step 4: Run locked dependency scans and per-architecture container scans**

Add and lock `pip-audit==2.9.0` in the development group. The evaluator invokes `uv export`, installed `pip-audit`, repository `pnpm@10.25.0 audit`, and the pinned operations-image `trivy image` command directly as argv and parses JSON from bounded stdout in memory. Trivy is run with scanners `vuln`, severities `HIGH,CRITICAL`, `--ignore-unfixed=false`, and each exact `linux/amd64` and `linux/arm64` platform against every repository OCI archive and all ten external runtime/rehearsal digests, including both matching admin-tools images.

The script owns unique mode-`0600` temporary requirements/OCI paths and removes them; the inventory loop covers every image. It checks scanner versions against resolved pins before scanning. A critical dependency or container finding always fails. A high finding passes only on exact finding ID + package URL/component version + architecture + unexpired repository allowance with owner and rationale; unused, duplicate, malformed, or expired allowances fail. OS/library fixed-version metadata is evaluated but never copied to public output.

- [ ] **Step 5: Add fail-closed release CI and safe evidence**

`.github/workflows/release-security.yml` runs for pull requests that change release inputs, every protected release candidate, and a daily scheduled dependency/runtime rescan. It uses a private Docker config and dedicated Buildx builder, installs locked uv/pnpm dependencies, enables QEMU for only amd64/arm64, verifies resolver registry content, builds all thirteen OCI archives under runner temporary storage, scans them plus all ten external runtime/rehearsal images per architecture, generates the allowlisted evidence, and destroys builder/temp files in an `if: always()` step. It never pushes on pull requests or schedules and never uploads archives, SBOMs, provenance, raw scan reports, Docker config, logs, or environment. Tag release publishing is a separately authorized job that pushes the already verified manifest digests and verifies registry digest equality before promotion.

Update `.github/workflows/phase10-sizing.yml` so every matrix cell runs `prepare-runtime` for its exact socket authority into runner-temporary mode-`0600` state, passes the paired resolved/runtime paths to `measure`, and always deletes the runtime env. Its aggregation job requires the complete resolved image-set hash in all ten shards. It never falls back to Task 11's pre-pinning local-test-image mode once `resolved-images.json` is complete.

The checked-in evidence contains scan date, commit, scanner versions, counts by ecosystem/severity/architecture, allowance finding IDs with expiration only, thirteen two-platform build summaries, and pass/fail. No finding description, package path, image config/history/layer, registry credential, absolute path, container ID, environment value, or raw report is permitted.

Implement `record_phase10_hardening_evidence.py` and its complete schema/redaction/determinism tests here, before building the operations image. It accepts the settled rate-limit/backup/restore/upgrade/sizing/image evidence schemas plus profile/version/image/allowlist/config paths, requires canonical runtime-image hashes, and exposes the final `--output`/`--check` CLI consumed in Task 13. Fixture tests cover missing mode, stale image set, RPO/RTO failure, critical/high policy failure, hostile extra/sensitive fields, and byte-for-byte deterministic Markdown. Task 13 supplies final live inputs and documentation but does not change this runtime file after image digests are recorded.

- [ ] **Step 6: Run GREEN, full builds/scans, and production digest validation**

```bash
set -euo pipefail
phase10_oci_dir="$(mktemp -d)"
phase10_resized_dir="$(mktemp -d)"
trap 'find "$phase10_oci_dir" "$phase10_resized_dir" -type f -delete; rmdir "$phase10_oci_dir" "$phase10_resized_dir"' EXIT
uv lock
uv run pytest tests/test_phase10_image_matrix.py tests/test_phase10_vulnerability_policy.py tests/test_phase10_hardening_evidence.py -q
uv run python scripts/build_phase10_images.py resolve --inventory ops/images/release-images.json --output ops/images/resolved-images.json
uv run python scripts/build_phase10_images.py build --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --output-dir "$phase10_oci_dir"
uv run python scripts/evaluate_phase10_vulnerabilities.py scan --oci-dir "$phase10_oci_dir" --allowlist ops/security/vulnerability-allowlist.json --evidence docs/evidence/phase10-image-security.json
uv run python scripts/build_phase10_images.py validate-evidence docs/evidence/phase10-image-security.json
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --oci-dir "$phase10_oci_dir" --socket-mode rootful --output "$phase10_oci_dir/rootful-runtime.env"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --oci-dir "$phase10_oci_dir" --socket-mode rootless --output "$phase10_oci_dir/rootless-runtime.env"
uv run python scripts/assert_phase10_production_compose.py
uv run python scripts/assert_phase10_command_inventory.py
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootful --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_oci_dir/rootful-runtime.env" --evidence "$phase10_resized_dir/${profile}-rootful.json"
done
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootless --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_oci_dir/rootless-runtime.env" --evidence "$phase10_resized_dir/${profile}-rootless.json"
done
uv run python scripts/run_phase10_sizing.py aggregate --input-dir "$phase10_resized_dir" --output docs/evidence/phase10-sizing.json
uv run python scripts/run_phase10_sizing.py validate-evidence docs/evidence/phase10-sizing.json
uv run ruff check scripts/build_phase10_images.py scripts/evaluate_phase10_vulnerabilities.py scripts/assert_phase10_command_inventory.py scripts/record_phase10_hardening_evidence.py tests/test_phase10_image_matrix.py tests/test_phase10_vulnerability_policy.py tests/test_phase10_command_inventory.py tests/test_phase10_hardening_evidence.py
uv run mypy scripts/build_phase10_images.py scripts/evaluate_phase10_vulnerabilities.py scripts/assert_phase10_command_inventory.py scripts/record_phase10_hardening_evidence.py
```

Expected: PASS with thirteen built images and ten external runtime/rehearsal images, two runnable architectures each, fresh PostgreSQL 17.11/16.15 and matching Temporal server/admin-tools digest pinning, SBOM/provenance for repository builds, locked dependency scans, forty-six architecture-specific container scans, zero critical findings, and every high finding exactly justified or removed. The repeated ten sizing runs bind capacity evidence to the final pinned production runtime images; any Dockerfile/runtime drift must be remeasured before commit.

- [ ] **Step 7: Guard, stage exactly Task 12, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
test -z "$(git diff --cached --name-only)"
git add .env.example pyproject.toml uv.lock compose.yaml docker/python.Dockerfile docker/sandbox.Dockerfile docker/operations.Dockerfile docker/monitoring.Dockerfile apps/web/Dockerfile ops/images/release-images.json ops/images/resolved-images.json ops/security/vulnerability-allowlist.json scripts/build_phase10_images.py scripts/evaluate_phase10_vulnerabilities.py scripts/assert_phase10_command_inventory.py scripts/record_phase10_hardening_evidence.py tests/test_phase10_image_matrix.py tests/test_phase10_vulnerability_policy.py tests/test_phase10_command_inventory.py tests/test_phase10_hardening_evidence.py docs/operations/image-security.md docs/evidence/phase10-image-security.json docs/evidence/phase10-sizing.json .github/workflows/phase10-sizing.yml .github/workflows/release-security.yml
git diff --cached --check
git commit -m "build: gate multiarch release images"
```

Expected: commit 14 of 15; image content, architecture parity, pinning, dependency/container findings, allowances, and public evidence are reviewed before production runbooks close this subproject.

### Task 13: Publish Production Runbooks and Gate Every Executable Drill

**Files:**
- Modify: `Makefile`
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/phase10-operations.yml`
- Modify: `docs/evidence/phase10-backup.json`
- Modify: `docs/evidence/phase10-rate-limits.json`
- Modify: `docs/evidence/phase10-restore.json`
- Modify: `docs/evidence/phase10-upgrades.json`
- Create: `docs/operations/production-deployment.md`
- Create: `docs/operations/production-readiness.md`
- Create: `tests/test_phase10_operations_docs.py`
- Create: `tests/test_phase10_runbook_commands.py`
- Create: `docs/evidence/phase10-hardening.md`

**Interfaces:**
- Consumes: Tasks 1–12 commands/evidence, Task 12 complete resolved image set and `prepare-runtime`, all five predecessor completion evidence sets, protected readiness, one-entrypoint Compose model, and separate rootful/rootless operations hosts.
- Produces: refreshed dual-mode operations evidence bound to final image content, indexed deployment/readiness runbooks, executable Make/CI gates, drill cadence and ownership, deterministic allowlisted hardening summary, a sub-project-6 completion decision, and explicit handoff to the still-separate secret-audit/chaos plan.

- [ ] **Step 1: Write RED runbook completeness, command execution, evidence, and handoff tests**

Require every operational domain and its negative/recovery path:

```python
def test_readiness_covers_every_hardening_domain() -> None:
    text = READINESS.read_text(encoding="utf-8")
    required = {
        "PostgreSQL globals and every non-template database",
        "Temporal history and visibility",
        "NATS JetStream streams, consumers, and retained messages",
        "separately protected master-key artifact",
        "restore into fresh empty volumes",
        "application rollback keeps additive schema",
        "Phase 9 rollback requires legacy version 1 key material",
        "post-retirement rollback requires a keyring-capable image",
        "PostgreSQL downgrade requires restore",
        "Temporal schema downgrade requires restore",
        "uncertain NATS storage downgrade requires restore",
        "linux/amd64",
        "linux/arm64",
        "secret-audit/chaos plan remains outstanding",
    }
    assert all(phrase in text for phrase in required)


def test_hardening_summary_rejects_sensitive_fields() -> None:
    summary = build_summary(load_validated_inputs())
    forbidden = (
        "dsn",
        "password",
        "secret_value",
        "key_path",
        "container_id",
        "volume_id",
        "raw_log",
    )
    lowered = summary.lower()
    assert all(field not in lowered for field in forbidden)
```

`tests/test_phase10_runbook_commands.py` parses every fenced `bash` block in the nine Phase 10 operations documents. Each block begins with exactly one `# phase10-command: static`, `# phase10-command: drill`, or `# phase10-command: operator` annotation. Static blocks execute in a scrubbed temporary environment. Drill blocks map to a tested Make target and execute in fixture mode during integration CI. Operator blocks are argv-parsed, checked against the operations-image inventory, and must define/check every variable before use; destructive/cutover commands require an immediately preceding explicit confirmation predicate. Reject `git add .`, `set -x`, `env`, `printenv`, secret `cat`/`echo`, live data-directory copies, floating images, default Compose project, unbounded waits, and fixed test ports.

Run:

```bash
uv run pytest tests/test_phase10_operations_docs.py tests/test_phase10_runbook_commands.py tests/test_phase10_hardening_evidence.py -q
```

Expected: FAIL because final deployment/readiness documents, audited command targets, and hardening summary do not exist and the operations evidence is not yet refreshed against Task 12's complete image-set hash. The already-green Task 12 aggregator rejects those stale/missing final inputs.

- [ ] **Step 2: Publish one exact deployment path for each socket mode**

`production-deployment.md` begins with host capacity/profile selection, immutable image resolution, PostgreSQL 17.11-or-newer policy validation, matching Temporal server/admin-tools resolution, three separate backup recipient sets, owner-only keyring/Temporal TLS files, exact public origin/proxy addresses, off-host backup destination, DNS/TLS readiness, and validated rootful or rootless daemon authority. It then renders production Compose, proves only Caddy publishes, validates configuration without starting, takes/verifies a backup for an existing deployment, creates/attaches Caddy without starting it, records its private resolved address, starts PostgreSQL, runs the explicit one-shot Temporal history then visibility schema setup/update from matching admin-tools when initializing (or only reviewed updates during an upgrade), proves admin-tools exited zero, starts NATS -> Temporal server -> maintenance-mode API/workers -> web, proves API pinned that same proxy address, then starts Caddy and verifies protected readiness/pollers/consumers/rate limits/security headers before enabling DNS/firewall cutover. It proves the Temporal server has no schema-admin credential and created no schema object at startup. It completes one synthetic durable task before declaring ready. Caddy is the supported reference configuration; an alternate Traefik/Nginx-compatible proxy is admissible only after the runbook's equivalence table and the same black-box proxy suite prove exact single entrypoint/routing, TLS and renewal, redirect, forwarded-header overwrite/trust, header/CSP preservation, body/time/streaming behavior, zero mutation retries, health behavior, and admin isolation.

Rootful commands use `/var/run/docker.sock`, re-stat it, derive its exact positive numeric GID, pass `PHASE10_DOCKER_SOCKET_GID`, and never chmod/chown it. Rootless host commands use the already-running `/run/user/10001/docker.sock`, verify UID 10001 ownership and daemon `name=rootless`, prove `net.ipv4.ip_unprivileged_port_start <= 80`, and run the exact disposable cgroup-v2 memory/CPU/PID enforcement probe before setting `PHASE10_ROOTLESS_DOCKER_SOCKET` and omitting all GID variables. They bootstrap the two private adapters; operations and sandbox-runner remain UID 10001 and receive only their separate internal TCP endpoints, never the socket or root. No runbook changes a sysctl/cgroup/socket permission or permits a rootful fallback. Both modes set `COMPOSE_DISABLE_ENV_FILE=1`, a concrete `jhin-production` project only for operator deployment, an owner-only `/etc/jhin/compose.env`, and a private Docker config; live drill commands continue to use generated project names, ports, volumes, namespaces, keys, and fake providers. No command displays rendered secret environment or Docker inspection/log output.

The runbook covers automatic Caddy issuance/renewal and an isolated renewal dry run; HTTP redirect; exact forwarding behavior; same-origin CSP nonce/HSTS/cookie/CORS checks; body/time/streaming limits; admin UI isolation; service CPU/memory/PID/log limits; 20% headroom; safe structured-log query fields; start/stop/recreate and Task 4's mandatory maintenance-protected `proxy-roll`; maintenance entry/exit; certificate and external Temporal certificate rotation; rate-limit inspection/reset via protected stdin; queue/consumer/timer checks; off-host backup/prune; fresh restore/cutover-check; state-specific legacy-v1 versus post-retirement keyring-capable application rollback; upgrades/recovery; image scans; and sizing selection. Every rollback stops at a stated authority boundary and preserves maintenance on ambiguity.

- [ ] **Step 3: Publish the production readiness checklist, evidence map, cadence, and ownership**

`production-readiness.md` maps each binding acceptance claim to exact command, evidence file, pass criterion, owner role, cadence, abort state, and recovery runbook. Minimum cadence is: encrypted backup daily and immediately before every component/key upgrade; backup verification each creation; restore drill before every release candidate and quarterly; rate-limit WAL/migration drill each release; application/infrastructure upgrade rehearsal before adopting a version; TLS renewal test monthly; vulnerability/dependency/image scan on every release candidate and daily; full five-profile sizing on release candidate and on image/resource/hardware changes; proxy/config/runbook static gates on every PR.

State the objectives exactly: scheduled disaster RPO is <= 86,400 seconds, maintenance-window consistency RPO is zero product-writer commits after the closed writer/session/NATS quiescence point, private backup evidence records PostgreSQL WAL fences without treating them as a cross-product sequence, public evidence records only the zero-gap result, restore RTO is <= 7,200 seconds, and all sizing thresholds/objectives from Task 11 are mandatory. Include incident decision tables for corrupted/missing backup component, missing master key, failed PostgreSQL logical upgrade, Temporal schema failure, NATS storage uncertainty, rate-limit primary outage/lock timeout, proxy certificate failure, any backing-filesystem exhaustion, vulnerability exception expiry, and incomplete drill teardown.

The checklist marks tool-worker, telemetry, protected health, DLQ/retry, master-key rotation, and this runbooks/hardening subproject as prerequisites/evidence only. It does not duplicate their internals or mark Phase 10 complete. Its final unchecked gate states exactly `secret-audit/chaos plan remains outstanding` and links the binding design rather than a not-yet-authored sibling plan.

- [ ] **Step 4: Add executable Make/CI drill gates with bounded cleanup**

Add phony Make targets for `phase10-static`, `phase10-proxy-drill`, `phase10-rate-limit-drill`, `phase10-backup-restore-drill`, `phase10-upgrade-drill`, `phase10-sizing-evidence-check`, `phase10-image-security`, and `phase10-hardening-evidence`. Targets invoke only the Python/pytest entrypoints already tested; every live target requires `PHASE10_SOCKET_MODE`, `PHASE10_RESOLVED_IMAGES`, `PHASE10_RUNTIME_IMAGE_ENV`, and an absolute private `PHASE10_EVIDENCE_PREFIX`, and never defaults any of them. They pass the paired image paths to every runner. The rate, backup, restore, and upgrade targets atomically write respectively `${PHASE10_EVIDENCE_PREFIX}-rate-limit.json`, `-backup.json`, `-restore.json`, and `-upgrades.json`, each with the canonical resolved image-set hash and one closed mode. `phase10-proxy-drill` runs the Task 5 proxy-security and maintenance integration pair with test-only certificates/dynamic ports. The combined backup/restore target keeps encrypted artifacts only in one private temporary directory for the duration of the job and removes it after both projects have torn down.

`.github/workflows/ci.yml` runs static Compose/config, production settings, runbook-command, migration-graph, evidence-schema, image-policy, and unit suites on every PR. `.github/workflows/phase10-operations.yml` has a rootful/rootless matrix for live proxy/TLS/maintenance, WAL rate-limit, combined backup/restore, and all component upgrades on dedicated self-hosted hosts; each cell first runs `prepare-runtime` for its exact authority, has a 60-minute job deadline, always-run project-labelled teardown/runtime-env verification, and uploads only its schema-validated public mode shard when that drill has evidence. A required aggregation job rejects a missing, duplicate, failed, or cancelled mode, emits the same deterministic dual-mode schemas checked into the repository, and uploads only those aggregates. It schedules the isolated TLS renewal/proxy drill monthly, the full restore drill quarterly, and supports a protected release-candidate dispatch. Task 11 sizing and Task 12 image workflows remain their dedicated long/security gates.

These are the exact local per-mode live invocations; the release gate runs both and aggregates their safe shards as Step 6 shows. Each drill itself creates collision-free projects and enforces bounded teardown:

```bash
# phase10-command: drill
set -euo pipefail
phase10_gate_tmp="$(mktemp -d)"
trap 'find "$phase10_gate_tmp" -type f -delete; rmdir "$phase10_gate_tmp"' EXIT
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --socket-mode rootful --output "$phase10_gate_tmp/runtime.env"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" PHASE10_RESOLVED_IMAGES=ops/images/resolved-images.json PHASE10_RUNTIME_IMAGE_ENV="$phase10_gate_tmp/runtime.env" PHASE10_EVIDENCE_PREFIX="$phase10_gate_tmp/rootful" make phase10-proxy-drill phase10-rate-limit-drill phase10-backup-restore-drill phase10-upgrade-drill
```

```bash
# phase10-command: drill
set -euo pipefail
phase10_gate_tmp="$(mktemp -d)"
trap 'find "$phase10_gate_tmp" -type f -delete; rmdir "$phase10_gate_tmp"' EXIT
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --socket-mode rootless --output "$phase10_gate_tmp/runtime.env"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock PHASE10_RESOLVED_IMAGES=ops/images/resolved-images.json PHASE10_RUNTIME_IMAGE_ENV="$phase10_gate_tmp/runtime.env" PHASE10_EVIDENCE_PREFIX="$phase10_gate_tmp/rootless" make phase10-proxy-drill phase10-rate-limit-drill phase10-backup-restore-drill phase10-upgrade-drill
```

Before running, each target verifies socket type/ownership/connectivity and a private empty Docker config. A failed `finally` teardown or any remaining resource bearing the exact generated project label fails the gate; cleanup never queries or removes an unrelated/default project.

- [ ] **Step 5: Generate deterministic, allowlisted hardening evidence**

`record_phase10_hardening_evidence.py` loads and schema-validates the rate-limit, backup, restore, upgrade, sizing, and image-security evidence. It requires all statuses pass and exact rootful/rootless results for every live operations artifact; every operations/sizing runtime-set hash equal the complete `resolved-images.json`; `0017 -> 0018`; physical WAL; zero oversubscription; closed writer/session/NATS quiescence; complete component classes; collision-free globals bootstrap removed; fresh restore/no migration; both restore totals <= 7,200 seconds; PostgreSQL current/source at least 17.11/16.15; separate Phase 9/current-infrastructure and component-major fixtures; explicit matching Temporal admin-tools with no auto-setup/startup mutation; `app_rollback_legacy_v1`, `app_rollback_keyring_capable`, and `incompatible_key_format_rejected`; all PostgreSQL/Temporal/NATS recovery booleans; five exact sizing profiles with rootful/rootless 1,800-second results and all backing filesystems resolved; thirteen repository images plus ten external roles/two architectures and forty-six container scans; zero criticals; and valid high allowances. It hashes each input file and the exact profile/version/image/allowlist/config files, so stale inputs fail when policy changes.

The generated Markdown contains only source filename, source SHA-256, the canonical resolved image-set content hash, safe versions/counts/durations/booleans, result, cadence, and next gate. It rejects DSN/URL/IP/path/runtime resource ID, database/stream/workflow/workspace names, ciphertext/plaintext hash, secret/key/certificate material, raw findings, raw metrics/logs/traces, and any field outside its closed schema. Generate and verify byte-for-byte determinism:

```bash
# phase10-command: static
set -euo pipefail
uv run python scripts/record_phase10_hardening_evidence.py --output docs/evidence/phase10-hardening.md
uv run python scripts/record_phase10_hardening_evidence.py --check docs/evidence/phase10-hardening.md
```

- [ ] **Step 6: Run GREEN and the complete sub-project-6 release gate**

```bash
uv run pytest tests/test_phase10_operations_docs.py tests/test_phase10_runbook_commands.py tests/test_phase10_hardening_evidence.py -q
make phase10-static
make phase10-sizing-evidence-check
make phase10-image-security
uv run pytest -m 'not integration' -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test
pnpm --filter jhin-web build
phase10_final_tmp="$(mktemp -d)"
trap 'find "$phase10_final_tmp" -type f -delete; rmdir "$phase10_final_tmp"' EXIT
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --socket-mode rootful --output "$phase10_final_tmp/rootful-runtime.env"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --socket-mode rootless --output "$phase10_final_tmp/rootless-runtime.env"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rootful_gid" PHASE10_RESOLVED_IMAGES=ops/images/resolved-images.json PHASE10_RUNTIME_IMAGE_ENV="$phase10_final_tmp/rootful-runtime.env" PHASE10_EVIDENCE_PREFIX="$phase10_final_tmp/rootful" make phase10-proxy-drill phase10-rate-limit-drill phase10-backup-restore-drill phase10-upgrade-drill
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock PHASE10_RESOLVED_IMAGES=ops/images/resolved-images.json PHASE10_RUNTIME_IMAGE_ENV="$phase10_final_tmp/rootless-runtime.env" PHASE10_EVIDENCE_PREFIX="$phase10_final_tmp/rootless" make phase10-proxy-drill phase10-rate-limit-drill phase10-backup-restore-drill phase10-upgrade-drill
uv run python scripts/record_phase10_rate_limit_evidence.py aggregate --rootful "$phase10_final_tmp/rootful-rate-limit.json" --rootless "$phase10_final_tmp/rootless-rate-limit.json" --output docs/evidence/phase10-rate-limits.json
uv run python scripts/phase10_backup.py aggregate-evidence --rootful "$phase10_final_tmp/rootful-backup.json" --rootless "$phase10_final_tmp/rootless-backup.json" --output docs/evidence/phase10-backup.json
uv run python scripts/phase10_restore.py aggregate-evidence --rootful "$phase10_final_tmp/rootful-restore.json" --rootless "$phase10_final_tmp/rootless-restore.json" --output docs/evidence/phase10-restore.json
uv run python scripts/phase10_upgrade.py aggregate-evidence --rootful "$phase10_final_tmp/rootful-upgrades.json" --rootless "$phase10_final_tmp/rootless-upgrades.json" --output docs/evidence/phase10-upgrades.json
make phase10-hardening-evidence
git diff --check
```

Expected: PASS. The full sizing evidence already represents ten real long runs and is freshness-checked rather than repeated serially here; every other live operation runs in both socket modes and leaves no labelled resources or secret artifacts.

- [ ] **Step 7: Run final schema, security-output, Compose, command, and scope audits**

```bash
set -euo pipefail
test "$(uv run python -c 'from alembic.script import ScriptDirectory; from jhin_db.migrate import alembic_config; print(ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head())')" = "0018"
migration_file=packages/db/src/jhin_db/alembic/versions/20260818_0018_rate_limit_bucket.py
test "$(rg -c '^revision: str = "0018"$' "$migration_file")" = "1"
test "$(rg -c '^down_revision: str \| None = "0017"$' "$migration_file")" = "1"
uv run python scripts/assert_phase10_production_compose.py
uv run python scripts/assert_phase10_command_inventory.py
test "$(uv run python -c 'import json; v=json.load(open("ops/versions.json", encoding="utf-8")); print(v["component_rehearsals"]["postgres"]["source"], v["component_rehearsals"]["postgres"]["target"])')" = "16.15-alpine 17.11-alpine"
! rg -n 'temporalio/auto-setup|16\.14-alpine|17\.10-alpine' compose.yaml compose.operations.yaml compose.rootful.yaml compose.rootless.yaml ops/versions.json ops/images/resolved-images.json
uv run python scripts/record_phase10_rate_limit_evidence.py --check docs/evidence/phase10-rate-limits.json
uv run python scripts/phase10_backup.py validate-evidence docs/evidence/phase10-backup.json
uv run python scripts/phase10_restore.py validate-evidence docs/evidence/phase10-restore.json
uv run python scripts/phase10_upgrade.py validate-evidence docs/evidence/phase10-upgrades.json
uv run python scripts/run_phase10_sizing.py validate-evidence docs/evidence/phase10-sizing.json
uv run python scripts/build_phase10_images.py validate-evidence docs/evidence/phase10-image-security.json
uv run python scripts/record_phase10_hardening_evidence.py --check docs/evidence/phase10-hardening.md
! rg -n 'postgresql://[^[:space:]/]+:[^@[:space:]]+@|AGE-SECRET-KEY-[A-Z0-9]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|password=[^"$][^[:space:]]*|[Aa]uthorization: (Bearer|Basic) [A-Za-z0-9]|[Cc]ookie: [^$[:space:]]|container_id|volume_id|raw_log' docs/evidence/phase10-rate-limits.json docs/evidence/phase10-backup.json docs/evidence/phase10-restore.json docs/evidence/phase10-upgrades.json docs/evidence/phase10-sizing.json docs/evidence/phase10-image-security.json docs/evidence/phase10-hardening.md docs/operations/rate-limits.md docs/operations/backup.md docs/operations/restore.md docs/operations/external-temporal.md docs/operations/upgrades.md docs/operations/resource-sizing.md docs/operations/image-security.md docs/operations/production-deployment.md docs/operations/production-readiness.md
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
```

Expected: one linear head `0018`; exact predecessor link `0017`; production Compose, commands, evidence, and negative leak scan pass; the OrgForge sentinel remains untracked with its original metadata. The status/stat/hash commands do not open or print its contents.

- [ ] **Step 8: Final exact staging command, commit, and clean-scope proof**

```bash
set -euo pipefail
test -z "$(git diff --cached --name-only)"
git add Makefile README.md .github/workflows/ci.yml .github/workflows/phase10-operations.yml docs/evidence/phase10-backup.json docs/evidence/phase10-rate-limits.json docs/evidence/phase10-restore.json docs/evidence/phase10-upgrades.json docs/operations/production-deployment.md docs/operations/production-readiness.md tests/test_phase10_operations_docs.py tests/test_phase10_runbook_commands.py docs/evidence/phase10-hardening.md
test "$(git diff --cached --name-only | sort)" = "$(printf '%s\n' .github/workflows/ci.yml .github/workflows/phase10-operations.yml Makefile README.md docs/evidence/phase10-backup.json docs/evidence/phase10-hardening.md docs/evidence/phase10-rate-limits.json docs/evidence/phase10-restore.json docs/evidence/phase10-upgrades.json docs/operations/production-deployment.md docs/operations/production-readiness.md tests/test_phase10_operations_docs.py tests/test_phase10_runbook_commands.py | sort)"
git diff --cached --check
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git commit -m "docs: publish production operations runbooks"
git status --short -- Makefile README.md .github/workflows/ci.yml .github/workflows/phase10-operations.yml docs/evidence/phase10-backup.json docs/evidence/phase10-rate-limits.json docs/evidence/phase10-restore.json docs/evidence/phase10-upgrades.json docs/operations/production-deployment.md docs/operations/production-readiness.md tests/test_phase10_operations_docs.py tests/test_phase10_runbook_commands.py docs/evidence/phase10-hardening.md
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
```

Expected: commit 15 of 15. The implementation-path status command prints nothing. The silent sentinel assertion proves it remains exactly its original untracked entry and was never staged. Sub-project 6 is complete, while secret-audit/chaos and final Phase 10 closure remain deliberately outstanding.

## Execution Handoff

Implement Prerequisite P0, then Tasks 0–13 in order on Linux. P0 must commit only the tracked rootless-boundary correction before Task 0 commits this plan. Stop whenever a RED test fails for a reason other than its stated missing behavior, whenever a live project cannot prove its exact socket/project/volume/transport authority, or whenever teardown/evidence redaction fails. Do not begin Task 1 before all five predecessor completion commits plus P0 are ancestors and head `0017` is present. Do not begin the separate secret-audit/chaos plan until Task 13 is green and committed; that later plan owns canary propagation, deterministic failpoints, chaos scenarios, and final Phase 10 closure.
