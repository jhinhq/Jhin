"""Real PostgreSQL 17 boundaries for bounded Supabase database tools.

These tests intentionally exercise asyncpg and PostgreSQL itself.  The fixture
database is disposable and never substitutes for Jhin's application database.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import asyncpg
import pytest
from pydantic import BaseModel

from jhin_connectors.base import VerifyContext
from jhin_connectors.supabase import database_preflight, database_tools
from jhin_connectors.supabase.connector import SupabaseConnector
from jhin_connectors.supabase.database_tools import SupabaseDatabaseError
from jhin_connectors.supabase.schemas import (
    DatabaseMutationInput,
    DatabaseMutationOutput,
    DatabaseReadInput,
    DatabaseReadOutput,
)
from jhin_db.models import Agent, AgentCapabilityGrant, Connection, Workspace
from jhin_secrets import SecretStore, get_redactor
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import ToolGateway

pytestmark = pytest.mark.integration

PROJECT_REF = "abcdefghijklmnopqrst"
EXECUTORS = {definition.name: executor for definition, executor in SupabaseConnector().tools()}


@dataclass(frozen=True)
class DatabaseDsns:
    reader: str
    writer: str
    admin: str


DatabaseRole = Literal["reader", "writer"]
DatabaseParam = bool | int | float | str | None
PostgresConnectionFactory = Callable[..., Awaitable[Connection]]


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.fail(f"{name} is required for the real PostgreSQL integration gate")
    return value


@pytest.fixture(scope="module")
def database_dsns() -> DatabaseDsns:
    dsns = DatabaseDsns(
        reader=_required_env("JHIN_PHASE9_DB_READER_DSN"),
        writer=_required_env("JHIN_PHASE9_DB_WRITER_DSN"),
        admin=_required_env("JHIN_PHASE9_DB_ADMIN_DSN"),
    )
    for dsn in (dsns.reader, dsns.writer, dsns.admin):
        parsed = urlsplit(dsn)
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or not parsed.hostname
            or not parsed.port
        ):
            pytest.fail("Phase 9 database DSNs must be explicit PostgreSQL host/port URLs")
    expected_allowlist = f"{urlsplit(dsns.reader).hostname}:{urlsplit(dsns.reader).port}"
    if os.getenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS") != expected_allowlist:
        pytest.fail("JHIN_CONNECTOR_ALLOWED_DB_HOSTS must exactly allow the fixture host and port")
    return dsns


@pytest.fixture
async def admin(database_dsns: DatabaseDsns) -> Any:
    connection = await asyncpg.connect(
        dsn=database_dsns.admin,
        timeout=5,
        statement_cache_size=0,
    )
    try:
        yield connection
    finally:
        await asyncio.wait_for(connection.close(), timeout=2)


@pytest.fixture(autouse=True)
async def reset_fixture_state(admin: Any) -> None:
    await admin.execute(
        """
        ALTER ROLE jhin_reader
          NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        ALTER ROLE jhin_writer
          NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        REVOKE pg_read_all_data FROM jhin_reader, jhin_writer;
        ALTER SCHEMA public OWNER TO pg_database_owner;
        ALTER TABLE public.widget_groups OWNER TO postgres;
        ALTER TABLE public.widgets OWNER TO postgres;
        GRANT SELECT ON public.widgets, public.widget_groups TO jhin_reader;
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public.widgets TO jhin_writer;
        GRANT SELECT ON public.widget_groups TO jhin_writer;
        GRANT MAINTAIN ON public.widget_groups TO jhin_writer;
        DELETE FROM public.widgets;
        DELETE FROM public.widget_groups;
        INSERT INTO public.widget_groups VALUES (1, 'primary');
        INSERT INTO public.widgets VALUES
          (1, 1, 'alpha'), (2, 1, 'beta'), (3, 1, repeat('x', 20000));
        TRUNCATE private.side_effects RESTART IDENTITY;
        """
    )


@pytest.fixture
def make_postgres_connection(
    workspace: Workspace,
    make_connection: Any,
    database_dsns: DatabaseDsns,
) -> PostgresConnectionFactory:
    connection_number = 0

    async def factory(
        *,
        role: DatabaseRole,
        max_rows: int = 200,
        max_cell_bytes: int = 4_096,
        max_result_bytes: int = 24_000,
        statement_timeout_ms: int = 5_000,
        lock_timeout_ms: int = 1_000,
        allow_writes: bool | None = None,
    ) -> Connection:
        nonlocal connection_number
        connection_number += 1
        can_write = role == "writer" if allow_writes is None else allow_writes
        dsn = database_dsns.writer if role == "writer" else database_dsns.reader
        return cast(
            Connection,
            await make_connection(
                workspace,
                connector_type="supabase",
                name=f"Phase 9 {role} database {connection_number}",
                auth_type="postgres",
                credentials={"database_url": dsn},
                config={
                    "project_ref": PROJECT_REF,
                    "allowed_schemas": ["public"],
                    "allow_writes": can_write,
                    "statement_timeout_ms": statement_timeout_ms,
                    "lock_timeout_ms": lock_timeout_ms,
                    "max_rows": max_rows,
                    "max_cell_bytes": max_cell_bytes,
                    "max_result_bytes": max_result_bytes,
                },
            ),
        )

    return factory


async def _invoke(
    name: str,
    context: ToolExecutionContext,
    payload: BaseModel,
) -> BaseModel:
    return await EXECUTORS[name](context, payload)


async def _expect_error(
    name: str,
    context: ToolExecutionContext,
    payload: BaseModel,
    code: str,
) -> SupabaseDatabaseError:
    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(name, context, payload)
    assert exc_info.value.code == code
    return exc_info.value


def _read_input(
    connection: Connection,
    sql: str,
    params: list[DatabaseParam] | None = None,
) -> BaseModel:
    return DatabaseReadInput(
        connection_id=str(connection.id),
        project_ref=PROJECT_REF,
        schema="public",
        sql=sql,
        params=params or [],
    )


def _mutation_input(
    connection: Connection,
    sql: str,
    params: list[DatabaseParam] | None = None,
) -> BaseModel:
    return DatabaseMutationInput(
        connection_id=str(connection.id),
        project_ref=PROJECT_REF,
        schema="public",
        sql=sql,
        params=params or [],
    )


async def _create_fk_chain(admin: Any, prefix: str, length: int) -> None:
    for index in reversed(range(length)):
        relation = f"{prefix}_{index:02d}"
        if index == length - 1:
            await admin.execute(f"CREATE TABLE public.{relation} (id integer PRIMARY KEY)")
            continue
        peer = f"{prefix}_{index + 1:02d}"
        await admin.execute(
            f"CREATE TABLE public.{relation} ("
            "id integer PRIMARY KEY, "
            f"peer_id integer REFERENCES public.{peer}(id) "
            "ON UPDATE NO ACTION ON DELETE NO ACTION NOT DEFERRABLE)"
        )
        await admin.execute(f"CREATE INDEX {relation}_peer_idx ON public.{relation}(peer_id)")
    target = f"{prefix}_00"
    peers = ", ".join(f"public.{prefix}_{index:02d}" for index in range(1, length))
    await admin.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public.{target} TO jhin_writer"
    )
    if peers:
        await admin.execute(f"GRANT SELECT, MAINTAIN ON {peers} TO jhin_writer")


async def _drop_fk_chain(admin: Any, prefix: str, length: int) -> None:
    relations = ", ".join(f"public.{prefix}_{index:02d}" for index in range(length))
    await admin.execute(f"DROP TABLE IF EXISTS {relations} CASCADE")


async def test_privileged_operator_namespace_tamper_is_a_declared_trust_boundary(
    database_dsns: DatabaseDsns,
    admin: Any,
) -> None:
    reader = await asyncpg.connect(dsn=database_dsns.reader, timeout=5, statement_cache_size=0)
    writer = await asyncpg.connect(dsn=database_dsns.writer, timeout=5, statement_cache_size=0)
    try:
        # Jhin roles and their reachable ancestors cannot rename/recreate the
        # allowed namespace. A privileged database operator still can, and is
        # therefore an explicit trust boundary rather than an agent sandbox.
        facts = await reader.fetchrow(
            """
            SELECT
              current_setting('server_version_num')::integer / 10000 AS major,
              session_user = current_user AS same_user,
              current_setting('server_encoding') AS encoding,
              current_setting('session_replication_role') AS replication_role,
              has_parameter_privilege(current_user, 'temp_file_limit', 'SET') AS can_set_temp,
              has_schema_privilege(current_user, 'public', 'CREATE') AS can_create,
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_roles AS role ON role.oid = namespace.nspowner
                WHERE namespace.nspname = 'public' AND role.rolname = current_user
              ) AS owns_public_schema,
              has_schema_privilege(current_user, 'private', 'USAGE') AS can_use_private,
              has_table_privilege(current_user, 'public.widgets', 'SELECT') AS can_read,
              has_table_privilege(current_user, 'public.widgets', 'INSERT') AS can_insert
            """
        )
        assert dict(facts) == {
            "major": 17,
            "same_user": True,
            "encoding": "UTF8",
            "replication_role": "origin",
            "can_set_temp": True,
            "can_create": False,
            "owns_public_schema": False,
            "can_use_private": False,
            "can_read": True,
            "can_insert": False,
        }
        assert await admin.fetchval("SELECT ready FROM public.fixture_ready") is True
        assert (
            await writer.fetchval(
                "SELECT has_table_privilege(current_user, 'public.widget_groups', 'MAINTAIN')"
            )
            is True
        )
        assert (
            await writer.fetchval(
                "SELECT has_table_privilege(current_user, 'public.widgets', 'TRUNCATE')"
            )
            is True
        )
        writer_schema_facts = await writer.fetchrow(
            """
            SELECT
              has_schema_privilege(current_user, 'public', 'CREATE') AS can_create,
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_roles AS role ON role.oid = namespace.nspowner
                WHERE namespace.nspname = 'public' AND role.rolname = current_user
              ) AS owns_public_schema
            """
        )
        assert dict(writer_schema_facts) == {
            "can_create": False,
            "owns_public_schema": False,
        }
    finally:
        await reader.close()
        await writer.close()


async def test_credential_rotation_after_successful_verify_uses_current_secret(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    database_dsns: DatabaseDsns,
) -> None:
    health = await SupabaseConnector().verify_connection(
        VerifyContext(
            auth_type="postgres",
            credentials={"database_url": database_dsns.reader},
            config={"project_ref": PROJECT_REF, "allowed_schemas": ["public"]},
        )
    )
    assert health.ok is True

    connection = await make_postgres_connection(role="reader")
    assert connection.encrypted_secret_id is not None
    store = SecretStore(context.session, context.crypto)
    rotated_marker = "credential-rotation-secret-marker"
    rotated_url = database_dsns.reader.replace("reader-pass", rotated_marker)
    await store.rotate(
        context.workspace_id,
        connection.encrypted_secret_id,
        json.dumps({"database_url": rotated_url}),
    )

    error = await _expect_error(
        "supabase.database.read",
        context,
        _read_input(connection, "SELECT id FROM public.widgets WHERE id = 1"),
        "database_execution_failed",
    )
    assert rotated_marker not in str(error)


async def test_session_user_current_user_mismatch_is_rejected_live(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE ROLE phase9_session_switched NOLOGIN;
        GRANT phase9_session_switched TO jhin_reader;
        ALTER ROLE jhin_reader SET role TO phase9_session_switched;
        """
    )
    try:
        connection = await make_postgres_connection(role="reader")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets WHERE id = 1"),
            "database_role_not_least_privilege",
        )
    finally:
        await admin.execute(
            """
            ALTER ROLE jhin_reader RESET role;
            REVOKE phase9_session_switched FROM jhin_reader;
            DROP ROLE IF EXISTS phase9_session_switched;
            """
        )


