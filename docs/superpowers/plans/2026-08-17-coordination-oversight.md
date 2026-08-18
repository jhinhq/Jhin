# Coordination and Oversight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make agents operate like an optional real company: peers can request cross-team help, managers can understand subordinate work, configurable exception reviews guide decisions, and users can see one coherent activity story.

**Architecture:** Peer work requests are distinct from delegation and become durable, idempotent records. Pure policy evaluators decide request eligibility, review matching, and reviewer resolution; the existing tool gateway remains the sole authority for agent actions. Manager rollups and activity cards are deterministic projections of authoritative tasks, messages, tools, approvals, and reviews. Review gates compose with—not replace—human approvals and capability enforcement.

**Tech Stack:** Python 3.13, SQLAlchemy/Alembic, FastAPI, Temporal, existing policy/tool gateway, PostgreSQL, pytest/httpx.

**Spec:** `docs/superpowers/specs/2026-08-17-jhin-ai-company-experience-design.md`

## Global Constraints

- A manager, teammate, or collaborator relationship is routing context, never authority.
- Explicit deny and structural limits always win over a work request or review recommendation.
- Cross-workspace agents, tasks, conversations, requests, policies, and reviews return 404.
- Request retries create at most one linked task; a decline creates no task.
- Accepted peer work remains distinct from delegation: it has no `parent_task_id` or delegation metadata and is linked only through `work_request_id` and conversation activity.
- Review trigger keys create at most one review for one exception.
- Routine low-risk work proceeds without review under the default configuration.
- Missing mandatory pre-action or before-close reviewers fail closed.
- AI review cannot approve human-reserved actions or bypass the tool gateway.
- Rollups never expose private memory or unauthorized conversation text.
- All evidence, rollup, request, review, and activity reads use `jhin-access`; no subsystem invents a second visibility rule.
- Release 1's generic durable `WorkflowCommand` outbox owns both `start` and `signal` commands. Commands have deterministic command IDs, target workflow ID/type, command kind, payload, delivery state, and retry metadata; commit-to-Temporal failure, start-response loss, and signal-response loss cannot duplicate or strand request, inbox, periodic-review, or reviewer work.
- Unified activity is a read model; append-only source records remain authoritative.
- Agent-facing tools never modify grants, membership, relationships, review policies, or audit history.

---

### Task 1: Add coordination persistence and domain vocabulary

**Files:**
- Create: `packages/db/src/jhin_db/models/coordination.py`
- Modify: `packages/db/src/jhin_db/models/conversation.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Modify: `packages/domain/src/jhin_domain/enums.py`
- Modify: `packages/domain/src/jhin_domain/__init__.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260817_0018_coordination_oversight.py`
- Create: `packages/db/tests/test_coordination_models.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Modify: `packages/domain/tests/test_enums.py`

**Interfaces:**
- `WorkRequest`: workspace/conversation/source task/run, `requested_by_type` plus typed user/agent ID, `requesting_agent_id`, target agent, parent work-request ID, title, instructions, expected output, status, idempotency key, depth, linked task, response, timestamps.
- `ReviewPolicy`: workspace, scope type, `scope_uuid` only for team/agent scope, `scope_key` only for workflow/task-type scope, and neither scope field for workspace scope; mode, conditions, reviewer selector, cadence for periodic mode, mandatory/enabled/priority, and monotonic `workflow_generation` incremented on every disabled-to-enabled periodic transition.
- Release 3 extends `AgentInboxItem` with nullable `work_request_id` and changes the source check to exactly one matching typed source: `message` requires only `message_id`; `work_request` requires only `work_request_id`. Both typed IDs use composite workspace foreign keys.
- `WorkReview`: policy/source evidence/task/run/tool/request, `reviewer_type` plus exactly one typed reviewer user/agent ID, status/verdict/feedback, unique trigger key, attempt, timestamps.
- Enums: `WorkRequestStatus`, `ReviewMode`, `WorkReviewStatus`, `ReviewVerdict`, `ReviewerType`.

- [ ] **Step 1: Write failing model and enum tests**

Cover request/review state values, exact actor/reviewer typed-ID checks, workspace scope requiring neither key nor UUID, team/agent scope requiring only UUID, workflow/task-type scope requiring only key, rejection of all invalid scope-field combinations, unique `(workspace_id, idempotency_key)`, unique non-null linked task, unique `(workspace_id, trigger_key)`, nonnegative/monotonic periodic `workflow_generation`, inbox exactly-one typed source, evidence shape, nullable manager/reviewer fallbacks, and indexes for inbox/activity reads. PostgreSQL tests reject cross-workspace actor/agent/task/conversation/inbox-work-request/evidence links and spoofed actor IDs.

```bash
uv run pytest packages/db/tests/test_coordination_models.py packages/domain/tests/test_enums.py -q
```

Expected: FAIL because the coordination model is absent.

- [ ] **Step 2: Define models and constraints**

