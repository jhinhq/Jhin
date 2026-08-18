"""Fail-closed live PostgreSQL catalog preflight for Supabase database tools.

Every query in this module runs on the same connection and transaction that
will execute the submitted statement.  User SQL is never interpolated into a
catalog query or rendered back from its parsed representation.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, NoReturn, Protocol, cast

import asyncpg

from jhin_connectors.supabase.sql_policy import RelationRef, ValidatedSql

MAX_ROLE_CLOSURE = 64
MAX_ALLOWED_SCHEMAS = 8
MAX_RELATIONS = 32
MAX_COLUMNS = 128
MAX_INDEXES = 16
MAX_CONSTRAINTS = 64

PreflightErrorCode = Literal[
    "database_role_not_least_privilege",
    "database_relation_not_allowed",
]
RelationSnapshotAccess = Literal["source", "target", "peer"]

_SAFE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_$]{0,62}\Z")
_PRIVILEGED_ROLE_PREFIX = "pg_"
_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})
_ROLE_ERROR: PreflightErrorCode = "database_role_not_least_privilege"
_RELATION_ERROR: PreflightErrorCode = "database_relation_not_allowed"
_SAFE_COLUMN_TYPES = {
    16: "bool",
    17: "bytea",
    20: "int8",
    21: "int2",
    23: "int4",
    25: "text",
    114: "json",
    700: "float4",
    701: "float8",
    1042: "bpchar",
    1043: "varchar",
    1082: "date",
    1114: "timestamp",
    1184: "timestamptz",
    1700: "numeric",
    2950: "uuid",
    3802: "jsonb",
}


class DatabasePreflightError(ValueError):
    """A stable preflight rejection that never contains provider details."""

    def __init__(
        self,
        message: str = "Supabase database preflight failed",
        *,
        code: PreflightErrorCode,
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, order=True)
class ColumnSnapshot:
    attnum: int
    name: str
    type_oid: int
    type_name: str
    storage: str


@dataclass(frozen=True, order=True)
class RelationSnapshot:
    schema: str
    name: str
    oid: int
    access: RelationSnapshotAccess
    columns: tuple[ColumnSnapshot, ...]


@dataclass(frozen=True)
class PreflightResult:
    relations: tuple[RelationSnapshot, ...]
    target: RelationSnapshot | None

    def relation_for_oid(self, oid: int) -> RelationSnapshot | None:
        """Return a locked relation snapshot without exposing a mutable map."""

        return next((relation for relation in self.relations if relation.oid == oid), None)


class _DatabaseConnection(Protocol):
    async def fetch(self, query: str, *args: object) -> Sequence[Any]: ...

    async def fetchrow(self, query: str, *args: object) -> Any: ...

    async def execute(self, query: str, *args: object) -> str: ...


@dataclass(frozen=True, order=True)
class _ConstraintState:
    oid: int
    kind: str
    relation_oid: int
    referenced_relation_oid: int
    deferrable: bool
    initially_deferred: bool
    validated: bool
    update_action: str
    delete_action: str
    match_type: str
    operators_safe: bool
    local_columns: tuple[int, ...]
    referenced_columns: tuple[int, ...]


@dataclass(frozen=True)
class _RelationState:
    snapshot: RelationSnapshot
    catalog_fingerprint: tuple[object, ...]
    column_fingerprints: tuple[tuple[object, ...], ...]
    index_fingerprints: tuple[tuple[object, ...], ...]
    constraints: tuple[_ConstraintState, ...]
    has_select_privilege: bool
    has_write_lock_privilege: bool
    has_maintain_privilege: bool


@dataclass(frozen=True)
class _PreflightState:
    relations: tuple[_RelationState, ...]
    target_oid: int | None


_ROLE_QUERY = """
/* jhin:role_closure */
WITH RECURSIVE role_closure(role_oid) AS (
    SELECT catalog_role.oid
    FROM pg_catalog.pg_roles AS catalog_role
    WHERE catalog_role.rolname = session_user

    UNION

    SELECT membership.roleid
    FROM role_closure AS closure
    JOIN pg_catalog.pg_auth_members AS membership
      ON membership.member = closure.role_oid
)
SELECT
    catalog_role.oid::bigint AS role_oid,
    catalog_role.rolname::text AS role_name,
    current_user::text AS current_user,
    session_user::text AS session_user,
    catalog_role.rolsuper AS rolsuper,
    catalog_role.rolbypassrls AS rolbypassrls,
    catalog_role.rolcreatedb AS rolcreatedb,
    catalog_role.rolcreaterole AS rolcreaterole,
    catalog_role.rolreplication AS rolreplication,
    catalog_database.datdba = catalog_role.oid AS owns_current_database,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS owned_namespace
        WHERE owned_namespace.nspname = ANY($1::text[])
          AND owned_namespace.nspowner = catalog_role.oid
    ) AS owns_allowed_schema,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS writable_namespace
        WHERE writable_namespace.nspname = ANY($1::text[])
          AND pg_catalog.has_schema_privilege(
              catalog_role.oid,
              writable_namespace.oid,
              'CREATE'
          )
    ) AS can_create_in_allowed_schema,
    current_setting('server_encoding')::text AS server_encoding,
    current_setting('session_replication_role')::text AS session_replication_role,
    (
        SELECT count(*)::integer
        FROM pg_catalog.pg_namespace AS allowed_namespace
        WHERE allowed_namespace.nspname = ANY($1::text[])
    ) AS allowed_schema_count
