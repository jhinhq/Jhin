"""Canonical enumerations shared across packages (plan section 6).

Stored as plain strings in Postgres (no native enum types) so adding a value
never requires a migration; validity is enforced at the API boundary.
"""

from enum import StrEnum


class WorkspaceRole(StrEnum):
    """Workspace RBAC roles, weakest to strongest (plan 20.2)."""

    VIEWER = "viewer"
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


_ROLE_ORDER: dict[WorkspaceRole, int] = {
    WorkspaceRole.VIEWER: 0,
    WorkspaceRole.MEMBER: 1,
    WorkspaceRole.ADMIN: 2,
    WorkspaceRole.OWNER: 3,
}


def role_satisfies(actual: WorkspaceRole, required: WorkspaceRole) -> bool:
    """True when ``actual`` grants at least the privileges of ``required``."""
    return _ROLE_ORDER[actual] >= _ROLE_ORDER[required]


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class AutonomyLevel(StrEnum):
    """How much an agent may do without human sign-off.

    Enforcement arrives with the policy engine (Phase 4); Phase 2 only stores
    the configuration.
    """

    MANUAL = "manual"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class ActorType(StrEnum):
    """Who performed an audited action (plan 6.17)."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class SecretType(StrEnum):
    """What kind of credential an encrypted secret holds (plan 6.10)."""

    API_KEY = "api_key"
    TOKEN = "token"
    PASSWORD = "password"
    # JSON object of connector credential fields (plan 6.9), e.g. a GitHub
    # PAT or app id + private key + installation id.
    CONNECTION_CREDENTIALS = "connection_credentials"
    # Per-connection webhook signing secret (plan 19); shown once at creation.
    WEBHOOK_SECRET = "webhook_secret"
    OTHER = "other"


class ConnectionStatus(StrEnum):
    """Health/lifecycle of an authenticated integration instance (plan 6.9)."""

    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"


class ModelProviderType(StrEnum):
    """Supported model provider adapters (plan 6.7, 15.1)."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class TaskState(StrEnum):
    """Lifecycle of user-visible work (plan 6.12)."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class RunStatus(StrEnum):
    """Lifecycle of a single agent run (plan 6.13)."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    # Parked on a blocking delegation: a child task/run is doing the work
    # and the parent resumes when its summary arrives (plan 7.5, 8.3).
    WAITING_DELEGATION = "waiting_delegation"
    # Parked on a pending pre-action work review (coordination release): the
    # tool call is persisted as ``pending_review`` and the workflow resumes
    # on the ``review_decision`` signal, exactly like an approval wait.
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


RUN_TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})

# Statuses that occupy a concurrency slot (plan 30): anything between
# admission and finalize, including parked waits — a parked run still owns
# its working state (sandbox volume, transcript) and must not be doubled.
RUN_ACTIVE_STATUSES = frozenset(
    {
        RunStatus.PENDING,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_DELEGATION,
        RunStatus.WAITING_REVIEW,
    }
)


