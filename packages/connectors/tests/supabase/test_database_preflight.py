"""Fail-closed live catalog preflight for Supabase PostgreSQL execution."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from typing import Any, cast

import asyncpg
import pytest

from jhin_connectors.supabase.database_preflight import (
    DatabasePreflightError,
    preflight_and_lock,
    verify_live_role,
)
from jhin_connectors.supabase.sql_policy import (
    RelationRef,
    SqlClass,
    ValidatedSql,
    classify_and_validate_sql,
)

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


def _safe_relation(name: str, oid: int, *, schema: str = "public") -> dict[str, object]:
    return {
        "oid": oid,
        "schema_name": schema,
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


SAFE_COLUMNS = (
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
)


def _safe_fk(child_oid: int, parent_oid: int) -> dict[str, object]:
    return {
        "constraint_oid": 1_000 + child_oid + parent_oid,
        "constraint_type": "f",
        "relation_oid": child_oid,
        "referenced_relation_oid": parent_oid,
        "deferrable": False,
        "initially_deferred": False,
        "validated": True,
        "update_action": "a",
        "delete_action": "a",
        "match_type": "s",
        "fk_operators_safe": True,
        "local_columns": (1,),
        "referenced_columns": (1,),
    }


def _safe_column(attnum: int) -> dict[str, object]:
    return {
        **SAFE_COLUMNS[0],
        "attnum": attnum,
        "attname": f"column_{attnum}",
    }


def _safe_index(index_oid: int) -> dict[str, object]:
    return {
        "index_oid": index_oid,
        "valid": True,
        "ready": True,
        "live": True,
        "nulls_not_distinct": False,
        "exclusion": False,
        "has_reloptions": False,
        "has_expressions": False,
        "has_predicate": False,
        "attribute_count": 1,
        "key_attribute_count": 1,
        "key_columns": (1,),
        "keys_are_columns": True,
        "operator_classes_safe": True,
        "index_collations_safe": True,
        "access_method_safe": True,
    }


def _safe_unique_constraint(constraint_oid: int, relation_oid: int) -> dict[str, object]:
    return {
        "constraint_oid": constraint_oid,
        "constraint_type": "u",
        "relation_oid": relation_oid,
        "referenced_relation_oid": 0,
        "deferrable": False,
        "initially_deferred": False,
        "validated": True,
        "update_action": " ",
        "delete_action": " ",
        "match_type": " ",
        "fk_operators_safe": True,
        "local_columns": (1,),
        "referenced_columns": None,
    }


class FakeConnection:
    def __init__(self) -> None:
        widgets = _safe_relation("widgets", 100)
        self.role_rows: list[dict[str, object]] = [dict(SAFE_ROLE)]
        self.relations: dict[tuple[str, str], dict[str, object]] = {
            ("public", "widgets"): widgets,
        }
        self.columns: dict[int, list[dict[str, object]]] = {
            100: [dict(row) for row in SAFE_COLUMNS],
        }
        self.indexes: dict[int, list[dict[str, object]]] = {100: []}
        self.constraints: dict[int, list[dict[str, object]]] = {100: []}
        self.calls: list[tuple[str, str, tuple[object, ...]]] = []
        self.raw_queries: list[str] = []
        self.lock_hook: Any = None
        self._lock_count = 0

    def add_relation(
        self,
        name: str,
        oid: int,
        *,
        schema: str = "public",
        columns: tuple[dict[str, object], ...] = SAFE_COLUMNS,
    ) -> None:
        self.relations[(schema, name)] = _safe_relation(name, oid, schema=schema)
        self.columns[oid] = [dict(row) for row in columns]
        self.indexes[oid] = []
        self.constraints[oid] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.raw_queries.append(query)
        if "jhin:role_closure" in query:
            kind = "role"
            rows = self.role_rows
        elif "jhin:columns" in query:
            kind = "columns"
            rows = self.columns.get(cast(int, args[0]), [])
        elif "jhin:indexes" in query:
            kind = "indexes"
            rows = self.indexes.get(cast(int, args[0]), [])
        elif "jhin:constraints" in query:
            kind = "constraints"
            rows = self.constraints.get(cast(int, args[0]), [])
        else:
            raise AssertionError(f"unexpected fetch query: {query}")
        self.calls.append(("fetch", kind, args))
        return [dict(row) for row in rows]

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.raw_queries.append(query)
        if "jhin:relation_by_name" in query:
            kind = "relation_by_name"
            row = self.relations.get((cast(str, args[0]), cast(str, args[1])))
        elif "jhin:relation_by_oid" in query:
            kind = "relation_by_oid"
            wanted_oid = cast(int, args[0])
            row = next(
                (
                    candidate
                    for candidate in self.relations.values()
                    if candidate["oid"] == wanted_oid
                ),
                None,
            )
        else:
            raise AssertionError(f"unexpected fetchrow query: {query}")
        self.calls.append(("fetchrow", kind, args))
        return dict(row) if row is not None else None

    async def execute(self, query: str, *args: object) -> str:
        self.raw_queries.append(query)
        self.calls.append(("execute", query, args))
        if query.startswith("LOCK TABLE"):
            self._lock_count += 1
            if self.lock_hook is not None:
                self.lock_hook(self, self._lock_count)
        return "LOCK TABLE"


def _validated(
    *relations: RelationRef,
    statement_type: str = "select",
    target: RelationRef | None = None,
) -> ValidatedSql:
    sql_class: SqlClass = (
        "read" if target is None else ("write" if statement_type == "insert" else "destructive")
    )
    return ValidatedSql(
        sql_class=sql_class,
        statement_type=statement_type,
        relations=tuple(relations),
        mutation_target=target,
        parameter_indexes=(),
        mutation_values=(),
        insert_row_count=None,
    )


def _error_code(exc_info: pytest.ExceptionInfo[DatabasePreflightError]) -> str:
    assert str(exc_info.value) == "Supabase database preflight failed"
    return exc_info.value.code


@contextmanager
def _case(**params: object) -> Iterator[None]:
    """Stamp the folded-loop case identity onto any failure.

    Mirrors the old per-case parametrize id: a failing iteration names its exact
    parameter values, including a pytest.raises DID-NOT-RAISE failure.
    """
    try:
        yield
    except BaseException as exc:
        exc.add_note(f"failed case: {params!r}")
        raise


async def test_verify_live_role_uses_cycle_safe_bounded_closure_on_same_connection() -> None:
    connection = FakeConnection()
    connection.role_rows = [
        {**SAFE_ROLE, "role_oid": 702, "role_name": "analytics"},
        dict(SAFE_ROLE),
    ]

    result = await verify_live_role(connection, ("public",))

    assert result == (700, 702)
    query = connection.raw_queries[0]
    assert "WITH RECURSIVE role_closure" in query
    assert "UNION" in query
    assert "UNION ALL" not in query
    assert "ORDER BY" not in query
    assert "LIMIT 65" in query
    assert "has_schema_privilege" in query
    assert "can_create_in_allowed_schema" in query
    assert connection.calls[0][2] == (["public"],)


async def test_verify_live_role_accepts_64_and_rejects_65_reachable_roles() -> None:
    connection = FakeConnection()
    connection.role_rows = [
        {
            **SAFE_ROLE,
            "role_oid": 700 + index,
            "role_name": "jhin_reader" if index == 0 else f"team_role_{index}",
        }
        for index in range(64)
    ]
    assert len(await verify_live_role(connection, ("public",))) == 64

    connection.role_rows.append({**SAFE_ROLE, "role_oid": 764, "role_name": "team_role_64"})
    with pytest.raises(DatabasePreflightError) as exc_info:
        await verify_live_role(connection, ("public",))
    assert _error_code(exc_info) == "database_role_not_least_privilege"


# Loop-folded (was @pytest.mark.parametrize with 15 items): identical matrix, a
# fresh FakeConnection per iteration, so the collected item count is 1.
async def test_verify_live_role_rejects_unsafe_ancestors_without_details() -> None:
    unsafe_cases: list[dict[str, object]] = [
        {"current_user": "other"},
        {"session_user": "other"},
        {"role_name": "postgres"},
        {"role_name": "pg_read_all_data"},
        {"rolsuper": True},
        {"rolbypassrls": True},
        {"rolcreatedb": True},
        {"rolcreaterole": True},
        {"rolreplication": True},
        {"owns_current_database": True},
        {"owns_allowed_schema": True},
        {"can_create_in_allowed_schema": True},
        {"server_encoding": "LATIN1"},
        {"session_replication_role": "replica"},
        {"allowed_schema_count": 0},
    ]

    for unsafe in unsafe_cases:
        with _case(unsafe=unsafe):
            connection = FakeConnection()
            connection.role_rows = [{**SAFE_ROLE, **unsafe}]

            with pytest.raises(DatabasePreflightError) as exc_info:
                await verify_live_role(connection, ("public",))

            assert _error_code(exc_info) == "database_role_not_least_privilege", f"case {unsafe!r}"
            assert all(str(value) not in str(exc_info.value) for value in unsafe.values()), (
                f"case {unsafe!r}"
            )


@pytest.mark.parametrize(
    "schemas",
    [
        (),
        tuple(f"schema_{index}" for index in range(9)),
        ("pg_catalog",),
        ("pg_user_schema",),
        ("information_schema",),
    ],
)
async def test_verify_live_role_bounds_allowed_schemas(schemas: tuple[str, ...]) -> None:
    with pytest.raises(DatabasePreflightError) as exc_info:
        await verify_live_role(FakeConnection(), schemas)
    assert _error_code(exc_info) == "database_role_not_least_privilege"


async def test_relationless_read_needs_no_catalog_or_relation_lock() -> None:
    connection = FakeConnection()

    result = await preflight_and_lock(
        connection,
        _validated(),
        (700,),
    )

    assert result.relations == ()
    assert result.target is None
    assert connection.calls == []


async def test_read_preflight_returns_frozen_snapshots_and_locks_before_recheck() -> None:
    connection = FakeConnection()
    validated = _validated(RelationRef("public", "widgets", "source"))

    result = await preflight_and_lock(connection, validated, (700,))

    assert len(result.relations) == 1
    relation = result.relations[0]
    assert (relation.schema, relation.name, relation.oid, relation.access) == (
        "public",
        "widgets",
        100,
        "source",
    )
    assert [
        (column.attnum, column.name, column.type_oid, column.type_name, column.storage)
        for column in relation.columns
    ] == [
        (1, "id", 23, "int4", "p"),
        (2, "name", 25, "text", "e"),
    ]
    assert result.target is None
    with pytest.raises(FrozenInstanceError):
        relation.oid = 999  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        relation.columns[0].name = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.target = relation  # type: ignore[misc]
    lock_index = next(
        index
        for index, call in enumerate(connection.calls)
        if call[0] == "execute" and call[1].startswith("LOCK TABLE")
    )
    assert connection.calls[lock_index][1] == (
        'LOCK TABLE ONLY "public"."widgets" IN ACCESS SHARE MODE'
    )
    assert any(call[1] == "relation_by_name" for call in connection.calls[:lock_index])
    assert any(call[1] == "relation_by_name" for call in connection.calls[lock_index + 1 :])
    column_query = next(query for query in connection.raw_queries if "jhin:columns" in query)
    assert "ORDER BY" not in column_query
    assert "LIMIT 129" in column_query
    relation_query = next(
        query for query in connection.raw_queries if "jhin:relation_by_name" in query
    )
    assert "MAINTAIN, UPDATE" not in relation_query
    for privilege in ("MAINTAIN", "UPDATE", "DELETE", "TRUNCATE"):
        assert f"has_table_privilege(relation.oid, '{privilege}')" in relation_query
    assert "internal_trigger.tgconstraint" in relation_query
    assert "trigger_constraint.contype <> 'f'" in relation_query


async def test_mutation_preflight_closes_safe_fk_and_locks_in_name_order() -> None:
    connection = FakeConnection()
    connection.add_relation("widget_groups", 200)
    fk = _safe_fk(100, 200)
    connection.constraints[100] = [fk]
    connection.constraints[200] = [fk]
    target = RelationRef("public", "widgets", "target")
    validated = _validated(target, statement_type="update", target=target)

    result = await preflight_and_lock(connection, validated, (700,))

    assert [(relation.name, relation.access) for relation in result.relations] == [
        ("widget_groups", "peer"),
        ("widgets", "target"),
    ]
    assert result.target is not None and result.target.name == "widgets"
    locks = [
        call[1]
        for call in connection.calls
        if call[0] == "execute" and call[1].startswith("LOCK TABLE")
    ]
    assert locks == [
        'LOCK TABLE ONLY "public"."widget_groups" IN SHARE ROW EXCLUSIVE MODE',
        'LOCK TABLE ONLY "public"."widgets" IN SHARE ROW EXCLUSIVE MODE',
    ]


async def test_truncate_uses_access_exclusive_target_and_locks_fk_peer() -> None:
    connection = FakeConnection()
    connection.add_relation("widget_groups", 200)
    fk = _safe_fk(100, 200)
    connection.constraints[100] = [fk]
    connection.constraints[200] = [fk]
    target = RelationRef("public", "widgets", "target")

    await preflight_and_lock(
        connection,
        _validated(target, statement_type="truncate", target=target),
        (700,),
    )

    locks = [call[1] for call in connection.calls if call[0] == "execute"]
    assert locks == [
        'LOCK TABLE ONLY "public"."widget_groups" IN SHARE ROW EXCLUSIVE MODE',
        'LOCK TABLE ONLY "public"."widgets" IN ACCESS EXCLUSIVE MODE',
    ]


async def test_truncate_rejects_external_inbound_fk_before_any_lock() -> None:
    connection = FakeConnection()
    connection.add_relation("widget_children", 200)
    inbound_fk = _safe_fk(200, 100)
    connection.constraints[100] = [inbound_fk]
    connection.constraints[200] = [inbound_fk]
    target = RelationRef("public", "widgets", "target")

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(target, statement_type="truncate", target=target),
            (700,),
        )

    assert _error_code(exc_info) == "database_relation_not_allowed"
    assert not any(call[0] == "execute" for call in connection.calls)


async def test_post_lock_oid_change_is_rejected_without_catalog_details() -> None:
    connection = FakeConnection()

    def replace_relation(fake: FakeConnection, lock_count: int) -> None:
        if lock_count == 1:
            replacement = dict(fake.relations[("public", "widgets")])
            replacement["oid"] = 101
            fake.relations[("public", "widgets")] = replacement
            fake.columns[101] = [dict(row) for row in SAFE_COLUMNS]
            fake.indexes[101] = []
            fake.constraints[101] = []

    connection.lock_hook = replace_relation

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(RelationRef("public", "widgets", "source")),
            (700,),
        )

    assert _error_code(exc_info) == "database_relation_not_allowed"
    assert "widgets" not in str(exc_info.value)


async def test_relation_cap_accepts_32_and_rejects_33_before_catalog_work() -> None:
    connection = FakeConnection()
    refs = [RelationRef("public", "widgets", "source")]
    for index in range(1, 32):
        name = f"relation_{index:02d}"
        connection.add_relation(name, 100 + index)
        refs.append(RelationRef("public", name, "source"))

    result = await preflight_and_lock(connection, _validated(*refs), (700,))
    assert len(result.relations) == 32

    calls_before_rejection = len(connection.calls)
    refs.append(RelationRef("public", "relation_32", "source"))
    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(connection, _validated(*refs), (700,))
    assert _error_code(exc_info) == "database_relation_not_allowed"
    assert len(connection.calls) == calls_before_rejection


def _fk_chain(count: int) -> FakeConnection:
    connection = FakeConnection()
    for index in range(1, count):
        name = f"fk_relation_{index:02d}"
        connection.add_relation(name, 100 + index)
    for index in range(count - 1):
        child_oid = 100 + index
        parent_oid = child_oid + 1
        fk = _safe_fk(child_oid, parent_oid)
        connection.constraints[child_oid].append(fk)
        connection.constraints[parent_oid].append(fk)
    return connection


async def test_fk_closure_accepts_32_and_rejects_33_relations() -> None:
    target = RelationRef("public", "widgets", "target")
    validated = _validated(target, statement_type="update", target=target)

    accepted = await preflight_and_lock(_fk_chain(32), validated, (700,))
    assert len(accepted.relations) == 32

    rejected_connection = _fk_chain(33)
    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(rejected_connection, validated, (700,))
    assert _error_code(exc_info) == "database_relation_not_allowed"
    assert not any(call[0] == "execute" for call in rejected_connection.calls)


async def test_column_cap_accepts_128_and_rejects_129() -> None:
    accepted = FakeConnection()
    accepted.columns[100] = [_safe_column(index) for index in range(1, 129)]
    result = await preflight_and_lock(
        accepted,
        _validated(RelationRef("public", "widgets", "source")),
        (700,),
    )
    assert len(result.relations[0].columns) == 128

    rejected = FakeConnection()
    rejected.columns[100] = [_safe_column(index) for index in range(1, 130)]
    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            rejected,
            _validated(RelationRef("public", "widgets", "source")),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


async def test_index_cap_accepts_16_and_rejects_17() -> None:
    accepted = FakeConnection()
    accepted.indexes[100] = [_safe_index(1_000 + index) for index in range(16)]
    await preflight_and_lock(
        accepted,
        _validated(RelationRef("public", "widgets", "source")),
        (700,),
    )
    index_query = next(query for query in accepted.raw_queries if "jhin:indexes" in query)
    assert "access_method.amname = 'btree'" in index_query
    assert "ANY(ARRAY['btree'" not in index_query
    assert "ORDER BY" not in index_query
    assert "LIMIT 17" in index_query

    rejected = FakeConnection()
    rejected.indexes[100] = [_safe_index(1_000 + index) for index in range(17)]
    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            rejected,
            _validated(RelationRef("public", "widgets", "source")),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


async def test_constraint_cap_accepts_64_and_rejects_65() -> None:
    accepted = FakeConnection()
    accepted.constraints[100] = [_safe_unique_constraint(2_000 + index, 100) for index in range(64)]
    await preflight_and_lock(
        accepted,
        _validated(RelationRef("public", "widgets", "source")),
        (700,),
    )

    rejected = FakeConnection()
    rejected.constraints[100] = [_safe_unique_constraint(2_000 + index, 100) for index in range(65)]
    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            rejected,
            _validated(RelationRef("public", "widgets", "source")),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


# Loop-folded (was @pytest.mark.parametrize with 13 items): identical matrix, a
# fresh FakeConnection per iteration, so the collected item count is 1.
async def test_unsafe_relation_shapes_and_privileges_fail_before_lock() -> None:
    cases: list[tuple[str, object, bool]] = [
        ("relkind", "v", False),
        ("relpersistence", "u", False),
        ("relispartition", True, False),
        ("table_am", "columnar", False),
        ("has_inheritance", True, False),
        ("relrowsecurity", True, False),
        ("relforcerowsecurity", True, False),
        ("has_policies", True, False),
        ("has_select_privilege", False, False),
        ("has_rules", True, True),
        ("has_user_triggers", True, True),
        ("has_unsafe_internal_triggers", True, True),
        ("has_write_lock_privilege", False, True),
    ]

    for field, unsafe_value, target in cases:
        with _case(field=field, unsafe_value=unsafe_value, target=target):
            connection = FakeConnection()
            connection.relations[("public", "widgets")][field] = unsafe_value
            ref = RelationRef("public", "widgets", "target" if target else "source")
            validated = (
                _validated(ref, statement_type="update", target=ref) if target else _validated(ref)
            )

            with pytest.raises(DatabasePreflightError) as exc_info:
                await preflight_and_lock(connection, validated, (700,))

            assert _error_code(exc_info) == "database_relation_not_allowed", (
                f"case ({field!r}, {unsafe_value!r}, {target!r})"
            )
            assert not any(call[0] == "execute" for call in connection.calls), (
                f"case ({field!r}, {unsafe_value!r}, {target!r})"
            )


async def test_relation_owner_in_role_closure_uses_least_privilege_error() -> None:
    connection = FakeConnection()
    connection.relations[("public", "widgets")]["owner_oid"] = 700

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(RelationRef("public", "widgets", "source")),
            (700,),
        )

    assert _error_code(exc_info) == "database_role_not_least_privilege"
    assert "widgets" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("attstorage", "z"),
        ("atthasdef", True),
        ("attidentity", "a"),
        ("attgenerated", "s"),
        ("type_schema", "public"),
        ("type_kind", "d"),
        ("type_element", 23),
        ("collation_schema", "public"),
    ],
)
async def test_unsafe_target_columns_reject_hidden_code_and_custom_types(
    field: str,
    unsafe_value: object,
) -> None:
    connection = FakeConnection()
    column = dict(SAFE_COLUMNS[0])
    column[field] = unsafe_value
    connection.columns[100] = [column]
    target = RelationRef("public", "widgets", "target")

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(target, statement_type="insert", target=target),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


@pytest.mark.parametrize(
    ("type_oid", "type_name"),
    [(869, "inet"), (26, "oid"), (790, "money"), (23, "int8")],
)
async def test_non_whitelisted_or_mismatched_builtin_types_are_rejected(
    type_oid: int,
    type_name: str,
) -> None:
    connection = FakeConnection()
    column = {**SAFE_COLUMNS[0], "type_oid": type_oid, "type_name": type_name}
    connection.columns[100] = [column]

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(RelationRef("public", "widgets", "source")),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


# Loop-folded (was @pytest.mark.parametrize with 15 items): identical matrix, a
# fresh FakeConnection and index row per iteration, so the collected item count is 1.
async def test_unsafe_indexes_fail_closed() -> None:
    cases: list[tuple[str, object]] = [
        ("valid", False),
        ("ready", False),
        ("live", False),
        ("nulls_not_distinct", True),
        ("exclusion", True),
        ("has_reloptions", True),
        ("has_expressions", True),
        ("has_predicate", True),
        ("attribute_count", 2),
        ("keys_are_columns", False),
        ("operator_classes_safe", False),
        ("index_collations_safe", False),
        ("access_method_safe", False),
        ("key_columns", (-1,)),
        ("key_columns", (3,)),
    ]

    for field, unsafe_value in cases:
        with _case(field=field, unsafe_value=unsafe_value):
            connection = FakeConnection()
            index = _safe_index(1_000)
            index[field] = unsafe_value
            connection.indexes[100] = [index]

            with pytest.raises(DatabasePreflightError) as exc_info:
                await preflight_and_lock(
                    connection,
                    _validated(RelationRef("public", "widgets", "source")),
                    (700,),
                )
            assert _error_code(exc_info) == "database_relation_not_allowed", (
                f"case ({field!r}, {unsafe_value!r})"
            )


@pytest.mark.parametrize("kind", ["c", "t", "x"])
async def test_effect_bearing_target_constraints_are_rejected(kind: str) -> None:
    connection = FakeConnection()
    constraint = _safe_unique_constraint(2_000, 100)
    constraint["constraint_type"] = kind
    connection.constraints[100] = [constraint]
    target = RelationRef("public", "widgets", "target")

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(target, statement_type="update", target=target),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


@pytest.mark.parametrize(
    "unsafe",
    [
        {"deferrable": True},
        {"initially_deferred": True},
        {"validated": False},
    ],
)
async def test_target_primary_and_unique_constraints_must_be_simple(
    unsafe: dict[str, object],
) -> None:
    connection = FakeConnection()
    constraint = {**_safe_unique_constraint(2_000, 100), **unsafe}
    connection.constraints[100] = [constraint]
    target = RelationRef("public", "widgets", "target")

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(target, statement_type="update", target=target),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


@pytest.mark.parametrize("kind", ["p", "u"])
async def test_simple_immediate_target_primary_and_unique_constraints_are_safe(
    kind: str,
) -> None:
    connection = FakeConnection()
    constraint = _safe_unique_constraint(2_000, 100)
    constraint["constraint_type"] = kind
    connection.constraints[100] = [constraint]
    target = RelationRef("public", "widgets", "target")

    result = await preflight_and_lock(
        connection,
        _validated(target, statement_type="update", target=target),
        (700,),
    )

    assert result.target is not None and result.target.oid == 100


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("deferrable", True),
        ("initially_deferred", True),
        ("validated", False),
        ("update_action", "c"),
        ("delete_action", "c"),
        ("match_type", "f"),
        ("fk_operators_safe", False),
        ("local_columns", (1, 2)),
        ("local_columns", (3,)),
        ("referenced_columns", (3,)),
    ],
)
async def test_unsafe_foreign_key_metadata_is_rejected(
    field: str,
    unsafe_value: object,
) -> None:
    connection = FakeConnection()
    connection.add_relation("widget_groups", 200)
    fk = _safe_fk(100, 200)
    fk[field] = unsafe_value
    connection.constraints[100] = [fk]
    connection.constraints[200] = [fk]
    target = RelationRef("public", "widgets", "target")

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(target, statement_type="update", target=target),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


async def test_restrict_foreign_key_actions_are_safe() -> None:
    connection = FakeConnection()
    connection.add_relation("widget_groups", 200)
    fk = {
        **_safe_fk(100, 200),
        "update_action": "r",
        "delete_action": "r",
    }
    connection.constraints[100] = [fk]
    connection.constraints[200] = [fk]
    target = RelationRef("public", "widgets", "target")

    result = await preflight_and_lock(
        connection,
        _validated(target, statement_type="update", target=target),
        (700,),
    )

    assert {relation.name for relation in result.relations} == {"widgets", "widget_groups"}
    constraint_query = next(
        query for query in connection.raw_queries if "jhin:constraints" in query
    )
    assert "pg_catalog.pg_amop" in constraint_query
    assert "access_operator.amopstrategy = 3" in constraint_query
    assert "operator_access_method.oid = operator_family.opfmethod" in constraint_query
    assert "access_operator.amopmethod" not in constraint_query
    assert "operator_access_method.amname = 'btree'" in constraint_query
    assert "family_namespace.nspname = 'pg_catalog'" in constraint_query
    assert "cardinality(catalog_constraint.conpfeqop)" in constraint_query
    assert "cardinality(catalog_constraint.conffeqop)" in constraint_query
    assert "ORDER BY" not in constraint_query
    assert "LIMIT 65" in constraint_query


@pytest.mark.parametrize("failure", ["cross_schema", "missing_lock_privilege"])
async def test_unsafe_foreign_key_peers_fail_before_lock(failure: str) -> None:
    connection = FakeConnection()
    peer_schema = "private" if failure == "cross_schema" else "public"
    connection.add_relation("widget_groups", 200, schema=peer_schema)
    if failure == "missing_lock_privilege":
        connection.relations[(peer_schema, "widget_groups")]["has_write_lock_privilege"] = False
        connection.relations[(peer_schema, "widget_groups")]["has_maintain_privilege"] = False
    fk = _safe_fk(100, 200)
    connection.constraints[100] = [fk]
    connection.constraints[200] = [fk]
    target = RelationRef("public", "widgets", "target")

    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(target, statement_type="update", target=target),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"
    assert not any(call[0] == "execute" for call in connection.calls)


async def test_update_only_fk_peer_lock_privilege_is_accepted() -> None:
    connection = FakeConnection()
    connection.add_relation("widget_groups", 200)
    connection.relations[("public", "widget_groups")]["has_maintain_privilege"] = False
    connection.relations[("public", "widget_groups")]["has_write_lock_privilege"] = True
    fk = _safe_fk(100, 200)
    connection.constraints[100] = [fk]
    connection.constraints[200] = [fk]
    target = RelationRef("public", "widgets", "target")

    result = await preflight_and_lock(
        connection,
        _validated(target, statement_type="update", target=target),
        (700,),
    )

    assert result.relation_for_oid(200) is not None


async def test_catalog_peer_identifiers_are_safely_quoted_in_lock_sql() -> None:
    connection = FakeConnection()
    connection.add_relation('odd"peer;select', 200)
    fk = _safe_fk(100, 200)
    connection.constraints[100] = [fk]
    connection.constraints[200] = [fk]
    target = RelationRef("public", "widgets", "target")

    await preflight_and_lock(
        connection,
        _validated(target, statement_type="update", target=target),
        (700,),
    )

    locks = [call[1] for call in connection.calls if call[0] == "execute"]
    assert locks[0] == ('LOCK TABLE ONLY "public"."odd""peer;select" IN SHARE ROW EXCLUSIVE MODE')


async def test_post_lock_hidden_default_change_is_rejected() -> None:
    connection = FakeConnection()
    target = RelationRef("public", "widgets", "target")

    def add_default(fake: FakeConnection, lock_count: int) -> None:
        if lock_count == 1:
            fake.columns[100][0]["atthasdef"] = True

    connection.lock_hook = add_default
    with pytest.raises(DatabasePreflightError) as exc_info:
        await preflight_and_lock(
            connection,
            _validated(target, statement_type="insert", target=target),
            (700,),
        )
    assert _error_code(exc_info) == "database_relation_not_allowed"


class _InsufficientPrivilegeError(RuntimeError):
    sqlstate = "42501"


class _TimeoutError(RuntimeError):
    sqlstate = "55P03"


@pytest.mark.parametrize(
    ("error", "mapped"),
    [
        (_InsufficientPrivilegeError("secret provider text"), True),
        (_TimeoutError("timeout"), False),
    ],
)
async def test_lock_privilege_is_sanitized_but_timeout_escapes_for_executor_mapping(
    error: RuntimeError,
    mapped: bool,
) -> None:
    connection = FakeConnection()

    async def fail_lock(query: str, *_args: object) -> str:
        connection.calls.append(("execute", query, ()))
        raise error

    connection.execute = fail_lock  # type: ignore[method-assign]
    if mapped:
        with pytest.raises(DatabasePreflightError) as mapped_error:
            await preflight_and_lock(
                connection,
                _validated(RelationRef("public", "widgets", "source")),
                (700,),
            )
        assert _error_code(mapped_error) == "database_relation_not_allowed"
        assert "secret" not in str(mapped_error.value)
    else:
        with pytest.raises(_TimeoutError) as timeout_error:
            await preflight_and_lock(
                connection,
                _validated(RelationRef("public", "widgets", "source")),
                (700,),
            )
        assert timeout_error.value is error


@pytest.mark.integration
async def test_live_postgres17_safe_fk_catalog_and_inbound_truncate_boundary() -> None:
    dsn = os.getenv("JHIN_PHASE9_DB_WRITER_DSN")
    if not dsn:
        pytest.skip("JHIN_PHASE9_DB_WRITER_DSN is required for the live catalog gate")
    connection = await asyncpg.connect(dsn=dsn, timeout=5, statement_cache_size=0)
    transaction = connection.transaction()
    await transaction.start()
    try:
        driver_row = await connection.fetchrow("SELECT 1 AS value")
        assert isinstance(driver_row, asyncpg.Record)
        assert not isinstance(driver_row, Mapping)
        before_count = await connection.fetchval("SELECT count(*) FROM public.widgets")
        role_oids = await verify_live_role(connection, ("public",))
        validated = classify_and_validate_sql(
            "UPDATE public.widgets SET name = $1 WHERE id = $2",
            expected="destructive",
            requested_schema="public",
        )

        result = await preflight_and_lock(connection, validated, role_oids)

        assert [(relation.name, relation.access) for relation in result.relations] == [
            ("widget_groups", "peer"),
            ("widgets", "target"),
        ]
        assert await verify_live_role(connection, ("public",)) == role_oids

        truncate = classify_and_validate_sql(
            "TRUNCATE TABLE public.widget_groups RESTRICT",
            expected="destructive",
            requested_schema="public",
        )
        with pytest.raises(DatabasePreflightError) as exc_info:
            await preflight_and_lock(connection, truncate, role_oids)
        assert _error_code(exc_info) == "database_relation_not_allowed"
        assert await connection.fetchval("SELECT count(*) FROM public.widgets") == before_count
    finally:
        await transaction.rollback()
        await connection.close()
