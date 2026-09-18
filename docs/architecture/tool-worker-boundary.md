# Deterministic tool-worker boundary

Phase 10 separates nondeterministic model reasoning from deterministic external
effects without creating a second source of truth. Temporal remains the durable
orchestration authority, PostgreSQL remains the product and invocation-claim
authority, and NATS remains transport. The runtime sequence for a new agent
step is exactly:

`resolve advertised tools → reason/bind → ordered execute → commit`

The workflow and activity ownership is explicit:

| Stage | Runtime owner | Temporal queue | Durable authority |
| --- | --- | --- | --- |
| Resolve advertised tools | tool-worker | `jhin-tool-queue` | current grants filtered against the executable catalog |
| Reason and atomically bind the complete call set | agent-worker | `jhin-agent-queue` | paired append-only run events under the locked run transaction |
| Execute the bound calls in ordinal order | tool-worker | `jhin-tool-queue` | stable PostgreSQL `ToolCall` and optional `Approval` claims |
| Commit transcript, timeline, totals, and status | agent-worker | `jhin-agent-queue` | an idempotent projection from the bound events and durable claim rows |

The agent worker never imports connector executors or a sandbox client. The
tool worker never imports model or prompt code. A worker restart may repeat an
activity, but it cannot choose a new call or create a new effect identity.

## Atomic bind and private reasoning

For a new step, `reason_agent_step` inserts two consecutive `RunEvent` rows in
one `AgentRun FOR UPDATE` transaction and commits them together before an
effect can start:

- `agent.step.tool_manifest` is the canonical provider-independent call set.
  Its durable payload contains only the step and ordered manifest. Each usable
  call contains the four canonical JSON scalars `ordinal`, `lossless`, `tool_name`, and `arguments_json`. It contains no completion, provider call
  ID, usage, transition, policy result, or tool outcome.
- `agent.step.reasoning` is a separate agent-only append-only record. It owns
  bounded completion, usage, transitions, provider call IDs, latency, and the
  other model metadata needed for agent-side projection. Its public API payload is always `{}`.

If the transaction fails, neither event exists. A retry finding both valid rows
reuses the pair without calling the model. A partial pair fails closed. The only
legacy repair path may append a missing reasoning sidecar for an already-bound
Phase 9 manifest, and only after a fresh model result reproduces that manifest
exactly; it never rewrites the manifest.

A third record rides in the same commit on the new-bind path, at `seq + 3`
directly after the pair: `agent.step.tools_offered`, the tool names the model
was offered on that step — the same list `to_model_tool_schemas` rendered.
Its payload is `{step, count, tools, truncated}`: names only (each cut to 200
characters), the first 256 in advertised order, `truncated` when there were
more. The replay path and the legacy sidecar repair never write it, and the
pair lookups select by their own event types, so it is invisible to them. Its
public API payload keeps exactly those four keys and fails closed to
`{"count": 0, "tools": [], "truncated": false}` on anything malformed. It is
what the task timeline renders as **Tools offered**, and what the next chat
turn compares against to tell the agent its tools changed
(docs/operations/agent-access.md).

`execute_bound_tool` receives only workspace ID, run ID, step index, and
ordinal. Its SQL projection selects only `ordinal`, `lossless`, `tool_name`,
and `arguments_json` from the requested manifest entry. It never loads
`agent.step.reasoning`, completion, usage, transitions, provider IDs, another
ordinal, or any other step history. Agent-side commit activities later reload
the private reasoning record and durable claim rows by stable IDs to build the
sanitized projection; they do not rerun policy or execute a connector.

## Effect and transaction ownership

The following paths all cross `jhin-tool-queue`:

- **Ordinary calls:** tool-worker reloads the one canonical call, current run
  context, live grants, connection state, and executable definition. After
  grant, scope, and validator authorization the gateway evaluates the
  pre-action review gate (`docs/architecture/coordination.md`): a blocking
  review is a recorded denial and a pending review parks the call as a
  `pending_review` row under the stable invocation id — both persisted
  before any approval row or execution claim exists. Otherwise the gateway
  inserts or reloads the stable `ToolCall` claim before dispatch and commits
  its sanitized terminal result. Agent-worker only projects that row.
- **Approval:** `AgentTaskWorkflow` owns the durable wait and signal. After a
  decision, tool-worker reloads the current PostgreSQL `Approval`, tool call,
  manifest binding, and authorization context, then resolves the existing
  claim. Agent-worker commits only the sanitized approval projection.
