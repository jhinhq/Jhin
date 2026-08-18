# Conversations and Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide multiple named, persistent chats per agent and safe long-term memory that follows an agent across chats while preserving Jhin's durable per-turn task engine.

**Architecture:** Conversations and visible messages become first-class user-facing records. Each user turn idempotently creates one visible message and one linked `Task`, then starts the unchanged Temporal task workflow. Conversation history spans tasks, while internal tool transcript stays task/run-local. Memory is immutable/versioned and source-attributed; deterministic policy owns screening, scope, promotion, contradiction, and retrieval. PostgreSQL full-text is always available, with pgvector semantic ranking when an embedding profile is configured.

**Tech Stack:** Python 3.13, SQLAlchemy/Alembic, PostgreSQL 17 + pgvector, FastAPI, Temporal, Pydantic, existing model adapters, Next.js 16/TanStack Query, pytest/Vitest.

**Spec:** `docs/superpowers/specs/2026-08-17-jhin-ai-company-experience-design.md`

## Global Constraints

- Do not change task workflow IDs, `AgentTaskInput`, `DelegatedTaskInput`, `Task.parent_task_id`, or Phase 8 delegation authorization.
- Existing task/message endpoints remain compatible through the final route cutover.
- A retry carrying the required redesigned-client idempotency key produces at most one user message, task, and agent reply. The legacy endpoint without a key intentionally retains create-new behavior.
- Conversation and message visibility use one shared resource-visibility resolver consumed by APIs, runtime history, memory, rollups, and activity.
- Visible conversation history and private task/run/tool history are queried separately.
- Every run has an immutable triggering-message cutoff; delegates, helpers, and reviewers receive only explicit handoff/evidence projections, never unrelated earlier chat turns.
- Delegated work projects source-linked summaries into the root conversation; raw child tool transcripts are not copied.
- Raw transcripts are never durable memory.
- Memory activation, scope broadening, and retrieval are deterministic authorization decisions, not model decisions.
- Memory visibility can never exceed source visibility.
- Hidden reasoning, provider scratchpads, secrets, and authorization headers are never stored as memory.
- Memory maintenance failure never fails the originating chat turn.
- Release 1's generic durable `WorkflowCommand` outbox owns both `start` and `signal` commands. A command has a deterministic command ID, target workflow ID/type, command kind, payload, delivery state, and retry metadata; commit-to-Temporal failure, start-response loss, and signal-response loss cannot strand or duplicate work.

---

### Task 1: Add first-class conversation persistence and compatibility backfill

**Files:**
- Create: `packages/db/src/jhin_db/models/conversation.py`
- Modify: `packages/db/src/jhin_db/models/work.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Modify: `packages/domain/src/jhin_domain/enums.py`
- Modify: `packages/domain/src/jhin_domain/__init__.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260817_0016_conversations.py`
- Create: `packages/db/tests/test_conversation_models.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Modify: `packages/domain/tests/test_enums.py`

**Interfaces:**
- `Conversation(workspace_id, title, status, created_by_user_id, primary_agent_id, pinned_at, archived_at, last_activity_at)`
- `Conversation` additionally stores `creator_type` (`user`, `agent`, `system`), nullable typed creator IDs, `visibility` (`participants`, `workspace`), and `next_message_seq`.
- `ConversationParticipant(conversation_id, workspace_id, participant_type, user_id, agent_id, role, joined_at, left_at)` with exactly one typed identity.
- `AgentInboxItem(workspace_id, target_agent_id, source_kind="message", message_id, dedupe_key, status, attempts, claim_token, lease_expires_at, last_error, created_at, completed_at)` is the persistent wakeup source for direct messages and is extensible by Release 3 for work requests. In `0016`, `message_id` is required and has a composite `(workspace_id, message_id)` foreign key; the source-kind check permits only `message`. The row stores no copied chat history or memory and is unique by `(workspace_id, dedupe_key)`.
- Nullable `Task.conversation_id`, `Task.trigger_message_id`, `Task.conversation_cutoff_seq`, and `Task.conversation_access_mode` (`full`, `handoff_only`).
- Nullable `Message.conversation_id`, `Message.conversation_seq`, `Message.dedupe_key`, `Message.in_reply_to_message_id`, `Message.audience_type`, and `Message.audience_id`.

- [ ] **Step 1: Write failing schema tests**

Cover one primary agent, user/agent/system creator shape, human/agent participant identity checks, editable title, pin/archive state, monotonic conversation sequence, immutable task cutoff/access mode, audience shape, nullable compatibility fields, unique `(workspace_id, dedupe_key)` for visible messages, and idempotent workspace-scoped message inbox items with valid target agents/status/claim-lease fields. PostgreSQL tests must reject cross-workspace participant/task/message/inbox-message links and inconsistent task/conversation message lineage.

```bash
uv run pytest packages/db/tests/test_conversation_models.py packages/domain/tests/test_enums.py -q
```

Expected: FAIL because the types/tables are missing.

- [ ] **Step 2: Define models and indexes**

Index active conversations by workspace/last activity and by primary agent. Require exactly one creator/participant identity column for each type. Add composite `(workspace_id, id)` targets/foreign keys for conversation-owned edges and checks aligning message task/conversation lineage. Index pending inbox items by `(workspace_id, target_agent_id, status, created_at)` and keep polymorphic source resolution in the shared visibility/service layer. Keep new task/message foreign keys nullable during compatibility.

- [ ] **Step 3: Write the additive migration and backfill**

