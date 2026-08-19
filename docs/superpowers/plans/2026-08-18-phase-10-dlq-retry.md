# Phase 10 DLQ and Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable, workspace-safe event quarantine/replay and conservative manual task retry controls without allowing a sixth handler attempt, losing a publish/start command, or repeating a committed or ambiguous external effect.

**Architecture:** PostgreSQL owns handler attempt counts, renewable fenced claims, immutable external-call authorizations, durable workspace-deletion intent, terminal failures, replay/task-retry commands, ordinary task-start authority, and the DLQ outbox. The event worker wraps both INGRESS and EVENTS business handlers in one deletion-gated lease state machine, then runs independently claimed reconcilers for DLQ publication, event replay, ordinary task start, task retry, and workspace-deletion drain. A neutral installed `jhin-recovery` package owns admission, workflow inventory, and reconciliation shared by API and workers. NATS and Temporal remain delivery/history authorities, but neither is the command-status source of truth. Prospective authorization is evaluated only before the first durable external-call authorization; afterward absence and lease expiry permit only same-identity reconciliation/re-drive and never terminal exhaustion. The web renders only allowlisted safe projections and never raw source messages, Temporal failures, or DLQ payloads.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, PostgreSQL 17, nats-py JetStream, Temporal Python SDK, structlog/redaction helpers, Next.js/React 19, TanStack Query, Vitest, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-18-phase-10-production-operations-design.md`, especially “Data model,” “DLQ and event replay,” “Task retry controls and at-most-once rules,” “Operations audit events,” sub-project 4, sequencing/migration expectations, recovery scenarios 3/4/6, and health/DLQ/retry acceptance evidence.

## Global Constraints

- Execute only after subprojects 1-3 are merged and Alembic head is exactly `0015`. This subproject owns one additive revision, `20260818_0016_dlq_retry.py`, with `revision = "0016"` and `down_revision = "0015"`; it must preserve the one-head graph.
- PostgreSQL is the product/command source of truth, Temporal is workflow-history authority, and NATS JetStream is at-least-once transport. Browser state and telemetry never authorize or reconcile work.
- `processing_max_attempts` is fixed at 5. JetStream application consumers use unlimited delivery (`max_deliver = -1`); a PostgreSQL outage must not consume a handler attempt or cause JetStream to abandon a message.
- `event_processing_state` is keyed by `(origin_stream, consumer_name, source_stream_sequence)`. A live lease suppresses concurrent handling, an expired lease permits the next numbered attempt, `quarantine_only` can only run quarantine persistence, and `completed` can never invoke business handling.
- The fifth handler exception commits `quarantine_only` before quarantine is attempted. Failure, outbox intent, `event.processing_failed` audit, and `quarantine_only -> completed` commit atomically. Any failure in that transaction leaves the previously committed `quarantine_only` state intact.
- A source message is acknowledged only after a successful-handler `completed` commit and terminated only after a completed-quarantine commit. A redelivery after either commit never enters the business handler.
- Failure/outbox/replay/task-retry records store IDs, closed enums, bounded metadata, and already-redacted safe text only. No raw NATS body, webhook payload, model content, tool arguments, provider response/error, Authorization/cookie/secret, SQL, DSN, or traceback is persisted or returned.
- Prospective replay and retry starts recheck current workspace membership, workspace/agent state, the existing agent budget, concurrency admission, source availability, and safety. A member may request an eligible task retry; only an admin may inspect/resolve/replay workspace event failures. There is no workspace-budget setting or invented authority in this subproject.
- Manual task retry is forbidden after any `ToolCall` in `executing` or `execution_unknown`, or any completed/failed tool whose persisted `retry_safety` is not `pure`. `idempotent` permits automatic recovery under the same invocation ID; it never permits a fresh model attempt.
- Duplicate HTTP requests with one idempotency key return one command. After taking the canonical workspace/deletion/target locks, replay and retry creation requery that key **before** mutable eligibility/state/counter checks: an exact target/requester binding returns the existing command even when the winner already changed parent state; any mismatch returns the fixed conflict. The unique-violation reload remains a defensive last race close. Distinct keys cannot create two nonterminal commands for one failure/task. Dispatcher claims expire, and deterministic replay event/workflow IDs close publish/start crash gaps.
- Replay reads the exact original NATS stream sequence. It never exposes those bytes through an API. Missing/deleted/aged-out source data produces `source_event_expired`, a failed request, and an `expired` failure.
- Replay preserves original `event_type`, `event_version`, `occurred_at`, `workspace_id`, `source`, `data`, and `correlation_id`; sets a fresh `received_at`; sets `causation_id` to the exact source transport event ID, sets `replay_of_event_id` to `source.replay_of_event_id or source.event_id`, and uses the request-derived deterministic event ID as `Nats-Msg-Id`.
- `replay_of_event_id` is the stable replay root, not merely the immediately previous transport ID. INGRESS normalization derives every canonical event ID from `replay_of_event_id or event_id`; TriggerMatcher uses the same root when `data.external_id` is absent. A handler is replayable only when a registered, tested semantic-key function returns a stable key that is unchanged by replay transport IDs.
- Trigger and manual-retry starts always pass `WorkflowIDReusePolicy.REJECT_DUPLICATE`. An already accepted start is success even after the workflow closes; ambiguous client errors are reconciled by deterministic start/describe and never converted into permission to start a different workflow ID.
- Task retry request eligibility describes the synthesized base ID `task-{task_id}`, the bounded persisted ordinary Task ID, every bounded prior retry/run workflow ID, and treats an unlinked Temporal history as an ambiguous prior attempt. NotFound is absence only for a never-authorized candidate; a persisted ordinary/retry authorization is outcome-unknown and same-ID reconciliation blocks a fresh retry until terminal proof. Dispatcher eligibility is separate: only the exact queued task bound to its own claimed retry command may start.
- `Task.temporal_workflow_id` and `Task.temporal_start_authorized_at` belong only to the ordinary attempt and are immutable once authorized. Before either ordinary or manual Temporal call, its owner also commits a versioned, canonical, bounded immutable `AgentTaskInput` JSON contract. Every initial caller and same-ID re-drive deserializes only that contract; it never rebuilds input from mutable `Task.assigned_agent_id`, `Task.description`, agent existence, or current configuration. Every version-1 authorized, unaccepted, uncleared ordinary authority remains durably due and is repaired regardless of mutable Task state or manual-retry queue metadata; the repair uses only the ordinary ID/input and never interprets a manual binding as ordinary authority. The final projection activity may persist only terminal Task/AgentRun product state: because it executes before the workflow returns, it cannot assert an actual closed Temporal status, mutate reconciliation authority, or clear input. Start acceptance schedules a separate neutral post-close reconciler; it describes the exact accepted workflow after return, validates its deterministic Task/TaskRetry/run binding, then atomically persists actual close status plus terminal proof and clears the input. Open, delayed, unavailable, or ambiguous describe keeps the input and deletion drain. Manual retry never overwrites the ordinary fields: `TaskRetry.temporal_workflow_id` and its own start contract are its sole Temporal authority, queue metadata names the exact retry row, and signal routing resolves the active retry explicitly.
- A trigger start is durable before Temporal: the started `TriggerInvocation` stores its deterministic workflow ID, closed workflow name, versioned bounded input JSON, and authorization timestamp atomically. A neutral background reconciler—not NATS redelivery—describes/re-drives that exact contract with `REJECT_DUPLICATE`; deletion drains authorized trigger starts to accepted terminal proof before cascade.
- At `attempt_count == 20`, no command claim increments to 21 and no different identity may be emitted. `external_call_authorized_at` is the irreversible handoff fence: before it is set, a command may fail prospectively with `dispatch_exhausted`; after it is set, neither lease expiry, a bounded-scan miss, Temporal NotFound, nor current authorization/configuration/deletion drift proves that a paused caller cannot resume. The row remains reconcilable and may re-drive only its immutable message/event/workflow identity without incrementing, below or at the cap.
- Command `attempt_count` is reachable and precise: each persisted typed transient failure before authorization increments it once, and the first immutable external-call authorization increments it once; database failure before that transaction increments nothing. A null-authorization row reaching 20 terminalizes, while an authorized row never increments again. Concurrency waits and nonretryable validation/source/authorization failures do not consume an attempt.
- Same-identity reconciliation is safe by contract: outbox transport duplicates collapse at a downstream durable `message_id` receipt, replay transport duplicates collapse at registered handler semantic keys rooted in the immutable replay identity, and Temporal always uses `WorkflowIDReusePolicy.REJECT_DUPLICATE`. It may produce another transport delivery but never a different durable business effect, event identity, or workflow. Unknown/absence never becomes `dispatch_exhausted` after authorization.
- DLQ publication is explicitly at-least-once. JetStream's 120-second duplicate window is an optimization only; every notification carries the stable outbox `message_id`, the database failure row remains authoritative, and downstream consumers must enforce a durable unique receipt on that ID.
- Recovery state, failures, outbox rows, replay commands, task-retry commands, and deletion intent use plain workspace/task/run UUIDs where parent deletion would otherwise erase intent. Workspace deletion first writes a durable `deleting` gate that bars new prospective handler/business-command claims **and every ordinary task create/start/assign/message/instruction/pause/resume/cancel/approval signal authorization**. Only deletion-owned reconciliation leases over already-committed recovery intent may then be acquired: they renew a live handler or drain the exact existing trigger/ordinary start, replay/retry authorization, or obligatory DLQ outbox identity and cannot create a new request or external identity. An ordinary start authorized before deletion persists its exact workflow ID/input contract and may only be described or re-driven under `REJECT_DUPLICATE`; deletion does not cancel it or infer absence from NotFound. The deletion inventory includes each canonical `task-{task_id}`, immutable ordinary `Task.temporal_workflow_id`, every `AgentRun.temporal_workflow_id/status`, every TaskRetry identity/proof, and every authorized `TriggerInvocation` workflow/name/input/status contract (including one not yet linked to a Task), merges duplicate IDs without losing linkage, and obtains bounded Temporal closure plus terminal-run/proof persistence before product cascade. It never terminalizes, cascades, or removes rows across a live, authorized, or outcome-unknown handler/publish/start. The surviving `deleted` gate prevents stale dispatch until terminal recovery history ages out. A later delivery for an exact workspace whose gate is already `deleted` takes a terminal gate claim and terms the NATS message without inserting processing/failure/outbox/audit rows; term failure simply redelivers to the same gate.
- Agent deletion and snapshot/run creation share the canonical `Workspace -> RecoveryWorkspaceDeletion -> Agent` row-lock prefix. If Agent deletion commits first, an already-authorized immutable start still runs but snapshot admission observes the missing Agent, creates zero `AgentRun` rows, and converges through `closed_before_run`. If snapshot creation locks first and commits a run, Agent deletion must return fixed 409 `Agent has workflow recovery in progress` without audit/delete/cascade until the matching ordinary or manual post-close proof is durable; only then may a retry delete the Agent and cascade that now-redundant run evidence.
- Claim order is workspace row lock, durable deletion gate check, then processing/command claim mutation. Outbox discovery may read bounded candidate IDs without locks, but each candidate must then take `Workspace -> RecoveryWorkspaceDeletion -> OperationsOutbox` locks and revalidate before any claim mutation; it never locks an outbox row first. Handler claims renew by token every 15 seconds while a bounded handler runs; a redelivery after the nominal 60-second lease cannot steal a renewed token. Deletion may bar new claims while allowing an already claimed handler to renew to terminal state.
- Every expired `publishing`/`dispatching` command is reconciled before requester authorization, workspace/deletion state, source/configuration, budget, or safety gates. Current gates apply only to a command with `external_call_authorized_at IS NULL`. Once authorized, accepted evidence finalizes; absence/NotFound and unknown evidence remain reconcilable and may invoke only the immutable same-identity call without another increment. They never become permission for a different identity or a prospective rejection, even after revocation, source expiry, or workspace deletion intent.
- Before either INGRESS or EVENTS handler is called, the validated subject workspace token and complete suffix must equal the parsed envelope: EVENTS is exactly `jhin.v1.{workspace_id}.{event_type}`; INGRESS is exactly `jhin.v1.{workspace_id}.ingress.{source.type}.{data.event}` and that suffix must also equal `event_type`. Every grammar or binding mismatch is terminal `invalid_subject`, replaces the subject with the single fixed `"<invalid-subject>"` sentinel before any persistence/telemetry path, and invokes no handler. The original otherwise-valid foreign workspace/suffix canary appears in no processing/failure/outbox/audit/API/UI/log/span/metric/DLQ sink.
- `Task.last_retry_attempt_number` is the monotonic retry-ID authority and survives `TaskRetry` cleanup; allocation locks the Task, increments it once, and never recomputes from retained history. Before workspace product rows cascade, the post-close reconciler records each accepted retry's immutable actual-close proof on the surviving `TaskRetry` with `terminal_proof_generation == attempt_number`; 90-day retention consumes that proof after Task/AgentRun deletion and never reuses an attempt/workflow ID.
- Replay/retry codes and safe copy live in `jhin-domain`'s cross-language recovery contract. Database-backed admission, workflow discovery/merge, command reconciliation, and deletion-gate contracts live in the neutral installed `jhin-recovery` package. API, event-worker, agent-worker, and web consume those authorities or prove exact JSON parity; no application/service package imports another application/service package.
- Public UI controls land in the same subproject as durable commands, authorization, idempotency, and audit. Members/viewers never fetch admin failure data; replay controls appear only after the detail endpoint has positively verified current source eligibility.
- TDD order is mandatory in every task: write focused failing tests, run and inspect RED, implement the smallest behavior, run GREEN, then run the affected suite. Do not write implementation before its stated RED command has failed for the expected missing behavior.
- Every commit stages only the paths listed in its task. Run `git diff --cached --name-only` and `git diff --cached --check` before every commit. Preserve `orgforge-production-implementation-plan.md` byte-for-byte and never stage it.
- Before Task 0 commits anything, record SHA-256 and byte size for the untracked `orgforge-production-implementation-plan.md` in the repository's private Git directory. Compare both values before every commit and at final handoff; `git diff` is not evidence for an untracked file.

---

## File Map

```text
docs/superpowers/plans/2026-08-18-phase-10-dlq-retry.md
    reviewed execution sequence (this file)

packages/domain/src/jhin_domain/enums.py
packages/domain/src/jhin_domain/recovery.py
packages/domain/src/jhin_domain/recovery_contract.json
packages/domain/src/jhin_domain/__init__.py
packages/domain/tests/test_enums.py
packages/domain/tests/test_recovery.py
    closed persisted lifecycle/reason/safety enums and exports

pyproject.toml
uv.lock
packages/recovery/pyproject.toml
packages/recovery/src/jhin_recovery/__init__.py
packages/recovery/src/jhin_recovery/deletion.py
packages/recovery/src/jhin_recovery/nats.py
packages/recovery/src/jhin_recovery/replay.py
packages/recovery/src/jhin_recovery/task_retry.py
packages/recovery/src/jhin_recovery/trigger_start.py
packages/recovery/tests/test_deletion.py
packages/recovery/tests/test_import_boundaries.py
packages/recovery/tests/test_nats_reconciliation.py
packages/recovery/tests/test_replay.py
packages/recovery/tests/test_task_retry.py
packages/recovery/tests/test_trigger_start.py
apps/api/pyproject.toml
services/event_worker/pyproject.toml
services/agent_worker/pyproject.toml
docker/python.Dockerfile
    installed neutral recovery admission/reconciliation package and import-boundary proof

packages/policy/src/jhin_policy/capabilities.py
packages/policy/tests/test_capabilities.py
packages/tools/src/jhin_tools/builtin.py
packages/tools/src/jhin_tools/organization.py
packages/tools/src/jhin_tools/gateway.py
packages/tools/tests/test_builtin.py
packages/tools/tests/test_gateway.py
packages/tools/tests/test_gateway_concurrency.py
packages/connectors/src/jhin_connectors/example/tools.py
packages/connectors/src/jhin_connectors/github/tools.py
packages/connectors/src/jhin_connectors/linear/tools.py
packages/connectors/src/jhin_connectors/cli/tools.py
packages/connectors/src/jhin_connectors/vercel/tools.py
packages/connectors/src/jhin_connectors/supabase/management_tools.py
packages/connectors/src/jhin_connectors/supabase/database_tools.py
packages/connectors/tests/test_manifest_registry.py
packages/policy/tests/test_evaluator.py
apps/api/tests/test_policy_unit.py
services/agent_worker/tests/test_phase9_invocation_activity.py
tests/integration/test_phase9_authorization.py
    required retry_safety declaration, persisted historical classification, complete catalog proof

packages/db/src/jhin_db/models/recovery.py
packages/db/src/jhin_db/models/work.py
packages/db/src/jhin_db/models/policy.py
packages/db/src/jhin_db/models/trigger.py
packages/db/src/jhin_db/models/__init__.py
packages/db/src/jhin_db/alembic/versions/20260818_0016_dlq_retry.py
packages/db/tests/test_recovery_models.py
packages/db/tests/test_migration_graph.py
tests/integration/test_phase10_dlq_retry_migration.py
    recovery ORM, task-run links/snapshot, durable deletion gate, additive constraints/indexes, real migration paths

apps/api/src/jhin_api/workspaces/service.py
apps/api/src/jhin_api/workspaces/router.py
apps/api/src/jhin_api/workspaces/schemas.py
apps/api/tests/test_workspace_recovery_delete.py
apps/api/src/jhin_api/deps.py
apps/api/src/jhin_api/agents/service.py
apps/api/src/jhin_api/tasks/service.py
apps/api/src/jhin_api/tasks/router.py
apps/api/src/jhin_api/approvals/service.py
apps/api/tests/test_task_workflow_deletion_gate.py
apps/api/tests/test_approvals_unit.py
tests/integration/test_phase10_workspace_deletion.py
    durable deleting/deleted protocol, ordinary create/start/assign/message/signal gate, bounded workflow inventory and drain

packages/events/src/jhin_events/envelope.py
packages/events/src/jhin_events/consumer.py
packages/events/src/jhin_events/publisher.py
packages/events/src/jhin_events/replay.py
packages/events/tests/test_envelope.py
packages/events/tests/test_consumer.py
packages/events/tests/test_publisher.py
packages/events/tests/test_replay.py
packages/events/tests/test_telemetry.py
    replay envelope field, unlimited delivery, explicit subject/message-id publication

services/event_worker/src/jhin_event_worker/matcher.py
services/event_worker/tests/test_matcher.py
tests/integration/test_phase10_processing_claim.py
    replay-root semantic identity, race-safe first claim, reject-duplicate trigger start reconciliation

services/event_worker/src/jhin_event_worker/delivery.py
services/event_worker/src/jhin_event_worker/failures.py
services/event_worker/src/jhin_event_worker/quarantine.py
services/event_worker/src/jhin_event_worker/commands.py
services/event_worker/src/jhin_event_worker/retention.py
services/event_worker/src/jhin_event_worker/processor.py
services/event_worker/src/jhin_event_worker/normalizer.py
services/event_worker/src/jhin_event_worker/main.py
services/event_worker/src/jhin_event_worker/settings.py
services/event_worker/tests/conftest.py
services/event_worker/tests/test_delivery.py
services/event_worker/tests/test_quarantine.py
services/event_worker/tests/test_commands.py
services/event_worker/tests/test_retention.py
services/event_worker/tests/test_normalizer.py
services/event_worker/tests/test_telemetry.py
apps/api/tests/test_webhooks_unit.py
    handler attempt/lease state machine, atomic quarantine, outbox/replay/retry/deletion reconcilers, bounded retention

apps/api/src/jhin_api/idempotency.py
apps/api/src/jhin_api/operations/__init__.py
apps/api/src/jhin_api/operations/router.py
apps/api/src/jhin_api/operations/schemas.py
apps/api/src/jhin_api/operations/service.py
apps/api/src/jhin_api/tasks/retry.py
apps/api/src/jhin_api/tasks/schemas.py
apps/api/src/jhin_api/main.py
apps/api/tests/test_idempotency.py
apps/api/tests/test_event_failures.py
apps/api/tests/test_task_retry.py
apps/api/tests/conftest.py
tests/integration/test_phase10_event_replay.py
    admin failure/replay/resolve/history APIs, member task-retry API, current eligibility and audit

apps/api/src/jhin_api/health/schemas.py
apps/api/src/jhin_api/health/service.py
apps/api/tests/test_operations_health.py
    bounded workspace open-DLQ count/oldest age in protected health

packages/workflows/src/jhin_workflows/agent_task/shared.py
packages/workflows/tests/test_agent_task_tool_routing.py
services/agent_worker/src/jhin_agent_worker/activities.py
services/agent_worker/src/jhin_agent_worker/projections.py
services/agent_worker/tests/test_task_retry_admission.py
tests/integration/test_phase10_task_retry.py
    retry/ordinary immutable start input, fresh snapshot persistence, one run linked to one retry, crash reattachment

apps/web/lib/types.ts
apps/web/Dockerfile
apps/web/lib/hooks.ts
apps/web/lib/recovery-contract.ts
apps/web/app/(app)/operations/page.tsx
apps/web/app/(app)/tasks/[id]/page.tsx
apps/web/components/event-failure-panel.tsx
apps/web/components/task-retry-card.tsx
apps/web/tests/event-failure-panel.test.tsx
apps/web/tests/operations-page.test.tsx
apps/web/tests/task-retry-card.test.tsx
apps/web/tests/task-detail-retry.test.tsx
apps/web/tests/recovery-contract-parity.test.ts
apps/web/tests/docker-build-context.test.ts
    allowlisted admin DLQ/history UI and safe member retry UI

tests/integration/conftest.py
tests/integration/test_phase10_dlq_retry.py
tests/integration/test_phase10_retry_recovery.py
tests/test_phase10_dlq_retry_harness.py
scripts/run_phase10_dlq_retry.py
compose.phase10-dlq-test.yaml
Makefile
.github/workflows/ci.yml
docs/operations/dlq-and-task-retry.md
README.md
    real PostgreSQL/NATS/Temporal plus fake-provider recovery, source-expiry/retention, operator contract
```

## Shared Interfaces

All tasks use these exact names and values. A task may consume an interface only after the task that produces it. Ellipsis bodies below are type-signature notation for the named concrete implementations required in the owning task; every callable's behavior, bounds, errors, and production wiring are specified in that task and no runtime stub may contain an ellipsis or `NotImplementedError`.

```python
# packages/domain/src/jhin_domain/enums.py
class RetrySafety(StrEnum):
    PURE = "pure"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"

class EventOriginStream(StrEnum):
    INGRESS = "INGRESS"
    EVENTS = "EVENTS"

class EventProcessingMode(StrEnum):
    HANDLING = "handling"
    QUARANTINE_ONLY = "quarantine_only"
    COMPLETED = "completed"

class EventFailureReason(StrEnum):
    INVALID_ENVELOPE = "invalid_envelope"
    INVALID_SUBJECT = "invalid_subject"
    UNSUPPORTED_INGRESS_EVENT = "unsupported_ingress_event"
    HANDLER_EXCEPTION = "handler_exception"
    PROCESSING_INVARIANT = "processing_invariant"
    WORKSPACE_DELETED = "workspace_deleted"

class EventFailureStatus(StrEnum):
    OPEN = "open"
    REPLAY_REQUESTED = "replay_requested"
    REPLAYED = "replayed"
    RESOLVED = "resolved"
    EXPIRED = "expired"

class EventReplayStatus(StrEnum):
    REQUESTED = "requested"
    DISPATCHING = "dispatching"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    FAILED = "failed"

class TaskRetryStatus(StrEnum):
    REQUESTED = "requested"
    DISPATCHING = "dispatching"
    STARTED = "started"
    REJECTED = "rejected"
    FAILED = "failed"

class OperationsOutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"

class RecoveryWorkspaceDeletionStatus(StrEnum):
    DELETING = "deleting"
    DELETED = "deleted"
    BLOCKED = "blocked"

EVENT_REPLAY_NONTERMINAL = frozenset({EventReplayStatus.REQUESTED, EventReplayStatus.DISPATCHING})
TASK_RETRY_NONTERMINAL = frozenset({TaskRetryStatus.REQUESTED, TaskRetryStatus.DISPATCHING})

# packages/policy/src/jhin_policy/capabilities.py
class ToolDefinition(BaseModel):
    # existing fields remain exact
    retry_safety: RetrySafety  # required; no default

# packages/events/src/jhin_events/envelope.py
class EventEnvelope(BaseModel):
    # existing fields remain exact
    replay_of_event_id: UUID | None = None

def replay_root_event_id(envelope: EventEnvelope) -> UUID:
    return envelope.replay_of_event_id or envelope.event_id

# packages/events/src/jhin_events/publisher.py
class EventPublisher:
    async def publish(
        self, envelope: EventEnvelope, *, headers: Mapping[str, str] | None = None,
    ) -> PubAck: ...
    async def publish_to_subject(
        self,
        *,
        subject: str,
        envelope: EventEnvelope,
        message_id: str,
        headers: Mapping[str, str] | None = None,
    ) -> PubAck: ...

# packages/events/src/jhin_events/replay.py
INVALID_SUBJECT_SENTINEL = "<invalid-subject>"

def validate_subject_envelope_binding(
    *, origin_stream: EventOriginStream, subject: str, envelope: EventEnvelope,
) -> None: ...

# services/event_worker/src/jhin_event_worker/failures.py
SafeEventErrorClass = Literal[
    "validation", "unsupported", "dependency_unavailable",
    "temporal_unavailable", "database_unavailable", "internal",
]

@dataclass(frozen=True)
class ClassifiedEventFailure:
    reason_code: EventFailureReason
    safe_error_class: SafeEventErrorClass
    safe_error_detail: str
    replayable: bool
    terminal_delivery: bool

def classify_handler_error(error: BaseException) -> ClassifiedEventFailure: ...
def invalid_envelope_failure(error_count: int) -> ClassifiedEventFailure: ...
def unsupported_ingress_failure() -> ClassifiedEventFailure: ...
def failure_for_reason(reason: EventFailureReason) -> ClassifiedEventFailure: ...

# packages/events/src/jhin_events/replay.py
@dataclass(frozen=True)
class ReplaySemanticIdentity:
    handler_name: Literal["ingress-normalizer", "connector-trigger-matcher"]
    root_event_id: UUID
    semantic_key: str

def replay_semantic_identity(
    origin_stream: EventOriginStream, envelope: EventEnvelope,
) -> ReplaySemanticIdentity | None: ...

# packages/recovery/src/jhin_recovery/trigger_start.py
TriggerWorkflowName = Literal["TriggeredTaskWorkflow", "EngineeringTicketWorkflow"]
TriggerWorkflowInput = TriggeredTaskInput | EngineeringTicketInput
TRIGGER_INPUT_MAX_BYTES = 20_000
TRIGGER_TITLE_MAX_CHARS = 500
TRIGGER_DESCRIPTION_MAX_CHARS = 10_000
TRIGGER_URL_MAX_CHARS = 2_000
TRIGGER_EXTERNAL_ID_MAX_CHARS = 500
TRIGGER_MAX_RETEST_CYCLES = 20

@dataclass(frozen=True)
class AuthorizedTriggerStart:
    invocation_id: UUID
    workspace_id: UUID
    workflow_id: str
    workflow_name: TriggerWorkflowName
    workflow_input: TriggerWorkflowInput
    contract_version: Literal[1]
    authorized_at: datetime

def serialize_trigger_workflow_input(
    workflow_name: TriggerWorkflowName, workflow_input: TriggerWorkflowInput,
) -> JsonDict: ...

def deserialize_trigger_workflow_input(
    workflow_name: TriggerWorkflowName, payload: JsonDict,
) -> TriggerWorkflowInput: ...

@dataclass(frozen=True)
class TriggerStartEvidence:
    outcome: Literal["accepted", "not_observed", "unknown", "invariant"]
    close_status: str | None

async def authorize_trigger_start(
    session: AsyncSession, *, invocation_id: UUID, workspace_id: UUID,
    workflow_id: str, workflow_name: TriggerWorkflowName,
    workflow_input: TriggerWorkflowInput, now: datetime,
) -> AuthorizedTriggerStart: ...

async def load_authorized_trigger_start(
    session: AsyncSession, *, invocation_id: UUID,
) -> AuthorizedTriggerStart: ...

async def ensure_trigger_workflow(
    temporal: TemporalClient, *, start: AuthorizedTriggerStart,
    timeout_seconds: float = 5.0,
) -> TriggerStartEvidence: ...

async def reconcile_authorized_trigger_start(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    *, invocation_id: UUID, timeout_seconds: float = 5.0,
) -> TriggerStartEvidence: ...

async def reconcile_due_trigger_starts_batch(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    *, now: datetime | None = None, limit: int = 25,
) -> int: ...

async def run_trigger_start_reconciler(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    stop: asyncio.Event, *, poll_seconds: float = 1.0,
) -> None: ...

# services/event_worker/src/jhin_event_worker/delivery.py
PROCESSING_MAX_ATTEMPTS = 5
PROCESSING_LEASE_SECONDS = 60
PROCESSING_LEASE_RENEW_SECONDS = 15
PROCESSING_HANDLER_TIMEOUT_SECONDS = 300
PROCESSING_NAK_SECONDS = (2, 4, 8, 16, 30)

@dataclass(frozen=True)
class SourceMessageMetadata:
    origin_stream: EventOriginStream
    consumer_name: str
    subject: str
    source_stream_sequence: int
    source_consumer_sequence: int | None
    delivery_count: int

    @classmethod
    def from_msg(
        cls, origin_stream: EventOriginStream, consumer_name: str, message: Msg,
    ) -> "SourceMessageMetadata": ...

    def with_invalid_subject_sentinel(self) -> "SourceMessageMetadata": ...

@dataclass(frozen=True)
class DeliveryIdentity:
    workspace_id: UUID | None
    event_id: UUID | None
    correlation_id: UUID | None

class BusinessHandler(Protocol):
    async def handle_event(self, envelope: EventEnvelope) -> None: ...

class QuarantineWriter(Protocol):
    async def __call__(
        self,
        source: SourceMessageMetadata,
        identity: DeliveryIdentity,
        failure: ClassifiedEventFailure,
    ) -> "QuarantineResult": ...

async def defer_quarantine(
    source: SourceMessageMetadata,
    identity: DeliveryIdentity,
    failure: ClassifiedEventFailure,
) -> "QuarantineResult": ...

class ClaimKind(StrEnum):
    HANDLE = "handle"
    LEASE_BUSY = "lease_busy"
    QUARANTINE = "quarantine"
    COMPLETED_SUCCESS = "completed_success"
    COMPLETED_QUARANTINE = "completed_quarantine"
    DELETED_WORKSPACE = "deleted_workspace"

@dataclass(frozen=True)
class ProcessingClaim:
    kind: ClaimKind
    attempt_number: int
    claim_token: UUID | None
    last_reason_code: EventFailureReason | None

async def claim_processing_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    source: SourceMessageMetadata,
    identity: DeliveryIdentity,
    *,
    terminal_failure: ClassifiedEventFailure | None = None,
    now: datetime | None = None,
) -> ProcessingClaim: ...

async def renew_processing_claim(
    session_factory: async_sessionmaker[AsyncSession],
    source: SourceMessageMetadata,
    *, claim_token: UUID, now: datetime | None = None,
) -> bool: ...

class ProcessingLeaseKeeper:
    async def __aenter__(self) -> "ProcessingLeaseKeeper": ...
    async def assert_owned(self) -> None: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> None: ...

async def mark_handler_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    source: SourceMessageMetadata,
    *, claim_token: UUID, now: datetime | None = None,
) -> None: ...

async def mark_handler_failed(
    session_factory: async_sessionmaker[AsyncSession],
    source: SourceMessageMetadata,
    *, claim_token: UUID, failure: ClassifiedEventFailure, now: datetime | None = None,
) -> EventProcessingMode: ...

class DurableEventConsumer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine,
        *, origin_stream: EventOriginStream, consumer_name: str,
        business_handler: BusinessHandler, quarantine_writer: QuarantineWriter,
    ) -> None: ...
    async def handle(self, message: Msg) -> None: ...

# services/event_worker/src/jhin_event_worker/quarantine.py
MAX_FAILURE_DETAIL_CHARS = 2_000
MAX_FAILURE_SUBJECT_CHARS = 500
MAX_DLQ_PAYLOAD_BYTES = 4_096

@dataclass(frozen=True)
class QuarantineResult:
    failure_id: UUID
    outbox_id: UUID

async def commit_quarantine(
    session_factory: async_sessionmaker[AsyncSession],
    source: SourceMessageMetadata,
    identity: DeliveryIdentity,
    failure: ClassifiedEventFailure,
    *, now: datetime | None = None,
) -> QuarantineResult: ...

# packages/domain/src/jhin_domain/recovery.py, loaded from recovery_contract.json
COMMAND_CLAIM_SECONDS = 30
COMMAND_BATCH_SIZE = 25
COMMAND_MAX_ATTEMPTS = 20
COMMAND_BACKOFF_SECONDS = (1, 2, 4, 8, 15, 30, 60, 120, 300, 600)
TASK_RETRY_MAX_GENERATION = 2_147_483_647

ReplayDispatchCode = Literal[
    "source_event_expired", "source_event_changed", "invalid_source_event",
    "requester_unauthorized", "workspace_inactive", "reason_not_replayable",
    "nats_unavailable", "database_unavailable", "dispatch_exhausted",
    "invariant_violation",
]
ReplayEligibilityReasonCode = Literal[
    "eligible", "failure_not_open", "reason_not_replayable",
    "source_event_expired", "source_event_changed", "source_check_unavailable",
    "replay_in_progress", "idempotency_key_conflict",
]
TaskRetryReasonCode = Literal[
    "eligible", "task_not_failed", "workflow_active", "retry_in_progress",
    "idempotency_key_conflict",
    "agent_missing", "agent_inactive", "budget_exhausted", "authorization_unchanged",
    "policy_unchanged", "configuration_unchanged", "max_steps_unchanged",
    "explicit_rejection_unchanged", "committed_external_effect",
    "ambiguous_external_effect", "unknown_tool_safety", "concurrency_wait",
    "requester_unauthorized", "workspace_inactive", "temporal_unavailable",
    "dispatch_exhausted", "invariant_violation",
]

REPLAY_SAFE_COPY: Final[dict[ReplayDispatchCode, str]] = {
    "source_event_expired": "The retained source event is no longer available.",
    "source_event_changed": "The retained source event no longer matches this failure.",
    "invalid_source_event": "The retained source event is invalid.",
    "requester_unauthorized": "The requester is no longer authorized to replay this event.",
    "workspace_inactive": "The workspace is not active.",
    "reason_not_replayable": "This failure must be resolved without replay.",
    "nats_unavailable": "Event storage is temporarily unavailable.",
    "database_unavailable": "Recovery storage is temporarily unavailable.",
    "dispatch_exhausted": "Replay dispatch could not be completed.",
    "invariant_violation": "Replay is unavailable because recovery state is inconsistent.",
}

