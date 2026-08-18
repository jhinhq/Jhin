"""Low-privilege Supabase PostgreSQL connection verification."""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import pytest

from jhin_connectors.supabase import database_client
from jhin_connectors.supabase.database_client import (
    DatabaseConnectionError,
    verify_database_connection,
)

SAFE_ROLE = {
    "current_user": "jhin_reader",
    "session_user": "jhin_reader",
    "rolsuper": False,
    "rolbypassrls": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolreplication": False,
    "owns_current_database": False,
    "owns_allowed_schema": False,
}


class FakeDatabaseConnection:
    def __init__(self, role: dict[str, object] | None = SAFE_ROLE) -> None:
        self.role = role
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False
        self.fetch_error: Exception | None = None
        self.close_error: Exception | None = None
        self.stall_fetch = False
        self.stall_close = False

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.queries.append((query, args))
        if self.stall_fetch:
            await asyncio.Event().wait()
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.role

    async def close(self) -> None:
        self.closed = True
        if self.stall_close:
            await asyncio.Event().wait()
        if self.close_error is not None:
            raise self.close_error


async def test_database_verify_validates_target_checks_role_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = "postgresql://jhin_reader:database-password-marker@127.0.0.1:65433/fixture"
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    connection = FakeDatabaseConnection()
    connect_calls: list[dict[str, Any]] = []

    async def fake_connect(**kwargs: Any) -> FakeDatabaseConnection:
        connect_calls.append(kwargs)
        return connection

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    await verify_database_connection(
        dsn,
        project_ref="abcdefghijklmnopqrst",
        allowed_schemas=("public",),
        app_database_url="postgresql://jhin:app-secret@127.0.0.1:5432/jhin",
    )

    assert connect_calls == [
        {
            "dsn": dsn,
            "timeout": 5,
            "statement_cache_size": 0,
            "ssl": None,
        }
    ]
    assert len(connection.queries) == 1
    query, args = connection.queries[0]
    assert "pg_catalog.pg_roles" in query
    assert "pg_catalog.pg_database" in query
    assert "pg_catalog.pg_namespace" in query
    assert "current_database()" in query
    assert args == (["public"],)
    for field in (
        "current_user",
        "session_user",
        "rolsuper",
        "rolbypassrls",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "owns_current_database",
        "owns_allowed_schema",
    ):
        assert field in query
    assert connection.closed is True


async def test_database_verify_uses_hosted_tls_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = (
        "postgresql://jhin_reader:database-password-marker@"
        "db.abcdefghijklmnopqrst.supabase.co:5432/postgres?sslmode=require"
    )
    connection = FakeDatabaseConnection()
    connect_calls: list[dict[str, Any]] = []

    async def fake_connect(**kwargs: Any) -> FakeDatabaseConnection:
        connect_calls.append(kwargs)
        return connection

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    await verify_database_connection(
        dsn,
        project_ref="abcdefghijklmnopqrst",
        allowed_schemas=("public",),
        app_database_url=None,
    )

    assert isinstance(connect_calls[0]["ssl"], ssl.SSLContext)
    assert connect_calls[0]["statement_cache_size"] == 0
    assert connection.closed is True


async def test_database_verify_rejects_official_transaction_pooler_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "database-password-marker"
    dsn = (
        f"postgresql://postgres.abcdefghijklmnopqrst:{marker}@"
        "aws-0-us-west-1.pooler.supabase.com:6543/postgres?sslmode=require"
    )
    connected = False

    async def fake_connect(**_kwargs: Any) -> FakeDatabaseConnection:
        nonlocal connected
        connected = True
        return FakeDatabaseConnection()

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    with pytest.raises(DatabaseConnectionError) as exc_info:
        await verify_database_connection(
            dsn,
            project_ref="abcdefghijklmnopqrst",
            allowed_schemas=("public",),
            app_database_url=None,
        )

    assert exc_info.value.code == "database_target_not_allowed"
    assert marker not in str(exc_info.value)
    assert connected is False