@pytest.mark.parametrize(
    ("enable", "disable"),
    [
        ("SUPERUSER", "NOSUPERUSER"),
        ("CREATEDB", "NOCREATEDB"),
        ("CREATEROLE", "NOCREATEROLE"),
        ("REPLICATION", "NOREPLICATION"),
        ("BYPASSRLS", "NOBYPASSRLS"),
    ],
)
async def test_each_direct_privileged_role_flag_is_rejected_live(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
    enable: str,
    disable: str,
) -> None:
    await admin.execute(f"ALTER ROLE jhin_reader {enable}")
    try:
        connection = await make_postgres_connection(role="reader")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets WHERE id = 1"),
            "database_role_not_least_privilege",
        )
    finally:
        await admin.execute(f"ALTER ROLE jhin_reader {disable}")


async def test_custom_symbolic_operator_fires_under_hostile_search_path_but_not_trusted_namespace(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    database_dsns: DatabaseDsns,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TYPE public.phase9_operator_value AS (id integer);
        CREATE FUNCTION private.phase9_operator_effect(
          left_value public.phase9_operator_value,
          right_value public.phase9_operator_value
        )
        RETURNS boolean
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
        BEGIN
          INSERT INTO side_effects(source) VALUES ('custom-operator');
          RETURN left_value.id = right_value.id;
        END
        $function$;
        REVOKE ALL ON FUNCTION private.phase9_operator_effect(
          public.phase9_operator_value, public.phase9_operator_value
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION private.phase9_operator_effect(
          public.phase9_operator_value, public.phase9_operator_value
        ) TO jhin_reader;
        CREATE OPERATOR public.#=# (
          LEFTARG = public.phase9_operator_value,
          RIGHTARG = public.phase9_operator_value,
          FUNCTION = private.phase9_operator_effect
        );
        ALTER ROLE jhin_reader SET search_path TO public, pg_catalog;
        """
    )
    control = await asyncpg.connect(
        dsn=database_dsns.reader,
        timeout=5,
        statement_cache_size=0,
    )
    try:
        assert (
            await control.fetchval(
                "SELECT ROW(1)::public.phase9_operator_value "
                "#=# ROW(1)::public.phase9_operator_value"
            )
            is True
        )
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM private.side_effects WHERE source = 'custom-operator'"
            )
            == 1
        )
        await admin.execute("TRUNCATE private.side_effects RESTART IDENTITY")

        await control.execute("SET search_path TO pg_catalog")
        with pytest.raises(asyncpg.UndefinedFunctionError):
            await control.fetchval(
                "SELECT ROW(1)::public.phase9_operator_value "
                "#=# ROW(1)::public.phase9_operator_value"
            )

        connection = await make_postgres_connection(role="reader")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                "SELECT id FROM public.widgets WHERE "
                "CAST(ROW(id) AS public.phase9_operator_value) "
                "OPERATOR(public.#=#) "
                "CAST(ROW(1) AS public.phase9_operator_value)",
            ),
            "database_sql_not_allowed",
        )
        assert await admin.fetchval("SELECT count(*) FROM private.side_effects") == 0
    finally:
        await asyncio.wait_for(control.close(), timeout=2)
        await admin.execute(
            """
            ALTER ROLE jhin_reader RESET search_path;
            DROP OPERATOR IF EXISTS public.#=# (
              public.phase9_operator_value, public.phase9_operator_value
            );
            DROP FUNCTION IF EXISTS private.phase9_operator_effect(
              public.phase9_operator_value, public.phase9_operator_value
            );
            DROP TYPE IF EXISTS public.phase9_operator_value;
            """
        )


async def test_read_is_positional_parameterized_and_bounded_by_rows_and_cells(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
) -> None:
    connection = await make_postgres_connection(role="reader", max_rows=2, max_cell_bytes=256)

    output = await _invoke(
        "supabase.database.read",
        context,
        _read_input(
            connection,
            "SELECT w.id AS duplicate, w.name AS duplicate "
            "FROM public.widgets AS w WHERE w.id >= $1 ORDER BY w.id",
            [1],
        ),
    )

    assert isinstance(output, DatabaseReadOutput)
    assert output.columns == ["duplicate", "duplicate"]
    assert output.rows == [["1", "alpha"], ["2", "beta"]]
    assert output.row_count == 2
    assert output.truncated is True


async def test_recursive_cte_join_and_set_operation_return_fixed_width_values(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
) -> None:
    connection = await make_postgres_connection(role="reader")

    output = await _invoke(
        "supabase.database.read",
        context,
        _read_input(
            connection,
            "WITH RECURSIVE wanted(id) AS ("
            "SELECT 1 UNION ALL SELECT id + 1 FROM wanted WHERE id < 2"
            "), matched AS ("
            "SELECT w.id FROM public.widgets AS w JOIN wanted AS x ON x.id = w.id"
            ") SELECT id FROM matched UNION SELECT 3",
        ),
    )

    assert isinstance(output, DatabaseReadOutput)
    assert output.columns == ["id"]
    assert {tuple(row) for row in output.rows} == {("1",), ("2",), ("3",)}
    assert output.row_count == 3
    assert output.truncated is False


async def test_postgres_identifier_folding_and_quoted_non_ascii_names_are_exact(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public."Phase9_Å" ("MiXeD" integer);
        INSERT INTO public."Phase9_Å" VALUES (17);
        GRANT SELECT ON public."Phase9_Å" TO jhin_reader;
        """
    )
    try:
        connection = await make_postgres_connection(role="reader")

        folded = await _invoke(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT ID FROM PUBLIC.WIDGETS WHERE ID = 1"),
        )
        exact = await _invoke(
            "supabase.database.read",
            context,
            _read_input(connection, 'SELECT "MiXeD" FROM public."Phase9_Å"'),
        )

        assert isinstance(folded, DatabaseReadOutput)
        assert folded.columns == ["id"]
        assert folded.rows == [["1"]]
        assert isinstance(exact, DatabaseReadOutput)
        assert exact.columns == ["MiXeD"]
        assert exact.rows == [["17"]]
    finally:
        await admin.execute('DROP TABLE IF EXISTS public."Phase9_Å"')


async def test_output_column_cap_accepts_64_and_rejects_65(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
) -> None:
    connection = await make_postgres_connection(role="reader", max_rows=1)
    accepted_sql = "SELECT " + ", ".join(f"{index} AS c{index}" for index in range(64))
    rejected_sql = "SELECT " + ", ".join(f"{index} AS c{index}" for index in range(65))

    output = await _invoke(
        "supabase.database.read",
        context,
        _read_input(connection, accepted_sql),
    )

    assert isinstance(output, DatabaseReadOutput)
    assert output.columns == [f"c{index}" for index in range(64)]
    assert output.rows == [[str(index) for index in range(64)]]
    await _expect_error(
        "supabase.database.read",
        context,
        _read_input(connection, rejected_sql),
        "database_output_type_not_allowed",
    )


async def test_live_unicode_control_column_is_rejected_during_output_inspection(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    hostile_column = "bad\u202ealias"
    await admin.execute(f'CREATE TABLE public.phase9_control_column ("{hostile_column}" integer)')
    await admin.execute("GRANT SELECT ON public.phase9_control_column TO jhin_reader")
    try:
        connection = await make_postgres_connection(role="reader")

        # SELECT * keeps the control character out of submitted SQL. The live
        # asyncpg attribute must still fail closed before it becomes output.
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT * FROM public.phase9_control_column"),
            "database_output_type_not_allowed",
        )
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_control_column")


