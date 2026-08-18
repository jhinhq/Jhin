"""Fail-closed static PostgreSQL policy decision table."""

from __future__ import annotations

import logging

import pytest
from sqlglot import exp, tokenize

from jhin_connectors.supabase import sql_policy
from jhin_connectors.supabase.sql_policy import (
    MAX_SQL_AST_DEPTH,
    MAX_SQL_AST_NODES,
    MAX_SQL_TOKENS,
    RelationRef,
    SqlPolicyError,
    classify_and_validate_sql,
)


@pytest.mark.parametrize(
    ("expected", "sql"),
    [
        ("read", "SELECT 1"),
        ("read", "SELECT id FROM public.widgets"),
        (
            "read",
            "WITH named_cte AS (SELECT id FROM public.widgets) SELECT id FROM named_cte",
        ),
        (
            "read",
            "SELECT w.id FROM public.widgets AS w "
            "JOIN public.widget_groups AS g ON w.group_id = g.id "
            "WHERE w.id BETWEEN 1 AND 10 ORDER BY w.id LIMIT 5 OFFSET 0",
        ),
        (
            "read",
            "SELECT id FROM public.widgets UNION SELECT id FROM public.archived_widgets",
        ),
        (
            "read",
            'SELECT w."MixedCase" AS "Result" FROM public."Widgets" AS w',
        ),
        (
            "write",
            "INSERT INTO public.widgets (id, group_id, name) "
            "VALUES ($1, $2, $3), ($4, $5, 'literal')",
        ),
        (
            "write",
            "INSERT INTO public.widgets (id, name) VALUES (1::bigint, 'ok'::text)",
        ),
        (
            "destructive",
            "UPDATE public.widgets SET name = $1 WHERE id = $2",
        ),
        (
            "destructive",
            "UPDATE public.widgets AS w SET name = $1 "
            "FROM public.widget_groups AS g WHERE w.group_id = g.id",
        ),
        (
            "destructive",
            "DELETE FROM public.widgets AS w USING public.widget_groups AS g "
            "WHERE w.group_id = g.id",
        ),
        ("destructive", "TRUNCATE public.widgets"),
        ("destructive", "TRUNCATE public.widgets CONTINUE IDENTITY RESTRICT"),
    ],
)
def test_exact_allowed_statement_matrix(expected: str, sql: str) -> None:
    validated = classify_and_validate_sql(
        sql,
        expected=expected,  # type: ignore[arg-type]
        requested_schema="public",
    )

    assert validated.sql_class == expected
    assert validated.statement_type


