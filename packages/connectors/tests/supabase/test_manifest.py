"""Static Supabase connector contracts and auth-plane separation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jhin_connectors.manifest import normalize_config
from jhin_connectors.supabase.connector import SupabaseConnector
from jhin_connectors.supabase.schemas import (
    DatabaseMutationInput,
    DatabaseMutationOutput,
    DatabaseReadInput,
    DatabaseReadOutput,
    FunctionDeployInput,
    LogsReadInput,
    SourceFile,
)
from jhin_policy import RiskLevel

EXPECTED_MANAGEMENT_TOOLS = {
    "supabase.project.read",
    "supabase.logs.read",
    "supabase.function.list",
    "supabase.function.deploy",
    "supabase.function.delete",
}
EXPECTED_DATABASE_TOOLS = {
    "supabase.database.read",
    "supabase.database.write",
    "supabase.database.destructive",
}


def _normalized(
    connector: SupabaseConnector,
    auth_type: str,
    submitted: dict[str, object],
) -> dict[str, object]:
    generic = normalize_config(connector.manifest, auth_type, submitted)
    return connector.validate_settings(auth_type, generic)


def test_manifest_declares_two_independent_auth_schemes() -> None:
    manifest = SupabaseConnector.manifest
    schemes = {scheme.type: scheme for scheme in manifest.auth_schemes}

    assert manifest.connector_type == "supabase"
    assert manifest.display_name == "Supabase"
    assert set(schemes) == {"management_token", "postgres"}
    assert schemes["management_token"].required_field_names() == ("access_token",)
    assert schemes["postgres"].required_field_names() == ("database_url",)
    assert manifest.webhook_secret_mode == "none"
    assert manifest.supports_webhooks is False


def test_manifest_exposes_auth_specific_config_defaults_and_bounds() -> None:
    fields = {field.name: field for field in SupabaseConnector.manifest.config_fields}

    assert fields["project_ref"].required is True
    assert set(fields["project_ref"].auth_types) == {"management_token", "postgres"}
    assert fields["base_url"].auth_types == ("management_token",)
    assert fields["base_url"].default == "https://api.supabase.com"
    assert fields["allowed_schemas"].auth_types == ("postgres",)
    assert fields["allowed_schemas"].default == ["public"]
    assert fields["allow_writes"].default is False
    assert "allow_ddl" not in fields
    assert (fields["statement_timeout_ms"].minimum, fields["statement_timeout_ms"].maximum) == (
        250,
        30_000,
    )
    assert (fields["lock_timeout_ms"].minimum, fields["lock_timeout_ms"].maximum) == (
        100,
        5_000,
    )
    assert (fields["max_rows"].minimum, fields["max_rows"].maximum) == (1, 1_000)
    assert (fields["max_cell_bytes"].minimum, fields["max_cell_bytes"].maximum) == (
        256,
        8_000,
    )
    assert (fields["max_result_bytes"].minimum, fields["max_result_bytes"].maximum) == (
        4_096,
        30_000,
    )


def test_management_settings_reject_database_plane_fields() -> None:
    connector = SupabaseConnector()

    with pytest.raises(ValueError, match="not allowed"):
        _normalized(
            connector,
            "management_token",
            {"project_ref": "abcdefghijklmnopqrst", "allowed_schemas": ["public"]},
        )


def test_postgres_settings_reject_management_plane_fields() -> None:
    connector = SupabaseConnector()

    with pytest.raises(ValueError, match="not allowed"):
        _normalized(
            connector,
            "postgres",
            {"project_ref": "abcdefghijklmnopqrst", "base_url": "https://api.supabase.com"},
        )


def test_auth_specific_defaults_are_normalized_without_cross_plane_values() -> None:
    connector = SupabaseConnector()

    management = _normalized(
        connector,
        "management_token",
        {"project_ref": "abcdefghijklmnopqrst"},
    )
    postgres = _normalized(
        connector,
        "postgres",
        {"project_ref": "abcdefghijklmnopqrst"},
    )

    assert management == {
        "project_ref": "abcdefghijklmnopqrst",
        "base_url": "https://api.supabase.com",
    }
    assert postgres == {
        "project_ref": "abcdefghijklmnopqrst",
        "allowed_schemas": ["public"],
        "allow_writes": False,
        "statement_timeout_ms": 5_000,
        "lock_timeout_ms": 1_000,
        "max_rows": 200,
        "max_cell_bytes": 4_096,
        "max_result_bytes": 24_000,
    }


def test_postgres_settings_require_cell_budget_within_result_budget() -> None:
    with pytest.raises(ValueError, match="max_cell_bytes"):
        _normalized(
            SupabaseConnector(),
            "postgres",
            {
                "project_ref": "abcdefghijklmnopqrst",
                "max_cell_bytes": 8_000,
                "max_result_bytes": 4_096,
            },
        )

    assert (
        _normalized(
            SupabaseConnector(),
            "postgres",
            {
                "project_ref": "abcdefghijklmnopqrst",
                "max_cell_bytes": 8_000,
                "max_result_bytes": 8_000,
            },
        )["max_cell_bytes"]
        == 8_000
    )


@pytest.mark.parametrize(
    "project_ref",
    ["", "UPPERCASE", "bad/ref", "-leading", "trailing-", "x" * 64],
)
def test_settings_reject_invalid_project_references(project_ref: str) -> None:
    with pytest.raises(ValueError, match="project_ref"):
        _normalized(SupabaseConnector(), "management_token", {"project_ref": project_ref})


@pytest.mark.parametrize(
    "allowed_schemas",
    [
        [],
        [""],
        ["public", "public"],
        ["public", "PUBLIC"],
        ["pg_catalog"],
        ["pg_temp_3"],
        ["information_schema"],
        ["bad.schema"],
    ],
)
def test_postgres_settings_reject_empty_duplicate_or_system_schemas(
    allowed_schemas: list[str],
) -> None:
    with pytest.raises(ValueError, match="allowed_schemas"):
        _normalized(
            SupabaseConnector(),
            "postgres",
            {"project_ref": "abcdefghijklmnopqrst", "allowed_schemas": allowed_schemas},
        )


def test_postgres_settings_cap_allowed_schemas_at_eight() -> None:
    connector = SupabaseConnector()
    accepted = [f"schema_{index}" for index in range(8)]

    assert (
        _normalized(
            connector,
            "postgres",
            {"project_ref": "abcdefghijklmnopqrst", "allowed_schemas": accepted},
        )["allowed_schemas"]
        == accepted
    )
    with pytest.raises(ValueError, match="allowed_schemas"):
        _normalized(
            connector,
            "postgres",
            {
                "project_ref": "abcdefghijklmnopqrst",
                "allowed_schemas": [f"schema_{index}" for index in range(9)],
            },
        )


def test_postgres_settings_reject_removed_ddl_authority() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        _normalized(
            SupabaseConnector(),
            "postgres",
            {
                "project_ref": "abcdefghijklmnopqrst",
                "allow_writes": True,
                "allow_ddl": True,
            },
        )


def test_management_origin_is_normalized_at_create_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://fake-supabase:8080")
    connector = SupabaseConnector()

    assert (
        _normalized(
            connector,
            "management_token",
            {
                "project_ref": "abcdefghijklmnopqrst",
                "base_url": "HTTP://FAKE-SUPABASE:8080/",
            },
        )["base_url"]
        == "http://fake-supabase:8080"
    )

    marker = "unsafe-supabase-origin-marker"
    with pytest.raises(ValueError) as exc_info:
        _normalized(
            connector,
            "management_token",
            {
                "project_ref": "abcdefghijklmnopqrst",
                "base_url": f"https://{marker}@api.supabase.com",
            },
        )
    assert marker not in str(exc_info.value)


def test_management_tool_definitions_have_fixed_risk_and_required_scopes() -> None:
    connector = SupabaseConnector()
    definitions = {definition.name: definition for definition, _ in connector.tools()}

    assert set(definitions) == EXPECTED_MANAGEMENT_TOOLS | EXPECTED_DATABASE_TOOLS
    assert set(connector.manifest.capabilities) == (
        EXPECTED_MANAGEMENT_TOOLS | EXPECTED_DATABASE_TOOLS
    )
    for name in EXPECTED_MANAGEMENT_TOOLS:
        definition = definitions[name]
        expected_required = {"connection_id", "project_ref"}
        if name in {"supabase.function.deploy", "supabase.function.delete"}:
            expected_required.add("function_slug")
            assert definition.risk is RiskLevel.DESTRUCTIVE
            assert definition.supports_approval is True
        else:
            assert definition.risk is RiskLevel.READ
            assert definition.supports_approval is False
        assert set(definition.required_grant_scope_keys) == expected_required
        assert expected_required.issubset(definition.scope_keys)


def test_database_tool_definitions_have_fixed_risks_and_scopes_without_ddl() -> None:
    definitions = {definition.name: definition for definition, _ in SupabaseConnector().tools()}
    expected = {
        "supabase.database.read": (RiskLevel.READ, False),
        "supabase.database.write": (RiskLevel.ELEVATED, True),
        "supabase.database.destructive": (RiskLevel.DESTRUCTIVE, True),
    }

    for name, (risk, supports_approval) in expected.items():
        definition = definitions[name]
        assert definition.risk is risk
        assert definition.supports_approval is supports_approval
        assert set(definition.scope_keys) == {"connection_id", "project_ref", "schema"}
        assert set(definition.required_grant_scope_keys) == {
            "connection_id",
            "project_ref",
            "schema",
        }
    assert "supabase.database.ddl" not in definitions
    assert "supabase.database.ddl" not in SupabaseConnector.manifest.capabilities


def test_database_request_models_preserve_strict_bounded_json_scalars() -> None:
    params = [None, True, False, -(2**63), 2**63 - 1, 1.25, "café"]
    payload = DatabaseReadInput(
        connection_id="connection",
        project_ref="abcdefghijklmnopqrst",
        schema="public",
        sql="SELECT id FROM public.widgets WHERE id = $1",
        params=params,
    )

    assert payload.model_dump(mode="json")["params"] == params
    mutation = DatabaseMutationInput.model_validate(payload.model_dump(mode="json"))
    assert mutation.params == params


@pytest.mark.parametrize(
    "params",
    [
        [[1]],
        [{"value": 1}],
        [2**63],
        [-(2**63) - 1],
        [float("inf")],
        [float("nan")],
        ["x" * 8_193],
        ["x" * 8_000, "y" * 8_000],
        list(range(51)),
    ],
)
def test_database_request_models_reject_unsupported_or_oversized_params(
    params: object,
) -> None:
    with pytest.raises(ValidationError):
        DatabaseReadInput(
            connection_id="connection",
            project_ref="abcdefghijklmnopqrst",
            schema="public",
            sql="SELECT 1",
            params=params,
        )


@pytest.mark.parametrize(
    "sql",
    [
        "x" * 7_001,
        "SELECT 1\x00",
        "SELECT 1\u202e",
        "SELECT '\ud800'",
    ],
)
def test_database_request_models_reject_oversized_or_unsafe_sql(sql: str) -> None:
    with pytest.raises(ValidationError):
        DatabaseReadInput(
            connection_id="connection",
            project_ref="abcdefghijklmnopqrst",
            schema="public",
            sql=sql,
            params=[],
        )


def test_database_outputs_are_positional_and_bounded() -> None:
    output = DatabaseReadOutput(
        columns=["duplicate", "duplicate"],
        rows=[["first", "second"], [None, "third"]],
        row_count=2,
        truncated=False,
    )
    mutation = DatabaseMutationOutput(affected_rows=2)

    assert output.rows[0] == ["first", "second"]
    assert output.model_dump() == {
        "columns": ["duplicate", "duplicate"],
        "rows": [["first", "second"], [None, "third"]],
        "row_count": 2,
        "truncated": False,
    }
    assert mutation.model_dump() == {"affected_rows": 2}


def test_logs_input_is_typed_bounded_and_never_accepts_sql() -> None:
    valid = LogsReadInput(
        connection_id="connection",
        project_ref="abcdefghijklmnopqrst",
        source="edge_logs",
        start="2026-08-17T00:00:00Z",
        end="2026-08-18T00:00:00Z",
        limit=200,
        text_filter="needle",
    )
    assert valid.limit == 200

    with pytest.raises(ValidationError):
        LogsReadInput(
            connection_id="connection",
            project_ref="abcdefghijklmnopqrst",
            source="edge_logs",
            start="2026-08-17T00:00:00Z",
            end="2026-08-18T00:00:01Z",
        )
    with pytest.raises(ValidationError):
        LogsReadInput(
            connection_id="connection",
            project_ref="abcdefghijklmnopqrst",
            source="unknown_logs",
            start="2026-08-17T00:00:00Z",
            end="2026-08-17T00:01:00Z",
        )
    with pytest.raises(ValidationError):
        LogsReadInput(
            connection_id="connection",
            project_ref="abcdefghijklmnopqrst",
            source="edge_logs",
            start="2026-08-17T00:00:00Z",
            end="2026-08-17T00:01:00Z",
            sql="SELECT * FROM logs",
        )


@pytest.mark.parametrize(
    "path",
    [
        "/index.ts",
        ".",
        "..",
        "src/../index.ts",
        "src/./index.ts",
        "src\\index.ts",
        "src//index.ts",
        "src/index.ts\x00",
        "src/index\u0085.ts",
        "src/index\u202e.ts",
    ],
)
def test_function_source_paths_are_strict_posix_relative(path: str) -> None:
    with pytest.raises(ValidationError):
        SourceFile(path=path, content="export default () => new Response('ok')")


def test_function_source_path_allows_ordinary_unicode_letters() -> None:
    assert SourceFile(path="données/файл.ts", content="x").path == "données/файл.ts"


def test_function_source_content_is_bounded_by_utf8_bytes() -> None:
    assert SourceFile(path="index.ts", content="x" * 6_144).path == "index.ts"

    with pytest.raises(ValidationError):
        SourceFile(path="index.ts", content="é" * 3_073)


def test_function_deploy_requires_lossless_bounded_unique_source_bundle() -> None:
    common = {
        "connection_id": "connection",
        "project_ref": "abcdefghijklmnopqrst",
        "function_slug": "hello-world",
        "entrypoint_path": "index.ts",
        "verify_jwt": True,
    }
    assert (
        FunctionDeployInput(
            **common,
            files=[{"path": "index.ts", "content": "export default () => 'ok'"}],
        ).verify_jwt
        is True
    )

    invalid_file_sets = [
        [],
        [{"path": f"file-{index}.ts", "content": "x"} for index in range(9)],
        [
            {"path": "index.ts", "content": "one"},
            {"path": "index.ts", "content": "two"},
        ],
        [
            {"path": "index.ts", "content": "x" * 6_144},
            {"path": "a.ts", "content": "x" * 6_144},
            {"path": "b.ts", "content": "x" * 6_144},
            {"path": "c.ts", "content": "x" * 6_144},
        ],
    ]
    for files in invalid_file_sets:
        with pytest.raises(ValidationError):
            FunctionDeployInput(**common, files=files)

    with pytest.raises(ValidationError):
        FunctionDeployInput(
            **{**common, "entrypoint_path": "missing.ts"},
            files=[{"path": "index.ts", "content": "x"}],
        )
    with pytest.raises(ValidationError):
        FunctionDeployInput(
            connection_id="connection",
            project_ref="abcdefghijklmnopqrst",
            function_slug="hello-world",
            entrypoint_path="index.ts",
            files=[{"path": "index.ts", "content": "x"}],
        )