Use existing UUID/timestamp/JSON helpers. Constrain status/mode/verdict/selector values, require requesting and target agents to differ, and preserve immutable request instructions/evidence after creation. Enforce a three-way scope check: `workspace` has `scope_uuid IS NULL AND scope_key IS NULL`; `team`/`agent` have non-null `scope_uuid` and null `scope_key`; `workflow`/`task_type` have null `scope_uuid` and non-null `scope_key`. Use composite workspace foreign keys for all workspace-owned edges; centralized `jhin-access` source checks supplement structural constraints.

- [ ] **Step 3: Write the additive migration**

Create `20260817_0018_coordination_oversight.py` with `revision="0018"` and `down_revision="0017"`, tables, indexes, the `AgentInboxItem.work_request_id` composite workspace foreign key/exactly-one typed-source check, and the three-way `ReviewPolicy` scope check without changing existing delegation/task rows. Extend `packages/db/tests/test_migration_graph.py` through `0018`. Downgrade restores the message-only inbox check and removes only Release 3 additions.

- [ ] **Step 4: Verify schema and migration head**

```bash
uv run pytest packages/db/tests/test_coordination_models.py packages/domain/tests/test_enums.py -q
uv run pytest packages/db/tests/test_migration_graph.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit coordination persistence**

```bash
git add packages/db/src/jhin_db/models packages/db/src/jhin_db/alembic/versions/20260817_0018_coordination_oversight.py packages/db/tests/test_coordination_models.py packages/db/tests/test_migration_graph.py packages/domain
git commit -m "feat: add coordination and review records"
```

### Task 2: Implement pure work-request and review policy evaluators

**Files:**
- Create: `packages/coordination/pyproject.toml`
- Create: `packages/coordination/src/jhin_coordination/__init__.py`
- Create: `packages/coordination/src/jhin_coordination/requests.py`
- Create: `packages/coordination/src/jhin_coordination/reviews.py`
- Create: `packages/coordination/src/jhin_coordination/rollups.py`
- Create: `packages/coordination/src/jhin_coordination/activity.py`
- Create: `packages/coordination/src/jhin_coordination/py.typed`
- Create: `packages/policy/src/jhin_policy/work_requests.py`
- Create: `packages/policy/src/jhin_policy/reviews.py`
- Modify: `packages/policy/src/jhin_policy/__init__.py`
- Create: `packages/policy/tests/test_work_requests.py`
- Create: `packages/policy/tests/test_reviews.py`
- Modify: `pyproject.toml`
- Modify: `apps/api/pyproject.toml`
- Modify: `packages/tools/pyproject.toml`
- Modify: `packages/agents/pyproject.toml`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `docker/python.Dockerfile`
- Modify: `uv.lock`

**Interfaces:**
- `evaluate_work_request(grants, WorkRequestFacts, WorkRequestLimits) -> WorkRequestDecision`.
- `match_review_policies(policies, ReviewFacts, mode) -> tuple[ReviewRequirement, ...]`.
- `resolve_reviewer(selector, ReviewerCandidates) -> ReviewerResolution`.

- [ ] **Step 1: Write failing pure-policy tests**

Test deny-by-default, explicit deny, target scope, active/same-workspace target, depth/rate/concurrency/budget limits, collaborator non-authority, condition matching, priority ordering, manager/named-agent/team-role/human selectors, fallback, and mandatory missing reviewer. Use exact workspace keys `coordination.max_request_depth`, `max_pending_requests_per_agent`, `max_requests_per_agent_per_hour`, `max_active_request_tasks_per_agent`, and `max_request_cost_micros` with safe defaults `4/10/30/3/250000`.

```bash
uv run pytest packages/policy/tests/test_work_requests.py packages/policy/tests/test_reviews.py -q
```

Expected: FAIL on missing evaluators.

- [ ] **Step 2: Implement typed pure decisions**

Keep database access outside evaluators. Decisions return stable reason/error codes, matched grant/policy IDs, and user-safe summaries. Review conditions support risk, cost/token/time threshold, failure, policy denial, blocked/risk, confidence, cross-team, scope change, and explicit request.

- [ ] **Step 3: Implement deterministic reviewer resolution**

Resolve against already-authorized candidates and return `resolved`, `human_required`, `skipped`, or `missing_mandatory`; never infer an authority from reporting alone.

- [ ] **Step 4: Wire the coordination package into consumers**

`jhin-coordination` depends on DB/domain/access and owns database-aware request/rollup/activity services; pure decisions remain in `jhin-policy` to avoid cycles. Add the package to uv workspace, Ruff, mypy, pytest, API, tools, agents, and agent-worker manifests/sources, Docker manifest copy, and the lock. Verify frozen all-package sync and API/worker imports.

- [ ] **Step 5: Run policy regressions**

```bash
uv run pytest packages/policy/tests -q
uv sync --frozen --all-packages
```

Expected: PASS.

- [ ] **Step 6: Commit policy core**

```bash
git add packages/coordination packages/policy pyproject.toml apps/api/pyproject.toml packages/tools/pyproject.toml packages/agents/pyproject.toml services/agent_worker/pyproject.toml docker/python.Dockerfile uv.lock
git commit -m "feat: evaluate peer work and review policy"
```

### Task 3: Implement idempotent peer and cross-team work requests

**Files:**
- Create: `packages/tools/src/jhin_tools/work_requests.py`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Create: `apps/api/src/jhin_api/work_requests/__init__.py`
- Create: `apps/api/src/jhin_api/work_requests/schemas.py`
- Create: `apps/api/src/jhin_api/work_requests/service.py`
- Create: `apps/api/src/jhin_api/work_requests/router.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/shared.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/workflows.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_inbox/shared.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_inbox/workflows.py`
- Modify: `packages/workflows/tests/test_agent_inbox_workflow.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/inbox_activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `packages/agents/src/jhin_agents/snapshot.py`
- Modify: `packages/agents/src/jhin_agents/context.py`
- Modify: `services/event_worker/src/jhin_event_worker/workflow_commands.py`
- Modify: `services/event_worker/tests/test_workflow_commands.py`
- Create: `packages/tools/tests/test_work_request_tools.py`
- Create: `packages/workflows/tests/test_agent_task_work_requests.py`
- Create: `services/agent_worker/tests/test_work_request_inbox_activities.py`
- Create: `apps/api/tests/test_work_requests_unit.py`

