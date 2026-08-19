# Phase 10 Secret Audit, Chaos Recovery, and Exit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Audit every secret-bearing data flow and persistence/export sink, prove deterministic recovery across the complete worker and dependency matrix, run the restored-environment Phase 10 exit, and close Phase 10 only from direct sanitized evidence.

**Architecture:** A repository-owned sink/scenario registry drives a per-run canary corpus, structural-before-persistence sanitization tests, and allowlisted scanners over PostgreSQL, Temporal, NATS, telemetry, APIs, UI, Docker, backup, and restore surfaces. Existing agent/tool crash barriers remain stable; a new dependency-light test-controls package adds only the event, activity-commit, and sandbox boundaries that the integrated matrix still needs. All live tests consume the runbooks plan's `IsolatedComposeProject`, current digest-pinned images, fake providers, encrypted backup/restore, upgrade rehearsal, key-rotation protocol, protected health, and DLQ/retry contracts. Test controls exist only in a chaos overlay, match one exact identity in one process, and are rejected by every production settings path. A bounded rootful/rootless runner records only allowlisted evidence after scanning it for all raw and encoded canaries.

**Tech Stack:** Python 3.13, Pydantic 2, FastAPI, SQLAlchemy 2.0.52, PostgreSQL 17, Temporal Python SDK 1.31.0 and server 1.29.7, NATS JetStream 2.12, Next.js 16.3.1, React 19.2.8, Docker Compose/Buildx, pytest, Temporal time-skipping, Vitest, Ruff, mypy, Trivy, pip-audit, pnpm audit, and GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-18-phase-10-production-operations-design.md`, especially “Secret and logging audit,” “Chaos and recovery tests,” sub-project 7, sequencing, acceptance evidence, and “Final recovery exit test.”

## Global Constraints

- This is exactly Phase 10 sub-project 7. Execute it only after the tool-worker boundary, telemetry core, protected health, DLQ/retry, master-key rotation, and runbooks/hardening plans have completed their acceptance commits. Consume those interfaces; do not weaken, duplicate, rename, or edit any predecessor plan.
- The starting Alembic head is exactly `0018`. This plan adds no migration and no product-authority table. PostgreSQL remains product/command truth, Temporal remains workflow-history truth, and NATS remains at-least-once transport.
- Preserve `TOOL_TASK_QUEUE = "jhin-tool-queue"`, all seven existing `jhin_tools.test_barriers.CrashBarrierName` values, stable invocation identities, protected-health freshness, DLQ attempt/quarantine/replay semantics, the staged keyring protocol, encrypted backup/restore, previous-release upgrade, digest-pinned images, and `scripts.phase10_compose.IsolatedComposeProject` unchanged at their public boundaries.
- The audited secret lifecycle is creation -> encryption -> ordinary decrypt/use -> active-read rotation -> encrypted backup -> fresh restore -> ordinary decrypt/use. A plaintext value may exist only inside the bounded process that currently needs it; the audit never creates a plaintext backup, trace, message, fixture artifact, or diagnostic.
- Audit every sink named by the design: secret create/encrypt/decrypt/rotation/backup/recovery; model and connector HTTP success/error; webhook, DLQ, and replay; Temporal payload/history/activity failures; tool manifest/gateway/approval/connectors/sandbox; NATS headers/envelopes; sandbox env/stdout/stderr/Docker/orphans; structured logs/traces/metrics/audit/run events; public/protected APIs, UI, backups, and restore output.
- Each live run generates unique values for API key, Authorization header, cookie, private-key fragment, DSN user, DSN password, webhook secret, master-key-like material, and sandbox secret environment. It also injects them under unknown camelCase/snake/kebab credential-bearing keys and in nested, JSON-escaped, form-encoded, percent-encoded, double-percent-encoded, standard-base64, and URL-safe-base64 forms.
- Every sink scan rejects the exact canary and every derived encoding. Structural tests separately prove unknown credential-bearing keys are replaced before a PostgreSQL/Temporal/NATS/audit/run-event/DLQ/sandbox persistence or export call, not merely hidden during log or UI rendering.
- Known-value redaction and structural redaction are defense in depth. Core user-authored product fields are not silently destroyed; only metadata, error, telemetry, sanitized input/output, event, audit, recovery, and diagnostic boundaries use the persistence sanitizer.
- Existing agent/tool barriers remain in `jhin_tools.test_barriers` with their exact names and marker protocol. New failpoints are process-local, selected by one exact versioned name and one exact UUID/job identity, configured only through `compose.phase10-chaos.yaml`, and absent from base/production Compose. There is no public or protected fault-injection endpoint.
- Production startup rejects any nonempty or empty-present key with prefixes `JHIN_TEST_CRASH_BARRIER_`, `JHIN_TEST_FAILPOINT_`, or `JHIN_CHAOS_`. Rendered production Compose contains none of those keys, no test-control mount, and no fake-provider service. Failure is a stable safe code and never echoes a value/path/identity.
- Test failpoint actions are only `wait` and `raise_once`. `wait` writes/fsyncs one arrival marker and waits for a release marker so the harness can send SIGKILL. `raise_once` writes/fsyncs a consumed marker and raises `InjectedTestFailure` once in that process. Runtime code never deletes markers or converts a failpoint into business authority.
- Every scenario uses a fresh project name matching the runbooks contract `^phase10-[a-z0-9-]{1,24}-[0-9a-f]{12}$`, dynamic loopback ports, project-labelled disposable volumes/networks, a unique Temporal namespace, a unique synthetic keyring/private identity, current digest-pinned images, fake model/connectors/webhooks, and explicit `rootful` or `rootless` socket authority. No fixed project, port, volume, key, canary, or provider effect ledger is reused.
- Compose vectors are exact and ordered: secret audit uses `compose.yaml`, `compose.operations.yaml`, `compose.{socket_mode}.yaml`, then `compose.phase10-secret-audit.yaml`; scenarios 01–09 use `compose.yaml`, `compose.{socket_mode}.yaml`, then `compose.phase10-chaos.yaml`; scenario 10's source upgrade uses the settled runbook-owned `compose.yaml`/`compose.operations.yaml`/socket/`compose.phase10-upgrade-test.yaml` vector, and its restored destination uses `compose.yaml`, `compose.operations.yaml`, `compose.{socket_mode}.yaml`, `compose.phase10-chaos.yaml`, then `compose.phase10-final-exit.yaml`. While one exact boundary is armed, the harness appends one runner-generated mode-`0600` control overlay as the final file; after SIGKILL it force-recreates from the preceding checked-in vector with that file omitted. Secret/chaos/final vectors enable the existing `observability` profile and define their own fake-provider services; none uses `compose.dev.yaml` or an implicit Compose file.
- Every scenario asserts the product UI/API state, authoritative PostgreSQL counts/rows, Temporal workflow count/status, NATS durable consumer pending/ack/redelivery state, fake-provider externally visible effect count, required audit actions, protected health, and zero raw/encoded canary matches. Polling uses monotonic bounded deadlines and emits sanitized diagnostics on timeout.
- The ten scenario IDs and effects are fixed: agent pre/post manifest-bind SIGKILL; tool preclaim/postclaim/posteffect SIGKILL; event post-handler/pre-ack; handler exhaustion with failed quarantine commit and one idempotent replay; workflow-worker timer+approval restart; NATS+Temporal restart during dispatch; PostgreSQL restart during activity commit; sandbox-runner hard kill/orphan reap/socket isolation; active-read master-key rotation plus API/agent/tool restart; and restored fresh-project worker-restart exit.
- Pull-request CI runs unit tests, Temporal time-skipping, DLQ/retry integration, secret canaries, migrations, production Compose validation, and one deterministic agent, one tool, and one event recovery case. Nightly runs the full ten-scenario matrix, backup/restore, previous-release upgrade, dependency/container scans, and only sanitized artifacts. Normal CI calls no third-party provider API.
- The final exit begins from a supported previous state, migrates/upgrades it with the settled runbook, takes a verified release backup, restores it into a second fresh project with empty volumes and a new private key mount, then runs the worker-restart chain using current images. It proves one durable outcome, at-most-once external effects or durable `execution_unknown`, zero lag, healthy recovery, and zero canary leakage before teardown.
- Teardown has the runbooks contract's 60-second outer bound, project-label verification, and one project-scoped kill/down retry. Cleanup never targets a default/production project, broad path, unresolved variable, or unrelated Docker resource.
- Evidence contains only schema versions, commit/image hashes, dates, versions, scenario/sink IDs, socket mode, integer counts/durations, closed outcomes, and booleans. Never upload or check in canary manifests, `.env`, keyrings, age identities, backups, dumps, NATS archives, Temporal histories, raw logs/traces/metrics, Docker inspect/status, raw scan reports, DSNs, URLs, resource IDs, absolute paths, or provider payloads.
- Raw diagnostic producer bytes are bounded, scanned, reduced, and discarded in memory; they are never persisted before sanitization, even in runner temp/cache/JUnit files. Only a fully scanned closed-schema artifact may acquire a directory entry through the unprivileged anonymous no-replace publication primitive; producer/sanitizer crash leaves no partial artifact. Before starting a producer, every runner proves `/proc/self/fd` plus `O_TMPFILE` publication works as its ordinary nonroot UID with no effective capabilities.
- Update `docs/implementation-plan.md` only after the aggregate verifier directly validates all predecessor evidence, both socket modes, all ten scenarios, restore/upgrade/scans, fresh-machine onboarding, and the manual real GitHub+Linear release gate. Then mark exactly the fourteen Phase 10 checkboxes and all section 49 readiness bullets complete. Do not change or check any Phase 11 box.
- Every task follows strict RED -> inspect the named expected failure -> minimal GREEN -> focused regression -> affected suite -> lint/typecheck/static validation -> exact path staging -> commit. Task 13 intentionally has two disjoint exact-path commits so the verifier exists before it creates closure evidence. Stop when RED fails for an unexpected reason. Never use `git add .`; the union of each task's staging commands equals its exact `Files` set.
- The user-owned untracked `orgforge-production-implementation-plan.md` remains exactly 82,118 bytes with SHA-256 `ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513`. It may be touched only by silent path-scoped `git status --short`, `stat -c %s`, and `shasum -a 256` guards. Never open, print, search, parse, edit, stage, rename, delete, or commit it.

## Shared Interfaces

The following names, values, ordering, and evidence shapes are fixed for this plan.

```python
# scripts/phase10_secret_audit.py
SECRET_CANARY_KINDS = (
    "api_key",
    "authorization",
    "cookie",
    "private_key_fragment",
    "dsn_user",
    "dsn_password",
    "webhook_secret",
    "master_key_like",
    "sandbox_secret_env",
)

CANARY_FORM_KINDS = (
    "raw",
    "json_escaped",
    "url_percent",
    "form_encoded",
    "double_percent",
    "base64_standard",
    "base64_urlsafe",
)

SENSITIVE_NESTED_KEYS = (
    "unknownApiKey",
    "unknown_authorization",
    "unknown-cookie",
    "unknownPrivateKey",
    "unknown_dsn",
    "unknownWebhookSecret",
    "unknownMasterKey",
    "unknownSecretEnv",
)
```

`SecretCanarySet.create()` independently generates at least 192 random bits for every kind, formats valid category-specific values with a distinct percent-encoded representation, uses a 32-byte standard-base64 value for `master_key_like`, and refuses duplicate/subsequence values or a value whose seven form classes are incomplete. `write_manifest(path)` uses `O_CREAT | O_EXCL | O_NOFOLLOW`, mode `0600`, a canonical schema-v1 JSON object, and fsync; `read_manifest(path)` requires the caller's UID, exact mode `0600`, a regular non-symlink file, exact keys, and unique values. `forbidden_forms_for(kind) -> dict[str, tuple[bytes, ...]]` returns a closed `CANARY_FORM_KINDS` mapping to raw, JSON-escaped, `urllib.parse.quote(value, safe="")`, `quote_plus`, double-percent-encoded, standard-base64, and URL-safe-base64 candidates; the Authorization/cookie/private-key/DSN/sandbox composites add candidates within those same classes. Classes remain present even if two encodings yield equal bytes. `CanaryScanner.scan_bytes(sink_id, payload)` returns only a safe `(sink_id, form_kind)` violation and never the matched value.

```python
# packages/secrets/src/jhin_secrets/sanitize.py
REDACTED = "[REDACTED]"
MAX_SANITIZE_DEPTH = 8
MAX_SANITIZE_ITEMS = 64
MAX_SANITIZE_STRING = 2_000
```

`is_sensitive_key(key: str) -> bool` normalizes camelCase, snake_case, kebab-case, dots, and brackets; it recognizes authorization, cookie/set-cookie, password, secret, token, API/private/master keys, credentials, DSN/userinfo, request/response bodies, prompts/completions, tool input/output, webhook payload/signature, and secret env by exact name or suffix. `sanitize_for_persistence(value: object) -> object` recursively replaces values under those keys before stringification, strips URL userinfo/query/fragment, bounds depth/items/strings, rejects unsupported cyclic values safely, and then applies the process known-value redactor. All metadata/error/recovery sinks call it before ORM assignment, Temporal/NATS serialization, audit/run-event construction, sandbox response creation, or exporter invocation.

`contains_secret_material(value: object) -> bool` performs the same bounded traversal without stringifying an unsupported object and returns true for a sensitive key, URL credential/query/fragment, process-registered known value, cycle, depth/item overflow, or unsupported value. Temporal's registered payload converter uses this fail-closed predicate to reject a payload with only `temporal_sensitive_payload_forbidden`; persistence/export sinks use the sanitizer when their contract permits a redacted projection.

```python
# packages/test_controls/src/jhin_test_controls/failpoints.py
from enum import StrEnum


class TestFailpointName(StrEnum):
    EVENT_AFTER_HANDLER_BEFORE_ACK = "phase10.event.after_handler.before_ack.v1"
    EVENT_BEFORE_QUARANTINE_COMMIT = "phase10.event.before_quarantine_commit.v1"
    EVENT_COMPLETED_BEFORE_TERM = "phase10.event.completed.before_term.v1"
    AGENT_BEFORE_ACTIVITY_COMMIT = "phase10.agent.before_activity_commit.v1"
    SANDBOX_AFTER_CONTAINER_START = "phase10.sandbox.after_container_start.v1"


class TestFailpointAction(StrEnum):
    WAIT = "wait"
    RAISE_ONCE = "raise_once"
```

`TestFailpointConfig(root: Path | None, selected: TestFailpointName | None, match_identity: str | None, action: TestFailpointAction | None)` requires all-or-none fields, an absolute owner-only nonsymlink root, and a canonical UUID identity except for a sandbox job ID matching `^[a-z0-9][a-z0-9-]{7,63}$`; service settings additionally require the exact container root `/run/jhin/test-failpoints`. `TestFailpoints.reach(name, identity)` is an async no-op when disabled; on an exact match it uses `name--identity.arrived`, `.release`, and `.consumed` regular files with fsync/no-follow semantics. `InjectedTestFailure` renders only `injected_test_failure`. `jhin_tools.test_barriers` retains its original seven-value `CrashBarrierName`, `CrashBarrierConfig`, `CrashBarrier`, and marker names; it may delegate filesystem primitives to the neutral package but its import/API behavior cannot change.

`TestControlOverlay.write(path, *, service, family, name, identity, action, host_root)` accepts closed service/family/name/action enums, validates the exact identity and a new mode-`0700` host root, and exclusively writes/fsyncs one mode-`0600` canonical-JSON Compose mapping (valid YAML, using only the standard library). It sets `APP_ENV=test`, one complete `JHIN_TEST_FAILPOINT_*` or legacy crash-barrier family, and one read-write test-control mount on exactly the selected service; no other service receives a key or mount. The checked-in chaos overlay contains only fake providers/test topology and no control variable/mount. The generated overlay lives beneath the harness private root, is never uploaded or accepted from CLI input, and is omitted—not emptied—when the worker is recreated after the fault.

```python
# tests/integration/phase10_chaos_assertions.py
from dataclasses import dataclass
from typing import Literal


WORKER_LOSS_TIMEOUT_SECONDS = 50
WORKER_RECOVERY_TIMEOUT_SECONDS = 65


@dataclass(frozen=True)
class AuthoritySnapshot:
    ui_state: str
    api_state: str
    postgres_task_count: int
    postgres_run_count: int
    temporal_workflow_count: int
    temporal_status: str
    nats_num_pending: int
    nats_num_ack_pending: int
    nats_num_redelivered: int
    fake_effect_count: int
    audit_action_count: int
    protected_health: str
    worker_fresh_owner_instances: int | None
    temporal_retained_pollers: int
    temporal_recently_accessed_pollers: int
    canary_violations: int


@dataclass(frozen=True)
class EffectExpectation:
    minimum_per_identity: int
    maximum_per_identity: int
    allow_execution_unknown: bool
    allow_safe_failed: bool = False


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    socket_mode: Literal["rootful", "rootless"]
    outcome: Literal[
        "exactly_once", "at_most_once", "safe_failed", "execution_unknown"
    ]
    duration_seconds: int
    final: AuthoritySnapshot
```

`eventually(label, probe, predicate, *, timeout_seconds, diagnostics, clock=None)` uses the injected monotonic clock, an initial 0.25-second interval capped at 2 seconds, and a hard scenario-specific deadline no larger than 180 seconds. `None` selects an internal adapter over `time.monotonic` and `asyncio.sleep`; unit tests inject a deterministic clock exposing the same `monotonic()`/`sleep()` methods. On failure it calls the allowlisted diagnostic collector and raises only `TimeoutError(f"{label}: {safe_code}")`, where `safe_code` is selected from the closed diagnostic registry. `assert_scenario_contract(result)` requires UI=API=PostgreSQL/Temporal terminal truth, zero duplicate task/run/workflow, NATS pending/ack-pending zero, the scenario's exact effect rule, required audit count, protected health `ok`, and zero canary violations.

The checked-in registries use exactly these scenario IDs, in order:

```text
01-agent-manifest-bind
02-tool-effect-boundaries
03-event-post-handler-pre-ack
04-quarantine-commit-replay
05-workflow-timer-approval
06-nats-temporal-dispatch
07-postgres-activity-commit
08-sandbox-orphan-socket
09-master-key-active-read
10-restored-worker-restart-exit
```

## Complete File Map

The union below is exactly the union of every task `Files` block and every `git add` path. A path modified by multiple tasks appears once.

```text
.github/workflows/ci.yml
.github/workflows/phase10-chaos-nightly.yml
.github/workflows/release-security.yml
apps/api/pyproject.toml
apps/api/src/jhin_api/audit/service.py
apps/api/src/jhin_api/connections/service.py
apps/api/src/jhin_api/models/service.py
apps/api/src/jhin_api/public_payloads.py
apps/api/src/jhin_api/settings.py
apps/api/src/jhin_api/webhooks/service.py
apps/api/tests/test_secret_persistence.py
apps/web/lib/server-logger.ts
apps/web/tests/secret-sink-audit.test.tsx
apps/web/tests/server-logger.test.ts
compose.phase10-chaos.yaml
compose.phase10-final-exit.yaml
compose.phase10-secret-audit.yaml
docker/sandbox.Dockerfile
docker/sandbox_secret_exec.py
docs/evidence/phase10-backup.json
docs/evidence/phase10-chaos.json
docs/evidence/phase10-final-exit.json
docs/evidence/phase10-hardening.md
docs/evidence/phase10-image-security.json
docs/evidence/phase10-manual-release.json
docs/evidence/phase10-rate-limits.json
docs/evidence/phase10-restore.json
docs/evidence/phase10-secret-audit.json
docs/evidence/phase10-security.md
docs/evidence/phase10-sizing.json
docs/evidence/phase10-telemetry.md
docs/evidence/phase10-upgrades.json
docs/implementation-plan.md
docs/operations/chaos-recovery.md
docs/security/phase10-secret-data-flow.md
docs/superpowers/plans/2026-08-18-phase-10-secret-audit-chaos.md
ops/chaos/phase10-scenarios.json
ops/images/resolved-images.json
ops/security/phase10-secret-sinks.json
packages/connectors/src/jhin_connectors/http_client.py
packages/connectors/tests/test_secret_boundaries.py
packages/events/src/jhin_events/envelope.py
packages/events/tests/test_envelope.py
packages/models/src/jhin_models/base.py
packages/models/src/jhin_models/providers/anthropic.py
packages/models/src/jhin_models/providers/openai_compatible.py
packages/models/tests/test_secret_boundaries.py
packages/observability/src/jhin_observability/redaction.py
packages/observability/tests/test_logging.py
packages/secrets/src/jhin_secrets/__init__.py
packages/secrets/src/jhin_secrets/sanitize.py
packages/secrets/tests/test_sanitize.py
packages/test_controls/pyproject.toml
packages/test_controls/src/jhin_test_controls/__init__.py
packages/test_controls/src/jhin_test_controls/failpoints.py
packages/test_controls/tests/test_failpoints.py
packages/tools/src/jhin_tools/sanitize.py
packages/tools/tests/test_sanitize.py
packages/workflows/pyproject.toml
packages/workflows/src/jhin_workflows/__init__.py
packages/workflows/src/jhin_workflows/safe_failure_converter.py
packages/workflows/src/jhin_workflows/temporal_connection.py
packages/workflows/tests/test_safe_failure_converter.py
pyproject.toml
scripts/assert_phase10_production_compose.py
scripts/phase10_artifact.py
scripts/phase10_backup.py
scripts/phase10_chaos_artifact.py
scripts/phase10_restore.py
scripts/phase10_secret_audit.py
scripts/phase10_upgrade.py
scripts/record_phase10_security_evidence.py
scripts/run_phase10_chaos.py
scripts/run_phase10_final_exit.py
scripts/run_phase10_secret_audit.py
scripts/verify_phase10_exit.py
services/agent_worker/pyproject.toml
services/agent_worker/src/jhin_agent_worker/projections.py
services/agent_worker/src/jhin_agent_worker/resources.py
services/agent_worker/src/jhin_agent_worker/settings.py
services/agent_worker/tests/test_secret_persistence.py
services/agent_worker/tests/test_test_failpoints.py
services/event_worker/pyproject.toml
services/event_worker/src/jhin_event_worker/delivery.py
services/event_worker/src/jhin_event_worker/main.py
services/event_worker/src/jhin_event_worker/quarantine.py
services/event_worker/src/jhin_event_worker/settings.py
services/event_worker/tests/test_secret_persistence.py
services/event_worker/tests/test_test_failpoints.py
services/sandbox_runner/pyproject.toml
services/sandbox_runner/src/jhin_sandbox_runner/jobs.py
services/sandbox_runner/src/jhin_sandbox_runner/main.py
services/sandbox_runner/src/jhin_sandbox_runner/schemas.py
services/sandbox_runner/src/jhin_sandbox_runner/settings.py
services/sandbox_runner/tests/test_secret_delivery.py
services/sandbox_runner/tests/test_secret_persistence.py
services/sandbox_runner/tests/test_test_failpoints.py
services/tool_worker/pyproject.toml
services/tool_worker/src/jhin_tool_worker/settings.py
services/workflow_worker/pyproject.toml
services/workflow_worker/src/jhin_workflow_worker/settings.py
tests/integration/phase10_chaos_assertions.py
tests/integration/phase10_chaos_harness.py
tests/integration/phase10_secret_audit_harness.py
tests/integration/test_phase10_chaos_dependencies.py
tests/integration/test_phase10_chaos_events.py
tests/integration/test_phase10_chaos_key_rotation.py
tests/integration/test_phase10_chaos_workers.py
tests/integration/test_phase10_final_exit.py
tests/integration/test_phase10_secret_audit.py
tests/test_phase10_chaos_artifact.py
tests/test_phase10_chaos_contract.py
tests/test_phase10_chaos_controls.py
tests/test_phase10_chaos_harness.py
tests/test_phase10_ci_schedule.py
tests/test_phase10_exit_evidence.py
tests/test_phase10_final_exit_harness.py
tests/test_phase10_secret_audit.py
tests/test_phase10_security_contract.py
tests/test_phase10_security_evidence.py
tests/test_production_configuration.py
uv.lock
```

---

### Task 0: Check In the Reviewed Final Phase 10 Plan

**Files:**
- Create: `docs/superpowers/plans/2026-08-18-phase-10-secret-audit-chaos.md`

**Interfaces:**
- Consumes: the six completed predecessor acceptance commits, Alembic head `0018`, the binding design, and the unchanged OrgForge metadata guard.
- Produces: one plan-only checkpoint from which sub-project 7 can be implemented without editing a predecessor or sibling plan.

- [ ] **Step 1: Prove all predecessor acceptance commits are ancestors and schema head is `0018`**

Run on the Linux implementation host:

```bash
set -euo pipefail
subjects=(
  "test: verify Phase 10 tool-worker boundary"
  "docs(observability): record Phase 10 telemetry evidence"
  "docs: explain protected health operations"
  "docs: explain dlq and retry recovery"
  "docs: publish master key rotation runbook"
  "docs: publish production operations runbooks"
)
for subject in "${subjects[@]}"; do
  commit="$(git log -1 --format=%H --fixed-strings --grep="$subject")"
  test -n "$commit"
  test "$(git show -s --format=%s "$commit")" = "$subject"
  git merge-base --is-ancestor "$commit" HEAD
