"""Fail-closed static SQL policy for the Supabase database tools.

The original SQL string is never rendered from the AST.  This module only
classifies it and returns immutable metadata used by the database executor.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, NoReturn, cast

from sqlglot import Token, TokenType, exp, parse_one, tokenize
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.scope import traverse_scope

MAX_SQL_TOKENS = 1_024
MAX_SQL_AST_NODES = 512
MAX_SQL_AST_DEPTH = 64
MAX_MUTATION_VALUE_BYTES = 1_048_576
_POSTGRES_SYSTEM_COLUMNS = frozenset({"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid"})

SqlClass = Literal["read", "write", "destructive"]
RelationAccess = Literal["source", "target"]


class SqlPolicyError(ValueError):
    """A credential-safe rejection of submitted SQL."""


@dataclass(frozen=True, order=True)
class RelationRef:
    schema: str
    name: str
    access: RelationAccess


@dataclass(frozen=True)
class MutationValueRef:
    parameter_index: int | None
    literal_bytes: int | None


@dataclass(frozen=True)
class ValidatedSql:
    sql_class: SqlClass
    statement_type: str
    relations: tuple[RelationRef, ...]
    mutation_target: RelationRef | None
    parameter_indexes: tuple[int, ...]
    mutation_values: tuple[MutationValueRef, ...]
    insert_row_count: int | None


_ALLOWED_CAST_TYPES = frozenset(
    {
        exp.DataType.Type.BOOLEAN,
        exp.DataType.Type.SMALLINT,
        exp.DataType.Type.INT,
        exp.DataType.Type.BIGINT,
        exp.DataType.Type.DECIMAL,
        exp.DataType.Type.FLOAT,
        exp.DataType.Type.DOUBLE,
        exp.DataType.Type.TEXT,
        exp.DataType.Type.VARCHAR,
        exp.DataType.Type.DATE,
        exp.DataType.Type.TIMESTAMP,
        exp.DataType.Type.TIMESTAMPTZ,
        exp.DataType.Type.UUID,
        exp.DataType.Type.JSON,
        exp.DataType.Type.JSONB,
    }
)

# This is deliberately concrete.  In particular, no generic Binary, Func, DDL,
# command, procedural, catalog, or session-setting expression is accepted.
_ALLOWED_NODE_TYPES = (
    exp.Add,
    exp.Alias,
    exp.And,
    exp.Between,
    exp.Boolean,
    exp.Case,
    exp.Cast,
    exp.Column,
    exp.CTE,
    exp.DataType,
    exp.Delete,
    exp.Distinct,
    exp.Div,
    exp.EQ,
    exp.Except,
    exp.From,
    exp.Group,
    exp.GT,
    exp.GTE,
    exp.Having,
    exp.Identifier,
    exp.If,
    exp.ILike,
    exp.In,
    exp.Insert,
    exp.Intersect,
    exp.Is,
    exp.Join,
    exp.Like,
    exp.Limit,
    exp.Literal,
    exp.LT,
    exp.LTE,
    exp.Mod,
    exp.Mul,
    exp.Neg,
    exp.NEQ,
    exp.Not,
    exp.Null,
    exp.Offset,
    exp.Or,
    exp.Order,
    exp.Ordered,
    exp.Parameter,
    exp.Paren,
    exp.RawString,
    exp.Schema,
    exp.Select,
    exp.Star,
    exp.Sub,
    exp.Subquery,
    exp.Table,
    exp.TableAlias,
    exp.TruncateTable,
    exp.Tuple,
    exp.Union,
    exp.Update,
    exp.Values,
    exp.Where,
    exp.With,
)
_ALLOWED_NODE_TYPE_SET = frozenset(_ALLOWED_NODE_TYPES)

_QUERY_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_MUTATION_TYPES = (exp.Insert, exp.Update, exp.Delete, exp.TruncateTable)


def _reject() -> NoReturn:
    raise SqlPolicyError("unsupported SQL") from None


def _parser_safe_sql(sql: str) -> str:
    """Work around SQLGlot's PostgreSQL ``$1,$2`` dollar-quote ambiguity.

    Only whitespace before a comma or closing parenthesis is inserted, and only
    outside quoted strings, quoted identifiers, comments, and dollar strings.
    The returned value is parser-only; execution always receives ``sql``.
    """

    output: list[str] = []
    index = 0
    length = len(sql)
    state = "plain"
    block_depth = 0
    dollar_tag = ""

    while index < length:
        character = sql[index]
        following = sql[index + 1] if index + 1 < length else ""

        if state == "line_comment":
            output.append(character)
            index += 1
            if character in "\r\n":
                state = "plain"
            continue

        if state == "block_comment":
            if character == "/" and following == "*":
                output.extend((character, following))
                block_depth += 1
                index += 2
            elif character == "*" and following == "/":
                output.extend((character, following))
                block_depth -= 1
                index += 2
                if block_depth == 0:
                    state = "plain"
            else:
                output.append(character)
                index += 1
            continue

        if state == "single_quote":
            output.append(character)
            index += 1
            if character == "'":
                if index < length and sql[index] == "'":
                    output.append(sql[index])
                    index += 1
                else:
                    previous = sql[index - 2] if index >= 2 else ""
                    adjacent = sql[index] if index < length else ""
                    if previous == "\\" and (
                        adjacent == "$"
                        or adjacent == '"'
                        or adjacent == "'"
                        or adjacent == "_"
                        or adjacent.isalnum()
                    ):
                        _reject()
                    state = "plain"
            continue

        if state == "double_quote":
            output.append(character)
            index += 1
            if character == '"':
                if index < length and sql[index] == '"':
                    output.append(sql[index])
                    index += 1
                else:
                    state = "plain"
            continue

        if state == "dollar_quote":
            if sql.startswith(dollar_tag, index):
                output.append(dollar_tag)
                index += len(dollar_tag)
                state = "plain"
            else:
                output.append(character)
                index += 1
            continue

        if character == "-" and following == "-":
            output.extend((character, following))
            index += 2
            state = "line_comment"
            continue
        if character == "/" and following == "*":
            output.extend((character, following))
            index += 2
            state = "block_comment"
            block_depth = 1
            continue
        if character == "'":
            output.append(character)
            index += 1
            state = "single_quote"
            continue
        if character == '"':
            output.append(character)
            index += 1
            state = "double_quote"
            continue
        if character == "$":
            tag_end = index + 1
            while tag_end < length and (sql[tag_end].isalnum() or sql[tag_end] == "_"):
                tag_end += 1
            if (
                tag_end < length
                and sql[tag_end] == "$"
                and (tag_end == index + 1 or not sql[index + 1].isdigit())
            ):
                dollar_tag = sql[index : tag_end + 1]
                output.append(dollar_tag)
                index = tag_end + 1
                state = "dollar_quote"
                continue
            parameter_end = index + 1
            while parameter_end < length and sql[parameter_end].isdigit():
                parameter_end += 1
            if parameter_end > index + 1:
                output.append(sql[index:parameter_end])
                if parameter_end < length and sql[parameter_end] in ",)":
                    output.append(" ")
                index = parameter_end
                continue

        output.append(character)
        index += 1

    return "".join(output)


def _tokenize_sql(sql: str) -> tuple[str, tuple[Token, ...]]:
    parser_sql = _parser_safe_sql(sql)
    try:
        tokens = tuple(tokenize(parser_sql, read="postgres"))
    except (TokenError, RecursionError, ValueError, TypeError):
        _reject()
    if not tokens or len(tokens) > MAX_SQL_TOKENS:
        _reject()
    if any(token.token_type is TokenType.SEMICOLON for token in tokens):
        _reject()
    return parser_sql, tokens


def _parse_sql(parser_sql: str) -> exp.Expression:
    try:
        root = parse_one(
            parser_sql,
            read="postgres",
            error_message_context=0,
            max_errors=1,
            max_nodes=MAX_SQL_AST_NODES,
        )
    except (ParseError, TokenError, RecursionError, ValueError, TypeError):
        _reject()
    return cast(exp.Expression, root)


def _walk_bounded(root: exp.Expression) -> tuple[exp.Expression, ...]:
    nodes: list[exp.Expression] = []
    stack: list[tuple[exp.Expression, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        nodes.append(node)
        if len(nodes) > MAX_SQL_AST_NODES or depth > MAX_SQL_AST_DEPTH:
            _reject()
        children = tuple(node.iter_expressions())
        stack.extend((child, depth + 1) for child in reversed(children))
    return tuple(nodes)


def _is_literal_value(node: exp.Expression) -> bool:
    if isinstance(node, (exp.Literal, exp.RawString, exp.Boolean, exp.Null)):
        return True
    if isinstance(node, exp.Neg):
        return isinstance(node.this, exp.Literal) and not node.this.is_string
    if isinstance(node, exp.Cast):
        return _valid_cast(node) and _is_literal_value(node.this)
    return False


def _valid_cast(node: exp.Cast) -> bool:
    target = node.args.get("to")
    if not isinstance(target, exp.DataType):
        return False
    if target.this not in _ALLOWED_CAST_TYPES:
        return False
    if target.args.get("nested") or target.args.get("expressions"):
        return False
    if target.args.get("kind") or target.args.get("values"):
        return False
    if any(node.args.get(key) is not None for key in ("format", "safe", "action", "default")):
        return False
    return _is_literal_value(node.this)


def _validate_whole_tree(root: exp.Expression, nodes: tuple[exp.Expression, ...]) -> None:
    for node in nodes:
        if type(node) not in _ALLOWED_NODE_TYPE_SET:
            _reject()
        if type(node) is exp.Cast and not _valid_cast(node):
            _reject()
        if isinstance(node, _MUTATION_TYPES) and node is not root:
            _reject()
        if isinstance(node, exp.DataType) and not isinstance(node.parent, exp.Cast):
            _reject()
        if isinstance(node, exp.Table):
            allowed_args = {"this", "db", "catalog", "alias", "joins"}
            if any(value for key, value in node.args.items() if key not in allowed_args):
                _reject()
        if isinstance(node, exp.Column) and (
            node.args.get("db") is not None or node.args.get("catalog") is not None
        ):
            _reject()
        if (
            isinstance(node, exp.Column)
            and _ascii_fold(_identifier_value(cast(exp.Expression, node.this)))
            in _POSTGRES_SYSTEM_COLUMNS
        ):
            _reject()
        if (
            isinstance(node, exp.Column)
            and isinstance(node.this, exp.Identifier)
            and not node.this.args.get("quoted")
            and _ascii_fold(str(node.this.this))
            in {"user", "current_user", "session_user", "current_role", "system_user"}
        ):
            _reject()
        if isinstance(node, exp.Identifier):
            _identifier_value(node)
        if type(node) is exp.If and type(node.parent) is not exp.Case:
            _reject()
        if isinstance(node, exp.CTE) and (
            node.args.get("materialized") is not None or node.args.get("key_expressions")
        ):
            _reject()
        if isinstance(node, exp.With) and (
            node.args.get("search") is not None or node.args.get("udfs")
        ):
            _reject()


def _validate_query_shapes(nodes: tuple[exp.Expression, ...]) -> None:
    for node in nodes:
        if isinstance(node, exp.Select):
            if not node.expressions:
                _reject()
            forbidden = (
                "into",
                "locks",
                "hint",
                "kind",
                "exclude",
                "operation_modifiers",
                "qualify",
                "windows",
                "connect",
                "match",
                "laterals",
                "distribute",
                "sort",
                "cluster",
            )
            if any(node.args.get(key) for key in forbidden):
                _reject()
        elif isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            if any(
                node.args.get(key)
                for key in ("by_name", "side", "kind", "on", "operation_modifiers")
            ):
                _reject()
        elif isinstance(node, exp.Join):
            if node.args.get("method") or node.args.get("global"):
                _reject()
            side = node.args.get("side")
            kind = node.args.get("kind")
            if side not in {None, "LEFT", "RIGHT", "FULL"}:
                _reject()
            if kind not in {None, "INNER", "CROSS"}:
                _reject()


def _ascii_fold(value: str) -> str:
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _identifier_value(identifier: exp.Expression | None) -> str:
    if not isinstance(identifier, exp.Identifier):
        _reject()
    value = identifier.this
    if not isinstance(value, str) or not value:
        _reject()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _reject()
    if (
        not encoded
        or len(encoded) > 63
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        _reject()
    return value if identifier.args.get("quoted") else _ascii_fold(value)


def _cte_key(alias: exp.TableAlias) -> tuple[bool, str]:
    identifier = alias.this
    if not isinstance(identifier, exp.Identifier):
        _reject()
    value = _identifier_value(identifier)
    return bool(identifier.args.get("quoted")), value


def _table_key(table: exp.Table) -> tuple[bool, str]:
    identifier = table.this
    if not isinstance(identifier, exp.Identifier):
        _reject()
    return bool(identifier.args.get("quoted")), _identifier_value(identifier)


def _is_descendant(node: exp.Expression, ancestor: exp.Expression) -> bool:
    current: exp.Expression | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = cast(exp.Expression | None, current.parent)
    return False


def _visible_cte_keys(table: exp.Table) -> set[tuple[bool, str]]:
    visible: set[tuple[bool, str]] = set()
    current: exp.Expression | None = table
    while current is not None:
        with_expression = current.args.get("with_")
        if isinstance(with_expression, exp.With):
            ctes = tuple(with_expression.expressions)
            containing_index: int | None = None
            for index, cte in enumerate(ctes):
                if _is_descendant(table, cte):
                    containing_index = index
                    break
            if containing_index is None:
                eligible = ctes
            else:
                eligible_count = containing_index
                if with_expression.args.get("recursive"):
                    eligible_count += 1
                eligible = ctes[:eligible_count]
            for cte in eligible:
                alias = cte.args.get("alias")
                if not isinstance(alias, exp.TableAlias):
                    _reject()
                visible.add(_cte_key(alias))
        current = cast(exp.Expression | None, current.parent)
    return visible


def _physical_relation(
    table: exp.Table,
    *,
    requested_schema: str,
    access: RelationAccess,
) -> RelationRef | None:
    if table.args.get("catalog") is not None:
        _reject()
    database = table.args.get("db")
    if database is None:
        if access == "source" and _table_key(table) in _visible_cte_keys(table):
            return None
        _reject()
    schema = _identifier_value(cast(exp.Expression, database))
    name = _identifier_value(cast(exp.Expression, table.this))
    if schema != requested_schema:
        _reject()
    return RelationRef(schema, name, access)


def _collect_relations(
    nodes: tuple[exp.Expression, ...],
    *,
    target_table: exp.Table | None,
    requested_schema: str,
) -> tuple[tuple[RelationRef, ...], RelationRef | None]:
    strongest: dict[tuple[str, str], RelationRef] = {}
    mutation_target: RelationRef | None = None
    for node in nodes:
        if not isinstance(node, exp.Table):
            continue
        access: RelationAccess = "target" if node is target_table else "source"
        relation = _physical_relation(
            node,
            requested_schema=requested_schema,
            access=access,
        )
        if relation is None:
            continue
        key = (relation.schema, relation.name)
        if access == "target":
            strongest[key] = relation
            mutation_target = relation
        else:
            strongest.setdefault(key, relation)
    return tuple(sorted(strongest.values())), mutation_target


def _validate_unqualified_columns(
    root: exp.Expression,
    nodes: tuple[exp.Expression, ...],
) -> None:
    if isinstance(root, _QUERY_TYPES):
        try:
            scopes = traverse_scope(root)
            for scope in scopes:
                if len(scope.selected_sources) > 1 and scope.unqualified_columns:
                    _reject()
        except (RecursionError, ValueError, TypeError):
            _reject()
        return

    physical_tables = sum(isinstance(node, exp.Table) for node in nodes)
    if physical_tables <= 1:
        return
    assignments: set[int] = set()
    if isinstance(root, exp.Update):
        for assignment in root.expressions:
            if isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column):
                assignments.add(id(assignment.this))
    for node in nodes:
        if isinstance(node, exp.Column) and not node.table and id(node) not in assignments:
            _reject()


def _parameter_index(parameter: exp.Parameter) -> int:
    value = parameter.this
    if not isinstance(value, exp.Literal) or value.is_string:
        _reject()
    raw = value.this
    if not isinstance(raw, str) or len(raw) > 2 or not raw.isascii() or not raw.isdigit():
        _reject()
    index = int(raw)
    if not 1 <= index <= 50:
        _reject()
    return index


def _literal_bytes(node: exp.Expression) -> int:
    if isinstance(node, exp.Cast):
        if not _valid_cast(node):
            _reject()
        return _literal_bytes(node.this)
    if isinstance(node, exp.Neg):
        if not isinstance(node.this, exp.Literal) or node.this.is_string:
            _reject()
        return len(("-" + str(node.this.this)).encode("utf-8"))
    if isinstance(node, (exp.Literal, exp.RawString)):
        value = node.this
        if not isinstance(value, str):
            _reject()
        return len(value.encode("utf-8"))
    if isinstance(node, exp.Boolean):
        return 4 if bool(node.this) else 5
    if isinstance(node, exp.Null):
        return 4
    _reject()


def _mutation_value(node: exp.Expression) -> MutationValueRef:
    if isinstance(node, exp.Parameter):
        return MutationValueRef(_parameter_index(node), None)
    if not _is_literal_value(node):
        _reject()
    return MutationValueRef(None, _literal_bytes(node))


def _table_from_insert(root: exp.Insert) -> tuple[exp.Table, tuple[exp.Identifier, ...]]:
    destination = root.this
    if not isinstance(destination, exp.Schema):
        _reject()
    table = destination.this
    columns = tuple(destination.expressions)
    if (
        not isinstance(table, exp.Table)
        or table.args.get("alias") is not None
        or not columns
        or not all(isinstance(column, exp.Identifier) for column in columns)
    ):
        _reject()
    names = tuple(_identifier_value(column) for column in columns)
    if len(names) != len(set(names)):
        _reject()
    return table, cast(tuple[exp.Identifier, ...], columns)


def _validate_insert(root: exp.Insert) -> tuple[exp.Table, tuple[MutationValueRef, ...], int]:
    allowed_nonempty = {"this", "expression"}
    if any(value for key, value in root.args.items() if key not in allowed_nonempty):
        _reject()
    table, columns = _table_from_insert(root)
    values = root.expression
    if not isinstance(values, exp.Values) or values.args.get("alias") is not None:
        _reject()
    rows = tuple(values.expressions)
    if not rows:
        _reject()
    mutation_values: list[MutationValueRef] = []
    for row in rows:
        if not isinstance(row, exp.Tuple) or len(row.expressions) != len(columns):
            _reject()
        mutation_values.extend(_mutation_value(value) for value in row.expressions)
    literal_total = sum(value.literal_bytes or 0 for value in mutation_values)
    if literal_total > MAX_MUTATION_VALUE_BYTES:
        _reject()
    return table, tuple(mutation_values), len(rows)


def _validate_update(root: exp.Update) -> tuple[exp.Table, tuple[MutationValueRef, ...]]:
    if not isinstance(root.this, exp.Table):
        _reject()
    if root.args.get("returning") or root.args.get("order") or root.args.get("limit"):
        _reject()
    if root.args.get("with_") or root.args.get("hint"):
        _reject()
    assignments = tuple(root.expressions)
    if not assignments:
        _reject()
    names: list[str] = []
    mutation_values: list[MutationValueRef] = []
    for assignment in assignments:
        if not isinstance(assignment, exp.EQ) or not isinstance(assignment.this, exp.Column):
            _reject()
        column = assignment.this
        if column.table or column.args.get("db") or column.args.get("catalog"):
            _reject()
        names.append(_identifier_value(cast(exp.Expression, column.this)))
        mutation_values.append(_mutation_value(assignment.expression))
    if len(names) != len(set(names)):
        _reject()
    literal_total = sum(value.literal_bytes or 0 for value in mutation_values)
    if literal_total > MAX_MUTATION_VALUE_BYTES:
        _reject()
    return root.this, tuple(mutation_values)


def _validate_delete(root: exp.Delete) -> exp.Table:
    if not isinstance(root.this, exp.Table):
        _reject()
    if any(
        root.args.get(key) for key in ("tables", "cluster", "returning", "order", "limit", "with_")
    ):
        _reject()
    return root.this


def _validate_truncate(root: exp.TruncateTable, tokens: tuple[Token, ...]) -> exp.Table:
    tables = tuple(root.expressions)
    if len(tables) != 1 or not isinstance(tables[0], exp.Table):
        _reject()
    if any(token.token_type is TokenType.STAR for token in tokens):
        _reject()
    if root.args.get("is_database") or root.args.get("exists"):
        _reject()
    if root.args.get("cluster") is not None or root.args.get("partition") is not None:
        _reject()
    if root.args.get("identity") not in {None, "CONTINUE"}:
        _reject()
    if root.args.get("option") not in {None, "RESTRICT"}:
        _reject()
    return tables[0]


def _classify_root(root: exp.Expression, expected: SqlClass) -> tuple[SqlClass, str]:
    if isinstance(root, _QUERY_TYPES):
        sql_class: SqlClass = "read"
        statement_type = "select"
    elif isinstance(root, exp.Insert):
        sql_class = "write"
        statement_type = "insert"
    elif isinstance(root, exp.Update):
        sql_class = "destructive"
        statement_type = "update"
    elif isinstance(root, exp.Delete):
        sql_class = "destructive"
        statement_type = "delete"
    elif isinstance(root, exp.TruncateTable):
        sql_class = "destructive"
        statement_type = "truncate"
    else:
        _reject()
    if sql_class != expected:
        _reject()
    return sql_class, statement_type


def classify_and_validate_sql(
    sql: str,
    *,
    expected: SqlClass,
    requested_schema: str,
) -> ValidatedSql:
    """Classify one PostgreSQL statement and return immutable safe metadata."""

    if expected not in {"read", "write", "destructive"}:
        _reject()
    if not re.fullmatch(r"[a-z_][a-z0-9_$]{0,62}", requested_schema):
        _reject()
    try:
        sql.encode("utf-8", errors="strict")
    except (AttributeError, UnicodeEncodeError):
        _reject()

    parser_sql, tokens = _tokenize_sql(sql)
    root = _parse_sql(parser_sql)
    nodes = _walk_bounded(root)
    sql_class, statement_type = _classify_root(root, expected)
    _validate_whole_tree(root, nodes)
    _validate_query_shapes(nodes)

    target_table: exp.Table | None = None
    mutation_values: tuple[MutationValueRef, ...] = ()
    insert_row_count: int | None = None
    if isinstance(root, exp.Insert):
        target_table, mutation_values, insert_row_count = _validate_insert(root)
    elif isinstance(root, exp.Update):
        target_table, mutation_values = _validate_update(root)
    elif isinstance(root, exp.Delete):
        target_table = _validate_delete(root)
    elif isinstance(root, exp.TruncateTable):
        target_table = _validate_truncate(root, tokens)

    relations, mutation_target = _collect_relations(
        nodes,
        target_table=target_table,
        requested_schema=requested_schema,
    )
    _validate_unqualified_columns(root, nodes)

    parameter_indexes = tuple(
        sorted({_parameter_index(node) for node in nodes if isinstance(node, exp.Parameter)})
    )
    if parameter_indexes and parameter_indexes != tuple(range(1, parameter_indexes[-1] + 1)):
        _reject()

    return ValidatedSql(
        sql_class=sql_class,
        statement_type=statement_type,
        relations=relations,
        mutation_target=mutation_target,
        parameter_indexes=parameter_indexes,
        mutation_values=mutation_values,
        insert_row_count=insert_row_count,
    )


__all__ = [
    "MAX_MUTATION_VALUE_BYTES",
    "MAX_SQL_AST_DEPTH",
    "MAX_SQL_AST_NODES",
    "MAX_SQL_TOKENS",
    "MutationValueRef",
    "RelationRef",
    "SqlClass",
    "SqlPolicyError",
    "ValidatedSql",
    "classify_and_validate_sql",
]