Create `20260817_0016_conversations.py` with `revision="0016"` and `down_revision="0015"`. Create one archived-compatible conversation for each existing root task with user-facing activity. Derive the primary agent from the root assignment when available; use `creator_type=system` and nullable creator IDs for triggered/agent-only legacy work. Link root-task visible user/agent messages plus allowlisted structured delegation/review/result projections only. Descendant raw agent messages and all tool transcripts remain task-local and conversation-null. Backfill participant rows explicitly, generate deterministic titles, and do not start workflows during migration. Add an upgrade fixture with child visible/tool messages and prove they are not disclosed. Extend the migration graph assertion to `0013 -> 0014 -> 0015 -> 0016`.

- [ ] **Step 4: Verify schema and migration**

```bash
uv run pytest packages/db/tests/test_conversation_models.py packages/domain/tests/test_enums.py -q
uv run pytest packages/db/tests/test_migration_graph.py -q
```

Expected: PASS and one migration head.

- [ ] **Step 5: Commit conversation persistence**

```bash
git add packages/db/src/jhin_db/models packages/db/src/jhin_db/alembic/versions/20260817_0016_conversations.py packages/db/tests/test_conversation_models.py packages/db/tests/test_migration_graph.py packages/domain
git commit -m "feat: add persistent conversations"
```

### Task 2: Implement the shared resource-visibility resolver

**Files:**
- Create: `packages/access/pyproject.toml`
- Create: `packages/access/src/jhin_access/__init__.py`
- Create: `packages/access/src/jhin_access/types.py`
- Create: `packages/access/src/jhin_access/visibility.py`
- Create: `packages/access/src/jhin_access/py.typed`
- Create: `packages/access/tests/test_visibility.py`
- Modify: `pyproject.toml`
- Modify: `apps/api/pyproject.toml`
- Modify: `packages/agents/pyproject.toml`
- Modify: `packages/tools/pyproject.toml`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `docker/python.Dockerfile`
- Modify: `uv.lock`

**Interfaces:**
- `Principal(kind, workspace_id, user_id=None, agent_id=None, workspace_role=None)`.
- `can_read_conversation(session, principal, conversation_id) -> VisibilityDecision`.
- `visible_message_predicate(principal, conversation, task=None, cutoff_seq=None)`.
- `assert_source_visibility(session, principal, SourceRef) -> VisibilityDecision` reused by memory, rollups, reviews, and activity.

- [ ] **Step 1: Write failing visibility matrix tests**

Test participant user, primary agent, owner/admin administrative read, member/viewer nonparticipant 404, workspace-visible conversation, departed participant cutoff, transient delegate/helper/reviewer evidence-only access, direct audience, task audience, workspace audience, cross-workspace 404, source visibility ceiling, and relationship/team/manager non-authority.

```bash
uv run pytest packages/access/tests/test_visibility.py -q
```

Expected: FAIL because the shared resolver does not exist.

- [ ] **Step 2: Implement one database-aware resolver**

Keep pure capability evaluation in `jhin-policy`; `jhin-access` depends on DB/domain and resolves resource audiences. Default conversations are participants-only. Owner/admin may perform audited administrative reads; ordinary member/viewer access still requires participation or workspace visibility. Primary agents receive full history only through their task cutoff. Transient workers receive explicit handoff/review/request projections and messages addressed to their task/agent, never whole-chat history.

- [ ] **Step 3: Wire the package into consumers**

Add the package to uv workspace, Ruff, mypy, pytest, API, agents, tools, and agent-worker manifests/sources plus Docker manifest copy. Refresh the lock and verify frozen all-package sync.

- [ ] **Step 4: Run access/package gates**

```bash
uv run pytest packages/access/tests/test_visibility.py -q
uv sync --frozen --all-packages
uv run mypy packages/access/src
```

Expected: PASS.

- [ ] **Step 5: Commit shared visibility**

```bash
git add packages/access pyproject.toml apps/api/pyproject.toml packages/agents/pyproject.toml packages/tools/pyproject.toml services/agent_worker/pyproject.toml docker/python.Dockerfile uv.lock
git commit -m "feat: centralize resource visibility"
```

### Task 3: Implement conversation CRUD, search, and idempotent turns

