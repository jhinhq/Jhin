"""Live low-privilege PostgreSQL verification for Supabase connections."""

from __future__ import annotations

import asyncio
import re
import ssl
import sys
from collections.abc import Sequence
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import asyncpg

from jhin_connectors.endpoints import EndpointPolicyError, validate_postgres_target
from jhin_connectors.supabase.database_preflight import (
    MAX_ALLOWED_SCHEMAS,
    DatabasePreflightError,
    verify_live_role,
)

DATABASE_VERIFY_TIMEOUT_SECONDS = 10.0
DATABASE_CLOSE_TIMEOUT_SECONDS = 2.0
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_$]{0,62}$")
_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})


class DatabaseConnectionError(Exception):
    """A stable, credential-free database verification failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class _DatabaseConnection(Protocol):
    async def fetch(self, query: str, *args: object) -> Sequence[Any]: ...

    async def fetchrow(self, query: str, *args: object) -> Any: ...

    async def execute(self, query: str, *args: object) -> str: ...

    async def close(self) -> None: ...


def _hosted_ssl_context(dsn: str, project_ref: str) -> ssl.SSLContext | None:
    try:
        host = urlsplit(dsn).hostname
    except ValueError:
        return None
    normalized = project_ref.casefold()
    if host is not None and (
        host.casefold() == f"db.{normalized}.supabase.co"
        or host.casefold().endswith(".pooler.supabase.com")
    ):
        return ssl.create_default_context()
    return None


def _asyncpg_dsn(validated_dsn: str) -> str:
    sqlalchemy_prefix = "postgresql+asyncpg://"
    if validated_dsn[: len(sqlalchemy_prefix)].casefold() == sqlalchemy_prefix:
        return "postgresql://" + validated_dsn[len(sqlalchemy_prefix) :]
    return validated_dsn


async def verify_database_connection(
    database_url: str,
    *,
    project_ref: str,
    allowed_schemas: tuple[str, ...],
    app_database_url: str | None,
) -> None:
    """Validate and verify one low-privilege PostgreSQL credential."""
    try:
        validated_url = validate_postgres_target(
            database_url,
            project_ref=project_ref,
            app_database_url=app_database_url,
        )
    except EndpointPolicyError:
        raise DatabaseConnectionError(
            "Supabase database target is not allowed",
            code="database_target_not_allowed",
        ) from None

    if (
        not isinstance(allowed_schemas, tuple)
        or not 1 <= len(allowed_schemas) <= MAX_ALLOWED_SCHEMAS
        or len(set(allowed_schemas)) != len(allowed_schemas)
        or any(
            not isinstance(schema, str)
            or not _SCHEMA_RE.fullmatch(schema)
            or schema in _SYSTEM_SCHEMAS
            or schema.startswith("pg_")
            for schema in allowed_schemas
        )
    ):
        raise DatabaseConnectionError(
            "Supabase database verification failed",
            code="database_verification_failed",
        )

    connection: _DatabaseConnection | None = None
    try:
        async with asyncio.timeout(DATABASE_VERIFY_TIMEOUT_SECONDS):
            driver_url = _asyncpg_dsn(validated_url)
            connection = cast(
                "_DatabaseConnection",
                await asyncpg.connect(
                    dsn=driver_url,
                    timeout=5,
                    statement_cache_size=0,
                    ssl=_hosted_ssl_context(validated_url, project_ref),
                ),
            )
            try:
                await connection.execute("SET search_path TO pg_catalog")
                await verify_live_role(connection, allowed_schemas)
            except DatabasePreflightError:
                raise DatabaseConnectionError(
                    "Supabase database role is not least privilege",
                    code="database_role_not_least_privilege",
                ) from None
    except DatabaseConnectionError:
        raise
    except Exception:
        raise DatabaseConnectionError(
            "Supabase database verification failed",
            code="database_verification_failed",
        ) from None
    finally:
        if connection is not None:
            active_exception = sys.exc_info()[0] is not None
            try:
                async with asyncio.timeout(DATABASE_CLOSE_TIMEOUT_SECONDS):
                    await connection.close()
            except Exception:
                if not active_exception:
                    raise DatabaseConnectionError(
                        "Supabase database verification failed",
                        code="database_verification_failed",
                    ) from None


__all__ = ["DatabaseConnectionError", "verify_database_connection"]
