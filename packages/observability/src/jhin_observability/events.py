"""Closed event and field registry for the JSON-v1 log contract."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path

from jhin_observability.errors import SafeErrorCode
from jhin_observability.redaction import MAX_TRACEBACK_FRAMES


class FieldKind(StrEnum):
    ID = "id"
    COUNT = "count"
    SECONDS = "seconds"
    BOOL = "bool"
    ENUM = "enum"
    ERROR_TYPE = "error_type"
    ERROR = "error"


CONTEXT_FIELD_RULES = {
    "request_id": FieldKind.ID,
    "correlation_id": FieldKind.ID,
    "workspace_id": FieldKind.ID,
    "task_id": FieldKind.ID,
    "run_id": FieldKind.ID,
    "trace_id": FieldKind.ID,
    "span_id": FieldKind.ID,
}
EVENT_FIELD_RULES: dict[str, dict[str, FieldKind]] = {
    "api.started": {},
    "api.stopped": {},
    "api.request_failed": {
        "error_code": FieldKind.ENUM,
        "error": FieldKind.ERROR,
    },
    "api.request_finished": {
        "http_method": FieldKind.ENUM,
        "http_route": FieldKind.ENUM,
        "http_status_class": FieldKind.ENUM,
    },
    "api_key.usage_not_recorded": {"error_code": FieldKind.ENUM},
    "secrets.master_key_unavailable": {"error_code": FieldKind.ENUM},
    "security.master_key_env_source": {},
    "temporal.connect_retry": {
        "error_type": FieldKind.ERROR_TYPE,
        "retry_in_seconds": FieldKind.SECONDS,
    },
    "temporal.connected": {"task_queue": FieldKind.ENUM},
    "resources.retry": {
        "error_type": FieldKind.ERROR_TYPE,
        "retry_in_seconds": FieldKind.SECONDS,
    },
    "resources.ready": {},
    "nats.connect_retry": {
        "error_type": FieldKind.ERROR_TYPE,
        "retry_in_seconds": FieldKind.SECONDS,
    },
    "nats.connected": {"stream": FieldKind.ENUM},
    "worker.started": {"task_queue": FieldKind.ENUM},
    "worker.stopping": {},
    "events.publish_failed": {
        "event_type": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
    },
    "concurrency.kick_failed": {"error_type": FieldKind.ERROR_TYPE},
    # Budget enforcement (plan 15.5): tracked month spend crossed a budget's
    # warning threshold when a run finished.
    "budget.warning": {"scope": FieldKind.ENUM, "percent_used": FieldKind.COUNT},
    # Memory release (docs/architecture/memory.md).
    "memory.retrieval_failed": {"error_type": FieldKind.ERROR_TYPE},
    "memory.embedding_failed": {
        "error_type": FieldKind.ERROR_TYPE,
        "workspace_id": FieldKind.ID,
        "count": FieldKind.COUNT,
    },
    "memory.embedded": {"workspace_id": FieldKind.ID, "count": FieldKind.COUNT},
    # Gray-zone dedup adjudication (docs/architecture/memory.md): failures
    # mean every pair counts as DIFFERENT (never merge on doubt).
    "memory.adjudicated": {"workspace_id": FieldKind.ID, "count": FieldKind.COUNT},
    "memory.adjudication_failed": {
        "error_type": FieldKind.ERROR_TYPE,
        "workspace_id": FieldKind.ID,
        "count": FieldKind.COUNT,
    },
    "memory.maintenance_start": {
        "status": FieldKind.ENUM,
        "task_id": FieldKind.ID,
        "message_id": FieldKind.ID,
    },
    "memory.maintenance_start_failed": {"error_type": FieldKind.ERROR_TYPE},
    "memory.maintained": {
        "workspace_id": FieldKind.ID,
        "agent_id": FieldKind.ID,
        "source_kind": FieldKind.ENUM,
        "activated": FieldKind.COUNT,
        "proposed": FieldKind.COUNT,
        "rejected": FieldKind.COUNT,
    },
    # Coordination release (docs/architecture/coordination.md).
    "coordination.context_failed": {"error_type": FieldKind.ERROR_TYPE},
    # Skills release (docs/architecture/skills.md): loading the prompt's
    # skills list is best-effort; a failure degrades to no skills block.
    "skills.context_failed": {"error_type": FieldKind.ERROR_TYPE},
    # Situational awareness (clock + interlocutor) is best-effort too; a
    # failure degrades to no time/interlocutor blocks rather than a dead run.
    "situation.context_failed": {"error_type": FieldKind.ERROR_TYPE},
    "work_request.finalized": {
        "work_request_id": FieldKind.ID,
        "task_id": FieldKind.ID,
        "run_status": FieldKind.ENUM,
        "request_status": FieldKind.ENUM,
    },
    # The requester gave up waiting inside its own run; the request itself is
    # still open and its answer still reaches the conversation later.
    # The run genuinely stopped (or resumed) between steps -- as opposed to a
    # pause having merely been asked for.
    "task.pause_observed": {
        "task_id": FieldKind.ID,
        "paused": FieldKind.BOOL,
    },
    # The person's answer (or the fact that nobody answered) reached the run
    # that parked on the question.
    "question.delivered": {
        "question_id": FieldKind.ID,
        "outcome": FieldKind.ENUM,
        "status": FieldKind.ENUM,
    },
    # Best-effort tidy-up of a question whose run ended before it was
    # answered; the row is closed so the card stops inviting an answer.
    "question.close_failed": {
        "run_id": FieldKind.ID,
        "error_type": FieldKind.ERROR_TYPE,
    },
    "work_request.unanswered": {
        "work_request_id": FieldKind.ID,
        "outcome": FieldKind.ENUM,
    },
    "periodic_review.window": {
        "policy_id": FieldKind.ID,
        "review_id": FieldKind.ID,
        "status": FieldKind.ENUM,
        "created": FieldKind.BOOL,
    },
    # Media release (docs/architecture/media.md).
    "avatar.generated": {
        "workspace_id": FieldKind.ID,
        "agent_id": FieldKind.ID,
        "generation_id": FieldKind.ID,
    },
    "model.client_close_failed": {"error_type": FieldKind.ERROR_TYPE},
    # A model tool call whose arguments were not one strict JSON object; it
    # is bound as a placeholder and denied as invalid_input (plan 21.4).
    "agent.step.invalid_tool_arguments": {
        "reason": FieldKind.ENUM,
        "detail": FieldKind.ERROR,
        "argument_chars": FieldKind.COUNT,
    },
    "sandbox.workspace_cleanup": {"deleted": FieldKind.BOOL},
    "sandbox.network_created": {"network_policy": FieldKind.ENUM},
    "sandbox.network_ensure_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox.job.finished": {
        "job_id": FieldKind.ID,
        "outcome": FieldKind.ENUM,
        "exit_code": FieldKind.COUNT,
        "network_policy": FieldKind.ENUM,
    },
    "sandbox.reaped_container": {"count": FieldKind.COUNT},
    "sandbox.reaped_workspace": {"count": FieldKind.COUNT},
    "sandbox.reap_containers_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox.reap_volumes_failed": {"error_type": FieldKind.ERROR_TYPE},
    "sandbox_runner.started": {
        "network_policy": FieldKind.ENUM,
        "token_configured": FieldKind.BOOL,
    },
    # The runner refused to start because its Docker authority did not match
    # the configured mode. uvicorn announces the dying process through the
    # stdlib logger, whose text this contract replaces with a constant, so the
    # runner has to name the refusal itself or the operator gets an exit code
    # and nothing else. The reason is a closed vocabulary of the runner's own
    # sentences (below) -- never a path, a GID, or anything it was handed.
    "sandbox_runner.docker_authority_refused": {"reason": FieldKind.ENUM},
    "trigger.task_deduped": {},
    "trigger.invoked": {"connector_type": FieldKind.ENUM, "outcome": FieldKind.ENUM},
    "trigger.duplicate_suppressed": {"connector_type": FieldKind.ENUM},
    "trigger.no_agent": {"connector_type": FieldKind.ENUM},
    "trigger.workflow_already_started": {"connector_type": FieldKind.ENUM},
    "webhook.accepted": {"connector_type": FieldKind.ENUM, "outcome": FieldKind.ENUM},
    "webhook.publish_or_commit_failed": {
        "connector_type": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
    },
    "webhook.rollback_failed": {"connector_type": FieldKind.ENUM},
    "jetstream.consumer_created": {"stream": FieldKind.ENUM, "consumer": FieldKind.ENUM},
    "jetstream.consumer_loop_started": {
        "stream": FieldKind.ENUM,
        "consumer": FieldKind.ENUM,
    },
    "jetstream.consumer_handler_failed": {
        "stream": FieldKind.ENUM,
        "consumer": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
        "error_code": FieldKind.ENUM,
        "error": FieldKind.ERROR,
    },
    "heartbeat.recorded": {},
    "health.heartbeat_write_failed": {},
    "ingress.invalid_envelope": {"error_code": FieldKind.ENUM},
    "ingress.unhandled": {
        "connector_type": FieldKind.ENUM,
        "event_type": FieldKind.ENUM,
    },
    "ingress.normalized": {
        "connector_type": FieldKind.ENUM,
        "event_type": FieldKind.ENUM,
        "produced": FieldKind.COUNT,
    },
    "event.invalid_envelope": {"error_code": FieldKind.ENUM},
    "event.duplicate_skipped": {"num_delivered": FieldKind.COUNT},
    "event.processed": {
        "event_type": FieldKind.ENUM,
        "num_delivered": FieldKind.COUNT,
    },
    "telemetry.queue_dropped": {"count": FieldKind.COUNT, "queue_capacity": FieldKind.COUNT},
    "telemetry.export_failed": {"error_code": FieldKind.ENUM},
    "telemetry.export_recovered": {},
    "telemetry.nats_lag_probe_failed": {
        "stream": FieldKind.ENUM,
        "consumer": FieldKind.ENUM,
        "error_type": FieldKind.ERROR_TYPE,
    },
    "telemetry.connector_health_probe_failed": {"error_type": FieldKind.ERROR_TYPE},
    "web.started": {},
    "web.stopping": {"signal": FieldKind.ENUM},
    "web.rewrite_configured": {"http_route": FieldKind.ENUM},
    "web.request_failed": {
        "http_method": FieldKind.ENUM,
        "http_route": FieldKind.ENUM,
        "error_code": FieldKind.ENUM,
    },
    "web.framework_output_suppressed": {
        "stream": FieldKind.ENUM,
        "count": FieldKind.COUNT,
    },
    "rootless_transport.ready": {},
    "rootless_transport.failed": {"error_code": FieldKind.ENUM},
    # OAuth. Every one of these is a refusal, and every one is deliberately
    # field-poor: an authorization server's own prose is attacker-influenced
    # text, so what is recorded is the machine-readable code and nothing the
    # provider wrote. The discovery and registration events are debug-level
    # because a rejected candidate document is the normal case while probing.
    "oauth.metadata_field_refused": {"field": FieldKind.ENUM},
    "oauth.metadata_fetch_failed": {},
    "oauth.metadata_candidate_skipped": {"status_code": FieldKind.COUNT},
    "oauth.authorization_server_refused": {},
    "oauth.authorization_server_document_rejected": {},
    "oauth.challenge_metadata_url_refused": {},
    "oauth.protected_resource_document_rejected": {},
    "oauth.protected_resource_scope_mismatch": {},
    "oauth.registration_client_uri_refused": {},
    "oauth.registration_retry_native": {},
    "oauth.registration_delete_failed": {},
    "oauth.token_request_refused": {
        "status_code": FieldKind.COUNT,
        "error_code": FieldKind.ENUM,
    },
    "oauth.revocation_failed": {},
    "oauth.device_verification_complete_refused": {},
    "oauth.code_exchange_failed": {"connector_type": FieldKind.ENUM},
    "oauth.connection_not_created": {"connector_type": FieldKind.ENUM},
    "oauth.github_app_conversion_failed": {},
    "oauth.refresher_not_started": {},
    "oauth.refresh_signal_failed": {"error_type": FieldKind.ERROR_TYPE},
    "oauth.refresh_start_failed": {"error_type": FieldKind.ERROR_TYPE},
    "oauth.refresh_sweep_needs_reauth": {
        "needs_reauth": FieldKind.COUNT,
        "refreshed": FieldKind.COUNT,
    },
    "oauth.refresh_on_use_failed": {},
    # A preload the API answered "loading" for, then lost in the background.
    "ollama.background_load_failed": {
        "error_type": FieldKind.ERROR_TYPE,
        "error": FieldKind.ERROR,
    },
    "stdlib.message": {},
    "log.event_rejected": {},
}

ENVIRONMENTS = frozenset({"dev", "test", "staging", "production"})
# "mcp" is a first-class connector type (jhin_connectors.mcp.manifest), and it
# is the *dominant* value on the OAuth failure events -- an MCP server is the
# only thing Jhin discovers an authorization server for. Omitting it would log
# those failures as "other" and erase the one field that says what broke.
CONNECTOR_TYPES = frozenset({"github", "linear", "vercel", "supabase", "cli", "mcp"})
EVENT_FAMILIES = frozenset({"connector", "task", "run", "tool", "approval"})
SANDBOX_OUTCOMES = frozenset(
    {
        "ok",
        "accepted",
        "started",
        "completed",
        "failed",
        "cancelled",
        "timeout",
        "duplicate",
    }
)


def normalize_environment(raw: object) -> str:
    value = getattr(raw, "value", raw)
    text = value.strip().lower() if isinstance(value, str) else ""
    aliases = {"development": "dev", "prod": "production"}
    normalized = aliases.get(text, text)
    return normalized if normalized in ENVIRONMENTS else "production"


def normalize_connector_type(raw: object) -> str:
    value = getattr(raw, "value", raw)
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in CONNECTOR_TYPES else "other"


def normalize_event_family(raw: object) -> str:
    value = getattr(raw, "value", raw)
    text = value.strip().lower() if isinstance(value, str) else ""
    family = text.split(".", 1)[0]
    return family if family in EVENT_FAMILIES else "other"


def normalize_sandbox_outcome(raw: object) -> str:
    value = getattr(raw, "value", raw)
    text = value.strip().lower() if isinstance(value, str) else ""
    aliases = {"running": "started"}
    normalized = aliases.get(text, text)
    return normalized if normalized in SANDBOX_OUTCOMES else "other"


FIELD_ENUM_VALUES: dict[str, frozenset[str]] = {
    "connector_type": frozenset({"github", "linear", "vercel", "supabase", "cli", "mcp", "other"}),
    "consumer": frozenset({"event-worker", "event-worker-ingress", "other"}),
    "error_code": frozenset(code.value for code in SafeErrorCode),
    "event_type": frozenset({"connector", "task", "run", "tool", "approval", "other"}),
    "http_method": frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"}),
    "http_route": frozenset({"/api/:path*", "other"}),
    "http_status_class": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "other"}),
    "network_policy": frozenset({"none", "internet", "other"}),
    "outcome": frozenset(
        {
            "ok",
            "accepted",
            "started",
            "completed",
            "failed",
            "cancelled",
            "timeout",
            "duplicate",
            "other",
        }
    ),
    "signal": frozenset({"SIGINT", "SIGTERM", "other"}),
    "stream": frozenset({"INGRESS", "EVENTS", "stdout", "stderr", "other"}),
    "task_queue": frozenset(
        {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue", "other"}
    ),
}
EVENT_FIELD_ENUM_VALUES: dict[tuple[str, str], frozenset[str]] = {
    ("budget.warning", "scope"): frozenset({"agent", "workspace"}),
    ("telemetry.export_failed", "error_code"): frozenset({"export_timeout", "export_failed"}),
    ("rootless_transport.failed", "error_code"): frozenset(
        {"configuration_error", "upstream_unavailable"}
    ),
    # The OAuth 2.0 error codes, not Jhin's SafeErrorCode taxonomy that the
    # global "error_code" vocabulary carries. Mirrors
    # jhin_oauth.errors.KNOWN_ERROR_CODES plus its UNKNOWN_ERROR_CODE
    # fallback, kept literal here so this registry stays a leaf package that
    # imports no other Jhin package.
    ("oauth.token_request_refused", "error_code"): frozenset(
        {
            "access_denied",
            "authorization_pending",
            "device_flow_disabled",
            "expired_token",
            "incorrect_client_credentials",
            "incorrect_device_code",
            "invalid_client",
            "invalid_grant",
            "invalid_request",
            "invalid_scope",
            "invalid_target",
            "server_error",
            "slow_down",
            "temporarily_unavailable",
            "unauthorized_client",
            "unsupported_grant_type",
            "unsupported_response_type",
            "unknown",
        }
    ),
    # Only the optional endpoints reach this event; a refused *required* URL
    # raises instead of logging.
    ("oauth.metadata_field_refused", "field"): frozenset(
        {"registration_endpoint", "revocation_endpoint", "device_authorization_endpoint"}
    ),
    # Every sentence jhin_sandbox_runner's authority check can raise, kept
    # literal here so this registry stays a leaf package that imports no other
    # Jhin package. The runner's own test holds this set equal to what those
    # modules spell, and an unlisted sentence is dropped rather than logged.
    ("sandbox_runner.docker_authority_refused", "reason"): frozenset(
        {
            "Docker socket group does not match SANDBOX_DOCKER_GID",
            "Docker socket is not readable and writable by the runner",
            "Docker socket must be owned by UID 0",
            "cannot inspect Docker socket",
            "configured Docker endpoint is not a Unix socket",
            "configured Docker endpoint must not be a symlink",
            "desktop Docker socket must be owned by GID 0 (Docker Desktop VM socket)",
            "desktop Docker socket path must be absolute",
            "desktop runner requires UID 10001",
            "desktop runner requires UID/GID 10001:10001",
            "desktop runner requires no SANDBOX_DOCKER_GID",
            "desktop runner requires one Unix socket only",
            "desktop runner requires the root group as its only supplemental group",
            "rootful Docker socket path must be absolute",
            "rootful runner requires UID/GID 10001:10001",
            "rootful runner requires a positive SANDBOX_DOCKER_GID",
            "rootful runner requires one Unix socket only",
            "rootless runner requires UID 10001",
            "rootless runner requires UID/GID 10001:10001",
            "rootless runner requires no socket GID",
            "rootless runner requires no socket mount",
            "rootless runner requires no supplemental groups",
            "rootless transport URL is not the private endpoint",
            "runner requires the exact Docker socket group only",
            "sandbox runner must not run as root",
            "unsupported Docker socket mode",
        }
    ),
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
BASE_FIELDS = frozenset(
    {"schema_version", "timestamp", "level", "service", "environment", "event", "logger"}
)


def normalize_log_field(event: str, key: str, value: object, kind: FieldKind) -> object | None:
    if kind is FieldKind.ID:
        return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None
    if kind is FieldKind.COUNT:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )
    if kind is FieldKind.SECONDS:
        return (
            value
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            else None
        )
    if kind is FieldKind.BOOL:
        return value if isinstance(value, bool) else None
    if kind is FieldKind.ERROR_TYPE:
        return value if isinstance(value, str) and _ERROR_TYPE_RE.fullmatch(value) else None
    if kind is FieldKind.ERROR:
        return filter_structured_error(value) if isinstance(value, Mapping) else None
    exact = EVENT_FIELD_ENUM_VALUES.get((event, key))
    if exact is not None:
        return value if isinstance(value, str) and value in exact else None
    allowed = FIELD_ENUM_VALUES.get(key, frozenset({"other"}))
    if isinstance(value, str) and value in allowed:
        return value
    return "other" if "other" in allowed else None


def filter_structured_error(value: Mapping[str, object]) -> dict[str, object]:
    error_type = normalize_log_field(
        "structured.error", "error_type", value.get("type"), FieldKind.ERROR_TYPE
    )
    error_code = normalize_log_field(
        "structured.error", "error_code", value.get("code"), FieldKind.ENUM
    )
    frames: list[dict[str, object]] = []
    raw_frames = value.get("traceback")
    if isinstance(raw_frames, Sequence) and not isinstance(raw_frames, (str, bytes)):
        for raw in raw_frames[:MAX_TRACEBACK_FRAMES]:
            if not isinstance(raw, Mapping):
                continue
            filename = Path(str(raw.get("file", "unknown"))).name[:128]
            function = str(raw.get("function", "unknown"))[:128]
            line = raw.get("line", 0)
            frames.append(
                {
                    "file": (
                        filename
                        if _ERROR_TYPE_RE.fullmatch(filename.replace("-", "_"))
                        else "unknown"
                    ),
                    "function": function if _ERROR_TYPE_RE.fullmatch(function) else "unknown",
                    "line": line if isinstance(line, int) and line >= 0 else 0,
                }
            )
    return {
        "type": error_type or "Error",
        "code": error_code or SafeErrorCode.INTERNAL_ERROR.value,
        "traceback": frames,
    }


def filter_log_event(event_dict: Mapping[str, object]) -> dict[str, object]:
    raw_event = event_dict.get("event")
    event = (
        raw_event
        if isinstance(raw_event, str) and raw_event in EVENT_FIELD_RULES
        else "log.event_rejected"
    )
    output = {key: event_dict[key] for key in BASE_FIELDS - {"event"} if key in event_dict}
    output["event"] = event
    rules = {**CONTEXT_FIELD_RULES, **EVENT_FIELD_RULES[event]}
    for key, kind in rules.items():
        if (
            key in event_dict
            and (value := normalize_log_field(event, key, event_dict[key], kind)) is not None
        ):
            output[key] = value
    return output