**Interfaces:**
- Capability/tool: `organization.work.request` / `organization.request_work`.
- Target response tool: `organization.respond_work_request`, structurally limited to the target agent.
- Human API RBAC is explicit: filtered list/detail use `ViewerCtx`; create uses `MemberCtx`; accept/decline/clarify on behalf of a target agent uses `AdminCtx`. Viewer cannot wake agents or create billable work. Inbox responses use the target agent identity plus a structural target check, never a human context.
- States: pending, clarification requested, accepted, declined, completed, failed.
- Acceptance returns one `linked_task_id`; clarification/decline return none.
- Pending requests become an addressed `AgentInboxItem` delivered to the already-defined durable `AgentInboxWorkflow(agent_id)` through one idempotent `enqueue_workflow_start`/`enqueue_workflow_signal` pair; delivery wakes the target agent and grants no conversation history.
- The inbox evaluates only the explicit request plus public roster facts and autonomously chooses `accept`, `decline`, or `clarify`. Acceptance creates one linked standalone task; decline creates none; the resulting decision/response is persisted and auditable.

- [ ] **Step 1: Write failing tool/API/workflow tests**

Cover agent and human-on-behalf actor/audit shape, ViewerCtx-filtered list/detail, viewer 403 on create/respond, member create success, member on-behalf response 403, admin accept/decline/clarify success, assigned-target-agent structural response, grant denial, explicit denial, relation non-authority, cross-team allowed with grant, cross-workspace 404, idempotent create, persistent target inbox delivery/wake, public-roster-and-request-only autonomous input, no unrelated conversation history, autonomous accept/decline/clarify, decline/no-task, retry-safe accept/one linked task, duplicate delivery handling, accepted task immediately storing `temporal_workflow_id == "task-{task_id}"` before command dispatch, commit-before-start/signal and start/signal-response-loss reconciliation, later pause/resume signaling against that same ID, no delegation parent/metadata, conversation linkage, exact transactional depth/rate/concurrency/budget counters, and terminal completion/failure projection. Extend the dispatcher registry test for the accepted AgentTask start and work-request inbox signal payload/queue mapping.

```bash
uv run pytest packages/tools/tests/test_work_request_tools.py packages/workflows/tests/test_agent_task_work_requests.py apps/api/tests/test_work_requests_unit.py services/event_worker/tests/test_workflow_commands.py -q
```

Expected: FAIL because work requests are missing.

- [ ] **Step 2: Implement request service and transitions**

Lock on `(workspace_id, idempotency_key)` and the target-agent admission key. An agent requester evaluates the gateway grant; a human requester enters through `MemberCtx`, must have source visibility, and records `requested_by_type=user` plus the chosen requesting agent. Human on-behalf transitions enter only through `AdminCtx`; inbox transitions require the exact target agent structurally. Load request-chain depth, pending/hour/active counters, and current cost inside the same transaction. Creation persists the immutable request, inserts `AgentInboxItem(source_kind="work_request", work_request_id=request_id, message_id=NULL, target_agent_id=target_agent_id)` without copied instructions/history, and calls `enqueue_workflow_start(..., command_id="agent-inbox-start:{target_agent_id}", target_workflow_id="agent-inbox-{target_agent_id}")` plus `enqueue_workflow_signal(..., command_id="work-request-inbox:{inbox_item_id}", target_workflow_id="agent-inbox-{target_agent_id}", signal_name="deliver_item", payload_json={"item_id": inbox_item_id}, depends_on_command_id="agent-inbox-start:{target_agent_id}")` before commit. The target inbox activity resolves only that composite-FK request plus fresh public roster facts and independently decides: acceptance locks the request, creates exactly one standalone task (`parent_task_id=NULL`, metadata `{origin: work_request, work_request_id}`) with `handoff_only` conversation access, sets `task.temporal_workflow_id = f"task-{task.id}"`, and calls `enqueue_workflow_start(..., command_id="work-request-task-start:{request_id}", target_workflow_id=task.temporal_workflow_id)` in the same transaction; decline creates no linked task; clarification creates no linked task. All transitions and inbox lease claims compare-and-set and return the persisted result on retry. The generic dispatcher never infers or backfills the task field.