class SenderType(StrEnum):
    """Who authored a message (plan 6.14)."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class RecipientType(StrEnum):
    USER = "user"
    AGENT = "agent"
    TEAM = "team"
    TASK = "task"


class MessageType(StrEnum):
    """What a persisted message means (plan 6.14, 29).

    The first group is conversational/runtime plumbing; the second group is
    the structured agent-to-agent vocabulary from plan 29 — those messages
    carry a structured ``content_json`` (see :mod:`jhin_domain.messages`)
    that the UI renders conversationally while the backend keeps structure.
    """

    TEXT = "text"
    NOTE = "note"
    ERROR = "error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"

    INSTRUCTION = "instruction"
    QUESTION = "question"
    STATUS = "status"
    RESULT = "result"
    DELEGATION = "delegation"
    REVIEW_REQUEST = "review_request"
    REVIEW_RESULT = "review_result"
    ESCALATION = "escalation"


# The structured agent-to-agent subset (plan 29).
AGENT_MESSAGE_TYPES = frozenset(
    {
        MessageType.INSTRUCTION,
        MessageType.QUESTION,
        MessageType.STATUS,
        MessageType.RESULT,
        MessageType.DELEGATION,
        MessageType.REVIEW_REQUEST,
        MessageType.REVIEW_RESULT,
        MessageType.ESCALATION,
    }
)


class MessageVisibility(StrEnum):
    """Whether a message is shown in the product UI or internal-only."""

    VISIBLE = "visible"
    INTERNAL = "internal"


class ConversationStatus(StrEnum):
    """Lifecycle of a human <-> agent conversation thread."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class ActivityKind(StrEnum):
    """Company activity feed card kinds (human-readable projections)."""

    STARTED = "started"
    ASKED_AGENT = "asked_agent"
    REPORTED = "reported"
    ESCALATED = "escalated"
    STATUS_UPDATE = "status_update"
    NEEDS_REVIEW = "needs_review"
    FINISHED = "finished"
    FAILED = "failed"
    PAUSED = "paused"
    STOPPED = "stopped"
    QUEUED = "queued"


ACTIVITY_LABELS: dict[ActivityKind, str] = {
    ActivityKind.STARTED: "Started working",
    ActivityKind.ASKED_AGENT: "Asked another agent",
    ActivityKind.REPORTED: "Reported back",
    ActivityKind.ESCALATED: "Needs help",
    ActivityKind.STATUS_UPDATE: "Shared an update",
    ActivityKind.NEEDS_REVIEW: "Needs your review",
    ActivityKind.FINISHED: "Finished",
    ActivityKind.FAILED: "Ran into a problem",
    ActivityKind.PAUSED: "Paused",
    ActivityKind.STOPPED: "Stopped",
    ActivityKind.QUEUED: "Waiting for a free slot",
}


class ApprovalStatus(StrEnum):
    """Lifecycle of a human approval request (plan 6.16)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


APPROVAL_DECIDED_STATUSES = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.CANCELLED}
)


class SandboxJobStatus(StrEnum):
    """Lifecycle of one ephemeral sandbox job (plan 14).

    ``completed`` means the job container ran to normal completion (any exit
    code — the exit code is stored separately); ``failed`` is an
    infrastructure error before/around execution (image missing, Docker
    error); ``timeout`` and ``cancelled`` are forced terminations.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


SANDBOX_JOB_TERMINAL_STATUSES = frozenset(
    {
        SandboxJobStatus.COMPLETED,
        SandboxJobStatus.FAILED,
        SandboxJobStatus.TIMEOUT,
        SandboxJobStatus.CANCELLED,
    }
)


class TriggerType(StrEnum):
    """What starts a trigger evaluation (plan 6.11)."""

    CONNECTOR_EVENT = "connector_event"
    SCHEDULE = "schedule"  # stored now; the scheduler arrives in a later phase
    MANUAL = "manual"


class TriggerActionType(StrEnum):
    """What a matched trigger does (plan 10.3). Only task starts for now."""

    START_AGENT_TASK = "start_agent_task"


class TriggerInvocationStatus(StrEnum):
    """Outcome of one trigger match (plan 9.4).

    ``started`` — the TriggeredTaskWorkflow was started; ``duplicate`` — the
    idempotency key (or Temporal's workflow-id policy) suppressed a repeat;
    ``failed`` — the workflow could not be started (surfaced in the UI).
    """

    STARTED = "started"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class ToolCallStatus(StrEnum):
    """Persisted outcome of one gateway-mediated tool call (plan 6.15).

    ``denied`` is a policy decision; ``rejected`` is a human decision on an
    approval-gated call; ``failed`` is an execution error after authorization.
    """

    PENDING_APPROVAL = "pending_approval"
    # Parked on a pending work review (``tool_call.review_id``); resumes
    # through the normal approval/claim/effect path once decided.
    PENDING_REVIEW = "pending_review"
    EXECUTING = "executing"
    EXECUTION_UNKNOWN = "execution_unknown"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    REJECTED = "rejected"