@pytest.mark.parametrize(
    ("expected", "sql"),
    [
        ("read", ""),
        ("read", "-- comment only"),
        ("read", "SELECT 1;"),
        ("read", "SELECT 1; SELECT 2"),
        ("read", "VALUES (1)"),
        ("read", "SELECT id INTO public.copied FROM public.widgets"),
        ("read", "SELECT id FROM public.widgets FOR UPDATE"),
        ("read", "SELECT id FROM public.widgets FOR NO KEY UPDATE"),
        ("read", "SELECT id FROM public.widgets FOR SHARE"),
        ("read", "SELECT id FROM public.widgets FOR KEY SHARE"),
        (
            "read",
            "WITH changed AS (DELETE FROM public.widgets RETURNING id) SELECT id FROM changed",
        ),
        ("read", "EXPLAIN SELECT id FROM public.widgets"),
        ("read", "ANALYZE public.widgets"),
        ("read", "COPY public.widgets TO STDOUT"),
        ("read", "CALL public.run_job()"),
        ("read", "DO $$ BEGIN NULL; END $$"),
        ("read", "PREPARE q AS SELECT 1"),
        ("read", "EXECUTE q"),
        ("read", "DEALLOCATE q"),
        ("read", "DECLARE c CURSOR FOR SELECT 1"),
        ("read", "FETCH NEXT FROM c"),
        ("read", "LOCK TABLE public.widgets"),
        ("read", "LISTEN channel"),
        ("read", "NOTIFY channel"),
        ("read", "SET search_path = public"),
        ("read", "RESET ALL"),
        ("read", "DISCARD ALL"),
        ("read", "BEGIN"),
        ("read", "COMMIT"),
        ("read", "ROLLBACK"),
        ("write", "UPDATE public.widgets SET name = $1"),
        ("write", "DELETE FROM public.widgets"),
        ("write", "TRUNCATE public.widgets"),
        ("write", "MERGE INTO public.widgets USING public.other ON true WHEN MATCHED THEN DELETE"),
        ("write", "INSERT INTO public.widgets DEFAULT VALUES"),
        (
            "write",
            "INSERT INTO public.widgets (id) SELECT id FROM public.other",
        ),
        (
            "write",
            "INSERT INTO public.widgets (id) VALUES ($1) ON CONFLICT DO NOTHING",
        ),
        (
            "write",
            "INSERT INTO public.widgets (id) OVERRIDING SYSTEM VALUE VALUES ($1)",
        ),
        ("write", "INSERT INTO public.widgets (id) VALUES (DEFAULT)"),
        ("write", "INSERT INTO public.widgets (id) VALUES ($1) RETURNING id"),
        ("destructive", "INSERT INTO public.widgets (id) VALUES ($1)"),
        ("destructive", "TRUNCATE public.widgets, public.other"),
        ("destructive", "TRUNCATE ONLY public.widgets"),
        ("destructive", "TRUNCATE public.widgets *"),
        ("destructive", "TRUNCATE public.widgets RESTART IDENTITY"),
        ("destructive", "TRUNCATE public.widgets CASCADE"),
        ("destructive", "UPDATE public.widgets SET name = other.name FROM public.other"),
        ("destructive", "UPDATE public.widgets SET name = name || 'x'"),
        ("destructive", "UPDATE public.widgets SET name = DEFAULT"),
        ("destructive", "UPDATE public.widgets SET name = $1 RETURNING id"),
        ("destructive", "DELETE FROM public.widgets RETURNING id"),
        ("read", "CREATE TABLE public.created (id integer)"),
        ("read", "ALTER TABLE public.widgets ADD COLUMN hidden integer"),
        ("read", "DROP TABLE public.widgets"),
        ("read", "GRANT SELECT ON public.widgets TO someone"),
        ("read", "REVOKE SELECT ON public.widgets FROM someone"),
    ],
)
def test_exact_denied_statement_matrix(expected: str, sql: str) -> None:
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(
            sql,
            expected=expected,  # type: ignore[arg-type]
            requested_schema="public",
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM widgets",
        "SELECT id FROM other.widgets",
        "SELECT id FROM pg_catalog.pg_class",
        "SELECT id FROM information_schema.tables",
        "SELECT id FROM pg_toast.hidden",
        "SELECT id FROM public.widgets JOIN public.other ON widgets.id = other.id",
    ],
)
def test_physical_relations_are_qualified_scoped_and_join_columns_unambiguous(
    sql: str,
) -> None:
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(sql, expected="read", requested_schema="public")


def test_cte_scopes_shadow_tables_without_hiding_physical_relations() -> None:
    validated = classify_and_validate_sql(
        "WITH RECURSIVE widgets AS ("
        "SELECT id FROM public.widgets UNION ALL "
        "SELECT w.id FROM widgets AS w WHERE w.id < 5"
        "), nested AS (SELECT id FROM widgets) "
        "SELECT nested.id FROM nested",
        expected="read",
        requested_schema="public",
    )

    assert validated.relations == (RelationRef("public", "widgets", "source"),)


def test_relation_metadata_is_immutable_sorted_and_marks_targets() -> None:
    validated = classify_and_validate_sql(
        "UPDATE public.widgets AS w SET name = $1 "
        "FROM public.alpha AS a, public.zeta AS z "
        "WHERE w.id = a.id AND a.id = z.id",
        expected="destructive",
        requested_schema="public",
    )

    assert validated.relations == (
        RelationRef("public", "alpha", "source"),
        RelationRef("public", "widgets", "target"),
        RelationRef("public", "zeta", "source"),
    )
    assert validated.mutation_target == RelationRef("public", "widgets", "target")
    with pytest.raises(AttributeError):
        validated.sql_class = "read"  # type: ignore[misc]