REPLAY_ELIGIBILITY_SAFE_COPY: Final[dict[ReplayEligibilityReasonCode, str]] = {
    "eligible": "The retained source event is available for replay.",
    "failure_not_open": "Only an open failure can be replayed.",
    "reason_not_replayable": "This failure must be resolved without replay.",
    "source_event_expired": "The retained source event is no longer available.",
    "source_event_changed": "The retained source event no longer matches this failure.",
    "source_check_unavailable": "Event storage is temporarily unavailable.",
    "replay_in_progress": "A replay is already in progress.",
    "idempotency_key_conflict": "Idempotency key is already bound to another operation.",
}

TASK_RETRY_SAFE_COPY: Final[dict[TaskRetryReasonCode, str]] = {
    "eligible": "This failed task can be retried with current configuration.",
    "task_not_failed": "Only a failed task can be retried.",
    "workflow_active": "A workflow attempt is still active.",
    "retry_in_progress": "A manual retry is already in progress.",
    "idempotency_key_conflict": "Idempotency key is already bound to another operation.",
    "agent_missing": "The assigned agent no longer exists.",
    "agent_inactive": "The assigned agent is not active.",
    "budget_exhausted": "The current agent budget does not admit another attempt.",
    "authorization_unchanged": "Authorization must change before this task can be retried.",
    "policy_unchanged": "Policy must change before this task can be retried.",
    "configuration_unchanged": "Configuration must be corrected before this task can be retried.",
    "max_steps_unchanged": "The agent step limit must be raised before this task can be retried.",
    "explicit_rejection_unchanged": "The rejecting policy or grant must change before retry.",
    "committed_external_effect": "A prior external effect was committed; create a new explicit task.",
    "ambiguous_external_effect": "A prior external effect is ambiguous; reconcile it and create a new explicit task.",
    "unknown_tool_safety": "A prior tool call has no reviewed retry-safety classification.",
    "concurrency_wait": "The retry is queued until a concurrency slot is available.",
    "requester_unauthorized": "The requester is no longer authorized to retry this task.",
    "workspace_inactive": "The workspace is not active.",
    "temporal_unavailable": "Workflow history is temporarily unavailable.",
    "dispatch_exhausted": "The retry workflow could not be started.",
    "invariant_violation": "Retry is unavailable because task state is inconsistent.",
}

# services/event_worker/src/jhin_event_worker/commands.py
OUTBOX_CANDIDATE_SCAN_LIMIT = 100

class CommandClaimMode(StrEnum):
    NEW_ATTEMPT = "new_attempt"
    RECONCILE_EXISTING = "reconcile_existing"
    RECONCILE_AT_CAP = "reconcile_at_cap"

@dataclass(frozen=True)
class CommandClaim:
    command_id: UUID
    claim_token: UUID
    attempt_count: int
    mode: CommandClaimMode
    external_call_authorized_at: datetime | None

@dataclass(frozen=True)
class AuthorizedExternalCall:
    command_id: UUID
    identity: str                 # validated ASCII, 1..200
    attempt_count: int            # 1..20, immutable across same-ID re-drives
    authorized_at: datetime
    baseline_sequence: int | None

async def claim_due_outbox(
    session: AsyncSession, *, now: datetime,
    deletion_drain_workspace_id: UUID | None = None, limit: int = 25,
) -> tuple[CommandClaim, ...]: ...

async def authorize_external_call(
    session: AsyncSession, *, command_id: UUID, claim_token: UUID,
    identity: str, baseline_sequence: int | None, now: datetime,
) -> AuthorizedExternalCall: ...

PreauthorizationFailureCode = Literal[
    "nats_unavailable", "temporal_unavailable", "database_unavailable",
]

async def record_preauthorization_failure(
    session: AsyncSession, *, command_id: UUID, claim_token: UUID,
    safe_error_code: PreauthorizationFailureCode, now: datetime,
) -> int: ...

class SourceEventReader:
    def __init__(self, js: JetStreamContext) -> None: ...
    async def get_exact(self, *, stream: EventOriginStream, sequence: int) -> RawStreamMsg: ...

@dataclass(frozen=True)
class RawStreamMsg:
    stream: str
    sequence: int
    subject: str
    data: bytes
    headers: Mapping[str, str]

async def publish_operations_message(
    js: JetStreamContext,
    *, subject: str, payload: bytes, message_id: str,
) -> PubAck: ...

# Implementation contract: publish_operations_message delegates to
# jhin_events.telemetry.publish_jetstream(..., stream="DLQ",
# message_id=message_id). It must not call js.publish directly.

class OperationsCommandDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        js: JetStreamContext,
        temporal: TemporalClient,
    ) -> None: ...
    async def reconcile_authorized_trigger_starts_batch(
        self, *, now: datetime | None = None,
    ) -> int: ...
    async def reconcile_authorized_ordinary_starts_batch(
        self, *, now: datetime | None = None,
    ) -> int: ...
    async def reconcile_ordinary_task_start_terminals_batch(
        self, *, now: datetime | None = None,
    ) -> int: ...
    async def dispatch_outbox_batch(self, *, now: datetime | None = None) -> int: ...
    async def dispatch_replay_batch(self, *, now: datetime | None = None) -> int: ...
    async def dispatch_task_retry_batch(self, *, now: datetime | None = None) -> int: ...
    async def reconcile_task_retry_terminals_batch(
        self, *, now: datetime | None = None,
    ) -> int: ...
    async def reconcile_workspace_deletions_batch(
        self, *, now: datetime | None = None,
    ) -> int: ...
    async def run(self, stop: asyncio.Event) -> None: ...

# apps/api/src/jhin_api/idempotency.py
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
def require_idempotency_key(request: Request) -> str: ...

# apps/api/src/jhin_api/deps.py
def require_operational_workspace_role(
    required: WorkspaceRole,
) -> Callable[..., Coroutine[Any, Any, WorkspaceContext]]: ...

OperationalMemberCtx = Annotated[
    WorkspaceContext, Depends(require_operational_workspace_role(WorkspaceRole.MEMBER))
]

# packages/recovery/src/jhin_recovery/task_retry.py
@dataclass(frozen=True)
class TaskRetryEligibility:
    eligible: bool
    reason_code: TaskRetryReasonCode
    source_run_id: UUID | None

MAX_TASK_WORKFLOW_IDS = 100
TASK_RETRY_TERMINAL_HISTORY_MAX_EVENTS = 10_000
ORDINARY_START_RECONCILE_BATCH_SIZE = 25
ORDINARY_START_RECONCILE_CLAIM_SECONDS = 30
ORDINARY_START_RECONCILE_BACKOFF_SECONDS = (1, 2, 4, 8, 15, 30, 60, 120, 300, 600)
TASK_START_TERMINAL_RECONCILE_BATCH_SIZE = 25
TASK_START_TERMINAL_RECONCILE_CLAIM_SECONDS = 30
TASK_START_TERMINAL_RECONCILE_BACKOFF_SECONDS = (2, 4, 8, 15, 30, 60, 120, 300, 600)

class TaskRetryEvaluationMode(StrEnum):
    API_REQUEST = "api_request"
    DISPATCH = "dispatch"

@dataclass(frozen=True)
class ManualRetryDispatchBinding:
    kind: Literal["manual_task_retry"]
    retry_id: UUID
    attempt_number: int

def parse_manual_retry_dispatch_binding(
    metadata: Mapping[str, object],
) -> ManualRetryDispatchBinding | None: ...

TaskWorkflowTargetKind = Literal[
    "ordinary", "manual_retry", "not_started", "invariant",
]

@dataclass(frozen=True)
class TaskWorkflowTarget:
    kind: TaskWorkflowTargetKind
    workflow_id: str | None
    retry_id: UUID | None
    run_id: UUID | None

async def resolve_active_task_workflow_target(
    session: AsyncSession, *, workspace_id: UUID, task_id: UUID,
) -> TaskWorkflowTarget: ...

@dataclass(frozen=True)
class KnownTaskWorkflow:
    workflow_id: str
    is_base: bool
    is_task_binding: bool
    task_start_authorized: bool
    agent_run_id: UUID | None
    task_retry_id: UUID | None

async def collect_known_task_workflows(
    session: AsyncSession, *, workspace_id: UUID, task_id: UUID,
) -> tuple[KnownTaskWorkflow, ...]: ...

async def evaluate_task_retry_request(
    session: AsyncSession,
    temporal: TemporalClient,
    *, workspace_id: UUID, task_id: UUID, now: datetime | None = None,
) -> TaskRetryEligibility: ...

async def evaluate_task_retry_dispatch(
    session: AsyncSession,
    temporal: TemporalClient,
    *, retry_id: UUID, claim_token: UUID, now: datetime | None = None,
) -> TaskRetryEligibility: ...

def classify_task_retry_effects(tool_calls: Sequence[ToolCall]) -> TaskRetryReasonCode | None: ...

async def reconcile_task_retry_started(
    session: AsyncSession,
    *, retry_id: UUID, workflow_id: str, now: datetime,
) -> TaskRetry: ...

@dataclass(frozen=True)
class TaskRetryTerminalProof:
    retry_id: UUID
    attempt_number: int
    workflow_id: str
    workflow_status: Literal["completed", "failed", "cancelled", "terminated", "timed_out"]
    run_outcome: Literal["terminal_run", "closed_before_run"]
    run_id: UUID | None
    run_status: Literal["completed", "failed", "cancelled"] | None
    proven_at: datetime

async def record_task_retry_terminal_proof(
    session: AsyncSession, *, retry_id: UUID, reconcile_claim_token: UUID,
    attempt_number: int,
    workflow_id: str, workflow_status: str, run_outcome: str,
    run_id: UUID | None, run_status: str | None, now: datetime,
) -> TaskRetryTerminalProof: ...

@dataclass(frozen=True)
class TaskRetryTerminalReconcileResult:
    retry_id: UUID
    outcome: Literal["open", "terminal_proven", "unknown", "invariant"]
    workflow_status: Literal[
        "completed", "failed", "cancelled", "terminated", "timed_out",
    ] | None
    run_outcome: Literal["terminal_run", "closed_before_run"] | None

async def reconcile_task_retry_terminal(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    *, retry_id: UUID, timeout_seconds: float = 5.0,
) -> TaskRetryTerminalReconcileResult: ...

async def reconcile_due_task_retry_terminals_batch(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    *, now: datetime | None = None,
    limit: int = TASK_START_TERMINAL_RECONCILE_BATCH_SIZE,
) -> int: ...

@dataclass(frozen=True)
class AuthorizedTaskRetryStart:
    retry_id: UUID
    task_id: UUID
    workflow_id: str
    workflow_input: AgentTaskInput
    contract_version: Literal[1]
    attempt_count: int
    authorized_at: datetime

async def authorize_task_retry_start(
    session: AsyncSession, *, retry_id: UUID, claim_token: UUID,
    workflow_input: AgentTaskInput, now: datetime,
) -> AuthorizedTaskRetryStart: ...

async def load_authorized_task_retry_start(
    session: AsyncSession, *, retry_id: UUID,
) -> AuthorizedTaskRetryStart: ...

# apps/api/src/jhin_api/tasks/retry.py
async def request_task_retry(
    session: AsyncSession,
    temporal: TemporalClient,
    ctx: WorkspaceContext,
    *, task_id: UUID, idempotency_key: str, request_id: UUID, ip_hash: str,
) -> TaskRetry: ...

# packages/recovery/src/jhin_recovery/deletion.py
WORKSPACE_DELETE_DUE_BATCH_SIZE = 25

@dataclass(frozen=True)
class WorkspaceClaimGate:
    workspace_id: UUID
    workspace_exists: bool
    workspace_active: bool
    deletion_status: RecoveryWorkspaceDeletionStatus | None

async def lock_workspace_claim_gate(
    session: AsyncSession, *, workspace_id: UUID,
) -> WorkspaceClaimGate: ...

@dataclass(frozen=True)
class AgentDeletionRecoveryEvidence:
    outcome: Literal[
        "safe", "active_run", "ordinary_proof_pending",
        "task_retry_proof_pending", "invariant",
    ]

async def evaluate_agent_deletion_recovery(
    session: AsyncSession, *, workspace_id: UUID, agent_id: UUID,
) -> AgentDeletionRecoveryEvidence: ...

async def request_workspace_deletion(
    session: AsyncSession, *, workspace_id: UUID, requested_by_user_id: UUID,
    request_id: UUID, ip_hash: str, now: datetime,
) -> RecoveryWorkspaceDeletion: ...

MAX_ORDINARY_SOURCE_ROWS_PER_PAGE = 500
MAX_ORDINARY_WORKFLOW_IDS_PER_PAGE = 2_500

@dataclass(frozen=True)
class OrdinaryRunBinding:
    run_id: UUID
    task_id: UUID | None
    status: RunStatus

@dataclass(frozen=True)
class OrdinaryTriggerBinding:
    invocation_id: UUID
    task_id: UUID | None
    status: TriggerInvocationStatus
    workflow_name: TriggerWorkflowName | None
    start_contract_version: int | None
    start_authorized: bool
    start_accepted: bool
    terminal_proven: bool

@dataclass(frozen=True)
class DelegatedWrapperBinding:
    child_task_id: UUID
    parent_task_id: UUID
    parent_run_id: UUID | None

@dataclass(frozen=True)
class KnownOrdinaryWorkflow:
    workflow_id: str
    task_ids: tuple[UUID, ...]
    canonical_task_ids: tuple[UUID, ...]
    task_binding_ids: tuple[UUID, ...]
    delegated_wrappers: tuple[DelegatedWrapperBinding, ...]
    agent_runs: tuple[OrdinaryRunBinding, ...]
    trigger_invocations: tuple[OrdinaryTriggerBinding, ...]
    start_authorized: bool

@dataclass(frozen=True)
class OrdinaryWorkflowEvidence:
    workflow_id: str
    outcome: Literal["open", "closed_terminal", "not_observed", "unknown", "invariant"]
    close_status: str | None

@dataclass(frozen=True)
class OrdinaryWorkflowInventoryPage:
    bindings: tuple[KnownOrdinaryWorkflow, ...]
    next_task_id: UUID | None
    next_agent_run_id: UUID | None
    next_trigger_invocation_id: UUID | None
    tasks_complete: bool
    agent_runs_complete: bool
    trigger_invocations_complete: bool

@dataclass(frozen=True)
class AuthorizedTaskStart:
    task_id: UUID
    workspace_id: UUID
    workflow_id: str              # ASCII, 1..200
    workflow_input: AgentTaskInput
    contract_version: Literal[1]
    authorized_at: datetime
    accepted_at: datetime | None

AGENT_TASK_START_CONTRACT_VERSION = 1
AGENT_TASK_INSTRUCTION_MAX_CHARS = 20_000
AGENT_TASK_INPUT_MAX_BYTES = 82_000
ORDINARY_START_TERMINAL_HISTORY_MAX_EVENTS = 10_000

def serialize_agent_task_input(workflow_input: AgentTaskInput) -> JsonDict: ...
def deserialize_agent_task_input(payload: JsonDict) -> AgentTaskInput: ...

async def authorize_ordinary_task_start(
    session: AsyncSession, *, workspace_id: UUID, task_id: UUID,
    agent_id: UUID, instruction: str, now: datetime,
) -> AuthorizedTaskStart: ...

async def load_authorized_ordinary_task_start(
    session: AsyncSession, *, task_id: UUID,
) -> AuthorizedTaskStart: ...

@dataclass(frozen=True)
class OrdinaryTaskStartTerminalProof:
    task_id: UUID
    workflow_id: str
    workflow_status: Literal["completed", "failed", "cancelled", "terminated", "timed_out"]
    run_outcome: Literal["terminal_run", "closed_before_run"]
    run_id: UUID | None
    run_status: Literal["completed", "failed", "cancelled"] | None
    proven_at: datetime

async def record_ordinary_task_start_terminal_proof(
    session: AsyncSession, *, task_id: UUID, reconcile_claim_token: UUID,
    workflow_id: str,
    workflow_status: str, run_outcome: str, run_id: UUID | None,
    run_status: str | None, now: datetime,
) -> OrdinaryTaskStartTerminalProof: ...

@dataclass(frozen=True)
class OrdinaryTaskStartTerminalReconcileResult:
    task_id: UUID
    outcome: Literal["open", "terminal_proven", "unknown", "invariant"]
    workflow_status: Literal[
        "completed", "failed", "cancelled", "terminated", "timed_out",
    ] | None
    run_outcome: Literal["terminal_run", "closed_before_run"] | None

async def reconcile_ordinary_task_start_terminal(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    *, task_id: UUID, timeout_seconds: float = 5.0,
) -> OrdinaryTaskStartTerminalReconcileResult: ...

async def reconcile_due_ordinary_task_start_terminals_batch(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    *, now: datetime | None = None,
    limit: int = TASK_START_TERMINAL_RECONCILE_BATCH_SIZE,
) -> int: ...

async def collect_workspace_ordinary_workflows(
    session: AsyncSession, *, workspace_id: UUID,
    after_task_id: UUID | None, after_agent_run_id: UUID | None,
    after_trigger_invocation_id: UUID | None,
    limit: int = MAX_ORDINARY_SOURCE_ROWS_PER_PAGE,
) -> OrdinaryWorkflowInventoryPage: ...

async def reconcile_authorized_ordinary_start(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    *, task_id: UUID, timeout_seconds: float = 5.0,
) -> OrdinaryWorkflowEvidence: ...

async def reconcile_due_authorized_ordinary_starts_batch(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    *, now: datetime | None = None,
    limit: int = ORDINARY_START_RECONCILE_BATCH_SIZE,
) -> int: ...

async def prove_ordinary_workflow_terminal(
    session: AsyncSession, temporal: TemporalClient,
    *, binding: KnownOrdinaryWorkflow, timeout_seconds: float = 5.0,
) -> OrdinaryWorkflowEvidence: ...

async def reconcile_workspace_deletion(
    session_factory: async_sessionmaker[AsyncSession],
    js: JetStreamContext,
    temporal: TemporalClient,
    *, workspace_id: UUID, now: datetime | None = None,
) -> RecoveryWorkspaceDeletionStatus: ...

async def reconcile_due_workspace_deletions_batch(
    session_factory: async_sessionmaker[AsyncSession],
    js: JetStreamContext,
    temporal: TemporalClient,
    *, now: datetime | None = None,
    limit: int = WORKSPACE_DELETE_DUE_BATCH_SIZE,
) -> int: ...

# packages/recovery/src/jhin_recovery/nats.py
NATS_RECONCILE_MAX_MESSAGES = 10_000

@dataclass(frozen=True)
class PublishEvidence:
    outcome: Literal["accepted", "not_observed", "unknown"]
    stream_sequence: int | None

async def capture_publish_baseline(
    js: JetStreamContext, *, stream: str,
) -> int: ...  # last sequence, inclusive lower bound; 0 is valid for an empty stream

async def reconcile_publish_after_baseline(
    js: JetStreamContext, *, stream: str, subject: str, message_id: str,
    baseline_sequence: int,
) -> PublishEvidence: ...

async def reconcile_or_redrive_authorized_publish(
    js: JetStreamContext, *, stream: str, subject: str, payload: bytes,
    message_id: str, baseline_sequence: int, timeout_seconds: float = 5.0,
) -> PublishEvidence: ...

# packages/workflows/src/jhin_workflows/agent_task/shared.py
@dataclass
class AgentTaskInput:
    workspace_id: str
    task_id: str
    agent_id: str
    instruction: str = ""
    retry_id: str | None = None
    attempt_number: int = 1
```

The complete retry-safety registry is fixed as follows. The catalog test must compare exact sets so a newly registered tool cannot silently inherit a classification.

```python
PURE_TOOLS = frozenset({
    "system.echo", "system.time", "system.demo.elevated", "example.ping",
    "github.repository.read", "github.branch.list", "github.file.read",
    "github.issue.read", "github.pull_request.read", "github.check.read",
    "github.workflow_run.read", "linear.issue.read", "linear.issue.search",
    "linear.metadata.read", "cli.file.read", "vercel.project.list",
    "vercel.project.read", "vercel.deployment.list", "vercel.deployment.read",
    "vercel.deployment.logs.read", "vercel.environment_metadata.read",
    "supabase.project.read", "supabase.logs.read", "supabase.function.list",
    "supabase.database.read",
})
IDEMPOTENT_TOOLS = frozenset({
    "system.note.append", "system.demo.destructive",
    "organization.delegate_task", "organization.report_result", "cli.file.write",
})
NON_IDEMPOTENT_TOOLS = frozenset({
    "github.branch.create", "github.issue.comment", "github.pull_request.create",
    "github.pull_request.comment", "github.pull_request.merge",
    "github.workflow.dispatch", "linear.issue.create", "linear.issue.update",
    "linear.comment.create", "cli.command.execute", "cli.repository.checkout",
    "cli.test.run", "vercel.deployment.preview.create",
    "vercel.deployment.redeploy", "vercel.deployment.promote",
    "vercel.deployment.alias.assign", "supabase.function.deploy",
    "supabase.function.delete", "supabase.database.write",
    "supabase.database.destructive",
})
```

`pure` means no durable/external mutation. `idempotent` here is limited to effects committed atomically with the gateway claim or deterministic same-content workspace writes. Every connector/provider mutation remains `non_idempotent` until a focused provider-idempotency contract and crash test justify changing that declaration.

---

### Task 0: Check In the Reviewed DLQ/Retry Execution Baseline

**Files:**
- Create: `docs/superpowers/plans/2026-08-18-phase-10-dlq-retry.md`

**Interfaces:**
- Consumes: corrected Phase 10 design, merged tool-worker/telemetry/protected-health subprojects, and migration head `0015`.
- Produces: this tracked implementation sequence; no runtime behavior.

- [ ] **Step 1: Verify the prerequisite tree without changing it**

```bash
test "$(git rev-parse --show-toplevel)" = "$PWD"
test -f packages/db/src/jhin_db/alembic/versions/20260818_0015_protected_health.py
uv run python -c 'from alembic.script import ScriptDirectory; from jhin_db.migrate import alembic_config; s=ScriptDirectory.from_config(alembic_config("sqlite://")); assert s.get_heads()==["0015"]'
git status --short
test "$(wc -c < orgforge-production-implementation-plan.md | tr -d ' ')" = "82118"
test "$(shasum -a 256 orgforge-production-implementation-plan.md | cut -d ' ' -f 1)" = "ddb4d42cda623bad3fc2fb36d9092aaba6e8293533b29c80994cd738d6167513"
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } > "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
test -s "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
```

Expected: head is exactly `0015`; the untracked OrgForge file begins at the reviewed 82,118-byte/SHA-256 baseline and the private Git checkpoint exists outside the worktree. If the predecessor or baseline differs, stop before Task 1 and ask the repository owner whether the changed prerequisite is intentional.

- [ ] **Step 2: Stage and commit only the plan**

```bash
git add docs/superpowers/plans/2026-08-18-phase-10-dlq-retry.md
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: plan phase 10 dlq and retry"
```

Expected cached path: `docs/superpowers/plans/2026-08-18-phase-10-dlq-retry.md` only.

---

### Task 1: Add Shared Recovery Contracts and Required Retry Safety

**Files:**
- Modify: `packages/domain/src/jhin_domain/enums.py`
- Create: `packages/domain/src/jhin_domain/recovery.py`
- Create: `packages/domain/src/jhin_domain/recovery_contract.json`
- Modify: `packages/domain/src/jhin_domain/__init__.py`
- Modify: `packages/domain/tests/test_enums.py`
- Create: `packages/domain/tests/test_recovery.py`
- Modify: `packages/policy/src/jhin_policy/capabilities.py`
- Modify: `packages/policy/tests/test_capabilities.py`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Modify: `packages/tools/src/jhin_tools/organization.py`
- Modify: `packages/tools/tests/test_builtin.py`
- Modify: `packages/tools/tests/test_gateway.py`
- Modify: `packages/tools/tests/test_gateway_concurrency.py`
- Modify: `packages/connectors/src/jhin_connectors/example/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/github/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/cli/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/management_tools.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/database_tools.py`
- Modify: `packages/connectors/tests/test_manifest_registry.py`
- Modify: `packages/policy/tests/test_evaluator.py`
- Modify: `apps/api/tests/test_policy_unit.py`
- Modify: `services/agent_worker/tests/test_phase9_invocation_activity.py`
- Modify: `tests/integration/test_phase9_authorization.py`

**Interfaces:**
- Consumes: existing `ToolDefinition`, the definition-only catalog from subproject 1, and every shipped tool registration.
- Produces: `RetrySafety`, required `ToolDefinition.retry_safety`, exact catalog classification, and the only Python/JSON authority for recovery reason codes and safe copy. Task 2 adds the ORM column and gateway persistence after the column exists.

- [ ] **Step 1: Write failing shared-contract, enum, required-field, and full-catalog tests**

In `test_recovery.py`, assert `recovery_contract.json` has exactly the three objects `replay_dispatch`, `replay_eligibility`, and `task_retry`; each object maps every Literal member in Shared Interfaces to the exact fixed copy and has no extra key. Assert every key/copy is ASCII, keys are `1..64`, copy is `1..200`, and no copy contains `{}`, `%`, IDs, URLs, infrastructure coordinates, or exception text. Import all types/maps and `TASK_RETRY_MAX_GENERATION == 2_147_483_647` through `jhin_domain.__init__` and assert the exported objects are the same objects consumed by `jhin_domain.recovery`, not service-local copies.

Add a test that omitting `retry_safety` raises Pydantic `ValidationError`; importing each enum returns the exact string values; and the definition catalog partitions every registered name exactly once:

```python
def test_every_registered_tool_has_one_reviewed_retry_safety() -> None:
    definitions = build_default_definition_catalog().definitions()
    actual = {definition.name: definition.retry_safety for definition in definitions}
    assert set(actual) == PURE_TOOLS | IDEMPOTENT_TOOLS | NON_IDEMPOTENT_TOOLS
    assert not (PURE_TOOLS & IDEMPOTENT_TOOLS)
    assert not (PURE_TOOLS & NON_IDEMPOTENT_TOOLS)
    assert not (IDEMPOTENT_TOOLS & NON_IDEMPOTENT_TOOLS)
    assert {name for name, value in actual.items() if value is RetrySafety.PURE} == PURE_TOOLS
    assert {name for name, value in actual.items() if value is RetrySafety.IDEMPOTENT} == IDEMPOTENT_TOOLS
    assert {
        name for name, value in actual.items() if value is RetrySafety.NON_IDEMPOTENT
    } == NON_IDEMPOTENT_TOOLS
```

Update every test fixture found by `rg -l 'ToolDefinition\(' --glob '*.py'` to pass an explicit reviewed value. Add regressions proving the read-only example connector action `example.ping` is exactly `pure` and the low-risk `cli.test.run` definition is `non_idempotent`, so neither package location nor risk is used as a safety proxy. After editing, rerun that `rg` inventory and verify every construction has an explicit `retry_safety=` within its call; this catches predecessor-plan fixtures added after this plan was written.

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/domain/tests/test_enums.py packages/domain/tests/test_recovery.py packages/policy/tests/test_capabilities.py packages/tools/tests/test_builtin.py packages/tools/tests/test_gateway.py packages/connectors/tests/test_manifest_registry.py -q
```

Expected: FAIL because `jhin_domain.recovery`, its canonical JSON contract, `RetrySafety`, the required model field, and reviewed catalog declarations do not exist.

- [ ] **Step 3: Add the closed enum and require an explicit declaration**

Add/export `RetrySafety` and every recovery Literal/map exactly as Shared Interfaces specifies. `recovery.py` loads and validates copy from package-local `recovery_contract.json` once; API and workers import only from `jhin_domain`, never from each other. Add `retry_safety: RetrySafety` to `ToolDefinition` without a default. Update every construction in the file map, including test fixtures and the example connector, using the exact three reviewed sets. Do not infer safety from `risk`, `supports_approval`, name prefixes, or HTTP method.

- [ ] **Step 4: Run GREEN and affected policy/tool suites**

```bash
uv run pytest packages/domain/tests packages/policy/tests packages/tools/tests packages/connectors/tests/test_manifest_registry.py apps/api/tests/test_policy_unit.py services/agent_worker/tests/test_phase9_invocation_activity.py tests/integration/test_phase9_authorization.py -q
uv run ruff check packages/domain packages/policy packages/tools packages/connectors/tests/test_manifest_registry.py
uv run mypy packages/domain/src packages/policy/src packages/tools/src packages/connectors/src
```

- [ ] **Step 5: Commit exact scope**

```bash
git add packages/domain/src/jhin_domain/enums.py packages/domain/src/jhin_domain/recovery.py packages/domain/src/jhin_domain/recovery_contract.json packages/domain/src/jhin_domain/__init__.py packages/domain/tests/test_enums.py packages/domain/tests/test_recovery.py packages/policy/src/jhin_policy/capabilities.py packages/policy/tests/test_capabilities.py packages/policy/tests/test_evaluator.py packages/tools/src/jhin_tools/builtin.py packages/tools/src/jhin_tools/organization.py packages/tools/tests/test_builtin.py packages/tools/tests/test_gateway.py packages/tools/tests/test_gateway_concurrency.py packages/connectors/src/jhin_connectors/example/tools.py packages/connectors/src/jhin_connectors/github/tools.py packages/connectors/src/jhin_connectors/linear/tools.py packages/connectors/src/jhin_connectors/cli/tools.py packages/connectors/src/jhin_connectors/vercel/tools.py packages/connectors/src/jhin_connectors/supabase/management_tools.py packages/connectors/src/jhin_connectors/supabase/database_tools.py packages/connectors/tests/test_manifest_registry.py apps/api/tests/test_policy_unit.py services/agent_worker/tests/test_phase9_invocation_activity.py tests/integration/test_phase9_authorization.py
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add recovery contracts and retry safety"
```

---

### Task 2: Add Recovery Models and Reversible `0016` Migration

**Files:**
- Create: `packages/db/src/jhin_db/models/recovery.py`
- Modify: `packages/db/src/jhin_db/models/work.py`
- Modify: `packages/db/src/jhin_db/models/policy.py`
- Modify: `packages/db/src/jhin_db/models/trigger.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Modify: `packages/tools/src/jhin_tools/gateway.py`
- Modify: `packages/tools/tests/test_gateway.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260818_0016_dlq_retry.py`
- Create: `packages/db/tests/test_recovery_models.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `packages/recovery/pyproject.toml`
- Create: `packages/recovery/src/jhin_recovery/__init__.py`
- Create: `packages/recovery/src/jhin_recovery/deletion.py`
- Create: `packages/recovery/src/jhin_recovery/nats.py`
- Create: `packages/recovery/src/jhin_recovery/replay.py`
- Create: `packages/recovery/src/jhin_recovery/task_retry.py`
- Create: `packages/recovery/src/jhin_recovery/trigger_start.py`
- Create: `packages/recovery/tests/test_import_boundaries.py`
- Create: `packages/recovery/tests/test_trigger_start.py`
- Modify: `apps/api/pyproject.toml`
- Modify: `services/event_worker/pyproject.toml`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `docker/python.Dockerfile`
- Modify: `packages/db/tests/test_migration_graph.py`
- Create: `tests/integration/test_phase10_dlq_retry_migration.py`

**Interfaces:**
- Consumes: head `0015`, UUIDv7 mixins, `JsonDict`, `UtcDateTime`, Task 1 enums, existing task/run/tool/audit tables.
- Produces: five recovery command/state models plus durable `RecoveryWorkspaceDeletion`, deletion-safe plain UUID ownership, immutable command/ordinary/trigger-start authorization fences and NATS reconciliation baselines, additive historical safety/snapshot/run-link/terminal-proof columns, monotonic task-retry generation authority, gateway/approval persistence of historical safety, an installed neutral `jhin-recovery` package boundary, exact constraints/indexes, fresh/upgrade/downgrade/re-upgrade proof, and head `0016`.

- [ ] **Step 1: Write failing metadata and graph tests**

Assert exact table/column nullability, string lengths, check constraints, foreign keys, unique indexes, and partial indexes. The model test must instantiate each row and assert no raw-payload column exists:

```python
FORBIDDEN_COLUMNS = {
    "raw_payload", "raw_message", "request_body", "response_body", "stack_trace",
    "authorization", "cookie", "secret", "prompt", "tool_input",
}

def test_recovery_tables_have_no_raw_payload_storage() -> None:
    for model in (
        RecoveryWorkspaceDeletion,
        EventProcessingState, EventProcessingFailure, EventReplayRequest,
        TaskRetry, OperationsOutbox,
    ):
        assert FORBIDDEN_COLUMNS.isdisjoint(model.__table__.columns.keys())
```

Add an import-boundary test that installs/imports `jhin_recovery` from the workspace wheel and AST-scans it, API, event-worker, and agent-worker: `jhin_recovery` may import `jhin_domain`, `jhin_db`, `jhin_triggers`, `jhin_workflows`, Temporal/NATS/SQLAlchemy, but no `apps.*`, `jhin_api`, `jhin_event_worker`, or `jhin_agent_worker`; service packages may import `jhin_recovery`, never one another. Assert all three service `pyproject.toml` files declare `jhin-recovery` and matching workspace sources, the root workspace includes `packages/recovery`, and the frozen lock contains the package. Update the graph test to require `0016 -> 0015 -> ... -> 0001`. In the real-PostgreSQL integration test, create two disposable databases and cover:

1. `base -> 0016` with all recovery tables/constraints present;
2. `0015 -> 0016` while seeded Phase 9/health rows survive;
3. at `0016`, exercise every new check/default/index with disposable Phase 10 rows; downgrade to `0015` and assert the Phase 10 tables plus added Task/AgentRun/ToolCall/TriggerInvocation columns are absent while the pre-existing `0015` workspace/task/run/tool/heartbeat row values remain byte-for-byte equal; re-upgrade to `0016` and assert recovery tables are recreated empty, new nullable fields are null, new counters/defaulted fields have their documented defaults, and the original `0015` data remains unchanged. Data in a dropped Phase 10 table/column is intentionally not asserted to survive downgrade;
4. construct the upgraded database config and call `alembic.command.check(config)`; the test passes only when it returns without `AutogenerateDiffsDetected`.
5. seed a workspace plus every recovery row, mark its durable deletion row `deleted`, hard-delete the workspace through SQL, and prove deletion/recovery state/failure/replay/task-retry/outbox survive while ordinary workspace-owned task/run rows cascade; no recovery ownership column has a workspace/task/run/user FK that can erase intent.
6. at `0016`, assert every ordinary/trigger/command authorization, versioned start-input, ordinary start-repair lease, start-acceptance, terminal-reconcile lease, input-clearing, and terminal-proof check rejects malformed partial shapes; both ordinary/manual contracts permit the current Agent FK to become null only after their immutable input is authorized; `Task.last_retry_attempt_number` allocates monotonically; SQLite and PostgreSQL both create the portable `length(...)` constraints and ordinary start-repair/terminal partial indexes; and no generated migration diff remains. Authorization/input/proof values in columns/tables dropped by the `0015` downgrade are expected to be absent after re-upgrade, not magically restored.

