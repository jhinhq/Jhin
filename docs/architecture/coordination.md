# Coordination and Oversight

Peer/cross-team work requests, organization awareness, configurable work
reviews, and manager rollups. These features reuse the task engine, the
tool gateway, approvals, conversations, and Temporal; nothing here creates a
second execution or authorization path.

Spec: `docs/superpowers/specs/2026-08-17-jhin-ai-company-experience-design.md`
(Organization awareness, Agent-to-agent communication, Management/review,
Authorization and safety).

## Invariants

- A manager, teammate, or collaborator relationship is routing context,
  never authority. Every agent action still needs a live capability grant.
- Explicit deny and structural guards always beat a request or a review.
- Cross-workspace ids return 404 (API) or a recorded denial (gateway).
- Work requests are distinct from delegation: an accepted request creates
  exactly one standalone task (`parent_task_id` is NULL); a decline creates
  none; retries never create a second request or task.
- One exception yields at most one review (`(workspace_id, trigger_key)` is
  unique). A review gates and records only: it can never approve a
  human-reserved approval, synthesize a grant, or override tool policy.
- Mandatory (`fail_closed`) pre-action/before-close reviews with no
  resolvable AI reviewer fail closed: they park on a human-assigned review
  that shows up in Attention.
- Directory, roster, and rollups expose public identity and structured
  status only — never system prompts, grants, model config, memories,
  transcripts, or conversation text.

## Data model (migration `0018`, down revision `0017`)

### `work_request`

| column | notes |
| --- | --- |
| `workspace_id`, `conversation_id` | workspace boundary; the requester task's conversation |
| `requester_agent_id`, `requester_task_id`, `requester_run_id` | who asked, from which work |
| `root_task_id` | top of the requester lineage (delegation parents + earlier request hops); the ping-pong guard keys on it |
| `requested_by_user_id` | set when a human opened it on behalf of the agent |
| `target_agent_id` | who is asked (`requester <> target` check) |
| `title`, `description`, `expected_output` | immutable ask |
| `status` | `pending`, `clarification_requested`, `accepted`, `declined`, `completed`, `failed` |
| `idempotency_key` | unique per workspace |
| `depth` | 1 for ordinary work, n+1 when opened from a request-created task |
| `created_task_id` | unique; set only on accept |
| `response`, `responded_at`, `completed_at`, `metadata_json` | target's answer, timestamps, display names |

### `review_policy`

`name`, `scope_kind` (`workspace` | `team` | `agent` | `task_type`) with a
three-way shape check (`scope_id` only for team/agent, `scope_key` only for
task_type), `enabled`, `mode` (`pre_action` | `before_close` | `post_action`
| `periodic`), `conditions_json` (list of `{kind, threshold?}`),
`reviewer_selector_json` (`{kind: reporting_manager|agent|team_role|human,
agent_id?, role_label?, fallback_agent_id?, fallback_to_human}`),
`fail_closed`, `priority` (lower wins), `period_seconds`.

Condition kinds: `elevated_action`, `destructive_action`, `cost_threshold`
(micro-dollars), `token_threshold`, `time_threshold` (seconds),
`tool_failure`, `test_failure`, `approval_denied`, `policy_denied`,
`blocked`, `low_confidence` (threshold 0..1, default 0.5),
`cross_team_request`, `explicit_request`, `always`.

### `work_review`

`policy_id`, `task_id`, `run_id`, `tool_call_id`, `work_request_id`,
`subject_agent_id` (whose work), unique `trigger_key`, `mode`,
`evidence_json` (tool name/risk, matched conditions, summary/artifacts/risks
from an explicit request, `fail_closed`), `reviewer_type` (`agent` | `human`
| `none`) with the matching typed reviewer id, `status` (`pending`,
`approved`, `changes_requested`, `skipped`, `escalated`), `verdict`
(`approve` | `changes_requested` | `escalate`), `feedback`, `requested_at`,
`decided_at`, `decided_by_user_id` / `decided_by_agent_id`.

## Pure policy (`jhin_policy`)

