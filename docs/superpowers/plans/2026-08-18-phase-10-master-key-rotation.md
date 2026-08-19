# Phase 10 Master-Key Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a strict versioned master-key keyring and a replica-gated, durable, bounded, resumable host-only rotation command that rewraps existing DEKs and migrates keyed fingerprints without changing secret ciphertext or nonce, then proves safe activation, recovery, and old-key retirement under real PostgreSQL and mixed application versions.

**Architecture:** The existing `Secret.key_version` remains the durable ciphertext-key authority and the existing `wrapped_data_key = wrap_nonce + wrapped_DEK` byte format remains compatible; `SecretCrypto` selects a key from an immutable in-memory ring, writes with the active version, and reads by the row version. Additive Alembic revision `0017` adds `master_key_rotation` plus a scalar PostgreSQL credential-mutation sequence/trigger that notices previous-image inserts, deletes, and ciphertext/nonce updates without scanning or rewriting `secret`; every mutating runner transaction is bound to and fences against the same PostgreSQL backend connection that owns the advisory lock, then commits a small keyset-paginated rewrap or verification batch with its checkpoint and captured generation. A durable retirement fence is armed under the final table lock and blocks semantic credential writes between that proof and the bounded service cutover; commit/cancel reacquires the advisory lease and revalidates the complete authority without holding an idle database lock across host work. Protected health projects bounded rotation state plus workspace-scoped row counts only. The fixed container target `/run/secrets/jhin_master_key` and the operator-selected Compose file source are expected configuration paths; key bytes, inline values, unexpected/sensitive host paths, plaintext, fingerprints, ciphertext, nonces, wrapped DEKs, global progress counters, database coordinates, and path-bearing/raw errors remain outside logs, CLI output, HTTP, and test diagnostics.

**Tech Stack:** Python 3.13, cryptography AES-256-GCM/HMAC-SHA256, SQLAlchemy 2 async, Alembic, PostgreSQL 17, Pydantic 2, the existing Phase 10 service observability bootstrap, FastAPI, Next.js 16.3.1, Docker Compose, pytest, Ruff, mypy, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-18-phase-10-production-operations-design.md`, especially “Master-key rotation and recovery,” `master_key_rotation`, operations audit events, protected key health, secret/logging boundaries, sub-project 5, sequencing/migration expectations, and key/backup/upgrade acceptance evidence.

## Global Constraints

- This is Phase 10 sub-project 5. Execute it only after deterministic tool-worker, telemetry, protected health, and DLQ/retry are complete. It does not implement backup tooling, general hardening, rate limits, or the final secret-audit/chaos matrix owned by sub-projects 6 and 7.
- Use the already locked dependency graph: Python `>=3.13`, cryptography `>=44.0`, SQLAlchemy `>=2.0.36`, Alembic `>=1.14`, Pydantic `>=2.10`, PostgreSQL 17, and Next.js 16.3.1. Add no third-party package and do not opportunistically upgrade unrelated lock entries.
- Health owns revision `0015`; DLQ/retry owns `packages/db/src/jhin_db/alembic/versions/20260818_0016_dlq_retry.py` with `revision = "0016"`. This plan adds exactly `packages/db/src/jhin_db/alembic/versions/20260818_0017_master_key_rotation.py` with `revision = "0017"` and `down_revision = "0016"`.
- Revision `0017` creates `master_key_rotation`, `secret_credential_mutation_generation_seq`, and one statement-level trigger/function on `secret`; it adds no column to and performs no row backfill/rewrite of `secret`, `service_instance_heartbeat`, DLQ/retry, or any earlier table. Previous images remain compatible because their ordinary `secret` INSERT/DELETE/credential UPDATE statements fire the database trigger without code changes; while the explicit retirement fence is armed, that same database trigger rejects those semantic mutations before they occur. Downgrade removes only the trigger/function/sequence and the new rotation table/indexes.
- `Secret.key_version` already exists and remains the version discriminator. Do not prepend a second version header to `ciphertext`, `nonce`, or `wrapped_data_key`; Phase 9 rows remain readable as version 1 without a data migration.
- Keyring JSON has exactly top-level fields `active_version` and `keys`; versions are canonical positive decimal integers in `1..2_147_483_647`; at most 32 sorted unique versions are supported; every decoded key is exactly 32 bytes; and the active version must exist.
- The loader rejects duplicate/unknown fields, duplicate or noncanonical versions, booleans, invalid base64, hex in new JSON, oversized/nonregular/symlink files, and every mode except exact `0400` or `0600`. A legacy single base64/hex key file maps to version 1 only in this first keyring-capable release.
- `MASTER_KEY` is a development-only legacy fallback. Any inline key/keyring setting is rejected when normalized `APP_ENV` is `production` or `prod`; production uses `MASTER_KEY_FILE` only. Errors and logs use closed safe reason codes and never echo material or paths.
- API degradation to `secret_crypto=None` is permitted only in normalized `dev`/`test`. In `production`/`prod`, missing, unreadable, unsafe, invalid, inline, or unavailable key material raises the closed `MasterKeyError` before the FastAPI application is returned; agent/tool workers already follow the same fail-closed startup rule.
- Compose file-backed secrets inherit host numeric ownership and permissions because Compose cannot remap `uid`, `gid`, or `mode` for a file source. Do not add silently ignored long-syntax ownership/mode fields. The runtime copy is an exact regular single-link file owned by numeric `10001:10001` with mode `0600` inside a randomly named root-owned mode-`0700` directory beneath fixed root-owned `/run/jhin-key-rotation`; `/`, `/run`, the fixed parent, and the random leaf are never invoking-user-writable. The invoking user opens the operator key with `O_NOFOLLOW` and passes that already-open descriptor only as stdin to exact privileged `install`; root never opens a user-controlled source path, and key bytes enter no argv/environment/output. Every initial install and replacement publishes its bounded nonsecret numeric identity receipt to one stable invoking-user-owned mode-0600 handoff path shared by Make, pytest, and the cleanup process: write/fsync a same-directory exclusive sibling, atomically replace the stable file, then fsync the directory. Cleanup reads only the latest stable handoff and must match every current root/leaf/file identity before unlinking only the exact runtime file and removing only the empty random leaf—never recursively or through a glob. A crash can leave either a matching latest receipt or a stale receipt that makes cleanup refuse; it cannot authorize the wrong inode. An operator-owned source/key backup remains separate; `chmod 600` alone is never described as making a host-UID file readable to container UID 10001.
- Key-bearing services are exactly `api`, `agent-worker`, and `tool-worker`. Workflow-worker, event-worker, web, sandbox-runner, monitoring services, job containers, and fake providers receive neither a key file nor keyring material.
- Freshness is the protected-health contract: `checked_at - 30 seconds <= last_seen_at <= checked_at`, inclusive at both ends. Production rotation obtains `checked_at` from PostgreSQL `clock_timestamp()` on the lock-owning transaction; host/app clocks never authorize arm, commit, cancel, freshness, or fence expiry. Every rollout gate requires at least one fresh row for each key-bearing service and every authoritative row to report the exact expected `(active_key_version, supported_key_versions)` tuple; a future-dated reporter closes the gate, while retained/stale rows are diagnostic and never grant authority.
- Stages are exact: distributed `(from, (from, to))`; activated `(to, (from, to))`; retirement-ready is a read-only preview requiring activated reporters, zero source/unexpected rows, and the latest matching attempt completed at the current credential-mutation generation; armed retirement repeats that proof under a table lock and durably blocks semantic credential mutation/new rotation; retired `(to, (to,))` is committed only after a fresh lease revalidates the same attempt/generation/rows plus exact retired reporters. A later active or aborted matching attempt closes retirement even if an older attempt completed. Operators restart all replicas between file stages.
- The supported run command remains `jhin-master-key-rotate --from 1 --to 2`. It refuses equal/missing versions, a closed replica gate, a conflicting active rotation, unexpected row versions, a busy advisory lock, or retirement with source rows.
- Rewrap unwraps the existing DEK with the row version and wraps that same DEK with the target version. Its SQL `SET` list is exactly `wrapped_data_key`, `key_version`, and `secret_fingerprint`; it uses explicit textual SQL so `TimestampMixin.updated_at.onupdate` cannot fire. `ciphertext`, `nonce`, masked hint, ownership, `created_at`, `updated_at`, `rotated_at`, `last_used_at`, and credential plaintext remain unchanged.
- Plaintext exists only inside one row-scoped rewrap/verification call. All extracted fragments are reference-counted in the process redactor before fallible work, remain protected while any exception is converted to a closed error, and are removed in `finally` before the next row. The scope cannot clear a pre-existing/concurrent registration and batch memory is bounded by one row's fragment limits. Plaintext is never returned, persisted, or logged; fingerprint comparison uses `hmac.compare_digest`.
- Rewrap and verification use primary-key keyset pagination, row locks without `SKIP LOCKED`, a maximum batch size of 1,000, a default of 100, and an optional bounded `--max-batches`. Each batch and its checkpoint/counters commit atomically on the lock-owning backend. Every lease/read/effect transaction installs exact PostgreSQL `lock_timeout=5000ms` and `statement_timeout=30000ms` plus a 35-second client deadline before its first authority query; row/state `FOR UPDATE`, final/retirement `LOCK TABLE ... IN SHARE MODE`, generation checks, reporter checks, prepare/abort, and advisory acquisition/release all map timeout SQLSTATEs/client expiry to closed codes with the transaction rolled back. The lease then proves its backend PID and granted advisory lock before any mutation; backend death or lock loss aborts before durable mutation. Verification captures the scalar mutation generation at pass start. Under the final `LOCK TABLE secret IN SHARE MODE`, completion requires that generation unchanged; any change—including a rolled-back sequence gap—conservatively resets and reruns verification. A rerun resumes by `(status, last_secret_id)`.
- Compose/Docker evidence never relies on implicit repository `.env` loading. The pinned environment removes inherited Compose env-file authority and then sets exact `COMPOSE_DISABLE_ENV_FILE=1`; it also pins the validated local Unix socket, empty private Docker config, project, file vector, profiles, socket GID, ports, and teardown environment for every Docker invocation.
- Executable rollout evidence independently verifies both the database backup and the separately protected keyring backup before any v2 distribution and repeats both after rotation before every retirement arm; a one-sided/absent/stale acceptance never advances the harness or runbook stage.
- Aborting prevents future batches but does not reverse committed rows. Before retirement, rollback selects the prior active writer while both readers remain; after retirement, rollback requires the separately protected old-key backup.
- Rotation audit actions are exactly `master_key.rotation_started`, `master_key.rotation_completed`, and `master_key.rotation_aborted`, with system actor and versions/counts/safe code only. No update rewrites a prior audit row.
- Wrapper-only rewrap must not invalidate a parked tool approval. Gateway authorization binds a credential revision derived from immutable secret ID plus ciphertext and nonce; actual credential rotation, connection config, auth type, status, or deletion still denies.
- Anonymous liveness/readiness remain opaque. The existing admin-only Operations response may add only closed state plus counts computed from `Secret.workspace_id == requested_workspace_id`; it never selects or exposes global rotation counters, rotation IDs, or secret-bearing fields.
- Every task follows RED -> focused GREEN -> affected regression -> lint/typecheck -> exact scoped staging -> commit. Never use `git add .`. At every staging boundary, assert the user-owned `orgforge-production-implementation-plan.md` remains exactly 82,118 bytes with SHA-256 `ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513`, and assert it is not staged. Never edit, stage, rename, delete, or print its content; reading it is permitted only inside that silent size/hash assertion.

---

## Complete File Map

The task-local `Files` lists are the staging authority. Repeated paths are intentionally extended by later tasks.

- **Plan:** `docs/superpowers/plans/2026-08-18-phase-10-master-key-rotation.md`.
- **Strict keyring/offline editing and compatible crypto bridge:** `packages/secrets/src/jhin_secrets/keyring.py`, `packages/secrets/src/jhin_secrets/safe_cli.py`, `packages/secrets/src/jhin_secrets/keyring_cli.py`, `packages/secrets/src/jhin_secrets/crypto.py`, `packages/secrets/src/jhin_secrets/__init__.py`, `packages/secrets/tests/test_keyring.py`, `packages/secrets/tests/test_keyring_cli.py`, `packages/secrets/tests/test_crypto.py`, `packages/secrets/pyproject.toml`, `scripts/generate_master_key.py`, `tests/test_generate_master_key.py`, `uv.lock`.
- **Multi-version rewrap/store/redactor scope:** `packages/secrets/src/jhin_secrets/crypto.py`, `packages/secrets/src/jhin_secrets/store.py`, `packages/secrets/src/jhin_secrets/material.py`, `packages/secrets/src/jhin_secrets/redaction.py`, `packages/secrets/src/jhin_secrets/__init__.py`, `packages/secrets/tests/test_crypto.py`, `packages/secrets/tests/test_store.py`, `packages/secrets/tests/test_redaction.py`.
- **Schema:** `packages/db/src/jhin_db/models/key_rotation.py`, `packages/db/src/jhin_db/models/__init__.py`, `packages/db/src/jhin_db/alembic/versions/20260818_0017_master_key_rotation.py`, `packages/db/tests/test_migration_graph.py`, `packages/db/tests/test_master_key_rotation_model.py`, `tests/integration/test_phase10_master_key_rotation_migration.py`, `apps/api/src/jhin_api/health/checks.py`, `apps/api/tests/test_health.py`.
- **Service loading/seed/heartbeat/Compose boundary:** `apps/api/src/jhin_api/main.py`, `apps/api/src/jhin_api/seed.py`, `apps/api/tests/test_keyring_startup.py`, `apps/api/tests/test_seed.py`, `services/agent_worker/src/jhin_agent_worker/resources.py`, `services/agent_worker/tests/test_keyring_resources.py`, `services/tool_worker/src/jhin_tool_worker/resources.py`, `services/tool_worker/tests/test_keyring_resources.py`, `compose.yaml`, `.env.example`, `tests/test_master_key_service_boundary.py`, `tests/test_master_key_compose.py`.
- **Rotation engine/audit/approval stability/real PostgreSQL:** `packages/secrets/src/jhin_secrets/rotation.py`, `packages/secrets/src/jhin_secrets/__init__.py`, `packages/secrets/tests/test_rotation.py`, `packages/tools/src/jhin_tools/gateway.py`, `packages/tools/tests/test_gateway.py`, `tests/integration/test_phase10_master_key_rotation_postgres.py`.
- **Host CLI:** `packages/secrets/src/jhin_secrets/rotation_cli.py`, `packages/secrets/tests/test_rotation_cli.py`, `packages/secrets/pyproject.toml`, `uv.lock`.
- **Protected projection/UI:** `apps/api/src/jhin_api/health/schemas.py`, `apps/api/src/jhin_api/health/service.py`, `apps/api/tests/conftest.py`, `apps/api/tests/test_operations_health.py`, `apps/web/lib/types.ts`, `apps/web/app/(app)/operations/page.tsx`, `apps/web/tests/operations-page.test.tsx`.
- **Mixed-version/live evidence:** `scripts/capture_pre_keyring_ref.py`, `tests/test_capture_pre_keyring_ref.py`, `tests/integration/fixtures/phase10-pre-keyring-ref.txt`, `tests/integration/phase10_key_rotation_harness.py`, `tests/integration/compose.phase10-keyring-upgrade.yaml`, `tests/integration/test_phase10_keyring_upgrade.py`, `tests/integration/test_phase10_master_key_rotation.py`, `tests/test_phase10_master_key_rotation_harness.py`, `tests/integration/conftest.py`, `Makefile`, `.github/workflows/ci.yml`.
- **Runbook/index:** `docs/operations/master-key-rotation.md`, `apps/web/public/runbooks/master-key-rotation.md`, `docs/operations/protected-health.md`, `README.md`, `tests/test_master_key_rotation_docs.py`, `apps/web/app/(app)/operations/page.tsx`, `apps/web/tests/operations-page.test.tsx`.

Read-only prior-plan inputs include the Phase 10 spec, the other four Phase 10 plans, `packages/db/src/jhin_db/models/operations.py`, `packages/db/src/jhin_db/models/recovery.py`, migrations `0015`/`0016`, `compose.dev.yaml`, `compose.rootful.yaml`, and `compose.rootless.yaml`. This plan never stages those inputs unless they are explicitly listed above.

## Shared Interfaces

These names and values are fixed across tasks.

```python
# packages/secrets/src/jhin_secrets/keyring.py
MAX_KEYRING_BYTES = 65_536
MAX_KEY_VERSIONS = 32
MIN_KEY_VERSION = 1
MAX_KEY_VERSION = 2_147_483_647

class MasterKeyErrorCode(StrEnum):
    NOT_CONFIGURED = "master_key_not_configured"
    FILE_UNREADABLE = "master_key_file_unreadable"
    FILE_UNSAFE = "master_key_file_unsafe"
    DOCUMENT_INVALID = "master_key_document_invalid"
    INLINE_FORBIDDEN = "master_key_inline_forbidden"
    VERSION_UNAVAILABLE = "master_key_version_unavailable"

class MasterKeyError(Exception):
    code: MasterKeyErrorCode

@dataclass(frozen=True, repr=False)
class MasterKey:
    key: bytes = field(repr=False)
    version: int = 1

@dataclass(frozen=True, repr=False)
class MasterKeyRing:
    active_version: int
    _keys: Mapping[int, bytes] = field(repr=False)

    @property
    def supported_key_versions(self) -> tuple[int, ...]:
        return tuple(sorted(self._keys))

    def key_for(self, version: int) -> MasterKey:
        if version not in self._keys:
            raise MasterKeyError(MasterKeyErrorCode.VERSION_UNAVAILABLE)
        return MasterKey(self._keys[version], version)

def parse_master_key_document(raw: bytes, *, allow_legacy: bool = True) -> MasterKeyRing: ...
def load_master_key(
    environ: Mapping[str, str] | None = None,
    *,
    app_env: str | None = None,
) -> MasterKeyRing: ...
def write_master_keyring(path: Path, ring: MasterKeyRing, *, exclusive: bool = True) -> None: ...
```

`MasterKeyError.__str__()` returns only `code.value`; `MasterKey` and `MasterKeyRing` reprs contain versions only. `parse_master_key_document` uses `json.loads(..., object_pairs_hook=...)`, strict base64 with `validate=True`, and canonical string versions matching `[1-9][0-9]{0,9}` before the numeric bound. `load_master_key` opens with `O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK` so a FIFO cannot block before validation, requires a regular file whose exact mode is `0400` or `0600`, validates size before reading, and never embeds the configured path in an exception.

```python
# packages/secrets/src/jhin_secrets/safe_cli.py
class ClosedArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, invalid_code: str, **kwargs: object) -> None: ...
    def error(self, message: str) -> NoReturn:
        self.exit(2, f"{self.invalid_code}\n")
```

Both master-key CLIs construct this parser and place only closed codes on stderr. Their executable `main()` catches `MasterKeyError`, `RotationError`, `FileExistsError`, expected `OSError`, SQLAlchemy/asyncpg connection errors, and cancellation cleanup failures at the outermost boundary and maps them to documented closed codes/exits without `str(exc)`, argparse's rejected token, a path, DSN, or key material. Unexpected exceptions are logged only through the configured structural/value redactor and stderr remains `master_key_internal_error`.

```python
# packages/secrets/src/jhin_secrets/crypto.py
@dataclass(frozen=True, repr=False)
class RewrappedPayload:
    wrapped_data_key: bytes = field(repr=False)
    key_version: int
    fingerprint: str = field(repr=False)

class SecretCrypto:
    def __init__(self, master: MasterKey | MasterKeyRing) -> None: ...
    @property
    def active_key_version(self) -> int: ...
    @property
    def supported_key_versions(self) -> tuple[int, ...]: ...
    @property
    def key_version(self) -> int: ...  # compatibility alias for active_key_version
    def fingerprint(self, plaintext: str, *, key_version: int | None = None) -> str: ...
    def encrypt(self, plaintext: str) -> EncryptedPayload: ...
    def decrypt(self, payload: EncryptedPayload) -> str: ...
    def rewrap(self, payload: EncryptedPayload, *, to_version: int) -> RewrappedPayload: ...
    def verify(self, payload: EncryptedPayload, *, expected_version: int) -> None: ...
```

Set both `EncryptedPayload` and `RewrappedPayload` to `repr=False`; their explicit reprs contain only `key_version`. `rewrap` unwraps the source DEK, decrypts/registers plaintext, produces a new random wrap nonce under `to_version`, and returns no plaintext/ciphertext/nonce. `verify` decrypts/registers and rejects a nonmatching row version or target-key fingerprint with a closed `SecretVerificationError`.

```python
# packages/secrets/src/jhin_secrets/redaction.py and material.py
class SecretRedactor:
    @contextmanager
    def scoped(self, values: Collection[str]) -> Iterator[None]: ...
    @property
    def registered_value_count(self) -> int: ...

@contextmanager
def scoped_secret_material(plaintext: str) -> Iterator[None]: ...
```

`SecretRedactor` stores reference counts, not an ever-growing set. `register()` preserves the existing long-lived behavior. `scoped()` adds the bounded unique fragments once, decrements exactly those counts in `finally`, retains a value registered by another scope/permanent caller, and never exposes values. `scoped_secret_material()` reuses the existing size/depth/fragment/URL parser before entering the redactor scope.

```python
# packages/db/src/jhin_db/models/key_rotation.py
RotationStatus = Literal["prepared", "rewrapping", "verifying", "completed", "aborted"]

class MasterKeyRotation(Base, UuidPkMixin):
    from_version: Mapped[int]
    to_version: Mapped[int]
    status: Mapped[str]
    last_secret_id: Mapped[UUID | None]
    rows_total: Mapped[int]
    rows_rewrapped: Mapped[int]
    rows_verified: Mapped[int]
    rows_failed: Mapped[int]
    credential_mutation_generation: Mapped[int | None]
    retirement_fence_id: Mapped[UUID | None]
    retirement_fence_generation: Mapped[int | None]
    retirement_fence_started_at: Mapped[datetime | None]
    retirement_fence_deadline: Mapped[datetime | None]
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    safe_error_code: Mapped[str | None]
```

```python
# packages/secrets/src/jhin_secrets/rotation.py
MASTER_KEY_ROTATION_ADVISORY_LOCK = 5_347_253_496_681_532_233
MASTER_KEY_ROTATION_LOCK_CLASSID = 1_245_004_473
MASTER_KEY_ROTATION_LOCK_OBJID = 1_772_817_225
DEFAULT_ROTATION_BATCH_SIZE = 100
MAX_ROTATION_BATCH_SIZE = 1_000
FRESH_HEARTBEAT_SECONDS = 30
ROTATION_LOCK_TIMEOUT_MS = 5_000
ROTATION_STATEMENT_TIMEOUT_MS = 30_000
ROTATION_CLIENT_TIMEOUT_SECONDS = 35.0
RETIREMENT_FENCE_WINDOW_SECONDS = 600
KEY_SERVICES = ("api", "agent-worker", "tool-worker")

CURRENT_CREDENTIAL_MUTATION_GENERATION_SQL = text("""
SELECT CASE WHEN is_called THEN last_value ELSE 0 END AS generation
FROM secret_credential_mutation_generation_seq
""")

POSTGRES_CLOCK_SQL = text("SELECT clock_timestamp()")

POSTGRES_LEASE_HELD_SQL = text("""
SELECT pg_backend_pid() = :backend_pid
   AND EXISTS (
       SELECT 1
       FROM pg_catalog.pg_locks
       WHERE pid = pg_backend_pid()
         AND locktype = 'advisory'
         AND mode = 'ExclusiveLock'
         AND classid = CAST(:classid AS oid)
         AND objid = CAST(:objid AS oid)
         AND objsubid = 1
         AND granted
   )
""")

class RotationStage(StrEnum):
    DISTRIBUTED = "distributed"
    ACTIVATED = "activated"
    RETIREMENT_READY = "retirement-ready"
    RETIRED = "retired"

class RotationSafeErrorCode(StrEnum):
    REPLICA_GATE_CLOSED = "replica_gate_closed"
    CONFLICTING_ROTATION = "conflicting_rotation"
    UNEXPECTED_SECRET_VERSION = "unexpected_secret_version"
    SECRET_DECRYPTION_FAILED = "secret_decryption_failed"
    SECRET_VERIFICATION_FAILED = "secret_verification_failed"
    SOURCE_ROWS_REMAIN = "source_rows_remain"
    ROW_LOCK_TIMEOUT = "row_lock_timeout"
    STATEMENT_TIMEOUT = "rotation_statement_timeout"
    ADVISORY_LOCK_LOST = "rotation_advisory_lock_lost"
    RETIREMENT_FENCE_ACTIVE = "retirement_fence_active"
    RETIREMENT_FENCE_MISSING = "retirement_fence_missing"
    RETIREMENT_FENCE_EXPIRED = "retirement_fence_expired"

@dataclass(frozen=True)
class ReplicaKeyDistribution:
    service: Literal["api", "agent-worker", "tool-worker"]
    active_version: int
    supported_versions: tuple[int, ...]
    instance_count: int

@dataclass(frozen=True)
class ReplicaGateResult:
    stage: RotationStage
    open: bool
    distributions: tuple[ReplicaKeyDistribution, ...]
    missing_services: tuple[str, ...]
    mismatched_instances: int
    future_instances: int

@dataclass(frozen=True)
class RotationRunResult:
    from_version: int
    to_version: int
    status: RotationStatus
    rows_total: int
    rows_rewrapped: int
    rows_verified: int
    rows_failed: int
    safe_error_code: RotationSafeErrorCode | None

RetirementFenceState = Literal["armed", "committed", "cancelled"]

@dataclass(frozen=True)
class RetirementFenceResult:
    from_version: int
    to_version: int
    fence_id: UUID
    state: RetirementFenceState
    safe_error_code: RotationSafeErrorCode | None

