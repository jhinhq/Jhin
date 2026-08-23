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
    # Memory release (docs/architecture/memory.md).
    "memory.retrieval_failed": {"error_type": FieldKind.ERROR_TYPE},
    "memory.embedding_failed": {
        "error_type": FieldKind.ERROR_TYPE,
        "workspace_id": FieldKind.ID,
        "count": FieldKind.COUNT,
    },
    "memory.embedded": {"workspace_id": FieldKind.ID, "count": FieldKind.COUNT},
    "memory.maintenance_start": {"status": FieldKind.ENUM, "task_id": FieldKind.ID},
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
    "work_request.finalized": {
        "work_request_id": FieldKind.ID,
        "task_id": FieldKind.ID,
        "run_status": FieldKind.ENUM,
        "request_status": FieldKind.ENUM,
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
    "stdlib.message": {},
    "log.event_rejected": {},
}

ENVIRONMENTS = frozenset({"dev", "test", "staging", "production"})
CONNECTOR_TYPES = frozenset({"github", "linear", "vercel", "supabase", "cli"})
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
    "connector_type": frozenset({"github", "linear", "vercel", "supabase", "cli", "other"}),
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
    ("telemetry.export_failed", "error_code"): frozenset({"export_timeout", "export_failed"}),
    ("rootless_transport.failed", "error_code"): frozenset(
        {"configuration_error", "upstream_unavailable"}
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