FROM role_closure AS closure
JOIN pg_catalog.pg_roles AS catalog_role ON catalog_role.oid = closure.role_oid
JOIN pg_catalog.pg_database AS catalog_database
  ON catalog_database.datname = current_database()
LIMIT 65
"""

_RELATION_SELECT = """
SELECT
    relation.oid::bigint AS oid,
    namespace.nspname::text AS schema_name,
    relation.relname::text AS relation_name,
    relation.relkind::text AS relkind,
    relation.relpersistence::text AS relpersistence,
    relation.relispartition AS relispartition,
    access_method.amname::text AS table_am,
    relation.relowner::bigint AS owner_oid,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_inherits AS inheritance
        WHERE inheritance.inhrelid = relation.oid
           OR inheritance.inhparent = relation.oid
    ) AS has_inheritance,
    relation.relrowsecurity AS relrowsecurity,
    relation.relforcerowsecurity AS relforcerowsecurity,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_policy AS policy
        WHERE policy.polrelid = relation.oid
    ) AS has_policies,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_rewrite AS rewrite
        WHERE rewrite.ev_class = relation.oid
          AND rewrite.rulename <> '_RETURN'
    ) AS has_rules,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_trigger AS catalog_trigger
        WHERE catalog_trigger.tgrelid = relation.oid
          AND NOT catalog_trigger.tgisinternal
    ) AS has_user_triggers,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_trigger AS internal_trigger
        LEFT JOIN pg_catalog.pg_constraint AS trigger_constraint
          ON trigger_constraint.oid = internal_trigger.tgconstraint
        WHERE internal_trigger.tgrelid = relation.oid
          AND internal_trigger.tgisinternal
          AND (
              internal_trigger.tgconstraint = 0
              OR trigger_constraint.oid IS NULL
              OR trigger_constraint.contype <> 'f'
          )
    ) AS has_unsafe_internal_triggers,
    pg_catalog.has_table_privilege(relation.oid, 'SELECT') AS has_select_privilege,
    (
        pg_catalog.has_table_privilege(relation.oid, 'MAINTAIN')
        OR pg_catalog.has_table_privilege(relation.oid, 'UPDATE')
        OR pg_catalog.has_table_privilege(relation.oid, 'DELETE')
        OR pg_catalog.has_table_privilege(relation.oid, 'TRUNCATE')
    ) AS has_write_lock_privilege,
    pg_catalog.has_table_privilege(relation.oid, 'MAINTAIN') AS has_maintain_privilege
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
LEFT JOIN pg_catalog.pg_am AS access_method ON access_method.oid = relation.relam
"""

_RELATION_BY_NAME_QUERY = (
    "/* jhin:relation_by_name */\n"
    + _RELATION_SELECT
    + "WHERE namespace.nspname = $1::text AND relation.relname = $2::text\n"
)

_RELATION_BY_OID_QUERY = (
    "/* jhin:relation_by_oid */\n" + _RELATION_SELECT + "WHERE relation.oid = $1::oid\n"
)

_COLUMN_QUERY = """
/* jhin:columns */
SELECT
    attribute.attnum::integer AS attnum,
    attribute.attname::text AS attname,
    attribute.attstorage::text AS attstorage,
    attribute.atthasdef AS atthasdef,
    attribute.attidentity::text AS attidentity,
    attribute.attgenerated::text AS attgenerated,
    type.oid::bigint AS type_oid,
    type.typname::text AS type_name,
    type_namespace.nspname::text AS type_schema,
    type.typtype::text AS type_kind,
    type.typelem::bigint AS type_element,
    collation_namespace.nspname::text AS collation_schema
FROM pg_catalog.pg_attribute AS attribute
JOIN pg_catalog.pg_type AS type ON type.oid = attribute.atttypid
JOIN pg_catalog.pg_namespace AS type_namespace ON type_namespace.oid = type.typnamespace
LEFT JOIN pg_catalog.pg_collation AS catalog_collation
  ON catalog_collation.oid = attribute.attcollation
LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
  ON collation_namespace.oid = catalog_collation.collnamespace