done
uv run python -c 'from alembic.script import ScriptDirectory; from jhin_db.migrate import alembic_config; s=ScriptDirectory.from_config(alembic_config("sqlite://")); assert s.get_heads()==["0018"]'
```

Expected: PASS. If a subject/head is absent or not an ancestor, stop; sub-project 7 cannot compensate for incomplete predecessor work.

- [ ] **Step 2: Validate the plan structure before staging**

```bash
set -euo pipefail
plan=docs/superpowers/plans/2026-08-18-phase-10-secret-audit-chaos.md
test "$(rg -c '^### Task [0-9]+:' "$plan")" = "14"
test "$(rg -c '^\*\*Files:\*\*$' "$plan")" = "14"
test "$(rg -c '^\*\*Interfaces:\*\*$' "$plan")" = "14"
test "$(rg -c '^git commit -m ' "$plan")" = "15"
test "$(rg -c '^```$' "$plan")" = "$(rg -c '^```(bash|python|json|text|typescript|yaml)$' "$plan")"
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
tasks = re.split(r"(?=^### Task \d+:)", text, flags=re.MULTILINE)[1:]
assert len(tasks) == 14
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
    expected_commits = 2 if expected_number == 13 else 1
    assert len(re.findall(r"^git commit -m ", task, re.MULTILINE)) == expected_commits
    all_files.update(files)
    all_adds.update(adds)

assert set(map_paths) == all_files == all_adds
PY
```

Expected: PASS with fourteen tasks, fifteen commits, balanced fences, and no placeholder or broad staging command.

- [ ] **Step 3: Guard, stage only this plan, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add docs/superpowers/plans/2026-08-18-phase-10-secret-audit-chaos.md
git diff --cached --check
git commit -m "docs: plan Phase 10 secret audit and chaos exit"
```

Expected: commit 1 of 15 contains only this plan.

### Task 1: Freeze the Cross-Sink Threat Model and Ten-Scenario Contract

**Files:**
- Create: `ops/security/phase10-secret-sinks.json`
- Create: `ops/chaos/phase10-scenarios.json`
- Create: `docs/security/phase10-secret-data-flow.md`
- Create: `docs/operations/chaos-recovery.md`
- Create: `tests/test_phase10_security_contract.py`
- Create: `tests/test_phase10_chaos_contract.py`

**Interfaces:**
- Consumes: every design-listed sink, section 48 security invariants, all predecessor authority/recovery contracts, and the ten fixed scenario IDs.
- Produces: canonical schema-v1 sink/scenario registries, a data-flow/threat review, and an operator recovery matrix that every later runner/evidence validator loads rather than retyping.

- [ ] **Step 1: Write RED registry and documentation contract tests**

Create `tests/test_phase10_security_contract.py` with the complete required set:

```python
import json
from pathlib import Path


REQUIRED_SINKS = {
    "secret.creation", "secret.encryption", "secret.decryption", "secret.rotation",
    "secret.backup", "secret.recovery", "model.http.success", "model.http.error",
    "connector.http.success", "connector.http.error", "webhook.ingress", "dlq.failure",
    "replay.command", "temporal.payload", "temporal.history", "temporal.activity_failure",
    "tool.manifest", "tool.gateway", "tool.approval", "tool.connector",
    "tool.sandbox_call", "nats.header", "nats.envelope", "sandbox.env",
    "sandbox.stdout", "sandbox.stderr", "sandbox.docker_error", "sandbox.orphan_cleanup",
    "telemetry.log", "telemetry.trace", "telemetry.metric", "product.audit_metadata",
    "product.run_event", "api.public", "api.protected", "ui.error_card",
    "backup.artifact", "restore.output",
}


def test_sink_registry_is_complete_and_pre_persistence_is_explicit() -> None:
    document = json.loads(Path("ops/security/phase10-secret-sinks.json").read_text())
    assert set(document) == {"schema_version", "sinks"}
    assert document["schema_version"] == 1
    rows = document["sinks"]
    assert {row["id"] for row in rows} == REQUIRED_SINKS
    assert len(rows) == len(REQUIRED_SINKS)
    for row in rows:
        assert set(row) == {"id", "authority", "write_boundary", "probe", "canary_forms"}
        assert row["write_boundary"] in {"before_persistence", "before_export", "no_plaintext"}
        assert row["canary_forms"] == "all"
```

Create `tests/test_phase10_chaos_contract.py`:

```python
import json
from pathlib import Path


SCENARIO_IDS = [
    "01-agent-manifest-bind", "02-tool-effect-boundaries",
    "03-event-post-handler-pre-ack", "04-quarantine-commit-replay",
    "05-workflow-timer-approval", "06-nats-temporal-dispatch",
    "07-postgres-activity-commit", "08-sandbox-orphan-socket",
    "09-master-key-active-read", "10-restored-worker-restart-exit",
]


def test_scenario_registry_has_exact_matrix_and_assertions() -> None:
    document = json.loads(Path("ops/chaos/phase10-scenarios.json").read_text())
    assert set(document) == {"schema_version", "scenarios"}
    assert document["schema_version"] == 1
    rows = document["scenarios"]
    assert [row["id"] for row in rows] == SCENARIO_IDS
    effect_rules = {
        "01-agent-manifest-bind": "exactly_once",
        "02-tool-effect-boundaries": "at_most_once_or_execution_unknown",
        "03-event-post-handler-pre-ack": "exactly_once",
        "04-quarantine-commit-replay": "exactly_once",
        "05-workflow-timer-approval": "exactly_once",
        "06-nats-temporal-dispatch": "exactly_once",
        "07-postgres-activity-commit": "at_most_once_or_execution_unknown",
        "08-sandbox-orphan-socket": "safe_failed_or_execution_unknown",
        "09-master-key-active-read": "exactly_once",
        "10-restored-worker-restart-exit": "at_most_once_or_execution_unknown",
    }
    for row in rows:
        assert set(row) == {
            "id", "pytest_node", "faults", "effect_rule", "timeout_seconds", "assertions",
        }
        assert 1 <= row["timeout_seconds"] <= 180
        assert row["assertions"] == [
            "ui_api", "postgres", "temporal", "nats", "fake_effects",
            "audit", "protected_health", "canary_absence",
        ]
        assert row["effect_rule"] == effect_rules[row["id"]]
```

Run:

```bash
uv run pytest tests/test_phase10_security_contract.py tests/test_phase10_chaos_contract.py -q
```

Expected: FAIL because both canonical registries and both reviewed documents are absent.

- [ ] **Step 2: Create the exact sink registry and threat model**

Write `phase10-secret-sinks.json` with exactly the 38 IDs above, sorted by lifecycle order rather than alphabetically. Every row names one closed authority (`process`, `postgres`, `temporal`, `nats`, `docker`, `telemetry`, `http`, `ui`, `backup`) and one concrete probe implemented in Task 4. No SQL, URL, table value, secret key, or filesystem path appears in the registry.

`phase10-secret-data-flow.md` must include one table row per registry ID with source, in-memory plaintext allowance, sanitization/encryption boundary, durable/export destinations, expected safe projection, and owning test. It explicitly traces:

1. API credential create -> envelope encryption -> ciphertext row -> ordinary worker decrypt/use;
2. dual-key active reads -> rewrap -> three key-bearing service restarts -> old-key retirement proof;
3. maintenance backup with separately encrypted keyring -> fresh-volume restore -> normal decrypt/use;
4. model/connector success and adversarial error bodies;
5. signed webhook -> ingress -> normalize -> canonical event -> quarantine/DLQ -> idempotent replay;
6. Temporal input/history/activity failure normalization;
7. manifest bind -> approval -> gateway claim -> connector/sandbox result;
8. NATS header/envelope, sandbox secret-file/env/stdout/stderr/Docker/orphan, telemetry, audit/run event, API/UI, backup/restore outputs.

For every section 48 invariant, name the existing direct regression test and state why telemetry/operations/test controls cannot retrieve secrets, authorize tools, cross workspaces, bypass signatures/approval, repeat durable work, expose a socket to jobs, or change permissions.

- [ ] **Step 3: Create the exact scenario registry and recovery matrix**

Each `pytest_node` is the concrete node added by Tasks 7-11. Use these exact values:

```json
[
  "tests/integration/test_phase10_chaos_workers.py::test_agent_manifest_bind_recovery",
  "tests/integration/test_phase10_chaos_workers.py::test_tool_effect_boundary_recovery",
  "tests/integration/test_phase10_chaos_events.py::test_event_post_handler_pre_ack_recovery",
  "tests/integration/test_phase10_chaos_events.py::test_quarantine_commit_failure_and_one_replay",
  "tests/integration/test_phase10_chaos_dependencies.py::test_workflow_timer_and_approval_recovery",
  "tests/integration/test_phase10_chaos_dependencies.py::test_nats_and_temporal_dispatch_recovery",
  "tests/integration/test_phase10_chaos_dependencies.py::test_postgres_activity_commit_recovery",
  "tests/integration/test_phase10_chaos_dependencies.py::test_sandbox_orphan_and_socket_recovery",
  "tests/integration/test_phase10_chaos_key_rotation.py::test_master_key_active_read_recovery",
  "tests/integration/test_phase10_final_exit.py::test_restored_worker_restart_exit"
]
```

Set effect rules to `exactly_once` for scenarios 1, 3, 4, 5, 6, and 9; scenarios 2, 7, and 10 permit `at_most_once_or_execution_unknown`; scenario 8 permits `safe_failed_or_execution_unknown`. Record each sub-fault explicitly, including all two agent and three tool boundaries, the first quarantine-commit failure plus one replay request, both timer and approval waits, both NATS and Temporal restarts, and all three key-bearing service restarts.

`chaos-recovery.md` documents trigger, authority during outage, safe UI state, automatic recovery, operator escalation, effect rule, maximum wait, diagnostics, and cleanup for every row. It states that operators never enable test controls in production and never manually repeat an ambiguous external effect.

- [ ] **Step 4: Run GREEN and documentation parity**

```bash
uv run pytest tests/test_phase10_security_contract.py tests/test_phase10_chaos_contract.py -q
uv run ruff check tests/test_phase10_security_contract.py tests/test_phase10_chaos_contract.py
rg -n '^\| (secret|model|connector|webhook|dlq|replay|temporal|tool|nats|sandbox|telemetry|product|api|ui|backup|restore)\.' docs/security/phase10-secret-data-flow.md
rg -n '^### Scenario (0[1-9]|10):' docs/operations/chaos-recovery.md
```

Expected: PASS with 38 threat-model rows and ten recovery sections.

- [ ] **Step 5: Guard, stage exactly Task 1, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add ops/security/phase10-secret-sinks.json ops/chaos/phase10-scenarios.json docs/security/phase10-secret-data-flow.md docs/operations/chaos-recovery.md tests/test_phase10_security_contract.py tests/test_phase10_chaos_contract.py
git diff --cached --check
git commit -m "docs: freeze Phase 10 security recovery matrix"
```

Expected: commit 2 of 15 contains only contracts and reviewed threat/recovery documentation.

### Task 2: Build the Unique Canary Corpus and Shared Persistence Sanitizer

**Files:**
- Modify: `scripts/phase10_artifact.py`
- Create: `scripts/phase10_secret_audit.py`
- Create: `tests/test_phase10_secret_audit.py`
- Modify: `packages/secrets/src/jhin_secrets/__init__.py`
- Create: `packages/secrets/src/jhin_secrets/sanitize.py`
- Create: `packages/secrets/tests/test_sanitize.py`
- Modify: `packages/observability/src/jhin_observability/redaction.py`
- Modify: `packages/observability/tests/test_logging.py`
- Modify: `packages/tools/src/jhin_tools/sanitize.py`
- Modify: `packages/tools/tests/test_sanitize.py`
- Modify: `apps/web/lib/server-logger.ts`
- Modify: `apps/web/tests/server-logger.test.ts`
- Modify: `ops/images/resolved-images.json`
- Modify: `docs/evidence/phase10-hardening.md`
- Modify: `docs/evidence/phase10-image-security.json`
- Modify: `docs/evidence/phase10-sizing.json`
- Modify: `docs/evidence/phase10-telemetry.md`

**Interfaces:**
- Consumes: telemetry `CANARY_KINDS`/artifact validator, process known-value redactors, `jhin_tools.sanitize_payload`, and the server-only web logger.
- Produces: `SecretCanarySet`, all-form `CanaryScanner`, mode-0600 never-upload manifests, one dependency-light `sanitize_for_persistence` contract shared by Python metadata/error/recovery sinks with an equivalent server-only web structural pass, and refreshed image/scan/sizing/hardening/telemetry evidence for the changed runtime redaction paths.

- [ ] **Step 1: Write RED corpus, scanner, structural, cyclic, and ordering tests**

Create tests that pin unique generation and every encoded form:

```python
import base64
import json
from urllib.parse import quote, quote_plus

from scripts.phase10_secret_audit import SecretCanarySet


def test_every_canary_is_unique_and_all_forms_are_scanned() -> None:
    canaries = SecretCanarySet.create()
    assert len(canaries.values()) == 9
    assert len(set(canaries.values())) == 9
    for kind, value in canaries.by_kind().items():
        forms = canaries.forbidden_forms_for(kind)
        assert set(forms) == {
            "raw", "json_escaped", "url_percent", "form_encoded",
            "double_percent", "base64_standard", "base64_urlsafe",
        }
        encoded = quote(value, safe="")
        assert value.encode() in forms["raw"]
        assert json.dumps(value).encode() in forms["json_escaped"]
        assert encoded.encode() in forms["url_percent"]
        assert quote_plus(value).encode() in forms["form_encoded"]
        assert quote(encoded, safe="").encode() in forms["double_percent"]
        assert base64.b64encode(value.encode()) in forms["base64_standard"]
        assert base64.urlsafe_b64encode(value.encode()) in forms["base64_urlsafe"]
```

Pin pre-persistence structure and URL behavior in `packages/secrets/tests/test_sanitize.py`:

```python
from jhin_secrets.sanitize import REDACTED, sanitize_for_persistence


def test_unknown_nested_credential_keys_are_removed_before_serialization() -> None:
    value = {
        "safe": "visible",
        "nested": [{"unknownApiKey": "api-canary"}],
        "unknown-master-key": "master-canary",
        "endpoint": "https://dsn-user:dsn-pass@example.test/path?token=query#fragment",
    }
    assert sanitize_for_persistence(value) == {
        "safe": "visible",
        "nested": [{"unknownApiKey": REDACTED}],
        "unknown-master-key": REDACTED,
        "endpoint": "https://example.test/path",
    }
```

Add tests for mode `0600`, owner, symlink/refuse-existing behavior; empty/duplicate canaries; JSON/form/double-percent/base64 variants; substring collision rejection; recursion/cycle/depth/item/string caps; unsupported objects whose `__str__` contains a canary; camel/snake/kebab/dotted keys; `Set-Cookie`; DSN userinfo; known-value redactor ordering; fail-closed `contains_secret_material`; and violations that return only safe sink/form IDs.

Run:

```bash
uv run pytest tests/test_phase10_secret_audit.py packages/secrets/tests/test_sanitize.py packages/observability/tests/test_logging.py packages/tools/tests/test_sanitize.py -q
pnpm --filter jhin-web test -- server-logger.test.ts
```

Expected: FAIL on importing `SecretCanarySet` and `sanitize_for_persistence`.

- [ ] **Step 2: Implement the manifest and all-form scanner without weakening telemetry artifacts**

Keep telemetry's existing `CANARY_KINDS` and schema-v1 manifest accepted byte-for-byte. Add reusable bounded encoding helpers to `phase10_artifact.py`, then have `phase10_secret_audit.py` import them. Secret-audit manifests use `kind = "phase10_secret_canaries"`, exact `SECRET_CANARY_KINDS`, canonical sorted JSON, owner mode `0600`, no-follow open/fstat, a separate per-run 128-bit nonce, and at least 192 independent random bits in every category value. The nonce binds the manifest/run and need not be embedded in fixed-format values. Regenerate a category value until it has a distinct percent representation; generate `master_key_like` from exactly 32 random bytes in standard base64 so it is a valid synthetic master key.

Generate syntactically meaningful composites:

```python
from collections.abc import Mapping
from urllib.parse import quote


def build_composites(values: Mapping[str, str]) -> dict[str, str]:
    return {
        "authorization": f"Bearer {values['authorization']}",
        "cookie": f"session={values['cookie']}; HttpOnly; Secure",
        "private_key_fragment": (
            f"-----BEGIN PRIVATE KEY-----{values['private_key_fragment']}"
        ),
        "dsn": (
            f"postgresql://{quote(values['dsn_user'], safe='')}:"
            f"{quote(values['dsn_password'], safe='')}@db.invalid/jhin"
        ),
        "sandbox_env": f"PHASE10_SANDBOX_SECRET={values['sandbox_secret_env']}",
    }
```

The scanner operates on bounded byte chunks with an overlap equal to the longest forbidden form, so a canary split across subprocess/log stream chunks is detected. It accepts `bytes`, UTF-8 text, or canonical JSON objects, never decodes arbitrary binary with replacement before the byte scan, and caps a single inspected source at 64 MiB. A violation contains `sink_id` and form class only.

- [ ] **Step 3: Implement one structural-before-known-value persistence sanitizer**

Put key normalization, recursive bounds, URL stripping, safe primitive handling, and process known-value replacement in `jhin_secrets.sanitize`. Keep `jhin_observability.redaction.structural_redaction` as the dependency-light final-rendering defense with its exact telemetry constants/event registry, and add parity fixtures proving that it recognizes the same credential-key spellings without adding an observability-to-secrets dependency. `jhin_tools.sanitize_payload`, whose package already depends directly on `jhin-secrets`, delegates nested credential-key removal to the shared persistence function before applying its tool output allowlist/size cap. No reverse dependency from secrets to observability/tools is introduced.

The web server logger mirrors the exact normalized key set and ordering under `import "server-only"`; it never exports the registry to client bundles. Add a parity fixture in both Python and TypeScript for all `SENSITIVE_NESTED_KEYS`.

- [ ] **Step 4: Run GREEN and all affected redaction suites**

```bash
set -euo pipefail
phase10_redaction_oci_dir="$(mktemp -d)"
phase10_redaction_sizing_dir="$(mktemp -d)"
trap 'find "$phase10_redaction_oci_dir" "$phase10_redaction_sizing_dir" -depth -mindepth 1 -delete; rmdir "$phase10_redaction_oci_dir" "$phase10_redaction_sizing_dir"' EXIT
uv run pytest tests/test_phase10_secret_audit.py packages/secrets/tests packages/observability/tests/test_logging.py packages/tools/tests/test_sanitize.py -q
pnpm --filter jhin-web test -- server-logger.test.ts
uv run ruff check scripts/phase10_artifact.py scripts/phase10_secret_audit.py tests/test_phase10_secret_audit.py packages/secrets packages/observability/src/jhin_observability/redaction.py packages/observability/tests/test_logging.py packages/tools/src/jhin_tools/sanitize.py packages/tools/tests/test_sanitize.py
uv run mypy scripts/phase10_artifact.py scripts/phase10_secret_audit.py packages/secrets/src packages/observability/src packages/tools/src
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
uv run python scripts/build_phase10_images.py resolve --inventory ops/images/release-images.json --output ops/images/resolved-images.json
uv run python scripts/build_phase10_images.py build --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --output-dir "$phase10_redaction_oci_dir"
uv run python scripts/evaluate_phase10_vulnerabilities.py scan --oci-dir "$phase10_redaction_oci_dir" --allowlist ops/security/vulnerability-allowlist.json --evidence docs/evidence/phase10-image-security.json
uv run python scripts/build_phase10_images.py validate-evidence docs/evidence/phase10-image-security.json
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_redaction_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_redaction_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_redaction_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_redaction_rootful_gid" uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --oci-dir "$phase10_redaction_oci_dir" --socket-mode rootful --output "$phase10_redaction_oci_dir/rootful-runtime.env"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --oci-dir "$phase10_redaction_oci_dir" --socket-mode rootless --output "$phase10_redaction_oci_dir/rootless-runtime.env"
uv run python scripts/assert_phase10_production_compose.py
uv run python scripts/assert_phase10_command_inventory.py
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_redaction_rootful_gid" uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootful --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_redaction_oci_dir/rootful-runtime.env" --evidence "$phase10_redaction_sizing_dir/${profile}-rootful.json"
done
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootless --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_redaction_oci_dir/rootless-runtime.env" --evidence "$phase10_redaction_sizing_dir/${profile}-rootless.json"
done
uv run python scripts/run_phase10_sizing.py aggregate --input-dir "$phase10_redaction_sizing_dir" --output docs/evidence/phase10-sizing.json
uv run python scripts/run_phase10_sizing.py validate-evidence docs/evidence/phase10-sizing.json
uv run python scripts/record_phase10_hardening_evidence.py --output docs/evidence/phase10-hardening.md
uv run python scripts/record_phase10_hardening_evidence.py --check docs/evidence/phase10-hardening.md
uv run python scripts/record_phase10_telemetry_evidence.py
test -s docs/evidence/phase10-telemetry.md
! rg -n 'FAIL|INCOMPLETE|PENDING RESULT|not run' docs/evidence/phase10-telemetry.md
```

Expected: PASS; telemetry manifests remain compatible, unknown credential-shaped keys are removed before serialization, both profile-absent/observed live gates rerun, all changed runtime images pass both-architecture scanning, all ten rootful/rootless sizing cells and hardening evidence bind the new digests, and current evidence contains pass results only.

- [ ] **Step 5: Guard, stage exactly Task 2, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add scripts/phase10_artifact.py scripts/phase10_secret_audit.py tests/test_phase10_secret_audit.py packages/secrets/src/jhin_secrets/__init__.py packages/secrets/src/jhin_secrets/sanitize.py packages/secrets/tests/test_sanitize.py packages/observability/src/jhin_observability/redaction.py packages/observability/tests/test_logging.py packages/tools/src/jhin_tools/sanitize.py packages/tools/tests/test_sanitize.py apps/web/lib/server-logger.ts apps/web/tests/server-logger.test.ts ops/images/resolved-images.json docs/evidence/phase10-hardening.md docs/evidence/phase10-image-security.json docs/evidence/phase10-sizing.json docs/evidence/phase10-telemetry.md
git diff --cached --check
git commit -m "security: add cross-sink canary sanitizer"
```

