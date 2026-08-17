"""Typed contracts between AgentTaskWorkflow and the agent worker activities.

Deliberately dependency-light (stdlib dataclasses only) so the API can import
this module to start/signal workflows without pulling agent-runtime
dependencies. Activities are referenced by name: the workflow definition
lives here, the implementations live in the agent worker service.

The snapshot travels as its JSON serialization (``snapshot_json``): it is
resolved once, hashed, and never mutated mid-run (plan 7.1). It contains no
credentials — activities decrypt provider secrets at the moment of use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ACTIVITY_RESOLVE_SNAPSHOT = "resolve_snapshot"
ACTIVITY_RUN_AGENT_STEP = "run_agent_step"
ACTIVITY_FINALIZE_RUN = "finalize_run"


@dataclass
class AgentTaskInput:
    workspace_id: str
    task_id: str
    agent_id: str
    instruction: str = ""


@dataclass
class SnapshotResult:
    run_id: str
    snapshot_json: str
    snapshot_hash: str
    max_steps: int


@dataclass
class RunStepInput:
    workspace_id: str
    task_id: str
    run_id: str
    agent_id: str
    snapshot_json: str
    step_index: int
    instruction: str = ""
    user_instructions: list[str] = field(default_factory=list)


@dataclass
class StepResult:
    done: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_micros: int = 0


@dataclass
class FinalizeInput:
    workspace_id: str
    task_id: str
    run_id: str | None
    status: str  # RunStatus value: completed | failed | cancelled
    steps_used: int
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class AgentTaskResult:
    run_id: str | None
    status: str
    steps_used: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0


@dataclass
class AgentTaskStatus:
    """Query response: current status and why the workflow is waiting."""

    status: str
    waiting_reason: str | None
    steps_used: int
    pending_instructions: int