WHERE attribute.attrelid = $1::oid
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
LIMIT 129
"""

_INDEX_QUERY = """
/* jhin:indexes */
SELECT
    catalog_index.indexrelid::bigint AS index_oid,
    catalog_index.indisvalid AS valid,
    catalog_index.indisready AS ready,
    catalog_index.indislive AS live,
    catalog_index.indnullsnotdistinct AS nulls_not_distinct,
    catalog_index.indisexclusion AS exclusion,
    index_relation.reloptions IS NOT NULL AS has_reloptions,
    catalog_index.indexprs IS NOT NULL AS has_expressions,
    catalog_index.indpred IS NOT NULL AS has_predicate,
    catalog_index.indnatts::integer AS attribute_count,
    catalog_index.indnkeyatts::integer AS key_attribute_count,
    catalog_index.indkey::smallint[] AS key_columns,
    NOT (0 = ANY(catalog_index.indkey::smallint[])) AS keys_are_columns,
    NOT EXISTS (
        SELECT 1
        FROM unnest(catalog_index.indclass::oid[]) AS selected_opclass(opclass_oid)
        LEFT JOIN pg_catalog.pg_opclass AS opclass
          ON opclass.oid = selected_opclass.opclass_oid
        LEFT JOIN pg_catalog.pg_namespace AS opclass_namespace
          ON opclass_namespace.oid = opclass.opcnamespace
        WHERE opclass.oid IS NULL OR opclass_namespace.nspname <> 'pg_catalog'
    ) AS operator_classes_safe,
    NOT EXISTS (
        SELECT 1
        FROM unnest(catalog_index.indcollation::oid[])
          AS selected_collation(collation_oid)
        LEFT JOIN pg_catalog.pg_collation AS catalog_collation
          ON catalog_collation.oid = selected_collation.collation_oid
        LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
          ON collation_namespace.oid = catalog_collation.collnamespace
        WHERE selected_collation.collation_oid <> 0
          AND (
              catalog_collation.oid IS NULL
              OR collation_namespace.nspname <> 'pg_catalog'
          )
    ) AS index_collations_safe,
    access_method.amname = 'btree' AS access_method_safe
FROM pg_catalog.pg_index AS catalog_index
JOIN pg_catalog.pg_class AS index_relation
  ON index_relation.oid = catalog_index.indexrelid
JOIN pg_catalog.pg_am AS access_method ON access_method.oid = index_relation.relam
WHERE catalog_index.indrelid = $1::oid
LIMIT 17
"""

_CONSTRAINT_QUERY = """
/* jhin:constraints */
SELECT
    catalog_constraint.oid::bigint AS constraint_oid,
    catalog_constraint.contype::text AS constraint_type,
    catalog_constraint.conrelid::bigint AS relation_oid,
    catalog_constraint.confrelid::bigint AS referenced_relation_oid,
    catalog_constraint.condeferrable AS deferrable,
    catalog_constraint.condeferred AS initially_deferred,
    catalog_constraint.convalidated AS validated,
    catalog_constraint.confupdtype::text AS update_action,
    catalog_constraint.confdeltype::text AS delete_action,
    catalog_constraint.confmatchtype::text AS match_type,
    CASE
        WHEN catalog_constraint.contype = 'f' THEN
            cardinality(catalog_constraint.conpfeqop)
              = cardinality(catalog_constraint.conkey)
            AND cardinality(catalog_constraint.conppeqop)
              = cardinality(catalog_constraint.conkey)
            AND cardinality(catalog_constraint.conffeqop)
              = cardinality(catalog_constraint.confkey)
            AND NOT EXISTS (
                SELECT 1
                FROM unnest(
                    catalog_constraint.conpfeqop
                    || catalog_constraint.conppeqop
                    || catalog_constraint.conffeqop
                ) AS selected_operator(operator_oid)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_amop AS access_operator
                    JOIN pg_catalog.pg_opfamily AS operator_family
                      ON operator_family.oid = access_operator.amopfamily
                    JOIN pg_catalog.pg_namespace AS family_namespace
                      ON family_namespace.oid = operator_family.opfnamespace
                    JOIN pg_catalog.pg_am AS operator_access_method
                      ON operator_access_method.oid = operator_family.opfmethod
                    JOIN pg_catalog.pg_operator AS catalog_operator
                      ON catalog_operator.oid = access_operator.amopopr
                    JOIN pg_catalog.pg_namespace AS operator_namespace
                      ON operator_namespace.oid = catalog_operator.oprnamespace
                    WHERE access_operator.amopopr = selected_operator.operator_oid
                      AND access_operator.amopstrategy = 3
                      AND access_operator.amoppurpose = 's'
                      AND operator_access_method.amname = 'btree'
                      AND family_namespace.nspname = 'pg_catalog'
                      AND operator_namespace.nspname = 'pg_catalog'
                )
            )
        ELSE true
    END AS fk_operators_safe,
    catalog_constraint.conkey::smallint[] AS local_columns,
    catalog_constraint.confkey::smallint[] AS referenced_columns
