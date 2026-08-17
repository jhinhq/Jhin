# Events and triggers

How an external webhook becomes exactly one unit of agent work (plan
sections 9, 10, 19, 26).

## The pipeline

```text
provider ──POST──▶ API webhook endpoint ──▶ NATS INGRESS ──▶ event worker
                   (verify, dedupe)          (raw event)      (normalize)
                                                                  │
        Temporal ◀── TriggerMatcher ◀────── NATS EVENTS ◀────────┘
        TriggeredTaskWorkflow                (canonical event)
              │
              ├─ prepare: create/load task (external linkage, origin message)
              ├─ child AgentTaskWorkflow (the existing agent run machinery)
              └─ sync: optional comment-back on the source system
```

### 1. Webhook ingress (`POST /api/v1/webhooks/{connector}/{public_id}`)

Machine-to-machine — no session, no CSRF. Authentication is the unguessable
public connection id plus mandatory signature verification **before any
parsing** (plan 48.5). Each connector implements its own scheme; Linear
sends `Linear-Signature` (bare hex HMAC-SHA256 of the raw body with the
per-connection secret) plus a `webhookTimestamp` (Unix ms) checked against a
60-second replay window.

Verified deliveries are recorded in `webhook_delivery` keyed by the
provider's delivery id (`Linear-Delivery`, `X-GitHub-Delivery`). A repeat of
the same delivery id is acknowledged with 202 — providers stop retrying —
and goes no further. This is **dedupe layer 1**.

The raw payload is published to the INGRESS stream
(`jhin.v1.<workspace>.ingress.<connector>.<event>`); the publisher dedupes
by deterministic event id derived from the delivery.

### 2. Normalization (event worker)

The event worker's INGRESS consumer hands the raw payload to the owning
connector's `normalize_event`, which returns canonical events
(`connector.linear.issue.updated`, `connector.github.pull_request.opened`,
…) published on the EVENTS stream. Connector-specific shapes end here —
everything downstream is generic (plan 52).

For transition matching, the Linear connector mirrors `updatedFrom` into a
`changed_from` object that parallels the event's `data` shape: if the state
changed, `data.changed_from.state` exists (with the previous `id`), so a
filter can address both the current value (`data.state.name`) and the fact
that it changed.

### 3. Trigger matching (event worker, `jhin_triggers`)

On every canonical `connector.*` event, the `TriggerMatcher` loads the
workspace's enabled triggers (cached ~5s), skips those whose `event_type` or
`connection_id` don't match the envelope, and evaluates each trigger's
`filter_json` — a safe, pure-JSON DSL:

```json
{
  "all": [
    {"path": "data.team.key", "op": "eq", "value": "ENG"},
    {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"}
  ]
}
```

- Groups: `all` / `any`, nestable (bounded depth and condition count).
- Ops: `eq neq in not_in contains exists gt gte lt lte transitioned_to`.
- Paths are dotted lookups into the event view (`event_type`, `data.…`) —
  no code execution, ever (plan 52).
- `transitioned_to` passes when the current value equals the target AND the
  same path under `data.changed_from` shows the value changed (previous
  value differs, or the changed branch exists without a resolvable previous
  value). "State CHANGES TO Todo" therefore does not fire on a title edit
  while the issue sits in Todo.

Evaluation returns per-condition explanations (actual value, previous
value, human detail) which power `POST /triggers/{id}/test` and the UI's
sample-event panel.

### 4. Idempotency (plan 9.4) — duplicates never duplicate work

For each matched trigger the matcher builds a deterministic key:

```text
sha256(trigger_id : connection_id : external_id : transition_fingerprint : time_bucket)
```

- `external_id` — the entity identity (e.g. `ENG-142`), not the delivery.
- `transition_fingerprint` — a canonical hash of the filter's resolved
  condition values (current + previous), so *this particular transition* is
  the unit of work; a later Todo → Done → Todo bounce outside the window is
  new work.
- `time_bucket` — `occurred_at` floored to the trigger's
  `dedupe_window_seconds` (0 disables bucketing).

**Dedupe layer 2:** the key is inserted into `trigger_invocation` under a
partial unique index on `(trigger_id, idempotency_key) WHERE status =
'started'`. Losers of the race (semantically identical event, new delivery
id) record a `duplicate` row — visible in the UI — and stop.

**Dedupe layer 3:** the Temporal workflow id is derived from the same key
(`triggered-task-<hash>`), so even a crash between the workflow start and
the invocation commit cannot double-start: Temporal rejects the duplicate
id and the matcher treats `WorkflowAlreadyStartedError` as success.

Every outcome is audited (`trigger.invoked`, `trigger.duplicate_suppressed`).

### 5. TriggeredTaskWorkflow (agent task queue)

1. **prepare** — if an active (queued/running/paused) task already exists
   for the same `(external_source, external_id)`, reuse it (plan 26.8);
   otherwise create the task with external linkage, trigger metadata, and a
   system message ("Started by trigger … from linear ENG-142 (url)"), and
   link the invocation row to the task.
2. **child `AgentTaskWorkflow`** — the exact machinery used for manually
   assigned tasks (same signals, approvals, sandbox tooling, timeline).
3. **sync** (optional) — when the trigger's `action_config.comment_back` is
   true, comment the outcome on the source issue.

Activity retry policies distinguish retryable infrastructure errors from
non-retryable ones (`task_not_found`, `agent_not_found`, `unsupported`).

## Comment-back authorization model

Sync-back does **not** run through an agent's grants: it executes the
connector's own tool implementation (`linear.comment.create`) directly with
a *system* actor context, authorized by trigger configuration — an admin
enabled `comment_back` when creating the trigger (trigger writes are
admin-only and audited). The tool call uses the trigger's connection and its
stored credentials; the result is audited as `trigger.synced_external` with
`actor_type=system`. Agents that should read or comment on Linear during
their runs still need explicit `linear.*` grants (deny-by-default, plan 12).

## Testing the slice

`services/fake_linear` (dev overlay) is a minimal Linear: GraphQL for the
connector's tools, plus admin endpoints that simulate a human — 
`/_admin/issues/{id}/transition` fires a properly signed webhook,
`/_admin/redeliver` replays a delivery byte-for-byte (same delivery id), and
`/_admin/refire` sends the same content under a new delivery id. The Phase 7
exit tests (`tests/integration/test_phase7_exit.py`) drive the full slice
and prove each dedupe layer independently.