**Files:**
- Create: `apps/api/src/jhin_api/conversations/__init__.py`
- Create: `apps/api/src/jhin_api/conversations/schemas.py`
- Create: `apps/api/src/jhin_api/conversations/service.py`
- Create: `apps/api/src/jhin_api/conversations/router.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `apps/api/src/jhin_api/tasks/schemas.py`
- Modify: `apps/api/src/jhin_api/tasks/service.py`
- Modify: `apps/api/src/jhin_api/tasks/router.py`
- Modify: `apps/api/src/jhin_api/agents/router.py`
- Modify: `services/event_worker/src/jhin_event_worker/workflow_commands.py`
- Modify: `services/event_worker/tests/test_workflow_commands.py`
- Create: `apps/api/tests/test_conversations_unit.py`

**Interfaces:**
- `POST/GET /api/v1/workspaces/{workspace_id}/conversations`
- `GET/PATCH /api/v1/workspaces/{workspace_id}/conversations/{conversation_id}`
- `POST /.../{conversation_id}/turns` with `{text, client_message_id, remember={enabled, requested_scope?}, memory_required=false}`; `client_message_id` is required UUID/ULID text and is the idempotency key. `remember` is caller-owned user/API intent: `enabled` is required, `requested_scope` is optional and validated against the caller's authority; neither field is inferred from model output.
- `GET /.../{conversation_id}/messages?cursor=&limit=`.
- `ConversationTurnOut(conversation, user_message, task)`.
- Existing `POST /agents/{agent_id}/message` accepts an additive `Idempotency-Key` header or `client_message_id` body field, creates/reuses one conversation for that key, and returns its existing `TaskOut` shape plus an additive conversation ID.
- Empty conversation titles use the deterministic first-turn suggestion: normalized first six non-empty words, Unicode-safe, capped at 60 characters, otherwise `Chat with {agent_name}`.

- [ ] **Step 1: Write failing API tests**

Test create/list/search/rename/pin/archive/resume; deterministic suggested title; default one human and one primary-agent participant; same-workspace nonparticipant 404; owner/admin administrative visibility; cross-workspace 404; archived-turn rejection; repeated required `client_message_id` returning the same message/task; explicit `remember.enabled` and authorized/unauthorized `requested_scope` persistence; legacy retry with the same idempotency key; no-key legacy requests creating distinct tasks; immediate `Task.temporal_workflow_id == "task-{task_id}"` before command dispatch; and compatibility endpoint behavior. Add a dispatcher registry test that a queued agent-task command maps the exact workflow class, `AgentTaskInput`, workflow ID, and agent task queue. After the delayed start command is delivered, prove an approval/review signal targets that same persisted workflow ID and resumes normally.

```bash
uv run pytest apps/api/tests/test_conversations_unit.py services/event_worker/tests/test_workflow_commands.py -q
```

Expected: FAIL because conversation routes are absent.

- [ ] **Step 2: Implement conversation queries and mutations**

Use cursor pagination by `(last_activity_at, id)` and escaped title/agent search. Restrict participant mutation in V1 to service-owned handoff/review events. Rename, pin, archive, and reopen emit audit records.

- [ ] **Step 3: Implement idempotent turn dispatch**

Within one transaction, lock/find the required dedupe key, allocate the message sequence, persist caller-owned `remember={enabled, requested_scope?}` into immutable task/message metadata, insert the visible user message and linked task with immutable cutoff/access mode, set `Task.temporal_workflow_id = f"task-{task_id}"`, call `enqueue_workflow_start(..., command_id="task-start:{task_id}", target_workflow_id=task.temporal_workflow_id)`, update last activity, and commit. The task's workflow ID is therefore available to approval/review signal services before reconciliation and always matches the outbox command; the generic dispatcher never infers or mutates a domain row. Make a best-effort dispatch through the Release 1 command dispatcher; its reconciler handles commit-before-start and start-response-loss, and Temporal already-started marks the command delivered. Retrying a supplied key whose workflow is still pending re-dispatches the same command/task, never a new row; a legacy request without a key always follows its compatibility create-new path.

- [ ] **Step 4: Add backward-compatible agent messaging**

The old endpoint calls the same service and derives its conversation dedupe identity from the supplied idempotency key. Do not maintain a second task-start implementation. Compatibility mapping supplies an explicit API-owned `remember={enabled:false}` when the legacy payload lacks it; it never derives remember intent from agent/model content. Requests without a key retain legacy create-new-work behavior, while the redesigned client always supplies the required key and explicit remember object.

- [ ] **Step 5: Run API and task regressions**

```bash
uv run pytest apps/api/tests/test_conversations_unit.py apps/api/tests/test_security.py -q
uv run pytest packages/workflows/tests/test_agent_task_delegation.py -q
uv run pytest services/event_worker/tests/test_workflow_commands.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit conversation APIs**

```bash
git add apps/api/src/jhin_api/conversations apps/api/src/jhin_api/main.py apps/api/src/jhin_api/tasks apps/api/src/jhin_api/agents/router.py apps/api/tests/test_conversations_unit.py services/event_worker/src/jhin_event_worker/workflow_commands.py services/event_worker/tests/test_workflow_commands.py
git commit -m "feat: add named chat APIs and durable turns"
```

### Task 4: Make worker history and delegated activity conversation-aware