- [ ] **Step 3: Add gateway tools and narrow capabilities**

Tools use the existing execution context, sanitization, audit, and target validators. Request output contains stable IDs, status, public agent identity, and instructions—not private prompts or memory. The target inbox execution has no general response tool or snapshot: it can emit only the narrow request decision (`accept`, `decline`, or `clarify`) and a bounded response. Creation writes the addressed inbox item and publishes the existing event envelope after commit.

- [ ] **Step 4: Integrate durable task starts and finalization**

Creation and autonomous acceptance use the same command dispatcher; Temporal already-started, duplicate signal acknowledgement, and already-delivered command IDs mark delivery, while the event worker reconciles retryable starts/signals. The inbox is started once per target and signaled for every addressed request, so delivery wakes an idle recipient after a crash/restart. `AgentInboxWorkflow` invokes a narrow activity that reads the request by ID, not a conversation snapshot, records the autonomous decision, and atomically creates the accepted task or no task. No notification creates a permanent participant or history access. Task finalization updates the request and inserts a deduplicated visible structured result into the originating conversation.

- [ ] **Step 5: Run coordination and Phase 8 regressions**

```bash
uv run pytest packages/tools/tests/test_work_request_tools.py packages/workflows/tests/test_agent_task_work_requests.py packages/workflows/tests/test_agent_inbox_workflow.py services/agent_worker/tests/test_work_request_inbox_activities.py apps/api/tests/test_work_requests_unit.py services/event_worker/tests/test_workflow_commands.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit peer work requests**

```bash
git add packages/tools packages/workflows packages/agents services/agent_worker services/event_worker apps/api/src/jhin_api/work_requests apps/api/src/jhin_api/main.py apps/api/tests/test_work_requests_unit.py
git commit -m "feat: add peer and cross-team work requests"
```

### Task 4: Add review-policy administration and review inbox APIs

**Files:**
- Create: `apps/api/src/jhin_api/reviews/__init__.py`
- Create: `apps/api/src/jhin_api/reviews/schemas.py`
- Create: `apps/api/src/jhin_api/reviews/service.py`
- Create: `apps/api/src/jhin_api/reviews/router.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Create: `packages/tools/src/jhin_tools/reviews.py`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Create: `packages/workflows/src/jhin_workflows/periodic_review/__init__.py`
- Create: `packages/workflows/src/jhin_workflows/periodic_review/shared.py`
- Create: `packages/workflows/src/jhin_workflows/periodic_review/workflows.py`
- Create: `packages/workflows/tests/test_periodic_review_workflow.py`
- Create: `services/agent_worker/src/jhin_agent_worker/review_activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/event_worker/src/jhin_event_worker/workflow_commands.py`
- Modify: `services/event_worker/tests/test_workflow_commands.py`
- Create: `services/agent_worker/tests/test_review_activities.py`
- Create: `apps/api/tests/test_reviews_unit.py`
- Create: `packages/tools/tests/test_review_tools.py`

**Interfaces:**
- Admin CRUD for `ReviewPolicy` at workspace/team/agent/workflow/task-type scope. Workspace scope carries neither `scope_uuid` nor `scope_key`; team/agent carry UUID only; workflow/task-type carry key only.
- `GET/POST /workspaces/{workspace_id}/review-policies`, `GET/PATCH/DELETE /.../review-policies/{id}`.
- `GET /workspaces/{workspace_id}/reviews`, `GET /.../reviews/{id}`, and `POST /.../reviews/{id}/decision` for assigned humans.
- Presets `hands_off`, `exceptions` (default), `manager_before_close`, and `always_before_close` expand server-side into exact policy conditions; the agent builder never writes raw policy JSON.
- `evaluate_review_event(session, ReviewFacts, mode) -> ReviewGate`.
- `decide_review(session, review_id, actor, verdict, feedback) -> WorkReview`.
- Capability/tool: `organization.review.request` / `organization.request_review`; submit tool limited to resolved AI reviewer.
- `PeriodicReviewWorkflow(policy_id, workflow_generation, window_seconds)` has workflow ID `review-periodic-{policy_id}-{workflow_generation}`, sleeps on deterministic UTC windows, and creates trigger key `periodic:{policy_id}:{workflow_generation}:{window_start}`. Each disabled-to-enabled transition increments and persists the generation before calling `enqueue_workflow_start`; cadence refresh and disable/delete target that exact generation with deterministic `enqueue_workflow_signal` commands (`refresh`/`stop`) in the same transaction as the policy change.