async def test_direct_external_multibyte_text_is_sliced_without_invalid_utf8(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    original = "🙂abc" * 3_000
    await admin.execute(
        """
        CREATE TABLE public.phase9_utf8_output (id integer, value varchar);
        ALTER TABLE public.phase9_utf8_output ALTER COLUMN value SET STORAGE EXTERNAL;
        GRANT SELECT ON public.phase9_utf8_output TO jhin_reader;
        """
    )
    await admin.execute("INSERT INTO public.phase9_utf8_output VALUES (1, $1)", original)
    try:
        assert (
            await admin.fetchval(
                "SELECT pg_column_compression(value) FROM public.phase9_utf8_output"
            )
            is None
        )
        connection = await make_postgres_connection(role="reader", max_cell_bytes=257)

        output = await _invoke(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT value FROM public.phase9_utf8_output"),
        )

        assert isinstance(output, DatabaseReadOutput)
        assert output.row_count == 1
        assert output.truncated is True
        value = output.rows[0][0]
        assert isinstance(value, str)
        value.encode("utf-8", errors="strict")
        assert value != original
        assert len(value.encode("utf-8")) <= 257
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_utf8_output")


async def test_unbounded_output_types_and_indirect_text_are_rejected(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_output_matrix (
          id integer,
          amount numeric,
          fixed bpchar(8),
          document_json json,
          document jsonb,
          bytes bytea,
          value text
        );
        ALTER TABLE public.phase9_output_matrix ALTER COLUMN value SET STORAGE EXTERNAL;
        INSERT INTO public.phase9_output_matrix
          VALUES (
            1, 1.5, 'fixed', '{"json":true}', '{"jsonb":true}', '\\x0102', 'direct'
          );
        GRANT SELECT ON public.phase9_output_matrix TO jhin_reader;
        """
    )
    try:
        connection = await make_postgres_connection(role="reader")
        for column in ("amount", "fixed", "document_json", "document", "bytes"):
            await _expect_error(
                "supabase.database.read",
                context,
                _read_input(connection, f"SELECT {column} FROM public.phase9_output_matrix"),
                "database_output_type_not_allowed",
            )

        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                "WITH projected AS (SELECT value FROM public.phase9_output_matrix) "
                "SELECT value FROM projected",
            ),
            "database_output_not_safely_sliceable",
        )
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                "SELECT value FROM public.phase9_output_matrix WHERE value = $1",
                ["direct"],
            ),
            "database_output_not_safely_sliceable",
        )
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_output_matrix")


async def test_insert_update_delete_and_truncate_execute_once_with_bounded_counts(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    connection = await make_postgres_connection(role="writer", max_rows=10)

    inserted = await _invoke(
        "supabase.database.write",
        context,
        _mutation_input(
            connection,
            "INSERT INTO public.widgets (id, group_id, name) VALUES ($1, $2, $3)",
            [4, 1, "delta"],
        ),
    )
    assert isinstance(inserted, DatabaseMutationOutput)
    assert inserted.affected_rows == 1

    updated = await _invoke(
        "supabase.database.destructive",
        context,
        _mutation_input(connection, "UPDATE public.widgets SET name = $1 WHERE id = $2", ["D", 4]),
    )
    assert isinstance(updated, DatabaseMutationOutput)
    assert updated.affected_rows == 1

    deleted = await _invoke(
        "supabase.database.destructive",
        context,
        _mutation_input(connection, "DELETE FROM public.widgets WHERE id = $1", [4]),
    )
    assert isinstance(deleted, DatabaseMutationOutput)
    assert deleted.affected_rows == 1
    assert await admin.fetchval("SELECT count(*) FROM public.widgets") == 3

    truncated = await _invoke(
        "supabase.database.destructive",
        context,
        _mutation_input(connection, "TRUNCATE public.widgets"),
    )
    assert isinstance(truncated, DatabaseMutationOutput)
    assert truncated.affected_rows == 3
    assert await admin.fetchval("SELECT count(*) FROM public.widgets") == 0


async def test_current_write_project_and_schema_configuration_is_authoritative(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    disabled = await make_postgres_connection(role="writer", allow_writes=False)
    await _expect_error(
        "supabase.database.write",
        context,
        _mutation_input(
            disabled,
            "INSERT INTO public.widgets (id, group_id, name) VALUES ($1, $2, $3)",
            [4, 1, "disabled"],
        ),
        "database_writes_disabled",
    )

    reader = await make_postgres_connection(role="reader")
    wrong_project = DatabaseReadInput(
        connection_id=str(reader.id),
        project_ref="tsrqponmlkjihgfedcba",
        schema="public",
        sql="SELECT id FROM public.widgets",
        params=[],
    )
    await _expect_error(
        "supabase.database.read",
        context,
        wrong_project,
        "project_scope_mismatch",
    )
    wrong_schema = DatabaseReadInput(
        connection_id=str(reader.id),
        project_ref=PROJECT_REF,
        schema="archive",
        sql="SELECT id FROM public.widgets",
        params=[],
    )
    await _expect_error(
        "supabase.database.read",
        context,
        wrong_schema,
        "schema_scope_mismatch",
    )

    assert await admin.fetchval("SELECT count(*) FROM public.widgets WHERE id = 4") == 0


async def test_target_row_cap_rejects_before_destructive_sql_and_preserves_rows(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    connection = await make_postgres_connection(role="writer", max_rows=2)

    await _expect_error(
        "supabase.database.destructive",
        context,
        _mutation_input(connection, "DELETE FROM public.widgets WHERE id = $1", [1]),
        "database_row_limit_exceeded",
    )

    rows = await admin.fetch("SELECT id, name FROM public.widgets ORDER BY id")
    assert [dict(row) for row in rows] == [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
        {"id": 3, "name": "x" * 20_000},
    ]


async def test_unique_violation_rolls_back_every_row_change(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    connection = await make_postgres_connection(role="writer", max_rows=3)

    await _expect_error(
        "supabase.database.destructive",
        context,
        _mutation_input(connection, "UPDATE public.widgets SET id = $1", [9]),
        "database_execution_failed",
    )

    assert await admin.fetchval("SELECT array_agg(id ORDER BY id) FROM public.widgets") == [1, 2, 3]


async def test_mutation_expansion_budget_accepts_exact_mebibyte_and_rejects_one_more_row(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_mutation_budget (value text);
        ALTER TABLE public.phase9_mutation_budget ALTER COLUMN value SET STORAGE EXTERNAL;
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_mutation_budget TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer", max_rows=200)
        value = "v" * 8_192
        exact_values = ", ".join("($1)" for _ in range(128))
        over_values = ", ".join("($1)" for _ in range(129))

        exact = await _invoke(
            "supabase.database.write",
            context,
            _mutation_input(
                connection,
                f"INSERT INTO public.phase9_mutation_budget (value) VALUES {exact_values}",
                [value],
            ),
        )
        assert isinstance(exact, DatabaseMutationOutput)
        assert exact.affected_rows == 128

        await _expect_error(
            "supabase.database.write",
            context,
            _mutation_input(
                connection,
                f"INSERT INTO public.phase9_mutation_budget (value) VALUES {over_values}",
                [value],
            ),
            "database_mutation_too_large",
        )
        assert await admin.fetchval("SELECT count(*) FROM public.phase9_mutation_budget") == 128
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_mutation_budget")


async def test_update_expansion_budget_accepts_exact_mebibyte_and_rejects_one_more_row(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    value_columns = ", ".join(f"c{index} text" for index in range(64))
    await admin.execute(
        f"""
        CREATE TABLE public.phase9_update_budget (id integer, {value_columns});
        INSERT INTO public.phase9_update_budget (id) VALUES (1), (2);
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_update_budget TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer", max_rows=3)
        value = "u" * 8_192
        assignments = ", ".join(f"c{index} = $1" for index in range(64))
        sql = f"UPDATE public.phase9_update_budget SET {assignments}"
        assert len(value.encode("utf-8")) * 64 * 2 == 1_048_576
        assert len(value.encode("utf-8")) * 64 * 3 > 1_048_576

        exact = await _invoke(
            "supabase.database.destructive",
            context,
            _mutation_input(connection, sql, [value]),
        )
        assert isinstance(exact, DatabaseMutationOutput)
        assert exact.affected_rows == 2
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM public.phase9_update_budget WHERE c0 = $1 AND c63 = $1",
                value,
            )
            == 2
        )

        await admin.execute("INSERT INTO public.phase9_update_budget (id) VALUES (3)")
        await _expect_error(
            "supabase.database.destructive",
            context,
            _mutation_input(connection, sql, [value]),
            "database_mutation_too_large",
        )
        assert (
            await admin.fetchval(
                "SELECT c0 IS NULL AND c63 IS NULL FROM public.phase9_update_budget WHERE id = 3"
            )
            is True
        )
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_update_budget")