**Files:**
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/trigger_activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/engineering_activities.py`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Modify: `packages/tools/src/jhin_tools/organization.py`
- Create: `packages/tools/src/jhin_tools/messages.py`
- Create: `packages/workflows/src/jhin_workflows/agent_inbox/__init__.py`
- Create: `packages/workflows/src/jhin_workflows/agent_inbox/shared.py`
- Create: `packages/workflows/src/jhin_workflows/agent_inbox/workflows.py`
- Modify: `packages/workflows/src/jhin_workflows/__init__.py`
- Create: `packages/workflows/tests/test_agent_inbox_workflow.py`
- Create: `services/agent_worker/src/jhin_agent_worker/inbox_activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/workflow_commands.py`
- Modify: `services/event_worker/tests/test_workflow_commands.py`
- Modify: `packages/policy/src/jhin_policy/capabilities.py`
- Create: `services/agent_worker/tests/test_conversation_history.py`
- Create: `services/agent_worker/tests/test_inbox_activities.py`
- Modify: `services/agent_worker/tests/test_delegation_activities.py`
- Modify: `packages/tools/tests/test_organization_tools.py`
- Create: `packages/tools/tests/test_message_tool.py`
- Modify: `packages/policy/tests/test_capabilities.py`

**Interfaces:**
- Primary-agent visible model history is ordered conversation history through the task's immutable cutoff across completed work episodes.
- Current task/run tool messages remain available only to the owning run.
- Child tasks inherit `conversation_id` and structured handoff/result messages appear in the root transcript.
- Dedupe keys derive from immutable run, step, tool-call, and child-task identifiers.
- Capability/tool `organization.message.send` / `organization.send_message` sends one structured `question`, `status`, or `escalation` to an authorized same-workspace agent using the current conversation/task audience.
- `AgentInboxWorkflow(agent_id, buffered_item_ids=())` has workflow ID `agent-inbox-{agent_id}` and one durable logical inbox per target agent. `enqueue_workflow_start` creates it once and `enqueue_workflow_signal` delivers a deterministic persisted inbox-item ID, so every committed delivery wakes the recipient even after worker restart. The run uses a signal-driven loop and continues-as-new after 200 processed items or when Temporal suggests it; it carries the deduplicated in-memory buffer into the new run and immediately drains persistent pending items, so a signal racing the boundary cannot be lost.
- `AgentInboxItem` is the persisted Release 2 row. For a direct message its `source_kind=message` and composite-FK `message_id` identify the source; activities resolve the explicit addressed body/audience and fresh public roster facts from that source only. It contains no copied conversation history, task transcript, memory, grant, or relationship payload.
- `record_inbox_agent_run(item_id) -> InboxRunResult` claims a pending or expired-processing row by compare-and-set, writes a random `claim_token`, increments attempts, and sets a 300-second lease; its Temporal activity start-to-close timeout is at most 240 seconds. Completion/update is fenced by that claim token. It creates the existing audited taskless `AgentRun(task_id=NULL, reason="agent_inbox", temporal_workflow_id="agent-inbox-{agent_id}")`, records only the addressed item and fresh public roster facts in its snapshot/events, exposes only `organization.message.respond`, persists one visible response under a unique inbound-item result key, and marks the item completed. A crash after claim is reclaimed only after lease expiry; stale claim holders cannot commit, and retries return the persisted result. It cannot query unrelated conversation history.

- [ ] **Step 1: Write failing worker/history tests**

Test two turns across separate tasks, concurrent later-turn cutoff exclusion, history exclusion of raw tool messages from previous tasks, reply idempotency after activity retry, child conversation inheritance without prior-chat access, structured handoff projection, and no private child payload copying. For the message/inbox path test deny-by-default, scoped allow, explicit deny, cross-workspace 404, recipient/audience validation, relationship/team/manager non-authority, dedupe, visible structured projection, one inbox workflow per target, committed inbox row plus start/signal retry and start/signal response-loss reconciliation, recipient wake after restart, autonomous reply persisted once, response-only capability, public-roster-only inbox input, crash after claim followed by lease-expiry reclaim, stale-token completion rejection, one visible result despite at-least-once model execution, continue-as-new after the bounded threshold, and a signal racing continue-as-new still being found through the persistent pending-item drain and processed once. Extend the dispatcher registry test to map the exact `AgentInboxWorkflow` class/input/workflow ID/task queue and preserve the item-ID signal payload. Prove unrelated conversation history cannot be loaded.

```bash
uv run pytest services/agent_worker/tests/test_conversation_history.py services/agent_worker/tests/test_delegation_activities.py services/agent_worker/tests/test_inbox_activities.py packages/tools/tests/test_organization_tools.py packages/tools/tests/test_message_tool.py packages/policy/tests/test_capabilities.py packages/workflows/tests/test_agent_inbox_workflow.py services/event_worker/tests/test_workflow_commands.py -q
```

Expected: FAIL on task-scoped-only history and missing conversation links.

- [ ] **Step 2: Split visible and internal history loaders**

Use `jhin_access.visible_message_predicate` to load visible user/agent/structured activity through `conversation_cutoff_seq`; load internal tool-call/tool-result context by current task/run. `full` access is reserved for the primary conversation agent. `handoff_only` delegates/helpers/reviewers receive the explicit handoff/request, addressed messages, their task interactions, and allowlisted results only. Apply one combined token budget with recent eligible visible turns favored and current internal context retained.

- [ ] **Step 3: Propagate conversation IDs and deterministic dedupe keys**

Every task-created visible message copies the task conversation. Delegation and workflow templates copy the root conversation to children. Insert replies with conflict-safe dedupe behavior.

- [ ] **Step 4: Project safe handoff summaries**

Use the existing structured message contract for asked/accepted/completed/failed handoffs. Include source task/message IDs, outcome, artifacts, risks, and next action; omit provider reasoning and raw tool payloads.

- [ ] **Step 5: Implement gateway-mediated ordinary agent messages**

Register the narrow capability and a validator that requires explicit target scope plus current resource visibility. Register `AgentInboxWorkflow` and its narrow activities in the worker, export its shared types through `jhin_workflows`, and wire inbox `WorkflowCommand` delivery through the existing event-worker dispatcher. The executor allocates a conversation sequence, stores recipient/audience fields and a deterministic run/step dedupe key, and emits audit/activity. In the same transaction it inserts the addressed `AgentInboxItem(source_kind="message", message_id=message_id)`, ensures `enqueue_workflow_start(..., command_id="agent-inbox-start:{target_agent_id}", target_workflow_id="agent-inbox-{target_agent_id}")` exists, and calls `enqueue_workflow_signal(..., command_id="agent-inbox-item:{inbox_item_id}", target_workflow_id="agent-inbox-{target_agent_id}", signal_name="deliver_item", payload_json={"item_id": inbox_item_id}, depends_on_command_id="agent-inbox-start:{target_agent_id}")`. The dispatcher claims a command only when its optional dependency is delivered, treats Temporal already-started/already-delivered and duplicate command IDs as delivered, and retries commit-to-start/signal and response-loss windows. The workflow's main loop deduplicates signaled IDs, drains persistent pending plus expired-leased rows at startup/after each item, waits when empty, and calls continue-as-new only after handlers finish while carrying its remaining buffer. Claim-token fencing plus persistent item/result idempotency closes both crash/retry and signal/continue-as-new races. It never adds the recipient as a permanent participant, grants history access, or loads unrelated conversation rows.

- [ ] **Step 6: Run worker/workflow regressions**

```bash
uv run pytest services/agent_worker/tests/test_conversation_history.py services/agent_worker/tests/test_delegation_activities.py services/agent_worker/tests/test_inbox_activities.py packages/tools/tests/test_organization_tools.py packages/tools/tests/test_message_tool.py packages/workflows/tests/test_agent_inbox_workflow.py services/event_worker/tests/test_workflow_commands.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit conversation-aware execution**