class AvatarKind(StrEnum):
    """Which avatar an agent currently presents (experience design: media)."""

    INITIALS = "initials"
    UPLOAD = "upload"
    GENERATED = "generated"


class MediaAssetStatus(StrEnum):
    """Lifecycle of a stored media asset.

    ``pending`` rows exist only inside the transaction that validates their
    variants; ``active`` assets are servable; ``rejected`` records a failed
    validation; ``retired`` is a replaced avatar kept for audit until pruned.
    """

    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    RETIRED = "retired"


class AvatarGenerationStatus(StrEnum):
    """Lifecycle of one asynchronous avatar generation request."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


AVATAR_GENERATION_TERMINAL_STATUSES = frozenset(
    {AvatarGenerationStatus.SUCCEEDED, AvatarGenerationStatus.FAILED}
)


class MemoryScope(StrEnum):
    """Who a curated memory record belongs to (experience design, Memory)."""

    AGENT = "agent"
    TEAM = "team"
    WORKSPACE = "workspace"


# Visibility ordering used by non-amplification checks: a memory may never be
# more visible than the source it was derived from.
MEMORY_SCOPE_ORDER: dict[MemoryScope, int] = {
    MemoryScope.AGENT: 0,
    MemoryScope.TEAM: 1,
    MemoryScope.WORKSPACE: 2,
}


class MemoryKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    PROCEDURE = "procedure"
    CONTEXT = "context"
    OTHER = "other"


class MemoryStatus(StrEnum):
    """Lifecycle of one memory record version."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    CONTESTED = "contested"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    FORGOTTEN = "forgotten"


# Statuses retrieval may ever inject into a prompt.
MEMORY_RETRIEVABLE_STATUSES = frozenset({MemoryStatus.ACTIVE, MemoryStatus.CONTESTED})


class MemorySensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    REDACTED = "redacted"


# --- Coordination and oversight (work requests, review policies, reviews) ---


class WorkRequestStatus(StrEnum):
    """Lifecycle of a peer/cross-team work request (distinct from delegation)."""

    PENDING = "pending"
    CLARIFICATION_REQUESTED = "clarification_requested"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    COMPLETED = "completed"
    FAILED = "failed"


# Requests still awaiting the target's decision.
WORK_REQUEST_OPEN_STATUSES = frozenset(
    {WorkRequestStatus.PENDING, WorkRequestStatus.CLARIFICATION_REQUESTED}
)
# Requests that occupy the target (awaiting decision or running as a task).
WORK_REQUEST_ACTIVE_STATUSES = frozenset(
    {
        WorkRequestStatus.PENDING,
        WorkRequestStatus.CLARIFICATION_REQUESTED,
        WorkRequestStatus.ACCEPTED,
    }
)


class ReviewScopeKind(StrEnum):
    """What a review policy applies to."""

    WORKSPACE = "workspace"
    TEAM = "team"
    AGENT = "agent"
    TASK_TYPE = "task_type"


class ReviewMode(StrEnum):
    """When a review policy triggers."""

    PRE_ACTION = "pre_action"
    BEFORE_CLOSE = "before_close"
    POST_ACTION = "post_action"
    PERIODIC = "periodic"


# Modes whose unresolved review blocks the source run.
REVIEW_BLOCKING_MODES = frozenset({ReviewMode.PRE_ACTION, ReviewMode.BEFORE_CLOSE})


class ReviewerType(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class WorkReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    SKIPPED = "skipped"
    ESCALATED = "escalated"


class ReviewVerdict(StrEnum):
    """The decision a reviewer submits; mapped onto WorkReviewStatus."""

    APPROVE = "approve"
    CHANGES_REQUESTED = "changes_requested"
    ESCALATE = "escalate"


WORK_REVIEW_DECIDED_STATUSES = frozenset(
    {
        WorkReviewStatus.APPROVED,
        WorkReviewStatus.CHANGES_REQUESTED,
        WorkReviewStatus.SKIPPED,
        WorkReviewStatus.ESCALATED,
    }
)
