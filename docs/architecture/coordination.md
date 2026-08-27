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
- A pending pre-action review parks the exact tool call durably
  (`tool_call.status = pending_review`, `tool_call.review_id`), holds the
  run's concurrency slot as `waiting_review`, and resumes that same call —
  never a re-issued one — through the normal authorization → human approval
  → claim → effect path exactly once. The Postgres `work_review` row is the
  authority; the `review_decision` signal only wakes the workflow.
- Directory, roster, and rollups expose public identity and structured
  status only — never system prompts, grants, model config, memories,
  transcripts, or conversation text.

## Default collaboration grants (safe-by-default)

A company of agents that work together is the product promise, so an ordinary
agent must be able to *ask a colleague for help without a human first
hand-granting an obscure capability*. Every agent therefore starts with a
fixed **collaboration baseline** of three allow grants
(`jhin_policy.collaboration_grant_specs`, applied by the wizard's
"Collaboration" preset, the `organization.create_agent` tool, and the dev
seed):

| capability | scope | why it is safe by default |
| --- | --- | --- |
| `organization.directory.read` | — | public identity and public work status only (no prompts, grants, model config, memories, or transcripts). Covers `organization.directory.search` and `organization.colleague_status`. |
| `organization.work.request` | `targets: any` | a request only *asks*. It cannot make the target do anything the target is not already permitted to do (the target's own grants still gate everything it then does); the target — or a human on its behalf — accepts, declines, or asks for clarification; an accept creates at most **one** task that stays visible in the conversation/Activity and stoppable; and every structural guard (no self-request, target active/available, depth, per-agent open/rate/active-task caps, and no ping-pong) runs in `evaluate_work_request` regardless of the grant. `targets: any` lets a small company ask across teams; the missing-scope default remains `team`. |
| `organization.work.respond` | — | structurally limited to the request's target agent, so an agent can be asked as well as ask. |

`organization.delegate` is deliberately **excluded**: delegation transfers
ownership/authority (a blocking parent wait, a child in the lineage), so it
stays deny-by-default with the restrictive delegation permission model.

This is a *platform* default, not a capability an agent chooses: the calling
agent cannot pick these grants, `organization.create_agent` is still
elevated → human approval, and no higher-authority capability (delegation,
connectors, sandbox, agent management) is ever auto-granted. Existing
workspaces are **not** mass-granted by migration (that would silently change
authority and surprise admins); an admin adds the baseline per agent through
the normal grants API, or toggles the "Collaboration" preset on the agent's
Tools tab.

## Data model (migration `0018`, down revision `0017`; parking in `0019`)

Migration `0019` (down revision `0018`) adds the nullable, indexed
`tool_call.review_id` (FK `work_review.id`, `SET NULL`) and the domain adds
`ToolCallStatus.PENDING_REVIEW` and `RunStatus.WAITING_REVIEW` (a member of
`RUN_ACTIVE_STATUSES`, so a parked run keeps its admission slot exactly like
`waiting_approval`).

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
  `max_active_request_tasks_per_agent=3`, `auto_activate_targets=true`.
  `open_requests_by_requester` counts every request the agent has out that
  has not come back — `pending`, `clarification_requested` **and**
  `accepted`. Counting only the undecided ones would make that cap vanish
  the moment requests auto-activate, which is exactly when a bound on
  concurrent asks matters.
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
| `directory.py` | `search_directory`, `build_roster`, `render_roster`, `DirectoryEntry` allowlist, `find_agent_by_reference` / `resolve_agent_reference` (the one name→agent resolver) | `organization.directory.search` and `organization.colleague_status` (capability `organization.directory.read`, read) |
| `work_requests.py` | `load_work_request_facts`, `create_work_request`, `accept_work_request`, `decline_work_request`, `request_clarification`, `finalize_work_request`, `root_task_id` | `organization.request_work` (capability `organization.work.request`, write, defers scope to the validator), `organization.respond_work_request` (capability `organization.work.respond`, write, structurally limited to the target agent) |
| `reviews.py` | `check_review_gate`, `evaluate_review_event`, `open_review`, `decide_review`, `load_policy_specs` | `organization.review.request` and `organization.review.submit` (capability `organization.review.request`, write; submit is structurally limited to the assigned AI reviewer while pending) |
| `rollups.py` | `build_manager_rollup`, `render_manager_rollup`, `build_colleague_status` | — |

All four tool groups register through `build_builtin_catalog` exactly like
the Phase 8 organization tools.

### Colleagues are referred to by name

The roster prints agent ids only for an agent holding a tool that consumes
one, and the platform preamble forbids putting an id in a message to a
person — so *a name is the only handle an agent reliably has for a
colleague*. Every tool that takes a colleague therefore accepts
`..._agent_name` as well as `..._agent_id`, and they all resolve through
`jhin_tools.directory.resolve_agent_reference` rather than inventing their
own matching:

- an id wins when both are given; otherwise matching is case-insensitive
  and runs in decreasing strength — exact name, exact slug, exact role
  title, then a *unique* substring of a name or role title;
- a needle matching several agents fails as `agent_name_ambiguous` instead
  of silently picking one;
- an unknown name fails as `agent_not_found` **naming the candidates**, so
  the model's next move is a retry with a real colleague rather than "I
  don't know who that is". The candidate list covers discoverable, active
  agents only: a wrong name must never become a way to enumerate agents the
  directory hides.

`organization.request_work` matches over every agent in the workspace (so
the structural deny codes `target_not_found` / `target_inactive` /
`target_unavailable` keep their meaning for a caller that already holds an
id), while `organization.colleague_status` matches over discoverable, active
agents only — a status lookup is itself a discovery.

### `organization.colleague_status` (read, no approval)

"What is the CTO doing right now?" is answerable from Jhin's own rows, and
an agent that cannot answer it looks like it works alone. The tool takes
`agent_name` (or `agent_id`) and returns `ColleagueStatus`: public identity,
the titles and lifecycle states of the tasks that colleague is working on,
has queued, and recently finished, the live run status, when they were last
active, counts of what is waiting on them (unanswered work requests, reviews
assigned to them, approvals pending on their work), and one plain-language
`summary` sentence assembled from those fields.

Why each field is safe to show any colleague: identity is exactly what the
roster already carries; a task **title** and its state are already visible
workspace-wide in the shared Activity feed, so naming them leaks nothing
new; the load figures are counts only, so "they are backed up" is answerable
without naming what they are backed up on. Deliberately absent, by
construction rather than by filtering: another agent's system prompt,
capability grants, model configuration, private metadata, memories, message
or conversation content, task **descriptions**, and reported-result
summaries (the manager rollup carries those under a reporting line; a peer
has no such standing). Task/run/review ids are omitted too — nothing here
consumes them and an agent only tends to echo them at a person. Every query
is pinned to the resolved agent's `workspace_id`, so cross-workspace reads
are impossible. `ColleagueStatus.model_fields` is asserted verbatim in
`packages/tools/tests/test_coordination_tools.py`, so widening the payload
is a deliberate act.

### Work request flow

1. Requester calls `organization.request_work` (or a human posts
   `POST /work-requests`). Only two arguments are genuinely required of the
   model — `target_agent_name` and `description` — because the commonest ask
   in the product ("ask the CTO what he's working on") is a question, not a
   work package; `title` defaults to the ask's own first sentence
   (`derived_title`, deterministic so the default idempotency key is stable).
   The validator resolves the colleague, loads live facts, and runs
   `evaluate_work_request`; denials are recorded tool calls.
2. `create_work_request` persists the row (idempotent on
   `idempotency_key`; the tool derives `run:{run_id}:{sha256}` from the
   *resolved* target id, the title, and the description when the model gives
   none, so naming a colleague and passing their id are the same request) and a `question` message on the requester's task with
   `kind="work_request"`, `work_request_id`, `target_agent_name`,
   `from_agent_name`.
3. **Auto-activation** (`activate_work_request`, same transaction as the
   create). A permitted request starts its target instead of waiting for a
   human: it calls the *same* `accept_work_request` and returns
   `created_task_id` + the target's `agent_id` in the tool output. Nothing
   else changes — one task, `origin: work_request`, and the task metadata
   records `work_request.auto_activated = true` while the audit row is
   written with `actor_type = system` (the platform accepted, not the
   target agent). Idempotent: a retried invocation returns the existing
   task, never a second one.
4. The target can still respond explicitly with
   `organization.respond_work_request` (or an admin via
   `POST /work-requests/{id}/accept|decline|clarify`) — that is the path
   when `auto_activate_targets` is off, when a request came back for
   clarification, or when a human overrides:
   - accept → one task (`origin: work_request`, `temporal_workflow_id =
     task-<id>`, same conversation/correlation as the requester task), a
     `status` message, audit `work_request.accepted`; repeat accepts return
     the same task;
   - decline → status only, no task; clarify → `question` back.
5. Durable execution: `WorkRequestTaskWorkflow(work_request_id, task_id,
   agent_id)` (id `work-request-<request id>`) runs the task's
   `AgentTaskWorkflow` as a child under `task-<id>`, then the
   `finalize_work_request` activity marks the request completed/failed and
   posts a `result` message (summary, artifacts, risks from
   `reported_result`) to the requester's task. The API starts it directly on
   human accept; an agent accept **and an auto-activation** are lifted by
   the worker into `StepResult.work_request_starts`
   (`organization.respond_work_request` or `organization.request_work`,
   both through `work_request_start_from_output`) and `AgentTaskWorkflow`
   starts it as an abandoned child (duplicate starts are no-ops). The child
   carries a 6-hour `execution_timeout`, so a task that never finishes ends
   as `run_status = "timed_out"` and the request is finalized `failed` with
   a readable reason rather than holding a target slot forever.

### Auto-activation: why acceptance is not human-in-the-loop

The product promise is a company of agents that work together, and the
commonest ask in it is "message the CTO and ask what they're working on".
While acceptance was human-in-the-loop that ask produced a `pending` row,
a "their response is pending" reply, and then nothing: no task was ever
created, so no answer ever existed, and the requester's own run had already
finished by the time anyone could have accepted. The loop has to close by
itself.

Auto-activation is a deliberate **policy** change, and the security
argument for it is that a *request is not an authority transfer*:

- A request cannot make the target exceed its own grants. Everything the
  created task then does goes through the tool gateway, which re-decides
  each call against the **target's** live grants, rules, validators, review
  policies and human-approval requirements. The requester's grants are
  irrelevant to it.
- It is not delegation. No lineage, no `parent_task_id`, no blocking
  parent wait, no ownership handover; `organization.delegate` stays
  deny-by-default with the restrictive delegation model.
- The created task is an ordinary task: visible in Activity and the
  conversation, stoppable, budgeted, and admitted through the same
  concurrency slots as any other work.

What is left is **cost and runaway loops**, so every guard that bounds
those still runs, unchanged, in `evaluate_work_request` *before* the row
exists: no self-request; target must exist, be active and be available;
chain depth ≤ `max_request_depth`; requester's outstanding-request cap and
hourly rate cap; the target's `max_active_request_tasks_per_agent`;
ping-pong prevention on the root task; explicit deny; and the grant's
`targets` scope. A refusal is a recorded tool denial whose `reason` is
plain language, so the requester tells the person *why* instead of
promising an answer that will never come.

`coordination.auto_activate_targets` (default **true**) turns it off per
workspace. It defaults on because that is the behaviour that makes the
product work out of the box; with it off, requests stay `pending` for the
admin accept/decline endpoints and the tool's `detail` says exactly that,
so the requester reports "waiting for a human to approve it" rather than
"an answer is on its way".

### Never silently stuck

A request must always reach a terminal state with a reason a person can
read:

- A guard refuses → the request is never created; the denial code and
  reason reach the model as the tool's result.
- Activation cannot proceed after the row exists (e.g. the colleague went
  inactive in between) → `fail_work_request` drives the row to `failed` and
  posts the same `result`-shaped message into the requester's task, so the
  conversation shows the reason.
- The target's task fails, is cancelled, or runs past the time box →
  `finalize_work_request` marks the request `failed` and posts a plain
  sentence ("their run failed before they could answer", "they did not
  finish within the time allowed…"). The failed task itself also shows up
  in the Attention inbox through the ordinary recent-failures projection.
- The `WorkRequestTaskWorkflow` child cannot be started at all →
  `AgentTaskWorkflow._start_work_request_task` finalizes the request as
  `failed` rather than leaving an `accepted` row whose task will never run
  (and never fails the requester's own run over it; a duplicate start stays
  a no-op).
- Auto-activation switched off → the row stays `pending` **by design**, is
  listed by `GET /work-requests?status=pending`, and the requester was told
  so in words.

The colleague's task description is framed as an incoming ask
(`"<Requester> asked you this. Answer it yourself — do not pass it on."`
followed by the request verbatim). Without that framing the bare question
reads as an instruction to go and ask somebody — observed live: the CTO
answered "I can't message myself" after trying to relay its own request
onward.

### How the answer gets back to the person

The requester's run has normally finished by the time the colleague
answers, so nothing can be handed back to it — the answer is delivered into
the **conversation** instead, which is what the person is actually looking
at:

- `accept_work_request` gives the created task the requester task's
  `conversation_id`, so the colleague's own final reply is an ordinary
  visible agent message in that conversation, attributed to them
  (`sender_name`), rendered as a normal bubble by
  `apps/web/components/chat/transcript.tsx`.
- `finalize_work_request` additionally posts the structured `result`
  message (summary/artifacts/risks) on the *requester's* task, which the
  transcript folds into the collapsed agent↔agent exchange row along with
  the `question` and `accepted` cards.

So the reader sees a quiet "…updates with <colleague>" row for the
mechanics and the colleague's actual answer as a bubble, without re-asking
and without the requester having to be woken and pay for another model
call.

### Review gate order

`capability/scope/validator → check_review_gate → human approval → execute`.

`check_review_gate(session, run, ToolCallIntent) -> GateResult`:

- `proceed` — no policy matched, review approved/skipped, or the mode is
  non-blocking;
- `wait_review` — a pending review exists (`review_id`, reviewer type/id);
  the gateway parks the call and the run resumes on the decision (below);
- `blocked` — `changes_requested`/`escalated`; return `feedback` to the
  model without executing.

Trigger keys are `pre_action:{tool_call_id}:{policy_id}` (or
`{run_id}:{tool_name}` when no tool call id exists yet), so retries of the
same call find the same review. `evaluate_review_event` is the generic
entry for `before_close`/`post_action` moments.

### Durable review parking (the approval path, mirrored)

Parking reuses the approval machinery one-for-one; there is no second wait
mechanism:

| approval wait | review wait |
| --- | --- |
| `ToolCall.status = pending_approval`, `approval_id` | `ToolCall.status = pending_review`, `review_id` |
| `GatewayOutcome.status = needs_approval` | `needs_review` (`review_id`) |
| `BoundToolResult.stop_reason = needs_approval` | `needs_review` (`review_id`) |
| `StepResult.waiting_approval_id` → `RunStatus.WAITING_APPROVAL` | `StepResult.waiting_review_id` → `RunStatus.WAITING_REVIEW` |
| workflow status `waiting_approval`, reason `approval:<id>` | `waiting_review`, reason `review:<id>` |
| signal `approval_decision(approval_id, decision)` | signal `review_decision(review_id, status)` (`SIGNAL_REVIEW_DECISION`) |
| `resolve_bound_tool_approval` (tool queue) → `ToolGateway.resolve_approved/rejected` | `resolve_bound_tool_review` (tool queue) → `ToolGateway.resolve_review` |
| `commit_approval_projection` (agent queue) | `commit_review_projection` (agent queue) |

1. **Park.** `ToolGateway._request_once` runs the gate after grant/scope/
   validator authorization. On `wait_review` with a deterministic
   invocation id it inserts the `pending_review` row under that stable id
   (audit `tool.call.requested`, `review.requested`) and commits before
   any approval row or execution claim exists. A retried bound execution
   replays the park (`replayed=True`); it never re-evaluates or executes.
   Without an invocation id (legacy/direct callers) a pending review is
   still a recorded `review_pending` denial, because nothing could resume
   it.
2. **Project.** `commit_agent_step` maps the row to `needs_review`, emits
   `node.request_review`, writes no `tool_result` message, sets
   `run.status = waiting_review`, stores `waiting_review_id` in the
   `agent.step.committed` bundle (so a projection retry replays it), and
   publishes `agent.run.waiting_review`.
3. **Wait.** `AgentTaskWorkflow` parks on `wait_condition` until
   `review_decision` arrives or the run is cancelled. Decisions are kept in
   a dict keyed by review id, so a decision delivered *before* the park
   (the race) still resumes; worker restarts replay the durable wait.
4. **Decide.** `POST /reviews/{id}/decide` commits the decision, then
   signals the source task's `temporal_workflow_id`; repeating the same
   verdict re-sends the signal (a commit→signal failure is repairable
   without a second decision); delivery failure is a 409 only when a tool
   call is parked on the review. The AI reviewer's
   `organization.review.submit` output carries `task_id` and
   `source_workflow_id`; the agent worker lifts every executed submit into
   `StepResult.review_decisions` (durable in the committed bundle) and the
   reviewer's workflow forwards each as a `review_decision` signal to the
   source workflow (a closed target is ignored).
5. **Resume.** `resolve_bound_tool_review` reloads review/tool call/run/
   agent/task and the canonical manifest binding, fails retryably while the
   review is pending, then `ToolGateway.resolve_review` (under the call's
   lifecycle lock): terminal rows replay; `executing` becomes
   `execution_unknown`; `pending_approval` replays the staged approval;
   `pending_review` re-validates the parked input against the tool schema,
   then — for a non-approved review — records a denial with the reviewer's
   feedback (`review_changes_requested`/`review_escalated`), otherwise
   reloads grants/rules/validator live, runs the review gate again (another
   still-pending policy re-parks the same row on its review id), and
   either stages a human approval on the **same** row (`pending_approval`,
   new `Approval`) or CAS-claims `pending_review → executing` and runs the
   effect once. `commit_review_projection` then writes the `review.<status>`
   run event (its idempotency marker), the `tool_result` message, and the
   run status — or, when an approval was staged, `node.request_approval`,
   `waiting_approval`, and returns `waiting_approval_id` so the workflow
   falls straight into the ordinary approval wait.

### Periodic reviews

`PeriodicReviewWorkflow(PeriodicReviewInput{workspace_id, policy_id})`,
workflow id `review-periodic-{policy_id}` on the agent queue, is the durable
scheduler for one enabled `periodic` policy. It reloads the policy
(`load_periodic_review_policy`) before every window, computes the
epoch-aligned UTC window of `period_seconds` containing `workflow.now()`,
sleeps to its end (interrupted by the `stop`/`refresh` signals), then
`open_periodic_review` opens at most one `work_review` per window (trigger
key `periodic:{policy_id}:{window_start}`), continuing-as-new every 500
windows. Evidence is the deterministic manager rollup of the reviewer's
scope — the named reviewer agent, else the scoped agent's manager, else the
scoped team's manager — as `rollup_source_ids`/`rollup_counts` plus the
window bounds; never transcripts. The API starts the workflow when a
periodic policy is created or enabled, sends `refresh` on cadence/reviewer
changes (a duplicate start is a no-op that refreshes), and `stop` on
disable, mode change, or delete; a lost signal only delays the effect by one
window because the workflow re-reads the policy itself.

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
| POST | `/reviews/{id}/decide` (`{verdict, feedback}`) | member for human-assigned reviews; admin for AI-assigned; commits, then signals `review_decision` to the source task workflow (idempotent on repeat) |
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
appended to the system prompt when set.

`render_roster` produces the **"Your colleagues"** block. It is framed as
the agent's own knowledge of the organization — the agent is told to answer
questions like "who is on your team?" from it, by name — and in the same
breath that knowing a colleague grants no capability and that it still acts
only through its granted tools. (The earlier "Company directory (routing
context only)" header was read by models as reference data they should not
speak from: agents answered "who is on your team?" without ever naming
their manager.) `render_manager_rollup` still states plainly that it is
status context and grants nothing.

Buckets, in render order: manager, direct reports, primary team, close
collaborators, other teams the agent belongs to, and **others in this
workspace** (whatever fits in the remaining budget — discoverable, active
agents only, so a small company is fully known without a tool call). Caps
are unchanged: 40 entries / 3 000 chars for the roster, 4 000 chars for the
rollup.

Agent ids are presentation-gated, not authorization-gated:
`render_roster(..., capabilities=[...])` takes the running agent's *allowed*
capability patterns and prints `[agent id: …]` at the **end** of a colleague's
line only when the agent holds a capability in
`ID_CONSUMING_CAPABILITIES` (`organization.delegate`,
`organization.work.request`) — the tools whose arguments are agent ids —
together with a line telling it never to put an id in a message to a person.
An agent holding `organization.directory.read` additionally gets a nudge to
look a missing colleague up with `organization.directory.search` rather than
answering "I don't know". The gateway remains the only authorization check.

## Worker integration points

Wired on top of the Phase 10 tool-worker boundary
(`docs/architecture/tool-worker-boundary.md`):

- `organization_context(session, workspace_id, agent_id)` and
  `manager_context(session, workspace_id, agent_id)` — the agent worker's
  `reason_agent_step` (`jhin_agent_worker.reasoning`) builds both in a
  dedicated session before each model call and passes them as
  `TaskContext(organization_context=…, manager_context=…)`. The manager
  block is empty for agents without reports. Failures are logged and
  yield empty blocks; they never fail the step.
- `jhin_tools.reviews.check_review_gate(session, run, ToolCallIntent)` —
  evaluated inside `ToolGateway._request_once` (on the tool worker, the
  only place tools execute) after grant/scope/validator authorization and
  before approval staging or the stable execution claim, so no effect
  identity exists when the gate decides. `blocked` is a recorded denial
  whose reason is the reviewer's feedback; `wait_review` parks the call as
  `pending_review` (see "Durable review parking") and the run resumes on
  the `review_decision` signal through `resolve_bound_tool_review` +
  `commit_review_projection`. A retried invocation replays the persisted
  park or denial rather than re-evaluating.
- `review_decision_from_output(row.sanitized_output_json)` — lifts every
  executed `organization.review.submit` row into
  `StepResult.review_decisions` so the reviewer's `AgentTaskWorkflow`
  signals the source task workflow.
- `CoordinationActivities.load_periodic_review_policy_activity` /
  `open_periodic_review_activity` — `PeriodicReviewWorkflow`'s activities,
  registered in `main.py` with the workflow.
- `work_request_start_from_output(row.sanitized_output_json)` — the agent
  worker's `commit_agent_step` lifts every executed
  `organization.respond_work_request` row into
  `StepResult.work_request_starts`, stores it in the `agent.step.committed`
  bundle so a projection retry replays it, and `AgentTaskWorkflow` starts
  one abandoned `WorkRequestTaskWorkflow` per entry (duplicate starts are
  no-ops).
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
- Review parking: `services/tool_worker/tests/test_bound_review.py` (park
  before any claim, retry replay, pending → retryable, approved executes
  once, changes requested denies with feedback, review → approval → effect
  order, live re-authorization, context mismatch),
  `services/agent_worker/tests/test_review_projection.py` (waiting_review
  projection and replay, decision lifting, review projection repair/replay,
  staged approval, execution unknown, terminal run),
  `packages/workflows/tests/test_agent_task_reviews.py` (signal after and
  before the park, review → approval fall-through, cancel while parked,
  decision forwarding to the source workflow),
  `packages/workflows/tests/test_periodic_review_workflow.py`
  (time-skipping windows, stop/refresh, disabled/deleted), the API
  decide→signal and periodic lifecycle tests in
  `apps/api/tests/test_coordination_unit.py`, and
  `packages/db/tests/test_migration_graph.py` (`0019` head).