async def test_insert_and_update_row_caps_reject_before_any_effect(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_row_caps (id integer, value text);
        ALTER TABLE public.phase9_row_caps ALTER COLUMN value SET STORAGE EXTERNAL;
        INSERT INTO public.phase9_row_caps VALUES (1, 'one'), (2, 'two'), (3, 'three');
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_row_caps TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer", max_rows=2)

        await _expect_error(
            "supabase.database.write",
            context,
            _mutation_input(
                connection,
                "INSERT INTO public.phase9_row_caps (id, value) "
                "VALUES ($1, $2), ($3, $4), ($5, $6)",
                [4, "four", 5, "five", 6, "six"],
            ),
            "database_row_limit_exceeded",
        )
        await _expect_error(
            "supabase.database.destructive",
            context,
            _mutation_input(
                connection,
                "UPDATE public.phase9_row_caps SET value = $1",
                ["changed"],
            ),
            "database_row_limit_exceeded",
        )

        rows = await admin.fetch("SELECT id, value FROM public.phase9_row_caps ORDER BY id")
        assert [tuple(row) for row in rows] == [
            (1, "one"),
            (2, "two"),
            (3, "three"),
        ]
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_row_caps")


async def test_truncate_continue_identity_restrict_succeeds_and_over_cap_is_unchanged(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_truncate_modes (id integer);
        INSERT INTO public.phase9_truncate_modes VALUES (1), (2);
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_truncate_modes TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer", max_rows=2)
        accepted = await _invoke(
            "supabase.database.destructive",
            context,
            _mutation_input(
                connection,
                "TRUNCATE public.phase9_truncate_modes CONTINUE IDENTITY RESTRICT",
            ),
        )
        assert isinstance(accepted, DatabaseMutationOutput)
        assert accepted.affected_rows == 2

        await admin.execute("INSERT INTO public.phase9_truncate_modes VALUES (1), (2), (3)")
        await _expect_error(
            "supabase.database.destructive",
            context,
            _mutation_input(connection, "TRUNCATE public.phase9_truncate_modes"),
            "database_row_limit_exceeded",
        )
        assert await admin.fetchval(
            "SELECT array_agg(id ORDER BY id) FROM public.phase9_truncate_modes"
        ) == [1, 2, 3]
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_truncate_modes")


async def test_unsupported_truncate_shapes_preserve_rows_and_identity_sequence(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_truncate_rejections (
          id integer GENERATED BY DEFAULT AS IDENTITY
        );
        INSERT INTO public.phase9_truncate_rejections VALUES (DEFAULT), (DEFAULT), (DEFAULT);
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_truncate_rejections TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer", max_rows=10)
        before_rows = await admin.fetchval(
            "SELECT array_agg(id ORDER BY id) FROM public.phase9_truncate_rejections"
        )
        before_sequence = await admin.fetchrow(
            "SELECT last_value::bigint, is_called FROM public.phase9_truncate_rejections_id_seq"
        )
        statements = (
            "TRUNCATE public.phase9_truncate_rejections, public.widgets",
            "TRUNCATE ONLY public.phase9_truncate_rejections",
            "TRUNCATE public.phase9_truncate_rejections RESTART IDENTITY",
            "TRUNCATE public.phase9_truncate_rejections CASCADE",
        )

        for sql in statements:
            await _expect_error(
                "supabase.database.destructive",
                context,
                _mutation_input(connection, sql),
                "database_sql_not_allowed",
            )
            assert (
                await admin.fetchval(
                    "SELECT array_agg(id ORDER BY id) FROM public.phase9_truncate_rejections"
                )
                == before_rows
            )
            assert tuple(
                await admin.fetchrow(
                    "SELECT last_value::bigint, is_called "
                    "FROM public.phase9_truncate_rejections_id_seq"
                )
            ) == tuple(before_sequence)
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_truncate_rejections")


async def test_huge_stored_source_mutations_are_rejected_before_dispatch(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_huge_source (id integer, value text);
        CREATE TABLE public.phase9_huge_target (id integer, value text);
        ALTER TABLE public.phase9_huge_source ALTER COLUMN value SET STORAGE EXTERNAL;
        ALTER TABLE public.phase9_huge_target ALTER COLUMN value SET STORAGE EXTERNAL;
        INSERT INTO public.phase9_huge_source VALUES (1, repeat('huge-source-', 200000));
        INSERT INTO public.phase9_huge_target VALUES (1, 'unchanged');
        GRANT SELECT ON public.phase9_huge_source TO jhin_writer;
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_huge_target TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer", max_rows=10)

        await _expect_error(
            "supabase.database.write",
            context,
            _mutation_input(
                connection,
                "INSERT INTO public.phase9_huge_target (id, value) "
                "SELECT id + 1, value FROM public.phase9_huge_source",
            ),
            "database_sql_not_allowed",
        )
        await _expect_error(
            "supabase.database.destructive",
            context,
            _mutation_input(
                connection,
                "UPDATE public.phase9_huge_target AS target SET value = source.value "
                "FROM public.phase9_huge_source AS source WHERE target.id = source.id",
            ),
            "database_sql_not_allowed",
        )

        assert await admin.fetchval(
            "SELECT array_agg(value ORDER BY id) FROM public.phase9_huge_target"
        ) == ["unchanged"]
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_huge_target;
            DROP TABLE IF EXISTS public.phase9_huge_source;
            """
        )


async def test_compressed_legacy_text_is_rejected_without_returning_cell_bytes(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_compressed_output (id integer, value text);
        INSERT INTO public.phase9_compressed_output VALUES (1, repeat('secret-compressed-', 2000));
        ALTER TABLE public.phase9_compressed_output ALTER COLUMN value SET STORAGE EXTERNAL;
        GRANT SELECT ON public.phase9_compressed_output TO jhin_reader;
        """
    )
    try:
        assert (
            await admin.fetchval(
                "SELECT pg_column_compression(value) IS NOT NULL "
                "FROM public.phase9_compressed_output"
            )
            is True
        )
        connection = await make_postgres_connection(role="reader")

        error = await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT value FROM public.phase9_compressed_output"),
            "database_output_not_safely_sliceable",
        )

        assert "secret-compressed" not in str(error)
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_compressed_output")


async def test_view_and_hidden_default_are_rejected_before_side_effects(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE OR REPLACE FUNCTION private.phase9_effect(source text)
        RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
        BEGIN
          INSERT INTO side_effects(source) VALUES ($1);
          RETURN 7;
        END
        $function$;
        REVOKE ALL ON FUNCTION private.phase9_effect(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION private.phase9_effect(text) TO jhin_reader, jhin_writer;
        CREATE VIEW public.phase9_effect_view AS
          SELECT private.phase9_effect('view') AS id;
        GRANT SELECT ON public.phase9_effect_view TO jhin_reader;
        CREATE TABLE public.phase9_default_target (
          id integer PRIMARY KEY,
          hidden integer DEFAULT private.phase9_effect('default')
        );
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_default_target TO jhin_writer;
        """
    )
    try:
        reader = await make_postgres_connection(role="reader")
        writer = await make_postgres_connection(role="writer")

        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(reader, "SELECT id FROM public.phase9_effect_view"),
            "database_relation_not_allowed",
        )
        await _expect_error(
            "supabase.database.write",
            context,
            _mutation_input(
                writer,
                "INSERT INTO public.phase9_default_target (id) VALUES ($1)",
                [1],
            ),
            "database_relation_not_allowed",
        )

        assert await admin.fetchval("SELECT count(*) FROM private.side_effects") == 0
        assert await admin.fetchval("SELECT count(*) FROM public.phase9_default_target") == 0
    finally:
        await admin.execute(
            """
            DROP VIEW IF EXISTS public.phase9_effect_view;
            DROP TABLE IF EXISTS public.phase9_default_target;
            DROP FUNCTION IF EXISTS private.phase9_effect(text);
            """
        )


async def test_non_heap_inherited_and_partition_relations_are_rejected(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE VIEW public.phase9_shape_view AS SELECT id FROM public.widgets;
        CREATE MATERIALIZED VIEW public.phase9_shape_matview AS
          SELECT id FROM public.widgets;
        CREATE TABLE public.phase9_partition_parent (id integer) PARTITION BY RANGE (id);
        CREATE TABLE public.phase9_partition_child
          PARTITION OF public.phase9_partition_parent FOR VALUES FROM (0) TO (10);
        CREATE TABLE public.phase9_inherit_parent (id integer);
        CREATE TABLE public.phase9_inherit_child () INHERITS (public.phase9_inherit_parent);
        GRANT SELECT ON public.phase9_shape_view, public.phase9_shape_matview,
          public.phase9_partition_parent, public.phase9_partition_child,
          public.phase9_inherit_parent, public.phase9_inherit_child TO jhin_reader;
        """
    )
    try:
        connection = await make_postgres_connection(role="reader")
        for relation in (
            "phase9_shape_view",
            "phase9_shape_matview",
            "phase9_partition_parent",
            "phase9_partition_child",
            "phase9_inherit_parent",
            "phase9_inherit_child",
        ):
            await _expect_error(
                "supabase.database.read",
                context,
                _read_input(connection, f"SELECT id FROM public.{relation}"),
                "database_relation_not_allowed",
            )
    finally:
        await admin.execute(
            """
            DROP VIEW IF EXISTS public.phase9_shape_view;
            DROP MATERIALIZED VIEW IF EXISTS public.phase9_shape_matview;
            DROP TABLE IF EXISTS public.phase9_partition_parent CASCADE;
            DROP TABLE IF EXISTS public.phase9_inherit_parent CASCADE;
            """
        )


async def test_foreign_table_is_rejected_before_read(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    extension_existed = await admin.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'file_fdw')"
    )
    try:
        await admin.execute(
            """
            CREATE EXTENSION IF NOT EXISTS file_fdw;
            CREATE SERVER phase9_file_server FOREIGN DATA WRAPPER file_fdw;
            CREATE FOREIGN TABLE public.phase9_foreign_table (id integer)
              SERVER phase9_file_server
              OPTIONS (filename '/dev/null', format 'csv');
            GRANT SELECT ON public.phase9_foreign_table TO jhin_reader;
            """
        )
        connection = await make_postgres_connection(role="reader")

        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.phase9_foreign_table"),
            "database_relation_not_allowed",
        )
    finally:
        await admin.execute("DROP SERVER IF EXISTS phase9_file_server CASCADE")
        if not extension_existed:
            await admin.execute("DROP EXTENSION IF EXISTS file_fdw")


async def test_custom_types_collation_rls_and_unsafe_indexes_are_rejected(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TYPE public.phase9_pair AS (left_value integer, right_value integer);
        CREATE DOMAIN public.phase9_positive AS integer CHECK (VALUE > 0);
        CREATE COLLATION public.phase9_custom_collation FROM "C";
        CREATE TABLE public.phase9_custom_type (id integer, payload public.phase9_pair);
        CREATE TABLE public.phase9_domain_type (id integer, payload public.phase9_positive);
        CREATE TABLE public.phase9_array_type (id integer, payload integer[]);
        CREATE TABLE public.phase9_custom_collation (
          id integer,
          label text COLLATE public.phase9_custom_collation
        );
        CREATE TABLE public.phase9_rls (id integer);
        ALTER TABLE public.phase9_rls ENABLE ROW LEVEL SECURITY;
        CREATE POLICY phase9_rls_policy ON public.phase9_rls USING (true);
        CREATE TABLE public.phase9_expression_index (id integer);
        CREATE INDEX phase9_expression_idx ON public.phase9_expression_index ((id + 1));
        CREATE TABLE public.phase9_partial_index (id integer);
        CREATE INDEX phase9_partial_idx ON public.phase9_partial_index (id) WHERE id > 0;
        CREATE TABLE public.phase9_include_index (id integer, label integer);
        CREATE INDEX phase9_include_idx ON public.phase9_include_index (id) INCLUDE (label);
        GRANT SELECT ON public.phase9_custom_type, public.phase9_domain_type,
          public.phase9_array_type, public.phase9_custom_collation, public.phase9_rls,
          public.phase9_expression_index, public.phase9_partial_index,
          public.phase9_include_index TO jhin_reader;
        """
    )
    try:
        connection = await make_postgres_connection(role="reader")
        for relation in (
            "phase9_custom_type",
            "phase9_domain_type",
            "phase9_array_type",
            "phase9_custom_collation",
            "phase9_rls",
            "phase9_expression_index",
            "phase9_partial_index",
            "phase9_include_index",
        ):
            await _expect_error(
                "supabase.database.read",
                context,
                _read_input(connection, f"SELECT id FROM public.{relation}"),
                "database_relation_not_allowed",
            )
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_custom_type;
            DROP TABLE IF EXISTS public.phase9_domain_type;
            DROP TABLE IF EXISTS public.phase9_array_type;
            DROP TABLE IF EXISTS public.phase9_custom_collation;
            DROP TABLE IF EXISTS public.phase9_rls;
            DROP TABLE IF EXISTS public.phase9_expression_index;
            DROP TABLE IF EXISTS public.phase9_partial_index;
            DROP TABLE IF EXISTS public.phase9_include_index;
            DROP COLLATION IF EXISTS public.phase9_custom_collation;
            DROP DOMAIN IF EXISTS public.phase9_positive;
            DROP TYPE IF EXISTS public.phase9_pair;
            """
        )


async def test_custom_btree_operator_class_is_rejected_before_read(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    try:
        await admin.execute(
            """
            CREATE OPERATOR CLASS public.phase9_custom_int4_ops
              FOR TYPE integer USING btree AS
              OPERATOR 1 pg_catalog.< (integer, integer),
              OPERATOR 2 pg_catalog.<= (integer, integer),
              OPERATOR 3 pg_catalog.= (integer, integer),
              OPERATOR 4 pg_catalog.>= (integer, integer),
              OPERATOR 5 pg_catalog.> (integer, integer),
              FUNCTION 1 pg_catalog.btint4cmp(integer, integer);
            CREATE TABLE public.phase9_custom_opclass_table (id integer);
            CREATE INDEX phase9_custom_opclass_idx
              ON public.phase9_custom_opclass_table
              USING btree (id public.phase9_custom_int4_ops);
            GRANT SELECT ON public.phase9_custom_opclass_table TO jhin_reader;
            """
        )
        connection = await make_postgres_connection(role="reader")

        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.phase9_custom_opclass_table"),
            "database_relation_not_allowed",
        )
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_custom_opclass_table;
            DROP OPERATOR CLASS IF EXISTS public.phase9_custom_int4_ops USING btree;
            """
        )


async def test_invalid_concurrent_index_is_rejected_before_read(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    try:
        await admin.execute(
            """
            CREATE TABLE public.phase9_invalid_index_table (id integer);
            INSERT INTO public.phase9_invalid_index_table VALUES (1), (1);
            GRANT SELECT ON public.phase9_invalid_index_table TO jhin_reader;
            """
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await admin.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY phase9_invalid_index "
                "ON public.phase9_invalid_index_table(id)"
            )
        assert (
            await admin.fetchval(
                "SELECT indisvalid FROM pg_catalog.pg_index "
                "WHERE indexrelid = 'public.phase9_invalid_index'::pg_catalog.regclass"
            )
            is False
        )
        connection = await make_postgres_connection(role="reader")

        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.phase9_invalid_index_table"),
            "database_relation_not_allowed",
        )
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_invalid_index_table")


async def test_security_definer_rls_policy_is_rejected_without_side_effects(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    try:
        await admin.execute(
            """
            CREATE OR REPLACE FUNCTION private.phase9_rls_effect(value integer)
            RETURNS boolean
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog, private
            AS $function$
            BEGIN
              INSERT INTO side_effects(source) VALUES ('rls-policy');
              RETURN true;
            END
            $function$;
            REVOKE ALL ON FUNCTION private.phase9_rls_effect(integer) FROM PUBLIC;
            GRANT USAGE ON SCHEMA private TO jhin_reader;
            GRANT EXECUTE ON FUNCTION private.phase9_rls_effect(integer) TO jhin_reader;
            CREATE TABLE public.phase9_rls_effect_table (id integer);
            INSERT INTO public.phase9_rls_effect_table VALUES (1);
            ALTER TABLE public.phase9_rls_effect_table ENABLE ROW LEVEL SECURITY;
            CREATE POLICY phase9_rls_effect_policy ON public.phase9_rls_effect_table
              FOR SELECT TO jhin_reader
              USING (private.phase9_rls_effect(id));
            GRANT SELECT ON public.phase9_rls_effect_table TO jhin_reader;
            """
        )
        connection = await make_postgres_connection(role="reader")

        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.phase9_rls_effect_table"),
            "database_relation_not_allowed",
        )
        assert await admin.fetchval("SELECT count(*) FROM private.side_effects") == 0
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_rls_effect_table;
            DROP FUNCTION IF EXISTS private.phase9_rls_effect(integer);
            REVOKE USAGE ON SCHEMA private FROM jhin_reader;
            """
        )