def test_unquoted_identifiers_use_postgres_ascii_folding_only() -> None:
    ascii_folded = classify_and_validate_sql(
        "SELECT W.ID FROM PUBLIC.WIDGETS AS W",
        expected="read",
        requested_schema="public",
    )
    non_ascii = classify_and_validate_sql(
        'SELECT x."ÄBC" FROM public."ÄBC" AS x',
        expected="read",
        requested_schema="public",
    )

    assert ascii_folded.relations == (RelationRef("public", "widgets", "source"),)
    assert non_ascii.relations == (RelationRef("public", "ÄBC", "source"),)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT public.widgets.id FROM public.widgets",
        'SELECT id FROM public."bad\u0085name"',
        'SELECT id FROM public."bad\u202ename"',
        'SELECT "bad\ud800" FROM public.widgets',
    ],
)
def test_column_schema_qualifiers_and_unsafe_identifiers_are_denied(sql: str) -> None:
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(sql, expected="read", requested_schema="public")


@pytest.mark.parametrize(
    ("expected", "sql"),
    [
        ("read", "SELECT ctid FROM public.widgets"),
        ("read", "SELECT w.id FROM public.widgets AS w WHERE w.xmin IS NOT NULL"),
        ("read", "SELECT id FROM public.widgets WHERE xmax = 0"),
        ("read", "SELECT cmin FROM public.widgets"),
        ("read", "SELECT cmax FROM public.widgets"),
        ("read", "SELECT tableoid FROM public.widgets"),
        ("destructive", "UPDATE public.widgets SET name = $1 WHERE ctid IS NOT NULL"),
        ("destructive", "DELETE FROM public.widgets WHERE widgets.xmin = 0"),
    ],
)
def test_implicit_postgres_system_columns_are_denied(
    expected: str,
    sql: str,
) -> None:
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(
            sql,
            expected=expected,  # type: ignore[arg-type]
            requested_schema="public",
        )


def test_system_column_substrings_and_output_aliases_remain_allowed() -> None:
    validated = classify_and_validate_sql(
        'SELECT ctid_value AS ctid, xmin_value AS "xmax" '
        "FROM public.widgets WHERE tableoid_value = 1",
        expected="read",
        requested_schema="public",
    )

    assert validated.relations == (RelationRef("public", "widgets", "source"),)


def test_postgres_identifier_byte_boundary_accepts_63_and_rejects_64() -> None:
    exact_alias = "a" * 63
    exact_column = "c" * 63
    exact_output = "o" * 63
    over_alias = exact_alias + "x"
    over_column = exact_column + "x"
    over_output = exact_output + "x"

    accepted = classify_and_validate_sql(
        f'SELECT "{exact_alias}"."{exact_column}" AS "{exact_output}" '
        f'FROM public.widgets AS "{exact_alias}"',
        expected="read",
        requested_schema="public",
    )
    assert accepted.relations == (RelationRef("public", "widgets", "source"),)

    for sql in (
        f'SELECT "{over_alias}".id FROM public.widgets AS "{over_alias}"',
        f'SELECT "{over_alias}".id FROM public.widgets AS "{exact_alias}"',
        f'SELECT "{over_column}" FROM public.widgets',
        f'SELECT id AS "{over_output}" FROM public.widgets',
    ):
        with pytest.raises(SqlPolicyError, match="unsupported SQL"):
            classify_and_validate_sql(sql, expected="read", requested_schema="public")