Expected: commit 3 of 15 establishes the reusable security primitive without live Compose changes.

### Task 3: Enforce Sanitization at Persistence Boundaries and Remove Docker-Inspect Secrets

**Files:**
- Modify: `apps/api/src/jhin_api/audit/service.py`
- Modify: `apps/api/src/jhin_api/connections/service.py`
- Modify: `apps/api/src/jhin_api/models/service.py`
- Modify: `apps/api/src/jhin_api/public_payloads.py`
- Modify: `apps/api/src/jhin_api/webhooks/service.py`
- Create: `apps/api/tests/test_secret_persistence.py`
- Create: `apps/web/tests/secret-sink-audit.test.tsx`
- Modify: `packages/events/src/jhin_events/envelope.py`
- Modify: `packages/events/tests/test_envelope.py`
- Modify: `packages/models/src/jhin_models/base.py`
- Modify: `packages/models/src/jhin_models/providers/anthropic.py`
- Modify: `packages/models/src/jhin_models/providers/openai_compatible.py`
- Create: `packages/models/tests/test_secret_boundaries.py`
- Modify: `packages/connectors/src/jhin_connectors/http_client.py`
- Create: `packages/connectors/tests/test_secret_boundaries.py`
- Modify: `packages/workflows/pyproject.toml`
- Modify: `packages/workflows/src/jhin_workflows/__init__.py`
- Create: `packages/workflows/src/jhin_workflows/safe_failure_converter.py`
- Modify: `packages/workflows/src/jhin_workflows/temporal_connection.py`
- Create: `packages/workflows/tests/test_safe_failure_converter.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/projections.py`
- Create: `services/agent_worker/tests/test_secret_persistence.py`
- Modify: `services/event_worker/src/jhin_event_worker/delivery.py`
- Modify: `services/event_worker/src/jhin_event_worker/quarantine.py`
- Create: `services/event_worker/tests/test_secret_persistence.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/schemas.py`
- Create: `services/sandbox_runner/tests/test_secret_persistence.py`
- Create: `services/sandbox_runner/tests/test_secret_delivery.py`
- Modify: `docker/sandbox.Dockerfile`
- Create: `docker/sandbox_secret_exec.py`
- Modify: `ops/images/resolved-images.json`
- Modify: `docs/evidence/phase10-hardening.md`
- Modify: `docs/evidence/phase10-image-security.json`
- Modify: `docs/evidence/phase10-sizing.json`
- Modify: `docs/evidence/phase10-telemetry.md`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Task 2 sanitizer, telemetry safe-error contract, the runbooks plan's shared `connect_temporal`, durable event/quarantine state, tool sanitization, the current sandbox request/redactor lifecycle, and the settled release-image resolver/scanner and ten-profile sizing harness.
- Produces: sanitizer calls before every metadata/error/recovery write/serialization, one mandatory safe Temporal payload/failure converter for every client/worker, safe model/connector/webhook/UI failure projections, a tmpfs secret-file handoff that keeps sandbox secrets out of Docker container configuration/inspect while preserving the job process environment, and refreshed digest/scan/sizing/hardening/telemetry evidence for the changed runtime paths and sandbox image.

- [ ] **Step 1: Write RED persistence-order and HTTP/error projection tests**

Use recording ORM/session, publisher, Temporal converter, and exporter fakes that reject the canary at the call boundary. Pin this order in the API test:

```python
async def test_audit_metadata_is_sanitized_before_session_add(audit_world) -> None:
    await audit_world.record(metadata={"nested": {"unknownApiKey": audit_world.canary}})
    persisted = audit_world.added_row.metadata_json
    assert persisted == {"nested": {"unknownApiKey": "[REDACTED]"}}
    assert audit_world.renderer_calls == 0
```

Add equivalent tests for run events/tool results, event envelope headers/metadata, quarantine failure/outbox payload, replay metadata, public/protected API errors, and UI error cards. Model and connector tests use local `httpx.MockTransport` responses whose success JSON, error body, header, URL userinfo/query, exception `str`, and close failure contain every canary; only business-allowlisted sanitized success fields may survive and no error projection may contain a canary.

Run:

```bash
uv run pytest apps/api/tests/test_secret_persistence.py packages/events/tests/test_envelope.py packages/models/tests/test_secret_boundaries.py packages/connectors/tests/test_secret_boundaries.py packages/workflows/tests/test_safe_failure_converter.py services/agent_worker/tests/test_secret_persistence.py services/event_worker/tests/test_secret_persistence.py -q
pnpm --filter jhin-web test -- secret-sink-audit.test.tsx
```

Expected: FAIL because the new persistence-order assertions are not enforced at all boundaries.

- [ ] **Step 2: Write RED sandbox secret-delivery and orphan-inspection tests**

The pure config test must prove the Docker create payload contains no secret value or secret variable name:

```python
def test_secret_env_is_absent_from_container_config(sandbox_secret_world) -> None:
    config, archive = sandbox_secret_world.build()
    rendered = sandbox_secret_world.canonical_bytes(config)
    assert sandbox_secret_world.canary.encode() not in rendered
    assert b"PHASE10_SANDBOX_SECRET" not in rendered
    assert sandbox_secret_world.canary.encode() in archive
    assert config["Cmd"][0] == "/usr/local/bin/jhin-sandbox-secret-exec"
    assert config["HostConfig"]["Tmpfs"]["/run/jhin-secrets"].startswith("rw,noexec,nosuid,nodev")
```

The lifecycle fake asserts: start neutral wrapper -> upload one bounded tar stream in memory -> wrapper writes a consumed acknowledgment -> runner reaches normal wait; archive bytes never enter exceptions/logs; failure deletes the container; and startup reaping inspects only exact job labels. Validate secret names/values have per-entry and aggregate bounds and reject NUL/newline in names.

Run:

```bash
uv run pytest services/sandbox_runner/tests/test_secret_persistence.py services/sandbox_runner/tests/test_secret_delivery.py services/sandbox_runner/tests/test_job_config.py services/sandbox_runner/tests/test_job_lifecycle.py -q
```

Expected: FAIL because `secret_env` is currently embedded directly in Docker `Config.Env` and the wrapper does not exist.

- [ ] **Step 3: Apply the sanitizer immediately before every write/export**

Keep business-authoritative raw input only where required. Specifically:

- Audit metadata, run-event payloads, tool sanitized input/output, recovery failure detail, quarantine/outbox payloads, and public/protected error bodies pass through `sanitize_for_persistence` before ORM construction/assignment.
- `ModelProviderError` retains only provider enum, closed reason code, optional numeric HTTP status, and retryability. Anthropic/OpenAI-compatible request, stream, JSON, verify, and close paths discard response bodies/headers/URLs and raise `from None`; success parsing reads only the documented response fields and ignores unknown keys. The central connector HTTP boundary likewise never attaches a request/response/transport exception and exposes only its closed safe code/status while returning bounded success JSON to the connector's schema allowlist.
- Webhook signature/auth headers never enter an envelope. Provider payload keys identified as credential-bearing are redacted before NATS serialization; the normalizer still receives all noncredential fields needed for matching.
- `SafeTemporalPayloadConverter` rejects any registered workflow/activity argument, result, signal, or query payload for which `contains_secret_material` is true before the default converter runs. `SafeTemporalFailureConverter` preserves retry/timeout/cancellation semantics and a closed allowlisted `ApplicationError.type`, but replaces message/stack/cause text with `SafeError(type, code)`, structurally sanitizes registered safe detail objects, and recursively normalizes causes. `connect_temporal` installs the same immutable `DataConverter` for API and workflow/agent/tool/event clients and workers; callers cannot override it from environment. No `str(exc)`, args, raw cause, request/response, prompt, tool input/output, or DSN is serialized.
- Event envelope metadata is an exact allowlist; unknown keys are structurally sanitized or rejected before `to_bytes`.
- UI fixtures receive only closed reason/error codes and safe copy; no hidden/expandable/raw error field is emitted to the client.

Tests monkeypatch the final renderer to an identity function and still pass, proving safety occurs before persistence/export.

- [ ] **Step 4: Replace Docker environment injection with one-shot tmpfs handoff**

`docker/sandbox_secret_exec.py` is copied to `/usr/local/bin/jhin-sandbox-secret-exec`, owned by root and mode `0555`. It accepts exactly `secret-json-path`, `consumed-ack-path`, `--`, then at least one command argv. Because the tmpfs exists only after container start, the neutral wrapper polls an `O_RDONLY | O_CLOEXEC | O_NOFOLLOW` open every 25 ms against a monotonic ten-second deadline, retries only `ENOENT`, and exits with one closed numeric status and no output on timeout or any other error. Once opened, it requires a regular file owned by UID 1000 with mode `0400`, size at most 65,536 bytes, exact uppercase env names, bounded UTF-8 values, and no NUL. It unlinks the file, fsyncs the directory, writes/fsyncs the zero-byte acknowledgment with `O_CREAT | O_EXCL | O_NOFOLLOW` mode `0400`, closes descriptors, and calls `os.execvpe(command[0], command, {**os.environ, **secrets})`. It never prints or catches an exception with file contents.

The runner excludes `request.secret_env` from `Config.Env`, mounts `/run/jhin-secrets` as a UID/GID-1000 `noexec,nosuid,nodev` tmpfs capped at 64 KiB, substitutes the wrapper command, starts the container, and uploads one deterministic in-memory tar member with UID/GID 1000 and mode `0400`. It polls for the consumed acknowledgment for at most 10 seconds before normal job wait. The tar buffer is zeroed in `finally`; Docker exceptions are normalized without archive/config rendering. An image used with nonempty `secret_env` must carry inspected label `org.jhin.sandbox.secret-file-v1=true`; otherwise fail before container creation. The curated image declares that label. Jobs without secrets retain their original exec-form command.

After the wrapper acknowledgment, `docker inspect` contains only the wrapper path, secret-file path, original command, and nonsecret env. The live audit later scans inspect bytes, Docker events, stdout/stderr/status, and post-reap listings.

- [ ] **Step 5: Run GREEN, existing security regressions, and image contract**

```bash
set -euo pipefail
phase10_oci_dir="$(mktemp -d)"
phase10_resized_dir="$(mktemp -d)"
trap 'find "$phase10_oci_dir" "$phase10_resized_dir" -depth -mindepth 1 -delete; rmdir "$phase10_oci_dir" "$phase10_resized_dir"' EXIT
uv lock
uv run pytest apps/api/tests/test_secret_persistence.py packages/events/tests/test_envelope.py packages/models/tests/test_secret_boundaries.py packages/connectors/tests/test_secret_boundaries.py packages/workflows/tests/test_safe_failure_converter.py services/agent_worker/tests/test_secret_persistence.py services/event_worker/tests/test_secret_persistence.py services/sandbox_runner/tests/test_secret_persistence.py services/sandbox_runner/tests/test_secret_delivery.py services/sandbox_runner/tests -q
pnpm --filter jhin-web test -- secret-sink-audit.test.tsx
uv run pytest apps/api/tests packages/events/tests packages/models/tests packages/connectors/tests packages/workflows/tests services/agent_worker/tests services/event_worker/tests -q
uv run pytest tests/test_phase10_image_matrix.py tests/test_phase10_vulnerability_policy.py -q
uv run ruff check apps/api/src/jhin_api/audit/service.py apps/api/src/jhin_api/connections/service.py apps/api/src/jhin_api/models/service.py apps/api/src/jhin_api/public_payloads.py apps/api/src/jhin_api/webhooks/service.py apps/api/tests/test_secret_persistence.py packages/events packages/models/src/jhin_models/base.py packages/models/src/jhin_models/providers/anthropic.py packages/models/src/jhin_models/providers/openai_compatible.py packages/models/tests/test_secret_boundaries.py packages/connectors/src/jhin_connectors/http_client.py packages/connectors/tests/test_secret_boundaries.py packages/workflows/src/jhin_workflows/safe_failure_converter.py packages/workflows/src/jhin_workflows/temporal_connection.py packages/workflows/tests/test_safe_failure_converter.py services/agent_worker/src/jhin_agent_worker/projections.py services/agent_worker/tests/test_secret_persistence.py services/event_worker/src/jhin_event_worker/delivery.py services/event_worker/src/jhin_event_worker/quarantine.py services/event_worker/tests/test_secret_persistence.py services/sandbox_runner docker/sandbox_secret_exec.py
uv run mypy apps/api/src packages/events/src packages/models/src packages/connectors/src packages/workflows/src services/agent_worker/src services/event_worker/src services/sandbox_runner/src docker/sandbox_secret_exec.py
uv run python scripts/build_phase10_images.py resolve --inventory ops/images/release-images.json --output ops/images/resolved-images.json
uv run python scripts/build_phase10_images.py build --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --output-dir "$phase10_oci_dir"
uv run python scripts/evaluate_phase10_vulnerabilities.py scan --oci-dir "$phase10_oci_dir" --allowlist ops/security/vulnerability-allowlist.json --evidence docs/evidence/phase10-image-security.json
uv run python scripts/build_phase10_images.py validate-evidence docs/evidence/phase10-image-security.json
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
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
uv run python scripts/record_phase10_hardening_evidence.py --output docs/evidence/phase10-hardening.md
uv run python scripts/record_phase10_hardening_evidence.py --check docs/evidence/phase10-hardening.md
uv run python scripts/record_phase10_telemetry_evidence.py
test -s docs/evidence/phase10-telemetry.md
! rg -n 'FAIL|INCOMPLETE|PENDING RESULT|not run' docs/evidence/phase10-telemetry.md
```

Expected: PASS. A sandbox secret reaches only the intended process environment and redactor, never Docker inspect/config or a persisted/exported sink. All repository images and five external runtime images are rebuilt/scanned for both supported architectures, the sandbox label/command probe passes, and all ten rootful/rootless sizing cells are remeasured against the new immutable image digests before commit.

- [ ] **Step 6: Guard, stage exactly Task 3, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add apps/api/src/jhin_api/audit/service.py apps/api/src/jhin_api/connections/service.py apps/api/src/jhin_api/models/service.py apps/api/src/jhin_api/public_payloads.py apps/api/src/jhin_api/webhooks/service.py apps/api/tests/test_secret_persistence.py apps/web/tests/secret-sink-audit.test.tsx packages/events/src/jhin_events/envelope.py packages/events/tests/test_envelope.py packages/models/src/jhin_models/base.py packages/models/src/jhin_models/providers/anthropic.py packages/models/src/jhin_models/providers/openai_compatible.py packages/models/tests/test_secret_boundaries.py packages/connectors/src/jhin_connectors/http_client.py packages/connectors/tests/test_secret_boundaries.py packages/workflows/pyproject.toml packages/workflows/src/jhin_workflows/__init__.py packages/workflows/src/jhin_workflows/safe_failure_converter.py packages/workflows/src/jhin_workflows/temporal_connection.py packages/workflows/tests/test_safe_failure_converter.py services/agent_worker/src/jhin_agent_worker/projections.py services/agent_worker/tests/test_secret_persistence.py services/event_worker/src/jhin_event_worker/delivery.py services/event_worker/src/jhin_event_worker/quarantine.py services/event_worker/tests/test_secret_persistence.py services/sandbox_runner/src/jhin_sandbox_runner/jobs.py services/sandbox_runner/src/jhin_sandbox_runner/main.py services/sandbox_runner/src/jhin_sandbox_runner/schemas.py services/sandbox_runner/tests/test_secret_persistence.py services/sandbox_runner/tests/test_secret_delivery.py docker/sandbox.Dockerfile docker/sandbox_secret_exec.py ops/images/resolved-images.json docs/evidence/phase10-hardening.md docs/evidence/phase10-image-security.json docs/evidence/phase10-sizing.json docs/evidence/phase10-telemetry.md uv.lock
git diff --cached --check
git commit -m "security: sanitize persistence and sandbox secrets"
```

Expected: commit 4 of 15 contains the independently reviewable production hardening forced by the audit.

### Task 4: Run the Integrated Cross-Sink Secret Lifecycle Audit

**Files:**
- Create: `compose.phase10-secret-audit.yaml`
- Create: `tests/integration/phase10_secret_audit_harness.py`
- Create: `tests/integration/test_phase10_secret_audit.py`
- Create: `scripts/run_phase10_secret_audit.py`
- Create: `tests/test_phase10_security_evidence.py`
- Create: `docs/evidence/phase10-secret-audit.json`

**Interfaces:**
- Consumes: all 38 sink probes, Task 2 canaries/scanner, Task 3 persistence boundaries, telemetry `Stack`/observed profile, protected APIs, DLQ/replay, staged key rotation, encrypted backup/restore, current digest-pinned images, and `IsolatedComposeProject`.
- Produces: one fake-provider-only rootful/rootless audit that exercises success and failure paths, scans every live/durable/rendered/backup/restore sink in memory, and writes allowlisted schema-v1 evidence only after zero violations.

- [ ] **Step 1: Write RED evidence schema, harness isolation, and sink-completeness tests**

Pin the public evidence shape:

```python
import json
from pathlib import Path


def test_secret_audit_evidence_covers_every_sink_and_mode() -> None:
    document = json.loads(Path("docs/evidence/phase10-secret-audit.json").read_text())
    assert set(document) == {
        "schema_version", "commit", "image_set_sha256", "canary_schema",
        "modes", "status",
    }
    assert document["schema_version"] == 1
    assert document["canary_schema"] == {"kinds": 9, "encoded_forms_minimum": 7}
    assert [row["socket_mode"] for row in document["modes"]] == ["rootful", "rootless"]
    for row in document["modes"]:
        assert set(row) == {
            "socket_mode", "sink_count", "secret_lifecycle", "success_paths",
            "failure_paths", "structural_redaction", "canary_violations", "status",
        }
        assert row["sink_count"] == 38
        assert row["secret_lifecycle"] == {
            "created": True, "encrypted": True, "ordinary_use": True,
            "rotated": True, "backed_up": True, "restored": True,
            "restored_use": True,
        }
        assert row["canary_violations"] == 0
        assert row["status"] == "pass"
    assert document["status"] == "pass"
```

Unit-test `run_phase10_secret_audit.py` with a recording `IsolatedComposeProject` factory. Require unique project/namespace/keyring/canary paths per mode, current resolved images, dynamic ports, `COMPOSE_DISABLE_ENV_FILE=1`, fake provider endpoints only, bounded commands, and teardown after both success and scanner failure. Its optional `--resolved-images`/`--runtime-image-env` flags are the unchanged runbooks pair: either both valid files are present or neither is; evidence-producing commands below always pass both. Assert canary manifest, backup, keyring, and restore workspace paths are never evidence/upload paths, and that raw producer output has no filesystem destination at all.

Pin two and only two integration nodes: `tests/integration/test_phase10_secret_audit.py::test_phase10_secret_lifecycle_audit` for `run`, and `tests/integration/test_phase10_secret_audit.py::test_phase10_secret_canary_smoke` for `pr-smoke`. Recording subprocess tests require argv `uv run pytest -o addopts= -m integration NODE -q` with exactly one full node, never a file or multiple nodes. A tiny in-process pytest plugin reports only closed collection/outcome counters through a private pipe; the runner requires `collected = passed = 1` and `deselected = 0`, and rejects absent marker/addopts override, selection drift, skip, xfail, or a nonzero exit. The smoke node uses the same scanner and every canary/form kind against a bounded fake-only representative of every sink family; it creates no checked-in evidence.

Run:

```bash
uv run pytest tests/test_phase10_security_evidence.py --collect-only -q
uv run pytest -o addopts='' -m integration tests/integration/test_phase10_secret_audit.py::test_phase10_secret_lifecycle_audit --collect-only -q
uv run pytest -o addopts='' -m integration tests/integration/test_phase10_secret_audit.py::test_phase10_secret_canary_smoke --collect-only -q
```

Expected: FAIL because the runner, overlay, harness, integration test, and evidence do not exist.

- [ ] **Step 2: Build the isolated observed-stack audit topology**

`compose.phase10-secret-audit.yaml` is always last in the exact ordered vector `("compose.yaml", "compose.operations.yaml", f"compose.{socket_mode}.yaml", "compose.phase10-secret-audit.yaml")`, with the existing `observability` profile and deterministic repository fake providers enabled. It publishes only dynamic loopback Caddy/telemetry test ports through the runbooks harness. It has no third-party DNS target, provider token, development default password, fixed port, fixed volume, or test-control setting.

The harness generates the real synthetic keyring from the `master_key_like` canary, mounts it through the settled owner/mode-safe path, and mounts the canary manifest read-only only into the fake-provider fixtures that must return adversarial values. Application services do not receive a general canary registry. Every fake provider records an in-memory effect ledger keyed by the stable invocation/message identity and exposes only an internal test-network count endpoint.

- [ ] **Step 3: Drive every success, failure, lifecycle, retry, replay, and rendering path**

Within one project and one canary corpus, execute this exact order:

1. Create API/model/connector/webhook/DSN credentials containing the typed canaries plus nested unknown credential keys. Query PostgreSQL directly and prove plaintext absent while ciphertext is nonempty.
2. Use each credential normally against local model, connector, webhook, database, and sandbox fakes. Drive one successful model answer, one successful connector effect, one signed webhook, one approval-gated tool, one sandbox job whose secret appears in attempted stdout/stderr, and one ordinary decrypt/use.
3. Return adversarial model/connector HTTP error bodies, headers, request IDs, URLs, close errors, malformed JSON, and nested fields containing every canary. Drive webhook rejection, activity failure, tool denial, approval rejection, sandbox Docker error, and public/protected API errors.
4. Exhaust one event handler, complete quarantine/DLQ/outbox, remediate, submit the same replay idempotency key twice, and prove one replay command/outcome. This audit path does not inject a failed quarantine commit; scenario 04 owns that later.
5. Complete the ordinary staged `1 -> (1,2) -> 2 -> (2)` key protocol with row verification but no chaos restart, keeping active decrypt/use throughout.
6. Take a maintenance-window encrypted backup with separate keyring recipients, scan ciphertext/manifests/stdout/stderr, restore into a fresh second project, scan restore output, and use the restored credential through its normal fake connector path.
7. Render the actual server/UI error-card fixtures and fetch every public/protected API response used by the run. Cross-workspace and unauthenticated probes must remain denied/opaque.

Across the success/error/header/metadata/env fixtures, cycle every kind through all seven `CANARY_FORM_KINDS`, including nested JSON, form bodies, once- and twice-percent-encoded URL fields, and both base64 alphabets. Boundary assertions inspect the exact pre-serialization object/bytes as well as the eventual sink, so an encoded canary cannot pass merely because a later renderer hides it.

No injection writes a canary into a core product field that is intentionally user-visible; adversarial values enter only credential, metadata, provider-body, error, sanitized-output, header, env, and unknown-key surfaces under audit.

- [ ] **Step 4: Scan all 38 sinks before any evidence write**

The harness scans, in this order, without printing source contents:

1. JSON stdout from API, web server, workflow/agent/tool/event workers, sandbox-runner, and all fake providers;
2. Tempo trace names, attributes, events, links, and status; Prometheus metric names/labels/exemplars;
3. every public/protected API body/header and server-rendered UI fixture;
4. every nonbinary PostgreSQL application column plus ciphertext bytes, using schema introspection and failing if an unregistered text/JSON column appears;
5. complete Jhin-owned Temporal histories/failures for the run, decoded through the registered data converter;
6. all messages and headers in the unique project's Jhin NATS streams, including ingress, canonical events, DLQ, and replay notification;
7. sandbox request/status/log response, container inspect/events, `docker top` command fields, wrapper tmpfs listing after consumption, and post-start orphan-reap listing;
8. backup ciphertext/manifests/command output and restore command output/evidence plus the restored authority scans.

For PostgreSQL/Temporal/NATS/Docker binary values, scan raw bytes before safe decoding. Enforce 64-MiB per-source and 512-MiB total bounds. Diagnostics retain only sink ID, safe probe code, item count, and duration. A single raw/encoded match aborts evidence generation and still tears down both projects.

- [ ] **Step 5: Run GREEN in both socket modes and aggregate safe evidence**

```bash
set -euo pipefail
phase10_audit_result_dir="$(mktemp -d)"
trap 'find "$phase10_audit_result_dir" -depth -mindepth 1 -delete; rmdir "$phase10_audit_result_dir"' EXIT
uv run pytest tests/test_phase10_security_evidence.py --collect-only -q
uv run pytest -o addopts='' -m integration tests/integration/test_phase10_secret_audit.py::test_phase10_secret_lifecycle_audit --collect-only -q
uv run pytest -o addopts='' -m integration tests/integration/test_phase10_secret_audit.py::test_phase10_secret_canary_smoke --collect-only -q
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_audit_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_audit_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_audit_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_audit_rootful_gid" uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --socket-mode rootful --output "$phase10_audit_result_dir/rootful-runtime.env"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --socket-mode rootless --output "$phase10_audit_result_dir/rootless-runtime.env"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_audit_rootful_gid" uv run python scripts/run_phase10_secret_audit.py run --socket-mode rootful --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_audit_result_dir/rootful-runtime.env" --result "$phase10_audit_result_dir/phase10-secret-audit-rootful.json"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_secret_audit.py run --socket-mode rootless --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_audit_result_dir/rootless-runtime.env" --result "$phase10_audit_result_dir/phase10-secret-audit-rootless.json"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_audit_rootful_gid" uv run python scripts/run_phase10_secret_audit.py pr-smoke --socket-mode rootful
uv run python scripts/run_phase10_secret_audit.py aggregate --input "$phase10_audit_result_dir/phase10-secret-audit-rootful.json" --input "$phase10_audit_result_dir/phase10-secret-audit-rootless.json" --output docs/evidence/phase10-secret-audit.json
uv run python scripts/run_phase10_secret_audit.py validate-evidence docs/evidence/phase10-secret-audit.json
uv run pytest tests/test_phase10_security_evidence.py -q
uv run ruff check scripts/run_phase10_secret_audit.py tests/integration/phase10_secret_audit_harness.py tests/integration/test_phase10_secret_audit.py tests/test_phase10_security_evidence.py
uv run mypy scripts/run_phase10_secret_audit.py tests/integration/phase10_secret_audit_harness.py
```

Expected: PASS twice with 38/38 sinks, the complete secret lifecycle, all success/failure paths, structural redaction, and zero violations. The private result directory contains allowlisted JSON only and is removed after aggregation.

- [ ] **Step 6: Guard, stage exactly Task 4, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add compose.phase10-secret-audit.yaml tests/integration/phase10_secret_audit_harness.py tests/integration/test_phase10_secret_audit.py scripts/run_phase10_secret_audit.py tests/test_phase10_security_evidence.py docs/evidence/phase10-secret-audit.json
git diff --cached --check
git commit -m "test: audit every Phase 10 secret sink"
```