async def test_generated_check_trigger_and_rule_code_never_runs(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;
        CREATE SEQUENCE private.phase9_expression_sequence;
        CREATE OR REPLACE FUNCTION private.phase9_immutable_effect(value integer)
        RETURNS integer
        LANGUAGE sql
        IMMUTABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
          SELECT pg_catalog.nextval('private.phase9_expression_sequence'::pg_catalog.regclass)
            ::integer + $1
        $function$;
        CREATE OR REPLACE FUNCTION private.phase9_check_effect(value integer)
        RETURNS boolean
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
        BEGIN
          INSERT INTO side_effects(source) VALUES ('check');
          RETURN true;
        END
        $function$;
        CREATE OR REPLACE FUNCTION private.phase9_trigger_effect()
        RETURNS trigger
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
        BEGIN
          INSERT INTO side_effects(source) VALUES ('trigger');
          RETURN NEW;
        END
        $function$;
        REVOKE ALL ON FUNCTION private.phase9_check_effect(integer) FROM PUBLIC;
        REVOKE ALL ON FUNCTION private.phase9_trigger_effect() FROM PUBLIC;
        REVOKE ALL ON FUNCTION private.phase9_immutable_effect(integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION private.phase9_check_effect(integer) TO jhin_writer;
        GRANT EXECUTE ON FUNCTION private.phase9_trigger_effect() TO jhin_writer;
        GRANT EXECUTE ON FUNCTION private.phase9_immutable_effect(integer) TO jhin_writer;

        CREATE TABLE public.phase9_generated_target (
          id integer,
          generated integer GENERATED ALWAYS AS (private.phase9_immutable_effect(id)) STORED
        );
        CREATE TABLE public.phase9_check_target (
          id integer CHECK (private.phase9_check_effect(id))
        );
        CREATE TABLE public.phase9_trigger_target (id integer);
        CREATE TRIGGER phase9_trigger
          BEFORE INSERT ON public.phase9_trigger_target
          FOR EACH ROW EXECUTE FUNCTION private.phase9_trigger_effect();
        CREATE TABLE public.phase9_rule_target (id integer);
        CREATE RULE phase9_rule AS ON INSERT TO public.phase9_rule_target
          DO ALSO INSERT INTO private.side_effects(source) VALUES ('rule');
        CREATE TABLE public.phase9_exclusion_target (id integer);
        ALTER TABLE public.phase9_exclusion_target
          ADD CONSTRAINT phase9_exclusion
          EXCLUDE USING gist ((private.phase9_immutable_effect(id)) WITH =);

        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_generated_target, public.phase9_check_target,
          public.phase9_trigger_target, public.phase9_rule_target,
          public.phase9_exclusion_target TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer")
        for relation in (
            "phase9_generated_target",
            "phase9_check_target",
            "phase9_trigger_target",
            "phase9_rule_target",
            "phase9_exclusion_target",
        ):
            await _expect_error(
                "supabase.database.write",
                context,
                _mutation_input(
                    connection,
                    f"INSERT INTO public.{relation} (id) VALUES ($1)",
                    [1],
                ),
                "database_relation_not_allowed",
            )

        assert await admin.fetchval("SELECT count(*) FROM private.side_effects") == 0
        sequence_state = await admin.fetchrow(
            "SELECT last_value, is_called FROM private.phase9_expression_sequence"
        )
        assert dict(sequence_state) == {"last_value": 1, "is_called": False}
        for relation in (
            "phase9_generated_target",
            "phase9_check_target",
            "phase9_trigger_target",
            "phase9_rule_target",
            "phase9_exclusion_target",
        ):
            assert await admin.fetchval(f"SELECT count(*) FROM public.{relation}") == 0
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_generated_target;
            DROP TABLE IF EXISTS public.phase9_check_target;
            DROP TABLE IF EXISTS public.phase9_trigger_target;
            DROP TABLE IF EXISTS public.phase9_rule_target;
            DROP TABLE IF EXISTS public.phase9_exclusion_target;
            DROP FUNCTION IF EXISTS private.phase9_check_effect(integer);
            DROP FUNCTION IF EXISTS private.phase9_trigger_effect();
            DROP FUNCTION IF EXISTS private.phase9_immutable_effect(integer);
            DROP SEQUENCE IF EXISTS private.phase9_expression_sequence;
            DROP EXTENSION IF EXISTS btree_gist;
            """
        )


async def test_unsafe_fk_actions_and_missing_peer_lock_privilege_fail_before_mutation(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_cascade_parent (id integer PRIMARY KEY);
        CREATE TABLE public.phase9_cascade_child (
          id integer PRIMARY KEY,
          parent_id integer NOT NULL REFERENCES public.phase9_cascade_parent(id)
            ON UPDATE NO ACTION ON DELETE CASCADE NOT DEFERRABLE,
          value integer
        );
        CREATE INDEX phase9_cascade_child_parent_idx
          ON public.phase9_cascade_child(parent_id);
        INSERT INTO public.phase9_cascade_parent VALUES (1);
        INSERT INTO public.phase9_cascade_child VALUES (1, 1, 0);
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_cascade_child TO jhin_writer;
        GRANT SELECT, MAINTAIN ON public.phase9_cascade_parent TO jhin_writer;

        CREATE TABLE private.phase9_cross_parent (id integer PRIMARY KEY);
        CREATE TABLE public.phase9_cross_child (
          id integer PRIMARY KEY,
          parent_id integer NOT NULL REFERENCES private.phase9_cross_parent(id)
            ON UPDATE NO ACTION ON DELETE NO ACTION NOT DEFERRABLE,
          value integer
        );
        CREATE INDEX phase9_cross_child_parent_idx ON public.phase9_cross_child(parent_id);
        INSERT INTO private.phase9_cross_parent VALUES (1);
        INSERT INTO public.phase9_cross_child VALUES (1, 1, 0);
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_cross_child TO jhin_writer;
        GRANT MAINTAIN ON private.phase9_cross_parent TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer")
        for relation in ("phase9_cascade_child", "phase9_cross_child"):
            await _expect_error(
                "supabase.database.destructive",
                context,
                _mutation_input(
                    connection,
                    f"UPDATE public.{relation} SET value = $1 WHERE id = $2",
                    [9, 1],
                ),
                "database_relation_not_allowed",
            )
            assert await admin.fetchval(f"SELECT value FROM public.{relation}") == 0

        await admin.execute("REVOKE MAINTAIN ON public.widget_groups FROM jhin_writer")
        await _expect_error(
            "supabase.database.destructive",
            context,
            _mutation_input(
                connection,
                "UPDATE public.widgets SET name = $1 WHERE id = $2",
                ["x", 1],
            ),
            "database_relation_not_allowed",
        )
        assert await admin.fetchval("SELECT name FROM public.widgets WHERE id = 1") == "alpha"
    finally:
        await admin.execute(
            """
            GRANT MAINTAIN ON public.widget_groups TO jhin_writer;
            DROP TABLE IF EXISTS public.phase9_cascade_child;
            DROP TABLE IF EXISTS public.phase9_cascade_parent;
            DROP TABLE IF EXISTS public.phase9_cross_child;
            DROP TABLE IF EXISTS private.phase9_cross_parent;
            """
        )


@pytest.mark.parametrize(
    ("suffix", "constraint_clause"),
    [
        pytest.param(
            "set_null",
            "ON UPDATE NO ACTION ON DELETE SET NULL NOT DEFERRABLE",
            id="set-null",
        ),
        pytest.param(
            "set_default",
            "ON UPDATE SET DEFAULT ON DELETE NO ACTION NOT DEFERRABLE",
            id="set-default",
        ),
        pytest.param(
            "deferrable",
            "ON UPDATE NO ACTION ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE",
            id="deferrable",
        ),
    ],
)
async def test_unsafe_fk_action_or_deferrability_is_rejected_before_mutation(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
    suffix: str,
    constraint_clause: str,
) -> None:
    parent = f"phase9_{suffix}_parent"
    child = f"phase9_{suffix}_child"
    try:
        await admin.execute(f"CREATE TABLE public.{parent} (id integer PRIMARY KEY)")
        await admin.execute(
            f"CREATE TABLE public.{child} ("
            "id integer PRIMARY KEY, "
            f"parent_id integer REFERENCES public.{parent}(id) {constraint_clause}, "
            "value integer NOT NULL)"
        )
        await admin.execute(f"CREATE INDEX {child}_parent_idx ON public.{child}(parent_id)")
        await admin.execute(f"INSERT INTO public.{parent} VALUES (1)")
        await admin.execute(f"INSERT INTO public.{child} VALUES (1, 1, 0)")
        await admin.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public.{child} TO jhin_writer"
        )
        await admin.execute(f"GRANT SELECT, MAINTAIN ON public.{parent} TO jhin_writer")
        connection = await make_postgres_connection(role="writer")

        await _expect_error(
            "supabase.database.destructive",
            context,
            _mutation_input(
                connection,
                f"UPDATE public.{child} SET value = $1 WHERE id = $2",
                [9, 1],
            ),
            "database_relation_not_allowed",
        )
        assert await admin.fetchval(f"SELECT value FROM public.{child}") == 0
    finally:
        await admin.execute(f"DROP TABLE IF EXISTS public.{child}")
        await admin.execute(f"DROP TABLE IF EXISTS public.{parent}")


async def test_safe_restrict_fk_allows_bounded_mutation(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    try:
        await admin.execute(
            """
            CREATE TABLE public.phase9_restrict_parent (id integer PRIMARY KEY);
            CREATE TABLE public.phase9_restrict_child (
              id integer PRIMARY KEY,
              parent_id integer NOT NULL REFERENCES public.phase9_restrict_parent(id)
                ON UPDATE RESTRICT ON DELETE RESTRICT NOT DEFERRABLE,
              value integer NOT NULL
            );
            CREATE INDEX phase9_restrict_child_parent_idx
              ON public.phase9_restrict_child(parent_id);
            INSERT INTO public.phase9_restrict_parent VALUES (1);
            INSERT INTO public.phase9_restrict_child VALUES (1, 1, 0);
            GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
              ON public.phase9_restrict_child TO jhin_writer;
            GRANT SELECT, MAINTAIN ON public.phase9_restrict_parent TO jhin_writer;
            """
        )
        connection = await make_postgres_connection(role="writer", max_rows=1)

        output = await _invoke(
            "supabase.database.destructive",
            context,
            _mutation_input(
                connection,
                "UPDATE public.phase9_restrict_child SET value = $1 WHERE id = $2",
                [9, 1],
            ),
        )

        assert output == DatabaseMutationOutput(affected_rows=1)
        assert await admin.fetchval("SELECT value FROM public.phase9_restrict_child") == 9
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_restrict_child;
            DROP TABLE IF EXISTS public.phase9_restrict_parent;
            """
        )


async def test_truncate_rejects_external_inbound_fk_without_changing_rows(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_truncate_parent (id integer PRIMARY KEY);
        CREATE TABLE public.phase9_truncate_external_child (
          id integer PRIMARY KEY,
          parent_id integer NOT NULL REFERENCES public.phase9_truncate_parent(id)
            ON UPDATE NO ACTION ON DELETE NO ACTION NOT DEFERRABLE
        );
        INSERT INTO public.phase9_truncate_parent VALUES (1);
        INSERT INTO public.phase9_truncate_external_child VALUES (1, 1);
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
          ON public.phase9_truncate_parent TO jhin_writer;
        """
    )
    try:
        connection = await make_postgres_connection(role="writer", max_rows=10)

        await _expect_error(
            "supabase.database.destructive",
            context,
            _mutation_input(connection, "TRUNCATE public.phase9_truncate_parent"),
            "database_relation_not_allowed",
        )

        assert await admin.fetchval("SELECT count(*) FROM public.phase9_truncate_parent") == 1
        assert (
            await admin.fetchval("SELECT count(*) FROM public.phase9_truncate_external_child") == 1
        )
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_truncate_external_child;
            DROP TABLE IF EXISTS public.phase9_truncate_parent;
            """
        )


async def test_fk_closure_accepts_32_locked_relations_and_rejects_33(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await _create_fk_chain(admin, "phase9_fk32", 32)
    await _create_fk_chain(admin, "phase9_fk33", 33)
    try:
        connection = await make_postgres_connection(role="writer")
        accepted = await _invoke(
            "supabase.database.destructive",
            context,
            _mutation_input(connection, "UPDATE public.phase9_fk32_00 SET id = $1", [1]),
        )
        assert isinstance(accepted, DatabaseMutationOutput)
        assert accepted.affected_rows == 0

        await _expect_error(
            "supabase.database.destructive",
            context,
            _mutation_input(connection, "UPDATE public.phase9_fk33_00 SET id = $1", [1]),
            "database_relation_not_allowed",
        )
        assert await admin.fetchval("SELECT count(*) FROM public.phase9_fk33_00") == 0
    finally:
        await _drop_fk_chain(admin, "phase9_fk32", 32)
        await _drop_fk_chain(admin, "phase9_fk33", 33)


async def test_live_role_and_relation_ownership_drift_fail_closed_without_markers(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    connection = await make_postgres_connection(role="reader")
    sql_marker = "ownership-sql-marker"
    try:
        await admin.execute("ALTER ROLE jhin_reader CREATEROLE")
        error = await _expect_error(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                f'SELECT id AS "{sql_marker}" FROM public.widgets',
            ),
            "database_role_not_least_privilege",
        )
        assert sql_marker not in str(error)
        assert "jhin_reader" not in str(error)
    finally:
        await admin.execute("ALTER ROLE jhin_reader NOCREATEROLE")

    try:
        await admin.execute("ALTER TABLE public.widgets OWNER TO jhin_reader")
        error = await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets"),
            "database_role_not_least_privilege",
        )
        assert "widgets" not in str(error)
    finally:
        await admin.execute("ALTER TABLE public.widgets OWNER TO postgres")


async def test_recursive_role_closure_accepts_64_and_rejects_65_reachable_roles(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    role_names = [f"phase9_role_cap_{index:02d}" for index in range(1, 65)]
    for role in role_names:
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.execute(
            f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS"
        )
    try:
        await admin.execute(f"GRANT {role_names[0]} TO jhin_reader")
        for index in range(1, 63):
            await admin.execute(f"GRANT {role_names[index]} TO {role_names[index - 1]}")

        connection = await make_postgres_connection(role="reader")
        accepted = await _invoke(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets WHERE id = 1"),
        )
        assert isinstance(accepted, DatabaseReadOutput)
        assert accepted.rows == [["1"]]

        await admin.execute(f"GRANT {role_names[63]} TO {role_names[62]}")
        error = await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets WHERE id = 1"),
            "database_role_not_least_privilege",
        )
        assert "phase9_role_cap" not in str(error)
    finally:
        await admin.execute(f"REVOKE {role_names[0]} FROM jhin_reader")
        for role in reversed(role_names):
            await admin.execute(f"DROP ROLE IF EXISTS {role}")


async def test_deep_inherited_privilege_and_database_schema_relation_owners_are_rejected(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute("DROP ROLE IF EXISTS phase9_owner_ancestor")
    await admin.execute("DROP ROLE IF EXISTS phase9_middle_ancestor")
    await admin.execute(
        """
        CREATE ROLE phase9_owner_ancestor NOLOGIN
          NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        CREATE ROLE phase9_middle_ancestor NOLOGIN
          NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        GRANT phase9_owner_ancestor TO phase9_middle_ancestor;
        GRANT phase9_middle_ancestor TO jhin_reader;
        """
    )
    connection = await make_postgres_connection(role="reader")
    try:
        await admin.execute("GRANT pg_read_all_data TO phase9_owner_ancestor")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets"),
            "database_role_not_least_privilege",
        )
        await admin.execute("REVOKE pg_read_all_data FROM phase9_owner_ancestor")

        await admin.execute("ALTER TABLE public.widgets OWNER TO phase9_owner_ancestor")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets"),
            "database_role_not_least_privilege",
        )
        await admin.execute("ALTER TABLE public.widgets OWNER TO postgres")

        await admin.execute("ALTER SCHEMA public OWNER TO phase9_owner_ancestor")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets"),
            "database_role_not_least_privilege",
        )
        await admin.execute("ALTER SCHEMA public OWNER TO pg_database_owner")

        await admin.execute("ALTER ROLE phase9_owner_ancestor CREATEDB")
        await admin.execute("ALTER DATABASE supabase_fixture OWNER TO phase9_owner_ancestor")
        await admin.execute("ALTER ROLE phase9_owner_ancestor NOCREATEDB")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets"),
            "database_role_not_least_privilege",
        )
        await admin.execute("ALTER DATABASE supabase_fixture OWNER TO postgres")
    finally:
        await admin.execute(
            """
            ALTER DATABASE supabase_fixture OWNER TO postgres;
            ALTER SCHEMA public OWNER TO pg_database_owner;
            ALTER TABLE public.widgets OWNER TO postgres;
            REVOKE pg_read_all_data FROM phase9_owner_ancestor;
            REVOKE phase9_middle_ancestor FROM jhin_reader;
            DROP ROLE IF EXISTS phase9_middle_ancestor;
            DROP ROLE IF EXISTS phase9_owner_ancestor;
            """
        )


