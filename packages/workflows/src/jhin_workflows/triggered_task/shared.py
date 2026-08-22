"""Typed contracts between TriggeredTaskWorkflow and its activities (plan 8.1).

Same convention as ``agent_task.shared``: stdlib dataclasses only, activities
referenced by name, implementations on the agent worker (which holds the
database, master key, and connector executors).
"""

from __future__ import annotations

from dataclasses import dataclass

ACTIVITY_PREPARE_TRIGGERED_TASK = "prepare_triggered_task"
ACTIVITY_SYNC_EXTERNAL = "sync_external"
ACTIVITY_SYNC_EXTERNAL_TOOL = "sync_external_tool"
PHASE10_TRIGGER_SYNC_PATCH = "phase10-trigger-sync-tool-routing-v1"


@dataclass
class TriggeredTaskInput:
    """Everything the matcher resolved at start time.

    Carrying the resolved facts (title, agent, trigger name) keeps the
    workflow deterministic and the activities simple; the trigger row may
    change later without affecting an in-flight invocation.
    """

    workspace_id: str
    trigger_id: str
    trigger_name: str
    invocation_id: str
    connection_id: str  # "" when the trigger has no connection
    event_id: str
    event_type: str
    external_source: str  # connector type, e.g. "linear"
    external_id: str  # provider entity id, e.g. "ENG-142"
    title: str
    description: str
    external_url: str
    agent_id: str
    # From the trigger's action_config_json: post an outcome comment on the
    # source entity when the run finishes (plan 26.14).
    comment_back: bool = False


@dataclass
class PreparedTask:
    task_id: str
    # False when an active task for the same external entity already exists
    # (task-level dedupe, plan 26.8): the workflow then starts no run.
    created: bool


@dataclass
class SyncExternalInput:
    workspace_id: str
    connection_id: str
    external_source: str
    external_id: str
    task_id: str
    run_id: str  # completed child run, for timeline attribution
    agent_id: str
    run_status: str
    trigger_name: str


@dataclass
class SyncExternalResult:
    synced: bool
    detail: str = ""


@dataclass
class TriggeredTaskResult:
    task_id: str
    run_status: str  # child result, or "skipped_duplicate_task"
    created_task: bool
    synced_external: bool = False