- [ ] **Step 1: Write failing API/tool tests**

Test viewer/member/admin boundaries, workspace scope with neither key/UUID, team/agent UUID-only scope, workflow/task-type key-only scope, rejection of invalid combinations, preset expansion, deterministic trigger dedupe, pre-action/before-close/post-action/periodic modes, condition matching, assigned-human decision, only-resolved-agent tool submission, immutable evidence, cross-workspace isolation, periodic workflow restart/duplicate window, cadence update, stop on disable/delete, and disable→re-enable incrementing the generation and starting exactly one new workflow. For periodic commands cover commit-before-signal, signal-response loss, duplicate command ID, worker restart, refresh applied before the next window, old-generation stop preventing all later old-generation windows, and re-enable producing new-generation windows without reviving the old workflow. Extend the dispatcher registry test to map the exact `PeriodicReviewWorkflow` class/input/generation-aware workflow ID/task queue and refresh/stop signal payloads.

```bash
uv run pytest apps/api/tests/test_reviews_unit.py packages/tools/tests/test_review_tools.py services/event_worker/tests/test_workflow_commands.py -q
```

Expected: FAIL because review services/tools are absent.

- [ ] **Step 2: Implement policy CRUD and review creation**

Normalize JSON conditions into validated Pydantic unions before storage. Match enabled policies by most-specific scope then priority. Generate trigger keys from policy, source kind/ID, mode, exception kind, and review attempt. Presets expand to ordinary persisted policies and return the expanded summary.

- [ ] **Step 3: Implement decisions and inbox queries**

Use compare-and-set transitions so retries return the existing verdict. List by assigned actor/status/urgency with stable cursors. Audit policy mutation and review decisions without storing hidden reasoning.

- [ ] **Step 4: Add request/submit review tools**

Apply gateway grants and structural reviewer checks. Feedback is a concise decision summary, evidence references, risks, and next action—not scratchpad.

- [ ] **Step 5: Run durable periodic reviews**

On each disabled-to-enabled transition, lock the policy, increment `workflow_generation`, and atomically call `enqueue_workflow_start(..., command_id="review-periodic-start:{policy_id}:{generation}", target_workflow_id="review-periodic-{policy_id}-{generation}")`. Cadence updates atomically call `enqueue_workflow_signal(..., command_id="review-periodic-refresh:{policy_id}:{generation}:{policy_version}", target_workflow_id="review-periodic-{policy_id}-{generation}", signal_name="refresh", depends_on_command_id="review-periodic-start:{policy_id}:{generation}")`; disable/delete capture the current generation and atomically call the same helper with `command_id="review-periodic-stop:{policy_id}:{generation}:{policy_version}"`, `target_workflow_id="review-periodic-{policy_id}-{generation}"`, `signal_name="stop"`, and that generation's start-command dependency. Re-enabling can never reuse a delivered start command or closed Temporal workflow ID. The dispatcher/reconciler retries commands after commit-to-start/signal failures and response loss; Temporal already-started and duplicate signal command IDs are delivered no-ops. The workflow uses durable timers, reloads the current policy and generation after every refresh and before every closed window, exits when disabled/deleted or superseded by a newer generation, calls an activity that builds the source-linked rollup, and inserts one `WorkReview` by generation-aware trigger key. Missing optional reviewer records a skipped window; missing mandatory reviewer records a failed-closed attention item.

- [ ] **Step 6: Run review/gateway regressions**