def test_identifier_limit_counts_strict_utf8_bytes_not_characters() -> None:
    exact = "é" * 31 + "x"
    over = exact + "y"

    classify_and_validate_sql(
        f'SELECT id AS "{exact}" FROM public.widgets',
        expected="read",
        requested_schema="public",
    )
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(
            f'SELECT id AS "{over}" FROM public.widgets',
            expected="read",
            requested_schema="public",
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT dblink('x', 'SELECT 1')",
        "SELECT setval('seq', 1)",
        "SELECT nextval('seq')",
        "SELECT pg_sleep(1)",
        "SELECT current_user",
        "SELECT session_user",
        "SELECT pg_catalog.current_user",
        "SELECT public.session_user",
        "SELECT user",
        "SELECT current_role",
        "SELECT system_user",
        "SELECT public.custom_function(1)",
        "SELECT custom_function(1)",
        "SELECT IF(TRUE, 1, 0)",
        "SELECT IF(id = 1, 2, 3) FROM public.widgets",
        "SELECT count(*) FROM public.widgets",
        "SELECT row_number() OVER () FROM public.widgets",
        "SELECT 1 COLLATE public.custom_collation",
        "SELECT 1 OPERATOR(public.+) 2",
        "SELECT id::integer FROM public.widgets",
        "SELECT $1::integer",
        "SELECT TRY_CAST(1 AS integer)",
        "SELECT 1::public.custom_type",
        "SELECT 1::integer[]",
    ],
)
def test_functions_operators_collations_and_unsafe_casts_are_denied(sql: str) -> None:
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(sql, expected="read", requested_schema="public")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CAST(1 AS boolean)",
        "SELECT CAST(-1 AS smallint)",
        "SELECT CAST(1 AS integer)",
        "SELECT CAST(1 AS bigint)",
        "SELECT CAST(1.5 AS numeric)",
        "SELECT CAST(1.5 AS real)",
        "SELECT CAST(1.5 AS double precision)",
        "SELECT CAST('x' AS text)",
        "SELECT CAST('x' AS varchar)",
        "SELECT CAST('2026-08-18' AS date)",
        "SELECT CAST('2026-08-18' AS timestamp)",
        "SELECT CAST('2026-08-18Z' AS timestamptz)",
        "SELECT CAST('00000000-0000-0000-0000-000000000000' AS uuid)",
        "SELECT CAST('{}' AS json)",
        "SELECT CAST('{}' AS jsonb)",
        "SELECT CAST(CAST(1 AS integer) AS bigint)",
    ],
)
def test_only_literal_casts_to_fixed_semantic_builtin_types_are_allowed(sql: str) -> None:
    validated = classify_and_validate_sql(
        sql,
        expected="read",
        requested_schema="public",
    )

    assert validated.sql_class == "read"


def test_case_branches_remain_allowed_without_authorizing_if_function() -> None:
    validated = classify_and_validate_sql(
        "SELECT CASE WHEN id = 1 THEN 2 ELSE 3 END FROM public.widgets",
        expected="read",
        requested_schema="public",
    )

    assert validated.sql_class == "read"


@pytest.mark.parametrize(
    ("sql", "indexes"),
    [
        ("SELECT $1", (1,)),
        ("SELECT $2 + $1 + $2", (1, 2)),
        ("SELECT $1,$2", (1, 2)),
        ("SELECT '$1', $$ $2 $$, 1 -- $3", ()),
        ("SELECT 1 /* $1 */", ()),
    ],
)
def test_postgresql_parameter_indexes_ignore_literals_and_comments(
    sql: str,
    indexes: tuple[int, ...],
) -> None:
    validated = classify_and_validate_sql(
        sql,
        expected="read",
        requested_schema="public",
    )

    assert validated.parameter_indexes == indexes


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ?",
        "SELECT :name",
        "SELECT $0",
        "SELECT $2",
        "SELECT $51",
    ],
)
def test_non_postgresql_or_out_of_range_parameters_are_denied(sql: str) -> None:
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(sql, expected="read", requested_schema="public")


def test_pathologically_long_placeholder_is_stably_rejected() -> None:
    marker = "9" * 5_000

    with pytest.raises(SqlPolicyError, match="unsupported SQL") as exc_info:
        classify_and_validate_sql(
            "SELECT $" + marker,
            expected="read",
            requested_schema="public",
        )

    assert str(exc_info.value) == "unsupported SQL"


