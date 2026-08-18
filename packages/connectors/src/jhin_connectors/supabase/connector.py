"""Supabase connector registration and connection lifecycle."""

from __future__ import annotations

import os
import re
from typing import Any

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.supabase.database_client import (
    DatabaseConnectionError,
    verify_database_connection,
)
from jhin_connectors.supabase.database_tools import SUPABASE_DATABASE_TOOLS
from jhin_connectors.supabase.management_client import (
    SupabaseManagementClient,
    SupabaseManagementError,
    validate_supabase_base_url,
)
from jhin_connectors.supabase.management_tools import (
    SUPABASE_MANAGEMENT_TOOLS,
    project_read_output,
)
from jhin_connectors.supabase.manifest import DEFAULT_BASE_URL, SUPABASE_MANIFEST
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor

_PROJECT_REF_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_$]{0,62}$")
_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})


def validate_project_ref(value: Any) -> str:
    if not isinstance(value, str) or not _PROJECT_REF_RE.fullmatch(value):
        raise ValueError("config field 'project_ref' is invalid")
    return value


def validate_allowed_schemas(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 8:
        raise ValueError("config field 'allowed_schemas' is invalid")
    normalized: list[str] = []
    seen: set[str] = set()
    for schema in value:
        if not isinstance(schema, str):
            raise ValueError("config field 'allowed_schemas' is invalid")
        folded = schema.casefold()
        if (
            schema != folded
            or not _SCHEMA_RE.fullmatch(schema)
            or folded in _SYSTEM_SCHEMAS
            or folded.startswith("pg_")
            or folded in seen
        ):
            raise ValueError("config field 'allowed_schemas' is invalid")
        seen.add(folded)
        normalized.append(schema)
    return normalized


class SupabaseConnector(Connector):
    manifest = SUPABASE_MANIFEST

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(config)
        if auth_type == "management_token":
            if set(normalized) - {"project_ref", "base_url"}:
                raise ValueError("config fields are not allowed for management_token")
            normalized["project_ref"] = validate_project_ref(normalized.get("project_ref"))
            base_url = normalized.get("base_url", DEFAULT_BASE_URL)
            if not isinstance(base_url, str):
                raise ValueError("config field 'base_url' must be text")
            try:
                normalized["base_url"] = validate_supabase_base_url(base_url)
            except SupabaseManagementError:
                raise ValueError("config field 'base_url' is not allowed") from None
            return normalized
        if auth_type == "postgres":
            allowed_fields = {
                "project_ref",
                "allowed_schemas",
                "allow_writes",
                "statement_timeout_ms",
                "lock_timeout_ms",
                "max_rows",
                "max_cell_bytes",
                "max_result_bytes",
            }
            if set(normalized) - allowed_fields:
                raise ValueError("config fields are not allowed for postgres")
            normalized["project_ref"] = validate_project_ref(normalized.get("project_ref"))
            normalized["allowed_schemas"] = validate_allowed_schemas(
                normalized.get("allowed_schemas", ["public"])
            )
            max_cell_bytes = normalized.get("max_cell_bytes")
            max_result_bytes = normalized.get("max_result_bytes")
            if (
                isinstance(max_cell_bytes, int)
                and not isinstance(max_cell_bytes, bool)
                and isinstance(max_result_bytes, int)
                and not isinstance(max_result_bytes, bool)
                and max_cell_bytes > max_result_bytes
            ):
                raise ValueError("config field 'max_cell_bytes' cannot exceed max_result_bytes")
            return normalized
        raise ValueError("unsupported Supabase auth type")

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        if ctx.auth_type == "postgres":
            database_url = ctx.credentials.get("database_url")
            project_ref = ctx.config.get("project_ref")
            if not isinstance(database_url, str) or not database_url:
                return ConnectionHealth(ok=False, message="Supabase database URL is missing")
            try:
                validated_ref = validate_project_ref(project_ref)
                allowed_schemas = validate_allowed_schemas(
                    ctx.config.get("allowed_schemas", ["public"])
                )
                await verify_database_connection(
                    database_url,
                    project_ref=validated_ref,
                    allowed_schemas=tuple(allowed_schemas),
                    app_database_url=os.getenv("DATABASE_URL"),
                )
            except (ValueError, DatabaseConnectionError) as exc:
                message = (
                    str(exc)
                    if isinstance(exc, DatabaseConnectionError)
                    else "Supabase project configuration is invalid"
                )
                return ConnectionHealth(ok=False, message=message)
            return ConnectionHealth(
                ok=True,
                message="Supabase database connection verified",
                details={"project_ref": validated_ref},
            )
        if ctx.auth_type == "management_token":
            access_token = ctx.credentials.get("access_token")
            project_ref = ctx.config.get("project_ref")
            base_url = ctx.config.get("base_url", DEFAULT_BASE_URL)
            if not isinstance(access_token, str) or not access_token:
                return ConnectionHealth(
                    ok=False, message="Supabase Management API access token is missing"
                )
            if not isinstance(base_url, str):
                return ConnectionHealth(
                    ok=False, message="Supabase Management API target is not allowed"
                )
            try:
                validated_ref = validate_project_ref(project_ref)
                client = SupabaseManagementClient(
                    base_url=base_url,
                    access_token=access_token,
                )
                project = project_read_output(
                    await client.get_project(validated_ref),
                    project_ref=validated_ref,
                )
            except (ValueError, SupabaseManagementError) as exc:
                message = (
                    str(exc)
                    if isinstance(exc, SupabaseManagementError)
                    else "Supabase project configuration is invalid"
                )
                return ConnectionHealth(ok=False, message=message)
            return ConnectionHealth(
                ok=True,
                message="Supabase Management API connection verified",
                details={"project_ref": validated_ref, "name": project.name},
            )
        return ConnectionHealth(ok=False, message="Unsupported Supabase authentication type")

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return SUPABASE_MANAGEMENT_TOOLS + SUPABASE_DATABASE_TOOLS


__all__ = ["SupabaseConnector", "validate_allowed_schemas", "validate_project_ref"]