In `test_recovery_models.py`, compile and create the metadata on SQLite and inspect the emitted check SQL: it must contain `length(` and must not contain `char_length` or a PostgreSQL-only JSON type. Insert ASCII and multibyte boundary cases through the application validators and repeat the accepted/rejected cases in the PostgreSQL migration test. Round-trip exact ordinary/manual `AgentTaskInput` contracts, reject extra keys/wrong retry-attempt pairing/noncanonical UUIDs/20,001-character or 82,001-byte input, and prove input can become null only atomically with matching accepted actual-close proof/clear time. Reject version-1 ordinary authorization without exactly one due-or-claimed start-repair shape, acceptance that leaves a start-repair claim/due row, proof without accepted-at, due-plus-claimed or half-claimed terminal-reconcile shapes, proof with a remaining terminal claim, and any terminal projection path that clears input or changes either reconciliation authority. Assert an accepted transition atomically clears start-repair due/claim/failure state and creates terminal-reconcile due state. Assert a replay/outbox baseline of `0` is accepted and `-1` is rejected. In `test_trigger_start.py`, assert the closed workflow-name/input dataclass is importable from the installed neutral package, rejects extra/unbounded/noncanonical input, and cannot be constructed from a version-0 legacy row. Temporal describe/re-drive behavior remains deliberately unwired until Task 3's RED.

Add gateway tests that claim one pure and one non-idempotent definition and assert the exact persisted strings, then mutate the catalog definition presented to an already-claimed invocation and assert replay fails closed on retry-safety mismatch. Add a parked-approval regression: request approval while a definition is `pure`, redeploy the same tool name/schema as `non_idempotent`, then resolve the approval. Resolution must compare the approval's bound `retry_safety`, the persisted `ToolCall.retry_safety`, and the current definition, reject with the fixed `invocation_mismatch` outcome, execute no tool, and append only the existing sanitized rejection audit. The reverse drift is rejected identically; risk/scope equality cannot mask safety drift.

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/db/tests/test_recovery_models.py packages/db/tests/test_migration_graph.py packages/recovery/tests/test_import_boundaries.py packages/recovery/tests/test_trigger_start.py packages/tools/tests/test_gateway.py -q
uv run pytest -m integration tests/integration/test_phase10_dlq_retry_migration.py -q
uv build --package jhin-recovery
```

Expected: tests FAIL because the models/revision/ToolCall field/package declarations do not exist, gateway persistence cannot pass, and head remains `0015`; `uv build` fails because the neutral distribution does not exist. Do not add package manifests or Docker copies before observing both REDs.

- [ ] **Step 3: Implement the exact ORM shape**

Use plain strings plus database checks, never native PostgreSQL enums. The exact model fields are:

```text
recovery_workspace_deletion
  workspace_id UUID PK with no workspace FK
  status String(16) nonnull check deleting|deleted|blocked
  requested_at timestamptz nonnull
  requested_by_user_id UUID nullable plain preserved actor ID
  available_at timestamptz nonnull
  attempt_count Integer nonnull default 0 check 0..20
  claim_token/claim_expires_at paired nullable
  safe_error_code String(64) nullable check live_handler|outcome_unknown|recovery_invariant
  deleted_at timestamptz nullable
  created_at timestamptz nonnull server now
  updated_at timestamptz nonnull server now

event_processing_state
  origin_stream String(16) PK
  consumer_name String(100) PK
  source_stream_sequence BigInteger PK
  workspace_id UUID nullable indexed, plain preserved workspace ID with no FK
  event_id UUID nullable
  correlation_id UUID nullable
  subject String(500) nonnull
  source_consumer_sequence BigInteger nullable
  last_delivery_count Integer nonnull check >=1
  handler_attempt_count SmallInteger nonnull default 0 check 0..5
  mode String(24) nonnull default handling check handling|quarantine_only|completed
  last_reason_code String(64) nullable closed check
  claim_token UUID nullable
  claim_expires_at timestamptz nullable
  first_seen_at timestamptz nonnull server now
  last_attempted_at timestamptz nullable
  created_at/updated_at timestamptz nonnull server now
  check claim_token and claim_expires_at are both null or both nonnull

event_processing_failure
  id UUIDv7 PK
  workspace_id UUID nullable indexed, plain preserved workspace ID with no FK
  event_id/correlation_id UUID nullable
  origin_stream String(16) nonnull closed
  consumer_name String(100) nonnull
  subject String(500) nonnull
  source_stream_sequence BigInteger nonnull
  source_consumer_sequence BigInteger nullable
  delivery_count Integer nonnull check >=1
  handler_attempt_count SmallInteger nonnull check 0..5
  reason_code String(64) nonnull closed
  safe_error_class String(64) nullable check validation|unsupported|dependency_unavailable|temporal_unavailable|database_unavailable|internal
  safe_error_detail Text nullable check length(safe_error_detail) <= 2000
  status String(24) nonnull default open closed
  first_failed_at/last_failed_at timestamptz nonnull
  replayed_at/resolved_at timestamptz nullable
  resolved_by_user_id UUID nullable (plain preserved actor ID)
  latest_replay_request_id UUID nullable FK event_replay_request SET NULL, use_alter
  created_at/updated_at
  unique(origin_stream, consumer_name, source_stream_sequence)
  index(workspace_id, status, first_failed_at DESC, id DESC)

event_replay_request
  id UUIDv7 PK
  workspace_id UUID nonnull indexed, plain preserved workspace ID with no FK
  failure_id UUID nonnull FK event_processing_failure CASCADE
  requested_by_user_id UUID nonnull (plain preserved actor ID)
  idempotency_key String(128) nonnull
  replay_generation Integer nonnull check >=1
  replay_event_id UUID nonnull unique
  status String(24) nonnull default requested closed
  attempt_count Integer nonnull default 0 check 0..20
  available_at timestamptz nonnull
  external_call_authorized_at timestamptz nullable
  dispatch_baseline_sequence BigInteger nullable check >=0
  safe_error_code String(64) nullable check source_event_expired|source_event_changed|invalid_source_event|requester_unauthorized|workspace_inactive|reason_not_replayable|nats_unavailable|database_unavailable|dispatch_exhausted|invariant_violation
  published_at timestamptz nullable
  claim_token UUID nullable / claim_expires_at timestamptz nullable paired check
  created_at/updated_at
  unique(workspace_id, idempotency_key)
  unique(failure_id, replay_generation)
  partial unique(failure_id) WHERE status IN ('requested','dispatching')
  index(status, available_at)

task_retry
  id UUIDv7 PK
  workspace_id UUID nonnull indexed, plain preserved workspace ID with no FK
  task_id UUID nonnull plain preserved task ID with no FK
  source_run_id UUID nullable plain preserved run ID with no FK
  requested_by_user_id UUID nonnull (plain preserved actor ID)
  idempotency_key String(128) nonnull
  attempt_number Integer nonnull check 2..2147483647
  temporal_workflow_id String(200) nonnull unique
  configuration_mode String(16) nonnull default current check current
  status String(24) nonnull default requested closed
  new_run_id UUID nullable unique plain preserved run ID with no FK
  safe_reason_code String(64) nullable check eligible|task_not_failed|workflow_active|retry_in_progress|idempotency_key_conflict|agent_missing|agent_inactive|budget_exhausted|authorization_unchanged|policy_unchanged|configuration_unchanged|max_steps_unchanged|explicit_rejection_unchanged|committed_external_effect|ambiguous_external_effect|unknown_tool_safety|concurrency_wait|requester_unauthorized|workspace_inactive|temporal_unavailable|dispatch_exhausted|invariant_violation
  attempt_count Integer nonnull default 0 check 0..20
  available_at timestamptz nonnull
  external_call_authorized_at timestamptz nullable
  start_contract_version Integer nonnull default 0 check 0|1
  start_input_json JsonDict nullable portable check length(CAST(... AS TEXT)) <= 82000
  start_input_cleared_at timestamptz nullable
  started_at timestamptz nullable
  terminal_reconcile_available_at timestamptz nullable
  terminal_reconcile_failure_count Integer nonnull default 0 check 0..9
  terminal_reconcile_claim_token UUID nullable / terminal_reconcile_claim_expires_at timestamptz nullable paired check
  terminal_proof_generation Integer nonnull default 0 check 0 or == attempt_number
  terminal_proven_at timestamptz nullable
  terminal_workflow_status String(24) nullable check completed|failed|cancelled|terminated|timed_out
  terminal_run_outcome String(24) nullable check terminal_run|closed_before_run
  terminal_run_id UUID nullable plain preserved run ID with no FK
  terminal_run_status String(24) nullable check completed|failed|cancelled
  claim_token/claim_expires_at paired nullable
  created_at/updated_at
  unique(workspace_id, idempotency_key)
  unique(task_id, attempt_number)
  partial unique(task_id) WHERE status IN ('requested','dispatching')
  index(status, available_at)
  check generation 0 iff all terminal proof fields are null; generation == attempt_number requires timestamp/workflow status/run outcome, terminal_run requires paired run ID/status, closed_before_run requires both run fields null
  check contract version 0 iff authorization/input/cleared are null; version 1 requires authorization and exactly one of input or cleared; cleared requires complete matching terminal proof and cleared_at >= terminal_proven_at
  check a started version-1 row without proof has exactly one due-or-claimed terminal-reconcile shape; proof clears due/claim and resets failure count

operations_outbox
  id UUIDv7 PK
  workspace_id UUID nullable indexed, plain preserved workspace ID with no FK
  kind String(32) nonnull check event_failure_dlq
  aggregate_id UUID nonnull
  payload_json JsonDict nonnull
  message_id String(200) nonnull unique
  status String(16) nonnull default pending closed
  attempt_count Integer nonnull default 0 check 0..20
  available_at timestamptz nonnull
  external_call_authorized_at timestamptz nullable
  dispatch_baseline_sequence BigInteger nullable check >=0
  published_at timestamptz nullable
  safe_error_code String(64) nullable check nats_unavailable|database_unavailable|dispatch_exhausted|invariant_violation
  claim_token/claim_expires_at paired nullable
  created_at/updated_at
  unique(kind, aggregate_id)
  index(status, available_at)
  portable check length(CAST(payload_json AS TEXT)) <= 4096; application validation enforces canonical compact UTF-8 bytes <=4096 before insert on every dialect
```

Add `ToolCall.retry_safety: Mapped[str]` nonnull with server default `non_idempotent`. Migration backfill uses the exact Task 1 pure/idempotent name sets; unknown and every other historical tool remain `non_idempotent`.

Add nullable `Task.temporal_start_authorized_at`, nullable `Task.temporal_start_accepted_at`, nonnull `Task.temporal_start_contract_version Integer default 0 check 0|1`, nullable `Task.temporal_start_input_json JsonDict`, nullable `Task.temporal_start_input_cleared_at`, nullable `Task.temporal_start_reconcile_available_at`, nonnull `Task.temporal_start_reconcile_failure_count Integer default 0 check 0..10`, paired nullable `Task.temporal_start_reconcile_claim_token/temporal_start_reconcile_claim_expires_at`, nullable `Task.temporal_terminal_reconcile_available_at`, nonnull `Task.temporal_terminal_reconcile_failure_count Integer default 0 check 0..9`, paired nullable `Task.temporal_terminal_reconcile_claim_token/temporal_terminal_reconcile_claim_expires_at`, nullable `Task.temporal_start_terminal_proven_at`, nullable `Task.temporal_start_terminal_workflow_status String(24)`, nullable `Task.temporal_start_terminal_run_outcome String(24)`, nullable plain-UUID `Task.temporal_start_terminal_run_id`, nullable `Task.temporal_start_terminal_run_status String(24)`, and nonnull `Task.last_retry_attempt_number: Mapped[int]` with server default `1` and check `1..TASK_RETRY_MAX_GENERATION`.

The ordinary-start invariant is exact. Version 0 requires authorization/acceptance/input/cleared/start-repair/terminal-reconcile/proof fields null and means `Task.temporal_workflow_id` is only an unstarted ordinary binding/projection. Version 1 requires a nonnull immutable workflow ID/authorization plus exactly one of bounded input JSON or cleared-at. An authorized, unaccepted, uncleared row has exactly one due-or-claimed start-repair shape, no terminal-reconcile fields, and remains selectable regardless of `Task.state`, assignment, description, metadata, or Agent existence. Acceptance atomically writes `temporal_start_accepted_at`, clears start-repair due/claim/failure state, and creates exactly one due-or-claimed terminal-reconcile shape. Before terminal proof, input is nonnull and cleared-at/proof are null. The terminal Task/AgentRun projection activity writes only product state; it cannot change either reconciliation authority, close status, proof, or input because Temporal has not closed yet. Clearing requires accepted-at, an actual allowlisted closed Temporal status observed later, and either an exact matching terminal `AgentRun` ID/status or bounded `closed_before_run` history proof; the post-close reconciler nulls input and writes cleared-at/proof together, then clears terminal due/claim and resets its diagnostic failure count.

Add portable `length(Task.temporal_workflow_id) BETWEEN 1 AND 200`, `length(CAST(temporal_start_input_json AS TEXT)) <= 82_000`, closed status/outcome checks, paired start/terminal claim checks, `cleared_at >= terminal_proven_at`, and proof-implies-accepted checks. Add `ix_task_ordinary_start_reconcile_due` on `(temporal_start_reconcile_available_at,id)` with PostgreSQL/SQLite predicate `temporal_start_contract_version=1 AND temporal_start_accepted_at IS NULL AND temporal_start_input_cleared_at IS NULL`, plus `ix_task_ordinary_terminal_reconcile_due` on `(temporal_terminal_reconcile_available_at,id)` with predicate `temporal_start_accepted_at IS NOT NULL AND temporal_start_terminal_proven_at IS NULL`. Do **not** require current `Task.assigned_agent_id` once authorized: agent deletion may set it null, while every call uses the persisted contract's agent UUID. Neither ordinary field/contract is ever overwritten by a manual retry. The counter is allocated under the Task row lock and is the only retry-generation authority, so deleting a retained `TaskRetry` can never cause an ID to be reused. At the fixed integer maximum, creation fails closed as `invariant_violation` without allocating or starting. These columns stay on the product row until deletion has post-close terminal proof for all ordinary/retry starts.

Extend `TriggerInvocation` with nullable `workflow_name String(64)`, nullable `workflow_input_json JsonDict`, nonnull `start_contract_version Integer default 0 check 0|1`, `start_authorized_at`, `start_accepted_at`, `terminal_proven_at`, `terminal_workflow_status String(24)`, nullable `start_reconcile_available_at`, nonnull `start_reconcile_failure_count Integer default 0 check 0..10`, and paired nullable `start_reconcile_claim_token/start_reconcile_claim_expires_at`. Add index `(start_reconcile_available_at,id)` with PostgreSQL/SQLite predicate `status='started' AND terminal_proven_at IS NULL`. Migration marks every predecessor row version 0 with name/input/authorization null and sets `start_reconcile_available_at=now()` only for legacy `status='started'`; duplicates/failed rows remain null. For every new started invocation the exact closed name, canonical bounded input, `version=1`, workflow ID, authorization timestamp, and reconcile availability are nonnull together before Temporal; database checks require version 0 to have all three authorization-contract fields null and version 1 to have all nonnull, plus the paired claim invariant. Use portable `length(...)` checks for name/ID and `length(CAST(workflow_input_json AS TEXT)) <= 20_000`, plus application validation of the canonical UTF-8 byte representation. A terminal proof requires accepted-at plus a closed allowlisted workflow status and clears claim/availability. The background reconciler may describe a version-0 workflow ID and record accepted/terminal history if observed, but cannot re-drive NotFound without immutable input; that legacy row stays an explicit recovery invariant/deletion blocker rather than inventing input.

For all three recovery command tables, add the check `external_call_authorized_at IS NULL OR attempt_count BETWEEN 1 AND 20`. Replay/outbox additionally require a nonnull baseline when authorized, task retry requires the version-1 start-contract shape above, and baseline zero is valid for an empty stream. The timestamp is immutable after first set. A pending/requested row at cap with a null timestamp is reachable only through 20 committed calls to `record_preauthorization_failure`; it may terminalize as proven pre-authorization exhaustion. An authorized row may never transition to `dispatch_exhausted` from absence/NotFound and may never clear the timestamp/baseline/identity. Implement every textual bound through SQLAlchemy `func.length(column)`/`func.length(cast(json_column, Text))`, which compiles on SQLite and PostgreSQL; do not use `char_length`, and use the existing `JsonDict` portable type rather than a model-local direct `JSONB` column. `serialize_agent_task_input` additionally enforces canonical compact UTF-8 bytes `<=82_000`, exact keys, canonical UUID strings, instruction `0..20_000` characters, retry/attempt pairing, and equality after deserialize/re-serialize.

When `ToolGateway` creates a `ToolCall` in `_denied`, the durable claim path, or an approval path, copy `definition.retry_safety.value` when a definition exists. Unknown/schema-invalid names are denied before a definition exists and store `RetrySafety.NON_IDEMPOTENT.value`. `_existing_invocation_outcome` includes persisted retry-safety equality in its binding check so a claimed invocation cannot be reinterpreted after a deploy. The parked approval's sanitized bound action payload also stores the closed `retry_safety` string. Approval resolution reloads the `ToolCall` and current `ToolDefinition` and requires all three safety values to match before execution; a `pure -> non_idempotent`, reverse, invalid, or missing drift takes the existing fixed `invocation_mismatch` rejection path and never calls the executor. Add the gateway persistence, replay-mismatch, and parked-approval drift tests here, after the ORM field exists.

Add nullable `AgentRun.task_retry_id` with unique FK to `task_retry.id` (`SET NULL`) and nullable `AgentRun.execution_snapshot_json` using `JsonDict`. The snapshot is credential-free internal state needed to return the same activity result after a commit/worker crash; it is never added to `RunOut` or public run-event payloads. Also add a unique partial index on `agent_run.temporal_workflow_id WHERE temporal_workflow_id IS NOT NULL` after asserting no duplicates in the migration.

- [ ] **Step 4: Implement reversible migration ordering**

Create deletion/state/failure before replay without the circular `latest_replay_request_id` FK, create replay, then add the FK. Create task-retry with its versioned start-contract/proof checks, add the complete ordinary Task authorization/input/clear/proof fields plus `Task.last_retry_attempt_number`, add TriggerInvocation authorization/terminal fields, add run columns/FKs/indexes, then create outbox and tool-call column/backfill. Recovery ownership UUIDs intentionally have no parent FK; keep only recovery-to-recovery FKs. Downgrade reverses that exact order: drop added existing-table constraints/indexes/columns, drop outbox/task-retry, drop the circular failure FK, then replay/failure/state/deletion. Do not drop or rewrite the protected-health table. The migration filename is exactly `20260818_0016_dlq_retry.py`, `revision = "0016"`, `down_revision = "0015"`.

Create the installable `jhin-recovery` skeleton in this commit so every later commit can import only existing modules. `deletion.py`, `nats.py`, `replay.py`, `task_retry.py`, and `trigger_start.py` initially expose only the complete model-independent closed dataclasses/protocols needed now; later tasks add behavior only after their own RED. A skeleton must not export an implementation that raises `NotImplementedError`, and no runnable caller is wired until the owning task's RED. Add it to the root uv workspace, all three consumer projects and sources, and regenerate `uv.lock` with `uv lock`. Update `docker/python.Dockerfile`'s dependency-cache manifest copies with `COPY packages/recovery/pyproject.toml packages/recovery/` before either `uv sync`; otherwise API/event/agent runtime images cannot install the declared workspace dependency. Build all four wheels and the three Python service images, then import `jhin_recovery` from a clean temporary virtual environment in the test. Do not put this contract in API or either worker.

`packages/recovery/pyproject.toml` declares exactly `jhin-db`, `jhin-domain`, `jhin-triggers`, `jhin-workflows`, `nats-py>=2.13`, `temporalio>=1.31`, and `SQLAlchemy[asyncio]>=2.0`, with workspace sources for the four Jhin packages and Hatch wheel package `src/jhin_recovery`. `jhin-workflows` supplies the canonical typed `AgentTaskInput`/queue contract for neutral ordinary/manual start reconciliation; recovery does not recreate it. The package does not depend on FastAPI, any app/service distribution, tools/models/connectors, or observability. API, event-worker, and agent-worker each add `jhin-recovery` plus its workspace source; they retain their existing dependencies. The import-boundary test compares these declarations and AST imports, so dependency drift fails before runtime.

- [ ] **Step 5: Run GREEN and migration gates**

```bash
uv run pytest packages/db/tests/test_recovery_models.py packages/db/tests/test_migration_graph.py packages/recovery/tests/test_import_boundaries.py packages/recovery/tests/test_trigger_start.py packages/tools/tests/test_gateway.py -q
uv run pytest -m integration tests/integration/test_phase10_dlq_retry_migration.py -q
uv run ruff check packages/db packages/recovery tests/integration/test_phase10_dlq_retry_migration.py
uv run mypy packages/db/src packages/recovery/src tests/integration/test_phase10_dlq_retry_migration.py
uv build --package jhin-recovery
docker build --build-arg SERVICE_PACKAGE=jhin-api -f docker/python.Dockerfile -t jhin-api-recovery-boundary .
docker build --build-arg SERVICE_PACKAGE=jhin-event-worker -f docker/python.Dockerfile -t jhin-event-worker-recovery-boundary .
docker build --build-arg SERVICE_PACKAGE=jhin-agent-worker -f docker/python.Dockerfile -t jhin-agent-worker-recovery-boundary .
```

- [ ] **Step 6: Commit exact scope**

```bash
git add pyproject.toml uv.lock docker/python.Dockerfile packages/recovery/pyproject.toml packages/recovery/src/jhin_recovery/__init__.py packages/recovery/src/jhin_recovery/deletion.py packages/recovery/src/jhin_recovery/nats.py packages/recovery/src/jhin_recovery/replay.py packages/recovery/src/jhin_recovery/task_retry.py packages/recovery/src/jhin_recovery/trigger_start.py packages/recovery/tests/test_import_boundaries.py packages/recovery/tests/test_trigger_start.py apps/api/pyproject.toml services/event_worker/pyproject.toml services/agent_worker/pyproject.toml packages/db/src/jhin_db/models/recovery.py packages/db/src/jhin_db/models/work.py packages/db/src/jhin_db/models/policy.py packages/db/src/jhin_db/models/trigger.py packages/db/src/jhin_db/models/__init__.py packages/db/src/jhin_db/alembic/versions/20260818_0016_dlq_retry.py packages/db/tests/test_recovery_models.py packages/db/tests/test_migration_graph.py packages/tools/src/jhin_tools/gateway.py packages/tools/tests/test_gateway.py tests/integration/test_phase10_dlq_retry_migration.py
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add durable recovery schema"
```

---

### Task 3: Replace Delivery Counts and Process Memory with Durable Handler Claims

**Files:**
- Modify: `packages/events/src/jhin_events/envelope.py`
- Modify: `packages/events/src/jhin_events/consumer.py`
- Modify: `packages/events/src/jhin_events/publisher.py`
- Create: `packages/events/src/jhin_events/replay.py`
- Modify: `packages/events/tests/test_envelope.py`
- Create: `packages/events/tests/test_consumer.py`
- Create: `packages/events/tests/test_publisher.py`
- Create: `packages/events/tests/test_replay.py`
- Modify: `packages/events/tests/test_telemetry.py`
- Modify: `packages/recovery/src/jhin_recovery/trigger_start.py`
- Modify: `packages/recovery/src/jhin_recovery/deletion.py`
- Modify: `packages/recovery/tests/test_trigger_start.py`
- Create: `services/event_worker/src/jhin_event_worker/delivery.py`
- Create: `services/event_worker/src/jhin_event_worker/failures.py`
- Modify: `services/event_worker/src/jhin_event_worker/processor.py`
- Modify: `services/event_worker/src/jhin_event_worker/normalizer.py`
- Modify: `services/event_worker/src/jhin_event_worker/matcher.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Create: `services/event_worker/tests/conftest.py`
- Create: `services/event_worker/tests/test_delivery.py`
- Modify: `services/event_worker/tests/test_normalizer.py`
- Modify: `services/event_worker/tests/test_matcher.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `apps/api/tests/test_webhooks_unit.py`
- Create: `tests/integration/test_phase10_processing_claim.py`

**Interfaces:**
- Consumes: Tasks 1-2 enums/models, existing `EventProcessor`/`IngressNormalizer` business behavior, NATS `Msg.metadata`, safe redaction, canonical stream/consumer constants from protected health.
- Produces: replay-root-aware envelope/normalization/matcher keys, immutable trigger-start authorization plus NATS-independent reject-duplicate reconciliation, race-safe PostgreSQL first claim, exact subject/envelope binding, unlimited JetStream consumer configuration, `ClassifiedEventFailure`, a runnable production durable-consumer bridge, and business handlers that never ack/nak/term themselves.

- [ ] **Step 1: Write the full state-machine tests before refactoring**

Use SQLite for pure transition tests and fake `Msg` objects that record `ack`, `nak(delay)`, and `term` calls. Cover:

- first claim locks the workspace row, checks no durable deletion intent, then inserts `handling`, attempt 1, token/expiry; a live competing delivery returns `LEASE_BUSY` without increment;
- an expired lease increments exactly once; attempts never exceed 5;
- a handler blocked for 75 seconds renews the same token at 15-second intervals; a simultaneous redelivery after the original 60-second expiry still returns `LEASE_BUSY`, handler count stays 1, and only the original may complete;
- DB begin/commit failure calls no business handler and naks;
- handler success commits `completed` before one ack;
- handler exceptions 1-4 clear lease, retain `handling`, set safe reason, and nak with bounded indexed delay;
- fifth exception first commits `quarantine_only`, clears the lease, and never calls the handler again;
- malformed envelope and invalid subject enter `quarantine_only` with attempt 0 and never call a handler;
- a syntactically valid subject whose workspace token or complete event suffix differs from the envelope becomes `invalid_subject` before either handler: EVENTS requires exact `jhin.v1.{workspace_id}.{event_type}`; INGRESS requires exact `jhin.v1.{workspace_id}.ingress.{source.type}.{data.event}` and `event_type == f"ingress.{source.type}.{data.event}"`. Cover spoofed workspace, changed event suffix, and connector/event mismatches on both origin streams; replace the source subject with exactly `INVALID_SUBJECT_SENTINEL` before claim/quarantine. Seed a foreign-workspace/suffix canary and assert it is absent from the processing/deferred-quarantine source, structured logs, spans, and metric attributes; no handler, matcher, or normalizer call occurs. Task 4 owns the first failure/outbox/audit/API/DLQ sink assertions after those sinks exist;
- an existing `deleting|blocked` gate observed before claim mutation enters `quarantine_only/workspace_deleted` without a handler so the still-running deletion drain can commit its failure/outbox; an exact `deleted` gate returns `DELETED_WORKSPACE`, creates or mutates no processing/failure/outbox/audit row, calls no handler, and terms once. A term failure leaves no row and redelivery repeats the terminal gate claim. A deletion request arriving after a live claim lets only that exact token renew and finish while every new source claim is barred;
- completed success acks; completed quarantine verifies failure/outbox identity and terms;
- no test path decides from `metadata.num_delivered` except storing the diagnostic delivery count.

Refactor normalizer tests so unsupported connector/event raises a typed `UnsupportedIngressEvent` consumed by `classify_handler_error`, rather than terminating silently. Refactor `EventProcessor` tests to prove its `_seen` LRU is deleted and repeated envelopes reach the database-idempotent matcher path.

Add semantic identity tests for all four paths: original INGRESS, replayed INGRESS, original canonical connector event without `external_id`, and replayed canonical connector event. A replayed INGRESS event must derive the same canonical IDs as its original by using `replay_of_event_id or event_id`; a canonical replay without `external_id` must build the same matcher key from that root. Non-connector EVENTS and malformed self-referential replay roots return no `ReplaySemanticIdentity` and cannot be marked replayable.

In matcher and neutral trigger-start tests, seed two matching triggers where the original delivery committed/started only the first. Before either Temporal call, assert the new `TriggerInvocation` transaction has persisted the deterministic workflow ID, exact closed workflow name, canonical version-1 bounded input JSON, authorization timestamp, and due reconciliation state. Replay records the first as duplicate and starts exactly the missing second workflow. Fake Temporal records `WorkflowIDReusePolicy.REJECT_DUPLICATE`. Simulate `start_workflow` accepting the ID and raising a client error, ack/lose the NATS delivery, close that workflow, restart the worker, and run only the background trigger reconciler: it must load the persisted name/input, describe/finalize accepted once, and create no second workflow. Pause one claimant after its durable contract/claim commit, expire the 30-second lease, let another claimant see NotFound and re-drive, then resume the old caller; both pass byte-equal workflow name/input/ID and one workflow exists. NotFound re-drives that exact persisted contract; unavailable remains authorized with saturated bounded backoff. Add name/input/version/claim-token drift tests. For a migrated version-0 row, observed open/closed history may be recorded, but NotFound must fail closed without a start because no immutable input exists.

Race trigger authorization with workspace DELETE, and crash at each boundary: before authorization commit, after authorization/before Temporal, after accepted start/before `start_accepted_at`, and after close/before terminal proof. If deletion wins the workspace/deletion lock, no new invocation is authorized. If authorization wins, deletion inventories the pre-Task `TriggerInvocation`, invokes the same neutral reconciler independently of NATS, and remains draining until accepted closed-history proof is persisted; it never cancels or guesses from NotFound. Assert one `status=started` authority row and one matching `trigger.invoked` started audit through restart; separately asserted duplicate-delivery rows/audits never authorize another workflow.

The real-PostgreSQL test releases two tasks simultaneously against a missing processing key. Exactly one transaction locks the workspace/deletion gate, performs `INSERT ON CONFLICT`, reloads `FOR UPDATE`, and returns `HANDLE` attempt 1; the other returns `LEASE_BUSY`, and the persisted count is 1 with one live token. Run it repeatedly for both origin streams. Add the real-time 75-second renewal/redelivery case and a replayed race where deletion marks `deleting` between workspace lock acquisition and the insert: either deletion wins and no handler claim is created, or the handler claim wins and deletion must drain it; there is no claimed row born behind the gate. Pause a claimed handler, terminate its PostgreSQL backend with `pg_terminate_backend`, and separately restart the database during renewal; a later redelivery can recover the durable row, but the stale token cannot finalize or create a second semantic effect.

This owning integration file also imports only predecessor `NATS_URL`/`TEMPORAL_ADDRESS`, opens real clients directly with five-second connect/call bounds, and creates unique test-owned NATS consumer/workflow IDs. Before implementation, publish real INGRESS/EVENTS messages and prove actual ack/nak/term plus unlimited-consumer behavior; a foreign-subject canary may appear only in the test's input assertion and is absent from the durable processing/deferred-quarantine projection and telemetry. Against real Temporal, persist one trigger authorization contract, call exactly `ensure_trigger_workflow(temporal, start=authorized_start, timeout_seconds=5.0)` through an accepted-start/finalize crash, wait for close, and call that same signature again; assert both internal starts use `REJECT_DUPLICATE` and one history with byte-equal input exists after NATS loss. Cleanup deletes only unique test consumers and terminates only unique closed/test workflows. `test_phase10_processing_claim.py` defines its own local `asyncio.Event` barriers/backend PID lookup and these minimal real-client fixtures over the existing integration stack; it imports no future Task 9 harness helper. All cases execute in this task's RED command before delivery/trigger code changes and are not deferred to the broad Task 9 harness. SQLite-only conflict behavior or fake-only NATS/Temporal is not acceptance evidence.

Update `apps/api/tests/test_webhooks_unit.py` before implementation so its ingress fake exercises the unchanged telemetry-aware `EventPublisher.publish(envelope, *, headers=None)` interface and expects normalizer/matcher acknowledgement to be owned only by `DurableEventConsumer`. Extend predecessor `packages/events/tests/test_telemetry.py` before implementation to prove caller headers are still merged with W3C propagation, reserved trace/message headers cannot be spoofed, and no payload enters spans. Its RED must fail on the old handler-owned ack/term or missing replay-subject behavior; keep both predecessor tests in every Task 3 RED/GREEN command and staging list.

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/events/tests/test_envelope.py packages/events/tests/test_consumer.py packages/events/tests/test_publisher.py packages/events/tests/test_replay.py packages/events/tests/test_telemetry.py packages/recovery/tests/test_trigger_start.py services/event_worker/tests/test_delivery.py services/event_worker/tests/test_normalizer.py services/event_worker/tests/test_matcher.py services/event_worker/tests/test_telemetry.py apps/api/tests/test_webhooks_unit.py -q
uv run pytest -m integration tests/integration/test_phase10_processing_claim.py -q
```

Expected: FAIL because the replay field, unlimited config, durable state machine, and typed unsupported-event behavior do not exist.

- [ ] **Step 3: Implement bounded source parsing and failure classification**

`SourceMessageMetadata.from_msg(origin_stream, consumer_name, message)` validates metadata exists; stream sequence is `1..2^63-1`; consumer sequence is null or positive; delivery count is at least one; consumer is `1..100`; and subject is ASCII, `1..500`, and matches the exact origin grammar. Invalid grammar becomes `INVALID_SUBJECT_SENTINEL` and terminal/nonreplayable without logging/persisting the raw value. After envelope validation but before deletion/processing claim or any business handler, `validate_subject_envelope_binding` compares parsed tokens exactly: workspace is the canonical lowercase UUID string; `source.type` is one valid subject token; every dot-separated `event_type`/`data.event` segment is ASCII, nonempty, bounded by its envelope model, and contains no whitespace, `*`, or `>`; EVENTS has no wildcard/extra token and equals `event_subject(envelope.workspace_id,envelope.event_type)`; INGRESS equals `jhin.v1.{workspace_id}.ingress.{source.type}.{data.event}`, requires bounded nonempty string `data.event`, and requires `event_type == f"ingress.{source.type}.{data.event}"`. It raises a typed terminal error containing no subject/body. On **any** binding mismatch, the driver immediately replaces the otherwise-valid source via `source.with_invalid_subject_sentinel()` and uses only that copy for processing identity, quarantine, audit/log context, telemetry, and DLQ construction. Thus every invalid-subject state/failure/API/DLQ projection contains exactly `"<invalid-subject>"`; the original mismatch is never a “validated bounded subject” eligible for persistence.

