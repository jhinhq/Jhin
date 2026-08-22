"""Typed contracts for WorkRequestTaskWorkflow (coordination release).

Dependency-light (stdlib dataclasses only) like the other workflow contracts:
the API starts this workflow when a human accepts a request, and
``AgentTaskWorkflow`` starts it as a child when the target agent accepts
through ``organization.respond_work_request``.
"""

from __future__ import annotations

from dataclasses import dataclass

ACTIVITY_FINALIZE_WORK_REQUEST = "finalize_work_request"


def work_request_workflow_id(work_request_id: str) -> str:
    return f"work-request-{work_request_id}"


@dataclass
class WorkRequestTaskInput:
    workspace_id: str
    work_request_id: str
    # The standalone task created on acceptance; runs under ``task-<id>``.
    task_id: str
    # The accepting (target) agent that owns the task.
    agent_id: str


@dataclass
class FinalizeWorkRequestInput:
    workspace_id: str
    work_request_id: str
    task_id: str
    run_status: str  # the task AgentTaskWorkflow's final status


@dataclass
class WorkRequestTaskResult:
    work_request_id: str
    task_id: str
    run_status: str
    request_status: str = ""
