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
| `agent-finalization.json` | `AgentTaskWorkflow` | `finalize_run` scheduled; worker parked before sandbox cleanup |
| `triggered-sync.json` | `TriggeredTaskWorkflow` | `sync_external` scheduled; worker parked before connector dispatch |
| `engineering-sync.json` | `EngineeringTicketWorkflow` | ticket finalized; sync scheduled and worker parked before connector dispatch |

Expected legacy activity names are `resolve_snapshot`, `run_agent_step`,
`resolve_approval`, `finalize_run`, `prepare_triggered_task`, `sync_external`,
`resolve_engineering_plan`, and `finalize_engineering_ticket`. No file contains
the Phase 10 patch marker or Phase 10 activity command names.

The following machine-readable manifest is the committed evidence authority.
Tests bind every fixture's exact bytes, metadata, event count, and end state.

<!-- phase9-evidence:start -->
```json
{
  "fixtures": {
    "agent-finalization.json": {
      "closed": false,
      "event_count": 17,
      "last_event_type": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
      "sha256": "7bcc21592f180baef8b613a8ac5ca933cd6d58dd02f0832b4d0a953e1ff6e3df",
      "task_queue": "jhin-agent-queue",
      "workflow_type": "AgentTaskWorkflow"
    },
    "agent-parked-approval.json": {
      "closed": false,
      "event_count": 16,
      "last_event_type": "EVENT_TYPE_WORKFLOW_TASK_COMPLETED",
      "sha256": "a9d3f13625b334b0162203dd478d55460b133bb1bb1a831fe602cd637035f330",
      "task_queue": "jhin-agent-queue",
      "workflow_type": "AgentTaskWorkflow"
    },
    "agent-post-bind-pre-effect.json": {
      "closed": false,
      "event_count": 11,
      "last_event_type": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
      "sha256": "504d7638f6b62ec394d35a35dfb8f40b0befc50a7d83550b7ef3985b3709f5bf",
      "task_queue": "jhin-agent-queue",
      "workflow_type": "AgentTaskWorkflow"
    },
    "agent-tool-step.json": {
      "closed": true,
      "event_count": 29,
      "last_event_type": "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED",
      "sha256": "cf894df193bd7067561a7cea7167355760602ea02424cc52b22d0b07af88ca87",
      "task_queue": "jhin-agent-queue",
      "workflow_type": "AgentTaskWorkflow"
    },
    "engineering-sync.json": {
      "closed": false,
      "event_count": 32,
      "last_event_type": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
      "sha256": "91cc30694224884e40d9acac765c2310f7b4937ea36e7fe901985ce1df431636",
      "task_queue": "jhin-agent-queue",
      "workflow_type": "EngineeringTicketWorkflow"
    },
    "triggered-sync.json": {
      "closed": false,
      "event_count": 20,
      "last_event_type": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
      "sha256": "c38db45134cd828f67008e23936aa4b51dea6a8f83f058b444a7471f89b13416",
      "task_queue": "jhin-agent-queue",
      "workflow_type": "TriggeredTaskWorkflow"
    }
  },
  "source_ref": "6318781b57692bf39f37cd428d73de115d7458e2",
  "temporal_sdk_version": "1.31.0"
}
```
<!-- phase9-evidence:end -->