async def test_replication_mode_is_rechecked_and_never_silently_overwritten(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    database_dsns: DatabaseDsns,
    admin: Any,
) -> None:
    await admin.execute("ALTER ROLE jhin_reader SET session_replication_role TO replica")
    try:
        control = await asyncpg.connect(
            dsn=database_dsns.reader,
            timeout=5,
            statement_cache_size=0,
        )
        try:
            assert await control.fetchval("SHOW session_replication_role") == "replica"
        finally:
            await control.close()

        connection = await make_postgres_connection(role="reader")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets"),
            "database_role_not_least_privilege",
        )
    finally:
        await admin.execute("ALTER ROLE jhin_reader RESET session_replication_role")


async def test_lock_timeout_is_bounded_and_does_not_run_submitted_sql(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    connection = await make_postgres_connection(
        role="reader",
        statement_timeout_ms=2_000,
        lock_timeout_ms=100,
    )
    await admin.execute("BEGIN")
    await admin.execute("LOCK TABLE public.widgets IN ACCESS EXCLUSIVE MODE")
    started = time.monotonic()
    try:
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets"),
            "database_timeout",
        )
    finally:
        await admin.execute("ROLLBACK")

    assert time.monotonic() - started < 3


async def test_statement_timeout_cancels_bounded_work_without_provider_text(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_timeout_input (id integer);
        INSERT INTO public.phase9_timeout_input SELECT generate_series(1, 5000);
        GRANT SELECT ON public.phase9_timeout_input TO jhin_reader;
        """
    )
    try:
        connection = await make_postgres_connection(
            role="reader",
            max_rows=1,
            statement_timeout_ms=250,
            lock_timeout_ms=100,
        )
        sql_marker = "statement-timeout-marker"
        started = time.monotonic()
        error = await _expect_error(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                'SELECT a.id AS "statement-timeout-marker" '
                "FROM public.phase9_timeout_input AS a "
                "CROSS JOIN public.phase9_timeout_input AS b "
                "WHERE a.id + b.id < 0",
            ),
            "database_timeout",
        )
        assert time.monotonic() - started < 5
        assert sql_marker not in str(error)
    finally:
        await admin.execute("DROP TABLE IF EXISTS public.phase9_timeout_input")


async def test_executor_overrides_index_favoring_role_defaults_with_sequential_planning(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_planner_input (id integer);
        INSERT INTO public.phase9_planner_input SELECT generate_series(1, 1000);
        CREATE INDEX phase9_planner_idx ON public.phase9_planner_input(id);
        GRANT SELECT ON public.phase9_planner_input TO jhin_reader;
        ALTER ROLE jhin_reader SET enable_seqscan TO off;
        ALTER ROLE jhin_reader SET enable_indexscan TO on;
        ALTER ROLE jhin_reader SET enable_indexonlyscan TO on;
        ALTER ROLE jhin_reader SET enable_bitmapscan TO on;
        SELECT pg_stat_reset_single_table_counters('public.phase9_planner_input'::regclass);
        """
    )
    try:
        connection = await make_postgres_connection(role="reader")
        output = await _invoke(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.phase9_planner_input WHERE id = 999"),
        )
        assert isinstance(output, DatabaseReadOutput)
        assert output.rows == [["999"]]

        index_scans = await admin.fetchval(
            "SELECT idx_scan FROM pg_stat_user_indexes WHERE indexrelname = 'phase9_planner_idx'"
        )
        sequential_scans = await admin.fetchval(
            "SELECT seq_scan FROM pg_stat_user_tables WHERE relname = 'phase9_planner_input'"
        )
        assert index_scans == 0
        assert sequential_scans >= 1
    finally:
        await admin.execute(
            """
            ALTER ROLE jhin_reader RESET enable_seqscan;
            ALTER ROLE jhin_reader RESET enable_indexscan;
            ALTER ROLE jhin_reader RESET enable_indexonlyscan;
            ALTER ROLE jhin_reader RESET enable_bitmapscan;
            DROP TABLE IF EXISTS public.phase9_planner_input;
            """
        )


async def test_live_transaction_settings_read_back_exactly_and_disable_parallelism(
    database_dsns: DatabaseDsns,
) -> None:
    connection = await asyncpg.connect(
        dsn=database_dsns.reader,
        timeout=5,
        statement_cache_size=0,
    )
    transaction = connection.transaction(readonly=True)
    await transaction.start()
    try:
        config = database_tools._execution_config(
            {
                "project_ref": PROJECT_REF,
                "allowed_schemas": ["public"],
                "allow_writes": False,
                "statement_timeout_ms": 5_000,
                "lock_timeout_ms": 1_000,
                "max_rows": 200,
                "max_cell_bytes": 4_096,
                "max_result_bytes": 24_000,
            }
        )
        await database_tools._apply_transaction_settings(connection, config)
        names = [
            "statement_timeout",
            "lock_timeout",
            "idle_in_transaction_session_timeout",
            "search_path",
            "standard_conforming_strings",
            "row_security",
            "work_mem",
            "hash_mem_multiplier",
            "temp_file_limit",
            "max_parallel_workers_per_gather",
            "jit",
            "enable_seqscan",
            "enable_indexscan",
            "enable_indexonlyscan",
            "enable_bitmapscan",
        ]
        rows = await connection.fetch(
            "SELECT name::text, setting::text, unit::text "
            "FROM pg_catalog.pg_settings WHERE name = ANY($1::text[]) ORDER BY name",
            names,
        )
        actual = {row["name"]: (row["setting"], row["unit"]) for row in rows}

        assert actual == {
            "enable_bitmapscan": ("off", None),
            "enable_indexonlyscan": ("off", None),
            "enable_indexscan": ("off", None),
            "enable_seqscan": ("on", None),
            "hash_mem_multiplier": ("1", None),
            "idle_in_transaction_session_timeout": ("16000", "ms"),
            "jit": ("off", None),
            "lock_timeout": ("1000", "ms"),
            "max_parallel_workers_per_gather": ("0", None),
            "row_security": ("on", None),
            "search_path": ("pg_catalog", None),
            "standard_conforming_strings": ("on", None),
            "statement_timeout": ("5000", "ms"),
            "temp_file_limit": ("16384", "kB"),
            "work_mem": ("1024", "kB"),
        }
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.current_setting('max_parallel_workers_per_gather')"
            )
            == "0"
        )
    finally:
        await transaction.rollback()
        await asyncio.wait_for(connection.close(), timeout=2)


async def test_temp_file_limit_permission_failure_is_credential_safe(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute("REVOKE SET ON PARAMETER temp_file_limit FROM jhin_reader")
    try:
        connection = await make_postgres_connection(role="reader")

        error = await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets WHERE id = 1"),
            "database_role_not_least_privilege",
        )

        assert str(error) == "Supabase database role is not least privilege"
    finally:
        await admin.execute("GRANT SET ON PARAMETER temp_file_limit TO jhin_reader")