Expected: commit 5 of 15 records only validated cross-sink evidence; no raw capture or canary manifest is staged.

### Task 5: Add Isolated Nonproduction Failpoints and Reject Every Chaos Setting

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `packages/test_controls/pyproject.toml`
- Create: `packages/test_controls/src/jhin_test_controls/__init__.py`
- Create: `packages/test_controls/src/jhin_test_controls/failpoints.py`
- Create: `packages/test_controls/tests/test_failpoints.py`
- Modify: `apps/api/pyproject.toml`
- Modify: `apps/api/src/jhin_api/settings.py`
- Modify: `services/workflow_worker/pyproject.toml`
- Modify: `services/workflow_worker/src/jhin_workflow_worker/settings.py`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/projections.py`
- Create: `services/agent_worker/tests/test_test_failpoints.py`
- Modify: `services/tool_worker/pyproject.toml`
- Modify: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Modify: `services/event_worker/pyproject.toml`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/delivery.py`
- Modify: `services/event_worker/src/jhin_event_worker/quarantine.py`
- Create: `services/event_worker/tests/test_test_failpoints.py`
- Modify: `services/sandbox_runner/pyproject.toml`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- Create: `services/sandbox_runner/tests/test_test_failpoints.py`
- Create: `compose.phase10-chaos.yaml`
- Modify: `scripts/assert_phase10_production_compose.py`
- Modify: `tests/test_production_configuration.py`
- Create: `tests/test_phase10_chaos_controls.py`
- Modify: `ops/images/resolved-images.json`
- Modify: `docs/evidence/phase10-hardening.md`
- Modify: `docs/evidence/phase10-image-security.json`
- Modify: `docs/evidence/phase10-secret-audit.json`
- Modify: `docs/evidence/phase10-sizing.json`
- Modify: `docs/evidence/phase10-telemetry.md`

**Interfaces:**
- Consumes: original seven-name `jhin_tools.test_barriers` API, settings fail-closed production rules, exact durable commit boundaries, sandbox post-secret-consumption lifecycle, base/rootful/rootless Compose, and the settled image/scan/sizing/hardening/telemetry evidence generators.
- Produces: dependency-light `jhin_test_controls`, five exact new failpoints, an exclusive one-service control-overlay writer, all-service runtime prefix rejection, a checked-in test-topology overlay, static proof that production has no setting/mount/endpoint/fake, and refreshed release/runtime/secret-audit evidence for the changed service images.

- [ ] **Step 1: Write RED filesystem, exact-match, one-shot, and production rejection tests**

Pin wait and one-shot behavior:

```python
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from jhin_test_controls import (
    InjectedTestFailure,
    TestFailpointAction,
    TestFailpointConfig,
    TestFailpointName,
    TestFailpoints,
)


async def wait_for_marker(path: Path) -> None:
    for _ in range(100):
        if path.is_file():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("marker_not_observed")


async def test_wait_failpoint_requires_exact_identity_and_release(tmp_path) -> None:
    identity = str(uuid4())
    name = TestFailpointName.EVENT_AFTER_HANDLER_BEFORE_ACK
    points = TestFailpoints(TestFailpointConfig(
        root=tmp_path,
        selected=name,
        match_identity=identity,
        action=TestFailpointAction.WAIT,
    ))
    waiting = asyncio.create_task(points.reach(name, identity))
    marker_stem = f"{name.value}--{identity}"
    await wait_for_marker(tmp_path / f"{marker_stem}.arrived")
    (tmp_path / f"{marker_stem}.release").touch(mode=0o600, exist_ok=False)
    await asyncio.wait_for(waiting, timeout=1)


async def test_raise_once_fails_once_in_one_process(tmp_path) -> None:
    identity = str(uuid4())
    points = TestFailpoints(TestFailpointConfig(
        root=tmp_path,
        selected=TestFailpointName.EVENT_BEFORE_QUARANTINE_COMMIT,
        match_identity=identity,
        action=TestFailpointAction.RAISE_ONCE,
    ))
    with pytest.raises(InjectedTestFailure, match="^injected_test_failure$"):
        await points.reach(TestFailpointName.EVENT_BEFORE_QUARANTINE_COMMIT, identity)
    await points.reach(TestFailpointName.EVENT_BEFORE_QUARANTINE_COMMIT, identity)
```

The test module's `wait_for_marker` is test-only and contains no secret-bearing diagnostics. Add cases for partial config, relative/wrong root, symlink/irregular marker, wrong identity/name, duplicate process, invalid action, sandbox job ID, cancellation, and disabled no-op.

Parameterize API, agent/tool/event/workflow worker, and sandbox-runner settings over every prefix and both empty/nonempty values with `APP_ENV=production`; each construction must raise the same `test_controls_forbidden` safe code. Also assert every original `CrashBarrierName` value/API remains exact.

Run:

```bash
uv run pytest packages/test_controls/tests/test_failpoints.py services/agent_worker/tests/test_test_failpoints.py services/event_worker/tests/test_test_failpoints.py services/sandbox_runner/tests/test_test_failpoints.py tests/test_phase10_chaos_controls.py tests/test_production_configuration.py -q
```

Expected: FAIL because `jhin_test_controls`, new settings, overlay, and production prefix rejection do not exist.

- [ ] **Step 2: Implement the neutral package and all-service startup guard**

Add `packages/test_controls` to the uv workspace, Ruff sources, mypy files, and direct dependencies of API/workflow/agent/tool/event/sandbox packages. It has no dependency on application, database, Temporal, NATS, Docker, observability, secrets, or tools.

Implement `reject_test_control_environment(app_env: str, environ: Mapping[str, str]) -> None`. When normalized environment is `production` or `prod`, it scans keys only and raises `TestControlConfigurationError("test_controls_forbidden")` if any key starts with the three forbidden prefixes—even if the value is empty. It never includes the key/value in the exception. Every service settings validator calls it against the captured process environment before accepting configuration.

Marker creation/open uses dirfd-relative `os.open` with `O_NOFOLLOW | O_CLOEXEC`, direct-child validated names, owner/mode checks, atomic create, file+directory fsync, and an async 50-ms poll with a 120-second internal ceiling. Production code exports no test wait/release helper; the test-only helpers above create marker files directly using the documented filename contract.

- [ ] **Step 3: Wire the five exact boundaries without changing authority**

- Agent projection calls `AGENT_BEFORE_ACTIVITY_COMMIT` with the stable run UUID after the complete durable bundle is staged and immediately before transaction commit. The barrier is outside any external model/tool call; PostgreSQL restart determines commit-or-retry.
- Event delivery calls `EVENT_AFTER_HANDLER_BEFORE_ACK` with canonical event UUID after durable handler success/processing completion and before `msg.ack()`.
- `commit_quarantine` calls `EVENT_BEFORE_QUARANTINE_COMMIT` with event UUID after failure/outbox/audit/completed rows are staged inside one transaction and before commit. `raise_once` rolls back that whole transaction; `quarantine_only` from the fifth handler failure remains authoritative.
- A completed-quarantine redelivery calls `EVENT_COMPLETED_BEFORE_TERM` with the canonical event UUID after observing durable `completed` and immediately before `msg.term()`. While that exact delivery waits, the independent production outbox reconciler may publish the DLQ notification; the delivery path never waits on, republishes, or authorizes the outbox. After SIGKILL, redelivery re-observes the same completed state and terms under the existing message identity.
- Sandbox runner calls `SANDBOX_AFTER_CONTAINER_START` with job ID only after the secret wrapper acknowledgment and actual job process start, before waiting for completion. SIGKILL leaves one exact-labelled orphan for startup reaping.

Controllers are constructed once per process from validated settings and injected explicitly. Disabled controllers are no-ops. No environment is reread after startup, no existing process can be toggled, and no endpoint/command/signal mutates configuration.

- [ ] **Step 4: Create the test topology and exclusive per-process control overlay**

`compose.phase10-chaos.yaml` contains only isolated fake providers, dynamic-port test routing, and test resource/network overrides. It contains zero `JHIN_TEST_*`/`JHIN_CHAOS_*` key and zero test-control mount. `TestControlOverlay.write` emits the one ephemeral final overlay described in Shared Interfaces; YAML parsing tests require one selected service, exact environment key set, one exact mount, no anchors/extensions/ports/images/commands/privileges, and no second service. A harness recreates only that target with one selected family. Post-kill recreation force-recreates from the checked-in vector with the ephemeral file omitted, then unlinks the file and removes the empty private control directory.

Static tests render:

1. production base + rootful;
2. production base + rootless;
3. production base + operations;
4. production base + chaos topology;
5. production base + chaos topology + one generated exact control overlay.

The first three contain zero forbidden keys, mount targets, fake services, or public fault routes. The fourth is rejected as production because it contains fake services even though it has no control key. The fifth is accepted only with `APP_ENV=test`; constructing every selected service's settings under `APP_ENV=production` rejects the generated keys, including empty-present mutations. Route introspection proves no path contains `chaos`, `failpoint`, `fault`, or `test-control`.

- [ ] **Step 5: Run GREEN, original barrier regressions, and static Compose proof**

```bash
set -euo pipefail
phase10_control_oci_dir="$(mktemp -d)"
phase10_control_sizing_dir="$(mktemp -d)"
phase10_control_audit_dir="$(mktemp -d)"
trap 'find "$phase10_control_oci_dir" "$phase10_control_sizing_dir" "$phase10_control_audit_dir" -depth -mindepth 1 -delete; rmdir "$phase10_control_oci_dir" "$phase10_control_sizing_dir" "$phase10_control_audit_dir"' EXIT
uv lock
uv run pytest packages/test_controls/tests/test_failpoints.py packages/tools/tests/test_crash_barriers.py services/agent_worker/tests/test_test_failpoints.py services/event_worker/tests/test_test_failpoints.py services/sandbox_runner/tests/test_test_failpoints.py tests/test_phase10_chaos_controls.py tests/test_production_configuration.py -q
uv run pytest tests/test_phase10_image_matrix.py tests/test_phase10_vulnerability_policy.py -q
uv run ruff check packages/test_controls apps/api/src/jhin_api/settings.py services/workflow_worker/src/jhin_workflow_worker/settings.py services/agent_worker services/tool_worker/src/jhin_tool_worker/settings.py services/event_worker services/sandbox_runner tests/test_phase10_chaos_controls.py tests/test_production_configuration.py
uv run mypy packages/test_controls/src apps/api/src services/workflow_worker/src services/agent_worker/src services/tool_worker/src services/event_worker/src services/sandbox_runner/src
uv run python scripts/build_phase10_images.py resolve --inventory ops/images/release-images.json --output ops/images/resolved-images.json
uv run python scripts/build_phase10_images.py build --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --output-dir "$phase10_control_oci_dir"
uv run python scripts/evaluate_phase10_vulnerabilities.py scan --oci-dir "$phase10_control_oci_dir" --allowlist ops/security/vulnerability-allowlist.json --evidence docs/evidence/phase10-image-security.json
uv run python scripts/build_phase10_images.py validate-evidence docs/evidence/phase10-image-security.json
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_control_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_control_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_control_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_control_rootful_gid" uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --oci-dir "$phase10_control_oci_dir" --socket-mode rootful --output "$phase10_control_oci_dir/rootful-runtime.env"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --oci-dir "$phase10_control_oci_dir" --socket-mode rootless --output "$phase10_control_oci_dir/rootless-runtime.env"
uv run python scripts/assert_phase10_production_compose.py
uv run python scripts/assert_phase10_command_inventory.py
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_control_rootful_gid" uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootful --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_control_oci_dir/rootful-runtime.env" --evidence "$phase10_control_sizing_dir/${profile}-rootful.json"
done
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootless --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_control_oci_dir/rootless-runtime.env" --evidence "$phase10_control_sizing_dir/${profile}-rootless.json"
done
uv run python scripts/run_phase10_sizing.py aggregate --input-dir "$phase10_control_sizing_dir" --output docs/evidence/phase10-sizing.json
uv run python scripts/run_phase10_sizing.py validate-evidence docs/evidence/phase10-sizing.json
uv run python scripts/record_phase10_hardening_evidence.py --output docs/evidence/phase10-hardening.md
uv run python scripts/record_phase10_hardening_evidence.py --check docs/evidence/phase10-hardening.md
uv run python scripts/record_phase10_telemetry_evidence.py
test -s docs/evidence/phase10-telemetry.md
! rg -n 'FAIL|INCOMPLETE|PENDING RESULT|not run' docs/evidence/phase10-telemetry.md
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_control_rootful_gid" uv run python scripts/run_phase10_secret_audit.py run --socket-mode rootful --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_control_oci_dir/rootful-runtime.env" --result "$phase10_control_audit_dir/rootful.json"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_secret_audit.py run --socket-mode rootless --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_control_oci_dir/rootless-runtime.env" --result "$phase10_control_audit_dir/rootless.json"
uv run python scripts/run_phase10_secret_audit.py aggregate --input "$phase10_control_audit_dir/rootful.json" --input "$phase10_control_audit_dir/rootless.json" --output docs/evidence/phase10-secret-audit.json
uv run python scripts/run_phase10_secret_audit.py validate-evidence docs/evidence/phase10-secret-audit.json
```

Expected: PASS. The original seven barriers are unchanged, new controls work only in exact test processes, production rejects every chaos key, all changed service images pass both-architecture scans, all ten sizing cells are current, and hardening/telemetry/38-sink secret-audit evidence is regenerated before commit.

- [ ] **Step 6: Guard, stage exactly Task 5, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add pyproject.toml uv.lock packages/test_controls/pyproject.toml packages/test_controls/src/jhin_test_controls/__init__.py packages/test_controls/src/jhin_test_controls/failpoints.py packages/test_controls/tests/test_failpoints.py apps/api/pyproject.toml apps/api/src/jhin_api/settings.py services/workflow_worker/pyproject.toml services/workflow_worker/src/jhin_workflow_worker/settings.py services/agent_worker/pyproject.toml services/agent_worker/src/jhin_agent_worker/settings.py services/agent_worker/src/jhin_agent_worker/resources.py services/agent_worker/src/jhin_agent_worker/projections.py services/agent_worker/tests/test_test_failpoints.py services/tool_worker/pyproject.toml services/tool_worker/src/jhin_tool_worker/settings.py services/event_worker/pyproject.toml services/event_worker/src/jhin_event_worker/settings.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/src/jhin_event_worker/delivery.py services/event_worker/src/jhin_event_worker/quarantine.py services/event_worker/tests/test_test_failpoints.py services/sandbox_runner/pyproject.toml services/sandbox_runner/src/jhin_sandbox_runner/settings.py services/sandbox_runner/src/jhin_sandbox_runner/jobs.py services/sandbox_runner/tests/test_test_failpoints.py compose.phase10-chaos.yaml scripts/assert_phase10_production_compose.py tests/test_production_configuration.py tests/test_phase10_chaos_controls.py ops/images/resolved-images.json docs/evidence/phase10-hardening.md docs/evidence/phase10-image-security.json docs/evidence/phase10-secret-audit.json docs/evidence/phase10-sizing.json docs/evidence/phase10-telemetry.md
git diff --cached --check
git commit -m "test: add isolated Phase 10 failpoints"
```

Expected: commit 6 of 15 is a reviewable test-control boundary; no public behavior or schema changes.

### Task 6: Build the Bounded Chaos Harness, Authority Assertions, and Safe Diagnostics

**Files:**
- Create: `tests/integration/phase10_chaos_assertions.py`
- Create: `tests/integration/phase10_chaos_harness.py`
- Create: `scripts/run_phase10_chaos.py`
- Create: `scripts/phase10_chaos_artifact.py`
- Create: `tests/test_phase10_chaos_harness.py`
- Create: `tests/test_phase10_chaos_artifact.py`

**Interfaces:**
- Consumes: scenario/sink registries, `IsolatedComposeProject`, both test-control families, protected health, PostgreSQL/Temporal/NATS/fake-provider query seams, Task 2 scanner, and current images.
- Produces: `ChaosHarness`, `AuthoritySnapshot`, bounded `eventually`, exact process/dependency restart primitives, `exact_integration_argv`, `validate_safe_pytest_summary`, a one-scenario/full-matrix runner, and an artifact sanitizer that never writes raw diagnostics.

- [ ] **Step 1: Write RED isolation, polling, assertion, command, and artifact tests**

Pin timeout behavior with a deterministic fake clock:

```python
import pytest

from scripts.run_phase10_chaos import (
    ChaosRunnerError,
    exact_integration_argv,
    validate_safe_pytest_summary,
)
from tests.integration.phase10_chaos_assertions import (
    WORKER_LOSS_TIMEOUT_SECONDS,
    WORKER_RECOVERY_TIMEOUT_SECONDS,
    eventually,
)


def test_worker_loss_and_recovery_deadlines_are_distinct() -> None:
    assert WORKER_LOSS_TIMEOUT_SECONDS == 50
    assert WORKER_RECOVERY_TIMEOUT_SECONDS == 65


async def test_eventually_times_out_with_safe_diagnostics(fake_clock) -> None:
    calls = 0

    async def probe() -> int:
        nonlocal calls
        calls += 1
        return 0

    with pytest.raises(TimeoutError, match="^nats-drain: condition_not_met$"):
        await eventually(
            "nats-drain",
            probe,
            lambda value: value == 1,
            timeout_seconds=3,
            diagnostics=lambda: {"safe_code": "condition_not_met", "count": calls},
            clock=fake_clock,
        )
    assert calls >= 2


def test_exact_integration_argv_overrides_addopts_and_selects_one_node() -> None:
    node = (
        "tests/integration/test_phase10_chaos_workers.py"
        "::test_agent_manifest_bind_recovery"
    )
    assert exact_integration_argv(node) == [
        "uv", "run", "pytest", "-o", "addopts=", "-m", "integration", node, "-q",
    ]
    assert validate_safe_pytest_summary(
        {
            "collected": 1,
            "deselected": 0,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "xfailed": 0,
        }
    ) is None


def test_exact_integration_summary_rejects_one_deselection() -> None:
    with pytest.raises(ChaosRunnerError, match="^pytest_selection_mismatch$"):
        validate_safe_pytest_summary(
            {
                "collected": 1,
                "deselected": 1,
                "passed": 1,
                "failed": 0,
                "skipped": 0,
                "xfailed": 0,
            }
        )