- **Review:** the same shape. `AgentTaskWorkflow` parks on the
  `review_decision` signal (`waiting_review` keeps the admission slot).
  After a decision, tool-worker's `resolve_bound_tool_review` reloads the
  PostgreSQL `work_review`, the `pending_review` tool call, manifest binding,
  and authorization context, then `ToolGateway.resolve_review` either
  records the reviewer's denial, stages a human approval on the same row, or
  CAS-claims `pending_review → executing` and runs the effect once.
  Agent-worker's `commit_review_projection` commits only the sanitized
  projection (idempotent on the `review.<status>` run event) and may hand
  the workflow straight into the approval wait.
- **Trigger and engineering sync:** tool-worker reloads the task, trigger,
  enabled `comment_back` standing authority, connection, run, and external
  identity. It claims `system.trigger.sync_external` under the stable sync ID
  before posting. A terminal claim replays; an abandoned executing claim is
  `execution_unknown` and is never reposted automatically.
- **Sandbox cleanup:** tool-worker validates the workspace/run binding and
  calls the idempotent runner deletion for `run-{run_id}`. New workflows do
  this before agent-worker finalizes the run projection. The legacy
  `finalize_run` handler is only an IDs-only coordinator over the same
  tool-queue cleanup workflow.

`ToolCall` stores sanitized input/output, state, duration, error, and approval
binding; `Approval` stores its sanitized decision context. There is no second
outcome table or outcome run event. Stable database invocation claims remain
the at-most-once authority, including after a Temporal activity retry.

## Stable identities and patches

Ordinary calls use UUIDv5 namespace
`4f0ac960-eab4-5f17-9b65-9f9bcbf3e0a8` with name
`v1:{run_id.hex}:{step_index}:{ordinal}`. Trigger sync uses UUIDv5 namespace
`3dc26b04-1af9-5ec5-a0ea-d7d95c3a393b` with name
`v1:{run_id.hex}:trigger-sync`. These IDs bind retries to the same PostgreSQL
claim; changing their format is a data migration, not a refactor.

Three independent Temporal patches preserve the commands already recorded in
Phase 9 histories:

| Workflow path | Patch ID |
| --- | --- |
| Agent step, approval, cleanup, and final projection routing | `phase10-tool-worker-boundary-v1` |
| Triggered-task comment-back routing | `phase10-trigger-sync-tool-routing-v1` |
| Engineering-ticket comment-back routing | `phase10-engineering-sync-tool-routing-v1` |

Pre-patch agent activity names remain registered as coordinators. They validate
UUID identities, use `REJECT_DUPLICATE`, and start or reattach one of these
tool-queue workflows:

| Compatibility purpose | Exact workflow ID formula |
| --- | --- |
| Advertised schemas for one legacy step | `phase10-compat-advertised-{run_id}-{step_index}` |
| Ordered tools for one legacy step | `phase10-compat-tool-step-{run_id}-{step_index}` |
| One decided approval | `phase10-compat-approval-{approval_id}` |
| One trigger/engineering sync | `phase10-compat-sync-{run_id}` |
| One run workspace cleanup | `phase10-compat-cleanup-{run_id}` |

The advertised and tool-step workflows receive IDs plus bounded step/count
integers; approval, sync, and cleanup receive durable IDs only. Connector and
runner effects never fall back to the agent worker. Closed compatibility
workflow IDs reattach to their one recorded result instead of starting a new
effect.

## Upgrade and removal gate

Do not remove a legacy activity, compatibility workflow, or patch branch based
on deployment age. Operators must query all open histories for every affected
workflow type and prove that none predates the corresponding patch. They must
also apply the configured Temporal retention policy and prove that no closed pre-patch history is queryable. Keep all handlers until both conditions hold.

Calling `workflow.deprecate_patch` is not allowed in Phase 10 subproject 1.
Patch deprecation and handler removal require a later, separately reviewed
operation after the history and retention gate above is satisfied.

## Test-only crash matrix

Crash barriers are disabled no-ops unless an explicit test configuration names
one exact barrier and stable identity. Any barrier setting makes production
worker startup fail. The upgrade harness verifies these exact outcomes:

