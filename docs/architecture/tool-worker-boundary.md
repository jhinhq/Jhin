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
  pre-action review gate (`docs/architecture/coordination.md`): a pending or
  blocking review is a recorded denial, persisted before any approval row or
  execution claim exists. Otherwise the gateway inserts or reloads the stable
  `ToolCall` claim before dispatch and commits its sanitized terminal result.
  Agent-worker only projects that row.
- **Approval:** `AgentTaskWorkflow` owns the durable wait and signal. After a
  decision, tool-worker reloads the current PostgreSQL `Approval`, tool call,
  manifest binding, and authorization context, then resolves the existing
  claim. Agent-worker commits only the sanitized approval projection.
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
| `phase10.tool.after_claim.before_effect.v1` | becomes `execution_unknown`; no automatic retry dispatches the effect |
| `phase10.tool.after_effect.before_terminal_commit.v1` | becomes `execution_unknown`; no automatic retry repeats an effect that may have completed |

The retained `phase9` name at the agent post-bind boundary is intentional: it
is the marker already captured by frozen Phase 9 histories. At both ambiguous
post-claim gaps, `execution_unknown` is persisted and projected before the
outer workflow stops for manual reconciliation.
