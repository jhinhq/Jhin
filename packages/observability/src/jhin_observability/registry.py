"""Single immutable registry for safe spans, attributes, and metric names."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, get_args

TEMPORAL_ACTIVITY_NAMES = (
    "reason_agent_step",
    "commit_agent_step",
    "commit_approval_projection",
    "resolve_advertised_tools",
    "execute_bound_tool",
    "resolve_bound_tool_approval",
    "sync_external_tool",
    "cleanup_run_workspace",
    "resolve_snapshot",
    "run_agent_step",
    "resolve_approval",
    "finalize_run",
    "finalize_run_projection",
    "summarize_delegation",
    "deliver_delegation_result",
    "prepare_triggered_task",
    "sync_external",
    "resolve_engineering_plan",
    "create_engineering_child_task",
    "finalize_engineering_ticket",
    "record_beat",
)

SpanName = Literal[
    "http.server.request",
    "db.operation",
    "nats.publish",
    "nats.consume",
    "trigger.dispatch",
    "temporal.start_workflow",
    "temporal.signal_workflow",
    "temporal.client.other",
    "temporal.activity.other",
    "temporal.activity.reason_agent_step",
    "temporal.activity.commit_agent_step",
    "temporal.activity.commit_approval_projection",
    "temporal.activity.resolve_advertised_tools",
    "temporal.activity.execute_bound_tool",
    "temporal.activity.resolve_bound_tool_approval",
    "temporal.activity.sync_external_tool",
    "temporal.activity.cleanup_run_workspace",
    "temporal.activity.resolve_snapshot",
    "temporal.activity.run_agent_step",
    "temporal.activity.resolve_approval",
    "temporal.activity.finalize_run",
    "temporal.activity.finalize_run_projection",
    "temporal.activity.summarize_delegation",
    "temporal.activity.deliver_delegation_result",
    "temporal.activity.prepare_triggered_task",
    "temporal.activity.sync_external",
    "temporal.activity.resolve_engineering_plan",
    "temporal.activity.create_engineering_child_task",
    "temporal.activity.finalize_engineering_ticket",
    "temporal.activity.record_beat",
    "model.request",
    "agent.reason_step",
    "tool.gateway.execute",
    "tool.approval.resolve",
    "connector.http",
    "connector.database",
    "sandbox.client",
    "sandbox.server",
    "sandbox.job.lifecycle",
]
SPAN_NAMES: frozenset[str] = frozenset(get_args(SpanName))
AttributeValue = str | bool | int | float

MetricName = Literal[
    "agent_runs_total",
    "agent_run_duration_seconds",
    "agent_run_failures_total",
    "model_requests_total",
    "model_tokens_total",
    "model_cost_estimate",
    "tool_calls_total",
    "tool_call_failures_total",
    "trigger_invocations_total",
    "trigger_failures_total",
    "sandbox_jobs_total",
    "sandbox_job_duration_seconds",
    "nats_consumer_lag",
    "temporal_activity_failures",
    "connector_health",
    "connector_connections",
]

DB_TABLE_VALUES = frozenset(
    {
        "agent",
        "agent_capability_grant",
        "agent_relationship",
        "agent_run",
        "agent_team_membership",
        "approval",
        "audit_event",
        "connection",
        "message",
        "model_profile",
        "model_provider",
        "run_event",
        "sandbox_job",
        "secret",
        "service_instance_heartbeat",
        "task",
        "team",
        "tool_call",
        "trigger",
        "trigger_invocation",
        "user",
        "user_session",
        "webhook_delivery",
        "workspace",
        "workspace_membership",
        "other",
    }
)
TEMPORAL_WORKFLOW_TYPE_VALUES = frozenset(
    {
        "AdvertisedToolsCompatibilityWorkflow",
        "AgentTaskWorkflow",
        "ApprovalCompatibilityWorkflow",
        "CleanupCompatibilityWorkflow",
        "DelegatedTaskWorkflow",
        "EngineeringTicketWorkflow",
        "HeartbeatWorkflow",
        "SyncExternalCompatibilityWorkflow",
        "ToolStepCompatibilityWorkflow",
        "TriggeredTaskWorkflow",
        "other",
    }
)
TEMPORAL_ACTIVITY_TYPE_VALUES = frozenset((*TEMPORAL_ACTIVITY_NAMES, "other"))

_SPAN_ATTRIBUTE_VALUE_DATA: dict[str, frozenset[str]] = {
    "http.request.method": frozenset(
        {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "other"}
    ),
    "http.route": frozenset({"/api/:path*", "other"}),
    "http.response.status_class": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "other"}),
    "db.system": frozenset({"postgresql", "other"}),
    "db.operation": frozenset(
        {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "ALTER", "DROP", "other"}
    ),
    "db.table": DB_TABLE_VALUES,
    "messaging.system": frozenset({"nats", "other"}),
    "jhin.stream": frozenset({"INGRESS", "EVENTS", "DLQ", "other"}),
    "jhin.consumer": frozenset({"event-worker", "event-worker-ingress", "other"}),
    "jhin.subject_family": frozenset(
        {
            "ingress",
            "task",
            "agent",
            "tool",
            "approval",
            "conversation",
            "connector",
            "trigger",
            "workflow",
            "system",
            "dlq",
            "other",
        }
    ),
    "jhin.provider_type": frozenset(
        {"openai", "anthropic", "openrouter", "ollama", "openai_compatible", "other"}
    ),
    "jhin.connector_type": frozenset({"github", "linear", "vercel", "supabase", "cli", "other"}),
    "jhin.operation": frozenset(
        {
            "generate",
            "stream",
            "verify",
            "embed",
            "list_models",
            "account_status",
            "issue_comment_create",
            "execute_read",
            "execute_write",
            "submit",
            "cancel",
            "status",
            "cleanup",
            "other",
        }
    ),
    "jhin.outcome": frozenset(
        {
            "ok",
            "accepted",
            "started",
            "completed",
            "failed",
            "cancelled",
            "timeout",
            "denied",
            "rejected",
            "duplicate",
            "execution_unknown",
            "healthy",
            "unhealthy",
            "other",
        }
    ),
    "jhin.tool_family": frozenset(
        {"system", "organization", "github", "linear", "vercel", "supabase", "cli", "other"}
    ),
    "jhin.risk": frozenset({"read", "write", "elevated", "destructive", "other"}),
    "jhin.network_policy": frozenset({"none", "internet", "other"}),
    "temporal.task_queue": frozenset(
        {"jhin-workflow-queue", "jhin-agent-queue", "jhin-tool-queue", "other"}
    ),
    "temporal.workflow_type": TEMPORAL_WORKFLOW_TYPE_VALUES,
    "temporal.activity_type": TEMPORAL_ACTIVITY_TYPE_VALUES,
}
SPAN_ATTRIBUTE_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(_SPAN_ATTRIBUTE_VALUE_DATA)

__all__ = [
    "DB_TABLE_VALUES",
    "SPAN_ATTRIBUTE_VALUES",
    "SPAN_NAMES",
    "TEMPORAL_ACTIVITY_NAMES",
    "TEMPORAL_ACTIVITY_TYPE_VALUES",
    "TEMPORAL_WORKFLOW_TYPE_VALUES",
    "AttributeValue",
    "MetricName",
    "SpanName",
]