```

Use a recording Compose factory to assert each scenario gets a distinct valid project, private Docker config, dynamic ports, unique namespace/keyring/canary/effect ledger, exact overlay order, explicit socket mode, current image set, and teardown. Record command argv and prove no shell, fixed project, default `.env`, unresolved secret, or broad Docker cleanup.

Add table-driven runner tests for all ten registered nodes and the three PR-smoke selections. `run_exact_integration_node` accepts one registry-owned full `FILE::FUNCTION` node only and constructs exactly `uv run pytest -o addopts= -m integration NODE -q`; file-only paths, multiple nodes, unknown nodes, another marker, inherited repository addopts, or extra selection arguments fail before subprocess launch. A private-FD pytest plugin emits only `{collected, deselected, passed, failed, skipped, xfailed}` counters. The runner requires `collected = passed = 1`, every other counter including `deselected` zero, and a zero process exit. Tests simulate repository addopts that exclude integration and prove the override still selects exactly one node; they also prove a marker typo or deselection cannot be reported as GREEN.

Add fake-clock health tests for `wait_for_worker_loss` and every recovery helper. For `agent-worker`, `tool-worker`, and `event-worker`, pin the predecessor boundary exactly: the killed instance's heartbeat is still fresh at age 30.000000 seconds, becomes stale only when age is strictly greater than 30 seconds, and loss must be observed by a 50-second monotonic deadline. A retained or recently accessed Temporal poller may remain nonzero throughout and must not satisfy loss or recovery. Recovery requires a different boot-scoped instance ID with a fresh heartbeat plus the service's actual queue capability/poller within a separate 65-second monotonic deadline; the old retained poller is diagnostic only. For credential-free `workflow-worker`, assert `fresh_owner_instances is None`, never infer liveness from retained/recent counts, and prove recovery by an actual new workflow-task execution on the queue within 65 seconds. Add the equivalent sandbox reachability/reap recovery case with the same 65-second deadline. An AST-based test scans the harness and all scenario call sites: every loss call must use `WORKER_LOSS_TIMEOUT_SECONDS`, every heartbeat-owner/queue-capability/workflow-capability/sandbox recovery call must use `WORKER_RECOVERY_TIMEOUT_SECONDS`, and literal or default recovery bounds of 50 are rejected. Timeout diagnostics expose only service, closed reason code, elapsed bucket, fresh-owner count, and retained/recent poller counts.

Artifact tests feed bounded in-memory service-output chunks containing identifiers/free text/canaries and require either rejection or a normalized summary containing only service, registered event, safe error code, and count. Feed valid in-memory status/health/authority/pytest-event summaries and assert canonical JSON succeeds. A recording filesystem must observe no open/write for Compose status, logs, pytest/JUnit, histories, messages, scans, or any other pre-sanitization diagnostic. Run publication tests as the ordinary runner UID with an empty effective-capability set. They preflight `/proc/self/fd`, reject missing/unmounted/unusable procfs before any producer starts, reject a filesystem without `O_TMPFILE`, and prove a pre-existing destination returns the closed `artifact_exists` failure without replacement. Inject exceptions and SIGKILL at capture, scan, reduction, anonymous-artifact write, immediately before the procfs link, and immediately after it; the public artifact path and upload directory must remain absent/empty until a fully scanned closed-schema object is atomically linked, and a post-link crash leaves exactly the complete validated inode. No absolute path, URL, DSN, ID, log message, stack, trace, header, payload, or canary may survive.

Run:

```bash
uv run pytest tests/test_phase10_chaos_harness.py tests/test_phase10_chaos_artifact.py -q
```

Expected: FAIL because harness, polling, assertion, runner, and artifact modules do not exist.

- [ ] **Step 2: Implement generic isolated lifecycle and exact service controls**

`ChaosHarness.create(scenario_id, socket_mode)` validates the registry row, enters `isolated_project` with the exact ordered steady-state vector `("compose.yaml", f"compose.{socket_mode}.yaml", "compose.phase10-chaos.yaml")` and the existing `observability` profile, loads current digests, creates canaries/effect ledger, starts only the required services, and waits at most 120 seconds for base protected health. `IsolatedComposeProject` passes every file explicitly with `-f`; it does not discover a default file or load `compose.dev.yaml`/`.env`. Arming one process appends the private `TestControlOverlay` path exactly once; recreating unconfigured returns to the three-file vector and proves rendered environment/mount absence before start.

Expose only exact service operations:

```python
ALLOWED_KILL_SERVICES = frozenset({
    "agent-worker", "tool-worker", "event-worker", "workflow-worker", "sandbox-runner",
})
ALLOWED_RESTART_DEPENDENCIES = frozenset({"postgres", "nats", "temporal"})
```

`sigkill_worker(service)` executes the Compose argv suffix `["kill", "--signal", "SIGKILL", service]` and verifies the exact project/service labels first. `restart_dependency(service)` performs `stop --timeout 10`, confirms exit, `start`, then bounded readiness/reconnect checks. `recreate_worker_with_legacy_barrier` or `recreate_worker_with_failpoint` creates a fresh mode-0700 host directory, supplies exactly one control family and identity, force-recreates one service, waits for the host-visible fsynced arrival marker, and never releases automatically. `recreate_worker_without_controls` removes all control variables before force-recreate.

`wait_for_worker_loss(service, previous_instance_id, *, timeout_seconds=WORKER_LOSS_TIMEOUT_SECONDS)` accepts only heartbeat-bearing `agent-worker`, `tool-worker`, or `event-worker`; it records the killed boot ID in memory, refuses a timeout other than 50, proves the old row is still fresh at the exact 30-second boundary, and returns only after the row is strictly older than 30 seconds and protected health reports the corresponding missing/stale reason. `wait_for_worker_recovery(..., timeout_seconds=WORKER_RECOVERY_TIMEOUT_SECONDS)` requires a different fresh owner ID, readiness `ok`, and real queue capability within exactly 65 seconds. `wait_for_workflow_capability_recovery(..., timeout_seconds=WORKER_RECOVERY_TIMEOUT_SECONDS)` uses execution of one uniquely identified no-effect workflow task rather than poller timestamp liveness, and `wait_for_sandbox_recovery(..., timeout_seconds=WORKER_RECOVERY_TIMEOUT_SECONDS)` requires both reachability and orphan reap. Each recovery helper rejects a timeout other than 65. None of these helpers treats `retained_pollers` or `recently_accessed_pollers` as a live/dead grant.

The harness validates container image digest and service label before every action. It never accepts arbitrary service, signal, Compose argument, project name, volume, or path.

- [ ] **Step 3: Implement all authority observers and the common assertion**

One observation call concurrently obtains:

- UI rendered state and authenticated API state for the exact workspace/task;
- PostgreSQL task/run/tool/failure/outbox/replay counts and closed statuses;
- Temporal workflow ID count, execution status, history event counts, pending activities/timers, and task-queue pollers;
- NATS stream/consumer `num_pending`, `num_ack_pending`, `num_redelivered`, delivered/ack floor, and lag;
- fake-provider effect count by stable identity;
- required audit action counts and protected-health state;
- fresh heartbeat-owner count for the selected heartbeat-bearing worker (or `None` for workflow-worker), plus retained/recent Temporal poller diagnostic counts that never grant liveness;
- an in-memory scan of new logs/traces/metrics/API/UI/authority payloads since the previous snapshot.

Raw identifiers are used only in memory to query the unique project. `AuthoritySnapshot` and evidence contain counts/status only. `assert_scenario_contract` additionally accepts an exact per-scenario `EffectExpectation(minimum_per_identity, maximum_per_identity, allow_execution_unknown, allow_safe_failed)` and fails on inconsistent UI/API/PG/Temporal truth, duplicate durable work, residual NATS lag, missing audit, unhealthy service, or any canary match. Exactly-once rows use `(1, 1, False, False)`, at-most-once/unknown rows use `(0, 1, True, False)`, and the sandbox row uses `(0, 0, True, True)`.

- [ ] **Step 4: Implement bounded runner and sanitized failure artifacts**

The `run_phase10_chaos.py scenario` subcommand requires `--id`, `--socket-mode`, and `--result`; argparse validates the ID against the checked-in registry, the mode against `rootful|rootless`, and the result as a new file beneath the runner-owned private temporary directory. `scenario`, `full`, and `pr-smoke` expose the runbooks' optional paired `--resolved-images`/`--runtime-image-env` flags and reject a partial pair; release-evidence/full/nightly/final invocations always pass both, while focused RED/GREEN tests may use the harness's private current-tree image IDs and cannot emit release evidence. It invokes exactly one registry `pytest_node` at a time through `run_exact_integration_node`, with the mode/project contract passed through a mode-0600 temporary JSON file and the private safe-counter plugin FD. `pr-smoke` runs exactly the registered agent, tool, and event nodes in three distinct projects while selecting only the requested registered sub-boundary in each contract. `full` iterates all ten rows in registry order, always creating a new project per row. `aggregate` requires exactly one result per `(scenario_id, mode)`, matching commit/image hashes, all assertions true, one collected/passed and zero deselected per node, and no skipped/cancelled/duplicate row.

The runner launches pytest and every diagnostic producer with bounded `stdout=PIPE`/`stderr=PIPE`, no shell, no `tee`/redirection, and no `--junitxml` or raw-output path. Concurrent readers scan each byte chunk plus the scanner overlap before decoding; they retain only closed counters/enums and discard the raw chunk. Compose status, protected health, registered JSON log lines, pytest lifecycle events, scan results, and teardown state are likewise captured through bounded pipes or in-process callbacks. Crossing an 8-MiB per-stream or 32-MiB total diagnostic bound yields only `diagnostic_bound_exceeded` and fails the run. No pre-sanitization byte is ever written to a named, anonymous, temporary, cache, artifact, or result file.

On pytest failure, `phase10_chaos_artifact.py` reduces those already-scanned in-memory observations and emits only:

```json
{
  "schema_version": 1,
  "kind": "phase10_chaos_diagnostics",
  "scenario_id": "03-event-post-handler-pre-ack",
  "socket_mode": "rootless",
  "services": [{"service": "event-worker", "state": "exited", "health": "none"}],
  "events": [{"service": "event-worker", "event": "event.processed", "error_code": "internal_error", "count": 1}],
  "assertions": [{"name": "nats", "safe_code": "condition_not_met"}]
}
```

Enums come from closed registries. `preflight_artifact_publication(private_probe_directory, publication_directory)` runs before any diagnostic producer: it requires distinct owner-only mode-`0700` directories on the same `st_dev`, an empty publication directory, and `/proc/self/fd` as a real usable procfs view. It opens a mode-`0600` Linux `O_TMPFILE` in the private probe directory, writes/fsyncs a fixed closed safe probe, and publishes it there under the reserved `.phase10-artifact-preflight-v1` basename by calling `os.link(f"/proc/self/fd/{fd}", probe_name, dst_dir_fd=private_directory_fd, follow_symlinks=True)`. This selects unprivileged `AT_SYMLINK_FOLLOW` and never uses the capability-gated empty-path form. It fsyncs/unlinks/fsyncs the private probe and fails closed before producer launch on any error; the publication directory stays empty throughout. Tests run this path with no effective capabilities, and outer teardown removes a complete safe private probe if the preflight process itself is killed after linking.

`publish_validated_artifact(directory, name, canonical_bytes)` accepts only bytes already proven canary-free and schema-valid, opens a mode-`0600` `O_TMPFILE` in the preflighted mode-`0700` result directory, writes/rewinds/revalidates/fsyncs it, then uses the same `/proc/self/fd/{fd}` plus `follow_symlinks=True` no-replace link and directory fsync to publish the final name. The destination is one validated basename beneath the held directory FD; `EEXIST` fails with `artifact_exists`, and neither unlink nor replacement is attempted. Unsupported procfs/anonymous publication fails closed; there is no named-temp or privileged-capability fallback. A crash before the link leaves no directory entry, and after a successful link the artifact is complete and validated. Successful runs emit only `ScenarioResult`; the canary manifest remains inside the separately owned harness-private root and outer teardown removes it.

Implement `phase10_chaos_artifact.py package` here, before any matrix evidence is produced. It accepts one allowlisted producer kind plus closed argv constructed by this module, performs publication preflight before launch, owns the bounded producer subprocess, scans both pipe streams in memory, reduces only safe counters/enums, and calls `publish_validated_artifact`; it never accepts shell text, arbitrary executable/argv, a raw input path, or a diagnostic output path. Recording and crash tests cover every producer kind later used by PR, nightly, and release workflows. Tasks 11–13 consume this command unchanged.

- [ ] **Step 5: Run GREEN and generic harness regressions**

```bash
uv run pytest tests/test_phase10_chaos_harness.py tests/test_phase10_chaos_artifact.py -q
uv run python scripts/run_phase10_chaos.py list
test "$(rg -c '^WORKER_LOSS_TIMEOUT_SECONDS = 50$' tests/integration/phase10_chaos_assertions.py)" = "1"
test "$(rg -c '^WORKER_RECOVERY_TIMEOUT_SECONDS = 65$' tests/integration/phase10_chaos_assertions.py)" = "1"
! rg -n 'wait_for_(worker_recovery|workflow_capability_recovery|sandbox_recovery).*timeout_seconds=(50|WORKER_LOSS_TIMEOUT_SECONDS)' tests/integration/phase10_chaos_harness.py tests/integration/test_phase10_chaos_workers.py tests/integration/test_phase10_chaos_events.py tests/integration/test_phase10_chaos_dependencies.py tests/integration/test_phase10_final_exit.py
uv run ruff check tests/integration/phase10_chaos_assertions.py tests/integration/phase10_chaos_harness.py scripts/run_phase10_chaos.py scripts/phase10_chaos_artifact.py tests/test_phase10_chaos_harness.py tests/test_phase10_chaos_artifact.py
uv run mypy tests/integration/phase10_chaos_assertions.py tests/integration/phase10_chaos_harness.py scripts/run_phase10_chaos.py scripts/phase10_chaos_artifact.py
```

Expected: PASS; `list` prints only the ten registered IDs, never node paths or runtime identifiers.

- [ ] **Step 6: Guard, stage exactly Task 6, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add tests/integration/phase10_chaos_assertions.py tests/integration/phase10_chaos_harness.py scripts/run_phase10_chaos.py scripts/phase10_chaos_artifact.py tests/test_phase10_chaos_harness.py tests/test_phase10_chaos_artifact.py
git diff --cached --check
git commit -m "test: add bounded Phase 10 chaos harness"
```

Expected: commit 7 of 15 provides only reusable orchestration/assertion/diagnostic infrastructure.

### Task 7: Prove Agent and Tool Recovery at Every Effect Boundary (Scenarios 01–02)

**Files:**
- Modify: `tests/integration/phase10_chaos_harness.py`
- Modify: `tests/integration/phase10_chaos_assertions.py`
- Create: `tests/integration/test_phase10_chaos_workers.py`

**Interfaces:**
- Consumes: existing `AGENT_BEFORE_BIND`, `PHASE9_AFTER_MANIFEST`, `TOOL_BEFORE_CLAIM`, `TOOL_AFTER_CLAIM`, `TOOL_AFTER_EFFECT`; dedicated tool queue; stable invocation IDs; fake model/effect ledgers; and common authority snapshots.
- Produces: scenario 01 with two agent SIGKILL sub-runs and scenario 02 with three tool SIGKILL sub-runs, each using real workers/Temporal activities and exact post-recovery effect semantics.

- [ ] **Step 1: Write RED scenario tests against not-yet-implemented drivers**

```python
import pytest


@pytest.mark.integration
async def test_agent_manifest_bind_recovery(chaos_harness) -> None:
    result = await chaos_harness.run_agent_manifest_bind_recovery()
    assert result.scenario_id == "01-agent-manifest-bind"
    assert result.outcome == "exactly_once"
    chaos_harness.assert_complete(result)


@pytest.mark.integration
async def test_tool_effect_boundary_recovery(chaos_harness) -> None:
    result = await chaos_harness.run_tool_effect_boundary_recovery()
    assert result.scenario_id == "02-tool-effect-boundaries"
    assert result.outcome in {"at_most_once", "execution_unknown"}
    chaos_harness.assert_complete(result)
```

Run:

```bash
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_workers_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_workers_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_workers_rootful_gid" -gt 0
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_workers_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_workers.py::test_agent_manifest_bind_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_workers_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_workers.py::test_tool_effect_boundary_recovery -q
```

Expected: FAIL because the two scenario drivers do not exist; no timing sleep or simulated worker is acceptable as GREEN.

- [ ] **Step 2: Implement the two agent manifest-bind SIGKILL sub-runs**

For each boundary, use a fresh task/run and recreate only agent-worker with the exact legacy barrier and run UUID.

At `AGENT_BEFORE_BIND`, wait for arrival, then assert UI/API `running`, zero manifest/reasoning event, Temporal running with one activity attempt, NATS/fake effect zero, protected health initially ok. SIGKILL; prove the killed owner's heartbeat remains fresh through the exact 30-second boundary, then require stale/missing protected health strictly after 30 seconds and no later than the 50-second monotonic loss deadline. Retained/recent Temporal pollers remain diagnostic and cannot mask the loss. Recreate unconfigured, require a different fresh agent-worker owner plus real agent-queue capability and health `ok` within the separate 65-second recovery deadline, wait for terminal, and assert exactly two model requests, one bound manifest, one reasoning event, one run, and exactly one final external effect under its stable invocation identity.

At `PHASE9_AFTER_MANIFEST`, assert the manifest commit and one manifest event already exist, reasoning/terminal/effect do not. SIGKILL, apply the same exact-boundary loss and fresh-owner/capability recovery helpers, and recreate; final assertions are one model request, one immutable bound manifest, one reasoning event, one run/outcome, and one effect. In both cases UI must never show false terminal state while Temporal is running/retrying; agent health returns `ok`; NATS drains; audit and canary scans pass.

- [ ] **Step 3: Implement all three tool boundary SIGKILL sub-runs**

Use one fresh bound invocation per boundary and prove the tool queue has a poller before fault and after recovery.

| Barrier | State at arrival | Recovery result | Fake effect count |
| --- | --- | --- | --- |
| `TOOL_BEFORE_CLAIM` | no `ToolCall` | one `completed` invocation | exactly 1 |
| `TOOL_AFTER_CLAIM` | durable `executing` claim | nonretryable `execution_unknown` | exactly 0 |
| `TOOL_AFTER_EFFECT` | durable `executing` claim, effect returned | nonretryable `execution_unknown` | exactly 1 |

At each arrival assert UI/API remains running/retrying, one Temporal workflow and correct activity attempt, NATS state, current fake count, audit prefix, and zero leaks. SIGKILL only tool-worker; prove heartbeat freshness at exactly 30 seconds, then stale/missing health strictly after 30 and by the 50-second loss deadline even if a retained/recent tool-queue poller remains. Recreate unconfigured and require a different fresh tool-worker owner, actual tool-queue capability, and health `ok` by the separate 65-second recovery deadline. There is exactly one terminal invocation row and no automatic executor re-entry after a durable claim. `execution_unknown` has no retry control and safe UI copy.

- [ ] **Step 4: Run GREEN in both modes and predecessor regressions**

```bash
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_workers_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_workers_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_workers_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_workers_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_workers.py::test_agent_manifest_bind_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_workers_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_workers.py::test_tool_effect_boundary_recovery -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_workers.py::test_agent_manifest_bind_recovery -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_workers.py::test_tool_effect_boundary_recovery -q
uv run pytest packages/tools/tests/test_crash_barriers.py services/agent_worker/tests/test_phase9_invocation_activity.py services/tool_worker/tests/test_bound_tool_execution.py -q
uv run ruff check tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_chaos_workers.py
uv run mypy tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py
```

Expected: each exact integration invocation reports one collected/passed node and zero deselected nodes. Both agent and all three tool hard-kill sub-boundaries pass in both modes while preserving predecessor counts and semantics.

- [ ] **Step 5: Guard, stage exactly Task 7, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_chaos_workers.py
git diff --cached --check
git commit -m "test: prove agent and tool crash recovery"
```

Expected: commit 8 of 15 covers scenarios 01–02 with five deterministic hard kills.

### Task 8: Prove Event Redelivery, Quarantine Commit Recovery, and One Replay (Scenarios 03–04)

**Files:**
- Modify: `tests/integration/phase10_chaos_harness.py`
- Modify: `tests/integration/phase10_chaos_assertions.py`
- Create: `tests/integration/test_phase10_chaos_events.py`

**Interfaces:**
- Consumes: event post-handler/pre-ack and completed/pre-term wait failpoints, quarantine `raise_once`, five-attempt durable processing state, atomic failure/outbox/audit/completed commit, independent DLQ publication, NATS dedupe/term, replay API idempotency, and trigger workflow identity.
- Produces: scenario 03 delivery-before-ack recovery and scenario 04 quarantine-commit rollback, delivery-before-term recovery, and exactly one replay request/outcome.

- [ ] **Step 1: Write RED scenario tests**

```python
import pytest


@pytest.mark.integration
async def test_event_post_handler_pre_ack_recovery(chaos_harness) -> None:
    result = await chaos_harness.run_event_post_handler_pre_ack_recovery()
    assert result.scenario_id == "03-event-post-handler-pre-ack"
    chaos_harness.assert_complete(result)


@pytest.mark.integration
async def test_quarantine_commit_failure_and_one_replay(chaos_harness) -> None:
    result = await chaos_harness.run_quarantine_commit_replay_recovery()
    assert result.scenario_id == "04-quarantine-commit-replay"
    chaos_harness.assert_complete(result)
```

Run:

```bash
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_events_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_events_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_events_rootful_gid" -gt 0
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_events_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_events.py::test_event_post_handler_pre_ack_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_events_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_events.py::test_quarantine_commit_failure_and_one_replay -q
```

Expected: FAIL because both event scenario drivers are absent.

- [ ] **Step 2: Implement canonical event work committed before NATS ack**

Publish one valid canonical event with unique event/correlation IDs and stable message ID. Configure `EVENT_AFTER_HANDLER_BEFORE_ACK` for that event. At arrival assert handler work and processing completion are durable, exactly one Task and one reject-duplicate Temporal workflow exist, UI/API are running/queued consistently, NATS has one ack-pending delivery, fake effects/audit match, and canaries are absent.

SIGKILL event-worker, prove its killed-owner heartbeat is fresh at exactly 30 seconds, then require stale/missing protected health strictly after 30 and by the 50-second loss deadline; retained Temporal metadata cannot satisfy this check. Recreate unconfigured, require a different fresh event-worker owner and real NATS consumer capability by the separate 65-second recovery deadline, and require redelivery. Completed processing state bypasses business handling; database/Temporal idempotency also independently protects the identity. Final state has one Task, one workflow, one processing row, no duplicate audit/effect, `num_pending = num_ack_pending = 0`, redelivery observed, and health `ok`.

- [ ] **Step 3: Implement fifth-failure quarantine rollback and recovery**

Publish one canonical event whose deterministic test handler raises the same classified retryable-safe failure. Count business calls directly in the fake handler ledger.

1. Let attempts 1–4 fail and redeliver.
2. On attempt 5, persist `quarantine_only` and configure `EVENT_BEFORE_QUARANTINE_COMMIT` with `raise_once`. Assert the transaction rolls back: attempt count remains 5, mode is `quarantine_only`, and failure/outbox/audit/completed counts are zero. NATS remains pending; handler count is exactly 5.
3. Let the next delivery enter quarantine without invoking business handling. Assert one transaction commits exactly one failure, one outbox intent, one audit, and `completed`; handler count stays 5.
4. Pause at `EVENT_COMPLETED_BEFORE_TERM`, wait boundedly for the independent outbox reconciler to produce one sanitized DLQ notification, assert the source delivery is still pending, SIGKILL event-worker, apply the strict 30-second boundary/50-second loss helper and the separate 65-second different-fresh-owner/consumer-capability recovery helper, recreate unconfigured, and prove redelivery observes completed quarantine, never calls handler, and terminates. DLQ/outbox dedupe keeps one notification.
5. Remediate the fixture. POST replay twice with the same CSRF/admin context and identical idempotency key. Both HTTP responses bind the same replay request; exactly one replay envelope, one durable triggered outcome/workflow, one replay audit, and one final resolved/history shape exist.

At every numbered boundary capture all eight common assertion groups and scan canaries. No sixth business attempt is permitted even while quarantine persistence is unavailable.

- [ ] **Step 4: Run GREEN in both modes and DLQ/replay regressions**

```bash
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_events_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_events_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_events_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_events_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_events.py::test_event_post_handler_pre_ack_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_events_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_events.py::test_quarantine_commit_failure_and_one_replay -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_events.py::test_event_post_handler_pre_ack_recovery -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_events.py::test_quarantine_commit_failure_and_one_replay -q
uv run pytest services/event_worker/tests/test_delivery.py services/event_worker/tests/test_quarantine.py services/event_worker/tests/test_commands.py apps/api/tests/test_event_failures.py -q
uv run ruff check tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_chaos_events.py
uv run mypy tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py
```

Expected: each exact integration invocation reports one collected/passed node and zero deselected nodes. Scenario 03 has one handler bundle/workflow; scenario 04 has exactly five handler calls, one atomic quarantine bundle, one notification, and one replay request/outcome, while the predecessor unit regressions stay green.

- [ ] **Step 5: Guard, stage exactly Task 8, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_chaos_events.py
git diff --cached --check
git commit -m "test: prove event quarantine and replay recovery"
```