- `evaluate_work_request(grants, WorkRequestFacts, CoordinationSettings) -> WorkRequestDecision`.
  Order: self-request → target exists/active/available → ping-pong → depth →
  requester open-request cap → requester hourly cap → target active
  request-task cap → explicit deny → allow grant + `targets` scope
  (`subordinates` | `team` | `any`; **missing means `team`**) + optional
  `target_agent_id` pin.
- `coordination_settings(workspace.settings_json)` reads
  `settings_json.coordination` with defaults `max_request_depth=4`,
  `max_pending_requests_per_agent=10`, `max_requests_per_agent_per_hour=30`,
  `max_active_request_tasks_per_agent=3`.
- `evaluate_review_policies(policies, ReviewContext) -> ReviewDecision`:
  enabled policies of the context's mode whose scope applies and at least
  one condition fires; ordered by scope specificity (agent → task_type →
  team → workspace), then priority, then id. `blocking` is true for
  pre-action/before-close.
- `resolve_reviewer(selector, ReviewerCandidates, mode, fail_closed) -> ReviewerResolution`:
  primary → `fallback_agent_id` → human (when `fallback_to_human`) →
  `fail_closed` (mandatory blocking) or `skipped`. An agent never reviews its
  own work; inactive agents are never reviewers.

## Services and tools (`jhin_tools`)

Shared by the API and the agent worker so there is one implementation.

| module | functions | gateway tools |
| --- | --- | --- |
| `directory.py` | `search_directory`, `build_roster`, `render_roster`, `DirectoryEntry` allowlist | `organization.directory.search` (capability `organization.directory.read`, read) |
| `work_requests.py` | `load_work_request_facts`, `create_work_request`, `accept_work_request`, `decline_work_request`, `request_clarification`, `finalize_work_request`, `root_task_id` | `organization.request_work` (capability `organization.work.request`, write, defers scope to the validator), `organization.respond_work_request` (capability `organization.work.respond`, write, structurally limited to the target agent) |
| `reviews.py` | `check_review_gate`, `evaluate_review_event`, `open_review`, `decide_review`, `load_policy_specs` | `organization.review.request` and `organization.review.submit` (capability `organization.review.request`, write; submit is structurally limited to the assigned AI reviewer while pending) |
| `rollups.py` | `build_manager_rollup`, `render_manager_rollup` | — |

All four tool groups register through `build_builtin_catalog` exactly like
the Phase 8 organization tools.

### Work request flow

1. Requester calls `organization.request_work` (or a human posts
   `POST /work-requests`). The validator loads live facts and runs
   `evaluate_work_request`; denials are recorded tool calls.
2. `create_work_request` persists the row (idempotent on
   `idempotency_key`; the tool derives `run:{run_id}:{sha256}` when the model
   gives none) and a `question` message on the requester's task with
   `kind="work_request"`, `work_request_id`, `target_agent_name`,
   `from_agent_name`.
3. The target responds with `organization.respond_work_request` (or an admin
   via `POST /work-requests/{id}/accept|decline|clarify`):
   - accept → one task (`origin: work_request`, `temporal_workflow_id =
     task-<id>`, same conversation/correlation as the requester task), a
     `status` message, audit `work_request.accepted`; repeat accepts return
     the same task;
   - decline → status only, no task; clarify → `question` back.
4. Durable execution: `WorkRequestTaskWorkflow(work_request_id, task_id,
   agent_id)` (id `work-request-<request id>`) runs the task's
   `AgentTaskWorkflow` as a child under `task-<id>`, then the
   `finalize_work_request` activity marks the request completed/failed and
   posts a `result` message (summary, artifacts, risks from
   `reported_result`) to the requester's task. The API starts it directly on
   human accept; an agent accept is lifted by the worker into
   `StepResult.work_request_starts` and `AgentTaskWorkflow` starts it as an
   abandoned child (duplicate starts are no-ops).

### Review gate order

`capability/scope/validator → check_review_gate → human approval → execute`.

`check_review_gate(session, run, ToolCallIntent) -> GateResult`:

- `proceed` — no policy matched, review approved/skipped, or the mode is
  non-blocking;