`DeliveryIdentity` comes only from a successfully validated envelope whose workspace string parses as UUID. A malformed envelope may use a UUID workspace parsed from the validated subject token; otherwise all three identity fields are null. `invalid_envelope_failure` stores `safe_error_detail=f"Envelope validation failed with {min(error_count, 1000)} issue(s)."`; unknown handler exceptions store the fixed text `"Event handling failed; inspect sanitized event-worker logs."`, never `str(error)`. Known Temporal/database/unavailable exception families map to closed safe classes and fixed text.

Reject `replay_of_event_id == event_id`. `replay_root_event_id` is exactly the optional root or current event ID. `replay_semantic_identity` accepts INGRESS only as `handler_name="ingress-normalizer"`, `semantic_key=f"ingress:{root}"`; accepts EVENTS only when `event_type.startswith("connector.")`, using `semantic_key=f"connector:{source.connection_id or ''}:{external_id or root}"`; and returns `None` for every other handler. A future handler must add an explicit branch and a partial-original-work idempotency test before replay eligibility can become true.

- [ ] **Step 4: Implement transactionally locked claim transitions**

Every transition opens its own short session/transaction. For a nonnull workspace, `claim_processing_attempt` first selects the exact `Workspace` row `FOR UPDATE`, then selects `RecoveryWorkspaceDeletion` by workspace ID `FOR UPDATE`. A missing workspace is valid only with a locked `deleted` gate; missing both is `processing_invariant`. If that exact gate is `deleted`, return `ProcessingClaim(ClaimKind.DELETED_WORKSPACE, 0, None, EventFailureReason.WORKSPACE_DELETED)` **before** inserting/selecting a processing row; assert any pre-existing nonterminal recovery row would be a deletion invariant because final cascade could not have committed with it. Only otherwise, while the locks are held, does PostgreSQL execute `INSERT ... ON CONFLICT (origin_stream,consumer_name,source_stream_sequence) DO NOTHING` with the zero-attempt identity, then select that exact key `FOR UPDATE`; SQLite performs insert in a savepoint and reloads after unique conflict. An inactive workspace or `deleting|blocked` writes/reloads that zero-attempt processing row directly as `quarantine_only/workspace_deleted` without incrementing or invoking a handler. Null-workspace malformed input skips only the workspace lock, not processing-key serialization. This workspace -> deletion -> processing lock order is universal. There is no read-then-insert window and no claim mutation before the durable gate. It then follows this exact order:

```python
if row is None:
    row = EventProcessingState(
        origin_stream=source.origin_stream.value,
        consumer_name=source.consumer_name,
        source_stream_sequence=source.source_stream_sequence,
        workspace_id=identity.workspace_id,
        event_id=identity.event_id,
        correlation_id=identity.correlation_id,
        subject=source.subject,
        source_consumer_sequence=source.source_consumer_sequence,
        last_delivery_count=source.delivery_count,
        handler_attempt_count=0,
        mode=EventProcessingMode.HANDLING.value,
    )
if row.mode == "completed":
    kind = (
        ClaimKind.COMPLETED_SUCCESS
        if row.last_reason_code is None
        else ClaimKind.COMPLETED_QUARANTINE
    )
    return ProcessingClaim(
        kind, row.handler_attempt_count, None,
        EventFailureReason(row.last_reason_code) if row.last_reason_code else None,
    )
if row.mode == "quarantine_only":
    if row.last_reason_code is None:
        raise ProcessingStateInvariantError("quarantine state has no terminal reason")
    return ProcessingClaim(
        ClaimKind.QUARANTINE, row.handler_attempt_count, None,
        EventFailureReason(row.last_reason_code),
    )
if row.claim_expires_at is not None and row.claim_expires_at > now:
    return ProcessingClaim(
        ClaimKind.LEASE_BUSY, row.handler_attempt_count, None,
        EventFailureReason(row.last_reason_code) if row.last_reason_code else None,
    )
if terminal_failure is not None:
    row.mode = "quarantine_only"
    row.last_reason_code = terminal_failure.reason_code.value
    row.claim_token = None
    row.claim_expires_at = None
    return ProcessingClaim(ClaimKind.QUARANTINE, row.handler_attempt_count, None, terminal_failure.reason_code)
if row.handler_attempt_count >= PROCESSING_MAX_ATTEMPTS:
    if row.last_reason_code is None:
        raise ProcessingStateInvariantError("exhausted state has no failure reason")
    row.mode = "quarantine_only"
    row.claim_token = None
    row.claim_expires_at = None
    return ProcessingClaim(
        ClaimKind.QUARANTINE, row.handler_attempt_count, None,
        EventFailureReason(row.last_reason_code),
    )
row.handler_attempt_count += 1
row.claim_token = new_uuid7()
row.claim_expires_at = now + timedelta(seconds=PROCESSING_LEASE_SECONDS)
row.last_attempted_at = now
return ProcessingClaim(
    ClaimKind.HANDLE, row.handler_attempt_count, row.claim_token,
    EventFailureReason(row.last_reason_code) if row.last_reason_code else None,
)
```

After lock/reload, require persisted origin/consumer/stream sequence/subject/workspace/event/correlation identity to equal the current validated source; mismatch is terminal `processing_invariant`. Update only `source_consumer_sequence` and `last_delivery_count=max(existing,source.delivery_count)` before returning a claim. These persisted bounded diagnostics let durable deletion drain inspect the exact row without guessing from a future NATS redelivery.

`mark_handler_succeeded` and `mark_handler_failed` require exact composite key, `mode=handling`, and matching token under lock; mismatch raises `ProcessingStateInvariantError` and cannot ack. Failure 1-4 returns `handling`; failure 5 sets `quarantine_only`; both clear claims. All UTC inputs must be aware.

`renew_processing_claim` locks the exact row, requires `handling` plus the same token, and extends expiry to `now + 60 seconds` without incrementing attempts. It is allowed when the workspace deletion row has become `deleting`, because stopping renewal would manufacture a stealable live external call; it is forbidden once the processing row itself is terminal or the token differs. `ProcessingLeaseKeeper` renews every 15 seconds in a separate short session, records the first ownership/DB failure, and makes `assert_owned()` fail closed. Business handling is bounded by `asyncio.timeout(300)` and finalization occurs only after `assert_owned()`. A competitor can reclaim only a truly expired token; token-CAS finalization makes a late original unable to ack. All replayable business handlers use the stable semantic keys above, so even the DB-outage edge remains same-effect idempotent rather than authorizing a new identity.

Successful completion also clears `last_reason_code`; otherwise a success after an earlier transient failure could be mistaken for completed quarantine. A classified failure with `terminal_delivery=True` (invalid envelope, invalid subject, unsupported ingress, processing invariant, or workspace deleted) enters `quarantine_only` immediately without waiting for attempt 5. Only ordinary handler exceptions consume all five attempts. When a redelivery sees `quarantine_only`, reconstruct the stable bounded classification with `failure_for_reason(last_reason_code)`; never require the original exception object to survive a commit failure or worker restart.

- [ ] **Step 5: Make the business handlers ack-free and implement the durable driver**

`EventProcessor.handle_event(envelope)` only invokes the matcher/logs. Remove `_seen`, JetStream dependency, JSON DLQ publishing, and acknowledgement. `IngressNormalizer.handle_event(envelope)` only validates connector/event/payload, normalizes, and publishes deterministic canonical events; unsupported input raises `UnsupportedIngressEvent`. Its `derived_event_id` hashes `(replay_root_event_id(ingress), index)`, and that derived ID is both the canonical envelope event ID and transport message ID on original and replay. The emitted canonical envelope leaves `replay_of_event_id=None` because its event ID already equals the original canonical identity; setting it to the ingress root would break matcher fallback, and setting it to itself is invalid. Its `causation_id` is the current ingress transport event ID.

TriggerMatcher uses nonempty `data.external_id` first and otherwise `str(replay_root_event_id(envelope))`; it never falls back to the changing replay transport ID. Under workspace/deletion serialization it resolves the deterministic target, workflow name, and fully typed workflow input first, calls the neutral exact-key `serialize_trigger_workflow_input` validator, and calls `authorize_trigger_start(...)`. Serialization accepts only the matching stdlib dataclass (`TriggeredTaskInput` or nested `EngineeringTicketInput`), recursively rejects extra/wrong keys/types, validates all workspace/trigger/invocation/event/agent and nonempty implementer/QA/connection IDs as canonical UUID strings, and requires: trigger name/event type `1..200`, source `1..64`, external ID `1..500`, title `0..500`, description `0..10_000` after source-URL composition, URL `0..2_000`, strict booleans, and max-retest cycles `0..20`. Malformed template configuration is normalized to the existing default 3 before serialization. It uses `dataclasses.asdict` plus sorted compact canonical JSON and enforces `TRIGGER_INPUT_MAX_BYTES=20_000` UTF-8 bytes before persistence; deserialization performs the inverse explicit constructors and equality round trip. The transaction inserts the unique invocation or locks/reloads it and validates the complete immutable contract; an existing semantic invocation with any workflow/name/input/version mismatch is an invariant. No Temporal call precedes this commit.

Both the delivery path and an independent `reconcile_authorized_trigger_starts_batch` call only `ensure_trigger_workflow(temporal, start=authorized_start, timeout_seconds=5.0)`. The background batch claims at most 25 due authorized/unaccepted or accepted/nonterminal rows by stable `(start_reconcile_available_at,id)` with `FOR UPDATE SKIP LOCKED` and a 30-second token/lease, including rows whose NATS message was acknowledged and whose workspace/trigger/agent is now inactive or deleting; it performs no prospective gate after authorization. Unknown increments `start_reconcile_failure_count` saturating at 10, clears the claim, and indexes the closed `(1,2,4,8,15,30,60,120,300,600)` backoff by that count; accepted evidence resets it to zero, open history schedules a 30-second closure check, and terminal proof clears availability. This diagnostic/backoff counter never authorizes/exhausts a call. An expired claimant may resume, but every caller can use only the stored workflow contract. The helper selects the workflow implementation from the closed persisted name, validates the versioned input back through `TriggeredTaskInput` or `EngineeringTicketInput`, always supplies `task_queue=AGENT_TASK_QUEUE` and `WorkflowIDReusePolicy.REJECT_DUPLICATE`, and accepts `WorkflowAlreadyStartedError` for open or closed history. Any other client exception describes the exact ID: any returned history is accepted; NotFound re-drives the exact same persisted start; unavailable returns `unknown`. The authorization transaction appends the existing `trigger.invoked` audit exactly once with target trigger ID and bounded metadata `{status:"started",event_id,event_type,idempotency_key,workflow_id}`; row plus audit commit together. On acceptance, `reconcile_authorized_trigger_start` locks/reloads the same row, verifies that audit exists, and sets only `start_accepted_at` idempotently. Closure reconciliation records the closed status/terminal timestamp; the deletion inventory blocks on authorized `not_observed|unknown`, open history, or unpersisted terminal proof. NATS redelivery is therefore optional repair, never the only repair source. Only deterministic no-agent resolution before authorization writes a failed invocation with the existing fixed safe error and one failed audit.

`DurableEventConsumer.handle` parses metadata/envelope, validates the exact subject/envelope binding, obtains the deletion-gated claim, and acts on it. A `deleting|blocked` gate turns an unclaimed row directly into terminal `workspace_deleted` quarantine without calling business code; `DELETED_WORKSPACE` bypasses quarantine and calls `Msg.term()` directly, with no row/audit/outbox and no ack. A `HANDLE` runs inside `ProcessingLeaseKeeper`, and deletion waits for that exact renewable token to commit success/failure/quarantine; there is no connection-scoped advisory lock. It catches business exceptions, records their durable failure transition, and only then naks. Its required `QuarantineWriter` makes construction type-complete. This commit wires both production consumers in `main.py` with `defer_quarantine`, which raises the fixed `QuarantineDeferredError` so terminal messages remain `quarantine_only` and are nacked without handler re-entry until Task 4 replaces the bridge with `commit_quarantine`. It also runs the authorized-trigger reconciler beside the consumers so accepted-start repair does not depend on another NATS delivery. The service is runnable after this commit; no import references a future file. Ack/term failures are allowed to escape to telemetry-aware `run_pull_consumer`, whose fallback nak is best effort. Do not catch `BaseException`; cancellation must propagate.

Preserve the merged telemetry interfaces exactly: `run_pull_consumer` still flows through `dispatch_message`, trace headers/context are unchanged, both handler adapters retain `bind_context`, `main.py` still initializes/shuts down the same observability runtime and consumer-lag poller, and `services/event_worker/tests/test_telemetry.py` plus all `packages/events/tests/test_telemetry.py` cases remain green.

Change `ensure_pull_consumer` default to `max_deliver=-1`, reject `0` or values below `-1`, and converge existing consumer config with `update_consumer` so Phase 9 consumers stop at neither delivery 5 nor an old configured limit. `run_pull_consumer` continues to provide a last-resort nak on an unexpected handler exception but never counts an attempt.

Add `publish_to_subject` with subject/message-id validation (`1..500` subject, `1..200` ID) and exact `Nats-Msg-Id`; `publish` delegates to it. Preserve the predecessor signature's keyword-only `headers: Mapping[str,str] | None = None` on both methods. Merge caller headers through the existing telemetry publisher, never replace W3C propagation, and prevent callers from overriding the deterministic message ID. Add the optional replay field to the frozen envelope and round-trip tests.

- [ ] **Step 6: Run GREEN and affected event-worker suites**

```bash
uv run pytest packages/events/tests packages/recovery/tests/test_trigger_start.py services/event_worker/tests/test_delivery.py services/event_worker/tests/test_normalizer.py services/event_worker/tests/test_matcher.py apps/api/tests/test_webhooks_unit.py -q
uv run pytest services/event_worker/tests/test_telemetry.py -q
uv run pytest -m integration tests/integration/test_phase10_processing_claim.py -q
uv run ruff check packages/events services/event_worker
uv run mypy packages/events/src services/event_worker/src
```

- [ ] **Step 7: Commit exact scope**

```bash
git add packages/events/src/jhin_events/envelope.py packages/events/src/jhin_events/consumer.py packages/events/src/jhin_events/publisher.py packages/events/src/jhin_events/replay.py packages/events/tests/test_envelope.py packages/events/tests/test_consumer.py packages/events/tests/test_publisher.py packages/events/tests/test_replay.py packages/events/tests/test_telemetry.py packages/recovery/src/jhin_recovery/trigger_start.py packages/recovery/src/jhin_recovery/deletion.py packages/recovery/tests/test_trigger_start.py services/event_worker/src/jhin_event_worker/delivery.py services/event_worker/src/jhin_event_worker/failures.py services/event_worker/src/jhin_event_worker/processor.py services/event_worker/src/jhin_event_worker/normalizer.py services/event_worker/src/jhin_event_worker/matcher.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/src/jhin_event_worker/settings.py services/event_worker/tests/conftest.py services/event_worker/tests/test_delivery.py services/event_worker/tests/test_normalizer.py services/event_worker/tests/test_matcher.py services/event_worker/tests/test_telemetry.py apps/api/tests/test_webhooks_unit.py tests/integration/test_phase10_processing_claim.py
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: persist event handler attempts"
```

---

### Task 4: Commit Atomic Quarantine, Durable Deletion Drain, and Outbox-Only Dispatch

