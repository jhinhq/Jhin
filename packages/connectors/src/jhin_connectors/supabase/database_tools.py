"""Fixed-risk, bounded Supabase PostgreSQL tool contracts and executors."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, cast

import asyncpg
from pydantic import BaseModel
from sqlglot import exp, parse_one

from jhin_connectors.endpoints import EndpointPolicyError, validate_postgres_target
from jhin_connectors.execution import ConnectionResolutionError, resolve_connection
from jhin_connectors.supabase.database_client import _asyncpg_dsn, _hosted_ssl_context
from jhin_connectors.supabase.schemas import (
    DatabaseMutationInput,
    DatabaseMutationOutput,
    DatabaseReadInput,
    DatabaseReadOutput,
)
from jhin_connectors.supabase.sql_policy import (
    MAX_MUTATION_VALUE_BYTES,
    SqlClass,
    SqlPolicyError,
    ValidatedSql,
    _parser_safe_sql,
    classify_and_validate_sql,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor
from jhin_tools.sanitize import (
    MAX_DOCUMENT_BYTES,
    MAX_STRING_CHARS,
    TRUNCATION_MARKER,
    sanitize_payload,
)

MAX_OUTPUT_COLUMNS = 64
MAX_PREFLIGHT_RELATIONS = 32
DATABASE_CONNECT_TIMEOUT_SECONDS = 5.0
DATABASE_CLEANUP_TIMEOUT_SECONDS = 2.0

_PROJECT_REF_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_$]{0,62}$")
_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})
_TRUSTED_GUCS: tuple[tuple[str, str], ...] = (
    ("search_path", "TO pg_catalog"),
    ("standard_conforming_strings", "= on"),
    ("row_security", "= on"),
    ("work_mem", "= '1MB'"),
    ("hash_mem_multiplier", "= 1.0"),
    ("temp_file_limit", "= '16MB'"),
    ("max_parallel_workers_per_gather", "= 0"),
    ("jit", "= off"),
    ("enable_seqscan", "= on"),
    ("enable_indexscan", "= off"),
    ("enable_indexonlyscan", "= off"),
    ("enable_bitmapscan", "= off"),
)
_TRUSTED_GUC_VALUES: dict[str, tuple[str, str | None]] = {
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
_GUC_DISCOVERY_QUERY = """
/* jhin:guc_discovery */
SELECT
    setting.name::text AS name,
    pg_catalog.current_setting('server_version_num')::integer AS server_version_num,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_settings AS capability
        WHERE capability.name = 'jit'
    ) AS jit_available
