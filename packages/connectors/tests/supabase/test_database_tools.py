"""Protocol tests for bounded Supabase PostgreSQL execution."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

from jhin_connectors.supabase import database_tools
from jhin_connectors.supabase.connector import SupabaseConnector
from jhin_connectors.supabase.database_tools import SupabaseDatabaseError
from jhin_connectors.supabase.schemas import (
    DatabaseMutationInput,
    DatabaseMutationOutput,
    DatabaseReadInput,
    DatabaseReadOutput,
)
from jhin_db.models import Connection, Workspace
from jhin_tools.builtin import ToolExecutionContext

PROJECT_REF = "abcdefghijklmnopqrst"
EXECUTORS = {definition.name: executor for definition, executor in SupabaseConnector().tools()}
ConnectionFactory = Callable[..., Awaitable[Connection]]


@pytest.fixture
def make_postgres_connection(
    workspace: Workspace,
    make_connection: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> ConnectionFactory:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "127.0.0.1:65433")
    created = 0

    async def factory(**config_overrides: object) -> Connection:
        nonlocal created
        created += 1
        config: dict[str, object] = {
            "project_ref": PROJECT_REF,
            "allowed_schemas": ["public"],
            "allow_writes": True,
            "statement_timeout_ms": 5_000,
            "lock_timeout_ms": 1_000,
            "max_rows": 200,
            "max_cell_bytes": 4_096,
            "max_result_bytes": 24_000,
        }
        config.update(config_overrides)
        return cast(
            Connection,
            await make_connection(
                workspace,
                connector_type="supabase",
                name=f"Phase 9 database {created}",
                auth_type="postgres",
                credentials={
                    "database_url": (
                        "postgresql://jhin_reader:database-password-marker@127.0.0.1:65433/fixture"
                    )
                },
                config=config,
            ),
        )

    return factory


async def _invoke(
    name: str,
    context: ToolExecutionContext,
    payload: BaseModel,
) -> BaseModel:
    return await EXECUTORS[name](context, payload)


def _read(connection: Connection, sql: str, params: list[object] | None = None) -> BaseModel:
    return DatabaseReadInput(
        connection_id=str(connection.id),
        project_ref=PROJECT_REF,
        schema="public",
        sql=sql,
        params=params or [],
    )


def _mutation(
    connection: Connection,
    sql: str,
    params: list[object] | None = None,
) -> BaseModel:
    return DatabaseMutationInput(
        connection_id=str(connection.id),
        project_ref=PROJECT_REF,
        schema="public",
        sql=sql,
        params=params or [],
    )


@pytest.mark.parametrize(
    ("name", "input_factory", "expected_code"),
    [
        (
            "supabase.database.read",
            lambda connection: DatabaseReadInput(
                connection_id=str(connection.id),
                project_ref="tsrqponmlkjihgfedcba",
                schema="public",
                sql="SELECT 1",
            ),
            "project_scope_mismatch",
        ),
        (
            "supabase.database.read",
            lambda connection: DatabaseReadInput(
                connection_id=str(connection.id),
                project_ref=PROJECT_REF,
                schema="private",
                sql="SELECT 1",
            ),
            "schema_scope_mismatch",
        ),
    ],
)
async def test_scope_drift_fails_before_connect(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    input_factory: Callable[[Connection], BaseModel],
    expected_code: str,
) -> None:
    connection = await make_postgres_connection(allowed_schemas=["public"])
    connected = False

    async def fake_connect(**_kwargs: object) -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(name, context, input_factory(connection))

    assert exc_info.value.code == expected_code
    assert exc_info.value.side_effect_possible is False
    assert connected is False


async def test_write_disabled_and_parameter_mismatch_fail_before_connect(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = await make_postgres_connection(allow_writes=False)
    enabled = await make_postgres_connection()
    connected = False

    async def fake_connect(**_kwargs: object) -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as disabled_error:
        await _invoke(
            "supabase.database.write",
            context,
            _mutation(disabled, "INSERT INTO public.widgets (id) VALUES ($1)", [1]),
        )
    with pytest.raises(SupabaseDatabaseError) as parameter_error:
        await _invoke(
            "supabase.database.read",
            context,
            _read(enabled, "SELECT $1", []),
        )

    assert disabled_error.value.code == "database_writes_disabled"
    assert disabled_error.value.side_effect_possible is False
    assert parameter_error.value.code == "database_parameter_mismatch"
    assert parameter_error.value.side_effect_possible is False
    assert connected is False


async def test_ddl_policy_failure_is_pre_effect_and_never_connects(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = await make_postgres_connection()
    connected = False

    async def fake_connect(**_kwargs: object) -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(
            "supabase.database.destructive",
            context,
            _mutation(connection, "ALTER TABLE public.widgets ADD COLUMN unsafe integer", []),
        )

    assert exc_info.value.code == "database_sql_not_allowed"
    assert exc_info.value.side_effect_possible is False
    # The model gets fixed policy guidance (never the parser's view of the SQL).
    assert "COUNT(*)" in exc_info.value.hint and "LIMIT" in exc_info.value.hint
    assert "ALTER" not in exc_info.value.hint
    assert connected is False


async def test_repeated_parameter_mutation_budget_is_preconnect_bounded(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = await make_postgres_connection()
    columns = ", ".join(f"c{index}" for index in range(129))
    values = ", ".join("$1" for _ in range(129))
    sql = f"INSERT INTO public.wide_values ({columns}) VALUES ({values})"
    connected = False

    async def fake_connect(**_kwargs: object) -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(
            "supabase.database.write",
            context,
            _mutation(connection, sql, ["x" * 8_192]),
        )

    assert exc_info.value.code == "database_mutation_too_large"
    assert connected is False


@dataclass(frozen=True)
class FakeType:
    oid: int
    name: str
    schema: str = "pg_catalog"


@dataclass(frozen=True)
class FakeAttribute:
    name: str
    type: FakeType


SAFE_ROLE = {
    "role_oid": 700,
    "role_name": "jhin_reader",
    "current_user": "jhin_reader",
    "session_user": "jhin_reader",
    "rolsuper": False,
    "rolbypassrls": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolreplication": False,
    "owns_current_database": False,
    "owns_allowed_schema": False,
    "can_create_in_allowed_schema": False,
    "server_encoding": "UTF8",
    "session_replication_role": "origin",
    "allowed_schema_count": 1,
}


def _safe_relation(name: str, oid: int) -> dict[str, object]:
    return {
        "oid": oid,
        "schema_name": "public",
        "relation_name": name,
        "relkind": "r",
        "relpersistence": "p",
        "relispartition": False,
        "table_am": "heap",
        "owner_oid": 900,
        "has_inheritance": False,
        "relrowsecurity": False,
        "relforcerowsecurity": False,
        "has_policies": False,
        "has_rules": False,
        "has_user_triggers": False,
        "has_unsafe_internal_triggers": False,
        "has_select_privilege": True,
        "has_write_lock_privilege": True,
        "has_maintain_privilege": True,
    }


SAFE_COLUMNS = {
    "widgets": [
        {
            "attnum": 1,
            "attname": "id",
            "attstorage": "p",
            "atthasdef": False,
            "attidentity": "",
            "attgenerated": "",
            "type_oid": 23,
            "type_name": "int4",
            "type_schema": "pg_catalog",
            "type_kind": "b",
            "type_element": 0,
            "collation_schema": None,
        },
        {
            "attnum": 2,
            "attname": "name",
            "attstorage": "e",
            "atthasdef": False,
            "attidentity": "",
            "attgenerated": "",
            "type_oid": 25,
            "type_name": "text",
            "type_schema": "pg_catalog",
            "type_kind": "b",
            "type_element": 0,
            "collation_schema": "pg_catalog",
        },
    ]
}

SAFE_GUC_SETTINGS: dict[str, tuple[str, str | None]] = {
    "statement_timeout": ("5000", "ms"),
    "lock_timeout": ("1000", "ms"),
    "idle_in_transaction_session_timeout": ("16000", "ms"),
    "search_path": ("pg_catalog", None),
    "standard_conforming_strings": ("on", None),
    "row_security": ("on", None),
    "work_mem": ("1024", "kB"),
    "hash_mem_multiplier": ("1", None),
    "temp_file_limit": ("16384", "kB"),
    "max_parallel_workers_per_gather": ("0", None),
    "jit": ("off", None),
    "enable_seqscan": ("on", None),
    "enable_indexscan": ("off", None),
    "enable_indexonlyscan": ("off", None),
    "enable_bitmapscan": ("off", None),
}


class FakeTransaction:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def start(self) -> None:
        self.events.append("transaction.start")

    async def commit(self) -> None:
        self.events.append("transaction.commit")

    async def rollback(self) -> None:
        self.events.append("transaction.rollback")


class FakeCursor:
    def __init__(self, rows: list[list[object]], events: list[str]) -> None:
        self.rows = list(rows)
        self.events = events

    async def fetchrow(self) -> list[object] | None:
        self.events.append("cursor.fetchrow")
        return self.rows.pop(0) if self.rows else None


class FakePrepared:
    def __init__(
        self,
        sql: str,
        events: list[str],
        *,
        attributes: list[FakeAttribute],
        cursor_rows: list[list[object]],
    ) -> None:
        self.sql = sql
        self.events = events
        self.attributes = attributes
        self.cursor_rows = cursor_rows
        self.status = ""

    def get_attributes(self) -> list[FakeAttribute]:
        self.events.append("prepared.attributes")
        return self.attributes

    async def cursor(self, *_args: object) -> FakeCursor:
        if self.sql.startswith("SELECT 1 FROM ONLY"):
            raise AssertionError("bounded mutation probes must be fully consumed")
        self.events.append(f"prepared.cursor:{_args!r}")
        return FakeCursor(self.cursor_rows, self.events)

    async def fetch(self, *_args: object) -> list[object]:
        self.events.append("prepared.fetch")
        if self.sql.startswith("SELECT 1 FROM ONLY"):
            return list(self.cursor_rows)
        if self.sql.lstrip().upper().startswith("INSERT"):
            self.status = "INSERT 0 1"
        elif self.sql.lstrip().upper().startswith("UPDATE"):
            self.status = "UPDATE 1"
        elif self.sql.lstrip().upper().startswith("DELETE"):
            self.status = "DELETE 1"
        elif self.sql.lstrip().upper().startswith("TRUNCATE"):
            self.status = "TRUNCATE TABLE"
        return []

    def get_statusmsg(self) -> str:
        return self.status


class FakeConnection:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.role_rows: list[dict[str, object]] = [dict(SAFE_ROLE)]
        self.role_row_sequence: list[list[dict[str, object]]] | None = None
        self.relations = {"widgets": _safe_relation("widgets", 100)}
        self.columns = {name: list(rows) for name, rows in SAFE_COLUMNS.items()}
        self.cursor_rows = [
            [
                base64.b64encode(b"1").decode("ascii"),
                False,
                base64.b64encode(b"alpha").decode("ascii"),
                False,
            ]
        ]
        self.probe_rows: list[list[object]] = [[1]]
        self.closed = False
        self.prepared_sql: list[str] = []
        self.jit_available: object = True
        self.setting_overrides: dict[str, tuple[str, str | None]] = {}

    def transaction(self, **kwargs: object) -> FakeTransaction:
        self.events.append(f"transaction.create:{kwargs}")
        return FakeTransaction(self.events)

    async def execute(self, query: str, *_args: object) -> str:
        if query.startswith("SET LOCAL"):
            self.events.append(f"setting:{query}")
        elif query.startswith("LOCK TABLE"):
            self.events.append(f"lock:{query}")
        else:
            self.events.append("execute:internal")
        return "OK"

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        if "jhin:guc_discovery" in query:
            self.events.append("settings.discover")
            names = cast(list[str], args[0])
            return [
                {
                    "name": name,
                    "server_version_num": 170000,
                    "jit_available": self.jit_available,
                }
                for name in names
                if name != "jit" or self.jit_available is True
            ]
        if "jhin:guc_verify" in query:
            self.events.append("settings.verify")
            names = cast(list[str], args[0])
            settings = {**SAFE_GUC_SETTINGS, **self.setting_overrides}
            return [
                {"name": name, "setting": settings[name][0], "unit": settings[name][1]}
                for name in names
                if name != "jit" or self.jit_available is True
            ]
        if "role_closure" in query:
            self.events.append("role.closure")
            if self.role_row_sequence is not None:
                return self.role_row_sequence.pop(0)
            return self.role_rows
        if "pg_catalog.pg_attribute" in query:
            self.events.append("catalog.columns")
            relation_oid = cast(int, args[0])
            relation_name = next(
                name for name, relation in self.relations.items() if relation["oid"] == relation_oid
            )
            return self.columns[relation_name]
        if "pg_catalog.pg_index" in query:
            self.events.append("catalog.indexes")
            return []
        if "pg_catalog.pg_constraint" in query:
            self.events.append("catalog.constraints")
            return []
        raise AssertionError("unexpected internal fetch")

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "jhin:relation_by_name" in query:
            self.events.append("catalog.relation")
            name = cast(str, args[1])
            return self.relations.get(name)
        if "jhin:relation_by_oid" in query:
            self.events.append("catalog.relation")
            relation_oid = cast(int, args[0])
            return next(
                (
                    relation
                    for relation in self.relations.values()
                    if relation["oid"] == relation_oid
                ),
                None,
            )
        raise AssertionError("unexpected internal fetchrow")

    async def prepare(self, sql: str) -> FakePrepared:
        self.prepared_sql.append(sql)
        if sql.startswith("SELECT ") and "__jhin_row" in sql:
            self.events.append("prepare.wrapper")
            return FakePrepared(
                sql,
                self.events,
                attributes=[],
                cursor_rows=self.cursor_rows,
            )
        self.events.append("prepare.original")
        if sql.startswith("SELECT 1 FROM ONLY"):
            return FakePrepared(
                sql,
                self.events,
                attributes=[],
                cursor_rows=self.probe_rows,
            )
        if sql.lstrip().upper().startswith("SELECT"):
            attributes = [
                FakeAttribute("id", FakeType(23, "int4")),
                FakeAttribute("name", FakeType(25, "text")),
            ]
        else:
            attributes = []
        return FakePrepared(
            sql,
            self.events,
            attributes=attributes,
            cursor_rows=[],
        )

    async def close(self) -> None:
        self.events.append("connection.close")
        self.closed = True


async def test_read_uses_one_transaction_locks_then_bounded_wrapper_and_commits(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection(max_rows=2, max_cell_bytes=256)
    fake = FakeConnection()
    connect_calls: list[dict[str, object]] = []

    async def fake_connect(**kwargs: object) -> FakeConnection:
        connect_calls.append(kwargs)
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)
    sql = "SELECT w.id, w.name FROM public.widgets AS w WHERE w.id >= $1 ORDER BY w.id"

    output = await _invoke(
        "supabase.database.read",
        context,
        _read(connection_row, sql, [1]),
    )

    assert isinstance(output, DatabaseReadOutput)
    assert output.columns == ["id", "name"]
    assert output.rows == [["1", "alpha"]]
    assert output.row_count == 1
    assert output.truncated is False
    assert connect_calls == [
        {
            "dsn": ("postgresql://jhin_reader:database-password-marker@127.0.0.1:65433/fixture"),
            "timeout": 5,
            "statement_cache_size": 0,
            "ssl": None,
        }
    ]
    assert fake.events.index("transaction.start") < fake.events.index("role.closure")
    assert fake.events[0] == "transaction.create:{'readonly': True}"
    assert fake.events.index("role.closure") < fake.events.index("catalog.relation")
    assert fake.events.count("role.closure") == 2
    assert next(index for index, event in enumerate(fake.events) if event.startswith("lock:")) < (
        fake.events.index("prepare.original")
    )
    assert fake.events.index("prepare.original") < fake.events.index("prepare.wrapper")
    assert fake.events[-2:] == ["transaction.commit", "connection.close"]
    assert fake.prepared_sql[0] == sql
    wrapper = fake.prepared_sql[1]
    assert f"{sql}\n" in wrapper
    assert "AS __jhin_row(__jhin_c0, __jhin_c1)" in wrapper
    assert "AS __jhin_c0" in wrapper
    assert "AS __jhin_c0_compressed" in wrapper
    assert "AS __jhin_c1" in wrapper
    assert "AS __jhin_c1_compressed" in wrapper
    for function in ("encode", "substr", "convert_to", "pg_column_compression"):
        assert f"pg_catalog.{function}(" in wrapper
        assert f" {function}(" not in wrapper
    assert "SELECT SELECT" not in wrapper
    assert "LIMIT 3" in wrapper
    assert "prepared.cursor:(1,)" in fake.events
    assert fake.events.count("cursor.fetchrow") == 2


async def test_insert_executes_exact_original_once_and_returns_strict_tag_count(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection(max_rows=2)
    fake = FakeConnection()

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)
    sql = "INSERT INTO public.widgets (id, name) VALUES ($1, $2)"

    output = await _invoke(
        "supabase.database.write",
        context,
        _mutation(connection_row, sql, [4, "delta"]),
    )

    assert output == DatabaseMutationOutput(affected_rows=1)
    assert fake.events[0] == "transaction.create:{}"
    assert fake.prepared_sql == [sql]
    assert fake.events.count("prepared.fetch") == 1
    assert fake.events[-2:] == ["transaction.commit", "connection.close"]


async def test_truncate_fully_consumes_the_bounded_probe_before_the_effect(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection(max_rows=2)
    fake = FakeConnection()
    fake.probe_rows = [[1], [1]]

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    output = await _invoke(
        "supabase.database.destructive",
        context,
        _mutation(connection_row, "TRUNCATE public.widgets"),
    )

    assert output == DatabaseMutationOutput(affected_rows=2)
    assert fake.prepared_sql[-2:] == [
        'SELECT 1 FROM ONLY "public"."widgets" LIMIT 3',
        "TRUNCATE public.widgets",
    ]
    assert fake.events.count("prepared.fetch") == 2


@pytest.mark.parametrize("failure", ["row_limit", "assignment_budget"])
async def test_mutation_probe_rejections_are_pre_effect(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    max_rows = 1 if failure == "row_limit" else 200
    connection_row = await make_postgres_connection(max_rows=max_rows)
    fake = FakeConnection()
    probe_count = 2 if failure == "row_limit" else 129
    fake.probe_rows = [[1] for _ in range(probe_count)]
    submitted_sql = "UPDATE public.widgets SET name = $1 WHERE id = $2"
    value = "x" if failure == "row_limit" else "x" * 8_192

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(
            "supabase.database.destructive",
            context,
            _mutation(connection_row, submitted_sql, [value, 0]),
        )

    expected_code = (
        "database_row_limit_exceeded" if failure == "row_limit" else "database_mutation_too_large"
    )
    assert exc_info.value.code == expected_code
    assert exc_info.value.side_effect_possible is False
    assert submitted_sql not in fake.prepared_sql
    assert fake.events[-2:] == ["transaction.rollback", "connection.close"]


async def test_transaction_guc_readback_mismatch_fails_before_role_or_prepare(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection()
    fake = FakeConnection()
    fake.setting_overrides = {"enable_indexscan": ("on", None)}

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(
            "supabase.database.read",
            context,
            _read(connection_row, "SELECT id FROM public.widgets"),
        )

    assert exc_info.value.code == "database_execution_failed"
    assert "settings.verify" in fake.events
    assert "role.closure" not in fake.events
    assert fake.prepared_sql == []
    assert fake.events[-2:] == ["transaction.rollback", "connection.close"]


async def test_explicit_jit_capability_absence_skips_only_jit(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection(max_rows=2, max_cell_bytes=256)
    fake = FakeConnection()
    fake.jit_available = False

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    output = await _invoke(
        "supabase.database.read",
        context,
        _read(
            connection_row,
            "SELECT w.id, w.name FROM public.widgets AS w ORDER BY w.id",
        ),
    )

    assert isinstance(output, DatabaseReadOutput)
    assert not any("SET LOCAL jit" in event for event in fake.events)
    assert "settings.verify" in fake.events


async def test_malformed_jit_capability_fact_fails_closed_before_settings(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection()
    fake = FakeConnection()
    fake.jit_available = None

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(
            "supabase.database.read",
            context,
            _read(connection_row, "SELECT id FROM public.widgets"),
        )

    assert exc_info.value.code == "database_execution_failed"
    assert "settings.verify" not in fake.events
    assert "role.closure" not in fake.events
    assert fake.prepared_sql == []


async def test_role_closure_drift_after_relation_locks_fails_before_prepare(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection()
    fake = FakeConnection()
    fake.role_row_sequence = [
        [dict(SAFE_ROLE)],
        [
            dict(SAFE_ROLE),
            {**SAFE_ROLE, "role_oid": 701, "role_name": "team_reader"},
        ],
    ]

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(
            "supabase.database.read",
            context,
            _read(connection_row, "SELECT id FROM public.widgets"),
        )

    assert exc_info.value.code == "database_role_not_least_privilege"
    assert exc_info.value.side_effect_possible is False
    assert fake.events.count("role.closure") == 2
    assert fake.prepared_sql == []
    assert fake.events[-2:] == ["transaction.rollback", "connection.close"]


async def test_privileged_role_rolls_back_closes_and_never_prepares_submitted_sql(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection()
    fake = FakeConnection()
    fake.role_rows = [{**SAFE_ROLE, "rolcreaterole": True}]

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(
            "supabase.database.read",
            context,
            _read(connection_row, "SELECT id FROM public.widgets"),
        )

    assert exc_info.value.code == "database_role_not_least_privilege"
    assert exc_info.value.side_effect_possible is False
    assert fake.prepared_sql == []
    assert fake.events[-2:] == ["transaction.rollback", "connection.close"]


@pytest.mark.parametrize(
    ("tool_name", "sql", "payload_factory", "side_effect_possible"),
    [
        (
            "supabase.database.read",
            "SELECT id FROM public.widgets",
            lambda connection, sql: _read(connection, sql),
            False,
        ),
        (
            "supabase.database.write",
            "INSERT INTO public.widgets (id, name) VALUES ($1, $2)",
            lambda connection, sql: _mutation(connection, sql, [1, "bounded"]),
            True,
        ),
    ],
)
async def test_database_timeout_is_safe_only_for_read_only_execution(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    sql: str,
    payload_factory: Callable[[Connection, str], BaseModel],
    side_effect_possible: bool,
) -> None:
    connection_row = await make_postgres_connection()
    fake = FakeConnection()
    original_prepare = fake.prepare

    async def timeout_submitted_sql(candidate: str) -> FakePrepared:
        if candidate == sql:
            raise TimeoutError
        return await original_prepare(candidate)

    fake.prepare = timeout_submitted_sql  # type: ignore[assignment]

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        await _invoke(tool_name, context, payload_factory(connection_row, sql))

    assert exc_info.value.code == "database_timeout"
    assert exc_info.value.side_effect_possible is side_effect_possible
    assert fake.events[-2:] == ["transaction.rollback", "connection.close"]


async def test_external_cancellation_is_preserved_after_bounded_cleanup(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_row = await make_postgres_connection()
    fake = FakeConnection()
    entered = asyncio.Event()

    async def stalled_fetch(_query: str, *_args: object) -> list[dict[str, object]]:
        entered.set()
        await asyncio.Event().wait()
        return []

    fake.fetch = stalled_fetch  # type: ignore[method-assign]

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)
    task = asyncio.create_task(
        _invoke(
            "supabase.database.read",
            context,
            _read(connection_row, "SELECT id FROM public.widgets"),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert "transaction.rollback" in fake.events
    assert fake.closed is True


async def test_cancellation_resistant_rollback_and_close_remain_time_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_tools, "DATABASE_CLEANUP_TIMEOUT_SECONDS", 0.01)

    class ResistantCleanup:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()
            self.task: asyncio.Task[object] | None = None

        async def run(self) -> None:
            self.task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release.wait()

    rollback = ResistantCleanup()
    close = ResistantCleanup()
    transaction = SimpleNamespace(rollback=rollback.run)
    connection = SimpleNamespace(close=close.run)

    try:
        async with asyncio.timeout(0.2):
            await database_tools._cleanup(transaction, connection, rollback=True)

        assert rollback.started.is_set()
        assert rollback.cancelled.is_set()
        assert close.started.is_set()
        assert close.cancelled.is_set()
    finally:
        rollback.release.set()
        close.release.set()
        cleanup_tasks = [task for task in (rollback.task, close.task) if task is not None]
        await asyncio.wait_for(asyncio.gather(*cleanup_tasks), timeout=0.2)
    assert all(task.done() for task in cleanup_tasks)


async def test_success_path_cancellation_resistant_close_fails_within_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_tools, "DATABASE_CLEANUP_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()
    release = asyncio.Event()
    cleanup_task: asyncio.Task[object] | None = None

    async def resistant_close() -> None:
        nonlocal cleanup_task
        cleanup_task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    connection = SimpleNamespace(close=resistant_close)
    try:
        with pytest.raises(SupabaseDatabaseError) as exc_info:
            async with asyncio.timeout(0.2):
                await database_tools._close_after_success(connection)
    finally:
        release.set()
        if cleanup_task is not None:
            await asyncio.wait_for(cleanup_task, timeout=0.2)

    assert exc_info.value.code == "database_execution_failed"
    assert cancelled.is_set()
    assert cleanup_task is not None and cleanup_task.done()


async def test_full_executor_never_retries_cancellation_resistant_post_commit_close(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_tools, "DATABASE_CLEANUP_TIMEOUT_SECONDS", 0.01)
    connection_row = await make_postgres_connection(max_rows=2)
    fake = FakeConnection()
    close_attempts = 0
    cancelled = asyncio.Event()
    release = asyncio.Event()
    close_task: asyncio.Task[object] | None = None

    async def resistant_close() -> None:
        nonlocal close_attempts, close_task
        close_attempts += 1
        close_task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    fake.close = resistant_close  # type: ignore[method-assign]

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)
    try:
        with pytest.raises(SupabaseDatabaseError) as exc_info:
            async with asyncio.timeout(0.2):
                await _invoke(
                    "supabase.database.write",
                    context,
                    _mutation(
                        connection_row,
                        "INSERT INTO public.widgets (id, name) VALUES ($1, $2)",
                        [4, "delta"],
                    ),
                )
    finally:
        release.set()
        if close_task is not None:
            await asyncio.wait_for(close_task, timeout=0.2)

    assert exc_info.value.code == "database_execution_failed"
    assert close_attempts == 1
    assert cancelled.is_set()
    assert fake.events.count("transaction.commit") == 1
    assert fake.events.count("transaction.rollback") == 0
    assert close_task is not None and close_task.done()


@pytest.mark.parametrize("failure", ["commit", "rollback", "close"])
async def test_commit_rollback_and_close_failures_remain_stable_and_bounded(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    connection_row = await make_postgres_connection(max_rows=2)
    fake = FakeConnection()
    marker = f"provider-{failure}-secret-marker"

    class FailingTransaction(FakeTransaction):
        async def commit(self) -> None:
            self.events.append("transaction.commit")
            if failure == "commit":
                raise RuntimeError(marker)

        async def rollback(self) -> None:
            self.events.append("transaction.rollback")
            if failure == "rollback":
                raise RuntimeError(marker)

    def fake_transaction(**kwargs: object) -> FailingTransaction:
        fake.events.append(f"transaction.create:{kwargs}")
        return FailingTransaction(fake.events)

    close_attempts = 0

    async def fake_close() -> None:
        nonlocal close_attempts
        close_attempts += 1
        fake.events.append("connection.close")
        if failure == "close":
            raise RuntimeError(marker)
        fake.closed = True

    fake.transaction = fake_transaction  # type: ignore[method-assign]
    fake.close = fake_close  # type: ignore[method-assign]
    if failure == "rollback":
        fake.role_rows = [{**SAFE_ROLE, "rolcreaterole": True}]

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        if failure == "rollback":
            await _invoke(
                "supabase.database.read",
                context,
                _read(connection_row, "SELECT id FROM public.widgets"),
            )
        else:
            await _invoke(
                "supabase.database.write",
                context,
                _mutation(
                    connection_row,
                    "INSERT INTO public.widgets (id, name) VALUES ($1, $2)",
                    [4, "delta"],
                ),
            )

    expected_code = (
        "database_role_not_least_privilege"
        if failure == "rollback"
        else "database_execution_failed"
    )
    assert exc_info.value.code == expected_code
    assert marker not in str(exc_info.value)
    assert close_attempts == 1
    if failure == "commit":
        assert "transaction.rollback" in fake.events
    if failure == "rollback":
        assert fake.events.count("transaction.rollback") == 1


@pytest.mark.parametrize(
    "boundary",
    ["connect", "role", "catalog", "prepare", "cursor", "execute"],
)
async def test_provider_failure_boundaries_are_stable_closed_and_never_retried(
    context: ToolExecutionContext,
    make_postgres_connection: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    connection_row = await make_postgres_connection(max_rows=2)
    fake = FakeConnection()
    marker = f"provider-{boundary}-secret-marker"
    connect_attempts = 0
    submitted_sql = (
        "INSERT INTO public.widgets (id, name) VALUES ($1, $2)"
        if boundary == "execute"
        else "SELECT id, name FROM public.widgets"
    )
    prepare_attempts = 0

    original_fetch = fake.fetch
    original_fetchrow = fake.fetchrow
    original_prepare = fake.prepare
    original_cursor = FakePrepared.cursor
    original_execute = FakePrepared.fetch

    async def fail_role(query: str, *args: object) -> list[dict[str, object]]:
        if "role_closure" in query:
            raise RuntimeError(marker)
        return await original_fetch(query, *args)

    async def fail_catalog(
        query: str,
        *args: object,
    ) -> dict[str, object] | None:
        if "jhin:relation_by_name" in query:
            raise RuntimeError(marker)
        return await original_fetchrow(query, *args)

    async def fail_prepare(sql: str) -> FakePrepared:
        nonlocal prepare_attempts
        if sql == submitted_sql:
            prepare_attempts += 1
            raise RuntimeError(marker)
        return await original_prepare(sql)

    async def fail_cursor(
        prepared: FakePrepared,
        *args: object,
    ) -> FakeCursor:
        if "__jhin_row" in prepared.sql:
            raise RuntimeError(marker)
        return await original_cursor(prepared, *args)

    async def fail_submitted_execute(
        prepared: FakePrepared,
        *args: object,
    ) -> list[object]:
        if prepared.sql == submitted_sql:
            raise RuntimeError(marker)
        return await original_execute(prepared, *args)

    if boundary == "role":
        fake.fetch = fail_role  # type: ignore[method-assign]
    elif boundary == "catalog":
        fake.fetchrow = fail_catalog  # type: ignore[method-assign]
    elif boundary == "prepare":
        fake.prepare = fail_prepare  # type: ignore[method-assign]
    elif boundary == "cursor":
        monkeypatch.setattr(FakePrepared, "cursor", fail_cursor)
    elif boundary == "execute":
        monkeypatch.setattr(FakePrepared, "fetch", fail_submitted_execute)

    async def fake_connect(**_kwargs: object) -> FakeConnection:
        nonlocal connect_attempts
        connect_attempts += 1
        if boundary == "connect":
            raise RuntimeError(marker)
        return fake

    monkeypatch.setattr(database_tools.asyncpg, "connect", fake_connect, raising=False)

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        if boundary == "execute":
            await _invoke(
                "supabase.database.write",
                context,
                _mutation(connection_row, submitted_sql, [4, "delta"]),
            )
        else:
            await _invoke(
                "supabase.database.read",
                context,
                _read(connection_row, submitted_sql),
            )

    assert exc_info.value.code == "database_execution_failed"
    assert marker not in str(exc_info.value)
    assert connect_attempts == 1
    if boundary == "connect":
        assert fake.events == []
    else:
        assert fake.events.count("transaction.rollback") == 1
        assert fake.events.count("connection.close") == 1
    if boundary in {"prepare", "cursor", "execute"}:
        attempts = (
            prepare_attempts if boundary == "prepare" else fake.prepared_sql.count(submitted_sql)
        )
        assert attempts == 1


async def test_pre_hardening_guc_discovery_sets_trusted_namespace_first() -> None:
    query = database_tools._GUC_DISCOVERY_QUERY
    fake = FakeConnection()
    config = SimpleNamespace(statement_timeout_ms=5_000, lock_timeout_ms=1_000)

    await database_tools._apply_transaction_settings(fake, config)

    assert "pg_catalog.current_setting(" in query
    assert "\n    current_setting(" not in query
    assert fake.events.index("setting:SET LOCAL search_path TO pg_catalog") < (
        fake.events.index("settings.discover")
    )


def test_postgres_wrapped_base64_cells_decode_strictly() -> None:
    raw = "🙂".encode() * 40
    encoded = base64.b64encode(raw).decode("ascii")
    postgres_wrapped = "\n".join(
        encoded[index : index + 76] for index in range(0, len(encoded), 76)
    )

    value, truncated = database_tools._decode_cell(
        postgres_wrapped,
        False,
        len(raw),
    )

    assert value == "🙂" * 40
    assert truncated is False


@pytest.mark.parametrize("alias", ["bad\u202ealias", "bad\u0085alias", "bad\ud800alias"])
def test_provider_output_aliases_reject_unicode_control_categories(alias: str) -> None:
    attribute = FakeAttribute(alias, FakeType(23, "int4"))

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        database_tools._column_name(attribute)

    assert exc_info.value.code == "database_output_type_not_allowed"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM public.widgets WHERE name = $1",
        "SELECT id FROM public.widgets ORDER BY name",
        "SELECT id FROM public.widgets AS w(alias_id, alias_name)",
    ],
)
def test_every_variable_width_column_use_must_be_a_direct_projection(sql: str) -> None:
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(
                schema="public",
                name="widgets",
                columns=(
                    SimpleNamespace(name="id", type_name="int4", storage="p"),
                    SimpleNamespace(name="name", type_name="text", storage="e"),
                ),
            ),
        )
    )

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        database_tools._validate_variable_width_uses(
            sql,
            (FakeAttribute("id", FakeType(23, "int4")),),
            preflight,
        )

    assert exc_info.value.code == "database_output_not_safely_sliceable"


def test_nested_alias_shadowing_cannot_hide_outer_wide_text_use() -> None:
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(
                schema="public",
                name="widgets",
                columns=(
                    SimpleNamespace(name="id", type_name="int4", storage="p"),
                    SimpleNamespace(name="name", type_name="text", storage="e"),
                ),
            ),
            SimpleNamespace(
                schema="public",
                name="widget_groups",
                columns=(SimpleNamespace(name="id", type_name="int4", storage="p"),),
            ),
        )
    )

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        database_tools._validate_variable_width_uses(
            "SELECT x.id FROM public.widgets AS x "
            "WHERE x.name LIKE 'a%' AND x.id IN ("
            "SELECT x.id FROM public.widget_groups AS x)",
            (FakeAttribute("id", FakeType(23, "int4")),),
            preflight,
        )

    assert exc_info.value.code == "database_output_not_safely_sliceable"


def test_distinct_nested_aliases_with_fixed_width_uses_remain_allowed() -> None:
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(
                schema="public",
                name="widgets",
                columns=(
                    SimpleNamespace(name="id", type_name="int4", storage="p"),
                    SimpleNamespace(name="name", type_name="text", storage="e"),
                ),
            ),
            SimpleNamespace(
                schema="public",
                name="widget_groups",
                columns=(SimpleNamespace(name="id", type_name="int4", storage="p"),),
            ),
        )
    )

    database_tools._validate_variable_width_uses(
        "SELECT outer_widget.id FROM public.widgets AS outer_widget "
        "WHERE outer_widget.id IN ("
        "SELECT inner_group.id FROM public.widget_groups AS inner_group)",
        (FakeAttribute("id", FakeType(23, "int4")),),
        preflight,
    )

    database_tools._validate_variable_width_uses(
        "SELECT x.id FROM public.widgets AS x WHERE x.id IN ("
        "SELECT x.id FROM public.widget_groups AS x)",
        (FakeAttribute("id", FakeType(23, "int4")),),
        preflight,
    )
    database_tools._validate_variable_width_uses(
        "SELECT id FROM public.widgets UNION SELECT id FROM public.widgets",
        (FakeAttribute("id", FakeType(23, "int4")),),
        preflight,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name AS n FROM public.widgets ORDER BY n",
        "SELECT name FROM public.widgets ORDER BY 1",
        "SELECT name FROM public.widgets ORDER BY (1)",
        "SELECT name FROM public.widgets ORDER BY (((1)))",
        "SELECT name FROM public.widgets GROUP BY (1)",
        "SELECT name AS n FROM public.widgets ORDER BY ((n))",
        "SELECT name AS n FROM public.widgets GROUP BY (n)",
        "SELECT name FROM public.widgets GROUP BY 1",
        ("SELECT w.name FROM public.widgets AS w JOIN public.archived_widgets AS a USING (name)"),
    ],
)
def test_text_alias_ordinal_and_join_using_cannot_bypass_usage_checks(sql: str) -> None:
    columns = (
        SimpleNamespace(name="id", type_name="int4", storage="p"),
        SimpleNamespace(name="name", type_name="text", storage="e"),
    )
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(schema="public", name="widgets", columns=columns),
            SimpleNamespace(schema="public", name="archived_widgets", columns=columns),
        )
    )

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        database_tools._validate_variable_width_uses(
            sql,
            (FakeAttribute("name", FakeType(25, "text")),),
            preflight,
        )

    assert exc_info.value.code == "database_output_not_safely_sliceable"


@pytest.mark.parametrize(
    ("sql", "attribute"),
    [
        ("SELECT DISTINCT name FROM public.widgets", FakeAttribute("name", FakeType(25, "text"))),
        (
            "SELECT label FROM public.widgets UNION SELECT label FROM public.archived_widgets",
            FakeAttribute("label", FakeType(1043, "varchar")),
        ),
        (
            "SELECT name || 'suffix' FROM public.widgets",
            FakeAttribute("name", FakeType(25, "text")),
        ),
        (
            "SELECT name = 'match' FROM public.widgets",
            FakeAttribute("matched", FakeType(16, "bool")),
        ),
    ],
)
def test_variable_width_distinct_set_and_projection_expressions_are_rejected(
    sql: str,
    attribute: FakeAttribute,
) -> None:
    columns = (
        SimpleNamespace(name="name", type_name="text", storage="e"),
        SimpleNamespace(name="label", type_name="varchar", storage="e"),
    )
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(schema="public", name="widgets", columns=columns),
            SimpleNamespace(schema="public", name="archived_widgets", columns=columns),
        )
    )

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        database_tools._validate_variable_width_uses(sql, (attribute,), preflight)

    assert exc_info.value.code == "database_output_not_safely_sliceable"


def test_pathologically_long_order_ordinal_is_stably_rejected() -> None:
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(
                schema="public",
                name="widgets",
                columns=(SimpleNamespace(name="name", type_name="text", storage="e"),),
            ),
        )
    )

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        database_tools._validate_variable_width_uses(
            "SELECT name FROM public.widgets ORDER BY " + "0" * 5_000 + "1",
            (FakeAttribute("name", FakeType(25, "text")),),
            preflight,
        )

    assert exc_info.value.code == "database_output_not_safely_sliceable"


def test_direct_text_may_order_by_a_fixed_width_column() -> None:
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(
                schema="public",
                name="widgets",
                columns=(
                    SimpleNamespace(name="id", type_name="int4", storage="p"),
                    SimpleNamespace(name="name", type_name="text", storage="e"),
                ),
            ),
        )
    )

    database_tools._validate_variable_width_uses(
        "SELECT name FROM public.widgets ORDER BY id",
        (FakeAttribute("name", FakeType(25, "text")),),
        preflight,
    )
    database_tools._validate_variable_width_uses(
        "SELECT id FROM public.widgets ORDER BY (((1)))",
        (FakeAttribute("id", FakeType(23, "int4")),),
        preflight,
    )


def test_fixed_width_recursive_cte_column_aliases_remain_allowed() -> None:
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(
                schema="public",
                name="widgets",
                columns=(SimpleNamespace(name="id", type_name="int4", storage="p"),),
            ),
        )
    )

    database_tools._validate_variable_width_uses(
        (
            "WITH RECURSIVE wanted(id) AS ("
            "SELECT id FROM public.widgets UNION SELECT id FROM wanted) "
            "SELECT id FROM wanted"
        ),
        (FakeAttribute("id", FakeType(23, "int4")),),
        preflight,
    )


def test_fixed_output_cte_star_cannot_materialize_unprojected_wide_text() -> None:
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(
                schema="public",
                name="widgets",
                columns=(
                    SimpleNamespace(name="id", type_name="int4", storage="p"),
                    SimpleNamespace(name="name", type_name="text", storage="e"),
                ),
            ),
        )
    )

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        database_tools._validate_variable_width_uses(
            (
                "WITH RECURSIVE wanted(id, name) AS ("
                "SELECT * FROM public.widgets UNION SELECT * FROM public.widgets) "
                "SELECT id FROM wanted"
            ),
            (FakeAttribute("id", FakeType(23, "int4")),),
            preflight,
        )

    assert exc_info.value.code == "database_output_not_safely_sliceable"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT w.id FROM public.widgets AS w ORDER BY w",
        "SELECT w.id FROM public.widgets AS w WHERE w IS NULL",
        "SELECT w FROM public.widgets AS w",
    ],
)
def test_whole_row_reference_cannot_materialize_or_compare_wide_text(sql: str) -> None:
    preflight = SimpleNamespace(
        relations=(
            SimpleNamespace(
                schema="public",
                name="widgets",
                columns=(
                    SimpleNamespace(name="id", type_name="int4", storage="p"),
                    SimpleNamespace(name="name", type_name="text", storage="e"),
                ),
            ),
        )
    )

    with pytest.raises(SupabaseDatabaseError) as exc_info:
        database_tools._validate_variable_width_uses(
            sql,
            (FakeAttribute("id", FakeType(23, "int4")),),
            preflight,
        )

    assert exc_info.value.code == "database_output_not_safely_sliceable"