**Files:**
- Create: `services/event_worker/src/jhin_event_worker/quarantine.py`
- Create: `services/event_worker/src/jhin_event_worker/commands.py`
- Modify: `services/event_worker/src/jhin_event_worker/delivery.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Create: `services/event_worker/tests/test_quarantine.py`
- Create: `services/event_worker/tests/test_commands.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `packages/recovery/src/jhin_recovery/deletion.py`
- Modify: `packages/recovery/src/jhin_recovery/nats.py`
- Create: `packages/recovery/tests/test_deletion.py`
- Create: `packages/recovery/tests/test_nats_reconciliation.py`
- Modify: `apps/api/src/jhin_api/workspaces/service.py`
- Modify: `apps/api/src/jhin_api/workspaces/router.py`
- Modify: `apps/api/src/jhin_api/workspaces/schemas.py`
- Modify: `apps/api/src/jhin_api/deps.py`
- Modify: `apps/api/src/jhin_api/agents/service.py`
- Modify: `apps/api/src/jhin_api/tasks/service.py`
- Modify: `apps/api/src/jhin_api/tasks/router.py`
- Modify: `apps/api/src/jhin_api/approvals/service.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Create: `apps/api/tests/test_workspace_recovery_delete.py`
- Create: `apps/api/tests/test_task_workflow_deletion_gate.py`
- Modify: `apps/api/tests/test_approvals_unit.py`
- Create: `tests/integration/test_phase10_workspace_deletion.py`

**Interfaces:**
- Consumes: Tasks 2-3 recovery rows/state driver, audit model, telemetry-core `publish_jetstream`, durable deletion/package skeleton, and replay envelope publication.
- Produces: `commit_quarantine`, deletion-surviving recovery/deletion-gate transitions, ordinary task-start authorization/reconciliation and complete workflow inventory, Agent-delete/snapshot serialization with proof-preserving rejection, at-least-once trace-aware DLQ payload contract, outbox-only sequence-evidence reconciliation, and runnable production wiring. Replay/task-retry dispatch are not referenced until their own RED tasks.

- [ ] **Step 1: Write failing atomicity, redaction, stable-identity, deletion, and crash-gap tests**

Test one fifth-failure sequence with an injected session `before_commit` hook that raises once. Assert the first quarantine transaction leaves `quarantine_only` and zero failure/outbox/audit rows; the next call commits exactly one of each and `completed`. Reinvoke and assert the same IDs/counts. Inject a failure after DB commit but before `Msg.term`; redelivery must call no business handler and term.

Carry Task 3's binding-mismatch fixture through the now-real quarantine sinks. The stored processing source, failure subject, outbox payload, audit metadata, structured logs/spans/metrics, and published DLQ bytes must contain exactly `INVALID_SUBJECT_SENTINEL`; seed one otherwise-valid foreign workspace/suffix canary and scan every available sink to prove it appears nowhere. This RED is written before `commit_quarantine`/outbox construction, so no earlier commit needs to import a future payload builder. Task 5 adds the admin API projection assertion when that endpoint exists.

Outbox tests first read at most `OUTBOX_CANDIDATE_SCAN_LIMIT=100` ordered due IDs without `FOR UPDATE`, then prove each selected claim acquires `Workspace -> RecoveryWorkspaceDeletion -> OperationsOutbox` locks and revalidates due/status/workspace/context before mutation. Instrument SQLAlchemy lock acquisition and fail if command lock precedes either product/gate lock. A live publishing claim is skipped; an expired publishing claim below cap becomes `RECONCILE_EXISTING` without increment. Once a deletion gate commits, assert the ordinary null-context batch skips the workspace, the exact deletion-drain context can claim only its already-committed DLQ outbox, a different workspace/drain ID cannot, and deletion alone neither increments nor fails a pending row. Exercise two different pause barriers, after the immutable authorization/baseline commit and immediately before the NATS await: expire the lease, let a new claimant scan and re-drive the same `message_id`, then resume the old caller. Run below cap, at count 20, and with workspace deletion already draining. `accepted` finalizes; `not_observed` and `unknown` never become exhausted after authorization and may only publish the identical subject/payload/message ID. Assert attempt count stays fixed, downstream durable receipt applies once, transport delivery may be more than one, and deletion cannot cascade before the receipt/accepted finalization. `PubAck.duplicate` finalizes successfully.

Write these tests before implementation against the telemetry publisher and sequence-evidence interfaces: producer span/header/payload exclusion; accepted/not-observed/unknown scans; and all three attempt-20 crash points plus old-caller resume. Their initial failure must be the missing recovery implementation/direct-publish behavior, not a skipped assertion.

Make command-attempt accounting observable and reachable in these tests. A claimed outbox row whose baseline lookup fails with typed `nats_unavailable` calls `record_preauthorization_failure` once under the claim token and returns to pending/backoff; a database failure before any writable transaction consumes zero. Repeat this proven preauthorization transition 20 times and assert the null-authorized row can become exhausted with no publish. Separately let baseline capture return `0` for an empty DLQ stream, authorize as attempt 20, and assert same-message reconciliation remains legal forever without count 21. Concurrency wait, malformed persisted payload, and other nonretryable validation do not consume an attempt.

Advance the fake clock by 121 seconds after an accepted publish/finalization crash and assert reconciliation may produce a second JetStream delivery with the same header/message ID. The contract is at-least-once, not unique. The test downstream creates `dlq_test_receipt(message_id varchar(200) primary key, applied_at timestamptz not null)`, applies each received notification with `INSERT ... ON CONFLICT (message_id) DO NOTHING RETURNING message_id`, and asserts one returned/applied row. Jhin's failure/outbox/audit counts also remain one. This table exists only in the disposable test database and is dropped by fixture teardown; production promises the stable ID, not an undeclared inbox service.

Workspace deletion tests seed a renewable live processing claim, an expired processing claim, `quarantine_only`, requested/dispatching replay and task-retry rows, pending/publishing outbox, ordinary queued tasks, an authorized ordinary start, open/terminal/stale `AgentRun` bindings, and a started `TriggerInvocation.workflow_id` whose task activity has not run yet. The first DELETE transaction locks workspace then deletion row, archives the workspace, creates/reloads one `deleting` intent, audits `workspace.deletion_requested`, and returns the bounded in-progress projection; it does not invalidate a live handler token, terminalize an authorized/outcome-unknown publish/start, or hard-delete the workspace. New processing/replay/retry claims and every new ordinary task create/start/assign/message/instruction/pause/resume/cancel/approval-signal authorization are barred immediately. An injected rollback preserves workspace and all prior states; rerun is idempotent.

Deletion-drain tests prove: expired claims are reconciled through their normal deterministic authority; a live handler may renew and finish; `quarantine_only` commits normally; only never-authorized requested commands may terminalize prospectively; and authorized commands remain same-identity reconcilable on absence. Inventory folds the canonical `task-{task_id}`, persisted `Task.temporal_workflow_id`, every exact `delegated-{task_id}` wrapper implied by a validated delegated-task metadata binding, and every bounded `AgentRun.temporal_workflow_id/status` without losing any task/run linkage. It describes each exact ID with a five-second timeout; an open workflow, unavailable/NotFound without previously persisted actual-close proof, nonterminal run, missing projection, overflow, or conflicting binding keeps deletion draining. A delegated wrapper may be NotFound only after its already-authorized parent workflow is terminal and the child Task has no start authorization/run; an open/outcome-unknown parent can still resume its child-start call, so deletion waits. Closed Temporal history plus an exact terminal run is evidence from which the post-close reconciler may atomically persist proof; it is not itself deletion authority before that commit. After Temporal retention, NotFound is terminal only when the same ordinary/retry terminal proof was already persisted from actual closed history. NotFound or a matching Task/AgentRun projection alone never does. Then—and only after every processing row is completed, every accepted ordinary/trigger start has persisted closed proof, and every replay/retry/outbox row has terminal accepted proof—deletion resolves inaccessible failures, appends exact audits once, cascades product rows, and sets the plain-ID deletion row `deleted`. A database connection termination cannot erase intent because no correctness fence is session advisory state.

Write `test_task_workflow_deletion_gate.py` before production edits. With controlled session/Temporal barriers it must observe RED for: assigned `POST /tasks`, `/agents/{id}/assign-task`, and `/agents/{id}/message` racing DELETE; an unassigned task create after deleting; a start accepted followed by client/DB-finalize ambiguity; pause/resume/cancel/instruction and a new assignment/message after deleting; an already-active ordinary workflow performing its existing tool effects while cascade waits; and eventual close + post-close proof allowing deletion.

Parameterize the ordinary acceptance-finalize crash over workflow projection `running` and terminal Task `completed|failed`. Commit a version-1 authorized, unaccepted, uncleared ordinary row and its due start-repair authority; let Temporal accept the exact start, crash before `temporal_start_accepted_at`, then let snapshot/final projection mutate Task state, assignment, description, and metadata before repair. `reconcile_due_authorized_ordinary_starts_batch` must still select that row by start-repair columns alone, describe the exact open/closed history, and atomically mark accepted, clear start-repair state, and schedule terminal reconciliation. For the terminal case, the next post-close pass validates history, persists proof, and clears input; workspace deletion remains draining between projection and those two later commits, then converges. A queued manual-retry-shaped metadata object is included as a hostile mutable projection: it cannot suppress ordinary repair or substitute the retry ID/input, and the manual dispatcher remains blocked until ordinary proof.

Split immutable-input drift from agent deletion. In the live-agent case, pause after version-1 authorization, mutate Task description and assignment to a second live Agent while leaving the authorized Agent present, expire/reconcile, then resume both callers; every start uses byte-equal stored input and the single `AgentRun.agent_id` is the authorized Agent from that input. In a distinct deletion-first case, acquire `Workspace -> RecoveryWorkspaceDeletion -> Agent` in `delete_agent`, commit Agent deletion before snapshot obtains those locks, then resume the already-authorized callers. They still submit byte-equal input and one workflow history, but snapshot admission observes the missing Agent, fails safely, the final projection sets only Task failed with `snapshot_failed`, and **zero AgentRun rows** exist. It must leave input present, proof null, and the acceptance-created terminal reconciliation due. Only after the workflow actually closes may the post-close reconciler inspect bounded history, prove no successful snapshot/run or step/tool/delegation execution, write `closed_before_run` with the actual Temporal close status, and atomically clear input.

Add the opposite lock-winner unit case. Snapshot holds the same lock prefix through AgentRun insert/commit; `delete_agent` waits, then `evaluate_agent_deletion_recovery` observes the ordinary or seeded manual retry run without durable post-close proof and returns fixed 409 `Agent has workflow recovery in progress`. Assert the Agent and run remain, `agent.deleted` was not audited, and no cascade occurred. Terminal Task/AgentRun projection alone still rejects. After matching actual-close proof/input-clear commits, the same DELETE succeeds once, emits the existing sanitized audit once, and may cascade the now-redundant run. An active legacy run or conflicting run/start binding also rejects; a terminal legacy row with no Phase-10 start authority retains existing deletion behavior. A delayed workflow return after terminal Task/AgentRun projection and an unavailable/ambiguous post-close describe both keep input/proof and Agent/workspace deletion blocked; a later exact closed describe plus deterministic binding commits proof/clear once. No input enters audit/API/logs. Add the approval regression in `test_approvals_unit.py`: approve/reject after deleting is barred before its Temporal signal, while a decision transaction authorized before deletion may finish and deletion still waits for the exact workflow. Assertions use fixed 409 copy and no workspace existence leakage. The next paragraph owns the corresponding real-PostgreSQL serialization evidence.

Put the real database races in `tests/integration/test_phase10_workspace_deletion.py` and run them in this task before implementation. Use PostgreSQL barriers and `pg_terminate_backend`: ordinary start authorization commits, caller pauses before `start_workflow`, deletion marks `deleting`, reconciler describes NotFound and re-drives exact ID under `REJECT_DUPLICATE`, old caller resumes, and only one workflow exists. Separately let Temporal accept, block the accepted-at commit, and allow the real workflow to create a run and project Task running/terminal before repair. Despite that mutable state, the due version-1 unaccepted scan must repair acceptance, then post-close proof/input-clear, and deletion must converge.

Race real Agent DELETE against snapshot immediately before AgentRun insert using the canonical `Workspace -> RecoveryWorkspaceDeletion -> Agent` barriers. In the deletion-first branch, commit Agent deletion, resume snapshot, and assert byte-identical history input, safe failed Task projection, zero runs, and later post-close `closed_before_run` proof/clear. In the snapshot-first branch, hold the Agent lock through run insert/commit, release DELETE, and assert exact 409, preserved Agent/run, and zero delete audit/cascade until actual workflow close plus post-close proof. Retry DELETE after proof and assert one audit plus cascade. Inspect PostgreSQL lock waits/order and repeat backend termination on each side; a lost connection cannot invert the winner or erase the committed proof. Task 6 extends this same real barrier to a manual TaskRetry run before its implementation.

In the live-agent drift case assert one exact run. Pause after the final projection commits but before the workflow-task completion is acknowledged: deletion and the terminal reconciler must observe open/unknown, preserve input, and refuse cascade. Terminate the post-close reconciler's PostgreSQL backend after closed describe but before proof commit, and separately make Temporal describe unavailable; retry must reload the exact binding, persist actual close status/proof/clear once, and only then let deletion continue. Race assigned/unassigned task create, assign, message/signal/instruction/pause/resume/cancel, approval resolution, and the already-authorized trigger-start paths against DELETE. Pause an outbox dispatcher after its read-only candidate scan; race DELETE before the per-candidate locks. If deletion wins, the normal claimant skips without mutation and deletion-owned drain processes the same row; if claim wins the canonical locks, deletion observes/drains it. Assert PostgreSQL lock order from `pg_locks`/barriers and no outbox claim is born behind the gate. Pause an accepted parent run before its deterministic delegated-child start, let DELETE begin, then resume it; the restart-from-zero inventory must discover the newly committed child plus `delegated-{child_task_id}` wrapper, wait for parent/wrapper/nested task terminal histories, and never infer wrapper NotFound while the parent is open. A Task whose canonical/base ID collides with its persisted binding and one or more AgentRun IDs must retain all flags/linkages; conflicting two-run or wrapper-parent binding is an invariant and blocks cascade. No test treats NotFound of an authorized ID **alone** as permission to delete; add the distinct post-history-retention case where exact persisted terminal proof, previously obtained from actual closed history, prevents duplicate re-drive.

Use predecessor `NATS_URL`/`TEMPORAL_ADDRESS` directly for minimal real fixtures in this owning file. Real Temporal proves the ordinary accepted-start/finalize crash even after running/terminal projection, live-agent field drift with one run, both Agent-delete/snapshot lock winners, deleted-agent zero-run close, and post-close proof-gated input clearing; no activity claims the workflow is closed before its return. Real NATS publishes one stored outbox notification through the trace-aware helper, terminates the PostgreSQL claimant after PubAck/before finalize, and proves the next claimant finalizes or re-drives only the identical message ID/payload. Local fakes remain only for deterministic exception branches. After an actual deletion reaches `deleted` and product rows have cascaded, publish a real NATS event with that exact workspace and pull it through `DurableEventConsumer`: it must term, call no handler, and leave counts at zero for processing/failure/outbox/new audit while the `deleted` gate remains. Force the first term to fail, redeliver, and assert the same. Advance database time to the processing/source-retention boundary and prove the gate is still retained; its 90-day cleanup is Task 8's unit boundary. The file defines its own local bounded barriers/backend-PID lookup and unique NATS/Temporal IDs over existing integration fixtures; it cannot import helpers introduced only in Task 9. These REDs must fail on the pre-Phase-10 gate/lock/start gaps and are not postponed to Task 9; Task 9 remains the aggregate restart/greater-than-120-second gate.

```python
{
    "message_id", "failure_id", "workspace_id", "event_id", "correlation_id",
    "origin_stream", "consumer_name", "subject", "source_stream_sequence",
    "source_consumer_sequence", "delivery_count", "handler_attempt_count",
    "reason_code", "first_failed_at", "last_failed_at",
}
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/recovery/tests/test_deletion.py packages/recovery/tests/test_nats_reconciliation.py services/event_worker/tests/test_quarantine.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_delivery.py services/event_worker/tests/test_telemetry.py apps/api/tests/test_workspace_recovery_delete.py apps/api/tests/test_task_workflow_deletion_gate.py apps/api/tests/test_approvals_unit.py -q
uv run pytest -m integration tests/integration/test_phase10_workspace_deletion.py -q
```

- [ ] **Step 3: Implement one locked quarantine transaction**

`commit_quarantine` locks the processing row and requires `quarantine_only`. It looks up the unique failure key. If absent, insert the failure with current diagnostic counts and sanitized classification; if present, validate its workspace/event/correlation/subject/reason against the state/source and reuse it. Do the same for `(kind="event_failure_dlq", aggregate_id=failure.id)` outbox, with `message_id=f"event-failure:{failure.id}"`.

Only when inserting the failure, call the append-only audit service/model once with:

```python
AuditEvent(
    workspace_id=identity.workspace_id,
    actor_type="system",
    actor_id=None,
    action="event.processing_failed",
    target_type="event_processing_failure",
    target_id=failure.id,
    metadata_json={
        "origin_stream": source.origin_stream.value,
        "consumer_name": source.consumer_name,
        "source_stream_sequence": source.source_stream_sequence,
        "handler_attempt_count": state.handler_attempt_count,
        "reason_code": failure.reason_code,
    },
)
```

Atomicity means an existing failure implies its audit was committed; validate that exact audit exists before completing a reused row, otherwise raise `QuarantineInvariantError`. Finally set state `completed`, preserve terminal `last_reason_code`, clear claim fields, and commit once. No network call occurs inside this transaction.

- [ ] **Step 4: Implement durable, idempotent workspace-deletion intent and drain**

Add `WORKSPACE_DELETE_BATCH_SIZE=500`, `WORKSPACE_DELETE_DUE_BATCH_SIZE=25`, `WORKSPACE_DELETE_CALL_TIMEOUT_SECONDS=5`, and bounded exponential backoff capped at 600 seconds. `DELETE /workspaces/{id}` locks the `Workspace` row first, then inserts/locks `RecoveryWorkspaceDeletion`; it changes workspace status to the existing `archived` value, records `workspace.deletion_requested`, commits, and returns `202` with `{workspace_id,status:"deleting",requested_at}`. Repeating as the same owner returns that projection; another actor follows existing 404/authorization rules. There is no synchronous recovery-count rejection and no connection-scoped advisory lock. `reconcile_due_workspace_deletions_batch` claims at most 25 due deletion IDs in stable `(available_at,workspace_id)` order, invokes the single-workspace drain with the same five-second external-call bounds, and leaves each row due/backed off according to that drain's returned state.

Change the route annotation to `status_code=202, response_model=WorkspaceDeletionOut`; `WorkspaceDeletionOut` is `ConfigDict(extra="forbid")` with `workspace_id: UUID`, `status: Literal["deleting","deleted","blocked"]`, and `requested_at: AwareDatetime`. This is the only deletion response shape. Tests assert exact 202 JSON and existing CSRF/owner/nonmember projections; do not leave the former `204 -> None` annotation.

The first request appends exactly one `workspace.deletion_requested` audit with target workspace ID and metadata `{deletion_status:"deleting"}`; an idempotent repeat adds none. Final cascade appends the existing `workspace.deleted` exactly once with its existing sanitized `{name,slug}` metadata plus no recovery counts/IDs/errors. Each deletion-resolved event failure uses the existing `event.failure_resolved` action and exact metadata `{reason_code, resolution_reason:"workspace_deleted"}`. Each never-authorized replay/task prospective terminal transition uses the Task 5/6 exact failed/rejected audit shape; deletion does not invent a second action or include claim tokens, source bytes, requester IDs, external errors, or workflow headers.

`reconcile_workspace_deletion` claims a due deletion row with token/lease and processes at most 500 stable-key rows. It never changes a live `handling` claim and waits while its renewable lease exists. Once such a lease expires, normal processing redelivery must reconcile/complete it; deletion itself does not invent handler success or failure. It invokes the same outbox/replay/task/ordinary-start/post-close reconciliation functions used by dispatchers, with five-second NATS/Temporal calls, before examining current authorization, workspace state, source expiry, or configuration. A row with `external_call_authorized_at` remains dispatching/draining on `not_observed`, NotFound, or unknown and may only be re-driven with its exact immutable identity. An ordinary task with `temporal_start_authorized_at` follows the same rule until its separate post-close reconciler has persisted actual close status, exact binding proof, and input clear; a terminal Task/AgentRun projection—even after Temporal history retention—never independently proves closure. Neither path is terminalized `workspace_inactive` or exhausted. Never-authorized replay/task-retry requests are prospective and may terminalize directly, but a committed `event_failure_dlq` outbox is an obligatory notification intent rather than new product work: only the deletion-owned drain may take its lease after `deleting`, capture/commit its immutable baseline authorization, and publish the existing `message_id` contract. It may not drop or mark that outbox failed merely because the workspace is deleting. Every inaccessible `open|replay_requested` failure is finally set `resolved`, `resolved_at`, and audited with `resolution_reason="workspace_deleted"`, so the 90-day retention job can purge it.

After a batch sees no live processing lease, no `handling|quarantine_only` state, no nonterminal/outcome-unknown replay/retry/outbox, and no ordinary open/authorized-NotFound/unknown workflow, one transaction locks workspace/deletion again and reruns the inventory from the beginning. `collect_workspace_ordinary_workflows` independently keyset-pages Tasks, AgentRuns, and TriggerInvocations by UUID, at most `MAX_ORDINARY_SOURCE_ROWS_PER_PAGE=500` of each per call, returning all three cursors/completion flags and at most `MAX_ORDINARY_WORKFLOW_IDS_PER_PAGE=2_500` merged IDs (three per Task plus one per run/trigger). Thus it includes the synthesized base and immutable ordinary Task binding for every task, the deterministic `delegated-{task_id}` wrapper for an exact validated delegation metadata object, **every AgentRun workflow ID/status including a run with null task linkage, and every authorized TriggerInvocation workflow ID/status/name/input contract including one whose task activity has not linked/created a Task yet**. Manual-retry queue metadata is never interpreted as an ordinary Task binding. A delegated wrapper is an already-accepted parent-workflow continuation, not permission for an API caller to create work after `deleting`; deletion describes it and its parent but never starts/cancels it. The final locked restart-from-zero scan catches a child Task committed by a caller that was already authorized before deletion. It validates ASCII IDs `1..200`, merges each field instead of winner-by-query-order across pages, and blocks on an over-bound/conflicting page or cursor regression. An authorized ordinary Task may have its validated input JSON while reconciliation is pending, or complete immutable terminal proof plus `input_cleared_at`; missing both/conflicting shapes block, and only the latter proof shape permits final cascade. A terminal `AgentRun` means exactly `RunStatus.COMPLETED|FAILED|CANCELLED`, matching workspace/task/workflow where linkage exists, nonnull completion, and no competing run; it is evidence for the post-close reconciler, never closure proof by itself. `PENDING|RUNNING|PAUSED|WAITING_APPROVAL|WAITING_DELEGATION` blocks. An authorized trigger must be independently reconciled from its persisted name/input, accepted, closed, and have matching linked terminal projections when those exist; NotFound/unknown or a still-open pre-task trigger blocks. A `started` task-retry is likewise insufficient: before product cascade Task 6 must store its matching `terminal_proof_generation`, exact workflow status, clear its start-input JSON, and either persist its exact terminal run linkage or the bounded `closed_before_run` history proof on TaskRetry. Only after all three inventory cursors report complete, every accepted ordinary start has post-close proof/input-clear, and a final locked empty/nonterminal recheck succeeds may it append `workspace.deleted`, hard-delete product rows, and set deletion `deleted/deleted_at`. Recovery rows, terminal retry proof, and the deletion row survive by plain UUID. The event-worker loop runs authorized-trigger, ordinary-start, ordinary-terminal, and outbox reconciliation before deletion drain in this task; replay/task-retry start/terminal batches are added only in Tasks 5/6 after their RED. Until then, seeded future commands keep deletion `deleting` rather than importing future code.

Add `OperationalMemberCtx` in `apps/api/src/jhin_api/deps.py`: it performs existing membership/role hiding, then calls neutral `lock_workspace_claim_gate` in the request's `DbSession` and returns fixed 409 `Workspace is being deleted` for inactive/deleting/deleted/blocked. Use it on every mutating route in `tasks/router.py`; reads keep existing contexts. This dependency is an early projection only. `tasks/service.py` must retake/validate the same workspace -> deletion lock in the transaction that authorizes work so a direct service caller cannot bypass it. Approval routes retain `MemberCtx` because an idempotent retry may need to deliver a decision durably authorized before deletion; `approvals/service.py` distinguishes that repair from a new decision under the lock.

Replace API `_start_workflow` with a two-phase exact-ID/immutable-input handshake. In one transaction, `authorize_ordinary_task_start` locks `Workspace -> RecoveryWorkspaceDeletion -> Agent -> Task`, validates the committed agent and instruction length `0..20_000`, constructs the complete `AgentTaskInput(workspace_id,task_id,agent_id,instruction,retry_id=None,attempt_number=1)`, validates/canonicalizes it through `serialize_agent_task_input`, and writes `Task.temporal_workflow_id = f"task-{task.id}"`, contract version 1, bounded JSON, `temporal_start_authorized_at=now`, and due start-repair state together. It commits before returning `AuthorizedTaskStart` loaded through the inverse validator. An existing authorization is idempotent only when the requested values serialize byte-equal; otherwise the caller does not rewrite it and must use `load_authorized_ordinary_task_start`. Every initial caller and event-worker reconciler passes only `authorized.workflow_input` to Temporal with `WorkflowIDReusePolicy.REJECT_DUPLICATE`; no re-drive reads current assigned agent/description or reconstructs input. Already-started, including closed history, is accepted. Any ambiguous error describes the same ID; history is accepted. On NotFound, first lock/reload exact task/run bindings: matching terminal proof is accepted; otherwise re-drive only the same stored contract. Agent deletion/SET NULL, direct product mutation, workflow projection, revocation, and deletion intent after authorization cannot change or suppress the call. Assigned create, assign-task, and agent-message create the Task/message/audit and authorization atomically; an unassigned create still locks/gates but has no authorization. User-facing endpoints continue to forbid post-authorization mutation, but correctness does not depend on that projection.

`reconcile_due_authorized_ordinary_starts_batch` claims at most 25 rows ordered by `(temporal_start_reconcile_available_at,id)` where the persisted shape is exactly contract version 1, authorized, unaccepted, input present/uncleared, and start-repair due or lease-expired. It uses a 30-second token/lease and does **not** filter on Task state, assigned Agent, description, metadata, workspace activity/deletion, or presence of a manual queue projection. After authorization all prospective gates are behind the fence. Each claimant loads only the immutable ordinary ID/input. `WorkflowAlreadyStartedError` or any described open/closed history is accepted; NotFound re-drives that same input with `REJECT_DUPLICATE`; unavailable/ambiguous clears the claim, increments the diagnostic count saturating at 10, and schedules `(1,2,4,8,15,30,60,120,300,600)` seconds. A stale claimant can resume only the same ID/input. Acceptance locks/reloads by Task ID and claim token, revalidates version/input/workflow binding, sets `temporal_start_accepted_at`, clears all start-repair fields/count, and makes terminal reconciliation due in one commit. Equal accepted repair is a no-op. Thus a workflow that already projected Task running or terminal before accepted-at repair remains discoverable and immediately proceeds to post-close proof/input clear.

Temporal acceptance writes `Task.temporal_start_accepted_at` idempotently and makes `temporal_terminal_reconcile_available_at` due. The workflow's final projection activity atomically sets only terminal Task/AgentRun product state; it does not mutate reconcile due/claim fields, write `temporal_start_terminal_*`, claim an actual Temporal close status, or clear `temporal_start_input_json`. The activity necessarily runs before the workflow returns, so a crash after that projection or a delayed workflow-task completion leaves the immutable input present and deletion draining until the already-scheduled post-close pass observes closure.

`reconcile_ordinary_task_start_terminal` is the only post-close path. Its batch claims at most 25 accepted, unproven Tasks ordered by `(temporal_terminal_reconcile_available_at,id)` using the paired 30-second token/lease. It describes the exact persisted workflow ID with a five-second timeout, independently of mutable Task/Agent/workspace state. Open history clears the claim and schedules a 30-second check; unavailable or ambiguous history increments the diagnostic failure count, saturating at 9, and schedules `(2,4,8,15,30,60,120,300,600)` seconds; NotFound after acceptance stays `unknown` and never proves closure. For an allowlisted closed status, it reads the execution-start event, requires workflow type `AgentTaskWorkflow`, deserializes/recanonicalizes its input, and requires byte equality with the stored version-1 `AgentTaskInput` plus exact workspace/task/workflow/ordinary-attempt binding. It then requires either an exact matching terminal `AgentRun`, or keyset-pages at most `ORDINARY_START_TERMINAL_HISTORY_MAX_EVENTS=10_000` events to prove `closed_before_run`: no successful `resolve_snapshot_activity`, no created run, and no step/tool/delegation activity. A caught snapshot failure can therefore yield product Task `failed` while the Temporal close status is `completed`; the two statuses are intentionally not equated.

Only after that external observation does `record_ordinary_task_start_terminal_proof` lock/reload Task plus exact run evidence under the still-current reconcile token, revalidate the immutable workflow/input binding, and atomically write actual close status/outcome/run proof, null `temporal_start_input_json`, set `temporal_start_input_cleared_at`, clear due/claim, and reset the diagnostic count. Equal proof is an idempotent no-op; a conflicting proof, stale token, open/unavailable/NotFound history, truncated history, or clear without proof is an invariant/unknown result and preserves input. If PostgreSQL disconnects after describe but before commit, the next claim repeats the same describe and proof; if Temporal is unavailable after projection, it keeps retrying. `reconcile_workspace_deletion` invokes this same helper and cannot cascade until its later proof-and-clear commit succeeds.

Change `resolve_snapshot_activity`'s ordinary path in this task so its transaction locks `Workspace -> RecoveryWorkspaceDeletion -> Agent` before Task/run lookup or insert and holds the Agent lock through snapshot/AgentRun commit. A preauthorized start may continue while workspace deletion is draining, but it must validate that immutable authorization; the gate is serialization, not a new prospective rejection. If Agent deletion committed first, the locked Agent lookup is absent and the existing safe snapshot-failure/final Task projection creates no run. Task 6 extends this same prefix to manual retry admission rather than adding a second lock order.

Change `delete_agent` to call `lock_workspace_claim_gate`, require an active/nondeleting workspace, lock the exact Agent, then call `evaluate_agent_deletion_recovery` before audit/delete. The evaluator uses bounded `EXISTS` queries while the Agent lock prevents a new AgentRun insert. Any nonterminal run blocks. For a terminal Phase-10 ordinary run, safety requires exact Task/workflow/run linkage plus complete `terminal_run` post-close proof/input clear; for a terminal manual run it requires exact TaskRetry attempt/workflow/new-run linkage plus its durable `terminal_run` proof/input clear. A run with mismatched linkage is `invariant` and blocks. `closed_before_run` has no run to protect. A terminal legacy run with no Phase-10 authorization keeps predecessor delete behavior. `active_run|ordinary_proof_pending|task_retry_proof_pending|invariant` all collapse to fixed HTTP 409 `Agent has workflow recovery in progress`, with no IDs/status detail and no `agent.deleted` audit. After proof is durable, the existing sanitized delete audit and cascade commit together. This is deliberately a retryable rejection, not background cancellation or deletion intent.

For pause/resume/cancel, keep the workspace/deletion row locks in the same request transaction until the bounded five-second signal returns and its audit commits. `send_instruction` locks/gates and commits the exact Message before signaling as today; that committed message is the pre-deletion authorization, so a paused caller may finish that exact signal but a new instruction request after `deleting` is barred. Approval resolution first locks workspace -> deletion -> approval: a new decision is barred once deleting; otherwise it commits decision/audit before signaling as today. An idempotent retry of the **same already-committed** decision may repair its exact signal while deleting, but a different/new decision cannot. DELETE waits on the lock and then inventories the already-open workflow. If Temporal accepted a signal, deletion cannot cascade while workflow history/run outcome remains open or unknown. Do not claim signal cancellation, general signal idempotency, or that deletion can retract it. The neutral package exposes gates/inventory/start reconciliation only and imports no API/service module.

- [ ] **Step 5: Implement outbox claims and publication reconciliation**

`claim_due_outbox` performs two phases. Phase A is a read-only query for at most `OUTBOX_CANDIDATE_SCAN_LIMIT=100` IDs/workspace IDs whose pending `available_at <= now` or publishing lease expired, ordered by `(available_at,id)`; it takes no row lock and mutates nothing. Phase B processes candidates until 25 claims are returned. For a nonnull workspace it opens one short transaction, selects the exact `Workspace FOR UPDATE` (absence allowed only with a locked recovery gate), then `RecoveryWorkspaceDeletion FOR UPDATE`, then the exact `OperationsOutbox FOR UPDATE SKIP LOCKED`; for null workspace it locks only the outbox because no workspace authority exists. It reloads/revalidates ID, workspace, kind, due/status/lease, attempt/authorization shape, and `deletion_drain_workspace_id` before mutation. The ordinary loop passes null and skips inactive/any deletion gate; the deletion drain passes the exact locked deletion-row workspace ID and may select only that deleting workspace's pre-existing outbox. A disappeared, changed, newly live-claimed, wrong-context, or already-terminal candidate is skipped, never updated from the stale Phase-A snapshot. No code path locks outbox before workspace/gate. A never-authorized pending row below cap becomes `publishing` and receives a token/expiry/`NEW_ATTEMPT` without increment. Expired publishing receives `RECONCILE_EXISTING` below cap or `RECONCILE_AT_CAP` at 20, also without increment. Pending at count 20 is terminal only after 20 proven typed preauthorization failures; deleting does not itself exhaust it. Any authorized-at-cap shape remains reconcilable. A typed transient baseline/source dependency error after the durable claim calls `record_preauthorization_failure` under the exact token, increments once, clears the claim, and schedules bounded backoff; a transaction failure before commit increments zero. No other branch increments. After prospective checks, obtain the read-only DLQ stream baseline (zero is valid for an empty stream), then `authorize_external_call` locks in the same workspace -> gate -> outbox order by ID/token, increments once (maximum 20), stores immutable `external_call_authorized_at`, baseline, and exact message identity, and commits. It returns the immutable `AuthorizedExternalCall`; every old or new caller may publish only from that value, so a stolen lease cannot authorize a different call. `publish_operations_message` validates subject length/form, payload length `1..4096`, and message ID length `1..200`, then delegates exactly to `jhin_events.telemetry.publish_jetstream(js, subject, payload, message_id=message_id, stream="DLQ")`; direct `js.publish` is forbidden.

Outbox dispatch uses `dlq_subject(origin_stream)`, canonical compact JSON bytes from the stored bounded object, and the exact outbox `message_id`, which appears both as payload field and `Nats-Msg-Id` header. On every authorized `publishing` claim—including count 20—it scans only `(dispatch_baseline_sequence,current_last_sequence]`, at most 10,000 direct-get sequence slots, and validates exact stream/subject/header/payload message ID. Found means accepted and finalizes without any current workspace/deletion gate. A complete miss is named `not_observed`, not deterministic absence: call `publish_operations_message` again with the identical stored subject/payload/message ID and finalize on PubAck, without incrementing. An unavailable, aged/truncated, or over-bound scan is `unknown`; once NATS accepts calls, the reconciler still re-drives that same immutable publish and finalizes its PubAck—scan availability is an optimization, not a terminal-proof prerequisite. Publish failure keeps `publishing` with bounded backoff. At count 20 the same rules apply. The crash/race tests place barriers (a) after authorization/baseline commit before publish, then new re-drive and old resume, (b) after accepted publish before DB finalize, and (c) after finalize commit before return, below and at cap and during deletion.

Telemetry tests start a parent span and assert a `nats.publish` producer child with closed `{messaging.system:"nats",jhin.stream:"DLQ",jhin.subject_family:"dlq"}` attributes, W3C `traceparent`, stable `Nats-Msg-Id`, no baggage, and no payload/subject workspace token/failure detail in span name, attributes, events, headers other than the allowed ID, or logs. Existing telemetry consumer tests remain green. Finalization locks by ID/token and sets `published/published_at`; it is idempotent when the row is already published with that message ID. The 120-second server duplicate window is never asserted as unique delivery.

The outbox payload is constructed from typed fields before persistence and passed through `sanitize_payload`; if size still exceeds 4,096 or keys differ, quarantine raises and remains `quarantine_only` rather than truncating identifiers unpredictably.

- [ ] **Step 6: Replace the quarantine bridge and wire only ordinary-start plus outbox recovery**

Replace both Task 3 `defer_quarantine` injections with `functools.partial(commit_quarantine, session_factory)`. Start `OperationsCommandDispatcher.run(stop)` beside both pull loops and merged heartbeat/consumer-lag telemetry tasks. Move Task 3's standalone trigger runner into this dispatcher by making `reconcile_authorized_trigger_starts_batch` delegate to neutral `reconcile_due_trigger_starts_batch`; `main.py` removes the standalone task so there is one scheduled loop (concurrent calls would remain token-safe). At this commit the loop invokes authorized-trigger reconciliation, `reconcile_authorized_ordinary_starts_batch`, `reconcile_ordinary_task_start_terminals_batch`, `dispatch_outbox_batch`, then `reconcile_workspace_deletions_batch`; it has no replay/task-retry method import or query. The ordinary-start method delegates to neutral `reconcile_due_authorized_ordinary_starts_batch`, whose sole selection authority is the version-1 authorized/unaccepted/uncleared due-or-expired start-repair shape. It never reads Task state or queue metadata to suppress a row and describes/re-drives only the persisted ordinary ID/input under `REJECT_DUPLICATE`. The separate post-close batch handles only accepted, due, unproven ordinary starts and follows Step 4's describe/proof/input-clear contract. Each pass catches/logs one sanitized category error and the loop waits with `asyncio.wait_for(stop.wait(), timeout=settings.command_poll_seconds)`; default poll is 1.0 seconds, validated `0.1..60`. On shutdown, set stop, await loops, close NATS, shut down observability, and dispose the engine.

- [ ] **Step 7: Run GREEN**

```bash
uv run pytest packages/recovery/tests/test_deletion.py packages/recovery/tests/test_nats_reconciliation.py services/event_worker/tests/test_delivery.py services/event_worker/tests/test_quarantine.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_telemetry.py apps/api/tests/test_workspace_recovery_delete.py apps/api/tests/test_task_workflow_deletion_gate.py apps/api/tests/test_approvals_unit.py -q
uv run pytest -m integration tests/integration/test_phase10_workspace_deletion.py -q
uv run ruff check services/event_worker services/agent_worker/src/jhin_agent_worker/activities.py packages/recovery apps/api/src/jhin_api/workspaces apps/api/src/jhin_api/agents/service.py apps/api/src/jhin_api/tasks apps/api/src/jhin_api/approvals apps/api/src/jhin_api/deps.py apps/api/tests/test_workspace_recovery_delete.py apps/api/tests/test_task_workflow_deletion_gate.py
uv run mypy services/event_worker/src services/agent_worker/src/jhin_agent_worker/activities.py packages/recovery/src apps/api/src/jhin_api/workspaces apps/api/src/jhin_api/agents/service.py apps/api/src/jhin_api/tasks apps/api/src/jhin_api/approvals apps/api/src/jhin_api/deps.py
```

- [ ] **Step 8: Commit exact scope**

```bash
git add packages/recovery/src/jhin_recovery/deletion.py packages/recovery/src/jhin_recovery/nats.py packages/recovery/tests/test_deletion.py packages/recovery/tests/test_nats_reconciliation.py services/event_worker/src/jhin_event_worker/quarantine.py services/event_worker/src/jhin_event_worker/commands.py services/event_worker/src/jhin_event_worker/delivery.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/src/jhin_event_worker/settings.py services/event_worker/tests/test_quarantine.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_telemetry.py services/agent_worker/src/jhin_agent_worker/activities.py apps/api/src/jhin_api/workspaces/service.py apps/api/src/jhin_api/workspaces/router.py apps/api/src/jhin_api/workspaces/schemas.py apps/api/src/jhin_api/deps.py apps/api/src/jhin_api/agents/service.py apps/api/src/jhin_api/tasks/service.py apps/api/src/jhin_api/tasks/router.py apps/api/src/jhin_api/approvals/service.py apps/api/tests/test_workspace_recovery_delete.py apps/api/tests/test_task_workflow_deletion_gate.py apps/api/tests/test_approvals_unit.py tests/integration/test_phase10_workspace_deletion.py
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: atomically quarantine failed events"
```

---

### Task 5: Add Workspace-Admin Failure, Replay, Resolve, and History APIs

**Files:**
- Create: `apps/api/src/jhin_api/idempotency.py`
- Create: `apps/api/src/jhin_api/operations/__init__.py`
- Create: `apps/api/src/jhin_api/operations/router.py`
- Create: `apps/api/src/jhin_api/operations/schemas.py`
- Create: `apps/api/src/jhin_api/operations/service.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/tests/conftest.py`
- Create: `apps/api/tests/test_idempotency.py`
- Create: `apps/api/tests/test_event_failures.py`
- Modify: `services/event_worker/src/jhin_event_worker/commands.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/tests/test_commands.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `apps/api/src/jhin_api/health/schemas.py`
- Modify: `apps/api/src/jhin_api/health/service.py`
- Modify: `apps/api/tests/test_operations_health.py`
- Modify: `packages/recovery/src/jhin_recovery/replay.py`
- Modify: `packages/recovery/src/jhin_recovery/deletion.py`
- Create: `packages/recovery/tests/test_replay.py`
- Modify: `packages/recovery/tests/test_deletion.py`
- Modify: `packages/recovery/tests/test_import_boundaries.py`
- Create: `tests/integration/test_phase10_event_replay.py`

**Interfaces:**
- Consumes: Tasks 2/4 models/dispatcher, `AdminCtx`, CSRF, API JetStream dependency, safe redaction/audit, nats-py `get_msg(stream, seq=...)`.
- Produces: exact admin endpoints, bounded cursor/latest-history schemas, source-verified stable-key replay eligibility, race-idempotent replay/resolve, replay-only dispatch transitions, and bounded protected-health DLQ backlog.

- [ ] **Step 1: Write failing RBAC, pagination, validation, idempotency, source, and audit tests**

Exercise every endpoint as owner/admin/member/viewer/nonmember. Nonmember is 404; viewer/member is 403; only workspace-owned rows return. Test cursor order `(first_failed_at DESC,id DESC)` across equal timestamps and reject malformed cursors with 422. Filters are closed `status`, `origin_stream`, `reason_code`, and UTC `failed_from/failed_to`; `limit` is `1..100`.

Load Task 4's binding-mismatch failure and assert list/detail expose subject exactly `INVALID_SUBJECT_SENTINEL`; the otherwise-valid foreign workspace/suffix canary is absent from the complete serialized response. API construction may read only the persisted sentinel and cannot reconstruct or substitute the incoming subject.

Replay tests require CSRF and an `Idempotency-Key`; same key returns the same request ID, distinct key during a nonterminal request returns 409, and keys outside the Shared Interface regex return 400 without echoing the submitted value. Release two same-key requests simultaneously so both pass the optional fast read and source preflight. The winner inserts and changes the failure to `replay_requested`; after the loser obtains `Workspace -> deletion -> failure` locks, it must requery `(workspace_id,idempotency_key)` **before** checking that now-mutated failure status and return the existing row when failure/requester match. Repeat with source preflight reporting deleted/changed/unavailable after the winner commits: that result is deferred until the locked requery, so an exact existing binding still wins. A key bound to another failure or requester returns the fixed `idempotency_key_conflict` without leaking that row even if the target failure is otherwise eligible. Inject a unique violation after that locked requery to exercise the defensive rollback/new-transaction exact reload; it is not the primary concurrency algorithm. Source checks validate exact stream sequence, subject, event ID, workspace, and nonnull `replay_semantic_identity`. No API response contains message bytes/data or outbox payload.

Detail/reload tests seed 25 replay generations and assert `latest_replay` plus exactly the newest 20 `replay_history` rows in `(replay_generation DESC,id DESC)` order, with `replay_history_truncated=true`. Status, attempts, safe code, and timestamps update after dispatcher reload; requester/idempotency/raw error fields never appear.

Replay dispatcher tests cover the stale-caller window below cap and at attempt 20. Pause the old dispatcher after immutable replay authorization/baseline commit and again immediately before publish; expire its lease, let a new claimant scan and re-drive the exact registered replay event/root/semantic identity without increment, then resume the old caller. Assert the same replay event ID, root, subject, source identity, and handler semantic key on every transport; only one business effect is applied although transport is at-least-once. Repeat while workspace deletion drains. Cover accepted-publish-before-finalize and finalized-before-return. After authorization, a complete scan miss is only `not_observed`: revocation, deletion, or source expiry cannot terminalize or authorize a changed envelope. Unknown remains dispatching. Before authorization, current gates may still reject without publish.

For reachability, inject 20 separate typed preauthorization NATS failures after successful claims and assert `attempt_count` advances `0..20`, each token is consumed once, and only that null-authorized row becomes `dispatch_exhausted`; a DB failure before the claim transaction leaves zero. In a separate case let an empty destination stream return baseline `0`, authorize the first external call as attempt 20, and prove accepted/re-drive paths never increment to 21. Invalid source, permission failure, semantic-key failure, and source expiry are terminal prospective outcomes and do not pretend to be dispatch attempts.

Extend protected-health tests with zero, one, foreign-workspace, old, future, and `MAX_SAFE_COUNT+1` fixtures. Assert `MAX_COMPONENTS == 10`, the exact new `event_failures_open/review_event_failures` enum values, and the fixed zero/open/database-error component projections before implementation. The workspace summary counts only `open|replay_requested`, computes nonnegative floored oldest age from the minimum `first_failed_at`, clamps both values, and exposes no IDs/reason/detail. Any open row degrades the `event-failures` component; foreign/null-workspace rows do not count.

Add an import-boundary regression that parses API and event-worker imports: recovery codes/copy may come from `jhin_domain` only. Reject any `jhin_api` import from `jhin_event_worker` and any `jhin_event_worker` import from `jhin_api`.

Before dispatcher production wiring, extend `services/event_worker/tests/test_telemetry.py` so its typed recording dispatcher expects authorized-trigger → ordinary-start → ordinary-terminal → outbox → replay → deletion order, retains the existing consumer/lag/heartbeat/observability lifecycle, and proves replay publication still flows through the trace-aware helper with no payload span data. This is part of the Step 2 RED, not a fixture repair written after implementation.

Release a replay POST and workspace DELETE simultaneously after both have loaded the failure. Both must lock in the universal order `Workspace -> RecoveryWorkspaceDeletion -> EventProcessingFailure`. If replay wins, deletion observes and drains its durable command; if deletion intent wins, replay returns the fixed inactive conflict and creates no command. Add the real-PostgreSQL unique-race variant where two same-key POSTs and DELETE overlap: the loser reloads the exact binding under lock and at most one request row exists.

Place those real races in `tests/integration/test_phase10_event_replay.py` and run the file in this task before implementation. Import predecessor `NATS_URL`, connect a real nats-py client with a five-second bound, and create unique test-owned source/destination streams/consumers. Use real stream sequences for source validation, actual publish/PubAck/direct-get evidence, source deletion, and same-key API races; deterministic local wrappers may pause immediately around the real await but do not emulate JetStream. Add PostgreSQL barriers around replay authorization and use `pg_terminate_backend` after the baseline/authorization commit, before publish, and after accepted publish/before finalization. Restart the locally constructed dispatcher (not the future Compose harness) between phases. Below and at 20, the recovered claimant must scan/re-drive only the persisted replay event/root/message ID; the resumed old claimant may add an at-least-once transport duplicate but semantic receipt applies once. Repeat with concurrent DELETE and source expiry after authorization: accepted evidence finalizes, unavailable source plus unproven acceptance remains draining, and no replacement event is invented. Cleanup deletes only unique test consumers/streams. The file owns its local barriers/backend-PID lookup over the existing PostgreSQL fixture and imports no Task 9 helper. These minimal real-NATS failures must be observed here rather than first in Task 9; the isolated greater-than-120-second aggregate proof remains Task 9.

Resolve tests require a nonempty redacted note of at most 1,000 characters, allow only `open`, store the note only in audit metadata, and prove a secret canary is redacted before commit.

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/recovery/tests/test_replay.py packages/recovery/tests/test_deletion.py packages/recovery/tests/test_import_boundaries.py apps/api/tests/test_idempotency.py apps/api/tests/test_event_failures.py apps/api/tests/test_operations_health.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_telemetry.py -q
uv run pytest -m integration tests/integration/test_phase10_event_replay.py -q
```

- [ ] **Step 3: Implement exact bounded API schemas**

Use `ConfigDict(extra="forbid")` everywhere. Define:

```python
class ReplayEligibilityOut(BaseModel):
    eligible: StrictBool
    reason_code: ReplayEligibilityReasonCode

class EventFailureSummaryOut(BaseModel):
    id: UUID
    event_id: UUID | None
    correlation_id: UUID | None
    origin_stream: EventOriginStream
    consumer_name: Annotated[str, Field(max_length=100)]
    subject: Annotated[str, Field(max_length=500)]
    source_stream_sequence: Annotated[int, Field(ge=1)]
    delivery_count: Annotated[int, Field(ge=1)]
    handler_attempt_count: Annotated[int, Field(ge=0, le=5)]
    reason_code: EventFailureReason
    safe_error_class: Annotated[str, Field(max_length=64)] | None
    safe_error_detail: Annotated[str, Field(max_length=2_000)] | None
    status: EventFailureStatus
    first_failed_at: AwareDatetime
    last_failed_at: AwareDatetime
    replayed_at: AwareDatetime | None
    resolved_at: AwareDatetime | None

class EventFailurePageOut(BaseModel):
    items: Annotated[list[EventFailureSummaryOut], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=200)] | None

class EventReplayRequestOut(BaseModel):
    id: UUID
    failure_id: UUID
    replay_generation: Annotated[int, Field(ge=1)]
    replay_event_id: UUID
    status: EventReplayStatus
    attempt_count: Annotated[int, Field(ge=0, le=20)]
    safe_error_code: Annotated[str, Field(max_length=64)] | None
    published_at: AwareDatetime | None
    created_at: AwareDatetime

class EventFailureDetailOut(EventFailureSummaryOut):
    replay_eligibility: ReplayEligibilityOut
    latest_replay: EventReplayRequestOut | None
    replay_history: Annotated[list[EventReplayRequestOut], Field(max_length=20)]
    replay_history_truncated: StrictBool

class ResolveFailureIn(BaseModel):
    note: Annotated[str, Field(strict=True, min_length=1, max_length=1_000)]

class TaskRetryHistoryOut(BaseModel):
    id: UUID
    task_id: UUID
    source_run_id: UUID | None
    attempt_number: Annotated[int, Field(ge=2, le=TASK_RETRY_MAX_GENERATION)]
    status: TaskRetryStatus
    new_run_id: UUID | None
    new_run_status: RunStatus | None
    safe_reason_code: Annotated[str, Field(max_length=64)] | None
    attempt_count: Annotated[int, Field(ge=0, le=20)]
    started_at: AwareDatetime | None
    created_at: AwareDatetime

class TaskRetryHistoryPageOut(BaseModel):
    items: Annotated[list[TaskRetryHistoryOut], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=200)] | None

class EventFailureHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open_count: Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_COUNT)]
    oldest_open_age_seconds: Annotated[
        int, Field(strict=True, ge=0, le=MAX_SAFE_COUNT)
    ] | None
    component: HealthComponent