```bash
uv run pytest apps/api/tests/test_reviews_unit.py packages/tools/tests packages/policy/tests packages/workflows/tests/test_periodic_review_workflow.py services/agent_worker/tests/test_review_activities.py services/event_worker/tests/test_workflow_commands.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit review administration**

```bash
git add apps/api/src/jhin_api/reviews apps/api/src/jhin_api/main.py apps/api/tests/test_reviews_unit.py packages/tools packages/workflows/src/jhin_workflows/periodic_review packages/workflows/tests/test_periodic_review_workflow.py services/agent_worker services/event_worker
git commit -m "feat: add configurable work reviews"
```

### Task 5: Compose blocking review gates with policy and human approval

**Files:**
- Modify: `packages/db/src/jhin_db/models/policy.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260817_0019_review_gates.py`
- Modify: `packages/domain/src/jhin_domain/enums.py`
- Modify: `packages/tools/src/jhin_tools/gateway.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/shared.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/workflows.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `apps/api/src/jhin_api/reviews/service.py`
- Modify: `services/event_worker/src/jhin_event_worker/workflow_commands.py`
- Modify: `apps/api/src/jhin_api/tasks/service.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Create: `packages/tools/tests/test_gateway_reviews.py`
- Create: `packages/workflows/tests/test_agent_task_reviews.py`
- Modify: `services/agent_worker/tests/test_review_activities.py`

**Interfaces:**
- `ToolCall.review_id` and `ToolCallStatus.PENDING_REVIEW`.
- `GatewayOutcome.review_id` and `resolve_review(review_id)`.
- `StepResult.waiting_review_id` and workflow signal `review_decision(review_id, verdict)` delivered only by a durable `enqueue_workflow_signal` command committed with the decision.
- Gate order: capability/scope/validator → AI review → human approval → execution.
- `RunStatus.WAITING_REVIEW` is an active/admitted state and remains in `RUN_ACTIVE_STATUSES` so concurrency slots stay held while parked.
- Resolved AI review creates one standalone reviewer task with `metadata.origin=work_review`, `work_review_id`, `handoff_only` evidence, sets its `temporal_workflow_id = f"task-{task.id}"`, and persists an `enqueue_workflow_start` command targeting that exact ID in the same transaction; human/AI decisions commit a deterministic `enqueue_workflow_signal` command to the source workflow with the decision row.

- [ ] **Step 1: Write failing gate-order and durability tests**

Cover routine bypass, pre-action parking/restart and active-slot accounting, reviewer task immediately storing `temporal_workflow_id == "task-{task_id}"` before its start command is delivered, reviewer task/command failure windows, later approval/review signals targeting that same stored ID, human and AI decision signaling, decision commit-before-signal, signal-response loss, duplicate decision/signal command IDs, approval after AI approval, changes-requested revision path, rejection, bounded before-close re-review attempts, existing explicit deny, post-denial nonblocking review creation, human-reserved approval, missing mandatory reviewer, optional missing reviewer, before-close gate, post-action nonblocking creation, and duplicate row/signal/event safety. After an AI verdict and again after a human approval wait, revoke a grant, add an explicit deny, remove target visibility, invalidate structural arguments, and exhaust active admission; prove each causes a fresh denial before execution and that no previously cached authorization is used.

```bash
uv run pytest packages/tools/tests/test_gateway_reviews.py packages/workflows/tests/test_agent_task_reviews.py services/agent_worker/tests/test_review_activities.py -q
```

Expected: FAIL on missing review states/signals.

- [ ] **Step 2: Persist review state beside tool calls**

Create `20260817_0019_review_gates.py` with `revision="0019"` and `down_revision="0018"`; add nullable `tool_call.review_id`, the pending-review status/checks, and any indexed source-workflow review fields required by the implementation. Add `RunStatus.WAITING_REVIEW` and include it in every active-run/admission set. Extend `packages/db/tests/test_migration_graph.py` through `0019` and add disposable upgrade/downgrade tests. Never overload `Approval`; review and security approval remain separately auditable.

- [ ] **Step 3: Implement gateway gate order**

For authorized actions, evaluate review before human approval. A positive review resumes into ordinary approval evaluation. Before actual execution—after an AI review resolves and again after a human approval resolves—reload the current capability grants, explicit denies, target/resource visibility, structural validator facts, and active admission/concurrency state, then rerun the complete capability/scope/validator/admission chain. A review verdict cannot mutate or synthesize grants, and a cached pre-wait decision is never executable. When capability/scope/policy denies and a `policy_denial` post-action policy matches, create a nonblocking review after persisting the unchanged deny; it can improve oversight but never reverse that outcome.

- [ ] **Step 4: Park and resume workflows durably**

Record waiting state before returning from an activity. A resolved AI reviewer creates a standalone review task, sets `task.temporal_workflow_id = f"task-{task.id}"`, and persists `enqueue_workflow_start(..., target_workflow_id=task.temporal_workflow_id)` atomically; an assigned human appears in Attention. `decide_review` transactionally commits the decision and calls `enqueue_workflow_signal(..., command_id="review-decision:{review_id}:{decision_version}", target_workflow_id=source_workflow_id, signal_name="review_decision")`; the reconciler retries commit-to-signal failure and signal-response loss, while duplicate command IDs and workflow-side decision versions are idempotent. On resume, the workflow invokes the full fresh authorization/visibility/structural/admission reload before it executes the tool call. Pre-action `changes_requested` returns feedback to the run without executing the call. Before-close `changes_requested` resumes the source agent for revision, then creates a new attempt/trigger key up to workspace setting `review.max_revision_rounds` (default 2); reject or exhausted rounds fail visibly. Before-close uses the same command-backed durable pattern around task finalization.

- [ ] **Step 5: Run all policy/tool/workflow regressions**

```bash
uv run pytest packages/policy/tests packages/tools/tests packages/workflows/tests services/agent_worker/tests -q
```

Expected: PASS.

- [ ] **Step 6: Commit durable review gates**

```bash
git add packages/db/src/jhin_db/models/policy.py packages/db/src/jhin_db/alembic/versions/20260817_0019_review_gates.py packages/db/tests/test_migration_graph.py packages/domain packages/tools packages/workflows services/agent_worker services/event_worker apps/api/src/jhin_api/reviews apps/api/src/jhin_api/tasks
git commit -m "feat: enforce durable exception reviews"
```

### Task 6: Add authorized manager rollups to APIs and runtime context

**Files:**
- Modify: `packages/coordination/src/jhin_coordination/rollups.py`
- Create: `packages/coordination/tests/test_rollups.py`
- Create: `apps/api/src/jhin_api/manager_rollups/__init__.py`
- Create: `apps/api/src/jhin_api/manager_rollups/schemas.py`
- Create: `apps/api/src/jhin_api/manager_rollups/service.py`
- Create: `apps/api/src/jhin_api/manager_rollups/router.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `packages/agents/src/jhin_agents/context.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Create: `packages/agents/tests/test_manager_context.py`
- Create: `services/agent_worker/tests/test_manager_rollups.py`
- Create: `apps/api/tests/test_manager_rollups_unit.py`

**Interfaces:**
- `build_manager_rollup(session, workspace_id, manager_agent_id, as_of, limit) -> ManagerRollup`.
- `GET /api/v1/workspaces/{workspace_id}/agents/{manager_agent_id}/manager-rollup?as_of=&cursor=&limit=` returns a stable source-linked page plus totals/queue state.
- `TaskContext.manager_rollup: ManagerRollupContext | None`.
- Rollup fields: reports, active/recent work, failures/blocks, pending reviews/approvals, outcomes, artifacts, risks, requested decisions, workload/queue, and source IDs.

- [ ] **Step 1: Write failing rollup/privacy tests**

Test direct/indirect reports, deterministic `as_of`, stable cursor, active/recent state, blocked/failure/attention items, workload, source links, managerless absence, no unrelated agents, no private memory, no unauthorized conversation text, bounded counts/tokens, and stable context hash. API tests cover viewer/member/admin read boundaries, same-workspace nonmanager human access filtered through source visibility, agent runtime restricted to the actual manager, and cross-workspace 404.

```bash
uv run pytest packages/coordination/tests/test_rollups.py packages/agents/tests/test_manager_context.py services/agent_worker/tests/test_manager_rollups.py -q
```

Expected: FAIL because rollups are absent.

- [ ] **Step 2: Build deterministic source-linked rollups**

Query structured task/message/review/approval data only through `jhin-access`. Prefer existing outcome/artifact/risk/request fields; do not summarize raw transcripts. Include indirect reports only when workspace setting `management.include_indirect_reports` is true and label reporting depth.

- [ ] **Step 3: Expose authorized read API and manager context**

Human viewers use workspace RBAC plus per-source visibility; owner/admin may perform audited administrative reads, while member/viewer output is visibility-filtered. Runtime receives a rollup only when the running agent is the current reporting manager and each source is visible. Render it as status context, not instructions.

- [ ] **Step 4: Keep V1 rollups deterministic**

Do not add a second model call in V1. The manager agent reasons over the structured rollup during its ordinary authorized run. A later optional derived-narrative adapter must be separately costed, source-linked, and labeled; its absence does not reduce manager context.

- [ ] **Step 5: Run context and API tests**

```bash
uv run pytest packages/coordination/tests packages/agents/tests services/agent_worker/tests apps/api/tests/test_manager_rollups_unit.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit manager awareness**