def test_mutation_metadata_preserves_repeated_value_occurrences() -> None:
    insert = classify_and_validate_sql(
        "INSERT INTO public.widgets (id, name) VALUES ($1, $2), ($1, 'fixed')",
        expected="write",
        requested_schema="public",
    )
    update = classify_and_validate_sql(
        "UPDATE public.widgets SET name = $1, label = 'fixed' WHERE id = $2",
        expected="destructive",
        requested_schema="public",
    )

    assert insert.parameter_indexes == (1, 2)
    assert [value.parameter_index for value in insert.mutation_values] == [1, 2, 1, None]
    assert insert.insert_row_count == 2
    assert update.parameter_indexes == (1, 2)
    assert [value.parameter_index for value in update.mutation_values] == [1, None]


def test_semicolons_inside_comments_and_literals_are_not_statement_terminators() -> None:
    for sql in (
        "SELECT ';'",
        "SELECT $$ ; $$",
        "SELECT 1 -- ;",
        "SELECT 1 /* ; */",
    ):
        assert (
            classify_and_validate_sql(
                sql,
                expected="read",
                requested_schema="public",
            ).sql_class
            == "read"
        )


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT E'lexical-secret-marker\'quote'",
        r"SELECT e'lexical-secret-marker\\backslash'",
        r"SELECT U&'lexical-secret-marker\0061'",
        r"SELECT U&'lexical-secret-marker!0061' UESCAPE '!'",
        r'SELECT U&"lexical-secret-marker\0061" FROM public.widgets',
        r'SELECT U&"lexical-secret-marker!0061" FROM public.widgets UESCAPE \'!\'',
    ],
)
def test_escape_and_unicode_lexical_forms_are_rejected_without_leaks(
    sql: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SqlPolicyError, match="unsupported SQL") as exc_info:
        classify_and_validate_sql(sql, expected="read", requested_schema="public")

    assert str(exc_info.value) == "unsupported SQL"
    assert "lexical-secret-marker" not in caplog.text


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'it''s $1; still one literal'",
        "SELECT $outer$before $inner$ $1; $inner$ after$outer$",
        "SELECT $tag_1$quoted '' text, $2); /* still text */$tag_1$",
    ],
)
def test_standard_and_nested_dollar_literals_preserve_parser_only_scanning(sql: str) -> None:
    validated = classify_and_validate_sql(
        sql,
        expected="read",
        requested_schema="public",
    )

    assert validated.parameter_indexes == ()


def test_backslash_quote_with_adjacent_parameter_is_rejected_without_leaks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    sql = r"SELECT 'lexical-secret-marker\'$1,'b'"

    with pytest.raises(SqlPolicyError, match="unsupported SQL") as exc_info:
        classify_and_validate_sql(sql, expected="read", requested_schema="public")

    assert str(exc_info.value) == "unsupported SQL"
    assert "lexical-secret-marker" not in caplog.text


@pytest.mark.parametrize(
    ("sql", "indexes"),
    [
        (r"SELECT 'a\b'", ()),
        (r"SELECT 'a\', $1", (1,)),
        (r"SELECT 'a\',$1 -- trailing quote '", (1,)),
    ],
)
def test_valid_standard_string_backslashes_remain_allowed(
    sql: str,
    indexes: tuple[int, ...],
) -> None:
    validated = classify_and_validate_sql(
        sql,
        expected="read",
        requested_schema="public",
    )

    assert validated.parameter_indexes == indexes


def test_invalid_or_fallback_sql_is_credential_safe_and_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "sql-policy-secret-marker"
    caplog.set_level(logging.DEBUG)

    for sql in (f"SELECT '{marker}", f"VACUUM {marker}"):
        with pytest.raises(SqlPolicyError) as exc_info:
            classify_and_validate_sql(sql, expected="read", requested_schema="public")
        assert str(exc_info.value) == "unsupported SQL"

    assert marker not in caplog.text


