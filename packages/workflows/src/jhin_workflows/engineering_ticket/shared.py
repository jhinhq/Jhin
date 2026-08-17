"""Typed contracts for EngineeringTicketWorkflow (plan 8.4, 27).

Stdlib dataclasses only, activities referenced by name (implementations on
the agent worker), matching the other workflow packages.

The template is selected per trigger via ``trigger.workflow_definition``::

    {
      "template": "engineering_ticket",
      "implementer_agent_id": "<uuid>",   # optional; set = coordinator mode
      "qa_agent_id": "<uuid>",            # optional; else team lookup
      "manager_review": false,             # optional pre-QA manager pass
      "max_retest_cycles": 3               # fail→fix→retest bound (1..10)
    }

Plain TriggeredTaskWorkflow stays the default — this is a built-in template,
never forced (plan 8.4), and nothing engineering-specific leaks into core
models (plan 28): the template lives entirely in trigger config + this
workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jhin_workflows.triggered_task.shared import TriggeredTaskInput

ACTIVITY_RESOLVE_ENGINEERING_PLAN = "resolve_engineering_plan"
ACTIVITY_CREATE_ENGINEERING_CHILD_TASK = "create_engineering_child_task"
ACTIVITY_FINALIZE_ENGINEERING_TICKET = "finalize_engineering_ticket"

DEFAULT_MAX_RETEST_CYCLES = 3


@dataclass
class EngineeringTicketInput:
    base: TriggeredTaskInput
    # "" = the trigger target implements directly; set = coordinator mode:
    # the trigger target (e.g. the CTO) owns the ticket and implementation is
    # delegated to this agent as a child task (plan 27 lifecycle).
    implementer_agent_id: str = ""
    qa_agent_id: str = ""
    manager_review: bool = False
    max_retest_cycles: int = DEFAULT_MAX_RETEST_CYCLES


@dataclass
class EngineeringPlanInput:
    workspace_id: str
    task_id: str
    coordinator_agent_id: str  # the trigger's resolved target
    implementer_agent_id: str  # config value; "" = coordinator implements
    qa_agent_id: str  # config value; "" = team lookup
    manager_review: bool


@dataclass
class EngineeringPlan:
    """Resolved, validated roles (all ids verified active, same workspace)."""

    implementer_agent_id: str
    coordinator_mode: bool
    qa_agent_id: str = ""  # "" = no QA available; review loop is skipped
    manager_agent_id: str = ""  # "" = manager review disabled/unresolvable


@dataclass
class CreateEngineeringChildTaskInput:
    workspace_id: str
    parent_task_id: str
    target_agent_id: str
    delegated_by_agent_id: str
    kind: str  # "delegation" | "review_request"
    title: str
    instructions: str
    expected_output: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    cycle: int = 0  # retest cycle counter, recorded in task metadata


@dataclass
class CreatedEngineeringChildTask:
    child_task_id: str


@dataclass
class FinalizeEngineeringTicketInput:
    workspace_id: str
    task_id: str
    status: str  # completed | review_failed | implementation_failed
    verdict: str  # final QA verdict ("" when QA never ran)
    cycles_used: int
    detail: str = ""


@dataclass
class EngineeringTicketResult:
    task_id: str
    status: str  # completed | review_failed | implementation_failed | skipped_duplicate_task
    verdict: str = ""
    cycles_used: int = 0
    created_task: bool = True
    synced_external: bool = False