| Exact barrier | Recovery outcome |
| --- | --- |
| `phase10.agent.before_manifest_bind.v1` | reruns the model; no tool effect |
| `phase9.agent.after_manifest.before_effect.v1` | reuses the committed pair; the model and bind are not repeated |
| `phase10.tool.before_claim.v1` | executes once after recovery under the same stable claim ID |
| `phase10.tool.after_claim.before_effect.v1` | executes once after recovery under the same stable claim ID |
| `phase10.tool.after_effect.before_terminal_commit.v1` | becomes `execution_unknown`; no automatic retry repeats an effect that may have completed |

The retained `phase9` name at the agent post-bind boundary is intentional: it
is the marker already captured by frozen Phase 9 histories. At the one
genuinely ambiguous gap — after the effect, before the terminal commit —
`execution_unknown` is persisted and projected before the outer workflow stops
for manual reconciliation.

## The two-step claim

The claim on a stable invocation id is taken in two durable steps, and which
step a crashed call stopped at is the whole difference between recovering it
and abandoning it:

| `tool_call.status` | What it means | What recovery does |
| --- | --- | --- |
| `claimed` | this attempt owns the invocation; the executor has not been entered | re-decide from live grants, policy and review state, then dispatch once |
| `executing` | the compare-and-set from `claimed` committed and the executor was entered | ask the tool: `redispatch_is_safe` re-decides and dispatches again (bounded), anything else is `execution_unknown` |

What a dispatched row costs is therefore the tool's own declaration, not a
constant. `ToolDefinition.redispatch_is_safe` says whether running this tool a
second time could repeat an effect the first dispatch may already have
produced; only a tool whose every effect stays on a disk Jhin owns says yes.
For those, recovery re-opens the claim and dispatches again, at most
`MAX_DISPATCH_ATTEMPTS` times counted from the durable `tool.call.dispatched`
trail — so a worker killed mid-retry resumes into the same budget instead of a
fresh one. A budget that runs out is recorded as a *failure*
(`execution_not_confirmed`), which is honest for a call that cannot have
reached anything outside Jhin and is a thing an agent can read and act on. For
every other tool — `cli.repository.push`, `cli.command.execute`, anything that
posts, merges or dispatches — the answer is still `execution_unknown`, and
that is the at-most-once guarantee, untouched.

The one place that decides this without being able to re-run the call is the
definition-independent re-entry, reached when the tool has been revoked or its
schema no longer accepts the stored arguments. It asks the same question and
gives the same two answers; it just closes the call rather than re-dispatching
it, because there is no validated input left to dispatch. The deliberate
exception is a *rejected* approval that nevertheless reached its executor: a
person has already answered a different question there, and that call stops and
is shown to them.

The move from `claimed` to `executing` is a single compare-and-set committed
on the statement before the executor call, on its own connection wherever the
process has an isolated session factory — so it neither releases the shared
connection locks an approved call holds across its executor, nor waits behind
them. Two attempts that both believe they hold the claim both try that
compare-and-set; exactly one wins, and the loser reads `executing` and
reconciles as unknown. At-most-once therefore does not depend on the
invocation lifecycle advisory lock, which is an optimization that keeps
attempts from doing redundant work.

Both verdicts are audited on the `tool_call` target and both name their
evidence rather than only their conclusion: `tool.call.dispatched` marks the
compare-and-set, `tool.call.claim_reentered` records a claim recovered
because its row was still `claimed`, and `tool.call.execution_unknown` carries
`evidence: the tool_call row was dispatched to its executor`.

An executor that fails *after* being entered is not covered by this: it is the
executor's own `ToolExecutionError.side_effect_possible` that decides between
a recorded failure and `execution_unknown`, and a raw exception out of an
executor is always read as "an effect may have happened".

## What a sandbox failure costs

`execution_unknown` stops the run for manual reconciliation. That is the right
price for a mutation nobody can vouch for and a ruinous one for an ordinary
failure, so every sandbox failure states which it is rather than defaulting to
the expensive answer by being untyped:

| Failure | Classification | Why |
| --- | --- | --- |
| a Jhin-authored job that finished non-zero (checkout, any `cli.file.*`) | recorded failure | the job is finished with a known exit code, and none of these tools can change anything outside the sandbox — the checkout's only remote traffic is `ls-remote`, `fetch` and `clone` |
| `cli.test.run` exiting non-zero | not a failure at all | it returns `passed: false` with its output |
| `git push` the remote refused | recorded failure, `push_rejected` | the script asks `ls-remote` what the remote holds and only claims this when the branch is *not* there at this workspace's HEAD — proof, not assumption |
| any other push failure | `execution_unknown` | a 502 after the ref was updated looks exactly like one before it |
| the runner stopped answering | depends on the job | a job with no egress, or one whose only remote traffic is a read, changed nothing outside the sandbox; `cli.command.execute` with `network: internet` may have reached the world, and stays unknown |
| an unusable connection, a missing GitHub connection, a malformed repository | recorded failure | reached before a credential is minted and before any container exists |