- `wait_review` — a pending review exists (`review_id`, reviewer type/id);
  park the run and re-run the full authorization chain on resume;
- `blocked` — `changes_requested`/`escalated`; return `feedback` to the
  model without executing.

Trigger keys are `pre_action:{tool_call_id}:{policy_id}` (or
`{run_id}:{tool_name}` when no tool call id exists yet), so retries of the
same call find the same review. `evaluate_review_event` is the generic
entry for `before_close`/`post_action` moments.

## API

All routes are under `/api/v1/workspaces/{workspace_id}`, CSRF-protected,
404 for non-members.

| method | path | role |
| --- | --- | --- |
| GET | `/directory?q=&team_id=&expertise=&limit=` | viewer |
| GET | `/work-requests?status=&agent_id=&limit=&offset=` | viewer |
| POST | `/work-requests` (`WorkRequestCreate`, on behalf of a requester agent) | member (grants apply; admins bypass the grant, never the structural guards) |
| GET | `/work-requests/{id}` | viewer |
| POST | `/work-requests/{id}/accept` · `/decline` · `/clarify` | admin |
| GET/POST | `/review-policies` | viewer / admin |
| GET/PATCH/DELETE | `/review-policies/{id}` | viewer / admin / admin |
| GET | `/reviews?status=&reviewer=human|agent` | viewer |
| GET | `/reviews/{id}` | viewer |
| POST | `/reviews/{id}/decide` (`{verdict, feedback}`) | member for human-assigned reviews; admin for AI-assigned |
| GET | `/agents/{id}/rollup` | viewer |

`GET /activity` now projects work requests (`asked_agent` at creation,
`reported` when completed/failed/declined; ids
`work_request:<id>:asked|reported`) and pending reviews (`needs_review`,
id `review:<id>`); cards carry `work_request_id` / `review_id`. Messages
with `content_json.kind == "work_request"` are skipped so each request
appears once. `GET /attention` adds `pending_reviews` (human-assigned
pending reviews) and `counts.reviews`.

## Runtime context (`jhin_agents.context`)

`TaskContext.organization_context` and `TaskContext.manager_context` are
appended to the system prompt when set. Both blocks state that they are
routing/status context and grant nothing. `render_roster` caps 40 entries /
3 000 chars; `render_manager_rollup` caps 4 000 chars.

## Worker integration points (not yet wired into `activities.py`)

`services/agent_worker/src/jhin_agent_worker/coordination_activities.py`
exposes:

- `organization_context(session, workspace_id, agent_id)` and
  `manager_context(session, workspace_id, agent_id)` — call in
  `run_agent_step_activity` when building `TaskContext`. The manager block
  is empty for agents without reports, so it is safe to call for everyone.
- `jhin_tools.reviews.check_review_gate(session, run, ToolCallIntent)` —
  call after the gateway authorized a call and before execution; on
  `wait_review` persist a waiting state (suggested `StepResult.waiting_review_id`
  and a `review_decision` signal delivered by `decide_review`), on `blocked`
  return the feedback as the observation.
- `work_request_start_from_output(outcome.sanitized_output)` — for executed
  `organization.respond_work_request` calls, append the result to
  `StepResult.work_request_starts` and include it in the committed
  step-result bundle so `_committed_step_result` replays it.
- `CoordinationActivities.finalize_work_request_activity` — registered in
  `main.py` together with `WorkRequestTaskWorkflow`.

## Tests

- `packages/policy/tests/test_work_requests.py`, `test_reviews.py` — pure
  evaluators and reviewer resolution.
- `packages/tools/tests/test_coordination_tools.py` — directory, roster,
  request/respond through the gateway (idempotent accept, decline without
  task, ping-pong, depth, caps), review gate (dedupe, fail-closed, submit
  structural check), explicit review request, rollup privacy.
- `apps/api/tests/test_coordination_unit.py` — human create/accept/retry/
  decline with a fake Temporal client, policy CRUD and scope validation,
  human decisions, activity/attention projection, rollup and directory.
- `packages/workflows/tests/test_work_request_task_workflow.py` and
  `services/agent_worker/tests/test_coordination_activities.py`.