async def test_database_verify_normalizes_sqlalchemy_scheme_without_rewriting_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded_tail = "jhin_reader:pa%2Fss%3Aword@127.0.0.1:65433/fixture"
    dsn = f"postgresql+asyncpg://{encoded_tail}"
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    connection = FakeDatabaseConnection()
    connect_calls: list[dict[str, Any]] = []

    async def fake_connect(**kwargs: Any) -> FakeDatabaseConnection:
        connect_calls.append(kwargs)
        return connection

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    await verify_database_connection(
        dsn,
        project_ref="abcdefghijklmnopqrst",
        allowed_schemas=("public",),
        app_database_url=None,
    )

    assert connect_calls[0]["dsn"] == f"postgresql://{encoded_tail}"
    assert connection.closed is True


async def test_database_verify_normalizes_mixed_case_sqlalchemy_scheme_for_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded_tail = "jhin_reader:pa%2Fss@127.0.0.1:65433/fixture"
    dsn = f"PostgreSQL+AsyncPG://{encoded_tail}"
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    connection = FakeDatabaseConnection()
    connect_calls: list[dict[str, Any]] = []

    async def fake_connect(**kwargs: Any) -> FakeDatabaseConnection:
        connect_calls.append(kwargs)
        return connection

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    await verify_database_connection(
        dsn,
        project_ref="abcdefghijklmnopqrst",
        allowed_schemas=("public",),
        app_database_url=None,
    )

    assert connect_calls[0]["dsn"] == f"postgresql://{encoded_tail}"
    assert connection.closed is True


@pytest.mark.parametrize(
    "role",
    [
        None,
        {**SAFE_ROLE, "current_user": "postgres"},
        {**SAFE_ROLE, "session_user": "another_role"},
        {**SAFE_ROLE, "rolsuper": True},
        {**SAFE_ROLE, "rolbypassrls": True},
        {**SAFE_ROLE, "rolcreatedb": True},
        {**SAFE_ROLE, "rolcreaterole": True},
        {**SAFE_ROLE, "rolreplication": True},
        {**SAFE_ROLE, "owns_current_database": True},
        {**SAFE_ROLE, "owns_allowed_schema": True},
    ],
)
async def test_database_verify_rejects_missing_or_privileged_role_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
    role: dict[str, object] | None,
) -> None:
    marker = "database-password-marker"
    dsn = f"postgresql://jhin_reader:{marker}@127.0.0.1:65433/fixture"
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    connection = FakeDatabaseConnection(role)

    async def fake_connect(**_kwargs: Any) -> FakeDatabaseConnection:
        return connection

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    with pytest.raises(DatabaseConnectionError) as exc_info:
        await verify_database_connection(
            dsn,
            project_ref="abcdefghijklmnopqrst",
            allowed_schemas=("public",),
            app_database_url=None,
        )

    assert exc_info.value.code == "database_role_not_least_privilege"
    assert marker not in str(exc_info.value)
    assert "jhin_reader" not in str(exc_info.value)
    assert "postgres" not in str(exc_info.value)
    assert connection.closed is True


async def test_database_verify_closes_after_role_query_failure_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "database-password-marker"
    dsn = f"postgresql://jhin_reader:{marker}@127.0.0.1:65433/fixture"
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    connection = FakeDatabaseConnection()
    connection.fetch_error = RuntimeError(f"provider included {marker}")

    async def fake_connect(**_kwargs: Any) -> FakeDatabaseConnection:
        return connection

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    with pytest.raises(DatabaseConnectionError) as exc_info:
        await verify_database_connection(
            dsn,
            project_ref="abcdefghijklmnopqrst",
            allowed_schemas=("public",),
            app_database_url=None,
        )

    assert exc_info.value.code == "database_verification_failed"
    assert marker not in str(exc_info.value)
    assert connection.closed is True