async def test_work_mem_spill_succeeds_below_temp_limit_and_cap_cancels_above(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    database_dsns: DatabaseDsns,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_temp_spill AS
        SELECT
          value::integer AS id,
          pg_catalog.repeat(pg_catalog.md5(value::text), 32)::char(1024) AS payload
        FROM pg_catalog.generate_series(1, 50000) AS value;
        GRANT SELECT ON public.phase9_temp_spill TO jhin_reader;
        """
    )
    probe = await asyncpg.connect(
        dsn=database_dsns.reader,
        timeout=5,
        statement_cache_size=0,
    )
    try:
        transaction = probe.transaction(readonly=True)
        await transaction.start()
        await probe.execute("SET LOCAL work_mem = '1MB'")
        await probe.execute("SET LOCAL temp_file_limit = '16MB'")
        plan = await probe.fetchval(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT id
            FROM public.phase9_temp_spill
            WHERE id <= 5000
            GROUP BY id, payload
            """
        )
        await transaction.rollback()
        decoded_plan = json.loads(plan) if isinstance(plan, str) else plan

        def temp_blocks(node: object) -> int:
            if isinstance(node, dict):
                own = node.get("Temp Written Blocks", 0)
                return (own if isinstance(own, int) else 0) + sum(
                    temp_blocks(value) for value in node.values()
                )
            if isinstance(node, list):
                return sum(temp_blocks(value) for value in node)
            return 0

        assert temp_blocks(decoded_plan) > 0

        connection = await make_postgres_connection(
            role="reader",
            max_rows=10,
            statement_timeout_ms=30_000,
        )
        under_cap = await _invoke(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                "SELECT id FROM public.phase9_temp_spill WHERE id <= 5000 GROUP BY id, payload",
            ),
        )
        assert isinstance(under_cap, DatabaseReadOutput)
        assert under_cap.row_count == 10
        assert under_cap.truncated is True

        over_cap_error = await _expect_error(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                "SELECT id FROM public.phase9_temp_spill GROUP BY id, payload",
            ),
            "database_execution_failed",
        )
        assert str(over_cap_error) == "Supabase database execution failed"
    finally:
        await asyncio.wait_for(probe.close(), timeout=2)
        await admin.execute("DROP TABLE IF EXISTS public.phase9_temp_spill")


async def test_external_cancellation_rolls_back_closes_and_propagates(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    connection = await make_postgres_connection(
        role="reader",
        statement_timeout_ms=5_000,
        lock_timeout_ms=5_000,
    )
    await admin.execute("BEGIN")
    await admin.execute("LOCK TABLE public.widgets IN ACCESS EXCLUSIVE MODE")
    task = asyncio.create_task(
        _invoke(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.widgets"),
        )
    )
    try:
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await admin.execute("ROLLBACK")

    for _ in range(20):
        live_connections = await admin.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'jhin_reader'"
        )
        if live_connections == 0:
            break
        await asyncio.sleep(0.05)
    assert live_connections == 0


async def test_mutation_relation_lock_blocks_concurrent_effect_bearing_ddl(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    database_dsns: DatabaseDsns,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE FUNCTION private.phase9_race_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        BEGIN
          RETURN NEW;
        END
        $function$;
        """
    )
    connection = await make_postgres_connection(
        role="writer",
        statement_timeout_ms=5_000,
        lock_timeout_ms=5_000,
    )
    observer = await asyncpg.connect(
        dsn=database_dsns.admin,
        timeout=5,
        statement_cache_size=0,
    )
    ddl_connection = await asyncpg.connect(
        dsn=database_dsns.admin,
        timeout=5,
        statement_cache_size=0,
    )
    await admin.execute("BEGIN")
    await admin.execute("SELECT id FROM public.widgets WHERE id = 1 FOR UPDATE")
    mutation = asyncio.create_task(
        _invoke(
            "supabase.database.destructive",
            context,
            _mutation_input(
                connection,
                "UPDATE public.widgets SET name = $1 WHERE id = $2",
                ["raced", 1],
            ),
        )
    )
    try:
        lock_observed = False
        for _ in range(50):
            lock_observed = bool(
                await observer.fetchval(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_locks AS lock
                      JOIN pg_catalog.pg_class AS relation ON relation.oid = lock.relation
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      JOIN pg_catalog.pg_stat_activity AS activity
                        ON activity.pid = lock.pid
                      WHERE namespace.nspname = 'public'
                        AND relation.relname = 'widgets'
                        AND activity.usename = 'jhin_writer'
                        AND lock.mode = 'ShareRowExclusiveLock'
                        AND lock.granted
                    )
                    """
                )
            )
            if lock_observed:
                break
            await asyncio.sleep(0.05)
        assert lock_observed is True

        await ddl_connection.execute("SET lock_timeout = '100ms'")
        with pytest.raises(asyncpg.LockNotAvailableError):
            await ddl_connection.execute(
                "ALTER TABLE public.widgets ADD COLUMN phase9_race_marker integer"
            )
        target_ddl = (
            "CREATE TRIGGER phase9_target_race_trigger BEFORE UPDATE ON public.widgets "
            "FOR EACH ROW EXECUTE FUNCTION private.phase9_race_trigger()",
            "CREATE INDEX phase9_target_race_idx ON public.widgets(id)",
            "CREATE POLICY phase9_target_race_policy ON public.widgets USING (true)",
        )
        for ddl in target_ddl:
            with pytest.raises(asyncpg.LockNotAvailableError):
                await ddl_connection.execute(ddl)
        with pytest.raises(asyncpg.LockNotAvailableError):
            await ddl_connection.execute(
                "CREATE INDEX CONCURRENTLY phase9_peer_race_idx ON public.widget_groups(id)"
            )

        mutation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await mutation
    finally:
        if not mutation.done():
            mutation.cancel()
        with suppress(asyncio.CancelledError, SupabaseDatabaseError):
            await mutation
        await admin.execute("ROLLBACK")
        await observer.close()
        await ddl_connection.close()
        await admin.execute(
            """
            DROP TRIGGER IF EXISTS phase9_target_race_trigger ON public.widgets;
            DROP POLICY IF EXISTS phase9_target_race_policy ON public.widgets;
            DROP INDEX IF EXISTS public.phase9_target_race_idx;
            DROP INDEX IF EXISTS public.phase9_peer_race_idx;
            DROP FUNCTION IF EXISTS private.phase9_race_trigger();
            """
        )

    assert (
        await admin.fetchval(
            """
        SELECT count(*)
        FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.widgets'::regclass
          AND attname = 'phase9_race_marker'
          AND NOT attisdropped
        """
        )
        == 0
    )
    assert await admin.fetchval("SELECT name FROM public.widgets WHERE id = 1") == "alpha"


async def test_source_access_share_lock_blocks_alter_owner_rls_and_rule_races(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    database_dsns: DatabaseDsns,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE TABLE public.phase9_source_race (id integer);
        CREATE TABLE public.phase9_z_source_blocker (id integer);
        INSERT INTO public.phase9_source_race VALUES (1);
        INSERT INTO public.phase9_z_source_blocker VALUES (1);
        GRANT SELECT ON public.phase9_source_race, public.phase9_z_source_blocker TO jhin_reader;
        """
    )
    observer = await asyncpg.connect(
        dsn=database_dsns.admin,
        timeout=5,
        statement_cache_size=0,
    )
    ddl_connection = await asyncpg.connect(
        dsn=database_dsns.admin,
        timeout=5,
        statement_cache_size=0,
    )
    await admin.execute("BEGIN")
    await admin.execute("LOCK TABLE public.phase9_z_source_blocker IN ACCESS EXCLUSIVE MODE")
    connection = await make_postgres_connection(
        role="reader",
        statement_timeout_ms=5_000,
        lock_timeout_ms=5_000,
    )
    read_task = asyncio.create_task(
        _invoke(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                "SELECT source.id FROM public.phase9_source_race AS source "
                "JOIN public.phase9_z_source_blocker AS blocker ON source.id = blocker.id",
            ),
        )
    )
    try:
        lock_observed = False
        for _ in range(50):
            lock_observed = bool(
                await observer.fetchval(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_locks AS lock
                      JOIN pg_catalog.pg_class AS relation ON relation.oid = lock.relation
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      JOIN pg_catalog.pg_stat_activity AS activity
                        ON activity.pid = lock.pid
                      WHERE namespace.nspname = 'public'
                        AND relation.relname = 'phase9_source_race'
                        AND activity.usename = 'jhin_reader'
                        AND lock.mode = 'AccessShareLock'
                        AND lock.granted
                    )
                    """
                )
            )
            if lock_observed:
                break
            await asyncio.sleep(0.05)
        assert lock_observed is True

        await ddl_connection.execute("SET lock_timeout = '100ms'")
        ddl_statements = (
            "ALTER TABLE public.phase9_source_race ADD COLUMN raced integer",
            "ALTER TABLE public.phase9_source_race OWNER TO jhin_reader",
            "ALTER TABLE public.phase9_source_race ENABLE ROW LEVEL SECURITY",
            "CREATE RULE phase9_source_rule AS ON UPDATE "
            "TO public.phase9_source_race DO ALSO NOTHING",
        )
        for ddl in ddl_statements:
            with pytest.raises(asyncpg.LockNotAvailableError):
                await ddl_connection.execute(ddl)
    finally:
        read_task.cancel()
        with suppress(asyncio.CancelledError, SupabaseDatabaseError):
            await read_task
        await admin.execute("ROLLBACK")
        await asyncio.wait_for(observer.close(), timeout=2)
        await asyncio.wait_for(ddl_connection.close(), timeout=2)
        await admin.execute(
            """
            DROP RULE IF EXISTS phase9_source_rule ON public.phase9_source_race;
            ALTER TABLE public.phase9_source_race DISABLE ROW LEVEL SECURITY;
            ALTER TABLE public.phase9_source_race OWNER TO postgres;
            ALTER TABLE public.phase9_source_race DROP COLUMN IF EXISTS raced;
            DROP TABLE IF EXISTS public.phase9_z_source_blocker;
            DROP TABLE IF EXISTS public.phase9_source_race;
            """
        )