```bash
git add services/agent_worker packages/tools packages/policy packages/workflows services/event_worker
git commit -m "feat: preserve chat history across durable work"
```

### Task 5: Add a functional persistent chat surface before the redesign

**Files:**
- Modify: `apps/web/lib/types.ts`
- Modify: `apps/web/lib/hooks.ts`
- Create: `apps/web/app/(app)/chats/page.tsx`
- Create: `apps/web/app/(app)/chats/[conversationId]/page.tsx`
- Create: `apps/web/components/chat/chat-list.tsx`
- Create: `apps/web/components/chat/conversation-view.tsx`
- Create: `apps/web/components/chat/composer.tsx`
- Create: `apps/web/components/chat/message-row.tsx`
- Modify: `apps/web/components/app-shell.tsx`
- Modify: `apps/web/components/org/agent-drawer.tsx`
- Create: `apps/web/tests/conversation-view.test.tsx`

**Interfaces:**
- Conversation list with create/search/pin/archive.
- Transcript polls the conversation/message/task state through existing TanStack Query infrastructure.
- Composer remains available after each task completes and submits a new durable turn.

- [ ] **Step 1: Write failing component tests**

Cover multiple named chats for one agent, composer persistence after completion/failure, rename/pin/archive controls, visible agent-agent handoff, retry-safe submit state, empty/loading/error states, and keyboard submit/newline behavior.

```bash
pnpm --filter jhin-web test -- conversation-view.test.tsx
```

Expected: FAIL because the chat components do not exist.

- [ ] **Step 2: Add typed query hooks**

Reuse `api.ts`, cookie authentication, CSRF mutation handling, and TanStack Query. Do not add Next route handlers, Server Actions, or SSE in this release.

- [ ] **Step 3: Build the compatible chat pages**

Keep route files thin. In Next 16, await dynamic `params`. Render structured activity distinctly from normal messages and provide source task links without exposing raw identifiers in prose.

- [ ] **Step 4: Add entry points without removing operations UI**

Add Chats to the existing shell and make Chat the primary action from an agent drawer. Existing Tasks/Runs remain available until Release 4.

- [ ] **Step 5: Run frontend gates**

```bash
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
```

Expected: PASS.

- [ ] **Step 6: Commit persistent chat UI**

```bash
git add apps/web
git commit -m "feat: add persistent named chats"
```

### Task 6: Add versioned memory storage and pgvector-compatible foundation