async def test_database_verify_rejects_target_before_connect_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "database-password-marker"
    dsn = f"postgresql://jhin_reader:{marker}@127.0.0.1:65433/fixture"
    connected = False

    async def fake_connect(**_kwargs: Any) -> FakeDatabaseConnection:
        nonlocal connected
        connected = True
        return FakeDatabaseConnection()

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    with pytest.raises(DatabaseConnectionError) as exc_info:
        await verify_database_connection(
            dsn,
            project_ref="abcdefghijklmnopqrst",
            allowed_schemas=("public",),
            app_database_url=None,
        )

    assert exc_info.value.code == "database_target_not_allowed"
    assert marker not in str(exc_info.value)
    assert connected is False


@pytest.mark.parametrize(
    ("dsn", "marker"),
    [
        (
            "postgresql://sensitive-login@db.abcdefghijklmnopqrst.supabase.co:5432/"
            "postgres?sslmode=require",
            "sensitive-login",
        ),
        (
            "postgresql://jhin_reader:password-marker@"
            "db.abcdefghijklmnopqrst.supabase.co:5432/postgres?"
            "sslmode=require&application_name=query-marker",
            "query-marker",
        ),
    ],
)
async def test_database_verify_rejects_missing_password_or_extra_query_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    dsn: str,
    marker: str,
) -> None:
    connected = False

    async def fake_connect(**_kwargs: Any) -> FakeDatabaseConnection:
        nonlocal connected
        connected = True
        return FakeDatabaseConnection()

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    with pytest.raises(DatabaseConnectionError) as exc_info:
        await verify_database_connection(
            dsn,
            project_ref="abcdefghijklmnopqrst",
            allowed_schemas=("public",),
            app_database_url=None,
        )

    assert exc_info.value.code == "database_target_not_allowed"
    assert marker not in str(exc_info.value)
    assert connected is False


async def test_database_verify_rejects_jhin_application_database_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "database-password-marker"
    dsn = f"postgresql://jhin_reader:{marker}@127.0.0.1:65433/fixture"
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    connected = False

    async def fake_connect(**_kwargs: Any) -> FakeDatabaseConnection:
        nonlocal connected
        connected = True
        return FakeDatabaseConnection()

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    with pytest.raises(DatabaseConnectionError) as exc_info:
        await verify_database_connection(
            dsn,
            project_ref="abcdefghijklmnopqrst",
            allowed_schemas=("public",),
            app_database_url="postgresql://jhin:another-password@127.0.0.1:65433/fixture",
        )

    assert exc_info.value.code == "database_target_not_allowed"
    assert marker not in str(exc_info.value)
    assert connected is False


@pytest.mark.parametrize("stall", ["query", "close"])
async def test_database_verify_bounds_query_and_close(
    monkeypatch: pytest.MonkeyPatch,
    stall: str,
) -> None:
    dsn = "postgresql://jhin_reader:database-password-marker@127.0.0.1:65433/fixture"
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    monkeypatch.setattr(database_client, "DATABASE_VERIFY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(database_client, "DATABASE_CLOSE_TIMEOUT_SECONDS", 0.01)
    connection = FakeDatabaseConnection()
    connection.stall_fetch = stall == "query"
    connection.stall_close = stall == "close"

    async def fake_connect(**_kwargs: Any) -> FakeDatabaseConnection:
        return connection

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)

    with pytest.raises(DatabaseConnectionError) as exc_info:
        async with asyncio.timeout(0.25):
            await verify_database_connection(
                dsn,
                project_ref="abcdefghijklmnopqrst",
                allowed_schemas=("public",),
                app_database_url=None,
            )

    assert exc_info.value.code == "database_verification_failed"
    assert connection.closed is True


async def test_database_verify_preserves_external_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = "postgresql://jhin_reader:database-password-marker@127.0.0.1:65433/fixture"
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    connection = FakeDatabaseConnection()
    connection.stall_fetch = True

    async def fake_connect(**_kwargs: Any) -> FakeDatabaseConnection:
        return connection

    monkeypatch.setattr(database_client.asyncpg, "connect", fake_connect)
    task = asyncio.create_task(
        verify_database_connection(
            dsn,
            project_ref="abcdefghijklmnopqrst",
            allowed_schemas=("public",),
            app_database_url=None,
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.closed is True
