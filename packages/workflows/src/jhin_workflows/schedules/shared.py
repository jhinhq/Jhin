from dataclasses import dataclass


@dataclass
class ScheduleTick:
    workspace_id: str
    schedule_id: str


@dataclass
class ScheduleClaim:
    task_id: str = ""
    occurrence_id: str = ""
    agent_id: str = ""
    brief: str = ""
    deleted: bool = False
    wait_seconds: float = 30


@dataclass
class ScheduleFinish:
    workspace_id: str
    occurrence_id: str
    status: str


def schedule_workflow_id(workspace_id: str, schedule_id: str) -> str:
    return f"schedule-{workspace_id}-{schedule_id}"