FROM pg_catalog.pg_constraint AS catalog_constraint
WHERE catalog_constraint.conrelid = $1::oid
   OR catalog_constraint.confrelid = $1::oid
LIMIT 65
"""


def _reject_role() -> NoReturn:
    raise DatabasePreflightError(code=_ROLE_ERROR) from None


def _reject_relation() -> NoReturn:
    raise DatabasePreflightError(code=_RELATION_ERROR) from None


def _row_mapping(row: object) -> Mapping[str, object]:
    if not isinstance(row, (Mapping, asyncpg.Record)):
        _reject_relation()
    return cast(Mapping[str, object], row)


def _role_row_mapping(row: object) -> Mapping[str, object]:
    if not isinstance(row, (Mapping, asyncpg.Record)):
        _reject_role()
    return cast(Mapping[str, object], row)


def _integer(value: object, *, role: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        if role:
            _reject_role()
        _reject_relation()
    return value


def _text(value: object, *, role: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        if role:
            _reject_role()
        _reject_relation()
    return value


def _boolean(value: object, *, role: bool = False) -> bool:
    if not isinstance(value, bool):
        if role:
            _reject_role()
        _reject_relation()
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _integer_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        _reject_relation()
    result = tuple(_integer(item) for item in value)
    if not result or len(result) != len(set(result)):
        _reject_relation()
    return result


async def verify_live_role(
    conn: _DatabaseConnection,
    allowed_schemas: tuple[str, ...],
) -> tuple[int, ...]:
    """Verify the live session and all recursively reachable role ancestors."""

    if (
        not isinstance(allowed_schemas, tuple)
        or not 1 <= len(allowed_schemas) <= MAX_ALLOWED_SCHEMAS
        or len(set(allowed_schemas)) != len(allowed_schemas)
        or any(
            not isinstance(schema, str)
            or not _SAFE_IDENTIFIER.fullmatch(schema)
            or schema in _SYSTEM_SCHEMAS
            or schema.startswith("pg_")
            for schema in allowed_schemas
        )
    ):
        _reject_role()

    rows = await conn.fetch(_ROLE_QUERY, list(allowed_schemas))
    if not isinstance(rows, Sequence) or not 1 <= len(rows) <= MAX_ROLE_CLOSURE:
        _reject_role()

    role_oids: list[int] = []
    role_names: set[str] = set()
    session_name: str | None = None
    for raw_row in rows:
        row = _role_row_mapping(raw_row)
        role_oid = _integer(row.get("role_oid"), role=True)
        role_name = _text(row.get("role_name"), role=True)
        current_user = _text(row.get("current_user"), role=True)
        session_user = _text(row.get("session_user"), role=True)
        if current_user != session_user:
            _reject_role()
        if session_name is None:
            session_name = session_user
        elif session_user != session_name:
            _reject_role()

        normalized_name = role_name.casefold()
        if normalized_name == "postgres" or normalized_name.startswith(_PRIVILEGED_ROLE_PREFIX):
            _reject_role()
        if any(
            _boolean(row.get(field), role=True)
            for field in (
                "rolsuper",
                "rolbypassrls",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "owns_current_database",
                "owns_allowed_schema",
                "can_create_in_allowed_schema",
            )
        ):
            _reject_role()
        if row.get("server_encoding") != "UTF8":
            _reject_role()
        if row.get("session_replication_role") != "origin":
            _reject_role()
        allowed_schema_count = row.get("allowed_schema_count")
        if (
            not isinstance(allowed_schema_count, int)
            or isinstance(allowed_schema_count, bool)
            or allowed_schema_count != len(allowed_schemas)
        ):
            _reject_role()
        if role_oid in role_oids or normalized_name in role_names:
            _reject_role()
        role_oids.append(role_oid)
        role_names.add(normalized_name)

    if session_name is None or session_name.casefold() not in role_names:
        _reject_role()
    return tuple(sorted(role_oids))


def _validate_role_oids(role_oids: tuple[int, ...]) -> frozenset[int]:
    if (
        not isinstance(role_oids, tuple)
        or not 1 <= len(role_oids) <= MAX_ROLE_CLOSURE
        or len(role_oids) != len(set(role_oids))
        or any(not isinstance(oid, int) or isinstance(oid, bool) or oid <= 0 for oid in role_oids)
    ):
        _reject_role()
    return frozenset(role_oids)


def _column_state(
    row: Mapping[str, object],
    *,
    effect_target: bool,
) -> tuple[ColumnSnapshot, tuple[object, ...]]:
    attnum = _integer(row.get("attnum"))
    name = _text(row.get("attname"))
    storage = _text(row.get("attstorage"))
    has_default = _boolean(row.get("atthasdef"))
    identity = row.get("attidentity")
    generated = row.get("attgenerated")
    if not isinstance(identity, str) or not isinstance(generated, str):
        _reject_relation()
    type_oid = _integer(row.get("type_oid"))
    type_name = _text(row.get("type_name"))
    type_schema = _text(row.get("type_schema"))
    type_kind = _text(row.get("type_kind"))
    type_element = row.get("type_element")
    if not isinstance(type_element, int) or isinstance(type_element, bool) or type_element < 0:
        _reject_relation()
    collation_schema = _optional_text(row.get("collation_schema"))
    if (
        storage not in {"p", "m", "x", "e"}
        or type_schema != "pg_catalog"
        or type_kind != "b"
        or type_element != 0
        or _SAFE_COLUMN_TYPES.get(type_oid) != type_name
        or collation_schema not in {None, "pg_catalog"}
        or (effect_target and (has_default or bool(identity) or bool(generated)))
    ):
        _reject_relation()
    snapshot = ColumnSnapshot(
        attnum=attnum,
        name=name,
        type_oid=type_oid,
        type_name=type_name,
        storage=storage,
    )
    return snapshot, (
        attnum,
        name,
        storage,
        has_default,
        identity,
        generated,
        type_oid,
        type_name,
        type_schema,
        type_kind,
        type_element,
        collation_schema,
    )


def _index_fingerprint(row: Mapping[str, object]) -> tuple[object, ...]:
    index_oid = _integer(row.get("index_oid"))
    valid = _boolean(row.get("valid"))
    ready = _boolean(row.get("ready"))
    live = _boolean(row.get("live"))
    nulls_not_distinct = _boolean(row.get("nulls_not_distinct"))
    exclusion = _boolean(row.get("exclusion"))
    has_reloptions = _boolean(row.get("has_reloptions"))
    has_expressions = _boolean(row.get("has_expressions"))
    has_predicate = _boolean(row.get("has_predicate"))
    attribute_count = _integer(row.get("attribute_count"))
    key_attribute_count = _integer(row.get("key_attribute_count"))
    key_columns = _integer_tuple(row.get("key_columns"))
    keys_are_columns = _boolean(row.get("keys_are_columns"))
    operator_classes_safe = _boolean(row.get("operator_classes_safe"))
    index_collations_safe = _boolean(row.get("index_collations_safe"))
    access_method_safe = _boolean(row.get("access_method_safe"))
    if (
        not valid
        or not ready
        or not live
        or nulls_not_distinct
        or exclusion
        or has_reloptions
        or has_expressions
        or has_predicate
        or attribute_count != key_attribute_count
        or len(key_columns) != key_attribute_count
        or not keys_are_columns
        or not operator_classes_safe
        or not index_collations_safe
        or not access_method_safe
    ):
        _reject_relation()
    return (
        index_oid,
        valid,
        ready,
        live,
        nulls_not_distinct,
        exclusion,
        has_reloptions,
        has_expressions,
        has_predicate,
        attribute_count,
        key_attribute_count,
        keys_are_columns,
        operator_classes_safe,
        index_collations_safe,
        access_method_safe,
        key_columns,
    )


def _constraint_state(row: Mapping[str, object]) -> _ConstraintState:
    kind = _text(row.get("constraint_type"))
    if kind not in {"c", "f", "p", "t", "u", "x"}:
        _reject_relation()
    relation_oid = _integer(row.get("relation_oid"))
    referenced_raw = row.get("referenced_relation_oid")
    if (
        not isinstance(referenced_raw, int)
        or isinstance(referenced_raw, bool)
        or referenced_raw < 0
    ):
        _reject_relation()
    deferrable = _boolean(row.get("deferrable"))
    initially_deferred = _boolean(row.get("initially_deferred"))
    validated = _boolean(row.get("validated"))
    update_action_raw = row.get("update_action")
    delete_action_raw = row.get("delete_action")
    match_type_raw = row.get("match_type")
    operators_safe = _boolean(row.get("fk_operators_safe"))
    local_raw = row.get("local_columns")
    referenced_columns_raw = row.get("referenced_columns")
    if kind == "f":
        update_action = _text(update_action_raw)
        delete_action = _text(delete_action_raw)
        match_type = _text(match_type_raw)
        local_columns = _integer_tuple(local_raw)
        referenced_columns = _integer_tuple(referenced_columns_raw)
        if referenced_raw == 0 or len(local_columns) != len(referenced_columns):
            _reject_relation()
    else:
        update_action = update_action_raw if isinstance(update_action_raw, str) else ""
        delete_action = delete_action_raw if isinstance(delete_action_raw, str) else ""
        match_type = match_type_raw if isinstance(match_type_raw, str) else ""
        local_columns = ()
        referenced_columns = ()
    return _ConstraintState(
        oid=_integer(row.get("constraint_oid")),
        kind=kind,
        relation_oid=relation_oid,
        referenced_relation_oid=referenced_raw,
        deferrable=deferrable,
        initially_deferred=initially_deferred,
        validated=validated,
        update_action=update_action,
        delete_action=delete_action,
        match_type=match_type,
        operators_safe=operators_safe,
        local_columns=local_columns,
        referenced_columns=referenced_columns,
    )


async def _inspect_relation(
    conn: _DatabaseConnection,
    row: object,
    *,
    expected_schema: str | None,
    expected_name: str | None,
    expected_oid: int | None,
    access: RelationSnapshotAccess,
    role_oids: frozenset[int],
    effect_target: bool,
) -> _RelationState:
    relation = _row_mapping(row)
    oid = _integer(relation.get("oid"))
    schema = _text(relation.get("schema_name"))
    name = _text(relation.get("relation_name"))
    if (
        (expected_schema is not None and schema != expected_schema)
        or (expected_name is not None and name != expected_name)
        or (expected_oid is not None and oid != expected_oid)
    ):
        _reject_relation()

    owner_oid = _integer(relation.get("owner_oid"))
    if owner_oid in role_oids:
        _reject_role()
    relkind = _text(relation.get("relkind"))
    persistence = _text(relation.get("relpersistence"))
    is_partition = _boolean(relation.get("relispartition"))
    table_am = _text(relation.get("table_am"))
    has_inheritance = _boolean(relation.get("has_inheritance"))
    row_security = _boolean(relation.get("relrowsecurity"))
    force_row_security = _boolean(relation.get("relforcerowsecurity"))
    has_policies = _boolean(relation.get("has_policies"))
    has_rules = _boolean(relation.get("has_rules"))
    has_user_triggers = _boolean(relation.get("has_user_triggers"))
    has_unsafe_internal_triggers = _boolean(relation.get("has_unsafe_internal_triggers"))
    has_select_privilege = _boolean(relation.get("has_select_privilege"))
    has_write_lock_privilege = _boolean(relation.get("has_write_lock_privilege"))
    has_maintain_privilege = _boolean(relation.get("has_maintain_privilege"))
    if (
        relkind != "r"
        or persistence != "p"
        or is_partition
        or table_am != "heap"
        or has_inheritance
        or row_security
        or force_row_security
        or has_policies
        or (effect_target and (has_rules or has_user_triggers or has_unsafe_internal_triggers))
        or (access == "source" and not has_select_privilege)
        or (access == "target" and not has_write_lock_privilege)
        or (access == "peer" and not has_write_lock_privilege)
    ):
        _reject_relation()

    raw_columns = await conn.fetch(_COLUMN_QUERY, oid)
    if not isinstance(raw_columns, Sequence) or len(raw_columns) > MAX_COLUMNS:
        _reject_relation()
    columns_and_fingerprints = tuple(
        sorted(
            (
                _column_state(_row_mapping(column), effect_target=effect_target)
                for column in raw_columns
            ),
            key=lambda value: value[0].attnum,
        )
    )
    columns = tuple(value[0] for value in columns_and_fingerprints)
    column_fingerprints = tuple(value[1] for value in columns_and_fingerprints)
    if len({column.attnum for column in columns}) != len(columns) or len(
        {column.name for column in columns}
    ) != len(columns):
        _reject_relation()

    raw_indexes = await conn.fetch(_INDEX_QUERY, oid)
    if not isinstance(raw_indexes, Sequence) or len(raw_indexes) > MAX_INDEXES:
        _reject_relation()
    index_fingerprints = tuple(
        sorted(_index_fingerprint(_row_mapping(index)) for index in raw_indexes)
    )
    if len({fingerprint[0] for fingerprint in index_fingerprints}) != len(index_fingerprints):
        _reject_relation()
    column_attnums = {column.attnum for column in columns}
    if any(
        not set(cast(tuple[int, ...], fingerprint[-1])).issubset(column_attnums)
        for fingerprint in index_fingerprints
    ):
        _reject_relation()

    raw_constraints = await conn.fetch(_CONSTRAINT_QUERY, oid)
    if not isinstance(raw_constraints, Sequence) or len(raw_constraints) > MAX_CONSTRAINTS:
        _reject_relation()
    constraints = tuple(
        sorted(_constraint_state(_row_mapping(constraint)) for constraint in raw_constraints)
    )
    if len({constraint.oid for constraint in constraints}) != len(constraints):
        _reject_relation()
    if effect_target:
        if any(constraint.kind in {"c", "t", "x"} for constraint in constraints):
            _reject_relation()
        if any(
            constraint.kind in {"p", "u"}
            and (constraint.deferrable or constraint.initially_deferred or not constraint.validated)
            for constraint in constraints
        ):
            _reject_relation()

    snapshot = RelationSnapshot(
        schema=schema,
        name=name,
        oid=oid,
        access=access,
        columns=columns,
    )
    fingerprint = (
        oid,
        schema,
        name,
        relkind,
        persistence,
        is_partition,
        table_am,
        owner_oid,
        has_inheritance,
        row_security,
        force_row_security,
        has_policies,
        has_rules,
        has_user_triggers,
        has_unsafe_internal_triggers,
        has_select_privilege,
        has_write_lock_privilege,
        has_maintain_privilege,
    )
    return _RelationState(
        snapshot=snapshot,
        catalog_fingerprint=fingerprint,
        column_fingerprints=column_fingerprints,
        index_fingerprints=index_fingerprints,
        constraints=constraints,
        has_select_privilege=has_select_privilege,
        has_write_lock_privilege=has_write_lock_privilege,
        has_maintain_privilege=has_maintain_privilege,
    )


async def _inspect_by_name(
    conn: _DatabaseConnection,
    ref: RelationRef,
    *,
    access: RelationSnapshotAccess,
    role_oids: frozenset[int],
    effect_target: bool,
) -> _RelationState:
    row = await conn.fetchrow(_RELATION_BY_NAME_QUERY, ref.schema, ref.name)
    if row is None:
        _reject_relation()
    return await _inspect_relation(
        conn,
        row,
        expected_schema=ref.schema,
        expected_name=ref.name,
        expected_oid=None,
        access=access,
        role_oids=role_oids,
        effect_target=effect_target,
    )


async def _inspect_by_oid(
    conn: _DatabaseConnection,
    oid: int,
    *,
    access: RelationSnapshotAccess,
    role_oids: frozenset[int],
) -> _RelationState:
    row = await conn.fetchrow(_RELATION_BY_OID_QUERY, oid)
    if row is None:
        _reject_relation()
    return await _inspect_relation(
        conn,
        row,
        expected_schema=None,
        expected_name=None,
        expected_oid=oid,
        access=access,
        role_oids=role_oids,
        effect_target=False,
    )


def _normalized_explicit_refs(validated: ValidatedSql) -> dict[tuple[str, str], RelationRef]:
    if not isinstance(validated, ValidatedSql) or not isinstance(validated.relations, tuple):
        _reject_relation()
    normalized: dict[tuple[str, str], RelationRef] = {}
    for ref in validated.relations:
        if (
            not isinstance(ref, RelationRef)
            or ref.access not in {"source", "target"}
            or not isinstance(ref.schema, str)
            or not isinstance(ref.name, str)
            or not ref.schema
            or not ref.name
            or "\x00" in ref.schema
            or "\x00" in ref.name
        ):
            _reject_relation()
        key = (ref.schema, ref.name)
        previous = normalized.get(key)
        if previous is None or ref.access == "target":
            normalized[key] = ref
    if len(normalized) > MAX_RELATIONS:
        _reject_relation()
    target = validated.mutation_target
    if target is None:
        if (
            validated.sql_class != "read"
            or validated.statement_type != "select"
            or any(ref.access == "target" for ref in normalized.values())
        ):
            _reject_relation()
    else:
        expected_class = "write" if validated.statement_type == "insert" else "destructive"
        if (
            not isinstance(target, RelationRef)
            or target.access != "target"
            or validated.statement_type not in {"insert", "update", "delete", "truncate"}
            or validated.sql_class != expected_class
            or normalized.get((target.schema, target.name)) != target
        ):
            _reject_relation()
    return normalized


def _validate_safe_fk(constraint: _ConstraintState) -> None:
    if (
        constraint.deferrable
        or constraint.initially_deferred
        or not constraint.validated
        or constraint.update_action not in {"a", "r"}
        or constraint.delete_action not in {"a", "r"}
        or constraint.match_type != "s"
        or not constraint.operators_safe
        or not constraint.local_columns
        or len(constraint.local_columns) != len(constraint.referenced_columns)
    ):
        _reject_relation()


async def _build_state(
    conn: _DatabaseConnection,
    validated: ValidatedSql,
    role_oids: frozenset[int],
) -> _PreflightState:
    refs = _normalized_explicit_refs(validated)
    target_ref = validated.mutation_target
    states: dict[int, _RelationState] = {}
    keys: dict[tuple[str, str], int] = {}

    for key, ref in sorted(refs.items()):
        access: RelationSnapshotAccess = ref.access
        effect_target = target_ref is not None and key == (target_ref.schema, target_ref.name)
        state = await _inspect_by_name(
            conn,
            ref,
            access=access,
            role_oids=role_oids,
            effect_target=effect_target,
        )
        oid = state.snapshot.oid
        if oid in states or key in keys:
            _reject_relation()
        states[oid] = state
        keys[key] = oid

    target_oid = None
    fk_closure: set[int] = set()
    if target_ref is not None:
        target_oid = keys.get((target_ref.schema, target_ref.name))
        if target_oid is None:
            _reject_relation()
        pending = [target_oid]
        visited: set[int] = set()
        while pending:
            current_oid = pending.pop(0)
            if current_oid in visited:
                continue
            visited.add(current_oid)
            current = states[current_oid]
            for constraint in current.constraints:
                if constraint.kind != "f":
                    continue
                if (
                    validated.statement_type == "truncate"
                    and constraint.referenced_relation_oid == target_oid
                    and constraint.relation_oid != target_oid
                ):
                    _reject_relation()
                _validate_safe_fk(constraint)
                if constraint.relation_oid == current_oid:
                    peer_oid = constraint.referenced_relation_oid
                elif constraint.referenced_relation_oid == current_oid:
                    peer_oid = constraint.relation_oid
                else:
                    _reject_relation()
                if peer_oid == current_oid:
                    continue
                peer = states.get(peer_oid)
                if peer is None:
                    if len(states) >= MAX_RELATIONS:
                        _reject_relation()
                    peer = await _inspect_by_oid(
                        conn,
                        peer_oid,
                        access="peer",
                        role_oids=role_oids,
                    )
                    peer_key = (peer.snapshot.schema, peer.snapshot.name)
                    if peer.snapshot.schema != target_ref.schema or peer_key in keys:
                        _reject_relation()
                    if not peer.has_write_lock_privilege:
                        _reject_relation()
                    states[peer_oid] = peer
                    keys[peer_key] = peer_oid
                elif peer_oid != target_oid and peer.snapshot.access == "source":
                    if not peer.has_write_lock_privilege:
                        _reject_relation()
                    peer = replace(
                        peer,
                        snapshot=replace(peer.snapshot, access="peer"),
                    )
                    states[peer_oid] = peer
                if peer.snapshot.schema != target_ref.schema:
                    _reject_relation()
                pending.append(peer_oid)
        fk_closure = visited

    for closure_oid in fk_closure:
        for constraint in states[closure_oid].constraints:
            if constraint.kind != "f":
                continue
            child = states.get(constraint.relation_oid)
            parent = states.get(constraint.referenced_relation_oid)
            if child is None or parent is None:
                _reject_relation()
            child_attnums = {column.attnum for column in child.snapshot.columns}
            parent_attnums = {column.attnum for column in parent.snapshot.columns}
            if not set(constraint.local_columns).issubset(child_attnums) or not set(
                constraint.referenced_columns
            ).issubset(parent_attnums):
                _reject_relation()

    ordered = tuple(
        sorted(
            states.values(),
            key=lambda state: (
                state.snapshot.schema,
                state.snapshot.name,
                state.snapshot.oid,
            ),
        )
    )
    if len(ordered) > MAX_RELATIONS:
        _reject_relation()
    return _PreflightState(relations=ordered, target_oid=target_oid)


def _quote_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        _reject_relation()
    return '"' + identifier.replace('"', '""') + '"'


def _lock_statement(relation: RelationSnapshot, statement_type: str) -> str:
    if relation.access == "source":
        mode = "ACCESS SHARE"
    elif relation.access == "target" and statement_type == "truncate":
        mode = "ACCESS EXCLUSIVE"
    else:
        mode = "SHARE ROW EXCLUSIVE"
    qualified = f"{_quote_identifier(relation.schema)}.{_quote_identifier(relation.name)}"
    return f"LOCK TABLE ONLY {qualified} IN {mode} MODE"


async def _lock_relations(
    conn: _DatabaseConnection,
    state: _PreflightState,
    statement_type: str,
) -> None:
    for relation in state.relations:
        try:
            await conn.execute(_lock_statement(relation.snapshot, statement_type))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42501":
                _reject_relation()
            raise


def _public_result(state: _PreflightState) -> PreflightResult:
    relations = tuple(relation.snapshot for relation in state.relations)
    target = next(
        (relation for relation in relations if relation.oid == state.target_oid),
        None,
    )
    return PreflightResult(relations=relations, target=target)


async def preflight_and_lock(
    conn: _DatabaseConnection,
    validated: ValidatedSql,
    role_oids: tuple[int, ...],
) -> PreflightResult:
    """Inspect, deterministically lock, and recheck every referenced relation."""

    # PostgreSQL relation locks do not serialize an external owner renaming and
    # recreating an allowed schema. Verified Jhin roles/ancestors therefore may
    # not own or CREATE there, and trusted operators must avoid concurrent
    # namespace DDL while a database tool is executing.
    safe_role_oids = _validate_role_oids(role_oids)
    before_lock = await _build_state(conn, validated, safe_role_oids)
    await _lock_relations(conn, before_lock, validated.statement_type)
    after_lock = await _build_state(conn, validated, safe_role_oids)
    if after_lock != before_lock:
        _reject_relation()
    return _public_result(after_lock)


__all__ = [
    "MAX_ALLOWED_SCHEMAS",
    "MAX_COLUMNS",
    "MAX_CONSTRAINTS",
    "MAX_INDEXES",
    "MAX_RELATIONS",
    "MAX_ROLE_CLOSURE",
    "ColumnSnapshot",
    "DatabasePreflightError",
    "PreflightResult",
    "RelationSnapshot",
    "preflight_and_lock",
    "verify_live_role",
]