```

Extend the protected-health closed contracts in the same schema file with `MAX_COMPONENTS = 10`, `HealthReasonCode.EVENT_FAILURES_OPEN = "event_failures_open"`, and `HealthAction.REVIEW_EVENT_FAILURES = "review_event_failures"`. The event-failures component is exactly `{name:"event-failures",status:"ok",reason_code:null,action:"none"}` at zero/null age and `{name:"event-failures",status:"degraded",reason_code:"event_failures_open",action:"review_event_failures"}` for one or more open rows. A database read failure uses the already-defined `database_unavailable/check_database` pair and never returns exception text. These additions are required in Python and mirrored TypeScript types; no free-form component reason/action is introduced.

Cursor encoding is URL-safe base64 without padding over `timestamp.isoformat() + "|" + uuid`; decoding rejects more than 200 characters, invalid base64/UTF-8, missing delimiter, naive timestamp, or invalid UUID. It carries no query or secret state.

- [ ] **Step 4: Implement workspace-scoped service transitions and routes**

Routes are exactly:

```text
GET  /api/v1/workspaces/{workspace_id}/operations/event-failures
GET  /api/v1/workspaces/{workspace_id}/operations/event-failures/{failure_id}
POST /api/v1/workspaces/{workspace_id}/operations/event-failures/{failure_id}/replay
POST /api/v1/workspaces/{workspace_id}/operations/event-failures/{failure_id}/resolve
GET  /api/v1/workspaces/{workspace_id}/operations/task-retries
```

Router prefix uses `AdminCtx`; mutating routes depend on `csrf_protect`. Every service query includes `workspace_id`. Null-workspace failures are therefore unreachable.

Replay request order is exact. An optional fast pre-read of `(workspace,idempotency_key)` may return only when both `failure_id` and `requested_by_user_id` match. Creation then performs the bounded five-second source check outside locks but stores its typed success/expired/changed/unavailable result without returning it, begins a transaction, locks `Workspace` then `RecoveryWorkspaceDeletion` then the target failure, and **immediately requeries** the idempotency key `FOR UPDATE` before active/deletion/failure-status/reason/nonterminal/source-result eligibility. If found, exact failure/requester equality returns that command even when the winning request changed failure state or the just-completed source check failed; mismatch returns fixed conflict. Only when absent does it evaluate the captured source result and other mutable eligibility, compute `generation=max+1`, allocate UUIDv7 request ID, compute `replay_event_id=uuid5(REPLAY_EVENT_NAMESPACE, str(request_id))`, insert, set failure `replay_requested/latest_replay_request_id`, append `event.replay_requested`, and commit once. A unique violation is defensive only: rollback the whole transaction, start a new transaction, retake `Workspace -> deletion -> target failure`, select the exact key `FOR UPDATE`, verify failure/requester binding, and return it; never return an object from the aborted session or retry with a new key/generation. This serializes replay creation with deletion intent and makes the POST-vs-delete outcome durable.

`HANDLER_EXCEPTION` and `UNSUPPORTED_INGRESS_EVENT` are replayable only when `replay_semantic_identity` returns the registered INGRESS or connector matcher identity. Every other current/future handler is resolution-only until it provides a stable key and partial-original-work test. Invalid envelope/subject/invariant/workspace-deleted failures are always resolution-only.

`jhin_recovery.replay.evaluate_replay_admission` validates the workspace is active and, for a canonical connector event, uses the existing pure trigger filter evaluator to inspect every currently matched enabled trigger target. A missing/disabled target, currently exhausted target-agent monthly budget, or invalid trigger configuration returns a fixed safe 409 and creates no requested command. There is no workspace budget key. Busy concurrency is admissible because downstream workflow admission durably queues it. INGRESS replay is normalized again and its downstream trigger/workflow path performs the same current admission; the admin replay itself never bypasses those checks. The later replica-safe rate limiter subproject inserts its gate at this service boundary; until it lands there is no process-local replay limiter to misrepresent as authoritative. API and event-worker import this evaluator from `jhin_recovery`, not from each other.

If the preflight source is already missing, still insert the idempotent generation as `failed/source_event_expired`, set failure `expired`, and append `event.replay_failed`, then return 409 with the safe code. If NATS is unavailable, return 503 and create nothing. A source changed at the same sequence is a terminal `source_event_changed`, returns failure to `open`, and is audited.

Resolve locks an `open` row, redacts/strips the note, rejects empty-after-redaction, sets resolved fields, and appends `event.failure_resolved` with only the sanitized note and reason code. There is no delete, raw-message, bulk-replay, or raw-DLQ route.

Replay eligibility and dispatch errors use `REPLAY_ELIGIBILITY_SAFE_COPY` and `REPLAY_SAFE_COPY` exactly; tests assert both maps are total over their Literal values. `failure_not_open`, `reason_not_replayable`, `source_event_expired`, `source_event_changed`, `replay_in_progress`, and `idempotency_key_conflict` are 409; `source_check_unavailable`, `nats_unavailable`, and `database_unavailable` are 503; authorization remains the existing fixed 403/404 RBAC projection. Never interpolate exception strings, source subjects, IDs from another workspace, Temporal/NATS addresses, raw idempotency keys, or submitted resolution text into `HTTPException.detail`. Unit tests assert the exact copy for 400/403/404/409/503 responses and scan it for canaries.

- [ ] **Step 5: Complete replay dispatch and publish/commit reconciliation**

Add replay dispatch to the Task 4 loop only after this task's RED. A new requested row receives `NEW_ATTEMPT` without increment; an expired existing `dispatching` row receives `RECONCILE_EXISTING` below cap or `RECONCILE_AT_CAP` at 20, never an increment. New claim creation locks workspace -> deletion row -> request/failure and bars a prospective claim once deletion starts. A row with `external_call_authorized_at` always reconciles first: call `reconcile_or_redrive_authorized_publish` on its exact subject/replay event ID/root before loading requester membership, workspace/deletion status, source bytes, reason, budget, or configuration. Accepted evidence token-finalizes request `published`, parent `replayed`, and one audit even after revocation/deletion/source expiry. `not_observed` or scan `unknown` re-drives only the exact previously authorized envelope/identity when the original source still validates; PubAck finalizes. If source has expired or changed, it remains outcome-unknown and blocks deletion rather than manufacturing replacement bytes or a false terminal state. In the same commit, replace deletion.py's Task-4 fail-closed replay branch with this exact neutral reconciliation function; no duplicate algorithm is added.

For a never-authorized row, lock/reload `Workspace -> RecoveryWorkspaceDeletion -> request -> failure -> requester membership`, validate active/no-deletion/admin/current source exactness/current replayable reason/exact semantic identity, and release the transaction. Read and validate the exact source plus destination stream baseline with five-second calls; baseline zero is valid. A typed transient NATS/database source or baseline failure after the durable claim calls `record_preauthorization_failure` once under that claim token and schedules backoff; a failure before the writable transaction, a concurrency wait, or terminal validation consumes zero. Build the replay envelope, then `authorize_external_call` increments once (maximum 20) and atomically persists `external_call_authorized_at`, baseline, and immutable replay event identity before returning the call value. Publish only that returned envelope. It sets `replay_of_event_id=replay_root_event_id(source)` and `causation_id=source.event_id`; a replay of a replay never creates a new semantic root while its immediate transport cause remains auditable. A caller paused after this commit is safe because any claimant can only re-drive those same bytes and IDs; lease ownership is not the external-I/O fence.

On publish success, a token-checked transaction sets request `published/published_at`, failure `replayed/replayed_at`, and appends `event.replayed`; an already-published row with the same ID is idempotent success. A DB failure after NATS publish leaves an expiring `dispatching` claim. Below and at count 20, accepted evidence finalizes; `not_observed`/unknown retain authorization and allow only same-identity re-drive without increment. No branch writes 21, clears authorization, evaluates prospective gates, or creates a different identity.

Replay audit metadata is exact and bounded. All three actions contain `failure_id`, `replay_request_id`, `replay_generation`, `origin_stream`, and `source_stream_sequence`. `event.replay_requested` additionally contains `replay_event_id`; `event.replayed` adds no fields; `event.replay_failed` additionally contains `safe_error_code` from `ReplayDispatchCode`. It never contains subject, source data, safe detail text, exception text, idempotency key, or NATS headers.

Missing source sets request `failed/source_event_expired` and parent `expired`. Other terminal validation/authorization failures set request `failed`, parent `open`, clear latest request, and append `event.replay_failed`. A parent no longer pointing at the claimed request makes it `superseded`. Below cap, definite pre-I/O NATS/database unavailability returns requested with backoff. At count 20 only a proven pre-I/O failure becomes failed/open with `dispatch_exhausted`; a call whose acceptance is unknown remains dispatching for same-identity reconciliation. No terminal transition rewrites a prior audit row.

Extend `OperationsHealthSnapshot` with required `event_failures: EventFailureHealthSummary`, set the exact component count to 10, and insert `event-failures` after `nats` in component order. One workspace-scoped aggregate query computes count/min timestamp for `open|replay_requested`; invalid/future/capped values degrade with the exact closed reason/action contract above and the existing bounded helpers. No health query joins replay detail or selects safe error text.

- [ ] **Step 6: Run GREEN**

```bash
uv run pytest packages/recovery/tests/test_replay.py packages/recovery/tests/test_deletion.py packages/recovery/tests/test_import_boundaries.py apps/api/tests/test_idempotency.py apps/api/tests/test_event_failures.py apps/api/tests/test_operations_health.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_telemetry.py -q
uv run pytest -m integration tests/integration/test_phase10_event_replay.py -q
uv run ruff check packages/recovery apps/api/src/jhin_api/idempotency.py apps/api/src/jhin_api/operations apps/api/src/jhin_api/health apps/api/tests/test_idempotency.py apps/api/tests/test_event_failures.py apps/api/tests/test_operations_health.py services/event_worker/src/jhin_event_worker/commands.py services/event_worker/tests/test_telemetry.py
uv run mypy packages/recovery/src apps/api/src/jhin_api/idempotency.py apps/api/src/jhin_api/operations apps/api/src/jhin_api/health services/event_worker/src/jhin_event_worker/commands.py
```

- [ ] **Step 7: Commit exact scope**

```bash
git add packages/recovery/src/jhin_recovery/replay.py packages/recovery/src/jhin_recovery/deletion.py packages/recovery/tests/test_replay.py packages/recovery/tests/test_deletion.py packages/recovery/tests/test_import_boundaries.py apps/api/src/jhin_api/idempotency.py apps/api/src/jhin_api/operations/__init__.py apps/api/src/jhin_api/operations/router.py apps/api/src/jhin_api/operations/schemas.py apps/api/src/jhin_api/operations/service.py apps/api/src/jhin_api/health/schemas.py apps/api/src/jhin_api/health/service.py apps/api/src/jhin_api/main.py apps/api/tests/conftest.py apps/api/tests/test_idempotency.py apps/api/tests/test_event_failures.py apps/api/tests/test_operations_health.py services/event_worker/src/jhin_event_worker/commands.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_telemetry.py tests/integration/test_phase10_event_replay.py
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add durable event replay api"
```

---

### Task 6: Add Conservative Manual Task Retry and Deterministic Temporal Start

**Files:**
- Create: `apps/api/src/jhin_api/tasks/retry.py`
- Modify: `apps/api/src/jhin_api/tasks/router.py`
- Modify: `apps/api/src/jhin_api/tasks/schemas.py`
- Modify: `apps/api/src/jhin_api/tasks/service.py`
- Create: `apps/api/tests/test_task_retry.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/shared.py`
- Modify: `packages/workflows/tests/test_agent_task_tool_routing.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/projections.py`
- Create: `services/agent_worker/tests/test_task_retry_admission.py`
- Modify: `services/event_worker/src/jhin_event_worker/commands.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/tests/test_commands.py`
- Modify: `services/event_worker/tests/test_telemetry.py`
- Modify: `packages/recovery/src/jhin_recovery/task_retry.py`
- Modify: `packages/recovery/src/jhin_recovery/deletion.py`
- Modify: `packages/recovery/tests/test_deletion.py`
- Create: `packages/recovery/tests/test_task_retry.py`
- Modify: `packages/recovery/tests/test_import_boundaries.py`
- Create: `tests/integration/test_phase10_task_retry.py`

**Interfaces:**
- Consumes: Tasks 1-2 durable safety/model links, Task 4 `OperationalMemberCtx` plus durable service gate, Temporal provider, agent snapshot/admission logic, tool-worker boundary, task-retry command reconciler.
- Produces: neutral exact retry eligibility/reconciliation contracts, monotonic retry generation and persisted terminal proof, idempotent request API, strict separation from the ordinary Task workflow binding, fresh-current snapshot/run linkage, deterministic Temporal reconciliation, and all task retry audit transitions.

- [ ] **Step 1: Write the complete safety matrix as failing tests**

Parameterize source runs/tool calls. Retry is ineligible for active/nonfailed tasks, any active DB run, any open Temporal workflow, executing/execution-unknown calls, completed or failed idempotent/non-idempotent calls, and unknown/missing safety. Completed/failed `pure` calls are allowed if no other blocker; denied/rejected/pending-approval calls produced no effect but condition-specific errors remain blocked until remediation.

Cover a failed task with no `AgentRun` because the original Temporal start failed. The evaluator must still describe synthesized `task-{task_id}` even when `Task.temporal_workflow_id`, retry rows, and runs are null. NotFound permits continuing current checks only when no ordinary start authorization exists; an open history is `workflow_active`; a closed history with no linked `AgentRun` is `ambiguous_external_effect`, because the original attempt may have executed without projection. If `Task.temporal_start_authorized_at` is nonnull, NotFound is outcome-unknown: reconcile/re-drive that same ordinary ID under `REJECT_DUPLICATE` and block manual retry, because a paused authorized caller may resume. The nullable source run is eligible only when every unauthorised synthesized/history candidate is NotFound and every authorized prior start has persisted terminal proof.

Seed distinct valid IDs in `Task.temporal_workflow_id`, 40 prior `TaskRetry` rows (including rows without `new_run_id`), and 40 `AgentRun` rows. Assert every deduplicated ID plus base is described, open history blocks, and closed history without an exact linked run/retry blocks ambiguous. Invalid/overlong IDs or more than `MAX_TASK_WORKFLOW_IDS=100` fail closed as `invariant_violation`; none are silently skipped. A Temporal error describing any ID returns `temporal_unavailable`, never inferred closed from DB state.

Add merge-invariant cases where synthesized base ID, `Task.temporal_workflow_id`, an `AgentRun.temporal_workflow_id`, and one or more `TaskRetry.temporal_workflow_id` strings are identical. The one output entry must retain `is_base`, `is_task_binding`, `task_start_authorized`, exact `agent_run_id`, and exact `task_retry_id`; deduplication may never drop authorization/run/retry linkage because base/task was inserted first. Two different AgentRuns for one workflow ID, two retries claiming the same workflow ID, a run/retry from another task/workspace, a Task authorization timestamp attached to a different/missing Task workflow ID, or a retry whose `new_run_id` conflicts with the attached run is `invariant_violation`, not precedence-by-query-order.

For failure remediation, compare relevant state to `source_run.completed_at`:

- authorization/policy denial requires an `AgentCapabilityGrant.updated_at` or `Agent.updated_at` later than completion;
- invalid configuration/provider/connection failure requires affected `Agent`, `ModelProfile`, `ModelProvider`, `Connection`, or workspace default-profile `updated_at` later than completion and current snapshot validation success;
- budget exhaustion requires current monthly spend below the existing `Agent.monthly_budget_cents`; no workspace budget/settings key is read;
- max steps requires current `agent.max_steps > source_run.steps_used` and an agent update later than completion;
- explicit rejection requires a later agent policy/grant update; a prior approval decision alone is not remediation.

Test same-key replay, distinct-key 409, atomic failed-to-queued transition, member/admin/owner allowed, viewer 403, nonmember 404, task from another workspace 404, and exact audit metadata. Release two same-key requests past the optional fast read: after the winner changes Task to queued/increments the generation, the loser takes `Workspace -> deletion -> Agent -> Task -> source run` locks, immediately requeries the key, and returns the exact task/requester binding **before** failed-state/eligibility/counter checks. A requester/task mismatch is the fixed conflict; an injected unique violation exercises only the defensive locked reload. Persist an ordinary `Task.temporal_workflow_id`/authorization, request a manual retry, and assert all ordinary ID/input/proof fields remain byte-for-byte unchanged while the `TaskRetry` alone owns `task-{task_id}-attempt-{attempt_number}` and its eventual versioned input. The API evaluator accepts only `task.state=failed`; dispatcher evaluation accepts only `task.state=queued`, exact `Task.metadata_json["recovery_dispatch"] == {"kind":"manual_task_retry","retry_id":str(retry.id),"attempt_number":retry.attempt_number}`, retry status/claim `dispatching`, and its own row excluded from `retry_in_progress`. It must not compare or assign `Task.temporal_workflow_id` to the retry ID. Every metadata/row mismatch is invariant/rejected. Test dispatcher membership revocation, durable deletion intent, disabled agent, new effect row, agent budget exhaustion, concurrency wait, Temporal outage, and `WorkflowAlreadyStartedError` under `REJECT_DUPLICATE` as success even after the exact workflow closes.

Add an explicit ordinary/manual race RED. Pause an already-authorized ordinary start before Temporal, expire its start-repair claim, and concurrently request manual retry; NotFound must re-drive the ordinary ID and the retry POST must remain blocked, even if workflow projection changed Task to running/failed or wrote manual-looking queue metadata. Also pause the manual request after it locks Task but before queue commit while the ordinary authorized-start reconciler scans; then reverse the winner. A queued task with only a valid manual binding and no authorized/unaccepted ordinary shape is absent from the ordinary query. If the immutable ordinary authority exists, however, state/metadata never suppress it: repair may use only `Task.temporal_workflow_id`/stored ordinary input, while manual dispatch remains blocked and may only use `TaskRetry.temporal_workflow_id` after ordinary proof. Neither owner can overwrite or pass the other's identity/input. Signal/message/pause/resume/cancel routing loads the exact active `AgentRun.task_retry_id`/nonterminal retry binding under lock: it targets that retry workflow when one accepted retry run is active, otherwise the immutable ordinary Task ID; zero or multiple active bindings fail closed. A manual-retry queued task with no accepted start cannot receive a speculative signal.

At attempt 7 and 20, test all three crash points and the stale caller: (a) immutable version-1 input plus authorization/increment commit and the old caller pauses before `start_workflow`; after lease expiry the new reconciler sees NotFound, loads/re-drives the exact workflow ID/input with `REJECT_DUPLICATE`, then the old caller resumes; (b) Temporal accepts start but DB finalization is blocked through lease expiry; (c) started/audit finalization commits but dispatcher crashes before return. Split mutable-field drift from Agent deletion. In the field-drift case, leave the authorized Agent alive, mutate Task description and assignment to a second live Agent, and assert every caller submits byte-equal stored `AgentTaskInput`, one workflow, one audit, and exactly one `AgentRun` whose agent is the originally authorized Agent.

For Agent deletion, run both canonical lock winners. Deletion-first locks `Workspace -> RecoveryWorkspaceDeletion -> Agent`, commits Agent deletion after immutable retry authorization but before snapshot, then resumes callers. They still submit byte-identical input and Temporal history contains one workflow start, but snapshot observes the missing Agent, the workflow catches that failure, projects only Task `failed` with `snapshot_failed`, returns with actual Temporal status `completed`, and creates **zero AgentRun rows**. Its final projection does not mutate reconcile authority; the acceptance schedule remains due and input/proof remain present/null until the post-close reconciler records `completed/closed_before_run` and clears input. Snapshot-first holds the same lock prefix through `AgentRun(task_retry_id=retry.id)` insert/commit; Agent DELETE waits, then returns the fixed recovery-in-progress 409 with Agent/run/audit unchanged until actual close plus TaskRetry terminal proof/input clear. A retry DELETE then succeeds once. Run both branches below and at cap and assert fixed count, stable workflow/input, and no count 21.

Repeat the authorized stale-caller path after requester revocation, workspace deletion intent, source-run cleanup, agent disablement, and budget exhaustion: once authorized, history or NotFound is reconciled/re-driven before and without every prospective gate. While history is open, a workflow-task completion is delayed, or describe is unavailable/ambiguous, the start input remains stored, proof stays null, and deletion drains. Only the later post-close `reconcile_task_retry_terminal` call may invoke `record_task_retry_terminal_proof`; a database crash before its atomic proof/clear keeps both, and a crash after commits both. Separately test a count-20 row with **null** authorization and contract version 0, produced only by 20 proven pre-authorization failures, terminalizes exhausted without a start/input.

Make the null-at-20 case executable: inject 20 separately committed typed `temporal_unavailable` failures after dispatcher claims but before external-call authorization, assert `record_preauthorization_failure` consumes each claim token once and advances `0..20`, and then exhaust with zero Temporal starts. A DB failure before the claim transaction, concurrency wait, or deterministic admission rejection consumes zero. In a separate row, authorize/start as attempt 20 and prove every describe/re-drive/finalize path leaves the count at 20.

Allocate and retain retry generations explicitly. Start with `Task.last_retry_attempt_number=1`; under the Task row lock a new binding increments it to 2 exactly once. Same-key replay reads the existing request before allocation. Two distinct-key races yield one nonterminal row/counter value, and the loser never burns or reuses a generation. After attempt 2 reaches terminal proof and its `TaskRetry` is purged by retention, the next accepted request allocates attempt 3 and workflow `task-{task_id}-attempt-3`; it never scans `max(TaskRetry.attempt_number)` or returns to 2. Seed `TASK_RETRY_MAX_GENERATION` and assert another distinct request fails `invariant_violation`, leaves the counter unchanged, and calls no Temporal API.

While a retry workflow is still open, commit its exact terminal Task/AgentRun product projection and invoke `reconcile_task_retry_terminal`; assert `outcome="open"`, input retained, proof null, and deletion blocked. Delay the workflow-task completion acknowledgement and repeat to prove a final activity cannot self-certify the close that follows its return. After the exact workflow actually closes, run the post-close reconciler: it describes the persisted ID, validates the deterministic retry/attempt/input/run binding, calls `record_task_retry_terminal_proof(run_outcome="terminal_run")` under the current reconcile claim, and atomically persists generation/ID/actual status/proven-at plus `start_input_cleared_at` while nulling `start_input_json`. Equal reconciliation is idempotent. For the deleted-agent path, require zero AgentRuns and permit `closed_before_run` only after a five-second, keyset-paged Temporal history scan of at most `TASK_RETRY_TERMINAL_HISTORY_MAX_EVENTS=10_000` proves `resolve_snapshot_activity` never succeeded and no run/step/tool/delegation activity occurred; a failed snapshot activity attempt is allowed evidence of the safe pre-run failure. Timeout, truncation, continuation beyond the bound, unavailable/ambiguous describe, and NotFound alone are insufficient. Wrong attempt, workflow, run outcome/link, status, contract, history input, or conflicting second proof is an invariant. Crash after closed describe but before proof keeps input and blocks deletion; crash after the atomic proof/clear but before cascade resumes and deletes product rows. Race delayed close, ambiguous describe, workspace DELETE, Agent DELETE versus snapshot on both canonical lock winners, and PostgreSQL backend termination in `tests/integration/test_phase10_task_retry.py`; include the manual/ordinary dispatcher race and start-accepted/finalize crash there. Assert snapshot-first Agent DELETE stays 409 until TaskRetry proof is durable and deletion-first produces the zero-run proof path. The surviving proof/clear then supports Agent deletion, workspace product cascade, and 90-day retention without Task/AgentRun/Temporal access.

This owning file imports predecessor `TEMPORAL_ADDRESS`, connects a real Temporal client with five-second operation bounds, and uses unique workflow IDs for the accepted/closed `REJECT_DUPLICATE`, attempt-7/20 stale-caller, immutable-input, and proof-clear cases. It uses the running agent worker/fake provider only for the minimal valid start/snapshot; local wrappers place deterministic barriers around the real client await. Cleanup terminates only unique test workflows. The file owns its barriers/backend-PID lookup over existing PostgreSQL fixtures and imports no Task 9 helper. Observe the real-PostgreSQL/Temporal RED in this task, not first in Task 9.

Agent-worker tests inject a crash after run/snapshot/retry-link commit but before activity return; activity retry must return the same run/snapshot and create no second run/event. Separately, block dispatcher finalization after Temporal accepts start until snapshot activity has been scheduled/retried: the activity may lock an exact `dispatching` or `started` retry, atomically reconcile `started/started_at` plus one `task.retry_started` audit and create/link the run; the later dispatcher finalizer is an idempotent no-op with no second audit.

Before changing ordinary admission, add a distinct RED `test_normal_snapshot_commit_then_activity_response_crash_reuses_run`: invoke a non-retry `AgentTaskInput`, commit run/snapshot/run-start projection, crash before returning, invoke the activity again under the same Temporal workflow ID, and assert exact same run/result, one snapshot, and one event. This test must be observed failing on the pre-implementation duplicate/unique-error path; it is not allowed to piggyback only on manual-retry coverage.

Also extend `services/event_worker/tests/test_telemetry.py` before implementation: its typed dispatcher fake gains task-retry start and post-close batches and asserts the exact authorized-trigger → ordinary-start → ordinary-terminal → outbox → replay → task-retry-start → task-retry-terminal → deletion loop order without changing consumer spans, lag polling, heartbeat, or shutdown. The old production loop must make this assertion RED until Step 5 wires both batches.

- [ ] **Step 2: Run RED**

```bash
uv run pytest packages/recovery/tests/test_task_retry.py packages/recovery/tests/test_deletion.py packages/recovery/tests/test_import_boundaries.py apps/api/tests/test_task_retry.py packages/workflows/tests/test_agent_task_tool_routing.py services/agent_worker/tests/test_task_retry_admission.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_telemetry.py -q
uv run pytest -m integration tests/integration/test_phase10_task_retry.py -q
```

- [ ] **Step 3: Extract one exact current-admission evaluator**

Implement admission in `packages/recovery/src/jhin_recovery/task_retry.py`; `apps/api/tasks/retry.py` owns only HTTP request creation/projection and imports the evaluator, as do event-worker and agent-worker. Lock workspace, durable deletion row, agent, task, and latest source run in that order. `evaluate_task_retry_request` requires failed and no nonterminal retry. `evaluate_task_retry_dispatch` locks its retry by ID/token, requires dispatching plus the exact queued task binding described in Step 1, excludes only that command/workflow from active checks, and otherwise calls the same current-condition core. Do not weaken the public evaluator to make dispatch work. Compute month start in UTC and sum committed `AgentRun.estimated_cost_micros`; one cent equals 10,000 micro-dollars. Compare only to existing `Agent.monthly_budget_cents`; delete every proposed workspace budget/settings read. Existing validated workspace concurrency remains a scheduling gate. Concurrency produces `concurrency_wait` for dispatcher rescheduling, not a terminal HTTP rejection. Budget/config/safety blockers return their exact closed reason.

`collect_known_task_workflows` validates every ID as ASCII `1..200`, gathers all sources before enforcing the cap, and folds a mapping keyed by workflow ID. Merge is fieldwise: base sets `is_base`; Task sets `is_task_binding` plus its exact `task_start_authorized`; exact AgentRun sets `agent_run_id`; exact TaskRetry sets `task_retry_id`. Source insertion order never overwrites another field. Any conflicting nonnull linkage listed in Step 1 fails closed. Return sorted by workflow ID, capped at 100 only after conflict validation. Describe every entry with a five-second timeout. An open execution is active; a closed authorized ordinary/retry execution is safe to classify only when its retained linkage includes the corresponding persisted post-close terminal proof and its linked run's tool calls are fully inspected. A closed base/task/retry history with no exact projection/proof is ambiguous. NotFound means absent only for a never-authorized candidate; an authorized Task/TaskRetry NotFound is reconciled/re-driven under its exact ID and blocks this fresh-attempt evaluator until terminal proof. One unavailable/invalid/overflow result fails closed.

`classify_task_retry_effects` is total and order-independent. Any `executing` or `execution_unknown` row returns `ambiguous_external_effect`. A `completed` non-`pure` row returns `committed_external_effect`; a `failed` non-`pure` row returns `ambiguous_external_effect`, because the Phase 10 contract cannot prove the executor failed before its effect. A `completed`/`failed` row with null, unknown, or invalid persisted safety returns `unknown_tool_safety`. Completed/failed `pure` rows add no blocker. `denied` with `no_grant`, `required_scope_missing`, or `scope_mismatch` maps to `authorization_unchanged`; `denied` with `explicit_deny`, `forbidden_by_policy`, or `approval_unsupported` maps to `policy_unchanged`; `rejected` or error `approval_rejected` maps to `explicit_rejection_unchanged`. A `pending_approval` row on a terminal failed attempt is `invariant_violation`, not evidence of safety. Any other denied/error code is `configuration_unchanged` until current snapshot validation plus a relevant later update proves remediation. When multiple blockers exist, precedence is: ambiguous effect, committed effect, unknown safety, invariant, authorization, policy, explicit rejection, configuration.

After the effect scan, source run `max_steps_exceeded` maps to `max_steps_unchanged` until the stated step-limit remediation passes; `snapshot_failed` maps to `configuration_unchanged` until current snapshot validation and the stated later update pass. Budget admission always runs against current committed spend and wins with `budget_exhausted`. Generic `step_failed`, `delegation_failed`, and `approval_resolution_failed` are not independently replay-safe or unsafe: their persisted ToolCall rows, current snapshot validation, and active-workflow check decide. Unknown run error text is never parsed heuristically and never returned.

Temporal openness recognizes every nonterminal SDK status and absent close time; do not enumerate only current happy-path values. Temporal unavailability makes request eligibility unavailable (HTTP 503) and dispatcher transient; never guess closed from task state alone.

For automatic retry projection, read only bounded pending-activity metadata from `description.raw_description.pending_activities`:

```python
class AutomaticRetryOut(BaseModel):
    state: Literal["running", "retry_scheduled", "exhausted", "inactive", "unknown"]
    activity: Annotated[str, Field(max_length=100)] | None
    attempt: Annotated[int, Field(ge=0, le=10_000)] | None
    maximum_attempts: Annotated[int, Field(ge=0, le=10_000)] | None
    next_attempt_at: AwareDatetime | None

class ManualRetryEligibilityOut(BaseModel):
    eligible: StrictBool
    reason_code: TaskRetryReasonCode
    source_run_id: UUID | None

class TaskRetryOut(BaseModel):
    id: UUID
    task_id: UUID
    source_run_id: UUID | None
    attempt_number: Annotated[int, Field(ge=2, le=TASK_RETRY_MAX_GENERATION)]
    temporal_workflow_id: Annotated[str, Field(max_length=200)]
    configuration_mode: Literal["current"]
    status: TaskRetryStatus
    new_run_id: UUID | None
    new_run_status: RunStatus | None
    safe_reason_code: Annotated[str, Field(max_length=64)] | None
    attempt_count: Annotated[int, Field(ge=0, le=20)]
    available_at: AwareDatetime
    started_at: AwareDatetime | None
    created_at: AwareDatetime

class TaskRetryStateOut(BaseModel):
    automatic: AutomaticRetryOut
    manual: ManualRetryEligibilityOut
    requests: Annotated[list[TaskRetryOut], Field(max_length=50)]