Expected: commit 9 of 15 covers scenarios 03–04 and the full event failure lifecycle.

### Task 9: Prove Workflow and Infrastructure Recovery (Scenarios 05–08)

**Files:**
- Modify: `tests/integration/phase10_chaos_harness.py`
- Modify: `tests/integration/phase10_chaos_assertions.py`
- Create: `tests/integration/test_phase10_chaos_dependencies.py`

**Interfaces:**
- Consumes: Temporal histories/timers/approval signals, durable command reconciliation, NATS reconnect/backoff, activity-commit failpoint, sandbox post-start failpoint and reaper, both socket modes, and common authority snapshots.
- Produces: workflow-worker timer/approval restart, NATS+Temporal dispatch restart, PostgreSQL activity-commit restart, and sandbox-runner orphan/socket recovery scenarios.

- [ ] **Step 1: Write RED tests for all four drivers**

```python
import pytest


@pytest.mark.integration
async def test_workflow_timer_and_approval_recovery(chaos_harness) -> None:
    chaos_harness.assert_complete(await chaos_harness.run_workflow_timer_approval_recovery())


@pytest.mark.integration
async def test_nats_and_temporal_dispatch_recovery(chaos_harness) -> None:
    chaos_harness.assert_complete(await chaos_harness.run_nats_temporal_dispatch_recovery())


@pytest.mark.integration
async def test_postgres_activity_commit_recovery(chaos_harness) -> None:
    chaos_harness.assert_complete(await chaos_harness.run_postgres_activity_commit_recovery())


@pytest.mark.integration
async def test_sandbox_orphan_and_socket_recovery(chaos_harness) -> None:
    chaos_harness.assert_complete(await chaos_harness.run_sandbox_orphan_socket_recovery())
```

Run:

```bash
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_dependencies_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_dependencies_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_dependencies_rootful_gid" -gt 0
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_workflow_timer_and_approval_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_nats_and_temporal_dispatch_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_postgres_activity_commit_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_sandbox_orphan_and_socket_recovery -q
```

Expected: FAIL because the four concrete recovery drivers are absent.

- [ ] **Step 2: Implement workflow timer and approval history recovery**

Start one real general workflow at a durable timer and one real agent/tool workflow at an open approval. Record history counts and UI/API state. SIGKILL workflow-worker while both are waiting; agent/tool workers remain live. Assert Temporal retains one timer and one approval wait, PostgreSQL approval is pending, no effect/audit decision exists, NATS is stable, and UI remains waiting.

While workflow-worker is absent, assert `fresh_owner_instances is None`; retained/recent poller records may remain and are capability diagnostics only, never liveness. Recreate workflow-worker unconfigured and require an actual newly dispatched no-effect workflow task on `jhin-workflow-queue` within the 65-second recovery deadline before resuming the scenario. Advance the test timer using Temporal time-skipping only in the unit companion; the live test waits for its short fixed timer. Approve once through the real protected API. Both histories resume from their existing workflow IDs; one timer outcome, one approval decision, one tool invocation/effect, one terminal run, correct audits, zero lag, and no leak result.

- [ ] **Step 3: Implement NATS and Temporal restart during dispatch**

Create two durable commands with separate stable identities.

For NATS, stop NATS after the outbox external-call authorization commit and before publish. Assert command remains authorized/due, no publish/effect occurs, UI/API truth is retrying, and safe reconnect telemetry has no URL/error text. Restart NATS; event-worker reconnects with bounded backoff, publishes once under the stable message ID, reconciles terminal state, and drains the consumer.

For Temporal, stop Temporal after ordinary/trigger workflow-start authorization and before dispatch acceptance. Assert immutable start input/identity remains persisted, no workflow/run/effect exists, and the command is retryable without attempt 21. Restart Temporal; all clients reconnect, dispatcher starts/describes the exact workflow once under reject-duplicate, closes proof, clears input only after proof, and reaches one outcome. Capture all common assertions before outage, during outage, and after recovery.

- [ ] **Step 4: Implement PostgreSQL restart during activity commit**

Run an activity that stages one complete product bundle and reaches `AGENT_BEFORE_ACTIVITY_COMMIT` with its run UUID. Assert no observer can see the uncommitted rows. Stop PostgreSQL, release the barrier, and require the activity transaction to fail/retry with a safe database-unavailable category. Restart PostgreSQL and wait for clients/health.

The final state must be exactly one committed bundle/run/tool identity. If the external effect occurred before the interrupted commit, stable tool invocation recovery permits at most one effect and either proves it or records nonretryable `execution_unknown`; it never automatically repeats. If no effect occurred, the activity completes once. PostgreSQL has no idle-in-transaction session, Temporal has one workflow, NATS drains, and UI/API matches the selected closed outcome.

- [ ] **Step 5: Implement sandbox hard kill, orphan reap, and socket isolation**

Start a real tool-worker CLI job that consumes `sandbox_secret_env`, blocks in the job command, and reaches `SANDBOX_AFTER_CONTAINER_START` only after wrapper acknowledgment. Inspect the exact labelled job container in memory and assert:

- no canary/secret variable in Config/HostConfig/labels/command;
- `User = 1000:1000`, `Privileged = false`, `ReadonlyRootfs = true`, `CapDrop = [ALL]`, no-new-privileges;
- no Docker/rootless socket, host-root, control/data network, host PID/IPC, device, privileged mount, or runner supplemental group;
- only the workspace bind/tmpfs and dedicated `none`/sandbox network are present.

SIGKILL sandbox-runner, not the job. Assert exactly one orphan with the exact project/job label and no unrelated container. Restart sandbox-runner unconfigured; require a successful bounded reachability probe and orphan reap within the 65-second recovery deadline, recording only safe short container metadata. Tool-worker reaches safe `failed` if absence is proven or nonretryable `execution_unknown` if ambiguous, with no automatic job/effect repeat. Run this exact check in both rootful and rootless modes; rootless runner authority never appears in the job.

- [ ] **Step 6: Run GREEN in both modes and durability/security regressions**

```bash
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_dependencies_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_dependencies_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_dependencies_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_workflow_timer_and_approval_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_nats_and_temporal_dispatch_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_postgres_activity_commit_recovery -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_sandbox_orphan_and_socket_recovery -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_workflow_timer_and_approval_recovery -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_nats_and_temporal_dispatch_recovery -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_postgres_activity_commit_recovery -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_dependencies.py::test_sandbox_orphan_and_socket_recovery -q
uv run pytest services/sandbox_runner/tests services/agent_worker/tests services/event_worker/tests/test_commands.py -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_temporal_durability.py::test_workflow_completes_across_worker_restart -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_dependencies_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_nats_durability.py::test_event_survives_consumer_restart_and_dedupes -q
uv run ruff check tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_chaos_dependencies.py
uv run mypy tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py
```

Expected: every exact integration invocation reports one collected/passed node and zero deselected nodes. Scenarios 05–08 preserve durable histories/commands, bounded reconnect, conservative commit ambiguity, orphan removal, and socket isolation; the focused existing durability nodes and unit regressions remain green.

- [ ] **Step 7: Guard, stage exactly Task 9, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_chaos_dependencies.py
git diff --cached --check
git commit -m "test: prove workflow and dependency recovery"
```

Expected: commit 10 of 15 covers scenarios 05–08 without changing production authority.

### Task 10: Prove Active-Read Master-Key Rotation and Three-Service Restart (Scenario 09)

**Files:**
- Modify: `tests/integration/phase10_chaos_harness.py`
- Modify: `tests/integration/phase10_chaos_assertions.py`
- Create: `tests/integration/test_phase10_chaos_key_rotation.py`

**Interfaces:**
- Consumes: `KeyRotationHarness`, strict versioned keyring, fresh instance reporters, credential mutation generation/fence, verified pre/post backups, ordinary API/agent/tool decrypt-use paths, and exact key-bearing services `api`, `agent-worker`, `tool-worker`.
- Produces: scenario 09 with uninterrupted old/new ordinary reads, complete rewrap/retirement gates, exact three-service restart, fresh reporter proof, and zero leaked key/canary material.

- [ ] **Step 1: Write the RED scenario test**

```python
import pytest


@pytest.mark.integration
async def test_master_key_active_read_recovery(chaos_harness) -> None:
    result = await chaos_harness.run_master_key_active_read_recovery()
    assert result.scenario_id == "09-master-key-active-read"
    assert result.outcome == "exactly_once"
    chaos_harness.assert_complete(result)
```

Run:

```bash
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_rotation_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_rotation_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_rotation_rootful_gid" -gt 0
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rotation_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_key_rotation.py::test_master_key_active_read_recovery -q
```

Expected: FAIL because the integrated active-read rotation driver is absent.

- [ ] **Step 2: Seed old/new rows and start independent ordinary read loops**

Create encrypted model, connector, webhook, and DSN credentials under v1. Start three bounded loops through normal paths: API verify/read, agent model/connector use, and tool-worker connector/sandbox use. Each iteration uses a new business idempotency key where an effect is expected and records only success/key-version/count; provider fakes dedupe by stable effect identity. Park one approval to prove long-lived workflow state survives the key stages.

Require at least five successful iterations per path before rotation. Capture full common authority snapshot plus key distribution `{1: seeded_count}`, active/supported `(1,(1,))`, exact current reporter set, zero errors, and zero canary/key matches.

- [ ] **Step 3: Execute every staged protocol gate while reads continue**

Use the predecessor CLI/harness rather than direct database updates:

1. Verify separate pre-rotation database and old-keyring backups.
2. Distribute dual ring `(1,(1,2))`; wait for fresh exact API/agent/tool reporters and continued reads of v1.
3. Activate `(2,(1,2))`; create new credentials under v2 while old v1 rows remain readable.
4. Run resumable rewrap to zero v1/unexpected rows, validating generation/fence/last-secret progress and continued reads after each bounded batch.
5. Reach retirement-ready only after the exact completed attempt, fresh reporters, zero v1 rows, and verified post-rotation database/keyring backups.
6. Arm retirement, install `(2,(2))`, and restart exactly API, agent-worker, then tool-worker—one at a time—while the other read paths continue. After each restart require a new instance ID/report, correct key tuple, protected health, and successful ordinary use.
7. Commit retirement only after all three fresh reporters match and the fence/generation are unchanged. Resolve the parked approval and complete one effect.

At every step assert UI/API, PostgreSQL row versions/counts, Temporal histories/status, NATS state, fake effects, audits, health, and canary absence. A failed install/restart/reporter check leaves the durable fence armed and uses the documented dual-ring cancel path; the test never guesses or regenerates a missing key.

- [ ] **Step 4: Assert final recovery and no stale readers**

Stop read loops only after at least five post-retirement successes per path. Require active/supported `(2,(2))`, zero v1/unexpected rows, exact fresh reporter set for the three services, no stale live reporter, verified post backup, one approval effect, no duplicated business effect, no failed decrypt, all service health `ok`, NATS lag zero, and no raw/encoded v1/v2 key/canary in any audited sink.

- [ ] **Step 5: Run GREEN in both modes and predecessor rotation regressions**

```bash
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_rotation_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_rotation_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_rotation_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rotation_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_key_rotation.py::test_master_key_active_read_recovery -q
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run pytest -o addopts='' -m integration tests/integration/test_phase10_chaos_key_rotation.py::test_master_key_active_read_recovery -q
uv run pytest tests/test_phase10_master_key_rotation_harness.py packages/secrets/tests/test_rotation.py packages/secrets/tests/test_rotation_cli.py -q
JHIN_RUN_MASTER_KEY_LIVE=1 PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_rotation_rootful_gid" uv run pytest -o addopts='' -m integration tests/integration/test_phase10_master_key_rotation.py::test_staged_rotation_survives_restarts_and_retires_old_key -q
uv run ruff check tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_chaos_key_rotation.py
uv run mypy tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py
```

Expected: each exact integration invocation reports one collected/passed node and zero deselected nodes, with continuous old/new reads, zero stale rows/readers, exact API/agent/tool restarts, and safe retirement.

- [ ] **Step 6: Guard, stage exactly Task 10, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_chaos_key_rotation.py
git diff --cached --check
git commit -m "test: prove active-read key rotation recovery"
```

Expected: commit 11 of 15 covers scenario 09 and preserves the complete staged key protocol.

### Task 11: Restore a Migrated Previous State and Run the Final Worker-Restart Exit (Scenario 10)

**Files:**
- Create: `compose.phase10-final-exit.yaml`
- Modify: `scripts/phase10_backup.py`
- Modify: `scripts/phase10_restore.py`
- Modify: `scripts/phase10_upgrade.py`
- Create: `scripts/run_phase10_final_exit.py`
- Create: `tests/test_phase10_final_exit_harness.py`
- Modify: `tests/integration/phase10_chaos_harness.py`
- Modify: `tests/integration/phase10_chaos_assertions.py`
- Modify: `scripts/run_phase10_chaos.py`
- Create: `tests/integration/test_phase10_final_exit.py`
- Modify: `ops/images/resolved-images.json`
- Modify: `docs/evidence/phase10-image-security.json`
- Modify: `docs/evidence/phase10-sizing.json`
- Modify: `docs/evidence/phase10-rate-limits.json`
- Modify: `docs/evidence/phase10-backup.json`
- Modify: `docs/evidence/phase10-restore.json`
- Modify: `docs/evidence/phase10-upgrades.json`
- Modify: `docs/evidence/phase10-hardening.md`
- Modify: `docs/evidence/phase10-secret-audit.json`
- Create: `docs/evidence/phase10-chaos.json`
- Create: `docs/evidence/phase10-final-exit.json`

**Interfaces:**
- Consumes: Phase 9 previous-state fixture, current `0018` upgrade rehearsal, verified encrypted backup, fresh restore, current image digests, both test-control families, protected health, and scenarios 01–09.
- Produces: `upgrade_retained_project(project, *, previous_state, resolved_images, runtime_image_env, scratch_root) -> UpgradeResult`, `backup_retained_project(project, *, recipients, scratch_root) -> VerifiedBackup`, and `restore_retained_project(project, *, backup, identity, scratch_root) -> RestoreResult`; a rebuilt/rescanned operations image and fully refreshed digest-bound runbooks evidence; scenario 10 in a fresh restored project; a rootful/rootless full ten-scenario aggregate; dedicated final-exit evidence; and the exact Phase 10 worker-restart acceptance outcome.

- [ ] **Step 1: Write RED source/target isolation, ordering, evidence, and final scenario tests**

Pin the orchestration order in `tests/test_phase10_final_exit_harness.py` with a recording runner:

```python
def test_final_exit_uses_distinct_source_and_restored_projects(recording_exit_runner) -> None:
    result = recording_exit_runner.run(socket_mode="rootless")
    assert result.calls == [
        "previous_state.seed", "upgrade.all", "backup.create", "backup.verify",
        "restore.preflight", "restore.fresh", "restore.validate",
        "exit.agent_kill", "exit.tool_kill", "exit.event_kill", "exit.validate",
        "target.teardown", "source.teardown",
    ]
    assert result.source_project != result.target_project
    assert result.source_volume_ids.isdisjoint(result.target_volume_ids)
    assert result.target_was_fresh is True
    assert result.retained_adapter_calls == [
        "upgrade_retained_project", "backup_retained_project",
        "restore_retained_project",
    ]
    assert result.adapter_lifecycle_calls == []
    assert result.source_live_during_restore is True
    assert result.target_live_during_worker_exit is True
```

Add direct RED unit tests importing all three retained adapter names. Pass recording `IsolatedComposeProject` instances whose `__enter__`, `__exit__`, and `teardown` methods raise if called; require each adapter to use only the supplied project's validated `run`/label/authority methods, leave it live, and return its closed result. Preserve the predecessor owning CLI tests that require `all` to create and always tear down its own project. Add a current-tree fingerprint regression proving that changing any of the three operations-image-copied scripts makes the old resolved operations child and paired runtime env invalid; Task 11 cannot reach `prepare-runtime` or final-exit GREEN until the image is rebuilt and every runtime-set-bound evidence file is regenerated. Pin public evidence: exactly two modes, `source = "phase9_previous_state"`, migration `0014 -> 0018`, upgrade/backup/restore/current-images booleans, fresh target, worker kills `[agent-worker, tool-worker, event-worker]`, one task/run/workflow, effect outcome, zero lag, health ok, zero violations, bounded teardown, and pass. Reject names/IDs/paths/DSNs/logs/histories/keys.

Run:

```bash
uv run pytest tests/test_phase10_final_exit_harness.py --collect-only -q
uv run pytest -o addopts='' -m integration tests/integration/test_phase10_final_exit.py::test_restored_worker_restart_exit --collect-only -q
```

Expected: FAIL on the missing retained adapter imports and then the absent final-exit runner/scenario; an owning predecessor CLI that tears down is not an acceptable GREEN.

- [ ] **Step 2: Build a supported migrated source and verified release backup**

Refactor only the already-tested inner operations of `phase10_upgrade.py`, `phase10_backup.py`, and `phase10_restore.py` into the three exact retained adapters above. An adapter accepts one already-entered `IsolatedComposeProject`, revalidates its generated name, exact source/target Compose vector, socket authority, labels, paired image inputs, and private scratch root, and never creates, enters, exits, tears down, installs a signal handler for, or assumes ownership of that project. The existing `all`/drill CLIs remain owning wrappers: they create their isolated project, call the same adapter, and always execute their settled teardown/cleanup contract. Adapter results contain only closed booleans/counts/status; encrypted artifact handles are opaque caller-owned values rooted under `scratch_root`. These three scripts are reviewed inputs copied into the settled operations image, so their first modification invalidates the old local-content fingerprint by design; do not run a supposedly current paired `prepare-runtime` until Step 6 records the rebuilt child.

`run_phase10_final_exit.py` is the sole lifecycle owner. It enters a unique source `IsolatedComposeProject` using the Phase 9 fixture and previous compatible images. Seed a signed webhook, waiting workflow history, NATS retained/pending event, encrypted credentials, approval, audit, task, and canary corpus. Call `upgrade_retained_project` one component at a time to current digest-pinned images and head `0018`; prove previous-image rollback rehearsal and current smoke while the same source project remains live. No production project/volume is accepted.

With the current source healthy, call `backup_retained_project` and verify all PostgreSQL databases/Temporal history, NATS state, separately encrypted config, and separately encrypted keyring. Recipient identities, runtime key, backup directory, ports, volumes, namespace, and Docker config are unique/private/disposable. The source remains live and isolated while restore preflight runs; the final-exit owner retains the opaque `VerifiedBackup` until target teardown and then deletes it.

- [ ] **Step 3: Restore into a second fresh current-image project**

Enter a distinct target `IsolatedComposeProject` directly, require all target project-labelled volumes newly created, empty, and unmounted, then call `restore_retained_project` on that already-entered target. Decrypt keyring to the owner-safe runtime mount, restore PostgreSQL/NATS/Temporal in binding order, assert exactly head `0018` without another migration, then start current API/workflow/event/tool/agent/sandbox/web/observability services. The adapter returns without teardown; only `run_phase10_final_exit.py` owns target cleanup.

Before faulting, prove protected health `ok`, exact pollers/consumers, restored waiting history/timer/pending message, normal credential decrypt/use, one synthetic durable smoke, and a complete zero-leak authority scan. Source and target resource sets must be disjoint; neither project uses fixed ports or a default/private key from another run.

- [ ] **Step 4: Run the exact restored worker-restart chain**

On the target only:

1. Start one real live task/run. Recreate agent-worker at `AGENT_BEFORE_ACTIVITY_COMMIT`, wait for arrival, assert UI/API running/retrying and uncommitted projection absent, SIGKILL, then recreate unconfigured and resume.
2. On the same workflow's bound tool invocation, recreate tool-worker at `TOOL_AFTER_EFFECT`, wait for one fake effect, SIGKILL, then recreate unconfigured. Require one terminal `execution_unknown`, no automatic repetition, and UI safe copy consistent with PostgreSQL/Temporal.
3. Publish a canonical no-trigger event whose durable processing state completes without creating another task/workflow. Pause event-worker at `EVENT_AFTER_HANDLER_BEFORE_ACK`, assert one ack-pending delivery, SIGKILL, recreate unconfigured, and prove redelivery/ack without duplicate work.

After each agent/tool/event kill, prove the killed heartbeat is still fresh at exactly 30 seconds, becomes stale only strictly after 30, and is reported missing/stale by the 50-second monotonic loss deadline even if retained poller diagnostics remain. Recovery requires a different fresh boot owner plus the worker's real queue/consumer capability and health `ok` within the separate 65-second recovery deadline. Every boundary asserts UI/API, PostgreSQL, Temporal counts/status, NATS state, fake effect count, audit, and no canary. Final truth is one durable task, one run, one workflow, one externally visible tool effect durably marked `execution_unknown`, one completed event-processing identity, NATS pending/ack-pending/lag zero, all required health `ok`, and no duplicate task/workflow/effect.

- [ ] **Step 5: Bound teardown and record only validated evidence**

Whether upgrade, backup, restore, worker recovery, scan, or assertion fails, target teardown runs first and source teardown second, each under the settled 60-second outer bound plus one label-scoped retry. Remove private recipient identities, backup ciphertext, canary/key manifests, Docker config, and result scratch only after projects are down. A teardown failure makes the scenario fail and emits only sanitized diagnostics.

`run_phase10_final_exit.py validate-evidence` enforces the dedicated schema. `run_phase10_chaos.py full` now requires all ten registered nodes and writes one safe mode result; `aggregate` requires exactly twenty rows (ten x two modes), no skip/duplicate/cancel, and extracts the two scenario-10 rows into final-exit evidence.

- [ ] **Step 6: Rebuild/rescan the operations image and refresh every invalidated predecessor result**

Consume the settled runbooks interfaces without editing them: `docker/operations.Dockerfile`, `ops/images/release-images.json`, and `scripts/record_phase10_hardening_evidence.py`. The rebuilt result updates `ops/images/resolved-images.json`, then refreshes the exact runbooks-bound evidence set `phase10-rate-limits.json`, `phase10-backup.json`, `phase10-restore.json`, `phase10-upgrades.json`, `phase10-sizing.json`, `phase10-image-security.json`, and `phase10-hardening.md`; the changed image-set hash also forces this plan's `phase10-secret-audit.json` refresh. Do not stage the unchanged Dockerfile, inventory, or recorder.