async def test_compatible_source_index_race_never_runs_custom_support_function(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    database_dsns: DatabaseDsns,
    admin: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await admin.execute(
        """
        CREATE SEQUENCE private.phase9_index_support_counter;
        CREATE FUNCTION private.phase9_counting_int4cmp(left_value integer, right_value integer)
        RETURNS integer
        LANGUAGE plpgsql
        IMMUTABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
        BEGIN
          PERFORM pg_catalog.nextval('private.phase9_index_support_counter'::pg_catalog.regclass);
          RETURN pg_catalog.btint4cmp(left_value, right_value);
        END
        $function$;
        CREATE OPERATOR CLASS public.phase9_counting_int4_ops
          FOR TYPE integer USING btree AS
          OPERATOR 1 pg_catalog.< (integer, integer),
          OPERATOR 2 pg_catalog.<= (integer, integer),
          OPERATOR 3 pg_catalog.= (integer, integer),
          OPERATOR 4 pg_catalog.>= (integer, integer),
          OPERATOR 5 pg_catalog.> (integer, integer),
          FUNCTION 1 private.phase9_counting_int4cmp(integer, integer);
        CREATE TABLE public.phase9_compatible_index_race (id integer);
        INSERT INTO public.phase9_compatible_index_race
          SELECT value FROM pg_catalog.generate_series(500, 1, -1) AS value;
        GRANT SELECT ON public.phase9_compatible_index_race TO jhin_reader;
        """
    )
    index_connection = await asyncpg.connect(
        dsn=database_dsns.admin,
        timeout=5,
        statement_cache_size=0,
    )
    snapshot_connection = await asyncpg.connect(
        dsn=database_dsns.admin,
        timeout=5,
        statement_cache_size=0,
    )
    snapshot_transaction = snapshot_connection.transaction(readonly=True)
    await snapshot_transaction.start()
    assert (
        await snapshot_connection.fetchval(
            "SELECT count(*) FROM public.phase9_compatible_index_race"
        )
        == 500
    )
    preflight_complete = asyncio.Event()
    resume_execution = asyncio.Event()
    original_preflight = database_preflight.preflight_and_lock

    async def pause_after_locked_recheck(
        connection: Any,
        validated: Any,
        role_oids: frozenset[int],
    ) -> Any:
        result = await original_preflight(connection, validated, role_oids)
        preflight_complete.set()
        await asyncio.wait_for(resume_execution.wait(), timeout=12)
        return result

    monkeypatch.setattr(database_preflight, "preflight_and_lock", pause_after_locked_recheck)
    connection = await make_postgres_connection(role="reader")
    invocation = asyncio.create_task(
        _invoke(
            "supabase.database.read",
            context,
            _read_input(
                connection,
                "SELECT id FROM public.phase9_compatible_index_race WHERE id = 499",
            ),
        )
    )
    index_task: asyncio.Task[str] | None = None
    snapshot_open = True
    try:
        await asyncio.wait_for(preflight_complete.wait(), timeout=5)
        index_task = asyncio.create_task(
            index_connection.execute(
                "CREATE INDEX CONCURRENTLY phase9_compatible_custom_idx "
                "ON public.phase9_compatible_index_race "
                "USING btree (id public.phase9_counting_int4_ops)"
            )
        )
        build_waiting = False
        for _ in range(100):
            state = await admin.fetchrow(
                "SELECT catalog_index.indisready, catalog_index.indisvalid "
                "FROM pg_catalog.pg_index AS catalog_index "
                "JOIN pg_catalog.pg_class AS index_relation "
                "ON index_relation.oid = catalog_index.indexrelid "
                "WHERE index_relation.relname = 'phase9_compatible_custom_idx'"
            )
            counter_called = await admin.fetchval(
                "SELECT is_called FROM private.phase9_index_support_counter"
            )
            build_waiting = (
                state is not None
                and state["indisready"] is True
                and state["indisvalid"] is False
                and counter_called is True
                and not index_task.done()
            )
            if build_waiting:
                break
            await asyncio.sleep(0.05)
        assert build_waiting is True

        before = await admin.fetchrow(
            "SELECT last_value::bigint, is_called FROM private.phase9_index_support_counter"
        )
        assert before["is_called"] is True

        resume_execution.set()
        output = await asyncio.wait_for(invocation, timeout=5)
        assert isinstance(output, DatabaseReadOutput)
        assert output.rows == [["499"]]

        after = await admin.fetchrow(
            "SELECT last_value::bigint, is_called FROM private.phase9_index_support_counter"
        )
        assert tuple(after) == tuple(before)
        assert (
            await admin.fetchval(
                "SELECT idx_scan FROM pg_catalog.pg_stat_user_indexes "
                "WHERE indexrelname = 'phase9_compatible_custom_idx'"
            )
            == 0
        )
        await snapshot_transaction.rollback()
        snapshot_open = False
        assert await asyncio.wait_for(index_task, timeout=10) == "CREATE INDEX"
    finally:
        resume_execution.set()
        if not invocation.done():
            invocation.cancel()
        with suppress(asyncio.CancelledError, SupabaseDatabaseError):
            await invocation
        if snapshot_open:
            with suppress(Exception):
                await snapshot_transaction.rollback()
        if index_task is not None and not index_task.done():
            index_task.cancel()
        if index_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await index_task
        await asyncio.wait_for(index_connection.close(), timeout=2)
        await asyncio.wait_for(snapshot_connection.close(), timeout=2)
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_compatible_index_race;
            DROP OPERATOR CLASS IF EXISTS public.phase9_counting_int4_ops USING btree;
            DROP FUNCTION IF EXISTS private.phase9_counting_int4cmp(integer, integer);
            DROP SEQUENCE IF EXISTS private.phase9_index_support_counter;
            """
        )


async def test_security_definer_backed_materialized_view_is_rejected_without_refresh_effect(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    await admin.execute(
        """
        CREATE FUNCTION private.phase9_matview_effect()
        RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
        BEGIN
          INSERT INTO side_effects(source) VALUES ('matview-refresh');
          RETURN 17;
        END
        $function$;
        REVOKE ALL ON FUNCTION private.phase9_matview_effect() FROM PUBLIC;
        CREATE MATERIALIZED VIEW public.phase9_effect_matview AS
          SELECT private.phase9_matview_effect() AS id
          WITH NO DATA;
        GRANT SELECT ON public.phase9_effect_matview TO jhin_reader;
        REFRESH MATERIALIZED VIEW public.phase9_effect_matview;
        TRUNCATE private.side_effects RESTART IDENTITY;
        """
    )
    try:
        connection = await make_postgres_connection(role="reader")
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.phase9_effect_matview"),
            "database_relation_not_allowed",
        )
        assert await admin.fetchval("SELECT count(*) FROM private.side_effects") == 0
    finally:
        await admin.execute(
            """
            DROP MATERIALIZED VIEW IF EXISTS public.phase9_effect_matview;
            DROP FUNCTION IF EXISTS private.phase9_matview_effect();
            """
        )


async def test_redaction_expansion_cannot_overrun_the_result_budget(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    marker = "redact7"
    await admin.execute("UPDATE public.widgets SET name = $1 WHERE id = 1", marker * 550)
    redactor = get_redactor()
    redactor.clear()
    redactor.register(marker)
    try:
        connection = await make_postgres_connection(
            role="reader",
            max_cell_bytes=4_000,
            max_result_bytes=4_096,
        )
        output = await _invoke(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT name FROM public.widgets WHERE id = 1"),
        )
    finally:
        redactor.clear()

    assert isinstance(output, DatabaseReadOutput)
    assert marker not in output.model_dump_json()
    assert output.truncated is True
    assert len(output.model_dump_json().encode("utf-8")) <= 4_096


async def test_exact_30000_byte_redacted_result_survives_default_tool_gateway(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    marker = "expand7"
    empty_rows = [[""] for _ in range(4)]
    empty_payload = DatabaseReadOutput(
        columns=["value"],
        rows=empty_rows,
        row_count=4,
        truncated=False,
    ).model_dump(mode="json")
    overhead = len(json.dumps(empty_payload, ensure_ascii=False, default=str).encode("utf-8"))
    remaining = 30_000 - overhead
    target_lengths = [remaining // 4] * 4
    for index in range(remaining % 4):
        target_lengths[index] += 1
    raw_values = [
        marker * (length // len("[REDACTED]")) + "x" * (length % len("[REDACTED]"))
        for length in target_lengths
    ]
    assert all(len(value.encode("utf-8")) <= 8_000 for value in raw_values)

    await admin.execute(
        """
        CREATE TABLE public.phase9_gateway_output (id integer, value text);
        ALTER TABLE public.phase9_gateway_output ALTER COLUMN value SET STORAGE EXTERNAL;
        GRANT SELECT ON public.phase9_gateway_output TO jhin_reader;
        """
    )
    await admin.executemany(
        "INSERT INTO public.phase9_gateway_output VALUES ($1, $2)",
        [(index, value) for index, value in enumerate(raw_values)],
    )
    redactor = get_redactor()
    redactor.clear()
    redactor.register(marker)
    try:
        connection = await make_postgres_connection(
            role="reader",
            max_rows=4,
            max_cell_bytes=8_000,
            max_result_bytes=30_000,
        )
        context.session.add(
            Agent(
                id=context.agent_id,
                workspace_id=context.workspace_id,
                name=context.agent_name,
                slug="phase9-database-gateway-agent",
            )
        )
        context.session.add(
            AgentCapabilityGrant(
                workspace_id=context.workspace_id,
                agent_id=context.agent_id,
                capability="supabase.database.read",
                scope_json={
                    "connection_id": str(connection.id),
                    "project_ref": PROJECT_REF,
                    "schema": "public",
                },
                effect="allow",
            )
        )
        await context.session.flush()
        definition, executor = next(
            pair for pair in SupabaseConnector().tools() if pair[0].name == "supabase.database.read"
        )
        catalog = ToolCatalog()
        catalog.register(definition, executor)
        gateway = ToolGateway(context, catalog)

        outcome = await gateway.request(
            "supabase.database.read",
            json.dumps(
                _read_input(
                    connection,
                    "SELECT value FROM public.phase9_gateway_output ORDER BY id",
                ).model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
            ),
        )
    finally:
        redactor.clear()
        await admin.execute("DROP TABLE IF EXISTS public.phase9_gateway_output")

    assert outcome.status == "executed"
    assert outcome.sanitized_output is not None
    assert "original_size_bytes" not in outcome.sanitized_output
    assert "rows" in outcome.sanitized_output
    assert outcome.sanitized_output["truncated"] is False
    rendered = json.dumps(
        outcome.sanitized_output,
        ensure_ascii=False,
        default=str,
    )
    assert marker not in rendered
    assert "[REDACTED]" in rendered
    assert len(rendered.encode("utf-8")) == 30_000


async def test_relation_column_and_index_caps_have_exact_real_catalog_boundaries(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    columns_128 = ", ".join(["id integer", *(f"c{i} integer" for i in range(1, 128))])
    columns_129 = ", ".join(["id integer", *(f"c{i} integer" for i in range(1, 129))])
    await admin.execute(f"CREATE TABLE public.phase9_columns_128 ({columns_128})")
    await admin.execute(f"CREATE TABLE public.phase9_columns_129 ({columns_129})")
    await admin.execute("CREATE TABLE public.phase9_indexes_16 (id integer)")
    await admin.execute("CREATE TABLE public.phase9_indexes_17 (id integer)")
    for index in range(16):
        await admin.execute(f"CREATE INDEX phase9_i16_{index} ON public.phase9_indexes_16 (id)")
    for index in range(17):
        await admin.execute(f"CREATE INDEX phase9_i17_{index} ON public.phase9_indexes_17 (id)")
    await admin.execute(
        """
        GRANT SELECT ON public.phase9_columns_128, public.phase9_columns_129,
          public.phase9_indexes_16, public.phase9_indexes_17 TO jhin_reader;
        """
    )
    try:
        connection = await make_postgres_connection(role="reader")
        for table in ("phase9_columns_128", "phase9_indexes_16"):
            output = await _invoke(
                "supabase.database.read",
                context,
                _read_input(connection, f"SELECT id FROM public.{table}"),
            )
            assert isinstance(output, DatabaseReadOutput)
            assert output.rows == []

        for table in ("phase9_columns_129", "phase9_indexes_17"):
            await _expect_error(
                "supabase.database.read",
                context,
                _read_input(connection, f"SELECT id FROM public.{table}"),
                "database_relation_not_allowed",
            )
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_columns_128;
            DROP TABLE IF EXISTS public.phase9_columns_129;
            DROP TABLE IF EXISTS public.phase9_indexes_16;
            DROP TABLE IF EXISTS public.phase9_indexes_17;
            """
        )


async def test_relation_constraint_cap_accepts_64_and_rejects_65_in_real_catalog(
    context: ToolExecutionContext,
    make_postgres_connection: PostgresConnectionFactory,
    admin: Any,
) -> None:
    constraints_64 = ", ".join(
        f"CONSTRAINT phase9_c64_{index} CHECK (id >= -{index})" for index in range(64)
    )
    constraints_65 = ", ".join(
        f"CONSTRAINT phase9_c65_{index} CHECK (id >= -{index})" for index in range(65)
    )
    await admin.execute(f"CREATE TABLE public.phase9_constraints_64 (id integer, {constraints_64})")
    await admin.execute(f"CREATE TABLE public.phase9_constraints_65 (id integer, {constraints_65})")
    await admin.execute(
        "GRANT SELECT ON public.phase9_constraints_64, public.phase9_constraints_65 TO jhin_reader"
    )
    try:
        connection = await make_postgres_connection(role="reader")

        accepted = await _invoke(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.phase9_constraints_64"),
        )
        assert isinstance(accepted, DatabaseReadOutput)
        assert accepted.rows == []
        await _expect_error(
            "supabase.database.read",
            context,
            _read_input(connection, "SELECT id FROM public.phase9_constraints_65"),
            "database_relation_not_allowed",
        )
    finally:
        await admin.execute(
            """
            DROP TABLE IF EXISTS public.phase9_constraints_64;
            DROP TABLE IF EXISTS public.phase9_constraints_65;
            """
        )