```

Add `GET /tasks/{task_id}/retry-state` under `ViewerCtx` and `POST /tasks/{task_id}/retry` under Task 4's `OperationalMemberCtx`, router CSRF, and required idempotency header. The dependency gives the same fixed early deletion projection as every other task mutation, while request creation still retakes the workspace/deletion locks in its own transaction. The GET never returns Temporal failure detail; unavailable becomes `automatic.state="unknown"` and manual reason `temporal_unavailable`.

- [ ] **Step 4: Implement atomic request creation**

An optional existing workspace/idempotency pre-read returns only when both `task_id` and `requested_by_user_id` match; otherwise it returns fixed 409 `idempotency_key_conflict` without exposing the other task/command. For creation lock `Workspace -> RecoveryWorkspaceDeletion -> Agent-if-present -> Task -> source-run-if-present`; missing optional rows are recorded for later eligibility but do not cause an early return. Then immediately requery `(workspace_id,idempotency_key) FOR UPDATE` before deletion/task state/current eligibility/counter checks. Exact task/requester equality returns the winner even if it already queued the Task or the agent/source run has since disappeared; mismatch is the fixed conflict. Only when absent does creation evaluate those missing/current conditions, reject deletion/ineligible state with allowlisted copy, increment `Task.last_retry_attempt_number` exactly once, and use that committed value as `attempt_number`; never derive it from retained retry rows. Allocate the request ID and set only `TaskRetry.temporal_workflow_id = f"task-{task_id}-attempt-{attempt_number}"`; its contract remains version 0/null until dispatcher authorization. Insert, set task `queued`, leave every ordinary Task workflow/input/proof field unchanged, replace only `Task.metadata_json["recovery_dispatch"]` with `{"kind":"manual_task_retry","retry_id":str(retry.id),"attempt_number":attempt_number}`, append `task.retry_requested`, and commit once. Application validation bounds this reserved object to exactly those keys/types while preserving unrelated metadata. A defensive unique-race loser rolls back the whole transaction/counter, retakes the same canonical locks, requeries the exact key, and returns only its exact binding; a mismatch returns 409 without allocating a generation.

Every task-retry API error uses `TASK_RETRY_SAFE_COPY` exactly; tests assert the map is total over `get_args(TaskRetryReasonCode)`. Immediate request blockers are 409, `requester_unauthorized` is 403, missing workspace/task is 404, malformed input is 400/422 under existing API conventions, and `temporal_unavailable` is 503. No copy contains task/agent/user IDs, stored run error text, Temporal failure detail, tool inputs/outputs, or exception strings. The POST response and Operations history expose `safe_reason_code` plus the joined `new_run_status`, never raw workflow failure payloads.

- [ ] **Step 5: Reconcile deterministic Temporal start with current checks**

Only after this task's Step 2 RED, add `dispatch_task_retry_batch` and `reconcile_task_retry_terminals_batch` to the existing loop after ordinary-start, ordinary-terminal, outbox, and replay batches and before deletion; prior commits never call these future methods. Preserve Task 4's ordinary authority query exactly: every due version-1 authorized/unaccepted/uncleared ordinary row is repaired regardless of Task state or `recovery_dispatch`. A queued Task with only a manual binding and no such ordinary authority is naturally absent; a malformed coexistence never causes the ordinary reconciler to use the retry ID, while the manual evaluator blocks until ordinary proof. A requested never-authorized retry claim takes the deletion gate before mutation, sets `dispatching`/token/expiry and `NEW_ATTEMPT`, then runs prospective admission. `concurrency_wait` returns to requested without incrementing; other invalid conditions reject safely. A typed transient Temporal dependency failure after the durable claim calls `record_preauthorization_failure` exactly once and returns to requested/backoff; database failure before commit increments zero. Immediately before the first Temporal call, construct the complete current `AgentTaskInput` with exact retry/workflow linkage and pass it to neutral `authorize_task_retry_start`. That transaction locks retry by ID/token, validates/canonicalizes the input, increments once (maximum 20), and atomically stores immutable `external_call_authorized_at`, contract version 1, bounded JSON, and the already-persisted `TaskRetry.temporal_workflow_id`; it returns `AuthorizedTaskRetryStart`. It never reads or writes the ordinary Task workflow/input/proof fields. No Temporal call occurs before that commit.

An expired authorized claim receives `RECONCILE_EXISTING` below cap or `RECONCILE_AT_CAP` at 20 without increment, calls `load_authorized_task_retry_start`, and performs a bounded describe of only its exact workflow ID **before and instead of** membership/workspace/deletion/task/source run/agent/tool/budget/concurrency re-admission. Any history—including closed—is accepted and calls neutral `reconcile_task_retry_started`; NotFound re-drives `authorized.workflow_input` with `REJECT_DUPLICATE`; unavailable remains dispatching/backoff. It never rebuilds from Task/Agent rows, rejects, or exhausts an authorized row. A never-authorized count-20/version-0 row may fail `dispatch_exhausted`, but that shape cannot coexist with a paused authorized caller. Replace deletion.py's task-retry branch with the same contract load/describe/re-drive/closed-workflow-run proof; accepted, NotFound, and unknown authorized executions all keep deletion draining until terminal proof and input clear.

Both the first caller and every reconciler start only the value returned by `authorize_task_retry_start`/`load_authorized_task_retry_start`; the caller must not retain a pre-commit `Task`/`Agent` projection or reconstruct the input after authorization:

```python
authorized = await load_authorized_task_retry_start(session, retry_id=retry_id)
await temporal.start_workflow(
    "AgentTaskWorkflow",
    authorized.workflow_input,
    id=authorized.workflow_id,
    task_queue=AGENT_TASK_QUEUE,
    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
)
```

Exact `WorkflowAlreadyStartedError` is success for open or closed history because reuse policy rejects both. Any other client error describes the exact ID: any history is accepted, NotFound on an authorized row calls the same start again without increment or current-gate evaluation, and unavailable/ambiguous remains dispatching for same-ID reconciliation. Neutral `reconcile_task_retry_started` locks the row and accepts either matching dispatching or already-started; only the first transition sets started/time, appends audit, and schedules `terminal_reconcile_available_at` for independent post-close observation. Dispatcher finalization uses it and is idempotent if snapshot activity won the race. The same rule holds at cap: accepted finalizes, NotFound re-drives exact ID, ambiguous remains reconcilable, and nothing writes count 21 or a different start.

Task retry audit metadata is exact: every action contains `task_retry_id`, `task_id`, `attempt_number`, nullable `source_run_id`, and `configuration_mode="current"`; started adds the deterministic `temporal_workflow_id`; rejected/failed add only the closed `safe_reason_code`. No event stores instruction text, old/new snapshot JSON, idempotency key, tool details, Temporal failure, or exception text.

The final workflow activity may update only terminal Task/AgentRun product state. It cannot mutate terminal-reconcile scheduling/claims, record actual workflow-close proof, or clear input before the workflow returns; `reconcile_task_retry_started` already scheduled the independent pass at acceptance. `reconcile_task_retry_terminal` claims an exact started/unproven row under its paired 30-second terminal-reconcile lease and describes only the persisted workflow ID with a five-second timeout. Open history schedules another 30-second check; unavailable, ambiguous, or post-acceptance NotFound preserves input and schedules the same saturated nine-step backoff as ordinary starts. For a closed execution it reads the execution-start event, requires workflow type `AgentTaskWorkflow`, deserializes/recanonicalizes its input, and requires byte equality with the stored version-1 input plus exact workspace/task/retry/attempt/workflow binding. It then requires either the exact matching terminal `AgentRun`/`new_run_id`, or the bounded `closed_before_run` history proof from Step 1. Only then may `record_task_retry_terminal_proof` lock/reload under the current claim, require proof generation equal to `attempt_number`, validate outcome-dependent paired/null run fields and allowlisted actual close status, and atomically set every proof field plus `start_input_cleared_at`, null `start_input_json`, clear due/claim, and reset diagnostic failures. Later equal calls are no-ops; an unavailable/open/NotFound execution, stale claim, product projection alone, or conflicting proof preserves input and blocks deletion. Deletion calls this same post-close reconciler and can use the durable proof after Task/AgentRun vanish, but cannot use `TaskRetry.status=started`, a terminal Task/AgentRun projection, or closed describe without binding/history validation. Signal-target resolution in `apps/api/tasks/service.py` takes the universal Workspace -> deletion -> Task -> active AgentRun/TaskRetry lock order and follows the exact ordinary/manual rule from Step 1; no API route guesses from the last retry row. `TaskWorkflowTarget(kind="not_started")` preserves the existing 409 copy `Task has no workflow (it was never assigned to an agent)`; `invariant` returns fixed 409 `Task workflow state is inconsistent`; neither response includes a workflow/retry/run ID. Existing state-conflict and Temporal-signal copies remain unchanged. Add exact response/canary assertions for instruction, pause, resume, and cancel.

- [ ] **Step 6: Make retry admission create/recover exactly one new run**

Extend `AgentTaskInput` with defaults in Shared Interfaces so old histories deserialize unchanged. On every ordinary/manual input, `resolve_snapshot_activity` validates UUIDs first, then starts its writable transaction with `Workspace -> RecoveryWorkspaceDeletion -> Agent` locks using the immutable input IDs, exactly matching Task 4's Agent deletion prefix. It holds the Agent lock through snapshot/AgentRun insert or idempotent reload and commit. An already-authorized start may continue while the workspace gate is `deleting`; that lock serializes deletion but does not rerun a prospective workspace gate. If Agent deletion won, the Agent is absent and the activity takes the safe zero-run snapshot-failure path. If snapshot won, Agent DELETE waits and then sees the committed unproven run and returns 409 until post-close proof.

For retry input after that prefix, lock Task then `TaskRetry`, require matching workspace/task/workflow ID and status in `{dispatching,started}`, and call `reconcile_task_retry_started` in the same transaction before snapshot/run creation, so Temporal execution need not wait for dispatcher projection. A rejected/failed/requested row or source-task mismatch fails nonretryably. The ordinary path likewise locks Task only after Agent and preserves Task 4's existing exact authorization validation. No snapshot path locks Task/TaskRetry before Agent.

Resolve current agent/model/grants/policy and existing agent budget exactly as a normal new admission. In the same transaction create `AgentRun(task_retry_id=row.id, temporal_workflow_id=activity.info().workflow_id, execution_snapshot_json=snapshot.model_dump(mode="json"))`, set `row.new_run_id`, set task running, and append the normal run-start projection. If `new_run_id` already exists, load that exact run, validate workflow/task/agent and `execution_snapshot_json` through `AgentExecutionSnapshot.model_validate`, and return the same `SnapshotResult`; do not resolve a new snapshot or add another event. Normal non-retry runs first look up the unique Temporal workflow ID, validate its task/agent and snapshot, and reuse it; otherwise they insert run/snapshot/projection atomically. This makes the explicit ordinary-crash RED green without exposing snapshot data publicly.

Finalization preserves `TaskRetry.status=started`; the task/run statuses remain the product work-outcome authority. For both retry and ordinary executions, the in-workflow final activity updates only Task/AgentRun state. It never mutates terminal-reconcile authority, writes a Temporal close status/proof, or clears input. Acceptance already made reconciliation due; the event worker's later `reconcile_task_retry_terminals_batch` and `reconcile_ordinary_task_start_terminals_batch` own exact closed-history validation and atomic proof/input clear. Until that later commit, mutation or deletion of the current Task/Agent cannot change the stored input and workspace deletion must keep draining. Do not turn task retry into a Temporal workflow reset, reuse an old run ID, copy old grants/secrets, or reuse the old snapshot.

- [ ] **Step 7: Run GREEN and affected workflow/agent suites**

```bash
uv run pytest packages/recovery/tests/test_task_retry.py packages/recovery/tests/test_deletion.py packages/recovery/tests/test_import_boundaries.py apps/api/tests/test_task_retry.py packages/workflows/tests/test_agent_task_tool_routing.py services/agent_worker/tests/test_task_retry_admission.py services/agent_worker/tests/test_reasoning_manifest.py services/agent_worker/tests/test_step_projection.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_telemetry.py -q
uv run pytest -m integration tests/integration/test_phase10_task_retry.py -q
uv run ruff check packages/recovery apps/api/src/jhin_api/tasks packages/workflows/src/jhin_workflows/agent_task services/agent_worker services/event_worker/src/jhin_event_worker/commands.py services/event_worker/tests/test_telemetry.py tests/integration/test_phase10_task_retry.py
uv run mypy packages/recovery/src apps/api/src/jhin_api/tasks packages/workflows/src/jhin_workflows/agent_task services/agent_worker/src services/event_worker/src/jhin_event_worker/commands.py tests/integration/test_phase10_task_retry.py
```

- [ ] **Step 8: Commit exact scope**

```bash
git add packages/recovery/src/jhin_recovery/task_retry.py packages/recovery/src/jhin_recovery/deletion.py packages/recovery/tests/test_task_retry.py packages/recovery/tests/test_deletion.py packages/recovery/tests/test_import_boundaries.py apps/api/src/jhin_api/tasks/retry.py apps/api/src/jhin_api/tasks/router.py apps/api/src/jhin_api/tasks/schemas.py apps/api/src/jhin_api/tasks/service.py apps/api/tests/test_task_retry.py packages/workflows/src/jhin_workflows/agent_task/shared.py packages/workflows/tests/test_agent_task_tool_routing.py services/agent_worker/src/jhin_agent_worker/activities.py services/agent_worker/src/jhin_agent_worker/projections.py services/agent_worker/tests/test_task_retry_admission.py services/event_worker/src/jhin_event_worker/commands.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/tests/test_commands.py services/event_worker/tests/test_telemetry.py tests/integration/test_phase10_task_retry.py
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add safe manual task retry"
```

---

### Task 7: Build the Admin DLQ and Task Retry User Interfaces

**Files:**
- Modify: `apps/web/Dockerfile`
- Modify: `apps/web/lib/types.ts`
- Modify: `apps/web/lib/hooks.ts`
- Create: `apps/web/lib/recovery-contract.ts`
- Modify: `apps/web/app/(app)/operations/page.tsx`
- Modify: `apps/web/app/(app)/tasks/[id]/page.tsx`
- Create: `apps/web/components/event-failure-panel.tsx`
- Create: `apps/web/components/task-retry-card.tsx`
- Create: `apps/web/tests/event-failure-panel.test.tsx`
- Modify: `apps/web/tests/operations-page.test.tsx`
- Create: `apps/web/tests/task-retry-card.test.tsx`
- Create: `apps/web/tests/task-detail-retry.test.tsx`
- Create: `apps/web/tests/recovery-contract-parity.test.ts`
- Create: `apps/web/tests/docker-build-context.test.ts`

**Interfaces:**
- Consumes: protected-health Operations page/visibility polling, Tasks 5-6 JSON, workspace roles, `api<T>()`, CSRF support, query invalidation.
- Produces: typed allowlisted DLQ/replay/resolve/history presentation and conservative member task retry with no speculative effects.

- [ ] **Step 1: Write failing role, rendering, eligibility, idempotency, and redaction tests**

Admin Operations fixtures show protected-health open-DLQ count/oldest age, filters, bounded failure cards, detail, safe action copy, replay generation/outcome, and task-retry history. Viewer/member render no DLQ panel and make zero operations failure/history requests. Hostile unknown fields/raw-payload/traceback/secret canaries never render.

Selecting a failure triggers its detail request. Before detail succeeds there is no Replay button. Detail `eligible=false` shows mapped reason only; `eligible=true` shows Replay. Double-click while mutation pending sends one POST and one generated idempotency key; a network retry reuses the stored key until success/error is resolved. After POST or full page reload, render latest generation/status/safe code and all returned history; poll while latest status is requested/dispatching and stop at published/failed/superseded. Resolve requires entered note and confirmation.

The parity test imports `packages/domain/src/jhin_domain/recovery_contract.json` through the monorepo-relative JSON path and asserts the TypeScript key unions/maps contain exactly those keys and byte-for-byte copy. Removing or adding a Python contract key fails web tests; no UI-local fallback map exists. `docker-build-context.test.ts` first requires the exact minimal `COPY` source/destination in `apps/web/Dockerfile`, then invokes `docker build --target build -f apps/web/Dockerfile .` only after that assertion. Before implementation it must be observed RED on the missing COPY, so a stale host-only build cannot make the regression pass accidentally.

Task detail shows attempt history, source failure, automatic retry state, manual reason, and new run link. Execution-unknown, completed idempotent/non-idempotent, and unknown safety fixtures have no retry button. Eligible member gets one button; viewer gets none and no POST. The confirmation text never claims idempotent writes make a new run safe.

- [ ] **Step 2: Run RED**

```bash
pnpm --filter jhin-web test -- recovery-contract-parity.test.ts event-failure-panel.test.tsx operations-page.test.tsx task-retry-card.test.tsx task-detail-retry.test.tsx
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test -- docker-build-context.test.ts
```

Expected: component/type tests fail on missing recovery UI, and the Docker regression fails on the absent canonical JSON `COPY` before spawning a build. Record that specific RED before editing `apps/web/Dockerfile`.

- [ ] **Step 3: Mirror exact API types and add visibility-aware hooks**

Import the canonical JSON in `recovery-contract.ts`, validate its fixed object shape with narrow TypeScript guards, export key-derived reason types/maps, and mirror every other Task 5/6 enum/field; do not use `any` or recursive rendering. Add:

```typescript
export function useEventFailures(workspaceId: string, params: EventFailureFilters, enabled: boolean)
export function useEventFailureDetail(workspaceId: string, failureId: string | null, enabled: boolean)
export function useOperationsTaskRetries(workspaceId: string, cursor: string | null, enabled: boolean)
export function useTaskRetryState(workspaceId: string, taskId: string, enabled: boolean)
```

Operations hooks share the protected-health document visibility signal and poll every 10 seconds only while admin-enabled/visible. Failure detail polls at `LIVE_POLL_MS` while `latest_replay.status` is requested/dispatching, survives component/page reload because state comes from GET, and stops for published/failed/superseded. Task retry state polls at `LIVE_POLL_MS` only while the task/retry is active; a terminal stable ineligible state does not poll.

Modify the deps/build portion of `apps/web/Dockerfile` with the minimal repository copy `COPY packages/domain/src/jhin_domain/recovery_contract.json packages/domain/src/jhin_domain/recovery_contract.json` before the web build. Do not copy the whole Python package, install Python, or duplicate the JSON under `apps/web`. Keep the runtime stage unchanged; Next compiles the validated map into standalone output. The Docker regression asserts the exact source/destination and a successful clean-context build, then runs the built image's server and requests the Operations route so host-only path resolution cannot hide a missing asset.

- [ ] **Step 4: Implement allowlisted failure/retry presentation**

The event panel renders only safe fields. Local closed maps provide remediation:

```typescript
const FAILURE_ACTION: Record<EventFailureReason, string> = {
  invalid_envelope: "Verify the producing service's envelope version, then resolve this record.",
  invalid_subject: "Correct the publisher subject; this source message cannot be replayed safely.",
  unsupported_ingress_event: "Enable support for this connector event, then re-check replay eligibility.",
  handler_exception: "Repair the handler dependency, then re-check replay eligibility.",
  processing_invariant: "Inspect sanitized event-worker logs and reconcile database invariants.",
  workspace_deleted: "The workspace was deleted; retain this record for audit and do not replay it.",
};
```

Each card answers what failed (stream/consumer/subject), step (handler attempt or quarantine), automatic retry (`handler_attempt_count/5`), why stopped, safe detail, action, and timestamps. It never renders source bytes, `payload_json`, arbitrary object keys, or expandable tracebacks.

Generate idempotency keys with `crypto.randomUUID()` prefixed `ui:` (valid pattern) and retain per mutation attempt in a ref; clear only after a terminal HTTP response. Send it as `Idempotency-Key`. After success invalidate event failure, detail, operations task history, task detail, task retry state, and audit query keys as applicable.

Both replay and task-retry primary copy comes directly from canonical `recovery-contract.ts`. For `committed_external_effect` or `ambiguous_external_effect`, the task card additionally renders the fixed action `Reconcile the external system and create a new explicit task; this attempt cannot be retried.` Never offer an override/confirmation path. Render `event_failures.open_count` and formatted bounded oldest age from protected health; zero/null means `No open event failures`.

- [ ] **Step 5: Run GREEN and full web gates**

```bash
pnpm --filter jhin-web test -- recovery-contract-parity.test.ts event-failure-panel.test.tsx operations-page.test.tsx task-retry-card.test.tsx task-detail-retry.test.tsx
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test -- docker-build-context.test.ts
docker build --target build -f apps/web/Dockerfile -t jhin-web-phase10-green .
```

- [ ] **Step 6: Commit exact scope**

```bash
git add apps/web/Dockerfile apps/web/lib/types.ts apps/web/lib/hooks.ts apps/web/lib/recovery-contract.ts 'apps/web/app/(app)/operations/page.tsx' 'apps/web/app/(app)/tasks/[id]/page.tsx' apps/web/components/event-failure-panel.tsx apps/web/components/task-retry-card.tsx apps/web/tests/recovery-contract-parity.test.ts apps/web/tests/docker-build-context.test.ts apps/web/tests/event-failure-panel.test.tsx apps/web/tests/operations-page.test.tsx apps/web/tests/task-retry-card.test.tsx apps/web/tests/task-detail-retry.test.tsx
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add dlq and retry controls"
```

---

### Task 8: Add Bounded Processing/Failure Retention

**Files:**
- Create: `services/event_worker/src/jhin_event_worker/retention.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/settings.py`
- Create: `services/event_worker/tests/test_retention.py`

**Interfaces:**
- Consumes: recovery models, INGRESS 7-day/EVENTS 14-day constants, Temporal describe provider, audit append-only contract.
- Produces: completed-state cutoff enforcement, 90-day terminal failure/task-retry cleanup with workflow-closure proof and aggregate audit, outbox/request cleanup, deleted-gate cleanup, and bounded periodic retention loop.

- [ ] **Step 1: Write failing cutoff, batch, and nonterminal preservation tests**

Seed boundary timestamps. INGRESS `completed` purges only when older than 14 days (7-day source window + 7); EVENTS only older than 21 days (14+7). Exactly-at-cutoff remains. `handling` and `quarantine_only` never purge regardless of age. Batch size is 500 and stable `(updated_at, composite key)` order.

Open/replay_requested failures never purge for a live workspace. Workspace deletion drain must resolve every inaccessible open/replay_requested row with audited `resolution_reason="workspace_deleted"`; tests assert no such row remains when deletion reaches `deleted`, eliminating the delete-to-retention deadlock. Resolved/replayed/expired older than 90 days purge with child replay rows and published/failed outbox rows in the same transaction; pending/publishing outbox prevents purge and is reported as invariant failure. Before delete, append one `event.failure_retention_purged` system audit per `(workspace,status)` with count/cutoff only. Null-workspace aggregates remain null and are not exposed via API.

Terminal task retries (`rejected|failed|started`) are retained for 90 days from `updated_at`. Rejected/failed may purge once no nonterminal task binding references them and must have `external_call_authorized_at IS NULL`, `start_contract_version=0`, and null start input; an authorized failed/rejected row is an invariant and is never purged. A started row may purge only with a complete persisted `terminal_proof_generation == attempt_number` whose workflow/status/outcome-dependent run fields agree **and** `start_input_json IS NULL` with nonnull `start_input_cleared_at` committed by that proof. Retention never deletes or nulls an immutable start contract itself and never infers closure from terminal Task/AgentRun product state. While product rows still exist, it may invoke the same neutral `reconcile_task_retry_terminal` post-close operation used by the event-worker/deletion drain; that operation must independently describe actual closed history and validate exact binding before its atomic proof-and-clear commit. Open/unavailable/ambiguous/NotFound-without-proof, a final projection whose workflow has not yet returned, a still-present input, or mismatched history is preserved and reported safely. After workspace product cascade, retention relies only on the surviving TaskRetry proof/clear and never requires deleted Task/AgentRun rows or retained Temporal history. Purge at most 500 ordered by `(updated_at,id)` and append one `task.retry_retention_purged` audit per `(workspace,status)` with count/cutoff only; the audit table's plain workspace UUID survives deletion. Deletion terminalizes only never-authorized retries, waits/re-drives every outcome-unknown accepted start until the post-close reconciler records proof/input-clear/audit before cascade, and therefore lets deleted workspaces converge to 90-day cleanup without treating absence or projection as cancellation.

A `deleted` workspace-deletion gate never purges while any processing state, failure, replay, task-retry, or outbox row carries its workspace ID. A `deleting|blocked` gate never purges by age. Tests begin from Task 4's post-cascade late-delivery state: a delivery while the gate is `deleted` terms and creates no recovery/audit row, including after source-processing retention cutoffs; exactly-at-90-days retains the gate, and only a later empty-row scan beyond 90 days removes it. After the last recovery row is terminal/eligible/purged and `deleted_at` is older than 90 days, delete the `deleted` gate in the same bounded job. Thus the gate survives every stale command and product cascade but has a finite lifecycle after terminal history; there is no tombstone/FK loop or contradictory early removal.

- [ ] **Step 2: Run RED**

```bash
uv run pytest services/event_worker/tests/test_retention.py -q
```

- [ ] **Step 3: Implement bounded retention functions**

```python
PROCESSING_STATE_EXTRA_RETENTION_DAYS = 7
FAILURE_TERMINAL_RETENTION_DAYS = 90
TASK_RETRY_TERMINAL_RETENTION_DAYS = 90
RETENTION_BATCH_SIZE = 500
RETENTION_INTERVAL_SECONDS = 3600

async def purge_completed_processing_states(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None,
) -> int: ...

async def purge_terminal_failures(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None,
) -> int: ...

async def purge_terminal_task_retries(
    session_factory: async_sessionmaker[AsyncSession],
    temporal: TemporalClient,
    *, now: datetime, limit: int = RETENTION_BATCH_SIZE,
) -> int: ...

async def purge_recovery_workspace_deletions(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None,
) -> int: ...

async def run_retention_loop(
    session_factory: async_sessionmaker[AsyncSession], temporal: TemporalClient,
    stop: asyncio.Event,
) -> None: ...
```

Use `DELETE ... WHERE key IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 500) RETURNING` on PostgreSQL and a select/delete equivalent for SQLite tests. Failure purge locks parents, validates terminal children/outbox, inserts aggregate audits, deletes children/outbox then parents, and commits once. Failure retention never calls NATS/Temporal. Task-retry retention describes only a live-product row missing proof, persists proof in a short transaction, and performs deletion in a later locked transaction; rows already carrying proof require no external call.

Start the loop beside consumers/dispatcher and pass the shared trace-aware Temporal client. A retention exception emits one sanitized warning and waits until the next interval; it never stops product work. Run order is processing states, failures, task retries, then deleted gates so parent-proof checks see committed child cleanup.

- [ ] **Step 4: Run GREEN**

```bash
uv run pytest services/event_worker/tests/test_retention.py services/event_worker/tests/test_delivery.py services/event_worker/tests/test_commands.py -q
uv run ruff check services/event_worker/src/jhin_event_worker/retention.py services/event_worker/tests/test_retention.py
uv run mypy services/event_worker/src/jhin_event_worker/retention.py
```

- [ ] **Step 5: Commit exact scope**

```bash
git add services/event_worker/src/jhin_event_worker/retention.py services/event_worker/src/jhin_event_worker/main.py services/event_worker/src/jhin_event_worker/settings.py services/event_worker/tests/test_retention.py
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: retain event recovery state safely"
```

---

### Task 9: Prove Real NATS/PostgreSQL/Temporal Recovery with Fake Effects

**Files:**
- Modify: `tests/integration/conftest.py`
- Create: `tests/integration/test_phase10_dlq_retry.py`
- Create: `tests/integration/test_phase10_retry_recovery.py`
- Create: `tests/test_phase10_dlq_retry_harness.py`
- Create: `scripts/run_phase10_dlq_retry.py`
- Create: `compose.phase10-dlq-test.yaml`
- Modify: `Makefile`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: complete Tasks 1-8, predecessor `compose.rootful.yaml`/`compose.rootless.yaml`, real Compose NATS/PostgreSQL/Temporal/workers, fake provider and fake GitHub/Linear/Vercel/Supabase services, and deterministic test-process barriers.
- Produces: a fresh per-run rootful/rootless-validated Compose environment and required PR gate with live quarantine/replay/task-retry/deletion/source-expiry/capped-reconciliation/retention evidence, without production fault endpoints or third-party calls.

The harness uses these exact typed interfaces; tests import them directly from `scripts.run_phase10_dlq_retry`:

```python
SocketMode = Literal["rootful", "rootless"]
KeyBearingService = Literal["api", "agent-worker", "tool-worker"]
KEY_BEARING_SERVICES: tuple[KeyBearingService, ...] = (
    "api", "agent-worker", "tool-worker",
)
MASTER_KEY_CONTAINER_PATH = "/run/secrets/jhin_master_key"

@dataclass(frozen=True)
class Phase10Compose:
    project: str
    files: tuple[Path, Path, Path, Path]
    socket_mode: SocketMode
    docker_socket: Path
    env: Mapping[str, str]

    def argv(self, *args: str) -> list[str]: ...

@dataclass(frozen=True)
class Phase10Endpoints:
    database_url: str
    nats_url: str
    nats_monitor_url: str
    temporal_address: str
    api_url: str
    web_url: str
    sandbox_runner_url: str
    fake_provider_url: str
    fake_github_url: str
    fake_linear_url: str
    fake_vercel_url: str
    fake_supabase_url: str
    fake_supabase_database_url: str

    @classmethod
    def from_required_env(cls, environ: Mapping[str, str]) -> "Phase10Endpoints": ...

@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

def select_socket_overlay(
    *, mode: SocketMode, environ: Mapping[str, str],
    lstat_result: os.stat_result, stat_result: os.stat_result,
) -> tuple[Path, Path, dict[str, str]]: ...  # overlay, resolved socket, child env

def validate_rendered_compose(document: Mapping[str, object], compose: Phase10Compose) -> None: ...
def resolve_endpoints(compose: Phase10Compose, *, timeout_seconds: int = 30) -> Phase10Endpoints: ...
def run_command(
    argv: Sequence[str], *, environ: Mapping[str, str], timeout_seconds: int,
    stdin: bytes | bytearray | None = None,
) -> CommandResult: ...
def key_read_check_argv(
    compose: Phase10Compose, *, service: KeyBearingService,
) -> list[str]: ...
def run_phase10(*, mode: SocketMode, environ: Mapping[str, str]) -> int: ...
def cleanup_from_state(*, state_path: Path, environ: Mapping[str, str]) -> int: ...
```

All timeouts validate `1..300`; endpoint polling validates `1..180`; subprocess output is truncated/redacted to the 32 KiB diagnostic contract before entering `CommandResult`. `Phase10Compose.argv` always emits the same `-p/-f` vector and rejects project `jhin`.

`tests/integration/conftest.py` defines, rather than assumes, the live helper surface:

```python
@dataclass(frozen=True)
class OwnedCompose:
    project: str
    files: tuple[Path, ...]
    docker_socket: Path
    environ: Mapping[str, str]
    def run(self, *args: str, timeout_seconds: int = 60) -> CommandResult: ...
    def restart(self, service: str, *, timeout_seconds: int = 120) -> None: ...

@dataclass(frozen=True)
class ProcessingRow:
    origin_stream: str
    consumer_name: str
    source_stream_sequence: int
    mode: str
    handler_attempt_count: int
    claim_token: UUID | None

@dataclass(frozen=True)
class CommandRow:
    kind: Literal["outbox", "replay", "task_retry", "ordinary_task_start", "workspace_deletion"]
    id: UUID
    status: str
    attempt_count: int
    external_call_authorized_at: datetime | None
    external_identity: str | None

@dataclass(frozen=True)
class TemporalHistory:
    workflow_id: str
    status: str
    closed: bool

@dataclass(frozen=True)
class WorkspaceDeletionProjection:
    workspace_id: UUID
    status: Literal["deleting", "deleted", "blocked"]
    requested_at: datetime

def owned_compose() -> Iterator[OwnedCompose]: ...
async def wait_for_processing(
    pool: asyncpg.Pool, *, key: tuple[str, str, int], predicate: Callable[[ProcessingRow], bool],
    timeout_seconds: int = 30,
) -> ProcessingRow: ...
async def wait_for_command(
    pool: asyncpg.Pool, *, kind: str, command_id: UUID,
    predicate: Callable[[CommandRow], bool], timeout_seconds: int = 30,
) -> CommandRow: ...
async def exact_stream_message(js: JetStreamContext, *, stream: str, sequence: int) -> RawStreamMsg: ...
async def delete_exact_stream_message(js: JetStreamContext, *, stream: str, sequence: int) -> None: ...
async def terminate_postgres_claim_backend(pool: asyncpg.Pool, *, application_name: str) -> None: ...
def phase10_crash_barrier(tmp_path: Path, *, boundary: str, row_id: UUID) -> Iterator[Path]: ...
async def temporal_history(client: TemporalClient, *, workflow_id: str) -> TemporalHistory: ...
def block_dispatch_finalize(
    tmp_path: Path,
    *, kind: Literal["outbox", "replay", "task_retry", "ordinary_task_start"], row_id: UUID,
) -> Iterator[Path]: ...
async def request_workspace_delete(client: httpx.AsyncClient, *, workspace_id: UUID, csrf: str) -> WorkspaceDeletionProjection: ...
async def wait_workspace_deletion(
    pool: asyncpg.Pool, *, workspace_id: UUID,
    status: Literal["deleting", "deleted", "blocked"], timeout_seconds: int = 30,
) -> WorkspaceDeletionProjection: ...
```

`OwnedCompose.run/restart` accept only the allowlisted Phase 10 services and reconstruct the exact frozen vector; `owned_compose` reads required environment and yields nothing on mismatch. Wait predicates poll every 100 ms until a monotonic deadline and raise a fixed assertion without row payloads.

- [ ] **Step 1: Write and observe RED for every harness lifecycle primitive**

Write only `tests/test_phase10_dlq_retry_harness.py`; the script/override must not exist yet. Mock command execution and assert each run generates `jhin-p10-dlq-<12 lowercase hex>` distinct from prior calls and production/default projects. One immutable compose vector is used by every Compose command and exported as `JHIN_TEST_COMPOSE_PROJECT`, JSON `JHIN_TEST_COMPOSE_FILES`, and absolute `JHIN_TEST_DOCKER_SOCKET`: `-p <project> -f compose.yaml -f compose.dev.yaml -f <selected socket overlay> -f compose.phase10-dlq-test.yaml`. Every Compose argv begins exactly `docker --host unix://<validated-absolute-local-socket> compose`; the only non-Compose Docker argv are the exact owned-volume fallback `volume inspect` and, after label validation, `volume rm` for `<project>_master-key`. Direct unpinned `docker compose`, inherited contexts, SDK default-daemon discovery, and shell invocation are forbidden. No helper may default to, address, stop, recreate, or inspect project `jhin`; reject a missing/mismatched project/files/socket variable and never use `shell=True`.

Socket mode is explicit, never inferred after startup. `--socket-mode rootful` (the CI mode) requires `PHASE10_DOCKER_SOCKET=/var/run/docker.sock`, rejects a symlink in the final socket entry, resolves allowed parent aliases to a canonical absolute path, verifies a local Unix socket, stats it, rejects GID 0/non-numeric/mismatch, sets `SANDBOX_DOCKER_SOCKET_HOST` and exact `SANDBOX_DOCKER_GID`, selects `compose.rootful.yaml`, renders config, and asserts sandbox-runner has only that supplemental group. `--socket-mode rootless` requires explicit absolute `PHASE10_ROOTLESS_DOCKER_SOCKET`, applies the same final-entry/canonical/socket checks, verifies its owner UID is 10001, unsets `SANDBOX_DOCKER_GID`, selects `compose.rootless.yaml`, and asserts no `group_add`. Before any subprocess, reconstruct an allowlisted environment and remove every inherited `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH`, `DOCKER_CONFIG`, `COMPOSE_PROJECT_NAME`, `COMPOSE_FILE`, `COMPOSE_PATH_SEPARATOR`, and `COMPOSE_PROFILES`; create a project-private `0700` empty Docker config directory and set `DOCKER_CONFIG` to it, set `DOCKER_HOST=unix://<same canonical socket>` as defense in depth, and still pass `--host` on every command. Host/context/TLS values supplied by the caller never reach a child. A wrong-mode/GID/socket/authority preflight fails before build/up. Unit tests cover both valid modes, hostile inherited authority, final symlink/non-socket/TCP/SSH rejection, and every cleanup path; the required live PR job uses rootful with `stat -c %g /var/run/docker.sock`.

Assert lifecycle order is authority/key-memory preparation → rendered-config assertions → fresh `build --pull --no-cache` → project-scoped key-volume initialization plus three-service read checks → infrastructure/fakes `up -d --wait` → `run --rm --no-deps api jhin-db-migrate` → application/workers `up -d --wait` → endpoint resolution → pytest → `down -v --remove-orphans` in `finally` → explicit owned-volume/config-directory cleanup fallback. `uv run alembic` is forbidden because uv is absent from runtime. Hold exactly `secrets.token_bytes(32).hex().encode("ascii") + b"\n"` in a `bytearray`, never a host file, argv, environment value, state file, log, or `CommandResult`. The Compose override uses `!reset []` separately on the inherited service secret list of each key-bearing service—exactly `KEY_BEARING_SERVICES`—and `secrets: !reset {}` at top level to remove the inherited host-file secret. It declares a project-scoped `master-key` named volume, mounts only `master-key:/run/secrets:ro` into each of those three services, and sets `MASTER_KEY_FILE=MASTER_KEY_CONTAINER_PATH` on all three; no other service receives the key volume or variable. It assigns the built API service and the profile-only `phase10-key-init` service the same validated local image name `${JHIN_TEST_COMPOSE_PROJECT}-api:phase10-dlq`; the init service has no build, ports, environment, dependencies, or network, mounts only `master-key:/run/secrets` read-write, and has `network_mode: none`. After the application images are built, invoke that init service as root with `run --rm --no-deps --no-build -T phase10-key-init`; its fixed image command reads stdin with `dd status=none` into `MASTER_KEY_CONTAINER_PATH`, verifies exactly 65 bytes, then `chown 10001:10001` and `chmod 0400` without echoing or reopening the content. Then iterate the closed `KEY_BEARING_SERVICES` tuple and pass `key_read_check_argv(compose, service=service)` to `run_command`. That argv is exactly the pinned Compose vector plus `run --rm --no-deps --no-build -T --entrypoint python`, the service value, `-c`, and one constant no-output Python program. The program requires `os.geteuid()==10001`, `os.environ.get("MASTER_KEY_FILE")==MASTER_KEY_CONTAINER_PATH`, `lstat` regular/non-symlink owner UID 10001/mode `0o400`/size 65, and `os.access(path, os.R_OK)`. It then attempts only `os.open("/run/secrets/.phase10-write-probe", os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)`: the expected read-only-mount `OSError` is success; if creation succeeds it closes/unlinks that exact probe and fails. The program prints nothing and never opens the key content. A missing mount, reset/environment drift, wrong owner/mode/size, or writable directory makes the harness fail. Rendered-config assertions require the exact volume/environment result on all three key-bearing services, no top-level/service `secrets`, and no host bind under any of them; a raw-overlay unit assertion separately requires the three `!reset []` tags. `run_command(stdin=...)` never copies stdin into output/errors/diagnostics, and the harness overwrites the bytearray in `finally` on a best-effort basis. `down -v` removes the declared volume; on failure, the fallback first inspects only `<validated-project>_master-key` and requires both Compose labels `com.docker.compose.project=<project>` and `com.docker.compose.volume=master-key` before the exact pinned `volume rm`, otherwise it refuses deletion. It also removes only the owned empty Docker-config directory. Set `SANDBOX_NETWORK=<project>_sandbox`; reject `jhin_sandbox`, external networks/volumes, fixed container names, host secret binds, or named resources not project-scoped.

`key_read_check_argv` begins the `-c` program with the literal assignment `MASTER_KEY_CONTAINER_PATH = "/run/secrets/jhin_master_key"`; the `lstat` operation above is exactly `os.lstat(MASTER_KEY_CONTAINER_PATH)`. It does not rely on a host variable or interpolate a path. Unit tests compare the complete argv suffix for all three services and scan it for the hostile host-path/key canaries.

The phase override uses Compose `!override` port lists for every service inheriting a fixed publication from base/dev: web 3000, api 8000, fake-supabase-db 5432, fake-provider 8080, fake-github 8080, fake-linear 8080, fake-vercel 8080, fake-supabase 8080, sandbox-runner 8085, postgres 5432, nats 4222 and 8222, temporal 7233, and temporal-ui 8080. Every entry is `127.0.0.1::CONTAINER_PORT`. Rendered-config tests enumerate all services and fail on any fixed host port, wildcard bind, retained inherited port, external volume/network, or shared sandbox network. The harness resolves all needed ports through the exact compose vector and passes explicit `PHASE10_DATABASE_URL`, `PHASE10_NATS_URL`, `PHASE10_NATS_MONITOR_URL`, `PHASE10_TEMPORAL_ADDRESS`, `PHASE10_API_URL`, `PHASE10_WEB_URL`, `PHASE10_SANDBOX_RUNNER_URL`, `PHASE10_FAKE_PROVIDER_URL`, `PHASE10_FAKE_GITHUB_URL`, `PHASE10_FAKE_LINEAR_URL`, `PHASE10_FAKE_VERCEL_URL`, `PHASE10_FAKE_SUPABASE_URL`, and `PHASE10_FAKE_SUPABASE_DATABASE_URL`. Each value is loopback plus the resolved ephemeral port; helpers reject missing values.