```bash
set -euo pipefail
phase10_refresh_dir="$(mktemp -d)"
phase10_refresh_sizing_dir="$phase10_refresh_dir/sizing"
phase10_refresh_audit_dir="$phase10_refresh_dir/secret-audit"
mkdir -m 700 "$phase10_refresh_sizing_dir" "$phase10_refresh_audit_dir"
trap 'find "$phase10_refresh_dir" -depth -mindepth 1 -delete; rmdir "$phase10_refresh_dir"' EXIT
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_refresh_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_refresh_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_refresh_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
uv run pytest tests/test_phase10_upgrade.py tests/test_phase10_backup.py tests/test_phase10_restore.py tests/test_phase10_final_exit_harness.py -q
uv run python scripts/build_phase10_images.py resolve --inventory ops/images/release-images.json --output ops/images/resolved-images.json
uv run python scripts/build_phase10_images.py build --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --output-dir "$phase10_refresh_dir/oci"
uv run python scripts/evaluate_phase10_vulnerabilities.py scan --oci-dir "$phase10_refresh_dir/oci" --allowlist ops/security/vulnerability-allowlist.json --evidence docs/evidence/phase10-image-security.json
uv run python scripts/build_phase10_images.py validate-evidence docs/evidence/phase10-image-security.json
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_refresh_rootful_gid" uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --oci-dir "$phase10_refresh_dir/oci" --socket-mode rootful --output "$phase10_refresh_dir/rootful-runtime.env"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --oci-dir "$phase10_refresh_dir/oci" --socket-mode rootless --output "$phase10_refresh_dir/rootless-runtime.env"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_refresh_rootful_gid" PHASE10_RESOLVED_IMAGES=ops/images/resolved-images.json PHASE10_RUNTIME_IMAGE_ENV="$phase10_refresh_dir/rootful-runtime.env" PHASE10_EVIDENCE_PREFIX="$phase10_refresh_dir/rootful" make phase10-proxy-drill phase10-rate-limit-drill phase10-backup-restore-drill phase10-upgrade-drill
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock PHASE10_RESOLVED_IMAGES=ops/images/resolved-images.json PHASE10_RUNTIME_IMAGE_ENV="$phase10_refresh_dir/rootless-runtime.env" PHASE10_EVIDENCE_PREFIX="$phase10_refresh_dir/rootless" make phase10-proxy-drill phase10-rate-limit-drill phase10-backup-restore-drill phase10-upgrade-drill
uv run python scripts/record_phase10_rate_limit_evidence.py aggregate --rootful "$phase10_refresh_dir/rootful-rate-limit.json" --rootless "$phase10_refresh_dir/rootless-rate-limit.json" --output docs/evidence/phase10-rate-limits.json
uv run python scripts/phase10_backup.py aggregate-evidence --rootful "$phase10_refresh_dir/rootful-backup.json" --rootless "$phase10_refresh_dir/rootless-backup.json" --output docs/evidence/phase10-backup.json
uv run python scripts/phase10_restore.py aggregate-evidence --rootful "$phase10_refresh_dir/rootful-restore.json" --rootless "$phase10_refresh_dir/rootless-restore.json" --output docs/evidence/phase10-restore.json
uv run python scripts/phase10_upgrade.py aggregate-evidence --rootful "$phase10_refresh_dir/rootful-upgrades.json" --rootless "$phase10_refresh_dir/rootless-upgrades.json" --output docs/evidence/phase10-upgrades.json
for profile in development small small-monitored medium medium-monitored; do
  PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_refresh_rootful_gid" uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootful --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_refresh_dir/rootful-runtime.env" --evidence "$phase10_refresh_sizing_dir/${profile}-rootful.json"
  PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_sizing.py measure --profile "$profile" --socket-mode rootless --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_refresh_dir/rootless-runtime.env" --evidence "$phase10_refresh_sizing_dir/${profile}-rootless.json"
done
uv run python scripts/run_phase10_sizing.py aggregate --input-dir "$phase10_refresh_sizing_dir" --output docs/evidence/phase10-sizing.json
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_refresh_rootful_gid" uv run python scripts/run_phase10_secret_audit.py run --socket-mode rootful --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_refresh_dir/rootful-runtime.env" --result "$phase10_refresh_audit_dir/rootful.json"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_secret_audit.py run --socket-mode rootless --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_refresh_dir/rootless-runtime.env" --result "$phase10_refresh_audit_dir/rootless.json"
uv run python scripts/run_phase10_secret_audit.py aggregate --input "$phase10_refresh_audit_dir/rootful.json" --input "$phase10_refresh_audit_dir/rootless.json" --output docs/evidence/phase10-secret-audit.json
uv run python scripts/record_phase10_rate_limit_evidence.py --check docs/evidence/phase10-rate-limits.json
uv run python scripts/phase10_backup.py validate-evidence docs/evidence/phase10-backup.json
uv run python scripts/phase10_restore.py validate-evidence docs/evidence/phase10-restore.json
uv run python scripts/phase10_upgrade.py validate-evidence docs/evidence/phase10-upgrades.json
uv run python scripts/run_phase10_sizing.py validate-evidence docs/evidence/phase10-sizing.json
uv run python scripts/run_phase10_secret_audit.py validate-evidence docs/evidence/phase10-secret-audit.json
uv run python scripts/record_phase10_hardening_evidence.py --output docs/evidence/phase10-hardening.md
uv run python scripts/record_phase10_hardening_evidence.py --check docs/evidence/phase10-hardening.md
```

Expected: the predecessor unit suites remain green, and the rootful/rootless owning proxy/rate-limit/backup/restore/upgrade drill interfaces each create and tear down their own isolated projects. The changed operations-image input is rebuilt for both architectures, all repository/runtime images rescan, and every runtime-set-bound rate-limit/backup/restore/upgrade/sizing/hardening/secret-audit artifact binds the new complete resolved image set. No file-level or mixed-node pytest invocation can bypass the repository's integration exclusion.

- [ ] **Step 7: Prove native fingerprints, final exit, and the full matrix in both modes**

```bash
set -euo pipefail
phase10_result_dir="$(mktemp -d)"
trap 'find "$phase10_result_dir" -depth -mindepth 1 -delete; rmdir "$phase10_result_dir"' EXIT
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_exit_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_exit_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_exit_rootful_gid" -gt 0
test -S /run/user/10001/docker.sock
test ! -L /run/user/10001/docker.sock
test "$(stat -c %u /run/user/10001/docker.sock)" = "10001"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_exit_rootful_gid" uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --socket-mode rootful --output "$phase10_result_dir/rootful-runtime.env"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/build_phase10_images.py prepare-runtime --inventory ops/images/release-images.json --resolved ops/images/resolved-images.json --socket-mode rootless --output "$phase10_result_dir/rootless-runtime.env"
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_exit_rootful_gid" uv run python scripts/run_phase10_chaos.py full --socket-mode rootful --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_result_dir/rootful-runtime.env" --result "$phase10_result_dir/phase10-chaos-rootful.json"
PHASE10_SOCKET_MODE=rootless PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock uv run python scripts/run_phase10_chaos.py full --socket-mode rootless --resolved-images ops/images/resolved-images.json --runtime-image-env "$phase10_result_dir/rootless-runtime.env" --result "$phase10_result_dir/phase10-chaos-rootless.json"
uv run python scripts/run_phase10_chaos.py aggregate --input "$phase10_result_dir/phase10-chaos-rootful.json" --input "$phase10_result_dir/phase10-chaos-rootless.json" --chaos-evidence docs/evidence/phase10-chaos.json --final-evidence docs/evidence/phase10-final-exit.json
uv run python scripts/run_phase10_chaos.py validate-evidence docs/evidence/phase10-chaos.json
uv run python scripts/run_phase10_final_exit.py validate-evidence docs/evidence/phase10-final-exit.json
uv run pytest tests/test_phase10_final_exit_harness.py -q
uv run ruff check scripts/phase10_backup.py scripts/phase10_restore.py scripts/phase10_upgrade.py scripts/run_phase10_final_exit.py scripts/run_phase10_chaos.py tests/test_phase10_final_exit_harness.py tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py tests/integration/test_phase10_final_exit.py
uv run mypy scripts/phase10_backup.py scripts/phase10_restore.py scripts/phase10_upgrade.py scripts/run_phase10_final_exit.py scripts/run_phase10_chaos.py tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py
```

Expected: native `prepare-runtime` reproduces the newly recorded operations child in both modes; all twenty scenario rows pass. Scenario 10 starts from Phase 9, reaches current state, backs up, restores fresh, survives three worker kills, and leaves no resources or secret artifacts.

- [ ] **Step 8: Guard, stage exactly Task 11, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add compose.phase10-final-exit.yaml scripts/phase10_backup.py scripts/phase10_restore.py scripts/phase10_upgrade.py scripts/run_phase10_final_exit.py tests/test_phase10_final_exit_harness.py tests/integration/phase10_chaos_harness.py tests/integration/phase10_chaos_assertions.py scripts/run_phase10_chaos.py tests/integration/test_phase10_final_exit.py ops/images/resolved-images.json docs/evidence/phase10-image-security.json docs/evidence/phase10-sizing.json docs/evidence/phase10-rate-limits.json docs/evidence/phase10-backup.json docs/evidence/phase10-restore.json docs/evidence/phase10-upgrades.json docs/evidence/phase10-hardening.md docs/evidence/phase10-secret-audit.json docs/evidence/phase10-chaos.json docs/evidence/phase10-final-exit.json
git diff --cached --check
git commit -m "test: prove restored Phase 10 recovery exit"
```

Expected: commit 12 of 15 records all ten rootful/rootless scenarios and the final restored exit. This freezes `run_phase10_secret_audit.py`, `run_phase10_chaos.py`, and `phase10_chaos_artifact.py` behavior for the remainder of Phase 10; Tasks 12–13 may only invoke those runners, so the evidence generated here remains current for candidate preflight.

### Task 12: Split Exact Pull-Request, Nightly, and Protected Manual Release Gates

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `.github/workflows/phase10-chaos-nightly.yml`
- Modify: `.github/workflows/release-security.yml`
- Create: `tests/test_phase10_ci_schedule.py`

**Interfaces:**
- Consumes: existing PR unit/DLQ/migration/production-Compose jobs, time-skipping tests, the frozen Task 4 secret-audit smoke runner, the frozen Task 6 artifact packager/three-worker smoke runner as extended only by Task 11's scenario-10/full-matrix work, backup/restore/upgrade/scans, and protected release environment.
- Produces: exact PR gates, scheduled nightly rootful/rootless full evidence, minimal CI permissions/no provider secrets, sanitized failure uploads, and a manual real GitHub+Linear gate isolated from normal CI.

- [ ] **Step 1: Write RED workflow graph, command, permissions, secrets, and artifact tests**

Parse workflows as YAML and require exact trigger/job content:

```python
def test_nightly_has_full_matrix_and_operations_gates(workflows) -> None:
    nightly = workflows["phase10-chaos-nightly.yml"]
    assert nightly["on"]["schedule"] == [{"cron": "17 6 * * *"}]
    assert nightly["permissions"] == {"contents": "read"}
    commands = workflows.run_commands(nightly)
    assert "scripts/run_phase10_chaos.py full" in commands
    assert "make phase10-backup-restore-drill" in commands
    assert "scripts/phase10_upgrade.py all" in commands
    assert "make phase10-image-security" in commands
```

Require PR commands for all nonintegration Python/web tests, the five concrete Temporal time-skipping files, live DLQ/retry, secret-canary smoke, `0014 -> 0018` plus downgrade/re-upgrade migrations, production Compose, and exactly these recovery smoke variants: agent post-manifest bind, tool post-claim/pre-effect, event post-handler/pre-ack. Reject a PR/full-nightly mismatch in scenario registry hashes.

For every workflow step whose command invokes a live Phase 10 runner or drill, tests resolve the matrix mode and require an exact complete socket environment on that same step: rootful has `PHASE10_SOCKET_MODE=rootful`, `/var/run/docker.sock`, and the validated positive `PHASE10_DOCKER_SOCKET_GID`; rootless has `PHASE10_SOCKET_MODE=rootless` and the validated UID-10001 `PHASE10_ROOTLESS_DOCKER_SOCKET`, with both GID keys absent. Reject a missing, partial, mixed, inherited-only, hard-coded unvalidated GID, or mode/CLI disagreement. Recording-runner unit tests in Tasks 4, 6, and 11 enforce the same rule outside YAML.

For all three workflows assert: top-level `permissions: contents: read`; checkout credentials not persisted; no `pull_request_target`; no provider hostname/token in verification jobs; no unvalidated artifact upload; explicit job timeouts; concurrency; always-run sanitized diagnostics/teardown; and no raw `docker logs`, Compose status, JUnit, `.env`, key/canary/backup/OCI/scan path in upload steps. `release-security.yml` has a required string `release_candidate_sha`; the manual job checks out exactly that full SHA, asserts `HEAD` equality before provider access, and records it in its safe shard. Scope the no-write/no-`id-token`/no-package-permission and no-secret/no-environment assertions to PR, nightly, scan, aggregation, and final-verifier jobs. Separately assert the protected manual-provider job has only `contents: read` plus its exact two provider-secret families, and the pre-existing separately authorized tag-publisher job has only `contents: read`, `packages: write`, and `id-token: write`, its release-publish environment, and no provider secrets; both exceptions are unreachable from PR/push/schedule verification. The test helper also has an optional exact whitelist for Task 13's not-yet-present `phase10-candidate-publisher`: if that job exists, it must have only `contents: write`/`actions: write`, the protected candidate-publication environment, no checkout/provider secret/OIDC/package authority, and dispatch-only reachability. Task 13 makes its presence mandatory and tests its behavior.

Run:

```bash
uv run pytest tests/test_phase10_ci_schedule.py tests/test_phase10_chaos_artifact.py -q
```

Expected: FAIL because the nightly workflow, exact split, manual gate, and strengthened artifact policy are absent.

- [ ] **Step 2: Add the exact PR gate without third-party calls**

Keep existing lint/typecheck/unit/web/image and Phase 10 DLQ jobs. Before any live rootful step, require `/var/run/docker.sock` to be a nonsymlink socket, derive and validate its exact positive numeric GID into `PHASE10_ROOTFUL_GID`, and set that step's complete authority environment to `PHASE10_SOCKET_MODE=rootful`, `PHASE10_DOCKER_SOCKET=/var/run/docker.sock`, and `PHASE10_DOCKER_SOCKET_GID=$PHASE10_ROOTFUL_GID`; no live command inherits a partial/default authority. Add or pin these PR steps/jobs:

1. `uv run pytest packages/workflows/tests/test_heartbeat_workflow.py packages/workflows/tests/test_triggered_task_workflow.py packages/workflows/tests/test_engineering_ticket_workflow.py packages/workflows/tests/test_delegated_task_workflow.py packages/workflows/tests/test_agent_task_delegation.py -q`;
2. existing `PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$PHASE10_ROOTFUL_GID" make test-phase10-dlq-retry`;
3. `PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$PHASE10_ROOTFUL_GID" uv run python scripts/run_phase10_secret_audit.py pr-smoke --socket-mode rootful`;
4. all migration graph and live `0014 -> 0018 -> 0017 -> 0018` tests;
5. `uv run python scripts/assert_phase10_production_compose.py`;
6. `PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$PHASE10_ROOTFUL_GID" uv run python scripts/run_phase10_chaos.py pr-smoke --socket-mode rootful --agent-boundary phase9.agent.after_manifest.before_effect.v1 --tool-boundary phase10.tool.after_claim.before_effect.v1 --event-boundary phase10.event.after_handler.before_ack.v1`.

The PR chaos command creates a distinct project for each of the three variants and still runs every common authority/canary assertion. All model, connector, webhook, GitHub, Linear, Vercel, Supabase, database, and sandbox calls target repository fake services on the private project network. Application egress to nonproject networks is denied during the tests. Normal CI receives no third-party credentials and treats any attempted provider hostname as failure.

- [ ] **Step 3: Add the nightly full ten plus backup/restore/upgrade/scans**

`.github/workflows/phase10-chaos-nightly.yml` runs on the exact daily cron and `workflow_dispatch`, with a rootful/rootless matrix on dedicated self-hosted labels, maximum 180 minutes per cell, and concurrency keyed by mode/commit. Each cell syncs locked dependencies, prepares the authority-specific mode-`0600` runtime-image env from current resolved digests, passes that mandatory pair to the full runner, validates production Compose, runs all ten scenarios, the combined backup/restore drill, previous-release `phase10_upgrade.py all`, and dependency/container scans. Scenario 10 may reuse that cell's validated upgrade/backup artifacts only within the same private temporary root; it still restores a new target.

An aggregate job requires both mode shards, matching commit/image/registry hashes, twenty scenario rows, backup/restore/upgrade/scan pass, zero skip/cancel/duplicate, and no teardown residue. It validates `phase10-chaos.json`, `phase10-final-exit.json`, `phase10-secret-audit.json`, and image-security evidence but does not commit from CI.

- [ ] **Step 4: Sanitize before every artifact upload**

Always-run steps never redirect or persist raw status, logs, pytest/JUnit, histories, messages, inspect output, or scan output. Workflows invoke the already-implemented Task 6 `phase10_chaos_artifact.py package` contract unchanged: it owns each bounded producer subprocess, consumes stdout/stderr through pipes with the streaming scanner, reduces only scanned chunks to closed schema counters/enums, and discards them immediately. It validates canonical safe bytes and publishes only through `publish_validated_artifact` into a mode-`0700` `safe-upload` directory. The canary manifest stays in the harness-private control root, is read only for scanning, and is removed by outer teardown—not copied to artifact scratch. Only `safe-upload/*.json` is passed to `actions/upload-artifact`; `include-hidden-files` is false and missing safe output fails the upload step.

Workflow tests inspect every upload path, producer argv, redirection, and environment. They reject `tee`, output redirection, `--junitxml`, named diagnostic/temp/cache paths, raw `docker logs`/status writes, or an upload producer other than the sanitizer. Kill the producer and packager independently before/after scanning and before anonymous publication; no directory entry may exist, upload must be suppressed, and the job must fail. No backup/key/certificate/identity, Docker config/inspect, raw service output, Temporal history, NATS message, database dump, OCI/SBOM/provenance/raw scan, provider payload, absolute path, or ID can enter artifacts. Scanner rejection suppresses upload and fails the job.

- [ ] **Step 5: Keep the real GitHub+Linear release path protected and manual**

Extend `.github/workflows/release-security.yml` with required string `workflow_dispatch` input `release_candidate_sha`, boolean input `run_manual_provider_exit` default false, and a job guarded by the protected `phase10-release-manual` environment. Only this job may receive short-lived GitHub App and Linear test-workspace secrets. It checks out exactly the full candidate SHA with credentials disabled, requires `git rev-parse HEAD` equality before provider access, runs on a clean supported host/current images, follows README owner onboarding without source edits, validates a signed Linear event, performs isolated repository work in a socket-free sandbox job, and creates one test pull request through the normal approval/tool-worker path.

The job records provider types, counts, signed-webhook/isolated-sandbox/PR booleans, clean-onboarding boolean, immutable `release_candidate_sha`, image hash, date, and pass only. It records no organization/repository/workspace/user/issue/PR ID, URL, token, payload, title, branch, or log. Cleanup closes/deletes only resources carrying the run's exact test label under operator-approved credentials. This job is absent from PR/push/schedule execution and is the sole source for Task 13's manual evidence.

- [ ] **Step 6: Run GREEN static schedule tests and local smoke commands**

```bash
set -euo pipefail
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
phase10_pr_rootful_gid="$(stat -c %g /var/run/docker.sock)"
case "$phase10_pr_rootful_gid" in ''|*[!0-9]*) exit 1 ;; esac
test "$phase10_pr_rootful_gid" -gt 0
uv run pytest tests/test_phase10_ci_schedule.py tests/test_phase10_chaos_artifact.py -q
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_pr_rootful_gid" uv run python scripts/run_phase10_secret_audit.py pr-smoke --socket-mode rootful
PHASE10_SOCKET_MODE=rootful PHASE10_DOCKER_SOCKET=/var/run/docker.sock PHASE10_DOCKER_SOCKET_GID="$phase10_pr_rootful_gid" uv run python scripts/run_phase10_chaos.py pr-smoke --socket-mode rootful --agent-boundary phase9.agent.after_manifest.before_effect.v1 --tool-boundary phase10.tool.after_claim.before_effect.v1 --event-boundary phase10.event.after_handler.before_ack.v1
git diff --exit-code HEAD -- scripts/phase10_chaos_artifact.py scripts/run_phase10_chaos.py scripts/run_phase10_secret_audit.py
uv run ruff check tests/test_phase10_ci_schedule.py
```

Expected: PASS; PR smoke uses only fakes, nightly declares all ten/operations/scans in both modes, and the real-provider job is manual/protected only.

- [ ] **Step 7: Guard, stage exactly Task 12, and commit**

```bash
set -euo pipefail
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git add .github/workflows/ci.yml .github/workflows/phase10-chaos-nightly.yml .github/workflows/release-security.yml tests/test_phase10_ci_schedule.py
git diff --cached --check
git commit -m "ci: schedule Phase 10 recovery security gates"
```

Expected: commit 13 of 15 implements the exact PR/nightly/manual split with sanitized artifacts.

### Task 13: Commit the Immutable Release Verifier, Then Close Phase 10 From Its Evidence

**Files:**
- Create: `scripts/record_phase10_security_evidence.py`
- Create: `scripts/verify_phase10_exit.py`
- Create: `tests/test_phase10_exit_evidence.py`
- Create: `docs/evidence/phase10-manual-release.json`
- Create: `docs/evidence/phase10-security.md`
- Modify: `docs/security/phase10-secret-data-flow.md`
- Modify: `docs/operations/chaos-recovery.md`
- Modify: `docs/implementation-plan.md`
- Modify: `.github/workflows/release-security.yml`

**Interfaces:**
- Consumes: all predecessor completion commits/evidence, runbooks hardening evidence, cross-sink/rootful/rootless evidence, twenty chaos rows, final restored exit, protected real-provider evidence, and unchanged Phase 11 content.
- Produces: first, a green verifier/tooling commit whose SHA becomes the immutable release candidate; second, a protected owner-authorized, create-only `phase10-candidate-SHA` tag plus a workflow run dispatched by that ref but bound everywhere to the raw SHA; third, fresh rootful/rootless/manual artifacts and deterministic Markdown all bound to that SHA; fourth, a documentation-only closure commit whose first parent is the candidate. It closes exactly fourteen Phase 10 boxes and section 49, never Phase 11.

- [ ] **Step 1: Write RED candidate-binding, semantic-evidence, workflow, and checklist tests**

Test missing/stale repository evidence, image/input hash mismatch, absent mode/scenario/sink, skipped/cancelled work, canary violation, failed/expired scan allowance, incomplete backup/restore/upgrade, unhealthy final state, nonzero lag, repeated ambiguous effect, teardown residue, and missing manual gate. Pin the manual schema and immutable candidate:

```python
import re


def test_manual_release_evidence_is_allowlisted(manual_release_document) -> None:
    assert set(manual_release_document) == {
        "schema_version", "release_candidate_sha", "image_set_sha256", "date",
        "clean_supported_host", "owner_onboarding", "provider_types",
        "signed_linear_event", "isolated_repository_work", "pull_request_created",
        "external_effect_count", "cleanup", "status",
    }
    assert re.fullmatch(r"[0-9a-f]{40}", manual_release_document["release_candidate_sha"])
    assert manual_release_document["provider_types"] == ["github", "linear"]
    assert manual_release_document["external_effect_count"] == 1
    assert manual_release_document["cleanup"] is True
    assert manual_release_document["status"] == "pass"


def test_release_final_verifier_binds_every_job_to_candidate(workflows) -> None:
    release = workflows["release-security.yml"]
    inputs = release["on"]["workflow_dispatch"]["inputs"]
    candidate = inputs["release_candidate_sha"]
    assert candidate["type"] == "string"
    assert candidate["required"] is True
    assert inputs["candidate_ref"]["type"] == "string"
    assert inputs["candidate_ref"]["required"] is True
    assert inputs["dispatch_nonce"]["type"] == "string"
    assert inputs["dispatch_nonce"]["required"] is True
    assert inputs["operation"]["type"] == "choice"
    assert inputs["operation"]["options"] == ["publish_candidate", "verify_candidate"]
    jobs = release["jobs"]

    publisher = jobs["phase10-candidate-publisher"]
    assert publisher["permissions"] == {"contents": "write", "actions": "write"}
    assert publisher["environment"]["name"] == "phase10-release-candidate"
    assert workflows.secret_references(publisher) == set()
    assert not workflows.uses_checkout(publisher)
    publish_commands = workflows.run_commands(publisher)
    for required in (
        "refs/tags/phase10-candidate-",
        "create-only",
        "operation=verify_candidate",
        "ref=candidate_ref",
        "head_sha",
        "dispatch_nonce",
        "run_id",
    ):
        assert required in publish_commands

    for job_id in (
        "phase10-release-rootful",
        "phase10-release-rootless",
        "phase10-manual-provider-exit",
        "phase10-final-verifier",
    ):
        workflows.assert_checkout_ref(jobs[job_id], "${{ inputs.release_candidate_sha }}")
        workflows.assert_exact_head_guard(jobs[job_id], "${{ inputs.release_candidate_sha }}")
        workflows.assert_exact_candidate_ref_guard(
            jobs[job_id],
            "${{ inputs.candidate_ref }}",
            "${{ inputs.release_candidate_sha }}",
        )

    verifier = jobs["phase10-final-verifier"]
    assert verifier["permissions"] == {"contents": "read"}
    assert set(verifier["needs"]) == {
        "phase10-release-rootful",
        "phase10-release-rootless",
        "phase10-manual-provider-exit",
    }
    assert "environment" not in verifier
    assert workflows.secret_references(verifier) == set()
    commands = workflows.run_commands(verifier)
    assert "aggregate-fresh" in commands
    assert "verify_phase10_exit.py fresh" in commands
    assert "--check docs/evidence/phase10-security.md" not in commands
    assert "verify_phase10_exit.py checklist" not in commands

    workflows.assert_exact_socket_authority(jobs["phase10-release-rootful"], mode="rootful")
    workflows.assert_exact_socket_authority(jobs["phase10-release-rootless"], mode="rootless")
```

The local `workflows` fixture implements the used `uses_checkout`, checkout/head/ref guard, socket-authority, command, and secret-reference inspectors by parsing workflow mappings and shell ASTs; no assertion above is pseudocode. Add fixtures with the same semantic results but different safe dates/durations and therefore different bytes. `fresh` must accept both when each independently binds the candidate; `check-fresh` must compare deterministic Markdown only with the exact fresh artifact set that generated it. Add fake-Actions tests proving local preflight makes no network mutation, `owner-dispatch` refuses without an explicit `--owner-authorized` flag, the publisher is reachable only from the protected default ref/environment, candidate publication is one create-only tag operation, self-dispatch uses that exact tag rather than a raw SHA, and both publisher and watcher capture exactly one bounded run whose `head_sha` is the candidate. Reject any shard/manual/aggregate candidate mismatch, candidate ref other than exact `phase10-candidate-SHA`, abbreviated/mixed-case SHA, checkout at workflow HEAD, tag update/delete/replacement, ambiguous/missing run, or reuse of a prior run's byte hashes.

Checklist tests initially require `phase10_checklist_open`. An in-memory completed fixture requires exactly fourteen Phase 10 `[x]`, all twenty section-49 `[x]`, unchanged Phase 10 exit text, and every Phase 11 item `[ ]`.

```bash
uv run pytest tests/test_phase10_exit_evidence.py -q
```

Expected: FAIL because the scripts and final-verifier workflow do not exist and the repository checklist remains open.

- [ ] **Step 2: Implement the strict candidate/manual parsers and make focused RED GREEN**

`parse_release_candidate_sha` accepts exactly 40 lowercase hexadecimal characters, resolves it to a commit, requires it equal checked-out `HEAD` for producer/verifier commands, and never accepts a branch/tag/abbreviation as the evidence authority. `parse_candidate_ref` accepts only the exact derived tag `phase10-candidate-{release_candidate_sha}`; `parse_dispatch_nonce` accepts exactly 32 lowercase hexadecimal characters. Implement the closed manual schema, canonical JSON loader, forbidden-key/value scanner, candidate/image/date binding, and fixture-only `validate-manual`. It rejects provider IDs/URLs/names/payloads/tokens, arbitrary strings, unknown/missing keys, wrong candidate/ref/images/date, count other than one, false booleans, duplicate provider types, and age over 30 days; it emits only a closed code.

```bash
uv run pytest tests/test_phase10_exit_evidence.py -k 'candidate or manual_release' -q
```

Expected: PASS for candidate/manual fixtures and hostile cases. Do not validate a repository manual-evidence path; real evidence cannot exist until the verifier implementation is committed.

- [ ] **Step 3: Implement fresh semantic aggregation and the candidate-pinned release workflow**

`preflight-candidate --release-candidate-sha SHA` validates Alembic `0018`, all six predecessor commits, current committed component evidence, image/registry/policy/input fingerprints, cadences, no owning runtime changes since evidence, and production Compose. Historical committed evidence hashes prove candidate repository inputs; they are not expected to equal a later fresh run's date/duration-bearing bytes.

`verify_phase10_exit.py live --release-candidate-sha SHA --socket-mode MODE --result-root PATH` requires `HEAD == SHA`, performs the complete selected-mode unit/live/scans/secret-audit/ten-scenario/backup/restore/upgrade/final-exit gate, and emits only closed safe shards containing that exact SHA and image set. `aggregate-fresh` requires one rootful, one rootless, and one protected manual shard from the same workflow run, exact candidate/image/registry hashes, 38 sinks, twenty scenario rows, one restored exit per mode, all operational drills/scans, no skip/violation/residue, and valid effect semantics. It permits safe measured dates/durations to differ from committed historical evidence and compares semantic fields, not prior artifact byte hashes.

Extend `release-security.yml` with required strings `release_candidate_sha`, `candidate_ref`, and `dispatch_nonce`; required choice `operation` with only `publish_candidate|verify_candidate`; and boolean `run_phase10_final_verifier` default false. Set a closed `run-name` containing operation, candidate SHA, and nonce so a bounded watcher can identify a unique run without persisting a run ID. Verification jobs run only for `operation == 'verify_candidate'`. They first require `candidate_ref == format('phase10-candidate-{0}', release_candidate_sha)`, `github.ref_type == 'tag'`, `github.ref_name == candidate_ref`, and immutable workflow `github.sha == release_candidate_sha`. All four then use `actions/checkout` with `ref: ${{ inputs.release_candidate_sha }}`, `fetch-depth: 0`, and `persist-credentials: false`, and independently require the input to match `[0-9a-f]{40}` plus `git rev-parse HEAD` equality before any producer. The candidate tag selects the workflow run because `workflow_dispatch` cannot target a raw SHA; the raw SHA remains the sole checkout/evidence authority. Full history is required only so the read-only verifier can prove every evidence commit is an ancestor; it never changes the immutable checkout target.

Add `phase10-candidate-publisher`, reachable only for `operation == 'publish_candidate'` when the bootstrap run itself is on the repository's protected default branch, `github.ref_protected == true`, `candidate_ref` is the exact derived name, and candidate SHA equals that default-branch `github.sha`. The job has the protected `phase10-release-candidate` environment with required owner/release reviewer and deployment-branch restriction to the default branch. Its only job permissions are `contents: write` and `actions: write`; it has no checkout, provider/repository secret, OIDC, package, pull-request, issue, or environment-secret access. A required repository ruleset named `phase10-candidate-immutable` covers `refs/tags/phase10-candidate-*`, rejects update/delete/force, and permits creation only through this owner-gated job.

After read-only checks prove the SHA is a repository commit and current protected-default HEAD, a commit-SHA-pinned `actions/github-script` step makes one REST `git.createRef` request for `refs/tags/{candidate_ref}`. It has `result-encoding: string`, returns only the closed value `candidate_dispatched`, and never prints an API object. It never updates, force-pushes, deletes, or treats an existing ref as success. In the same in-memory script it immediately reads the new ref and requires its object SHA equal the input, then calls `actions.createWorkflowDispatch` for this same workflow with `ref=candidate_ref`, `operation=verify_candidate`, all immutable inputs, and both release booleans true. It records the dispatch start only in memory and polls `actions.listWorkflowRuns` at most 120 seconds for exactly one current-workflow `workflow_dispatch` run whose closed run-name contains the nonce, `head_branch` is the candidate ref, `head_sha` is the candidate SHA, event is `workflow_dispatch`, and creation falls inside the window. It captures that numeric run ID only in the JavaScript local variable used by `actions.getWorkflowRun`, never an artifact, output, log, evidence, or Markdown field, and fails on zero/two runs or any mismatch. Every caught API/action failure maps to one closed publisher code before `core.setFailed`; no exception object, response, URL, ref payload, run object, or identifier is printed. Tests pin the action SHA and reject interpolation of untrusted input into executable JavaScript; inputs enter through validated environment strings only.

`phase10-release-rootful` and `phase10-release-rootless` are verification-dispatch-only, read-only, secret-free shard jobs on validated dedicated runners. Each has the exact socket authority for its mode, runs `live` with the candidate, and uploads only its safe candidate-bound shard directory. `phase10-manual-provider-exit` remains the protected `phase10-release-manual` exception with `contents: read` and only the exact GitHub App/Linear secret families; it checks out the same candidate, performs one real cleaned path, and emits the closed manual shard with no provider detail.

`phase10-final-verifier` needs exactly those three jobs, runs only when both boolean inputs are true and every need succeeded, has `contents: read`, no environment/secrets/socket/provider access, checks out the same candidate, and downloads only the current run's named safe artifacts. It runs `aggregate-fresh` and `verify_phase10_exit.py fresh`, then uploads exactly `phase10-fresh-verification.json`, `phase10-manual-release.json`, and the deterministic `phase10-security.md` render. Every file includes the candidate SHA; the Markdown separately labels hashes as `candidate_repository_inputs` or `fresh_run_inputs`. It does not compare fresh bytes with previously committed measured artifacts, mutate the checkout, or check the still-open checklist.

Preserve the separately authorized release tag/package publisher with its existing publish environment and exact `contents: read`, `packages: write`, and `id-token: write` permissions, no provider secrets. Before publishing a Phase 10 release tag it runs the later `closure` check requiring the tagged commit's first parent to equal the evidence candidate and the intervening diff to contain only the five Task 13 closure documents. No-write/no-secret assertions continue to cover PR, nightly, scan, shard, aggregation, and final-verifier jobs. The only exceptions are (a) protected manual provider secrets with read-only contents, (b) the existing release tag/package publisher, and (c) the new protected candidate-ref publisher with only contents/actions write; tests reject permission union or reachability across those three jobs.

Run focused GREEN before any evidence use:

```bash
uv run pytest tests/test_phase10_exit_evidence.py -k 'candidate or candidate_ref or owner_dispatch or publisher or manual or aggregate or stale or final_verifier or live_runner or fresh' -q
uv run pytest tests/test_phase10_ci_schedule.py -q
uv run ruff check scripts/record_phase10_security_evidence.py scripts/verify_phase10_exit.py tests/test_phase10_exit_evidence.py
uv run mypy scripts/record_phase10_security_evidence.py scripts/verify_phase10_exit.py
```

Expected: PASS for immutable SHA checkout, create-only protected ref publication, dispatch-by-ref with exact `head_sha`, explicit owner mutation gating, semantic fresh-run handling, exact authorities, read-only verification, protected exceptions, and deterministic safe rendering. Checklist-open remains intentional.

- [ ] **Step 4: Commit the green verifier implementation and freeze its SHA**

```bash
set -euo pipefail
uv run pytest -m 'not integration' -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test
pnpm --filter jhin-web build
uv run python scripts/assert_phase10_production_compose.py
git add scripts/record_phase10_security_evidence.py scripts/verify_phase10_exit.py tests/test_phase10_exit_evidence.py .github/workflows/release-security.yml
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git commit -m "ci: add immutable Phase 10 final verifier"
release_candidate_sha="$(git rev-parse HEAD)"
case "$release_candidate_sha" in *[!0-9a-f]*|'') exit 1 ;; esac
test "${#release_candidate_sha}" = "40"
candidate_ref="phase10-candidate-$release_candidate_sha"
uv run python scripts/record_phase10_security_evidence.py preflight-candidate --release-candidate-sha "$release_candidate_sha"
uv run python scripts/verify_phase10_exit.py candidate-ref local-preflight --release-candidate-sha "$release_candidate_sha" --candidate-ref "$candidate_ref" --workflow .github/workflows/release-security.yml
```

Expected: commit 14 of 15 is green and is now the immutable release candidate. Local preflight proves its exact future ref/workflow contract without a network mutation; no ref is created, no workflow is dispatched, and no evidence/checklist document has been created or edited.

- [ ] **Step 5: Pause for owner-authorized candidate publication/dispatch, then materialize only its safe aggregate**

Publishing a Git ref and dispatching Actions are external repository mutations; the Phase 10 implementation request does not silently authorize either. After local preflight, the implementation agent must stop here—without `git push`, `gh workflow run`, a Git-ref API call, or an Actions-dispatch API call—and request the repository owner's explicit release-gate authorization. The owner first makes commit 14 the exact HEAD of the protected default branch through the repository's ordinary approved merge/publication path. This is an explicit Phase 11/release gate, not a reason to mark any Phase 11 checkbox.

Once separately authorized, `verify_phase10_exit.py candidate-ref remote-preflight` performs read-only checks that the default remote HEAD equals the local candidate, the required immutable candidate-tag ruleset exists, the candidate environment has an owner/release reviewer and permits only the protected default branch, and the workflow at that commit has the locally tested bytes. It never creates a ref or dispatches a workflow.

`verify_phase10_exit.py owner-dispatch` is the only local mutation entry point. It requires the literal `--owner-authorized` flag, an exact protected bootstrap ref, exact derived candidate ref, candidate SHA, and random closed nonce. Without the flag or any preflight proof it performs zero HTTP mutations. With authorization it dispatches the protected-default workflow with `operation=publish_candidate`, the immutable inputs, and both release booleans false—not at the raw SHA—and records the bounded start only in memory. Only the publisher job is reachable in that bootstrap run; its self-dispatch sets `operation=verify_candidate` and both booleans true. The local watcher captures exactly one bootstrap run, then exactly one self-dispatched verification run by nonce/ref/`head_sha`, watches that run to success, and downloads only the final verifier's named aggregate artifact. It rejects a branch/ref substitution, a second candidate/run, an artifact from another run, missing protected approval, or any member outside the three-file closed manifest. Run IDs remain in memory only.

First run the mutation-free local pause point:

```bash
set -euo pipefail
release_candidate_sha="$(git rev-parse HEAD)"
case "$release_candidate_sha" in *[!0-9a-f]*|'') exit 1 ;; esac
test "${#release_candidate_sha}" = "40"
candidate_ref="phase10-candidate-$release_candidate_sha"
uv run python scripts/verify_phase10_exit.py candidate-ref local-preflight --release-candidate-sha "$release_candidate_sha" --candidate-ref "$candidate_ref" --workflow .github/workflows/release-security.yml
```

Stop here unless the owner explicitly authorizes the protected release gate. The following block is owner-only after that authorization and after commit 14 is the protected default HEAD:

```bash
set -euo pipefail
release_candidate_sha="$(git rev-parse HEAD)"
case "$release_candidate_sha" in *[!0-9a-f]*|'') exit 1 ;; esac
test "${#release_candidate_sha}" = "40"
candidate_ref="phase10-candidate-$release_candidate_sha"
dispatch_nonce="$(uv run python -c 'import secrets; print(secrets.token_hex(16))')"
case "$dispatch_nonce" in *[!0-9a-f]*|'') exit 1 ;; esac
test "${#dispatch_nonce}" = "32"
owner_repository="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
owner_release_base="$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)"
test -n "$owner_repository"
test -n "$owner_release_base"
test "$(gh api "repos/$owner_repository/git/ref/heads/$owner_release_base" --jq .object.sha)" = "$release_candidate_sha"
uv run python scripts/verify_phase10_exit.py candidate-ref remote-preflight --repository "$owner_repository" --bootstrap-ref "$owner_release_base" --release-candidate-sha "$release_candidate_sha" --candidate-ref "$candidate_ref" --workflow .github/workflows/release-security.yml
phase10_verified_root="$(mktemp -d)"
trap 'find "$phase10_verified_root" -depth -mindepth 1 -delete; rmdir "$phase10_verified_root"' EXIT
uv run python scripts/verify_phase10_exit.py owner-dispatch --owner-authorized --repository "$owner_repository" --workflow release-security.yml --bootstrap-ref "$owner_release_base" --release-candidate-sha "$release_candidate_sha" --candidate-ref "$candidate_ref" --dispatch-nonce "$dispatch_nonce" --result-root "$phase10_verified_root"
uv run python scripts/verify_phase10_exit.py fresh --release-candidate-sha "$release_candidate_sha" --result-root "$phase10_verified_root"
uv run python scripts/record_phase10_security_evidence.py render-fresh --release-candidate-sha "$release_candidate_sha" --direct-result-root "$phase10_verified_root" --security-output docs/evidence/phase10-security.md --manual-output docs/evidence/phase10-manual-release.json
uv run python scripts/record_phase10_security_evidence.py check-fresh --release-candidate-sha "$release_candidate_sha" --direct-result-root "$phase10_verified_root" --security docs/evidence/phase10-security.md --manual docs/evidence/phase10-manual-release.json
uv run python scripts/record_phase10_security_evidence.py validate-manual docs/evidence/phase10-manual-release.json --release-candidate-sha "$release_candidate_sha"
uv run python scripts/verify_phase10_exit.py evidence --security docs/evidence/phase10-security.md --release-candidate-sha "$release_candidate_sha"
```

Expected: without separate owner authority, execution stops after local preflight with no external mutation and the checklist untouched. With authority, the create-only protected candidate tag resolves exactly to commit 14, the verification run's `head_sha` equals that commit, and both socket modes plus the protected clean-host GitHub+Linear path pass before any checked-in evidence is rendered.

- [ ] **Step 6: Review deterministic candidate-bound evidence without conflating byte histories**

The preceding bounded command renders the two checked-in evidence files from only the downloaded candidate-bound safe summaries and validates them before its private root is deleted. Never hand-edit either generated file.

`phase10-security.md` records all current image hashes, 38 sinks, ten scenarios/two modes, restored exit, scans/hardening, fake-only normal CI, protected provider result, and safe counts/status. It stores SHA-256 of the exact fresh safe inputs under `fresh_run_inputs`; committed evidence fingerprints remain separately named `candidate_repository_inputs`. A fresh result is never required to reproduce a previous measured artifact's bytes.

- [ ] **Step 7: Finalize the threat and recovery documents from the same aggregate**

Update each of 38 data-flow rows with candidate SHA, direct evidence ID, both-mode result, and date. Update each of ten recovery sections with candidate SHA, observed safe state, recovery outcome, effect rule, loss/recovery bounds, sanitized diagnostic validation, and both-mode pass. Add no raw values, runtime IDs, paths, URLs, logs, traces, metrics, histories, or provider details.

- [ ] **Step 8: Mark only Phase 10 and section 49 complete**

Change exactly fourteen Phase 10 entries to `[x]` and exactly twenty section-49 readiness bullets to `[x]`, preserving their text. Keep the Phase 10 deliverable/exit text and every Phase 11 byte unchanged.

```bash
uv run python scripts/verify_phase10_exit.py checklist --implementation-plan docs/implementation-plan.md
uv run pytest tests/test_phase10_exit_evidence.py -q
```

Expected: PASS with Phase 10 `14/14`, section 49 `20/20`, and Phase 11 open.

- [ ] **Step 9: Run final candidate, schema, secret-output, scope, and whitespace audits**

```bash
set -euo pipefail
release_candidate_sha="$(git rev-parse HEAD)"
case "$release_candidate_sha" in *[!0-9a-f]*|'') exit 1 ;; esac
test "${#release_candidate_sha}" = "40"
test "$(uv run python -c 'from alembic.script import ScriptDirectory; from jhin_db.migrate import alembic_config; print(ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head())')" = "0018"
uv run python scripts/verify_phase10_exit.py evidence --security docs/evidence/phase10-security.md --release-candidate-sha "$release_candidate_sha"
uv run python scripts/verify_phase10_exit.py checklist --implementation-plan docs/implementation-plan.md
git status --short -- docs/evidence/phase10-manual-release.json docs/evidence/phase10-security.md docs/implementation-plan.md docs/operations/chaos-recovery.md docs/security/phase10-secret-data-flow.md | cut -c4- | sort | diff -u <(printf '%s\n' docs/evidence/phase10-manual-release.json docs/evidence/phase10-security.md docs/implementation-plan.md docs/operations/chaos-recovery.md docs/security/phase10-secret-data-flow.md) -
! rg -n 'postgresql://|AGE-SECRET-KEY|BEGIN .*PRIVATE KEY|password=|authorization:|cookie:|container_id|volume_id|raw_log' docs/evidence docs/security docs/operations
uv run python scripts/assert_phase10_production_compose.py
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git diff --check
```

Expected: only the five closure documents differ from the candidate; they all bind its SHA and contain no forbidden output.

- [ ] **Step 10: Commit the documentation-only closure and verify its parent/scope**

```bash
set -euo pipefail
release_candidate_sha="$(git rev-parse HEAD)"
case "$release_candidate_sha" in *[!0-9a-f]*|'') exit 1 ;; esac
test "${#release_candidate_sha}" = "40"
git add docs/evidence/phase10-manual-release.json docs/evidence/phase10-security.md docs/security/phase10-secret-data-flow.md docs/operations/chaos-recovery.md docs/implementation-plan.md
git diff --cached --check
test -z "$(git diff --cached --name-only -- orgforge-production-implementation-plan.md)"
test "$(stat -c %s orgforge-production-implementation-plan.md)" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d' ' -f1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
git commit -m "docs: close Phase 10 from candidate-bound recovery evidence"
test "$(git rev-parse HEAD^)" = "$release_candidate_sha"
git diff --name-only "$release_candidate_sha" HEAD | diff -u <(printf '%s\n' docs/evidence/phase10-manual-release.json docs/evidence/phase10-security.md docs/implementation-plan.md docs/operations/chaos-recovery.md docs/security/phase10-secret-data-flow.md) -
uv run python scripts/verify_phase10_exit.py closure --closure-sha "$(git rev-parse HEAD)" --security docs/evidence/phase10-security.md --implementation-plan docs/implementation-plan.md
git status --short -- scripts/record_phase10_security_evidence.py scripts/verify_phase10_exit.py tests/test_phase10_exit_evidence.py docs/evidence/phase10-manual-release.json docs/evidence/phase10-security.md docs/security/phase10-secret-data-flow.md docs/operations/chaos-recovery.md docs/implementation-plan.md .github/workflows/release-security.yml
git status --short -- orgforge-production-implementation-plan.md
```

Expected: commit 15 of 15 is a five-document child of the exact verified candidate. Implementation-path status is clean, the sentinel remains its original untracked entry, Phase 10 is complete, and Phase 11 remains open.

## Execution Handoff

Implement Tasks 0–13 in order on Linux. Stop whenever RED fails for an unexpected reason, a service/dependency action cannot prove exact project authority, an artifact/canary scan fails, cleanup leaves labelled resources, current image/evidence hashes diverge, or the protected manual gate is absent. Task 13 Step 5 is a mandatory external-authority boundary: complete local preflight, then stop unless the owner separately authorizes candidate-ref publication and Actions dispatch; never push or dispatch implicitly. Do not edit `docs/implementation-plan.md` before Task 13 Step 8, and do not create checked-in release evidence before the protected commit-14 candidate workflow succeeds. Never edit a predecessor/sibling plan or the OrgForge sentinel.