**Files:**
- Create: `packages/db/src/jhin_db/models/memory.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Modify: `packages/db/src/jhin_db/columns.py`
- Modify: `packages/domain/src/jhin_domain/enums.py`
- Modify: `packages/domain/src/jhin_domain/__init__.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260817_0017_memory.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Create: `packages/memory/pyproject.toml`
- Create: `packages/memory/src/jhin_memory/__init__.py`
- Create: `packages/memory/src/jhin_memory/types.py`
- Create: `packages/memory/src/jhin_memory/policy.py`
- Create: `packages/memory/src/jhin_memory/extraction.py`
- Create: `packages/memory/src/jhin_memory/retrieval.py`
- Create: `packages/memory/src/jhin_memory/py.typed`
- Create: `packages/memory/tests/test_policy.py`
- Create: `packages/memory/tests/test_extraction.py`
- Create: `packages/memory/tests/test_retrieval.py`
- Modify: `packages/db/pyproject.toml`
- Modify: `apps/api/pyproject.toml`
- Modify: `packages/agents/pyproject.toml`
- Modify: `packages/tools/pyproject.toml`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `docker/python.Dockerfile`
- Modify: `compose.yaml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- `MemoryRecord`: workspace, scope/type/ID, kind, content, source references, visibility, sensitivity, confidence, importance, tags, status, validity/expiry, `pinned_at`, version/supersedes, creator, and audit metadata.
- `MemoryShare`: explicit authorized subject share without copying content.
- Optional `memory_embedding` rows store record/version, model, dimensions, and pgvector value only when the extension/table is available; mismatched model/dimension rows are never compared.
- `MemoryCandidate`, `MemoryDecision`, `MemorySelection`, `MemoryProvenance`.
- Pure functions for screening, scope authorization, normalization, dedupe, contradiction, promotion, and rank fusion.

- [ ] **Step 1: Write failing memory-policy tests**

Test secret/API-key/authorization-header rejection; shared visibility-resolver non-amplification; agent/team/workspace/explicit-share authorization; status/time filtering; pin state; immutable versions; deterministic exact/near dedupe; contradiction to contested; full-text relevance; matching-model semantic+lexical rank fusion; incompatible dimension exclusion; token/record caps; extension-unavailable migration; and embedding-unavailable fallback.

```bash
uv run pytest packages/memory/tests -q
```

Expected: FAIL because the package/model is absent.

- [ ] **Step 2: Define storage with a portable vector column**

Create `20260817_0017_memory.py` with `revision="0017"` and `down_revision="0016"` and extend the exact migration graph. Keep `MemoryRecord` portable and full-text capable without pgvector. Switch default Compose to the PostgreSQL 17 pgvector image. In migration, attempt `CREATE EXTENSION vector` and conditionally create the raw-SQL `memory_embedding` table/vector index only when the extension exists; failure/insufficient privilege leaves the base schema valid and records semantic search unavailable. Never map a required ORM vector column. Add status/scope/source/expiry indexes and composite workspace/source/share foreign keys. Extend migration tests for both extension-present and extension-denied PostgreSQL databases.

- [ ] **Step 3: Implement deterministic policy**

The model may propose content/kind/tags/confidence; code calls `jhin_access.assert_source_visibility` and determines allowed source, sensitivity, scope ceiling, activation/review state, dedupe/supersession, and conflict state. Workspace promotion and visibility broadening must yield `proposed`, never automatic active.

- [ ] **Step 4: Implement hybrid retrieval and visible fallback**

Filter authorization/status/validity in SQL before ranking. Fuse normalized semantic, full-text, recency, confidence, importance, and scope scores with deterministic tie-breaking. Return retrieval mode `hybrid`, `full_text`, or `unavailable` and bounded items.

- [ ] **Step 5: Wire memory package dependencies**

Add `packages/memory` to uv workspace, Ruff, mypy, and pytest; make it depend on `jhin-access`, DB, domain, and models as required; add `jhin-memory` workspace dependencies to API, agents, tools, and agent worker; update Docker manifest copy, Compose image, and lock. Verify a frozen all-package sync and clean imports in built API/worker images.

- [ ] **Step 6: Run memory/schema tests**

```bash
uv run pytest packages/memory/tests packages/db/tests -q
uv run pytest packages/db/tests/test_migration_graph.py -q
uv sync --frozen --all-packages
```

Expected: PASS.

- [ ] **Step 7: Commit memory foundation**

```bash
git add packages/db/src/jhin_db/models packages/db/src/jhin_db/columns.py packages/db/src/jhin_db/alembic/versions/20260817_0017_memory.py packages/db/tests/test_migration_graph.py packages/db/pyproject.toml packages/domain packages/memory compose.yaml pyproject.toml apps/api/pyproject.toml packages/agents/pyproject.toml packages/tools/pyproject.toml services/agent_worker/pyproject.toml docker/python.Dockerfile uv.lock
git commit -m "feat: add scoped versioned agent memory"
```

### Task 7: Add embedding and structured memory-candidate model contracts

**Files:**
- Modify: `packages/models/src/jhin_models/base.py`
- Modify: `packages/models/src/jhin_models/factory.py`
- Create: `packages/models/src/jhin_models/embeddings.py`
- Create: `packages/models/src/jhin_models/structured.py`
- Modify: `packages/models/src/jhin_models/providers/openai.py`
- Modify: `packages/models/src/jhin_models/providers/openai_compatible.py`
- Modify: `apps/api/src/jhin_api/models/schemas.py`
- Modify: `apps/api/src/jhin_api/models/service.py`
- Modify: `apps/api/src/jhin_api/workspaces/schemas.py`
- Modify: `apps/api/src/jhin_api/workspaces/service.py`
- Create: `packages/models/tests/test_embeddings.py`
- Create: `packages/models/tests/test_structured_memory.py`
- Create: `apps/api/tests/test_memory_embedding_config_unit.py`

**Interfaces:**
- Optional `EmbeddingProvider.embed(texts, model, dimensions) -> EmbeddingBatch`.
- `extract_structured(schema, messages, idempotency_key)` returns validated Pydantic data or a typed failure.
- Unsupported providers produce explicit degradation, not an application crash.
- Model profile config: `config_json.embedding = {enabled, model, dimensions, cost_micros_per_million_tokens}`.
- Workspace config: `settings_json.memory = {embedding_profile_id, retrieval_enabled, automatic_maintenance, max_records, max_tokens}`.

- [ ] **Step 1: Write failing adapter tests**

Use fake HTTP transports to test deterministic request shape, dimensions, batching, malformed vectors, provider errors, redacted logging, and structured JSON validation/retry. Test providers with no embedding support, cross-workspace profile rejection, dimension bounds, disabled config/full-text fallback, and cost metadata.

```bash
uv run pytest packages/models/tests/test_embeddings.py packages/models/tests/test_structured_memory.py -q
```

Expected: FAIL on missing optional contracts.

- [ ] **Step 2: Add provider-neutral optional interfaces**

Do not make chat providers implement embedding or strict structured output. Factory capability checks select supported adapters and return typed unsupported states. Validate dimensions in `1..4096`; embedding persistence records provider profile, model, dimensions, and content hash.

- [ ] **Step 3: Implement OpenAI-compatible support**

Use configured base URL/credentials and existing request/redaction conventions. Validate vector count/dimensions and strict structured data before returning. Never log source conversation text.

- [ ] **Step 4: Validate and persist workspace/profile configuration**

Reuse existing model/workspace update endpoints with typed additive fields. An enabled workspace embedding profile must be enabled, same-workspace, and advertise a matching embedding capability. Configuration changes invalidate semantic caches; old embeddings remain source versions but are ineligible until recomputed for the selected model/dimensions.

- [ ] **Step 5: Run all model/API tests**

```bash
uv run pytest packages/models/tests apps/api/tests/test_memory_embedding_config_unit.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit optional model capabilities**

```bash
git add packages/models apps/api/src/jhin_api/models apps/api/src/jhin_api/workspaces apps/api/tests/test_memory_embedding_config_unit.py
git commit -m "feat: add optional memory model capabilities"
```

### Task 8: Run memory maintenance asynchronously after visible work

**Files:**
- Create: `packages/workflows/src/jhin_workflows/memory_maintenance/__init__.py`
- Create: `packages/workflows/src/jhin_workflows/memory_maintenance/shared.py`
- Create: `packages/workflows/src/jhin_workflows/memory_maintenance/workflows.py`
- Create: `packages/workflows/tests/test_memory_maintenance_workflow.py`
- Create: `services/agent_worker/src/jhin_agent_worker/memory_activities.py`
- Create: `services/agent_worker/tests/test_memory_activities.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/workflows.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`

