# Delegation and Teams

## Scope

Phase 8 adds structured agent communication, authorized delegation, durable
child workflows, engineering/QA workflow templates, task lineage, and
workspace/agent concurrency admission. These features reuse the normal task,
run, message, tool-gateway, audit, and Temporal machinery rather than creating
a separate execution path for delegated work.

## Message contract

Agent-to-agent messages use a fixed vocabulary:

| Type | Meaning |
| --- | --- |
| `instruction` | Work or direction sent to an agent. |
| `question` | A request for information or a decision. |
| `status` | A progress or lifecycle update. |
| `result` | A completed delegation's structured outcome. |
| `delegation` | A task handoff to another agent. |
| `review_request` | A handoff that requires a review verdict. |
| `review_result` | The structured response to a review request. |
| `escalation` | A problem raised for attention outside the current execution path. |

Every structured message has `summary`, `artifacts`, `risks`, and
`recommended_next_action`. An artifact is a typed reference with an optional
identifier and URL reference. Message-specific fields add context such as task
and agent identifiers, blocking state, status, or review verdict without
changing the common envelope.

`organization.report_result` is the explicit completion boundary for an agent.
It accepts `completed`, `pass`, `fail`, or `blocked`, persists a `result` (or
`review_result` for a review task), and mirrors that record into task metadata.
Managers and parent workflows receive this standardized summary rather than the
child's raw transcript or tool output. A review passes only when the child
explicitly reports `pass`; missing or free-form verdicts fail closed.

## Delegation authorization

`organization.delegate_task` is a write-risk, approval-capable tool enforced by
the live tool gateway. The model's request is not authority: the gateway checks
the current `organization.delegate` capability grants and then runs the
delegation validator before creating a child task.

Delegation is deny-by-default. An allow grant's `targets` scope may permit
subordinates, members of the same team, or any active agent in the workspace;
when omitted it permits subordinates only. The optional `target_agent_id` scope
further pins the allowed target by identifier pattern. An applicable explicit
deny wins over allows.

The validator also applies structural rules that grants cannot override. The
target must exist, be active, and belong to the workspace. The target may not
already own a task in the active ancestor lineage, which prevents delegation
cycles. The new child must also fit within
`workspace.settings_json.delegation.max_task_depth` (default 5, configurable
from 1 through 20). These facts are loaded from current database state for the
tool call, so stale model context cannot bypass policy.

## Durable workflow flow

The authorized `organization.delegate_task` executor creates the child task,
links it to its parent, records the structured `delegation` or `review_request`
message, and returns a delegation request to `AgentTaskWorkflow`. The parent
workflow starts one `DelegatedTaskWorkflow`, which in turn runs the child's
ordinary `AgentTaskWorkflow` under the child's stable `task-<id>` workflow ID.

For a blocking delegation, the parent moves to `waiting_delegation` and parks
durably. When the child ends, a retrying summarize activity reads the child's
reported result, persists the standardized result on the parent, and returns
the summary. The parent resumes with that summary stitched into the original
tool call as its observation. A non-blocking parent continues immediately and
receives the eventual summary as a message.

If the child does not call `organization.report_result`, summarization falls
back to the child's final run status and latest visible agent text. Abnormal
child-workflow failure resolves as `failed` and still goes through
summarization. Cancelling or closing the parent does not destroy already
started delegated work; Temporal history, stable workflow IDs, child workflow
execution, and retrying activities preserve parked and in-flight state across
worker restarts.

## Engineering ticket template

`EngineeringTicketWorkflow` is an opt-in trigger template; the standard
triggered workflow remains the default. It supports two routing modes:

- In direct mode, the trigger's target is the implementer and runs the root
  task through `AgentTaskWorkflow`.
- In coordinator mode, trigger configuration supplies a distinct
  `implementer_agent_id`. The trigger target (for example, a CTO) owns the root
  ticket without running a model, and implementation is a delegated child.

After successful implementation, the workflow optionally asks the
implementer's manager to review and then asks the configured or resolved QA
agent for a review. Each review is a real `review_request` child executed by
`DelegatedTaskWorkflow`. A failure creates a new implementation fix child with
the review summary as context, then repeats the review sequence. The
failure/fix/retest loop is bounded by `max_retest_cycles` (clamped to 1 through
10, default 3); exhausted reviews finish as `review_failed`, while an
implementation or fix failure finishes as `implementation_failed`.

Finalization records the status, verdict, and cycle count on the root task. If
the trigger has comment-back enabled and the implementation produced a run,
the workflow makes a best-effort sync to the external source. External sync
failure does not rewrite the engineering result, and merge or other gated tool
operations still pass through their normal approval policy.

## Concurrency

Admission is checked before a run row is created. In one transaction, the
agent worker locks the workspace and assigned agent, counts active runs, and
applies the agent's `max_concurrent_runs` followed by the optional workspace
`concurrency.max_concurrent_runs` ceiling. Running, paused, approval-waiting,
and delegation-waiting runs continue to occupy their slots.

When no slot is available, the task remains queued with no run row. Its
metadata and `task.queued` event expose either `agent_concurrency` or
`workspace_concurrency`, and the initial queue transition is audited. When a
run finishes, the worker best-effort signals the oldest relevant queued
workflow through `slot_available`; every wakeup rechecks Postgres rather than
assuming admission. A durable timer also polls as a fallback, so a missed
signal only adds latency. The queued loop and parked active runs live in
Temporal history and resume correctly after worker restart.

## API and UI

- `GET /api/v1/workspaces/{workspace_id}/tasks/{task_id}/tree` walks to the
  lineage root and returns every descendant with its task, assigned-agent name,
  latest run status, children, and the requested focus task. The task page
  renders this as the delegation chain.
- The task messages API returns persisted structured JSON. The task
  conversation renders message-type badges plus summary, artifact links,
  risks, and recommended next action instead of exposing raw child transcripts.
- The task page renders distinct banners for `agent_concurrency` and
  `workspace_concurrency`, and a separate durable-wait banner while a parent is
  waiting on delegation.
- The agent Tools & Access view shows deny-by-default grants and allows the
  delegation relationship scope (`subordinates`, `team`, or `any`) to be
  selected. The API-level grant contract also supports explicit deny and
  `target_agent_id` pins.
- Agent settings expose `max_concurrent_runs`. The workspace update API accepts
  the optional workspace-wide concurrency ceiling and the delegation depth
  limit.
- The trigger editor offers the standard workflow or the engineering ticket
  template, with QA selection, optional manager review, and maximum retest
  cycles. Trigger configuration also supports `implementer_agent_id` for
  coordinator mode.

## Verification

The focused Phase 8 integration suite defines five repository scenarios:

1. A blocking SWE-to-QA review parks the parent, returns an explicit passing
   summary, and resumes the parent with task lineage and structured messages.
2. The engineering template handles a failed QA review through a new fix task
   and a passing retest within the configured bound.
3. Coordinator mode keeps the CTO-owned root task model-free while routing
   implementation to SWE and review to QA, including artifact propagation.
4. Delegation is denied without a grant, outside the permitted relationship,
   and beyond the configured task depth.
5. A second task queues behind an agent-held approval slot, remains durable
   across an agent-worker restart, and starts only after the slot is released.

**Repository-wide verification pending Task 3.**

This document does not claim the focused scenarios or repository-wide gates
have been rerun for closure; Task 3 records fresh commands and counts.

## Deferred scope

Delegation budget enforcement remains deferred until the later budget engine.
Concurrency limits for connector calls, model providers, and sandboxes also
belong to later phases; Phase 8 admission covers agent runs and the optional
workspace-wide run ceiling only.