Run the no-cache build for exactly the locally built services `api web workflow-worker agent-worker tool-worker sandbox-runner event-worker fake-provider fake-github fake-linear fake-vercel fake-supabase`; rendered config must show each has a build definition. First start exactly `postgres nats temporal fake-provider fake-github fake-linear fake-vercel fake-supabase fake-supabase-db`, migrate with the built API image, then start exactly `api web workflow-worker agent-worker tool-worker sandbox-runner event-worker`. That union is the required long-running service set; the profile-only `phase10-key-init` is permitted solely for the two bounded pre-up key commands and can never be started by an integration helper. No integration helper may start/stop a service outside this project/vector. All disposable project volumes/networks are created by this run and removed by `down -v --remove-orphans`.

Every subprocess timeout is `1..300` seconds and every poll deadline `1..180`; diagnostics are bounded to 32 KiB and allow only Compose `ps`, Alembic revision, NATS stream/consumer counts, Temporal workflow ID/status, command status/count, and audit action names. No service logs, environment dump, source body, credential, tool payload, DSN password, key bytes, host key path, stdin, or raw exception output is captured. Unit tests seed a key canary and hostile path canary and scan argv, child env, state JSON, stdout/stderr, exception strings, and diagnostics; both must be absent.

- [ ] **Step 2: Run harness RED**

```bash
uv run pytest tests/test_phase10_dlq_retry_harness.py -q
```

Expected: FAIL because the harness module and isolated Compose override do not exist. Do not create either before observing this failure.

- [ ] **Step 3: Implement only harness/config primitives and make their tests GREEN**

Implement exactly the contracts from Step 1. `run_phase10_dlq_retry.py` owns one frozen `Phase10Compose` value and constructs argv lists; the same value, validated socket, and scrubbed child environment are passed to key initialization, endpoint lookup, pytest, diagnostics, and teardown. Run the pinned `docker --host ... compose ... config --format json` before build and validate ports/resources/socket boundary. Run the installed `jhin-db-migrate` console script in the API image. Invoke exactly `uv run pytest -m integration tests/integration/test_phase10_dlq_retry_migration.py tests/integration/test_phase10_processing_claim.py tests/integration/test_phase10_workspace_deletion.py tests/integration/test_phase10_event_replay.py tests/integration/test_phase10_task_retry.py tests/integration/test_phase10_dlq_retry.py tests/integration/test_phase10_retry_recovery.py -v` with explicit endpoints/project/vector/socket and no inherited `PHASE10_*` or `JHIN_TEST_*` defaults. Register SIGINT and SIGTERM handlers that terminate only the currently owned pytest/Compose child process group, then enter the same `finally` path for bounded allowlisted diagnostics, pinned-daemon teardown, key zeroization, and config-directory removal. Redact DSNs before diagnostics. When `PHASE10_STATE_FILE` is supplied, atomically write only `{project,files,socket_mode}` with mode `0600` before build and remove it only after successful `down`; `cleanup_from_state` requires the caller to supply and revalidate the mode's local socket, rejects any non-Phase-10 project or unexpected file vector, reconstructs the scrubbed environment, performs the same pinned bounded down, and unlinks the state file. It never uses inherited Docker authority or a default daemon.

Add the Make target only after the RED:

```make
.PHONY: test-phase10-dlq-retry
test-phase10-dlq-retry:
	uv run python scripts/run_phase10_dlq_retry.py
```

Run `uv run pytest tests/test_phase10_dlq_retry_harness.py -q` and require GREEN before writing live tests.

- [ ] **Step 4: Write and observe RED for poison/quarantine/replay live helpers and scenarios**

First write scenario tests referencing not-yet-created typed helpers `Phase10Endpoints.from_required_env`, `owned_compose`, `OwnedCompose.restart`, `wait_for_processing`, `wait_for_command`, `exact_stream_message`, `delete_exact_stream_message`, `terminate_postgres_claim_backend`, and `phase10_crash_barrier`. Run each listed node below and record RED on the missing helper/barrier—not skip, connection refusal, or already-green behavior—before implementing that helper or its scenario support:

```bash
uv run pytest -m integration tests/integration/test_phase10_dlq_retry.py::test_poison_quarantine_commit_crashes -q
uv run pytest -m integration tests/integration/test_phase10_dlq_retry.py::test_replay_root_partial_work_and_source_expiry -q
uv run pytest -m integration tests/integration/test_phase10_dlq_retry.py::test_outbox_and_replay_attempt20_reconcile -q
uv run pytest -m integration tests/integration/test_phase10_dlq_retry.py::test_authorized_outbox_old_caller_resume_below_at_cap_and_delete -q
uv run pytest -m integration tests/integration/test_phase10_dlq_retry.py::test_authorized_replay_old_caller_resume_below_at_cap_and_delete -q
uv run pytest -m integration tests/integration/test_phase10_dlq_retry.py::test_processing_claim_renews_past_lease -q
```

Then implement those helpers in `tests/integration/conftest.py`. They require every harness endpoint plus exact `JHIN_TEST_COMPOSE_PROJECT/FILES` and `JHIN_TEST_DOCKER_SOCKET`, return typed dataclasses, never print credentials/source bodies, and never fall back to developer ports/project/daemon. `owned_compose` reverifies the local socket plus project/vector before every stop/start/restart/exec and always passes `--host`. Barriers are test-process or predecessor test-only crash barriers; add no production endpoint/flag.

Run a test-process `DurableEventConsumer` against real NATS/PostgreSQL with a unique durable name and `CountingPoisonHandler`. Its injected SQLAlchemy `before_commit` hook raises only on the first quarantine commit for the exact test stream/sequence; this is test-process injection, not an environment flag or public endpoint.

Assert attempts 1-5, durable `quarantine_only`, handler count exactly 5 after further redelivery, zero partial rows after failed commit, then one failure/outbox/audit plus `completed` after recovery. Stop the test consumer after DB commit and before term, restart it, and prove no sixth handler call. Wait for the production outbox reconciler to publish exactly the sanitized notification contract.

Through the public admin API, race two same-key replays and assert one request/event ID. Exercise partial original work twice: an INGRESS original that published only canonical index 0 before failure must replay with the same root-derived IDs and produce missing index 1; an EVENTS original that started trigger A but not B must suppress A and start only B. Remove `external_id` in both canonical passes so matcher fallback proves replay-root identity. A nonregistered event domain must be resolution-only. Publish a second poison source, delete its exact stream sequence with `js.delete_msg`, request replay, and assert failed request + expired parent + audited `source_event_expired`.

For outbox and replay separately, test counts 7 and 20. Pause the old claimant after `external_call_authorized_at`/baseline commit and immediately before its NATS await; expire the lease, let a new claimant scan `not_observed` and re-drive, then resume the old caller. Repeat after DELETE reaches `deleting`. Assert immutable message/replay/root/semantic IDs and payload, unchanged attempt count (never 21), one failure/replay audit and downstream business receipt, one semantic handler effect, and at-least-once transport only. Repeat after accepted publish/finalize block beyond 120 seconds and after terminal commit/before return. Never expect absence to exhaust an authorized row or allow cascade.

Hold a real handler for 75 seconds while lease renewal runs and redeliver after 65 seconds; assert one handler call/attempt/token. Terminate its claim-session backend with `pg_terminate_backend`, and separately restart PostgreSQL during renewal. The durable row survives; no second token may finalize over the first, stable semantic effects remain one, and workspace deletion stays `deleting` until normal redelivery establishes terminal proof.

Advance only database fixture timestamps (not wall clock) and call retention functions directly; prove completed-state stream-specific cutoffs and preservation of `handling`/`quarantine_only`.

After a workspace reaches `deleted` and its product rows have cascaded, publish a new real NATS delivery for that exact workspace before and after the source-processing retention cutoffs. Both deliveries must take the surviving terminal gate, invoke no handler, term, and create no processing/failure/outbox/audit/replay row; force one term failure and prove redelivery has the same zero-row result. Retain the gate exactly at 90 days and remove it only after the strictly-older empty-recovery scan, so this aggregate test links Task 4's after-cascade delivery behavior to Task 8's tombstone lifecycle.

- [ ] **Step 5: Write and observe RED for Temporal/task/deletion helpers, then implement scenarios**

Before adding `temporal_history`, `block_dispatch_finalize`, `request_workspace_delete`, or deletion polling helpers, write the tests below and observe the expected missing-helper/barrier RED independently:

```bash
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_manual_retry_start_snapshot_crashes -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_authorized_temporal_old_caller_resume_below_at_cap_and_delete -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_accepted_calls_reconcile_before_current_gates -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_workspace_deletion_survives_connection_and_service_restart -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_ordinary_task_authority_races_workspace_delete -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_ordinary_start_repair_ignores_projected_task_state -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_active_ordinary_workflow_blocks_cascade_until_terminal_proof -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_post_close_terminal_proof_waits_for_actual_close -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_deleted_agent_closes_without_run_then_clears_input -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_agent_delete_snapshot_lock_winners_wait_for_proof -q
uv run pytest -m integration tests/integration/test_phase10_retry_recovery.py::test_task_retry_terminal_retention_waits_for_workflow_close -q
```

Implement only after those REDs, using the owned project/vector and five-second call timeouts.

Use the fake provider to finish a pure-read-only task in failed state and request retry. Race two same-key POSTs; assert one row/workflow/new run. Assert the pre-existing ordinary Task workflow ID/authorization is unchanged and retry identity lives only on TaskRetry. A manual-only queued binding with no authorized/unaccepted ordinary authority is absent from ordinary reconciliation; if a version-1 authorized/unaccepted/uncleared ordinary authority also exists, however, the authority-only query must repair it regardless of that same Task's manual queue metadata and manual dispatch must wait for ordinary proof. Send an allowed signal only after the retry run is active and prove routing selects that retry ID; malformed/ambiguous binding fails closed. Purge terminal attempt 2, request again, and prove the Task counter allocates attempt 3 without ID reuse. For a failed task with no run/DB workflow ID, seed an open base `task-{task_id}` history and assert blocked; repeat with a closed unlinked base history and assert ambiguous-effect blocked; NotFound is eligible. Seed task/run/retry IDs and prove each bounded history is described.

Stop Temporal before dispatcher start using `owned_compose`, assert a never-authorized command remains requested/queued, restart Temporal, and assert deterministic start. Hold dispatcher finalization until snapshot activity runs; it must accept dispatching, project started/audit once, and create one run before the finalizer resumes idempotently. Repeat for ordinary non-retry snapshot commit/response crash.

For the ordinary start-repair RED, commit a version-1 authorized/unaccepted/uncleared authority and due repair claim, let Temporal accept, then crash the DB accepted-at finalization. Allow the real workflow to project Task/AgentRun `running`, and in a second case terminal, before the repair loop resumes; also mutate assignment, description, and queue metadata. The authority-only batch must still select the row from its persisted repair columns, describe the exact open/closed history, atomically persist acceptance and schedule terminal reconciliation, then—only after actual close—persist binding-validated proof, clear the immutable input, and let workspace deletion converge. Assert no Task state, Agent FK, manual metadata, or product projection suppresses the scan or substitutes a retry identity.

Then at counts 7 and 20 pause the old claimant after the version-1 `AgentTaskInput` contract plus authorization/increment commit and before Temporal start. First leave the authorized Agent live, mutate Task instruction/assignment to a second live Agent, expire the lease, let the reconciler see NotFound and call the exact same ID/input with `REJECT_DUPLICATE`, then resume the old caller. Assert byte-equal submitted/history input, one workflow/audit, exactly one run owned by the originally authorized Agent, unchanged count/no 21, and no instruction/input in public or telemetry sinks. Repeat while deletion drains; pause the final projection before workflow return and prove Task/AgentRun terminal state cannot produce close proof, clear input, or permit cascade. Make describe unavailable/ambiguous, then restore it after actual close; only the post-close reconciler may validate the exact binding and atomically persist actual status/proof/input-clear.

Run a separate attempt-7/20 case that races Agent DELETE against snapshot with the shared `Workspace -> RecoveryWorkspaceDeletion -> Agent` prefix. When deletion wins, it commits before snapshot and the already-authorized old/new callers still submit byte-identical input and create one workflow history, but snapshot observes the missing Agent, Task becomes `failed` with `snapshot_failed`, the caught path returns with Temporal status `completed`, and there are **zero AgentRun rows**. After the workflow actually closes, the post-close history scan must prove no successful snapshot/run/step/tool/delegation, write `completed` plus `closed_before_run`, and clear input; until then deletion remains draining. When snapshot wins, hold the Agent lock through exact AgentRun insert/commit; DELETE must wait, then return fixed 409 with no audit/delete/cascade while the run or its ordinary/manual close proof is pending. Terminal product projection is insufficient. After exact post-close proof/input-clear commits, retry DELETE and assert one existing sanitized audit plus cascade. Run both winners for ordinary and manual starts, terminate each side's PostgreSQL backend at the barrier, and prove reconnect cannot invert the committed winner, erase proof, or cascade early. This also proves Task-owned input/ID never crosses into manual retry authority. Terminate PostgreSQL after closed describe but before proof commit and prove exact idempotent recovery for both owners.

Create failed attempts containing completed non-idempotent and `execution_unknown` calls using the existing tool-worker fake/crash barriers from subproject 1; assert POST 409 and no UI/API eligibility. A completed `pure` read remains eligible. Revoke requester membership and disable the agent after command creation in separate cases; dispatcher rejects without a workflow and returns task failed.

For replay publish and Temporal start separately, authorize the exact external call, terminate the dispatcher's PostgreSQL backend before finalization, then revoke membership, mark source expired/agent disabled, and request workspace deletion. Restart PostgreSQL/event-worker. Accepted evidence finalizes/audits once below and at cap. `not_observed`/NotFound re-drives only the authorized identity and never rechecks those prospective gates; if exact replay bytes are no longer recoverable, deletion remains draining. Separately crash **before** the authorization transaction: only that null-authorization row may recheck current gates and terminalize with no call.

Race replay POST and every ordinary API mutation against DELETE under real PostgreSQL: unassigned/assigned task create, agent assign-task/message, task pause/resume/cancel/instruction, and approval decision. Prove the workspace/deletion lock winner; DELETE-first yields fixed 409 and no new task/message/signal/start, while a mutation that authorized first is preserved. For an assigned create/start accepted with client/DB ambiguity, inventory must find the persisted task ID/start authorization and exact history even after close. Seed canonical/Task/AgentRun ID collisions and prove fieldwise linkage; conflicting histories block.

Race deletion against a live handler, outbox/replay publish, manual retry start, ordinary authorized start, a trigger whose exact name/version/input authorization committed before Temporal but whose NATS delivery is gone, and an already-active ordinary workflow performing normal/tool effects. Deletion returns `202/deleting`, never cancels or terminalizes paused/unknown work, and never removes product rows. Terminate the relevant PostgreSQL backends and restart API/event-worker/NATS/Temporal in the owned project. Resume old callers only after new reconcilers have re-driven the exact identities. Assert stable IDs, one business workflow/effect, no count 21, no false exhausted, and no cascade until the trigger plus every ordinary/retry workflow is actually closed and its independent post-close reconciler has validated the deterministic binding and committed terminal proof/input clear. A terminal Task/AgentRun projection while its workflow is open is insufficient. Persist the retry's exact generation/workflow/run-or-closed-before-run terminal proof before cascade; after Task/AgentRun deletion, retention must converge from that surviving proof. Pending DLQ still publishes its sanitized at-least-once notification. All waits are bounded and diagnostics allowlisted.

Advance database timestamps and close the exact retry workflow; prove terminal task retry is retained while open, while close acknowledgement is delayed after product projection, and while post-close describe is unavailable/ambiguous. It may purge only 90 days after the later actual-close proof/input-clear commit, audited once; then the deleted gate purges only after every recovery row is gone.

- [ ] **Step 6: Add the required PR target and run the isolated live suite**

Add `test-phase10-dlq-retry` to `Makefile` exactly as Step 3 and a required `phase10-dlq-retry` job to `.github/workflows/ci.yml` under `pull_request`. It checks out, installs uv/Node/pnpm, verifies `/var/run/docker.sock` is a nonsymlink Unix socket, computes `socket_gid=$(stat -c %g /var/run/docker.sock)`, rejects `0`, exports exact `PHASE10_DOCKER_SOCKET=/var/run/docker.sock`, `SANDBOX_DOCKER_GID`, and `PHASE10_STATE_FILE="$RUNNER_TEMP/jhin-phase10-dlq-state.json"`, and runs `uv sync --frozen --all-packages`, `pnpm install --frozen-lockfile`, then `make test-phase10-dlq-retry PHASE10_SOCKET_MODE=rootful`. The job has a 45-minute timeout and an `if: always()` step with the same socket/mode environment running `uv run python scripts/run_phase10_dlq_retry.py --cleanup-state "$PHASE10_STATE_FILE"`; missing state is a successful no-op, and a retained state can control only the validated unique Phase-10 project/vector on that revalidated local socket. The harness remains primary teardown owner. The workflow check name is exactly `Phase 10 DLQ / retry live recovery` for required branch protection.

```bash
make test-phase10-dlq-retry
```

Expected GREEN from a unique fresh build/migration with all fake services: no lease steal/sixth handler, one atomic failure/outbox/audit, stable replay-root partial-work recovery, forbidden effect retries absent, isolated immutable ordinary/manual IDs and byte-equal stored inputs across product mutation, ordinary accepted-at repair selected after running/terminal projection, exactly one original-agent run for live-agent field drift, Agent-delete/snapshot both-winner serialization with snapshot-first proof-gated 409 and deletion-first zero runs plus safe `snapshot_failed`/`closed_before_run` proof, one reject-duplicate trigger/ordinary/retry workflow history, ordinary/retry snapshot handshakes, actual-close post-close proof/input clearing, authorized old-caller/reconciler races below and at 20 without a different effect or count 21, at-least-once >120-second DLQ/replay transport with durable/semantic dedupe, complete ordinary/trigger workflow deletion inventory, after-cascade deleted-gate term with zero new rows, durable retry terminal proof/input clear and monotonic generation, deletion through backend/service restarts, NATS/Temporal reconnect, and exact failure/task-retry/deletion retention. Rendered config and no-output runtime checks must prove exactly API, agent-worker, and tool-worker read the same UID-10001-owned read-only key path. The script must report successful pinned-daemon `down -v --remove-orphans`, owned key-volume removal, and in-memory key zeroization even when pytest fails.

- [ ] **Step 7: Commit exact scope**

```bash
git add tests/integration/conftest.py tests/integration/test_phase10_dlq_retry.py tests/integration/test_phase10_retry_recovery.py tests/test_phase10_dlq_retry_harness.py scripts/run_phase10_dlq_retry.py compose.phase10-dlq-test.yaml Makefile .github/workflows/ci.yml
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "test: prove dlq and retry recovery"
```

---

### Task 10: Document Operator Semantics and Run the Full Subproject Gate

**Files:**
- Create: `docs/operations/dlq-and-task-retry.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: implemented state transitions, roles, retention, source expiry, UI actions, NATS/Temporal recovery, and test commands.
- Produces: exact operator runbook, complete verification/staging evidence, and clean handoff to Phase 10 subproject 5.

- [ ] **Step 1: Write the operator contract**

Document:

- where admins find Operations → Event failures and task retry history;
- five database-counted handler attempts, unlimited JetStream delivery, lease expiry, `quarantine_only`, atomic failure/outbox/audit/completed, and why redelivery cannot run a sixth handler;
- failure reason/action mapping, protected-health open count/oldest age, bounded replay history, exact source-retention windows (INGRESS 7 days, EVENTS 14 days), replay expiry, replay-root identity across ingress derivation/matcher fallback, original correlation/causation, and partial-original-work dedupe;
- admin vs member permissions, CSRF/idempotency, no raw-message/download/bulk/delete endpoint;
- automatic activity retry vs fresh manual task retry, current configuration resolution, the no-override rule for committed/ambiguous effects including `execution_unknown`, immutable separation of the Task's ordinary workflow ID/input authority from TaskRetry's ID/input authority, versioned bounded `AgentTaskInput` persistence before either Temporal call, why every authorized/unaccepted/uncleared ordinary authority remains repairable from its own due/claim columns even after Task state, assignment, queue metadata, or workflow projection changes, why later Task mutation or Agent deletion cannot change a re-drive, the live-agent field-drift path's one original-agent run versus deleted-agent safe `snapshot_failed` with zero runs, active-retry signal routing, why an in-workflow final projection cannot certify the workflow close, independent post-close describe/binding validation, proof-atomic sensitive-input clearing only after actual closure, monotonic retry generation after history retention, and durable terminal proof before deletion;
- Agent deletion and snapshot serialize on `Workspace -> RecoveryWorkspaceDeletion -> Agent`: deletion-first gives the bounded zero-run `closed_before_run` path, while snapshot-first returns fixed retryable 409 with no delete audit/cascade until the exact ordinary/manual post-close proof and input clear are durable; terminal product projection alone is not deletion authority;
- command statuses/leases/backoff, the exact reachable preauthorization-attempt transitions, immutable authorization timestamps, why lease expiry/NotFound/scan miss cannot disprove a paused caller, same-identity re-drive below/at 20, persisted trigger name/input plus trigger/ordinary/manual `REJECT_DUPLICATE` after workflow close, ordinary/retry dispatch-snapshot handshake, and safe recovery steps for PostgreSQL, NATS, Temporal, event-worker, and database-connection restarts;
- DLQ notification is at-least-once beyond JetStream's 120-second duplicate window; downstream consumers durably dedupe the stable `message_id`, while PostgreSQL failure/outbox rows remain authoritative;
- durable workspace deletion behavior: `202/deleting` bars recovery claims and ordinary task/start/assign/message/signal authority, authorized/unknown calls reconcile or re-drive exact identities without current gates, canonical/Task/every-AgentRun/pre-task-TriggerInvocation workflow inventory must prove closure and terminal runs, no accepted/authorized effect is cancelled/terminalized, a delivery after final `deleted` terms against the gate without creating recovery rows, and recovery/deletion history survives until bounded retention;
- completed processing-state retention (14 days INGRESS, 21 days EVENTS), terminal failure/task-retry 90-day retention, workflow-closure proof, and final deleted-gate cleanup;
- `0016 -> 0015` is schema-reversible but intentionally drops Phase 10 command/history columns and tables: perform it only before admitting Phase 10 work or under an explicit maintenance backup/destructive rollback procedure; a later re-upgrade recreates empty recovery state/default counters and must not be represented as restoring dropped authorization timestamps or retry generations;
- isolated live harness socket-mode preflight, pinned local Docker socket with inherited authority scrubbed, unique project/file vector/dynamic endpoints/key volume/sandbox network, exact read-only constant `MASTER_KEY_FILE` mount and UID-10001 readability in all three key-bearing services (`api`, `agent-worker`, `tool-worker`) without key material in host files/args/env/diagnostics, rootful exact socket GID versus explicit rootless ownership, installed `jhin-db-migrate`, and guaranteed bounded teardown;
- commands for focused/unit/live tests and sanitized evidence to collect.

The README adds one Operations/runbook link and does not expose internal NATS/Temporal URLs or imply viewers can inspect failures.

- [ ] **Step 2: Run the focused backend/frontend gate**

```bash
uv run pytest packages/domain/tests packages/policy/tests packages/db/tests packages/recovery/tests packages/events/tests services/event_worker/tests apps/api/tests/test_idempotency.py apps/api/tests/test_event_failures.py apps/api/tests/test_task_retry.py apps/api/tests/test_workspace_recovery_delete.py apps/api/tests/test_task_workflow_deletion_gate.py apps/api/tests/test_approvals_unit.py apps/api/tests/test_operations_health.py apps/api/tests/test_webhooks_unit.py packages/workflows/tests/test_agent_task_tool_routing.py services/agent_worker/tests/test_task_retry_admission.py tests/test_phase10_dlq_retry_harness.py -q
pnpm --filter jhin-web test -- recovery-contract-parity.test.ts docker-build-context.test.ts event-failure-panel.test.tsx operations-page.test.tsx task-retry-card.test.tsx task-detail-retry.test.tsx
docker build --target build -f apps/web/Dockerfile -t jhin-web-phase10-final .
```

- [ ] **Step 3: Run repository static and regression gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web build
docker build --build-arg SERVICE_PACKAGE=jhin-api -f docker/python.Dockerfile -t jhin-api-phase10 .
docker build --build-arg SERVICE_PACKAGE=jhin-event-worker -f docker/python.Dockerfile -t jhin-event-worker-phase10 .
docker build --build-arg SERVICE_PACKAGE=jhin-agent-worker -f docker/python.Dockerfile -t jhin-agent-worker-phase10 .
```

- [ ] **Step 4: Run real migration and recovery acceptance**

```bash
uv run pytest -m integration tests/integration/test_phase10_dlq_retry_migration.py tests/integration/test_phase10_processing_claim.py tests/integration/test_phase10_workspace_deletion.py tests/integration/test_phase10_event_replay.py tests/integration/test_phase10_task_retry.py -v
make test-phase10-dlq-retry
uv run pytest -m integration tests/integration/test_nats_durability.py tests/integration/test_temporal_durability.py tests/integration/test_phase9_exit.py -v
```

- [ ] **Step 5: Run explicit security/contract scans**

```bash
rg -n 'raw_payload|raw_message|request_body|response_body|stack_trace|traceback|authorization|cookie|secret|prompt|tool_input' packages/db/src/jhin_db/models/recovery.py apps/api/src/jhin_api/operations apps/web/components/event-failure-panel.tsx
rg -n 'max_deliver|processing_max_attempts|quarantine_only|source_event_expired|execution_unknown|non_idempotent' packages/events services/event_worker apps/api apps/web docs/operations/dlq-and-task-retry.md
! rg -n 'workspace_dispatch_fence|pg_advisory_lock|tombstone|workspace.*budget|uv run alembic' packages/recovery/src services/event_worker/src apps/api/src scripts/run_phase10_dlq_retry.py compose.phase10-dlq-test.yaml docs/operations/dlq-and-task-retry.md
uv run python -c 'from pathlib import Path; bad=[]; roots=[Path("apps/api/src"),Path("services/event_worker/src"),Path("services/agent_worker/src")]; text="\n".join(p.read_text() for r in roots for p in r.rglob("*.py")); assert "from jhin_event_worker" not in text and "from jhin_api" not in "\n".join(p.read_text() for r in roots[1:] for p in r.rglob("*.py"))'
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git status --short
```

Expected: the first scan shows only explicit forbidden-key tests/comments, not stored/returned fields; the second finds every safety contract; the obsolete-contract scan returns no matches; import assertions pass; `cmp` exits zero, proving both bytes and size still match the pre-Task-0 checkpoint even though OrgForge is untracked.

- [ ] **Step 6: Stage documentation only and commit**

```bash
git add docs/operations/dlq-and-task-retry.md README.md
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: explain dlq and retry recovery"
```

Expected cached paths: the two documentation paths only.

- [ ] **Step 7: Final clean-scope audit**

```bash
git status --short
git log --oneline --decorate -11
git diff HEAD^ --check
{ shasum -a 256 orgforge-production-implementation-plan.md; wc -c orgforge-production-implementation-plan.md; } | cmp - "$(git rev-parse --git-path phase10-dlq-orgforge.checkpoint)"
```

Expected: no unstaged task files, no unrelated staged files, and the task commits appear in order. Do not squash unless the repository owner requests it.

## Acceptance Matrix

| Requirement | Primary task | Proof |
| --- | --- | --- |
| DB-counted attempts, renewable fenced leases, no sixth/stolen handler call | 3, 9 | >60-second concurrent redelivery + backend/restart races |
| Simultaneous missing-row first claim is race-safe | 3 | real PostgreSQL barrier: one attempt-1 handle + one lease-busy |
| Subject workspace and complete origin suffix match the envelope before handlers | 3-5 | EVENTS/INGRESS spoofed workspace/event/connector zero-handler matrix; staged sink-by-sink `<invalid-subject>`/foreign-canary proof |
| `quarantine_only` survives commit failure; atomic failure/outbox/audit/completed | 4, 9 | injected commit failure against real PostgreSQL |
| Completed redelivery skips handler and terminates | 3, 4, 9 | post-commit/pre-term recovery test |
| Unlimited JetStream delivery and PostgreSQL outage behavior | 3, 9 | consumer config + DB outage/no-handler assertion |
| Sanitized at-least-once DLQ notification and durable `message_id` receipt dedupe | 4, 9 | >120-second finalize crash + downstream unique receipt |
| DLQ publication preserves telemetry-core producer spans/trace headers without payloads | 4 | parent/span/header and canary exclusion tests |
| EventPublisher preserves predecessor keyword headers contract | 3 | predecessor telemetry suite + publish/publish-to-subject header merge tests |
| Admin RBAC, cursor filters, detail, resolve, no raw access | 5 | API role/isolation/schema tests |
| Replay idempotency, concurrent same-key binding, deterministic root, source expiry | 3, 5, 9 | post-lock key reload before mutable state, equality/mismatch + defensive unique reload, exact source delete |
| Partial original work replays only missing ingress/trigger work | 3, 5, 9 | root-derived IDs + two-trigger live fixtures |
| Trigger starts persist immutable name/input before Temporal and reconcile independent of NATS | 2, 3, 4, 9 | ack-loss/restart/delete + ambiguous-client/closed-workflow tests |
| Historical retry safety and low-risk/non-idempotent distinction | 1, 2 | exact catalog partition + persisted ToolCall value |
| Conservative manual retry including execution_unknown/no override | 6, 7, 9 | safety matrix, API/UI absence, live barrier case |
| Authorized external calls reconcile/re-drive before and without current gates | 4-6, 9 | paused old caller + new same-ID caller, revocation/deletion/expiry below + at cap |
| Prospective authorization/config/agent-budget/concurrency and requester recheck | 5, 6, 9 | null-authorization transition matrix; no workspace budget authority |
| Base/task/run/retry workflow histories checked without DB-run assumptions | 6, 9 | bounded ID matrix + unlinked base ambiguity |
| API-failed vs dispatcher-exact-queued eligibility stays separate | 6 | mode-specific state/binding matrix |
| Manual retry never overwrites the ordinary Task binding or crosses dispatcher/signal routing | 6, 9 | post-lock same-key reload, ordinary/manual race + exact queue/run binding tests |
| Ordinary/manual starts use immutable versioned bounded `AgentTaskInput` contracts | 2, 4, 6, 8, 9 | live-agent field drift gives one original-agent run; accepted-at finalize crash remains repairable after running/terminal projection; deleted-agent stale callers give byte-equal history, zero runs, safe failure, then post-close `closed_before_run` clear below/at cap |
| Agent delete and snapshot/run creation serialize without erasing terminal evidence | 4, 6, 9 | real PostgreSQL both-winner race: deletion-first zero-run close; snapshot-first fixed 409/no audit until ordinary/manual post-close proof, then one delete/cascade |
| Retry generation never reuses IDs after retained command cleanup | 2, 6, 8, 9 | locked Task counter + purge/next-attempt test |
| Retry terminal proof survives product cascade and permits 90-day convergence | 2, 4, 6, 8, 9 | final projection-before-return cannot prove close; delayed/ambiguous post-close describe + proof-commit crash/delete/retention tests |
| Deterministic reject-duplicate Temporal start and start/snapshot handshake | 4, 6, 9 | authority-only repair after running/terminal projection, blocked finalizer + closed AlreadyStarted + one audit/run or deletion-first zero-run safe close, followed by independent post-close proof |
| All three command kinds have reachable preauthorization counts, stop at 20, and re-drive only same identity | 4-6, 9 | persisted transient 0..20 + old-caller/lease-expiry/new-reconciler/resume below/at cap; no count 21 |
| Fresh snapshot and retry/run linkage, including ordinary runs | 2, 6 | retry and non-retry activity commit-crash reattachment tests |
| Admin DLQ/history and member task retry UI | 7 | role/fetch/render/mutation Vitest suites |
| Bounded latest/history replay outcome survives reload | 5, 7 | 25-generation API bound + polling/reload UI test |
| Shared recovery codes/copy and admission/reconciliation have no service-local forks | 1, 2, 5-7 | neutral wheel/import boundaries + JSON/TypeScript parity |
| Protected health exposes scoped bounded open count/oldest age | 5, 7 | foreign/capped/future projection tests |
| Workspace deletion survives lost connections and never crosses live/unknown work | 2, 4, 5, 6, 9 | durable deleting gate + NATS/Temporal/PG restart races + actual-close post-close proof/input-clear before cascade |
| Ordinary task/workflow authority is deletion-gated and fully inventoried | 2, 4, 9 | create/start/assign/message/signal races; canonical/Task/every AgentRun/pre-task TriggerInvocation ID; terminal-run or bounded `closed_before_run` post-close proof |
| Outbox claims never invert deletion lock order | 4, 9 | unlocked candidate scan then Workspace → gate → outbox lock/revalidate; real PostgreSQL DELETE race |
| Replay POST/delete and first processing claims serialize on durable gate | 3, 5, 9 | real PostgreSQL winner/drain matrix |
| Post-cascade delivery and processing/failure/task-retry/deleted-gate retention | 4, 8, 9 | real NATS term with zero new rows + exact boundaries + proof/input-clear + delete-to-90d convergence |
| Fresh, `0015 -> 0016`, downgrade/re-upgrade migration | 2 | real PostgreSQL schema plus preserved-0015-data semantics; dropped Phase10 data not falsely asserted |
| Minimal real NATS/Temporal recovery is RED before owning implementation | 3, 4, 5, 6 | task-local unique clients/streams/workflows and integration RED commands; no Task 9 helper import |
| NATS/Temporal recovery without third-party services | 9 | fresh unique Compose project using fake provider/connectors |
| Every intermediate commit leaves event-worker runnable | 3-6 | deferred quarantine bridge, then outbox/replay/retry loop tests |
| Live harness is rootful/rootless validated, pinned, isolated, and always tears down | 9 | local socket/GID/owner, authority scrub, rendered all-port override, UID10001 read-only key volume on API/agent/tool, unique vector/network, failure paths |
| Web canonical JSON survives standalone Docker build | 7 | pre-copy Docker RED + clean-context build/smoke |
| Required PR CI executes the fresh live harness | 9 | named rootful job and Make target |
| Untracked OrgForge is byte-for-byte preserved | 0-10 | private baseline SHA-256+size `cmp` before every commit/final |
| Audits for failure/replay/resolve/task retry | 4-6, 9 | exact append-only action/count tests |