```bash
git add packages/coordination packages/agents services/agent_worker apps/api/src/jhin_api/manager_rollups apps/api/src/jhin_api/main.py
git commit -m "feat: give managers authorized work rollups"
```

### Task 7: Add one unified human-readable activity read model

**Files:**
- Modify: `packages/coordination/src/jhin_coordination/activity.py`
- Create: `packages/coordination/tests/test_activity.py`
- Create: `apps/api/src/jhin_api/activity/__init__.py`
- Create: `apps/api/src/jhin_api/activity/schemas.py`
- Create: `apps/api/src/jhin_api/activity/router.py`
- Create: `apps/api/src/jhin_api/attention/__init__.py`
- Create: `apps/api/src/jhin_api/attention/schemas.py`
- Create: `apps/api/src/jhin_api/attention/service.py`
- Create: `apps/api/src/jhin_api/attention/router.py`
- Modify: `apps/api/src/jhin_api/approvals/schemas.py`
- Modify: `apps/api/src/jhin_api/approvals/service.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Create: `apps/api/tests/test_activity_unit.py`
- Create: `apps/api/tests/test_attention_unit.py`

**Interfaces:**
- `GET /api/v1/workspaces/{workspace_id}/activity` with conversation/agent/team/type/time filters, `detail=default|advanced`, and stable cursor.
- `GET /api/v1/workspaces/{workspace_id}/attention?status=&cursor=&limit=` merges approvals, assigned reviews, blocked/failed work, and requested decisions; it returns one stable page plus open/urgent counts.
- Card kinds/labels: Started working, Asked another agent, Used an app, Needs your review, Finished, Needs help, Paused or stopped.
- Default payload is plain-language summary/status/participants/timestamp/source link; Advanced payload contains sanitized structured evidence and identifiers.

- [ ] **Step 1: Write failing projection tests**

Create representative messages, tasks, events, tool calls, approvals, requests, and reviews. Assert deduplication, chronological stable cursor, each card label, filters at conversation/agent/team/workspace scope, default redaction, owner/admin-only Advanced evidence, viewer/member denial of raw evidence, source conversation lookup for approvals/reviews, and source record immutability. Attention tests cover union ordering, source-kind/ID dedupe, cursor across kinds, count calculation, assigned-actor visibility, decision invalidation version, and conversation/task links.

```bash
uv run pytest packages/coordination/tests/test_activity.py apps/api/tests/test_activity_unit.py apps/api/tests/test_attention_unit.py -q
```

Expected: FAIL because projection/read API is absent.

- [ ] **Step 2: Implement normalized activity projection**

Map source rows to one typed card union and merge-sort by `(occurred_at, source_kind, source_id)`. Collapse redundant low-level events but never suppress failure, approval, review, stop, or outcome events.

- [ ] **Step 3: Apply visibility before projection**

Filter every source query through `jhin-access`. Default output omits raw capability names, workflow IDs, tool payloads, and internal event names. Advanced output reuses existing sanitization and requires owner/admin RBAC; requesting it as member/viewer returns 403.

- [ ] **Step 4: Project one Attention inbox**

Normalize approvals, assigned work reviews, blocked/failed tasks, and structured requested decisions into a discriminated union with `(occurred_at, source_kind, source_id)` cursor. Derive `conversation_id` through task/run linkage when a legacy row lacks it. Return `counts={open, urgent}` and a `version` formed from the maximum source timestamp/ID so clients can invalidate one query after any decision.

- [ ] **Step 5: Add APIs and run projection tests**

```bash
uv run pytest packages/coordination/tests/test_activity.py apps/api/tests/test_activity_unit.py apps/api/tests/test_attention_unit.py apps/api/tests/test_audit_unit.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit unified activity**