class RotationLease(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[AsyncSession]: ...
    async def assert_held(self, session: AsyncSession) -> None: ...

class MutationGenerationSource(Protocol):
    async def current(self, session: AsyncSession) -> int: ...

class PostgresMutationGenerationSource:
    async def current(self, session: AsyncSession) -> int: ...

class RotationDatabaseClock(Protocol):
    async def checked_at(self, session: AsyncSession) -> datetime: ...

class PostgresRotationDatabaseClock:
    async def checked_at(self, session: AsyncSession) -> datetime: ...

class PostgresRotationLease:
    @classmethod
    async def try_acquire(
        cls, connection: AsyncConnection
    ) -> PostgresRotationLease | None: ...
    def transaction(self) -> AbstractAsyncContextManager[AsyncSession]: ...
    async def assert_held(self, session: AsyncSession) -> None: ...
    async def release(self) -> None: ...
    @property
    def backend_pid(self) -> int: ...

async def check_replica_gate(
    session: AsyncSession,
    *,
    stage: RotationStage,
    from_version: int,
    to_version: int,
    now: datetime,
) -> ReplicaGateResult: ...

class RotationRunner:
    def __init__(
        self,
        crypto: SecretCrypto,
        lease: RotationLease,
        generation_source: MutationGenerationSource,
        database_clock: RotationDatabaseClock,
        *,
        batch_size: int = DEFAULT_ROTATION_BATCH_SIZE,
        max_batches: int | None = None,
    ) -> None: ...
    async def run(self, *, from_version: int, to_version: int) -> RotationRunResult: ...
    async def abort(self, *, from_version: int, to_version: int) -> RotationRunResult: ...
    async def arm_retirement_fence(
        self, *, from_version: int, to_version: int
    ) -> RetirementFenceResult: ...
    async def commit_retirement_fence(
        self, *, from_version: int, to_version: int, fence_id: UUID
    ) -> RetirementFenceResult: ...
    async def cancel_retirement_fence(
        self, *, from_version: int, to_version: int, fence_id: UUID
    ) -> RetirementFenceResult: ...
```

```python
# packages/secrets/tests/test_rotation.py
@dataclass
class FakeMutationGenerationSource:
    value: int = 0
    async def current(self, session: AsyncSession) -> int:
        return self.value
    def advance(self) -> None:
        self.value += 1

@dataclass
class FakeRotationDatabaseClock:
    value: datetime
    async def checked_at(self, session: AsyncSession) -> datetime:
        return self.value
    def set(self, value: datetime) -> None:
        self.value = value
```

The runner requires a live CLI-owned session-level advisory `RotationLease`; it has no independent session factory. `PostgresRotationLease.try_acquire()` enters `asyncio.timeout(35.0)`, executes session-level `SET lock_timeout = '5000ms'` and `SET statement_timeout = '30000ms'`, reads both settings back and requires exact `5s`/`30s`, and only then performs its first authority query, the single `SELECT pg_backend_pid(), pg_try_advisory_lock(...)`; failure to install/read either setting is closed and cannot query rotation state. `PostgresRotationLease.transaction()` begins on that exact connection, executes `SET LOCAL lock_timeout = '5000ms'` and `SET LOCAL statement_timeout = '30000ms'`, then yields inside the same client bound. The runner's first authority SQL is `await lease.assert_held(session)`, before any read used to authorize mutation and before every mutation. Release and read-only stage checks use the same settings and 35-second client bound. PostgreSQL SQLSTATE `55P03` maps to `ROW_LOCK_TIMEOUT`, server cancellation `57014` maps to `STATEMENT_TIMEOUT`, and client expiry maps to `STATEMENT_TIMEOUT`; all paths roll back and discard raw DBAPI text. Every prepare, batch, status, checkpoint, audit, abort, completion, retirement arm/commit/cancel, and stage-check transaction uses this one adapter and backend. `PostgresRotationLease.assert_held` checks both the recorded backend PID and the granted 64-bit lock row in `pg_catalog.pg_locks` (`classid=1245004473`, `objid=1772817225`, `objsubid=1`) before mutation; connection loss becomes `ADVISORY_LOCK_LOST` with the transaction rolled back. `PostgresMutationGenerationSource` reads only the bounded scalar sequence state on that same session. `PostgresRotationDatabaseClock` executes only `POSTGRES_CLOCK_SQL`, rejects null/naive values, and is the sole production freshness/fence time authority; the runner accepts no host-clock callback. SQLite/unit worlds inject a monotonic `FakeMutationGenerationSource` advanced by their secret-mutation helpers and `FakeRotationDatabaseClock`. Each batch rechecks the activated gate and validates every row version is `from_version` or `to_version`. Rewrap and checkpoint share one transaction. When no source rows remain it changes to `verifying`, resets `last_secret_id` and `rows_verified`, and captures `credential_mutation_generation`. A source row observed at any verification boundary atomically transitions `verifying -> rewrapping` and clears both checkpoint and generation. Final completion takes a PostgreSQL `LOCK TABLE secret IN SHARE MODE`, then requires zero source/unexpected rows and the current scalar generation equal to the pass's captured generation; generation drift resets the verification checkpoint/count and captures the newer generation. A completed row persists that exact generation. Sequence increments are intentionally nontransactional, so a rolled-back credential statement may cause a harmless full reverification rather than a false completion.

`RETIREMENT_READY` is only a read-only preview. It runs on the lock-owning bounded transaction, takes the same PostgreSQL `LOCK TABLE secret IN SHARE MODE`, and selects the latest matching attempt ordered by `(started_at DESC, id DESC)`. It opens only when that attempt is `completed`, no later matching active/aborted attempt exists, zero source/unexpected rows remain, and its nonnull `credential_mutation_generation` equals the current sequence generation while the table lock excludes credential writers. It does not authorize removing the old key.

`arm_retirement_fence` is the exact handoff authority. In one bounded lease transaction it locks the latest attempt, takes `secret` and `service_instance_heartbeat` SHARE table locks, obtains `checked_at` from `clock_timestamp()`, and only then repeats the latest-attempt/generation/row proof plus the exact activated reporter gate immediately before one fence update. It stores a new UUID fence ID, the current generation, that same database `checked_at` as `started_at`, and a database deadline exactly 600 seconds later on that completed attempt before committing. The heartbeat SHARE lock prevents a reporter write between the final gate query and fence mutation. The credential-generation trigger is a `BEFORE ... FOR EACH STATEMENT` trigger: while any fence ID is nonnull it rejects `Secret` INSERT, DELETE, or UPDATE OF `ciphertext, nonce` with a fixed SQLSTATE/message before changing a row or generation; wrapper-only updates remain allowed. Every runner prepare/batch/abort and a second arm also refuses an armed fence. The fence remains fail-closed even after its deadline—expiry makes commit fail and requires rollback/cancel; it never silently re-enables writes.

After the arm transaction commits, no PostgreSQL lock is held during the bounded runtime-file install and service restart, but the durable trigger closes the semantic-mutation gap. `commit_retirement_fence` reacquires a fresh advisory lease, locks the row plus both authority tables in SHARE mode, reads `clock_timestamp()` after those locks, and immediately rechecks exact retired `(to,(to,))` reporters, the same fence ID/latest completed attempt/stored completion generation/fence generation/current scalar, zero source/unexpected rows, and database `checked_at <= retirement_fence_deadline` before one atomic clear. `cancel_retirement_fence` is rollback, not retirement authority: only after the dual-key activated ring has been restored does it reacquire the lease, takes the same row/secret/heartbeat locks, reads database time, and immediately rechecks exact activated reporters, the same fence/latest attempt/generation, and no version outside `{from,to}` before clearing. It deliberately permits source rows because both readers are restored; the next ordinary run creates/resumes a new attempt and rewraps them. A completed attempt followed by fallback source rows, same-ID credential mutation, delete/create, a later abort, any generation drift, an expired fence, reporter drift, or host-clock skew is never retirement authority. `(rows_verified,last_secret_id)` remains only a resumable bounded verification checkpoint/count, never completion/fence authority.

```python
# packages/secrets/src/jhin_secrets/rotation_cli.py
@dataclass(frozen=True)
class RotationOptions:
    from_version: int
    to_version: int
    batch_size: int
    max_batches: int | None
    check_stage: RotationStage | None
    abort: bool
    retirement_action: Literal["arm", "commit", "cancel"] | None
    retirement_fence_id: UUID | None

def parse_args(argv: Sequence[str]) -> RotationOptions: ...
async def async_main(options: RotationOptions, environ: Mapping[str, str]) -> int: ...
def main() -> None: ...
```

Default mode runs/resumes. `--check-stage`, `--abort`, and `--retirement-action` are mutually exclusive; `--fence-id` is required exactly for retirement `commit|cancel` and forbidden otherwise. CLI exit codes are `0` completed/open/fence action complete, `2` invalid configuration, `3` gate closed, `4` advisory lock busy, `5` safe row/verification failure, and `75` bounded work remains. Stdout is one safe JSON object shaped exactly like `RotationRunResult`, `ReplicaGateResult`, or `RetirementFenceResult` for the chosen mode; stderr contains a closed code only.

---

### Task 0: Check In the Reviewed Master-Key Rotation Plan

**Files:**
- Create: `docs/superpowers/plans/2026-08-18-phase-10-master-key-rotation.md`

**Interfaces:**
- Consumes: the Phase 10 design and completed sub-project plans 1–4.
- Produces: the reviewed execution baseline only; no runtime behavior.

- [ ] **Step 1: Verify the plan is the only intended path**

```bash
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git status --short -- docs/superpowers/plans/2026-08-18-phase-10-master-key-rotation.md orgforge-production-implementation-plan.md
git diff --check -- docs/superpowers/plans/2026-08-18-phase-10-master-key-rotation.md
```

Expected: the plan is new/modified; the user-owned file may be untracked but has no diff and is not staged.

- [ ] **Step 2: Stage and commit only the plan**

```bash
git add docs/superpowers/plans/2026-08-18-phase-10-master-key-rotation.md
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "docs: plan master-key rotation"
```

Expected cached-name output: exactly `docs/superpowers/plans/2026-08-18-phase-10-master-key-rotation.md`.

### Task 1: Parse Keyrings Without Breaking Existing Crypto Callers

**Files:**
- Create: `packages/secrets/src/jhin_secrets/keyring.py`
- Create: `packages/secrets/src/jhin_secrets/safe_cli.py`
- Create: `packages/secrets/src/jhin_secrets/keyring_cli.py`
- Modify: `packages/secrets/src/jhin_secrets/crypto.py`
- Modify: `packages/secrets/src/jhin_secrets/__init__.py`
- Create: `packages/secrets/tests/test_keyring.py`
- Create: `packages/secrets/tests/test_keyring_cli.py`
- Modify: `packages/secrets/tests/test_crypto.py`
- Modify: `packages/secrets/pyproject.toml`
- Modify: `scripts/generate_master_key.py`
- Create: `tests/test_generate_master_key.py`
- Create: `scripts/capture_pre_keyring_ref.py`
- Create: `tests/test_capture_pre_keyring_ref.py`
- Create: `tests/integration/fixtures/phase10-pre-keyring-ref.txt`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: current `MasterKey`, `MasterKeyError`, `decode_master_key_material`, `load_master_key`, the ignored `secrets/dev/jhin_master_key`, and `make master-key`.
- Produces: the Shared Interfaces keyring types/loader/writer and closed parser; console command `jhin-master-keyring`; a compatibility `load_master_key` import; a `SecretCrypto(MasterKey | MasterKeyRing)` bridge that keeps every existing API/worker/store caller functional at this commit; and new installations whose generated file is JSON with active version 1 while existing raw files still load as version 1.

- [ ] **Step 0: Freeze the pre-keyring application source before any runtime edit**

First write `tests/test_capture_pre_keyring_ref.py` so a temporary Git repository proves the generator accepts a caller-supplied ref, resolves one 40-character commit, rejects a ref outside the current `HEAD` ancestry, tolerates unrelated uncommitted plan/test files without incorporating them, and writes exactly `<sha>\n` without timestamps or local paths. Run it RED:

```bash
uv run pytest tests/test_capture_pre_keyring_ref.py -q
```

Expected: FAIL because the generator does not exist. Implement `capture(repo: Path, ref: str, output: Path) -> str` in `scripts/capture_pre_keyring_ref.py` using argument-vector `git rev-parse --verify <ref>^{commit}` and `git merge-base --is-ancestor`; use `Path.write_text` only for this nonsecret immutable fixture. Then run:

```bash
uv run pytest tests/test_capture_pre_keyring_ref.py -q
uv run python scripts/capture_pre_keyring_ref.py --ref HEAD --output tests/integration/fixtures/phase10-pre-keyring-ref.txt
test "$(cat tests/integration/fixtures/phase10-pre-keyring-ref.txt)" = "$(git rev-parse HEAD)"
```

Expected: the fixture points to Task 0's plan-only commit, before `keyring.py` or any runtime edit. Never regenerate it after Step 0.

- [ ] **Step 1: Write strict parser, file, repr, and environment tests**

```python
def key(version_byte: int) -> str:
    return base64.b64encode(bytes([version_byte]) * 32).decode("ascii")


def document(active: int = 1, versions: tuple[int, ...] = (1,)) -> bytes:
    return json.dumps(
        {"active_version": active, "keys": {str(v): key(v) for v in versions}},
        separators=(",", ":"),
    ).encode("ascii")


def write_private(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def test_keyring_is_strict_sorted_and_repr_safe() -> None:
    ring = parse_master_key_document(document(active=2, versions=(2, 1)))
    assert ring.active_version == 2
    assert ring.supported_key_versions == (1, 2)
    assert ring.key_for(1).key == bytes([1]) * 32
    rendered = repr(ring) + repr(ring.key_for(1))
    assert key(1) not in rendered
    assert (bytes([1]) * 32).hex() not in rendered


@pytest.mark.parametrize(
    "raw",
    [
        b'{"active_version":1,"active_version":2,"keys":{"1":"x"}}',
        b'{"active_version":1,"keys":{"1":"x","1":"y"}}',
        b'{"active_version":1,"keys":{"01":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}}',
        b'{"active_version":true,"keys":{"1":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}}',
        b'{"active_version":1,"keys":{"0":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}}',
        b'{"active_version":2,"keys":{"1":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}}',
        b'{"active_version":1,"keys":{"1":"not-base64"}}',
        b'{"active_version":1,"keys":{"1":"AA=="}}',
        b'{"active_version":1,"keys":{},"extra":"forbidden"}',
    ],
)
def test_invalid_documents_fail_with_only_a_safe_code(raw: bytes) -> None:
    with pytest.raises(MasterKeyError) as excinfo:
        parse_master_key_document(raw)
    assert str(excinfo.value) == MasterKeyErrorCode.DOCUMENT_INVALID.value
    assert raw.decode("utf-8", "ignore") not in str(excinfo.value)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o666])