Every one of these carries `detail`: the exit code and the container's or the
provider's own stderr, bounded and redacted through the same
`sanitize_payload` path as any other tool output. An `execution_unknown`
carries it too — uncertainty about *whether* an effect happened is not a reason
to discard the evidence about *what* happened, and an unknown outcome with an
empty output document is the one an operator cannot reconcile at all.

The `sandbox_job` row and its audit events are written on their own connection
and committed as they happen, not in the gateway's transaction. A tool call
that fails rolls that transaction back, and a job that ran is a fact the tool
call does not get to take back with it.

## One job at a time per workspace volume

The sandbox runner runs at most one container per `workspace_key`, and a job
whose workspace is busy waits for it (`sandbox_workspace_queue_seconds`, then
fails having run nothing). The exclusion has to live there rather than in the
control plane, because the case it exists for is a tool worker that is *gone*:
its advisory lock protects an invocation, a dead worker leaves a container
rather than a coroutine, and the re-dispatch that follows carries a new
`job_id` — so the duplicate-id check in `JobManager.submit` can never fire on
it. Two containers on one disk is a data-loss bug for `cli.repository.checkout`
in particular, whose first act is to make the tree match the remote.

## Shutting down, and how long it may take

SIGTERM starts a clock nobody here controls: Docker's default stop grace is ten
seconds, and `compose.yaml` sets no `stop_grace_period` for `tool-worker`. The
shutdown spends it in three budgets, owned by `jhin_tool_worker.drain`:

| Budget | Seconds | What it buys |
| --- | --- | --- |
| drain (`tool_worker_drain_timeout_seconds`) | 4 | in-flight tool calls finish instead of being cut off |
| cancel cleanup (`_CANCEL_CLEANUP_SECONDS`, per job) | 2 | a cancelled job's `sandbox_job` row is closed rather than orphaned |
| telemetry flush (`runtime.shutdown`) | 2 | spans and metrics reach the collector |

Eight of ten, leaving two seconds for `worker.__aexit__`, the connection pool,
and the arithmetic being off. The margin is not the guarantee: a job that
outlives the drain is still cut off, and under SIGKILL no shutdown path runs at
all. The real backstop for both is the sandbox sweep
(`jhin_tool_worker.sandbox_reconcile`, also available on demand as
`jhin-sandbox-reconcile`), which closes an orphaned row from the runner's own
account of the job. What the budgets buy is that an ordinary redeploy does not
need the backstop.

## Rolling this change back

`tool_call.status` gained one value, `claimed`. The column is `varchar(32)`, so
neither the deploy nor the rollback needs a migration — but the previous
release refuses a status it has never heard of
(`GatewayStateError("... has unexpected status 'claimed'")`), and a row left in
that state fails the run that owns it. Rolling back therefore has exactly one
manual step, run once against the live database from the tool worker image:

```
docker compose run --rm --no-deps tool-worker jhin-tool-calls-rollback
```

**Run it before you revert anything.** The command is part of the release that
introduced `claimed` and exists in no earlier image, so `tool-worker` has to
still resolve to this version for that line to work at all. An operator who
reverts first has not lost the step, only the short spelling of it: it becomes
`JHIN_VERSION=<the release being left> docker compose run --rm --no-deps
tool-worker jhin-tool-calls-rollback`, or the same command from that release's
own compose file. `docs/deployment.md` (**Rolling back**) has the ordered
procedure.

It closes every `claimed` row as a *failed* tool call carrying
`rolled_back_before_dispatch`, which the old release replays as an ordinary
failed call. Failed rather than `executing` on purpose: `claimed` proves the
executor was never entered, and writing `executing` would throw that proof away
and hand an operator a pile of unknowns to reconcile for calls that provably
did nothing. Pass `--dry-run` to see what it would close; it is idempotent, and
it is guarded on `claimed`, so racing a live worker is safe in both directions.
Nothing else needs undoing: `redispatch_is_safe` lives in code, and every other
status the new gateway writes is one the old one already understands.