FROM pg_catalog.pg_settings AS setting
WHERE setting.name = ANY($1::text[])
ORDER BY setting.name
LIMIT 13
"""
_GUC_VERIFY_QUERY = """
/* jhin:guc_verify */
SELECT name::text AS name, setting::text AS setting, unit::text AS unit
FROM pg_catalog.pg_settings
WHERE name = ANY($1::text[])
ORDER BY name
LIMIT 16
"""
_FIXED_OUTPUT_TYPES: dict[int, str] = {
    16: "bool",
    20: "int8",
    21: "int2",
    23: "int4",
    700: "float4",
    701: "float8",
    1082: "date",
    1114: "timestamp",
    1184: "timestamptz",
    2950: "uuid",
}
_TEXT_OUTPUT_TYPES = {25: "text", 1043: "varchar"}


@dataclass(frozen=True)
class _ExecutionConfig:
    project_ref: str
    allowed_schemas: tuple[str, ...]
    allow_writes: bool
    statement_timeout_ms: int
    lock_timeout_ms: int
    max_rows: int
    max_cell_bytes: int
    max_result_bytes: int


@dataclass(frozen=True)
class _CleanupOutcome:
    cancellation: asyncio.CancelledError | None
    failed: bool


class SupabaseDatabaseError(Exception):
    """A stable, credential-free database execution failure."""

    def __init__(self, message: str, *, code: str = "database_execution_failed") -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, message: str = "Supabase database execution failed") -> NoReturn:
    raise SupabaseDatabaseError(message, code=code) from None


def _bounded_int(
    config: Mapping[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _error("invalid_configuration", "Supabase database configuration is invalid")
    return value


def _execution_config(config: Mapping[str, Any]) -> _ExecutionConfig:
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
    if set(config) - allowed_fields:
        _error("invalid_configuration", "Supabase database configuration is invalid")
    project_ref = config.get("project_ref")
    if not isinstance(project_ref, str) or not _PROJECT_REF_RE.fullmatch(project_ref):
        _error("invalid_configuration", "Supabase database configuration is invalid")
    raw_schemas = config.get("allowed_schemas", ["public"])
    if not isinstance(raw_schemas, list) or not 1 <= len(raw_schemas) <= 8:
        _error("invalid_configuration", "Supabase database configuration is invalid")
    schemas: list[str] = []
    for schema in raw_schemas:
        if (
            not isinstance(schema, str)
            or not _SCHEMA_RE.fullmatch(schema)
            or schema in _SYSTEM_SCHEMAS
            or schema.startswith("pg_")
            or schema in schemas
        ):
            _error("invalid_configuration", "Supabase database configuration is invalid")
        schemas.append(schema)
    allow_writes = config.get("allow_writes", False)
    if not isinstance(allow_writes, bool):
        _error("invalid_configuration", "Supabase database configuration is invalid")
    max_cell_bytes = _bounded_int(config, "max_cell_bytes", 4_096, 256, 8_000)
    max_result_bytes = _bounded_int(config, "max_result_bytes", 24_000, 4_096, 30_000)
    if max_cell_bytes > max_result_bytes:
        _error("invalid_configuration", "Supabase database configuration is invalid")
    return _ExecutionConfig(
        project_ref=project_ref,
        allowed_schemas=tuple(schemas),
        allow_writes=allow_writes,
        statement_timeout_ms=_bounded_int(config, "statement_timeout_ms", 5_000, 250, 30_000),
        lock_timeout_ms=_bounded_int(config, "lock_timeout_ms", 1_000, 100, 5_000),
        max_rows=_bounded_int(config, "max_rows", 200, 1, 1_000),
        max_cell_bytes=max_cell_bytes,
        max_result_bytes=max_result_bytes,
    )


def _parameter_bytes(value: object) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )


def _validate_bindings_and_static_budget(
    validated: ValidatedSql,
    params: list[object],
    *,
    max_rows: int,
) -> int:
    if validated.parameter_indexes != tuple(range(1, len(params) + 1)):
        _error("database_parameter_mismatch", "Database parameters do not match SQL")
    contribution = 0
    for value in validated.mutation_values:
        if value.parameter_index is not None:
            contribution += _parameter_bytes(params[value.parameter_index - 1])
        elif value.literal_bytes is not None:
            contribution += value.literal_bytes
        else:
            _error("database_sql_not_allowed", "Database SQL is not allowed")
        if contribution > MAX_MUTATION_VALUE_BYTES:
            _error("database_mutation_too_large", "Database mutation is too large")
    if validated.insert_row_count is not None and validated.insert_row_count > max_rows:
        _error("database_row_limit_exceeded", "Database mutation exceeds the row limit")
    return contribution


def _database_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    return sqlstate in {"55P03", "57014"}


async def _apply_transaction_settings(connection: Any, config: _ExecutionConfig) -> None:
    total_ms = min(config.statement_timeout_ms + config.lock_timeout_ms + 10_000, 45_000)
    fixed = (
        f"SET LOCAL statement_timeout = '{config.statement_timeout_ms}ms'",
        f"SET LOCAL lock_timeout = '{config.lock_timeout_ms}ms'",
        f"SET LOCAL idle_in_transaction_session_timeout = '{total_ms}ms'",
    )
    for query in fixed:
        await connection.execute(query)
    # Establish a trusted namespace before even the capability query: operator
    # lookup in PostgreSQL is search-path sensitive as well as function lookup.
    await connection.execute("SET LOCAL search_path TO pg_catalog")
    setting_names = [name for name, _value in _TRUSTED_GUCS]
    rows = await connection.fetch(_GUC_DISCOVERY_QUERY, setting_names)
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(setting_names):
        _error("database_execution_failed")
    available: set[str] = set()
    server_version: int | None = None
    jit_available: bool | None = None
    for raw_row in rows:
        if not isinstance(raw_row, (Mapping, asyncpg.Record)):
            _error("database_execution_failed")
        name = raw_row.get("name")
        version = raw_row.get("server_version_num")
        has_jit = raw_row.get("jit_available")
        if (
            not isinstance(name, str)
            or name not in setting_names
            or name in available
            or not isinstance(version, int)
            or isinstance(version, bool)
            or not 100_000 <= version <= 999_999
            or not isinstance(has_jit, bool)
        ):
            _error("database_execution_failed")
        if server_version is None:
            server_version = version
            jit_available = has_jit
        elif version != server_version or has_jit is not jit_available:
            _error("database_execution_failed")
        available.add(name)
    required = set(setting_names) - {"jit"}
    if (
        available & required != required
        or jit_available is None
        or (("jit" in available) is not jit_available)
    ):
        _error("database_execution_failed")
    for name, value in _TRUSTED_GUCS:
        if name not in available:
            continue
        try:
            await connection.execute(f"SET LOCAL {name} {value}")
        except asyncio.CancelledError:
            raise
        except Exception:
            if name == "temp_file_limit":
                _error(
                    "database_role_not_least_privilege",
                    "Supabase database role is not least privilege",
                )
            raise

    expected = {
        "statement_timeout": (str(config.statement_timeout_ms), "ms"),
        "lock_timeout": (str(config.lock_timeout_ms), "ms"),
        "idle_in_transaction_session_timeout": (str(total_ms), "ms"),
        **{name: value for name, value in _TRUSTED_GUC_VALUES.items() if name in available},
    }
    verified_rows = await connection.fetch(_GUC_VERIFY_QUERY, list(expected))
    if not isinstance(verified_rows, list) or len(verified_rows) != len(expected):
        _error("database_execution_failed")
    verified: dict[str, tuple[str, str | None]] = {}
    for raw_row in verified_rows:
        if not isinstance(raw_row, (Mapping, asyncpg.Record)):
            _error("database_execution_failed")
        name = raw_row.get("name")
        setting = raw_row.get("setting")
        unit = raw_row.get("unit")
        if (
            not isinstance(name, str)
            or name not in expected
            or name in verified
            or not isinstance(setting, str)
            or (unit is not None and not isinstance(unit, str))
        ):
            _error("database_execution_failed")
        verified[name] = (setting, unit)
    if verified != expected:
        _error("database_execution_failed")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def _probe_target_rows(connection: Any, target: Any, max_rows: int) -> int:
    query = (
        f"SELECT 1 FROM ONLY {_quote_identifier(target.schema)}."
        f"{_quote_identifier(target.name)} LIMIT {max_rows + 1}"
    )
    statement = await connection.prepare(query)
    rows = await statement.fetch()
    if not isinstance(rows, list):
        _error("database_execution_failed")
    count = len(rows)
    if count > max_rows:
        _error("database_row_limit_exceeded", "Database mutation exceeds the row limit")
    return count


def _attribute_type(attribute: Any) -> tuple[int, str, str]:
    type_info = getattr(attribute, "type", None)
    oid = getattr(type_info, "oid", None)
    name = getattr(type_info, "name", None)
    schema = getattr(type_info, "schema", None)
    if (
        isinstance(oid, bool)
        or not isinstance(oid, int)
        or not isinstance(name, str)
        or not isinstance(schema, str)
    ):
        _error("database_output_type_not_allowed", "Database output type is not allowed")
    return oid, name, schema


def _column_name(attribute: Any) -> str:
    name = getattr(attribute, "name", None)
    if not isinstance(name, str) or not name:
        _error("database_output_type_not_allowed", "Database output type is not allowed")
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _error("database_output_type_not_allowed", "Database output type is not allowed")
    if len(encoded) > 256 or any(
        unicodedata.category(character).startswith("C") for character in name
    ):
        _error("database_output_type_not_allowed", "Database output type is not allowed")
    return name


def _ascii_fold(value: str) -> str:
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _sql_identifier(identifier: Any) -> str:
    if not isinstance(identifier, exp.Identifier) or not isinstance(identifier.this, str):
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")
    return identifier.this if identifier.args.get("quoted") else _ascii_fold(identifier.this)


def _relation_lookup(preflight: Any) -> dict[tuple[str, str], Any]:
    return {(relation.schema, relation.name): relation for relation in preflight.relations}


def _direct_text_column(
    sql: str,
    projection_index: int,
    preflight: Any,
) -> Any:
    try:
        root = parse_one(
            _parser_safe_sql(sql),
            read="postgres",
            error_message_context=0,
            max_errors=1,
            max_nodes=512,
        )
    except Exception:
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")
    if type(root) is not exp.Select or root.args.get("with_") or root.args.get("distinct"):
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")
    projections = tuple(root.expressions)
    if projection_index >= len(projections):
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")
    selected = projections[projection_index]
    if type(selected) is exp.Alias:
        selected = selected.this
    if type(selected) is not exp.Column or isinstance(selected.this, exp.Star):
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")

    tables: list[exp.Table] = []
    stack = list(root.iter_expressions())
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Subquery, exp.CTE, exp.Union, exp.Intersect, exp.Except)):
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
        if isinstance(node, exp.TableAlias) and node.args.get("columns"):
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
        if isinstance(node, exp.Table):
            tables.append(node)
        stack.extend(node.iter_expressions())

    snapshots = _relation_lookup(preflight)
    aliases: dict[str, Any] = {}
    for table in tables:
        if not isinstance(table.this, exp.Identifier) or not isinstance(
            table.args.get("db"), exp.Identifier
        ):
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
        schema = _sql_identifier(table.args["db"])
        name = _sql_identifier(table.this)
        relation = snapshots.get((schema, name))
        if relation is None:
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
        alias = table.args.get("alias")
        alias_name = name
        if isinstance(alias, exp.TableAlias):
            alias_name = _sql_identifier(alias.this)
        aliases[alias_name] = relation
        aliases.setdefault(name, relation)

    column_name = _sql_identifier(selected.this)
    qualifier = selected.args.get("table")
    if qualifier is not None:
        relation = aliases.get(_sql_identifier(qualifier))
        candidates = [] if relation is None else [relation]
    else:
        candidates = [
            relation
            for relation in {(item.schema, item.name): item for item in aliases.values()}.values()
            if any(column.name == column_name for column in relation.columns)
        ]
    if len(candidates) != 1:
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")
    relation = candidates[0]
    columns = [column for column in relation.columns if column.name == column_name]
    if len(columns) != 1 or columns[0].storage != "e":
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")

    for walked in root.walk():
        if walked is selected or not isinstance(walked, exp.Column):
            continue
        if _sql_identifier(walked.this) != column_name:
            continue
        other_qualifier = walked.args.get("table")
        if other_qualifier is None or aliases.get(_sql_identifier(other_qualifier)) is relation:
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
    return columns[0]


def _validate_variable_width_uses(
    sql: str,
    attributes: tuple[Any, ...],
    preflight: Any,
) -> None:
    try:
        root = parse_one(
            _parser_safe_sql(sql),
            read="postgres",
            error_message_context=0,
            max_errors=1,
            max_nodes=512,
        )
    except Exception:
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")

    nodes = tuple(root.walk())
    variable_projection_indexes = {
        index
        for index, attribute in enumerate(attributes)
        if _attribute_type(attribute)[0] in _TEXT_OUTPUT_TYPES
    }
    if (
        variable_projection_indexes
        and type(root) is exp.Select
        and root.args.get("distinct") is not None
    ):
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")
    if any(
        isinstance(node, exp.TableAlias)
        and node.args.get("columns")
        and (isinstance(node.parent, exp.Table) or bool(variable_projection_indexes))
        for node in nodes
    ):
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")

    snapshots = _relation_lookup(preflight)
    aliases: dict[str, Any] = {}
    physical_bindings: dict[str, list[Any]] = {}
    for node in nodes:
        if not isinstance(node, exp.Table):
            continue
        database = node.args.get("db")
        if not isinstance(database, exp.Identifier) or not isinstance(node.this, exp.Identifier):
            continue
        relation = snapshots.get((_sql_identifier(database), _sql_identifier(node.this)))
        if relation is None:
            continue
        alias = node.args.get("alias")
        alias_name = relation.name
        if isinstance(alias, exp.TableAlias):
            alias_name = _sql_identifier(alias.this)
        physical_bindings.setdefault(alias_name, []).append(relation)
        aliases[alias_name] = relation
        aliases.setdefault(relation.name, relation)

    def has_variable_width(relation: Any) -> bool:
        return any(column.type_name in {"text", "varchar"} for column in relation.columns)

    # A global alias map cannot faithfully represent PostgreSQL's nested lexical
    # scopes.  For a shadowed binding, reject only actual variable-width column
    # uses that cannot be attributed safely; repeated fixed-width bindings remain
    # valid for nested queries and set operations.
    shadowed_variable_columns: dict[str, set[str]] = {
        binding: {
            column.name
            for relation in relations
            for column in relation.columns
            if column.type_name in {"text", "varchar"}
        }
        for binding, relations in physical_bindings.items()
        if len(relations) > 1
    }
    all_shadowed_variable_columns = {
        name for names in shadowed_variable_columns.values() for name in names
    }
    for node in nodes:
        if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
            continue
        column_name = _sql_identifier(node.this)
        qualifier = node.args.get("table")
        if qualifier is None:
            unsafe = column_name in all_shadowed_variable_columns
        else:
            unsafe = column_name in shadowed_variable_columns.get(_sql_identifier(qualifier), set())
        if unsafe:
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )

    if any(isinstance(node, exp.Star) for node in nodes) and any(
        has_variable_width(relation) for relation in snapshots.values()
    ):
        _error("database_output_not_safely_sliceable", "Database output is not safely sliceable")

    root_projections: dict[int, int] = {}
    variable_output_aliases: set[str] = set()
    if type(root) is exp.Select:
        for index, projection in enumerate(root.expressions):
            selected = projection.this if type(projection) is exp.Alias else projection
            if type(selected) is exp.Column:
                root_projections[id(selected)] = index
            if type(projection) is exp.Alias and index in variable_projection_indexes:
                alias = projection.args.get("alias")
                if isinstance(alias, exp.Identifier):
                    variable_output_aliases.add(_sql_identifier(alias))

    def rejects_projection_reference(expression: exp.Expression) -> bool:
        candidate = expression.this if isinstance(expression, exp.Ordered) else expression
        while type(candidate) is exp.Paren:
            nested = candidate.this
            if not isinstance(nested, exp.Expression):
                _error(
                    "database_output_not_safely_sliceable",
                    "Database output is not safely sliceable",
                )
            candidate = nested
        if isinstance(candidate, exp.Literal) and not candidate.is_string:
            raw = candidate.this
            if isinstance(raw, str) and raw.isascii() and raw.isdigit():
                if len(raw) > 2:
                    _error(
                        "database_output_not_safely_sliceable",
                        "Database output is not safely sliceable",
                    )
                return int(raw) - 1 in variable_projection_indexes
        return (
            isinstance(candidate, exp.Column)
            and not candidate.table
            and _sql_identifier(candidate.this) in variable_output_aliases
        )

    for node in nodes:
        if isinstance(node, (exp.Order, exp.Group)) and any(
            rejects_projection_reference(expression) for expression in node.expressions
        ):
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
        if isinstance(node, exp.Join):
            using = node.args.get("using")
            if isinstance(using, list):
                for key in using:
                    if not isinstance(key, exp.Identifier):
                        _error(
                            "database_output_not_safely_sliceable",
                            "Database output is not safely sliceable",
                        )
                    key_name = _sql_identifier(key)
                    if any(
                        column.name == key_name and column.type_name in {"text", "varchar"}
                        for relation in aliases.values()
                        for column in relation.columns
                    ):
                        _error(
                            "database_output_not_safely_sliceable",
                            "Database output is not safely sliceable",
                        )

    unique_relations = {
        (relation.schema, relation.name): relation for relation in aliases.values()
    }.values()
    for node in nodes:
        if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star):
            continue
        column_name = _sql_identifier(node.this)
        qualifier = node.args.get("table")
        if (
            qualifier is None
            and column_name in aliases
            and has_variable_width(aliases[column_name])
        ):
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
        if qualifier is not None:
            relation = aliases.get(_sql_identifier(qualifier))
            candidate_relations = [] if relation is None else [relation]
        else:
            candidate_relations = [
                relation
                for relation in unique_relations
                if any(column.name == column_name for column in relation.columns)
            ]
        variable_columns = [
            column
            for relation in candidate_relations
            for column in relation.columns
            if column.name == column_name and column.type_name in {"text", "varchar"}
        ]
        if not variable_columns:
            continue
        projection_index = root_projections.get(id(node))
        if projection_index is None or projection_index >= len(attributes):
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
        oid, _type_name, _schema = _attribute_type(attributes[projection_index])
        if oid not in _TEXT_OUTPUT_TYPES:
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )


def _effective_cell_bytes(columns: list[str], config: _ExecutionConfig) -> int:
    header = DatabaseReadOutput(columns=columns, rows=[], row_count=0, truncated=False)
    header_bytes = len(
        json.dumps(header.model_dump(mode="json"), ensure_ascii=False, default=str).encode("utf-8")
    )
    remaining = config.max_result_bytes - header_bytes - len(columns) * 4 - 32
    minimum = len(TRUNCATION_MARKER.encode("utf-8")) + 4
    if remaining < minimum * len(columns):
        _error("database_output_type_not_allowed", "Database output cannot fit safely")
    return min(config.max_cell_bytes, MAX_STRING_CHARS, remaining // len(columns))


def _trusted_read_wrapper(
    sql: str,
    *,
    text_columns: tuple[bool, ...],
    effective_cell_bytes: int,
    max_rows: int,
) -> str:
    projections: list[str] = []
    aliases = [f"__jhin_c{index}" for index in range(len(text_columns))]
    for index, is_text in enumerate(text_columns):
        source = f"__jhin_row.__jhin_c{index}"
        if is_text:
            encoded = (
                "CASE WHEN pg_catalog.pg_column_compression("
                f"{source}) IS NULL THEN pg_catalog.encode(pg_catalog.substr("
                "pg_catalog.convert_to(pg_catalog.substr("
                f"{source}::pg_catalog.text, 1, {effective_cell_bytes + 1}), 'UTF8'), "
                f"1, {effective_cell_bytes + 1}), 'base64') ELSE NULL END"
            )
            compressed = f"(pg_catalog.pg_column_compression({source}) IS NOT NULL)"
        else:
            encoded = (
                "pg_catalog.encode(pg_catalog.substr(pg_catalog.convert_to("
                f"pg_catalog.substr({source}::pg_catalog.text, 1, "
                f"{effective_cell_bytes + 1}), 'UTF8'), 1, {effective_cell_bytes + 1}), "
                "'base64')"
            )
            compressed = "false"
        projections.extend(
            (
                f"{encoded} AS __jhin_c{index}",
                f"{compressed} AS __jhin_c{index}_compressed",
            )
        )
    return (
        "SELECT "
        + ", ".join(projections)
        + "\nFROM (\n"
        + sql
        + "\n) AS __jhin_row("
        + ", ".join(aliases)
        + f")\nLIMIT {max_rows + 1}"
    )


def _decode_cell(encoded: object, compressed: object, maximum: int) -> tuple[str | None, bool]:
    if compressed is not False:
        if compressed is True:
            _error(
                "database_output_not_safely_sliceable",
                "Database output is not safely sliceable",
            )
        _error("database_execution_failed")
    if encoded is None:
        return None, False
    if not isinstance(encoded, str):
        _error("database_execution_failed")
    try:
        raw = base64.b64decode(encoded.replace("\n", ""), validate=True)
    except (binascii.Error, ValueError):
        _error("database_execution_failed")
    truncated = len(raw) > maximum
    if truncated:
        marker = TRUNCATION_MARKER.encode("utf-8")
        raw = raw[: max(0, maximum - len(marker))]
        while raw:
            try:
                prefix = raw.decode("utf-8", errors="strict")
                break
            except UnicodeDecodeError as exc:
                raw = raw[: exc.start]
        else:
            prefix = ""
        return prefix + TRUNCATION_MARKER, True
    try:
        return raw.decode("utf-8", errors="strict"), False
    except UnicodeDecodeError:
        _error("database_execution_failed")


def _serialized_bytes(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _contains_leaf_truncation(value: object) -> bool:
    if isinstance(value, str):
        return value.endswith(TRUNCATION_MARKER)
    if isinstance(value, list):
        return any(_contains_leaf_truncation(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_leaf_truncation(item) for item in value.values())
    return False


async def _execute_read(
    connection: Any,
    data: DatabaseReadInput,
    config: _ExecutionConfig,
    preflight: Any,
) -> DatabaseReadOutput:
    original = await connection.prepare(data.sql)
    attributes = tuple(original.get_attributes())
    if not 1 <= len(attributes) <= MAX_OUTPUT_COLUMNS:
        _error("database_output_type_not_allowed", "Database output type is not allowed")
    columns = [_column_name(attribute) for attribute in attributes]
    _validate_variable_width_uses(data.sql, attributes, preflight)
    text_columns: list[bool] = []
    for index, attribute in enumerate(attributes):
        oid, type_name, schema = _attribute_type(attribute)
        if schema != "pg_catalog":
            _error("database_output_type_not_allowed", "Database output type is not allowed")
        if _FIXED_OUTPUT_TYPES.get(oid) == type_name:
            text_columns.append(False)
        elif _TEXT_OUTPUT_TYPES.get(oid) == type_name:
            _direct_text_column(data.sql, index, preflight)
            text_columns.append(True)
        else:
            _error("database_output_type_not_allowed", "Database output type is not allowed")

    effective = _effective_cell_bytes(columns, config)
    wrapper = _trusted_read_wrapper(
        data.sql,
        text_columns=tuple(text_columns),
        effective_cell_bytes=effective,
        max_rows=config.max_rows,
    )
    wrapped = await connection.prepare(wrapper)
    cursor = await wrapped.cursor(*data.params)
    rows: list[list[str | None]] = []
    truncated = False
    for row_index in range(config.max_rows + 1):
        raw_row = await cursor.fetchrow()
        if raw_row is None:
            break
        if row_index == config.max_rows:
            truncated = True
            break
        if len(raw_row) != len(columns) * 2:
            _error("database_execution_failed")
        decoded: list[str | None] = []
        for index in range(len(columns)):
            cell, cell_truncated = _decode_cell(
                raw_row[index * 2], raw_row[index * 2 + 1], effective
            )
            decoded.append(cell)
            truncated = truncated or cell_truncated
        candidate = DatabaseReadOutput(
            columns=columns,
            rows=[*rows, decoded],
            row_count=len(rows) + 1,
            truncated=truncated,
        )
        sanitized = sanitize_payload(candidate.model_dump(mode="json"))
        if "original_size_bytes" in sanitized or _serialized_bytes(sanitized) > min(
            config.max_result_bytes, MAX_DOCUMENT_BYTES
        ):
            truncated = True
            break
        if _contains_leaf_truncation(sanitized):
            truncated = True
        safe_candidate = DatabaseReadOutput.model_validate(sanitized)
        columns = safe_candidate.columns
        rows = safe_candidate.rows

    result = DatabaseReadOutput(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
    )
    sanitized_result = sanitize_payload(result.model_dump(mode="json"))
    if (
        "original_size_bytes" in sanitized_result
        or _serialized_bytes(sanitized_result) > config.max_result_bytes
    ):
        _error("database_execution_failed")
    return DatabaseReadOutput.model_validate(sanitized_result)


def _affected_rows(status: object, statement_type: str) -> int:
    if not isinstance(status, str):
        _error("database_execution_failed")
    patterns = {
        "insert": r"INSERT 0 ([0-9]+)",
        "update": r"UPDATE ([0-9]+)",
        "delete": r"DELETE ([0-9]+)",
    }
    pattern = patterns.get(statement_type)
    if pattern is None:
        _error("database_execution_failed")
    match = re.fullmatch(pattern, status)
    if match is None:
        _error("database_execution_failed")
    return int(match.group(1))


async def _execute_mutation(
    connection: Any,
    data: DatabaseMutationInput,
    config: _ExecutionConfig,
    validated: ValidatedSql,
    preflight: Any,
    assignment_bytes: int,
) -> DatabaseMutationOutput:
    target = preflight.target
    if target is None:
        _error("database_relation_not_allowed", "Database relation is not allowed")
    probed_rows: int | None = None
    if validated.statement_type in {"update", "delete", "truncate"}:
        probed_rows = await _probe_target_rows(connection, target, config.max_rows)
    if (
        validated.statement_type == "update"
        and probed_rows is not None
        and assignment_bytes * probed_rows > MAX_MUTATION_VALUE_BYTES
    ):
        _error("database_mutation_too_large", "Database mutation is too large")

    statement = await connection.prepare(data.sql)
    await statement.fetch(*data.params)
    status = statement.get_statusmsg()
    if validated.statement_type == "truncate":
        if status != "TRUNCATE TABLE" or probed_rows is None:
            _error("database_execution_failed")
        affected = probed_rows
    else:
        affected = _affected_rows(status, validated.statement_type)
    if affected > config.max_rows:
        _error("database_row_limit_exceeded", "Database mutation exceeds the row limit")
    return DatabaseMutationOutput(affected_rows=affected)


async def _bounded_cleanup_operation(
    operation: Callable[[], Awaitable[Any]],
) -> _CleanupOutcome:
    task: asyncio.Future[Any] = asyncio.ensure_future(operation())
    cancellation: asyncio.CancelledError | None = None
    failed = False
    try:
        async with asyncio.timeout(DATABASE_CLEANUP_TIMEOUT_SECONDS):
            await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        cancellation = exc
        try:
            async with asyncio.timeout(DATABASE_CLEANUP_TIMEOUT_SECONDS):
                await asyncio.shield(task)
        except asyncio.CancelledError as repeated:
            cancellation = repeated
        except Exception:
            failed = True
    except Exception:
        failed = True
    if not task.done():
        failed = True
        task.cancel()

    def consume_result(completed: asyncio.Future[Any]) -> bool:
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            return True
        except Exception:
            return True
        return error is not None

    if task.done():
        failed = consume_result(task) or failed
    else:
        task.add_done_callback(consume_result)
        await asyncio.sleep(0)
    return _CleanupOutcome(cancellation=cancellation, failed=failed)


async def _cleanup(transaction: Any, connection: Any, *, rollback: bool) -> None:
    cancellation: asyncio.CancelledError | None = None
    if rollback and transaction is not None:
        rollback_outcome = await _bounded_cleanup_operation(transaction.rollback)
        cancellation = rollback_outcome.cancellation
    if connection is not None:
        close_outcome = await _bounded_cleanup_operation(connection.close)
        cancellation = close_outcome.cancellation or cancellation
    if cancellation is not None:
        raise cancellation


async def _close_after_success(connection: Any) -> None:
    outcome = await _bounded_cleanup_operation(connection.close)
    if outcome.cancellation is not None:
        raise outcome.cancellation
    if outcome.failed:
        _error("database_execution_failed")


async def _database_executor(
    ctx: ToolExecutionContext,
    payload: BaseModel,
    *,
    expected: SqlClass,
) -> BaseModel:
    data = cast(DatabaseReadInput | DatabaseMutationInput, payload)
    try:
        resolved = await resolve_connection(ctx, data.connection_id, connector_type="supabase")
    except ConnectionResolutionError:
        _error("connection_unavailable", "Supabase database connection is unavailable")
    if resolved.connection.auth_type != "postgres":
        _error("unsupported_auth_type", "This Supabase connection is not PostgreSQL")
    config = _execution_config(resolved.config)
    if data.project_ref != config.project_ref:
        _error("project_scope_mismatch", "Supabase project scope does not match")
    if data.requested_schema not in config.allowed_schemas:
        _error("schema_scope_mismatch", "Supabase schema scope does not match")
    if expected != "read" and not config.allow_writes:
        _error("database_writes_disabled", "Supabase database writes are disabled")
    database_url = resolved.credentials.get("database_url")
    if not isinstance(database_url, str) or not database_url:
        _error("credential_invalid", "Supabase database credential is invalid")
    try:
        validated_url = validate_postgres_target(
            database_url,
            project_ref=config.project_ref,
            app_database_url=os.getenv("DATABASE_URL"),
        )
    except EndpointPolicyError:
        _error("database_target_not_allowed", "Supabase database target is not allowed")
    try:
        validated = classify_and_validate_sql(
            data.sql,
            expected=expected,
            requested_schema=data.requested_schema,
        )
    except SqlPolicyError:
        _error("database_sql_not_allowed", "Database SQL is not allowed")
    assignment_bytes = _validate_bindings_and_static_budget(
        validated,
        cast(list[object], data.params),
        max_rows=config.max_rows,
    )
    if len(validated.relations) > MAX_PREFLIGHT_RELATIONS:
        _error("database_relation_not_allowed", "Database relation is not allowed")

    connection: Any = None
    transaction: Any = None
    committed = False
    try:
        async with asyncio.timeout(DATABASE_CONNECT_TIMEOUT_SECONDS):
            connection = await asyncpg.connect(
                dsn=_asyncpg_dsn(validated_url),
                timeout=5,
                statement_cache_size=0,
                ssl=_hosted_ssl_context(validated_url, config.project_ref),
            )
        work_seconds = min(
            (config.statement_timeout_ms + config.lock_timeout_ms + 10_000) / 1_000,
            45.0,
        )
        async with asyncio.timeout(work_seconds):
            transaction = (
                connection.transaction(readonly=True)
                if expected == "read"
                else connection.transaction()
            )
            await transaction.start()
            await _apply_transaction_settings(connection, config)
            from jhin_connectors.supabase.database_preflight import (
                DatabasePreflightError,
                preflight_and_lock,
                verify_live_role,
            )

            try:
                role_oids = await verify_live_role(connection, config.allowed_schemas)
                preflight = await preflight_and_lock(connection, validated, role_oids)
                locked_role_oids = await verify_live_role(
                    connection,
                    config.allowed_schemas,
                )
            except DatabasePreflightError as exc:
                _error(exc.code, str(exc))
            if locked_role_oids != role_oids:
                _error(
                    "database_role_not_least_privilege",
                    "Supabase database role is not least privilege",
                )
            result: DatabaseReadOutput | DatabaseMutationOutput
            if expected == "read":
                result = await _execute_read(
                    connection,
                    cast(DatabaseReadInput, data),
                    config,
                    preflight,
                )
            else:
                result = await _execute_mutation(
                    connection,
                    cast(DatabaseMutationInput, data),
                    config,
                    validated,
                    preflight,
                    assignment_bytes,
                )
            await transaction.commit()
            committed = True
        committed_connection = connection
        connection = None
        await _close_after_success(committed_connection)
        return result
    except asyncio.CancelledError:
        await _cleanup(transaction, connection, rollback=not committed)
        connection = None
        raise
    except SupabaseDatabaseError:
        await _cleanup(transaction, connection, rollback=not committed)
        connection = None
        raise
    except Exception as exc:
        await _cleanup(transaction, connection, rollback=not committed)
        connection = None
        if _database_timeout(exc):
            _error("database_timeout", "Supabase database operation timed out")
        _error("database_execution_failed")
    except BaseException:
        await _cleanup(transaction, connection, rollback=not committed)
        connection = None
        raise
    finally:
        if committed:
            await _cleanup(None, connection, rollback=False)


async def _read_executor(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    return await _database_executor(ctx, payload, expected="read")


async def _write_executor(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    return await _database_executor(ctx, payload, expected="write")


async def _destructive_executor(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    return await _database_executor(ctx, payload, expected="destructive")


def _tool(
    *,
    name: str,
    description: str,
    risk: RiskLevel,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    supports_approval: bool,
    executor: ToolExecutor,
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
            scope_keys=("connection_id", "project_ref", "schema"),
            required_grant_scope_keys=("connection_id", "project_ref", "schema"),
        ),
        executor,
    )


SUPABASE_DATABASE_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    _tool(
        name="supabase.database.read",
        description="Read bounded rows from explicitly scoped Supabase tables.",
        risk=RiskLevel.READ,
        input_model=DatabaseReadInput,
        output_model=DatabaseReadOutput,
        supports_approval=False,
        executor=_read_executor,
    ),
    _tool(
        name="supabase.database.write",
        description="Insert bounded rows into one explicitly scoped Supabase table.",
        risk=RiskLevel.ELEVATED,
        input_model=DatabaseMutationInput,
        output_model=DatabaseMutationOutput,
        supports_approval=True,
        executor=_write_executor,
    ),
    _tool(
        name="supabase.database.destructive",
        description="Run a bounded update, delete, or truncate on one scoped table.",
        risk=RiskLevel.DESTRUCTIVE,
        input_model=DatabaseMutationInput,
        output_model=DatabaseMutationOutput,
        supports_approval=True,
        executor=_destructive_executor,
    ),
)


__all__ = ["SUPABASE_DATABASE_TOOLS", "SupabaseDatabaseError"]
