"""Typed contracts for MemoryMaintenanceWorkflow (experience design, Memory).

Dependency-light (stdlib dataclasses only), mirroring the other workflow
packages: workflow definitions live here, activity implementations in the
agent worker (``jhin_agent_worker.memory_activities``).

``remember_enabled`` / ``requested_scope`` / ``actor_authority`` are copied
verbatim from the authenticated user/API turn; a model can never set them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MEMORY_MAINTENANCE_WORKFLOW = "MemoryMaintenanceWorkflow"
ACTIVITY_EXTRACT_MEMORY_CANDIDATES = "extract_memory_candidates"
ACTIVITY_APPLY_MEMORY_CANDIDATES = "apply_memory_candidates"

SOURCE_KIND_MESSAGE = "message"
SOURCE_KIND_TASK_OUTCOME = "task_outcome"
SOURCE_KINDS = (SOURCE_KIND_MESSAGE, SOURCE_KIND_TASK_OUTCOME)


@dataclass
class MemoryMaintenanceInput:
    workspace_id: str
    agent_id: str
    # "message" (source_id = message id) or "task_outcome" (source_id = task id).
    source_kind: str
    source_id: str
    # Extra idempotency discriminator (e.g. run id or turn counter). The
    # workflow id is deterministic over (source_kind, source_id, turn_marker).
    turn_marker: str = ""
    task_id: str = ""
    conversation_id: str = ""
    # Explicit human "remember this" (from the API turn), never from a model.
    remember_enabled: bool = False
    requested_scope: str = ""  # agent | team | workspace; only with remember_enabled
    actor_user_id: str = ""
    # Widest scope the human may activate (from workspace RBAC), validated by
    # the API before the workflow starts.
    actor_authority: str = "agent"


def memory_maintenance_workflow_id(params: MemoryMaintenanceInput) -> str:
    base = f"memory-maintenance-{params.source_kind}-{params.source_id}"
    return f"{base}-{params.turn_marker}" if params.turn_marker else base


@dataclass
class ExtractMemoryCandidatesInput:
    workspace_id: str
    agent_id: str
    source_kind: str
    source_id: str
    task_id: str = ""
    conversation_id: str = ""


@dataclass
class ExtractMemoryCandidatesResult:
    ok: bool
    # Serialized MemoryCandidate dicts (validated again before apply).
    candidates_json: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    model: str = ""
    source_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ApplyMemoryCandidatesInput:
    workspace_id: str
    agent_id: str
    source_kind: str
    source_id: str
    candidates_json: list[dict[str, Any]] = field(default_factory=list)
    task_id: str = ""
    conversation_id: str = ""
    remember_enabled: bool = False
    requested_scope: str = ""
    actor_user_id: str = ""
    actor_authority: str = "agent"
    idempotency_key: str = ""


@dataclass
class ApplyMemoryCandidatesResult:
    ok: bool
    error: str = ""
    created_ids: list[str] = field(default_factory=list)
    activated: int = 0
    proposed: int = 0
    contested: int = 0
    rejected: int = 0
    duplicates: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class MemoryMaintenanceResult:
    # applied | nothing_to_remember | extraction_failed | apply_failed
    status: str
    workflow_id: str = ""
    candidate_count: int = 0
    extraction_error: str = ""
    apply: ApplyMemoryCandidatesResult | None = None