def test_sql_resource_limits_are_fixed_and_enforced() -> None:
    assert MAX_SQL_TOKENS == 1_024
    assert MAX_SQL_AST_NODES == 512
    assert MAX_SQL_AST_DEPTH == 64

    too_many_tokens = "SELECT " + ", ".join("1" for _ in range(513))
    too_deep = "SELECT " + "(" * 65 + "1" + ")" * 65
    too_many_nodes = "SELECT " + " + ".join("1" for _ in range(300))
    for sql in (too_many_tokens, too_deep, too_many_nodes):
        with pytest.raises(SqlPolicyError, match="unsupported SQL"):
            classify_and_validate_sql(sql, expected="read", requested_schema="public")


def test_sql_resource_helpers_accept_exact_token_node_and_depth_boundaries() -> None:
    exact_tokens_sql = "SELECT " + ", ".join("1" for _ in range(512))
    _parser_sql, tokens = sql_policy._tokenize_sql(exact_tokens_sql)
    assert len(tokens) == MAX_SQL_TOKENS

    exact_nodes = exp.Tuple(expressions=[exp.Literal.number(1) for _ in range(511)])
    assert len(sql_policy._walk_bounded(exact_nodes)) == MAX_SQL_AST_NODES
    over_nodes = exp.Tuple(expressions=[exp.Literal.number(1) for _ in range(512)])
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        sql_policy._walk_bounded(over_nodes)

    exact_depth: exp.Expression = exp.Literal.number(1)
    for _ in range(MAX_SQL_AST_DEPTH - 1):
        exact_depth = exp.Paren(this=exact_depth)
    walked = sql_policy._walk_bounded(exact_depth)
    assert len(walked) == MAX_SQL_AST_DEPTH
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        sql_policy._walk_bounded(exp.Paren(this=exact_depth))


def test_sql_token_limit_accepts_exact_boundary_and_rejects_cap_plus_one() -> None:
    exact_items = ["CAST(1 AS integer) AS boundary"] + ["CAST(1 AS integer)"] * 145
    exact_sql = "SELECT " + ", ".join(exact_items)
    over_sql = exact_sql.replace(
        "CAST(1 AS integer) AS boundary",
        "CAST(+1 AS integer) AS boundary",
        1,
    )
    assert len(tokenize(exact_sql, read="postgres")) == MAX_SQL_TOKENS
    assert len(tokenize(over_sql, read="postgres")) == MAX_SQL_TOKENS + 1

    accepted = classify_and_validate_sql(
        exact_sql,
        expected="read",
        requested_schema="public",
    )

    assert accepted.sql_class == "read"
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(
            over_sql,
            expected="read",
            requested_schema="public",
        )


def test_sql_ast_node_limit_accepts_exact_boundary_and_rejects_cap_plus_one() -> None:
    # SELECT contributes one node; every scalar literal contributes one more.
    exact_sql = "SELECT " + ", ".join("1" for _ in range(MAX_SQL_AST_NODES - 1))
    over_sql = exact_sql + ", 1"

    accepted = classify_and_validate_sql(
        exact_sql,
        expected="read",
        requested_schema="public",
    )

    assert accepted.sql_class == "read"
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(
            over_sql,
            expected="read",
            requested_schema="public",
        )


def test_sql_ast_depth_limit_accepts_exact_boundary_and_rejects_cap_plus_one() -> None:
    # SELECT + 62 left-associated Add nodes + the deepest literal reaches depth 64.
    exact_sql = "SELECT " + " + ".join("1" for _ in range(MAX_SQL_AST_DEPTH - 1))
    over_sql = exact_sql + " + 1"

    accepted = classify_and_validate_sql(
        exact_sql,
        expected="read",
        requested_schema="public",
    )

    assert accepted.sql_class == "read"
    with pytest.raises(SqlPolicyError, match="unsupported SQL"):
        classify_and_validate_sql(
            over_sql,
            expected="read",
            requested_schema="public",
        )