**Interfaces:**
- `MemoryMaintenanceInput(workspace_id, agent_id, task_id, conversation_id, source_kind, source_id, remember_enabled, requested_scope=None)` where `source_kind` is exactly `message` or `task_outcome`; `source_id` is the matching workspace-local message or task ID. Remember intent/scope fields are copied verbatim from the user/API turn input and are never proposed, enabled, or broadened by a model. The tagged source union lets headless completed work use its structured outcome without inventing a message row.
- `ExtractMemoryCandidatesInput/Result` and `ApplyMemoryCandidatesInput/Result`.
- Workflow ID: `memory-maintenance-{source_kind}-{source_id}`.

- [ ] **Step 1: Write failing workflow/activity tests**

Test deterministic child ID, `ParentClosePolicy.ABANDON`, origin completion independent of maintenance, explicit `remember.enabled=true` activation sourced from the user message, `remember.enabled=false` ordinary private activation sourced from the final agent message, API-owned `requested_scope` validation/persistence, model output unable to enable or broaden remember scope, completed-task structured outcome maintenance even without a final chat message, proposed shared/workspace promotion, secret rejection, source-visibility denial, retry idempotency, contradiction state, and extraction/provider failure.

```bash
uv run pytest packages/workflows/tests/test_memory_maintenance_workflow.py services/agent_worker/tests/test_memory_activities.py -q
```

Expected: FAIL on absent workflow/activities.

- [ ] **Step 2: Implement the durable workflow**

Load a bounded source through `jhin-access`, ask for strict candidates, then pass candidates and immutable source visibility/audience facts to deterministic policy/application activities. Only persisted user/API `remember.enabled=true` uses the user message as source and supplies its validated `requested_scope`; `remember.enabled=false` ordinary turns use the visible final response; headless completed tasks use their structured outcome only. The model may propose candidate content but cannot enable remembering, choose a source, set a requested scope, or directly write memory state.

- [ ] **Step 3: Start maintenance without blocking the task**

After the eligible source message/outcome is persisted, start the deterministic child with `ABANDON`. Persisted user/API `remember.enabled=true` may start immediately after the user message is committed, while ordinary maintenance starts after completion. If start or extraction fails, emit a user-safe `memory.maintenance_failed` event, queue the deterministic retry, and allow chat/task success.

- [ ] **Step 4: Register activities and run workflow regressions**

```bash
uv run pytest packages/workflows/tests services/agent_worker/tests -q
```

Expected: PASS.

- [ ] **Step 5: Commit memory maintenance**

```bash
git add packages/workflows services/agent_worker
git commit -m "feat: maintain agent memory asynchronously"
```

### Task 9: Retrieve authorized memory with run provenance

**Files:**
- Modify: `packages/agents/src/jhin_agents/snapshot.py`
- Modify: `packages/agents/src/jhin_agents/context.py`
- Modify: `packages/agents/src/jhin_agents/runtime.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Create: `packages/agents/tests/test_memory_context.py`
- Create: `services/agent_worker/tests/test_memory_retrieval.py`

**Interfaces:**
- `MemoryContextItem(id, version, kind, content, scope_label, source_label)`.
- `AgentExecutionSnapshot.memory_context` with bounded token/hash participation.
- Run event `context.memory.selected` stores IDs, versions, policy outcome, retrieval mode, and context hash, but not memory content.

- [ ] **Step 1: Write failing retrieval-context tests**

Test relevance across a different conversation, irrelevant/unauthorized/expired/superseded/rejected/forgotten exclusion, explicit shares, team membership changes, deterministic cap/hash, full-text fallback, memory outage behavior, and content-free provenance. Add a multi-step run that revokes/forgets a selected record after step one and proves step two does not receive it.

```bash
uv run pytest packages/agents/tests/test_memory_context.py services/agent_worker/tests/test_memory_retrieval.py -q
```

Expected: FAIL because snapshots do not retrieve memory.

- [ ] **Step 2: Retrieve after live authorization**

Use the current agent/team/workspace identities on every run; do not cache an authorization result across membership changes. Before every model step, revalidate selected IDs/versions through `jhin-access` and strip revoked/expired/forgotten/unauthorized rows before prompt assembly. A refreshed snapshot/context hash is recorded whenever the selection changes.

- [ ] **Step 3: Render attributable bounded context**

Label memory as recalled information, preserve source labels, and keep it separate from system instructions. Record selection provenance and hash on the timeline.

- [ ] **Step 4: Implement degraded behavior**

Embedding failure uses full text and records mode. A complete memory outage records `memory_unavailable`; ordinary work continues. `Task.metadata_json["memory_required"] = true` (settable by the conversation turn/API) makes retrieval failure terminate visibly with `memory_required_unavailable` before the model call.

- [ ] **Step 5: Run context/runtime regressions**

```bash
uv run pytest packages/agents/tests services/agent_worker/tests -q
```

Expected: PASS.

- [ ] **Step 6: Commit memory retrieval**

```bash
git add packages/agents services/agent_worker
git commit -m "feat: recall authorized memory across chats"
```

### Task 10: Add memory management APIs and compatible UI controls

**Files:**
- Create: `apps/api/src/jhin_api/memory/__init__.py`
- Create: `apps/api/src/jhin_api/memory/schemas.py`
- Create: `apps/api/src/jhin_api/memory/service.py`
- Create: `apps/api/src/jhin_api/memory/router.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Create: `apps/api/tests/test_memory_unit.py`
- Create: `packages/tools/src/jhin_tools/memory.py`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Modify: `packages/policy/src/jhin_policy/capabilities.py`
- Create: `packages/tools/tests/test_memory_tools.py`
- Modify: `packages/policy/tests/test_capabilities.py`
- Modify: `apps/web/lib/types.ts`
- Modify: `apps/web/lib/hooks.ts`
- Create: `apps/web/components/memory/memory-list.tsx`
- Create: `apps/web/tests/memory-list.test.tsx`
- Modify: `apps/web/components/org/agent-drawer.tsx`

