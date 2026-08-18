"""Supabase Management API executors against the deterministic fake."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from jhin_connectors.base import VerifyContext
from jhin_connectors.supabase import management_client, management_tools
from jhin_connectors.supabase.connector import SupabaseConnector
from jhin_connectors.supabase.management_client import SupabaseManagementError
from jhin_connectors.supabase.management_tools import (
    MAX_FUNCTION_LIST_OUTPUT_BYTES,
    MAX_LOG_OUTPUT_BYTES,
)
from jhin_connectors.supabase.schemas import (
    FunctionDeleteInput,
    FunctionDeployInput,
    FunctionInfo,
    FunctionListInput,
    FunctionListOutput,
    LogsReadInput,
    LogsReadOutput,
    ProjectReadInput,
    ProjectReadOutput,
)
from jhin_connectors.testing.fake_supabase import FakeSupabaseServer
from jhin_db.models import Workspace
from jhin_secrets.redaction import SecretRedactor
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.sanitize import MAX_DOCUMENT_BYTES, sanitize_payload

PROJECT_REF = "abcdefghijklmnopqrst"
OTHER_PROJECT_REF = "tsrqponmlkjihgfedcba"
EXECUTORS = {definition.name: executor for definition, executor in SupabaseConnector().tools()}


@pytest.fixture
def fake_supabase(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSupabaseServer]:
    with FakeSupabaseServer(project_ref=PROJECT_REF) as server:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
        yield server


@pytest.fixture
async def management_connection(  # type: ignore[no-untyped-def]
    workspace: Workspace,
    make_connection,
    fake_supabase: FakeSupabaseServer,
):
    return await make_connection(
        workspace,
        connector_type="supabase",
        auth_type="management_token",
        credentials={"access_token": fake_supabase.state.token},
        config={"project_ref": PROJECT_REF, "base_url": fake_supabase.base_url},
    )


@pytest.fixture
async def postgres_connection(  # type: ignore[no-untyped-def]
    workspace: Workspace,
    make_connection,
):
    return await make_connection(
        workspace,
        connector_type="supabase",
        auth_type="postgres",
        credentials={"database_url": "postgresql://jhin_reader:secret@db.example.test/fixture"},
        config={"project_ref": PROJECT_REF, "allowed_schemas": ["public"]},
    )


async def _run(
    name: str,
    context: ToolExecutionContext,
    payload: BaseModel,
) -> BaseModel:
    return await EXECUTORS[name](context, payload)


def _assert_gateway_retains(output: BaseModel, collection_key: str) -> None:
    sanitized = sanitize_payload(output.model_dump(mode="json"))

    assert collection_key in sanitized
    assert "original_size_bytes" not in sanitized
    assert (
        len(json.dumps(sanitized, ensure_ascii=False, default=str).encode("utf-8"))
        <= MAX_DOCUMENT_BYTES
    )


def _deploy_input(connection_id: str, *, project_ref: str = PROJECT_REF) -> FunctionDeployInput:
    return FunctionDeployInput(
        connection_id=connection_id,
        project_ref=project_ref,
        function_slug="hello-world",
        entrypoint_path="index.ts",
        verify_jwt=True,
        files=[
            {
                "path": "index.ts",
                "content": "import { helper } from './helper.ts';\nexport default helper;",
            },
            {
                "path": "helper.ts",
                "content": "export const helper = () => new Response('source-secret-marker');",
            },
        ],
    )


def _logs_input(connection_id: str, *, project_ref: str = PROJECT_REF) -> LogsReadInput:
    return LogsReadInput(
        connection_id=connection_id,
        project_ref=project_ref,
        source="edge_logs",
        start="2026-08-17T00:00:00Z",
        end="2026-08-17T01:00:00Z",
        limit=17,
        text_filter="quote' and \\ slash",
    )


async def test_verify_management_connection_is_bounded_and_display_safe(
    fake_supabase: FakeSupabaseServer,
) -> None:
    health = await SupabaseConnector().verify_connection(
        VerifyContext(
            auth_type="management_token",
            credentials={"access_token": fake_supabase.state.token},
            config={"project_ref": PROJECT_REF, "base_url": fake_supabase.base_url},
        )
    )

    assert health.ok is True
    assert health.details == {"project_ref": PROJECT_REF, "name": "Fake Supabase Project"}
    rendered = health.model_dump_json()
    assert fake_supabase.state.token not in rendered
    assert "project-provider-secret" not in rendered


async def test_verify_management_connection_maps_auth_failure_without_token_leak(
    fake_supabase: FakeSupabaseServer,
) -> None:
    marker = "wrong-management-token-marker"

    health = await SupabaseConnector().verify_connection(
        VerifyContext(
            auth_type="management_token",
            credentials={"access_token": marker},
            config={"project_ref": PROJECT_REF, "base_url": fake_supabase.base_url},
        )
    )

    assert health.ok is False
    assert marker not in health.message


@pytest.mark.parametrize(
    ("tool_name", "payload_factory"),
    [
        (
            "supabase.project.read",
            lambda connection_id: ProjectReadInput(
                connection_id=connection_id, project_ref=PROJECT_REF
            ),
        ),
        ("supabase.logs.read", _logs_input),
        (
            "supabase.function.list",
            lambda connection_id: FunctionListInput(
                connection_id=connection_id, project_ref=PROJECT_REF
            ),
        ),
        ("supabase.function.deploy", _deploy_input),
        (
            "supabase.function.delete",
            lambda connection_id: FunctionDeleteInput(
                connection_id=connection_id,
                project_ref=PROJECT_REF,
                function_slug="hello-world",
            ),
        ),
    ],
)
async def test_every_management_executor_rejects_postgres_before_network(
    context: ToolExecutionContext,
    postgres_connection,
    fake_supabase: FakeSupabaseServer,
    tool_name: str,
    payload_factory,  # type: ignore[no-untyped-def]
) -> None:
    payload = payload_factory(str(postgres_connection.id))

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(tool_name, context, payload)

    assert exc_info.value.code == "unsupported_auth_type"
    assert fake_supabase.state.requests == []
    assert fake_supabase.state.counters == {"deploy": 0, "delete": 0}


@pytest.mark.parametrize(
    ("tool_name", "payload_factory"),
    [
        (
            "supabase.project.read",
            lambda connection_id: ProjectReadInput(
                connection_id=connection_id, project_ref=OTHER_PROJECT_REF
            ),
        ),
        (
            "supabase.logs.read",
            lambda connection_id: _logs_input(connection_id, project_ref=OTHER_PROJECT_REF),
        ),
        (
            "supabase.function.list",
            lambda connection_id: FunctionListInput(
                connection_id=connection_id, project_ref=OTHER_PROJECT_REF
            ),
        ),
        (
            "supabase.function.deploy",
            lambda connection_id: _deploy_input(connection_id, project_ref=OTHER_PROJECT_REF),
        ),
        (
            "supabase.function.delete",
            lambda connection_id: FunctionDeleteInput(
                connection_id=connection_id,
                project_ref=OTHER_PROJECT_REF,
                function_slug="hello-world",
            ),
        ),
    ],
)
async def test_every_management_executor_binds_configured_project_before_network(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
    tool_name: str,
    payload_factory,  # type: ignore[no-untyped-def]
) -> None:
    payload = payload_factory(str(management_connection.id))

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(tool_name, context, payload)

    assert exc_info.value.code == "project_scope_mismatch"
    assert fake_supabase.state.requests == []
    assert fake_supabase.state.counters == {"deploy": 0, "delete": 0}


async def test_project_read_binds_provider_ref_and_filters_unknown_fields(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    output = await _run(
        "supabase.project.read",
        context,
        ProjectReadInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
        ),
    )

    assert isinstance(output, ProjectReadOutput)
    assert output.model_dump() == {
        "project_id": "project-id-1",
        "project_ref": PROJECT_REF,
        "organization_id": "organization-id-1",
        "organization_slug": "fake-organization",
        "name": "Fake Supabase Project",
        "region": "us-west-1",
        "created_at": "2026-08-17T00:00:00Z",
        "status": "ACTIVE_HEALTHY",
    }
    rendered = output.model_dump_json()
    assert "project-provider-secret" not in rendered
    assert fake_supabase.state.token not in rendered
    _assert_gateway_retains(output, "project_ref")

    fake_supabase.state.project["ref"] = OTHER_PROJECT_REF
    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.project.read",
            context,
            ProjectReadInput(
                connection_id=str(management_connection.id),
                project_ref=PROJECT_REF,
            ),
        )
    assert exc_info.value.code == "project_scope_mismatch"


async def test_function_list_is_capped_truncated_and_display_safe(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.seed_many_functions(240)

    output = await _run(
        "supabase.function.list",
        context,
        FunctionListInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
            limit=200,
        ),
    )

    assert isinstance(output, FunctionListOutput)
    assert 1 <= len(output.functions) <= 200
    assert output.truncated is True
    rendered = output.model_dump_json()
    assert len(rendered.encode("utf-8")) <= MAX_FUNCTION_LIST_OUTPUT_BYTES
    assert "function-provider-secret" not in rendered
    assert "source-secret-marker" not in rendered
    assert fake_supabase.state.token not in rendered
    _assert_gateway_retains(output, "functions")


async def test_function_list_stays_below_gateway_document_budget_at_field_maxima(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.seed_many_functions(200)
    for function in fake_supabase.state.functions.values():
        function["name"] = "n" * 256
        function["status"] = "s" * 50
        function["entrypoint_path"] = "p" * 256

    output = await _run(
        "supabase.function.list",
        context,
        FunctionListInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
            limit=200,
        ),
    )

    assert MAX_FUNCTION_LIST_OUTPUT_BYTES < 32_768
    assert len(output.model_dump_json().encode("utf-8")) <= MAX_FUNCTION_LIST_OUTPUT_BYTES
    assert len(output.functions) < 200  # type: ignore[attr-defined]
    assert output.truncated is True  # type: ignore[attr-defined]
    _assert_gateway_retains(output, "functions")


async def test_function_list_budget_accounts_for_secret_redaction_expansion(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redactor = SecretRedactor()
    redactor.register("aaaaaa")

    def controlled_sanitize(
        payload: dict[str, object], *, max_document_bytes: int
    ) -> dict[str, object]:
        return sanitize_payload(
            payload,
            redactor=redactor,
            max_document_bytes=max_document_bytes,
        )

    monkeypatch.setattr(
        management_tools,
        "sanitize_payload",
        controlled_sanitize,
        raising=False,
    )
    fake_supabase.state.seed_many_functions(200)
    for function in fake_supabase.state.functions.values():
        function["name"] = "aaaaaa" * 42 + "aaaa"
        function["status"] = "aaaaaa" * 8 + "aa"
        function["entrypoint_path"] = "aaaaaa" * 42 + "aaaa"

    output = await _run(
        "supabase.function.list",
        context,
        FunctionListInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
            limit=200,
        ),
    )
    sanitized = sanitize_payload(
        output.model_dump(mode="json"),
        redactor=redactor,
    )

    assert "functions" in sanitized
    assert "original_size_bytes" not in sanitized
    assert output.truncated is True  # type: ignore[attr-defined]


async def test_logs_use_current_endpoint_and_exact_internally_built_query(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    output = await _run(
        "supabase.logs.read",
        context,
        _logs_input(str(management_connection.id)),
    )

    assert isinstance(output, LogsReadOutput)
    assert MAX_LOG_OUTPUT_BYTES < 32_768
    assert output.logs
    assert set(output.logs[0].model_dump()) == {
        "timestamp",
        "source",
        "event_message",
        "path",
        "status_code",
        "method",
    }
    requests = fake_supabase.state.requests_for(
        "GET", f"/v1/projects/{PROJECT_REF}/analytics/endpoints/logs"
    )
    assert len(requests) == 1
    assert requests[0]["query"] == {
        "sql": [
            "SELECT\n"
            "  timestamp,\n"
            "  source,\n"
            "  event_message,\n"
            "  log_attributes['request.path'] AS path,\n"
            "  toInt32OrZero(log_attributes['response.status_code']) AS status_code,\n"
            "  log_attributes['request.method'] AS method\n"
            "FROM logs\n"
            "WHERE source = 'edge_logs'\n"
            "  AND positionCaseInsensitiveUTF8(event_message, 'quote\\' and \\\\ slash') > 0\n"
            "ORDER BY timestamp DESC\n"
            "LIMIT 17"
        ],
        "iso_timestamp_start": ["2026-08-17T00:00:00Z"],
        "iso_timestamp_end": ["2026-08-17T01:00:00Z"],
    }
    all_paths = [request["path"] for request in fake_supabase.state.requests]
    assert all("logs.all" not in path for path in all_paths)
    assert "logs.all" not in requests[0]["query"]["sql"][0]


async def test_log_text_filter_escapes_controls_without_changing_query_shape(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    hostile = "x'\\\x00\n\r\t); DROP TABLE logs; --"

    await _run(
        "supabase.logs.read",
        context,
        LogsReadInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
            source="edge_logs",
            start="2026-08-17T00:00:00Z",
            end="2026-08-17T01:00:00Z",
            limit=1,
            text_filter=hostile,
        ),
    )

    request = fake_supabase.state.requests_for(
        "GET", f"/v1/projects/{PROJECT_REF}/analytics/endpoints/logs"
    )[0]
    query = request["query"]["sql"][0]
    assert (
        "positionCaseInsensitiveUTF8(event_message, "
        "'x\\'\\\\\\0\\n\\r\\t); DROP TABLE logs; --') > 0" in query
    )
    assert query.count("FROM logs") == 1
    assert query.count("ORDER BY timestamp DESC") == 1
    assert "\x00" not in query


async def test_logs_fail_closed_on_provider_query_error_without_leak(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    marker = "provider-log-error-secret-marker"
    fake_supabase.state.log_error = marker

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.logs.read",
            context,
            _logs_input(str(management_connection.id)),
        )

    assert exc_info.value.code == "provider_query_error"
    assert marker not in str(exc_info.value)


async def test_logs_fail_closed_on_structured_provider_query_error(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.log_error = {"message": "provider-log-error-secret-marker"}

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.logs.read",
            context,
            _logs_input(str(management_connection.id)),
        )

    assert exc_info.value.code == "provider_query_error"
    assert "provider-log-error-secret-marker" not in str(exc_info.value)


async def test_logs_enforce_requested_limit_and_document_byte_budget(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.ignore_log_limit = True
    fake_supabase.state.logs = [
        {
            "timestamp": f"2026-08-17T00:{index % 60:02d}:00Z",
            "source": "edge_logs",
            "event_message": "x" * 4_000,
            "path": "/large",
            "status_code": 200,
            "method": "GET",
            "providerSecret": "log-provider-secret",
        }
        for index in range(100)
    ]

    output = await _run(
        "supabase.logs.read",
        context,
        LogsReadInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
            source="edge_logs",
            start="2026-08-17T00:00:00Z",
            end="2026-08-17T01:00:00Z",
            limit=80,
        ),
    )

    assert isinstance(output, LogsReadOutput)
    assert len(output.logs) <= 80
    assert output.truncated is True
    assert len(output.model_dump_json().encode("utf-8")) <= MAX_LOG_OUTPUT_BYTES
    assert "log-provider-secret" not in output.model_dump_json()
    _assert_gateway_retains(output, "logs")


async def test_log_budget_accounts_for_secret_redaction_expansion(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redactor = SecretRedactor()
    redactor.register("aaaaaa")

    def controlled_sanitize(
        payload: dict[str, object], *, max_document_bytes: int
    ) -> dict[str, object]:
        return sanitize_payload(
            payload,
            redactor=redactor,
            max_document_bytes=max_document_bytes,
        )

    monkeypatch.setattr(
        management_tools,
        "sanitize_payload",
        controlled_sanitize,
        raising=False,
    )
    fake_supabase.state.logs = [
        {
            "timestamp": f"2026-08-17T00:{index % 60:02d}:00Z",
            "source": "edge_logs",
            "event_message": "aaaaaa" * 600,
            "path": "/large",
            "status_code": 200,
            "method": "GET",
        }
        for index in range(100)
    ]

    output = await _run(
        "supabase.logs.read",
        context,
        LogsReadInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
            source="edge_logs",
            start="2026-08-17T00:00:00Z",
            end="2026-08-17T01:00:00Z",
            limit=100,
        ),
    )
    sanitized = sanitize_payload(
        output.model_dump(mode="json"),
        redactor=redactor,
    )

    assert "logs" in sanitized
    assert "original_size_bytes" not in sanitized
    assert output.truncated is True  # type: ignore[attr-defined]


async def test_log_text_filter_rejects_lone_surrogate_before_network(
    fake_supabase: FakeSupabaseServer,
) -> None:
    with pytest.raises(ValidationError):
        LogsReadInput(
            connection_id="connection",
            project_ref=PROJECT_REF,
            source="edge_logs",
            start="2026-08-17T00:00:00Z",
            end="2026-08-17T01:00:00Z",
            text_filter="unsafe\ud800filter",
        )

    assert fake_supabase.state.requests == []


async def test_logs_reject_cross_source_provider_rows(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.logs[0]["source"] = "postgres_logs"

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.logs.read",
            context,
            _logs_input(str(management_connection.id)),
        )

    assert exc_info.value.code == "source_scope_mismatch"


async def test_project_output_rejects_provider_unicode_format_characters(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.project["name"] = "unsafe\u202ename"

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.project.read",
            context,
            ProjectReadInput(
                connection_id=str(management_connection.id),
                project_ref=PROJECT_REF,
            ),
        )

    assert exc_info.value.code == "invalid_provider_response"


async def test_function_output_rejects_provider_unicode_controls(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.functions["hello-world"]["status"] = "unsafe\u0085status"

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.function.list",
            context,
            FunctionListInput(
                connection_id=str(management_connection.id),
                project_ref=PROJECT_REF,
            ),
        )

    assert exc_info.value.code == "invalid_provider_response"


async def test_log_output_rejects_provider_lone_surrogates(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.logs[0]["path"] = "/unsafe\ud800path"

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.logs.read",
            context,
            _logs_input(str(management_connection.id)),
        )

    assert exc_info.value.code == "invalid_provider_response"


async def test_log_event_message_preserves_newlines_and_tabs(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.logs[0]["event_message"] = "line one\nline two\tok"

    output = await _run(
        "supabase.logs.read",
        context,
        _logs_input(str(management_connection.id)),
    )

    assert isinstance(output, LogsReadOutput)
    assert output.logs[0].event_message == "line one\nline two\tok"


async def test_log_event_message_rejects_carriage_returns(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.logs[0]["event_message"] = "unsafe\roverwrite"

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.logs.read",
            context,
            _logs_input(str(management_connection.id)),
        )

    assert exc_info.value.code == "invalid_provider_response"


async def test_deploy_sends_official_multipart_without_wire_idempotency_and_redacts_source(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    payload = _deploy_input(str(management_connection.id))

    output = await _run("supabase.function.deploy", context, payload)

    assert isinstance(output, FunctionInfo)
    assert output.project_ref == PROJECT_REF
    assert output.slug == "hello-world"
    rendered = output.model_dump_json()
    assert "source-secret-marker" not in rendered
    assert fake_supabase.state.token not in rendered
    assert fake_supabase.state.counters["deploy"] == 1
    request = fake_supabase.state.last_requests["deploy"]
    assert request["method"] == "POST"
    assert request["path"] == f"/v1/projects/{PROJECT_REF}/functions/deploy"
    assert request["query"] == {"slug": ["hello-world"]}
    assert request["metadata"] == {
        "entrypoint_path": "index.ts",
        "name": "hello-world",
        "verify_jwt": True,
    }
    assert request["files"] == [
        {"filename": "index.ts", "size": 60},
        {"filename": "helper.ts", "size": 65},
    ]
    assert request["content_type"].startswith("multipart/form-data; boundary=")
    serialized_request = json.dumps(request).casefold()
    assert "source-secret-marker" not in serialized_request
    assert "idempotency" not in serialized_request
    assert "tool_call_id" not in serialized_request


async def test_deploy_rejects_mismatched_provider_slug_after_side_effect(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.deploy_response_slug = "another-function"

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.function.deploy",
            context,
            _deploy_input(str(management_connection.id)),
        )

    assert exc_info.value.code == "function_scope_mismatch"
    assert fake_supabase.state.counters["deploy"] == 1


async def test_deploy_rejects_unsafe_provider_unicode_after_side_effect(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    fake_supabase.state.deploy_response_slug = "unsafe\u202eslug"

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.function.deploy",
            context,
            _deploy_input(str(management_connection.id)),
        )

    assert exc_info.value.code == "invalid_provider_response"
    assert fake_supabase.state.counters["deploy"] == 1


async def test_delete_uses_official_slug_path_and_bounded_output(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    output = await _run(
        "supabase.function.delete",
        context,
        FunctionDeleteInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
            function_slug="hello-world",
        ),
    )

    assert output.model_dump() == {
        "project_ref": PROJECT_REF,
        "function_slug": "hello-world",
        "deleted": True,
    }
    assert fake_supabase.state.counters["delete"] == 1
    request = fake_supabase.state.last_requests["delete"]
    assert request["path"] == f"/v1/projects/{PROJECT_REF}/functions/hello-world"
    assert "idempotency" not in json.dumps(request).casefold()


async def test_input_validation_failure_has_zero_provider_side_effects(
    fake_supabase: FakeSupabaseServer,
) -> None:
    with pytest.raises(ValidationError):
        FunctionDeployInput(
            connection_id="connection",
            project_ref=PROJECT_REF,
            function_slug="hello-world",
            entrypoint_path="missing.ts",
            verify_jwt=True,
            files=[{"path": "index.ts", "content": "x"}],
        )

    assert fake_supabase.state.counters == {"deploy": 0, "delete": 0}
    assert fake_supabase.state.requests == []


@pytest.mark.parametrize("failure", ["redirect", "oversize"])
async def test_management_client_rejects_redirects_and_oversized_streams(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
    failure: str,
) -> None:
    path = f"/v1/projects/{PROJECT_REF}"
    if failure == "redirect":
        fake_supabase.state.redirects[path] = "https://api.supabase.com/v1/projects/elsewhere"
        expected_code = "provider_redirect"
    else:
        fake_supabase.state.project["providerPadding"] = "x" * 530_000
        expected_code = "provider_transport_error"

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.project.read",
            context,
            ProjectReadInput(
                connection_id=str(management_connection.id),
                project_ref=PROJECT_REF,
            ),
        )

    assert exc_info.value.code == expected_code


async def test_management_client_enforces_total_wall_clock_deadline(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = f"/v1/projects/{PROJECT_REF}"
    fake_supabase.state.response_delays[path] = 0.2
    monkeypatch.setattr(management_client, "_TOTAL_TIMEOUT_SECONDS", 0.02)
    started = time.monotonic()

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.project.read",
            context,
            ProjectReadInput(
                connection_id=str(management_connection.id),
                project_ref=PROJECT_REF,
            ),
        )

    assert time.monotonic() - started < 0.15
    assert exc_info.value.code == "provider_transport_error"


async def test_management_client_preserves_external_cancellation(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    path = f"/v1/projects/{PROJECT_REF}"
    fake_supabase.state.response_delays[path] = 5.0
    task = asyncio.create_task(
        _run(
            "supabase.project.read",
            context,
            ProjectReadInput(
                connection_id=str(management_connection.id),
                project_ref=PROJECT_REF,
            ),
        )
    )
    entered_delay = await asyncio.wait_for(
        asyncio.to_thread(fake_supabase.state.response_delay_started.wait, 1.0),
        timeout=1.5,
    )
    assert entered_delay is True
    task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        fake_supabase.state.response_delay_release.set()


@pytest.mark.parametrize("mutation", ["deploy", "delete"])
async def test_one_shot_post_effect_fault_records_exactly_one_effect_then_clears(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
    mutation: str,
) -> None:
    fake_supabase.state.arm_fault(mutation)
    if mutation == "deploy":
        tool_name = "supabase.function.deploy"
        payload: BaseModel = _deploy_input(str(management_connection.id))
    else:
        tool_name = "supabase.function.delete"
        payload = FunctionDeleteInput(
            connection_id=str(management_connection.id),
            project_ref=PROJECT_REF,
            function_slug="hello-world",
        )

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(tool_name, context, payload)

    assert exc_info.value.code == "provider_transport_error"
    assert fake_supabase.state.counters[mutation] == 1
    assert mutation not in fake_supabase.state.faults


def test_fake_fault_and_reset_admin_routes_are_deterministic(
    fake_supabase: FakeSupabaseServer,
) -> None:
    armed = httpx.post(
        f"{fake_supabase.base_url}/_fault",
        json={"mutation": "deploy"},
        timeout=5,
    )
    unknown = httpx.post(
        f"{fake_supabase.base_url}/_fault",
        json={"mutation": "unknown"},
        timeout=5,
    )
    reset = httpx.post(f"{fake_supabase.base_url}/_reset", timeout=5)
    state = httpx.get(f"{fake_supabase.base_url}/_state", timeout=5)

    assert armed.status_code == 200
    assert unknown.status_code == 400
    assert reset.status_code == 200
    assert state.status_code == 200
    assert state.json()["counters"] == {"deploy": 0, "delete": 0}
    assert fake_supabase.state.faults == set()


async def test_malformed_provider_fields_map_to_stable_credential_free_errors(
    context: ToolExecutionContext,
    management_connection,
    fake_supabase: FakeSupabaseServer,
) -> None:
    marker = "malformed-provider-marker"
    fake_supabase.state.functions["hello-world"]["version"] = marker

    with pytest.raises(SupabaseManagementError) as exc_info:
        await _run(
            "supabase.function.list",
            context,
            FunctionListInput(
                connection_id=str(management_connection.id),
                project_ref=PROJECT_REF,
            ),
        )

    assert exc_info.value.code == "invalid_provider_response"
    assert marker not in str(exc_info.value)
