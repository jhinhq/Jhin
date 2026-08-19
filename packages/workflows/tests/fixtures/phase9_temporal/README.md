# Frozen Phase 9 Temporal histories

These immutable SDK JSON histories were captured from `6318781b57692bf39f37cd428d73de115d7458e2` before any
Phase 10 workflow patch was added. The generator used Temporal Python SDK
1.31.0 and supplied each original workflow ID explicitly when reconstructing
`WorkflowHistory`; the JSON files intentionally contain only the SDK `events`
document.

| Fixture | Workflow type | Database state at capture |
| --- | --- | --- |
| `agent-tool-step.json` | `AgentTaskWorkflow` | normal tool step and finalization completed |
| `agent-post-bind-pre-effect.json` | `AgentTaskWorkflow` | one lossless manifest; no reasoning, `ToolCall`, or effect |
| `agent-parked-approval.json` | `AgentTaskWorkflow` | pending `Approval` and pending-approval `ToolCall`; workflow open |
| `agent-finalization.json` | `AgentTaskWorkflow` | `finalize_run` started and parked before sandbox cleanup |
| `triggered-sync.json` | `TriggeredTaskWorkflow` | `sync_external` started and parked before connector dispatch |
| `engineering-sync.json` | `EngineeringTicketWorkflow` | ticket finalized; `sync_external` parked before connector dispatch |

Expected legacy activity names are `resolve_snapshot`, `run_agent_step`,
`resolve_approval`, `finalize_run`, `prepare_triggered_task`, `sync_external`,
`resolve_engineering_plan`, and `finalize_engineering_ticket`. No file contains
the Phase 10 patch marker or Phase 10 activity command names.