```bash
git add packages/coordination apps/api/src/jhin_api/activity apps/api/src/jhin_api/attention apps/api/src/jhin_api/approvals apps/api/src/jhin_api/main.py apps/api/tests/test_activity_unit.py apps/api/tests/test_attention_unit.py
git commit -m "feat: project company activity consistently"
```

### Task 8: Prove Release 3 acceptance and architecture

**Files:**
- Create: `tests/integration/test_release3_coordination.py`
- Modify: `tests/integration/test_release1_migrations.py`
- Create: `docs/architecture/coordination-and-review.md`
- Modify: `docs/implementation-plan.md`

- [ ] **Step 1: Write end-to-end coordination cases**

Prove addressed inbox delivery wakes an idle target, request retry/one standalone linked task, autonomous inbox accept/decline/clarify from request-plus-public-roster facts only, decline/no task, cross-team help without prior-chat access, API-to-Temporal start/signal reconciliation, routine review bypass, one exception/one review, periodic windows across worker restart, periodic refresh/stop command recovery, disable→re-enable starting one new generation without reviving the old workflow, policy-denial review without changing deny, mandatory missing reviewer/fallback, AI/human decision command signaling, changes-requested bounded revision, worker restart while waiting review with concurrency held, full revalidation after AI and human waits under grant/deny/visibility/structural/admission revocation, reproducible private-safe rollup/API, explicit deny unchanged, unified Attention counts/links, and activity parity/Advanced RBAC. Extend disposable migrations through exact `0017 -> 0018 -> 0019`, fresh upgrade, prior-head upgrade, downgrade/re-upgrade.

- [ ] **Step 2: Run focused and inherited integration suites**

```bash
uv run pytest -m integration tests/integration/test_release1_migrations.py tests/integration/test_release3_coordination.py tests/integration/test_release2_conversations.py tests/integration/test_release2_memory.py tests/integration/test_phase8_exit.py -v
```

Expected: PASS.

- [ ] **Step 3: Run complete backend gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"
uv run pytest packages/db/tests/test_migration_graph.py -q
uv sync --frozen --all-packages
```

Expected: PASS.

- [ ] **Step 4: Document invariants and evidence**

Document delegation vs request, request state/idempotency, review modes and gate order, manager privacy, activity projection, capability boundaries, failure behavior, and fresh counts. Mark only completed Release 3 items.

- [ ] **Step 5: Commit Release 3 evidence**

```bash
git add tests/integration/test_release3_coordination.py tests/integration/test_release1_migrations.py docs/architecture/coordination-and-review.md docs/implementation-plan.md
git commit -m "docs: verify coordination and oversight release"
```