**Interfaces:**
- List/filter by agent/team/workspace/status/kind/source.
- Pin/unpin; versioned edit; contest with reason; approve/reject proposed promotion; explicit share create/revoke; forget.
- Tools/capabilities: `memory.read` / `memory.search` and `memory.propose` / `memory.propose`, both gateway-mediated and target/scope constrained.
- Forget clears content, embedding, searchable text, and caches immediately; audit retains identifiers/action/timestamps only.

- [ ] **Step 1: Write failing API and component tests**

Cover this RBAC matrix: owner/admin manage all visible scopes and promotions; member reads visible records and manages agent-private records for agents it may message; viewer is read-only; team/workspace promotion requires admin; share creator or admin may revoke. Cover pin/unpin, versioned edits, promotion approve/reject, share create/revoke, contest, every mutation's audit action, forget/tombstone/cache invalidation, same-workspace unauthorized 404, cross-workspace 404, source links, and plain-language controls. Tool tests cover deny-by-default, scoped allow, explicit deny, relationship/team/manager non-authority, source visibility, proposal scope ceiling, and cross-workspace 404.

```bash
uv run pytest apps/api/tests/test_memory_unit.py -q
uv run pytest packages/tools/tests/test_memory_tools.py packages/policy/tests/test_capabilities.py -q
pnpm --filter jhin-web test -- memory-list.test.tsx
```

Expected: FAIL on missing API/UI.

- [ ] **Step 2: Implement memory mutations transactionally**

Edits create a new version and supersede the old row. Shares reference the version/scope without copying content and call the common visibility ceiling. Forget locks the active chain, clears live/search/vector content, marks forgotten, and emits a content-free audit event. Audit pin/edit/contest/promotion/share/forget without secret content. Admin/human review is required for workspace promotion.

- [ ] **Step 3: Add inspect/edit/contest/forget UI**

Show content, scope, source, last update, confidence in plain language, and consequences before forget. Keep raw IDs and policy evidence behind an expandable technical detail until Release 4 moves them to Advanced.

- [ ] **Step 4: Register memory tools through the gateway**

`memory.search` returns only active authorized records and records selected IDs; `memory.propose` accepts concise content/kind/requested scope/source message ID and routes it through deterministic screening/promotion. Neither executor can directly activate workspace memory or bypass source visibility.

- [ ] **Step 5: Run memory and frontend gates**

```bash
uv run pytest packages/memory/tests apps/api/tests/test_memory_unit.py -q
uv run pytest packages/tools/tests/test_memory_tools.py packages/policy/tests -q
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
```

Expected: PASS.

- [ ] **Step 6: Commit memory controls**

```bash
git add apps/api/src/jhin_api/memory apps/api/src/jhin_api/main.py apps/api/tests/test_memory_unit.py packages/tools packages/policy apps/web
git commit -m "feat: add transparent memory controls"
```

### Task 11: Prove Release 2 acceptance and compatibility

**Files:**
- Create: `tests/integration/test_release2_conversations.py`
- Create: `tests/integration/test_release2_memory.py`
- Modify: `tests/integration/test_release1_migrations.py`
- Create: `docs/architecture/conversations-and-memory.md`
- Modify: `docs/implementation-plan.md`

- [ ] **Step 1: Write end-to-end conversation scenarios**

Create/name/rename/pin/archive/resume multiple chats with one agent; run several turns; prove same-workspace nonparticipant denial; reproduce API-to-Temporal failure/retry and worker restart without duplicate messages; prove immutable concurrent-turn cutoffs; delegate/request help and observe the handoff/result in the original chat while the transient agent cannot read prior turns; exercise ordinary gateway-mediated agent messaging.

- [ ] **Step 2: Write end-to-end memory scenarios**

Explicitly remember a fact in chat A and use it in chat B; exercise `memory.read`/`memory.propose` grant boundaries; reject a secret; exclude irrelevant, expired, unauthorized, superseded, rejected, and forgotten records; create/revoke an explicit share; change team membership; revoke memory mid-run; exercise full-text fallback with pgvector unavailable; verify content-free provenance/tombstone. Extend disposable migration tests for exact `0015 -> 0016 -> 0017`, fresh upgrade, extension-denied upgrade, `0015 -> head`, and downgrade/re-upgrade.

- [ ] **Step 3: Run focused and inherited integration suites**

```bash
uv run pytest -m integration tests/integration/test_release1_migrations.py tests/integration/test_release2_conversations.py tests/integration/test_release2_memory.py tests/integration/test_phase8_exit.py -v
```

Expected: PASS.

- [ ] **Step 4: Run complete quality gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"
pnpm --filter jhin-web test
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web exec next build --webpack
```

Expected: PASS.

- [ ] **Step 5: Document and commit the release evidence**

Document conversation/task boundaries, history privacy, dedupe keys, memory state machine, authorization, hybrid/fallback retrieval, provenance, failure behavior, and fresh counts. Mark only completed Release 2 items.

```bash
git add tests/integration/test_release2_conversations.py tests/integration/test_release2_memory.py tests/integration/test_release1_migrations.py docs/architecture/conversations-and-memory.md docs/implementation-plan.md
git commit -m "docs: verify conversations and memory release"
```
