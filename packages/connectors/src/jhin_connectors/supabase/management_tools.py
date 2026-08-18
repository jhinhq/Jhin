"""Scoped Supabase Management API tool definitions and executors."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel

from jhin_connectors.execution import resolve_connection
from jhin_connectors.supabase.management_client import (
    SupabaseManagementClient,
    SupabaseManagementError,
)
from jhin_connectors.supabase.manifest import DEFAULT_BASE_URL
from jhin_connectors.supabase.schemas import (
    FunctionDeleteInput,
    FunctionDeleteOutput,
    FunctionDeployInput,
    FunctionInfo,
    FunctionListInput,
    FunctionListOutput,
    LogRecord,
    LogsReadInput,
    LogsReadOutput,
    ProjectReadInput,
    ProjectReadOutput,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor
from jhin_tools.sanitize import sanitize_payload

MAX_LOG_OUTPUT_BYTES = 28_000
MAX_FUNCTION_LIST_OUTPUT_BYTES = 28_000
_PROJECT_REF_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _provider_shape(message: str) -> SupabaseManagementError:
    return SupabaseManagementError(message, code="invalid_provider_response")


def _validate_provider_string(
    value: str,
    *,
    field: str,
    allowed_controls: frozenset[str] = frozenset(),
) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _provider_shape(f"Supabase returned an invalid {field}") from None
    if any(
        unicodedata.category(character).startswith("C") and character not in allowed_controls
        for character in value
    ):
        raise _provider_shape(f"Supabase returned an invalid {field}")
    return value


def _identifier(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _provider_shape(f"Supabase returned an invalid {field}")
    return _validate_provider_string(value, field=field)


def _display_string(value: Any, *, field: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _provider_shape(f"Supabase returned an invalid {field}")
    return _validate_provider_string(value, field=field)[:maximum]


def _display_message(value: Any, *, field: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _provider_shape(f"Supabase returned an invalid {field}")
    return _validate_provider_string(
        value,
        field=field,
        allowed_controls=frozenset({"\t", "\n"}),
    )[:maximum]


def _gateway_json_bytes(output: BaseModel) -> int:
    return len(
        json.dumps(
            output.model_dump(mode="json"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


def _gateway_document_retained(output: BaseModel, *, maximum: int) -> bool:
    if _gateway_json_bytes(output) > maximum:
        return False
    sanitized = sanitize_payload(
        output.model_dump(mode="json"),
        max_document_bytes=maximum,
    )
    return "original_size_bytes" not in sanitized


def _provider_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise _provider_shape(f"Supabase returned an invalid {field}")
    return value


def project_read_output(project: dict[str, Any], *, project_ref: str) -> ProjectReadOutput:
    returned_ref = _identifier(project.get("ref"), field="project reference", maximum=63)
    if returned_ref != project_ref:
        raise SupabaseManagementError(
            "Supabase project does not match the configured project",
            code="project_scope_mismatch",
        )
    return ProjectReadOutput(
        project_id=_identifier(project.get("id"), field="project id", maximum=200),
        project_ref=returned_ref,
        organization_id=_display_string(
            project.get("organization_id"), field="organization id", maximum=200
        ),
        organization_slug=_display_string(
            project.get("organization_slug"), field="organization slug", maximum=200
        ),
        name=_identifier(project.get("name"), field="project name", maximum=256),
        region=_display_string(project.get("region"), field="project region", maximum=100),
        created_at=_display_string(
            project.get("created_at"), field="project creation timestamp", maximum=64
        ),
        status=_display_string(project.get("status"), field="project status", maximum=50),
    )


async def _api(
    ctx: ToolExecutionContext,
    connection_id: str,
    requested_project_ref: str,
) -> SupabaseManagementClient:
    resolved = await resolve_connection(ctx, connection_id, connector_type="supabase")
    if resolved.connection.auth_type != "management_token":
        raise SupabaseManagementError(
            "This Supabase connection does not use Management API authentication",
            code="unsupported_auth_type",
        )
    access_token = resolved.credentials.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise SupabaseManagementError(
            "This Supabase connection has no Management API access token",
            code="credential_invalid",
        )
    configured_ref = resolved.config.get("project_ref")
    if not isinstance(configured_ref, str) or not _PROJECT_REF_RE.fullmatch(configured_ref):
        raise SupabaseManagementError(
            "Supabase project configuration is invalid",
            code="invalid_configuration",
        )
    if configured_ref != requested_project_ref:
        raise SupabaseManagementError(
            "Supabase project does not match the configured project",
            code="project_scope_mismatch",
        )
    base_url = resolved.config.get("base_url", DEFAULT_BASE_URL)
    if not isinstance(base_url, str):
        raise SupabaseManagementError(
            "Supabase Management API target is not allowed",
            code="endpoint_not_allowed",
        )
    return SupabaseManagementClient(base_url=base_url, access_token=access_token)


async def _project_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(ProjectReadInput, payload)
    client = await _api(ctx, data.connection_id, data.project_ref)
    project = await client.get_project(data.project_ref)
    return project_read_output(project, project_ref=data.project_ref)


def _clickhouse_string_literal(value: str) -> str:
    escaped: list[str] = []
    named_controls = {
        "\0": r"\0",
        "\b": r"\b",
        "\t": r"\t",
        "\n": r"\n",
        "\f": r"\f",
        "\r": r"\r",
    }
    for character in value:
        if character == "\\":
            escaped.append(r"\\")
        elif character == "'":
            escaped.append(r"\'")
        elif character in named_controls:
            escaped.append(named_controls[character])
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(character)
    return "'" + "".join(escaped) + "'"


def build_logs_query(*, source: str, text_filter: str | None, limit: int) -> str:
    lines = [
        "SELECT",
        "  timestamp,",
        "  source,",
        "  event_message,",
        "  log_attributes['request.path'] AS path,",
        "  toInt32OrZero(log_attributes['response.status_code']) AS status_code,",
        "  log_attributes['request.method'] AS method",
        "FROM logs",
        f"WHERE source = {_clickhouse_string_literal(source)}",
    ]
    if text_filter is not None:
        lines.append(
            "  AND positionCaseInsensitiveUTF8(event_message, "
            f"{_clickhouse_string_literal(text_filter)}) > 0"
        )
    lines.extend(["ORDER BY timestamp DESC", f"LIMIT {limit}"])
    return "\n".join(lines)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _log_record(raw: dict[str, Any], *, requested_source: str) -> LogRecord:
    returned_source = _identifier(raw.get("source"), field="log source", maximum=50)
    if returned_source != requested_source:
        raise SupabaseManagementError(
            "Supabase returned a log from another source",
            code="source_scope_mismatch",
        )
    status_code = raw.get("status_code", 0)
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 0 <= status_code <= 999
    ):
        raise _provider_shape("Supabase returned an invalid log status code")
    return LogRecord(
        timestamp=_identifier(raw.get("timestamp"), field="log timestamp", maximum=64),
        source=returned_source,
        event_message=_display_message(
            raw.get("event_message"), field="log event message", maximum=4_000
        ),
        path=_display_string(raw.get("path"), field="log request path", maximum=2_048),
        status_code=status_code,
        method=_display_string(raw.get("method"), field="log request method", maximum=32),
    )


async def _logs_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(LogsReadInput, payload)
    client = await _api(ctx, data.connection_id, data.project_ref)
    response = await client.get_logs(
        data.project_ref,
        sql=build_logs_query(
            source=data.source,
            text_filter=data.text_filter,
            limit=data.limit,
        ),
        iso_timestamp_start=_utc_iso(data.start),
        iso_timestamp_end=_utc_iso(data.end),
    )
    provider_error = response.get("error")
    if provider_error is not None and provider_error != "":
        raise SupabaseManagementError(
            "Supabase log query failed",
            code="provider_query_error",
        )
    raw_rows = response.get("result")
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        raise _provider_shape("Supabase returned an unexpected logs response")

    logs: list[LogRecord] = []
    truncated = len(raw_rows) > data.limit
    for raw in raw_rows[: data.limit]:
        record = _log_record(raw, requested_source=data.source)
        candidate = LogsReadOutput(logs=[*logs, record], truncated=True)
        if not _gateway_document_retained(candidate, maximum=MAX_LOG_OUTPUT_BYTES):
            truncated = True
            break
        logs.append(record)
    output = LogsReadOutput(logs=logs, truncated=truncated)
    if not _gateway_document_retained(output, maximum=MAX_LOG_OUTPUT_BYTES):
        raise _provider_shape("Supabase log output exceeded its safety limit")
    return output


def _function_info(raw: dict[str, Any], *, project_ref: str) -> FunctionInfo:
    verify_jwt = raw.get("verify_jwt")
    if not isinstance(verify_jwt, bool):
        raise _provider_shape("Supabase returned an invalid function verify_jwt value")
    return FunctionInfo(
        project_ref=project_ref,
        function_id=_identifier(raw.get("id"), field="function id", maximum=200),
        slug=_identifier(raw.get("slug"), field="function slug", maximum=100),
        name=_identifier(raw.get("name"), field="function name", maximum=256),
        status=_display_string(raw.get("status"), field="function status", maximum=50),
        version=_provider_int(raw.get("version"), field="function version"),
        created_at=_provider_int(raw.get("created_at"), field="function creation timestamp"),
        updated_at=_provider_int(raw.get("updated_at"), field="function update timestamp"),
        verify_jwt=verify_jwt,
        entrypoint_path=_display_string(
            raw.get("entrypoint_path"), field="function entrypoint path", maximum=256
        ),
    )


async def _function_list(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FunctionListInput, payload)
    client = await _api(ctx, data.connection_id, data.project_ref)
    raw_functions = await client.list_functions(data.project_ref)
    functions: list[FunctionInfo] = []
    truncated = len(raw_functions) > data.limit
    for raw in raw_functions[: data.limit]:
        function = _function_info(raw, project_ref=data.project_ref)
        candidate = FunctionListOutput(functions=[*functions, function], truncated=True)
        if not _gateway_document_retained(
            candidate,
            maximum=MAX_FUNCTION_LIST_OUTPUT_BYTES,
        ):
            truncated = True
            break
        functions.append(function)
    output = FunctionListOutput(functions=functions, truncated=truncated)
    if not _gateway_document_retained(
        output,
        maximum=MAX_FUNCTION_LIST_OUTPUT_BYTES,
    ):
        raise _provider_shape("Supabase function output exceeded its safety limit")
    return output


async def _function_deploy(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FunctionDeployInput, payload)
    client = await _api(ctx, data.connection_id, data.project_ref)
    metadata = json.dumps(
        {
            "entrypoint_path": data.entrypoint_path,
            "name": data.function_slug,
            "verify_jwt": data.verify_jwt,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    response = await client.deploy_function(
        data.project_ref,
        function_slug=data.function_slug,
        metadata=metadata,
        source_files=[(source.path, source.content.encode("utf-8")) for source in data.files],
    )
    output = _function_info(response, project_ref=data.project_ref)
    if output.slug != data.function_slug:
        raise SupabaseManagementError(
            "Supabase function does not match the requested function",
            code="function_scope_mismatch",
        )
    return output


async def _function_delete(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FunctionDeleteInput, payload)
    client = await _api(ctx, data.connection_id, data.project_ref)
    await client.delete_function(data.project_ref, data.function_slug)
    return FunctionDeleteOutput(
        project_ref=data.project_ref,
        function_slug=data.function_slug,
        deleted=True,
    )


def _tool(
    *,
    name: str,
    description: str,
    risk: RiskLevel,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    executor: ToolExecutor,
    scope_keys: tuple[str, ...],
    supports_approval: bool = False,
) -> tuple[ToolDefinition, ToolExecutor]:
    return (
        ToolDefinition(
            name=name,
            description=description,
            risk=risk,
            input_model=input_model,
            output_model=output_model,
            required_capability=name,
            supports_approval=supports_approval,
            scope_keys=scope_keys,
            required_grant_scope_keys=(
                ("connection_id", "project_ref", "function_slug")
                if name in {"supabase.function.deploy", "supabase.function.delete"}
                else ("connection_id", "project_ref")
            ),
        ),
        executor,
    )


SUPABASE_MANAGEMENT_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    _tool(
        name="supabase.project.read",
        description="Read bounded display-safe Supabase project metadata.",
        risk=RiskLevel.READ,
        input_model=ProjectReadInput,
        output_model=ProjectReadOutput,
        executor=_project_read,
        scope_keys=("connection_id", "project_ref"),
    ),
    _tool(
        name="supabase.logs.read",
        description="Read bounded projected logs from one closed Supabase log source.",
        risk=RiskLevel.READ,
        input_model=LogsReadInput,
        output_model=LogsReadOutput,
        executor=_logs_read,
        scope_keys=("connection_id", "project_ref", "source"),
    ),
    _tool(
        name="supabase.function.list",
        description="List bounded display-safe Supabase Edge Function metadata.",
        risk=RiskLevel.READ,
        input_model=FunctionListInput,
        output_model=FunctionListOutput,
        executor=_function_list,
        scope_keys=("connection_id", "project_ref"),
    ),
    _tool(
        name="supabase.function.deploy",
        description="Deploy a bounded in-memory Supabase Edge Function source bundle.",
        risk=RiskLevel.DESTRUCTIVE,
        input_model=FunctionDeployInput,
        output_model=FunctionInfo,
        executor=_function_deploy,
        scope_keys=("connection_id", "project_ref", "function_slug"),
        supports_approval=True,
    ),
    _tool(
        name="supabase.function.delete",
        description="Delete one Supabase Edge Function by scoped slug.",
        risk=RiskLevel.DESTRUCTIVE,
        input_model=FunctionDeleteInput,
        output_model=FunctionDeleteOutput,
        executor=_function_delete,
        scope_keys=("connection_id", "project_ref", "function_slug"),
        supports_approval=True,
    ),
)


__all__ = [
    "MAX_FUNCTION_LIST_OUTPUT_BYTES",
    "MAX_LOG_OUTPUT_BYTES",
    "SUPABASE_MANAGEMENT_TOOLS",
    "build_logs_query",
    "project_read_output",
]