def test_loader_rejects_group_or_world_bits(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "canary-keyring-path"
    path.write_bytes(document())
    path.chmod(mode)
    with pytest.raises(MasterKeyError) as excinfo:
        load_master_key({"MASTER_KEY_FILE": str(path)}, app_env="production")
    assert excinfo.value.code is MasterKeyErrorCode.FILE_UNSAFE
    assert str(path) not in str(excinfo.value)


def test_loader_rejects_symlink_and_oversized_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    write_private(target, document())
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(MasterKeyError, match="master_key_file_unsafe"):
        load_master_key({"MASTER_KEY_FILE": str(link)}, app_env="production")
    huge = tmp_path / "huge"
    write_private(huge, b"x" * (MAX_KEYRING_BYTES + 1))
    with pytest.raises(MasterKeyError, match="master_key_file_unsafe"):
        load_master_key({"MASTER_KEY_FILE": str(huge)}, app_env="production")


def test_legacy_file_is_version_one_but_inline_is_forbidden_in_production(
    tmp_path: Path,
) -> None:
    legacy = key(9)
    path = tmp_path / "legacy"
    write_private(path, (legacy + "\n").encode("ascii"))
    ring = load_master_key({"MASTER_KEY_FILE": str(path)}, app_env="production")
    assert ring.active_version == 1
    assert ring.supported_key_versions == (1,)
    with pytest.raises(MasterKeyError, match="master_key_inline_forbidden"):
        load_master_key({"MASTER_KEY": legacy}, app_env="production")


def test_crypto_bridge_accepts_legacy_key_and_loaded_ring_at_this_commit(
    tmp_path: Path,
) -> None:
    legacy_crypto = SecretCrypto(MasterKey(b"1" * 32, version=1))
    old = legacy_crypto.encrypt("compatibility-canary")
    path = tmp_path / "ring"
    write_private(path, document(active=2, versions=(1, 2)))
    ring_crypto = SecretCrypto(
        load_master_key({"MASTER_KEY_FILE": str(path)}, app_env="production")
    )
    assert ring_crypto.decrypt(old) == "compatibility-canary"
    assert ring_crypto.encrypt("new-canary").key_version == 2
    assert ring_crypto.supported_key_versions == (1, 2)
```

Also cover missing/empty/non-UTF-8 documents, a FIFO/nonregular file without blocking, version `2_147_483_648`, 33 keys, active key absent, JSON hex material rejection, accepted owner modes `0400` and `0600`, `MASTER_KEY` legacy fallback in `dev` with one safe warning, and `MasterKeyError` plus keyring `str`/`repr`/structured-log rendering never exposing a key or path. Do not claim that in-process introspection cannot see bytes the crypto process must use.

- [ ] **Step 2: Write offline CLI and generator tests**

```python
def test_offline_add_activate_retire_never_overwrites_or_prints_material(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, distributed, active, retired = (tmp_path / name for name in ("v1", "both", "v2", "only-v2"))
    assert keyring_main(["create", "--output", str(first), "--version", "1"]) == 0
    assert keyring_main([
        "add", "--input", str(first), "--output", str(distributed), "--version", "2"
    ]) == 0
    assert keyring_main([
        "activate", "--input", str(distributed), "--output", str(active), "--version", "2"
    ]) == 0
    assert keyring_main([
        "retire", "--input", str(active), "--output", str(retired), "--version", "1"
    ]) == 0
    assert stat.S_IMODE(retired.stat().st_mode) == 0o600
    ring = load_master_key({"MASTER_KEY_FILE": str(retired)}, app_env="production")
    assert (ring.active_version, ring.supported_key_versions) == (2, (2,))
    output = capsys.readouterr()
    for path in (first, distributed, active, retired):
        assert path.read_text(encoding="utf-8").strip() not in output.out + output.err
    assert keyring_main(["create", "--output", str(retired), "--version", "3"]) == 4
    assert capsys.readouterr().err == "master_key_output_exists\n"


def test_generate_script_writes_initial_json_keyring(tmp_path: Path) -> None:
    output = tmp_path / "generated"
    result = subprocess.run(
        [sys.executable, "scripts/generate_master_key.py", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    ring = load_master_key({"MASTER_KEY_FILE": str(output)}, app_env="production")
    assert (ring.active_version, ring.supported_key_versions) == (1, (1,))
    assert output.read_text(encoding="utf-8").strip() not in result.stdout + result.stderr


def test_cli_subprocess_closes_argparse_and_os_errors(tmp_path: Path) -> None:
    output = tmp_path / "PATH_CANARY_DO_NOT_ECHO"
    output.write_text("occupied", encoding="utf-8")
    output.chmod(0o600)
    exists = subprocess.run(
        [
            sys.executable, "-m", "jhin_secrets.keyring_cli", "create",
            "--output", str(output), "--version", "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (exists.returncode, exists.stdout, exists.stderr) == (
        4, "", "master_key_output_exists\n"
    )
    rejected = subprocess.run(
        [
            sys.executable, "-m", "jhin_secrets.keyring_cli", "create",
            "--output", str(tmp_path / "unused"), "--version", "1",
            "--database-url", "postgresql://DSN_CANARY_DO_NOT_ECHO",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (rejected.returncode, rejected.stdout, rejected.stderr) == (
        2, "", "master_key_invalid_arguments\n"
    )
    rendered = exists.stdout + exists.stderr + rejected.stdout + rejected.stderr
    assert "PATH_CANARY_DO_NOT_ECHO" not in rendered
    assert "DSN_CANARY_DO_NOT_ECHO" not in rendered
```

Add exact closed failures for add-existing-version, activate-missing-version, retire-active-version, retire-last-version, unsafe input permissions, same input/output, output parent symlink, missing parent, permission denial, and all output-exists cases. Run each path/DSN/key canary through `python -m jhin_secrets.keyring_cli`, not only a direct function, and require stdout empty plus one closed stderr line on every failure. The writer opens with `O_CREAT | O_EXCL | O_NOFOLLOW`, mode `0600`, writes canonical compact JSON plus newline, `fsync`s file and parent directory, closes/unlinks a partial new file on failure, and never mutates an input file.

- [ ] **Step 3: Run RED and inspect the failures**

```bash
uv run pytest packages/secrets/tests/test_keyring.py packages/secrets/tests/test_keyring_cli.py packages/secrets/tests/test_crypto.py tests/test_generate_master_key.py -q
```

Expected: FAIL because `keyring.py`, `safe_cli.py`, `keyring_cli.py`, the ring-compatible crypto constructor, strict errors, JSON generation, and the console entry point do not exist; the existing loader accepts unsafe mode and path-bearing errors.

- [ ] **Step 4: Implement the keyring and safe offline editor**

Move key-loading primitives out of `crypto.py` into `keyring.py`, then re-import/re-export them from `crypto.py` so existing imports keep working. Preserve legacy hex only in the single-key branch; JSON values are strict base64. Copy the input mapping to an immutable `MappingProxyType`, validate before constructing, and make `__repr__` render only `MasterKeyRing(active_version=2, supported_key_versions=(1, 2))`. In the same commit, normalize `SecretCrypto(MasterKey)` to a one-entry ring and make `SecretCrypto(MasterKeyRing)` encrypt with `active_version` and decrypt strictly with `payload.key_version`; retain `key_version` as the active-version compatibility alias. Do not defer that bridge to Task 2: run the full existing API/agent/tool secret callers before committing.

`jhin-master-keyring` has exact subcommands `create`, `add`, `activate`, and `retire`. It uses `ClosedArgumentParser`; success prints only `keyring_written action=<closed action> active_version=<n> supported_versions=<comma-separated integers>`. Failure stdout is empty and stderr is exactly one of `master_key_invalid_arguments`, `master_key_document_invalid`, `master_key_file_unsafe`, `master_key_output_exists`, or `master_key_io_error`; neither parser tokens nor caught exception strings are rendered. Update `scripts/generate_master_key.py` to invoke the same create implementation and closed error boundary. Add only this script in `packages/secrets/pyproject.toml` now:

```toml
[project.scripts]
jhin-master-keyring = "jhin_secrets.keyring_cli:main"
```

- [ ] **Step 5: Run GREEN, packaging checks, and scoped commit**

```bash
uv lock
uv run pytest packages/secrets/tests/test_keyring.py packages/secrets/tests/test_keyring_cli.py packages/secrets/tests/test_crypto.py tests/test_generate_master_key.py -q
uv run pytest apps/api/tests/test_secrets_unit.py apps/api/tests/test_connections_unit.py packages/tools/tests/test_gateway.py services/agent_worker/tests/test_phase9_invocation_activity.py -q
uv run ruff check packages/secrets scripts/generate_master_key.py tests/test_generate_master_key.py
uv run mypy packages/secrets/src scripts/generate_master_key.py tests/test_generate_master_key.py
uv run jhin-master-keyring --help
git add packages/secrets/src/jhin_secrets/keyring.py packages/secrets/src/jhin_secrets/safe_cli.py packages/secrets/src/jhin_secrets/keyring_cli.py packages/secrets/src/jhin_secrets/crypto.py packages/secrets/src/jhin_secrets/__init__.py packages/secrets/tests/test_keyring.py packages/secrets/tests/test_keyring_cli.py packages/secrets/tests/test_crypto.py packages/secrets/pyproject.toml scripts/generate_master_key.py tests/test_generate_master_key.py scripts/capture_pre_keyring_ref.py tests/test_capture_pre_keyring_ref.py tests/integration/fixtures/phase10-pre-keyring-ref.txt uv.lock
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "feat: add strict versioned master keyrings"
```

Expected cached names: exactly the fifteen paths in `git add`, and the recorded ref is the parent execution baseline rather than this commit.

### Task 2: Add Scoped Rewrap Without Touching Ciphertext or Timestamps

**Files:**
- Modify: `packages/secrets/src/jhin_secrets/crypto.py`
- Modify: `packages/secrets/src/jhin_secrets/store.py`
- Modify: `packages/secrets/src/jhin_secrets/material.py`
- Modify: `packages/secrets/src/jhin_secrets/redaction.py`
- Modify: `packages/secrets/src/jhin_secrets/__init__.py`
- Modify: `packages/secrets/tests/test_crypto.py`
- Modify: `packages/secrets/tests/test_store.py`
- Modify: `packages/secrets/tests/test_redaction.py`

**Interfaces:**
- Consumes: Task 1's already-functional mixed-version `SecretCrypto`, existing `EncryptedPayload`, Phase 9 `nonce + wrapped DEK`, `SecretRedactor`, and `TimestampMixin.updated_at.onupdate`.
- Produces: `RewrappedPayload`, `SecretVerificationError`, reference-counted `scoped_secret_material`, and an exact three-column textual rewrap update that cannot trigger ORM `updated_at` behavior; existing store create/credential-rotate/reveal behavior stays unchanged.

- [ ] **Step 1: Write failing legacy, mixed-version, rewrap, and invariant tests**

```python
def crypto_ring(active: int = 1) -> SecretCrypto:
    return SecretCrypto(
        MasterKeyRing(active_version=active, _keys={1: b"1" * 32, 2: b"2" * 32})
    )


def unwrap_for_test(master_key: bytes, wrapped_data_key: bytes) -> bytes:
    wrap_nonce = wrapped_data_key[:12]
    wrapped_dek = wrapped_data_key[12:]
    return AESGCM(master_key).decrypt(wrap_nonce, wrapped_dek, None)


def test_active_writer_and_both_version_readers_share_legacy_format() -> None:
    old = SecretCrypto(MasterKey(b"1" * 32, version=1)).encrypt("legacy-canary-value")
    ring = crypto_ring(active=2)
    new = ring.encrypt("new-canary-value")
    assert old.key_version == 1
    assert new.key_version == 2
    assert ring.decrypt(old) == "legacy-canary-value"
    assert ring.decrypt(new) == "new-canary-value"
    assert len(old.wrapped_data_key) == len(new.wrapped_data_key)


def test_rewrap_preserves_ciphertext_nonce_and_dek(monkeypatch: pytest.MonkeyPatch) -> None:
    ring = crypto_ring(active=2)
    original = SecretCrypto(MasterKey(b"1" * 32, version=1)).encrypt("rewrap-canary-value")
    redactor = get_redactor()
    baseline = redactor.registered_value_count
    before_dek = unwrap_for_test(b"1" * 32, original.wrapped_data_key)
    migrated = ring.rewrap(original, to_version=2)
    after_dek = unwrap_for_test(b"2" * 32, migrated.wrapped_data_key)
    assert before_dek == after_dek
    assert migrated.key_version == 2
    assert migrated.wrapped_data_key != original.wrapped_data_key
    assert migrated.fingerprint == ring.fingerprint("rewrap-canary-value", key_version=2)
    assert redactor.registered_value_count == baseline
    assert redactor.redact_text("rewrap-canary-value") == "rewrap-canary-value"


def test_verify_uses_constant_time_target_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    ring = crypto_ring(active=2)
    payload = ring.encrypt("verify-canary-value")
    comparisons: list[tuple[str, str]] = []
    real_compare = hmac.compare_digest
    monkeypatch.setattr(
        crypto_module.hmac,
        "compare_digest",
        lambda left, right: comparisons.append((left, right)) or real_compare(left, right),
    )
    ring.verify(payload, expected_version=2)
    assert comparisons == [(payload.fingerprint, ring.fingerprint("verify-canary-value", key_version=2))]


def test_rewrap_and_verify_errors_never_render_sensitive_fields() -> None:
    ring = crypto_ring(active=2)
    payload = SecretCrypto(MasterKey(b"1" * 32)).encrypt("error-canary-value")
    broken = dataclasses.replace(payload, ciphertext=b"error-canary-ciphertext")
    with pytest.raises(SecretDecryptionError) as excinfo:
        ring.rewrap(broken, to_version=2)
    rendered = str(excinfo.value) + repr(excinfo.value) + repr(payload) + repr(broken)
    for forbidden in ("error-canary-value", "error-canary-ciphertext", payload.fingerprint):
        assert forbidden not in rendered


def test_scoped_material_survives_exception_conversion_then_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ring = crypto_ring(active=2)
    payload = SecretCrypto(MasterKey(b"1" * 32)).encrypt("scope-canary-value")
    redactor = get_redactor()
    baseline = redactor.registered_value_count
    seen_inside: list[str] = []

    def fail_while_scoped(*args: object, **kwargs: object) -> str:
        seen_inside.append(redactor.redact_text("scope-canary-value"))
        raise RuntimeError("scope-canary-value")

    monkeypatch.setattr(crypto_module, "_target_fingerprint", fail_while_scoped)
    with pytest.raises(SecretVerificationError, match="secret_verification_failed"):
        ring.rewrap(payload, to_version=2)
    assert seen_inside == [REDACTED]
    assert redactor.registered_value_count == baseline
    assert redactor.redact_text("scope-canary-value") == "scope-canary-value"
```

In `test_store.py`, persist one v1 and one v2 row in SQLite, reveal both through a ring active at v2, create and credential-rotate a third row, and assert new/rotated rows use v2. Capture `created_at`, `updated_at`, `rotated_at`, `last_used_at`, ciphertext, nonce, masked hint, and ownership before rewrap. Attach SQLAlchemy `before_cursor_execute`, execute the planned `rewrap_secret_row(session, secret_id, expected_from_version, payload)`, and assert the normalized `UPDATE secret SET` list is exactly `wrapped_data_key`, `key_version`, `secret_fingerprint`; after commit/refresh every captured nonwrapper field and all four timestamps are equal. A stale expected source version updates zero rows and returns the closed concurrent-change error. Add missing-source/target version, corrupt wrap, corrupt ciphertext, wrong expected version, wrong fingerprint, invalid UTF-8, and safe repr tests.

In `test_redaction.py`, nest two scopes containing one shared value, keep one permanent registration alive across a scope, inject an exception, and assert counts return to baseline in every `finally`. Rewrap 1,001 distinct rows one at a time and assert `registered_value_count` returns to baseline after every row rather than growing with the batch.

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/secrets/tests/test_crypto.py packages/secrets/tests/test_store.py packages/secrets/tests/test_redaction.py -q
```

Expected: FAIL because rewrap/verify, scoped reference-counted registration, exact textual wrapper update, and timestamp-preservation behavior do not exist; Task 1's active-writer/version-reader compatibility tests remain green.

- [ ] **Step 3: Implement the minimal multi-version crypto**

Retain Task 1's `MasterKey | MasterKeyRing` normalization and active-writer/version-reader behavior. Factor private `_unwrap_dek`, `_decrypt_plaintext`, `_target_fingerprint`, and `_wrap_dek` helpers. `rewrap` decrypts, enters `scoped_secret_material(plaintext)`, converts every fallible fingerprint/wrap/encoding error to a closed exception before leaving the scope, returns only `RewrappedPayload`, and drops its local plaintext reference in `finally`. `verify` requires the expected row version, decrypts, enters the same scope, recomputes with the explicit target key, uses `compare_digest`, converts failure while the scope still protects logs, and drops its local reference in `finally`. Do not claim Python can zero immutable string memory; the enforceable boundary is one-row lifetime, reference-counted cleanup, no return/persistence/logging, and protection through exception conversion.

Keep `SecretStore` API signatures unchanged. Add only this internal helper next to it, implemented with `text()` so SQLAlchemy column defaults/onupdate cannot augment the statement:

```python
async def rewrap_secret_row(
    session: AsyncSession,
    *,
    secret_id: UUID,
    expected_from_version: int,
    payload: RewrappedPayload,
) -> bool:
    result = await session.execute(
        text(
            "UPDATE secret "
            "SET wrapped_data_key=:wrapped_data_key, key_version=:key_version, "
            "secret_fingerprint=:secret_fingerprint "
            "WHERE id=:secret_id AND key_version=:expected_from_version"
        ),
        {
            "wrapped_data_key": payload.wrapped_data_key,
            "key_version": payload.key_version,
            "secret_fingerprint": payload.fingerprint,
            "secret_id": secret_id,
            "expected_from_version": expected_from_version,
        },
    )
    return result.rowcount == 1
```

Do not assign wrapper fields on a loaded `Secret`, call ORM `update(Secret)`, include `updated_at=Secret.updated_at`, add a second version column, or add a public bulk method to `SecretStore`.

- [ ] **Step 4: Run GREEN and commit exactly the crypto slice**

```bash
uv run pytest packages/secrets/tests/test_crypto.py packages/secrets/tests/test_store.py packages/secrets/tests/test_redaction.py -q
uv run ruff check packages/secrets
uv run mypy packages/secrets/src
git add packages/secrets/src/jhin_secrets/crypto.py packages/secrets/src/jhin_secrets/store.py packages/secrets/src/jhin_secrets/material.py packages/secrets/src/jhin_secrets/redaction.py packages/secrets/src/jhin_secrets/__init__.py packages/secrets/tests/test_crypto.py packages/secrets/tests/test_store.py packages/secrets/tests/test_redaction.py
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "feat: add scoped master key rewrap"
```

Expected cached names: exactly the eight paths in `git add`.

### Task 3: Add Durable Rotation State at Alembic `0017`

**Files:**
- Create: `packages/db/src/jhin_db/models/key_rotation.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260818_0017_master_key_rotation.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Create: `packages/db/tests/test_master_key_rotation_model.py`
- Create: `tests/integration/test_phase10_master_key_rotation_migration.py`
- Modify: `apps/api/src/jhin_api/health/checks.py`
- Modify: `apps/api/tests/test_health.py`

**Interfaces:**
- Consumes: DLQ revision `0016`, `UuidPkMixin`, `UtcDateTime`, existing `secret.ciphertext`/`nonce`, and the real-PostgreSQL migration fixture pattern established by protected health/DLQ.
- Produces: the Shared Interfaces `MasterKeyRotation` export including the four nullable retirement-fence columns, one partial unique active-rotation index, scalar `secret_credential_mutation_generation_seq`, statement trigger `trg_secret_credential_mutation_generation` that both advances semantic-mutation generation and rejects those statements while a fence is armed, single head `0017`, and protected schema health comparing the live revision to the packaged head `0017` rather than stale `0016`.

The health interface is exact:

```python
# apps/api/src/jhin_api/health/checks.py
class PackagedSchemaError(RuntimeError):
    pass

def packaged_schema_head() -> str:
    heads = ScriptDirectory.from_config(alembic_config("sqlite://")).get_heads()
    if len(heads) != 1 or REVISION_PATTERN.fullmatch(heads[0]) is None:
        raise PackagedSchemaError("packaged_schema_head_invalid")
    return heads[0]
```

`probe_database` calls this helper once; no revision literal exists in runtime health code.

- [ ] **Step 1: Write graph/model tests and real-PostgreSQL migration-contract tests**

```python
def test_master_key_rotation_is_only_head_after_dlq() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = scripts.get_revision("0017")
    assert revision is not None
    assert revision.down_revision == "0016"
    assert scripts.get_heads() == ["0017"]


def test_0017_has_no_secret_backfill_or_secret_column_ddl() -> None:
    MIGRATION_0017 = ROOT / (
        "packages/db/src/jhin_db/alembic/versions/"
        "20260818_0017_master_key_rotation.py"
    )
    source = MIGRATION_0017.read_text(encoding="utf-8")
    assert "UPDATE secret SET" not in source
    assert "INSERT INTO secret" not in source
    assert "ADD COLUMN" not in source
    assert "secret_credential_mutation_generation_seq" in source
    assert "trg_secret_credential_mutation_generation" in source
    assert "retirement_fence_id IS NOT NULL" in source
    assert "BEFORE INSERT OR DELETE OR UPDATE OF ciphertext, nonce" in source
    assert "AFTER INSERT OR DELETE OR UPDATE OF ciphertext, nonce" not in source


async def test_database_probe_uses_packaged_head_0017(database_probe_world: DatabaseProbeWorld) -> None:
    database_probe_world.current_revisions = ["0017"]
    result = await database_probe_world.probe()
    assert result.packaged_head == "0017"
    assert result.current_revision == "0017"
    assert result.schema_component.status == HealthStatus.OK


async def test_only_one_nonterminal_rotation_is_allowed(session: AsyncSession) -> None:
    session.add(rotation(from_version=1, to_version=2, status="prepared"))
    await session.commit()
    session.add(rotation(from_version=2, to_version=3, status="rewrapping"))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.parametrize(
    "overrides",
    [
        {"from_version": 0},
        {"to_version": -1},
        {"from_version": 2, "to_version": 2},
        {"status": "failed"},
        {"rows_total": -1},
        {"status": "completed", "completed_at": None},
        {"status": "rewrapping", "completed_at": NOW},
        {"status": "verifying", "credential_mutation_generation": None},
        {"status": "completed", "credential_mutation_generation": None},
        {"status": "prepared", "credential_mutation_generation": 1},
        {"credential_mutation_generation": -1},
        {"retirement_fence_id": uuid4()},
        {
            "status": "completed",
            "completed_at": NOW,
            "credential_mutation_generation": 7,
            "retirement_fence_id": uuid4(),
            "retirement_fence_generation": 7,
            "retirement_fence_started_at": NOW,
            "retirement_fence_deadline": None,
        },
        {
            "status": "rewrapping",
            "retirement_fence_id": uuid4(),
            "retirement_fence_generation": 7,
            "retirement_fence_started_at": NOW,
            "retirement_fence_deadline": NOW + timedelta(seconds=600),
        },
        {
            "status": "completed",
            "completed_at": NOW,
            "credential_mutation_generation": 7,
            "retirement_fence_id": uuid4(),
            "retirement_fence_generation": 8,
            "retirement_fence_started_at": NOW,
            "retirement_fence_deadline": NOW + timedelta(seconds=600),
        },
        {
            "status": "completed",
            "completed_at": NOW,
            "credential_mutation_generation": 7,
            "retirement_fence_id": uuid4(),
            "retirement_fence_generation": 7,
            "retirement_fence_started_at": NOW,
            "retirement_fence_deadline": NOW,
        },
        {"safe_error_code": "x" * 65},
    ],
)
async def test_rotation_constraints_fail_closed(
    session: AsyncSession, overrides: dict[str, object]
) -> None:
    session.add(rotation(**overrides))
    with pytest.raises(IntegrityError):
        await session.commit()
```

Add this real-PostgreSQL trigger contract using plain SQL writes so it also represents previous-image behavior:

```python
@pytest.mark.integration
async def test_credential_mutation_generation_tracks_only_semantic_secret_changes(
    migrated_pg: MigratedPostgres,
) -> None:
    assert await migrated_pg.credential_mutation_generation() == 0
    secret_id = await migrated_pg.insert_secret_with_plain_phase9_sql()
    assert await migrated_pg.credential_mutation_generation() == 1

    await migrated_pg.execute(
        "UPDATE secret SET wrapped_data_key=:wrapped, key_version=2, "
        "secret_fingerprint=:fingerprint WHERE id=:id",
        wrapped=b"wrapper-only", fingerprint="wrapper-fingerprint", id=secret_id,
    )
    assert await migrated_pg.credential_mutation_generation() == 1

    await migrated_pg.execute(
        "UPDATE secret SET ciphertext=:ciphertext WHERE id=:id",
        ciphertext=b"rotated-ciphertext", id=secret_id,
    )
    assert await migrated_pg.credential_mutation_generation() == 2
    await migrated_pg.execute(
        "UPDATE secret SET nonce=:nonce WHERE id=:id",
        nonce=b"rotated-nonce", id=secret_id,
    )
    assert await migrated_pg.credential_mutation_generation() == 3
    await migrated_pg.execute("DELETE FROM secret WHERE id=:id", id=secret_id)
    assert await migrated_pg.credential_mutation_generation() == 4

    before_rollback = await migrated_pg.credential_mutation_generation()
    await migrated_pg.insert_then_rollback_phase9_sql()
    assert await migrated_pg.credential_mutation_generation() > before_rollback


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    ["insert", "ciphertext-update", "nonce-update", "delete"],
)
async def test_armed_retirement_fence_rejects_previous_image_semantic_sql(
    migrated_pg: MigratedPostgres,
    mutation: str,
) -> None:
    secret_id = await migrated_pg.insert_secret_with_plain_phase9_sql()
    generation = await migrated_pg.credential_mutation_generation()
    snapshot = await migrated_pg.secret_snapshot()
    await migrated_pg.seed_completed_rotation_and_arm_fence(
        from_version=1,
        to_version=2,
        generation=generation,
        started_at=NOW,
        deadline=NOW + timedelta(seconds=600),
    )

    sqlstate = await migrated_pg.execute_previous_image_mutation_rejected(
        mutation=mutation,
        target_id=secret_id,
    )
    assert sqlstate == "55006"
    assert await migrated_pg.credential_mutation_generation() == generation
    assert await migrated_pg.secret_snapshot() == snapshot


@pytest.mark.integration
async def test_armed_fence_allows_wrapper_only_rewrap_then_clear_restores_writes(
    migrated_pg: MigratedPostgres,
) -> None:
    secret_id = await migrated_pg.insert_secret_with_plain_phase9_sql()
    generation = await migrated_pg.credential_mutation_generation()
    await migrated_pg.seed_completed_rotation_and_arm_fence(
        from_version=1,
        to_version=2,
        generation=generation,
        started_at=NOW,
        deadline=NOW + timedelta(seconds=600),
    )
    await migrated_pg.execute(
        "UPDATE secret SET wrapped_data_key=:wrapped, key_version=2, "
        "secret_fingerprint=:fingerprint WHERE id=:id",
        wrapped=b"wrapper-only",
        fingerprint="wrapper-fingerprint",
        id=secret_id,
    )
    assert await migrated_pg.credential_mutation_generation() == generation

    await migrated_pg.clear_retirement_fence()
    await migrated_pg.execute(
        "UPDATE secret SET ciphertext=:ciphertext WHERE id=:id",
        ciphertext=b"allowed-after-cancel",
        id=secret_id,
    )
    assert await migrated_pg.credential_mutation_generation() == generation + 1
```

`MigratedPostgres.execute_previous_image_mutation_rejected()` opens a fresh transaction for each case, executes exactly one of the four literal Phase 9 shapes below with fixture-owned UUID/workspace/name/bytes, captures only `orig.sqlstate`, rolls back, and returns that five-character code:

```python
PREVIOUS_IMAGE_SEMANTIC_SQL = {
    "insert": (
        "INSERT INTO secret "
        "(id, workspace_id, name, type, ciphertext, nonce, wrapped_data_key, "
        "key_version, secret_fingerprint, masked_hint, created_by_user_id, "
        "last_used_at, rotated_at) VALUES "
        "(:new_id, :workspace_id, :new_name, 'api_key', :ciphertext, :nonce, "
        ":wrapped_data_key, 1, :fingerprint, '', NULL, NULL, NULL)"
    ),
    "ciphertext-update": (
        "UPDATE secret SET ciphertext=:ciphertext WHERE id=:target_id"
    ),
    "nonce-update": "UPDATE secret SET nonce=:nonce WHERE id=:target_id",
    "delete": "DELETE FROM secret WHERE id=:target_id",
}
```

The real-PostgreSQL module creates two isolated databases. For `fresh`, run `base -> head`; for `previous_head`, run `base -> 0016`, insert representative heartbeat, DLQ/retry, secret, and audit rows, then `0016 -> 0017 -> 0016 -> 0017`. At every point assert exact current revision plus rotation-table/sequence/function/trigger presence. The first upgrade starts at generation zero without scanning/backfilling existing secrets; a plain Phase 9 insert/update/delete after upgrade advances it. After downgrade, assert the trigger/function/sequence and only `master_key_rotation` are gone and every seeded `0016`/earlier row and `secret` column value is byte-for-byte unchanged. Re-upgrade recreates generation zero for the then-current dataset and subsequent mutations advance it.

- [ ] **Step 2: Run RED before creating the model/migration**

```bash
uv run pytest packages/db/tests/test_migration_graph.py packages/db/tests/test_master_key_rotation_model.py apps/api/tests/test_health.py -q
JHIN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres uv run pytest -m integration tests/integration/test_phase10_master_key_rotation_migration.py -q
```

Expected: both commands FAIL because revision/model `0017` do not exist and the health probe still expects the prior packaged head. The PostgreSQL command must be observed failing against a real server before implementation.

- [ ] **Step 3: Implement exact checks and the partial unique index**

Create checks for positive/distinct versions, closed status, nonnegative counters/generation, generation required in `verifying|completed` and absent in `prepared|rewrapping`, safe-code length, and terminal/completed timestamp agreement. The four fence columns must be all null or all nonnull; a nonnull fence is allowed only on `completed`, has `retirement_fence_generation = credential_mutation_generation >= 0`, and has `retirement_fence_deadline > retirement_fence_started_at`. Create:

```python
Index(
    "uq_master_key_rotation_active",
    literal_column("1"),
    unique=True,
    postgresql_where=text("status IN ('prepared','rewrapping','verifying')"),
    sqlite_where=text("status IN ('prepared','rewrapping','verifying')"),
)
```

Use `BigInteger` counters and nullable `credential_mutation_generation`/`retirement_fence_generation`, `Uuid` checkpoint/fence ID, timezone-aware fence timestamps, server-default `started_at`, and no updated-at/key/path/material JSON column. On PostgreSQL, create `secret_credential_mutation_generation_seq AS bigint START WITH 1 NO CYCLE`; generation reads use `CASE WHEN is_called THEN last_value ELSE 0 END`, so migration has scalar zero authority without a row scan. Create `jhin_bump_secret_credential_mutation_generation()` returning trigger and this statement-level trigger (the trigger return is ignored):

```sql
CREATE SEQUENCE secret_credential_mutation_generation_seq
AS bigint START WITH 1 NO CYCLE;

CREATE FUNCTION jhin_bump_secret_credential_mutation_generation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM master_key_rotation
        WHERE retirement_fence_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55006',
            MESSAGE = 'master_key_retirement_fenced';
    END IF;
    PERFORM nextval('secret_credential_mutation_generation_seq');
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_secret_credential_mutation_generation
BEFORE INSERT OR DELETE OR UPDATE OF ciphertext, nonce ON secret
FOR EACH STATEMENT
EXECUTE FUNCTION jhin_bump_secret_credential_mutation_generation();
```

Execute this sequence/function/trigger SQL only when `op.get_bind().dialect.name == "postgresql"`; the existing SQLite test branch creates the rotation table/constraints but no imitation trigger and can run only with Task 5's injected fake source/fence. Do not trigger on `wrapped_data_key`, `key_version`, or `secret_fingerprint`; Task 2's exact wrapper statement therefore does not invalidate its own verification pass and remains permitted during the handoff. The fixed `55006` signal is translated at the application boundary to `retirement_fence_active`; raw PostgreSQL text never reaches CLI/API output. Sequence advancement is deliberately nontransactional: gaps caused by rollback conservatively force a full pass. Downgrade drops trigger, function, sequence, then the rotation table/index and its fence columns and nothing older on PostgreSQL; SQLite drops only its rotation table/index. Extract or retain the exact `packaged_schema_head()` helper above, make `probe_database` call it, and require exactly one bounded head; remove any literal `0016` comparison rather than replacing it with a second hard-coded revision authority.

- [ ] **Step 4: Run GREEN and commit schema only**

```bash
uv run pytest packages/db/tests/test_migration_graph.py packages/db/tests/test_master_key_rotation_model.py apps/api/tests/test_health.py -q
JHIN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres uv run pytest -m integration tests/integration/test_phase10_master_key_rotation_migration.py -q
uv run ruff check packages/db tests/integration/test_phase10_master_key_rotation_migration.py
uv run mypy packages/db/src tests/integration/test_phase10_master_key_rotation_migration.py
git add packages/db/src/jhin_db/models/key_rotation.py packages/db/src/jhin_db/models/__init__.py packages/db/src/jhin_db/alembic/versions/20260818_0017_master_key_rotation.py packages/db/tests/test_migration_graph.py packages/db/tests/test_master_key_rotation_model.py tests/integration/test_phase10_master_key_rotation_migration.py apps/api/src/jhin_api/health/checks.py apps/api/tests/test_health.py
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "feat: add durable master key rotation state"
```

Expected cached names: exactly the eight paths in `git add`.

### Task 4: Load the Complete Ring in Exactly Three Key-Bearing Services

**Files:**
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/src/jhin_api/seed.py`
- Create: `apps/api/tests/test_keyring_startup.py`
- Create: `apps/api/tests/test_seed.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Create: `services/agent_worker/tests/test_keyring_resources.py`
- Modify: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Create: `services/tool_worker/tests/test_keyring_resources.py`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Create: `tests/test_master_key_service_boundary.py`
- Create: `tests/test_master_key_compose.py`

**Interfaces:**
- Consumes: Tasks 1–2 loader/crypto, prior `Settings.app_env`, the API's current unsafe optional-key degradation, transient `jhin-seed-dev`, agent/tool `Resources`, protected-health `HeartbeatState`, and prior Compose topology.
- Produces: all three long-lived processes loading one immutable ring at startup and reporting exact active/supported numbers through existing heartbeat providers; API degradation is restricted to dev/test while every production loader failure aborts startup with a closed `MasterKeyError`; the transient seed command supports legacy/versioned files and fails closed on production inline input; no fourth long-lived process gains key access.

- [ ] **Step 1: Write startup/error/heartbeat and static ownership tests**

```python
def write_startup_ring(path: Path, *, raw: bytes | None = None) -> Path:
    material = base64.b64encode(b"S" * 32).decode("ascii")
    path.write_bytes(
        raw
        if raw is not None
        else json.dumps(
            {"active_version": 1, "keys": {"1": material}},
            separators=(",", ":"),
        ).encode("ascii")
    )
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("service", ["api", "agent-worker", "tool-worker"])
def test_each_key_service_reports_the_exact_ring(service: str, keyring_file: Path) -> None:
    process = start_service_resource_for_test(
        service,
        app_env="test",
        environ={"MASTER_KEY_FILE": str(keyring_file)},
    )
    assert process.crypto.active_key_version == 2
    assert process.crypto.supported_key_versions == (1, 2)
    state = asyncio.run(process.heartbeat_state())
    assert (state.active_key_version, state.supported_key_versions) == (2, (1, 2))


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_api_degrades_only_in_dev_or_test_with_a_safe_loader_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, app_env: str
) -> None:
    path = tmp_path / "PATH_CANARY_DO_NOT_LOG"
    path.write_text("KEY_MATERIAL_CANARY_DO_NOT_LOG", encoding="utf-8")
    path.chmod(0o644)
    with caplog.at_level("WARNING"):
        app = create_app(Settings(app_env=app_env), environ={"MASTER_KEY_FILE": str(path)})
    assert app.state.secret_crypto is None
    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "master_key_file_unsafe" in rendered
    assert "PATH_CANARY_DO_NOT_LOG" not in rendered
    assert "KEY_MATERIAL_CANARY_DO_NOT_LOG" not in rendered


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("missing", "master_key_not_configured"),
        ("unsafe", "master_key_file_unsafe"),
        ("invalid", "master_key_document_invalid"),
        ("inline", "master_key_inline_forbidden"),
    ],
)
def test_production_api_startup_fails_closed_in_a_subprocess(
    tmp_path: Path,
    scenario: str,
    expected_code: str,
) -> None:
    import_key = write_startup_ring(tmp_path / "import-key")
    unsafe = write_startup_ring(tmp_path / "PATH_CANARY_UNSAFE")
    unsafe.chmod(0o644)
    invalid = write_startup_ring(
        tmp_path / "PATH_CANARY_INVALID", raw=b"KEY_MATERIAL_CANARY_INVALID"
    )
    inline = base64.b64encode(b"INLINE_API_CANARY_VALUE_123456789"[:32]).decode()
    probe = """
import os
import sys
from jhin_api.main import create_app
from jhin_api.settings import Settings
from jhin_secrets import MasterKeyError

scenario = os.environ["JHIN_STARTUP_PROBE_SCENARIO"]
cases = {
    "missing": {},
    "unsafe": {"MASTER_KEY_FILE": os.environ["JHIN_STARTUP_PROBE_UNSAFE"]},
    "invalid": {"MASTER_KEY_FILE": os.environ["JHIN_STARTUP_PROBE_INVALID"]},
    "inline": {"MASTER_KEY": os.environ["JHIN_STARTUP_PROBE_INLINE"]},
}
try:
    create_app(Settings(app_env="production"), environ=cases[scenario])
except MasterKeyError as exc:
    sys.stderr.write(exc.code.value + "\\n")
    raise SystemExit(78)
raise SystemExit(0)
"""
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MASTER_KEY", "MASTER_KEY_FILE"}
    }
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env={
            **clean_env,
            # The module-level compatibility app imports with a valid production key;
            # the explicit create_app call is the production startup under test.
            "APP_ENV": "production",
            "MASTER_KEY_FILE": str(import_key),
            "JHIN_STARTUP_PROBE_SCENARIO": scenario,
            "JHIN_STARTUP_PROBE_UNSAFE": str(unsafe),
            "JHIN_STARTUP_PROBE_INVALID": str(invalid),
            "JHIN_STARTUP_PROBE_INLINE": inline,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        78, "", f"{expected_code}\n"
    )
    for forbidden in (
        str(unsafe), str(invalid), inline,
        "PATH_CANARY", "KEY_MATERIAL_CANARY", "INLINE_API_CANARY",
    ):
        assert forbidden not in result.stdout + result.stderr


def test_only_expected_services_and_transient_seed_load_master_key() -> None:
    callers = ast_service_callers(
        "load_master_key",
        roots=(Path("apps/api/src"), Path("services")),
    )
    assert callers == {
        "apps/api/src/jhin_api/main.py",
        "apps/api/src/jhin_api/seed.py",
        "services/agent_worker/src/jhin_agent_worker/resources.py",
        "services/tool_worker/src/jhin_tool_worker/resources.py",
    }
    assert classify_key_callers(callers) == {
        "long_lived_key_service": {
            "apps/api/src/jhin_api/main.py",
            "services/agent_worker/src/jhin_agent_worker/resources.py",
            "services/tool_worker/src/jhin_tool_worker/resources.py",
        },
        "transient_seed_command": {"apps/api/src/jhin_api/seed.py"},
    }
    rootful = compose_config(
        files=("compose.yaml", "compose.rootful.yaml"),
        env={
            "APP_ENV": "production",
            "SANDBOX_DOCKER_GID": "4242",
            "SANDBOX_DOCKER_SOCKET_HOST": "/var/run/docker.sock",
            "MASTER_KEY_FILE_HOST": "/tmp/EXPECTED_CONFIGURED_KEY_SOURCE",
        },
    )
    assert rootful["secrets"]["jhin_master_key"]["file"] == (
        "/tmp/EXPECTED_CONFIGURED_KEY_SOURCE"
    )
    rendered = rootful["services"]
    for service in ("api", "agent-worker", "tool-worker"):
        assert rendered[service]["environment"]["MASTER_KEY_FILE"] == "/run/secrets/jhin_master_key"
        assert rendered[service]["secrets"] == [{
            "source": "jhin_master_key",
            "target": "/run/secrets/jhin_master_key",
        }]
    for service in set(rendered) - {"api", "agent-worker", "tool-worker"}:
        assert "MASTER_KEY_FILE" not in rendered[service].get("environment", {})
        assert all(item.get("source") != "jhin_master_key" for item in rendered[service].get("secrets", []))
    rootless = compose_config(
        files=("compose.yaml", "compose.rootless.yaml"),
        env_without={"SANDBOX_DOCKER_GID", "SANDBOX_DOCKER_SOCKET_HOST"},
    )
    assert "SANDBOX_DOCKER_GID" not in json.dumps(rootless)
    assert "SANDBOX_DOCKER_SOCKET_HOST" not in json.dumps(rootless)


@pytest.mark.parametrize("kind", ["legacy", "versioned"])
def test_seed_loads_file_formats_and_uses_active_writer(
    tmp_path: Path, kind: str
) -> None:
    path = write_legacy_v1(tmp_path) if kind == "legacy" else write_ring(
        tmp_path, active=2, versions=(1, 2)
    )
    crypto = _load_seed_crypto(
        environ={"MASTER_KEY_FILE": str(path)}, app_env="production"
    )
    assert crypto is not None
    assert crypto.active_key_version == (1 if kind == "legacy" else 2)


def test_seed_rejects_production_inline_without_echoing_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = base64.b64encode(b"INLINE_SEED_CANARY_VALUE_123456"[:32]).decode()
    with pytest.raises(MasterKeyError, match="master_key_inline_forbidden"):
        _load_seed_crypto(
            environ={"MASTER_KEY": marker}, app_env="production"
        )
    assert marker not in " ".join(record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(("kind", "expected_version"), [("legacy", 1), ("versioned", 2)])
async def test_seed_writes_with_file_active_version(
    session: AsyncSession,
    tmp_path: Path,
    kind: str,
    expected_version: int,
) -> None:
    path = write_legacy_v1(tmp_path) if kind == "legacy" else write_ring(
        tmp_path, active=2, versions=(1, 2)
    )
    result = await seed(
        session,
        environ={"MASTER_KEY_FILE": str(path)},
        app_env="production",
    )
    assert result.startswith("seeded:")
    versions = set(await session.scalars(select(Secret.key_version)))
    assert versions == {expected_version}
```

Add agent/tool tests that unsafe/missing/inline-production keyring causes resource creation to fail with only a safe code. API may remain started with `master_key_unavailable` only in normalized dev/test; production missing, unsafe, invalid, or inline configuration must raise its closed `MasterKeyError` before `create_app` returns and before a server begins accepting traffic. The subprocess matrix above is mandatory because the live module currently constructs a compatibility app at import time; its valid test-only import key is not the production case under test. In `apps/api/tests/test_seed.py`, missing key is tolerated only in normalized dev/test, every other loader error is raised by code only, and `run()` passes explicit `APP_ENV` plus a copied environment into `seed(session, *, environ, app_env)`. Exercise `seed()` against isolated SQLite schemas with a legacy v1 file and versioned JSON active v2, then assert seeded secret row versions match; add a subprocess production-inline case whose stderr is only `master_key_inline_forbidden`. Assert no key bytes, inline values, or unexpected/sensitive host paths enter structlog capture, heartbeat rows, process health files, Compose environment, or HTTP. `docker compose config` is expected to contain exactly the configured `MASTER_KEY_FILE_HOST` source and fixed container target `/run/secrets/jhin_master_key`; service environments may contain only that fixed target, never the host source or material. In `test_master_key_compose.py`, assert file-backed top-level source remains `file: ${MASTER_KEY_FILE_HOST:-./secrets/dev/jhin_master_key}` and service grants do not declare `uid`, `gid`, or `mode`, because Compose silently ignores those attributes for file sources. Assert `docker/python.Dockerfile` fixes the runtime user at UID 10001 and do not mistake a host-owned `0600` file for a readable runtime file.

- [ ] **Step 2: Run RED**

```bash
uv run pytest apps/api/tests/test_keyring_startup.py apps/api/tests/test_seed.py services/agent_worker/tests/test_keyring_resources.py services/tool_worker/tests/test_keyring_resources.py tests/test_master_key_service_boundary.py tests/test_master_key_compose.py -q
```

Expected: FAIL because production API currently catches the unsafe-key error and returns `secret_crypto=None`, services/seed do not all pass `app_env`, safe loader errors are not wired, tool resources do not expose the complete ring, caller classification is absent, and Compose/docs still describe a single key/two consumers.

- [ ] **Step 3: Wire settings explicitly and preserve heartbeat authority**

Change API to `create_app(settings: Settings | None = None, *, environ: Mapping[str, str] | None = None) -> FastAPI` and `_load_secret_crypto(settings: Settings, environ: Mapping[str, str] | None = None) -> SecretCrypto | None`; resolve the mapping with `os.environ if environ is None else environ`, never `environ or os.environ`, so an explicit empty mapping exercises the missing-key case. `_load_secret_crypto` catches `MasterKeyError` only to return `None` plus a safe `reason_code` when normalized `settings.app_env in {"dev", "test"}`; for `production`/`prod` it re-raises the original closed exception for **every** code (`NOT_CONFIGURED`, `FILE_UNREADABLE`, `FILE_UNSAFE`, `DOCUMENT_INVALID`, `INLINE_FORBIDDEN`, or `VERSION_UNAVAILABLE`) without logging raw exception text, so `create_app` cannot return a degraded production app. Change both worker factories to `Resources.create(settings: Settings, *, environ: Mapping[str, str] | None = None) -> Resources` and call `load_master_key(environ, app_env=settings.app_env)`. Change seed to `_load_seed_crypto(*, environ: Mapping[str, str], app_env: str) -> SecretCrypto | None` and `seed(session, *, environ: Mapping[str, str] | None = None, app_env: str | None = None) -> str`; only `NOT_CONFIGURED` in dev/test returns `None`, while production inline/unsafe/invalid/missing inputs raise the closed `MasterKeyError`. `run()` maps it to one safe stderr line. `ast_service_callers` scans Python files only beneath supplied roots, ignores tests/package internals, and classifies seed separately from the fixed long-lived set. Log only `reason_code=exc.code.value`; do not pass `exc`, the path, or a traceback field. Do not reload files in heartbeat callbacks or per request/activity: every callback reads the process-lifetime `SecretCrypto` accessors.

Update only Compose comments/grants to say versioned keyring and all three key-bearing services. Keep file source/short service grants so host permissions are authoritative; do not inject key JSON through Compose environment. Update `.env.example` with exact three long-lived consumers, UID `10001:10001` plus mode `0600` for the installed runtime copy, a warning that `chmod 600` on a differently owned host file is insufficient, and the legacy-v1 compatibility warning. Static rootful renders pass `SANDBOX_DOCKER_GID=4242` and `SANDBOX_DOCKER_SOCKET_HOST=/var/run/docker.sock`; rootless renders explicitly remove both.

- [ ] **Step 4: Run GREEN and commit service wiring**

```bash
uv run pytest apps/api/tests/test_keyring_startup.py apps/api/tests/test_seed.py services/agent_worker/tests/test_keyring_resources.py services/tool_worker/tests/test_keyring_resources.py tests/test_master_key_service_boundary.py tests/test_master_key_compose.py packages/db/tests/test_heartbeat.py apps/api/tests/test_operations_health.py -q
uv run ruff check apps/api/src/jhin_api/main.py apps/api/src/jhin_api/seed.py apps/api/tests/test_keyring_startup.py apps/api/tests/test_seed.py services/agent_worker services/tool_worker tests/test_master_key_service_boundary.py tests/test_master_key_compose.py
uv run mypy apps/api/src services/agent_worker/src services/tool_worker/src
git add apps/api/src/jhin_api/main.py apps/api/src/jhin_api/seed.py apps/api/tests/test_keyring_startup.py apps/api/tests/test_seed.py services/agent_worker/src/jhin_agent_worker/resources.py services/agent_worker/tests/test_keyring_resources.py services/tool_worker/src/jhin_tool_worker/resources.py services/tool_worker/tests/test_keyring_resources.py compose.yaml .env.example tests/test_master_key_service_boundary.py tests/test_master_key_compose.py
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "feat: load keyrings in key-bearing services"
```

Expected cached names: exactly the twelve paths in `git add`.

### Task 5: Implement Replica Gates and Bounded Resumable Rewrap

**Files:**
- Create: `packages/secrets/src/jhin_secrets/rotation.py`
- Modify: `packages/secrets/src/jhin_secrets/__init__.py`
- Create: `packages/secrets/tests/test_rotation.py`
- Modify: `packages/tools/src/jhin_tools/gateway.py`
- Modify: `packages/tools/tests/test_gateway.py`
- Create: `tests/integration/test_phase10_master_key_rotation_postgres.py`

**Interfaces:**
- Consumes: Tasks 2–4 crypto/state/heartbeat, `AuditEvent`, the gateway's current wrapper-dependent authorization digest, and an already-held advisory lock connection.
- Produces: Shared Interfaces gate/result/runner/lease APIs, exact durable status transitions and completion-generation authority, same-transaction audits, wrapper-stable parked approvals, and real-PostgreSQL lock-loss/concurrency/resume evidence.

- [ ] **Step 1: Write pure gate and validation tests**

```python
@pytest.mark.parametrize(
    ("stage", "active", "supported"),
    [
        (RotationStage.DISTRIBUTED, 1, (1, 2)),
        (RotationStage.ACTIVATED, 2, (1, 2)),
        (RotationStage.RETIRED, 2, (2,)),
    ],
)
async def test_gate_requires_every_fresh_replica_exactly(
    session: AsyncSession,
    stage: RotationStage,
    active: int,
    supported: tuple[int, ...],
) -> None:
    for service in KEY_SERVICES:
        session.add(heartbeat(service, NOW, active=active, supported=supported))
    session.add(heartbeat("api", NOW - timedelta(seconds=31), active=99, supported=(99,)))
    await session.commit()
    gate = await check_replica_gate(
        session, stage=stage, from_version=1, to_version=2, now=NOW
    )
    assert gate.open is True
    assert gate.missing_services == ()
    assert gate.mismatched_instances == 0
    assert gate.future_instances == 0


async def test_one_fresh_mismatch_closes_gate_without_unioning_versions(
    session: AsyncSession,
) -> None:
    for service in KEY_SERVICES:
        session.add(heartbeat(service, NOW, active=2, supported=(1, 2)))
    session.add(heartbeat("tool-worker", NOW, active=1, supported=(1, 2)))
    await session.commit()
    gate = await check_replica_gate(
        session, stage=RotationStage.ACTIVATED, from_version=1, to_version=2, now=NOW
    )
    assert gate.open is False
    assert gate.mismatched_instances == 1
    assert [
        (row.service, row.active_version, row.supported_versions)
        for row in gate.distributions
    ] == [
        ("agent-worker", 2, (1, 2)),
        ("api", 2, (1, 2)),
        ("tool-worker", 1, (1, 2)),
        ("tool-worker", 2, (1, 2)),
    ]


async def test_reporter_freshness_is_closed_at_both_ends(
    session: AsyncSession,
) -> None:
    for service in KEY_SERVICES:
        session.add(heartbeat(service, NOW, active=2, supported=(1, 2)))
    session.add(heartbeat("api", NOW - timedelta(seconds=30), active=2, supported=(1, 2)))
    await session.commit()
    exact = await check_replica_gate(
        session, stage=RotationStage.ACTIVATED, from_version=1, to_version=2, now=NOW
    )
    assert (exact.open, exact.future_instances) == (True, 0)

    session.add(
        heartbeat("tool-worker", NOW + timedelta(microseconds=1), active=2, supported=(1, 2))
    )
    await session.commit()
    future = await check_replica_gate(
        session, stage=RotationStage.ACTIVATED, from_version=1, to_version=2, now=NOW
    )
    assert (future.open, future.future_instances) == (False, 1)
```

Also cover one microsecond older than the cutoff, missing service, missing/invalid metadata, duplicate/unsorted/33 supported versions injected directly, boolean/negative/overlarge versions, and more than 10,000 in-window rows. Query future key-service rows separately and fail closed before DTO construction; authority is exactly `cutoff <= last_seen_at <= checked_at`, never a lower-bound-only predicate. For `RETIREMENT_READY`, test latest 1->2 completed plus unchanged scalar mutation generation opens; fallback source/same-ID credential mutation/delete/create closes it; rewrap followed by a later aborted 1->2 attempt remains closed even if a test removes the source row; a newer completed verification at the current generation reopens it. Output is sorted closed service/version/count data only and never unions supported versions.

- [ ] **Step 2: Write transactional runner/audit and secret-leak tests**

```python
async def test_one_batch_rewraps_only_three_fields_and_checkpoints_atomically(
    rotation_world: RotationWorld,
) -> None:
    first, second = await rotation_world.seed_v1_secrets(2)
    before_generation = await rotation_world.credential_mutation_generation()
    before = {
        row.id: (
            row.ciphertext, row.nonce, row.wrapped_data_key, row.secret_fingerprint,
            row.created_at, row.updated_at, row.rotated_at, row.last_used_at,
        )
        for row in (first, second)
    }
    result = await rotation_world.runner(batch_size=1, max_batches=1).run(
        from_version=1, to_version=2
    )
    assert result.status == "rewrapping"
    assert result.rows_rewrapped == 1
    rows = await rotation_world.secrets()
    migrated = next(row for row in rows if row.key_version == 2)
    assert (migrated.ciphertext, migrated.nonce) == before[migrated.id][:2]
    assert migrated.wrapped_data_key != before[migrated.id][2]
    assert migrated.secret_fingerprint != before[migrated.id][3]
    assert (
        migrated.created_at, migrated.updated_at, migrated.rotated_at, migrated.last_used_at
    ) == before[migrated.id][4:]
    assert rotation_world.rewrap_update_set_columns == [
        "wrapped_data_key", "key_version", "secret_fingerprint"
    ]
    assert await rotation_world.credential_mutation_generation() == before_generation
    state = await rotation_world.rotation()
    assert state.last_secret_id == migrated.id


async def test_resume_verifies_and_completes_with_append_only_audits(
    rotation_world: RotationWorld,
) -> None:
    await rotation_world.seed_v1_secrets(3)
    first = await rotation_world.runner(batch_size=1, max_batches=1).run(
        from_version=1, to_version=2
    )
    assert first.status == "rewrapping"
    final = await rotation_world.runner(batch_size=1).run(from_version=1, to_version=2)
    assert final.status == "completed"
    assert final.rows_rewrapped == 3
    assert final.rows_verified == 3
    assert await rotation_world.secret_versions() == [2, 2, 2]
    assert await rotation_world.audit_actions() == [
        "master_key.rotation_started", "master_key.rotation_completed"
    ]
    for metadata in await rotation_world.audit_metadata():
        assert set(metadata) <= {
            "from_version", "to_version", "rows_total", "rows_rewrapped",
            "rows_verified", "rows_failed", "safe_error_code",
        }


async def test_abort_stops_future_work_but_keeps_mixed_rows(
    rotation_world: RotationWorld,
) -> None:
    await rotation_world.seed_v1_secrets(2)
    await rotation_world.runner(batch_size=1, max_batches=1).run(from_version=1, to_version=2)
    aborted = await rotation_world.runner().abort(from_version=1, to_version=2)
    assert aborted.status == "aborted"
    assert await rotation_world.secret_versions() == [1, 2]
    assert await rotation_world.audit_actions()[-1] == "master_key.rotation_aborted"


async def test_source_seen_at_completion_returns_to_rewrapping_atomically(
    rotation_world: RotationWorld,
) -> None:
    await rotation_world.seed_v1_secrets(1)
    barrier = rotation_world.pause_before_completion_check()
    task = asyncio.create_task(
        rotation_world.runner(batch_size=1).run(from_version=1, to_version=2)
    )
    await barrier.arrived.wait()
    fallback = await rotation_world.insert_v1_secret_below_checkpoint()
    barrier.release.set()
    result = await task
    assert result.status == "completed"
    assert await rotation_world.secret_version(fallback.id) == 2
    transitions = await rotation_world.status_transitions()
    assert ("verifying", "rewrapping") in transitions
    assert transitions[-1] == ("verifying", "completed")


async def test_source_inserted_during_verification_resets_checkpoint(
    rotation_world: RotationWorld,
) -> None:
    await rotation_world.seed_v1_secrets(3)
    barrier = rotation_world.pause_after_first_verification_batch()
    task = asyncio.create_task(
        rotation_world.runner(batch_size=1).run(from_version=1, to_version=2)
    )
    await barrier.arrived.wait()
    fallback = await rotation_world.insert_v1_secret_below_checkpoint()
    barrier.release.set()
    result = await task
    assert result.status == "completed"
    assert await rotation_world.secret_version(fallback.id) == 2
    assert await rotation_world.verification_reset_count() >= 1
    assert await rotation_world.count_source_rows(1) == 0


async def test_retirement_uses_latest_completed_mutation_generation(
    rotation_world: RotationWorld,
) -> None:
    await rotation_world.seed_v1_secrets(2)
    completed = await rotation_world.runner().run(from_version=1, to_version=2)
    assert completed.status == "completed"
    first_generation = await rotation_world.credential_mutation_generation()
    assert (await rotation_world.rotation()).credential_mutation_generation == first_generation
    assert await rotation_world.retirement_ready(1, 2) is True

    fallback = await rotation_world.seed_v1_secrets(1)
    assert await rotation_world.retirement_ready(1, 2) is False
    partial = await rotation_world.runner(max_batches=1).run(from_version=1, to_version=2)
    assert partial.status in {"rewrapping", "verifying"}
    await rotation_world.runner().abort(from_version=1, to_version=2)
    await rotation_world.force_target_wrapper_for_test(fallback.id)
    assert await rotation_world.count_source_rows(1) == 0
    assert await rotation_world.retirement_ready(1, 2) is False

    latest = await rotation_world.runner().run(from_version=1, to_version=2)
    assert latest.status == "completed"
    assert (
        await rotation_world.rotation()
    ).credential_mutation_generation == await rotation_world.credential_mutation_generation()
    assert await rotation_world.retirement_ready(1, 2) is True


@pytest.mark.parametrize("mutation", ["same-id-rotate", "delete", "create"])
async def test_post_verification_credential_mutation_forces_a_new_full_pass(
    rotation_world: RotationWorld,
    mutation: str,
) -> None:
    target = (await rotation_world.seed_v1_secrets(1))[0]
    barrier = rotation_world.pause_before_completion_check()
    task = asyncio.create_task(
        rotation_world.runner(batch_size=1).run(from_version=1, to_version=2)
    )
    await barrier.arrived.wait()
    state_before = await rotation_world.rotation()
    captured = state_before.credential_mutation_generation
    assert captured is not None
    assert captured == await rotation_world.credential_mutation_generation()
    if mutation == "same-id-rotate":
        await rotation_world.rotate_actual_credential(target.id, "changed-credential")
    elif mutation == "delete":
        await rotation_world.delete_secret(target.id)
    else:
        await rotation_world.insert_target_secret("new-credential")
    assert await rotation_world.credential_mutation_generation() > captured
    barrier.release.set()

    result = await task
    assert result.status == "completed"
    assert await rotation_world.verification_reset_count() >= 1
    completed = await rotation_world.rotation()
    assert completed.credential_mutation_generation == (
        await rotation_world.credential_mutation_generation()
    )


async def test_retirement_preview_then_arm_creates_durable_write_fence(
    rotation_world: RotationWorld,
) -> None:
    target = (await rotation_world.seed_v1_secrets(1))[0]
    await rotation_world.runner().run(from_version=1, to_version=2)
    rotation_world.set_database_clock(NOW)
    await rotation_world.set_exact_reporters(active=2, supported=(1, 2), seen_at=NOW)
    assert await rotation_world.retirement_ready(1, 2) is True
    assert (await rotation_world.rotation()).retirement_fence_id is None

    armed = await rotation_world.runner().arm_retirement_fence(
        from_version=1, to_version=2
    )
    state = await rotation_world.rotation()
    assert armed.state == "armed"
    assert state.retirement_fence_id == armed.fence_id
    assert state.retirement_fence_generation == state.credential_mutation_generation
    assert state.retirement_fence_started_at == NOW
    assert state.retirement_fence_deadline == NOW + timedelta(seconds=600)

    before = await rotation_world.secret_snapshot()
    generation = await rotation_world.credential_mutation_generation()
    for mutation in ("same-id-rotate", "delete", "create"):
        with pytest.raises(RotationError, match="retirement_fence_active"):
            await rotation_world.semantic_mutation(mutation, target.id)
    with pytest.raises(RotationError, match="retirement_fence_active"):
        await rotation_world.runner().run(from_version=1, to_version=2)
    with pytest.raises(RotationError, match="retirement_fence_active"):
        await rotation_world.runner().arm_retirement_fence(
            from_version=1, to_version=2
        )
    assert await rotation_world.secret_snapshot() == before
    assert await rotation_world.credential_mutation_generation() == generation

    await rotation_world.wrapper_only_rewrap(target.id)
    assert await rotation_world.credential_mutation_generation() == generation


async def test_retirement_commit_revalidates_retired_reporters_and_generation(
    rotation_world: RotationWorld,
) -> None:
    await rotation_world.seed_v1_secrets(1)
    await rotation_world.runner().run(from_version=1, to_version=2)
    rotation_world.set_database_clock(NOW)
    await rotation_world.set_exact_reporters(active=2, supported=(1, 2), seen_at=NOW)
    armed = await rotation_world.runner().arm_retirement_fence(
        from_version=1, to_version=2
    )

    await rotation_world.set_exact_reporters(active=2, supported=(1, 2), seen_at=NOW)
    with pytest.raises(RotationError, match="replica_gate_closed"):
        await rotation_world.runner().commit_retirement_fence(
            from_version=1, to_version=2, fence_id=armed.fence_id
        )
    await rotation_world.set_exact_reporters(active=2, supported=(2,), seen_at=NOW)
    committed = await rotation_world.runner().commit_retirement_fence(
        from_version=1, to_version=2, fence_id=armed.fence_id
    )
    assert committed.state == "committed"
    assert (await rotation_world.rotation()).retirement_fence_id is None
    await rotation_world.insert_target_secret("allowed-after-commit")


async def test_expired_fence_stays_closed_until_dual_ring_cancel(
    rotation_world: RotationWorld,
) -> None:
    await rotation_world.seed_v1_secrets(1)
    await rotation_world.runner().run(from_version=1, to_version=2)
    rotation_world.set_database_clock(NOW)
    await rotation_world.set_exact_reporters(active=2, supported=(1, 2), seen_at=NOW)
    armed = await rotation_world.runner().arm_retirement_fence(
        from_version=1, to_version=2
    )
    rotation_world.set_database_clock(NOW + timedelta(seconds=601))
    await rotation_world.set_exact_reporters(
        active=2, supported=(2,), seen_at=NOW + timedelta(seconds=601)
    )
    with pytest.raises(RotationError, match="retirement_fence_expired"):
        await rotation_world.runner().commit_retirement_fence(
            from_version=1,
            to_version=2,
            fence_id=armed.fence_id,
        )
    assert (await rotation_world.rotation()).retirement_fence_id == armed.fence_id

    await rotation_world.force_source_wrapper_for_test()
    rotation_world.set_database_clock(NOW + timedelta(seconds=602))
    await rotation_world.set_exact_reporters(
        active=2, supported=(1, 2), seen_at=NOW + timedelta(seconds=602)
    )
    cancelled = await rotation_world.runner().cancel_retirement_fence(
        from_version=1,
        to_version=2,
        fence_id=armed.fence_id,
    )
    assert cancelled.state == "cancelled"
    assert (await rotation_world.rotation()).retirement_fence_id is None
    assert await rotation_world.count_source_rows(1) == 1
```

`pause_after_first_verification_batch()` and `pause_before_completion_check()` use a test subclass overriding protected no-op `_after_verification_batch()`/`_before_completion_check()` methods; production never configures callbacks. `RotationWorld` owns `FakeMutationGenerationSource` and `FakeRotationDatabaseClock`; `set_database_clock()` mutates only that fake, while the production runner constructor has no host `now` callback. Every unit helper that inserts/deletes a secret or updates ciphertext/nonce first checks the fake retirement fence and then advances the generation; its exact wrapper-only helper does neither. Its fake lease serializes completion and retirement proof. Inject commit failure after the exact three-field SQL but before commit and assert wrapper fields/counters/checkpoint all roll back. Inject a process stop immediately after a committed batch and prove a new runner resumes without double counting. Cover equal/missing versions, conflicting active state, gate drift between batches, an unexpected v3 row, corrupt wrap/ciphertext/fingerprint, target rows already written concurrently, source rows inserted below checkpoint/during verification/immediately before completion, same-ID credential rotation, deletion/creation during and after verification, rolled-back generation gaps, batch sizes 0/1001, and bounded `max_batches`. A safe failure increments `rows_failed`, persists only a closed code, leaves a resumable nonterminal status, and never places canary plaintext/key/path/cipher/fingerprint in rotation/audit/log capture. After abort, rerunning the same pair always creates a newer active attempt; an older completed/aborted row and every audit remain immutable. Rerunning a completed pair returns it only when zero source/unexpected rows and its stored mutation generation still equals the current scalar; otherwise it creates a verification/rewrap attempt. Construct a runner with `FakeRotationLease(held=False)` and assert `run`, `abort`, and every retirement-fence action raise `RotationLockError("rotation_advisory_lock_not_held")` before mutation; `RotationWorld.runner()` supplies `FakeRotationLease(held=True)`, its fake generation source, and its fake database clock. No public API path can manufacture a lease, generation source, or production time source.

In `packages/tools/tests/test_gateway.py`, add the wrapper-stability regression against the real gateway helper:

```python
async def test_parked_approval_survives_wrapper_only_rewrap_but_not_credential_drift(
    gateway_world: GatewayWorld,
) -> None:
    connection, secret = await gateway_world.connection_requiring_approval()
    first = await gateway_world.park(connection)
    before = await gateway_world.authorization_digest(connection.id)
    await gateway_world.rewrap_exact_three_fields(secret.id, from_version=1, to_version=2)
    after = await gateway_world.authorization_digest(connection.id)
    assert after == before
    await gateway_world.approve(first.approval_id)
    executed = await gateway_world.resolve(first.approval_id)
    assert executed.status == "executed"
    assert gateway_world.effects == 1

    second = await gateway_world.park(connection)
    await gateway_world.rotate_actual_credentials(connection.id, "changed-token")
    await gateway_world.approve(second.approval_id)
    denied = await gateway_world.resolve(second.approval_id)
    assert (denied.status, denied.decision_code) == (
        "denied", "approval_connection_changed"
    )
    assert gateway_world.effects == 1
```

Replace only the credential part of `_connection_authorization_digest` with `credential_revision = sha256(b"jhin-credential-revision-v1\0" + secret.id.bytes + secret.ciphertext + secret.nonce).hexdigest()`. Keep connection ID/auth type/status/config in the outer digest. Never include wrapper, key version, fingerprint, ciphertext, or nonce in the approval payload; only the final authorization digest is stored. Retain and extend the existing parameterized actual credentials/status/config/auth type/deleted denial test.

- [ ] **Step 3: Write real-PostgreSQL lock/concurrency/resume tests**

```python
@pytest.mark.integration
async def test_real_pg_rewrap_survives_restart_during_active_reads(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    seeded = await postgres_rotation_world.seed_v1_secrets(25)
    stop = asyncio.Event()
    readers = [asyncio.create_task(postgres_rotation_world.read_loop(row.id, stop)) for row in seeded]
    try:
        while True:
            result = await postgres_rotation_world.new_runner(
                batch_size=3, max_batches=2
            ).run(from_version=1, to_version=2)
            if result.status == "completed":
                break
        assert result.rows_rewrapped == 25
        assert result.rows_verified == 25
    finally:
        stop.set()
        await asyncio.gather(*readers)
    assert await postgres_rotation_world.count_by_version() == {2: 25}
    assert await postgres_rotation_world.ciphertext_nonce_snapshot() == {
        row.id: (row.ciphertext, row.nonce) for row in seeded
    }


@pytest.mark.integration
async def test_lock_backend_loss_fences_mutation_then_new_runner_resumes(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    await postgres_rotation_world.seed_v1_secrets(3)
    lease = await postgres_rotation_world.acquire_lease()
    first = await postgres_rotation_world.runner(
        lease=lease, batch_size=1, max_batches=1
    ).run(from_version=1, to_version=2)
    assert first.rows_rewrapped == 1
    checkpoint = await postgres_rotation_world.durable_checkpoint()
    await postgres_rotation_world.terminate_backend(lease.backend_pid)
    with pytest.raises(
        RotationLockError, match="rotation_advisory_lock_lost"
    ):
        await postgres_rotation_world.runner(lease=lease).run(
            from_version=1, to_version=2
        )
    assert await postgres_rotation_world.durable_checkpoint() == checkpoint
    assert await postgres_rotation_world.count_by_version() == {1: 2, 2: 1}

    replacement = await postgres_rotation_world.acquire_lease()
    final = await postgres_rotation_world.runner(lease=replacement).run(
        from_version=1, to_version=2
    )
    assert final.status == "completed"
    assert final.rows_rewrapped == 3


@pytest.mark.integration
async def test_second_runner_cannot_mutate_while_first_backend_holds_lock(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    await postgres_rotation_world.seed_v1_secrets(2)
    first = await postgres_rotation_world.acquire_lease()
    second_connection = await postgres_rotation_world.connect()
    assert await PostgresRotationLease.try_acquire(second_connection) is None
    assert await postgres_rotation_world.rotation_count() == 0
    assert await postgres_rotation_world.rotation_audit_count() == 0
    await first.release()
    second = await PostgresRotationLease.try_acquire(second_connection)
    assert second is not None
    result = await postgres_rotation_world.runner(lease=second).run(
        from_version=1, to_version=2
    )
    assert result.status == "completed"


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["same-id-rotate", "delete", "create"])
async def test_real_pg_post_verify_mutation_cannot_reuse_stale_completion_authority(
    postgres_rotation_world: PostgresRotationWorld,
    mutation: str,
) -> None:
    target = (await postgres_rotation_world.seed_v1_secrets(1))[0]
    barrier = postgres_rotation_world.pause_before_completion_check()
    task = asyncio.create_task(
        postgres_rotation_world.new_runner(batch_size=1).run(
            from_version=1, to_version=2
        )
    )
    await barrier.arrived.wait()
    captured = (
        await postgres_rotation_world.rotation()
    ).credential_mutation_generation
    assert captured is not None
    assert captured == await postgres_rotation_world.credential_mutation_generation()
    await postgres_rotation_world.semantic_mutation_from_independent_connection(
        mutation=mutation,
        target_id=target.id,
    )
    assert await postgres_rotation_world.credential_mutation_generation() > captured
    barrier.release.set()

    completed = await task
    assert completed.status == "completed"
    state = await postgres_rotation_world.rotation()
    assert state.credential_mutation_generation == (
        await postgres_rotation_world.credential_mutation_generation()
    )
    assert await postgres_rotation_world.verification_pass_count() >= 2


@pytest.mark.integration
@pytest.mark.parametrize(
    "boundary",
    [
        "prepare-state-for-update",
        "abort-state-for-update",
        "activated-reporter-query",
        "rewrap-secret-for-update",
        "verification-secret-for-update",
        "completion-share-table",
        "retirement-preview-reporter-query",
        "retirement-preview-share-table",
        "retirement-arm-reporter-query",
        "retirement-arm-state-for-update",
        "retirement-arm-share-table",
        "retirement-arm-heartbeat-share-table",
        "retirement-commit-reporter-query",
        "retirement-commit-state-for-update",
        "retirement-commit-share-table",
        "retirement-commit-heartbeat-share-table",
        "retirement-cancel-reporter-query",
        "retirement-cancel-state-for-update",
        "retirement-cancel-share-table",
        "retirement-cancel-heartbeat-share-table",
    ],
)
async def test_every_lock_wait_times_out_closed_without_state_change(
    postgres_rotation_world: PostgresRotationWorld,
    boundary: str,
) -> None:
    invocation = await postgres_rotation_world.prepare_blockable_boundary(boundary)
    before = await postgres_rotation_world.durable_snapshot()
    async with postgres_rotation_world.hold_conflicting_lock(boundary):
        started = time.monotonic()
        with pytest.raises(RotationError) as excinfo:
            await invocation()
        elapsed = time.monotonic() - started
    assert excinfo.value.code is RotationSafeErrorCode.ROW_LOCK_TIMEOUT
    assert 4.0 <= elapsed < 10.0
    assert await postgres_rotation_world.durable_snapshot() == before
    assert postgres_rotation_world.captured_error_text() == ""


@pytest.mark.integration
async def test_statement_timeout_is_exact_and_rolls_back(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    lease = await postgres_rotation_world.acquire_lease()
    before = await postgres_rotation_world.durable_snapshot()
    started = time.monotonic()
    with pytest.raises(RotationError) as excinfo:
        await postgres_rotation_world.run_timeout_probe(
            lease,
            sql="SELECT pg_sleep(60)",
        )
    elapsed = time.monotonic() - started
    assert excinfo.value.code is RotationSafeErrorCode.STATEMENT_TIMEOUT
    assert 29.0 <= elapsed < ROTATION_CLIENT_TIMEOUT_SECONDS
    assert await postgres_rotation_world.durable_snapshot() == before
    async with lease.transaction() as session:
        assert await postgres_rotation_world.show_setting(session, "lock_timeout") == "5s"
        assert await postgres_rotation_world.show_setting(session, "statement_timeout") == "30s"
    await lease.release()


@pytest.mark.integration
async def test_advisory_acquisition_installs_both_exact_timeouts_before_authority(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    connection = await postgres_rotation_world.connect()
    with postgres_rotation_world.capture_connection_sql(connection) as sql:
        started = time.monotonic()
        lease = await PostgresRotationLease.try_acquire(connection)
        elapsed = time.monotonic() - started
    assert lease is not None
    assert elapsed < ROTATION_CLIENT_TIMEOUT_SECONDS
    assert await postgres_rotation_world.show_connection_setting(
        connection, "lock_timeout"
    ) == "5s"
    assert await postgres_rotation_world.show_connection_setting(
        connection, "statement_timeout"
    ) == "30s"
    assert sql.authority_prefix == [
        "SET lock_timeout = '5000ms'",
        "SET statement_timeout = '30000ms'",
        "SHOW lock_timeout",
        "SHOW statement_timeout",
        "SELECT pg_backend_pid(), pg_try_advisory_lock(:lock_id)",
    ]
    assert sql.first_rotation_authority_index == 4
    await lease.release()
    await connection.close()


@pytest.mark.integration
async def test_advisory_acquire_is_nonblocking_and_release_is_client_bounded(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    first = await postgres_rotation_world.acquire_lease()
    second_connection = await postgres_rotation_world.connect()
    started = time.monotonic()
    assert await PostgresRotationLease.try_acquire(second_connection) is None
    assert time.monotonic() - started < 1.0
    assert await postgres_rotation_world.rotation_count() == 0
    await asyncio.wait_for(first.release(), timeout=ROTATION_CLIENT_TIMEOUT_SECONDS)
    await second_connection.close()


@pytest.mark.integration
@pytest.mark.parametrize("action", ["arm", "cancel"])
async def test_arm_and_cancel_recheck_reporters_after_final_state_and_secret_locks(
    postgres_rotation_world: PostgresRotationWorld,
    action: Literal["arm", "cancel"],
) -> None:
    await postgres_rotation_world.seed_v1_secrets(1)
    await postgres_rotation_world.complete_rotation(1, 2)
    checked_at = await postgres_rotation_world.db_now()
    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(1, 2), seen_at=checked_at
    )
    fence_id = None
    if action == "cancel":
        armed = await postgres_rotation_world.new_runner().arm_retirement_fence(
            from_version=1, to_version=2
        )
        fence_id = armed.fence_id
        await postgres_rotation_world.set_exact_reporters(
            active=2, supported=(1, 2), seen_at=await postgres_rotation_world.db_now()
        )
    barrier = postgres_rotation_world.pause_before_final_reporter_recheck(action)
    before = await postgres_rotation_world.durable_snapshot()
    if action == "arm":
        task = asyncio.create_task(
            postgres_rotation_world.new_runner().arm_retirement_fence(
                from_version=1, to_version=2
            )
        )
    else:
        assert fence_id is not None
        task = asyncio.create_task(
            postgres_rotation_world.new_runner().cancel_retirement_fence(
                from_version=1, to_version=2, fence_id=fence_id
            )
        )
    await barrier.arrived.wait()
    await postgres_rotation_world.insert_future_reporter_for_test()
    barrier.release.set()
    with pytest.raises(RotationError, match="replica_gate_closed"):
        await task
    after = await postgres_rotation_world.durable_snapshot()
    assert after.rotation == before.rotation
    assert after.secret_rows == before.secret_rows
    assert after.rotation_audits == before.rotation_audits


@pytest.mark.integration
async def test_retirement_uses_database_clock_despite_host_clock_skew(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    await postgres_rotation_world.seed_v1_secrets(1)
    await postgres_rotation_world.complete_rotation(1, 2)
    db_before = await postgres_rotation_world.db_now()
    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(1, 2), seen_at=db_before
    )
    with postgres_rotation_world.poison_rotation_host_clock(
        db_before + timedelta(days=3650)
    ):
        armed = await postgres_rotation_world.new_runner().arm_retirement_fence(
            from_version=1, to_version=2
        )
    db_after = await postgres_rotation_world.db_now()
    state = await postgres_rotation_world.rotation()
    assert db_before <= state.retirement_fence_started_at <= db_after
    assert state.retirement_fence_deadline == (
        state.retirement_fence_started_at + timedelta(seconds=600)
    )
    assert postgres_rotation_world.production_clock_sql == "SELECT clock_timestamp()"

    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(2,), seen_at=await postgres_rotation_world.db_now()
    )
    await postgres_rotation_world.force_fence_deadline_before_database_now(armed.fence_id)
    before = await postgres_rotation_world.durable_snapshot()
    with postgres_rotation_world.poison_rotation_host_clock(
        db_before - timedelta(days=3650)
    ):
        with pytest.raises(RotationError, match="retirement_fence_expired"):
            await postgres_rotation_world.new_runner().commit_retirement_fence(
                from_version=1, to_version=2, fence_id=armed.fence_id
            )
    assert await postgres_rotation_world.durable_snapshot() == before


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["same-id-rotate", "delete", "create"])
async def test_arm_boundary_blocks_racing_mutation_and_second_runner(
    postgres_rotation_world: PostgresRotationWorld,
    mutation: str,
) -> None:
    target = (await postgres_rotation_world.seed_v1_secrets(1))[0]
    await postgres_rotation_world.complete_rotation(1, 2)
    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(1, 2), seen_at=await postgres_rotation_world.db_now()
    )
    barrier = postgres_rotation_world.pause_after_retirement_share_lock()
    arm_task = asyncio.create_task(
        postgres_rotation_world.new_runner().arm_retirement_fence(
            from_version=1, to_version=2
        )
    )
    await barrier.arrived.wait()
    before = await postgres_rotation_world.durable_snapshot()
    mutation_task = asyncio.create_task(
        postgres_rotation_world.semantic_mutation_from_independent_connection(
            mutation=mutation, target_id=target.id
        )
    )
    runner_task = asyncio.create_task(
        postgres_rotation_world.try_second_rotation(from_version=1, to_version=2)
    )
    await postgres_rotation_world.wait_until_blocked_on_secret_table(mutation_task)
    barrier.release.set()

    armed = await arm_task
    mutation_result, runner_result = await asyncio.gather(
        mutation_task, runner_task, return_exceptions=True
    )
    assert armed.state == "armed"
    assert isinstance(mutation_result, RotationError)
    assert mutation_result.code is RotationSafeErrorCode.RETIREMENT_FENCE_ACTIVE
    assert runner_result == RotationSafeErrorCode.RETIREMENT_FENCE_ACTIVE
    after = await postgres_rotation_world.durable_snapshot()
    assert after.secret_rows == before.secret_rows
    assert after.credential_mutation_generation == before.credential_mutation_generation
    assert after.rotation.retirement_fence_id == armed.fence_id


@pytest.mark.integration
async def test_commit_rechecks_exact_boundary_and_closes_on_drift(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    await postgres_rotation_world.seed_v1_secrets(1)
    await postgres_rotation_world.complete_rotation(1, 2)
    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(1, 2), seen_at=await postgres_rotation_world.db_now()
    )
    armed = await postgres_rotation_world.new_runner().arm_retirement_fence(
        from_version=1, to_version=2
    )
    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(2,), seen_at=await postgres_rotation_world.db_now()
    )
    await postgres_rotation_world.insert_future_reporter_for_test()
    before = await postgres_rotation_world.durable_snapshot()
    with pytest.raises(RotationError, match="replica_gate_closed"):
        await postgres_rotation_world.new_runner().commit_retirement_fence(
            from_version=1, to_version=2, fence_id=armed.fence_id
        )
    assert await postgres_rotation_world.durable_snapshot() == before
    await postgres_rotation_world.delete_future_reporter_for_test()
    committed = await postgres_rotation_world.new_runner().commit_retirement_fence(
        from_version=1, to_version=2, fence_id=armed.fence_id
    )
    assert committed.state == "committed"


@pytest.mark.integration
async def test_wrapper_source_drift_blocks_commit_but_dual_ring_can_cancel(
    postgres_rotation_world: PostgresRotationWorld,
) -> None:
    target = (await postgres_rotation_world.seed_v1_secrets(1))[0]
    await postgres_rotation_world.complete_rotation(1, 2)
    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(1, 2), seen_at=await postgres_rotation_world.db_now()
    )
    armed = await postgres_rotation_world.new_runner().arm_retirement_fence(
        from_version=1, to_version=2
    )
    generation = await postgres_rotation_world.credential_mutation_generation()
    await postgres_rotation_world.force_wrapper_only_source_row(target.id)
    assert await postgres_rotation_world.credential_mutation_generation() == generation
    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(2,), seen_at=await postgres_rotation_world.db_now()
    )
    with pytest.raises(RotationError, match="source_rows_remain"):
        await postgres_rotation_world.new_runner().commit_retirement_fence(
            from_version=1, to_version=2, fence_id=armed.fence_id
        )
    assert (await postgres_rotation_world.rotation()).retirement_fence_id == armed.fence_id

    await postgres_rotation_world.set_exact_reporters(
        active=2, supported=(1, 2), seen_at=await postgres_rotation_world.db_now()
    )
    cancelled = await postgres_rotation_world.new_runner().cancel_retirement_fence(
        from_version=1, to_version=2, fence_id=armed.fence_id
    )
    assert cancelled.state == "cancelled"
    assert await postgres_rotation_world.count_by_version() == {1: 1}
```

`prepare_blockable_boundary()` creates the exact valid pre-state for its named path and returns the production runner/gate call; `hold_conflicting_lock()` uses a second real connection, a row lock for `*-for-update`, `LOCK TABLE secret IN ACCESS EXCLUSIVE MODE` for secret `*-share-table`, or `LOCK TABLE service_instance_heartbeat IN ACCESS EXCLUSIVE MODE` for reporter queries and heartbeat SHARE-table boundaries, and commits only after the call returns. `durable_snapshot()` reads rotation rows, secret fields/generation, heartbeat rows, and audit rows from a third connection. Thus every production wait named in the parameterization proves the exact five-second server lock timeout, 35-second client ceiling, closed code, rollback, and unchanged durable state. `run_timeout_probe()` enters the production lease transaction—so it installs both settings before the probe—and translates `57014` without DBAPI text. `capture_connection_sql()` is a test-only SQLAlchemy event recorder over the exact acquired connection; it normalizes bound parameters only for the five allowlisted prefix statements and proves that acquisition installs and verifies both settings within the client deadline before its first PID/advisory authority query. Test acquisition separately because `pg_try_advisory_lock` is deliberately nonblocking; `release()` is still under the exact client ceiling.

`terminate_backend` uses an independent admin connection and `SELECT pg_terminate_backend(:pid)` for the exact recorded PID. The lost lease's next mutating transaction must fail its same-backend `pg_locks` fence before SQL update/insert; a DBAPI disconnect is translated from no raw exception text. Run two `try_acquire` calls concurrently and prove exactly one lease, one active row, and one started audit. Use separate sessions to insert a source row during verification and to perform a valid same-ID credential rotation, delete, and create after the last verification row but before completion; source returns state to rewrapping, while every scalar generation change restarts the whole verification pass. The retirement arm barrier is a protected no-op after the secret SHARE lock and immediately before the final heartbeat SHARE lock/database-time/reporter recheck, overridden only by the real-PostgreSQL fixture; it proves a writer already queued at the handoff is rejected by the newly armed `BEFORE STATEMENT` trigger and never advances the sequence. The action-specific `_before_final_reporter_recheck(action)` no-op is used only by the reporter-drift fixture: the independent future reporter commits before the heartbeat SHARE lock, the final gate sees it, and arm/cancel leave the durable fence state unchanged. `poison_rotation_host_clock()` replaces every module-local wall-clock callable with a raising/skewed canary while leaving PostgreSQL untouched; successful arm and database-expired commit prove `PostgresRotationDatabaseClock` is authoritative. Inspect `pg_backend_pid()` from each effecting transaction and assert it always equals `lease.backend_pid`.

- [ ] **Step 4: Run RED**

```bash
uv run pytest packages/secrets/tests/test_rotation.py packages/tools/tests/test_gateway.py -q
JHIN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres uv run pytest -m integration tests/integration/test_phase10_master_key_rotation_postgres.py -q
```

Expected: FAIL because the lease-bound gate/runner/state machine, acquisition-time exact dual timeouts, closed reporter window, database-only time source, final arm/cancel reporter rechecks, injected mutation-generation source, bounded batches, generation-safe completion/retirement, audit integration, and wrapper-stable gateway digest do not exist.

- [ ] **Step 5: Implement the state machine and transaction boundaries**

Query heartbeat distributions with grouped SQL over only the three closed services and exact `last_seen_at >= checked_at - 30 seconds AND last_seen_at <= checked_at`; separately count any future key-service row and close the gate if nonzero. Cap diagnostic distributions/counts before DTO construction. `PostgresRotationLease.try_acquire(connection)` enters `asyncio.timeout(35.0)`, executes session-level exact `SET lock_timeout = '5000ms'` and `SET statement_timeout = '30000ms'`, executes `SHOW lock_timeout`/`SHOW statement_timeout`, requires exact normalized results `5s`/`30s`, and only then issues one `SELECT pg_backend_pid(), pg_try_advisory_lock(:lock_id)` as its first authority query. A setting error, mismatch, server cancellation, or client expiry closes and rolls back before any rotation-state/generation/reporter SQL. On grant it commits the acquisition transaction, records that PID, and keeps that exact `AsyncConnection` open. Its `transaction()` creates `AsyncSession(bind=connection, expire_on_commit=False)`, begins, executes exact `SET LOCAL lock_timeout = '5000ms'` and `SET LOCAL statement_timeout = '30000ms'`, and then makes its first authority SQL `assert_held`, which requires the recorded PID and exact granted `pg_locks` tuple (`classid=1245004473`, `objid=1772817225`, `objsubid=1`). The whole context is within `asyncio.timeout(35.0)`. Release installs/verifies the same two session settings within a fresh client bound before its bounded unlock query. It converts `55P03` to `row_lock_timeout`, `57014` or client expiry to `rotation_statement_timeout`, and disconnect/termination to `rotation_advisory_lock_lost`, always rolls back, and discards raw exception text. Every gate, prepare, state `FOR UPDATE`, batch, audit, abort, completion table lock, retirement preview/arm/commit/cancel secret/heartbeat table lock, generation/reporter/time read, and fence mutation uses this adapter; no runner session factory or pooled authority connection exists.

At prepare, on the lease connection, lock/read any armed fence first and refuse with `retirement_fence_active`; then recheck activated gate and inspect the latest matching attempt/current sequence generation plus source/unexpected counts. Return an existing completion only when that latest row is completed, its stored nonnull generation equals current, and both counts are zero; otherwise insert a newer `prepared` row plus started audit atomically. The first effecting transaction locks that row and changes `prepared -> rewrapping`; no separate commit can advertise `rewrapping` before the first batch is owned. Before every effecting batch, run a bounded aggregate query and refuse if any row version is outside `{from_version, to_version}`. For each rewrap transaction: fence lease; lock state; refuse an armed retirement fence; recheck gate; select explicit payload/timestamp columns for `Secret.id > checkpoint AND key_version = from` ordered by ID with `FOR UPDATE LIMIT batch_size`; call `crypto.rewrap`; call Task 2's exact textual `rewrap_secret_row`; require one row updated; update counter/checkpoint; commit. Never load and assign an ORM `Secret`, so `updated_at`/`rotated_at` remain byte-for-byte unchanged. The exact wrapper-only SQL does not mention `ciphertext` or `nonce`, so the database trigger does not advance and is permitted while armed only for recovery verification—not ordinary new rotation work.

When no source row above the checkpoint exists, globally count invalid/source rows. If a source row appeared at or below the checkpoint, reset `last_secret_id=None` and continue rewrapping. Once zero source/invalid rows remain, reset checkpoint to `None`, set status `verifying`, set `rows_total` to current target count, reset `rows_verified`, and read/persist the current scalar through `generation_source.current(session)` in that fenced transaction. At the start/end of every verification batch, recheck for source rows; finding one changes `verifying -> rewrapping` and clears `last_secret_id`, `rows_verified`, and `credential_mutation_generation`, committing no completion. Verification selects ordered target rows `FOR UPDATE`, calls `crypto.verify`, and commits checkpoint/count. A current-generation mismatch at any verification boundary keeps status `verifying`, resets checkpoint/count, captures the new current generation, and starts a full pass.

At scan end, call `_before_completion_check()` (a no-op protected method overridden only by tests), then open a fresh bounded/fenced transaction, lock state, execute PostgreSQL `LOCK TABLE secret IN SHARE MODE`, and require zero source/unexpected rows plus `generation_source.current(session) == state.credential_mutation_generation`. The production `PostgresRotationLease` path executes that table lock and reads the sequence on the same backend; `FakeRotationLease` supplies an explicitly serialized fake critical section with `FakeMutationGenerationSource` for SQLite/unit tests, while the real-PostgreSQL tests remain the concurrency authority. Source changes status to rewrapping and clears generation; scalar drift resets the full verification pass and captures current. Only a stable generation marks completed and appends the completed audit. `RETIREMENT_READY` independently repeats the latest-attempt/status/zero-source/current-generation equality checks under that bounded SHARE lock, so it is a preview and an older completion cannot authorize a later aborted attempt or same-ID credential mutation. Abort fences/locks the active matching row, first refuses an armed retirement fence, marks it terminal, and appends aborted audit atomically.

Implement `arm_retirement_fence` as a separate fresh lease transaction: select the latest matching attempt by `(started_at DESC, id DESC)` `FOR UPDATE`; lock/check no existing fence; take bounded `LOCK TABLE secret IN SHARE MODE`; re-read current generation and zero source/unexpected counts; require the latest row is completed with the same stored generation; invoke test-only `_after_retirement_share_lock()` and `_before_final_reporter_recheck("arm")`; take bounded `LOCK TABLE service_instance_heartbeat IN SHARE MODE`; obtain one aware `checked_at` from `PostgresRotationDatabaseClock`; recheck the row/generation/count proof and exact activated reporters against that value; then immediately store a cryptographically random UUID, exact generation, that same database start, and `start + interval '600 seconds'` deadline together. `commit_retirement_fence` requires the caller's UUID, then in one lease transaction locks the completed row, takes both SHARE locks, obtains database `checked_at`, and repeats latest-row/current-generation/fence-generation/zero-row/exact fresh-retired-reporter/deadline proof immediately before clearing all four fence columns. `cancel_retirement_fence` requires the caller's UUID, then locks the same row/table pair, invokes `_before_final_reporter_recheck("cancel")` before taking the heartbeat SHARE lock, obtains database `checked_at`, and immediately rechecks the fence/latest attempt/current generation, exact activated dual-ring reporters, and zero versions outside `{from,to}` before clearing. No action uses `utc_now`, `datetime.now`, `time.time`, a caller timestamp, or application-host clock for authority. A wrong/missing fence, later attempt, reporter drift, semantic-generation drift, clock skew, or unexpected version changes nothing and returns only its closed code. An expired fence cannot commit but remains cancellable through this dual-ring recovery path. While armed, the migration's statement trigger is the semantic-write fence; there is deliberately no open database transaction while the operator installs/restarts key files. A new runner/abort/second arm is rejected until commit or safe dual-ring cancel.

Keep audit insertion in one private `_append_rotation_audit(session, action, state)` helper that constructs the existing `AuditEvent` worker-style append-only row; `jhin_secrets` must not import the higher-layer `jhin_api` package or create a second audit table/service. The helper has no update/delete function, accepts only the three closed actions, and builds metadata from explicit integer/count/code fields rather than `vars(state)` or an ORM serializer. In the gateway, replace wrapper-dependent credential fields with the master-key-stable semantic credential revision defined in Step 2; retain all other live authorization/revalidation locks.

- [ ] **Step 6: Run GREEN and commit the engine**

```bash
uv run pytest packages/secrets/tests/test_rotation.py packages/tools/tests/test_gateway.py -q
uv run pytest packages/tools/tests/test_gateway_concurrency.py services/agent_worker/tests/test_approval_activity.py apps/api/tests/test_approvals_unit.py apps/api/tests/test_connections_unit.py packages/secrets/tests/test_store.py -q
JHIN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres uv run pytest -m integration tests/integration/test_phase10_master_key_rotation_postgres.py -q
uv run ruff check packages/secrets packages/tools/src/jhin_tools/gateway.py packages/tools/tests/test_gateway.py tests/integration/test_phase10_master_key_rotation_postgres.py
uv run mypy packages/secrets/src packages/tools/src/jhin_tools/gateway.py tests/integration/test_phase10_master_key_rotation_postgres.py
git add packages/secrets/src/jhin_secrets/rotation.py packages/secrets/src/jhin_secrets/__init__.py packages/secrets/tests/test_rotation.py packages/tools/src/jhin_tools/gateway.py packages/tools/tests/test_gateway.py tests/integration/test_phase10_master_key_rotation_postgres.py
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "feat: rewrap master keys in resumable batches"
```

Expected cached names: exactly the six paths in `git add`.

### Task 6: Expose a Host-Only Advisory-Locked Rotation CLI

**Files:**
- Create: `packages/secrets/src/jhin_secrets/rotation_cli.py`
- Create: `packages/secrets/tests/test_rotation_cli.py`
- Modify: `packages/secrets/pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Task 5 runner/gates and environment-only `DATABASE_URL`/`MASTER_KEY_FILE`.
- Produces: `jhin-master-key-rotate` with the exact default run form, safe check/abort modes, fixed session advisory lock, bounded exits, and no HTTP route.

- [ ] **Step 1: Write parse, output, lock, and leakage tests**

```python
def test_exact_supported_command_shapes() -> None:
    assert parse_args(["--from", "1", "--to", "2"]) == RotationOptions(
        from_version=1,
        to_version=2,
        batch_size=100,
        max_batches=None,
        check_stage=None,
        abort=False,
        retirement_action=None,
        retirement_fence_id=None,
    )
    assert parse_args([
        "--from", "1", "--to", "2", "--batch-size", "25", "--max-batches", "8"
    ]).max_batches == 8
    assert parse_args([
        "--from", "1", "--to", "2", "--check-stage", "retirement-ready"
    ]).check_stage is RotationStage.RETIREMENT_READY
    assert parse_args(["--from", "1", "--to", "2", "--abort"]).abort is True
    armed = parse_args([
        "--from", "1", "--to", "2", "--retirement-action", "arm"
    ])
    assert (armed.retirement_action, armed.retirement_fence_id) == ("arm", None)
    fence_id = UUID("00000000-0000-4000-8000-000000000012")
    for action in ("commit", "cancel"):
        parsed = parse_args([
            "--from", "1", "--to", "2", "--retirement-action", action,
            "--fence-id", str(fence_id),
        ])
        assert (parsed.retirement_action, parsed.retirement_fence_id) == (
            action, fence_id
        )


@pytest.mark.parametrize(
    "argv",
    [
        [], ["--from", "1"], ["--from", "1", "--to", "1"],
        ["--from", "0", "--to", "2"],
        ["--from", "1", "--to", "2", "--abort", "--check-stage", "activated"],
        ["--from", "1", "--to", "2", "--abort", "--retirement-action", "arm"],
        ["--from", "1", "--to", "2", "--check-stage", "retired", "--retirement-action", "commit"],
        ["--from", "1", "--to", "2", "--retirement-action", "commit"],
        ["--from", "1", "--to", "2", "--retirement-action", "cancel"],
        ["--from", "1", "--to", "2", "--retirement-action", "arm", "--fence-id", "00000000-0000-4000-8000-000000000012"],
        ["--from", "1", "--to", "2", "--fence-id", "not-a-uuid"],
        ["--from", "1", "--to", "2", "--batch-size", "1001"],
        ["--from", "1", "--to", "2", "--database-url", "forbidden"],
        ["--from", "1", "--to", "2", "--key-file", "forbidden"],
    ],
)
def test_invalid_or_secret_bearing_arguments_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_args(argv)
    assert excinfo.value.code == 2


async def test_cli_prints_one_bounded_safe_object(
    cli_world: CliWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await async_main(
        parse_args(["--from", "1", "--to", "2", "--max-batches", "1"]),
        cli_world.environ,
    )
    assert code == 75
    output = capsys.readouterr()
    body = json.loads(output.out)
    assert set(body) == {
        "from_version", "to_version", "status", "rows_total", "rows_rewrapped",
        "rows_verified", "rows_failed", "safe_error_code",
    }
    assert output.err == ""
    for canary in cli_world.key_plaintext_path_dsn_fingerprint_canaries:
        assert canary not in output.out + output.err


async def test_retirement_actions_use_fresh_lease_and_fixed_output(
    cli_world: CliWorld,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await cli_world.seed_completed_rotation_and_activated_reporters()
    arm_code = await async_main(
        parse_args([
            "--from", "1", "--to", "2", "--retirement-action", "arm"
        ]),
        cli_world.environ,
    )
    arm_body = json.loads(capsys.readouterr().out)
    assert arm_code == 0
    assert set(arm_body) == {
        "from_version", "to_version", "fence_id", "state", "safe_error_code"
    }
    assert arm_body["state"] == "armed"

    await cli_world.set_retired_reporters()
    commit_code = await async_main(
        parse_args([
            "--from", "1", "--to", "2", "--retirement-action", "commit",
            "--fence-id", arm_body["fence_id"],
        ]),
        cli_world.environ,
    )
    commit_body = json.loads(capsys.readouterr().out)
    assert (commit_code, commit_body["state"]) == (0, "committed")
    assert cli_world.advisory_lease_acquisitions == 2


def test_rotation_entrypoint_closes_argparse_path_dsn_and_os_errors(
    tmp_path: Path,
) -> None:
    path_canary = tmp_path / "KEY_PATH_CANARY_DO_NOT_ECHO"
    dsn_canary = "postgresql+asyncpg://user:DSN_CANARY_DO_NOT_ECHO@127.0.0.1:1/db"
    cases = [
        (
            ["--from", "1", "--to", "2", "--database-url", dsn_canary],
            {"DATABASE_URL": dsn_canary, "MASTER_KEY_FILE": str(path_canary)},
            2,
            "master_key_invalid_arguments\n",
        ),
        (
            ["--from", "1", "--to", "2"],
            {"DATABASE_URL": dsn_canary, "MASTER_KEY_FILE": str(path_canary)},
            2,
            "master_key_file_unreadable\n",
        ),
    ]
    for argv, extra_env, code, stderr in cases:
        result = subprocess.run(
            [sys.executable, "-m", "jhin_secrets.rotation_cli", *argv],
            env={**os.environ, "APP_ENV": "production", **extra_env},
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode, result.stdout, result.stderr) == (code, "", stderr)
        assert "KEY_PATH_CANARY_DO_NOT_ECHO" not in result.stderr
        assert "DSN_CANARY_DO_NOT_ECHO" not in result.stderr
```

Use real PostgreSQL for the lock test:

```python
@pytest.mark.integration
async def test_busy_advisory_lock_refuses_without_state_change(pg_cli_world: PgCliWorld) -> None:
    async with pg_cli_world.hold_advisory_lock(MASTER_KEY_ROTATION_ADVISORY_LOCK):
        code = await async_main(
            parse_args(["--from", "1", "--to", "2"]), pg_cli_world.environ
        )
    assert code == 4
    assert await pg_cli_world.rotation_count() == 0
    assert await pg_cli_world.audit_count() == 0
```

Also run subprocess cases for a valid private key file plus unreachable DSN (`rotation_database_unavailable`, exit 5), permission/OSError (`master_key_file_unreadable`, exit 2), missing/redacted `DATABASE_URL`, and production inline `MASTER_KEY` rejection. Each asserts stdout empty, one exact stderr line, and absence of argument/path/DSN/key/fence canaries. Test missing/wrong local ring tuples for every stage and retirement action, wrong/missing/stale fence UUID, gate-closed exit 3, safe runner failure/lock or statement timeout exit 5, lock loss/busy exit 4, completion 0, abort idempotency, all four `--check-stage` results, arm/commit/cancel, and interruption/cancellation releasing the session advisory lock in `finally`. No CLI mode changes keyring files or exposes an API/router function.

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/secrets/tests/test_rotation_cli.py -q
JHIN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres uv run pytest -m integration packages/secrets/tests/test_rotation_cli.py -q
```

Expected: FAIL because the parser/entry point/advisory-lock context and safe result renderer do not exist.

- [ ] **Step 3: Implement lock lifetime and closed output**

Read both required values from the supplied environment, but pass only the database URL to `create_engine` and only the key path to `load_master_key(..., app_env="production")`; neither may enter an error. Acquire one dedicated engine connection and call `PostgresRotationLease.try_acquire(connection)`. Pass that lease, `PostgresMutationGenerationSource()`, and `PostgresRotationDatabaseClock()` to `RotationRunner(crypto, lease, generation_source, database_clock, ...)`; because the runner has no session factory or host-clock callback, all status/audit/secret/generation/freshness/deadline transactions use the lock-owning backend and PostgreSQL time. Keep the lease/connection open only across the selected default/check/abort/arm/commit/cancel call. In `finally`, call `lease.release()` under the 35-second client bound and then close/dispose; release verifies `pg_advisory_unlock` when the backend remains alive, while a terminated backend closes without printing its DBAPI error. Each host CLI invocation therefore gets a fresh lease; there is no database lock held between `arm`, runtime-file work, and `commit|cancel`.

Before querying replicas, require the CLI's local ring to equal the stage tuple exactly: distributed `(from,(from,to))`, activated/retirement-ready/retirement `arm|cancel` `(to,(from,to))`, and retired/retirement `commit` `(to,(to,))`. `--check-stage` then runs `check_replica_gate`; retirement-ready is preview only and takes the Task 5 bounded SHARE table lock. `arm` calls the exact Task 5 durable handoff and emits its UUID; `commit|cancel` parse a required canonical UUID and pass it to the runner. After old-key removal, retired/commit must not call `crypto.key_for(from_version)`. Default/abort require both keys, and default additionally requires the activated local tuple. Build parsing with Task 1's `ClosedArgumentParser`; make the mutually exclusive mode group explicit, reject batch/max-batches outside default run, require `--fence-id` exactly for commit/cancel, and forbid it for every other mode. Render only fixed result/gate/fence keys and integer/count/UUID-enum values; never use `str(exc)` except closed enum `.value`. The module-level executable boundary catches parser, key-file, expected OS, SQLAlchemy, DBAPI, lock/statement timeout, lock-loss, retirement-fence, and cancellation cleanup errors into one documented code/exit.

Add:

```toml
[project.scripts]
jhin-master-keyring = "jhin_secrets.keyring_cli:main"
jhin-master-key-rotate = "jhin_secrets.rotation_cli:main"
```

- [ ] **Step 4: Run GREEN, inspect help, and commit**

```bash
uv lock
uv run pytest packages/secrets/tests/test_rotation_cli.py -q
JHIN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres uv run pytest -m integration packages/secrets/tests/test_rotation_cli.py -q
uv run jhin-master-key-rotate --help
uv run ruff check packages/secrets
uv run mypy packages/secrets/src
git add packages/secrets/src/jhin_secrets/rotation_cli.py packages/secrets/tests/test_rotation_cli.py packages/secrets/pyproject.toml uv.lock
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "feat: add host master key rotation command"
```

Expected cached names: exactly the four paths in `git add`.

### Task 7: Project Rotation State Safely in Protected Operations

**Files:**
- Modify: `apps/api/src/jhin_api/health/schemas.py`
- Modify: `apps/api/src/jhin_api/health/service.py`
- Modify: `apps/api/tests/conftest.py`
- Modify: `apps/api/tests/test_operations_health.py`
- Modify: `apps/web/lib/types.ts`
- Modify: `apps/web/app/(app)/operations/page.tsx`
- Modify: `apps/web/tests/operations-page.test.tsx`

**Interfaces:**
- Consumes: protected-health `KeyHealthSummary`, workspace-admin route, Task 3 state, and Task 5 audit contract.
- Produces: fail-closed `MasterKeyRotationSummary | None` nested at `OperationsHealthSnapshot.keyring.rotation`, with only workspace-scoped counts; an exact TypeScript mirror and explicit UI fields. Anonymous endpoints and permissions are unchanged.

- [ ] **Step 1: Write API projection/audit opacity tests**

Add this exact schema:

```python
class MasterKeyRotationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_version: BoundedKeyVersion
    to_version: BoundedKeyVersion
    status: Literal["prepared", "rewrapping", "verifying", "completed", "aborted"]
    workspace_rows_total: BoundedCount
    workspace_rows_from_version: BoundedCount
    workspace_rows_to_version: BoundedCount
    started_at: AwareDatetime
    completed_at: AwareDatetime | None
    safe_error_code: Literal[
        "replica_gate_closed", "conflicting_rotation", "unexpected_secret_version",
        "secret_decryption_failed", "secret_verification_failed", "source_rows_remain",
        "row_lock_timeout", "rotation_statement_timeout",
        "rotation_advisory_lock_lost", "retirement_fence_active",
        "retirement_fence_missing", "retirement_fence_expired",
    ] | None

class KeyHealthSummary(BaseModel):
    # Preserve every existing field exactly, then append:
    rotation: MasterKeyRotationSummary | None = None
```

```python
def recursive_values(value: object) -> list[object]:
    if isinstance(value, dict):
        return [child for item in value.values() for child in recursive_values(item)]
    if isinstance(value, (list, tuple)):
        return [child for item in value for child in recursive_values(item)]
    return [value]


async def test_rotation_projection_is_bounded_and_omits_authority_fields(
    operations_world: OperationsHealthWorld,
) -> None:
    await operations_world.seed_rotation(
        from_version=1, to_version=2, status="rewrapping",
        rows_total=999, rows_rewrapped=555, rows_verified=444, rows_failed=7,
    )
    await operations_world.seed_workspace_secret(version=1)
    await operations_world.seed_workspace_secret(version=2)
    foreign = await operations_world.create_foreign_workspace()
    for _ in range(25):
        await operations_world.seed_workspace_secret(version=2, workspace_id=foreign.id)
    snapshot = await operations_world.key_snapshot(
        active=2, supported=(1, 2), secret_versions=[]
    )
    assert snapshot.keyring.rotation == MasterKeyRotationSummary(
        from_version=1, to_version=2, status="rewrapping",
        workspace_rows_total=2,
        workspace_rows_from_version=1,
        workspace_rows_to_version=1,
        started_at=NOW, completed_at=None, safe_error_code=None,
    )
    body = snapshot.model_dump(mode="json")
    serialized = json.dumps(body)
    for forbidden_key in (
        "id", "last_secret_id", "key", "path", "plaintext", "fingerprint",
        "ciphertext", "nonce", "wrapped_data_key", "credential_mutation_generation",
        "retirement_fence_id", "retirement_fence_generation",
        "retirement_fence_started_at", "retirement_fence_deadline",
    ):
        assert forbidden_key not in recursive_keys(body)
    assert "ROTATION_SECRET_CANARY" not in serialized
    for forbidden_global in (999, 555, 444, 7, 25):
        assert forbidden_global not in recursive_values(body)


async def test_anonymous_health_never_adds_rotation_fields(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/health")).json() == {
        "app": "Jhin", "version": "0.1.0", "status": "ok"
    }
    readiness = await client.get("/api/v1/health/ready")
    assert readiness.json() in ({"status": "ok"}, {"status": "degraded"})
```

Implement `OperationsHealthWorld.seed_rotation(...) -> MasterKeyRotation` in `apps/api/tests/conftest.py`; it fills every required durable field explicitly, including null generation for prepared/rewrapping, a bounded test generation for verifying/completed, and all four retirement-fence fields null unless a test explicitly seeds an armed internal row, then commits and returns the row. Add `seed_workspace_secret(version, workspace_id=None)` and `create_foreign_workspace()` there too; do not reference an undefined ad-hoc fixture helper from the test module. Seed invalid/boolean/negative/overflow workspace aggregates through hostile query results, invalid status/code, naive/future timestamps, and a hostile ORM-like object. Projection must return `rotation=None`, degrade `master-key` with the existing safe health code/action, and never stringify invalid input. When multiple terminal rows exist, select latest by `started_at,id`; any active row takes precedence. A completed/aborted row has `completed_at`; active rows do not. Verify Task 5 audits directly: actor `system`, workspace/actor/request/IP null, exact action, exact allowlisted metadata, and no rotation/key/secret IDs, mutation generation, fence identity, or canaries. Durable audit counters/generation/fence fields remain operator-internal authority and are never copied into the workspace response.

- [ ] **Step 2: Write the frontend typed rendering test**

```typescript
it("renders only the typed master-key rotation summary", async () => {
  server.use(operationsHealth(rotationFixture({
    from_version: 1,
    to_version: 2,
    status: "rewrapping",
    workspace_rows_total: 2,
    workspace_rows_from_version: 1,
    workspace_rows_to_version: 1,
    started_at: "2026-08-18T12:00:00Z",
    completed_at: null,
    safe_error_code: null,
  })));
  render(<OperationsPage />);
  expect(await screen.findByText("rewrapping")).toBeDefined();
  expect(screen.getByText("1 / 2 on target version")).toBeDefined();
  expect(screen.queryByText(/555|999|failed rows/i)).toBeNull();
  expect(screen.queryByText(
    /last_secret_id|credential_mutation_generation|fingerprint|ciphertext|wrapped/i
  )).toBeNull();
});
```

The TypeScript interface mirrors all fields exactly and adds no index signature/`any`. The page renders labels/status/counts/timestamps explicitly. This task keeps nonlinked safe action copy; Task 9 adds `/runbooks/master-key-rotation.md` in the same commit that creates the served byte-for-byte runbook copy, so no intermediate commit contains a broken link.

- [ ] **Step 3: Run RED**

```bash
uv run pytest apps/api/tests/test_operations_health.py -q
pnpm --filter jhin-web test -- operations-page.test.tsx
```

Expected: FAIL because rotation state is neither queried/projected nor present in the exact Python/TypeScript/UI contracts.

- [ ] **Step 4: Implement fail-closed selection and typed UI**

Select only `from_version`, `to_version`, `status`, `started_at`, `completed_at`, and `safe_error_code` from the latest state; do not select/materialize `rows_total`, `rows_rewrapped`, `rows_verified`, `rows_failed`, `last_secret_id`, `credential_mutation_generation`, any `retirement_fence_*` column, or the ORM entity. In a separate bounded aggregate, count only `Secret.workspace_id == workspace_id` into total/from/to. Convert each field with existing bounded helpers and closed enums. An invalid row/aggregate cannot partially project. Do not add another route or widen anonymous readiness. Extend existing master-key component degradation only for invalid projection/safe active error; an ordinary prepared/rewrapping/verifying state remains operational when reporter/row versions are supported.

- [ ] **Step 5: Run GREEN and commit projection/UI**

```bash
uv run pytest apps/api/tests/test_operations_health.py apps/api/tests/test_health.py -q
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test
pnpm --filter jhin-web build
uv run ruff check apps/api/src/jhin_api/health apps/api/tests/conftest.py apps/api/tests/test_operations_health.py
uv run mypy apps/api/src/jhin_api/health apps/api/tests/conftest.py
git add apps/api/src/jhin_api/health/schemas.py apps/api/src/jhin_api/health/service.py apps/api/tests/conftest.py apps/api/tests/test_operations_health.py apps/web/lib/types.ts 'apps/web/app/(app)/operations/page.tsx' apps/web/tests/operations-page.test.tsx
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "feat: show safe master key rotation progress"
```

Expected cached names: exactly the seven paths in `git add`.

### Task 8: Prove the Pre-Keyring Upgrade and Live Staged Rotation

**Files:**
- Create: `tests/integration/phase10_key_rotation_harness.py`
- Create: `tests/integration/compose.phase10-keyring-upgrade.yaml`
- Create: `tests/integration/test_phase10_keyring_upgrade.py`
- Create: `tests/integration/test_phase10_master_key_rotation.py`
- Create: `tests/test_phase10_master_key_rotation_harness.py`
- Modify: `tests/integration/conftest.py`
- Modify: `Makefile`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: Task 1's immutable pre-keyring source ref, prior rootful Compose stack/helper, fake model/connectors, all Tasks 1–7, and migrations through `0017`.
- Produces: true old-image/schema compatibility, a native-Linux UID-10001 key install/read proof, one collision-free disposable Compose project, legacy-file handoff, staged exact replica gates, active ordinary reads and parked approval, restart/resume, rollback/abort, retirement smoke, a focused Make target, and CI evidence.

- [ ] **Step 1: Write the executable harness contract before the harness**

```python
def test_pre_keyring_ref_precedes_runtime_files() -> None:
    source_ref = read_pre_keyring_ref(ROOT)
    assert re.fullmatch(r"[0-9a-f]{40}", source_ref)
    assert git_file_exists(source_ref, "services/tool_worker/pyproject.toml")
    assert not git_file_exists(source_ref, "packages/secrets/src/jhin_secrets/keyring.py")
    assert git_is_ancestor(source_ref, "HEAD")


def test_rotation_live_recipe_is_uid_correct_isolated_and_destructive_only_to_its_project() -> None:
    body = make_recipe("test-master-key-rotation-integration")
    assert "mktemp -d" in body
    assert "JHIN_TEST_COMPOSE_PROJECT" in body
    assert '-p "$$project"' in body
    assert "SANDBOX_DOCKER_GID" in body
    assert "SANDBOX_DOCKER_SOCKET_HOST" in body
    assert "validated-docker-socket" in body
    assert "install-runtime-key" in body
    assert "cleanup-runtime-key" in body
    assert "pinned-compose-down" in body
    assert (
        'JHIN_RUNTIME_KEY_IDENTITY_HANDOFF="$$fixture_dir/'
        'runtime-key-identity.json"' in body
    )
    assert "export JHIN_RUNTIME_KEY_IDENTITY_HANDOFF" in body
    assert '--identity-output "$$JHIN_RUNTIME_KEY_IDENTITY_HANDOFF"' in body
    assert '--identity-file "$$JHIN_RUNTIME_KEY_IDENTITY_HANDOFF"' in body
    assert (
        'test "$$runtime_cleanup_output" = "runtime_key_cleaned"' in body
    )
    assert "outer_trap_cleanup_ok" in body
    assert "outer_trap_cleanup_failed" in body
    assert "runtime_key_cleanup_rejected" not in body
    assert 'find "$$fixture_dir" -xdev -depth -delete' in body
    assert "trap" in body
    assert body.index("export JHIN_RUNTIME_KEY_IDENTITY_HANDOFF") < body.index(
        "trap"
    )
    assert body.index("trap") < body.index("install-runtime-key")
    assert body.rindex("pinned-compose-down") < body.rindex("cleanup-runtime-key")
    assert 'MASTER_KEY_FILE_HOST="$$runtime_key"' in body
    assert "compose.rootful.yaml" in body
    assert "test_phase10_keyring_upgrade.py" in body
    assert "test_phase10_master_key_rotation.py" in body
    assert body.count("keyring_preflight_ok") == 3
    assert body.rindex("keyring_preflight_ok") < body.index(" up -d --build --wait")
    assert "jhin-db-migrate" in body
    assert "down -v --remove-orphans" in body
    assert "POSTGRES_DEV_PORT=0" in body
    assert "API_PORT=0" in body
    assert "cat $$runtime_key" not in body
    assert "set -x" not in body
    assert "chown" not in body
    assert "rm -r" not in body


def test_compose_project_pins_validated_socket_and_ignores_hostile_inheritance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "docker.sock"
    with socket.socket(socket.AF_UNIX) as unix_socket:
        unix_socket.bind(str(socket_path))
        monkeypatch.setattr(harness_module, "DOCKER_SOCKET_HOST", socket_path)
        hostile = {
            "SANDBOX_DOCKER_SOCKET_HOST": "/tmp/HOSTILE_SOCKET_DO_NOT_USE",
            "SANDBOX_DOCKER_GID": "-123",
            "DOCKER_HOST": "tcp://remote.invalid:2376",
            "DOCKER_CONTEXT": "hostile-context",
            "DOCKER_TLS": "1",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": "/tmp/HOSTILE_CERT_PATH",
            "DOCKER_TLS_CERTDIR": "/tmp/HOSTILE_TLS_CERTDIR",
            "DOCKER_API_VERSION": "0.0",
            "DOCKER_CUSTOM_HEADERS": "X-Canary=HOSTILE_HEADER",
            "DOCKER_CONFIG": "/tmp/HOSTILE_DOCKER_CONFIG",
            "DOCKER_DEFAULT_PLATFORM": "hostile/platform",
            "COMPOSE_PROFILES": "hostile-profile",
            "COMPOSE_FILE": "/tmp/HOSTILE_COMPOSE_FILE",
            "COMPOSE_PROJECT_NAME": "hostile-project",
            "COMPOSE_ENV_FILES": "/tmp/HOSTILE_ENV_FILE",
            "COMPOSE_PATH_SEPARATOR": "!",
            "COMPOSE_DISABLE_ENV_FILE": "0",
            "JHIN_RUNTIME_KEY_IDENTITY_HANDOFF": (
                "/tmp/HOSTILE_RUNTIME_IDENTITY_HANDOFF"
            ),
        }
        for name, value in hostile.items():
            monkeypatch.setenv(name, value)
        docker_socket = validated_docker_socket()
    expected_stat = socket_path.lstat()
    assert docker_socket == DockerSocket(
        host_path=socket_path.resolve(),
        gid=expected_stat.st_gid,
        device=expected_stat.st_dev,
        inode=expected_stat.st_ino,
    )

    first = new_isolated_project(
        docker_socket=docker_socket, pid=1234, token="a1b2c3d4"
    )
    second = new_isolated_project(
        docker_socket=docker_socket, pid=1234, token="b2c3d4e5"
    )
    assert first.name == "jhin-keyrot-1234-a1b2c3d4"
    assert first.name != second.name
    assert first.env["SANDBOX_DOCKER_SOCKET_HOST"] == str(socket_path.resolve())
    assert first.env["SANDBOX_DOCKER_GID"] == str(expected_stat.st_gid)
    assert first.env["DOCKER_HOST"] == f"unix://{socket_path.resolve()}"
    assert first.env["COMPOSE_DISABLE_ENV_FILE"] == "1"
    docker_config = Path(first.env["DOCKER_CONFIG"])
    assert docker_config.is_dir()
    assert stat.S_IMODE(docker_config.stat().st_mode) == 0o700
    assert list(docker_config.iterdir()) == []
    for name in DOCKER_AUTHORITY_ENV_TO_SCRUB - {
        "DOCKER_HOST", "DOCKER_CONFIG", "COMPOSE_DISABLE_ENV_FILE"
    }:
        assert name not in first.env
    assert first.env["DOCKER_HOST"] != hostile["DOCKER_HOST"]
    assert first.env["DOCKER_CONFIG"] != hostile["DOCKER_CONFIG"]
    assert first.env["COMPOSE_DISABLE_ENV_FILE"] != hostile[
        "COMPOSE_DISABLE_ENV_FILE"
    ]
    assert first.env["SANDBOX_DOCKER_SOCKET_HOST"] != hostile[
        "SANDBOX_DOCKER_SOCKET_HOST"
    ]
    assert first.env["SANDBOX_DOCKER_GID"] != hostile["SANDBOX_DOCKER_GID"]
    assert "JHIN_RUNTIME_KEY_IDENTITY_HANDOFF" not in first.env
    zero_ports = {
        "WEB_PORT", "API_PORT", "FAKE_SUPABASE_DB_DEV_PORT",
        "FAKE_PROVIDER_DEV_PORT", "FAKE_GITHUB_DEV_PORT",
        "FAKE_LINEAR_DEV_PORT", "FAKE_VERCEL_DEV_PORT",
        "FAKE_SUPABASE_DEV_PORT", "SANDBOX_RUNNER_DEV_PORT",
        "POSTGRES_DEV_PORT", "NATS_DEV_PORT", "NATS_MONITOR_DEV_PORT",
        "TEMPORAL_DEV_PORT", "TEMPORAL_UI_DEV_PORT",
    }
    assert {name for name in zero_ports if first.env[name] == "0"} == zero_ports
    assert first.env["SANDBOX_NETWORK"] == f"{first.name}_sandbox"
    commands = [
        first.argv("build"),
        first.argv("up", "-d", "--wait"),
        first.argv("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
        first.cleanup_argv,
    ]
    for command in commands:
        assert command[:4] == ["docker", "compose", "-p", first.name]
        assert command[4:12] == [
            "-f", "compose.yaml",
            "-f", "compose.dev.yaml",
            "-f", "compose.rootful.yaml",
            "-f", "tests/integration/compose.phase10-keyring-upgrade.yaml",
        ]
    assert first.cleanup_argv[-3:] == ["down", "-v", "--remove-orphans"]
    rendered = first.render_config()
    assert all(
        config["name"].startswith(f"{first.name}_")
        for config in rendered["volumes"].values()
    )
    assert rendered["networks"]["sandbox"]["name"] == f"{first.name}_sandbox"
    assert all(
        published == "0"
        for service in rendered["services"].values()
        for port in service.get("ports", [])
        for published in [str(port["published"])]
    )

    recorder = RecordingCommandRunner()
    for argv in (
        first.argv("config"),
        first.argv("build"),
        first.argv("up", "-d", "--wait"),
        first.argv("port", "api", "8000"),
        ["docker", "build", "--tag", "probe", "-"],
        ["docker", "image", "inspect", "probe"],
        ["docker", "network", "inspect", f"{first.name}_sandbox"],
        ["docker", "volume", "inspect", f"{first.name}_postgres_data"],
        first.argv(
            "exec", "-T", "api", "stat", "-c", "%u:%g:%a",
            "/run/secrets/jhin_master_key",
        ),
        first.cleanup_argv,
    ):
        first.run_docker_argv(argv, runner=recorder)
    assert len(recorder.calls) == 10
    for call in recorder.calls:
        assert call.env == first.env
        assert call.env["DOCKER_HOST"] == f"unix://{socket_path.resolve()}"
        assert call.env["COMPOSE_DISABLE_ENV_FILE"] == "1"
        assert call.env.get("DOCKER_CONTEXT") is None
        assert call.env.get("COMPOSE_PROFILES") is None
def test_runtime_installer_uses_only_stdin_and_root_owned_exact_targets() -> None:
    target = RuntimeKeyTarget(
        fixed_root=Path("/run/jhin-key-rotation"),
        leaf_name="jhin-keyrot-1234-a1b2c3d4",
        runtime_file=Path(
            "/run/jhin-key-rotation/jhin-keyrot-1234-a1b2c3d4/jhin_master_key"
        ),
    )
    assert prepare_runtime_root_argv(target) == [
        "sudo", "-n", "install", "-d", "-m", "0700", "-o", "0", "-g", "0",
        "--", "/run/jhin-key-rotation",
    ]
    assert prepare_runtime_leaf_argv(target) == [
        "sudo", "-n", "install", "-d", "-m", "0700", "-o", "0", "-g", "0",
        "--", str(target.runtime_file.parent),
    ]
    install = runtime_key_install_argv(target)
    assert install == [
        "sudo", "-n", "install", "-m", "0600", "-o", "10001", "-g", "10001",
        "--", "/proc/self/fd/0", str(target.runtime_file),
    ]
    identity = RuntimeKeyIdentitySnapshot(
        format_version=1,
        project_name=target.leaf_name,
        filesystem_root=RuntimePathIdentity(1, 2, 0, 0, 0o755, 1),
        run_root=RuntimePathIdentity(1, 3, 0, 0, 0o755, 1),
        fixed_root=RuntimePathIdentity(1, 4, 0, 0, 0o700, 1),
        random_leaf=RuntimePathIdentity(1, 5, 0, 0, 0o700, 1),
        runtime_file=RuntimePathIdentity(1, 6, 10001, 10001, 0o600, 1),
    )
    assert cleanup_runtime_key_argv(target, identity) == [
        ["sudo", "-n", "rm", "--", str(target.runtime_file)],
        ["sudo", "-n", "rmdir", "--", str(target.runtime_file.parent)],
    ]
    rendered = " ".join(install)
    assert "operator" not in rendered
    assert "MASTER_KEY" not in rendered
    assert all("*" not in arg and "-R" not in arg for arg in install)
    assert set(runtime_key_install_environment()) == {"PATH", "LANG"}


def test_runtime_install_passes_only_the_unprivileged_open_fd(
    tmp_path: Path,
) -> None:
    operator_key = tmp_path / "operator-key"
    operator_key.write_bytes(b"K" * 32)
    operator_key.chmod(0o600)
    target = RuntimeKeyTarget(
        fixed_root=Path("/run/jhin-key-rotation"),
        leaf_name="jhin-keyrot-1234-a1b2c3d4",
        runtime_file=Path(
            "/run/jhin-key-rotation/jhin-keyrot-1234-a1b2c3d4/jhin_master_key"
        ),
    )
    fd = open_operator_key_fd(operator_key)
    try:
        assert not os.get_inheritable(fd)
        recorder = RecordingCommandRunner()
        invoke_runtime_key_install(fd=fd, target=target, runner=recorder)
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call.argv == runtime_key_install_argv(target)
        assert call.stdin_fd == fd
        assert call.stdin_stream is None
        assert call.env == runtime_key_install_environment()
    finally:
        os.close(fd)


def test_runtime_installer_cli_closes_argparse_and_os_errors_without_paths_or_bytes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    operator_key = tmp_path / "OPERATOR_SOURCE_PATH_CANARY"
    operator_key.write_bytes(b"RUNTIME_KEY_BYTES_CANARY")
    operator_key.chmod(0o600)
    target = RuntimeKeyTarget(
        fixed_root=Path("/run/jhin-key-rotation"),
        leaf_name="jhin-keyrot-1234-a1b2c3d4",
        runtime_file=Path(
            "/run/jhin-key-rotation/jhin-keyrot-1234-a1b2c3d4/jhin_master_key"
        ),
    )
    identity_output = tmp_path / "runtime-identity.json"
    outside = tmp_path / "OUTSIDE_REFERENT_PATH_CANARY"
    failures = (
        FileExistsError(17, "HOSTILE_FILE_EXISTS_ERROR", str(outside)),
        OSError(5, "HOSTILE_OS_ERROR", str(outside)),
    )
    results = [
        run_runtime_key_install_cli(
            [
                "--operator-key", str(operator_key),
                "--project", "jhin-keyrot-test",
                "--identity-output", str(identity_output),
            ],
            runner=RaisingCommandRunner(failure),
            target_factory=lambda _project: target,
        )
        for failure in failures
    ]
    results.append(
        run_runtime_key_install_cli(
            [
                "--operator-key", str(operator_key),
                "--project", "../BAD_PROJECT_CANARY",
                "--identity-output", str(identity_output),
            ],
            runner=NoOpCommandRunner(),
            target_factory=lambda _project: target,
        )
    )
    results.append(
        run_runtime_key_install_cli(
            [
                "--operator-key", str(tmp_path / "MISSING_SOURCE_PATH_CANARY"),
                "--project", "jhin-keyrot-test",
                "--identity-output", str(identity_output),
            ],
            runner=NoOpCommandRunner(),
            target_factory=lambda _project: target,
        )
    )
    assert [(result.returncode, result.stdout, result.stderr) for result in results] == [
        (1, "", "runtime_key_install_failed\n"),
        (1, "", "runtime_key_install_failed\n"),
        (2, "", "runtime_key_install_invalid\n"),
        (1, "", "runtime_key_source_unreadable\n"),
    ]
    rendered = "".join(result.stdout + result.stderr for result in results) + caplog.text
    for forbidden in (
        str(operator_key), str(target.runtime_file), str(outside),
        str(identity_output),
        "RUNTIME_KEY_BYTES_CANARY", "HOSTILE_FILE_EXISTS_ERROR",
        "HOSTILE_OS_ERROR", "BAD_PROJECT_CANARY", "MISSING_SOURCE_PATH_CANARY",
    ):
        assert forbidden not in rendered


def identity_snapshot(*, runtime_inode: int) -> RuntimeKeyIdentitySnapshot:
    return RuntimeKeyIdentitySnapshot(
        format_version=1,
        project_name="jhin-keyrot-test",
        filesystem_root=RuntimePathIdentity(1, 2, 0, 0, 0o755, 1),
        run_root=RuntimePathIdentity(1, 3, 0, 0, 0o755, 1),
        fixed_root=RuntimePathIdentity(1, 4, 0, 0, 0o700, 1),
        random_leaf=RuntimePathIdentity(1, 5, 0, 0, 0o700, 1),
        runtime_file=RuntimePathIdentity(
            1, runtime_inode, 10001, 10001, 0o600, 1
        ),
    )


def copy_identity_handoff_for_test(source: Path, destination: Path) -> None:
    raw = source.read_bytes()
    fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        assert os.write(fd, raw) == len(raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def test_replacement_atomically_updates_stable_identity_handoff_for_cleanup(
    tmp_path: Path,
) -> None:
    world = FakePrivilegedRuntimeWorld(project="jhin-keyrot-test")
    source = write_private_keyring(tmp_path / "operator-key")
    handoff = tmp_path / "runtime-identity.json"
    assert run_runtime_key_install_cli(
        [
            "--operator-key", str(source),
            "--project", world.project,
            "--identity-output", str(handoff),
        ],
        runner=world.runner,
        target_factory=world.target,
    ).returncode == 0
    assert stat.S_IMODE(handoff.lstat().st_mode) == 0o600
    assert handoff.lstat().st_nlink == 1
    assert handoff.stat().st_size <= MAX_RUNTIME_IDENTITY_BYTES
    receipt = json.loads(handoff.read_text(encoding="ascii"))
    assert set(receipt) == {
        "format_version", "project_name", "filesystem_root", "run_root",
        "fixed_root", "random_leaf", "runtime_file",
    }
    assert str(world.target(world.project).runtime_file) not in json.dumps(receipt)
    assert "KEY_MATERIAL" not in json.dumps(receipt)
    first = read_runtime_identity_handoff(handoff)
    stale = tmp_path / "stale-identity.json"
    copy_identity_handoff_for_test(handoff, stale)
    handoff_inode = handoff.lstat().st_ino

    # GNU install replaces the runtime target inode; the same stable handoff
    # path must be atomically replaced with the new validated receipt.
    world.replace_runtime_file_inode()
    assert run_runtime_key_install_cli(
        [
            "--operator-key", str(source),
            "--project", world.project,
            "--identity-output", str(handoff),
        ],
        runner=world.runner,
        target_factory=world.target,
    ).returncode == 0
    current = read_runtime_identity_handoff(handoff)
    assert current.runtime_file.inode != first.runtime_file.inode
    assert handoff.lstat().st_ino != handoff_inode
    assert list(tmp_path.glob(".runtime-identity.*.next")) == []
    before = world.privileged_metadata()
    stale_result = run_runtime_key_cleanup_cli(
        [
            "--project", world.project,
            "--identity-file", str(stale),
        ],
        runner=world.runner,
        target_factory=world.target,
    )
    assert (stale_result.returncode, stale_result.stdout, stale_result.stderr) == (
        1, "", "runtime_key_cleanup_rejected\n"
    )
    assert world.privileged_metadata() == before

    cleaned = run_runtime_key_cleanup_cli(
        [
            "--project", world.project,
            "--identity-file", str(handoff),
        ],
        runner=world.runner,
        target_factory=world.target,
    )
    assert (cleaned.returncode, cleaned.stdout, cleaned.stderr) == (
        0, "runtime_key_cleaned\n", ""
    )
    assert world.runtime_leaf_absent()
    call_count_before = len(world.runner.calls)
    missing = run_runtime_key_cleanup_cli(
        [
            "--project", world.project,
            "--identity-file", str(handoff),
        ],
        runner=world.runner,
        target_factory=world.target,
    )
    assert (missing.returncode, missing.stdout, missing.stderr) == (
        1, "", "runtime_key_cleanup_rejected\n"
    )
    assert not any(
        tuple(call.argv[:3])
        in (("sudo", "-n", "rm"), ("sudo", "-n", "rmdir"))
        for call in world.runner.calls[call_count_before:]
    )
    assert world.runtime_leaf_absent()


@pytest.mark.parametrize(
    ("failpoint", "expected"),
    [
        ("after-runtime-install-before-identity-write", "old"),
        ("after-identity-sibling-fsync-before-replace", "old"),
        ("after-identity-replace-before-directory-fsync", "new"),
    ],
)
def test_identity_handoff_crash_is_old_or_new_never_partial(
    tmp_path: Path,
    failpoint: RuntimeIdentityFailpoint,
    expected: Literal["old", "new"],
) -> None:
    handoff = tmp_path / "runtime-identity.json"
    old = identity_snapshot(runtime_inode=101)
    new = identity_snapshot(runtime_inode=202)
    publish_runtime_identity_handoff(handoff, old)
    with pytest.raises(SimulatedIdentityPublishCrash):
        publish_runtime_identity_handoff(handoff, new, failpoint=failpoint)
    actual = read_runtime_identity_handoff(handoff)
    assert actual == (old if expected == "old" else new)
    assert handoff.lstat().st_nlink == 1
    assert stat.S_IMODE(handoff.lstat().st_mode) == 0o600
    assert handoff.stat().st_size <= MAX_RUNTIME_IDENTITY_BYTES
```

Also assert `new_isolated_project(docker_socket=...)` returns a lowercase regex-valid `jhin-keyrot-<pid>-<8 hex>` value and two calls differ. `validated_docker_socket()` ignores all inherited Docker/Compose authority and profile variables, resolves only the fixed module constant `/var/run/docker.sock`, requires a nonsymlink Unix socket with positive numeric GID, and records its `(device,inode)`. `pinned_docker_environment(...)` starts from a minimal allowlist, removes every member of `DOCKER_AUTHORITY_ENV_TO_SCRUB`, then sets exact `COMPOSE_DISABLE_ENV_FILE=1`, `DOCKER_HOST=unix://<that exact resolved socket>`, `SANDBOX_DOCKER_SOCKET_HOST` and `SANDBOX_DOCKER_GID` from the same snapshot, and points `DOCKER_CONFIG` at a new empty mode-0700 harness directory so neither a current context nor TLS/auth configuration is inherited. It never merely unsets `COMPOSE_DISABLE_ENV_FILE`: automatic repository `.env` loading is explicitly disabled. Immediately before **every** Docker CLI call—including direct image builds/inspection, network/volume inspection, Compose config/build/up/run/port/down, the hostile-`.env` render, probes, and teardown—one central `ComposeProject.run_docker_argv(...)` re-lstats the same socket and rejects a changed path, inode, type, or GID. Every call receives the identical pinned environment; no code path calls `docker` or `docker compose` directly. This prevents a hostile repository `.env`, `DOCKER_CONTEXT`, remote `DOCKER_HOST`, TLS/certificate settings, custom headers, Compose file/project/env-file/profile settings, or profile from redirecting even a cleanup call. Unit/static rootful rendering elsewhere supplies only sentinel `4242`; the live caller uses the discovered socket/GID pair and has no dummy fallback. `FakePrivilegedRuntimeWorld` models only numeric metadata and exact privileged argv; each install assigns a new runtime-file inode without changing the derived path. The missing-path test inspects newly recorded argv and rejects any second exact `sudo -n rm --`/`sudo -n rmdir --` call. `identity_snapshot()`, `read_runtime_identity_handoff()`, and `copy_identity_handoff_for_test()` use the production strict serializer/parser and descriptor rules; the copy helper exists only in tests to retain one old receipt. It never weakens the native-Linux separate-process authority test below.

Parse every operational stack command argv and require `docker compose -p <that exact project> -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml -f tests/integration/compose.phase10-keyring-upgrade.yaml`; no invocation may use the default `jhin` project. Its environment sets every published dev port variable (`WEB_PORT`, `API_PORT`, all five fake-service ports, fake DB, sandbox, PostgreSQL, both NATS ports, Temporal, and Temporal UI) to `0`; `ComposeProject.host_port(service, container_port)` obtains Docker's assigned loopback port and builds the API/PostgreSQL DSNs without logging them. `SANDBOX_NETWORK=<project>_sandbox`, a unique Temporal namespace, and project-prefixed volumes eliminate fixed resources. Parse `compose.phase10-keyring-upgrade.yaml`: legacy services are exactly `api-pre-keyring`, `agent-worker-pre-keyring`, and `tool-worker-pre-keyring`; they use caller-supplied immutable image tags, same project-local data/control/runner networks and database/Temporal namespace, no published ports, no model settings in tool-worker, `APP_ENV=test`, and the same read-only `/run/secrets/jhin_master_key` runtime target. No key material or inline key value appears in YAML/environment; only the expected fixed container target and exact validated source path may appear.

The `render_hostile_repository_dotenv_probe()` signature below is declared at this boundary but is deliberately not implemented in Step 3. Step 5 first adds its real-Compose RED test; Step 6 then creates a private nonprivileged temporary project directory containing only the supplied hostile `.env` and one-service Compose file. Through `project.run_docker_argv` and the identical socket-pinned environment it runs exactly `docker compose -p <project> --project-directory <temp> -f <temp>/compose.yaml config --format json`; `COMPOSE_DISABLE_ENV_FILE=1` must leave the safe interpolation default and empty profile set. It parses only the project name, probe image, and profiles into `DotenvRenderProbe`, discards raw output, and removes the two files/directory in `finally`. The probe never starts a container and is the sole non-stack Compose file-vector exception; it still uses the same project, Docker host/config, socket revalidation, timeout, and teardown-independent command boundary.

- [ ] **Step 2: Run the harness RED test**

```bash
uv run pytest tests/test_phase10_master_key_rotation_harness.py -q
```

Expected: FAIL because the upgrade harness/overlay, pinned Docker runner, strict runtime identity receipt/install-cleanup CLIs, and Make target do not exist.

- [ ] **Step 3: Implement only the frozen-ref/archive/overlay harness contract**

`build_pre_keyring_images(repo, source_ref)` streams `git archive --format=tar <sha>` separately into three `docker build -` invocations using the frozen Dockerfile and package arguments `jhin-api`, `jhin-agent-worker`, and `jhin-tool-worker`. Tag them `jhin-pre-keyring-<service>:<sha12>`. Never build the legacy image from the working tree.

Define `ComposeProject(name, env, docker_socket)` with `argv(*args)` always inserting `docker compose -p name` before the four fixed files, `run_docker_argv(argv, runner, timeout)` as the single Docker subprocess boundary, `run(*args, timeout)` delegating to it, and `host_port(service, container_port)`. `validated_docker_socket()` ignores inherited socket/GID values and all members of `DOCKER_AUTHORITY_ENV_TO_SCRUB`, resolves only `DOCKER_SOCKET_HOST`, rejects a symlink/non-socket/zero-or-negative GID, and snapshots `(path,gid,device,inode)` from one `lstat`. `pinned_docker_environment(...)` builds a minimal environment, creates an empty mode-0700 `DOCKER_CONFIG`, scrubs all authority variables, and then sets exact `COMPOSE_DISABLE_ENV_FILE=1`, `DOCKER_HOST=unix://<resolved socket>`, plus the rootful socket/GID pair. Immediately before every `docker` argv, `run_docker_argv` re-lstats and refuses a changed socket path, inode, type, or GID. The pre-keyring `docker build -` stream, image/network/volume queries, hostile repository `.env` config probe, Compose config/build/up/run/port/down, and failure cleanup all use this same method and identical environment. Do not inherit or reconstruct a second Docker environment for teardown.

`new_isolated_project(docker_socket=...)` validates `jhin-keyrot-{os.getpid()}-{secrets.token_hex(4)}` against `[a-z0-9][a-z0-9_-]*` and stores that exact validated path/GID pair in its isolated Docker environment; it sets every dev published-port variable to `0`, `SANDBOX_NETWORK=f"{name}_sandbox"`, `TEMPORAL_NAMESPACE=f"{name}-ns"`, and a per-project database name. In addition to `DOCKER_AUTHORITY_ENV_TO_SCRUB`, it removes inherited `JHIN_TEST_POSTGRES_DSN`, `JHIN_API_URL`, `JHIN_TEST_COMPOSE_PROJECT`, `JHIN_RUNTIME_KEY_IDENTITY_HANDOFF`, `SANDBOX_DOCKER_SOCKET_HOST`, `SANDBOX_DOCKER_GID`, and every stale port override before setting its own values, then explicitly restores only the pinned `DOCKER_HOST`, `DOCKER_CONFIG`, `COMPOSE_DISABLE_ENV_FILE=1`, and validated rootful socket/GID values. The Make recipe separately exports the dynamically discovered loopback API URL/PostgreSQL DSN, exact project name, and stable handoff to its pytest process; it never passes the handoff variable into `ComposeProject.env` or a container.

Runtime-key delivery uses no user-writable privileged path. `new_runtime_key_target(project_name)` returns only a validated random leaf under fixed `/run/jhin-key-rotation`: `/`, `/run`, and the fixed parent are root-owned/non-user-writable, the random leaf is root-owned mode `0700`, and the exact target is `<leaf>/jhin_master_key`. `prepare_runtime_root_argv` and `prepare_runtime_leaf_argv` use exact `sudo -n install -d -m 0700 -o 0 -g 0 -- <path>` argument arrays. Before and after each privileged operation, a bounded numeric-stat helper validates `/`, `/run`, the fixed parent, the leaf, and target as applicable: no symlink, exact expected root ownership/mode, exact captured device/inode, and no unexpected path component. The random leaf and fixed root must match closed regex/constant checks before an argv is constructed. Nothing beneath an invoking-user-owned temporary directory participates in a privileged path.

`open_operator_key_fd(operator_key)` runs without privilege and opens the operator-controlled source with `O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK`; `fstat` requires a regular, single-link, safely permissioned, bounded-size file. It never passes the source path to `sudo`. Every `install_runtime_key(operator_key)` call supplies that already-open descriptor as subprocess stdin to exactly `sudo -n install -m 0600 -o 10001 -g 10001 -- /proc/self/fd/0 <validated-target>`. GNU install creates/replaces the target, so every initial install, distributed/activated/retired replacement, rollback, and negative-mode recovery must assume a new runtime-file inode. The method closes the source descriptor in `finally`, discards raw subprocess output, maps every `argparse`, `OSError`, `CalledProcessError`, stat, and timeout case to a closed code, then obtains privileged numeric metadata for `/`, `/run`, fixed root, leaf, and the new file and validates the regular single-link target at `10001:10001/0600` beneath the unchanged root-owned chain.

Only after that validation, `publish_runtime_identity_handoff(handoff, snapshot)` serializes strict ASCII JSON of at most 4,096 bytes. `handoff` is the single stable `$fixture_dir/runtime-key-identity.json` exposed to both Make and pytest as `JHIN_RUNTIME_KEY_IDENTITY_HANDOFF`; its invoking-user-owned mode-0700 parent and an existing handoff's regular-file/invoking-UID/0600/single-link identity are validated without following links. Publication never truncates the stable file. It creates a randomized same-directory `.runtime-key-identity.<pid>.<16-hex>.next` with `O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW` mode `0600`, writes completely, calls `fsync(sibling_fd)`, revalidates sibling and stable parent, calls `os.replace(sibling, handoff)`, opens/fsyncs the parent directory, and finally parses/revalidates the stable receipt against the current privileged metadata before reporting `runtime_key_installed`. The receipt contains only project plus numeric metadata—no key bytes or privileged/source path. Duplicate/unknown fields, booleans, negative/noncanonical numbers, symlink/nonregular/oversized/multi-link files, a different parent filesystem, or an unexpected handoff path fail closed. The nonsecret stable handoff path appears only in its named local Make/pytest variable and explicit installer/cleanup argv and is scrubbed from Docker/container environments; the exact runtime source and fixed container target may appear only in Compose configuration. The operator source, key bytes, randomized sibling, unexpected/sensitive paths, raw OS error, and command argv never appear on stdout/stderr or in artifacts.

Crash semantics are explicit. Before `os.replace`, the stable handoff remains the complete old receipt; because GNU install has already replaced the runtime inode, cleanup compares it, rejects `runtime_key_cleanup_rejected`, and touches nothing. After `os.replace`, readers see the complete new receipt; if process death precedes directory fsync, a running-system cleanup may proceed only when metadata matches, while reboot recovery may expose old or new and therefore either matches or refuses. No state exposes partial JSON or lets an old receipt authorize a new inode. A leftover `.next` file is ignored by cleanup and removed only by nonprivileged fixture cleanup; code never globs it into authority. Re-running install safely republishes a matching current receipt. `install_runtime_key_process(..., failpoint=name)` appends exact hidden argv `--test-identity-failpoint <name>`; the three exact test-only failpoints above execute `os._exit(97)` only in the harness subprocess under `APP_ENV=test` and `JHIN_RUN_MASTER_KEY_LIVE=1`. The parser rejects that argv in every other environment as `runtime_key_install_invalid`, and the process writes neither stdout nor stderr before the intentional exit.

Cleanup runs only after the pinned Compose teardown and in a separate process requires `--identity-file "$JHIN_RUNTIME_KEY_IDENTITY_HANDOFF"`; alternate `.next`/snapshot paths are never selected by the Make trap. It opens the stable receipt unprivileged with `O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK`, requires exact invoking ownership/mode/single link/bounded strict schema, and derives the privileged target only from the independently validated `--project` and fixed root—receipt data never supplies a path. Immediately before each effect it obtains sanitized privileged numeric metadata and requires every current root/leaf/file device+inode/owner/mode/link value to equal the latest receipt; it then calls exactly `sudo -n rm -- <validated-target>`, revalidates root/leaf identity and emptiness, and calls exactly `sudo -n rmdir -- <validated-empty-leaf>`. A stale copied receipt from a prior install cannot authorize cleanup of a same-project replacement tree in another process, while the stable receipt published by the latest successful replacement does. Cleanup never recursively removes, globs, follows a link, removes the fixed parent, removes the handoff, or removes an operator file. A failed receipt/identity/emptiness check emits only `runtime_key_cleanup_rejected` and leaves the path for bounded privileged inspection. The test-only `_before_runtime_key_operation(phase)` no-op hook is overridden only by `RuntimeAncestorRaceBarrier`; production has no callback. Immediately before `install` and immediately before cleanup, the rename case tries `os.rename(random_leaf, sibling_name)` as the invoking user; the symlink case tries that rename followed by `os.symlink(outside, random_leaf)`. The root-owned mode-0700 fixed parent makes the first path mutation fail with `PermissionError`, so the symlink step is unreachable. The operation then completes against the receipt-pinned target and the outside referent remains byte- and metadata-identical.

`runtime_ancestor_metadata()`, `runtime_key_metadata()`, and `runtime_key_identity()` invoke the same exact bounded privileged numeric-stat primitive, parse only allowlisted integers, validate them internally, and return fixed labels or a `RuntimePathIdentity`; they discard raw stdout before returning. `runtime_identity_file()` returns only the exported stable handoff, `read_runtime_identity()` defaults to that path, and `runtime_identity_orphan_count()` counts only exact same-parent `.runtime-key-identity.<pid>.<16-hex>.next` regular files without treating one as authority. The invoking-user pytest process never calls `Path.stat()`/`lstat()` on the runtime file or leaf because root-owned mode `0700` intentionally prevents traversal. Container readability is proved separately by `container_key_stat()` running as UID 10001 through the pinned Compose runner.

The Make target sets and exports one `JHIN_RUNTIME_KEY_IDENTITY_HANDOFF="$fixture_dir/runtime-key-identity.json"` before installing its trap, then invokes `install-runtime-key --operator-key "$operator_key" --project "$JHIN_TEST_COMPOSE_PROJECT" --identity-output "$JHIN_RUNTIME_KEY_IDENTITY_HANDOFF"` for **every** initial/replacement/rollback install. The CLI interprets `--identity-output` as the stable handoff destination and performs the exclusive-sibling atomic publication above; pytest's `KeyRotationHarness.runtime_identity_file()` reads the same exported value and never invents a second current path. The EXIT trap preserves the incoming test status, performs pinned Compose teardown, then invokes `cleanup-runtime-key --project "$JHIN_TEST_COMPOSE_PROJECT" --identity-file "$JHIN_RUNTIME_KEY_IDENTITY_HANDOFF"` exactly once and captures its bounded combined output in `runtime_cleanup_output`. It requires exit zero and exact `test "$runtime_cleanup_output" = "runtime_key_cleaned"`; success prints only `outer_trap_cleanup_ok`, while any missing/stale/mismatched receipt or other nonzero/alternate output is suppressed and becomes `outer_trap_cleanup_failed` plus a nonzero target exit. It never treats an already-missing runtime path as success and never prints `runtime_key_cleanup_rejected` from the outer trap. After cleanup succeeds it removes the nonprivileged fixture and returns the original nonzero test status, or zero only when both tests and cleanup succeeded. Although the fixed container target and configured Compose source are allowed inside Compose configuration, neither source/target/identity/sibling path nor command argv is reflected in CLI output. All privileged constructors reject an invalid constant/root/leaf/target or mismatched receipt before returning argv.

At this step implement only source-ref validation, immutable image building, safe subprocess/result primitives, overlay parsing, and the Make-recipe contract needed to make `tests/test_phase10_master_key_rotation_harness.py` green. Do not implement staged lifecycle actions before their live tests are written and observed RED. The next steps require `KeyRotationHarness` to expose these exact bounded methods:

```python
DOCKER_SOCKET_HOST = Path("/var/run/docker.sock")
RUNTIME_KEY_ROOT = Path("/run/jhin-key-rotation")
MAX_RUNTIME_IDENTITY_BYTES = 4_096
RuntimeIdentityFailpoint = Literal[
    "after-runtime-install-before-identity-write",
    "after-identity-sibling-fsync-before-replace",
    "after-identity-replace-before-directory-fsync",
]
DOCKER_AUTHORITY_ENV_TO_SCRUB = frozenset({
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH", "DOCKER_TLS_CERTDIR", "DOCKER_API_VERSION",
    "DOCKER_CUSTOM_HEADERS",
    "DOCKER_CONFIG", "DOCKER_DEFAULT_PLATFORM",
    "COMPOSE_PROFILES", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME",
    "COMPOSE_ENV_FILES", "COMPOSE_PATH_SEPARATOR", "COMPOSE_DISABLE_ENV_FILE",
})

@dataclass(frozen=True)
class DockerSocket:
    host_path: Path
    gid: int
    device: int
    inode: int

def validated_docker_socket(
    environ: Mapping[str, str] | None = None,
) -> DockerSocket: ...

@dataclass(frozen=True)
class RuntimeKeyTarget:
    fixed_root: Path
    leaf_name: str
    runtime_file: Path

@dataclass(frozen=True)
class RuntimePathIdentity:
    device: int
    inode: int
    uid: int
    gid: int
    mode: int
    links: int

@dataclass(frozen=True)
class RuntimeKeyIdentitySnapshot:
    format_version: Literal[1]
    project_name: str
    filesystem_root: RuntimePathIdentity
    run_root: RuntimePathIdentity
    fixed_root: RuntimePathIdentity
    random_leaf: RuntimePathIdentity
    runtime_file: RuntimePathIdentity

class RuntimeKeyInstallError(RuntimeError): ...
class SimulatedIdentityPublishCrash(RuntimeError): ...
class HarnessContractError(RuntimeError): ...

@dataclass(frozen=True)
class SafeCommandResult:
    returncode: int
    stdout: str
    stderr: str

class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        stdin_fd: int | None = None,
        stdin_stream: BinaryIO | None = None,
        stdout_fd: int | None = None,
        timeout: float,
    ) -> SafeCommandResult: ...

@dataclass(frozen=True)
class DotenvRenderProbe:
    image: str
    project_name: str
    profiles: tuple[str, ...]
    command_env: Mapping[str, str]
    allowlisted_render: Mapping[str, object]

@dataclass(frozen=True)
class SafeCliResult:
    returncode: int
    body: Mapping[str, object]
    stdout: str
    stderr: str

@dataclass(frozen=True)
class BackupEvidence:
    stage: Literal["pre", "post"]
    project_name: str
    credential_mutation_generation: int
    keyring_versions: tuple[int, ...]
    database_verified: bool
    keyring_verified: bool

@dataclass(frozen=True)
class StackRuntimeSnapshot:
    key_service_generations: tuple[tuple[str, int], ...]
    runtime_key_metadata: tuple[str, str, str, str] | None
    rotation_attempt_count: int
    rotation_audit_count: int

class RuntimeAncestorRaceBarrier(Protocol):
    arrived: asyncio.Event
    release: asyncio.Event
    def attempt_unprivileged_swap(
        self, *, outside: Path, attack: Literal["rename", "symlink"]
    ) -> None: ...

@dataclass(frozen=True)
class ComposeProject:
    name: str
    env: Mapping[str, str]
    docker_socket: DockerSocket
    def argv(self, *args: str) -> list[str]: ...
    @property
    def cleanup_argv(self) -> list[str]: ...
    def render_config(self) -> dict[str, object]: ...
    def host_port(self, service: str, container_port: int) -> int: ...
    def run_docker_argv(
        self,
        argv: Sequence[str],
        *,
        runner: CommandRunner,
        stdout_fd: int | None = None,
        timeout: float = 120.0,
    ) -> SafeCommandResult: ...

def pinned_docker_environment(
    socket: DockerSocket,
    *,
    base: Mapping[str, str],
    docker_config_dir: Path,
) -> dict[str, str]: ...

def new_isolated_project(
    *, docker_socket: DockerSocket, pid: int | None = None, token: str | None = None
) -> ComposeProject: ...

class KeyRotationHarness:
    project: ComposeProject
    def install_runtime_key(self, operator_key: Path) -> Path: ...
    def install_runtime_key_for_mode_test(
        self, operator_key: Path, *, mode: Literal[0o640]
    ) -> Path: ...
    def install_runtime_key_process(
        self,
        operator_key: Path,
        *,
        failpoint: RuntimeIdentityFailpoint | None = None,
    ) -> SafeCommandResult: ...
    def cleanup_runtime_key_process(
        self, *, identity_file: Path
    ) -> SafeCommandResult: ...
    def private_legacy_v1_file(self) -> Path: ...
    def distributed_ring(self, *, from_version: int, to_version: int) -> Path: ...
    def activate_ring(self, source: Path, *, version: int) -> Path: ...
    def retire_version(self, source: Path, *, version: int) -> Path: ...
    def offline_add_version(self, source: Path, *, version: int, active: int) -> Path: ...
    async def migrate_head(self, revision: str) -> None: ...
    async def start_pre_keyring_services(self, legacy_file: Path) -> None: ...
    async def start_current_key_services(self, runtime_file: Path) -> None: ...
    async def stop_pre_keyring_services(self) -> None: ...
    async def recreate_key_services(
        self, keyring_file: Path, *, fail_service: str | None = None
    ) -> None: ...
    async def restart(self, service: str) -> None: ...
    async def container_key_stat(self, service: str) -> tuple[str, str, str, str]: ...
    async def wait_key_gate(self, stage: RotationStage, timeout: float = 65.0) -> None: ...
    async def wait_exact_reporters(self, *, active: int, supported: tuple[int, ...]) -> None: ...
    async def run_rotation(self, *, max_batches: int | None = None) -> SafeCliResult: ...
    async def retirement_action(
        self,
        action: Literal["arm", "commit", "cancel"],
        *,
        fence_id: UUID | None = None,
    ) -> SafeCliResult: ...
    async def render_hostile_repository_dotenv_probe(
        self, *, dotenv: str, compose: str
    ) -> DotenvRenderProbe: ...
    async def assert_semantic_mutation_fenced(
        self, mutation: Literal["same-id-rotate", "delete", "create"]
    ) -> str: ...
    async def verify_backup_pair(self, *, stage: Literal["pre", "post"]) -> BackupEvidence: ...
    def fail_backup_component_for_test(
        self,
        *,
        stage: Literal["pre", "post"],
        component: Literal["database", "keyring"],
    ) -> None: ...
    async def completed_activated_rotation(
        self,
        *,
        from_version: int,
        to_version: int,
        pre_backup: BackupEvidence,
    ) -> Path: ...
    async def ordinary_secret_read_cycle(self) -> OrdinaryReadEvidence: ...
    async def ordinary_read_loop(self, stop: asyncio.Event) -> OrdinaryReadEvidence: ...
    async def park_connector_approval(self) -> ParkedApprovalEvidence: ...
    async def approve_and_resolve(self, approval_id: UUID) -> ToolEffectEvidence: ...
    async def seed_credentials(self, *, expected_version: int) -> tuple[UUID, ...]: ...
    async def seed_credentials_through_pre_keyring_api(self) -> tuple[UUID, ...]: ...
    async def pre_keyring_read_all(self, secret_ids: Sequence[UUID]) -> str: ...
    async def read_all_from_each_generation(self, secret_ids: Sequence[UUID]) -> str: ...
    async def snapshot_credentials(self, secret_ids: Sequence[UUID]) -> CredentialSnapshot: ...
    async def stack_and_runtime_snapshot(self) -> StackRuntimeSnapshot: ...
    async def ciphertext_nonce_snapshot(self) -> dict[UUID, tuple[bytes, bytes]]: ...
    async def secret_versions(self) -> set[int]: ...
    async def current_revision(self) -> str: ...
    async def credential_mutation_generation(self) -> int: ...
    async def pre_keyring_rotate_actual_credential(self, secret_id: UUID) -> None: ...
    async def wait_rotation_status(self, status: str, timeout: float = 65.0) -> dict[str, object]: ...
    def runtime_ancestor_metadata(
        self,
    ) -> tuple[tuple[str, str, str, str], ...]: ...
    def runtime_key_metadata(self) -> tuple[str, str, str, str]: ...
    def runtime_key_identity(self) -> RuntimePathIdentity: ...
    def runtime_identity_file(self) -> Path: ...
    def read_runtime_identity(
        self, identity_file: Path | None = None
    ) -> RuntimeKeyIdentitySnapshot: ...
    def snapshot_runtime_identity_for_test(self, output: Path) -> None: ...
    def runtime_identity_orphan_count(self) -> int: ...
    def runtime_leaf_absent(self) -> bool: ...
    def cleanup_runtime_key(self, *, identity_file: Path | None = None) -> None: ...
    def arm_runtime_ancestor_race(
        self, phase: Literal["install", "cleanup"]
    ) -> RuntimeAncestorRaceBarrier: ...

def new_runtime_key_target(project_name: str) -> RuntimeKeyTarget: ...
def open_operator_key_fd(operator_key: Path) -> int: ...
def prepare_runtime_root_argv(target: RuntimeKeyTarget) -> list[str]: ...
def prepare_runtime_leaf_argv(target: RuntimeKeyTarget) -> list[str]: ...
def runtime_key_install_argv(target: RuntimeKeyTarget) -> list[str]: ...
def cleanup_runtime_key_argv(
    target: RuntimeKeyTarget, identity: RuntimeKeyIdentitySnapshot
) -> list[list[str]]: ...
def runtime_key_install_environment() -> dict[str, str]: ...
def invoke_runtime_key_install(
    *, fd: int, target: RuntimeKeyTarget, runner: CommandRunner
) -> SafeCommandResult: ...
def publish_runtime_identity_handoff(
    handoff: Path,
    snapshot: RuntimeKeyIdentitySnapshot,
    *,
    failpoint: RuntimeIdentityFailpoint | None = None,
) -> None: ...
def read_runtime_identity_handoff(path: Path) -> RuntimeKeyIdentitySnapshot: ...
def run_runtime_key_install_cli(
    argv: Sequence[str],
    *,
    runner: CommandRunner,
    target_factory: Callable[[str], RuntimeKeyTarget] = new_runtime_key_target,
) -> SafeCliResult: ...
def run_runtime_key_cleanup_cli(
    argv: Sequence[str],
    *,
    runner: CommandRunner,
    target_factory: Callable[[str], RuntimeKeyTarget] = new_runtime_key_target,
) -> SafeCliResult: ...
def run_pinned_compose_down_cli(
    argv: Sequence[str], *, runner: CommandRunner
) -> SafeCliResult: ...
```

Helpers may print `docker compose ps` and sanitized protected-health JSON on timeout, but never `docker container inspect`, container environments, mounts, key files, database rows containing secret fields, or raw logs. Bounded image/network/volume inspection used only to prove project isolation is routed through the pinned command runner and its output is reduced to allowlisted identifiers before retention. `stdout_fd` is accepted only for the mode-0600 database-backup sink; when supplied, `SafeCommandResult.stdout == ""`, the runner never buffers or decodes those bytes, and the descriptor is closed by its unprivileged caller in `finally`. Every subprocess uses argument arrays, finite timeouts, and `check=False` followed by a safe bounded error.

The harness script exposes `pinned-compose-down --project <validated-name>` only for the EXIT trap. `run_pinned_compose_down_cli` reconstructs the same `ComposeProject` from the already-pinned environment, revalidates the socket snapshot, requires the project argument to equal `JHIN_TEST_COMPOSE_PROJECT`, and calls `project.run_docker_argv(project.cleanup_argv, ...)`; it never starts a raw or differently configured Docker subprocess. Its only outputs are empty success or `compose_teardown_failed`.

- [ ] **Step 4: Write and run the true mixed-version upgrade test RED**

```python
@pytest.mark.integration
async def test_pre_keyring_images_ignore_0017_and_handoff_legacy_v1(
    key_rotation: KeyRotationHarness,
) -> None:
    legacy = key_rotation.private_legacy_v1_file()
    runtime = key_rotation.install_runtime_key(legacy)
    # The invoking UID cannot traverse the root-owned mode-0700 leaf. This
    # projection comes from the exact privileged numeric-stat helper and is
    # sanitized before crossing the command boundary.
    assert key_rotation.runtime_key_metadata() == (
        "uid=10001", "gid=10001", "mode=600", "links=1"
    )
    await key_rotation.migrate_head("0017")
    await key_rotation.start_pre_keyring_services(runtime)
    for service in ("api-pre-keyring", "agent-worker-pre-keyring", "tool-worker-pre-keyring"):
        assert await key_rotation.container_key_stat(service) == (
            "uid=10001", "gid=10001", "mode=600", "reader_uid=10001"
        )
    secret_ids = await key_rotation.seed_credentials_through_pre_keyring_api()
    assert await key_rotation.pre_keyring_read_all(secret_ids) == "ok"
    before_previous_image_mutation = await key_rotation.credential_mutation_generation()
    await key_rotation.pre_keyring_rotate_actual_credential(secret_ids[0])
    assert await key_rotation.credential_mutation_generation() > (
        before_previous_image_mutation
    )
    assert await key_rotation.pre_keyring_read_all(secret_ids) == "ok"

    await key_rotation.start_current_key_services(runtime)
    await key_rotation.wait_exact_reporters(active=1, supported=(1,))
    assert await key_rotation.read_all_from_each_generation(secret_ids) == "ok"
    assert await key_rotation.current_revision() == "0017"

    await key_rotation.stop_pre_keyring_services()
    pre_backup = await key_rotation.verify_backup_pair(stage="pre")
    assert (pre_backup.database_verified, pre_backup.keyring_verified) == (True, True)
    distributed = key_rotation.offline_add_version(legacy, version=2, active=1)
    await key_rotation.recreate_key_services(
        key_rotation.install_runtime_key(distributed)
    )
    await key_rotation.wait_key_gate(RotationStage.DISTRIBUTED)
```

Expected RED command:

```bash
JHIN_RUN_MASTER_KEY_LIVE=1 uv run pytest -m integration tests/integration/test_phase10_keyring_upgrade.py -q
```

Expected: FAIL because previous-image building, overlay services, legacy/current coexistence, and keyring stage helpers are absent. This is a real application-image upgrade, not a unit mock.

The test proves schema rollback by keeping `0017` while previous images work; it never downgrades the shared live database. Old images are stopped before JSON is distributed because they understand only the legacy single-key file.

- [ ] **Step 5: Write and run the complete live rotation test RED**

```python
import os


def assert_exported_runtime_identity_handoff(actual: Path) -> None:
    expected = Path(os.environ["JHIN_RUNTIME_KEY_IDENTITY_HANDOFF"])
    if actual != expected:
        pytest.fail("runtime_identity_handoff_mismatch", pytrace=False)


@pytest.fixture
def outer_trap_runtime_guard(
    key_rotation: KeyRotationHarness,
) -> Iterator[Callable[[Path], None]]:
    operator_key: Path | None = None

    def arm(source: Path) -> None:
        nonlocal operator_key
        if operator_key is not None:
            pytest.fail("outer_trap_guard_armed_twice", pytrace=False)
        operator_key = source

    yield arm

    if operator_key is None:
        pytest.fail("outer_trap_guard_not_armed", pytrace=False)
    stable = key_rotation.runtime_identity_file()
    assert_exported_runtime_identity_handoff(stable)
    previous_handoff_inode = stable.lstat().st_ino
    restored = key_rotation.install_runtime_key_process(operator_key)
    assert (restored.returncode, restored.stdout, restored.stderr) == (
        0, "runtime_key_installed uid=10001 gid=10001 mode=600\n", ""
    )
    assert stable.lstat().st_ino != previous_handoff_inode
    assert key_rotation.read_runtime_identity().runtime_file == (
        key_rotation.runtime_key_identity()
    )
    assert not key_rotation.runtime_leaf_absent()


@pytest.mark.integration
async def test_staged_rotation_survives_restarts_and_retires_old_key(
    key_rotation: KeyRotationHarness,
) -> None:
    pre_backup = await key_rotation.verify_backup_pair(stage="pre")
    assert (pre_backup.database_verified, pre_backup.keyring_verified) == (True, True)
    distributed = key_rotation.distributed_ring(from_version=1, to_version=2)
    await key_rotation.recreate_key_services(
        key_rotation.install_runtime_key(distributed)
    )
    await key_rotation.wait_key_gate(RotationStage.DISTRIBUTED)
    v1 = await key_rotation.seed_credentials(expected_version=1)

    activated = key_rotation.activate_ring(distributed, version=2)
    await key_rotation.recreate_key_services(
        key_rotation.install_runtime_key(activated)
    )
    await key_rotation.wait_key_gate(RotationStage.ACTIVATED)
    v2 = await key_rotation.seed_credentials(expected_version=2)
    before = await key_rotation.snapshot_credentials((*v1, *v2))
    parked = await key_rotation.park_connector_approval()
    assert parked.status == "pending"

    stop_reads = asyncio.Event()
    reads = asyncio.create_task(key_rotation.ordinary_read_loop(stop_reads))
    try:
        first = await key_rotation.run_rotation(max_batches=1)
        assert first.returncode == 75
        assert await key_rotation.secret_versions() == {1, 2}
        await key_rotation.restart("api")
        await key_rotation.restart("agent-worker")
        await key_rotation.restart("tool-worker")
        await key_rotation.wait_key_gate(RotationStage.ACTIVATED)
        final = await key_rotation.run_rotation()
        assert final.returncode == 0
    finally:
        stop_reads.set()
        evidence = await reads

    assert evidence.api_provider_verifications > 0
    assert evidence.agent_model_requests > 0
    assert evidence.tool_connector_calls > 0
    assert evidence.failures == 0
    assert await key_rotation.secret_versions() == {2}
    assert await key_rotation.ciphertext_nonce_snapshot() == before.ciphertext_nonce
    resolved = await key_rotation.approve_and_resolve(parked.approval_id)
    assert resolved.status == "executed"
    assert resolved.external_effect_count == 1
    await key_rotation.wait_key_gate(RotationStage.RETIREMENT_READY)

    backup = await key_rotation.verify_backup_pair(stage="post")
    assert (backup.database_verified, backup.keyring_verified) == (True, True)
    retired = key_rotation.retire_version(activated, version=1)
    armed = await key_rotation.retirement_action("arm")
    assert (armed.returncode, armed.body["state"]) == (0, "armed")
    fence_id = UUID(str(armed.body["fence_id"]))
    for mutation in ("same-id-rotate", "delete", "create"):
        assert await key_rotation.assert_semantic_mutation_fenced(mutation) == (
            "retirement_fence_active"
        )
    refused = await key_rotation.run_rotation()
    assert (refused.returncode, refused.body["safe_error_code"]) == (
        5, "retirement_fence_active"
    )
    await key_rotation.recreate_key_services(
        key_rotation.install_runtime_key(retired)
    )
    await key_rotation.wait_key_gate(RotationStage.RETIRED)
    committed = await key_rotation.retirement_action("commit", fence_id=fence_id)
    assert (committed.returncode, committed.body["state"]) == (0, "committed")
    smoke = await key_rotation.ordinary_secret_read_cycle()
    assert smoke.api_provider_verified
    assert smoke.agent_run_completed
    assert smoke.tool_effect_count == 1


@pytest.mark.integration
@pytest.mark.parametrize("component", ["database", "keyring"])
@pytest.mark.parametrize("stage", ["pre", "post"])
async def test_both_backup_components_are_required_at_each_effect_boundary(
    key_rotation: KeyRotationHarness,
    component: Literal["database", "keyring"],
    stage: Literal["pre", "post"],
) -> None:
    if stage == "pre":
        key_rotation.fail_backup_component_for_test(stage="pre", component=component)
        evidence = await key_rotation.verify_backup_pair(stage="pre")
        assert (evidence.database_verified, evidence.keyring_verified) != (True, True)
        before = await key_rotation.stack_and_runtime_snapshot()
        with pytest.raises(HarnessContractError, match="backup_pair_not_verified"):
            key_rotation.distributed_ring(from_version=1, to_version=2)
        assert await key_rotation.stack_and_runtime_snapshot() == before
        return

    pre = await key_rotation.verify_backup_pair(stage="pre")
    assert (pre.database_verified, pre.keyring_verified) == (True, True)
    await key_rotation.completed_activated_rotation(
        from_version=1, to_version=2, pre_backup=pre
    )
    key_rotation.fail_backup_component_for_test(stage="post", component=component)
    evidence = await key_rotation.verify_backup_pair(stage="post")
    assert (evidence.database_verified, evidence.keyring_verified) != (True, True)
    before = await key_rotation.stack_and_runtime_snapshot()
    with pytest.raises(HarnessContractError, match="backup_pair_not_verified"):
        await key_rotation.retirement_action("arm")
    assert await key_rotation.stack_and_runtime_snapshot() == before


@pytest.mark.integration
async def test_failed_retirement_cutover_restores_dual_ring_before_cancel(
    key_rotation: KeyRotationHarness,
) -> None:
    pre_backup = await key_rotation.verify_backup_pair(stage="pre")
    assert (pre_backup.database_verified, pre_backup.keyring_verified) == (True, True)
    activated = await key_rotation.completed_activated_rotation(
        from_version=1, to_version=2, pre_backup=pre_backup
    )
    retired = key_rotation.retire_version(activated, version=1)
    post_backup = await key_rotation.verify_backup_pair(stage="post")
    assert (post_backup.database_verified, post_backup.keyring_verified) == (True, True)
    armed = await key_rotation.retirement_action("arm")
    fence_id = UUID(str(armed.body["fence_id"]))

    await key_rotation.recreate_key_services(
        key_rotation.install_runtime_key(retired),
        fail_service="tool-worker",
    )
    failed_commit = await key_rotation.retirement_action("commit", fence_id=fence_id)
    assert (failed_commit.returncode, failed_commit.body["safe_error_code"]) == (
        3, "replica_gate_closed"
    )
    assert await key_rotation.assert_semantic_mutation_fenced("create") == (
        "retirement_fence_active"
    )

    await key_rotation.recreate_key_services(
        key_rotation.install_runtime_key(activated)
    )
    await key_rotation.wait_key_gate(RotationStage.ACTIVATED)
    cancelled = await key_rotation.retirement_action("cancel", fence_id=fence_id)
    assert (cancelled.returncode, cancelled.body["state"]) == (0, "cancelled")
    assert (await key_rotation.run_rotation()).returncode == 0


@pytest.mark.integration
async def test_hostile_repository_dotenv_is_not_loaded_by_real_compose(
    key_rotation: KeyRotationHarness,
) -> None:
    probe = await key_rotation.render_hostile_repository_dotenv_probe(
        dotenv=(
            "PHASE10_DOTENV_IMAGE=registry.invalid/HOSTILE_DOTENV_IMAGE\n"
            "COMPOSE_PROFILES=hostile-profile\n"
            "COMPOSE_FILE=/tmp/HOSTILE_DOTENV_COMPOSE_FILE\n"
            "DOCKER_HOST=tcp://remote.invalid:2376\n"
        ),
        compose=(
            'services:\n  probe:\n    image: "${PHASE10_DOTENV_IMAGE:-'
            'local.invalid/jhin-dotenv-probe:fixed}"\n'
        ),
    )
    assert probe.image == "local.invalid/jhin-dotenv-probe:fixed"
    assert probe.project_name == key_rotation.project.name
    assert probe.profiles == ()
    assert probe.command_env["COMPOSE_DISABLE_ENV_FILE"] == "1"
    assert probe.command_env["DOCKER_HOST"] == (
        f"unix://{key_rotation.project.docker_socket.host_path}"
    )
    assert "HOSTILE_DOTENV" not in json.dumps(probe.allowlisted_render)


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["install", "cleanup"])
@pytest.mark.parametrize("attack", ["rename", "symlink"])
async def test_runtime_key_ancestor_swap_cannot_redirect_privilege(
    key_rotation: KeyRotationHarness,
    tmp_path: Path,
    phase: Literal["install", "cleanup"],
    attack: Literal["rename", "symlink"],
    outer_trap_runtime_guard: Callable[[Path], None],
) -> None:
    operator_key = key_rotation.private_legacy_v1_file()
    outer_trap_runtime_guard(operator_key)
    key_rotation.install_runtime_key(operator_key)
    assert key_rotation.runtime_ancestor_metadata() == (
        ("filesystem-root", "uid=0", "gid=0", "user_writable=false"),
        ("run", "uid=0", "gid=0", "user_writable=false"),
        ("fixed-root", "uid=0", "gid=0", "user_writable=false"),
        ("random-leaf", "uid=0", "gid=0", "user_writable=false"),
    )
    outside = tmp_path / "outside-canary"
    outside.write_bytes(b"outside-bytes-must-not-change")
    before_stat = outside.lstat()
    before_identity = (
        before_stat.st_dev, before_stat.st_ino, before_stat.st_uid,
        before_stat.st_gid, stat.S_IMODE(before_stat.st_mode), before_stat.st_nlink,
    )
    barrier = key_rotation.arm_runtime_ancestor_race(phase)
    if phase == "install":
        operation = lambda: key_rotation.install_runtime_key(operator_key)
    else:
        operation = key_rotation.cleanup_runtime_key
    task = asyncio.create_task(asyncio.to_thread(operation))
    await barrier.arrived.wait()
    with pytest.raises(PermissionError):
        barrier.attempt_unprivileged_swap(outside=outside, attack=attack)
    barrier.release.set()
    await task
    after_stat = outside.lstat()
    assert (
        after_stat.st_dev, after_stat.st_ino, after_stat.st_uid,
        after_stat.st_gid, stat.S_IMODE(after_stat.st_mode), after_stat.st_nlink,
    ) == before_identity
    assert outside.read_bytes() == b"outside-bytes-must-not-change"
    if phase == "install":
        assert key_rotation.runtime_key_metadata() == (
            "uid=10001", "gid=10001", "mode=600", "links=1"
        )
    else:
        assert key_rotation.runtime_leaf_absent()


@pytest.mark.integration
async def test_replacement_publishes_latest_receipt_for_separate_cleanup(
    key_rotation: KeyRotationHarness,
    tmp_path: Path,
    outer_trap_runtime_guard: Callable[[Path], None],
) -> None:
    operator_key = key_rotation.private_legacy_v1_file()
    outer_trap_runtime_guard(operator_key)
    stable = key_rotation.runtime_identity_file()
    assert_exported_runtime_identity_handoff(stable)
    first = key_rotation.install_runtime_key_process(operator_key)
    assert (first.returncode, first.stdout, first.stderr) == (
        0, "runtime_key_installed uid=10001 gid=10001 mode=600\n", ""
    )
    first_receipt = key_rotation.read_runtime_identity()
    assert first_receipt.runtime_file == key_rotation.runtime_key_identity()
    stale_receipt = tmp_path / "stale-runtime-identity.json"
    key_rotation.snapshot_runtime_identity_for_test(stale_receipt)
    first_handoff_inode = stable.lstat().st_ino

    # This is a direct GNU-install replacement. There is deliberately no
    # cleanup between the two separate installer processes.
    replacement = key_rotation.install_runtime_key_process(operator_key)
    assert (replacement.returncode, replacement.stdout, replacement.stderr) == (
        0, "runtime_key_installed uid=10001 gid=10001 mode=600\n", ""
    )
    latest = key_rotation.read_runtime_identity()
    assert_exported_runtime_identity_handoff(key_rotation.runtime_identity_file())
    assert latest.runtime_file == key_rotation.runtime_key_identity()
    assert latest.runtime_file.inode != first_receipt.runtime_file.inode
    assert stable.lstat().st_ino != first_handoff_inode
    assert key_rotation.runtime_identity_orphan_count() == 0

    outside = tmp_path / "outside-two-process-canary"
    outside.write_bytes(b"outside-two-process-bytes")
    outside_before = outside.lstat()
    stale = key_rotation.cleanup_runtime_key_process(identity_file=stale_receipt)
    assert (stale.returncode, stale.stdout, stale.stderr) == (
        1, "", "runtime_key_cleanup_rejected\n"
    )
    assert key_rotation.runtime_key_identity() == latest.runtime_file
    outside_after = outside.lstat()
    assert (
        outside_after.st_dev, outside_after.st_ino, outside_after.st_mode,
        outside_after.st_uid, outside_after.st_gid, outside_after.st_nlink,
    ) == (
        outside_before.st_dev, outside_before.st_ino, outside_before.st_mode,
        outside_before.st_uid, outside_before.st_gid, outside_before.st_nlink,
    )
    assert outside.read_bytes() == b"outside-two-process-bytes"

    # A separately launched cleanup reads the latest receipt from the same
    # stable handoff that Make exported before installing its trap.
    current = key_rotation.cleanup_runtime_key_process(identity_file=stable)
    assert (current.returncode, current.stdout, current.stderr) == (
        0, "runtime_key_cleaned\n", ""
    )
    assert key_rotation.runtime_leaf_absent()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failpoint", "stable_generation", "orphan_count"),
    [
        ("after-runtime-install-before-identity-write", "old", 0),
        ("after-identity-sibling-fsync-before-replace", "old", 1),
        ("after-identity-replace-before-directory-fsync", "new", 0),
    ],
)
async def test_identity_handoff_process_crash_is_atomic_and_cleanup_is_closed(
    key_rotation: KeyRotationHarness,
    failpoint: RuntimeIdentityFailpoint,
    stable_generation: Literal["old", "new"],
    orphan_count: int,
    outer_trap_runtime_guard: Callable[[Path], None],
) -> None:
    operator_key = key_rotation.private_legacy_v1_file()
    outer_trap_runtime_guard(operator_key)
    stable = key_rotation.runtime_identity_file()
    assert_exported_runtime_identity_handoff(stable)
    assert key_rotation.install_runtime_key_process(operator_key).returncode == 0
    old = key_rotation.read_runtime_identity()

    crashed = key_rotation.install_runtime_key_process(
        operator_key, failpoint=failpoint
    )
    assert (crashed.returncode, crashed.stdout, crashed.stderr) == (97, "", "")
    current_runtime = key_rotation.runtime_key_identity()
    assert current_runtime.inode != old.runtime_file.inode
    published = key_rotation.read_runtime_identity()
    if stable_generation == "old":
        assert published == old
    else:
        assert published != old
        assert published.runtime_file == current_runtime
    assert key_rotation.runtime_identity_orphan_count() == orphan_count

    cleanup = key_rotation.cleanup_runtime_key_process(identity_file=stable)
    if stable_generation == "new":
        assert published.runtime_file == current_runtime
        assert (cleanup.returncode, cleanup.stdout, cleanup.stderr) == (
            0, "runtime_key_cleaned\n", ""
        )
        assert key_rotation.runtime_leaf_absent()
    else:
        assert published.runtime_file == old.runtime_file
        assert (cleanup.returncode, cleanup.stdout, cleanup.stderr) == (
            1, "", "runtime_key_cleanup_rejected\n"
        )
        assert key_rotation.runtime_key_identity() == current_runtime
        recovered = key_rotation.install_runtime_key_process(operator_key)
        assert recovered.returncode == 0
        assert key_rotation.read_runtime_identity().runtime_file == (
            key_rotation.runtime_key_identity()
        )
        final_cleanup = key_rotation.cleanup_runtime_key_process(identity_file=stable)
        assert final_cleanup.returncode == 0
        assert key_rotation.runtime_leaf_absent()
```

`outer_trap_runtime_guard` is mandatory on every live case that directly invokes cleanup, including both ancestor-race phases so parametrization cannot accidentally omit the cleanup case. Because it depends on `key_rotation`, pytest runs this guard's finalizer before the underlying harness fixture can release its state. The finalizer runs after the test's intentional missing-leaf assertion, calls a fresh installer subprocess with the same unprivileged operator key, and therefore recreates the root-owned leaf/runtime inode and atomically replaces the stable handoff receipt. It requires the handoff inode to change, the new receipt's runtime identity to equal privileged numeric metadata, and `runtime_leaf_absent()` to be false. The finalizer never weakens cleanup itself: the unit test above calls cleanup a second time while the leaf is missing and still requires `runtime_key_cleanup_rejected` with no additional destructive call. Thus only the outer Make EXIT trap performs the final successful cleanup of the restored runtime tree.

The ordinary loop uses product APIs and fake provider/connector effects: API provider/connection verification, an agent task model request, and a tool-worker connector call. It never invokes a test-only decrypt endpoint. The parked connector approval is requested before any wrapper update and resolved after completion; its one effect proves master-key-stable credential revision, while a second live parked approval followed by the existing credential-rotation API is denied with zero effects. `verify_backup_pair(stage="pre")` independently validates a restorable database backup and a separately protected keyring backup before `distributed_ring`, `offline_add_version`, or `completed_activated_rotation` may distribute a second key; either false result aborts with no service/file change. `verify_backup_pair(stage="post")` repeats both checks after rotation and before each live `retirement_action("arm")`; its bounded `BackupEvidence` is bound to the current project, requested stage, credential generation, and supported keyring versions, and the harness refuses distribution/arm when either component is absent, false, or stale. The retired offline file is also created before arming. After `arm` returns, semantic writes and a new rotation are rejected for the entire service-file/restart gap. Only exact fresh retired reporters allow the separate `commit` invocation. The failure test proves a partial cutover cannot commit, the fence stays armed, the dual activated file/reporters must be restored, and only then may `cancel` reopen writes. Task 5 runner/CLI unit and real-PG tests intentionally exercise database authority below this host acceptance layer; all executable distribution/arm paths in the mixed/live harness and runbook require the paired evidence. No database session/lock crosses the service restart.

For this acceptance harness (not general backup tooling), database verification appends `("exec", "-T", "postgres", "pg_dump", "-U", "postgres", "--format=custom", project.database_name)` to `project.argv(...)` and streams the pinned runner's binary stdout directly into a new invoking-user mode-0600 file without retaining it in `SafeCommandResult`, checks a SHA-256 internally, restores it into `f"{project.database_name}_verify_{stage}"` in the same disposable project, and compares allowlisted schema revision plus secret row/version counts before dropping that verification database. Keyring verification independently copies the current operator ring through two unprivileged `O_NOFOLLOW` descriptors into a distinct mode-0600 fixture file, loads it with the production parser, compares active/supported versions, and decrypts one allowlisted credential probe from the matching database snapshot without retaining plaintext. The two checks have separate failure injection points; success returns no paths, hashes, material, dump bytes, DSN, or plaintext. The pre evidence is consumed exactly once by the first distribution helper; the post evidence is invalidated by any later credential-generation, keyring-version, or project change and is consumed exactly once by arm.

Add separate live cases: one reporter left distributed closes activation; one reporter left activated closes retired/commit; future-dated reporter closes every gate/action; source row/same-ID credential rotation/delete/create after completion changes the scalar and blocks both preview and arm until a new full verification; 1->2 completes, a fallback v1 row appears, a later attempt aborts, and retirement stays closed until a newer completion; wrong/stale fence UUID and expired fence cannot commit; partial run then `--abort` preserves readable mixed rows; pre-retirement rollback activates v1 while supporting `(1,2)` and both row versions remain usable; advisory contention creates no second state/audit. On native Linux, `install_runtime_key_for_mode_test(..., mode=0o640)` uses the same invoking-user `O_NOFOLLOW` source FD, root-owned fixed ancestor chain, `/proc/self/fd/0` stdin source, exact target, and post-install validation, but its closed test-only argv uses `install -m 0640`; it accepts only that literal mode and never runs a separate privileged permission mutation. Under the isolated harness's explicit `APP_ENV=test`, recreate the key-bearing services and prove the API's permitted test-only degradation remains up with sanitized `master_key_unavailable`, agent/tool initialization emits only `master_key_file_unsafe`, and reinstalling a fresh UID-10001/mode-0600 runtime copy restores all three. Separately, Task 4's production subprocess matrix must fail startup; this negative mode fixture is not a production degradation policy or delivery strategy.

Run RED:

```bash
JHIN_RUN_MASTER_KEY_LIVE=1 uv run pytest -m integration tests/integration/test_phase10_master_key_rotation.py -q
```

Expected: FAIL until the live helper, fully pinned Docker authority with explicit env-file disable, hostile repository `.env` probe, root-anchored stdin/FD install, atomic stable cross-process identity handoff and process-crash failpoints, stale-receipt/ancestor-race barriers, post-cleanup finalizer restoration, exact outer-trap success assertion, paired backup acceptance, Compose wiring, mutation-generation probes, CLI image availability, durable retirement arm/commit/cancel handoff, and exact staged/recovery behavior are implemented.

- [ ] **Step 6: Implement the tested lifecycle helpers, add Make/CI gates, and run GREEN**

Implement the Step 3 target methods now, using the already-failing upgrade/live tests as the contract. Add `test-master-key-rotation` (unit/real-PG modules) and `test-master-key-rotation-integration` to `.PHONY`. The live target is Linux-only and fails rather than skips when CI requests it. It creates one invoking-user-owned `mktemp -d` only for nonprivileged offline keyring outputs, the stable mode-0600 nonsecret identity handoff and its exclusive atomic-publication siblings, an empty harness `DOCKER_CONFIG`, and test artifacts; no privileged path descends from it. It also creates a validated unique `JHIN_TEST_COMPOSE_PROJECT` and corresponding random root-owned leaf beneath fixed `/run/jhin-key-rotation`. Export the handoff path and install the trap before either privileged runtime preparation or any Docker call. Before build/up, open the operator legacy key unprivileged with `O_NOFOLLOW` and install from `/proc/self/fd/0` into the exact root-owned runtime target through the bounded UID-10001 procedure above, then fsync and atomically publish the validated numeric identity receipt before reporting success. Every later GNU-install replacement publishes a fresh receipt through the same handoff; neither Make nor pytest retains a different current-receipt variable.

Run `validated_docker_socket` before constructing any Docker environment: it disregards inherited `SANDBOX_DOCKER_SOCKET_HOST`/`SANDBOX_DOCKER_GID`, scrubs every Docker context/host/TLS/certificate/header/config and Compose profile/file/project/env-file authority variable, validates and pins the fixed resolved `/var/run/docker.sock` Unix-socket inode, and derives the positive GID from that same snapshot. Set exact `COMPOSE_DISABLE_ENV_FILE=1`, `DOCKER_HOST=unix://<validated socket>`, and the empty private `DOCKER_CONFIG`; never leave automatic `.env` loading to an unset/default value. `ComposeProject.run_docker_argv` receives that same environment and revalidates the socket snapshot for every Compose/build/image/network/volume/stat/port/hostile-`.env`-render/teardown command. The harness fails closed if any method tries a raw Docker subprocess or if the socket changes. Set all dev published ports to `0` and set `MASTER_KEY_FILE_HOST` only to the exact runtime path.

Run project-scoped `build` first, without starting a container. Then run three command-overridden `docker compose run --rm --no-deps` probes using those final service image users. Each asserts effective UID/GID `10001:10001`, mounted key stat `10001:10001/0600`, calls the real production loader, asserts `(active,supported) == (1,(1,))`, and prints only its exact line: `keyring_preflight_ok service=api`, `keyring_preflight_ok service=agent-worker`, or `keyring_preflight_ok service=tool-worker`. Only after all probes pass, run infrastructure `up -d --wait`, `run --rm --no-deps api jhin-db-migrate`, and full `up -d --build --wait`. Discover dynamic API/PostgreSQL ports with `compose port`; never use defaults `8000`/`55432` or another running stack's DSN. The live pytest process receives the exact project, loopback endpoints, and `JHIN_RUN_MASTER_KEY_LIVE=1`.

The EXIT trap preserves the incoming pytest status and always invokes the exact project-scoped `down -v --remove-orphans` through the same pinned Docker environment before it starts the separate cleanup CLI with `--identity-file "$JHIN_RUNTIME_KEY_IDENTITY_HANDOFF"`; it never holds the initial receipt object in shell state. Cleanup therefore reopens the stable handoff after any number of replacements, derives the target from the validated project, matches current privileged numeric metadata to that latest receipt, removes only the exact file with `sudo -n rm -- <target>`, revalidates, and removes the exact empty random leaf with `sudo -n rmdir -- <leaf>`. It never removes the fixed root or trusts receipt data as a path. The trap captures only the bounded cleanup result, requires exit zero plus exact `runtime_cleanup_output=runtime_key_cleaned`, emits `outer_trap_cleanup_ok`, and only then removes the nonprivileged fixture directory, including any non-authoritative `.next` left by a killed publisher. A missing/stale/malformed runtime identity, ownership, type, or emptiness mismatch remains a fail-closed cleanup result; the trap suppresses that inner output, emits only `outer_trap_cleanup_failed`, returns nonzero, and leaves the privileged leaf and stable receipt for bounded manual inspection. It never converts a missing runtime path to success or falls back to recursive/glob cleanup. The live command captures the target output and explicitly requires exactly one `outer_trap_cleanup_ok` and no `outer_trap_cleanup_failed`/`runtime_key_cleanup_rejected`. Assert after teardown, through the same pinned runner, that project containers, networks, and volumes are absent and that the root-owned random leaf is absent. The invoking user's operator key and backup are never opened by root or removed. No command uses the default project name, a fixed port, a second Docker environment, a user-writable privileged ancestor, recursive privilege, a glob, or a shared volume.

Use this exact status-preserving core in the single-shell Make recipe; the already-pinned environment supplies `DOCKER_HOST`, `DOCKER_CONFIG`, `COMPOSE_DISABLE_ENV_FILE`, socket/GID, and zero-port values to the harness-owned Compose teardown:

```makefile
outer_cleanup() { \
  incoming_status=$$?; \
  set +e; \
  uv run python tests/integration/phase10_key_rotation_harness.py \
    pinned-compose-down \
    --project "$$JHIN_TEST_COMPOSE_PROJECT" >/dev/null 2>&1; \
  teardown_status=$$?; \
  runtime_cleanup_output="$$(uv run python \
    tests/integration/phase10_key_rotation_harness.py cleanup-runtime-key \
    --project "$$JHIN_TEST_COMPOSE_PROJECT" \
    --identity-file "$$JHIN_RUNTIME_KEY_IDENTITY_HANDOFF" 2>&1)"; \
  runtime_cleanup_status=$$?; \
  if [ "$$teardown_status" -ne 0 ] || \
     [ "$$runtime_cleanup_status" -ne 0 ] || \
     ! test "$$runtime_cleanup_output" = "runtime_key_cleaned"; then \
    printf '%s\n' outer_trap_cleanup_failed >&2; \
    exit 1; \
  fi; \
  find "$$fixture_dir" -xdev -depth -delete >/dev/null 2>&1; \
  if [ "$$?" -ne 0 ]; then \
    printf '%s\n' outer_trap_cleanup_failed >&2; \
    exit 1; \
  fi; \
  printf '%s\n' outer_trap_cleanup_ok; \
  exit "$$incoming_status"; \
}; \
trap outer_cleanup EXIT
```

CI adds a required `master-key-rotation-linux` job on `ubuntu-24.04` after Python/Docker jobs with PostgreSQL 17 and Docker. Its `actions/checkout@v4` uses `fetch-depth: 0` so the committed pre-keyring ancestor is available and verifies that ref is an ancestor before building. The job exercises only the exact `sudo -n install -d`, stdin-fed `sudo -n install -m 0600 -o 10001 -g 10001`, numeric-stat, exact-file `sudo -n rm`, and empty-leaf `sudo -n rmdir` constructors; it never grants or tests privilege on the operator source path. It runs the focused unit/migration/PostgreSQL tests plus the isolated mixed-version/live target, the real hostile repository `.env` render, bounded PostgreSQL lock/statement-wait matrix, fenced retirement handoff/recovery, all four deterministic install/cleanup × rename/symlink ancestor-race cases, direct install→replacement with no intermediate cleanup followed by successful separate-process cleanup from the shared stable handoff, copied-stale-receipt rejection, and all three `os._exit(97)` identity-publication crash boundaries. The crash matrix proves pre-replace deaths leave a complete old receipt that makes cleanup refuse without effects, post-replace death leaves a complete matching new receipt that permits cleanup, `.next` is never authority, and an ordinary reinstall atomically republishes recovery authority. It asserts `uname -s == Linux`, every privileged ancestor is root-owned/non-user-writable, service UID 10001, exact runtime-file numeric ownership through sanitized privileged metadata (never invoking-user `Path.stat()` beneath the 0700 leaf), stable-handoff replacement and stale-receipt preservation of the current tree/outside referent, pinned socket path/inode/GID and `DOCKER_HOST`, `COMPOSE_DISABLE_ENV_FILE=1` on every captured Docker call, hostile inherited authority/profile variables absent, hostile `.env` interpolation ignored, dynamic ports, project-local DSN, independently verified pre/post database+keyring backups, disposable volumes, and clean teardown. It uploads only JUnit/sanitized Compose-ps artifacts; never keyrings, identity receipts, `.env`, database dumps, container/image/network inspection output, service logs, DSNs, privileged target paths, or temp paths.

For every live test that intentionally removes the runtime leaf, the job observes the cleanup assertion first and the fixture finalizer's fresh atomic reinstall second. The enclosing Make process remains the sole final cleanup authority. Its private captured output must contain exactly one `outer_trap_cleanup_ok` and neither `outer_trap_cleanup_failed` nor `runtime_key_cleanup_rejected`; this proves the actual EXIT trap, not merely a harness helper, removed the restored tree. The separate unit missing-path call still returns `runtime_key_cleanup_rejected` and records no extra destructive command, so the success gate does not make cleanup idempotent.

```bash
uv run pytest tests/test_phase10_master_key_rotation_harness.py -q
make test-master-key-rotation
(
  live_gate_output="$(mktemp)"
  trap 'rm -f "$live_gate_output"' EXIT
  if ! make test-master-key-rotation-integration >"$live_gate_output" 2>&1; then
    exit 1
  fi
  test "$(rg -c '^outer_trap_cleanup_ok$' "$live_gate_output")" -eq 1
  ! rg -n '^(outer_trap_cleanup_failed|runtime_key_cleanup_rejected)$' "$live_gate_output"
)
uv run ruff check tests/integration/phase10_key_rotation_harness.py tests/integration/test_phase10_keyring_upgrade.py tests/integration/test_phase10_master_key_rotation.py tests/test_phase10_master_key_rotation_harness.py
uv run mypy tests/integration/phase10_key_rotation_harness.py tests/integration/test_phase10_keyring_upgrade.py tests/integration/test_phase10_master_key_rotation.py
git add tests/integration/phase10_key_rotation_harness.py tests/integration/compose.phase10-keyring-upgrade.yaml tests/integration/test_phase10_keyring_upgrade.py tests/integration/test_phase10_master_key_rotation.py tests/test_phase10_master_key_rotation_harness.py tests/integration/conftest.py Makefile .github/workflows/ci.yml
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "test: prove staged master key rotation"
```

Expected cached names: exactly the eight paths in `git add`. The capture generator/test/ref were already committed in Task 1 and are read-only here.

### Task 9: Publish the Staged Runbook and Execute the Final Release Gate

**Files:**
- Create: `docs/operations/master-key-rotation.md`
- Create: `apps/web/public/runbooks/master-key-rotation.md`
- Modify: `docs/operations/protected-health.md`
- Modify: `README.md`
- Create: `tests/test_master_key_rotation_docs.py`
- Modify: `apps/web/app/(app)/operations/page.tsx`
- Modify: `apps/web/tests/operations-page.test.tsx`

**Interfaces:**
- Consumes: the complete system and its exact CLI/gate names.
- Produces: the operator protocol, recovery/rollback/retirement rules, a now-valid Operations link, and final repository-wide verification/staging evidence.

- [ ] **Step 1: Write a documentation contract that parses every required command/stage**

```python
def test_runbook_has_exact_ordered_gates_and_no_secret_printing() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    ordered = [
        "verify database backup",
        "verify key-file backup",
        "jhin-master-keyring add",
        "--check-stage distributed",
        "jhin-master-keyring activate",
        "--check-stage activated",
        "jhin-master-key-rotate --from 1 --to 2",
        "--check-stage retirement-ready",
        "verify post-rotation database backup",
        "verify post-rotation keyring backup",
        "jhin-master-keyring retire",
        "--retirement-action arm",
        "--check-stage retired",
        "--retirement-action commit",
        "credential-use smoke test",
    ]
    offsets = [text.index(item) for item in ordered]
    assert offsets == sorted(offsets)
    for forbidden in ("cat $", "cat /run/secrets", "echo $MASTER", "set -x", "docker inspect"):
        assert forbidden not in text


def test_runbook_documents_abort_and_both_rollback_boundaries() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "--abort" in text
    assert "does not reverse committed rows" in text
    assert "before old-key retirement" in text
    assert "select version 1 active and retain versions 1 and 2" in text
    assert "after old-key retirement" in text
    assert "restore the separately protected old-key backup" in text
    assert "never restore a database backup without its matching keyring backup" in text


def test_runbook_documents_root_anchored_fd_delivery_and_generation() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "root-owned non-user-writable" in text
    assert "O_NOFOLLOW" in text
    assert "/proc/self/fd/0" in text
    assert "sudo -n install -m 0600 -o 10001 -g 10001" in text
    assert "sudo -n rm --" in text
    assert "sudo -n rmdir --" in text
    assert "--identity-output" in text
    assert "--identity-file" in text
    assert "numeric identity receipt" in text
    assert "JHIN_RUNTIME_KEY_IDENTITY_HANDOFF" in text
    assert "exclusive sibling" in text
    assert "atomically replace the stable handoff" in text
    assert "fsync the handoff directory" in text
    assert "a stale receipt makes cleanup refuse" in text
    assert "old or new complete receipt" in text
    assert "outer_trap_cleanup_ok" in text
    assert "a missing runtime path remains a cleanup failure" in text
    assert "root never opens the operator source path" in text
    assert "an ancestor rename or symlink attack cannot redirect privilege" in text
    assert "sudo chown" not in text
    assert "rm -r" not in text
    assert "credential-mutation generation" in text
    assert "rolled-back generation gap may require harmless reverification" in text
    assert "A future-dated heartbeat closes the gate" in text
    assert "/run/secrets/jhin_master_key" in text


def test_runbook_documents_fenced_handoff_timeouts_and_compose_env_authority() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "retirement-ready is a preview, not cutover authority" in text
    assert "--retirement-action arm" in text
    assert "--retirement-action commit" in text
    assert "--retirement-action cancel" in text
    assert "--fence-id" in text
    assert "the durable fence remains armed" in text
    assert "no database lock is held while services restart" in text
    assert "lock_timeout=5000ms" in text
    assert "statement_timeout=30000ms" in text
    assert "35-second client deadline" in text
    assert "COMPOSE_DISABLE_ENV_FILE=1" in text
    assert "repository .env" in text


def test_operations_link_target_exists_and_readme_indexes_runbook() -> None:
    assert RUNBOOK.is_file()
    public_copy = ROOT / "apps/web/public/runbooks/master-key-rotation.md"
    assert public_copy.read_bytes() == RUNBOOK.read_bytes()
    assert "docs/operations/master-key-rotation.md" in README.read_text(encoding="utf-8")
    page = OPERATIONS_PAGE.read_text(encoding="utf-8")
    assert 'href="/runbooks/master-key-rotation.md"' in page
```

Add assertions for the separate operator key versus UID-10001/mode-0600 runtime copy, invoking-user `O_NOFOLLOW` source open, stdin/FD-only exact-target privileged install beneath fixed root-owned ancestors, the one stable bounded numeric identity handoff shared by Make/pytest/separate cleanup, exclusive-sibling write+fsync+atomic replace+directory fsync on every replacement, old-or-new complete crash semantics, stale-receipt substitution rejection, exact file/empty-leaf cleanup, install/cleanup ancestor-race failure, no recursive privilege/no inline production material, isolated `-p`/dynamic ports/disposable volumes, the exact pinned local Docker socket/GID/`DOCKER_HOST` and `COMPOSE_DISABLE_ENV_FILE=1` with inherited context/TLS/profile/env-file authority scrubbed for every call plus hostile repository `.env` rejection, exact three services and the closed 30-second-to-checked-at window, no stale/future row granting authority, exact acquisition and transaction 5000ms/30000ms/35-second database wait bounds and closed outcomes, database `clock_timestamp()` reporter/fence authority despite host skew, final arm/cancel reporter rechecks, latest-completed scalar generation plus zero-source/every-fresh retirement, independently verified database+keyring backup pairs before distribution and before arm, preview versus armed handoff authority, arm/commit/cancel UUID protocol and fail-closed expiry, no database lock held across service work, no ciphertext/nonce/timestamp rewrite, bounded/resumable behavior, expected safe exit codes, mixed-row abort recovery, workspace-scoped protected-health fields with no global counters/generation/fence fields, audit actions, wrapper-stable parked approvals, legacy file support limited to first release, and removal of legacy parser only in a separately planned later release.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_master_key_rotation_docs.py -q
pnpm --filter jhin-web test -- operations-page.test.tsx
```

Expected: FAIL because the runbook and valid link do not exist and README/protected-health do not describe rotation.

- [ ] **Step 3: Write the exact safe operator protocol**

The runbook must use separate input/output files for offline edits and controlled replacement only after backup. Commands use paths as shell variables but never print file contents. It distinguishes the invoking-user-owned operator keyring/backup from the installed runtime copy. On native Linux, the deployment primitive first opens the operator source as the invoking user with `O_NOFOLLOW`, verifies the open descriptor is a bounded regular single-link file, and never gives that source pathname to root. The privileged destination has no user-writable ancestor: `/run` and fixed `/run/jhin-key-rotation` are root-owned, the per-rollout random leaf is root-owned mode `0700`, and the exact file is beneath that leaf. After closed constant/regex and numeric-stat validation, the only material-bearing command is `sudo -n install -m 0600 -o 10001 -g 10001 -- /proc/self/fd/0 "$runtime_key"`, with the already-open descriptor connected to stdin; root never opens the operator source path and key bytes enter no argv/environment/output. Before the Make trap is installed, set and export the single `JHIN_RUNTIME_KEY_IDENTITY_HANDOFF` in the invoking-user fixture directory. Every initial install, replacement, and rollback supplies that same path through `--identity-output`; the runbook requires the installer to write and fsync a mode-0600 exclusive sibling, atomically replace the stable handoff, and fsync the handoff directory before success. The receipt is bounded nonsecret numeric metadata created only after privileged validation; pytest and the cleanup process reopen the same stable path rather than retaining an initial receipt.

Cleanup occurs after project teardown in a separate process and requires `--identity-file "$JHIN_RUNTIME_KEY_IDENTITY_HANDOFF"`. It derives the target from the fixed root and validated project, requires current privileged numeric metadata to equal the latest stable receipt, and only then uses `sudo -n rm -- "$runtime_key"` followed by revalidation and `sudo -n rmdir -- "$runtime_leaf"`; it does not remove the fixed parent or accept a receipt-provided path. It states that an ancestor rename or symlink attack cannot redirect privilege because the complete privileged destination chain is root-owned non-user-writable, that a stale receipt makes cleanup refuse and cannot delete a same-project replacement, and that any receipt/identity/ownership/emptiness mismatch fails closed for bounded privileged inspection. If install dies before stable replacement, cleanup sees the old complete receipt and refuses the new runtime inode; after replacement it sees the new complete receipt and may proceed only when metadata matches. After a machine crash the handoff is an old or new complete receipt, never partial authority; reinstall republishes a matching receipt. A leftover exclusive sibling is non-authoritative and is removed only with the nonprivileged fixture after privileged cleanup succeeds. The isolated evidence section must say that tests performing intentional cleanup reinstall and republish before fixture finalization, the sole outer EXIT trap then requires exact `outer_trap_cleanup_ok`, and a missing runtime path remains a cleanup failure rather than idempotent success. It explicitly forbids `sudo chown`, recursive removal, globs, root opening/copying an operator source pathname, using the operator backup as the mount target, or claiming `chmod 600` changes ownership. The fixed `/run/secrets/jhin_master_key` container target and exact configured Compose source path may appear in configuration; key material, inline values, unexpected/sensitive host paths, and all path-bearing errors/output remain forbidden. It then distinguishes:

1. Verify separate PostgreSQL and old-key-file backups before generating or distributing v2; both verifications must pass and their checksums stay in the protected operator system, not application logs.
2. Deploy current code to every API/agent/tool replica while the existing legacy/raw v1 file is unchanged; prove exact `(1,(1,))` heartbeat support and ordinary reads. Drain all pre-keyring images.
3. Offline-add v2, keep v1 active, install the separate `10001:10001/0600` runtime copy, run each container-UID read preflight, restart all three services, and run `--check-stage distributed` until exact `(1,(1,2))` everywhere.
4. Offline-activate v2, restart all three, and run `--check-stage activated` until exact `(2,(1,2))`; only now can rewrap start.
5. Run `jhin-master-key-rotate --from 1 --to 2 --batch-size 100`; exit 75 means safely resume the same command. Monitor protected counts and ordinary credential-use probes.
6. On failure, keep both keys. `--abort` stops new batches without reversal. Before retirement, rollback means activate v1 but support both keys; it does not convert v2 rows back.
7. Require the latest 1->2 attempt completed, no later active/aborted attempt, its stored credential-mutation generation equal to the current scalar, zero v1/unexpected rows, exact activated reporters, and `--check-stage retirement-ready`; state explicitly that retirement-ready is a preview, not cutover authority. A same-ID credential update/delete/create or rolled-back generation gap requires harmless reverification. Independently verify the post-rotation database backup and post-rotation keyring backup, requiring both before any arm command, then generate the offline retired-v1 output without installing it.
8. Run `jhin-master-key-rotate --from 1 --to 2 --retirement-action arm`; record its nonsecret fence UUID from the protected operator output. Advisory acquisition itself installs and verifies `lock_timeout=5000ms` plus `statement_timeout=30000ms` inside a 35-second client deadline before its first authority query. Arm locks the state and both authority tables, obtains `clock_timestamp()` from PostgreSQL, and revalidates the latest attempt, mutation generation, source/unexpected rows, and exact fresh activated reporters immediately before the mutation. After it commits, no database lock is held while services restart; the durable fence remains armed and rejects semantic credential writes/new rotations. Install the already-generated retired ring, restart every replica, require exact `(2,(2,))` with `--check-stage retired`, then run `--retirement-action commit --fence-id <recorded UUID>`. Commit uses a fresh lease/row/table lock and database clock and repeats exact attempt/fence/generation/row/fresh-retired-reporter/deadline proof immediately before clearing the fence. Only a successful commit authorizes the credential-use smokes.
9. If any retired-file install/restart/reporter/commit step fails, the durable fence remains armed, even after its 600-second database-clock deadline. Restore the dual-key activated ring to every replica, wait for exact `(2,(1,2))`, then run `--retirement-action cancel --fence-id <recorded UUID>`; cancel reacquires and locks, then rechecks the exact fresh activated reporters, fence/generation, and absence of versions outside `{1,2}` immediately before reopening writes. Source rows are safe on cancel because both readers have been restored; resume the ordinary rotation command to create/resume a fresh verification attempt. Never cancel while any replica lacks v1. After successful retirement, application rollback must remain keyring-capable; data/key rollback requires the separately encrypted old ring matched to the database backup. Never guess/recreate a key.

Document safe exit codes and include the exact sentences “A future-dated heartbeat closes the gate.” and “A rolled-back generation gap may require harmless reverification; it can never authorize retirement.” State that every database wait—including advisory acquisition/release, reporter/table/state locks, rewrap/verification rows, and completion/retirement actions—is bounded by the exact 5000ms/30000ms/35-second server/client settings and returns only a closed code with its transaction rolled back. PostgreSQL `clock_timestamp()`, not the application host clock, defines heartbeat freshness and fence start/deadline decisions. Stale reporters also do not open gates, and an operator must drain old deployments so they cannot return. Explain that Compose file-backed secret permissions come from host numeric ownership/mode, so the root-anchored stdin/FD install producing UID-10001 ownership plus the live non-root read gate is mandatory; `chmod 600` alone and ignored Compose `uid/gid/mode` fields are not remedies. The isolated evidence recipe must clear hostile inherited `DOCKER_HOST`, `DOCKER_CONTEXT`, all Docker TLS/certificate/config/header variables, and Compose profile/file/project/env-file variables; it validates one local Unix socket, sets exact `DOCKER_HOST=unix://<validated socket>` and `COMPOSE_DISABLE_ENV_FILE=1`, and uses that same environment for every build/config/up/port/inspection/stat/down call. It explicitly says a hostile repository `.env` must not load. Explicitly distinguish allowed path visibility—the fixed `/run/secrets/jhin_master_key` container target and exact operator-configured Compose source—from forbidden key material, inline values, unrelated sensitive paths, and path-bearing errors/logs. Document that the protected workspace page shows workspace row counts only, while global rotation counters/generation/fence identity remain host-internal data. Copy the canonical Markdown byte-for-byte to `apps/web/public/runbooks/master-key-rotation.md`, link that served target from Operations, and link the canonical repository document from protected health/README. The equality test prevents drift.

- [ ] **Step 4: Run documentation GREEN and focused security scans**

```bash
uv run pytest tests/test_master_key_rotation_docs.py -q
pnpm --filter jhin-web test -- operations-page.test.tsx
! rg -n 'cat .*master|echo .*MASTER|set -x|docker inspect|MASTER_KEY=.*\{|MASTER_KEY=.*base64' docs/operations/master-key-rotation.md README.md .env.example Makefile
! rg -n 'sudo( -n)? chown|sudo( -n)? (cp|mv)|sudo( -n)? rm .*-[Rr]|chmod 600 (is enough|is sufficient)' docs/operations/master-key-rotation.md
rg -n 'ciphertext and nonce do not change|row-scoped plaintext remains redacted through exception conversion and is then cleared|root-owned non-user-writable|/proc/self/fd/0|sudo -n install -m 0600 -o 10001 -g 10001|credential-mutation generation' docs/operations/master-key-rotation.md
git diff --check
```

Expected: both negative searches return no unsafe instruction; the positive search finds every exact safety statement.

- [ ] **Step 5: Run the complete release gate**

```bash
uv run pytest -m 'not integration' -q
uv run pytest packages/tools/tests/test_gateway.py packages/tools/tests/test_gateway_concurrency.py services/agent_worker/tests/test_approval_activity.py apps/api/tests/test_approvals_unit.py apps/api/tests/test_connections_unit.py packages/secrets/tests/test_store.py -q
JHIN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres uv run pytest -m integration tests/integration/test_phase10_master_key_rotation_migration.py tests/integration/test_phase10_master_key_rotation_postgres.py packages/secrets/tests/test_rotation_cli.py -q
(
  live_gate_output="$(mktemp)"
  trap 'rm -f "$live_gate_output"' EXIT
  if ! make test-master-key-rotation-integration >"$live_gate_output" 2>&1; then
    exit 1
  fi
  test "$(rg -c '^outer_trap_cleanup_ok$' "$live_gate_output")" -eq 1
  ! rg -n '^(outer_trap_cleanup_failed|runtime_key_cleanup_rejected)$' "$live_gate_output"
)
uv run ruff check .
uv run ruff format --check .
uv run mypy
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test
pnpm --filter jhin-web build
compose_socket=/var/run/docker.sock
test -S "$compose_socket"
test ! -L "$compose_socket"
compose_gid="$(uv run python -c 'import os; print(os.lstat("/var/run/docker.sock").st_gid)')"
case "$compose_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$compose_gid" -gt 0
compose_render_config="$(mktemp -d)"
trap 'rmdir "$compose_render_config"' EXIT
env -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH -u DOCKER_TLS_CERTDIR -u DOCKER_API_VERSION -u DOCKER_CUSTOM_HEADERS -u DOCKER_DEFAULT_PLATFORM -u COMPOSE_PROFILES -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_ENV_FILES -u COMPOSE_PATH_SEPARATOR DOCKER_HOST="unix://$compose_socket" DOCKER_CONFIG="$compose_render_config" COMPOSE_DISABLE_ENV_FILE=1 SANDBOX_DOCKER_GID="$compose_gid" SANDBOX_DOCKER_SOCKET_HOST="$compose_socket" docker compose -p jhin-keyrot-render-rootful -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml config --quiet
env -u SANDBOX_DOCKER_GID -u SANDBOX_DOCKER_SOCKET_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH -u DOCKER_TLS_CERTDIR -u DOCKER_API_VERSION -u DOCKER_CUSTOM_HEADERS -u DOCKER_DEFAULT_PLATFORM -u COMPOSE_PROFILES -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_ENV_FILES -u COMPOSE_PATH_SEPARATOR DOCKER_HOST="unix://$compose_socket" DOCKER_CONFIG="$compose_render_config" COMPOSE_DISABLE_ENV_FILE=1 docker compose -p jhin-keyrot-render-rootless -f compose.yaml -f compose.dev.yaml -f compose.rootless.yaml config --quiet
```

Expected: all commands pass. The web gate executes lint, typecheck, all tests, and a production build after the TS/TSX changes. Both Compose renders validate one actual local nonsymlink socket, use its real positive GID for rootful only, explicitly omit mount GID/socket variables for rootless, pin distinct `-p` names plus the same empty Docker config/`DOCKER_HOST`, force `COMPOSE_DISABLE_ENV_FILE=1`, and scrub every conflicting inherited authority. The captured live target contains exactly one `outer_trap_cleanup_ok`, no cleanup failure/rejection, and proves old/new application images, active ordinary reads, three service restarts, bounded resume, zero source rows, exact heartbeat gates, armed handoff, old-key removal, committed retirement, and credential-use smoke.

- [ ] **Step 6: Run final authority/leakage/schema/file-map scans**

```bash
rg -n 'MASTER_KEY|jhin_master_key' compose.yaml compose.dev.yaml compose.rootful.yaml compose.rootless.yaml
! rg -n 'key_bytes|key_file_path|plaintext|fingerprint|ciphertext|nonce|wrapped_data_key' packages/db/src/jhin_db/models/key_rotation.py
! rg -n 'key_bytes|key_file_path|plaintext|fingerprint|wrapped_data_key' packages/db/src/jhin_db/alembic/versions/20260818_0017_master_key_rotation.py
rg -n 'class MasterKeyRotationSummary|from_version|to_version|workspace_rows_(total|from_version|to_version)|safe_error_code' apps/api/src/jhin_api/health/schemas.py
! rg -n '^ *rows_(total|rewrapped|verified|failed):' apps/api/src/jhin_api/health/schemas.py
! rg -n 'credential_mutation_generation|retirement_fence_(id|generation|started_at|deadline)' apps/api/src/jhin_api/health/schemas.py apps/web/lib/types.ts 'apps/web/app/(app)/operations/page.tsx'
rg -n 'master_key.rotation_(started|completed|aborted)' packages/secrets/src/jhin_secrets/rotation.py packages/secrets/tests/test_rotation.py
rg -n 'revision.*0017|down_revision.*0016|0017.*0016|secret_credential_mutation_generation_seq|trg_secret_credential_mutation_generation' packages/db/src/jhin_db/alembic/versions/20260818_0017_master_key_rotation.py packages/db/tests/test_migration_graph.py tests/integration/test_phase10_master_key_rotation_migration.py
git diff --name-only HEAD -- packages/db/src/jhin_db/models/secret.py packages/db/src/jhin_db/alembic/versions/20260816_0006_secret.py
git status --short -- orgforge-production-implementation-plan.md
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
```

Expected: Compose grants only the fixed target and configured file source to API/agent/tool; the rotation model has no material fields and the migration mentions ciphertext/nonce only in the required trigger event; the protected schema search finds only the explicit bounded workspace summary and no mutation generation; all three audit actions are implemented/tested; graph is `0017 -> 0016`; the existing secret model/migration diff is empty; and the user-owned file is unstaged/untouched.

- [ ] **Step 7: Stage only Task 9 docs/UI, commit, then prove the complete path inventory is clean**

```bash
git add docs/operations/master-key-rotation.md apps/web/public/runbooks/master-key-rotation.md docs/operations/protected-health.md README.md tests/test_master_key_rotation_docs.py 'apps/web/app/(app)/operations/page.tsx' apps/web/tests/operations-page.test.tsx
git diff --cached --name-only
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
uv run python -c 'from pathlib import Path; import hashlib; b=Path("orgforge-production-implementation-plan.md").read_bytes(); assert len(b) == 82118 and hashlib.sha256(b).hexdigest() == "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"'
git commit -m "docs: publish master key rotation runbook"
git status --short -- docs/superpowers/plans/2026-08-18-phase-10-master-key-rotation.md packages/secrets/src/jhin_secrets/keyring.py packages/secrets/src/jhin_secrets/safe_cli.py packages/secrets/src/jhin_secrets/keyring_cli.py packages/secrets/src/jhin_secrets/crypto.py packages/secrets/src/jhin_secrets/__init__.py packages/secrets/tests/test_keyring.py packages/secrets/tests/test_keyring_cli.py packages/secrets/tests/test_crypto.py packages/secrets/pyproject.toml scripts/generate_master_key.py tests/test_generate_master_key.py scripts/capture_pre_keyring_ref.py tests/test_capture_pre_keyring_ref.py tests/integration/fixtures/phase10-pre-keyring-ref.txt uv.lock packages/secrets/src/jhin_secrets/store.py packages/secrets/src/jhin_secrets/material.py packages/secrets/src/jhin_secrets/redaction.py packages/secrets/tests/test_store.py packages/secrets/tests/test_redaction.py packages/db/src/jhin_db/models/key_rotation.py packages/db/src/jhin_db/models/__init__.py packages/db/src/jhin_db/alembic/versions/20260818_0017_master_key_rotation.py packages/db/tests/test_migration_graph.py packages/db/tests/test_master_key_rotation_model.py tests/integration/test_phase10_master_key_rotation_migration.py apps/api/src/jhin_api/health/checks.py apps/api/tests/test_health.py apps/api/src/jhin_api/main.py apps/api/src/jhin_api/seed.py apps/api/tests/test_keyring_startup.py apps/api/tests/test_seed.py services/agent_worker/src/jhin_agent_worker/resources.py services/agent_worker/tests/test_keyring_resources.py services/tool_worker/src/jhin_tool_worker/resources.py services/tool_worker/tests/test_keyring_resources.py compose.yaml .env.example tests/test_master_key_service_boundary.py tests/test_master_key_compose.py packages/secrets/src/jhin_secrets/rotation.py packages/secrets/tests/test_rotation.py packages/tools/src/jhin_tools/gateway.py packages/tools/tests/test_gateway.py tests/integration/test_phase10_master_key_rotation_postgres.py packages/secrets/src/jhin_secrets/rotation_cli.py packages/secrets/tests/test_rotation_cli.py apps/api/src/jhin_api/health/schemas.py apps/api/src/jhin_api/health/service.py apps/api/tests/conftest.py apps/api/tests/test_operations_health.py apps/web/lib/types.ts 'apps/web/app/(app)/operations/page.tsx' apps/web/tests/operations-page.test.tsx tests/integration/phase10_key_rotation_harness.py tests/integration/compose.phase10-keyring-upgrade.yaml tests/integration/test_phase10_keyring_upgrade.py tests/integration/test_phase10_master_key_rotation.py tests/test_phase10_master_key_rotation_harness.py tests/integration/conftest.py Makefile .github/workflows/ci.yml docs/operations/master-key-rotation.md apps/web/public/runbooks/master-key-rotation.md docs/operations/protected-health.md README.md tests/test_master_key_rotation_docs.py
git status --short -- orgforge-production-implementation-plan.md
```

Expected Task 9 cached names: exactly the seven paths in its `git add`. After commit, the complete implementation-path status command prints nothing. The user-owned file may remain untracked exactly as it was and is never staged.

## Execution Handoff

Run tasks in order. Stop at every RED gate if the failure is not the stated missing behavior; repair the test/assumption before implementation. Never distribute or activate a new key merely because unit tests pass: deployment stages advance only through the exact fresh-replica gates, verified backup checkpoints, and ordinary credential-use evidence in Task 9's runbook.
