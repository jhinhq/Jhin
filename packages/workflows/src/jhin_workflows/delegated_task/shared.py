"""Typed contracts for DelegatedTaskWorkflow (plan 8.3).

Dependency-light (stdlib dataclasses only), mirroring
``jhin_workflows.agent_task.shared``: workflow definitions live in the
workflows package, activity implementations in the agent worker.

The delegation summary is the plan-7.6 boundary object: the parent agent
receives this standardized record as its observation — never the child's
full transcript (that stays retrievable via API/UI on demand).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACTIVITY_SUMMARIZE_DELEGATION = "summarize_delegation"
ACTIVITY_DELIVER_DELEGATION_RESULT = "deliver_delegation_result"


@dataclass
class DelegatedTaskInput:
    workspace_id: str
    parent_task_id: str
    child_task_id: str
    # The agent assigned to run the child task.
    agent_id: str
    delegating_agent_id: str
    # "" when no parent run exists (e.g. template-orchestrated delegation).
    parent_run_id: str = ""
    kind: str = "delegation"  # "delegation" | "review_request"
    blocking: bool = True


@dataclass
class DelegationSummary:
    """The standardized child-result summary returned to the parent (plan 7.6)."""

    task_id: str
    # Reported status if the child called organization.report_result
    # (completed|pass|fail|blocked), else the child run's final status.
    status: str
    summary: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    recommended_next_action: str = ""
    # For review_request delegations: "pass" | "fail" (derived from the
    # reported status / run outcome in the summarize activity, never from
    # free-form model text).
    verdict: str = ""
    reported: bool = False  # whether the child explicitly reported a result


@dataclass
class SummarizeDelegationInput:
    workspace_id: str
    parent_task_id: str
    child_task_id: str
    agent_id: str
    delegating_agent_id: str
    kind: str
    run_status: str  # the child AgentTaskWorkflow's final status
    parent_run_id: str = ""
    # Blocking delegations get the summary stitched into the parent
    # transcript as a tool observation; the visible message is then marked so
    # history rebuilding does not feed it to the model twice.
    blocking: bool = True


@dataclass
class DelegatedTaskResult:
    child_task_id: str
    run_status: str
    summary: DelegationSummary
    run_id: str = ""  # the child's agent run, when one was created


@dataclass
class DeliverDelegationResultInput:
    """Resume a delegating run: stitch the child's summary into the parent
    transcript as the delegate_task call's observation (plan 7.6 — summary,
    not transcript)."""

    workspace_id: str
    task_id: str  # the parent (delegating) task
    run_id: str
    agent_id: str
    child_task_id: str
    provider_call_id: str
    kind: str
    summary: DelegationSummary
